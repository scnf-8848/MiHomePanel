# SPDX-License-Identifier: GPL-3.0-or-later
"""左侧房间导航。

桌面端相对手机最大的优势是横向空间。几十台设备平铺下来要滚很久，没有导航就只能
一路滚到底。这里只列**当前家庭**的房间——家庭由顶部的横向切换栏负责，如果两边都
列家庭，同一件事就有两个入口，还容易点错。

房间顺序跟用户配置走（设置页里拖）。房间多到一屏放不下时自己滚动，不做滚动条：
导航本身就该"安静"，多一条滚动条反而抢眼。

底色和右边的卡片墙**不是同一个色**（``theme.nav_bg``）：栏是机身、卡片墙是台面。
两边同色时只能靠一条 1px 竖线分隔，整块界面看起来是连着的。

最下面固定一个「设置」入口，收纳主题、排序、隐藏设备这些不常改的东西。
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
FOOT_H = 46


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

    def __init__(self, palette: theme.Palette) -> None:
        super().__init__()
        self._p = palette
        self._rows: list[_Row] = []
        self._active: tuple[str, str] = ("", "*")
        self._hover = -1
        self._settings_hover = False
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

    def build(self, store: Store, home: str) -> None:
        self._home = home
        rows: list[_Row] = []
        y = TOP
        items = [d for d in store.devices.values() if not d.node.home or d.node.home == home]
        rows.append(_Row("all", "全部设备", len(items), "", "*", y))
        y += ITEM_H + 4

        for view in store.rooms(home):
            rows.append(
                _Row("hidden" if view.kind == "hidden" else "room",
                     view.room, len(view.devices), view.home, view.room, y)
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
        return QRect(0, TOP - 6, self.width(), max(0, self.height() - TOP + 6 - FOOT_H))

    def _settings_rect(self) -> QRect:
        return QRect(8, self.height() - FOOT_H + 7, self.width() - 16, 32)

    def _max_offset(self) -> int:
        return max(0, self._content_h + 10 - (self.height() - FOOT_H))

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

        # 底部：一条分隔线 + 设置入口
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
        if idx != self._hover or over_settings != self._settings_hover:
            self._hover = idx
            self._settings_hover = over_settings
            self.setCursor(Qt.PointingHandCursor if (idx >= 0 or over_settings) else Qt.ArrowCursor)
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = -1
        self._settings_hover = False
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            return
        pos = event.position().toPoint()
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


__all__ = ["SideNav"]
