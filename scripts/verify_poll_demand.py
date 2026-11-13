# SPDX-License-Identifier: GPL-3.0-or-later
"""验证轮询需求登记（`mihome/core/poll_demand.py`）。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_poll_demand.py``

这一层是纯逻辑（不依赖 Qt），所以直接跑真对象。验的是四类事：

* **取最严**：多条登记里间隔最小的那条说了算；
* **不互相重置**：结果没变就不回调（否则挂件要的 1 秒会被窗口切后台那一下重置）；
* **有下限**：谁报 100 ms 都会被收敛到 0.5 秒，这是"保守设计"的兜底；
* **能回落**：撤掉最严的那条之后回到次严 / 默认 / 停表；
* **写入确认**：回包后 mute+0.2 秒起读、每台只保留一个序列、最多 4 台、动作 1.0 秒，
  且确认读不进健康账本；
* **不落盘**：需求表只活在内存里，崩溃重启仍从常量起（prefs 里没有任何轮询键）。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MIHOME_NO_TRAY", "1")
# 真窗口那组会建 Prefs / Store，别落到真实用户目录
_TMP = Path(tempfile.mkdtemp(prefix="mihome-poll-"))
os.environ["MIHOME_DATA_DIR"] = str(_TMP / "data")

from mihome.core.poll_demand import MIN_INTERVAL_MS, PollDemandBoard  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(
        f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}",
        flush=True,
    )
    if not ok:
        FAILS.append(f"{name} {detail}")


def _record(board: PollDemandBoard) -> list[int | None]:
    hits: list[int | None] = []
    board.subscribe(hits.append)
    return hits


def _basics() -> None:
    # -- 没人登记 ---------------------------------------------------------
    attr = PollDemandBoard("属性轮询", idle_ms=3000)
    check("空表回落到 idle", attr.interval_ms == 3000, str(attr.interval_ms))
    check("空表 holder 为 None", attr.holder() is None)

    ev = PollDemandBoard("事件轮询", idle_ms=None)
    check("事件表默认不轮询", ev.interval_ms is None, str(ev.interval_ms))
    check("不轮询时 describe 说清", ev.describe() == "事件轮询：未轮询", ev.describe())

    # -- 单条 -------------------------------------------------------------
    hits = _record(attr)
    attr.set("window", 3000, "窗口可见")
    check("登记后生效", attr.interval_ms == 3000)
    check("首次登记就回调一次", hits == [3000], str(hits))
    who = attr.holder()
    check("holder 指到登记人", who is not None and who.owner == "window")
    check("describe 带原因", "窗口可见" in attr.describe(), attr.describe())

    # -- 同值重复登记不回调 -----------------------------------------------
    attr.set("window", 3000, "窗口可见")
    check("同值重复登记不回调（不重置定时器）", len(hits) == 1, str(hits))

    # -- 多条取最严 -------------------------------------------------------
    attr.set("widget", 1000, "桌面挂件")
    check("取最严（1000）", attr.interval_ms == 1000, str(attr.interval_ms))
    check("变严会回调", hits[-1] == 1000, str(hits))
    check(
        "demands 按间隔升序",
        [d.owner for d in attr.demands()] == ["widget", "window"],
        str([d.owner for d in attr.demands()]),
    )
    check("holder 是最严那条", attr.holder().owner == "widget")

    # 窗口切后台（3000 → 15000），最严仍是挂件 1000：结果不变，不该回调
    before = len(hits)
    attr.set("window", 15000, "窗口在托盘 / 最小化")
    check("切后台不改变结果时不回调", len(hits) == before, str(hits))
    check("切后台后仍是 1000", attr.interval_ms == 1000, str(attr.interval_ms))

    # 撤掉挂件 → 回落到窗口的 15000
    attr.clear("widget")
    check("撤掉最严后回落到次严", attr.interval_ms == 15000, str(attr.interval_ms))
    check("回落会回调", hits[-1] == 15000, str(hits))

    # 窗口回到前台
    attr.set("window", 3000, "窗口可见")
    check("回前台变回 3000", attr.interval_ms == 3000 and hits[-1] == 3000, str(hits))

    # -- 全部撤掉 → 回落到 idle ------------------------------------------
    attr.clear("window")
    check("全撤后回落到 idle", attr.interval_ms == 3000, str(attr.interval_ms))


def _floor() -> None:
    b = PollDemandBoard("属性轮询", idle_ms=3000)
    hits = _record(b)
    b.set("rude", 100, "报得太快")
    check(
        f"100 ms 被收敛到 {MIN_INTERVAL_MS}",
        b.interval_ms == MIN_INTERVAL_MS,
        str(b.interval_ms),
    )
    check("收敛后登记值也是收敛值", b.holder().interval_ms == MIN_INTERVAL_MS)
    check("收敛后仍会回调", hits == [MIN_INTERVAL_MS], str(hits))

    b.set("rude", 0, "手滑")
    check("0 也被挡住", b.interval_ms == MIN_INTERVAL_MS, str(b.interval_ms))

    # floor 可以单独指定（比如给某个低频通道放宽）
    slow = PollDemandBoard("拓扑", idle_ms=300_000, floor_ms=60_000)
    slow.set("x", 5000)
    check("自定义 floor 生效", slow.interval_ms == 60_000, str(slow.interval_ms))


def _none_and_subscribe() -> None:
    b = PollDemandBoard("事件轮询", idle_ms=None)
    hits = _record(b)
    b.set("automation", 3000, "自动化规则")
    check("事件表登记后开始轮询", b.interval_ms == 3000 and hits == [3000], str(hits))
    b.clear("automation")
    check("事件表清空后停表", b.interval_ms is None and hits[-1] is None, str(hits))

    # set(None) 等价于 clear
    b.set("automation", 3000)
    b.set("automation", None)
    check("set(None) 等价于 clear", b.interval_ms is None, str(b.interval_ms))

    # 撤销订阅之后不该再收到
    calls: list[int | None] = []
    b.subscribe(calls.append)
    b.subscribe(calls.append)  # 重复订阅只算一次
    b.set("a", 2000)
    check("重复订阅不重复回调", calls == [2000], str(calls))
    b.unsubscribe(calls.append)
    b.set("a", 4000)
    check("退订后不再回调", calls == [2000], str(calls))

    # clear 不存在的 owner 不炸、不回调
    n = len(hits)
    b.clear("ghost")
    check("撤销不存在的登记是空操作", len(hits) == n, f"{n} 次回调不变")


def _describe() -> None:
    b = PollDemandBoard("属性轮询", idle_ms=3000)
    b.set("window", 3000, "窗口可见")
    b.set("widget", 1000, "桌面挂件")
    text = b.describe()
    check("describe 报最严那条", "1000 ms" in text and "桌面挂件" in text, text)
    check("describe 提到还有更宽的需求", "另有 1 条" in text, text)

    b2 = PollDemandBoard("属性轮询", idle_ms=3000)
    check("describe 默认档", "默认" in b2.describe(), b2.describe())


def _confirm() -> None:
    """写入确认：回包后等 mute 到期，再对那台设备定向读，最多 5 次。

    对应 ``MainWindow._start_confirm``：同一台设备只保留一个序列、重新计时；
    同时最多跟踪 4 台，第 5 台挤掉最早的那台；动作没有 mute，用固定起点。
    """
    from PySide6.QtWidgets import QApplication  # noqa: PLC0415

    from mihome.ui import main_window as mw  # noqa: PLC0415
    from mihome.ui import theme  # noqa: PLC0415

    check("确认参数：mute 后 +200ms、间隔 500ms、5 次、最多 4 台",
          (mw.CONFIRM_DELAY_MS, mw.CONFIRM_INTERVAL_MS, mw.CONFIRM_MAX_TRIES,
           mw.CONFIRM_MAX_DEVICES) == (200, 500, 5, 4),
          str((mw.CONFIRM_DELAY_MS, mw.CONFIRM_INTERVAL_MS,
               mw.CONFIRM_MAX_TRIES, mw.CONFIRM_MAX_DEVICES)))
    check("动作的确认起点是 1.0 秒", mw.CONFIRM_ACTION_DELAY_MS == 1000,
          str(mw.CONFIRM_ACTION_DELAY_MS))
    check("burst 相关常量已经删掉",
          not any(hasattr(mw, n) for n in ("BURST_MS", "BURST_HOLD_MS")), "")

    QApplication.instance() or QApplication([])
    win = mw.MainWindow(theme.DARK, tray_available=False)
    win._save_settings = lambda: None  # type: ignore[method-assign]  # noqa: SLF001
    try:
        check("确认读的入口存在", hasattr(win.store, "confirm_read"), "")
        check("reraise 已经删除（没有死代码）",
              not hasattr(win.store, "reraise"), "")
        check("写入成功的回调挂在 on_write_done",
              win.store.on_write_done == win._start_confirm, "")  # noqa: SLF001

        # 走一遍**真实启动回调**：Store 加载完成后 ``MainWindow._on_loaded`` 必须能跑完。
        # 这层平时只由 jobs.drain 从主线程事件循环里调，脚本只建窗口不会触发——
        # 2026-11-13 删除 adopt_home 后这里残留的 ``self._adopt_rules()`` 就是这么漏掉的。
        polls: list[int] = []
        win.store.poll = lambda *a, **k: polls.append(1)
        detail = ""
        try:
            win._on_loaded()
            ok = True
        except Exception as exc:  # noqa: BLE001
            ok = False
            detail = f"{type(exc).__name__}: {exc}"
        check("真实启动路径 _on_loaded 能跑完（无残留方法调用）", ok, detail)
        check("_on_loaded 末尾仍会发起轮询", len(polls) == 1, str(len(polls)))

        # mute 3.0 → 3.2 秒后开始
        win._start_confirm("did-a", {(2, 1): True}, 3.0)  # noqa: SLF001
        left = win._confirm_timers["did-a"].remainingTime()  # noqa: SLF001
        check("mute 3.0 → 3.2 秒起读", 3100 <= left <= 3200, str(left))

        # 同一台再写一次：重新计时、不叠加
        win._start_confirm("did-a", {(2, 2): 25}, 2.0)  # noqa: SLF001
        left2 = win._confirm_timers["did-a"].remainingTime()  # noqa: SLF001
        plan = win._confirm_plans["did-a"]  # noqa: SLF001
        check("同设备重新计时（2.0+0.2）", 2100 <= left2 <= 2200, str(left2))
        check("同一台只有一个序列、次数重置为 5", plan["left"] == 5, str(plan))
        check("期望值按 key 合并",
              plan["expected"] == {(2, 1): True, (2, 2): 25}, str(plan["expected"]))

        # 4 台上限：第 5 台挤掉最早的
        for i in range(3):
            win._start_confirm(f"did-{i}", {(2, 1): i}, 3.0)  # noqa: SLF001
        check("现在跟踪 4 台", len(win._confirm_plans) == 4,
              str(list(win._confirm_plans)))
        check("最早的是 did-a", "did-a" in win._confirm_plans, str(list(win._confirm_plans)))
        win._start_confirm("did-new", {(2, 1): 1}, 3.0)  # noqa: SLF001
        check("第 5 台挤掉最早的那台",
              "did-a" not in win._confirm_plans and "did-new" in win._confirm_plans,
              str(list(win._confirm_plans)))
        check("定时器一起清掉", "did-a" not in win._confirm_timers, "")

        # 动作：没有 mute，1.0 秒起
        win._start_confirm("did-act", {}, None)  # noqa: SLF001
        act = win._confirm_timers["did-act"].remainingTime()  # noqa: SLF001
        check("动作 1.0 秒起读", 900 <= act <= 1000, str(act))

        # 确认读不进健康账本：失败不加 streak、不清 streak
        st = win.store
        st._fail_streak = 2  # noqa: SLF001
        st._next_poll_at = 123.0  # noqa: SLF001
        calls = {}

        def fake_submit(fn, args=(), kwargs=None, priority=0, label="", callback=None):
            calls["label"] = label
            callback(None, RuntimeError("boom"))

        st.worker.submit = fake_submit  # type: ignore[method-assign]
        got = []
        st.confirm_read("did-x", [], lambda res, err: got.append((res, err)))
        check("确认读走 confirm 标签", calls.get("label") == "confirm", str(calls))
        check("失败回调拿到了错误", bool(got) and got[0][0] is None, str(got))
        check("确认失败不改 streak", st._fail_streak == 2, str(st._fail_streak))  # noqa: SLF001
        check("确认失败不改退避时刻", st._next_poll_at == 123.0, str(st._next_poll_at))  # noqa: SLF001

        # 最后一轮改全量读（覆盖联动改到的设备） -------------------------------
        win._start_confirm("did-tail", {(2, 1): True}, 3.0)  # noqa: SLF001
        win._confirm_plans["did-tail"]["left"] = 1  # noqa: SLF001
        full_calls: list[str] = []
        st.confirm_full = lambda done: full_calls.append("full")  # type: ignore[method-assign]
        win._confirm_tick("did-tail")  # noqa: SLF001
        check("第 5 次走全量读", full_calls == ["full"], str(full_calls))
        win._drop_confirm("did-tail")  # noqa: SLF001

        # 场景 / 音箱指令：1 / 2.5 / 4 秒，固定 3 次，不做早停 ------------------
        check("全量序列时点 1 / 2.5 / 4 秒",
              mw.CONFIRM_FULL_AT_MS == (1000, 2500, 4000), str(mw.CONFIRM_FULL_AT_MS))
        check("on_command_done 接线到全量序列",
              st.on_command_done == win._start_command_confirm, "")  # noqa: SLF001
        win._start_command_confirm()  # noqa: SLF001
        check("起点 1 秒", 900 <= win._cmd_timer.remainingTime() <= 1000,  # noqa: SLF001
              str(win._cmd_timer.remainingTime()))  # noqa: SLF001

        steps: list[int] = []

        def fake_full(done):
            steps.append(win._cmd_step)  # noqa: SLF001
            done(None, None)

        st.confirm_full = fake_full  # type: ignore[method-assign]
        win._command_confirm_tick()  # noqa: SLF001
        check("第 1 次读完 → 等下一个时点", steps == [0] and win._cmd_timer.isActive(),  # noqa: SLF001
              f"{steps} active={win._cmd_timer.isActive()}")  # noqa: SLF001
        win._command_confirm_tick()  # noqa: SLF001
        win._command_confirm_tick()  # noqa: SLF001
        check("固定跑满 3 次", steps == [0, 1, 2] and not win._cmd_timer.isActive(),  # noqa: SLF001
              f"{steps} active={win._cmd_timer.isActive()}")  # noqa: SLF001

        # 中途有值变化也不早停（签名覆盖全设备，常变属性会让"有变化"恒真）
        win._start_command_confirm()  # noqa: SLF001
        moved: list[int] = []

        def full_with_changes(done):
            moved.append(win._cmd_step)  # noqa: SLF001
            done({"dev/1.1": object()}, None)

        st.confirm_full = full_with_changes  # type: ignore[method-assign]
        win._command_confirm_tick()  # noqa: SLF001
        still_running = win._cmd_timer.isActive()  # noqa: SLF001
        win._command_confirm_tick()  # noqa: SLF001
        win._command_confirm_tick()  # noqa: SLF001
        check("有值变化也不提前结束（仍然跑满 3 次）",
              still_running and moved == [0, 1, 2] and not win._cmd_timer.isActive(),  # noqa: SLF001
              f"{moved} active={win._cmd_timer.isActive()}")  # noqa: SLF001

        # 1 秒去重：距上一轮全量读不足 1 秒就跳过，不发请求 ----------------------
        import time as _t  # noqa: PLC0415

        del st.confirm_full  # 恢复真实的实现（前面几段打了桩）
        st._last_full_at = _t.monotonic()  # noqa: SLF001

        def boom(*a, **k):
            raise AssertionError("不足 1 秒时不该发全量读")

        st.worker.submit = boom  # type: ignore[method-assign]
        skipped: list[tuple] = []
        st.confirm_full(lambda res, err: skipped.append((res, err)))
        check("不足 1 秒 → 跳过这一次", skipped == [(None, None)], str(skipped))

        # 解耦：全量确认不改 tick 的间隔
        before_ms = win._poll_timer.interval()  # noqa: SLF001
        win._start_command_confirm()  # noqa: SLF001
        check("全量确认不动 _poll_timer 的间隔",
              win._poll_timer.interval() == before_ms,  # noqa: SLF001
              f"{before_ms} → {win._poll_timer.interval()}")  # noqa: SLF001
    finally:
        win.close()


def _no_persist() -> None:
    """确认节奏只活在内存里：**崩溃重启不会把临时档带进下一次**。

    2026-11-12 用户问过：发指令后几秒内程序崩了，下次启动会不会沿用临时的高频？
    答案是不会，但"不会"必须能证明，否则哪天有人为了"记住上次的间隔"往 prefs 里
    塞一个键，就真的会了。这组从四个角度钉住：

    1. ``poll_demand`` 这个模块**根本不碰文件**（源码里没有任何写盘调用）；
    2. 间隔来自模块常量，新建一张表就是常量值，没有任何"上次留下的"可读；
    3. 模拟崩溃：登记过需求的旧表**直接丢掉**（不清、不保存），新建一张仍是常态；
    4. 唯一的偏好文件 ``prefs.json`` 里没有任何轮询 / 确认相关键（真存一次再看 JSON）。
    """
    import inspect  # noqa: PLC0415
    import json  # noqa: PLC0415

    from mihome.core import poll_demand as pd  # noqa: PLC0415
    from mihome.core.prefs import Prefs  # noqa: PLC0415

    src = inspect.getsource(pd)
    touched = [w for w in ("write_text(", "json.dump", "open(", "os.replace") if w in src]
    check("poll_demand 不写任何文件", touched == [], str(touched))

    # 新建 = 从常量起。idle_ms 就是常态档，没人给它"上次的值"。
    # 注意新表的需求表是**空的**（idle 只是兜底，不算一条登记）——正因如此，
    # 没有任何"上次留下的临时档"能被读到。
    fresh = PollDemandBoard("属性轮询", idle_ms=3000)
    check("新建的需求表就是常态 3000", fresh.interval_ms == 3000, str(fresh.interval_ms))
    check("这次运行没有任何登记（没有残留的 window / widget）",
          fresh.demands() == (), str(fresh.demands()))

    # 模拟"登记过一条临时需求、还没撤就崩了"：旧表在这里被丢弃，不做任何清理/保存
    doomed = PollDemandBoard("属性轮询", idle_ms=3000)
    doomed.set("window", 3000, "窗口可见")
    doomed.set("widget", 1000, "挂件临时要 1 秒")
    check("崩之前确实是 1000", doomed.interval_ms == 1000, str(doomed.interval_ms))
    del doomed
    after = PollDemandBoard("属性轮询", idle_ms=3000)
    check("崩后新进程仍是 3000（临时档没被继承）", after.interval_ms == 3000,
          str(after.interval_ms))

    # 唯一的偏好文件里也不许有轮询键
    path = _TMP / "prefs-probe.json"
    prefs = Prefs(path)
    prefs.save()
    payload = json.loads(path.read_text(encoding="utf-8"))
    bad = [k for k in payload if any(w in k.lower() for w in ("poll", "interval", "confirm"))]
    check("prefs.json 里没有轮询 / 确认相关的键", bad == [], str(bad))
    check("prefs.json 的键是白名单（新增键要过这一关）",
          sorted(payload) == sorted([
              "version", "card_styles", "home_order", "room_order", "device_order",
              "scene_order", "hidden", "hidden_scenes", "hidden_collapsed",
              "camera_network", "speaker_command_did", "speaker_silent",
              "close_to_tray", "start_in_tray", "layout",
          ]),
          str(sorted(payload)))

    # 常量本身就是"每次运行的起点"：改这里才会改默认，env / 配置都改不了
    from mihome.ui.main_window import (  # noqa: PLC0415
        POLL_HIDDEN_MS,
        POLL_MS,
    )

    check("常量：前台 3 秒 / 托盘 15 秒",
          (POLL_MS, POLL_HIDDEN_MS) == (3000, 15_000),
          str((POLL_MS, POLL_HIDDEN_MS)))


def main() -> int:
    _basics()
    _floor()
    _none_and_subscribe()
    _describe()
    _confirm()
    _no_persist()
    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败：")
        for f in FAILS:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
