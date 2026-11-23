# SPDX-License-Identifier: GPL-3.0-or-later
"""消息中心：把云端 ``/v2/message/v2/typelist`` 的返回摊成一张能给界面画的表。

**只读**（2026-11-23 用户定）：只列出来看，不标已读、不删除——那是写账号数据。
所以这个文件里没有任何网络调用，全是纯函数，离线就能断言。

和"设备事件"是两条不同的路，别混：

- 这里读的是**账号级通知记录**（洗衣完成、滤芯过期、场景执行失败），有 ``msg_id``、
  有正文、有 ``is_new``；
- ``/miotspec/event/get``（``poll_events`` 留给它的位置，**目前还没有消费者**）读的是
  **单台设备的最近一次事件**，只有一份、读完被覆盖，用来做触发，不能回看也不能计数。
  2026-11-23 曾在详情页左下角展示过这条，实测体验不理想、当天撤掉（结论见
  ``docs/dev/ui.md``「详情页底部」）。

接口形状是 2026-11-23 用只读探针在本机账号上实测的
（探针结论记在 ``docs/reference/mihome-pc-unofficial.md`` §4.5）：

- ``typelist`` 返回 ``{"messages": [...]}``，**没有游标**；``limit`` / ``type`` /
  ``home_id`` / ``timestamp`` 传什么都不改变结果（实测 limit 30 与 100 都只回 6 条：
  取 5 条设备消息 + 每类 1 条）。这是**账号级"最近几条"**，不是历史列表。
- 每条带 ``msg_id / uid / type / title / content / img_url / did / params /
  ctime / is_new / home_id / home_owner``；
- ``ctime`` 是 **unix 秒**（不是毫秒，别拿 ``time.time()*1000`` 比）；
- **家庭 id 在 ``params.body.home_id`` 里**，不在顶层：顶层那个 ``home_id`` 实测**恒为 0**
  （6 条全是），真正的 15 位家庭 id 只出现在 ``params.body.home_id``（5 条设备消息都有，
  两个家庭两个值，都在账号的家庭列表里）。场景通知（``type=13``）的 ``params`` 是空
  dict，**归属不明**——这种按 ``parse(home=...)`` 的规矩照留。
- 单设备历史 ``/v2/message/v2/list`` 的 ``type`` 是**必填**（不传 → ``-8 invalid param``），
  同类型各问一次才能凑齐；但**只有最近一批、不能往回翻**，且"有没有记录"取决于设备
  会不会发通知（实测家里只有门锁 / 洗衣机 / 蒸烤箱 / 净烟机有）——所以它没做成通用
  入口，详情页最后也没挂它。接口结论留在
  ``docs/reference/mihome-pc-unofficial.md`` §4.5，要恢复实现看 ``git log``。

已知类型：``6`` 设备消息、``11`` 设备离线、``13`` 场景通知。没见过的类型**保留数字
和一条中性标签**，不猜——把场景通知误标成设备消息，比显示"消息"更糟。

**不做未读**（2026-11-23 用户定）：``is_new`` 仍然读得到，但界面不显示——我们从
不标已读，那个点会永远亮着，反而成了噪音。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

#: 类型 → 中文标签。来源：同类项目真账号实测（2026-11-21），见 §4.5 那张接口表。
TYPE_LABELS: dict[int, str] = {
    6: "设备消息",
    11: "设备离线",
    13: "场景通知",
}

#: 不认识的类型统一这么叫（保留数字在 ``Message.kind`` 里）
FALLBACK_LABEL = "消息"

#: 一次拉多少条。云端这个接口没有游标，给多少就是上限；30 条已经够"看看有什么"，
#: 再多也只是把同一屏塞满（对方默认也是 30）。
DEFAULT_LIMIT = 30

#: 正文超过这个长度就截断。列表是一眼扫的，没人读长文；完整正文留给悬停。
BODY_MAX = 120


def _int(value: Any) -> int:
    """云端字段什么都可能给：``None`` / 字符串 / 浮点。转不动就当 0。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _body(record: Mapping[str, Any]) -> Mapping[str, Any]:
    """``params.body``；消息中心这条链路里真正的业务字段都埋在它下面。"""
    params = record.get("params")
    body = params.get("body") if isinstance(params, Mapping) else None
    return body if isinstance(body, Mapping) else {}


