# SPDX-License-Identifier: GPL-3.0-or-later
"""电脑自动化 v1：热键 → 条件 → 按序动作（对某台设备做一件事 / 延迟）。

纯逻辑层（**不 import Qt、不碰网络**，验证脚本能纯离线跑）：

    rules.py      规则的 schema、校验、``rules.json`` 原子读写（唯一真相）
    action_catalog.py  设备动作目录：白名单功能（唤醒 / 播报文字 / 执行文本指令 /
                  发送虚拟事件）与每项的入参元数据、可用性判据、参数清洗
    hotkeys.py    组合键文本 ↔ Win32 修饰符 / 虚拟键码，兼做保留键拦截
    pc_state.py   Windows 系统级 API 只读快照（ctypes，非 Windows 安全退化）
    engine.py     条件求值、AND/OR、三层防重、动作队列、触发日志
    logbook.py    触发日志的中文结果名、筛选 / 查找、表格列（独立日志窗共用）

适配层（**按需单独 import**，本文件不导入，免得纯逻辑验证被 Qt 拖住）：

    hotkeys_win.py ``RegisterHotKey`` + 原生 ``WM_HOTKEY`` 过滤 + 冲突标红
    service.py    Qt 常驻对象：把上面这些接成一台 ``AutomationService``

设备坐标不在本包里：action 名字 → ``siid/aiid`` 与入参顺序由
``mihome/core/miot_action.py`` 现查（顶栏指令条、中枢发送、动作目录三处共用）。

``__init__.py`` 只导出纯逻辑层；界面 / 主窗口请用
``from ..core.automation.service import AutomationService``。
**规则编辑态、冷却表、热键注册表都必须挂常驻对象**（主窗口 / Service），不能挂对话框：
``exec_with_dim_click`` 关窗会 ``deleteLater()`` 销毁弹窗。

契约见 ``docs/reference/automation-hotkeys-and-ui.md`` 第 6 节，明确不做清单见 6.2。
"""

from . import action_catalog
from .engine import (
    CONDITION,
    COOLDOWN,
    DONE,
    ELSE,
    FAILED,
    FIRED,
    REENTRY,
    SENT,
    AutomationEngine,
    LogEntry,
)
from .logbook import (
    FAIL_OUTCOMES,
    NEUTRAL_OUTCOMES,
    OUTCOME_LABELS,
    OUTCOME_ORDER,
    TIME_RANGES,
    WARN_OUTCOMES,
    date_text,
    filter_entries,
    full_text,
    log_columns,
    matches,
    outcome_label,
    outcome_tone,
)
from .hotkeys import (
    MOD_CONTROL,
    MOD_NOREPEAT,
    MOD_SHIFT,
    normalize,
    parse_native,
    reserved_reason,
    to_native,
)
from .pc_state import PcState, Snapshot, availability, evaluate_conditions
from .rules import (
    ACTION_TYPES,
    CONDITION_MODES,
    CONDITION_TYPES,
    DEFAULT_COOLDOWN_MS,
    MAX_COOLDOWN_MS,
    MAX_DELAY_MS,
    MAX_SPEAK_CHARS,
    OPS,
    SCHEMA_VERSION,
    TRIGGER_TYPES,
    Action,
    Condition,
    Rule,
    RuleSet,
    Trigger,
    validate_rule,
)

__all__ = [
    "ACTION_TYPES",
    "action_catalog",
    "CONDITION",
    "CONDITION_MODES",
    "CONDITION_TYPES",
    "COOLDOWN",
    "DEFAULT_COOLDOWN_MS",
    "DONE",
    "ELSE",
    "FAILED",
    "FAIL_OUTCOMES",
    "FIRED",
    "MAX_COOLDOWN_MS",
    "MAX_DELAY_MS",
    "MAX_SPEAK_CHARS",
    "MOD_CONTROL",
    "MOD_NOREPEAT",
    "MOD_SHIFT",
    "NEUTRAL_OUTCOMES",
    "OPS",
    "OUTCOME_LABELS",
    "OUTCOME_ORDER",
    "REENTRY",
    "SCHEMA_VERSION",
    "SENT",
    "TIME_RANGES",
    "TRIGGER_TYPES",
    "WARN_OUTCOMES",
    "Action",
    "AutomationEngine",
    "Condition",
    "LogEntry",
    "PcState",
    "Rule",
    "RuleSet",
    "Snapshot",
    "Trigger",
    "availability",
    "date_text",
    "evaluate_conditions",
    "filter_entries",
    "full_text",
    "log_columns",
    "matches",
    "normalize",
    "outcome_label",
    "outcome_tone",
    "parse_native",
    "reserved_reason",
    "to_native",
    "validate_rule",
]
