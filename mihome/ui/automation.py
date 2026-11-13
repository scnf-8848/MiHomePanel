# SPDX-License-Identifier: GPL-3.0-or-later
"""电脑自动化的**单规则编辑窗**：仿米家 App「创建自动化」的三段式表单。

    ① 当以下情况发生时 —— 快捷键（录制 / 手动选键）+ 注册状态 + **冷静期**
    ② 满足全部 / 任一状态条件 —— 电脑条件行，可为空
    ③ 则执行动作 —— 发虚拟事件 / 延迟 / 发音箱指令，按从上到下顺序执行，可加减与调序

② 和 ③ 都只有**一枚**「＋ 添加…」按钮（11-13 用户第八条）：加出来的那一行落在空档
「-」上，用户自己在下拉里挑类型，② 和 ③ 因此长得一样。空档是**还没选**，不是"类型
写错了"——不选就点保存会被拦（见 ``ActionRow.needs_kind``），但刚加进来不先亮红字。

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
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core import speaker
from ..core.automation import (
    ACTION_TYPES,
    CONDITION_TYPES,
    DEFAULT_COOLDOWN_MS,
    MAX_COOLDOWN_MS,
    MAX_DELAY_MS,
    MAX_SPEAK_CHARS,
    OPS,
    TRIGGER_TYPES,
    Action,
    Condition,
    Rule,
    Trigger,
    normalize,
    parse_native,
    reserved_reason,
    validate_rule,
)
from ..core.automation.hotkeys import MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, key_choices
from ..core.automation.service import AutomationService
from . import theme
from .automation_style import (
    chip_border, chip_fill, common_qss, fit_height, reserve_scrollbar, tab_focus_only,
)
from .chrome import (
    SHADOW_PAD,
    TitleBar,
    WindowFrame,
    floating_size,
    shadow_alpha,
    surface_bg,
    surface_border,
)
from .combo import Combo
from .confirm import confirm, notice
from .settings import dialog_qss
from .settings_rows import SettingsCard

#: 条件 / 动作行右侧的小图标按钮统一尺寸（命中区 ≥28px，padding 必须为 0）
_ICON_BTN = 30

#: 三块共用的左缘基准 = ③ 序号之后的下拉框位置：卡片内边距 16 + 序号列 20 +
#: 行距 8 = **44px**。①/② 没有序号，用 **28px** 的左内边距（= 20 + 8）补齐，
#: 不塞 104px 的空占位列——偏移量只等于序号本身占的宽度。三块底部按钮同样
#: 缩进 28px，左缘与各自的下拉框严格对齐（11-13 用户第六、七次调整）。
_INDEX_W = 20
_ROW_GAP = 8
_ROW_INDENT = _INDEX_W + _ROW_GAP


def _rule(parent: QWidget) -> QFrame:
    """卡内 1px 分隔线；和 ``SettingsCard`` 自动补的那条同一样式，但可单独缩进。"""
    line = QFrame(parent)
    line.setObjectName("RowDivider")
    line.setFrameShape(QFrame.NoFrame)
    line.setFixedHeight(1)
    return line

#: 动作类型下拉的四档。首档是**空档**「-」= 还没选（11-13 用户第八条：③ 只留一枚
#: 「＋ 添加动作」，加出来先落在空档上，用户再挑类型）。**延迟只有一种**，毫秒 / 秒
#: 是它旁边那格单位下拉（同一份 ``ms`` 的两把尺子，见 ``Action.unit``）。
_ACTION_LABELS = (("", "-"),
                  ("virtual_event", "发虚拟事件"),
                  ("delay", "延迟"),
                  ("speaker", "发音箱指令"))
#: 延迟的单位下拉。data 直接就是 ``Action.unit`` 的取值
_UNIT_LABELS = (("ms", "毫秒"), ("s", "秒"))
#: 换成延迟时的默认值：两种单位都是 1 秒，换单位不跳数量级
_DELAY_DEFAULTS = {"ms": "1000", "s": "1"}
#: 音箱指令的「静默」下拉。data 是 bool，直接进 ``Action.silent``
_SILENT_LABELS = ((False, "不静默"), (True, "静默"))
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
    "要先选动作类型": "这一行还没选类型：点左边的下拉挑「发虚拟事件 / 延迟 / 发音箱指令」；"
                    "不想加这条就点右边的 × 删掉",
    "要先选中枢": "「发虚拟事件」由中枢网关发出：先在左边选一台中枢。家庭里有多台时"
               "可随时切换，执行时按这一行选中的那台发送",
    "没有可选中枢": "当前家庭里没有探测到能发虚拟事件的中枢网关；换一个有中枢的家庭"
                 "再建这条规则",
    "不能小于 0": "延迟不能是负数",
    "该家庭缺少中枢网关": "「发虚拟事件」由中枢网关执行；这个家庭里没有探测到中枢网关，"
                    "所以输入框是禁用的。换一个有中枢的家庭再建这条规则",
    f"不能超过 {MAX_DELAY_MS} 毫秒": f"单条延迟上限 {MAX_DELAY_MS} 毫秒（{MAX_DELAY_MS // 1000} 秒）",
    f"不能超过 {MAX_DELAY_MS // 1000} 秒": f"单条延迟上限 {MAX_DELAY_MS} 毫秒（{MAX_DELAY_MS // 1000} 秒）",
    "要先选音箱": "这一行要先挑一台音箱；下拉里只有当前家庭里支持「执行文本指令」的那几台",
    "没有可选音箱": "当前家庭里没有支持「执行文本指令」的音箱（小爱音箱多数支持，"
                    "触屏音箱不一定），先在米家 App 里确认设备在线",
    "要填指令": "音箱指令不能为空；不想说话就删掉这一行动作",
    # ② 条件行（用户 11-13 第九条：② 也改成行内红字）
    "要先选条件类型": "这一行还没选条件类型：点左边的下拉挑「某程序正在运行 / 电脑空闲 / …」；"
                    "不想加就点右边的 × 删掉",
    "要填参数": "这一格是条件的判断依据：进程名、分钟数、标题片段、是 / 否、ac / battery，"
              "看左边类型那格的 placeholder",
    "参数要填数字": "这一格只收数字（分钟数、电量百分比这类），中文和单位后缀都不行",
    "填「是」或「否」": "锁屏条件只认四种写法：是 / 否 / true / false",
    "填 ac 或 battery": "供电条件只认 ac（插电）或 battery（电池），不区分大小写",
    "要填程序名": "「程序已运行 N 分钟」要同时知道程序名和分钟数，右边这格填可执行文件名",
    # 「多了 N 个字」「含 N 个发不出的字符」带数字，由 ActionRow._hint_tip 现拼
}
_MODE_LABELS = (("all", "满足全部状态条件"), ("any", "满足任一状态条件"))
#: ① 触发类型下拉。首档空档「-」= 还没选；目前只有键盘快捷键一种，
#: 多行触发之间是 **OR**（任一命中即执行），见 ``core.automation.rules``。
_TRIGGER_LABELS = (("", "-"), ("keyboard", "键盘快捷键"))


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
    return dialog_qss(p) + theme.field_qss(
        p, "QLineEdit#Field", radius=8, padding="5px 9px", font_size=12
    ) + f"""
    QLabel#Hint {{ color: {p.text_soft}; font-size: 11px; }}
    QLabel#RegOff {{ color: {p.text}; font-size: 11px; }}
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
    /* 键帽是"点击进入录制的输入框"，不是普通按钮：底与描边都按输入控件那套算
       （panel_bg + p.border 在深色下只有 1.01:1，键位文字会浮在一块没有边界的灰上，
       和编辑页输入框是同一个坑，见 theme.field_border）。 */
    QPushButton#Keycap {{
        background: {theme.field_bg(p)}; color: {p.text}; font-size: 13px;
        border: 1px solid {theme.field_border(p)}; border-radius: 8px;
        padding: 7px 12px; min-width: 150px; text-align: left;
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
# 触发行 / 条件行 / 动作行
# ---------------------------------------------------------------------------

