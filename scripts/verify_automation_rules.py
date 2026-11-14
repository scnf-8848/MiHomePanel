# SPDX-License-Identifier: GPL-3.0-or-later
"""离线验证「电脑自动化 v1」的 core 层：不联网、不注册热键、不碰真实设备。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_automation_rules.py``

覆盖范围：

* ``hotkeys.py`` —— 规范串、Win32 参数（含 ``MOD_NOREPEAT``）、反查、保留键；
* ``rules.py``   —— schema 校验、v4 动作（``device_action`` / ``delay``）的落盘形状
  （无参不写 ``args``、bool 保持 bool、旧 ``virtual_event`` / ``speaker`` 读不回来）、
  坏数据字段级修复、原子落盘、启用键位去重与分发；
* ``pc_state.py``—— 6 类电脑条件、AND / OR、空条件、条件不可用（台式机无电池）；
* ``engine.py``  —— 冷静期（默认 1s / 0=关 / 剩余秒数）、动作队列重入、条件不成立、
  **提交即下一步**（不等回执、各步耗时只有延迟本身）、失败不拦后续动作、
  未知动作跳过、回执晚到只补一条 ``FAILED`` 日志（不动已发出的那条）、
  测试绕过冷静期、日志、close；
* ``logbook.py``—— 独立日志窗的筛选 / 查找（关键字、规则、结果、时间范围）与表格列；
* ``rules.py``   —— 软删除回收站：trash / restore / purge 与「删除后不注册热键」。

时间 / 延迟 / 云端发送全部是假的，所以任何平台都能跑。
"""

from __future__ import annotations

import json
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
    Trigger,
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


class Runner:
    """假的执行通道（引擎唯一的出口 ``run``）：记录跑了哪台设备的哪个功能。

    真实现要解析设备、按名字查坐标、清洗参数、拼 miot 入参
    （``action_catalog`` + ``AutomationService._run``），这里一概不管——引擎本来就不该
    认设备、也不该认功能名。

    ``calls`` 记的是"标签"（事件名 / 文本），沿用从前按事件名断言的那套写法；
    要看完整入参用 ``raw``。
    """

    def __init__(self, fail_on: str = "") -> None:
        self.calls: list[str] = []
        self.raw: list[Action] = []
        self.devices: list[str] = []
        self.fail_on = fail_on

    @staticmethod
    def label(act: Action) -> str:
        return str(act.args.get("text-content") or act.args.get("virtual-param") or "")

    def __call__(self, act: Action, done) -> None:
        label = self.label(act)
        self.raw.append(act)
        self.devices.append(act.did)
        self.calls.append(label)
        # 无参功能（唤醒）的标签是空串，别让「没设 fail_on」被当成命中
        failed = bool(self.fail_on) and label == self.fail_on
        done(f"模拟失败：{label}" if failed else "")


def evt(name: str, hub: str = "hub1", device: str = "客厅中枢") -> Action:
    """一条「发送虚拟事件」动作。"""
    return Action("device_action", did=hub, device=device,
                  action="emit-virtual-event", args={"virtual-param": name})


def speak(text: str, did: str = "d1", device: str = "客厅音箱",
          silent: bool = False) -> Action:
    """一条「执行文本指令」动作（静默时才带那个入参）。"""
    args: dict[str, object] = {"text-content": text}
    if silent:
        args["silent-execution"] = True
    return Action("device_action", did=did, device=device,
                  action="execute-text-directive", args=args)


def base_rule(**kw) -> Rule:
    # 旧用例按单个 keys 写；这里统一折成 triggers（v3 起的数据结构），
    # 调用点不用全改成 Trigger(...)
    keys = kw.pop("keys", "Ctrl+Alt+F7")
    data = dict(
        id="r1", name="测试规则", enabled=True,
        triggers=[Trigger("keyboard", keys)] if keys else [],
        condition_mode="all", conditions=[], cooldown_ms=1000,
        actions=[evt("事件A")],
        # 归属家庭是云端 id；空归属的规则不会运行，引擎用例必须有归属
        home="1001",
    )
    data.update(kw)
    return Rule(**data)


