"""多键面板「隐藏转无线」样式的验证。

    .venv/Scripts/python.exe -X utf8 scripts/verify_switch_wired_style.py

样式要回答两件事：① 只画"按下去真的会切继电器"的那几路，按键按**剩余数量**重排
（3 键里 1 路转无线 → 两键布局）；② 一路都不剩时退回产品图（单开整块转无线）。

合成规格与 `Gang` 取值直接复用 ``verify_gang_wireless.py`` 的助手：两个脚本测的是
同一套判据的上下游（谁能识别转无线 / 识别出来之后怎么摆），各写一份造规格的代码
只会让它们慢慢漂开。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import verify_gang_wireless as V  # noqa: E402
from mihome.core import catalog  # noqa: E402
from mihome.core.icons import IconCache  # noqa: E402
from mihome.ui import card_templates as T, theme  # noqa: E402
from mihome.ui.device_card import DeviceCard  # noqa: E402
from mihome.ui.widgets import IconProvider  # noqa: E402

check = V.check
PASS, FAIL = V.PASS, V.FAIL

SPEC = V.make_spec([V.WIRED_ONLY, V.WIRED_ONLY, V.WIRED_ONLY], sensor_mode=V.SENSOR_MODE)
CARD = catalog.build_card(SPEC, model="test.switch.sw3")
STYLE = T.style_by_id(T.SWITCH_WIRED)


def main() -> int:
    QApplication.instance() or QApplication([])  # 离屏渲染要先有 app
    icons = IconProvider(IconCache(None))
    pal = theme.DARK

    print("\n== 菜单可见性 ==")
    check("三路都带 mode 的面板：样式可用", STYLE.available(CARD))
    no_mode = catalog.build_card(V.make_spec([None, None, None]))
    check("没有 mode 属性（插排/老面板）：不出现", not STYLE.available(no_mode))
    check("非 switch 品类：不出现",
          not STYLE.available(SimpleNamespace(category="light", gangs=[])))
    names = [s.id for s in T.available_styles(CARD)]
    check("排在标准之后（默认项必须第一）", names[:2] == [T.STANDARD, T.SWITCH_WIRED], str(names))
    check("菜单文字是「小卡 1×1：隐藏转无线」",
          STYLE.menu_label == "小卡 1×1：隐藏转无线", STYLE.menu_label)

    print("\n== 三键里中键转无线 ==")
    st, refs = V.make_state(SPEC, {"g1": True, "m1": 0, "g2": False, "m2": 1, "g3": False, "m3": 0})
    card = DeviceCard(st, icons, pal, STYLE)
    card.resize(166, 168)
    shown = card._gangs()
    check("只剩两路（左键、右键）", [g.siid for g in shown] == [2, 4], str([g.siid for g in shown]))
    rects = card._gang_rects()
    check("按两键布局摆（两块竖条）", len(rects) == 2, str(len(rects)))
    # 两键卡的标准几何：内容带 138 宽，两块各 (138-8)/2 = 65
    check("每块宽度=两键布局（65px）", abs(rects[0].width() - 65) < 0.6, f"{rects[0].width():.1f}")
    check("副标题按画出来的两路报：2 路 · 1 路开启",
          card._subtitle() == "2 路 · 1 路开启", card._subtitle())
    hits = {card._hit(QPoint(int(r.center().x()), int(r.center().y()))) for r in rects}
    check("两块按键都可命中，且不含已隐藏的中键",
          hits == {("gang", 2), ("gang", 4)}, str(hits))

    print("\n== 同一张卡在标准样式下不受影响 ==")
    std = DeviceCard(st, icons, pal, T.style_by_id(T.STANDARD))
    std.resize(166, 168)
    check("标准样式仍画三路", len(std._gangs()) == 3, str(len(std._gangs())))
    check("标准样式副标题仍报无线：3 路 · 1 开 · 1 无线",
          std._subtitle() == "3 路 · 1 开 · 1 无线", std._subtitle())

    print("\n== 单开整块转无线：退回产品图 ==")
    one = V.make_spec([V.WIRED_ONLY])
    st1, _ = V.make_state(one, {"g1": True, "m1": 1}, name="吸顶灯开关")
    card1 = DeviceCard(st1, icons, pal, STYLE)
    card1.resize(166, 168)
    check("没有要画的按键", card1._gangs() == [])
    check("副标题解释原因：1 路 · 全部转无线",
          card1._subtitle() == "1 路 · 全部转无线", card1._subtitle())
    check("内容带不命中任何键（点卡片即打开详情）",
          card1._hit(QPoint(83, 120)) is None)
    check("内容带改画产品图", not card1._paints_gangs())

    print("\n== 三路全有线：与标准等价 ==")
    st3, _ = V.make_state(SPEC, {"g1": True, "m1": 0, "g2": True, "m2": 0, "g3": False, "m3": 0})
    card3 = DeviceCard(st3, icons, pal, STYLE)
    card3.resize(166, 168)
    check("三路都在", len(card3._gangs()) == 3)
    check("副标题与标准一致：3 路 · 2 路开启",
          card3._subtitle() == "3 路 · 2 路开启", card3._subtitle())

    out = ROOT / "scripts" / "out"
    out.mkdir(parents=True, exist_ok=True)
    shots = []
    for name, c in (("hidden_3to2", card), ("hidden_single", card1), ("hidden_all_wired", card3)):
        path = out / f"switch_{name}.png"
        c.grab().save(str(path))
        shots.append(path)
    check("三张对比图落盘", all(p.is_file() for p in shots),
          "、".join(p.name for p in shots))

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for name in FAIL:
            print(f"  - {name}")
        return 1
    return 0




def live_shot() -> int:
    """用真机数据 + 真图标渲染三张卡，拼成一张图（只读 prop/get）。"""
    from PySide6.QtGui import QColor, QPainter, QPixmap

    from mihome.core.api import Client, PropRef
    from mihome.core.spec import SpecRepository
    from mihome.core import store as store_mod

    cli = Client()
    topo = cli.load_topology()
    repo = SpecRepository()
    specs = repo.get_many([d.spec_type for d in topo.real_devices()], workers=4)
    targets = {"zimi.switch.dhkg01", "zimi.switch.dhkg05", "linp.switch.e3sbw3"}

    QApplication.instance() or QApplication([])  # 离屏渲染要先有 app
    icons = IconProvider(IconCache(None))
    pal = theme.DARK
    cards = []
    for node in topo.real_devices():
        if node.model not in targets:
            continue
        spec = specs.get(node.spec_type)
        if spec is None:
            continue
        card = catalog.build_card(spec, did=node.did, model=node.model,
                                  gang_names=node.gang_names, gang_rooms=node.gang_rooms)
        st = store_mod.DeviceState(node=node, spec=spec, card=card)
        refs = []
        for g in card.gangs:
            refs.append(PropRef(node.did, g.power.siid, g.power.piid))
            if g.mode is not None:
                refs.append(PropRef(node.did, g.mode.siid, g.mode.piid))
        got = cli.read(refs)
        for ref, val in got.items():
            st.values[(ref.siid, ref.piid)] = val.value
            st.keys[(ref.siid, ref.piid)] = ref
        w = DeviceCard(st, icons, pal, STYLE)
        cards.append(w)
        print(f"  {node.room} {node.name}：画 {len(w._gangs())}/{len(card.gangs)} 路"
              f"，副标题「{w._subtitle()}」")

    if not cards:
        print("没有匹配的设备")
        return 1
    pad, gap = 16, 12
    width = pad * 2 + sum(c.width() for c in cards) + gap * (len(cards) - 1)
    height = pad * 2 + max(c.height() for c in cards)
    out = QPixmap(width, height)
    out.fill(QColor(pal.bg))
    p = QPainter(out)
    x = pad
    for c in cards:
        p.drawPixmap(x, pad, c.grab())
        x += c.width() + gap
    p.end()
    path = ROOT / "scripts" / "out" / "switch_wired_live.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    out.save(str(path))
    print(f"已保存 {path.relative_to(ROOT)}  {width}x{height}")
    return 0


if __name__ == "__main__":
    if "--live" in sys.argv[1:]:
        raise SystemExit(live_shot())
    raise SystemExit(main())