class TriggerRow(QWidget):
    """一行触发 = 类型下拉 + 键帽 + 注册状态 / 行内红字 + 手动选键 + 删除。

    ① 允许多行，任意一行命中就执行这条规则（OR）；每行有自己的类型和键位，
    互不影响。目前只有「键盘快捷键」一种类型，空档「-」= 还没选（保存会被拦）。

    手动选键收在本行里（点「或手动选键」展开）：多行各调各的，不会把键位改到
    别的行上。录制由对话框统一管（``keyPressEvent`` 只认 ``_recording_row``）。
    """

    def __init__(self, trig: Trigger, palette: theme.Palette, service: Any,
                 on_change: Callable[[], None], on_remove: Callable[[QWidget], None],
                 on_record: Callable[[TriggerRow], None],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._p = palette
        self._service = service
        self._on_change = on_change
        self._on_record = on_record
        self._loading = True
        self._show_missing = False
        self._keys_value = trig.keys
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        line = QWidget(self)
        lay = QHBoxLayout(line)
        # 和 ②③ 同一套缩进：没有序号列，用 28px 补齐到 ③ 下拉那条 44px 线
        lay.setContentsMargins(_ROW_INDENT, 3, 0, 3)
        lay.setSpacing(_ROW_GAP)

        self.kind = Combo(palette, line)
        # addItem 会发 currentIndexChanged；此时其它控件还没建好，连接放到最后
        with QSignalBlocker(self.kind):
            for key, label in _TRIGGER_LABELS:
                self.kind.addItem(label, key)
        self.kind.setFixedWidth(160)

        self.keycap = QPushButton("未设置", line)
        self.keycap.setObjectName("Keycap")
        self.keycap.setCursor(Qt.PointingHandCursor)
        self.keycap.clicked.connect(lambda: self._on_record(self))

        self.status = QLabel("", line)
        self.status.setObjectName("Hint")

        self.hint = QLabel("", line)
        self.hint.setObjectName("Warn")
        self.hint.setWordWrap(False)
        self.hint.setVisible(False)

        self.remove_btn = _icon_button("×", "删除这条触发", line)
        self.remove_btn.clicked.connect(lambda: on_remove(self))

        lay.addWidget(self.kind)
        lay.addWidget(self.keycap)
        lay.addWidget(self.status, 1)
        lay.addWidget(self.hint)
        lay.addWidget(self.remove_btn)

        # 手动选键与触发行同一条左缘（44px）
        self.manual_box = QWidget(self)
        mcol = QVBoxLayout(self.manual_box)
        mcol.setContentsMargins(_ROW_INDENT, 0, 0, 0)
        mcol.setSpacing(4)
        self.manual_toggle = QPushButton("或手动选键（组合键录不到时展开）", self.manual_box)
        self.manual_toggle.setObjectName("Disclosure")
        self.manual_toggle.setCheckable(True)
        self.manual_toggle.setChecked(False)
        self.manual_toggle.setCursor(Qt.PointingHandCursor)
        self.manual = QWidget(self.manual_box)
        mrow = QHBoxLayout(self.manual)
        mrow.setContentsMargins(0, 4, 0, 4)
        mrow.setSpacing(8)
        self.key_combo = Combo(palette, self.manual)
        for key in key_choices():
            self.key_combo.addItem(key, key)
        self.key_combo.setFixedWidth(96)
        mrow.addWidget(self.key_combo)
        self._mod_boxes: dict[str, QCheckBox] = {}
        for name in ("Ctrl", "Alt", "Shift", "Win"):
            box = QCheckBox(name, self.manual)
            self._mod_boxes[name] = box
            mrow.addWidget(box)
        mrow.addStretch(1)
        # 所有控件都建好之后再连：_manual_keys 会遍历 _mod_boxes
        self.key_combo.currentIndexChanged.connect(self._manual_keys)
        for box in self._mod_boxes.values():
            box.stateChanged.connect(self._manual_keys)
        self.manual.setVisible(False)
        self.manual_toggle.toggled.connect(self.manual.setVisible)
        mcol.addWidget(self.manual_toggle)
        mcol.addWidget(self.manual)

        outer.addWidget(line)
        outer.addWidget(self.manual_box)

        with QSignalBlocker(self.kind):
            self.kind.setCurrentIndex(max(0, self.kind.findData(trig.type)))
        self._apply_keycap()
        self._set_manual_from_keys(self._keys_value)
        self._sync_visibility()
        self.refresh_status(self._service)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        # 行是动态创建的，窗口级 tab_focus_only 扫不到：自己收一次，
        # 免得点一下键帽 / 展开把手就留下焦点框（11-13 用户报）
        tab_focus_only(self)
        self._loading = False

    # -- 取值 ------------------------------------------------------------
    def _kind_key(self) -> str:
        return str(self.kind.currentData() or "")

    def value(self) -> Trigger:
        return Trigger(self._kind_key(), self._keys_value)

    def needs_kind(self) -> bool:
        return self._kind_key() not in TRIGGER_TYPES

    # -- 键位 ------------------------------------------------------------
    def set_keys(self, keys: str) -> None:
        """录制得到的新组合。空串 = 清掉。"""
        self._keys_value = normalize(keys)
        self._apply_keycap()
        self._set_manual_from_keys(self._keys_value)
        if not self._loading:
            self._on_change()

    def set_recording(self, on: bool) -> None:
        self.keycap.setProperty("recording", "true" if on else "false")
        self.keycap.setText("请按下组合键…" if on else (self._keys_value or "未设置"))
        self.keycap.style().unpolish(self.keycap)
        self.keycap.style().polish(self.keycap)
        if on:
            self.keycap.setFocus()

    def _apply_keycap(self) -> None:
        if self.keycap.property("recording") == "true":
            return
        self.keycap.setText(self._keys_value or "未设置")

    def _manual_keys(self, *_args: Any) -> None:
        if self._loading:
            return
        parts = [name for name in ("Ctrl", "Alt", "Shift", "Win")
                 if self._mod_boxes[name].isChecked()]
        parts.append(str(self.key_combo.currentData()))
        self._keys_value = normalize("+".join(parts))
        self._apply_keycap()
        self._on_change()

    def _set_manual_from_keys(self, keys: str) -> None:
        parts = keys.split("+") if keys else []
        with QSignalBlocker(self.key_combo):
            idx = self.key_combo.findData(parts[-1]) if parts else -1
            self.key_combo.setCurrentIndex(max(0, idx))
        for name, box in self._mod_boxes.items():
            with QSignalBlocker(box):
                box.setChecked(name in parts[:-1])

    # -- 行内状态 --------------------------------------------------------
    def refresh_status(self, service: Any, conflict: str = "") -> None:
        """右侧一行小字：注册状态（+ 另一个规则也在用这个键的提示）。

        真正的行内红字归 ``hint`` 管；这里只报"系统层面注册上了没有"，
        空档 / 没设键时整格留白，不跟红字抢位置。
        """
        keys = self._keys_value
        if self._kind_key() != "keyboard" or not keys:
            self.status.setText("")
            return
        state = service.status_for(keys)
        reason = reserved_reason(keys)
        text = f"注册状态：{state}" + (f"（{reason}）" if reason else "")
        # 用户 11-13 第十一条：注册成功才变绿；未注册 / 被占用用主文字色（白），
        # 不再默认绿色——绿色应当只表示"已经在系统里注册好了"。
        registered = not reason and ("已注册" in state or "注册成功" in state)
        tone = "Ok" if registered else "RegOff"
        if conflict:
            text += f"　·　{conflict}"
            tone = "Warn"
        self.status.setText(text)
        self.status.setObjectName(tone)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    # -- 行内红字（与 rules.validate_rule 的触发那一段逐条对齐） -----------
    def error_text(self) -> str:
        if self.needs_kind():
            return "要先选触发方式" if self._show_missing else ""
        if not self._keys_value:
            return "要先设置快捷键" if self._show_missing else ""
        if reserved_reason(self._keys_value):
            return "这个快捷键不可用"
        return ""

    def reveal_missing(self) -> None:
        self._show_missing = True
        self.sync_hint()

    def sync_hint(self) -> None:
        text = self.error_text()
        self.hint.setText(text)
        self.hint.setToolTip(_HINT_TIPS.get(text, ""))
        self.hint.setVisible(bool(text))

    def focus_widget(self) -> QWidget:
        if self.needs_kind():
            return self.kind
        return self.keycap

    def _kind_changed(self) -> None:
        if self._kind_key() != "keyboard":
            # 切离键盘：清掉键位，避免"藏着的旧快捷键"继续注册
            self._keys_value = ""
        self._show_missing = False
        self._apply_keycap()
        self._set_manual_from_keys(self._keys_value)
        self._sync_visibility()
        self.refresh_status(self._service)
        self.sync_hint()
        if not self._loading:
            self._on_change()

    def _sync_visibility(self) -> None:
        keyboard = self._kind_key() == "keyboard"
        # 空档时键帽 / 手动选键都没有意义：灰掉但保留位置，行宽别跳
        self.keycap.setEnabled(keyboard)
        self.manual_box.setVisible(keyboard)
        self.status.setVisible(keyboard)
        if not keyboard:
            self.status.setText("")


class ConditionRow(QWidget):
    """一行 = 类型 + 比较符 + 参数（+ 程序名）+ 行内红字 + 删除。

    类型下拉首档是空档「-」：② 只有一枚「＋ 添加条件」，加出来的行落在空档上，
    用户再挑条件类型（11-13 用户第八条）。空档时后面几格灰掉、比较符清空，
    ``value()`` 交出去的 ``type`` 是空串。

    **红字和 ③ 动作行同一套**（用户 11-13 第九条）：``hint``（``QLabel#Warn``）夹在
    参数 / 程序名与 × 之间，``textChanged`` 就刷，边打边判；空档那条「要先选条件类型」
    只在点过保存后才亮（``reveal_missing()``），理由同 ③——刚加一行就一片红是在骂人。
    判据必须和 ``rules.validate_rule`` 逐条对齐，否则会出现"行内没红字、底部却报错"。
    """

    def __init__(self, cond: Condition, palette: theme.Palette,
                 on_change: Callable[[], None], on_remove: Callable[[QWidget], None],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._p = palette
        self._on_change = on_change
        self._show_missing = False
        # 构造期回填会触发 textChanged；照 ActionRow 的做法挡住标脏
        self._loading = True
        lay = QHBoxLayout(self)
        # ② 条件行没有序号列：用 28px 左内边距补齐到 ③ 序号之后的下拉框那条线
        lay.setContentsMargins(_ROW_INDENT, 3, 0, 3)
        lay.setSpacing(_ROW_GAP)

        self.kind = Combo(palette, self)
        # addItem 会发 currentIndexChanged；此时其它控件还没建好，连接必须放到最后
        with QSignalBlocker(self.kind):
            self.kind.addItem("-", "")
            for key, (label, *_rest) in CONDITION_TYPES.items():
                self.kind.addItem(label, key)
        self.kind.setFixedWidth(160)

        self.op = Combo(palette, self)
        self.op.setFixedWidth(78)

        self.arg = QLineEdit(self)
        self.arg.setObjectName("Field")
        self.arg.setPlaceholderText("参数")
        self.arg.textChanged.connect(self._on_text)

        self.target = QLineEdit(self)
        self.target.setObjectName("Field")
        self.target.setPlaceholderText("程序名，如 python.exe")
        self.target.setFixedWidth(150)
        self.target.textChanged.connect(self._on_text)

        self.hint = QLabel("", self)
        self.hint.setObjectName("Warn")
        self.hint.setWordWrap(False)
        self.hint.setVisible(False)

        self.remove_btn = _icon_button("×", "删除这条条件", self)
        self.remove_btn.clicked.connect(lambda: on_remove(self))

        lay.addWidget(self.kind)
        lay.addWidget(self.op)
        lay.addWidget(self.arg, 1)
        lay.addWidget(self.target)
        lay.addWidget(self.hint)
        lay.addWidget(self.remove_btn)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        # 比较符以前没接：改「≥ / ≤」「是 / 不是」既不刷新红字也不标脏，
        # 关窗不会提示未保存。顺手补上（同一个 _on_text 就够）。
        self.op.currentIndexChanged.connect(self._on_text)

        idx = self.kind.findData(cond.type)
        self.kind.setCurrentIndex(max(0, idx))
        self._fill_ops(cond.type)
        opidx = self.op.findData(cond.op)
        self.op.setCurrentIndex(max(0, opidx))
        self.arg.setText(cond.arg)
        self.target.setText(cond.target)
        self._sync_visibility()
        self._loading = False
        self.sync_hint()

    def _on_text(self, *_args: Any) -> None:
        self.sync_hint()
        if not self._loading:
            self._on_change()

    def _kind_key(self) -> str:
        return str(self.kind.currentData() or "")

    def _fill_ops(self, kind: str) -> None:
        with QSignalBlocker(self.op):
            self.op.clear()
            spec = CONDITION_TYPES.get(kind)
            if spec is None:  # 空档：比较符留空，免得 currentData 交出 None
                return
            for op in spec[1]:
                self.op.addItem(OPS[op], op)

    def _kind_changed(self) -> None:
        kind = self._kind_key()
        self._fill_ops(kind)
        self.arg.clear()  # 旧参数换类型后多半不再适用，别留着混进新条件
        self._show_missing = False  # 选了类型就不再是"还没选"
        self._sync_visibility()
        self.sync_hint()
        if not self._loading:
            self._on_change()

    def _sync_visibility(self) -> None:
        kind = self._kind_key()
        spec = CONDITION_TYPES.get(kind)
        self.target.setVisible(kind == "process_elapsed")
        self.arg.setPlaceholderText(spec[2] if spec else "先选条件类型")
        # 空档时比较符 / 参数没有意义：灰掉但**保留位置**，行宽别跟着跳
        self.op.setEnabled(spec is not None)
        self.arg.setEnabled(spec is not None)

    # -- 行内红字（与 rules.validate_rule 的条件那一段逐条对齐） -----------
    def error_text(self) -> str:
        """这一行的问题，没有则返回空串。文案要短——宽度是从参数输入框那里抢的。"""
        kind = self._kind_key()
        spec = CONDITION_TYPES.get(kind)
        if spec is None:
            return "要先选条件类型" if self._show_missing else ""
        arg = self.arg.text().strip()
        if not arg:
            return "要填参数"
        if spec[3]:
            try:
                float(arg)
            except ValueError:
                return "参数要填数字"
        elif kind == "locked" and arg.lower() not in ("true", "false", "是", "否"):
            return "填「是」或「否」"
        elif kind == "power" and self._op_key() == "is" \
                and arg.lower() not in ("ac", "battery"):
            return "填 ac 或 battery"
        if kind == "process_elapsed" and not self.target.text().strip():
            return "要填程序名"
        return ""

    def needs_kind(self) -> bool:
        return self._kind_key() not in CONDITION_TYPES

    def reveal_missing(self) -> None:
        """点保存时把「要先选条件类型」亮出来；选了类型会自动撤掉。"""
        if not self.needs_kind():
            return
        self._show_missing = True
        self.sync_hint()

    def sync_hint(self) -> None:
        text = self.error_text()
        self.hint.setText(text)
        self.hint.setToolTip(_HINT_TIPS.get(text, ""))
        self.hint.setVisible(bool(text))

    def focus_widget(self) -> QWidget:
        """出错时焦点该给谁：没选类型 / 参数灰着 → 类型下拉；缺程序名 → 那格。"""
        if self.needs_kind() or not self.arg.isEnabled():
            return self.kind
        if not self.target.isHidden() and not self.target.text().strip():
            return self.target
        return self.arg

    def _op_key(self) -> str:
        return str(self.op.currentData() or "")

    def value(self) -> Condition:
        return Condition(
            self._kind_key(), self._op_key(),
            self.arg.text().strip(), self.target.text().strip(),
        )


def _speaker_targets(service: AutomationService) -> list[speaker.Target]:
    """当前家庭里能「执行文本指令」的音箱；取不到就当没有。

    为什么要吞异常：这一层只决定"下拉里有几项"，探测失败不该把整个编辑窗带崩
    （离线验证用的假 service 根本没有这个方法）。
    """
    getter = getattr(service, "speaker_targets", None)
    if not callable(getter):
        return []
    try:
        return list(getter())
    except Exception:  # noqa: BLE001
        return []


def _hub_targets(service: AutomationService) -> list[tuple[str, str, bool]]:
    """当前家庭里能发虚拟事件的中枢：``[(did, 名字, 在线)]``；取不到就当没有。

    和 :func:`_speaker_targets` 一样只决定"下拉里有几项"，假 service 没有这个方法
    时返回空列表，别把编辑窗带崩。
    """
    getter = getattr(service, "hub_choices", None)
    if not callable(getter):
        return []
    try:
        return list(getter())
    except Exception:  # noqa: BLE001
        return []


class ActionRow(QWidget):
    """一行 = 类型 + 该类型自己那几个下拉 + 值 + 上移 / 下移 / 删除。

    列数跟着类型走（用户 11-13 第七条反馈）::

        -（未选）     —
        发虚拟事件    [事件名]
        延迟          [毫秒/秒] [值]
        发音箱指令    [音箱] [不静默/静默] [要说的话]

    首档空档「-」= **还没选类型**：③ 只有一枚「＋ 添加动作」，加出来的行落在这里，
    用户再点下拉挑（11-13 用户第八条）。空档时输入框灰着只留一行 placeholder，
    ``needs_kind()`` 为真；红字**不立刻亮**，点保存才由 ``reveal_missing()`` 亮出来
    ——刚点「＋ 添加动作」就一片红是在骂用户。

    **延迟只有一种动作**：毫秒 / 秒是同一份 ``ms`` 的两把尺子（见 ``Action.unit``），
    所以它退回"下拉里的一档 + 单位下拉"，不再像上一版那样在类型下拉里占两行。
    上一版 ``rules.json`` 里带 ``unit:"s"`` 的延迟读进来会落在「秒」这档上，无损。
    """

    def __init__(self, action: Action, palette: theme.Palette,
                 on_change: Callable[[], None], on_remove: Callable[[QWidget], None],
                 on_move: Callable[[QWidget, int], None],
                 targets: list[speaker.Target] | None = None,
                 hub_ready: bool = True,
                 hubs: list[tuple[str, str, bool]] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._on_change = on_change
        self._loading = True
        self._targets = list(targets or [])
        #: 当前家庭可作虚拟事件中枢的设备：``[(did, 名字, 在线)]``
        self._hubs = list(hubs or [])
        #: 当前家庭有没有中枢网关（「发虚拟事件」的前提，判据同极客版入口）
        self._hub_ready = hub_ready
        self._kind = action.type  # 上一次的类型：判"换的是不是同一种动作"
        self._prev_unit = action.unit if action.unit == "s" else "ms"
        # 空档的"还没选"红字只在点保存后亮（见 needs_kind / reveal_missing）
        self._show_missing = False
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 3, 0, 3)
        lay.setSpacing(_ROW_GAP)

        self.index_label = QLabel("", self)
        self.index_label.setObjectName("Hint")
        self.index_label.setFixedWidth(_INDEX_W)
        # 序号靠右：它占的是和 ①② 标题列等宽的那一列，贴着右边的类型下拉
        self.index_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.kind = Combo(palette, self)
        for key, label in _ACTION_LABELS:
            self.kind.addItem(label, key)
        # 「发音箱指令」5 个全角字 + 左右内边距 12/38：118 刚好不省略
        self.kind.setFixedWidth(118)

        # 中枢设备：只有「发虚拟事件」用得到。多中枢的家庭把几台都列出来，
        # 发的时候按这一行选中的发（关系显式化，不再隐式取第一台）。
        self.hub = Combo(palette, self)
        self.hub.setFixedWidth(176)
        self._fill_hubs(action)

        # 延迟的单位：同一份毫秒值的两把尺子
        self.unit = Combo(palette, self)
        for key, label in _UNIT_LABELS:
            self.unit.addItem(label, key)
        self.unit.setFixedWidth(80)

        self.speaker = Combo(palette, self)
        self.speaker.setFixedWidth(176)
        self._fill_speakers(action)
        self.speak_targets = list(self._targets)  # 给验证脚本读，别去翻 combo 的 itemData

        self.silent = Combo(palette, self)
        for value, label in _SILENT_LABELS:
            self.silent.addItem(label, value)
        self.silent.setFixedWidth(92)

        self.value_edit = QLineEdit(self)
        self.value_edit.setObjectName("Field")
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
        lay.addWidget(self.hub)
        lay.addWidget(self.unit)
        lay.addWidget(self.speaker)
        lay.addWidget(self.silent)
        lay.addWidget(self.value_edit, 1)
        lay.addWidget(self.hint)
        for btn in (self.up_btn, self.down_btn, self.delete_btn):
            lay.addWidget(btn)

        # 控件都建好再连：下面回填值时不能让 _kind_changed 抢先清空输入框
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self.unit.currentIndexChanged.connect(self._unit_changed)
        self.hub.currentIndexChanged.connect(self._selection_changed)
        self.speaker.currentIndexChanged.connect(self._selection_changed)
        self.silent.currentIndexChanged.connect(self._selection_changed)

        # 认不出的类型（空串 / 旧版本残留）一律落在空档上，别偷偷替用户选成虚拟事件
        kind = action.type if action.type in dict(_ACTION_LABELS) else ""
        with QSignalBlocker(self.kind), QSignalBlocker(self.unit), \
                QSignalBlocker(self.hub), QSignalBlocker(self.speaker), \
                QSignalBlocker(self.silent):
            self.kind.setCurrentIndex(max(0, self.kind.findData(kind)))
            self.unit.setCurrentIndex(max(0, self.unit.findData(self._prev_unit)))
            # 没存中枢的旧动作：落在第一台上（有就显式选中），改了才落盘
            self.hub.setCurrentIndex(max(
                0, self.hub.findData(action.hub if kind == "virtual_event" else "")))
            self.speaker.setCurrentIndex(
                max(0, self.speaker.findData(action.did if kind == "speaker" else "")))
            self.silent.setCurrentIndex(
                max(0, self.silent.findData(bool(action.silent))))
        self._kind = kind
        if kind == "delay":
            self.value_edit.setText(_delay_text(action.ms, self._prev_unit))
        elif kind == "speaker":
            self.value_edit.setText(action.text)
        else:
            self.value_edit.setText(action.name if kind else "")
        self._loading = False
        self._sync_columns()
        self._sync_placeholder()
        self.sync_hint()

    # -- 候选 ------------------------------------------------------------
    def _fill_hubs(self, action: Action) -> None:
        """中枢候选 = 当前家庭里被探测为中枢的那几台（由调用方筛好传进来）。

        规则里存的是 did。那台设备如果已经不在这个家庭的候选里（被移除 / 换家庭），
        仍然补成一条特殊项，让这一行有东西可显示；执行时服务层按 did 解析，真
        找不到才在日志里报错。
        """
        dids = {h[0] for h in self._hubs}
        for did, name, online in self._hubs:
            label = name or did
            if not online:
                label += "（离线）"
            self.hub.addItem(label, did)
            self.hub.setItemData(self.hub.count() - 1, label, Qt.ToolTipRole)
        if action.type == "virtual_event" and action.hub and action.hub not in dids:
            self.hub.insertItem(0, f"{action.hub_name or action.hub}（不在当前家庭）",
                                action.hub)
            self.hub.setItemData(
                0, "这台中枢已不在当前家庭的候选里（可能被移除或换了家庭）",
                Qt.ToolTipRole)
        if not self._hubs and not (action.type == "virtual_event" and action.hub):
            self.hub.addItem("没有可选中枢", "")
            self.hub.setEnabled(False)
            return
        if not self._hubs:
            self.hub.setEnabled(False)

    def _fill_speakers(self, action: Action) -> None:
        """音箱候选 = 当前家庭里能执行文本指令的那几台（由调用方筛好传进来）。

        规则里存的是 did，若那台音箱已经不在这个家庭的候选里（设备被移除 / 换了家庭），
        仍然把它作为一条**特殊项**补在最前面，让这一行有东西可显示；保存不拦它，
        执行时服务层会按 did 在全家庭里找，真找不到才报错。
        """
        dids = {t.did for t in self._targets}
        for t in self._targets:
            label = t.name or t.model or t.did
            if not t.online:
                label += "（离线）"
            self.speaker.addItem(label, t.did)
            self.speaker.setItemData(self.speaker.count() - 1, label, Qt.ToolTipRole)
        if action.type == "speaker" and action.did and action.did not in dids:
            self.speaker.insertItem(0, f"{action.name or action.did}（不在当前家庭）",
                                    action.did)
        if not self._targets and not (action.type == "speaker" and action.did):
            self.speaker.addItem("没有可用音箱", "")
            self.speaker.setEnabled(False)
            return
        if not self._targets:
            self.speaker.setEnabled(False)

    def _current_did(self) -> str:
        return str(self.speaker.currentData() or "")

    def _current_hub(self) -> str:
        return str(self.hub.currentData() or "")

    def _pick_default_hub(self) -> None:
        """从别的类型切到「发虚拟事件」：没选就挑第一台在线的中枢。"""
        if self._current_hub():
            return
        best = next((h for h in self._hubs if h[2]), None) or (
            self._hubs[0] if self._hubs else None)
        if best is not None:
            with QSignalBlocker(self.hub):
                self.hub.setCurrentIndex(max(0, self.hub.findData(best[0])))

    def _pick_default_speaker(self) -> None:
        """从别的类型切到音箱指令：没选就挑第一台在线的，别让用户先空跑一次校验。"""
        if self._current_did():
            return
        best = next((t for t in self._targets if t.online), None) or (
            self._targets[0] if self._targets else None)
        if best is not None:
            with QSignalBlocker(self.speaker):
                self.speaker.setCurrentIndex(
                    max(0, self.speaker.findData(best.did)))

    # -- 事件 ------------------------------------------------------------
    def _on_text(self, *_args: Any) -> None:
        """输入即校验：红字跟着光标走，不用等点保存才发现填错。"""
        if self._loading:
            return
        self.sync_hint()
        self._on_change()

    def _selection_changed(self, *_args: Any) -> None:
        if self._loading:
            return
        self.sync_hint()
        self._on_change()

    def _kind_changed(self) -> None:
        if self._loading:
            return
        # 换类型必须换掉旧值：把「发虚拟事件」切成「延迟」后残留的事件名
        # 会被 int() 解析失败，用户看到的是"明明填了东西却报校验错"。
        kind = self._kind_key()
        if kind == self._kind:
            return
        if kind == "delay":
            self.value_edit.setText(_DELAY_DEFAULTS[self._unit()])
        else:
            self.value_edit.setText("")
            if kind == "speaker":
                self._pick_default_speaker()
            elif kind == "virtual_event":
                self._pick_default_hub()
        self._kind = kind
        # 选了类型就不再是"还没选"，之前那条红字跟着撤掉
        self._show_missing = False
        self._sync_columns()
        self._sync_placeholder()
        self.sync_hint()
        self._on_change()

    def _unit_changed(self) -> None:
        """毫秒 ↔ 秒只换算不重置：2000 ms ⇄ 2 秒是同一个量。"""
        if self._loading:
            return
        unit = self._unit()
        if self._kind_key() == "delay" and unit != self._prev_unit:
            ms = _delay_ms(self.value_edit.text(), self._prev_unit)
            self.value_edit.setText(_delay_text(ms, unit) if ms >= 0
                                    else _DELAY_DEFAULTS[unit])
        self._prev_unit = unit
        self._sync_placeholder()
        self.sync_hint()
        self._on_change()

    def _sync_columns(self) -> None:
        """按类型显隐那几列。隐藏的控件在 QHBoxLayout 里不占位置。

        空档（还没选类型）时输入框**留着但灰掉**：直接隐藏会让后面 ×↑↓ 三枚按钮
        左跳到下拉旁边，选完类型又跳回来，一行之内抖两下。
        """
        kind = self._kind_key()
        self.unit.setVisible(kind == "delay")
        self.hub.setVisible(kind == "virtual_event")
        self.hub.setEnabled(kind == "virtual_event" and (
            bool(self._hubs) or bool(self._current_hub())))
        self.speaker.setVisible(kind == "speaker")
        self.silent.setVisible(kind == "speaker")
        # 发虚拟事件需要家庭里有中枢网关：没有就禁用输入框，红字由 sync_hint 给
        self.value_edit.setEnabled(bool(kind) and not self._no_hub(kind))

    def _kind_key(self) -> str:
        return str(self.kind.currentData() or "")

    def _no_hub(self, kind: str | None = None) -> bool:
        """这一行是不是「要发虚拟事件，但这个家庭没有中枢网关」。"""
        return (kind or self._kind_key()) == "virtual_event" and not self._hub_ready

    def needs_kind(self) -> bool:
        """还没选动作类型（空档）。空行刚加进来不算错，点保存才算（11-13 用户第八条）。"""
        return self._kind_key() not in ACTION_TYPES

    def focus_widget(self) -> QWidget:
        """出错时焦点该给谁：空档行的输入框是禁用的，交给类型下拉。"""
        if self._kind_key() == "virtual_event" and not self._current_hub():
            return self.hub
        return self.value_edit if self.value_edit.isEnabled() else self.kind

    def reveal_missing(self) -> None:
        """点保存时把「要先选动作类型」亮出来；选了类型会自动撤掉。"""
        if not self.needs_kind():
            return
        self._show_missing = True
        self.sync_hint()

    def _is_delay(self) -> bool:
        return self._kind_key() == "delay"

    def _unit(self) -> str:
        """当前录入刻度："ms" / "s"。存盘与校验一律用毫秒。"""
        return str(self.unit.currentData() or "ms")

    def _is_silent(self) -> bool:
        return bool(self.silent.currentData())

    def _sync_placeholder(self) -> None:
        kind = self._kind_key()
        if not kind:
            self.value_edit.setPlaceholderText("先选左边的动作类型，这里才知道要填什么")
        elif kind == "delay":
            self.value_edit.setPlaceholderText(
                "秒，如 30 或 1.5（最多 3 位小数）" if self._unit() == "s"
                else "毫秒，如 2000（只填整数）")
        elif kind == "speaker":
            self.value_edit.setPlaceholderText("要说的话，如「打开客厅灯」")
        elif self._no_hub(kind):
            self.value_edit.setPlaceholderText("该家庭缺少中枢网关，发不了虚拟事件")
        else:
            self.value_edit.setPlaceholderText("虚拟事件名（要和米家自动化里监听的完全一致）")

    def format_error(self) -> str:
        """这一行**填法**的问题，没有则返回空串（延迟的进制 / 位数、音箱指令的长度）。

        延迟只能在这一层判：``Action`` 交出去的只有毫秒，core 看不出用户敲的是
        ``1.5 秒`` 还是 ``1500 毫秒``，更看不出多打了第 4 位小数。两条规矩：
        毫秒档只收整数（半毫秒没有意义）；秒档最多 3 位小数（执行精度就是 1 ms）。
        超出就**明说**，不静默四舍五入——用户专门问过精度这件事。

        文案是**行内红字**：挂在输入框右边，得短。来龙去脉进 tooltip。
        """
        if self.needs_kind():
            # 「还没选类型」只在点过保存之后才说（用户 11-13 第八条）：刚点
            # 「＋ 添加动作」就一片红，等于边让人挑边骂人。
            return "要先选动作类型" if self._show_missing else ""
        if self._no_hub():
            # 红字顶掉所有填法错误：家庭没有中枢网关时，填对了也发不出去
            return "该家庭缺少中枢网关"
        if self._kind_key() == "speaker":
            text = self.value_edit.text()
            # 超长不静默截断（主窗口的发送框才那么干：它没有"存下来再说"的机会）。
            # 编辑页要落盘，**报出差额**让用户自己决定删哪句，比悄悄砍掉尾巴好。
            extra = len(text) - MAX_SPEAK_CHARS
            if extra > 0:
                return f"多了 {extra} 个字"
            # 字符白名单与主窗口共用 ``speaker.clean``：米家只收中英文、数字、
            # 空格与「，。？！」。不在这里拦的话，规则存进去是"脏"的，执行时
            # 服务层会悄悄清洗掉——用户只会发现"音箱少念了几个字"。
            dropped = speaker.clean(text).dropped
            if dropped:
                return f"含 {len(dropped)} 个发不出的字符"
            return ""
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

        顺序是先具体后笼统：填法（整数 / 位数 / 字数）→ 缺失项（音箱 / 值）→ 范围。
        **范围也在这层判**，以前只有点保存后由 ``validate_rule`` 在底部说"延迟超出
        范围"，现在跟填法一样就地红字，用户不用点保存才知道填大了。
        """
        err = self.format_error()
        if err:
            return err
        kind = self._kind_key()
        if kind == "speaker":
            if not self._current_did():
                return "没有可选音箱" if not self._targets else "要先选音箱"
            if not self.value_edit.text().strip():
                return "要填指令"
            return ""
        if kind == "virtual_event":
            if not self._current_hub():
                return "没有可选中枢" if not self._hubs else "要先选中枢"
            return ""
        if not self._is_delay():
            return ""
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
        self.hint.setToolTip(self._hint_tip(text))
        self.hint.setVisible(bool(text))

    def _hint_tip(self, text: str) -> str:
        """红字的来龙去脉。带数字的文案（「多了 37 个字」「含 3 个发不出的字符」）
        没法预先写进 ``_HINT_TIPS``，音箱指令这两条按当前内容现拼。"""
        tip = _HINT_TIPS.get(text)
        if tip or self._kind_key() != "speaker":
            return tip or ""
        raw = self.value_edit.text()
        if len(raw) > MAX_SPEAK_CHARS:
            return (f"米家「自定义指令」上限 {MAX_SPEAK_CHARS} 个字符；"
                    f"当前 {len(raw)} 个，删掉 {len(raw) - MAX_SPEAK_CHARS} 个才能保存")
        dropped = speaker.clean(raw).dropped
        if dropped:
            return ("米家只接受中英文、数字、空格和「，。？！」；"
                    f"这几个要删掉或换掉：{dropped}")
        return ""

    def set_index(self, number: int) -> None:
        self.index_label.setText(f"{number}.")

    def set_move_enabled(self, up: bool, down: bool) -> None:
        """上移 / 下移**从不置灰**（用户 11-13 第十二条）：第一行按 ↑、最后一行按 ↓
        顺序不变即可，灰按钮反而像"这一行坏了"。``up`` / ``down`` 只为接口
        兼容（调用方仍按位置传），按钮一律可点。"""
        self.up_btn.setEnabled(True)
        self.down_btn.setEnabled(True)
        self.up_btn.setToolTip("上移这条动作" if up else "已经是第一条，按了也不会动")
        self.down_btn.setToolTip("下移这条动作" if down else "已经是最后一条，按了也不会动")

    def value(self) -> Action:
        """读回一条动作。延迟**永远是毫秒**，秒档在这里折算，界面之外没人认"秒"。"""
        kind = self._kind_key()
        if kind == "delay":
            unit = self._unit()
            return Action("delay", "", _delay_ms(self.value_edit.text(), unit), unit)
        if kind == "speaker":
            did = self._current_did()
            target = next((t for t in self._targets if t.did == did), None)
            # name 只是显示名快照（日志 / 重新打开时回显），执行永远按 did 走
            name = target.name if target is not None else self.speaker.currentText()
            return Action("speaker", name, 0, "ms", did,
                          self.value_edit.text().strip(), self._is_silent())
        if not kind:
            # 空档如实出去：``type`` 空串，ui 层 _save 会拦，core 的 validate_rule
            # 也认这个（见 rules.validate_rule）。别默认成 virtual_event，
            # 那等于替用户选了一个他从没选过的类型。
            return Action("")
        did = self._current_hub()
        target = next((h for h in self._hubs if h[0] == did), None)
        # hub_name 只是显示名快照；执行按 did 解析（见 sender.resolve_did）
        name = target[1] if target is not None else ""
        return Action("virtual_event", self.value_edit.text().strip(), 0,
                      hub=did, hub_name=name)


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
        # 规则归属在创建时按所在家庭固定：编辑时只读展示，不提供改家庭的能力
        # （改了家庭，③ 里已选的设备还是旧家庭的，逻辑不成立）。新建取当前家庭；
        # 老文件里归属为空的规则也在打开时按当前家庭固定，保存即落盘。
        self._home = str(getattr(service, "current_home", "") or "")
        source = service.rules.get(rule_id) if rule_id else None
        self._draft: Rule = (
            source.clone() if source is not None else Rule(
                id=service.rules.new_id(), name="新规则", triggers=[], conditions=[],
                # 新建先给一行**空档**动作：用户自己挑类型（11-13 用户第八条）。
                # 预选成「发虚拟事件」会让人以为事件是默认动作、找不到音箱那档。
                actions=[Action("")],
                cooldown_ms=DEFAULT_COOLDOWN_MS, enabled=True,
                home=self._home,
            )
        )
        if not self._draft.home:
            self._draft.home = self._home
        # 「发虚拟事件」要求这个家庭有中枢网关，判据与极客版入口共用一套
        checker = getattr(service, "home_has_hub", None)
        self._hub_ready = bool(checker(self._draft.home)) if callable(checker) else True
        self._dirty = False
        self._loading = False
        #: 正在录制的触发行（None = 没在录）。录制只认这一行，多行不会串键。
        self._recording_row: TriggerRow | None = None
        self._trigger_rows: list[TriggerRow] = []
        self._cond_rows: list[ConditionRow] = []
        self._action_rows: list[ActionRow] = []
        # 「发音箱指令」的候选：**当前家庭**里能执行文本指令的那几台。
        # 取一次就够——本窗是模态的，开着的时候切不了家庭。
        self._speaker_targets = _speaker_targets(service)
        # 「发虚拟事件」的中枢候选：同样取当前家庭、一次取完（窗开着时切不了家庭）
        self._hub_targets = _hub_targets(service)

        title = "新建规则" if self._is_new else "编辑规则"
        self.setWindowTitle(title)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self._frame = WindowFrame(self, resizable=True,
                                  radius=theme.RADIUS_CARD,
                                  shadow=SHADOW_PAD,
                                  shadow_alpha=shadow_alpha(self._p),
                                  fill=lambda: surface_bg(self._p),
                                  border=lambda: surface_border(self._p))
        self.resize(*floating_size(860, fit_height(900, reserve=2 * SHADOW_PAD)))
        self.setMinimumSize(*floating_size(760, 620))
        self.setStyleSheet(_page_qss(palette))

        outer = QVBoxLayout(self._frame.body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.title_bar = TitleBar(title, palette, subtitle=self._draft.name,
                                  buttons=("close",), variant="dialog",
                                  frame=self._frame)
        outer.addWidget(self.title_bar)

        body = QWidget(self._frame.body)  # 内容区，别挂窗口（窗口大一圈）
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
        # 滚动条常驻占位：③动作一多就出现竖向滚动条，以前会把三段内容挤窄 10px，
        # 保存前后输入框宽度跳一下（同列表窗，见 automation_style）。
        reserve_scrollbar(area)
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

        # 归属家庭：**只读**。规则在哪个家庭创建就属于哪个家庭，不能改——改了家庭，
        # ③ 里已经选好的设备还留在旧家庭，执行逻辑不成立（用户 11-13 第四轮）。
        labeler = getattr(self.service, "home_label", None)
        home_text = str(labeler(self._draft.home) if callable(labeler) else self._draft.home)
        self.home_value = QLabel(home_text or "未指定家庭", content)
        self.home_value.setObjectName("Hint")
        self.home_value.setToolTip("规则归属于创建时所在的家庭，不能修改")
        head.add_row("归属家庭", self.home_value)
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

        # 触发行：和 ②③ 一样是"一行 = 类型下拉 + 参数"。① 可以挂**多条**，
        # 语义是 OR：任意一条命中就执行这条规则；新规则点「＋ 添加动作」才加
        # 第一行。没有序号，用 28px 左内边距对齐到 ③ 序号之后的下拉框（44px）。
        self.trigger_box = QWidget(content)
        self.trigger_layout = QVBoxLayout(self.trigger_box)
        self.trigger_layout.setContentsMargins(0, 0, 0, 0)
        self.trigger_layout.setSpacing(2)

        # 底部一行：左边「＋ 添加动作」，右边冷静期。冷静期标签贴着它自己的
        # 输入框（不再被弹性标题列推到最左边），语义上仍属于①。
        trigger_btns = QWidget(content)
        brow = QHBoxLayout(trigger_btns)
        brow.setContentsMargins(_ROW_INDENT, 4, 0, 4)
        brow.setSpacing(8)
        self.add_trigger_btn = QPushButton("＋ 添加动作", trigger_btns)
        self.add_trigger_btn.setObjectName("Tool")
        self.add_trigger_btn.setCursor(Qt.PointingHandCursor)
        self.add_trigger_btn.clicked.connect(lambda: self._add_trigger())
        brow.addWidget(self.add_trigger_btn)
        brow.addStretch(1)
        cool_label = QLabel("冷静期（毫秒）", trigger_btns)
        cool_label.setObjectName("RowTitle")
        cool_label.setToolTip(
            f"默认 {DEFAULT_COOLDOWN_MS} ms；0 = 关闭。按住不放的自动重复另有内置抑制，不在这里配")
        brow.addWidget(cool_label)
        brow.addWidget(self.cooldown)

        # 两块合成一个整行单元：触发行一多也不会在它们之间冒出分隔线
        trigger_section = QWidget(content)
        tsec = QVBoxLayout(trigger_section)
        tsec.setContentsMargins(0, 0, 0, 0)
        tsec.setSpacing(0)
        tsec.addWidget(self.trigger_box)
        tsec.addWidget(trigger_btns)
        card1.add_unit(trigger_section)
        col.addWidget(card1)

        # ② 条件
        # 「条件关系」常显在章节标题右侧（用户 11-13 第五次调整）：0 / 1 / 多条都
        # 显示，不再按条数隐藏。它原本占卡片里一整行、右边一长条空白，现在只占
        # 标题行，卡片里只剩条件行和按钮；也不再配「条件关系」文字标签。
        head2 = self._step_header(content, "2", "状态条件")
        self.mode = Combo(self._p, content)
        for key, label in _MODE_LABELS:
            self.mode.addItem(label, key)
        self.mode.setFixedWidth(190)
        self.mode.setToolTip("多条条件之间的关系：全部满足 / 满足任一")
        self.mode.currentIndexChanged.connect(self._mark_dirty)
        head2.insertWidget(2, self.mode)  # 编号、标题之后，弹性之前
        col.addLayout(head2)
        card2 = SettingsCard(content)
        self._set_step(card2, "2")
        self.cond_card = card2
        self.cond_box = QWidget(content)
        self.cond_layout = QVBoxLayout(self.cond_box)
        self.cond_layout.setContentsMargins(0, 0, 0, 0)
        self.cond_layout.setSpacing(2)
        card2.add_unit(self.cond_box)
        self.cond_empty = QLabel("可以为空：不设条件时，快捷键按下就会执行动作", content)
        self.cond_empty.setObjectName("Hint")
        self.cond_empty.setContentsMargins(0, 8, 0, 8)
        # 空态提示和它**上下两条分隔线**一起缩进到 44px（用户 11-13 第八次调整），
        # 右缘仍到卡片内边距。这两条线自己做，所以 add_unit 不再自动补线。
        self.cond_empty_box = QWidget(content)
        ebox = QVBoxLayout(self.cond_empty_box)
        ebox.setContentsMargins(_ROW_INDENT, 0, 0, 0)
        ebox.setSpacing(0)
        self.cond_empty_top = _rule(self.cond_empty_box)
        self.cond_empty_bottom = _rule(self.cond_empty_box)
        ebox.addWidget(self.cond_empty_top)
        ebox.addWidget(self.cond_empty)
        ebox.addWidget(self.cond_empty_bottom)
        card2.add_unit(self.cond_empty_box, divider=False)
        cond_btns = QWidget(content)
        cbox = QVBoxLayout(cond_btns)
        cbox.setContentsMargins(0, 0, 0, 0)
        cbox.setSpacing(0)
        # 有条件的行时，空态那块整体隐藏；这时用这条全宽线把条件和按钮隔开
        self.cond_btns_divider = _rule(cond_btns)
        cbox.addWidget(self.cond_btns_divider)
        btn_row = QWidget(cond_btns)
        crow = QHBoxLayout(btn_row)
        # 和条件行同一条左缘（44px）
        crow.setContentsMargins(_ROW_INDENT, 4, 0, 4)
        add_cond = QPushButton("＋ 添加条件", btn_row)
        add_cond.setObjectName("Tool")
        add_cond.setCursor(Qt.PointingHandCursor)
        self.add_cond_btn = add_cond
        # 加出来落在空档「-」上，用户再挑条件类型（和③一致，用户 11-13 第八条）
        add_cond.clicked.connect(
            lambda: self._add_condition(Condition(type="", op="")))
        crow.addWidget(add_cond)
        crow.addStretch(1)
        cbox.addWidget(btn_row)
        card2.add_unit(cond_btns, divider=False)
        self._sync_cond_empty()
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
        # 按钮左缘与 ③ 类型下拉对齐（44px）：缩进 = 序号列 + 行距，不含额外空列
        arow.setContentsMargins(_ROW_INDENT, 4, 0, 4)
        # 只有一枚按钮（和②的「＋ 添加条件」一样）：加出来的行落在空档「-」上，
        # 用户自己在下拉里挑虚拟事件 / 延迟 / 发音箱指令。以前是三个按钮各加一种，
        # 加完还得回来改类型才知道有别的档。
        add_action = QPushButton("＋ 添加动作", action_btns)
        add_action.setObjectName("Tool")
        add_action.setCursor(Qt.PointingHandCursor)
        self.add_action_btn = add_action
        add_action.clicked.connect(lambda: self._add_action(Action("")))
        arow.addWidget(add_action)
        arow.addStretch(1)
        card3.add_unit(action_btns)
        col.addWidget(card3)

        # 中枢 / 云端说明**不再上界面**（用户 11-13 第十二条）：
        # ``Sender.call`` 返回 code 0 只代表云端收下了这条 action，极客版本地规则
        # 有没有真的执行、设备有没有动，云端不会回报——只能看设备端反馈。
        # 这句话留在代码里给维护者看，底栏只保留错误条 + 按钮。

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
        self._clear_rows()
        # 触发行：规则里有几条就展开几条。旧文件的单个 keys 在 Rule.from_dict
        # 里已经变成一条 keyboard，所以这里不用再兼容。
        for trig in rule.triggers:
            self._add_trigger(trig, quiet=True)
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
        for row in (*self._trigger_rows, *self._cond_rows, *self._action_rows):
            row.setParent(None)
            row.deleteLater()
        self._trigger_rows.clear()
        self._cond_rows.clear()
        self._action_rows.clear()
        self._recording_row = None
        self._sync_cond_empty()

    def _set_error(self, text: str) -> None:
        """错误条空的时候连一行高度都不占，把高度让给③动作块。"""
        self.error_label.setText(text)
        self.error_label.setVisible(bool(text))

    def _mark_dirty(self, *_args: Any) -> None:
        if self._loading:
            return
        self._dirty = True
        self._refresh_status()

    def _sync_cond_empty(self) -> None:
        """空态块（提示 + 上下缩进分隔线）和按钮上方分隔线二选一，避免叠线。"""
        empty = not self._cond_rows
        self.cond_empty_box.setVisible(empty)
        self.cond_btns_divider.setVisible(not empty)

    def _add_condition(self, cond: Condition, *, quiet: bool = False) -> None:
        row = ConditionRow(cond, self._p, self._mark_dirty, self._remove_condition,
                           self.cond_box)
        self.cond_layout.addWidget(row)
        self._cond_rows.append(row)
        self._sync_cond_empty()
        if not quiet:
            self._mark_dirty()
            if not row._kind_key():
                # 空档行（同 ③）：滚进视野 + 焦点落到类型下拉
                self.area.ensureWidgetVisible(row, 0, 24)
                row.kind.setFocus()

    def _remove_condition(self, row: QWidget) -> None:
        if row in self._cond_rows:
            self._cond_rows.remove(row)
            self.cond_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
            self._sync_cond_empty()
            self._mark_dirty()

    def _add_action(self, action: Action, *, quiet: bool = False) -> None:
        row = ActionRow(action, self._p, self._mark_dirty,
                        self._remove_action, self._move_action,
                        targets=self._speaker_targets,
                        hub_ready=self._hub_ready,
                        hubs=self._hub_targets,
                        parent=self.action_box)
        self.action_layout.addWidget(row)
        self._action_rows.append(row)
        self._renumber()
        if not quiet:
            self._mark_dirty()
            if row.needs_kind():
                # 空档行：滚进视野、焦点直接落到类型下拉——用户刚点了「添加动作」，
                # 下一步必然是挑类型，别让他再去点一次
                self.area.ensureWidgetVisible(row, 0, 24)
                row.kind.setFocus()

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
        self._draft.triggers = [r.value() for r in self._trigger_rows]
        self._draft.conditions = [r.value() for r in self._cond_rows]
        self._draft.actions = [r.value() for r in self._action_rows]
        return self._draft

    # -- 触发行 ----------------------------------------------------------
    def _add_trigger(self, trig: Trigger | None = None, *,
                     quiet: bool = False) -> None:
        """加一条触发。空档「-」先落位，用户再选类型（和 ②③ 一致）。

        ① 允许多条：任意一条命中就执行这条规则，``RuleSet.rules_for`` 负责分发。
        """
        row = TriggerRow(trig or Trigger("", ""), self._p, self.service,
                         self._mark_dirty, self._remove_trigger, self._toggle_record,
                         self.trigger_box)
        self.trigger_layout.addWidget(row)
        self._trigger_rows.append(row)
        if not quiet:
            self._mark_dirty()
            if row.needs_kind():
                self.area.ensureWidgetVisible(row, 0, 24)
                row.kind.setFocus()
        else:
            row.refresh_status(self.service)

    def _remove_trigger(self, row: QWidget) -> None:
        if row not in self._trigger_rows:
            return
        if self._recording_row is row:
            self._recording_row = None
        self._trigger_rows.remove(row)
        self.trigger_layout.removeWidget(row)
        row.setParent(None)
        row.deleteLater()
        self._mark_dirty()

    def _toggle_record(self, row: TriggerRow) -> None:
        """录制只针对某一行：点哪行的键帽就录哪行，别的行先收工。"""
        if row not in self._trigger_rows:
            return
        if self._recording_row is not None and self._recording_row is not row:
            self._recording_row.set_recording(False)
        if self._recording_row is row:
            self._recording_row = None
            row.set_recording(False)
            return
        self._recording_row = row
        row.set_recording(True)
        self._set_error("")

    # -- 快捷键 ----------------------------------------------------------
    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        row = self._recording_row
        if row is None:
            super().keyPressEvent(event)
            return
        if event.key() == Qt.Key_Escape:
            self._recording_row = None
            row.set_recording(False)
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
        self._recording_row = None
        row.set_recording(False)
        row.set_keys(keys)
        event.accept()

    # -- 保存 ------------------------------------------------------------
    def _save(self, *, quiet: bool = False) -> bool:
        rule = self._collect()
        # 还没选类型的行：刚加进来时不亮红字，**点到保存**才亮（11-13 用户第八条）
        rows = [*self._trigger_rows, *self._cond_rows, *self._action_rows]
        for row in rows:
            row.reveal_missing()
        # 填法 / 范围都只在本行报（用户 11-13 指定）：core 只拿到毫秒，看不出
        # 用户敲了什么，混进 validate_rule 只会得到"要在 0–600000 毫秒之间"这种
        # 答非所问。这里也不写底部错误条、不弹 notice——红字就在输入框右边亮着，
        # 再在下面重复一遍是噪音。② 条件行同样走这条路（11-13 第九条）。
        bad = [row for row in rows if row.error_text()]
        if bad:
            for row in rows:
                row.sync_hint()  # 边打边判已经刷过，这里兜住 quiet 保存的路径
            self._set_error("")
            self._reveal_row(bad[0])
            return False
        if not rule.home:
            # 正常路径下 __init__ 已按当前家庭固定；只有登录信息还没回来才可能为空
            self._set_error("当前没有可用家庭，无法保存规则")
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
        """把出错的行滚进视野、焦点交给它该改的那一格。

        行内红字现在是**唯一**的提示（不在底部重复），所以必须保证看得见：
        ③ 里动作一多，出错那行很可能正好滚在视野外面。焦点由行自己说了算
        （``focus_widget()``）：空档行给类型下拉，条件行缺程序名时给那格。
        """
        self.area.ensureWidgetVisible(row, 0, 24)
        widget = row.focus_widget() if hasattr(row, "focus_widget") else None
        if widget is not None:
            widget.setFocus()

    # -- 状态 ------------------------------------------------------------
    def _refresh_status(self) -> None:
        for row in self._trigger_rows:
            row.refresh_status(self.service, self._conflict_note(row))

    def _conflict_note(self, row: TriggerRow) -> str:
        """这个键还被哪条**启用**的规则用着（非阻塞提示）。

        热键是系统级资源：同一个组合被两条规则用到时，``RuleSet.rules_for`` 会把
        两条都叫起来（各触发一次）。这不是错误，但用户多半想知道，所以在行内
        注册状态后面点一句。停用的规则不注册，不算冲突。
        """
        keys = row.value().keys
        if not keys:
            return ""
        try:
            others = self.service.rules.active()
        except Exception:  # noqa: BLE001  验证用的假 service 可能没有 rules
            return ""
        names = [r.name or "未命名规则" for r in others
                 if r.id != self._draft.id and r.enabled
                 and any(t.type == "keyboard" and t.keys == keys for t in r.triggers)]
        if not names:
            return ""
        more = f" 等 {len(names)} 条" if len(names) > 1 else ""
        return f"这个键也被「{names[0]}」{more}使用，按下会各触发一次"

    # -- 关闭 ------------------------------------------------------------
    def reject(self) -> None:  # noqa: N802
        """录制态 / 有未保存修改时，别让「点遮罩 = reject」把页面直接关掉。"""
        if self._recording_row is not None:
            row = self._recording_row
            self._recording_row = None
            row.set_recording(False)
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
