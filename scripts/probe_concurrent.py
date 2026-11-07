# SPDX-License-Identifier: GPL-3.0-or-later
"""探测：同一台摄像头能不能被两个客户端同时取流。

疑问来自"我这里正播着，又用米家 App 打开摄像头会不会冲突"。米家 App 走的是
自己的 P2P 会话，和我们的 go2rtc 是**两次独立连接**，不是共享同一条流，所以冲突
只可能来自摄像头固件对并发观看数的限制。

这个脚本用两个**独立的 go2rtc 实例**、各自对设备发起一次 P2P 会话，来模拟"第二个
观看者"：如果第二路能同时出画、第一路不受影响，说明摄像头允许多路并发，App 那边
大概率也不冲突（App 只是换了个客户端）。

取流用裸 RTSP 读字节，不解码——HEVC 2960×1666 解两路会吃满 CPU，测出来的是我们
自己的瓶颈，不是摄像头的。只关心"数据还在不在流"。

用法：
  .venv/Scripts/python.exe -X utf8 scripts/probe_concurrent.py
  .venv/Scripts/python.exe -X utf8 scripts/probe_concurrent.py --did 1000000002
"""

from __future__ import annotations

import argparse
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mijiaAPI import mijiaAPI

from mihome.core.camera import CameraSource, Go2rtcRuntime, find_binary


class RtspPull(threading.Thread):
    """最小 RTSP 客户端：只收字节，不解析、不解码。"""

    def __init__(self, url: str, tag: str) -> None:
        super().__init__(daemon=True)
        self.url = url
        self.tag = tag
        self.total = 0
        self.started_at = 0.0
        self.first_byte_at: float | None = None
        self.error = ""
        self._stop = threading.Event()
        self._samples: list[tuple[float, int]] = []
        self._lock = threading.Lock()

    # -- 样本 ------------------------------------------------------------
    def rate(self) -> float:
        """最近一秒的速率（KB/s），没数据就是 0。"""
        with self._lock:
            if len(self._samples) < 2:
                return 0.0
            (t0, b0), (t1, b1) = self._samples[-2], self._samples[-1]
            if t1 <= t0:
                return 0.0
            return (b1 - b0) / (t1 - t0) / 1024.0

    def stop(self) -> None:
        self._stop.set()

    # -- RTSP ------------------------------------------------------------
    def _request(self, sock: socket.socket, text: str) -> tuple[str, bytes]:
        sock.sendall(text.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("RTSP 连接被关闭")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        header = head.decode("utf-8", "replace")
        length = 0
        for line in header.split("\r\n"):
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1].strip())
        while len(rest) < length:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("RTSP 连接被关闭")
            rest += chunk
        return header, rest[:length]

    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:  # noqa: BLE001 - 探测脚本，任何异常都要报出来
            self.error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__

    def _run(self) -> None:
        parts = urlsplit(self.url)
        sock = socket.create_connection((parts.hostname or "127.0.0.1", parts.port or 554), timeout=8)
        # DESCRIBE 会等 go2rtc 把生产者拉起来（P2P 打洞 3–6 秒），别用读循环的短超时
        sock.settimeout(12.0)
        self.started_at = time.time()

        _, sdp = self._request(
            sock,
            f"DESCRIBE {self.url} RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\n\r\n",
        )
        # 只关心"有几路数据在流"，所以按 SDP 里的 a=control 逐轨 SETUP 就够，
        # 不去解析 c= / m= 的行。轨道 URL 是相对会话 URL 的。
        controls: list[str] = []
        for line in sdp.decode("utf-8", "replace").split("\r\n"):
            if not line.startswith("a=control:"):
                continue
            ctrl = line.split(":", 1)[1].strip()
            if not ctrl or ctrl == "*":
                continue
            controls.append(ctrl if "://" in ctrl else f"{self.url}/{ctrl.lstrip('/')}")
        if not controls:
            controls = [self.url]

        session = ""
        cseq = 2
        for index, ctrl in enumerate(controls):
            header, _ = self._request(
                sock,
                f"SETUP {ctrl} RTSP/1.0\r\nCSeq: {cseq}\r\n"
                f"Transport: RTP/AVP/TCP;unicast;interleaved={index * 2}-{index * 2 + 1}\r\n\r\n",
            )
            cseq += 1
            for line in header.split("\r\n"):
                if line.lower().startswith("session:"):
                    session = line.split(":", 1)[1].split(";")[0].strip()
        self._request(
            sock,
            f"PLAY {self.url} RTSP/1.0\r\nCSeq: {cseq}\r\nSession: {session}\r\n\r\n",
        )
        sock.settimeout(0.5)

        buf = bytearray(65536)
        next_sample = time.time() + 1.0
        while not self._stop.is_set():
            try:
                got = sock.recv_into(buf)
            except TimeoutError:
                got = 0
            except OSError:
                break
            if got == 0 and self._stop.is_set():
                break
            now = time.time()
            if got:
                with self._lock:
                    self.total += got
                    if self.first_byte_at is None:
                        self.first_byte_at = now
            if now >= next_sample:
                next_sample = now + 1.0
                with self._lock:
                    self._samples.append((now, self.total))
                    if len(self._samples) > 120:
                        self._samples.pop(0)
        try:
            sock.close()
        except OSError:
            pass


