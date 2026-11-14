# SPDX-License-Identifier: GPL-3.0-or-later
"""Windows 全局热键注册层（``RegisterHotKey`` + ``WM_HOTKEY`` 原生消息过滤）。

**会话内排他、先到先得**：同一组合先注册者持有，后来者 ``RegisterHotKey`` 返回
``FALSE``（``ERROR_HOTKEY_ALREADY_REGISTERED``）而且**失败是静默的**——所以每次
注册都查返回值，失败在规则页标红，不猜是谁占的。详见
``docs/reference/automation-hotkeys-and-ui.md`` 1 节。

* **一个组合只注册一次**；多条规则共用同一个 id，由 ``RuleSet.rules_for`` 分发。
* 恒带 ``MOD_NOREPEAT``（在 ``hotkeys.to_native`` 里），抑制按住不放的自动重复。
* **同一条 ``WM_HOTKEY`` 会被 Qt 分发两次**，必须自己去重（见 :data:`_DEDUP_SECONDS`）。
* 停用 / 改键 = 先 ``UnregisterHotKey`` 再重注册；启动后**只重试一次**，不无限刷。
* 注册表挂在常驻对象（``AutomationService``）上，绝不挂在会被 ``deleteLater()``
  销毁的对话框上。

骨架复用 ``core/single_instance.py`` 的原生事件过滤器写法；非 Windows 上不注册、
状态置为「不支持」，界面照常打开。
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from functools import lru_cache

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QWidget

from .hotkeys import reserved_reason, to_native

_IS_WINDOWS = sys.platform == "win32"
_WM_HOTKEY = 0x0312
_ID_FIRST = 0x0100          # 官方要求 0x0000–0xBFFF；0xC000+ 留给 DLL
_ID_LAST = 0xBFFF
_OCCUPIED = "被其它程序占用，请换一个（Windows 不告诉是谁占的）"

#: 同一条热键消息的去重窗口（秒）。**实测**：PySide6 6.12 / Win11 上一次按键，
#: ``nativeEventFilter`` 会被调 **两** 次（``wParam`` 相同、``eventType`` 都是
#: ``windows_generic_MSG``），两次间隔 < 1 ms。不去重的话一次按键触发两遍自动化，
#: 第二遍正好撞进 1 s 冷静期——日志里就是那条莫名其妙的「冷静期阻止，剩余 1 秒」。
#:
#: 取 50 ms 而不是 1 ms：两次分发之间可能夹着 Qt 的其它处理，留余量；而人连按同一个
#: 组合键不可能快过 50 ms（一次完整按下 + 抬起就要 60~80 ms），所以不会误丢。
_DEDUP_SECONDS = 0.05


@lru_cache(maxsize=1)
def _user32():
    if not _IS_WINDOWS:
        return None
    try:
        lib = ctypes.WinDLL("user32", use_last_error=True)
    except OSError:
        return None
    lib.RegisterHotKey.restype = wintypes.BOOL
    lib.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    lib.UnregisterHotKey.restype = wintypes.BOOL
    lib.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    return lib


def _address(message) -> int:
    """PySide6 各版本给的是 int 或 PyCapsule，两条路都试（同 single_instance）。"""
    if isinstance(message, int):
        return message
    try:
        from shiboken6 import VoidPtr

        return int(VoidPtr(message))
    except (ImportError, TypeError, ValueError):
        return int(message)  # type: ignore[arg-type]


class _HotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, fire) -> None:
        super().__init__()
        self._fire = fire
        self._last: dict[int, float] = {}

    def nativeEventFilter(self, event_type, message):  # noqa: N802 - Qt 命名
        try:
            msg = wintypes.MSG.from_address(_address(message))
        except (TypeError, ValueError, OSError):
            return False
        if msg.message != _WM_HOTKEY:
            return False
        hotkey_id = int(msg.wParam)
        # 同一条消息会被 Qt 分发两次（原因见 ``_DEDUP_SECONDS``），这里只认第一次。
        # **不能**改成"只看某个 eventType"——实测两次都是 ``windows_generic_MSG``。
        # 时间戳按 id 各记各的：不同热键紧挨着触发时互不影响。
        now = time.monotonic()
        if now - self._last.get(hotkey_id, 0.0) < _DEDUP_SECONDS:
            return True  # 已经处理过，照样吞掉这条消息
        self._last[hotkey_id] = now
        # 消息分发中途别动 UI / 别发云请求，等这一轮结束
        QTimer.singleShot(0, lambda: self._fire(hotkey_id))
        return True


class HotkeyRegistry(QObject):
    """全局热键表。``fired`` 带的是规范键位串，不是 id。"""

    fired = Signal(str)
    status_changed = Signal()

    def __init__(self, window: QWidget | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._hwnd = int(window.winId()) if window is not None else 0
        self._ids: dict[str, int] = {}
        self._by_id: dict[int, str] = {}
        self._status: dict[str, str] = {}
        self._next_id = _ID_FIRST
        self._retried = False
        self._filter = _HotkeyFilter(self._dispatch)
        app = QApplication.instance()
        if app is not None:
            app.installNativeEventFilter(self._filter)

    # -- 查询 ------------------------------------------------------------
    def status(self, keys: str) -> str:
        if not _IS_WINDOWS or _user32() is None:
            return "当前系统不支持全局热键"
        if not keys:
            return "未设置快捷键"
        return self._status.get(keys, "未注册")

    @property
    def registered(self) -> dict[str, str]:
        return dict(self._status)

    # -- 同步 ------------------------------------------------------------
    def sync(self, keys: list[str]) -> None:
        """把注册表对齐到「启用规则里出现过的去重组合」。增删都在这里收口。"""
        want = list(dict.fromkeys(k for k in keys if k))
        for old in list(self._ids):
            if old not in want:
                self._unregister(old)
        for key in want:
            if key not in self._ids:
                self._register(key)
        self.status_changed.emit()

    def retry_once(self, delay_ms: int = 3000) -> None:
        """启动后对「被占用」的组合重试一次，应对开机自启顺序不定的问题。"""
        if self._retried or not any(v == _OCCUPIED for v in self._status.values()):
            return
        self._retried = True
        QTimer.singleShot(delay_ms, self._retry)

    def _retry(self) -> None:
        for key, state in list(self._status.items()):
            if state == _OCCUPIED:
                self._status.pop(key, None)
                self._register(key)
        self.status_changed.emit()

    def _register(self, keys: str) -> bool:
        lib = _user32()
        if lib is None or not self._hwnd:
            self._status[keys] = "当前系统不支持全局热键"
            return False
        reason = reserved_reason(keys)
        if reason:
            self._status[keys] = f"不可用：{reason}"
            return False
        native = to_native(keys)
        if native is None:
            self._status[keys] = "快捷键格式无效"
            return False
        hotkey_id = self._alloc_id()
        if hotkey_id is None:
            self._status[keys] = "注册数量已达上限"
            return False
        ok = lib.RegisterHotKey(wintypes.HWND(self._hwnd), hotkey_id,
                                wintypes.UINT(native[0]), wintypes.UINT(native[1]))
        if not ok:
            self._status[keys] = _OCCUPIED
            return False
        self._ids[keys] = hotkey_id
        self._by_id[hotkey_id] = keys
        self._status[keys] = "已注册"
        return True

    def _unregister(self, keys: str) -> None:
        hotkey_id = self._ids.pop(keys, None)
        if hotkey_id is None:
            self._status.pop(keys, None)
            return
        self._by_id.pop(hotkey_id, None)
        lib = _user32()
        if lib is not None and self._hwnd:
            lib.UnregisterHotKey(wintypes.HWND(self._hwnd), hotkey_id)
        self._status.pop(keys, None)

    def _alloc_id(self) -> int | None:
        while self._next_id <= _ID_LAST:
            candidate = self._next_id
            self._next_id += 1
            if candidate not in self._by_id:
                return candidate
        return None

    def _dispatch(self, hotkey_id: int) -> None:
        keys = self._by_id.get(hotkey_id)
        if keys:
            self.fired.emit(keys)

    # -- 退出 ------------------------------------------------------------
    def release_all(self) -> None:
        for key in list(self._ids):
            self._unregister(key)
        app = QApplication.instance()
        if app is not None:
            app.removeNativeEventFilter(self._filter)
        self.status_changed.emit()


__all__ = ["HotkeyRegistry"]
