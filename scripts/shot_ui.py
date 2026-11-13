# SPDX-License-Identifier: GPL-3.0-or-later
"""离屏截图（多视图）：主窗口 / 深色 / 切换家庭 / 设置页 / 详情页 / 隐藏设备。

用法：
  .venv/Scripts/python.exe -X utf8 scripts/shot_ui.py [输出目录]

这个脚本只用于改界面后的自查，不参与运行时。
"""

from __future__ import annotations

import os
import sys

import traceback
from pathlib import Path
from types import TracebackType

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QPoint, QRect, QSettings, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

# 这些脚本自己驱动窗口（建 → 截 → close → 退）：让托盘接管 close 的话，
# 关闭只会把窗口藏起来，脚本等的是一个不会结束的事件循环。
os.environ.setdefault("MIHOME_NO_TRAY", "1")

from mihome.core import paths  # noqa: E402
from mihome.ui import theme  # noqa: E402
from mihome.ui.main_window import MainWindow  # noqa: E402

OUT = Path("scripts/out")


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT
    out.mkdir(parents=True, exist_ok=True)

    # 这个脚本会隐藏设备、切家庭、切主题。为了不给日常使用留下痕迹，
    # 跑完把 prefs.json 和 QSettings 里的两项还原。
    prefs_path = paths.data_dir() / "prefs.json"
    prefs_backup = prefs_path.read_bytes() if prefs_path.exists() else None
    saved = QSettings("MiHomePanel", "MiHomePanel")
    start_home = str(saved.value("ui/active_home", "") or "")
    start_theme = str(saved.value("ui/theme", "light") or "light")

    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(13)
    app.setFont(font)

    win = MainWindow(theme.LIGHT)
    win.palette_ref = theme.LIGHT
    win._apply_theme()  # noqa: SLF001
    win.resize(1360, 900)
    win.show()
    win.start()

    state = {"i": 0, "wait": 0, "failed": False, "closed": False}

    def grab(name: str, widget=None) -> None:
        w = widget or win
        pix = w.grab()
        pix.save(str(out / name))
        print(f"已保存 {name}  {pix.width()}x{pix.height()}")

    def ready() -> bool:
        return (
            not win.store.loading
            and bool(win.wall._cards)  # noqa: SLF001
            and not win.icon_provider._pending  # noqa: SLF001
        )

    def save_gangs(name: str) -> None:
        """把多键开关卡片单独排一张图。

        按键的分格（1 路一块 / 2–3 路竖条 / 4 路以上胶囊）在整墙截图里只有
        一两百像素宽，看不出竖排文字和条间缝隙，所以单独按 1/2/3 路各抓一张放大。
        """
        from mihome.ui.device_card import DeviceCard

        def collect() -> list:
            return [c for c in win.findChildren(DeviceCard) if c.state.card.gangs]

        home = win.store.active_home
        cards = collect()
        restored = False
        if not cards:
            # 深色那张是在切到第二个家庭之后拍的，那个家庭可能一台多键开关都没有；
            # 临时切回有的家庭，抓完切回去，免得影响后面的步骤。
            for home_name in win.store.home_names():
                if any(d.node.home == home_name and d.card.gangs
                       for d in win.store.devices.values()):
                    win._on_home_selected(home_name)  # noqa: SLF001
                    QApplication.processEvents()
                    cards = collect()
                    restored = True
                    break
        if not cards:
            print(f"    没有多键设备，跳过 {name}")
            return
        cards.sort(key=lambda c: (len(c.state.card.gangs), c.state.did))
        seen: set[int] = set()
        picked = []
        for c in cards:
            n = len(c.state.card.gangs)
            if n not in seen:
                seen.add(n)
                picked.append(c)
        zoom, pad = 2, 14
        shots = [c.grab().toImage() for c in picked[:4]]
        w, h = shots[0].width(), shots[0].height()
        canvas = QImage(len(shots) * (w * zoom + pad) + pad, h * zoom + pad * 2,
                        QImage.Format_ARGB32)
        canvas.fill(QColor(theme.DARK.bg if win.palette_ref.is_dark else theme.LIGHT.bg))
        painter = QPainter(canvas)
        for i, shot in enumerate(shots):
            painter.drawImage(pad + i * (w * zoom + pad), pad,
                              shot.scaled(w * zoom, h * zoom, Qt.KeepAspectRatio,
                                          Qt.SmoothTransformation))
        painter.end()
        canvas.save(str(out / name))
        print(f"已保存 {name}  {canvas.width()}x{canvas.height()}"
              f"（{len(picked)} 种路数：{[len(c.state.card.gangs) for c in picked]}）")
        if restored and home:
            win._on_home_selected(home)  # noqa: SLF001
            QApplication.processEvents()

    def step_settings() -> None:
        from mihome.ui.settings import SettingsDialog

        dlg = SettingsDialog(win.store, win.palette_ref, win,
                             account=win.client.account_info())
        dlg.show()

        def page_account() -> None:
            dlg.nav.setCurrentRow(3)
            QTimer.singleShot(300, lambda: (grab("settings_account.png", dlg),
                                            dlg.close(), step_dim()))

        def page_hidden() -> None:
            dlg.nav.setCurrentRow(2)
            QTimer.singleShot(300, lambda: (grab("settings_hidden.png", dlg), page_account()))

        def page_order() -> None:
            dlg.nav.setCurrentRow(1)

            def shoot() -> None:
                # 选中一行再截：选中块的形状只有选中态才看得出来——曾经缩进区会
                # 多出一块系统高亮蓝，和这边自绘的圆角块并排。
                tree = dlg.tree_rooms
                home = tree.topLevelItem(0)
                if home is not None and home.childCount():
                    tree.setCurrentItem(home.child(0))
                QTimer.singleShot(150, lambda: (grab("settings_order.png", dlg), page_hidden()))

            QTimer.singleShot(300, shoot)

        dlg.nav.setCurrentRow(0)
        QTimer.singleShot(400, lambda: (grab("settings_appearance.png", dlg), page_order()))

    def step_dim() -> None:
        """子窗口弹出时主窗口被压暗的样子（详情页自己的 exec 里看不到，单独截）。"""
        win._dim.cover(True)  # noqa: SLF001
        QTimer.singleShot(300, lambda: (
            grab("main_dim.png"), win._dim.cover(False), step_detail()  # noqa: SLF001
        ))

    def step_detail() -> None:
        from mihome.ui.detail import DetailDialog

        st = next(
            (d for d in win.store.devices.values()
             if d.online and d.card.controls and d.card.category == "light"),
            next(iter(win.store.devices.values())),
        )
        dlg = DetailDialog(st, win.store, win.palette_ref, win, icons=win.icon_provider)
        dlg.show()
        QTimer.singleShot(1600, lambda: (grab("detail.png", dlg), dlg.close(), step_tabs()))

    def step_tabs() -> None:
        """把家庭标签换成"激活置顶 + 胶囊"，截一张对比图，然后还原。"""
        keep = (win.store.home_placement, win.store.home_active_style)
        win.store.set_home_layout(placement="active_first", style="pill")
        QTimer.singleShot(500, lambda: (
            grab("tabs_pill.png"),
            win.store.set_home_layout(placement=keep[0], style=keep[1]),
            QTimer.singleShot(400, step_home2),
        ))

    def step_home2() -> None:
        names = win.store.home_names()
        if len(names) < 2:
            step_hidden()
            return
        win._on_home_selected(names[1])  # noqa: SLF001
        QTimer.singleShot(700, lambda: (grab("home2.png"), step_hidden()))

    def step_hidden() -> None:
        active = win.store.active_home
        victim = next(
            (d for d in win.store.devices.values()
             if d.node.home == active and not win.store.prefs.is_hidden(d.did)),
            None,
        )
        if victim is not None:
            win.store.set_hidden(victim.did, True)
        QTimer.singleShot(600, lambda: (win.wall.verticalScrollBar().setValue(
            win.wall.verticalScrollBar().maximum()), grab("hidden.png"), step_dark()))

    def step_dark() -> None:
        win.set_theme("dark")
        QTimer.singleShot(700, lambda: (grab("main_dark.png"), save_gangs("switch_cards_dark.png"),
                                        step_detail_dark()))

    def step_detail_dark() -> None:
        """深色下再截一张详情页：深色里"两块同色"最难看，必须单独验。"""
        from mihome.ui.detail import DetailDialog

        st = next(
            (d for d in win.store.devices.values()
             if d.online and d.card.controls and d.card.category == "light"),
            next(iter(win.store.devices.values())),
        )
        dlg = DetailDialog(st, win.store, win.palette_ref, win, icons=win.icon_provider)
        dlg.show()
        QTimer.singleShot(1000, lambda: (grab("detail_dark.png", dlg), dlg.close(),
                                        step_settings_dark()))

    def step_settings_dark() -> None:
        """深色下再截一遍设置页：分类栏和内容区在深色里最容易糊成一片
        （浅色下差 17 个明度档，深色只剩 4 档时就该重算底色了）。"""
        from mihome.ui.settings import SettingsDialog

        dlg = SettingsDialog(win.store, win.palette_ref, win,
                             account=win.client.account_info())
        dlg.show()

        def page_account() -> None:
            dlg.nav.setCurrentRow(3)
            QTimer.singleShot(300, lambda: (grab("settings_account_dark.png", dlg),
                                            dlg.close(), step_tooltip()))

        def page_hidden() -> None:
            dlg.nav.setCurrentRow(2)
            QTimer.singleShot(300, lambda: (grab("settings_hidden_dark.png", dlg),
                                            page_account()))

        def page_order() -> None:
            dlg.nav.setCurrentRow(1)

            def shoot() -> None:
                tree = dlg.tree_rooms
                home = tree.topLevelItem(0)
                if home is not None and home.childCount():
                    tree.setCurrentItem(home.child(0))
                QTimer.singleShot(150, lambda: (grab("settings_order_dark.png", dlg),
                                                page_hidden()))

            QTimer.singleShot(300, shoot)

        dlg.nav.setCurrentRow(0)
        QTimer.singleShot(400, lambda: (grab("settings_appearance_dark.png", dlg),
                                        page_order()))

    def step_tooltip() -> None:
        """悬停提示是独立窗口，grab 主窗口时带不上，单独渲染一张对照。

        行为验证在 ``scripts/verify_tooltip.py``（离屏、不加窗口），
        这里出的是给人看的图——离屏平台没有字体，中文会变豆腐块。
        """
        from mihome.ui import tooltip

        bubble = tooltip.manager()._bubble  # noqa: SLF001 - 截图脚本，白盒
        shots = []
        for pal in (theme.LIGHT, theme.DARK):
            bubble.show_for("小米米家智能开关（三开单控）", QPoint(0, 0), pal)
            app.processEvents()
            shots.append(bubble.grab().toImage())
        bubble.hide()

        zoom, pad = 2, 14
        width = max(s.width() for s in shots) * zoom + pad * 2
        height = sum(s.height() for s in shots) * zoom + pad * 3
        canvas = QImage(width, height, QImage.Format_ARGB32)
        canvas.fill(QColor("#EDEFF2"))
        painter = QPainter(canvas)
        y = pad
        for shot in shots:
            painter.drawImage(pad, y, shot.scaled(
                shot.width() * zoom, shot.height() * zoom,
                Qt.KeepAspectRatio, Qt.SmoothTransformation))
            y += shot.height() * zoom + pad
        painter.end()
        canvas.save(str(out / "tooltip.png"))
        print(f"已保存 tooltip.png  {canvas.width()}x{canvas.height()}")
        step_combo()

    def step_combo() -> None:
        """下拉框对照：收起（v）/ 展开（^ + 圆角列表）× 浅色 / 深色。

        弹出层是独立的顶层窗口，``grab`` 主窗口带不上，所以和 tooltip 一样
        单独合成一张图。展开格按 ``_place_below`` 的真实几何叠：控件在上、
        列表在下方留 8px、右边缘对齐——控件和当前选项全程可见。
        四角是不是真透明不靠这张图（``grab`` 只是控件层），查
        ``verify_combo.py --native`` 里的 ``WS_EX_LAYERED`` / alpha 断言。
        """
        from mihome.ui.combo import POPUP_GAP, Combo

        raw: list[tuple[theme.Palette, QImage, QImage, QImage]] = []
        for pal in (theme.LIGHT, theme.DARK):
            c = Combo(pal)
            c.addItems(["保持设定顺序", "激活的排到最左", "按名称排序"])
            c.setFixedWidth(190)
            c.show()
            app.processEvents()
            closed = c.grab().toImage()
            c.showPopup()
            app.processEvents()
            app.processEvents()
            opened = c.grab().toImage()
            popup = c.view().parentWidget().grab().toImage()
            # grab() 出来的 QImage 带着 1.75 的 DPR；拼板用的是物理像素坐标，
            # 不把 DPR 抹掉的话 drawImage 会再缩放一次，图层会缩小甚至跑出画布。
            for layer in (closed, opened, popup):
                layer.setDevicePixelRatio(1.0)
            raw.append((pal, closed, opened, popup))
            c.hidePopup()
            c.close()
            app.processEvents()

        # 格子高度按最高的那张展开图算，别把最后一项裁掉；gap 也要按 DPR 放大，
        # 因为 grab() 出来的是物理像素，而 POPUP_GAP 是逻辑像素
        gap = round(POPUP_GAP * app.devicePixelRatio())
        cw = max(250, max(t[3].width() for t in raw) + 60)
        ch = max(24 + t[2].height() + gap + t[3].height() + 16 for t in raw)
        print("下拉框图层尺寸：",
              " / ".join(f"{t[0].name} {t[1].width()}x{t[1].height()}"
                         f"→{t[3].width()}x{t[3].height()}" for t in raw))

        def cell(pal, top: QImage, bottom: QImage | None = None) -> QImage:
            img = QImage(cw, ch, QImage.Format_ARGB32)
            img.fill(QColor(theme.surface_bg(pal)))
            p = QPainter(img)
            p.drawImage(20, 24, top)
            if bottom is not None:
                # 与 _place_below 一致：右边缘对齐、下方留 gap，绝不盖住控件
                p.drawImage(20 + top.width() - bottom.width(),
                            24 + top.height() + gap, bottom)
            p.end()
            return img

        cells: list[QImage] = []
        for pal, closed, opened, popup in raw:
            cells.append(cell(pal, closed))
            cells.append(cell(pal, opened, popup))

        # Windows 上 DPR 常是 1.75，grab() 出来的已经是放大过的物理像素；
        # 再乘 2 会把 13px 的字放大到糊，这里 1:1 拼板最清楚。
        zoom, pad = 1, 16
        bw, bh = cw * zoom, ch * zoom
        board = QImage(bw * 2 + pad * 3, bh * 2 + pad * 3, QImage.Format_ARGB32)
        board.fill(QColor("#D8DBE0"))
        p = QPainter(board)
        for i, shot in enumerate(cells):
            x = pad + (i % 2) * (bw + pad)
            y = pad + (i // 2) * (bh + pad)
            # 整数倍放大用最近邻：SmoothTransformation 会把 13px 的中文字糊出重影
            p.drawImage(x, y, shot.scaled(bw, bh, Qt.KeepAspectRatio,
                                          Qt.FastTransformation))
        p.end()
        board.save(str(out / "combo.png"))
        print(f"已保存 combo.png  {board.width()}x{board.height()}")

        # 单独一张对照：把列表的临时高亮挪到第三项（等同于鼠标划过去），
        # 值仍是第一项。左边"勾与高亮重合"，右边"高亮走了、勾还钉在原项"。
        check_cells: list[QImage] = []
        for pal in (theme.LIGHT, theme.DARK):
            c = Combo(pal)
            c.addItems(["保持设定顺序", "激活的排到最左", "按名称排序"])
            c.setFixedWidth(190)
            c.show()
            app.processEvents()
            c.showPopup()
            app.processEvents()
            app.processEvents()
            cont = c.view().parentWidget()
            first = cont.grab().toImage()
            v = c.view()
            v.setCurrentIndex(v.model().index(2, 0))
            v.viewport().update()
            app.processEvents()
            moved = cont.grab().toImage()
            for layer in (first, moved):
                layer.setDevicePixelRatio(1.0)
                check_cells.append(layer)
            c.hidePopup()
            c.close()
            app.processEvents()

        c2w = max(x.width() for x in check_cells) + 40
        c2h = max(x.height() for x in check_cells) + 64
        board2 = QImage(c2w * 2 + 48, c2h * 2 + 48, QImage.Format_ARGB32)
        board2.fill(QColor("#D8DBE0"))
        p2 = QPainter(board2)
        caption = QFont("Microsoft YaHei UI")
        caption.setPixelSize(round(13 * app.devicePixelRatio()))
        p2.setFont(caption)
        titles = ("刚展开：勾与高亮重合", "高亮移走：勾还在原项")
        for i, shot in enumerate(check_cells):
            pal = theme.LIGHT if i < 2 else theme.DARK
            x = 16 + (i % 2) * (c2w + 16)
            y = 16 + (i // 2) * (c2h + 16)
            p2.fillRect(x, y, c2w, c2h, QColor(theme.surface_bg(pal)))
            p2.setPen(QColor(pal.text_soft))
            p2.drawText(x + 16, y + 30,
                        f"{'浅色' if i < 2 else '深色'} · {titles[i % 2]}")
            p2.drawImage(x + 20, y + 44, shot)
        p2.end()
        board2.save(str(out / "combo_check.png"))
        print(f"已保存 combo_check.png  {board2.width()}x{board2.height()}")

        # 再给一张"设置页里真展开"的图：popup 是独立顶层窗口，dlg.grab() 带不上，
        # 按它实际的全局坐标叠回窗口截图。四角靠 alpha 混合，黑块会一眼看出来。
        def settings_combo(pal, name: str) -> None:
            from mihome.ui.settings import SettingsDialog

            dlg = SettingsDialog(win.store, pal, win,
                                 account=win.client.account_info())
            dlg.show()
            dlg.nav.setCurrentRow(0)
            app.processEvents()
            box = dlg.cb_placement
            box.showPopup()
            for _ in range(4):
                app.processEvents()
            base = dlg.grab().toImage()
            base.setDevicePixelRatio(1.0)
            cont = box.view().parentWidget()
            pop = cont.grab().toImage()
            pop.setDevicePixelRatio(1.0)
            dpr = app.devicePixelRatio()
            origin = dlg.mapToGlobal(QPoint(0, 0))
            at = cont.mapToGlobal(QPoint(0, 0))
            canvas = QImage(base)
            p = QPainter(canvas)
            p.drawImage(round((at.x() - origin.x()) * dpr),
                        round((at.y() - origin.y()) * dpr), pop)
            p.end()
            canvas.save(str(out / name))
            print(f"已保存 {name}  {canvas.width()}x{canvas.height()}")
            box.hidePopup()
            dlg.close()
            app.processEvents()

        settings_combo(theme.LIGHT, "settings_combo.png")
        settings_combo(theme.DARK, "settings_combo_dark.png")

        # 单独出一张"固定顺序 + 胶囊底色、激活项在中间"的设置页预览：
        # 这正是用户反馈"激活项明显偏右"的那一屏，浅深各一张便于对照。
        # 只改对话框里的下拉显示和预览实例，不写 store（免得还要还原主窗口）。
        def tabs_preview(pal: theme.Palette, name: str) -> None:
            from mihome.ui.settings import SettingsDialog

            dlg = SettingsDialog(win.store, pal, win,
                                 account=win.client.account_info())
            dlg.show()
            dlg.nav.setCurrentRow(0)
            app.processEvents()
            for box, value in ((dlg.cb_placement, "fixed"),
                               (dlg.cb_style, "pill")):
                box.blockSignals(True)
                box.setCurrentIndex(box.findData(value))
                box.blockSignals(False)
            dlg.preview.configure("fixed", "pill")
            app.processEvents()

            base = dlg.grab().toImage()
            base.setDevicePixelRatio(1.0)
            dpr = app.devicePixelRatio()
            top = dlg.preview.mapTo(dlg, QPoint(0, 0))
            y0 = max(0, round((top.y() - 190) * dpr))
            h = min(base.height() - y0, round(300 * dpr))
            crop = base.copy(0, y0, base.width(), h)
            crop.save(str(out / name))
            print(f"已保存 {name}  {crop.width()}x{crop.height()}")
            dlg.close()
            app.processEvents()

        tabs_preview(theme.LIGHT, "settings_tabs_pill.png")
        tabs_preview(theme.DARK, "settings_tabs_pill_dark.png")
        step_card_style()

    def step_card_style() -> None:
        """卡片样式：浅深 × 标准 1×1 / 双滑块 2×1，外加真实墙面一例。

        卡片本身是普通 QWidget，``grab()`` 能直接拿到；但 ``grab()`` 出来的
        QImage 带着 1.75 的 DPR，拼板前一律抹成 1 再按**物理像素**坐标摆，
        和 ``step_combo()`` 同一套规矩。
        """
        import tempfile
        from types import SimpleNamespace

        from PySide6.QtWidgets import QWidget  # noqa: E402

        from mihome.core.prefs import Prefs  # noqa: E402
        from mihome.core.spec import PropertySpec  # noqa: E402
        from mihome.core.store import DeviceState  # noqa: E402
        from mihome.core.tree import DeviceNode  # noqa: E402
        from mihome.ui.card_templates import (  # noqa: E402
            LIGHT_DUAL, STANDARD, available_styles, resolve_card_style, style_by_id,
        )
        from mihome.ui.menu import Menu, add_choice, submenu  # noqa: E402
        from mihome.ui.device_card import DeviceCard  # noqa: E402

        dpr = app.devicePixelRatio()
        real_light = next((d for d in win.store.devices.values()
                           if d.card.category == "light"), None)
        model = real_light.node.model if real_light is not None else "m"

        def p(siid: int, piid: int, rng=None, unit: str | None = None) -> PropertySpec:
            return PropertySpec(
                siid=siid, piid=piid, name="p", urn="", description="", comment="",
                format="uint16", access=("read", "write", "notify"), unit=unit,
                value_range=rng, value_list=(), service="", service_label="",
            )

        def state() -> DeviceState:
            ctls = {
                "power": SimpleNamespace(slot="power", label="开关", prop=p(2, 1),
                                         write=True, role="switch"),
                "brightness": SimpleNamespace(slot="brightness", label="亮度",
                                              prop=p(2, 2, (1, 100, 1), "percentage"),
                                              write=True, role="slider"),
                "color_temp": SimpleNamespace(slot="color_temp", label="色温",
                                              prop=p(2, 3, (2700, 6500, 1), "kelvin"),
                                              write=True, role="slider"),
            }
            card = SimpleNamespace(
                category="light", span=1, gangs=[], controls=list(ctls.values()),
                metrics=[], layout="light", power_actions=None, default_style="",
                control=lambda slot: ctls.get(slot),
            )
            card.bound = card.control
            node = DeviceNode("shot_d1", "客厅吸顶灯 Pro", model, "light.x",
                              home="我的家", room="客厅")
            return DeviceState(node=node, spec=None, card=card,
                               values={(2, 1): True, (2, 2): 60, (2, 3): 4000})

        panes: list[tuple[str, QImage]] = []
        for pal in (theme.LIGHT, theme.DARK):
            for preset in (style_by_id(STANDARD), style_by_id(LIGHT_DUAL)):
                prefs = Prefs(Path(tempfile.mkdtemp()) / "prefs.json")
                prefs.set_card_style("shot_d1", preset.id)
                st = state()
                c = DeviceCard(st, win.icon_provider, pal,
                               resolve_card_style(prefs, "shot_d1", st.card))
                # 直接 grab 卡片会用不透明黑填圆角外，抗锯齿边缘混进黑色后
                # 浅色底上会留一圈暗描边。套一层同色父容器再抓，圆角外就是
                # pal.bg，和屏幕上「卡片画在窗口底色上」的合成结果一致。
                holder = QWidget()
                holder.setObjectName("CardShotHolder")
                holder.setStyleSheet(f"#CardShotHolder {{ background: {pal.bg}; }}")
                margin = 2
                holder.setFixedSize(c.width() + margin * 2, c.height() + margin * 2)
                c.setParent(holder)
                c.move(margin, margin)
                holder.show()
                c.show()
                app.processEvents()
                img = holder.grab().toImage()
                img.setDevicePixelRatio(1.0)
                panes.append((preset.menu_label, img))
                holder.close()
                app.processEvents()

        pad, head, label_h = (round(v * dpr) for v in (20, 44, 26))
        col_w = round(364 * dpr)
        row_h = label_h + max(x[1].height() for x in panes) + round(16 * dpr)
        board = QImage(col_w * 2 + pad * 3,
                       head + row_h * 2 + pad * 2, QImage.Format_ARGB32)
        board.fill(QColor("#2A2D33"))
        painter = QPainter(board)
        painter.setRenderHint(QPainter.Antialiasing)
        font = QFont("Microsoft YaHei UI")
        for ci, pal in enumerate((theme.LIGHT, theme.DARK)):
            x0 = pad + ci * (col_w + pad)
            painter.fillRect(QRect(x0, pad, col_w, board.height() - pad * 2),
                             QColor(pal.bg))
            font.setPixelSize(round(15 * dpr))
            painter.setFont(font)
            painter.setPen(QColor(pal.text))
            painter.drawText(QRect(x0 + round(20 * dpr), pad, col_w, head),
                             Qt.AlignLeft | Qt.AlignVCenter,
                             "浅色主题" if not pal.is_dark else "深色主题")
            for ri in range(2):
                label, img = panes[ci * 2 + ri]
                y = pad + head + ri * row_h
                font.setPixelSize(round(12 * dpr))
                painter.setFont(font)
                painter.setPen(QColor(pal.text_soft))
                painter.drawText(QRect(x0 + round(20 * dpr), y, col_w, label_h),
                                 Qt.AlignLeft | Qt.AlignVCenter, label)
                px, py = x0 + round(20 * dpr), y + label_h
                painter.drawImage(QPoint(px, py), img)
        painter.end()
        board.save(str(out / "card_style.png"))
        print(f"已保存 card_style.png  {board.width()}x{board.height()}")

        # 真实账号里找一盏真的绑上了亮度 + 色温的灯，改样式抓一张墙面，
        # 抓完立刻还原；prefs.json 在 done() 里还会整体回滚一次。
        def slider_ready(ctl) -> bool:
            return bool(ctl is not None and ctl.prop is not None
                        and ctl.prop.value_range)

        # 前面的步骤切到了第二个家庭，那边的灯少；先回设备多的家庭，
        # 并且只认当前墙上真的有卡片的 did（隐藏分组 / 别的家庭都不算）。
        names = win.store.home_names()
        if names:
            win._on_home_selected(names[0])  # noqa: SLF001
            app.processEvents()
        target = next(
            (d for d in win.store.devices.values()
             if d.did in win.wall._cards  # noqa: SLF001
             and d.card.category == "light"
             and slider_ready(d.card.control("brightness"))
             and slider_ready(d.card.control("color_temp"))),
            None,
        )
        if target is not None:
            win.store.set_card_style(target.did, LIGHT_DUAL)
            win.wall.apply_card_style(target.did)
            app.processEvents()
            card = win.wall._cards.get(target.did)
            if card is not None:
                card.grab().save(str(out / "wall_card_style_card.png"))
                print(f"已保存 wall_card_style_card.png  {target.name}")
            grab("wall_card_style.png")
            win.store.set_card_style(target.did)  # 回默认 1×1
            win.wall.apply_card_style(target.did)
            app.processEvents()
        else:
            print("    真实账号里没有亮度+色温齐备的灯，跳过墙面图")

        # 右键菜单：两层独立顶层窗口（子菜单不能让 Qt 自己建，否则方底），
        # 分别 grab 后按位置叠。真机上一次只展开一层，这张是结构示意。
        pal = theme.LIGHT
        root = Menu(None, pal)
        root.addAction("打开详情")
        root.addSeparator()
        style_m = submenu(root, "卡片样式", pal)
        for preset in available_styles(state().card):
            add_choice(style_m, preset.menu_label, ("style", preset.id),
                       ("style", LIGHT_DUAL), pal)
        root.addSeparator()
        root.addAction("隐藏设备")
        for w in (root, style_m):
            w.show()
        app.processEvents()
        app.processEvents()
        layers = []
        for w in (root, style_m):
            img = w.grab().toImage()
            img.setDevicePixelRatio(1.0)
            layers.append(img)
            w.close()
        app.processEvents()

        gap = round(16 * dpr)
        ox, oy = round(36 * dpr), round(72 * dpr)
        sub_x = ox + layers[0].width() + gap
        menu_board = QImage(
            sub_x + layers[1].width() + ox,
            oy + max(layers[0].height(), layers[1].height()) + ox,
            QImage.Format_ARGB32,
        )
        menu_board.fill(QColor(pal.bg))
        mp = QPainter(menu_board)
        mp.setRenderHint(QPainter.Antialiasing)
        font.setPixelSize(round(15 * dpr))
        mp.setFont(font)
        mp.setPen(QColor(pal.text))
        mp.drawText(QRect(ox, round(14 * dpr), menu_board.width() - ox * 2,
                          round(44 * dpr)),
                    Qt.AlignLeft | Qt.AlignVCenter,
                    "设备右键菜单 · 卡片样式（结构示意）")
        mp.drawImage(QPoint(ox, oy), layers[0])
        mp.drawImage(QPoint(sub_x, oy), layers[1])
        mp.end()
        menu_board.save(str(out / "card_style_menu.png"))
        print(f"已保存 card_style_menu.png  {menu_board.width()}x{menu_board.height()}")

        # ---- 存在传感器：品类默认样式直接显示有人/无人 -------------------
        # 这张不是"某个可选样式"，而是**没有偏好时**的默认外观：卡片正中一个
        # 大字读数，副标题让给照度。浅/深 × 有人/无人 四格对照。
        from mihome.core.spec import ValueOption  # noqa: E402
        from mihome.ui.card_templates import PRESENCE_STATE  # noqa: E402

        occ = PropertySpec(
            siid=3, piid=1, name="p", urn="", description="", comment="",
            format="uint8", access=("read", "notify"), unit=None,
            value_range=None,
            value_list=(ValueOption(0, "无人", "no-one"),
                        ValueOption(1, "有人", "someone")),
            service="", service_label="",
        )
        lux = p(3, 2, None, "percentage")

        def presence_state(occupied: bool) -> DeviceState:
            card = SimpleNamespace(
                category="occupancy-sensor", span=1, gangs=[], controls=[],
                metrics=[
                    SimpleNamespace(slot="occupancy", label="人体状态", prop=occ,
                                    write=False, role="readout"),
                    SimpleNamespace(slot="illumination", label="照度", prop=lux,
                                    write=False, role="readout"),
                ],
                layout="sensor", power_actions=None, default_style=PRESENCE_STATE,
                control=lambda slot: None,
            )
            card.bound = lambda slot: next(
                (m for m in card.metrics if m.slot == slot), None
            )
            node = DeviceNode("shot_p1", "领普人体存在传感器", model,
                              "occupancy-sensor.x", home="我的家", room="客厅")
            return DeviceState(node=node, spec=None, card=card,
                               values={(3, 1): 1 if occupied else 0, (3, 2): 49})

        # 行 = 有人/无人，列 = 浅色/深色。循环按这个顺序走，拼板时用
        # (is_dark, occupied) 直接定位，不靠下标猜行列——之前按"pal 外层"生成、
        # 又按"列=深浅"贴，结果第二行深色卡贴到了浅色底上。
        cells: list[tuple[bool, bool, QImage]] = []
        for occupied in (True, False):
            for pal in (theme.LIGHT, theme.DARK):
                st = presence_state(occupied)
                fresh = Prefs(Path(tempfile.mkdtemp()) / "p.json")
                c = DeviceCard(st, win.icon_provider, pal,
                               resolve_card_style(fresh, "shot_p1", st.card))
                holder = QWidget()
                holder.setObjectName("CardShotHolder")
                holder.setStyleSheet(f"#CardShotHolder {{ background: {pal.bg}; }}")
                holder.setFixedSize(c.width() + 4, c.height() + 4)
                c.setParent(holder)
                c.move(2, 2)
                holder.show()
                c.show()
                app.processEvents()
                img = holder.grab().toImage()
                img.setDevicePixelRatio(1.0)
                cells.append((pal.is_dark, occupied, img))
                holder.close()
                app.processEvents()

        p_pad, p_head, p_label = (round(v * dpr) for v in (18, 42, 24))
        p_col = round(190 * dpr)
        p_row = p_label + max(x[2].height() for x in cells) + round(14 * dpr)
        p_board = QImage(p_col * 2 + p_pad * 3, p_head + p_row * 2 + p_pad * 2,
                         QImage.Format_ARGB32)
        p_board.fill(QColor("#2A2D33"))
        pp = QPainter(p_board)
        pp.setRenderHint(QPainter.Antialiasing)
        font.setPixelSize(round(14 * dpr))
        pp.setFont(font)
        pp.setPen(QColor("#E8EAED"))
        pp.drawText(QRect(p_pad, round(10 * dpr), p_board.width() - p_pad * 2,
                          round(26 * dpr)),
                    Qt.AlignLeft | Qt.AlignVCenter,
                    "存在传感器 · 品类默认样式「状态」（副标题不随样式变）")
        font.setPixelSize(round(12 * dpr))
        for ci, (is_dark, occupied, img) in enumerate(cells):
            col, row = ci % 2, ci // 2
            x0 = p_pad + col * (p_col + p_pad)
            y0 = p_head + row * p_row
            pp.fillRect(QRect(x0, y0, p_col, p_row - round(6 * dpr)),
                        QColor(theme.DARK.bg if is_dark else theme.LIGHT.bg))
            pp.setFont(font)
            pp.setPen(QColor("#C9CDD4"))
            label = f"{'深色' if is_dark else '浅色'} · {'有人' if occupied else '无人'}"
            pp.drawText(QRect(x0, y0 + round(2 * dpr), p_col, p_label),
                        Qt.AlignCenter, label)
            pp.drawImage(QPoint(x0 + (p_col - img.width()) // 2, y0 + p_label), img)
        pp.end()
        p_board.save(str(out / "card_presence.png"))
        print(f"已保存 card_presence.png  {p_board.width()}x{p_board.height()}")

        # ---- 空调：宽卡 2×1「温度控制」-----------------------------------
        # 大数字步进器 + 模式胶囊。四格 = 浅/深 × 开/关：关机那两格要能看出
        # "控件还在、只是压暗"（米家也是这样，先调好温度再开机）。
        from mihome.ui.card_templates import TEMP_CONTROL, style_by_id  # noqa: E402

        tgt = p(2, 4, (16, 32, 1), "celsius")
        power_prop = PropertySpec(
            siid=2, piid=1, name="p", urn="", description="", comment="",
            format="bool", access=("read", "write", "notify"), unit=None,
            value_range=None, value_list=(), service="", service_label="",
        )
        mode_prop = PropertySpec(
            siid=2, piid=2, name="p", urn="", description="", comment="",
            format="uint8", access=("read", "write", "notify"), unit=None,
            value_range=None,
            value_list=tuple(
                ValueOption(v, t, t) for v, t in
                ((0, "自动"), (1, "制冷"), (2, "除湿"), (3, "制热"), (4, "送风"))),
            service="", service_label="",
        )
        room = p(4, 7, None, "celsius")
        hum = p(4, 9, None, "percentage")

        def ac_state(powered: bool) -> DeviceState:
            ctls = {
                "power": SimpleNamespace(slot="power", label="开关",
                                         prop=power_prop, write=True, role="switch"),
                "target_temp": SimpleNamespace(slot="target_temp", label="温度",
                                               prop=tgt, write=True, role="stepper"),
                "mode": SimpleNamespace(slot="mode", label="模式",
                                        prop=mode_prop, write=True, role="select"),
            }
            metrics = [
                SimpleNamespace(slot="temperature", label="室温", prop=room,
                                write=False, role="readout"),
                SimpleNamespace(slot="humidity", label="湿度", prop=hum,
                                write=False, role="readout"),
            ]
            card = SimpleNamespace(
                category="air-conditioner", span=1, gangs=[], controls=list(ctls.values()),
                metrics=metrics, layout="climate", power_actions=None, default_style="",
                control=lambda slot: ctls.get(slot),
            )
            card.bound = lambda slot: ctls.get(slot) or next(
                (m for m in metrics if m.slot == slot), None)
            node = DeviceNode("shot_ac", "空调", model, "air-conditioner.x",
                              home="我的家", room="强")
            return DeviceState(
                node=node, spec=None, card=card,
                values={(2, 1): powered, (2, 4): 26, (2, 2): 1,
                        (4, 7): 24.9, (4, 9): 54})

        ac_cells: list[tuple[bool, bool, QImage]] = []
        for powered in (True, False):
            for pal in (theme.LIGHT, theme.DARK):
                st = ac_state(powered)
                c = DeviceCard(st, win.icon_provider, pal,
                               style_by_id(TEMP_CONTROL))
                holder = QWidget()
                holder.setObjectName("CardShotHolder")
                holder.setStyleSheet(f"#CardShotHolder {{ background: {pal.bg}; }}")
                holder.setFixedSize(c.width() + 4, c.height() + 4)
                c.setParent(holder)
                c.move(2, 2)
                holder.show()
                c.show()
                app.processEvents()
                img = holder.grab().toImage()
                img.setDevicePixelRatio(1.0)
                ac_cells.append((pal.is_dark, powered, img))
                holder.close()
                app.processEvents()

        # 列宽按卡片实际宽度算：2×1 卡是 340，presence 那张的 190 是给 1×1 的，
        # 照抄会把 344 宽的图裁掉两边
        a_col = max(x[2].width() for x in ac_cells) + round(8 * dpr)
        a_row = p_label + max(x[2].height() for x in ac_cells) + round(14 * dpr)
        a_board = QImage(a_col * 2 + p_pad * 3, p_head + a_row * 2 + p_pad * 2,
                         QImage.Format_ARGB32)
        a_board.fill(QColor("#2A2D33"))
        ap = QPainter(a_board)
        ap.setRenderHint(QPainter.Antialiasing)
        font.setPixelSize(round(14 * dpr))
        ap.setFont(font)
        ap.setPen(QColor("#E8EAED"))
        ap.drawText(QRect(p_pad, round(10 * dpr), a_board.width() - p_pad * 2,
                          round(26 * dpr)),
                    Qt.AlignLeft | Qt.AlignVCenter,
                    "空调 · 宽卡 2×1「温度控制」（大数字 ± 模式胶囊）")
        font.setPixelSize(round(12 * dpr))
        for ci, (is_dark, powered, img) in enumerate(ac_cells):
            col, row = ci % 2, ci // 2
            x0 = p_pad + col * (a_col + p_pad)
            y0 = p_head + row * a_row
            ap.fillRect(QRect(x0, y0, a_col, a_row - round(6 * dpr)),
                        QColor(theme.DARK.bg if is_dark else theme.LIGHT.bg))
            ap.setFont(font)
            ap.setPen(QColor("#C9CDD4"))
            label = f"{'深色' if is_dark else '浅色'} · {'开机' if powered else '关机'}"
            ap.drawText(QRect(x0, y0 + round(2 * dpr), a_col, p_label),
                        Qt.AlignCenter, label)
            ap.drawImage(QPoint(x0 + (a_col - img.width()) // 2, y0 + p_label), img)
        ap.end()
        a_board.save(str(out / "card_temp.png"))
        print(f"已保存 card_temp.png  {a_board.width()}x{a_board.height()}")
        step_hidden_fold()

    def step_hidden_fold() -> None:
        """「已隐藏设备」分组展开 / 收起对照（用真实那面墙，滚到底部）。

        现场临时藏两台设备，而不是只截现有的：用户当前可能一台都没隐藏，
        那样这张图就永远是空的。改动只落在内存的 prefs 上，``done()`` 会把
        整份 prefs.json 覆盖回跑之前的样子。
        """
        prefs = win.store.prefs
        dids = list(win.wall._cards)  # noqa: SLF001
        # 尽量挑**两个不同房间**的设备：隐藏分组会按房间再分一层，同房间两台
        # 就截不出那一层，对照图会少讲一半
        picks: list[str] = []
        seen_rooms: set[str] = set()
        for did, c in win.wall._cards.items():  # noqa: SLF001
            room = c.state.room
            if room in seen_rooms:
                continue
            seen_rooms.add(room)
            picks.append(did)
            if len(picks) == 2:
                break
        dids = picks or dids
        if len(dids) < 2:
            print("    设备不足两台，跳过 hidden_fold")
            done()
            return

        saved_hidden = list(prefs.hidden)
        saved_collapsed = prefs.hidden_collapsed

        def shoot(name: str, collapsed: bool) -> None:
            prefs.set_hidden_collapsed(collapsed)
            # 走 main_window 的正规入口：墙和左侧导航一起重建。只调 wall.build()
            # 的话导航上的台数会停在临时藏设备之前的旧值（它数的是同一个 hidden
            # 集合），截图里就出现「导航 19 / 分区 21 台」这种自相矛盾
            win._on_layout_changed()  # noqa: SLF001
            win.wall._relayout()  # noqa: SLF001 — 分组后高度要重算，否则滚动范围是旧的
            app.processEvents()
            app.processEvents()
            sec = win.wall._sections[-1]  # noqa: SLF001
            bar = win.wall.verticalScrollBar()
            # 滚到隐藏分组的**顶部**而不是底部：分组后内容可能比一屏还高，
            # 滚到底只看到最后一两组，反而看不出"按房间分层"这件事
            y = sec.mapTo(win.wall.widget(), QPoint(0, 0)).y()
            bar.setValue(max(0, y - 12))
            app.processEvents()
            grab(name)
            print(f"    hidden 分区高度 {sec.height()}px"
                  f"（{'收起' if collapsed else '展开'}）"
                  f" 滚动 {bar.value()}/{bar.maximum()}"
                  f" 房间组 {len(sec._groups)} 个")

        for did in dids[:2]:
            prefs.set_hidden(did, True)
        prefs.set_hidden_collapsed(False)
        shoot("hidden_fold_open.png", False)
        shoot("hidden_fold_closed.png", True)

        # 还原内存里的偏好（磁盘那份由 done() 整体覆盖，两处都要对得上）
        prefs.hidden = saved_hidden
        prefs.hidden_collapsed = saved_collapsed
        prefs.save()
        win.wall.build()
        app.processEvents()
        done()

    def done() -> None:
        """收尾：还原偏好、退出事件循环。**幂等**——正常走完和异常钩子都会调。"""
        if state["closed"]:
            return
        state["closed"] = True
        win.close()
        settings = QSettings("MiHomePanel", "MiHomePanel")
        settings.setValue("ui/active_home", start_home)
        settings.setValue("ui/theme", start_theme)
        settings.sync()
        if prefs_backup is not None:
            prefs_path.write_bytes(prefs_backup)
        app.quit()

    def tick() -> None:
        if not ready():
            state["wait"] += 1
            if state["wait"] > 200:
                # 200 × 150ms ≈ 30 秒还没数据：多半是没登录、没设备或网络不通。
                # 硬截出来只是一面空墙，所以标成失败并直说下一步——别让人对着一堆
                # 空白图去猜原因。这里必须走 done()，它会还原被改过的家庭/主题和
                # prefs.json；原来直接 app.quit() 会把这轮的改动留在你日常配置里。
                print(
                    "数据未就绪：30 秒内没拿到设备。请先运行 run.bat 登录米家账号，"
                    "确认家里有设备、网络正常，再重跑截图。"
                )
                grab("main.png")  # 留一张现状图，便于分清是登录页还是空墙
                state["failed"] = True
                done()
                return
            QTimer.singleShot(150, tick)
            return
        state["i"] += 1
        if state["i"] < 6:
            QTimer.singleShot(120, tick)
            return
        grab("main.png")
        grab("topbar.png")
        save_gangs("switch_cards.png")
        step_settings()

    def on_uncaught(
        exc_type: type[BaseException],
        exc: BaseException,
        tb: TracebackType | None,
    ) -> None:
        """漏出定时器 / 信号回调的异常统一走这里收尾。

        截图链路全是 ``QTimer.singleShot`` 的连锁回调，PySide6 对回调里的异常
        默认只把 traceback 打到 stderr 然后**继续跑事件循环**——脚本会一直挂着等
        一个再也不会到来的 ``done()``（实测只能靠外层 timeout 杀掉，退出码 124）。
        这里主动还原偏好、退出循环，并让 ``main()`` 返回非零。
        """
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        traceback.print_exception(exc_type, exc, tb)
        state["failed"] = True
        done()

    sys.excepthook = on_uncaught

    QTimer.singleShot(500, tick)
    code = app.exec()
    # 退出码必须跟着失败走：否则漏截几张图也和成功一样返回 0，批量跑截图时
    # 只能靠数文件和时间戳才发现（照抄我上次踩的坑）。
    return code or (1 if state["failed"] else 0)


if __name__ == "__main__":
    sys.exit(main())
