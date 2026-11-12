# SPDX-License-Identifier: GPL-3.0-or-later
"""电脑状态快照 + 条件求值（纯 ``ctypes`` 调 Win32，不装任何第三方库）。

能力边界由 ``docs/reference/pc-state-api.md`` 实测给出：进程 / 前台窗口 / 空闲 /
锁屏 / 电源都能读，且**不需要管理员**；CPU 封装功率（EMI）虽能读但 **v1 明确不做**；
整机墙上功率没有通用系统 API；温度 / 风扇要 Ring 0 驱动，建议永不引入。

设计上两点：

* **只在触发瞬间 read 一次**，不另起 timer、不加轮询、不发云端请求；
* ``Snapshot`` 是纯数据，验证脚本可以塞假快照进来，不必真的在 Windows 上跑。
  非 Windows 上 ``available=False``，界面把相关条件灰掉而不是静默判 false。

**条件不可用 ≠ 条件不成立**：台式机没有电池时「电量 ≥ 50」不该被当成 false 骗用户，
``availability()`` 给出原因，由界面置灰并在日志里写清楚。
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Iterable

from .rules import Condition

_IS_WINDOWS = sys.platform == "win32"

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TH32CS_SNAPPROCESS = 0x00000002
_DESKTOP_READOBJECTS = 0x00000001
_UOI_NAME = 2
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


@dataclass(frozen=True, slots=True)
class Snapshot:
    """一次系统状态采样。所有 ``None`` 都表示「本机拿不到」，不是「否」。"""

    available: bool = False
    reason: str = ""
    processes: frozenset[str] = field(default_factory=frozenset)
    foreground_exe: str = ""
    foreground_title: str = ""
    idle_seconds: float = 0.0
    locked: bool | None = None
    ac: bool | None = None
    battery_percent: int | None = None
    has_battery: bool = False


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_byte),
        ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_byte),
        ("SystemStatusFlag", ctypes.c_byte),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
    ]


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


@lru_cache(maxsize=1)
def _libs() -> tuple[Any, Any] | None:
    """加载 kernel32 / user32。**显式设 restype / argtypes**：x64 上 HANDLE 是 8 字节，
    不设会被 ctypes 按 c_int 截断（``core/single_instance.py`` 踩过这个坑）。"""
    if not _IS_WINDOWS:
        return None
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u32 = ctypes.WinDLL("user32", use_last_error=True)
    except OSError:
        return None
    k32.GetTickCount64.restype = ctypes.c_ulonglong
    k32.GetTickCount64.argtypes = []
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.restype = wintypes.BOOL
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.restype = wintypes.BOOL
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetProcessTimes.restype = wintypes.BOOL
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(FILETIME)] * 4
    k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    k32.GetSystemPowerStatus.restype = wintypes.BOOL
    k32.GetSystemPowerStatus.argtypes = [ctypes.POINTER(SYSTEM_POWER_STATUS)]
    u32.GetLastInputInfo.restype = wintypes.BOOL
    u32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
    u32.GetForegroundWindow.restype = wintypes.HWND
    u32.GetForegroundWindow.argtypes = []
    u32.GetWindowTextW.restype = ctypes.c_int
    u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u32.GetWindowThreadProcessId.restype = wintypes.DWORD
    u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u32.OpenInputDesktop.restype = wintypes.HANDLE
    u32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    u32.GetUserObjectInformationW.restype = wintypes.BOOL
    u32.GetUserObjectInformationW.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    u32.CloseDesktop.restype = wintypes.BOOL
    u32.CloseDesktop.argtypes = [wintypes.HANDLE]
    return k32, u32


def _pids_by_name(k32: Any) -> dict[str, list[int]]:
    snap = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snap == _INVALID_HANDLE_VALUE or not snap:
        return {}
    out: dict[str, list[int]] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.setdefault(entry.szExeFile.lower(), []).append(int(entry.th32ProcessID))
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def _names(name: str) -> set[str]:
    """用户可能填 ``Notepad`` 或 ``notepad.exe``，两种都认。"""
    raw = name.strip().lower()
    return {raw, raw + ".exe"} if not raw.endswith(".exe") else {raw, raw[:-4]}


def _ft_seconds(ft: FILETIME) -> float:
    raw = (ft.dwHighDateTime << 32) | ft.dwLowDateTime
    return (raw - 116444736000000000) / 1e7


class PcState:
    """默认的状态提供者。每个 ``read()`` 都是一次即时采样，不缓存。"""

    def read(self) -> Snapshot:
        libs = _libs()
        if libs is None:
            return Snapshot(False, "不是 Windows，读不到电脑状态")
        k32, u32 = libs
        try:
            pids = _pids_by_name(k32)
        except Exception:  # noqa: BLE001  快照失败不该让整条规则崩
            pids = {}
        snap = Snapshot(
            available=True,
            processes=frozenset(pids),
            idle_seconds=self._idle(k32, u32),
        )
        exe, title = self._foreground(k32, u32)
        locked = self._locked(u32)
        ac, pct, has_battery = self._power(k32)
        return Snapshot(
            available=True, processes=frozenset(pids),
            foreground_exe=exe, foreground_title=title,
            idle_seconds=snap.idle_seconds, locked=locked,
            ac=ac, battery_percent=pct, has_battery=has_battery,
        )

    def process_elapsed_minutes(self, name: str) -> float | None:
        """某进程启动至今多少分钟。取不到返回 ``None``（不是 0）。"""
        libs = _libs()
        if libs is None:
            return None
        k32, _ = libs
        try:
            pids = _pids_by_name(k32)
        except Exception:  # noqa: BLE001
            return None
        wanted = _names(name)
        pid = next((v[0] for k, v in pids.items() if k in wanted and v), None)
        if pid is None:
            return None
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            creation, exit_time, kernel, user = (FILETIME(), FILETIME(), FILETIME(), FILETIME())
            if not k32.GetProcessTimes(handle, ctypes.byref(creation),
                                       ctypes.byref(exit_time), ctypes.byref(kernel),
                                       ctypes.byref(user)):
                return None
            return max(0.0, (time.time() - _ft_seconds(creation)) / 60)
        finally:
            k32.CloseHandle(handle)

    # -- 单项 ------------------------------------------------------------
    @staticmethod
    def _idle(k32: Any, u32: Any) -> float:
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not u32.GetLastInputInfo(ctypes.byref(lii)):
            return 0.0
        now = k32.GetTickCount64() & 0xFFFFFFFF
        return ((now - lii.dwTime) & 0xFFFFFFFF) / 1000

    @staticmethod
    def _foreground(k32: Any, u32: Any) -> tuple[str, str]:
        hwnd = u32.GetForegroundWindow()
        if not hwnd:
            return "", ""
        title = ctypes.create_unicode_buffer(512)
        u32.GetWindowTextW(hwnd, title, 512)
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return "", title.value
        try:
            size = wintypes.DWORD(1024)
            path = ctypes.create_unicode_buffer(1024)
            if k32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                return path.value.rsplit("\\", 1)[-1].lower(), title.value
        finally:
            k32.CloseHandle(handle)
        return "", title.value

    @staticmethod
    def _locked(u32: Any) -> bool | None:
        """``OpenInputDesktop`` 打不开 = 锁屏 / UAC 安全桌面 → 保守当作已锁。"""
        desk = u32.OpenInputDesktop(0, False, _DESKTOP_READOBJECTS)
        if not desk:
            return True
        try:
            name = ctypes.create_unicode_buffer(256)
            needed = wintypes.DWORD()
            if not u32.GetUserObjectInformationW(
                    desk, _UOI_NAME, name, ctypes.sizeof(name), ctypes.byref(needed)):
                return None
            return name.value != "Default"
        finally:
            u32.CloseDesktop(desk)

    @staticmethod
    def _power(k32: Any) -> tuple[bool | None, int | None, bool]:
        st = SYSTEM_POWER_STATUS()
        if not k32.GetSystemPowerStatus(ctypes.byref(st)):
            return None, None, False
        ac = {0: False, 1: True}.get(st.ACLineStatus)
        has_battery = not bool(st.BatteryFlag & 128)
        pct = st.BatteryLifePercent if 0 <= st.BatteryLifePercent <= 100 else None
        return ac, pct if has_battery else None, has_battery


# ---------------------------------------------------------------------------
# 条件求值
# ---------------------------------------------------------------------------

def _bool_arg(text: str) -> bool:
    return text.strip().lower() in ("true", "是", "yes", "1", "on")


def _number(text: str) -> float | None:
    try:
        return float(text.strip())
    except (TypeError, ValueError):
        return None


def availability(cond: Condition, snap: Snapshot) -> str:
    """这条条件在当前机器上**能不能判断**；空串 = 能。给界面置灰用。"""
    if not snap.available:
        return snap.reason or "读不到电脑状态"
    if cond.type == "locked" and snap.locked is None:
        return "本机判断不了锁屏状态"
    if cond.type == "power":
        if cond.op == "is" and (cond.arg.strip().lower() == "battery" and not snap.has_battery):
            return "本机没有电池"
        if cond.op in ("ge", "le") and (not snap.has_battery or snap.battery_percent is None):
            return "本机没有电池，电量条件不可用"
        if cond.op == "is" and cond.arg.strip().lower() == "ac" and snap.ac is None:
            return "读不到供电方式"
    return ""


def evaluate_condition(
    cond: Condition,
    snap: Snapshot,
    elapsed: Callable[[str], float | None] | None = None,
) -> tuple[bool, str]:
    """返回 ``(成立?, 说明)``。说明会进触发日志，别只给布尔值。"""
    reason = availability(cond, snap)
    if reason:
        return False, f"条件不可用：{reason}"
    arg = cond.arg.strip()
    if cond.type == "process_running":
        hit = bool(_names(arg) & snap.processes)
        ok = hit if cond.op == "is" else not hit
        return ok, f"{arg} {'正在运行' if hit else '没在运行'}"
    if cond.type == "process_elapsed":
        mins = elapsed(cond.target) if elapsed else None
        want = _number(arg)
        if mins is None or want is None:
            return False, f"取不到 {cond.target} 的运行时长"
        ok = mins >= want if cond.op == "ge" else mins <= want
        return ok, f"{cond.target} 已运行 {mins:.1f} 分钟（{cond.op} {want:g}）"
    if cond.type == "foreground":
        low = arg.lower()
        hit = (low in snap.foreground_exe.lower() or low in snap.foreground_title.lower())
        if cond.op == "equals":
            hit = (snap.foreground_exe.lower() == low
                   or snap.foreground_title.lower() == low)
        return hit, f"前台：{snap.foreground_exe or '(未知)'} / {snap.foreground_title or '(无标题)'}"
    if cond.type == "idle_minutes":
        want = _number(arg)
        mins = snap.idle_seconds / 60
        if want is None:
            return False, "空闲分钟数填的不是数字"
        ok = mins >= want if cond.op == "ge" else mins <= want
        return ok, f"空闲 {mins:.1f} 分钟（{cond.op} {want:g}）"
    if cond.type == "locked":
        want = _bool_arg(arg)
        ok = (snap.locked is True) == want
        return ok, f"锁屏 = {'是' if snap.locked else '否'}"
    if cond.type == "power":
        if cond.op == "is":
            want = arg.lower() == "ac"
            return snap.ac is want, f"供电 = {'交流' if snap.ac else '电池'}"
        want_n = _number(arg)
        if want_n is None or snap.battery_percent is None:
            return False, "电量填的不是数字或读不到电量"
        ok = (snap.battery_percent >= want_n if cond.op == "ge"
              else snap.battery_percent <= want_n)
        return ok, f"电量 {snap.battery_percent}%（{cond.op} {want_n:g}）"
    return False, f"未知条件类型 {cond.type}"


def evaluate_conditions(
    conditions: Iterable[Condition],
    mode: str,
    snap: Snapshot,
    elapsed: Callable[[str], float | None] | None = None,
) -> tuple[bool, list[str]]:
    """空条件 = 无条件（True）。AND 短路、OR 短路；说明逐条带回来给日志。"""
    results = [evaluate_condition(c, snap, elapsed) for c in conditions]
    notes = [f"{'✓' if ok else '✗'} {note}" for ok, note in results]
    if not results:
        return True, ["无条件限制"]
    if mode == "any":
        return any(ok for ok, _ in results), notes
    return all(ok for ok, _ in results), notes


__all__ = [
    "PcState",
    "Snapshot",
    "availability",
    "evaluate_condition",
    "evaluate_conditions",
]
