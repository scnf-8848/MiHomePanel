# SPDX-License-Identifier: GPL-3.0-or-later
"""设置页两棵树（排序 / 隐藏）的离线验证。

八件事：

1. **结构**：家庭 → 房间 → 设备三层，场景是家庭 → 场景两层，"快捷场景"是隐藏页
   里场景所在的那一层；
2. **展开状态**：折起来之后再重建，还保持折起来；
3. **排序**：同层拖动后 ``apply_order_snapshot`` 真的落进了 prefs（并且只落一次盘），
   重填过程中（``_loading``）不写；
4. **跨层拖动被拒**：把设备拖到别的房间 = 改归属，不在这个程序的职责里；
5. **隐藏页聚合开关**：全显 → 全隐 → 部分态停在中间，家庭那一层把场景也算进去；
6. **几何与绘制**：行高 32 / 缩进 16，折角只画在有子项的行上，开关按状态上色；
7. **分类栏分层**：分类栏和内容区曾经同色（内容区是 ``surface_bg``，直接套主界面
   那套 ``NAV_BG`` 只差两三个明度档），这一节在真实窗口上抓图量"栏底 / 内容区 /
   选中行"三处，并把圆角也一起验了；
8. **分区面板**：排序页那两块（设备树 / 场景树）以前直接摊在窗口底色上，深色下
   几乎看不出边界。这一节量面板底 vs 窗口底色、四个角是不是圆的、树有没有内缩、
   选中行在面板上还看不看得出来。

用法：``.venv/Scripts/python.exe -X utf8 scripts/verify_tree.py``
"""

from __future__ import annotations

import inspect
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractItemView,
    QApplication,
    QFrame,
    QLabel,
)

from mihome.core.api import HomeNode, RoomNode, Topology  # noqa: E402
from mihome.core.prefs import Prefs  # noqa: E402
from mihome.core.store import DeviceState, Store, build_card_empty  # noqa: E402
from mihome.core.tree import DeviceNode  # noqa: E402
from mihome.ui import theme  # noqa: E402
from mihome.ui.settings import COMBO_W, SettingsDialog  # noqa: E402
from mihome.ui.settings_rows import ROW_MIN_H, SettingsRow  # noqa: E402
from mihome.ui.tree import (  # noqa: E402
    CHEV,
    INDENT,
    ROLE_DATA,
    ROLE_SUFFIX,
    ROLE_SWITCH,
    ROW_H,
    _chevron_hit,
    _switch_rect,
    row_x,
)

PATH = Path(tempfile.gettempdir()) / "mihome_verify_tree.json"
FAILS: list[str] = []
OPEN: list[SettingsDialog] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(f"{name} {detail}")


def make_store() -> Store:
    class _Caps:
        def save(self) -> None:
            pass

    class _Client:
        capabilities = _Caps()

    class _Worker:
        pass

    store = Store(_Client(), _Worker(), None)  # type: ignore[arg-type]
    store.prefs = Prefs(PATH)
    store.prefs.hidden = []
    store.prefs.hidden_scenes = []

    homes = [
        HomeNode("h1", "我的家", [RoomNode("r1", "客厅"), RoomNode("r2", "卧室")]),
        HomeNode("h2", "办公室", [RoomNode("r3", "工位")]),
    ]
    devices = [
        DeviceNode("d1", "主灯", "m", "light.x", home="我的家", room="客厅"),
        DeviceNode("d2", "台灯", "m", "light.x", home="我的家", room="客厅"),
        DeviceNode("d3", "床灯", "m", "light.x", home="我的家", room="卧室", online=False),
        DeviceNode("d4", "吸顶灯", "m", "light.x", home="办公室", room="工位"),
    ]
    store.topology = Topology(homes, devices)
    for node in devices:
        store.devices[node.did] = DeviceState(node=node, spec=None, card=build_card_empty(node))
    store.scenes_by_home = {
        "我的家": [
            {"scene_id": "s1", "name": "回家", "home_id": "h1", "enable": True},
            {"scene_id": "s2", "name": "离家", "home_id": "h1", "enable": True},
        ],
        "办公室": [],
    }
    return store


def fresh(palette: theme.Palette = theme.LIGHT) -> tuple[Store, SettingsDialog]:
    """干净偏好 + 新窗口。每个小节都从头来，免得上一节的顺序影响下一节。"""
    PATH.unlink(missing_ok=True)
    store = make_store()
    dlg = SettingsDialog(store, palette)
    dlg.show()
    QApplication.processEvents()
    OPEN.append(dlg)
    return store, dlg


def find(tree, **want):
    for item in tree._walk():
        data = item.data(0, ROLE_DATA) or {}
        if all(data.get(k) == v for k, v in want.items()):
            return item
    return None


