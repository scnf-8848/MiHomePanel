"""通用控件与布局工具。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QLabel, QLayout, QSizePolicy, QWidget

from ..core.icons import IconCache
from . import theme, tooltip


class FlowLayout(QLayout):
    """自动换行布局：宽度变化时重排列数，卡片墙靠它自适应窗口大小。"""

    def __init__(self, parent: QWidget | None = None, margin: int = 0, h_gap: int = 12, v_gap: int = 12) -> None:
        super().__init__(parent)
        self._items: list = []
        self._h_gap = h_gap
        self._v_gap = v_gap
        self.setContentsMargins(margin, margin, margin, margin)

    # -- QLayout 接口 ----------------------------------------------------
    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._layout(rect, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    # -- 排布 ------------------------------------------------------------
    def _layout(self, rect: QRect, apply: bool) -> int:
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = area.x(), area.y(), 0

        for item in self._items:
            # 搜索过滤时卡片是隐藏的，布局必须跳过它们，否则会留出空洞
            w = item.widget()
            if w is not None and w.isHidden():
                continue
            hint = item.sizeHint()
            if x + hint.width() > area.right() + 1 and line_h > 0:
                x = area.x()
                y += line_h + self._v_gap
                line_h = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._h_gap
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()


class IconProvider(QWidget):
    """把一个 model 的产品图变成 QPixmap，带内存缓存与异步回填。

    继承 QWidget 只是为了拿到对象生命周期与信号机制，不参与布局。
    """

    ready = Signal(str)

    def __init__(self, cache: IconCache, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_DontShowOnScreen, True)
        self.setFixedSize(1, 1)
        self._cache = cache
        self._pix: dict[str, QPixmap | None] = {}
        self._scaled: dict[tuple[str, int], QPixmap] = {}
        self._pending: set[str] = set()

    def ensure(self, model: str) -> QPixmap | None:
        if not model:
            return None
        if model in self._pix:
            return self._pix[model]
        path = self._cache.path_for(model)
        if path is not None:
            pm = QPixmap(str(path))
            self._pix[model] = pm if not pm.isNull() else None
            return self._pix[model]
        if model not in self._pending:
            self._pending.add(model)
            self._cache.request(model, self._loaded)
        return None

    def _loaded(self, model: str, path: Path | None) -> None:
        # 回调发生在主线程（由 worker.drain 执行），可以直接改状态
        self._pending.discard(model)
        if path is None:
            self._pix[model] = None
            return
        pm = QPixmap(str(path))
        self._pix[model] = pm if not pm.isNull() else None
        self._scaled = {k: v for k, v in self._scaled.items() if k[0] != model}
        if self._pix[model] is not None:
            QTimer.singleShot(0, lambda m=model: self.ready.emit(m))

    def scaled(self, model: str, size: int) -> QPixmap | None:
        """等比缩放到 size 见方（不拉伸）。结果按 (model,size) 缓存，轮询重绘不重复缩放。"""
        key = (model, size)
        hit = self._scaled.get(key)
        if hit is not None:
            return hit
        pm = self.ensure(model)
        if pm is None:
            return None
        out = pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._scaled[key] = out
        return out

    def has(self, model: str) -> bool:
        return self._pix.get(model) is not None


# ---------------------------------------------------------------------------
# 绘制小工具
# ---------------------------------------------------------------------------


def rounded_path(rect: QRect, radius: int) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


def draw_text(
    painter: QPainter,
    rect: QRect,
    text: str,
    *,
    size: int,
    color: str,
    weight: QFont.Weight = QFont.Normal,
    align: Qt.AlignmentFlag = Qt.AlignLeft | Qt.AlignVCenter,
    elide: bool = True,
) -> None:
    if not text:
        return
    font = QFont(painter.font())
    font.setPixelSize(size)
    font.setWeight(weight)
    painter.setFont(font)
    painter.setPen(QColor(color))
    if elide:
        fm = QFontMetrics(font)
        text = fm.elidedText(text, Qt.ElideRight, rect.width())
    painter.drawText(rect, int(align), text)


def draw_text_block(
    painter: QPainter,
    rect: QRect,
    text: str,
    *,
    size: int,
    color: str,
    weight: QFont.Weight = QFont.Normal,
    max_lines: int = 2,
    line_gap: int = 3,
) -> None:
    """多行文本，超出行数时末行省略。

    设备名常常是英文默认名（"Xiaomi Smart Wall Switch (3 Gang)"），单行必然被
    截成 "Xiaomi Sm…" 毫无信息量。给两行能把大部分型号名放完。
    """
    if not text:
        return
    font = QFont(painter.font())
    font.setPixelSize(size)
    font.setWeight(weight)
    painter.setFont(font)
    painter.setPen(QColor(color))
    fm = QFontMetrics(font)

    line_h = fm.height() + line_gap
    lines: list[str] = []
    rest = text
    for i in range(max_lines):
        if not rest:
            break
        if i == max_lines - 1:
            lines.append(fm.elidedText(rest, Qt.ElideRight, rect.width()))
            break
        # 找能放下的最长前缀
        cut = len(rest)
        while cut > 1 and fm.horizontalAdvance(rest[:cut]) > rect.width():
            cut -= 1
        if cut < len(rest):
            # 英文不能在单词中间断（"Purif / ying" 这种最难读）。
            # 回退到最近的空格，但不能退掉半行以上，否则不如硬断。
            sp = rest.rfind(" ", 1, cut)
            if sp > 0 and cut - sp <= 12:
                cut = sp
        lines.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()

    y = rect.y()
    for line in lines:
        painter.drawText(QRect(rect.x(), y, rect.width(), fm.height()),
                         int(Qt.AlignLeft | Qt.AlignVCenter), line)
        y += line_h


class ElidedLabel(QLabel):
    """单行、宽度不够时打省略号的标签。

    ``QLabel`` 的 minimumSizeHint 是整段文字的宽度，属性名一长就会把整行撑宽，
    详情页甚至会被撑出横向滚动条。这里把宽度让出来，按当前宽度裁文字；完整内容
    放在 tooltip 里。
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        tooltip.watch(self, lambda: self._full)
        super().setText(text)

    def set_full(self, text: str) -> None:
        self._full = text
        self._apply()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply()

    def _apply(self) -> None:
        width = self.width() - 2
        if width <= 0:
            return
        text = self.fontMetrics().elidedText(self._full, Qt.ElideRight, width)
        if text != super().text():
            super().setText(text)


