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
"""

from __future__ import annotations

import os

from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from . import theme
from .menu import Menu

_ICON_SIZES = (16, 20, 24, 32, 48)


def tray_available() -> bool:
    """这台机器、这个会话里能不能用托盘。"""
    if os.environ.get("MIHOME_NO_TRAY"):
        return False
    return QSystemTrayIcon.isSystemTrayAvailable()


def tray_icon(palette: theme.Palette) -> QIcon:
    """画一个托盘图标：圆角方块 + 2×2 圆点（就是首页那张卡片墙）。

    Windows 通知区域按 16×16（高 DPI 下 20/24）显示，所以每个尺寸单独画一遍，
    不能拿一张大图缩——缩下去圆点会糊成一团。方块铺不铺满也试过：留 1px 内缩，
    否则在浅色任务栏上会和背景黏在一起，看不出边界。
    """
    icon = QIcon()
    for size in _ICON_SIZES:
        pix = QPixmap(size, size)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(palette.accent))
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
    ) -> None:
        super().__init__(parent)
        self._p = palette
        self._available = tray_available() if available is None else bool(available)
        self._shown = False
        #: 返回一行状态文字，拼进悬停提示
        self.status_provider = lambda: ""

        self._icon = QSystemTrayIcon(tray_icon(palette), self)
        self._icon.setToolTip("米家控制台")
        # 圆角菜单（透明底 + 去边框），理由见模块开头的第 5 段
        self._menu = Menu(None, palette)
        self._build_menu()
        self._icon.setContextMenu(self._menu)
        self._icon.activated.connect(self._on_activated)

    # -- 生命周期 --------------------------------------------------------
    @property
    def available(self) -> bool:
        """这台机器能不能用托盘（与有没有装起来不是一回事）。"""
        return self._available

    @property
    def active(self) -> bool:
        """图标是否真的在通知区域里。"""
        return self._shown

    def install(self) -> bool:
        """挂上托盘。返回是否可用——不可用时调用方要退回"关窗即退出"。"""
        if not self._available:
            return False
        self._icon.show()
        self._shown = True
        app = QApplication.instance()
        if app is not None:
            # 隐藏最后一个窗口并不等于关闭：默认的"关最后一个窗口就退出"会
            # 在 hide() 之后把程序带走，那正是我们要避免的
            app.setQuitOnLastWindowClosed(False)
        return True

    def remove(self) -> None:
        """退出前摘掉图标，避免通知区域留下一个空位。"""
        self._shown = False
        self._icon.hide()

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self._icon.setIcon(tray_icon(palette))
        # 菜单是独立的一层窗口，自己带一份样式表，不会跟着主窗口的 QSS 走
        self._menu.set_palette(palette)

    def set_status(self, text: str) -> None:
        tip = "米家控制台"
        if text:
            tip = f"{tip}\n{text}"
        self._icon.setToolTip(tip)

    def show_message(self, title: str, body: str, msecs: int = 4000) -> None:
        """气泡提示。系统不支持时静默跳过——它只是提示，不是功能。"""
        if not self._shown or not QSystemTrayIcon.supportsMessages():
            return
        self._icon.showMessage(title, body, QSystemTrayIcon.Information, msecs)

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
