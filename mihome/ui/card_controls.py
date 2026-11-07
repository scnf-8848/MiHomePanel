"""卡片内联控件：直接摆进设备卡片的 body 区，不必打开详情窗。

四条约定，四个控件都遵守：

1. **高度写死，宽度交给模板**。滑条/步进器 36 高，胶囊 26 高；内部栅格按
   ``width()`` 实时重算，所以同一控件在 1 列卡和 2 列卡里都不用改代码。
2. **拖动只改本地视觉，松手才发一次命令**。mijiaAPI 走云端 HTTP，一次往返几百
   毫秒；边拖边发会把请求队列堵满，设备反应还会滞后于手。拖动过程中只发
   ``previewed``（让卡片能即时染色/更新数字），``committed`` 在松手时发一次。
3. **拖动期间忽略外部赋值**。设备上报可能正好在拖动中到达，直接 set_value 会让
   滑块跳回去。``set_value()`` 在 ``_dragging`` 为真时自动跳过。
4. **不吞掉卡片的点击语义**。这些都是真实 QWidget，落在它们身上的鼠标事件不会
   冒泡给卡片，所以「点空白开详情、点滑条调亮度」不需要额外判断——这是不用
   QPainter 手绘滑条的主要理由（拖动捕获、移出控件后的追踪都是白送的）。
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..core.catalog import format_value
from ..core.spec import PropertySpec
from . import theme
from .widgets import ToggleSwitch, draw_text

#: 一行控件的高度。卡片 body 区 76 高，正好放两行（36 + 4 + 36）。
ROW_H = 36
#: 滑条左侧属性名、右侧数值的固定宽度
LABEL_W = 40
VALUE_W = 48
TRACK_H = 6
THUMB_D = 14
#: 胶囊控件的高度
CHIP_H = 26
#: 电容触摸的最小可点高度。桌面端鼠标可以更小，但卡片在触摸屏上也要能用。
MIN_HIT_H = 24


def kelvin_color(kelvin: float) -> QColor:
    """色温 → RGB（Tanner Helland 近似）。

    纯物理换算下 6500K 已经接近纯白，整条轨道会失去"冷暖方向感"，所以冷端
    再往蓝偏一点（最多 55%），让渐变一眼能看出哪头是冷。
    """
    t = max(1000.0, min(40000.0, float(kelvin))) / 100.0
    if t <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(t) - 161.1195681661
        b = 0.0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    else:
        r = 329.698727446 * ((t - 60) ** -0.1332047592)
        g = 288.1221695283 * ((t - 60) ** -0.0755148492)
        b = 255.0
    color = QColor(
        max(0, min(255, int(r))),
        max(0, min(255, int(g))),
        max(0, min(255, int(b))),
    )
    if kelvin > 4500:
        # 纯物理色温到 6500K 已经是纯白，轨道冷端会"消失"。这里最多往蓝偏 80%，
        # 让冷端在浅色卡片上也能被一眼认出来。
        mix = min(0.80, (kelvin - 4500) / 2000 * 0.80)
        color = QColor(theme.blend(color.name(), "#AFD0FF", mix))
    return color


def _numeric_range(prop: PropertySpec) -> tuple[float, float, float]:
    lo, hi, step = prop.value_range or (0, 100, 1)
    lo, hi = float(lo), float(hi)
    # 有些属性是 uint32 全域（0..4294967295），滑条按 int32 夹住才不丢精度到溢出
    hi = min(hi, 2**31 - 1)
    lo = max(lo, -(2**31))
    if hi <= lo:
        hi = lo + 1
    return lo, hi, max(1.0, float(step or 1))


class CardControl(QWidget):
    """载体：卡片内所有控件的公共部分（主题、置灰、尺寸策略）。

    ``CardBody`` 用这个类型持有它们——滑条、步进、读数对 ``sync`` 来说都是
    "能 ``set_value``、能置灰"的东西，具体是哪个不影响装配逻辑。
    """

    def __init__(
        self,
        prop: PropertySpec,
        palette: theme.Palette,
        accent: str,
        *,
        label: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.prop = prop
        self._p = palette
        self._accent = accent
        self._label = label or prop.label
        self._dimmed = False
        self._write = prop.writeable
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)

    # -- 主题 ------------------------------------------------------------
    def apply_theme(self, palette: theme.Palette, accent: str) -> None:
        self._p = palette
        self._accent = accent
        self.update()

    def set_dimmed(self, dim: bool) -> None:
        """设备关着但控件还留在卡片上（灯的亮度条）时压一档透明度。"""
        if dim != self._dimmed:
            self._dimmed = dim
            self.update()

    def _ink(self) -> str:
        return self._p.text_mute if self._dimmed else self._p.text_soft

    def _fmt(self, value: float) -> str:
        return format_value(self.prop, int(round(value)))

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(120, self.height())


# ---------------------------------------------------------------------------
# 滑条：亮度、色温、窗帘开合度
# ---------------------------------------------------------------------------


class CardSlider(CardControl):
    """图标 + 轨道 + 数值。``gradient=True`` 时轨道本身是色带（色温用）。"""

    previewed = Signal(float)  # 拖动中的实时值（只更新界面，别发命令）
    committed = Signal(int)  # 松手/滚轮之后才发

    HEIGHT = ROW_H

    def __init__(
        self,
        prop: PropertySpec,
        palette: theme.Palette,
        accent: str,
        *,
        label: str = "",
        gradient: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(prop, palette, accent, label=label, parent=parent)
        self._lo, self._hi, self._step = _numeric_range(prop)
        self._value = self._lo
        self._gradient = gradient
        self._dragging = False
        self.setFixedHeight(self.HEIGHT)

    # -- 值 --------------------------------------------------------------
    @property
    def dragging(self) -> bool:
        return self._dragging

    def value(self) -> float:
        return self._value

    def set_value(self, value: object, animate: bool = False) -> None:
        # 拖动中不接外部值：设备上报晚到会让滑块往回跳一格，手感像卡住
        if self._dragging or value is None:
            return
        try:
            v = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        v = min(self._hi, max(self._lo, v))
        if abs(v - self._value) > 1e-6:
            self._value = v
            self.update()

    # -- 栅格 ------------------------------------------------------------
    def _span(self) -> tuple[float, float]:
        x0 = float(LABEL_W + 8)
        x1 = float(max(x0 + 40, self.width() - VALUE_W - 8))
        return x0, x1

    def _ratio(self) -> float:
        return (self._value - self._lo) / (self._hi - self._lo)

    def _value_at(self, x: float) -> float:
        x0, x1 = self._span()
        r = min(1.0, max(0.0, (x - x0) / max(1.0, x1 - x0)))
        raw = self._lo + r * (self._hi - self._lo)
        raw = self._lo + round((raw - self._lo) / self._step) * self._step
        return min(self._hi, max(self._lo, raw))

    # -- 绘制 ------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(0.55 if self._dimmed else 1.0)
        h = self.height()
        cy = h / 2.0

        draw_text(p, QRect(0, 0, LABEL_W, h), self._label, size=12, color=self._ink())

        x0, x1 = self._span()
        track = QRect(int(x0), int(cy - TRACK_H / 2), int(x1 - x0), TRACK_H)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.blend(self._p.card, self._p.text, 0.13)))
        p.drawRoundedRect(track, TRACK_H / 2, TRACK_H / 2)

        ratio = max(0.0, min(1.0, self._ratio()))
        if self._gradient:
            grad = QLinearGradient(x0, 0.0, x1, 0.0)
            # 只插两端的话，中间会被线性插值拉成灰白（橙→淡蓝的中点没有色相）。
            # 按色温本身取 5 个档，才是「橙→暖白→白→冷白」的真实过渡。
            for i in range(5):
                t = i / 4.0
                grad.setColorAt(t, kelvin_color(self._lo + (self._hi - self._lo) * t))
            p.setBrush(grad)
            p.drawRoundedRect(track, TRACK_H / 2, TRACK_H / 2)
        else:
            filled = int(track.width() * ratio)
            if filled > 1:
                p.setBrush(QColor(self._accent))
                p.drawRoundedRect(
                    QRect(track.x(), track.y(), filled, track.height()),
                    TRACK_H / 2,
                    TRACK_H / 2,
                )

        thumb = QPointF(x0 + (x1 - x0) * ratio, cy)
        p.setBrush(QColor("#FFFFFF"))
        p.setPen(QPen(QColor(theme.blend(self._p.text, "#000000", 0.12 if not self._p.is_dark else 0.0)), 1))
        r = THUMB_D / 2 + (1 if self._dragging else 0)
        p.drawEllipse(thumb, r, r)

        draw_text(
            p,
            QRect(self.width() - VALUE_W, 0, VALUE_W, h),
            self._fmt(self._value),
            size=12,
            color=self._p.text if not self._dimmed else self._ink(),
            align=Qt.AlignRight | Qt.AlignVCenter,
        )
        p.end()

    # -- 交互 ------------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton or not self.isEnabled():
            return
        self._dragging = True
        self._update_from(event.position().x())
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging and self.isEnabled():
            self._update_from(event.position().x())
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton or not self._dragging:
            return
        self._dragging = False
        self._update_from(event.position().x())
        self.committed.emit(int(round(self._value)))
        event.accept()

    def wheelEvent(self, event) -> None:  # noqa: N802
        """桌面端刚需：滚轮微调 ±1 步。"""
        if not self.isEnabled():
            return
        # 上滚加、下滚减（angleDelta 正数是向上）
        step = 1 if event.angleDelta().y() > 0 else -1
        target = min(self._hi, max(self._lo, self._value + step * self._step))
        if target != self._value:
            self._value = target
            self.update()
            self.committed.emit(int(round(target)))
        event.accept()

    def _update_from(self, x: float) -> None:
        v = self._value_at(x)
        if abs(v - self._value) < 1e-6:
            return
        self._value = v
        self.update()
        self.previewed.emit(v)


# ---------------------------------------------------------------------------
# 步进器：空调温度
# ---------------------------------------------------------------------------


class CardStepper(CardControl):
    """``26°  −  +``（详情页）/ ``− 26°  +``（卡片 body）。按住不放连续加减，16°→30° 不用点十四下。

    ``centered=True`` 时把「减号 + 数值 + 加号」当成**一整组**居中，并把减号挪到
    数字左侧（``− 26° +``）。卡片 body 有 312 宽（2×1），靠左贴右会在中间留出
    200px 空白，看着像少画了一块。数字两边各一枚按钮，也是温控面板 / 空调遥控器
    的固有样子——「左边调低、右边调高」比「两个按钮都挤在右边」好按得多，
    手指不用在同一个位置分辨哪个是减号。
    """

    committed = Signal(int)

    HEIGHT = ROW_H
    BTN = 26
    BTN_GAP = 6
    #: 数值与减号之间的距离。居中排布时这段也计入整组宽度。
    TEXT_GAP = 10
    #: 数值字号。居中排布（卡片 body）是卡片的主角，放大一档。
    VALUE_PX = 20
    VALUE_PX_CENTERED = 24
    HOLD_MS = 380
    REPEAT_MS = 90

    def __init__(
        self,
        prop: PropertySpec,
        palette: theme.Palette,
        accent: str,
        *,
        label: str = "",
        centered: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(prop, palette, accent, label=label, parent=parent)
        self._lo, self._hi, self._step = _numeric_range(prop)
        self._value = self._lo
        self._hot = 0  # -1 减、+1 加、0 无
        self._centered = centered
        self.setFixedHeight(self.HEIGHT)
        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.setInterval(self.HOLD_MS)
        self._hold.timeout.connect(self._start_repeat)
        self._repeat = QTimer(self)
        self._repeat.setInterval(self.REPEAT_MS)
        self._repeat.timeout.connect(lambda: self._bump(self._hot))

    # -- 值 --------------------------------------------------------------
    def value(self) -> float:
        return self._value

    def set_value(self, value: object, animate: bool = False) -> None:
        if value is None:
            return
        try:
            v = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        v = min(self._hi, max(self._lo, v))
        if abs(v - self._value) > 1e-6:
            self._value = v
            self.update()

    # -- 栅格 ------------------------------------------------------------
    def _value_px(self) -> int:
        return self.VALUE_PX_CENTERED if self._centered else self.VALUE_PX

    def _text_font(self) -> QFont:
        """量数值宽度用的字体，必须和 ``draw_text`` 实际画的一致（同族同字号）。"""
        f = QFont(self.font())
        f.setPixelSize(self._value_px())
        f.setWeight(QFont.DemiBold)
        return f

    def _layout(self) -> tuple[QRect, QRect, QRect]:
        """返回 (数值区, 减号, 加号) 三个矩形。居中/靠右的差别只在这里。

        靠右（详情页）沿用 ``数值 …… − +``：那一行左侧还要放属性名，
        按钮聚在右端更好对齐全表。居中（卡片 body）是 ``− 数值 +``：
        整组居中，数字两侧各一枚，TEXT_GAP 左右对称。
        """
        y = (self.height() - self.BTN) // 2
        if not self._centered:
            plus = QRect(self.width() - self.BTN, y, self.BTN, self.BTN)
            minus = QRect(plus.x() - self.BTN_GAP - self.BTN, y, self.BTN, self.BTN)
            return QRect(0, 0, max(40, minus.x() - 8), self.height()), minus, plus

        fm = QFontMetrics(self._text_font())
        tw = fm.horizontalAdvance(self._fmt(self._value))
        # 数值矩形比字形宽 2px（给抗锯齿留一点余量），扣掉这 2px 后
        # 两侧间距才在**视觉上**相等：量的是矩形边到边，多出的 1px 会翻倍。
        text_w = tw + 2
        gap = self.TEXT_GAP - 1
        group = self.BTN * 2 + gap * 2 + text_w
        x = max(0, (self.width() - group) // 2)
        minus = QRect(x, y, self.BTN, self.BTN)
        text = QRect(minus.right() + 1 + gap, 0, text_w, self.height())
        plus = QRect(text.right() + 1 + gap, y, self.BTN, self.BTN)
        return text, minus, plus

    def _buttons(self) -> tuple[QRect, QRect]:
        _, minus, plus = self._layout()
        return minus, plus

    # -- 绘制 ------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(0.55 if self._dimmed else 1.0)
        text_rect, minus, plus = self._layout()
        draw_text(
            p,
            text_rect,
            self._fmt(self._value),
            size=self._value_px(),
            color=self._p.text if self.isEnabled() else self._ink(),
            weight=QFont.DemiBold,
            align=(Qt.AlignCenter if self._centered
                   else Qt.AlignLeft | Qt.AlignVCenter),
        )
        for rect, glyph, hot, muted in (
            (minus, "\u2212", self._hot == -1, self._value <= self._lo),
            (plus, "+", self._hot == 1, self._value >= self._hi),
        ):
            p.setPen(Qt.NoPen)
            if hot:
                p.setBrush(QColor(theme.blend(self._p.card, self._accent, 0.30)))
            else:
                p.setBrush(QColor(theme.blend(self._p.card, self._p.text, 0.10)))
            p.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
            draw_text(
                p,
                rect,
                glyph,
                size=15,
                color=self._p.text_mute if muted else self._p.text,
                weight=QFont.Medium,
                align=Qt.AlignCenter,
                elide=False,
            )
        p.end()

    # -- 交互 ------------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton or not self.isEnabled():
            return
        minus, plus = self._buttons()
        pos = event.position().toPoint()
        if minus.adjusted(-6, -6, 6, 6).contains(pos):
            self._hot = -1
        elif plus.adjusted(-6, -6, 6, 6).contains(pos):
            self._hot = 1
        else:
            return
        self._bump(self._hot)
        self._hold.start()
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._hold.stop()
        self._repeat.stop()
        if self._hot:
            self._hot = 0
            self.update()
        event.accept()

    def wheelEvent(self, event) -> None:  # noqa: N802
        if not self.isEnabled():
            return
        self._bump(1 if event.angleDelta().y() > 0 else -1)
        event.accept()

    def _start_repeat(self) -> None:
        if self._hot:
            self._repeat.start()

    def _bump(self, direction: int) -> None:
        target = self._value + direction * self._step
        target = min(self._hi, max(self._lo, target))
        if abs(target - self._value) < 1e-6:
            return
        self._value = target
        self.update()
        self.committed.emit(int(round(target)))


# ---------------------------------------------------------------------------
# 胶囊组：模式、风速
# ---------------------------------------------------------------------------


class CardChips(CardControl):
    """横排胶囊。放不下时先压内边距、再截字，最后才丢弃多余项。

    ``spread=True`` 时把**余宽均分给每一项**，整排铺满控件宽度（分段控件的观感）。
    卡片 body 有 312 宽，五个两字模式只占 230，靠左排会在右边留一条空档；铺满
    之后每个胶囊一样宽，点哪儿都不会点错。
    """

    committed = Signal(int)

    HEIGHT = CHIP_H
    GAP = 6
    PAD = 11

    def __init__(
        self,
        prop: PropertySpec,
        palette: theme.Palette,
        accent: str,
        *,
        label: str = "",
        spread: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(prop, palette, accent, label=label, parent=parent)
        self._items: list[tuple[int, str]] = [
            (o.value, o.display) for o in prop.value_list
        ]
        self._current: int | None = self._items[0][0] if self._items else None
        self._hover = -1
        self._spread = spread
        self.setFixedHeight(self.HEIGHT)
        self.setMouseTracking(True)
        self.setVisible(bool(self._items))

    # -- 值 --------------------------------------------------------------
    def value(self) -> int | None:
        return self._current

    def set_value(self, value: object, animate: bool = False) -> None:
        if value is None:
            return
        try:
            v = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        if v != self._current:
            self._current = v
            self.update()

    # -- 栅格 ------------------------------------------------------------
    def _font(self) -> QFont:
        f = QFont()
        f.setPixelSize(12)
        f.setWeight(QFont.Medium)
        return f

    def _layout(self) -> list[tuple[QRect, int, str]]:
        n = len(self._items)
        if n == 0:
            return []
        fm = QFontMetrics(self._font())
        avail = max(60, self.width())
        pad = self.PAD
        texts = [t for _, t in self._items]
        widths = [fm.horizontalAdvance(t) + pad * 2 for t in texts]
        total = sum(widths) + self.GAP * (n - 1)

        if total > avail:
            # 先挤内边距，通常挤完就够了（5 个两字模式 ≤ 300 宽）
            pad = max(6, pad - (total - avail) // max(1, n * 2))
            widths = [fm.horizontalAdvance(t) + pad * 2 for t in texts]
            total = sum(widths) + self.GAP * (n - 1)
        if total > avail:
            budget = max(26, (avail - self.GAP * (n - 1)) // n)
            widths = [min(w, budget) for w in widths]
            texts = [
                fm.elidedText(t, Qt.ElideRight, max(10, w - pad * 2))
                for t, w in zip(texts, widths)
            ]

        if self._spread and total < avail:
            # 有余宽才铺：紧的时候上面已经压过内边距/截过字，再撑开就又溢出了
            extra = (avail - total) // n
            widths = [w + extra for w in widths]

        out: list[tuple[QRect, int, str]] = []
        x = 0
        for (value, _), w, text in zip(self._items, widths, texts):
            if x + w > avail and out:
                break
            out.append((QRect(x, 0, w, self.height()), value, text))
            x += w + self.GAP
        return out

    # -- 绘制 ------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(0.55 if self._dimmed else 1.0)
        radius = self.height() / 2
        for i, (rect, value, text) in enumerate(self._layout()):
            on = value == self._current
            if on:
                p.setBrush(QColor(theme.blend(self._p.card, self._accent, 0.24)))
            elif i == self._hover:
                p.setBrush(QColor(theme.blend(self._p.card, self._p.text, 0.12)))
            else:
                p.setBrush(QColor(theme.blend(self._p.card, self._p.text, 0.07)))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(rect, radius, radius)
            draw_text(
                p,
                rect,
                text,
                size=12,
                color=self._accent if on else self._p.text_soft,
                weight=QFont.Medium if on else QFont.Normal,
                align=Qt.AlignCenter,
                elide=False,
            )
        p.end()

    # -- 交互 ------------------------------------------------------------
    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        hit = self._hit(event.position().toPoint())
        if hit != self._hover:
            self._hover = hit
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover != -1:
            self._hover = -1
            self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton or not self.isEnabled():
            return
        idx = self._hit(event.position().toPoint())
        if idx < 0:
            return
        _, value, _ = self._layout()[idx]
        if value != self._current:
            self._current = value
            self.update()
        self.committed.emit(value)
        event.accept()

    def _hit(self, point) -> int:
        for i, (rect, _, _) in enumerate(self._layout()):
            if rect.contains(point):
                return i
        return -1


# ---------------------------------------------------------------------------
# 开关行：摇头、扫风、离子
# ---------------------------------------------------------------------------


class CardToggle(CardControl):
    """左侧属性名 + 右侧 44×24 开关。"""

    committed = Signal(bool)

    HEIGHT = ROW_H

    def __init__(
        self,
        prop: PropertySpec,
        palette: theme.Palette,
        accent: str,
        *,
        label: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(prop, palette, accent, label=label, parent=parent)
        self._value = False
        self.setFixedHeight(self.HEIGHT)
        self.switch = ToggleSwitch(
            False, palette=palette, parent=self, size="sm", accent=accent
        )
        self.switch.toggled.connect(self._on_toggle)

    # -- 值 --------------------------------------------------------------
    def value(self) -> bool:
        return self._value

    def set_value(self, value: object, animate: bool = False) -> None:
        if value is None:
            return
        on = bool(value)
        if on != self._value:
            self._value = on
            self.switch.blockSignals(True)
            self.switch.set_on(on, animate=False)
            self.switch.blockSignals(False)
            self.update()

    def apply_theme(self, palette: theme.Palette, accent: str) -> None:
        super().apply_theme(palette, accent)
        self.switch._p = palette  # noqa: SLF001 — 同包内的轻量主题同步
        self.switch._accent = accent  # noqa: SLF001
        self.switch.update()

    # -- 绘制 ------------------------------------------------------------
    def resizeEvent(self, event) -> None:  # noqa: N802
        self.switch.move(
            self.width() - self.switch.width(),
            (self.height() - self.switch.height()) // 2,
        )

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(0.55 if self._dimmed else 1.0)
        w = max(40, self.width() - self.switch.width() - 8)
        draw_text(
            p,
            QRect(0, 0, w, self.height()),
            self._label,
            size=12,
            color=self._ink() if self.isEnabled() else self._p.text_mute,
        )
        p.end()

    def _on_toggle(self, on: bool) -> None:
        self._value = on
        self.update()
        self.committed.emit(on)


# ---------------------------------------------------------------------------
# 只读读数：有人/无人、门开/关
# ---------------------------------------------------------------------------


class CardReadout(CardControl):
    """只读大字读数。占满整块内容带，居中排「大字值 + 小字属性名」。

    和滑条共用 ``set_value`` / ``setEnabled`` 接口，``CardBody.sync`` 不必为它
    开分支；区别只在于它**没有** ``committed``——它不写设备。

    不按值染色。``occupancy`` 这种属性 1=有人、0=无人，看着可以直接"非零就
    高亮"，但同一套映射套到门锁的 ``lock-state`` 上就反了。状态语义各品类
    不同，这里只负责把云端已经翻好的中文（``format_value``）放大，颜色交给
    外壳的品类色。
    """

    HEIGHT = 78

    def __init__(
        self,
        prop: PropertySpec,
        palette: theme.Palette,
        accent: str,
        *,
        label: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(prop, palette, accent, label=label, parent=parent)
        self._text = "—"
        self.setFixedHeight(self.HEIGHT)
        self.setCursor(Qt.ArrowCursor)  # 不可点，别给手型

    def set_value(self, value: object, animate: bool = False) -> None:
        if value is None:
            return
        text = format_value(self.prop, value)
        if text != self._text:
            self._text = text
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # 只读控件没有"不可写"这回事，唯一的灰态是设备离线
        p.setOpacity(1.0 if self.isEnabled() else 0.45)
        w = self.width()
        # 26px→30px：内容带 78 高、卡宽 138，两字状态用 30px 才压得住这块空白；
        # 四字（"有人移动"）也才 120px，仍在一行内。
        draw_text(
            p,
            QRect(0, 6, w, 40),
            self._text,
            size=30,
            color=self._p.text,
            weight=QFont.DemiBold,
            align=Qt.AlignCenter,
            elide=False,
        )
        draw_text(
            p,
            QRect(0, 48, w, 16),
            self._label,
            size=11,
            color=self._p.text_soft,
            align=Qt.AlignCenter,
            elide=False,
        )
        p.end()


__all__ = [
    "CHIP_H",
    "CardChips",
    "CardControl",
    "CardReadout",
    "CardSlider",
    "CardStepper",
    "CardToggle",
    "MIN_HIT_H",
    "ROW_H",
    "kelvin_color",
]
