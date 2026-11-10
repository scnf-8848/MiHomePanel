# SPDX-License-Identifier: GPL-3.0-or-later
"""真机端到端：详情页里的云台方向盘按下真的能转。

前面 ``verify_camera_ptz.py`` 用假的 ``camera_service().ptz`` 验交互与显隐，
这个脚本验的是**真链路**：

1. 卡片点播放 → 等首帧（有会话才有 producer，云台指令才受理）；
2. 走 ``MainWindow._on_card_clicked`` 进详情页（等同双击）；
3. 等方向盘出现且可点，在圆的上方按下、按住 0.75 秒、松开；
4. 断言：指令全部被 go2rtc 受理（``ok: true``）、方向是「上」、按住期间补发多条、
   松开后不再发；同时检查方向盘确实排在画面**下方**。

需要登录态、一台在线摄像头，以及**自建版 go2rtc**（``scripts/build_go2rtc.py``
装的那份）；用官方构建的话方向盘会整块隐藏，脚本按「不支持」告退而不是失败。

用法：
  .venv/Scripts/python.exe -X utf8 scripts/verify_camera_ptz_ui.py
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
from mihome.ui.camera_view import CameraView  # noqa: E402
from mihome.ui.detail import DetailDialog  # noqa: E402
from mihome.ui.main_window import MainWindow  # noqa: E402

FAILS: list[str] = []
FIRST_FRAME_WAIT = 30.0
HOLD_S = 0.75

#: 真实指令的去向：(op, 是否受理)。由 spy 填
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
    """套一层真实现：既让指令真的发出去，也把结果留下来断言。"""
    service = camera_service()
    real = service.ptz

    def spy(did: str, op: int) -> bool:
        ok = real(did, op)
        CALLS.append((op, ok))
        return ok

    service.ptz = spy  # type: ignore[method-assign]


def _press_point(pad) -> QPoint:
    """圆的上方 3/4 半径处：落在「上」那 90° 扇区正中。"""
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
    pump(app, 1.0)

    did = card.state.did
    view = card.view
    assert view is not None
    print(f"-- 设备 {card.state.name}（{did}）--")

    # 1) 卡片先播起来（云台指令走的是正在取流的那条连接，producer 在才有得发）
    view._play_btn.clicked.emit()
    if not wait_until(app, lambda: view.state == CameraView.PLAYING, FIRST_FRAME_WAIT):
        check("卡片出画", view.state, CameraView.PLAYING)
        return 1
    check("卡片开始播放", view.state, CameraView.PLAYING)
    print(f"   首帧后 frames={view.frames}")

    # 构建支持性：官方 go2rtc 没有这条路由，方向盘本就不该出现。
    # 必须在播放之后问——没起进程时 ptz_supported() 一律返回 False。
    if not camera_service().ptz_supported():
        print("这个 go2rtc 构建不带云台接口（官方版）。先跑 scripts/build_go2rtc.py")
        win.wall.stop_playback("验证结束")
        return 1

    # 2) 进详情页，在里面按住方向盘上方
    _install_spy()
    seen: dict[str, bool] = {"opened": False, "pad": False}

    def in_detail() -> None:
        dlg = QApplication.activeModalWidget()
        if not isinstance(dlg, DetailDialog):
            QTimer.singleShot(300, in_detail)
            return
        seen["opened"] = True
        pad = getattr(dlg, "_ptz", None)
        if pad is None:
            check("详情页里有方向盘", pad is not None, True)
            dlg.done(0)
            return

        # 画面已经在放（从卡片接过来的会话），方向盘应当立刻可点
        got_pad = wait_until(app, lambda: pad.isVisible() and pad._ready, 20)
        seen["pad"] = got_pad
        check("方向盘出现", pad.isVisible(), True)
        check("方向盘可点", pad._ready, True)

        # 位置：必须排在画面下方，而且与画面、与卡片下沿的间隔要一样宽
        # （用户 2026-11-11 反馈"圆盘几乎贴着画面"，要求按卡片下边距对齐）
        cam_bottom = dlg.camera_view.mapTo(
            dlg, dlg.camera_view.rect().bottomLeft()
        ).y()
        pad_top = pad.mapTo(dlg, pad.rect().topLeft()).y()
        pad_bottom = pad.mapTo(dlg, pad.rect().bottomLeft()).y()
        card = pad.parentWidget().parentWidget()  # 圆盘 → ptz_host → 分组卡片
        card_bottom = card.mapTo(dlg, card.rect().bottomLeft()).y()
        gap_top = pad_top - cam_bottom
        gap_bottom = card_bottom - pad_bottom
        check_true("方向盘在画面下方", gap_top > 0, f"画面底 {cam_bottom} / 盘顶 {pad_top}")
        check_true(
            "与画面、与卡片下沿的间隔一致",
            abs(gap_top - gap_bottom) <= 2,
            f"{gap_top} vs {gap_bottom}",
        )

        try:
            if got_pad:
                pt = _press_point(pad)
                QTest.mousePress(pad, Qt.LeftButton, Qt.NoModifier, pos=pt)
                pump(app, HOLD_S)
                QTest.mouseRelease(pad, Qt.LeftButton, Qt.NoModifier, pos=pt)
                held = len(CALLS)
                pump(app, 0.6)
                check_true(
                    "按住期间补发多条", held >= 2, f"{HOLD_S:.2f}s 共 {held} 条"
                )
                check_true("松手后不再发", len(CALLS) == held, f"{held} → {len(CALLS)}")

                # 用户报的"点一下转太多"：一次点击只能走一格。真点一下（按下即松开）
                before_click = len(CALLS)
                QTest.mouseClick(pad, Qt.LeftButton, Qt.NoModifier, pos=pt)
                pump(app, 0.7)
                check(
                    "点一下只发一条",
                    len(CALLS) - before_click,
                    1,
                )
        finally:
            # 断言里抛异常也必须把模态框关掉，否则 exec() 永远不返回
            dlg.done(0)

    QTimer.singleShot(1200, in_detail)
    win._on_card_clicked(did)  # type: ignore[attr-defined]
    pump(app, 0.3)

    check("详情页确实打开过", seen["opened"], True)
    check_true("指令真的发出去过", bool(CALLS), str(CALLS[:6]))
    check_true("方向都是「上」", all(op == PTZ_UP for op, _ in CALLS), str(CALLS[:6]))
    check_true("go2rtc 全部受理", all(ok for _, ok in CALLS), str(CALLS[:6]))

    # 3) 收尾
    win.wall.stop_playback("验证结束")
    pump(app, 0.3)
    win.close()
    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败：")
        for line in FAILS:
            print("  - " + line)
        return 1
    print(f"全部通过（{len(CALLS)} 条真实指令）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
