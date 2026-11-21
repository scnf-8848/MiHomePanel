# SPDX-License-Identifier: GPL-3.0-or-later
"""验证 core/store.py：加载 / 房间聚合 / 轮询 / 卡片状态。

用法：
  .venv/Scripts/python.exe -X utf8 scripts/verify_store.py          # 只读
  .venv/Scripts/python.exe -X utf8 scripts/verify_store.py --write  # 额外做一次开关往返
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mihome.core.api import Client  # noqa: E402
from mihome.core.jobs import SerialWorker  # noqa: E402
from mihome.core.store import Store  # noqa: E402


def pump(worker: SerialWorker, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        worker.drain()
        time.sleep(0.02)


def main() -> None:
    do_write = "--write" in sys.argv
    worker = SerialWorker()
    client = Client()
    store = Store(client, worker)
    changes: set[str] = set()
    store.on_changed = changes.update
    store.on_status = lambda s: print(f"  [状态] {s}")

    t0 = time.time()
    store.load()
    timeout = time.time() + 60
    while store.loading and time.time() < timeout:
        worker.drain()
        time.sleep(0.02)
    print(f"加载完成 {time.time()-t0:.1f}s  设备 {len(store.devices)}")

    views = store.rooms()
    print(f"房间视图 {len(views)} 组")
    for v in views:
        c = store.counts(v.devices)
        print(f"  {v.home_label}/{v.room:<6} {c['total']:>2} 台  开 {c['on']:>2}  离线 {c['offline']}")
    print(f"  轮询属性数: {len(store.poll_refs())}")

    # 三轮轮询，看稳定态耗时
    for i in range(3):
        changes.clear()
        t = time.time()
        store.poll()
        while store.polling and time.time() < t + 30:
            worker.drain()
            time.sleep(0.01)
        print(f"轮询#{i+1}: {time.time()-t:.2f}s  变化 {len(changes)} 台")

    print("\n卡片状态抽样:")
    for st in list(store.devices.values())[:14]:
        if st.spec is None:
            print(f"  {st.name[:18]:<20} [无规格] 只能展示")
            continue
        flag = "开" if st.any_on else "关"
        print(f"  {st.name[:18]:<20} [{st.card.layout:<8}] {flag}  {st.subtitle or '—':<18} 控件{len(st.card.controls)} 键{len(st.card.gangs)}")

    # 面板卡：确认多个按键绑定到不同 siid
    print("\n多键面板:")
    for st in store.devices.values():
        if not st.card.gangs:
            continue
        print(f"  {st.name[:24]:<26} 房间={st.room}")
        for g in st.card.gangs:
            v = st.values.get(g.power.key)
            print(f"      siid={g.siid:<3} {g.label[:14]:<16} = {v}  房间={g.room}")
        break

    if do_write:
        test_write(store, worker)


def test_write(store: Store, worker: SerialWorker) -> None:
    """挑一台灯，开关各写一次再还原，验证写入链路与乐观值。"""
    target = None
    for st in store.devices.values():
        if st.card.layout == "light" and st.node.online and st.power is not None:
            c = st.card.control("power")
            if c and c.prop is not None:
                target = (st, c.prop)
                break
    if target is None:
        print("\n没有可测的灯，跳过写入测试")
        return

    st, prop = target
    key = prop.key
    before = st.values.get(key)
    print(f"\n写入测试: {st.name}  当前={before}")
    for value in (not before, before):
        store.set_value(st.did, key, value)
        t = time.time()
        while time.time() < t + 15 and key in st.optimistic:
            worker.drain()
            time.sleep(0.01)
        print(f"  写 {value} → 本地 {st.values.get(key)}  乐观队列清空={key not in st.optimistic}  错误={st.error or '无'}")
        pump(worker, 4)  # 等静默期过去，让回读落地


if __name__ == "__main__":
    main()
