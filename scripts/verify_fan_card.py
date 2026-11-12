# SPDX-License-Identifier: GPL-3.0-or-later
"""验证风扇卡片：显式控件、枚举上限、双速度按模式切换、2×1 两列布局。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_fan_card.py``

全程离线：规格是手写的 MIoT JSON（结构与云端一致），不连账号、不碰设备。

分六组：

1. **声明校验**——``Control.widget`` / ``Control.kind`` 写错在 import / 构造期就炸；
2. **绑定**——连续风速藏在 ``dm-service``（自定义服务）里也能绑上；同名候选
   靠 ``kind`` 分开（``swing_h`` 是布尔、``swing_angle`` 是枚举，不许互相抢）；
3. **双速度规则**——``speed_by_mode`` 只在槽位真绑上时生效，未知模式值返回
   ``None``（退回通用顺序，不把风速藏起来）；
4. **枚举步进器**——内部用下标、对外收发设备值；不在档位表里的值不甩指针；
5. **选型**——``widget`` 显式声明优先于类型推断；枚举超过上限不铺胶囊；
6. **2×1 布局**——四格不重叠、都在内容带里；模式 0 显示滑条、1 显示胶囊。

中文在 offscreen 平台渲染成豆腐块，所以这里量几何与控件类型，不读文字。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRect, Qt  # noqa: E402
from PySide6.QtGui import QFont, QImage, QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mihome.core.catalog import Control, build_card  # noqa: E402
from mihome.core.spec import parse_raw  # noqa: E402
from mihome.core.store import DeviceState  # noqa: E402
from mihome.core.tree import DeviceNode  # noqa: E402
from mihome.ui import card_controls  # noqa: E402
from mihome.ui import card_templates as ct  # noqa: E402
from mihome.ui import theme  # noqa: E402
from mihome.ui.card_controls import (  # noqa: E402
    CardSeg,
    CardSlider,
    CardStepper,
    CardToggle,
    fits_segments,
    make_control,
)
from mihome.ui.fan_body import FanBody  # noqa: E402
from mihome.ui.device_card import DeviceCard  # noqa: E402


class _Icons:
    """``DeviceCard`` 只拿它取型号图标；离屏渲染不需要真的图标。"""

    def ensure(self, model: str) -> None:
        pass

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


# ---------------------------------------------------------------------------
# 手写规格（结构与云端 miot-spec 一致）
# ---------------------------------------------------------------------------


def _svc(iid: int, name: str, props: list[dict]) -> dict:
    return {
        "iid": iid,
        "type": f"urn:miot-spec-v2:service:{name}:0000788{iid % 10}:dmaker-p5c:1",
        "description": name,
        "properties": props,
    }


def _p(
    iid: int,
    name: str,
    fmt: str,
    access: tuple[str, ...],
    rng: list | None = None,
    values: tuple[int, ...] = (),
    labels: tuple[str, ...] = (),
) -> dict:
    item: dict = {
        "iid": iid,
        "type": f"urn:miot-spec-v2:property:{name}:0000000{iid % 10}:dmaker-p5c:1",
        "description": name,
        "format": fmt,
        "access": list(access),
    }
    if rng is not None:
        item["value-range"] = rng
    if values:
        texts = labels or tuple(str(v) for v in values)
        item["value-list"] = [
            {"value": v, "description": str(v), "comment": t}
            for v, t in zip(values, texts, strict=True)
        ]
    return item


#: 这台风扇的真机形状（2026-11-12 实测，见 docs/reference/fan-purifier-speed.md）
FAN_RAW = {
    "type": "urn:miot-spec-v2:device:fan:0000A005:dmaker-p5c:1",
    "description": "fan",
    "services": [
        _svc(2, "fan", [
            _p(1, "on", "bool", ("read", "write", "notify")),
            # 真机文案就是 "1挡"…"4挡"（两个字符），这一点很关键：四个两字胶囊
            # 在半个卡片宽里放不下，卡片上会退到步进器
            _p(2, "fan-level", "uint8", ("read", "write", "notify"),
               values=(1, 2, 3, 4), labels=("1挡", "2挡", "3挡", "4挡")),
            _p(3, "mode", "uint8", ("read", "write", "notify"), values=(0, 1),
               labels=("直吹风", "自然风")),
            _p(4, "horizontal-swing", "bool", ("read", "write", "notify")),
            _p(5, "horizontal-swing-included-angle", "uint8",
               ("read", "write", "notify"), values=(30, 60, 90, 120, 140)),
        ]),
        # 连续风速在厂家自定义服务里——正是这条让"更多功能"看不到它
        _svc(8, "dm-service", [
            _p(1, "speed-level", "uint8", ("read", "write", "notify"),
               rng=[1, 100, 1]),
        ]),
        _svc(7, "physical-controls-locked", [
            _p(1, "physical-controls-locked", "bool", ("read", "write", "notify")),
        ]),
    ],
}

#: 只有档位、没有连续量、没有模式的另一种风扇
FAN_MIN_RAW = {
    "type": "urn:miot-spec-v2:device:fan:0000A005:other-fan:1",
    "description": "fan",
    "services": [
        _svc(2, "fan", [
            _p(1, "on", "bool", ("read", "write", "notify")),
            _p(2, "fan-level", "uint8", ("read", "write", "notify"),
               values=(1, 2, 3)),
        ]),
    ],
}


def node(model: str, name: str = "风扇") -> DeviceNode:
    return DeviceNode(
        did="f1", name=name, model=model,
        spec_type="urn:miot-spec-v2:device:fan:0000A005:x:1",
        home="我的家", room="1103-强",
    )


def state_of(card, did: str = "f1", values: dict | None = None) -> DeviceState:
    return DeviceState(node=node(card.model), spec=None, card=card, values=values or {})


def values_for(card, **by_slot) -> dict:
    """按槽位名给值：``values_for(card, mode=1, fan_level=2)``。"""
    out: dict = {}
    for slot, value in by_slot.items():
        ctl = card.bound(slot)
        if ctl is not None and ctl.prop is not None:
            out[(ctl.prop.siid, ctl.prop.piid)] = value
    return out


# ---------------------------------------------------------------------------
# 1. 声明校验
# ---------------------------------------------------------------------------


def test_declaration() -> None:
    print("\n1) 声明校验")
    for field, bad in (("widget", "dial"), ("kind", "list")):
        try:
            Control(slot="x", label="x", role="select", **{field: bad})
        except ValueError:
            ok = True
        else:
            ok = False
        check_true(f"{field}={bad!r} 构造期就报错", ok)
    # 合法值不报错
    ctl = Control(slot="x", label="x", role="slider", widget="slider", kind="range")
    check("合法声明原样保留", (ctl.widget, ctl.kind), ("slider", "range"))


# ---------------------------------------------------------------------------
# 2. 绑定：自定义服务 + 同名候选靠 kind 分开
# ---------------------------------------------------------------------------


def test_binding() -> None:
    print("\n2) 绑定")
    spec = parse_raw(FAN_RAW)
    card = build_card(spec, did="f1", model="dmaker.fan.p5c")

    speed = card.bound("fan_speed")
    check_true("连续风速从 dm-service 绑上了", speed is not None and speed.prop is not None)
    check("连续风速的形状与控件", (speed.prop.kind, speed.widget), ("range", "slider"))
    check("连续风速不被登记进「更多功能」",
          [p.name for p in card.advanced if p.name == "speed-level"], [])

    swing = card.bound("swing_h")
    check("左右扫风绑的是布尔，不是角度枚举",
          (swing.prop.name, swing.prop.kind), ("horizontal-swing", "bool"))

    angle = card.bound("swing_angle")
    check("角度绑的是枚举", (angle.prop.name, angle.prop.kind),
          ("horizontal-swing-included-angle", "enum"))

    check("档位是枚举", card.bound("fan_level").prop.name, "fan-level")
    check("模式是枚举", card.bound("mode").prop.name, "mode")
    check_true("童锁也绑上了", card.bound("child_lock") is not None)

    # 只有档位的风扇：连续风速必须绑不上，不能去抢 fan-level
    plain = build_card(parse_raw(FAN_MIN_RAW), did="f2", model="other.fan")
    check("没有连续量时 fan_speed 空着", plain.bound("fan_speed"), None)
    check("档位照常绑定", plain.bound("fan_level").prop.name, "fan-level")
    check("没有模式时 mode 空着", plain.bound("mode"), None)


# ---------------------------------------------------------------------------
# 3. 双速度规则
# ---------------------------------------------------------------------------


def test_speed_rule() -> None:
    print("\n3) 双速度规则")
    card = build_card(parse_raw(FAN_RAW), did="f1", model="dmaker.fan.p5c")
    check("直吹风(0) → 连续风速", card.speed_slot(0), "fan_speed")
    check("自然风(1) → 档位", card.speed_slot(1), "fan_level")
    check("未知模式值 → 没有结论", card.speed_slot(7), None)
    check("模式没读到 → 没有结论", card.speed_slot(None), None)
    check("值以字符串形式到达也认", card.speed_slot("1"), "fan_level")

    plain = build_card(parse_raw(FAN_MIN_RAW), did="f2", model="other.fan")
    check("没登记规则的型号没有这条规则", plain.speed_by_mode, {})
    check("没规则时 speed_slot 恒为 None", plain.speed_slot(0), None)

    # 规则里指向没绑上的槽位时，那一条会被丢掉（不然卡片会去显示一个不存在的控件）
    partial = build_card(parse_raw(FAN_MIN_RAW), did="f3", model="dmaker.fan.p5c")
    check("指向空槽的规则被剔除（只剩档位那条）", partial.speed_by_mode,
          {1: "fan_level"})


# ---------------------------------------------------------------------------
# 4. 枚举步进器
# ---------------------------------------------------------------------------


def test_enum_stepper(app: QApplication) -> None:
    print("\n4) 枚举步进器")
    spec = parse_raw(FAN_RAW)
    angle = [p for p in spec.properties if p.name == "horizontal-swing-included-angle"][0]
    step = CardStepper(angle, theme.DARK, theme.DARK.accent, label="")
    check("初始落在第一档", step.value(), 30)
    step.set_value(60)
    check("收到设备值 → 指针跟着走", step.value(), 60)
    check("文案是档位名（不是下标）", step._text_at(step._value), "60")  # noqa: SLF001

    sent: list[int] = []
    step.committed.connect(sent.append)
    step._bump(1)  # noqa: SLF001
    check("加一档发出设备值", (sent, step.value()), ([90], 90))
    step._bump(-1)  # noqa: SLF001
    check("减一档回到 60", (sent[-1], step.value()), (60, 60))

    step.set_value(75)
    check("不在档位表里的值不甩指针", step.value(), 60)

    step.set_value(140)
    step._bump(1)  # noqa: SLF001
    check("到顶不再发命令", sent[-1], 60)


# ---------------------------------------------------------------------------
# 5. 选型：声明优先 + 枚举上限（全部走控件库 make_control）
# ---------------------------------------------------------------------------


def _bound(slot: str, label: str, prop, widget: str = "") -> SimpleNamespace:
    return SimpleNamespace(slot=slot, label=label, prop=prop, write=True,
                           role="select", widget=widget)


def _pick(ctl, avail: int = ct.BODY_W) -> object:
    """直接问控件库：``CardBody`` 与 ``FanBody`` 都只是它的调用方。"""
    return make_control(ctl, theme.DARK, theme.DARK.accent, avail=avail)


def test_pick(app: QApplication) -> None:
    print("\n5) 选型（控件库）")
    spec = parse_raw(FAN_RAW)
    by_name = {p.name: p for p in spec.properties}
    check("range + widget=slider → 滑条",
          type(_pick(_bound("fan_speed", "风速", by_name["speed-level"], "slider"))),
          CardSlider)
    check("enum + widget=stepper → 步进器",
          type(_pick(_bound("a", "角度",
                            by_name["horizontal-swing-included-angle"], "stepper"))),
          CardStepper)
    check("bool 自动 → 开关",
          type(_pick(_bound("s", "扫风", by_name["horizontal-swing"]))), CardToggle)
    # 枚举默认不再是胶囊，而是分段轨道（单选语义 + 选中态一眼看得见）
    check("enum 自动 → 分段轨道",
          type(_pick(_bound("m", "模式", by_name["mode"]))), CardSeg)
    check("enum 4 项自动 → 分段轨道",
          type(_pick(_bound("l", "档位", by_name["fan-level"]))), CardSeg)
    # 宽一点才谈得上"放得下"：离屏平台没有字体，度量是方块宽，别拿它定档
    angle_prop = by_name["horizontal-swing-included-angle"]
    check("角度 5 段（给足宽度）→ 分段轨道",
          type(_pick(_bound("a", "角度", angle_prop), avail=220)), CardSeg)

    # 15 档枚举（净化器形状）不切段
    big = parse_raw({
        "type": "urn:miot-spec-v2:device:air-purifier:0000A007:x:1",
        "services": [_svc(11, "air-purifier-favorite", [
            _p(1, "fan-level", "uint8", ("read", "write", "notify"),
               values=tuple(range(15))),
        ])],
    })
    lvl = [p for p in big.properties if p.name == "fan-level"][0]
    check("enum 15 项自动 → 步进器（超上限）", type(_pick(_bound("l", "档位", lvl))),
          CardStepper)
    check("上限 8 段（不再是 6 个胶囊）", ct.MAX_SEGMENTS, 8)
    check_true("上限只有一个来源（模板转发控件库）",
               ct.MAX_SEGMENTS is card_controls.MAX_SEGMENTS)

    # 声明了但放不下：退回按类型挑，**不返回 None**（整格消失比换个控件糟）
    narrow = _pick(_bound("l", "档位", by_name["fan-level"], "chips"), avail=60)
    check_true("widget=chips 窄到装不下也不消失", narrow is not None)
    check("……退到步进器", type(narrow), CardStepper)

    # 同一个 ctl、同一宽度，两个 body 必须给出同一个答案（选型只有一份）
    ctl = _bound("m", "模式", by_name["mode"])
    via_fan = make_control(ctl, theme.DARK, theme.DARK.accent, avail=100, label="")
    check("两个 body 共用同一条规则", type(via_fan), type(_pick(ctl, avail=100)))


# ---------------------------------------------------------------------------
# 6. 分段轨道：选中态必须一眼看得见
# ---------------------------------------------------------------------------


def _rgb(widget, x: int, y: int) -> tuple[int, int, int]:
    """取控件自己画出来的像素（不用 show，``render`` 就够）。"""
    img = QImage(widget.size(), QImage.Format_RGB32)
    img.fill(0)
    widget.render(img)
    c = img.pixelColor(x, y)
    return (c.red(), c.green(), c.blue())


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#%02X%02X%02X" % rgb


def _fill_of(widget, rect) -> str:
    """那一段的**填充色**：在段内取几个点，取出现次数最多的那个。

    不能只取中心点——中心正好是文字，量到的是字色。段边与上下留白才是填充。
    """
    pts = [
        (rect.left() + 3, rect.center().y()),
        (rect.right() - 3, rect.center().y()),
        (rect.center().x(), rect.top() + 3),
        (rect.center().x(), rect.bottom() - 3),
        (rect.left() + 3, rect.top() + 3),
    ]
    seen: dict[str, int] = {}
    for x, y in pts:
        c = _hex(_rgb(widget, x, y))
        seen[c] = seen.get(c, 0) + 1
    return max(seen, key=lambda k: seen[k])


def test_seg(app: QApplication) -> None:
    print("\n6) 分段轨道")
    spec = parse_raw(FAN_RAW)
    mode = [p for p in spec.properties if p.name == "mode"][0]
    accent = theme.category_color("fan")
    seg = CardSeg(mode, theme.DARK, accent)
    seg.resize(200, seg.HEIGHT)
    seg.set_value(0)
    rects = seg._seg_rects()  # noqa: SLF001
    check("两段都排出来了", len(rects), 2)
    r0 = rects[0][0]
    on = _fill_of(seg, r0)
    off = _fill_of(seg, rects[1][0])
    accent = theme.category_color("fan")
    check("选中的那段是实心品类色", on.upper(), accent.upper())
    check_true("没选中的那段不是品类色", off.upper() != accent.upper(), off)
    # 这条是用户 2026-11-12 报的问题：原来的激活态是"卡片底色再混 24% 品类色"，
    # 而卡片开启时底色已经混了 10%，26px 高的一小段上根本看不出差别。
    gap = abs(theme.luma(on) - theme.luma(theme.DARK.card))
    check_true("选中态与卡片底色的亮度差够看（≥60）", gap >= 60, f"{gap:.0f}")
    check("反色字：亮底配深字", seg._ink_on_accent(), "#101317")  # noqa: SLF001

    # 段宽按文字分配：3 位数的段要比 2 位数的段宽
    spec2 = parse_raw({
        "type": "urn:miot-spec-v2:device:fan:0000A005:x:1",
        "services": [_svc(2, "fan", [
            _p(5, "horizontal-swing-included-angle", "uint8",
               ("read", "write", "notify"), values=(30, 140)),
        ])],
    })
    angle = spec2.properties[0]
    seg2 = CardSeg(angle, theme.DARK, theme.category_color("fan"))
    seg2.resize(200, seg2.HEIGHT)
    w = [r.width() for r, _, _ in seg2._seg_rects()]  # noqa: SLF001
    check_true("宽文案的段更宽（不搞等宽）", w[1] > w[0], str(w))

    # 点一下就提交设备值
    sent: list[int] = []
    seg.committed.connect(sent.append)
    seg.setEnabled(True)
    from PySide6.QtCore import Qt as _Qt
    pos = rects[1][0].center()
    ev = QMouseEvent(QMouseEvent.MouseButtonPress, QPointF(pos),
                     QPointF(pos), _Qt.LeftButton, _Qt.LeftButton, _Qt.NoModifier)
    seg.mousePressEvent(ev)
    check("点第二段 → 发设备值 1", sent, [1])


# ---------------------------------------------------------------------------
# 6. 2×1 布局
# ---------------------------------------------------------------------------


def _fan_body(card, values: dict | None = None) -> FanBody:
    state = state_of(card, values=values)
    body = FanBody(ct.style_by_id(ct.FAN_CONTROLS), state,
                   theme.DARK, theme.DARK.accent, None)
    body.resize(312, 78)
    # 隐藏的窗口收不到 resize 事件（Qt 推迟到 show），所以显式 sync 一次把几何
    # 按 312×78 重排——真实卡片里 ``setGeometry`` 之后紧跟 ``refresh`` → ``sync``
    body.sync(state)
    return body


def test_fan_body(app: QApplication) -> None:
    print("\n7) 2×1 布局")
    card = build_card(parse_raw(FAN_RAW), did="f1", model="dmaker.fan.p5c")
    body = _fan_body(card, values_for(card, mode=0, fan_speed=45, fan_level=2,
                                      swing_h=True, swing_angle=60))

    check("四格都建了控件", sorted(k for k, w in body.controls.items() if not w.isHidden()),
          ["fan_speed", "mode", "swing_angle", "swing_h"])
    check("双速度格两个都建了（一个藏着）", sorted(body.controls),
          ["fan_level", "fan_speed", "mode", "swing_angle", "swing_h"])
    # 模式和角度都是"互斥几档"，用分段轨道。角度能不能真切成 5 段取决于**字体
    # 度量**（"140" 在 134px 的格子里要 12px 字号才放得下）；离屏平台没有字体、
    # 度量是方块宽，这里按同一条规则算期望值，避免把环境差异记成失败。
    check("模式 → 分段轨道", type(body.controls["mode"]), CardSeg)
    angle_prop = card.bound("swing_angle").prop
    avail = body.controls["swing_angle"].width() if body.controls["swing_angle"] else 0
    want_angle = CardSeg if fits_segments(angle_prop, avail) else CardStepper
    check("角度 → 按规则选（放得下就是轨道）", type(body.controls["swing_angle"]),
          want_angle)

    # 直吹风：滑条可见、档位藏着，且两者占同一格
    check("mode=0 显示滑条", type(body.controls["fan_speed"]), CardSlider)
    check_true("mode=0 时档位轨道藏着", body.controls["fan_level"].isHidden())

    rects = {k: w.geometry() for k, w in body.controls.items() if not w.isHidden()}
    for slot, rect in rects.items():
        check_true(f"{slot} 落在内容带里",
                   rect.left() >= 0 and rect.right() < 312
                   and rect.top() >= 0 and rect.bottom() < 78, str(rect))

    def overlap(a: QRect, b: QRect) -> bool:
        return a.intersects(b) and not a.intersected(b).isEmpty()

    slots = list(rects)
    bad = [
        (x, y) for i, x in enumerate(slots) for y in slots[i + 1:]
        if overlap(rects[x], rects[y])
    ]
    check("四个控件互不重叠", bad, [])
    check_true("左列在右列左边",
               rects["swing_h"].right() < rects["mode"].left(),
               f"{rects['swing_h']} / {rects['mode']}")

    # 开关贴着格子左边那句文字：`label=""` 表示标签由格子画，开关就该从 0 摆。
    # 靠右摆的话它离右列的「模式」只有 44px，看着像开关属于模式那一格。
    toggle = body.controls["swing_h"]
    check("开关贴着左边文字（不在行尾）", toggle.switch.x(), 0)
    gap = rects["mode"].left() - (toggle.geometry().right() + 1)
    check_true("开关这一格右端到模式之间留出列间距", gap >= 10, f"{gap}px")

    # 切到自然风：滑条藏起来、档位轨道露出来，位置不变（同一格垂直居中）
    before = body.controls["fan_speed"].geometry()
    body.sync(state_of(card, values=values_for(card, mode=1, fan_level=2)))
    check_true("mode=1 时滑条收起", body.controls["fan_speed"].isHidden())
    check_true("mode=1 时档位露出", not body.controls["fan_level"].isHidden())
    # 档位同样是"互斥几档"，能不能切成 4 段一样取决于字体度量（"1挡" 是中文）
    lvl_prop = card.bound("fan_level").prop
    lvl_avail = body.controls["fan_level"].width()
    want_lvl = CardSeg if fits_segments(lvl_prop, lvl_avail) else CardStepper
    check("自然风下档位也按规则选", type(body.controls["fan_level"]), want_lvl)
    after = body.controls["fan_level"].geometry()
    check("换控件后仍落在同一格（左边界与列宽一致）",
          (after.left(), after.width()), (before.left(), before.width()))
    check_true("高度不同但都在格内居中",
               after.center().y() == before.center().y(),
               f"{after.center().y()} vs {before.center().y()}")

    # 没登记双速度规则的型号：档位优先（和详情页一致），滑条藏着
    plain = build_card(parse_raw(FAN_MIN_RAW), did="f2", model="other.fan")
    plain_body = _fan_body(plain, values_for(plain, fan_level=2))
    visible = sorted(k for k, w in plain_body.controls.items() if not w.isHidden())
    check("没有规则时只留能用的那一项", visible, ["fan_level"])
    check_true("没有档位就没有风速格", "fan_speed" not in plain_body.controls)

    # 用户报的那个 bug：设备在直吹风，模式里却没有高亮。这里直接问控件
    # "你当前的选中值是哪个"，以及"选中那段画出来是不是品类色"。
    mode_seg = body.controls["mode"]
    body.sync(state_of(card, values=values_for(card, mode=0, power=True, fan_speed=45,
                                               swing_h=True, swing_angle=60)))
    check("直吹风时模式控件记住了当前值", mode_seg._current, 0)  # noqa: SLF001
    check_true("设备开着时不置灰（置灰会把高亮压到看不出）", not mode_seg._dimmed)  # noqa: SLF001
    mode_seg.setGeometry(0, 0, 118, mode_seg.HEIGHT)
    r0 = mode_seg._seg_rects()[0][0]  # noqa: SLF001
    px = _fill_of(mode_seg, r0)
    # 这张 body 用的是 palette 的强调色（不是品类色），取它自己的
    check("选中的那一段画成了实心强调色", px.upper(), theme.DARK.accent.upper())


def _mouse(widget, pos: QPointF, *, press: bool = True) -> None:
    """给控件发一个真实的左键事件（不经 hit-test，直接进控件自己的处理函数）。"""
    kind = QMouseEvent.MouseButtonPress if press else QMouseEvent.MouseButtonRelease
    ev = QMouseEvent(kind, pos, pos, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    if press:
        widget.mousePressEvent(ev)
    else:
        widget.mouseReleaseEvent(ev)


def test_wiring(app: QApplication) -> None:
    """控件点下去，必须从 body 的信号口发出 (siid, piid, value)。

    这一组是用户 2026-11-12 报的第二个 bug：点模式 UI 切成自然风了、风扇没动，
    而详情页同一个模式能控制。根因是 ``FanBody._build`` 只把控件塞进
    ``self.controls``，**没接 ``committed``**——控件改了 ``_current`` 并重绘
    （所以"看起来生效了"），写请求一个都没发。

    前面第 6/7 组测的是零件本身（直接连 ``widget.committed``），绕过了这层
    接线，所以它们全绿也没发现。**零件能单测 ≠ 接好了**：这里从 body 的出口
    断言，三类信号（int / bool / int）各来一发。
    """
    print("\n8) 接线（控件 → value_committed）")
    card = build_card(parse_raw(FAN_RAW), did="f1", model="dmaker.fan.p5c")
    body = _fan_body(card, values_for(card, mode=0, fan_speed=45, fan_level=2,
                                      swing_h=True, swing_angle=60))
    sent: list[tuple[int, int, object]] = []
    body.value_committed.connect(lambda siid, piid, v: sent.append((siid, piid, v)))

    def prop_of(slot: str):
        return card.bound(slot).prop

    # 1) 分段轨道（mode）：press 就提交，值为这一段对应的设备值
    seg = body.controls["mode"]
    hit = seg._seg_rects()[1][0].center()  # noqa: SLF001
    _mouse(seg, QPointF(hit))
    _mouse(seg, QPointF(hit), press=False)
    p = prop_of("mode")
    check("点模式轨道 → 发 (siid, piid, 自然风=1)", sent[-1] if sent else None,
          (p.siid, p.piid, 1))

    # 2) 开关（swing_h）：真实点击是 press 冒到父、release 落在滑块上
    toggle = body.controls["swing_h"]
    sw = toggle.switch
    _mouse(toggle, QPointF(1, 1))
    _mouse(sw, QPointF(sw.width() / 2, sw.height() / 2), press=False)
    p = prop_of("swing_h")
    check("点开关 → 发开关的反面（True → False）", sent[-1], (p.siid, p.piid, False))

    # 3) 滑条（fan_speed）：松手才提交，发的是整数
    slider = body.controls["fan_speed"]
    pos = QPointF(slider.width() * 0.75, slider.height() / 2)
    _mouse(slider, pos)
    _mouse(slider, pos, press=False)
    p = prop_of("fan_speed")
    got = sent[-1] if sent else (None, None, None)
    check("拖滑条松手 → 发 (siid, piid, 值)", (got[0], got[1]), (p.siid, p.piid))
    check_true("滑条发出的值在 1–100 内", isinstance(got[2], int) and 1 <= got[2] <= 100,
               repr(got[2]))

    # 反向证明：把接线摘掉，同样一发点击就不该有请求。没有这一条，将来有人把
    # 断言改回"看控件自己的 _current"就又漏了（第 6 组正是这么漏的）。
    body2 = _fan_body(card, values_for(card, mode=0, swing_h=True))
    idle: list = []
    body2.value_committed.connect(lambda *a: idle.append(a))
    seg2 = body2.controls["mode"]
    seg2.committed.disconnect()  # 摘掉 body 那条接线
    hit2 = seg2._seg_rects()[1][0].center()  # noqa: SLF001
    _mouse(seg2, QPointF(hit2))
    check("摘掉接线后同样点击不再发请求（证明上面测的是接线）", idle, [])


def test_summary(app: QApplication) -> None:
    """副标题替第二行报数，第二行就不画标签（2026-11-12 用户提）。

    用户的原话：副标题只写"已开启/已关闭"太浪费——开启时改成「扫风角度 xx
    风速 xx」同样看得懂，却能把第二行两个标签的宽度让给控件（五段角度从每段
    26px 回到 33px）。这一组要守住三件事：

    1. 副标题**只在开且有值**时报数（关着报设定值像它正在吹）；
    2. 让出的只有第二行那两格，开关和模式的名字照旧；
    3. 关机 / 没值的时候标签会回来——不能出现"标签没了、副标题也没提"的格子。
    """
    print("\n9) 副标题替「角度 / 风速」报数")
    card = build_card(parse_raw(FAN_RAW), did="f1", model="dmaker.fan.p5c")
    style = ct.style_by_id(ct.FAN_CONTROLS)

    def card_of(**kw) -> DeviceCard:
        return DeviceCard(state_of(card, values=values_for(card, **kw)), _Icons(),
                          theme.DARK, style)

    check("开启时副标题报角度与风速",
          card_of(power=True, mode=0, fan_speed=45, fan_level=2,
                  swing_h=True, swing_angle=60)._subtitle(),
          "扫风角度 60 · 风速 45")
    check("自然风只报档位，不同时报两个数",
          card_of(power=True, mode=1, fan_level=2, swing_angle=60,
                  swing_h=True)._subtitle(),
          "扫风角度 60 · 风速 2挡")
    check("关着不报设定值（像是它正在吹）",
          card_of(power=False, mode=0, fan_speed=45, swing_angle=60)._subtitle(),
          "已关闭")

    # 开着的 body：第二行两格不画标签，控件吃满整列
    body = _fan_body(card, values_for(card, power=True, mode=0, fan_speed=45,
                                      fan_level=2, swing_h=True, swing_angle=60))
    labels = sorted(t for _, t in body._labels)  # noqa: SLF001
    check("第二行两个标签让出来了（只剩开关和模式）", labels, ["左右扫风", "模式"])
    cols = FanBody._column_x(312)  # noqa: SLF001
    angle = body.controls["swing_angle"].geometry()
    check("角度轨道吃满左列", (angle.left(), angle.width()), (0, cols[0][1]))
    speed = body.controls["fan_speed"].geometry()
    check("风速滑条吃满右列", (speed.left(), speed.width()),
          (cols[1][0], cols[1][1]))

    # 滑条右侧那个数值也交给副标题了：轨道因此长了整整 VALUE_W（48px）。
    # 用"开/关 show_value 两测"来断言，而不是背一个绝对坐标——这样 VALUE_W
    # 改了这条依然成立，只有"数值真的不再占位"才会红。
    slider = body.controls["fan_speed"]
    check("风速滑条的数值交给副标题了", slider.show_value, False)
    span_off = slider._span()[1] - slider._span()[0]  # noqa: SLF001
    slider.set_show_value(True)
    span_on = slider._span()[1] - slider._span()[0]  # noqa: SLF001
    slider.set_show_value(False)
    check_true("为此轨道长了 48px（VALUE_W）", abs(span_on + 48 - span_off) < 1.5,
               f"{span_off:.0f} vs {span_on:.0f}")

    # 关机：控件不重建，只是把宽度让回去，标签跟着回来
    body.sync(state_of(card, values=values_for(card, power=False, mode=0,
                                               fan_speed=45, swing_angle=60)))
    labels = sorted(t for _, t in body._labels)  # noqa: SLF001
    check("关机后标签回来", labels, ["左右扫风", "模式", "角度", "风速"])
    check("关机后数值也回来（副标题不再报它）",
          body.controls["fan_speed"].show_value, True)
    angle = body.controls["swing_angle"].geometry()
    check_true("角度轨道把标签宽度让了回去", angle.left() > 0, str(angle))

    # 槽位绑上了但这一轮没读到值：副标题不提它，标签也不能省
    empty = dict(values_for(card, power=True, mode=0, swing_angle=60, swing_h=True))
    body.sync(state_of(card, values=empty))
    check("风速没值时副标题只报角度",
          card_of(power=True, mode=0, swing_angle=60, swing_h=True)._subtitle(),
          "扫风角度 60")
    labels = [t for _, t in body._labels]  # noqa: SLF001
    check_true("没值的那格标签留着", "风速" in labels, str(labels))
    check("没值的那格数值也留着（副标题没提它）",
          body.controls["fan_speed"].show_value, True)


def main() -> int:
    app = QApplication.instance() or QApplication([])
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(13)
    app.setFont(font)

    test_declaration()
    test_binding()
    test_speed_rule()
    test_enum_stepper(app)
    test_pick(app)
    test_seg(app)
    test_fan_body(app)
    test_wiring(app)
    test_summary(app)

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败：")
        for f in FAILS:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
