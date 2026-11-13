"""设备树：把云端的"设备条目"整理成"真机 + 虚拟子设备"。

云端返回的 64 条设备里只有 56 台真机。多键面板会额外派生出 did 形如
``<父did>.s3`` 的虚拟条目——这里的 ``.sN`` 的 N **就是规格里的 siid**，也就是
面板的第 N 路。它们不是独立设备：

* 不能对子 did 发读写请求（实测拖到 6 秒后返回 ``-704083036``）；
* 但它们的 ``name`` 是用户在 App 里给这一路起的名字（"厨房灯"），这才是卡片上
  该显示的标签；
* 它们的 ``room`` 可能与父设备不同（实测 ``2002710290.s3`` 在厨房、父在客厅）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

_VIRTUAL_RE = re.compile(r"^(?P<parent>.+)\.s(?P<siid>\d+)$")


def parse_virtual_did(did: str) -> tuple[str, int] | None:
    m = _VIRTUAL_RE.match(did or "")
    if not m:
        return None
    return m.group("parent"), int(m.group("siid"))


#: 未分配到任何房间的设备的稳定占位值。云端家庭 / 房间 id 都是数字串，这个值
#: 不会和真实 id 撞车；排序、分组、显示三处必须统一用它，不能用 0 冒充云端 id。
UNASSIGNED_ROOM_ID = "__unassigned__"
UNASSIGNED_ROOM_LABEL = "未分组"


@dataclass(frozen=True, slots=True)
class VirtualChild:
    did: str
    siid: int
    name: str
    room: str
    home_id: str = ""
    room_id: str = ""
    model: str = ""


@dataclass
class DeviceNode:
    did: str
    name: str
    model: str
    spec_type: str
    home: str = ""
    room: str = ""
    home_id: str = ""
    room_id: str = ""
    online: bool = True
    spec_name: str = ""
    children: list[VirtualChild] = field(default_factory=list)
    # 局域网地址与设备 token：摄像头取流要用（go2rtc 的 xiaomi 源按 localip 直连），
    # 其余品类暂时用不上。token 不进 repr，免得排障日志把它带出去。
    localip: str = ""
    token: str = field(default="", repr=False)

    @property
    def gang_names(self) -> dict[int, str]:
        return {c.siid: c.name for c in self.children}

    @property
    def gang_rooms(self) -> dict[int, str]:
        return {c.siid: c.room for c in self.children}

    @property
    def room_span(self) -> set[str]:
        """这棵子树牵涉到的房间（多键面板可能跨房间）。"""
        rooms = {self.room} if self.room else set()
        rooms |= {c.room for c in self.children if c.room}
        return rooms

    @property
    def controllable(self) -> bool:
        return bool(self.spec_type)


def group_devices(rows: Iterable[Mapping[str, Any]]) -> list[DeviceNode]:
    """按 did 分组，返回真机节点（含各自虚拟子设备）。保持云端返回顺序。"""
    rows = list(rows)
    parents: dict[str, DeviceNode] = {}
    order: list[str] = []
    pending: list[tuple[str, VirtualChild]] = []

    for r in rows:
        did = str(r.get("did", ""))
        if not did:
            continue
        virt = parse_virtual_did(did)
        if virt is None:
            node = DeviceNode(
                did=did,
                name=r.get("name", "") or did,
                model=r.get("model", "") or "",
                spec_type=r.get("spec_type", "") or "",
                home=r.get("home", "") or "",
                room=r.get("room", "") or "",
                home_id=r.get("home_id", "") or "",
                room_id=r.get("room_id", "") or UNASSIGNED_ROOM_ID,
                online=bool(r.get("online", True)),
                spec_name=r.get("spec_name", "") or "",
                localip=r.get("localip", "") or "",
                token=r.get("token", "") or "",
            )
            parents[did] = node
            order.append(did)
        else:
            parent_did, siid = virt
            pending.append(
                (
                    parent_did,
                    VirtualChild(
                        did=did,
                        siid=siid,
                        name=r.get("name", "") or "",
                        room=r.get("room", "") or "",
                        home_id=r.get("home_id", "") or "",
                        room_id=r.get("room_id", "") or UNASSIGNED_ROOM_ID,
                        model=r.get("model", "") or "",
                    ),
                )
            )

    orphans: list[DeviceNode] = []
    for parent_did, child in pending:
        node = parents.get(parent_did)
        if node is None:
            # 父不在列表里：当成独立设备，避免丢数据
            node = DeviceNode(
                did=parent_did,
                name=child.name or parent_did,
                model=child.model,
                spec_type="",
                room=child.room,
                home_id=child.home_id,
                room_id=child.room_id,
                online=True,
            )
            parents[parent_did] = node
            order.append(parent_did)
            orphans.append(node)
        node.children.append(child)

    return [parents[d] for d in order]


__all__ = [
    "DeviceNode",
    "UNASSIGNED_ROOM_ID",
    "UNASSIGNED_ROOM_LABEL",
    "VirtualChild",
    "group_devices",
    "parse_virtual_did",
]
