# SPDX-License-Identifier: GPL-3.0-or-later
"""设置页用的树：三层可展开、同层内拖动调序、行尾带开关。

为什么不用 ``QListWidget`` 平铺：设备是"家庭 → 房间 → 设备"三层结构，平铺时必须
用两个下拉框先选家庭和房间，选完才看到一小截设备——每调一次顺序要重复三次选择，
而且看不到整体。树把三层摊开，一次看全。

为什么也不直接用 ``QTreeWidget`` 的默认外观：默认行高、分支箭头、选中色都是系统
样式，和这套自绘界面放在一起像另一个程序。这里用 ``QStyledItemDelegate`` 自己画：

* 圆角选中块（和侧边栏同款），行高 32；
* 分支折角自己画（系统那个三角在深色主题下几乎看不见）；
* 行尾可以带一段次要文字（"6 台"、"已隐藏"）或一枚开关；
* 开关支持"部分"：房间里有 3 台设备、只藏了 1 台时停在中间并画一道减号——
  画成"全开"是撒谎，画成"全关"会让人以为再点一下是全部打开。
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QGraphicsOpacityEffect,
    QLabel,
    QStyle,
    QStyledItemDelegate,
    QTreeWidget,
    QTreeWidgetItem,
    QWidget,
)

from . import theme

ROW_H = 32
INDENT = 16
#: 拖到上下边缘多少像素内开始自动滚动，以及每次滚多少
AUTO_SCROLL_EDGE = 30
AUTO_SCROLL_STEP = 18
CHEV = 14
SWITCH_W = 36
SWITCH_H = 20
#: 行数据（dict）
ROLE_DATA = Qt.UserRole
#: 行尾副文本
ROLE_SUFFIX = Qt.UserRole + 1
#: 开关状态：1.0 = 全开 / 0.0 = 全关 / 中间值 = 部分
ROLE_SWITCH = Qt.UserRole + 2


def _switch_rect(rect: QRect) -> QRect:
    return QRect(rect.right() - SWITCH_W - 10, rect.y() + (rect.height() - SWITCH_H) // 2,
                 SWITCH_W, SWITCH_H)


def row_x(rect: QRect, depth: int) -> int:
    """这一层文字／折角的起始 x。

    缩进是自己算的，不是 ``setIndentation``：``QTreeView`` 会给缩进区（branch
    区域）单独铺一层 ``palette.highlight()`` 的方底色，选中时就变成文字块左边
    突兀的一块深蓝，和这边的圆角选中块打架。把 indentation 设成 0，整个 rect
    都归 delegate，缩进按层级自己加，圆角块才能从行首铺到行尾。
    """
    return rect.x() + 2 + depth * INDENT


def _chevron_hit(rect: QRect, depth: int = 0) -> QRect:
    return QRect(row_x(rect, depth), rect.y(), CHEV + 4, rect.height())


def _paint_chevron(p: QPainter, rect: QRect, expanded: bool, color: str) -> None:
    """一个 7px 的小折角：展开朝下，收起朝右。"""
    p.setPen(QPen(QColor(color), 1.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    cx, cy = rect.center().x(), rect.center().y()
    if expanded:
        p.drawPolyline([QPoint(cx - 3, cy - 2), QPoint(cx, cy + 2), QPoint(cx + 3, cy - 2)])
    else:
        p.drawPolyline([QPoint(cx - 2, cy - 3), QPoint(cx + 2, cy), QPoint(cx - 2, cy + 3)])


def _paint_switch(p: QPainter, rect: QRect, t: float, pal: theme.Palette) -> None:
    """和 ``widgets.ToggleSwitch`` 同一套观感，但没有动画（一屏几十行，动起来太吵）。"""
    t = max(0.0, min(1.0, t))
    partial = 0.02 < t < 0.98
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(theme.TOGGLE_ON) if t >= 0.98 else QColor(_track_off(pal)))
    p.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)

    knob = rect.height() - 4
    x = rect.x() + 2 + t * (rect.width() - knob - 4)
    box = QRect(int(round(x)), rect.y() + 2, knob, knob)
    p.setBrush(QColor("#FFFFFF"))
    p.drawEllipse(box)
    if partial:
        # 停中间的滑块 + 一道减号：这是"一部分显示了、一部分没显示"
        p.setPen(QPen(QColor(pal.text_mute), 1.6, Qt.SolidLine, Qt.RoundCap))
        y = box.center().y()
        p.drawLine(box.center().x() - 3, y, box.center().x() + 3, y)


def _track_off(p: theme.Palette) -> str:
    return theme.blend(p.bg, p.text, 0.20 if not p.is_dark else 0.26)


class _RowDelegate(QStyledItemDelegate):
    def __init__(self, palette: theme.Palette, mode: str = "order") -> None:
        super().__init__()
        self._p = palette
        self._mode = mode

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette

    def sizeHint(self, option, index: QModelIndex) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), ROW_H)

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        p = painter
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        pal = self._p
        rect = option.rect
        row = rect.adjusted(2, 1, -2, -1)

        if option.state & QStyle.State_Selected:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.panel_row_selected(pal)))
            p.drawRoundedRect(row, 8, 8)
        elif option.state & QStyle.State_MouseOver:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.panel_row_hover(pal)))
            p.drawRoundedRect(row, 8, 8)

        depth = 0
        node = index.parent()
        while node.isValid():
            depth += 1
            node = node.parent()
        has_kids = index.model().rowCount(index) > 0

        x = row_x(rect, depth)
        if has_kids:
            _paint_chevron(p, QRect(x, rect.y() + (rect.height() - CHEV) // 2, CHEV, CHEV),
                           self._expanded(option, index), pal.text_mute)
        x += CHEV + 2

        switch = index.data(ROLE_SWITCH)
        suffix = str(index.data(ROLE_SUFFIX) or "")
        right_pad = 8
        sw_rect = QRect()
        if switch is not None:
            sw_rect = _switch_rect(rect)
            right_pad = rect.right() - sw_rect.x() + 8
        if suffix:
            font = QFont(option.font)
            font.setPixelSize(11)
            p.setFont(font)
            fm = QFontMetrics(font)
            w = fm.horizontalAdvance(suffix)
            right_at = rect.right() - 12 if switch is None else sw_rect.x() - 8
            p.setPen(QColor(pal.text_mute))
            p.drawText(QRect(right_at - w, rect.y(), w, rect.height()),
                       int(Qt.AlignRight | Qt.AlignVCenter), suffix)
            right_pad = max(right_pad, rect.right() - (right_at - w) + 8)

        text = str(index.data(Qt.DisplayRole) or "")
        font = QFont(option.font)
        font.setPixelSize(13)
        if depth == 0:
            font.setWeight(QFont.DemiBold)
            color = pal.text
        elif depth == 1:
            color = pal.text_soft if self._mode == "order" else pal.text
        else:
            color = pal.text
        p.setFont(font)
        fm = QFontMetrics(font)
        avail = max(0, rect.right() - right_pad - x)
        p.setPen(QColor(color))
        p.drawText(QRect(x, rect.y(), avail, rect.height()),
                   int(Qt.AlignLeft | Qt.AlignVCenter),
                   fm.elidedText(text, Qt.ElideRight, avail))

        if switch is not None:
            _paint_switch(p, sw_rect, float(switch), pal)
        p.restore()

    @staticmethod
    def _expanded(option, index: QModelIndex) -> bool:
        view = option.widget
        if isinstance(view, QTreeWidget):
            item = view.itemFromIndex(index)
            return bool(item is not None and item.isExpanded())
        return True


class ReorderTree(QTreeWidget):
    """可展开、可同层拖动调序的树。行尾开关点了发 ``toggle_requested``。"""

    moved = Signal()
    toggle_requested = Signal(dict)

    def __init__(self, palette: theme.Palette, *, mode: str = "order",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._p = palette
        self._mode = mode
        self._delegate = _RowDelegate(palette, mode)
        self.setItemDelegate(self._delegate)

        self.setHeaderHidden(True)
        self.setColumnCount(1)
        # 缩进交给 delegate 自己算（见 ``row_x``）：留 0 是为了不让 QTreeView
        # 在左侧画那条会被 highlight 上色的 branch 区。
        self.setIndentation(0)
        self.setRootIsDecorated(False)      # 折角自己画
        self.setUniformRowHeights(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setMouseTracking(True)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.NoFocus)
        self.setStyleSheet(
            "QTreeWidget { background: transparent; border: none; outline: none;"
            " selection-background-color: transparent; show-decoration-selected: 0; }"
            "QTreeWidget::item { border: none; }"
            # 系统的展开三角在深色下几乎看不见，而且会和我们画的折角并排出现两个
            "QTreeWidget::branch { image: none; border-image: none; background: transparent; }"
        )

        # 不用 QTreeWidget 自带的拖放：它是系统拖影 + 系统落位线，和首页卡片
        # 那套（幽灵行、自己画的落位线、边缘自动滚、松手才落盘）不一致。
        self.setDragEnabled(False)
        self.setAcceptDrops(False)
        self.setDropIndicatorShown(False)

        self._press_pos: QPoint | None = None
        self._press_item: QTreeWidgetItem | None = None
        self._drag_item: QTreeWidgetItem | None = None
        self._drag_ghost: QLabel | None = None
        self._drop_line: QWidget | None = None
        self._drop_parent: QTreeWidgetItem | None = None
        self._drop_index = -1
        self._last_drag_pos = QPoint()
        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(16)
        self._auto_timer.timeout.connect(self._auto_scroll)

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self._delegate.set_palette(palette)
        if self._drop_line is not None:
            self._drop_line.setStyleSheet(
                f"background: {palette.accent}; border-radius: 1px;"
            )
        self.viewport().update()

    # -- 展开状态 --------------------------------------------------------
    def _walk(self):
        """前序遍历所有行（``QTreeWidgetItemIterator`` 在 PySide6 里不好用）。"""
        stack = [self.topLevelItem(i) for i in range(self.topLevelItemCount())]
        while stack:
            item = stack.pop()
            yield item
            stack.extend(item.child(i) for i in range(item.childCount()))

    def expanded_keys(self) -> set[str]:
        """只统计**有子项**的节点——叶子没有"展开"这回事，记上它只会污染集合。"""
        return {
            self._item_key(item) for item in self._walk()
            if item.childCount() and item.isExpanded()
        }

    def restore_expanded(self, keys: set[str] | None) -> None:
        """``None`` = 第一次填充，全部展开；空集合 = 用户把它们都折起来了，别再撑开。"""
        for item in self._walk():
            if not item.childCount():
                continue
            if keys is None or self._item_key(item) in keys:
                item.setExpanded(True)

    @staticmethod
    def _item_key(item: QTreeWidgetItem) -> str:
        data = item.data(0, ROLE_DATA) or {}
        return "|".join(str(data.get(k, "")) for k in ("kind", "home", "room", "id", "key"))

    @staticmethod
    def _depth(item: QTreeWidgetItem) -> int:
        depth = 0
        node = item.parent()
        while node is not None:
            depth += 1
            node = node.parent()
        return depth

    # -- 交互 ------------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._press_pos = event.position().toPoint()
            self._press_item = self.itemAt(self._press_pos)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._drag_item is not None:
            self._finish_drag()
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            pos = event.position().toPoint()
            item = self.itemAt(pos)
            if item is not None:
                rect = self.visualItemRect(item)
                if item.childCount() and _chevron_hit(rect, self._depth(item)).contains(pos):
                    item.setExpanded(not item.isExpanded())
                    self.viewport().update()
                    self._clear_press()
                    return
                if item.data(0, ROLE_SWITCH) is not None and _switch_rect(rect).contains(pos):
                    self.toggle_requested.emit(dict(item.data(0, ROLE_DATA) or {}))
                    self._clear_press()
                    return
        self._clear_press()
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position().toPoint()
        if self._drag_item is not None:
            self._update_drag(pos)
            event.accept()
            return
        if (self._press_item is not None
                and event.buttons() & Qt.LeftButton
                and (pos - self._press_pos).manhattanLength()
                >= QApplication.startDragDistance()):
            if self._can_drag(self._press_item, self._press_pos):
                self._begin_drag(self._press_item, pos)
                event.accept()
                return
            self._clear_press()
        item = self.itemAt(pos)
        hot = False
        if item is not None:
            rect = self.visualItemRect(item)
            hot = (bool(item.childCount())
                   and _chevron_hit(rect, self._depth(item)).contains(pos)) or (
                item.data(0, ROLE_SWITCH) is not None and _switch_rect(rect).contains(pos)
            )
        self.viewport().setCursor(Qt.PointingHandCursor if hot else Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    def _can_drag(self, item: QTreeWidgetItem, pos: QPoint) -> bool:
        if self._mode != "order":
            return False
        data = item.data(0, ROLE_DATA) or {}
        if data.get("kind") not in ("home", "room", "scene"):
            return False
        rect = self.visualItemRect(item)
        if item.childCount() and _chevron_hit(rect, self._depth(item)).contains(pos):
            return False
        if item.data(0, ROLE_SWITCH) is not None and _switch_rect(rect).contains(pos):
            return False
        return True

    def _begin_drag(self, item: QTreeWidgetItem, pos: QPoint) -> None:
        rect = self.visualItemRect(item)
        if not rect.isValid() or rect.isEmpty():
            return
        self._drag_item = item
        self._press_item = None
        self._last_drag_pos = pos
        ghost = QLabel(self.viewport())
        ghost.setPixmap(self.viewport().grab(rect))
        ghost.adjustSize()
        ghost.setAttribute(Qt.WA_TransparentForMouseEvents)
        effect = QGraphicsOpacityEffect(ghost)
        effect.setOpacity(0.92)
        ghost.setGraphicsEffect(effect)
        ghost.show()
        ghost.raise_()
        self._drag_ghost = ghost
        line = QWidget(self.viewport())
        line.setAttribute(Qt.WA_TransparentForMouseEvents)
        line.setStyleSheet(f"background: {self._p.accent}; border-radius: 1px;")
        line.hide()
        self._drop_line = line
        self._update_drag(pos)

    def _update_drag(self, pos: QPoint) -> None:
        self._last_drag_pos = pos
        src = self._drag_item
        if src is None:
            return
        ghost = self._drag_ghost
        if ghost is not None:
            rect = self.visualItemRect(src)
            ghost.move(
                max(0, min(pos.x() + 8, self.viewport().width() - ghost.width())),
                max(0, min(pos.y() - rect.height() // 2,
                           self.viewport().height() - ghost.height())),
            )
        parent, index, line_y = self._drop_target(src, pos)
        self._drop_parent, self._drop_index = parent, index
        line = self._drop_line
        if line is not None:
            if line_y < 0:
                line.hide()
            else:
                line.setGeometry(4, max(0, line_y - 1), max(8, self.viewport().width() - 12), 2)
                line.show()
                line.raise_()
        bar = self.verticalScrollBar()
        y = pos.y()
        if y < AUTO_SCROLL_EDGE and bar.value() > bar.minimum():
            self._auto_timer.start()
        elif y > self.viewport().height() - AUTO_SCROLL_EDGE and bar.value() < bar.maximum():
            self._auto_timer.start()
        else:
            self._auto_timer.stop()

    def _drop_target(
        self, src: QTreeWidgetItem, pos: QPoint,
    ) -> tuple[QTreeWidgetItem | None, int, int]:
        """返回 (父节点, 插入下标, 落位线 y)；y = -1 表示当前位置不允许放。"""
        parent = src.parent()
        old = self._index_of(parent, src)
        tgt = self.itemAt(pos)
        if tgt is None:
            # 空白处：顶层可以挪到末尾；子层保持原有约束，不接受落空
            if parent is not None:
                return parent, -1, -1
            return None, self.topLevelItemCount() - 1, self.viewport().height() - 3
        if tgt is src or tgt.parent() is not parent:
            return parent, -1, -1
        rect = self.visualItemRect(tgt)
        after = pos.y() > rect.center().y()
        new = self._index_of(parent, tgt) + (1 if after else 0)
        if new > old:
            new -= 1
        return parent, new, rect.bottom() if after else rect.top()

    def _finish_drag(self) -> None:
        src = self._drag_item
        new = self._drop_index
        if src is not None and new >= 0:
            old = self._index_of(src.parent(), src)
            if new != old:
                self._move(src.parent(), old, new)
                self.moved.emit()
        self._cleanup_drag()

    def _cleanup_drag(self) -> None:
        self._auto_timer.stop()
        for widget in (self._drag_ghost, self._drop_line):
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self._drag_ghost = None
        self._drop_line = None
        self._drag_item = None
        self._drop_parent = None
        self._drop_index = -1
        self._clear_press()

    def _clear_press(self) -> None:
        self._press_pos = None
        self._press_item = None

    def _auto_scroll(self) -> None:
        if self._drag_item is None:
            self._auto_timer.stop()
            return
        bar = self.verticalScrollBar()
        y = self._last_drag_pos.y()
        before = bar.value()
        if y < AUTO_SCROLL_EDGE:
            bar.setValue(max(bar.minimum(), bar.value() - AUTO_SCROLL_STEP))
        elif y > self.viewport().height() - AUTO_SCROLL_EDGE:
            bar.setValue(min(bar.maximum(), bar.value() + AUTO_SCROLL_STEP))
        else:
            self._auto_timer.stop()
        if bar.value() != before:
            self._update_drag(self._last_drag_pos)

    def _index_of(self, parent: QTreeWidgetItem | None, item: QTreeWidgetItem) -> int:
        if parent is None:
            return self.indexOfTopLevelItem(item)
        return parent.indexOfChild(item)

    def _move(self, parent: QTreeWidgetItem | None, frm: int, to: int) -> None:
        """把同层的第 ``frm`` 项搬到第 ``to`` 项的位置。

        ``takeChild`` 一旦返回 ``None``（索引越界）就直接放弃：这个 item 已经不在
        树上了，再拿 ``None`` 去 ``insertChild`` 只会抛 TypeError，反而让指针停在
        半路。
        """
        if parent is None:
            item = self.takeTopLevelItem(frm)
            if item is None:
                return
            self.insertTopLevelItem(max(0, min(to, self.topLevelItemCount())), item)
        else:
            item = parent.takeChild(frm)
            if item is None:
                return
            parent.insertChild(max(0, min(to, parent.childCount())), item)
        self.setCurrentItem(item)

    # -- 构建辅助 --------------------------------------------------------
    def add_row(self, parent: QTreeWidgetItem | None, text: str, data: dict,
                *, suffix: str = "", switch: float | None = None) -> QTreeWidgetItem:
        item = QTreeWidgetItem([text])
        item.setData(0, ROLE_DATA, data)
        if suffix:
            item.setData(0, ROLE_SUFFIX, suffix)
        if switch is not None:
            item.setData(0, ROLE_SWITCH, switch)
        # 拖动由 ReorderTree 自己用鼠标事件实现，不再交给 QTreeWidget 的
        # ItemIsDragEnabled / ItemIsDropEnabled。

        if parent is None:
            self.addTopLevelItem(item)
        else:
            parent.addChild(item)
        return item


__all__ = [
    "INDENT",
    "ROLE_DATA",
    "ROLE_SUFFIX",
    "ROLE_SWITCH",
    "ROW_H",
    "ReorderTree",
]
