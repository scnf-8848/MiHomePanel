"""主窗口。

只做四件事：撑起外壳（自绘标题栏 + 家庭切换栏 + 侧边栏 + 卡片墙）、把 Store 的变化
转成界面刷新、管理三个定时器、把偏好落盘。

定时器：

* **16ms** —— 从工作队列取回调（``worker.drain``）。这是参考项目验证过的
  "子线程不碰 UI"方案：worker 只负责把结果塞队列，主线程高频取走执行。
* **3s** —— 发起一轮轮询。轮询本身在后台线程，所以 UI 不会卡。
* **5min** —— 重拉拓扑，新增设备不用手动刷新。

网络慢的时候不做堆叠：``Store.poll`` 看到上一轮没回来直接跳过。
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QPoint, QPointF, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPainterPath, QShortcut
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QMainWindow,
    QVBoxLayout,
    QWidget,
)

from ..core.api import Client
from ..core.icons import IconCache
from ..core.jobs import SerialWorker
from ..core.prefs import Prefs
from ..core.store import Store
from . import theme, tooltip
from .chrome import DimOverlay, TitleBar, WindowFrame
from .home_tabs import HomeTabs
from .side_nav import SideNav
from .wall import WallView
from .widgets import HLine, IconProvider, StatusChip

DRAIN_MS = 16
POLL_MS = 3000
TOPOLOGY_MS = 5 * 60 * 1000  # 设备列表低频刷新：新增设备不用手动点刷新


def _refresh_path(c: QPointF, half: float) -> QPainterPath:
    """「刷新」图标：右上开口的圆环，末端甩出一块**实心方头箭头**。

    几何取自 Material Icons 的 ``refresh`` 字形（Apache-2.0，致谢见 README），
    在 24×24 视口里描述、按 ``half`` 缩放并居中。**不要**再自己拿圆 + 三角去拼：
    试过两版都不行——三角箭头的底边是沿半径方向的，32px 按钮里图标只有 ~20px，
    那条 6~7px 的底边会读成"环上鼓起一根竖刺"，而它真正指的方向反而看不见。
    Material 的方头是**一整块**实心色块，缩小后仍然是一个色块，所以看得清。

    ``c`` 是图标中心，``half`` 是图标外接框的半宽（正常 9.9，即约 20px）。
    """
    k = half / 8.0  # 24×24 视口里图形占 4..20，半宽 8 个单位

    def at(x: float, y: float) -> QPointF:
        return QPointF(c.x() + (x - 12.0) * k, c.y() + (y - 12.0) * k)

    d = QPainterPath(at(17.65, 6.35))
    # 外圈：从右上的箭头根部逆时针绕一整圈回到起点
    d.cubicTo(at(16.20, 4.90), at(14.21, 4.00), at(12.00, 4.00))
    d.cubicTo(at(7.58, 4.00), at(4.01, 7.58), at(4.00, 12.00))
    d.cubicTo(at(3.99, 16.42), at(7.58, 20.00), at(12.00, 20.00))
    d.cubicTo(at(15.73, 20.00), at(18.84, 17.45), at(19.73, 14.00))
    # 箭头下沿的缺口，往回收半格形成"箭头压住环"的层次
    d.lineTo(at(17.65, 14.00))
    d.cubicTo(at(16.83, 16.33), at(14.61, 18.00), at(12.00, 18.00))
    d.cubicTo(at(8.69, 18.00), at(6.00, 15.31), at(6.00, 12.00))
    d.cubicTo(at(6.00, 8.69), at(8.69, 6.00), at(12.00, 6.00))
    d.cubicTo(at(13.66, 6.00), at(15.14, 6.69), at(16.22, 7.78))
    # 方头箭头：斜边 → 内角 → 顶边 → 右边
    d.lineTo(at(13.00, 11.00))
    d.lineTo(at(20.00, 11.00))
    d.lineTo(at(20.00, 4.00))
    d.closeSubpath()
    return d


class RoundButton(QWidget):
    """圆形图标按钮。自绘而不是 QSS，是为了保证 hover/按下状态的圆角不出锯齿。"""

    clicked = Signal()

    def __init__(self, glyph: str, palette: theme.Palette, size: int = 32) -> None:
        super().__init__()
        self._glyph = glyph
        self._p = palette
        self._hover = False
        self._down = False
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        self._tip = {"refresh": "刷新设备列表  F5", "theme": "切换深色 / 浅色"}.get(glyph, "")
        if self._tip:
            tooltip.watch(self, lambda: self._tip)

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self._p
        r = self.rect().adjusted(1, 1, -1, -1)
        if self._down:
            p.setBrush(QColor(theme.blend(pal.bg_soft, pal.text, 0.10)))
        elif self._hover:
            p.setBrush(QColor(theme.blend(pal.bg_soft, pal.text, 0.05)))
        else:
            p.setBrush(QColor(pal.bg_soft))
        p.setPen(Qt.NoPen)
        p.drawEllipse(r)

        c = r.center()
        if self._glyph == "refresh":
            # 方头箭头画法，为什么不用三角见 _refresh_path 的 docstring
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(pal.text_soft))
            p.drawPath(_refresh_path(QPointF(c), 9.6))
        elif self._glyph == "theme":
            p.setBrush(QColor(pal.text_soft))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPoint(int(c.x()), int(c.y())), 7, 7)
            p.setBrush(QColor(pal.bg_soft))
            p.drawEllipse(QPoint(int(c.x() + 5), int(c.y() - 4)), 6, 6)
        p.end()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self._down = False
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._down = True
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._down and event.button() == Qt.LeftButton:
            self._down = False
            self.update()
            if self.rect().contains(event.position().toPoint()):
                self.clicked.emit()


class MainWindow(QMainWindow):
    def __init__(self, palette: theme.Palette | None = None) -> None:
        super().__init__()
        self.palette_ref = palette or theme.LIGHT
        self.prefs = Prefs()
        self.setWindowTitle("米家控制台")
        self.resize(1200, 800)
        self.setMinimumSize(920, 600)

        self._frame = WindowFrame(self, resizable=True, fill=lambda: self.palette_ref.bg)

        self.worker = SerialWorker()
        self.client = Client()
        self.icons = IconCache(self.client.raw(), self.worker)
        self.store = Store(self.client, self.worker, prefs=self.prefs)

        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        col = QVBoxLayout(root)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)

        self.titlebar = TitleBar("米家控制台", self.palette_ref, frame=self._frame)
        col.addWidget(self.titlebar)

        header = QWidget()
        header.setFixedHeight(58)
        head = QHBoxLayout(header)
        self._head = head
        head.setContentsMargins(24, 0, 18, 0)
        head.setSpacing(0)

        self.tabs = HomeTabs(self.palette_ref)
        self.tabs.home_selected.connect(self._on_home_selected)
        head.addWidget(self.tabs)
        head.addSpacing(20)

        # 状态做成米家那种深蓝黑胶囊，而不是一行灰字：它是一眼扫的信息块，
        # 落在顶栏右侧时比长句子安静。
        self.status = StatusChip("正在连接…", self.palette_ref)
        tooltip.watch(self.status, self.status.text)
        head.addWidget(self.status)
        head.addStretch(1)

        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索设备    Ctrl+F")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedSize(216, 32)
        # 不抢初始焦点：启动时搜索框带蓝色边框会显得像"待输入"，而且会吞掉快捷键
        self.search.setFocusPolicy(Qt.ClickFocus)
        head.addWidget(self.search)
        head.addSpacing(12)

        self.btn_theme = RoundButton("theme", self.palette_ref)
        self.btn_refresh = RoundButton("refresh", self.palette_ref)
        head.addWidget(self.btn_theme)
        head.addSpacing(8)
        head.addWidget(self.btn_refresh)
        col.addWidget(header)
        col.addWidget(HLine(self.palette_ref.border))

        self.icon_provider = IconProvider(self.icons, root)
        self.icon_provider.ready.connect(self._on_icon_ready)

        # 子窗口弹出时压暗主窗口。放在 root 上但不进布局，按 root 的尺寸铺满。
        self._dim = DimOverlay(root)
        self._dim.set_palette(self.palette_ref)

        body = QWidget()
        body_row = QHBoxLayout(body)
        body_row.setContentsMargins(0, 0, 0, 0)
        body_row.setSpacing(0)
        self.nav = SideNav(self.palette_ref)
        self.nav.room_selected.connect(self._on_room_selected)
        self.nav.settings_requested.connect(self.open_settings)
        body_row.addWidget(self.nav)
        self.wall = WallView(self.store, self.icon_provider, self.palette_ref)
        body_row.addWidget(self.wall, 1)
        col.addWidget(body, 1)

        self.store.on_changed = self._on_changed
        self.store.on_loaded = self._on_loaded
        self.store.on_status = self._set_status
        self.store.on_layout_changed = self._on_layout_changed
        self.store.on_tabs_changed = self._refresh_tabs

        self.wall.card_toggled.connect(self._on_toggle)
        self.wall.card_value_committed.connect(self._on_card_value)
        self.wall.card_style_requested.connect(self._on_card_style)
        self.wall.hide_requested.connect(self._on_hide_requested)
        self.wall.scene_hide_requested.connect(self.store.set_scene_hidden)
        self.search.textChanged.connect(self.wall.set_filter)
        self.wall.scene_run.connect(self._on_scene)
        self.wall.card_clicked.connect(self._on_card_clicked)
        self.btn_refresh.clicked.connect(self._reload)
        self.btn_theme.clicked.connect(self.toggle_theme)

        self._drain_timer = QTimer(self)
        self._drain_timer.timeout.connect(self._drain)
        self._drain_timer.start(DRAIN_MS)

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start(POLL_MS)

        self._topo_timer = QTimer(self)
        self._topo_timer.timeout.connect(self._refresh_topology)
        self._topo_timer.start(TOPOLOGY_MS)

        self._shortcuts = []
        for seq, slot in (
            ("Ctrl+F", self._focus_search),
            ("F5", self._reload),
            ("Esc", self._clear_search),
            ("Ctrl+R", self._reload),
            ("Ctrl+,", self.open_settings),
        ):
            sc = QShortcut(QKeySequence(seq), self)
            sc.activated.connect(slot)
            self._shortcuts.append(sc)

        self._known_dids: set[str] = set()
        self._pending_maximize = False
        self._load_settings()
        self._apply_theme()

    # -- 设置持久化 ------------------------------------------------------
    def _settings(self) -> QSettings:
        return QSettings("MiHomePanel", "MiHomePanel")

    def _load_settings(self) -> None:
        s = self._settings()
        geo = s.value("window/geometry")
        if isinstance(geo, QByteArray) and not geo.isEmpty():
            self.restoreGeometry(geo)
            if self.isMaximized():
                # 无边框窗口的"最大化"是我们自己铺的，交给系统会连任务栏一起盖住，
                # 所以先退回普通态，等窗口显示出来再按 availableGeometry 铺一次。
                self.setWindowState(Qt.WindowNoState)
                self._pending_maximize = True
        if s.value("ui/theme", "light") == "dark":
            self.palette_ref = theme.DARK
        self.store.active_home = str(s.value("ui/active_home", "") or "")

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._pending_maximize:
            self._pending_maximize = False
            self._frame.set_maximized(True)

    def _save_settings(self) -> None:
        s = self._settings()
        s.setValue("window/geometry", self.saveGeometry())
        s.setValue("ui/theme", "dark" if self.palette_ref is theme.DARK else "light")
        s.setValue("ui/active_home", self.store.active_home)
        s.sync()

    def _set_status(self, text: str) -> None:
        # 悬停提示走 tooltip.watch 注册的 provider，这里只要更新文字
        self.status.setText(text)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._save_settings()
        self.prefs.save()
        self.client.flush()
        self.worker.stop()
        super().closeEvent(event)

    # -- 启动 ------------------------------------------------------------
    def start(self) -> None:
        self.wall.show_placeholder("正在连接米家…")
        if not self.client.available:
            self._set_status("未登录 · 需要扫码")
            self._show_login()
            return
        self.store.load()

    def _show_login(self) -> None:
        from .login import LoginDialog

        dlg = LoginDialog(self.client.raw(), self.worker, self.palette_ref, self)

        def ok() -> None:
            # available 有缓存，登录后要让它重新判断
            try:
                self.client.raw()._available_cache = None
            except Exception:  # noqa: BLE001
                pass
            self._set_status("登录成功，正在连接…")
            self.wall.show_placeholder("正在连接米家…")
            self.store.load()

        dlg.succeeded.connect(ok)
        self._run_dialog(dlg)

    def _run_dialog(self, dlg) -> None:
        """模态子窗口统一入口：弹出期间压暗主窗口，关掉就恢复。"""
        self._dim.cover(True)
        try:
            dlg.exec()
        finally:
            self._dim.cover(False)

    # -- 定时器 ----------------------------------------------------------
    def _drain(self) -> None:
        self.worker.drain()

    def _poll(self) -> None:
        if not self.store.loading and self.store.devices:
            self.store.poll()

    def _refresh_topology(self) -> None:
        if not self.store.loading and self.store.devices:
            self.store.load()

    # -- 事件 ------------------------------------------------------------
    def _on_room_selected(self, home: str, room: str) -> None:
        self.wall.scroll_to_room(home, room)

    def _on_home_selected(self, name: str) -> None:
        if name == self.store.active_home:
            return
        self.store.active_home = name
        self.store.scenes = self.store.scenes_for(name)
        self.wall.set_home(name)
        self.nav.build(self.store, name)
        self._settings().setValue("ui/active_home", name)
        self._update_header()
        self.store.poll()

    def _refresh_tabs(self) -> None:
        """把家庭列表 + 排列方式重新灌给顶部标签栏（只影响显示，不动设定顺序）。"""
        homes = self.store.homes()
        active = self.store.active_home or (homes[0].name if homes else "")
        self.tabs.configure(self.store.home_placement, self.store.home_active_style)
        # 胶囊档的标签自带 14px 左内边距，从顶栏左边距里扣掉，标签文字才仍与
        # 下方卡片墙左对齐；切回另外两档时 inset 归零，边距自动还原。
        self._head.setContentsMargins(24 - self.tabs.content_inset, 0, 18, 0)
        self.tabs.set_homes([(h.name, h.display_name) for h in homes], active)

    def _on_changed(self, dids: set[str]) -> None:
        self.wall.update_cards(dids)
        self._update_header()

    def _on_layout_changed(self) -> None:
        """排序或隐藏设备变了：墙和导航都要重建（顺序变了无法增量更新）。"""
        active = self.store.active_home
        self.wall.set_home(active)
        self.nav.build(self.store, active)
        self._refresh_tabs()
        self._update_header()

    def _update_header(self) -> None:
        names = self.store.home_names()
        active = self.store.active_home or (names[0] if names else "")
        devices = [
            d for d in self.store.devices.values()
            if not d.node.home or d.node.home == active
        ]
        on = sum(1 for d in devices if d.any_on)
        offline = sum(1 for d in devices if not d.online)
        hidden = sum(1 for d in devices if self.prefs.is_hidden(d.did))
        bits = [f"{len(devices)} 台设备", f"{on} 台开启"]
        if offline:
            bits.append(f"{offline} 台离线")
        if hidden:
            bits.append(f"{hidden} 台已隐藏")
        self._set_status(" · ".join(bits))

    def _reload(self) -> None:
        self._set_status("正在刷新设备列表…")
        self.store.rebuild()

    def _on_loaded(self) -> None:
        homes = self.store.homes()
        names = [h.name for h in homes]
        if self.store.active_home not in names:
            self.store.active_home = names[0] if names else ""
        active = self.store.active_home
        self.store.scenes = self.store.scenes_for(active)
        self._refresh_tabs()

        dids = set(self.store.devices)
        first = not self.wall._cards  # noqa: SLF001
        if first or dids != self._known_dids or self.wall.home() != active:
            self.wall.set_home(active)
            self.nav.build(self.store, active)
            self._known_dids = dids
        else:
            # 设备集合没变：只刷新卡片，保住滚动位置和鼠标悬停
            self.wall.update_cards(set(self.store.devices))
        self._update_header()
        self.store.poll()

    def _on_toggle(self, did: str, siid: int, piid: int) -> None:
        st = self.store.devices.get(did)
        if st is None or not st.online:
            return
        self.store.toggle(did, siid, piid)

    # -- 卡片上的控件与外观 -----------------------------------------------
    def _on_card_value(self, did: str, siid: int, piid: int, value: object) -> None:
        """卡片滑条松手：和详情页走同一条乐观写入链路。

        拖动过程中只有 ``previewed``，不发请求——云端一次往返几百毫秒，
        边拖边发会把请求队列堵满，设备反应还会滞后于手。
        """
        st = self.store.devices.get(did)
        if st is None or not st.online:
            return
        self.store.set_value(did, (siid, piid), value)

    def _on_card_style(self, did: str, style_id: str) -> None:
        """样式自带宽度：一次写入就同时换掉尺寸和 body，不需要第二个信号。"""
        if self.store.set_card_style(did, style_id):
            self.wall.apply_card_style(did)

    def _on_hide_requested(self, did: str, hidden: bool) -> None:
        self.store.set_hidden(did, hidden)

    def _on_scene(self, scene_id: str) -> None:
        self.store.run_scene(scene_id, self.store.active_home)

    def _focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    def _clear_search(self) -> None:
        if self.search.text():
            self.search.clear()
        else:
            self.search.clearFocus()

    def _on_card_clicked(self, did: str) -> None:
        st = self.store.devices.get(did)
        if st is None:
            return
        from .detail import DetailDialog

        dlg = DetailDialog(st, self.store, self.palette_ref, self, icons=self.icon_provider)
        self._run_dialog(dlg)

    def _on_icon_ready(self, model: str) -> None:
        for card in self.wall._cards.values():  # noqa: SLF001
            if card.state.node.model == model:
                card.update()

    # -- 设置 ------------------------------------------------------------
    def open_settings(self) -> None:
        from .settings import SettingsDialog

        dlg = SettingsDialog(self.store, self.palette_ref, self,
                             account=self.client.account_info())
        dlg.theme_selected.connect(self.set_theme)
        # 账号动作（退出登录 / 扫码登录）不能在设置窗口还开着的时候做：
        # 登录窗口是另一个模态窗口，套在设置里面会把模态栈搞乱。
        action = {"kind": ""}
        dlg.account_action.connect(lambda kind: action.update(kind=kind))
        self._run_dialog(dlg)
        self._apply_theme()
        self._refresh_tabs()
        if action["kind"] == "logout":
            self._logout()
        elif action["kind"] == "login":
            self._show_login()

    def _logout(self) -> None:
        from .confirm import confirm, notice

        path = self.client.auth_path
        self._dim.cover(True)
        try:
            ok = confirm(
                self,
                "退出登录",
                f"会删除本机保存的米家凭据：\n{path}\n\n"
                "米家 App 和云端的登录状态不受影响，但本程序里需要重新扫码才能继续控制设备。",
                self.palette_ref,
                ok_text="退出登录",
                danger=True,
            )
            if not ok:
                return
            self.client.logout()
            if self.client.last_error:
                # 文件被占用 / 只读目录：内存里的凭据已经清掉了，但磁盘上还留着，
                # 下次启动会直接自动登录，得跟用户说清楚。
                notice(self, "凭据文件没有删掉", self.client.last_error, self.palette_ref)
        finally:
            self._dim.cover(False)
        self.store.clear()
        self.wall.show_placeholder("已退出登录，扫码后可继续使用")
        self.tabs.set_homes([], "")
        self.nav.build(self.store, "")
        self._set_status("已退出登录 · 等待扫码")
        self._show_login()

    # -- 主题 ------------------------------------------------------------
    def toggle_theme(self) -> None:
        self.set_theme("light" if self.palette_ref is theme.DARK else "dark")

    def set_theme(self, name: str) -> None:
        palette = theme.DARK if name == "dark" else theme.LIGHT
        if palette is self.palette_ref:
            return
        self.palette_ref = palette
        self._apply_theme()
        self._save_settings()

    def _apply_theme(self) -> None:
        pal = self.palette_ref
        theme.apply_to_app(pal)
        self.setStyleSheet(theme.app_qss(pal))
        self.titlebar.set_palette(pal)
        self.tabs.set_palette(pal)
        self.btn_theme.set_palette(pal)
        self.btn_refresh.set_palette(pal)
        self.nav.set_palette(pal)
        self.wall.set_palette(pal)
        self.wall.rebuild_palette(pal)
        self._dim.set_palette(pal)
        self.status.set_palette(pal)
        tooltip.set_palette(pal)
        self.search.setStyleSheet(
            f"QLineEdit{{background:{pal.bg_soft};color:{pal.text};"
            f"border:1px solid {pal.border};border-radius:16px;padding:0 14px;font-size:13px;"
            f"selection-background-color:{pal.accent};}}"
            f"QLineEdit:focus{{background:{pal.card};border:1px solid {pal.accent};}}"
        )


__all__ = ["MainWindow"]
