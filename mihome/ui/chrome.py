# SPDX-License-Identifier: GPL-3.0-or-later
"""自绘窗口外壳：标题栏 + 无边框窗口的边缘缩放。

为什么不用系统标题栏：Windows 原生标题栏是一块纯白或纯灰的方块，和这套"暖灰底 +
大圆角卡片"的界面拼在一起像两个程序。这里用 38px 的自绘条换掉它，颜色跟着主题走，
右上角三个按钮的交互和系统保持一致（悬停变色、关闭悬停变红）。

去掉系统边框之后，有两个系统行为必须自己补回来：

* **拖动 / 双击最大化** —— 按下标题栏交给 ``startSystemMove()``，让系统去处理贴边
  分屏；双击自己切最大化。
* **边缘缩放** —— 在四边四角铺 8 个透明热区，按下时调 ``startSystemResize()``；
  平台不支持时退回手动改 geometry。

最大化不调 ``showMaximized()``：无边框窗口在 Windows 上被当成 WS_POPUP，最大化会连
任务栏一起盖住。改成手动铺满 ``availableGeometry()``，并且记住还原尺寸。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QRegion,
)
from PySide6.QtWidgets import QApplication, QDialog, QSizePolicy, QWidget

from . import theme
from .widgets import draw_text

BTN_W = 46
BAR_H = 38
EDGE = 5
CORNER = 12
#: 窗口圆角。
WINDOW_RADIUS = theme.RADIUS_WINDOW


def rounded_path(rect: QRectF, radius: float, corners: str = "tl tr br bl") -> QPainterPath:
    """圆角矩形路径，可以只圆其中几个角（``"tl"``/``"tr"``/``"br"``/``"bl"``）。

    窗口外框必须**抗锯齿**地画出来：``setMask`` 是 1 bit 的，圆角在 21 物理像素
    （12dp @1.75）上只能近似成台阶；卡片圆角用的是 ``drawRoundedRect``（抗锯齿），
    两者放在一起，窗口的角明显更"毛"。所以这里返回路径，由调用方 ``fillPath``。
    """
    x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    r = max(0.0, min(radius, w / 2, h / 2))
    tl, tr = "tl" in corners, "tr" in corners
    br, bl = "br" in corners, "bl" in corners

    path = QPainterPath()
    path.moveTo(x + (r if tl else 0), y)
    path.lineTo(x + w - (r if tr else 0), y)
    if tr:
        path.quadTo(x + w, y, x + w, y + r)
    path.lineTo(x + w, y + h - (r if br else 0))
    if br:
        path.quadTo(x + w, y + h, x + w - r, y + h)
    path.lineTo(x + (r if bl else 0), y + h)
    if bl:
        path.quadTo(x, y + h, x, y + h - r)
    path.lineTo(x, y + (r if tl else 0))
    if tl:
        path.quadTo(x, y, x + r, y)
    path.closeSubpath()
    return path


def bar_bg(p: theme.Palette) -> str:
    """主窗口标题栏底色：比窗口底色亮/暗一点点，但不用卡片色（会显得太凸）。"""
    return theme.blend(p.bg, p.card, 0.62)


# 子窗口（详情 / 设置 / 登录）跟主窗口原本是同底色 + 同标题栏，叠在一起就是两块
# 一样的灰糊成一片，看不出谁在前。这里把子窗口整体挪开一档：
#   surface —— 正文底色，浅色往深挪、深色往浅挪，方向都朝着"浮起来"；
#   bar     —— 标题栏再挪一档，形成一条明显的 header 带；
#   border  —— 一圈描边，无边框窗口没有系统边框，这一圈就是"这是另一个窗口"的信号。
def surface_bg(p: theme.Palette) -> str:
    # 公式在 theme 里（``theme.surface_bg``）——分类栏 rail_bg 要相对它算，
    # 两处各写一遍的话，以后改一档明度必然会漏掉一处。
    return theme.surface_bg(p)


def dialog_bar_bg(p: theme.Palette) -> str:
    return theme.blend(surface_bg(p), "#FFFFFF" if p.is_dark else "#000000", 0.05)


def surface_border(p: theme.Palette) -> str:
    # 深色下 1px 的中灰几乎不可见，比例要放大一点才看得出"这里有一圈框"
    return theme.blend(surface_bg(p), p.text, 0.15 if p.is_dark else 0.16)


class _GlyphButton:
    """标题栏按钮的命中矩形，纯粹是数据。"""

    def __init__(self, name: str, rect: QRect) -> None:
        self.name = name
        self.rect = rect


class TitleBar(QWidget):
    """自绘标题栏。``frame`` 用来做最大化/还原，对话框传 None 只保留关闭。"""

    def __init__(
        self,
        title: str,
        palette: theme.Palette,
        *,
        subtitle: str = "",
        buttons: tuple[str, ...] = ("min", "max", "close"),
        frame: WindowFrame | None = None,
        height: int = BAR_H,
        variant: str = "window",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._p = palette
        self._title = title
        self._sub = subtitle
        self._buttons = tuple(buttons)
        self._frame = frame
        self._variant = variant
        self._hover = -1
        self._down = -1
        self._drag_origin: QPoint | None = None
        self.setFixedHeight(height)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setObjectName("TitleBar")

    # -- 外部接口 --------------------------------------------------------
    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.update()

    def set_title(self, title: str) -> None:
        if title != self._title:
            self._title = title
            self.update()

    def set_subtitle(self, subtitle: str) -> None:
        if subtitle != self._sub:
            self._sub = subtitle
            self.update()

    # -- 绘制 ------------------------------------------------------------
    def _button_list(self) -> list[_GlyphButton]:
        out: list[_GlyphButton] = []
        x = self.width()
        for name in reversed(self._buttons):
            x -= BTN_W
            out.append(_GlyphButton(name, QRect(x, 0, BTN_W, self.height())))
        return out

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self._p
        is_dialog = self._variant == "dialog"
        # 顶部两个角必须自己圆：窗口背景是抗锯齿画的，标题栏这里如果整块铺色，
        # 方角会把窗口的圆弧盖掉（关闭按钮悬停变红时最明显）。用抗锯齿裁剪，
        # 悬停色块也就跟着被裁圆了。
        p.setClipPath(rounded_path(QRectF(self.rect()), WINDOW_RADIUS, "tl tr"))
        p.fillRect(self.rect(), QColor(dialog_bar_bg(pal) if is_dialog else bar_bg(pal)))

        if is_dialog and self.window().isActiveWindow():
            # 焦点在哪一层：左侧一条主色窄边，失焦自动消失
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(pal.accent))
            p.drawRect(QRect(0, 0, 3, self.height()))

        logo = QRect(16, (self.height() - 20) // 2, 20, 20)
        self._paint_logo(p, logo, pal.accent)

        text_x = logo.right() + 10
        avail = self.width() - text_x - BTN_W * len(self._buttons) - 16
        # 副标题跟在标题后面（不是右对齐）：它是对标题的补充，不是窗口状态
        title_font = QFont()
        title_font.setPixelSize(13)
        title_font.setWeight(QFont.DemiBold)
        title_w = min(QFontMetrics(title_font).horizontalAdvance(self._title), avail)
        draw_text(p, QRect(text_x, 0, title_w, self.height()), self._title,
                  size=13, color=pal.text, weight=QFont.DemiBold, elide=False)
        if self._sub:
            sub_x = text_x + title_w + 10
            draw_text(p, QRect(sub_x, 0, max(0, avail - (sub_x - text_x)), self.height()),
                      self._sub, size=12, color=pal.text_mute)

        for i, btn in enumerate(self._button_list()):
            self._paint_button(p, btn, pal, self._hover == i, self._down == i)

        # 一条极淡的下边线，把标题栏和内容分开
        p.setPen(QPen(QColor(pal.border), 1))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        p.end()

    @staticmethod
    def _paint_logo(p: QPainter, rect: QRect, accent: str) -> None:
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(accent))
        p.drawRoundedRect(rect, 6, 6)
        # 一个极简的"房子"：屋顶三角 + 屋身
        cx = rect.center().x()
        roof = QPolygonF([
            QPointF(cx, rect.top() + 4.0),
            QPointF(rect.right() - 4.0, rect.top() + 9.0),
            QPointF(rect.left() + 4.0, rect.top() + 9.0),
        ])
        p.setBrush(QColor("#FFFFFF"))
        p.drawPolygon(roof)
        p.drawRoundedRect(
            QRect(rect.left() + 6, rect.top() + 9, rect.width() - 12, rect.height() - 13), 1.5, 1.5
        )

    def _paint_button(self, p: QPainter, btn: _GlyphButton, pal: theme.Palette,
                      hover: bool, down: bool) -> None:
        name = btn.name
        if hover:
            color = "#E5484D" if name == "close" else theme.blend(bar_bg(pal), pal.text, 0.09)
            if down:
                color = theme.blend(color, "#000000", 0.10)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(color))
            p.drawRect(btn.rect)

        glyph = "#FFFFFF" if (hover and name == "close") else pal.text_soft
        p.setPen(QPen(QColor(glyph), 1.2, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)
        c = btn.rect.center()
        s = 5
        if name == "min":
            p.drawLine(c.x() - s, c.y() + s, c.x() + s, c.y() + s)
        elif name == "max":
            if self._frame is not None and self._frame.is_maximized:
                p.drawRect(QRect(c.x() - s + 1, c.y() - s + 1, s * 2 - 2, s * 2 - 2))
                p.drawRect(QRect(c.x() - s, c.y() - s, s * 2 - 3, s * 2 - 3))
            else:
                p.drawRect(QRect(c.x() - s, c.y() - s, s * 2, s * 2))
        elif name == "close":
            p.drawLine(c.x() - s, c.y() - s, c.x() + s, c.y() + s)
            p.drawLine(c.x() + s, c.y() - s, c.x() - s, c.y() + s)

    # -- 交互 ------------------------------------------------------------
    def _hit(self, pos: QPoint) -> int:
        for i, btn in enumerate(self._button_list()):
            if btn.rect.contains(pos):
                return i
        return -1

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_origin is not None and (event.buttons() & Qt.LeftButton):
            self.window().move(QCursor.pos() - self._drag_origin)
            return
        idx = self._hit(event.position().toPoint())
        if idx != self._hover:
            self._hover = idx
            self.setCursor(Qt.ArrowCursor)
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover != -1:
            self._hover = -1
            self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            return
        idx = self._hit(event.position().toPoint())
        if idx >= 0:
            self._down = idx
            self.update()
            return
        # 空白处 = 拖动窗口。最大化状态下先还原，让鼠标停在原来的相对位置
        win = self.window()
        if self._frame is not None and self._frame.is_maximized:
            self._frame.restore_under_cursor(QCursor.pos())
        handle = win.windowHandle()
        if handle is not None and handle.startSystemMove():
            return
        self._drag_origin = QCursor.pos() - win.frameGeometry().topLeft()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_origin = None
        if event.button() != Qt.LeftButton:
            return
        idx = self._hit(event.position().toPoint())
        pressed, self._down = self._down, -1
        self.update()
        if pressed >= 0 and pressed == idx:
            self._activate(self._button_list()[idx].name)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton or self._hit(event.position().toPoint()) >= 0:
            return
        if self._frame is not None:
            self._frame.toggle_maximize()

    def _activate(self, name: str) -> None:
        win = self.window()
        if name == "min":
            win.showMinimized()
        elif name == "max":
            if self._frame is not None:
                self._frame.toggle_maximize()
            else:
                win.showNormal() if win.isMaximized() else win.showMaximized()
        elif name == "close":
            win.close()


class _ResizeEdge(QWidget):
    """窗口边缘的透明热区。系统支持就走系统缩放，否则手动改 geometry。"""

    def __init__(self, parent: QWidget, edges: Qt.Edges, cursor: Qt.CursorShape) -> None:
        super().__init__(parent)
        self._edges = edges
        self._manual = False
        self._press = QPoint()
        self._geo = QRect()
        self.setCursor(cursor)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            return
        win = self.window()
        handle = win.windowHandle()
        if handle is not None and handle.startSystemResize(self._edges):
            self._manual = False
            return
        self._manual = True
        self._press = event.globalPosition().toPoint()
        self._geo = win.geometry()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self._manual:
            return
        delta = event.globalPosition().toPoint() - self._press
        geo = QRect(self._geo)
        e = self._edges
        if e & Qt.LeftEdge:
            geo.setLeft(min(geo.left() + delta.x(), geo.right() - 640))
        if e & Qt.RightEdge:
            geo.setRight(max(geo.right() + delta.x(), geo.left() + 640))
        if e & Qt.TopEdge:
            geo.setTop(min(geo.top() + delta.y(), geo.bottom() - 420))
        if e & Qt.BottomEdge:
            geo.setBottom(max(geo.bottom() + delta.y(), geo.top() + 420))
        self.window().setGeometry(geo)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._manual = False


class _FrameBorder(QWidget):
    """最上面一层 1px 描边。

    子控件（标题栏、列表）会把窗口边缘盖住，父窗口先画、子控件后画，所以描边不能
    跟填充一起画——单独放一个不吃鼠标的透明覆盖层，永远在最上面。
    """

    def __init__(self, parent: QWidget, frame: WindowFrame) -> None:
        super().__init__(parent)
        self._frame = frame
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

    def paintEvent(self, event) -> None:  # noqa: N802
        f = self._frame
        if f.is_maximized or not f.border_color:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # 0.5 内缩：1px 的描边正好压在第一像素列上，不会糊成 2px
        path = rounded_path(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
                            f.radius)
        p.setPen(QPen(QColor(f.border_color()), 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.end()


class WindowFrame(QObject):
    """给任意顶层窗口装上"无边框 + 圆角 + 边缘缩放 + 手动最大化"。

    圆角用 ``WA_TranslucentBackground`` + 抗锯齿路径画，**不再用 setMask**：
    mask 只有 1 bit，12dp 的圆弧在 1.75 倍屏上就是 21 像素的台阶；卡片圆角是
    ``drawRoundedRect`` 抗锯齿画的，两者放一起，窗口的角会明显更毛。
    代价是窗口矩形的四角对鼠标仍然"存在"（透明但可点），12px 可以接受。
    """

    def __init__(
        self,
        window: QWidget,
        *,
        resizable: bool = True,
        radius: int = WINDOW_RADIUS,
        fill=None,
        border=None,
    ) -> None:
        super().__init__(window)
        self.win = window
        self.radius = radius
        self._resizable = resizable
        self._fill = fill          # () -> "#rrggbb"：窗口底色
        self._border = border      # () -> "#rrggbb"：1px 描边色
        self._overlay: _FrameBorder | None = None
        self._max = False
        self._normal: QRect | None = None
        self._edges: list[_ResizeEdge] = []

        window.setWindowFlags(window.windowFlags() | Qt.FramelessWindowHint)
        if fill is not None:
            # 有了这一条，四角才是"真的透明"，抗锯齿边缘才能压到桌面上
            window.setAttribute(Qt.WA_TranslucentBackground, True)
            if border is not None:
                self._overlay = _FrameBorder(window, self)
        window.installEventFilter(self)
        # Python 侧引用跟着被监控的窗口走。只有 ``self.win`` 不够：调用方若没有
        # 长期持有 frame（对话框常是局部变量），wrapper 被回收而 C++ 对象还挂在
        # 窗口名下时，Qt 仍会回调 ``eventFilter``，访问 ``self.win`` 就是
        # ``AttributeError: 'WindowFrame' object has no attribute 'win'``。
        window._window_frame = self

    @property
    def is_maximized(self) -> bool:
        return self._max

    def fill_color(self) -> str:
        return self._fill() if self._fill is not None else ""

    def border_color(self) -> str:
        return self._border() if self._border is not None else ""

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.win:
            kind = event.type()
            if kind == QEvent.Paint:
                # 事件过滤器先于窗口自己的 paintEvent 跑，正好当"背景层"用
                self._paint_fill()
            elif kind == QEvent.Show:
                self._ensure_edges()
                self._place()
            elif kind == QEvent.Resize:
                self._place()
            elif kind in (QEvent.ActivationChange, QEvent.WindowActivate, QEvent.WindowDeactivate):
                # 标题栏靠 isActiveWindow() 决定画不画焦点条，状态变了要重绘
                self.win.update()
        return False

    # -- 背景 ------------------------------------------------------------
    def _paint_fill(self) -> None:
        if self._fill is None:
            return
        p = QPainter(self.win)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.win.rect())
        if self._max:
            # 最大化时不能留圆角，否则四角会漏出桌面
            p.fillRect(rect, QColor(self.fill_color()))
        else:
            # 路径落在整数像素边界上：直边是满覆盖（不会糊成半透明的一圈），
            # 只有圆弧处的边缘像素才带 alpha
            p.fillPath(rounded_path(rect, self.radius), QColor(self.fill_color()))
        p.end()

    # -- 最大化 ----------------------------------------------------------
    def toggle_maximize(self) -> None:
        self.set_maximized(not self._max)

    def set_maximized(self, on: bool) -> None:
        if on == self._max:
            return
        if on:
            self._normal = self.win.geometry()
            screen = self.win.screen() or QApplication.primaryScreen()
            if screen is not None:
                self.win.setGeometry(screen.availableGeometry())
            self._max = True
        else:
            if self._normal is not None:
                self.win.setGeometry(self._normal)
            self._max = False
        self._place()
        self.win.update()

    def restore_under_cursor(self, global_pos: QPoint) -> None:
        """从最大化状态还原，并让鼠标落在标题栏的同一横向比例上。"""
        if not self._max:
            return
        width = max(1, self.win.width())
        ratio = min(0.92, max(0.08, (global_pos.x() - self.win.x()) / width))
        normal = self._normal or self.win.geometry()
        self._max = False
        self.win.setGeometry(normal)
        self.win.move(global_pos.x() - int(normal.width() * ratio),
                      max(0, global_pos.y() - 19))
        self._place()
        self.win.update()

    # -- 热区 ------------------------------------------------------------
    def _ensure_edges(self) -> None:
        if self._edges or not self._resizable:
            return
        specs = [
            (Qt.TopEdge, Qt.SizeVerCursor),
            (Qt.BottomEdge, Qt.SizeVerCursor),
            (Qt.LeftEdge, Qt.SizeHorCursor),
            (Qt.RightEdge, Qt.SizeHorCursor),
            (Qt.TopEdge | Qt.LeftEdge, Qt.SizeFDiagCursor),
            (Qt.TopEdge | Qt.RightEdge, Qt.SizeBDiagCursor),
            (Qt.BottomEdge | Qt.LeftEdge, Qt.SizeBDiagCursor),
            (Qt.BottomEdge | Qt.RightEdge, Qt.SizeFDiagCursor),
        ]
        self._edges = [_ResizeEdge(self.win, edges, cursor) for edges, cursor in specs]

    def _place(self) -> None:
        if self._overlay is not None:
            # 描边层永远压在最上面：子控件是后加的，尺寸变化后要重新抬一次
            self._overlay.setGeometry(self.win.rect())
            self._overlay.raise_()
        if not self._edges:
            return
        if self._max:
            for edge in self._edges:
                edge.hide()
            return
        w, h = self.win.width(), self.win.height()
        geo = {
            Qt.TopEdge: QRect(CORNER, 0, w - CORNER * 2, EDGE),
            Qt.BottomEdge: QRect(CORNER, h - EDGE, w - CORNER * 2, EDGE),
            Qt.LeftEdge: QRect(0, CORNER, EDGE, h - CORNER * 2),
            Qt.RightEdge: QRect(w - EDGE, CORNER, EDGE, h - CORNER * 2),
            Qt.TopEdge | Qt.LeftEdge: QRect(0, 0, CORNER, CORNER),
            Qt.TopEdge | Qt.RightEdge: QRect(w - CORNER, 0, CORNER, CORNER),
            Qt.BottomEdge | Qt.LeftEdge: QRect(0, h - CORNER, CORNER, CORNER),
            Qt.BottomEdge | Qt.RightEdge: QRect(w - CORNER, h - CORNER, CORNER, CORNER),
        }
        for edge in self._edges:
            edge.setGeometry(geo[edge._edges])  # noqa: SLF001
            edge.show()
            edge.raise_()


class DimOverlay(QWidget):
    """盖在父窗口上的一层压暗蒙版，用来把"当前在跟哪个窗口打交道"说清楚。

    子窗口弹出来时主窗口变暗，前层就自动成为视觉焦点——这是系统模态窗口原本
    自带的效果，无边框之后得自己补。蒙版不吃鼠标事件，否则主窗口的悬停全废。

    "点压暗区域关掉子窗口"不在这里做：应用模态期间 Qt 会在窗口系统层丢弃落到
    主窗口上的点击（主窗口里的任何控件都收不到），见 ``DimClickShield``。
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self._alpha = 0.12
        self.hide()

    def set_palette(self, palette: theme.Palette) -> None:
        self._alpha = 0.24 if palette.is_dark else 0.12
        if self.isVisible():
            self.update()

    def cover(self, on: bool) -> None:
        if not on:
            self.hide()
            return
        parent = self.parentWidget()
        if parent is not None:
            self.setGeometry(parent.rect())
        self.show()
        self.raise_()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # 跟着窗口的圆角走，否则四角会被压成方角（窗口本身已经是透明的了）
        p.fillPath(rounded_path(QRectF(self.rect()), WINDOW_RADIUS),
                   QColor(0, 0, 0, int(round(255 * self._alpha))))
        p.end()


