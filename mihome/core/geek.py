# SPDX-License-Identifier: GPL-3.0-or-later
"""米家自动化极客版：一条云端 RPC 换一枚登录码 + **能力探测**。

极客版是设备自带的局域网后台（浏览器打开 ``http://<设备 IP>/``），密码是 6 位
数字、**用一次就废**。码不是设备本地生成的，而是米家云端返回的——App 里
「中枢网关 → 自动化极客版」那几层菜单，本质只是替用户发这条请求。所以程序不必
抓包、不必掏手机：拿现有账号凭据直接调即可（2026-11-07 实测）。

契约（详见 ``docs/reference/mijia-geek-login-code.md``）：

    POST https://api.mijia.tech/app/home/rpc/<did>
    {"method": "miIO.get_autowebconfig_url",     "params": []}  → {"url": "http://<网关 IP>"}
    {"method": "miIO.get_central_link_passcode", "params": []}  → {"passcode": "123456"}

签名 / RC4 / 解密全由 ``mijiaAPI._request()`` 现成做完，这里只负责：

* **先取地址再取码**——取地址不耗码，地址都拿不到就早退，别白费一枚；
* **把失败翻成一句人话**，并尽量把地址带回给调用方（网页还能打开，码手输）。

## 谁有极客版：型号白名单 + 窄口径探测兜底（2026-11-16 改）

MIoT 规格里**没有**这项能力（中枢网关的 spec 只有网络 / 指示灯 / 虚拟事件），
所以没有任何"读规格就知道"的判据，只能由人登记型号：

* **白名单**（``adapters`` 台账里的 ``geek=True``）是主判据。命中就**零请求**
  直接显示入口——开机第一帧就是极客版卡，不用等网络往返。能力是**型号属性**，
  同一型号不会一台有一台没有，所以登记一次管一台设备。
* **探测**只对 ``gateway`` / ``router`` / ``screen`` / ``central-control`` 这四类
  兜底，用来"捡漏"还没登记的型号（``get_autowebconfig_url`` 不耗码，问一句就知道）。

为什么不给每台设备都探（2026-11-16 实测的代价）：这台机器上 43 条探测记录
只换来 **1 台**设备显示入口，38 个型号被判否，而否定结论 7 天后还要重探一次——
等于每周拿几十条请求去赌一个本该写死的事实。原来不写死名单的理由是"漏一个型号
就永远看不到入口"，现在由台账（``scripts/list_adapters.py``）回答"哪些型号登记过"，
漏了能查出来，不必靠全量请求来兜。

探测结论仍按 ``did`` 落进 ``geek_ability.json``（``ability_of`` / ``remember``），
只作为未登记型号的事后证据，不再是唯一来源。

**取新码 = 旧码立即失效**。所以取码这条链没有任何缓存、重试、轮询或预取：
一次调用 = 用户主动点一下 = 消耗一枚码。探测走的是另一条方法，不消耗码。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import paths
from .adapters import find_adapter

URL_METHOD = "miIO.get_autowebconfig_url"
CODE_METHOD = "miIO.get_central_link_passcode"
#: 返回体里登录码可能出现的键（实测是 ``passcode``，其余是兜底）
_CODE_KEYS = ("passcode", "passwd", "password", "pwd")

#: 文案里用的地址是否看起来可用（云端理论上只给 http(s) 地址）
_URL_PREFIXES = ("http://", "https://")

#: 能力缓存文件名（数据根目录下，和 ``prefs.json`` 同级；只是缓存，删了会重探）
ABILITY_FILE = "geek_ability.json"
#: "不支持"的结论多久后重探（秒）。探测失败可能是当时设备没应答、云端抽风，
#: 把一次失败当永久结论会让用户永远看不到这个样式——所以给个自愈窗口。
NEGATIVE_TTL = 7 * 24 * 3600
#: "支持"的结论不带时效：型号变了才会重探（重新配对 / 换设备）

_CACHE_VERSION = 1


class GeekError(RuntimeError):
    """取码失败。``url`` 有值时表示地址拿到了、只是码没拿到——网页仍可打开。"""

    def __init__(self, message: str, url: str = "") -> None:
        super().__init__(message)
        self.url = url


@dataclass(frozen=True, slots=True)
class Entry:
    """一次成功取码的结果。码只在内存里传，不日志、不落盘。"""

    url: str
    code: str


# ---------------------------------------------------------------------------
# 能力缓存：did → {model, ok, at}
# ---------------------------------------------------------------------------

_cache: dict[str, dict[str, Any]] | None = None


def ability_path() -> Path:
    return paths.data_dir() / ABILITY_FILE


def forget() -> None:
    """丢掉内存里的缓存（测试用；不删磁盘文件）。"""
    global _cache
    _cache = None


def _records() -> dict[str, dict[str, Any]]:
    global _cache
    if _cache is not None:
        return _cache
    _cache = {}
    try:
        data = json.loads(ability_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _cache
    devices = data.get("devices") if isinstance(data, dict) else None
    if isinstance(devices, dict):
        _cache = {k: v for k, v in devices.items() if isinstance(v, dict)}
    return _cache


def _save() -> None:
    payload = {"version": _CACHE_VERSION, "devices": _records()}
    path = ability_path()
    try:
        # 先写临时文件再替换：写到一半断电不会留下半个 JSON，害得下次全量重探
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        # 写不进去只影响"下次还要再探一次"，绝不能让取码 / 界面挂掉
        pass


def ability_of(did: str, model: str = "", *, now: float | None = None) -> bool | None:
    """这台设备有没有极客版：``True`` / ``False`` / ``None``（还没探过）。

    ``model`` 给了就校验：型号变了（重新配对、换了一台）旧结论不能沿用，
    返回 ``None`` 让它重探一次。
    """
    if not did:
        return None
    rec = _records().get(did)
    if rec is None:
        return None
    if model and rec.get("model") and rec["model"] != model:
        return None
    if rec.get("ok"):
        return True
    stamp = rec.get("at") if isinstance(rec.get("at"), (int, float)) else 0
    if (now or time.time()) - stamp > NEGATIVE_TTL:
        # 否定结论过期：当时可能是离线 / 云端临时错误，重探一次
        return None
    return False


def supports(did: str, model: str = "") -> bool:
    """这张卡片能不能挂「极客版入口」样式。

    **先看型号白名单**（``adapters`` 台账里的 ``geek=True``）：命中就直接 True，
    不看探测缓存、也不发任何请求。这是主路径——能力是型号属性，登记一次就够了。

    没登记才退到运行期证据：这台自己的探测记录，或**同型号已被证实**（同一型号里
    只要有一台探通了，新来的那台不必再发一轮请求）。
    """
    if declared(model):
        return True
    if ability_of(did, model) is True:
        return True
    return bool(model) and model in confirmed_models()


def declared(model: str) -> bool:
    """型号台账里是否登记了「有极客版」——白名单，命中就不必探测。

    ``candidates`` 也用它跳过探测：已登记的设备再发一条请求，纯属白问。
    """
    if not model:
        return False
    adapter = find_adapter(model)
    return bool(adapter is not None and adapter.geek)


def confirmed_models() -> set[str]:
    """已被探测证实的型号集合（``HUB_MODELS`` 的运行期版本）。"""
    models: set[str] = set()
    now = time.time()
    for rec in _records().values():
        if rec.get("ok") and isinstance(rec.get("model"), str) and rec["model"]:
            stamp = rec.get("at") if isinstance(rec.get("at"), (int, float)) else 0
            if now - stamp < 365 * 24 * 3600:  # 超过一年的老记录也当没说过
                models.add(rec["model"])
    return models


def remember(did: str, model: str, ok: bool) -> None:
    """记下探测结论。``ok=False`` 只在 ``NEGATIVE_TTL`` 内有效。"""
    if not did:
        return
    _records()[did] = {"model": model or "", "ok": bool(ok), "at": int(time.time())}
    _save()


#: 只有这几类设备才值得发探测。极客版是"设备自带局域网后台"，只有中枢 / 网关 /
#: 中控屏这类东西才有；灯、插座、传感器、摄像头问了也是白问（实测 43 条请求里
#: 41 条打在这上面，全部返回"没有"）。**漏掉的新品类仍有救**：型号登记进
#: ``adapters`` 台账就是白名单，不依赖探测（见 :func:`declared`）。
PROBE_CATEGORIES = frozenset({"gateway", "router", "screen", "central-control"})


def candidates(
    nodes: list[Any], shared_homes: set[str], limit: int = 0
) -> list[Any]:
    """挑出**该探**的设备：只挑中枢类里还没登记、也还没有结论的那些。

    * 跳过共享家庭的设备：那是另一户人家，不该替他们发请求（见 ``TODO`` 的
      测试纪律）；
    * 跳过离线设备：云端要转发给设备，不在线只会拿到超时，白探还占队列；
    * 跳过**白名单已登记**的型号（``declared``）：能力是型号属性，登记过就是答案，
      再问一次纯属白烧请求；
    * 跳过已知结论的设备（``ability_of`` 非 ``None``）；
    * 只探 ``PROBE_CATEGORIES`` 里的品类——这一条把候选从"全屋几十台"压到
      "一两台"，是 2026-11-16 那次改动的核心（理由见模块 docstring）。

    ``limit`` 大于 0 时只取前 N 台：给"某户装了一排中控屏"这种情况封顶，
    别让探测挤在正常轮询前面。
    """
    picked: list[tuple[str, Any]] = []
    for node in nodes:
        if getattr(node, "home", "") in shared_homes:
            continue
        if not getattr(node, "online", True):
            continue
        did = getattr(node, "did", "")
        model = getattr(node, "model", "") or ""
        if not did or ability_of(did, model) is not None:
            continue
        if declared(model):
            continue
        if model and model in confirmed_models():
            continue
        if _category(node) not in PROBE_CATEGORIES:
            continue
        picked.append((did, node))
    picked.sort(key=lambda item: item[0])
    out = [node for _, node in picked]
    return out[:limit] if limit > 0 else out


def _category(node: Any) -> str:
    """设备品类的粗略判断，**只用来筛掉非中枢类**。

    宁可粗略也不要漏：这一段来自 ``spec_type`` 的 URN，判错最多是"该探的没探"
    （型号登记白名单仍能救回来），不会让一台真的中枢设备永远看不到入口。
    """
    urn = getattr(node, "spec_type", "") or ""
    # urn:miot-spec-v2:device:<品类>:<型号码>:<型号>:<版本>，第 3 段才是品类
    # （和 ``spec._urn_name`` 同一个取法，这里不 import 它，免得 core 里多一条依赖）
    parts = urn.split(":")
    return parts[3] if len(parts) > 3 else ""


# ---------------------------------------------------------------------------
# 云端调用
# ---------------------------------------------------------------------------


def url_of(result: Any) -> str:
    """从 ``get_autowebconfig_url`` 的返回里取地址（dict 或纯字符串都认）。"""
    if isinstance(result, dict):
        for key in ("url", "webconfig", "address"):
            value = result.get(key)
            if isinstance(value, str) and value:
                return value
        return ""
    if isinstance(result, str):
        return result
    return ""


def code_of(result: Any) -> str:
    """从 ``get_central_link_passcode`` 的返回里取 6 位码（数字也认）。"""
    if isinstance(result, dict):
        for key in _CODE_KEYS:
            value = result.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()
        return ""
    if isinstance(result, (str, int)) and not isinstance(result, bool):
        return str(result).strip()
    return ""


def _call(api: Any, did: str, method: str, what: str) -> Any:
    try:
        return api._request(f"/home/rpc/{did}", {"method": method, "params": []})
    except Exception as exc:  # noqa: BLE001  上游抛什么的都有，统一收口
        raise GeekError(f"{what}失败：{exc}") from exc


def probe(api: Any, did: str) -> bool:
    """问一句"这台有没有极客版"：只发 ``get_autowebconfig_url``（**不耗码**）。

    返回 True = 云端给了可用地址；其余（报错、地址为空、地址不像 http）算不支持。
    异常一律吞成 False 但**仍会落盘**——过 ``NEGATIVE_TTL`` 天会自愈重探，
    不会因为一次超时就把一台中枢永久排除。
    """
    if not did:
        return False
    try:
        url = url_of(_call(api, did, URL_METHOD, "探测极客版"))
    except GeekError:
        return False
    return url.startswith(_URL_PREFIXES)


def fetch(api: Any, did: str) -> Entry:
    """取一次地址 + 一枚登录码。失败抛 :class:`GeekError`（不重试）。

    ``api`` 是 ``mijiaAPI`` 实例（``Client.raw()``）——直接收上游对象而不是
    ``Client``，验证脚本才能塞个假的进来，不必联网。
    """
    if not did:
        raise GeekError("没有可用的中枢网关")
    url = url_of(_call(api, did, URL_METHOD, "取网关地址"))
    if not url:
        raise GeekError("云端没有返回网关地址")
    if not url.startswith(_URL_PREFIXES):
        # 理论不会发生；真发生了也别把一个奇怪的串塞进浏览器
        raise GeekError(f"网关地址不可用：{url}")
    try:
        code = code_of(_call(api, did, CODE_METHOD, "取登录码"))
    except GeekError as exc:
        # 地址已经拿到了：把它带出去，网页照样能打开，只是码要手输
        raise GeekError(str(exc), url) from exc
    if not code:
        raise GeekError("云端没有返回登录码", url)
    return Entry(url=url, code=code)


__all__ = [
    "ABILITY_FILE",
    "CODE_METHOD",
    "NEGATIVE_TTL",
    "URL_METHOD",
    "Entry",
    "GeekError",
    "ability_of",
    "ability_path",
    "candidates",
    "code_of",
    "confirmed_models",
    "fetch",
    "forget",
    "probe",
    "remember",
    "supports",
    "url_of",
]
