# SPDX-License-Identifier: GPL-3.0-or-later
"""陈旧读数提示的回归：``core/freshness.py`` 的判定 + 两处接线。

跑法：.venv/Scripts/python.exe -X utf8 scripts/verify_freshness.py
"""

from __future__ import annotations

import inspect
import sys
import time

from mihome.core.api import PropRef, ReadValue
from mihome.core.freshness import STALE_AFTER, stale_text
from mihome.core.store import DeviceState, Store

PASS = 0
FAIL = 0


def check(name: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"PASS  {name}  {got!r}")
    else:
        FAIL += 1
        print(f"FAIL  {name}  得到 {got!r} 期望 {want!r}")


def main() -> int:
    now = 1_800_000_000.0  # 固定基准，避免跑测试的时钟影响结果
    day = 86400.0

    print("1) 阈值与文案")
    check("默认阈值是 3 天", STALE_AFTER, 3 * day)
    check("没有时间戳（静态元数据）", stale_text(0, now=now), "")
    check("None 当没有", stale_text(None, now=now), "")
    check("差 1 秒不到 3 天", stale_text(int(now - 3 * day + 1), now=now), "")
    check("刚好 3 天", stale_text(int(now - 3 * day), now=now), "3 天未更新")
    check("4 天 12 小时（向下取整）", stale_text(int(now - 4.5 * day), now=now), "4 天未更新")
    check("29 天还是按天", stale_text(int(now - 29 * day), now=now), "29 天未更新")
    check("刚好 30 天换成月", stale_text(int(now - 30 * day), now=now), "1 个月未更新")
    check("253 天（实测滤芯）", stale_text(int(now - 253 * day), now=now), "8 个月未更新")
    check("时间戳在未来（设备时钟不准）", stale_text(int(now + day), now=now), "")

    print("\n2) 阈值可调（别显示“0 天”）")
    check("1 小时阈值 + 2 小时", stale_text(int(now - 7200), now=now, threshold=3600), "2 小时未更新")
    check(
        "1 小时阈值 + 30 分钟",
        stale_text(int(now - 1800), now=now, threshold=3600),
        "",
    )

    print("\n3) 接线：云端 updateTime 真的落进 DeviceState.stamps")
    check("DeviceState 有 stamps 且默认空", DeviceState.__dataclass_fields__["stamps"].default_factory(), {})
    poll_src = inspect.getsource(Store._on_polled)
    adv_src = inspect.getsource(Store.read_props)
    check("轮询路径记 stamps", "st.stamps[key] = rv.update_time" in poll_src, True)
    check("「更多功能」按需读也记 stamps", "st.stamps[key] = rv.update_time" in adv_src, True)
    check("时间戳缺失时不写入（不覆盖旧值）", "if rv.update_time:" in poll_src, True)

    print("\n4) ReadValue 的时间戳就是拿它算")
    rv = ReadValue(ref=PropRef(did="1", siid=2, piid=1), value=1, ok=True, update_time=int(time.time() - 5 * day))
    check("changed_ago 约 5 天", round(rv.changed_ago / day), 5)
    check("同一时间戳喂给 stale_text", stale_text(rv.update_time, now=time.time()), "5 天未更新")

    print(f"\n{PASS} 项通过，{FAIL} 项失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
