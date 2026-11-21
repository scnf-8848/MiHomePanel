# SPDX-License-Identifier: GPL-3.0-or-later
"""全局下拉框（``ui/combo.py``）的验证。

跑法：

  ``.venv/Scripts/python.exe -X utf8 scripts/verify_combo.py``           离屏
  ``.venv/Scripts/python.exe -X utf8 scripts/verify_combo.py --native``  真机

**两套都要跑**。之前只跑离屏那一套，74 项全绿，用户真机上四角仍是黑块：
``QWidget.grab()`` 抓的是控件自己的 backing store，四角 alpha=0 很容易做到，
但平台原生窗口若没有 ``WS_EX_LAYERED`` + 8bit alpha，合成到屏幕上时这些"透明"
像素就是黑方块。``--native`` 直接查原生窗口属性，是唯一能挡住这类假绿的断言。

离屏那套量六件事（不需要设备、不需要网络）：

1. **v 形箭头**——收起是 v、展开是 ^，箭头区确实有墨迹且不吃到文字；
2. **弹出层真圆角**——四角 alpha=0、卡心不透明、当前项是圆角高亮而不是方框，
   且弹出层在控件**下方留 8px**（Qt 默认会把当前项对齐到控件、盖住它）；
3. **滚轮**——滚轮不改索引、事件被 ignore（原生 ``QComboBox`` 做对照，证明
   测试事件本身是有效的），键盘上下键仍能改（只禁滚轮，不禁明确的输入）；
4. **全局接线**——设置页两个、详情页 select 行都换成 ``Combo``，窗口级 QSS
   里不再残留旧的 ``QComboBox`` 规则（两处各写一份会互相盖）；
5. **主题**——切深色后主控件、箭头、弹出层一起换色；
6. **当前值打勾**——勾只画在 ``combo.currentIndex()`` 那一行；把视图的临时
   高亮挪到别的行（等同于鼠标划过去）后，值不变、勾不动、别的行不出现勾；
   换值后勾跟着走到新行。这一条专门挡住"高亮跟着鼠标跑就认不出原选项"。

中文在 offscreen 平台会渲染成豆腐块，所以这里全部量几何与像素，不读文字。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
NATIVE = "--native" in sys.argv
if NATIVE:
    # 真机模式故意不钉 DPR，跑在用户实际的 1.75 倍屏上
    os.environ["QT_QPA_PLATFORM"] = "windows"
    os.environ.pop("QT_SCALE_FACTOR", None)
else:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # 抓图量的是像素坐标，而控件几何是逻辑像素：钉住 DPR，免得在有缩放的机器上
    # 把 1.75 倍物理图当 1 倍来量（shot_ui.py 跑在真平台上就会遇到）。
    os.environ.setdefault("QT_SCALE_FACTOR", "1")

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QKeyEvent,
    QPainter,
    QWheelEvent,
)
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QComboBox,
    QStyle,
    QStyleOptionComboBox,
    QStyleOptionViewItem,
)

from mihome.core.spec import PropertySpec, ValueOption  # noqa: E402
from mihome.ui import theme  # noqa: E402
from mihome.ui.combo import (  # noqa: E402
    ARROW_RIGHT,
    ARROW_W,
    CHECK_W,
    ITEM_H,
    ITEM_INSET_X,
    ITEM_PAD_X,
    POPUP_BAR_W,
    POPUP_GAP,
    POPUP_MAX_EXTRA,
    VIEW_PAD,
    Combo,
    PopupDelegate,
    combo_qss,
    popup_qss,
)

FAILS: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {got!r}")
    if not ok:
        FAILS.append(f"{name} → 期望 {want!r}，实际 {got!r}")


def check_true(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(f"{name} {detail}")


def diff(a: QColor, b: QColor) -> int:
    return max(abs(a.red() - b.red()), abs(a.green() - b.green()),
               abs(a.blue() - b.blue()))


def wheel(app: QApplication, target, delta: int = -120) -> QWheelEvent:
    ev = QWheelEvent(QPointF(6, 6), QPointF(6, 6), QPoint(0, 0),
                     QPoint(0, delta), Qt.NoButton, Qt.NoModifier,
                     Qt.ScrollUpdate, False)
    app.sendEvent(target, ev)
    app.processEvents()
    return ev


def arrow_cols(combo: Combo, img, palette: theme.Palette) -> dict[int, float]:
    """右侧箭头区里，每一列墨迹的平均 y。用来判断画的是 v 还是 ^。"""
    bg = QColor(theme.field_bg(palette))
    w, h = combo.width(), combo.height()
    x0 = int(w - ARROW_RIGHT - ARROW_W - 3)
    x1 = int(w - ARROW_RIGHT + 3)
    cols: dict[int, float] = {}
    for x in range(max(0, x0), min(w - 3, x1)):
        ys = [y for y in range(2, h - 2)
              if diff(img.pixelColor(x, y), bg) > 80]
        if ys:
            cols[x] = sum(ys) / len(ys)
    return cols


def arrow_shape(cols: dict[int, float]) -> str:
    xs = sorted(cols)
    if len(xs) < 5:
        return "none"
    ends = (sum(cols[x] for x in xs[:2]) / 2 + sum(cols[x] for x in xs[-2:]) / 2) / 2
    m = len(xs) // 2
    mid = (cols[xs[m]] + cols[xs[m + 1 if m + 1 < len(xs) else m]]) / 2
    if mid > ends + 1.0:
        return "v"
    if mid < ends - 1.0:
        return "^"
    return "flat"


def new_combo(app: QApplication, palette: theme.Palette, n: int = 3) -> Combo:
    c = Combo(palette)
    c.addItems([f"选项{i + 1}" for i in range(n)])
    c.setFixedWidth(168)
    c.show()
    app.processEvents()
    return c


def local_slot(w: int, h: int = ITEM_H) -> tuple[int, int, int, int]:
    """单独画一行时，右侧勾槽的像素范围。和 delegate 里的计算保持同构。"""
    r = QRectF(0, 0, w, h).adjusted(ITEM_INSET_X, 1, -ITEM_INSET_X, -1)
    x1 = round(r.right() - ITEM_PAD_X)
    return x1 - CHECK_W, round(r.top()), x1, round(r.bottom())


def popup_slot(c: Combo, cont, row: int,
               dpr: float = 1.0) -> tuple[int, int, int, int]:
    """真实弹出层里第 row 行的勾槽（cont 坐标 → grab 出来的设备像素）。"""
    v = c.view()
    vr = v.visualRect(v.model().index(row, 0))
    top = v.mapTo(cont, vr.topLeft())
    x1 = top.x() + vr.width() - ITEM_INSET_X - ITEM_PAD_X
    x0 = x1 - CHECK_W
    y0, y1 = top.y() + 1, top.y() + vr.height() - 1
    return (round(x0 * dpr), round(y0 * dpr),
            round(x1 * dpr), round(y1 * dpr))


def count_accent(img: QImage, box: tuple[int, int, int, int],
                 pal: theme.Palette, tol: int = 90) -> int:
    """勾槽里接近 accent 色的像素数（抗锯齿边缘也算进去）。

    底色、文字、圆角描边都不接近 accent：浅色已选底 #EAF1FF、深色 #323E56
    与蓝勾的最大通道差都 > 90，不会误判成"有勾"。
    """
    accent = QColor(pal.accent)
    x0, y0, x1, y1 = box
    n = 0
    for y in range(max(0, y0), min(img.height(), y1)):
        for x in range(max(0, x0), min(img.width(), x1)):
            px = img.pixelColor(x, y)
            if px.alpha() and diff(px, accent) <= tol:
                n += 1
    return n


def painted_row(c: Combo, row: int, state, pal: theme.Palette,
                w: int = 200) -> QImage:
    """白盒：只把 delegate 画到一张图上，精确控制视图状态，不依赖真实鼠标。"""
    img = QImage(w, ITEM_H, QImage.Format_ARGB32)
    img.fill(QColor(theme.popup_bg(pal)))
    opt = QStyleOptionViewItem()
    opt.rect = QRect(0, 0, w, ITEM_H)
    opt.state = state
    opt.font = QFont("Microsoft YaHei UI")
    painter = QPainter(img)
    c.itemDelegate().paint(painter, opt, c.model().index(row, 0))
    painter.end()
    return img


# ---------------------------------------------------------------------------
def section_qss() -> None:
    print("\n1) 样式表：箭头留位、原生底抹干净、弹出层底色")
    for pal in (theme.LIGHT, theme.DARK):
        qss = combo_qss(pal)
        check_true(f"{pal.name}：右内边距给箭头留了 38px", "padding: 6px 38px" in qss)
        check_true(f"{pal.name}：原生下拉区/箭头都关掉",
                   "QComboBox::down-arrow" in qss and "width: 0px" in qss)
        check_true(f"{pal.name}：列表不许用 palette.highlight 画方底",
                   "selection-background-color: transparent" in qss
                   and "show-decoration-selected: 0" in qss)
        # 一行里四格挨着（编辑页 [类型][单位][值] 之类），下拉有边框、输入框没边框
        # 会读成两种控件：描边必须和 theme.field_qss 出的那支完全一样。
        check_true(f"{pal.name}：描边与输入框同一支（theme.field_border）",
                   theme.field_border(pal) in qss)
        pop = popup_qss(pal)
        check_true(f"{pal.name}：弹出层有圆角", f"border-radius: {theme.RADIUS_SMALL}px" in pop)
        # 底色不能写在 QSS 里：QFrame 一旦有 background/border，就会在圆角外
        # 补一层方底或斜接外框。颜色由 _PopupSurface 用 QPainterPath 画。
        check_true(f"{pal.name}：容器背景全透明（由自绘负责）",
                   "background: transparent" in pop and theme.panel_bg(pal) not in pop)
        # QFrame 一有 border，样式引擎就在圆角外再画一圈斜接的原生框（高 DPI
        # 下特别明显）。这是"方形背景色"最后残留的一层，钉死不许加回来。
        check_true(f"{pal.name}：弹出层不写 border（否则又多一圈方角框）",
                   "border: none" in pop and "1px solid" not in pop)
    # 深色下 card 和窗口底只差 2–5 档，照抄等于没浮起来（分类栏踩过）
    check_true("深色弹出层没有照抄 card",
               theme.panel_bg(theme.DARK) != theme.DARK.card,
               f"{theme.panel_bg(theme.DARK)} vs {theme.DARK.card}")

    # 黑块修好之后，同色会让整张菜单隐身进分区卡；边界必须来自底色差 + 自绘描边
    combo_src = (ROOT / "mihome/ui/combo.py").read_text(encoding="utf-8")
    check_true("浮层底色走 popup_bg", "theme.popup_bg(" in combo_src)
    check_true("描边自绘、不回到 QSS border",
               "theme.popup_border(" in combo_src
               and "border: none" in popup_qss(theme.LIGHT))
    for pal in (theme.LIGHT, theme.DARK):
        bg, line = QColor(theme.popup_bg(pal)), QColor(theme.popup_border(pal))
        check_true(f"{pal.name}：浮层与分区卡拉开 ≥ 8 档",
                   diff(bg, QColor(theme.panel_bg(pal))) >= 8,
                   f"{bg.name()} vs {theme.panel_bg(pal)}")
        check_true(f"{pal.name}：浮层描边与底色可辨", diff(line, bg) >= 10,
                   f"{line.name()} vs {bg.name()}")

    settings_src = (ROOT / "mihome/ui/settings.py").read_text(encoding="utf-8")
    detail_src = (ROOT / "mihome/ui/detail.py").read_text(encoding="utf-8")
    check_true("设置窗口 QSS 里不再有 QComboBox 规则",
               re.search(r"QComboBox\s*\{", settings_src) is None)
    # 注意别拿 "box.setStyleSheet" 当特征串：_group() 里那个 box 是分组卡，
    # 它本来就该有自己的底色。要看的是有没有又写一份 QComboBox 样式表。
    check_true("详情页不再逐控件写下拉框样式表",
               "QComboBox{{" not in detail_src
               and "selection-background-color" not in detail_src)
    check_true("两处都用 Combo 构造",
               "Combo(self._p)" in settings_src and "Combo(palette)" in detail_src)


def section_arrow(app: QApplication) -> None:
    print("\n2) 收起 v / 展开 ^，且不吃到文字")
    for pal in (theme.LIGHT, theme.DARK):
        c = new_combo(app, pal)
        img = c.grab().toImage()
        cols = arrow_cols(c, img, pal)
        check(f"{pal.name}：收起画的是 v", arrow_shape(cols), "v")
        # Qt 会把当前项文字 elide 到 editField 的右边界。直接量这条边界和自绘
        # 箭头之间还剩多少，别让详情页那些长中文选项贴到箭头上。
        opt = QStyleOptionComboBox()
        c.initStyleOption(opt)
        field = c.style().subControlRect(
            QStyle.CC_ComboBox, opt, QStyle.SC_ComboBoxEditField, c)
        gap = c.width() - ARROW_RIGHT - ARROW_W - 1 - field.right()
        check_true(f"{pal.name}：文字区与箭头留白 ≥ 12px", gap >= 12,
                   f"{gap:.0f}px")
        check_true(f"{pal.name}：箭头有足够墨迹（≥ 6 列）", len(cols) >= 6,
                   f"{len(cols)} 列")
        # 文字不能进箭头区：文字右边界由 30px padding 保证，量一下墨迹最左端
        if cols:
            check_true(f"{pal.name}：箭头贴右侧 13px 起",
                       min(cols) >= c.width() - ARROW_RIGHT - ARROW_W - 3,
                       f"最左 {min(cols)}")

        c.showPopup()
        app.processEvents()
        app.processEvents()
        check(f"{pal.name}：展开后 is_popup_open", c.is_popup_open, True)
        up = arrow_shape(arrow_cols(c, c.grab().toImage(), pal))
        check(f"{pal.name}：展开翻成 ^", up, "^")
        c.hidePopup()
        app.processEvents()
        check(f"{pal.name}：收起翻回 v",
              arrow_shape(arrow_cols(c, c.grab().toImage(), pal)), "v")
        c.close()


def section_popup(app: QApplication) -> None:
    print("\n3) 弹出层：四角透明、delegate 自绘、当前项是圆角高亮")
    for pal in (theme.LIGHT, theme.DARK):
        c = new_combo(app, pal)
        c.showPopup()
        app.processEvents()
        app.processEvents()
        cont = c.view().parentWidget()
        check_true(f"{pal.name}：container 是透明窗口",
                   cont.testAttribute(Qt.WA_TranslucentBackground))
        check_true(f"{pal.name}：无边框", bool(cont.windowFlags() & Qt.FramelessWindowHint))
        check_true(f"{pal.name}：无系统阴影",
                   bool(cont.windowFlags() & Qt.NoDropShadowWindowHint))
        check(f"{pal.name}：列表用自绘 delegate",
              type(c.view().itemDelegate()).__name__, PopupDelegate.__name__)

        # Qt 默认把当前项对齐到 combo，列表会盖住控件；现在必须留在下方、
        # 控件和 "^" 全程可见。右边缘对齐是参考 WorkBuddy 设置页的做法。
        geo = cont.geometry()
        bottom = c.mapToGlobal(QPoint(0, c.height())).y()
        right = c.mapToGlobal(QPoint(c.width(), 0)).x()
        check_true(f"{pal.name}：弹出层在控件下方 ≥ {POPUP_GAP}px（不遮挡）",
                   geo.top() - bottom >= POPUP_GAP - 1,
                   f"控件底 {bottom} / 弹出层顶 {geo.top()}")
        check_true(f"{pal.name}：右边缘与控件对齐",
                   abs(geo.right() - right) <= 2, f"{geo.right()} vs {right}")

        img = cont.grab().toImage()
        W, H = img.width(), img.height()
        for label, (x, y) in (("左上", (0, 0)), ("右上", (W - 1, 0)),
                              ("左下", (0, H - 1)), ("右下", (W - 1, H - 1))):
            check(f"{pal.name}：弹出层{label}角透明", img.pixelColor(x, y).alpha(), 0)
        card = QColor(theme.popup_bg(pal))
        # 取右侧中段而非底部中点：底边那条 1px 自绘描边会混进采样点，
        # 真机 1.75 倍屏下尤其明显（量出来会比底色亮/暗 5–6 档）。
        center = img.pixelColor(W - 12, H // 2)
        check_true(f"{pal.name}：卡心不透明且是浮层底色",
                   center.alpha() == 255 and diff(center, card) <= 4,
                   f"{center.name()} vs {card.name()}")
        check_true(f"{pal.name}：浮层底和分区卡不同色（否则菜单会隐身）",
                   diff(card, QColor(theme.panel_bg(pal))) >= 8,
                   f"{card.name()} vs {theme.panel_bg(pal)}")
        edge = img.pixelColor(3, H // 2)
        check_true(f"{pal.name}：左侧中段被圆角卡盖住", edge.alpha() > 0,
                   f"alpha={edge.alpha()}")

        # 当前项：左上那一小格因为圆角被切掉，往里 10px 才是高亮色
        v = c.view()
        top = v.mapTo(cont, v.visualRect(v.model().index(0, 0)).topLeft())
        hl = QColor(theme.panel_row_selected(pal))
        x, y = top.x() + ITEM_INSET_X, top.y() + 1
        inner = img.pixelColor(x + 10, y + 10)
        corner = img.pixelColor(x + 1, y + 1)
        check_true(f"{pal.name}：当前项有高亮块", diff(inner, hl) <= 8,
                   f"{inner.name()} vs {hl.name()}")
        check_true(f"{pal.name}：高亮块是圆角（左上被切）", diff(corner, hl) > 8,
                   f"{corner.name()}")
        # 行高由 delegate 定，Qt 默认只有 21
        check(f"{pal.name}：行高 34", v.visualRect(v.model().index(0, 0)).height(), 34)
        c.hidePopup()
        c.close()


def popup_clipped(c: Combo) -> list[str]:
    """弹出层里会被省略号吃掉的项。逐项按 delegate 的框宽 elide 一遍。

    框宽和 ``PopupDelegate.paint`` 里那个 ``box`` 同构：
    ``view 宽 - 左右外缩进 - 左右内留白 - 右侧勾槽``。
    """
    view = c.view()
    fm = QFontMetrics(view.font())
    box = view.width() - 2 * ITEM_INSET_X - 2 * ITEM_PAD_X - CHECK_W
    out = []
    for i in range(c.count()):
        text = c.itemText(i)
        if fm.elidedText(text, Qt.ElideRight, max(1, box)) != text:
            out.append(text)
    return out


def section_popup_width(app: QApplication) -> None:
    """弹出层宽度**和控件分开算**（用户 11-13 第二轮报）。

    控件宽度是布局给的：编辑页一行四格，单位那格 80px、音箱条静默那格 88px，
    写「毫秒」「不静默」进去必然被省略成「…」「不…」。弹出层是浮层，没人跟它
    抢地方——按最长一项撑开即可，只要别超过 ``POPUP_MAX_EXTRA`` 和屏幕。
    """
    print("\n7) 弹出层比控件宽：长选项不再被省略号吃掉")
    cases = (
        (80, ["毫秒", "秒"], "编辑页单位格"),
        (88, ["不静默", "静默"], "音箱条静默格"),
        (118, ["发虚拟事件", "延迟", "发音箱指令"], "编辑页动作类型"),
        (176, ["小爱音箱Pro", "卧室的小爱音箱 Play 增强版"], "编辑页选音箱"),
    )
    for pal in (theme.LIGHT, theme.DARK):
        for width, items, label in cases:
            c = Combo(pal)
            c.setFixedWidth(width)
            c.move(300, 200)  # 离屏幕左边远一点，右对齐才不会被夹到 0
            c.addItems(items)
            c.show()
            app.processEvents()
            c.showPopup()
            app.processEvents()
            app.processEvents()
            cont, view = c._popup, c.view()  # noqa: SLF001
            fm = QFontMetrics(view.font())
            longest = max(items, key=len)
            text_w = fm.horizontalAdvance(longest)
            need = (text_w + 2 * ITEM_INSET_X + 2 * ITEM_PAD_X + CHECK_W
                    + POPUP_BAR_W + 2 * VIEW_PAD)
            check_true(f"{pal.name}·{label}：控件 {width}px 太窄，弹出层撑宽了",
                       cont.width() > width, f"{width} → {cont.width()}")
            check_true(f"{pal.name}·{label}：view 铺满容器",
                       abs(view.width() - cont.width()) <= 2,
                       f"view {view.width()} / cont {cont.width()}")
            check_true(f"{pal.name}·{label}：宽度够放最长项（含滚动条与勾槽）",
                       cont.width() >= need or cont.width() >= text_w + CHECK_W,
                       f"需 {need} / 实 {cont.width()}")
            check_true(f"{pal.name}·{label}：一项都没被省略",
                       not popup_clipped(c), str(popup_clipped(c)))
            geo = cont.geometry()
            right = c.mapToGlobal(QPoint(c.width(), 0)).x()
            check_true(f"{pal.name}·{label}：加宽后右边缘仍与控件对齐",
                       abs(geo.right() - right) <= 2, f"{geo.right()} vs {right}")
            check_true(f"{pal.name}·{label}：没盖住控件",
                       geo.top() - c.mapToGlobal(QPoint(0, c.height())).y() >= POPUP_GAP - 1)
            c.hidePopup()
            c.close()

        # 控件本来就够宽 → 不动它，别无事生非地长出去
        wide = Combo(pal)
        wide.setFixedWidth(400)
        wide.move(300, 200)
        wide.addItems(["毫秒", "秒"])
        wide.show()
        app.processEvents()
        wide.showPopup()
        app.processEvents()
        check_true(f"{pal.name}：控件够宽时弹出层不额外增长",
                   abs(wide._popup.width() - 400) <= 2,  # noqa: SLF001
                   f"{wide._popup.width()}")  # noqa: SLF001
        wide.hidePopup()
        wide.close()

        # 超长项：上界是控件宽 + POPUP_MAX_EXTRA，不许无限长出去
        huge = Combo(pal)
        huge.setFixedWidth(150)
        huge.move(300, 200)
        huge.addItem("一条特别长的自定义自动化规则名称" * 6)
        huge.show()
        app.processEvents()
        huge.showPopup()
        app.processEvents()
        check_true(f"{pal.name}：超长项时宽度有上限（≤ 控件 + {POPUP_MAX_EXTRA}）",
                   huge._popup.width() <= 150 + POPUP_MAX_EXTRA,  # noqa: SLF001
                   f"{huge._popup.width()}px")  # noqa: SLF001
        check_true(f"{pal.name}：上限内也不出屏幕",
                   huge._popup.geometry().right()  # noqa: SLF001
                   <= (QApplication.primaryScreen().availableGeometry().right() + 1),
                   f"right={huge._popup.geometry().right()}")  # noqa: SLF001
        huge.hidePopup()
        huge.close()


def section_wheel(app: QApplication) -> None:
    print("\n4) 滚轮不改值，键盘仍可用")
    # 对照：同一串事件打在原生 QComboBox 上确实会改值，证明事件构造没问题
    native = QComboBox()
    native.addItems(["甲", "乙", "丙"])
    native.show()
    app.processEvents()
    for _ in range(2):
        wheel(app, native)
    check("对照：原生 QComboBox 会被滚轮改值", native.currentIndex() > 0, True)
    native.close()

    for pal in (theme.LIGHT, theme.DARK):
        c = new_combo(app, pal)
        events = [wheel(app, c) for _ in range(3)]
        check(f"{pal.name}：三次滚轮后索引不变", c.currentIndex(), 0)
        check(f"{pal.name}：滚轮事件全被 ignore",
              all(not e.isAccepted() for e in events), True)

        c.showPopup()
        app.processEvents()
        wheel(app, c.view())
        check(f"{pal.name}：展开时滚轮也不改值", c.currentIndex(), 0)
        c.hidePopup()
        app.processEvents()

        # 只禁滚轮，不禁键盘（上下键是明确的、有意的输入）
        app.sendEvent(c, QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Down, Qt.NoModifier))
        app.processEvents()
        check(f"{pal.name}：按 ↓ 仍能换项", c.currentIndex(), 1)
        c.close()


def section_wiring(app: QApplication) -> None:
    print("\n5) 全局接线：设置页 / 设备详情页 / 主题切换")
    import verify_tree as vt  # 复用它造 Store + SettingsDialog 的 fixture

    _, dlg = vt.fresh(theme.LIGHT)
    dlg.show()
    dlg.nav.setCurrentRow(0)
    app.processEvents()
    for name in ("cb_placement", "cb_style"):
        box = getattr(dlg, name)
        check(f"设置页 {name} 是 Combo", isinstance(box, Combo), True)
        check_true(f"设置页 {name} 自带样式表", bool(box.styleSheet()))
    # 切主题：两个控件和它们的箭头都跟着换
    dlg._apply_palette(theme.DARK)  # noqa: SLF001 - 白盒验证
    app.processEvents()
    check("切深色后 cb_placement 跟着换 palette", dlg.cb_placement._p, theme.DARK)  # noqa: SLF001
    base = QColor(theme.field_bg(theme.DARK))
    got = dlg.cb_placement.grab().toImage().pixelColor(4, dlg.cb_placement.height() // 2)
    check_true("切深色后控件底色变暗", diff(got, base) <= 6,
               f"{got.name()} vs {base.name()}")
    dlg.close()

    # 详情页的单选枚举现在优先用轨道（2026-11-12，和卡片统一），**放不下才退回
    # Combo**：项数超过 MAX_SEGMENTS 或自然宽度超过 SEG_MAX_W。Combo 这条通路
    # 必须还在——二十项的枚举只有它列得清楚。
    prop = PropertySpec(
        siid=2, piid=7, name="mode", urn="", description="", comment="模式",
        format="uint8", access=("read", "write"), unit=None, value_range=None,
        value_list=(ValueOption(1, "日光", "Day"), ValueOption(2, "夜光", "Night")),
        service="", service_label="",
    )
    many = PropertySpec(
        siid=2, piid=8, name="scene-mode", urn="", description="", comment="模式",
        format="uint8", access=("read", "write"), unit=None, value_range=None,
        value_list=tuple(
            ValueOption(i, f"模式{i}", f"M{i}") for i in range(1, 13)
        ),
        service="", service_label="",
    )
    from mihome.ui import card_controls as cc
    from mihome.ui.detail import ControlRow

    row_seg = ControlRow(prop, "模式", theme.LIGHT, "#3D7EFF", "select")
    check("两项枚举 → 详情页也用轨道", isinstance(row_seg.editor, cc.CardSeg), True)

    row = ControlRow(many, "模式", theme.LIGHT, "#3D7EFF", "select")
    check("十二项枚举 → 详情页退回 Combo", isinstance(row.editor, Combo), True)
    check_true("详情页下拉框没有另写 styleSheet 覆盖全局",
               "border-radius: 9px" in row.editor.styleSheet())
    row.set_value(2)  # ControlRow 里 isinstance(ed, QComboBox) 的分支仍要生效
    check("set_value 仍能按 itemData 选中", row.editor.currentIndex(), 1)


def section_check(app: QApplication) -> None:
    """6) 当前值打勾：勾钉在值上，不跟视图的临时高亮跑。

    这是"鼠标一移到别的选项就忘了原本选的是什么"的正解：``State_Selected``
    在下拉里只是视图的临时当前项，``combo.currentIndex()`` 才是值。两段验证：

    * 白盒——手工构造三种视图状态逐行画，精确检查勾与底色的归属；
    * 集成——真实展开后把临时高亮挪到别的行 / 换值，检查像素跟着谁动。
    """
    print("\n6) 当前值打勾：高亮跟着鼠标，勾不跟着")
    combo_src = (ROOT / "mihome/ui/combo.py").read_text(encoding="utf-8")
    check_true("勾的归属按 currentIndex 判定，不看视图状态",
               "index.row() == self._combo.currentIndex()" in combo_src)
    check_true("临时高亮用相对浮层底的悬停色（不是挖洞的 panel_row_hover）",
               "theme.popup_row_hover(" in combo_src
               and "theme.panel_row_hover(" not in combo_src)
    check_true("每行都预留同一个勾槽（文字边界不随鼠标抖）",
               "-(ITEM_PAD_X + CHECK_W)" in combo_src
               and CHECK_W >= 14, f"CHECK_W={CHECK_W}")

    base_state = QStyle.State_Enabled | QStyle.State_Active
    hot_state = base_state | QStyle.State_Selected | QStyle.State_MouseOver
    for pal in (theme.LIGHT, theme.DARK):
        c = new_combo(app, pal)
        slot = local_slot(200)
        bg = QColor(theme.popup_bg(pal))
        sel = QColor(theme.panel_row_selected(pal))
        hover = QColor(theme.popup_row_hover(pal))
        # 三种底两两可分：否则"已选"和"鼠标在这"又糊成一块
        check_true(f"{pal.name}：已选底 / 悬停底 / 浮层底可辨",
                   diff(sel, hover) >= 8 and diff(sel, bg) >= 8
                   and diff(hover, bg) >= 8,
                   f"{sel.name()} {hover.name()} {bg.name()}")

        # 第 0 行 = 当前值，但**故意不给它任何高亮状态**：勾必须还在
        row_checked = painted_row(c, 0, base_state, pal)
        n = count_accent(row_checked, slot, pal)
        check_true(f"{pal.name}：当前值行没有高亮态也画勾", n >= 8,
                   f"accent 像素 {n}")
        got = row_checked.pixelColor(100, ITEM_H // 2)
        check_true(f"{pal.name}：当前值行是稳定的选中底",
                   diff(got, sel) <= 8, f"{got.name()} vs {sel.name()}")

        # 第 1 行不是当前值，但把 Qt 会跟着鼠标改的那两个状态全给它
        row_hot = painted_row(c, 1, hot_state, pal)
        check(f"{pal.name}：临时高亮行不画勾",
              count_accent(row_hot, slot, pal), 0)
        got = row_hot.pixelColor(100, ITEM_H // 2)
        check_true(f"{pal.name}：临时高亮行只有一层淡中性底",
                   diff(got, hover) <= 8, f"{got.name()} vs {hover.name()}")
        row_plain = painted_row(c, 1, base_state, pal)
        check(f"{pal.name}：普通行不画勾",
              count_accent(row_plain, slot, pal), 0)
        got = row_plain.pixelColor(100, ITEM_H // 2)
        check_true(f"{pal.name}：普通行就是浮层底", diff(got, bg) <= 4,
                   f"{got.name()} vs {bg.name()}")
        c.close()

    # -- 真实弹出层：值放在第 1 项，避免"勾恰好在首行"掩盖问题 ----------
    for pal in (theme.LIGHT, theme.DARK):
        c = new_combo(app, pal)
        c.setCurrentIndex(1)
        c.showPopup()
        app.processEvents()
        app.processEvents()
        cont = c.view().parentWidget()
        img = cont.grab().toImage()
        rows = [count_accent(img, popup_slot(c, cont, r), pal) for r in range(3)]
        check_true(f"{pal.name}：展开时只有当前值那行有勾",
                   rows[1] >= 8 and rows[0] == 0 and rows[2] == 0, f"{rows}")

        # 等同于鼠标划到第 2 项：Qt 只改 view 的临时当前项，不改 combo 的值
        v = c.view()
        v.setCurrentIndex(v.model().index(2, 0))
        v.viewport().update()
        app.processEvents()
        img = cont.grab().toImage()
        rows = [count_accent(img, popup_slot(c, cont, r), pal) for r in range(3)]
        check(f"{pal.name}：临时高亮挪走后值没变", c.currentIndex(), 1)
        check_true(f"{pal.name}：临时高亮挪走后勾仍在原项",
                   rows[1] >= 8 and rows[0] == 0 and rows[2] == 0, f"{rows}")
        top = v.mapTo(cont, v.visualRect(v.model().index(2, 0)).topLeft())
        got = img.pixelColor(top.x() + 80, top.y() + ITEM_H // 2)
        check_true(f"{pal.name}：第 2 行确实拿到了临时高亮底",
                   diff(got, QColor(theme.popup_row_hover(pal))) <= 8,
                   f"{got.name()} vs {theme.popup_row_hover(pal)}")
        plain_top = v.mapTo(cont, v.visualRect(v.model().index(0, 0)).topLeft())
        got = img.pixelColor(plain_top.x() + 80, plain_top.y() + ITEM_H // 2)
        check_true(f"{pal.name}：第 0 行既无勾也无高亮",
                   diff(got, QColor(theme.popup_bg(pal))) <= 4, got.name())

        # 换值之后勾必须跟着走到新行，别把勾缓存成"第一次展开的那行"
        c.setCurrentIndex(2)
        v.viewport().update()
        app.processEvents()
        img = cont.grab().toImage()
        rows = [count_accent(img, popup_slot(c, cont, r), pal) for r in range(3)]
        check_true(f"{pal.name}：换值后勾移到新行",
                   rows[2] >= 8 and rows[0] == 0 and rows[1] == 0, f"{rows}")
        c.hidePopup()
        c.close()


def section_native(app: QApplication) -> None:
    """真机窗口层。

    离屏那套全绿也挡不住四角黑块：``grab()`` 只看控件自己的 backing store，
    平台窗口没 alpha 时，透明像素在屏幕上被合成成纯黑。这里查的是原生窗口
    本身的 ``WS_EX_LAYERED`` 和 QWindow 的 alpha buffer，并用已确认没毛病的
    ``Menu`` 做判据有效性对照。
    """
    print("\n7) 真机窗口层：WS_EX_LAYERED / 8bit alpha / 不遮挡 / 交互 / 勾")
    import ctypes

    from PySide6.QtTest import QTest  # noqa: PLC0415

    from mihome.ui.menu import Menu  # noqa: PLC0415

    check("跑在 windows 原生平台", app.platformName(), "windows")
    if app.platformName() != "windows":
        return

    GWL_EXSTYLE, WS_EX_LAYERED = -20, 0x00080000

    def layered(w) -> bool:
        ex = ctypes.windll.user32.GetWindowLongW(int(w.winId()), GWL_EXSTYLE)
        return bool(ex & WS_EX_LAYERED)

    # 判据本身有没有效：Menu 在构造时设透明属性，真机上四角是干净的
    m = Menu(None, theme.DARK)
    m.addAction("甲")
    m.addAction("乙")
    m.popup(QPoint(120, 120))
    app.processEvents()
    app.processEvents()
    check_true("对照：Menu 确实是 layered（判据有效）", layered(m))
    m.close()

    for pal in (theme.LIGHT, theme.DARK):
        c = Combo(pal)
        c.addItems(["选项一", "选项二", "选项三"])
        c.setFixedWidth(190)
        c.show()
        app.processEvents()
        c.showPopup()
        for _ in range(4):
            app.processEvents()
        cont = c.view().parentWidget()
        fmt = cont.windowHandle().format()
        check_true(f"{pal.name}：QWindow 有 8bit alpha（不是 -1）",
                   fmt.alphaBufferSize() == 8 and fmt.hasAlpha(),
                   f"alpha={fmt.alphaBufferSize()} hasAlpha={fmt.hasAlpha()}")
        check_true(f"{pal.name}：原生窗口是 WS_EX_LAYERED（四角不会变黑块）",
                   layered(cont))

        img = cont.grab().toImage()
        W, H = img.width(), img.height()
        for label, (x, y) in (("左上", (0, 0)), ("右上", (W - 1, 0)),
                              ("左下", (0, H - 1)), ("右下", (W - 1, H - 1))):
            check(f"{pal.name}：真机{label}角 alpha=0", img.pixelColor(x, y).alpha(), 0)
        center = img.pixelColor(W - 12, H // 2)
        check_true(f"{pal.name}：真机卡心是 popup_bg",
                   center.alpha() == 255 and diff(center, QColor(theme.popup_bg(pal))) <= 4,
                   f"{center.name()} vs {theme.popup_bg(pal)}")
        # 真机 1.75 倍屏：grab() 是物理像素，勾槽的逻辑坐标要乘 DPR
        dpr = app.devicePixelRatio()
        n = count_accent(img, popup_slot(c, cont, 0, dpr), pal)
        check_true(f"{pal.name}：真机当前值行有勾（DPR {dpr} 下也量得到）",
                   n >= 8, f"accent 像素 {n}")

        geo = cont.geometry()
        bottom = c.mapToGlobal(QPoint(0, c.height())).y()
        check_true(f"{pal.name}：真机上弹出层仍没盖住控件",
                   geo.top() - bottom >= POPUP_GAP - 1,
                   f"控件底 {bottom} / 弹出层顶 {geo.top()}")

        # 重建窗口之后 Qt 的鼠标抓取、选中、收起都得照常工作
        v = c.view()
        r = v.visualRect(v.model().index(1, 0))
        QTest.mouseClick(v.viewport(), Qt.LeftButton, Qt.NoModifier, r.center())
        for _ in range(4):
            app.processEvents()
        check(f"{pal.name}：点第二项能选中", c.currentIndex(), 1)
        check(f"{pal.name}：选完自动收起", c.is_popup_open, False)
        check(f"{pal.name}：popup 已隐藏", cont.isVisible(), False)

        hwnd = int(cont.winId())
        c.showPopup()
        for _ in range(3):
            app.processEvents()
        check(f"{pal.name}：二次展开复用同一个原生窗口", int(cont.winId()), hwnd)
        check_true(f"{pal.name}：二次展开仍是 layered", layered(cont))
        img2 = cont.grab().toImage()
        rows = [count_accent(img2, popup_slot(c, cont, r, dpr), pal)
                for r in range(3)]
        check_true(f"{pal.name}：点选后重新展开，勾跟着值走到第 1 行",
                   rows[1] >= 8 and rows[0] == 0 and rows[2] == 0, f"{rows}")
        c.hidePopup()
        app.processEvents()
        c.close()


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(13)
    app.setFont(font)

    if NATIVE:
        section_native(app)
    else:
        section_qss()
        section_arrow(app)
        section_popup(app)
        section_popup_width(app)
        section_wheel(app)
        section_wiring(app)
        section_check(app)

    print("\n" + ("全部通过" if not FAILS else f"{len(FAILS)} 项失败：\n  " + "\n  ".join(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
