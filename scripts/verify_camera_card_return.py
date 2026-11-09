# SPDX-License-Identifier: GPL-3.0-or-later
"""真机端到端：卡片播放 → 双击进详情 → 关掉详情 → 卡片画面回来。

这是 2026-11-10 用户实测报的 bug（卡片停在"画面已在其他窗口中播放"不再恢复）。
走的是**主窗口真正那条路径**（``MainWindow._on_card_clicked``，含
``about_to_stop`` 接线），不是手工拼的近似流程：

1. 墙面点播放，等首帧；
2. 调 ``_on_card_clicked``（等同于双击进详情）；
3. 详情页里等一会儿，然后关掉它；
4. 断言：卡片回到 PLAYING、帧号继续增长、会话所有者是卡片、不再显示 MOVED 提示。

需要登录态与一台在线的摄像头；会真取流（首帧约 4–6 秒）。

用法：
  .venv/Scripts/python.exe -X utf8 scripts/verify_camera_card_return.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MIHOME_NO_TRAY", "1")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mihome.ui import theme  # noqa: E402
from mihome.ui.camera_card import CameraCard  # noqa: E402
from mihome.ui.camera_view import CameraView, playbacks  # noqa: E402
from mihome.ui.detail import DetailDialog  # noqa: E402
from mihome.ui.main_window import MainWindow  # noqa: E402

FAILS: list[str] = []
FIRST_FRAME_WAIT = 30.0
DETAIL_STAY = 2.0


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {got!r}")
    if not ok:
        FAILS.append(f"{name} → 期望 {want!r}，实际 {got!r}")


def check_true(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(f"{name} {detail}")


def pump(app: QApplication, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def wait_until(app: QApplication, cond, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def main() -> int:
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

    ok = wait_until(app, lambda: not win.store.loading and bool(win.store.devices), 25)
    if not ok:
        print("设备列表没加载出来（没登录？）")
        return 1
    pump(app, 1.2)  # 让墙面把卡片建完（store 一到就建，但布局要再过一轮事件）
    cam = next(
        (d for d in win.store.devices.values()
         if d.online and "camera" in d.node.model.lower()),
        None,
    )
    if cam is None:
        print("没有在线的摄像头")
        return 1
    # 墙上可能有隐藏房间的卡、也可能在视口下方：不是"看不见"的卡点播放会被
    # ``_check_active_visible`` 立刻收掉。这里挑一张真在视口里的，必要时滚过去。
    def on_screen_card():
        return next(
            (c for c in win.wall._cards.values()  # noqa: SLF001
             if isinstance(c, CameraCard) and c.view is not None
             and win.wall._card_on_screen(c)),  # noqa: SLF001
            None,
        )

    card = on_screen_card()
    if card is None:
        any_cam = next(
            (c for c in win.wall._cards.values()  # noqa: SLF001
             if isinstance(c, CameraCard) and c.view is not None),
            None,
        )
        if any_cam is None:
            print("墙上没有摄像头卡")
            return 1
        win.wall.ensureWidgetVisible(any_cam)
        # 滚动是异步的：等它真的进视口再算（不然一点播放就被"不可见"收掉）
        wait_until(app, lambda: on_screen_card() is not None, 5)
        card = on_screen_card() or any_cam
    # 布局刚成立时点播放会被"卡片不可见"收掉，先让事件循环跑一轮
    pump(app, 1.0)
    did = card.state.did
    view = card.view
    assert view is not None
    cam = win.store.devices.get(did, cam)
    print(f"-- 设备 {cam.name}（{did}）--")

    # 1) 卡片上点播放
    view._play_btn.clicked.emit()
    if not wait_until(app, lambda: view.state == CameraView.PLAYING, FIRST_FRAME_WAIT):
        check("卡片出画", view.state, CameraView.PLAYING)
        return 1
    check("卡片开始播放", view.state, CameraView.PLAYING)
    frames_before = view.frames
    check_true("已有帧", frames_before > 0, f"frames={frames_before}")
    print(f"   首帧后 frames={frames_before}")

    # 2) 双击进详情（走主窗口真正那条路径），2 秒后自动关闭
    closer = {"done": False}

    def close_detail() -> None:
        dlg = QApplication.activeModalWidget()
        if isinstance(dlg, DetailDialog):
            closer["done"] = True
            print(f"   详情页已打开，会话所有者={type(playbacks().active_view).__name__}"
                  f" state={view.state}")
            dlg.done(0)
        else:
            QTimer.singleShot(300, close_detail)

    QTimer.singleShot(int(DETAIL_STAY * 1000), close_detail)
    win._on_card_clicked(did)  # type: ignore[attr-defined]
    pump(app, 0.3)

    check_true("详情页确实打开过", closer["done"], "")
    check("卡片重新播放（画面回来了）", view.state, CameraView.PLAYING)
    check("会话所有者是卡片", playbacks().active_view is view, True)
    check("提示文案已清掉", view._hint, "")

    frames_after = view.frames
    pump(app, 2.0)
    check_true("恢复后画面还在继续（帧数增长）",
               view.frames > frames_after, f"{frames_after} → {view.frames}")

    # 收尾：停播并归还观看位
    win.wall.stop_playback("验证结束")
    pump(app, 0.3)
    check("收尾后没有活动会话", playbacks().active, False)
    check("卡片回到空闲", view.state, CameraView.IDLE)

    win.close()
    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败：")
        for line in FAILS:
            print("  - " + line)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
