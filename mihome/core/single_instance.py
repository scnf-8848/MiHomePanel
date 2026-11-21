# SPDX-License-Identifier: GPL-3.0-or-later
"""单实例守卫：同一份数据目录只允许跑一个窗口。

**为什么不用 `QLocalServer`**：那要 `PySide6.QtNetwork`，而打包清单里根本没收
Qt 的网络模块（`dist` 里没有 `Qt6Network.dll`）。为了"把已有窗口叫到前台"多带
一个 DLL 不划算，所以只用 Win32 的两样东西：

* **命名互斥量**（`CreateMutexW`）回答"是不是已经有人在跑"。内核对象随进程退出
  自动释放，主实例崩掉也不会留下锁死的坑（文件锁会留下死锁）。
* **全局注册消息**（`RegisterWindowMessageW` + `HWND_BROADCAST`）把"用户又点了
  一次图标"告诉在场的那个人：同一个字符串在任何进程里都得到同一个消息号，不必
  按窗口标题找窗口（标题会随家庭、设备变，靠不住）。

拿不到互斥量时**不拦人**：宁可多开一个，也不要打不开。互斥量建不出来（权限、
句柄耗尽）本身就是异常现场，这时候最该做的是让程序起来。

判据是**数据目录**而不是程序名：`prefs.json`、`cache/`、`auth.json` 都在
`data_dir()` 下，两套互不相干的数据（便携版、不同账号）各开一个是合理的。
"""

from __future__ import annotations

import ctypes
import hashlib
import sys
import time
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QTimer, Signal
from PySide6.QtWidgets import QWidget

_IS_WINDOWS = sys.platform == "win32"

#: `Local\` = 只在当前登录会话内可见：换 Windows 用户/远程桌面各一个，互不干扰
_MUTEX_PREFIX = "Local\\MiHomePanel-"
_MESSAGE_PREFIX = "MiHomePanel.Activate-"
_HWND_BROADCAST = 0xFFFF
_ERROR_ALREADY_EXISTS = 183


def _dll(name: str, specs: dict[str, tuple]) -> ctypes.WinDLL | None:
    """开一个 DLL 并把签名设全。

    签名**必须显式设**：ctypes 默认把 Python `int` 当 32 位 `c_int` 传，而 `HANDLE`
    在 x64 上是 8 字节。不设的话 `CloseHandle(0x1f4)` 这类小句柄碰巧能过，
    稍大的句柄会被截断——`release()` 静默失败、互斥量一直占着，表现出来就是
    "退出程序后重开，说已经有一个实例"。
    """
    if not _IS_WINDOWS:
        return None
    try:
        lib = ctypes.WinDLL(name, use_last_error=True)
    except OSError:
        return None
    for fn, (restype, argtypes) in specs.items():
        func = getattr(lib, fn, None)
        if func is None:
            continue
        func.restype = restype
        func.argtypes = argtypes
    return lib


@lru_cache(maxsize=1)
def _kernel32():
    return _dll("kernel32", {
        "CreateMutexW": (wintypes.HANDLE, (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)),
        "CloseHandle": (wintypes.BOOL, (wintypes.HANDLE,)),
    })


@lru_cache(maxsize=1)
def _user32():
    return _dll("user32", {
        "RegisterWindowMessageW": (wintypes.UINT, (wintypes.LPCWSTR,)),
        "PostMessageW": (
            wintypes.BOOL,
            (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM),
        ),
        "SetForegroundWindow": (wintypes.BOOL, (wintypes.HWND,)),
    })


def instance_suffix(data_dir: Path | None = None) -> str:
    """数据目录的短哈希，拼在互斥量名 / 消息名后面。"""
    from . import paths

    target = Path(data_dir or paths.data_dir()).resolve()
    return hashlib.sha1(str(target).encode("utf-8")).hexdigest()[:10]


def focus_window(widget: QWidget) -> None:
    """把已存在的窗口叫到前台。

    最小化时必须先 `showNormal()`，否则 `raise_()` 只会在任务栏上闪一下。
    `SetForegroundWindow` 在前台锁（用户正在操作别的程序）下会失败并返回 0，
    这时 Windows 只让任务栏闪烁——可以接受，不重试、不弹提示。
    """
    if widget.isMinimized():
        widget.showNormal()
    widget.show()
    widget.raise_()
    widget.activateWindow()
    user32 = _user32()
    if user32 is None:
        return
    try:
        user32.SetForegroundWindow(wintypes.HWND(int(widget.winId())))
    except (OSError, AttributeError, TypeError, ValueError):
        pass


