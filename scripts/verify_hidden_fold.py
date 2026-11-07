# SPDX-License-Identifier: GPL-3.0-or-later
"""验证首页「已隐藏设备」分组的收起 / 展开。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_hidden_fold.py``

这一组断言盯的是"改了会悄悄退化"的几条：

1. **默认展开**——老文件里没有这个偏好，一进来就看不见隐藏设备太吓人；
2. **只有 ``kind="hidden"`` 可折叠**——其它房间的标题行照旧透传鼠标，
   点它不会把一整个房间收掉；
3. **收起后只剩标题行高**（``_RoomHeader.H``），卡片流不占位，这才是加它的理由；
4. **落盘 / 重读 / 重建墙后仍收起**——切主题、切家庭都会重建，记不住等于没有；
5. **搜索时临时展开**，否则命中项藏在收起的分区里，搜到了却看不见；
   清空搜索要回到用户自己选的状态，而不是"搜过一次就永久展开"；
6. **坏数据当 False**——"yes" / 1 这类值不该让人一开就看不见设备。

中文在 offscreen 平台渲染成豆腐块，这里只量状态与几何，不读文字。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mihome.core.prefs import HIDDEN_ROOM, Prefs  # noqa: E402
from mihome.core.store import DeviceState, RoomView  # noqa: E402
from mihome.core.tree import DeviceNode  # noqa: E402
from mihome.ui import theme  # noqa: E402
from mihome.ui.wall import WallView, _RoomHeader  # noqa: E402

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


class Icons:
    def ensure(self, model: str) -> None:
        pass

    def scaled(self, model: str, size: int):
        return None


def make_state(did: str, name: str, room: str) -> DeviceState:
    """最朴素的卡片替身：没有 gangs / controls / metrics，一律回落标准样式。"""
    card = SimpleNamespace(
        category="light", span=1, gangs=[], controls=[], metrics=[],
        layout="light", power_actions=None, default_style="",
        profile=SimpleNamespace(label="灯"),
        control=lambda slot: None, bound=lambda slot: None,
    )
    node = DeviceNode(did, name, "m", "light.x", home="我的家", room=room, online=True)
    return DeviceState(node=node, spec=None, card=card, values={})


class FakeStore:
    """WallView 用到的那几个接口。"""

    def __init__(self, prefs: Prefs, views: list[RoomView]) -> None:
        self.prefs = prefs
        self.scenes: list = []
        self.devices = {s.did: s for v in views for s in v.devices}
        self.views = views

    def rooms(self, home: str | None = None):
        return self.views

    def scenes_for(self, home: str):
        return []


def click(header: _RoomHeader) -> None:
    """真的走一遍 mouseReleaseEvent，而不是直接 emit 信号。

    顺带验证"可折叠的标题行自己接鼠标"这件事——不可点的那种在
    ``WA_TransparentForMouseEvents`` 下拿不到这个事件。
    """
    pos = QPointF(header.width() / 2, header.height() / 2)
    ev = QMouseEvent(QMouseEvent.Type.MouseButtonRelease, pos, pos,
                     Qt.LeftButton, Qt.NoButton, Qt.NoModifier)
    header.mouseReleaseEvent(ev)


def ink_runs(header: _RoomHeader) -> list[tuple[int, int]]:
    """把标题行渲染出来，按列扫出"有墨"的连续区段（顺序即从左到右）。

    文字在哪、三角在哪不能靠"重算一遍布局公式"来断言——那就变成把实现抄
    一遍。这里直接看渲染结果：字形各自成段，三角是独立的一小段。
    """
    img = header.grab().toImage()
    counter: Counter[str] = Counter(
        img.pixelColor(x, y).name()
        for x in range(0, header.width(), 3)
        for y in range(0, header.height(), 3)
    )
    bg = counter.most_common(1)[0][0]
    runs: list[tuple[int, int]] = []
    start = prev = None
    for x in range(header.width()):
        if any(img.pixelColor(x, y).name() != bg for y in range(header.height())):
            if prev is None or x != prev + 1:
                if start is not None:
                    runs.append((start, prev))
                start = x
            prev = x
    if start is not None:
        runs.append((start, prev))
    return runs


def make_wall(prefs: Prefs, tmp: Path, name: str = "p.json",
              hidden_rooms: tuple[str, str] = ("客厅", "餐厅")) -> WallView:
    """三组：客厅 / 餐厅 / 已隐藏设备。

    隐藏设备**保留真实房间名**（线上就是这样：``_hidden_devices`` 拿的是原
    设备状态，房间没被抹掉），所以隐藏分组能按房间再分一层。
    """
    shown1 = make_state("d1", "客厅吸顶灯", "客厅")
    shown2 = make_state("d2", "餐厅灯", "餐厅")
    hidden1 = make_state("d3", "旧台灯", hidden_rooms[0])
    hidden2 = make_state("d4", "备用插座", hidden_rooms[1])
    views = [
        RoomView("h", "我的家", "客厅", [shown1]),
        RoomView("h", "我的家", "餐厅", [shown2]),
        RoomView("h", "我的家", HIDDEN_ROOM, [hidden1, hidden2], "hidden"),
    ]
    wall = WallView(FakeStore(prefs, views), Icons(), theme.LIGHT)
    wall.resize(900, 600)
    wall.build()
    wall.show()
    QApplication.processEvents()
    return wall


def main() -> int:
    app = QApplication(sys.argv[:1])  # noqa: F841
    tmp = Path(tempfile.mkdtemp(prefix="mihome-hidden-fold-"))

    print("\n1) 偏好：默认展开 / 只认真正的 true / 往返")
    p = Prefs(tmp / "p.json")
    check("老文件没有这个键 → 展开", p.hidden_collapsed, False)
    check("默认不落盘", "hidden_collapsed" in json.loads((tmp / "p.json").read_text())
          if (tmp / "p.json").exists() else False, False)
    check("设成收起", p.set_hidden_collapsed(True), True)
    check("再设一次不写盘", p.set_hidden_collapsed(True), False)
    check("重读仍是收起", Prefs(tmp / "p.json").hidden_collapsed, True)
    check("设回展开", p.set_hidden_collapsed(False), True)
    for bad in ("yes", 1, "true", [], {}):
        (tmp / "bad.json").write_text(json.dumps({"hidden_collapsed": bad}), encoding="utf-8")
        check(f"坏值 {bad!r} 当展开", Prefs(tmp / "bad.json").hidden_collapsed, False)
    (tmp / "bad.json").write_text(json.dumps({"hidden_collapsed": True}), encoding="utf-8")
    check("真正的 true 才认", Prefs(tmp / "bad.json").hidden_collapsed, True)

    print("\n2) 谁能折叠：只有 kind=hidden 的标题行")
    p = Prefs(tmp / "wall.json")
    wall = make_wall(p, tmp)
    sections = wall._sections
    check(f"三个分区（客厅 / 餐厅 / {HIDDEN_ROOM}）", [s.view.room for s in sections],
          ["客厅", "餐厅", HIDDEN_ROOM])
    check("普通房间不可折叠", [s.collapsible for s in sections], [False, False, True])
    check("隐藏分区默认展开", sections[2].collapsed, False)
    check_true("隐藏分区卡片流可见", sections[2]._body.isVisible())
    check("普通房间标题行仍透传鼠标",
          sections[0].header.testAttribute(Qt.WA_TransparentForMouseEvents), True)
    check("隐藏分区标题行自己接鼠标",
          sections[2].header.testAttribute(Qt.WA_TransparentForMouseEvents), False)
    before = sections[0].header._note
    click(sections[0].header)
    QApplication.processEvents()
    check("点普通房间标题不会收起", sections[0].collapsed, False)
    check("普通房间的说明文字没被动过", sections[0].header._note, before)

    print("\n2b) 标题旁边的三角与常驻台数")
    hdr = sections[2].header
    check("展开时也报台数", hdr._note, "2 台")
    check_true("不再劝人去设置页", "设置" not in hdr._note, f"（{hdr._note!r}）")
    # 先把台数藏掉，最后一段墨迹就只剩三角，位置一目了然
    hdr.set_note("")
    runs = ink_runs(hdr)
    check_true("标题行有渲染出东西", len(runs) >= 2, f"（{runs}）")
    title_end = runs[-2][1]
    chev = runs[-1]
    check_true("三角紧跟标题（不再飘到最右边）",
               chev[0] - title_end <= 20,
               f"（标题到 {title_end}，三角 {chev[0]}–{chev[1]}）")
    check_true("三角没贴到分区右边缘",
               hdr.width() - chev[1] > 60,
               f"（行宽 {hdr.width()}，三角到 {chev[1]}）")
    check_true("三角是细长一小段，不是整段文字",
               chev[1] - chev[0] <= 14, f"（宽 {chev[1] - chev[0] + 1}px）")
    hs_note = sections[2]
    hs_note.sync_note()
    check("台数回来了", hdr._note, "2 台")
    check_true("台数在三角右侧渲染出来",
               len(ink_runs(hdr)) > len(runs), f"（{ink_runs(hdr)}）")
    expanded_chev_w = chev[1] - chev[0] + 1

    click(hdr)
    QApplication.processEvents()
    check("收起后仍是「2 台」", hdr._note, "2 台")
    hdr.set_note("")
    collapsed_chev = ink_runs(hdr)[-1]
    collapsed_chev_w = collapsed_chev[1] - collapsed_chev[0] + 1
    check_true("收起后三角指向右（比展开时窄）", collapsed_chev_w < expanded_chev_w,
               f"（展开 {expanded_chev_w}px → 收起 {collapsed_chev_w}px）")
    hs_note.sync_note()

    # 收回展开态，下一节还要从"展开"开始量高度
    click(hdr)
    QApplication.processEvents()
    check("再点一下回到展开", sections[2].collapsed, False)

    print("\n2c) 隐藏分组内部按房间分一层")
    hs = sections[2]
    check("两个房间 → 两个分组", list(hs._groups), ["客厅", "餐厅"])
    check("分组标题带房间名与台数", [g[0]._text for g in hs._groups.values()],
          ["客厅 · 1 台", "餐厅 · 1 台"])
    check("每张卡进了自己房间的流",
          [hs._groups[r][2].count() for r in ("客厅", "餐厅")], [1, 1])
    check("普通房间不分层", sections[0]._groups, {})
    check("普通房间只有一条流", len(sections[0].flows), 1)

    one_room = make_wall(Prefs(tmp / "one.json"), tmp, hidden_rooms=("客厅", "客厅"))
    one = one_room._sections[2]
    check("同一个房间的隐藏设备不分层", one._groups, {})
    check("只有一条流", len(one.flows), 1)
    check("台数照样统计", one.header._note, "2 台")

    print("\n3) 收起：只剩标题行高，落盘，重建后仍收起")
    open_h = sections[2].height()
    click(sections[2].header)
    QApplication.processEvents()
    check("点一下 → 收起", sections[2].collapsed, True)
    check_true("卡片流不再显示", not sections[2]._body.isVisible())
    check("分区高度 = 标题行高", sections[2].height(), _RoomHeader.H)
    check_true("确实比展开时矮很多", sections[2].height() < open_h,
               f"（展开 {open_h} → 收起 {sections[2].height()}）")
    check("偏好跟着落盘", p.hidden_collapsed, True)
    check_true("标题后面报了台数", "2 台" in sections[2].header._note,
               f"（{sections[2].header._note!r}）")

    rebuilt = WallView(wall._store, Icons(), theme.LIGHT)
    rebuilt.resize(900, 600)
    rebuilt.build()
    rebuilt.show()
    QApplication.processEvents()
    check("重建墙（切主题/家庭会走这条）后仍收起", rebuilt._sections[2].collapsed, True)
    check("重建后卡片对象照样建出来", len(rebuilt._sections[2].cards), 2)

    print("\n4) 搜索：临时展开，清空后收回")
    wall.set_filter("旧台灯")
    QApplication.processEvents()
    check("命中项在收起的分区里也搜得到", wall._sections[2].collapsed, False)
    check_true("分区可见", wall._sections[2].isVisible())
    check("临时展开不写偏好", p.hidden_collapsed, True)
    groups = wall._sections[2]._groups
    check("没命中的房间小标题也藏起来",
          [groups[r][0].isVisible() for r in ("客厅", "餐厅")], [True, False])
    wall.set_filter("")
    QApplication.processEvents()
    check("清空搜索回到用户选的折叠态", wall._sections[2].collapsed, True)
    check("清空搜索后房间小标题全回来",
          [groups[r][0].isHidden() for r in ("客厅", "餐厅")], [False, False])

    print("\n5) 展开 / 再收起，信号只从可折叠分区发出")
    got: list[bool] = []
    sections = wall._sections
    for s in sections:
        s.collapse_changed.connect(got.append)
    click(sections[2].header)
    QApplication.processEvents()
    check("展开一次只发一个信号", got, [False])
    check("展开后偏好回到 false", p.hidden_collapsed, False)
    click(sections[0].header)
    QApplication.processEvents()
    check("点普通房间不发信号", got, [False])
    click(sections[2].header)
    QApplication.processEvents()
    check("再收起", (sections[2].collapsed, p.hidden_collapsed), (True, True))

    print("\n6) 收起态在深色主题下同样成立")
    wall.set_palette(theme.DARK)
    wall.rebuild_palette(theme.DARK)
    wall.show()
    QApplication.processEvents()
    check("切主题重建后仍收起", wall._sections[2].collapsed, True)
    check("高度仍是标题行", wall._sections[2].height(), _RoomHeader.H)

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
