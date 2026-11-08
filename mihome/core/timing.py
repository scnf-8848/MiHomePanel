# SPDX-License-Identifier: GPL-3.0-or-later
"""【临时探针】摄像头取流计时 —— 用完可以整个文件删掉。

这不是功能代码，只在取流链路上打时间点，用来在真实网络里判断首帧到底卡在哪。
不打点的时候（``on == False``）所有函数立刻返回，不建线程、不建文件。

开关
----
环境变量 ``MIHOME_CAMERA_TIMING``，**默认开启**：

* ``MIHOME_CAMERA_TIMING=0``（或 ``false`` / ``no`` / ``off``）关掉
* ``MIHOME_CAMERA_TIMING_PATH=<文件>`` 改输出位置

默认输出 ``<项目根>/logs/camera_timing.log``。

彻底删除
--------
1. 删掉本文件
2. 删掉 ``logs/camera_timing.log``
3. 全局搜索 ``timing.``，把 ``core/relay.py``、``core/camera.py``、
   ``ui/detail.py``、``ui/camera_view.py`` 里的 import 和打点行删掉即可。
   打点只读取量、不参与任何判断，删掉不影响取流行为。

写文件在独立 daemon 线程里做；``mark()`` 只取时间、拼一个元组入队，
不碰磁盘，所以不会把同步 IO 塞进首帧路径。

网络环境
--------
每条记录开头会多一行 ``环境``：活动网卡、本机 IP/前缀、网关、SSID、
摄像头 ``localip`` 与是否同网段、本次偏好档位。采集用 ``ipconfig`` /
``netsh wlan show interfaces``，在后台线程做，结果缓存给下一条记录复用；
采集失败只写「未知」，不影响取流，也不发任何网络请求。

go2rtc 日志
-----------
``watch_log()`` 会起一个只读 tail 线程，把 go2rtc 自己日志里的新增行
（跳过 ``[webrtc]`` / ``[api]`` 这类噪音）镜像成当前记录的
``go2rtc 日志`` 打点，用来拆 ``LoadingMedia`` 内部的建流阶段。
探针关闭时这两项都不启线程。
"""

from __future__ import annotations

import atexit
import ipaddress
import locale
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

_ENV = "MIHOME_CAMERA_TIMING"
_ENV_PATH = "MIHOME_CAMERA_TIMING_PATH"
_OFF = {"0", "false", "no", "off", "disable", "disabled"}

#: 关掉时所有打点都是空操作
on = os.environ.get(_ENV, "").strip().lower() not in _OFF


def _default_path() -> Path:
    raw = os.environ.get(_ENV_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parents[2] / "logs" / "camera_timing.log"


LOG_PATH = _default_path()


# ---------------------------------------------------------------- 网络环境

_IPV4 = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})")
#: 环境快照缓存。采集一次约 100–150 ms，缓存给紧接着的下一条记录用
_net_cache: tuple[float, dict] | None = None
_net_lock = threading.Lock()
_net_busy = False
#: 采集进行中时又开的新记录，采完一起补「环境」行，别让它们停在「采集中」
_net_waiting: list[tuple[int, str, str]] = []


