"""运行时状态机：把"云端读数"变成"界面能直接画的卡片状态"。

这一层是界面和控制之间唯一的桥，负责四件事：

**1. 只轮询卡片上真的会显示的属性。**
全量读 1403 条可读属性要 5 个请求、约 2 秒；卡片属性只有约 150 条，一个请求
0.4 秒就够。所以轮询集合是 ``card.controls + card.metrics + gangs``，不是规格全集。
规格里剩下的一千多条（固件版本、故障码、各种内部开关）只在详情页打开时按需读。

**2. 乐观 UI + 版本号。**
点开关先改本地值让界面立刻响应，再发请求。失败回滚。同一个属性被连续操作时，
只有最后一次的回调有权改状态（``gen`` 版本号），否则会出现"快速点两下又弹回去"。

**3. 写入静默期。**
``api.Client`` 在写成功后会让该属性 3 秒内的回读失效。这一层配合保留乐观值，
避免滑块松手后弹回旧位置。

**4. 离线不轮询。**
``DeviceNode.online`` 为假的设备直接跳过，既省请求也避免"离线一次就记成不支持"。

状态存在 ``DeviceState.values`` 里，键是 ``(siid, piid)``——和 ``catalog.summary_line``
的接口对齐，渲染卡片时不用再做一次映射。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from .api import Client, HomeNode, PropRef, ReadValue, Topology, classify
from .catalog import CardSpec, build_card, summary_line
from .jobs import PRIORITY_CONTROL, PRIORITY_DATA, SerialWorker
from .prefs import HIDDEN_ROOM, Prefs
from .spec import DeviceSpec, SpecRepository
from .tree import DeviceNode

# 轮询时按"这个属性会不会主动上报"排序，但实测云端不区分，先只做去重。
POLL_INTERVAL_OK = 3.0  # 正常轮询间隔（秒）
POLL_INTERVAL_FAST = 1.5  # 用户刚操作过时的追帧间隔
POLL_BACKOFF = 6.0  # 连续失败后的退避


@dataclass
class DeviceState:
    """一台真机的运行时状态。"""

    node: DeviceNode
    spec: DeviceSpec | None
    card: CardSpec
    values: dict[tuple[int, int], Any] = field(default_factory=dict)
    keys: dict[tuple[int, int], PropRef] = field(default_factory=dict)
    optimistic: dict[tuple[int, int], Any] = field(default_factory=dict)
    gen: int = 0
    updated_at: float = 0.0  # 本地最后一次成功读到数据的时间
    error: str = ""
    missing: int = 0  # 连续轮询失败次数

    # -- 便捷查询 --------------------------------------------------------
    @property
    def did(self) -> str:
        return self.node.did

    @property
    def name(self) -> str:
        return self.node.name

    @property
    def online(self) -> bool:
        return self.node.online

    @property
    def room(self) -> str:
        return self.node.room

    def get(self, siid: int, piid: int) -> Any:
        return self.values.get((siid, piid))

    def get_prop(self, prop: Any) -> Any:
        if prop is None:
            return None
        return self.values.get((prop.siid, prop.piid))

    @property
    def power(self) -> bool | None:
        """卡片主开关的状态。None = 这台设备没有开关。"""
        card = self.card
        for c in card.controls:
            if c.slot == "power" and c.prop is not None:
                v = self.values.get(c.prop.key)
                return bool(v) if v is not None else None
        for g in card.gangs:
            v = self.values.get(g.power.key)
            if v:
                return True
        return False if card.gangs else None

    @property
    def any_on(self) -> bool:
        if self.card.gangs:
            return any(self.values.get(g.power.key) for g in self.card.gangs)
        return bool(self.power)

    @property
    def subtitle(self) -> str:
        return summary_line(self.card, self.values)

    @property
    def age(self) -> float:
        return time.monotonic() - self.updated_at if self.updated_at else 1e9


@dataclass
class RoomView:
    home: str
    home_label: str
    room: str
    devices: list[DeviceState] = field(default_factory=list)
    #: ``"room"`` 普通房间 / ``"lost"`` 未分组 / ``"hidden"`` 已隐藏设备分组
    kind: str = "room"


class Store:
    """卡片状态仓库。所有方法都在主线程调用。"""

    def __init__(
        self,
        client: Client,
        worker: SerialWorker,
        repo: SpecRepository | None = None,
        prefs: Prefs | None = None,
    ) -> None:
        self.client = client
        self.worker = worker
        self.repo = repo or SpecRepository()
        self.prefs = prefs or Prefs()
        self.topology = Topology()
        self.devices: dict[str, DeviceState] = {}
        self.scenes: list[dict[str, Any]] = []
        self.scenes_by_home: dict[str, list[dict[str, Any]]] = {}
        self.home_id = ""
        self.active_home = ""  # 当前正在看的家庭名（首页只有这一个家庭）
        self.loading = False
        self.polling = False
        self.last_poll = 0.0
        self.last_error = ""
        self.on_changed: Callable[[set[str]], None] | None = None
        self.on_loaded: Callable[[], None] | None = None
        self.on_status: Callable[[str], None] | None = None
        self.on_layout_changed: Callable[[], None] | None = None
        # 只影响顶部家庭标签外观的偏好（排列方式 / 激活样式）。单独一个回调，
        # 免得改个字号就把整面卡片墙重建一遍。
        self.on_tabs_changed: Callable[[], None] | None = None
        self._poll_generation = 0
        self._last_topology_change: set[str] = set()
        self._fail_streak = 0
        self._next_poll_at = 0.0

    # -- 加载 ------------------------------------------------------------
    def clear(self) -> None:
        """退出登录时把内存里的家庭 / 设备 / 场景全丢掉。

        不碰磁盘上的偏好（排序、隐藏设备还是用户的），下次登录回来顺序照旧。
        """
        self.devices.clear()
        self.topology = Topology()
        self.scenes = []
        self.scenes_by_home = {}
        self.active_home = ""
        self.home_id = ""
        self.loading = False
        self.last_error = ""
        if self.on_layout_changed:
            self.on_layout_changed()

    def load(self) -> None:
        """后台拉拓扑 + 规格 + 建卡片，完成后回调 ``on_loaded``。"""
        if self.loading:
            return
        self.loading = True
        self._status("正在连接米家…")
        self.worker.submit(self._load_job, priority=PRIORITY_CONTROL, label="load",
                           callback=self._on_loaded)

    def _load_job(self) -> tuple[Topology, dict[str, DeviceSpec], dict[str, list[dict[str, Any]]]]:
        topo = self.client.load_topology()
        urns = [d.spec_type for d in topo.real_devices()]
        specs = self.repo.get_many(urns, workers=4)
        # 场景是按家庭分开存的。两个家庭就是两次请求，启动时一起拿完，
        # 切家庭时就不用再等一次网络。
        scenes: dict[str, list[dict[str, Any]]] = {}
        for home in topo.homes:
            scenes[home.name] = self.client.scenes(home.id)
        return topo, specs, scenes

    def _on_loaded(self, result: Any, error: BaseException | None) -> None:
        self.loading = False
        if error is not None or result is None:
            self.last_error = f"加载失败：{error}"
            self._status(self.last_error)
            return
        topo, specs, scenes = result
        self.scenes_by_home = scenes
        self.home_id = topo.homes[0].id if topo.homes else ""
        self.apply_topology(topo, specs)
        names = [h.name for h in self.homes()]
        if self.active_home not in names:
            self.active_home = names[0] if names else ""
        self.scenes = self.scenes_for(self.active_home)
        self._status(f"已连接 · {len(self.devices)} 台设备")
        if self.on_loaded:
            self.on_loaded()

    def apply_topology(self, topo: Topology, specs: Mapping[str, DeviceSpec]) -> None:
        """用新拓扑重建设备表，尽量保留旧读数避免界面闪白。"""
        old = self.devices
        self.topology = topo
        devices: dict[str, DeviceState] = {}
        for node in topo.devices:
            spec = specs.get(node.spec_type) if node.spec_type else None
            prev = old.get(node.did)
            # 设备集合没变时复用同一个 DeviceState，界面就不用重建卡片
            if prev is not None and prev.node.spec_type == node.spec_type:
                prev.node = node
                if spec is not None and prev.spec is not spec:
                    prev.spec = spec
                    prev.card = build_card(
                        spec, gang_names=node.gang_names, gang_rooms=node.gang_rooms
                    )
                devices[node.did] = prev
                continue
            card = (
                build_card(spec, gang_names=node.gang_names, gang_rooms=node.gang_rooms)
                if spec
                else build_card_empty(node)
            )
            st = DeviceState(node=node, spec=spec, card=card)
            if prev is not None:
                st.values = {k: v for k, v in prev.values.items() if k in _card_keys(card)}
                st.keys = prev.keys
                st.updated_at = prev.updated_at
            # 预先登记卡片属性的完整寻址
            for key, prop in _card_props(card):
                st.keys[key] = PropRef(node.did, prop.siid, prop.piid)
            devices[node.did] = st
        # 结构变化（增删设备）+ 状态变化（在线/改名）都要让界面知道
        changed = set(old) ^ set(devices)
        for did, st in devices.items():
            prev = old.get(did)
            if prev is not None and (
                prev.node.online != st.node.online
                or prev.node.name != st.node.name
                or prev.node.room != st.node.room
            ):
                changed.add(did)
        self.devices = devices
        self.client.capabilities.save()
        self._last_topology_change = changed

    # -- 视图 ------------------------------------------------------------
    def homes(self) -> list[HomeNode]:
        """按用户偏好排过序的家庭。界面上的家庭切换栏直接用这个顺序。"""
        by_name = {h.name: h for h in self.topology.homes}
        return [by_name[name] for name in self.prefs.order_homes(list(by_name))]

    def home_label(self, name: str) -> str:
        for h in self.topology.homes:
            if h.name == name:
                return h.display_name
        return name or "我的家"

    def id_for_home(self, name: str) -> str:
        for h in self.topology.homes:
            if h.name == name:
                return h.id
        return ""

    def _home_of(self, state: DeviceState) -> str:
        """设备归属的家庭名。个别共享设备的 home 是空的，归到第一个家庭。"""
        if state.node.home:
            return state.node.home
        return self.topology.homes[0].name if self.topology.homes else ""

    def rooms(self, home: str | None = None) -> list[RoomView]:
        """输出某个家庭的 房间 → 设备，顺序是"用户排过的在前，其余跟云端"。

        ``hidden`` 分组的设备不会出现在自己的房间里，而是集中到末尾一组，
        这样卡片墙只留真正会去点的东西。
        """
        homes = self.homes()
        if home:
            homes = [h for h in homes if h.name == home]
        views: list[RoomView] = []
        for h in homes:
            names = [r.name for r in h.rooms]
            for room in self.prefs.order_rooms(h.name, names):
                items = [
                    d for d in self.devices.values()
                    if d.node.room == room
                    and self._home_of(d) == h.name
                    and d.did not in self.prefs.hidden_set
                ]
                items = self._ordered_devices(h.name, room, items)
                if items:
                    views.append(RoomView(h.name, h.display_name, room, items))
            lost = [
                d for d in self.devices.values()
                if not d.node.room
                and self._home_of(d) == h.name
                and d.did not in self.prefs.hidden_set
            ]
            if lost:
                views.append(RoomView(h.name, h.display_name, "未分组",
                                      self._ordered_devices(h.name, "未分组", lost), "lost"))
            hidden = self._hidden_devices(h.name)
            if hidden:
                views.append(RoomView(h.name, h.display_name, HIDDEN_ROOM, hidden, "hidden"))
        return views

    def _ordered_devices(self, home: str, room: str, items: list[DeviceState]) -> list[DeviceState]:
        order = self.prefs.order_devices(home, room, [d.did for d in items])
        by_did = {d.did: d for d in items}
        return [by_did[did] for did in order]

    def _hidden_devices(self, home: str) -> list[DeviceState]:
        items = [
            d for d in self.devices.values()
            if d.did in self.prefs.hidden_set and self._home_of(d) == home
        ]
        order = self.prefs.order_hidden([d.did for d in items])
        by_did = {d.did: d for d in items}
        return [by_did[did] for did in order]

    def room_names(self, home: str) -> list[str]:
        """配置页用：这个家庭下所有房间（含空房间与"未分组"）。"""
        node = next((h for h in self.topology.homes if h.name == home), None)
        names = [r.name for r in node.rooms] if node else []
        if any(self._home_of(d) == home and not d.node.room for d in self.devices.values()):
            names.append("未分组")
        return self.prefs.order_rooms(home, names)

    def device_states(self, home: str, room: str) -> list[DeviceState]:
        """配置页用：房间内全部设备（含被隐藏的），按当前顺序。"""
        if room == "未分组":
            items = [
                d for d in self.devices.values()
                if self._home_of(d) == home and not d.node.room
            ]
        else:
            items = [
                d for d in self.devices.values()
                if self._home_of(d) == home and d.node.room == room
            ]
        return self._ordered_devices(home, room, items)

    def home_devices(self, home: str) -> list[DeviceState]:
        items = [d for d in self.devices.values() if self._home_of(d) == home]
        return sorted(items, key=lambda d: (d.node.room, d.name))

    def scenes_for(self, home: str, *, include_hidden: bool = False) -> list[dict[str, Any]]:
        """按用户偏好排过的场景列表。未知的新场景追加在末尾。

        默认滤掉被隐藏的场景（首页那排胶囊用）；设置页要 ``include_hidden=True``，
        否则一旦隐藏就再也找不回来。
        """
        raw = self.scenes_by_home.get(home) or []
        order = self.prefs.order_scenes(home, [str(s.get("scene_id") or "") for s in raw])
        by_id = {str(s.get("scene_id") or ""): s for s in raw}
        hidden = self.prefs.hidden_scene_set
        out = []
        for sid in order:
            scene = by_id.get(sid)
            if scene is None:
                continue
            if not include_hidden and sid in hidden:
                continue
            out.append(scene)
        return out

    def home_names(self) -> list[str]:
        return [h.name for h in self.homes()]

    # -- 顶部家庭标签的排列 / 激活样式 -----------------------------------
    @property
    def home_placement(self) -> str:
        return self.prefs.home_placement

    @property
    def home_active_style(self) -> str:
        return self.prefs.home_active_style

    def set_home_layout(self, placement: str = "", style: str = "") -> None:
        """改的是"怎么显示"，不是"什么顺序"——设定顺序始终留在 ``prefs.home_order``。"""
        changed = False
        if placement:
            changed |= self.prefs.set_home_placement(placement)
        if style:
            changed |= self.prefs.set_home_active_style(style)
        if changed and self.on_tabs_changed:
            self.on_tabs_changed()

    # -- 卡片外观 --------------------------------------------------------
    def set_card_style(self, did: str, style_id: str | None = None) -> bool:
        """右键菜单改卡片样式（宽度随样式一起换）：只落盘，**不重建整面墙**。

        ``None`` 表示回默认，偏好里那条记录会被删掉。"默认"是哪一项由**品类**
        决定（``CardSpec.default_style``，存在传感器指向状态卡），所以要把
        ``default_id`` 一起交给 ``prefs``：在默认=标准的品类上点「标准」等于
        删记录，而在默认=状态的设备上点「标准」是一条真实偏好，必须存下来。

        设备集合和顺序都没变，走 ``_layout_changed()`` 会让滚动位置、鼠标悬停、
        正在拖动的滑条全部丢掉。调用方拿返回值按 did 局部刷新那一张卡即可
        （见 ``WallView.apply_card_style``）。
        """
        st = self.devices.get(did)
        return self.prefs.set_card_style(
            did, style_id, default_id=st.card.default_style if st is not None else None
        )

    # -- 排序与隐藏 ------------------------------------------------------
    def set_home_order(self, names: Sequence[str]) -> None:
        self.prefs.set_home_order(names)
        self._layout_changed()

    def set_room_order(self, home: str, names: Sequence[str]) -> None:
        self.prefs.set_room_order(home, names)
        self._layout_changed()

    def set_device_order(self, home: str, room: str, dids: Sequence[str]) -> None:
        self.prefs.set_device_order(home, room, dids)
        self._layout_changed()

    def set_scene_order(self, home: str, scene_ids: Sequence[str]) -> None:
        self.prefs.set_scene_order(home, scene_ids)
        self._layout_changed()

    def set_hidden(self, did: str, hidden: bool) -> None:
        if not self.prefs.set_hidden(did, hidden):
            return
        self._status("设备已隐藏，可在「设置 → 隐藏设备」里恢复" if hidden else "设备已恢复显示")
        self._layout_changed()

    def clear_hidden(self) -> None:
        if self.prefs.clear_hidden():
            self._status("已恢复全部隐藏设备")
            self._layout_changed()

    def set_scene_hidden(self, scene_id: str, hidden: bool) -> None:
        if not self.prefs.set_scene_hidden(scene_id, hidden):
            return
        self._status("场景已隐藏，可在「设置 → 隐藏设备」里恢复" if hidden else "场景已恢复显示")
        self._layout_changed()

    def apply_order_snapshot(
        self,
        *,
        homes: Sequence[str] | None = None,
        rooms: dict[str, Sequence[str]] | None = None,
        devices: dict[tuple[str, str], Sequence[str]] | None = None,
        scenes: dict[str, Sequence[str]] | None = None,
    ) -> None:
        """设置页拖完树之后一次性提交：只落一次盘、只重建一次界面。"""
        self.prefs.set_orders(
            home_order=homes, room_orders=rooms, device_orders=devices, scene_orders=scenes
        )
        self._layout_changed()

    def _layout_changed(self) -> None:
        if self.on_layout_changed:
            self.on_layout_changed()

    def refresh_layout(self) -> None:
        """外部直接改了 ``prefs``（例如"恢复默认顺序"）后，通知界面重建。"""
        self._layout_changed()


    def counts(self, devices: Iterable[DeviceState] | None = None) -> dict[str, int]:
        items = list(devices if devices is not None else self.devices.values())
        return {
            "total": len(items),
            "on": sum(1 for d in items if d.any_on),
            "offline": sum(1 for d in items if not d.online),
        }

    # -- 轮询 ------------------------------------------------------------
    def poll_refs(self) -> list[PropRef]:
        refs: list[PropRef] = []
        for st in self.devices.values():
            if not st.online or st.spec is None:
                continue
            for key in _card_keys(st.card):
                ref = st.keys.get(key)
                if ref is not None:
                    refs.append(ref)
        return refs

    def poll(self) -> None:
        """发起一轮读取。上一轮没回来就跳过，避免请求堆积。"""
        if self.polling or self.loading or not self.devices:
            return
        # 连续失败后指数退避：网络抖动时不要每 3 秒空打一次请求
        if time.monotonic() < self._next_poll_at:
            return
        refs = self.poll_refs()
        if not refs:
            return
        self.polling = True
        gen = self._poll_generation
        self.worker.submit(
            self.client.read,
            (refs,),
            priority=PRIORITY_DATA,
            label="poll",
            callback=lambda res, err: self._on_polled(gen, res, err),
        )

    def _on_polled(self, gen: int, result: Any, error: BaseException | None) -> None:
        self.polling = False
        if gen != self._poll_generation:  # 拓扑已重建，这批结果作废
            return
        self.last_poll = time.monotonic()
        if error is not None or not isinstance(result, Mapping):
            self._fail_streak += 1
            backoff = min(POLL_BACKOFF * self._fail_streak, 30.0)
            self._next_poll_at = time.monotonic() + backoff
            self.last_error = f"轮询失败：{error}"
            if self._fail_streak == 1:
                self._status(f"连接不稳定，{int(backoff)} 秒后重试…")
            return
        if self._fail_streak:
            self._fail_streak = 0
            self._next_poll_at = 0.0
            self._status("已重新连接")
        changed: set[str] = set()
        now = time.monotonic()
        for ref, rv in result.items():  # type: ignore[union-attr]
            if not isinstance(rv, ReadValue):
                continue
            st = self.devices.get(ref.did)
            if st is None:
                continue
            key = (ref.siid, ref.piid)
            if not rv.ok:
                if rv.unsupported:
                    st.missing += 1
                continue
            st.updated_at = now
            st.missing = 0
            if key in st.optimistic:
                # 静默期的双保险：api 层已过滤，这里再挡一次
                continue
            if st.values.get(key) != rv.value:
                st.values[key] = rv.value
                changed.add(ref.did)
        if changed:
            self._changed(changed)

    def reraise(self, did: str) -> None:
        """用户手动操作后立刻追加一轮，体感快一些。"""
        st = self.devices.get(did)
        if st is None or not st.online:
            return
        refs = [st.keys[k] for k in _card_keys(st.card) if k in st.keys]
        if not refs:
            return
        self.worker.submit(
            self.client.read,
            (refs,),
            priority=PRIORITY_DATA,
            label="refresh",
            callback=lambda res, err: self._on_polled(self._poll_generation, res, err),
        )

    # -- 写入 ------------------------------------------------------------
    def set_value(self, did: str, key: tuple[int, int], value: Any, *, mute: float | None = None) -> bool:
        """乐观写入。返回 False 表示这台设备当前不可控。"""
        st = self.devices.get(did)
        if st is None or not st.node.spec_type:
            return False
        ref = st.keys.get(key)
        if ref is None:
            return False

        old = st.values.get(key)
        st.gen += 1
        gen = st.gen
        st.values[key] = value
        st.optimistic[key] = value
        self._changed({did})

        def done(res: Any, err: BaseException | None) -> None:
            self._on_written(did, key, old, gen, res, err, mute)

        self.worker.submit(
            self.client.write,
            ([(ref, value)],),
            kwargs={"mute": mute},
            priority=PRIORITY_CONTROL,
            label="write",
            callback=done,
        )
        return True

    def _on_written(
        self,
        did: str,
        key: tuple[int, int],
        old: Any,
        gen: int,
        result: Any,
        error: BaseException | None,
        mute: float | None,
    ) -> None:
        st = self.devices.get(did)
        if st is None:
            return
        ok = False
        if error is None and isinstance(result, Mapping):
            from .api import classify

            ok = all(classify(int(c)) == "ok" for c in result.values()) if result else False
        if gen != st.gen:
            # 期间用户又操作了同一台设备，交给最后一次回调决定
            return
        st.optimistic.pop(key, None)
        if not ok:
            st.values[key] = old
            st.error = "操作失败，已还原"
            self._changed({did})
            return
        st.error = ""
        self._changed({did})

    def set_power_for_devices(self, dids: Sequence[str], value: Any) -> int:
        """把一组设备的电源键一次性写到同一个值，返回实际下发的属性条数。

        界面上的「房间全开 / 全关」已经去掉了（一个房间品类太杂，一刀切没有意义），
        这里留着是因为"批量关一组设备"仍是自动化动作的基础能力，并且有
        ``scripts/verify_batch.py`` 覆盖着——一次请求打多条属性、失败逐条回滚。
        """
        targets: list[tuple[str, tuple[int, int]]] = []
        for did in dids:
            st = self.devices.get(did)
            if st is None:
                continue
            for ctl in st.card.controls:
                if ctl.slot == "power" and ctl.prop is not None:
                    targets.append((did, ctl.prop.key))
            for g in st.card.gangs:
                targets.append((did, g.power.key))
        return self.set_many(targets, value, mute=2.0)

    def set_many(
        self,
        targets: Sequence[tuple[str, tuple[int, int]]],
        value: Any,
        *,
        mute: float | None = 2.0,
    ) -> int:
        """一次请求写多条属性（乐观 UI 仍然逐条记版本号）。"""
        pairs: list[tuple[PropRef, Any]] = []
        plan: list[tuple[str, tuple[int, int], Any, int]] = []
        pending: set[str] = set()
        for did, key in targets:
            st = self.devices.get(did)
            if st is None or not st.online or not st.node.spec_type:
                continue
            ref = st.keys.get(key)
            if ref is None:
                continue
            old = st.values.get(key)
            if old == value:
                continue
            st.gen += 1
            st.values[key] = value
            st.optimistic[key] = value
            plan.append((did, key, old, st.gen))
            pairs.append((ref, value))
            pending.add(did)
        if not pairs:
            return 0
        self._changed(pending)

        def done(result: Any, error: BaseException | None) -> None:
            ok = (
                error is None
                and isinstance(result, Mapping)
                and bool(result)
                and all(classify(int(c)) == "ok" for c in result.values())
            )
            touched: set[str] = set()
            for did, key, old, gen in plan:
                st = self.devices.get(did)
                if st is None or gen != st.gen:
                    continue
                st.optimistic.pop(key, None)
                if not ok:
                    st.values[key] = old
                    st.error = "操作失败，已还原"
                touched.add(did)
            self._changed(touched)
            self._status(
                f"已{'开启' if value else '关闭'} {len(pairs)} 项"
                if ok
                else "批量操作失败，已还原"
            )

        self.worker.submit(
            self.client.write,
            (pairs,),
            kwargs={"mute": mute},
            priority=PRIORITY_CONTROL,
            label="write-many",
            callback=done,
        )
        return len(pairs)

    def toggle(self, did: str, siid: int, piid: int) -> None:
        st = self.devices.get(did)
        if st is None:
            return
        cur = st.values.get((siid, piid))
        self.set_value(did, (siid, piid), not bool(cur), mute=2.0)

    def run_action(self, did: str, siid: int, aiid: int, value: Sequence[Any] | None = None) -> None:
        def done(res: Any, err: BaseException | None) -> None:
            if err is not None:
                st = self.devices.get(did)
                if st:
                    st.error = f"动作失败：{err}"
                    self._changed({did})
            else:
                self.reraise(did)

        self.worker.submit(
            self.client.run_action,
            ((did, siid, aiid), value),
            priority=PRIORITY_CONTROL,
            label="action",
            callback=done,
        )

    def run_scene(self, scene_id: str, home: str = "") -> None:
        self.worker.submit(
            self.client.run_scene,
            (scene_id, self.id_for_home(home) or self.home_id),
            priority=PRIORITY_CONTROL,
            label="scene",
            callback=self._on_scene_done,
        )

    def _on_scene_done(self, result: Any, error: BaseException | None) -> None:
        self._status("场景执行失败" if error or result is False else "场景已执行")
        # 场景会改动一批设备，等它生效后再追一轮
        self.worker.submit(lambda: time.sleep(1.2), priority=PRIORITY_DATA, callback=lambda *_: self.poll())

    # -- 杂务 ------------------------------------------------------------
    @property
    def topology_changed(self) -> set[str]:
        """上一次加载中增删或状态变了的设备。空集说明只需要局部刷新卡片。"""
        return self._last_topology_change

    def rebuild(self) -> None:
        """拓扑变了（新增/删除设备）后重新加载规格并换代。"""
        self._poll_generation += 1
        self.polling = False
        self.load()

    def _changed(self, dids: set[str]) -> None:
        if self.on_changed and dids:
            self.on_changed(dids)

    def _status(self, text: str) -> None:
        if self.on_status:
            self.on_status(text)


# ---------------------------------------------------------------------------
# 卡片属性的收集
# ---------------------------------------------------------------------------


def _card_props(card: CardSpec) -> list[tuple[tuple[int, int], Any]]:
    out: list[tuple[tuple[int, int], Any]] = []
    for c in card.controls:
        if c.prop is not None:
            out.append((c.prop.key, c.prop))
    for m in card.metrics:
        if m.prop is not None:
            out.append((m.prop.key, m.prop))
    for g in card.gangs:
        out.append((g.power.key, g.power))
    return out


def _card_keys(card: CardSpec) -> set[tuple[int, int]]:
    return {key for key, _ in _card_props(card)}


def build_card_empty(node: DeviceNode) -> CardSpec:
    """没有 spec_type 的设备（如手环）：只展示，不给控件。"""
    from .catalog import GENERIC

    return CardSpec(category="unknown", profile=GENERIC)


__all__ = ["DeviceState", "RoomView", "Store", "build_card_empty"]
