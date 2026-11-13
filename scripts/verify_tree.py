# SPDX-License-Identifier: GPL-3.0-or-later
"""设置页两棵树（排序 / 隐藏）的离线验证。

八件事：

1. **结构**：排序页缩窄成"家庭 → 房间"两层（设备层已移除），场景是家庭 → 场景
   两层；隐藏页和排序页同一套骨架，两个互斥 tab：设备树"家庭 → 房间 → 设备"
   三层，场景树"家庭 → 快捷场景"两层（不再有「快捷场景」分组行）；
2. **展开状态**：折起来之后再重建，还保持折起来；
3. **排序**：同层拖动后 ``apply_order_snapshot`` 真的落进了 prefs（并且只落一次盘），
   重填过程中（``_loading``）不写；「恢复默认」仍会清掉各房间的设备顺序；
4. **跨层拖动被拒**：把房间拖进别的家庭 = 改归属，不在这个程序的职责里；
5. **隐藏页聚合开关**：全显 → 全隐 → 部分态停在中间；设备树和场景树各管各的，
   家庭那一层只聚合本 tab 的条目；「全部恢复显示」也按 tab 分开；
6. **几何与绘制**：行高 32 / 缩进 16，折角只画在有子项的行上，开关按状态上色；
7. **分类栏分层**：分类栏和内容区曾经同色（内容区是 ``surface_bg``，直接套主界面
   那套 ``NAV_BG`` 只差两三个明度档），这一节在真实窗口上抓图量"栏底 / 内容区 /
   选中行"三处，并把圆角也一起验了；
8. **分区面板**：排序页那两块（家庭 / 房间树、场景树）以前直接摊在窗口底色上，深色下
   几乎看不出边界。这一节量面板底 vs 窗口底色、四个角是不是圆的、树有没有内缩、
   选中行在面板上还看不看得出来。

用法：``.venv/Scripts/python.exe -X utf8 scripts/verify_tree.py``
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QMouseEvent  # noqa: E402
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
        DeviceNode("d1", "主灯", "m", "light.x", home="我的家", room="客厅",
                   home_id="h1", room_id="r1"),
        DeviceNode("d2", "台灯", "m", "light.x", home="我的家", room="客厅",
                   home_id="h1", room_id="r1"),
        DeviceNode("d3", "床灯", "m", "light.x", home="我的家", room="卧室",
                   home_id="h1", room_id="r2", online=False),
        DeviceNode("d4", "吸顶灯", "m", "light.x", home="办公室", room="工位",
                   home_id="h2", room_id="r3"),
    ]
    store.topology = Topology(homes, devices)
    for node in devices:
        store.devices[node.did] = DeviceState(node=node, spec=None, card=build_card_empty(node))
    store.scenes_by_home = {
        "h1": [
            {"scene_id": "s1", "name": "回家", "home_id": "h1", "enable": True},
            {"scene_id": "s2", "name": "离家", "home_id": "h1", "enable": True},
        ],
        "h2": [],
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


def _mouse(tree, kind, pos: QPoint, button, buttons) -> None:
    event = QMouseEvent(
        kind, QPointF(pos), QPointF(tree.viewport().mapToGlobal(pos)),
        button, buttons, Qt.NoModifier,
    )
    if kind == QEvent.MouseButtonPress:
        tree.mousePressEvent(event)
    elif kind == QEvent.MouseMove:
        tree.mouseMoveEvent(event)
    else:
        tree.mouseReleaseEvent(event)


def drag(tree, src, target: QPoint) -> None:
    """按首页卡片那套手势拖一行：按下 → 超过阈值 → 移动 → 松手。"""
    rect = tree.visualItemRect(src)
    start = QPoint(120, rect.center().y())
    _mouse(tree, QEvent.MouseButtonPress, start, Qt.LeftButton, Qt.LeftButton)
    _mouse(tree, QEvent.MouseMove, target, Qt.NoButton, Qt.LeftButton)
    _mouse(tree, QEvent.MouseButtonRelease, target, Qt.LeftButton, Qt.NoButton)
    QApplication.processEvents()


def center(tree, item) -> QPoint:
    return tree.visualItemRect(item).center()


# ---------------------------------------------------------------------------
def section_structure() -> None:
    print("1) 两层结构（家庭 → 房间）+ 场景")
    store, dlg = fresh()
    rooms, scene = dlg.tree_rooms, dlg.tree_scenes
    hidden_dev, hidden_sc = dlg.tree_hidden_devices, dlg.tree_hidden_scenes

    check("排序树 = 两个家庭", rooms.topLevelItemCount() == 2, f"{rooms.topLevelItemCount()} 行")
    home = find(rooms, kind="home", key="h1")
    check("家庭行找得到", home is not None)
    check("家庭下两个房间", home.childCount() == 2,
          str([home.child(i).text(0) for i in range(2)]))
    living = find(rooms, kind="room", room="r1")
    check("房间是叶子（设备层已移除）", living.childCount() == 0)
    check("排序树里没有任何设备行", find(rooms, kind="device") is None)
    check("房间行带台数", suffix(living) == "2 台", suffix(living))

    check("场景树也是两个家庭", scene.topLevelItemCount() == 2)
    s_home = find(scene, kind="home", key="h1")
    check("场景树只有两层", s_home.child(0).childCount() == 0)
    check("我的家两个场景", s_home.childCount() == 2,
          str([s_home.child(i).text(0) for i in range(2)]))
    check("没场景的家庭是空行", find(scene, kind="home", key="h2").childCount() == 0)

    check("隐藏页设备树 = 两个家庭", hidden_dev.topLevelItemCount() == 2)
    h_home = find(hidden_dev, kind="home", home="h1")
    check("设备树家庭下只有两个房间（场景不在这一 tab）", h_home.childCount() == 2,
          str([h_home.child(i).text(0) for i in range(2)]))
    check("设备树里没有场景行", find(hidden_dev, kind="scene") is None)
    check("设备树家庭行有 3/3", suffix(h_home) == "3/3", suffix(h_home))
    check("离线设备标了已离线", suffix(find(hidden_dev, kind="device", id="d3")) == "已离线",
          suffix(find(hidden_dev, kind="device", id="d3")))

    check("隐藏页场景树 = 两个家庭", hidden_sc.topLevelItemCount() == 2)
    s_h = find(hidden_sc, kind="scene_home", home="h1")
    check("场景树家庭下是两个场景叶子", s_h.childCount() == 2,
          str([s_h.child(i).text(0) for i in range(2)]))
    check("场景树没有「快捷场景」分组行", find(hidden_sc, kind="scene_group") is None)
    check("场景树家庭行有 2/2", suffix(s_h) == "2/2", suffix(s_h))
    check("没场景的家庭是 0/0", suffix(find(hidden_sc, kind="scene_home", home="h2")) == "0/0",
          suffix(find(hidden_sc, kind="scene_home", home="h2")))
    dlg.close()


def section_expand() -> None:
    print("\n2) 展开 / 收起")
    store, dlg = fresh()
    rooms = dlg.tree_rooms
    all_open = rooms.expanded_keys()
    check("默认全展开（只算有子项的节点）", len(all_open) == 2, f"{len(all_open)} 个展开的节点")
    check("叶子不进展开集合",
          not (all_open & {rooms._item_key(i) for i in rooms._walk() if not i.childCount()}))

    find(rooms, kind="home", key="h1").setExpanded(False)
    rooms.viewport().update()
    dlg._fill_order_trees_silently()
    check("重建后仍然收着", find(rooms, kind="home", key="h1").isExpanded() is False)
    check("别的家庭不受影响", find(rooms, kind="home", key="h2").isExpanded() is True)

    # 全折起来 → 在隐藏页点一次开关（会静默重填排序树）→ 不许自己撑开
    for item in rooms._walk():
        if item.childCount():
            item.setExpanded(False)
    check("全折起来后集合为空", rooms.expanded_keys() == set(), str(rooms.expanded_keys()))
    dlg._fill_order_trees_silently()
    check("重填不会把折起来的撑开", rooms.expanded_keys() == set(), str(rooms.expanded_keys()))

    rooms.restore_expanded(None)
    check("传 None = 全部展开",
          all(i.isExpanded() for i in rooms._walk() if i.childCount()),
          f"{len(rooms.expanded_keys())} 个展开")

    # 点折角：真的要点在折角上才算，点文字不能把行折起来
    home = find(rooms, kind="home", key="h1")
    QTest.mouseClick(rooms.viewport(), Qt.LeftButton, Qt.NoModifier,
                     _chevron_hit(rooms.visualItemRect(home)).center())
    QApplication.processEvents()
    check("点折角能收起", home.isExpanded() is False)
    QTest.mouseClick(rooms.viewport(), Qt.LeftButton, Qt.NoModifier,
                     rooms.visualItemRect(home).center() + QPoint(90, 0))
    QApplication.processEvents()
    check("点文字不收起", home.isExpanded() is False)
    dlg.close()


def section_order() -> None:
    print("\n3) 同层拖动 → 排序落盘")
    store, dlg = fresh()
    rooms, scene = dlg.tree_rooms, dlg.tree_scenes

    # 3.1 家庭重排
    rooms._move(None, 0, 1)
    dlg._commit_room_tree()
    check("家庭顺序写进 prefs", store.prefs.home_order == ["h2", "h1"],
          str(store.prefs.home_order))
    check("store.homes() 跟着变", store.home_ids() == ["h2", "h1"],
          str(store.home_ids()))

    # 3.2 房间重排（只在 h1 里）
    home = find(rooms, kind="home", key="h1")
    living = find(rooms, kind="room", room="r1")
    rooms._move(home, home.indexOfChild(living), 1)
    dlg._commit_room_tree()
    check("房间顺序写进 prefs", store.prefs.room_order.get("h1") == ["r2", "r1"],
          str(store.prefs.room_order.get("h1")))
    check("房间内的顺序也真的换了",
          store.room_ids("h1") == ["r2", "r1"],
          str(store.room_ids("h1")))
    check("排序页不再写设备顺序", store.prefs.device_order == {},
          str(store.prefs.device_order))
    check("设备仍按首页拖好的原顺序",
          [d.did for d in store.device_states("h1", "r1")] == ["d1", "d2"],
          str([d.did for d in store.device_states("h1", "r1")]))

    # 3.3 场景重排
    s_home = find(scene, kind="home", key="h1")
    scene._move(s_home, 1, 0)
    dlg._commit_scene_tree()
    check("场景顺序写进 prefs", store.prefs.scene_order.get("h1") == ["s2", "s1"],
          str(store.prefs.scene_order.get("h1")))
    check("首页拿到的场景顺序跟着变",
          [s["scene_id"] for s in store.scenes_for("h1")] == ["s2", "s1"],
          str([s["scene_id"] for s in store.scenes_for("h1")]))

    # 3.4 一次拖动只落一次盘
    reloaded = Prefs(PATH)
    check("三样都落盘了",
          reloaded.home_order == ["h2", "h1"]
          and reloaded.room_order.get("h1") == ["r2", "r1"]
          and reloaded.scene_order.get("h1") == ["s2", "s1"],
          f"{reloaded.home_order} / {reloaded.room_order.get('h1')} / "
          f"{reloaded.scene_order.get('h1')}")

    # 3.5 房间重排：拖完这一行不能没了
    #     Qt 的 startDrag 在 exec 返回 MoveAction 时会按**当前选择集**调
    #     clearOrRemove()。dropEvent 已经把 item 搬到新位置、选择集跟着它走，
    #     那一下就正好把刚放下的行再删一遍。所以 startDrag 必须自己实现。
    store, dlg = fresh()
    rooms = dlg.tree_rooms
    home = find(rooms, kind="home", key="h1")
    living = find(rooms, kind="room", room="r1")
    bed = find(rooms, kind="room", room="r2")
    want = {home.child(i).text(0) for i in range(home.childCount())}
    drag(rooms, living, QPoint(300, rooms.visualItemRect(bed).bottom() - 4))
    got = [home.child(i).text(0) for i in range(home.childCount())]
    check("房间拖完顺序变了", got == ["卧室", "客厅"], str(got))
    check("房间一个都没少", set(got) == want, f"{sorted(got)} vs {sorted(want)}")
    check("房间顺序落盘", store.prefs.room_order.get("h1") == ["r2", "r1"],
          str(store.prefs.room_order.get("h1")))
    check("拖动结束后幽灵行已清理", rooms._drag_ghost is None)
    check("拖动结束后落位线已清理", rooms._drop_line is None)
    check("不再启用 Qt 原生拖放",
          rooms.dragDropMode() == QAbstractItemView.NoDragDrop,
          str(rooms.dragDropMode()))

    # 3.5b 拖动过程中的幽灵行 / 落位线 / 边缘自动滚
    store, dlg = fresh()
    dlg.show()
    dlg.nav.setCurrentRow(1)
    for _ in range(3):
        QApplication.processEvents()
    rooms = dlg.tree_rooms
    rooms.clear()
    for i in range(40):
        rooms.add_row(None, f"家庭{i}", {"kind": "home", "key": f"h{i}"})
    rooms.resize(320, 180)
    QApplication.processEvents()
    src = rooms.topLevelItem(0)
    start = QPoint(120, rooms.visualItemRect(src).center().y())
    edge = QPoint(120, rooms.viewport().height() - 4)
    _mouse(rooms, QEvent.MouseButtonPress, start, Qt.LeftButton, Qt.LeftButton)
    _mouse(rooms, QEvent.MouseMove, edge, Qt.NoButton, Qt.LeftButton)
    QApplication.processEvents()
    check("拖动时出现幽灵行",
          rooms._drag_ghost is not None and rooms._drag_ghost.isVisible())
    check("拖动时出现落位线",
          rooms._drop_line is not None and rooms._drop_line.isVisible())
    before = rooms.verticalScrollBar().value()
    rooms._auto_scroll()
    check("拖到边缘会自动滚动",
          rooms.verticalScrollBar().value() > before,
          f"{before} → {rooms.verticalScrollBar().value()}")
    _mouse(rooms, QEvent.MouseButtonRelease, edge, Qt.LeftButton, Qt.NoButton)
    QApplication.processEvents()
    check("松手后幽灵行 / 落位线都清掉",
          rooms._drag_ghost is None and rooms._drop_line is None)
    dlg.close()

    # 3.6 重填中不写盘
    store, dlg = fresh()
    rooms = dlg.tree_rooms
    before = list(store.prefs.home_order)
    dlg._loading = True
    rooms._move(None, 0, 1)
    dlg._commit_room_tree()
    dlg._loading = False
    check("重填过程中拖动不落盘", list(store.prefs.home_order) == before, str(store.prefs.home_order))

    # 3.7 恢复默认仍然清得掉设备顺序（设备层不在页面上，但能力不能丢）
    store.prefs.set_device_order("h1", "r1", ["d2", "d1"])
    store.prefs.set_room_order("h1", ["r2", "r1"])
    dlg._reset_layout()
    check("恢复默认清空家庭 / 房间顺序",
          store.prefs.home_order == [] and "h1" not in store.prefs.room_order,
          f"{store.prefs.home_order} / {store.prefs.room_order}")
    check("恢复默认也清空各房间设备顺序", store.prefs.device_order == {},
          str(store.prefs.device_order))
    check("设备回到云端顺序",
          [d.did for d in store.device_states("h1", "r1")] == ["d1", "d2"],
          str([d.did for d in store.device_states("h1", "r1")]))
    dlg.close()


def section_cross_layer() -> None:
    print("\n4) 跨层拖动被拒")
    store, dlg = fresh()
    rooms = dlg.tree_rooms
    living = find(rooms, kind="room", room="r1")
    bed = find(rooms, kind="room", room="r2")
    office = find(rooms, kind="home", key="h2")
    desk = find(rooms, kind="room", room="r3")

    check("拖到同家庭另一房间：允许",
          rooms._drop_target(living, center(rooms, bed))[1] >= 0)
    check("拖到自己身上：不算",
          rooms._drop_target(living, center(rooms, living))[1] < 0)
    check("拖到别的家庭：拒绝",
          rooms._drop_target(living, center(rooms, office))[1] < 0)
    check("拖到别的家庭的房间：拒绝",
          rooms._drop_target(living, center(rooms, desk))[1] < 0)
    check("拖到空白处（房间）：拒绝",
          rooms._drop_target(
              living, QPoint(10, rooms.viewport().height() - 4))[1] < 0)
    check("拖到空白处（家庭）：挪到末尾",
          rooms._drop_target(
              office, QPoint(10, rooms.viewport().height() - 4))[1] >= 0)

    # 跨层拖动真的不会改 prefs
    before_rooms = {k: list(v) for k, v in store.prefs.room_order.items()}
    before_homes = list(store.prefs.home_order)
    drag(rooms, living, center(rooms, desk))
    check("跨层拖动不改偏好",
          store.prefs.room_order == before_rooms and store.prefs.home_order == before_homes,
          f"{store.prefs.room_order} / {store.prefs.home_order}")
    dlg.close()


def section_hidden() -> None:
    print("\n5) 隐藏页：设备 / 场景两个 tab 的开关")
    store, dlg = fresh()
    dlg.nav.setCurrentRow(2)
    QApplication.processEvents()
    dev, sc = dlg.tree_hidden_devices, dlg.tree_hidden_scenes

    home = find(dev, kind="home", home="h1")
    living = find(dev, kind="room", room="r1")
    check("初始全显示", switch(home) == 1.0 and switch(living) == 1.0,
          f"{switch(home)}/{switch(living)}")
    check("默认停在设备 tab", dlg.hidden_stack.currentWidget() is dev)
    check("设备 tab 只露出设备那枚恢复按钮",
          not dlg.btn_show_devices.isHidden() and dlg.btn_show_scenes.isHidden())

    # 点房间开关 → 房间里两台全隐藏
    QTest.mouseClick(dev.viewport(), Qt.LeftButton, Qt.NoModifier,
                     _switch_rect(dev.visualItemRect(living)).center())
    QApplication.processEvents()
    living = find(dev, kind="room", room="r1")
    home = find(dev, kind="home", home="h1")
    check("房间开关 → 0", switch(living) == 0.0, str(switch(living)))
    check("房间行变成 0/2", suffix(living) == "0/2", suffix(living))
    check("两台设备都关上了",
          switch(find(dev, kind="device", id="d1")) == 0.0
          and switch(find(dev, kind="device", id="d2")) == 0.0)
    check("家庭行按比例 1/3", suffix(home) == "1/3" and abs(float(switch(home)) - 1 / 3) < 1e-6,
          f"{suffix(home)} / {switch(home)}")
    check("prefs 里真的藏了", store.prefs.is_hidden("d1") and store.prefs.is_hidden("d2"))

    # 再点一次 → 全显示
    QTest.mouseClick(dev.viewport(), Qt.LeftButton, Qt.NoModifier,
                     _switch_rect(dev.visualItemRect(find(dev, kind="room", room="r1"))).center())
    QApplication.processEvents()
    living = find(dev, kind="room", room="r1")
    check("再点一次回到全显", switch(living) == 1.0 and suffix(living) == "2/2",
          f"{switch(living)} / {suffix(living)}")

    # 单台 → 房间停在中间
    QTest.mouseClick(dev.viewport(), Qt.LeftButton, Qt.NoModifier,
                     _switch_rect(dev.visualItemRect(find(dev, kind="device", id="d1"))).center())
    QApplication.processEvents()
    living = find(dev, kind="room", room="r1")
    check("只藏一台时房间是半开", abs(float(switch(living)) - 0.5) < 1e-6, str(switch(living)))
    check("半开时的计数是 1/2", suffix(living) == "1/2", suffix(living))
    check("半开状态不是 0 也不是 1", 0.02 < float(switch(living)) < 0.98)

    # 家庭开关：还有没显示的 → 全部显示
    QTest.mouseClick(dev.viewport(), Qt.LeftButton, Qt.NoModifier,
                     _switch_rect(dev.visualItemRect(find(dev, kind="home", home="h1"))).center())
    QApplication.processEvents()
    home = find(dev, kind="home", home="h1")
    check("部分隐藏时点家庭 = 全部显示", switch(home) == 1.0 and suffix(home) == "3/3",
          f"{switch(home)} / {suffix(home)}")

    # 家庭开关：全显示时 → 只隐藏这一 tab 的设备，场景不动
    QTest.mouseClick(dev.viewport(), Qt.LeftButton, Qt.NoModifier,
                     _switch_rect(dev.visualItemRect(home)).center())
    QApplication.processEvents()
    home = find(dev, kind="home", home="h1")
    check("全显时点家庭 = 设备全隐藏", suffix(home) == "0/3", suffix(home))
    check("设备 tab 不影响场景",
          store.prefs.hidden_scenes == [] and suffix(find(sc, kind="scene_home", home="h1")) == "2/2",
          f"{store.prefs.hidden_scenes} / {suffix(find(sc, kind='scene_home', home='h1'))}")

    # -- 切到场景 tab ------------------------------------------------
    dlg._switch_hidden_tab("scenes")
    QApplication.processEvents()
    check("切到场景 tab 只显示场景树", dlg.hidden_stack.currentWidget() is sc)
    check("场景 tab 只露出场景那枚恢复按钮",
          dlg.btn_show_devices.isHidden() and not dlg.btn_show_scenes.isHidden())

    s_home = find(sc, kind="scene_home", home="h1")
    check("场景初始全显示", switch(s_home) == 1.0 and suffix(s_home) == "2/2",
          f"{switch(s_home)} / {suffix(s_home)}")

    # 单条场景开关
    QTest.mouseClick(sc.viewport(), Qt.LeftButton, Qt.NoModifier,
                     _switch_rect(sc.visualItemRect(find(sc, kind="scene", id="s1"))).center())
    QApplication.processEvents()
    s_home = find(sc, kind="scene_home", home="h1")
    check("单条场景关掉", switch(find(sc, kind="scene", id="s1")) == 0.0)
    check("场景家庭行停在半开", suffix(s_home) == "1/2" and abs(float(switch(s_home)) - 0.5) < 1e-6,
          f"{suffix(s_home)} / {switch(s_home)}")
    check("场景隐藏进了 prefs", store.prefs.is_scene_hidden("s1"))

    # 家庭开关：部分隐藏 → 全部显示
    QTest.mouseClick(sc.viewport(), Qt.LeftButton, Qt.NoModifier,
                     _switch_rect(sc.visualItemRect(s_home)).center())
    QApplication.processEvents()
    s_home = find(sc, kind="scene_home", home="h1")
    check("部分隐藏时点家庭 = 场景全显示", switch(s_home) == 1.0 and suffix(s_home) == "2/2",
          f"{switch(s_home)} / {suffix(s_home)}")

    # 家庭开关：全显示 → 全部隐藏
    QTest.mouseClick(sc.viewport(), Qt.LeftButton, Qt.NoModifier,
                     _switch_rect(sc.visualItemRect(s_home)).center())
    QApplication.processEvents()
    s_home = find(sc, kind="scene_home", home="h1")
    check("全显时点家庭 = 场景全隐藏", suffix(s_home) == "0/2", suffix(s_home))
    check("场景隐藏不影响设备", len(store.prefs.hidden) == 3, str(store.prefs.hidden))

    # 再点一次（全隐藏）→ 全部显示
    QTest.mouseClick(sc.viewport(), Qt.LeftButton, Qt.NoModifier,
                     _switch_rect(sc.visualItemRect(s_home)).center())
    QApplication.processEvents()
    check("全隐时再点家庭 = 场景全显示",
          suffix(find(sc, kind="scene_home", home="h1")) == "2/2",
          suffix(find(sc, kind="scene_home", home="h1")))

    # -- 「全部恢复显示」按 tab 分开 --------------------------------
    # 两边各藏一条，先清设备：场景那条必须还在
    store.set_hidden("d1", True)
    store.set_scene_hidden("s1", True)
    dlg._reload_all()
    dlg._switch_hidden_tab("devices")
    dlg.btn_show_devices.click()
    QApplication.processEvents()
    check("设备 tab 的恢复只清设备",
          store.prefs.hidden == [] and store.prefs.is_scene_hidden("s1"),
          f"{store.prefs.hidden} / {store.prefs.hidden_scenes}")
    check("设备树拉回全显", suffix(find(dev, kind="home", home="h1")) == "3/3",
          suffix(find(dev, kind="home", home="h1")))

    dlg._switch_hidden_tab("scenes")
    dlg.btn_show_scenes.click()
    QApplication.processEvents()
    check("场景 tab 的恢复只清场景",
          store.prefs.hidden_scenes == [] and store.prefs.hidden == [],
          f"{store.prefs.hidden} / {store.prefs.hidden_scenes}")
    check("场景树拉回全显", suffix(find(sc, kind="scene_home", home="h1")) == "2/2",
          suffix(find(sc, kind="scene_home", home="h1")))

    # -- 切 tab 不重建、不丢展开状态 --------------------------------
    dlg._switch_hidden_tab("devices")
    find(dev, kind="home", home="h1").setExpanded(False)
    dlg._switch_hidden_tab("scenes")
    dlg._switch_hidden_tab("devices")
    check("切 tab 不丢折叠状态", find(dev, kind="home", home="h1").isExpanded() is False)
    dlg.close()


def _px(img: QImage, dpr: float, x: float, y: float) -> QColor:
    return QColor(img.pixelColor(int(round(x * dpr)), int(round(y * dpr))))


def _diff(a: QColor, b: QColor) -> int:
    return max(abs(a.red() - b.red()), abs(a.green() - b.green()), abs(a.blue() - b.blue()))


def section_geometry() -> None:
    print("\n6) 几何与绘制")
    store, dlg = fresh()
    tree = dlg.tree_hidden_devices
    dlg.nav.setCurrentRow(2)  # 切到隐藏页，让它真的拿到宽度
    QApplication.processEvents()

    check("行高常量 32", ROW_H == 32 and INDENT == 16, f"{ROW_H}/{INDENT}")
    # 缩进不由控件做：QTreeView 会给 indentation 区铺一层 palette.highlight()
    # 的方底色，选中时就是文字块左边那块深蓝。改由 delegate 自己算。
    check("控件自身不缩进（缩进交给 delegate）", tree.indentation() == 0,
          str(tree.indentation()))
    home = find(tree, kind="home", home="h1")
    room = find(tree, kind="room", room="r1")
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
    dlg._fill_hidden_devices()
    QApplication.processEvents()
    img = tree.viewport().grab().toImage()
    half = track(find(tree, kind="room", room="r1"))
    check("半开的开关不是绿的", _diff(half, QColor(theme.TOGGLE_ON)) > 24, half.name())
    off = track(find(tree, kind="device", id="d1"))
    check("关掉的开关也是灰轨", _diff(off, QColor(theme.TOGGLE_ON)) > 24, off.name())

    # 选中一块看行首：QTreeView 曾经把缩进区铺成 palette.highlight() 的方底色
    # （浅色主题下是 #3d7eff），和这边自绘的圆角块并排，就成了"蓝色方块 + 浅蓝
    # 长条"两个选中指示打架。缩进改由 delegate 自算后，整行都归圆角块。
    room = find(tree, kind="room", room="r1")  # 重填过，旧 item 已经没了
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
        check(f"{pal.name}：六页都有大标题",
              sorted(heads) == sorted(["外观", "排序", "隐藏", "账号", "常规", "关于"]),
              "、".join(heads))

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
        check(f"{pal.name}：排序页一张卡（两棵树用 tab 切换）", len(cards_of(1)) == 1,
              f"{len(cards_of(1))} 张")
        check(f"{pal.name}：隐藏页一张卡", len(cards_of(2)) == 1, f"{len(cards_of(2))} 张")
        check(f"{pal.name}：账号页一张卡", len(cards_of(3)) == 1, f"{len(cards_of(3))} 张")

        # 排序页：两个 tab 互斥，各自吃满卡片高度
        show_page(1)
        check(f"{pal.name}：默认显示房间树",
              dlg.order_stack.currentWidget() is dlg.tree_rooms)
        dlg._switch_order_tab("scenes")
        QApplication.processEvents()
        check(f"{pal.name}：切到场景 tab 只显示场景树",
              dlg.order_stack.currentWidget() is dlg.tree_scenes)
        check(f"{pal.name}：场景树铺满卡片高度",
              abs(dlg.tree_scenes.height() - dlg.order_stack.height()) <= 2,
              f"树 {dlg.tree_scenes.height()} / 栈 {dlg.order_stack.height()}")
        dlg._switch_order_tab("rooms")
        QApplication.processEvents()
        check(f"{pal.name}：房间树铺满卡片高度",
              abs(dlg.tree_rooms.height() - dlg.order_stack.height()) <= 2,
              f"树 {dlg.tree_rooms.height()} / 栈 {dlg.order_stack.height()}")

        # 隐藏页同一套：两个 tab 互斥，各自吃满卡片高度
        show_page(2)
        check(f"{pal.name}：隐藏页默认显示设备树",
              dlg.hidden_stack.currentWidget() is dlg.tree_hidden_devices)
        dlg._switch_hidden_tab("scenes")
        QApplication.processEvents()
        check(f"{pal.name}：隐藏页切到场景 tab 只显示场景树",
              dlg.hidden_stack.currentWidget() is dlg.tree_hidden_scenes)
        check(f"{pal.name}：隐藏场景树铺满卡片高度",
              abs(dlg.tree_hidden_scenes.height() - dlg.hidden_stack.height()) <= 2,
              f"树 {dlg.tree_hidden_scenes.height()} / 栈 {dlg.hidden_stack.height()}")
        dlg._switch_hidden_tab("devices")
        QApplication.processEvents()
        check(f"{pal.name}：隐藏设备树铺满卡片高度",
              abs(dlg.tree_hidden_devices.height() - dlg.hidden_stack.height()) <= 2,
              f"树 {dlg.tree_hidden_devices.height()} / 栈 {dlg.hidden_stack.height()}")

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

        # 窗口拉到最小时，六页内容都不能被裁（外观页最高，最小高度就是按它定的）
        dlg.resize(dlg.minimumSize())
        for index, name in enumerate(("外观", "排序", "隐藏", "账号", "常规", "关于")):
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

        card = dlg.order_stack.parentWidget()
        check(f"{pal.name}：tab 栈住在 SectionCard 里",
              card is not None and card.objectName() == "SectionCard")
        if card is None:
            dlg.close()
            continue

        # 卡底：取右侧那条留白（栈在里面，树行画不到这里），
        # y 取底部往上 20——已经离开圆角弧线，横向是直的
        bar = px(card, card.width() - 4, card.height() - 20)
        # 窗口底色：卡片右边那一条页边距（页右边距 26，取 8 进去仍在底色上）
        base = card.mapTo(dlg, QPoint(card.width() + 8, 300))
        canvas = _px(img, dpr, base.x(), base.y())
        gap = abs(bar.lightness() - canvas.lightness())
        check(f"{pal.name}：卡片与窗口底色不同（Δ ≥ 12）", gap >= 12,
              f"卡 {bar.name()} L={bar.lightness()} / 底 {canvas.name()} L={canvas.lightness()}")

        # 圆角：角上那点仍是窗口底色，说明画的是圆角而不是一块方底
        for corner, (dx, dy) in (("左上", (1, 1)), ("右上", (card.width() - 2, 1)),
                                 ("左下", (1, card.height() - 2))):
            got = px(card, dx, dy)
            check(f"{pal.name}：卡片{corner}角是圆的", _diff(got, canvas) <= 8,
                  f"{got.name()} vs 底 {canvas.name()}")

        # tab 栈被内缩进卡片里（padding 10）
        dev_in = dlg.order_stack.geometry()
        right = card.width() - dev_in.x() - dev_in.width()
        check(f"{pal.name}：tab 栈在卡片内缩进 10 / 10", (dev_in.x(), right) == (10, 10),
              f"左 {dev_in.x()} / 右 {right}")

        # 选中行在卡片上仍然读得出来（深色下 accent_soft 比卡底还暗就毁了）
        tree = dlg.tree_rooms
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
