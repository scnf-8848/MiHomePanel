# SPDX-License-Identifier: GPL-3.0-or-later
"""开机启动：在 `HKCU\\...\\CurrentVersion\\Run` 里写一行，登录 Windows 后自动打开。

**只写 HKCU，不碰 HKLM**：不需要管理员、不弹 UAC，用户随时能在"任务管理器 →
启动"页里自己禁掉——绿色版没有卸载器，可控比"装上了就走不掉"重要。

**只在打包后的 exe 上开放**：源码运行得写 `pythonw.exe -m mihome` 外加工作目录，
venv 一换路径就废。开发态 `supported()` 为假，设置页把开关置灰并说明原因。

值长这样（引号是必须的，路径常带空格）：:

    "C:\\...\\MiHomePanel.exe" --autostart

`--autostart` 让程序知道自己是被系统拉起来的：窗口最小化出现，不抢用户正在看的
东西（见 `app.main`）。

**路径会变**：重新打包到别的目录、`dist/` 改名，旧值就指向一个不存在的 exe，
下次登录会弹"找不到文件"。所以每次启动都比一次（`heal()`），指向不是当前 exe
就改写成当前路径——但**只在该功能本来就开着的时候**动手，绝不凭空创建。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

if sys.platform == "win32":
    import winreg
else:  # pragma: no cover - 本项目只发布 Windows
    winreg = None  # type: ignore[assignment]

#: 注册表里的值名。用户会在任务管理器里看到它，别用带版本号的名字
VALUE_NAME = "MiHomePanel"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

#: 命令行里标记"这次是开机自启"的参数
FLAG = "--autostart"

STATE_ON = "on"
STATE_OFF = "off"
STATE_STALE = "stale"
STATE_UNSUPPORTED = "unsupported"


class AutostartError(RuntimeError):
    """写注册表失败（权限被策略收紧、注册表被锁）。"""


@dataclass(frozen=True)
class Status:
    state: str
    #: 注册表里的原始命令（没启用时是空串）
    command: str = ""
    #: 这条命令指向的 exe（解析不出来就是空串）
    target: str = ""

    @property
    def is_on(self) -> bool:
        """开着（含指向旧路径）——开关该显示为开。"""
        return self.state in (STATE_ON, STATE_STALE)


def current_exe() -> Path | None:
    """当前可执行文件；源码运行时返回 None（那时没有"开机启动"这回事）。"""
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable)
    return exe if exe.name.lower().endswith(".exe") else None


def supported() -> bool:
    return winreg is not None and current_exe() is not None


def command_for(exe: Path | str) -> str:
    return f'"{exe}" {FLAG}'


def exe_from_command(command: str) -> str:
    """从注册表命令里取出 exe 路径（兼容带引号和不带引号的旧值）。"""
    text = (command or "").strip()
    if not text:
        return ""
    if text.startswith('"'):
        end = text.find('"', 1)
        return text[1:end] if end > 1 else text.strip('"')
    low = text.lower()
    at = low.find(".exe")
    if at > 0:
        return text[: at + 4]
    return text.split(" ")[0]


def _same_path(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def status(
    *,
    exe: Path | str | None = None,
    root=None,
    key_path: str = RUN_KEY,
    value_name: str = VALUE_NAME,
) -> Status:
    """读注册表判断当前状态。读不到就当没启用，不抛异常。

    参数是给测试用的：``root`` / ``key_path`` 换成临时键，``exe`` 伪造"当前程序"，
    这样验证脚本永远不碰用户的真实启动项。
    """
    if winreg is None:
        return Status(STATE_UNSUPPORTED)
    here = Path(exe) if exe is not None else current_exe()
    if here is None:
        return Status(STATE_UNSUPPORTED)
    try:
        with winreg.OpenKey(root or winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
            command, _ = winreg.QueryValueEx(key, value_name)
    except (FileNotFoundError, OSError):
        return Status(STATE_OFF)
    command = str(command or "")
    target = exe_from_command(command)
    if _same_path(target, str(here)):
        return Status(STATE_ON, command, target)
    return Status(STATE_STALE, command, target)


def enable(
    *,
    exe: Path | str | None = None,
    root=None,
    key_path: str = RUN_KEY,
    value_name: str = VALUE_NAME,
) -> Status:
    if winreg is None:
        raise AutostartError("只有 Windows 支持开机启动")
    here = Path(exe) if exe is not None else current_exe()
    if here is None:
        raise AutostartError("源码运行时不支持：打包成 exe 后才能注册开机启动")
    command = command_for(here)
    try:
        with winreg.CreateKeyEx(
            root or winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, command)
    except OSError as exc:
        raise AutostartError(f"写入注册表失败：{exc}") from exc
    return Status(STATE_ON, command, str(here))


def disable(
    *,
    root=None,
    key_path: str = RUN_KEY,
    value_name: str = VALUE_NAME,
) -> Status:
    """删掉这一行。本来就没有也算成功（用户可能在任务管理器里先删了）。"""
    if winreg is None:
        raise AutostartError("只有 Windows 支持开机启动")
    try:
        with winreg.OpenKey(
            root or winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, value_name)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise AutostartError(f"删除注册表值失败：{exc}") from exc
    return Status(STATE_OFF)


def heal(
    *,
    exe: Path | str | None = None,
    root=None,
    key_path: str = RUN_KEY,
    value_name: str = VALUE_NAME,
) -> Status:
    """启动时调一次：开着但指向旧路径 → 改写成当前 exe。

    只在**本来就开着**的时候动手（`STATE_STALE`），`STATE_OFF` 一律不碰——
    "启动时顺手把功能打开"是最讨厌的一类自作聪明。
    """
    now = status(exe=exe, root=root, key_path=key_path, value_name=value_name)
    if now.state != STATE_STALE:
        return now
    try:
        return enable(exe=exe, root=root, key_path=key_path, value_name=value_name)
    except AutostartError:
        # 自愈失败不值得拦启动：设置页里还会显示"指向旧路径"，用户能手动重开
        return now
