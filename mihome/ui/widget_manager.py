# SPDX-License-Identifier: GPL-3.0-or-later
"""桌面挂件的设备管理窗：按宫格槽位摆设备。

挂件本身是**固定宫格**（`行×列`）：每格对应挂件上的一个位置，空槽就是空的，
不会把后面的设备挤上来。所以这里也不再是"一个有序列表 + 上移/下移"，而是：

- 上方选排列方式（1x1 / 1x2 / 1x3 / 2x2 …），换布局按行优先保留设备；
- 下方按槽位画宫格，空格中间是「＋ 添加设备」，点它打开设备选择树；
- 两格之间拖动可以交换（拖到空格就是移过去）；
- 「完成」时把布局与槽位表一起落盘，主窗口据此重建挂件。

设备选择弹窗是「家庭 → 房间 → 设备」三层树，因为挂件**跨家庭**（左栏把它放在
分组线下方，和设置同组，就是因为它不随当前家庭切换）。

数据只落在 ``Prefs.widget_layout`` / ``Prefs.widget_slots``；本窗不碰 ``Store``
的写接口，也不负责把挂件显示出来——那是主窗口的事。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .automation_style import ghost_qss, tab_focus_only
from .chrome import (
    SHADOW_PAD,
    TitleBar,
    WindowFrame,
    exec_with_dim_click,
    shadow_alpha,
    surface_bg,
    surface_border,
)
from .combo import Combo
from .tree import ReorderTree
from .widgets import draw_text
from ..core.prefs import WIDGET_COLS_MAX, WIDGET_ROWS_MAX, widget_layout_size

#: 按钮外框高度，和确认框 / 设置页对齐（见 ``confirm.BTN_H`` 的说明：别用 padding 算）
BTN_H = 32
#: 宫格单格尺寸。比挂件卡片（158×56）略大一圈：这里要放下「＋ 添加设备」和悬停 ×
CELL_W = 182
CELL_H = 64
CELL_GAP = 12
CELL_RADIUS = 12


def _qss(p: theme.Palette) -> str:
    return (
        "QDialog{background:transparent;}"
        f"QLabel{{color:{p.text};}}"
        f"QLabel#Hint{{color:{p.text_mute};font-size:12px;}}"
        f"QPushButton{{border:none;border-radius:{theme.RADIUS_BUTTON_SOFT}px;"
        f"padding:0 16px;font-size:13px;}}"
        f"QPushButton#Primary{{background:{theme.ACCENT_GREEN};color:#FFFFFF;"
        f"font-weight:600;}}"
        f"QPushButton#Primary:hover{{background:"
        f"{theme.blend(theme.ACCENT_GREEN, '#FFFFFF', 0.12)};}}"
        f"QPushButton#Primary:disabled{{background:"
        f"{theme.blend(theme.ACCENT_GREEN, p.bg, 0.55)};color:{p.text_mute};}}"
        # 树背景透明，交给外面这层卡面
        "QTreeWidget{background:transparent;border:none;}"
    ) + ghost_qss(p) + (
        "QPushButton#Ghost{padding:0 14px;}"
        f"QPushButton#Ghost:disabled{{background:{theme.blend(p.bg, p.text, 0.03)};"
        f"color:{p.text_mute};border-color:{p.border};}}"
    )


class SlotGrid(QWidget):
    """宫格槽位画布：空格点「＋ 添加设备」，两格拉动交换。"""

    add_requested = Signal(int)
    remove_requested = Signal(int)
    changed = Signal()

    def __init__(self, store, palette: theme.Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self._p = palette
        self.rows = 1
        self.cols = 3
        self.slots: list[str] = [""] * 3
        self._hover = -1
        self._press_index = -1
        self._press_pos = QPoint()
        self._dragging = False
        self._drag_pos = QPoint()
        self.setMouseTracking(True)

    # -- 数据 ------------------------------------------------------------
    def set_grid(self, rows: int, cols: int, slots: list[str]) -> None:
        self.rows, self.cols = rows, cols
        self.slots = list(slots[: rows * cols]) + [""] * max(0, rows * cols - len(slots))
        self._hover = -1
        self.updateGeometry()
        self.update()

    def resize_layout(self, rows: int, cols: int) -> None:
        """换布局：按行优先保留，多的截掉、少的补空。"""
        cells = rows * cols
        slots = [s for s in self.slots[:cells]]
        slots += [""] * (cells - len(slots))
        self.set_grid(rows, cols, slots)

    def cells(self) -> list[str]:
        return list(self.slots)

    # -- 几何 ------------------------------------------------------------
    def cell_rect(self, index: int) -> QRect:
        row, col = divmod(index, self.cols)
        return QRect(
            col * (CELL_W + CELL_GAP),
            row * (CELL_H + CELL_GAP),
            CELL_W,
            CELL_H,
        )

    def index_at(self, pos: QPoint) -> int:
        if pos.x() < 0 or pos.y() < 0:
            return -1
        col = pos.x() // (CELL_W + CELL_GAP)
        row = pos.y() // (CELL_H + CELL_GAP)
        if col >= self.cols or row >= self.rows:
            return -1
        index = row * self.cols + col
        if index >= self.rows * self.cols:
            return -1
        return index if self.cell_rect(index).contains(pos) else -1

    def sizeHint(self):  # noqa: N802
        from PySide6.QtCore import QSize

        return QSize(
            self.cols * CELL_W + (self.cols - 1) * CELL_GAP,
            self.rows * CELL_H + (self.rows - 1) * CELL_GAP,
        )

    def minimumSizeHint(self):  # noqa: N802
        # 和 sizeHint 钉死：宫格随列数变宽变窄，父布局不能用旧的"最小宽度"把它钳住
        # （否则 3 列时定下的 minWidth 会一直留着，切到 4 列窗口不涨、切回去也不缩）
        return self.sizeHint()

    # -- 绘制 ------------------------------------------------------------
    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        pal = self._p
        for index in range(self.rows * self.cols):
            rect = self.cell_rect(index)
            did = self.slots[index] if index < len(self.slots) else ""
            st = self.store.devices.get(did) if did else None
            if st is None:
                self._paint_empty_cell(p, rect, index, pal)
            else:
                self._paint_device_cell(p, rect, index, st, pal)
        if self._dragging and 0 <= self._press_index < len(self.slots):
            did = self.slots[self._press_index]
            st = self.store.devices.get(did) if did else None
            if st is not None:
                ghost = self.cell_rect(self._press_index)
                ghost.moveTo(self._drag_pos + QPoint(CELL_W // 2, CELL_H // 2) * 0)
                p.setOpacity(0.85)
                self._paint_device_cell(p, ghost, -1, st, pal, ghost_mode=True)
                p.setOpacity(1.0)
        p.end()

    def _paint_empty_cell(self, p: QPainter, rect: QRect, index: int,
                          pal: theme.Palette) -> None:
        hot = index == self._hover
        p.setPen(QPen(QColor(pal.accent if hot else pal.border), 1,
                      Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(QColor(theme.blend(pal.bg, pal.text, 0.02)))
        p.drawRoundedRect(QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5), CELL_RADIUS, CELL_RADIUS)
        draw_text(
            p, rect, "＋ 添加设备", size=12,
            color=pal.accent if hot else pal.text_mute,
            align=Qt.AlignCenter,
        )

    def _paint_device_cell(self, p: QPainter, rect: QRect, index, st,
                           pal: theme.Palette, *, ghost_mode: bool = False) -> None:
        hot = index == self._hover and not ghost_mode
        p.setPen(QPen(QColor(pal.border), 1))
        p.setBrush(QColor(theme.blend(pal.card, pal.text, 0.05)))
        p.drawRoundedRect(QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5), CELL_RADIUS, CELL_RADIUS)
        title = QRect(rect.x() + 12, rect.y() + 10, rect.width() - 40, 18)
        draw_text(p, title, st.name or st.did, size=13, color=pal.text,
                  weight=QFont.DemiBold)
        room = st.room or ""
        sub = f"{room} · 已离线" if (room and not st.online) else (
            "已离线" if not st.online else room
        )
        draw_text(p, QRect(rect.x() + 12, rect.y() + 32, rect.width() - 24, 16),
                  sub, size=11, color=pal.text_mute)
        if ghost_mode:
            return
        if hot:
            # 悬停才出现的小 ×：常驻会让整排格子看起来都是"可删"的
            p.setPen(QPen(QColor(pal.text_mute), 1.3))
            cx, cy = rect.right() - 16, rect.y() + 16
            p.drawLine(cx - 4, cy - 4, cx + 4, cy + 4)
            p.drawLine(cx - 4, cy + 4, cx + 4, cy - 4)

    # -- 交互 ------------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        pos = event.position().toPoint()
        index = self.index_at(pos)
        if index >= 0 and self.slots[index] and self._hit_remove(index, pos):
            self.remove_requested.emit(index)
            event.accept()
            return
        self._press_index = index
        self._press_pos = pos
        self._drag_pos = pos
        self._dragging = False
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position().toPoint()
        index = self.index_at(pos)
        if index != self._hover:
            self._hover = index
            self.update()
        if self._press_index >= 0 and event.buttons() & Qt.LeftButton:
            if (not self._dragging
                    and self.slots[self._press_index]
                    and (pos - self._press_pos).manhattanLength() >= 8):
                self._dragging = True
            if self._dragging:
                self._drag_pos = pos
                self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            super().mouseReleaseEvent(event)
            return
        pos = event.position().toPoint()
        src = self._press_index
        dragging = self._dragging
        self._press_index = -1
        self._dragging = False
        self.update()
        if dragging and src >= 0:
            dst = self.index_at(pos)
            if dst >= 0 and dst != src:
                self.slots[src], self.slots[dst] = self.slots[dst], self.slots[src]
                self.changed.emit()
                self.update()
            return
        index = self.index_at(pos)
        if index >= 0 and not self.slots[index]:
            self.add_requested.emit(index)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = -1
        self.update()
        super().leaveEvent(event)

    def _hit_remove(self, index: int, pos: QPoint) -> bool:
        rect = self.cell_rect(index)
        return (pos.x() >= rect.right() - 28 and pos.y() <= rect.y() + 30)


class _DevicePicker(QDialog):
    """选一台设备：家庭 → 房间 → 设备。挂件跨家庭，所以从家庭层开始。"""

    def __init__(self, store, used: set[str], palette: theme.Palette,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self._p = palette
        self._chosen = ""
        self.setWindowTitle("添加设备")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self._frame = WindowFrame(
            self,
            resizable=False,
            radius=theme.RADIUS_CARD,
            shadow=SHADOW_PAD,
            shadow_alpha=shadow_alpha(palette),
            fill=lambda: surface_bg(palette),
            border=lambda: surface_border(palette),
        )
        self.setStyleSheet(_qss(palette))

        outer = QVBoxLayout(self._frame.body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(
            TitleBar("添加设备", palette, buttons=("close",), variant="dialog",
                     frame=self._frame)
        )
        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(20, 14, 20, 16)
        col.setSpacing(10)
        outer.addWidget(body)

        self.tree = ReorderTree(palette, mode="order", draggable=False)
        self.tree.setFixedHeight(320)
        self.tree.setMinimumWidth(400)
        self.tree.itemClicked.connect(self._on_click)
        self.tree.itemDoubleClicked.connect(self._on_double)
        col.addWidget(self.tree)
        self._fill(used)

        ops = QHBoxLayout()
        ops.setSpacing(8)
        ops.addStretch(1)
        self.btn_ok = QPushButton("添加")
        self.btn_ok.setObjectName("Primary")
        self.btn_ok.setFixedHeight(BTN_H)
        self.btn_ok.setCursor(Qt.PointingHandCursor)
        self.btn_ok.setEnabled(False)
        self.btn_ok.clicked.connect(self.accept)
        cancel = QPushButton("取消")
        cancel.setObjectName("Ghost")
        cancel.setFixedHeight(BTN_H)
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        ops.addWidget(cancel)
        ops.addWidget(self.btn_ok)
        col.addLayout(ops)

        self._activate_size()
        tab_focus_only(self)

    def _fill(self, used: set[str]) -> None:
        for home in self.store.homes():
            home_item = self.tree.add_row(
                None, self.store.home_label(home.id),
                {"kind": "home", "home": home.id, "room": "", "id": home.id},
            )
            home_item.setExpanded(True)
            for room_id, room_name in self.store.room_entries(home.id):
                states = [st for st in self.store.device_states(home.id, room_id)
                          if st.did not in used]
                if not states:
                    continue
                room_item = self.tree.add_row(
                    home_item, room_name,
                    {"kind": "room", "home": home.id, "room": room_id, "id": room_id},
                    suffix=f"{len(states)} 台",
                )
                room_item.setExpanded(True)
                for st in states:
                    self.tree.add_row(
                        room_item, st.name or st.did,
                        {"kind": "device", "home": home.id, "room": room_id, "id": st.did},
                        suffix="" if st.online else "已离线",
                    )

    def _on_click(self, item: QTreeWidgetItem, _col: int) -> None:
        data = item.data(0, Qt.UserRole) or {}
        if data.get("kind") == "device":
            self._chosen = str(data.get("id") or "")
            self.btn_ok.setEnabled(bool(self._chosen))

    def _on_double(self, item: QTreeWidgetItem, _col: int) -> None:
        self._on_click(item, 0)
        if self._chosen:
            self.accept()

    def _activate_size(self) -> None:
        self._frame.body.layout().activate()
        hint = self._frame.body.sizeHint()
        pad2 = 2 * SHADOW_PAD
        self.resize(max(460, hint.width()) + pad2, hint.height() + pad2)
        self.setFixedWidth(self.width())

    def chosen(self) -> str:
        return self._chosen


class WidgetManagerDialog(QDialog):
    """管理挂件：选排列方式、按槽位增删设备、拖动换位。"""

    def __init__(self, store, palette: theme.Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self.prefs = store.prefs
        self._p = palette
        self.setWindowTitle("管理桌面挂件")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self._frame = WindowFrame(
            self,
            resizable=False,
            radius=theme.RADIUS_CARD,
            shadow=SHADOW_PAD,
            shadow_alpha=shadow_alpha(palette),
            fill=lambda: surface_bg(palette),
            border=lambda: surface_border(palette),
        )
        self.setStyleSheet(_qss(palette))

        outer = QVBoxLayout(self._frame.body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(
            TitleBar("管理桌面挂件", palette, buttons=("close",), variant="dialog",
                     frame=self._frame)
        )
        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(24, 14, 24, 18)
        col.setSpacing(12)
        outer.addWidget(body)
        self._outer = outer
        self._body = body

        head = QHBoxLayout()
        head.setSpacing(10)
        label = QLabel("排列方式", body)
        head.addWidget(label, 0, Qt.AlignVCenter)
        # 行、列各一个下拉框：两个轴独立选，才能"只加行不加列"，
        # 上限也直接从常量来（WIDGET_ROWS_MAX / WIDGET_COLS_MAX，各 1–6）
        rows, cols = widget_layout_size(self.prefs.widget_layout)
        self._layout = self.prefs.widget_layout
        self.cb_rows = Combo(palette, body)
        self.cb_cols = Combo(palette, body)
        for combo, top, current in (
            (self.cb_rows, WIDGET_ROWS_MAX, rows),
            (self.cb_cols, WIDGET_COLS_MAX, cols),
        ):
            combo.setFixedWidth(72)
            for value in range(1, top + 1):
                combo.addItem(str(value), value)
            combo.setCurrentIndex(current - 1)
        head.addWidget(self.cb_rows, 0, Qt.AlignVCenter)
        unit_row = QLabel("行 ×", body)
        head.addWidget(unit_row, 0, Qt.AlignVCenter)
        head.addWidget(self.cb_cols, 0, Qt.AlignVCenter)
        head.addWidget(QLabel("列", body), 0, Qt.AlignVCenter)
        head.addStretch(1)
        col.addLayout(head)

        self.grid = SlotGrid(store, palette, body)
        self.grid.set_grid(rows, cols, list(self.prefs.widget_slots))
        self.cb_rows.currentIndexChanged.connect(self._on_shape)
        self.cb_cols.currentIndexChanged.connect(self._on_shape)
        self.grid.add_requested.connect(self._on_add)
        self.grid.remove_requested.connect(self._on_remove)
        self.grid.changed.connect(self._activate_size)
        col.addWidget(self.grid)

        hint = QLabel("点空格添加设备；两格之间拖动可以交换位置。空着的格子会原样留在挂件上。")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        col.addWidget(hint)

        ops = QHBoxLayout()
        ops.setSpacing(8)
        ops.addStretch(1)
        done = QPushButton("完成")
        done.setObjectName("Primary")
        done.setFixedHeight(BTN_H)
        done.setCursor(Qt.PointingHandCursor)
        done.clicked.connect(self.accept)
        ops.addWidget(done)
        col.addLayout(ops)

        self._activate_size()
        tab_focus_only(self)

    # -- 交互 ------------------------------------------------------------
    def _on_shape(self, _index: int = 0) -> None:
        rows = int(self.cb_rows.currentData() or 1)
        cols = int(self.cb_cols.currentData() or 1)
        layout = f"{rows}x{cols}"
        if layout == self._layout:
            return
        self._layout = layout
        self.grid.resize_layout(rows, cols)
        self._activate_size()

    def _on_add(self, index: int) -> None:
        used = set(self.grid.cells()) - {""}
        dlg = _DevicePicker(self.store, used, self._p, self)
        if exec_with_dim_click(dlg) != QDialog.Accepted:
            return
        did = dlg.chosen()
        if not did:
            return
        cells = self.grid.cells()
        if did in cells:
            cells[cells.index(did)] = ""
        cells[index] = did
        self.grid.set_grid(self.grid.rows, self.grid.cols, cells)
        self._activate_size()

    def _on_remove(self, index: int) -> None:
        cells = self.grid.cells()
        if 0 <= index < len(cells):
            cells[index] = ""
            self.grid.set_grid(self.grid.rows, self.grid.cols, cells)

    def _activate_size(self) -> None:
        # 宫格刚换过行/列，父布局的 sizeHint 要到下一轮事件循环才更新；
        # 先算一次（首帧），再补一次，避免"窗口永远慢一步/切回旧列数不缩"
        self._apply_size()
        QTimer.singleShot(0, self._apply_size)

    def _apply_size(self) -> None:
        # 窗口没有自己的布局（内容挂在 ``WindowFrame.body`` 上），adjustSize() 算不出
        # 东西，只能按面板的 sizeHint 显式加回两圈阴影留白（同 confirm.py）
        self._outer.activate()
        self._body.layout().activate()
        hint = self._frame.body.sizeHint()
        pad2 = 2 * SHADOW_PAD
        self.resize(max(560, hint.width()) + pad2, hint.height() + pad2)

    # -- 结果 ------------------------------------------------------------
    def accept(self) -> None:  # noqa: N802
        self.prefs.set_widget_layout(self._layout)
        self.prefs.set_widget_slots(self.grid.cells())
        super().accept()


def manage_widget_devices(store, palette: theme.Palette,
                          parent: QWidget | None = None) -> bool:
    """打开管理窗；返回 True 表示用户点了「完成」（布局与槽位已落盘）。"""
    dlg = WidgetManagerDialog(store, palette, parent)
    return exec_with_dim_click(dlg) == QDialog.Accepted


__all__ = ["SlotGrid", "WidgetManagerDialog", "manage_widget_devices"]
