# SPDX-License-Identifier: GPL-3.0-or-later
"""离线验证「电脑自动化 v1」的 core 层：不联网、不注册热键、不碰真实设备。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_automation_rules.py``

覆盖范围：

* ``hotkeys.py`` —— 规范串、Win32 参数（含 ``MOD_NOREPEAT``）、反查、保留键；
* ``rules.py``   —— schema 校验、三类动作的落盘形状（含音箱的 ``did/text/silent``）、
  坏数据字段级修复、原子落盘、启用键位去重与分发；
* ``pc_state.py``—— 6 类电脑条件、AND / OR、空条件、条件不可用（台式机无电池）；
* ``engine.py``  —— 冷静期（默认 1s / 0=关 / 剩余秒数）、动作队列重入、条件不成立、
  动作按序 + 延迟、发送失败停止、音箱通道（``say``）的成功 / 静默 / 失败 / 未注入、
  测试绕过冷静期、日志、close；
* ``logbook.py``—— 独立日志窗的筛选 / 查找（关键字、规则、结果、时间范围）与表格列；
* ``rules.py``   —— 软删除回收站：trash / restore / purge 与「删除后不注册热键」。

时间 / 延迟 / 云端发送全部是假的，所以任何平台都能跑。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_TMP = Path(tempfile.mkdtemp(prefix="mihome-automation-"))
os.environ["MIHOME_DATA_DIR"] = str(_TMP / "data")

from mihome.core.automation import (  # noqa: E402
    ACTION_TYPES,
    CONDITION,
    COOLDOWN,
    DONE,
    FAILED,
    FAIL_OUTCOMES,
    FIRED,
    MAX_SPEAK_CHARS,
    MOD_NOREPEAT,
    NEUTRAL_OUTCOMES,
    OPS,
    OUTCOME_LABELS,
    OUTCOME_ORDER,
    REENTRY,
    SENT,
    TIME_RANGES,
    WARN_OUTCOMES,
    Action,
    AutomationEngine,
    Condition,
    LogEntry,
    PcState,
    Rule,
    RuleSet,
    Snapshot,
    availability,
    date_text,
    evaluate_conditions,
    filter_entries,
    full_text,
    log_columns,
    matches,
    normalize,
    outcome_label,
    outcome_tone,
    parse_native,
    reserved_reason,
    to_native,
    validate_rule,
)

FAILS: list[str] = []
N = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global N
    N += 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAILS.append(f"{name} {detail}")


def section(title: str) -> None:
    print(f"\n--- {title} ---", flush=True)


# ---------------------------------------------------------------------------
# 假的时钟 / 延迟 / 状态 / 发送通道
# ---------------------------------------------------------------------------

class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def now(self) -> float:
        return self.t

    def wall(self) -> float:
        return 1_700_000_000 + self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Scheduler:
    """记录延迟回调，手动 pump；避免真等，也不引入 Qt。"""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.pending: list[tuple[float, int, object]] = []
        self.seq = 0

    def __call__(self, ms: int, fn) -> None:
        self.pending.append((self.clock.t + ms / 1000, self.seq, fn))
        self.seq += 1

    def pump(self) -> None:
        while self.pending:
            self.pending.sort(key=lambda item: (item[0], item[1]))
            at, _, fn = self.pending.pop(0)
            self.clock.t = max(self.clock.t, at)
            fn()


class FakeState:
    def __init__(self, snap: Snapshot, elapsed: dict[str, float] | None = None) -> None:
        self.snap = snap
        self.elapsed = elapsed or {}
        self.reads = 0

    def read(self) -> Snapshot:
        self.reads += 1
        return self.snap

    def process_elapsed_minutes(self, name: str) -> float | None:
        return self.elapsed.get(name.lower())


class Emitter:
    def __init__(self, fail_on: str = "") -> None:
        self.calls: list[str] = []
        self.fail_on = fail_on

    def __call__(self, name: str, done) -> None:
        self.calls.append(name)
        done(f"模拟失败：{name}" if name == self.fail_on else "")


class Speaker:
    """假的音箱通道（引擎的第三个出口 ``say``）：记录说给谁、说了什么、静不静默。

    真实现要解析设备、清洗文本、拼 miot 入参（``core/speaker.py`` +
    ``AutomationService._say``），这里一概不管——引擎本来就不该认设备。
    """

    def __init__(self, fail_on: str = "") -> None:
        self.calls: list[tuple[str, str, bool]] = []
        self.fail_on = fail_on

    def __call__(self, act: Action, done) -> None:
        self.calls.append((act.did, act.text, bool(act.silent)))
        done(f"模拟失败：{act.text}" if act.text == self.fail_on else "")


def base_rule(**kw) -> Rule:
    data = dict(
        id="r1", name="测试规则", enabled=True, keys="Ctrl+Alt+F7",
        condition_mode="all", conditions=[], cooldown_ms=1000,
        actions=[Action("virtual_event", name="事件A")],
    )
    data.update(kw)
    return Rule(**data)


def make_engine(rule: Rule, *, state=None, emitter=None, clock=None, sched=None,
                say=None, fail_on: str = ""):
    clock = clock or Clock()
    sched = sched or Scheduler(clock)
    emitter = emitter or Emitter(fail_on)
    rules = RuleSet(_TMP / "engine-rules.json")
    rules.rules = [rule]
    engine = AutomationEngine(
        rules, state=state or FakeState(Snapshot(available=True)),
        emit=emitter, say=say, call_later=sched, now=clock.now, wall_now=clock.wall,
    )
    return engine, clock, sched, emitter


# ---------------------------------------------------------------------------
section("热键：规范串与 Win32 参数")
# ---------------------------------------------------------------------------

check("大小写/别名/乱序收敛", normalize("Control+alt+f7") == "Ctrl+Alt+F7")
check("裸单键允许", normalize("F7") == "F7")
check("两个主键拒绝", normalize("F7+F8") == "")
check("重复修饰键拒绝", normalize("Ctrl+Ctrl+F7") == "")
check("未知键拒绝", normalize("Ctrl+Hyper") == "")
check("空串拒绝", normalize("") == "")

native = to_native("Ctrl+Alt+F7")
check("native 带 MOD_NOREPEAT", native is not None and bool(native[0] & MOD_NOREPEAT))
check("native mods = Ctrl|Alt", native is not None and native[0] & 0xF == 0x0002 | 0x0001)
check("native vk = F7(0x76)", native is not None and native[1] == 0x76)
check("反查回到规范串", native is not None
      and parse_native(native[0] & 0xF, native[1]) == "Ctrl+Alt+F7")
check("F13–F24 可表达", to_native("F18") is not None and to_native("F18")[1] == 0x81)
check("系统保留键被拦", bool(reserved_reason("Win+L")) and bool(reserved_reason("F12")))
check("Win+字母被拦", bool(reserved_reason("Win+Q")))
check("普通组合不预judge", reserved_reason("Ctrl+Alt+F7") == "")

# ---------------------------------------------------------------------------
section("规则：schema 与落盘")
# ---------------------------------------------------------------------------

check("合法规则零错误", validate_rule(base_rule()) == [])
bad = base_rule(name="", keys="F12", actions=[Action("delay", ms=20)])
errs = validate_rule(bad)
check("空名/保留键/没有发事件动作全被抓到", len(errs) >= 3, str(errs))
check("延迟越界被抓", bool(validate_rule(base_rule(actions=[
    Action("virtual_event", name="A"), Action("delay", ms=10**9)]))))
check("条件模式非法被抓", bool(validate_rule(base_rule(condition_mode="xor"))))
check("数值条件填中文被抓", bool(validate_rule(base_rule(conditions=[
    Condition("idle_minutes", "ge", "十分钟")]))))
check("运行时长缺程序名被抓", bool(validate_rule(base_rule(conditions=[
    Condition("process_elapsed", "ge", "20", "")]))))
check("锁屏参数非法被抓", bool(validate_rule(base_rule(conditions=[
    Condition("locked", "is", "也许")]))))
check("比较符表覆盖所有条件类型", all(k in OPS for k in
      ("is", "is_not", "ge", "le", "contains", "equals")))

# 延迟的两种录入规格（界面「＋ 延迟（毫秒）」/「＋ 延迟（秒）」）：unit 只是刻度，
# 语义永远由 ms 决定；毫秒档不写这个键，旧 rules.json 与旧版本读者都不受影响。
check("秒档落盘带 unit", Action("delay", ms=30000, unit="s").to_dict()
      == {"type": "delay", "ms": 30000, "unit": "s"})
check("毫秒档落盘没有 unit 键", "unit" not in Action("delay", ms=30000).to_dict())
check("秒档能原样读回", (lambda a: a.unit == "s" and a.ms == 30000)(
    Action.from_dict({"type": "delay", "ms": 30000, "unit": "s"})))
check("没有 unit 的旧数据读成毫秒档",
      Action.from_dict({"type": "delay", "ms": 30000}).unit == "ms")
check("坏 unit 退回毫秒档而不是丢动作",
      Action.from_dict({"type": "delay", "ms": 1000, "unit": "秒"}).unit == "ms")
check("延迟越界提示写明两个刻度",
      any("600 秒" in e for e in validate_rule(base_rule(actions=[
          Action("virtual_event", name="A"), Action("delay", ms=10**9)]))))

# 「发音箱指令」动作：did 是执行用的，name 只是显示名快照（日志 / 回显），
# silent 默认 false 时不写键——旧版本读新文件、新版本读旧文件都不受影响。
check("动作类型只有三档，没有 delay_s 之类的伪类型",
      ACTION_TYPES == ("virtual_event", "delay", "speaker"), str(ACTION_TYPES))
spk_dict = Action("speaker", name="客厅音箱", did="123456", text="打开台灯").to_dict()
check("音箱动作落盘 = did/name/text 三字段",
      spk_dict == {"type": "speaker", "did": "123456", "name": "客厅音箱",
                   "text": "打开台灯"}, str(spk_dict))
check("不静默是默认值，不写 silent 键", "silent" not in spk_dict)
check("静默才写 silent: true",
      Action("speaker", did="d1", text="开灯", silent=True).to_dict().get("silent") is True)
back = Action.from_dict({"type": "speaker", "did": "123456", "name": "客厅音箱",
                         "text": "开灯", "silent": True})
check("音箱动作能原样读回", (back.did, back.name, back.text, back.silent)
      == ("123456", "客厅音箱", "开灯", True), str(back))
check("silent 只认 true（写「是」/1 都算不静默）",
      Action.from_dict({"type": "speaker", "did": "d", "text": "x",
                        "silent": "是"}).silent is False)
check("音箱动作不残留 delay 的 ms/unit",
      (lambda a: a.ms == 0 and a.unit == "ms")(
          Action.from_dict({"type": "speaker", "did": "d", "text": "x"})))
check("没选音箱被抓", any("没选音箱" in e for e in validate_rule(base_rule(actions=[
    Action("speaker", name="客厅", did="", text="开灯")]))))
check("没填指令被抓", any("没填要说的话" in e for e in validate_rule(base_rule(actions=[
    Action("speaker", name="客厅", did="d1", text="   ")]))))
check(f"指令超过 {MAX_SPEAK_CHARS} 字被抓",
      any(str(MAX_SPEAK_CHARS) in e for e in validate_rule(base_rule(actions=[
          Action("speaker", name="客厅", did="d1", text="灯" * (MAX_SPEAK_CHARS + 1))]))))
check("只有一条音箱动作也能过校验（按键 → 说句话是完整规则）",
      validate_rule(base_rule(actions=[
          Action("speaker", name="客厅", did="d1", text="我回来了")])) == [])
check("只有延迟仍然过不了（得有一条真会干事的）",
      any("发音箱指令" in e for e in validate_rule(base_rule(actions=[
          Action("delay", ms=1000)]))))
check("发虚拟事件 + 延迟照旧合法",
      validate_rule(base_rule(actions=[Action("virtual_event", name="A"),
                                       Action("delay", ms=1000)])) == [])

# 空档「-」（用户 11-13 第八条）：② 和 ③ 加出来的一行默认没选类型，type 是空串。
# 这一层必须拦——`Action.to_dict` 会把空类型和未知类型都写成 virtual_event，
# 而 `from_dict` 又读不回它，放过去就等于悄悄把动作换成了另一种。
check("动作没选类型（空串）被抓，且说「还没选」而不是「未知」",
      any("还没选动作类型" in e for e in validate_rule(base_rule(actions=[
          Action("virtual_event", name="A"), Action("")]))),
      str(validate_rule(base_rule(actions=[Action("virtual_event", name="A"),
                                           Action("")]))))
check("空类型动作不会被顺手写成 virtual_event 混过校验",
      bool(validate_rule(base_rule(actions=[Action("")]))))
check("条件没选类型（空串）被抓，且和「类型未知」分开说",
      any("还没选条件类型" in e for e in validate_rule(base_rule(conditions=[
          Condition("", "", "x")]))),
      str(validate_rule(base_rule(conditions=[Condition("", "", "x")]))))
check("真正写错的条件类型仍然是「类型未知」",
      any("类型未知" in e for e in validate_rule(base_rule(conditions=[
          Condition("no_such_type", "is", "x")]))))
check("选了类型的规则不受影响（空档只在没选时算错）",
      validate_rule(base_rule(conditions=[Condition("idle_minutes", "ge", "10")],
                              actions=[Action("virtual_event", name="A")])) == [])

# 家庭归属（用户 11-13 第二轮）：「1103 家庭里不显示共享家庭的规则」。
# home 为空串 = 还没归属（升级前写的 rules.json 没这个键 / 没登录时建的），
# 那种规则**在任何家庭都可见**——藏起来会让人以为规则丢了，不是隔离是丢数据。
check("home 非空才落盘", base_rule(home="1103 家").to_dict().get("home") == "1103 家")
check("没归属时不写 home 键", "home" not in base_rule().to_dict())
check("home 能原样读回",
      Rule.from_dict(base_rule(home="共享家庭").to_dict()).home == "共享家庭")
check("旧数据没 home 键 → 读成空串",
      Rule.from_dict({"id": "old", "name": "老规则"}).home == "")

path = _TMP / "rules.json"
rs = RuleSet(path)
r1 = base_rule(id="a", keys="Ctrl+Alt+F7")
r2 = base_rule(id="b", name="第二条", keys="ctrl+alt+f7", enabled=False)
check("add 两条", rs.add(r1) and rs.add(r2))
check("重复 id 拒绝", not rs.add(base_rule(id="a")))
check("落盘文件存在", path.exists())
rs3 = RuleSet(path)
check("启用键位去重（只注册一次）", rs3.enabled_keys() == ["Ctrl+Alt+F7"])
check("启用的才触发", len(rs3.rules_for("Ctrl+Alt+F7")) == 1)
check("启用第二条后同一组合分发给两条", (
    rs3.set_enabled("b", True), len(rs3.rules_for("ctrl+alt+f7")) == 2)[1])
check("停用后该组合仍只剩一条", (
    rs3.set_enabled("b", False), len(rs3.rules_for("Ctrl+Alt+F7")) == 1)[1])
check("remove 生效", rs3.remove("b") and rs3.get("b") is None)
check("update 生效", rs3.update(base_rule(id="a", name="改名后")) and rs3.get("a").name == "改名后")

# 坏数据字段级修复
path.write_text('{"version": 1, "rules": ['
                '{"id":"ok","keys":"alt+ctrl+f9","cooldown_ms":"坏","actions":[{"type":"delay"}]},'
                '{"keys":"没有 id"}, 42]}', encoding="utf-8")
fixed = RuleSet(path)
check("坏数据不整体丢掉", len(fixed.rules) == 1 and fixed.rules[0].id == "ok")
check("坏 cooldown 退回默认", fixed.rules[0].cooldown_ms == 1000)
check("键位读入时规范化", fixed.rules[0].keys == "Ctrl+Alt+F9")
check("坏结构留下 warning", len(fixed.warnings) >= 2, str(fixed.warnings))

# ---------------------------------------------------------------------------
section("条件：6 类电脑状态 / AND / OR / 不可用")
# ---------------------------------------------------------------------------

snap = Snapshot(
    available=True,
    processes=frozenset({"notepad.exe", "python.exe"}),
    foreground_exe="workbuddy.exe", foreground_title="MiHomePanel - 文档",
    idle_seconds=12 * 60, locked=False, ac=True, battery_percent=42, has_battery=True,
)

ok, _ = evaluate_conditions([], "all", snap)
check("空条件 = 无条件", ok)
check("进程在运行 is", evaluate_conditions(
    [Condition("process_running", "is", "Notepad.exe")], "all", snap)[0])
check("进程 is_not", evaluate_conditions(
    [Condition("process_running", "is_not", "chrome.exe")], "all", snap)[0])
check("不带 .exe 也能匹配", evaluate_conditions(
    [Condition("process_running", "is", "notepad")], "all", snap)[0])
check("运行时长 ge 成立", evaluate_conditions(
    [Condition("process_elapsed", "ge", "20", "python.exe")], "all", snap,
    lambda name: 25.0 if name == "python.exe" else None)[0])
check("运行时长拿不到不算成立", not evaluate_conditions(
    [Condition("process_elapsed", "ge", "20", "python.exe")], "all", snap,
    lambda name: None)[0])
check("前台标题 contains", evaluate_conditions(
    [Condition("foreground", "contains", "MiHomePanel")], "all", snap)[0])
check("前台 equals 大小写不敏感", evaluate_conditions(
    [Condition("foreground", "equals", "WORKBUDDY.EXE")], "all", snap)[0])
check("前台 equals 不等于 contains", not evaluate_conditions(
    [Condition("foreground", "equals", "WorkBuddy")], "all", snap)[0])
check("空闲 ge", evaluate_conditions([Condition("idle_minutes", "ge", "10")], "all", snap)[0])
check("空闲 le 不成立", not evaluate_conditions(
    [Condition("idle_minutes", "le", "5")], "all", snap)[0])
check("锁屏 is 否成立", evaluate_conditions([Condition("locked", "is", "否")], "all", snap)[0])
check("供电 ac", evaluate_conditions([Condition("power", "is", "ac")], "all", snap)[0])
check("电量 le", evaluate_conditions([Condition("power", "le", "50")], "all", snap)[0])

and_conds = [Condition("process_running", "is", "notepad.exe"),
             Condition("idle_minutes", "ge", "10")]
check("AND 全成立", evaluate_conditions(and_conds, "all", snap)[0])
and_bad = and_conds + [Condition("locked", "is", "是")]
check("AND 一条不成立即失败", not evaluate_conditions(and_bad, "all", snap)[0])
check("OR 一条成立即通过", evaluate_conditions(and_bad, "any", snap)[0])
check("条件说明逐条带回来", len(evaluate_conditions(and_bad, "all", snap)[1]) == 3)

desktop = Snapshot(available=True, ac=True, has_battery=False, battery_percent=None)
check("台式机无电池：电量条件不可用", bool(availability(
    Condition("power", "ge", "50"), desktop)))
check("不可用 ≠ 成立", not evaluate_conditions(
    [Condition("power", "ge", "50")], "all", desktop)[0])
check("无电池时供电方式仍可用", evaluate_conditions(
    [Condition("power", "is", "ac")], "all", desktop)[0])
check("非 Windows 全部条件不可用", bool(availability(
    Condition("idle_minutes", "ge", "1"), Snapshot(False, "不是 Windows"))))
check("PcState 可实例化（不读系统）", isinstance(PcState(), PcState))

# ---------------------------------------------------------------------------
section("引擎：冷静期 / 重入 / 条件 / 动作队列 / 日志")
# ---------------------------------------------------------------------------

engine, clock, sched, emitter = make_engine(base_rule())
check("触发被接受", engine.trigger("Ctrl+Alt+F7") == 1)
check("动作已发送", emitter.calls == ["事件A"])
check("冷静期立刻拦截第二次", engine.trigger("Ctrl+Alt+F7") == 0)
check("拦截有日志且不静默", engine.log[-1].outcome == COOLDOWN
      and "剩余" in engine.log[-1].detail)
engine2, clock2, _, emitter2 = make_engine(base_rule())
engine2.trigger("Ctrl+Alt+F7")
check("剩余秒数正确（0.4s）", (clock2.advance(0.6),
      abs(engine2.cooldown_remaining(base_rule()) - 0.4) < 1e-6)[1])
clock2.advance(0.5)
check("过了 1s 可再触发", engine2.trigger("Ctrl+Alt+F7") == 1)
check("cooldown=0 完全不拦", (lambda e: (e.trigger("Ctrl+Alt+F7"),
      e.trigger("Ctrl+Alt+F7")) == (1, 1))(
      make_engine(base_rule(cooldown_ms=0))[0]))
engine_t, _, _, _ = make_engine(base_rule())
engine_t.trigger("Ctrl+Alt+F7")
check("测试绕过冷静期", engine_t.test("r1") is True)
check("测试日志标明绕过", any("绕过冷静期" in x.detail for x in engine_t.log))

# 动作顺序 + 延迟
seq_rule = base_rule(actions=[
    Action("virtual_event", name="A"),
    Action("delay", ms=2000),
    Action("virtual_event", name="B"),
])
engine3, clock3, sched3, emitter3 = make_engine(seq_rule)
engine3.trigger("Ctrl+Alt+F7")
check("延迟前只发了第一个", emitter3.calls == ["A"])
check("延迟回调挂起", len(sched3.pending) == 1)
check("冷却从触发被接受起算（延迟期间已在走）",
      0 < engine3.cooldown_remaining(seq_rule) <= 1.0)
sched3.pump()
check("延迟后按序发第二个", emitter3.calls == ["A", "B"])
check("队列结束有 done 日志", engine3.log[-1].outcome == DONE)
check("延迟 2s 跑完后冷却也已到点", engine3.cooldown_remaining(seq_rule) == 0.0
      and clock3.t >= 102)

# 日志按用户填的那把尺子写：设「30 秒」的人不该在日志里读到 30000 ms
sec_rule = base_rule(actions=[Action("virtual_event", name="A"),
                              Action("delay", ms=30000, unit="s")])
engine_s, _, _, _ = make_engine(sec_rule)
engine_s.trigger("Ctrl+Alt+F7")
check("秒档延迟按秒记日志", any("延迟 30 秒" in x.detail for x in engine_s.log),
      str([x.detail for x in engine_s.log]))
check("毫秒档日志保持毫秒写法",
      any("延迟 2000 ms" in x.detail for x in engine3.log))

# 重入：延迟未跑完时再来一次
engine4, clock4, sched4, emitter4 = make_engine(base_rule(cooldown_ms=0, actions=[
    Action("virtual_event", name="A"),
    Action("delay", ms=5000),
    Action("virtual_event", name="B"),
]))
engine4.trigger("Ctrl+Alt+F7")
check("队列运行中", "r1" in engine4.running)
engine4.trigger("Ctrl+Alt+F7")
check("重入被丢弃且不排队", emitter4.calls == ["A"])
check("重入有日志", engine4.log[-1].outcome == REENTRY)
sched4.pump()
check("只跑完一串", emitter4.calls == ["A", "B"])

# 条件不成立
state = FakeState(snap)
engine5, _, _, emitter5 = make_engine(base_rule(conditions=[
    Condition("process_running", "is", "chrome.exe")]), state=state)
engine5.trigger("Ctrl+Alt+F7")
check("条件不成立不发事件", emitter5.calls == [])
check("条件不成立有日志", engine5.log[-1].outcome == CONDITION)
check("条件求值恰好一次", state.reads == 1)
check("条件不成立不写冷静期", engine5.cooldown_remaining(base_rule()) == 0.0)

# 发送失败
engine6, _, _, emitter6 = make_engine(base_rule(actions=[
    Action("virtual_event", name="A"),
    Action("virtual_event", name="坏事件"),
    Action("virtual_event", name="C"),
]), fail_on="坏事件")
engine6.trigger("Ctrl+Alt+F7")
check("失败即停止后续动作", emitter6.calls == ["A", "坏事件"])
check("失败有日志且释放队列", engine6.log[-1].outcome == FAILED and "r1" not in engine6.running)

# 音箱指令：引擎的第三个出口 say（解析设备 / 清洗文本都不在引擎里）
spk_rule = base_rule(actions=[
    Action("speaker", name="客厅音箱", did="d1", text="打开台灯"),
    Action("virtual_event", name="B"),
])
spk = Speaker()
engine_k, _, _, emitter_k = make_engine(spk_rule, say=spk)
engine_k.trigger("Ctrl+Alt+F7")
check("音箱动作走 say 通道（did / 文本 / 静默）",
      spk.calls == [("d1", "打开台灯", False)], str(spk.calls))
check("音箱成功日志带音箱名与指令",
      any("已让「客厅音箱」执行「打开台灯」" in x.detail for x in engine_k.log),
      str([x.detail for x in engine_k.log]))
check("音箱之后接着跑下一步", emitter_k.calls == ["B"] and engine_k.log[-1].outcome == DONE)

spk_si = Speaker()
engine_si, _, _, _ = make_engine(base_rule(actions=[
    Action("speaker", name="卧室音箱", did="d2", text="小点声", silent=True)]), say=spk_si)
engine_si.trigger("Ctrl+Alt+F7")
check("静默一路传到通道", spk_si.calls == [("d2", "小点声", True)], str(spk_si.calls))
check("静默在日志里写明", any("已静默让" in x.detail for x in engine_si.log),
      str([x.detail for x in engine_si.log]))

engine_f, _, _, emitter_f = make_engine(base_rule(actions=[
    Action("speaker", name="客厅", did="d1", text="说点错的"),
    Action("virtual_event", name="C"),
]), say=Speaker(fail_on="说点错的"))
engine_f.trigger("Ctrl+Alt+F7")
check("音箱失败即停后续动作", emitter_f.calls == [])
check("音箱失败有日志且释放队列", engine_f.log[-1].outcome == FAILED
      and "模拟失败" in engine_f.log[-1].detail and "r1" not in engine_f.running,
      str([x.detail for x in engine_f.log]))

long_text = "把客厅的灯关掉" * 5
spk_l = Speaker()
engine_l, _, _, _ = make_engine(base_rule(actions=[
    Action("speaker", name="客厅", did="d1", text=long_text)]), say=spk_l)
engine_l.trigger("Ctrl+Alt+F7")
check("日志里的长指令打省略号", any("…" in x.detail for x in engine_l.log),
      str([x.detail for x in engine_l.log]))
check("发给音箱的仍是全文", spk_l.calls == [("d1", long_text, False)], str(spk_l.calls))


def _boom(_act, _done) -> None:
    raise RuntimeError("通道炸了")


engine_b, _, _, _ = make_engine(base_rule(actions=[
    Action("speaker", name="客厅", did="d1", text="开灯")]), say=_boom)
engine_b.trigger("Ctrl+Alt+F7")
check("音箱通道抛异常也记失败并释放队列",
      engine_b.log[-1].outcome == FAILED and "r1" not in engine_b.running,
      str([x.detail for x in engine_b.log]))

# 没注入 say：明确失败，**不许静默跳过**（否则界面上看着"成功"其实没出声）
rules_nb = RuleSet(_TMP / "no-speaker.json")
rules_nb.rules = [base_rule(actions=[
    Action("speaker", name="客厅", did="d1", text="开灯")])]
engine_nb = AutomationEngine(rules_nb, state=FakeState(Snapshot(available=True)))
engine_nb.trigger("Ctrl+Alt+F7")
check("没接音箱通道时明确报失败", engine_nb.log[-1].outcome == FAILED
      and "没有配置音箱通道" in engine_nb.log[-1].detail,
      str([x.detail for x in engine_nb.log]))

# 日志
engine7, _, _, _ = make_engine(base_rule())
engine7.trigger("Ctrl+Alt+F7")
check("触发日志有序", [x.outcome for x in engine7.log] == [FIRED, SENT, DONE])
check("日志带规则名和键位", engine7.log[0].rule_name == "测试规则"
      and engine7.log[0].keys == "Ctrl+Alt+F7")
engine7.close()
check("close 后不再接受触发", engine7.trigger("Ctrl+Alt+F7") == 0)
check("close 清空运行队列", not engine7.running)

# ---------------------------------------------------------------------------
section("回收站：软删除 / 还原 / 彻底删除 / 不注册热键")
# ---------------------------------------------------------------------------

tpath = _TMP / "trash.json"
ts = RuleSet(tpath)
ts.add(base_rule(id="t1", keys="Ctrl+Alt+F9"))
ts.add(base_rule(id="t2", name="第二条", keys="F8"))
check("trash 打标记并落盘", ts.trash("t1", at=1000.0)
      and ts.get("t1", include_deleted=True).deleted is True)
check("默认 get 查不到已删规则", ts.get("t1") is None)
check("active 只剩在用的", [r.id for r in ts.active()] == ["t2"])
check("trashed 含被删的", [r.id for r in ts.trashed()] == ["t1"])
check("已删规则不注册热键", ts.enabled_keys() == ["F8"])
check("已删规则不参与触发", ts.rules_for("Ctrl+Alt+F9") == [])
check("删除标记写进 rules.json", '"deleted": true' in tpath.read_text(encoding="utf-8"))

ts2 = RuleSet(tpath)
check("重启后仍在回收站", ts2.get("t1") is None
      and ts2.get("t1", include_deleted=True).deleted is True)
check("deleted_at 读回", ts2.get("t1", include_deleted=True).deleted_at == 1000.0)
check("set_enabled 不碰已删规则", not ts2.set_enabled("t1", False))
check("update 不会让已删规则复活", not ts2.update(base_rule(id="t1", name="复活")))
check("add 不能用回收站里占着的 id", not ts2.add(base_rule(id="t1")))
check("restore 回到列表", ts2.restore("t1") and ts2.get("t1") is not None)
check("还原后热键回来", "Ctrl+Alt+F9" in ts2.enabled_keys())
check("还原后清掉删除标记与时间", ts2.get("t1").deleted is False
      and ts2.get("t1").deleted_at == 0.0)
check("重复 restore 无效", not ts2.restore("t1"))
check("purge 默认拒绝删在用规则", not ts2.purge("t1"))

ts2.trash("t1", at=3000.0)
ts2.trash("t2", at=2000.0)
check("trashed 按删除时间倒序", [r.id for r in ts2.trashed()] == ["t1", "t2"])
check("purge 彻底移除回收站规则", ts2.purge("t2")
      and ts2.get("t2", include_deleted=True) is None)
check("彻底删除后不在文件里", '"t2"' not in tpath.read_text(encoding="utf-8"))

eng_del, _, _, em_del = make_engine(
    base_rule(id="gone", keys="F9", deleted=True, deleted_at=1.0))
check("已删规则不能被热键触发", eng_del.trigger("F9") == 0 and not em_del.calls)
check("已删规则不能被测试发送", eng_del.test("gone") is False)

# ---------------------------------------------------------------------------
section("家庭隔离：列表只看本家庭，热键照旧全局")
# ---------------------------------------------------------------------------

hpath = _TMP / "homes.json"
hs = RuleSet(hpath)
hs.add(base_rule(id="h1", name="1103 的规则", keys="F1", home="1103 家"))
hs.add(base_rule(id="h2", name="共享家庭的规则", keys="F2", home="共享家庭"))
hs.add(base_rule(id="h3", name="还没归属的老规则", keys="F3"))
check("1103 家庭只看到自己那条 + 没归属的",
      {r.id for r in hs.active("1103 家")} == {"h1", "h3"},
      str([r.id for r in hs.active("1103 家")]))
check("共享家庭的规则一条都不漏进 1103",
      all(r.home != "共享家庭" for r in hs.active("1103 家")))
check("切到共享家庭只剩它自己的 + 没归属的",
      {r.id for r in hs.active("共享家庭")} == {"h2", "h3"})
check("不传 home = 不过滤（热键 / 引擎走这条）",
      {r.id for r in hs.active()} == {"h1", "h2", "h3"})
check("热键仍然全部注册（切家庭不该让快捷键失灵）",
      set(hs.enabled_keys()) == {"F1", "F2", "F3"}, str(hs.enabled_keys()))
check("引擎按热键找规则也不看家庭",
      {r.id for r in hs.rules_for("F2")} == {"h2"})

hs.trash("h1")
check("回收站按家庭过滤", {r.id for r in hs.trashed("1103 家")} == {"h1"})
check("别的家庭的回收站看不到它", hs.trashed("共享家庭") == [])
hs.trash("h3")
check("没归属的规则在哪个家的回收站都能看到（还没认领过）",
      {r.id for r in hs.trashed("共享家庭")} == {"h3"},
      str([r.id for r in hs.trashed("共享家庭")]))
hs.restore("h1")
hs.restore("h3")

# claim：升级前写的 rules.json 没有 home，登录后一次性认到当时那个家庭
check("claim 只认领空归属的", hs.claim("1103 家") is True)
check("认领后老规则有了 home", hs.get("h3").home == "1103 家")
check("已经归属别家的一动没动", hs.get("h2").home == "共享家庭")
check("没有可认领的再调返回 False", hs.claim("共享家庭") is False)
check("claim 落了盘", RuleSet(hpath).get("h3").home == "1103 家")
check("认领后它不再出现在别的家庭",
      {r.id for r in hs.active("共享家庭")} == {"h2"},
      str([r.id for r in hs.active("共享家庭")]))

# ---------------------------------------------------------------------------
section("日志：中文结果名 / 筛选查找 / 表格列")
# ---------------------------------------------------------------------------

wall = time.time()
e1 = LogEntry(wall - 3600, "a", "进门播报", "Ctrl+K", FIRED, "热键触发")
e2 = LogEntry(wall - 120, "a", "进门播报", "Ctrl+K", SENT, "已发送虚拟事件「开灯」")
e3 = LogEntry(wall - 60, "b", "离开关灯", "F8", FAILED, "发送失败：云端超时")
e4 = LogEntry(wall - 10, "a", "进门播报", "Ctrl+K", COOLDOWN, "冷静期拦截，剩余 0.6 秒")
entries = [e1, e2, e3, e4]

check("结果名表覆盖全部 outcome", all(o in OUTCOME_LABELS for o in OUTCOME_ORDER))
check("未知 outcome 原样回显", outcome_label("weird") == "weird")
check("默认显示所有规则", len(filter_entries(entries)) == 4)
check("默认最新在最上面", filter_entries(entries)[0] is e4)
check("正序开关可用", filter_entries(entries, newest_first=False)[0] is e1)
check("按规则筛", [x.rule_id for x in filter_entries(entries, rule_id="b")] == ["b"])
check("按结果筛", filter_entries(entries, outcome=FAILED) == [e3])
check("关键字命中说明", filter_entries(entries, keyword="开灯") == [e2])
check("关键字不区分大小写", len(filter_entries(entries, keyword="ctrl+k")) == 3)
check("关键字命中结果名", filter_entries(entries, keyword="冷静期") == [e4])
check("时间范围（最近 5 分钟）", len(filter_entries(entries, since=wall - 300)) == 3)
check("组合筛选取交集", filter_entries(entries, rule_id="a", keyword="冷静期") == [e4])
check("无匹配返回空", filter_entries(entries, keyword="不存在") == [])
check("matches 空条件恒真", all(matches(e) for e in entries))
check("TIME_RANGES 首项 = 全部时间", TIME_RANGES[0] == ("全部时间", 0))
check("WARN_OUTCOMES 只含拦截与失败",
      COOLDOWN in WARN_OUTCOMES and FAILED in WARN_OUTCOMES and FIRED not in WARN_OUTCOMES)
check("三档结果集互斥且覆盖全部 outcome",
      FAIL_OUTCOMES.isdisjoint(NEUTRAL_OUTCOMES)
      and (FAIL_OUTCOMES | NEUTRAL_OUTCOMES) <= set(OUTCOME_LABELS)
      and FAIL_OUTCOMES == {FAILED})
check("outcome_tone：真失败 bad", outcome_tone(FAILED) == "bad")
check("outcome_tone：拦截类 neutral",
      all(outcome_tone(o) == "neutral" for o in (COOLDOWN, REENTRY, CONDITION)))
check("outcome_tone：正常流程 ok",
      all(outcome_tone(o) == "ok" for o in (FIRED, SENT, DONE))
      and outcome_tone("没见过的结果") == "ok")
check("表格五列且含规则名", len(log_columns(e2)) == 5
      and log_columns(e2)[1] == "进门播报" and log_columns(e2)[3] == OUTCOME_LABELS[SENT])
check("全文含时刻 / 规则 / 结果 / 说明",
      all(part in full_text(e3) for part in ("离开关灯", "失败", "云端超时")))
check("date_text 比 time_text 多日期", len(date_text(e1)) > len(e1.time_text))

print(f"\n合计 {N} 项，失败 {len(FAILS)} 项")
for line in FAILS:
    print("  !! " + line)
raise SystemExit(1 if FAILS else 0)
