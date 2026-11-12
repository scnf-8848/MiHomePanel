# SPDX-License-Identifier: GPL-3.0-or-later
"""纯逻辑规则引擎：三层防重、条件求值、按序动作队列、触发日志。

**不 import Qt、不 import ctypes、不碰网络。** 两个出口由外部注入：

* ``emit(name, done)`` —— 发一条中枢虚拟事件；``done(error)`` 在结果回来后调用
  （生产里由 ``jobs.SerialWorker`` 提交、主线程 ``drain()`` 回调，不能跨线程 emit Qt 信号）。
* ``call_later(ms, fn)`` —— 延迟动作；生产里必须接到**成员 QTimer**，不能在 UI 线程
  ``time.sleep()``。默认实现是同步立即执行，仅供离线验证兜底。

三层防重（见 ``docs/reference/pc-state-api.md`` 1.4）：

* **L0** ``MOD_NOREPEAT`` —— 在热键注册层，本引擎不管；
* **L1** 规则级冷静期 —— 从**触发被接受**那一刻起算（不是等动作跑完），只活在内存；
* **L2** 动作队列重入 —— 上一串没跑完又来一次，**丢弃 + 记日志**，不排队。

条件只在触发瞬间求值一次，不另起 timer、不加轮询、不加云端请求。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from .pc_state import PcState, evaluate_conditions
from .rules import Action, Rule, RuleSet

#: 日志 outcome 常量，界面按这个挑颜色 / 图标
FIRED = "fired"
COOLDOWN = "cooldown"
REENTRY = "reentry"
CONDITION = "condition"
SENT = "sent"
FAILED = "failed"
DONE = "done"

Emit = Callable[[str, Callable[[str], None]], None]
Later = Callable[[int, Callable[[], None]], Any]


@dataclass(frozen=True, slots=True)
class LogEntry:
    at: float
    rule_id: str
    rule_name: str
    keys: str
    outcome: str
    detail: str

    @property
    def time_text(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.at))


def _immediate(_ms: int, fn: Callable[[], None]) -> None:
    """离线兜底：不真的等。生产必须换成成员 QTimer。"""
    fn()


def _no_channel(name: str, done: Callable[[str], None]) -> None:
    done("没有配置发送通道")


class AutomationEngine:
    """一台常驻的规则执行器。**挂在主窗口 / 服务层，别挂在会被销毁的对话框上。**"""

    def __init__(
        self,
        rules: RuleSet,
        *,
        state: Any = None,
        emit: Emit | None = None,
        call_later: Later | None = None,
        now: Callable[[], float] | None = None,
        wall_now: Callable[[], float] | None = None,
        on_log: Callable[[LogEntry], None] | None = None,
        max_log: int = 200,
    ) -> None:
        self.rules = rules
        self.state = state if state is not None else PcState()
        self.emit = emit or _no_channel
        self.call_later = call_later or _immediate
        self._now = now or time.monotonic
        self._wall_now = wall_now or time.time
        self.on_log = on_log
        self.max_log = max_log
        self.log: list[LogEntry] = []
        self._last_fired: dict[str, float] = {}
        self._running: set[str] = set()
        self._closed = False

    # -- 对外 ------------------------------------------------------------
    @property
    def running(self) -> set[str]:
        return set(self._running)

    def cooldown_remaining(self, rule: Rule) -> float:
        """还剩多少秒冷静期（0 = 可以触发）。界面显示「剩余 0.6 s」用这个。"""
        last = self._last_fired.get(rule.id)
        if last is None or rule.cooldown_ms <= 0:
            return 0.0
        return max(0.0, rule.cooldown_ms / 1000 - (self._now() - last))

    def trigger(self, keys: str, *, source: str = "热键") -> int:
        """一个组合被按下。返回**被接受**的规则条数（被拦截的不算）。"""
        if self._closed:
            return 0
        accepted = 0
        for rule in self.rules.rules_for(keys):
            if self._fire(rule, source):
                accepted += 1
        return accepted

    def test(self, rule_id: str) -> bool:
        """页面「测试发送」。**绕过冷静期**（但仍记日志），不绕过重入保护。"""
        rule = self.rules.get(rule_id)
        if rule is None or self._closed:
            return False
        return self._fire(rule, "测试", bypass_cooldown=True)

    def close(self) -> None:
        """退出程序：丢弃未跑完的队列。延迟回调再来时会被 ``_closed`` 挡掉。"""
        self._closed = True
        self._running.clear()

    def clear_log(self) -> None:
        self.log.clear()

    # -- 触发 ------------------------------------------------------------
    def _fire(self, rule: Rule, source: str, *, bypass_cooldown: bool = False) -> bool:
        wait = 0.0 if bypass_cooldown else self.cooldown_remaining(rule)
        if wait > 0:
            self._add_log(rule, COOLDOWN,
                          f"{source}被冷静期拦截，剩余 {wait:.1f} 秒"
                          f"（本规则 {rule.cooldown_ms} ms）")
            return False
        if rule.id in self._running:
            self._add_log(rule, REENTRY, "上一串动作还没跑完，本次触发已丢弃（不排队）")
            return False

        # 条件只在触发瞬间求值一次
        snap = self.state.read()
        elapsed = getattr(self.state, "process_elapsed_minutes", None)
        passed, notes = evaluate_conditions(rule.conditions, rule.condition_mode, snap, elapsed)
        if not passed:
            label = "全部" if rule.condition_mode == "all" else "任一"
            self._add_log(rule, CONDITION,
                          f"条件（{label}）不成立；" + "；".join(notes))
            return False

        # **从触发被接受起算**，不是等动作跑完；否则「发事件 → 延迟 5s → 再发」
        # 会在延迟期间被反复重入
        self._last_fired[rule.id] = self._now()
        self._add_log(rule, FIRED,
                      f"{source}触发" + ("（测试，绕过冷静期）" if bypass_cooldown else ""))
        actions = tuple(rule.actions)  # 快照：队列跑的时候改规则不影响这一串
        self._running.add(rule.id)
        self._step(rule, actions, 0)
        return True

    # -- 动作队列 --------------------------------------------------------
    def _step(self, rule: Rule, actions: tuple[Action, ...], index: int) -> None:
        if self._closed or rule.id not in self._running:
            return
        if index >= len(actions):
            self._running.discard(rule.id)
            self._add_log(rule, DONE, f"动作执行完毕（共 {len(actions)} 步）")
            return
        act = actions[index]
        if act.type == "delay":
            self._add_log(rule, SENT, f"延迟 {act.ms} ms")
            self.call_later(act.ms, lambda: self._step(rule, actions, index + 1))
            return
        if act.type != "virtual_event":
            self._running.discard(rule.id)
            self._add_log(rule, FAILED, f"未知动作类型 {act.type}，队列已停止")
            return

        def done(error: str) -> None:
            if self._closed or rule.id not in self._running:
                return
            if error:
                self._running.discard(rule.id)
                self._add_log(rule, FAILED, f"发送「{act.name}」失败：{error}，队列已停止")
                return
            self._add_log(rule, SENT, f"已发送虚拟事件「{act.name}」")
            self._step(rule, actions, index + 1)

        try:
            self.emit(act.name, done)
        except Exception as exc:  # noqa: BLE001  发送通道抛异常也要收尾，别让 _running 卡死
            self._running.discard(rule.id)
            self._add_log(rule, FAILED, f"发送「{act.name}」异常：{exc}")

    # -- 日志 ------------------------------------------------------------
    def _add_log(self, rule: Rule, outcome: str, detail: str) -> None:
        entry = LogEntry(
            at=self._wall_now(), rule_id=rule.id, rule_name=rule.name,
            keys=rule.keys, outcome=outcome, detail=detail,
        )
        self.log.append(entry)
        if len(self.log) > self.max_log:
            del self.log[: len(self.log) - self.max_log]
        if self.on_log:
            try:
                self.on_log(entry)
            except Exception:  # noqa: BLE001  界面回调出错不能拖垮规则执行
                pass


__all__ = [
    "CONDITION",
    "COOLDOWN",
    "DONE",
    "FAILED",
    "FIRED",
    "REENTRY",
    "SENT",
    "AutomationEngine",
    "LogEntry",
]
