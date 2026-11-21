"""把「首帧 5–7 秒」拆开计时，找出时间到底花在哪一段。

用法：
    python -X utf8 scripts/measure_camera_startup.py --mode raw
    python -X utf8 scripts/measure_camera_startup.py --mode qt
    （可加 --subtype 1 量子码流、--audio 0 不要音频轨）

``raw`` 只解析 RTP 不解码，量的是「摄像头什么时候把可解码的帧吐到本机」；
``qt`` 走真正的 QMediaPlayer，量的是「用户什么时候看到画面」。

2026-11-08 实测（局域网，Xiaomi Smart Camera 3 / 069a01，2960×1666 HEVC）：

    0.85s  go2rtc 进程启动
    0.65s  云端 miss_get_vendor（go2rtc 每开一路都要调一次）
    0.55s  MISS 握手到首个 RTP 包
    3.05s  等下一个 IDR —— 会话起点那个 IDR 到不了消费者手里，只能等满一个 GOP
    1.0s   177 KB 的 IDR 传完 + 解码出首帧
    ─────
    ≈6s    其中 3 秒是摄像头的 GOP，客户端无法消掉
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mijiaAPI import mijiaAPI

from mihome.core.camera import CameraSource, Go2rtcRuntime, find_binary

# HEVC NAL 类型
IDR_TYPES = (19, 20, 21)
FU_TYPE = 49


class LogWatcher(threading.Thread):
    """盯 go2rtc 日志的新增行，记录它们出现的相对时刻（比读文件里的时间戳稳）。"""

    def __init__(self, path: Path, t0: float) -> None:
        super().__init__(daemon=True)
        self.path = path
        self.t0 = t0
        self.lines: list[tuple[float, str]] = []
        self._stop = threading.Event()
        self._seen = 0

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                text = self.path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                time.sleep(0.05)
                continue
            parts = text.splitlines()
            if len(parts) > self._seen:
                for line in parts[self._seen:]:
                    if line.strip():
                        self.lines.append((time.time() - self.t0, line.strip()))
                self._seen = len(parts)
            time.sleep(0.02)

    def stop(self) -> None:
        self._stop.set()


class RawPull:
    """裸 RTSP 拉流：只解析 RTP 头，看关键帧什么时候到。"""

    def __init__(self, url: str, t0: float) -> None:
        self.url = url
        self.t0 = t0
        self.marks: list[tuple[str, float]] = []
        self.frames: list[tuple[float, int]] = []
        self.sizes: list[tuple[float, int, int]] = []   # (时刻, NAL 类型, 字节)
        self._cur_ts = -1
        self._cur_bytes = 0
        self._cur_nal = 0
        self._cur_idr = False
        self._cur_at = 0.0
        self.error = ""

    def _mark(self, name: str) -> None:
        self.marks.append((name, time.time() - self.t0))

    def run(self, seconds: float = 20.0) -> None:
        parts = urlsplit(self.url)
        sock = socket.create_connection((parts.hostname, int(parts.port or 554)), timeout=10)
        sock.settimeout(2.0)
        self._mark("TCP 连上 go2rtc")
        f = sock.makefile("rb")
        cseq = 1

        def request(method: str, extra: str = "", url: str = "") -> tuple[str, bytes]:
            nonlocal cseq
            head = f"{method} {url or self.url} RTSP/1.0\r\nCSeq: {cseq}\r\n"
            if extra:
                head += extra
            sock.sendall((head + "\r\n").encode())
            cseq += 1
            lines = []
            while True:
                line = f.readline()
                if not line:
                    raise ConnectionError("RTSP 连接被关闭")
                line = line.rstrip(b"\r\n")
                if not line:
                    break
                lines.append(line.decode("utf-8", "replace"))
            body = b""
            for ln in lines:
                if ln.lower().startswith("content-length:"):
                    body = f.read(int(ln.split(":", 1)[1].strip()))
            return lines[0], body

        try:
            status, _ = request("OPTIONS")
            self._mark(f"OPTIONS → {status.split()[1] if ' ' in status else status}")
            status, sdp = request("DESCRIBE", "Accept: application/sdp\r\n")
            self._mark(f"DESCRIBE → {status.split()[1]}")
            print("  ---- SDP ----")
            for ln in sdp.decode("utf-8", "replace").splitlines():
                if ln.startswith(("m=", "a=control", "a=rtpmap", "a=fmtp")):
                    print("   ", ln[:120])
            # SDP 里音频可能排在视频前面，必须挑 H265 那一节的控制地址，
            # 否则量到的是音频包（首字节当成 NAL 类型，会得出荒谬的结论）
            ctrl = ""
            section_is_video = False
            for ln in sdp.decode("utf-8", "replace").splitlines():
                if ln.startswith("m="):
                    section_is_video = False
                elif ln.startswith("a=rtpmap:") and ("H265" in ln or "H264" in ln):
                    section_is_video = True
                elif ln.startswith("a=control:") and section_is_video:
                    val = ln.split(":", 1)[1].strip()
                    if "://" not in val:
                        ctrl = val
                        break
            setup_url = self.url.rstrip("/") + "/" + ctrl if ctrl else self.url
            status, _ = request(
                "SETUP",
                "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n"
                "Session: 0\r\n",
                url=setup_url,
            )
            self._mark(f"SETUP → {status.split()[1]}")
            session = ""
            print("  SETUP 应答：", status)
            status, _ = request("PLAY", f"Session: {session}\r\n")
            self._mark(f"PLAY → {status.split()[1]}")
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
            return

        buf = b""
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                chunk = sock.recv(65536)
            except TimeoutError:
                continue
            if not chunk:
                break
            buf += chunk
            while len(buf) >= 4:
                if buf[0:1] != b"$":
                    # 不是交错帧，跳过（多半是 RTSP 应答残片）
                    idx = buf.find(b"$")
                    if idx < 0:
                        buf = b""
                        break
                    buf = buf[idx:]
                    continue
                size = int.from_bytes(buf[2:4], "big")
                if len(buf) < 4 + size:
                    break
                payload = buf[4:4 + size]
                buf = buf[4 + size:]
                self._on_rtp(payload)
        sock.close()

    def _on_rtp(self, rtp: bytes) -> None:
        if len(rtp) < 13:
            return
        if not self.frames and not any(m == "首个 RTP 包" for m, _ in self.marks):
            self._mark("首个 RTP 包")
        cc = rtp[0] & 0x0F
        off = 12 + 4 * cc
        if rtp[0] & 0x10 and len(rtp) >= off + 4:
            ext_len = int.from_bytes(rtp[off + 2:off + 4], "big")
            off += 4 + 4 * ext_len
        if len(rtp) <= off:
            return
        ts = int.from_bytes(rtp[4:8], "big")
        marker = bool(rtp[1] & 0x80)
        if ts != self._cur_ts:
            if self._cur_ts >= 0 and self._cur_bytes:
                self.sizes.append(
                    (self._cur_at, -1 if self._cur_idr else self._cur_nal, self._cur_bytes)
                )
            self._cur_ts = ts
            self._cur_bytes = 0
            self._cur_nal = 0
            self._cur_idr = False
            self._cur_at = time.time() - self.t0
        self._cur_bytes += len(rtp) - off
        if marker:
            pass
        nal = (rtp[off] >> 1) & 0x3F
        if nal == FU_TYPE:
            if len(rtp) < off + 3 or not (rtp[off + 2] & 0x80):
                return  # 不是分片首包，不算一帧的开头
            nal = rtp[off + 2] & 0x3F
        now = time.time() - self.t0
        if nal in IDR_TYPES:
            self._cur_idr = True
        if not self._cur_nal:
            self._cur_nal = nal
        self.frames.append((now, nal))
        if nal in IDR_TYPES and not any(m == "首个关键帧(IDR)" for m, _ in self.marks):
            self._mark("首个关键帧(IDR)")

    def report(self) -> None:
        print("\n  RTSP 阶段：")
        for name, at in self.marks:
            print(f"    {at:6.2f}s  {name}")
        if self.error:
            print(f"    错误：{self.error}")
        if not self.frames:
            print("    没有收到任何视频帧")
            return
        first_idr = next((t for t, n in self.frames if n in IDR_TYPES), None)
        lead = [n for t, n in self.frames if first_idr is None or t < first_idr]
        non_idr = sum(1 for n in lead if n not in IDR_TYPES)
        print(
            f"\n  首个 RTP 包到首个 IDR 之间：{non_idr} 帧非关键帧"
            f"（率先到达的 NAL 类型 {sorted(set(lead))[:6]}）"
        )
        big = [s for s in self.sizes if s[1] == -1]
        if big:
            at, _, size = big[0]
            print(f"  首个 IDR 帧：{size // 1024} KB，从 {at:.2f}s 开始到")
            later = [s for s in self.sizes if s[1] != -1][:60]
            if later:
                avg = sum(x[2] for x in later) / len(later)
                print(f"  普通帧平均 {avg / 1024:.1f} KB")
        idrs = [t for t, n in self.frames if n in IDR_TYPES]
        if len(idrs) > 1:
            gaps = [round(b - a, 2) for a, b in itertools.pairwise(idrs)]
            print(f"  IDR 时刻：{[round(t, 2) for t in idrs[:8]]}  间隔 {gaps[:7]}")
        print(f"  帧率约 {len(self.frames) / (self.frames[-1][0] - self.frames[0][0]):.1f} fps，"
              f"共 {len(self.frames)} 帧 / {self.frames[-1][0] - self.frames[0][0]:.1f}s")
        if first_idr is not None:
            print(f"  视频从 {self.frames[0][0]:.2f}s 开始到，但第一个 IDR 在 {first_idr:.2f}s"
                  f" —— 中间 {first_idr - self.frames[0][0]:.2f}s 没有可解码的帧")


class ProducerPoll(threading.Thread):
    """每 100ms 读一次 /api/streams，记录生产者什么时候建、什么时候开始出字节。"""

    def __init__(self, api_port: int, name: str, t0: float) -> None:
        super().__init__(daemon=True)
        self.api_port = api_port
        self.name_ = name
        self.t0 = t0
        self.events: list[str] = []
        self._stop = threading.Event()
        self._last = -1

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.api_port}/api/streams", timeout=1
                ) as resp:
                    info = json.loads(resp.read() or b"{}").get(self.name_, {})
                prods = info.get("producers") or []
                if not prods and self._last != 0:
                    self.events.append(f"{time.time() - self.t0:6.2f}s  生产者：无")
                    self._last = 0
                elif prods:
                    kb = int(prods[0].get("bytes_recv") or 0) // 1024
                    if kb != self._last:
                        if self._last <= 0 or kb - self._last >= 64:
                            self.events.append(
                                f"{time.time() - self.t0:6.2f}s  生产者 recv={kb} KB"
                                f" state={prods[0].get('state')}"
                            )
                        self._last = kb
            except Exception:  # noqa: BLE001, S110 - 轮询失败就下一轮，不影响计时
                pass
            time.sleep(0.1)

    def stop(self) -> None:
        self._stop.set()


def run_qt(url: str, t0: float, limit: float, api_port: int = 0, name: str = "") -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QUrl
    from PySide6.QtMultimedia import QMediaPlayer, QVideoSink
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv[:1])
    player = QMediaPlayer()
    sink = QVideoSink()
    player.setVideoSink(sink)
    marks: list[str] = []
    started = time.time()

    def on_status(status) -> None:
        marks.append(f"{time.time() - t0:6.2f}s  mediaStatus={status.name}")

    player.mediaStatusChanged.connect(on_status)

    first: list[float] = []

    def on_frame(frame) -> None:
        if frame.isValid() and not first:
            first.append(time.time() - t0)
            print(f"{first[0]:6.2f}s  第一个解码出来的帧 {frame.width()}x{frame.height()}")

    sink.videoFrameChanged.connect(on_frame)
    print(f"{time.time() - t0:6.2f}s  setSource")
    player.setSource(QUrl(url))
    player.play()
    while time.time() - started < limit and not first:
        app.processEvents()
        time.sleep(0.005)
    for line in marks:
        print("  " + line)
    player.stop()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["raw", "qt"], default="raw")
    parser.add_argument("--subtype", type=int, default=0)
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--audio", type=int, default=1, help="0 = 流里不要音频轨")
    args = parser.parse_args()

    os.environ["MIHOME_GO2RTC_LOG"] = "debug"
    api = mijiaAPI()
    dev = next(
        d
        for d in api.get_devices_list()
        if "camera" in str(d.get("model") or "").lower() and d.get("isOnline")
    )
    src = CameraSource(
        did=str(dev["did"]),
        model=str(dev["model"]),
        localip=str(dev.get("localip") or ""),
        name=str(dev.get("name") or ""),
    )
    auth = api.auth_data
    binary = find_binary()
    if binary is None:
        print("找不到 go2rtc")
        return 1

    workdir = Path(tempfile.mkdtemp(prefix="mihome-measure-"))
    rt = Go2rtcRuntime(binary, str(auth["userId"]), str(auth["passToken"]))
    rt._workdir = workdir
    rt._log_path = workdir / "go2rtc.log"
    rt._stream_url = lambda s: (
        f"xiaomi://{rt.user_id}:{rt.region}@{s.localip}"
        f"?did={s.did}&model={s.model}&channel=0&subtype={args.subtype}"
        + ("" if args.audio else "&audio=0")
    )

    t0 = time.time()
    watcher = LogWatcher(rt._log_path, t0)
    watcher.start()
    rt.start([src])
    print(f"{time.time() - t0:6.2f}s  go2rtc 就绪  api={rt.api_port} rtsp={rt.rtsp_port}")
    url = rt.url(src.did)
    print(f"          流地址 {url}  subtype={args.subtype}")

    if args.mode == "raw":
        pull = RawPull(url, t0)
        pull.run(args.seconds)
        pull.report()
    else:
        poll = ProducerPoll(rt.api_port, rt.stream_name(src.did), t0)
        poll.start()
        run_qt(url, t0, args.seconds)
        poll.stop()
        print("\n  生产者侧时间线：")
        for line in poll.events[:14]:
            print("  " + line)

    # 生产者侧：摄像头 → go2rtc 有没有在动
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{rt.api_port}/api/streams", timeout=3
        ) as resp:
            info = json.loads(resp.read() or b"{}").get(rt.stream_name(src.did), {})
        for prod in info.get("producers") or []:
            print(f"\n  生产者 state={prod.get('state')} recv={int(prod.get('bytes_recv') or 0)//1024} KB"
                  f" remote={prod.get('remote_addr')}")
    except Exception as exc:  # noqa: BLE001
        print(f"  读 /api/streams 失败：{exc}")

    print("\n  go2rtc 关键日志（相对时刻）：")
    keys = ("miss", "xiaomi", "producer", "rtsp:", "error", "streams")
    for at, line in watcher.lines:
        low = line.lower()
        if any(k in low for k in keys):
            print(f"    {at:6.2f}s  {line[:150]}")

    watcher.stop()
    rt.stop()
    shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
