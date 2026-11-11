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

from .api import Client, HomeNode, PropRef, ReadValue, Topology, classify, explain
from .catalog import CardSpec, build_card, summary_line
from .geek import Entry as GeekEntry
from .geek import GeekError
from .geek import candidates as geek_candidates
from .geek import fetch as fetch_geek_entry
from .geek import probe as geek_probe
from .geek import remember as geek_remember
from .jobs import PRIORITY_CONTROL, PRIORITY_DATA, SerialWorker
from .prefs import HIDDEN_ROOM, Prefs
from .spec import DeviceSpec, PropertySpec, SpecRepository, localize
from .tree import DeviceNode

# 轮询的**频率**不在这里：它由"谁在看"决定（`ui/main_window.py` 的 `POLL_MS` /
# `POLL_HIDDEN_MS`），Store 只负责"该发的时候发、上一轮没回来就跳过"。
# 曾经这里还放着 POLL_INTERVAL_OK/FAST 两个数值常量，没有任何人读——留着等于第二份
# 事实来源，改频率时容易只改一处。删掉（2026-11-10）。
POLL_BACKOFF = 6.0  # 连续失败后的退避基数


@dataclass
class DeviceState:
    """一台真机的运行时状态。"""

    node: DeviceNode
    spec: DeviceSpec | None
    card: CardSpec
    values: dict[tuple[int, int], Any] = field(default_factory=dict)
    keys: dict[tuple[int, int], PropRef] = field(default_factory=dict)
    optimistic: dict[tuple[int, int], Any] = field(default_factory=dict)
    #: 每个属性**值最后一次变化**的云端时间戳（epoch 秒，来自 ``updateTime``）。
    #: 只用于详情页"N 天未更新"的提示（判据见 ``core/freshness.py``），不参与取值逻辑；
    #: 没有这个字段的属性（静态元数据）不会进来，界面据此不提示。
    stamps: dict[tuple[int, int], int] = field(default_factory=dict)
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

    def mode_active_value(self) -> Any:
        """型号适配后的"模式激活值"；没适配（或还没读到）返回 ``None``。

        界面判断模式高亮统一走这里：``None`` 表示该走默认路径（用 ``mode``
        自己的值），不要自己拼 siid/piid 去 ``values`` 里查。
        """
        return self.get_prop(self.card.mode_active)

    @property
    def power(self) -> bool | None:
        """卡片主开关的状态。None = 这台设备没有开关。"""
        card = self.card
        for c in card.controls:
            if c.slot == "power" and c.prop is not None:
                v = self.values.get(c.prop.key)
                return bool(v) if v is not None else None
        for g in card.gangs:
            if g.is_wireless(self.values):
                # 转无线的路继电器恒通，但那是"给灯供电"，不是"这盏灯开着"：
                # 拿它算"已开启"，整块面板会永远显示开着，房间标题的台数也虚高。
                continue
            v = self.values.get(g.power.key)
            if v:
                return True
        return False if card.gangs else None

    @property
    def any_on(self) -> bool:
        if self.card.gangs:
            # 全部转无线的面板没有可切换的负载，不该被算成"开着"
            return any(
                self.values.get(g.power.key)
                for g in self.card.gangs
                if not g.is_wireless(self.values)
            )
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
        # 每轮轮询结束后回调一次（主线程），参数是 `PollHealth.snapshot()`。
        # 界面拿它决定要不要弹提醒——Store 自己不判断"该不该打扰用户"。
        self.on_poll_health: Callable[[dict[str, Any]], None] | None = None
        # 某台设备的「有没有极客版」探出来了（参数是 did）。界面拿它刷菜单提示，
        # Store 自己不改界面。
        self.on_geek_ability: Callable[[str], None] | None = None
        #: 刚**发出**一条写请求（主线程，乐观值已经落到 ``st.values`` 里）。
        #:
        #: 界面拿它开"指令后高频轮询"窗口：写入是乐观的，屏幕上显示的就是目标值，
        #: 真正要确认的是设备有没有照做——那必须靠回读。这个回调只说明"有事发生了"，
        #: 具体多快、持续多久由界面决定（Store 不该知道轮询频率）。
        #: 放在这里而不是各个 UI 入口：写入口有卡片、详情页、场景、批量、音箱指令
        #: 好几处，逐处接线一定会漏。
        self.on_write_sent: Callable[[], None] | None = None
        self._poll_generation = 0
        self._last_topology_change: set[str] = set()
        # (URN, 型号) → (源规格, 套了型号级文案的规格)。规格对象换了就重算；
        # 没换就复用同一个对象，否则 ``prev.spec is not spec`` 每次都为真，
        # 一次拓扑刷新会把所有卡片重建成一遍。
        self._text_specs: dict[tuple[str, str], tuple[DeviceSpec, DeviceSpec]] = {}
        self._fail_streak = 0
        self._next_poll_at = 0.0
        #: 本轮已经派出去、还没回来的能力探测（同一台别重复提交）
        self._geek_pending: set[str] = set()

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
        self._geek_pending.clear()
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
        # 设备表齐了才谈得上"给谁探能力"。放在 on_loaded 之后：先把界面铺出来，
        # 探测走低优先级队列，不和首屏抢带宽。
        self.scan_geek_abilities()

    def _text_spec(self, node: DeviceNode, specs: Mapping[str, DeviceSpec]) -> DeviceSpec | None:
        """这台设备的规格，套上**型号级**文案覆盖；没登记就是原对象。

        类别级文案在解析期就进了短名（URN 自带 category，缓存天然隔离），只有型号级
        要在这里补：规格缓存按 URN 共享，而 URN 里的型号段（``yeelink-lamp22``）和
        ``DeviceNode.model``（``yeelink.light.lamp22``）不是一个东西。

        结果按 (URN, 型号) 记住：源规格没换就复用同一个对象，``apply_topology`` 里
        ``prev.spec is not spec`` 才不会每次都为真（否则一次拓扑刷新会把所有卡片重建）。
        """
        spec = specs.get(node.spec_type) if node.spec_type else None
        if spec is None:
            return None
        key = (node.spec_type or "", node.model or "")
        cached = self._text_specs.get(key)
        if cached is not None and cached[0] is spec:
            return cached[1]
        localized = localize(spec, node.model)
        self._text_specs[key] = (spec, localized)
        return localized

    def apply_topology(self, topo: Topology, specs: Mapping[str, DeviceSpec]) -> None:
        """用新拓扑重建设备表，尽量保留旧读数避免界面闪白。"""
        old = self.devices
        self.topology = topo
        devices: dict[str, DeviceState] = {}
        for node in topo.devices:
            spec = self._text_spec(node, specs)
            prev = old.get(node.did)
            # 设备集合没变时复用同一个 DeviceState，界面就不用重建卡片
            if prev is not None and prev.node.spec_type == node.spec_type:
                prev.node = node
                if spec is not None and prev.spec is not spec:
                    prev.spec = spec
                    prev.card = build_card(
                        spec, did=node.did, model=node.model,
                        gang_names=node.gang_names, gang_rooms=node.gang_rooms
                    )
                _register_keys(prev, node.did)
                devices[node.did] = prev
                continue
            card = (
                build_card(
                    spec, did=node.did, model=node.model,
                    gang_names=node.gang_names, gang_rooms=node.gang_rooms
                )
                if spec
                else build_card_empty(node)
            )
            st = DeviceState(node=node, spec=spec, card=card)
            if prev is not None:
                st.values = {k: v for k, v in prev.values.items() if k in _card_keys(card)}
                st.keys = prev.keys
                st.stamps = {k: v for k, v in prev.stamps.items() if k in _card_keys(card)}
                st.updated_at = prev.updated_at
            # 预先登记卡片属性的完整寻址
            _register_keys(st, node.did)
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

    def device_room(self, did: str) -> tuple[str, str] | None:
        """设备所在的 (家庭, 房间)；没有房间的设备记作「未分组」。

        隐藏 / 恢复时要把 did 追加到**它自己的房间**末尾，``Prefs`` 不知道
        设备归属，只能由这里解析。设备还没加载出来时返回 ``None``。
        """
        st = self.devices.get(did)
        if st is None:
            return None
        return self._home_of(st), (st.node.room or "未分组")

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

    # -- 摄像头取流方式 --------------------------------------------------
    @property
    def camera_network(self) -> str:
        return self.prefs.camera_network

    def set_camera_network(self, value: str) -> bool:
        return self.prefs.set_camera_network(value)

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

    def set_device_order(
        self, home: str, room: str, dids: Sequence[str], *, notify: bool = True
    ) -> None:
        """写入房间内的卡片顺序（只含可见设备）。

        主界面拖动排序时传 ``notify=False``：布局项已经当场挪好了，再广播一次
        ``on_layout_changed`` 会把整面墙重建，滚动位置和刚放下的卡片都会跳。
        隐藏设备不在顺序表里，这一点由 ``Prefs`` 的写入口统一保证。
        """
        self.prefs.set_device_order(home, room, dids)
        if notify:
            self._layout_changed()

    def set_scene_order(self, home: str, scene_ids: Sequence[str]) -> None:
        self.prefs.set_scene_order(home, scene_ids)
        self._layout_changed()

    def set_hidden(self, did: str, hidden: bool) -> None:
        # 恢复显示时要把 did 追加回它所在房间的末尾；隐藏时 Prefs 会把它
        # 从顺序表里剔除，保证「顺序表 = 可见设备」。
        slot = self.device_room(did) if not hidden else None
        home, room = slot if slot is not None else ("", "")
        if not self.prefs.set_hidden(did, hidden, home=home, room=room):
            return
        self._status("设备已隐藏，可在「设置 → 隐藏设备」里恢复" if hidden else "设备已恢复显示")
        self._layout_changed()

    def clear_hidden(self) -> None:
        hidden = list(self.prefs.hidden)
        if not self.prefs.clear_hidden():
            return
        # 全部恢复显示：逐台追加到各自房间末尾（与单台恢复同一口径）。
        slots: list[tuple[str, str, str]] = []
        for did in hidden:
            slot = self.device_room(did)
            if slot is not None:
                slots.append((slot[0], slot[1], did))
        if slots:
            self.prefs.append_device_orders(slots)
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
            # 只有属性轮询进健康账本；写后补读、详情页按需读不传 source
            kwargs={"source": "poll"},
            priority=PRIORITY_DATA,
            label="poll",
            callback=lambda res, err: self._on_polled(gen, res, err),
        )

    def _on_polled(self, gen: int, result: Any, error: BaseException | None) -> None:
        self.polling = False
        if gen != self._poll_generation:  # 拓扑已重建，这批结果作废
            return
        self.last_poll = time.monotonic()
        try:
            self._apply_poll(result, error)
        finally:
            # 每轮结束都把健康快照交给界面。放 finally 里，失败分支也不会漏。
            if self.on_poll_health is not None:
                self.on_poll_health(self.client.health.snapshot())

    def _apply_poll(self, result: Any, error: BaseException | None) -> None:
        # 判据是"这轮到底读回来东西没有"，不是"返回值是不是 Mapping"：
        # 整轮网络失败时 ``read`` 返回的是空 dict，那也是 Mapping，以前会被判成功，
        # 于是退避永远不触发、界面还显示"已重新连接"。空读（没有可读属性 /
        # 全部确认不支持）由 ``Client.last_poll_ok`` 区分为正常。
        if (
            error is not None
            or not isinstance(result, Mapping)
            or self.client.last_poll_ok is False
        ):
            self._fail_streak += 1
            backoff = min(POLL_BACKOFF * self._fail_streak, 30.0)
            self._next_poll_at = time.monotonic() + backoff
            reason = error or self.client.last_error or "本轮没有读回任何属性"
            self.last_error = f"轮询失败：{reason}"
            # 退避的起始时间与当前间隔一并记进健康账本（不改变退避本身的计算）
            self.client.health.backoff(streak=self._fail_streak, seconds=backoff)
            if self._fail_streak == 1:
                self._status(f"连接不稳定，{int(backoff)} 秒后重试…")
            return
        if self._fail_streak:
            self._fail_streak = 0
            self._next_poll_at = 0.0
            self.client.health.backoff(streak=0, seconds=0.0)
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
            if rv.update_time:
                st.stamps[key] = rv.update_time
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

    def read_props(
        self,
        did: str,
        props: Iterable[PropertySpec],
        done: Callable[[dict[tuple[int, int], ReadValue], str], None],
    ) -> None:
        """按需读一批不在轮询里的属性（详情页「更多功能」）。

        这些属性平时不参与轮询：设备多的时候全量读一次要两秒。只有用户真的展开
        「更多功能」才发这一批请求，读完照常回填 ``values`` 并通知界面刷新。
        ``done`` 在主线程回调：(读取结果, 错误文案)；请求本身没出错时错误文案为空，
        单项失败由界面按 ``ReadValue.ok`` 自己统计。
        """
        st = self.devices.get(did)
        if st is None or not st.online:
            done({}, "设备离线")
            return
        refs: list[PropRef] = []
        seen: set[tuple[int, int]] = set()
        for prop in props:
            key = (prop.siid, prop.piid)
            ref = st.keys.get(key)
            if ref is not None and key not in seen:
                seen.add(key)
                refs.append(ref)
        if not refs:
            done({}, "没有可读取的属性")
            return

        def finish(res: Any, err: BaseException | None) -> None:
            if err is not None or not isinstance(res, Mapping):
                done({}, f"读取失败：{err or '无返回'}")
                return
            out: dict[tuple[int, int], ReadValue] = {}
            changed = False
            now = time.monotonic()
            for ref, rv in res.items():
                if not isinstance(rv, ReadValue):
                    continue
                key = (ref.siid, ref.piid)
                out[key] = rv
                if not rv.ok or key in st.optimistic:
                    continue
                st.updated_at = now
                if rv.update_time:
                    st.stamps[key] = rv.update_time
                if st.values.get(key) != rv.value:
                    st.values[key] = rv.value
                    changed = True
            if changed:
                self._changed({did})
            done(out, "")

        self.worker.submit(
            self.client.read,
            (refs,),
            priority=PRIORITY_DATA,
            label="detail",
            callback=finish,
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
        self._notify_write()
        return True

    def _notify_write(self) -> None:
        """告诉界面"刚发了一条写指令"。回调异常不许污染写入链路。"""
        cb = self.on_write_sent
        if cb is None:
            return
        try:
            cb()
        except Exception:  # noqa: BLE001
            pass

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
                if g.is_wireless(st.values):
                    # 转无线那一路写 on/off 没有意义（固件不认），批量关灯时跳过
                    continue
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
        self._notify_write()
        return len(pairs)

    def toggle(self, did: str, siid: int, piid: int) -> None:
        st = self.devices.get(did)
        if st is None:
            return
        cur = st.values.get((siid, piid))
        self.set_value(did, (siid, piid), not bool(cur), mute=2.0)

    def run_action(
        self,
        did: str,
        siid: int,
        aiid: int,
        value: Sequence[Any] | None = None,
        *,
        on_done: Callable[[int, str], None] | None = None,
    ) -> None:
        """执行一次设备方法。``on_done`` 收到 ``(错误码, 失败原因)``，0 表示成功。

        码与原因都是 ``Client.run_action`` 的既有契约（云端非 0 码会原样返回，
        异常统一 -1 并在 ``last_error`` 里留一句话），这里只做搬运。
        """

        def done(res: Any, err: BaseException | None) -> None:
            if err is not None:
                st = self.devices.get(did)
                if st:
                    st.error = f"动作失败：{err}"
                    self._changed({did})
                code = -1
                reason = str(err)
            else:
                code = int(res) if isinstance(res, int) else 0
                reason = "" if code == 0 else (self.client.last_error or explain(code))
                self.reraise(did)
            if on_done is not None:
                on_done(code, reason)

        self.worker.submit(
            self.client.run_action,
            ((did, siid, aiid), value),
            priority=PRIORITY_CONTROL,
            label="action",
            callback=done,
        )
        self._notify_write()

    # -- 极客版能力探测 ----------------------------------------------------
    #: 一次启动最多探几台。探测是"每台一次、结论落盘"的成本，但设备多的家里
    #: 一轮全探就是几十条请求挤在正常轮询前面；分批之后每轮都是小步慢走，
    #: 而 ``geek.candidates`` 会把 gateway / router / screen 排到最前面，
    #: 中枢本身第一轮就命中，不靠"扫完全部"才出现入口。
    GEEK_SCAN_LIMIT = 20

    def scan_geek_abilities(self) -> None:
        """给还没定论的设备各发一条**不耗码**的探测，判定它有没有极客版。

        为什么必须探：MIoT 规格里根本没有这项能力（中枢网关的 spec 只有网络 /
        指示灯 / 虚拟事件），所以「按型号写死名单」等于让人肉维护——用户家里还
        有中控屏、路由器这类内置中枢，漏一个就永远看不到入口。而
        ``get_autowebconfig_url`` 不耗码，问一句就知道。

        成本上限：每台设备**一次**请求，结论落盘（``geek_ability.json``）；
        "不支持"的结论 7 天后才重探一次；共享家庭整户跳过（那是另一户人家，
        不替他们发请求），离线设备也跳过。
        """
        shared = {h.name for h in self.topology.homes if h.shared}
        nodes = [st.node for st in self.devices.values() if st.node.did not in self._geek_pending]
        for node in geek_candidates(nodes, shared, limit=self.GEEK_SCAN_LIMIT):
            self._geek_pending.add(node.did)
            self.worker.submit(
                geek_probe,
                (self.client.raw(), node.did),
                priority=PRIORITY_DATA,
                label="geek-probe",
                callback=lambda result, error, d=node.did, m=node.model: self._on_ability(
                    d, m, result, error
                ),
            )

    def _on_ability(
        self, did: str, model: str, result: Any, error: BaseException | None
    ) -> None:
        self._geek_pending.discard(did)
        if error is not None:
            # 工作线程层面就抛了（不是"设备说没有"）：当没探过，下次启动再来
            return
        geek_remember(did, model, bool(result))
        if self.on_geek_ability:
            self.on_geek_ability(did)

    def fetch_geek_passcode(
        self,
        did: str,
        on_done: Callable[[GeekEntry | None, str, str], None],
    ) -> None:
        """取一枚极客版登录码。``on_done(entry, 错误原因, 可用地址)``。

        成功时 ``entry`` 有值（``url`` + ``code``）；失败时 ``entry`` 为 ``None``、
        第二个参数是一句人话，第三个参数有值表示地址已经拿到、网页还能打开。

        **一次调用 = 消耗一枚码**（取新码会让旧码立即失效），所以这里没有缓存、
        没有重试、也没有自动刷新——只由用户点按钮触发。
        """

        def done(result: Any, error: BaseException | None) -> None:
            if error is None and isinstance(result, GeekEntry):
                on_done(result, "", "")
                return
            url = error.url if isinstance(error, GeekError) else ""
            on_done(None, str(error) if error is not None else "取码失败", url)

        self.worker.submit(
            fetch_geek_entry,
            (self.client.raw(), did),
            priority=PRIORITY_CONTROL,
            label="geek",
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
        # 场景会改动一批设备，"刚发过指令"这件事对自动化的意义更大：接下来
        # 那 5 秒要连着几轮才看得出一组设备各自变成了什么
        self._notify_write()

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
        # 每一路的"有线 / 转无线"必须跟着轮询走：它决定这一路能不能点。
        # 单独读 mode 的属性名和 on 同服务，寻址不冲突（(siid,2) vs (siid,1)）。
        if g.mode is not None and g.wireless_value is not None:
            out.append((g.mode.key, g.mode))
    # 型号级轮询扩展（CardSpec.extra）：只读状态，不渲染，但要跟着轮询走。
    # 未适配的机型这里是空列表，所以轮询集合完全不变。
    for p in card.extra:
        out.append((p.key, p))
    return out


def _register_keys(st: DeviceState, did: str) -> None:
    """登记卡片各属性的完整寻址。

    controls / metrics / gangs / extra 参与轮询；advanced 不参与轮询（展开详情页
    才按需读），但也要登记——不然「更多功能」里的控件连写都写不出去
    （``set_value`` 查不到 ref）。
    """
    for key, prop in _card_props(st.card):
        st.keys[key] = PropRef(did, prop.siid, prop.piid)
    for prop in st.card.advanced:
        st.keys[(prop.siid, prop.piid)] = PropRef(did, prop.siid, prop.piid)


def _card_keys(card: CardSpec) -> set[tuple[int, int]]:
    return {key for key, _ in _card_props(card)}


def build_card_empty(node: DeviceNode) -> CardSpec:
    """没有 spec_type 的设备（如手环）：只展示，不给控件。"""
    from .catalog import GENERIC

    return CardSpec(category="unknown", profile=GENERIC, did=node.did, model=node.model)


__all__ = ["DeviceState", "RoomView", "Store", "build_card_empty"]
