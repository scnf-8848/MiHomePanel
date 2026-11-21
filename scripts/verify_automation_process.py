# SPDX-License-Identifier: GPL-3.0-or-later
"""验证自动化①的「某程序 启动 / 退出」触发：边沿检测与生命周期。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_automation_process.py``

这里不真的开关进程：把 ``AutomationService.pc_state`` 换成假快照，直接调
``_poll_processes()`` 模拟"一次采样结果回来了"。要钉住的是**边沿**而不是状态——
进程本来开着不算"启动"，读不到进程表的那一轮什么都不判，也不把基线冲掉。

2026-11-19 后采样在 worker 线程，本脚本额外钉住两件事：
① 主线程不再读进程表（``_sync_process_watch`` 只发请求不自己采样）；
② worker 的 ``sampled`` 信号确实跨线程把结果送到主线程判定。

同日追加「不重复触发」语义：``process_names()`` 出口就把 pid 丢掉、只留名字，
边沿基线是布尔存在性而非实例计数——开多个记事本、同名进程整批替换、同一规则
挂两条等价触发，都不该产生额外边沿。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_TMP = Path(tempfile.mkdtemp(prefix="mihome-auto-proc-"))
os.environ["MIHOME_DATA_DIR"] = str(_TMP / "data")

from PySide6.QtCore import QThread, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from mihome.core.automation.pc_state import Snapshot, process_in  # noqa: E402
from mihome.core.automation.rules import Action, Rule, Trigger  # noqa: E402
from mihome.core.automation.service import PROCESS_POLL_MS, AutomationService  # noqa: E402

app = QApplication.instance() or QApplication([])
FAILS: list[str] = []
N = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global N
    N += 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(f"{name}  {detail}")


class FakeClient:
    """只给 ``Channel(client.raw())`` 构造时读的那几个字段。"""

    def __init__(self) -> None:
        self.last_error = ""

    def raw(self):
        return SimpleNamespace(
            auth_data={"ssecurity": "0" * 32},
            session=SimpleNamespace(headers={}),
            api_base_url="https://api.io.mi.com/app",
        )


class FakeWorker:
    """动作不真发：本脚本只关心触发那一行日志。"""

    def submit(self, fn, *, callback=None, priority=0, label="") -> None:
        pass


class FakePc:
    """一个可改写的进程表；``names=None`` 表示读不到（非 Windows / API 失败）。"""

    def __init__(self, names: set[str] | None) -> None:
        self.names = names
        self.call_threads: list[QThread] = []

    def process_names(self) -> set[str] | None:
        self.call_threads.append(QThread.currentThread())
        return None if self.names is None else set(self.names)

    def read(self) -> Snapshot:
        return Snapshot(available=True, processes=frozenset(self.names or ()))

    def process_elapsed_minutes(self, name: str) -> float | None:
        return None


class PidTablePc(FakePc):
    """按名字存 pid 列表的底层快照，复刻 ``process_names()`` 的出口口径。

    真实 ``pc_state._pids_by_name()`` 采到了每个名字的 pid 列表，但
    :func:`process_names` 返回的是 ``set(pids)``——**pid 在这一步被丢掉**，边沿
    只看"名字在不在"。这里照搬同一形状，才能验证"开多个记事本不会重复触发"，
    而不是把假快照自己先去重、让断言恒真。
    """

    def __init__(self, pids: dict[str, list[int]] | None) -> None:
        super().__init__(set(pids) if pids is not None else None)
        self.pids = pids

    def process_names(self) -> set[str] | None:
        self.call_threads.append(QThread.currentThread())
        return set(self.pids) if self.pids is not None else None

    def read(self) -> Snapshot:
        return Snapshot(available=True, processes=frozenset(self.pids or ()))


def new_service(name: str) -> AutomationService:
    return AutomationService(
        store=SimpleNamespace(devices={}, active_home="10000001",
                              schedule_confirm=lambda: None),
        client=FakeClient(), worker=FakeWorker(), window=None,
        path=_TMP / f"{name}.json", transport="serial",
        log_path=_TMP / f"{name}-log.jsonl",
    )


def work() -> Action:
    return Action("device_action", did="1000000001", device="示例-音箱", action="wake-up")


def proc_rule(rid: str, name: str, arg: str, op: str) -> Rule:
    return Rule(id=rid, name=name, enabled=True, home="10000001",
                triggers=[Trigger("process", "", arg=arg, op=op)],
                actions=[work()], cooldown_ms=0)


def wait_baseline(service: AutomationService, *, timeout_ms: int = 3000) -> None:
    """等 worker 的「规则刚改完立即采样」回来建好基线，再手动推进边沿。"""
    for _ in range(timeout_ms // 10):
        app.processEvents()
        if all(v is not None for v in service._process_seen.values()):
            return
        QThread.msleep(10)


svc = new_service("proc")
pc = FakePc({"notepad.exe"})
svc.pc_state = pc
svc.rules.add(proc_rule("p1", "记事本启动", "notepad.exe", "start"))

check("轮询间隔 2 秒（用户 11-19 拍板，能接受漏 2s 内短命进程）",
      PROCESS_POLL_MS == 2000)
check("worker 线程起来了，只盯规则里那个名字",
      svc._process_thread.isRunning()
      and svc._sampler.thread() is svc._process_thread
      and set(svc._process_seen) == {"notepad.exe"},
      str((svc._process_thread.isRunning(), svc._process_seen)))

# 1. 保存规则时进程已经开着：建基线，不算一次"启动"
svc._poll_processes()
check("已运行的进程不触发启动（首次只建基线）", svc.engine.log == [], str(svc.engine.log))

# 2. 消失 → 再出现：这才有启动边沿
pc.names = set()
svc._poll_processes()
check("进程消失不会触发只关心启动的规则", svc.engine.log == [])
pc.names = {"notepad.exe"}
svc._poll_processes()
# 一次触发会写「fired + 每个动作一步」多行，判触发只看 fired 那行
fired = [e for e in svc.engine.log if e.outcome == "fired"]
check("从无到有触发一次", len(fired) == 1, str(svc.engine.log))
check("日志说明带程序名和事件",
      "进程 notepad.exe 启动触发" in fired[0].detail, fired[0].detail)

# 3. 读不到进程表：整轮不判，基线也不许被冲成"没在运行"
svc.engine.clear_log()
pc.names = None
svc._poll_processes()
check("读不到时不触发", svc.engine.log == [])
check("读不到时基线保持原值", svc._process_seen["notepad.exe"] is True,
      str(svc._process_seen))
pc.names = set()
svc._poll_processes()
check("恢复读取后按旧基线判，不把'刚读到'当成启动", svc.engine.log == [])

# 4. 再加一条退出规则：大小写 / 带不带 .exe 两种写法都认
svc.rules.add(proc_rule("p2", "记事本退出", "NOTEPAD", "exit"))
check("两种写法各建一份基线（互不干扰）",
      set(svc._process_seen) == {"notepad.exe", "notepad"}, str(svc._process_seen))
pc.names = {"notepad.exe"}
svc._poll_processes()
check("从无到有只叫启动那条",
      [e.rule_id for e in svc.engine.log if e.outcome == "fired"] == ["p1"],
      str([(e.rule_id, e.detail) for e in svc.engine.log]))
svc.engine.clear_log()
pc.names = set()
svc._poll_processes()
check("从有到无只叫退出那条",
      [e.rule_id for e in svc.engine.log if e.outcome == "fired"] == ["p2"],
      str([(e.rule_id, e.detail) for e in svc.engine.log]))
check("退出日志写的是退出", "退出触发" in svc.engine.log[0].detail,
      svc.engine.log[0].detail)

# 5. 编辑中的规则不执行（与「本程序启动时」同一口径）
svc.engine.clear_log()
svc.suspend_hotkeys("p2")
pc.names = {"notepad.exe"}
svc._poll_processes()
check("编辑中的规则不执行",
      all(e.rule_id != "p2" for e in svc.engine.log if e.outcome == "fired"),
      str([(e.rule_id, e.detail) for e in svc.engine.log]))
svc.resume_hotkeys("p2")

# 6. 规则删光：名单清空、定时器停；close 也不留句柄
svc.rules.trash("p1")
svc.rules.trash("p2")
check("规则删光后名单清空、worker 停",
      not svc._process_seen and not svc._process_thread.isRunning(),
      str(svc._process_seen))
svc.close()

# 7. 跨线程采样：主线程不读进程表，worker 采完由信号把结果送回来建基线
svc2 = new_service("proc-thread")
pc2 = FakePc({"notepad.exe"})
svc2.pc_state = pc2
svc2.rules.add(proc_rule("t1", "线程验证", "notepad.exe", "start"))
main_thread = QThread.currentThread()
check("新增规则时主线程不采样（基线先留 None）",
      svc2._process_seen.get("notepad.exe") is None
      and not any(t is main_thread for t in pc2.call_threads),
      str((svc2._process_seen, [t.objectName() for t in pc2.call_threads])))
for _ in range(300):
    app.processEvents()
    if svc2._process_seen.get("notepad.exe") is not None:
        break
    QThread.msleep(10)
check("worker 采样后由信号回主线程建基线",
      svc2._process_seen.get("notepad.exe") is True, str(svc2._process_seen))
check("进程表只在 worker 线程读，主线程一次都不读",
      bool(pc2.call_threads)
      and all(t is svc2._process_thread for t in pc2.call_threads),
      str([t.objectName() for t in pc2.call_threads]))
svc2.close()

# 8. 多实例：pid 列表在 process_names() 出口折叠成名字，开多关少都不产生新边沿
svc3 = new_service("proc-multi")
pc3 = PidTablePc({"notepad.exe": [100, 101, 102]})
svc3.pc_state = pc3
svc3.rules.add(proc_rule("m1", "记事本启动", "notepad.exe", "start"))
svc3.rules.add(proc_rule("m2", "记事本退出", "notepad.exe", "exit"))
wait_baseline(svc3)
check("3 个同名进程只建一份基线（pid 在出口去重）",
      svc3._process_seen == {"notepad.exe": True}, str(svc3._process_seen))
svc3.engine.clear_log()
pc3.pids = {"notepad.exe": [100]}
svc3._poll_processes()
check("同名进程 3→1 不触发退出（只看名字在不在）",
      svc3.engine.log == [], str(svc3.engine.log))
pc3.pids = {"notepad.exe": [100, 101, 102, 103, 104]}
svc3._poll_processes()
check("同名进程 1→5 不触发启动（只看名字在不在）",
      svc3.engine.log == [], str(svc3.engine.log))
pc3.pids = {"notepad.exe": [200, 201, 202, 203, 204]}
svc3._poll_processes()
check("同名进程整批替换不触发（pid 不参与边沿）",
      svc3.engine.log == [], str(svc3.engine.log))
pc3.pids = {}
svc3._poll_processes()
fired3 = [e for e in svc3.engine.log if e.outcome == "fired"]
check("关到 0 只触发一次退出",
      [e.rule_id for e in fired3] == ["m2"], str(svc3.engine.log))
svc3.engine.clear_log()
pc3.pids = {"notepad.exe": [300, 301, 302, 303]}
svc3._poll_processes()
fired3 = [e for e in svc3.engine.log if e.outcome == "fired"]
check("从 0 一次开 4 个只触发一次启动",
      [e.rule_id for e in fired3] == ["m1"], str(svc3.engine.log))
svc3.close()

# 9. 同一条规则挂两条等价触发（notepad.exe / NOTEPAD）：OR 语义只该跑一次
svc4 = new_service("proc-duptrig")
pc4 = FakePc(set())
svc4.pc_state = pc4
svc4.rules.add(Rule(
    id="d1", name="重复触发行", enabled=True, home="10000001",
    triggers=[Trigger("process", "", arg="notepad.exe", op="start"),
              Trigger("process", "", arg="NOTEPAD", op="start")],
    actions=[work()], cooldown_ms=0,
))
wait_baseline(svc4)
check("两种写法各建一份基线", set(svc4._process_seen) == {"notepad.exe", "notepad"},
      str(svc4._process_seen))
pc4.names = set()
svc4._poll_processes()
svc4.engine.clear_log()
pc4.names = {"notepad.exe"}
svc4._poll_processes()
fired4 = [e for e in svc4.engine.log if e.outcome == "fired"]
check("同一规则两条等价启动触发只执行一次",
      [e.rule_id for e in fired4] == ["d1"], str(svc4.engine.log))
svc4.close()

# 10. 同一规则挂两个不同进程：本轮同时命中也只该跑一次（OR 语义）
svc6 = new_service("proc-two-procs")
pc6 = FakePc(set())
svc6.pc_state = pc6
svc6.rules.add(Rule(
    id="o1", name="两个进程", enabled=True, home="10000001",
    triggers=[Trigger("process", "", arg="notepad.exe", op="start"),
              Trigger("process", "", arg="calc.exe", op="start")],
    actions=[work()], cooldown_ms=0,
))
wait_baseline(svc6)
check("两个进程各建一份基线", set(svc6._process_seen) == {"notepad.exe", "calc.exe"},
      str(svc6._process_seen))
pc6.names = {"notepad.exe", "calc.exe"}
svc6._poll_processes()
fired6 = [e for e in svc6.engine.log if e.outcome == "fired"]
check("同一规则两个进程同一轮都从无到有，只执行一次",
      [e.rule_id for e in fired6] == ["o1"], str(svc6.engine.log))
svc6.close()

# 11. 保存第二条同名规则：基线不重置；真有边沿时两条各跑一次
svc5 = new_service("proc-samerule")
pc5 = FakePc({"notepad.exe"})
svc5.pc_state = pc5
svc5.rules.add(proc_rule("s1", "第一条", "notepad.exe", "start"))
wait_baseline(svc5)
baseline5 = dict(svc5._process_seen)
svc5.engine.clear_log()
svc5.rules.add(proc_rule("s2", "第二条同名", "notepad.exe", "start"))
check("保存第二条同名规则不重置基线",
      svc5._process_seen == baseline5, str((baseline5, svc5._process_seen)))
app.processEvents()
svc5._poll_processes()
check("同名规则增减不产生假启动",
      [e for e in svc5.engine.log if e.outcome == "fired"] == [],
      str(svc5.engine.log))
pc5.names = set()
svc5._poll_processes()
svc5.engine.clear_log()
pc5.names = {"notepad.exe"}
svc5._poll_processes()
fired5 = [e for e in svc5.engine.log if e.outcome == "fired"]
check("两条同名规则各触发一次（各自配的动作，不是机制重复）",
      sorted(e.rule_id for e in fired5) == ["s1", "s2"], str(svc5.engine.log))
svc5.close()

# 12. 输入框里粘的是"完整路径"（任务管理器 / 资源管理器复制出来的样子）
live = {"notepad.exe"}
check("粘完整路径也认", process_in(live, r"C:\Windows\System32\notepad.exe"))
check("带引号的整串也认（「复制文件地址」那种）",
      process_in(live, r'"C:\Windows\System32\notepad.exe"'))
check("正斜杠、不带 .exe 也认", process_in(live, "C:/Windows/notepad"))
check("空名字不匹配任何进程", not process_in(live, "   "))

# 13. worker 反复启停（规则删光 → quit，再加回来 → started）：定时器不累积
svc7 = new_service("proc-restart")
svc7.pc_state = FakePc({"notepad.exe"})
counts: list[int] = []
for i in range(3):
    svc7.rules.add(proc_rule(f"q{i}", "启停验证", "notepad.exe", "start"))
    for _ in range(200):
        app.processEvents()
        if svc7._sampler.findChildren(QTimer):
            break
        QThread.msleep(10)
    for _ in range(30):                    # 给 deleteLater 一点时间落地
        app.processEvents()
        QThread.msleep(10)
    counts.append(len(svc7._sampler.findChildren(QTimer)))
    svc7.rules.trash(f"q{i}")
    for _ in range(200):
        app.processEvents()
        if not svc7._process_thread.isRunning():
            break
        QThread.msleep(10)
check("反复启停后始终只有一个采样定时器（旧的被停掉并删除）",
      counts == [1, 1, 1], str(counts))
svc7.close()

print(f"\n合计 {N} 项，失败 {len(FAILS)} 项")
for line in FAILS:
    print("  !! " + line)
raise SystemExit(1 if FAILS else 0)
