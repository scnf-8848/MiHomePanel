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

import math
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

#: 条件 / 动作行右侧的小图标按钮统一尺寸（命中区 ≥28px，padding 必须为 0）
_ICON_BTN = 30

#: 下拉里三个动作类型的顺序即列表顺序。``delay_s`` 不是新动作类型，只是
#: ``delay`` 的另一种录入刻度（值仍然折算成毫秒存，见 ``Action.unit``）。
_ACTION_LABELS = (("virtual_event", "发虚拟事件"),
                  ("delay", "延迟（毫秒）"),
                  ("delay_s", "延迟（秒）"))
#: 两种延迟规格，换算只在这两者之间做
_DELAY_KINDS = ("delay", "delay_s")
#: 换成延迟档时的默认值：两种规格都是 1 秒，切换规格不跳数量级
_DELAY_DEFAULTS = {"delay": "1000", "delay_s": "1"}
#: 秒档最多几位小数。**执行精度是 1 ms，所以 3 位刚好无损**：再多存不进去，
#: 再少（比如只留 1 位）就会让 1234 ms 之类的值重开一次就变样。改这个数要连着
#: `_delay_text` 的格式化一起看。
MAX_DELAY_DECIMALS = 3
#: 行内红字只放得下一句短的（它压在输入框右边，太长会把输入框挤窄），
#: 为什么这么规、怎么改，全挂在这张表里当 tooltip。
_HINT_TIPS = {
    "只能填整数": "毫秒档以 1 ms 为最小单位，带小数点会被拦；需要小数请换成「延迟（秒）」",
    "要填数字": "延迟只能填数字，中文、空格、单位后缀都会导致无法执行",
    f"最多 {MAX_DELAY_DECIMALS} 位小数":
        f"执行精度是 1 毫秒，第 {MAX_DELAY_DECIMALS + 1} 位以后存不进去"
        f"（1.234 秒 = 1234 毫秒）",
    "要填延迟时间": "延迟必须有值；不想等待就删掉这一行动作",
    "不能小于 0": "延迟不能是负数",
    f"不能超过 {MAX_DELAY_MS} 毫秒": f"单条延迟上限 {MAX_DELAY_MS} 毫秒（{MAX_DELAY_MS // 1000} 秒）",
    f"不能超过 {MAX_DELAY_MS // 1000} 秒": f"单条延迟上限 {MAX_DELAY_MS} 毫秒（{MAX_DELAY_MS // 1000} 秒）",
}
_MODE_LABELS = (("all", "满足全部状态条件"), ("any", "满足任一状态条件"))


def _delay_unit(kind: str) -> str:
    return "s" if kind == "delay_s" else "ms"


def _delay_text(ms: int, unit: str) -> str:
    """毫秒 → 当前刻度的文本。秒档去掉无意义的小数：2000 → "2"、1500 → "1.5"。"""
    if unit != "s":
        return str(ms)
    text = f"{ms / 1000:.{MAX_DELAY_DECIMALS}f}".rstrip("0").rstrip(".")
    return text or "0"


def _delay_ms(text: str, unit: str) -> int:
    """当前刻度的文本 → 毫秒。坏输入返回 -1（故意越界，让 validate_rule 报出来）。"""
    raw = text.strip()
    if unit == "s":
        try:
            seconds = float(raw)
        except ValueError:
            return -1
        if not math.isfinite(seconds):
            return -1
        return int(round(seconds * 1000))
    try:
        return int(raw)
    except ValueError:
        return -1


def _decimal_places(raw: str) -> int:
    """小数点后有几位。"1.234" → 3、"30" → 0、"1." → 0。"""
    return len(raw.split(".", 1)[1]) if "." in raw else 0


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


def _error_color(p: theme.Palette) -> str:
    """就地红字的颜色。色相只认主题那支 ``DANGER``（"界面里只允许一种红"），
    但同一支红在浅底 / 深底上都只有 ~3.9:1，11px 小字读着吃力：浅色压深一档、
    深色提亮一档，两边都过 4.5:1（验证脚本里钉着这条）。"""
    return theme.blend(theme.DANGER, "#000000" if not p.is_dark else "#FFFFFF",
                       0.16 if not p.is_dark else 0.24)


