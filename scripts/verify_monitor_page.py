# SPDX-License-Identifier: GPL-3.0-or-later
"""监控页回归：布局几何 / 落盘 / 选源 / 拖动分配 / 多路仲裁 / 缩略图。

不取流：``_start`` 被换成记录器，``prepare_streams`` 不调（页面不 show）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QPoint
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtMultimedia import QMediaPlayer, QVideoSink
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mihome.core.api import HomeNode, RoomNode, Topology  # noqa: E402
from mihome.core.prefs import (  # noqa: E402
    MONITOR_CELLS_MAX,
    MONITOR_LAYOUTS,
    PREFS_VERSION,
    Prefs,
)
from mihome.core.store import DeviceState, Store, build_card_empty  # noqa: E402
from mihome.core.tree import DeviceNode  # noqa: E402
from mihome.ui import theme  # noqa: E402
from mihome.ui.camera_view import (  # noqa: E402
    SURFACE_MONITOR,
    CameraView,
    PlaybackOwnership,
    playbacks,
)
from mihome.ui.monitor_page import (  # noqa: E402
    CELL_GAP,
    LAYOUT_SPECS,
    RAIL_W,
    MonitorPage,
)
from mihome.ui.thumbs import (  # noqa: E402
    POSTER_H,
    POSTER_W,
    THUMB_H,
    THUMB_W,
    ThumbCache,
    blurred,
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


class _Caps:
    def save(self) -> None:
        pass


class _Client:
    capabilities = _Caps()

    def raw(self):
        return SimpleNamespace(auth_data={"userId": "u", "passToken": "p"})


CAM = "chuangmi.camera.069a01"


def make_store(tmp: Path) -> Store:
    """一个家庭三个房间、四台摄像头 + 一台灯（灯不能进监控列表）。"""
    store = Store(_Client(), None)  # type: ignore[arg-type]
    store.prefs = Prefs(tmp / "prefs.json")
    store.active_home = "h1"
    homes = [
        HomeNode("h1", "我的家", [RoomNode("r1", "客厅"), RoomNode("r2", "走廊"),
                                  RoomNode("r3", "阳台")]),
        HomeNode("h2", "共享家庭", [RoomNode("r9", "别处")]),
    ]
    devices = [
        DeviceNode("c1", "客厅摄像头", CAM, "camera.x", home="我的家", room="客厅",
                   home_id="h1", room_id="r1", localip="192.0.2.1"),
        DeviceNode("c2", "走廊摄像头", CAM, "camera.x", home="我的家", room="走廊",
                   home_id="h1", room_id="r2", localip="192.0.2.2"),
        DeviceNode("c3", "阳台摄像头", CAM, "camera.x", home="我的家", room="阳台",
                   home_id="h1", room_id="r3", online=False),
        DeviceNode("c4", "别家摄像头", CAM, "camera.x", home="共享家庭", room="别处",
                   home_id="h2", room_id="r9"),
        DeviceNode("l1", "台灯", "yeelink.light.lamp22", "light.x", home="我的家",
                   room="客厅", home_id="h1", room_id="r1"),
    ]
    store.topology = Topology(homes, devices)
    for node in devices:
        store.devices[node.did] = DeviceState(
            node=node,
            spec=SimpleNamespace(properties=[]),
            card=build_card_empty(node),
            values={},
            keys={},
        )
    return store


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    tmp = Path(tempfile.mkdtemp())

    print("\n1) 布局表：几何自洽")
    check("五种预设登记齐全", tuple(LAYOUT_SPECS), MONITOR_LAYOUTS)
    check("1+5 是 6 格（= 播放上限）", len(LAYOUT_SPECS["1+5"].cells), MONITOR_CELLS_MAX)
    for key, spec in LAYOUT_SPECS.items():
        cover = [[0] * spec.cols for _ in range(spec.rows)]
        bad = False
        for row, col, rspan, cspan in spec.cells:
            if row + rspan > spec.rows or col + cspan > spec.cols:
                bad = True
            for r in range(row, row + rspan):
                for c in range(col, col + cspan):
                    cover[r][c] += 1
        check_true(f"{key} 不出界也不重叠", not bad and all(v == 1 for line in cover for v in line),
                   str([line.count(0) for line in cover]))
    check("1+5 的主窗是左上 2×2（参考项目的画法）",
          LAYOUT_SPECS["1+5"].cells[0], (0, 0, 2, 2))
    check("1+2 的主窗宽是小窗的 2 倍（靠 col_stretch，跨列改不了列宽）",
          LAYOUT_SPECS["1+2"].col_stretch, (2, 1))

    print("\n2) 落盘：布局与格子")
    check("偏好版本升到 20", PREFS_VERSION, 20)
    store = make_store(tmp)
    p = store.prefs
    check("默认单窗口", p.monitor_layout, "1x1")
    check("换布局落盘", (p.set_monitor_layout("2x2"),
                        Prefs(tmp / "prefs.json").monitor_layout), (True, "2x2"))
    check("非法布局被拒", p.set_monitor_layout("3x3"), False)
    check("同值不写盘", p.set_monitor_layout("2x2"), False)
    check("格子落盘且末尾空格不写", (p.set_monitor_cells(["c1", "", "c2", "", "", ""]),
                                  Prefs(tmp / "prefs.json").monitor_cells),
          (True, ["c1", "", "c2"]))
    check("超过 6 格被截断", (p.set_monitor_cells([f"d{i}" for i in range(9)]),
                          len(p.monitor_cells)), (True, MONITOR_CELLS_MAX))

    print("\n3) 页面：选源栏只列本家摄像头")
    store.prefs = Prefs(tmp / "prefs3.json")
    thumbs = ThumbCache(tmp / "thumbs")
    page = MonitorPage(store, theme.LIGHT, thumbs=thumbs)
    page.prepare_streams = lambda: 0  # type: ignore[method-assign]
    page._start = lambda cell: None  # type: ignore[method-assign]
    page.resize(920, 560)
    page.show()
    app.processEvents()
    check("只列本家三台摄像头（灯和别家都不出现）",
          sorted(page._sources), ["c1", "c2", "c3"])
    check("默认单窗口 = 一格", len(page._cells), 1)
    check("离线设备也在列表里（能拖，点了会给提示）", "c3" in page._sources, True)
    meta = page._sources["c3"].lbl_meta.text()
    check_true("离线项标了「离线」", "离线" in meta, meta)

    print("\n4) 换布局：格数跟着变，设备按顺序搬")
    page.cb_layout.setCurrentIndex(page.cb_layout.findData("2x2"))
    app.processEvents()
    check("四宫格 = 四格", len(page._cells), 4)
    page._assign(page._cells[0], "c1")
    page._assign(page._cells[2], "c2")
    check("两格各占一台", [c.did for c in page._cells], ["c1", "", "c2", ""])
    check("落盘跟着更新", store.prefs.monitor_cells, ["c1", "", "c2"])
    page.cb_layout.setCurrentIndex(page.cb_layout.findData("1x2"))
    app.processEvents()
    check("切回两格时前面两格还在（c2 是第 3 格，超出的丢掉）",
          [c.did for c in page._cells], ["c1", ""])
    page.cb_layout.setCurrentIndex(page.cb_layout.findData("2x2"))
    app.processEvents()
    check("再切回四格，只剩落盘里的 c1", [c.did for c in page._cells], ["c1", "", "", ""])

    print("\n4b) 换回格数更少的布局：旧的行列拉伸要归零")
    # 2026-11-19 用户报：1x2 → 1x1 后单窗口只剩左半边。takeAt 不会清
    # setColumnStretch，第 1 列那半截拉伸留着，网格仍被平分。
    page.cb_layout.setCurrentIndex(page.cb_layout.findData("1x2"))
    app.processEvents()
    check("两分：两列都拉伸", (page._grid.columnStretch(0), page._grid.columnStretch(1)), (1, 1))
    page.cb_layout.setCurrentIndex(page.cb_layout.findData("1x1"))
    app.processEvents()
    check("回到单窗口：第 1 列不再拉伸", page._grid.columnStretch(1), 0)
    check("只有一格", len(page._cells), 1)
    page.resize(920, 560)
    app.processEvents()
    page._grid.activate()
    check("单窗口铺满整块，不是半屏",
          (page._cells[0].width(), page._grid_host.width()),
          (page._grid_host.width(), page._grid_host.width()))
    # 再切一次大布局回来（3×3，左上 2×2 主窗 + 其余 5 格）
    page.cb_layout.setCurrentIndex(page.cb_layout.findData("1+5"))
    app.processEvents()
    page.resize(920, 560)
    app.processEvents()
    page._grid.activate()
    check("一大五小：网格 3 列 3 行",
          (page._grid.columnCount(), page._grid.rowCount()), (3, 3))
    main, small = page._cells[0], page._cells[1]
    check("主窗宽高都约是小窗的 2 倍",
          (abs(main.width() / max(1, small.width()) - 2) <= 0.15,
           abs(main.height() / max(1, small.height()) - 2) <= 0.15), (True, True))
    host_pos = page._grid_host.mapTo(page, page._grid_host.rect().topLeft())
    main_pos = main.mapTo(page, main.rect().topLeft())
    check("主窗贴网格左上角，小窗在右侧与底行",
          (main_pos == host_pos,
           page._cells[1].x() > main.x(),
           page._cells[4].y() > main.y()),
          (True, True, True))
    # 一大两小：主窗占左列两行、宽度是小窗的 2 倍（col_stretch=(2, 1)）
    page.cb_layout.setCurrentIndex(page.cb_layout.findData("1+2"))
    app.processEvents()
    page.resize(920, 560)
    app.processEvents()
    page._grid.activate()
    main, small = page._cells[0], page._cells[1]
    check("一大两小：主窗宽约为小窗的 2 倍",
          abs(main.width() / max(1, small.width()) - 2) <= 0.15, True)
    check("一大两小：主窗占左列两行（高也是小窗的 2 倍）",
          abs(main.height() / max(1, small.height()) - 2) <= 0.15, True)
    check("一大两小：两个小窗等高、上下叠在右侧",
          (abs(page._cells[1].height() - page._cells[2].height()) <= 2,
           page._cells[2].y() > page._cells[1].y(),
           page._cells[1].x() > main.x()), (True, True, True))
    page.cb_layout.setCurrentIndex(page.cb_layout.findData("1x1"))
    app.processEvents()
    page.cb_layout.setCurrentIndex(page.cb_layout.findData("2x2"))
    app.processEvents()
    page._grid.activate()
    widths = [c.width() for c in page._cells]
    check("来回切三次后仍是干净的四宫格：四格等宽且铺满",
          (len(page._cells), len(set(widths)),
           abs(widths[0] * 2 + CELL_GAP - page._grid_host.width()) <= 2),
          (4, 1, True))

    print("\n4c) 换布局 / 搬格子：播放器整份搬过去，不重新取流")
    # 2026-11-19 用户提：参考卡片墙 → 详情页 → 独立窗口那条接续，换布局和拖到
    # 另一格也不该重新取址。做法是把 QMediaPlayer 摘下来交给新控件
    # （PlaybackOwnership.replace_view），会话 / URL 一个字不改。
    started: list[str] = []
    page._start = lambda cell: started.append(cell.did)  # type: ignore[method-assign]
    cell0 = page._cells[0]
    view0 = cell0.ensure_view()
    view0._player = QMediaPlayer()  # noqa: SLF001 - 假会话：只造对象，不 setSource
    view0._sink = QVideoSink()  # noqa: SLF001
    view0._set_state(CameraView.PLAYING)  # noqa: SLF001
    playbacks().claim(view0, "c1", "rtsp://fake/c1", False, SURFACE_MONITOR)
    player = view0._player

    page.cb_layout.setCurrentIndex(page.cb_layout.findData("1+5"))
    app.processEvents()
    new0 = page._cells[0].view
    check("换布局没有重新取流", started, [])
    check("新格子接手的是同一个播放器",
          new0 is not None and new0._player is player, True)
    check("所有权跟着换到新控件",
          playbacks().session_of(new0).did if new0 is not None else "", "c1")
    check("旧控件已从会话表里摘掉", playbacks().session_of(view0), None)
    check("设备仍留在第一格", page._cells[0].did, "c1")

    page.cb_layout.setCurrentIndex(page.cb_layout.findData("1+2"))
    app.processEvents()
    check("再切一次还是同一个播放器", page._cells[0].view._player is player, True)
    check("一路没重开过", started, [])

    # 拖到另一格：同一台设备只占一格，搬过去同样不该重新开流
    page._assign(page._cells[2], "c1")
    check("拖到另一格不重新取流", started, [])
    check("播放器搬到了第 3 格", page._cells[2].view._player is player, True)
    check("原来的格子空了", page._cells[0].did, "")
    check("会话只有一条（没多占一个观看位）", playbacks().count(SURFACE_MONITOR), 1)

    # 换成格数更少的布局：留下的那路接着播，被挤出布局的那路要真停
    playbacks().release(None, "4c 收尾：清掉假会话")
    views: dict[str, CameraView] = {}
    for i, did in enumerate(("c1", "c2")):
        page._cells[i].set_device(store.devices[did])
        v = page._cells[i].ensure_view()
        v._player = QMediaPlayer()  # noqa: SLF001
        v._sink = QVideoSink()  # noqa: SLF001
        v._set_state(CameraView.PLAYING)  # noqa: SLF001
        playbacks().claim(v, did, f"rtsp://fake/{did}", False, SURFACE_MONITOR)
        views[did] = v
    page.cb_layout.setCurrentIndex(page.cb_layout.findData("1x1"))
    app.processEvents()
    check("格数变少：留下的那一路接着播", playbacks().count(SURFACE_MONITOR), 1)
    check("被挤出布局的那一路真停", playbacks().session_of(views["c2"]), None)
    started.clear()
    playbacks().release(None, "4c 收尾：清掉假会话")
    page.cb_layout.setCurrentIndex(page.cb_layout.findData("2x2"))
    app.processEvents()
    check("收尾后回到四宫格、c1 在第一格",
          (len(page._cells), page._cells[0].did, started), (4, "c1", []))

    print("\n5) 单击 / 清空 / 同一台只占一格")
    page._on_source_clicked("c2")
    check("单击放进第一个空格", [c.did for c in page._cells], ["c1", "c2", "", ""])
    page._on_source_clicked("c2")
    check("再点同一台不重复占格", [c.did for c in page._cells], ["c1", "c2", "", ""])
    page._assign(page._cells[2], "c2")
    check("拖到别的格：原来那格腾出来", [c.did for c in page._cells], ["c1", "", "c2", ""])
    page._on_clear_cell(page._cells[0])
    check("清空格子", [c.did for c in page._cells], ["", "", "c2", ""])

    print("\n6) 离线设备：写原因、不起取址任务")
    off = page._cells[3]
    page.__dict__.pop("_start", None)  # 恢复真实实现（前面为了不联网换成了记录器）
    off.set_device(store.devices["c3"])
    page._start(off)
    check("格子里写了原因、没有发起取址",
          (off._message, dict(page._pending)), ("设备离线，无法取流", {}))

    print("\n6b) 发起取址后必须有定时器在收结果")
    # 2026-11-19 真机：拖进格子后永远停在「正在取流…」。原因是 _start 只起了线程、
    # 没 start 结果定时器——地址在队列里躺着没人取。这条断言钉住这个 start。
    page._work = lambda job: None  # type: ignore[method-assign] - 不联网
    page._poll.stop()
    cell2 = page._cells[2]
    page._start(cell2)
    check("结果定时器在跑", page._poll.isActive(), True)
    check("这一格登记了待取址", list(page._pending), ["c2"])
    token = page._pending["c2"]
    page._results.put(("c2", token, "", "模拟失败", False))
    page._collect()
    check("结果取走后清掉待取址并停表",
          (dict(page._pending), page._poll.isActive()), ({}, False))
    check("失败写进格子里，而不是卡在取流", cell2._message, "模拟失败")

    print("\n7) 双击放大：这一格拉满整块画面区，再双击还原")
    zoom = page._cells[2]
    before = zoom.size()
    check("放大前这一格只是四宫格的一角", before.width() < page._grid_host.width(), True)
    page._on_zoom_cell(zoom)
    app.processEvents()
    page._grid.activate()
    check("放大时只显示一格", sum(1 for c in page._cells if not c.isHidden()), 1)
    idx = page._grid.indexOf(zoom)
    check("放大这一格还在布局里", idx >= 0, True)
    # 2026-11-25 用户报的就是这里：旧实现只把别的格 setVisible(False)，
    # 藏起来的格仍占着自己的格位，放大那格的尺寸一点没变
    check("在布局里跨满全部行列（0,0,2 行 2 列）",
          page._grid.getItemPosition(idx), (0, 0, 2, 2))
    check("宽高真的等于整块画面区", (zoom.width(), zoom.height()),
          (page._grid_host.width(), page._grid_host.height()))
    check("比放大前大（不再保持原样）",
          (zoom.width() > before.width(), zoom.height() > before.height()),
          (True, True))
    page._on_zoom_cell(zoom)
    app.processEvents()
    page._grid.activate()
    check("还原后四格都在", sum(1 for c in page._cells if not c.isHidden()), 4)
    check("还原后尺寸回到原来那样", zoom.size(), before)
    check("还原后每格回到预设坐标",
          [page._grid.getItemPosition(page._grid.indexOf(c))[:2] for c in page._cells],
          [(0, 0), (0, 1), (1, 0), (1, 1)])

    print("\n7b) 放大时点源 / 清空：只作用在看得见的那一格")
    page._on_zoom_cell(page._cells[2])
    app.processEvents()
    page._start = lambda cell: None  # type: ignore[method-assign]
    page._on_source_clicked("c1")
    check("点源换的是放大这一路，没塞进藏着的空格",
          [c.did for c in page._cells], ["", "", "c1", "c3"])
    page._on_clear_cell(page._cells[2])
    app.processEvents()
    check("清空放大那一格 → 自动还原成网格",
          (page._zoomed, sum(1 for c in page._cells if not c.isHidden())), (None, 4))
    page._on_source_clicked("c1")
    check("还原后单击回到「放第一个空格」", [c.did for c in page._cells][0], "c1")

    print("\n8) 多路仲裁：monitor 面单独放宽，其余面仍是单槽")
    owner = PlaybackOwnership()
    views = [CameraView(theme.LIGHT, "#000000") for _ in range(7)]
    oks = [owner.claim(v, f"d{i}", f"rtsp://x/{i}", False, SURFACE_MONITOR)
           for i, v in enumerate(views)]
    check("6 路都能登记，第 7 路被拒", (sum(oks), oks[-1]), (6, False))
    check("监控页会话数", owner.count(SURFACE_MONITOR), 6)
    check("有监控页会话时 active 仍为 False（单槽面语义不变）", owner.active, False)
    dup = CameraView(theme.LIGHT, "#000000")
    check("同一台设备不能占第二格", owner.claim(dup, "d0", "rtsp://x/0", False, SURFACE_MONITOR),
          False)
    detail_view = CameraView(theme.LIGHT, "#000000")
    check("详情页能拿到同 did（先腾出监控页那格）",
          owner.claim(detail_view, "d0", "rtsp://x/0", False, "detail"), True)
    check("只腾了那一格，其余 5 路还在", owner.count(SURFACE_MONITOR), 5)
    check("单槽面能查到", (owner.active, owner.did), (True, "d0"))
    other = CameraView(theme.LIGHT, "#000000")
    check("再开一路详情页被拒（单槽面仍然只允许一条）",
          owner.claim(other, "d1", "rtsp://x/1", False, "detail"), False)
    check("被拒的登记不该顺手腾掉监控页那一格", owner.count(SURFACE_MONITOR), 5)
    ended = owner.release_surface(SURFACE_MONITOR, "离开页面")
    check("离开监控页停掉剩下 5 路", (ended, owner.count(SURFACE_MONITOR)), (5, 0))
    check("详情页那路不受影响", owner.did, "d0")

    print("\n9) 缩略图：模糊与落盘")
    img = QImage(640, 360, QImage.Format_RGB32)
    img.fill(QColor("#202830"))
    painter = QPainter(img)
    painter.fillRect(0, 0, 320, 360, QColor("#c0392b"))
    painter.end()
    out = blurred(img)
    # 存的尺寸是**封面**尺寸（480×270）：同一张图既当列表缩略图又当格子的整幅
    # 封面，列表那边画的时候缩到 168×94（见 9d）
    check("缩成封面尺寸", (out.width(), out.height()), (POSTER_W, POSTER_H))
    check("竖图也裁成 16:9（不拉变形）",
          (blurred(QImage(100, 400, QImage.Format_RGB32)).width(),
           blurred(QImage(100, 400, QImage.Format_RGB32)).height()), (POSTER_W, POSTER_H))
    pix = thumbs.put("c1", img)
    check_true("写入内存与磁盘", pix is not None and thumbs.path("c1").exists(),
               str(thumbs.path("c1")))
    fresh = ThumbCache(tmp / "thumbs")
    check_true("新实例能从磁盘读回", fresh.get("c1") is not None)
    check("没播过的设备返回 None", fresh.get("nope"), None)
    check("不存在的设备被清掉", fresh.prune({"c1"}), 0)
    fresh.put("c2", img)
    check("prune 清掉多余的那张", fresh.prune({"c1"}), 1)

    print("\n9b) 画面缩放：按宽高比里小的那个，整幅可见")
    # 2026-11-19 用户报"只按高度拉伸"：格子接近 1:1，而画面是 16:9，按 cover（取
    # max）画时高度铺满、两侧被裁掉将近一半宽度。监控要看全，改成取 min。
    probe = CameraView(theme.LIGHT, "#000000", compact=True)
    probe.resize(400, 400)
    shot = QImage(160, 90, QImage.Format_RGB32)
    shot.fill(QColor("#ff0000"))
    probe._image = shot  # noqa: SLF001 - 离屏验证要的就是这一帧
    probe.set_contain_fit(True)
    canvas = probe.grab().toImage()
    check("contain：上下留边（方形格子里 16:9 只占中间一条）",
          QColor(canvas.pixel(200, 2)).name(), QColor(probe.video_bg).name())
    check("contain：画面在中间且横向铺满", QColor(canvas.pixel(200, 200)).name(), "#ff0000")
    probe.set_contain_fit(False)
    canvas = probe.grab().toImage()
    check("cover（对照组）：填满画布、两侧被裁", QColor(canvas.pixel(2, 200)).name(),
          "#ff0000")
    page._cells[2].ensure_view()
    check("监控页格子用的是 contain", page._cells[2].view.contain_fit, True)

    print("\n9c) 选源栏：单独一张卡片，摄像头多了能上下滚")
    # 用户 2026-11-19 问"设备多了能不能滚"——QScrollArea 本来就会滚，但一直没验过；
    # 而滚动条一出现要占掉 10px，所以顺带量一下缩略图有没有被挤窄。
    big_dir = tmp / "big"
    big_dir.mkdir()
    big = make_store(big_dir)
    for i in range(8):
        node = DeviceNode(f"x{i}", f"摄像头{i}", CAM, "camera.x", home="我的家",
                          room="客厅", home_id="h1", room_id="r1",
                          localip=f"192.0.2.{10 + i}")
        big.topology.devices.append(node)
        big.devices[node.did] = DeviceState(
            node=node,
            spec=SimpleNamespace(properties=[]),
            card=build_card_empty(node),
            values={},
            keys={},
        )
    page2 = MonitorPage(big, theme.DARK, thumbs=ThumbCache(tmp / "th2"))
    page2.prepare_streams = lambda: 0  # type: ignore[method-assign]
    page2._start = lambda cell: None  # type: ignore[method-assign]
    page2.resize(1100, 520)
    page2.show()
    app.processEvents()
    check("选源栏套在卡片里（卡片是它的父级）", page2.rail.parent() is page2.rail_card, True)
    check("卡片宽度 = 缩略图 + 内边距 + 边框 + 滚动条", page2.rail_card.width(), RAIL_W)
    check("11 台摄像头都列出来了", len(page2._sources), 11)
    bar = page2.rail.verticalScrollBar()
    check_true("超出高度就出现竖向滚动条", bar.maximum() > 0, f"max={bar.maximum()}")
    check_true("滚动条没把缩略图挤窄",
               page2.rail.viewport().width() >= THUMB_W,
               f"viewport={page2.rail.viewport().width()} thumb={THUMB_W}")
    bar.setValue(bar.maximum())
    app.processEvents()
    last = list(page2._sources.values())[-1]
    top = last.mapTo(page2.rail.viewport(), QPoint(0, 0)).y()
    check_true("滚到底能看到最后一项（整项都在视口里）",
               0 <= top and top + last.height() <= page2.rail.viewport().height() + 1,
               f"top={top} h={last.height()} vp={page2.rail.viewport().height()}")

    print("\n10) 标题对齐：三页同一个位置")
    from PySide6.QtWidgets import QLabel

    title = next(w for w in page.findChildren(QLabel) if w.objectName() == "PageTitle")
    top = title.mapTo(page, title.rect().topLeft())
    check("标题贴在 (28,8)、高度是自身行高（与另两页同一条线）",
          (top.x(), top.y(), title.height()), (28, 8, title.sizeHint().height()))
    check("统计文字与标题共中线",
          abs(page.lbl_hint.mapTo(page, page.lbl_hint.rect().center()).y()
              - title.mapTo(page, title.rect().center()).y()) <= 1, True)
    # 2026-11-19：本页原先漏写 PageTitle / PageHint 两条 qss，标题退回 13px 正文，
    # 看起来比「耗材」「电脑自动化」小一号。钉住字号与字重，别再漏。
    check("标题是 20px 粗体（与耗材页同一套）",
          (title.font().pixelSize(), title.font().weight()), (20, 700))
    check("统计提示是 12px 弱色", page.lbl_hint.font().pixelSize(), 12)
    check("顶栏按钮与右侧下拉等高（不写死 min-height）",
          page.btn_stop.height(), page.cb_layout.height())
    check("下拉与耗材页同宽", page.cb_layout.width(), 132)

    print("\n11) 进页面不自动播：封面 + 播放键，源与地址先备好")
    # 2026-11-19 用户定：一进监控页就 6 路齐解会让风扇先响起来，所以默认不播，
    # 格子里放"上次那一帧的模糊图 + 播放键"。这条也顺手解释了另一个报障：
    # 点过「全部停止」再换布局，格子看起来空了 → 其实设备还在，只是原先没画空态。
    store4 = make_store(tmp / "s4")
    store4.prefs = Prefs(tmp / "prefs11.json")
    store4.prefs.set_monitor_layout("2x2")
    store4.prefs.set_monitor_cells(["c1", "c2"])
    th4 = ThumbCache(tmp / "th11")
    for did, color in (("c1", "#2e7d32"), ("c2", "#8e44ad")):
        cover = QImage(320, 180, QImage.Format_RGB32)
        cover.fill(QColor(color))
        th4.put(did, cover)
    page4 = MonitorPage(store4, theme.LIGHT, thumbs=th4)
    page4.prepare_streams = lambda: 0  # type: ignore[method-assign]
    page4.prefetch_urls = lambda: 0  # type: ignore[method-assign]
    page4.resize(920, 560)
    page4.show()
    app.processEvents()
    cell4 = page4._cells[0]
    check("进页面不自动取流（一条会话都没有）", playbacks().count(SURFACE_MONITOR), 0)
    check("不再有「回页面自动播」的入口", hasattr(page4, "resume_all"), False)
    check("有设备的格子已经建出画面控件（空态带播放键）", cell4.view is not None, True)
    check("播放键是显示状态", not cell4.view._play_btn.isHidden(), True)  # noqa: SLF001
    check("格子拿到了封面", cell4.poster is not None, True)
    shot = cell4.view.grab().toImage()
    px = QColor(shot.pixel(shot.width() // 2, shot.height() // 2)).name()
    check_true("封面真的画上去了（不是黑块）", px != QColor(cell4.view.video_bg).name(), px)
    check("缩略图控件仍是 168×94（图缩着画）",
          (page4._sources["c1"].thumb.width(), page4._sources["c1"].thumb.height()),
          (THUMB_W, THUMB_H))
    check("存的图是封面尺寸（一份存档两处用）",
          page4._sources["c1"].thumb._pix.width(), POSTER_W)  # noqa: SLF001
    page4.hide()
    page4.show()
    app.processEvents()
    check("切走再回来也不自动播", playbacks().count(SURFACE_MONITOR), 0)

    print("\n12) 全部停止后换布局：设备留在格子里，封面还在，只需点播放")
    page4._work = lambda job: None  # type: ignore[method-assign] - 不联网
    page4._start(cell4)
    check("点了播放才去取址", list(page4._pending), ["c1"])
    page4.stop_all()
    check("全部停止后没有会话", playbacks().count(SURFACE_MONITOR), 0)
    page4.cb_layout.setCurrentIndex(page4.cb_layout.findData("1+5"))
    app.processEvents()
    check("换布局后设备还在原来的格子（不用重新拖）",
          [c.did for c in page4._cells][:2], ["c1", "c2"])
    check("每一格都还有画面控件", all(c.view is not None for c in page4._cells[:2]), True)
    check("封面跟着搬到新格子", all(c.poster is not None for c in page4._cells[:2]), True)
    check("没有偷偷重新取流",
          (list(page4._pending), playbacks().count(SURFACE_MONITOR)), ([], 0))
    page4._cells[2].set_device(store4.devices["c1"])
    page4._cells[0].set_device(None)
    page4.cb_layout.setCurrentIndex(page4.cb_layout.findData("1x1"))
    app.processEvents()
    page4.stop_all()

    print("\n13) 封面：播放中每 10 秒换一张，停播时存最后一帧")
    def center_color(pix) -> str:
        img = pix.toImage()
        return QColor(img.pixel(img.width() // 2, img.height() // 2)).name()

    cell5 = page4._cells[0]
    page4.cb_layout.setCurrentIndex(page4.cb_layout.findData("2x2"))
    app.processEvents()
    cell5 = page4._cells[0]
    cell5.set_device(store4.devices["c1"])
    page4._save_cells()
    frame_a = QImage(320, 180, QImage.Format_RGB32)
    frame_a.fill(QColor("#c0392b"))
    frame_b = QImage(320, 180, QImage.Format_RGB32)
    frame_b.fill(QColor("#1f6feb"))
    page4._snap_at.clear()
    page4._on_snapshot(cell5, frame_a)
    check("第一张帧立刻存成封面", center_color(th4.get("c1")), "#c0392b")
    page4._on_snapshot(cell5, frame_b)
    check("10 秒内不重复落盘", center_color(th4.get("c1")), "#c0392b")
    page4._snap_at["c1"] -= 11
    page4._on_snapshot(cell5, frame_b)
    check("过了 10 秒换新的一张", center_color(th4.get("c1")), "#1f6feb")
    check_true("左侧列表的缩略图跟着换",
               page4._sources["c1"].thumb._pix is not None)  # noqa: SLF001

    last = QImage(320, 180, QImage.Format_RGB32)
    last.fill(QColor("#f1c40f"))
    cell5.view._image = last  # noqa: SLF001 - 模拟正在播的最后一帧（离屏不取流）
    page4._stop_cell(cell5)
    check("停播前把最后一帧存成封面", center_color(th4.get("c1")), "#f1c40f")
    check("停播后回到空态：封面 + 播放键",
          (cell5.poster is not None, not cell5.view._play_btn.isHidden()),  # noqa: SLF001
          (True, True))

    print("\n14) 预热地址：点播放不再走一遍取址")
    from mihome.core.camera import shared as cam_service  # noqa: PLC0415

    played: list[str] = []
    cell5.view.play = lambda url: played.append(url)  # type: ignore[method-assign]
    page4._urls["c1"] = ("rtsp://prewarm/c1", False, cam_service().epoch)
    page4._start(cell5)
    check("命中预热地址：直接开播", played, ["rtsp://prewarm/c1"])
    session = playbacks().session_of(cell5.view)
    check("会话登记在监控页面上", session.did if session else "", "c1")
    page4.stop_all()
    check("停播归还观看位", playbacks().count(SURFACE_MONITOR), 0)
    # go2rtc 重起过（epoch 变了）：旧地址可能是上一个端口，必须重取
    called: list[str] = []
    page4._work = lambda job: called.append(job.did)  # type: ignore[method-assign]
    page4._urls["c1"] = ("rtsp://stale/c1", False, cam_service().epoch - 1)
    page4._start(cell5)
    check("epoch 变了就重取地址，不拿旧端口硬连", called, ["c1"])
    page4.stop_all()

    page.stop_all()
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：" + "、".join(FAILED))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
