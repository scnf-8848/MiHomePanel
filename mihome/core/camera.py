"""摄像头实时画面：把 P2P 取流交给可选的 go2rtc 外部进程。

米家摄像头没有 ``stream_address``，码流走 TUTK/MISS 私有 P2P（UDP 打洞 + 帧加密）。
厂商 SDK 是商业授权、不能进 GPL 发行包；``go2rtc``（MIT）已经把这套协议栈实现完整，
本机实测 ``chuangmi.camera.069a01`` 能出画。所以这里的职责很窄：

* 找到 go2rtc 可执行文件（用户自备，或跑 ``scripts/install_go2rtc.py`` 下载）
* 用登录凭据生成一份**只监听 127.0.0.1** 的配置
* 起子进程、等 API 就绪、把每个摄像头的 ``rtsp://`` 地址交出去
* 退出时收干净

解码与渲染不在这里，那是 ``ui/camera_view.py`` 的事。

凭据只写 ``<data>/go2rtc/go2rtc.yaml``，不进日志、不进仓库；端口从 1984 / 8554
往后找空位，避免跟用户自己的 go2rtc 或别的服务打架。

**没装 go2rtc 时功能降级为一句明确的提示**，不静默失败——这是外部依赖，不是本程序
能兜底的东西。
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import paths, relay, timing

API_PORT = 1984
RTSP_PORT = 8554
START_TIMEOUT = 12.0
# 上次异常退出留下的进程记录。正常退出会删掉，留着就说明是崩溃/强杀
PIDFILE = "runtime.json"
# go2rtc 日志里代表"这条流被对端掐了"的片段
_FAILURE_MARKS = ("cs2: EOF", "read media")
# `MIHOME_CAMERA_AUDIO` 取这些值时整条音轨都不要（排障用，不是静音开关）
_AUDIO_OFF = ("0", "off", "false", "no")

#: 云台指令编号，和自建 go2rtc 的 ``/api/xiaomi/ptz`` 对齐（MISS 0x112 的 operation）
PTZ_LEFT, PTZ_RIGHT, PTZ_UP, PTZ_DOWN, PTZ_CALIBRATE = 1, 2, 3, 4, 5
PTZ_OPS = frozenset({PTZ_LEFT, PTZ_RIGHT, PTZ_UP, PTZ_DOWN, PTZ_CALIBRATE})


def audio_enabled() -> bool:
    """这次取的流要不要带音轨（默认要；``MIHOME_CAMERA_AUDIO=0`` 关掉）。

    注意它和"静音"不是一回事：静音由界面上的 ``QAudioOutput.setMuted()`` 完成，
    音轨照常收、点一下就能出声；这里关的是**整条音轨**，关掉之后界面上的喇叭
    会因为 ``hasAudio()`` 为假自动消失。只在排障时用（怀疑音频拖慢首帧、或跨网
    带宽紧张），不做成设置项——它不该是日常操作。
    """
    flag = os.environ.get("MIHOME_CAMERA_AUDIO", "").strip().lower()
    return flag not in _AUDIO_OFF


class CameraError(RuntimeError):
    """取流失败。``kind`` 给 UI 决定是提示重试还是提示安装。"""

    def __init__(self, message: str, kind: str = "unknown") -> None:
        super().__init__(message)
        self.kind = kind


def is_camera(model: str, category: str = "") -> bool:
    """摄像头品类判定。品类来自 catalog，model 是兜底。"""
    return category == "camera" or "camera" in (model or "").lower()


# 局域网可达性探测：这些摄像头在 UDP 32108 上应答 CS2 的 LAN search 包。
# 实测同网段摄像头 8.9 ms 回包；不可达地址按超时处理。
PROBE_PORT = 32108
_PROBE_QUERY = b"\xf1\x30\x00\x00"  # LAN search
_PROBE_REPLY = b"\xf1\x41"  # LAN search 的应答（PUNCH）
#: 发几个包、每个等多久。3 个包 × 120 ms 最多容忍 2 次丢失，总预算 360 ms
_PROBE_TRIES = 3
_PROBE_GAP = 0.12


def probe_local(
    ip: str,
    *,
    port: int = PROBE_PORT,
    tries: int = _PROBE_TRIES,
    gap: float = _PROBE_GAP,
) -> tuple[bool, float, str]:
    """快速判断摄像头的局域网地址是否可达（发 CS2 发现包等应答）。

    返回 ``(是否可达, 耗时 ms, 说明)``。只发 UDP 探测，不建立 P2P 会话、不占观看位，
    因此可以放心在"取流前"跑；调用方负责放到后台线程。

    容忍丢包：连发 ``tries`` 个包，任意一个拿到应答就算通；全部超时才判不可达，
    最坏耗时 ``tries * gap``（默认 360 ms）。
    """
    started = time.perf_counter()
    if not ip:
        return False, 0.0, "无地址"
    try:
        addr = socket.gethostbyname(ip)
    except OSError as exc:
        return False, (time.perf_counter() - started) * 1000, f"地址解析失败：{exc}"
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError as exc:
        return False, (time.perf_counter() - started) * 1000, f"建套接字失败：{exc}"
    with sock:
        for _ in range(tries):
            try:
                sock.sendto(_PROBE_QUERY, (addr, port))
            except OSError as exc:
                return False, (time.perf_counter() - started) * 1000, f"发送失败：{exc}"
            deadline = time.monotonic() + gap
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                sock.settimeout(left)
                try:
                    data, _src = sock.recvfrom(64)
                except TimeoutError:
                    break
                except OSError as exc:
                    return False, (time.perf_counter() - started) * 1000, f"接收失败：{exc}"
                if data.startswith(_PROBE_REPLY):
                    elapsed = (time.perf_counter() - started) * 1000
                    return True, elapsed, f"{len(data)} 字节应答"
    elapsed = (time.perf_counter() - started) * 1000
    return False, elapsed, f"{tries} 个包均无应答"


def _exe_name() -> str:
    return "go2rtc.exe" if sys.platform == "win32" else "go2rtc"


def program_dirs() -> list[Path]:
    """程序自己能放资源的目录，按优先级排。

    源码运行时只有一个：仓库根。**打包后不是这样**——PyInstaller 会把模块塞进
    ``_internal``（onedir）或临时解包目录（onefile），``Path(__file__).parents[2]``
    算回仓库根这条就失效了。要显式补上两个候选：exe 所在目录与其解包目录，否则
    开发机上好好的，打包版一律"没有找到 go2rtc"。
    """
    dirs: list[Path] = []
    if getattr(sys, "frozen", False):
        try:
            dirs.append(Path(sys.executable).resolve().parent)
        except OSError:
            pass
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        dirs.append(Path(meipass))
    dirs.append(Path(__file__).resolve().parents[2])

    seen: set[str] = set()
    out: list[Path] = []
    for d in dirs:
        key = os.path.normcase(str(d))
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def find_binary(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """按"显式指定 → 环境变量 → 程序目录 → 数据目录 → PATH"找 go2rtc。

    程序目录排在数据目录前面：打包版把 go2rtc.exe 一起装在安装目录里，它跟着程序
    版本走，是默认项；数据目录那份是用户自己下载/编译的，属于覆盖，要压过它请用
    ``MIHOME_GO2RTC`` 或 ``explicit``。
    """
    exe = _exe_name()
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env = os.environ.get("MIHOME_GO2RTC", "").strip()
    if env:
        candidates.append(Path(env).expanduser())
    for root in program_dirs():
        candidates.append(root / "go2rtc" / exe)
        candidates.append(root / "tools" / "go2rtc" / exe)
    candidates.append(paths.data_dir() / "go2rtc" / exe)
    for c in candidates:
        if c.is_file():
            return c
    found = shutil.which("go2rtc")
    return Path(found) if found else None


def missing_binary_hint() -> str:
    """缺 go2rtc 时该怎么说。

    打包版没有 ``scripts/`` 目录，让用户"运行 install_go2rtc.py"是一句空话，
    得改成"把它放到这个位置"。
    """
    if getattr(sys, "frozen", False):
        target = program_dirs()[0] / "go2rtc" / _exe_name()
        return (
            f"没有找到 go2rtc。把 go2rtc 放到 {target}，"
            "或设置环境变量 MIHOME_GO2RTC 指向它"
        )
    return (
        "没有找到 go2rtc。运行 scripts/install_go2rtc.py 自动下载，"
        "或设置环境变量 MIHOME_GO2RTC 指向 go2rtc.exe"
    )


def _port_free(port: int) -> bool:
    # Windows 上 SO_REUSEADDR 允许 bind 到别人已经 listen 的端口（Linux 不允许），
    # 拿它探测会误判成空闲，于是新实例抢不到端口、start() 却连上了旧实例的 API。
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if sys.platform == "win32":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _pick_port(preferred: int, span: int = 20) -> int:
    for port in range(preferred, preferred + span):
        if _port_free(port):
            return port
    raise CameraError(
        f"本机没有可用端口（试过 {preferred}–{preferred + span - 1}）", "port"
    )


def _process_image(pid: int) -> str:
    """进程映像名。只用来确认这个 pid 没被别的程序复用。"""
    if sys.platform != "win32":
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return ""
        return raw.split(b"\0")[0].decode("utf-8", "replace")
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdin=subprocess.DEVNULL,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    first = out.strip().splitlines()[0] if out.strip() else ""
    return first.split(",")[0].strip().strip('"').lower()


def _command_line(pid: int) -> str:
    """进程命令行，用来确认"这个 go2rtc 是我们起的"。拿不到就返回空。"""
    if sys.platform != "win32":
        return _process_image(pid)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    attempts = [
        ["wmic", "process", "where", f"processid={pid}", "get", "commandline", "/value"],
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine",
        ],
    ]
    for cmd in attempts:
        try:
            out = subprocess.run(
                cmd, check=False, capture_output=True, text=True,
                errors="replace", timeout=8, creationflags=flags,
                stdin=subprocess.DEVNULL,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        out = out.replace("CommandLine=", "").strip()
        if out:
            return out
    return ""


def _kill_pid(pid: int) -> None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if sys.platform == "win32":
        cmd = ["taskkill", "/F", "/PID", str(pid)]
    else:
        cmd = ["kill", "-9", str(pid)]
    try:
        subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            timeout=8,
            creationflags=flags,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _log_age(line: str, now: datetime) -> float | None:
    """go2rtc 日志行首是 `HH:MM:SS.mmm`，算它离现在多少秒；认不出就返回 None。"""
    head = line.split(" ", 1)[0].strip()
    try:
        stamp = datetime.strptime(head, "%H:%M:%S.%f").time()
    except ValueError:
        return None
    age = (now - datetime.combine(now.date(), stamp)).total_seconds()
    # 跨零点：刚过 00:00 时那行的时间在"未来"，补一天
    return age + 86400 if age < -60 else age


def cleanup_stale() -> int | None:
    """清掉上次异常退出留下的 go2rtc，返回被清掉的 pid。

    程序被强杀时 Windows 不会跟着结束子进程，那个 go2rtc 会一直活着占住
    1984 / 8554；下一次启动只能漂到 1985 / 8555，开几次就攒几个。
    它自己不再占摄像头的观看位（消费者断开后生产者就停了），但要收掉。

    只认自己写的进程记录，并且三样都对上才动手：pid 还活着、映像名是 go2rtc、
    命令行里有本程序的配置文件路径。用户自己跑的 go2rtc 不会命中。
    """
    record = paths.data_dir() / "go2rtc" / PIDFILE
    try:
        info = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = int(info.get("pid") or 0)
    config = str(info.get("config") or "")
    killed: int | None = None
    if pid > 0 and _process_image(pid) == "go2rtc.exe":
        line = _command_line(pid)
        if not line or (config and config in line):
            _kill_pid(pid)
            killed = pid
    record.unlink(missing_ok=True)
    return killed


@dataclass
class CameraSource:
    """一台摄像头在 go2rtc 配置里的最小信息。

    ``remote=True`` 时 ``host/port`` 指向本机的 relay 代理（``127.0.0.1:<本机端口>``），
    ``localip`` 只作为设备信息保留、不再参与取流；局域网模式则完全沿用 ``localip``。
    """

    did: str
    model: str
    localip: str
    name: str = ""
    online: bool = True
    remote: bool = False
    host: str = ""
    port: int = 0


def _same_source(old: CameraSource | None, new: CameraSource) -> bool:
    """只看会影响取流地址的字段；改个显示名不该重启 go2rtc。"""
    return (
        old is not None
        and old.online == new.online
        and old.remote == new.remote
        and old.model == new.model
        and old.localip == new.localip
        and old.host == new.host
        and old.port == new.port
    )


class Go2rtcRuntime:
    """一个 go2rtc 子进程。进程内只应有一个实例（同一台摄像头只允许一个 P2P 会话）。"""

    def __init__(self, binary: Path, user_id: str, pass_token: str, *, region: str = "CN") -> None:
        self.binary = Path(binary)
        self.user_id = str(user_id)
        self.pass_token = str(pass_token)
        self.region = region
        self.api_port = 0
        self.rtsp_port = 0
        self._proc: subprocess.Popen[bytes] | None = None
        self._sources: dict[str, CameraSource] = {}
        self._workdir = paths.data_dir() / "go2rtc"
        self._log_path = self._workdir / "go2rtc.log"

    # -- 状态 ------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def dids(self) -> set[str]:
        return set(self._sources)

    @property
    def sources(self) -> list[CameraSource]:
        return list(self._sources.values())

    def url(self, did: str) -> str:
        if not self.running:
            raise CameraError("取流服务没有在运行", "not_running")
        src = self._sources.get(did)
        if src is None:
            raise CameraError("这台摄像头还没有登记到取流服务", "unknown_device")
        if not src.online:
            raise CameraError("设备离线，无法取流", "offline")
        if src.remote:
            if not src.host or not src.port:
                raise CameraError("远程通道还没有准备好", "relay")
        elif not src.localip:
            raise CameraError(
                "这台摄像头没有上报局域网地址（可能不在同一局域网），无法取流", "no_ip"
            )
        return f"rtsp://127.0.0.1:{self.rtsp_port}/{self.stream_name(did)}"

    @staticmethod
    def stream_name(did: str) -> str:
        return f"cam_{did}"

    def _stream_names(self) -> set[str]:
        return {
            self.stream_name(src.did)
            for src in self._sources.values()
            if src.online and (src.remote or src.localip)
        }

    # -- 配置 ------------------------------------------------------------
    def _stream_url(self, src: CameraSource) -> str:
        # 默认高清（subtype=0）。想更快出画面可以设 MIHOME_CAMERA_SUBTYPE=sd：
        # 关键帧从 ~177KB 降到 ~21KB，解码也便宜，代价是清晰度明显下降。
        # 注意它**消不掉等 GOP 的那 3–4 秒**——那是摄像头编码器决定的。
        subtype = os.environ.get("MIHOME_CAMERA_SUBTYPE", "").strip() or "0"
        # 声音：`xiaomi://` 的 `audio` 参数**不写就是开**（go2rtc 默认 "1"，直接拼进
        # MISS 的 enableaudio）。以前跨网分支写死 `audio=0`，现在要出声就得让设备发。
        # 静音是客户端 `QAudioOutput.setMuted()`，**不是**关掉这条音轨——关轨要重建
        # producer，点一下喇叭就得重新等 GOP。所以只有排障才用环境变量关：
        # `MIHOME_CAMERA_AUDIO=0`（音频疑似拖慢首帧 / 跨网带宽不够时）。
        audio = "" if audio_enabled() else "&audio=0"
        if src.remote:
            # 指向本机 relay 代理。必须显式 transport=udp：代理只搬 UDP，
            # go2rtc 默认会先 UDP 再 TCP，TCP 那一轮只会白等。
            return (
                f"xiaomi://{self.user_id}:{self.region}@{src.host}:{src.port}"
                f"?did={src.did}&model={src.model}&channel=0&subtype={subtype}"
                f"{audio}&transport=udp"
            )
        return (
            f"xiaomi://{self.user_id}:{self.region}@{src.localip}"
            f"?did={src.did}&model={src.model}&channel=0&subtype={subtype}{audio}"
        )

    def _write_config(self) -> Path:
        self._workdir.mkdir(parents=True, exist_ok=True)
        # 取流计时开着（MIHOME_CAMERA_TIMING=1）时默认用 trace：
        # LoadingMedia 那几秒只能靠 go2rtc 的 RTSP 请求行（DESCRIBE/SETUP/PLAY）拆开。
        # 平时（计时关闭）是 warn；只想看 go2rtc 自己的日志可单独 MIHOME_GO2RTC_LOG=debug。
        level = os.environ.get("MIHOME_GO2RTC_LOG", "").strip()
        if not level:
            level = "trace" if timing.on else "warn"
        lines = [
            "log:",
            f"  level: {level}",
            "api:",
            f'  listen: "127.0.0.1:{self.api_port}"',
            "rtsp:",
            f'  listen: "127.0.0.1:{self.rtsp_port}"',
            "xiaomi:",
            f'  "{self.user_id}": "{self.pass_token}"',
            "streams:",
        ]
        for src in self._sources.values():
            if not src.online or not (src.remote or src.localip):
                continue
            lines.append(f'  "{self.stream_name(src.did)}":')
            lines.append(f'    - "{self._stream_url(src)}"')
        cfg = self._workdir / "go2rtc.yaml"
        cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            os.chmod(cfg, 0o600)
        except OSError:
            pass
        return cfg

    # -- 启停 ------------------------------------------------------------
    def start(self, sources: Sequence[CameraSource]) -> None:
        if self.running:
            return
        self._sources = {s.did: s for s in sources}
        self.api_port = _pick_port(API_PORT)
        self.rtsp_port = _pick_port(RTSP_PORT, span=40)
        # 端口可能和 api 撞，避开
        if self.rtsp_port == self.api_port:
            self.rtsp_port = _pick_port(self.rtsp_port + 1, span=40)
        cfg = self._write_config()
        timing.mark(
            "go2rtc 配置写好",
            f"api={self.api_port} rtsp={self.rtsp_port}",
        )

        flags = 0
        if sys.platform == "win32":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            with open(self._log_path, "wb") as log:
                self._proc = subprocess.Popen(
                    [str(self.binary), "-config", str(cfg)],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    cwd=str(self._workdir),
                    creationflags=flags,
                )
        except OSError as exc:
            self._proc = None
            raise CameraError(f"go2rtc 启动失败：{exc}", "start") from exc
        timing.mark("go2rtc 子进程已拉起", f"pid={self._proc.pid}")
        # 把 go2rtc 自己的日志行镜像进主计时日志；探针关掉时 watch_log 直接返回
        timing.watch_log(self._log_path)
        timing.mark("go2rtc 日志文件", str(self._log_path))

        self._write_record()

        deadline = time.time() + START_TIMEOUT
        # 探测 /api/streams 而不是 /api：万一端口被别的 go2rtc 占着，只有看到
        # 我们自己登记的流才算"起来了"，否则会拿别人的实例去取流、报错还看不懂
        probe = f"http://127.0.0.1:{self.api_port}/api/streams"
        want = self._stream_names()
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                tail = self.tail_log()
                self.stop()
                raise CameraError(f"go2rtc 启动后立即退出。{tail}", "start")
            try:
                with urllib.request.urlopen(probe, timeout=1) as resp:
                    if resp.status == 200:
                        body = json.loads(resp.read() or b"{}")
                        if want <= set(body):
                            # API 比协议模块先就绪：`xiaomi` 排在 init 列表靠后，
                            # 这时候发来的 RTSP 请求会撞上"handler 还没注册"、报
                            # unsupported scheme。等一小会儿比再探一次端点更省事。
                            time.sleep(0.3)
                            timing.mark(
                                "go2rtc /api 就绪",
                                f"api={self.api_port} rtsp={self.rtsp_port}",
                            )
                            return
            except (urllib.error.URLError, OSError, ValueError):
                time.sleep(0.2)
            else:
                time.sleep(0.2)
        timing.mark("go2rtc 启动超时", f"{START_TIMEOUT:.0f}s")
        self.stop()
        raise CameraError(
            f"go2rtc 在 {START_TIMEOUT:.0f} 秒内没有就绪。{self.tail_log()}", "start"
        )

    def restart(self, sources: Sequence[CameraSource]) -> None:
        self.stop()
        self.start(sources)

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        timing.mark("go2rtc 子进程停止")
        try:
            proc.terminate()
            proc.wait(timeout=4)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                # 进程已经自己退了，或者系统拒绝结束它——都没有可补救的动作
                self._proc = None
        self._forget_record(proc)

    # -- 进程记录 --------------------------------------------------------
    def _record_path(self) -> Path:
        return self._workdir / PIDFILE

    def _write_record(self) -> None:
        """落一份 pid 记录，供下次启动清理孤儿进程。正常退出会删掉。"""
        proc = self._proc
        if proc is None:
            return
        try:
            self._record_path().write_text(
                json.dumps(
                    {
                        "pid": proc.pid,
                        "api_port": self.api_port,
                        "rtsp_port": self.rtsp_port,
                        "config": str(self._workdir / "go2rtc.yaml"),
                    }
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _forget_record(self, proc: subprocess.Popen[bytes] | None = None) -> None:
        """只删自己那条：pid 对不上就别动，免得把别人写的记录删了。"""
        path = self._record_path()
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if proc is not None and int(info.get("pid") or 0) != proc.pid:
            return
        path.unlink(missing_ok=True)

    # -- 排障 ------------------------------------------------------------
    def tail_log(self, lines: int = 6) -> str:
        """日志末尾几行，用于把"为什么没起来"说出来。凭据不落日志。"""
        try:
            text = self._log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        picked = [ln.strip() for ln in text.splitlines() if ln.strip()][-lines:]
        return "｜".join(picked)

    def recent_failure(self, window: float = 20.0) -> str:
        """最近 ``window`` 秒内日志里有没有"流被对端掐断"。

        返回 ``"eof"``（设备断开 / 观看位被占满）或空串。只认新鲜记录：日志是整个
        进程共用的，昨天那条 EOF 不能让今天的错误背锅。
        """
        try:
            text = self._log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        now = datetime.now()
        for line in reversed(text.splitlines()[-200:]):
            if not any(mark in line for mark in _FAILURE_MARKS):
                continue
            age = _log_age(line, now)
            if age is None or age <= window:
                return "eof"
        return ""


class CameraService:
    """进程级单例：一个 go2rtc 实例服务所有摄像头。"""

    def __init__(self) -> None:
        self._runtime: Go2rtcRuntime | None = None
        self._binary: Path | None = None
        self._cleaned_stale = False
        #: 同一时刻只保留一条 relay 会话（一台摄像头只有有限的观看位）
        self._relay: relay.RelaySession | None = None
        self._relay_did = ""
        # 详情页与卡片墙会从不同线程调 open/release（墙面取址在工作线程）。
        # ``open`` 可能阻塞数秒，串行是预期——宁可让墙面多等一会儿，也不能让
        # 两条会话同时改 go2rtc 的源、互相把 relay 抢掉。
        self._lock = threading.RLock()
        #: 这个 go2rtc 构建带不带云台接口；None = 还没问过
        self._ptz_ok: bool | None = None

    @property
    def available(self) -> bool:
        return find_binary() is not None

    @property
    def binary(self) -> Path | None:
        return find_binary()

    def open(
        self,
        client: Any,
        node: Any,
        *,
        remote: bool = False,
        guard: Any = None,
    ) -> str:
        """加锁的公开入口；``guard`` 在真正动 relay / go2rtc 前再确认一次是否还需取流。

        卡片墙的取址在工作线程里跑，用户可能刚点播放就进了详情页。``guard``
        在拿到锁之后才调用，所以"墙面的旧任务"不会在详情页已经接管 relay 之后
        再改一次 go2rtc 的源。
        """
        with self._lock:
            if guard is not None and not guard():
                raise CameraError("取流已取消", "cancelled")
            return self._open(client, node, remote=remote)

    def _open(self, client: Any, node: Any, *, remote: bool = False) -> str:
        """确保 go2rtc 已带这台摄像头启动，返回本机 rtsp 地址。

        ``remote=True`` 时先建立（或复用）relay 会话，把 go2rtc 的源指向本机代理；
        否则走原来的局域网直连。局域网分支的 URL、启动顺序和错误文案保持不变。
        """
        timing.mark("取流 open 开始", f"remote={remote}")
        model = str(getattr(node, "model", "") or "")
        if not is_camera(model):
            raise CameraError("这不是摄像头", "not_camera")
        if not bool(getattr(node, "online", True)):
            raise CameraError("设备离线，无法取流", "offline")
        localip = str(getattr(node, "localip", "") or "")
        if not remote and not localip:
            raise CameraError("没有拿到这台摄像头的局域网地址，无法取流", "no_ip")

        binary = find_binary()
        if binary is None:
            raise CameraError(missing_binary_hint(), "no_binary")

        auth = getattr(client.raw(), "auth_data", {}) or {}
        user_id = str(auth.get("userId", "") or "")
        pass_token = str(auth.get("passToken", "") or "")
        if not user_id or not pass_token:
            raise CameraError("登录凭据不完整，请重新扫码登录", "auth")

        if not self._cleaned_stale:
            # 上次是崩溃/强杀退出的：先把那个还活着、占着端口的 go2rtc 收掉，
            # 否则这次会漂到 1985/8555，开几次攒几个
            self._cleaned_stale = True
            cleanup_stale()

        did = str(getattr(node, "did", ""))
        if remote:
            session = self._ensure_relay(client, did)
            timing.mark("relay 通道就绪", f"proxy=127.0.0.1:{session.port}")
            src = CameraSource(
                did=did,
                model=model,
                localip=localip,
                name=str(getattr(node, "name", "") or ""),
                online=True,
                remote=True,
                host="127.0.0.1",
                port=session.port,
            )
        else:
            # 转回局域网（或本来就是）：远程会话没有必要继续占着
            self._close_relay()
            timing.mark("局域网直连选定", localip)
            src = CameraSource(
                did=did,
                model=model,
                localip=localip,
                name=str(getattr(node, "name", "") or ""),
                online=True,
            )

        if self._runtime is None or not self._runtime.running or self._binary != binary:
            timing.mark("go2rtc 首次启动")
            self._stop_runtime()  # 旧实例必须先收掉，否则会漏一个进程和两个端口
            self._runtime = Go2rtcRuntime(binary, user_id, pass_token)
            self._binary = binary
            self._runtime.start([src])
        elif self._runtime.user_id != user_id or self._runtime.pass_token != pass_token:
            # 重新登录过：配置里的 passToken 要跟着换，否则下一次启动会用旧凭据
            timing.mark("go2rtc 重启：登录凭据变化")
            self._stop_runtime()
            self._runtime = Go2rtcRuntime(binary, user_id, pass_token)
            self._binary = binary
            self._runtime.start([src])
        else:
            current = {s.did: s for s in self._runtime.sources}.get(did)
            if _same_source(current, src):
                timing.mark("go2rtc 复用现有流")
                return self._runtime.url(did)
            timing.mark("go2rtc 重启：取流源变化")
            sources = [s for s in self._runtime.sources if s.did != did]
            if remote:
                # 切到另一台摄像头时，上一台的远程源和会话都要让位
                sources = [s for s in sources if not s.remote]
            sources.append(src)
            self._runtime.restart(sources)
        url = self._runtime.url(src.did)
        timing.mark("RTSP 地址下发", url)
        return url

    # -- relay 会话 ------------------------------------------------------
    def _ensure_relay(self, client: Any, did: str) -> relay.RelaySession:
        """复用同一条会话；换摄像头或授权将到期时先收旧再建新。"""
        session = self._relay
        if session is not None and self._relay_did == did and session.usable:
            timing.mark("复用 relay 会话", f"port={session.port}")
            return session
        self._close_relay()
        timing.mark("申请远程 P2P 授权")
        try:
            # 借用 Client 的串行锁：Session 不是线程安全的，不能和轮询并发打请求
            vendor = client.locked(
                lambda: relay.request_vendor(client.raw(), did)
            )
            timing.mark("创建 relay 会话")
            session = relay.RelaySession(vendor)
            session.start()
        except relay.RelayError as exc:
            timing.mark("relay 启动失败", str(exc))
            raise CameraError(str(exc), f"relay_{exc.kind}") from exc
        except OSError as exc:
            timing.mark("relay 启动失败", str(exc))
            raise CameraError(f"远程中继启动失败：{exc}", "relay") from exc
        self._relay = session
        self._relay_did = did
        timing.mark("relay 会话可用", f"port={session.port}")
        return session

    def _close_relay(self) -> None:
        session = self._relay
        self._relay = None
        self._relay_did = ""
        if session is not None:
            timing.mark("释放 relay 会话")
            session.close()

    def release(self, did: str = "") -> None:
        """界面不再观看（关闭详情页 / 闲置释放）：放掉远程会话，别继续占观看位。

        局域网路径不经过这里——go2rtc 的会话由播放器断开自然结束。
        """
        with self._lock:
            if did and self._relay_did and did != self._relay_did:
                return
            self._close_relay()

    def _stop_runtime(self) -> None:
        if self._runtime is not None:
            timing.mark("停止 go2rtc 子进程")
            self._runtime.stop()
        self._runtime = None

    def shutdown(self) -> None:
        with self._lock:
            timing.mark("程序退出：收 go2rtc 与 relay")
            self._stop_runtime()
            self._close_relay()

    def stream_hint(self) -> str:
        """画面中断的"人话"解释；判断不出来就给空串，让控件用默认文案。"""
        if self._relay is not None:
            hint = self._relay.hint()
            if hint:
                return hint
        rt = self._runtime
        if rt is None or not rt.running:
            return ""
        if rt.recent_failure() == "eof":
            return "画面中断：观看位可能已满（最多 3 路）"
        return ""

    # -- 云台 ------------------------------------------------------------
    def ptz_supported(self) -> bool:
        """这个 go2rtc 构建带不带云台接口。

        只有**自建版**有（``go2rtc-ptz.patch``）：这台机型的 MIoT 规格里没有 ptz
        服务，云台只能走取流那条 MISS 连接的 ``0x112``，得由 go2rtc 发。官方构建
        没有这条路由（404），界面据此把方向键整块隐藏。

        结论按构建缓存一次（同一个二进制不会中途变），但**连不上不缓存**——
        go2rtc 可能刚重启，下次进页面再问。
        """
        if self._ptz_ok is not None:
            return self._ptz_ok
        rt = self._runtime
        if rt is None or not rt.running or not rt.api_port:
            return False
        url = f"http://127.0.0.1:{rt.api_port}/api/xiaomi/ptz"
        try:
            with urllib.request.urlopen(url, timeout=0.8) as resp:
                reply = json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                # 官方构建：根本没有这条路由，不必每次进页面再问一遍
                self._ptz_ok = False
            return False
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return False
        self._ptz_ok = bool(reply.get("ok"))
        return self._ptz_ok

    def ptz(self, did: str, op: int) -> bool:
        """发一条云台指令（1 左 / 2 右 / 3 上 / 4 下 / 5 校准），返回是否受理。

        一次一条、单步：连续转动由界面按 220ms 补发，这里不做队列。
        指令走的是**正在取流的那条连接**，所以没在播放时会被 go2rtc 拒
        （``stream has no ptz producer``）——那是预期，不是错误。
        """
        rt = self._runtime
        if rt is None or not rt.running or not rt.api_port or not did:
            return False
        if op not in PTZ_OPS:
            return False
        name = Go2rtcRuntime.stream_name(did)
        url = (
            f"http://127.0.0.1:{rt.api_port}/api/xiaomi/ptz"
            f"?name={urllib.parse.quote(name)}&op={op}"
        )
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                reply = json.loads(resp.read().decode("utf-8") or "{}")
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            timing.mark("云台指令失败", f"op={op} {exc}")
            return False
        if not reply.get("ok"):
            timing.mark("云台指令被拒", f"op={op} {reply.get('error', '')}")
            return False
        timing.mark("云台指令", f"op={op}")
        return True


_SERVICE: CameraService | None = None


def shared() -> CameraService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = CameraService()
    return _SERVICE


def _shutdown_all() -> None:
    if _SERVICE is not None:
        _SERVICE.shutdown()


atexit.register(_shutdown_all)

__all__ = [
    "PROBE_PORT",
    "PTZ_DOWN",
    "PTZ_LEFT",
    "PTZ_OPS",
    "PTZ_RIGHT",
    "PTZ_UP",
    "CameraError",
    "CameraService",
    "CameraSource",
    "Go2rtcRuntime",
    "find_binary",
    "is_camera",
    "missing_binary_hint",
    "probe_local",
    "program_dirs",
    "shared",
]