class DimClickShield(QWidget):
    """模态子窗口外侧的点击层：点一下压暗区 = 按 Esc，只关当前这一层。

    为什么不让 ``DimOverlay`` 直接吃点击：``QDialog.exec()`` 是应用模态，模态期间
    Qt 在窗口系统层就把落到被拦窗口（主窗口）上的鼠标事件丢掉了，主窗口里的控件
    根本收不到。唯一仍然能收点击的是模态窗口自己的子窗口，所以这里给每个弹窗挂
    一个透明子窗口，铺满主窗口，再用 ``setMask`` 把弹窗本身挖空：

    * 洞外按下再松开 —— ``reject()``，和 Esc 同一条路径；
    * 洞内点击 —— 命中下面的弹窗，视频单击、滑条拖动都不受影响；
    * 洞外按下、拖进弹窗再松开 —— 松开点落在洞里，不关；
    * 弹窗上按下、拖到洞外松开 —— 按下事件不经过这里，不关；
    * 滚轮 / 右键 —— 不参与。

    视觉仍然是主窗口里的 ``DimOverlay``，这一层不画可见的内容；点击区域就是压暗
    区域本身（同样的圆角、同样的范围），洞内被弹窗占住的部分不算。

    **必须画一层 alpha=1**。Windows 对 ``WS_EX_LAYERED`` 窗口的命中测试是逐像素的：
    alpha=0 的地方鼠标消息会直接穿透（微软文档里写明的 "hit testing of a layered
    window... alpha value is zero will let the mouse messages through"），
    ``setMask`` 只裁剪窗口区域，不会把零透明像素变成可点。完全透明的窗口在真机上
    表现就是"看着有遮罩、点哪都没反应"；离屏 QPA 不模拟这条，所以只有真机能测出来。
    alpha=1 的黑在 255 级里肉眼不可见，点击却能正常落到这一层。
    """

    def __init__(self, dialog: QWidget) -> None:
        super().__init__(dialog, Qt.Window | Qt.FramelessWindowHint
                         | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self._dialog = dialog
        self._press: QPoint | None = None
        dialog.installEventFilter(self)
        # 和 WindowFrame 同一条理由：Python 引用必须跟着弹窗走，别只靠调用方
        # 那个局部变量（否则 wrapper 被回收、C++ 还在，eventFilter 里就报
        # 'DimClickShield' object has no attribute '_dialog'）。
        dialog._dim_click_shield = self
        if dialog.isVisible() and self._place():
            self.show()
            self.raise_()

    def paintEvent(self, event) -> None:  # noqa: N802
        # alpha=1 是给 Windows 命中测试看的（见类注释），肉眼不可见；
        # 真正的压暗仍由主窗口里的 DimOverlay 画。setMask 已经把洞挖掉，
        # 这层填充只会落在可点的压暗区上。
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 1))
        p.end()

    # -- 跟随弹窗 --------------------------------------------------------
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self._dialog:
            kind = event.type()
            if kind == QEvent.Show:
                if self._place():
                    self.show()
                    self.raise_()
                    self._raise_layer_windows()
            elif kind in (QEvent.Move, QEvent.Resize):
                if self._place():
                    self.raise_()
                    self._raise_layer_windows()
            elif kind in (QEvent.Hide, QEvent.Close):
                self.hide()
        return False

    def _anchor(self) -> QWidget | None:
        """铺哪个窗口：弹窗所在的最外层窗口（主窗口）。

        嵌套时（详情页里再弹确认框）弹窗的 parent 是上一层弹窗，一直往上是主窗口。
        """
        w = self._dialog.parentWidget()
        if w is None:
            return None
        while w.parentWidget() is not None:
            w = w.parentWidget()
        return w

    def _stack(self) -> list[QWidget]:
        """当前弹窗 + 还开着的父弹窗：挖洞要一起挖，否则会盖住父弹窗。

        弹窗自己再开的独立子窗口（例如摄像头的独立播放窗）也是"洞"：那些区域
        属于上一层，不该被遮罩点击判成"点外侧"。
        """
        out: list[QWidget] = []
        w = self._dialog
        while w is not None:
            if isinstance(w, QDialog) and w.isVisible():
                out.append(w)
            w = w.parentWidget()
        for child in self._dialog.findChildren(
                QWidget, options=Qt.FindDirectChildrenOnly):
            if child is not self and child.isWindow() and child.isVisible():
                out.append(child)
        return out

    def _raise_layer_windows(self) -> None:
        """遮罩抬起后，把弹窗上的独立子窗口再抬回去，免得盖住它们。"""
        for child in self._dialog.findChildren(
                QWidget, options=Qt.FindDirectChildrenOnly):
            if child is not self and child.isWindow() and child.isVisible():
                child.raise_()

    def _place(self) -> bool:
        anchor = self._anchor()
        if anchor is None or not anchor.isVisible():
            self.hide()
            return False
        self.setGeometry(anchor.geometry())
        # 外边界跟压暗蒙版用同一条圆角路径，点击区域和看到的一致
        base = QRegion(rounded_path(QRectF(self.rect()), WINDOW_RADIUS)
                       .toFillPolygon().toPolygon())
        holes = QRegion()
        for w in self._stack():
            holes += QRegion(w.frameGeometry().translated(-self.x(), -self.y()))
        self.setMask(base.subtracted(holes))
        return True

    def _over_dialog(self, global_pos: QPoint) -> bool:
        for w in self._stack():
            if w.frameGeometry().contains(global_pos):
                return True
        return False

    # -- 交互 ------------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._press = (event.globalPosition().toPoint()
                       if event.button() == Qt.LeftButton else None)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        press, self._press = self._press, None
        if event.button() != Qt.LeftButton or press is None:
            return
        if not self.rect().contains(event.position().toPoint()):
            return
        # 按下和松开必须都在洞外：拖进 / 拖出弹窗的两端都不算"点遮罩"
        if self._over_dialog(press) or self._over_dialog(event.globalPosition().toPoint()):
            return
        self._dialog.reject()

    def wheelEvent(self, event) -> None:  # noqa: N802
        # 压在遮罩上的滚轮不改变任何东西，更不关窗
        event.ignore()