class StatusChip(QWidget):
    """状态胶囊（米家首页那排"28.3-29.5°""灯亮 5"）。

    米家那支深蓝黑是给"压在照片壁纸上"用的；我们顶栏是纯色底，照搬会出问题：
    深色主题下深色胶囊和背景几乎同色，切主题看不出变化。所以这里把它做成一张
    **缩小的卡片**——底色取 ``palette.card``、文字取次级色，深浅两套各一档。
    """

    H = 30
    PAD = 14
    DOT = 6

    def __init__(self, text: str, palette: theme.Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._p = palette
        self._text = text
        self._dot = True
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setFixedHeight(self.H)
        self._apply_width()

    def setText(self, text: str) -> None:  # noqa: N802
        if text == self._text:
            return
        self._text = text
        self._apply_width()
        self.update()

    def text(self) -> str:
        return self._text

    def set_dot(self, on: bool) -> None:
        if on != self._dot:
            self._dot = on
            self._apply_width()
            self.update()

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.update()

    def _font(self) -> QFont:
        f = QFont(self.font())
        f.setPixelSize(12)
        return f

    def _apply_width(self) -> None:
        fm = QFontMetrics(self._font())
        w = fm.horizontalAdvance(self._text) + self.PAD * 2
        if self._dot:
            w += self.DOT + 7
        self.setFixedWidth(max(56, w))

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.chip_bg(self._p)))
        p.drawRoundedRect(r, theme.RADIUS_PILL, theme.RADIUS_PILL)

        x = self.PAD
        if self._dot:
            p.setBrush(QColor(theme.TOGGLE_ON))
            p.drawEllipse(QPointF(x + self.DOT / 2, self.H / 2), self.DOT / 2, self.DOT / 2)
            x += self.DOT + 7
        draw_text(p, QRect(int(x), 0, self.width() - int(x) - 4, self.H), self._text,
                  size=12, color=theme.chip_text(self._p))
        p.end()


