# SPDX-License-Identifier: GPL-3.0-or-later
"""验证轮询需求登记（`mihome/core/poll_demand.py`）。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_poll_demand.py``

这一层是纯逻辑（不依赖 Qt），所以直接跑真对象。验的是四类事：

* **取最严**：多条登记里间隔最小的那条说了算；
* **不互相重置**：结果没变就不回调（否则挂件要的 1 秒会被窗口切后台那一下重置）；
* **有下限**：谁报 100 ms 都会被收敛到 1 秒，这是"保守设计"的兜底；
* **能回落**：撤掉最严的那条之后回到次严 / 默认 / 停表。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mihome.core.poll_demand import MIN_INTERVAL_MS, PollDemandBoard

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


def main() -> int:
    _basics()
    _floor()
    _none_and_subscribe()
    _describe()
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