def suffix(item) -> str:
    return str(item.data(0, ROLE_SUFFIX) or "") if item is not None else "<无此行>"


def switch(item) -> float | None:
    return None if item is None else item.data(0, ROLE_SWITCH)


class _Drop:
    """只带 ``position()`` / ``accept()`` / ``ignore()`` 的假事件——够判定和丢弃用了。"""

    def __init__(self, pos: QPoint) -> None:
        self._pos = pos
        self.accepted = False

    def position(self) -> QPointF:  # noqa: N802
        return QPointF(self._pos)

    def accept(self) -> None:
        self.accepted = True

    def ignore(self) -> None:
        self.accepted = False


def center(tree, item) -> QPoint:
    return tree.visualItemRect(item).center()


# ---------------------------------------------------------------------------
def section_structure() -> None:
    print("1) 三层结构")
    store, dlg = fresh()
    dev, scene, hidden = dlg.tree_devices, dlg.tree_scenes, dlg.tree_hidden

    check("设备树 = 两个家庭", dev.topLevelItemCount() == 2, f"{dev.topLevelItemCount()} 行")
    home = find(dev, kind="home", key="我的家")
    check("家庭行找得到", home is not None)
    check("家庭下两个房间", home.childCount() == 2, str([home.child(i).text(0) for i in range(2)]))
    living = find(dev, kind="room", room="客厅")
    check("客厅下两台设备", living.childCount() == 2,
          str([living.child(i).text(0) for i in range(2)]))
    check("设备是叶子（没有第四层）", living.child(0).childCount() == 0)
    check("房间行带台数", suffix(living) == "2 台", suffix(living))
    check("设备行不带台数", suffix(living.child(0)) == "", f"'{suffix(living.child(0))}'")

    check("场景树也是两个家庭", scene.topLevelItemCount() == 2)
    s_home = find(scene, kind="home", key="我的家")
    check("场景树只有两层", s_home.child(0).childCount() == 0)
    check("我的家两个场景", s_home.childCount() == 2,
          str([s_home.child(i).text(0) for i in range(2)]))
    check("没场景的家庭是空行", find(scene, kind="home", key="办公室").childCount() == 0)

    check("隐藏树 = 两个家庭", hidden.topLevelItemCount() == 2)
    h_home = find(hidden, kind="home", home="我的家")
    check("隐藏树家庭下 = 两个房间 + 快捷场景", h_home.childCount() == 3,
          str([h_home.child(i).text(0) for i in range(3)]))
    group = find(hidden, kind="scene_group", home="我的家")
    check("「快捷场景」是个分组行", group is not None and group.childCount() == 2)
    check("分组行的数据是 scene_group", (group.data(0, ROLE_DATA) or {}).get("kind") == "scene_group")
    check("隐藏树家庭行有 5/5（3 设备 + 2 场景）", suffix(h_home) == "5/5", suffix(h_home))
    check("离线设备标了已离线", suffix(find(hidden, kind="device", id="d3")) == "已离线",
          suffix(find(hidden, kind="device", id="d3")))
    dlg.close()


def section_expand() -> None:
    print("\n2) 展开 / 收起")
    store, dlg = fresh()
    dev = dlg.tree_devices
    all_open = dev.expanded_keys()
    check("默认全展开（只算有子项的节点）", len(all_open) == 5, f"{len(all_open)} 个展开的节点")
    check("叶子不进展开集合",
          not (all_open & {dev._item_key(i) for i in dev._walk() if not i.childCount()}))

    find(dev, kind="home", key="我的家").setExpanded(False)
    dev.viewport().update()
    dlg._fill_order_trees_silently()
    check("重建后仍然收着", find(dev, kind="home", key="我的家").isExpanded() is False)
    check("别的家庭不受影响", find(dev, kind="home", key="办公室").isExpanded() is True)

    # 全折起来 → 在隐藏页点一次开关（会静默重填排序树）→ 不许自己撑开
    for item in dev._walk():
        if item.childCount():
            item.setExpanded(False)
    check("全折起来后集合为空", dev.expanded_keys() == set(), str(dev.expanded_keys()))
    dlg._fill_order_trees_silently()
    check("重填不会把折起来的撑开", dev.expanded_keys() == set(), str(dev.expanded_keys()))

    dev.restore_expanded(None)
    check("传 None = 全部展开",
          all(i.isExpanded() for i in dev._walk() if i.childCount()),
          f"{len(dev.expanded_keys())} 个展开")

    # 点折角：真的要点在折角上才算，点文字不能把行折起来
    home = find(dev, kind="home", key="我的家")
    QTest.mouseClick(dev.viewport(), Qt.LeftButton, Qt.NoModifier,
                     _chevron_hit(dev.visualItemRect(home)).center())
    QApplication.processEvents()
    check("点折角能收起", home.isExpanded() is False)
    QTest.mouseClick(dev.viewport(), Qt.LeftButton, Qt.NoModifier,
                     dev.visualItemRect(home).center() + QPoint(90, 0))
    QApplication.processEvents()
    check("点文字不收起", home.isExpanded() is False)
    dlg.close()