def _home_id(record: Mapping[str, Any]) -> str:
    """这条记录属于哪个家庭；判断不了就是 ``""``。

    **顶层 ``home_id`` 不可用**：2026-11-23 本机只读探针里 6 条记录全是 ``0``——
    拿它比对会让"按家庭过滤"看起来永远生效不了（或者把它们全删掉）。真正的家庭 id
    在 ``params.body.home_id``（15 位整数，两个家庭两个值，都在账号的家庭列表里）。
    两处都试，取第一个大于 0 的；场景通知的 ``params`` 是空 dict，于是得到 ``""``。
    """
    for value in (record.get("home_id"), _body(record).get("home_id")):
        try:
            number = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if number > 0:
            return str(number)
    return ""


@dataclass(frozen=True)
class Message:
    """一条通知记录。字段名与云端一致，只有 ``label`` 是加工出来的。"""

    msg_id: str
    kind: int
    label: str
    title: str
    content: str
    did: str
    device: str
    home: str
    when: int

    @property
    def body(self) -> str:
        """能显示在标题下面的正文；**和设备名/标题重复时返回空串**。

        实测这批消息的 ``content`` 经常就是设备名（"洗衣机"），照抄会在标题下面
        再写一遍"洗衣机"，白白多占一行。
        """
        text = self.content.strip()
        if not text:
            return ""
        if text == self.title.strip() or text == self.device.strip():
            return ""
        return text


def parse(
    payload: Any,
    names: Mapping[str, str] | None = None,
    *,
    home: str = "",
) -> list[Message]:
    """云端 payload → 按时间倒序的 :class:`Message` 列表。

    ``names`` 是 ``did → 设备名``，用来把 ``did`` 变成人看得懂的名字；查不到就留空，
    界面只显示时间和类型。脏记录（没有 ``msg_id``、不是 dict）直接丢：没有 id 的
    记录既没法定位也没法去重，留着只会让列表里多一行空白。

    ``home`` 给当前家庭 id 时只留这个家庭的消息。`home_id` 取的是
    ``params.body.home_id``（顶层那个恒为 0，见 :func:`_home_id`）。
    **判断不了家庭的照留**：场景通知的 ``params`` 是空的，宁可多显示一条别家消息，
    也不要把自己家的消息悄悄藏掉。
    """
    data = payload if isinstance(payload, Mapping) else {}
    known = names or {}
    rows: list[Message] = []
    for record in data.get("messages") or ():
        if not isinstance(record, Mapping):
            continue
        msg_id = str(record.get("msg_id") or "").strip()
        if not msg_id:
            continue
        kind = _int(record.get("type"))
        did = str(record.get("did") or "")
        msg_home = _home_id(record)
        if home and msg_home and msg_home != str(home):
            continue
        rows.append(
            Message(
                msg_id=msg_id,
                kind=kind,
                label=TYPE_LABELS.get(kind, FALLBACK_LABEL),
                title=str(record.get("title") or "").strip(),
                content=str(record.get("content") or "").strip(),
                did=did,
                device=known.get(did, "") if did else "",
                home=msg_home,
                when=_int(record.get("ctime")),
            )
        )
    # 云端返回顺序不保证（同一秒内更是随机），按时间倒序排一遍
    rows.sort(key=lambda item: item.when, reverse=True)
    return rows


def summarise(items: Iterable[Message]) -> str:
    """标题那行小字：``6 条消息``。

    不写未读数（2026-11-23 用户定）：这里从不标已读，未读数只会一直是"全部未读"。
    """
    rows = list(items)
    return f"{len(rows)} 条消息" if rows else "没有消息"


def ago(seconds: float, *, now: float | None = None) -> str:
    """``ctime`` → ``刚刚 / 5 分钟前 / 3 小时前 / 2 天前``。

    90 秒以内写"刚刚"：这一屏的信息本来就不精确到秒，写"43 秒前"只会让人去数。
    """
    if not seconds:
        return ""
    delta = max(0.0, (now if now is not None else time.time()) - seconds)
    if delta < 90:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    return f"{int(delta // 86400)} 天前"


def clip(text: str, limit: int = BODY_MAX) -> str:
    """正文截断。按字符数切，不按像素——这里只需要"别把一行撑到五行"。"""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


__all__ = [
    "BODY_MAX",
    "DEFAULT_LIMIT",
    "FALLBACK_LABEL",
    "Message",
    "TYPE_LABELS",
    "ago",
    "clip",
    "parse",
    "summarise",
]
