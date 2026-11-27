# SPDX-License-Identifier: GPL-3.0-or-later
"""桌面挂件上的紧凑卡片：158×56，一行名字 + 一行副标题 + 一个电源键。

它不是 ``ui/device_card.py`` 的缩小版——主界面的卡片有产品图、多键按键、
卡片样式，挂件只需要"看一眼 + 点一下"两件事。共用的是文案（``summary_line`` /
``gang_summary``）和电源键的命中语义（"按下在控件上就不算点击"），绘制全部重写：
56 px 高的小卡片塞不下 32 px 电源 + 内容带。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from . import theme
from .widgets import draw_text
from ..core.catalog import gang_summary, summary_line
from ..core.store import DeviceState

#: 卡片尺寸。158 = 主界面标准卡的一半宽再收一点，56 是"标题 20 + 副标题 16"的最小舒适高度
CARD_W = 158
CARD_H = 56
#: 卡片内部留白
PAD = 10
#: 电源键直径（比主界面的 32 小一号，否则 56 高塞不下两行字）
POWER_D = 26
#: 场景卡右箭头（›）的半宽 / 半高，按圆直径的比例给——换图标尺寸时字形跟着缩放。
#: 两个数只影响"笔画的张开程度"，位置由圆心上镜像对称保证，见 ``_paint_arrow``。
CHEVRON_HW = 0.138
CHEVRON_HH = 0.20
RADIUS = 10
#: 场景卡执行结果在副标题上停留多久（之后换回家庭名）
FLASH_MS = 2600


def card_subtitle(st: DeviceState) -> str:
    """紧凑卡的副标题。和主界面 ``DeviceCard._subtitle`` 同一套优先级：

    离线 → 「房间 · 已离线」；没有可控规格 → 「不支持控制」；多键 → 路数摘要；
    其余 → 「房间 · 读数」；一个读数都没有就退回开关状态。
    """
    room = st.room
    if not st.online:
        return f"{room} · 已离线" if room else "已离线"
    if not st.node.spec_type:
        return "不支持控制"
    if st.card.gangs:
        text = gang_summary(st.card.gangs, st.values)
    else:
        text = summary_line(st.card, st.values)
    if not text:
        power = st.power
        if power is None:
            return ""
        text = "已开启" if power else "已关闭"
    return f"{room} · {text}" if room else text


def power_available(st: DeviceState) -> bool:
    """这张卡画不画电源键。

    多键面板不画：它的开关在每一路上，一张卡一个总开关会和按键打架；传感器、
    网关这类没有 ``power`` 控件的也不画。
    """
    return bool(st.online and not st.card.gangs and st.card.control("power"))


class WidgetCard(QWidget):
    """挂件卡片。尺寸固定，位置由 ``DesktopWidget`` 摆。"""

    open_requested = Signal(str)  # did：点了卡片空白
    toggled = Signal(str, int, int)  # did, siid, piid：点了电源
    menu_requested = Signal(str, QPoint)  # did, 全局坐标

    def __init__(self, state: DeviceState, palette: theme.Palette, parent=None) -> None:
        super().__init__(parent)
        self.state = state
        self._pal = palette
        self._hover = False
        self._press_power = False
        self._press_pos: QPoint | None = None
        self.setFixedSize(CARD_W, CARD_H)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)

    # -- 外部接口 --------------------------------------------------------
    def set_palette(self, palette: theme.Palette) -> None:
        self._pal = palette
        self.update()

    def set_state(self, state: DeviceState) -> None:
        self.state = state
        # 离线时电源键不再可点，别留着按下的高亮
        if not power_available(state):
            self._press_power = False
        self.setCursor(Qt.PointingHandCursor if state.online else Qt.ArrowCursor)
        self.update()

    # -- 布局 ------------------------------------------------------------
    def _power_rect(self) -> QRect:
        return QRect(CARD_W - PAD - POWER_D, (CARD_H - POWER_D) // 2, POWER_D, POWER_D)

    def _power_hit(self, pos: QPoint) -> bool:
        return power_available(self.state) and self._power_rect().contains(pos)

    # -- 绘制 ------------------------------------------------------------
    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt 命名)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        pal = self._pal
        st = self.state
        # 和主窗口的 DeviceCard 一致：**不画描边**，只靠填充色与面板底色的明度差
        # 分出层次（悬停挪一档明度）。早先这里套了一圈 pal.border，在浅色底板上
        # 会读成一个"卡片框"，和主界面的无边框卡片不是一套语言（用户 2026-11-21 指出）。
        base = pal.card
        if self._hover:
            base = theme.card_hover_color(base, pal)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.card_fill(base, pal))
        p.drawRoundedRect(QRectF(0, 0, CARD_W, CARD_H), RADIUS, RADIUS)

        if not st.online:
            # 离线整卡压暗：比任何文字提示都先被看见
            p.setOpacity(0.55)

        has_power = power_available(st)
        title_w = CARD_W - PAD * 2 - (POWER_D + 8 if has_power else 0)
        draw_text(
            p,
            QRect(PAD, PAD, title_w, 20),
            st.name,
            size=13,
            color=pal.text,
            weight=QFont.DemiBold,
        )
        draw_text(
            p,
            QRect(PAD, PAD + 22, CARD_W - PAD * 2, 16),
            card_subtitle(st),
            size=11,
            color=pal.text_soft,
        )
        if has_power:
            self._paint_power(p)
        p.end()

    def _paint_power(self, p: QPainter) -> None:
        """电源键：和主界面 ``DeviceCard._paint_power`` 同一套画法。

        开 = 实心主色圆 + 白字形，关 = 一档中性圆 + ``text_soft`` 字形。字形本身
        也是同一套比例（缺口 76° 朝上、竖线从 1.18r 到圆心）——紧凑卡只是把直径
        从 32 缩到 26，不能自己发明一套更小的圈。
        """
        pal = self._pal
        on = bool(self.state.power)
        rect = QRectF(self._power_rect())
        p.setPen(Qt.NoPen)
        if on:
            p.setBrush(QBrush(QColor(pal.accent)))
        else:
            p.setBrush(QBrush(QColor(theme.blend(pal.card, pal.text, 0.08))))
        p.drawEllipse(rect)
        c = rect.center()
        r = rect.width() * 0.21
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor("#FFFFFF" if on else pal.text_soft), 1.7,
                      Qt.SolidLine, Qt.RoundCap))
        p.drawArc(QRectF(c.x() - r, c.y() - r, r * 2, r * 2), 128 * 16, 284 * 16)
        p.drawLine(QPointF(c.x(), c.y() - r * 1.18), QPointF(c.x(), c.y()))

    # -- 交互 ------------------------------------------------------------
    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        self._press_pos = event.position().toPoint()
        self._press_power = self._power_hit(self._press_pos)
        if self._press_power:
            self.update()
            event.accept()
            return
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton or self._press_pos is None:
            super().mouseReleaseEvent(event)
            return
        pos = event.position().toPoint()
        press = self._press_pos
        was_power = self._press_power
        self._press_pos = None
        self._press_power = False
        self.update()
        if was_power:
            # "按下在控件上就不算点击"：松手必须还在同一个电源键上
            if self._power_hit(pos):
                control = self.state.card.control("power")
                if control is not None and control.prop is not None:
                    self.toggled.emit(
                        self.state.did, control.prop.siid, control.prop.piid
                    )
            event.accept()
            return
        if (pos - press).manhattanLength() <= 4:
            self.open_requested.emit(self.state.did)
        event.accept()

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        self.menu_requested.emit(self.state.did, event.globalPos())
        event.accept()


class SceneCard(QWidget):
    """挂件上的快捷场景卡：158×56，标题是场景名，右侧圆里画一个右箭头。

    和设备卡的差别只在两处：场景没有在线 / 开关这些状态（它是一次性动作），
    右边那颗圆里的字形从电源换成右箭头（用户 2026-11-27 定）。点击**整张卡**
    就执行——场景卡摆上挂件就是为了点，再要求"必须点中箭头"只会让人点空。

    执行结果就地显示在副标题上（``flash()``，2.6 s 后换回家庭名）：用户在看着
    挂件，不需要再弹一条系统通知，也不必把主窗口拉出来。
    """

    run_requested = Signal()
    menu_requested = Signal(QPoint)  # 全局坐标

    def __init__(self, name: str, home_label: str, palette: theme.Palette,
                 parent=None) -> None:
        super().__init__(parent)
        self.name = name
        self.home_label = home_label
        self._pal = palette
        self._hover = False
        self._pressed = False
        self._press_pos: QPoint | None = None
        self._flash = ""
        self._flash_ok: bool | None = True
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._end_flash)
        self.setFixedSize(CARD_W, CARD_H)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)

    # -- 外部接口 --------------------------------------------------------
    def set_palette(self, palette: theme.Palette) -> None:
        self._pal = palette
        self.update()

    def set_text(self, name: str, home_label: str) -> None:
        self.name = name
        self.home_label = home_label
        self.update()

    def flash(self, text: str, ok: bool | None = True) -> None:
        """把结果临时顶到副标题上；下一次执行会重新计时。

        ``ok=None`` 表示"进行中"——用中性色，不要一上来就染成成功色。
        """
        self._flash = text
        self._flash_ok = ok
        self._flash_timer.start(FLASH_MS)
        self.update()

    def _end_flash(self) -> None:
        self._flash = ""
        self.update()

    # -- 绘制 ------------------------------------------------------------
    def _arrow_rect(self) -> QRect:
        return QRect(CARD_W - PAD - POWER_D, (CARD_H - POWER_D) // 2, POWER_D, POWER_D)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        pal = self._pal
        base = pal.card
        if self._hover or self._pressed:
            base = theme.card_hover_color(base, pal)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.card_fill(base, pal))
        p.drawRoundedRect(QRectF(0, 0, CARD_W, CARD_H), RADIUS, RADIUS)

        title_w = CARD_W - PAD * 2 - (POWER_D + 8)
        draw_text(
            p,
            QRect(PAD, PAD, title_w, 20),
            self.name,
            size=13,
            color=pal.text,
            weight=QFont.DemiBold,
        )
        if self._flash:
            sub = self._flash
            sub_color = (
                pal.text_soft if self._flash_ok is None
                else pal.accent if self._flash_ok else theme.DANGER
            )
        else:
            sub, sub_color = self.home_label or "快捷场景", pal.text_soft
        draw_text(
            p,
            QRect(PAD, PAD + 22, CARD_W - PAD * 2, 16),
            sub,
            size=11,
            color=sub_color,
        )
        self._paint_arrow(p)
        p.end()

    def _paint_arrow(self, p: QPainter) -> None:
        """右箭头（›）：一个中性圆 + 一笔折线，圆心与设备卡的电源键完全对齐。

        圆底只在悬停 / 按下时上主题色——常驻主色会让整排卡片都在喊"点我"，
        和设备卡的关态保持同一档安静。

        字形**以圆心为镜像点对称**（尾端在 ``±CHEVRON_HW``、尖端在 ``+CHEVRON_HW``）。
        早先那版是「尖端 +0.7dx、尾端 -dx」（dx 是按圆宽算的），画出来更像一支箭，
        但墨迹包围盒和墨量重心都落在圆心左边约 0.6 px —— 26 px 的圆里看得出来，
        用户 2026-11-27 报"右箭头在圆里有点偏左"。对称之后两者都正好在圆心上，
        不需要再加光学补偿；要微调只改下面两个常量。
        """
        pal = self._pal
        hot = self._hover or self._pressed
        rect = QRectF(self._arrow_rect())
        p.setPen(Qt.NoPen)
        p.setBrush(
            QBrush(QColor(pal.accent)) if hot
            else QBrush(QColor(theme.blend(pal.card, pal.text, 0.08)))
        )
        p.drawEllipse(rect)
        c = rect.center()
        ink = QColor("#FFFFFF" if hot else pal.text_soft)
        p.setPen(QPen(ink, 1.7, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        hw = rect.width() * CHEVRON_HW
        hh = rect.width() * CHEVRON_HH
        apex = QPointF(c.x() + hw, c.y())
        p.drawLine(QPointF(c.x() - hw, c.y() - hh), apex)
        p.drawLine(QPointF(c.x() - hw, c.y() + hh), apex)

    # -- 交互 ------------------------------------------------------------
    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        self._press_pos = event.position().toPoint()
        self._pressed = True
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton or self._press_pos is None:
            super().mouseReleaseEvent(event)
            return
        pos = event.position().toPoint()
        press = self._press_pos
        self._press_pos = None
        self._pressed = False
        self.update()
        if (pos - press).manhattanLength() <= 4 and self.rect().contains(pos):
            self.run_requested.emit()
        event.accept()

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        self.menu_requested.emit(event.globalPos())
        event.accept()
