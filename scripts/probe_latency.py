# SPDX-License-Identifier: GPL-3.0-or-later
"""只读探测：**我们这条路**看到一次状态变化要多久（顺带回答"能不能靠加频率追上米家 App"）。

背景：米家 App 里按实体键，界面几乎 1 秒内就变；我们这边是 3 秒一轮的 HTTP 轮询，所以
最差要等 3 秒。这个脚本把我们的延迟量出来——它测的是"云端已经有新值 → 我们读到手"的
那一段，以及"设备 → 云端"那一段有多快（云端返回的 ``updateTime`` 与本地时钟对比）。

用法（先按提示挑一台设备，然后**去按实体按键**）：

    .venv/Scripts/python.exe -X utf8 scripts/probe_latency.py --device 客厅 --seconds 60
    .venv/Scripts/python.exe -X utf8 scripts/probe_latency.py --device 客厅 --interval 0.3

读法：

* ``round trip`` = 一次 ``prop/get`` 的往返（含云端处理）。它决定"轮询最快能多快"：
  间隔小于往返时间就只是在排队（``Client`` 那把锁会串行化请求）。
* 每条 ``变化`` 行里的 ``云端戳 +x.x s`` = 云端记录的这次变化距我们读到的时刻多久，
  也就是"设备 → 云端 → 我们"整条链路；理论上它约等于「真实动作时刻 → 我们读到」
  （假设云端时间戳准）。**这个数就是 App 那种 1 秒内更新的上限对照**。
* 只调用读接口，不写任何属性、不改任何配置。
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mihome.core.api import Client, PropRef  # noqa: E402
from mihome.core.spec import SpecRepository  # noqa: E402


def _pick(nodes: list, hint: str):
    if hint:
        hit = [n for n in nodes if hint in n.name]
        if not hit:
            print(f"没有名字里带「{hint}」的在线设备。可选：")
            for n in nodes[:40]:
                print(f"  - {n.name}  ({n.model})")
            raise SystemExit(1)
        return hit[0]
    return nodes[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="测量云端轮询能看到多快的变化")
    ap.add_argument("--device", default="", help="设备名的一部分（不填就用第一台在线设备）")
    ap.add_argument("--interval", type=float, default=0.5, help="轮询间隔，秒（默认 0.5）")
    ap.add_argument("--seconds", type=float, default=60.0, help="跑多久，秒（默认 60）")
    args = ap.parse_args()

    client = Client()
    if not client.available:
        print("没有可用的米家凭据，先运行主程序扫码登录一次")
        return 1

    print("拉设备列表…", flush=True)
    topo = client.load_topology()
    online = [n for n in topo.real_devices() if n.online]
    if not online:
        print("没有在线设备")
        return 1
    node = _pick(online, args.device)

    spec = SpecRepository().get(node.spec_type or "")
    if spec is None:
        print(f"拿不到 {node.model} 的规格")
        return 1

    refs: list[PropRef] = []
    labels: dict[PropRef, str] = {}
    for svc in spec.services:
        for prop in svc.properties:
            if "read" not in prop.access:
                continue
            ref = PropRef(did=node.did, siid=prop.siid, piid=prop.piid)
            refs.append(ref)
            labels[ref] = f"{svc.label}/{prop.label}"
    print(f"设备：{node.name}（{node.model}），{len(refs)} 条可读属性，"
          f"每 {args.interval:.2f}s 读一次，共 {args.seconds:.0f}s")
    print("现在去按一下实体按键（灯/开关），或者用别的客户端改一次状态。\n", flush=True)

    started = time.monotonic()
    last: dict[PropRef, object] = {}
    rtts: list[float] = []
    changes = 0
    rounds = 0
    next_at = started
    try:
        while True:
            now = time.monotonic()
            if now - started >= args.seconds:
                break
            if now < next_at:
                time.sleep(min(0.02, next_at - now))
                continue
            t0 = time.perf_counter()
            got = client.read(refs)
            rtt = time.perf_counter() - t0
            rtts.append(rtt)
            rounds += 1
            at = time.monotonic() - started
            for ref, rv in got.items():
                if not rv.ok:
                    continue
                before = last.get(ref, "<未读>")
                if before == rv.value:
                    continue
                last[ref] = rv.value
                if before == "<未读>":
                    print(f"  基线  {labels.get(ref, ref)} = {rv.value!r}")
                    continue
                changes += 1
                stamp = rv.changed_ago
                print(f"{at:6.2f}s  变化  {labels.get(ref, ref)}: {before!r} → {rv.value!r}"
                      f"   云端戳 +{stamp:.2f}s   本轮往返 {rtt * 1000:.0f}ms", flush=True)
            next_at = max(next_at + args.interval, time.monotonic() - rtt)
    except KeyboardInterrupt:
        print("\n（手动中断）")

    if not rtts:
        print("一轮都没成功，检查网络")
        return 1
    rtts_ms = sorted(x * 1000 for x in rtts)
    p50 = statistics.median(rtts_ms)
    p95 = rtts_ms[min(len(rtts_ms) - 1, int(len(rtts_ms) * 0.95))]
    print(f"\n共 {rounds} 轮、{changes} 次变化。往返：p50 {p50:.0f}ms / "
          f"p95 {p95:.0f}ms / 最大 {rtts_ms[-1]:.0f}ms")
    print(f"按 p95 算，轮询间隔的实际下限约 {p95 / 1000:.2f}s；"
          f"{args.interval:.2f}s 间隔下一小时约 {3600 / args.interval:.0f} 轮")
    if p50 > args.interval * 1000:
        print("注意：间隔已经小于一次往返，实际节奏被往返时间拖着走，再调小没有意义")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