def exec_with_dim_click(dialog: QDialog) -> int:
    """模态执行弹窗，顺便让"点外侧压暗区"等同 Esc。

    只关当前这一层：``reject()`` 和 Esc 是同一条路径；嵌套时每个弹窗各有一层
    ``DimClickShield``，最上层的 shield 也在最上面，点到的只会是最上层。

    **关掉就销毁**（``deleteLater``，2026-11-12 修）。以前关掉只是 hide：``dialog``
    的 parent 是主窗口，C++ 对象被 parent 一直持有，于是挂在它身上的两个事件过滤器
    （``WindowFrame`` / ``DimClickShield``）也活了下来；而 Python 侧这些对象只由
    ``dialog.__dict__`` 和调用方的局部变量引用，函数一返回就可能被 GC。之后 Qt 继续
    回调它们的 ``eventFilter``，访问 ``self.win`` / ``self._dialog`` 就是
    ``AttributeError: 'WindowFrame' object has no attribute 'win'``——用户报的
    "连续开关设置页后终端刷错、主页面变空"就是这个。顺带解决另一件事：每次都新建
    对话框，不销毁就会越攒越多（每个还都是 ``Qt.Dialog`` 顶层窗口），反复开关会闪。
    """
    shield = getattr(dialog, "_dim_click_shield", None)
    if shield is None:
        shield = DimClickShield(dialog)
        dialog._dim_click_shield = shield
    try:
        return dialog.exec()
    finally:
        shield.hide()
        dialog.hide()
        dialog.deleteLater()


__all__ = [
    "BAR_H",
    "DimClickShield",
    "DimOverlay",
    "TitleBar",
    "WINDOW_RADIUS",
    "WindowFrame",
    "bar_bg",
    "dialog_bar_bg",
    "exec_with_dim_click",
    "rounded_path",
    "surface_bg",
    "surface_border",
]
