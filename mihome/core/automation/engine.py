# SPDX-License-Identifier: GPL-3.0-or-later
"""纯逻辑规则引擎：三层防重、条件求值、按序动作队列、触发日志。

**不 import Qt、不 import ctypes、不碰网络。** 两个出口由外部注入：

* ``run(action, done)`` —— 执行一条**设备动作**（对某台设备做一件事）。**提交即返回**，
  队列不等回执（理由见 ``_step``）；``done(error)`` 只在**失败**时调一次——发送前被
  判据挡下是同步调，回执报错是稍后调。**解析设备、查 action 坐标、清洗参数、
  拼入参全在调用方**（``core/automation/action_catalog.py`` + ``AutomationService._run``），
  引擎不认设备、不认型号、也不认功能名。
* ``call_later(ms, fn)`` —— 延迟动作；生产里必须接到**成员 QTimer**，不能在 UI 线程
  ``time.sleep()``。默认实现是同步立即执行，仅供离线验证兜底。

三层防重（见 ``docs/reference/pc-state-api.md`` 1.4）：

* **L0** ``MOD_NOREPEAT`` —— 在热键注册层，本引擎不管；
* **L1** 规则级冷静期 —— 从**触发被接受**那一刻起算（不是等动作跑完），只活在内存；
* **L2** 动作队列重入 —— 上一串没跑完又来一次，**丢弃 + 记日志**，不排队。

条件只在触发瞬间求值一次，不另起 timer、不加轮询、不加云端请求。

**日志**：本层只往 ``self.log`` 里追加（上限 ``max_log``），**不碰文件系统**；
要落盘由外部通过 ``on_log`` 接 :class:`logstore.LogStore`（生产里是
``AutomationService._on_log``），启动时用 :meth:`AutomationEngine.restore_log` 读回来。

**④ 否则分支**：条件不成立且规则有 ``else_actions`` 时，跑那一串动作并**照常占住**
冷静期与重入锁——冷静期针对的是"触发动作"，不区分走哪条分支，否则连按会反复执行
否则队列。没有 ``else_actions`` 时保持原样：记一条"条件不成立"，不算触发被接受。
**动作执行失败不走 ④**：那是故障，走日志 + 系统通知（见 ``ui/main_window``）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .pc_state import PcState, evaluate_conditions
from .rules import Action, Rule, RuleSet

#: 日志 outcome 常量，界面按这个挑颜色 / 图标
FIRED = "fired"
#: 条件不成立、改走了 ④「否则执行动作」。**别和 ``CONDITION`` 合并**：那个是
#: "按设计没执行"，这个是"真的跑了另一串动作"，日志表里两者颜色和含义都不同。
ELSE = "else"
COOLDOWN = "cooldown"
REENTRY = "reentry"
CONDITION = "condition"
SENT = "sent"
FAILED = "failed"
DONE = "done"

Run = Callable[[Action, Callable[[str], None]], None]
Later = Callable[[int, Callable[[], None]], Any]


@dataclass(frozen=True, slots=True)
class LogEntry:
    at: float
    rule_id: str
    rule_name: str
    keys: str
    outcome: str
    detail: str
    #: **同一次触发**产生的日志共用一个批次号，形如 ``规则id@触发毫秒-序号``；空串表示
    #: "不属于任何一次动作执行"——冷静期拦截 / 重入丢弃 / 条件不成立这三类本身就是一条
    #: 独立记录，没有"内部动作"可展开。日志窗靠它把一串动作折叠到触发那行下面。
    #:
    #: **是字符串不是内存计数器**：日志要落盘（见 ``logstore``），整数计数器重启即归零，
    #: 昨天的第 3 次和今天的第 3 次会折进同一行；带时间戳则跨重启唯一。
    run: str = ""

    @property
    def time_text(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.at))


def _immediate(_ms: int, fn: Callable[[], None]) -> None:
    """离线兜底：不真的等。生产必须换成成员 QTimer。"""
    fn()


def _no_channel(_act: Action, done: Callable[[str], None]) -> None:
    done("没有配置执行通道")


class AutomationEngine:
    """一台常驻的规则执行器。**挂在主窗口 / 服务层，别挂在会被销毁的对话框上。**"""

    def __init__(
        self,
        rules: RuleSet,
        *,
        state: Any = None,
        run: Run | None = None,
        call_later: Later | None = None,
        now: Callable[[], float] | None = None,
        wall_now: Callable[[], float] | None = None,
        on_log: Callable[[LogEntry], None] | None = None,
        max_log: int = 200,
    ) -> None:
        self.rules = rules
        self.state = state if state is not None else PcState()
        self.run = run or _no_channel
        self.call_later = call_later or _immediate
        self._now = now or time.monotonic
        self._wall_now = wall_now or time.time
        self.on_log = on_log
        self.max_log = max_log
        self.log: list[LogEntry] = []
        self._last_fired: dict[str, float] = {}
        self._running: set[str] = set()
        self._closed = False
        self._run_seq = 0

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
        """一个组合被按下。返回**被接受**的规则条数（被拦截的不算）。

        规则可以有多个触发，``rules_for`` 已按"任一命中"筛过；这里把**实际按下的
        那个组合**带进日志——一条规则挂了 F7 / F8 两个键时，日志要能分清是哪次。
        """
        if self._closed:
            return 0
        accepted = 0
        for rule in self.rules.rules_for(keys):
            if self._fire(rule, source, keys=keys):
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
        """清空内存日志。

        **只清内存**：日志已经落盘，单清这一半等于下次启动又全翻回来。要彻底清走
        ``AutomationService.clear_log()``（它会把 ``logstore`` 的文件一起删）。
        """
        self.log.clear()

    def restore_log(self, entries: Iterable[LogEntry]) -> None:
        """启动时把上次落盘的日志读回来（``logstore.load()`` 的结果）。

        **不触发 ``on_log``**：界面还没建，重放一遍回调没有意义；日志窗打开时本来
        就会读 ``engine.log`` 整表铺一遍。
        """
        self.log = list(entries)[-self.max_log:]

    # -- 触发 ------------------------------------------------------------
    def _fire(self, rule: Rule, source: str, *, keys: str = "",
              bypass_cooldown: bool = False) -> bool:
        wait = 0.0 if bypass_cooldown else self.cooldown_remaining(rule)
        if wait > 0:
            self._add_log(rule, COOLDOWN,
                          f"{source}被冷静期拦截，剩余 {wait:.1f} 秒"
                          f"（本规则 {rule.cooldown_ms} ms）", keys=keys)
            return False
        if rule.id in self._running:
            self._add_log(rule, REENTRY, "上一串动作还没跑完，本次触发已丢弃（不排队）",
                          keys=keys)
            return False

        # 条件只在触发瞬间求值一次
        snap = self.state.read()
        elapsed = getattr(self.state, "process_elapsed_minutes", None)
        passed, notes = evaluate_conditions(rule.conditions, rule.condition_mode, snap, elapsed)
        if not passed:
            label = "全部" if rule.condition_mode == "all" else "任一"
            note = f"条件（{label}）不成立；" + "；".join(notes)
            else_actions = tuple(rule.else_actions)
            if not else_actions:
                # 没有 ④：按设计什么都不做。**不算触发被接受**，不占冷静期、不占重入锁
                self._add_log(rule, CONDITION, note, keys=keys)
                return False
            # 有 ④：这一趟同样算"触发被接受"。冷静期是**针对触发动作**的，不分走的是
            # 哪条分支——否则连按会反复跑否则队列（一句"还没到点"能刷十遍）。
            self._last_fired[rule.id] = self._now()
            at = self._wall_now()
            run = self._new_run(rule, at)
            self._add_log(rule, ELSE, note + "；改走否则动作", keys=keys, run=run, at=at)
            self._running.add(rule.id)
            self._step(rule, else_actions, 0, branch="否则", run=run)
            return True

        # **从触发被接受起算**，不是等动作跑完；否则「发事件 → 延迟 5s → 再发」
        # 会在延迟期间被反复重入
        self._last_fired[rule.id] = self._now()
        at = self._wall_now()
        run = self._new_run(rule, at)
        self._add_log(rule, FIRED,
                      f"{source}触发" + ("（测试，绕过冷静期）" if bypass_cooldown else ""),
                      keys=keys, run=run, at=at)
        actions = tuple(rule.actions)  # 快照：队列跑的时候改规则不影响这一串
        self._running.add(rule.id)
        self._step(rule, actions, 0, run=run)
        return True

    def _new_run(self, rule: Rule, at: float) -> str:
        """领一个批次号：``规则id@触发毫秒-序号``。

        以前是内存里从 1 递增的整数。日志**落盘之后那样就不行了**：重启后新的一轮又
        从 1 开始，昨天的第 3 次和今天的第 3 次会在文件里折进同一行。改用「规则 + 触发
        毫秒」——同一条规则不可能在同一毫秒触发两次（冷静期最短 1 秒），跨重启也唯一；
        而且对着 ``automation_log.jsonl`` 排查时，光看这个号就知道是哪条规则哪一刻。

        末尾的 ``-序号`` 只是给"同一毫秒真的来了两次"（离线验证的假时钟会这样）和
        跨规则撞键兜底的，不参与任何展示。
        """
        self._run_seq += 1
        return f"{rule.id}@{int(at * 1000)}-{self._run_seq}"

    # -- 动作队列 --------------------------------------------------------
    def _step(self, rule: Rule, actions: tuple[Action, ...], index: int,
              *, branch: str = "", run: str = "") -> None:
        """**提交即下一步，不等回执**。

        动作之间没有依赖关系——米家的自动化也是按顺序执行，不是"前一条成功了才做
        下一条"的条件链。从前每一步都等 ``done`` 才走下一步，而 ``done`` 等的是
        云端把命令转给设备再等它回执：唤醒让音箱进聆听，这一等就是好几秒，后面的
        动作全堵在这条串行队列后面（``jobs.SerialWorker`` 只有一条线程，前一条的
        HTTP 不返回，后一条连发都发不出去）。

        现在 ``run`` 返回就算这一步完成了。失败只在日志和系统通知里体现，
        **不阻断后续动作**——一条规则里的第 2 步失败，用户仍然希望第 3 步照做。

        ``branch`` 只影响日志文案（"否则动作已发出"），调度完全相同。
        """
        if self._closed or rule.id not in self._running:
            return
        if index >= len(actions):
            self._running.discard(rule.id)
            self._add_log(rule, DONE, f"{branch}动作已发出（共 {len(actions)} 步）", run=run)
            return
        act = actions[index]
        if act.type == "delay":
            # 按用户填的那把尺子记：设「30 秒」的人不该在日志里读到 30000 ms。
            # ``unit`` 只影响这行文案，调度永远用 ``ms``。
            self._add_log(rule, SENT, f"延迟 {act.ms / 1000:g} 秒" if act.unit == "s"
                          else f"延迟 {act.ms} ms", run=run)
            self.call_later(act.ms, lambda: self._step(rule, actions, index + 1,
                                                      branch=branch, run=run))
            return
        feat = act.feature
        if act.type != "device_action" or feat is None:
            # 功能不在白名单里（规则文件被手改 / 以后删了某个功能）：说清楚，别静默
            # 跳过，但**不停队列**——后面那几步是完整的，凭什么不跑。
            self._add_log(rule, FAILED,
                          f"未知动作「{act.action or act.type or '-'}」，已跳过", run=run)
            self._step(rule, actions, index + 1, branch=branch, run=run)
            return

        label = act.device or act.did
        brief = self._brief(feat, act)
        sent = False

        def done(error: str) -> None:
            """结果回调。**推进队列不在这里做**，只负责把失败写进日志。

            ``sent=True`` 说明请求已经提交出去，此时失败是"回执说没成功"；
            ``sent=False`` 说明发送前就被判据挡下了（没选设备 / 离线 / 参数不合法），
            这一步其实**一个字都没发**。
            """
            if self._closed:
                return
            if not error:
                return
            where = "回执报错" if sent else "没发出去"
            self._add_log(rule, FAILED, f"{feat.label}未成功（「{label}」）：{error}（{where}）",
                          run=run)

        try:
            self.run(act, done)
            sent = True
        except Exception as exc:  # noqa: BLE001  通道抛异常也只记日志，不拦后续
            done(f"{exc}")
        if sent:
            self._add_log(rule, SENT, f"{feat.label} → 「{label}」{brief}", run=run)
        self._step(rule, actions, index + 1, branch=branch, run=run)

    @staticmethod
    def _brief(feat: Any, act: Action) -> str:
        """日志里的参数摘要：``（关灯，静默）``。太长截一下，别把日志表撑破。"""
        parts: list[str] = []
        for p in feat.params:
            value = act.args.get(p.name, p.default)
            if p.kind == "bool":
                if value:
                    parts.append(p.label)
                continue
            text = str(value or "").strip()
            if text:
                parts.append(text if len(text) <= 24 else text[:24] + "…")
        return f"（{'，'.join(parts)}）" if parts else ""

    # -- 日志 ------------------------------------------------------------
    def _add_log(self, rule: Rule, outcome: str, detail: str,
                 *, keys: str = "", run: str = "", at: float | None = None) -> None:
        """记一条日志。``at`` 由 :meth:`_fire` 传进来——批次号里的毫秒要和这行
        的时刻**完全一致**，各取一次 ``time.time()`` 就可能差 1 ms 对不上。"""
        entry = LogEntry(
            at=self._wall_now() if at is None else at, rule_id=rule.id, rule_name=rule.name,
            # 多触发时记**本次按下的那个键**；测试 / 无键位时退回第一条
            keys=keys or rule.keys, outcome=outcome, detail=detail, run=run,
        )
        self.log.append(entry)
        if len(self.log) > self.max_log:
            del self.log[: len(self.log) - self.max_log]
        if self.on_log:
            # 落盘在 ``AutomationService._on_log`` 里做（这一层不碰文件系统）
            try:
                self.on_log(entry)
            except Exception:  # noqa: BLE001  界面 / 落盘出错不能拖垮规则执行
                pass


__all__ = [
    "CONDITION",
    "COOLDOWN",
    "DONE",
    "ELSE",
    "FAILED",
    "FIRED",
    "REENTRY",
    "SENT",
    "AutomationEngine",
    "LogEntry",
]
