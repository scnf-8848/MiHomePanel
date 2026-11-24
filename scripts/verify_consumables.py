# SPDX-License-Identifier: GPL-3.0-or-later
"""验证耗材页：模型 / 落盘 / 轮询 / 页面 / 像素 / 侧栏圆点。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_consumables.py``

分十组，钉住那些"改一眼看不出来、以后会悄悄退化"的约束：

1. **模型**——阈值含等于、两档填反自动换位、只开一档、读不到数值时不参与变色；
2. **落盘**——同属性去重、改标签不换位置、拖拽移动、排序方式落盘、坏条目丢弃；
3. **轮询**——耗材属性即使**没挂在任何卡片上**也进 ``poll_refs``（否则这页永远
   显示"—"，且没人会发现是轮询没带上它）；
4. **页面**——空态、卡片数、两种排序的先后、按房间分组的分组头、两档计数；
5. **像素 / 拖动**——横条的填充宽度随比例、颜色随档位；整卡染色从左往右；
   圆环的圆心放百分比、弧长按比例且读不到值时不画假进度；拖动只在自定义排序
   开放（按余量 / 按房间真发鼠标事件也不许动）；
6. **侧栏**——圆点在有计数时才画，灰 / 红各一颗；
7. **设备树**——家庭 → 房间 → 设备的层级、过滤、只选设备；
8. **空卡与按家庭**——空卡不计数不进轮询、当前家庭过滤；
9. **属性可改与重名**——挑错能改、同名属性靠服务注释分开；
10. **离线读数时间**——只在离线设备上标、一天以上换警示色、没时间戳不写。

中文在 offscreen 平台渲染成豆腐块，所以这里量几何与像素，不读文字。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QPushButton  # noqa: E402

from mihome.core import consumables as model  # noqa: E402
from mihome.core.api import HomeNode, PropRef, RoomNode, Topology  # noqa: E402
from mihome.core.consumables import Consumable  # noqa: E402
from mihome.core.prefs import PREFS_VERSION, Prefs  # noqa: E402
from mihome.core.spec import PropertySpec, ValueOption  # noqa: E402
from mihome.core.store import (  # noqa: E402
    DeviceState,
    Store,
    _register_keys,
    build_card_empty,
)
from mihome.core.tree import DeviceNode  # noqa: E402
from mihome.ui import theme  # noqa: E402
from mihome.ui.consumables_page import (  # noqa: E402
    BAR_BOTTOM,
    BAR_H,
    CARD_H,
    CARD_W,
    PAD,
    RING_CARD,
    RING_CENTER_LIFT,
    RING_CENTER_SIZE,
    RING_D,
    RING_GAP,
    RING_LABEL_DY,
    RING_LABEL_H,
    RING_LABEL_SIZE,
    RING_W,
    RING_X,
    RING_Y,
    ConsumablesPage,
    Row,
    _ring_chord,
    card_size,
)
from mihome.ui.side_nav import SideNav  # noqa: E402

FAILS: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {got!r}")
    if not ok:
        FAILS.append(f"{name} → 期望 {want!r}，实际 {got!r}")


def check_true(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(f"{name}{('  ' + detail) if detail else ''}")


def color_count(image, color: str, x0: int, y0: int, x1: int, y1: int) -> int:
    """矩形里精确等于某个颜色的像素数（圆环/横条的笔画都是实色）。"""
    want = color.upper()
    return sum(
        1
        for y in range(y0, y1)
        for x in range(x0, x1)
        if image.pixelColor(x, y).name().upper() == want
    )


def near(a: int, b: int, tol: int = 6) -> bool:
    return abs(a - b) <= tol


# -- 夹具 ----------------------------------------------------------------
def prop(siid: int, piid: int, label: str, *, unit: str = "%",
         span: tuple[float, float, float] | None = (0, 100, 1),
         svc: str = "服务") -> PropertySpec:
    return PropertySpec(
        siid=siid, piid=piid, name=f"p{siid}_{piid}", urn=f"urn:test:{siid}:{piid}",
        description=label, comment=label, format="uint8", access=("read", "notify"),
        unit=unit, value_range=span, value_list=(), service="svc", service_label=svc,
        label_short=label,
    )


FILTER = prop(2, 9, "滤芯剩余")
WATER = prop(3, 1, "洗衣液", span=(0, 500, 1))     # 非百分数：靠值域折算
BATTERY = prop(4, 1, "电量")

#: 洗衣液的真实形态：枚举（1=低 / 2=标准），没有百分比。云端台账对它是
#: ``inadeq = {"val":"1","type":"list"}``，语义是"读到 1 就该换"。
DETERGENT = PropertySpec(
    siid=6, piid=1, name="detergent-left-level", urn="urn:test:6:1",
    description="清洁剂剩余量", comment="清洁剂剩余量", format="uint8",
    access=("read", "notify"), unit=None, value_range=None,
    value_list=(ValueOption(1, "Low", "Low", "低"),
                ValueOption(2, "Normal", "Normal", "标准")),
    service="detergent-management", service_label="洗涤剂", label_short="洗衣液",
)


class _Caps:
    def save(self) -> None:
        pass


class _Client:
    capabilities = _Caps()

    def consumable_items(self) -> dict:
        """云端耗材台账。默认空 = 每台设备都"云端没有记录"，候选块不出现按钮。"""
        return {}


class _Worker:
    """同步假 worker：提交的活立刻跑完、回调立刻调（测试不等线程）。

    弹窗里的"云端耗材"是异步取数的，同步执行才能在同一段测试里断言结果。
    """

    def submit(self, fn, args=(), *, kwargs=None, callback=None, priority=0, label=""):
        try:
            result = fn(*args, **(kwargs or {}))
        except BaseException as exc:  # noqa: BLE001
            if callback is not None:
                callback(None, exc)
            return
        if callback is not None:
            callback(result, None)


def make_store(tmp: Path) -> Store:
    """两台设备：净化器（滤芯 15% → 轻提醒）与洗衣机（洗衣液 20/500 → 4% → 重提醒）。"""
    store = Store(_Client(), _Worker())  # type: ignore[arg-type]
    store.prefs = Prefs(tmp / "prefs.json")
    homes = [HomeNode("h1", "我的家", [RoomNode("r1", "客厅"), RoomNode("r2", "阳台")])]
    devices = [
        DeviceNode("d1", "净化器", "m", "air-purifier.x", home="我的家", room="客厅",
                   home_id="h1", room_id="r1"),
        DeviceNode("d2", "洗衣机", "m", "washer.x", home="我的家", room="阳台",
                   home_id="h1", room_id="r2"),
    ]
    store.topology = Topology(homes, devices)
    specs = {
        "d1": [FILTER, BATTERY],
        "d2": [WATER, DETERGENT],
    }
    values = {"d1": {(2, 9): 15, (4, 1): 88}, "d2": {(3, 1): 20, (6, 1): 1}}
    for node in devices:
        props = specs[node.did]
        store.devices[node.did] = DeviceState(
            node=node,
            spec=SimpleNamespace(properties=props),
            card=build_card_empty(node),
            values=dict(values[node.did]),
            keys={p.key: PropRef(node.did, p.siid, p.piid) for p in props},
        )
    return store


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    tmp = Path(tempfile.mkdtemp())

    print("\n1) 模型：阈值 / 换算 / 坏数据")
    check("等于阈值就算提醒（写 20 就是 20% 时提醒）",
          [model.level_of(v, 20, 10) for v in (100, 21, 20, 11, 10, 0)],
          [0, 0, 1, 1, 2, 2])
    check("只开轻提醒时不出现红色", model.level_of(5, 20, None), model.LEVEL_WARN)
    check("两档都不开 = 只显示读数", model.level_of(0, None, None), model.LEVEL_NONE)
    check("读不到值不判档", model.level_of(None, 20, 10), model.LEVEL_NONE)
    swapped = model.parse({"did": "d", "siid": 1, "piid": 2, "warn": 10, "crit": 20})
    check("两档填反按「橙 20 / 红 10」理解", (swapped.warn, swapped.crit), (20, 10))
    check("值域不同时按值域折算", round(model.fill_percent(20, (0, 500)) or -1, 1), 4.0)
    check("没有值域时把读数当百分数", model.fill_percent(45, None), 45.0)
    check("布尔 / 文本 / 空值都不参与", [model.fill_percent(True, None),
                                        model.fill_percent("abc", None),
                                        model.fill_percent(None, None)],
          [None, None, None])
    check("坏条目（空 did / 非法 siid）丢弃",
          [model.parse({"did": "", "siid": 1, "piid": 2}),
           model.parse({"did": "d", "siid": 0, "piid": 2})],
          [None, None])
    check("阈值超范围被夹住", model.parse({"did": "d", "siid": 1, "piid": 2,
                                           "warn": 999}).warn, 100)

    print("\n2) 落盘：去重 / 改值不换位 / 移动 / 排序")
    store = make_store(tmp)
    prefs = store.prefs
    a = Consumable(did="d1", siid=2, piid=9, label="滤芯", warn=30, crit=15)
    b = Consumable(did="d2", siid=3, piid=1, label="洗衣液")
    check("新增两条", (prefs.add_consumable(a), prefs.add_consumable(b)), (True, True))
    check("同一属性不允许第二条", prefs.add_consumable(a), False)
    check("顺序就是添加顺序", [c.label for c in prefs.consumables], ["滤芯", "洗衣液"])
    prefs.update_consumable(b.with_changes(label="洗衣液（桶装）"))
    check("改标签不换位置", [c.label for c in prefs.consumables], ["滤芯", "洗衣液（桶装）"])
    check("拖到最前", (prefs.move_consumable(b.id, 0),
                     prefs.consumables[0].label), (True, "洗衣液（桶装）"))
    check("越界收敛到末尾", (prefs.move_consumable(b.id, 99),
                          prefs.consumables[-1].label), (True, "洗衣液（桶装）"))
    check("排序方式落盘", (prefs.set_consumable_sort("custom"), prefs.consumable_sort),
          (True, "custom"))
    check("卡片样式整页一个：落盘", (prefs.set_consumable_style("fill"),
                                    prefs.consumable_style), (True, "fill"))
    again = Prefs(tmp / "prefs.json")
    check("重读后条目都在（每条不再带样式）",
          [(c.label, c.warn) for c in again.consumables],
          [("滤芯", 30), ("洗衣液（桶装）", 20)])
    check_true("监控项落盘里没有 style 键（样式是整页的）",
               all("style" not in c.to_dict() for c in again.consumables))
    check("重读后排序方式还在", again.consumable_sort, "custom")
    check("重读后卡片样式还在", again.consumable_style, "fill")
    check("三种样式登记齐全", model.STYLES, ("bar", "fill", "ring"))
    check("圆环样式也能落盘", (prefs.set_consumable_style("ring"),
                          Prefs(tmp / "prefs.json").consumable_style), (True, "ring"))
    check("偏好版本已升到 20（新增字段要有版本号）", PREFS_VERSION, 20)
    check("删除一条", (again.remove_consumable(a.id), len(again.consumables)), (True, 1))

    print("\n3) 轮询：耗材属性即使没上卡片也要读到")
    store.prefs = Prefs(tmp / "prefs2.json")
    store.prefs.add_consumable(Consumable(did="d1", siid=2, piid=9))
    refs = {(r.did, r.siid, r.piid) for r in store.poll_refs()}
    check_true("滤芯属性进了轮询", ("d1", 2, 9) in refs, str(sorted(refs)))
    check("这条属性不在任何卡片上（正面证据）",
          len(build_card_empty(store.devices["d1"].node).controls) == 0, True)
    before = len(refs)
    store.prefs.remove_consumable("d1:2.9")
    after = {(r.did, r.siid, r.piid) for r in store.poll_refs()}
    check("删掉监控项后不再为它轮询", (before, len(after)), (1, 0))

    # 2026-11-24 修的坑：耗材项指向"卡片没画"的属性时，以前 keys 里没有它，
    # poll_refs 用 keys 反查寻址 → 请求从来没发出去，界面永远「暂时读不到这个属性」。
    # 鹿客 V6 Max 的大电池（10.1，实测读得到 61%）就是这么被漏掉的。
    bare = DeviceState(
        node=store.devices["d2"].node,
        spec=SimpleNamespace(properties=[WATER, DETERGENT]),
        card=build_card_empty(store.devices["d2"].node),
        keys={},
    )
    _register_keys(bare, "d2", [
        Consumable(did="d2", siid=6, piid=1, low=1),   # 枚举属性，卡片没画
        Consumable(did="d2", siid=9, piid=9),          # 规格里没有：不发请求
        Consumable(did="d1", siid=3, piid=1),          # 别台设备的
    ])
    check("卡片没画的耗材属性也登记进寻址（不然永远不发请求）",
          sorted(bare.keys), [(6, 1)])

    print("\n4) 页面：空态 / 卡片 / 排序 / 分组 / 计数")
    store.prefs = Prefs(tmp / "prefs3.json")
    page = ConsumablesPage(store, theme.LIGHT)
    page.resize(760, 520)
    page.show()
    app.processEvents()
    # 标题位置要和「监控」「电脑自动化」两页严丝合缝：右上角两个下拉比 20px 标题的
    # 行高（26）高 7px，直接把 QLabel 放进 head 会被撑到 33 再垂直居中，标题比另两页
    # 低 3.5px（用户 2026-11-18 报）。现在标题包了一层容器 + AlignTop，贴住 8px。
    title = next(w for w in page.findChildren(QLabel) if w.objectName() == "PageTitle")
    top = title.mapTo(page, title.rect().topLeft())
    check("标题贴在 (28,8)、高度是自身行高，没被右侧下拉撑高",
          (top.x(), top.y(), title.height()), (28, 8, title.sizeHint().height()))
    check("统计文字与标题共中线",
          abs(page.lbl_hint.mapTo(page, page.lbl_hint.rect().center()).y()
              - title.mapTo(page, title.rect().center()).y()) <= 1, True)

    check("一条都没有时是空态", len(page._cards), 0)
    check_true("空态提示可见", page._empty.isVisible())
    check("空态下圆点是 (0, 0)", page.counts(), (0, 0))

    store.prefs.add_consumable(Consumable(did="d1", siid=2, piid=9, warn=20, crit=10))
    store.prefs.add_consumable(Consumable(did="d2", siid=3, piid=1, warn=10, crit=5))
    page.refresh()
    app.processEvents()
    check("两张卡", len(page._cards), 2)
    check("15% → 轻提醒；4% → 重提醒", page.counts(), (1, 1))
    # 名字留空时用属性的中文名（"滤芯剩余"而不是落盘的短名）
    check("按余量排序：4% 的洗衣液在前", [r.name for r in page._rows],
          ["洗衣液", "滤芯剩余"])

    store.prefs.set_consumable_sort("room")
    page.refresh()
    app.processEvents()
    check("按房间分组：客厅（净化器）在阳台（洗衣机）之前",
          [r.place_text for r in page._rows], ["我的家 · 客厅", "我的家 · 阳台"])
    headers = [lab.text() for lab in page.findChildren(type(page.lbl_hint), "RoomHeader")]
    check("分组头是「家庭 · 房间」（跨家庭靠这个区分，不是只写房间名）",
          headers, ["我的家 · 客厅", "我的家 · 阳台"])

    store.prefs.set_consumable_sort("level")
    page.refresh()
    values = store.devices["d1"].values
    values[(2, 9)] = 1          # 滤芯从 15% 掉到 1%（低于洗衣液的 4%）→ 变红并排到最前
    page.update_cards({"d1"})
    app.processEvents()
    check("值变了：两档计数跟着变", page.counts(), (0, 2))
    check("值变了：按余量重新排到最前", [r.name for r in page._rows][0], "滤芯剩余")

    # 只刷新一台设备的读数 → 另一张卡的 Row 对象与 self._rows 里的不再是同一个；
    # 这时切样式仍要盖到全部卡片（_on_style 按 id 重发，不能只改 self._rows）
    store.devices["d2"].values[(3, 1)] = 60
    page.update_cards({"d2"})
    page.cb_style.setCurrentIndex(page.cb_style.findData(model.STYLE_FILL))
    app.processEvents()
    check("部分卡换过 Row 对象后，切样式仍覆盖全部卡片",
          {c.row.style for c in page._cards.values()}, {model.STYLE_FILL})
    page.cb_style.setCurrentIndex(page.cb_style.findData(model.STYLE_BAR))
    app.processEvents()
    page.cb_style.setCurrentIndex(page.cb_style.findData(model.STYLE_RING))
    app.processEvents()
    check("下拉里能选圆环，整页统一切过去",
          {r.style for r in page._rows}, {model.STYLE_RING})
    check_true("每张卡都跟上圆环样式",
               all(c.row.style == model.STYLE_RING for c in page._cards.values()))
    page.cb_style.setCurrentIndex(page.cb_style.findData(model.STYLE_BAR))
    app.processEvents()

    print("\n4c) 切家庭保住滚动位置；普通刷新仍回顶部")
    page.resize(760, 170)
    app.processEvents()
    bar = page.scroll.verticalScrollBar()
    check_true("页面够矮、滚动条确实有行程", bar.maximum() > 0,
               f"max={bar.maximum()}")
    bar.setValue(min(40, bar.maximum()))
    scrolled = bar.value()
    page.refresh(preserve_scroll=True)
    app.processEvents()
    check("切家庭重建后滚动值还在", bar.value(), scrolled)
    page.refresh()
    app.processEvents()
    check("普通刷新（加 / 删、拓扑）仍回顶部", bar.value(), 0)
    page.resize(760, 520)
    app.processEvents()

    print("\n4b) 设备不在了：配置保留，并且仍然删得掉")
    del store.devices["d2"]
    page.refresh()
    app.processEvents()
    ghost = [r for r in page._rows if r.device == "设备已不存在"]
    check("缺失设备的卡还在（否则这条配置永远删不掉）", len(ghost), 1)
    check("缺失的卡不参与提醒计数", page.counts(), (0, 1))
    store.prefs.remove_consumable(ghost[0].item.id)
    page.refresh()
    app.processEvents()
    check("删掉后只剩那条还在的", len(page._cards), 1)

    print("\n5) 像素：横条宽度与整卡染色")
    card = page._cards[page._rows[0].item.id]
    image = card.grab().toImage()
    bar_y = card.height() - BAR_BOTTOM - BAR_H // 2
    left = image.pixelColor(PAD + 2, bar_y)
    right = image.pixelColor(card.width() - PAD - 4, bar_y)
    check("低值卡片用红", left.name().upper(), theme.DANGER.upper())
    check_true("填充只占 5%：右侧还是轨道色",
               right.name().upper() != theme.DANGER.upper(), right.name())
    mid_ratio_pixel = image.pixelColor(PAD + int((CARD_W - PAD * 2) * 0.10), bar_y)
    check_true("10% 处已经回到轨道色（条随比例缩短）",
               mid_ratio_pixel.name().upper() != theme.DANGER.upper(),
               mid_ratio_pixel.name())

    # 染色面积太小就量不出来，先把这条抬到 60%（左右各取一点才有对比）
    store.devices["d1"].values[(2, 9)] = 60
    page.update_cards({"d1"})
    # 换样式走右上角下拉（整页一个），不是改这一条
    page.cb_style.setCurrentIndex(page.cb_style.findData(model.STYLE_FILL))
    app.processEvents()
    check("右上角下拉切换卡片样式并落盘", store.prefs.consumable_style,
          model.STYLE_FILL)
    check("行数据跟着换样式（卡片不用回查 store）",
          {r.style for r in page._rows}, {model.STYLE_FILL})
    filled = page._cards[store.prefs.consumables[0].id].grab().toImage()
    tinted = filled.pixelColor(8, 60)
    plain = filled.pixelColor(filled.width() - 8, 60)
    check_true("整卡染色：左侧被染、右侧还是卡底色",
               tinted.name().upper() != plain.name().upper(),
               f"{tinted.name()} / {plain.name()}")
    page.cb_style.setCurrentIndex(page.cb_style.findData(model.STYLE_BAR))
    app.processEvents()
    check("切回横条后不再整卡染色（同一份行数据）",
          {r.style for r in page._rows}, {model.STYLE_BAR})

    # 圆环：圆心那行字在 offscreen 下渲染成豆腐块，量弧线的像素而不是读文字。
    page.cb_style.setCurrentIndex(page.cb_style.findData(model.STYLE_RING))
    app.processEvents()
    ring_card = page._cards[store.prefs.consumables[0].id]
    ring_img = ring_card.grab().toImage()
    ring_box = (RING_X, RING_Y, RING_X + RING_D, RING_Y + RING_D)
    ring_pixels = color_count(ring_img, theme.TOGGLE_ON, *ring_box)
    check_true("60% 的圆环画出档位色弧", ring_pixels > 20, f"命中 {ring_pixels} 像素")
    check_true("圆环样式不再画底部横条",
               ring_img.pixelColor(card.width() - PAD - 4, bar_y).name().upper()
               != theme.TOGGLE_ON.upper())

    # 结构图（2026-11-18 用户提供）：方形卡 + 居中大环 + 12 点开口 + 圆心大字。
    check("圆环卡是方形，横条卡仍是横的",
          ((ring_card.width(), ring_card.height()), card_size(model.STYLE_BAR)),
          ((RING_CARD, RING_CARD), (CARD_W, CARD_H)))
    check("方卡缩到 3/4（用户 11-18 嫌 240 太大）", RING_CARD, 180)
    check_true("环占卡片主体（直径超过卡宽一半）", RING_D > RING_CARD // 2)
    check_true("环宽够粗（不是细线）", RING_W >= 12)
    check_true("圆心百分比相对卡宽是大字", RING_CENTER_SIZE >= RING_CARD // 10)
    cx = RING_X + RING_D // 2
    top_band = (RING_Y + 3, RING_Y + RING_W + 5)
    check("12 点方向留开口（正上方没有弧）",
          color_count(ring_img, theme.TOGGLE_ON, cx - 3, top_band[0],
                      cx + 3, top_band[1]), 0)
    check_true("开口右侧紧接着就是弧（不是整段没画）",
               color_count(ring_img, theme.TOGGLE_ON, cx + 16, top_band[0],
                           cx + 26, top_band[1]) > 0)
    check("右上角不再画原始读数（结构上只留圆心百分比）",
          color_count(ring_img, theme.TOGGLE_ON,
                      RING_CARD - PAD - 62, 10, RING_CARD - PAD, 34), 0)

    store.devices["d1"].values[(2, 9)] = 100
    page.update_cards({"d1"})
    app.processEvents()
    full_img = page._cards[store.prefs.consumables[0].id].grab().toImage()
    check("满格也留着 12 点开口（不补成整圆）",
          color_count(full_img, theme.TOGGLE_ON, cx - 3, top_band[0],
                      cx + 3, top_band[1]), 0)
    check_true("满格时环的其余部分都画满", RING_GAP < 15)
    store.devices["d1"].values[(2, 9)] = 0
    page.update_cards({"d1"})
    app.processEvents()
    check("读数为 0 时不画假进度",
          color_count(page._cards[store.prefs.consumables[0].id].grab().toImage(),
                      theme.TOGGLE_ON, *ring_box), 0)
    page.cb_style.setCurrentIndex(page.cb_style.findData(model.STYLE_BAR))
    app.processEvents()

    print("\n5b) 拖动只在「自定义排序」开放：按余量 / 按房间都拖不动")
    from PySide6.QtCore import QPoint, Qt  # noqa: PLC0415
    from PySide6.QtTest import QTest  # noqa: PLC0415

    # 前面的「删掉设备 / 删掉监控项」组可能把表删到只剩一条；拖动断言必须有
    # 两张卡才有意义（只有一张时 target==moving，断言会假阳性）。
    if len(store.prefs.consumables) < 2:
        store.prefs.add_consumable(
            Consumable(did="d2", siid=3, piid=1, warn=10, crit=5))
    for mode in ("level", "room"):
        store.prefs.set_consumable_sort(mode)
        page.refresh()
        app.processEvents()
        check_true(f"{mode} 夹具里至少两张卡（否则断言是假的）", len(page._rows) >= 2)
        before = [c.id for c in store.prefs.consumables]
        moving_id = before[-1]
        card = page._cards[moving_id]
        check(f"{mode} 下卡片不开放拖动", card._draggable, False)
        # 真发一遍鼠标手势，而不是绕过卡片直接调页面回调：8px 阈值过了也不许动。
        # 松手会被当普通点击 → 临时摘掉编辑弹窗，否则离屏下模态框把验证挂住。
        card.clicked.disconnect(page._edit)
        try:
            QTest.mousePress(card, Qt.LeftButton, Qt.NoModifier, QPoint(20, 20))
            QTest.mouseMove(card, QPoint(140, 70))
            QTest.mouseRelease(card, Qt.LeftButton, Qt.NoModifier, QPoint(140, 70))
        finally:
            card.clicked.connect(page._edit)
        app.processEvents()
        check(f"{mode} 下拖过阈值也没启动拖动", card._dragging, False)
        check_true(f"{mode} 下没生成幽灵", page._drag_ghost is None)
        check(f"{mode} 下排序方式没被偷改", store.prefs.consumable_sort, mode)
        check(f"{mode} 下盘上顺序没动", [c.id for c in store.prefs.consumables], before)

    # 安全默认：卡片**不传 draggable 就不可拖**。唯一该打开的是页面 _reflow
    # （按当前排序方式显式传参），将来新增调用点忘了传只会"拖不动"，不会让
    # 按余量 / 按房间的模式又能把顺序拖乱（那种 bug 很难查）。
    from mihome.ui.consumables_page import ConsumableCard  # noqa: PLC0415

    bare = ConsumableCard(page._rows[0], page._p)
    check_true("卡片默认不可拖（安全默认）", not bare._draggable)
    bare.deleteLater()

    print("\n5c) 卡片墙式拖动：幽灵 + 实时落点 + 松手落盘")
    from mihome.ui.dnd import DragGesture  # noqa: PLC0415
    from mihome.ui.wall import DragGhost  # noqa: PLC0415

    store.prefs.set_consumable_sort("custom")
    page.refresh()
    app.processEvents()
    check_true("拖动夹具里仍至少两张卡", len(page._rows) >= 2)
    # 基准取**存储顺序**：拖动落盘改的就是 prefs 的顺序，拿显示顺序当基准会
    # 把拖动中的内存重排误当成落盘结果。
    stored_before = [c.id for c in store.prefs.consumables]
    moving_id, target_id = stored_before[-1], stored_before[0]
    check_true("被拖的那条原本不在首位（否则落盘断言没意义）",
               moving_id != stored_before[0])
    check_true("自定义排序下卡片开放拖动", page._cards[moving_id]._draggable)
    check_true("卡片用的是共享 DragGesture（不是自己写的阈值）",
               isinstance(page._cards[moving_id]._gesture, DragGesture))
    page._on_drag_start(moving_id)
    app.processEvents()
    check_true("开始拖动就创建幽灵", isinstance(page._drag_ghost, DragGhost))
    check_true("被拖的卡变成虚线占位框", page._cards[moving_id]._placeholder)
    check("拖动中先不动盘上的顺序",
          store.prefs.consumables[0].id, stored_before[0])

    ghost = page._drag_ghost
    ghost_pos = ghost.pos()
    target_card = page._cards[target_id]
    # 落点看**幽灵中心**（不是鼠标点）：把幽灵中心对到目标卡中心，反解出鼠标该在哪。
    # 抓取点是随机的，同一处鼠标换个抓法会得出不同落点——那正是用户报的"拖不准"。
    target_local = target_card.geometry().center()
    mouse_local = target_local + page._drag_grab - ghost.rect().center()
    drop_pos = page._canvas.mapToGlobal(mouse_local)
    probe = target_card.mapToGlobal(target_card.rect().center())
    page._on_drag_move(drop_pos)
    app.processEvents()
    check("拖动中内存顺序已实时重排到目标", page._rows[0].item.id, moving_id)
    check("但还没写盘（松手才算）", store.prefs.consumables[0].id, stored_before[0])
    check_true("幽灵跟着鼠标移动了", ghost.pos() != ghost_pos)
    check_true("占位框还在（落点提示没丢）", page._cards[moving_id]._placeholder)
    check("落点索引：探针在目标卡中心 → 排它前面",
          page._drop_index(probe, moving_id), 0)

    page._on_drop(moving_id, drop_pos)
    app.processEvents()
    check("松手才把顺序写盘", store.prefs.consumables[0].id, moving_id)
    check_true("松手后幽灵被收掉", page._drag_ghost is None)
    check_true("新卡片没有残留占位态", not page._cards[moving_id]._placeholder)

    # 拖到一半切程序（Alt+Tab / 截图快捷键）：窗口失活之后 Qt 不再派发鼠标事件，
    # 卡片自身的 mouseRelease 永远不来 → 必须由 DragGuard 兜底把半途状态收干净，
    # 否则幽灵和占位框就留在页面上（用户 2026-11-18 报的第二个 bug）。
    moving_id2 = page._rows[0].item.id
    last_id = page._rows[-1].item.id
    order_before = [r.item.id for r in page._rows]
    stored_before_abort = [c.id for c in store.prefs.consumables]
    page._on_drag_start(moving_id2)
    page._on_drag_move(page._cards[last_id].mapToGlobal(page._cards[last_id].rect().center()))
    app.processEvents()
    check_true("中断前内存顺序确实已经变了", page._rows[-1].item.id == moving_id2)
    page._on_drag_aborted("测试：应用切到后台")
    app.processEvents()
    check_true("中断后幽灵被收掉", page._drag_ghost is None)
    check_true("中断后占位框复位", not page._cards[moving_id2]._placeholder)
    check_true("中断后卡片的按下状态也清了", page._cards[moving_id2]._press is None)
    check("中断把内存顺序还原成拖动前", [r.item.id for r in page._rows], order_before)
    check("中断不写盘", [c.id for c in store.prefs.consumables], stored_before_abort)

    print("\n6) 侧栏：两档提醒的数字圆点")
    nav = SideNav(theme.LIGHT)
    nav.resize(200, 560)
    nav.show()
    app.processEvents()
    base = nav.grab().toImage()
    nav.set_zone_badges("consumables", 2, 1)
    app.processEvents()
    shot = nav.grab().toImage()
    check_true("登记后确实重绘（有像素变化）", base != shot)

    def has_color(img, hex_color: str, tol: int = 12) -> bool:
        want = theme.QColor(hex_color) if hasattr(theme, "QColor") else None
        want = want or __import__("PySide6.QtGui", fromlist=["QColor"]).QColor(hex_color)
        for y in range(img.height()):
            for x in range(img.width()):
                c = img.pixelColor(x, y)
                if (abs(c.red() - want.red()) <= tol
                        and abs(c.green() - want.green()) <= tol
                        and abs(c.blue() - want.blue()) <= tol):
                    return True
        return False

    check_true("重提醒那颗是红的", has_color(shot, theme.DANGER))
    nav.set_zone_badges("consumables", 0, 0)
    app.processEvents()
    check_true("计数清零后圆点消失", not has_color(nav.grab().toImage(), theme.DANGER))

    print("\n7) 设备按「家庭 → 房间 → 设备」树选，第一步只选设备")
    from PySide6.QtCore import QPoint  # noqa: PLC0415

    from mihome.ui.consumable_dialog import ConsumableDialog  # noqa: PLC0415

    store2 = make_store(Path(tempfile.mkdtemp()))
    # 只有布尔属性的设备：两个数字阈值套不上去，不该出现在可选清单里
    bool_node = DeviceNode("d3", "灯泡", "m", "light.x", home="我的家", room="客厅",
                           home_id="h1", room_id="r1")
    bool_prop = PropertySpec(
        siid=5, piid=1, name="on", urn="urn:test:5:1", description="开关", comment="开关",
        format="bool", access=("read", "write", "notify"), unit="", value_range=None,
        value_list=(), service="svc", service_label="服务", label_short="开关",
    )
    store2.topology.devices.append(bool_node)
    store2.devices["d3"] = DeviceState(
        node=bool_node, spec=SimpleNamespace(properties=[bool_prop]),
        card=build_card_empty(bool_node), values={(5, 1): True},
        keys={bool_prop.key: PropRef("d3", 5, 1)},
    )

    # 第二个家庭：耗材页和选择树都该跟着顶栏的家庭走
    store2.topology.homes.append(HomeNode("h2", "老家", [RoomNode("r9", "厨房")]))
    far_node = DeviceNode("d4", "净水器", "m", "water.x", home="老家", room="厨房",
                          home_id="h2", room_id="r9")
    far_prop = prop(2, 1, "滤芯")
    store2.topology.devices.append(far_node)
    store2.devices["d4"] = DeviceState(
        node=far_node, spec=SimpleNamespace(properties=[far_prop]),
        card=build_card_empty(far_node), values={(2, 1): 70},
        keys={far_prop.key: PropRef("d4", 2, 1)},
    )
    store2.active_home = "h1"

    dialog = ConsumableDialog(store2, theme.LIGHT)
    tree = dialog.tree_device
    tops = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    # 2026-11-21 起选设备树跨家庭（和挂件管理窗一致）：顶层是家庭
    check("顶层是家庭，跨全部家庭", [t.text(0) for t in tops], ["我的家", "老家"])
    my_home = tops[0]
    check("只有开关属性的灯泡被滤掉，客厅只剩净化器",
          [my_home.child(i).text(0) for i in range(my_home.childCount())],
          ["客厅", "阳台"])
    check("树列出全部家庭（老家那台也在）",
          sorted(dialog._device_items), ["d1", "d2", "d4"])
    check("第一步不再留「设备」这一列标签",
          [w.text() for w in dialog.findChildren(QLabel) if w.objectName() == "FieldLabel"], [])
    check("默认选中第一台设备", dialog._selected_did, "d1")
    check("第一步只选设备：没有属性下拉、没有阈值输入框",
          (dialog.cb_prop, dialog.ed_warn), (None, None))
    check("没点「下一步」就不算选完", dialog.selected_did, "")
    dialog._on_next()
    check("点「下一步」把设备交出去", dialog.selected_did, "d1")

    living = my_home.child(0)          # 我的家 → 客厅
    living.setExpanded(True)
    dialog._on_device_clicked(living, 0)
    check("点房间行只折角，不把已选设备清掉", dialog._selected_did, "d1")
    check("点房间行确实折起来了", living.isExpanded(), False)
    check_true("这棵树只能选行、不能拖动",
               not tree._can_drag(living.child(0), QPoint(5, 5)))

    # 树不跟顶栏家庭走：换家后还是那两个家庭（页面已经不跟家庭了）
    store2.active_home = "h2"
    other = ConsumableDialog(store2, theme.LIGHT)
    check("换顶栏家庭不影响选设备树",
          ([other.tree_device.topLevelItem(i).text(0)
            for i in range(other.tree_device.topLevelItemCount())],
           sorted(other._device_items)),
          (["我的家", "老家"], ["d1", "d2", "d4"]))
    store2.active_home = "h1"

    print("\n8) 空卡：只记设备，属性等点开再配")
    shell = model.unset("d1")
    check("空壳 id 固定是 did:0.0（所以一台设备只可能有一张）", shell.id, "d1:0.0")
    check("空壳不算配置好", shell.is_configured, False)
    check("空壳不带阈值（还没到那一步）", (shell.warn, shell.crit), (None, None))
    check("第一次添加成功", store2.prefs.add_consumable(shell), True)
    check("同一台设备再加一次被去重，墙上不会摞出第二张空壳",
          store2.prefs.add_consumable(model.unset("d1")), False)
    check("半配置（只填 siid）当坏数据丢掉",
          model.parse({"did": "d1", "siid": 2, "piid": 0}), None)

    form = ConsumableDialog(store2, theme.LIGHT, item=shell)
    check("第二步：设备固定，属性变成可选项", form.cb_prop.count(), 2)
    check("阈值给了能直接用的默认值", (form.ed_warn.text(), form.ed_crit.text()), ("20", "10"))
    built = form._build()
    check("保存时把属性和阈值一起填进去",
          (built.did, built.siid, built.piid, built.warn, built.crit),
          ("d1", 2, 9, 20, 10))
    form._on_save()
    check("空壳被原地替换：位置不动，id 换成属性",
          [c.id for c in store2.prefs.consumables], ["d1:2.9"])

    # 同一台设备的第二条：允许（不同属性），但挑中已被占用属性时不能保存
    check("同设备再加空卡是允许的（要盯第二个耗材）",
          store2.prefs.add_consumable(model.unset("d1")), True)
    again = ConsumableDialog(store2, theme.LIGHT, item=store2.prefs.consumable("d1:0.0"))
    again._on_save()
    check("属性已被另一条占着：不新增、不顶掉，留在窗里",
          (again.added, len(store2.prefs.consumables)), (None, 2))
    check_true("并且说一句为什么没保存", not again.lbl_err.isHidden())
    store2.prefs.remove_consumable("d1:0.0")

    page2 = ConsumablesPage(store2, theme.LIGHT)
    store2.prefs.add_consumable(model.unset("d2"))
    store2.prefs.add_consumable(
        model.parse({"did": "d4", "siid": 2, "piid": 1, "warn": 20, "crit": 10}))
    store2.active_home = "h1"
    page2.refresh()
    app.processEvents()
    check("空卡在墙上、写「未设置」而不是「—」",
          [(r.name, r.value_text) for r in page2._rows if r.is_shell],
          [("洗衣机", "未设置")])
    check("别的家庭的监控项也在这面墙上（跨家庭）",
          "净水器" in [r.device for r in page2._rows], True)
    check("空卡按余量排序垫底", page2._rows[-1].is_shell, True)
    check("空卡不进提醒计数", page2.counts(), (1, 0))
    check("空卡不进轮询（没有可读的东西）",
          ("d2", 0, 0) in {(r.did, r.siid, r.piid) for r in store2.poll_refs()}, False)

    before = [(r.item.id, r.device, r.place_text) for r in page2._rows]
    store2.active_home = "h2"
    page2.refresh()
    app.processEvents()
    check("换顶栏家庭不影响这面墙（同一批监控项、同一个顺序）",
          [(r.item.id, r.device, r.place_text) for r in page2._rows], before)
    check("侧栏圆点是全家庭计数，不随切家庭归零", page2.counts(), (1, 0))
    store2.active_home = "h1"

    empty_store = Store(_Client(), _Worker())  # type: ignore[arg-type]
    empty_store.prefs = Prefs(Path(tempfile.mkdtemp()) / "prefs.json")
    empty_store.topology = Topology(
        [HomeNode("h1", "我的家", [RoomNode("r1", "客厅")])], [bool_node])
    empty_store.devices["d3"] = store2.devices["d3"]
    empty_dialog = ConsumableDialog(empty_store, theme.LIGHT)
    check("一台能监控的都没有时给一行说明，不是空白框",
          [empty_dialog.tree_device.topLevelItem(0).text(0)],
          ["没有可监控的设备"])
    check("没有可选设备时「下一步」是灰的", empty_dialog.btn_next.isEnabled(), False)

    print("\n9) 属性可以反复改；同名的两块电池要能分清")
    dry = prop(6, 1003, "电池电量", svc="干电池")
    lith = prop(13, 1, "电池电量", svc="锂电池")

    def make_lock(props: list) -> Store:
        """一台门锁：两块电池属性同名（``battery-level``），只有服务注释不同。"""
        s = Store(_Client(), _Worker())  # type: ignore[arg-type]
        s.prefs = Prefs(Path(tempfile.mkdtemp()) / "prefs.json")
        node = DeviceNode("d5", "门锁", "m", "xiaomi.lock.s1", home="我的家",
                          room="客厅", home_id="h1", room_id="r1")
        s.topology = Topology(
            [HomeNode("h1", "我的家", [RoomNode("r1", "客厅")])], [node])
        s.devices["d5"] = DeviceState(
            node=node, spec=SimpleNamespace(properties=props),
            card=build_card_empty(node),
            values={(p.siid, p.piid): 54 for p in props},
            keys={p.key: PropRef("d5", p.siid, p.piid) for p in props},
        )
        s.active_home = "h1"
        return s

    lock = make_lock([dry, lith])
    saved = model.parse({"did": "d5", "siid": 6, "piid": 1003,
                         "label": "门锁电量", "warn": 20, "crit": 10})
    lock.prefs.add_consumable(saved)
    edit = ConsumableDialog(lock, theme.LIGHT, item=saved)
    check("同名属性补服务名，能看出哪块是锂电", 
          [edit.cb_prop.itemText(i) for i in range(edit.cb_prop.count())],
          ["电池电量 · 干电池（%）", "电池电量 · 锂电池（%）"])
    check("打开时选中的是这条自己的属性", edit.cb_prop.currentData(), (6, 1003))
    edit.cb_prop.setCurrentIndex(1)          # 改成锂电池
    check("改属性时名称 / 阈值都留着",
          (edit.ed_name.text(), edit.ed_warn.text(), edit.ed_crit.text()),
          ("门锁电量", "20", "10"))
    edit._on_save()
    check("改完原地替换：还是 1 条，id 换成新属性，名称没丢",
          [(c.id, c.label) for c in lock.prefs.consumables], [("d5:13.1", "门锁电量")])

    reopened = ConsumableDialog(lock, theme.LIGHT, item=lock.prefs.consumable("d5:13.1"))
    check("下次打开还是可改的下拉，不是死文本", reopened.cb_prop.currentData(), (13, 1))
    check("第二步的设备行仍然是只读的",
          [w.text() for w in reopened.findChildren(QLabel)
           if w.objectName() == "Fixed"], ["门锁 · 客厅"])

    lock.prefs.add_consumable(model.parse(
        {"did": "d5", "siid": 6, "piid": 1003, "warn": 20, "crit": 10}))
    clash = ConsumableDialog(lock, theme.LIGHT, item=lock.prefs.consumable("d5:13.1"))
    clash.cb_prop.setCurrentIndex(0)          # 想改成已被另一条占着的干电池
    clash._on_save()
    check("改到已被占用的属性：不顶掉别人、也不改自己",
          [c.id for c in lock.prefs.consumables], ["d5:13.1", "d5:6.1003"])
    check_true("并且说一句为什么没保存", not clash.lbl_err.isHidden())

    twins = make_lock([prop(7, 1, "电池电量", svc="电池"),
                       prop(8, 1, "电池电量", svc="电池")])
    tw = ConsumableDialog(twins, theme.LIGHT, item=model.unset("d5"))
    check("服务名也撞时退回 siid.piid，至少还能分辨",
          [tw.cb_prop.itemText(i) for i in range(tw.cb_prop.count())],
          ["电池电量 · 7.1（%）", "电池电量 · 8.1（%）"])

    shrunk = make_lock([dry])                 # 规格更新：锂电那条读不到了
    shrunk.prefs.add_consumable(model.parse(
        {"did": "d5", "siid": 13, "piid": 1, "warn": 20, "crit": 10}))
    stale = ConsumableDialog(shrunk, theme.LIGHT,
                             item=shrunk.prefs.consumable("d5:13.1"))
    check("旧属性从规格里消失：留着一条说明，不悄悄换成第一个",
          (stale.cb_prop.currentData(), stale.cb_prop.currentText()),
          ((13, 1), "13.1（规格里已没有这个属性）"))
    stale._on_save()
    check("原样保存不会把监控项指到别的量上",
          [c.id for c in shrunk.prefs.consumables], ["d5:13.1"])

    wall = ConsumablesPage(lock, theme.LIGHT)
    wall.refresh()
    app.processEvents()
    check("卡片墙上的两张电池卡也分得清（没自定义名字的那张自动带服务名）",
          sorted(r.name for r in wall._rows),
          sorted(["门锁电量", "电池电量 · 干电池"]))
    check("卡片副标题是「家庭 · 房间 | 属性名」",
          wall._rows[0].subtitle, "我的家 · 客厅 | 电池电量 · 干电池")
    probe = Row(item=model.unset("d5"), name="滤芯", device="净化器", room="",
                percent=None, level=model.LEVEL_NONE, value_text="—")
    check("房间为空（设备已不存在之类）时不留一根孤零零的竖线",
          probe.subtitle, "滤芯")
    check("有家没房时写「家庭 · 未分房间」，不只写个家庭名",
          Row(item=model.unset("d5"), name="滤芯", device="净化器", room="",
              percent=None, level=model.LEVEL_NONE, value_text="—",
              home="示例家庭", home_id="h9", room_id="r9").place_text,
          "示例家庭 · 未分房间")

    # 跨家庭后副标题多了「家庭 · 房间」前缀，把宽度实测固化成断言：
    # 以后改字号 / 卡片尺寸会在这里红，而不是上线后才发现被截成「示例家庭 · 客厅 | 滤芯剩…」。
    # 代表串取"4 字家庭 + 2 字房间 + 6 字属性"，比常见情况长一档。
    from PySide6.QtGui import QFont, QFontMetrics  # noqa: PLC0415

    def _advance(text: str, size: int) -> int:
        font = QFont(QApplication.font())
        font.setPixelSize(size)
        return QFontMetrics(font).horizontalAdvance(text)

    wide = "示例家庭 · 客厅 | 滤芯剩余寿命"
    wide_px = _advance(wide, 12)
    check(f"横卡副标题放得下（12px 实测 {wide_px}px / 可用 {CARD_W - PAD * 2}px）",
          wide_px <= CARD_W - PAD * 2, True)
    # 环卡：属性名搬进环里后，第二行只写位置，字号回到 11；环里的名字按**弦长**卡宽度。
    # 注意这里量的是 offscreen 的回退字体，比真实雅黑**宽约三成**，所以当"下限"用：
    # 6 字名字在这一关都放得下，真机上（雅黑 10px = 60px）必然更宽松。真实字体的样张在
    # `scripts/out/shot_ring_real.py`。
    place_px = _advance("示例家庭 · 客厅", 11)
    check(f"环卡第二行只写位置，放得下（11px 实测 {place_px}px / 方卡可用 "
          f"{RING_CARD - PAD * 2}px）", place_px <= RING_CARD - PAD * 2, True)
    label_px = _advance("滤芯剩余寿命", RING_LABEL_SIZE)
    avail = int(_ring_chord(RING_LABEL_DY + RING_LABEL_H / 2)) - 4
    check(f"环内属性名按弦长放得下（{RING_LABEL_SIZE}px 六字实测 {label_px}px / "
          f"环内可用 {avail}px）", label_px <= avail, True)
    check("弦长按圆心算就是环内径",
          (_ring_chord(0), _ring_chord(RING_D / 2 - RING_W / 2)), (RING_D - RING_W, 0.0))
    check("越往下环越窄（名字按行下沿取宽度才有意义）",
          _ring_chord(10) > _ring_chord(23) > _ring_chord(40), True)
    check_true("环内名字那一行整行都在环里（下沿不越内缘）",
               RING_LABEL_DY + RING_LABEL_H / 2 < RING_D / 2 - RING_W / 2,
               f"{RING_LABEL_DY + RING_LABEL_H / 2} vs {RING_D / 2 - RING_W / 2}")
    check_true("环心数字上抬后仍在内缘之内",
               RING_CENTER_LIFT + RING_CENTER_SIZE / 2 < RING_D / 2 - RING_W / 2,
               f"{RING_CENTER_LIFT + RING_CENTER_SIZE / 2} vs {RING_D / 2 - RING_W / 2}")

    print("\n10) 离线设备的读数：右下角标更新时间，一天没更新算不新鲜")
    import time as clock  # noqa: PLC0415

    from PySide6.QtGui import QColor  # noqa: PLC0415

    from mihome.core.freshness import OFFLINE_STALE_AFTER, read_note  # noqa: PLC0415

    now = 1_700_000_000.0
    check("没有时间戳（静态元数据）不标", read_note(0, now=now), ("", False))
    check("时间戳在未来（设备时钟不准）按今天算",
          read_note(now + 600, now=now), ("今天更新", False))
    check("几小时前读到的还算新鲜",
          read_note(now - 3600, now=now), ("今天更新", False))
    check("刚过一天就判不新鲜",
          read_note(now - OFFLINE_STALE_AFTER - 60, now=now), ("1 天前更新", True))
    check("久了换成月，不写「253 天前」",
          read_note(now - 70 * 86400, now=now), ("2 个月前更新", True))

    off = make_store(Path(tempfile.mkdtemp()))
    off.prefs.add_consumable(Consumable(did="d1", siid=2, piid=9))
    off.prefs.add_consumable(Consumable(did="d2", siid=3, piid=1))
    wall = ConsumablesPage(off, theme.LIGHT)
    d1 = off.devices["d1"]

    def note_of(device: str) -> tuple[str, bool]:
        row = next(r for r in wall._rows if r.device == device)
        return (row.updated, row.stale)

    def orange_bottom_right(card) -> bool:
        """卡片右下角有没有警示色（时间那一行）。只看这一角：右上角读数本身
        可能就是橙色的档位色，全卡扫会把两者混在一起。"""
        img = card.grab().toImage()
        want = QColor(theme.WARNING)
        for y in range(img.height() - 22, img.height()):
            for x in range(img.width() - 110, img.width()):
                c = img.pixelColor(x, y)
                if (abs(c.red() - want.red()) <= 12
                        and abs(c.green() - want.green()) <= 12
                        and abs(c.blue() - want.blue()) <= 12):
                    return True
        return False

    # 在线：不标时间。读数是每轮都在刷新的，标出来只是噪音（用户 2026-11-18 定）
    d1.stamps[(2, 9)] = int(clock.time()) - 3 * 86400
    wall.refresh()
    app.processEvents()
    check("在线设备不标读数时间（哪怕时间戳很旧）", note_of("净化器"), ("", False))

    d1.node.online = False
    wall.refresh()
    app.processEvents()
    check("离线设备标出读数时间；三天前 → 不新鲜",
          note_of("净化器"), ("3 天前更新", True))
    card = wall._cards[next(r.item.id for r in wall._rows if r.device == "净化器")]
    check_true("不新鲜的时间用警示色画在右下角", orange_bottom_right(card))

    d1.stamps[(2, 9)] = int(clock.time()) - 3600
    wall.refresh()
    app.processEvents()
    check("离线但刚读到过：写「今天更新」、不算不新鲜",
          note_of("净化器"), ("今天更新", False))
    card = wall._cards[next(r.item.id for r in wall._rows if r.device == "净化器")]
    check_true("新鲜的时间不用警示色", not orange_bottom_right(card))

    del d1.stamps[(2, 9)]
    wall.refresh()
    app.processEvents()
    check("离线且从没读到过：不写时间，也不硬编「未知」",
          note_of("净化器"), ("", False))

    print()
    print("\n11) 云端耗材只当候选清单：prop 拆 siid.piid / 阈值 / 点一下填好")
    check("prop.6.1 拆成 siid.piid", model.parse_prop("prop.6.1"), (6, 1))
    check("prop.2.1003 也拆得开", model.parse_prop("prop.2.1003"), (2, 1003))
    check("老门锁的 prop.electricity 不猜", model.parse_prop("prop.electricity"), None)
    check("疑似拼接的 prop.4106 也不猜（规格里 siid 4 只有 4.1）",
          model.parse_prop("prop.4106"), None)
    check("不是 prop 前缀", model.parse_prop("6.1"), None)
    check("少一段", model.parse_prop("prop.6"), None)
    check("空值", model.parse_prop(None), None)

    def det(prop_: str, desc: str, value: str, inadeq: str = "",
            exhaust: str = "") -> dict:
        return {"prop": prop_, "description": desc, "value": value,
                "inadeq": inadeq, "exhaust": exhaust, "consumable_type": ""}

    def row(did: str, details: list) -> dict:
        return {"did": did, "name": "设备", "model": "m", "details": details,
                "is_ignore": False, "is_online": True}

    pct5 = '{"val":"5","unit":"percentage","type":"range"}'
    pct0 = '{"val":"0","unit":"percentage","type":"range"}'
    payload = {
        "h1": {
            "items": [
                # 云端按 state 分三组：只取第一组会漏掉后两组（上游 mijiaAPI 就是这么漏的）
                {"state": 1, "count": 2, "ignore_count": 0, "consumes_data": [
                    row("d9", [det("prop.4.1", "滤芯", "17", pct5, pct0)]),
                    row("d9", [det("prop.2.1003", "5号电池", "48", pct5, pct0)]),
                ]},
                {"state": 2, "count": 1, "ignore_count": 0, "consumes_data": [
                    row("d9", [det("prop.13.1", "锂电池", "61", pct5, pct0)]),
                ]},
                {"state": 3, "count": 1, "ignore_count": 0, "consumes_data": [
                    row("d9", [det("prop.electricity", "老门锁电池", "49")]),
                ]},
            ]
        },
        "h2": {
            "items": [
                {"state": 1, "count": 2, "ignore_count": 0, "consumes_data": [
                    # details 是列表：门锁双电池一次给两条
                    row("d8", [det("prop.6.1003", "5号电池", "100", pct5, pct0),
                                det("prop.4.1003", "锂电池", "93", pct5, pct0)]),
                    # 洗衣液是 {val, type:list}，没有 unit：不是百分比，两档都不该收
                    row("d7", [det("prop.6.1", "洗衣液", "",
                                   '{"val":"1","type":"list"}', "{}")]),
                    # 同一 (siid, piid) 再来一条：去重
                    row("d7", [det("prop.6.1", "洗衣液（重复）", "3", pct5, pct0)]),
                ]}
            ]
        },
    }

    cloud = model.cloud_items(payload)
    check("三组 state 全部收（不只第一组）", len(cloud.get("d9", [])), 4)
    check("双电池的两条 details 都收", [c.label for c in cloud.get("d8", [])],
          ["5号电池", "锂电池"])
    # 拆不出的不再静默丢掉：列出来、标成不可添加（老门锁 prop.electricity 那种），
    # 少一条用户只会以为"米家不认这台"，其实是我们寻不到地址
    check("prop 解不出的也列，但标成不可添加",
          [(c.method, c.readable) for c in cloud["d9"]],
          [("4.1", True), ("2.1003", True), ("13.1", True), ("0.0", False)])
    check("同一 siid.piid 去重（留先到的那条）",
          [(c.method, c.value) for c in cloud.get("d7", [])], [("6.1", "")])
    check("百分比阈值收进 warn / 耗尽 0% 当没设", 
          (cloud["d9"][0].warn, cloud["d9"][0].crit), (5, None))
    check("洗衣液那种档位阈值不硬套成数字",
          (cloud["d7"][0].warn, cloud["d7"][0].crit), (None, None))
    check("档位阈值收进 low，kind 标成 level",
          (cloud["d7"][0].low, cloud["d7"][0].kind), (1, "level"))
    check("候选按钮的文案", cloud["d9"][0].summary(), "滤芯 · 剩余 17% · 不足 5%")
    check("没读数时不写「剩余」", model.CloudItem("d1", 1, 1, "洗衣液").summary(),
          "洗衣液")
    check("转成监控项时名称与阈值都带上",
          cloud["d9"][0].consumable().to_dict(),
          {"did": "d9", "siid": 4, "piid": 1, "label": "滤芯",
           "warn": 5, "crit": None})
    check("坏 payload 不炸", model.cloud_items(None), {})

    # -- 弹窗：两个 tab 各走各的路（2026-11-24 用户定） -------------------
    store3 = make_store(Path(tempfile.mkdtemp()))
    store3.prefs.add_consumable(model.parse({"did": "d1", "siid": 2, "piid": 9}))
    dlg = ConsumableDialog(store3, theme.LIGHT)
    check("默认停在「米家耗材」", dlg.stack.currentIndex(), 0)
    check_true("米家 tab 显示「添加到耗材页」", dlg.btn_add.isVisibleTo(dlg))
    check("自选 tab 的「下一步」不占位置", dlg.btn_next.isVisibleTo(dlg), False)

    cands = {
        "d1": [model.CloudItem("d1", 2, 9, "滤芯", "17", 5, None),          # 已配过
               model.CloudItem("d1", 4, 1, "5号电池", "48", 10, None)],
        "d2": [model.CloudItem("d2", 3, 1, "洗衣液", "", None, None, 1, "level")],
    }
    dlg._on_cloud_loaded(cands, None)  # noqa: SLF001
    tops = dlg.tree_cloud.topLevelItemCount()
    check("一台设备一个父节点", tops, 2)
    listed = [dlg.tree_cloud.topLevelItem(i).child(j)
              for i in range(tops)
              for j in range(dlg.tree_cloud.topLevelItem(i).childCount())]
    check("已经配过的那条不列，档位类的写「到不足档时提醒」",
          [r.text(0) for r in listed],
          ["5号电池 · 剩余 48% · 不足 10%", "洗衣液 · 到不足档时提醒"])

    dlg._on_cloud_clicked(listed[0], 0)  # noqa: SLF001
    check_true("选中一行后「添加」才可用", dlg.btn_add.isEnabled())
    dlg._on_add_cloud()  # noqa: SLF001
    got = [c for c in store3.prefs.consumables if c.did == "d1" and c.siid == 4]
    check("落卡用云端的名字（规格里两块电池都叫电池电量）",
          [c.label for c in got], ["5号电池"])
    check("阈值也按云端：耗尽 0% 在我们这里等于不启用",
          [(c.warn, c.crit, c.low) for c in got], [(10, None, None)])

    dlg2 = ConsumableDialog(store3, theme.LIGHT)
    dlg2._on_cloud_loaded(  # noqa: SLF001
        {"d1": [model.CloudItem("d1", 0, 0, "老门锁电池", "49",
                                raw_prop="prop.electricity")]}, None)
    check("全是拆不出地址的：说清有几项读不到，不静默少一条",
          "读不到" in dlg2._cloud_note.text(), True)  # noqa: SLF001
    dlg2.close()

    dlg3 = ConsumableDialog(store3, theme.LIGHT)
    dlg3._on_cloud_loaded(None, RuntimeError("网络断了"))  # noqa: SLF001
    check("读取失败与「没有记录」分开写",
          dlg3._cloud_note.text().startswith("读取失败"), True)  # noqa: SLF001
    dlg3.close()

    # -- 枚举耗材（洗衣液）：显示「不足档位」，不套百分比 ------------------
    enum_item = model.parse({"did": "d2", "siid": 6, "piid": 1,
                             "label": "洗衣液", "low": 1})
    dlg4 = ConsumableDialog(store3, theme.LIGHT, item=enum_item)
    check("枚举项：属性下拉里认得出它（不再说「规格里已没有」）",
          dlg4.cb_prop.currentData(), (6, 1))
    check_true("枚举项显示「不足档位」", dlg4._low_widget.isVisibleTo(dlg4))  # noqa: SLF001
    check("枚举项不显示两档百分比",
          dlg4._warn_widget.isVisibleTo(dlg4), False)  # noqa: SLF001
    check("档位下拉选中落盘那一档", dlg4.cb_low.currentData(), 1)
    built = dlg4._build()  # noqa: SLF001
    check("保存的是档位，不是百分比",
          (built.low, built.warn, built.crit), (1, None, None))

    # 2026-11-24 踩的坑：窗口高度原来读 ``body.sizeHint()``，而 body 被 setGeometry
    # 钉死，报的是"窗口能给的"——枚举表单把「不足档位」压成一条缝（下拉只有 8px）。
    num_item = model.parse({"did": "d2", "siid": 3, "piid": 1, "warn": 20, "crit": 10})
    dlg6 = ConsumableDialog(store3, theme.LIGHT, item=num_item)
    check_true("枚举表单的档位行有正常高度（不是被压成一条缝）",
               dlg4._low_widget.sizeHint().height() >= 40,  # noqa: SLF001
               str(dlg4._low_widget.sizeHint().height()))   # noqa: SLF001
    check_true("数值表单比枚举表单高一行阈值",
               dlg6.height() > dlg4.height(),
               f"{dlg6.height()} vs {dlg4.height()}")
    dlg6.close()
    dlg4.close()

    store3.prefs.add_consumable(enum_item)
    page3 = ConsumablesPage(store3, theme.LIGHT)
    row = next(r for r in page3._rows if r.item.siid == 6)
    check("卡片上写档位文字，不写百分比", (row.state_text, row.percent), ("低", 50.0))
    check("读到不足那档算轻提醒", row.level, model.LEVEL_WARN)
    check("档位序号与总数给卡片画分段条", (row.state_index, row.state_count), (0, 2))

    # 档位卡在三种样式下都不许画"进度"：两档里的"低"按比例是 50%，半满的环 / 染了
    # 一半的卡会被读成"还剩一半洗衣液"，而这页要说的只是"到不足那档了"。
    from mihome.ui.consumables_page import ConsumableCard  # noqa: PLC0415
    ring_row = next(r for r in page3._rows if r.item.siid == 6)
    ring_row.style = model.STYLE_RING
    ring_img = ConsumableCard(ring_row, theme.LIGHT).grab().toImage()
    probe = ring_img.pixelColor(RING_X + RING_D - RING_W // 2, RING_Y + RING_D // 2)
    check_true("圆环样式：档位卡不画半满的弧",
               probe.name().upper() != ring_row.color.upper(), probe.name())
    fill_row = next(r for r in page3._rows if r.item.siid == 6)
    fill_row.style = model.STYLE_FILL
    fill_img = ConsumableCard(fill_row, theme.LIGHT).grab().toImage()
    left = fill_img.pixelColor(8, 60)
    right = fill_img.pixelColor(fill_img.width() - 8, 60)
    check("整卡染色样式：档位卡两边同色（没按档位比例染）",
          (left.name().upper(), right.name().upper()),
          (left.name().upper(), left.name().upper()))

    # -- 米家耗材加进来的项：必须落卡、必须去读（2026-11-24 用户报的 bug）----
    # 现象：弹窗里点「添加到耗材页」后，prefs 有这条、弹窗里也不再列（被 taken
    # 过滤），唯独墙上没卡。两个原因都在这段盯着：①add_item 只认 selected_did，
    # 云端这条路没有它 → 直接 return，不刷新；②登记只在 rebuild_topology 里做，
    # 新属性没进 st.keys → poll_refs 查不到 ref，一次也不读。
    store4 = make_store(Path(tempfile.mkdtemp()))
    page4 = ConsumablesPage(store4, theme.LIGHT)
    # 挑一个"卡片没画过"的属性：模拟滤芯寿命那种只读量
    store4.devices["d1"].keys.pop((4, 1), None)
    before = len(store4.prefs.consumables)

    def _add_via_cloud(dlg: ConsumableDialog) -> None:
        dlg._on_cloud_loaded(  # noqa: SLF001
            {"d1": [model.CloudItem("d1", 4, 1, "5号电池", "48", 10, None)]}, None)
        leaf = dlg.tree_cloud.topLevelItem(0).child(0)
        dlg._on_cloud_clicked(leaf, 0)  # noqa: SLF001
        dlg._on_add_cloud()  # noqa: SLF001
        dlg.close()

    page4._run_dialog = _add_via_cloud  # noqa: SLF001
    page4.add_item()  # noqa: SLF001
    check("云端添加后清单里多一条",
          [c.id for c in store4.prefs.consumables], ["d1:4.1"])
    check("而且墙上真的出现这张卡（前端没刷新就会少一张）",
          ("d1:4.1" in page4._cards, len(page4._rows)),  # noqa: SLF001
          (True, before + 1))
    check("新属性登记进轮询了，否则卡片永远是「—」",
          store4.devices["d1"].keys.get((4, 1)) is not None, True)
    check("这一轮确实会去读它",
          (4, 1) in [(r.siid, r.piid) for r in store4.poll_refs()], True)
    check("卡上用的是云端的名字与阈值",
          [(r.item.label, r.item.warn) for r in page4._rows  # noqa: SLF001
           if r.item.id == "d1:4.1"], [("5号电池", 10)])
    # 历史遗留：prefs 里已经有这条、可 keys 没登记（本次修复前加进去的那些）。
    # 页面对一遍登记就能自愈，不必重建拓扑 / 重启
    store4.devices["d1"].keys.pop((4, 1), None)
    page4.refresh()
    check("刷新页面会补登记（旧配置不用等重构拓扑）",
          store4.devices["d1"].keys.get((4, 1)) is not None, True)
    page4.deleteLater()

    # -- 自选设备：老路一个字没动，也不再塞云端候选 -----------------------
    dlg5 = ConsumableDialog(store3, theme.LIGHT)
    dlg5._on_tab(1)  # noqa: SLF001
    check("切到自选：显示「下一步」", dlg5.btn_next.isVisibleTo(dlg5), True)
    check_true("自选树里有设备", dlg5.tree_device.topLevelItemCount() > 0)
    check("上一版的云端候选按钮已经删干净",
          [b for b in dlg5.findChildren(QPushButton)
           if b.objectName() == "Candidate"], [])
    dlg5.close()

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败：")
        for line in FAILS:
            print("  -", line)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