def section_order() -> None:
    print("\n3) 同层拖动 → 排序落盘")
    store, dlg = fresh()
    dev, scene = dlg.tree_devices, dlg.tree_scenes

    # 3.1 家庭重排
    dev._move(None, 0, 1)
    dlg._commit_device_tree()
    check("家庭顺序写进 prefs", store.prefs.home_order == ["办公室", "我的家"],
          str(store.prefs.home_order))
    check("store.homes() 跟着变", store.home_names() == ["办公室", "我的家"],
          str(store.home_names()))

    # 3.2 设备重排（只在客厅里）
    living = find(dev, kind="room", room="客厅")
    d1 = find(dev, kind="device", id="d1")
    dev._move(living, living.indexOfChild(d1), 1)
    dlg._commit_device_tree()
    key = Prefs.room_key("我的家", "客厅")
    check("设备顺序写进 prefs", store.prefs.device_order.get(key) == ["d2", "d1"],
          str(store.prefs.device_order.get(key)))
    check("房间内的顺序也真的换了",
          [d.did for d in store.device_states("我的家", "客厅")] == ["d2", "d1"],
          str([d.did for d in store.device_states("我的家", "客厅")]))
    check("别的房间顺序保持原样",
          store.prefs.device_order.get(Prefs.room_key("我的家", "卧室")) == ["d3"]
          and store.prefs.device_order.get(Prefs.room_key("办公室", "工位")) == ["d4"],
          str(store.prefs.device_order))

    # 3.3 场景重排
    s_home = find(scene, kind="home", key="我的家")
    scene._move(s_home, 1, 0)
    dlg._commit_scene_tree()
    check("场景顺序写进 prefs", store.prefs.scene_order.get("我的家") == ["s2", "s1"],
          str(store.prefs.scene_order.get("我的家")))
    check("首页拿到的场景顺序跟着变",
          [s["scene_id"] for s in store.scenes_for("我的家")] == ["s2", "s1"],
          str([s["scene_id"] for s in store.scenes_for("我的家")]))

    # 3.4 一次拖动只落一次盘
    reloaded = Prefs(PATH)
    check("三样都落盘了",
          reloaded.home_order == ["办公室", "我的家"] and reloaded.scene_order.get("我的家") == ["s2", "s1"],
          f"{reloaded.home_order} / {reloaded.scene_order.get('我的家')}")

    # 3.5 房间重排：拖完这一行不能没了
    #     Qt 的 startDrag 在 exec 返回 MoveAction 时会按**当前选择集**调
    #     clearOrRemove()。dropEvent 已经把 item 搬到新位置、选择集跟着它走，
    #     那一下就正好把刚放下的行再删一遍。所以 startDrag 必须自己实现。
    store, dlg = fresh()
    dev = dlg.tree_devices
    home = find(dev, kind="home", key="我的家")
    living = find(dev, kind="room", room="客厅")
    bed = find(dev, kind="room", room="卧室")
    want = {home.child(i).text(0) for i in range(home.childCount())}
    dev.setCurrentItem(living)
    dev.dropEvent(_Drop(QPoint(300, dev.visualItemRect(bed).bottom() - 4)))
    QApplication.processEvents()
    rooms = [home.child(i).text(0) for i in range(home.childCount())]
    check("房间拖完顺序变了", rooms == ["卧室", "客厅"], str(rooms))
    check("房间一个都没少", set(rooms) == want, f"{sorted(rooms)} vs {sorted(want)}")
    check("房间顺序落盘", store.prefs.room_order.get("我的家") == ["卧室", "客厅"],
          str(store.prefs.room_order.get("我的家")))
    src_drag = inspect.getsource(type(dev).startDrag)
    check("startDrag 是自己实现的", type(dev).startDrag is not QAbstractItemView.startDrag)
    check("startDrag 不调基类（基类会把源行删掉）", "super().startDrag" not in src_drag)

    # 3.6 重填中不写盘
    store, dlg = fresh()
    dev = dlg.tree_devices
    before = list(store.prefs.home_order)
    dlg._loading = True
    dev._move(None, 0, 1)
    dlg._commit_device_tree()
    dlg._loading = False
    check("重填过程中拖动不落盘", list(store.prefs.home_order) == before, str(store.prefs.home_order))
    dlg.close()


