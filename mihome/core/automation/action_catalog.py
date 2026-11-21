# SPDX-License-Identifier: GPL-3.0-or-later
"""设备动作目录：**这台设备能做什么、每件事要填什么**。

编辑页里一行动作 = 一台设备 + 一个功能 + 该功能的参数。功能不是"写死的三种动作
类型"，而是从设备规格里按**白名单**挑出来的一小组 action：

===================  ==========  ============================  ==========
功能（``action``）    界面标签    入参（spec 原名）              可用设备
===================  ==========  ============================  ==========
``emit-virtual-event``  发送虚拟事件  ``virtual-param`` → 事件名      中枢网关
``wake-up``          唤醒        无                            音箱
``play-text``        播报文字     ``text-content`` → 文字         音箱
``execute-text-directive``  执行文本指令  ``text-content`` → 文字、``silent-execution`` → 静默  音箱
===================  ==========  ============================  ==========

**为什么必须白名单**：一台小爱音箱的规格里有 11 个 action（``play`` / ``pause`` /
``next`` / ``play-radio`` / ``stop-alarm`` / ``tv-switchon`` …）。全列出来比从前
"四档类型下拉"还乱，而且大部分跟自动化无关。要放开时在 ``FEATURES`` 里加一行，
别把过滤逻辑散到 UI 里。

**中文名以本地表为准，不用云端 ``comment``**：``xiaomi-oh2p`` 上 ``play-text`` 的
comment 是「播放文本」，``xiaomi-lx06`` 上干脆没有——同一件事在两台设备上显示两个
名字比没有名字更糟。云端名字只作为"这台设备到底有没有这个能力"的判据（有没有这个
action），显示名一律走 ``FEATURES``。

**只支持 text / bool 两种参数**。别的格式（``uint8`` / ``enum`` / ``float``）一期的
设备直接不进列表：宁可"选不到"，也不要造一个填什么都会错值的输入框。

本模块不 import Qt，也不发请求：``scripts/verify_action_catalog.py`` 全离线验。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .. import miot_action, speaker

#: 事件名的长度上限。和规则名一致（``rules.MAX_NAME_LEN``），别两处各写一个数。
MAX_EVENT_CHARS = 60


@dataclass(frozen=True, slots=True)
class Param:
    """一个入参的元数据。``kind`` 只有 ``text`` / ``bool`` 两种（见模块 docstring）。"""

    name: str
    label: str
    kind: str = "text"
    required: bool = True
    #: 文本类才用得上：超过就截断（0 = 不限）
    max_chars: int = 0
    #: 文本类是否按音箱的字符白名单清洗。事件名不清洗——它是用户自定义串，
    #: 可能带下划线和括号，被白名单剔掉会静默改掉事件名。
    whitelist: bool = False
    default: Any = None


@dataclass(frozen=True, slots=True)
class Feature:
    """一个可用的功能。``service`` 是查 action 时的服务名提示（固件改名有退路）。

    ``refreshes`` 决定**执行完之后要不要做一轮全量回读**（界面那串 1 / 2.5 / 4 秒）：

    * ``execute-text-directive`` 要——"打开客厅灯"这类指令的效果落在**别的设备**上，
      只能靠全量读看到结果，没有别的办法知道它到底做了什么；
    * 唤醒 / 播报文字 / 发送虚拟事件都不要——它们不改变任何可读属性，回读只是把
      同一批属性白读三遍，还占着串行队列给后面的动作添堵。

    以后加"设置音量"这类真会改属性的动作，把它设成 ``True`` 就行，执行层不用改。
    """

    name: str
    label: str
    service: str
    params: tuple[Param, ...] = ()
    refreshes: bool = False


@dataclass(frozen=True, slots=True)
class ActionDevice:
    """一台能执行动作的设备。``features`` 只含**这台设备真的能用**的那几个。"""

    did: str
    name: str
    room: str = ""
    model: str = ""
    online: bool = True
    features: tuple[Feature, ...] = ()

    @property
    def label(self) -> str:
        return self.name or self.model or self.did


#: 白名单。加功能就在这里加一行，并在 ``docs/dev/automation.md``「设备动作」一节补说明。
FEATURES: tuple[Feature, ...] = (
    Feature(
        "emit-virtual-event", "发送虚拟事件", "virtual-service",
        (Param("virtual-param", "事件名", "text", max_chars=MAX_EVENT_CHARS),),
    ),
    #: 唤醒会让音箱进入**聆听态**：之后收到的指令（尤其是发给它的）要等聆听结束才执行。
    #: 11-14 实测确认——这不是本程序排队，是设备 / 云端侧的顺序，换哪条收发通道都一样。
    #: 编排上需要唤醒时，尽量把它放到动作串的**最后一步**，否则后面的动作看着像"卡了"。
    Feature("wake-up", "唤醒", "intelligent-speaker", ()),
    Feature(
        "play-text", "播报文字", "intelligent-speaker",
        (Param("text-content", "文字", "text",
               max_chars=speaker.MAX_CHARS, whitelist=True),),
    ),
    Feature(
        "execute-text-directive", "执行文本指令", "intelligent-speaker",
        (
            Param("text-content", "文字", "text",
                  max_chars=speaker.MAX_CHARS, whitelist=True),
            Param("silent-execution", "静默", "bool", required=False, default=False),
        ),
        # 指令的效果可能落在别的设备上（"打开客厅灯"），只有全量回读才看得到
        refreshes=True,
    ),
)

_BY_NAME: dict[str, Feature] = {f.name: f for f in FEATURES}


def feature(name: str) -> Feature | None:
    """按 action 名取功能的元数据；不在白名单里返回 ``None``。"""
    return _BY_NAME.get(name)


def action_for(spec: Any, feat: Feature) -> Any:
    """一台设备的规格里，这个功能的 action；用不了返回 ``None``。

    两个方向都要对得上才叫能用：

    * 设备**少**了必填入参 → 发了会被云端拒（"参数个数对不上"，见踩坑清单
      ``-704220025``），不显示；
    * 设备**多**了入参 → 说明这个功能在我们不认识的固件上长胖了，猜着填会填错槽，
      也不显示。

    第二条是**刻意保守**的：将来某台固件给 ``play-text`` 加了音量参数，这个功能会从
    它的列表里消失，而不是发一条参数错位的请求。宁可少一个功能，也不要"看着能用、
    发出去报参数个数不对"。
    """
    if feat is None:
        return None
    act = miot_action.find_action(spec, feat.name, feat.service)
    if act is None:
        return None
    names = miot_action.arg_names(spec, act)
    # 有认不出的槽（piid 在服务里找不到对应属性）：形状不清楚，不猜
    if any(not name for name in names):
        return None
    # **一个都不能少**，包括可选的：少一个槽发出去就是"参数个数对不上"。
    # required 只决定"值能不能为空"，不决定"这台设备有没有这个槽"。
    if not {p.name for p in feat.params}.issubset(names):
        return None
    # 多出来的入参：这个功能在我们不认识的固件上长胖了，参数会错位
    if set(names) - {p.name for p in feat.params}:
        return None
    return act


def features_for(spec: Any) -> tuple[Feature, ...]:
    """这台设备可用的功能，按 ``FEATURES`` 的顺序。"""
    return tuple(f for f in FEATURES if action_for(spec, f) is not None)


def collect(states: Iterable[Any], home: str = "") -> list[ActionDevice]:
    """当前家庭里能执行动作的设备（中枢 + 音箱），在线的排前面。

    ``home`` 收家庭 **id**（与规则的 ``home``、``Store.active_home`` 同一口径），
    比的是 ``node.home_id``——**不是** ``node.home``（那是显示名，改个家庭名就全对
    不上了）。``home`` 为空不过滤；没挂家庭的设备（云端没给 home 字段）算作任何家庭
    都可见，和顶栏指令条的约定一致。
    """
    out: list[ActionDevice] = []
    for st in states:
        owner = str(getattr(getattr(st, "node", None), "home_id", "") or "")
        if home and owner and owner != home:
            continue
        spec = getattr(st, "spec", None)
        feats = features_for(spec)
        if not feats:
            continue
        node = getattr(st, "node", None)
        did = str(getattr(st, "did", "") or getattr(node, "did", "") or "")
        if not did:
            continue
        out.append(ActionDevice(
            did=did,
            name=str(getattr(st, "name", "") or getattr(node, "name", "") or ""),
            room=str(getattr(st, "room", "") or ""),
            model=str(getattr(node, "model", "") or ""),
            online=bool(getattr(st, "online", True)),
            features=feats,
        ))
    out.sort(key=lambda d: (not d.online, d.room, d.name))
    return out


def clean_values(feat: Feature, raw: Mapping[str, Any], *,
                 truncate: bool = True) -> tuple[dict[str, Any], str]:
    """把界面 / 规则里存的原始值收成"能直接发"的一份。返回 ``(值, 出错原因)``。

    这里是**唯一**做参数清洗的地方：界面校验、存盘校验、执行前清洗都走它，三处各写
    一遍迟早出现"界面能存、执行被拒"。``原因`` 为空串表示这一份能用。

    ``truncate=False`` 用于**存盘前校验**：超过上限就报错，而不是悄悄截短——用户填了
    一段话，保存后少了半句，比直接说"太长了"糟得多。执行前清洗保留截断（历史上
    顶栏指令条就是这个行为，那里有 toast 明说截断）。
    """
    out: dict[str, Any] = {}
    for p in feat.params:
        value = raw.get(p.name, p.default)
        if p.kind == "bool":
            out[p.name] = bool(value)
            continue
        text = "" if value is None else str(value).strip()
        too_long = False
        if p.whitelist:
            cleaned = speaker.clean(text)
            text, too_long = cleaned.text, cleaned.truncated
        # ``speaker.clean`` 自己也会截到 200；走白名单的文本参数得靠它报"超了"，
        # 否则 ``truncate=False``（存盘校验）会变成"悄悄截短"——正是这里要避免的。
        if (too_long or (p.max_chars and len(text) > p.max_chars)):
            if not truncate:
                return out, f"「{p.label}」最多 {p.max_chars} 个字符"
            text = text[: p.max_chars].rstrip() if p.max_chars else text
        if p.required and not text:
            return out, f"「{p.label}」不能为空"
        out[p.name] = text
    return out, ""


def payload_for(feat: Feature, act: Any, spec: Any,
                values: Mapping[str, Any]) -> list[Any]:
    """按设备**实际的**入参名拼 ``in``。名字对不上时退回白名单表里的顺序。"""
    names = miot_action.arg_names(spec, act)
    fallback = [values.get(p.name, p.default) for p in feat.params]
    return miot_action.payload(names, values, fallback)


__all__ = [
    "FEATURES",
    "MAX_EVENT_CHARS",
    "ActionDevice",
    "Feature",
    "Param",
    "action_for",
    "clean_values",
    "collect",
    "feature",
    "features_for",
    "payload_for",
]