class HLine(QWidget):
    """1px 分隔线，用背景色画而不是 QFrame，避免 Qt 的默认长度策略。"""

    def __init__(self, color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = color
        self.setFixedHeight(1)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(self._color))
        p.end()


class ToggleSwitch(QWidget):
    """米家风格的开关：胶囊轨道 + 白色滑块。

    自绘而不是 ``QCheckBox`` + QSS：QSS 画不出"滑块从一边滑到另一边"的中间态，
    也没法单独给滑块加阴影。绿色只在"开"的时候出现（见 ``theme.TOGGLE_ON``），
    关的时候是一条中性灰轨道，这样一列开关扫一眼就知道谁开着。

    两种尺寸（米家实测）：``sm`` 44×24 用于设置行，``md`` 49×32 用于列表/卡片。
    """

    toggled = Signal(bool)

    #: size → (宽, 高)
    SIZES: dict[str, tuple[int, int]] = {"sm": (44, 24), "md": (49, 32)}
    W = 44
    H = 24

    def __init__(
        self,
        on: bool = False,
        palette: theme.Palette | None = None,
        parent: QWidget | None = None,
        size: str = "sm",
        accent: str = "",
    ) -> None:
        super().__init__(parent)
        self._p = palette or theme.LIGHT
        self._on = bool(on)
        self._t = 1.0 if on else 0.0
        self._hover = False
        #: 设备详情页用设备的品类色（灯是琥珀、插座是蓝），列表里用默认薄荷绿
        self._accent = accent or theme.TOGGLE_ON
        self._w, self._h = self.SIZES.get(size, self.SIZES["sm"])
        self.setFixedSize(self._w, self._h)
        self.setCursor(Qt.PointingHandCursor)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._tick)

    # -- 状态 ------------------------------------------------------------
    def is_on(self) -> bool:
        return self._on

    def set_on(self, on: bool, animate: bool = True) -> None:
        on = bool(on)
        if on == self._on:
            return
        self._on = on
        target = 1.0 if on else 0.0
        self._anim.stop()
        if animate and self.isVisible():
            self._anim.setStartValue(self._t)
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self._t = target
            self.update()

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.update()

    def _tick(self, value: object) -> None:
        self._t = float(value)
        self.update()

    # -- 绘制 ------------------------------------------------------------
    def _track_off(self) -> str:
        return theme.blend(self._p.bg, self._p.text, 0.16)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0, 0, -1, -1)
        radius = r.height() / 2
        t = self._t
        enabled = self.isEnabled()

        track = QColor(theme.blend(self._track_off(), self._accent, t))
        if self._hover and enabled:
            track = QColor(theme.blend(track.name(), self._p.overlay, 0.10))
        if not enabled:
            track.setAlphaF(track.alphaF() * 0.55)
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(r, radius, radius)

        margin = 3.0 if r.height() < 30 else 4.0
        d = r.height() - margin * 2
        x = r.left() + margin + (r.width() - d - margin * 2) * t
        knob = QRectF(x, r.top() + margin, d, d)
        # 一层极淡的投影，让滑块从轨道上"浮"起来一点点
        p.setBrush(QColor(0, 0, 0, 34))
        p.drawEllipse(knob.translated(0, 0.8))
        p.setBrush(QColor("#FFFFFF" if enabled else "#E6E8EB"))
        p.drawEllipse(knob)
        p.end()

    # -- 交互 ------------------------------------------------------------
    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton or not self.rect().contains(
            event.position().toPoint()
        ):
            return
        self.set_on(not self._on)
        self.toggled.emit(self._on)


__all__ = [
    "ElidedLabel",
    "FlowLayout",
    "HLine",
    "IconProvider",
    "StatusChip",
    "ToggleSwitch",
    "draw_text",
    "draw_text_block",
    "rounded_path",
    "theme",
]
