# SPDX-License-Identifier: GPL-3.0-or-later
"""电脑自动化的**单规则编辑窗**：仿米家 App「创建自动化」的三段式表单。

    ① 当以下情况发生时 —— 快捷键（录制 / 手动选键）+ 注册状态 + **冷静期**
    ② 满足全部 / 任一状态条件 —— 电脑条件行，可为空
    ③ 则执行动作 —— 发虚拟事件 / 延迟，按从上到下顺序执行，可加减与调序

**四层窗口**：点功能区「电脑自动化」弹的是 ``ui/automation_list.py`` 的规则列表，
点卡片「编辑」/「＋ 新建规则」才叠出这一层。所以本窗口只认一个 ``rule_id``
（``None`` = 新建草稿），不再自己维护规则列表、启停开关、复制 / 删除入口。
启停归列表卡片，触发历史归列表页的「日志」独立窗口。**本页没有「测试发送」**
（用户 11-13 定）：那个按钮会静默落盘并发云端事件，之后点「取消」也撤不回。

**页面只是 ``AutomationService`` 的视图**：规则、冷却表、热键注册表全在常驻对象上，
本对话框关掉会被 ``exec_with_dim_click`` 的 ``deleteLater()`` 销毁，所以这里只持有
一份「草稿」``Rule``，点保存才写回 ``RuleSet``；**保存成功即关闭**（列表页负责刷新）。

两个容易踩的交互：

* 按键录制态下按 Esc / 点遮罩 **只取消录制**，不能把整个页面关掉、丢未保存编辑；
* 关窗后主窗口**不补** ``_apply_theme()``（11-12 关窗闪烁的根因）。

实现取舍与踩坑见 ``docs/DEVELOPMENT.md``「电脑自动化：四层窗口」。
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core.automation import (
    CONDITION_TYPES,
    DEFAULT_COOLDOWN_MS,
    MAX_COOLDOWN_MS,
    MAX_DELAY_MS,
    OPS,
    Action,
    Condition,
    Rule,
    normalize,
    parse_native,
    reserved_reason,
    validate_rule,
)
from ..core.automation.hotkeys import MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, key_choices
from ..core.automation.service import AutomationService
from . import theme
from .automation_style import chip_border, chip_fill, common_qss, fit_height, tab_focus_only
from .chrome import TitleBar, WindowFrame, surface_bg, surface_border
from .combo import Combo
from .confirm import confirm, notice
from .settings import dialog_qss
from .settings_rows import SettingsCard

#: 危险 / 错误文字色。QSS 里单独给一个可读的红，深浅主题共用。
_ERROR_COLOR = "#E5534B"

#: 条件 / 动作行右侧的小图标按钮统一尺寸（命中区 ≥28px，padding 必须为 0）
_ICON_BTN = 30

#: 下拉里两个动作类型的顺序即列表顺序
_ACTION_LABELS = (("virtual_event", "发虚拟事件"), ("delay", "延迟"))
_MODE_LABELS = (("all", "满足全部状态条件"), ("any", "满足任一状态条件"))

#: Qt 的特殊键值 → Win32 VK。字母 / 数字 / F1–F12 本身与 VK 相同，不用列。
_SPECIAL_KEYS: dict[int, int] = {
    Qt.Key_Escape: 0x1B, Qt.Key_Tab: 0x09, Qt.Key_Return: 0x0D, Qt.Key_Enter: 0x0D,
    Qt.Key_Space: 0x20, Qt.Key_Backspace: 0x08, Qt.Key_Delete: 0x2E,
    Qt.Key_Insert: 0x2D, Qt.Key_Home: 0x24, Qt.Key_End: 0x23,
    Qt.Key_PageUp: 0x21, Qt.Key_PageDown: 0x22,
    Qt.Key_Up: 0x26, Qt.Key_Down: 0x28, Qt.Key_Left: 0x25, Qt.Key_Right: 0x27,
    Qt.Key_Print: 0x2C, Qt.Key_ScrollLock: 0x91, Qt.Key_Pause: 0x13,
}


def _qt_key_to_vk(key: int) -> int | None:
    if key in _SPECIAL_KEYS:
        return _SPECIAL_KEYS[key]
    if Qt.Key_F1 <= key <= Qt.Key_F12:
        return 0x70 + (key - Qt.Key_F1)
    if Qt.Key_F13 <= key <= Qt.Key_F24:
        return 0x7C + (key - Qt.Key_F13)
    # Qt 的 A–Z / 0–9 与 VK 同值；标点键不同（VK_OEM_*），交给手动选择
    if 0x30 <= key <= 0x5A and (0x30 <= key <= 0x39 or 0x41 <= key <= 0x5A):
        return key
    return None


def _qt_mods(modifiers: Qt.KeyboardModifiers) -> int:
    out = 0
    if modifiers & Qt.ControlModifier:
        out |= MOD_CONTROL
    if modifiers & Qt.AltModifier:
        out |= MOD_ALT
    if modifiers & Qt.ShiftModifier:
        out |= MOD_SHIFT
    if modifiers & Qt.MetaModifier:
        out |= MOD_WIN
    return out


def _page_qss(p: theme.Palette) -> str:
    return dialog_qss(p) + f"""
    QLineEdit#Field {{
        background: {theme.panel_bg(p)}; color: {p.text}; font-size: 12px;
        border: 1px solid {p.border}; border-radius: 8px; padding: 5px 9px;
    }}
    QLineEdit#Field:focus {{ border-color: {p.accent}; }}
    QLabel#Hint {{ color: {p.text_soft}; font-size: 11px; }}
    QLabel#Warn {{ color: {_ERROR_COLOR}; font-size: 11px; }}
    QLabel#Ok {{ color: {theme.ACCENT_GREEN}; font-size: 11px; }}
    /* 浅色主题下 text_soft 压在浅灰底上几乎看不见，小按钮统一用主文字色。
       底色从 **panel_bg** 起算：这些按钮压在 SectionCard（panel_bg）上，
       从窗口底 bg 起算深色下只有 1.01:1，等于没有按钮（见 chip_fill）。 */
    QPushButton#Tool {{
        background: {chip_fill(p, 0.12)}; color: {p.text};
        border: 1px solid {chip_border(p)}; border-radius: 8px;
        padding: 5px 13px; font-size: 12px;
    }}
    QPushButton#Tool:hover {{
        background: {chip_fill(p, 0.20)}; color: {p.accent}; border-color: {p.accent};
    }}
    QPushButton#Tool:pressed {{ background: {chip_fill(p, 0.28)}; }}
    QPushButton#Tool:focus {{ border-color: {p.accent}; }}
    /* 行右侧的 × / ↑ / ↓ 是**图标按钮**：绝不能套 #Tool（padding 6/14 会把
       26px 宽的文字整个挤没，深色下只剩一块和卡片近色的灰方块）。
       焦点只改 border-color：30px 固定宽再被 2px 描边吃掉就会把字形裁掉。 */
    QPushButton#IconTool {{
        background: {chip_fill(p, 0.16)}; color: {p.text};
        border: 1px solid {chip_border(p)};
        border-radius: 8px; padding: 0; font-size: 15px; font-weight: 600;
    }}
    QPushButton#IconTool:hover {{
        background: {p.accent_soft}; color: {p.accent}; border-color: {p.accent};
    }}
    QPushButton#IconTool:pressed {{ background: {theme.blend(p.accent_soft, p.accent, 0.28)}; }}
    QPushButton#IconTool:disabled {{
        background: transparent; color: {p.text_mute}; border-color: {p.border};
    }}
    QPushButton#Disclosure {{
        background: transparent; color: {p.text_soft};
        border: 2px solid transparent;
        font-size: 12px; padding: 4px 2px; text-align: left;
    }}
    QPushButton#Disclosure:hover {{ color: {p.accent}; }}
    QPushButton#Disclosure:pressed {{ color: {theme.blend(p.accent, p.bg, 0.32)}; }}
    QPushButton#Disclosure:checked {{ color: {p.text}; }}
    QPushButton#Keycap {{
        background: {theme.panel_bg(p)}; color: {p.text}; font-size: 13px;
        border: 1px solid {p.border}; border-radius: 8px; padding: 7px 12px;
        min-width: 150px; text-align: left;
    }}
    QPushButton#Keycap:hover {{ border-color: {p.accent}; }}
    QPushButton#Keycap[recording="true"] {{ border-color: {p.accent}; color: {p.accent}; }}
    QCheckBox {{ color: {p.text_soft}; font-size: 12px; spacing: 5px; }}
    QScrollArea {{ background: transparent; border: none; }}
    """ + common_qss(p)


def _icon_button(text: str, tip: str, parent: QWidget) -> QPushButton:
    """统一造一枚行内图标按钮：固定 30×30、padding 0、带 tooltip 与无障碍名。"""
    btn = QPushButton(text, parent)
    btn.setObjectName("IconTool")
    btn.setFixedSize(_ICON_BTN, _ICON_BTN)
    btn.setFocusPolicy(Qt.TabFocus)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setToolTip(tip)
    btn.setAccessibleName(tip)
    return btn


# ---------------------------------------------------------------------------
# 条件行 / 动作行
# ---------------------------------------------------------------------------

class ConditionRow(QWidget):
    """一行 = 类型 + 比较符 + 参数（+ 程序名）+ 删除。"""

    def __init__(self, cond: Condition, palette: theme.Palette,
                 on_change: Callable[[], None], on_remove: Callable[[QWidget], None],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._p = palette
        self._on_change = on_change
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 3, 0, 3)
        lay.setSpacing(8)

        self.kind = Combo(palette, self)
        # addItem 会发 currentIndexChanged；此时其它控件还没建好，连接必须放到最后
        with QSignalBlocker(self.kind):
            for key, (label, *_rest) in CONDITION_TYPES.items():
                self.kind.addItem(label, key)
        self.kind.setFixedWidth(160)

        self.op = Combo(palette, self)
        self.op.setFixedWidth(78)

        self.arg = QLineEdit(self)
        self.arg.setObjectName("Field")
        self.arg.setPlaceholderText("参数")
        self.arg.textChanged.connect(on_change)

        self.target = QLineEdit(self)
        self.target.setObjectName("Field")
        self.target.setPlaceholderText("程序名，如 python.exe")
        self.target.setFixedWidth(150)
        self.target.textChanged.connect(on_change)

        self.remove_btn = _icon_button("×", "删除这条条件", self)
        self.remove_btn.clicked.connect(lambda: on_remove(self))

        lay.addWidget(self.kind)
        lay.addWidget(self.op)
        lay.addWidget(self.arg, 1)
        lay.addWidget(self.target)
        lay.addWidget(self.remove_btn)
        self.kind.currentIndexChanged.connect(self._kind_changed)

        idx = self.kind.findData(cond.type)
        self.kind.setCurrentIndex(max(0, idx))
        self._fill_ops(cond.type)
        opidx = self.op.findData(cond.op)
        self.op.setCurrentIndex(max(0, opidx))
        self.arg.setText(cond.arg)
        self.target.setText(cond.target)
        self._sync_visibility()

    def _fill_ops(self, kind: str) -> None:
        with QSignalBlocker(self.op):
            self.op.clear()
            for op in CONDITION_TYPES[kind][1]:
                self.op.addItem(OPS[op], op)

    def _kind_changed(self) -> None:
        kind = str(self.kind.currentData())
        self._fill_ops(kind)
        self.arg.clear()  # 旧参数换类型后多半不再适用，别留着混进新条件
        self.arg.setPlaceholderText(CONDITION_TYPES[kind][2])
        self._sync_visibility()
        self._on_change()

    def _sync_visibility(self) -> None:
        kind = str(self.kind.currentData())
        self.target.setVisible(kind == "process_elapsed")
        self.arg.setPlaceholderText(CONDITION_TYPES[kind][2])

    def value(self) -> Condition:
        return Condition(
            str(self.kind.currentData()), str(self.op.currentData()),
            self.arg.text().strip(), self.target.text().strip(),
        )


class ActionRow(QWidget):
    """一行 = 类型 + 事件名 / 毫秒 + 上移 / 下移 / 删除。"""

    def __init__(self, action: Action, palette: theme.Palette,
                 on_change: Callable[[], None], on_remove: Callable[[QWidget], None],
                 on_move: Callable[[QWidget, int], None],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._on_change = on_change
        self._loading = True
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 3, 0, 3)
        lay.setSpacing(8)

        self.index_label = QLabel("", self)
        self.index_label.setObjectName("Hint")
        self.index_label.setFixedWidth(20)

        self.kind = Combo(palette, self)
        with QSignalBlocker(self.kind):
            for key, label in _ACTION_LABELS:
                self.kind.addItem(label, key)
        self.kind.setFixedWidth(118)

        self.value_edit = QLineEdit(self)
        self.value_edit.setObjectName("Field")
        self.value_edit.setPlaceholderText("虚拟事件名（要和米家自动化里监听的完全一致）")
        self.value_edit.textChanged.connect(on_change)

        self.up_btn = _icon_button("↑", "上移这条动作", self)
        self.up_btn.clicked.connect(lambda: on_move(self, -1))
        self.down_btn = _icon_button("↓", "下移这条动作", self)
        self.down_btn.clicked.connect(lambda: on_move(self, 1))
        self.delete_btn = _icon_button("×", "删除这条动作", self)
        self.delete_btn.clicked.connect(lambda: on_remove(self))

        lay.addWidget(self.index_label)
        lay.addWidget(self.kind)
        lay.addWidget(self.value_edit, 1)
        for btn in (self.up_btn, self.down_btn, self.delete_btn):
            lay.addWidget(btn)
        self.kind.currentIndexChanged.connect(self._kind_changed)

        # 连接已建好：setCurrentIndex 会触发 _kind_changed，构造期用 _loading 挡住，
        # 否则它会把下面刚回填的值清空。
        idx = self.kind.findData(action.type)
        self.kind.setCurrentIndex(max(0, idx))
        self.value_edit.setText(action.name if action.type == "virtual_event"
                                else str(action.ms))
        self._loading = False
        self._sync_placeholder()

    def _kind_changed(self) -> None:
        if self._loading:
            return
        # 换类型必须换掉旧值：把「发虚拟事件」切成「延迟」后残留的事件名
        # 会被 int() 解析失败，用户看到的是"明明填了东西却报校验错"。
        self.value_edit.setText("1000" if self._is_delay() else "")
        self._sync_placeholder()
        self._on_change()

    def _is_delay(self) -> bool:
        return str(self.kind.currentData()) == "delay"

    def _sync_placeholder(self) -> None:
        if self._is_delay():
            self.value_edit.setPlaceholderText("毫秒，如 2000")
        else:
            self.value_edit.setPlaceholderText("虚拟事件名（要和米家自动化里监听的完全一致）")

    def set_index(self, number: int) -> None:
        self.index_label.setText(f"{number}.")

    def set_move_enabled(self, up: bool, down: bool) -> None:
        """到边界的箭头直接禁用 + tooltip 说明，别让点了没反应。"""
        self.up_btn.setEnabled(up)
        self.down_btn.setEnabled(down)
        self.up_btn.setToolTip("上移这条动作" if up else "已经是第一条")
        self.down_btn.setToolTip("下移这条动作" if down else "已经是最后一条")

    def value(self) -> Action:
        if self._is_delay():
            try:
                ms = int(self.value_edit.text().strip())
            except ValueError:
                ms = -1  # 故意越界，让 validate_rule 报出来
            return Action("delay", "", ms)
        return Action("virtual_event", self.value_edit.text().strip(), 0)


# ---------------------------------------------------------------------------
# 对话框
# ---------------------------------------------------------------------------

class AutomationDialog(QDialog):
    """一条规则的三段式编辑器。``rule_id=None`` 表示新建草稿。"""

    def __init__(
        self,
        service: AutomationService,
        palette: theme.Palette,
        rule_id: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self._p = palette
        self._is_new = rule_id is None
        source = service.rules.get(rule_id) if rule_id else None
        self._draft: Rule = (
            source.clone() if source is not None else Rule(
                id=service.rules.new_id(), name="新规则", keys="", conditions=[],
                actions=[Action("virtual_event", "", 0)],
                cooldown_ms=DEFAULT_COOLDOWN_MS, enabled=True,
            )
        )
        self._dirty = False
        self._loading = False
        self._recording = False
        self._cond_rows: list[ConditionRow] = []
        self._action_rows: list[ActionRow] = []

        title = "新建规则" if self._is_new else "编辑规则"
        self.setWindowTitle(title)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self._frame = WindowFrame(self, resizable=True,
                                  fill=lambda: surface_bg(self._p),
                                  border=lambda: surface_border(self._p))
        self.resize(860, fit_height(900))
        self.setMinimumSize(760, 620)
        self.setStyleSheet(_page_qss(palette))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.title_bar = TitleBar(title, palette, subtitle=self._draft.name,
                                  buttons=("close",), variant="dialog")
        outer.addWidget(self.title_bar)

        body = QWidget(self)
        col = QVBoxLayout(body)
        col.setContentsMargins(18, 10, 20, 8)
        col.setSpacing(0)
        col.addWidget(self._build_editor(body), 1)
        outer.addWidget(body, 1)

        self.service.status_changed.connect(self._refresh_status)
        self._fill_form()
        # 焦点框只在 Tab 时出现（鼠标点完按钮不再留一个像"按下没弹起"的描边）
        tab_focus_only(self)

    # -- 三段式 + 固定底栏 ------------------------------------------------
    def _set_step(self, card: QWidget, number: str) -> None:
        """QSS 属性选择器要 polish 才重算；三段各给一条语义色竖条。"""
        card.setProperty("step", number)
        card.style().unpolish(card)
        card.style().polish(card)

    def _step_header(self, parent: QWidget, number: str, title: str) -> QHBoxLayout:
        """编号圆徽章 + 段落标题，替代裸的一行文字（深色下三段会连成一片）。"""
        row = QHBoxLayout()
        row.setContentsMargins(4, 6, 4, 2)
        row.setSpacing(10)
        badge = QLabel(number, parent)
        badge.setObjectName("StepBadge")
        badge.setProperty("step", number)
        badge.setFixedSize(22, 22)
        badge.setAlignment(Qt.AlignCenter)
        badge.style().unpolish(badge)
        badge.style().polish(badge)
        row.addWidget(badge)
        label = QLabel(title, parent)
        label.setObjectName("SectionTitle")
        row.addWidget(label)
        row.addStretch(1)
        return row

    def _build_editor(self, parent: QWidget) -> QWidget:
        # 滚动区只装三段式内容；状态提示和按钮在滚动区外的固定底栏。
        # 按钮若跟着内容滚，③动作一多就得先滚动才找得到保存。
        root = QWidget(parent)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

        area = QScrollArea(root)
        area.setWidgetResizable(True)
        content = QWidget(area)
        col = QVBoxLayout(content)
        col.setContentsMargins(0, 2, 8, 2)
        # 段距拉开；①里省下的行（注册状态并回快捷键行、手动选键折叠）正好补回来
        col.setSpacing(12)

        # 头部只有名称。启停归列表卡片，冷静期按用户要求挪到①触发块下面。
        head = SettingsCard(content)
        self.head_card = head
        self.name_edit = QLineEdit(content)
        self.name_edit.setObjectName("Field")
        self.name_edit.setPlaceholderText("规则名称")
        self.name_edit.textChanged.connect(self._mark_dirty)
        self.name_edit.textChanged.connect(self._sync_title)
        head.add_row("名称", self.name_edit, expand=True)
        col.addWidget(head)

        self.cooldown = QLineEdit(content)
        self.cooldown.setObjectName("Field")
        self.cooldown.setFixedWidth(90)
        self.cooldown.textChanged.connect(self._mark_dirty)

        # ① 触发
        col.addLayout(self._step_header(content, "1", "当以下情况发生时"))
        card1 = SettingsCard(content)
        self.trigger_card = card1
        self._set_step(card1, "1")

        # 注册状态并进快捷键行右侧，不再单独占一行（①里原本塞了四行）
        key_row = QWidget(content)
        krow = QHBoxLayout(key_row)
        krow.setContentsMargins(0, 0, 0, 0)
        krow.setSpacing(10)
        self.keycap = QPushButton("未设置", key_row)
        self.keycap.setObjectName("Keycap")
        self.keycap.setCursor(Qt.PointingHandCursor)
        self.keycap.clicked.connect(self._toggle_record)
        krow.addWidget(self.keycap)
        self.key_status = QLabel("", key_row)
        self.key_status.setObjectName("Hint")
        krow.addWidget(self.key_status, 1)
        card1.add_row("键盘快捷键", key_row, expand=True)

        # 手动选键是次级操作：默认收起，①块一屏只看得到主路径
        self.manual_toggle = QPushButton("或手动选键（组合键录不到时展开）", content)
        self.manual_toggle.setObjectName("Disclosure")
        self.manual_toggle.setCheckable(True)
        self.manual_toggle.setChecked(False)
        self.manual_toggle.setCursor(Qt.PointingHandCursor)
        manual = QWidget(content)
        mrow = QHBoxLayout(manual)
        mrow.setContentsMargins(0, 4, 0, 4)
        mrow.setSpacing(8)
        self.key_combo = Combo(self._p, manual)
        for key in key_choices():
            self.key_combo.addItem(key, key)
        self.key_combo.setFixedWidth(96)
        mrow.addWidget(self.key_combo)
        self._mod_boxes: dict[str, QCheckBox] = {}
        for name in ("Ctrl", "Alt", "Shift", "Win"):
            box = QCheckBox(name, manual)
            self._mod_boxes[name] = box
            mrow.addWidget(box)
        mrow.addStretch(1)
        # 所有控件都建好之后再连：_manual_keys 会遍历 _mod_boxes
        self.key_combo.currentIndexChanged.connect(self._manual_keys)
        for box in self._mod_boxes.values():
            box.stateChanged.connect(self._manual_keys)
        manual.setVisible(False)
        self.manual_toggle.toggled.connect(manual.setVisible)
        self.manual_keys_widget = manual
        manual_box = QWidget(content)
        mcol = QVBoxLayout(manual_box)
        mcol.setContentsMargins(0, 0, 0, 0)
        mcol.setSpacing(4)
        mcol.addWidget(self.manual_toggle)
        mcol.addWidget(manual)
        card1.add_unit(manual_box)

        # 冷静期是「触发后多久内不重复响应」，属于①的语义，不放在头部通用设置里。
        # 单位维持毫秒（用户 11-13 定），内部存 ms、rules.json 不变。
        card1.add_row("冷静期（毫秒）", self.cooldown,
                      sub=f"默认 {DEFAULT_COOLDOWN_MS} ms；0 = 关闭。按住不放的自动重复另有内置抑制，不在这里配",
                      expand=False)
        col.addWidget(card1)

        # ② 条件
        col.addLayout(self._step_header(content, "2", "状态条件（可以为空）"))
        card2 = SettingsCard(content)
        self._set_step(card2, "2")
        self.mode = Combo(self._p, content)
        for key, label in _MODE_LABELS:
            self.mode.addItem(label, key)
        self.mode.setFixedWidth(190)
        self.mode.currentIndexChanged.connect(self._mark_dirty)
        card2.add_row("条件关系", self.mode)
        self.cond_box = QWidget(content)
        self.cond_layout = QVBoxLayout(self.cond_box)
        self.cond_layout.setContentsMargins(0, 0, 0, 0)
        self.cond_layout.setSpacing(2)
        card2.add_unit(self.cond_box)
        self.cond_empty = QLabel("没有条件限制，快捷键按下就会执行动作", content)
        self.cond_empty.setObjectName("Hint")
        card2.add_unit(self.cond_empty)
        cond_btns = QWidget(content)
        crow = QHBoxLayout(cond_btns)
        crow.setContentsMargins(0, 4, 0, 4)
        add_cond = QPushButton("＋ 添加条件", cond_btns)
        add_cond.setObjectName("Tool")
        add_cond.clicked.connect(lambda: self._add_condition(Condition()))
        crow.addWidget(add_cond)
        crow.addStretch(1)
        card2.add_unit(cond_btns)
        col.addWidget(card2)

        # ③ 动作
        col.addLayout(self._step_header(content, "3", "则执行动作（从上到下依次执行）"))
        card3 = SettingsCard(content)
        self._set_step(card3, "3")
        self.action_box = QWidget(content)
        self.action_layout = QVBoxLayout(self.action_box)
        self.action_layout.setContentsMargins(0, 0, 0, 0)
        self.action_layout.setSpacing(2)
        card3.add_unit(self.action_box)
        action_btns = QWidget(content)
        arow = QHBoxLayout(action_btns)
        arow.setContentsMargins(0, 4, 0, 4)
        for text, act in (("＋ 发虚拟事件", Action("virtual_event", "", 0)),
                          ("＋ 延迟", Action("delay", "", 1000))):
            btn = QPushButton(text, action_btns)
            btn.setObjectName("Tool")
            btn.clicked.connect(lambda _=False, a=act: self._add_action(a))
            arow.addWidget(btn)
        arow.addStretch(1)
        card3.add_unit(action_btns)
        col.addWidget(card3)

        # 中枢说明是静态文字，放滚动区末尾即可；别占固定底栏的高度。
        self.target_label = QLabel("", content)
        self.target_label.setObjectName("Hint")
        col.addWidget(self.target_label)

        col.addStretch(1)
        area.setWidget(content)
        outer.addWidget(area, 1)

        # 固定底栏：只留错误条 + 按钮。没有「测试发送」，也没有任何结果回显，
        # 触发历史统一去列表页的「日志」独立窗口看。
        self.error_label = QLabel("", root)
        self.error_label.setObjectName("Warn")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)  # 没错误时连一行高度都不占
        outer.addWidget(self.error_label)

        footer = QWidget(root)
        frow = QHBoxLayout(footer)
        frow.setContentsMargins(0, 2, 0, 0)
        frow.setSpacing(10)
        frow.addStretch(1)
        save = QPushButton("保存", footer)
        save.setObjectName("Primary")
        save.clicked.connect(self._on_save)
        cancel = QPushButton("取消", footer)
        # 和「保存」同一个外框尺寸（ghost_qss）：以前用 #Tool，5px 13px 的内边距
        # 让它比绿色主按钮矮一圈（用户 11-13 报"右下角两个按钮尺寸没统一"）
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self.reject)
        for btn in (save, cancel):
            btn.setCursor(Qt.PointingHandCursor)
            frow.addWidget(btn)
        outer.addWidget(footer)
        return root

    # -- 表单 ------------------------------------------------------------
    def _sync_title(self, *_args: Any) -> None:
        """标题栏副标题跟名称实时同步，别停留在构造时的旧名字。"""
        self.title_bar.set_subtitle(self.name_edit.text().strip() or "未命名规则")

    def _fill_form(self) -> None:
        rule = self._draft
        self._loading = True
        self.name_edit.setText(rule.name)
        self.cooldown.setText(str(rule.cooldown_ms))
        self.mode.setCurrentIndex(max(0, self.mode.findData(rule.condition_mode)))
        self.keycap.setText(rule.keys or "未设置")
        self._set_manual_from_keys(rule.keys)
        self._clear_rows()
        for cond in rule.conditions:
            self._add_condition(cond, quiet=True)
        for act in rule.actions:
            self._add_action(act, quiet=True)
        self._renumber()
        self._loading = False
        self._dirty = False
        self._set_error("")
        self._refresh_status()

    def _clear_rows(self) -> None:
        for row in (*self._cond_rows, *self._action_rows):
            row.setParent(None)
            row.deleteLater()
        self._cond_rows.clear()
        self._action_rows.clear()
        self.cond_empty.setVisible(True)

    def _set_error(self, text: str) -> None:
        """错误条空的时候连一行高度都不占，把高度让给③动作块。"""
        self.error_label.setText(text)
        self.error_label.setVisible(bool(text))

    def _mark_dirty(self, *_args: Any) -> None:
        if self._loading:
            return
        self._dirty = True
        self._refresh_status()

    def _add_condition(self, cond: Condition, *, quiet: bool = False) -> None:
        row = ConditionRow(cond, self._p, self._mark_dirty, self._remove_condition,
                           self.cond_box)
        self.cond_layout.addWidget(row)
        self._cond_rows.append(row)
        self.cond_empty.setVisible(False)
        if not quiet:
            self._mark_dirty()

    def _remove_condition(self, row: QWidget) -> None:
        if row in self._cond_rows:
            self._cond_rows.remove(row)
            self.cond_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
            self.cond_empty.setVisible(not self._cond_rows)
            self._mark_dirty()

    def _add_action(self, action: Action, *, quiet: bool = False) -> None:
        row = ActionRow(action, self._p, self._mark_dirty,
                        self._remove_action, self._move_action, self.action_box)
        self.action_layout.addWidget(row)
        self._action_rows.append(row)
        self._renumber()
        if not quiet:
            self._mark_dirty()

    def _remove_action(self, row: QWidget) -> None:
        if row in self._action_rows:
            self._action_rows.remove(row)
            self.action_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
            self._renumber()
            self._mark_dirty()

    def _move_action(self, row: QWidget, delta: int) -> None:
        if row not in self._action_rows:
            return
        i = self._action_rows.index(row)
        j = max(0, min(len(self._action_rows) - 1, i + delta))
        if i == j:
            return
        self._action_rows.pop(i)
        self._action_rows.insert(j, row)
        self.action_layout.removeWidget(row)
        self.action_layout.insertWidget(j, row)
        self._renumber()
        self._mark_dirty()

    def _renumber(self) -> None:
        n = len(self._action_rows)
        for i, row in enumerate(self._action_rows, 1):
            row.set_index(i)
            row.set_move_enabled(up=i > 1, down=i < n)

    def _collect(self) -> Rule:
        try:
            cooldown = int(self.cooldown.text().strip())
        except ValueError:
            cooldown = -1
        self._draft.name = self.name_edit.text().strip()
        # enabled 归列表卡片的开关管，这里不碰，免得编辑一次就把停用的规则打开
        self._draft.cooldown_ms = cooldown
        self._draft.condition_mode = str(self.mode.currentData())
        self._draft.conditions = [r.value() for r in self._cond_rows]
        self._draft.actions = [r.value() for r in self._action_rows]
        return self._draft

    # -- 快捷键 ----------------------------------------------------------
    def _toggle_record(self) -> None:
        self._recording = not self._recording
        self._apply_keycap()

    def _apply_keycap(self) -> None:
        self.keycap.setProperty("recording", "true" if self._recording else "false")
        self.keycap.setText("请按下组合键…" if self._recording
                            else (self._draft.keys or "未设置"))
        self.keycap.style().unpolish(self.keycap)
        self.keycap.style().polish(self.keycap)
        if self._recording:
            self.keycap.setFocus()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if not self._recording:
            super().keyPressEvent(event)
            return
        if event.key() == Qt.Key_Escape:
            self._toggle_record()
            event.accept()
            return
        vk = _qt_key_to_vk(event.key())
        if vk is None:
            self._set_error("这个键不支持录制，请用下面的「手动选键」")
            event.accept()
            return
        keys = parse_native(_qt_mods(event.modifiers()), vk)
        if not keys:
            self._set_error("没能识别这个组合，请用「手动选键」")
            event.accept()
            return
        self._recording = False
        self._draft.keys = keys
        self._apply_keycap()
        self._set_manual_from_keys(keys)
        self._mark_dirty()
        event.accept()

    def _manual_keys(self) -> None:
        if self._loading:
            return
        parts = [name for name in ("Ctrl", "Alt", "Shift", "Win")
                 if self._mod_boxes[name].isChecked()]
        parts.append(str(self.key_combo.currentData()))
        self._draft.keys = normalize("+".join(parts))
        self.keycap.setText(self._draft.keys or "未设置")
        self._mark_dirty()

    def _set_manual_from_keys(self, keys: str) -> None:
        parts = keys.split("+") if keys else []
        with QSignalBlocker(self.key_combo):
            idx = self.key_combo.findData(parts[-1]) if parts else -1
            self.key_combo.setCurrentIndex(max(0, idx))
        for name, box in self._mod_boxes.items():
            with QSignalBlocker(box):
                box.setChecked(name in parts[:-1])

    # -- 保存 ------------------------------------------------------------
    def _save(self, *, quiet: bool = False) -> bool:
        rule = self._collect()
        errors = validate_rule(rule)
        if errors:
            self._set_error("；".join(errors[:3]))
            if not quiet:
                notice(self, "还有地方没填好", "\n".join(errors), self._p)
            return False
        if not (0 <= rule.cooldown_ms <= MAX_COOLDOWN_MS):
            self._set_error(f"冷静期要在 0–{MAX_COOLDOWN_MS} 毫秒之间")
            return False
        if any(a.type == "delay" and a.ms > MAX_DELAY_MS for a in rule.actions):
            self._set_error(f"单条延迟不能超过 {MAX_DELAY_MS} 毫秒")
            return False
        # 屏蔽 service 的信号：保存会触发 rules_changed，列表窗的刷新由 _open_editor
        # 返回后统一排一次，别在保存栈里就重建卡片。
        with QSignalBlocker(self.service):
            ok = (self.service.rules.add(rule) if self._is_new
                  else self.service.rules.update(rule))
        if not ok:
            self._set_error("这条规则已被删除或 id 冲突，请关闭后重试")
            return False
        self._is_new = False
        self.service.sync()
        self._dirty = False
        self._set_error("")
        self._refresh_status()
        return True

    def _on_save(self, *_args) -> None:  # noqa: ANN002  clicked 会带一个 bool
        """「保存」= 写盘 + **自动关闭**（用户 11-13 定）。列表页负责刷新。"""
        if self._save():
            self.accept()

    # -- 状态 ------------------------------------------------------------
    def _refresh_status(self) -> None:
        keys = self._draft.keys
        state = self.service.status_for(keys) if keys else "先设置快捷键"
        reason = reserved_reason(keys) if keys else ""
        self.key_status.setText(f"注册状态：{state}" + (f"（{reason}）" if reason else ""))
        self.key_status.setObjectName(
            "Warn" if "占用" in state or "不支持" in state or reason else "Ok")
        self.key_status.style().unpolish(self.key_status)
        self.key_status.style().polish(self.key_status)
        self.target_label.setText(f"中枢：{self.service.target_label()}　·　"
                                  "云端返回 code 0 只代表收下，末端动作请听设备反馈")

    # -- 关闭 ------------------------------------------------------------
    def reject(self) -> None:  # noqa: N802
        """录制态 / 有未保存修改时，别让「点遮罩 = reject」把页面直接关掉。"""
        if self._recording:
            self._toggle_record()
            return
        if self._dirty:
            if not confirm(self, "有未保存的修改", "要保存后关闭吗？", self._p,
                           ok_text="保存并关闭", cancel_text="直接关闭"):
                self._dirty = False
                super().reject()
                return
            if not self._save():
                return
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        # 不在这里碰主窗口主题 / 不做任何整墙重建（11-12 关窗闪烁的根因）
        event.accept()


__all__ = ["AutomationDialog"]
