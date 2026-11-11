# SPDX-License-Identifier: GPL-3.0-or-later
"""只读探测：看风扇 / 净化器 / 浴霸的「风速」到底是枚举还是区间。

为什么要有这个脚本：卡片内容带是按属性**形状**选控件的（``CardBody._pick``：
枚举→胶囊、布尔→开关、有 range→步进器）。风速若是 1–100 或 0–1100 的 range，
卡片上会出现「− 45 +」那种要按二十下的步进器；是枚举就直接是胶囊。
这件事不能按品类猜，只能看真机 spec。

安全约定（见记忆里的测试纪律）：
  * 只读：列规格 + 可选读一次当前值，**不发任何写请求、不跑动作**。
  * 默认只输出指定房间的设备（``--room`` 默认「1103-强」），共享家庭整户跳过。
  * 规格走本地缓存，缺了才联网拉 miot-spec（不经过设备）。

用法（项目根目录）：

    .venv/Scripts/python.exe -X utf8 scripts/probe_fan_spec.py
    .venv/Scripts/python.exe -X utf8 scripts/probe_fan_spec.py --read
    .venv/Scripts/python.exe -X utf8 scripts/probe_fan_spec.py --room 1103-强 --all

依据：docs/reference/openmexia-notes.md 第 7 节（尺寸预算与 ``_pick`` 的量纲问题）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mihome.core.api import Client, PropRef  # noqa: E402
from mihome.core.catalog import build_card  # noqa: E402
from mihome.core.spec import DeviceSpec, SpecRepository  # noqa: E402

#: 关心这些品类（风扇 / 净化器 / 浴霸 / 油烟机 / 空调都靠风速说话）
CATS = {"fan", "air-purifier", "bath-heater", "hood", "air-conditioner"}
#: 属性名里带这些词的都要看：风速可能落在四个候选名之一，还可能藏在别的服务里
HINTS = ("fan", "speed", "level", "percent", "mode", "swing", "wind", "angle")


def _shape(prop) -> str:
    """一句话描述属性形状——这就是卡片选控件的依据。"""
    kind = prop.kind
    if kind == "enum":
        items = " / ".join(f"{o.value}={o.label}" for o in prop.value_list)
        return f"enum({len(prop.value_list)} 项) {items}"
    if prop.value_range:
        lo, hi, step = prop.value_range
        return f"range {lo:g}~{hi:g} step {step:g}"
    if kind == "bool":
        return "bool"
    return kind


def _dump_spec(spec: DeviceSpec) -> None:
    print(f"  规格 urn: {spec.urn}  category={spec.category}")
    rows = []
    for prop in spec.properties:
        text = f"{prop.name} {prop.description}".lower()
        if not any(h in text for h in HINTS):
            continue
        rw = "".join(sorted(set(prop.access)))
        rows.append(
            f"    {prop.method:<5} {prop.name:<18} rw={rw:<3} {_shape(prop)}"
        )
    print("  相关属性：")
    for line in rows:
        print(line)


def _dump_card(card) -> list[tuple[str, object]]:
    """列出绑定结果，并返回 (slot, prop) 供 --read 用。"""
    out: list[tuple[str, object]] = []
    print("  卡片绑定（build_card 的结果）：")
    for c in list(card.controls) + list(card.metrics):
        if c.prop is None:
            print(f"    {c.slot:<12} {c.label:<6} —— 未绑定（规格里没这个属性）")
            continue
        print(
            f"    {c.slot:<12} {c.label:<6} {c.role:<8} "
            f"{c.prop.method} {c.prop.name:<18} {_shape(c.prop)}"
        )
        out.append((c.slot, c.prop))
    if card.missing:
        print(f"    ⚠ critical 槽位缺失：{', '.join(card.missing)}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", default="1103-强", help="只看这个房间（可写「家庭-房间」）")
    ap.add_argument("--all", action="store_true", help="不看房间，列所有设备")
    ap.add_argument("--read", action="store_true", help="额外读一次当前值（只读）")
    args = ap.parse_args()

    client = Client()
    if not client.available:
        print("未登录或凭据不可用。")
        return 1

    topo = client.load_topology()
    homes = {h.name for h in topo.homes}
    home_filter, room_filter = "", args.room
    # 「1103-强」= 家庭 1103 的「强」房间。左边能对上家庭名才拆，否则整串当房间名，
    # 免得房间名本身带「-」时被误拆。
    if "-" in args.room:
        head, tail = args.room.rsplit("-", 1)
        if head in homes:
            home_filter, room_filter = head, tail

    wanted = []
    for node in topo.real_devices():
        if args.all:
            wanted.append(node)
            continue
        if room_filter not in (node.room or ""):
            continue
        if home_filter and (node.home or "") != home_filter:
            continue
        wanted.append(node)

    if not wanted:
        print(f"房间「{args.room}」里没有设备（可以用 --all 看全部）。")
        return 1

    repo = SpecRepository()
    specs = repo.get_many([n.spec_type for n in wanted])
    targets: list[tuple[str, object, list]] = []

    for node in wanted:
        spec = specs.get(node.spec_type)
        if spec is None:
            print(f"\n[{node.name}] {node.model} —— 取不到规格（{node.spec_type}）")
            continue
        if not args.all and spec.category not in CATS:
            continue
        print(f"\n[{spec.category}] {node.name}  model={node.model}")
        print(f"  房间 {node.room}  在线 {'是' if node.online else '否'}")
        _dump_spec(spec)
        card = build_card(
            spec, did=node.did, model=node.model,
            gang_names=node.gang_names, gang_rooms=node.gang_rooms,
        )
        targets.append((node.did, node, _dump_card(card)))

    if args.read:
        refs = [
            PropRef(did=did, siid=prop.siid, piid=prop.piid)
            for did, _, bound in targets
            for _, prop in bound
        ]
        if refs:
            print("\n---- 当前值（只读）----")
            values = client.read(refs, source="")
            for did, node, bound in targets:
                for slot, prop in bound:
                    rv = values.get(PropRef(did, prop.siid, prop.piid))
                    if rv is None:
                        text = "未返回"
                    elif not rv.ok:
                        text = f"失败 code={rv.code}"
                    else:
                        text = repr(rv.value)
                    print(f"  {node.name} · {slot} = {text}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
