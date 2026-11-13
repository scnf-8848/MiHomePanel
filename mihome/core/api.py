"""云端访问层：把 mijiaAPI 包成"面向面板"的接口。

项目按 GPL-3.0 开源，所以直接复用 mijiaAPI 的签名 / 加解密 / token 刷新，不重造。
这一层只补它缺的五件事：

1. **能力学习**——"规格里有"不等于"这台设备认"。实测红外空调的 ``ir-mode``、
   部分门锁的 ``lock-action`` 回读会返回 ``-704002000``（设备不支持）。把确认
   不支持的 ``(did, siid, piid)`` 记下来，之后轮询直接跳过、UI 同时隐藏，
   而不是每 3 秒重试一次徒增延迟。
   注意：**离线 / 超时不算不支持**，否则设备离线一次就永久失去控制能力。

2. **子设备路由**——多键面板派生出的虚拟设备 did 形如 ``<父did>.s3``，云端只认
   父 did + 该路 siid，对子 did 发请求要拖到 6 秒才返回 ``-704083036``。

3. **批量读 + 分片**——一次请求带多条 ``(did, siid, piid)``，每片 20 条。
   一次轮询只发几个请求，而不是每台设备一个请求。

4. **属性级静默期**——本地写入后的一小段时间内，云端回读到的还是旧值。
   这期间的回读结果要丢弃，否则滑块会"松手弹回去"。

5. **请求语言**——上游按系统 locale 猜，中文 Windows 上猜出来的不是合法 locale 串，
   服务端会回退英文，设备名全变成厂商默认英文名（"Xiaomi Smart Wall Switch (3
   Gang)"）。这里在构造时钉死 ``zh_CN``，见 :func:`apply_locale`。

线程模型：``requests.Session`` 不是线程安全的，所有方法都在内部锁内串行执行，
调用方（``core/jobs.py`` 的单线程队列）不需要再加锁。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from mijiaAPI import mijiaAPI
from mijiaAPI.errors import APIError, ERROR_CODE

from . import dev_fault, paths
from .health import (
    NETWORK,
    PROTOCOL,
    RATE_LIMIT_TAIL,
    SERVER,
    Failure,
    PollHealth,
    classify_error,
)
from .tree import DeviceNode, group_devices, parse_virtual_did

# 米家错误码格式：-70 + xxx(HTTP 状态码) + y(出错位置) + zzz(错误码)。
# 后三位才是语义，所以按 abs(code) % 1000 判断，比硬编码整个码值稳。
# 依据：https://iot.mi.com/v2/new/doc/help-support/faq/spec/error-code
_UNSUPPORTED_TAILS: frozenset[int] = frozenset(
    {
        1,  # Device 不存在
        2,  # Service 不存在
        3,  # Property 不存在
        4,  # Event 不存在
        5,  # Action 不存在
        8,  # 无效 ID
        13,  # 属性不可读（规格标了 read、固件不认）
        23,  # 属性不可写
        33,  # 属性不可订阅
        101,  # 红外设备不支持此操作
    }
)
# 明确"暂时性"的错误，绝不能记成不支持，否则设备离线一次就永久失去控制。
# 11=设备离线  36=设备操作超时  43=属性值错误（设备侧异常）  992=请求过于频繁
_TRANSIENT_TAILS: frozenset[int] = frozenset({0, 11, 36, 43, 100, 992})

SUCCESS_CODES: frozenset[int] = frozenset({0, 1})  # 上游把 0/1 都当成功

# 一次批量读最多带多少条属性。实测（2026-11-10，200 条真实属性各跑 3 次）：
# ``chunk=120`` → 2 个请求 510–620ms；``chunk=300`` → 1 个请求 261–357ms，返回条数一致。
# 分批是**串行**的，每多一批就多付一次往返，所以"批大一点"既省请求也更快。
# 代价：整批失败时影响面从"丢一半"变成"丢一整轮"——但失败本来就是网络层问题，
# 调用方一律保留旧值 + 指数退避，所以这个代价可以接受。
DEFAULT_CHUNK = 300
DEFAULT_MUTE = 3.0  # 写入后忽略回读的秒数

#: 请求语言。**必须显式指定**，不能交给 mijiaAPI 去猜——见 :func:`apply_locale`。
API_LOCALE = "zh_CN"


def apply_locale(api: mijiaAPI, locale: str = API_LOCALE) -> None:
    """把上游客户端的请求语言钉成中文，并重建会话。

    mijiaAPI 直接用 ``locale.getlocale()[0]`` 当 locale。中文 Windows 上它返回的是
    ``"Chinese (Simplified)_China"``——一个**显示名**，不是 locale 串。上游只检查
    里面有没有 ``_`` 就放行，于是两个值都坏了：

    * Cookie 里 ``locale=Chinese (Simplified)_China``（非法，服务端认不出）；
    * ``split('_')[1]`` 给出 ``countryCode=China``（该是 ``CN``，而且这个串被空格
      一截，实际只取到 ``China``）。

    服务端解析不了就只能回退英文，设备列表里返回的是**厂商默认英文名**
    （"Xiaomi Smart Wall Switch (3 Gang)"），而不是用户在米家 App 里看到的中文。
    实测同一份凭据：``locale`` 为 ``zh_CN`` 时 64 台设备全部中文，为默认值时
    只有用户自己改过名的那些是中文。

    locale 是拼接进 Cookie 的，所以改完必须重建 session 才生效；上游没有公开的
    setter，这里直接调它的 ``_init_session``（``Client.raw()`` 本来就是这个用途）。
    """
    if getattr(api, "locale", "") == locale:
        return
    api.locale = locale
    # 登录页的 URL 里也拼了一份 locale（``_locale=``），顺手换掉，否则登录页会用
    # 那个非法串去请求，扫码时的界面语言跟着不对。
    url = str(getattr(api, "service_login_url", ""))
    if "_locale=" in url:
        api.service_login_url = url.split("_locale=")[0] + f"_locale={locale}"
    data = getattr(api, "auth_data", None) or {}
    # 没登录时还没有 session（mijiaAPI 只在凭据存在时才 _init_session），
    # 此时不能碰它——登录流程会自己建。
    if data.get("cUserId") and data.get("serviceToken"):
        api._init_session()  # noqa: SLF001


def classify(code: int) -> str:
    """把错误码归成三类：ok / unsupported / transient / unknown。"""
    if code in SUCCESS_CODES:
        return "ok"
    tail = abs(code) % 1000
    if tail in _UNSUPPORTED_TAILS:
        return "unsupported"
    if tail in _TRANSIENT_TAILS:
        return "transient"
    return "unknown"


#: 会直接弹给用户看的错误码释义。完整表在上游 ``mijiaAPI/errors.py``
#: （注释里给了 ha_xiaomi_home 的 i18n 出处），这里只收本项目真碰得到的几条——
#: 全抄一遍既没必要，也会随上游变动而失修。
_CODE_TEXT: dict[int, str] = {
    -704010000: "授权失效，设备可能已被删除",
    -704014006: "没找到设备描述，先刷新设备列表",
    -704040002: "这个型号没有该服务",
    -704040005: "这个型号没有该动作",
    -704042001: "设备不存在",
    -704042011: "设备离线",
    -704053036: "设备操作超时",
    -704053100: "设备当前状态下不能做这个操作",
    -704220008: "参数 ID 无效",
    -704220025: "参数个数对不上（多半是参数没送到）",
    -704220035: "参数值不合法",
    -704222034: "动作返回值异常",
    -705201015: "设备执行失败",
}


def explain(code: int) -> str:
    """把错误码翻成一句人话，**始终带上原始码**，方便回头对文档。"""
    if code in SUCCESS_CODES:
        return ""
    text = _CODE_TEXT.get(code)
    return f"{text}（{code}）" if text else f"错误码 {code}"


# ---------------------------------------------------------------------------
# 寻址
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, order=True)
class PropRef:
    """属性的完整寻址：设备 + 服务 + 属性。"""

    did: str
    siid: int
    piid: int

    @property
    def method(self) -> str:
        return f"{self.siid}.{self.piid}"

    def to_wire(self) -> dict[str, Any]:
        return {"did": self.did, "siid": self.siid, "piid": self.piid}


@dataclass(frozen=True, slots=True)
class ReadValue:
    """一次属性读取的结果。

    ``ok=False`` 时 ``value`` 为 ``None``；``unsupported=True`` 表示设备明确不认
    这个属性（与"暂时读不到"区分开）。
    """

    ref: PropRef
    value: Any = None
    code: int = 0
    update_time: int = 0
    ok: bool = False
    unsupported: bool = False
    retryable: bool = True

    @property
    def changed_ago(self) -> float:
        """云端记录的这个值**最后一次变化**距今多少秒。

        注意：实测这个时间戳不会随读取推进——一台从没变过状态的插座会显示
        "3 天前"。所以它只表示"多久没变过"，**不能**当作数据新鲜度用。
        判断数据是否实时，要看本地最后一次成功读取的时间。
        """
        if not self.update_time:
            return 0.0
        return max(0.0, time.time() - self.update_time)


def _route(ref: PropRef) -> tuple[dict[str, Any], tuple[str, int, int] | None]:
    """把引用翻译成云端能接受的请求项。

    多键面板派生的虚拟设备 did 形如 ``<父did>.s3``，云端**只认父 did + 该路 siid**：
    对子 did 直接发请求会静默拖到约 6 秒再返回 ``-704083036``。虽然 ``tree.py`` 已经
    把子设备折叠进父设备、正常路径不会产生子 did，但只要有别处漏进来一条，
    整轮轮询就会被拖慢 6 秒，所以这一层必须兜住。

    返回 ``(请求项, 反向映射键)``，后者用来把响应里的父 did 还原成原来的引用。
    """
    virt = parse_virtual_did(ref.did)
    if virt is None:
        return ref.to_wire(), None
    parent, siid = virt
    wire = {"did": parent, "siid": siid, "piid": ref.piid}
    return wire, (parent, siid, ref.piid)


def _wire_batch(
    batch: Sequence[PropRef],
) -> tuple[list[dict[str, Any]], dict[tuple[str, int, int], PropRef]]:
    payload: list[dict[str, Any]] = []
    remap: dict[tuple[str, int, int], PropRef] = {}
    for ref in batch:
        item, routed = _route(ref)
        payload.append(item)
        if routed is not None:
            remap[routed] = ref
    return payload, remap


# ---------------------------------------------------------------------------
# 能力学习
# ---------------------------------------------------------------------------


class CapabilityBook:
    """记录"设备不认哪些属性"的小账本，落盘复用。

    结构::

        {"entries": {"<did>|<siid>|<piid>": {"code": -704002000, "at": 1793813501}}}
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (paths.cache_dir() / "capabilities.json")
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self._load()

    # -- 落盘 ------------------------------------------------------------
    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        entries = raw.get("entries")
        if isinstance(entries, dict):
            self._data = {k: v for k, v in entries.items() if isinstance(v, dict)}

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            payload = {"version": 1, "entries": self._data}
            self._dirty = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    # -- 读写 ------------------------------------------------------------
    @staticmethod
    def _key(ref: PropRef) -> str:
        return f"{ref.did}|{ref.siid}|{ref.piid}"

    def unsupported(self, ref: PropRef) -> bool:
        return self._key(ref) in self._data

    def mark_unsupported(self, ref: PropRef, code: int) -> None:
        k = self._key(ref)
        with self._lock:
            if k in self._data:
                return
            self._data[k] = {"code": code, "at": int(time.time())}
            self._dirty = True

    def forget_device(self, did: str) -> None:
        """设备重新配对 / 换了型号时清掉旧账。"""
        prefix = f"{did}|"
        with self._lock:
            stale = [k for k in self._data if k.startswith(prefix)]
            for k in stale:
                del self._data[k]
            if stale:
                self._dirty = True

    def __len__(self) -> int:
        return len(self._data)


