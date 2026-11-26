# SPDX-License-Identifier: GPL-3.0-or-later
"""快捷场景 → 桌面快捷方式（``.lnk``），以及"点一下"的交接通道。

两块职责，都只用标准库：

1. **建 / 读 ``.lnk``**：ctypes 直连 COM（``IShellLinkW`` + ``IPersistFile``）。
   本机没有 pywin32 / comtypes，也不该为这一件事引入依赖（绿色版、无安装器）。
2. **交接**：快捷方式带 ``--scene <sid> --home <hid>`` 启动程序。已经有一个实例
   在跑时，第二个进程**只落一个待执行文件 + 广播，然后退出**，网络请求一律由主
   实例发——``auth.json`` 是非原子写、``passToken`` 会轮换，两个进程同时刷新会把
   用户踢下线（`docs/reference/scene-shortcut.md` 第 3 节）。

不记"哪个场景建过快捷方式"：快捷方式本身带 ``scene_id``，桌面文件归用户管，
程序既不需要也不可能维护那份账（用户改名/挪走/删掉都在程序视野之外）。
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import secrets
import sys
import time
from ctypes import POINTER, byref, c_int, c_void_p, c_wchar_p, wintypes
from pathlib import Path
from typing import Any, NamedTuple

#: 文件名里 Windows 不允许的字符
_INVALID_NAME = '\\/:*?"<>|'
#: 文件名上限（不含 ``.lnk``）。桌面图标能显示的字远少于这个数，长名字只是难认
_NAME_MAX = 60
#: 待执行条目最多活这么久：程序崩溃前留下的残骸，事后突然跑一次场景比不跑更吓人
PENDING_MAX_AGE = 60.0
#: 一次最多消费几条（连点五次就执行五次，但别让一轮处理几十个）
PENDING_LIMIT = 5
#: id 允许的字符：场景 / 家庭 id 都是数字，留字母下划线给测试桩
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


# ---------------------------------------------------------------------------
# 桌面快捷方式
# ---------------------------------------------------------------------------


class Launch(NamedTuple):
    """一个快捷方式要写进去的四样东西。"""

    target: str
    arguments: str
    workdir: str
    description: str


def sanitize_name(name: str, scene_id: str = "") -> str:
    """把场景名变成能当文件名的东西。

    替换非法字符、去掉首尾空格与句点（Windows 上 ``回家.`` 会被截成 ``回家``，
    和另一个叫 ``回家`` 的文件撞名）、超长截断、空名回落。
    """
    text = "".join(
        "_" if (ch in _INVALID_NAME or ord(ch) < 32) else ch for ch in (name or "")
    )
    text = text.strip(" .")
    if len(text) > _NAME_MAX:
        text = text[:_NAME_MAX].strip(" .")
    if text:
        return text
    tail = "".join(ch for ch in str(scene_id) if ch.isalnum())[-6:]
    return f"场景{tail}" if tail else "快捷场景"


def desktop_dir() -> Path:
    """当前用户的桌面目录。

    **必须走 ``QStandardPaths``**，不能拼 ``%USERPROFILE%\\Desktop``：OneDrive
    接管桌面之后后者指向一个用户看不见的空目录，建出来的图标等于没建。
    """
    try:
        from PySide6.QtCore import QStandardPaths

        raw = QStandardPaths.writableLocation(QStandardPaths.DesktopLocation)
    except Exception:  # noqa: BLE001 - 没有 Qt（验证脚本）时退回默认路径
        raw = ""
    return Path(raw) if raw else Path.home() / "Desktop"


def launch_spec(scene_id: str, home_id: str, scene_name: str = "") -> Launch:
    """快捷方式的目标 / 参数 / 工作目录。

    | 形态 | 目标 | 参数 |
    | --- | --- | --- |
    | 打包 | ``MiHomePanel.exe`` | ``--scene <sid> --home <hid>`` |
    | 源码 | ``.venv\\Scripts\\pythonw.exe`` | ``-m mihome --scene … --home …`` |

    源码模式**用 ``pythonw.exe`` 不用 ``python.exe``**：否则每点一次桌面图标都闪
    一个黑框。找不到 ``pythonw`` 才退回 ``python``。
    """
    arguments = f"--scene {scene_id} --home {home_id}"
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        return Launch(
            str(exe), arguments, str(exe.parent),
            f"米家控制台 · 执行「{scene_name or scene_id}」",
        )
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        windowed = exe.with_name("pythonw.exe")
        if windowed.is_file():
            exe = windowed
    root = Path(__file__).resolve().parents[2]
    return Launch(
        str(exe), f"-m mihome {arguments}", str(root),
        f"米家控制台 · 执行「{scene_name or scene_id}」",
    )


def create(
    scene_id: str,
    home_id: str,
    scene_name: str,
    *,
    desktop: Path | None = None,
    icon: str = "",
) -> Path:
    """建（或覆盖）一个桌面快捷方式，返回落地的路径。

    ``icon`` 由调用方给（界面层知道程序图标在哪）；空串就用目标程序自己的图标。
    同名文件已经绑着**别的**场景时另起一个 ``(2)``，不静默抢占——两个家庭各有一个
    「回家」是很正常的事。
    """
    folder = Path(desktop) if desktop is not None else desktop_dir()
    path = unique_path(folder, sanitize_name(scene_name, scene_id), scene_id)
    spec = launch_spec(scene_id, home_id, scene_name)
    write_lnk(path, spec.target, spec.arguments, spec.workdir, icon, spec.description)
    return path


def unique_path(folder: Path, base: str, scene_id: str) -> Path:
    """``<base>.lnk``；被别的场景占了就找 ``<base> (2).lnk``、``(3)``…"""
    path = folder / f"{base}.lnk"
    if not path.exists():
        return path
    try:
        bound = read_lnk(path).get("arguments", "")
    except OSError:
        return path  # 读不出来（损坏 / 不是我们建的）→ 直接覆盖，别留一堆残渣
    if _scene_of(bound) in ("", str(scene_id)):
        return path
    for i in range(2, 100):
        candidate = folder / f"{base} ({i}).lnk"
        if not candidate.exists():
            return candidate
    return folder / f"{base}-{scene_id}.lnk"


def _scene_of(arguments: str) -> str:
    """从参数串里抠出 ``--scene`` 的值（判断"这个 .lnk 是不是同一场景"用）。"""
    match = re.search(r"--scene[ =](\S+)", arguments or "")
    return match.group(1) if match else ""


# -- COM --------------------------------------------------------------------

class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


_CLSID_SHELLLINK = "{00021401-0000-0000-C000-000000000046}"
_IID_SHELLLINK = "{000214F9-0000-0000-C000-000000000046}"
_IID_PERSISTFILE = "{0000010B-0000-0000-C000-000000000046}"
_CLSCTX_INPROC_SERVER = 1
#: ``CoInitializeEx`` 的公寓模型；STA 是 shell 扩展的要求
_COINIT_APARTMENTTHREADED = 2

# vtable 序号（IShellLinkW 继承 IUnknown 的 3 个方法，所以第一个业务方法是 3）
_VT_RELEASE = 2
_VT_GET_PATH = 3
_VT_SET_DESCRIPTION = 7
_VT_GET_WORKDIR = 8
_VT_SET_WORKDIR = 9
_VT_GET_ARGUMENTS = 10
_VT_SET_ARGUMENTS = 11
_VT_GET_ICON = 16
_VT_SET_ICON = 17
_VT_SET_PATH = 20
# IPersistFile 同理：Q I AddRef Release GetClassID IsDirty Load Save …
_VT_SAVE = 6


def _ole32():
    return ctypes.WinDLL("ole32", use_last_error=True)


def _guid(text: str) -> _GUID:
    ole = _ole32()
    out = _GUID()
    ole.CLSIDFromString(c_wchar_p(text), byref(out))
    return out


def _hr(hr: int) -> str:
    return f"0x{hr & 0xFFFFFFFF:08X}"


def _vcall(obj: c_void_p, index: int, restype, *argtypes):
    """取 ``vtable[index]`` 并包成可调用对象（COM 的 this 指针由调用处补）。"""
    table = ctypes.cast(obj, POINTER(POINTER(c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return proto(table[index])


class _Link:
    """``IShellLinkW`` 的最小封装：本模块用到的 5 个 setter + 3 个 getter + Save。

    **必须在同一线程里 CoInitialize**：shell 链接是 STA 对象，从没初始化的线程
    创建会拿到 ``CO_E_NOTINITIALIZED``。:meth:`close` 只在本类自己成功初始化时
    才 ``CoUninitialize``，不去动宿主（Qt）的 COM 计数。
    """

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("桌面快捷方式只有 Windows 实现")
        self._ole = _ole32()
        hr = self._ole.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
        self._inited = (hr & 0xFFFFFFFF) in (0, 1)  # S_OK / S_FALSE
        self._ptr = c_void_p()
        clsid = _guid(_CLSID_SHELLLINK)
        iid = _guid(_IID_SHELLLINK)
        hr = self._ole.CoCreateInstance(
            byref(clsid), None, _CLSCTX_INPROC_SERVER, byref(iid), byref(self._ptr)
        )
        if hr < 0 or not self._ptr.value:
            self.close()
            raise OSError(f"创建 ShellLink 对象失败：{_hr(hr)}")

    # -- 生命周期 --------------------------------------------------------
    def close(self) -> None:
        if self._ptr is not None and self._ptr.value:
            try:
                _vcall(self._ptr, _VT_RELEASE, wintypes.ULONG)(self._ptr)
            except (OSError, ValueError):
                pass
            self._ptr = c_void_p()
        if self._inited:
            self._ole.CoUninitialize()
            self._inited = False

    def __enter__(self) -> _Link:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- 写 --------------------------------------------------------------
    def set_path(self, path: str) -> None:
        _vcall(self._ptr, _VT_SET_PATH, ctypes.HRESULT, c_wchar_p)(self._ptr, path)

    def set_arguments(self, args: str) -> None:
        _vcall(self._ptr, _VT_SET_ARGUMENTS, ctypes.HRESULT, c_wchar_p)(self._ptr, args)

    def set_workdir(self, path: str) -> None:
        _vcall(self._ptr, _VT_SET_WORKDIR, ctypes.HRESULT, c_wchar_p)(self._ptr, path)

    def set_description(self, text: str) -> None:
        _vcall(self._ptr, _VT_SET_DESCRIPTION, ctypes.HRESULT, c_wchar_p)(self._ptr, text)

    def set_icon(self, path: str, index: int = 0) -> None:
        _vcall(self._ptr, _VT_SET_ICON, ctypes.HRESULT, c_wchar_p, c_int)(
            self._ptr, path, index
        )

    def save(self, path: Path) -> None:
        persist = self._persist()
        try:
            hr = _vcall(persist, _VT_SAVE, ctypes.HRESULT, c_wchar_p, wintypes.BOOL)(
                persist, str(path), True
            )
        finally:
            _vcall(persist, _VT_RELEASE, wintypes.ULONG)(persist)
        if hr < 0:
            raise OSError(f"保存快捷方式失败：{_hr(hr)}")

    # -- 读 --------------------------------------------------------------
    def load(self, path: Path) -> None:
        """把已有的 ``.lnk`` 读进这个对象，之后三个 getter 才有内容。"""
        persist = self._persist()
        try:
            # IPersistFile::Load 是 5 号方法（GetClassID IsDirty Load Save …）
            hr = _vcall(persist, 5, ctypes.HRESULT, c_wchar_p, wintypes.DWORD)(
                persist, str(path), 0
            )
        finally:
            _vcall(persist, _VT_RELEASE, wintypes.ULONG)(persist)
        if hr < 0:
            raise OSError(f"读取快捷方式失败：{_hr(hr)}")

    def _persist(self) -> c_void_p:
        """``QueryInterface(IPersistFile)``。用一次就 Release 一次，不缓存接口指针。"""
        persist = c_void_p()
        iid = _guid(_IID_PERSISTFILE)
        hr = _vcall(self._ptr, 0, ctypes.HRESULT, POINTER(_GUID), POINTER(c_void_p))(
            self._ptr, byref(iid), byref(persist)
        )
        if hr < 0 or not persist.value:
            raise OSError(f"QueryInterface(IPersistFile) 失败：{_hr(hr)}")
        return persist

    def get_path(self) -> str:
        buf = ctypes.create_unicode_buffer(1024)
        _vcall(
            self._ptr, _VT_GET_PATH, ctypes.HRESULT, c_wchar_p, c_int, c_void_p, wintypes.DWORD
        )(self._ptr, buf, 1024, None, 0)
        return buf.value

    def get_arguments(self) -> str:
        buf = ctypes.create_unicode_buffer(1024)
        _vcall(self._ptr, _VT_GET_ARGUMENTS, ctypes.HRESULT, c_wchar_p, c_int)(
            self._ptr, buf, 1024
        )
        return buf.value

    def get_workdir(self) -> str:
        buf = ctypes.create_unicode_buffer(1024)
        _vcall(self._ptr, _VT_GET_WORKDIR, ctypes.HRESULT, c_wchar_p, c_int)(
            self._ptr, buf, 1024
        )
        return buf.value

    def get_icon(self) -> str:
        buf = ctypes.create_unicode_buffer(1024)
        _vcall(
            self._ptr, _VT_GET_ICON, ctypes.HRESULT, c_wchar_p, c_int,
            POINTER(c_int), POINTER(c_int),
        )(self._ptr, buf, 1024, byref(c_int()), None)
        return buf.value


def write_lnk(
    path: Path,
    target: str,
    arguments: str,
    workdir: str,
    icon: str = "",
    description: str = "",
) -> None:
    """落一个 ``.lnk``。``target`` 不要自己加引号——它存的是路径本身。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _Link() as link:
        link.set_path(target)
        link.set_arguments(arguments)
        link.set_workdir(workdir)
        if description:
            link.set_description(description)
        if icon:
            link.set_icon(icon)
        link.save(path)
    _notify_shell()