def make_engine(rule: Rule, *, state=None, runner=None, clock=None, sched=None,
                fail_on: str = ""):
    clock = clock or Clock()
    sched = sched or Scheduler(clock)
    runner = runner or Runner(fail_on)
    rules = RuleSet(_TMP / "engine-rules.json")
    rules.rules = [rule]
    engine = AutomationEngine(
        rules, state=state or FakeState(Snapshot(available=True)),
        run=runner, call_later=sched, now=clock.now, wall_now=clock.wall,
    )
    return engine, clock, sched, runner


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
    evt("A"), Action("delay", ms=10**9)]))))
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
          evt("A"), Action("delay", ms=10**9)]))))

# 一条动作 = **设备 + 功能 + 该功能的参数**（11-14 重构）。**绝不存 siid/aiid**：
# 同一能力跨型号方法号完全不同（wake-up 在 lx06 是 aiid 3、在 oh2p 是 1），
# 执行时按 action 名字现查（core/miot_action.py）。
check("动作类型只有两档（device_action / delay）",
      ACTION_TYPES == ("device_action", "delay"), str(ACTION_TYPES))
evt_dict = evt("事件A").to_dict()
check("设备动作落盘 = did/device/action/args",
      evt_dict == {"type": "device_action", "did": "hub1", "device": "客厅中枢",
                   "action": "emit-virtual-event", "args": {"virtual-param": "事件A"}},
      str(evt_dict))
wake = Action("device_action", did="d3", device="书房音箱", action="wake-up")
check("无参功能不写 args 键", "args" not in wake.to_dict(), str(wake.to_dict()))
check("args 里的 bool 写盘再读回来还是 bool",
      Action.from_dict(speak("开灯", silent=True).to_dict()).args.get("silent-execution") is True,
      str(speak("开灯", silent=True).to_dict()))
check("静默不选就不写那个键",
      "silent-execution" not in speak("开灯").to_dict().get("args", {}),
      str(speak("开灯").to_dict()))
evt_back = Action.from_dict(evt_dict)
check("设备动作能原样读回",
      (evt_back.did, evt_back.device, evt_back.action, evt_back.args)
      == ("hub1", "客厅中枢", "emit-virtual-event", {"virtual-param": "事件A"}),
      str(evt_back))
check("旧 virtual_event 动作读不回来",
      Action.from_dict({"type": "virtual_event", "name": "A"}) is None)
check("旧 speaker 动作读不回来",
      Action.from_dict({"type": "speaker", "did": "d", "text": "x"}) is None)
check("认不出的类型也读不回来", Action.from_dict({"type": "play-music"}) is None)
check("没选设备被抓", any("没选设备" in e for e in validate_rule(base_rule(actions=[
    Action("device_action", did="", action="wake-up")]))))
check("功能未知被抓", any("功能未知" in e for e in validate_rule(base_rule(actions=[
    Action("device_action", did="d1", action="play-music")]))))
check("文字为空被抓", any("不能为空" in e for e in validate_rule(base_rule(actions=[
    speak("   ")]))))
check(f"文字超过 {MAX_SPEAK_CHARS} 字被抓",
      any(str(MAX_SPEAK_CHARS) in e for e in validate_rule(base_rule(actions=[
          speak("灯" * (MAX_SPEAK_CHARS + 1))]))))
check("事件名不走音箱的字符白名单（下划线括号能存）",
      validate_rule(base_rule(actions=[evt("my_event(1)")])) == [],
      str(validate_rule(base_rule(actions=[evt("my_event(1)")]))))
check("只有一条设备动作也能过校验（按键 → 让音箱说句话是完整规则）",
      validate_rule(base_rule(actions=[speak("我回来了")])) == [])
check("只有延迟仍然过不了（得有一条真会干事的）",
      any("至少要有一条设备动作" in e for e in validate_rule(base_rule(actions=[
          Action("delay", ms=1000)]))),
      str(validate_rule(base_rule(actions=[Action("delay", ms=1000)]))))
check("发虚拟事件 + 延迟照旧合法",
      validate_rule(base_rule(actions=[evt("A"), Action("delay", ms=1000)])) == [])

