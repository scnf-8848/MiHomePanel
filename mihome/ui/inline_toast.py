# SPDX-License-Identifier: GPL-3.0-or-later
"""窗口内的轻提示：与极客版登录码那条提示同一套视觉。

视觉对齐 ``tooltip.toast_top``（取极客版登录码时那条），配色再对齐顶部警告条：
10px 圆角、状态色按 9%（深色 16%）混进卡面色做底、1px 描边按 38%（深色 52%）混、
左侧 4px 圆点、13px 常规字重、18×12 内边距、最宽 460、最小高 46。**没有图标、
没有阴影、没有淡出动画**——极客版那条也没有，照着来即可。

和 ``tooltip`` 悬停气泡的区别是**它不是顶层窗口，也不归 TipManager 管**：

- 悬停气泡是 ``Qt.ToolTip`` 顶层窗口，鼠标划过任何注册过 ``tooltip.watch`` 的控件
  （设备卡片）都会把它收掉；
- 这里的是主窗口的**子控件**：位置跟着窗口走，不受悬停 / 焦点 / 离开事件影响，
  只在计时器到点后自己消失。

锚点不再是某个控件，而是**右栏内容区**（含顶部说话条与页面宿主的那一列）：垂直贴
内容区顶部 + 沿用警告条的 ``12`` 边距，水平按内容区宽度居中、左右各留 ``28``。这样
说话条在功能区页面被藏起来时，提示的位置**完全不动**（右栏顶部不随横带显隐变化），
也不会挂在已经隐藏的输入框坐标上；水平中线与警告条、卡片墙一致（不是按整窗居中）。

用法（``SpeakerBar`` 里只建一个，主窗口随后把右栏内容区灌进来）::

    self._toast = InlineToast(self, self._p)
    self._toast.set_content(content_region)
    self._toast.show_message("已刷新 30 项属性")
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

from . import theme
from .alert_bar import ALERT_MARGIN_TOP, ALERT_MARGIN_X

#: 与 ``tooltip`` 的 ``MSG_*`` / ``DOT_*`` / ``RADIUS`` 保持一致。
FONT_PX = 13
PAD_X = 18
PAD_Y = 12
MAX_W = 460
MIN_H = 46
LINE_GAP = 2
RADIUS = 10
DOT_R = 4.0
DOT_LEAD = 22
#: 停留时长；极客版那条是 3.2 秒，这里沿用之前定的 3 秒。
MS = 3000
#: 浮层与窗口边缘的距离（只在窗口比提示还窄时兜底用）。
EDGE = 12


class InlineToast(QWidget):
    """窗口内的一次性提示浮层。一个窗口只该有一个实例。"""

    def __init__(
        self,
        content: QWidget,
        palette: theme.Palette,
    ) -> None:
        super().__init__(content.window())
        #: 定位基准：右栏内容区（宽度与 QStackedWidget 相同，顶部高于说话条）。
        #: 用右栏而不是页面宿主，是为了说话条显隐时提示不跟着上下漂。
        self._content = content
        self._p = palette
        self.is_ok = True
        self._text = ""
        self._lines: list[str] = []
        self._font = QFont(QApplication.font())
        self._font.setPixelSize(FONT_PX)
        self._fm = QFontMetrics(self._font)
        self.setVisible(False)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        if self.parentWidget() is not None:
            self.parentWidget().installEventFilter(self)
        # 内容区自己 resize / 移动也要跟着挪：窗口 resize 派发时子控件的几何
        # 可能还没更新，只监听窗口会在"布局刚变"的那一帧算到旧位置。
        self._content.installEventFilter(self)

    # -- 对外 ------------------------------------------------------------
    def show_message(self, text: str, kind: str = "ok", ms: int = MS) -> None:
        """显示（或替换）一条提示并重新计时；后到的直接顶掉先到的。

        ``kind`` 只认 ``ok`` / ``fail`` 两档；其它值一律按 ``fail``。宁可红得
        显眼，也别把一条本该报错的提示画成绿点（之前用 ``kind != "fail"``
        兜底，任何拼错的字符串都会静默变成成功）。
        """
        if not text:
            return
        # 构造时内容区可能还没接进主窗口，这里按当前顶层窗口重新挂一次
        host = self._content.window()
        if host is not None and self.parentWidget() is not host:
            if self.parentWidget() is not None:
                self.parentWidget().removeEventFilter(self)
            self.setParent(host)
            host.installEventFilter(self)
        self.is_ok = kind == "ok"
        self._text = text
        self._measure()
        self._place()
        self.show()
        self.raise_()
        self.update()
        self._timer.start(ms)

    def dismiss(self) -> None:
        self._timer.stop()
        self.hide()

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        if self.isVisible():
            self.update()

    def set_content(self, content: QWidget | None) -> None:
        """换定位基准（主窗口把右栏内容区灌进来）；正在显示就立刻挪过去。"""
        if content is None or content is self._content:
            return
        self._content.removeEventFilter(self)
        self._content = content
        content.installEventFilter(self)
        self.reposition()

    def reposition(self) -> None:
        """按内容区当前几何重算位置（切页改变内容区起点后要调一次）。"""
        if self.isVisible():
            self._place()

    def is_showing(self) -> bool:
        return self.isVisible() and self._timer.isActive()

    def text(self) -> str:
        return self._text

    # -- 视觉参数（验证脚本按这些断言） ----------------------------------
    def dot_color(self) -> str:
        return theme.ACCENT_GREEN if self.is_ok else theme.DANGER

    def bg_color(self) -> str:
        """底色：参照顶部警告条，状态色按 9%（深色 16%）混进卡面色。"""
        base = self._p.card if not self._p.is_dark else self._p.bg_soft
        return theme.blend(base, self.dot_color(), 0.16 if self._p.is_dark else 0.09)

    def border_color(self) -> str:
        """描边：警告条的比例（浅色 38% / 深色 52%）。"""
        base = self._p.card if not self._p.is_dark else self._p.bg_soft
        return theme.blend(base, self.dot_color(), 0.52 if self._p.is_dark else 0.38)

    # -- 布局 ------------------------------------------------------------
    def _measure(self) -> None:
        fm = self._fm
        # 内容区左右各扣掉警告条那套内边距，再和最大宽取小
        budget = min(MAX_W, max(120, self._content.width() - ALERT_MARGIN_X * 2))
        limit = max(60, budget - (PAD_X + DOT_LEAD + PAD_X))
        lines: list[str] = []
        for para in self._text.split("\n"):
            cur = ""
            for ch in para:
                if fm.horizontalAdvance(cur + ch) <= limit or not cur:
                    cur += ch
                else:
                    lines.append(cur)
                    cur = ch
            lines.append(cur)
        self._lines = lines or [""]
        text_w = max(fm.horizontalAdvance(line) for line in self._lines)
        text_h = len(self._lines) * fm.height() + (len(self._lines) - 1) * LINE_GAP
        w = PAD_X + DOT_LEAD + text_w + PAD_X + 2
        h = max(MIN_H, text_h + PAD_Y * 2 + 2)
        self.resize(w, h)

    def _place(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        # 参照警告条：垂直贴内容区顶部 + 同一套上下边距；水平按**内容区**宽度居中，
        # 不是整窗——按整窗居中会左移半个侧栏宽，和警告条 / 卡片墙的中线对不齐。
        at = self._content.mapTo(parent, QPoint(0, 0))
        left = at.x() + ALERT_MARGIN_X
        right = at.x() + self._content.width() - ALERT_MARGIN_X
        x = at.x() + (self._content.width() - self.width()) // 2
        if self.width() <= right - left:
            x = max(left, min(x, right - self.width()))
        else:
            # 内容区比提示还窄：先保住左内边距，再保证不越出窗口
            x = max(EDGE, min(left, parent.width() - EDGE - self.width()))
        y = max(EDGE, at.y() + ALERT_MARGIN_TOP)
        self.move(x, y)

    def eventFilter(self, obj, evt) -> bool:  # noqa: N802
        # 窗口或内容区几何变化时如果提示还在，跟着重算位置（内容区自己的
        # resize / move 也要接：窗口 resize 时子控件未必已经拿到新几何）
        if evt.type() in (QEvent.Resize, QEvent.Move) and self.isVisible() \
                and obj in (self.parentWidget(), self._content):
            self._place()
        return super().eventFilter(obj, evt)

    # -- 绘制 ------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        body = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(self.border_color()), 1))
        p.setBrush(QColor(self.bg_color()))
        p.drawRoundedRect(body, RADIUS, RADIUS)

        # 左侧状态圆点：唯一的语义色，文字保持主文字色
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self.dot_color()))
        p.drawEllipse(QPointF(PAD_X + DOT_R + 1, self.height() / 2), DOT_R, DOT_R)

        p.setPen(QColor(self._p.text))
        p.setFont(self._font)
        text_x = PAD_X + DOT_LEAD
        text_h = len(self._lines) * self._fm.height() + (len(self._lines) - 1) * LINE_GAP
        y = (self.height() - text_h) / 2
        for line in self._lines:
            p.drawText(int(text_x), int(y + self._fm.ascent()), line)
            y += self._fm.height() + LINE_GAP

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # 提示浮在任何东西上面：点击只收起提示，不穿透到底下的卡片
        self.dismiss()
        event.accept()