def read_lnk(path: Path) -> dict[str, str]:
    """读回目标 / 参数 / 工作目录 / 图标路径。文件不是 ``.lnk`` 或读不了时抛 OSError。"""
    if not path.is_file():
        raise OSError(f"文件不存在：{path}")
    try:
        with _Link() as link:
            link.load(path)
            return {
                "target": link.get_path(),
                "arguments": link.get_arguments(),
                "workdir": link.get_workdir(),
                "icon": link.get_icon(),
            }
    except OSError:
        raise
    except Exception as exc:  # noqa: BLE001 - COM 抛什么都算"读不了"
        raise OSError(f"读取快捷方式失败：{type(exc).__name__}: {exc}") from exc


def _notify_shell() -> None:
    """告诉资源管理器"桌面变了"，否则新图标要等它下一次自己刷新才出现。"""
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.SHChangeNotify(0x08000000, 0x0000, None, None)  # SHCNE_ASSOCCHANGED
    except (OSError, AttributeError):
        pass


# ---------------------------------------------------------------------------
# 交接通道：待执行文件
# ---------------------------------------------------------------------------


def pending_dir() -> Path:
    from . import paths

    folder = paths.data_dir() / "pending"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def queue(scene_id: str, home_id: str) -> Path:
    """写一条"请执行这个场景"。

    先写 ``.tmp`` 再 ``os.replace``：读到半个文件就执行是最糟的结果。
    """
    folder = pending_dir()
    stamp = int(time.time() * 1000)
    name = f"scene-{stamp}-{os.getpid()}-{secrets.token_hex(2)}"
    payload = {"scene_id": str(scene_id), "home_id": str(home_id), "at": stamp}
    tmp = folder / f"{name}.tmp"
    final = folder / f"{name}.json"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, final)
    return final