def section_cross_layer() -> None:
    print("\n4) 跨层拖动被拒")
    store, dlg = fresh()
    dev = dlg.tree_devices
    d1 = find(dev, kind="device", id="d1")
    d2 = find(dev, kind="device", id="d2")
    d3 = find(dev, kind="device", id="d3")
    office = find(dev, kind="home", key="办公室")

    dev.setCurrentItem(d1)
    check("拖到同房间另一台：允许", dev._same_parent(_Drop(center(dev, d2))) is True)
    check("拖到自己身上：不算", dev._same_parent(_Drop(center(dev, d1))) is False)
    check("拖到别的房间：拒绝", dev._same_parent(_Drop(center(dev, d3))) is False)
    check("拖到别的家庭：拒绝", dev._same_parent(_Drop(center(dev, office))) is False)
    check("拖到空白处（设备）：拒绝",
          dev._same_parent(_Drop(QPoint(10, dev.viewport().height() - 4))) is False)

    dev.setCurrentItem(office)
    check("拖到空白处（家庭）：挪到末尾",
          dev._same_parent(_Drop(QPoint(10, dev.viewport().height() - 4))) is True)

    # 跨层拖动真的不会改 prefs
    before = dict(store.prefs.device_order)
    dev.setCurrentItem(d1)
    dev.dropEvent(_Drop(center(dev, d3)))
    check("跨层 dropEvent 不改偏好", store.prefs.device_order == before,
          str(store.prefs.device_order))
    dlg.close()


def section_hidden() -> None:
    print("\n5) 隐藏页的聚合开关")
    store, dlg = fresh()
    tree = dlg.tree_hidden

    home = find(tree, kind="home", home="我的家")
    living = find(tree, kind="room", room="客厅")
    check("初始全显示", switch(home) == 1.0 and switch(living) == 1.0,
          f"{switch(home)}/{switch(living)}")

    # 点房间开关 → 房间里两台全隐藏
    QTest.mouseClick(tree.viewport(), Qt.LeftButton, Qt.NoModifier,
                     _switch_rect(tree.visualItemRect(living)).center())
    QApplication.processEvents()
    living = find(tree, kind="room", room="客厅")
    home = find(tree, kind="home", home="我的家")
    check("房间开关 → 0", switch(living) == 0.0, str(switch(living)))
    check("房间行变成 0/2", suffix(living) == "0/2", suffix(living))
    check("两台设备都关上了",
          switch(find(tree, kind="device", id="d1")) == 0.0
          and switch(find(tree, kind="device", id="d2")) == 0.0)
    check("家庭行按比例 3/5", suffix(home) == "3/5" and abs(float(switch(home)) - 0.6) < 1e-6,
          f"{suffix(home)} / {switch(home)}")
    check("prefs 里真的藏了", store.prefs.is_hidden("d1") and store.prefs.is_hidden("d2"))

    # 再点一次 → 全显示
    QTest.mouseClick(tree.viewport(), Qt.LeftButton, Qt.NoModifier,
                     _switch_rect(tree.visualItemRect(find(tree, kind="room", room="客厅"))).center())
    QApplication.processEvents()
    living = find(tree, kind="room", room="客厅")
    check("再点一次回到全显", switch(living) == 1.0 and suffix(living) == "2/2",
          f"{switch(living)} / {suffix(living)}")

    # 单台 → 房间停在中间
    QTest.mouseClick(tree.viewport(), Qt.LeftButton, Qt.NoModifier,
                     _switch_rect(tree.visualItemRect(find(tree, kind="device", id="d1"))).center())
    QApplication.processEvents()
    living = find(tree, kind="room", room="客厅")
    check("只藏一台时房间是半开", abs(float(switch(living)) - 0.5) < 1e-6, str(switch(living)))
    check("半开时的计数是 1/2", suffix(living) == "1/2", suffix(living))
    check("半开状态不是 0 也不是 1", 0.02 < float(switch(living)) < 0.98)

    # 家庭开关：还有没显示的 → 全部显示
    QTest.mouseClick(tree.viewport(), Qt.LeftButton, Qt.NoModifier,
                     _switch_rect(tree.visualItemRect(find(tree, kind="home", home="我的家"))).center())
    QApplication.processEvents()
    home = find(tree, kind="home", home="我的家")
    check("部分隐藏时点家庭 = 全部显示", switch(home) == 1.0 and suffix(home) == "5/5",
          f"{switch(home)} / {suffix(home)}")

    # 家庭开关：全显示时 → 全部隐藏（设备；场景不动）
    QTest.mouseClick(tree.viewport(), Qt.LeftButton, Qt.NoModifier,
                     _switch_rect(tree.visualItemRect(home)).center())
    QApplication.processEvents()
    home = find(tree, kind="home", home="我的家")
    check("全显时点家庭 = 设备全隐藏", suffix(home) == "2/5", suffix(home))
    check("场景没受影响", suffix(find(tree, kind="scene", id="s1")) == "",
          f"'{suffix(find(tree, kind='scene', id='s1'))}'")

    # 场景组开关
    QTest.mouseClick(tree.viewport(), Qt.LeftButton, Qt.NoModifier,
                     _switch_rect(tree.visualItemRect(find(tree, kind="scene_group"))).center())
    QApplication.processEvents()
    home = find(tree, kind="home", home="我的家")
    check("场景组一点全藏", switch(find(tree, kind="scene_group")) == 0.0)
    check("家庭行跟着变 0/5", suffix(home) == "0/5", suffix(home))
    check("场景隐藏进了 prefs",
          store.prefs.is_scene_hidden("s1") and store.prefs.is_scene_hidden("s2"))

    # 全部恢复显示
    dlg.btn_show_all.click()
    QApplication.processEvents()
    home = find(tree, kind="home", home="我的家")
    check("「全部恢复显示」拉回 5/5", suffix(home) == "5/5", suffix(home))
    check("prefs 两侧都清空了", store.prefs.hidden == [] and store.prefs.hidden_scenes == [],
          f"{store.prefs.hidden} / {store.prefs.hidden_scenes}")
    dlg.close()


