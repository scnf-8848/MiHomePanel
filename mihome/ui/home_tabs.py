# SPDX-License-Identifier: GPL-3.0-or-later
"""家庭切换栏。

原版米家 App 里家庭是"左上角一个下拉"，因为手机屏宽只够放一个。桌面端横向空间
富余，把家庭直接平铺成一排更好：一眼看到有几个家、当前在哪个家，切换只要一次点击，
不用先展开菜单。

这里有两件互相独立的事，都被做成了设置项：

**位置** ``placement``

* ``fixed`` —— 位置永远等于用户设定的顺序，激活与否不移动。切家庭时整排是静止的，
  只有样式在变，最稳。
* ``active_first`` —— 激活的家庭自动提到最左。好处是"当前的永远在第一个"，坏处是
  切换时其余标签要整体平移，所以加了 170ms 的位移动画把跳跃抹平。

**激活态** ``active_style``

* ``size``   —— 字号差（20 vs 16）+ accent 下划线，信息层级最清楚（默认）
* ``weight`` —— 字号统一，只有加粗和主色文字，最安静
* ``pill``   —— 字号统一，激活项背后垫一块 accent 淡色胶囊，最好认。
  这一档**所有标签都用同一个居中盒模型**：每个 rect 都比文字左右各宽
  ``PILL_PAD``，激活时胶囊正好铺满这个盒子。只对激活项居中会让它的文字凭空
  多出一段左偏移（看起来"偏右"），切换家庭时文字还会左右跳，见 ``paintEvent``。

无论哪种，**设定顺序本身都不会被改写**：``active_first`` 只是绘制时把激活项临时提到
最前，``prefs.home_order`` 里仍是用户拖出来的那一份。否则切一次家庭就把排序改一次，
设置页里会看到顺序自己在动。
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPoint, QRect, QSize, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import theme
from .widgets import draw_text

PLACEMENT_FIXED = "fixed"
PLACEMENT_ACTIVE_FIRST = "active_first"

STYLE_SIZE = "size"
STYLE_WEIGHT = "weight"
STYLE_PILL = "pill"

HEIGHT = 46
TAB_PAD = 6
PILL_PAD = 14
PILL_H = 34
ANIM_MS = 170

#: 下划线：宽=文字宽、高 2、距文字底 6（米家实测值，见风格参考 3.2）
UNDERLINE_H = 2
UNDERLINE_GAP = 6

# style → (激活字号, 激活字重, 未激活字号, 未激活字重, 间距)
# 字号照米家的房间标签行：激活 20sp、未激活 16sp。这一行在视觉上等价于米家的
# 标签行（家庭名那一行 24sp 留给了页面标题）。
_STYLE_SPEC: dict[str, tuple[int, QFont.Weight, int, QFont.Weight, int]] = {
    STYLE_SIZE: (theme.FS_TAB_ACTIVE, QFont.DemiBold, theme.FS_TAB, QFont.Medium, 22),
    STYLE_WEIGHT: (theme.FS_TAB, QFont.DemiBold, theme.FS_TAB, QFont.Medium, 22),
    STYLE_PILL: (theme.FS_TAB, QFont.DemiBold, theme.FS_TAB, QFont.Medium, 10),
}


def _font(size: int, weight: QFont.Weight) -> QFont:
    f = QFont()
    f.setPixelSize(size)
    f.setWeight(weight)
    return f


class HomeTabs(QWidget):
    home_selected = Signal(str)

    def __init__(self, palette: theme.Palette, *, interactive: bool = True) -> None:
        super().__init__()
        self._p = palette
        self._interactive = interactive
        self._homes: list[tuple[str, str]] = []  # 设定顺序，永不重排
        self._view: list[tuple[str, str]] = []  # 绘制顺序（active_first 时可能重排）
        self._widths: list[int] = []
        self._x: list[float] = []  # 当前横坐标，动画期间是小数
        self._target: list[float] = []
        self._from: list[float] = []
        self._active = ""
        self._hover = -1
        self._placement = PLACEMENT_FIXED
        self._style = STYLE_SIZE
        self.setFixedHeight(HEIGHT)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setMouseTracking(True)

        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(ANIM_MS)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)

    # -- 数据 ------------------------------------------------------------
    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self._recompute(animate=False)

    def configure(self, placement: str = "", style: str = "") -> None:
        """设置页改完调这里。空字符串表示这一项不动。"""
        if placement in (PLACEMENT_FIXED, PLACEMENT_ACTIVE_FIRST):
            self._placement = placement
        if style in _STYLE_SPEC:
            self._style = style
        self._recompute(animate=False)

    @property
    def placement(self) -> str:
        return self._placement

    @property
    def active_style(self) -> str:
        return self._style

    @property
    def content_inset(self) -> int:
        """胶囊档首项文字相对 widget 左边缘的缩进。

        pill 档每个 rect 都比文字左右各宽 ``PILL_PAD``，第一枚标签的文字天然离左边界
        14px，另外两档是 0。父布局用 ``setContentsMargins(-inset, ...)`` 把这段缩进
        抵消掉，标签行才能和周围的文字左对齐（``settings._preview_unit`` /
        ``main_window._refresh_tabs``）。切样式后要重新读这个值。
        """
        return PILL_PAD if self._style == STYLE_PILL else 0

    def set_homes(self, homes: list[tuple[str, str]], active: str) -> None:
        homes = list(homes)
        if active not in {name for name, _ in homes}:
            active = homes[0][0] if homes else ""
        changed = homes != self._homes or active != self._active
        self._homes = homes
        self._active = active
        if changed:
            self._recompute(animate=True)

    def set_active(self, name: str) -> None:
        if name != self._active:
            self._active = name
            self._recompute(animate=True)

    @property
    def active(self) -> str:
        return self._active

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.width(), HEIGHT)

    # -- 布局 ------------------------------------------------------------
    def _ordered(self) -> list[tuple[str, str]]:
        homes = list(self._homes)
        if self._placement == PLACEMENT_ACTIVE_FIRST and self._active:
            for i, (name, _) in enumerate(homes):
                if name == self._active:
                    if i:
                        homes.insert(0, homes.pop(i))
                    break
        return homes

    def _fonts(self) -> tuple[QFontMetrics, QFontMetrics]:
        a_size, a_w, i_size, i_w, _ = _STYLE_SPEC[self._style]
        return QFontMetrics(_font(a_size, a_w)), QFontMetrics(_font(i_size, i_w))

    def _recompute(self, *, animate: bool) -> None:
        view = self._ordered()
        fm_active, fm_idle = self._fonts()
        pad = PILL_PAD if self._style == STYLE_PILL else TAB_PAD
        gap = _STYLE_SPEC[self._style][4]

        widths: list[int] = []
        targets: list[float] = []
        x = 0.0
        for name, label in view:
            fm = fm_active if name == self._active else fm_idle
            w = max(28, fm.horizontalAdvance(label) + pad * 2)
            widths.append(w)
            targets.append(x)
            x += w + gap

        total = int(x - gap) if view else 0
        self._view = view
        self._widths = widths
        self._target = targets

        can_anim = animate and len(self._x) == len(targets) and self._x != targets
        if can_anim:
            self._anim.stop()
            self._from = list(self._x)
            self._anim.start()
        else:
            self._anim.stop()
            self._x = list(targets)

        # 动画途中标签可能还停在右边，宽度要够，否则右侧会被裁掉
        widest = max((xx + w for xx, w in zip(self._x, widths)), default=0)
        self.setFixedWidth(max(total, int(widest)))
        self.update()

    def _on_anim(self, value: float) -> None:
        self._x = [a + (b - a) * float(value) for a, b in zip(self._from, self._target)]
        self.update()

    # -- 绘制 ------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self._p
        a_size, a_weight, i_size, i_weight, _ = _STYLE_SPEC[self._style]

        for i, (name, label) in enumerate(self._view):
            x = int(round(self._x[i])) if i < len(self._x) else int(self._target[i])
            rect = QRect(x, 0, self._widths[i], self.height())
            if rect.right() < 0 or rect.left() > self.width():
                continue
            active = name == self._active
            size = a_size if active else i_size
            weight = a_weight if active else i_weight
            if active:
                color = pal.accent if self._style == STYLE_WEIGHT else pal.text
                if self._style == STYLE_PILL:
                    color = pal.accent
            elif i == self._hover:
                color = pal.text
            else:
                color = pal.text_mute

            if active and self._style == STYLE_PILL:
                pill = QRect(x, (self.height() - PILL_H) // 2, self._widths[i], PILL_H)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(pal.accent_soft))
                p.drawRoundedRect(pill, theme.RADIUS_PILL, theme.RADIUS_PILL)

            # 胶囊档**所有**标签统一居中：布局给每个 rect 留的左右 PILL_PAD 就是
            # 胶囊的内边距，激活时胶囊正好铺满盒子。以前只有激活项居中、其余左对齐，
            # 激活项文字平白多出 14px 左偏移（看起来"偏右"），切换家庭时文字还会在
            # 居中/左对齐之间来回跳。其余两档仍左对齐，下划线也以 rect.x() 起画。
            centered = self._style == STYLE_PILL
            draw_text(p, rect, label, size=size, color=color, weight=weight,
                      align=(Qt.AlignHCenter if centered else Qt.AlignLeft)
                      | Qt.AlignVCenter)
            if active and self._style == STYLE_SIZE:
                # 下划线只画在文字宽度上，不要撑满整块可点区域
                fm = QFontMetrics(_font(size, weight))
                w = min(rect.width(), fm.horizontalAdvance(label))
                top = rect.center().y() - fm.height() // 2
                # 量到字形底部而不是行框底部：行框里那段 leading 会让间距看起来偏大
                bottom = top + fm.ascent() + fm.descent()
                y = min(bottom + UNDERLINE_GAP, self.height() - UNDERLINE_H)
                bar = QRect(rect.x(), y, w, UNDERLINE_H)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(pal.accent))
                p.drawRoundedRect(bar, UNDERLINE_H / 2, UNDERLINE_H / 2)
        p.end()

    # -- 交互 ------------------------------------------------------------
    def _hit(self, pos: QPoint) -> int:
        for i in range(len(self._view)):
            x = int(round(self._x[i])) if i < len(self._x) else int(self._target[i])
            if QRect(x, 0, self._widths[i], self.height()).contains(pos):
                return i
        return -1

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        idx = self._hit(event.position().toPoint())
        if idx != self._hover:
            self._hover = idx
            self.setCursor(Qt.PointingHandCursor if idx >= 0 else Qt.ArrowCursor)
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = -1
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if not self._interactive or event.button() != Qt.LeftButton:
            return
        idx = self._hit(event.position().toPoint())
        if idx < 0:
            return
        name = self._view[idx][0]
        if name != self._active:
            self.set_active(name)
            self.home_selected.emit(name)


__all__ = [
    "PLACEMENT_ACTIVE_FIRST",
    "PLACEMENT_FIXED",
    "STYLE_PILL",
    "STYLE_SIZE",
    "STYLE_WEIGHT",
    "HomeTabs",
]
