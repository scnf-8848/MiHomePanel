# SPDX-License-Identifier: GPL-3.0-or-later
"""验证轮询需求登记（`mihome/core/poll_demand.py`）。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_poll_demand.py``

这一层是纯逻辑（不依赖 Qt），所以直接跑真对象。验的是四类事：

* **取最严**：多条登记里间隔最小的那条说了算；
* **不互相重置**：结果没变就不回调（否则挂件要的 1 秒会被窗口切后台那一下重置）；
* **有下限**：谁报 100 ms 都会被收敛到 0.5 秒，这是"保守设计"的兜底；
* **能回落**：撤掉最严的那条之后回到次严 / 默认 / 停表；
* **连点不叠加**：同一个 owner 再登记是覆盖那一条，撤一次就全撤掉；窗口那半个
  （`MainWindow` 的单次定时器）只顺延、不攒定时器，连点 3 次仍然只有一个窗口；
* **不落盘**：突发只活在内存里，崩溃重启仍从常量起（prefs 里没有任何轮询键）。
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


def _burst() -> None:
    """指令后的高频窗口：压在上面的一档，撤掉就回原档。

    这条对应 ``main_window`` 的 ``_start_burst`` / ``_end_burst``：窗口该是 3 秒
    还是 15 秒由"看不看得见"决定，突发只在写指令之后的几秒里临时更严。
    """
    hits: list[int | None] = []

    # 前台：3 秒常态 → 发指令 → 0.5 秒 → 窗口结束回 3 秒
    b = PollDemandBoard("属性轮询", idle_ms=3000)
    hits = _record(b)
    b.set("window", 3000, "窗口可见")
    b.set("burst", 500, "刚发过指令")
    check("发指令后压到 500", b.interval_ms == 500, str(b.interval_ms))
    check("突发那条是最严的", b.holder().owner == "burst", str(b.holder()))
    b.clear("burst")
    check("窗口结束回 3000", b.interval_ms == 3000, str(b.interval_ms))
    # 两次切换各一次回调（3000 → 500 → 3000），没有多余的重置
    check("只为两次真实变化回调", hits == [3000, 500, 3000], str(hits))

    # 托盘：15 秒常态 → 发指令 → 仍然 0.5 秒（后台也不能等 15 秒）
    b2 = PollDemandBoard("属性轮询", idle_ms=3000)
    b2.set("window", 15_000, "窗口在托盘")
    b2.set("burst", 500, "刚发过指令")
    check("托盘里发指令也压到 500", b2.interval_ms == 500, str(b2.interval_ms))
    b2.clear("burst")
    check("托盘里窗口结束回 15000", b2.interval_ms == 15_000, str(b2.interval_ms))

    # 下限：谁报 100 都会被抬到 MIN_INTERVAL_MS，0.5 秒已经不再被抬
    b3 = PollDemandBoard("属性轮询", idle_ms=3000)
    b3.set("burst", 500, "刚发过指令")
    check("500 不被下限抬高", b3.interval_ms == 500, str(b3.interval_ms))
    check("下限就是 500", MIN_INTERVAL_MS == 500, str(MIN_INTERVAL_MS))

    # 连点：同一个 owner 再登记是**覆盖**那一条，不会又挂一条 ---------------------
    b4 = PollDemandBoard("属性轮询", idle_ms=3000)
    b4.set("window", 3000, "窗口可见")
    for _ in range(3):
        b4.set("burst", 500, "刚发过指令")
    check("连点 3 次仍然只有一条 burst 需求", len(b4.demands()) == 2, str(b4.demands()))
    check("连点 3 次间隔仍是 500", b4.interval_ms == 500, str(b4.interval_ms))
    b4.clear("burst")
    check("撤一次就全撤掉（不是撤三份）", b4.interval_ms == 3000, str(b4.interval_ms))


def _no_persist() -> None:
    """突发窗口只活在内存里：**崩溃重启不会把 0.5 秒带进下一次**。

    用户问过这个（2026-11-12）：发指令后 5 秒内程序崩了，下次启动会不会沿用 0.5 秒？
    答案是不会，但"不会"必须能证明，否则哪天有人为了"记住上次的间隔"往 prefs 里
    塞一个键，就真的会了。这组从四个角度钉住：

    1. ``poll_demand`` 这个模块**根本不碰文件**（源码里没有任何写盘调用）；
    2. 间隔来自模块常量，新建一张表就是常量值，没有任何"上次留下的"可读；
    3. 模拟崩溃：登记了 burst 的旧表**直接丢掉**（不清、不保存），新建一张仍是常态；
    4. 唯一的偏好文件 ``prefs.json`` 里没有任何轮询相关键（真存一次再看 JSON）。
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
    # 没有任何"上次留下的 burst"能被读到。
    fresh = PollDemandBoard("属性轮询", idle_ms=3000)
    check("新建的需求表就是常态 3000", fresh.interval_ms == 3000, str(fresh.interval_ms))
    check("这次运行没有任何登记（没有残留的 burst / window）",
          fresh.demands() == (), str(fresh.demands()))

    # 模拟"发过指令、窗口没结束就崩了"：旧表在这里被丢弃，不做任何清理/保存
    doomed = PollDemandBoard("属性轮询", idle_ms=3000)
    doomed.set("window", 3000, "窗口可见")
    doomed.set("burst", 500, "刚发过指令")
    check("崩之前确实是 500", doomed.interval_ms == 500, str(doomed.interval_ms))
    del doomed
    after = PollDemandBoard("属性轮询", idle_ms=3000)
    check("崩后新进程仍是 3000（突发没被继承）", after.interval_ms == 3000,
          str(after.interval_ms))

    # 唯一的偏好文件里也不许有轮询键
    path = _TMP / "prefs-probe.json"
    prefs = Prefs(path)
    prefs.save()
    payload = json.loads(path.read_text(encoding="utf-8"))
    bad = [k for k in payload if any(w in k.lower() for w in ("poll", "interval", "burst"))]
    check("prefs.json 里没有轮询 / 突发相关的键", bad == [], str(bad))
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
        BURST_HOLD_MS,
        BURST_MS,
        POLL_HIDDEN_MS,
        POLL_MS,
    )

    check("常量：前 3 秒 / 托盘 15 秒 / 突发 0.5 秒×5 秒",
          (POLL_MS, POLL_HIDDEN_MS, BURST_MS, BURST_HOLD_MS) == (3000, 15_000, 500, 5000),
          str((POLL_MS, POLL_HIDDEN_MS, BURST_MS, BURST_HOLD_MS)))


