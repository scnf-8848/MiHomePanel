# SPDX-License-Identifier: GPL-3.0-or-later
"""验证「隐藏」的语义边界：**隐藏 ≠ 不存在**。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_hidden_scope.py``

用户 2026-11-24 定的口径：隐藏只决定"不在首页卡片墙上显示"，房间还在、
设备还在、读数照读、排序照留。这个口径已经被踩坏过两次（耗材页按房间分组
把同一间房拆成两组、设置页房间台数把整间房写成 0 台），所以单独立一份断言。

分六组：

1. **偏好层**——隐藏不动顺序表；恢复回原位；隐藏期间拖拽不抹掉隐藏项；
   换房间后恢复进新房间的表、旧表不留残影；显式重置仍然清顺序；
2. **Store 视图**——隐藏设备仍在 ``devices``；``rooms()`` 不含它（那是墙上
   视图）；``room_entries()`` / ``device_states()`` / ``home_devices()`` 仍
   含它；轮询照旧读它的属性；
3. **耗材页**——整间房设备全隐藏时，按房间分组仍给这间房留序号，卡片照出；
4. **侧栏**——全隐藏的房间不列（与墙一致，点进去是空白），但「已隐藏设备」
   那行在，「全部设备」的计数含隐藏；
5. **设置页台数**——按全部设备算，有隐藏的补一句，绝不出现「0 台」；
6. **场景**——默认滤隐藏、``include_hidden=True`` 全量（别在别处漏传）。

中文在 offscreen 平台渲染成豆腐块，这里只量数据与控件状态，不读文字。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from mihome.core import consumables as model  # noqa: E402
from mihome.core.api import HomeNode, RoomNode, Topology  # noqa: E402
from mihome.core.prefs import Prefs  # noqa: E402
from mihome.core.spec import PropertySpec  # noqa: E402
from mihome.core.store import DeviceState, Store, build_card_empty  # noqa: E402
from mihome.core.tree import DeviceNode  # noqa: E402
from mihome.ui import theme  # noqa: E402
from mihome.ui.consumables_page import ConsumablesPage  # noqa: E402
from mihome.ui.settings import room_count_note  # noqa: E402
from mihome.ui.side_nav import SideNav  # noqa: E402

FAILS: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {got!r}")
    if not ok:
        FAILS.append(f"{name} → 期望 {want!r}，实际 {got!r}")


def check_true(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(f"{name}{('  ' + detail) if detail else ''}")


class _Client:
    available = False

    def capabilities(self):
        return SimpleNamespace(save=lambda: None)


class _Worker:
    def submit(self, *args, **kwargs) -> None:
        pass


def _prop(siid: int, piid: int, label: str) -> PropertySpec:
    return PropertySpec(
        siid=siid, piid=piid, name=f"p{siid}_{piid}", urn=f"urn:test:{siid}:{piid}",
        description=label, comment=label, format="uint8", access=("read", "notify"),
        unit="%", value_range=(0, 100, 1), value_list=(), service="svc",
        service_label="服务", label_short=label,
    )


def make_store(tmp: Path, *, hidden: tuple[str, ...] = ()) -> Store:
    """三间房四台设备：客厅 2、厨房 1、阳台 1。厨房那台单独用来"整间房隐藏"。"""
    store = Store(_Client(), _Worker())  # type: ignore[arg-type]
    store.prefs = Prefs(tmp / "prefs.json")
    homes = [
        HomeNode(
            "h1", "我的家",
            [RoomNode("r1", "客厅"), RoomNode("r2", "厨房"), RoomNode("r3", "阳台")],
        )
    ]
    devices = [
        DeviceNode("d1", "门锁", "m", "lock.x", home="我的家", room="客厅",
                   home_id="h1", room_id="r1"),
        DeviceNode("d2", "电池", "m", "lock.x", home="我的家", room="客厅",
                   home_id="h1", room_id="r1"),
        DeviceNode("d3", "传感器", "m", "sensor.x", home="我的家", room="厨房",
                   home_id="h1", room_id="r2"),
        DeviceNode("d4", "晾衣架", "m", "rack.x", home="我的家", room="阳台",
                   home_id="h1", room_id="r3"),
    ]
    store.topology = Topology(homes, devices)
    specs = {
        "d1": [_prop(2, 9, "门锁电量")],
        "d2": [_prop(4, 1, "电池电量")],
        "d3": [_prop(3, 1, "滤芯剩余")],
        "d4": [_prop(3, 2, "耗材")],
    }
    for node in devices:
        props = specs[node.did]
        store.devices[node.did] = DeviceState(
            node=node,
            spec=SimpleNamespace(properties=props),
            card=build_card_empty(node),
            values={},
            keys={},
        )
    for did in hidden:
        store.prefs.hidden.append(did)
    store.prefs.save()
    return store


def key(home: str, room: str) -> str:
    return Prefs.room_key(home, room)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])  # noqa: F841
    tmp = Path(tempfile.mkdtemp(prefix="mihome-hidden-scope-"))

    print("\n1) 偏好层：隐藏不动顺序表，恢复回原位")
    p = Prefs(tmp / "a.json")
    p.set_device_order("h1", "r1", ["d1", "d2"])
    check("初始顺序", p.device_order[key("h1", "r1")], ["d1", "d2"])
    check("隐藏 d1 返回变化", p.set_hidden("d1", True), True)
    check("隐藏后顺序表原样保留", p.device_order[key("h1", "r1")], ["d1", "d2"])
    check("隐藏列表里有 d1", p.is_hidden("d1"), True)
    check("重复隐藏不写盘", p.set_hidden("d1", True), False)
    check("恢复返回变化", p.set_hidden("d1", False, home="h1", room="r1"), True)
    check("恢复后仍在原位（不是末尾）", p.device_order[key("h1", "r1")], ["d1", "d2"])

    print("\n1b) 隐藏期间拖拽：不抹掉隐藏项")
    p.set_device_order("h1", "r1", ["d2", "d1"])
    p.set_hidden("d2", True)
    # 墙上拖拽传的是可见列表（只有 d1）
    p.set_device_order("h1", "r1", ["d1"])
    check("隐藏项接在可见项后面", p.device_order[key("h1", "r1")], ["d1", "d2"])
    check("重读文件后仍保留", Prefs(tmp / "a.json").device_order[key("h1", "r1")],
          ["d1", "d2"])
    p.set_hidden("d2", False, home="h1", room="r1")
    check("恢复后顺序没跳", p.device_order[key("h1", "r1")], ["d1", "d2"])

    print("\n1c) 换过房间的设备：恢复进新房间，旧房间不留残影")
    p2 = Prefs(tmp / "b.json")
    p2.set_device_order("h1", "r1", ["d3", "d1"])
    p2.set_device_order("h1", "r2", ["d2"])
    p2.set_hidden("d1", True)
    p2.set_hidden("d1", False, home="h1", room="r2")
    check("新房间的表里有它", p2.device_order[key("h1", "r2")], ["d2", "d1"])
    check("旧房间表里没有它", "d1" in p2.device_order.get(key("h1", "r1"), []), False)

    print("\n1d) 显式重置仍然清顺序（隐藏与重置是两件事）")
    p3 = Prefs(tmp / "c.json")
    p3.set_device_order("h1", "r1", ["d1", "d2"])
    p3.set_hidden("d1", True)
    check("重置前顺序还在", p3.device_order[key("h1", "r1")], ["d1", "d2"])
    p3.reset_device("d1")
    check("重置设备清掉隐藏", p3.is_hidden("d1"), False)
    check("重置设备清掉顺序", p3.device_order.get(key("h1", "r1")), ["d2"])

    print("\n1e) 全部恢复显示：不重复追加")
    p4 = Prefs(tmp / "d.json")
    p4.set_device_order("h1", "r1", ["d2", "d1"])
    p4.set_hidden("d1", True)
    p4.append_device_orders([("h1", "r1", "d1")])
    check("批量恢复也只出现一次", p4.device_order[key("h1", "r1")], ["d2", "d1"])

    print("\n2) Store 视图：房间还在，设备还在")
    store = make_store(tmp, hidden=("d3",))
    check("隐藏设备仍在 devices", sorted(store.devices), ["d1", "d2", "d3", "d4"])
    views = store.rooms("h1")
    check("墙上分区（厨房整间隐藏 → 不出现）",
          [(v.kind, v.room_label, len(v.devices)) for v in views],
          [("room", "客厅", 2), ("room", "阳台", 1), ("hidden", "已隐藏设备", 1)])
    check("隐藏组的设备还是原来那台", [d.did for v in views if v.kind == "hidden"
                                       for d in v.devices], ["d3"])
    check("设备自己的房间没被抹掉", store.device_room("d3"), ("h1", "r2"))
    check("room_entries 仍列出厨房",
          [r for r, _ in store.room_entries("h1")], ["r1", "r2", "r3"])
    check("device_states 仍含隐藏设备",
          [d.did for d in store.device_states("h1", "r2")], ["d3"])
    check("home_devices 仍含隐藏设备",
          sorted(d.did for d in store.home_devices("h1")),
          ["d1", "d2", "d3", "d4"])

    print("\n2b) 读数照读：耗材属性仍在轮询里")
    store.prefs.add_consumable(
        model.parse({"did": "d3", "siid": 3, "piid": 1, "label": "滤芯", "warn": 20})
    )
    store.register_consumables()
    refs = {(r.did, r.siid, r.piid) for r in store.poll_refs()}
    check_true("隐藏设备的耗材属性被读", ("d3", 3, 1) in refs, f"（{sorted(refs)}）")
    check("隐藏不影响登记（keys 里有它）", (3, 1) in store.devices["d3"].keys, True)

    print("\n3) 耗材页：整间房隐藏也留序号、卡片照出")
    page = ConsumablesPage(store, theme.LIGHT)
    page.resize(900, 700)
    order = page._room_order()  # noqa: SLF001
    check("厨房仍有房间序号", ("h1", "r2") in order, True)
    check("三间房都编上号", sorted(order), [("h1", "r1"), ("h1", "r2"), ("h1", "r3")])
    page.refresh()
    check("隐藏设备的耗材照样出卡", [r.item.id for r in page._rows],  # noqa: SLF001
          ["d3:3.1"])
    page.deleteLater()

    print("\n4) 侧栏：不列空房间，但隐藏组与总数对得上")
    nav = SideNav(theme.LIGHT)
    nav.resize(220, 600)
    nav.build(store, "h1")
    rows = [(r.kind, r.label, r.count) for r in nav._rows]
    check("全隐藏的房间不列（与墙一致）",
          [label for kind, label, _ in rows if kind == "room"], ["客厅", "阳台"])
    check("「全部设备」计数含隐藏", [n for k, _, n in rows if k == "all"], [4])
    check("「已隐藏设备」那行在", [label for k, label, _ in rows if k == "hidden"],
          ["已隐藏设备"])
    nav.deleteLater()

    print("\n5) 设置页台数：按全部设备算，别写成 0 台")
    check("没有隐藏", room_count_note(store.device_states("h1", "r1"), store.prefs),
          "2 台")
    check("整间房隐藏仍报真实台数",
          room_count_note(store.device_states("h1", "r2"), store.prefs), "1 台 · 1 隐藏")
    store.prefs.set_hidden("d1", True)
    check("部分隐藏补一句",
          room_count_note(store.device_states("h1", "r1"), store.prefs), "2 台 · 1 隐藏")
    check("空房间才是 0 台（房间真的没设备）",
          room_count_note([], store.prefs), "0 台")

    print("\n6) 场景：默认滤隐藏，设置页要全量")
    store.scenes_by_home["h1"] = [
        {"scene_id": "s1", "name": "回家"},
        {"scene_id": "s2", "name": "睡觉"},
    ]
    store.prefs.set_scene_hidden("s2", True)
    check("首页只要可见的",
          [s["scene_id"] for s in store.scenes_for("h1")], ["s1"])
    check("设置页要全量（否则藏了就找不回来）",
          [s["scene_id"] for s in store.scenes_for("h1", include_hidden=True)],
          ["s1", "s2"])
    check("场景隐藏不动设备隐藏", store.prefs.hidden_set, {"d3", "d1"})

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败：")
        for item in FAILS:
            print(f"  - {item}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
