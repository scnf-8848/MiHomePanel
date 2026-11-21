# SPDX-License-Identifier: GPL-3.0-or-later
"""验证中枢网关的「极客版入口」卡片（2×1：左产品图 + 右大按钮）。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_geek.py``

全程离线，不发任何真实请求：

* **取码逻辑**（``core/geek.py``）：云端返回的四种形态；先取地址再取码的顺序
  （地址失败时**不能**白白消耗一枚码）；失败时把地址带回去。
* **能力判据**（``core/geek.py``）：**型号白名单优先**（台账 ``geek=True`` 命中就
  零请求给入口）；没登记的型号才看探测结论——否定结论会过期自愈、换型号重探、
  同型号复用「支持」；该探谁（只探中枢类 / 跳过共享家庭 / 离线 / 已知 / 已登记）。
* **样式可见性**（``card_templates``）：**白名单或探通了才出现**——两者都没有时
  菜单里只有「标准」。
* **卡片与按钮**（``ui/device_card.py`` + ``ui/geek_button.py``）：2×1 尺寸、左右各半
  不重叠、点击发信号、取码中与离线时点不动（连点会白烧码）、切回标准样式后按钮消失；
  结果态的三种文案与配色，以及 **30 秒后自己退回常态**。
* **1×1 小卡**：同一套代码的窄落点——按钮铺满 body、不画产品图、三种状态的每一行
  都放得下（不会悄悄省略掉动作名）；切回 2×1 时几何 / 文案 / 产品图都跟着回来。
* **接线**（``WallView._on_geek_done``）：成功时写剪贴板 + 打开浏览器；
  失败但拿到地址时仍然打开网页。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_TMP = Path(tempfile.mkdtemp(prefix="mihome-geek-"))
os.environ["MIHOME_DATA_DIR"] = str(_TMP / "data")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPixmap  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mihome.core import geek  # noqa: E402
from mihome.core.catalog import PROFILES, CardSpec  # noqa: E402
from mihome.core.store import DeviceState, Store  # noqa: E402
from mihome.core.tree import DeviceNode  # noqa: E402
from mihome.ui import card_templates as ct  # noqa: E402
from mihome.ui import theme, tooltip, wall  # noqa: E402
from mihome.ui.device_card import DeviceCard  # noqa: E402
from mihome.ui.geek_button import BTN_HOVER, RESULT_MS  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(
        f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}",
        flush=True,
    )
    if not ok:
        FAILS.append(f"{name} {detail}")


HUB = "xiaomi.gateway.hub1"          # 台账白名单里的型号（adapters 的 geek=True）
UNLISTED = "xiaomi.gateway.hub2"     # 没登记、也没探过：走"未知"这条路
MULTI = "lumi.gateway.mcn001"        # 不支持的否定结论用例


def hub_card(did: str = "hub1", model: str = HUB) -> CardSpec:
    return CardSpec(category="gateway", profile=PROFILES["gateway"], did=did, model=model)


def reset() -> None:
    """清空能力缓存（内存 + 磁盘），每个用例自己从零开始。"""
    geek.forget()
    try:
        geek.ability_path().unlink()
    except OSError:
        pass
    geek.forget()


def hub_state(did: str = "hub1", *, online: bool = True, model: str = HUB) -> DeviceState:
    node = DeviceNode(
        did, "Xiaomi 中枢网关", model,
        "urn:miot-spec-v2:device:gateway:0000A019:xiaomi-hub1:3",
        home="我的家", room="客厅", online=online,
    )
    return DeviceState(node=node, spec=None, card=hub_card(did, model))


class Icons:
    def ensure(self, model: str) -> None:
        return None

    def scaled(self, model: str, size: int) -> QPixmap | None:
        return None


class FakeApi:
    """假的上游：按方法名返回预设结果，或者按预设抛异常。"""

    def __init__(self, *, url=None, code=None, raise_on: str = "", exc: Exception | None = None):
        self.url = url
        self.code = code
        self.raise_on = raise_on
        self.exc = exc
        self.calls: list[str] = []

    def _request(self, uri: str, data: dict):
        method = data["method"]
        self.calls.append(method)
        if method == self.raise_on:
            raise self.exc or RuntimeError("boom")
        if method == geek.URL_METHOD:
            return self.url
        return self.code


# ---------------------------------------------------------------------------
# 1. 取码逻辑
# ---------------------------------------------------------------------------

def test_core() -> None:
    check("地址：dict", geek.url_of({"url": "http://192.0.2.10"}) == "http://192.0.2.10")
    check("地址：纯字符串", geek.url_of("http://192.0.2.10") == "http://192.0.2.10")
    check("地址：别的键兜底", geek.url_of({"webconfig": "http://a"}) == "http://a")
    check("地址：认不出返回空", geek.url_of({"foo": 1}) == "" and geek.url_of(None) == "")

    check("登录码：dict", geek.code_of({"passcode": "123456"}) == "123456")
    check("登录码：数字也认", geek.code_of({"passcode": 123456}) == "123456")
    check("登录码：别名叫法", geek.code_of({"passwd": "654321"}) == "654321")
    check("登录码：认不出返回空", geek.code_of({}) == "" and geek.code_of(True) == "")

    api = FakeApi(url={"url": "http://192.0.2.10"}, code={"passcode": "123456"})
    entry = geek.fetch(api, "hub1")
    check("成功时两个字段都对", (entry.url, entry.code) == ("http://192.0.2.10", "123456"),
          f"{entry.url} / {entry.code}")
    check("先取地址后取码（顺序不能反）", api.calls == [geek.URL_METHOD, geek.CODE_METHOD],
          str(api.calls))

    api = FakeApi(url=None, code={"passcode": "1"}, raise_on=geek.URL_METHOD, exc=RuntimeError("网络断了"))
    try:
        geek.fetch(api, "hub1")
    except geek.GeekError as exc:
        check("地址失败会抛 GeekError 并带原因", "取网关地址失败" in str(exc), str(exc))
    else:
        check("地址失败会抛 GeekError 并带原因", False, "没抛")
    check("地址失败时**没有**调取码接口（不白烧一枚码）",
          api.calls == [geek.URL_METHOD], str(api.calls))

    api = FakeApi(url={"url": "http://192.0.2.10"}, raise_on=geek.CODE_METHOD,
                  exc=RuntimeError("token 过期"))
    try:
        geek.fetch(api, "hub1")
    except geek.GeekError as exc:
        check("取码失败也抛，但把地址带回去（网页还能开）",
              exc.url == "http://192.0.2.10" and "取登录码失败" in str(exc),
              f"{exc.url} / {exc}")
    else:
        check("取码失败也抛，但把地址带回去（网页还能开）", False, "没抛")

    try:
        geek.fetch(FakeApi(url={"url": "http://x"}, code={}), "hub1")
    except geek.GeekError as exc:
        check("云端没给码时明确说清楚", "没有返回登录码" in str(exc), str(exc))
    else:
        check("云端没给码时明确说清楚", False, "没抛")

    try:
        geek.fetch(FakeApi(url={"url": "ftp://x"}, code={"passcode": "1"}), "hub1")
    except geek.GeekError as exc:
        check("非 http 地址不往浏览器里塞", "地址不可用" in str(exc), str(exc))
    else:
        check("非 http 地址不往浏览器里塞", False, "没抛")

    try:
        geek.fetch(FakeApi(url={"url": "http://x"}), "")
    except geek.GeekError as exc:
        check("没有 did 直接拒（不拼出坏 URL）", "没有可用的中枢网关" in str(exc), str(exc))
    else:
        check("没有 did 直接拒（不拼出坏 URL）", False, "没抛")


# ---------------------------------------------------------------------------
# 2. 样式可见性
# ---------------------------------------------------------------------------

def test_ability() -> None:
    reset()
    # 主判据是**型号白名单**（adapters 台账 geek=True）：登记过的型号零请求就给
    # 入口，连"探过没有"都不必问。这是 2026-11-16 改判据后新增的第一条路径。
    check("白名单命中：没探过也给入口", geek.supports("x0", HUB))
    check("白名单命中：不写探测缓存（一个请求都没发）", geek.ability_of("x0", HUB) is None)
    check("白名单只认整串型号，不认前缀",
          not geek.supports("x0", "xiaomi.gateway.hub10"))

    check("未登记也没探过 = 未知（不当成没有）", geek.ability_of("x1", UNLISTED) is None)
    check("未知就不给入口", not geek.supports("x1", UNLISTED))

    geek.remember("x1", UNLISTED, True)
    check("探通了记住", geek.ability_of("x1", UNLISTED) is True)
    check("supports 跟上", geek.supports("x1", UNLISTED))
    check("同型号的新设备不用再探一轮", geek.supports("x9", UNLISTED))
    geek.forget()
    check("结论落盘（下次启动不用重探）", geek.supports("x1", UNLISTED))
    check("换型号（重新配对）就当没探过",
          geek.ability_of("x1", "xiaomi.gateway.hub3") is None,
          str(geek.ability_of("x1", "xiaomi.gateway.hub3")))

    geek.remember("y1", MULTI, False)
    check("「说不支持」也记下来", geek.ability_of("y1", MULTI) is False)
    check("同型号另一台不靠否定结论推断（模型级只复用「支持」）",
          geek.supports("y2", MULTI) is False)
    check("否定结论会过期（当时可能只是离线/抽风）",
          geek.ability_of("y1", MULTI, now=time.time() + geek.NEGATIVE_TTL + 1) is None)

    # 探测只发「取地址」：这条不耗码，是能否自动化发现的全部前提
    api = FakeApi(url={"url": "http://192.0.2.10"})
    check("探测：给了地址就算支持", geek.probe(api, "d1"))
    check("探测：只调一条 RPC，且不是取码那一条",
          api.calls == [geek.URL_METHOD] and geek.CODE_METHOD not in api.calls,
          str(api.calls))
    check("探测：没给地址算不支持", not geek.probe(FakeApi(url={}), "d2"))
    check("探测：给了个不像 http 的串也算不支持",
          not geek.probe(FakeApi(url={"url": "ftp://x"}), "d3"))
    check("探测：报错吞成不支持（不冒泡）",
          not geek.probe(FakeApi(raise_on=geek.URL_METHOD), "d4"))
    check("探测：没 did 直接 False", not geek.probe(api, ""))


def test_candidates() -> None:
    reset()
    nodes = [
        DeviceNode("l1", "客厅灯", "yeelink.light.lamp22",
                   "urn:miot-spec-v2:device:light:0000A001:yeelink-lamp22:1",
                   home="我的家", online=True),
        DeviceNode("r1", "路由器", "xiaomi.router.rd08",
                   "urn:miot-spec-v2:device:router:0000A036:xiaomi-rd08:1",
                   home="我的家", online=True),
        DeviceNode("s1", "共享客厅灯", "yeelink.light.lamp22",
                   "urn:miot-spec-v2:device:light:0000A001:yeelink-lamp22:1",
                   home="共享家庭", online=True),
        DeviceNode("o1", "离线网关", "xiaomi.gateway.hub9",
                   "urn:miot-spec-v2:device:gateway:0000A019:xiaomi-hub9:1",
                   home="我的家", online=False),
        DeviceNode("k1", "已探过", "xiaomi.gateway.hub8",
                   "urn:miot-spec-v2:device:gateway:0000A019:xiaomi-hub8:1",
                   home="我的家", online=True),
        DeviceNode("h1", "中枢网关", HUB,
                   "urn:miot-spec-v2:device:gateway:0000A019:xiaomi-hub1:3",
                   home="我的家", online=True),
    ]
    geek.remember("k1", "xiaomi.gateway.hub8", False)
    got = [n.did for n in geek.candidates(nodes, {"共享家庭"})]
    check("共享家庭整户跳过", "s1" not in got, str(got))
    check("离线设备跳过", "o1" not in got, str(got))
    check("已有结论的跳过", "k1" not in got, str(got))
    check("只探中枢类：灯这种问了也是白问", "l1" not in got, str(got))
    check("白名单型号不探（登记过就是答案，省一条请求）", "h1" not in got, str(got))
    check("剩下该探的就是那台没登记的路由", got == ["r1"], str(got))
    check("一次只派前 N 台（别一轮把队列塞满）",
          [n.did for n in geek.candidates(nodes, {"共享家庭"}, limit=1)] == ["r1"])


def test_styles() -> None:
    reset()
    style = ct.style_by_id(ct.GATEWAY_GEEK)
    check("样式注册成了 2×1", style is not None and (style.span, style.rows) == (2, 1),
          str(style and (style.span, style.rows)))
    check("body 是极客版那一种", style is not None and style.body == ct.BODY_GEEK,
          str(style and style.body))
    check("它有 body（不画产品图铺满整条）", bool(style and style.has_body))
    check("门槛是「能力」而不是写死的型号",
          style.ability == ct.ABILITY_GEEK and not hasattr(style, "models"),
          style.ability)

    check("未登记也没探过：菜单里只有标准卡（不猜也不许点）",
          [s.id for s in ct.available_styles(hub_card("x1", UNLISTED))] == [ct.STANDARD])
    check("白名单型号：不用探测就有三种样式（开机第一帧就是小卡）",
          [s.id for s in ct.available_styles(hub_card())]
          == [ct.GATEWAY_GEEK_SMALL, ct.STANDARD, ct.GATEWAY_GEEK])

    geek.remember("hub1", HUB, True)
    ids = [s.id for s in ct.available_styles(hub_card())]
    check("探通了才出现：默认的小卡排第一，标准与宽卡跟上",
          ids == [ct.GATEWAY_GEEK_SMALL, ct.STANDARD, ct.GATEWAY_GEEK], str(ids))
    menu = [s.menu_label for s in ct.available_styles(hub_card())]
    check("菜单文字带尺寸前缀", menu[-1] == "宽卡 2×1：极客版入口", str(menu))
    check("第一项就是默认的小卡（菜单第一项 = 回默认）",
          menu[0] == "小卡 1×1：极客版入口", str(menu))
    check("探过的同型号新设备直接可用",
          ct.style_by_id(ct.GATEWAY_GEEK).available(hub_card("hub9", HUB)))

    geek.remember("m1", MULTI, False)
    check("探了说不支持的看不到",
          [s.id for s in ct.available_styles(hub_card("m1", MULTI))] == [ct.STANDARD],
          str([s.id for s in ct.available_styles(hub_card("m1", MULTI))]))
    # 品类不再是门槛：带中枢的路由器 / 中控屏品类不是 gateway，照样该有入口
    check("能力确认的设备不看品类（路由器也能有）",
          ct.style_by_id(ct.GATEWAY_GEEK).available(
              CardSpec(category="router", profile=PROFILES["gateway"], did="hub1", model=HUB)
          ) is True)
    check("能力没确认的设备，什么品类都没有这一项",
          ct.style_by_id(ct.GATEWAY_GEEK).available(
              CardSpec(category="router", profile=PROFILES["gateway"],
                       did="zzz", model="xiaomi.router.rd08")
          ) is False)

    check("探测通过后默认就是 1×1 小卡（网关品类声明，2026-11-16 用户定）",
          ct.default_style(hub_card()).id == ct.GATEWAY_GEEK_SMALL,
          ct.default_style(hub_card()).id)
    check("探测不出极客版的网关退回标准卡（默认也要过能力判据）",
          ct.default_style(hub_card("m1", MULTI)).id == ct.STANDARD,
          ct.default_style(hub_card("m1", MULTI)).id)
    check("别的品类不跟着变小卡（默认只写在 gateway 品类上）",
          ct.default_style(
              CardSpec(category="light", profile=PROFILES["light"], did="l1", model="yeelink.light.1")
          ).id == ct.STANDARD)
    check("探测前默认退回标准卡（能力没确认，不能赌）",
          ct.default_style(hub_card("fresh", "xiaomi.gateway.unknown999")).id == ct.STANDARD,
          ct.default_style(hub_card("fresh", "xiaomi.gateway.unknown999")).id)
    # 真正建卡走的是 resolve_card_style，不是 default_style：无偏好时必须直接给小卡
    no_prefs = SimpleNamespace(card_style=lambda _did: None)
    check("无偏好时建卡直接落到小卡（不用等用户点菜单）",
          ct.resolve_card_style(no_prefs, "hub1", hub_card()).id == ct.GATEWAY_GEEK_SMALL,
          ct.resolve_card_style(no_prefs, "hub1", hub_card()).id)


def test_store_wiring() -> None:
    """Store 侧：探测结果落盘 + 通知界面；探测自己抛错时不落盘。"""
    reset()
    store = Store.__new__(Store)  # 跳过 __init__（要真 client / worker）
    store._geek_pending = {"d1"}
    seen: list[str] = []
    store.on_geek_ability = seen.append
    Store._on_ability(store, "d1", HUB, True, None)
    check("探通了落盘", geek.supports("d1", HUB))
    check("探通了通知界面", seen == ["d1"], str(seen))
    check("落定后从待办里摘掉", store._geek_pending == set(), str(store._geek_pending))

    geek.forget()
    stall = Store.__new__(Store)
    stall._geek_pending = {"d2"}
    Store._on_ability(stall, "d2", "m2", None, RuntimeError("worker 崩了"))
    check("探测自己出错时不落盘（下次启动再来）",
          geek.ability_of("d2", "m2") is None)


# ---------------------------------------------------------------------------
# 2b. 探测结论回来时，哪些卡片要重解析
# ---------------------------------------------------------------------------

def test_ability_repair() -> None:
    """``WallView._on_geek_ability`` 的四条分支。

    "偏好为空"那条容易漏：网关的默认样式改成极客版小卡之后，没选过样式的设备也
    必须重解析——探测前 ``available()`` 不成立，卡片先按标准产品图显示，结论回来
    不重解析就会一直停在产品图上，直到重启。
    """
    calls: list[str] = []

    def fake(want: str | None, card: CardSpec, did: str = "hub1"):
        return SimpleNamespace(
            _cards={did: object()},
            _store=SimpleNamespace(
                devices={did: SimpleNamespace(card=card)},
                prefs=SimpleNamespace(card_style=lambda _did: want),
            ),
            apply_card_style=calls.append,
        )

    calls.clear()
    wall.WallView._on_geek_ability(fake(None, hub_card()), "hub1")  # noqa: SLF001
    check("偏好为空、默认是小卡：结论回来要重解析（否则永远停在产品图）",
          calls == ["hub1"], str(calls))

    calls.clear()
    wall.WallView._on_geek_ability(  # noqa: SLF001
        fake(ct.GATEWAY_GEEK, hub_card("hub2"), "hub2"), "hub2")
    check("显式选了宽卡：也要重解析（清了能力缓存时要切回标准）",
          calls == ["hub2"], str(calls))

    calls.clear()
    light = CardSpec(category="light", profile=PROFILES["light"],
                     did="l1", model="yeelink.light.1")
    wall.WallView._on_geek_ability(fake(None, light, "l1"), "l1")  # noqa: SLF001
    check("跟极客版无关的设备：什么都不做", calls == [], str(calls))

    calls.clear()
    wall.WallView._on_geek_ability(  # noqa: SLF001
        fake(ct.STANDARD, hub_card("hub2"), "hub2"), "hub2")
    check("显式选了标准：用户的选择优先，不擅自改回小卡", calls == [], str(calls))


# ---------------------------------------------------------------------------
# 3. 卡片与按钮
# ---------------------------------------------------------------------------

def test_card(app: QApplication) -> None:
    pal = theme.DARK
    style = ct.style_by_id(ct.GATEWAY_GEEK)
    card = DeviceCard(hub_state(), Icons(), pal, style)
    btn = card.button
    assert btn is not None
    check("2×1 卡尺寸", (card.width(), card.height()) == (166 * 2 + 8, 168),
          f"{card.width()}×{card.height()}")
    check("按钮在右半、且真的建出来了", btn is not None and btn.width() > 0,
          str(btn and btn.geometry()))
    check("按钮在卡片右半边", btn is not None and btn.geometry().left() > card.width() // 2,
          str(btn and btn.geometry()))
    art = card._art_rect()  # noqa: SLF001
    check("产品图落点在左半", art.left() == 14 and art.right() < card.width() // 2, str(art))
    check("左图右按钮不重叠、中间留缝",
          btn is not None and art.right() < btn.geometry().left(),
          f"art.right={art.right()} btn.left={btn.geometry().left()}")
    check("两边高度一致（同一条内容带）",
          btn is not None and art.height() == btn.height() == btn.geometry().height(),
          f"{art.height()} / {btn and btn.height()}")
    check("外壳会画产品图（不然左半是空的）", card._draws_art())  # noqa: SLF001

    got: list[str] = []
    card.geek_requested.connect(got.append)
    QTest.mouseClick(btn, Qt.LeftButton)
    check("点按钮发出 geek_requested（带 did）", got == ["hub1"], str(got))

    got.clear()
    card.set_busy(True)
    QTest.mouseClick(btn, Qt.LeftButton)
    check("取码中再点不发（连点白烧码）", got == [], str(got))
    check("取码中光标变忙", btn.is_busy())
    card.set_busy(False)
    got.clear()
    QTest.mouseClick(btn, Qt.LeftButton)
    check("恢复后又能点", got == ["hub1"], str(got))

    off = DeviceCard(hub_state(online=False), Icons(), pal, style)
    off_got: list[str] = []
    off.geek_requested.connect(off_got.append)
    assert off.button is not None
    QTest.mouseClick(off.button, Qt.LeftButton)
    check("离线时点不动（云端转发不过去）", off_got == [], str(off_got))

    # 切回标准样式：按钮该消失，卡片退回 1×1 产品图
    plain = ct.style_by_id(ct.STANDARD)
    card.set_style(plain)
    check("切回标准后按钮被销毁", card.button is None)
    check("标准样式仍是 1×1", (card.width(), card.height()) == (166, 168),
          f"{card.width()}×{card.height()}")
    check("标准样式退回「没 body、画产品图」的老路径", card._draws_art())  # noqa: SLF001
    # 落了产品图还不够，落点必须是**整条内容带**：1×1 的 body 只有 138 宽，
    # 被「窄卡不分栏」那条判据误伤的话会返回空矩形，卡片上什么都画不出来，
    # 而 ``_draws_art()`` 仍是 True——只查前者查不出这个 bug。
    plain_art = card._art_rect()  # noqa: SLF001
    check("标准样式的产品图占满整条内容带（不被极客窄卡判据清空）",
          plain_art == card._body_rect() and not plain_art.isEmpty(),  # noqa: SLF001
          f"{plain_art} vs {card._body_rect()}")  # noqa: SLF001
    card.set_style(style)
    check("再切回来按钮又有了", card.button is not None)

    card.set_palette(theme.LIGHT)
    check("换主题不炸", card.button is not None and card.button.width() > 0)

    shot = card.grab()
    check("整卡能画出来（按钮 + 产品图 + 外壳）", not shot.isNull() and shot.width() == card.width(),
          f"{shot.width()}×{shot.height()}")

    # 按钮颜色必须跟主题走。第一版照抄参考图的红棕 #B5432C，整面墙里只有这一块红，
    # 像个别人的控件——参考图只借几何，颜色一律回主题。
    for pal in (theme.DARK, theme.LIGHT):
        c = DeviceCard(hub_state(), Icons(), pal, style)
        assert c.button is not None
        img = c.button.grab().toImage()
        edge = QColor(img.pixel(img.width() // 2, 1)).name().lower()
        check(f"{pal.name}：描边就是主题 accent", edge == pal.accent.lower(),
              f"{edge} vs {pal.accent}")
        inner = QColor(img.pixel(img.width() // 2, img.height() - 6)).name().lower()
        check(f"{pal.name}：没悬停时按钮底还是卡片色", inner == pal.card.lower(),
              f"{inner} vs {pal.card}")
        c.button._hover = True  # noqa: SLF001
        img = c.button.grab().toImage()
        hover = QColor(img.pixel(img.width() // 2, img.height() - 6)).name().lower()
        check(f"{pal.name}：悬停底色往 accent 掺",
              hover != pal.card.lower()
              and hover == theme.blend(pal.card, pal.accent, BTN_HOVER).lower(),
              hover)

    # 按钮上的三种结果态：常态 / 已复制（绿边 + 码）/ 失败（红边 + 重试）
    print("\n按钮结果态")
    result = DeviceCard(hub_state(), Icons(), theme.LIGHT, style)
    rbtn = result.button
    assert rbtn is not None
    result.geek_requested.connect(got.append)
    check("常态文案是动作 + 副作用",
          rbtn.lines() == ["打开极客版", "自动复制登录码"], str(rbtn.lines()))
    check("常态边框是 accent",
          QColor(rbtn.grab().toImage().pixel(rbtn.width() // 2, 1)).name().lower()
          == theme.LIGHT.accent.lower())

    rbtn.set_result("123456")
    check("成功后状态记下来了", rbtn.state() == "done", rbtn.state())
    check("成功文案含那串登录码", "123456" in rbtn.lines(), str(rbtn.lines()))
    check("成功文案给出下一步（重新获取）",
          any("重新获取" in line for line in rbtn.lines()), str(rbtn.lines()))
    # 150 宽的按钮里最怕的失败形态：码被省略成「已复制 登录码：12…」，
    # 正好把最要紧的数字切掉。所以逐行量一次字宽，确保一行都不用省略。
    avail = rbtn.width() - 16
    wide = []
    for line, size, _, _ in rbtn._lines():  # noqa: SLF001
        font = QFont(rbtn.font())
        font.setPixelSize(size)
        fm = QFontMetrics(font)
        if fm.horizontalAdvance(line) > avail:
            wide.append(f"{line}({fm.horizontalAdvance(line)}>{avail})")
    check("成功态每一行都放得下（码不会被省略号吃掉）", not wide, "; ".join(wide))
    check("成功边框转薄荷绿（配色语义是「通了」不是「出错了」）",
          QColor(rbtn.grab().toImage().pixel(rbtn.width() // 2, 1)).name().lower()
          == theme.ACCENT_GREEN.lower(),
          QColor(rbtn.grab().toImage().pixel(rbtn.width() // 2, 1)).name())

    got.clear()
    QTest.mouseClick(rbtn, Qt.LeftButton)
    check("成功态仍可再点一次（重新取码）", got == ["hub1"], str(got))

    rbtn.set_error()
    check("失败状态记下来了", rbtn.state() == "failed", rbtn.state())
    check("失败文案第一行说清楚失败了",
          rbtn.lines()[0] == "获取登录码失败", str(rbtn.lines()))
    check("失败文案第二行仍是重试入口",
          rbtn.lines()[-1] == "重新获取并打开极客版", str(rbtn.lines()))
    check("失败边框转警告红",
          QColor(rbtn.grab().toImage().pixel(rbtn.width() // 2, 1)).name().lower()
          == theme.DANGER.lower(),
          QColor(rbtn.grab().toImage().pixel(rbtn.width() // 2, 1)).name())

    rbtn.set_busy(True)
    check("取码中盖过结果态（先说正在做什么）",
          rbtn.lines() == ["正在取码…"], str(rbtn.lines()))
    check("新一轮取码把旧的结果计时停掉（不然它会在新结果上到点）",
          not rbtn._revert.isActive())  # noqa: SLF001
    rbtn.set_busy(False)
    check("忙态收掉后回到上一次的结论", rbtn.state() == "failed", rbtn.state())

    # 结果态是「刚刚发生了什么」，不是常驻装饰：30 秒后退回常态。
    # 这里直接把 timeout 打出来，不等真实 30 秒。
    rbtn.set_result("654321")
    check(f"结果态起了一支 {RESULT_MS // 1000}s 的单次计时",
          rbtn._revert.isActive()  # noqa: SLF001
          and rbtn._revert.isSingleShot()  # noqa: SLF001
          and rbtn._revert.interval() == RESULT_MS,  # noqa: SLF001
          str(rbtn._revert.interval()))  # noqa: SLF001
    rbtn.set_result("111111")
    check("再来一次结果重新起表（第二个码也看满 30 秒）",
          rbtn._revert.isActive())  # noqa: SLF001
    rbtn._revert.timeout.emit()  # noqa: SLF001
    check("到点退回常态", rbtn.state() == "idle", rbtn.state())
    check("退回常态后文案也回去了",
          rbtn.lines() == ["打开极客版", "自动复制登录码"], str(rbtn.lines()))
    check("退回常态后不再举着那串码", "111111" not in rbtn.lines(), str(rbtn.lines()))
    check("退回常态后边框也回到 accent",
          QColor(rbtn.grab().toImage().pixel(rbtn.width() // 2, 1)).name().lower()
          == theme.LIGHT.accent.lower(),
          QColor(rbtn.grab().toImage().pixel(rbtn.width() // 2, 1)).name())
    got.clear()
    QTest.mouseClick(rbtn, Qt.LeftButton)
    check("退回常态后照样能点（不是死按钮）", got == ["hub1"], str(got))


# ---------------------------------------------------------------------------
# 3b. 1×1 小卡：按钮铺满、不画产品图、文案不省略
# ---------------------------------------------------------------------------

def _text_w(text: str, size: int, font: QFont) -> int:
    f = QFont(font)
    f.setPixelSize(size)
    return QFontMetrics(f).horizontalAdvance(text)


def test_small_card(app: QApplication) -> None:
    """1×1 版不是另写一张卡，是同一套 ``DeviceCard`` 的窄落点。

    这里钉三件事：几何（按钮铺满 body）、产品图（必须不画）、文案（138 宽里
    每一行都放得下——``GeekButton`` 用的是 ``elidedText``，超了不报错，只是悄悄
    把最要紧的动作名吃掉一个字）。
    """
    reset()
    geek.remember("hub1", HUB, True)
    small = ct.style_by_id(ct.GATEWAY_GEEK_SMALL)
    check("小卡注册成了 1×1", small is not None and (small.span, small.rows) == (1, 1),
          str(small and (small.span, small.rows)))
    check("小卡与宽卡共用 body（同一个按钮，不分叉）",
          small is not None and small.body == ct.BODY_GEEK, str(small and small.body))
    check("小卡同样靠能力出现，不写死型号",
          small is not None and small.ability == ct.ABILITY_GEEK,
          str(small and small.ability))

    card = DeviceCard(hub_state(), Icons(), theme.DARK, small)
    btn = card.button
    assert btn is not None
    check("1×1 卡片尺寸", (card.width(), card.height()) == (166, 168),
          f"{card.width()}×{card.height()}")
    body = card._body_rect()  # noqa: SLF001
    check("按钮铺满整条内容带", btn.geometry() == body, f"{btn.geometry()} vs {body}")
    check("1×1 不再画产品图（整条让给按钮）", not card._draws_art())  # noqa: SLF001
    check("窄卡自动换短文案", btn.is_compact())

    # 三种状态逐行量宽：超出可用宽度就会被 elidedText 悄悄省略
    avail = btn.width() - 16
    overflow: list[str] = []
    for label, setup in (
        ("常态", lambda: None),
        ("已复制", lambda: btn.set_result("123456")),
        ("失败", btn.set_error),
    ):
        setup()
        for text, size, _color, _weight in btn._lines():  # noqa: SLF001
            used = _text_w(text, size, btn.font())
            if used > avail:
                overflow.append(f"{label}/{text}: {used}>{avail}")
    check("三种状态的每一行都放得下（不会被省略）", not overflow, "; ".join(overflow))
    check("窄卡的动作名是短的「重新获取」", btn.lines()[-1] == "重新获取", str(btn.lines()))

    got: list[str] = []
    card.geek_requested.connect(got.append)
    QTest.mouseClick(btn, Qt.LeftButton)
    check("小卡点一下照样发 geek_requested", got == ["hub1"], str(got))

    # 切到宽卡：几何、文案、产品图三样都要跟着回来
    card.set_style(ct.style_by_id(ct.GATEWAY_GEEK))
    btn = card.button
    assert btn is not None
    check("切到 2×1 后按钮回到右半", btn.geometry().left() > card.width() // 2,
          str(btn.geometry()))
    check("切到 2×1 后不用短文案", not btn.is_compact())
    check("切到 2×1 后产品图回来", card._draws_art())  # noqa: SLF001
    check("宽卡文案仍是完整那句", "重新获取并打开极客版" in btn.lines(), str(btn.lines()))

    # 同一张卡切回标准样式：产品图必须回到整条内容带。这是 2026-11-16 那次
    # 事故的现场——网关默认变 1×1 小卡后，"窄就不分栏"那条判据把**所有标准
    # 1×1 卡**也当成窄极客卡，产品图落点返回空矩形，整面墙的产品图一起消失
    # （卡片类不随样式重建，非摄像头一律是这个类）。``_draws_art()`` 当时仍是
    # True，所以只查"画不画"查不出来，必须查落点。
    card.set_style(ct.style_by_id(ct.STANDARD))
    back = card._art_rect()  # noqa: SLF001
    check("小卡切回标准：产品图落点回到整条内容带",
          back == body and not back.isEmpty(), f"{back} vs {body}")
    check("小卡切回标准：按钮随之销毁", card.button is None)


# ---------------------------------------------------------------------------
# 4. 接线：取到码之后做什么
# ---------------------------------------------------------------------------

def test_done(app: QApplication) -> None:
    pal = theme.DARK
    style = ct.style_by_id(ct.GATEWAY_GEEK)
    card = DeviceCard(hub_state(), Icons(), pal, style)
    fake_self = SimpleNamespace(_cards={"hub1": card}, _p=pal)

    opened: list[str] = []

    class FakeDesk:
        @staticmethod
        def openUrl(url):  # noqa: N802
            opened.append(url.toString())
            return True

    # 提示走哪条路、带什么语义，是这次改动的重点：必须是**居中置顶**那条
    # （浏览器抢焦点后还看得见），并且成功/失败各一种颜色。
    toasts: list[tuple[str, str]] = []
    real_top = wall.tooltip.toast_top
    wall.tooltip.toast_top = (  # type: ignore[assignment]
        lambda anchor, text, palette=None, *, kind="ok", ms=0:
            toasts.append((kind, text))
    )

    real_desk = wall.QDesktopServices
    wall.QDesktopServices = FakeDesk
    try:
        QGuiApplication.clipboard().setText("")
        card.set_busy(True)
        wall.WallView._on_geek_done(  # noqa: SLF001
            fake_self, "hub1", geek.Entry(url="http://192.0.2.10", code="123456"), "", ""
        )
        check("成功：登录码进了剪贴板", QGuiApplication.clipboard().text() == "123456",
              QGuiApplication.clipboard().text())
        check("成功：打开了网关地址", opened == ["http://192.0.2.10"], str(opened))
        check("成功：按钮的忙态收掉了", not card.button.is_busy())
        check("成功：按钮上留下「已复制」+ 那串码",
              card.button.state() == "done" and "123456" in card.button.lines(),
              f"{card.button.state()} {card.button.lines()}")
        check("成功：提示走居中置顶那条、语义是 ok",
              toasts and toasts[-1][0] == tooltip.TOAST_OK, str(toasts))
        check("成功：提示里带着登录码", "123456" in toasts[-1][1], str(toasts[-1]))

        toasts.clear()
        opened.clear()
        wall.WallView._on_geek_done(  # noqa: SLF001
            fake_self, "hub1", None, "取登录码失败：token 过期", "http://192.0.2.10"
        )
        check("失败但有地址：网页照开，码让用户手输", opened == ["http://192.0.2.10"], str(opened))
        check("失败时不动剪贴板里的东西", QGuiApplication.clipboard().text() == "123456",
              QGuiApplication.clipboard().text())
        check("失败：按钮转成「获取登录码失败」",
              card.button.state() == "failed"
              and card.button.lines()[0] == "获取登录码失败",
              f"{card.button.state()} {card.button.lines()}")
        check("失败：提示语义是 fail",
              toasts and toasts[-1][0] == tooltip.TOAST_FAIL, str(toasts))

        toasts.clear()
        opened.clear()
        wall.WallView._on_geek_done(fake_self, "hub1", None, "取网关地址失败：网络断了", "")  # noqa: SLF001
        check("失败且没地址：不打开任何东西", opened == [], str(opened))
        check("失败且没地址：仍有 fail 提示", toasts and toasts[-1][0] == tooltip.TOAST_FAIL,
              str(toasts))
    finally:
        wall.QDesktopServices = real_desk
        wall.tooltip.toast_top = real_top  # type: ignore[assignment]


def test_click_guard(app: QApplication) -> None:
    """连点保护到底靠什么：**发请求之前**就置忙，不是等回调回来。

    这条是"要不要加 3 秒冷却"的答案：只要 ``set_busy(True)`` 在 ``fetch`` 之前
    同步执行，双击的第二击必然落在忙态里、被 ``_can_click()`` 挡掉，多一个计时器
    只会在用户**有意重试**时静默拒绝一次点击。
    """
    card = DeviceCard(hub_state(), Icons(), theme.DARK, ct.style_by_id(ct.GATEWAY_GEEK))
    btn = card.button
    assert btn is not None

    sent: list[str] = []
    fake_self = SimpleNamespace(
        _cards={"hub1": card},
        _store=SimpleNamespace(fetch_geek_passcode=lambda did, cb: sent.append(did)),
    )
    # 真界面里这条连接在 ``WallView.build`` 里；这里手动接同一条，才能测到点击路径
    card.geek_requested.connect(
        lambda did: wall.WallView._on_geek_requested(fake_self, did)  # noqa: SLF001
    )
    wall.WallView._on_geek_requested(fake_self, "hub1")  # noqa: SLF001
    check("点下去先置忙、再发请求（不是等回调才忙）", btn.is_busy() and sent == ["hub1"],
          f"busy={btn.is_busy()} sent={sent}")

    QTest.mouseClick(btn, Qt.LeftButton)
    check("忙态里的第二击不会再发一个请求（不会白烧第二枚码）", sent == ["hub1"], str(sent))

    card.set_busy(False)
    QTest.mouseClick(btn, Qt.LeftButton)
    check("结果落定后立刻可再点（有意重试不该被冷却期拦）", sent == ["hub1", "hub1"],
          str(sent))


def main() -> int:
    app = QApplication.instance() or QApplication([])
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(13)
    app.setFont(font)

    test_core()
    test_ability()
    test_candidates()
    test_styles()
    test_ability_repair()
    test_card(app)
    test_small_card(app)
    test_store_wiring()
    test_done(app)
    test_click_guard(app)

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败：")
        for item in FAILS:
            print("  -", item)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
