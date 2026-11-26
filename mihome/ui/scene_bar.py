"""首页顶部的场景快捷条。

场景是米家首页仅次于设备的入口（"全部关灯""回家模式"这类）。数据来自
``get_scenes_list``，执行走 ``run_scene``。

呈现上用一排胶囊，横向排列、多了换行。没有用卡片，是因为场景是**一次性动作**
而不是**有状态的设备**——用卡片会让它看起来像"可以打开的东西"。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..core import applog
from . import theme
from .widgets import draw_text

PILL_H = 60
PILL_MIN_W = 163
GAP = 12


class ScenePill(QWidget):
    clicked = Signal(str)
    #: 右键：场景 id + 屏幕坐标（菜单由 WallView 建，它才知道隐藏状态）
    menu_requested = Signal(str, QPoint)

    def __init__(self, scene_id: str, name: str, palette: theme.Palette) -> None:
        super().__init__()
        self.scene_id = scene_id
        self.name = name
        self._p = palette
        self._hover = False
        self._flash = 0.0
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        fm = QFontMetrics(self._font())
        self._w = max(PILL_MIN_W, fm.horizontalAdvance(name) + 68)
        self.setFixedSize(self._w, PILL_H)

    def _font(self) -> QFont:
        f = QFont()
        f.setPixelSize(15)
        f.setWeight(QFont.Medium)
        return f

    def sizeHint(self):  # noqa: N802
        from PySide6.QtCore import QSize

        return QSize(self._w, PILL_H)

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self._p
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        # 大按钮：20dp 圆角，**不是全圆胶囊**（米家 60dp 高的按钮也只用 20）
        p.setPen(Qt.NoPen)
        # 填充走 ``card_fill``：场景条也是"卡片面"，背景预设半透明时它要跟着透，
        # 否则左上角一条实心白胶囊压在有渐变的背景上，比卡片还突兀。
        fill = theme.card_hover_color(pal.card, pal) if self._hover else pal.card
        p.setBrush(theme.card_fill(fill, pal))
        p.drawRoundedRect(r, theme.RADIUS_BUTTON, theme.RADIUS_BUTTON)

        # 左侧一个小闪电，表示"动作"而不是"状态"
        c = QRectF(22, PILL_H / 2 - 9, 15, 18)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(pal.accent if self._hover else pal.text_soft))
        from PySide6.QtGui import QPolygonF
        from PySide6.QtCore import QPointF

        bolt = QPolygonF([
            QPointF(c.x() + 9, c.y()),
            QPointF(c.x() + 2, c.y() + 9),
            QPointF(c.x() + 8, c.y() + 9),
            QPointF(c.x() + 6, c.y() + 18),
            QPointF(c.x() + 14, c.y() + 7),
            QPointF(c.x() + 8, c.y() + 7),
        ])
        p.drawPolygon(bolt)

        draw_text(p, QRect(46, 0, self._w - 62, PILL_H), self.name, size=15,
                  color=pal.text, weight=QFont.Medium)
        p.end()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.scene_id)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        self.menu_requested.emit(self.scene_id, event.globalPos())


class SceneBar(QWidget):
    """一行场景胶囊 + 标题。没有场景时整体隐藏。"""

    scene_run = Signal(str)
    scene_menu = Signal(str, QPoint)

    def __init__(self, palette: theme.Palette) -> None:
        super().__init__()
        self._p = palette
        self._title = "快捷场景"
        self._pills: list[ScenePill] = []
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        for pill in self._pills:
            pill.set_palette(palette)
        self.update()

    def set_scenes(self, scenes: list[dict[str, Any]]) -> None:
        for pill in self._pills:
            pill.setParent(None)
            pill.deleteLater()
        self._pills = []
        disabled = 0
        nameless = 0
        for s in scenes:
            if not s.get("enable", True):
                disabled += 1
                continue
            sid = str(s.get("scene_id") or "")
            name = str(s.get("name") or "")
            if not sid or not name:
                nameless += 1
                continue
            pill = ScenePill(sid, name, self._p)
            pill.setParent(self)
            pill.clicked.connect(self.scene_run)
            pill.menu_requested.connect(self.scene_menu)
            # **必须显式 show()**：父控件已经可见时新建的子控件默认是隐藏的
            # （`isHidden()` 为真，父的 setVisible 不会再把它带出来）。
            # 首次建墙时条还没显示，子控件跟着父一起出现，所以那里看不出问题；
            # 而 `refresh_scenes()` 是在条可见时换胶囊的——漏了这句就只有标题和
            # 占位高度、一个按钮都不画（2026-11-27 用户实测：App 里增删场景后
            # 刷新即如此，切一次家庭整墙重建就正常）。
            pill.show()
            self._pills.append(pill)
        if scenes and not self._pills:
            # 拿到了数据却一条都没画出来，界面只是"空着"，没有任何痕迹——
            # 2026-11-26 场景整排消失时，就是卡在这里分不清是没数据还是字段变了。
            applog.log(
                "scene",
                f"{len(scenes)} 条全部跳过：enable 为假 {disabled} 条、"
                f"缺 scene_id/name {nameless} 条",
            )
        self._relayout()
        self.setVisible(bool(self._pills))
        applog.log(
            "scene",
            f"条更新：{len(scenes)} 条进、{len(self._pills)} 个胶囊出，"
            f"可见={self.isVisible()} 高={self.height()}",
        )

    def _relayout(self) -> None:
        x = 0
        y = 26
        width = self.width() or 900
        for pill in self._pills:
            if x + pill.width() > width and x > 0:
                x = 0
                y += PILL_H + GAP
            pill.move(x, y)
            x += pill.width() + GAP
        self.setFixedHeight(y + PILL_H if self._pills else 0)

    def resizeEvent(self, event) -> None:  # noqa: N802
        self._relayout()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        draw_text(p, QRect(0, 0, self.width(), 22), self._title, size=12, color=self._p.text_mute)
        p.end()


__all__ = ["SceneBar"]