# 空档（用户 11-13 第八条）：③ 加出来的一行默认没选类型，type 是空串。
# 这一层必须拦——`Action.to_dict` 会把空类型写成 device_action 落盘，
# 而 `from_dict` 读回来又是一条完整的空动作，等于悄悄换了语义。
check("动作没选类型（空串）被抓，且说「还没选」而不是「未知」",
      any("还没选动作类型" in e for e in validate_rule(base_rule(actions=[
          evt("A"), Action("")]))),
      str(validate_rule(base_rule(actions=[evt("A"), Action("")]))))
check("空类型动作不会被顺手写成 device_action 混过校验",
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
                              actions=[evt("A")])) == [])

# 家庭归属（用户 11-13 第三轮）：归属存**家庭 id**，空归属 = 未指定家庭。
check("home 非空才落盘", base_rule(home="1001").to_dict().get("home") == "1001")
check("没归属时不写 home 键", "home" not in base_rule(home="").to_dict())
check("home 能原样读回",
      Rule.from_dict(base_rule(home="1002").to_dict()).home == "1002")
check("旧数据没 home 键 → 读成空串",
      Rule.from_dict({"id": "old", "name": "老规则"}).home == "")

# 多触发（用户 11-13 第十条）：triggers 是列表，任一命中即执行
multi = base_rule(triggers=[Trigger("keyboard", "Ctrl+Alt+F7"),
                            Trigger("keyboard", "F8")])
check("多触发规则校验通过", validate_rule(multi) == [], str(validate_rule(multi)))
check("triggers 落盘是列表", [t["keys"] for t in multi.to_dict()["triggers"]]
      == ["Ctrl+Alt+F7", "F8"], str(multi.to_dict().get("triggers")))
check("v3 不再写单个 keys 键", "keys" not in multi.to_dict())
check("多触发能原样读回",
      [t.keys for t in Rule.from_dict(multi.to_dict()).triggers] == ["Ctrl+Alt+F7", "F8"])
check("旧文件的单个 keys 不再转换（当前结构只认 triggers）",
      Rule.from_dict({"id": "old", "keys": "ctrl+alt+f9"}).triggers == [])
check("空触发列表被拦",
      any("至少要有一个触发方式" in e
          for e in validate_rule(base_rule(triggers=[]))))
check("触发空档被拦，且和「类型未知」分开说",
      any("还没选触发方式" in e
          for e in validate_rule(base_rule(triggers=[Trigger("", "")]))))
check("真正写错的触发类型仍然是「触发方式未知」",
      any("触发方式未知" in e
          for e in validate_rule(base_rule(triggers=[Trigger("magic", "F7")]))))
check("同一规则里两个相同快捷键被拦",
      any("用了同一个快捷键" in e
          for e in validate_rule(base_rule(triggers=[Trigger("keyboard", "F7"),
                                                     Trigger("keyboard", "f7")]))))
check("触发键位大小写不同也算同一个",
      any("触发 2 与触发 1" in e
          for e in validate_rule(base_rule(triggers=[Trigger("keyboard", "Ctrl+Alt+F7"),
                                                     Trigger("keyboard", "ctrl+alt+f7")]))))

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
# 多触发：一条规则挂两个键，两个都注册、都命中同一条规则
rsm = RuleSet(_TMP / "multi-rules.json")
check("多触发规则能加入", rsm.add(base_rule(
    id="m1", triggers=[Trigger("keyboard", "F7"), Trigger("keyboard", "F8")])))
check("两个触发的键位都注册", rsm.enabled_keys() == ["F7", "F8"], str(rsm.enabled_keys()))
check("任一触发都能命中同一条规则",
      [r.id for r in rsm.rules_for("F8")] == ["m1"])
check("停用后多触发的键位一起释放",
      (rsm.set_enabled("m1", False), rsm.enabled_keys() == [])[1])
check("remove 生效", rs3.remove("b") and rs3.get("b") is None)
check("update 生效", rs3.update(base_rule(id="a", name="改名后")) and rs3.get("a").name == "改名后")

# 坏数据字段级修复
path.write_text('{"version": 1, "rules": ['
                '{"id":"ok","triggers":[{"type":"keyboard","keys":"alt+ctrl+f9"}],'
                '"cooldown_ms":"坏","actions":[{"type":"delay"}]},'
                '{"triggers":[],"name":"没有 id"}, 42]}', encoding="utf-8")
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