def _decode(raw: bytes) -> str:
    """ipconfig/netsh 在中文 Windows 上是 GBK，控制台又可能是 UTF-8，都试一遍。"""
    for enc in (locale.getpreferredencoding(False), "utf-8", "gbk", "cp936"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _run(cmd: list[str], timeout: float = 2.5) -> str:
    try:
        done = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return _decode(done.stdout or b"")
    except (OSError, subprocess.SubprocessError):
        return ""


def _iface_kind(name: str) -> str:
    low = name.lower()
    if "vmware" in low or "vmnet" in low or "virtualbox" in low:
        return "虚拟网卡"
    if "蓝牙" in name or "bluetooth" in low:
        return "蓝牙"
    if "本地连接*" in name or "local area connection*" in low:
        return "本机热点"
    if "wlan" in low or "wi-fi" in low or "无线" in name:
        return "Wi-Fi"
    if "以太网" in name or "ethernet" in low:
        return "以太网"
    return "其他"


def _adapters() -> list[dict[str, str]]:
    """解析 ``ipconfig``。只取 IPv4/掩码/默认网关，字段缺失就留空。"""
    if sys.platform != "win32":
        return []
    text = _run(["ipconfig"])
    out: list[dict[str, str]] = []
    cur: dict[str, str] | None = None
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if raw[0] not in " \t":
            name = raw.strip().rstrip(":").strip()
            if "适配器" in name:
                name = name.split("适配器", 1)[1].strip()
            cur = {"name": name, "ip": "", "mask": "", "gateway": ""}
            out.append(cur)
            continue
        if cur is None or ":" not in raw:
            continue
        key = raw.split(":", 1)[0].strip().lower()
        found = _IPV4.search(raw.split(":", 1)[1])
        if not found:
            continue
        value = found.group(1)
        if "ipv4" in key and not cur["ip"]:
            cur["ip"] = value
        elif ("子网掩码" in key or "subnet mask" in key) and not cur["mask"]:
            cur["mask"] = value
        elif ("默认网关" in key or "default gateway" in key) and not cur["gateway"]:
            cur["gateway"] = value
    return out


def _ssid() -> str:
    if sys.platform != "win32":
        return "未知"
    text = _run(["netsh", "wlan", "show", "interfaces"])
    for raw in text.splitlines():
        key, sep, value = raw.partition(":")
        if sep and key.strip() == "SSID":
            return value.strip() or "未知"
    return "未知"


def _prefix(mask: str) -> int:
    try:
        return sum(int(part).bit_count() for part in mask.split("."))
    except ValueError:
        return 24


def _same_subnet(ip: str, mask: str, other: str) -> str:
    if not other:
        return "无地址"
    if not ip or not mask:
        return "未知"
    try:
        net = ipaddress.ip_network(f"{ip}/{_prefix(mask)}", strict=False)
        return "同网段" if ipaddress.ip_address(other) in net else "不同网段"
    except ValueError:
        return "未知"


def _collect_net() -> dict:
    return {"adapters": _adapters(), "ssid": _ssid()}


def _cached_net() -> dict | None:
    with _net_lock:
        return _net_cache[1] if _net_cache else None


def _pick_adapter(adapters: list[dict[str, str]], camera_ip: str):
    usable = [a for a in adapters if a["ip"] and not a["ip"].startswith("169.254.")]
    if not usable:
        return None
    if camera_ip:
        same = [a for a in usable if _same_subnet(a["ip"], a["mask"], camera_ip) == "同网段"]
        if same:
            return max(same, key=lambda a: bool(a["gateway"]))
    routed = [a for a in usable if a["gateway"]]
    pool = routed or usable
    return max(pool, key=lambda a: (_iface_kind(a["name"]) == "Wi-Fi", bool(a["gateway"])))


def _format_env(net: dict, camera_ip: str, preference: str) -> str:
    adapter = _pick_adapter(net.get("adapters") or [], camera_ip)
    if adapter is None:
        iface, kind, local, gateway = "未知", "未知", "未知", "未知"
        same = "无地址" if not camera_ip else "未知"
    else:
        kind = _iface_kind(adapter["name"])
        iface = adapter["name"] or "未知"
        prefix = _prefix(adapter["mask"]) if adapter["mask"] else 24
        local = f"{adapter['ip']}/{prefix}" if adapter["ip"] else "未知"
        gateway = adapter["gateway"] or "未知"
        same = _same_subnet(adapter["ip"], adapter["mask"], camera_ip)
    ssid = (net.get("ssid") or "未知") if kind == "Wi-Fi" else "不适用"
    if kind == "本机热点":
        ssid = "由本机提供"
    return (
        f"网卡={iface}({kind}) 本机={local} 网关={gateway} SSID={ssid} "
        f"摄像头={camera_ip or '无地址'} 网段={same} 偏好={preference or '未知'}"
    )


def _refresh_env(rid: int, camera_ip: str, preference: str) -> None:
    """后台采集一次网络环境；``rid`` 非 0 时把结果补进这条记录。

    采集期间又开了新记录就排进等待队列，采完一起补——否则第二条记录会永远
    停在「采集中」。同一时刻只跑一次 ipconfig/netsh。
    """
    global _net_busy
    with _net_lock:
        if _net_busy:
            if rid:
                _net_waiting.append((rid, camera_ip, preference))
            return
        _net_busy = True
        pending = [(rid, camera_ip, preference)] if rid else []

    def work() -> None:
        global _net_busy, _net_cache
        try:
            net = _collect_net()
            with _net_lock:
                _net_cache = (time.monotonic(), net)
                pending.extend(_net_waiting)
                _net_waiting.clear()
            for wait_rid, ip, pref in pending:
                mark_on(wait_rid, "环境", _format_env(net, ip, pref))
        finally:
            with _net_lock:
                _net_busy = False

    threading.Thread(target=work, name="mihome-camera-env", daemon=True).start()


# ---------------------------------------------------------------- go2rtc 日志镜像

_tail_started = False
_tail_path = ""
_tail_lock = threading.Lock()
#: go2rtc 在启动阶段会刷一堆网卡枚举和 API 行，跟建流无关，别塞进计时日志；
#: ``client msg`` 是逐条 RTSP 消息的 trace，真正要看的是它的请求行
_TAIL_SKIP = ("[webrtc]", "[api]", "[log] init", "config path=", "[rtsp] client msg")
#: 单条记录最多镜像这么多行 go2rtc 日志，防止 trace 级别的刷屏把计时日志淹掉
_TAIL_MAX = 200
_tail_rid = 0
_tail_count = 0


def _latest_open() -> int:
    with _records_lock:
        ids = [rid for rid, rec in _records.items() if not rec.closed]
    return max(ids) if ids else 0


def _mirror(stage: str, detail: str) -> None:
    """把一行 go2rtc 日志贴到当前打开的记录上；超量就只留一句提示。"""
    rid = _latest_open()
    if not rid:
        return
    global _tail_rid, _tail_count
    if rid != _tail_rid:
        _tail_rid, _tail_count = rid, 0
    if _tail_count >= _TAIL_MAX:
        if _tail_count == _TAIL_MAX:
            _tail_count += 1
            mark_on(rid, "go2rtc 日志", f"已达 {_TAIL_MAX} 行，后续忽略")
        return
    _tail_count += 1
    mark_on(rid, stage, detail)


def _tail_loop() -> None:
    offset = 0
    pending = ""  # "server request:" 的下一行才是 DESCRIBE/SETUP/PLAY
    swallow = False  # 拿到方法/状态行后，后面的 RTSP 头不再逐行记
    while True:
        path = _tail_path
        try:
            size = os.path.getsize(path)
        except OSError:
            time.sleep(0.1)
            continue
        if size < offset:  # go2rtc 重启会以 wb 截断同一个文件
            offset = 0
        if size == offset:
            time.sleep(0.05)
            continue
        try:
            with open(path, "rb") as fh:
                fh.seek(offset)
                chunk = fh.read()
        except OSError:
            time.sleep(0.1)
            continue
        # 只消费到最后一个换行，避免把写了一半的行截断记进去
        cut = chunk.rfind(b"\n")
        if cut < 0:
            time.sleep(0.05)
            continue
        chunk = chunk[: cut + 1]
        offset += len(chunk)
        for raw in chunk.decode("utf-8", "replace").splitlines():
            line = raw.strip()
            if pending:
                if line:
                    _mirror(pending, line[:160])
                    pending = ""
                    swallow = True
                continue
            if swallow:
                if not line:
                    swallow = False
                continue
            if not line or any(skip in line for skip in _TAIL_SKIP):
                continue
            if "[rtsp]" in line and (
                "server request" in line
                or "server response" in line
                or "client request" in line
                or "client response" in line
            ):
                pending = (
                    "go2rtc RTSP 请求" if "request" in line else "go2rtc RTSP 响应"
                )
                continue
            _mirror("go2rtc 日志", line[:200])
        time.sleep(0.02)


def watch_log(path: str | os.PathLike[str]) -> None:
    """把 go2rtc 日志的新增行镜像进当前记录。探针关掉时是空操作。"""
    if not on:
        return
    global _tail_path, _tail_started
    _tail_path = str(path)
    with _tail_lock:
        if _tail_started:
            return
        _tail_started = True
    threading.Thread(target=_tail_loop, name="mihome-camera-go2rtc-log", daemon=True).start()


class _Record:
    __slots__ = ("closed", "did", "label", "marks", "rid", "t0")

    def __init__(self, rid: int, label: str, did: str) -> None:
        self.rid = rid
        self.label = label
        self.did = did
        self.t0 = time.perf_counter()
        self.marks: list[tuple[int, str, str]] = []
        self.closed = False


_q: queue.SimpleQueue[object] = queue.SimpleQueue()
_writer: threading.Thread | None = None
_writer_lock = threading.Lock()
_records: dict[int, _Record] = {}
_records_lock = threading.Lock()
_local = threading.local()
_next_id = 0


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]