def _page_qss(p: theme.Palette) -> str:
    return dialog_qss(p) + f"""
    QLineEdit#Field {{
        background: {theme.panel_bg(p)}; color: {p.text}; font-size: 12px;
        border: 1px solid {p.border}; border-radius: 8px; padding: 5px 9px;
    }}
    QLineEdit#Field:focus {{ border-color: {p.accent}; }}
    QLabel#Hint {{ color: {p.text_soft}; font-size: 11px; }}
    QLabel#Warn {{ color: {_error_color(p)}; font-size: 11px; }}
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
    """一行 = 类型 + 事件名 / 延迟值 + 上移 / 下移 / 删除。

    延迟有两种录入规格（毫秒 / 秒），下拉里就是两档；**存的一律是毫秒**，
    秒档只是把输入框里的数字 ×1000，见 ``_delay_ms`` / ``_delay_text``。
    """

    def __init__(self, action: Action, palette: theme.Palette,
                 on_change: Callable[[], None], on_remove: Callable[[QWidget], None],
                 on_move: Callable[[QWidget, int], None],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._on_change = on_change
        self._loading = True
        self._kind = "virtual_event"  # 上一次的类型，_kind_changed 里判"是不是同一把尺子"
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
        # 6 个全角字（延迟（毫秒））+ 左右内边距 12/38，118 会把括号挤成省略号
        self.kind.setFixedWidth(138)

        self.value_edit = QLineEdit(self)
        self.value_edit.setObjectName("Field")
        self.value_edit.setPlaceholderText("虚拟事件名（要和米家自动化里监听的完全一致）")
        self.value_edit.textChanged.connect(self._on_text)

        # 就地报错（用户 11-13 第六条反馈）：照①「注册状态：…」的样子长在本行右侧，
        # 不占行高、不回到底部错误条。**边打边判**，不用等点保存。
        self.hint = QLabel("", self)
        self.hint.setObjectName("Warn")
        self.hint.setWordWrap(False)
        self.hint.setVisible(False)

        self.up_btn = _icon_button("↑", "上移这条动作", self)
        self.up_btn.clicked.connect(lambda: on_move(self, -1))
        self.down_btn = _icon_button("↓", "下移这条动作", self)
        self.down_btn.clicked.connect(lambda: on_move(self, 1))
        self.delete_btn = _icon_button("×", "删除这条动作", self)
        self.delete_btn.clicked.connect(lambda: on_remove(self))

        lay.addWidget(self.index_label)
        lay.addWidget(self.kind)
        lay.addWidget(self.value_edit, 1)
        lay.addWidget(self.hint)
        for btn in (self.up_btn, self.down_btn, self.delete_btn):
            lay.addWidget(btn)
        self.kind.currentIndexChanged.connect(self._kind_changed)

        # 连接已建好：setCurrentIndex 会触发 _kind_changed，构造期用 _loading 挡住，
        # 否则它会把下面刚回填的值清空。
        kind = "delay_s" if action.type == "delay" and action.unit == "s" else action.type
        self.kind.setCurrentIndex(max(0, self.kind.findData(kind)))
        # 记下当前规格：_kind_changed 靠它判断"是不是同一把尺子"，是就只换算不重置
        self._kind = self._kind_key()
        self.value_edit.setText(action.name if action.type == "virtual_event"
                                else _delay_text(action.ms, self._unit()))
        self._loading = False
        self._sync_placeholder()
        self.sync_hint()

    def _on_text(self, *_args: Any) -> None:
        """输入即校验：红字跟着光标走，不用等点保存才发现填错。"""
        self.sync_hint()
        self._on_change()

    def _kind_changed(self) -> None:
        if self._loading:
            return
        # 换类型必须换掉旧值：把「发虚拟事件」切成「延迟」后残留的事件名
        # 会被 int() 解析失败，用户看到的是"明明填了东西却报校验错"。
        # 但毫秒 ↔ 秒是同一个量，换算一下就行，别把刚填的 2000 重置成 1。
        kind = self._kind_key()
        if kind in _DELAY_KINDS and self._kind in _DELAY_KINDS:
            ms = _delay_ms(self.value_edit.text(), _delay_unit(self._kind))
            self.value_edit.setText(_delay_text(ms, _delay_unit(kind)) if ms >= 0
                                    else _DELAY_DEFAULTS[kind])
        elif kind in _DELAY_KINDS:
            self.value_edit.setText(_DELAY_DEFAULTS[kind])
        else:
            self.value_edit.setText("")
        self._kind = kind
        self._sync_placeholder()
        self.sync_hint()
        self._on_change()

    def _kind_key(self) -> str:
        return str(self.kind.currentData())

    def _is_delay(self) -> bool:
        return self._kind_key() in _DELAY_KINDS

    def _unit(self) -> str:
        """当前录入刻度："ms" / "s"。存盘与校验一律用毫秒。"""
        return _delay_unit(self._kind_key())

    def _sync_placeholder(self) -> None:
        if self._is_delay():
            self.value_edit.setPlaceholderText(
                "秒，如 30 或 1.5（最多 3 位小数）" if self._unit() == "s"
                else "毫秒，如 2000（只填整数）")
        else:
            self.value_edit.setPlaceholderText("虚拟事件名（要和米家自动化里监听的完全一致）")

    def format_error(self) -> str:
        """这一行延迟**填法**的问题，没有则返回空串。

        只能在这一层判：``Action`` 交出去的只有毫秒，core 看不出用户敲的是
        ``1.5 秒`` 还是 ``1500 毫秒``，更看不出多打了第 4 位小数。两条规矩：
        毫秒档只收整数（半毫秒没有意义）；秒档最多 3 位小数（执行精度就是 1 ms）。
        超出就**明说**，不静默四舍五入——用户专门问过精度这件事。

        文案是**行内红字**：挂在输入框右边，得短。来龙去脉进 tooltip。
        """
        if not self._is_delay():
            return ""
        raw = self.value_edit.text().strip()
        if not raw:
            return ""  # 空值交给 error_text 的「要填延迟时间」
        if self._unit() == "s":
            try:
                seconds = float(raw)
            except ValueError:
                return "要填数字"
            if not math.isfinite(seconds):
                return "要填数字"
            if _decimal_places(raw) > MAX_DELAY_DECIMALS:
                return f"最多 {MAX_DELAY_DECIMALS} 位小数"
            return ""
        # 毫秒档只收整数：负号先放过去，交给下面的范围判断说「不能小于 0」
        if not raw.lstrip("+-").isdigit():
            return "只能填整数"
        return ""

    def error_text(self) -> str:
        """行内红字的全文；空串 = 这一行合规矩。

        顺序是先具体后笼统：填法（整数 / 位数）→ 空值 → 范围。**范围也在这层判**，
        以前只有点保存后由 ``validate_rule`` 在底部说"延迟超出范围"，现在跟填法
        一样就地红字，用户不用点保存才知道填大了。
        """
        err = self.format_error()
        if err or not self._is_delay():
            return err
        raw = self.value_edit.text().strip()
        if not raw:
            return "要填延迟时间"
        ms = _delay_ms(raw, self._unit())
        if ms < 0:
            return "不能小于 0"
        if ms > MAX_DELAY_MS:
            return (f"不能超过 {MAX_DELAY_MS // 1000} 秒" if self._unit() == "s"
                    else f"不能超过 {MAX_DELAY_MS} 毫秒")
        return ""

    def sync_hint(self) -> None:
        text = self.error_text()
        self.hint.setText(text)
        self.hint.setToolTip(_HINT_TIPS.get(text, ""))
        self.hint.setVisible(bool(text))

    def set_index(self, number: int) -> None:
        self.index_label.setText(f"{number}.")

    def set_move_enabled(self, up: bool, down: bool) -> None:
        """到边界的箭头直接禁用 + tooltip 说明，别让点了没反应。"""
        self.up_btn.setEnabled(up)
        self.down_btn.setEnabled(down)
        self.up_btn.setToolTip("上移这条动作" if up else "已经是第一条")
        self.down_btn.setToolTip("下移这条动作" if down else "已经是最后一条")

    def value(self) -> Action:
        """读回一条动作。延迟**永远是毫秒**，秒档在这里折算，界面之外没人认"秒"。"""
        if self._is_delay():
            unit = self._unit()
            return Action("delay", "", _delay_ms(self.value_edit.text(), unit), unit)
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
        self.area = area  # 保存时要滚到出错的那一行
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
        # 延迟两种规格各一枚按钮（用户 11-13 要的）：等量的延迟在「秒」档里好填
        # （30 秒 vs 30000 毫秒），坏处只有一个——多占一格地方，所以三枚挤在
        # 一行里，靠文本自适应的 padding 而不是定宽，窄窗也不会被挤成省略号。
        for text, act in (
            ("＋ 发虚拟事件", Action("virtual_event", "", 0)),
            ("＋ 延迟（毫秒）", Action("delay", "", 1000)),
            ("＋ 延迟（秒）", Action("delay", "", 1000, "s")),
        ):
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
        # 填法 / 范围都只在本行报（用户 11-13 指定）：core 只拿到毫秒，看不出
        # 用户敲了什么，混进 validate_rule 只会得到"要在 0–600000 毫秒之间"这种
        # 答非所问。这里也不写底部错误条、不弹 notice——红字就在输入框右边亮着，
        # 再在下面重复一遍是噪音。
        bad = [row for row in self._action_rows if row.error_text()]
        if bad:
            for row in self._action_rows:
                row.sync_hint()  # 边打边判已经刷过，这里兜住 quiet 保存的路径
            self._set_error("")
            self._reveal_row(bad[0])
            return False
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
            # 两种规格共用一条上限，提示里两个刻度都写出来，免得秒档用户要自己换算
            self._set_error(f"单条延迟不能超过 {MAX_DELAY_MS} 毫秒"
                            f"（{MAX_DELAY_MS // 1000} 秒）")
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

    def _reveal_row(self, row: QWidget) -> None:
        """把出错的行滚进视野、焦点交回它的输入框。

        行内红字现在是**唯一**的提示（不在底部重复），所以必须保证看得见：
        ③ 里动作一多，出错那行很可能正好滚在视野外面。
        """
        self.area.ensureWidgetVisible(row, 0, 24)
        edit = getattr(row, "value_edit", None)
        if edit is not None:
            edit.setFocus()

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