class LateRunner(Runner):
    """回执照样收，但**晚一步**：先让队列跑完，再报失败（真实通道就是这样）。

    这是 11-14 改动的核心语义——引擎提交完就走下一步，回执是异步的。
    """

    def __init__(self, fail_on: str = "") -> None:
        super().__init__(fail_on)
        self.late: list[tuple[str, object]] = []

    def __call__(self, act, done) -> None:
        label = self.label(act)
        self.raw.append(act)
        self.devices.append(act.did)
        self.calls.append(label)
        self.late.append((label, done))

    def flush(self) -> None:
        items, self.late = self.late, []
        for label, done in items:
            failed = bool(self.fail_on) and label == self.fail_on
            done(f"回执说没成功：{label}" if failed else "")


engine, clock, sched, runner = make_engine(base_rule())
check("触发被接受", engine.trigger("Ctrl+Alt+F7") == 1)
check("动作已交给通道", runner.calls == ["事件A"], str(runner.calls))
check("did 与功能名原样带进通道",
      runner.devices == ["hub1"] and runner.raw[0].action == "emit-virtual-event",
      f"{runner.devices} {runner.raw[0].action if runner.raw else ''}")
check("冷静期立刻拦截第二次", engine.trigger("Ctrl+Alt+F7") == 0)
check("拦截有日志且不静默", engine.log[-1].outcome == COOLDOWN
      and "剩余" in engine.log[-1].detail)
engine2, clock2, _, _ = make_engine(base_rule())
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
check("测试日志标明绕过", any("绕过冷却期" in x.detail or "绕过冷静期" in x.detail
                              for x in engine_t.log))

# 多触发：按哪个键都会执行，日志要记下实际按下的那个
eng_m, _, _, rn_m = make_engine(base_rule(
    triggers=[Trigger("keyboard", "F7"), Trigger("keyboard", "F8")]))
check("第二个触发也能执行", eng_m.trigger("F8") == 1 and rn_m.calls == ["事件A"])
check("日志记下实际按下的键", eng_m.log[0].keys == "F8", eng_m.log[0].keys)
check("冷静期按规则算：换另一个触发也被同一条规则挡住", eng_m.trigger("F7") == 0)

# 设备动作按动作里存的那台设备发，日志写明是哪台
eng_h, _, _, rn_h = make_engine(base_rule(actions=[
    evt("事件A", hub="hub9", device="书房中枢")]))
check("动作把设备 did 带进通道", eng_h.trigger("Ctrl+Alt+F7") == 1
      and rn_h.devices == ["hub9"], str(rn_h.devices))
check("发送日志写明设备名", any("「书房中枢」" in e.detail for e in eng_h.log),
      str([e.detail for e in eng_h.log]))
check("成功日志写明功能与参数",
      any(e.detail == "发送虚拟事件 → 「书房中枢」（事件A）" for e in eng_h.log),
      str([e.detail for e in eng_h.log]))

# 动作顺序 + 延迟
seq_rule = base_rule(actions=[
    evt("A"),
    Action("delay", ms=2000),
    evt("B"),
])
engine3, clock3, sched3, rn3 = make_engine(seq_rule)
engine3.trigger("Ctrl+Alt+F7")
check("延迟前只发了第一个", rn3.calls == ["A"], str(rn3.calls))
check("延迟回调挂起", len(sched3.pending) == 1)
check("冷却从触发被接受起算（延迟期间已在走）",
      0 < engine3.cooldown_remaining(seq_rule) <= 1.0)
sched3.pump()
check("延迟后按序发第二个", rn3.calls == ["A", "B"], str(rn3.calls))
check("队列结束有 done 日志", engine3.log[-1].outcome == DONE)
check("延迟 2s 跑完后冷却也已到点", engine3.cooldown_remaining(seq_rule) == 0.0
      and clock3.t >= 102)

# 日志按用户填的那把尺子写：设「30 秒」的人不该在日志里读到 30000 ms
sec_rule = base_rule(actions=[evt("A"),
                              Action("delay", ms=30000, unit="s")])
