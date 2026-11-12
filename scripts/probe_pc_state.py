# SPDX-License-Identifier: GPL-3.0-or-later
"""只读探测：这台 Windows 电脑上，**纯系统级 API** 能拿到哪些「电脑状态」。

背景与结论见 ``docs/reference/pc-state-api.md``。用途是给「电脑自动化」规则里的
条件块判断可行性：哪些状态用 ctypes 调 Win32 就能读、哪些必须外接第三方程序。

只读保证：不注册热键、不锁屏、不改音量、不发米家请求、不写文件；
结果只打印到终端。**不 import 任何第三方库**（项目打包体积敏感）。

    .venv/Scripts/python.exe -X utf8 scripts/probe_pc_state.py

``[OK]`` = 拿到了；``[--]`` = 接口存在但本机无此硬件 / 无数据；
``[!!]`` = 调用失败（附 GetLastError）。
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from datetime import datetime

if sys.platform != "win32":
    raise SystemExit("这个探针只能在 Windows 上跑")

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
u32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
setupapi = ctypes.WinDLL("setupapi", use_last_error=True)

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TH32CS_SNAPPROCESS = 0x00000002
_DESKTOP_READOBJECTS = 0x00000001
_UOI_NAME = 2
_DIGCF_PRESENT = 0x00000002
_DIGCF_DEVICEINTERFACE = 0x00000010
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


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
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", ctypes.c_void_p),
    ]


# EMI（Energy Meter Interface）设备接口 GUID：
# {45BD8344-7ED6-49CF-A440-C276C933B053}
GUID_DEVICE_ENERGY_METER = GUID(
    0x45BD8344,
    0x7ED6,
    0x49CF,
    (ctypes.c_ubyte * 8)(0xA4, 0x40, 0xC2, 0x76, 0xC9, 0x33, 0xB0, 0x53),
)


def _err() -> str:
    return f"GetLastError={ctypes.get_last_error()}"


def _line(tag: str, name: str, value: str) -> None:
    print(f"  [{tag:2}] {name:<22} {value}")


def _ft_seconds(ft: FILETIME) -> float:
    """FILETIME（1601 起 100ns）→ Unix 秒。"""
    raw = (ft.dwHighDateTime << 32) | ft.dwLowDateTime
    return (raw - 116444736000000000) / 1e7


# -- 1. 基础环境 -----------------------------------------------------------


def probe_basics() -> None:
    v = sys.getwindowsversion()
    k32.GetTickCount64.restype = ctypes.c_ulonglong
    up_h = k32.GetTickCount64() / 1000 / 3600
    _line("OK", "系统版本", f"Windows {v.major}.{v.minor} build {v.build}")
    _line("OK", "开机时长", f"{up_h:.1f} 小时（GetTickCount64）")
    _line("OK", "管理员权限", "是" if shell32.IsUserAnAdmin() else "否")
    sid = wintypes.DWORD()
    k32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    k32.ProcessIdToSessionId(k32.GetCurrentProcessId(), ctypes.byref(sid))
    _line("OK", "会话 ID", f"{sid.value}（0 = 服务会话，非交互）")


# -- 2. 空闲时长 -----------------------------------------------------------


def probe_idle() -> None:
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    u32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
    u32.GetLastInputInfo.restype = wintypes.BOOL
    if not u32.GetLastInputInfo(ctypes.byref(lii)):
        _line("!!", "空闲时长", _err())
        return
    # dwTime 是 32 位 tick，GetTickCount64 低 32 位与 GetTickCount 一致
    now = k32.GetTickCount64() & 0xFFFFFFFF
    idle = ((now - lii.dwTime) & 0xFFFFFFFF) / 1000
    _line("OK", "空闲时长", f"{idle:.1f} 秒（GetLastInputInfo，仅当前会话）")


# -- 3. 前台窗口 / 前台进程 / 运行时长 ------------------------------------


def probe_foreground() -> None:
    u32.GetForegroundWindow.restype = wintypes.HWND
    hwnd = u32.GetForegroundWindow()
    if not hwnd:
        _line("--", "前台窗口", "当前没有前台窗口（可能在锁屏 / 无交互桌面）")
        return
    title = ctypes.create_unicode_buffer(512)
    u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u32.GetWindowTextW(hwnd, title, 512)
    pid = wintypes.DWORD()
    u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    _line("OK", "前台窗口标题", title.value or "(空标题)")

    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    proc = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not proc:
        _line("!!", "前台进程", f"OpenProcess 失败 pid={pid.value} {_err()}")
        return
    try:
        size = wintypes.DWORD(1024)
        path = ctypes.create_unicode_buffer(1024)
        k32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        ok = k32.QueryFullProcessImageNameW(proc, 0, path, ctypes.byref(size))
        _line("OK", "前台进程路径", path.value if ok else f"读取失败 {_err()}")
        creation, exit_time, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(FILETIME)] * 4
        if k32.GetProcessTimes(
            proc,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            started = datetime.fromtimestamp(_ft_seconds(creation))
            mins = (time.time() - _ft_seconds(creation)) / 60
            _line(
                "OK",
                "前台进程已运行",
                f"{mins:.1f} 分钟（启动于 {started:%H:%M:%S}，GetProcessTimes）",
            )
    finally:
        k32.CloseHandle(proc)


# -- 4. 进程列表 -----------------------------------------------------------


def probe_processes() -> None:
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    snap = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snap == _INVALID_HANDLE_VALUE or not snap:
        _line("!!", "进程列表", f"快照失败 {_err()}")
        return
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    names: set[str] = set()
    ok = k32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        names.add(entry.szExeFile)
        ok = k32.Process32NextW(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    _line("OK", "进程列表", f"{len(names)} 个不同的 exe 名（按名匹配可做「某应用是否运行」）")
    for probe in ("explorer.exe", "notepad.exe", "python.exe"):
        _line("OK", f"  含 {probe}", "是" if any(n.lower() == probe for n in names) else "否")


# -- 5. 电源 / 电池 --------------------------------------------------------


def probe_power() -> None:
    st = SYSTEM_POWER_STATUS()
    k32.GetSystemPowerStatus.argtypes = [ctypes.POINTER(SYSTEM_POWER_STATUS)]
    k32.GetSystemPowerStatus.restype = wintypes.BOOL
    if not k32.GetSystemPowerStatus(ctypes.byref(st)):
        _line("!!", "电源状态", _err())
        return
    ac = {0: "电池供电", 1: "交流供电", 255: "未知"}.get(st.ACLineStatus, "?")
    pct = st.BatteryLifePercent
    _line("OK", "电源状态", f"{ac}；电量 {pct if pct != 255 else '未知'}%")
    if st.BatteryFlag & 128:
        _line("--", "电池", "本机报告「没有系统电池」（台式机常见）→ 拿不到瓦数")
    life = ctypes.c_long(st.BatteryLifeTime).value
    _line("OK", "剩余续航估计", f"{life} 秒（-1 = 未知 / 交流供电）")
    _line("--", "实时功率(瓦)", "GetSystemPowerStatus 不提供；见下面 EMI 一项")


# -- 6. 锁屏 / 输入桌面 ----------------------------------------------------


def probe_desktop() -> None:
    u32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    u32.OpenInputDesktop.restype = wintypes.HANDLE
    desk = u32.OpenInputDesktop(0, False, _DESKTOP_READOBJECTS)
    if not desk:
        _line("--", "输入桌面", f"打不开（锁屏 / UAC 安全桌面时常见）{_err()}")
        return
    try:
        name = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD()
        u32.GetUserObjectInformationW.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        ok = u32.GetUserObjectInformationW(
            desk, _UOI_NAME, name, ctypes.sizeof(name), ctypes.byref(needed)
        )
        value = name.value if ok else f"读取失败 {_err()}"
        _line("OK", "输入桌面", f"{value}（Default=正常 / Winlogon=锁屏）")
    finally:
        u32.CloseDesktop.argtypes = [wintypes.HANDLE]
        u32.CloseDesktop(desk)


# -- 7. EMI 能耗计量设备（功率的关键判据）----------------------------------


class SP_DEVICE_INTERFACE_DETAIL_DATA_W(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("DevicePath", wintypes.WCHAR * 1)]


# emi.h: CTL_CODE(FILE_DEVICE_UNKNOWN, n, METHOD_BUFFERED, FILE_READ_ACCESS)
# FILE_DEVICE_UNKNOWN=0x22, FILE_READ_ACCESS=1, METHOD_BUFFERED=0
def _emi_ioctl(function: int) -> int:
    return (0x22 << 16) | (1 << 14) | (function << 2)


_IOCTL_GET_VERSION = _emi_ioctl(0)
_IOCTL_GET_METADATA_SIZE = _emi_ioctl(1)
_IOCTL_GET_METADATA = _emi_ioctl(2)
_IOCTL_GET_MEASUREMENT = _emi_ioctl(3)


def _ioctl(handle: int, code: int, out_size: int) -> bytes | None:
    buf = ctypes.create_string_buffer(out_size)
    returned = wintypes.DWORD()
    k32.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    k32.DeviceIoControl.restype = wintypes.BOOL
    ok = k32.DeviceIoControl(
        handle, code, None, 0, buf, out_size, ctypes.byref(returned), None
    )
    return bytes(buf)[: returned.value] if ok else None


def _u16(raw: bytes, offset: int, size: int) -> str:
    return raw[offset : offset + size].decode("utf-16-le", "replace").rstrip("\x00")


def _emi_metadata(raw: bytes, version: int) -> tuple[str, list[str]]:
    """``(设备描述, 通道名列表)``。V1 与 V2 的 metadata 布局完全不同。"""
    if version == 2:
        oem, model = _u16(raw, 0, 32), _u16(raw, 32, 32)
        channels = []
        offset = 68
        while offset + 6 <= len(raw):
            name_size = int.from_bytes(raw[offset + 4 : offset + 6], "little")
            if name_size == 0 or offset + 6 + name_size > len(raw):
                break
            channels.append(_u16(raw, offset + 6, name_size))
            offset += 6 + name_size
        return f"{oem} {model}".strip(), channels
    unit = int.from_bytes(raw[:4], "little")
    oem, model = _u16(raw, 4, 32), _u16(raw, 36, 32)
    name_size = int.from_bytes(raw[70:72], "little")
    return f"{oem} {model} (unit={unit})".strip(), [_u16(raw, 72, name_size)]


def probe_emi() -> None:
    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.POINTER(GUID),
        wintypes.LPCWSTR,
        wintypes.HWND,
        wintypes.DWORD,
    ]
    setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
    hdev = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(GUID_DEVICE_ENERGY_METER),
        None,
        None,
        _DIGCF_PRESENT | _DIGCF_DEVICEINTERFACE,
    )
    if hdev == _INVALID_HANDLE_VALUE or not hdev:
        _line("--", "EMI 能耗计量", f"枚举失败 {_err()}")
        return
    try:
        setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.POINTER(GUID),
            wintypes.DWORD,
            ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
        ]
        setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
        first = None
        count = 0
        while count < 32:
            data = SP_DEVICE_INTERFACE_DATA()
            data.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
            if not setupapi.SetupDiEnumDeviceInterfaces(
                hdev, None, ctypes.byref(GUID_DEVICE_ENERGY_METER), count, ctypes.byref(data)
            ):
                break
            if first is None:
                first = data
            count += 1
        if not first:
            _line("--", "EMI 能耗计量", "本机没有暴露任何 EMI 设备接口 → 拿不到功率")
            return
        _line("OK", "EMI 能耗计量", f"发现 {count} 个设备接口")
        _read_emi_device(hdev, first)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
        setupapi.SetupDiDestroyDeviceInfoList(hdev)


def _read_emi_device(hdev: int, data: SP_DEVICE_INTERFACE_DATA) -> None:
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    needed = wintypes.DWORD()
    setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(data), None, 0, ctypes.byref(needed), None)
    raw = ctypes.create_string_buffer(needed.value or 4096)
    detail = ctypes.cast(raw, ctypes.POINTER(SP_DEVICE_INTERFACE_DETAIL_DATA_W)).contents
    detail.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W)
    if not setupapi.SetupDiGetDeviceInterfaceDetailW(
        hdev, ctypes.byref(data), raw, ctypes.sizeof(raw), ctypes.byref(needed), None
    ):
        _line("!!", "  EMI 设备路径", f"取路径失败 {_err()}")
        return
    path = ctypes.wstring_at(ctypes.addressof(raw) + ctypes.sizeof(wintypes.DWORD))

    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    k32.CreateFileW.restype = wintypes.HANDLE
    handle = k32.CreateFileW(path, 0x80000000, 1, None, 3, 0, None)
    if not handle or handle == _INVALID_HANDLE_VALUE:
        _line("--", "  EMI 设备打开", f"失败（可能需要管理员）{_err()}")
        return
    try:
        ver = _ioctl(handle, _IOCTL_GET_VERSION, 2)
        if not ver or len(ver) < 2:
            _line("--", "  EMI 版本", f"读不到 {_err()}")
            return
        version = int.from_bytes(ver[:2], "little")
        _line("OK", "  EMI 版本", f"V{version}")
        size_raw = _ioctl(handle, _IOCTL_GET_METADATA_SIZE, 4)
        meta_size = int.from_bytes(size_raw[:4], "little") if size_raw else 0
        meta = _ioctl(handle, _IOCTL_GET_METADATA, meta_size or 4096)
        if not meta:
            _line("--", "  EMI 元数据", f"读不到 {_err()}")
            return
        desc, channels = _emi_metadata(meta, version)
        _line("OK", "  计量硬件", f"{desc}；{len(channels)} 个通道：{'、'.join(channels)}")

        # 每个通道 16 字节：AbsoluteEnergy(8) + AbsoluteTime(8)，能量单位 pWh、时间单位 100ns
        meas_size = 16 * max(len(channels), 1)
        a = _ioctl(handle, _IOCTL_GET_MEASUREMENT, meas_size)
        time.sleep(1.0)
        b = _ioctl(handle, _IOCTL_GET_MEASUREMENT, meas_size)
        if not a or not b:
            _line("--", "  能量读数", f"读不到 {_err()}")
            return
        for i in range(min(len(channels), len(a) // 16, len(b) // 16)):
            off = i * 16
            e0 = int.from_bytes(a[off : off + 8], "little")
            t0 = int.from_bytes(a[off + 8 : off + 16], "little")
            e1 = int.from_bytes(b[off : off + 8], "little")
            t1 = int.from_bytes(b[off + 8 : off + 16], "little")
            dt = (t1 - t0) * 1e-7
            # pWh -> W：1 pWh = 1e-12 Wh = 3.6e-9 J
            watts = (e1 - e0) * 3.6e-9 / dt if dt > 0 else 0.0
            _line(
                "OK",
                f"  {channels[i]}",
                f"1 秒平均 {watts:.2f} W（累计能量差 {e1 - e0} pWh，计时差 {dt:.3f}s）",
            )
        _line("--", "  口径", "这是被计量部件（通常 CPU Package）的能耗，不是整机墙上功率")
    finally:
        k32.CloseHandle(handle)


def main() -> int:
    print("Windows 电脑状态：纯系统级 API 只读探测（不发米家请求、不改系统设置）\n")
    for title, fn in (
        ("基础环境", probe_basics),
        ("用户是否在电脑前", probe_idle),
        ("前台在用什么", probe_foreground),
        ("运行了哪些程序", probe_processes),
        ("电源 / 电池 / 功率", probe_power),
        ("锁屏判定", probe_desktop),
        ("整机功率的可能性", probe_emi),
    ):
        print(f"--- {title} ---")
        try:
            fn()
        except Exception as exc:  # noqa: BLE001  单项失败不该中断整份报告
            _line("!!", "探测异常", f"{type(exc).__name__}: {exc}")
        print()
    print("提示：本探针不读取音量 / GPU 占用 / 温度 / 风扇，详见 docs/reference/pc-state-api.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
