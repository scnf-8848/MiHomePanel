# SPDX-License-Identifier: GPL-3.0-or-later
"""轮询频率的需求登记：谁需要多快就报一份，取最严的那份统一生效。

为什么要有这一层：一个程序里迟早有多个"想要多快"的消费者——主窗口前台、主窗口
在托盘 / 最小化、将来的桌面挂件、将来的自动化规则。如果各自在自己代码里
``setInterval``，后写的那份会静默盖掉前面的：挂件要 1 秒，结果被窗口切后台那一下
改回 15 秒，而且没人说得清"现在这个间隔是谁要的"。

所以消费者**不碰定时器**，只登记需求 ``(owner, interval_ms, reason)``；由
:class:`PollDemandBoard` 取最严（间隔最小）的那份作为实际间隔，消费者消失就把自己
那条撤掉。

属性轮询与事件轮询是**两个接口、两套频率**（``prop/get`` 一轮通常就是 1 个请求、
``event/get`` 一个请求就能带几十个源），所以各开一张表，互不干扰。

三条刻意的选择：

1. **有下限**（``MIN_INTERVAL_MS``）。谁报得太快都不行——这是"保守设计"的兜底：
   将来即使某个模块手滑报 100 毫秒，也不会真的每 100 毫秒去打一次云端接口
   （米家对非官方客户端的频控没有公开保证，宁可慢一点）。
2. **结果不变就不通知**。多消费者时，切到后台不该把别人要的 1 秒重置成 15 秒；
   只有"取最严之后的值"真的变了才回调，定时器才不会被人反复重新计时。
3. **没人登记时回落到 ``idle_ms``**。属性轮询用默认间隔（窗口始终有需求，等于兜底），
   事件轮询用 ``None`` 表示"没人要就不跑"——不凭空加一路请求。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

#: 轮询**定时器**的生效下限（只约束 ``_poll_timer`` 这种周期性轮询）。
#:
#: 它**不约束**写入确认那类一次性 job：目标写的 0.5 秒定向读、场景 / 音箱的
#: 1 / 2.5 / 4 秒全量读都按自己的节奏走，见 ``ui/main_window.CONFIRM_*``。
#:
#: 和 ``Store.FULL_READ_MIN_GAP``（1.0 秒）是两层不同的规则，谁也替代不了谁：
#: 前者管"定时器最快多久跳一次"，后者管"两次**全量读**之间至少隔多久"（tick 与
#: 确认读都算）。把其中任何一个调大，都不能防止另一类的重复——比如下限是 1 秒时，
#: 1.0 / 2.5 / 4.0 秒的三次确认读依旧要和 tick 靠 ``FULL_READ_MIN_GAP`` 去重。
#:
#: 2026-11-12 曾从 1000 降到 500，是为了"写指令后开 3 秒高频窗口"；那个窗口已在
#: 2026-11-13 被写入确认取代，500 的下限失去用途，故改回 1000。现存需求只有
#: 3000 / 15000，所以这是一次**零行为变化**的改动，只是给未来更快需求的兜底。
MIN_INTERVAL_MS = 1000


@dataclass(frozen=True, slots=True)
class Demand:
    """一个消费者报上来的频率需求（间隔已按 ``floor_ms`` 收敛）。"""

    owner: str
    interval_ms: int
    reason: str = ""


class PollDemandBoard:
    """一张需求表。``interval_ms`` 是所有登记里最小的那个（``None`` = 不轮询）。

    ``subscribe`` 的回调签名是 ``Callable[[int | None], None]``，只在**结果真的变了**
    时触发。回调在调用 :meth:`set` / :meth:`clear` 的那条线程上同步执行。
    """

    def __init__(
        self,
        name: str,
        *,
        idle_ms: int | None = None,
        floor_ms: int = MIN_INTERVAL_MS,
    ) -> None:
        self.name = name
        self.idle_ms = idle_ms
        self.floor_ms = max(int(floor_ms), 1)
        self._items: dict[str, Demand] = {}
        self._listeners: list[Callable[[int | None], None]] = []
        self._current: int | None = None

    # -- 登记 ------------------------------------------------------------
    def set(self, owner: str, interval_ms: int | None, reason: str = "") -> None:
        """登记 / 更新一条需求。``interval_ms=None`` 等价于 :meth:`clear`。"""
        if interval_ms is None:
            self.clear(owner)
            return
        ms = max(int(interval_ms), self.floor_ms)
        self._items[owner] = Demand(owner=owner, interval_ms=ms, reason=reason)
        self._refresh()

    def clear(self, owner: str) -> None:
        if self._items.pop(owner, None) is not None:
            self._refresh()

    # -- 查询 ------------------------------------------------------------
    @property
    def interval_ms(self) -> int | None:
        """当前实际间隔；``None`` 表示没人要，应当停掉定时器。"""
        if self._items:
            return min(d.interval_ms for d in self._items.values())
        return self.idle_ms

    def demands(self) -> tuple[Demand, ...]:
        """全部登记，按间隔升序（第一条就是当前生效的那条）。"""
        return tuple(
            sorted(self._items.values(), key=lambda d: (d.interval_ms, d.owner))
        )

    def holder(self) -> Demand | None:
        """当前生效的那条需求（``idle_ms`` 兜底时返回 ``None``）。"""
        items = self.demands()
        return items[0] if items else None

    def describe(self) -> str:
        """给人看的一行摘要，排障时塞进状态提示 / 日志。"""
        cur = self.interval_ms
        if cur is None:
            return f"{self.name}：未轮询"
        who = self.holder()
        tail = f"（{who.owner}：{who.reason}）" if who and who.reason else ""
        if who is None:
            tail = "（默认）"
        others = len(self._items) - (1 if who else 0)
        more = f"，另有 {others} 条更宽的需求" if others > 0 else ""
        return f"{self.name}：{cur} ms{tail}{more}"

    # -- 订阅 ------------------------------------------------------------
    def subscribe(self, fn: Callable[[int | None], None]) -> None:
        if fn not in self._listeners:
            self._listeners.append(fn)

    def unsubscribe(self, fn: Callable[[int | None], None]) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    # -- 内部 ------------------------------------------------------------
    def _refresh(self) -> None:
        new = self.interval_ms
        if new == self._current:
            return
        self._current = new
        for fn in list(self._listeners):
            fn(new)


__all__ = ["MIN_INTERVAL_MS", "Demand", "PollDemandBoard"]
