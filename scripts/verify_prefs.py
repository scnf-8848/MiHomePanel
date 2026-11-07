# SPDX-License-Identifier: GPL-3.0-or-later
"""验证"排序 + 隐藏设备"这条链路，不需要真实账号。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_prefs.py``

覆盖的是最容易被忽略的部分：用户顺序和云端顺序的合并、隐藏设备从原房间挪走、
偏好落盘后能读回来。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["MIHOME_DATA_DIR"] = tempfile.mkdtemp(prefix="mihome-prefs-")

from mihome.core.api import HomeNode, RoomNode, Topology  # noqa: E402
from mihome.core.prefs import HIDDEN_ROOM, Prefs, apply_order  # noqa: E402
from mihome.core.store import Store  # noqa: E402
from mihome.core.tree import DeviceNode  # noqa: E402

FAILS: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {got!r}")
    if not ok:
        FAILS.append(f"{name} → 期望 {want!r}，实际 {got!r}")


class _Caps:
    def save(self) -> None:
        pass


class _Client:
    capabilities = _Caps()


class _Worker:
    def submit(self, *args, **kwargs) -> None:
        pass

    def stop(self) -> None:
        pass


def build(topology: Topology, prefs: Prefs) -> Store:
    store = Store(_Client(), _Worker(), prefs=prefs)  # type: ignore[arg-type]
    store.apply_topology(topology, {})
    return store


def main() -> int:
    topo = Topology(
        homes=[
            HomeNode("h1", "示例家庭", [RoomNode("r1", "客厅"), RoomNode("r2", "卧室"), RoomNode("r3", "厨房")]),
            HomeNode("h2", "1000000002", [RoomNode("r4", "客厅")], shared=True),
        ],
        devices=[
            DeviceNode("d1", "灯1", "m1", "urn:1", home="示例家庭", room="客厅"),
            DeviceNode("d2", "灯2", "m1", "urn:1", home="示例家庭", room="客厅"),
            DeviceNode("d3", "插座", "m1", "urn:1", home="示例家庭", room="卧室"),
            DeviceNode("d4", "共享灯", "m1", "urn:1", home="1000000002", room="客厅"),
        ],
    )

    prefs = Prefs()
    store = build(topo, prefs)
    store.active_home = "示例家庭"
    changed = {"n": 0}
    store.on_layout_changed = lambda: changed.__setitem__("n", changed["n"] + 1)

    check("默认房间顺序跟随云端", [v.room for v in store.rooms("示例家庭")], ["客厅", "卧室"])
    check("默认家庭顺序跟随云端", [h.name for h in store.homes()], ["示例家庭", "1000000002"])
    check("共享家庭房间独立", [v.room for v in store.rooms("1000000002")], ["客厅"])

    # 房间重排：厨房没有设备，也要能排；排完顺序生效
    store.set_room_order("示例家庭", ["厨房", "卧室", "客厅"])
    check("房间按用户顺序", [v.room for v in store.rooms("示例家庭")], ["卧室", "客厅"])
    check("排序触发界面重建回调", changed["n"], 1)

    # 设备重排
    store.set_device_order("示例家庭", "客厅", ["d2", "d1"])
    check("设备按用户顺序", [d.did for v in store.rooms("示例家庭") if v.room == "客厅"
                              for d in v.devices], ["d2", "d1"])

    # 新设备没有排过 → 追加在末尾，不会被挤到最前面
    topo.devices.append(DeviceNode("d5", "新灯", "m1", "urn:1", home="示例家庭", room="客厅"))
    store.apply_topology(topo, {})
    check("新设备追加在末尾",
          [d.did for v in store.rooms("示例家庭") if v.room == "客厅" for d in v.devices],
          ["d2", "d1", "d5"])

    # 隐藏
    store.set_hidden("d1", True)
    views = store.rooms("示例家庭")
    check("隐藏设备离开原房间",
          [d.did for v in views if v.room == "客厅" for d in v.devices], ["d2", "d5"])
    check("隐藏分组在最后", views[-1].room, HIDDEN_ROOM)
    check("隐藏分组内容", [d.did for d in views[-1].devices], ["d1"])
    check("隐藏分组类型", views[-1].kind, "hidden")
    check("隐藏不影响其他家庭", [v.room for v in store.rooms("1000000002")], ["客厅"])

    store.set_hidden("d1", False)
    check("取消隐藏回到原房间",
          sorted(d.did for v in store.rooms("示例家庭") if v.room == "客厅" for d in v.devices),
          ["d1", "d2", "d5"])

    # 家庭顺序
    store.set_home_order(["1000000002", "示例家庭"])
    check("家庭按用户顺序", [h.name for h in store.homes()], ["1000000002", "示例家庭"])
    check("共享家庭显示名", store.home_label("1000000002"), "共享家庭")

    # 场景顺序
    store.scenes_by_home = {"示例家庭": [
        {"scene_id": "s1", "name": "回家"},
        {"scene_id": "s2", "name": "离家"},
        {"scene_id": "s3", "name": "全关"},
    ]}
    store.set_scene_order("示例家庭", ["s3"])
    check("场景按用户顺序且未排过的追加",
          [s["scene_id"] for s in store.scenes_for("示例家庭")], ["s3", "s1", "s2"])

    # 落盘 → 重新读
    again = Prefs(prefs.path)
    check("偏好落盘：家庭顺序", again.home_order, ["1000000002", "示例家庭"])
    check("偏好落盘：房间顺序", again.room_order.get("示例家庭"), ["厨房", "卧室", "客厅"])
    check("偏好落盘：场景顺序", again.scene_order.get("示例家庭"), ["s3"])

    # 家庭标签的排列 / 激活样式：只影响显示，绝不能改设定顺序
    check("默认排列方式", store.home_placement, "fixed")
    check("默认激活样式", store.home_active_style, "size")
    tabs_changed = {"n": 0}
    store.on_tabs_changed = lambda: tabs_changed.__setitem__("n", tabs_changed["n"] + 1)
    store.active_home = "示例家庭"
    store.set_home_layout(placement="active_first")
    check("改排列触发标签刷新", tabs_changed["n"], 1)
    check("排列方式已记住", Prefs(prefs.path).home_placement, "active_first")
    check("改排列不改设定顺序", store.prefs.home_order, ["1000000002", "示例家庭"])
    check("homes() 仍返回设定顺序", [h.name for h in store.homes()], ["1000000002", "示例家庭"])

    store.set_home_layout(placement="fixed", style="pill")
    check("改样式也触发刷新", tabs_changed["n"], 2)
    check("激活样式已记住", Prefs(prefs.path).home_active_style, "pill")
    check("非法值被忽略", store.prefs.set_home_placement("bogus"), False)
    check("非法值不落盘", Prefs(prefs.path).home_placement, "fixed")

    # 纯函数边界
    check("排序函数容忍未知项", apply_order(["b"], ["a", "b", "c"]), ["b", "a", "c"])
    check("排序函数容忍空顺序", apply_order([], ["a", "b"]), ["a", "b"])

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败：")
        for line in FAILS:
            print("  -", line)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
