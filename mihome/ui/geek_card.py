# SPDX-License-Identifier: GPL-3.0-or-later
"""中枢网关卡片：左半产品图，右半一个「打开极客版 + 复制登录码」的大按钮。

为什么会长成这样（用户 2026-11-11 给的参考图 + 需求）：

* 网关这类设备**没有可拨的开关、没有可读的数值**，标准卡就是一张产品图，
  摆在那里不产生任何交互。而真正高频的动作只有一个：进极客版写/看规则。
* 登录码是「一次性 + 取新码会让旧码失效」的，所以这个动作必须**由人触发**、
  一次一枚，不能预取、不能放进轮询（见 ``core/geek.py`` 顶部）。
* 于是把动作做成卡片上的大按钮：点一下 = 取地址 → 取码 → 复制到剪贴板 →
  打开浏览器。四步在 ``MainWindow`` 里串起来，卡片只负责"被点了"。

几何：外层仍是 ``DeviceCard``（外壳、标题带、副标题都在父类画），这里只 override
两处落点——``_art_rect()`` 收窄到左半（产品图），``_geek_rect()`` 拿右半放按钮。
这样 2×1 的尺寸、圆角、hover、离线压暗全部沿用外壳，不用重画一遍。
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ..core.store import DeviceState
from . import theme, tooltip
from .card_templates import BODY_GEEK, CardStyle
from .device_card import DeviceCard
from .widgets import IconProvider

#: 圆角与描边。2px 描边在深色卡上是这根按钮唯一的分界线，细了就散
BTN_R = 12
BTN_W = 2
#: 两行文字的间距
BTN_LINE_GAP = 5
#: 悬停 / 按下时往强调色里掺多少。10% 只是"这块是活的"，不抢产品图
BTN_HOVER = 0.10
BTN_PRESS = 0.20

#: 按钮上的三种结果态。``idle`` 是常态；另两个把"刚才发生了什么"直接写在按钮上：
#: 浏览器一开就把主窗口盖住，提示条再漂亮也只是配角，回到程序时按钮上还留着
#: 结论才是真正的"结果"。失败时按钮同时是重试入口，所以两种都保留第二行。
STATE_IDLE = "idle"
STATE_DONE = "done"
STATE_FAILED = "failed"

#: 结果态在按钮上停留多久，到点自己退回常态。
#:
#: 30 秒是「切去浏览器看一眼、回来还记得刚才发生了什么」的尺度：短于 15 秒，
#: 从浏览器切回来时按钮已经变回「打开极客版」，等于什么都没说；再长就成了常驻
#: 装饰，按钮本来的动作文案反而看不到。码本身已经在剪贴板里，按钮不需要一直举着它。
RESULT_MS = 30_000


def _tint(palette: theme.Palette) -> str:
    """按钮颜色 = 主题强调色。

    第一版照抄了参考图里的红棕（``#B5432C``），结果整面墙里只有这一块红——
    选中态、焦点框、菜单高亮全用 ``accent``，冒出一块外来色反而像别人的控件。
    参考图只借几何（左图右按钮），颜色一律回到主题。
    """
    return palette.accent


class GeekButton(QWidget):
    """卡片右半那个大按钮。自绘，两行字，整块可点。

    状态只有四种：常态（描边）、悬停（淡底）、按下（更深的底）、取码中
    （文案换成一行「正在取码…」，点不动）。取码是个几百毫秒的云端往返，
    不给反馈的话用户会以为没点上，于是连点——而每点一次就消耗一枚码。
    """

    clicked = Signal()

    def __init__(self, palette: theme.Palette, parent: QWidget) -> None:
        super().__init__(parent)
        self._p = palette
        self._hover = False
        self._press = False
        self._busy = False
        #: 上一次取码的结果（``STATE_*``）。点第二次时不清空——只有新的结果才换掉它。
        self._state = STATE_IDLE
        #: 上次取到的登录码，成功态直接印在按钮上
        self._code = ""
        #: 设备离线时置灰（云端 RPC 要转发给这台设备，离线时取不到）
        self._live = True
        #: 结果态自动退回常态。挂在自己身上（父对象）——卡片随整墙重建会被
        #: ``deleteLater``，独立计时器会在那之后才触发，撞上一个已销毁的按钮。
        self._revert = QTimer(self)
        self._revert.setSingleShot(True)
        self._revert.timeout.connect(self._revert_idle)
        self.setCursor(Qt.PointingHandCursor)
        tooltip.watch(self, self._tip)

    # -- 对外 ------------------------------------------------------------
    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.update()

    def set_busy(self, busy: bool) -> None:
        """取码往返期间置忙：文案换掉、不再接受点击。"""
        busy = bool(busy)
        if busy == self._busy:
            return
        self._busy = busy
        if busy:
            # 新一轮取码开始，上一次的结论已经没有显示价值（文案被「正在取码…」
            # 盖住了），别再让旧计时器到点把新结果提前抹掉。
            self._revert.stop()
        self.setCursor(Qt.BusyCursor if busy else Qt.PointingHandCursor)
        self.update()

    def is_busy(self) -> bool:
        return self._busy

    def set_result(self, code: str) -> None:
        """取码成功：按钮改成「已复制登录码 + 那串码 + 重新获取」。"""
        self._state = STATE_DONE
        self._code = str(code)
        self._hold_result()
        self.update()

    def set_error(self) -> None:
        """取码失败：按钮改成「获取登录码失败 + 重新获取」。

        失败原因（云端原话）不往按钮里塞：这根按钮只有 150 宽，塞进去必然省略
        成半句。原因交给居中提示去说，按钮只回答"成没成、下一步点什么"。
        """
        self._state = STATE_FAILED
        self._hold_result()
        self.update()

    def state(self) -> str:
        return self._state

    def lines(self) -> list[str]:
        """当前画出来的文字（验证脚本与悬停提示都读它）。"""
        return [text for text, _, _, _ in self._lines()]

    def set_live(self, live: bool) -> None:
        live = bool(live)
        if live == self._live:
            return
        self._live = live
        self.setCursor(Qt.PointingHandCursor if live else Qt.ArrowCursor)
        self.update()

    # -- 内部 ------------------------------------------------------------
    def _tip(self) -> str:
        if not self._live:
            return "中枢网关离线，取不到登录码"
        return (
            "打开极客版并把一枚新登录码复制到剪贴板。\n"
            "登录码只能用一次；取新码会让旧的立即失效。"
        )

    def _can_click(self) -> bool:
        return self._live and not self._busy

    def _hold_result(self) -> None:
        """每次落到结果态都**重新**起表：连点两次成功，第二个码也该看满 30 秒。"""
        self._revert.start(RESULT_MS)

    def _revert_idle(self) -> None:
        """结果态到点，退回常态。

        不碰剪贴板：码还留在那儿，用户随时能粘贴；按钮只是不再举着它。
        """
        if self._state == STATE_IDLE:
            return
        self._state = STATE_IDLE
        self._code = ""
        self.update()

    def _tone(self) -> str:
        """这根按钮当前该用哪支色。

        常态是主题强调色（蓝）；成功后转薄荷绿、失败后转警告红——和界面里
        "通电/生效"与"出错"已有的两套语义对齐（见 ``theme.TOGGLE_ON`` /
        ``theme.DANGER``）。边框是这块矩形唯一的分界线，换色就是换状态。
        """
        if self._state == STATE_FAILED:
            return theme.DANGER
        if self._state == STATE_DONE:
            return theme.ACCENT_GREEN
        return _tint(self._p)

    def _fill(self) -> str:
        pal = self._p
        if self._press:
            return theme.blend(pal.card, self._tone(), BTN_PRESS)
        if self._hover:
            return theme.blend(pal.card, self._tone(), BTN_HOVER)
        return pal.card

    def _lines(self) -> list[tuple[str, int, str, QFont.Weight]]:
        """(文字, 字号, 颜色, 字重)。**四种状态的文案只有这一个来源。**

        成功态特意排成三行：动作说明（小）、登录码（大）、下一步（小）。用户要的
        是「已复制 登录码：xxxxxx」+「重新获取并打开极客版」，但 150 宽的按钮里
        14px 装不下那串码（会省略成 ``已复制 登录码：12…``，正好把最要紧的数字切掉）。
        拆成上下两行后码可以放大到 18px，反而更醒目。
        """
        pal = self._p
        if self._busy:
            return [("正在取码…", 13, pal.text, QFont.Medium)]
        if self._state == STATE_DONE:
            return [
                ("已复制 登录码", 11, pal.text_soft, QFont.Normal),
                (self._code, 18, pal.text, QFont.Medium),
                ("重新获取并打开极客版", 12, pal.text_soft, QFont.Normal),
            ]
        if self._state == STATE_FAILED:
            return [
                ("获取登录码失败", 14, theme.DANGER, QFont.Medium),
                ("重新获取并打开极客版", 12, pal.text_soft, QFont.Normal),
            ]
        # 文案两行：第一行是动作（大一号、正文色），第二行是副作用（小一号、弱色）
        # ——"复制登录码"是点了才会发生的事，不该和动作同等重。
        return [
            ("打开极客版", 14, pal.text, QFont.Medium),
            ("自动复制登录码", 12, pal.text_soft, QFont.Normal),
        ]

    # -- 绘制 ------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        pal = self._p
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self._live:
            # 与外壳同一条规则：离线整卡压到 45%。子控件不会被父控件的
            # QPainter opacity 带下去，这里必须自己压一次，否则离线时按钮
            # 反而成了整张卡里最实的一块。
            p.setOpacity(0.45)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        tone = self._tone()

        p.setPen(QPen(QColor(tone), BTN_W))
        p.setBrush(QColor(self._fill()))
        p.drawRoundedRect(rect, BTN_R, BTN_R)

        lines = self._lines()
        if not self._live:
            lines = [(text, size, pal.text_mute, weight) for text, size, _, weight in lines]

        heights = [self._line_h(size) for _, size, _, _ in lines]
        total = sum(heights) + BTN_LINE_GAP * (len(lines) - 1)
        y = rect.center().y() - total / 2
        for (text, size, color, weight), h in zip(lines, heights, strict=True):
            font = QFont(self.font())
            font.setPixelSize(size)
            font.setWeight(weight)
            p.setFont(font)
            p.setPen(QColor(color))
            fm = QFontMetrics(font)
            avail = max(0, int(rect.width()) - 16)
            p.drawText(
                QRectF(rect.x() + 8, y, avail, h),
                int(Qt.AlignHCenter | Qt.AlignVCenter),
                fm.elidedText(text, Qt.ElideRight, avail),
            )
            y += h + BTN_LINE_GAP
        p.end()

    @staticmethod
    def _line_h(size: int) -> float:
        # 字号 + 6 是这两行在 78 高内容带里的呼吸量，14/12 两行加起来 44，
        # 上下各留 17，看着不挤
        return size + 6

    # -- 交互 ------------------------------------------------------------
    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self._press = False
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self._can_click():
            self._press = True
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        was_press = self._press
        self._press = False
        self.update()
        if (
            was_press
            and event.button() == Qt.LeftButton
            and self._can_click()
            and self.rect().contains(event.position().toPoint())
        ):
            self.clicked.emit()
            return
        super().mouseReleaseEvent(event)


class GeekCard(DeviceCard):
    """中枢网关的卡片。非「极客版入口」样式时行为与 ``DeviceCard`` 完全一致。"""

    #: 用户点了右半的按钮。取码、复制、开浏览器都由 ``MainWindow`` 串
    geek_requested = Signal(str)  # did

    def __init__(
        self,
        state: DeviceState,
        icons: IconProvider,
        palette: theme.Palette,
        style: CardStyle | None = None,
    ) -> None:
        # 父类构造里会调 ``_build_body``，字段必须先有
        self._geek: GeekButton | None = None
        super().__init__(state, icons, palette, style)
        self._sync_button()

    # -- 内容带 ----------------------------------------------------------
    def _build_body(self) -> None:
        if self._style.body != BODY_GEEK:
            self._drop_button()
            super()._build_body()
            return
        if self._body is not None:
            old = self._body
            old.setParent(None)
            old.deleteLater()
            self._body = None
        if self._geek is None:
            self._geek = GeekButton(self._p, self)
            self._geek.clicked.connect(self._on_click)
        self._geek.setGeometry(self._geek_rect())
        self._geek.show()

    def _drop_button(self) -> None:
        if self._geek is None:
            return
        self._geek.setParent(None)
        self._geek.deleteLater()
        self._geek = None

    @property
    def button(self) -> GeekButton | None:
        return self._geek

    # -- 几何 ------------------------------------------------------------
    #: 左半产品图与右半按钮之间的留白。12 让两者之间有一条能喘气的缝，
    #: 同时不至于把按钮挤窄（2×1 的内容带只有 312 宽，各分 150）
    GAP = 12

    def _art_rect(self) -> QRect:
        area = super()._body_rect()
        left = (area.width() - self.GAP) // 2
        return QRect(area.x(), area.y(), left, area.height())

    def _geek_rect(self) -> QRect:
        area = super()._body_rect()
        left = (area.width() - self.GAP) // 2
        return QRect(
            area.x() + left + self.GAP,
            area.y(),
            area.width() - left - self.GAP,
            area.height(),
        )

    def _draws_art(self) -> bool:
        # 极客版样式：右半让给按钮，左半仍然画产品图；其它样式交回父类
        # （标准样式 = 没 body = 照常画产品图）
        if self._style.body == BODY_GEEK:
            return not self.state.card.gangs
        return super()._draws_art()

    # -- 状态 ------------------------------------------------------------
    def _sync_button(self) -> None:
        if self._geek is not None:
            self._geek.set_live(bool(self.state.online))

    def set_busy(self, busy: bool) -> None:
        """取码往返期间置忙（由 ``MainWindow`` 在发请求前后各调一次）。"""
        if self._geek is not None:
            self._geek.set_busy(busy)

    def set_result(self, code: str) -> None:
        """取码成功：把这串码直接印在按钮上（由 ``WallView._on_geek_done`` 调）。"""
        if self._geek is not None:
            self._geek.set_result(code)

    def set_error(self) -> None:
        """取码失败：按钮转成「获取登录码失败 + 重新获取」。"""
        if self._geek is not None:
            self._geek.set_error()

    def _on_click(self) -> None:
        self.geek_requested.emit(self.state.did)

    # -- 外壳联动 --------------------------------------------------------
    def set_style(self, style: CardStyle) -> None:
        if style == self._style:
            return
        super().set_style(style)
        if self._geek is not None:
            self._geek.setGeometry(self._geek_rect())
        self._sync_button()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._geek is not None:
            self._geek.setGeometry(self._geek_rect())

    def set_palette(self, palette: theme.Palette) -> None:
        super().set_palette(palette)
        if self._geek is not None:
            self._geek.set_palette(palette)

    def refresh(self, state: DeviceState | None = None) -> None:
        super().refresh(state)
        if self._geek is not None:
            self._geek.setGeometry(self._geek_rect())
        self._sync_button()

    def set_drag_placeholder(self, on: bool) -> None:
        super().set_drag_placeholder(on)
        if self._geek is not None:
            self._geek.setVisible(not on)


__all__ = [
    "RESULT_MS",
    "STATE_DONE",
    "STATE_FAILED",
    "STATE_IDLE",
    "GeekButton",
    "GeekCard",
]