def _write_loop() -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        handle = LOG_PATH.open("a", encoding="utf-8")
    except OSError:
        # 写不了就静默丢弃：探针绝不能反过来影响取流
        while _q.get() is not None:
            pass
        return
    with handle:
        if handle.tell() == 0:
            handle.write(
                "# 【临时探针】mihome/core/timing.py；"
                "MIHOME_CAMERA_TIMING=0 关闭；删掉本文件即可撤除\n"
            )
        while True:
            item = _q.get()
            if item is None:
                handle.flush()
                return
            if isinstance(item, tuple):
                item = _format_mark(*item)  # type: ignore[arg-type]
            try:
                handle.write(str(item) + "\n")
                handle.flush()
            except OSError:
                return


def _format_mark(rid: int, ms: int, ts: float, stage: str, detail: str) -> str:
    line = f"{_fmt(ts)}  #{rid}  +{ms:>6} ms  {stage}"
    if detail:
        line += f"  ({detail})"
    return line


def _ensure_writer() -> None:
    global _writer
    if _writer is not None:
        return
    with _writer_lock:
        if _writer is not None:
            return
        thread = threading.Thread(
            target=_write_loop, name="mihome-camera-timing", daemon=True
        )
        thread.start()
        _writer = thread


def _emit(item: object) -> None:
    _ensure_writer()
    _q.put(item)