class _ActivateFilter(QAbstractNativeEventFilter):
    """QApplication 级别的原生消息过滤（装在应用上，不依赖哪个窗口存在）。

    只认我们自己广播的那条消息，其余一律 `False`（交回 Qt 正常处理）。
    """

    def __init__(self, message_id: int, fire) -> None:
        super().__init__()
        self._message_id = message_id
        self._fire = fire

    def nativeEventFilter(self, event_type, message):  # noqa: N802 - Qt 命名
        try:
            msg = wintypes.MSG.from_address(_address(message))
        except (TypeError, ValueError, OSError):
            return False
        if msg.message != self._message_id:
            return False
        # 不在过滤器里动窗口：这是消息分发的中途，show/raise 要等这一轮结束
        QTimer.singleShot(0, self._fire)
        return True


def _address(message) -> int:
    """原生消息指针 → 整数地址。

    PySide6 各版本传进来的东西不一样（有的给 `int`，有的给 `PyCapsule`），
    两条路都试一下。
    """
    if isinstance(message, int):
        return message
    try:
        from shiboken6 import VoidPtr

        return int(VoidPtr(message))
    except (ImportError, TypeError, ValueError):
        return int(message)  # type: ignore[arg-type]


class SingleInstance(QObject):
    """占用标记 + "又有人点图标"的通知通道。

    用法：
        guard = SingleInstance()
        if not guard.acquire():
            guard.notify_existing()   # 叫醒已有窗口，然后自己退出
            return 0
        guard.activated.connect(...)  # 主实例：别人再启动时调到这里
    """

    #: 又有一个实例启动，希望把当前窗口叫到前台
    activated = Signal()

    def __init__(self, data_dir: Path | None = None) -> None:
        super().__init__()
        self._suffix = instance_suffix(data_dir)
        self.mutex_name = _MUTEX_PREFIX + self._suffix
        self.message_name = _MESSAGE_PREFIX + self._suffix
        self._handle: int | None = None
        self._filter: _ActivateFilter | None = None
        self._message_id = 0

    # -- 状态 ------------------------------------------------------------
    @property
    def held(self) -> bool:
        return self._handle is not None

    # -- 主实例 ----------------------------------------------------------
    def acquire(self) -> bool:
        """True = 拿到了，可以继续启动；False = 已经有人在跑。

        拿不到（API 失败）也返回 True：见模块头的说明。
        """
        kernel32 = _kernel32()
        if kernel32 is None:
            return True
        # 第二个参数 False：不要初始拥有者。我们只关心"内核里有没有同名对象"，
        # 拿拥有权反而会在同线程重复调用时叠加计数、退出时还得 ReleaseMutex
        handle = kernel32.CreateMutexW(None, False, self.mutex_name)
        if not handle:
            return True
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        self._install_filter()
        return True

    def release(self) -> None:
        """显式放开（测试用；正常退出时进程一死内核对象就没了）。"""
        if self._filter is not None:
            from PySide6.QtWidgets import QApplication

            app = QApplication.instance()
            if app is not None:
                app.removeNativeEventFilter(self._filter)
            self._filter = None
        kernel32 = _kernel32()
        if self._handle is not None and kernel32 is not None:
            kernel32.CloseHandle(self._handle)
        self._handle = None

    def _install_filter(self) -> None:
        user32 = _user32()
        if user32 is None:
            return
        self._message_id = user32.RegisterWindowMessageW(self.message_name) or 0
        if not self._message_id:
            return  # 注册不上：单实例仍然成立，只是不能把窗口叫到前台
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            return
        self._filter = _ActivateFilter(self._message_id, self.activated.emit)
        app.installNativeEventFilter(self._filter)

    # -- 后来的实例 ------------------------------------------------------
    def notify_existing(self, rounds: int = 3, interval: float = 0.12) -> bool:
        """广播"我来了"，让在场那个窗口自己跳到前台。

        发三遍是因为对面可能正处在启动过程中（窗口还没建好、过滤器还没装），
        单发一次容易丢。三次总共只多等 240ms，用户点完图标就退出，无感。
        """
        user32 = _user32()
        if user32 is None:
            return False
        message_id = user32.RegisterWindowMessageW(self.message_name) or 0
        if not message_id:
            return False
        sent = 0
        for i in range(max(1, rounds)):
            sent += int(bool(user32.PostMessageW(
                wintypes.HWND(_HWND_BROADCAST), message_id, 0, 0)))
            if i + 1 < rounds:
                time.sleep(interval)
        return sent > 0
