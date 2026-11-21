# SPDX-License-Identifier: GPL-3.0-or-later
"""系统托盘常驻：关窗进托盘，程序留在后台继续轮询。

为什么值得做：关窗即退出意味着"看一眼设备"每次都要重新启动、重新读凭据、重新拉
一遍设备列表。留在托盘里，点开就是热的。

四条刻意的选择：

1. **托盘不可用时不假装**：``tray_available()`` 为假（远程会话、精简系统、离屏
   测试）就直接走"关窗即退出"。硬撑一个不显示的托盘图标，结果是程序还在跑却
   没有任何入口——用户只能去任务管理器里杀。
2. **收进托盘时释放摄像头会话**：窗口藏了画面就完全不可见，继续取流等于白占一个
   观看位（设备侧并发上限 3 路）和一百多 KB/s。释放是免费的，点开再取。
3. **真退出必须显式关图标**：Windows 通知区域不会因为进程结束就清掉图标，会留一个
   "幽灵"空位，必须把鼠标划过去才消失。``remove()`` 先 ``hide()`` 再退出事件循环。
4. **系统注销/关机时不拦**：那时 Qt 也会来要一次 ``close``，收进托盘会让关机卡在
   我们这个进程上。判据是 ``QGuiApplication.isSavingSession()``。

菜单用 ``ui/menu.py`` 的 ``Menu``（自绘圆角 + 透明底），不是裸 ``QMenu``：裸的那层
是**独立原生窗口**，默认带不透明实心底，QSS 的 ``border-radius`` 只画出圆角图案，
四角外面还是方块底色——真机上就是"四个黑角"。这个坑在主窗口卡片菜单上踩过，做法
别翻回去。菜单里目前只有**打开主窗口**和**退出**两项，其余功能（场景、刷新）留着
以后按需加，不要提前堆。

``MIHOME_NO_TRAY=1`` 是排障开关：怀疑"托盘里点不开 / 图标不显示"时用它把托盘
整个关掉，程序回到"关窗即退出"的老行为，好确认问题是不是托盘引起的。

**``show()`` 成功返回不等于图标挂上了**（2026-11-21 事故）：``QSystemTrayIcon.show()``
是 void，内部那句 ``Shell_NotifyIcon(NIM_ADD)`` 失败时既不抛异常也不留返回值，QSS
一切正常的程序照样可能没有任何托盘图标。用户那次是"关掉管理员启动、过了系统安全
警告对话框之后启动"，图标没挂上，而窗口还是照旧"收进托盘"——收进了一个空托盘，
除了任务管理器没有别的入口。

所以 ``install()`` 现在分三步：``show()`` → **问系统"到底挂上没"** → 没挂上就
1.2s / 3.5s / 9s 重试（``hide()`` + ``show()`` 是幂等的），四次都失败才认输。
判据用 ``Shell_NotifyIconGetRect``（只读，不改图标）：先在**本进程**里找 Qt 建的
托盘消息窗口（类名以 ``TrayIconMessageWindowClass`` 结尾），再按 (hwnd, uID=0)
要矩形，拿得到才算真的在通知区域里。每次尝试都写 ``logs/tray.log``。
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from ..core import applog
from . import app_icon, theme
from .menu import Menu

_ICON_SIZES = (16, 20, 24, 32, 48)

#: ``show()`` 没挂上时的重试时刻（毫秒）。0 时刻那次是 install 里同步做的。
#: 三次的间距越拉越开：前两次盖住"通知区域正好忙"（NIM_ADD 会以 ERROR_TIMEOUT
#: 失败），最后一次盖住 explorer 短暂抽风。
_RETRY_DELAYS_MS = (1200, 3500, 9000)


def tray_available() -> bool:
    """这台机器、这个会话里能不能用托盘。"""
    if os.environ.get("MIHOME_NO_TRAY"):
        return False
    return QSystemTrayIcon.isSystemTrayAvailable()


class _NotifyIconIdentifier(ctypes.Structure):
    """``NOTIFYICONIDENTIFIER``。查图标位置要用它，字段顺序不能动。"""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("guidItem", ctypes.c_ubyte * 16),
    ]


def _tray_message_hwnd() -> int:
    """本进程里 Qt 建的托盘消息窗口句柄；没有就返回 0。

    Qt 每个 ``QSystemTrayIcon`` 建一个这样的窗口，类名带版本前缀
    （本机实测 ``Qt6120TrayIconMessageWindowClass``），所以按后缀匹配而不是全名。
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    me = ctypes.windll.kernel32.GetCurrentProcessId()

    found: list[int] = []

    @enum_proc
    def visit(hwnd, _param):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == me:
            name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, name, 256)
            if name.value.endswith("TrayIconMessageWindowClass"):
                found.append(int(hwnd))
        return True

    user32.EnumWindows(visit, 0)
    return found[0] if found else 0


