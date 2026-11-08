"""卡片墙的跨格网格布局。

``FlowLayout`` 只能放等高的小卡：一行高度 = 该行最高项，2×2 卡片一进来，
同一行剩下的一列就会被撑成一条空洞，下一行也跟着错位。卡片墙现在允许
1×1 / 2×1 / 2×2 三种尺寸，所以换成一个按**单位格**占位的布局：

* 单位格 = ``CARD_MIN_W × CARD_H``，格间距 = ``CARD_GAP``；
* 卡片占 ``span`` 列 × ``rows`` 行，尺寸由它自己的 ``grid_span/grid_rows`` 决定；
* 从左上起**行优先首次适配**，放不下就换行；后面的小卡会回填前面的空位，
  所以 2×2 旁边剩下的单格不会被漏掉；
* 宽度不够时列数自动减少（2–6 列都成立）。少于 3 列时把 2×2 降成 1×1，
  否则 2×2 会占满整行、把房间标题下的空间读成一张孤零零的大图。

**为拖拽留的接口**（主界面拖动排序）：

* 卡片顺序只有一处事实来源——布局里的 ``_items`` 列表，也就是 ``prefs`` 的排序；
* ``slot_of(widget)`` / ``drop_index(pos, source)`` 给出「拖到哪个位置」的
  插入索引（已排除被拖卡片自身，和 ``list.insert`` 的语义一致）；
* ``move_item(index_from, index_to)`` 只移动列表项、不删不建控件，
  重排后 ``invalidate()`` 由布局自己完成。拖动过程中让被拖卡片继续占着
  原来的 ``span × rows``（画成占位框），网格就不会崩。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QMargins, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QLayoutItem, QWidget

from . import theme


@dataclass(frozen=True)
class Slot:
    """一张卡片在网格里的位置；``rect`` 是它最终分到的像素矩形。"""

    item: QLayoutItem
    col: int
    row: int
    span: int
    rows: int
    rect: QRect

    @property
    def widget(self) -> QWidget | None:
        return self.item.widget()


class CardGridLayout(QLayout):
    """按单位格占位的卡片网格；接口刻意保持和 ``FlowLayout`` 一致。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        margin: int = 0,
        h_gap: int = theme.CARD_GAP,
        v_gap: int = theme.CARD_GAP,
    ) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._h_gap = max(0, int(h_gap))
        self._v_gap = max(0, int(v_gap))
        self._placement: list[Slot] = []
        self._cols = 1
        self._laying_out = False
        self.setContentsMargins(margin, margin, margin, margin)

    # -- QLayout 必需接口 -------------------------------------------------
    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientations:  # noqa: N802
        return Qt.Orientations()

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._measure(width).height()

    def sizeHint(self) -> QSize:  # noqa: N802
        return self._max_item_size()

    def minimumSize(self) -> QSize:  # noqa: N802
        return self._max_item_size()

    def _max_item_size(self) -> QSize:
        """和 ``FlowLayout`` 一样取「最大的一张卡」作为首选尺寸。

        不能固定回 166×168：那样滚动区会按一张小卡缩到最窄，列数恒为 1，
        2×1 / 2×2 永远轮不到正常宽度。取最大项也能保证窗口默认打开时至少
        放得下用户选过的最大卡。
        """
        size = QSize(theme.CARD_MIN_W, theme.CARD_H)
        for item in self._items:
            w = item.widget()
            if w is None or w.isHidden():
                continue
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    # -- 几何 -------------------------------------------------------------
    def _visible_items(self) -> list[QLayoutItem]:
        # 搜索 / 折叠用 setVisible 隐藏卡片；布局要跳过它们，但不能删
        out: list[QLayoutItem] = []
        for item in self._items:
            w = item.widget()
            if w is None or w.isHidden():
                continue
            out.append(item)
        return out

    def _cell_size(self) -> tuple[int, int]:
        return theme.CARD_MIN_W, theme.CARD_H

    @staticmethod
    def _grid_size(widget: QWidget) -> tuple[int, int]:
        span = int(getattr(widget, "grid_span", 1) or 1)
        rows = int(getattr(widget, "grid_rows", 1) or 1)
        return max(1, span), max(1, rows)

    def columns_for(self, width: int) -> int:
        """这个宽度能放下几列（至少 1 列）。"""
        margins: QMargins = self.contentsMargins()
        avail = width - margins.left() - margins.right()
        cw, _ = self._cell_size()
        if avail <= 0:
            return 1
        return max(1, (avail + self._h_gap) // (cw + self._h_gap))

    def _effective(self, item: QLayoutItem, cols: int) -> tuple[int, int]:
        w = item.widget()
        # 取**样式声明的尺寸**，不是卡片当前的 grid_span——后者可能已经被
        # 上一次窄窗口降级覆盖成 1×1，拿它重算会导致"变窄一次就再也变不回来"。
        style = getattr(w, "style", None) if w is not None else None
        if style is not None:
            span, rows = int(style.span), int(style.rows)
        else:
            span, rows = self._grid_size(w) if w is not None else (1, 1)
        span, rows = max(1, span), max(1, rows)
        # 三列以下不放 2×2：它会占掉两列两行，在窄窗口里读成一张孤图。
        # 2×1 仍然保留——两列正好放得下，滑条也不需要第三列。
        if rows > 1 and cols < 3:
            return 1, 1
        span = min(span, cols)
        return span, rows

    def _place(self, cols: int) -> list[tuple[QLayoutItem, int, int, int, int]]:
        occupied: set[tuple[int, int]] = set()
        placed: list[tuple[QLayoutItem, int, int, int, int]] = []
        for item in self._visible_items():
            span, rows = self._effective(item, cols)
            col, row = self._find_slot(occupied, cols, span, rows)
            for dr in range(rows):
                for dc in range(span):
                    occupied.add((row + dr, col + dc))
            placed.append((item, col, row, span, rows))
        return placed

    @staticmethod
    def _find_slot(
        occupied: set[tuple[int, int]], cols: int, span: int, rows: int
    ) -> tuple[int, int]:
        """行优先首次适配；``span`` 已保证 ≤ ``cols``。"""
        row = 0
        while True:
            for col in range(cols - span + 1):
                cells = [
                    (row + dr, col + dc)
                    for dr in range(rows)
                    for dc in range(span)
                ]
                if all(cell not in occupied for cell in cells):
                    return col, row
            row += 1

    def _measure(self, width: int) -> QSize:
        cols = self.columns_for(width)
        placed = self._place(cols)
        if not placed:
            return QSize(0, 0)
        cw, ch = self._cell_size()
        bottom = 0
        right = 0
        for _item, col, row, span, rows in placed:
            bottom = max(bottom, row + rows)
            right = max(right, col + span)
        margins = self.contentsMargins()
        h = bottom * ch + max(0, bottom - 1) * self._v_gap
        w = right * cw + max(0, right - 1) * self._h_gap
        return QSize(
            w + margins.left() + margins.right(),
            h + margins.top() + margins.bottom(),
        )

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        if self._laying_out:
            return
        self._laying_out = True
        try:
            super().setGeometry(rect)
            self._apply(rect)
        finally:
            self._laying_out = False

    def _apply(self, rect: QRect) -> None:
        margins = self.contentsMargins()
        cols = self.columns_for(rect.width())
        self._cols = cols
        cw, ch = self._cell_size()
        placed = self._place(cols)
        self._placement = []
        for item, col, row, span, rows in placed:
            w = item.widget()
            if w is not None and hasattr(w, "set_grid_cells"):
                # 2×2 在窄窗口里降成 1×1 时，卡片自己的固定尺寸也要跟着改；
                # 否则布局按 1×1 算、控件仍是 2×2，就会盖到右边。
                w.set_grid_cells(span, rows)
            x = rect.x() + margins.left() + col * (cw + self._h_gap)
            y = rect.y() + margins.top() + row * (ch + self._v_gap)
            r = QRect(
                x,
                y,
                span * cw + (span - 1) * self._h_gap,
                rows * ch + (rows - 1) * self._v_gap,
            )
            item.setGeometry(r)
            self._placement.append(Slot(item, col, row, span, rows, r))

    # -- 拖拽排序接口 -----------------------------------------------------
    @property
    def placement(self) -> list[Slot]:
        """上一次 ``setGeometry`` 的结果，按卡片顺序排列。"""
        return list(self._placement)

    def widgets(self) -> list[QWidget]:
        """布局里的卡片顺序（含被搜索隐藏的卡，持久化要用完整顺序）。"""
        return [it.widget() for it in self._items if it.widget() is not None]

    def index_of(self, widget: QWidget) -> int:
        for i, item in enumerate(self._items):
            if item.widget() is widget:
                return i
        return -1

    def drop_index(self, pos: QPoint, source: QWidget) -> int:
        """把 ``pos``（布局坐标）换算成插入索引。

        按"阅读顺序"数：位于某张卡片中线以下 / 同一行中线以右，就算排到它后面。
        返回的是**排除被拖卡片之后**的索引，可直接喂给 ``move_item``。
        """
        idx = 0
        for slot in self._placement:
            if slot.widget is None or slot.widget is source:
                continue
            r = slot.rect
            if pos.y() > r.center().y():
                idx += 1
            elif abs(pos.y() - r.center().y()) <= r.height() / 4 and pos.x() > r.center().x():
                idx += 1
        return idx

    def move_item(self, index_from: int, index_to: int) -> bool:
        """把第 ``index_from`` 项移到 ``index_to``（按移除后的列表计）。

        只重排布局项，不删不建控件；拖动过程中反复调用也只改顺序。
        """
        from .dnd import move_in_list

        moved = move_in_list(self._items, index_from, index_to)
        if moved:
            self.invalidate()
        return moved

    @property
    def columns(self) -> int:
        return self._cols


__all__ = ["CardGridLayout", "Slot"]
