"""主窗口。

只做四件事：撑起外壳（自绘标题栏 + 家庭切换栏 + 侧边栏 + 卡片墙）、把 Store 的变化
转成界面刷新、管理三个定时器、把偏好落盘。

定时器：

* **16ms** —— 从工作队列取回调（``worker.drain``）。这是参考项目验证过的
  "子线程不碰 UI"方案：worker 只负责把结果塞队列，主线程高频取走执行。
* **3s** —— 发起一轮轮询。轮询本身在后台线程，所以 UI 不会卡。
  看不见的时候（收进托盘 / 最小化）降到 15s，切回前台立刻补一轮，
  见 ``_apply_poll_mode``。
* **5min** —— 重拉拓扑，新增设备不用手动刷新。

网络慢的时候不做堆叠：``Store.poll`` 看到上一轮没回来直接跳过。
"""

from __future__ import annotations

import time
from typing import Any

from PySide6.QtCore import (
    QByteArray,
    QEvent,
    QPoint,
    QPointF,
    QSettings,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPainterPath, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLineEdit,
    QMainWindow,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..core import speaker
from ..core.alerts import ERROR, TOAST_COOLDOWN, Alert, AlertPolicy
from ..core.api import Client, explain
from ..core.automation import FAILED
from ..core.automation.service import AutomationService
from ..core.icons import IconCache
from ..core.jobs import SerialWorker
from ..core.poll_demand import PollDemandBoard
from ..core.prefs import Prefs
from ..core.store import Store
from . import theme, tooltip
from .alert_bar import AlertBar
from .chrome import DimOverlay, TitleBar, WindowFrame, exec_with_dim_click
from .home_tabs import HomeTabs
from .side_nav import SideNav
from .speaker_bar import SpeakerBar
from .tray import TrayController
from .wall import WallView
from .widgets import HLine, IconProvider, StatusChip

DRAIN_MS = 16
POLL_MS = 3000
#: 看不见的时候（收进托盘 / 最小化 / 开机直接进托盘）把轮询降到这个间隔。
#: 不加这一档，托盘里待一整天等于对着云端按 3 秒一次敲：1200 次/小时。
#: 15 秒是比较出来的折中——再降（30/60 秒）每小时只少几十次请求，但"从托盘点开"
#: 的第一眼更容易看到旧值（点开会立刻补一轮，可那一轮也得几百毫秒才回来）。
POLL_HIDDEN_MS = 15_000
#: 写入确认：写请求**回包成功**之后，等 mute 到期再多等一会儿，然后对这台设备
#: 定向读卡片属性，直到读到的值和写进去的一致。
#:
#: 为什么不能更早：写入是乐观的（按下开关的瞬间屏幕上就是新值），而云端快照在
#: 回包后还会滞后一段（«强» 与 «客厅» 实测：Mesh 灯 0.26 秒、Wi-Fi 灯中位 0.65 秒、
#: P90 2.5 秒、最慢 4.05 秒），并且 ``Client`` 自己还有 ``DEFAULT_MUTE`` 的静默期，
#: 静默期内该属性根本不会出现在读请求里。所以在 mute 到期前读是纯浪费。
CONFIRM_DELAY_MS = 200  # mute 到期后再等这么久，避开边界
CONFIRM_INTERVAL_MS = 500  # 之后每 0.5 秒读一次
CONFIRM_MAX_TRIES = 5  # 最多 5 次（mute 3s + 0.2s 起，覆盖到约 5.2 秒）
CONFIRM_MAX_DEVICES = 4  # 同时跟踪的设备数；超出丢弃最早的那台
#: 设备动作没有目标值、也没有 mute 条目：实测"回包→云端新值"P50 0.65 秒、
#: P90 2.5 秒，所以从 1.0 秒起读，5 次跑到 3.0 秒，能覆盖大部分动作。
CONFIRM_ACTION_DELAY_MS = 1000
#: 没有目标设备的写（场景、音箱指令）改动的设备说不准，只能按几个时点做**全量读**：
#: 回包后约 1 / 2.5 / 4 秒各一次（相对回包计时，不随读取耗时漂移）。
#: 判据：**不做早停**，三个时点固定跑满——签名覆盖全部设备，烤箱腔体温度、温湿度、
#: PM2.5、人体存在都在持续上报，"有变化"必然为真，早停会退化成 1 秒后就结束。
CONFIRM_FULL_AT_MS = (1000, 2500, 4000)
TOPOLOGY_MS = 5 * 60 * 1000  # 设备列表低频刷新：新增设备不用手动点刷新
#: 在线状态的刷新节奏。它挂在既有的轮询 tick 上（不另起定时器），但**不跟 3 秒**：
#: 设备清单是唯一能拿到上下线的接口，按家庭逐个请求，比一轮属性读贵得多。
#: 这里沿用现有的降频档（与托盘同一个 15 秒），掉线 / 回线最多晚 15 秒可见。
ONLINE_MS = POLL_HIDDEN_MS

#: 顶部那条横带的边距（左 / 上 / 右）。指令条按这三个值摆，警告条浮层也按它
#: 定位——两者叠在一起时轮廓必须重合。左右取 28 而不是 24：``WallView`` 的内容
#: 边距就是 28，指令条要和下面第一张卡片左对齐，整列才不会差半格。
ALERT_MARGIN_X = 28
ALERT_MARGIN_TOP = 12


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
    def __init__(
        self,
        palette: theme.Palette | None = None,
        *,
        tray_available: bool | None = None,
    ) -> None:
        super().__init__()
        self.palette_ref = palette or theme.LIGHT
        self.prefs = Prefs()
        self.setWindowTitle(f"米家控制台 v{__version__}")
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
        self.nav.zone_requested.connect(self._on_zone)
        body_row.addWidget(self.nav)

        # 电脑自动化的常驻服务：规则、冷静期冷却表、全局热键注册表都得活过
        # 对话框的销毁（exec_with_dim_click 关窗会 deleteLater），所以挂主窗口上。
        self.automation = AutomationService(
            store=self.store, client=self.client, worker=self.worker,
            window=self, parent=self,
        )
        # 动作执行失败要让人知道（用户 11-14：失败**不走** ④，改用系统通知）。
        # 去重键用规则 id：同一条规则失败二十次，用户要知道的是"这条在失败"，
        # 不需要二十条通知；每次失败的明细在日志窗里都有，不丢信息。
        self._auto_fail_at: dict[str, float] = {}
        self.automation.log_added.connect(self._on_automation_log)

        # 右侧：顶部指令条 + 卡片墙。指令条占的正是警告条浮层那条横带
        # （同一个 ALERT_MARGIN_X / ALERT_MARGIN_TOP + AlertBar.H），所以警告条
        # 一出现就正好压住它——这是定下的预期，不改浮层层级。
        right = QWidget(body)
        right_col = QVBoxLayout(right)
        right_col.setContentsMargins(0, 0, 0, 0)
        right_col.setSpacing(0)
        band = QWidget(right)
        band_row = QHBoxLayout(band)
        band_row.setContentsMargins(ALERT_MARGIN_X, ALERT_MARGIN_TOP, ALERT_MARGIN_X, 8)
        band_row.setSpacing(0)
        self.speaker_bar = SpeakerBar(self.palette_ref, self.icon_provider, band)
        self.speaker_bar.sent.connect(self._on_speaker_send)
        self.speaker_bar.target_changed.connect(self._on_speaker_target)
        self.speaker_bar.silent_changed.connect(self._on_speaker_silent)
        # 启动时把上次的静默状态灌进去。放在 connect 之后：set_silent 会发
        # silent_changed，回调里再回写一遍同样的值，set_speaker_silent 判等后不落盘
        self.speaker_bar.set_silent(self.prefs.speaker_silent)
        band_row.addWidget(self.speaker_bar)
        right_col.addWidget(band)

        self.wall = WallView(self.store, self.icon_provider, self.palette_ref)
        right_col.addWidget(self.wall, 1)
        body_row.addWidget(right, 1)
        col.addWidget(body, 1)

        # -- 顶部警告条：浮层，不参与布局 --------------------------------
        # 父控件是 body（侧栏 + 卡片墙的容器）。几何完全手动给，只占卡片自身
        # 矩形：出现/消失不引起任何重排，也不会盖住下方的滚轮与点击（那些坐标
        # 根本不在浮层矩形里）。显示时 raise 到兄弟控件最上层，压在滚动区与
        # 卡片之上；body 尺寸一变就跟着重新定位。
        self._alerts = AlertPolicy()
        self.alert_bar = AlertBar(self.palette_ref)
        self.alert_bar.dismissed.connect(self._on_alert_dismissed)
        self._alert_host = QWidget(body)
        host_row = QVBoxLayout(self._alert_host)
        host_row.setContentsMargins(0, 0, 0, 0)
        host_row.setSpacing(0)
        host_row.addWidget(self.alert_bar)
        self._alert_host.hide()
        self._body = body
        body.installEventFilter(self)

        self.store.on_changed = self._on_changed
        self.store.on_loaded = self._on_loaded
        self.store.on_status = self._set_status
        self.store.on_layout_changed = self._on_layout_changed
        self.store.on_tabs_changed = self._refresh_tabs
        # 每轮轮询结束后把健康快照交给界面，由 AlertPolicy 决定要不要提醒
        self.store.on_poll_health = self._on_poll_health

        self.wall.card_toggled.connect(self._on_toggle)
        self.wall.card_value_committed.connect(self._on_card_value)
        self.wall.card_style_requested.connect(self._on_card_style)
        self.wall.hide_requested.connect(self._on_hide_requested)
        self.wall.capability_refresh_requested.connect(self._on_capability_refresh)
        self.wall.device_reset_requested.connect(self._on_device_reset)
        self.wall.scene_hide_requested.connect(self.store.set_scene_hidden)
        self.search.textChanged.connect(self.wall.set_filter)
        self.wall.scene_run.connect(self._on_scene)
        self.wall.card_clicked.connect(self._on_card_clicked)
        self.btn_refresh.clicked.connect(self._reload)
        self.btn_theme.clicked.connect(self.toggle_theme)

        # 轮询频率不由这里直接拍：窗口只登记"我现在要 3 秒还是 15 秒"，
        # 由 ``poll_demand`` 取最严的那份（将来的挂件、自动化各自登记一条）。
        self.poll_demand = PollDemandBoard("属性轮询", idle_ms=POLL_MS)
        #: 事件轮询（``/miotspec/event/get``）的登记表。**现在还没有消费者**，
        #: 所以 ``idle_ms=None`` = 不轮询、不起请求。将来自动化要读事件时，
        #: 由它自己 ``self.poll_events.set("automation", 3000, "自动化规则")``，
        #: 接线照抄 ``_on_poll_interval`` 那三行即可，不用改这里的结构。
        self.poll_events = PollDemandBoard("事件轮询", idle_ms=None)

        self._drain_timer = QTimer(self)
        self._drain_timer.timeout.connect(self._drain)
        self._drain_timer.start(DRAIN_MS)

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll)
        self.poll_demand.subscribe(self._on_poll_interval)
        self.poll_demand.set("window", POLL_MS, "窗口可见")
        self._poll_timer.start(POLL_MS)

        # 写入确认：每台正在跟踪的设备一个单次定时器（最多 CONFIRM_MAX_DEVICES 台）。
        # 不用一条高频全局轮询去盯所有设备——那样代价是"每次点击都多打 6 个批量包"，
        # 而真正需要确认的只有刚写的那台。
        self._confirm_timers: dict[str, QTimer] = {}
        self._confirm_plans: dict[str, dict[str, Any]] = {}
        self.store.on_write_done = self._start_confirm
        # 场景 / 音箱指令：只留一条短序列（新指令重置它，不叠加）
        self._cmd_timer = QTimer(self)
        self._cmd_timer.setSingleShot(True)
        self._cmd_timer.timeout.connect(self._command_confirm_tick)
        self._cmd_start = 0.0
        self._cmd_step = 0
        self.store.on_command_done = self._start_command_confirm

        self._topo_timer = QTimer(self)
        self._topo_timer.timeout.connect(self._refresh_topology)
        self._topo_timer.start(TOPOLOGY_MS)

        #: 上一次发在线状态刷新请求的时刻（``time.monotonic``）。在线状态跟着
        #: 轮询 tick 走，但节流到 ``ONLINE_MS``；不单独起 QTimer。
        self._online_at = 0.0

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
        #: 窗口是否至少显示过一次。开机直接进托盘（从没 show 过）时不能把几何落盘，
        #: 否则退出时存下的是"没显示过的窗口"，把用户上次调好的大小覆盖掉
        self._ever_shown = False
        self._load_settings()
        self._apply_theme()

        # 托盘放在最后：它要写状态提示、要判"能不能用"，前面这些都得先就位
        self._quitting = False
        self._shutdown_done = False
        self._tray_hint_shown = False
        self.tray = TrayController(self.palette_ref, self, available=tray_available)
        self.tray.open_requested.connect(self.show_from_tray)
        self.tray.quit_requested.connect(self.quit_app)
        self.tray.install()
        self.tray.set_status(self.status.text())
        self._apply_poll_mode()

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
        self._ever_shown = True
        if self._pending_maximize:
            self._pending_maximize = False
            self._frame.set_maximized(True)
        self._apply_poll_mode()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._apply_poll_mode()

    def _save_settings(self) -> None:
        s = self._settings()
        # 从没显示过的窗口（开机直接进托盘）没有"用户调过的大小"，别拿默认值去盖
        if self._ever_shown:
            s.setValue("window/geometry", self.saveGeometry())
        s.setValue("ui/theme", "dark" if self.palette_ref is theme.DARK else "light")
        s.setValue("ui/active_home", self.store.active_home)
        s.sync()

    def _set_status(self, text: str) -> None:
        # 悬停提示走 tooltip.watch 注册的 provider，这里只要更新文字
        self.status.setText(text)
        tray = getattr(self, "tray", None)
        if tray is not None:
            tray.set_status(text)

    # -- 轮询异常提醒 ----------------------------------------------------
    def _place_alert(self) -> None:
        """把浮层摆到卡片墙顶部。只改自己的几何，不碰任何布局。"""
        body = getattr(self, "_body", None)
        host = getattr(self, "_alert_host", None)
        if body is None or host is None:
            return
        # 只盖卡片区，不压住左侧房间栏：左边从侧栏右缘算起，左右各留同样边距
        left = self.nav.width() + ALERT_MARGIN_X
        width = max(0, body.width() - left - ALERT_MARGIN_X)
        host.setGeometry(left, ALERT_MARGIN_TOP, width, AlertBar.H)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        # body 跟着窗口缩放时，浮层自己重新定位（它不在布局里，没人替它做）
        if obj is getattr(self, "_body", None) and event.type() == QEvent.Resize:
            self._place_alert()
        return super().eventFilter(obj, event)

    def _on_poll_health(self, snapshot: dict) -> None:
        """每轮轮询结束后的提醒决策。全部在主线程，不需要跨线程处理。"""
        update = self._alerts.observe(snapshot, time.monotonic())
        if update.hide:
            self._alert_host.hide()
        if update.bar is not None:
            self.alert_bar.set_alert(update.bar.level, update.bar.title, update.bar.detail)
            self._place_alert()
            self._alert_host.show()
            # 后加的孩子默认就在上面，但卡片墙会随房间切换重建，显式抬一次最稳
            self._alert_host.raise_()
        if update.toast is not None:
            self._notify(update.toast)

    def _notify(self, alert: Alert) -> None:
        """系统通知。弹不出来（没有托盘 / 系统关了通知）时什么都不发生，
        顶部那条横幅已经是兜底通道。"""
        tray = getattr(self, "tray", None)
        if tray is None:
            return
        tray.show_message(
            "米家控制台",
            f"{alert.title} · {alert.detail}",
            8000,
            warning=alert.level != "success",
        )

    def _on_automation_log(self, entry) -> None:
        """自动化日志里只挑 ``FAILED``：条件不成立有 ④ 兜着，失败是故障，要打扰用户。

        托盘不可用 / 系统关了通知时 ``_notify`` 内部静默跳过——日志窗是兜底通道。
        """
        if entry.outcome != FAILED:
            return
        now = time.monotonic()
        last = self._auto_fail_at.get(entry.rule_id)
        if last is not None and now - last < TOAST_COOLDOWN:
            return
        self._auto_fail_at[entry.rule_id] = now
        detail = entry.detail if len(entry.detail) <= 120 else entry.detail[:119] + "…"
        self._notify(Alert(ERROR, f"自动化「{entry.rule_name}」执行失败", detail,
                           entry.rule_id))

    def _on_alert_dismissed(self) -> None:
        # 关掉只是不再打扰，状态胶囊与日志照旧记录
        self._alerts.dismiss()
        self._alert_host.hide()

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            if self.isMinimized():
                # 最小化后画面完全不可见，暂停等于继续占着观看位
                self._release_camera_sessions("窗口最小化")
            # 最小化 / 还原都改轮询频率（还原那一支顺带立刻补一轮）
            self._apply_poll_mode()

    def _release_camera_sessions(self, reason: str) -> None:
        """最小化/退出：连独立窗口一起收掉，确保没有会话还占着观看位。"""
        from .camera_popout import close_all_popouts
        from .camera_view import playbacks

        close_all_popouts()
        self.wall.stop_playback(reason)
        playbacks().release(None, reason)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._should_hide_to_tray():
            # 必须 ignore：接受关闭事件之后窗口就真的被销毁了，托盘图标还在，
            # 点它只会崩（"窗口对象已删除"）。收进托盘是"藏起来"，不是"关掉"。
            event.ignore()
            self.hide_to_tray()
            return
        # 有托盘时 `quitOnLastWindowClosed` 是关掉的（见 `tray.install`，否则
        # `hide()` 会把整个程序带走），所以这里**接受关闭并不等于退出**：只
        # ``shutdown()`` + accept，窗口会被销毁而进程和托盘图标都还在，用户看到
        # 的就是"选了退出程序，结果还是缩到托盘"，再去点那个图标又因为窗口对象
        # 已删而没反应。要退出就必须显式走 ``quit_app``（它摘图标 + `app.quit()`）。
        self.quit_app()
        super().closeEvent(event)

    # -- 托盘 ------------------------------------------------------------
    def _should_hide_to_tray(self) -> bool:
        if self._quitting or not self.tray.active or not self.prefs.close_to_tray:
            return False
        app = QApplication.instance()
        # 注销 / 关机时 Qt 也会来要一次 close。这时候收进托盘会让关机卡在我们
        # 这一格上——Windows 会先等所有程序退出，再强杀。宁可干净地退。
        if app is not None and app.isSavingSession():
            return False
        return True

    def start_in_tray(self) -> bool:
        """开机自启时**不显示窗口**，直接待在托盘里。返回是否真的待进去了。

        进不去（这个环境没有托盘、或者用户把这一项关了）就返回 False，让
        ``app.present_window`` 退回"最小化到任务栏"。宁可多占一格任务栏，也不要
        程序在哪都看不见——Win11 默认把新图标塞进折叠区，不提示一句很容易被当成
        没启动，所以进来时弹一次气泡。
        """
        if not (self.tray.active and self.prefs.start_in_tray):
            return False
        # 已经用气泡交代过"在后台"，第一次点关闭时不必再弹一次
        self._tray_hint_shown = True
        self.tray.show_message(
            "米家控制台已在后台运行",
            "单击右下角的图标打开面板；右键菜单里可以退出。",
        )
        return True

    def hide_to_tray(self) -> None:
        """收进托盘：窗口藏起来，轮询降频（``hide()`` 的 hideEvent 里自己会调）。"""
        # 藏起来之后画面完全不可见，继续取流只是白占一个观看位（设备侧上限 3 路）
        self._release_camera_sessions("收进托盘")
        # 几何要在 hide() 之前存：隐藏后 saveGeometry 记的是隐藏时的状态
        self._save_settings()
        self.prefs.save()
        self.hide()
        if not self._tray_hint_shown:
            # 只提示一次：每次关窗都弹气泡是骚扰，而第一次不知道"程序去哪了"
            # 是真会让人以为关掉了
            self._tray_hint_shown = True
            self.tray.show_message(
                "米家控制台还在后台运行",
                "单击右下角的图标可以重新打开；右键菜单里可以退出。",
            )

    def show_from_tray(self) -> None:
        from ..core.single_instance import focus_window

        try:
            focus_window(self)
        except RuntimeError:  # 窗口已经销毁（正在退出）
            pass

    def quit_app(self) -> None:
        """真退出：托盘菜单里那一条，也是所有"确实要结束"的唯一入口。"""
        self._quitting = True
        self.shutdown()
        # 先摘图标再退：Windows 不会因为进程结束就清掉通知区域那一格，
        # 留一个空图标要等鼠标划过去才消失
        self.tray.remove()
        self.hide()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def shutdown(self) -> None:
        """收尾（幂等）：释放会话、落盘、停线程。"""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._release_camera_sessions("关闭窗口")
        self._save_settings()
        self.prefs.save()
        self.client.flush()
        # 把本次运行结束时的轮询健康状况落盘，下次启动能看出上次到底好不好
        self.client.health.close()
        # 先注销全局热键、丢弃未跑完的动作队列，再停后台线程
        self.automation.close()
        self.worker.stop()

    # -- 启动 ------------------------------------------------------------
    def start(self) -> None:
        # 热键不依赖网络：未登录也要能用（发事件时才需要中枢）。开机自启顺序
        # 不定，被占用的组合由服务层延迟重试一次。
        self.automation.start()
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
        """模态子窗口统一入口：弹出期间压暗主窗口，关掉就恢复。

        点压暗区关闭子窗口也在这里接（``exec_with_dim_click``）：只关当前这一层，
        行为等同 Esc。
        """
        # 子窗口一盖上来，墙上的画面就看不见了；停播而不是暂停，
        # 把观看位让给详情页（墙面和详情页共用同一个 CameraService）。
        self.wall.stop_playback("打开子窗口")
        self._cover_dim(True)
        try:
            exec_with_dim_click(dlg)
        finally:
            self._cover_dim(False)

    def _cover_dim(self, on: bool) -> None:
        """压暗层的开 / 关。

        关的时候多一道**冻结更新**：关一个子窗口这一下会连着产生三处"整窗失效"
        ——弹窗被销毁、它的点击层（``DimClickShield``，铺满主窗口）隐藏、压暗蒙版
        隐藏。每一处都让系统要求主窗口重画整片区域；而主窗口是
        ``WA_TranslucentBackground`` 的（圆角要用透明角），Qt 在这种窗口上是
        **逐个控件分段提交**的，于是能看到"父控件画完了、卡片 body 这些子控件还
        空着"的那一帧——用户 2026-11-12 报的关窗闪烁，截图里卡片全变矮、只剩标题。

        这三处失效都发生在同一轮调用里（弹窗 hide 在 ``done()`` 里、exec 的
        finally 紧接着跑，中间没有回到事件循环），所以在这里先把更新关掉，把积压
        的失效合成**一次** paint 再放开。以后往"关窗时要恢复点什么"里加东西（比如
        摄像头画面还回卡片），也写进这段冻结里，别在外面另起一段——那会变成第二次
        整窗重绘。
        """
        if on:
            self._dim.cover(True)
            return
        self.setUpdatesEnabled(False)
        try:
            self._dim.cover(False)
        finally:
            self.setUpdatesEnabled(True)
            self.update()

    # -- 定时器 ----------------------------------------------------------
    def _drain(self) -> None:
        self.worker.drain()

    def _poll(self) -> None:
        if not self.store.loading and self.store.devices:
            self.store.poll()
            # 在线状态只有设备清单能给（属性轮询对离线设备照样回 code=0），
            # 挂在这个 tick 上、按 ONLINE_MS 节流，不另起定时器。
            now = time.monotonic()
            if now - self._online_at >= ONLINE_MS / 1000:
                self._online_at = now
                self.store.refresh_online()

    def _apply_poll_mode(self) -> None:
        """按"看不看得见"登记属性轮询的需求，回到前台时立刻补一轮。

        判据是 ``isVisible() and not isMinimized()``：收进托盘（``hide()``）和
        最小化到任务栏都是"看不见"，开机直接进托盘（从没 show 过）同样走降频档。
        降频只动定时器的间隔，请求闸门与失败退避那套逻辑一行没改。

        这里**只管登记**，不碰定时器：定时器由 :meth:`_on_poll_interval` 按
        "所有需求里最严的那条"来设。将来挂件 / 自动化各报一条，就不会互相覆盖。
        """
        if getattr(self, "poll_demand", None) is None:  # 构造期间
            return
        active = self.isVisible() and not self.isMinimized()
        self.poll_demand.set(
            "window",
            POLL_MS if active else POLL_HIDDEN_MS,
            "窗口可见" if active else "窗口在托盘 / 最小化",
        )
        if active:
            self._poll()

    def _start_confirm(
        self,
        did: str,
        expected: dict[tuple[int, int], Any],
        mute: float | None,
    ) -> None:
        """写请求回包成功：安排这台设备的定向回读。

        同一台设备连续写只保留一个确认序列、重新计时，期望值按 key 合并（后写的
        覆盖先写的）；跟踪的设备超过 ``CONFIRM_MAX_DEVICES`` 时丢弃最早的那台。
        ``mute is None`` 表示这是设备动作（没有静默期，也没有目标值）。
        """
        plan = self._confirm_plans.get(did)
        if plan is None:
            while len(self._confirm_plans) >= CONFIRM_MAX_DEVICES:
                self._drop_confirm(next(iter(self._confirm_plans)))
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda d=did: self._confirm_tick(d))
            self._confirm_timers[did] = timer
            plan = {"expected": {}}
            self._confirm_plans[did] = plan
        plan["expected"].update(expected)
        plan["left"] = CONFIRM_MAX_TRIES
        delay = (
            CONFIRM_ACTION_DELAY_MS
            if mute is None
            else int(mute * 1000) + CONFIRM_DELAY_MS
        )
        self._confirm_timers[did].start(delay)

    def _confirm_tick(self, did: str) -> None:
        """到点：读一次这台设备的卡片属性；**最后一次改成全量读**。"""
        plan = self._confirm_plans.get(did)
        if plan is None:
            return
        if plan["left"] <= 1:
            # 最后一轮顺手覆盖联动改到的其它设备（开关 → 场景 / 自动化）
            self.store.confirm_full(
                lambda res, err: self._on_confirm_tail(did, res, err)
            )
            return
        refs = self.store.card_refs(did)
        if not refs:  # 离线 / 卡片没属性：没什么可确认的，直接收工
            self._drop_confirm(did)
            return
        self.store.confirm_read(
            did, refs, lambda res, err: self._on_confirm_read(did, res, err)
        )

    def _on_confirm_tail(
        self,
        did: str,
        res: dict[Any, Any] | None,
        err: BaseException | None,
    ) -> None:
        """最后一轮（全量读）的结果：无论是否命中，这个序列都到此为止。"""
        plan = self._confirm_plans.get(did)
        if plan is None:
            return
        if err is not None:
            print(f"[confirm] {did} 全量确认失败：{err}")
        self._drop_confirm(did)

    def _on_confirm_read(
        self,
        did: str,
        res: dict[Any, Any] | None,
        err: BaseException | None,
    ) -> None:
        plan = self._confirm_plans.get(did)
        if plan is None:
            return
        if res is None:
            # 确认读失败不进退避账本，只留一行日志，下一次照常重试
            print(f"[confirm] {did} 回读失败：{err}")
        else:
            expected = plan["expected"]
            got = {
                (ref.siid, ref.piid): rv.value
                for ref, rv in res.items()
                if getattr(rv, "ok", False)
            }
            if expected and all(
                key in got and got[key] == value for key, value in expected.items()
            ):
                self._drop_confirm(did)
                return
        plan["left"] -= 1
        if plan["left"] <= 0:
            self._drop_confirm(did)
            return
        self._confirm_timers[did].start(CONFIRM_INTERVAL_MS)

    def _drop_confirm(self, did: str) -> None:
        self._confirm_plans.pop(did, None)
        timer = self._confirm_timers.pop(did, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()

    def _start_command_confirm(self) -> None:
        """场景 / 音箱指令回包：起一条 1 / 2.5 / 4 秒的全量读序列。

        新指令直接重置这一条（不叠加）。它只是一串一次性 job：**不碰
        ``_poll_timer``**——不改间隔、不重置相位、也不取消它，所以 3 秒轮询照旧。
        """
        self._cmd_start = time.monotonic()
        self._cmd_step = 0
        self._cmd_timer.start(CONFIRM_FULL_AT_MS[0])

    def _command_confirm_tick(self) -> None:
        self._cmd_timer.stop()  # 单次定时器：到点先停，后面按需要重新起
        self.store.confirm_full(self._on_command_confirm_read)

    def _on_command_confirm_read(
        self,
        res: dict[Any, Any] | None,
        err: BaseException | None,
    ) -> None:
        if err is not None:
            print(f"[confirm] 全量确认失败：{err}")
        # **不做早停**：签名覆盖全部设备，烤箱腔体温度、温湿度、PM2.5、人体存在
        # 都在持续上报，用它判"有变化"必然为真，相当于 1 秒后就结束。1 / 2.5 / 4 秒
        # 三点固定跑满。
        self._cmd_step += 1
        if self._cmd_step >= len(CONFIRM_FULL_AT_MS):
            return
        elapsed = (time.monotonic() - self._cmd_start) * 1000
        delay = max(150, int(CONFIRM_FULL_AT_MS[self._cmd_step] - elapsed))
        self._cmd_timer.start(delay)

    def _on_poll_interval(self, ms: int | None) -> None:
        """需求表算出新间隔时调这里。``None`` = 没人要，停表。"""
        timer = getattr(self, "_poll_timer", None)
        if timer is None:
            return
        if ms is None:
            timer.stop()
            return
        # ``setInterval`` 会把正在跑的定时器重新计时——正是想要的：切到后台时从
        # 头数 15 秒，切回前台时立刻归零，配一次 ``poll`` 所以第一眼不会看到旧值。
        # 值没变时需求表不会回调（见 ``PollDemandBoard._refresh``），所以不会互相重置。
        if timer.interval() != ms:
            timer.setInterval(ms)
        if not timer.isActive():
            timer.start()

    def _refresh_topology(self) -> None:
        if not self.store.loading and self.store.devices:
            self.store.load()

    # -- 事件 ------------------------------------------------------------
    def _on_room_selected(self, home: str, room: str) -> None:
        self.wall.scroll_to_room(home, room)

    def _on_home_selected(self, home_id: str) -> None:
        if home_id == self.store.active_home:
            return
        self.store.active_home = home_id
        self.store.scenes = self.store.scenes_for(home_id)
        self.wall.set_home(home_id)
        self.nav.build(self.store, home_id)
        self._settings().setValue("ui/active_home", home_id)
        self._update_header()
        self._refresh_speakers()
        self.store.poll()

    def _refresh_tabs(self) -> None:
        """把家庭列表 + 排列方式重新灌给顶部标签栏（只影响显示，不动设定顺序）。"""
        homes = self.store.homes()
        active = self.store.active_home or (homes[0].id if homes else "")
        self.tabs.configure(self.store.home_placement, self.store.home_active_style)
        # 胶囊档的标签自带 14px 左内边距，从顶栏左边距里扣掉，标签文字才仍与
        # 下方卡片墙左对齐；切回另外两档时 inset 归零，边距自动还原。
        self._head.setContentsMargins(24 - self.tabs.content_inset, 0, 18, 0)
        self.tabs.set_homes([(h.id, h.display_name) for h in homes], active)

    def _on_changed(self, dids: set[str]) -> None:
        self.wall.update_cards(dids)
        self._update_header()
        self._refresh_speakers()

    def _on_layout_changed(self) -> None:
        """排序或隐藏设备变了：墙和导航都要重建（顺序变了无法增量更新）。"""
        active = self.store.active_home
        self.wall.set_home(active)
        self.nav.build(self.store, active)
        self._refresh_tabs()
        self._update_header()

    def _update_header(self) -> None:
        ids = self.store.home_ids()
        active = self.store.active_home or (ids[0] if ids else "")
        devices = [
            d for d in self.store.devices.values()
            if not d.node.home_id or d.node.home_id == active
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
        ids = [h.id for h in homes]
        if self.store.active_home not in ids:
            self.store.active_home = ids[0] if ids else ""
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
        self._refresh_speakers()
        self.store.poll()

    # -- 顶栏音箱指令条 --------------------------------------------------
    def _refresh_speakers(self) -> None:
        """把"哪些设备能执行文本指令"重新灌给指令条。

        每轮轮询后都会走一次（``_on_changed``）：在线状态一变，候选列表的置灰与
        输入框的可用性就要跟着变。构造都是轻量 dataclass，三条五条不构成开销。
        """
        bar = getattr(self, "speaker_bar", None)
        if bar is None:  # 构造期间（store 回调早于指令条）
            return
        # 只取**当前家庭**的设备：指令条跟着这一屏的卡片墙走，跨家庭把别处的音箱列进来
        # 会让人找不到是哪台。筛选与自动化编辑页共用 ``speaker.collect_home``。
        bar.set_targets(
            speaker.collect_home(
                self.store.devices.values(),
                self.store.home_label(self.store.active_home),
            ),
            self.prefs.speaker_command_did,
        )

    def _on_speaker_target(self, did: str) -> None:
        self.prefs.set_speaker_command_did(did)

    def _on_speaker_silent(self, on: bool) -> None:
        self.prefs.set_speaker_silent(on)

    def _on_speaker_send(self, text: str) -> None:
        target = self.speaker_bar.target()
        if target is None or not target.online:
            return
        silent = self.speaker_bar.silent()
        self.store.run_action(
            target.did,
            target.siid,
            target.aiid,
            speaker.directive(target, text, silent),
            on_done=lambda code, reason: self._on_speaker_done(
                target, text, code, reason, silent
            ),
            untargeted=True,
        )

    def _on_speaker_done(
        self, target: speaker.Target, text: str, code: int, reason: str, silent: bool = False
    ) -> None:
        short = text if len(text) <= 18 else text[:18] + "…"
        if code == 0:
            # 静默时补一句：音箱此刻一声不出，不写清楚就像没发出去
            tail = "（静默）" if silent else ""
            self.speaker_bar.notice(f"{target.name} 已收到{tail}：{short}")
        else:
            # reason 由 store 备好（云端码已翻成人话，异常则带原文）
            self.speaker_bar.notice(f"发送失败：{reason or explain(code)}")

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

    def _device_name(self, did: str) -> str:
        st = self.store.devices.get(did)
        return st.name if st is not None else did

    def _on_capability_refresh(self, did: str) -> None:
        """右键「强制刷新」：清掉这台设备的「已确认不支持」记录，并立刻重查。

        不是刷新读数（读数本来就在 3 秒轮询里），也不改任何本地偏好，
        所以不弹二次确认；结果用顶部指令条那条提示说清楚。
        """
        from .device_reset import refresh_result_text

        def done(removed: int, read: int, error: str) -> None:
            self.speaker_bar.notice(
                refresh_result_text(self._device_name(did), removed, read, error)
            )

        self.store.refresh_capabilities(did, done)

    def _on_device_reset(self, did: str) -> None:
        """右键「重置设备配置」：确认后清掉样式 / 隐藏 / 排序 / 能力缓存。"""
        from .confirm import confirm
        from .device_reset import device_reset_text

        name = self._device_name(did)
        counts = self.store.device_config_counts(did)
        self._cover_dim(True)
        try:
            ok = confirm(
                self,
                "重置设备配置",
                device_reset_text(name, counts),
                self.palette_ref,
                ok_text="重置",
                danger=True,
            )
        finally:
            self._cover_dim(False)
        if not ok:
            return
        self.store.reset_device_config(did)
        self.speaker_bar.notice(f"「{name}」的本地配置已重置，卡片已回到默认样式")

    def _on_scene(self, scene_id: str) -> None:
        self.store.run_scene(scene_id, self.store.active_home)

    def _focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    def _clear_search(self) -> None:
        """``Esc``：先照顾正在输入的那一格。

        光标在指令条里时，按 Esc 却清掉顶栏搜索框是莫名其妙的——按"离光标最近的
        输入框"来排序：指令条有字就清字，没字就交出焦点，然后才轮到搜索框。
        """
        bar = getattr(self, "speaker_bar", None)
        if bar is not None and bar.input.hasFocus():
            if bar.input.text():
                bar.input.clear()
            else:
                bar.input.clearFocus()
            return
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
        # 墙上正在播这一路：把 player/sink 直接接到详情页，不重新 open。
        # 接不过去（没在播 / 播的是别的设备 / 目标控件不可用）才退回原路。
        if not self.wall.hand_off_active_to(dlg.camera_view, st.did):
            self.wall.stop_playback("打开详情")
        # 关页面时反过来：还看得见就收回去继续播（同一个播放器），不再重新取流
        dlg.about_to_stop.connect(self.wall.adopt_back)
        dlg.adopt_playing_camera()
        self._run_dialog(dlg)

    def _on_icon_ready(self, model: str) -> None:
        for card in self.wall._cards.values():  # noqa: SLF001
            if card.state.node.model == model:
                card.update()
        # 指令条左侧那台音箱也在等图（产品图是异步抓的）
        bar = getattr(self, "speaker_bar", None)
        if bar is not None and bar.target() is not None and bar.target().model == model:
            bar.update_icon()

    # -- 功能区 / 设置 ---------------------------------------------------
    def _on_zone(self, key: str) -> None:
        """功能区入口。目前只有「电脑自动化」有页面；其余两个仍是占位，忽略即可。"""
        if key == "automation":
            self.open_automation()

    def open_automation(self) -> None:
        """先弹规则列表；三段式编辑窗由列表窗自己再叠一层（嵌套模态）。"""
        from .automation_list import AutomationListDialog

        dlg = AutomationListDialog(self.automation, self.palette_ref, self)
        self._run_dialog(dlg)
        # **不要补 _apply_theme()**：那会整墙重建，正是 11-12 关窗闪烁的根因。
        # 规则保存 / 热键重注册都在常驻服务里完成，不需要主窗口跟着刷新。

    def open_settings(self) -> None:
        from .settings import SettingsDialog

        dlg = SettingsDialog(self.store, self.palette_ref, self,
                             account=self.client.account_info(),
                             tray_available=self.tray.active)
        dlg.theme_selected.connect(self.set_theme)
        # 账号动作（退出登录 / 扫码登录）不能在设置窗口还开着的时候做：
        # 登录窗口是另一个模态窗口，套在设置里面会把模态栈搞乱。
        action = {"kind": ""}
        dlg.account_action.connect(lambda kind: action.update(kind=kind))
        self._run_dialog(dlg)
        # **不要再补一次 ``_apply_theme()``**（2026-11-12 删）。设置页里点主题时
        # ``theme_selected`` → ``set_theme`` 已经就地应用过了，这里再来一遍等于
        # 整墙重建第二次：``_apply_theme`` 里有 ``wall.rebuild_palette``，那会把
        # 56 张卡拆掉重造（它自己的注释：卡片是自绘的，换色靠整墙重建）。而
        # 什么都没改直接关窗时，这次重建就是纯浪费——用户看到的主窗口闪烁就是它。
        self._refresh_tabs()
        if action["kind"] == "logout":
            self._logout()
        elif action["kind"] == "login":
            self._show_login()

    def _logout(self) -> None:
        from .confirm import confirm, notice

        path = self.client.auth_path
        self._cover_dim(True)
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
            self._cover_dim(False)
        self.store.clear()
        # 设备列表空了，指令条也别再挂着一台已经不认识的音箱
        self.speaker_bar.set_targets([])
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
        self.alert_bar.set_palette(pal)
        self.speaker_bar.set_palette(pal)
        tooltip.set_palette(pal)
        tray = getattr(self, "tray", None)
        if tray is not None:
            tray.set_palette(pal)
        # 搜索框压在**窗口底**上（不是卡片），所以底色从 pal.bg 起算：原来用
        # bg_soft + pal.border，深色下 #15171C 对 #0E0F12 只有 1.07:1、描边
        # 1.22:1，整个胶囊几乎看不见（用户 11-13：暗色下输入框的描边没了）。
        self.search.setStyleSheet(
            theme.field_qss(pal, radius=16, padding="0 14px", font_size=13,
                            base=pal.bg)
        )


__all__ = ["MainWindow"]