def _resolve(token: int | None) -> int:
    if token:
        return int(token)
    return int(getattr(_local, "token", 0) or 0)


def bind(token: int | None) -> None:
    """把当前线程的后续 ``mark()`` 绑到某条记录上。"""
    _local.token = int(token or 0)


def current() -> int:
    return int(getattr(_local, "token", 0) or 0)


def begin(label: str, did: str = "", *, context: dict[str, str] | None = None) -> int:
    """开一条记录，返回 token；关掉探针时返回 0。

    ``context`` 目前只认两个键：``camera_ip``（摄像头 ``localip``）和
    ``preference``（``camera_network`` 档位），用来拼记录开头的「环境」行。
    """
    if not on:
        return 0
    global _next_id
    with _records_lock:
        _next_id += 1
        rid = _next_id
        _records[rid] = _Record(rid, label, did)
    now = time.time()
    head = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    _emit("")
    _emit(
        f"================ 记录 #{rid}  {head}  "
        f"{label}  did={did} ================"
    )
    mark("记录开始", f"通道={label}", token=rid)
    ctx = dict(context or {})
    camera_ip = str(ctx.get("camera_ip", "") or "")
    preference = str(ctx.get("preference", "") or "")
    net = _cached_net()
    if net is None:
        # 第一次记录：采集在后台做，拿到后补进这条记录
        mark("环境", "采集中（后台读取网卡/网关/SSID）", token=rid)
        _refresh_env(rid, camera_ip, preference)
    else:
        mark("环境", _format_env(net, camera_ip, preference), token=rid)
        _refresh_env(0, camera_ip, preference)
    return rid


def set_label(label: str, *, token: int | None = None) -> None:
    rid = _resolve(token)
    if not rid:
        return
    with _records_lock:
        rec = _records.get(rid)
        if rec is not None and not rec.closed:
            rec.label = label


def mark(stage: str, detail: str = "", *, token: int | None = None) -> None:
    if not on:
        return
    rid = _resolve(token)
    if not rid:
        return
    now = time.perf_counter()
    with _records_lock:
        rec = _records.get(rid)
        if rec is None or rec.closed:
            return
        ms = int((now - rec.t0) * 1000)
        rec.marks.append((ms, stage, detail))
    _emit((rid, ms, time.time(), stage, detail))


def mark_on(token: int | None, stage: str, detail: str = "") -> None:
    mark(stage, detail, token=token)


def end(result: str = "", detail: str = "", *, token: int | None = None) -> None:
    if not on:
        return
    rid = _resolve(token)
    if not rid:
        return
    with _records_lock:
        rec = _records.pop(rid, None)
        if rec is None or rec.closed:
            return
        rec.closed = True
        label = rec.label
        marks = list(rec.marks)
    total = int((time.perf_counter() - rec.t0) * 1000)
    tail = f"  通道={label}  结果={result}"
    if detail:
        tail += f"  {detail}"
    _emit("")
    _emit(
        f"================ 记录 #{rid} 结束  总耗时 {total} ms{tail} "
        "================"
    )
    prev = 0
    for ms, stage, extra in marks:
        line = f"  {ms:>6} ms  Δ{ms - prev:>6} ms  {stage}"
        if extra:
            line += f"  ({extra})"
        _emit(line)
        prev = ms
    _emit("")


def end_on(token: int | None, result: str = "", detail: str = "") -> None:
    end(result, detail, token=token)


def _shutdown() -> None:
    if _writer is not None:
        _q.put(None)


atexit.register(_shutdown)

__all__ = [
    "LOG_PATH",
    "begin",
    "bind",
    "current",
    "end",
    "end_on",
    "mark",
    "mark_on",
    "on",
    "set_label",
    "watch_log",
]