def _px(img: QImage, dpr: float, x: float, y: float) -> QColor:
    return QColor(img.pixelColor(int(round(x * dpr)), int(round(y * dpr))))


def _diff(a: QColor, b: QColor) -> int:
    return max(abs(a.red() - b.red()), abs(a.green() - b.green()), abs(a.blue() - b.blue()))


def section_geometry() -> None:
    print("\n6) 几何与绘制")
    store, dlg = fresh()
    tree = dlg.tree_hidden
    dlg.nav.setCurrentRow(2)  # 切到隐藏页，让它真的拿到宽度
    QApplication.processEvents()

    check("行高常量 32", ROW_H == 32 and INDENT == 16, f"{ROW_H}/{INDENT}")
    # 缩进不由控件做：QTreeView 会给 indentation 区铺一层 palette.highlight()
    # 的方底色，选中时就是文字块左边那块深蓝。改由 delegate 自己算。
    check("控件自身不缩进（缩进交给 delegate）", tree.indentation() == 0,
          str(tree.indentation()))
    home = find(tree, kind="home", home="我的家")
    room = find(tree, kind="room", room="客厅")
    leaf = find(tree, kind="device", id="d1")
    check("实际行高 = 32", tree.visualItemRect(home).height() == ROW_H,
          str(tree.visualItemRect(home).height()))
    x_home = row_x(tree.visualItemRect(home), 0)
    x_room = row_x(tree.visualItemRect(room), 1)
    x_leaf = row_x(tree.visualItemRect(leaf), 2)
    check("三层缩进逐级加 16",
          x_room - x_home == INDENT and x_leaf - x_room == INDENT,
          f"{x_home} / {x_room} / {x_leaf}")

    img = tree.viewport().grab().toImage()
    dpr = img.width() / max(1, tree.viewport().width())
    check("抓得到画面", img.width() > 100 and img.height() > 100,
          f"{img.width()}x{img.height()} dpr={dpr:.2f}")

    # 折角：有子项的行左边有笔画，叶子那一段是空的
    def chev_ink(item) -> int:
        rect = tree.visualItemRect(item)
        box_x = rect.x() + 2
        bg = _px(img, dpr, box_x + 1, rect.y() + 2)
        ink = 0
        for y in range(rect.y() + 8, rect.y() + 24):
            for x in range(box_x, box_x + CHEV + 4):
                if _diff(_px(img, dpr, x, y), bg) > 24:
                    ink += 1
        return ink

    check("有子项的行画了折角", chev_ink(home) > 5, f"{chev_ink(home)} px")
    check("叶子行不画折角", chev_ink(leaf) == 0, f"{chev_ink(leaf)} px")

    # 开关配色：全开是绿的，半开/全关是灰轨
    def track(item) -> QColor:
        """取轨道颜色。滑块是白的，占左或占右，所以两点里挑没被占的那个。"""
        rect = tree.visualItemRect(item)
        sw = _switch_rect(rect)
        y = sw.center().y()
        left = _px(img, dpr, sw.x() + 4, y)
        right = _px(img, dpr, sw.right() - 5, y)
        return left if _diff(right, QColor("#FFFFFF")) < 8 else right

    on = track(home)
    check("全开的开关是绿的", _diff(on, QColor(theme.TOGGLE_ON)) < 12, on.name())
    store.prefs.set_hidden("d1", True)
    dlg._fill_hidden_tree()
    QApplication.processEvents()
    img = tree.viewport().grab().toImage()
    half = track(find(tree, kind="room", room="客厅"))
    check("半开的开关不是绿的", _diff(half, QColor(theme.TOGGLE_ON)) > 24, half.name())
    off = track(find(tree, kind="device", id="d1"))
    check("关掉的开关也是灰轨", _diff(off, QColor(theme.TOGGLE_ON)) > 24, off.name())

    # 选中一块看行首：QTreeView 曾经把缩进区铺成 palette.highlight() 的方底色
    # （浅色主题下是 #3d7eff），和这边自绘的圆角块并排，就成了"蓝色方块 + 浅蓝
    # 长条"两个选中指示打架。缩进改由 delegate 自算后，整行都归圆角块。
    room = find(tree, kind="room", room="客厅")  # 重填过，旧 item 已经没了
    tree.setCurrentItem(room)
    QApplication.processEvents()
    img = tree.viewport().grab().toImage()
    ey = tree.visualItemRect(room).center().y()
    head = _px(img, dpr, 3, ey)
    check("选中块铺到行首", _diff(head, QColor(theme.LIGHT.accent_soft)) < 12, head.name())
    blue = sum(1 for x in range(0, 80)
               if _diff(_px(img, dpr, x, ey), QColor("#3d7eff")) < 6)
    check("缩进区没有系统高亮蓝", blue == 0, f"{blue} px")
    dlg.close()