engine_s, _, _, _ = make_engine(sec_rule)
engine_s.trigger("Ctrl+Alt+F7")
check("秒档延迟按秒记日志", any("延迟 30 秒" in x.detail for x in engine_s.log),
      str([x.detail for x in engine_s.log]))
check("毫秒档日志保持毫秒写法",
      any("延迟 2000 ms" in x.detail for x in engine3.log))

# 重入：延迟未跑完时再来一次
engine4, clock4, sched4, rn4 = make_engine(base_rule(cooldown_ms=0, actions=[
    evt("A"),
    Action("delay", ms=5000),
    evt("B"),
]))
engine4.trigger("Ctrl+Alt+F7")
check("队列运行中", "r1" in engine4.running)
engine4.trigger("Ctrl+Alt+F7")
check("重入被丢弃且不排队", rn4.calls == ["A"], str(rn4.calls))
check("重入有日志", engine4.log[-1].outcome == REENTRY)
sched4.pump()
check("只跑完一串", rn4.calls == ["A", "B"], str(rn4.calls))

# 条件不成立
state = FakeState(snap)
engine5, _, _, rn5 = make_engine(base_rule(conditions=[
    Condition("process_running", "is", "chrome.exe")]), state=state)
engine5.trigger("Ctrl+Alt+F7")
check("条件不成立不发事件", rn5.calls == [])
check("条件不成立有日志", engine5.log[-1].outcome == CONDITION)
check("条件求值恰好一次", state.reads == 1)
check("条件不成立不写冷静期", engine5.cooldown_remaining(base_rule()) == 0.0)

# 发送失败：**不拦后续动作**。动作之间是顺序关系、不是条件链（米家也一样）——
# 第 2 步失败，第 3 步照样做；失败只落一条日志 + 系统通知。
engine6, _, _, rn6 = make_engine(base_rule(actions=[
    evt("A"),
    evt("坏事件"),
    evt("C"),
]), fail_on="坏事件")
engine6.trigger("Ctrl+Alt+F7")
check("失败不拦后续动作", rn6.calls == ["A", "坏事件", "C"], str(rn6.calls))
check("失败有日志、队列照常跑完",
      any(x.outcome == FAILED for x in engine6.log) and engine6.log[-1].outcome == DONE,
      str([x.outcome for x in engine6.log]))
check("发送前失败文案写「没发出去」",
      any("（没发出去）" in x.detail for x in engine6.log),
      str([x.detail for x in engine6.log]))

# 回执晚一步才报错：队列早已跑完，失败只补一条日志，不改变已经发出去的东西
engine_r, _, _, late = make_engine(
    base_rule(actions=[evt("A"), evt("坏事件")]), runner=LateRunner("坏事件"))
engine_r.trigger("Ctrl+Alt+F7")
check("提交完队列就收尾（不等回执）", engine_r.log[-1].outcome == DONE,
      str([x.outcome for x in engine_r.log]))
count_sent = len([x for x in engine_r.log if x.outcome == SENT])
late.flush()
check("回执失败补一条 FAILED、写明「回执报错」",
      any(x.outcome == FAILED and "（回执报错）" in x.detail for x in engine_r.log),
      str([x.detail for x in engine_r.log]))
check("回执失败不动已经发出的日志", count_sent == 2,
      str([x.outcome for x in engine_r.log]))

# 文本指令：解析设备 / 清洗文本都不在引擎里，引擎只看功能名与参数摘要
spk_rule = base_rule(actions=[
    speak("打开台灯"),
    evt("B"),
])
spk = Runner()
engine_k, _, _, rn_k = make_engine(spk_rule, runner=spk)
engine_k.trigger("Ctrl+Alt+F7")
check("文本指令的 did / 参数原样进通道",
      rn_k.devices == ["d1", "hub1"] and rn_k.raw[0].args.get("text-content") == "打开台灯",
      str(rn_k.devices))
check("成功日志带设备名与指令",
      any("执行文本指令 → 「客厅音箱」（打开台灯）" in x.detail for x in engine_k.log),
      str([x.detail for x in engine_k.log]))
check("文本指令之后接着跑下一步",
      rn_k.calls == ["打开台灯", "B"] and engine_k.log[-1].outcome == DONE)

