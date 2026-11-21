# SPDX-License-Identifier: GPL-3.0-or-later
"""桌面挂件：主窗口之外的常驻小面板，贴在桌面上显示几台设备的状态。

窗口形态和三档层级（贴桌面 / 普通 / 置顶）的取舍见
``docs/reference/desktop-widget.md``。这里是 P0 的实现，刻意保持小：

- 头部只有三个按钮：管理（齿轮，直接打开管理窗）、图层、关闭；
- 设备由独立管理窗（``widget_manager``）按宫格槽位增删 / 拖动换位，空槽留空；
  主界面卡片右键不再提供入口；
- 不画半透明（分层窗口挂进 DefView 整窗不渲染，见 ``desktop_zorder`` 的说明）；
- 卡片尺寸固定 158×56，行数由布局决定，超出屏高就纵向滚动，不缩小卡片。

一句话原则：**挂件的每个像素都是只读的**——除了点电源键（走 ``Store``）和拖动
自己的窗口，它不碰桌面、不碰 DefView。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import math
from typing import Iterable

from PySide6.QtCore import (
    QEvent,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QToolTip, QWidget

from . import theme
from ..core.poll_demand import PollDemandBoard
from ..core.prefs import (
    WIDGET_LAYER_DEFAULT,
    WIDGET_LAYERS,
)
from ..core.store import DeviceState, Store
from . import desktop_zorder as zorder
from .desktop_card import CARD_H, CARD_W, WidgetCard
from .menu import Menu, add_choice, submenu
from .widgets import draw_text

HEADER_H = 30
PANEL_PAD = 10
GAP = 6
BOTTOM_H = 20
RADIUS = 14
BTN = 20
BTN_GAP = 4
#: 面板的绝对最小宽。单列内容只要 158+20=178，以前写 320 是因为空态文案要放得下；
#: 现在空态单独抬到 260，1 列时面板就能收窄到 200（用户 2026-11-21 要求）
MIN_W = 200
#: 离屏幕边缘的默认留白
SCREEN_MARGIN = 24
#: 屏高再紧也要给任务栏 / 其他窗口留的余地
SCREEN_RESERVE = 80
#: 拖动开始前允许的手抖，也是"点击"与"拖动"的分界
DRAG_SLOP = 6
#: 挂件**可见时**的轮询需求：和前台主窗口同档 3 s。
#:
#: 只有这一档。挂件没有"低一档"的间隔——收藏起来（``hide()``）就整条撤销，
#: 落回主窗口那条（托盘 / 最小化时 15 s），见 ``hideEvent``。
#: 将来的折叠态（P1，目前搁置）也**不给独立档**：收起时登记 15 s 那一档（和托盘同频），
#: 展开那一下补问一轮即可——用户 2026-11-21 定的口径，别再加 ``POLL_COLLAPSED_MS``。
POLL_EXPANDED_MS = 3000
#: 常规看门狗节拍：explorer 被杀、窗口被一起销毁，最多 2 s 发现并重建
WATCHDOG_MS = 2000
#: 降级成普通档后的重试节拍：shell 一回来就贴回桌面，别让用户等满 2 s
WATCHDOG_RETRY_MS = 500

#: 头部按钮的名字（从右往左排）：× / 图层 / 管理 / 打开主窗口
BTN_CLOSE = "close"
BTN_MANAGE = "manage"
BTN_LAYER = "layer"
BTN_OPEN = "open"

_BUTTON_TIPS = {
    BTN_CLOSE: "收起挂件",
    BTN_LAYER: "切换层级：贴桌面 / 普通 / 置顶",
    BTN_MANAGE: "管理挂件",
    BTN_OPEN: "打开主窗口",
}
_BUTTON_ORDER = (BTN_OPEN, BTN_LAYER, BTN_MANAGE, BTN_CLOSE)


class DesktopWidget(QWidget):
    """桌面挂件窗口。由 ``MainWindow`` 持有；被 explorer 杀掉时发 ``rebuild_needed``。"""

    open_device_requested = Signal(str)
    toggle_requested = Signal(str, int, int)
    remove_device_requested = Signal(str)
    open_consumables_requested = Signal()
    open_main_requested = Signal()
    manage_requested = Signal()
    #: 右键菜单里的「退出程序」。托盘挂不上时，这是用户唯一能结束进程的入口
    #: （2026-11-21：关窗改成收进挂件之后必须有这个口，否则只能去任务管理器）
    quit_requested = Signal()
    rebuild_needed = Signal()
    #: 显示 / 收起时发一次，参数是当前的可见状态；左栏「挂件」入口跟着它变亮
    visibility_changed = Signal(bool)

    def __init__(
        self,
        store: Store,
        prefs,
        palette: theme.Palette,
        poll: PollDemandBoard,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.prefs = prefs
        self.poll = poll
        self._pal = palette
        self._layer = WIDGET_LAYER_DEFAULT
        self._cards: dict[str, WidgetCard] = {}
        self._order: list[str] = []
        self._scroll = 0
        self._badge_warn = 0
        self._badge_crit = 0
        self._hover_btn = ""
        self._active_btn = ""
        self._drag_origin: QPoint | None = None
        self._drag_start: QPoint | None = None
        self._dragging = False
        self._empty_btn = QRect()

        self.setWindowTitle("桌面挂件")
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.NoFocus)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self.save_geometry)

        self._watchdog = QTimer(self)
        self._watchdog.setInterval(WATCHDOG_MS)
        self._watchdog.timeout.connect(self._tick_watchdog)
        #: 想贴桌面但当时挂不上（explorer 还没起来）：普通档显示、后台重试
        self._retry_desktop = False

        self.rebuild()
        self._restore_geometry()
        app = QApplication.instance()
        if app is not None:
            app.screenAdded.connect(lambda _s: self._clamp_geometry())
            app.screenRemoved.connect(lambda _s: self._clamp_geometry())
            app.primaryScreenChanged.connect(lambda _s: self._clamp_geometry())

    # -- 状态 ------------------------------------------------------------
    @property
    def layer(self) -> str:
        return self._layer

    @property
    def columns(self) -> int:
        return self.prefs.widget_cols

    @property
    def rows(self) -> int:
        return self.prefs.widget_rows

    # -- 数据 ------------------------------------------------------------
    def _slot_dids(self) -> list[str]:
        """当前槽位表（长度 = 行×列，空串 = 空槽）。"""
        return list(self.prefs.widget_slots)

    def _live_states(self) -> list[DeviceState]:
        states: list[DeviceState] = []
        for did in self.prefs.widget_dids():
            st = self.store.devices.get(did)
            if st is not None:
                states.append(st)
        return states

    def rebuild(self) -> None:
        """整块重建（槽位表 / 拓扑变了才调）。单台状态变化走 ``update_states``。

        卡片**按下标**存：槽位是固定的，中间空着的那格就该空着，不能像列表那样
        把后面的设备挤上来。
        """
        for card in self._cards.values():
            card.hide()
            card.deleteLater()
        self._cards.clear()
        self._order = self._slot_dids()
        for index, did in enumerate(self._order):
            if not did:
                continue
            st = self.store.devices.get(did)
            if st is None:
                continue
            card = WidgetCard(st, self._pal, self)
            # 卡片是独立子控件，按下事件不会冒泡到挂件；普通档要「点哪儿都浮到最前」，
            # 所以在事件过滤器里补一次（见 _bring_to_front）。
            card.installEventFilter(self)
            card.open_requested.connect(self.open_device_requested)
            card.toggled.connect(self.toggle_requested)
            card.menu_requested.connect(self._on_card_menu)
            card.show()
            self._cards[index] = card
        self._scroll = 0
        self.resize_for_content()
        self._place_cards()
        self.update()

    def update_states(self, dids: Iterable[str]) -> None:
        changed = False
        wanted = set(dids)
        for index, card in self._cards.items():
            if self._order[index] not in wanted:
                continue
            st = self.store.devices.get(self._order[index])
            if st is not None:
                card.set_state(st)
                changed = True
        if changed:
            self.update()

    def set_palette(self, palette: theme.Palette) -> None:
        self._pal = palette
        for card in self._cards.values():
            card.set_palette(palette)
        self.update()

    def set_badges(self, warn: int, crit: int) -> None:
        """耗材圆点。由主窗口转发 ``consumables_page.counts_changed``。

        这两个数是**全部家庭的合计**（2026-11-21 起耗材页跨家庭）：挂件本身跨家庭，
        页脚左边那句「N 台设备 · M 台离线」也是全量，两边口径一致才对得上。
        不要把这里改成"当前家庭"——那会让同一个页脚里出现两种统计范围。
        """
        warn, crit = int(warn), int(crit)
        if (warn, crit) != (self._badge_warn, self._badge_crit):
            self._badge_warn, self._badge_crit = warn, crit
            self.update()

    def refresh_geometry(self) -> None:
        """列数变了：重算尺寸并重新摆卡片。"""
        self.resize_for_content()
        self._place_cards()
        self._schedule_save()

    # -- 几何 ------------------------------------------------------------
    def _max_rows(self) -> int:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return 8
        avail = screen.availableGeometry()
        per = CARD_H + GAP
        chrome = HEADER_H + GAP + GAP + BOTTOM_H + PANEL_PAD
        return max(1, (avail.height() - SCREEN_RESERVE - chrome + GAP) // per + 1)

    def _panel_width(self) -> int:
        cols = self.columns
        content = CARD_W * cols + GAP * (cols - 1) + PANEL_PAD * 2
        # 空态那句「点右上角齿轮，添加要显示的设备」一行放得下要 ~250 px，
        # 有卡片时跟内容走（1 列 178 → MIN_W 200）
        floor = MIN_W if self._cards else max(MIN_W, 260)
        return max(floor, content)

    def _rows(self) -> int:
        """行数由布局定，不随设备数变——空槽就是空着的格子。"""
        return self.rows

    def _panel_height(self) -> int:
        rows = min(self._rows(), self._max_rows())
        per = CARD_H + GAP
        content = rows * per - GAP
        chrome = HEADER_H + GAP + GAP + BOTTOM_H + PANEL_PAD
        return chrome + content

    def resize_for_content(self) -> None:
        self.resize(self._panel_width(), self._panel_height())
        self._apply_region()

    def _content_top(self) -> int:
        return HEADER_H + GAP

    def _viewport_rows(self) -> int:
        rows = max(1, (self.height() - self._content_top() - GAP - BOTTOM_H - PANEL_PAD + GAP))
        return max(1, rows // (CARD_H + GAP))

    def _max_scroll(self) -> int:
        return max(0, self._rows() - self._viewport_rows())

    def _place_cards(self) -> None:
        cols = self.columns
        top = self._content_top()
        self._scroll = max(0, min(self._scroll, self._max_scroll()))
        view = self._viewport_rows()
        for index, card in self._cards.items():
            row, col = divmod(index, cols)
            rel = row - self._scroll
            visible = 0 <= rel < view
            card.setVisible(visible)
            if visible:
                x = PANEL_PAD + col * (CARD_W + GAP)
                y = top + rel * (CARD_H + GAP)
                card.move(x, y)

    def _restore_geometry(self) -> None:
        self.resize_for_content()
        geom = list(self.prefs.widget_geom)
        rect: QRect | None = None
        if len(geom) == 4:
            candidate = QRect(geom[0], geom[1], self.width(), self.height())
            if self._screen_for(candidate) is not None:
                rect = candidate
        if rect is None:
            screen = QApplication.primaryScreen()
            avail = screen.availableGeometry() if screen else QRect(0, 0, 1280, 720)
            rect = QRect(
                avail.right() - self.width() - SCREEN_MARGIN,
                avail.top() + SCREEN_MARGIN,
                self.width(),
                self.height(),
            )
        self.setGeometry(rect)
        self._clamp_geometry()

    @staticmethod
    def _screen_for(rect: QRect):
        center = rect.center()
        for screen in QApplication.screens():
            if screen.availableGeometry().contains(center):
                return screen
        return None

    def _clamp_geometry(self) -> None:
        """屏幕拔了 / 分辨率变了：把窗口夹回某个屏幕里，别让它漂到看不见的地方。"""
        if not self.isVisible() and not self.prefs.widget_geom:
            return
        rect = self.frameGeometry()
        screen = self._screen_for(rect)
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        w = min(rect.width(), avail.width())
        h = min(rect.height(), avail.height())
        x = min(max(rect.x(), avail.left()), avail.right() - w)
        y = min(max(rect.y(), avail.top()), avail.bottom() - h)
        if QRect(x, y, w, h) != rect:
            self.setGeometry(x, y, w, h)
            self._place_cards()

    def save_geometry(self) -> None:
        if not self.isVisible():
            return
        if self._layer == "desktop":
            # 挂进 DefView 后自己就是子窗口，位置仍按屏幕坐标记（DefView 通常
            # 覆盖整个虚拟桌面，原点在 0,0）
            geo = self.geometry()
        else:
            geo = self.geometry()
        if geo.width() <= 0 or geo.height() <= 0:
            return
        self.prefs.set_widget_geom([geo.x(), geo.y(), geo.width(), geo.height()])

    def _schedule_save(self) -> None:
        self._save_timer.start()

    # -- 层级 ------------------------------------------------------------
    def apply_layer(self, name: str | None = None, *, save: bool = True) -> bool:
        """切档。返回是否真的到了目标档；桌面档挂不上时**静默降级**普通。

        挂不上时记一个 ``_retry_desktop``：explorer 刚被杀、DefView 还没起来时
        重建出来的新窗口会落到普通档，得等 shell 回来再贴回去，否则用户看到的
        就是"重启 explorer 之后挂件再也回不到桌面"。
        """
        target = name or self.prefs.widget_layer
        if target not in WIDGET_LAYERS:
            target = WIDGET_LAYER_DEFAULT
        hwnd = int(self.winId())
        if not zorder.is_window(hwnd):
            # 窗口还没建（show 之前）：只记偏好，show 之后再落
            self._layer = target
            return False
        self._layer = target
        self._retry_desktop = False
        if target == "desktop":
            ok = zorder.attach_desktop(hwnd)
            if ok:
                zorder.set_topmost(hwnd, False)
            else:
                self._layer = "normal"
                self._retry_desktop = True
                zorder.detach_desktop(hwnd)
                zorder.set_topmost(hwnd, False)
        else:
            zorder.detach_desktop(hwnd)
            zorder.set_topmost(hwnd, target == "top")
        # 普通档要能被"点击 → 系统激活 → 提层"这条标准链路带上最前；桌面/置顶不抢
        # 前台（桌面档是 DefView 子窗、置顶档本来就最前），保持 NOACTIVATE。
        zorder.set_noactivate(hwnd, self._layer != "normal")
        if save and self._layer == target:
            self.prefs.set_widget_layer(target)
        if self._layer == "desktop":
            self._watchdog.start()
        elif self._retry_desktop:
            # 半秒看一次，shell 一回来就贴回去（不按 2 s 的常规节拍等）
            self._watchdog.start(WATCHDOG_RETRY_MS)
        else:
            self._watchdog.stop()
        self._apply_region()
        self.update()
        return self._layer == target

    def _tick_watchdog(self) -> None:
        if not self.isVisible():
            return
        if self._retry_desktop:
            # 普通档降级中：每次节拍都试试能不能贴回 DefView
            hwnd = int(self.winId())
            if not zorder.is_window(hwnd) or not zorder.defview():
                return
            if zorder.attach_desktop(hwnd):
                zorder.set_topmost(hwnd, False)
                self._layer = "desktop"
                self._retry_desktop = False
                self._watchdog.start()
                self._apply_region()
                self.update()
            return
        if self._layer != "desktop":
            return
        hwnd = int(self.winId())
        action = zorder.watchdog_action(
            layer=self._layer,
            is_window=zorder.is_window(hwnd),
            parent=zorder.get_parent(hwnd),
            defview=zorder.defview(),
        )
        if action == "rebuild":
            self._watchdog.stop()
            self.rebuild_needed.emit()
        elif action == "reattach":
            zorder.attach_desktop(hwnd)

    def _apply_region(self) -> None:
        hwnd = int(self.winId())
        if not hwnd:
            return
        dpr = self.devicePixelRatioF() or 1.0
        zorder.set_region(
            hwnd,
            round(self.width() * dpr),
            round(self.height() * dpr),
            round(RADIUS * dpr),
        )

    # -- 事件 ------------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802
        self._register_poll()
        self._apply_region()
        if self._layer == "desktop":
            self._watchdog.start()
        elif self._retry_desktop:
            self._watchdog.start(WATCHDOG_RETRY_MS)
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def hideEvent(self, event) -> None:  # noqa: N802
        self._watchdog.stop()
        self.poll.clear("widget")
        self.save_geometry()
        super().hideEvent(event)
        self.visibility_changed.emit(False)

    def resizeEvent(self, event) -> None:  # noqa: N802
        self._apply_region()
        super().resizeEvent(event)

    def _register_poll(self) -> None:
        if not self.isVisible():
            return
        self.poll.set("widget", POLL_EXPANDED_MS, "挂件展开")
        # 显示出来这一下补问一轮：上一轮可能是 15 s 档的相位，不能让用户先看到
        # 十几秒前的值。``Store.poll`` 自带 ``polling`` 闸门，刚跑过就跳过一次，
        # 所以这里的"补问"不会和 tick 叠成两个请求。
        self.store.poll()

    # -- 绘制 ------------------------------------------------------------
    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        pal = self._pal
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        p.setPen(QPen(QColor(pal.border), 1))
        # 底板要和主窗口那片"墙"同源：自定义底色优先，否则直接用预设的 surface。
        # 以前是 blend(card, bg, 0.30)——纯白卡往浅灰底上混，底板落在 L≈247，卡片 255，
        # 只差 8 级，卡片和底板糊成一片（用户 2026-11-21 指出主窗口对比明显、挂件不明显）。
        # surface 是主窗口 WindowFrame 亲自铺的那层（渐变档就是渐变），卡片压上去的
        # 明度差和主界面一致；深色档同理（底板暗、卡片亮一档）。
        if self.prefs.widget_bg:
            p.setBrush(QBrush(QColor(self.prefs.widget_bg)))
        else:
            p.setBrush(QBrush(pal.surface.brush(rect)))
        p.drawRoundedRect(rect, RADIUS, RADIUS)

        self._paint_header(p)
        if not self._cards:
            self._paint_empty(p)
        self._paint_footer(p)
        p.end()

    def _paint_header(self, p: QPainter) -> None:
        pal = self._pal
        draw_text(
            p,
            QRect(PANEL_PAD, 0, self.width() - PANEL_PAD * 2 - (BTN + BTN_GAP) * len(_BUTTON_ORDER),
                  HEADER_H),
            "桌面挂件",
            size=12,
            color=pal.text_soft,
            weight=QFont.DemiBold,
        )
        for name in _BUTTON_ORDER:
            rect = self._button_rect(name)
            if name == self._hover_btn or name == self._active_btn:
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(QColor(theme.alpha(pal.text_soft, 0.12))))
                p.drawRoundedRect(QRectF(rect), 6, 6)
            self._paint_button(p, name, rect)

    def _paint_button(self, p: QPainter, name: str, rect: QRect) -> None:
        pal = self._pal
        color = QColor(pal.text_soft if name != BTN_CLOSE else pal.text_mute)
        p.setPen(QPen(color, 1.4))
        p.setBrush(Qt.NoBrush)
        if name == BTN_CLOSE:
            cx, cy = rect.center().x() + 0.5, rect.center().y() + 0.5
            p.drawLine(QPoint(cx - 4, cy - 4), QPoint(cx + 4, cy + 4))
            p.drawLine(QPoint(cx - 4, cy + 4), QPoint(cx + 4, cy - 4))
        elif name == BTN_MANAGE:
            # 齿轮：管理挂件的入口。和左栏「设置」的齿轮同一套笔触，但这里是
            # **直接打开管理窗**，不弹二级菜单（用户 2026-11-20 定）。
            self._paint_gear_glyph(p, rect, color)
        elif name == BTN_OPEN:
            # 打开主窗口：一个窗口轮廓 + 右上角向外的小箭头（"弹出到桌面"）
            self._paint_open_glyph(p, rect, color)
        else:
            self._paint_layer_glyph(p, rect, color)

    @staticmethod
    def _paint_open_glyph(p: QPainter, rect: QRect, color: QColor) -> None:
        """打开主窗口：一个指向左上的箭头（↖）。

        早先是"方框 + 右上角外链箭头"，20 px 下和"全屏 / 放大"的图标撞脸；
        用户要的是"回到主窗口"，用纯箭头、不套方框，方向也反过来（左上 = 收回
        到应用里）。箭头头用两条短线画，20 px 下比实心三角更干净。
        """
        cx, cy = rect.center().x() + 0.5, rect.center().y() + 0.5
        p.setPen(QPen(color, 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        tip_x, tip_y = cx - 4.5, cy - 4.5
        p.drawLine(QPointF(cx + 4.0, cy + 4.0), QPointF(tip_x, tip_y))
        p.drawLine(QPointF(tip_x, tip_y), QPointF(tip_x, cy - 0.5))
        p.drawLine(QPointF(tip_x, tip_y), QPointF(cx - 0.5, tip_y))

    @staticmethod
    def _paint_gear_glyph(p: QPainter, rect: QRect, color: QColor) -> None:
        """管理按钮：齿轮。8 个齿用"外/内半径交替"的多边形画，中间留一个孔。

        早先用"细圆环 + 8 根细辐条"，20px 下看着像太阳；齿廓要粗、要贴边，
        才读得出是齿轮。
        """
        cx, cy = rect.center().x() + 0.5, rect.center().y() + 0.5
        teeth, r_out, r_base = 8, 6.2, 4.4
        path = QPainterPath()
        for i in range(teeth * 2):
            ang = -math.pi / 2 + i * math.pi / teeth
            rad = r_out if i % 2 == 0 else r_base
            pt = QPointF(cx + math.cos(ang) * rad, cy + math.sin(ang) * rad)
            if i == 0:
                path.moveTo(pt)
            else:
                path.lineTo(pt)
        path.closeSubpath()
        p.setPen(QPen(color, 1.3, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.drawEllipse(QPointF(cx, cy), 1.8, 1.8)

    def _paint_layer_glyph(self, p: QPainter, rect: QRect, color: QColor) -> None:
        """图层按钮：画当前档的隐喻（桌面轮廓 / 窗口 / 图钉）。自绘而不是图标文件，
        和托盘图标一个路子，主题换色时不用准备两套资源。"""
        cx, cy = rect.center().x() + 0.5, rect.center().y() + 0.5
        p.setPen(QPen(color, 1.3))
        if self._layer == "desktop":
            p.drawRect(QRectF(cx - 6, cy - 5, 12, 9))
            p.drawLine(QPoint(cx - 6, cy + 6), QPoint(cx + 6, cy + 6))
        elif self._layer == "top":
            # 图钉：一个圆头 + 一根针
            p.drawEllipse(QRectF(cx - 4, cy - 5, 8, 8))
            p.drawLine(QPoint(cx, cy + 3), QPoint(cx, cy + 7))
        else:
            p.drawRect(QRectF(cx - 6, cy - 5, 12, 10))
            p.drawLine(QPoint(cx - 6, cy - 2), QPoint(cx + 6, cy - 2))

    def _paint_footer(self, p: QPainter) -> None:
        pal = self._pal
        y = self.height() - BOTTOM_H - PANEL_PAD // 2
        offline = sum(1 for st in self._live_states() if not st.online)
        text = f"{len(self._cards)} 台设备" if self._cards else "还没有设备"
        if offline:
            text += f" · {offline} 台离线"
        draw_text(
            p,
            QRect(PANEL_PAD, y, self.width() - PANEL_PAD * 2, BOTTOM_H),
            text,
            size=10,
            color=pal.text_mute if not offline else pal.text_soft,
        )
        badges = self._footer_badge()
        if badges:
            self._paint_footer_badges(p, y, badges)

    def _paint_footer_badges(self, p: QPainter, y: int, badges: tuple[int, int]) -> None:
        """右下角「耗材：[轻] [重]」：和左栏耗材入口同一套圆点（灰 = 轻提醒，红 = 重提醒）。

        只画有计数的那个；两个都为 0 时整段不出现（``_footer_badge()`` 返回 None）。
        """
        pal = self._pal
        items = [
            (badges[0], pal.text_mute, pal.bg),
            (badges[1], theme.DANGER, "#FFFFFF"),
        ]
        items = [(count, fill, ink) for count, fill, ink in items if count > 0]
        if not items:
            return
        widths = [15 if count < 10 else 21 for count, _, _ in items]
        total = sum(widths) + 4 * (len(widths) - 1)
        label_w = 34
        right = self.width() - PANEL_PAD
        label = QRect(right - total - label_w - 6, y, label_w, BOTTOM_H)
        draw_text(p, label, "耗材", size=10, color=pal.text_mute,
                  align=Qt.AlignRight | Qt.AlignVCenter)
        cy = label.center().y()
        x = right - total
        for (count, fill, ink), wide in zip(items, widths):
            text = str(count) if count < 100 else "99+"
            pill = QRectF(x, cy - 7.5, wide, 15)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(QColor(fill)))
            p.drawRoundedRect(pill, 7.5, 7.5)
            f = QFont(p.font())
            f.setPixelSize(10)
            f.setWeight(QFont.DemiBold)
            p.setFont(f)
            p.setPen(QColor(ink))
            p.drawText(pill, Qt.AlignCenter, text)
            x += wide + 4

    def _footer_badge(self) -> tuple[int, int] | None:
        if self._badge_warn or self._badge_crit:
            return (self._badge_warn, self._badge_crit)
        return None

    def _paint_empty(self, p: QPainter) -> None:
        pal = self._pal
        top = self._content_top()
        area = QRect(PANEL_PAD, top, self.width() - PANEL_PAD * 2, CARD_H)
        draw_text(
            p,
            QRect(area.left(), area.top(), area.width(), 24),
            "点右上角齿轮，添加要显示的设备",
            size=11,
            color=pal.text_mute,
            align=Qt.AlignCenter,
        )
        self._empty_btn = QRect(area.center().x() - 48, area.top() + 32, 96, 24)
        p.setPen(QPen(QColor(pal.border), 1))
        p.setBrush(QBrush(QColor(theme.blend(pal.card, pal.text, 0.05))))
        p.drawRoundedRect(QRectF(self._empty_btn), 8, 8)
        draw_text(
            p,
            self._empty_btn,
            "打开主界面",
            size=11,
            color=pal.text_soft,
            align=Qt.AlignCenter,
        )

    # -- 头部按钮 --------------------------------------------------------
    def _button_rect(self, name: str) -> QRect:
        # _BUTTON_ORDER 是"从左往右"的阅读顺序；x 从右侧往回数，所以要把下标翻过来，
        # 保证 × 永远贴右上角（index 0 = 最右）
        index = len(_BUTTON_ORDER) - 1 - _BUTTON_ORDER.index(name)
        x = self.width() - PANEL_PAD - BTN - index * (BTN + BTN_GAP)
        return QRect(x, (HEADER_H - BTN) // 2, BTN, BTN)

    def _button_at(self, pos: QPoint) -> str:
        for name in _BUTTON_ORDER:
            if self._button_rect(name).contains(pos):
                return name
        return ""

    def _activate_button(self, name: str) -> None:
        if name == BTN_CLOSE:
            self.hide()
        elif name == BTN_MANAGE:
            self.manage_requested.emit()
        elif name == BTN_OPEN:
            self.open_main_requested.emit()
        else:
            self._layer_menu(
                self.mapToGlobal(QPoint(self.width() - PANEL_PAD - BTN, HEADER_H))
            )

    # -- 菜单 ------------------------------------------------------------
    def _layer_menu(self, global_pos: QPoint) -> None:
        menu = Menu(self, self._pal)
        desktop_ok = bool(zorder.defview())
        for value, label in (
            ("desktop", "贴桌面（置底）· 不挡窗口"),
            ("normal", "普通"),
            ("top", "置顶 · 压在最前"),
        ):
            text = label if (value != "desktop" or desktop_ok) else f"{label}（当前不可用）"
            act = add_choice(menu, text, value, self._layer, self._pal)
            if value == "desktop" and not desktop_ok:
                act.setEnabled(False)
            act.triggered.connect(lambda _checked=False, v=value: setattr(self, "_pending_layer", v))
        menu.exec(global_pos)
        pending = getattr(self, "_pending_layer", None)
        self._pending_layer = None
        if pending and pending != self._layer:
            self._apply_layer_from_menu(pending)
        else:
            # 勾选态要落在实际档位，没有变化时重画一次就好
            self.update()

    def _apply_layer_from_menu(self, target: str) -> None:
        if not self.apply_layer(target):
            # 桌面档挂不上：降级普通，但**不写回**偏好，explorer 回来后下次点还能贴回去
            self.update()

    def _panel_menu(self, global_pos: QPoint) -> None:
        menu = Menu(self, self._pal)
        open_main = menu.addAction("打开主界面")
        open_main.triggered.connect(self.open_main_requested)
        manage = menu.addAction("管理挂件…")
        manage.triggered.connect(self.manage_requested)
        layer_menu = submenu(menu, "图层", self._pal)
        for value, label in (
            ("desktop", "贴桌面（置底）"),
            ("normal", "普通"),
            ("top", "置顶"),
        ):
            act = add_choice(layer_menu, label, value, self._layer, self._pal)
            act.triggered.connect(lambda _checked=False, v=value: setattr(self, "_pending_layer", v))
        menu.addSeparator()
        # 托盘挂不上时这是唯一的退出口：关窗只是把面板藏起来，进程还活着，
        # 不给一个"真的结束"的入口用户只能去任务管理器。
        quit_act = menu.addAction("退出程序")
        quit_act.triggered.connect(self.quit_requested)
        # 宽度按文字算：默认 hint 没算进 QSS 的左内边距，中文 + 省略号会被截掉
        menu.setMinimumWidth(menu.sizeHint().width() + 24)
        menu.exec(global_pos)
        pending = getattr(self, "_pending_layer", None)
        self._pending_layer = None
        if pending and pending != self._layer:
            self._apply_layer_from_menu(pending)

    def _on_card_menu(self, did: str, global_pos: QPoint) -> None:
        st = self.store.devices.get(did)
        if st is None:
            return
        menu = Menu(self, self._pal)
        open_act = menu.addAction("打开详情")
        open_act.triggered.connect(lambda: self.open_device_requested.emit(did))
        menu.addSeparator()
        remove = menu.addAction("从挂件移除")
        remove.triggered.connect(lambda: self.remove_device_requested.emit(did))
        menu.exec(global_pos)

    # -- 鼠标 ------------------------------------------------------------
    def nativeEvent(self, event_type, message):  # noqa: N802 (Qt 命名)
        """记下"点击之前"的前台窗口。

        普通档清掉 ``WS_EX_NOACTIVATE`` 之后，系统会在我们收到 mousePress 之前就把
        挂件激活（因此它才会被提到最前）——等事件到手里，``GetForegroundWindow``
        已经是挂件自己了，找不回该把键盘还给谁。所以要在 ``WM_MOUSEACTIVATE``
        （0x0021，激活之前）里抓一次。
        """
        try:
            msg = ctypes.wintypes.MSG.from_address(int(message))
            if msg.message == 0x0021:  # WM_MOUSEACTIVATE
                prev = zorder.foreground_window()
                if prev and prev != int(self.winId()):
                    self._prev_foreground = prev
        except Exception:  # noqa: BLE001 —— 探针失败不能影响输入
            pass
        return super().nativeEvent(event_type, message)

    def _bring_to_front(self) -> None:
        """普通档点一下就浮到最前。

        挂件是 ``WA_ShowWithoutActivating`` + ``WindowDoesNotAcceptFocus``，点它不抢
        焦点是刻意的，但 Windows 因此也不会把它提前——被别的窗口压住时点半天没反应
        （用户 2026-11-21 实测）。这里只改 z 序、不动前台窗口。桌面档是 DefView 的
        子窗口、置顶档本来就最前，都不用做。
        """
        if self._layer != "normal" or not self.isVisible():
            return
        hwnd = int(self.winId())
        zorder.raise_window(hwnd)
        # 挂件不接键盘：提层之后把前台还给点它之前那个窗口，别让用户接着打字
        # 打在挂件上（它是 NoFocus 空窗，按键会直接丢掉）。
        prev = getattr(self, "_prev_foreground", 0)
        if prev and prev != hwnd and zorder.foreground_window() == hwnd:
            zorder.set_foreground(prev)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt 命名)
        if event.type() == QEvent.MouseButtonPress:
            self._bring_to_front()
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._bring_to_front()
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        pos = event.position().toPoint()
        button = self._button_at(pos)
        if button:
            self._active_btn = button
            self.update()
            event.accept()
            return
        if not self._cards and self._empty_btn.contains(pos):
            self.open_main_requested.emit()
            event.accept()
            return
        if pos.y() >= self.height() - BOTTOM_H - PANEL_PAD and self._footer_badge():
            self.open_consumables_requested.emit()
            event.accept()
            return
        if pos.y() < HEADER_H or self._card_at(pos) is None:
            self._drag_origin = event.globalPosition().toPoint()
            self._drag_start = self.pos()
            self._dragging = False
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position().toPoint()
        if self._drag_origin is not None and self._drag_start is not None:
            delta = event.globalPosition().toPoint() - self._drag_origin
            if not self._dragging and delta.manhattanLength() >= DRAG_SLOP:
                self._dragging = True
            if self._dragging:
                self.move(self._drag_start + delta)
                event.accept()
                return
        hover = self._button_at(pos)
        if hover != self._hover_btn:
            self._hover_btn = hover
            self.setCursor(Qt.PointingHandCursor if hover else Qt.ArrowCursor)
            # 头部按钮都是自绘的，没有 QWidget 可以挂 setToolTip；悬停时手动弹
            if hover:
                QToolTip.showText(
                    self.mapToGlobal(self._button_rect(hover).center()), _BUTTON_TIPS[hover], self
                )
            else:
                QToolTip.hideText()
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        QToolTip.hideText()
        if self._hover_btn:
            self._hover_btn = ""
            self.setCursor(Qt.ArrowCursor)
            self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            super().mouseReleaseEvent(event)
            return
        pos = event.position().toPoint()
        if self._active_btn:
            name = self._active_btn
            self._active_btn = ""
            self.update()
            if self._button_at(pos) == name:
                self._activate_button(name)
            event.accept()
            return
        dragging = self._dragging
        self._prev_foreground = 0
        self._drag_origin = None
        self._drag_start = None
        self._dragging = False
        if dragging:
            if self._layer == "desktop":
                # 挂进 DefView 后 Qt 设的是父窗口坐标；父窗口原点在虚拟桌面左上角，
                # 与屏幕坐标同值，直接存即可
                pass
            self._clamp_geometry()
            self._schedule_save()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        if self._max_scroll() <= 0:
            super().wheelEvent(event)
            return
        step = -1 if event.angleDelta().y() > 0 else 1
        before = self._scroll
        self._scroll = max(0, min(self._scroll + step, self._max_scroll()))
        if self._scroll != before:
            self._place_cards()
            event.accept()
            return
        super().wheelEvent(event)

    def _card_at(self, pos: QPoint) -> WidgetCard | None:
        for card in self._cards.values():
            if card.isVisible() and card.geometry().contains(pos):
                return card
        return None

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        self._bring_to_front()
        pos = event.pos()
        card = self._card_at(pos)
        if card is not None:
            card.contextMenuEvent(event) if False else self._on_card_menu(
                card.state.did, event.globalPos()
            )
            return
        self._panel_menu(event.globalPos())
