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

实现取舍与踩坑见 ``docs/dev/automation.md``「电脑自动化：四层窗口」。
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
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core import speaker
from ..core.automation import (
    CONDITION_TYPES,
    DEFAULT_COOLDOWN_MS,
    MAX_COOLDOWN_MS,
    MAX_DELAY_MS,
    OPS,
    TRIGGER_TYPES,
    Action,
    Condition,
    Rule,
    Trigger,
    action_catalog,
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

#: ④ 空态的两句话。**分开写**：没有条件时的"不能加"和条件就绪时的"可以留空"
#: 不是一回事，混成一句话会让人以为前者也是可选项。
ELSE_HINT_READY = "可以留空：条件成立时照常执行上面的动作，不成立时什么都不做"
ELSE_HINT_NO_COND = "② 里还没有条件：先加一条状态条件，才有「不成立」可走"
#: ③ 空态。新建规则不再预置一行空档动作（11-14），所以这句要告诉用户入口在哪。
ACTION_HINT_EMPTY = "还没有动作：点下面「＋ 选择设备」或「＋ 延迟」开始"


def _rule(parent: QWidget) -> QFrame:
    """卡内 1px 分隔线；和 ``SettingsCard`` 自动补的那条同一样式，但可单独缩进。"""
    line = QFrame(parent)
    line.setObjectName("RowDivider")
    line.setFrameShape(QFrame.NoFrame)
    line.setFixedHeight(1)
    return line

#: 延迟的单位下拉。data 直接就是 ``Action.unit`` 的取值。**延迟只有一种**，毫秒 / 秒
#: 是同一份 ``ms`` 的两把尺子（见 ``Action.unit``）。
_UNIT_LABELS = (("ms", "毫秒"), ("s", "秒"))
#: 设备名按钮的宽度。6 个全角字 + 左右内边距刚好；再长就靠按钮自己省略号
_DEVICE_W = 132
#: 功能下拉的宽度。「执行文本指令」6 个全角字 + chevron，118 刚好不省略
_FEATURE_W = 118
#: 换成延迟时的默认值：两种单位都是 1 秒，换单位不跳数量级
_DELAY_DEFAULTS = {"ms": "1000", "s": "1"}
#: 「静默」下拉（只有带 bool 参数的功能才露出来）。data 是 bool，按入参名进 ``Action.args``
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
    "要先选设备": "这一行还没选设备：点左边的设备名，从当前家庭的中枢网关 / 小爱音箱里挑一台",
    "要先选功能": "先选设备，再在右边挑这台设备要做什么（唤醒 / 播报文字 / 执行文本指令 / …）",
    # 「要填文字」「要填事件名」由功能元数据现拼，见 ActionRow.error_text
    "要填文字": "这一格是要发给设备的文字；不想说就删掉这一行动作",
    "要填事件名": "事件名要和米家自动化里监听的完全一致（区分空格与全角字符）",
    "没有可选设备": "当前家庭里没有能执行动作的设备：需要中枢网关（发虚拟事件）"
                    "或小爱音箱（唤醒 / 播报文字 / 执行文本指令）",
    "不能小于 0": "延迟不能是负数",
    f"不能超过 {MAX_DELAY_MS} 毫秒": f"单条延迟上限 {MAX_DELAY_MS} 毫秒（{MAX_DELAY_MS // 1000} 秒）",
    f"不能超过 {MAX_DELAY_MS // 1000} 秒": f"单条延迟上限 {MAX_DELAY_MS} 毫秒（{MAX_DELAY_MS // 1000} 秒）",
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


def _picker_qss(p: theme.Palette) -> str:
    """选择设备窗：页面样式 + 列表行。

    行高 40、圆角 hover，和设置页左侧分类栏同一套观感；**不用** ``item:selected``
    之外的强调色——这一窗只回答"是哪台"，不该有第二个焦点。
    """
    return _page_qss(p) + f"""
    QListWidget#PickerList {{
        background: transparent; border: none; outline: none;
        font-size: 13px; color: {p.text};
    }}
    QListWidget#PickerList::item {{
        height: 40px; padding: 0 10px; border-radius: 8px; color: {p.text};
    }}
    QListWidget#PickerList::item:hover {{ background: {theme.popup_row_hover(p)}; }}
    QListWidget#PickerList::item:selected {{
        background: {theme.panel_row_selected(p)}; color: {p.text};
    }}
    QListWidget#PickerList::item:disabled {{ color: {p.text_soft}; }}
    """


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


def _action_devices(service: AutomationService) -> list[action_catalog.ActionDevice]:
    """当前家庭里能执行动作的设备（中枢 + 音箱）；取不到就当没有。

    为什么要吞异常：这一层只决定"选择窗里有几台"，探测失败不该把整个编辑窗带崩
    （离线验证用的假 service 根本没有这个方法）。
    """
    getter = getattr(service, "action_devices", None)
    if not callable(getter):
        return []
    try:
        return list(getter())
    except Exception:  # noqa: BLE001
        return []


def _new_delay() -> Action:
    """「＋ 延迟」加出来的行：默认 1 秒（和 ``_DELAY_DEFAULTS["ms"]`` 同一档），用户改。"""
    return Action("delay", ms=1000)


class DevicePicker(QDialog):
    """「选择设备」窗：列出当前家庭里**能做点什么**的设备，点一下就拿走。

    只列 ``action_catalog.collect`` 挑出来的那几台（中枢 + 音箱），不是全部设备：
    灯、扫地机没有可执行动作，列进来只会让人点进一个空功能列表。判据在 core 里，
    这一层不重复筛。

    **单击即选中并关闭**（用户 11-14）：这不是多选窗，犹豫的代价是再点一次按钮。
    键盘上下键移动、回车确认走 ``QListWidget`` 自带的那套，不另写焦点逻辑。
    """

    def __init__(self, devices: list[action_catalog.ActionDevice],
                 palette: theme.Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._p = palette
        self._devices = list(devices)
        self._chosen: action_catalog.ActionDevice | None = None
        self.setWindowTitle("选择设备")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self._frame = WindowFrame(self, resizable=True,
                                  radius=theme.RADIUS_CARD,
                                  shadow=SHADOW_PAD,
                                  shadow_alpha=shadow_alpha(self._p),
                                  fill=lambda: surface_bg(self._p),
                                  border=lambda: surface_border(self._p))
        self.resize(*floating_size(420, fit_height(560, reserve=2 * SHADOW_PAD)))
        self.setMinimumSize(*floating_size(360, 320))
        self.setStyleSheet(_picker_qss(palette))

        outer = QVBoxLayout(self._frame.body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.title_bar = TitleBar("选择设备", palette,
                                  subtitle="点一台设备，再用它做点什么",
                                  buttons=("close",), variant="dialog",
                                  frame=self._frame)
        outer.addWidget(self.title_bar)

        body = QWidget(self._frame.body)
        col = QVBoxLayout(body)
        col.setContentsMargins(16, 8, 16, 14)
        col.setSpacing(10)
        _p = palette

        self.search = QLineEdit(body)
        self.search.setObjectName("Field")
        self.search.setPlaceholderText("搜索设备名或房间")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        col.addWidget(self.search)

        self.list = QListWidget(body)
        self.list.setObjectName("PickerList")
        self.list.setFrameShape(QFrame.NoFrame)
        self.list.setUniformItemSizes(True)
        self.list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.list.itemClicked.connect(self._on_item)
        self.list.itemActivated.connect(self._on_item)
        for dev in self._devices:
            item = QListWidgetItem(self._row_text(dev))
            item.setData(Qt.UserRole, dev.did)
            item.setToolTip(f"{dev.label}　{dev.room or '未分配房间'}")
            if not dev.online:
                item.setFlags(item.flags() & ~Qt.ItemIsSelectable)
            self.list.addItem(item)
        col.addWidget(self.list, 1)

        self.empty = QLabel("当前家庭没有可执行动作的设备。", body)
        self.empty.setObjectName("Hint")
        self.empty.setWordWrap(True)
        self.empty.setVisible(not self._devices)
        self.list.setVisible(bool(self._devices))
        col.addWidget(self.empty)
        outer.addWidget(body, 1)
        tab_focus_only(self)
        if self._devices:
            self.list.setCurrentRow(0)

    # -- 数据 ------------------------------------------------------------
    @staticmethod
    def _row_text(dev: action_catalog.ActionDevice) -> str:
        """一行字：``名称　　房间　　离线``。功能数放最后，免得有人以为只能做一件事。"""
        room = dev.room or "未分配"
        tail = "　离线" if not dev.online else ""
        return f"{dev.label}　　{room}{tail}"

    def chosen(self) -> action_catalog.ActionDevice | None:
        return self._chosen

    # -- 交互 ------------------------------------------------------------
    def _on_item(self, item: QListWidgetItem) -> None:
        if not (item.flags() & Qt.ItemIsSelectable):
            return
        did = str(item.data(Qt.UserRole) or "")
        self._chosen = next((d for d in self._devices if d.did == did), None)
        if self._chosen is not None:
            self.accept()

    def _apply_filter(self, text: str) -> None:
        want = text.strip().lower()
        shown = 0
        for row, dev in enumerate(self._devices):
            hit = (not want or want in dev.label.lower()
                   or want in (dev.room or "").lower())
            self.list.item(row).setHidden(not hit)
            shown += 1 if hit else 0
        self.empty.setText("没有匹配的设备。" if want
                           else "当前家庭没有可执行动作的设备。")
        self.empty.setVisible(shown == 0)
        if shown == 0:
            self.list.setVisible(bool(want) and bool(self._devices))
            if not want:
                self.list.setVisible(False)

    @staticmethod
    def pick(devices: list[action_catalog.ActionDevice],
             palette: theme.Palette, parent: QWidget | None = None):
        """弹一次窗；选中返回设备，取消返回 ``None``。"""
        if not devices:
            notice(parent, "没有可选设备",
                   "当前家庭里没有能执行动作的设备（中枢网关或小爱音箱）。")
            return None
        dlg = DevicePicker(devices, palette, parent)
        ok = dlg.exec() == QDialog.Accepted
        got = dlg.chosen() if ok else None
        dlg.deleteLater()
        return got


class ActionRow(QWidget):
    """一行动作 = 序号 + 设备 + 功能 + 该功能的参数 + ↑↓×。

    形态跟着**功能**走（11-14 用户定：动作不再按"类型"分类，而是"对哪台设备做什么"）::

        延迟          [毫秒 ▾][1000]
        唤醒          [小爱音箱Pro][唤醒]
        播报文字      [小爱音箱Pro][播报文字][门铃响了____]
        执行文本指令  [小爱音箱Pro][执行文本指令][静默 ▾][关灯____]
        发送虚拟事件  [客厅中枢][发送虚拟事件][事件名____]

    三件事刻意如此：

    * **参数列按功能长出来**，不是按类型写死。加功能只改 ``action_catalog.FEATURES``，
      这里不动（``Param.kind`` 目前只有 text / bool 两档）。
    * **无参功能不留禁用空框**（唤醒）：一个灰着的输入框会让人以为"这里该填点什么、
      只是坏了"。直接把那一列收掉，行短一截，反而说明了"没有参数"。
    * **设备名是一个按钮**，点它重开选择窗。不另做设备下拉：两套入口迟早不一致，而且
      设备一多，下拉里放不下房间与在线状态（那正是选择窗要显示的东西）。

    ``devices`` 是当前家庭的候选。存盘只认 ``did``——设备改名、被移到别的家庭，规则
    照样能跑；``device`` 只是日志与回显用的名字快照。
    """

    def __init__(
        self,
        action: Action,
        palette: theme.Palette,
        on_change: Callable[[], None],
        on_remove: Callable[[QWidget], None],
        on_move: Callable[[QWidget, int], None],
        on_pick_device: Callable[[ActionRow], None] | None = None,
        devices: list[action_catalog.ActionDevice] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_change = on_change
        self._loading = True
        self._devices = {d.did: d for d in (devices or [])}
        self._did = action.did
        self._device_name = action.device
        #: 行的形状：``delay`` 只有单位与数值；``device_action`` 才有设备 / 功能
        self._type = "delay" if action.type == "delay" else "device_action"
        self._prev_unit = action.unit if action.unit == "s" else "ms"
        #: "缺东西"的红字只在点过保存后才亮（``reveal_missing()``），理由同 ①②：
        #: 刚加一行就一片红是在骂人。填法错误（位数 / 超长 / 脏字符）例外——那是
        #: 用户正在敲的东西，边打边说要紧。
        self._show_missing = False

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 3, 0, 3)
        lay.setSpacing(_ROW_GAP)

        self.index_label = QLabel("", self)
        self.index_label.setObjectName("Hint")
        self.index_label.setFixedWidth(_INDEX_W)
        # 序号靠右：它占的是和 ①② 标题列等宽的那一列，贴着右边的设备按钮
        self.index_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.device_btn = QPushButton("", self)
        self.device_btn.setObjectName("Tool")
        # 和其他按钮一样只在 Tab 时拿焦点：点完不该留一个焦点框在行里
        self.device_btn.setFocusPolicy(Qt.TabFocus)
        self.device_btn.setCursor(Qt.PointingHandCursor)
        self.device_btn.setFixedWidth(_DEVICE_W)
        self.device_btn.clicked.connect(
            lambda: on_pick_device(self) if on_pick_device is not None else None)

        # 功能（spec 里的 action 名）。候选跟着所选设备走，见 _fill_features
        self.kind = Combo(palette, self)
        self.kind.setFixedWidth(_FEATURE_W)

        # 延迟的单位：同一份毫秒值的两把尺子
        self.unit = Combo(palette, self)
        for key, label in _UNIT_LABELS:
            self.unit.addItem(label, key)
        self.unit.setFixedWidth(80)

        # bool 参数（目前只有「静默」）。固定放在输入框**左边**：输入框要占满剩余宽度，
        # 它在右边的话会被拉成一条特别长的框。
        self.silent = Combo(palette, self)
        for value, label in _SILENT_LABELS:
            self.silent.addItem(label, value)
        self.silent.setFixedWidth(92)

        # text 参数与延迟数值共用这一个框：同一时刻只会有一种行
        self.value_edit = QLineEdit(self)
        self.value_edit.setObjectName("Field")
        self.value_edit.textChanged.connect(self._on_text)

        # 无参功能时顶住右半边，让 ↑↓× 老实待在行尾（否则按钮会左跳到下拉旁边）
        self.spacer = QWidget(self)

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
        lay.addWidget(self.device_btn)
        lay.addWidget(self.kind)
        lay.addWidget(self.unit)
        lay.addWidget(self.silent)
        lay.addWidget(self.value_edit, 1)
        lay.addWidget(self.spacer, 1)
        lay.addWidget(self.hint)
        for btn in (self.up_btn, self.down_btn, self.delete_btn):
            lay.addWidget(btn)

        # 控件都建好再连：下面回填值时不能让 _feature_changed 抢先清空输入框
        self.kind.currentIndexChanged.connect(self._feature_changed)
        self.unit.currentIndexChanged.connect(self._unit_changed)
        self.silent.currentIndexChanged.connect(self._selection_changed)

        with QSignalBlocker(self.kind), QSignalBlocker(self.unit), \
                QSignalBlocker(self.silent):
            self.unit.setCurrentIndex(max(0, self.unit.findData(self._prev_unit)))
        self._fill_features(action.action)
        if self._type == "delay":
            self.value_edit.setText(_delay_text(action.ms, self._prev_unit))
        else:
            feat = self._feat()
            if feat is not None:
                for param in feat.params:
                    if param.kind == "bool":
                        want = bool(action.args.get(param.name, param.default))
                        with QSignalBlocker(self.silent):
                            self.silent.setCurrentIndex(
                                max(0, self.silent.findData(want)))
                    else:
                        self.value_edit.setText(
                            str(action.args.get(param.name, "") or ""))
        self._loading = False
        self._sync_device_button()
        self._sync_columns()
        self._sync_placeholder()
        self.sync_hint()

    # -- 设备 / 功能 ------------------------------------------------------
    def device(self) -> action_catalog.ActionDevice | None:
        return self._devices.get(self._did)

    def set_device(self, dev: action_catalog.ActionDevice) -> None:
        """换设备。**尽量保留已选的功能**：同一家庭里两台音箱都能播报时，换设备不该
        让用户重选一次；新设备没有那个功能才落到第一个。"""
        keep = self._kind_key()
        self._did = dev.did
        self._device_name = dev.label
        self._fill_features(keep)
        self._sync_device_button()
        self._sync_columns()
        self._sync_placeholder()
        self.sync_hint()
        self._on_change()

    def _sync_device_button(self) -> None:
        dev = self.device()
        name = dev.label if dev is not None else (self._device_name or "选择设备")
        if dev is not None and not dev.online:
            name += "（离线）"
        self.device_btn.setText(name)
        if dev is None:
            self.device_btn.setToolTip("这台设备不在当前家庭的候选里（可能被移除或换了家庭）")
        else:
            room = dev.room or "未分配房间"
            self.device_btn.setToolTip(f"{dev.label}　{room}\n点一下重新选择设备")

    def _fill_features(self, keep: str = "") -> None:
        """按当前设备重建功能下拉。

        设备不在候选里（被移除 / 换了家庭）时退回**整个白名单**：这一行仍然要能显示
        它原来选的功能，否则用户只会看到一台不认识的设备加一个空下拉。
        """
        dev = self.device()
        feats = dev.features if dev is not None else action_catalog.FEATURES
        with QSignalBlocker(self.kind):
            self.kind.clear()
            for feat in feats:
                self.kind.addItem(feat.label, feat.name)
            index = self.kind.findData(keep) if keep else -1
            if index < 0 and self.kind.count():
                index = 0
            self.kind.setCurrentIndex(index)

    def _feature_changed(self) -> None:
        if self._loading:
            return
        # 换功能要把不适用的值清掉：「执行文本指令」切到「唤醒」后残留的文字，
        # 改回来时看着像自动填了上次的内容，其实那已经不该属于这一行。
        feat = self._feat()
        with QSignalBlocker(self.value_edit):
            self.value_edit.setText("" if feat is not None else "")
        self._sync_columns()
        self._sync_placeholder()
        self.sync_hint()
        self._on_change()

    def _feat(self) -> action_catalog.Feature | None:
        return action_catalog.feature(self._kind_key())

    def _kind_key(self) -> str:
        return str(self.kind.currentData() or "")

    def _selection_changed(self, *_args: Any) -> None:
        if self._loading:
            return
        self.sync_hint()
        self._on_change()

    def _on_text(self, *_args: Any) -> None:
        """输入即校验：红字跟着光标走，不用等点保存才发现填错。"""
        if self._loading:
            return
        self.sync_hint()
        self._on_change()

    def _unit_changed(self) -> None:
        """毫秒 ↔ 秒只换算不重置：2000 ms ⇄ 2 秒是同一个量。"""
        if self._loading:
            return
        unit = self._unit()
        if self._is_delay() and unit != self._prev_unit:
            ms = _delay_ms(self.value_edit.text(), self._prev_unit)
            self.value_edit.setText(_delay_text(ms, unit) if ms >= 0
                                    else _DELAY_DEFAULTS[unit])
        self._prev_unit = unit
        self._sync_placeholder()
        self.sync_hint()
        self._on_change()

    # -- 列显隐 ----------------------------------------------------------
    def _sync_columns(self) -> None:
        """按行的形状与功能的参数表显隐那几列。隐藏的控件在 QHBoxLayout 里不占位置。"""
        delay = self._is_delay()
        feat = None if delay else self._feat()
        has_bool = feat is not None and any(p.kind == "bool" for p in feat.params)
        has_text = feat is not None and any(p.kind == "text" for p in feat.params)

        self.device_btn.setVisible(not delay)
        self.kind.setVisible(not delay)
        self.unit.setVisible(delay)
        self.silent.setVisible(has_bool)
        self.value_edit.setVisible(delay or has_text)
        # 没有输入框时用占位控件顶住右半边；有输入框时它必须让位（两个都 stretch 会把
        # 输入框挤成一半宽）
        self.spacer.setVisible(not (delay or has_text))

    def _sync_placeholder(self) -> None:
        if self._is_delay():
            self.value_edit.setPlaceholderText(
                "秒，如 30 或 1.5（最多 3 位小数）" if self._unit() == "s"
                else "毫秒，如 2000（只填整数）")
            return
        feat = self._feat()
        if feat is None:
            self.value_edit.setPlaceholderText("先选功能")
            return
        text_param = next((p for p in feat.params if p.kind == "text"), None)
        if text_param is None:
            self.value_edit.setPlaceholderText("这个功能没有参数")
        elif feat.name == "emit-virtual-event":
            self.value_edit.setPlaceholderText("事件名（要和米家自动化里监听的完全一致）")
        elif feat.name == "play-text":
            self.value_edit.setPlaceholderText("要念出来的话，如「B 程序已经运行 20 分钟」")
        elif feat.name == "execute-text-directive":
            self.value_edit.setPlaceholderText("要说的话，如「打开客厅灯」")
        else:
            self.value_edit.setPlaceholderText(text_param.label)

    # -- 校验 ------------------------------------------------------------
    def format_error(self) -> str:
        """这一行**填法**的问题，没有则返回空串（延迟的进制 / 位数、文本的长度与字符）。

        延迟只能在这一层判：``Action`` 交出去的只有毫秒，core 看不出用户敲的是
        ``1.5 秒`` 还是 ``1500 毫秒``，更看不出多打了第 4 位小数。两条规矩：
        毫秒档只收整数（半毫秒没有意义）；秒档最多 3 位小数（执行精度就是 1 ms）。
        超出就**明说**，不静默四舍五入——用户专门问过精度这件事。
        """
        if not self._is_delay():
            return self._text_error()
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

    def _text_error(self) -> str:
        """文本参数的填法问题。上限与字符白名单都取自功能的元数据，不在这里写死 200。"""
        feat = self._feat()
        param = next((p for p in feat.params if p.kind == "text"), None) \
            if feat is not None else None
        if param is None:
            return ""
        text = self.value_edit.text()
        # 超长不静默截断（主窗口的发送框才那么干：它没有"存下来再说"的机会）。
        # 编辑页要落盘，**报出差额**让用户自己决定删哪句，比悄悄砍掉尾巴好。
        if param.max_chars and len(text) > param.max_chars:
            return f"多了 {len(text) - param.max_chars} 个字"
        if param.whitelist:
            dropped = speaker.clean(text).dropped
            if dropped:
                return f"含 {len(dropped)} 个发不出的字符"
        return ""

    def error_text(self) -> str:
        """行内红字的全文；空串 = 这一行合规矩。

        顺序是先具体后笼统：填法（整数 / 位数 / 字数）→ 范围 → 缺失项。**范围紧跟
        填法**：用户正在敲的那个数已经能看出超了（``601`` 秒），不该等到点保存才说；
        而"还没选设备 / 还没填"这种缺失项走 ``reveal_missing()``，免得刚加一行就一片红。
        """
        err = self.format_error()
        if err:
            return err
        if self._is_delay():
            raw = self.value_edit.text().strip()
            if not raw:
                return "要填延迟时间" if self._show_missing else ""
            ms = _delay_ms(raw, self._unit())
            if ms < 0:
                return "不能小于 0"
            if ms > MAX_DELAY_MS:
                return (f"不能超过 {MAX_DELAY_MS // 1000} 秒" if self._unit() == "s"
                        else f"不能超过 {MAX_DELAY_MS} 毫秒")
            return ""
        if not self._show_missing:
            return ""
        if not self._did:
            return "要先选设备"
        feat = self._feat()
        if feat is None:
            return "要先选功能"
        for param in feat.params:
            if param.kind != "text" or not param.required:
                continue
            if not self.value_edit.text().strip():
                return f"要填{param.label}"
        return ""

    def reveal_missing(self) -> None:
        """点了保存才把"缺东西"的红字亮出来（①② 的行是同一套做法）。"""
        self._show_missing = True
        self.sync_hint()

    def sync_hint(self) -> None:
        text = self.error_text()
        self.hint.setText(text)
        self.hint.setToolTip(self._hint_tip(text))
        self.hint.setVisible(bool(text))

    def _hint_tip(self, text: str) -> str:
        """红字的来龙去脉。带数字的文案（「多了 37 个字」「含 3 个发不出的字符」）
        没法预先写进 ``_HINT_TIPS``，按当前内容现拼。"""
        tip = _HINT_TIPS.get(text)
        if tip:
            return tip
        feat = None if self._is_delay() else self._feat()
        param = next((p for p in feat.params if p.kind == "text" and p.whitelist),
                     None) if feat is not None else None
        if param is None:
            return ""
        raw = self.value_edit.text()
        if param.max_chars and len(raw) > param.max_chars:
            return (f"米家「自定义指令」上限 {param.max_chars} 个字符；"
                    f"当前 {len(raw)} 个，删掉 {len(raw) - param.max_chars} 个才能保存")
        dropped = speaker.clean(raw).dropped
        if dropped:
            return ("米家只接受中英文、数字、空格和「，。？！」；"
                    f"这几个要删掉或换掉：{dropped}")
        return ""

    # -- 杂项 ------------------------------------------------------------
    def set_index(self, number: int) -> None:
        self.index_label.setText(f"{number}.")

    def set_move_enabled(self, up: bool, down: bool) -> None:
        """上移 / 下移**从不置灰**（用户 11-13 第十二条）：第一行按 ↑、最后一行按 ↓
        顺序不变即可，灰按钮反而像"这一行坏了"。``up`` / ``down`` 只为接口兼容
        （调用方仍按位置传），按钮一律可点。"""
        self.up_btn.setEnabled(True)
        self.down_btn.setEnabled(True)
        self.up_btn.setToolTip("上移这条动作" if up else "已经是第一条，按了也不会动")
        self.down_btn.setToolTip("下移这条动作" if down else "已经是最后一条，按了也不会动")

    def focus_widget(self) -> QWidget:
        """出错时焦点该给谁：没选设备 → 设备按钮；没选功能 → 功能下拉。"""
        if not self._is_delay() and not self._did:
            return self.device_btn
        if not self._is_delay() and self._feat() is None:
            return self.kind
        return self.value_edit if self.value_edit.isVisible() else self.kind

    def _is_delay(self) -> bool:
        return self._type == "delay"

    def _unit(self) -> str:
        """当前录入刻度："ms" / "s"。存盘与校验一律用毫秒。"""
        return str(self.unit.currentData() or "ms")

    def value(self) -> Action:
        """读回一条动作。延迟**永远是毫秒**，秒档在这里折算——界面之外没人认"秒"。"""
        if self._is_delay():
            unit = self._unit()
            return Action("delay", ms=_delay_ms(self.value_edit.text(), unit), unit=unit)
        feat = self._feat()
        args: dict[str, Any] = {}
        if feat is not None:
            for param in feat.params:
                if param.kind == "bool":
                    args[param.name] = bool(self.silent.currentData())
                else:
                    args[param.name] = self.value_edit.text().strip()
        dev = self.device()
        name = dev.label if dev is not None else self._device_name
        return Action("device_action", did=self._did, device=name,
                      action=(feat.name if feat is not None else ""), args=args)


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
                # 新建**不给动作行**（11-14）：动作是"对哪台设备做什么"，没有默认值可
                # 预选。③ 里给一句空态提示，用户点「＋ 选择设备」或「＋ 延迟」自己加。
                actions=[],
                cooldown_ms=DEFAULT_COOLDOWN_MS, enabled=True,
                home=self._home,
            )
        )
        if not self._draft.home:
            self._draft.home = self._home
        self._dirty = False
        self._loading = False
        #: 正在录制的触发行（None = 没在录）。录制只认这一行，多行不会串键。
        self._recording_row: TriggerRow | None = None
        self._trigger_rows: list[TriggerRow] = []
        self._cond_rows: list[ConditionRow] = []
        self._action_rows: list[ActionRow] = []
        #: ④ 否则分组的动作行。和 ③ 分开存：两段的序号各自从 1 开始，
        #: 上下移也不能串到对方那一段里。
        self._else_rows: list[ActionRow] = []
        #: 当前家庭里能执行动作的设备（中枢 + 音箱）。取一次就够——本窗是模态的，
        #: 开着的时候切不了家庭。行内换设备也在这一份里挑，所以两处永远一致。
        self._devices = _action_devices(service)

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
        # 语义是 OR：任意一条命中就执行这条规则；新规则点「＋ 添加触发」才加
        # 第一行。没有序号，用 28px 左内边距对齐到 ③ 序号之后的下拉框（44px）。
        self.trigger_box = QWidget(content)
        self.trigger_layout = QVBoxLayout(self.trigger_box)
        self.trigger_layout.setContentsMargins(0, 0, 0, 0)
        self.trigger_layout.setSpacing(2)

        # 底部一行：左边「＋ 添加触发」，右边冷静期。冷静期标签贴着它自己的
        # 输入框（不再被弹性标题列推到最左边），语义上仍属于①。
        trigger_btns = QWidget(content)
        brow = QHBoxLayout(trigger_btns)
        brow.setContentsMargins(_ROW_INDENT, 4, 0, 4)
        brow.setSpacing(8)
        self.add_trigger_btn = QPushButton("＋ 添加触发", trigger_btns)
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
        # 空态提示：没有动作行时才显示。不加分隔线——这时候它就是卡片里唯一的内容。
        self.action_empty = QLabel(ACTION_HINT_EMPTY, content)
        self.action_empty.setObjectName("Hint")
        self.action_empty.setContentsMargins(_ROW_INDENT, 8, 0, 8)
        card3.add_unit(self.action_empty, divider=False)
        action_btns = QWidget(content)
        arow = QHBoxLayout(action_btns)
        # 按钮左缘与行内设备列对齐（44px）：缩进 = 序号列 + 行距，不含额外空列
        arow.setContentsMargins(_ROW_INDENT, 4, 0, 4)
        # **两枚**按钮（11-14）：动作是"对某台设备做一件事"或"等一会"，所以入口就是
        # 「选择设备」和「延迟」。设备要先选出来才谈得上功能，所以点「选择设备」先弹窗，
        # 选中了才加行——不像以前先加一个空档行再让用户去下拉里挑。
        pick_action = QPushButton("＋ 选择设备", action_btns)
        pick_action.setObjectName("Tool")
        pick_action.setCursor(Qt.PointingHandCursor)
        pick_action.setToolTip("从当前家庭的中枢网关 / 小爱音箱里挑一台，再选它做什么")
        self.add_action_btn = pick_action
        pick_action.clicked.connect(lambda: self._pick_and_add())
        add_delay = QPushButton("＋ 延迟", action_btns)
        add_delay.setObjectName("Tool")
        add_delay.setCursor(Qt.PointingHandCursor)
        add_delay.setToolTip("等一段时间再执行下一步")
        self.add_delay_btn = add_delay
        add_delay.clicked.connect(lambda: self._add_action(_new_delay()))
        arow.addWidget(pick_action)
        arow.addWidget(add_delay)
        arow.addStretch(1)
        card3.add_unit(action_btns)
        col.addWidget(card3)

        # ④ 否则执行动作。结构**整套复用 ③**（同一款 ActionRow / 同一枚按钮 /
        # 同样从 1 开始编号），区别只有两点：条件不成立才跑；② 为空时不能加行。
        # 动作执行失败**不**走这一段（用户 11-14）：那是故障，走系统通知 + 日志。
        col.addLayout(self._step_header(content, "4", "否则执行动作（条件不成立时）"))
        card4 = SettingsCard(content)
        self._set_step(card4, "4")
        self.else_card = card4
        self.else_box = QWidget(content)
        self.else_layout = QVBoxLayout(self.else_box)
        self.else_layout.setContentsMargins(0, 0, 0, 0)
        self.else_layout.setSpacing(2)
        card4.add_unit(self.else_box)
        # 空态提示：没有行时才显示。不加分隔线——它这时候就是卡片里唯一的内容。
        self.else_hint = QLabel(ELSE_HINT_READY, content)
        self.else_hint.setObjectName("Hint")
        self.else_hint.setContentsMargins(_ROW_INDENT, 8, 0, 8)
        card4.add_unit(self.else_hint, divider=False)
        else_btns = QWidget(content)
        erow = QHBoxLayout(else_btns)
        # 和 ④ 的动作行设备列同一条左缘（序号列 + 行距），和 ③ 的对齐规则一致
        erow.setContentsMargins(_ROW_INDENT, 4, 0, 4)
        pick_else = QPushButton("＋ 选择设备", else_btns)
        pick_else.setObjectName("Tool")
        pick_else.setCursor(Qt.PointingHandCursor)
        pick_else.setToolTip("从当前家庭的中枢网关 / 小爱音箱里挑一台，再选它做什么")
        self.add_else_btn = pick_else
        pick_else.clicked.connect(lambda: self._pick_and_add(to_else=True))
        add_else_delay = QPushButton("＋ 延迟", else_btns)
        add_else_delay.setObjectName("Tool")
        add_else_delay.setCursor(Qt.PointingHandCursor)
        add_else_delay.setToolTip("等一段时间再执行下一步")
        self.add_else_delay_btn = add_else_delay
        add_else_delay.clicked.connect(
            lambda: self._add_else_action(_new_delay()))
        erow.addWidget(pick_else)
        erow.addWidget(add_else_delay)
        erow.addStretch(1)
        card4.add_unit(else_btns)
        col.addWidget(card4)

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
        for act in rule.else_actions:
            self._add_else_action(act, quiet=True)
        self._renumber()
        self._sync_else_enabled()
        self._loading = False
        self._dirty = False
        self._set_error("")
        self._refresh_status()

    def _clear_rows(self) -> None:
        for row in (*self._trigger_rows, *self._cond_rows, *self._action_rows,
                    *self._else_rows):
            row.setParent(None)
            row.deleteLater()
        self._trigger_rows.clear()
        self._cond_rows.clear()
        self._action_rows.clear()
        self._else_rows.clear()
        self._recording_row = None
        self._sync_cond_empty()
        self._sync_action_empty()

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
        # ④ 的「能不能加行」完全由 ② 决定，条件条数一变就跟着刷新（增 / 删 /
        # 载入规则 / 清空全都会走到这里）
        self._sync_else_enabled()

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
                        on_pick_device=self._pick_device_for,
                        devices=self._devices,
                        parent=self.action_box)
        self.action_layout.addWidget(row)
        self._action_rows.append(row)
        self._renumber()
        self._sync_action_empty()
        if not quiet:
            self._mark_dirty()
            # 刚加的行：滚进视野、焦点落到该填的地方（延迟→数值框，设备动作→功能
            # 下拉）。用户刚点了按钮，下一步必然是填东西，别让他再点一次。
            # **先 show()**：新建的 QWidget 要等布局激活才算"显示"，在那之前
            # setFocus() 是空调用，焦点会留在滚动区上（点了按钮却要再点一次输入框）。
            row.show()
            self.area.ensureWidgetVisible(row, 0, 24)
            row.focus_widget().setFocus()

    def _pick_and_add(self, *, to_else: bool = False) -> None:
        """「＋ 选择设备」：**先弹窗，选中才加行**（取消就什么都不加）。

        和旧版"先加一个空档行、再让用户去下拉里挑"的差别：动作的主语是设备，
        没有设备就无从谈起，先加一行空的只会留下一个半成品。
        """
        dev = DevicePicker.pick(self._devices, self._p, self)
        if dev is None:
            return
        feat = dev.features[0] if dev.features else None
        act = Action("device_action", did=dev.did, device=dev.label,
                     action=(feat.name if feat is not None else ""), args={})
        if to_else:
            self._add_else_action(act)
        else:
            self._add_action(act)

    def _pick_device_for(self, row: ActionRow) -> None:
        """行内点设备名：重开同一个选择窗，选完就地换设备（功能尽量保留）。"""
        dev = DevicePicker.pick(self._devices, self._p, self)
        if dev is not None:
            row.set_device(dev)

    def _sync_action_empty(self) -> None:
        box = getattr(self, "action_empty", None)
        if box is None:  # 兜住 _clear_rows 早于 _build_editor 的调用顺序
            return
        box.setVisible(not self._action_rows)

    def _remove_action(self, row: QWidget) -> None:
        if row in self._action_rows:
            self._action_rows.remove(row)
            self.action_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
            self._renumber()
            self._sync_action_empty()
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

    # -- ④ 否则动作 ------------------------------------------------------
    def _add_else_action(self, action: Action, *, quiet: bool = False) -> None:
        row = ActionRow(action, self._p, self._mark_dirty,
                        self._remove_else_action, self._move_else_action,
                        on_pick_device=self._pick_device_for,
                        devices=self._devices,
                        parent=self.else_box)
        self.else_layout.addWidget(row)
        self._else_rows.append(row)
        self._renumber_else()
        self._sync_else_enabled()
        if not quiet:
            self._mark_dirty()
            row.show()  # 理由同 ③：不先显示出来，setFocus() 落不到这一行
            self.area.ensureWidgetVisible(row, 0, 24)
            row.focus_widget().setFocus()

    def _remove_else_action(self, row: QWidget) -> None:
        if row in self._else_rows:
            self._else_rows.remove(row)
            self.else_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
            self._renumber_else()
            self._sync_else_enabled()
            self._mark_dirty()

    def _move_else_action(self, row: QWidget, delta: int) -> None:
        if row not in self._else_rows:
            return
        i = self._else_rows.index(row)
        j = max(0, min(len(self._else_rows) - 1, i + delta))
        if i == j:
            return
        self._else_rows.pop(i)
        self._else_rows.insert(j, row)
        self.else_layout.removeWidget(row)
        self.else_layout.insertWidget(j, row)
        self._renumber_else()
        self._mark_dirty()

    def _renumber_else(self) -> None:
        n = len(self._else_rows)
        for i, row in enumerate(self._else_rows, 1):
            row.set_index(i)
            row.set_move_enabled(up=i > 1, down=i < n)

    def _sync_else_enabled(self) -> None:
        """④ 跟着 ② 走：没有条件就谈不上"条件不成立"，所以加动作按钮灰掉。

        **已有动作不清空**（用户 11-14 明确定）：删条件可能只是要换成别的，不是要
        扔掉整段否则动作。真删光了还去保存，由 ``validate_rule`` 拦下来，并说清
        「要么加条件，要么清空这一段」。
        """
        btn = getattr(self, "add_else_btn", None)
        if btn is None:  # 兜住 _clear_rows 早于 _build_editor 的调用顺序
            return
        has_cond = bool(self._cond_rows)
        for button in (btn, getattr(self, "add_else_delay_btn", None)):
            if button is None:
                continue
            button.setEnabled(has_cond)
            button.setToolTip("" if has_cond
                              else "先在上面「状态条件」里加一条，否则这里永远不会触发")
        hint = getattr(self, "else_hint", None)
        if hint is not None:
            hint.setText(ELSE_HINT_READY if has_cond else ELSE_HINT_NO_COND)
            hint.setVisible(not self._else_rows)

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
        self._draft.else_actions = [r.value() for r in self._else_rows]
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
        rows = [*self._trigger_rows, *self._cond_rows, *self._action_rows,
                *self._else_rows]
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
