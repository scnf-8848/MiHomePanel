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

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, Qt, QTimer
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


def _wrap(text: str, fm: QFontMetrics, max_w: int) -> list[str]:
    """按字符折行。

    设备名多是中文（"小米米家智能开关（三开单控）"），没有空格，``TextWordWrap``
    对它们完全无效，只能按字宽自己断。混排的英文/数字也跟着字符走，最坏情况是
    从单词中间断开——比撑出屏幕强。
    """
    lines: list[str] = []
    cur = ""
    for ch in text:
        if fm.horizontalAdvance(cur + ch) <= max_w or not cur:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines


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
        #: 一次性提示的自动收起。和悬停提示共用同一个窗口，但计时归 toast 自己管：
        #: 悬停那条链（TipManager）只负责"光标停多久出提示"，不碰这里。
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self.hide)

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.update()

    def _measure(self, text: str) -> tuple[int, int]:
        """按 ``text`` 折行并算出气泡尺寸（顺便把行填进 ``self._lines``）。"""
        self._font = QFont(QApplication.font())
        self._font.setPixelSize(12)
        fm = QFontMetrics(self._font)
        lines = _wrap(text, fm, MAX_W) or [""]
        self._lines = lines
        text_w = max(fm.horizontalAdvance(line) for line in lines)
        line_h = fm.height()
        w = text_w + PAD_X * 2 + 2
        h = len(lines) * line_h + (len(lines) - 1) * LINE_GAP + PAD_Y * 2 + 2
        return w, h

    def show_for(self, text: str, at: QPoint, palette: theme.Palette | None = None) -> None:
        if palette is not None:
            self._p = palette
        self._toast_timer.stop()
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

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(self._p.border), 1))
        p.setBrush(QColor(self._p.card))
        p.drawRoundedRect(rect, RADIUS, RADIUS)

        p.setPen(QColor(self._p.text))
        p.setFont(self._font)
        fm = QFontMetrics(self._font)
        y = PAD_Y + 1
        for line in self._lines:
            p.drawText(
                QRect(PAD_X + 1, y, self.width() - PAD_X * 2, fm.height()),
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
        if self._still_inside(widget):
            return
        self._dismiss()

    def _on_timeout(self) -> None:
        widget = self._target
        if widget is None:
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
            if obj is self._target:
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


__all__ = [
    "DELAY",
    "MAX_W",
    "RADIUS",
    "TOAST_MS",
    "TipBubble",
    "TipManager",
    "manager",
    "set_palette",
    "toast",
    "watch",
]
