# SPDX-License-Identifier: GPL-3.0-or-later
"""量「点播放 → 出画面」：走真实详情页，局域网 / 中继两条通道分开跑。

和 ``measure_camera_coldstart.py`` 的区别：那个直接拿 RTSP 地址喂播放器，只覆盖
go2rtc + Qt 那一段；这里从 ``DetailDialog`` 点播放开始计时，把详情页、取流线程、
中继握手、go2rtc 全算进去——也就是用户实际感觉到的那一下。

通道偏好**只改内存**（``store.prefs.camera_network``），不落盘：
``--lane lan`` 模拟「局域网优先」，``--lane remote`` 模拟「仅远程」。

    python -X utf8 scripts/measure_app_play.py --lane lan --rounds 3
    python -X utf8 scripts/measure_app_play.py --lane remote --rounds 2
"""

from __future__ import annotations

import argparse
import os
import sys

import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

# 这些脚本自己驱动窗口（建 → 截 → close → 退）：让托盘接管 close 的话，
# 关闭只会把窗口藏起来，脚本等的是一个不会结束的事件循环。
os.environ.setdefault("MIHOME_NO_TRAY", "1")
# 取流计时默认关闭（发布版零开销），诊断脚本自己要明细，这里兜个默认值；
# 外部显式设了 0 仍然尊重
os.environ.setdefault("MIHOME_CAMERA_TIMING", "1")

from mihome.ui import theme
from mihome.ui.detail import DetailDialog
from mihome.ui.main_window import MainWindow

#: 通道 → 落盘用的偏好值（只在内存里改，见模块说明）；``keep`` 不动，用磁盘上的值
LANE_PREF = {"lan": "lan_first", "remote": "remote_only", "keep": ""}


def _wait(app: QApplication, store, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if not store.loading and store.devices:
            return True
        time.sleep(0.02)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lane", choices=sorted(LANE_PREF), default="lan")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--settle", type=float, default=3.0, help="每轮结束等生产者自然停")
    parser.add_argument("--timeout", type=float, default=25.0, help="单轮等首帧上限")
    parser.add_argument("--open-delay", type=float, default=0.5, help="打开页面到点播放的间隔")
    args = parser.parse_args()

    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(13)
    app.setFont(font)

    win = MainWindow(theme.DARK)
    win.palette_ref = theme.DARK
    win._apply_theme()
    win.resize(1360, 900)
    win.show()
    win.start()

    if not _wait(app, win.store):
        print("设备列表没加载出来")
        return 1
    cam = next(
        (
            d
            for d in win.store.devices.values()
            if d.online and "camera" in d.node.model.lower()
        ),
        None,
    )
    if cam is None:
        print("没有在线摄像头")
        return 1

    if LANE_PREF[args.lane]:
        win.store.prefs.camera_network = LANE_PREF[args.lane]  # 只改内存，不写盘
    print(f"摄像头 {cam.name}  localip={getattr(cam.node, 'localip', '')}")
    print(f"通道偏好={win.store.camera_network}（{'磁盘值' if not LANE_PREF[args.lane] else '本次强制'}）  轮数={args.rounds}")

    from mihome.core import timing

    times: list[float] = []
    for i in range(args.rounds):
        dlg = DetailDialog(cam, win.store, win.palette_ref, win, icons=win.icon_provider)
        dlg.show()
        view = dlg._camera
        if view is None:
            print("画面控件没建出来")
            dlg.close()
            break
        end = time.time() + args.open_delay
        while time.time() < end:
            app.processEvents()
            time.sleep(0.005)

        started = time.time()
        view.toggle()  # 等价于用户点播放键
        while time.time() - started < args.timeout:
            app.processEvents()
            if view.state == view.PLAYING:
                break
            time.sleep(0.005)
        took = time.time() - started
        if view.state == view.PLAYING:
            times.append(took)
            detail = f"首帧 {took:5.2f}s"
        else:
            detail = f"没出画面，状态 {view.state}，提示 {view._hint!r}"
        print(f"  #{i + 1} {detail}")

        dlg.close()
        dlg.deleteLater()
        app.processEvents()
        end = time.time() + args.settle
        while time.time() < end:
            app.processEvents()
            time.sleep(0.02)

    if times:
        print(
            f"\n{args.lane}: 均值 {sum(times) / len(times):.2f}s  "
            f"区间 {min(times):.2f}–{max(times):.2f}s（{len(times)} 次）"
        )
    print(f"计时明细：{timing.LOG_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
