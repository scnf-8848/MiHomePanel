# SPDX-License-Identifier: GPL-3.0-or-later
"""用户偏好：排序、隐藏设备、家庭标签、卡片样式。

卡片样式是「宽度 + 内容」的一个选择（`did → style_id`），不再单独存大小；
``card_styles`` 是稀疏字典，只记显式改过的设备。


云端给的家庭 / 房间 / 设备 / 场景顺序是它自己的逻辑（多半是创建时间），
用起来未必顺手。这里只做一件事：把"云端顺序"和"用户顺序"合成一份最终列表。

合并规则很简单，也是唯一一种不会丢条目的规则：

    用户排过的按用户顺序在前，没排过的（新增的设备、刚建的房间）按云端顺序追加。

这样即使换设备、加房间，也不会出现"新设备被藏起来找不到"。

隐藏设备是另一条独立偏好。被隐藏的设备不进入原房间，而是集中到每个家庭末尾的
"已隐藏设备"分组。它解决的是一个很实际的问题：56 台设备里真正天天要点的可能不到
二十台，剩下的网关、传感器、路由子设备铺在墙上只会稀释注意力。隐藏不是删除——
卡片还在、轮询范围也不变（隐藏分组里仍然能看实时状态），只是从主视图里挪开。

文件位置：``<data>/prefs.json``，和凭据、缓存放在一起。写入是原子的，掉了也不会
留下半个 JSON。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import paths

#: 隐藏分组（首页每个家庭末尾那一块）的显示名。用中文常量而不是枚举，因为它要
#: 直接画在界面上。叫「已隐藏设备」而不是「隐藏设备」：这一组里装的是**已经藏起来
#: 的**设备，是结果不是动作；「隐藏设备」读起来像右键菜单里那个动作，容易混。
#: 它只用于显示和 ``RoomView.room`` 标识——隐藏项的顺序走 ``order_hidden``
#: （``self.hidden`` 列表），不按房间名落盘，所以改名不会丢任何用户数据。
HIDDEN_ROOM = "已隐藏设备"

# 家庭切换栏的两种排列方式：
#   fixed        —— 位置只由用户设定的顺序决定，激活与否不动位置（靠样式区分）
#   active_first —— 激活的家庭自动提到最左，其余保持设定顺序
HOME_PLACEMENTS = ("fixed", "active_first")
HOME_PLACEMENT_DEFAULT = "fixed"

# 激活态的表达方式（见 ui/home_tabs.py 的绘制分支）
HOME_STYLES = ("size", "weight", "pill")
HOME_STYLE_DEFAULT = "size"

#: 卡片样式的默认 id（= 1×1 标准小卡）。样式自带宽度，所以这里不再有
#: 「大小」这个独立偏好。id 只做词法校验；哪些 id 真的存在由
#: ``ui/card_templates.py`` 的样式注册表决定，免得 core 反向依赖 ui。
CARD_STYLE_DEFAULT = "standard"

_SEP = "\x1f"  # 用作 (家庭, 房间) 组合键的分隔符，不会出现在正常名字里


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item)
        if text and text not in out:
            out.append(text)
    return out


def _order_map(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, items in value.items():
        texts = _str_list(items)
        if texts:
            out[str(key)] = texts
    return out


def _card_style_map(value: Any) -> dict[str, str]:
    """校验 ``card_styles``：``did → 样式 id``，坏数据整条丢掉而不是让界面崩。

    存的是**稀疏**字典：只有显式改过样式的设备才留一条，选回 ``standard``
    就删记录。这样以后调默认外观，没手动设过的卡片会自动跟上。

    v3 里一条记录是 ``{"span": 1|2, "template": "..."}``。大小与模板合并后
    ``span`` 作废（宽度由样式自带），``template`` 直接当样式 id 沿用；原来的
    「2×1 + 标准」在新版没有对应样式，退回默认 1×1 小卡——那种组合本来就
    只是一张更宽的空卡。
    """
    if not isinstance(value, dict):
        return {}
    sid = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
    out: dict[str, str] = {}
    for did, raw in value.items():
        key = str(did)
        if not key:
            continue
        # v4：直接就是样式 id
        if isinstance(raw, str):
            candidate = raw
        # v3：{"span": ..., "template": ...}，只认 template
        elif isinstance(raw, dict):
            candidate = raw.get("template")
        else:
            continue
        if isinstance(candidate, str) and sid.match(candidate) \
                and candidate != CARD_STYLE_DEFAULT:
            out[key] = candidate
    return out


def apply_order(order: Sequence[str], items: Iterable[str]) -> list[str]:
    """按 ``order`` 重排 ``items``；不在 order 里的追加在后面，保持原相对顺序。"""
    rank = {key: i for i, key in enumerate(order)}
    indexed = list(enumerate(items))
    indexed.sort(key=lambda pair: (rank.get(pair[1], len(order)), pair[0]))
    return [key for _, key in indexed]


class Prefs:
    """一份可读可写的偏好集合。所有方法都在主线程调用。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else paths.data_dir() / "prefs.json"
        self.home_order: list[str] = []
        self.room_order: dict[str, list[str]] = {}
        self.device_order: dict[str, list[str]] = {}
        self.scene_order: dict[str, list[str]] = {}
        self.hidden: list[str] = []
        self.hidden_scenes: list[str] = []
        self.home_placement = HOME_PLACEMENT_DEFAULT
        self.home_active_style = HOME_STYLE_DEFAULT
        #: did → 样式 id，只存显式改过的设备（默认 standard 不落盘）
        self.card_styles: dict[str, str] = {}
        #: 首页末尾「已隐藏设备」分组是否收起（只留标题行）
        self.hidden_collapsed = False
        self.load()

    # -- 落盘 ------------------------------------------------------------
    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        self.home_order = _str_list(data.get("home_order"))
        self.room_order = _order_map(data.get("room_order"))
        self.device_order = _order_map(data.get("device_order"))
        self.scene_order = _order_map(data.get("scene_order"))
        self.hidden = _str_list(data.get("hidden"))
        self.hidden_scenes = _str_list(data.get("hidden_scenes"))
        self.card_styles = _card_style_map(data.get("card_styles"))
        # 只认真正的 true：老文件里没有这个键，坏数据（"yes" / 1）也不该让分组
        # 一开就收起——那是用户看不到自己的设备，比"没记住"糟得多。
        self.hidden_collapsed = data.get("hidden_collapsed") is True
        layout = data.get("layout")
        if isinstance(layout, dict):
            placement = str(layout.get("home_placement") or "")
            style = str(layout.get("home_active_style") or "")
            if placement in HOME_PLACEMENTS:
                self.home_placement = placement
            if style in HOME_STYLES:
                self.home_active_style = style

    def save(self) -> None:
        payload = {
            "version": 4,
            "card_styles": self.card_styles,
            "home_order": self.home_order,
            "room_order": self.room_order,
            "device_order": self.device_order,
            "scene_order": self.scene_order,
            "hidden": self.hidden,
            "hidden_scenes": self.hidden_scenes,
            "hidden_collapsed": self.hidden_collapsed,
            "layout": {
                "home_placement": self.home_placement,
                "home_active_style": self.home_active_style,
            },
        }
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            # 偏好丢了不该影响使用，静默失败即可
            pass

    # -- 排序：读 --------------------------------------------------------
    def order_homes(self, names: Sequence[str]) -> list[str]:
        return apply_order(self.home_order, names)

    def order_rooms(self, home: str, names: Sequence[str]) -> list[str]:
        return apply_order(self.room_order.get(home, ()), names)

    def order_devices(self, home: str, room: str, dids: Sequence[str]) -> list[str]:
        return apply_order(self.device_order.get(self._room_key(home, room), ()), dids)

    def order_scenes(self, home: str, scene_ids: Sequence[str]) -> list[str]:
        return apply_order(self.scene_order.get(home, ()), scene_ids)

    def order_hidden(self, dids: Sequence[str]) -> list[str]:
        return apply_order(self.hidden, dids)

    # -- 排序：写 --------------------------------------------------------
    def set_home_order(self, names: Sequence[str]) -> None:
        self.home_order = list(names)
        self.save()

    def set_room_order(self, home: str, names: Sequence[str]) -> None:
        self.room_order[home] = list(names)
        self.save()

    def set_device_order(self, home: str, room: str, dids: Sequence[str]) -> None:
        self.device_order[self._room_key(home, room)] = list(dids)
        self.save()

    def set_scene_order(self, home: str, scene_ids: Sequence[str]) -> None:
        self.scene_order[home] = list(scene_ids)
        self.save()

    def set_orders(
        self,
        *,
        home_order: Sequence[str] | None = None,
        room_orders: dict[str, Sequence[str]] | None = None,
        device_orders: dict[tuple[str, str], Sequence[str]] | None = None,
        scene_orders: dict[str, Sequence[str]] | None = None,
    ) -> None:
        """一次写入多份顺序，只落一次盘。

        设置页的树是"拖一下重排整棵树"，如果逐层调用 ``set_*_order``，一次拖动会写
        十几遍 JSON、还会触发十几遍界面重建。这里合并成一次。
        """
        if home_order is not None:
            self.home_order = list(home_order)
        if room_orders:
            for home, names in room_orders.items():
                self.room_order[home] = list(names)
        if device_orders:
            for (home, room), dids in device_orders.items():
                self.device_order[self.room_key(home, room)] = list(dids)
        if scene_orders:
            for home, ids in scene_orders.items():
                self.scene_order[home] = list(ids)
        self.save()

    def reset_rooms(self, home: str) -> None:
        self.room_order.pop(home, None)
        self.save()

    def reset_devices(self, home: str, room: str) -> None:
        self.device_order.pop(self._room_key(home, room), None)
        self.save()

    # -- 家庭标签的排列 / 激活样式 ---------------------------------------
    def set_home_placement(self, value: str) -> bool:
        if value not in HOME_PLACEMENTS or value == self.home_placement:
            return False
        self.home_placement = value
        self.save()
        return True

    def set_home_active_style(self, value: str) -> bool:
        if value not in HOME_STYLES or value == self.home_active_style:
            return False
        self.home_active_style = value
        self.save()
        return True

    # -- 卡片外观：一个样式同时决定宽度与内容 -----------------------------
    def card_style(self, did: str) -> str | None:
        """这台设备**显式**选过的样式 id；没设过返回 ``None``。

        不能在这里兜底成 ``standard``：品类可以有自己声明的默认样式（人在
        传感器默认显示有人/无人），"没选过"和"显式选了标准"必须是两种状态，
        否则品类默认永远轮不到。兜底逻辑在 ``ui/card_templates.default_style``。
        """
        return self.card_styles.get(did)

    def set_card_style(
        self, did: str, style_id: str | None, default_id: str | None = None
    ) -> bool:
        """``None`` 表示回默认；``style_id == default_id`` 也不落盘。

        ``default_id`` 是该品类**当前**的默认样式（由调用方从 ``CardSpec`` 取）。
        在默认=标准的品类上点「标准」就等于回默认、删记录；而在默认是
        「状态」的存在传感器上点「标准」是一次真实的偏好，必须存下来——否则
        用户点了"标准"却又回到"状态"，菜单会像失灵一样。

        返回是否真的产生了变化，让调用方决定要不要局部刷新那张卡。
        """
        did = str(did)
        if not did:
            return False
        if style_id is not None and (
            not isinstance(style_id, str)
            or not re.match(r"^[a-z][a-z0-9_]{0,31}$", style_id)
        ):
            return False
        target = None if style_id in (None, default_id or CARD_STYLE_DEFAULT) else style_id
        if self.card_styles.get(did) == target:
            return False
        if target is None:
            self.card_styles.pop(did, None)
        else:
            self.card_styles[did] = target
        self.save()
        return True

    def clear_card_style(self, did: str) -> bool:
        """和 ``set_card_style(did, None)`` 等价，留给"恢复默认"这类调用方。"""
        return self.set_card_style(did, None)

    # -- 隐藏 ------------------------------------------------------------
    @property
    def hidden_set(self) -> set[str]:
        return set(self.hidden)

    def is_hidden(self, did: str) -> bool:
        return did in self.hidden

    def set_hidden(self, did: str, hidden: bool) -> bool:
        """返回是否真的产生了变化，方便界面判断要不要重建。"""
        did = str(did)
        if not did:
            return False
        if hidden:
            if did in self.hidden:
                return False
            self.hidden.append(did)
        else:
            if did not in self.hidden:
                return False
            self.hidden.remove(did)
        self.save()
        return True

    def clear_hidden(self) -> bool:
        if not self.hidden:
            return False
        self.hidden = []
        self.save()
        return True

    def set_hidden_collapsed(self, collapsed: bool) -> bool:
        """「已隐藏设备」分组收起 / 展开。返回是否真的变了，让界面决定要不要刷新。"""
        collapsed = bool(collapsed)
        if self.hidden_collapsed == collapsed:
            return False
        self.hidden_collapsed = collapsed
        self.save()
        return True

    # -- 隐藏：场景 ------------------------------------------------------
    # 场景和设备的隐藏分两个列表：场景没有房间、也不进"已隐藏设备"分组，
    # 混在一起会让人以为隐藏一个列表就同时管住了两种东西。
    @property
    def hidden_scene_set(self) -> set[str]:
        return set(self.hidden_scenes)

    def is_scene_hidden(self, scene_id: str) -> bool:
        return scene_id in self.hidden_scenes

    def set_scene_hidden(self, scene_id: str, hidden: bool) -> bool:
        scene_id = str(scene_id)
        if not scene_id:
            return False
        if hidden:
            if scene_id in self.hidden_scenes:
                return False
            self.hidden_scenes.append(scene_id)
        else:
            if scene_id not in self.hidden_scenes:
                return False
            self.hidden_scenes.remove(scene_id)
        self.save()
        return True

    def clear_hidden_scenes(self) -> bool:
        if not self.hidden_scenes:
            return False
        self.hidden_scenes = []
        self.save()
        return True

    # -- 内部 ------------------------------------------------------------
    @staticmethod
    def room_key(home: str, room: str) -> str:
        return f"{home}{_SEP}{room}"

    #: 老名字，内部还在用
    _room_key = room_key


__all__ = [
    "CARD_STYLE_DEFAULT",
    "HIDDEN_ROOM",
    "HOME_PLACEMENTS",
    "HOME_PLACEMENT_DEFAULT",
    "HOME_STYLES",
    "HOME_STYLE_DEFAULT",
    "Prefs",
    "apply_order",
]
