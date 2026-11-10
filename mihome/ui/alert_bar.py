# SPDX-License-Identifier: GPL-3.0-or-later
"""顶部警告条：一条"不自己消失、必须手动关"的整宽横幅。

样式照着产品给的四条参考横幅做：圆角 + 1px 状态色描边 + 状态色低浓度染底，
左边一个状态图标，然后是加粗着色的标题、一个 ``·``、正常字色的正文，右边一个
小号 ×。标题与正文同字号，只靠颜色和字重分层——参考图就是这么分的，正文再降一档
字号在 48px 高度里会显得头重脚轻。

几个刻意的选择
--------------
1. **整条自己画**：正文里有"标题 + 分隔符 + 正文"三段不同颜色/字重的文字，还要
   在窄窗口下按剩余宽度省略。用 QLabel 拼会得到三个各自为政的尺寸策略；直接
   QPainter 画，位置和省略都在一处算清（``StatusChip`` 也是这么做的）。
2. **没有定时器**：这条横幅的存在意义是"不许自己消失"，任何自动收起都是 bug。
   收起只有两个来源：用户点 ×，或界面层明确调 ``hide()``。
3. **× 的命中区比图形大**：24×24，鼠标移进去换成手型并浮出一个淡色圆底，
   否则 48px 高的一条里那个小叉很难点准。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..core.alerts import ERROR, SUCCESS, WARNING
from . import theme
from .widgets import draw_text

#: 状态 → 强调色。绿色沿用"开关打开"那一支，避免再引入一个绿
_ACCENT = {
    ERROR: "#E5484D",
    WARNING: "#E8A33D",
    SUCCESS: theme.TOGGLE_ON,
}


class AlertBar(QWidget):
    """一条横幅。显示内容由 ``set_alert`` 决定，高度固定，宽度跟着窗口走。"""

    dismissed = Signal()

    H = 48
    R = 10
    PAD_L = 18
    PAD_R = 8
    ICON = 18
    GAP = 12
    CLOSE = 24
    SIZE = 13

    def __init__(self, palette: theme.Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._p = palette
        self._level = WARNING
        self._title = ""
        self._detail = ""
        self._hover_close = False
        self._close_rect = QRect()
        self.setFixedHeight(self.H)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)

    # -- 对外 ------------------------------------------------------------
    def set_alert(self, level: str, title: str, detail: str) -> None:
        self._level = level
        self._title = title
        self._detail = detail
        self.update()

    def alert(self) -> tuple[str, str, str]:
        """给验证脚本查当前内容用。"""
        return self._level, self._title, self._detail

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.update()

    # -- 绘制 ------------------------------------------------------------
    def _font(self, weight: QFont.Weight = QFont.Normal) -> QFont:
        f = QFont(self.font())
        f.setPixelSize(self.SIZE)
        f.setWeight(weight)
        return f

    def _colors(self) -> tuple[str, str, str]:
        """(强调色, 底色, 描边色)。底色是把强调色按极低比例混进卡片色。"""
        accent = _ACCENT.get(self._level, _ACCENT[WARNING])
        base = self._p.card if not self._p.is_dark else self._p.bg_soft
        strength = 0.16 if self._p.is_dark else 0.09
        return (
            accent,
            theme.blend(base, accent, strength),
            theme.blend(base, accent, 0.52 if self._p.is_dark else 0.38),
        )

    def paintEvent(self, event) -> None:  # noqa: N802
        accent, bg, border = self._colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        body = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(border), 1))
        p.setBrush(QColor(bg))
        p.drawRoundedRect(body, self.R, self.R)

        cy = self.H / 2
        self._paint_icon(p, QPointF(self.PAD_L + self.ICON / 2, cy), accent)

        x = self.PAD_L + self.ICON + self.GAP
        right = self.width() - self.PAD_R - self.CLOSE
        title_font = self._font(QFont.DemiBold)
        fm = QFontMetrics(title_font)
        w_title = min(fm.horizontalAdvance(self._title), max(0, right - x))
        draw_text(
            p,
            QRect(int(x), 0, int(w_title) + 4, self.H),
            self._title,
            size=self.SIZE,
            color=accent,
            weight=QFont.DemiBold,
        )
        x += w_title

        sep_font = self._font()
        fm_sep = QFontMetrics(sep_font)
        sep = " · "
        draw_text(
            p,
            QRect(int(x), 0, int(fm_sep.horizontalAdvance(sep)) + 2, self.H),
            sep,
            size=self.SIZE,
            color=self._p.text_mute,
            elide=False,
        )
        x += fm_sep.horizontalAdvance(sep)
        if right > x:
            draw_text(
                p,
                QRect(int(x), 0, int(right - x), self.H),
                self._detail,
                size=self.SIZE,
                color=self._p.text,
            )

        self._paint_close(p)
        p.end()

    def _paint_icon(self, p: QPainter, c: QPointF, color: str) -> None:
        pen = QPen(QColor(color), 1.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        r = self.ICON / 2
        if self._level == SUCCESS:
            path = QPainterPath()
            path.moveTo(c.x() - r * 0.72, c.y() + r * 0.02)
            path.lineTo(c.x() - r * 0.20, c.y() + r * 0.55)
            path.lineTo(c.x() + r * 0.75, c.y() - r * 0.55)
            p.drawPath(path)
        elif self._level == ERROR:
            d = r * 0.62
            p.drawLine(QPointF(c.x() - d, c.y() - d), QPointF(c.x() + d, c.y() + d))
            p.drawLine(QPointF(c.x() + d, c.y() - d), QPointF(c.x() - d, c.y() + d))
        else:
            # 三角 + 感叹号：和参考图的"体验环境"那条一致
            path = QPainterPath()
            path.moveTo(c.x(), c.y() - r * 0.92)
            path.lineTo(c.x() + r * 0.98, c.y() + r * 0.72)
            path.lineTo(c.x() - r * 0.98, c.y() + r * 0.72)
            path.closeSubpath()
            p.drawPath(path)
            p.setBrush(QColor(color))
            p.setPen(Qt.NoPen)
            p.drawRect(QRectF(c.x() - 0.85, c.y() - r * 0.30, 1.7, r * 0.62))
            p.drawEllipse(QPointF(c.x(), c.y() + r * 0.52), 0.95, 0.95)

    def _paint_close(self, p: QPainter) -> None:
        size = self.CLOSE
        rect = QRect(self.width() - self.PAD_R - size, (self.H - size) // 2, size, size)
        self._close_rect = rect
        if self._hover_close:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.rgba(self._p.text, 0.10)))
            p.drawEllipse(QRectF(rect))
        color = self._p.text if self._hover_close else self._p.text_mute
        pen = QPen(QColor(color), 1.5)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        c = QPointF(rect.center()) + QPointF(0.5, 0.5)
        d = 3.6
        p.drawLine(QPointF(c.x() - d, c.y() - d), QPointF(c.x() + d, c.y() + d))
        p.drawLine(QPointF(c.x() + d, c.y() - d), QPointF(c.x() - d, c.y() + d))

    # -- 交互 ------------------------------------------------------------
    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        inside = self._close_rect.contains(event.position().toPoint())
        if inside != self._hover_close:
            self._hover_close = inside
            self.setCursor(Qt.PointingHandCursor if inside else Qt.ArrowCursor)
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover_close:
            self._hover_close = False
            self.setCursor(Qt.ArrowCursor)
            self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self._close_rect.contains(
            event.position().toPoint()
        ):
            self.dismissed.emit()
            return
        super().mouseReleaseEvent(event)
