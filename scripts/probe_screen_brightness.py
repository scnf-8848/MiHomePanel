# SPDX-License-Identifier: GPL-3.0-or-later
"""只读探测：这台电脑的**屏幕亮度**能从哪条路读到。

背景与结论见 ``docs/reference/pc-brightness-follow.md``。给「灯跟随屏幕亮度」这个
场景做前置判断：读不到亮度，后面的一切都无从谈起。

三条路各覆盖不同硬件，**没有一条是通用的**：

* **WMI** ``root\\wmi:WmiMonitorBrightness`` —— 笔记本内置屏（ACPI 背光）通常有，
  台式机 / 纯外接屏通常整个类都不存在；
* **DDC/CI**（``dxva2.dll``）—— 外接显示器走 I2C 读 VCP 0x10，要求显示器 OSD 里
  开着 DDC/CI，且线材 / 转接不砍 DDC；
* **电源方案** ``VIDEONORMALLEVEL``（``powrprof.dll``）—— 存的是「系统设置的亮度」，
  笔记本上跟着亮度键走，台式机上多数是死值。

只读保证：不设置任何亮度、不写文件、不发米家请求。``--watch`` 只是重复读同一个值，
用来判断「这个值会不会动」（死值的典型表现是一直不变）。

    .venv/Scripts/python.exe -X utf8 scripts/probe_screen_brightness.py
    .venv/Scripts/python.exe -X utf8 scripts/probe_screen_brightness.py --watch

``[OK]`` = 读到了；``[--]`` = 接口在但本机没有；``[!!]`` = 调用失败（附错误码）。
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

if sys.platform != "win32":
    raise SystemExit("这个探针只能在 Windows 上跑")

#: 显示子组 / 「显示器亮度」设置（和 powercfg 里 SUB_VIDEO / ADED5E82 是同一对）
GUID_VIDEO_SUBGROUP = "7516b95f-f776-4464-8c53-06167f40cc99"
GUID_VIDEO_BRIGHTNESS = "aded5e82-b909-4619-9949-f5d71dac0bcb"


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [
        ("hPhysicalMonitor", wintypes.HANDLE),
        ("szPhysicalMonitorDescription", wintypes.WCHAR * 128),
    ]


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


def guid_of(text: str) -> GUID:
    head, tail = text.split("-")[0], text.replace("-", "")[8:]
    return GUID(int(head, 16),
                int(tail[0:4], 16), int(tail[4:8], 16),
                (ctypes.c_ubyte * 8)(*bytes.fromhex(tail[8:])))


def hr(code: int) -> str:
    return f"0x{code & 0xFFFFFFFF:08X}"


def battery_count() -> int:
    """有电池基本就是笔记本；没有就是台式机。只查设备类，不改任何东西。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue | Measure-Object).Count"],
            capture_output=True, text=True, encoding="gbk", errors="replace", timeout=20)
        return int((out.stdout or "0").strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def probe_power_scheme() -> tuple[int | None, str]:
    """电源方案里的「显示器亮度」。返回 (值, 说明)；值为 None 表示读不到。"""
    powrprof = ctypes.WinDLL("powrprof", use_last_error=True)
    powrprof.PowerGetActiveScheme.argtypes = [wintypes.HKEY, ctypes.POINTER(ctypes.POINTER(GUID))]
    powrprof.PowerReadACValueIndex.argtypes = [
        wintypes.HKEY, ctypes.POINTER(GUID), ctypes.POINTER(GUID),
        ctypes.POINTER(GUID), ctypes.POINTER(wintypes.DWORD)]
    scheme = ctypes.POINTER(GUID)()
    if powrprof.PowerGetActiveScheme(None, ctypes.byref(scheme)) != 0 or not scheme:
        return None, f"取当前电源方案失败 err={hr(ctypes.get_last_error())}"
    sub, setting = guid_of(GUID_VIDEO_SUBGROUP), guid_of(GUID_VIDEO_BRIGHTNESS)
    value = wintypes.DWORD(0)
    rc = powrprof.PowerReadACValueIndex(
        None, scheme, ctypes.byref(sub), ctypes.byref(setting), ctypes.byref(value))
    if rc != 0:
        return None, f"读「显示器亮度」失败 rc={hr(rc)}"
    return int(value.value), "电源方案"


def probe_wmi() -> str:
    """WMI 的 WmiMonitorBrightness。类不存在 = 本机这条路不通。"""
    script = (
        "Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightness "
        "-ErrorAction Stop | ForEach-Object { "
        "'{0}|{1}' -f $_.CurrentBrightness, $_.InstanceName }"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                             capture_output=True, text=True, encoding="gbk", errors="replace", timeout=25)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"[!!] WMI WmiMonitorBrightness 调用失败：{exc}"
    text = (out.stdout or "").strip()
    if out.returncode == 0 and text:
        return "[OK] WMI WmiMonitorBrightness：" + "；".join(
            f"亮度 {line.split('|')[0]}%（{line.split('|')[-1]}）" for line in text.splitlines())
    err = (out.stderr or "").strip().splitlines()
    detail = err[0] if err else "本机不支持该类"
    return f"[--] WMI WmiMonitorBrightness：{detail}（笔记本内置屏通常有，台式机常整类不存在）"


def probe_ddc() -> list[str]:
    """外接显示器的 DDC/CI：能力查询 + VCP 0x10 亮度。"""
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)
    dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [
        wintypes.HMONITOR, ctypes.POINTER(wintypes.DWORD)]
    dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = [
        wintypes.HMONITOR, wintypes.DWORD, ctypes.POINTER(PHYSICAL_MONITOR)]
    dxva2.GetMonitorCapabilities.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
    dxva2.GetVCPFeatureAndVCPFeatureReply.argtypes = [
        wintypes.HANDLE, ctypes.c_byte, ctypes.POINTER(ctypes.c_byte),
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
    dxva2.DestroyPhysicalMonitor.argtypes = [wintypes.HANDLE]

    lines: list[str] = []
    proc_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
        ctypes.POINTER(RECT), wintypes.LPARAM)

    def callback(hmon, _hdc, _rect, _data):
        count = wintypes.DWORD(0)
        if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmon, ctypes.byref(count)) \
                or not count.value:
            lines.append("[--] DDC/CI：这个 HMONITOR 拿不到物理显示器（虚拟显示器常见）")
            return True
        arr = (PHYSICAL_MONITOR * count.value)()
        if not dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, count.value, arr):
            lines.append(f"[!!] DDC/CI 取物理显示器失败 err={hr(ctypes.get_last_error())}")
            return True
        for i in range(count.value):
            handle, desc = arr[i].hPhysicalMonitor, arr[i].szPhysicalMonitorDescription
            caps, color = wintypes.DWORD(0), wintypes.DWORD(0)
            ctypes.set_last_error(0)
            if dxva2.GetMonitorCapabilities(handle, ctypes.byref(caps), ctypes.byref(color)):
                lines.append(f"[OK] DDC/CI {desc}：能力 caps=0x{caps.value:08X}")
            else:
                lines.append(f"[!!] DDC/CI {desc} 能力查询失败 err={hr(ctypes.get_last_error())}"
                             "（0xC0262582 = I2C 传输失败，整条 DDC 通路没通；"
                             "不是某个值不支持，那会是 0xC0262584）")
            kind = ctypes.c_byte(0)
            cur, mx = wintypes.DWORD(0), wintypes.DWORD(0)
            ctypes.set_last_error(0)
            if dxva2.GetVCPFeatureAndVCPFeatureReply(
                    handle, 0x10, ctypes.byref(kind), ctypes.byref(cur), ctypes.byref(mx)):
                lines.append(f"[OK] DDC/CI {desc} 亮度 VCP 0x10：{cur.value} / {mx.value}")
            else:
                lines.append(f"[!!] DDC/CI {desc} 亮度 VCP 0x10 失败 "
                             f"err={hr(ctypes.get_last_error())}")
            dxva2.DestroyPhysicalMonitor(handle)
        return True

    if not u32.EnumDisplayMonitors(None, None, proc_type(callback), 0):
        lines.append(f"[!!] 枚举显示器失败 err={hr(ctypes.get_last_error())}")
    return lines or ["[--] 没有枚举到任何显示器"]


