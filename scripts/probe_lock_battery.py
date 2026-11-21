# SPDX-License-Identifier: GPL-3.0-or-later
"""只读探测：门锁到底有几个电量属性，程序现在绑定的是哪一个。

为什么要有这个脚本：小米智能门锁同时装**两套电池**——锂电池（主用）和干电池（备用），
米家 App 里两个电量分开展示。程序目前只显示一个「电量」读数，且用户观察到它跟的是
干电池（100%），而真正该监控的是锂电池（54%）。

毛病出在绑定阶段：``catalog._bind`` 用 ``spec.find("battery-level", service="lock")``，
门锁的两块电池都叫 ``battery-level``、都在叫 ``battery`` 的服务里（服务名撞车），
``find`` 在"没有服务名精确命中"时按 siid/piid 升序取第一条 —— 谁 siid 小就绑谁，
跟"主用 / 备用"完全无关。所以要先看清楚真实 spec，再决定按什么区分。

安全约定（见记忆里的测试纪律）：
  * **只读**：只列规格 + 可选读一次当前值，**不发任何写请求、不跑动作**。
  * 必须显式指定房间（``--room 家庭-房间``）或 ``--all``，共享家庭整户跳过；
    ``--all`` 才不限房间。``--dry`` = 只列属性、连读数都不发。
  * 规格走本地缓存，缺了才联网拉 miot-spec（不经过设备）。

用法（项目根目录）：

    .venv/Scripts/python.exe -X utf8 scripts/probe_lock_battery.py --room 示例家庭-示例-客厅 --dry
    .venv/Scripts/python.exe -X utf8 scripts/probe_lock_battery.py --room 示例家庭-示例-客厅 --read
    .venv/Scripts/python.exe -X utf8 scripts/probe_lock_battery.py --all --read
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mihome.core.api import Client, PropRef  # noqa: E402
from mihome.core.catalog import build_card  # noqa: E402
from mihome.core.spec import DeviceSpec, SpecRepository  # noqa: E402

#: 电量可能落在这几个属性名上（不同固件 / 型号起名不一致）
HINTS = ("battery", "power", "charge", "voltage", "percent")


def _name_of(urn: str) -> str:
    """``urn:miot-spec-v2:property:battery-level:000000xx:dev:1`` → ``battery-level``。"""
    parts = (urn or "").split(":")
    for i, seg in enumerate(parts):
        if seg in ("property", "service", "event") and i + 1 < len(parts):
            return parts[i + 1]
    return urn or "?"


def _dump(spec: DeviceSpec, did: str) -> list:
    """列出所有电池相关属性，返回 ``[(prop, 是否当前绑定)]``。"""
    bound = build_card(spec, did=did)
    current = {c.slot: c.prop for c in list(bound.controls) + list(bound.metrics)}
    picked = current.get("battery")

    hits = [
        p for p in spec.properties
        if any(h in p.name.lower() for h in HINTS) and p.readable
    ]
    print(f"  电池相关属性 {len(hits)} 条：")
    print(f"    {'方法':<7}{'属性名':<26}{'服务':<12}{'形状':<28}标记")
    for p in hits:
        shape = f"{p.format}"
        if p.unit:
            shape += f" unit={p.unit}"
        if p.value_range:
            lo, hi, step = p.value_range
            shape += f" {lo:g}~{hi:g}/{step:g}"
        flag = ""
        if p is picked:
            flag = "← 卡片『电量』现在绑的就是这条"
        # 服务名两台电池都叫 ``battery``，只能靠 ``service_label``（服务自己的注释，
        # 「干电池」/「锂电池」）区分；按服务名查会让第二条继承第一条的标签。
        where = f"{p.service}({p.service_label})"
        print(f"    {p.method:<7}{p.name:<26}{where:<12}{shape:<28}{flag}")
    dup = [p for p in hits if p.name == "battery-level"]
    if len(dup) > 1:
        print(f"  ⚠ 有 {len(dup)} 条同名 battery-level（两块电池），"
              f"find() 靠 siid 升序挑第一条 → 当前挑的是 {picked.method if picked else '无'}")
    if not hits:
        print("  （规格里没有可读的电量属性）")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", default="",
                    help="只看这个房间（可写「家庭-房间」）；不传则必须用 --all")
    ap.add_argument("--all", action="store_true", help="不看房间，列所有门锁")
    ap.add_argument("--dry", action="store_true", help="只列属性，不读当前值")
    ap.add_argument("--read", action="store_true", help="读一次当前值（只读，等价于不传 --dry）")
    args = ap.parse_args()
    if not args.room and not args.all:
        print("请显式传 --room 家庭-房间，或用 --all 查看全部门锁。")
        return 2
    want_read = args.read and not args.dry

    client = Client()
    if not client.available:
        print("未登录或凭据不可用。")
        return 1

    topo = client.load_topology()
    homes = {h.name for h in topo.homes}
    home_filter, room_filter = "", args.room
    if "-" in args.room:
        head, tail = args.room.rsplit("-", 1)
        if head in homes:
            home_filter, room_filter = head, tail

    locks = []
    for node in topo.real_devices():
        if "lock" not in (node.spec_type or ""):
            continue
        if args.all:
            locks.append(node)
            continue
        if room_filter not in (node.room or ""):
            continue
        if home_filter and (node.home or "") != home_filter:
            continue
        locks.append(node)

    if not locks:
        print(f"房间「{args.room}」里没有门锁（用 --all 列全部门锁）。")
        return 1

    repo = SpecRepository()
    specs = repo.get_many([n.spec_type for n in locks])
    targets = []
    for node in locks:
        spec = specs.get(node.spec_type)
        print(f"\n[{node.name}]  model={node.model}")
        print(f"  家庭 {node.home}  房间 {node.room}  在线 {'是' if node.online else '否'}")
        if spec is None:
            print(f"  ⚠ 取不到规格（{node.spec_type}）")
            continue
        print(f"  规格 {spec.urn}")
        print(f"  服务 {len(spec.services)} 个、属性 {len(spec.properties)} 条")
        hits = _dump(spec, node.did)
        targets.append((node, hits))

    if want_read and targets:
        refs = [
            PropRef(did=node.did, siid=p.siid, piid=p.piid)
            for node, hits in targets for p in hits
        ]
        if refs:
            print("\n---- 当前值（只读一次）----")
            values = client.read(refs, source="")
            for node, hits in targets:
                for p in hits:
                    rv = values.get(PropRef(node.did, p.siid, p.piid))
                    if rv is None:
                        text = "未返回（被跳过）"
                    elif not rv.ok:
                        text = f"失败 code={rv.code}"
                    else:
                        text = f"{rv.value}{'%' if p.unit == 'percentage' else ''}"
                    print(f"  {node.name}  {p.method:<7} {p.name:<24} = {text}")
    elif not want_read:
        print("\n（--dry / 未要求读数：没有向设备发任何请求）")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
