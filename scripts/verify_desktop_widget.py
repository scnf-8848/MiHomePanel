# SPDX-License-Identifier: GPL-3.0-or-later
"""桌面挂件回归：prefs v19 槽位 / 几何 / 卡片 / 点击 / 拖动 / 轮询 / 层级。

不碰真实桌面：``zorder._api`` 换成假的，DefView / explorer 全在内存里模拟。
窗口在 offscreen 平台上跑，不显示给用户。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mihome.core.catalog import gang_summary  # noqa: E402
from mihome.core.poll_demand import PollDemandBoard  # noqa: E402
from mihome.core.prefs import (  # noqa: E402
    PREFS_VERSION,
    WIDGET_CELLS_MAX,
    WIDGET_LAYOUT_DEFAULT,
    Prefs,
    parse_widget_scene_slot,
    widget_layout_size,
    widget_scene_slot,
)
from mihome.ui import theme  # noqa: E402
from mihome.ui.desktop_card import (  # noqa: E402
    CARD_H,
    CARD_W,
    PAD,
    POWER_D,
    SceneCard,
    WidgetCard,
    card_subtitle,
    power_available,
)
from mihome.ui.desktop_widget import (  # noqa: E402
    BTN_LOCK,
    BTN_OPEN,
    GAP,
    HEADER_H,
    MIN_W,
    PANEL_PAD,
    _BUTTON_ORDER,
    _BUTTON_TIPS,
    DesktopWidget,
    zorder,
)

FAILED: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}  {got!r}")
    if not ok:
        print(f"         期望 {want!r}")
        FAILED.append(name)


def check_true(name: str, got, detail: str = "") -> None:
    print(f"  [{'OK  ' if got else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not got:
        FAILED.append(name)


# ---------------------------------------------------------------------------
# 假数据
# ---------------------------------------------------------------------------


def make_state(
    did: str = "d1",
    *,
    name: str = "示例-吸顶灯",
    room: str = "示例-客厅",
    online: bool = True,
    power: bool | None = True,
    gangs=(),
    metrics=(),
    has_power_control: bool = True,
    error: str = "",
    spec_type: str = "urn:miot-spec-v2:device:light:0000A001",
):
    control = None
    if has_power_control:
        control = SimpleNamespace(prop=SimpleNamespace(siid=2, piid=1))
    card = SimpleNamespace(
        metrics=list(metrics),
        gangs=list(gangs),
        control=lambda slot: control if slot == "power" else None,
    )
    node = SimpleNamespace(spec_type=spec_type)
    return SimpleNamespace(
        did=did,
        name=name,
        room=room,
        online=online,
        power=power,
        error=error,
        card=card,
        node=node,
        values={},
        keys={},
    )


class FakeStore:
    def __init__(self, states: dict[str, object] | None = None,
                 scenes: dict[str, list[dict]] | None = None,
                 home_labels: dict[str, str] | None = None) -> None:
        self.devices = dict(states or {})
        self.scenes_by_home = dict(scenes or {})
        self.home_labels = dict(home_labels or {})
        self.polls = 0
        self.toggles: list[tuple[str, int, int]] = []
        self.ran: list[tuple[str, str]] = []
        self.run_ok = True

    def poll(self) -> None:
        self.polls += 1

    def toggle(self, did: str, siid: int, piid: int) -> None:
        self.toggles.append((did, siid, piid))

    def home_label(self, home_id: str) -> str:
        return self.home_labels.get(home_id, home_id)

    def scenes_for(self, home_id: str, *, include_hidden: bool = False) -> list[dict]:
        return list(self.scenes_by_home.get(home_id) or [])

    def run_scene(self, scene_id: str, home_id: str = "", on_done=None) -> None:
        self.ran.append((scene_id, home_id))
        if on_done is not None:
            on_done(self.run_ok, "" if self.run_ok else "云端拒绝了这次请求")


class FakeZorder:
    """可以在测试里摆出"窗口还活着 / 已被 explorer 带走 / DefView 换了"三种局面。"""

    def __init__(self) -> None:
        self.alive = True
        self.defview = 100
        self.parent: dict[int, int] = {}
        self.topmost: dict[int, bool] = {}
        self.attached: list[int] = []
        self.calls: list[str] = []
        self.fg = 0

    def find_defview(self) -> int:
        return self.defview

    def is_window(self, hwnd: int) -> bool:
        return self.alive

    def get_parent(self, hwnd: int) -> int:
        return self.parent.get(hwnd, 0)

    def is_topmost(self, hwnd: int) -> bool:
        return self.topmost.get(hwnd, False)

    def set_parent(self, hwnd: int, parent: int) -> bool:
        self.calls.append("set_parent")
        if not self.alive:
            return False
        self.parent[hwnd] = parent
        return True

    def set_pos(self, hwnd: int, insert_after: int, flags: int) -> bool:
        self.calls.append(f"set_pos:{insert_after}")
        if insert_after == -1:
            self.topmost[hwnd] = True
        elif insert_after == -2:
            self.topmost[hwnd] = False
        return True

    def set_style(self, hwnd: int, add: int = 0, remove: int = 0) -> None:
        self.calls.append("set_style")

    def set_region(self, hwnd: int, width: int, height: int, radius: int) -> bool:
        return True

    def set_ext_style(self, hwnd: int, add: int = 0, remove: int = 0) -> None:
        self.calls.append(f"ext:+{add:#x}" if add else f"ext:-{remove:#x}")

    def foreground_window(self) -> int:
        return self.fg

    def set_foreground(self, hwnd: int) -> bool:
        self.calls.append(f"set_foreground:{hwnd}")
        self.fg = hwnd
        return True


def make_widget(tmp: Path, states: dict[str, object] | None = None,
                scenes: dict[str, list[dict]] | None = None,
                home_labels: dict[str, str] | None = None):
    prefs = Prefs(tmp / "prefs.json")
    store = FakeStore(states, scenes, home_labels)
    poll = PollDemandBoard("属性轮询", idle_ms=None)
    widget = DesktopWidget(store, prefs, theme.palette_for("porcelain", "light"), poll)
    return widget, store, prefs, poll


# ---------------------------------------------------------------------------
# 1. prefs v19（布局 + 槽位）
# ---------------------------------------------------------------------------


def test_prefs(tmp: Path) -> None:
    print("prefs v19")
    prefs = Prefs(tmp / "p1.json")
    check("版本号", PREFS_VERSION, 20)
    check("默认不开机启用", prefs.widget_autostart, False)
    check("默认布局", prefs.widget_layout, WIDGET_LAYOUT_DEFAULT)
    check("默认槽位", prefs.widget_slots, ["", "", ""])
    check("默认层级是普通档", prefs.widget_layer, "normal")
    check("默认几何", prefs.widget_geom, [])
    check("默认背景", prefs.widget_bg, "")

    prefs.set_widget_autostart(True)
    prefs.set_widget_layout("2x2")
    prefs.set_widget_slots(["a", "b", "a", ""])
    prefs.set_widget_geom([10, 20, 500, 200])
    prefs.set_widget_collapsed(True)
    prefs.set_widget_layer("top")
    prefs.set_widget_locked(True)
    prefs.set_widget_bg("#AABBCC")
    again = Prefs(tmp / "p1.json")
    check("开机启用落盘", again.widget_autostart, True)
    check("布局落盘", again.widget_layout, "2x2")
    check("槽位落盘（同一台只留第一格）", again.widget_slots, ["a", "b", "", ""])
    check("几何落盘", again.widget_geom, [10, 20, 500, 200])
    check("收起落盘", again.widget_collapsed, True)
    check("层级落盘", again.widget_layer, "top")
    check("锁定落盘", again.widget_locked, True)
    check("底色小写", again.widget_bg, "#aabbcc")
    check("行列推导", (again.widget_rows, again.widget_cols), (2, 2))

    check("同值不写布局", prefs.set_widget_layout("2x2"), False)
    check("非法布局不写", prefs.set_widget_layout("7x3"), False)
    check("非法层级不写", prefs.set_widget_layer("wallpaper"), False)
    check("非法底色不写", prefs.set_widget_bg("red"), False)
    check("坏几何不写", prefs.set_widget_geom([1, 2, 3]), False)

    # 换布局：按行优先保留，多了截、少了补空
    prefs.set_widget_slots(["a", "b", "c", "d"])
    prefs.set_widget_layout("1x3")
    check("换小布局截断", prefs.widget_slots, ["a", "b", "c"])
    prefs.set_widget_layout("2x2")
    check("换大布局补空", prefs.widget_slots, ["a", "b", "c", ""])

    # 单格写入：同一台设备在别处会被先清掉
    prefs.set_widget_layout("1x3")
    prefs.set_widget_slots(["a", "b", "c"])
    prefs.set_widget_slot(2, "a")
    check("写入后旧格被清", prefs.widget_slots, ["", "b", "a"])
    check("clear 清格", prefs.clear_widget_slot(1) and prefs.widget_slots, ["", "", "a"])
    check("remove 按 did 清", prefs.remove_widget("a") and prefs.widget_slots, ["", "", ""])
    check("越界不写", prefs.set_widget_slot(9, "x"), False)

    bad = tmp / "p2.json"
    bad.write_text(
        json.dumps(
            {
                "version": 19,
                "widget_autostart": "yes",
                "widget_layout": "9x9",
                "widget_slots": ["a", "a", "", 3, "b", "c", "d", "e", "f", "g", "h", "i", "j"],
                "widget_geom": [1, 2, 0, 5],
                "widget_layer": "wallpaper",
                "widget_bg": "red",
                "widget_collapsed": 1,
            }
        ),
        encoding="utf-8",
    )
    broken = Prefs(bad)
    check("坏开关回默认", broken.widget_autostart, False)
    check("坏布局回默认", broken.widget_layout, WIDGET_LAYOUT_DEFAULT)
    check("槽位去重去非法值（超格截断）", broken.widget_slots, ["a", "", ""])
    check("坏几何当没摆过", broken.widget_geom, [])
    check("坏层级回默认（普通档）", broken.widget_layer, "normal")
    check("坏底色回默认", broken.widget_bg, "")
    check("布局尺寸函数", widget_layout_size("2x3"), (2, 3))
    check("非法布局尺寸回默认", widget_layout_size("0x9"), widget_layout_size(
        WIDGET_LAYOUT_DEFAULT))
    check("格子上限", WIDGET_CELLS_MAX, 36)

    # 场景槽位：scene:<home>:<scene_id>。设备槽位仍是裸 did，两种可以混在一张表里
    check("场景槽位写法", widget_scene_slot("h1", "s9"), "scene:h1:s9")
    check("解析场景槽位", parse_widget_scene_slot("scene:h1:s9"), ("h1", "s9"))
    check("设备槽位不解析成场景", parse_widget_scene_slot("1234567"), None)
    check("缺 home 的槽位不认", parse_widget_scene_slot("scene::s9"), None)
    check("缺 scene_id 的槽位不认", parse_widget_scene_slot("scene:h1:"), None)
    prefs.set_widget_layout("1x3")
    prefs.set_widget_slots(["d1", widget_scene_slot("h1", "s1"), ""])
    check("混排后只数设备", prefs.widget_dids(), ["d1"])
    check("场景槽位单列", prefs.widget_scene_slots(), ["scene:h1:s1"])
    check("槽位原样保住场景", prefs.widget_slots[1], "scene:h1:s1")


def _items_of_kind(tree, kind: str) -> list:
    """按 ``data(0, UserRole)["kind"]`` 收集树里的节点（含各层子节点）。"""
    from PySide6.QtCore import Qt as _Qt
    from PySide6.QtWidgets import QTreeWidgetItem as _Item

    out: list = []

    def walk(item: _Item) -> None:
        if (item.data(0, _Qt.UserRole) or {}).get("kind") == kind:
            out.append(item)
        for i in range(item.childCount()):
            walk(item.child(i))

    for i in range(tree.topLevelItemCount()):
        walk(tree.topLevelItem(i))
    return out


def test_manager(tmp: Path) -> None:
    """管理窗：布局 + 槽位落盘，换布局按行优先保留。"""
    print("挂件管理窗")
    from types import SimpleNamespace

    from mihome.ui.widget_manager import WidgetManagerDialog, _DevicePicker

    prefs = Prefs(tmp / "mgr.json")
    prefs.set_widget_layout("1x2")
    prefs.set_widget_slots(["d1", "d2"])
    scenes = {
        "h1": [
            {"scene_id": "s1", "name": "示例-回家", "enable": True},
            {"scene_id": "s2", "name": "示例-已停用", "enable": False},
        ]
    }
    store = SimpleNamespace(
        devices={
            "d1": make_state("d1", name="示例-灯"),
            "d2": make_state("d2", name="示例-风扇"),
            "d3": make_state("d3", name="示例-插座"),
        },
        prefs=prefs,
        homes=lambda: [SimpleNamespace(id="h1", name="家")],
        home_label=lambda home_id: "示例-家",
        room_entries=lambda home_id: [("r1", "示例-客厅")],
        device_states=lambda home_id, room_id: [
            make_state("d1", name="示例-灯"), make_state("d3", name="示例-插座"),
        ],
        scenes_for=lambda home_id, include_hidden=False: list(scenes.get(home_id, [])),
        scenes_by_home=scenes,
    )
    dlg = WidgetManagerDialog(store, theme.palette_for("porcelain", "light"))
    check("按偏好铺宫格", (dlg.grid.rows, dlg.grid.cols), (1, 2))
    check("槽位原样读入", dlg.grid.cells(), ["d1", "d2"])

    dlg._on_remove(0)
    check("清空一格", dlg.grid.cells(), ["", "d2"])

    dlg.cb_rows.setCurrentIndex(dlg.cb_rows.findData(2))
    dlg.cb_cols.setCurrentIndex(dlg.cb_cols.findData(2))
    check("换布局行列", (dlg.grid.rows, dlg.grid.cols), (2, 2))
    check("换布局保留现有设备", dlg.grid.cells(), ["", "d2", "", ""])

    # 场景格：宫格要认得 scene: 前缀，画得出来、落盘不失真
    dlg.grid.set_grid(2, 2, [widget_scene_slot("h1", "s1"), "d2", "", ""])
    check_true("宫格能画场景格", not dlg.grid.grab().isNull())

    # 选卡弹窗：家庭下多一个「快捷场景」分组，只列 enable 为真的
    picker = _DevicePicker(store, set(), theme.palette_for("porcelain", "light"))
    check("树里有快捷场景分组", len(_items_of_kind(picker.tree, "scene_group")), 1)
    scene_items = _items_of_kind(picker.tree, "scene")
    check("只列启用中的场景", [i.text(0) for i in scene_items], ["示例-回家"])
    picker._on_click(scene_items[0], 0)
    check("选场景得到槽位值", picker.chosen(), "scene:h1:s1")
    check("选中后「添加」可用", picker.btn_ok.isEnabled(), True)
    picker.deleteLater()

    dlg.accept()
    check("关窗后布局落盘", prefs.widget_layout, "2x2")
    check("场景槽位一起落盘", prefs.widget_slots[0], "scene:h1:s1")
    check("关窗后槽位落盘", prefs.widget_slots, ["scene:h1:s1", "d2", "", ""])
    dlg.deleteLater()


# ---------------------------------------------------------------------------
# 1b. v18 → v19 迁移：旧的 columns + devices 变布局 + 槽位
# ---------------------------------------------------------------------------


def test_prefs_legacy_key(tmp: Path) -> None:
    print("prefs 迁移")
    legacy = tmp / "legacy.json"
    legacy.write_text(
        json.dumps(
            {
                "version": 17,
                "widget_enabled": True,
                "widget_layer": "desktop",
                "widget_columns": 3,
                "widget_devices": ["a", "b"],
            }
        ),
        encoding="utf-8",
    )
    prefs = Prefs(legacy)
    check("旧的开机启用键不再生效", prefs.widget_autostart, False)
    check("老文件的贴桌面档迁到普通档", prefs.widget_layer, "normal")
    check("v18 的三列变 1x3", prefs.widget_layout, "1x3")
    check("设备按行优先灌进槽位", prefs.widget_slots, ["a", "b", ""])

    # 设备多于一行：行数跟着长，最多 3 行
    more = tmp / "more.json"
    more.write_text(
        json.dumps(
            {
                "version": 18,
                "widget_columns": 2,
                "widget_devices": ["a", "b", "c", "d", "e"],
            }
        ),
        encoding="utf-8",
    )
    check("v18 两列五台 → 3x2", Prefs(more).widget_layout, "3x2")

    fresh = tmp / "fresh19.json"
    fresh.write_text(
        json.dumps({"version": 19, "widget_layer": "desktop", "widget_layout": "2x3"}),
        encoding="utf-8",
    )
    check("v19 之后按原样读", Prefs(fresh).widget_layer, "desktop")
    check("v19 布局原样读", Prefs(fresh).widget_layout, "2x3")


# ---------------------------------------------------------------------------
# 2. 几何
# ---------------------------------------------------------------------------


def test_geometry(tmp: Path) -> None:
    print("几何")
    widget, _store, prefs, _poll = make_widget(tmp, {})
    check_true("空设备也有尺寸", widget.width() >= MIN_W and widget.height() > HEADER_H)

    prefs.set_widget_geom([-9999, -9999, 500, 200])
    fresh, _s, _p, _q = make_widget(tmp, {})
    screen = QApplication.primaryScreen().availableGeometry()
    check_true("屏幕外被夹回", screen.contains(fresh.frameGeometry().center()),
               f"{fresh.geometry()}")

    widget.show()
    widget.move(100, 120)
    widget.save_geometry()
    saved = prefs.widget_geom
    check("落盘四个整数", len(saved), 4)
    check("x 落盘", saved[0], 100)
    widget.hide()
    check("hide 也会存", len(prefs.widget_geom), 4)


# ---------------------------------------------------------------------------
# 3. 卡片文案 / 4. 电源可用性
# ---------------------------------------------------------------------------


def test_card_text(tmp: Path) -> None:
    print("卡片文案与电源")
    offline = make_state(online=False)
    check("离线带房间", card_subtitle(offline), "示例-客厅 · 已离线")
    no_spec = make_state(spec_type="")
    check("无规格", card_subtitle(no_spec), "不支持控制")
    plain = make_state(power=True)
    check("没读数退回开关", card_subtitle(plain), "示例-客厅 · 已开启")

    metric = SimpleNamespace(
        prop=SimpleNamespace(
            key=(2, 1), format="int", value_list=[], unit="none", unit_text="",
        ),
        name="亮度",
    )
    with_metric = make_state(metrics=[metric])
    with_metric.values = {(2, 1): 40}
    check_true("有读数用读数", card_subtitle(with_metric).startswith("示例-客厅 · "),
               card_subtitle(with_metric))

    gang = SimpleNamespace(
        is_wireless=lambda values: False,
        power=SimpleNamespace(key=(3, 1)),
        label="左",
    )
    panel = make_state(gangs=[gang])
    panel.values = {(3, 1): True}
    check("多键摘要", card_subtitle(panel), "示例-客厅 · 1 路 · 1 路开启")
    check("gang_summary 一致", gang_summary([gang], panel.values), "1 路 · 1 路开启")

    check("灯可点电源", power_available(make_state()), True)
    check("离线不画电源", power_available(make_state(online=False)), False)
    check("多键不画总开关", power_available(make_state(gangs=[gang])), False)
    check("传感器不画电源", power_available(make_state(has_power_control=False)), False)


# ---------------------------------------------------------------------------
# 5. 点击语义（卡内）
# ---------------------------------------------------------------------------


def _mouse(widget, kind, pos: QPoint, *, button=Qt.LeftButton) -> QMouseEvent:
    global_pos = QPointF(widget.mapToGlobal(pos))
    return QMouseEvent(kind, QPointF(pos), global_pos, button, button, Qt.NoModifier)


def test_card_clicks(tmp: Path) -> None:
    print("点击语义")
    palette = theme.palette_for("porcelain", "light")
    st = make_state()
    card = WidgetCard(st, palette)
    opened: list[str] = []
    toggled: list[tuple[str, int, int]] = []
    card.open_requested.connect(opened.append)
    card.toggled.connect(lambda d, s, p: toggled.append((d, s, p)))
    card.resize(CARD_W, 56)

    blank = QPoint(20, 20)
    card.mousePressEvent(_mouse(card, QEvent.MouseButtonPress, blank))
    card.mouseReleaseEvent(_mouse(card, QEvent.MouseButtonRelease, blank))
    check("点空白进详情", opened, ["d1"])
    check("点空白不切电源", toggled, [])

    power = card._power_rect().center()
    card.mousePressEvent(_mouse(card, QEvent.MouseButtonPress, power))
    card.mouseReleaseEvent(_mouse(card, QEvent.MouseButtonRelease, power))
    check("点电源切一次", toggled, [("d1", 2, 1)])
    check("点电源不进详情", opened, ["d1"])

    # 按下在电源、松手滑出去：不算点击也不算切换（和主界面同一套语义）
    card.mousePressEvent(_mouse(card, QEvent.MouseButtonPress, power))
    card.mouseReleaseEvent(_mouse(card, QEvent.MouseButtonRelease, QPoint(10, 10)))
    check("滑出去不切", toggled, [("d1", 2, 1)])
    check("滑出去不进详情", opened, ["d1"])


# ---------------------------------------------------------------------------
# 6. 拖动阈值 / 7. 尺寸 / 8. 轮询 / 9. 主题 / 10. 徽标
# ---------------------------------------------------------------------------


def test_widget_behaviour(tmp: Path) -> None:
    print("挂件本体")
    states = {f"d{i}": make_state(f"d{i}", name=f"示例-灯{i}") for i in range(3)}
    widget, store, prefs, poll = make_widget(tmp, states)
    prefs.set_widget_slots(["d0", "d1", "d2"])
    widget.rebuild()
    check("三张卡", len(widget._cards), 3)
    check("宽 = 3 列", widget.width(), 158 * 3 + GAP * 2 + PANEL_PAD * 2)
    check_true("卡片按槽位下标存", sorted(widget._cards) == [0, 1, 2],
               str(sorted(widget._cards)))

    prefs.set_widget_layout("1x1")
    check("1 列宽度", widget._panel_width(), max(MIN_W, 158 + PANEL_PAD * 2))

    widget.show()
    check("展开轮询 3 秒", poll.interval_ms, 3000)
    check_true("登记 owner", "挂件展开" in poll.describe(), poll.describe())
    widget.hide()
    check("隐藏后撤销登记", poll.interval_ms, None)

    # 拖动：不到 6 px 不动，过了才动
    widget.show()
    widget.move(100, 100)
    start = QPoint(30, 10)
    widget.mousePressEvent(_mouse(widget, QEvent.MouseButtonPress, start))
    widget.mouseMoveEvent(
        _mouse(widget, QEvent.MouseMove, start + QPoint(4, 0), button=Qt.NoButton)
    )
    check("4 px 不动", widget.pos(), QPoint(100, 100))
    widget.mouseMoveEvent(
        _mouse(widget, QEvent.MouseMove, start + QPoint(12, 8), button=Qt.NoButton)
    )
    check("超过阈值才动", widget.pos(), QPoint(112, 108))
    widget.mouseReleaseEvent(
        _mouse(widget, QEvent.MouseButtonRelease, start + QPoint(12, 8))
    )
    widget.hide()

    widget.set_badges(2, 1)
    # 两档同时有：右下角要并排画两个圆点（先轻后重），不再是"重的顶掉轻的"
    check("两档都在", widget._footer_badge(), (2, 1))
    widget.set_badges(2, 0)
    check("只有轻提醒", widget._footer_badge(), (2, 0))
    widget.set_badges(0, 3)
    check("只有重提醒", widget._footer_badge(), (0, 3))
    widget.set_badges(0, 0)
    check("无提醒不画", widget._footer_badge(), None)

    other = theme.palette_for("midnight", "dark")
    widget.set_palette(other)
    check("换主题", widget._pal.preset, other.preset)
    check_true("主题确实换了", widget._pal.is_dark and widget._pal.card != theme.palette_for(
        "porcelain", "light").card)

    # 设备被移出挂件：重建后卡片消失
    prefs.set_widget_layout("1x3")
    prefs.set_widget_slots(["d0", "d1", "d2"])
    prefs.remove_widget("d1")
    widget.rebuild()
    check("移出后两张", sorted(widget._cards), [0, 2])

    # 空槽留空：不把后面的设备挤上来
    prefs.set_widget_slots(["d0", "", "d2"])
    widget.rebuild()
    check("空槽不建卡", sorted(widget._cards), [0, 2])
    check_true("第 2 格空着、第 3 格在右侧",
               widget._cards[2].x() > widget._cards[0].x() + 158,
               f"x0={widget._cards[0].x()} x2={widget._cards[2].x()}")

    # 开关关着时点「预览」：浮一下之后要收起来，不能留幽灵窗口
    # 默认档改成普通档：show() 之后就能直接看到，不需要先花里胡哨地"预览"
    old = zorder._api
    zorder._api = FakeZorder()
    try:
        widget.hide()
        widget.show()
        check_true("普通档 show 即可见", widget.isVisible())
        widget.apply_layer("normal")
        check("普通档就是普通档", widget.layer, "normal")
    finally:
        zorder._api = old
        widget.hide()


# ---------------------------------------------------------------------------
# 11. 空态 / 12. 状态刷新
# ---------------------------------------------------------------------------


def test_header_buttons(tmp: Path) -> None:
    print("头部按钮")
    widget, _store, prefs, _poll = make_widget(tmp, {"d0": make_state("d0")})
    prefs.set_widget_slots(["d0"])
    widget.rebuild()
    rects = {name: widget._button_rect(name) for name in _BUTTON_ORDER}
    check("头部五个按钮", len(rects), 5)
    check("位置互不重叠", len({(r.x(), r.y()) for r in rects.values()}), 5)
    check_true(
        "都落在头部横带里",
        all(r.top() >= 0 and r.bottom() <= HEADER_H for r in rects.values()),
        str([(r.x(), r.y()) for r in rects.values()]),
    )
    check("每个按钮都有悬停提示", all(t in _BUTTON_TIPS for t in _BUTTON_ORDER), True)
    emitted: list[bool] = []
    widget.open_main_requested.connect(lambda: emitted.append(True))
    widget._activate_button(BTN_OPEN)
    check("打开主窗口要发信号", len(emitted), 1)


def test_lock(tmp: Path) -> None:
    """顶栏锁定按钮：锁上只挡拖动，解锁后恢复；状态进 prefs。"""
    print("锁定位置")
    widget, _store, prefs, _poll = make_widget(tmp, {"d0": make_state("d0")})
    prefs.set_widget_slots(["d0"])
    widget.rebuild()
    check("默认不锁", widget.locked, False)
    check("未锁时的提示", widget._button_tip(BTN_LOCK), _BUTTON_TIPS[BTN_LOCK])

    widget.show()
    widget.move(100, 100)
    widget._activate_button(BTN_LOCK)
    check("点按钮锁上", prefs.widget_locked, True)
    check("锁定落盘", Prefs(tmp / "prefs.json").widget_locked, True)
    check("锁定时提示改成解锁", widget._button_tip(BTN_LOCK), "解锁位置")

    start = QPoint(30, 10)
    widget.mousePressEvent(_mouse(widget, QEvent.MouseButtonPress, start))
    widget.mouseMoveEvent(
        _mouse(widget, QEvent.MouseMove, start + QPoint(24, 18), button=Qt.NoButton)
    )
    check("锁住后拖不动", widget.pos(), QPoint(100, 100))
    widget.mouseReleaseEvent(
        _mouse(widget, QEvent.MouseButtonRelease, start + QPoint(24, 18))
    )

    widget._activate_button(BTN_LOCK)
    check("再点解锁", prefs.widget_locked, False)
    widget.mousePressEvent(_mouse(widget, QEvent.MouseButtonPress, start))
    widget.mouseMoveEvent(
        _mouse(widget, QEvent.MouseMove, start + QPoint(24, 18), button=Qt.NoButton)
    )
    check("解锁后能拖", widget.pos(), QPoint(124, 118))
    widget.mouseReleaseEvent(
        _mouse(widget, QEvent.MouseButtonRelease, start + QPoint(24, 18))
    )
    widget.hide()


def arrow_ink_center(card: SceneCard, scale: int = 4) -> float:
    """右箭头墨迹的**包围盒中心**相对圆心的横向偏移（逻辑像素，左负右正）。

    为什么要量像素：2026-11-27 用户报"右箭头在圆里有点偏左"。当时那版是
    「尖端 +0.7dx、尾端 -dx」，圆本身居中但字形墨迹的包围盒中心落在圆心左边
    0.5 px —— 26 px 的圆里看得出来。这里按 4 倍离屏渲染后统计暗像素范围，
    不依赖屏幕缩放，也不会像目测那样放过 0.5 px 的偏心。
    """
    img = QImage(CARD_W * scale, CARD_H * scale, QImage.Format_ARGB32)
    img.fill(QColor("#FFFFFF"))
    p = QPainter(img)
    p.scale(scale, scale)
    card.render(p, QPoint(0, 0))
    p.end()

    fill = QColor(theme.blend(card._pal.card, card._pal.text, 0.08))
    base = 0.299 * fill.red() + 0.587 * fill.green() + 0.114 * fill.blue()
    left = max(int((CARD_W - PAD - POWER_D) * scale) - 2, 0)
    right = min(int((CARD_W - PAD) * scale) + 2, img.width())
    xs = [
        x
        for y in range(img.height())
        for x in range(left, right)
        if base - (
            0.299 * img.pixelColor(x, y).red()
            + 0.587 * img.pixelColor(x, y).green()
            + 0.114 * img.pixelColor(x, y).blue()
        ) > 4
    ]
    if not xs:
        return float("nan")
    circle_cx = (CARD_W - PAD - POWER_D + POWER_D / 2) * scale
    return ((min(xs) + max(xs) + 1) / 2 - circle_cx) / scale


def test_scene_card(tmp: Path) -> None:
    """快捷场景卡：槽位识别、点击执行、结果就地显示、场景被删不建卡。"""
    print("快捷场景卡")
    scenes = {
        "h1": [
            {"scene_id": "s1", "name": "示例-回家", "enable": True},
            {"scene_id": "s2", "name": "示例-离家", "enable": True},
        ]
    }
    widget, store, prefs, _poll = make_widget(
        tmp, {"d0": make_state("d0")}, scenes, {"h1": "示例-家"}
    )
    prefs.set_widget_layout("1x2")
    prefs.set_widget_slots([widget_scene_slot("h1", "s1"), "d0"])
    widget.rebuild()
    check("场景槽位建场景卡", isinstance(widget._cards.get(0), SceneCard), True)
    check("设备槽位还是设备卡", isinstance(widget._cards.get(1), WidgetCard), True)
    check("场景槽位不混进设备表", prefs.widget_dids(), ["d0"])

    card = widget._cards[0]
    check("副标题用家庭名", card.home_label, "示例-家")
    check_true("场景卡画得出来", not card.grab().isNull())
    offset = arrow_ink_center(card)
    check("右箭头在圆里居中（实测偏心 ≤0.35 px）", abs(offset) <= 0.35, True)
    widget.update_states({"d0"})
    check_true("增量刷新跳过场景卡不炸", True)

    blank = QPoint(20, 20)
    card.mousePressEvent(_mouse(card, QEvent.MouseButtonPress, blank))
    card.mouseReleaseEvent(_mouse(card, QEvent.MouseButtonRelease, blank))
    check("点一下执行一次", store.ran, [("s1", "h1")])
    check("成功写在副标题上", card._flash, "已执行")

    store.run_ok = False
    card.mousePressEvent(_mouse(card, QEvent.MouseButtonPress, blank))
    card.mouseReleaseEvent(_mouse(card, QEvent.MouseButtonRelease, blank))
    check("失败也写在副标题上", card._flash, "执行失败")
    check("失败原因不糊在卡片上", len(store.ran), 2)

    store.run_ok = True
    card.mousePressEvent(_mouse(card, QEvent.MouseButtonPress, blank))
    card.mouseReleaseEvent(_mouse(card, QEvent.MouseButtonRelease, QPoint(400, 400)))
    check("按下后滑走不执行", len(store.ran), 2)

    # 场景在 App 里被删：槽位留着（不替用户改），但卡片不建——别摆一颗点了必失败的按钮
    store.scenes_by_home["h1"] = [{"scene_id": "s2", "name": "示例-离家", "enable": True}]
    widget.rebuild()
    check("场景没了就不建卡", 0 in widget._cards, False)
    check("槽位没被偷偷改写", prefs.widget_slots[0], "scene:h1:s1")
    check("设备卡照旧", isinstance(widget._cards.get(1), WidgetCard), True)


def test_empty_and_update(tmp: Path) -> None:
    print("空态与增量刷新")
    widget, store, prefs, _poll = make_widget(tmp, {})
    check("没有卡片", widget._cards, {})
    widget.show()
    pix = widget.grab()
    check_true("空态能画出来", not pix.isNull() and pix.width() == widget.width())

    states = {"d1": make_state("d1", name="示例-灯")}
    widget2, store2, prefs2, _poll2 = make_widget(tmp, states)
    prefs2.set_widget_slots(["d1"])
    widget2.rebuild()
    card = widget2._cards[0]
    new_state = make_state("d1", name="示例-灯", online=False)
    store2.devices["d1"] = new_state
    widget2.update_states({"d1"})
    check("卡片状态跟着换", card.state.online, False)
    widget2.update_states({"不存在"})
    check_true("未知 did 不炸", True)


# ---------------------------------------------------------------------------
# 13. 层级纯逻辑（watchdog_action / current_layer）
# ---------------------------------------------------------------------------


def test_zorder_logic(tmp: Path) -> None:
    print("层级纯逻辑")
    check("非桌面档不管", zorder.watchdog_action(
        layer="normal", is_window=False, parent=0, defview=0), "none")
    check("句柄没了要重建", zorder.watchdog_action(
        layer="desktop", is_window=False, parent=0, defview=100), "rebuild")
    check("DefView 还没起来先等", zorder.watchdog_action(
        layer="desktop", is_window=True, parent=0, defview=0), "none")
    check("父窗口没换", zorder.watchdog_action(
        layer="desktop", is_window=True, parent=100, defview=100), "none")
    check("explorer 换了 DefView", zorder.watchdog_action(
        layer="desktop", is_window=True, parent=99, defview=100), "reattach")


# ---------------------------------------------------------------------------
# 14. 层级切换与看门狗
# ---------------------------------------------------------------------------


def test_bring_to_front(tmp: Path) -> None:
    print("普通档点击浮到最前")
    old = zorder._api
    fake = FakeZorder()
    zorder._api = fake
    try:
        widget, _store, _prefs, _poll = make_widget(tmp, {"d1": make_state("d1")})
        widget.rebuild()
        widget.show()

        widget.apply_layer("normal", save=False)
        check_true("普通档可被点击激活（清掉 WS_EX_NOACTIVATE）", "ext:-0x8000000" in fake.calls)
        fake.calls.clear()
        widget._bring_to_front()
        check_true(
            "普通档点一下请求提到最前（HWND_TOP 的 SetWindowPos）",
            "set_pos:0" in fake.calls,
        )
        check("提层不改档位", widget.layer, "normal")

        fake.calls.clear()
        widget.apply_layer("desktop", save=False)
        fake.calls.clear()
        widget._bring_to_front()
        check_true("桌面档不发 SetWindowPos", "set_pos:0" not in fake.calls)

        widget.apply_layer("top", save=False)
        check_true("置顶档恢复 NOACTIVATE（不抢前台）", "ext:+0x8000000" in fake.calls)
        fake.calls.clear()
        widget._bring_to_front()
        check_true("置顶档不发 SetWindowPos", "set_pos:0" not in fake.calls)

        # 鼠标按下要真的走到这条路上（右键菜单入口同理）
        hits: list[int] = []
        widget._bring_to_front = lambda: hits.append(1)  # type: ignore[method-assign]
        widget.mousePressEvent(
            QMouseEvent(
                QEvent.MouseButtonPress,
                QPointF(5, 5),
                widget.mapToGlobal(QPoint(5, 5)),
                Qt.LeftButton,
                Qt.LeftButton,
                Qt.NoModifier,
            )
        )
        check("按下先请求浮前", len(hits), 1)
    finally:
        zorder._api = old


def test_layer(tmp: Path) -> None:
    print("层级切换与看门狗")
    old = zorder._api
    fake = FakeZorder()
    zorder._api = fake
    try:
        widget, _store, prefs, _poll = make_widget(tmp, {"d1": make_state("d1")})
        prefs.set_widget_slots(["d1"])
        widget.rebuild()
        widget.show()

        check("默认普通档", prefs.widget_layer, "normal")
        ok = widget.apply_layer("desktop")
        check_true("挂进 DefView 成功", ok)
        check("层级属性", widget.layer, "desktop")
        check_true("父窗口是 DefView", fake.parent.get(int(widget.winId())) == 100)

        check_true("切置顶", widget.apply_layer("top"))
        check("置顶层", widget.layer, "top")
        check_true("置顶生效", fake.topmost.get(int(widget.winId())) is True)

        check_true("切普通", widget.apply_layer("normal"))
        check("普通层", widget.layer, "normal")
        check_true("取消置顶", fake.topmost.get(int(widget.winId())) is False)

        # 桌面档挂不上：静默降级成普通，但 prefs 不改（explorer 回来还能贴回去）
        nodef = FakeZorder()
        nodef.defview = 0
        zorder._api = nodef
        check("挂不上返回 False", widget.apply_layer("desktop"), False)
        check("降级成普通", widget.layer, "normal")
        check("不写回偏好", prefs.widget_layer, "normal")

        # shell 还没起来时降级，起来以后要自己贴回去（不用用户再点）
        check_true("降级后开着重试", widget._retry_desktop)
        nodef.defview = 100
        widget._tick_watchdog()
        check("shell 回来后自愈到桌面档", widget.layer, "desktop")
        check_true("自愈后父窗口是 DefView", nodef.parent.get(int(widget.winId())) == 100)
        check("自愈后不再重试", widget._retry_desktop, False)

        # 窗口被 explorer 带走：看门狗要发 rebuild
        zorder._api = fake
        widget.apply_layer("desktop")
        fake.alive = False
        fired: list[int] = []
        widget.rebuild_needed.connect(lambda: fired.append(1))
        widget._tick_watchdog()
        check("死句柄触发重建", len(fired), 1)
        fake.alive = True
    finally:
        zorder._api = old


def test_close_fallback(tmp: Path) -> None:
    """托盘挂不上时，关窗要收进挂件，而不是把挂件一起带走（2026-11-21）。

    用户的现场：设置里选的是"最小化到托盘"，但托盘注册失败 → 关面板 →
    挂件和程序一起消失，看起来就是"关掉了程序"。进程还有可见的存在，
    就不该无声退出。
    """
    print("关窗退路：托盘不可用时收进挂件")
    from mihome.ui.main_window import MainWindow

    class _Panel:
        def __init__(self, visible: bool) -> None:
            self._visible = visible

        def isVisible(self) -> bool:  # noqa: N802 —— 跟着 Qt 的命名
            return self._visible

    def stub(*, close_to_tray: bool, visible: bool, quitting: bool = False):
        return SimpleNamespace(
            _quitting=quitting,
            prefs=SimpleNamespace(close_to_tray=close_to_tray),
            widget_panel=_Panel(visible),
        )

    ask = MainWindow._should_hide_to_widget
    check("挂件在 → 收进挂件", ask(stub(close_to_tray=True, visible=True)), True)
    check("挂件不在 → 走退出", ask(stub(close_to_tray=True, visible=False)), False)
    check("用户选了退出程序 → 走退出", ask(stub(close_to_tray=False, visible=True)), False)
    check("正在退出 → 不再拦", ask(stub(close_to_tray=True, visible=True, quitting=True)), False)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    tmp = Path(tempfile.mkdtemp())
    for fn in (
        test_prefs,
        test_manager,
        test_prefs_legacy_key,
        test_geometry,
        test_card_text,
        test_card_clicks,
        test_widget_behaviour,
        test_header_buttons,
        test_lock,
        test_scene_card,
        test_empty_and_update,
        test_zorder_logic,
        test_layer,
        test_bring_to_front,
        test_close_fallback,
    ):
        try:
            fn(tmp)
        except Exception as exc:  # noqa: BLE001 —— 一项炸了要继续跑后面的
            import traceback

            traceback.print_exc()
            FAILED.append(f"{fn.__name__}: {exc}")
    app.processEvents()
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：" + "、".join(FAILED))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
