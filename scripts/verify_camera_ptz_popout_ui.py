# SPDX-License-Identifier: GPL-3.0-or-later
"""真机端到端：独立窗口里的云台方向盘按下真的能转。

``verify_camera_ptz_popout.py`` 用离屏 + 假控件验位置与显隐，这个脚本验**真链路**：

1. 卡片点播放 → 等首帧（有会话才有 producer）；
2. 双击进详情页 → 开独立窗口（画面搬过去，会话不重建）；
3. 等方向盘出现且可点，在圆的上方点一下；
4. 断言：指令被 go2rtc 受理（``ok: true``）、方向是「上」、点一下只发一条；
   同时检查方向盘贴在画面左下角（不是浮在窗口左下角，标题栏要扣掉）。

需要登录态、一台在线摄像头，以及**自建版 go2rtc**（``scripts/build_go2rtc.py``）。
用官方构建时方向盘整块不显示，脚本按「不支持」告退而不是失败。

用法：``.venv/Scripts/python.exe -X utf8 scripts/verify_camera_ptz_popout_ui.py``
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MIHOME_NO_TRAY", "1")

from PySide6.QtCore import QPoint, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mihome.core.camera import PTZ_UP  # noqa: E402
from mihome.core.camera import shared as camera_service  # noqa: E402
from mihome.ui import theme  # noqa: E402
from mihome.ui.camera_card import CameraCard  # noqa: E402
from mihome.ui.camera_popout import PTZ_MARGIN  # noqa: E402
from mihome.ui.camera_view import CameraView  # noqa: E402
from mihome.ui.detail import DetailDialog  # noqa: E402
from mihome.ui.main_window import MainWindow  # noqa: E402

FAILS: list[str] = []
FIRST_FRAME_WAIT = 30.0
CALLS: list[tuple[int, bool]] = []


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


def _install_spy() -> None:
    service = camera_service()
    real = service.ptz

    def spy(did: str, op: int) -> bool:
        ok = real(did, op)
        CALLS.append((op, ok))
        return ok

    service.ptz = spy  # type: ignore[method-assign]


def _press_point(pad) -> QPoint:
    r = pad.width() // 2
    return QPoint(r, r - int(r * 0.75))


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

    if not wait_until(app, lambda: not win.store.loading and bool(win.store.devices), 25):
        print("设备列表没加载出来（没登录？）")
        return 1
    pump(app, 1.2)

    cam = next(
        (d for d in win.store.devices.values()
         if d.online and "camera" in d.node.model.lower()),
        None,
    )
    if cam is None:
        print("没有在线的摄像头")
        return 1
    print(f"摄像头：{cam.name} / {cam.node.model}")

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
        wait_until(app, lambda: on_screen_card() is not None, 5)
        card = on_screen_card() or any_cam

    view = card.view
    view._play_btn.clicked.emit()
    if not wait_until(app, lambda: view.state == CameraView.PLAYING, FIRST_FRAME_WAIT):
        check("卡片出画", view.state, CameraView.PLAYING)
        return 1
    check("卡片开始播放", view.state, CameraView.PLAYING)
    print(f"   首帧后 frames={view.frames}")

    did = card.state.did
    print(f"-- 设备 {card.state.name}（{did}）--")

    # 构建支持性要在播放之后问：go2rtc 没起来时 ptz_supported() 一律 False
    if not camera_service().ptz_supported():
        print("这个 go2rtc 构建不带云台接口（官方版）。先跑 scripts/build_go2rtc.py")
        win.wall.stop_playback("验证结束")  # noqa: SLF001
        return 1

    def in_detail() -> None:
        dlg = QApplication.activeModalWidget()
        if not isinstance(dlg, DetailDialog):
            QTimer.singleShot(300, in_detail)
            return

        popup = None
        try:
            dlg._toggle_camera_popout()  # noqa: SLF001
            pump(app, 1.0)
            popup = dlg._cam_popout  # noqa: SLF001
            if popup is None:
                check("独立窗口建起来", popup is not None, True)
                return

            pad = popup._ptz  # noqa: SLF001
            if pad is None:
                check("独立窗口里有方向盘", pad is not None, True)
                return

            got = wait_until(app, lambda: pad.isVisible() and pad._ready, 8)  # noqa: SLF001
            check("方向盘出现", pad.isVisible(), True)
            check("方向盘可点", pad._ready, True)  # noqa: SLF001

            geo = popup._view.geometry()  # noqa: SLF001
            check("贴画面左边（12px）", pad.x(), geo.left() + PTZ_MARGIN)
            check(
                "贴画面底部（12px，扣掉标题栏）",
                pad.y(),
                geo.top() + geo.height() - pad.height() - PTZ_MARGIN,
            )
            check_true(
                "整块在画面里", geo.contains(pad.geometry()), f"{pad.geometry()} ⊂ {geo}"
            )
            check_true(
                "在左半边（不压右下角那排键）",
                pad.geometry().center().x() < geo.center().x(),
                f"{pad.geometry().center().x()} < {geo.center().x()}",
            )

            if got:
                _install_spy()
                pt = _press_point(pad)
                QTest.mouseClick(pad, Qt.LeftButton, Qt.NoModifier, pos=pt)
                pump(app, 0.8)
                check("点一下只发一条", len(CALLS), 1)
                check("方向是「上」", CALLS[0][0] if CALLS else None, PTZ_UP)
                check("go2rtc 受理了", CALLS[0][1] if CALLS else None, True)
        finally:
            # 断言里抛异常也必须把窗口收掉，否则 exec() 永远不返回
            if popup is not None:
                popup.close()
            dlg.done(0)

    QTimer.singleShot(1200, in_detail)
    win._on_card_clicked(did)  # noqa: SLF001
    win.wall.stop_playback("验证结束")  # noqa: SLF001

    print(f"\n结果：{len(FAILS)} 项失败" if FAILS else "\n全部通过")
    for f in FAILS:
        print(" -", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
