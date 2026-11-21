# SPDX-License-Identifier: GPL-3.0-or-later
"""验证「点压暗遮罩关闭设备子窗口」。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_dim_click.py``

覆盖：

1. 点遮罩关闭，且只关当前这一层（主窗口不动）；
2. 点子窗口内部不关；
3. 多层浮层（详情页里再弹确认框）只关最上层，点父弹窗区域也算点遮罩；
4. 子窗口内部按下、拖到遮罩松开不关；
5. 遮罩按下、拖进子窗口松开不关；
6. Esc 与点遮罩走同一条 ``done(Rejected)`` 路径；
7. 滚轮不关；
8. 压暗关闭（``DIM_ALPHA = 0``）时主窗口一个像素都不变，命中测试仍在；
9. 没有压暗之后，弹窗靠投影 + 1px 描边仍分得开（四种背景组合，压在卡片色上）。

说明：这里直接向弹窗的 ``DimClickShield`` 子窗口投递点击，正是真实窗口系统里
点击落在这一层上的路径（应用模态会丢弃落到主窗口上的点击，见 chrome.py 的注释）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, QTimer, Qt  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QWidget  # noqa: E402

from mihome.ui import theme  # noqa: E402
from mihome.ui.chrome import (  # noqa: E402
    DIM_ALPHA,
    SHADOW_PAD,
    DimClickShield,
    DimOverlay,
    WindowFrame,
    exec_with_dim_click,
    floating_size,
    shadow_alpha,
    surface_bg,
    surface_border,
)

FAILS: list[str] = []


def contrast(a: str, b: str) -> float:
    """WCAG 对比度（和 verify_theme.py 同一条公式）。"""

    def lum(h: str) -> float:
        r, g, bl = (int(h.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))

        def f(c: float) -> float:
            return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(bl)

    la, lb = lum(a), lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


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
        sh = shield_of(d)
        seen["shield"] = sh
        # 当前弹窗自己的面板仍是洞，点它不关
        inside = sh.mapFromGlobal(d.frameGeometry().center()) if sh else QPoint()
        click_shield(app, sh, inside)
        state["inner_after_inside"] = d.isVisible()
        # 父弹窗区域现在算「外侧」：洞只挖当前这一层（2026-11-15 修）。
        # 取父窗左上角内侧一点，避开居中的子弹窗。
        parent_global = outer.frameGeometry().topLeft() + QPoint(10, 10)
        parent = sh.mapFromGlobal(parent_global) if sh else QPoint()
        state["parent_in_mask"] = bool(sh and sh.mask().contains(parent))
        click_shield(app, sh, parent)
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
    check("多层：点子弹窗内部不关", state.get("inner_after_inside") is True)
    check("多层：父弹窗区域也可点（洞只挖当前层）",
          state.get("parent_in_mask") is True)
    check("多层：点父弹窗等同点遮罩，关掉内层", state.get("inner_visible") is False)
    check("多层：内层关闭时外层还在", state.get("outer_visible") is True)
    check("多层：内层走 done(Rejected)", inner.done_calls == [QDialog.Rejected],
          str(inner.done_calls))
    check("多层：外层没有被连带关闭", state.get("outer_done") == [],
          str(state.get("outer_done")))
    check("多层：再点遮罩才关外层", state.get("outer_after") is False)
    check("多层：外层最终 done(Rejected)", outer.done_calls == [QDialog.Rejected],
          str(outer.done_calls))
    check("多层：主窗口始终在", host.isVisible())


def _flush_deletes(app: QApplication) -> None:
    """跑一轮 DeferredDelete：``deleteLater()`` 只是排队，要发事件才真删。"""
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def test_closed_is_deleted(app: QApplication, host: QWidget) -> None:
    """关掉就销毁，别让弹窗攒在主窗口名下（2026-11-12 用户报的闪烁 + 终端刷错）。

    以前 ``exec_with_dim_click`` 只 ``shield.hide()``，弹窗本身留活：parent 是主窗口，
    C++ 对象被 parent 一直持有，挂在它身上的 ``WindowFrame`` / ``DimClickShield``
    两个事件过滤器也跟着活；而 Python 侧只由调用方的局部变量引用，函数一返回就可能
    被 GC——Qt 再回调时就报 ``'WindowFrame' object has no attribute 'win'``。
    """
    dialogs = []
    for _ in range(5):
        dlg = ProbeDialog(host)
        dialogs.append(dlg)
        QTimer.singleShot(0, dlg.reject)
        exec_with_dim_click(dlg)
    _flush_deletes(app)
    from shiboken6 import isValid

    alive = [i for i, d in enumerate(dialogs) if isValid(d)]
    check("连续开关 5 次，没有一个弹窗还活着", not alive, f"活着 {alive}")
    left = host.findChildren(QDialog)
    check("主窗口名下不再挂着对话框（不会越攒越多）", not left, f"剩 {len(left)} 个")
    # 过滤器本身也得随弹窗一起走：它们都是 dialog 的子对象
    frames = [w for w in host.findChildren(QWidget) if getattr(w, "_window_frame", None)]
    check("没有残留的 WindowFrame", not frames, f"剩 {len(frames)} 个")
    # 反向：只 hide 不 delete 的对话框会一直活着，证明上面三条真能测到东西
    kept = ProbeDialog(host)
    kept.hide()
    _flush_deletes(app)
    check("只 hide 不 delete 的弹窗仍然活着（对照）", isValid(kept), "")
    kept.deleteLater()
    _flush_deletes(app)


def test_no_dim(app: QApplication, host: QWidget) -> None:
    """压暗取消之后：主窗口一个像素不变，但命中层照旧（两条互不影响）。"""
    check("压暗参数取 0（浅/深两档都关）",
          DIM_ALPHA == {"light": 0.0, "dark": 0.0}, str(DIM_ALPHA))

    before = host.grab().toImage()
    overlay = DimOverlay(host)
    for pal in (theme.LIGHT, theme.DARK):
        overlay.set_palette(pal)
        check(f"{pal.name}：蒙版强度为 0", overlay._alpha == 0.0, str(overlay._alpha))
        overlay.cover(True)
        app.processEvents()
        img = overlay.grab().toImage()
        spot = [(3, 3), (img.width() // 2, img.height() // 2),
                (img.width() - 4, img.height() - 4)]
        worst = max(QColor(img.pixelColor(x, y)).alpha() for x, y in spot)
        check(f"{pal.name}：蒙版不画任何像素", worst == 0, f"max alpha={worst}")
        overlay.cover(False)

    overlay.cover(True)
    app.processEvents()
    after = host.grab().toImage()
    diff = sum(1 for y in range(0, before.height(), 7) for x in range(0, before.width(), 7)
               if before.pixel(x, y) != after.pixel(x, y))
    check("蒙版盖上去之后主窗口逐像素不变", diff == 0, f"{diff} 个采样点不同")
    overlay.cover(False)

    # 最大化 / 非最大化：蒙版是跟着 root 尺寸铺的，两种尺寸下同样"什么都不画"。
    # （主窗口自身的外框与最大化分支在 verify_window_corners.py 里另有覆盖）
    for label, size in (("非最大化", (900, 600)), ("最大化", (1280, 800))):
        host.resize(*size)
        app.processEvents()
        base = host.grab().toImage()
        overlay.cover(True)
        app.processEvents()
        top = host.grab().toImage()
        overlay.cover(False)
        same = all(base.pixel(x, y) == top.pixel(x, y)
                   for y in range(0, base.height(), 11)
                   for x in range(0, base.width(), 11))
        check(f"{label}：蒙版盖满 {base.width()}x{base.height()} 且像素不变", same)

    overlay.deleteLater()

    # 命中测试那条线必须还在：弹窗打开时点击层仍然铺满主窗口，且遮罩区可点
    dlg = ProbeDialog(host)

    def act(d, s):
        sh = shield_of(d)
        s["alpha"] = sh.grab().toImage().pixelColor(QPoint(20, 20)).alpha() if sh else 0
        s["mask"] = bool(sh and sh.mask().contains(QPoint(20, 20)))
        click_shield(app, sh, QPoint(20, 20))
        s["closed"] = not d.isVisible()

    _code, seen = run_with(dlg, act)
    check("压暗为 0 时点击层依旧可点（alpha>0）", int(seen.get("alpha") or 0) > 0,
          f"alpha={seen.get('alpha')}")
    check("压暗为 0 时遮罩区仍在 mask 里", seen.get("mask") is True)
    check("压暗为 0 时点外圈仍然关窗", seen.get("closed") is True)


def test_dialog_edges(app: QApplication) -> None:
    """没有压暗之后，弹窗的投影 + 1px 描边要能独立撑起边界。

    取样口径：
    * 投影：面板外紧贴的一行 alpha，以及它落在**卡片色**（卡片密集区的最坏情况）
      上压暗的 luma 档位——浅色 26~38 档、深色 11~18 档是"看得见但不发脏"的带，
      超出上界就是黑晕，低于下界就看不出浮起；
    * 描边：面板最外一行的颜色与 ``surface_border`` 同族，对**面板底**和
      **卡片色**都要 ≥ 1.1:1。
    """
    bands = {"light": (26, 38), "dark": (11, 18)}
    for preset, mode in (("sand", "light"), ("sand", "dark"),
                         ("dawn", "light"), ("dawn", "dark")):
        p = theme.palette_for(preset, mode)
        dlg = QDialog()
        dlg.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        dlg.setAttribute(Qt.WA_TranslucentBackground, True)
        frame = WindowFrame(
            dlg, resizable=False, radius=theme.RADIUS_CARD,
            shadow=SHADOW_PAD, shadow_alpha=shadow_alpha(p),
            fill=lambda: surface_bg(p),
            border=lambda: surface_border(p),
        )
        dlg.resize(*floating_size(420, 280))
        dlg.show()
        app.processEvents()

        img = dlg.grab().toImage()
        panel = frame.panel_rect()
        mid = panel.center().x()
        near = QColor(img.pixelColor(mid, panel.top() - 1)).alpha()
        far = QColor(img.pixelColor(mid, 1)).alpha()
        card = theme.blend(p.bg, p.card, p.card_alpha)
        shadowed = theme.blend(card, "#000000", near / 255.0)
        drop = theme.luma(card) - theme.luma(shadowed)
        lo, hi = bands[mode]
        check(f"{preset}/{mode}：面板外有投影", near >= 12, f"贴边 alpha={near}")
        check(f"{preset}/{mode}：投影往外衰减", near > far, f"{near} → {far}")
        check(f"{preset}/{mode}：压暗档位在 {lo}~{hi}（不丢边界、不发脏）",
              lo <= drop <= hi,
              f"{theme.luma(card):.1f} → {theme.luma(shadowed):.1f}（{drop:.1f} 档）")

        edge = QColor(img.pixelColor(mid, panel.top())).name()
        fill = surface_bg(p)
        check(f"{preset}/{mode}：描边对面板底看得见",
              contrast(edge, fill) >= 1.1,
              f"{edge} vs {fill} {contrast(edge, fill):.2f}:1")
        check(f"{preset}/{mode}：描边压在卡片色上也看得见",
              contrast(edge, card) >= 1.1,
              f"{edge} vs {card} {contrast(edge, card):.2f}:1")
        if mode == "dark":
            # 深色档最弱的一环：投影落在比卡片更暗的底色上。
            # 黑色投影在暗底上绝对差很小，所以这里只要求"仍在"，主要靠描边。
            bg = p.bg
            bg_shadowed = theme.blend(bg, "#000000", near / 255.0)
            drop_bg = theme.luma(bg) - theme.luma(bg_shadowed)
            check(f"{preset}/{mode}：投影落在底色上仍压暗 ≥ 5 档", drop_bg >= 5,
                  f"{theme.luma(bg):.1f} → {theme.luma(bg_shadowed):.1f}（{drop_bg:.1f} 档）")
            check(f"{preset}/{mode}：描边对底色 ≥ 1.3:1（暗底主信号）",
                  contrast(edge, bg) >= 1.3,
                  f"{edge} vs {bg} {contrast(edge, bg):.2f}:1")
        # 面板底本身也要和卡片色分得开（投影之外的第二重信号）
        check(f"{preset}/{mode}：面板底与卡片色分得开",
              contrast(fill, card) >= 1.05,
              f"{fill} vs {card} {contrast(fill, card):.2f}:1")

        dlg.close()
        dlg.deleteLater()
    _flush_deletes(app)


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
    test_closed_is_deleted(app, host)
    test_no_dim(app, host)
    test_dialog_edges(app)
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
