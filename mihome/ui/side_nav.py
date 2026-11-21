# SPDX-License-Identifier: GPL-3.0-or-later
"""左侧房间导航。

桌面端相对手机最大的优势是横向空间。几十台设备平铺下来要滚很久，没有导航就只能
一路滚到底。这里只列**当前家庭**的房间——家庭由顶部的横向切换栏负责，如果两边都
列家庭，同一件事就有两个入口，还容易点错。

房间顺序跟用户配置走（设置页里拖）。房间多到一屏放不下时自己滚动，不做滚动条：
导航本身就该"安静"，多一条滚动条反而抢眼。

底色和右边的卡片墙**不是同一个色**（``theme.nav_bg``）：栏是机身、卡片墙是台面。
两边同色时只能靠一条 1px 竖线分隔，整块界面看起来是连着的。

最下面固定一块「功能区」+ 一个「设置」入口：前者是全屋级页面的入口（四个按钮
——耗材 / 监控 / 电脑自动化 / 挂件），后者收纳主题、排序、隐藏设备这些不常改的东西。
点击功能区统一发 ``zone_requested(key)``；前三个入口都接了页面（「耗材」「监控」
目前是明确的「尚未开放」占位页），页面切换由主窗口的 ``QStackedWidget`` 完成
（`docs/dev/ui.md`「主界面页面宿主」）。「挂件」不是页面，是开关：点一下弹出桌面
挂件、再点一下收起（用户 2026-11-20 定，原来开关只藏在设置页里，没人找得到）；
它的高亮跟着挂件**实际是否可见**走，不跟功能区页面选中态走。
其余页面入口的选中态：底色同房间
选中行，左侧一条 accent 竖条；此时房间行不高亮。点任一房间会清掉选中态、回到
卡片墙，所以 ``build()``（切家庭 / 排序变化）**不重置**页面入口的选中态。
两条分隔线把底部切出语义：房间列表 + 功能区都是「这个家」的内容，
「设置」是「这个程序」的配置，所以功能区上下各一条线，设置单独成一组。
"已隐藏设备"分组排在房间列表末尾，图标上和普通房间区分开——它是收纳箱，不是房间。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..core.store import Store
from . import chrome, theme
from .chrome import rounded_path
from .widgets import draw_text

ITEM_H = 36
PAD_X = 18
TOP = 30
#: 底部「程序级」两行（桌面挂件 / 设置）的总高：上下留白 + 两行 32 + 行距 2。
#: 分隔线在它上面，功能区在再上面。
FOOT_H = 80
#: 功能区：占位按钮 + 上下留白。按钮数由 ``ZONE_ITEMS`` 决定，别把数量写死——
#: 加一条「电脑自动化」时就是漏了这里，底部会压掉最后一条并盖住分隔线。
ZONE_BTN_H = 32
ZONE_GAP = 2
ZONE_PAD_TOP = 7
ZONE_PAD_BOTTOM = 6
#: (按钮文字, 图标类型)，顺序即从上到下的绘制顺序。
#: 这里只放**单个家庭内**的页面；跨家庭的入口（桌面挂件）在分隔线下方。
ZONE_ITEMS = (("耗材", "battery"), ("监控室", "camera"), ("电脑自动化", "pc"))
#: 与 ``ZONE_ITEMS`` 一一对应的稳定 key。信号带 key 而不是索引：以后在中间插一个
#: 入口，主窗口的接线不会因为下标挪动而点错页面。
ZONE_KEYS = ("consumables", "monitor", "automation")
#: 是页面的入口（挂件是开关，不在这里）
PAGE_ZONE_KEYS = ("consumables", "monitor", "automation")
#: 分隔线下方、和「设置」同组的那一行「桌面挂件」开关的 key
FOOT_WIDGET = "widget"
ZONE_H = (ZONE_PAD_TOP + ZONE_BTN_H * len(ZONE_ITEMS)
          + ZONE_GAP * (len(ZONE_ITEMS) - 1) + ZONE_PAD_BOTTOM)
#: 底部固定区总高；房间列表的可见区域与滚动上限都按它算。
BOTTOM_H = ZONE_H + FOOT_H
#: 第二条分隔线落在 FOOT_H 的上边界：上面是家庭级入口，下面是程序级设置。
#: 两条线样式一致（pal.border / 1px / 左右各缩 12），别给其中一条加粗换色。


@dataclass
class _Row:
    kind: str  # "all" | "room" | "hidden"
    label: str
    count: int
    home: str
    room: str
    y: int
    h: int = ITEM_H


class SideNav(QWidget):
    room_selected = Signal(str, str)  # home, room；("", "*") 表示全部
    settings_requested = Signal()
    #: 功能区入口被点击，参数是 ``ZONE_KEYS`` 里的稳定 key
    zone_requested = Signal(str)

    def __init__(self, palette: theme.Palette) -> None:
        super().__init__()
        self._p = palette
        self._rows: list[_Row] = []
        self._active: tuple[str, str] = ("", "*")
        #: 功能区当前页面的 key（``None`` = 正在看卡片墙）。它和 ``_active`` 互斥：
        #: 有功能区选中态时房间行不再高亮。
        self._active_zone: str | None = None
        #: 功能区入口后面的提醒计数：key → (轻提醒, 重提醒)。没登记 = 不画圆点
        self._zone_badges: dict[str, tuple[int, int]] = {}
        #: 「桌面挂件」入口的开关态：跟挂件**实际是否可见**走（不是页面选中态）
        self._widget_on = False
        self._hover = -1
        self._settings_hover = False
        self._widget_hover = False
        self._zone_hover = -1
        self._offset = 0
        self._content_h = 0
        self._home = ""
        self.setFixedWidth(176)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.setMouseTracking(True)

    # -- 数据 ------------------------------------------------------------
    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.update()

    def build(self, store: Store, home_id: str) -> None:
        self._home = home_id
        rows: list[_Row] = []
        y = TOP
        items = [
            d for d in store.devices.values()
            if not d.node.home_id or d.node.home_id == home_id
        ]
        rows.append(_Row("all", "全部设备", len(items), "", "*", y))
        y += ITEM_H + 4

        for view in store.rooms(home_id):
            rows.append(
                _Row("hidden" if view.kind == "hidden" else "room",
                     view.room_label, len(view.devices), view.home, view.room, y)
            )
            y += ITEM_H + 2

        self._rows = rows
        self._content_h = y
        self._offset = 0
        self._active = ("", "*")
        self.update()

    def set_active(self, home: str, room: str) -> None:
        if (home, room) != self._active:
            self._active = (home, room)
            self.update()

    def set_active_zone(self, key: str | None) -> None:
        """功能区选中态。``None`` = 回到卡片墙，房间高亮重新生效。

        ``build()`` 不碰这个值：切家庭 / 改排序时房间行会重建，但用户还停在
        同一个功能区页面，选中态不该自己跳回房间。
        """
        if key != self._active_zone:
            self._active_zone = key
            self.update()

    def set_zone_badges(self, key: str, warn: int, crit: int) -> None:
        """在功能区入口的文字后面画两个计数圆点（灰=轻提醒 / 红=重提醒）。

        为什么挂在这里而不是页面里：它是**入口级**信息——用户停在卡片墙上也要
        知道"有几项耗材该换了"。0 的两档都不画，避免一排 0 变成噪声。
        """
        target = (max(0, int(warn)), max(0, int(crit)))
        if target == (0, 0):
            if self._zone_badges.pop(key, None) is None:
                return
        elif self._zone_badges.get(key) == target:
            return
        else:
            self._zone_badges[key] = target
        self.update()

    def set_widget_on(self, on: bool) -> None:
        """「桌面挂件」入口的亮起状态 = 挂件此刻真的在屏幕上。"""
        on = bool(on)
        if on != self._widget_on:
            self._widget_on = on
            self.update()

    # -- 布局辅助 --------------------------------------------------------
    def _view_rect(self) -> QRect:
        return QRect(0, TOP - 6, self.width(),
                     max(0, self.height() - TOP + 6 - BOTTOM_H))

    def _settings_rect(self) -> QRect:
        return QRect(8, self.height() - FOOT_H + 7 + 32 + 2, self.width() - 16, 32)

    def _widget_rect(self) -> QRect:
        return QRect(8, self.height() - FOOT_H + 7, self.width() - 16, 32)

    def _zone_rect(self, index: int) -> QRect:
        top = self.height() - BOTTOM_H + ZONE_PAD_TOP
        return QRect(8, top + index * (ZONE_BTN_H + ZONE_GAP),
                     self.width() - 16, ZONE_BTN_H)

    def _zone_index_at_point(self, pos: QPoint) -> int:
        for i in range(len(ZONE_ITEMS)):
            if self._zone_rect(i).contains(pos):
                return i
        return -1

    def _max_offset(self) -> int:
        return max(0, self._content_h + 10 - (self.height() - BOTTOM_H))

    # -- 绘制 ------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self._p
        # 栏底色：渐变档铺"卡面"（和卡片 / 指令条 / 搜索框同一支半透明白），
        # 纯色档仍是 nav_bg 压一档。见 theme.nav_fill。
        nav = theme.nav_fill(pal)
        hover = theme.nav_hover_fill(pal)
        # 左下角就是窗口的角，得自己圆，否则会把窗口的抗锯齿弧盖成方角
        p.setClipPath(rounded_path(QRectF(self.rect()), chrome.WINDOW_RADIUS, "bl"))
        p.fillRect(self.rect(), nav)

        draw_text(p, QRect(PAD_X, 4, self.width() - PAD_X * 2, 20), "房间", size=11,
                  color=pal.text_mute)

        p.save()
        p.setClipRect(self._view_rect())
        for i, row in enumerate(self._rows):
            y = row.y - self._offset
            if y + row.h < self._view_rect().top() or y > self._view_rect().bottom():
                continue
            rect = QRect(8, y, self.width() - 16, row.h)
            # 停在功能区页面时房间行全部不高亮：当前"在哪"由底部那一格表示
            active = (self._active_zone is None
                      and (row.home, row.room) == self._active)
            hovered = i == self._hover
            if active:
                p.setPen(Qt.NoPen)
                # 选中块比栏底再实一档（渐变档：卡面 0.42 → 选中 0.68），见 theme.nav_row_fill
                p.setBrush(theme.nav_row_fill(pal))
                p.drawRoundedRect(rect, theme.RADIUS_SMALL, theme.RADIUS_SMALL)
                p.setBrush(QColor(pal.accent))
                p.drawRoundedRect(QRect(8, y + 10, 3, row.h - 20), 1.5, 1.5)
            elif hovered:
                p.setPen(Qt.NoPen)
                p.setBrush(hover)
                p.drawRoundedRect(rect, theme.RADIUS_SMALL, theme.RADIUS_SMALL)

            text_x = PAD_X + 4
            if row.kind == "hidden":
                self._paint_eye_off(p, QPoint(text_x + 6, y + row.h // 2), pal.text_mute)
                text_x += 18
            color = pal.text if active else (pal.text_mute if row.kind == "hidden" else pal.text_soft)
            draw_text(p, QRect(text_x, y, rect.width() - 52, row.h), row.label, size=14,
                      color=color, weight=QFont.DemiBold if active else QFont.Normal)
            if row.count:
                draw_text(p, QRect(rect.width() - 32, y, 24, row.h), str(row.count), size=11,
                          color=pal.text_mute, align=Qt.AlignRight | Qt.AlignVCenter)
        p.restore()

        # 底部固定区：分隔线 + 功能区按钮 + 分隔线 + 设置入口
        p.setPen(QPen(theme.nav_line(pal), 1))
        p.drawLine(12, self.height() - BOTTOM_H, self.width() - 12, self.height() - BOTTOM_H)

        for i, (label, icon) in enumerate(ZONE_ITEMS):
            zrect = self._zone_rect(i)
            hot = i == self._zone_hover
            key = ZONE_KEYS[i]
            active = key == self._active_zone
            if active or hot:
                p.setPen(Qt.NoPen)
                p.setBrush(theme.nav_row_fill(pal) if active else hover)
                p.drawRoundedRect(zrect, 10, 10)
            if active:
                # 与房间选中行同一套标记：左侧 accent 竖条 + 主文字色 + 半粗
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(pal.accent))
                p.drawRoundedRect(QRect(zrect.x(), zrect.y() + 8, 3, zrect.height() - 16),
                                  1.5, 1.5)
            # 未选中跟「未选中的房间」一档（text_soft），不是 hidden 那档的 text_mute：
            # 这两组都是**可点的入口**，压到 mute 会被读成"不可用"（用户 2026-11-16 定）。
            color = pal.text if (active or hot) else pal.text_soft
            self._paint_zone_icon(p, QPoint(zrect.x() + 16, zrect.center().y()), icon, color)
            draw_text(p, QRect(zrect.x() + 32, zrect.y(), zrect.width() - 32, zrect.height()),
                      label, size=13, color=color,
                      weight=QFont.DemiBold if active else QFont.Normal)
            badges = self._zone_badges.get(ZONE_KEYS[i])
            if badges:
                self._paint_zone_badges(p, zrect, label, badges, active)

        # 家庭级入口到此为止。下面两行是**跨家庭 / 程序级**的东西：桌面挂件挂在
        # 所有家庭之上（不随当前家庭切换），设置是程序本身——它们和功能区不是一组，
        # 所以放在分隔线下面（用户 2026-11-20 定）。
        p.setPen(QPen(theme.nav_line(pal), 1))
        p.drawLine(12, self.height() - FOOT_H, self.width() - 12, self.height() - FOOT_H)

        self._paint_foot_row(
            p, self._widget_rect(), "widget", "桌面挂件", self._widget_on,
            self._widget_hover, hover, pal,
        )
        self._paint_foot_row(
            p, self._settings_rect(), "settings", "设置", False,
            self._settings_hover, hover, pal,
        )
        p.end()

    def _paint_foot_row(
        self, p: QPainter, rect: QRect, kind: str, label: str, active: bool,
        hot: bool, hover: QColor, pal: theme.Palette,
    ) -> None:
        """分隔线下方的一行：桌面挂件（开关，亮 = 挂件此刻在屏幕上）/ 设置。

        挂件行的"开"**只由标签右侧那个主色圆点表示**（用户 2026-11-21 定）：
        不画左侧 accent 竖条、不铺选中底色、文字也不提亮加粗——它是个开关，
        不是"当前所在页面"，用功能区那套选中态会读成"你正在挂件页"。
        鼠标悬停仍给底色，那是可点的提示，与开关状态无关。
        """
        emph = active and kind != "widget"
        if hot or emph:
            p.setPen(Qt.NoPen)
            p.setBrush(theme.nav_row_fill(pal) if emph else hover)
            p.drawRoundedRect(rect, 10, 10)
        if emph:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(pal.accent))
            p.drawRoundedRect(QRect(rect.x(), rect.y() + 8, 3, rect.height() - 16), 1.5, 1.5)
        color = pal.text if (hot or emph) else pal.text_soft
        center = QPoint(rect.x() + 16, rect.center().y())
        if kind == "widget":
            self._paint_zone_icon(p, center, "widget", color)
        else:
            self._paint_gear(p, center, 6, color)
        text_rect = QRect(rect.x() + 32, rect.y(), rect.width() - 32, rect.height())
        draw_text(p, text_rect, label, size=13, color=color,
                  weight=QFont.DemiBold if emph else QFont.Normal)
        if kind == "widget" and active:
            self._paint_widget_dot(p, text_rect, label)

    def _paint_widget_dot(self, p: QPainter, text_rect: QRect, label: str) -> None:
        """挂件启用时在标签右侧点一个小圆点。

        这里**不能**照搬功能区那两枚计数 pill：挂件没有数字可显示，而 pill 的
        形状与"有 N 项提醒"绑定，会读成耗材那种告警。用户要的是"这个开关此刻
        是开的"，所以只画一个直径 7 的实心点，位置按文字实际宽度算（同 badge），
        颜色用主色——和左侧 accent 竖条是一套"已启用"语义。
        """
        font = QFont(p.font())
        font.setPixelSize(13)
        x = text_rect.x() + QFontMetrics(font).horizontalAdvance(label) + 6
        cy = text_rect.center().y()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self._p.accent))
        p.drawEllipse(QPointF(x + 3.5, cy), 3.5, 3.5)

    def _paint_zone_badges(
        self, p: QPainter, zrect: QRect, label: str, badges: tuple[int, int], active: bool
    ) -> None:
        """入口文字后面的两个计数圆点：灰 = 轻提醒，红 = 重提醒。

        位置按**文字实际宽度**算（不是右对齐）：用户要的是"耗材 ●●"这种读法，
        右对齐会跑到行的另一头，跟这个入口失去视觉联系。
        """
        font = QFont(p.font())
        font.setPixelSize(13)
        font.setWeight(QFont.DemiBold if active else QFont.Normal)
        x = zrect.x() + 32 + QFontMetrics(font).horizontalAdvance(label) + 7
        cy = zrect.center().y()
        for count, fill, ink in (
            (badges[0], self._p.text_mute, self._p.bg),
            (badges[1], theme.DANGER, "#FFFFFF"),
        ):
            if count <= 0:
                continue
            text = str(count) if count < 100 else "99+"
            wide = 15 if count < 10 else 21
            pill = QRectF(x, cy - 7.5, wide, 15)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(fill))
            p.drawRoundedRect(pill, 7.5, 7.5)
            badge_font = QFont(font)
            badge_font.setPixelSize(10)
            badge_font.setWeight(QFont.DemiBold)
            p.setFont(badge_font)
            p.setPen(QColor(ink))
            p.drawText(pill, Qt.AlignCenter, text)
            x += wide + 4

    @staticmethod
    def _paint_zone_icon(p: QPainter, center: QPoint, kind: str, color: str) -> None:
        """功能区线框图标，笔触与齿轮一致（1.5px / 圆头 / 默认无填充）。"""
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(color), 1.5, Qt.SolidLine, Qt.RoundCap))
        if kind == "battery":
            # 横放电池：外壳 + 正极 + 一格余量
            p.drawRoundedRect(QRectF(center.x() - 7, center.y() - 4.5, 12, 9), 1.5, 1.5)
            p.drawLine(QPoint(center.x() + 6, center.y() - 2),
                       QPoint(center.x() + 6, center.y() + 2))
            p.setBrush(QColor(color))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(QRectF(center.x() - 5, center.y() - 2.5, 4, 5), 1, 1)
        elif kind == "pc":
            # 显示器 + 支架：和「监控」那枚摄像头区分开，别用同一个方框
            p.drawRoundedRect(QRectF(center.x() - 8, center.y() - 6, 16, 11), 1.5, 1.5)
            p.drawLine(QPoint(center.x(), center.y() + 5),
                       QPoint(center.x(), center.y() + 7))
            p.drawLine(QPoint(center.x() - 4, center.y() + 7),
                       QPoint(center.x() + 4, center.y() + 7))
        elif kind == "widget":
            # 桌面挂件：一块圆角面板 + 底部状态条，跟挂件窗口自己的样子对齐
            p.drawRoundedRect(QRectF(center.x() - 8, center.y() - 5.5, 16, 11), 2, 2)
            p.drawLine(QPoint(center.x() - 4, center.y() + 2),
                       QPoint(center.x() + 4, center.y() + 2))
        else:
            # 摄像头：圆角机身 + 镜头 + 底座
            p.drawRoundedRect(QRectF(center.x() - 7, center.y() - 5, 14, 10), 2, 2)
            p.drawEllipse(center, 2.5, 2.5)
            p.drawLine(QPoint(center.x(), center.y() + 5),
                       QPoint(center.x(), center.y() + 7))
            p.drawLine(QPoint(center.x() - 4, center.y() + 7),
                       QPoint(center.x() + 4, center.y() + 7))

    @staticmethod
    def _paint_gear(p: QPainter, center: QPoint, r: int, color: str) -> None:
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(color), 1.5, Qt.SolidLine, Qt.RoundCap))
        p.drawEllipse(center, max(2, r // 2), max(2, r // 2))
        for i in range(8):
            ang = i * math.pi / 4
            x1 = center.x() + math.cos(ang) * r * 0.75
            y1 = center.y() + math.sin(ang) * r * 0.75
            x2 = center.x() + math.cos(ang) * r
            y2 = center.y() + math.sin(ang) * r
            p.drawLine(QPoint(round(x1), round(y1)), QPoint(round(x2), round(y2)))

    @staticmethod
    def _paint_eye_off(p: QPainter, center: QPoint, color: str) -> None:
        """一只被划掉的眼睛：表示"收起来了"，不是"删掉了"。"""
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(color), 1.3, Qt.SolidLine, Qt.RoundCap))
        rect = QRect(center.x() - 6, center.y() - 4, 12, 9)
        p.drawArc(rect, 200 * 16, 140 * 16)
        p.setBrush(QColor(color))
        p.setPen(Qt.NoPen)
        p.drawEllipse(center, 1, 1)
        p.setPen(QPen(QColor(color), 1.3, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(center.x() - 6, center.y() + 5, center.x() + 5, center.y() - 6)

    # -- 命中 ------------------------------------------------------------
    def _row_index_at_point(self, pos: QPoint) -> int:
        if not self._view_rect().contains(pos):
            return -1
        y = pos.y() + self._offset
        for i, row in enumerate(self._rows):
            if row.y <= y <= row.y + row.h:
                return i
        return -1

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position().toPoint()
        idx = self._row_index_at_point(pos)
        over_settings = self._settings_rect().contains(pos)
        over_widget = self._widget_rect().contains(pos)
        zidx = self._zone_index_at_point(pos)
        if (idx != self._hover or over_settings != self._settings_hover
                or over_widget != self._widget_hover or zidx != self._zone_hover):
            self._hover = idx
            self._settings_hover = over_settings
            self._widget_hover = over_widget
            self._zone_hover = zidx
            pointing = idx >= 0 or over_settings or over_widget or zidx >= 0
            self.setCursor(Qt.PointingHandCursor if pointing else Qt.ArrowCursor)
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = -1
        self._settings_hover = False
        self._widget_hover = False
        self._zone_hover = -1
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            return
        pos = event.position().toPoint()
        if self._widget_rect().contains(pos):
            # 开关：不切页面、不改页面选中态，主窗口负责显隐并回写 _widget_on
            self.zone_requested.emit(FOOT_WIDGET)
            return
        zidx = self._zone_index_at_point(pos)
        if zidx >= 0:
            key = ZONE_KEYS[zidx]
            # 选中态先就地生效，主窗口随后切页
            self._active_zone = key
            self.update()
            self.zone_requested.emit(key)
            return
        if self._settings_rect().contains(pos):
            self.settings_requested.emit()
            return
        idx = self._row_index_at_point(pos)
        if idx < 0:
            return
        row = self._rows[idx]
        self._active_zone = None
        self._active = (row.home, row.room)
        self.update()
        self.room_selected.emit(row.home, row.room)

    def wheelEvent(self, event) -> None:  # noqa: N802
        if self._max_offset() <= 0:
            return
        step = event.angleDelta().y() / 120 * 42
        new = min(self._max_offset(), max(0, self._offset - int(step)))
        if new != self._offset:
            self._offset = new
            self.update()


__all__ = [
    "FOOT_WIDGET", "SideNav", "PAGE_ZONE_KEYS", "ZONE_ITEMS", "ZONE_KEYS",
]
