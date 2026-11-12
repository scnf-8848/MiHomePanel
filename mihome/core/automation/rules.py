# SPDX-License-Identifier: GPL-3.0-or-later
"""三段式规则的 schema、校验与 ``rules.json`` 原子读写。

**这是规则的唯一真相。** 键位、条件、动作、冷静期全在每条规则里，不存在单独的
``hotkeys.json`` 或 ``prefs.json`` 副本（Windows 注册热键不需要任何文件，见
``docs/reference/automation-hotkeys-and-ui.md`` 第 2 节）。

形状（v1，对应米家 App「创建自动化」页）：

    {id, name, enabled, keys, condition_mode, conditions[], actions[], cooldown_ms}
    conditions[] = {type, op, arg}        空数组 = 无条件
    actions[]    = {type:"virtual_event", name} | {type:"delay", ms}

**软删除**：列表里的「删除」只是打 ``deleted`` 标记（连同 ``deleted_at`` 一起落盘），
规则仍留在 ``rules.json`` 里，但 ``active()`` / ``enabled_keys()`` / ``rules_for()``
全部跳过它，热键随即释放。回收站里可 ``restore()`` 还原，或 ``purge()`` 彻底删除。
所以列表点删除**不需要二次确认**（可恢复），只有彻底删除才弹确认。

读入走**字段级宽容修复**：某个字段坏了就退回默认，不要把用户整份配置丢掉；只有
连 id 都没有的结构才跳过。写盘和 ``core/prefs.py`` 一样是「先 .tmp 再 os.replace」。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import paths
from .hotkeys import normalize, reserved_reason

SCHEMA_VERSION = 1
RULES_FILE = "rules.json"
DEFAULT_COOLDOWN_MS = 1000
MAX_COOLDOWN_MS = 600_000
MAX_DELAY_MS = 600_000
MAX_NAME_LEN = 60

CONDITION_MODES = ("all", "any")

#: type → (中文名, 允许的 op, 参数提示, 参数是不是数值)
CONDITION_TYPES: dict[str, tuple[str, tuple[str, ...], str, bool]] = {
    "process_running": ("某程序正在运行", ("is", "is_not"), "可执行文件名，如 notepad.exe", False),
    "process_elapsed": ("程序已运行", ("ge", "le"), "分钟数", True),
    "foreground": ("前台程序 / 窗口标题", ("contains", "equals"), "进程名或标题片段", False),
    "idle_minutes": ("电脑空闲", ("ge", "le"), "分钟数", True),
    "locked": ("电脑已锁屏", ("is", "is_not"), "是 / 否", False),
    "power": ("供电与电量", ("is", "ge", "le"), "ac / battery 或电量百分比", False),
}

#: op → 中文，界面下拉直接用这份，别在 UI 里另写一份
OPS: dict[str, str] = {
    "is": "是",
    "is_not": "不是",
    "ge": "≥",
    "le": "≤",
    "contains": "包含",
    "equals": "等于",
}

ACTION_TYPES = ("virtual_event", "delay")


def _text(value: Any, limit: int = MAX_NAME_LEN) -> str:
    return str(value).strip()[:limit] if value is not None else ""


def _integer(value: Any, default: int, low: int, high: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, out))


@dataclass(slots=True)
class Condition:
    """一条状态条件。比较符按类型限定（见 ``CONDITION_TYPES``）。

    ``arg`` 是主参数；``target`` 只给需要**两个**参数的条件用——目前只有
    「某程序已运行 N 分钟」要同时知道**程序名**和**分钟数**，界面上就是两个输入框。
    """

    type: str = "process_running"
    op: str = "is"
    arg: str = ""
    target: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "op": self.op, "arg": self.arg, "target": self.target}

    @classmethod
    def from_dict(cls, raw: Any) -> Condition | None:
        if not isinstance(raw, dict):
            return None
        kind = _text(raw.get("type"), 40)
        if kind not in CONDITION_TYPES:
            return None
        allowed = CONDITION_TYPES[kind][1]
        op = _text(raw.get("op"), 20)
        return cls(kind, op if op in allowed else allowed[0],
                   _text(raw.get("arg"), 200), _text(raw.get("target"), 200))


@dataclass(slots=True)
class Action:
    """一个动作。``virtual_event`` 用 ``name``，``delay`` 用 ``ms``。"""

    type: str = "virtual_event"
    name: str = ""
    ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        if self.type == "delay":
            return {"type": "delay", "ms": self.ms}
        return {"type": "virtual_event", "name": self.name}

    @classmethod
    def from_dict(cls, raw: Any) -> Action | None:
        if not isinstance(raw, dict):
            return None
        kind = _text(raw.get("type"), 40)
        if kind == "delay":
            return cls(kind, "", _integer(raw.get("ms"), 0, 0, MAX_DELAY_MS))
        if kind == "virtual_event":
            return cls(kind, _text(raw.get("name"), 120), 0)
        return None


@dataclass(slots=True)
class Rule:
    """一条完整规则。``conditions`` 为空 = 条件块不设限。

    ``deleted`` = 在回收站里。它**不是**启停：停用规则还会显示、还能改，删除的规则
    从列表和所有触发路径里消失。``deleted_at`` 是 epoch 秒，只给回收站显示时间用。
    """

    id: str
    name: str = ""
    enabled: bool = True
    keys: str = ""
    condition_mode: str = "all"
    conditions: list[Condition] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    cooldown_ms: int = DEFAULT_COOLDOWN_MS
    deleted: bool = False
    deleted_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        out = {
            "id": self.id,
            "name": self.name,
            "enabled": bool(self.enabled),
            "keys": self.keys,
            "condition_mode": self.condition_mode,
            "conditions": [c.to_dict() for c in self.conditions],
            "actions": [a.to_dict() for a in self.actions],
            "cooldown_ms": self.cooldown_ms,
        }
        # 正常规则不写这两个键，旧版 rules.json 读进来仍是原样
        if self.deleted:
            out["deleted"] = True
            out["deleted_at"] = self.deleted_at
        return out

    @classmethod
    def from_dict(cls, raw: Any) -> Rule | None:
        if not isinstance(raw, dict):
            return None
        rid = _text(raw.get("id"), 64)
        if not rid:
            return None
        mode = _text(raw.get("mode") or raw.get("condition_mode"), 10)
        conds = raw.get("conditions")
        acts = raw.get("actions")
        try:
            deleted_at = float(raw.get("deleted_at") or 0.0)
        except (TypeError, ValueError):
            deleted_at = 0.0
        return cls(
            id=rid,
            name=_text(raw.get("name")) or "未命名规则",
            enabled=raw.get("enabled") is not False,
            keys=normalize(raw.get("keys") or ""),
            condition_mode=mode if mode in CONDITION_MODES else "all",
            conditions=[c for item in conds if (c := Condition.from_dict(item))]
            if isinstance(conds, list) else [],
            actions=[a for item in acts if (a := Action.from_dict(item))]
            if isinstance(acts, list) else [],
            cooldown_ms=_integer(raw.get("cooldown_ms"), DEFAULT_COOLDOWN_MS,
                                 0, MAX_COOLDOWN_MS),
            deleted=raw.get("deleted") is True,
            deleted_at=deleted_at,
        )

    def clone(self) -> Rule:
        return Rule.from_dict(self.to_dict()) or Rule(id=str(uuid.uuid4())[:8])


def validate_rule(rule: Rule) -> list[str]:
    """返回中文错误列表；空列表 = 可以保存。界面保存前调一次。"""
    errors: list[str] = []
    if not rule.name.strip():
        errors.append("规则名称不能为空")
    keys = normalize(rule.keys)
    if not keys:
        errors.append("快捷键无效")
    elif reason := reserved_reason(keys):
        errors.append(f"快捷键不可用：{reason}")
    if rule.condition_mode not in CONDITION_MODES:
        errors.append("条件模式只能是「全部」或「任一」")
    for i, cond in enumerate(rule.conditions, 1):
        spec = CONDITION_TYPES.get(cond.type)
        if spec is None:
            errors.append(f"条件 {i} 的类型未知")
            continue
        if cond.op not in spec[1]:
            errors.append(f"条件 {i} 的比较符不适用")
        if not cond.arg.strip():
            errors.append(f"条件 {i} 的参数不能为空")
        elif spec[3]:
            try:
                float(cond.arg)
            except ValueError:
                errors.append(f"条件 {i} 的参数要填数字")
        elif cond.type == "locked" and cond.arg.strip().lower() not in ("true", "false", "是", "否"):
            errors.append("锁屏条件的参数要填「是」或「否」")
        elif cond.type == "power" and cond.op == "is" \
                and cond.arg.strip().lower() not in ("ac", "battery"):
            errors.append("供电条件的参数要填 ac 或 battery")
        if cond.type == "process_elapsed" and not cond.target.strip():
            errors.append(f"条件 {i} 要填程序名")
    if not rule.actions:
        errors.append("至少要有一个动作")
    for i, act in enumerate(rule.actions, 1):
        if act.type == "virtual_event" and not act.name.strip():
            errors.append(f"动作 {i} 没填虚拟事件名")
        elif act.type == "delay" and not (0 <= act.ms <= MAX_DELAY_MS):
            errors.append(f"动作 {i} 的延迟超出范围")
    if not any(a.type == "virtual_event" and a.name.strip() for a in rule.actions):
        errors.append("动作里至少要有一条「发虚拟事件」")
    if not (0 <= rule.cooldown_ms <= MAX_COOLDOWN_MS):
        errors.append("冷静期超出范围")
    return errors


class RuleSet:
    """规则集合 + 落盘。所有方法都在主线程调用。

    ``on_changed`` 是给常驻服务层的钩子：增删改之后要重新注册热键。挂在 RuleSet 上
    而不是对话框上，因为 ``exec_with_dim_click`` 关窗会销毁弹窗实例。
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else paths.data_dir() / RULES_FILE
        self.rules: list[Rule] = []
        self.warnings: list[str] = []
        self.on_changed: Callable[[], None] | None = None
        self.load()

    # -- 落盘 ------------------------------------------------------------
    def load(self) -> None:
        self.rules = []
        self.warnings = []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        items = data.get("rules")
        if not isinstance(items, list):
            return
        seen: set[str] = set()
        for raw in items:
            rule = Rule.from_dict(raw)
            if rule is None or rule.id in seen:
                self.warnings.append("跳过一条结构损坏或重复 id 的规则")
                continue
            seen.add(rule.id)
            self.rules.append(rule)
        if data.get("version") not in (None, SCHEMA_VERSION):
            self.warnings.append(f"规则文件版本 {data.get('version')} 不是 {SCHEMA_VERSION}，已按兼容方式读入")

    def save(self) -> bool:
        payload = {
            "version": SCHEMA_VERSION,
            "rules": [r.to_dict() for r in self.rules],
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
            return True
        except OSError:
            return False

    def _changed(self) -> bool:
        ok = self.save()
        if self.on_changed:
            self.on_changed()
        return ok

    # -- 查询 ------------------------------------------------------------
    def active(self) -> list[Rule]:
        """列表窗 / 热键注册 / 触发用的规则。**回收站里的不算**。"""
        return [r for r in self.rules if not r.deleted]

    def trashed(self) -> list[Rule]:
        """回收站里的规则，按删除时间倒序（最近删的在上面）。"""
        return sorted((r for r in self.rules if r.deleted),
                      key=lambda r: r.deleted_at, reverse=True)

    # -- 增删改 ----------------------------------------------------------
    def new_id(self) -> str:
        used = {r.id for r in self.rules}
        while True:
            rid = uuid.uuid4().hex[:8]
            if rid not in used:
                return rid

    def get(self, rule_id: str, *, include_deleted: bool = False) -> Rule | None:
        """默认只返回在用的规则；回收站 UI 才传 ``include_deleted=True``。

        ``AutomationEngine.test()`` 也走这里：删掉的规则不能被「测试发送」叫起来。
        """
        return next((r for r in self.rules
                     if r.id == rule_id and (include_deleted or not r.deleted)), None)

    def add(self, rule: Rule) -> bool:
        # 回收站里同 id 的规则也算占用，否则还原时会撞出两条
        if not rule.id or self.get(rule.id, include_deleted=True):
            return False
        self.rules.append(rule)
        return self._changed()

    def update(self, rule: Rule) -> bool:
        for i, old in enumerate(self.rules):
            if old.id == rule.id and not old.deleted:
                rule.deleted = False
                self.rules[i] = rule
                return self._changed()
        return False

    def remove(self, rule_id: str) -> bool:
        """硬删除（彻底从 ``rules.json`` 抹掉）。列表窗的「删除」请用 ``trash()``。"""
        kept = [r for r in self.rules if r.id != rule_id]
        if len(kept) == len(self.rules):
            return False
        self.rules = kept
        return self._changed()

    def trash(self, rule_id: str, *, at: float | None = None) -> bool:
        """软删除：打标记、落盘、立刻释放热键。**不做二次确认**（可还原）。"""
        rule = self.get(rule_id)
        if rule is None:
            return False
        rule.deleted = True
        rule.deleted_at = time.time() if at is None else at
        return self._changed()

    def restore(self, rule_id: str) -> bool:
        rule = self.get(rule_id, include_deleted=True)
        if rule is None or not rule.deleted:
            return False
        rule.deleted = False
        rule.deleted_at = 0.0
        return self._changed()

    def purge(self, rule_id: str, *, only_deleted: bool = True) -> bool:
        """彻底删除。默认只允许删回收站里的，避免误把在用的规则抹掉。"""
        rule = self.get(rule_id, include_deleted=True)
        if rule is None or (only_deleted and not rule.deleted):
            return False
        return self.remove(rule_id)

    def set_enabled(self, rule_id: str, on: bool) -> bool:
        rule = self.get(rule_id)
        if rule is None or rule.enabled == on:
            return False
        rule.enabled = on
        return self._changed()

    # -- 热键注册用 ------------------------------------------------------
    def enabled_keys(self) -> list[str]:
        """启用规则里出现过的**去重**组合；顺序稳定，便于分配不变的 id。"""
        out: list[str] = []
        for rule in self.active():
            if not rule.enabled:
                continue
            keys = normalize(rule.keys)
            if keys and keys not in out:
                out.append(keys)
        return out

    def rules_for(self, keys: str) -> list[Rule]:
        want = normalize(keys)
        return [r for r in self.active()
                if r.enabled and normalize(r.keys) == want]


__all__ = [
    "ACTION_TYPES",
    "CONDITION_MODES",
    "CONDITION_TYPES",
    "DEFAULT_COOLDOWN_MS",
    "MAX_COOLDOWN_MS",
    "MAX_DELAY_MS",
    "MAX_NAME_LEN",
    "OPS",
    "RULES_FILE",
    "SCHEMA_VERSION",
    "Action",
    "Condition",
    "Rule",
    "RuleSet",
    "validate_rule",
]
