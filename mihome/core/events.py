# SPDX-License-Identifier: GPL-3.0-or-later
"""设备事件：把 ``/miotspec/event/get`` 的返回摊成能给界面画的行。

**和消息中心（``core/messages.py``）不是一套数据**，别混：

- 消息中心读的是**账号级通知记录**——云端决定要推的那些（洗衣完成、滤芯到期），
  只有一部分设备会有，所以做不成通用入口；
- 这里读的是**设备规格里声明的事件**（``spec.events()``）——按键单击、开锁、检测到
  移动、滤芯舱门被打开……只要设备规格里有，就能问。本机 55 台设备里 35 台有事件源，
  比"会发通知"的覆盖面大得多，这才是详情页该挂的东西。

代价是**只有最近一次**：``event/get`` 给的是"这个事件最后发生在什么时候"，不是历史。
晚一步去看，中间发生十次也只看到最后一次。所以详情页只画**时间最大的那一条**
（``line()``），不列多条也不计数（要计数得自己轮询 + 去重 ``updateTime``，那是自动化的事）。

两个必须知道的返回约定（2026-11-23 本机实测，细节见
``docs/reference/mijia-automation-events.md`` §3）：

- ``code == -9`` 表示**从来没发生过**，不是错误——按"没有"处理，别当失败；
- ``value`` 是**字符串**，里面才是 ``[{"piid": 1, "value": 3}, …]``，要二次解析。

参数只翻译**翻得出来**的：``piid`` 按事件所属服务的属性表换成中文名，值命中属性的
``value_list`` 才换文案。翻不出来的（时间戳、内部 ID）直接丢掉——"锁事件 · 当前时间
1795410088"对看的人没有意义。

这个文件全是纯函数、不发请求，离线就能断言。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .messages import ago
from .spec import DeviceSpec, PropertySpec

#: 一条事件最多附带几个参数。**只给 1 个**：详情页一行就那么大，两个参数叠上去
#: （"操作位置 无法区分门内外 · 操作方式 自动"）在最小窗口宽度下必折行，反而不好读。
DETAIL_MAX = 1
#: 字符串参数（虚拟事件的"事件名"这种）最多留多少字。中文 12px 一行约 29 字，
#: 事件名 + 时间已经占掉一半，留给参数的就这么多。
VALUE_MAX = 10


@dataclass(frozen=True, slots=True)
class DeviceEvent:
    """一个事件源的"最近一次"。

    ``when`` 是 unix **秒**；``0`` / ``code == -9`` 表示从来没发生过。
    """

    siid: int
    eiid: int
    label: str
    when: int = 0
    detail: str = ""
    code: int = 0

    @property
    def occurred(self) -> bool:
        return self.code == 0 and self.when > 0

    @property
    def method(self) -> str:
        return f"{self.siid}.{self.eiid}"

    def text(self) -> str:
        """一行给人看的文本：``开锁 · 3 分钟前（门外 · 指纹）``。

        界面直接用这一句，不再各自拼——两处拼法迟早不一致。
        """
        if not self.occurred:
            return f"{self.label} · 还没有记录"
        head = f"{self.label} · {ago(self.when)}"
        return f"{head}（{self.detail}）" if self.detail else head


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _pairs(raw: Any) -> list[tuple[int, Any]]:
    """``value`` 字段 → ``[(piid, value)]``。

    云端给的是**字符串**（``'[{"piid":1,"value":3}]'``），偶尔也可能是已经解析好的
    列表；坏数据一律当空——少显示一个参数，不该让整个页面报错。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if not isinstance(raw, list):
        return []
    out: list[tuple[int, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        piid = _int(item.get("piid"), -1)
        if piid >= 0:
            out.append((piid, item.get("value")))
    return out


def _value_text(prop: PropertySpec | None, value: Any) -> str:
    """一个参数值的文案。翻不出来返回空串（调用方据此跳过）。"""
    if prop is not None:
        for opt in prop.value_list:
            if opt.value == _int(value, -9999):
                return opt.short or opt.display
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, str):
        text = " ".join(value.split())
        if text and not text.isdigit():
            return text if len(text) <= VALUE_MAX else text[: VALUE_MAX - 1] + "…"
    return ""


def _detail(raw: Any, props: Mapping[int, PropertySpec]) -> str:
    parts: list[str] = []
    for piid, value in _pairs(raw):
        prop = props.get(piid)
        text = _value_text(prop, value)
        if not text:
            continue
        name = prop.display_label if prop is not None else ""
        parts.append(f"{name} {text}" if name else text)
        if len(parts) >= DETAIL_MAX:
            break
    return " · ".join(parts)


def _props_by_service(spec: DeviceSpec | None) -> dict[int, dict[int, PropertySpec]]:
    """``siid → {piid: 属性}``：事件参数按**所属服务**的属性表翻译。"""
    out: dict[int, dict[int, PropertySpec]] = {}
    if spec is None:
        return out
    for service in spec.services:
        out[service.siid] = {p.piid: p for p in service.properties}
    return out


def sources(spec: DeviceSpec | None) -> list[tuple[int, int]]:
    """``[(siid, eiid)]``：这台设备要问哪些事件源。规格里没事件就是空表。"""
    if spec is None:
        return []
    return [(e.siid, e.eiid) for e in spec.events()]


def parse(payload: Any, spec: DeviceSpec | None = None) -> list[DeviceEvent]:
    """``event/get`` 的返回 → 行；**发生过**的按时间倒序在前，没发生过的垫后。

    排序在这里做一次，``latest()`` 就能直接取第一个时间最大的，不用各调用方各排一遍。
    """
    if isinstance(payload, Mapping):
        payload = payload.get("events") or payload.get("params") or []
    if not isinstance(payload, list):
        return []

    events = {(_int(e.siid), _int(e.eiid)): e for e in (spec.events() if spec else ())}
    props = _props_by_service(spec)
    rows: list[DeviceEvent] = []
    seen: set[tuple[int, int]] = set()
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        key = (_int(item.get("siid"), -1), _int(item.get("eiid"), -1))
        if key in seen:
            # 同一源重复返回时认第一条：去重靠 updateTime，不靠顺序
            continue
        seen.add(key)
        meta = events.get(key)
        code = _int(item.get("code"))
        when = _int(item.get("updateTime"))
        rows.append(
            DeviceEvent(
                siid=key[0],
                eiid=key[1],
                # 规格里没有的就照实写方法号，不猜名字
                label=meta.label if meta is not None else f"事件 {key[0]}.{key[1]}",
                when=when if code == 0 else 0,
                detail=_detail(item.get("value"), props.get(key[0], {})),
                code=code,
            )
        )
    rows.sort(key=lambda r: (not r.occurred, -r.when, r.siid, r.eiid))
    return rows


def occurred(rows: Iterable[DeviceEvent]) -> list[DeviceEvent]:
    """只留真的发生过的（界面用）。"""
    return [r for r in rows if r.occurred]


def latest(rows: Iterable[DeviceEvent]) -> DeviceEvent | None:
    """最近发生的那一条；一条都没发生过返回 ``None``。

    ``event/get`` 只回答"这个事件最后发生在什么时候"，所以多问几个事件源，
    要给人看的也只是**时间最大的那一条**——其余几条（几个月前的开锁、从没触发过
    的告警）对"它最近出过什么事"没有增量信息。
    """
    live = occurred(rows)
    return max(live, key=lambda r: r.when) if live else None


def line(rows: Iterable[DeviceEvent]) -> str:
    """详情页左下角那一行：``开锁 · 3 分钟前（门外 · 指纹）``。

    没有记录时写「还没有记录」——**不返回空串**：空串会让那一行看起来像读取失败，
    而这台设备只是真的没出过事。读失败由调用方自己写（它才知道是网络问题）。
    """
    row = latest(rows)
    return row.text() if row is not None else "还没有记录"


__all__ = [
    "DETAIL_MAX",
    "DeviceEvent",
    "VALUE_MAX",
    "latest",
    "line",
    "occurred",
    "parse",
    "sources",
]