def registered_with_shell() -> bool:
    """图标此刻是不是真的在通知区域里。

    ``QSystemTrayIcon.show()`` 没有任何返回值，失败也静默；唯一可靠的判据是反过来
    问系统一句。``Shell_NotifyIconGetRect`` 是**只读**的：图标在（哪怕缩在溢出
    面板里）就给矩形，不在就返回 E_FAIL。

    非 Windows 或**平台插件不是 windows**（离屏 / minimal，50 多个验证脚本都在跑）
    以及探针本身出问题时一律当作"在"——这条探针是给真机排障用的，不能让它在别处
    把托盘判死。反过来说：**只有在真的 windows 平台上它才会说"没挂上"**，那正是
    唯一有意义的场合。
    """
    if sys.platform != "win32":
        return True
    app = QApplication.instance()
    if app is not None and app.platformName() != "windows":
        return True
    try:
        hwnd = _tray_message_hwnd()
        if not hwnd:
            return False
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.Shell_NotifyIconGetRect.argtypes = [
            ctypes.POINTER(_NotifyIconIdentifier),
            ctypes.POINTER(wintypes.RECT),
        ]
        shell32.Shell_NotifyIconGetRect.restype = ctypes.c_long
        nid = _NotifyIconIdentifier()
        nid.cbSize = ctypes.sizeof(_NotifyIconIdentifier)
        nid.hWnd = wintypes.HWND(hwnd)
        nid.uID = 0  # Qt 用消息窗口本身当唯一键，uID 固定 0
        rect = wintypes.RECT()
        return shell32.Shell_NotifyIconGetRect(ctypes.byref(nid), ctypes.byref(rect)) == 0
    except (OSError, AttributeError, TypeError, ValueError):
        return True




def tray_icon() -> QIcon:
    """托盘图标：**就是程序图标**（``assets/app.ico``）。

    以前这里用代码画"圆角方块 + 2×2 圆点"——那是首页卡片墙的抽象，跟任务栏里的
    程序图标根本不是同一枚图形，同一个程序在任务栏和托盘里两个样，用户会当成两个
    程序在跑（2026-11-17 反馈）。现在统一读同一份 ICO。

    和旧画法相比有两点要记住：

    * **不再跟主题换色**：这是一枚品牌标记，不是状态点。主题切换时不用重设。
    * **别拿一张大图缩**：ICO 里 16 / 24 / 32 各档的几何是分开画的（16 那档砍掉
      屏幕，只留房子 + 短颈 + 底座），QIcon 会按通知区域的设备像素自己挑档，所以
      托盘里那枚 16px 图案和任务栏里的是同一套设计。

    ICO 读不到时退回旧画法——托盘里空一块比图形不对更糟，那意味着用户找不到程序。
    """
    icon = app_icon.load()
    if not icon.isNull():
        return icon
    return _drawn_icon(theme.LIGHT.accent)


def _drawn_icon(accent: str) -> QIcon:
    """兜底画法：圆角方块 + 2×2 圆点（首页那张卡片墙）。只在缺 ICO 时用。"""
    icon = QIcon()
    for size in _ICON_SIZES:
        pix = QPixmap(size, size)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(accent))
        edge = size - 1
        p.drawRoundedRect(QRectF(0.5, 0.5, edge, edge), size * 0.28, size * 0.28)

        p.setBrush(QColor("#FFFFFF"))
        if size <= 20:
            # 小尺寸下圆点的抗锯齿只会把 2~3 个像素抹成灰边，看起来像一张脏图；
            # 直接按整数像素画方块，反而干净、边界清楚
            p.setRenderHint(QPainter.Antialiasing, False)
            px = max(2, round(size * 0.19))
            off = max(2, round(size * 0.17))
            lo = (size - px) // 2 - off
            hi = (size - px) // 2 + off
            for x in (lo, hi):
                for y in (lo, hi):
                    p.drawRect(x, y, px, px)
        else:
            dot = size * 0.13
            span = size * 0.34
            for dx in (-span / 2, span / 2):
                for dy in (-span / 2, span / 2):
                    p.drawEllipse(
                        QRectF(size / 2 + dx - dot / 2, size / 2 + dy - dot / 2, dot, dot)
                    )
        p.end()
        icon.addPixmap(pix)
    return icon


