# SPDX-License-Identifier: GPL-3.0-or-later
"""悬停提示：自己画，不用 ``QToolTip``。

原生 tooltip 有两个问题，都是"全局"的，没法按控件调：

1. **节奏由 Qt 统一管**——第一次要等 ``SH_ToolTip_WakeUpDelay``（默认 700ms），
   而一旦某个控件显示过提示，20 秒内（``FallAsleepDelay``）在**别的**控件上会
   立刻弹出。表现就是"第一台设备等半天，之后每台都秒出"，用户要的是每台设备
   都独立计时。
2. **窗口是系统画的**——``QToolTip`` 的矩形底色由平台窗口承担，样式表里的
   ``border-radius`` 只是在上面再画一遍圆角图案，四个角外面仍然是那块方底。
   和右键菜单（``ui/menu.py``）一模一样的毛病。

所以这一层自己做：``Qt.ToolTip`` 独立窗口 + 透明背景 + 自绘圆角，计时完全由
``TipManager`` 控制——进入一个新的控件就重新计时，离开就取消，互不影响。

用法：

    from . import tooltip
    tooltip.watch(card, lambda: card.state.name)

``provider`` 是惰性的：提示文字每次显示时才取，控件内容变了不用重新注册。

**可以只让控件的一部分触发提示**：传 ``hit_test``，它收到控件局部坐标的
``QPoint``，返回 True 才算"悬停在有效区"。设备卡片就靠它把提示钉在标题带——
亮度/色温滑条、多键按键区都是可拖可点的真控件，气泡盖上去既挡视线又挡操作。
进了子控件时父控件只收到 Leave，卡片**内部**从标题移到内容带则只有 MouseMove，
所以两种事件都要按 ``hit_test`` 复判（见 ``eventFilter``）。
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import (
    QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt, QTimer,
)
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

from . import theme

#: 悬停多久出提示。Qt 原生默认 700ms，实测偏慢——桌面用户鼠标停留通常不到半秒。
DELAY = 380
#: 提示框与光标之间的距离（右下方向）。
GAP = 16
PAD_X = 12
PAD_Y = 7
MAX_W = 340  # 超过就折行，不然长名字会横穿半个屏幕
LINE_GAP = 2
RADIUS = 10
#: 一次性提示（toast）默认停留多久。2.6 秒：够读完一句"已发送：把客厅灯关掉"，
#: 又不至于在下一条指令输入时还挂着。
TOAST_MS = 2600

#: 提示的语义。``toast`` 是锚在控件下方的一条回声，``toast_top`` 是屏幕中央
#: 偏上的通知——后者要跨程序停留，两种的排版和寿命都不一样，颜色也不同。
TOAST_INFO = "info"
TOAST_OK = "ok"
TOAST_FAIL = "fail"
#: 居中提示落在屏幕可用高度从上往下 18% 处（1080p ≈ 190px）。"中间偏上"：
#: 视线从屏幕中部往上抬一点就看到，又不像系统通知那样贴着上沿。
TOAST_TOP_RATIO = 0.18
#: 居中提示的字号与留白，比悬停提示大一档——它在屏幕上孤零零一条，
#: 12px 会读成"某个程序打印了一行调试信息"。
MSG_FONT = 13
MSG_PAD_X = 18
MSG_PAD_Y = 12
MSG_MAX_W = 460
MSG_MIN_H = 46
#: 圆点直径 + 它到文字的间距。``toast_top`` 用一个小圆点表达成功/失败，
#: 不靠文字颜色——13px 的绿字在白底上读不出来。
DOT_R = 4.0
DOT_LEAD = 22
MSG_MS = 3200



def _kind_color(kind: str, palette: theme.Palette) -> str:
    """提示语义 → 圆点颜色。``info`` 没有语义色，用主题强调色。"""
    if kind == TOAST_OK:
        return theme.ACCENT_GREEN
    if kind == TOAST_FAIL:
        return theme.DANGER
    return palette.accent


def _wrap(text: str, fm: QFontMetrics, max_w: int) -> list[str]:
    """先按 ``\\n`` 分段，段内再按字符折行。

    设备名多是中文（"小米米家智能开关（三开单控）"），没有空格，``TextWordWrap``
    对它们完全无效，只能按字宽自己断。混排的英文/数字也跟着字符走，最坏情况是
    从单词中间断开——比撑出屏幕强。

    **``\\n`` 必须在这里吃掉，不能留给 ``drawText``**：provider 写的换行是作者
    意图（"动作一句、后果一句"）。整串丢进按字宽折行的话，``\\n`` 会被当成一个
    普通字符塞进某一行中间，而 ``paintEvent`` 给每行的矩形只有 ``fm.height()``
    高——``drawText`` 见到 ``\\n`` 仍然换行，第二行溢出矩形，和后一行叠在一起。
    表现就是提示文字挤成一团（2026-11-11 用户实测截图）。
    """
    lines: list[str] = []
    for para in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        cur = ""
        for ch in para:
            if fm.horizontalAdvance(cur + ch) <= max_w or not cur:
                cur += ch
            else:
                lines.append(cur)
                cur = ch
        # 空段也要占一行：作者的换行就是要有条缝
        lines.append(cur)
    return lines or [""]


class TipBubble(QWidget):
    """一块圆角提示条。整个进程只用一个，反复复用。"""

    def __init__(self) -> None:
        super().__init__(
            None, Qt.ToolTip | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._p = theme.LIGHT
        self._lines: list[str] = []
        self._font = QFont()
        self._font.setPixelSize(12)
        #: 当前排版参数。悬停提示与锚定提示用 12px 的窄版，居中提示用 ``MSG_*``
        #: 那一套；三个数跟着每次 ``_measure`` 一起换，绘制只读它们。
        self._pad_x = PAD_X
        self._pad_y = PAD_Y
        #: 非空就在文字左边画一个小圆点，值就是那个颜色（成功/失败的区别）。
        self._dot = ""
        #: 程序失焦也不收。只给居中提示打开——它是"跨程序的通知"，
        #: 而悬停提示和锚定提示都是"本程序内的一条回声"，失焦就没意义了。
        self._pinned = False
        #: 一次性提示的自动收起。和悬停提示共用同一个窗口，但计时归 toast 自己管：
        #: 悬停那条链（TipManager）只负责"光标停多久出提示"，不碰这里。
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self.hide)
        # 程序一失去激活就收（见 ``_on_app_state``）。这里连信号，不装事件过滤器：
        # 气泡是顶层窗口，主窗口的 WindowDeactivate 它收不到。
        app = QGuiApplication.instance()
        if app is not None:
            app.applicationStateChanged.connect(self._on_app_state)

    def _on_app_state(self, state: Qt.ApplicationState) -> None:
        """程序失去激活 → 立刻收起（居中提示除外）。

        气泡是 ``Qt.ToolTip`` 顶层窗口，**会盖在别的程序上面**。悬停提示和锚定
        提示都是"本程序内的一条回声"：用户切走了，它就没有读者了。最典型的一幕是
        「点卡片 → 复制登录码 → 打开浏览器」——那一刻主窗口被盖住，挂在它上面的
        提示成了浏览器顶上的一块膏药（2026-11-11 实测）。

        居中提示是反过来的：它的全部价值就在于"用户已经跳到浏览器了，还能看见
        刚才那串码"。所以它自己钉住（``_pinned``），只由计时器收。
        """
        if state != Qt.ApplicationState.ApplicationActive and not self._pinned:
            self._toast_timer.stop()
            self.hide()

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.update()

    def is_toasting(self) -> bool:
        """当前显示的是不是一次性提示（而不是悬停提示）。

        ``TipManager`` 用它挡掉一个真发生过的误收：``Leave`` 的收尾是延后一个
        事件循环执行的（见 ``_after_leave``），如果那个 singleShot 正好落在
        ``show_toast`` 之后，就会把刚弹出来的一次性提示一起收掉。
        """
        return self._toast_timer.isActive()

    def pinned(self) -> bool:
        """当前是不是那条"跨程序也停留"的居中通知。

        它是**独占**的：显示期间悬停提示不抢占（用户正在看结果，鼠标划过的
        卡片不该把它顶掉），程序失焦也不收。
        """
        return self._pinned and self.isVisible()

    def _measure(
        self,
        text: str,
        *,
        size: int = 12,
        max_w: int = MAX_W,
        pad_x: int = PAD_X,
        pad_y: int = PAD_Y,
        lead: int = 0,
        min_h: int = 0,
    ) -> tuple[int, int]:
        """按 ``text`` 折行并算出气泡尺寸（顺便把行填进 ``self._lines``）。

        ``size/pad_*/lead/min_h`` 是三种提示共用的排版旋钮：悬停与锚定提示用默认
        那一套（12px 窄条），居中提示用 ``MSG_*``。绘制只读这里落下的
        ``_pad_x/_pad_y`` 和 ``_font``，所以它俩必须成对更新。
        """
        self._font = QFont(QApplication.font())
        self._font.setPixelSize(size)
        self._pad_x = pad_x
        self._pad_y = pad_y
        fm = QFontMetrics(self._font)
        lines = _wrap(text, fm, max_w) or [""]
        self._lines = lines
        text_w = max(fm.horizontalAdvance(line) for line in lines)
        line_h = fm.height()
        w = text_w + pad_x * 2 + lead + 2
        h = len(lines) * line_h + (len(lines) - 1) * LINE_GAP + pad_y * 2 + 2
        return w, max(h, min_h)

    def _plain(self) -> None:
        """回到"悬停/锚定提示"那一套排版与行为。"""
        self._dot = ""
        self._pinned = False

    def show_for(self, text: str, at: QPoint, palette: theme.Palette | None = None) -> None:
        if palette is not None:
            self._p = palette
        self._toast_timer.stop()
        self._plain()
        w, h = self._measure(text)
        self.resize(w, h)

        # 贴屏幕边时翻到光标另一侧，别让提示跑出可视区
        screen = QGuiApplication.screenAt(at) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        x = at.x() + GAP
        y = at.y() + GAP
        if x + w > area.right() + 1:
            x = at.x() - w - GAP
        if y + h > area.bottom() + 1:
            y = at.y() - h - GAP
        self.move(max(area.left(), x), max(area.top(), y))
        self.show()
        self.raise_()
        self.update()

    def show_toast(
        self,
        text: str,
        anchor: QRect,
        palette: theme.Palette | None = None,
        *,
        ms: int = TOAST_MS,
    ) -> None:
        """一次性提示：锚在一个**控件矩形**下方居中，``ms`` 后自己消失。

        和悬停提示共用同一个窗口，但驱动方式相反——这里不看鼠标、不重计时，
        调用方给位置、给时长，到点就收。``anchor`` 是全局坐标矩形。
        """
        if palette is not None:
            self._p = palette
        self._plain()
        w, h = self._measure(text)
        self.resize(w, h)

        screen = QGuiApplication.screenAt(anchor.center()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        x = anchor.center().x() - w // 2
        # 先试锚点下方；下方放不下（贴近屏幕底）就翻到上方
        y = anchor.bottom() + 8
        if y + h > area.bottom() + 1:
            y = anchor.top() - h - 8
        x = max(area.left(), min(x, area.right() + 1 - w))
        y = max(area.top(), y)
        self.move(x, y)
        self.show()
        self.raise_()
        self.update()
        self._toast_timer.start(ms)

    def show_message(
        self,
        text: str,
        anchor: QRect,
        palette: theme.Palette | None = None,
        *,
        kind: str = TOAST_INFO,
        ms: int = MSG_MS,
    ) -> None:
        """居中偏上的通知：**跨程序停留**，``ms`` 后自己消失。

        和 ``show_toast`` 的三点不同，每一点都是被实测逼出来的：

        1. **位置在屏幕上沿往下 18%**，横向居中。锚在卡片下方的话，点完按钮浏览器
           就抢走了整屏，提示只活在那 0.2 秒里，等于没有；
        2. **不随失焦收起**（``_pinned``）。用户此刻正在浏览器里等这串码，
           它必须跟过去；
        3. **成功/失败各一种样子**——一个小圆点定色（绿/红），边框跟着染。
           成功和失败都只有两个字之差，只靠文字区分等于没区分。

        ``anchor`` 只用来决定落在**哪块屏幕**（多显示器时跟主窗口走）。
        """
        if palette is not None:
            self._p = palette
        self._dot = _kind_color(kind, self._p)
        self._pinned = True
        w, h = self._measure(
            text,
            size=MSG_FONT,
            max_w=MSG_MAX_W,
            pad_x=MSG_PAD_X,
            pad_y=MSG_PAD_Y,
            lead=DOT_LEAD,
            min_h=MSG_MIN_H,
        )
        self.resize(w, h)

        screen = QGuiApplication.screenAt(anchor.center()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        y = area.top() + int(area.height() * TOAST_TOP_RATIO)
        self.move(area.left() + (area.width() - w) // 2, y)
        self.show()
        self.raise_()
        self.update()
        self._toast_timer.start(ms)

    def border_color(self) -> str:
        """气泡描边色。有圆点时跟着圆点染一档——1px 的边在截图上不好取样，
        所以绘制和验证都走这一个出口，别各算一遍。"""
        return theme.blend(self._p.card, self._dot, 0.45) if self._dot else self._p.border

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        border = self.border_color()
        p.setPen(QPen(QColor(border), 1))
        p.setBrush(QColor(self._p.card))
        p.drawRoundedRect(rect, RADIUS, RADIUS)

        text_x = self._pad_x + 1
        if self._dot:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(self._dot))
            p.drawEllipse(
                QPointF(self._pad_x + DOT_R + 1, self.height() / 2), DOT_R, DOT_R
            )
            text_x = self._pad_x + DOT_LEAD

        p.setPen(QColor(self._p.text))
        p.setFont(self._font)
        fm = QFontMetrics(self._font)
        y = self._pad_y + 1
        for line in self._lines:
            p.drawText(
                QRect(text_x, y, self.width() - text_x - self._pad_x, fm.height()),
                int(Qt.AlignLeft | Qt.AlignVCenter),
                line,
            )
            y += fm.height() + LINE_GAP


class TipManager(QObject):
    """全局唯一的提示调度器。

    计时按**控件**算：进入 A 就启动 380ms，光标移到 B 会先取消 A 的计时再给 B
    重新计时。这样"每台设备独立"是结构性的，不依赖 Qt 的 wake-up 全局状态。
    """

    def __init__(self) -> None:
        super().__init__()
        self._bubble = TipBubble()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DELAY)
        self._timer.timeout.connect(self._on_timeout)
        self._providers: dict[QWidget, Callable[[], str]] = {}
        #: 可选的有效区判据，参数是控件局部坐标。没注册就退回"整个控件矩形"。
        self._hit_tests: dict[QWidget, Callable[[QPoint], bool]] = {}
        self._target: QWidget | None = None
        self._p = theme.LIGHT
        #: 光标位置来源。测试里替换成固定点，生产环境就是真的鼠标。
        self._cursor_pos: Callable[[], QPoint] = QCursor.pos

    # -- 注册 ------------------------------------------------------------
    def watch(
        self,
        widget: QWidget,
        provider: Callable[[], str],
        hit_test: Callable[[QPoint], bool] | None = None,
    ) -> None:
        self._providers[widget] = provider
        if hit_test is None:
            self._hit_tests.pop(widget, None)
        else:
            self._hit_tests[widget] = hit_test
        widget.installEventFilter(self)
        widget.destroyed.connect(lambda *_: self._forget(widget))

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self._bubble.set_palette(palette)

    def show_toast(
        self,
        text: str,
        anchor: QRect,
        palette: theme.Palette | None = None,
        *,
        ms: int = TOAST_MS,
    ) -> None:
        """一次性提示（见 ``TipBubble.show_toast``）。与悬停提示互不影响。"""
        self._dismiss()
        self._bubble.show_toast(text, anchor, palette or self._p, ms=ms)

    def show_message(
        self,
        text: str,
        anchor: QRect,
        palette: theme.Palette | None = None,
        *,
        kind: str = TOAST_INFO,
        ms: int = MSG_MS,
    ) -> None:
        """居中偏上的通知（见 ``TipBubble.show_message``）。"""
        self._dismiss()
        self._bubble.show_message(text, anchor, palette or self._p, kind=kind, ms=ms)

    # -- 内部 ------------------------------------------------------------
    def _forget(self, widget: QWidget) -> None:
        self._providers.pop(widget, None)
        self._hit_tests.pop(widget, None)
        if self._target is widget:
            self._dismiss()

    def _still_inside(self, widget: QWidget) -> bool:
        """光标是不是还落在控件的**有效区**里。

        默认整个矩形；注册了 ``hit_test`` 的控件（设备卡片）只有标题带算数。
        Enter / Leave / 定时到点 / MouseMove 四处都走这里，判定只有一份。
        """
        try:
            if not widget.isVisible():
                return False
            pos = widget.mapFromGlobal(self._cursor_pos())
            # 先钉住控件矩形，hit_test 只负责在矩形**内部**再收窄。否则一个
            # 写松了的判据会让光标已经移出控件还继续算"悬停中"。
            if not widget.rect().contains(pos):
                return False
            test = self._hit_tests.get(widget)
            return True if test is None else bool(test(pos))
        except RuntimeError:  # 底层 C++ 对象已经没了
            return False

    def _enter(self, widget: QWidget) -> None:
        # 从卡片里的子控件（电源键、按键区）移回卡片本身也会收到 Enter，
        # 这时不重新计时，否则在卡片内部划一下提示就重新等了。
        if self._target is widget:
            return
        # 居中通知正在显示：让位到它收起来为止。它是"刚才那件事的结果"，
        # 比一条设备名重要；而 Enter 的第一件事就是 ``_dismiss`` 把它踢掉。
        if self._bubble.pinned():
            return
        # 提示收起时 Qt 会把 hover 重新派发给下面的控件，凭空冒出一个 Enter。
        # 光标其实不在这个控件上，不能当成"用户刚移进来"。
        if not self._still_inside(widget):
            return
        self._dismiss()
        self._target = widget
        self._timer.start()

    def _after_leave(self, widget: QWidget) -> None:
        """Leave 要延后一个事件循环再判。

        鼠标移到卡片的子控件上时，卡片自己也会收到 Leave。立刻隐藏的话，
        提示会在"卡片 ↔ 电源键"之间反复闪。延后之后真正起决定作用的是
        ``_still_inside``：光标还在有效区就留着，落到内容带上（滑条 /
        按键区）就收起——正好把"悬停提示遮挡控件"挡掉。
        """
        if self._target is not widget:
            return
        if self._bubble.is_toasting():
            # 一次性提示正显示着：它的寿命归 toast 计时器管，别被这段延后的
            # Leave 收尾顺手收掉
            return
        if self._still_inside(widget):
            return
        self._dismiss()

    def _on_timeout(self) -> None:
        widget = self._target
        if widget is None:
            return
        if self._bubble.pinned():
            # 计时是在通知弹出之前启动的：它还没到点，别让悬停提示盖上去
            return
        if not self._still_inside(widget):
            self._dismiss()
            return
        try:
            text = self._providers.get(widget, lambda: "")()
        except RuntimeError:
            self._dismiss()
            return
        if text:
            self._bubble.show_for(text, self._cursor_pos(), self._p)

    def _hide(self) -> None:
        """收起气泡但**保留** target。

        点击、滚轮这类"用户已经动手了"的收起，光标还停在原控件上。保留 target
        才能让随后那个"假 Enter"撞上 ``_target is widget`` 直接返回——否则提示
        会自己再弹一次。
        """
        try:
            self._timer.stop()
            self._bubble.hide()
        except RuntimeError:
            # 退出程序时 manager 的 C++ 对象可能已经没了，这时没什么可收的。
            pass

    def _dismiss(self) -> None:
        """彻底收起：光标离开、目标被销毁、或者该换人了。"""
        self._target = None
        self._hide()

    # -- 事件 ------------------------------------------------------------
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        kind = event.type()
        if kind == QEvent.Enter:
            self._enter(obj)
        elif kind == QEvent.Leave:
            QTimer.singleShot(0, lambda o=obj: self._after_leave(o))
        elif kind == QEvent.MouseMove and obj in self._hit_tests:
            # 有限定区时，控件内部从标题移到内容带**不会**产生 Enter/Leave，
            # 只有 MouseMove。离开有效区立刻收，重新进来重新计时。
            if self._target is obj:
                if not self._still_inside(obj):
                    self._dismiss()
            elif self._still_inside(obj):
                self._enter(obj)
        elif kind in (QEvent.MouseButtonPress, QEvent.Wheel, QEvent.WindowDeactivate):
            if obj is self._target and not self._bubble.pinned():
                self._hide()
        elif kind == QEvent.Hide:
            if obj is self._target:
                self._dismiss()
        return False


_manager: TipManager | None = None


def manager() -> TipManager:
    global _manager
    if _manager is None:
        if QApplication.instance() is None:
            raise RuntimeError("tooltip 需要先有 QApplication")
        _manager = TipManager()
    return _manager


def watch(
    widget: QWidget,
    provider: Callable[[], str],
    hit_test: Callable[[QPoint], bool] | None = None,
) -> None:
    manager().watch(widget, provider, hit_test)


def set_palette(palette: theme.Palette) -> None:
    """主题切换时叫一声。没人注册过提示就不用建管理器。"""
    if _manager is not None:
        _manager.set_palette(palette)


def toast(
    anchor: QWidget,
    text: str,
    palette: theme.Palette | None = None,
    *,
    ms: int = TOAST_MS,
) -> None:
    """在 ``anchor`` 控件**下方居中**弹一条一次性提示，``ms`` 后自己消失。

    没有 QApplication（纯逻辑脚本）或控件已销毁时静默返回：提示是附赠信息，
    绝不能因为它自己崩掉主流程。
    """
    if not text or QApplication.instance() is None:
        return
    try:
        rect = QRect(anchor.mapToGlobal(QPoint(0, 0)), anchor.size())
        manager().show_toast(text, rect, palette, ms=ms)
    except RuntimeError:  # 底层 C++ 对象已经没了
        return


def toast_top(
    anchor: QWidget,
    text: str,
    palette: theme.Palette | None = None,
    *,
    kind: str = TOAST_INFO,
    ms: int = MSG_MS,
) -> None:
    """在**屏幕上沿往下一点、横向居中**弹一条通知，``ms`` 后自己消失。

    和 :func:`toast` 的分工：``toast`` 是"这个控件刚才做了什么"的回声，贴在控件
    下方；这条是"刚才那件事的结果"，用户很可能已经切到别的程序去了（点卡片 →
    浏览器抢焦点），所以它钉在屏幕上、不随失焦收起、成功失败各一种颜色。

    ``anchor`` 只用来定位到主窗口所在的那块屏幕。
    """
    if not text or QApplication.instance() is None:
        return
    try:
        rect = QRect(anchor.mapToGlobal(QPoint(0, 0)), anchor.size())
        manager().show_message(text, rect, palette, kind=kind, ms=ms)
    except RuntimeError:  # 底层 C++ 对象已经没了
        return


__all__ = [
    "DELAY",
    "MAX_W",
    "MSG_MS",
    "RADIUS",
    "TOAST_FAIL",
    "TOAST_INFO",
    "TOAST_MS",
    "TOAST_OK",
    "TOAST_TOP_RATIO",
    "TipBubble",
    "TipManager",
    "manager",
    "set_palette",
    "toast",
    "toast_top",
    "watch",
]
