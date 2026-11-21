# SPDX-License-Identifier: GPL-3.0-or-later
"""进程触发的采样成本探针（2026-11-19 调研，只读，不写盘、不动设备）。

对比两条零依赖路径：

* 现役 ``process_names()``：``CreateToolhelp32Snapshot`` + 逐条 ``Process32NextW``；
* ``NtQuerySystemInformation(SystemProcessInformation)``：一次调用取回整块结构。

数据落到 ``docs/reference/process-poll-vs-wmi.md``。WMI 事件订阅不在本探针里——
它的实测数据来自微软官方博客（见文档第 4 节），本机不装 COM 也能判定排除。
"""
from __future__ import annotations

import ctypes
import os
import statistics
import sys
import time
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mihome.core.automation.pc_state import process_names  # noqa: E402

SYSTEM_PROCESS_INFORMATION_CLASS = 5
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
STATUS_BUFFER_OVERFLOW = 0x80000005


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", ctypes.c_void_p),
    ]


class SYSTEM_PROCESS_INFORMATION(ctypes.Structure):
    """只定义到 ImageName：后面字段用 NextEntryOffset 跳过，不必逐项声明。"""

    _fields_ = [
        ("NextEntryOffset", wintypes.ULONG),
        ("NumberOfThreads", wintypes.ULONG),
        ("WorkingSetPrivateSize", ctypes.c_longlong),
        ("HardFaultCount", wintypes.ULONG),
        ("NumberOfThreadsHighWatermark", wintypes.ULONG),
        ("CycleTime", ctypes.c_ulonglong),
        ("CreateTime", ctypes.c_longlong),
        ("UserTime", ctypes.c_longlong),
        ("KernelTime", ctypes.c_longlong),
        ("ImageName", UNICODE_STRING),
        ("BasePriority", wintypes.ULONG),
    ]


def nt_process_names() -> set[str]:
    """NtQuerySystemInformation 一次取回 + Python 侧解析。读不到返回空集。"""
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtQuerySystemInformation.argtypes = [
        wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
    ntdll.NtQuerySystemInformation.restype = ctypes.c_long
    size = wintypes.ULONG()
    ntdll.NtQuerySystemInformation(
        SYSTEM_PROCESS_INFORMATION_CLASS, None, 0, ctypes.byref(size))
    buf = None
    for _ in range(4):
        buf = ctypes.create_string_buffer(size.value)
        st = ntdll.NtQuerySystemInformation(
            SYSTEM_PROCESS_INFORMATION_CLASS, buf, size.value, ctypes.byref(size))
        if st == 0:
            break
        if st not in (STATUS_INFO_LENGTH_MISMATCH, STATUS_BUFFER_OVERFLOW):
            return set()
    else:
        return set()
    out: set[str] = set()
    pos, base = 0, ctypes.cast(buf, ctypes.c_void_p).value
    while pos < size.value:
        entry = ctypes.cast(
            base + pos, ctypes.POINTER(SYSTEM_PROCESS_INFORMATION)).contents
        name = entry.ImageName
        if name.Buffer and name.Length:
            text = ctypes.wstring_at(name.Buffer, name.Length // 2)
            if text:
                out.add(text.lower())
        step = entry.NextEntryOffset
        if not step:
            break
        pos += step
    return out


def bench(label: str, fn, n: int = 300) -> None:
    for _ in range(10):  # 预热，排除首次导入 / DLL 加载
        fn()
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000)
    ordered = sorted(samples)
    print(f"{label} x{n}: 平均 {statistics.mean(samples):.3f} ms, "
          f"中位 {statistics.median(samples):.3f} ms, "
          f"P95 {ordered[int(n * 0.95)]:.3f} ms, 最大 {max(samples):.3f} ms")


def main() -> None:
    if sys.platform != "win32":
        print("只在 Windows 上有意义")
        return
    a, b = process_names(), nt_process_names()
    print(f"Toolhelp32 {len(a)} 名 / NtQuery {len(b)} 名；"
          f"仅 Toolhelp 有 {sorted(a - b)[:5]}，仅 NtQuery 有 {sorted(b - a)[:5]}")
    bench("Toolhelp32", process_names)
    bench("NtQuery   ", nt_process_names)
    print("每秒一次 ≈ 平均毫秒数 / 10 = 占单核百分比")


if __name__ == "__main__":
    main()