def section_rail() -> None:
    print("\n7) 设置页分类栏分层")
    # 这一节是"上一轮的侧栏同色问题"在设置页的复发点：设置窗口的内容区是
    # surface_bg（比主界面 pal.bg 亮/暗一档），直接套 NAV_BG 差不到 3 个明度档。
    for pal in (theme.LIGHT, theme.DARK):
        store, dlg = fresh(pal)
        dlg.nav.setCurrentRow(2)
        QApplication.processEvents()

        img = dlg.grab().toImage()
        dpr = img.width() / max(1, dlg.width())

        def px(widget, x: float, y: float) -> QColor:
            pt = widget.mapTo(dlg, QPoint(int(x), int(y)))
            return _px(img, dpr, pt.x(), pt.y())

        rail = dlg._rail
        bar = px(rail, 30, rail.height() - 20)  # 栏底：左下角往里，避开文字
        # 窗口比面板大一圈（浮窗阴影留白），底色要取面板内的那一条，
        # 取窗口边缘会落到透明阴影上（grab 出来是纯黑）
        canvas = px(dlg, dlg.width() - dlg._frame.shadow_pad - 6, 300)
        gap = abs(bar.lightness() - canvas.lightness())
        check(f"{pal.name}：分类栏与内容区不同色（Δ ≥ 12）", gap >= 12,
              f"栏 {bar.name()} L={bar.lightness()} / 内容 {canvas.name()} L={canvas.lightness()}")

        # 圆角：角上那一点仍落在窗口底色上，说明底色块是圆角而不是一块方底
        for name, (dx, dy) in (("左上", (1, 1)), ("左下", (1, rail.height() - 2))):
            corner = px(rail, dx, dy)
            check(f"{pal.name}：分类栏{name}角是圆的", _diff(corner, canvas) <= 6,
                  f"{corner.name()} vs 底 {canvas.name()}")

        sel = dlg.nav.visualItemRect(dlg.nav.item(2))
        sel_color = px(dlg.nav, sel.left() + 4, sel.center().y())
        check(f"{pal.name}：选中行比栏底亮一层", sel_color.lightness() > bar.lightness() + 4,
              f"{sel_color.name()} L={sel_color.lightness()} vs {bar.lightness()}")

        idle_y = dlg.nav.visualItemRect(dlg.nav.item(1)).center().y()
        idle = px(dlg.nav, sel.left() + 4, idle_y)
        check(f"{pal.name}：未选中的行不铺底", _diff(idle, bar) <= 6, idle.name())

        check(f"{pal.name}：栏宽 132、列表四周内缩 6",
              (rail.width(), dlg.nav.width()) == (132, 120),
              f"{rail.width()}/{dlg.nav.width()}")
        dlg.close()


