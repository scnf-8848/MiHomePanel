# SPDX-License-Identifier: GPL-3.0-or-later
"""左侧房间导航。

桌面端相对手机最大的优势是横向空间。几十台设备平铺下来要滚很久，没有导航就只能
一路滚到底。这里只列**当前家庭**的房间——家庭由顶部的横向切换栏负责，如果两边都
列家庭，同一件事就有两个入口，还容易点错。

房间顺序跟用户配置走（设置页里拖）。房间多到一屏放不下时自己滚动，不做滚动条：
导航本身就该"安静"，多一条滚动条反而抢眼。

底色和右边的卡片墙**不是同一个色**（``theme.nav_bg``）：栏是机身、卡片墙是台面。
两边同色时只能靠一条 1px 竖线分隔，整块界面看起来是连着的。

最下面固定一块「功能区」+ 一个「设置」入口：前者是全屋级页面的入口（三个按钮
——耗材 / 监控 / 电脑自动化），后者收纳主题、排序、隐藏设备这些不常改的东西。
点击功能区统一发 ``zone_requested(key)``；目前只有「电脑自动化」接了页面，另两个
由主窗口忽略（仍是占位），**不要在侧栏里悄悄跳一个空页面**（`docs/dev/ui.md`「左侧导航底部的功能区」）。
两条分隔线把底部切出语义：房间列表 + 功能区都是「这个家」的内容，
「设置」是「这个程序」的配置，所以功能区上下各一条线，设置单独成一组。
"已隐藏设备"分组排在房间列表末尾，图标上和普通房间区分开——它是收纳箱，不是房间。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..core.store import Store
from . import chrome, theme
from .chrome import rounded_path
from .widgets import draw_text

ITEM_H = 36
PAD_X = 18
TOP = 30
#: 设置行那一块的高度（含上下留白）。功能区在它上面，别把它当成整个底部区。
FOOT_H = 46
#: 功能区：占位按钮 + 上下留白。按钮数由 ``ZONE_ITEMS`` 决定，别把数量写死——
#: 加一条「电脑自动化」时就是漏了这里，底部会压掉最后一条并盖住分隔线。
ZONE_BTN_H = 32
ZONE_GAP = 2
ZONE_PAD_TOP = 7
ZONE_PAD_BOTTOM = 6
#: (按钮文字, 图标类型)，顺序即从上到下的绘制顺序。
ZONE_ITEMS = (("耗材", "battery"), ("监控", "camera"), ("电脑自动化", "pc"))
#: 与 ``ZONE_ITEMS`` 一一对应的稳定 key。信号带 key 而不是索引：以后在中间插一个
#: 入口，主窗口的接线不会因为下标挪动而点错页面。
ZONE_KEYS = ("consumables", "monitor", "automation")
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
        self._hover = -1
        self._settings_hover = False
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

    # -- 布局辅助 --------------------------------------------------------
    def _view_rect(self) -> QRect:
        return QRect(0, TOP - 6, self.width(),
                     max(0, self.height() - TOP + 6 - BOTTOM_H))

    def _settings_rect(self) -> QRect:
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
        # 栏底色比内容区退/提一档：之前两边都是 pal.bg，只靠一条 1px 竖线分隔，
        # 整块界面看起来是连在一起的，房间栏像是浮在卡片墙上的文字。
        bg = theme.nav_bg(pal)
        hover_bg = theme.blend(bg, pal.text, 0.06)
        # 左下角就是窗口的角，得自己圆，否则会把窗口的抗锯齿弧盖成方角
        p.setClipPath(rounded_path(QRectF(self.rect()), chrome.WINDOW_RADIUS, "bl"))
        p.fillRect(self.rect(), QColor(bg))

        draw_text(p, QRect(PAD_X, 4, self.width() - PAD_X * 2, 20), "房间", size=11,
                  color=pal.text_mute)

        p.save()
        p.setClipRect(self._view_rect())
        for i, row in enumerate(self._rows):
            y = row.y - self._offset
            if y + row.h < self._view_rect().top() or y > self._view_rect().bottom():
                continue
            rect = QRect(8, y, self.width() - 16, row.h)
            active = (row.home, row.room) == self._active
            hovered = i == self._hover
            if active:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(theme.nav_row_bg(pal)))
                p.drawRoundedRect(rect, theme.RADIUS_SMALL, theme.RADIUS_SMALL)
                p.setBrush(QColor(pal.accent))
                p.drawRoundedRect(QRect(8, y + 10, 3, row.h - 20), 1.5, 1.5)
            elif hovered:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(hover_bg))
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
        p.setPen(QPen(QColor(pal.border), 1))
        p.drawLine(12, self.height() - BOTTOM_H, self.width() - 12, self.height() - BOTTOM_H)

        for i, (label, icon) in enumerate(ZONE_ITEMS):
            zrect = self._zone_rect(i)
            hot = i == self._zone_hover
            if hot:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(hover_bg))
                p.drawRoundedRect(zrect, 10, 10)
            color = pal.text_soft if hot else pal.text_mute
            self._paint_zone_icon(p, QPoint(zrect.x() + 16, zrect.center().y()), icon, color)
            draw_text(p, QRect(zrect.x() + 32, zrect.y(), zrect.width() - 32, zrect.height()),
                      label, size=13, color=color)

        # 家庭级入口到此为止，下面只剩「这个程序」的设置；再分一道，别让功能区
        # 和设置读成同一组。位置取 FOOT_H 上边界，正好落在两组留白的中间。
        p.setPen(QPen(QColor(pal.border), 1))
        p.drawLine(12, self.height() - FOOT_H, self.width() - 12, self.height() - FOOT_H)

        srect = self._settings_rect()
        if self._settings_hover:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(hover_bg))
            p.drawRoundedRect(srect, 10, 10)
        self._paint_gear(p, QPoint(srect.x() + 16, srect.center().y()), 6,
                         pal.text_soft if self._settings_hover else pal.text_mute)
        draw_text(p, QRect(srect.x() + 32, srect.y(), srect.width() - 32, srect.height()),
                  "设置", size=13, color=pal.text_soft if self._settings_hover else pal.text_mute)
        p.end()

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
        zidx = self._zone_index_at_point(pos)
        if (idx != self._hover or over_settings != self._settings_hover
                or zidx != self._zone_hover):
            self._hover = idx
            self._settings_hover = over_settings
            self._zone_hover = zidx
            pointing = idx >= 0 or over_settings or zidx >= 0
            self.setCursor(Qt.PointingHandCursor if pointing else Qt.ArrowCursor)
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = -1
        self._settings_hover = False
        self._zone_hover = -1
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            return
        pos = event.position().toPoint()
        zidx = self._zone_index_at_point(pos)
        if zidx >= 0:
            # 只有「电脑自动化」目前接了页面；其余两个主窗口收到后忽略，仍是占位
            self.zone_requested.emit(ZONE_KEYS[zidx])
            return
        if self._settings_rect().contains(pos):
            self.settings_requested.emit()
            return
        idx = self._row_index_at_point(pos)
        if idx < 0:
            return
        row = self._rows[idx]
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


__all__ = ["SideNav", "ZONE_ITEMS", "ZONE_KEYS"]
