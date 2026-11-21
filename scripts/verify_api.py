# SPDX-License-Identifier: GPL-3.0-or-later
"""验证 core/api.py：拓扑解析 + 批量读 + 分片 + 能力学习。

只读，不发任何写指令。

用法：.venv/Scripts/python.exe -X utf8 scripts/verify_api.py
"""

from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mihome.core.api import Client, PropRef  # noqa: E402
from mihome.core.spec import SpecRepository  # noqa: E402


def main() -> None:
    t0 = time.time()
    client = Client()
    print(f"认证可用: {client.available}   auth={client.auth_path}")

    topo = client.load_topology()
    print(f"拓扑: {len(topo.homes)} 个家庭 / {len(topo.devices)} 台真机 / 耗时 {time.time()-t0:.1f}s")
    for h in topo.homes:
        n = sum(1 for d in topo.devices if d.home == h.name)
        print(f"  {h.display_name:<12} 房间 {len(h.rooms):>2}  设备 {n:>2}  共享={h.shared}")

    unknown = [d for d in topo.devices if not d.room]
    print(f"  未归属房间: {len(unknown)} 台 {[d.name for d in unknown]}")

    # 用规格找出每台设备的"开关" + "会主动上报"的属性
    repo = SpecRepository()
    urns = [d.spec_type for d in topo.real_devices()]
    specs = repo.get_many(urns, workers=4)
    print(f"\n规格: {len(specs)}/{len(set(urns))}  "
          f"来源={dict(Counter(s.source for s in specs.values()))}")

    refs: list[PropRef] = []
    power_refs: list[PropRef] = []
    for d in topo.real_devices():
        spec = specs.get(d.spec_type)
        if not spec:
            continue
        for p in spec.properties:
            if not p.readable:
                continue
            ref = PropRef(d.did, p.siid, p.piid)
            if p.is_state_of_power:
                power_refs.append(ref)
                refs.append(ref)
            elif p.notifies:
                refs.append(ref)
    print(f"待读属性: {len(refs)}（其中开关 {len(power_refs)}）")

    t1 = time.time()
    result = client.read(refs, chunk=20)
    dt = time.time() - t1
    ok = [v for v in result.values() if v.ok]
    bad = [v for v in result.values() if not v.ok]
    print(f"\n读取 {len(result)} 条 / 成功 {len(ok)} / 失败 {len(bad)} / 耗时 {dt:.1f}s "
          f"/ 约 {-(-len(refs)//20)} 个请求")

    print("\n失败分布:")
    for code, n in Counter(v.code for v in bad).most_common():
        sample = next(v.ref for v in bad if v.code == code)
        print(f"  code={code:<12} {n:>3} 条   例: {sample.did} {sample.method}")

    print(f"\n能力账本: 新记 {len(client.capabilities)} 条不支持")
    client.flush()

    # 第二次读同样的集合：不支持的应被跳过，更快
    t2 = time.time()
    again = client.read(refs, chunk=20)
    print(f"二次读取 {len(again)} 条 / {time.time()-t2:.1f}s（应更少，因为跳过已确认不支持的）")

    print("\n开关状态抽样:")
    for r in power_refs[:10]:
        v = result.get(r)
        if v and v.ok:
            node = topo.get(r.did)
            print(f"  {node.name if node else r.did:<20} {r.method:<6} = {v.value}  "
                  f"上次变化 {v.changed_ago/60:.0f} 分钟前")


if __name__ == "__main__":
    main()
