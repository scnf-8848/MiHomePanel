# SPDX-License-Identifier: GPL-3.0-or-later
"""米家自动化极客版：一条云端 RPC 换一枚登录码 + **能力探测**。

极客版是设备自带的局域网后台（浏览器打开 ``http://<设备 IP>/``），密码是 6 位
数字、**用一次就废**。码不是设备本地生成的，而是米家云端返回的——App 里
「中枢网关 → 自动化极客版」那几层菜单，本质只是替用户发这条请求。所以程序不必
抓包、不必掏手机：拿现有账号凭据直接调即可（2026-11-07 实测）。

契约（详见 ``docs/reference/mijia-geek-login-code.md``）：

    POST https://api.mijia.tech/app/home/rpc/<did>
    {"method": "miIO.get_autowebconfig_url",     "params": []}  → {"url": "http://192.168.1.10"}
    {"method": "miIO.get_central_link_passcode", "params": []}  → {"passcode": "123456"}

签名 / RC4 / 解密全由 ``mijiaAPI._request()`` 现成做完，这里只负责：

* **先取地址再取码**——取地址不耗码，地址都拿不到就早退，别白费一枚；
* **把失败翻成一句人话**，并尽量把地址带回给调用方（网页还能打开，码手输）。

## 谁有极客版：探测出来的，不是写死的

MIoT 规格里**没有**这项能力（中枢网关的 spec 只有网络 / 指示灯 / 虚拟事件），
所以没有任何"读规格就知道"的判据。但 ``get_autowebconfig_url`` **不耗码**，
调一次就能问出"这台能不能给地址"——这就是判据。

于是 ``HUB_MODELS`` 那种型号常量不再需要维护：程序对设备发一条探测请求，
结果按 ``did`` 落进 ``geek_ability.json``（型号变了 / 结论是"不支持"且超过
``NEGATIVE_TTL`` 天才重探）。**型号级白名单是探测的产物，不是配置项**：
同一型号只要有一台确认支持，后续同型号设备直接算支持，不用再探。

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

    先看这台自己的探测记录，再看**同型号是否已被证实**——同一型号里只要有一台
    探通了，新来的那台不必再发一轮请求（换设备 / 新配对时省一条）。
    """
    if ability_of(did, model) is True:
        return True
    return bool(model) and model in confirmed_models()


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


def candidates(
    nodes: list[Any], shared_homes: set[str], limit: int = 0
) -> list[Any]:
    """挑出**该探**的设备，按"最可能是中枢"的顺序排。

    * 跳过共享家庭的设备：那是另一户人家，不该替他们发请求（见 ``TODO`` 的
      测试纪律）；
    * 跳过离线设备：云端要转发给设备，不在线只会拿到超时，白探还占队列；
    * 已知结论的设备跳过（``ability_of`` 非 ``None``）；
    * ``gateway`` / ``router`` / ``screen`` 排前面——极客版就长在这几类上，
      设备多的家里头一批就能命中，不用等整轮扫完。

    ``limit`` 大于 0 时只取前 N 台：一次启动探完全部设备，在几十台设备的家里
    就是几十条请求挤在正常轮询前面。分批之后每轮都是小步。
    """
    first = {"gateway", "router", "screen", "central-control"}
    ranked: list[tuple[int, str, Any]] = []
    for node in nodes:
        if getattr(node, "home", "") in shared_homes:
            continue
        if not getattr(node, "online", True):
            continue
        did = getattr(node, "did", "")
        model = getattr(node, "model", "") or ""
        if not did or ability_of(did, model) is not None:
            continue
        if model and model in confirmed_models():
            continue
        ranked.append((0 if _category(node) in first else 1, did, node))
    ranked.sort(key=lambda item: (item[0], item[1]))
    picked = [node for _, _, node in ranked]
    return picked[:limit] if limit > 0 else picked


def _category(node: Any) -> str:
    """设备品类的粗略判断。

    这里只用来**排序**（先探谁），不做筛选，所以宁可粗略：真判错也只是顺序差一点，
    不会漏掉设备。品类字符串来自 ``spec_type`` 的 URN 段。
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