def drain(
    limit: int = PENDING_LIMIT,
    max_age: float = PENDING_MAX_AGE,
    *,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """取走待执行条目，按写入时间升序，最多 ``limit`` 条。

    超龄的直接删掉不执行（那是上次崩溃留下的残骸）；写坏的也删掉。
    ``.tmp`` 不在 ``*.json`` 里，天然不会被取走。
    """
    folder = pending_dir()
    stamp_now = (now if now is not None else time.time()) * 1000
    ready: list[tuple[int, Path, dict[str, Any]]] = []
    for path in folder.glob("scene-*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            at = int(payload.get("at") or 0)
        except (OSError, ValueError, TypeError):
            path.unlink(missing_ok=True)
            continue
        if stamp_now - at > max_age * 1000:
            path.unlink(missing_ok=True)
            continue
        if not payload.get("scene_id"):
            path.unlink(missing_ok=True)
            continue
        ready.append((at, path, payload))
    ready.sort(key=lambda item: item[0])
    out: list[dict[str, Any]] = []
    for _at, path, payload in ready[: max(0, limit)]:
        path.unlink(missing_ok=True)
        out.append(payload)
    return out


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def requested(argv: list[str]) -> bool:
    """命令行里有没有 ``--scene``。用来区分"快捷方式调用"和"普通启动"。"""
    return any(a == "--scene" or a.startswith("--scene=") for a in argv)


def parse_request(argv: list[str]) -> tuple[str, str] | None:
    """取出 ``(scene_id, home_id)``；不是快捷方式调用、或参数不合法都返回 ``None``。

    不合法要**记一条日志**：快捷方式的参数是程序自己写的，哪天写错了，日志是唯一
    能倒查的地方。**不接受空 id、也不接受带空格/引号的串**——它们会被原样拼进
    命令行，等于给自己开了一个注入口子。
    """
    if not requested(argv):
        return None
    scene_id = _flag_value(argv, "--scene")
    home_id = _flag_value(argv, "--home")
    if not _ID_RE.fullmatch(scene_id or ""):
        _log(f"--scene 取值不合法：{scene_id!r}，本次不执行")
        return None
    if not _ID_RE.fullmatch(home_id or ""):
        _log(f"--home 取值不合法：{home_id!r}，本次不执行")
        return None
    return scene_id, home_id


def _flag_value(argv: list[str], flag: str) -> str:
    for i, arg in enumerate(argv):
        if arg == flag:
            return argv[i + 1] if i + 1 < len(argv) else ""
        if arg.startswith(flag + "="):
            return arg[len(flag) + 1:]
    return ""


def _log(message: str) -> None:
    try:
        from . import applog

        applog.log("shortcut", message)
    except Exception:  # noqa: BLE001 - 日志不该影响启动
        pass


__all__ = [
    "Launch",
    "PENDING_LIMIT",
    "PENDING_MAX_AGE",
    "create",
    "desktop_dir",
    "drain",
    "launch_spec",
    "parse_request",
    "pending_dir",
    "queue",
    "read_lnk",
    "requested",
    "sanitize_name",
    "unique_path",
    "write_lnk",
]