def probe_monitors() -> list[str]:
    """型号 / 接口 / 分辨率：判断"该走哪条路"的旁证。"""
    script = (
        "$id = Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorID -ErrorAction SilentlyContinue;"
        "$cn = Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorConnectionParams "
        "-ErrorAction SilentlyContinue;"
        "foreach ($m in $id) { $n = ($m.UserFriendlyName | Where-Object {$_ -ne 0} | "
        "ForEach-Object {[char]$_}) -join ''; "
        "$c = ($cn | Where-Object { $_.InstanceName -eq $m.InstanceName }).VideoOutputTechnology; "
        "'{0}|{1}|{2}' -f $m.InstanceName, $n, $c }"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                             capture_output=True, text=True, encoding="gbk", errors="replace", timeout=25)
    except (OSError, subprocess.SubprocessError):
        return []
    kinds = {0: "未知", 4: "DVI", 5: "HDMI", 10: "DisplayPort 外接", 11: "DisplayPort 内置"}
    lines = []
    for line in (out.stdout or "").strip().splitlines():
        parts = line.split("|")
        if len(parts) != 3:
            continue
        tech = kinds.get(int(parts[2]) if parts[2].isdigit() else 0, parts[2])
        lines.append(f"    显示器：{parts[0]} 型号 {parts[1] or '?'} 接口 {tech}")
    return lines


def main() -> int:
    watch = "--watch" in sys.argv[1:]
    print("=== 屏幕亮度来源探测（只读）===\n")
    for line in probe_monitors():
        print(line)
    print(f"    电池：{battery_count()} 块"
          f"（0 = 台式机，笔记本才有 ACPI 背光那条路）\n")

    print(probe_wmi())
    for line in probe_ddc():
        print(line)
    value, why = probe_power_scheme()
    if value is None:
        print(f"[!!] 电源方案 VIDEONORMALLEVEL：{why}")
    else:
        print(f"[OK] 电源方案 VIDEONORMALLEVEL：{value}%（0–100；{why}。"
              "台式机上多为死值，别只凭它判断）")

    if watch:
        print("\n=== 10 秒采样（现在去按亮度键 / 拨 OSD）===")
        samples = []
        for _ in range(20):
            v, _why = probe_power_scheme()
            samples.append(v)
            time.sleep(0.5)
        changed = len({v for v in samples if v is not None}) > 1
        print(f"    电源方案采样：{samples}")
        print(f"    会不会动：{'会（这条值可信）' if changed else '没动过（要么没调，要么是死值）'}")

    print("\n=== 结论 ===")
    print("    上面三条路里只要有一条 [OK] 且值会跟着调，这个场景才有得做；")
    print("    全 [--]/[!!] 就别在触发侧花时间了，见 docs/reference/pc-brightness-follow.md。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
