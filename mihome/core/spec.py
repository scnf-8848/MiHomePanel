"""MIoT-Spec 规格解析层。

为什么不用 mijiaAPI 的 ``get_device_info()``：
它把原始规格压缩成 ``name/type/rw/range/value-list/method``，丢掉了
``unit``（单位）、``format``（数值宽度）、``access``（是否 notify 主动上报）、
``comment``（官方中文名）以及整块 ``events``。而卡片上要显示"亮度 60%""色温
4000K"、要按"会主动上报"排轮询优先级、要显示门锁开锁记录，都需要这些字段。

所以这里直接吃 miot-spec.org 的原始 JSON：

    GET https://miot-spec.org/miot-spec-v2/instance?type=<spec_type>

原始结构（实测）::

    {"type": "urn:miot-spec-v2:device:light:...",
     "description": "leshi light wy0a01",
     "services": [
       {"iid": 2, "type": "urn:miot-spec-v2:service:light:00007802:...",
        "description": "Light", "comment": "灯",
        "properties": [{"iid": 1, "type": "urn:miot-spec-v2:property:on:00000006:...",
                        "description": "Switch Status", "comment": "开关",
                        "format": "bool", "access": ["read","write","notify"]},
                       {"iid": 2, ..., "unit": "percentage",
                        "value-range": [1,100,1]},
                       {"iid": 7, ..., "value-list": [{"value":4,"description":"Day","comment":"日光"}]}],
        "actions": [{"iid": 1, "type": "...:action:toggle:...", "in": []}],
        "events":  [{"iid": 1, "type": "...:event:...", "argument": [1,2]}]}
     ]}

层级是 设备 → 服务(siid) → 属性(piid) / 方法(aiid) / 事件(eiid)，写属性的寻址就是
``(sid, pid)``。多键面板的"第 N 路"不是虚拟设备，而是规格里的第 N 个 ``switch``
服务——这一点决定了卡片怎么聚合。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import enum_text, paths
from .adapters import find_adapter

SPEC_ENDPOINT = "https://miot-spec.org/miot-spec-v2/instance?type={urn}"
DEFAULT_TTL = 30 * 24 * 3600  # 规格极少变，缓存 30 天

# ---------------------------------------------------------------------------
# URN
# ---------------------------------------------------------------------------

_DEVICE_URN_RE = re.compile(
    r"^urn:miot-spec-v2:device:([A-Za-z0-9_\-]+):([0-9A-Fa-f]+):([^:]+):([^:]+)(?::([^:]+))?$"
)
_SUFFIX_RE = re.compile(r"-\d+$")


def squash(name: str) -> str:
    """去掉厂商为同服务内重名属性添加的 ``-2`` / ``-3`` 后缀。

    洗衣机规格里同时存在 ``target-temperature`` 和 ``target-temperature-2``；
    它们属于不同服务（不同子功能），做"按名字找属性"时必须能退化匹配。
    """
    return _SUFFIX_RE.sub("", name)


@dataclass(frozen=True, slots=True)
class UrnParts:
    category: str
    category_id: str
    model: str
    version: str
    internal_id: str | None = None


def parse_urn(urn: str | None) -> UrnParts | None:
    """拆解 ``urn:miot-spec-v2:device:<category>:<catid>:<model>:<ver>[:<id>]``。

    解析不出来（例如 wear 手表没有 spec_type）时返回 None，调用方按"仅展示"处理。
    """
    if not urn:
        return None
    m = _DEVICE_URN_RE.match(urn.strip())
    if not m:
        return None
    return UrnParts(
        category=m.group(1),
        category_id=m.group(2),
        model=m.group(3),
        version=m.group(4),
        internal_id=m.group(5),
    )


# ---------------------------------------------------------------------------
# 单位
# ---------------------------------------------------------------------------

_UNIT_SYMBOL = {
    "percentage": "%",
    "kelvin": "K",
    "celsius": "°C",
    "fahrenheit": "°F",
    "minutes": "分钟",
    "seconds": "秒",
    "hours": "小时",
    "days": "天",
    "micrograms-per-cubic-meter": "μg/m³",
    "mg-per-cubic-meter": "mg/m³",
    "parts-per-million": "ppm",
    "decibel": "dB",
    "lux": "lx",
    "volt": "V",
    "ampere": "A",
    "watt": "W",
    "kilowatt-hour": "kWh",
    "liter": "L",
    "milliliter": "mL",
    "cubic-meter": "m³",
    "gram": "g",
    "kilogram": "kg",
    "pascal": "Pa",
    "rpm": "rpm",
}


def unit_symbol(unit: str | None) -> str:
    if not unit:
        return ""
    return _UNIT_SYMBOL.get(unit, unit)


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValueOption:
    value: int
    label: str  # 云端原话（comment 优先，可能是整句中文或英文）
    name: str  # 官方英文描述
    #: 界面短名（``core/enum_text.py`` 的映射结果）。原文不丢，tooltip 用 ``display``。
    short: str = ""

    @property
    def display(self) -> str:
        return self.label or self.name or str(self.value)

    @property
    def display_short(self) -> str:
        """下拉框 / 读数用的短名；没映射过就是 ``display`` 的截断版。"""
        return self.short or self.display


@dataclass(frozen=True, slots=True)
class PropertySpec:
    siid: int
    piid: int
    name: str
    urn: str
    description: str
    comment: str
    format: str
    access: tuple[str, ...]
    unit: str | None
    value_range: tuple[float, float, float] | None
    value_list: tuple[ValueOption, ...]
    service: str
    service_label: str
    #: 界面行名（``core/enum_text.py`` 的结果）。类别级在解析期算；
    #: 型号级要等知道设备型号，由 :func:`localize` 补 —— 见那里为什么不能在解析期做。
    label_short: str = ""

    # -- 寻址 ------------------------------------------------------------
    @property
    def key(self) -> tuple[int, int]:
        return (self.siid, self.piid)

    @property
    def method(self) -> str:
        return f"{self.siid}.{self.piid}"

    # -- 文案 ------------------------------------------------------------
    @property
    def label(self) -> str:
        return self.comment or self.description or self.name

    @property
    def display_label(self) -> str:
        """界面行名：英文属性名换成中文，其余原样（映射表见 ``core/enum_text.py``）。"""
        return self.label_short or enum_text.prop_short(self.label)

    @property
    def unit_text(self) -> str:
        return unit_symbol(self.unit)

    # -- 能力 ------------------------------------------------------------
    @property
    def readable(self) -> bool:
        return "read" in self.access or "notify" in self.access

    @property
    def writeable(self) -> bool:
        return "write" in self.access

    @property
    def notifies(self) -> bool:
        """会主动上报。轮询优先级应按它排序。"""
        return "notify" in self.access

    # -- 控件形态 --------------------------------------------------------
    @property
    def kind(self) -> str:
        """给 UI 用的控件类型：bool / enum / range / number / text / raw。"""
        if self.format == "bool":
            return "bool"
        if self.value_list:
            return "enum"
        if self.format in ("string", "hex", "float", ""):
            return "text" if self.format in ("string", "hex") else "number"
        if self.value_range:
            return "range"
        return "number"

    @property
    def is_state_of_power(self) -> bool:
        return self.name == "on" and self.format == "bool"

    def option(self, value: Any) -> ValueOption | None:
        for o in self.value_list:
            if o.value == value:
                return o
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "siid": self.siid,
            "piid": self.piid,
            "name": self.name,
            "label": self.label,
            "format": self.format,
            "kind": self.kind,
            "access": list(self.access),
            "unit": self.unit,
            "unit_symbol": self.unit_text,
            "range": list(self.value_range) if self.value_range else None,
            "value_list": [{"value": o.value, "label": o.display} for o in self.value_list],
            "service": self.service,
        }


@dataclass(frozen=True, slots=True)
class ActionSpec:
    siid: int
    aiid: int
    name: str
    urn: str
    description: str
    comment: str
    in_piids: tuple[int, ...]

    @property
    def key(self) -> tuple[int, int]:
        return (self.siid, self.aiid)

    @property
    def label(self) -> str:
        return self.comment or self.description or self.name


@dataclass(frozen=True, slots=True)
class EventSpec:
    siid: int
    eiid: int
    name: str
    urn: str
    description: str
    comment: str
    arg_piids: tuple[int, ...]

    @property
    def label(self) -> str:
        return self.comment or self.description or self.name


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    siid: int
    name: str
    urn: str
    description: str
    comment: str
    properties: tuple[PropertySpec, ...]
    actions: tuple[ActionSpec, ...]
    events: tuple[EventSpec, ...]

    @property
    def label(self) -> str:
        return self.comment or self.description or self.name


# ---------------------------------------------------------------------------
# 设备规格
# ---------------------------------------------------------------------------


class DeviceSpec:
    """一台设备（或同类设备）的完整能力描述，带名字/方法号索引。"""

    __slots__ = (
        "urn",
        "category",
        "category_id",
        "model",
        "version",
        "description",
        "services",
        "source",
        "fetched_at",
        "_by_key",
        "_by_name",
        "_by_squash",
        "_services_by_name",
    )

    def __init__(
        self,
        urn: str,
        category: str,
        category_id: str,
        model: str,
        version: str,
        description: str,
        services: Sequence[ServiceSpec],
        source: str = "cloud",
        fetched_at: int = 0,
    ) -> None:
        self.urn = urn
        self.category = category
        self.category_id = category_id
        self.model = model
        self.version = version
        self.description = description
        self.services = tuple(services)
        self.source = source
        self.fetched_at = fetched_at

        by_key: dict[tuple[int, int], PropertySpec] = {}
        by_name: dict[str, list[PropertySpec]] = {}
        by_squash: dict[str, list[PropertySpec]] = {}
        svc: dict[str, list[ServiceSpec]] = {}
        for s in self.services:
            svc.setdefault(s.name, []).append(s)
            for p in s.properties:
                by_key[p.key] = p
                by_name.setdefault(p.name, []).append(p)
                by_squash.setdefault(squash(p.name), []).append(p)
        self._by_key = by_key
        self._by_name = by_name
        self._by_squash = by_squash
        self._services_by_name = svc

    # -- 基本 ------------------------------------------------------------
    @property
    def label(self) -> str:
        return self.description or self.model or self.category

    @property
    def properties(self) -> tuple[PropertySpec, ...]:
        return tuple(self._by_key.values())

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<DeviceSpec {self.category} {self.model} v{self.version} "
            f"services={len(self.services)} props={len(self._by_key)} src={self.source}>"
        )

    # -- 查询 ------------------------------------------------------------
    def prop(self, siid: int, piid: int) -> PropertySpec | None:
        return self._by_key.get((siid, piid))

    def service(self, name: str) -> ServiceSpec | None:
        found = self._services_by_name.get(name)
        return found[0] if found else None

    def services_named(self, name: str) -> tuple[ServiceSpec, ...]:
        return tuple(self._services_by_name.get(name, ()))

    def find(
        self,
        *names: str,
        service: str | None = None,
        need_write: bool = False,
    ) -> PropertySpec | None:
        """按属性名依次尝试查找。

        匹配顺序：精确名 → 去 ``-N`` 后缀；同分时优先所属服务名等于 ``service``
        的属性（避免"灯的 on"被"遥控器管理的 on"抢走），再按 siid/piid 升序。
        """
        for name in names:
            for pool in (self._by_name.get(name), self._by_squash.get(squash(name))):
                if not pool:
                    continue
                cand = list(pool)
                if need_write:
                    cand = [p for p in cand if p.writeable]
                if not cand:
                    continue
                if service:
                    exact = [p for p in cand if p.service == service]
                    if exact:
                        return exact[0]
                return cand[0]
        return None

    def find_all(self, name: str) -> tuple[PropertySpec, ...]:
        return tuple(self._by_name.get(name, ()))

    def prop_names(self) -> set[str]:
        return set(self._by_name)

    def writeable(self) -> tuple[PropertySpec, ...]:
        return tuple(p for p in self._by_key.values() if p.writeable)

    def notifying(self) -> tuple[PropertySpec, ...]:
        return tuple(p for p in self._by_key.values() if p.notifies)

    def actions(self) -> tuple[ActionSpec, ...]:
        return tuple(a for s in self.services for a in s.actions)

    def events(self) -> tuple[EventSpec, ...]:
        return tuple(e for s in self.services for e in s.events)

    # -- 序列化 ----------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "urn": self.urn,
            "category": self.category,
            "model": self.model,
            "version": self.version,
            "description": self.description,
            "source": self.source,
            "services": [
                {
                    "siid": s.siid,
                    "name": s.name,
                    "label": s.label,
                    "properties": [p.to_dict() for p in s.properties],
                    "actions": [
                        {"aiid": a.aiid, "name": a.name, "label": a.label, "in": list(a.in_piids)}
                        for a in s.actions
                    ],
                    "events": [
                        {"eiid": e.eiid, "name": e.name, "label": e.label, "args": list(e.arg_piids)}
                        for e in s.events
                    ],
                }
                for s in self.services
            ],
        }


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def _urn_name(urn: str, fallback: str = "") -> str:
    parts = (urn or "").split(":")
    return parts[3] if len(parts) > 3 else fallback


def _as_piids(value: Any) -> tuple[int, ...]:
    if not value:
        return ()
    out: list[int] = []
    for item in value:
        if isinstance(item, int):
            out.append(item)
        elif isinstance(item, dict):
            # 少数规格里 in/argument 写成 {"piid": 1, "value": ...}
            pid = item.get("piid", item.get("value"))
            if isinstance(pid, int):
                out.append(pid)
    return tuple(out)


def parse_raw(raw: dict[str, Any], source: str = "cloud", fetched_at: int = 0) -> DeviceSpec:
    """把 miot-spec.org 的原始 JSON 转成 DeviceSpec。"""
    urn = raw.get("type") or raw.get("spec_type") or ""
    parts = parse_urn(urn) or UrnParts("unknown", "", "", "")
    services: list[ServiceSpec] = []
    for s in raw.get("services") or []:
        sname = _urn_name(s.get("type", ""), f"service-{s.get('iid')}")
        slabel = s.get("comment") or s.get("description") or sname
        props: list[PropertySpec] = []
        for p in s.get("properties") or []:
            rng = p.get("value-range")
            # 枚举项短名要用属性做上下文（少数词在不同属性下含义不同：Dry 在空调
            # 是除湿、在洗衣流程是烘干），所以先算出这一行属性自己的两个标识：
            # ``pname`` 是 URN 名（查表用的稳定键），``plabel`` 是云端中文名（给人看的）。
            pname = _urn_name(p.get("type", ""), p.get("description", ""))
            plabel = p.get("comment") or p.get("description") or ""
            props.append(
                PropertySpec(
                    siid=int(s.get("iid", 0)),
                    piid=int(p.get("iid", 0)),
                    name=pname,
                    urn=p.get("type", ""),
                    description=p.get("description", "") or "",
                    comment=p.get("comment", "") or "",
                    format=p.get("format", "") or "",
                    access=tuple(p.get("access") or ()),
                    unit=p.get("unit"),
                    value_range=(float(rng[0]), float(rng[1]), float(rng[2]))
                    if isinstance(rng, list) and len(rng) >= 3
                    else None,
                    value_list=tuple(
                        ValueOption(
                            value=int(o.get("value", 0)),
                            label=(o.get("comment") or o.get("description") or ""),
                            name=o.get("description", "") or "",
                            short=enum_text.enum_short(
                                o.get("comment") or o.get("description") or "",
                                pname,
                                category=parts.category,
                            ),
                        )
                        for o in (p.get("value-list") or [])
                    ),
                    service=sname,
                    service_label=slabel,
                    label_short=enum_text.prop_short(plabel, category=parts.category),
                )
            )
        acts = [
            ActionSpec(
                siid=int(s.get("iid", 0)),
                aiid=int(a.get("iid", 0)),
                name=_urn_name(a.get("type", ""), a.get("description", "")),
                urn=a.get("type", ""),
                description=a.get("description", "") or "",
                comment=a.get("comment", "") or "",
                in_piids=_as_piids(a.get("in")),
            )
            for a in (s.get("actions") or [])
        ]
        evts = [
            EventSpec(
                siid=int(s.get("iid", 0)),
                eiid=int(e.get("iid", 0)),
                name=_urn_name(e.get("type", ""), e.get("description", "")),
                urn=e.get("type", ""),
                description=e.get("description", "") or "",
                comment=e.get("comment", "") or "",
                arg_piids=_as_piids(e.get("argument")),
            )
            for e in (s.get("events") or [])
        ]
        services.append(
            ServiceSpec(
                siid=int(s.get("iid", 0)),
                name=sname,
                urn=s.get("type", ""),
                description=s.get("description", "") or "",
                comment=s.get("comment", "") or "",
                properties=tuple(props),
                actions=tuple(acts),
                events=tuple(evts),
            )
        )
    return DeviceSpec(
        urn=urn,
        category=parts.category,
        category_id=parts.category_id,
        model=parts.model,
        version=parts.version,
        description=raw.get("description", "") or "",
        services=services,
        source=source,
        fetched_at=fetched_at,
    )


def localize(spec: DeviceSpec, model: str | None) -> DeviceSpec:
    """把**型号级**文案覆盖应用到一份规格上，返回新的规格；没登记就原样返回。

    为什么不能在 :func:`parse_raw` 里做：规格缓存的键是 URN，URN 里的型号段是
    ``yeelink-lamp22``（``spec.model``），不是设备节点上报的 ``yeelink.light.lamp22``。
    把"这台设备"的文案烤进"这个 URN"的缓存，同 URN 的第二台设备就会串味。
    URN 自带 category，所以类别级在解析期做是安全的，只有型号级要等到这里。

    **没登记文案时返回原对象**（不是副本）：``Store.apply_topology`` 用
    ``prev.spec is not spec`` 判断要不要重建卡片，副本会让每次拓扑刷新都把卡片
    重建一遍。store 那边还会把结果按 (URN, 型号) 记住，保证同一输入同一对象。
    """
    adapter = find_adapter(model)
    if adapter is None or adapter.text.empty:
        return spec
    services = []
    for svc in spec.services:
        props = []
        for prop in svc.properties:
            options = tuple(
                replace(
                    o,
                    short=enum_text.enum_short(
                        o.label or o.name,
                        prop.name,
                        category=spec.category,
                        model=model or "",
                    ),
                )
                for o in prop.value_list
            )
            props.append(
                replace(
                    prop,
                    value_list=options,
                    label_short=enum_text.prop_short(
                        prop.label, category=spec.category, model=model or ""
                    ),
                )
            )
        services.append(replace(svc, properties=tuple(props)))
    return DeviceSpec(
        urn=spec.urn,
        category=spec.category,
        category_id=spec.category_id,
        model=spec.model,
        version=spec.version,
        description=spec.description,
        services=services,
        source=spec.source,
        fetched_at=spec.fetched_at,
    )


def parse_legacy(data: dict[str, Any]) -> DeviceSpec:
    """把 mijiaAPI 的扁平缓存（无 services / unit / access / events）转成 DeviceSpec。

    仅在联网不可用时兜底：单位、事件、notify 都拿不到，``source`` 会是 ``legacy``。
    """
    urn = data.get("spec_type") or ""
    parts = parse_urn(urn) or UrnParts("unknown", "", "", "")
    buckets: dict[int, list[PropertySpec]] = {}
    for p in data.get("properties") or []:
        m = p.get("method") or {}
        siid = int(m.get("siid", 0))
        piid = int(m.get("piid", 0))
        rw = p.get("rw") or "r"
        access = []
        if "r" in rw:
            access.append("read")
        if "w" in rw:
            access.append("write")
        if p.get("notify") or "n" in rw:
            access.append("notify")
        rng = p.get("range")
        buckets.setdefault(siid, []).append(
            PropertySpec(
                siid=siid,
                piid=piid,
                name=p.get("name", ""),
                urn=p.get("type", ""),
                description=p.get("description", "") or "",
                comment="",
                format=p.get("type", ""),
                access=tuple(access),
                unit=None,
                value_range=(float(rng[0]), float(rng[1]), float(rng[2]))
                if isinstance(rng, list) and len(rng) >= 3
                else None,
                value_list=tuple(
                    ValueOption(int(o.get("value", 0)), o.get("description", ""), o.get("description", ""))
                    for o in (p.get("value-list") or [])
                ),
                service="",
                service_label="",
            )
        )
    services = []
    for siid in sorted(buckets):
        services.append(
            ServiceSpec(
                siid=siid,
                name="",
                urn="",
                description="",
                comment="",
                properties=tuple(sorted(buckets[siid], key=lambda x: x.piid)),
                actions=(),
                events=(),
            )
        )
    return DeviceSpec(
        urn=urn,
        category=parts.category,
        category_id=parts.category_id,
        model=parts.model,
        version=parts.version,
        description=data.get("name", "") or "",
        services=services,
        source="legacy",
        fetched_at=int(time.time()),
    )


# ---------------------------------------------------------------------------
# 仓库（内存 → 磁盘 → 网络 → 旧缓存）
# ---------------------------------------------------------------------------


class SpecRepository:
    """按 spec_type 取 DeviceSpec，带磁盘缓存与并发预取。"""

    def __init__(
        self,
        cache_dir: Path | None = None,
        ttl: int = DEFAULT_TTL,
        timeout: float = 25.0,
        retries: int = 3,
        allow_network: bool = True,
    ) -> None:
        self._dir = Path(cache_dir) if cache_dir else paths.spec_cache_dir()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._ttl = ttl
        self._timeout = timeout
        self._retries = retries
        self._allow_network = allow_network
        self._mem: dict[str, DeviceSpec] = {}
        self._lock = threading.Lock()

    # -- 磁盘 ------------------------------------------------------------
    def _path(self, urn: str) -> Path:
        digest = hashlib.sha1(urn.encode("utf-8")).hexdigest()[:16]
        return self._dir / f"{digest}.json"

    def _load_disk(self, urn: str) -> tuple[DeviceSpec | None, bool]:
        """返回 (spec, 是否新鲜)。"""
        f = self._path(urn)
        if not f.exists():
            return None, False
        try:
            blob = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, False
        if blob.get("urn") != urn:
            return None, False
        raw = blob.get("raw")
        if not isinstance(raw, dict):
            return None, False
        ts = int(blob.get("fetched_at") or 0)
        spec = parse_raw(raw, source="cache", fetched_at=ts)
        fresh = (time.time() - ts) < self._ttl
        return spec, fresh

    def _save_disk(self, urn: str, raw: dict[str, Any], ts: int) -> None:
        payload = {"urn": urn, "fetched_at": ts, "raw": raw}
        tmp = self._path(urn).with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path(urn))
        except OSError:
            pass

    # -- 网络 ------------------------------------------------------------
    def _fetch_raw(self, urn: str) -> dict[str, Any] | None:
        url = SPEC_ENDPOINT.format(urn=urllib.parse.quote(urn, safe=":"))
        last: Exception | None = None
        for attempt in range(self._retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "MiHomePanel/0.1"})
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, dict) and data.get("services"):
                    return data
                last = ValueError("empty spec payload")
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
                last = exc
                time.sleep(0.6 * (attempt + 1))
        if last:
            print(f"[spec] 拉取失败 {urn}: {last}")
        return None

    def _legacy(self, urn: str) -> DeviceSpec | None:
        fname = urn.replace(":", "_") + ".json"
        f = paths.legacy_mijia_cache_dir() / fname
        if not f.exists():
            return None
        try:
            return parse_legacy(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None

    # -- 对外 ------------------------------------------------------------
    def get(self, urn: str, *, force: bool = False) -> DeviceSpec | None:
        if not urn:
            return None
        with self._lock:
            cached = self._mem.get(urn)
        if cached and not force:
            return cached

        spec, fresh = self._load_disk(urn)
        if spec and fresh and not force:
            with self._lock:
                self._mem[urn] = spec
            return spec

        if self._allow_network:
            raw = self._fetch_raw(urn)
            if raw:
                ts = int(time.time())
                self._save_disk(urn, raw, ts)
                spec = parse_raw(raw, source="cloud", fetched_at=ts)
                with self._lock:
                    self._mem[urn] = spec
                return spec

        if spec:  # 过期但可用
            with self._lock:
                self._mem[urn] = spec
            return spec
        fallback = self._legacy(urn)
        if fallback:
            with self._lock:
                self._mem[urn] = fallback
        return fallback

    def get_many(self, urns: Iterable[str], workers: int = 4) -> dict[str, DeviceSpec]:
        """并发预取。同一 spec_type 会被 64 台设备共享，去重后真正要拉的不多。"""
        uniq = list(dict.fromkeys(u for u in urns if u))
        if not uniq:
            return {}
        result: dict[str, DeviceSpec] = {}
        if workers <= 1 or len(uniq) == 1:
            for u in uniq:
                s = self.get(u)
                if s:
                    result[u] = s
            return result

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(workers, len(uniq))) as pool:
            for u, s in zip(uniq, pool.map(self.get, uniq)):
                if s:
                    result[u] = s
        return result

    def stats(self) -> dict[str, int]:
        files = list(self._dir.glob("*.json"))
        return {"cached_files": len(files), "memory": len(self._mem)}


__all__ = [
    "ActionSpec",
    "DeviceSpec",
    "EventSpec",
    "PropertySpec",
    "ServiceSpec",
    "SpecRepository",
    "UrnParts",
    "ValueOption",
    "parse_legacy",
    "parse_raw",
    "parse_urn",
    "squash",
    "unit_symbol",
]
