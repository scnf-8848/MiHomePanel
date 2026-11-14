# SPDX-License-Identifier: GPL-3.0-or-later
"""规则的结构、校验与 ``rules.json`` 原子读写。

**这是规则的唯一真相。** 键位、条件、动作、冷静期全在每条规则里，不存在单独的
``hotkeys.json`` 或 ``prefs.json`` 副本（Windows 注册热键不需要任何文件，见
``docs/reference/automation-hotkeys-and-ui.md`` 第 2 节）。

形状（v4，对应米家 App「创建自动化」页）：

    {id, name, enabled, triggers[], condition_mode, conditions[], actions[], cooldown_ms[, else_actions][, home]}
    triggers[]   = {type:"keyboard", keys}   至少一条；**任一触发命中即执行**
    conditions[] = {type, op, arg}        空数组 = 无条件
    actions[]    = {type:"device_action", did, device, action, args{入参名: 值}}
                 | {type:"delay", ms[, unit:"s"]}
    else_actions[] = 同 actions[]，**只在条件不成立时执行**；空数组不写键

**设备动作**（2026-11-14 重构）：一条动作 = 一台设备 + 一个功能 + 该功能的参数。
``action`` 存的是 spec 里的 action 名（``wake-up`` / ``play-text`` /
``execute-text-directive`` / ``emit-virtual-event``），``args`` 按**入参名**存
（``text-content`` / ``silent-execution`` / ``virtual-param``）。白名单与每项的
参数元数据在 ``action_catalog``。

**绝不存 siid/aiid**：同一份能力在型号之间方法号完全不同（``wake-up`` 在 lx06 是
aiid 3、在 oh2p 是 1），固件升级也可能变；存名字、执行时按名字查坐标才是稳的
（见 ``core/miot_action``）。

**④ 否则分支**（用户 11-14）：条件不成立时执行 ``else_actions``，动作执行失败**不**走
这里（失败是故障，走系统通知 + 日志，见 ``ui/main_window`` 的失败提醒）。没有条件时
``else_actions`` 必须为空——"条件不成立"这个前提不存在，存了也永远不会触发，
``validate_rule`` 会挡住那种状态。

**多触发**（用户 11-13 第十条）：一条规则可以挂多个触发，语义是 **OR**——任何一条
命中都执行这条规则，条件 / 冷静期 / 动作队列与单触发完全一样。目前只实现
``keyboard`` 一种触发类型，``triggers[]`` 的 ``type`` 先占住位置，以后加别的
触发源（进程启动、网络变化）不用再改 schema。

**只认当前结构**：本机 ``rules.json`` 已规范成 v4。**旧的 ``virtual_event`` /
``speaker`` 动作读不回来**（``Action.from_dict`` 遇到不认识的 type 返回 ``None``，
那一条动作被丢弃，规则本身还在），不再做转换——用户 11-14 确认本机只有一条测试
规则，不值得为它养一套兼容分支。迁移工具见 ``scripts/migrate_actions_v4.py``。

**规则按家庭隔离**（用户 11-13）：``home`` 记规则归属的云端**家庭 id**，列表窗 /
回收站 / 日志的规则筛选只显示**当前家庭**的那几条，免得在 1103 家庭里看到共享家庭
建的规则。三条约定：

* ``home`` 为**空串 = 未指定家庭**（规则文件从别处拷来、或早先没选家庭就保存的）。
  这种规则在**任何**家庭都可见，方便用户找到并重新选一次；但它**不注册热键、
  不参与触发**——不能静默按某个家庭执行。空归属只在编辑窗里由用户显式落定，
  加载时**不再按文件版本清空**（本机数据已全是家庭 id）。
* **热键与引擎不做家庭过滤**：热键是这台电脑的系统资源，被自动化的也是这台电脑，
  切到别的家庭去看设备不该让自己的快捷键失灵。家庭只决定"在哪个页面能看到和改"，
  以及执行时这条规则到底算不算数（空归属不算）。
* 执行时按 ``did`` 全家庭找音箱（见 ``service._say``），所以指向别的家庭音箱的规则
  只要那台设备还在，照样能跑。

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
#: 文本类参数的上限现在由 ``action_catalog`` 的 ``Param.max_chars`` 决定；这里只重导出
#: 给界面文案用（"最多 200 个字符"），别在 UI 里再写死一个 200。
from ..speaker import MAX_CHARS as MAX_SPEAK_CHARS
from . import action_catalog
from .hotkeys import normalize, reserved_reason

SCHEMA_VERSION = 3
RULES_FILE = "rules.json"
DEFAULT_COOLDOWN_MS = 1000
MAX_COOLDOWN_MS = 600_000
MAX_DELAY_MS = 600_000
MAX_NAME_LEN = 60

CONDITION_MODES = ("all", "any")

#: 触发类型 → 中文名。目前只有键盘快捷键；多触发行是 OR（任一命中即执行）。
TRIGGER_TYPES: dict[str, str] = {"keyboard": "键盘快捷键"}

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

#: 落盘的动作类型。**延迟只有一种**，界面上的「毫秒 / 秒」是 ``Action.unit``
#: 那把尺子（两种规格存的是同一个 ``ms``），所以这里不该出现 ``delay_s``。
#: ``device_action`` 的"功能"不在这里列举——那是 ``action_catalog.FEATURES``
#: 的白名单，加一个功能不该改这个常量。
ACTION_TYPES = ("device_action", "delay")


def _text(value: Any, limit: int = MAX_NAME_LEN) -> str:
    return str(value).strip()[:limit] if value is not None else ""


def _integer(value: Any, default: int, low: int, high: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, out))


def _in_home(rule: Rule, home: str | None) -> bool:
    """这条规则属不属于 ``home`` 这个家庭。

    ``home is None`` = 不按家庭过滤（列表 / 回收站 / 日志传当前家庭 id）。
    **空归属的规则在任何家庭都算**——那是 v1 老数据或用户没选家庭，藏起来会让人
    以为规则丢了；但执行路径（``enabled_keys`` / ``rules_for``）会显式跳过它，
    可见 ≠ 会运行。
    """
    return home is None or not rule.home or rule.home == home


@dataclass(slots=True)
class Trigger:
    """一条触发方式。目前只有 ``keyboard``，``type`` 先留着给以后的触发源。

    ``type`` 为空串 = 编辑页那档空档「-」（还没选），不是"写错了"：这种行会被
    ``validate_rule`` 拦下，不会落盘。``keys`` 在构造时统一 ``normalize``，
    所以 ``Trigger("keyboard", "ctrl+alt+f7")`` 存下来也是规范串。
    """

    type: str = "keyboard"
    keys: str = ""

    def __post_init__(self) -> None:
        self.keys = normalize(self.keys)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "keys": self.keys}

    @classmethod
    def from_dict(cls, raw: Any) -> Trigger | None:
        if not isinstance(raw, dict):
            return None
        kind = _text(raw.get("type"), 20)
        # 未知类型多半是未来版本写的：这一条留不住，但别拖坏整条规则
        if kind and kind not in TRIGGER_TYPES:
            return None
        return cls(kind, _text(raw.get("keys"), 40))


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
    """一个动作：**对某台设备做一件事**，或者等一会。

    * ``device_action`` —— ``did``（设备 id）+ ``device``（显示名快照，只给日志和
      界面回显；设备改名 / 移除后执行仍按 ``did`` 走）+ ``action``（spec 里的 action
      名）+ ``args``（入参名 → 值）。
    * ``delay`` —— ``ms``。

    **不存 siid/aiid**（见模块 docstring）：执行时按 ``action`` 名字现查。

    ``unit`` 只是 ``delay`` 的**输入刻度**，语义永远由 ``ms`` 决定：校验、存盘、引擎
    执行全按毫秒算。留着它是为了重开编辑页时「秒」那一档仍显示 ``30`` 而不是
    ``30000``——用户 11-13 要的是两种录入规格，不是两套数据。

    ``args`` 的值只有 ``str`` 与 ``bool`` 两种（``action_catalog`` 的 ``Param.kind``
    就这两档）。不在白名单里的键在存盘前已被 ``from_dict`` 过滤掉，不会带进文件。
    """

    type: str = "device_action"
    did: str = ""
    device: str = ""
    action: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    ms: int = 0
    unit: str = "ms"

    @property
    def feature(self) -> action_catalog.Feature | None:
        """这个动作的功能元数据；未知功能返回 ``None``。"""
        return action_catalog.feature(self.action)

    def to_dict(self) -> dict[str, Any]:
        if self.type == "delay":
            out: dict[str, Any] = {"type": "delay", "ms": self.ms}
            # 毫秒是默认刻度，不写这个键
            if self.unit == "s":
                out["unit"] = "s"
            return out
        out = {
            "type": "device_action",
            "did": self.did,
            "device": self.device,
            "action": self.action,
        }
        # 无参功能（唤醒）不写空字典，保持文件干净
        if self.args:
            out["args"] = dict(self.args)
        return out

    @classmethod
    def from_dict(cls, raw: Any) -> Action | None:
        if not isinstance(raw, dict):
            return None
        kind = _text(raw.get("type"), 40)
        if kind == "delay":
            unit = "s" if _text(raw.get("unit"), 4) == "s" else "ms"
            return cls(kind, ms=_integer(raw.get("ms"), 0, 0, MAX_DELAY_MS), unit=unit)
        if kind != "device_action":
            # 旧形状（virtual_event / speaker）与未知类型：这一条动作留不住。
            # 规则本身还在，用户重选一次即可——见模块 docstring「只认当前结构」。
            return None
        raws = raw.get("args")
        args: dict[str, Any] = {}
        if isinstance(raws, dict):
            for key, value in raws.items():
                name = _text(key, 60)
                if not name:
                    continue
                # bool 要保持 bool：写盘再读回来还是 True，不能变成 "True"
                args[name] = value if isinstance(value, bool) else _text(value, 400)
        return cls(
            type=kind,
            did=_text(raw.get("did"), 64),
            device=_text(raw.get("device"), 120),
            action=_text(raw.get("action"), 60),
            args=args,
        )


@dataclass(slots=True)
class Rule:
    """一条完整规则。``conditions`` 为空 = 条件块不设限。

    ``triggers`` 是**触发列表**：任意一条命中就执行这条规则（OR），互不排斥。
    以前的单个 ``keys`` 只是它的一种特例；``keys`` 现在是个只读属性，回第一条
    键盘触发的键位，给日志 / 旧卡片文案兜底。

    ``deleted`` = 在回收站里。它**不是**启停：停用规则还会显示、还能改，删除的规则
    从列表和所有触发路径里消失。``deleted_at`` 是 epoch 秒，只给回收站显示时间用。

    ``home`` = 这条规则归属的**家庭 id**（见模块 docstring）。空串表示未指定家庭，
    这种规则在所有家庭都可见、可以编辑，但**不会注册热键、不会执行**。
    """

    id: str
    name: str = ""
    enabled: bool = True
    triggers: list[Trigger] = field(default_factory=list)
    condition_mode: str = "all"
    conditions: list[Condition] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    #: 第 ④ 段「否则执行动作」。**只在条件不成立时跑**；空列表 = 什么都不做。
    #: 条件为空的规则不允许有它（见 :func:`validate_rule`）。
    else_actions: list[Action] = field(default_factory=list)
    cooldown_ms: int = DEFAULT_COOLDOWN_MS
    deleted: bool = False
    deleted_at: float = 0.0
    home: str = ""

    @property
    def keys(self) -> str:
        """第一条有键位的键盘触发；多触发请直接读 ``triggers``。

        日志 / 回收站 / 旧调用方还按"一个规则一个键位"用这个属性，语义等价于
        "这条规则最显眼的那个触发"。
        """
        return next((t.keys for t in self.triggers
                     if t.type == "keyboard" and t.keys), "")

    def to_dict(self) -> dict[str, Any]:
        out = {
            "id": self.id,
            "name": self.name,
            "enabled": bool(self.enabled),
            "triggers": [t.to_dict() for t in self.triggers],
            "condition_mode": self.condition_mode,
            "conditions": [c.to_dict() for c in self.conditions],
            "actions": [a.to_dict() for a in self.actions],
            "cooldown_ms": self.cooldown_ms,
        }
        # 正常规则不写这两个键，旧版 rules.json 读进来仍是原样
        if self.deleted:
            out["deleted"] = True
            out["deleted_at"] = self.deleted_at
        # 没归属（升级前的老数据）不写键更干净，读回来仍是空串
        if self.home:
            out["home"] = self.home
        # 否则分支为空不写键：老版本读到这个键会当未知字段忽略，但保持文件干净
        if self.else_actions:
            out["else_actions"] = [a.to_dict() for a in self.else_actions]
        return out

    @classmethod
    def from_dict(cls, raw: Any) -> Rule | None:
        if not isinstance(raw, dict):
            return None
        rid = _text(raw.get("id"), 64)
        if not rid:
            return None
        mode = _text(raw.get("condition_mode"), 10)
        conds = raw.get("conditions")
        acts = raw.get("actions")
        els = raw.get("else_actions")
        raws = raw.get("triggers")
        triggers = ([t for item in raws if (t := Trigger.from_dict(item))]
                    if isinstance(raws, list) else [])
        try:
            deleted_at = float(raw.get("deleted_at") or 0.0)
        except (TypeError, ValueError):
            deleted_at = 0.0
        return cls(
            id=rid,
            name=_text(raw.get("name")) or "未命名规则",
            enabled=raw.get("enabled") is not False,
            triggers=triggers,
            condition_mode=mode if mode in CONDITION_MODES else "all",
            conditions=[c for item in conds if (c := Condition.from_dict(item))]
            if isinstance(conds, list) else [],
            actions=[a for item in acts if (a := Action.from_dict(item))]
            if isinstance(acts, list) else [],
            else_actions=[a for item in els if (a := Action.from_dict(item))]
            if isinstance(els, list) else [],
            cooldown_ms=_integer(raw.get("cooldown_ms"), DEFAULT_COOLDOWN_MS,
                                 0, MAX_COOLDOWN_MS),
            deleted=raw.get("deleted") is True,
            deleted_at=deleted_at,
            home=_text(raw.get("home"), 80),
        )

    def clone(self) -> Rule:
        return Rule.from_dict(self.to_dict()) or Rule(id=str(uuid.uuid4())[:8])


def _action_errors(actions: list[Action], label: str) -> list[str]:
    """一串动作的合法性。``label`` 只改文案前缀（「动作」/「否则动作」）。

    ③ 与 ④ 是同一套动作、同一套填法，这里写一份给两处用——各写一遍必然会有一天
    只改了一边，界面上就会出现在 ③ 报错、在 ④ 放过的情况。

    **只校验形状**（选了设备没有、功能认不认识、参数填没填、长度超没超），**不校验
    这台设备到底支不支持这个功能**：那要读 spec，而校验是纯函数。设备换了 / 固件变了
    由执行层报错（日志里写"这台音箱没有该功能"），编辑页不拦。
    """
    errors: list[str] = []
    for i, act in enumerate(actions, 1):
        if act.type not in ACTION_TYPES:
            # 编辑页的动作行默认停在「-」上（11-13 用户第八条）。这一层必须拦，
            # 否则 ``Action.to_dict`` 会把空类型当成 device_action 写进 rules.json，
            # 而 ``Action.from_dict`` 又读不回它——存进去就变成了另一条动作。
            errors.append(f"{label} {i} 还没选动作类型")
            continue
        if act.type == "delay":
            # 界面上延迟有两种录入刻度，上限只写毫秒的话「秒」档用户得自己换算
            if not (0 <= act.ms <= MAX_DELAY_MS):
                errors.append(f"{label} {i} 的延迟要在 0–{MAX_DELAY_MS} 毫秒"
                              f"（{MAX_DELAY_MS // 1000} 秒）之间")
            continue
        if not act.did.strip():
            errors.append(f"{label} {i} 没选设备")
            continue
        feat = action_catalog.feature(act.action)
        if feat is None:
            errors.append(f"{label} {i} 的功能未知（{act.action or '空'}）")
            continue
        _, why = action_catalog.clean_values(feat, act.args, truncate=False)
        if why:
            errors.append(f"{label} {i} {why}")
    return errors


def validate_rule(rule: Rule) -> list[str]:
    """返回中文错误列表；空列表 = 可以保存。界面保存前调一次。"""
    errors: list[str] = []
    if not rule.name.strip():
        errors.append("规则名称不能为空")
    if not rule.triggers:
        errors.append("至少要有一个触发方式")
    seen_keys: dict[str, int] = {}
    for i, trig in enumerate(rule.triggers, 1):
        if not trig.type:
            # 编辑页那档空档「-」，和条件 / 动作的空档同一个说法
            errors.append(f"触发 {i} 还没选触发方式")
            continue
        if trig.type not in TRIGGER_TYPES:
            errors.append(f"触发 {i} 的触发方式未知")
            continue
        keys = trig.keys
        if not keys:
            errors.append(f"触发 {i} 的快捷键无效")
            continue
        if reason := reserved_reason(keys):
            errors.append(f"触发 {i} 的快捷键不可用：{reason}")
        if keys in seen_keys:
            errors.append(f"触发 {i} 与触发 {seen_keys[keys]} 用了同一个快捷键")
        else:
            seen_keys[keys] = i
    if rule.condition_mode not in CONDITION_MODES:
        errors.append("条件模式只能是「全部」或「任一」")
    for i, cond in enumerate(rule.conditions, 1):
        spec = CONDITION_TYPES.get(cond.type)
        if spec is None:
            # 空串 = 编辑页那档「-」（还没选），不是"写错了"：两种说法别混，
            # 否则用户在空行上看到"类型未知"会以为自己填错了什么。
            errors.append(f"条件 {i} 还没选条件类型" if not cond.type
                          else f"条件 {i} 的类型未知")
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
    errors.extend(_action_errors(rule.actions, "动作"))
    # ④ 的前提是「有②」：没有条件就谈不上"条件不成立"，这段动作永远不会跑。
    # 编辑页按用户要求**不清空**已有动作（可能只是在重排条件），所以只能在这里拦，
    # 并且把两条出路都写出来，别让用户自己猜。
    if rule.else_actions and not rule.conditions:
        errors.append("没有状态条件时「否则执行动作」永远不会触发："
                      "要么给它加一条条件，要么清空这一段")
    errors.extend(_action_errors(rule.else_actions, "否则动作"))
    # 「至少有一条真会干事的动作」——延迟不算，它只是等。以前这条只认虚拟事件，
    # 加了音箱指令之后一条「按键 → 让音箱说句话」的规则完全成立，所以按"是不是设备
    # 动作"判，而不是按具体功能判。
    if not any(a.type == "device_action" and a.did.strip() and a.action
               for a in rule.actions):
        errors.append("动作里至少要有一条设备动作（延迟只是等待，不算）")
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
        # 本机 rules.json 已规范成 v3；版本不符只提示，不做字段转换（旧 keys /
        # 家庭名归属 / mode 的分支已删除）。
        version = data.get("version")
        seen: set[str] = set()
        for raw in items:
            rule = Rule.from_dict(raw)
            if rule is None or rule.id in seen:
                self.warnings.append("跳过一条结构损坏或重复 id 的规则")
                continue
            seen.add(rule.id)
            self.rules.append(rule)
        if version != SCHEMA_VERSION:
            self.warnings.append(
                f"规则文件版本 {version} 不是 {SCHEMA_VERSION}，已按当前结构读入")

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
    def active(self, home: str | None = None) -> list[Rule]:
        """列表窗用的规则。**回收站里的不算**。

        ``home`` 传**家庭 id** 就只留那个家庭的（列表 / 回收站 / 日志筛选都这么调）；
        传 ``None`` = 不按家庭过滤（引擎内部枚举走这条）。空归属规则照样会返回，
        但执行路径会再挡一次。
        """
        return [r for r in self.rules if not r.deleted and _in_home(r, home)]

    def trashed(self, home: str | None = None) -> list[Rule]:
        """回收站里的规则，按删除时间倒序（最近删的在上面）。"""
        return sorted((r for r in self.rules if r.deleted and _in_home(r, home)),
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
        """启用规则里出现过的**去重**组合；顺序稳定，便于分配不变的 id。

        一条规则可以有多条触发：这里把它们**摊平**再跨规则去重——同一个组合在
        几条规则 / 几个触发行里出现，系统层都只注册一次。

        **未指定家庭的规则不注册**：它没有明确的归属，不能静默按某个家庭执行。
        """
        out: list[str] = []
        for rule in self.active():
            if not rule.enabled or not rule.home:
                continue
            for trig in rule.triggers:
                if trig.type != "keyboard" or not trig.keys:
                    continue
                if trig.keys not in out:
                    out.append(trig.keys)
        return out

    def rules_for(self, keys: str) -> list[Rule]:
        """按热键找可执行的规则。**任一触发命中即算**，未指定家庭的不返回。"""
        want = normalize(keys)
        return [r for r in self.active()
                if r.enabled and r.home and any(
                    t.type == "keyboard" and t.keys == want for t in r.triggers)]


__all__ = [
    "ACTION_TYPES",
    "CONDITION_MODES",
    "CONDITION_TYPES",
    "DEFAULT_COOLDOWN_MS",
    "MAX_COOLDOWN_MS",
    "MAX_DELAY_MS",
    "MAX_NAME_LEN",
    "MAX_SPEAK_CHARS",
    "OPS",
    "RULES_FILE",
    "SCHEMA_VERSION",
    "TRIGGER_TYPES",
    "Action",
    "Condition",
    "Rule",
    "RuleSet",
    "Trigger",
    "validate_rule",
]
