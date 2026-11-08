# SPDX-License-Identifier: GPL-3.0-or-later
"""验证「设备右键菜单设定卡片样式」与 2×1 灯具双滑块。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_card_style.py``

分六组，重点钉住那些「改了以后会悄悄退化」的约束：

1. **偏好**——按 did 落盘 / 重读、v3 的 ``{span,template}`` 迁移、坏数据过滤、
   回默认不残留冗余记录；
2. **样式解析**——一个样式同时决定宽度与 body；带滑条的样式都是 2×1，
   标准样式固定 1×1，不再读 ``CardSpec.span``；只有绑上 ``value_range``
   的属性才给滑条；未知 id / 能力不够都兜底回标准；
3. **几何**——所有卡片等高、2×1 = 2×166+8、body 严格落在内容带里；
4. **像素**——亮度轨道是灯品类色、色温轨道左暖右冷（不是同一支控件换配色）；
5. **菜单**——只有一个「卡片样式」子菜单（没有单独的大小 / 恢复默认）、
   透明无边框、自绘 ✓、只列可用样式且文字标出尺寸；
6. **接线**——松手才发命令、局部刷新复用卡片对象、FlowLayout 跟着重排。

中文在 offscreen 平台会渲染成豆腐块，所以这里量几何与像素，不读文字。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect, Qt  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QComboBox  # noqa: E402

from mihome.core import adapters, catalog  # noqa: E402
from mihome.core.catalog import PROFILES  # noqa: E402
from mihome.core.prefs import Prefs  # noqa: E402
from mihome.core.spec import PropertySpec, ValueOption, parse_raw  # noqa: E402
from mihome.core.store import DeviceState, RoomView, _card_props  # noqa: E402
from mihome.core.tree import DeviceNode  # noqa: E402
from mihome.ui import theme  # noqa: E402
from mihome.ui.card_controls import ROW_H  # noqa: E402
import mihome.ui.card_templates as card_templates  # noqa: E402
from mihome.ui.card_templates import (  # noqa: E402
    BODY_READOUT,
    LIGHT_BRIGHT,
    LIGHT_DUAL,
    PRESENCE_STATE,
    SIZES,
    STANDARD,
    STYLES,
    TEMP_CONTROL,
    CardStyle,
    available_styles,
    default_style,
    resolve_card_style,
    size_of,
    style_by_id,
    validate_styles,
)
from mihome.ui.device_card import DeviceCard  # noqa: E402
from mihome.ui.menu import Menu  # noqa: E402
from mihome.ui.wall import WallView  # noqa: E402

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


def diff(a: QColor, b: QColor) -> int:
    return max(abs(a.red() - b.red()), abs(a.green() - b.green()),
               abs(a.blue() - b.blue()))


class Icons:
    def ensure(self, model: str) -> None:
        pass

    def scaled(self, model: str, size: int):
        return None


def prop(siid: int, piid: int, rng=None, unit: str | None = None) -> PropertySpec:
    return PropertySpec(
        siid=siid, piid=piid, name="p", urn="", description="", comment="",
        format="uint16", access=("read", "write", "notify"), unit=unit,
        value_range=rng, value_list=(), service="", service_label="",
    )


def enum_prop(siid: int, piid: int, *options: tuple[int, str]) -> PropertySpec:
    """只读枚举属性（有人/无人），没有 value_range——滑条用不了，读数能显示。"""
    return PropertySpec(
        siid=siid, piid=piid, name="p", urn="", description="", comment="",
        format="uint8", access=("read", "notify"), unit=None,
        value_range=None,
        value_list=tuple(ValueOption(v, text, text) for v, text in options),
        service="", service_label="",
    )


def _by_slot(items, slot: str):
    for it in items:
        if it.slot == slot:
            return it
    return None


def make_state(
    did: str = "d1",
    *,
    category: str = "light",
    bright: bool = True,
    temp: bool = True,
    ranged: bool = True,
    online: bool = True,
    on: bool = True,
    name: str = "客厅吸顶灯",
) -> DeviceState:
    """造一台灯（或别的东西），属性是真实 ``PropertySpec``，卡片用鸭子类型。"""
    ctls: dict[str, SimpleNamespace] = {
        "power": SimpleNamespace(slot="power", label="开关",
                                 prop=prop(2, 1), write=True, role="switch")
    }
    values: dict[tuple[int, int], object] = {(2, 1): on}
    if bright:
        ctls["brightness"] = SimpleNamespace(
            slot="brightness", label="亮度",
            prop=prop(2, 2, (1, 100, 1) if ranged else None, "percentage"),
            write=True, role="slider")
        values[(2, 2)] = 60
    if temp:
        ctls["color_temp"] = SimpleNamespace(
            slot="color_temp", label="色温",
            prop=prop(2, 3, (2700, 6500, 1) if ranged else None, "kelvin"),
            write=True, role="slider")
        values[(2, 3)] = 4000
    card = SimpleNamespace(
        category=category, span=1, gangs=[], controls=list(ctls.values()),
        metrics=[], advanced=[], layout=category, power_actions=None,
        default_style="", mode_active=None,
        control=lambda slot: ctls.get(slot),
    )

    def _bound(slot: str):
        hit = card.control(slot)
        return hit if hit is not None else _by_slot(card.metrics, slot)

    card.bound = _bound
    node = DeviceNode(did, name, "m", f"{category}.x",
                      home="我的家", room="客厅", online=online)
    return DeviceState(node=node, spec=None, card=card, values=values)


def make_presence_state(
    did: str = "p1",
    *,
    state: int | None = 1,
    default_style: str = PRESENCE_STATE,
    online: bool = True,
) -> DeviceState:
    """人在传感器：``occupancy`` 登记在 **metrics**（只读指标），不在 controls。

    真实设备就是这个形状（``catalog`` 的 ``occupancy-sensor`` 品类），所以这条
    用例专门盯着"样式能不能引用一个可写控件列表里压根没有的槽位"。
    """
    st = make_state(did, category="occupancy-sensor", bright=False, temp=False,
                    name="领普人体存在传感器", online=online)
    st.card.metrics = [
        SimpleNamespace(
            slot="occupancy", label="人体状态", write=False, role="readout",
            prop=enum_prop(3, 1, (0, "无人"), (1, "有人")),
        ),
        SimpleNamespace(
            slot="illumination", label="照度", write=False, role="readout",
            prop=prop(3, 2, None, "percentage"),
        ),
    ]
    st.card.default_style = default_style
    if state is not None:
        st.values[(3, 1)] = state
    st.values[(3, 2)] = 49
    return st


def select_prop(siid: int, piid: int, *options: tuple[int, str]) -> PropertySpec:
    """**可写**枚举（模式下拉）。``enum_prop`` 是只读的，选完写不出去。"""
    return PropertySpec(
        siid=siid, piid=piid, name="p", urn="", description="", comment="",
        format="uint8", access=("read", "write", "notify"), unit=None,
        value_range=None,
        value_list=tuple(ValueOption(v, text, text) for v, text in options),
        service="", service_label="",
    )


LAMP_MODES = ((5, "阅读"), (6, "电脑"), (7, "影院"), (8, "暖光"), (9, "冷光"))

#: 米家显示器挂灯 1S 的规格形状（照 `parse_raw` 的输入格式手写）。
#: ``mode``(2,5) 可写、``scene-mode``(3,9) 只读——后者才是实际生效的预设。
LAMP_RAW = {
    "type": "urn:miot-spec-v2:device:light:0000A001:yeelink-lamp22:1:0000C802",
    "description": "米家显示器挂灯 1S",
    "services": [
        {
            "iid": 2,
            "type": "urn:miot-spec-v2:service:light:00007802:yeelink-lamp22:1",
            "description": "灯光",
            "properties": [
                {"iid": 1, "type": "urn:miot-spec-v2:property:on:00000006:yeelink-lamp22:1",
                 "description": "开关", "format": "bool",
                 "access": ["read", "write", "notify"]},
                {"iid": 2, "type": "urn:miot-spec-v2:property:brightness:0000000D:yeelink-lamp22:1",
                 "description": "亮度", "format": "uint8",
                 "access": ["read", "write", "notify"], "value-range": [1, 100, 1]},
                {"iid": 3, "type": "urn:miot-spec-v2:property:color-temperature:0000000B:yeelink-lamp22:1",
                 "description": "色温", "format": "uint16",
                 "access": ["read", "write", "notify"], "value-range": [2700, 6500, 1]},
                {"iid": 5, "type": "urn:miot-spec-v2:property:mode:00000008:yeelink-lamp22:1",
                 "description": "模式", "format": "uint8",
                 "access": ["read", "write", "notify"],
                 "value-list": [{"value": v, "description": t} for v, t in LAMP_MODES]},
            ],
        },
        {
            "iid": 3,
            "type": "urn:miot-spec-v2:service:scene:0000780B:yeelink-lamp22:1",
            "description": "情景",
            "properties": [
                {"iid": 9, "type": "urn:miot-spec-v2:property:scene-mode:00000010:yeelink-lamp22:1",
                 "description": "情景模式", "format": "uint8",
                 "access": ["read", "notify"], "value-range": [0, 10, 1]},
            ],
        },
    ],
}


def make_lamp_state(
    did: str = "lamp1",
    *,
    scene: object = 0,
    mode: int = 5,
) -> DeviceState:
    """挂灯：``mode``(2,5) 只写，``scene-mode``(3,9) 当激活判据（0 = 无激活项）。

    走的是 ``CardSpec.mode_active`` 那条真链路：``DeviceState.mode_active_value()``
    从这个属性取值，界面不自己拼 siid/piid。
    """
    st = make_state(did, category="light", name="米家显示器挂灯 1S")
    mode_prop = select_prop(2, 5, *LAMP_MODES)
    scene_prop = enum_prop(3, 9, *((i, f"场景{i}") for i in range(11)))
    st.card.controls.append(
        SimpleNamespace(slot="mode", label="模式", prop=mode_prop,
                        write=True, role="select")
    )
    st.card.mode_active = scene_prop
    st.values[mode_prop.key] = mode
    if scene is not None:
        st.values[scene_prop.key] = scene

    def _ctl(slot: str):
        return next((c for c in st.card.controls if c.slot == slot), None)

    def _bound(slot: str):
        hit = _ctl(slot)
        return hit if hit is not None else _by_slot(st.card.metrics, slot)

    st.card.control = _ctl
    st.card.bound = _bound
    return st


def fresh_prefs(tmp: Path, name: str = "p.json") -> Prefs:
    return Prefs(tmp / name)


AC_MODES = ("自动", "制冷", "除湿", "制热", "送风")


def bool_prop(siid: int, piid: int) -> PropertySpec:
    return PropertySpec(
        siid=siid, piid=piid, name="p", urn="", description="", comment="",
        format="bool", access=("read", "write", "notify"), unit=None,
        value_range=None, value_list=(), service="", service_label="",
    )


def enum_writable(siid: int, piid: int, *options: tuple[int, str]) -> PropertySpec:
    """可写枚举（空调模式）：有 ``value_list``、没有 ``value_range``。

    ``enum_prop`` 那个是只读的，用来测读数；这里要测「点胶囊发命令」，
    access 必须带 write，否则 ``CardBody.sync`` 会把它禁用掉。
    """
    return PropertySpec(
        siid=siid, piid=piid, name="p", urn="", description="", comment="",
        format="uint8", access=("read", "write", "notify"), unit=None,
        value_range=None,
        value_list=tuple(ValueOption(v, text, text) for v, text in options),
        service="", service_label="",
    )


def make_ac_state(
    did: str = "ac1",
    *,
    on: bool = True,
    target: int | None = 26,
    mode: int = 1,
    online: bool = True,
    ranged: bool = True,
    modes: tuple[str, ...] = AC_MODES,
    default: str | None = None,
) -> DeviceState:
    """一台空调。形状照真机：``target_temp`` 是 stepper、``mode`` 是 select，
    另外两个只读指标（室温 / 湿度）挂在 metrics 上，构成副标题。

    ``target=None`` / ``modes=()`` 用来模拟红外空调（只有 ir-mode / ir-temperature），
    ``ranged=False`` 模拟"有温度属性但没有刻度"的畸形规格。``default`` 不传时
    取 ``PROFILES`` 里空调品类**真实声明**的默认样式（红外版是另一个 Profile，
    显式传空串），这样改了品类默认这条夹具会跟着走，不会偷偷脱节。
    """
    ctls: dict[str, SimpleNamespace] = {
        "power": SimpleNamespace(slot="power", label="开关", prop=bool_prop(2, 1),
                                 write=True, role="switch"),
    }
    values: dict[tuple[int, int], object] = {(2, 1): on}
    if target is not None:
        ctls["target_temp"] = SimpleNamespace(
            slot="target_temp", label="温度",
            prop=prop(2, 4, (16, 32, 1) if ranged else None, "celsius"),
            write=True, role="stepper")
        values[(2, 4)] = target
    if modes:
        ctls["mode"] = SimpleNamespace(
            slot="mode", label="模式",
            prop=enum_writable(2, 2, *list(enumerate(modes))),
            write=True, role="select")
        values[(2, 2)] = mode
    metrics = [
        SimpleNamespace(slot="temperature", label="室温", write=False, role="readout",
                        prop=prop(4, 7, None, "celsius")),
        SimpleNamespace(slot="humidity", label="湿度", write=False, role="readout",
                        prop=prop(4, 9, None, "percentage")),
    ]
    values[(4, 7)] = 24.9
    values[(4, 9)] = 54
    card = SimpleNamespace(
        category="air-conditioner", span=1, gangs=[], controls=list(ctls.values()),
        metrics=metrics, layout="climate", power_actions=None, mode_active=None,
        default_style=(PROFILES["air-conditioner"].default_style if default is None
                       else default),
        control=lambda slot: ctls.get(slot),
    )
    card.bound = lambda slot: ctls.get(slot) or _by_slot(metrics, slot)
    node = DeviceNode(did, "空调", "xiaomi.aircondition.m9", "air-conditioner.x",
                      home="我的家", room="强", online=online)
    return DeviceState(node=node, spec=None, card=card, values=values)


def pixel(img, x: float, y: float) -> QColor:
    dpr = img.devicePixelRatioF()
    return img.pixelColor(round(x * dpr), round(y * dpr))


def icon_alpha(act) -> int:
    img = act.icon().pixmap(16, 16).toImage()
    return max(img.pixelColor(x, y).alpha()
               for x in range(img.width()) for y in range(img.height()))


def layout_now(section, width: int = 800) -> None:
    """离屏窗口没有真实 resize 事件，手动给 FlowLayout 一次确定的宽度。

    不这么做的话 ``itemAt(1).geometry()`` 可能还是 (0,0)，量出来的
    「切换前位置」是假值。
    """
    flow = section.flow
    flow.setGeometry(QRect(0, 0, width, flow.heightForWidth(width)))


class FakeStore:
    """WallView 用到的 Store 接口，只给验证脚本用。"""

    def __init__(self, prefs: Prefs, states: list[DeviceState]) -> None:
        self.prefs = prefs
        self.devices = {s.did: s for s in states}
        self.scenes: list = []
        self.views = [RoomView("h", "我的家", "客厅", list(states))]

    def rooms(self, home: str | None = None):
        return self.views

    def scenes_for(self, home: str):
        return []


class LooseStore(FakeStore):
    """造详情页用：没实现的 Store 方法一律 no-op。

    详情页会碰 ``set_value`` / 各种偏好；这个测试只关心"构造得出来、交接接口
    名字对得上"，不关心写请求去了哪。如果哪天真的少了一个**必须**的方法，
    这里会静默 no-op——所以只在"点开详情不崩"这一条上用它，别扩用。

    ``set_value`` 单独记下来：模式行要验证"占位态下点一项写出去的是真实枚举值"。
    """

    def __init__(self, prefs: Prefs, states: list[DeviceState]) -> None:
        super().__init__(prefs, states)
        self.writes: list[tuple[str, tuple[int, int], object]] = []

    def set_value(self, did: str, key: tuple[int, int], value: object) -> None:
        self.writes.append((did, key, value))

    def __getattr__(self, name: str):
        return lambda *a, **k: None


def main() -> int:
    app = QApplication(sys.argv[:1])  # noqa: F841
    tmp = Path(tempfile.mkdtemp(prefix="mihome-card-style-"))
    light = make_state()

    print("1) 偏好：按 did 落盘 / 重读 / v3 迁移 / 过滤")
    p = fresh_prefs(tmp)
    # "没设过"必须是 None，"显式选了标准"必须是 "standard"：两者混成一个值的话，
    # 品类默认样式（存在传感器 → 状态卡）永远轮不到。兜底在 default_style()。
    check("没设过是 None，不预填 standard", p.card_style("d1"), None)
    check("默认不落盘", p.card_styles, {})
    check("由 default_style 兜底成标准", default_style(light.card).id, STANDARD)
    check("设样式", p.set_card_style("d1", LIGHT_DUAL), True)
    check("重复设同一个值不再写盘", p.set_card_style("d1", LIGHT_DUAL), False)
    again = fresh_prefs(tmp, "p.json")
    check("重读样式", again.card_style("d1"), LIGHT_DUAL)
    check("一条记录就是一个字符串 id", again.card_styles["d1"], LIGHT_DUAL)
    check("回默认会删记录", again.set_card_style("d1", STANDARD), True)
    check("回默认后字典为空", again.card_styles, {})
    check("已在默认再回默认不写盘", again.set_card_style("d1", STANDARD), False)
    check("clear 等价回默认", again.set_card_style("d1", LIGHT_BRIGHT), True)
    check("clear_card_style", again.clear_card_style("d1"), True)
    check("非法样式 id 被拒", again.set_card_style("d1", "Bad Id!"), False)
    check("空 did 被拒", again.set_card_style("", LIGHT_DUAL), False)

    bad = tmp / "bad.json"
    bad.write_text(json.dumps({
        "card_styles": {
            "new": "light_bright",                           # v4
            "v3_dual": {"span": 1, "template": "light_dual"},  # v3 → 沿用 id
            "v3_wide": {"span": 2, "template": "standard"},    # v3 伪组合 → 回默认
            "v3_span": {"span": 2},                            # 只改过大小 → 回默认
            "junk": {"span": 9, "template": "BAD ID"},
            "plain": 42,
            "empty": "",
        }}), encoding="utf-8")
    loaded = Prefs(bad)
    check("坏数据 / v3 迁移只留合法项", loaded.card_styles,
          {"new": LIGHT_BRIGHT, "v3_dual": LIGHT_DUAL})
    check("v3 的 2×1 标准退回默认（记录被清掉）", loaded.card_style("v3_wide"), None)
    check("落盘版本已升到 4",
          json.loads((tmp / "p.json").read_text(encoding="utf-8"))["version"], 4)

    print("\n2) 样式解析：能力过滤 / 宽度由样式自带")
    check("灯的三个样式", [s.id for s in available_styles(light.card)],
          [STANDARD, LIGHT_DUAL, LIGHT_BRIGHT])
    check("标准样式 1×1、无 body", (STYLES[0].span, STYLES[0].has_body),
          (1, False))
    check("挂了滑条的样式一律 2×1",
          [s.span for s in STYLES if s.has_body and s.body == "sliders"], [2, 2])
    check("默认样式排第一（灯的默认是标准）",
          available_styles(light.card)[0].id, STANDARD)
    check("菜单文字尺寸在前", style_by_id(LIGHT_DUAL).menu_label,
          "宽卡 2×1：亮度 + 色温")

    print("\n2a) 维度分离：尺寸通用 / 内容跟品类")
    check("尺寸表覆盖 1×1 / 2×1 / 2×2，文案只在这里写一次",
          [s.label for s in SIZES], ["小卡 1×1", "宽卡 2×1", "大卡 2×2"])
    check("表外的 span 查不到", size_of(3), None)
    check_true("每个样式都指向同一张尺寸表（不是各写一份）",
               all(size_of(s.span, s.rows) is s.size for s in STYLES))
    for s in STYLES:
        check_true(f"{s.id}：菜单前段由尺寸表生成",
                   s.menu_label.startswith(f"{size_of(s.span, s.rows).label}："),
                   s.menu_label)

    def raises(**kw) -> bool:
        try:
            CardStyle(**kw)
        except ValueError:
            return True
        return False

    check_true("span 写错在声明期就报错",
               raises(id="bad", label="随便", span=3))
    check_true("label 里再写一遍尺寸会被拒（避免两个来源）",
               raises(id="bad", label="宽卡 2×1：温度控制", span=2))
    check("标准样式的内容名就是「标准」", STYLES[0].label, "标准")

    # 用户设想的空调模板：不改注册表、不碰外壳，只声明内容名与尺寸即可。
    ac_style = CardStyle(id="ac_temp", label="温度控制", span=2, category="ac")
    check("空调模板只写内容名，尺寸自动带出",
          ac_style.menu_label, "宽卡 2×1：温度控制")
    check("空调模板不会出现在灯的菜单里", ac_style.available(light.card), False)

    print("\n2b) 注册表自检：同内容可多尺寸，撞车要拒")
    validate_styles(STYLES)  # 真实注册表必须自洽，否则 import 就该炸
    check_true("真实注册表通过自检", True)

    def registry_error(styles) -> str:
        try:
            validate_styles(styles)
        except ValueError as exc:
            return str(exc)
        return ""

    small = CardStyle(id="ac_temp_1", label="温度", span=1, category="ac")
    wide = CardStyle(id="ac_temp_2", label="温度", span=2, category="ac")
    validate_styles((small, wide))
    check("同名不同尺寸允许共存，菜单靠尺寸前缀区分",
          [s.menu_label for s in (small, wide)],
          ["小卡 1×1：温度", "宽卡 2×1：温度"])
    check_true("同尺寸 + 同名没有可辨差别，拒绝",
               "无法分辨" in registry_error(
                   (wide, CardStyle(id="ac_temp_3", label="温度", span=2))))
    check_true("id 重复会覆盖偏好，拒绝",
               "id 重复" in registry_error(
                   (wide, CardStyle(id="ac_temp_2", label="温度控制"))))
    check("标准样式不属于任何品类（全设备兜底）", STYLES[0].category, None)
    check("标准样式没有内容槽位", STYLES[0].slots, ())

    print("\n2c) 菜单顺序：按尺寸分组，同尺寸保持注册顺序")
    wide_a = CardStyle(id="t_wide_a", label="温度", span=2)
    small_a = CardStyle(id="t_small", label="亮度", span=1)
    wide_b = CardStyle(id="t_wide_b", label="色温", span=2)
    with patch.object(card_templates, "STYLES", (wide_a, small_a, wide_b)):
        order = [s.id for s in card_templates.available_styles(light.card)]
    check("交错注册也会排成 小卡 → 宽卡", order,
          ["t_small", "t_wide_a", "t_wide_b"])
    sensor = make_state("d2", category="sensor_ht", bright=False, temp=False)
    check("非灯只有标准", [s.id for s in available_styles(sensor.card)], [STANDARD])
    no_temp = make_state("d3", temp=False)
    check("只有亮度的灯不列双滑块",
          [s.id for s in available_styles(no_temp.card)], [STANDARD, LIGHT_BRIGHT])
    check("（机械原因）平板灯根本没绑上 color_temp 槽位",
          no_temp.card.control("color_temp"), None)
    check_true("（结果）双滑块自检为不可用",
               style_by_id(LIGHT_DUAL).available(no_temp.card) is False)
    no_range = make_state("d4", ranged=False)
    check("没有 value_range 不给滑条",
          [s.id for s in available_styles(no_range.card)], [STANDARD])

    print("\n2d) 品类默认样式：存在传感器直接显示有人/无人")
    pres = make_presence_state()
    check("状态卡的槽位在 metrics 里（controls 里没有）",
          pres.card.control("occupancy"), None)
    check_true("但样式仍然可用（CardSpec.bound 两处都查）",
               style_by_id(PRESENCE_STATE).available(pres.card))
    check("状态卡声明的是只读 body",
          style_by_id(PRESENCE_STATE).body, BODY_READOUT)
    check("无偏好时默认就是状态卡",
          resolve_card_style(fresh_prefs(tmp, "p0.json"), "p1", pres.card).id,
          PRESENCE_STATE)
    check("菜单第一项是默认（状态），标准作为备选仍然在",
          [s.id for s in available_styles(pres.card)], [PRESENCE_STATE, STANDARD])
    check("灯不受影响，默认仍是标准", default_style(light.card).id, STANDARD)

    pp = fresh_prefs(tmp, "presence.json")
    wid = pres.card.default_style
    # 显式选回产品图必须真的落盘：否则"点标准又回到状态"，菜单像失灵
    check("显式选标准会落盘", pp.set_card_style("p1", STANDARD, default_id=wid), True)
    check("落盘内容就是 standard", pp.card_styles["p1"], STANDARD)
    check("解析结果真是标准", resolve_card_style(pp, "p1", pres.card).id, STANDARD)
    check("点默认项则删记录", pp.set_card_style("p1", PRESENCE_STATE, default_id=wid),
          True)
    check("回默认后不留冗余记录", pp.card_styles, {})

    print("\n2e) 状态卡的 body：大字读数 / 副标题照常 / 离线灰")
    pres_style = resolve_card_style(pp, "p1", pres.card)
    pc = DeviceCard(pres, Icons(), theme.LIGHT, pres_style)
    pc.show()
    app.processEvents()
    check("状态卡是 1×1", pc.width(), theme.CARD_MIN_W)
    check("body 建的是读数控件",
          type(pc._body.controls["occupancy"]).__name__, "CardReadout")
    check("读数取到设备值并翻成中文",
          pc._body.controls["occupancy"]._text, "有人")
    sub = pc._subtitle()
    check("副标题照常列出指标，不因 body 画过就跳过", sub, "有人 · 49%")
    check("换样式不动副标题（body 与副标题互不牵连）",
          DeviceCard(pres, Icons(), theme.LIGHT, style_by_id(STANDARD))._subtitle(), sub)
    check("小字标签是指标名而不是值（无人时不能写成有人）",
          pc._body.controls["occupancy"]._label, "人体状态")
    check("读数不被 any_on 置灰（传感器没有开关态）",
          pc._body.controls["occupancy"].isEnabled(), True)
    pc.refresh(make_presence_state(state=0))
    check("无人 → 大字换成无人", pc._body.controls["occupancy"]._text, "无人")
    off = make_presence_state("p2", online=False)
    oc = DeviceCard(off, Icons(), theme.LIGHT, style_by_id(PRESENCE_STATE))
    oc.show()
    app.processEvents()
    check("离线时读数禁用", oc._body.controls["occupancy"].isEnabled(), False)

    print("\n2f) 空调「温度控制」：大数字步进器 + 模式胶囊")
    ac = make_ac_state()
    ac_style = style_by_id(TEMP_CONTROL)
    check("温度控制是宽卡 2×1", (ac_style.span, ac_style.menu_label),
          (2, "宽卡 2×1：温度控制"))
    check("body 是 controls（按属性类型选控件）", ac_style.body, "controls")
    check_true("真机形状的空调可用", ac_style.available(ac.card))
    check("品类默认与注册表里的 id 是同一个字面量",
          PROFILES["air-conditioner"].default_style, TEMP_CONTROL)
    check("菜单里默认样式排第一",
          [s.id for s in available_styles(ac.card)], [TEMP_CONTROL, STANDARD])
    check("空调的默认样式就是温度控制（品类声明生效）",
          default_style(ac.card).id, TEMP_CONTROL)
    check("没设偏好时直接解析成温度控制",
          resolve_card_style(fresh_prefs(tmp, "ac0.json"), "ac1", ac.card).id,
          TEMP_CONTROL)
    ir = make_ac_state("ac_ir", target=None, modes=(), default="")
    check("红外空调（只有 ir-mode）没有这一项",
          [s.id for s in available_styles(ir.card)], [STANDARD])
    check("红外空调没声明默认，仍是标准", default_style(ir.card).id, STANDARD)
    check("声明了默认但这台能力不够，兜回标准",
          default_style(make_ac_state("ac_ir2", target=None, modes=(),
                                      default=TEMP_CONTROL).card).id, STANDARD)
    check("有温度属性但没有刻度也不给（步进器需要步长）",
          [s.id for s in available_styles(make_ac_state("ac4", ranged=False).card)],
          [STANDARD])

    p_ac = fresh_prefs(tmp, "ac.json")
    p_ac.set_card_style("ac1", TEMP_CONTROL)
    acp = resolve_card_style(p_ac, "ac1", ac.card)
    check("偏好解析成温度控制", acp.id, TEMP_CONTROL)
    acc = DeviceCard(ac, Icons(), theme.LIGHT, acp)
    acc.show()
    app.processEvents()
    check("卡宽 = 2 列", acc.width(), theme.CARD_MIN_W * 2 + theme.CARD_GAP)
    body = acc._body
    check("body 两个控件，顺序跟 slots 一致",
          list(body.controls), ["target_temp", "mode"])
    check("温度建成步进器",
          type(body.controls["target_temp"]).__name__, "CardStepper")
    check("模式建成胶囊", type(body.controls["mode"]).__name__, "CardChips")
    check("温度取到设备值", body.controls["target_temp"].value(), 26)
    check("模式取到设备值", body.controls["mode"].value(), 1)
    check("步进器是居中排布（不是靠左贴右）",
          body.controls["target_temp"]._centered, True)
    tstep = body.controls["target_temp"]
    tr_text, tr_minus, tr_plus = tstep._layout()
    check_true("从左到右是「− 数值 +」（减号在数字左侧）",
               tr_minus.right() < tr_text.left() < tr_text.right() < tr_plus.left(),
               f"−{tr_minus.right()} | {tr_text.left()}~{tr_text.right()} | +{tr_plus.left()}")
    check("数字两侧的间距对称", tr_text.left() - tr_minus.right(),
          tr_plus.left() - tr_text.right())
    check_true("整组水平居中（左右余量相差 ≤ 3，只差取整）",
               abs((tr_minus.left()) - (tstep.width() - tr_plus.right() - 1)) <= 3,
               f"左 {tr_minus.left()} / 右 {tstep.width() - tr_plus.right() - 1}")

    gt = body.controls["target_temp"].geometry()
    gm = body.controls["mode"].geometry()
    check_true("两行都在 body 内",
               gt.left() >= 0 and gt.right() <= body.width()
               and gm.left() >= 0 and gm.right() <= body.width()
               and gm.bottom() <= body.height(),
               f"body={body.width()}×{body.height()}")
    check_true("步进器在上、胶囊在下且不重叠", gt.bottom() < gm.top(),
               f"{gt.bottom()} < {gm.top()}")
    check("两行都铺满 body 宽", (gt.width(), gm.width()),
          (body.width(), body.width()))
    chips = body.controls["mode"]
    widths = {r.width() for r, _, _ in chips._layout()}
    check("胶囊铺满后等宽", len(widths), 1)
    check("胶囊取到当前模式", [v for _, v, _ in chips._layout()],
          [0, 1, 2, 3, 4])
    check("副标题照旧是室温 · 湿度", acc._subtitle(), "24.9°C · 54%")

    sent: list[tuple[int, int, object]] = []
    acc.value_committed.connect(lambda did, siid, piid, v: sent.append((siid, piid, v)))
    minus, plus = body.controls["target_temp"]._buttons()
    QTest.mouseClick(body.controls["target_temp"], Qt.LeftButton, pos=plus.center())
    check("点 + 发 (2,4,27)", sent[-1], (2, 4, 27))
    QTest.mouseClick(body.controls["target_temp"], Qt.LeftButton, pos=minus.center())
    check("点 − 回到 26", sent[-1], (2, 4, 26))
    rect, value, _ = chips._layout()[3]
    QTest.mouseClick(chips, Qt.LeftButton, pos=rect.center())
    check("点「制热」发 (2,2,3)", sent[-1], (2, 2, 3))

    acc.refresh(make_ac_state(target=18, mode=4))
    check("轮询回来温度跟着变", body.controls["target_temp"].value(), 18)
    check("轮询回来模式跟着变", chips.value(), 4)
    dim = DeviceCard(make_ac_state("ac2", on=False), Icons(), theme.LIGHT, ac_style)
    dim.show()
    app.processEvents()
    check("关机时 body 压暗（米家也保留温控）",
          dim._body.controls["target_temp"]._dimmed, True)
    check("关机但在线仍可点", dim._body.controls["target_temp"].isEnabled(), True)
    ofl = DeviceCard(make_ac_state("ac3", online=False), Icons(), theme.LIGHT, ac_style)
    ofl.show()
    app.processEvents()
    check("离线时步进器禁用", ofl._body.controls["target_temp"].isEnabled(), False)
    check("离线时胶囊禁用", ofl._body.controls["mode"].isEnabled(), False)

    print("\n2g) 判重：跨品类同名放行，同框撞名拒绝")
    a = CardStyle(id="x_light", label="亮度", span=2, category="light")
    b = CardStyle(id="x_heater", label="亮度", span=2, category="bath-heater")
    validate_styles((a, b))
    check_true("不同品类的同尺寸同名放行（永不同框）", True)
    for desc, other in (
        ("同品类同尺寸同名被拒",
         CardStyle(id="x_light2", label="亮度", span=2, category="light")),
        ("通用样式与品类样式同名也拒（会同时可见）",
         CardStyle(id="x_common", label="亮度", span=2)),
    ):
        try:
            validate_styles((a, other))
            hit = False
        except ValueError:
            hit = True
        check_true(desc, hit)

    try:
        CardStyle(id="bad_body", label="x", body="hologram")
        bad_ok = False
    except ValueError:
        bad_ok = True
    check_true("未注册的 body 类型在声明期报错", bad_ok)
    validate_styles(STYLES)
    check_true("正式注册表通过自检", True)
    ghost = make_presence_state("p3", default_style="not_registered")
    check("未知 default_style 兜底标准，不崩", default_style(ghost.card).id, STANDARD)

    p.set_card_style("d1", LIGHT_DUAL)
    s = resolve_card_style(p, "d1", light.card)
    check("双滑块样式是 2×1", s.span, 2)
    check("不再有 auto_widened 影子状态", hasattr(s, "auto_widened"), False)
    check("body 标志", s.has_body, True)
    p.set_card_style("d1", STANDARD)
    s2 = resolve_card_style(p, "d1", light.card)
    check("切回标准就是 1×1", s2.span, 1)
    check("标准样式没有 body", s2.has_body, False)
    p.set_card_style("d1", "not_registered")
    check("未知 id 退回标准", resolve_card_style(p, "d1", light.card).id, STANDARD)
    p.set_card_style("d1", LIGHT_DUAL)
    check("设备缺属性时退回标准并缩回 1×1",
          (resolve_card_style(p, "d1", sensor.card).id,
           resolve_card_style(p, "d1", sensor.card).span), (STANDARD, 1))
    wide_spec = make_state("d7")
    wide_spec.card.span = 2  # 老品类声明里的 span 不该再影响宽度
    check("品类声明 span=2 也不再撑宽默认卡",
          resolve_card_style(fresh_prefs(tmp, "empty.json"), "d7",
                             wide_spec.card).span, 1)

    print("\n3) 几何：等高 / 跨列公式 / body 落在内容带")
    style = resolve_card_style(p, "d1", light.card)
    card = DeviceCard(light, Icons(), theme.LIGHT, style)
    card.show()
    app.processEvents()
    want_w = theme.CARD_MIN_W * 2 + theme.CARD_GAP
    check("2×1 宽度 = 2×单列 + 间距", card.width(), want_w)
    check("所有卡片等高", card.height(), theme.CARD_H)
    check("sizeHint 是稳定的设计尺寸", card.sizeHint().width(), want_w)
    check("body 严格落在 _body_rect", card._body.geometry(), card._body_rect())
    check("两条滑条", list(card._body.controls), ["brightness", "color_temp"])
    check("亮度单色轨道", card._body.controls["brightness"]._gradient, False)
    check("色温渐变轨道", card._body.controls["color_temp"]._gradient, True)
    check("亮度取到设备值", card._body.controls["brightness"].value(), 60)
    check("色温取到设备值", card._body.controls["color_temp"].value(), 4000)
    check("滑条行高", card._body.controls["brightness"].height(), ROW_H)
    tall = card._body.controls["brightness"].geometry()
    check("首行从内容带起", tall.y(), 1)  # 78 高的带里，76 高的两行上下各 1px
    check("内容带内没有产品图", card.style.has_body, True)

    std = DeviceCard(light, Icons(), theme.LIGHT)
    std.show()
    app.processEvents()
    check("不传 style 保持原状 1×1", std.width(), theme.CARD_MIN_W)
    check("不传 style 没有 body 子控件", std._body, None)

    print("\n4) 像素：亮度填充 / 色温冷暖方向")
    brightness = card._body.controls["brightness"]
    color_temp = card._body.controls["color_temp"]
    bimg = brightness.grab().toImage()
    cimg = color_temp.grab().toImage()
    x0 = 48  # LABEL_W + 8
    x1 = color_temp.width() - 56  # -VALUE_W - 8
    cy = ROW_H / 2
    got = pixel(bimg, x0 + 10, cy)
    accent = QColor(theme.category_color("light"))
    check_true("亮度填充是灯品类色", diff(got, accent) <= 24,
               f"{got.name()} vs {accent.name()}")
    warm = pixel(cimg, x0 + 8, cy)
    cool = pixel(cimg, x1 - 8, cy)
    check_true("色温暖端偏红", warm.red() > warm.blue() + 40, warm.name())
    check_true("色温冷端偏蓝", cool.blue() > cool.red(), cool.name())
    check_true("色温轨道两端不同色", diff(warm, cool) > 40,
               f"{warm.name()} → {cool.name()}")

    print("\n5) 交互：提交信号 / 防回弹 / 关灯置灰 / 离线禁用 / 主题")
    got_events: list[tuple] = []
    card.value_committed.connect(lambda *a: got_events.append(a))
    brightness.committed.emit(77)
    check("松手才发一次，且带 siid/piid", got_events, [("d1", 2, 2, 77)])
    card._body.controls["color_temp"].committed.emit(5000)
    check("色温提交", got_events[-1], ("d1", 2, 3, 5000))
    brightness._dragging = True
    light.values[(2, 2)] = 5
    card.refresh(light)
    check("拖动中不接外部赋值", brightness.value(), 60)
    brightness._dragging = False
    card.refresh(light)
    check("松手后恢复同步", brightness.value(), 5)
    menu_events: list[tuple] = []
    card.menu_requested.connect(lambda *a: menu_events.append(a))
    card._body.menu_requested.emit(QPoint(30, 40))
    check("在滑条上右键也转发给卡片", menu_events, [("d1", QPoint(30, 40))])

    off = make_state("d5", on=False)
    c_off = DeviceCard(off, Icons(), theme.LIGHT, style)
    check("关灯时滑条置灰", c_off._body.controls["brightness"]._dimmed, True)
    offline = make_state("d6", online=False)
    c_offline = DeviceCard(offline, Icons(), theme.LIGHT, style)
    check("离线时滑条不可用（isEnabled=False）",
          c_offline._body.controls["brightness"].isEnabled(), False)
    card.set_palette(theme.DARK)
    check("切深色同步到滑条", card._body.controls["brightness"]._p, theme.DARK)

    print("\n6) 菜单与接线：子菜单透明 / 自绘勾 / 局部刷新")
    states = [light, make_state("d2", name="餐厅灯")]
    store = FakeStore(p, states)
    wall = WallView(store, Icons(), theme.LIGHT)
    wall.resize(900, 700)  # 离屏窗口不会自动撑到桌面宽；给足列数
    wall.build()
    wall.show()
    app.processEvents()

    def menu_for(data: object):
        """拦掉 exec（offscreen 里它会一直等），把选中的 action 直接交回去。"""
        seen: dict[str, Menu] = {}

        def fake_exec(menu_self, pos, *args, **kwargs):
            seen["root"] = menu_self
            for act in menu_self.actions():
                sub = act.menu()
                if sub is None:
                    continue
                for a in sub.actions():
                    if a.data() == data:
                        return a
            return None

        with patch.object(Menu, "exec", fake_exec):
            wall._on_card_menu("d1", QPoint(10, 10))
        return seen["root"]

    root = menu_for(("style", LIGHT_BRIGHT))
    titles = [a.text() for a in root.actions()]
    check("菜单只有详情 / 卡片样式 / 隐藏（无大小、无恢复默认）", titles,
          ["打开详情", "", "卡片样式", "", "隐藏设备"])
    subs = [a.menu() for a in root.actions() if a.menu() is not None]
    check("只有一层子菜单", len(subs), 1)
    style_menu = subs[0]
    check("子菜单标题", next(a for a in root.actions() if a.menu()).text(), "卡片样式")
    check_true("样式子菜单是透明窗口",
               isinstance(style_menu, Menu)
               and style_menu.testAttribute(Qt.WA_TranslucentBackground))
    check_true("样式子菜单无边框",
               bool(style_menu.windowFlags() & Qt.FramelessWindowHint))
    check("样式只列这台灯能用的",
          [a.data() for a in style_menu.actions() if isinstance(a.data(), tuple)],
          [("style", STANDARD), ("style", LIGHT_DUAL),
           ("style", LIGHT_BRIGHT)])
    check("每项尺寸在前、冒号后跟内容",
          [a.text() for a in style_menu.actions() if isinstance(a.data(), tuple)],
          ["小卡 1×1：标准", "宽卡 2×1：亮度 + 色温", "宽卡 2×1：亮度"])
    wide = [a.text() for a in style_menu.actions()
            if isinstance(a.data(), tuple) and a.text().startswith("宽卡 2×1：")]
    check("同尺寸的项前缀一致（纵向好扫）", len(wide), 2)
    checked = next(a for a in style_menu.actions()
                   if a.data() == ("style", LIGHT_DUAL))
    unchecked = next(a for a in style_menu.actions()
                     if a.data() == ("style", STANDARD))
    check_true("当前项有自绘勾", icon_alpha(checked) > 100,
               f"alpha={icon_alpha(checked)}")
    check_true("未选项是同尺寸透明占位", icon_alpha(unchecked) == 0,
               f"alpha={icon_alpha(unchecked)}")

    picks: list[tuple] = []
    wall.card_style_requested.connect(lambda *a: picks.append(a))
    menu_for(("style", LIGHT_BRIGHT))
    menu_for(("style", STANDARD))
    check("点样式发出样式请求（含默认）", picks,
          [("d1", LIGHT_BRIGHT), ("d1", STANDARD)])

    values: list[tuple] = []
    wall.card_value_committed.connect(lambda *a: values.append(a))
    wall._cards["d1"]._body.controls["brightness"].committed.emit(88)
    check("卡片提交转发到墙", values, [("d1", 2, 2, 88)])

    before = wall._cards["d1"]
    section = wall._sections[0]
    layout_now(section)
    first_x = section.flow.itemAt(1).geometry().x()
    check("宽卡时第二张卡排在 340+8 处", first_x, want_w + 8)
    p.set_card_style("d1", STANDARD)
    wall.apply_card_style("d1")
    app.processEvents()
    check("局部刷新复用卡片对象", wall._cards["d1"] is before, True)
    check("切回标准宽度", before.width(), theme.CARD_MIN_W)
    check("切回标准后 body 销毁", before._body, None)
    second_x = section.flow.itemAt(1).geometry().x()
    check("局部刷新自己就会重排 FlowLayout", second_x, theme.CARD_MIN_W + 8)
    check_true("第二张卡跟着左移", second_x < first_x, f"{first_x} → {second_x}")

    p.set_card_style("d1", LIGHT_DUAL)
    wall.apply_card_style("d1")
    app.processEvents()
    check("再切回双滑块自动变宽", wall._cards["d1"].width(), want_w)
    check("再切回 body 重建", len(wall._cards["d1"]._body.controls), 2)

    wall.set_palette(theme.DARK)
    check("切主题同步到墙上滑条",
          wall._cards["d1"]._body.controls["color_temp"]._p, theme.DARK)

    # -- 主窗口点卡片 → 详情页的接口契约 -------------------------------
    # 这条路径没有别的离屏脚本覆盖：曾经主窗口调了 WallView 上不存在的
    # ``hand_off_active_to``，结果点任何设备都 AttributeError 崩在终端里，
    # 而各模块自己的测试全绿。这里把「主窗口依赖的名字」钉住，少一个就红。
    print("\n7) 主窗口 → 详情页：摄像头交接接口都在")
    from mihome.ui.detail import DetailDialog  # noqa: PLC0415 — 只有这一段用

    for name in ("stop_playback", "hand_off_active_to"):
        check_true(f"WallView 有 {name}（主窗口直接调它）",
                   callable(getattr(wall, name, None)))
    # 墙上没在播时主窗口要能安全拿到 False，再退回"先停再开"
    check("没在播时交接返回 False", wall.hand_off_active_to(None, "d1"), False)
    check_true("详情页有 camera_view 属性（交接目标）",
               isinstance(getattr(DetailDialog, "camera_view", None), property))
    check_true("详情页有 adopt_playing_camera（接管后挂上播放器）",
               callable(getattr(DetailDialog, "adopt_playing_camera", None)))
    # 真造一个详情页走一遍主窗口那两步：先问 camera_view，再调 adopt。
    # 名字对得上但构造就崩，同样是"点设备就废"，所以这一步不能省。
    dlg = DetailDialog(light, LooseStore(p, [light]), theme.LIGHT, None, icons=Icons())
    check("灯没有画面控件（camera_view 是 None）", dlg.camera_view, None)
    check("没有画面控件时 adopt 返回 False，不抛异常",
          dlg.adopt_playing_camera(), False)
    check("非摄像头设备不会生成模式行（挂灯才走型号适配）", dlg._mode_row, None)
    dlg.deleteLater()

    # -- 卡片底色：浅色模式下必须和背景分得开 -----------------------------
    # 浅色背景 #F1F2F4 只比白卡暗 13，混一层 10% 的品类色就只剩 1–3；
    # 电视 / 摄像头这类深灰品类混完比背景**还暗**（旧版 -2.2），卡片彻底糊进背景。
    # theme.tint 的 min_gap 就是干这个的，这里把"最差的品类"钉住。
    print("\n8) 卡片底色：浅色下任何品类都要和背景分得开")
    gaps = {
        cat: theme.luma(theme.tint(col, 0.10, theme.LIGHT, min_gap=6))
        - theme.luma(theme.LIGHT.bg)
        for cat, col in theme.CATEGORY_COLOR.items()
    }
    worst_cat = min(gaps, key=gaps.__getitem__)
    # 5.9 而不是 6：blend 是逐通道取整，解出来的强度落下去会有 ±0.5 的误差
    check_true("最差品类也 ≥ 6 个亮度级（取整误差内）", gaps[worst_cat] >= 5.5,
               f"最差 {worst_cat} {gaps[worst_cat]:+.1f}")
    check_true("没有品类被压成纯白（染色还在）",
               all(theme.tint(c, 0.10, theme.LIGHT, min_gap=6) != "#FFFFFF"
                   for c in theme.CATEGORY_COLOR.values()))
    # 深色模式的差本来就有 20+，不该被这条约束动到
    check_true("深色模式的混色结果不受 min_gap 影响",
               all(theme.tint(c, 0.10, theme.DARK, min_gap=6) == theme.tint(c, 0.10, theme.DARK)
                   for c in theme.CATEGORY_COLOR.values()))

    print("\n9) 挂灯模式：只给 yeelink.light.lamp22 开「无激活项」")
    # 型号级适配（mihome/core/adapters/）：挂灯的 mode(2,5) 只用于写入，
    # 手调亮度/色温后它不会变；真正生效的预设要从只读的 scene-mode(3,9) 读，
    # 0 = 无激活项。**别的灯不能套**——那是这台设备实测出来的行为，套错了会
    # 把别人的选中态弄坏。所以整件事只动"型号 → 属性"这一张表，通用逻辑零改动。
    check("适配表只有挂灯一条（没扩散到别的型号）",
          sorted(catalog.MODEL_ADAPTERS), ["yeelink.light.lamp22"])
    # 表在 adapters 包里，catalog 只是转出；两边必须是同一对象，不然查表会各查各的
    check_true("catalog.MODEL_ADAPTERS 就是 adapters 里那张表",
               catalog.MODEL_ADAPTERS is adapters.MODEL_ADAPTERS)
    check("每条适配都写清了 note", [m for m, a in adapters.MODEL_ADAPTERS.items()
                                  if not a.note.strip()], [])
    check("find_adapter 认已知型号", adapters.find_adapter("yeelink.light.lamp22").mode_active,
          "scene-mode")
    check("find_adapter 对未登记型号返回 None",
          [adapters.find_adapter("dmaker.fan.p5"), adapters.find_adapter(None),
           adapters.find_adapter("")], [None, None, None])
    # 登记期的两道闸：写错型号格式、同一型号写两遍，都要当场报错
    bad_models = []
    for wrong in ("light", "yeelink.light.lamp22 ", "", "yeelink.light"):
        try:
            adapters.check_model(wrong)
        except ValueError:
            bad_models.append(wrong)
    check("格式不对的型号键会被拒（品类名/空格/空/段数不足）", bad_models,
          ["light", "yeelink.light.lamp22 ", "", "yeelink.light"])
    dup = (("yeelink", {"yeelink.light.lamp22": adapters.ModelAdapter()}),
           ("yeelink", {"yeelink.light.lamp22": adapters.ModelAdapter()}))
    try:
        adapters.collect(dup)
        dup_raised = False
    except ValueError as exc:
        dup_raised = "登记了两次" in str(exc)
    check_true("重复登记同一型号会被拒", dup_raised)
    spec = parse_raw(LAMP_RAW)
    lamp_card = catalog.build_card(spec, model="yeelink.light.lamp22")
    check("挂灯：scene-mode 被认成模式判据", lamp_card.mode_active.key, (3, 9))
    check("挂灯：scene-mode 进了轮询集合（extra）",
          [p.name for p in lamp_card.extra], ["scene-mode"])
    # extra 不在 _card_props 里的话，界面拿到了属性也读不到值
    check("挂灯：scene-mode 真的在轮询 / 寻址集合里",
          (3, 9) in [k for k, _ in _card_props(lamp_card)], True)
    check("同规格换个型号就完全不适配",
          catalog.build_card(spec, model="yeelink.light.ceiling1").mode_active, None)
    check("不传型号 = 不做型号适配",
          catalog.build_card(spec).mode_active, None)

    # 详情页：用客户端占位表示"无激活项"，不往枚举里塞假选项——假选项既能被
    # 写出去（设备不认的值），也会被当成枚举值回读。
    lamp = make_lamp_state(scene=7)
    store_l = LooseStore(p, [lamp])
    dlg_l = DetailDialog(lamp, store_l, theme.LIGHT, None, icons=Icons())
    box = dlg_l._mode_row.editor
    dlg_l._sync()  # 构造函数不自己刷值，轮询定时器才会；这里先手动走一次
    check("模式行是下拉框", isinstance(box, QComboBox), True)
    check("枚举项就是设备给的 5–9（没有伪造的「无」）",
          [box.itemData(i) for i in range(box.count())], [5, 6, 7, 8, 9])
    check("scene=7 → 选中「影院」", (box.currentIndex(), box.currentText()), (2, "影院"))

    lamp.values[(3, 9)] = 0
    dlg_l._sync()
    check("scene=0（无激活项）→ 不选中任何真实项", box.currentIndex(), -1)
    check("…同时在框里显示客户端文案「无」", box.placeholderText(), "无")

    lamp.values[(3, 9)] = 3
    dlg_l._sync()
    check("自定义场景 3（不在枚举里）→ 显示「其他」",
          (box.currentIndex(), box.placeholderText()), (-1, "其他"))

    lamp.values[(3, 9)] = 6
    dlg_l._sync()
    check("回到正常值 → 占位文案复位，不留上一轮的「其他」",
          (box.currentIndex(), box.placeholderText()), (1, "—"))

    lamp.values.pop((3, 9))
    lamp.values[(2, 5)] = 8
    dlg_l._sync()
    check("scene 还没读到 / 不合法 → 退回按 mode(2,5) 高亮",
          (box.currentIndex(), box.currentText()), (3, "暖光"))

    # 占位态下用户点一项：写出去的必须是真实枚举值，而不是「无」
    lamp.values[(3, 9)] = 0
    dlg_l._mode_pending = False
    dlg_l._sync()
    check("回到「无」占位态", (box.currentIndex(), box.placeholderText()), (-1, "无"))
    store_l.writes.clear()
    box.setCurrentIndex(2)
    check("占位态下选「影院」→ 只写 (2,5,7)",
          store_l.writes, [(lamp.did, (2, 5), 7)])
    check("选完立刻高亮所选（不等设备回读）", box.currentIndex(), 2)

    # 刚点完模式、设备还没把 scene 改过来：这一段不能被 0 擦掉，否则下拉会在
    # 一两百毫秒内先跳回「无」再跳回所选模式，闪两下。``_on_change`` 会置位。
    check("鼠标点选后进入 pending 保护", dlg_l._mode_pending, True)
    lamp.values[(3, 9)] = 0
    dlg_l._sync()
    check("pending + scene=0 → 保留本地选择，不显示「无」",
          (box.currentIndex(), box.currentText()), (2, "影院"))
    dlg_l.deleteLater()

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
