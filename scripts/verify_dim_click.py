# SPDX-License-Identifier: GPL-3.0-or-later
"""验证「点压暗遮罩关闭设备子窗口」。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_dim_click.py``

覆盖：

1. 点遮罩关闭，且只关当前这一层（主窗口不动）；
2. 点子窗口内部不关；
3. 多层浮层（详情页里再弹确认框）只关最上层；
4. 子窗口内部按下、拖到遮罩松开不关；
5. 遮罩按下、拖进子窗口松开不关；
6. Esc 与点遮罩走同一条 ``done(Rejected)`` 路径；
7. 滚轮不关。

说明：这里直接向弹窗的 ``DimClickShield`` 子窗口投递点击，正是真实窗口系统里
点击落在这一层上的路径（应用模态会丢弃落到主窗口上的点击，见 chrome.py 的注释）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, QTimer, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QWidget  # noqa: E402

from mihome.ui.chrome import DimClickShield, exec_with_dim_click  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(f"{name} {detail}")


class ProbeDialog(QDialog):
    """只记录 done() 的结果。DetailDialog 也是靠 done() 停播并释放观看位。"""

    def __init__(self, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setGeometry(300, 200, 240, 160)
        self.done_calls: list[int] = []

    def done(self, result: int) -> None:
        self.done_calls.append(int(result))
        super().done(result)


def run_with(dlg: ProbeDialog, act) -> tuple[int, dict]:
    """在 exec 的事件循环里执行 act，返回 (exec 退出码, act 里记下的状态)。"""
    seen: dict = {}

    def step() -> None:
        try:
            act(dlg, seen)
        finally:
            if dlg.isVisible():
                dlg.reject()

    QTimer.singleShot(0, step)
    code = exec_with_dim_click(dlg)
    seen["code"] = code
    return code, seen


def shield_of(dlg: QDialog) -> DimClickShield | None:
    return dlg.findChild(DimClickShield)


def make_host(app: QApplication) -> QWidget:
    host = QWidget()
    host.resize(900, 600)
    host.show()
    app.processEvents()
    return host


def click_shield(app: QApplication, shield: DimClickShield, pos: QPoint) -> None:
    QTest.mouseClick(shield.windowHandle(), Qt.LeftButton, Qt.NoModifier, pos)


def test_click_dim(app: QApplication, host: QWidget) -> None:
    dlg = ProbeDialog(host)
    close_seen: dict = {}

    def act(d, seen):
        sh = shield_of(d)
        seen["shield"] = sh
        seen["shield_visible"] = bool(sh and sh.isVisible())
        seen["cover"] = sh.geometry() if sh else None
        # 遮罩区域（洞外）与弹窗区域（洞里）
        seen["dim_in_mask"] = bool(sh and sh.mask().contains(QPoint(20, 20)))
        # Windows 对分层窗口是逐像素命中测试：alpha=0 的像素收不到鼠标。
        # 这一层必须画 alpha>0，否则真机上就是"看着有遮罩、点哪都没反应"。
        seen["alpha"] = sh.grab().toImage().pixelColor(QPoint(20, 20)).alpha() if sh else 0
        local_dialog = d.frameGeometry().translated(-sh.x(), -sh.y()) if sh else None
        seen["hole_in_mask"] = bool(sh and local_dialog is not None
                                    and sh.mask().contains(local_dialog.center()))
        click_shield(app, sh, QPoint(20, 20))
        close_seen["visible"] = d.isVisible()

    code, seen = run_with(dlg, act)
    check("点遮罩：点击层可见", seen.get("shield_visible") is True)
    check("点遮罩：覆盖主窗口", seen.get("cover") == host.geometry(),
          str(seen.get("cover")))
    check("点遮罩：遮罩区域可点击", seen.get("dim_in_mask") is True)
    check("点遮罩：压暗区 alpha>0（Windows 命中测试前提）",
          int(seen.get("alpha") or 0) > 0, f"alpha={seen.get('alpha')}")
    check("点遮罩：弹窗区域被挖空", seen.get("hole_in_mask") is False)
    check("点遮罩：弹窗关闭", close_seen.get("visible") is False)
    check("点遮罩：走 done(Rejected)", dlg.done_calls == [QDialog.Rejected],
          str(dlg.done_calls))
    check("点遮罩：主窗口不受影响", host.isVisible())


def test_inside_no_close(app: QApplication, host: QWidget) -> None:
    dlg = ProbeDialog(host)
    state: dict = {}

    def act(d, seen):
        # 点在弹窗自己的窗口上：等同于视频画面 / 滑条这些内部控件
        seen["shield"] = shield_of(d)
        QTest.mouseClick(d.windowHandle(), Qt.LeftButton, Qt.NoModifier, QPoint(30, 30))
        state["visible"] = d.isVisible()
        state["done"] = list(d.done_calls)

    _, _ = run_with(dlg, act)
    check("点弹窗内部：不关闭", state.get("visible") is True)
    check("点弹窗内部：没有 done 调用", state.get("done") == [], str(state.get("done")))


def test_esc_same(app: QApplication, host: QWidget) -> None:
    esc = ProbeDialog(host)

    def act(d, seen):
        QTest.keyClick(d, Qt.Key_Escape)
        seen["visible"] = d.isVisible()

    esc_code, esc_seen = run_with(esc, act)
    check("Esc：关闭弹窗", esc_seen.get("visible") is False)
    check("Esc：走 done(Rejected)", esc.done_calls == [QDialog.Rejected],
          str(esc.done_calls))

    dim = ProbeDialog(host)

    def act2(d, seen):
        click_shield(app, shield_of(d), QPoint(20, 20))

    dim_code, _ = run_with(dim, act2)
    check("Esc 与点遮罩退出码一致", esc_code == dim_code == QDialog.Rejected,
          f"{esc_code}/{dim_code}")


def test_press_inside_release_dim(app: QApplication, host: QWidget) -> None:
    dlg = ProbeDialog(host)
    state: dict = {}

    def act(d, seen):
        sh = shield_of(d)
        # 弹窗内部按下（弹窗会拿到原生抓取），再向遮罩投递松开
        QTest.mousePress(d.windowHandle(), Qt.LeftButton, Qt.NoModifier, QPoint(30, 30))
        QTest.mouseRelease(sh.windowHandle(), Qt.LeftButton, Qt.NoModifier, QPoint(20, 20))
        state["visible"] = d.isVisible()
        state["done"] = list(d.done_calls)

    run_with(dlg, act)
    check("内部按下拖到遮罩松开：不关闭", state.get("visible") is True)
    check("内部按下拖到遮罩松开：没有 done 调用", state.get("done") == [],
          str(state.get("done")))


def test_press_dim_release_inside(app: QApplication, host: QWidget) -> None:
    dlg = ProbeDialog(host)
    state: dict = {}

    def act(d, seen):
        sh = shield_of(d)
        local_dialog = d.frameGeometry().translated(-sh.x(), -sh.y())
        QTest.mousePress(sh.windowHandle(), Qt.LeftButton, Qt.NoModifier, QPoint(20, 20))
        # 原生抓取会把松开也送回遮罩，只是坐标落在弹窗矩形里
        QTest.mouseRelease(sh.windowHandle(), Qt.LeftButton, Qt.NoModifier,
                           local_dialog.center())
        state["visible"] = d.isVisible()
        state["done"] = list(d.done_calls)

    run_with(dlg, act)
    check("遮罩按下拖进弹窗松开：不关闭", state.get("visible") is True)
    check("遮罩按下拖进弹窗松开：没有 done 调用", state.get("done") == [],
          str(state.get("done")))


def test_wheel_no_close(app: QApplication, host: QWidget) -> None:
    dlg = ProbeDialog(host)
    state: dict = {}

    def act(d, seen):
        sh = shield_of(d)
        QTest.wheelEvent(sh.windowHandle(), QPointF(20, 20), QPoint(0, -120))
        state["visible"] = d.isVisible()

    run_with(dlg, act)
    check("滚轮压在遮罩上：不关闭", state.get("visible") is True)


def test_nested(app: QApplication, host: QWidget) -> None:
    outer = ProbeDialog(host)
    outer.setGeometry(250, 150, 400, 300)
    inner = ProbeDialog(outer)
    inner.setGeometry(350, 250, 200, 140)
    state: dict = {}

    def inner_act(d, seen):
        seen["shield"] = shield_of(d)
        click_shield(app, shield_of(d), QPoint(10, 10))
        state["inner_visible"] = d.isVisible()
        state["outer_visible"] = outer.isVisible()
        state["outer_done"] = list(outer.done_calls)

    def outer_act(d, seen):
        QTimer.singleShot(0, lambda: inner_act(inner, {}))
        inner_code = exec_with_dim_click(inner)
        state["inner_code"] = inner_code
        # 内层关掉后，再点外层遮罩
        click_shield(app, shield_of(d), QPoint(10, 10))
        state["outer_after"] = d.isVisible()

    code, _ = run_with(outer, outer_act)
    check("多层：内层遮罩关闭内层", state.get("inner_visible") is False)
    check("多层：内层关闭时外层还在", state.get("outer_visible") is True)
    check("多层：内层走 done(Rejected)", inner.done_calls == [QDialog.Rejected],
          str(inner.done_calls))
    check("多层：外层没有被连带关闭", state.get("outer_done") == [],
          str(state.get("outer_done")))
    check("多层：再点遮罩才关外层", state.get("outer_after") is False)
    check("多层：外层最终 done(Rejected)", outer.done_calls == [QDialog.Rejected],
          str(outer.done_calls))
    check("多层：主窗口始终在", host.isVisible())


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    host = make_host(app)
    test_click_dim(app, host)
    test_inside_no_close(app, host)
    test_esc_same(app, host)
    test_press_inside_release_dim(app, host)
    test_press_dim_release_inside(app, host)
    test_wheel_no_close(app, host)
    test_nested(app, host)
    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败：")
        for line in FAILS:
            print("  -", line)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
