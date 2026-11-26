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
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..core import applog, climate as climate_mod, scene_shortcut, speaker
from ..core.alerts import ERROR, TOAST_COOLDOWN, Alert, AlertPolicy
from ..core.api import Client, explain
from ..core.automation import FAILED
from ..core.automation.service import AutomationService
from ..core.icons import IconCache
from ..core.jobs import SerialWorker
from ..core.poll_demand import PollDemandBoard
from ..core.prefs import Prefs
from ..core.single_instance import focus_window
from ..core.store import Store
from . import app_icon, chrome, theme, tooltip
from .alert_bar import ALERT_MARGIN_TOP, ALERT_MARGIN_X, AlertBar
from .automation_list import AutomationPage
from .chrome import DimOverlay, TitleBar, WindowFrame, exec_with_dim_click
from .consumables_page import ConsumablesPage
from .desktop_widget import DesktopWidget
from .home_tabs import HomeTabs
from .messages_popup import MessagePopup
from .monitor_page import MonitorPage
from .side_nav import SideNav
from .speaker_bar import SpeakerBar
from .tray import TrayController
from .wall import WallView
from .widgets import ChipButton, ClimateStrip, HLine, IconProvider, StatusChip

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
#: 回包后还会滞后一段（«示例房间 A» 与 «示例房间 B» 实测：Mesh 灯 0.26 秒、Wi-Fi 灯中位 0.65 秒、
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
#: 桌面快捷方式一次点击会连发 3 次全局广播（防"对面还没装消息过滤器"时丢失），
#: 主实例每次都会去 pending 目录看一眼：第一眼取到条目并执行，后两眼是空的。
#: 这个宽限期把这些空眼归到同一次点击，**不要**顺手把窗口抢到前台。
SCENE_DRAIN_GRACE = 3.0


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


