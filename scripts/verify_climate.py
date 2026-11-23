# SPDX-License-Identifier: GPL-3.0-or-later
"""验证全屋温湿度：取数口径、范围文字、悬停提示、顶栏胶囊接线。

跑法：``PYTHONPATH=. .venv/Scripts/python.exe scripts/verify_climate.py``

口径（2026-11-22 用户定）：只看品类自己声明的读数（``CardSpec.metrics`` 里的
``temperature`` / ``humidity`` 槽位）、只算当前家庭、多台取最小-最大范围。
这里全部离线：假设备、假读数、offscreen 起 QApplication。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 主窗口那一关会建 Prefs：别落到真实用户目录
_TMP = Path(tempfile.mkdtemp(prefix="mihome-climate-"))
os.environ["MIHOME_DATA_DIR"] = str(_TMP / "data")

from PySide6.QtWidgets import QApplication  # noqa: E402

from mihome.core import catalog, climate as C  # noqa: E402
from mihome.core.catalog import BoundControl, CardSpec  # noqa: E402
from mihome.core.spec import PropertySpec  # noqa: E402
from mihome.core.store import DeviceState  # noqa: E402
from mihome.core.tree import DeviceNode  # noqa: E402

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


# -- 夹具 ----------------------------------------------------------------
def prop(siid: int, piid: int, label: str, *, unit: str = "celsius") -> PropertySpec:
    return PropertySpec(
        siid=siid, piid=piid, name=f"p{siid}_{piid}", urn=f"urn:test:{siid}:{piid}",
        description=label, comment=label, format="float", access=("read", "notify"),
        unit=unit, value_range=None, value_list=(), service="env",
        service_label="环境", label_short=label,
    )


TEMP = prop(2, 1, "温度")
HUM = prop(3, 1, "湿度", unit="percentage")
PM25 = prop(4, 1, "PM2.5", unit="micrograms-per-cubic-meter")


def device(
    did: str,
    name: str,
    room: str,
    *,
    home_id: str = "h1",
    temp: object = None,
    hum: object = None,
    online: bool = True,
    category: str = "temperature-humidity-sensor",
) -> DeviceState:
    """造一台设备：卡片的 metrics 里按需挂温度 / 湿度两个读数。"""
    node = DeviceNode(did, name, "model.x", f"urn:miot-spec-v2:device:{category}:0000:x:1",
                      home="示例家庭", room=room, home_id=home_id, room_id=f"{home_id}-r",
                      online=online)
    metrics = []
    values: dict[tuple[int, int], object] = {}
    if temp is not None:
        metrics.append(BoundControl("temperature", "温度", "readout", TEMP, False))
        values[TEMP.key] = temp
    if hum is not None:
        metrics.append(BoundControl("humidity", "湿度", "readout", HUM, False))
        values[HUM.key] = hum
    card = CardSpec(category=category, profile=catalog.profile_for(category),
                    did=did, metrics=metrics)
    return DeviceState(node=node, spec=SimpleNamespace(properties=[TEMP, HUM, PM25]),
                       card=card, values=values,
                       keys={p.key: object() for p in (TEMP, HUM, PM25)})


def text_of(devices: list[DeviceState], slot: str = "temperature", *, home: str = "h1") -> str:
    return C.collect(devices, home).text(slot)


class _FakeTray:
    """主窗口在托盘不可用的环境里也要能起来（三个用例共用）。"""

    active = False

    def set_status(self, _text: str) -> None:
        pass

    def show_message(self, *_a, **_k) -> bool:
        return True

    def remove(self) -> None:
        pass


# ---------------------------------------------------------------------------
def test_collect() -> None:
    print("\n1) 取数口径：哪些设备、哪些值算数")
    one = [device("d1", "温湿度计", "客厅", temp=22.3, hum=48)]
    check("单台就是单值", text_of(one), "22.3°C")
    check("湿度同样取单值", text_of(one, "humidity"), "48%")

    many = [
        device("d1", "温湿度计", "客厅", temp=22.8, hum=51),
        device("d2", "空调", "主卧", category="air-conditioner", temp=21.5, hum=46),
    ]
    check("多台取最小-最大范围，不是平均", text_of(many), "21.5-22.8°C")
    check("湿度范围取整", text_of(many, "humidity"), "46-51%")

    check("整数温度不补 .0", text_of([device("d1", "空调", "客厅", temp=22),
                                device("d2", "空调", "主卧", temp=23)]), "22-23°C")
    check("0 °C 是有效读数（不能被当成空值）",
          text_of([device("d1", "温湿度计", "阳台", temp=0)]), "0°C")
    check("负温用 ~ 当分隔符，不写成 -5.0--3.0",
          text_of([device("d1", "温湿度计", "阳台", temp=-5.0),
                   device("d2", "温湿度计", "露台", temp=-3.0)]), "-5~-3°C")
    check("取整后两端相同就写单值",
          text_of([device("d1", "空调", "客厅", temp=21.44),
                   device("d2", "空调", "主卧", temp=21.45)]), "21.4°C")

    print("\n2) 不该参与的情况")
    check("别的家庭不参与",
          C.collect([device("d1", "空调", "客厅", temp=22),
                     device("d2", "空调", "厨房", temp=40, home_id="h2")], "h1").text("temperature"),
          "22°C")
    check("离线设备不参与（读数可能是几天前的）",
          text_of([device("d1", "温湿度计", "客厅", temp=22.3),
                   device("d2", "温湿度计", "主卧", temp=35.0, online=False)]), "22.3°C")
    # 2026-11-22 用户定：隐藏只是"别在卡片墙上占一格"，房间还在那儿
    check("被隐藏的设备**照算**（隐藏不等于搬走）",
          text_of([device("d1", "温湿度计", "客厅", temp=22.3),
                   device("d2", "温湿度计", "主卧", temp=30.0)]), "22.3-30.0°C")
    check("读不到值（None）不算", text_of([device("d1", "温湿度计", "客厅", temp=None)]), "")
    check("字符串不当数字用", text_of([device("d1", "温湿度计", "客厅", temp="22.3")]), "")
    check("bool 不当 1 用（True 是 int 的子类）",
          text_of([device("d1", "温湿度计", "客厅", temp=True)]), "")
    check("湿度 255 这种坏值被剔掉",
          text_of([device("d1", "温湿度计", "客厅", hum=48),
                   device("d2", "潮湿度计", "卫生间", hum=255)], "humidity"), "48%")
    check("温度 120 °C 被剔掉（那不是室内空气）",
          text_of([device("d1", "温湿度计", "客厅", temp=22.0),
                   device("d2", "烤箱", "厨房", temp=120.0)]), "22°C")
    check("没声明读数指标的设备不进范围",
          text_of([device("d1", "净化器", "客厅", category="air-purifier", temp=None)]), "")
    check("同一台设备的两条指标各进各的槽位",
          [text_of([device("d1", "温湿度计", "客厅", temp=22.3, hum=48)]),
           text_of([device("d1", "温湿度计", "客厅", temp=22.3, hum=48)], "humidity")],
          ["22.3°C", "48%"])

    print("\n3) 悬停提示与内部结构")
    one = C.collect([device("d1", "温湿度计", "客厅", temp=22.3, hum=48)], "h1")
    check("提示是「数值在前、名字在后」", one.tooltip("temperature"), "22.3°C  客厅 温湿度计")
    check("湿度提示同样带单位", one.tooltip("humidity"), "48%  客厅 温湿度计")

    home = [device(f"d{i}", f"温湿度计{i}", f"房{i}", temp=20 + i, hum=40 + i) for i in range(8)]
    climate = C.collect(home, "h1")
    tip = climate.tooltip("temperature")
    lines = tip.split("\n")
    check("一台一行", len(lines), 8)
    check("每行都以数值开头", [ln.split("  ")[0] for ln in lines],
          [f"{20 + i}°C" for i in range(8)])
    check("样本按数值升序（范围先小后大）",
          [s.value for s in climate.get("temperature")],
          [20.0, 21.0, 22.0, 23.0, 24.0, 25.0, 26.0, 27.0])

    many = [device(f"d{i}", f"温湿度计{i}", f"房{i}", temp=20 + i) for i in range(C.TOOLTIP_ROWS + 3)]
    long_tip = C.collect(many, "h1").tooltip("temperature").split("\n")
    check("超过上限就收尾成「等 N 台」", long_tip[-1], f"… 等 {len(many)} 台")
    check("上限内的台数一行一台", len(long_tip) - 1, C.TOOLTIP_ROWS)
    check("没有样本时提示为空串（悬停不弹空框）", C.collect([], "h1").tooltip("humidity"), "")


def test_strip() -> int:
    print("\n4) 顶栏胶囊：有数据才出现，两颗各自独立，各自带图标")
    from PySide6.QtCore import QRectF  # noqa: PLC0415
    from PySide6.QtGui import QColor, QImage, QPainter  # noqa: PLC0415
    from mihome.ui import theme  # noqa: PLC0415
    from mihome.ui.widgets import (  # noqa: PLC0415
        CHIP_GLYPH_GAP,
        CHIP_GLYPH_W,
        ClimateStrip,
        StatusChip,
        paint_chip_glyph,
    )

    app = QApplication.instance() or QApplication(sys.argv)
    strip = ClimateStrip(theme.LIGHT)
    check_true("初始整块隐藏", not strip.isVisibleTo(None) and not strip.isVisible())

    strip.set_climate(C.collect([device("d1", "温湿度计", "客厅", temp=22.3, hum=48)], "h1"))
    check("两颗的文字", (strip.temp.text(), strip.hum.text()), ("22.3°C", "48%"))
    check("两颗都不带那个表示开着/在线的绿点",
          (strip.temp._dot, strip.hum._dot), (False, False))  # noqa: SLF001
    check("温度计 / 水滴各认各的图标",
          (strip.temp._icon, strip.hum._icon),  # noqa: SLF001
          ("thermometer", "droplet"))
    check("胶囊宽度跟着文字走",
          strip.hum.width() < strip.temp.width(), True)

    # 图标真的画出来了（不是只占位）：三种都画进一张透明图上数非透明像素
    for kind in ("house", "thermometer", "droplet"):
        img = QImage(20, 26, QImage.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 0))
        painter = QPainter(img)
        painter.setRenderHint(QPainter.Antialiasing)
        paint_chip_glyph(painter, QRectF(5, 6, CHIP_GLYPH_W, 13), kind,
                         QColor("#000000"))
        painter.end()
        painted = sum(
            1 for y in range(26) for x in range(20) if img.pixelColor(x, y).alpha() > 0
        )
        check_true(f"{kind} 有实际笔画", painted > 30, f"{painted} 个像素")

    bare = StatusChip("22.3°C", theme.LIGHT)
    bare.set_dot(False)
    iconed = StatusChip("22.3°C", theme.LIGHT, icon="thermometer")
    check("图标真的占掉一段宽度（不是白画的）",
          iconed.width() - bare.width(), CHIP_GLYPH_W + CHIP_GLYPH_GAP)
    check_true("给了图标就不再画那个绿点", not iconed._dot)  # noqa: SLF001
    check("在线设备那颗也换成房子图标（顶栏三颗同一套长相）",
          (StatusChip("9 台设备 · 6 台开启", theme.LIGHT, icon="house")._icon,  # noqa: SLF001
           StatusChip("9 台设备 · 6 台开启", theme.LIGHT, icon="house")._dot),  # noqa: SLF001
          ("house", False))

    strip.set_climate(C.collect([device("d1", "空调", "客厅", category="air-conditioner",
                                        temp=21.5)], "h1"))
    check("只有温度时只显示温度", (strip.temp.text(), strip.hum.text()), ("21.5°C", ""))
    check_true("湿度那颗自己隐藏（不是显示空胶囊）",
               strip.hum.isVisibleTo(strip) is False or not strip.hum.isVisible())

    print("\n4b) 设置里的开关：关掉一颗不影响另一颗，也不丢数据")
    strip.set_climate(C.collect([device("d1", "温湿度计", "客厅", temp=22.3, hum=48)], "h1"))
    strip.set_shown("humidity", False)
    check("关掉湿度：湿度那颗不画", strip.hum.isVisibleTo(strip), False)
    check_true("温度那颗不受影响", strip.temp.isVisibleTo(strip))
    check("数据还在（不是当没读到）", strip.hum.text(), "48%")
    check("present() 只报该画的", [c is strip.temp for c in strip.present()], [True])
    strip.set_shown("humidity", True)
    check("开回来立刻恢复", strip.present(), [strip.temp, strip.hum])
    strip.set_shown("temperature", False)
    strip.set_shown("humidity", False)
    check_true("两颗都关掉时整块隐藏", not strip.isVisible())
    strip.set_shown("temperature", True)
    strip.set_shown("humidity", True)

    strip.set_climate(C.collect([], "h1"))
    check("一台都没有时整块消失", (strip.temp.text(), strip.hum.text()), ("", ""))
    check_true("容器也跟着隐藏", not strip.isVisible())
    app.processEvents()
    return 0


def test_window() -> None:
    print("\n5) 主窗口接线：跟当前家庭走，切家就换数")
    from mihome.core.api import HomeNode, RoomNode, Topology  # noqa: PLC0415
    from mihome.ui import theme  # noqa: PLC0415
    from mihome.ui.main_window import MainWindow  # noqa: PLC0415

    app = QApplication.instance() or QApplication(sys.argv)
    win = MainWindow(theme.LIGHT, tray_available=False)
    win._save_settings = lambda: None  # type: ignore[method-assign]  # noqa: SLF001
    win.tray = _FakeTray()  # type: ignore[assignment]
    try:
        win.store.topology = Topology(
            [HomeNode("h1", "示例家庭", [RoomNode("r1", "示例-客厅")]),
             HomeNode("h2", "示例-老家", [RoomNode("r9", "示例-厨房")])],
            [],
        )
        win.store.devices.clear()
        a = device("d1", "温湿度计", "示例-客厅", temp=22.3, hum=48)
        b = device("d2", "空调", "示例-厨房", home_id="h2", category="air-conditioner",
                   temp=28.0, hum=60)
        win.store.devices["d1"] = a
        win.store.devices["d2"] = b
        win.store.active_home = "h1"
        win._update_header()  # noqa: SLF001
        check("当前家庭 h1：只有那台的读数", win.climate.temp.text(), "22.3°C")

        # 隐藏设备照算：把 d1 藏起来，顶栏那个数不该变（2026-11-22 用户定）
        win.prefs.set_hidden("d1", True)
        win._update_header()  # noqa: SLF001
        check("隐藏的设备仍进范围", win.climate.temp.text(), "22.3°C")
        win.prefs.set_hidden("d1", False)

        win.store.active_home = "h2"
        win._update_header()  # noqa: SLF001
        check("切到 h2：换成另一家的读数", win.climate.temp.text(), "28°C")
        check("湿度也跟着换", win.climate.hum.text(), "60%")

        print("\n5b) 设置里的三块开关接到顶栏")
        win.resize(1400, 800)
        check_true("默认三块都画", not win.status.isHidden()
                   and not win.climate.temp.isHidden()
                   and not win.climate.hum.isHidden())
        win.prefs.set_header_show("online", False)
        win._update_header()  # noqa: SLF001
        check_true("关掉在线设备：那颗胶囊不画", win.status.isHidden())
        win.prefs.set_header_show("temperature", False)
        win._update_header()  # noqa: SLF001
        check_true("关掉温度：温度那颗不画", win.climate.temp.isHidden())
        check_true("湿度仍在", not win.climate.hum.isHidden())
        win.prefs.set_header_show("online", True)
        win.prefs.set_header_show("temperature", True)
        win._update_header()  # noqa: SLF001
        check_true("全部开回来", not win.status.isHidden()
                   and not win.climate.temp.isHidden())

        win.store.devices.clear()
        win._update_header()  # noqa: SLF001
        check_true("一台都没有时整块隐藏", not win.climate.isVisible())
    finally:
        win.close()
        app.processEvents()


def test_fit() -> None:
    print("\n6) 窄窗口：挤不下就先收湿度、再整颗收，绝不压住搜索框")
    from mihome.ui import theme  # noqa: PLC0415
    from mihome.ui.main_window import MainWindow  # noqa: PLC0415

    app = QApplication.instance() or QApplication(sys.argv)
    win = MainWindow(theme.LIGHT, tray_available=False)
    win._save_settings = lambda: None  # type: ignore[method-assign]  # noqa: SLF001
    win.tray = _FakeTray()  # type: ignore[assignment]
    try:
        win.climate.set_climate(
            C.collect([device("d1", "温湿度计", "示例-客厅", temp=21.5, hum=46),
                       device("d2", "空调", "示例-次卧", category="air-conditioner",
                              temp=22.8, hum=51)], "h1")
        )
        win.status.setText("9 台设备 · 6 台开启 · 2 台离线 · 1 台已隐藏")
        win.resize(1400, 800)
        win.show()
        app.processEvents()
        check_true("宽窗口两颗都在", win.climate.temp.isVisible() and win.climate.hum.isVisible())

        win.resize(920, 600)
        app.processEvents()
        win._fit_climate()  # noqa: SLF001
        app.processEvents()
        check_true("最小宽度下没压住搜索框",
                   win.climate.geometry().right() <= win.btn_messages.geometry().x(),
                   f"{win.climate.geometry().right()} vs {win.btn_messages.geometry().x()}")
        # 这个宽度在回退字体下两颗都放不下，整块收起也算对；关键是**不许压住搜索框**
        check_true("放不下时按顺序收：要么只剩温度、要么整块收起",
                   (not win.climate.isVisible())
                   or (win.climate.temp.isVisible() and not win.climate.hum.isVisible()))

        win.resize(1400, 800)
        app.processEvents()
        check_true("拉宽之后湿度又回来（不是一次性决定）", win.climate.hum.isVisible())

        # 只留湿度：温度关掉以后，"先收湿度"那条顺序不该把唯一的湿度也收掉。
        # 这里直接调 set_shown + _fit_climate，而不是 _update_header——后者会按
        # store 里的（空）设备重算读数，把上面手工塞的样本冲掉。
        #
        # 窗口宽度**按当前顶栏现算**，不写死 920：顶栏每加一个控件（2026-11-23
        # 加了「消息」胶囊）基线就变，写死的话这条会莫名其妙假红。
        win.climate.set_shown("temperature", False)
        win.climate.setVisible(False)
        win._head.invalidate()  # noqa: SLF001
        base = win._head.minimumSize().width()  # noqa: SLF001
        need_one = base + win.climate.GAP + win.climate.hum.width()
        win.resize(need_one + 20, 600)
        app.processEvents()
        win._fit_climate()  # noqa: SLF001
        app.processEvents()
        check_true("只剩湿度时窄窗口也留着它",
                   win.climate.isVisible() and win.climate.hum.isVisible(),
                   f"{win.width()} vs 需要 {need_one}")
        check_true("关掉的那颗没算进所需宽度",
                   win.climate.geometry().right() <= win.btn_messages.geometry().x(),
                   f"{win.climate.geometry().right()} vs {win.btn_messages.geometry().x()}")
        win.climate.set_shown("temperature", True)
    finally:
        win.close()
        app.processEvents()


def test_settings() -> None:
    print("\n7) 设置 → 外观 → 顶栏展示：三行开关写偏好并通知主窗口")
    from mihome.ui import theme  # noqa: PLC0415
    from mihome.ui.main_window import MainWindow  # noqa: PLC0415
    from mihome.ui.settings import SettingsDialog  # noqa: PLC0415

    app = QApplication.instance() or QApplication(sys.argv)
    win = MainWindow(theme.LIGHT, tray_available=False)
    win._save_settings = lambda: None  # type: ignore[method-assign]  # noqa: SLF001
    win.tray = _FakeTray()  # type: ignore[assignment]
    dlg = SettingsDialog(win.store, theme.LIGHT, tray_available=False)
    try:
        check("三行都在", sorted(dlg.header_switches),
              ["humidity", "online", "temperature"])
        check("开关初值就是偏好初值（默认三块全开）",
              [dlg.header_switches[k]._on for k in ("online", "temperature", "humidity")],  # noqa: SLF001
              [True, True, True])
        seen: list[str] = []
        dlg.header_changed.connect(seen.append)
        sw = dlg.header_switches["humidity"]
        sw.set_on(False)
        sw.toggled.emit(False)
        check("关掉湿度写进偏好", win.store.prefs.shows_header("humidity"), False)
        check("发信号给主窗口，参数是关掉的那一块", seen, ["humidity"])
        check("样式展示不跟着藏（关掉也要看得见它长什么样）",
              dlg._chip_previews[2].chip.text(), "46-51%")  # noqa: SLF001
        check("样式展示就是胶囊本身的大小（四周不多留背景）",
              [(pv.width() - pv.chip.width(), pv.height() - pv.chip.height())
               for pv in dlg._chip_previews],  # noqa: SLF001
              [(0, 0), (0, 0), (0, 0)])
        check("同色底上靠胶囊自己的描边分辨边界",
              [pv.chip._outline for pv in dlg._chip_previews],  # noqa: SLF001
              [True, True, True])
        check("三颗预览都带图标（房子 / 温度计 / 水滴）",
              [pv.chip._icon for pv in dlg._chip_previews],  # noqa: SLF001
              ["house", "thermometer", "droplet"])
        sw.set_on(True)
        sw.toggled.emit(True)
        check("再打开也写回偏好", win.store.prefs.shows_header("humidity"), True)
    finally:
        dlg.deleteLater()
        win.close()
        app.processEvents()


def main() -> int:
    test_collect()
    test_strip()
    test_window()
    test_fit()
    test_settings()
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
