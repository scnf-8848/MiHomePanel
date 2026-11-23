"""程序图标（任务栏 / Alt-Tab / 窗口左上角 / exe 自身）。

图标本体是 ``assets/app.ico``，由 ``scripts/make_icon.py --ico`` 生成。那份脚本里
**每个尺寸是分别画的**（16/24/32 三档换了几何：砍掉屏幕、加粗描边），所以这里只
负责"把文件找到、读出来"，绝不在运行时缩放——拿 256 缩到 16 会把三档细节全抹平，
正是生成脚本一直在躲的糊法。

路径不能只按仓库根算。PyInstaller 打包后代码在 ``_internal``（onedir）或临时解包
目录（onefile），``Path(__file__)`` 那条链直接失效——开发机上好好的，打包版图标
一片空白。候选顺序照抄 ``core/camera.py: search_dirs()`` 那一套：exe 旁边优先
（用户能替换），其次解包目录，最后才是源码树。

**系统通知的图标是另一条路**（2026-11-23 实测）：Win10/11 会把托盘气泡
（``Shell_NotifyIcon`` + ``NIF_INFO``）转成 toast，toast 头部的图标**不是**当前
``QSystemTrayIcon`` 的图标。没有显式 AUMID 时，Windows 按 exe 路径生成
``NotifyIconGeneratedAumid_<hash>``，并在**第一次弹通知时**把当时的托盘图标快照
成 ``%TEMP%\\NotifyIconGeneratedAumid_<hash>.png``，写进
``HKCU\\Software\\Classes\\AppUserModelId\\...`` 的 ``IconUri``。之后换图标不会重拍，
托盘新了、通知一直是旧图。所以这里显式注册自己的 AUMID，把 ``IconUri`` 指向
``assets/app.ico``，每次启动重写一遍（换图标、挪目录都不用手工清缓存）。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon

_cache: QIcon | None = None

#: Windows 通知身份。``[公司].[程序]`` 的形状是微软推荐写法；一旦定下就别改——
#: 改了等于换了一个"应用"，用户在通知设置里的开关和已弹的通知都会另起一份。
AUMID = "scnf8848.MiHomePanel"


def search_dirs() -> list[Path]:
    """图标可能所在的目录，按优先级排。"""
    dirs: list[Path] = []
    if getattr(sys, "frozen", False):
        try:
            dirs.append(Path(sys.executable).resolve().parent)
        except OSError:  # 老 Windows 上 exe 路径解析偶尔会炸
            pass
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        dirs.append(Path(meipass))
    dirs.append(Path(__file__).resolve().parents[2])

    seen: set[str] = set()
    out: list[Path] = []
    for d in dirs:
        # 同一个目录可能在上面出现两次（onefile 且 cwd 就是 exe 所在处）
        key = str(d).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def path() -> Path | None:
    """找到的 `.ico` 全路径；一个都没有就返回 ``None``。"""
    for d in search_dirs():
        p = d / "assets" / "app.ico"
        if p.is_file():
            return p
    return None


def load() -> QIcon:
    """给 ``QApplication.setWindowIcon`` 用。

    找不到文件时返回**空 QIcon** 而不是抛异常：图标缺失只该让任务栏显示默认
    空白图标，不该拦住整个程序启动。
    """
    global _cache
    if _cache is None:
        p = path()
        _cache = QIcon(str(p)) if p is not None else QIcon()
    return _cache


def install_notification_identity(display_name: str) -> bool:
    """把 Windows 通知的应用身份钉到本程序 + 当前 ``assets/app.ico``。

    **必须在建任何窗口 / 托盘图标之前调用**（``app.py: main()`` 里紧跟参数解析），
    并且每次启动都重写一遍 ``HKCU\\Software\\Classes\\AppUserModelId\\<AUMID>``：
    ``IconUri`` 取本次实际找到的 ICO 绝对路径，这样换图标、换目录之后系统通知也
    跟着换，不必让用户去清 ``%TEMP%`` 里那份旧的 AUMID 快照。

    源码运行与打包版走同一套：``path()`` 已经处理了 ``_internal`` / 源码树两种
    落点；打包不需要额外资源，只要 ``assets/app.ico`` 被打进 ``--add-data``
    （``build_exe.py`` 默认就这么做）。

    返回是否注册成功。失败一律吞掉——通知图标不对不该拦住程序启动，调用方把
    返回值写进日志即可。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        import winreg
        from ctypes import wintypes

        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        set_aumid = shell32.SetCurrentProcessExplicitAppUserModelID
        set_aumid.argtypes = [wintypes.LPCWSTR]
        set_aumid.restype = ctypes.c_long  # HRESULT，0 == S_OK
        if set_aumid(AUMID) != 0:
            return False

        p = path()
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Classes\AppUserModelId\{AUMID}",
            0,
            winreg.KEY_WRITE,
        ) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, display_name)
            if p is not None:
                winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(p))
        return True
    except (OSError, AttributeError, ValueError):
        return False


__all__ = ["AUMID", "install_notification_identity", "load", "path", "search_dirs"]