class HeaderBand(QWidget):
    """顶栏底板：和标题栏同色的半透明带（渐变档专用）。

    标题栏（``chrome.TitleBar``）本来就是"背景色往卡片色混 62%、再跟着 ``card_alpha``
    一起透"，渐变档下它是一条很淡的白带。顶栏（家庭标签 / 状态胶囊 / 搜索框 / 两个
    圆形按钮）原来是整块透明，只在下面压一条 1px 描边——那条线是从**背景**推出来的
    **单一颜色**，压在会变色的渐变上当然突兀（用户 2026-11-17）。

    现在顶栏铺同一支色，和标题栏连成一条完整的窗框带，与内容区的分界交给色差，
    不再靠线。纯色档（含黑白）不铺：那里一条 1px 线本来就是对的，也保住了
    "黑白档逐像素没变"这条底线。
    """

    #: 底带的不透明度。渐变档的 ``bar_bg`` 是"壁纸压深一档"，和标题栏铺的是同一支色，
    #: 所以这里也跟 ``card_alpha`` 走——两者一起才连成一条完整的窗框带。
    def __init__(self, palette: theme.Palette) -> None:
        super().__init__()
        self._p = palette

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        pal = self._p
        if not pal.surface.is_gradient():
            return
        p = QPainter(self)
        p.fillRect(self.rect(), theme.alpha(chrome.bar_bg(pal), pal.card_alpha))
        p.end()


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
            fill = theme.blend(pal.bg_soft, pal.text, 0.10)
        elif self._hover:
            fill = theme.blend(pal.bg_soft, pal.text, 0.05)
        else:
            fill = pal.bg_soft
        # 和卡片、指令条、搜索框同一支"卡面"：渐变档下跟着 card_alpha 一起透
        p.setBrush(theme.card_fill(fill, pal))
        p.setPen(Qt.NoPen)
        p.drawEllipse(r)

        c = r.center()
        if self._glyph == "refresh":
            # 方头箭头画法，为什么不用三角见 _refresh_path 的 docstring
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(pal.text_soft))
            p.drawPath(_refresh_path(QPointF(c), 9.6))
        elif self._glyph == "theme":
            # 月亮：整圆减去偏上右的一圆。**不能再用"再画一个背景色的圆"**——
            # 按钮底现在是半透明的，拿不透明的背景色去抠洞会亮出一块实心斑。
            moon = QPainterPath()
            moon.addEllipse(QPointF(c), 7.0, 7.0)
            bite = QPainterPath()
            bite.addEllipse(QPointF(c.x() + 5.0, c.y() - 4.0), 6.0, 6.0)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(pal.text_soft))
            p.drawPath(moon.subtracted(bite))
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
        #: 当前背景预设 id；明暗模式由 ``palette_ref.name`` 表示。两者一起决定
        #: 调色板，持久化也只存这两个值（见 ``_load_settings`` / ``_save_settings``）。
        self._preset = self.palette_ref.preset
        self.prefs = Prefs()
        self.setWindowTitle(f"米家控制台 v{__version__}")
        self.resize(1200, 800)
        self.setMinimumSize(920, 600)

        self._frame = WindowFrame(
            self,
            resizable=True,
            fill=lambda: self.palette_ref.bg,
            # 主窗口没有浮窗留白（能最大化），阴影无处可画；靠一条 1px 描边
            # 把面板和桌面分开，颜色从当前背景推，换预设自动跟随。
            border=lambda: theme.window_edge(self.palette_ref),
            backdrop=lambda: self.palette_ref.surface,
        )

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

        header = HeaderBand(self.palette_ref)
        self._header = header
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
        # 落在顶栏右侧时比长句子安静。左边挂一枚房子图标（和温湿度那两颗一致）：
        # 原来那个绿点在这套界面里表示"开着 / 在线"，压在一句"6 台开启"前面
        # 容易被读成"这一栏在线"，而它其实说的是"这是设备"。
        self.status = StatusChip("正在连接…", self.palette_ref, icon="house")
        tooltip.watch(self.status, self.status.text)
        head.addWidget(self.status)
        # 全屋温湿度紧跟着设备计数（同一个范围 = 当前家庭）。没数据时整块隐藏，
        # 连左边那 8px 间距一起消失——间距做在容器内部，不在顶栏上加 addSpacing。
        self.climate = ClimateStrip(self.palette_ref)
        head.addWidget(self.climate)
        head.addStretch(1)

        # 消息入口：原来这里是搜索框（2026-11-23 撤掉——几十台设备用不上搜索，
        # Ctrl+F 一并不要，位置让给消息）。**悬停浮出**列表，点击只是"立刻浮出"
        # 一次（外接鼠标坏了 / 触屏时还能用），实现在 ``ui/messages_popup.py``。
        self.btn_messages = ChipButton("消息", self.palette_ref, icon="bell")
        self.btn_messages.clicked.connect(self._show_messages)
        head.addWidget(self.btn_messages)
        head.addSpacing(12)

        self.btn_theme = RoundButton("theme", self.palette_ref)
        self.btn_refresh = RoundButton("refresh", self.palette_ref)
        head.addWidget(self.btn_theme)
        head.addSpacing(8)
        head.addWidget(self.btn_refresh)
        col.addWidget(header)

        # 浮层挂在主窗口下面（生命周期跟着窗口走），但它是顶层 ``Qt.Tool``，
        # 不会被内容区裁掉。``attach`` 之后 Enter / Leave 全由它自己接管。
        self.messages = MessagePopup(self.store, self.palette_ref, self)
        self.messages.attach(self.btn_messages)
        # 顶栏和内容之间的那条 1px 线只在纯色档保留：渐变档顶栏自己铺了一层色带
        # （见 HeaderBand），再叠一条"从背景推的单一颜色"就会在变色背景上突兀。
        self._header_line = HLine(self.palette_ref.border)
        col.addWidget(self._header_line)

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

        # 桌面快捷方式：上次消费待执行条目的时刻（把一次点击的 3 次广播归成一批），
        # 以及"启动补捞做过没有"（广播在启动窗口期会丢，加载完要补一次）。
        self._last_scene_drain = 0.0
        self._scene_boot_drained = False

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
        #: 说话条所在的那条横带。功能区页面会整条藏起来（不是只藏输入框），
        #: 页面因此能顶到窗口上沿；回卡片墙时再显示。
        self._band = band

        # 中间内容区是页面宿主：默认卡片墙，功能区入口整页替换它。
        # 宿主只包住卡片墙这一层——说话条在它**外面**（上面），所以切页不会
        # 挪动说话条，也不会改变顶部警告条浮层的定位（那条按 body / nav 的
        # 几何算，和当前是哪一页无关）。
        self.pages = QStackedWidget(right)
        self.pages.setObjectName("PageHost")
        self.wall = WallView(self.store, self.icon_provider, self.palette_ref)
        self.automation_page = AutomationPage(
            self.automation, self.palette_ref, self.pages,
            # 页面不是模态：编辑 / 回收站 / 日志仍由主窗口统一压暗
            run_dialog=self._run_dialog,
        )
        self.consumables_page = ConsumablesPage(
            self.store, self.palette_ref, run_dialog=self._run_dialog, parent=self.pages
        )
        # 监控页：多路摄像头同屏。它不是"把卡片墙放大"——左边选源、右边布局，
        # 播放调度也独立（见 ui/monitor_page.py 顶部）。
        self.monitor_page = MonitorPage(self.store, self.palette_ref, self.pages)
        self.pages.addWidget(self.wall)
        self.pages.addWidget(self.automation_page)
        self.pages.addWidget(self.consumables_page)
        self.pages.addWidget(self.monitor_page)
        #: 功能区 key → 页面索引。索引跟着 addWidget 顺序，加页时一起改。
        self._zone_pages = {"automation": 1, "consumables": 2, "monitor": 3}
        # 提示浮层按右栏内容区定位：垂直贴其顶部 +12（与警告条同一条线），水平按
        # 其宽度居中（左右各 28）。右栏顶部不随说话条显隐变化，切页时提示不会漂。
        self.speaker_bar.set_toast_content(right)
        right_col.addWidget(self.pages, 1)
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
        self.wall.scene_run.connect(self._on_scene)
        self.wall.scene_shortcut_requested.connect(self._on_scene_shortcut)
        self.wall.card_clicked.connect(self._on_card_clicked)
        # 耗材页算出两档提醒的条数，由这里转给侧栏圆点：页面不认识 SideNav，
        # 也不需要知道圆点画在哪
        self.consumables_page.counts_changed.connect(self._on_consumable_counts)
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
            ("F5", self._reload),
            ("Esc", self._on_escape),
            ("Ctrl+R", self._reload),
            ("Ctrl+,", self.open_settings),
        ):
            sc = QShortcut(QKeySequence(seq), self)
            sc.activated.connect(slot)
            self._shortcuts.append(sc)

        self._known_dids: set[str] = set()
        self._pending_maximize = False
        #: 启动落点的"房间"要等拓扑加载完才能算，只在第一次 ``_on_loaded`` 消费一次
        self._start_placement_pending = True
        #: 待滚到的 (home, room)；从托盘启动时窗口还没显示，等首次显示后再滚一次
        self._pending_start_room: tuple[str, str] | None = None
        #: 窗口是否至少显示过一次。开机直接进托盘（从没 show 过）时不能把几何落盘，
        #: 否则退出时存下的是"没显示过的窗口"，把用户上次调好的大小覆盖掉
        self._ever_shown = False
        self._load_settings()
        self._restore_start_page()
        # 桌面挂件：这里只建对象不上屏。``widget_autostart`` 为真才在事件循环
        # 起来之后显示——普通档不用 SetParent，show 即可见（默认档已改成 normal）。
        # 放在 ``_apply_theme`` 之前建，换主题那一步就顺手给它上色了。
        #
        # **parent 必须是 None**（2026-11-20 实测）。带 ``Qt.Tool`` 的**子**控件
        # 一旦建出原生窗口，Qt 会把主窗口里的兄弟控件（``_FrameBorder``、
        # ``_ResizeEdge``、central widget）也全改成原生窗口；铺满整窗的描边层
        # 于是吃掉所有鼠标事件，主界面表现成"点哪都没反应"。挂件本来就是独立
        # 顶层窗口，不挂 parent 不影响它跟着主窗口一起 show/hide（见 shutdown）。
        self.widget_panel = DesktopWidget(
            self.store, self.prefs, self.palette_ref, self.poll_demand, None
        )
        self._wire_desktop_widget()
        if self.prefs.widget_autostart:
            QTimer.singleShot(0, self._show_widget_at_start)
        self._apply_theme()

        # 托盘放在最后：它要写状态提示、要判"能不能用"，前面这些都得先就位
        self._quitting = False
        self._shutdown_done = False
        self._tray_hint_shown = False
        self.tray = TrayController(self.palette_ref, self, available=tray_available)
        self.tray.open_requested.connect(self.show_from_tray)
        self.tray.quit_requested.connect(self.quit_app)
        self.tray.widget_toggle_requested.connect(self._on_tray_widget_toggle)
        self.tray.install()
        # 勾选态从挂件的实际显隐来（启动时多半还没显示，autostart 那次 show 会
        # 经 visibility_changed 再把它点亮）
        self.tray.set_widget_checked(self.widget_panel.isVisible())
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
        preset = self._read_preset(s)
        mode = "dark" if s.value("ui/mode", "light") == "dark" else "light"
        self._preset = preset
        self.palette_ref = theme.palette_for(preset, mode)
        self.store.active_home = str(s.value("ui/active_home", "") or "")

    @staticmethod
    def _read_preset(s: QSettings) -> str:
        """只认 ``theme.PRESET_IDS`` 里的值；缺配置 / 旧配置 / 乱写一律退黑白。"""
        value = str(s.value("ui/background/preset", "mono") or "mono")
        return value if value in theme.PRESET_IDS else "mono"

    def _restore_start_page(self) -> None:
        """按偏好决定启动落点；必须在页面宿主与侧栏都建好之后调用。

        开关关掉时固定落在卡片墙，但 **不清空** ``prefs.last_page``：记录一直随
        切页更新，用户下次打开开关就又能回到上次的页面。

        房间落点不能在这里做：房间 id 要等拓扑加载完才能校验，统一在第一次
        ``_on_loaded`` 里处理（见 :meth:`_apply_start_room`）。
        """
        page = self.prefs.last_page
        if not self.prefs.restore_last_page or page not in self._zone_pages:
            return
        self._show_page(self._zone_pages[page], page)

    def _start_room(self, home: str) -> str | None:
        """启动时该高亮哪个房间；``None`` 表示"全部设备"。

        只有恢复开关打开、且记录的家庭就是当前家庭时才用 ``last_room``；
        其余情况（开关关掉、家庭已不存在、房间被删 / id 对不上）一律回该家庭
        第一个普通房间。没有普通房间时退回"全部设备"，不报错也不卡住。

        用 ``rooms()`` 而不是 ``room_entries()`` 是有意的：这里的候选是"墙上
        真能滚过去的分区"。设备全被隐藏的房间在墙上没有分区（隐藏项集中在
        末尾那组），落进去只会看到一片空白，所以它不参与启动落点——**这不
        等于房间不存在**，房间仍在 ``room_entries()`` 里，设置页照样能管理。
        """
        rooms = [v.room for v in self.store.rooms(home) if v.kind == "room"]
        if (self.prefs.restore_last_page and self.prefs.last_home == home):
            want = self.prefs.last_room
            if want == "*":
                return None
            if want and want in rooms:
                return want
        return rooms[0] if rooms else None

    def _apply_start_room(self, home: str) -> None:
        """把启动落点同步到侧栏高亮与卡片墙滚动位置。"""
        room = self._start_room(home)
        if room:
            self.nav.set_active(home, room)
            self._pending_start_room = (home, room)
        else:
            # 没有普通房间 / 上次停在全部设备：高亮"全部设备"并回到墙顶
            self.nav.set_active("", "*")
            self._pending_start_room = (home, "*")
        self._scroll_pending_start_room()
        # 布局要到下一轮事件循环才定下来，稍后再校一次滚动位置
        QTimer.singleShot(0, self._scroll_pending_start_room)

    def _scroll_pending_start_room(self) -> None:
        """滚动到启动房间。窗口还没显示时不消费，等首次显示后由 showEvent 再调。"""
        if self._pending_start_room is None:
            return
        home, room = self._pending_start_room
        self.wall.scroll_to_room(home, room)
        if self.isVisible():
            self._pending_start_room = None

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._ever_shown = True
        if self._pending_start_room is not None:
            # 从托盘启动时数据先到、窗口还没显示；显示后补一次启动房间的滚动
            QTimer.singleShot(0, self._scroll_pending_start_room)
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
        # 外观只存两个值：背景预设 id + 明暗模式。旧键（ui/theme 等）不再读取，
        # 也不会被写回；读不到就是黑白 + 浅色。
        s.setValue("ui/background/preset", self._preset)
        s.setValue("ui/mode", "dark" if self.palette_ref.is_dark else "light")
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
        if self._should_hide_to_widget():
            # 托盘挂不上、但挂件还在屏幕上：这是"最小化"唯一还剩的落点。
            # 走 quit_app 的话用户看到的是"面板一关，挂件也一起没了"（2026-11-21）。
            event.ignore()
            self.hide()
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

    def _should_hide_to_widget(self) -> bool:
        """托盘不可用时，「最小化」的退路换成挂件。

        用户选的是"关闭面板时最小化"（``close_to_tray``），但托盘那一格没了
        （见 ``tray.log`` 的 NIM_ADD 失败）。这时直接 ``quit_app`` 会连屏幕上
        的挂件一起带走——用户的原话是"似乎可以理解为关闭了程序"。进程还有
        可见的存在，就不该无声退出。

        挂件头部第二个按钮是「打开主窗口」，能叫回来；挂件右键菜单里也有
        「退出程序」，所以这不是"退不出去"的死路。
        """
        if self._quitting or not self.prefs.close_to_tray:
            return False
        panel = getattr(self, "widget_panel", None)
        if panel is None or not panel.isVisible():
            return False
        app = QApplication.instance()
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
        # 挂件是独立顶层窗口，主窗口 hide 不会带走它；退出时手动收拾
        panel = getattr(self, "widget_panel", None)
        if panel is not None:
            panel.hide()
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

    def _show_messages(self) -> None:
        """顶栏「消息」胶囊：把浮层叫出来。

        悬停已经会自动浮出，这里只是给"鼠标不动 / 触屏"留一个显式入口。
        **不压暗主窗口、不停摄像头播放**——它是胶囊的展开态，不是一层页面。
        """
        self.messages.show_for()

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
    def _show_page(self, index: int, zone: str | None) -> None:
        """切换中间内容区。``index == 0`` 是卡片墙，``zone`` 对应侧栏选中态。

        离开卡片墙要把墙上的画面停掉：整页盖住后画面不可见，继续取流只是白占
        一个观看位（和打开子窗口 / 收进托盘同一套理由）。切回卡片墙时不自动
        恢复播放，仍需用户点卡片上的播放。

        说话条只在卡片墙上显示：功能区页面整条横带一起藏，页面顶到窗口上沿；
        回到卡片墙再显示。顶部警告条是 body 上的浮层，位置不随这个开关变化。
        """
        if index != 0:
            self.wall.stop_playback("切换到独立页面")
        self.pages.setCurrentIndex(index)
        # 切到耗材页时对一遍数据：页面常驻，期间可能加了 / 删了监控项，
        # 或者设备改了名、换了房间
        if zone == "consumables":
            self.consumables_page.refresh()
        self.nav.set_active_zone(zone)
        self._band.setVisible(index == 0)
        # 记录上次页面：恢复开关关掉时也照记，打开后下次启动才能回到这里
        self.prefs.set_last_page(zone or "wall")

    def _on_room_selected(self, home: str, room: str) -> None:
        # 房间 = 卡片墙的入口：从任何功能区页面点回来都先回墙，再滚到那一间
        self._show_page(0, None)
        self.wall.scroll_to_room(home, room)
        # "全部设备"那一行的 home 是空串（它跨家庭复用），记录时补当前家庭
        self.prefs.set_last_room(home or self.store.active_home, room)

    def _on_home_selected(self, home_id: str) -> None:
        if home_id == self.store.active_home:
            return
        self.store.active_home = home_id
        # 浮层里的消息按家庭过滤过：切家就收起来，悬停再读一次新的
        self.messages.hide()
        self.store.scenes = self.store.scenes_for(home_id)
        self.wall.set_home(home_id)
        self.nav.build(self.store, home_id)
        # 切家庭后侧栏默认停在"全部设备"（build 会重置高亮），房间记录跟着更新
        self.prefs.set_last_room(home_id, "*")
        self._settings().setValue("ui/active_home", home_id)
        self._update_header()
        self._refresh_speakers()
        # 规则按家庭隔离：停在自动化页时切家庭，列表要跟着换成新家的
        if self.pages.currentIndex() == self._zone_pages["automation"]:
            self.automation_page.refresh()
        # 耗材页**不跟着切家庭**（2026-11-21 起按全部家庭统计，见 consumables_page）：
        # 这里不再重建。重建一次内容也不会变，只是白丢焦点。侧栏那两颗圆点同理，
        # 它们现在也是全家庭计数，不随这个动作变化。
        # 监控页的选源栏只列当前家庭的摄像头；页面不显示时重建没有副作用
        self.monitor_page.refresh()
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
        # 耗材页只关心自己订阅的那几条属性，传 dids 让它自己过滤
        self.consumables_page.update_cards(dids)
        # 监控页只改选源栏的在线状态与缩略图，**不重建**（重建会打断拖动）
        self.monitor_page.update_states(dids)
        self.widget_panel.update_states(dids)
        self._update_header()
        self._refresh_speakers()

    def _on_consumable_counts(self, warn: int, crit: int) -> None:
        """把「N 项轻提醒 / M 项重提醒」画到侧栏「耗材」入口和挂件右下角。

        2026-11-21 起耗材页跨家庭统计，所以这两个数**本身就是全部家庭的合计**，
        两个消费者拿同一份数、不会再出现"挂件说 3 项、侧栏说 0 项"。以后若有人
        想把侧栏改回单家庭，必须同时给挂件单独算一份——别只改这一边。
        """
        self.nav.set_zone_badges("consumables", warn, crit)
        self.widget_panel.set_badges(warn, crit)

    def _on_layout_changed(self) -> None:
        """排序或隐藏设备变了：墙和导航都要重建（顺序变了无法增量更新）。"""
        active = self.store.active_home
        self.wall.set_home(active)
        self.nav.build(self.store, active)
        self._refresh_tabs()
        self._update_header()
        # 挂件只按 did 记设备，顺序/隐藏跟墙无关；但设备被隐藏或改名后卡片文案
        # 要跟着变，整块重建最省事（最多 24 张）
        self.widget_panel.rebuild()

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
        # 「在线设备」这块也能关（设置 → 外观 → 顶栏展示）。关掉就整颗不画，
        # 温湿度顺着流式布局往左靠；顶栏余量随之变宽，_fit_climate 会自己多放一颗。
        self.status.setVisible(self.prefs.shows_header("online"))
        self._update_climate(active)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # 顶栏余量跟着窗口宽度变：拉宽要把刚才收起的温湿度放回来，收窄要再收
        self._fit_climate()

    def _update_climate(self, active: str) -> None:
        """全屋温湿度两颗胶囊（口径见 ``core/climate.py``）。

        跟着顶栏的家庭走，所以每次 ``_update_header`` 都重算——它挂在 ``_on_changed``
        上，读数每轮轮询回来就会刷一次，不需要另设定时器。

        隐藏设备**照算**：隐藏只是"别在卡片墙上占一格"，它的房间还在那儿。
        被用户关掉的那颗（设置 → 外观 → 顶栏展示）交给 ``set_shown``，
        与"有没有数据"分开记。
        """
        for slot in climate_mod.SLOTS:
            self.climate.set_shown(slot, self.prefs.shows_header(slot))
        self.climate.set_climate(
            climate_mod.collect(self.store.devices.values(), active)
        )
        self._fit_climate()

    def _fit_climate(self) -> None:
        """窄窗口下逐颗收起温湿度胶囊，别让它们把右边的消息 / 圆按钮顶出去。

        ``_update_climate`` 只管"有没有数据 / 用户关没关"，这里管"放不放得下"——
        两件事分开，否则要么窄窗口少看一个数、要么顶栏控件互相压住（实测 offscreen
        字体下 920px 就压上了，真雅黑刚好够，多几个家庭就没了）。
        顺序：两颗 → 只留温度 → 整颗隐藏。窗口拉宽时同一段代码会按数据重新放出来。
        """
        strip = getattr(self, "climate", None)
        head = getattr(self, "_head", None)
        if strip is None or head is None:
            return
        # 先让数据侧复算一遍可见性：不这样的话"因为窄而收起"会被当成"没有数据"，
        # 窗口拉宽以后再也放不回来。
        strip.set_climate(strip.climate())
        present = strip.present()
        if not present:
            return
        # 按"没有它"量一次顶栏的最小宽度：其余都是定宽控件，最小即所需
        strip.setVisible(False)
        head.invalidate()
        base = head.minimumSize().width()
        gap = strip.GAP
        need_one = base + gap + present[0].width()
        need_all = need_one + (gap + present[1].width() if len(present) > 1 else 0)
        if self.width() >= need_all:
            strip.setVisible(True)
            for chip in present:
                chip.setVisible(True)
        elif self.width() >= need_one:
            strip.setVisible(True)
            present[0].setVisible(True)
            for chip in present[1:]:
                chip.setVisible(False)
        else:
            strip.setVisible(False)

    def _reload(self) -> None:
        self._set_status("正在刷新设备列表…")
        self.store.rebuild()

    def _on_loaded(self) -> None:
        homes = self.store.homes()
        ids = [h.id for h in homes]
        # 启动恢复只在第一次加载时改家庭；之后的重载尊重用户当前的选择
        if (self._start_placement_pending and self.prefs.restore_last_page
                and self.prefs.last_home in ids):
            self.store.active_home = self.prefs.last_home
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
            # 场景另算：这一轮可能刚拉到新建 / 改名 / 删掉的场景，而设备集没变时
            # 上面那条路不会重画场景条（5 分钟一轮，不补就永远是一屏旧场景）
            self.wall.refresh_scenes(self.store.scenes)
        if self._start_placement_pending and active:
            self._start_placement_pending = False
            self._apply_start_room(active)
        # 设备表变了：耗材页要重新对设备名 / 房间 / 值域（否则改名后卡片还挂着旧名）
        self.consumables_page.refresh()
        self._update_header()
        self._refresh_speakers()
        self.widget_panel.rebuild()
        self.store.poll()
        # 设备表齐了，"本程序启动时"的规则才真的发得出设备动作（服务内部幂等，
        # 重登 / 重新加载不会再触发一次）。未登录时走不到这里，规则等下次启动。
        fire_start = getattr(self.automation, "fire_start_rules", None)
        if callable(fire_start):
            fire_start()
        # 桌面快捷方式在"程序正在启动"的窗口期发来的广播会丢（原生消息过滤器还没
        # 装好），所以加载完成后补捞一次；此后的点击都走广播那条路。
        # 延到下一拍：先让界面画完，再发请求。
        if not self._scene_boot_drained:
            self._scene_boot_drained = True
            QTimer.singleShot(0, lambda: self._drain_scene_requests())

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
            self.speaker_bar.notice(f"发送失败：{reason or explain(code)}", kind="fail")

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
        所以不弹二次确认。结果走**主窗口内**的浮层（`speaker_bar.notice`，
        与极客版登录码那条同一套样式：卡面色 + 左侧状态圆点，锚在说话条输入框
        下方、固定 3 秒）：右键菜单只有主窗口可见时才点得到，窗口内提示就一定
        看得见。文案只说刷新了多少项，没读到的条数与原因只进日志；一项都没读到
        才提示"稍后会自动重试"。
        """
        from .device_reset import refresh_result_text

        def done(ok: int, failed: int, error: str) -> None:
            self.speaker_bar.notice(
                refresh_result_text(ok, failed, error),
                kind="ok" if ok else "fail",
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
        from .device_reset import reset_result_text

        self.speaker_bar.notice(reset_result_text(name))

    def _on_scene(self, scene_id: str) -> None:
        self.store.run_scene(scene_id, self.store.active_home)

    # -- 桌面快捷方式 ------------------------------------------------------
    def on_activation_broadcast(self) -> None:
        """收到"又有人启动了这个程序"的全局广播。

        两种来源：用户又点了一次程序图标（→ 把窗口叫到前台），或者点了一个场景的
        桌面快捷方式（→ 只执行场景，**不动窗口**）。广播本身没有载荷
        （``PostMessageW(HWND_BROADCAST, msg, 0, 0)``），所以判据是 pending 目录里
        有没有刚放进来的条目。

        ``notify_existing`` 一次发三条，后两条会看到目录已空；用
        ``SCENE_DRAIN_GRACE`` 把这簇广播当成同一次点击，不去抢焦点。
        """
        if self._drain_scene_requests():
            return
        if time.monotonic() - self._last_scene_drain < SCENE_DRAIN_GRACE:
            return
        try:
            focus_window(self)
        except RuntimeError:  # 窗口正在销毁（退出途中）
            pass

    def _drain_scene_requests(self) -> bool:
        """把待执行条目取走并执行，返回这一轮有没有东西。

        执行不排队、不合并：用户连点五次就是五次。但一轮最多取
        ``scene_shortcut.PENDING_LIMIT`` 条，剩下的下一轮拿走。
        """
        items = scene_shortcut.drain()
        if not items:
            return False
        self._last_scene_drain = time.monotonic()
        for item in items:
            self._execute_scene_request(
                str(item.get("scene_id") or ""), str(item.get("home_id") or "")
            )
        return True

    def _execute_scene_request(self, scene_id: str, home_id: str) -> None:
        """执行一个由桌面快捷方式发起的场景。

        ``home_id`` 来自快捷方式，**不改当前页面 / 当前家庭**——用户可能正在看
        另一个家庭的那面墙。场景名查不到（刚在手机里删掉）就用中性说法，
        不拿 id 当名字。
        """
        scene = next(
            (
                s
                for s in (self.store.scenes_by_home.get(home_id) or [])
                if str(s.get("scene_id") or "") == scene_id
            ),
            None,
        )
        name = str((scene or {}).get("name") or "快捷场景")
        applog.log("shortcut", f"执行快捷方式：{name}（scene={scene_id} home={home_id}）")
        self.store.run_scene(
            scene_id,
            home_id,
            on_done=lambda ok, why: self._scene_request_done(name, ok, why),
        )

    def _scene_request_done(self, name: str, ok: bool, reason: str) -> None:
        """快捷方式触发的执行结果。

        成功**只弹窗口内轻提示，不发系统通知**（用户 2026-11-26 定）：成功没有需要
        用户处理的信息，一次点击换一条通知进了通知中心还得自己清，是纯噪音。
        窗口恰好不可见时退回托盘 tooltip——hover 能看到，也不打扰。

        失败才发系统通知：用户可能已经离开窗口，而失败是要他动手的（重登 / 场景
        被删 / 设备离线），漏掉的代价比多一条通知大。
        """
        if ok:
            text = f"已执行「{name}」"
        else:
            text = f"场景执行失败「{name}」" + (f" · {reason}" if reason else "")
        visible = self.isVisible()
        tray = getattr(self, "tray", None)
        if not ok and tray is not None:
            tray.show_message("米家控制台", text, 6000, warning=True)
        if visible:
            tooltip.toast_top(
                self, text, self.palette_ref,
                kind=tooltip.TOAST_OK if ok else tooltip.TOAST_FAIL, ms=6000,
            )
        elif ok and tray is not None:
            # 窗口不在（最小化 / 收进托盘）时成功不提示太像"点了没反应"，也不该升级成
            # 系统通知：托盘 tooltip 是零打扰的一档，鼠标扫一眼就有。8 秒后换回当前
            # 状态，免得一条"已执行"长期挂在那儿让人以为是刚发生的。
            tray.set_status(text)

            def _restore_tip() -> None:
                try:
                    tray.set_status(self.status.text())
                except RuntimeError:  # 窗口已销毁（正在退出）
                    pass

            QTimer.singleShot(8000, _restore_tip)

    def _on_scene_shortcut(self, scene_id: str) -> None:
        """右键场景 → 创建桌面快捷方式。

        **不记"哪个场景建过"**：快捷方式自己带着 ``scene_id``，再点一次就是覆盖；
        用户改名 / 挪走 / 删掉都在程序视野之外，记一份账必然对不上，还得自愈。
        """
        home = self.store.active_home
        scene = next(
            (
                s
                for s in self.store.scenes_for(home, include_hidden=True)
                if str(s.get("scene_id") or "") == scene_id
            ),
            None,
        )
        name = str((scene or {}).get("name") or "")
        try:
            path = scene_shortcut.create(
                scene_id, home, name, icon=str(app_icon.path() or "")
            )
        except OSError as exc:
            applog.log("shortcut", f"创建桌面快捷方式失败：{exc}")
            tooltip.toast_top(
                self, f"创建快捷方式失败 · {exc}", self.palette_ref,
                kind=tooltip.TOAST_FAIL, ms=6000,
            )
            return
        applog.log("shortcut", f"已创建桌面快捷方式：{path}")
        tooltip.toast_top(
            self, f"已在桌面创建「{path.stem}」", self.palette_ref, ms=5000
        )

    def _on_escape(self) -> None:
        """``Esc``：先收消息浮层，再照顾正在输入的那一格。

        浮层是"悬停出来的"，Esc 是它的键盘退路；光标又在指令条里时，按 Esc 却
        关掉浮层也说不通——所以先看浮层开没开，再看离光标最近的输入框：
        指令条有字就清字，没字就交出焦点。
        """
        if self.messages.isVisible():
            self.messages.hide()
            return
        bar = getattr(self, "speaker_bar", None)
        if bar is not None and bar.input.hasFocus():
            if bar.input.text():
                bar.input.clear()
            else:
                bar.input.clearFocus()

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
        """功能区入口：整页替换中间内容区，选中态跟着挪到这一格。

        「挂件」不是页面而是开关：点一下弹出桌面挂件、再点一下收起（用户
        2026-11-20 定，原先开关只藏在设置页里）。其余入口都有页面：「电脑自动化」
        是规则列表页，「耗材」「监控」目前是写明「尚未开放」的占位页（别给空白页）。
        回到卡片墙统一靠点左栏房间。
        """
        if key == "widget":
            self._toggle_widget_visible()
            return
        index = self._zone_pages.get(key)
        if index is None:
            return
        if key == "automation":
            # 规则按家庭隔离：进来时按当前家庭重画一次，别停在上次那个家的列表
            self.automation_page.refresh()
        elif key == "monitor":
            # 摄像头列表按家庭过滤，设备也可能刚改名 / 换房间；进页时对一遍。
            # 真正的取流在页面自己的 showEvent 里（prepare + 预热地址；**不自动播**）
            self.monitor_page.refresh()
        self._show_page(index, key)

    def open_settings(self) -> None:
        from .settings import SettingsDialog

        dlg = SettingsDialog(self.store, self.palette_ref, self,
                             account=self.client.account_info(),
                             tray_available=self.tray.active)
        dlg.theme_selected.connect(self.set_theme)
        dlg.preset_selected.connect(self.set_preset)
        # 顶栏三块胶囊的开关：设置页只写偏好，画不画由这里重算一遍——
        # 顶栏是流式布局，少一颗要多出来的宽度得重新量（_fit_climate）
        dlg.header_changed.connect(lambda _key: self._update_header())
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
        """执行退出登录。二次确认已经在设置页里做过（与「全部重置」同序）。"""
        from .confirm import notice

        self.client.logout()
        if self.client.last_error:
            # 文件被占用 / 只读目录：内存里的凭据已经清掉了，但磁盘上还留着，
            # 下次启动会直接自动登录，得跟用户说清楚。
            self._cover_dim(True)
            try:
                notice(self, "凭据文件没有删掉", self.client.last_error, self.palette_ref)
            finally:
                self._cover_dim(False)
        self.store.clear()
        # 设备列表空了，指令条也别再挂着一台已经不认识的音箱
        self.speaker_bar.set_targets([])
        # 卡片墙换成"已退出"占位，先把内容区切回墙，别停在某个功能区页面
        self._show_page(0, None)
        self.wall.show_placeholder("已退出登录，扫码后可继续使用")
        self.tabs.set_homes([], "")
        self.nav.build(self.store, "")
        self._set_status("已退出登录 · 等待扫码")
        self._show_login()

    # -- 桌面挂件 --------------------------------------------------------
    def _wire_desktop_widget(self) -> None:
        """挂件只发"用户干了什么"，怎么落盘、开哪个窗口都由主窗口决定。"""
        w = self.widget_panel
        w.open_device_requested.connect(self._on_card_clicked)
        w.toggle_requested.connect(self._on_toggle)
        w.remove_device_requested.connect(self._on_widget_remove)
        w.open_consumables_requested.connect(self._on_widget_open_consumables)
        w.open_main_requested.connect(self.show_from_tray)
        w.manage_requested.connect(self._open_widget_manager)
        w.quit_requested.connect(self.quit_app)
        w.rebuild_needed.connect(self._rebuild_desktop_widget)
        w.visibility_changed.connect(self._on_widget_visibility_changed)

    def _on_widget_visibility_changed(self, visible: bool) -> None:
        """挂件真的上了屏 / 收了：左栏入口亮起状态与托盘「桌面挂件」勾选一起变。

        两处都以"此刻在不在屏幕上"为准，不回写偏好——当次显隐是会话状态。
        托盘比挂件晚建（``__init__`` 里 tray 最后才创建），所以取不到时就跳过，
        创建后那次 ``set_widget_checked`` 会补上初始值。
        """
        self.nav.set_widget_on(visible)
        tray = getattr(self, "tray", None)
        if tray is not None:
            tray.set_widget_checked(visible)

    def _set_widget_visible(self, visible: bool) -> None:
        """显隐挂件。左栏入口、挂件自己的 × 之外的托盘入口都走这里。"""
        if visible == self.widget_panel.isVisible():
            return
        if not visible:
            self.widget_panel.hide()
            return
        self.widget_panel.show()
        self.widget_panel.raise_()
        self.widget_panel.rebuild()
        self.widget_panel.apply_layer(self.prefs.widget_layer)

    def _toggle_widget_visible(self) -> None:
        """左栏「桌面挂件」入口：在屏幕上就收起，不在就弹出来。

        这里**不写** ``widget_autostart``：那个开关只回答"下次启动要不要自动出现"，
        归设置页管；当次显隐是会话状态。挂件自己的 × 也走同一条路径。
        """
        self._set_widget_visible(not self.widget_panel.isVisible())

    def _on_tray_widget_toggle(self, on: bool) -> None:
        """托盘「桌面挂件」：只切换**当前显隐**，不碰 ``widget_autostart``。

        托盘开关与设置页「启动时开启桌面挂件」是两个独立设定（用户 2026-11-23 明确）：
        前者只管此刻在不在屏幕上，后者只管下次启动要不要自动出现，不能联动。
        勾选态由 ``visibility_changed`` 回灌，菜单不另存一份。
        """
        self._set_widget_visible(on)

    def _show_widget_at_start(self) -> None:
        if not self.prefs.widget_autostart:
            return
        self.widget_panel.show()
        self.widget_panel.apply_layer(self.prefs.widget_layer)

    def _open_widget_manager(self) -> None:
        """头部齿轮：独立的宫格管理窗（布局 + 槽位），不再借道设置页。"""
        from .widget_manager import manage_widget_devices

        before = (self.prefs.widget_layout, list(self.prefs.widget_slots))
        if not manage_widget_devices(self.store, self.palette_ref, self):
            return
        after = (self.prefs.widget_layout, list(self.prefs.widget_slots))
        if after != before:
            self.widget_panel.rebuild()

    def _on_widget_remove(self, did: str) -> None:
        """卡片右键的「从挂件移除」：清掉占着这台设备的槽位，显隐不变。"""
        if self.prefs.remove_widget(did):
            self.widget_panel.rebuild()

    def _on_widget_open_consumables(self) -> None:
        self.show_from_tray()
        self._show_page(self._zone_pages["consumables"], "consumables")

    def _rebuild_desktop_widget(self) -> None:
        """看门狗判到原生窗口已经没了（explorer 被杀）：换一个新窗口顶上。

        旧对象不能再 show / 再 SetParent——它的 Win32 句柄已经失效，Qt 这边
        却还以为它可见。建新的、接好信号、按旧窗口记的档位落位，然后丢掉旧的。
        """
        if not self.widget_panel.isVisible():
            return
        old = self.widget_panel
        # 用偏好里的目标档，不是旧窗口的实际档：explorer 刚被杀时旧窗口可能已经
        # 降级成普通档，但用户想的是贴桌面——新窗口要再试一次并自己重试
        layer = self.prefs.widget_layer or old.layer
        # 先藏旧的：轮询登记用的是同一个 owner "widget"，旧的 hideEvent 会把新窗口
        # 刚登记的 3000 ms 撤掉
        old.hide()
        new = DesktopWidget(
            self.store, self.prefs, self.palette_ref, self.poll_demand, None
        )
        self.widget_panel = new
        self._wire_desktop_widget()
        new.show()
        new.rebuild()
        new.apply_layer(layer)
        old.deleteLater()

    # -- 主题 ------------------------------------------------------------
    def toggle_theme(self) -> None:
        self.set_theme("light" if self.palette_ref.is_dark else "dark")

    def set_theme(self, name: str) -> None:
        name = "dark" if name == "dark" else "light"
        if self.palette_ref.name == name:
            return
        self.palette_ref = theme.palette_for(self._preset, name)
        self._apply_theme()
        self._save_settings()

    def set_preset(self, preset_id: str) -> None:
        """换背景预设：明暗模式不变，只换底色与卡片透明度。"""
        if preset_id not in theme.PRESET_IDS or preset_id == self._preset:
            return
        self._preset = preset_id
        self.palette_ref = theme.palette_for(preset_id, self.palette_ref.name)
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
        self.automation_page.set_palette(pal)
        self.consumables_page.set_palette(pal)
        self.monitor_page.set_palette(pal)
        self.widget_panel.set_palette(pal)
        self._dim.set_palette(pal)
        self.status.set_palette(pal)
        self.climate.set_palette(pal)
        self.btn_messages.set_palette(pal)
        self.messages.set_palette(pal)
        self.alert_bar.set_palette(pal)
        self.speaker_bar.set_palette(pal)
        tooltip.set_palette(pal)
        tray = getattr(self, "tray", None)
        if tray is not None:
            tray.set_palette(pal)
        self._header.set_palette(pal)
        self._header_line.set_color(pal.border)
        self._header_line.setVisible(not pal.surface.is_gradient())
        self.update()


__all__ = ["MainWindow"]