# ---------------------------------------------------------------------------
# 拓扑
# ---------------------------------------------------------------------------


@dataclass
class RoomNode:
    id: str
    name: str
    order: int = 0


@dataclass
class HomeNode:
    id: str
    name: str
    rooms: list[RoomNode] = field(default_factory=list)
    shared: bool = False

    @property
    def display_name(self) -> str:
        """共享家庭的 name 有时就是数字 id（没设名字），退回"共享家庭"。"""
        if self.name and not self.name.isdigit():
            return self.name
        return "共享家庭" if self.shared else self.name or "我的家"


@dataclass
class Topology:
    homes: list[HomeNode] = field(default_factory=list)
    devices: list[DeviceNode] = field(default_factory=list)
    _by_did: dict[str, DeviceNode] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._by_did = {d.did: d for d in self.devices}

    def get(self, did: str) -> DeviceNode | None:
        return self._by_did.get(did)

    def by_room(self, home: str, room: str) -> list[DeviceNode]:
        return [d for d in self.devices if d.home == home and d.room == room]

    def real_devices(self) -> list[DeviceNode]:
        return [d for d in self.devices if d.spec_type]


def _locate(homes_raw: Sequence[Mapping[str, Any]]) -> dict[str, tuple[str, str]]:
    """did → (家庭名, 房间名)。设备列表本身不带房间，只能从 roomlist 反查。"""
    out: dict[str, tuple[str, str]] = {}
    for h in homes_raw:
        home_name = str(h.get("name") or "")
        for r in h.get("roomlist") or []:
            room_name = str(r.get("name") or "")
            for did in r.get("dids") or []:
                out[str(did)] = (home_name, room_name)
    return out


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class Client:
    """米家云端访问入口。"""

    def __init__(self, *, api: mijiaAPI | None = None, health: PollHealth | None = None) -> None:
        self._lock = threading.RLock()
        self._api = api or mijiaAPI()
        # 中文界面就得用中文设备名。上游按系统 locale 猜，中文 Windows 上猜出来
        # 的是一个非法串，服务端回退英文（详见 apply_locale 的注释）。
        apply_locale(self._api)
        self.capabilities = CapabilityBook()
        #: 轮询健康账本。记账点在请求层——只有这里拿得到原始错误码与异常文本，
        #: 再往上（``Store``）异常已经变成空 dict 了。
        self.health = health or PollHealth()
        self._mute: dict[PropRef, float] = {}
        self._mute_window = DEFAULT_MUTE
        #: 拓扑加载时记下的 ``home_id → uid``。重读在线状态要逐家庭请求设备清单，
        #: 上游的 ``get_devices_list`` 会为拿 ``home_owner`` 再请求一次家庭列表，
        #: 两个家庭就是 5 次请求；这里用记下的 uid 直接发，一次家庭一次。
        self._owners: dict[str, int] = {}
        self.last_error: str = ""
        #: 最近一轮轮询到底读没读回来东西。``None`` = 上一次 ``read`` 不是轮询。
        #: ``Store`` 判退避只能看这个，别再拿"返回值是不是 Mapping"当判据——失败时
        #: ``read`` 返回的是空 dict，那也是 Mapping，会把整轮网络故障判成成功。
        #: 正常空转（没有可读属性 / 全部确认不支持）算成功，见 ``_record_poll``。
        self.last_poll_ok: bool | None = None

    # -- 认证 ------------------------------------------------------------
    @property
    def available(self) -> bool:
        try:
            return bool(self._api.available)
        except Exception:  # noqa: BLE001
            return False

    @property
    def auth_path(self) -> Path:
        try:
            return Path(self._api.auth_data_path)
        except Exception:  # noqa: BLE001
            return paths.auth_file()

    def raw(self) -> mijiaAPI:
        """逃生口：登录二维码等场景需要直接摸上游对象。"""
        return self._api

    def locked(self, action: Any) -> Any:
        """在串行锁里执行一次直接摸上游对象的调用。

        ``requests.Session`` 不是线程安全的，所有云端请求都要走同一把锁；
        摄像头跨网授权（``miss_get_vendor``）不在常规读写路径上，用它借锁。
        """
        with self._lock:
            return action()

    @property
    def signed_in(self) -> bool:
        """本地有没有可用凭据。只看文件内容，不联网（``available`` 会真的去请求）。"""
        data = getattr(self._api, "auth_data", None) or {}
        return bool(data.get("userId") and data.get("serviceToken"))

    def account_info(self) -> dict[str, str]:
        """账号页要展示的东西：状态、打码后的 uid、凭据文件位置。"""
        data = getattr(self._api, "auth_data", None) or {}
        uid = str(data.get("userId") or "")
        masked = f"{uid[:4]}****{uid[-2:]}" if len(uid) > 6 else ("已登录" if uid else "未登录")
        return {
            "signed_in": "1" if self.signed_in else "",
            "user": masked,
            "path": str(self.auth_path),
        }

    def logout(self) -> str:
        """退出登录：删掉本机凭据，并把内存里的 token / cookie 一起清掉。

        只动本机凭据，云端和米家 App 里的登录状态不受影响。返回凭据文件路径，
        方便上层在提示里说明"删了哪个文件"。
        """
        with self._lock:
            path = self.auth_path
            error = ""
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:  # 只读目录 / 文件被占用
                error = str(exc)
            # 就地清空而不是换对象：IconCache / Store 都持有这个 api 的引用
            auth = getattr(self._api, "auth_data", None)
            if isinstance(auth, dict):
                auth.clear()
            self._api._available_cache = None  # noqa: SLF001
            self._api._available_cache_time = 0  # noqa: SLF001
            session = getattr(self._api, "session", None)
            cookies = getattr(session, "cookies", None)
            if cookies is not None:
                cookies.clear()  # 不清的话下一次刷新 token 还是拿旧身份
            self.last_error = f"凭据文件删除失败：{error}" if error else ""
            return str(path)

    # -- 拓扑 ------------------------------------------------------------
    def load_topology(self) -> Topology:
        """拉家庭/房间/设备，整理成真机节点树。

        只读操作。失败时抛 ``APIError``，由上层决定是提示还是用缓存。
        """
        with self._lock:
            homes_raw = self._api.get_homes_list()
            devices_raw = self._api.get_devices_list()

        loc = _locate(homes_raw)
        rows: list[dict[str, Any]] = []
        for d in devices_raw:
            did = str(d.get("did", ""))
            if not did:
                continue
            home, room = loc.get(did, ("", ""))
            if not home:
                # 共享家庭的设备：roomlist 里没有，用 home_id 反查
                hid = str(d.get("home_id") or "")
                home = next(
                    (str(h.get("name") or "") for h in homes_raw if str(h.get("id")) == hid),
                    home,
                )
            rows.append(
                {
                    "did": did,
                    "name": d.get("name"),
                    "model": d.get("model"),
                    "spec_type": d.get("spec_type") or "",
                    "online": bool(d.get("isOnline")),
                    "home": home,
                    "room": room,
                    # 摄像头取流用：go2rtc 的 xiaomi 源按局域网地址直连
                    "localip": str(d.get("localip") or ""),
                    "token": str(d.get("token") or ""),
                }
            )

        self._owners = {
            str(h.get("id") or ""): int(h.get("uid") or 0)
            for h in homes_raw
            if h.get("id")
        }

        homes: list[HomeNode] = []
        for h in homes_raw:
            rooms = [
                RoomNode(id=str(r.get("id") or ""), name=str(r.get("name") or ""), order=i)
                for i, r in enumerate(h.get("roomlist") or [])
            ]
            homes.append(
                HomeNode(
                    id=str(h.get("id") or ""),
                    name=str(h.get("name") or ""),
                    rooms=rooms,
                    shared=bool(h.get("shareflag")),
                )
            )

        return Topology(homes=homes, devices=group_devices(rows))

    # -- 在线状态 --------------------------------------------------------
    def device_online(self) -> dict[str, bool]:
        """读一次设备清单，返回 ``did → 是否在线``。

        **在线状态只有这一个来源**：``/miotspec/prop/get``（``datasource=1``，也就是
        我们轮询用的那条）对**离线设备**同样返回云缓存的旧值和 ``code=0``——实测
        四台掉线设备各读 20 条属性，一条离线码都没有；``datasource=2``（强制找设备）
        才会回 ``-704042011``，但 150 条批量要 1.05s（约是 ``datasource=1`` 的 9 倍），
        而且值一模一样，不能挂在 3 秒轮询上。所以"读成功"不等于在线，在线只能
        定期重读设备清单。

        走 ``_request`` 而不是上游的 ``get_devices_list``：后者为了拿 ``home_owner``
        会再请求一次家庭列表，两个家庭累计 5 次请求；这里用拓扑加载时记下的
        ``home_id → uid``，一次家庭一次。设备数 64 条远小于单页 200，不翻页。
        """
        owners = dict(self._owners)
        out: dict[str, bool] = {}
        if not owners:
            return out
        for home_id, owner in owners.items():
            data = {
                "home_owner": owner,
                "home_id": int(home_id) if home_id.isdigit() else home_id,
                "limit": 200,
                "start_did": "",
                "get_split_device": True,
                "support_smart_home": True,
                "get_cariot_device": True,
                "get_third_device": True,
            }
            with self._lock:
                try:
                    ret = self._api._request("/home/home_device_list", data)
                except Exception as exc:  # noqa: BLE001  网络层什么都可能抛
                    self.last_error = f"读取设备在线状态失败: {exc}"
                    continue
            for d in (ret or {}).get("device_info") or []:
                did = str(d.get("did") or "")
                if did:
                    out[did] = bool(d.get("isOnline"))
        return out

    # -- 读 --------------------------------------------------------------
    def read(
        self,
        refs: Iterable[PropRef],
        *,
        chunk: int = DEFAULT_CHUNK,
        source: str = "",
    ) -> dict[PropRef, ReadValue]:
        """批量读属性。已确认不支持的、静默期内的会被跳过。

        返回的 dict 只包含"真的发了请求并且拿到响应"的条目；被跳过的不会出现，
        调用方据此保留旧值。

        ``source="poll"`` 表示这是属性轮询（``Store.poll``），成功与失败都会记进
        ``self.health``。详情页按需读、写后补读这些一次性请求传空串即可——它们不该
        影响"轮询健康"的连续失败计数。
        """
        wanted: list[PropRef] = []
        seen: set[PropRef] = set()
        now = time.monotonic()
        for r in refs:
            if r in seen:
                continue
            seen.add(r)
            if self.capabilities.unsupported(r):
                continue
            until = self._mute.get(r)
            if until and until > now:
                continue
            wanted.append(r)
        if not wanted:
            if source == "poll":
                # 这一轮没有任何属性要读（都被确认不支持 / 仍在静默期）。
                # 这是正常空转，不能算失败，否则设备全掉线时会误报。
                self.last_poll_ok = True
            return {}

        out: dict[PropRef, ReadValue] = {}
        failure: Failure | None = None
        # 开发用故障注入：按「轮询轮次」取一次异常工厂，只对 source="poll" 生效。
        # 没设 MIHOME_DEV_FAULT 时 injector 为 None，这里等于什么都没做。
        injector = dev_fault.current() if source == "poll" else None
        injected = injector.next_poll_error() if injector is not None else None
        for i in range(0, len(wanted), chunk):
            batch = wanted[i : i + chunk]
            part, fail = self._read_batch(batch, injected)
            out.update(part)
            if fail is not None and failure is None:
                # 一次 read 可能分多片，只按第一片的错误记一条，避免把
                # "连续失败次数"按分片放大
                failure = fail

        if source == "poll":
            self._record_poll(out, failure, len(wanted))
        return out

    def _record_poll(
        self,
        out: Mapping[PropRef, ReadValue],
        failure: Failure | None,
        wanted: int,
    ) -> None:
        """把这一轮轮询的结果记进健康账本，并给出"这轮到底成没成"的结论。"""
        if failure is not None:
            self.last_poll_ok = False
            self.health.failure(failure)
            return
        good = sum(1 for rv in out.values() if rv.ok)
        # 请求都回来了，但一条都没读到：整批属性都被服务端按错误码拒了
        # （设备集体离线 / 网关掉线 / 属性集体不可读）。这不能算成功。
        bad = [rv for rv in out.values() if not rv.ok and not rv.unsupported]
        if not good and bad:
            self.last_poll_ok = False
            counts: dict[int, int] = {}
            for rv in bad:
                counts[rv.code] = counts.get(rv.code, 0) + 1
            code = max(counts, key=lambda c: counts[c])
            tail = abs(code) % 1000
            kind = SERVER if tail == RATE_LIMIT_TAIL else NETWORK
            detail = ERROR_CODE.get(str(code), "未知错误")
            self.health.failure(
                Failure(kind, str(code), f"{detail}（本轮 {len(bad)}/{wanted} 条全部失败）")
            )
            return
        # 读到东西了，或压根没有"被拒绝"的条目（全是确认不支持）→ 这轮正常
        self.last_poll_ok = True
        self.health.success(items=good)

    def _read_batch(
        self,
        batch: Sequence[PropRef],
        injected: Callable[[], BaseException] | None = None,
    ) -> tuple[dict[PropRef, ReadValue], Failure | None]:
        """读一片属性。返回值第二项是请求层错误（用于健康记账），不改异常语义。

        ``injected`` 只由开发用故障注入传入：在真正发请求的位置抛出，异常照样走
        下面两个 ``except``，分类与真实故障一模一样。
        """
        payload, remap = _wire_batch(batch)
        with self._lock:
            try:
                if injected is not None:
                    raise injected()
                resp = self._api.get_devices_prop(payload)
            except APIError as exc:
                self.last_error = f"读取失败: {exc}"
                return {}, classify_error(exc)
            except Exception as exc:  # noqa: BLE001  网络层什么都可能抛
                self.last_error = f"读取异常: {exc}"
                return {}, classify_error(exc)

        if isinstance(resp, Mapping):
            resp = [resp]
        if not isinstance(resp, list):
            self.last_error = f"读取失败: 响应结构异常（{type(resp).__name__}）"
            return {}, Failure(PROTOCOL, "", self.last_error)

        # 响应按 (did, siid, piid) 回填；缺的条目说明也没读成功
        out: dict[PropRef, ReadValue] = {}
        for item in resp:
            if not isinstance(item, Mapping):
                continue
            try:
                wire = (
                    str(item.get("did", "")),
                    int(item.get("siid", -1)),
                    int(item.get("piid", -1)),
                )
            except (TypeError, ValueError):
                continue
            ref = remap.get(wire) or PropRef(did=wire[0], siid=wire[1], piid=wire[2])
            code = int(item.get("code") or 0)
            kind = classify(code)
            if kind == "ok":
                out[ref] = ReadValue(
                    ref=ref,
                    value=item.get("value"),
                    code=code,
                    update_time=int(item.get("updateTime") or 0),
                    ok=True,
                )
            elif kind == "unsupported":
                self.capabilities.mark_unsupported(ref, code)
                out[ref] = ReadValue(ref=ref, code=code, unsupported=True, retryable=False)
            else:
                # transient / unknown：可以重试，绝不记死
                out[ref] = ReadValue(ref=ref, code=code, retryable=(kind != "unknown"))

        for r in batch:
            if r not in out:
                out[r] = ReadValue(ref=r, code=-1)
        return out, None

    # -- 写 --------------------------------------------------------------
    def write(
        self,
        values: Sequence[tuple[PropRef, Any]],
        *,
        mute: float | None = None,
    ) -> dict[PropRef, int]:
        """写属性，返回每个 ref 的错误码（0/1 为成功）。

        成功的条目会进入静默期：这之后 ``mute`` 秒内该属性的回读结果一律不采信。
        """
        if not values:
            return {}
        payload: list[dict[str, Any]] = []
        remap: dict[tuple[str, int, int], PropRef] = {}
        for ref, value in values:
            item, routed = _route(ref)
            item["value"] = value
            payload.append(item)
            if routed is not None:
                remap[routed] = ref

        with self._lock:
            try:
                resp = self._api.set_devices_prop(payload)
            except APIError as exc:
                self.last_error = f"写入失败: {exc}"
                return {ref: -1 for ref, _ in values}
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"写入异常: {exc}"
                return {ref: -1 for ref, _ in values}

        if isinstance(resp, Mapping):
            resp = [resp]
        codes: dict[PropRef, int] = {}
        if isinstance(resp, list):
            for item in resp:
                if not isinstance(item, Mapping):
                    continue
                try:
                    wire = (
                        str(item.get("did", "")),
                        int(item.get("siid", -1)),
                        int(item.get("piid", -1)),
                    )
                except (TypeError, ValueError):
                    continue
                ref = remap.get(wire) or PropRef(did=wire[0], siid=wire[1], piid=wire[2])
                codes[ref] = int(item.get("code") or 0)

        window = self._mute_window if mute is None else mute
        now = time.monotonic()
        for ref, _ in values:
            code = codes.get(ref, -1)
            if classify(code) == "ok":
                self._mute[ref] = now + window
        # 顺手清理过期条目，避免长跑后无限增长
        if len(self._mute) > 256:
            self._mute = {k: v for k, v in self._mute.items() if v > now}
        return {ref: codes.get(ref, -1) for ref, _ in values}

    # -- 动作 / 场景 -----------------------------------------------------
    def run_action(self, ref: tuple[str, int, int], value: Sequence[Any] | None = None) -> int:
        """执行一次设备方法，(did, siid, aiid) → 错误码。

        参数键是 ``in``，**不是** ``value``：``value`` 是属性（``prop/set``）的键，
        action 上用它会静默失效——云端按 ``in`` 找参数，一个都找不到，于是报
        ``-704220025 Action参数个数不匹配``，看起来像"设备不支持"，实际是键名错。
        依据：虚拟事件 ``siid 4 / aiid 1`` 用 ``in:["事件名"]`` 实测 ``code 0``
        （见 ``docs/reference/mijia-automation-events.md``）；``-704220025`` 的释义
        见 ``mijiaAPI/errors.py``。
        """
        did, siid, aiid = ref
        virt = parse_virtual_did(did)
        if virt is not None:  # 子设备 did 同样要换成父 did + 该路 siid
            did, siid = virt[0], virt[1]
        item: dict[str, Any] = {"did": did, "siid": siid, "aiid": aiid}
        if value is not None:
            item["in"] = list(value)
        with self._lock:
            try:
                resp = self._api.run_action(item)
            except APIError as exc:
                self.last_error = f"动作失败: {exc}"
                return -1
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"动作异常: {exc}"
                return -1
        if isinstance(resp, list) and resp and isinstance(resp[0], Mapping):
            return int(resp[0].get("code") or 0)
        if isinstance(resp, Mapping):
            return int(resp.get("code") or 0)
        return 0

    def run_scene(self, scene_id: str, home_id: str) -> bool:
        with self._lock:
            try:
                return bool(self._api.run_scene(scene_id, home_id))
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"场景执行失败: {exc}"
                return False

    def scenes(self, home_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            try:
                return list(self._api.get_scenes_list(home_id))
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"场景列表失败: {exc}"
                return []

    # -- 杂务 ------------------------------------------------------------
    def flush(self) -> None:
        """把能力账本落盘（退出前 / 定时调用）。"""
        self.capabilities.save()


__all__ = [
    "CapabilityBook",
    "Client",
    "DEFAULT_CHUNK",
    "HomeNode",
    "PropRef",
    "ReadValue",
    "RoomNode",
    "SUCCESS_CODES",
    "Topology",
    "classify",
]