def pick_camera(devices: list[dict], did: str) -> dict:
    cams = [d for d in devices if "camera" in str(d.get("model") or "").lower()]
    if not cams:
        raise SystemExit("账号里没有摄像头设备")
    if did:
        hit = next((c for c in cams if str(c.get("did")) == did), None)
        if hit is None:
            raise SystemExit(f"没找到 did={did} 的摄像头")
        return hit
    return next((c for c in cams if c.get("isOnline")), cams[0])


def producer_bytes(api_port: int, stream: str) -> int:
    """go2rtc 自己从设备那边收到了多少字节（生产者视角）。"""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{api_port}/api/streams", timeout=2
        ) as resp:
            import json

            data = json.loads(resp.read() or b"{}")
        info = data.get(stream) or {}
        total = 0
        for prod in info.get("producers") or []:
            total += int(prod.get("bytes_recv") or 0)
        return total
    except Exception:  # noqa: BLE001
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="并发取流探测")
    parser.add_argument("--did", default="")
    parser.add_argument("--both", type=float, default=10.0, help="两路同时观察时长")
    parser.add_argument("--extra", type=int, default=1, help="在 A 之外再加几路")
    args = parser.parse_args()

    api = mijiaAPI()
    dev = pick_camera(api.get_devices_list(), args.did)
    node = SimpleNamespace(
        did=str(dev.get("did") or ""),
        model=str(dev.get("model") or ""),
        localip=str(dev.get("localip") or ""),
        name=str(dev.get("name") or ""),
        online=bool(dev.get("isOnline")),
    )
    print(f"目标：{node.name} / {node.model} / did={node.did} / ip={node.localip}")
    if not node.online or not node.localip:
        raise SystemExit("设备离线或没有局域网地址，跨会话探测没有意义")

    binary = find_binary()
    if binary is None:
        raise SystemExit("没找到 go2rtc")
    auth = api.auth_data if hasattr(api, "auth_data") else {}
    uid = str(auth.get("userId", ""))
    token = str(auth.get("passToken", ""))
    src = CameraSource(did=node.did, model=node.model, localip=node.localip, name=node.name)
    stream = f"cam_{node.did}"
    workdirs: list[Path] = []

    def open_session(name: str) -> tuple[Go2rtcRuntime, RtspPull] | None:
        """起一个独立实例 + 一条裸 RTSP 消费，等它真的收到数据。"""
        rt = Go2rtcRuntime(binary, uid, token)
        # 每个实例单开一个临时目录：配置文件里有 passToken，不能落在数据目录里
        # 留成第二份凭据；进程退出就删。
        rt._workdir = Path(tempfile.mkdtemp(prefix=f"mihome-probe-{name.lower()}-"))
        rt._log_path = rt._workdir / "go2rtc.log"
        workdirs.append(rt._workdir)
        started = time.time()
        try:
            rt.start([src])
        except Exception as exc:  # noqa: BLE001
            print(f"    {name} 实例起不来：{exc}")
            return None
        pull = RtspPull(rt.url(node.did), name)
        pull.start()
        deadline = time.time() + 30
        while time.time() < deadline and pull.total < 200_000:
            time.sleep(0.2)
        if pull.total < 200_000:
            print(f"    {name} 没出流：{pull.error or 'no data'}（{pull.total}B）")
            pull.stop()
            rt.stop()
            return None
        first = (pull.first_byte_at or time.time()) - pull.started_at
        print(
            f"    {name} api={rt.api_port} rtsp={rt.rtsp_port} "
            f"实例 {pull.started_at - started:.1f}s / 首包 {first:.1f}s"
        )
        return rt, pull

    print("\n[1] 起第一个实例（会话 A）")
    first = open_session("A")
    if first is None:
        return 1
    a, pull_a = first

    print("\n[2] 基线：只跑 A，5 秒")
    for _ in range(5):
        time.sleep(1)
        print(f"    A {pull_a.rate():7.0f} KB/s   生产者 {producer_bytes(a.api_port, stream) // 1024} KB")

    extras: list[tuple[Go2rtcRuntime, RtspPull]] = []
    for index, name in enumerate("BCDEF"[: args.extra], start=2):
        print(f"\n[{index}] 再加一路（会话 {name}，同一台设备）")
        opened = open_session(name)
        if opened is None:
            print(f"    并发上限出现在第 {len(extras) + 2} 路")
            break
        extras.append(opened)

    print(f"\n[{len(extras) + 3}] 全部 {len(extras) + 1} 路同时跑 {args.both:.0f} 秒")
    for _ in range(int(args.both)):
        time.sleep(1)
        row = [f"A {pull_a.rate():6.0f}"]
        row += [f"{p.tag} {p.rate():6.0f}" for _, p in extras]
        total = pull_a.total + sum(p.total for _, p in extras)
        print("    " + " KB/s  ".join(row) + f" KB/s   合计 {total // 1024} KB")

    print("\n[收尾] 依次关掉附加会话，看 A 是否恢复")
    for rt, pull in reversed(extras):
        pull.stop()
        rt.stop()
    for _ in range(4):
        time.sleep(1)
        print(f"    A {pull_a.rate():7.0f} KB/s   生产者 {producer_bytes(a.api_port, stream) // 1024} KB")

    pull_a.stop()
    a.stop()
    for path in workdirs:
        shutil.rmtree(path, ignore_errors=True)
    print("\n结论：")
    print(f"  能同时取流的会话数：{len(extras) + 1}")
    print(f"  A 全程未断：{'是' if pull_a.total > 200_000 else '否'}")
    return 0 if extras else 2


if __name__ == "__main__":
    raise SystemExit(main())