def section_panels() -> None:
    print("\n8) 设置页的行式排版（标题 → 圆角卡 → 行）")
    # 四个页面现在共用一套骨架（ui/settings_rows.py）：分区标题在卡片**外面**，
    # 卡片是圆角的容器，卡里一行行控件用 1px 线分开。这一节既验结构（谁的父是
    # 谁、行数 / 分隔线数对不对），也在真实窗口上抓图验观感（卡片 vs 窗口底色、
    # 圆角、树的内缩、选中行在卡片上读不读得出来）。
    for pal in (theme.LIGHT, theme.DARK):
        store, dlg = fresh(pal)

        heads = [lab.text() for lab in dlg.findChildren(QLabel, "PageTitle")]
        check(f"{pal.name}：五页都有大标题",
              sorted(heads) == sorted(["外观", "排序", "隐藏", "账号", "常规"]), "、".join(heads))

        # -- 结构：每页的卡片 --------------------------------
        def cards_of(index: int) -> list:
            return dlg.pages.widget(index).findChildren(QFrame, "SectionCard")

        def show_page(index: int) -> None:
            """切到那一页再量：隐藏的页从来没跑过布局，子控件的几何是默认值 640×480。"""
            dlg.nav.setCurrentRow(index)
            for _ in range(3):
                QApplication.processEvents()

        check(f"{pal.name}：外观页三张卡（主题 / 家庭标签 / 摄像头）", len(cards_of(0)) == 3,
              f"{len(cards_of(0))} 张")
        check(f"{pal.name}：排序页两张卡（设备树 / 场景树）", len(cards_of(1)) == 2,
              f"{len(cards_of(1))} 张")
        check(f"{pal.name}：隐藏页一张卡", len(cards_of(2)) == 1, f"{len(cards_of(2))} 张")
        check(f"{pal.name}：账号页一张卡", len(cards_of(3)) == 1, f"{len(cards_of(3))} 张")

        # 分区标题必须在卡片外面：父控件不是卡，纵坐标在卡上方
        show_page(1)
        order_page = dlg.pages.widget(1)
        scene_card = dlg.tree_scenes.parentWidget()
        titles = [lab for lab in order_page.findChildren(QLabel, "SectionTitle")]
        check(f"{pal.name}：排序页两条分区标题", len(titles) == 2, f"{len(titles)} 条")
        if titles and scene_card is not None:
            check(f"{pal.name}：分区标题不装在卡片里",
                  all(lab.parentWidget() is not scene_card for lab in titles))
            last = max(titles, key=lambda lab: lab.mapTo(dlg, QPoint(0, 0)).y())
            gap = (scene_card.mapTo(dlg, QPoint(0, 0)).y()
                   - last.mapTo(dlg, QPoint(0, 0)).y())
            check(f"{pal.name}：标题在卡片上方", 0 < gap <= 40, f"间距 {gap}")

        # 外观页："家庭标签"卡里两行（排列 / 激活样式）+ 预览；三段 → 两条线
        show_page(0)
        tags = cards_of(0)[1]
        check(f"{pal.name}：家庭标签卡 2 行 + 预览 + 2 条线",
              (len(tags.findChildren(SettingsRow)),
               len(tags.findChildren(QFrame, "RowDivider"))) == (2, 2),
              f"{len(tags.findChildren(SettingsRow))} 行 / "
              f"{len(tags.findChildren(QFrame, 'RowDivider'))} 线")
        # 定宽是"看起来齐"的关键：两个下拉框必须等宽。QSS 的 min-width/padding 会和
        # setFixedWidth 抢 2px，所以量的是"等宽且接近设定值"，不是精确相等
        w1, w2 = dlg.cb_placement.width(), dlg.cb_style.width()
        check(f"{pal.name}：两个下拉框等宽且接近 {COMBO_W}",
              w1 == w2 and abs(w1 - COMBO_W) <= 4, f"{w1}/{w2}")

        # 账号页 3 行 + 2 条分隔线；可见的行高不小于 ROW_MIN_H（52）。
        # 别写成 ROW_H——那是 ui/tree.py 的树行高（32），拿来当设置行下限会假绿
        show_page(3)
        acc = cards_of(3)[0]
        rows = acc.findChildren(SettingsRow)
        seps = acc.findChildren(QFrame, "RowDivider")
        check(f"{pal.name}：账号页 3 行、2 条分隔线", (len(rows), len(seps)) == (3, 2),
              f"{len(rows)} 行 / {len(seps)} 线")
        check(f"{pal.name}：行高不小于 {ROW_MIN_H}",
              all(r.height() >= ROW_MIN_H for r in rows),
              "、".join(str(r.height()) for r in rows))
        check(f"{pal.name}：值标签右对齐",
              all(r.findChild(QLabel, "RowValue") is not None for r in rows[:2])
              and bool(rows[0].findChild(QLabel, "RowValue").alignment() & Qt.AlignRight))
        # 只给 expand 行传 stretch 是必须的：addWidget 一旦带 alignment，ElidedLabel
        # 会被压成 12×12，"凭据文件"那行看着是空的
        check(f"{pal.name}：值铺满到行尾", dlg.lbl_path.width() >= 200,
              f"{dlg.lbl_path.width()} px")

        # 窗口拉到最小时，五页内容都不能被裁（外观页最高，最小高度就是按它定的）
        dlg.resize(dlg.minimumSize())
        for index, name in enumerate(("外观", "排序", "隐藏", "账号", "常规")):
            show_page(index)
            page = dlg.pages.widget(index)
            need = page.layout().minimumSize().height()
            check(f"{pal.name}：最小窗口下「{name}」页不被裁",
                  need <= page.height(), f"需要 {need} / 有 {page.height()}")

        # -- 观感：排序页两块卡 ------------------------------
        show_page(1)
        img = dlg.grab().toImage()
        dpr = img.width() / max(1, dlg.width())

        def px(widget, x: float, y: float) -> QColor:
            pt = widget.mapTo(dlg, QPoint(int(x), int(y)))
            return _px(img, dpr, pt.x(), pt.y())

        dev = dlg.tree_devices.parentWidget()
        check(f"{pal.name}：树住在 SectionCard 里",
              dev is not None and dev.objectName() == "SectionCard")
        if dev is None or scene_card is None:
            dlg.close()
            continue

        # 卡底：取右侧那条 10px 内缩留白（树在里面，行画不到这里），
        # y 取底部往上 20——已经离开圆角弧线，横向是直的
        bar = px(dev, dev.width() - 4, dev.height() - 20)
        # 窗口底色：卡片右边那一条页边距（页右边距 26，取 8 进去仍在底色上）
        base = dev.mapTo(dlg, QPoint(dev.width() + 8, 300))
        canvas = _px(img, dpr, base.x(), base.y())
        gap = abs(bar.lightness() - canvas.lightness())
        check(f"{pal.name}：卡片与窗口底色不同（Δ ≥ 12）", gap >= 12,
              f"卡 {bar.name()} L={bar.lightness()} / 底 {canvas.name()} L={canvas.lightness()}")

        # 两块要一个颜色，不能一块白一块灰
        bar2 = px(scene_card, scene_card.width() - 4, scene_card.height() - 20)
        check(f"{pal.name}：两块卡片同色", _diff(bar, bar2) <= 3,
              f"{bar.name()} vs {bar2.name()}")

        # 圆角：角上那点仍是窗口底色，说明画的是圆角而不是一块方底
        for corner, (dx, dy) in (("左上", (1, 1)), ("右上", (dev.width() - 2, 1)),
                                 ("左下", (1, dev.height() - 2))):
            got = px(dev, dx, dy)
            check(f"{pal.name}：卡片{corner}角是圆的", _diff(got, canvas) <= 8,
                  f"{got.name()} vs 底 {canvas.name()}")

        # 树被内缩进卡片里（padding 10）
        dev_in = dlg.tree_devices.geometry()
        right = dev.width() - dev_in.x() - dev_in.width()
        check(f"{pal.name}：树在卡片内缩进 10 / 10", (dev_in.x(), right) == (10, 10),
              f"左 {dev_in.x()} / 右 {right}")

        # 选中行在卡片上仍然读得出来（深色下 accent_soft 比卡底还暗就毁了）
        tree = dlg.tree_devices
        home = tree.topLevelItem(0)
        if home is not None and home.childCount():
            tree.setCurrentItem(home.child(0))
            QApplication.processEvents()
            img = dlg.grab().toImage()
            dpr = img.width() / max(1, dlg.width())
            row_y = tree.visualItemRect(home.child(0)).center().y()
            row = px(tree, 6, row_y)
            check(f"{pal.name}：选中行在卡片上可辨（Δ ≥ 8）",
                  abs(row.lightness() - bar.lightness()) >= 8,
                  f"{row.name()} L={row.lightness()} vs 卡 {bar.lightness()}")
        dlg.close()


def main() -> int:
    app = QApplication(sys.argv[:1])  # noqa: F841
    theme.apply_to_app(theme.LIGHT)

    section_structure()
    section_expand()
    section_order()
    section_cross_layer()
    section_hidden()
    section_geometry()
    section_rail()
    section_panels()

    for dlg in OPEN:
        dlg.close()
    PATH.unlink(missing_ok=True)

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败")
        for line in FAILS:
            print("  -", line)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