class TrayController(QObject):
    """托盘图标 + 菜单。只管"点了什么"，真正的动作交给窗口接。"""

    open_requested = Signal()
    quit_requested = Signal()

    def __init__(
        self,
        palette: theme.Palette,
        parent: QObject | None = None,
        available: bool | None = None,
        probe=None,
    ) -> None:
        super().__init__(parent)
        self._p = palette
        #: 调用方**明确**表过态（测试 / 设置页）时，这个判断不许被"重试猜出来的
        #: 结论"推翻；只有自动探测（``available=None``）才允许先试再定。
        self._explicit = available is not None
        self._available = tray_available() if available is None else bool(available)
        self._shown = False
        #: 注册判据的注入点（离屏平台没有通知区域，测试要能替掉它）
        self._probe = probe
        self._attempt = 0
        #: 待办的事是"复查可用性"而不是"重试注册"（自动探测说没有通知区域时）
        self._recheck = False
        #: 返回一行状态文字，拼进悬停提示
        self.status_provider = lambda: ""

        self._icon = QSystemTrayIcon(tray_icon(), self)
        self._icon.setToolTip("米家控制台")
        # 圆角菜单（透明底 + 去边框），理由见模块开头的第 5 段
        self._menu = Menu(None, palette)
        self._build_menu()
        self._icon.setContextMenu(self._menu)
        self._icon.activated.connect(self._on_activated)

        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._retry)

    # -- 生命周期 --------------------------------------------------------
    @property
    def available(self) -> bool:
        """这台机器能不能用托盘（与有没有装起来不是一回事）。"""
        return self._available

    @property
    def active(self) -> bool:
        """图标**真的**在通知区域里吗。

        注意这是"问过系统"的结论，不是"我们调过 show()"——两者在 2026-11-21 那次
        事故里正好相反，而这个差别决定关窗是"收进托盘"还是"直接退出"。
        """
        return self._shown

    def describe_state(self) -> str:
        """一行诊断：系统说支不支持 / 我们挂到第几次 / 图标是不是空的。

        Qt 对空图标是"show() 直接返回，连 NIM_ADD 都不发"，所以 ``icon_null``
        这一项能把"注册失败"和"根本没注册"分开——两者在界面上长得一样。
        """
        return (
            f"available={self._available} active={self._shown} "
            f"icon_null={self._icon.icon().isNull()} "
            f"attempt={self._attempt}/{len(_RETRY_DELAYS_MS)} "
            f"system={QSystemTrayIcon.isSystemTrayAvailable()}"
        )

    def install(self) -> bool:
        """挂上托盘。返回图标是否真的进了通知区域。

        返回 False 时调用方要退回"关窗即退出"——**绝不能**留一个藏在空托盘里的
        进程：那等于程序人间蒸发，用户只能开任务管理器。失败后的重试仍在后台
        进行，成功了 ``active`` 会自己变真。
        """
        if os.environ.get("MIHOME_NO_TRAY"):
            applog.log("tray", "MIHOME_NO_TRAY 已设置，不挂托盘")
            return False
        if self._explicit and not self._available:
            return False
        if self._icon.icon().isNull():
            # Qt 对空图标是"调了 setVisible(true) 也直接返回"，连 NIM_ADD 都不发。
            # 这种失败在托盘里看起来和"注册失败"一模一样，先在这里说清楚。
            applog.log("tray", "托盘图标是空的（assets/app.ico 没找到且兜底画法也失败），不挂托盘")
            return False
        if not self._available:
            # 自动探测说"这个会话没有通知区域"。多半是真的（远程会话 / 精简系统 /
            # 离屏），但也可能是探测那一刻 shell 正忙——给一次复查机会再判死。
            applog.log("tray", "自动探测说没有通知区域，稍后复查一次再决定")
            self._recheck = True
            self._retry_timer.start(_RETRY_DELAYS_MS[1])
            return False
        return self._register(first=True)

    def _register(self, *, first: bool) -> bool:
        """真的要挂了：先关掉"关最后一个窗口就退出"，再走注册 + 校验。"""
        app = QApplication.instance()
        if app is not None:
            # 隐藏最后一个窗口并不等于关闭：默认的"关最后一个窗口就退出"会在
            # hide() 之后把程序带走，那正是我们要避免的。关窗到底是"收进托盘"
            # 还是"退出"由 `_should_hide_to_tray()` 按 `active` 兜底，所以这里
            # 先按"要挂"处理：重试可能稍后才成功。
            app.setQuitOnLastWindowClosed(False)
        self._attempt = 0
        return self._try_register(first=first)

    def _registered(self) -> bool:
        probe = self._probe or registered_with_shell
        return bool(probe())

    def _try_register(self, *, first: bool) -> bool:
        """show()（重试时先 hide()）+ 问系统一句。"""
        if not first:
            # hide() + show() 是幂等的：图标其实已经在时只是重新注册一次，
            # 不在时正好把"半死"的状态清干净再来。
            self._icon.hide()
        self._icon.show()
        if self._registered():
            self._shown = True
            self._available = True
            self._retry_timer.stop()
            tail = "" if first else f"（第 {self._attempt + 1} 次尝试）"
            applog.log("tray", f"托盘图标已挂上{tail}")
            return True

        self._shown = False
        if self._attempt < len(_RETRY_DELAYS_MS):
            delay = _RETRY_DELAYS_MS[self._attempt]
            self._attempt += 1
            applog.log(
                "tray",
                f"show() 之后系统里查不到图标（NIM_ADD 没生效），{delay} ms 后重试"
                f"第 {self._attempt}/{len(_RETRY_DELAYS_MS)} 次",
            )
            self._retry_timer.start(delay)
        else:
            applog.log(
                "tray",
                "托盘图标重试全部失败，退回「关窗即退出」（窗口不会藏进空托盘）",
            )
        return False

    def _retry(self) -> None:
        if self._recheck:
            self._recheck = False
            self._available = tray_available()
            if not self._available:
                applog.log("tray", "复查：仍然没有通知区域，放弃托盘")
                return
            applog.log("tray", "复查：通知区域回来了，开始挂图标")
            self._register(first=True)
            return
        self._try_register(first=False)

    def remove(self) -> None:
        """退出前摘掉图标，避免通知区域留下一个空位。"""
        self._shown = False
        self._recheck = False
        self._retry_timer.stop()
        self._icon.hide()

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        # 图标不跟着主题走（是程序图标，不是状态点），这里不用重设
        # 菜单是独立的一层窗口，自己带一份样式表，不会跟着主窗口的 QSS 走
        self._menu.set_palette(palette)

    def set_status(self, text: str) -> None:
        tip = "米家控制台"
        if text:
            tip = f"{tip}\n{text}"
        self._icon.setToolTip(tip)

    def can_notify(self) -> bool:
        """这条运行路径上系统通知到底能不能弹。

        Windows 上 ``showMessage`` 走的是老式气泡接口：只要托盘图标真的 ``show()``
        过就能弹，窗口是否最小化无所谓。两种弹不出来的情况——系统托盘不可用
        （``MIHOME_NO_TRAY=1`` 或没有通知区域）、以及用户在系统里关掉了本程序的
        通知——后者 Qt 探测不到，所以顶部横幅才是主通道，这里只是加强。
        """
        return self._shown and QSystemTrayIcon.supportsMessages()

    def show_message(
        self,
        title: str,
        body: str,
        msecs: int = 4000,
        *,
        warning: bool = False,
    ) -> bool:
        """气泡提示。系统不支持时静默跳过——它只是提示，不是功能。

        返回是否真的提交给了系统（提交了也可能被系统静默吞掉，见 ``can_notify``）。
        """
        if not self.can_notify():
            return False
        icon = QSystemTrayIcon.Warning if warning else QSystemTrayIcon.Information
        self._icon.showMessage(title, body, icon, msecs)
        return True

    # -- 菜单 ------------------------------------------------------------
    def _build_menu(self) -> None:
        """两项：打开窗口、退出。中间一条分隔线，把"退出"和日常动作分开。

        这里**故意**只留这两项。以前放过「常用场景」子菜单和「刷新设备」，
        用户看过之后要求先去掉——托盘菜单要加什么，等想清楚再加，不提前堆功能。
        """
        m = self._menu
        act_open = m.addAction("打开主窗口")
        act_open.triggered.connect(self.open_requested)
        m.addSeparator()
        act_quit = m.addAction("退出")
        act_quit.triggered.connect(self.quit_requested)

    def menu(self) -> Menu:
        """给验证脚本用：直接查菜单结构，不必真去模拟鼠标。"""
        return self._menu

    # -- 事件 ------------------------------------------------------------
    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # 单击和双击都当"打开"：Windows 用户两种习惯都有，而右键才出菜单，
        # 不会和"我只想看看菜单"打架
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.open_requested.emit()
