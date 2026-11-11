"""风扇 2×1 的 body：两列 × 两行的格子。

为什么不用 ``CardBody``（一个槽位一行竖着堆）：风扇要同时摆四件事，
两行的内容带只够两行，只能左右分栏：

    ┌─────────────────────┬─────────────────────┐
    │ 左右扫风      [ 开关]│ 模式    [自然风|直吹风]│
    │ 角度      [− 60°  +] │ 风速   [0–100 滑条]  │
    └─────────────────────┴─────────────────────┘

**列有语义**：左列永远是"往哪儿吹"（扫风开关 + 角度），右列是"吹多大"
（模式 + 风速）。所以某台设备缺一项时，那一格空着，别的格子**不挪位置**——
挪了之后同一台设备换个型号，用户就得重新找一遍。

**哪些是品类级、哪些是型号级**（这条容易糊）：

* 品类级（``catalog`` 的 fan 画像）：这些功能"可能都有"，绑不上就跳过。
  有没有上下扫风、有没有模式、风速是档位还是连续量，全由规格回答，
  不写死型号、不按名字猜。
* 型号级（``adapters/dmaker.py`` 的 ``speed_by_mode``）：两个风速属性**同时
  存在**时哪个算数。这是固件语义，规格里看不出来，只能按型号登记。

右下的"风速"格因此是个双选格：按当前模式显示滑条或胶囊，同格切换不改变布局
（两个控件高度不同，各自在本格内垂直居中）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import QWidget

from . import theme
from .card_controls import (
    BODY_W,
    ROW_H,
    CardControl,
    CardSlider,
    draw_text,
    make_control,
)

#: 左右两列的比例。左列要放"四字标签 + 44px 开关"（≈102），以及"角度 + 五段
#: 轨道"（24 + 6 + 5×24 ≈ 150）；右列要放"两字标签 + 两段轨道"（≈90）和一条
#: 能拖的滑条（≈120）。取 150/152 是让**五段角度**能放进左列（每段 24px 时
#: "140" 用 11px 刚好），右列仍够放模式与风速。
COL_RATIO = (168, 134)
#: 两列之间、同一列两行之间的间距
CELL_GAP_X = 10
CELL_GAP_Y = 4
#: 格子里的标签与控件之间
LABEL_GAP = 6
#: 同一格里"按模式二选一"的两个槽位（右下角的风速格）。写成常量而不是
#: ``len(slots) > 1``：左上那格也有两个候选（左右/上下扫风），但那不是二选一的
#: 运行时切换，而是"哪台设备有哪个"的绑定兜底。
SPEED_SLOTS = ("fan_speed", "fan_level")
#: 标签最多占多宽（四字 12px ≈ 48）。实际按文字量出来，取小值：
#: "角度"两个字只占 24，多出来的宽度全给控件。
LABEL_MAX = 52
LABEL_PX = 12


@dataclass
class _Cell:
    """一格：一个标签 + 一到多个候选控件（右下的风速格有两个）。"""

    col: int
    slots: tuple[str, ...]
    #: 二选一格：运行时按模式显示其中一个（见 ``SPEED_SLOTS``）
    dual: bool = False
    label: str = ""
    label_w: int = 0
    widgets: list[tuple[str, CardControl]] = field(default_factory=list)

    def current(self) -> CardControl | None:
        for widget in self._visible():
            return widget
        return None

    def _visible(self) -> list[CardControl]:
        return [w for _, w in self.widgets if not w.isHidden()]

    def height(self) -> int:
        ctl = self.current()
        return ctl.height() if ctl is not None else 0

    def pick(self, slot: str) -> bool:
        """只显示 ``slot`` 对应的控件；返回是否发生了变化。"""
        changed = False
        for name, widget in self.widgets:
            want = name == slot
            if widget.isHidden() == want:
                widget.setVisible(want)
                changed = True
        return changed


class FanBody(QWidget):
    """风扇卡片的 2×1 内容带（接口与 ``CardBody`` 一致，外壳不用分支）。"""

    value_committed = Signal(int, int, object)
    previewed = Signal()
    menu_requested = Signal(QPoint)

    def __init__(
        self,
        style: object,
        state: object,
        palette: theme.Palette,
        accent: str,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self._palette = palette
        self._accent = accent
        self.controls: dict[str, CardControl] = {}
        self._cells: list[_Cell] = []
        self._labels: list[tuple[QRect, str]] = []
        self._build(state)

    # -- 构建 ------------------------------------------------------------
    #: 格子表：``(列, 槽位候选)``，列内按从上到下。**候选只写一个**的格子
    #: 绑不上就空着；右下的风速格写两个，按模式挑（见 ``_sync_speed``）。
    #: 上下扫风与左右扫风同时存在时只上左右——四个格子放不下五个控件，
    #: 上下扫风留在详情页（选择理由见模块开头）。
    CELLS: tuple[tuple[int, tuple[str, ...]], ...] = (
        (0, ("swing_h", "swing_v")),
        (0, ("swing_angle",)),
        (1, ("mode",)),
        (1, ("fan_speed", "fan_level")),
    )

    def _build(self, state: object) -> None:
        card = state.card
        for col, slots in self.CELLS:
            cell = _Cell(col=col, slots=slots, dual=tuple(slots) == SPEED_SLOTS)
            # 建控件时还不知道真实宽度（Qt 还没给几何），用尺寸档定死的标称宽
            col_w = self._column_x(BODY_W)[col][1]
            for slot in slots:
                ctl = card.bound(slot)
                if ctl is None or ctl.prop is None:
                    continue
                # 标签宽度先按"候选里最长那个"估，量控件可用宽度时够用即可
                label_w = min(LABEL_MAX, self._label_width(ctl.label))
                widget = self._make(ctl, col_w - label_w - LABEL_GAP)
                if widget is None:
                    continue
                cell.widgets.append((slot, widget))
                self.controls[slot] = widget
                if not cell.label:
                    cell.label = getattr(ctl, "label", "") or ctl.prop.display_label
            if not cell.widgets:
                continue
            # 标签统一按这一格**第一个候选**的名字量宽度：风速格的两个候选
            # 都叫"风速"，量出来一样。
            cell.label_w = self._label_width(cell.label)
            self._cells.append(cell)
            if len(cell.widgets) > 1:
                # 双选格：先全隐藏，由 ``_sync_speed`` 按模式点亮一个
                for _, widget in cell.widgets:
                    widget.setVisible(False)
        self._sync_speed(state, force=True)

    def _label_width(self, text: str) -> int:
        font = QFont(self.font())
        font.setPixelSize(LABEL_PX)
        return min(LABEL_MAX, QFontMetrics(font).horizontalAdvance(text) + 4)

    def _make(self, ctl: object, avail: int) -> CardControl | None:
        """这一格画什么——**转给控件库**（``card_controls.make_control``）。

        以前这里自己写了一遍选型（滑条/胶囊/步进器 + "放不下就换步进器"），
        ``CardBody._pick`` 也写了一遍，两份阈值已经开始漂。现在只提供两个
        参数：``avail``（这一格留给控件的宽度）和 ``label=""``（格子自己画
        名字，见 ``CardControl`` 的注释）。
        """
        widget = make_control(
            ctl, self._palette, self._accent,
            avail=avail, label="", compact=True, centered=True, parent=self,
        )
        if widget is None:
            return None
        # 接线：控件只负责说"用户把它改成了这个值"，siid/piid 由 body 补。
        #
        # 这一步以前漏了——``_build`` 把控件塞进 ``self.controls`` 就完事，
        # 于是风扇卡上**每一个**控件都只改自己的视觉、一个写请求也没发出去：
        # 点模式 UI 切过去了、设备不动，拖风速同理（2026-11-12 用户报）。
        # 详情页没事，因为那边是 ``ControlRow`` 自己接信号再写 store，另一条路。
        # 教训写进 ``docs/DEVELOPMENT.md``「卡片控件库」：**零件能单测不等于
        # 接好了**，验证脚本必须从 body 的 ``value_committed`` 这头断言。
        if isinstance(widget, CardSlider):
            widget.previewed.connect(self.previewed)
        prop = ctl.prop
        widget.committed.connect(
            lambda value, p=prop: self.value_committed.emit(p.siid, p.piid, value)
        )
        return widget

    # -- 布局 ------------------------------------------------------------
    @staticmethod
    def _column_x(width: int) -> tuple[tuple[int, int], tuple[int, int]]:
        """两列的 ``(起点 x, 宽度)``。按 ``COL_RATIO`` 分配，扣掉列间距。"""
        avail = width - CELL_GAP_X
        total = COL_RATIO[0] + COL_RATIO[1]
        w0 = avail * COL_RATIO[0] // total
        return ((0, w0), (w0 + CELL_GAP_X, avail - w0))

    def _layout(self) -> None:
        # 构造期宽度还是 0（Qt 还没给几何），等 ``resizeEvent`` 再来一次
        if not self._cells or self.width() < 60:
            return
        cols = self._column_x(self.width())
        self._labels = []
        for col, (x, width) in enumerate(cols):
            cells = [c for c in self._cells if c.col == col and c.current() is not None]
            # 每格固定占一行 36（``ROW_H``），控件在自己的行里居中：胶囊 26 高的
            # 那格上下各留 5，和 36 高的开关/滑条**基线对齐**。否则两列的"第一行"
            # 会差 5px，一眼就能看出来没对齐。
            total = len(cells) * ROW_H + CELL_GAP_Y * max(0, len(cells) - 1)
            # 竖向居中：一格的列（设备缺了另一项）不会贴着顶边
            y = (self.height() - total) // 2
            for cell in cells:
                ctl = cell.current()
                assert ctl is not None
                h = ctl.height()
                ctl.setGeometry(
                    x + cell.label_w + LABEL_GAP,
                    y + (ROW_H - h) // 2,
                    max(40, width - cell.label_w - LABEL_GAP),
                    h,
                )
                self._labels.append(
                    (QRect(x, y, cell.label_w, ROW_H), cell.label)
                )
                y += ROW_H + CELL_GAP_Y
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._layout()

    def paintEvent(self, event) -> None:  # noqa: N802
        if not self._labels:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        for rect, text in self._labels:
            draw_text(
                p,
                rect,
                text,
                size=LABEL_PX,
                color=self._palette.text_soft,
                align=Qt.AlignLeft | Qt.AlignVCenter,
            )
        p.end()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(312, 78)

    # -- 数据 ------------------------------------------------------------
    def _sync_speed(self, state: object, *, force: bool = False) -> None:
        """按当前模式决定右下角显示滑条还是胶囊。

        判据只来自 ``card.speed_by_mode``（型号级，见 ``adapters/dmaker.py``）：
        没登记这条规则（或模式值没读到）时，退到"档位优先、否则连续量"的通用
        顺序——不猜，也不因为一个未知模式值把风速整块藏起来。
        """
        card = state.card
        cell = next((c for c in self._cells if c.dual), None)
        if cell is None:
            return
        mode_ctl = card.bound("mode")
        mode_value = state.get_prop(mode_ctl.prop) if mode_ctl is not None else None
        slot = card.speed_slot(mode_value)
        if slot is None or slot not in dict(cell.widgets):
            bound = [name for name, _ in cell.widgets if card.bound(name) is not None]
            if not bound:
                return
            # 档位优先：有档位的设备按"档位是主、连续量是辅"展示，和详情页一致
            slot = "fan_level" if "fan_level" in bound else bound[0]
        if force or not any(
            name == slot and not w.isHidden() for name, w in cell.widgets
        ):
            cell.pick(slot)
            self._layout()

    def sync(self, state: object) -> None:
        self._sync_speed(state)
        for slot, widget in self.controls.items():
            ctl = state.card.bound(slot)
            prop = ctl.prop if ctl is not None else None
            value = state.get_prop(prop) if prop is not None else None
            if slot == "mode":
                active = state.mode_active_value()
                if active is not None:
                    widget.set_active(active)
                elif value is not None:
                    widget.set_value(value)
            elif value is not None:
                widget.set_value(value)
            writable = bool(prop is not None and prop.writeable and ctl and ctl.write)
            widget.setEnabled(bool(state.online and writable))
            widget.set_dimmed(not state.any_on)
        self._layout()

    def apply_theme(self, palette: theme.Palette, accent: str) -> None:
        self._palette = palette
        self._accent = accent
        for widget in self.controls.values():
            widget.apply_theme(palette, accent)
        self.update()

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        self.menu_requested.emit(event.globalPos())
        event.accept()


__all__ = ["COL_RATIO", "FanBody"]