def _burst_window() -> None:
    """真窗口里的那半个：连点只**顺延**窗口，不会攒出好几个定时器。

    需求表那边已经证明了"同 owner 覆盖"，但窗口的时长是 ``MainWindow`` 里一个
    单次定时器负责的：必须是**同一个** ``QTimer`` 反复 ``start()``（start 会重新
    计时）。要是哪天改成 ``QTimer.singleShot(5000, self._end_burst)``，连点三次就
    是三个独立的单次定时器——第一个会在"第一次点击后 5 秒"把 burst 撤掉，后面两次
    的窗口被提前砍断，而且没人看得出来。这组断言就是防这个。
    """
    from PySide6.QtTest import QTest  # noqa: PLC0415
    from PySide6.QtWidgets import QApplication  # noqa: PLC0415

    from mihome.ui import theme  # noqa: PLC0415
    from mihome.ui.main_window import BURST_HOLD_MS, MainWindow  # noqa: PLC0415

    QApplication.instance() or QApplication([])
    win = MainWindow(theme.DARK, tray_available=False)
    # 别把测试写进用户真实的 QSettings
    win._save_settings = lambda: None  # type: ignore[method-assign]  # noqa: SLF001
    try:
        # 常态是多少取决于窗口看不看得见（离屏没 show 时是按 15 秒登记的），
        # 所以先记下基线，别拿常量比
        base = win.poll_demand.interval_ms
        win._start_burst()  # noqa: SLF001
        check("发指令后窗口是 500", win.poll_demand.interval_ms == 500,
              str(win.poll_demand.interval_ms))
        win._start_burst()  # noqa: SLF001
        win._start_burst()  # noqa: SLF001
        owners = [d.owner for d in win.poll_demand.demands()]
        check("连点 3 次只有一条 burst", owners.count("burst") == 1, str(owners))
        check("burst 窗口用的是同一个单次定时器",
              win._burst_timer.isSingleShot(), "")  # noqa: SLF001

        # 走 300ms 再点一次：剩余时间要回到 5 秒，而不是"5 秒减去已经过的"
        QTest.qWait(300)
        left = win._burst_timer.remainingTime()  # noqa: SLF001
        win._start_burst()  # noqa: SLF001
        again = win._burst_timer.remainingTime()  # noqa: SLF001
        check("再点一次把窗口重新算满 5 秒（顺延）",
              again >= BURST_HOLD_MS - 50, f"{left} → {again}")
        check("窗口不会因为连点变长", again <= BURST_HOLD_MS, str(again))

        win._end_burst()  # noqa: SLF001
        check("窗口到点自己回常态", win.poll_demand.interval_ms == base,
              f"{win.poll_demand.interval_ms}，基线 {base}")
    finally:
        win.close()


def main() -> int:
    _basics()
    _floor()
    _none_and_subscribe()
    _describe()
    _burst()
    _no_persist()
    _burst_window()
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
