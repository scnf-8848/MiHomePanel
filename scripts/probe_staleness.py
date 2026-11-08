# SPDX-License-Identifier: GPL-3.0-or-later
"""只读探测：云端 ``updateTime`` 的真实口径，用来给「读数陈旧」提示定阈值。

要回答三个问题：

1. ``updateTime`` 是**每个属性一份**，还是整台设备一份？（设备级的话，
   一个开关动作就会把温度读数的"未变化天数"清零，提示会永远不出现）
2. 它表示「这个值最后一次变化」还是「最后一次被读取」？——读两遍看它推不推进。
   如果读一次就刷新，那它只能当数据新鲜度用，不能当"多久没变"用。
3. 真实家庭里的分布长什么样：多久算老？有没有大量属性天生不变
   （门磁、开关），会不会让提示变成噪音。

只调用读接口，不发任何写指令。

用法：
    .venv/Scripts/python.exe -X utf8 scripts/probe_staleness.py
    .venv/Scripts/python.exe -X utf8 scripts/probe_staleness.py --limit 12 --wait 20
"""

from __future__ import annotations

import argparse
import os
import time
from collections import Counter

from mihome.core.api import Client, PropRef
from mihome.core.spec import SpecRepository


def human(seconds: float) -> str:
    if seconds < 3600:
        return f"{seconds / 60:.0f} 分钟"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} 小时"
    return f"{seconds / 86400:.1f} 天"


def pass_once(client: Client, limit: int) -> tuple[dict[PropRef, int], dict[PropRef, str], dict[PropRef, object]]:
    """读一遍所有在线设备的可读属性，返回 {ref: updateTime}、{ref: 标签}、{ref: 值}。"""
    topo = client.load_topology()
    repo = SpecRepository()
    # 按 did 排序再取前 N 台：云端的设备顺序不保证稳定，两遍取到不同样本的话
    # 第二轮就没法逐条比对时间戳（第一次跑就踩到了，交集只剩 9 条）。
    devices = sorted((d for d in topo.real_devices() if d.online), key=lambda d: d.did)[:limit]
    specs = repo.get_many([d.spec_type for d in topo.real_devices()], workers=4)

    labels: dict[PropRef, str] = {}
    values: dict[PropRef, object] = {}
    stamps: dict[PropRef, int] = {}
    total = 0
    for node in devices:
        spec = specs.get(node.spec_type) if node.spec_type else None
        if spec is None:
            continue
        refs: list[PropRef] = []
        for svc in spec.services:
            for prop in svc.properties:
                if "read" not in prop.access:
                    continue
                ref = PropRef(did=node.did, siid=prop.siid, piid=prop.piid)
                refs.append(ref)
                labels[ref] = f"{node.name} / {prop.label}"
        if not refs:
            continue
        got = client.read(refs)
        total += len(refs)
        for ref, rv in got.items():
            if not rv.ok:
                continue
            stamps[ref] = rv.update_time
            values[ref] = rv.value
    print(f"  设备 {len(devices)} 台，请求属性 {total} 条，拿到读数 {len(stamps)} 条")
    return stamps, labels, values


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10, help="最多拿多少台在线设备做样本")
    parser.add_argument("--wait", type=int, default=15, help="两遍之间等多少秒，验证时间戳是否随读取推进")
    args = parser.parse_args()

    client = Client()
    if not client.available:
        print("没有可用凭据，先扫码登录一次")
        return

    print("[1] 第一遍")
    t0 = time.time()
    first, labels, values = pass_once(client, args.limit)
    if not first:
        print("  没读到任何属性")
        return

    now = time.time()
    missing = [r for r, ts in first.items() if not ts]
    print(f"  缺 updateTime 的读点：{len(missing)} / {len(first)}")
    if missing:
        for ref in missing[:5]:
            print(f"    - {labels.get(ref, ref)}")

    buckets: Counter[str] = Counter()
    for ref, ts in first.items():
        if not ts:
            continue
        age = max(0.0, now - ts)
        if age < 3600:
            buckets["< 1 小时"] += 1
        elif age < 86400:
            buckets["1 小时 – 1 天"] += 1
        elif age < 7 * 86400:
            buckets["1 – 7 天"] += 1
        else:
            buckets["> 7 天"] += 1
    print("  分布：")
    for key in ("< 1 小时", "1 小时 – 1 天", "1 – 7 天", "> 7 天"):
        print(f"    {key:16s} {buckets[key]:4d}")

    old = sorted(
        ((now - ts, ref) for ref, ts in first.items() if ts and now - ts > 86400),
        reverse=True,
    )
    print(f"  超过 1 天没变的读点：{len(old)} 条，最旧的 10 条：")
    for age, ref in old[:10]:
        stamp = time.strftime("%m-%d %H:%M", time.localtime(now - age))
        val = values.get(ref)
        print(f"    {human(age):>8s}  {stamp}  {labels.get(ref, ref)} = {val!r}")

    print(f"\n[2] 等 {args.wait}s 再读一遍，看 updateTime 会不会推进")
    time.sleep(args.wait)
    second, _, _ = pass_once(client, args.limit)
    moved = same = 0
    for ref, ts in first.items():
        if ref not in second:
            continue
        if second[ref] != ts:
            moved += 1
            if moved <= 5:
                print(f"    变化：{labels.get(ref, ref)} {ts} → {second[ref]}")
        else:
            same += 1
    print(f"  时间戳没动：{same}，动了：{moved}")
    print("  结论口径：时间戳不动 = 只记「值最后一次变化」；大面积推进 = 读一次就刷新")
    print(f"\n总用时 {time.time() - t0:.1f}s")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    main()
