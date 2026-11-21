# SPDX-License-Identifier: GPL-3.0-or-later
"""启动现场：把"这个进程以什么身份跑、能不能写数据目录"落一份小报告。

为什么不用 ``logs/``——这份报告存在的意义**正是**"数据目录可能写不进去"。
2026-11-21 的现场：普通权限双击 exe 弹了「设置无法保存」，而 ``logs/prefs.log``
里一条失败记录都没有，因为日志本身也落在那个目录。``%TEMP%`` 是最后一个几乎
总能写的地方，把现场放这儿才有东西可看。

报告是**追加**的，每次启动一段：这样"这次改了设置 → 重启 → 又没了"这种
跨启动的现象能并排比。每段不超过二十行，长不到哪去。

它只读环境与令牌，不碰任何窗口，也不会在写失败时抛异常——诊断自己不该
成为新的故障源。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

#: 报告文件名。放在 ``%TEMP%`` 根下而不是子目录：子目录也可能建不出来。
REPORT_NAME = "MiHomePanel-diag.txt"


def _identity() -> list[str]:
    """进程身份四件事：账户、完整性级别、提权、AppContainer。

    "写不进去"最常见的原因就是这四条里的一条——尤其是**完整性级别**：
    被塞进 Low / AppContainer 的进程只能写自己的沙箱目录，写 ``%LOCALAPPDATA%``
    一律"拒绝访问"，看起来和权限设置坏了一模一样。
    """
    if sys.platform != "win32":
        return [f"平台 {sys.platform}，跳过令牌检查"]
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]

    class TOKEN_USER(ctypes.Structure):
        _fields_ = [("User", SID_AND_ATTRIBUTES)]

    class TOKEN_MANDATORY_LABEL(ctypes.Structure):
        _fields_ = [("Label", SID_AND_ATTRIBUTES)]

    advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    advapi32.GetSidSubAuthorityCount.argtypes = [wintypes.LPVOID]
    advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    advapi32.GetSidSubAuthority.argtypes = [wintypes.LPVOID, wintypes.DWORD]
    # 64 位句柄必须显式声明，否则 GetCurrentProcess() 的 -1 被截成 32 位，
    # OpenProcessToken 直接回 ERROR_INVALID_HANDLE(6)——第一次就踩了这个坑。
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)  # TOKEN_QUERY
    ):
        return [f"OpenProcessToken 失败，last_error={ctypes.get_last_error()}"]

    out: list[str] = []

    def _info(cls: int) -> ctypes.Array | None:
        need = wintypes.DWORD()
        advapi32.GetTokenInformation(token, cls, None, 0, ctypes.byref(need))
        if not need.value:
            return None
        buf = ctypes.create_string_buffer(need.value)
        if not advapi32.GetTokenInformation(token, cls, buf, need, ctypes.byref(need)):
            return None
        return buf

    buf = _info(1)  # TokenUser
    if buf is not None:
        user = ctypes.cast(buf, ctypes.POINTER(TOKEN_USER)).contents
        text = wintypes.LPWSTR()
        if advapi32.ConvertSidToStringSidW(user.User.Sid, ctypes.byref(text)) and text.value:
            out.append(f"SID={text.value}")
    out.append(f"USERNAME={os.environ.get('USERNAME')}  USERDOMAIN={os.environ.get('USERDOMAIN')}")

    buf = _info(25)  # TokenIntegrityLevel
    if buf is not None:
        label = ctypes.cast(buf, ctypes.POINTER(TOKEN_MANDATORY_LABEL)).contents
        count = advapi32.GetSidSubAuthorityCount(label.Label.Sid).contents.value
        rid = advapi32.GetSidSubAuthority(label.Label.Sid, count - 1).contents.value
        name = {
            0x0000: "Untrusted",
            0x1000: "Low",
            0x2000: "Medium",
            0x2100: "MediumPlus",
            0x3000: "High",
            0x4000: "System",
        }.get(rid, hex(rid))
        out.append(f"完整性级别={name}(0x{rid:04x})")

    value = wintypes.DWORD()
    need = wintypes.DWORD()
    if advapi32.GetTokenInformation(
        token, 29, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(need)  # AppContainer
    ):
        out.append(f"AppContainer={'是' if value.value else '否'}")
    if advapi32.GetTokenInformation(
        token, 20, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(need)  # Elevation
    ):
        out.append(f"已提权={'是' if value.value else '否'}")

    kernel32.CloseHandle(token)
    return out


def integrity_level() -> str:
    """进程完整性级别（``"Low"`` / ``"Medium"`` / ``"High"`` …），取不到返回空串。

    单独给一条是因为**这一个值就是 2026-11-21 全部症状的根因**：exe 所在目录
    带 Low 标签 → Windows 以 Low 启动它 → 写不了 ``%LOCALAPPDATA%``（设置存不住）
    、发不了窗口消息给 explorer（托盘挂不上）。知道它是 Low，就不用再查杀软了。
    """
    if sys.platform != "win32":
        return ""
    try:
        for line in _identity():
            if line.startswith("完整性级别="):
                return line.split("=", 1)[1].split("(")[0].strip()
    except Exception:  # noqa: BLE001 - 诊断失败不当故障
        return ""
    return ""


def low_integrity() -> bool:
    """是不是被 Windows 以降级（Low）身份跑起来了。"""
    return integrity_level() == "Low"


def identity_summary() -> str:
    """"Medium，未提权，非 AppContainer" 这样的一行，给弹窗用。

    文件可能一条也落不下去（2026-11-21：exe 连 ``%TEMP%`` 都写不进去），
    那就得让用户截个图就能把现场带走。这一行比二十行报告更适合看图。
    """
    try:
        parts = [
            line.split("=", 1)[1]
            for line in _identity()
            if line.startswith(("完整性级别=", "AppContainer=", "已提权="))
        ]
    except Exception:  # noqa: BLE001 - 诊断失败不该影响弹窗
        return "（取不到进程身份）"
    return "，".join(parts) if parts else "（取不到进程身份）"


def write_report(extra: dict[str, str] | None = None) -> Path | None:
    """追加一段现场到 ``%TEMP%/MiHomePanel-diag.txt``，返回报告路径（写不了返回 None）。"""
    lines = [f"===== {time.strftime('%Y-%m-%d %H:%M:%S')}  pid={os.getpid()} ====="]
    lines.append(f"frozen={getattr(sys, 'frozen', False)}  python={sys.version.split()[0]}")
    lines.append(f"argv={' '.join(sys.argv[1:]) or '(无参数)'}")
    lines.append(f"cwd={os.getcwd()}")
    for key in ("LOCALAPPDATA", "APPDATA", "TEMP", "MIHOME_DATA_DIR", "MIHOME_NO_TRAY"):
        value = os.environ.get(key)
        lines.append(f"{key}={value if value else '(未设置)'}")
    try:
        lines.extend(_identity())
    except Exception as exc:  # noqa: BLE001 - 诊断自己绝不能把启动带崩
        lines.append(f"令牌检查失败：{type(exc).__name__}: {exc}")
    if extra:
        lines.extend(f"{k}={v}" for k, v in extra.items())

    path = Path(os.environ.get("TEMP") or ".") / REPORT_NAME
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n\n")
    except OSError:
        return None
    return path
