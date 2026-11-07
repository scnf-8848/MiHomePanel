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
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import paths

API_PORT = 1984
RTSP_PORT = 8554
START_TIMEOUT = 12.0
# 上次异常退出留下的进程记录。正常退出会删掉，留着就说明是崩溃/强杀
PIDFILE = "runtime.json"
# go2rtc 日志里代表"这条流被对端掐了"的片段
_FAILURE_MARKS = ("cs2: EOF", "read media")


class CameraError(RuntimeError):
    """取流失败。``kind`` 给 UI 决定是提示重试还是提示安装。"""

    def __init__(self, message: str, kind: str = "unknown") -> None:
        super().__init__(message)
        self.kind = kind


def is_camera(model: str, category: str = "") -> bool:
    """摄像头品类判定。品类来自 catalog，model 是兜底。"""
    return category == "camera" or "camera" in (model or "").lower()


def find_binary(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """按"显式指定 → 环境变量 → 数据目录 → 项目 tools → PATH"找 go2rtc。"""
    exe = "go2rtc.exe" if sys.platform == "win32" else "go2rtc"
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env = os.environ.get("MIHOME_GO2RTC", "").strip()
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(paths.data_dir() / "go2rtc" / exe)
    candidates.append(Path(__file__).resolve().parents[2] / "tools" / "go2rtc" / exe)
    for c in candidates:
        if c.is_file():
            return c
    found = shutil.which("go2rtc")
    return Path(found) if found else None


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
        subprocess.run(cmd, check=False, capture_output=True, timeout=8, creationflags=flags)
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
    """一台摄像头在 go2rtc 配置里的最小信息。"""

    did: str
    model: str
    localip: str
    name: str = ""
    online: bool = True


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
        if not src.localip:
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
            if src.online and src.localip
        }

    # -- 配置 ------------------------------------------------------------
    def _stream_url(self, src: CameraSource) -> str:
        return (
            f"xiaomi://{self.user_id}:{self.region}@{src.localip}"
            f"?did={src.did}&model={src.model}&channel=0&subtype=0"
        )

    def _write_config(self) -> Path:
        self._workdir.mkdir(parents=True, exist_ok=True)
        # 排障时可以 MIHOME_GO2RTC_LOG=debug 打开完整日志，默认只留警告
        level = os.environ.get("MIHOME_GO2RTC_LOG", "").strip() or "warn"
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
            if not src.online or not src.localip:
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

        flags = 0
        if sys.platform == "win32":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            with open(self._log_path, "wb") as log:
                self._proc = subprocess.Popen(
                    [str(self.binary), "-config", str(cfg)],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    cwd=str(self._workdir),
                    creationflags=flags,
                )
        except OSError as exc:
            self._proc = None
            raise CameraError(f"go2rtc 启动失败：{exc}", "start") from exc

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
                            return
            except (urllib.error.URLError, OSError, ValueError):
                time.sleep(0.2)
            else:
                time.sleep(0.2)
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

    @property
    def available(self) -> bool:
        return find_binary() is not None

    @property
    def binary(self) -> Path | None:
        return find_binary()

    def open(self, client: Any, node: Any) -> str:
        """确保 go2rtc 已带这台摄像头启动，返回本机 rtsp 地址。"""
        model = str(getattr(node, "model", "") or "")
        if not is_camera(model):
            raise CameraError("这不是摄像头", "not_camera")
        if not bool(getattr(node, "online", True)):
            raise CameraError("设备离线，无法取流", "offline")
        localip = str(getattr(node, "localip", "") or "")
        if not localip:
            raise CameraError("没有拿到这台摄像头的局域网地址，无法取流", "no_ip")

        binary = find_binary()
        if binary is None:
            raise CameraError(
                "没有找到 go2rtc。运行 scripts/install_go2rtc.py 自动下载，"
                "或设置环境变量 MIHOME_GO2RTC 指向 go2rtc.exe",
                "no_binary",
            )

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

        src = CameraSource(
            did=str(getattr(node, "did", "")),
            model=model,
            localip=localip,
            name=str(getattr(node, "name", "") or ""),
            online=True,
        )
        if self._runtime is None or not self._runtime.running or self._binary != binary:
            self.shutdown()  # 旧实例必须先收掉，否则会漏一个进程和两个端口
            self._runtime = Go2rtcRuntime(binary, user_id, pass_token)
            self._binary = binary
            self._runtime.start([src])
        elif src.did not in self._runtime.dids:
            self._runtime.restart([*self._runtime.sources, src])
        elif self._runtime.user_id != user_id or self._runtime.pass_token != pass_token:
            # 重新登录过：配置里的 passToken 要跟着换，否则下一次启动会用旧凭据
            self.shutdown()
            self._runtime = Go2rtcRuntime(binary, user_id, pass_token)
            self._binary = binary
            self._runtime.start([src])
        return self._runtime.url(src.did)

    def shutdown(self) -> None:
        if self._runtime is not None:
            self._runtime.stop()
        self._runtime = None

    def stream_hint(self) -> str:
        """画面中断的"人话"解释；判断不出来就给空串，让控件用默认文案。"""
        rt = self._runtime
        if rt is None or not rt.running:
            return ""
        if rt.recent_failure() == "eof":
            return "画面中断：观看位可能已满（最多 3 路）"
        return ""


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
    "CameraError",
    "CameraService",
    "CameraSource",
    "Go2rtcRuntime",
    "find_binary",
    "is_camera",
    "shared",
]
