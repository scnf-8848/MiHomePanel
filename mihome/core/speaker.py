# SPDX-License-Identifier: GPL-3.0-or-later
"""小爱音箱「执行文本指令」：能力探测、入参拼装、文本清洗。

为什么单独一层
--------------
同一份固件能力，**方法号在型号之间完全不同**：`wake-up` 在 ``xiaomi-lx06`` 上是
``aiid 3``，在 ``xiaomi-oh2p`` / ``xiaomi-x6a`` 上是 ``aiid 1``；
``execute-text-directive`` 是 5 vs 4。所以一条 iid 都不许写死，只能按**名字**在
``DeviceSpec`` 里查。这一层把"按名字查"收敛成一个函数，界面（顶栏指令条）和将来
的快捷键入口都调它，不各写一遍。

三个容易踩的点（都在本机规格缓存上核对过）：

1. ``text-content``(piid 1) 与 ``silent-execution``(piid 2) 的 ``access`` 是**空数组**
   —— 它们只作为 action 入参存在，不是可读可写属性。所以能力探测不能走
   ``spec.writeable()`` / ``prop_names()``，只能看 ``ActionSpec.in_piids``
   再回服务里按 piid 取名字（见 :func:`_arg_names`）。
2. ``in_piids`` 的顺序就是**实参顺序**，但别假设它一定是 ``(1, 2)``：按 piid 映射
   到名字再拼 ``in``，换型号也不会错位。
3. 服务名在三个型号上都叫 ``intelligent-speaker``（URN 段），但万一遇到改名的固件，
   退一步全服务搜同名 action（见 :func:`_find_action`）。

本模块不 import Qt，也不碰网络：能离线用假 spec 验（``scripts/verify_speaker.py``）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from . import miot_action

#: 音箱服务的 URN 名（``urn:miot-spec-v2:service:...:intelligent-speaker:...`` 的第 4 段）
SERVICE = "intelligent-speaker"
#: 执行文本指令（米家 App 里叫「执行文本指令」）
TEXT_ACTION = "execute-text-directive"
#: 唤醒（让音箱进入聆听，无参）
WAKE_ACTION = "wake-up"
#: 入参名，用来按 piid 拼实参
ARG_TEXT = "text-content"
ARG_SILENT = "silent-execution"

#: 米家 App「自定义指令」页写的是 ``0/200``。按**字符**算（不是一个 UTF-16 单元），
#: 所以一个中文算 1；emoji 本来就会被白名单剔除，不存在半个代理对的问题。
MAX_CHARS = 200

#: 除中英文与数字、空格之外，唯一放行的标点：中文逗号、句号、问号、感叹号
#: （``，`` U+FF0C / ``。`` U+3002 / ``？`` U+FF1F / ``！`` U+FF01），与米家文案一致。
_PUNCT = frozenset("\u3002\uff0c\uff1f\uff01")
#: 空白统一收敛成半角空格：粘贴来的换行/制表符既打不出来也不该发出去
_SPACES = frozenset(" \t\r\n\u00a0\u3000")


@dataclass(frozen=True, slots=True)
class Target:
    """一台能执行文本指令的音箱。``args`` 是入参名字，顺序即实参顺序。"""

    did: str
    name: str
    model: str
    siid: int
    aiid: int
    args: tuple[str, ...] = ()
    online: bool = True
    room: str = ""

    @property
    def ref(self) -> tuple[str, int, int]:
        """``(did, siid, aiid)``，直接喂 ``Client.run_action``。"""
        return (self.did, self.siid, self.aiid)


@dataclass(frozen=True, slots=True)
class Cleaned:
    """一次文本清洗的结果。三个字段都要给用户看，不能只留 ``text``。"""

    text: str
    truncated: bool = False
    #: 被剔除的字符（去重、保序），用来在 toast 里说清"哪些没发出去"
    dropped: str = ""

    @property
    def empty(self) -> bool:
        return not self.text


# ---------------------------------------------------------------------------
# 能力探测
# ---------------------------------------------------------------------------


def _find_action(spec: Any, name: str) -> Any:
    """在规格里按**名字**找 action。

    实现已收敛到 ``core/miot_action.py``：这里只补上"优先看
    ``intelligent-speaker`` 服务"这一条提示（固件改名时它会退到全服务搜）。
    """
    return miot_action.find_action(spec, name, SERVICE)


def target_for(
    did: str,
    name: str,
    model: str,
    spec: Any,
    *,
    online: bool = True,
    room: str = "",
) -> Target | None:
    """一台设备能不能执行文本指令；不能就返回 ``None``。"""
    act = _find_action(spec, TEXT_ACTION)
    if act is None:
        return None
    return Target(
        did=did,
        name=name,
        model=model,
        siid=int(act.siid),
        aiid=int(act.aiid),
        args=miot_action.arg_names(spec, act),
        online=bool(online),
        room=room,
    )


def collect(states: Iterable[Any]) -> list[Target]:
    """从 ``Store.devices`` 里挑出所有支持文本指令的音箱，在线的排前面。

    排序只在"首次选择默认目标"时起作用（见 ``ui/speaker_bar.py`` 的 ``set_targets``）；
    用户手动选过的 did 由偏好记住，不会因为排序变化被改掉。
    """
    out: list[Target] = []
    for st in states:
        node = getattr(st, "node", None)
        got = target_for(
            getattr(st, "did", ""),
            getattr(st, "name", ""),
            getattr(node, "model", ""),
            getattr(st, "spec", None),
            online=getattr(st, "online", True),
            room=getattr(st, "room", ""),
        )
        if got is not None and got.did:
            out.append(got)
    out.sort(key=lambda t: (not t.online, t.room, t.name))
    return out


def collect_home(states: Iterable[Any], home: str) -> list[Target]:
    """只取**某个家庭**里的音箱（``home`` 为空 = 只留没挂家庭的设备）。

    主窗口顶栏那条指令条和自动化编辑页的「发音箱指令」下拉共用这一份判据：
    两边各自写一遍筛选，迟早会出现"指令条看不到、编辑页能选到"这种对不上的情况。
    没挂家庭的设备（云端没给 home 字段）按现有约定算作任何家庭都可见。
    """
    keep = []
    for st in states:
        owner = getattr(getattr(st, "node", None), "home", "")
        if not owner or owner == home:
            keep.append(st)
    return collect(keep)


def wake_ref(spec: Any) -> tuple[int, int] | None:
    """``wake-up`` 的 ``(siid, aiid)``。无参，直接 ``run_action`` 即可。"""
    act = _find_action(spec, WAKE_ACTION)
    if act is None:
        return None
    return (int(act.siid), int(act.aiid))


# ---------------------------------------------------------------------------
# 入参
# ---------------------------------------------------------------------------


def directive(target: Target, text: str, silent: bool = False) -> list[Any]:
    """拼 ``execute-text-directive`` 的实参。

    按 ``args`` 里每个入参的名字取值，所以 ``in_piids`` 换个顺序也不会把"静默"
    塞进"文本"里。名字认不出时退回米家的实际顺序 ``[文本, 是否静默]``——见
    ``core/miot_action.payload``。
    """
    return miot_action.payload(
        target.args,
        {ARG_TEXT: text, ARG_SILENT: bool(silent)},
        [text, bool(silent)],
    )


# ---------------------------------------------------------------------------
# 文本清洗
# ---------------------------------------------------------------------------


def _allowed(ch: str) -> bool:
    """字符是否在放行范围内。

    数字是**刻意放进来的**：米家文案只写了"中文、英文"，但「音量调到 20」这类指令
    离了数字根本说不通，剔掉会静默改掉语义（比多发一个字符糟得多）。真被云端拒了
    会走失败 toast，用户看得见。
    """
    if ch in _PUNCT or ch in _SPACES:
        return True
    if "0" <= ch <= "9" or "a" <= ch <= "z" or "A" <= ch <= "Z":
        return True
    o = ord(ch)
    if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:  # CJK 基本区 / 扩展 A
        return True
    if 0xFF21 <= o <= 0xFF3A or 0xFF41 <= o <= 0xFF5A:  # 全角字母
        return True
    return 0xFF10 <= o <= 0xFF19  # 全角数字


def clean(raw: str) -> Cleaned:
    """按米家的字符白名单清洗，并截到 200 字符。

    被剔除的字符**不静默吞掉**：``dropped`` 交回界面弹一条 toast。清洗不对原文
    做别的加工（不删动词、不补标点），改一个字就可能改掉指令的意思。
    """
    if not raw:
        return Cleaned("")
    kept: list[str] = []
    dropped: list[str] = []
    for ch in raw:
        if ch in _SPACES:
            # 连续空白压成一个，避免"把　灯　关掉"这种带一堆空格的指令
            if kept and kept[-1] != " ":
                kept.append(" ")
        elif _allowed(ch):
            kept.append(ch)
        elif ch not in dropped:
            dropped.append(ch)
    text = "".join(kept).strip()
    truncated = len(text) > MAX_CHARS
    if truncated:
        text = text[:MAX_CHARS].rstrip()
    return Cleaned(text, truncated, "".join(dropped))


__all__ = [
    "ARG_SILENT",
    "ARG_TEXT",
    "MAX_CHARS",
    "SERVICE",
    "TEXT_ACTION",
    "WAKE_ACTION",
    "Cleaned",
    "Target",
    "clean",
    "collect",
    "collect_home",
    "directive",
    "target_for",
    "wake_ref",
]