spk_si = Runner()
engine_si, _, _, _ = make_engine(base_rule(actions=[
    speak("小点声", did="d2", device="卧室音箱", silent=True)]), runner=spk_si)
engine_si.trigger("Ctrl+Alt+F7")
check("静默一路传到通道",
      spk_si.raw[0].args.get("silent-execution") is True, str(spk_si.raw[0].args))
check("静默在日志里写明",
      any("「卧室音箱」（小点声，静默）" in x.detail for x in engine_si.log),
      str([x.detail for x in engine_si.log]))

engine_f, _, _, rn_f = make_engine(base_rule(actions=[
    speak("说点错的"),
    evt("C"),
]), fail_on="说点错的")
engine_f.trigger("Ctrl+Alt+F7")
check("文本指令失败也不拦后续", rn_f.calls == ["说点错的", "C"], str(rn_f.calls))
check("失败日志写明功能、设备名与原因",
      any(x.outcome == FAILED and "执行文本指令未成功（「客厅音箱」）" in x.detail
          for x in engine_f.log),
      str([x.detail for x in engine_f.log]))

# 无参功能：摘要为空，日志里不该冒出空括号
engine_w, _, _, rn_w = make_engine(base_rule(actions=[
    Action("device_action", did="d3", device="书房音箱", action="wake-up")]))
engine_w.trigger("Ctrl+Alt+F7")
check("唤醒动作没有参数也照常执行",
      rn_w.devices == ["d3"] and engine_w.log[-1].outcome == DONE, str(rn_w.devices))
check("无参功能日志不写空括号",
      any(x.detail == "唤醒 → 「书房音箱」" for x in engine_w.log),
      str([x.detail for x in engine_w.log]))

long_text = "把客厅的灯关掉" * 5
engine_l, _, _, rn_l = make_engine(base_rule(actions=[speak(long_text)]))
engine_l.trigger("Ctrl+Alt+F7")
check("日志里的长指令打省略号", any("…" in x.detail for x in engine_l.log),
      str([x.detail for x in engine_l.log]))
check("交给通道的仍是全文",
      rn_l.raw[0].args.get("text-content") == long_text, str(rn_l.raw[0].args))


def _boom(_act, _done) -> None:
    raise RuntimeError("通道炸了")


engine_b, _, _, _ = make_engine(base_rule(actions=[speak("开灯")]), runner=_boom)
engine_b.trigger("Ctrl+Alt+F7")
check("通道抛异常也记失败，队列照常收尾",
      any(x.outcome == FAILED and "通道炸了" in x.detail for x in engine_b.log)
      and engine_b.log[-1].outcome == DONE,
      str([x.detail for x in engine_b.log]))

# 没注入 run：明确失败，**不许静默跳过**（否则界面上看着"成功"其实什么都没做）
rules_nb = RuleSet(_TMP / "no-channel.json")
rules_nb.rules = [base_rule(actions=[speak("开灯")])]
engine_nb = AutomationEngine(rules_nb, state=FakeState(Snapshot(available=True)))
engine_nb.trigger("Ctrl+Alt+F7")
check("没接执行通道时明确报失败（不静默跳过）",
      any(x.outcome == FAILED and "没有配置执行通道" in x.detail for x in engine_nb.log),
      str([x.detail for x in engine_nb.log]))

# 规则文件被手改、或以后删了某个功能：功能不在白名单 → 报出来，但**不停队列**
engine_u, _, _, rn_u = make_engine(base_rule(actions=[
    Action("device_action", did="d1", device="音箱", action="play-music"),
    evt("B"),
]))
engine_u.trigger("Ctrl+Alt+F7")
check("不在白名单的功能报出来（不停队列）",
      any(x.outcome == FAILED and "未知动作「play-music」，已跳过" in x.detail
          for x in engine_u.log),
      str([x.detail for x in engine_u.log]))
check("未知功能不交给通道", rn_u.calls == ["B"], str(rn_u.calls))
engine_e, _, _, _ = make_engine(base_rule(actions=[Action("")]))
engine_e.trigger("Ctrl+Alt+F7")
check("空类型动作也当未知处理",
      any(x.outcome == FAILED and "未知动作「-」" in x.detail for x in engine_e.log),
      str([x.detail for x in engine_e.log]))

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
section("家庭隔离：归属按 id，未指定家庭可见但不运行")
# ---------------------------------------------------------------------------

