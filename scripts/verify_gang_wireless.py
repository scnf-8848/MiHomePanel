"""多键面板「转无线」的验证：规格识别、卡片改成不可点、详情页行锁住。

    .venv/Scripts/python.exe -X utf8 scripts/verify_gang_wireless.py

背景（`docs/dev/ui.md`「多键面板：转无线」）：米家设置里可以把某一路设成
"转无线"——继电器常通（给智能灯一直供电），物理按键不再切换。规格里表现为
``switch`` 服务的 ``mode`` 属性（0 有线 / 1 仅无线），**逐路独立**：实测一块
三开单控只有左键是 1。这一路如果还能在面板上点，轻则点了没反应，重则把给灯
供电的那路断掉。

整套用离线规格，不碰云端；``--live`` 才读一次真设备（只读 prop/get）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mihome.core import catalog, store as store_mod, tree  # noqa: E402
from mihome.core.icons import IconCache  # noqa: E402
from mihome.core.spec import parse_raw  # noqa: E402
from mihome.ui import theme  # noqa: E402
from mihome.ui.device_card import card_for  # noqa: E402
from mihome.ui.detail import ControlRow, DetailDialog  # noqa: E402
from mihome.ui.widgets import IconProvider  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {name}{('  — ' + detail) if detail else ''}")


# ---------------------------------------------------------------------------
# 造规格
# ---------------------------------------------------------------------------

WIRED_WIRELESS = ((0, "有线和无线开关"), (1, "无线开关"))
WIRED_ONLY = ((0, "有线开关"), (1, "无线开关"))
ENGLISH = ((0, "Wired and wireless switch"), (1, "Wireless switch"))
USB_MODE = ((0, "均衡分配"), (1, "C1高效模式"), (2, "C2高效模式"), (3, "NONE"))
AMBIGUOUS = ((0, "无线"), (1, "无线开关"))
SENSOR_MODE = ((0, "极速单击模式"), (1, "多功能模式"))


def _prop(iid: int, name: str, fmt: str, access: list[str], **kw) -> dict:
    out = {
        "iid": iid,
        "type": f"urn:miot-spec-v2:property:{name}:00000000:test-sw:1:0000A003",
        "description": name,
        "format": fmt,
        "access": access,
        "comment": kw.get("comment", name),
    }
    if "values" in kw:
        out["value-list"] = [
            {"value": v, "description": c, "comment": c} for v, c in kw["values"]
        ]
    return out


def make_spec(gangs: list[tuple | None], sensor_mode: tuple | None = None):
    """``gangs``: 每一路的 mode 值列表；``None`` = 这一路没有 mode 属性。

    ``sensor_mode``: 顺手把 switch-sensor 的 mode（按键手势模式，同名但不相干）
    也塞进去——真实规格里它就长这样，用来保证"只看同服务的 mode"。
    """
    services: list[dict] = [
        {
            "iid": 1,
            "type": "urn:miot-spec-v2:service:device-information:00007801:test-sw:1:0000A003",
            "description": "Device Information",
            "properties": [_prop(1, "manufacturer", "string", ["read"])],
        }
    ]
    for i, values in enumerate(gangs):
        siid = 2 + i
        props = [_prop(1, "on", "bool", ["read", "write", "notify"], comment="开关")]
        if values is not None:
            props.append(
                _prop(2, "mode", "uint8", ["read", "write", "notify"],
                      comment="模式", values=values)
            )
        services.append({
            "iid": siid,
            "type": "urn:miot-spec-v2:service:switch:0000780A:test-sw:1:0000A003",
            "description": "Switch",
            "comment": ["左键", "中键", "右键"][i] if len(gangs) <= 3 else f"开关{i + 1}",
            "properties": props,
        })
    if sensor_mode is not None:
        services.append({
            "iid": 20,
            "type": "urn:miot-spec-v2:service:switch-sensor:0000780F:test-sw:1:0000A003",
            "description": "Switch Sensor",
            "comment": "按键",
            "properties": [
                _prop(1, "mode", "uint8", ["read", "write", "notify"],
                      comment="模式", values=sensor_mode)
            ],
        })
    raw = {
        "type": "urn:miot-spec-v2:device:switch:0000A003:test-sw:1:0000A003",
        "description": "test switch",
        "services": services,
    }
    return parse_raw(raw)


def make_state(spec, values: dict[str, object], did: str = "did-test", name: str = "三开"):
    card = catalog.build_card(spec, did=did, model="test.switch.sw3")
    node = tree.DeviceNode(
        did=did, name=name, model="test.switch.sw3", spec_type=spec.urn,
        home="1103", room="强", online=True,
    )
    st = store_mod.DeviceState(node=node, spec=spec, card=card)
    keys = {}
    for key, prop in store_mod._card_props(card):
        keys[key] = store_mod.PropRef(did, prop.siid, prop.piid)
    st.keys = keys
    refs: dict[str, tuple[int, int]] = {}
    for i, g in enumerate(card.gangs, start=1):
        refs[f"g{i}"] = g.power.key
        if g.mode is not None:
            refs[f"m{i}"] = g.mode.key
    for k, v in values.items():
        st.values[refs[k]] = v
    return st, refs


class FakeStore:
    """详情页只要 ``devices`` 和 ``prefs.is_hidden``。"""

    def __init__(self, st) -> None:
        self.devices = {st.did: st}
        self.prefs = _FakePrefs()
        self.client = None


class _FakePrefs:
    def is_hidden(self, did: str) -> bool:
        return False


def live_report() -> int:
    """只读地看一眼真设备：每台开关的每一路现在是不是转无线。

    不发任何写请求（只有 prop/get），用来核对"界面判据和云端实际值对得上"。
    """
    from mihome.core.api import Client, PropRef
    from mihome.core.spec import SpecRepository

    cli = Client()
    topo = cli.load_topology()
    repo = SpecRepository()
    specs = repo.get_many([d.spec_type for d in topo.real_devices()], workers=4)
    total = wireless = 0
    print("\n== 真机（只读）==")
    for d in topo.real_devices():
        spec = specs.get(d.spec_type)
        if spec is None:
            continue
        card = catalog.build_card(spec, model=d.model, gang_names=d.gang_names)
        gangs = [g for g in card.gangs if g.mode is not None]
        if not gangs:
            continue
        refs = [PropRef(d.did, g.mode.siid, g.mode.piid) for g in gangs]
        refs += [PropRef(d.did, g.power.siid, g.power.piid) for g in gangs]
        got = cli.read(refs)
        values = {
            g.siid: got[PropRef(d.did, g.mode.siid, g.mode.piid)].value
            for g in gangs
            if PropRef(d.did, g.mode.siid, g.mode.piid) in got
        }
        parts = []
        for g in gangs:
            v = values.get(g.siid)
            mark = "转无线" if v == g.wireless_value else "有线"
            parts.append(f"{g.label}={v}({mark})")
        total += len(gangs)
        wireless += sum(1 for g in gangs if values.get(g.siid) == g.wireless_value)
        print(f"  [{d.room}] {d.name}（{d.model}）：{'  '.join(parts)}")
    print(f"\n共 {total} 路，其中 {wireless} 路转无线")
    ok = total > 0 and wireless > 0
    check("真机上确实能读出转无线的路", ok, f"{wireless}/{total}")
    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    return 0 if not FAIL else 1


def main() -> int:
    app = QApplication.instance() or QApplication([])
    for w in app.topLevelWidgets():
        w.hide()

    print("\n== 规格识别 ==")
    spec = make_spec([WIRED_ONLY, WIRED_ONLY, WIRED_ONLY], sensor_mode=SENSOR_MODE)
    card = catalog.build_card(spec, model="test.switch.sw3")
    check("三开识别出 3 路", len(card.gangs) == 3, str(len(card.gangs)))
    check("每一路都绑到 mode(2 号属性)",
          all(g.mode is not None and g.mode.piid == 2 for g in card.gangs))
    check("mode 不会被 switch-sensor 的同名属性顶掉",
          all(g.mode.service == "switch" for g in card.gangs),
          str({g.mode.service for g in card.gangs}))
    check("wireless_value = 1（不含'有线和无线'那项 0）",
          all(g.wireless_value == 1 for g in card.gangs),
          str([g.wireless_value for g in card.gangs]))
    check("switch-sensor 的 mode 不进 gang",
          all(g.mode.siid != 20 for g in card.gangs))

    check("英文字面也能认出（Wireless switch）",
          catalog.wireless_value(parse_raw(_mode_only(ENGLISH)).find("mode")) == 1)
    check("歧义值列表（两项都含'无线'）→ 不认",
          catalog.wireless_value(parse_raw(_mode_only(AMBIGUOUS)).find("mode")) is None)
    check("无关值列表（USB 功率分配）→ 不认",
          catalog.wireless_value(parse_raw(_mode_only(USB_MODE)).find("mode")) is None)
    none_spec = make_spec([None])
    check("没有 mode 属性 → 不认（老面板/插座）",
          catalog.build_card(none_spec).gangs[0].wireless_value is None
          and catalog.build_card(none_spec).gangs[0].mode is None)

    check("每路 mode 不再出现在「更多功能」里",
          all(g.mode.key not in {p.key for p in card.advanced} for g in card.gangs if g.mode))
    d = card.to_dict()
    check("to_dict 带出 mode 与 wireless_value",
          d["gangs"][0]["mode"] == "2.2" and d["gangs"][0]["wireless_value"] == 1,
          str(d["gangs"][0]))

    print("\n== 当前是不是转无线（按设备上报值）==")
    st, keys = make_state(spec, {"g1": True, "m1": 1, "g2": False, "m2": 0, "g3": False, "m3": 0})
    g1, g2, g3 = st.card.gangs
    check("左键 mode=1 → 转无线", g1.is_wireless(st.values))
    check("中键 mode=0 → 不是", not g2.is_wireless(st.values))
    check("mode 还没读到时不当成转无线",
          not g3.is_wireless({}) and not g1.is_wireless({}))

    print("\n== 轮询与统计 ==")
    polled = {k for k, _ in store_mod._card_props(st.card)}
    check("每一路的 mode 参与轮询",
          all((g.mode.key in polled) for g in st.card.gangs))
    check("转无线那一路不算'已开启'（power）", st.power is False, str(st.power))
    check("any_on 也跳过无线路", st.any_on is False, str(st.any_on))
    st.values[keys["g1"]] = False
    st.values[keys["g2"]] = True
    check("有线路开着才算开启", st.any_on is True and st.power is True)

    # 批量关灯：转无线那一路不该被写
    st2, _ = make_state(spec, {"g1": True, "m1": 1, "g2": True, "m2": 0, "g3": True, "m3": 0})
    fake = store_mod.Store.__new__(store_mod.Store)
    fake.devices = {st2.did: st2}
    captured: list[tuple[str, tuple[int, int]]] = []
    fake.set_many = lambda targets, value, mute=2.0: captured.extend(targets) or 0  # type: ignore[method-assign]
    fake.set_power_for_devices([st2.did], False)
    check("批量关灯不下发无线路（发 2 条：中键+右键）",
          len(captured) == 2 and all(k[0] in (3, 4) for _did, k in captured),
          str(captured))

    print("\n== 卡片：副标题与点击 ==")
    mixed, _ = make_state(spec, {"g1": True, "m1": 1, "g2": False, "m2": 0, "g3": True, "m3": 0})
    icons = IconProvider(IconCache(None))
    pal = theme.DARK
    c = card_for(mixed, icons, pal)
    c.resize(166, 168)
    check("副标题：3 路 · 1 开 · 1 无线",
          c._subtitle() == "3 路 · 1 开 · 1 无线", c._subtitle())
    zero, _ = make_state(spec, {"g1": True, "m1": 1, "g2": False, "m2": 0, "g3": False, "m3": 0})
    cz = card_for(zero, icons, pal)
    cz.resize(166, 168)
    check("一路都没开时写「全关」而不是「0 开」",
          cz._subtitle() == "3 路 · 全关 · 1 无线", cz._subtitle())

    all_wireless, _ = make_state(spec, {"g1": True, "m1": 1, "g2": True, "m2": 1, "g3": True, "m3": 1})
    c2 = card_for(all_wireless, icons, pal)
    c2.resize(166, 168)
    check("全部转无线 → 3 路 · 全部转无线",
          c2._subtitle() == "3 路 · 全部转无线", c2._subtitle())

    wired_only, _ = make_state(spec, {"g1": True, "m1": 0, "g2": False, "m2": 0, "g3": False, "m3": 0})
    c3 = card_for(wired_only, icons, pal)
    c3.resize(166, 168)
    check("全部有线时不改原样式（3 路 · 1 路开启）",
          c3._subtitle() == "3 路 · 1 路开启", c3._subtitle())

    fired: list[tuple] = []
    c.toggled.connect(lambda *a: fired.append(a))
    c.resize(166, 168)
    rects = c._gang_rects()
    # 左键是转无线的，中键/右键是有线的
    QTest.mouseClick(c, Qt.LeftButton, Qt.NoModifier, rects[0].center().toPoint())
    check("点转无线的左键：不发写请求", fired == [], str(fired))
    QTest.mouseClick(c, Qt.LeftButton, Qt.NoModifier, rects[2].center().toPoint())
    check("点有线的右键：正常发一条", len(fired) == 1 and fired[0][1:] == (4, 1), str(fired))
    check("按住无线路也不算拖动热点（_hit 仍命中，免得变成点开详情）",
          c._hit(rects[0].center().toPoint()) == ("gang", 2),
          str(c._hit(rects[0].center().toPoint())))

    out_dir = ROOT / "scripts" / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    png = out_dir / "gang_wireless.png"
    c.grab().save(str(png))
    check("抓图落盘（人工看虚线 + 无线标记）", png.is_file(), str(png.relative_to(ROOT)))

    print("\n== 详情页：行锁住 ==")
    detail = DetailDialog(mixed, FakeStore(mixed), pal, icons=icons)
    check("详情页建了 3 行", len(detail._gang_rows) == 3, str(len(detail._gang_rows)))
    row1 = detail._gang_rows[2]
    check("无线路的行名带（转无线）", "转无线" in row1.name.text(), row1.name.text())
    check("无线路的开关不可操作", row1.editor is not None and not row1.editor.isEnabled())
    row3 = detail._gang_rows[4]
    check("有线路仍可操作", row3.editor is not None and row3.editor.isEnabled())
    check("有线路行名不带后缀", "转无线" not in row3.name.text(), row3.name.text())

    # 设备侧改成有线（轮询读到 mode=0）→ 这一行要能自己解锁
    mixed.values[(2, 2)] = 0
    detail._sync()
    check("mode 改回 0 后该行自动解锁", row1.editor is not None and row1.editor.isEnabled())
    check("解锁后行名去掉（转无线）", "转无线" not in row1.name.text(), row1.name.text())
    mixed.values[(2, 2)] = 1
    detail._sync()
    check("mode 又变回 1 时重新锁住", not row1.editor.isEnabled())
    detail.deleteLater()

    print("\n== ControlRow 的 locked 语义 ==")
    row = ControlRow(spec.services[1].properties[0], "左键", pal, "#FF6900", "switch")
    check("默认在线可操作", row.editor.isEnabled())
    row.set_enabled(True, locked=True)
    check("locked=True 时不可操作", not row.editor.isEnabled())
    row.set_enabled(True)
    check("locked=False 恢复可操作", row.editor.isEnabled())
    row.set_enabled(False)
    check("离线仍然不可操作", not row.editor.isEnabled())

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for name in FAIL:
            print(f"  - {name}")
        return 1
    return 0


def _mode_only(values) -> dict:
    """只用来测 wireless_value 的迷你规格（一个 switch 服务 + mode）。"""
    return {
        "type": "urn:miot-spec-v2:device:switch:0000A003:m:1:0000A003",
        "description": "m",
        "services": [
            {
                "iid": 2,
                "type": "urn:miot-spec-v2:service:switch:0000780A:m:1:0000A003",
                "description": "Switch",
                "properties": [
                    _prop(1, "on", "bool", ["read", "write"]),
                    _prop(2, "mode", "uint8", ["read", "notify"], values=values),
                ],
            }
        ],
    }


if __name__ == "__main__":
    if "--live" in sys.argv[1:]:
        raise SystemExit(live_report())
    raise SystemExit(main())
