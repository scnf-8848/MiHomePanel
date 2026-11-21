"""量「点播放 → 出画面」的冷启动耗时，用来对比不同的 go2rtc 构建。

只测最要紧的那一段：go2rtc 已经起来、生产者还没建（用户点第一下播放时的真实情况）。
每次接流之后等 2.5 秒让生产者自然停，保证下一次仍是冷启动。

    python -X utf8 scripts/measure_camera_coldstart.py --binary <data>/go2rtc/go2rtc.exe
    python -X utf8 scripts/measure_camera_coldstart.py --binary <data>/go2rtc/go2rtc-official.exe

2026-11-08 实测（Xiaomi Smart Camera 3 / 069a01，局域网，Qt 真播）：官方构建
4.82/4.84/4.82/6.32 s（均值 5.33），带 ``scripts/patches/go2rtc-keyframe.patch``
的自建版 1.85–3.15 s（均值 2.18）。差别就是「等一个 GOP」那一项。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mijiaAPI import mijiaAPI

from mihome.core.camera import CameraSource, Go2rtcRuntime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class Probe:
    def __init__(self) -> None:
        from PySide6.QtWidgets import QApplication

        self.app = QApplication(sys.argv[:1])

    def run(self, url: str, timeout: float = 20.0, settle: float = 3.0):
        from PySide6.QtCore import QUrl
        from PySide6.QtMultimedia import QMediaPlayer, QVideoSink

        player = QMediaPlayer()
        sink = QVideoSink()
        player.setVideoSink(sink)
        started = time.time()
        first: list[float] = []
        total: list[int] = []
        size: list[str] = []
        marks: list[str] = []

        def on_status(status) -> None:
            marks.append(f"{time.time() - started:4.2f}s {status.name}")

        def on_frame(frame) -> None:
            if not frame.isValid():
                return
            total.append(1)
            if not first:
                first.append(time.time() - started)
                size.append(f"{frame.width()}x{frame.height()}")

        player.mediaStatusChanged.connect(on_status)
        sink.videoFrameChanged.connect(on_frame)
        player.setSource(QUrl(url))
        player.play()
        deadline = time.time() + timeout
        while time.time() < deadline and not first:
            self.app.processEvents()
            time.sleep(0.005)
        # 出画后再看 3 秒，确认补丁没有把流搞乱（帧数应该 ≈ 20fps × 3s）
        end = time.time() + settle
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.005)
        player.stop()
        player.setSource(QUrl())
        for _ in range(80):
            self.app.processEvents()
            time.sleep(0.002)
        player.deleteLater()
        sink.deleteLater()
        self.app.processEvents()
        return {
            "first": first[0] if first else None,
            "size": size[0] if size else "",
            "fps": len(total) / settle if total else 0,
            "marks": marks,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()

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
    binary = Path(args.binary)
    print(f"二进制 {binary.name}")

    workdir = Path(tempfile.mkdtemp(prefix="mihome-patch-"))
    rt = Go2rtcRuntime(binary, str(auth["userId"]), str(auth["passToken"]))
    rt._workdir = workdir
    rt._log_path = workdir / "go2rtc.log"
    rt.start([src])
    url = rt.url(src.did)
    probe = Probe()

    times: list[float] = []
    for i in range(args.rounds):
        got = probe.run(url)
        t = got["first"]
        if t is None:
            print(f"  #{i + 1} 没出画面")
        else:
            times.append(t)
            print(
                f"  #{i + 1} 首帧 {t:5.2f}s  {got['size']}  {got['fps']:.1f} fps"
                f"   {' → '.join(got['marks'][:4])}"
            )
        time.sleep(2.5)

    if times:
        print(
            f"\n  {binary.name}: 均值 {sum(times) / len(times):.2f}s  "
            f"区间 {min(times):.2f}–{max(times):.2f}s（{len(times)} 次）"
        )
    rt.stop()
    shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