hpath = _TMP / "homes.json"
hs = RuleSet(hpath)
hs.add(base_rule(id="h1", name="1001 的规则", keys="F1", home="1001"))
hs.add(base_rule(id="h2", name="1002 的规则", keys="F2", home="1002"))
hs.add(base_rule(id="h3", name="还没选家庭的老规则", keys="F3", home=""))
check("1001 家庭只看到自己那条 + 未指定家庭的",
      {r.id for r in hs.active("1001")} == {"h1", "h3"},
      str([r.id for r in hs.active("1001")]))
check("1002 的规则一条都不漏进 1001",
      all(r.home != "1002" for r in hs.active("1001")))
check("切到 1002 只剩它自己的 + 未指定家庭的",
      {r.id for r in hs.active("1002")} == {"h2", "h3"})
check("不传 home = 不过滤（内部枚举）",
      {r.id for r in hs.active()} == {"h1", "h2", "h3"})
check("未指定家庭的规则不注册热键",
      set(hs.enabled_keys()) == {"F1", "F2"}, str(hs.enabled_keys()))
check("引擎按热键也找不到未指定家庭的规则", hs.rules_for("F3") == [])
check("有归属的规则照旧能触发",
      {r.id for r in hs.rules_for("F2")} == {"h2"})

hs.trash("h1")
check("回收站按家庭 id 过滤", {r.id for r in hs.trashed("1001")} == {"h1"})
check("别的家庭的回收站看不到它", hs.trashed("1002") == [])
hs.trash("h3")
check("未指定家庭的规则在哪个家的回收站都能看到（方便找回）",
      {r.id for r in hs.trashed("1002")} == {"h3"},
      str([r.id for r in hs.trashed("1002")]))
hs.restore("h1")
hs.restore("h3")

# 旧结构不再转换：本机 rules.json 已规范成 v3，加载只按当前字段读
legacy = _TMP / "legacy-rules.json"
legacy.write_text(json.dumps({"version": 1, "rules": [{
    "id": "old1", "name": "老规则", "enabled": False, "keys": "F11",
    "condition_mode": "any",
    "conditions": [{"type": "idle_minutes", "op": "ge", "arg": "5"}],
    "actions": [{"type": "delay", "ms": 300}],
    "cooldown_ms": 2000, "home": "共享家庭",
}]}, ensure_ascii=False), encoding="utf-8")
old = RuleSet(legacy).get("old1", include_deleted=True)
check("旧文件的 keys 不再变成触发（数据已规范化，不兼容旧字段）",
      old is not None and old.triggers == [])
check("旧文件其余字段照常读入",
      old is not None and (old.name, old.enabled, old.condition_mode,
                           old.cooldown_ms) == ("老规则", False, "any", 2000)
      and [c.type for c in old.conditions] == ["idle_minutes"]
      and [a.type for a in old.actions] == ["delay"])
check("没有触发的旧规则不注册热键", RuleSet(legacy).enabled_keys() == [])

# v2 的 home 已经是 id：加载时不再按版本清空
v2 = _TMP / "v2-rules.json"
v2.write_text(json.dumps({"version": 2, "rules": [{
    "id": "v2r", "name": "v2 规则", "keys": "Ctrl+Alt+F6", "home": "1002",
    "actions": [{"type": "virtual_event", "name": "A"}],
}]}, ensure_ascii=False), encoding="utf-8")
v2r = RuleSet(v2).get("v2r", include_deleted=True)
check("v2 的家庭 id 原样保留", v2r is not None and v2r.home == "1002",
      repr(v2r.home if v2r else None))
check("v2 的 keys 不再自动升成触发", v2r is not None and v2r.triggers == [])

# 空归属规则直接进引擎也不会被触发（不能静默按某个家庭执行）
eng_empty, _, _, em_empty = make_engine(base_rule(id="e1", keys="F12", home=""))
check("空归属规则热键触发被拒", eng_empty.trigger("F12") == 0 and not em_empty.calls)

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
