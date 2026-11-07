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

from PySide6.QtCore import QModelIndex, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QDrag, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QStyle,
    QStyledItemDelegate,
    QTreeWidget,
    QTreeWidgetItem,
    QWidget,
)

from . import theme

ROW_H = 32
INDENT = 16
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

        if mode == "order":
            self.setDragEnabled(True)
            self.setAcceptDrops(True)
            self.setDropIndicatorShown(True)
            self.setDragDropMode(QAbstractItemView.DragDrop)
            self.setDefaultDropAction(Qt.MoveAction)

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self._delegate.set_palette(palette)
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
    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            pos = event.position().toPoint()
            item = self.itemAt(pos)
            if item is not None:
                rect = self.visualItemRect(item)
                if item.childCount() and _chevron_hit(rect, self._depth(item)).contains(pos):
                    item.setExpanded(not item.isExpanded())
                    self.viewport().update()
                    return
                if item.data(0, ROLE_SWITCH) is not None and _switch_rect(rect).contains(pos):
                    self.toggle_requested.emit(dict(item.data(0, ROLE_DATA) or {}))
                    return
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position().toPoint()
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

    # -- 拖动：只允许同层内重排 --------------------------------------------
    def startDrag(self, supported_actions) -> None:  # noqa: N802
        """自己起拖动：**不让 Qt 在松手后删掉源行**。

        ``QAbstractItemView.startDrag`` 在 ``drag.exec()`` 返回 ``MoveAction`` 时会
        调 ``clearOrRemove()``，按**当前选择集**删行。而这里的 ``dropEvent`` 已经
        自己把 item 搬到新位置了，选择集跟着 item 走——那一下就正好把刚放下的那行
        再删一遍。用户看到的是"拖完这个房间就从列表里没了"。

        所以这里只负责把 mime 和拖影递出去，移动完全交给 ``dropEvent``。
        """
        indexes = self.selectedIndexes()
        if not indexes:
            return
        model = self.model()
        mime = model.mimeData(indexes) if model is not None else None
        if mime is None:
            return
        drag = QDrag(self)
        drag.setMimeData(mime)
        item = self.currentItem()
        if item is not None:
            rect = self.visualItemRect(item)
            if rect.isValid() and not rect.isEmpty():
                drag.setPixmap(self.viewport().grab(rect))
                drag.setHotSpot(QPoint(rect.width() // 2, rect.height() // 2))
        drag.exec(Qt.MoveAction)

    def _same_parent(self, event) -> bool:
        src = self.currentItem()
        if src is None:
            return False
        tgt = self.itemAt(event.position().toPoint())
        if tgt is None:
            # 落到空白处 = 挪到这一层的末尾，只有顶层能这么干
            return src.parent() is None
        return tgt is not src and tgt.parent() is src.parent()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if self._mode != "order" or not self._same_parent(event):
            event.ignore()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        if self._mode != "order" or not self._same_parent(event):
            event.ignore()
            return
        src = self.currentItem()
        if src is None:
            event.ignore()
            return
        parent = src.parent()
        pos = event.position().toPoint()
        tgt = self.itemAt(pos)
        old = self._index_of(parent, src)
        if tgt is None or tgt is src:
            new = parent.childCount() - 1 if parent is not None else self.topLevelItemCount() - 1
        else:
            anchor = self.visualItemRect(tgt)
            new = self._index_of(parent, tgt) + (1 if pos.y() > anchor.center().y() else 0)
            if new > old:
                new -= 1
        if new != old:
            self._move(parent, old, new)
        event.accept()
        self.moved.emit()

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
        item.setFlags(item.flags() | Qt.ItemIsDragEnabled | Qt.ItemIsDropEnabled)
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
