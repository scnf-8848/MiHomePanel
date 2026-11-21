# SPDX-License-Identifier: GPL-3.0-or-later
"""桌面挂件的窗口层级：贴桌面 / 普通 / 置顶。

三档不是三个窗口，是同一个窗口的三种"父窗口 + z 序"组合：

- ``desktop``：``SetParent`` 到资源管理器的 ``SHELLDLL_DefView``（桌面图标所在
  的那个窗口），再提到 DefView 子窗口的顶。桌面露出时才可见，按 Win+D 也还在，
  普通窗口永远不会被它挡住。
- ``normal``：普通顶层窗口，谁先谁后按系统默认。
- ``top``：``HWND_TOPMOST``，压在所有普通窗口之上。

几条踩过的坑，改这里之前先读：

1. **DefView 只许读。** 往它的 z 序里插别人的窗口、改它的尺寸，能把桌面图标
   一起写没（窗口变成 0×0）。这里唯一碰它的操作是 ``SetParent`` 自己的窗口。
2. **挂进去之后 Qt 不知道。** explorer 真被杀掉时，作为 DefView 子窗口的挂件
   会被系统一起销毁（实测 0.03 s），但 ``QWidget.isVisible()`` 依然返回 True，
   ``winId()`` 变成死句柄。分辨"还活着"只能用 ``IsWindow``，见
   ``watchdog_action``。
3. **非 Windows 全 no-op。** 这个仓库只发布 Windows，但 ``verify_*`` 和文档
   构建会 import 到 UI 包；这里每个入口都必须在没有 user32 时安静返回。
"""

from __future__ import annotations

import sys

WINDOWS = sys.platform == "win32"

#: z 序锚点。用 ``c_void_p(-1)`` 传，64 位下直接传 -1 会被当成无符号值截断
HWND_TOP = 0
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
#: 重挂之后必须显式带 ``WS_VISIBLE``：``SetParent`` 前后 Qt 可能已经把子窗口的
#: WS_VISIBLE 清掉（实测挂进 DefView 后 IsWindowVisible=False、Qt 侧仍以为可见），
#: 结果是"挂是挂上了，桌面上什么都没有"。
SWP_SHOWWINDOW = 0x0040

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_CHILD = 0x40000000
WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080

#: 圆角半径（逻辑像素）。``SetWindowRgn`` 吃的是物理像素，调用方自己乘 DPR
DEFAULT_RADIUS = 10


def _as_hwnd(value) -> int:
    """``winId()`` 给的是 ``WId``，各平台包装不同，统一成 int。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class _NullApi:
    """非 Windows 的实现：一切"没做到"，调用方据此降级成普通窗口。"""

    def find_defview(self) -> int:
        return 0

    def set_parent(self, hwnd: int, parent: int) -> bool:
        return False

    def get_parent(self, hwnd: int) -> int:
        return 0

    def is_window(self, hwnd: int) -> bool:
        return False

    def is_topmost(self, hwnd: int) -> bool:
        return False

    def set_pos(self, hwnd: int, insert_after: int, flags: int) -> bool:
        return False

    def set_style(self, hwnd: int, add: int = 0, remove: int = 0) -> None:
        return None

    def set_region(self, hwnd: int, width: int, height: int, radius: int) -> bool:
        return False

    def set_ext_style(self, hwnd: int, add: int = 0, remove: int = 0) -> None:
        return None

    def foreground_window(self) -> int:
        return 0

    def set_foreground(self, hwnd: int) -> bool:
        return False


class _Win32Api:
    """user32 的最小门面。只声明真正用到的几个函数，argtypes 写全防截断。"""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wt = wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self.user32 = user32
        self.gdi32 = gdi32

        u = user32
        u.FindWindowW.restype = wintypes.HWND
        u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        u.FindWindowExW.restype = wintypes.HWND
        u.FindWindowExW.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
        ]
        u.EnumWindows.restype = wintypes.BOOL
        u.GetClassNameW.restype = ctypes.c_int
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.SetParent.restype = wintypes.HWND
        u.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
        u.GetParent.restype = wintypes.HWND
        u.GetParent.argtypes = [wintypes.HWND]
        u.IsWindow.restype = wintypes.BOOL
        u.IsWindow.argtypes = [wintypes.HWND]
        u.SetWindowPos.restype = wintypes.BOOL
        u.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        u.GetWindowLongW.restype = ctypes.c_long
        u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        u.SetWindowLongW.restype = ctypes.c_long
        u.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        u.SetWindowRgn.restype = ctypes.c_int
        u.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]

        g = gdi32
        g.CreateRoundRectRgn.restype = wintypes.HRGN
        g.CreateRoundRectRgn.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
        ]
        g.DeleteObject.restype = wintypes.BOOL
        g.DeleteObject.argtypes = [wintypes.HGDIOBJ]

        self._enum_proc = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )

    # -- 查询 ------------------------------------------------------------
    def find_defview(self) -> int:
        """桌面图标所在的 ``SHELLDLL_DefView``。

        优先取 Progman 的子窗口：壁纸软件 / 多桌面工具也会造出别的 DefView
        （里面没有图标），挂错了就是"挂件在桌面上，但桌面图标在全屏窗口里"。
        找不到再退回全局查找，最后才枚举顶层 WorkerW（部分系统把 DefView
        挪到 WorkerW 下）。
        """
        u = self.user32
        progman = u.FindWindowW("Progman", None)
        if progman:
            child = u.FindWindowExW(progman, None, "SHELLDLL_DefView", None)
            if child:
                return _as_hwnd(child)
        direct = u.FindWindowW("SHELLDLL_DefView", None)
        if direct:
            return _as_hwnd(direct)
        found: list[int] = []
        user32 = self.user32
        buf = self._ctypes.create_unicode_buffer(64)

        def _visit(hwnd, _lparam):
            user32.GetClassNameW(hwnd, buf, 64)
            if buf.value != "WorkerW":
                return True
            child = user32.FindWindowExW(hwnd, None, "SHELLDLL_DefView", None)
            if child:
                found.append(_as_hwnd(child))
                return False
            return True

        user32.EnumWindows(self._enum_proc(_visit), 0)
        return found[0] if found else 0

    def get_parent(self, hwnd: int) -> int:
        return _as_hwnd(self.user32.GetParent(self._wt.HWND(hwnd)))

    def is_window(self, hwnd: int) -> bool:
        return bool(self.user32.IsWindow(self._wt.HWND(hwnd)))

    def is_topmost(self, hwnd: int) -> bool:
        style = self.user32.GetWindowLongW(self._wt.HWND(hwnd), GWL_EXSTYLE)
        return bool(style & WS_EX_TOPMOST)

    # -- 写 --------------------------------------------------------------
    def set_parent(self, hwnd: int, parent: int) -> bool:
        wt = self._wt
        # SetParent 返回旧父窗口；失败返回 0。父窗口本来就是 0（桌面）时没法
        # 用返回值区分，所以这里再问一次 GetParent 确认落点。
        self._ctypes.set_last_error(0)
        self.user32.SetParent(wt.HWND(hwnd), wt.HWND(parent) if parent else None)
        if self._ctypes.get_last_error():
            return False
        return self.get_parent(hwnd) == parent

    def set_pos(self, hwnd: int, insert_after: int, flags: int) -> bool:
        wt = self._wt
        # c_void_p(-1) 才是 "HWND_TOPMOST" 的位模式；HWND(-1) 在部分 ctypes
        # 版本里会被当成无符号值截断
        anchor = wt.HWND()
        anchor.value = self._ctypes.c_void_p(insert_after).value
        return bool(
            self.user32.SetWindowPos(wt.HWND(hwnd), anchor, 0, 0, 0, 0, flags)
        )

    def set_style(self, hwnd: int, add: int = 0, remove: int = 0) -> None:
        wt = self._wt
        style = self.user32.GetWindowLongW(wt.HWND(hwnd), GWL_STYLE)
        new = (style | add) & ~remove
        if new != style:
            self.user32.SetWindowLongW(wt.HWND(hwnd), GWL_STYLE, new)

    def set_ext_style(self, hwnd: int, add: int = 0, remove: int = 0) -> None:
        wt = self._wt
        style = self.user32.GetWindowLongW(wt.HWND(hwnd), GWL_EXSTYLE)
        new = (style | add) & ~remove
        if new != style:
            self.user32.SetWindowLongW(wt.HWND(hwnd), GWL_EXSTYLE, new)

    def foreground_window(self) -> int:
        return int(self.user32.GetForegroundWindow() or 0)

    def set_foreground(self, hwnd: int) -> bool:
        return bool(self.user32.SetForegroundWindow(self._wt.HWND(hwnd)))

    def set_region(self, hwnd: int, width: int, height: int, radius: int) -> bool:
        if width <= 0 or height <= 0:
            return False
        rgn = self.gdi32.CreateRoundRectRgn(
            0, 0, width + 1, height + 1, radius * 2, radius * 2
        )
        if not rgn:
            return False
        # SetWindowRgn 接管 rgn 的所有权，成功后不能再 DeleteObject
        if not self.user32.SetWindowRgn(self._wt.HWND(hwnd), rgn, True):
            self.gdi32.DeleteObject(rgn)
            return False
        return True


#: 当前后端。``verify_desktop_widget.py`` 直接替换成假实现来跑纯逻辑
_api = _Win32Api() if WINDOWS else _NullApi()


# ---------------------------------------------------------------------------
# 模块级入口：挂件只认这些函数，不直接碰 ctypes
# ---------------------------------------------------------------------------


def defview() -> int:
    """桌面图标层。0 表示暂时拿不到（explorer 正在重启 / 非 Windows）。"""
    return _api.find_defview()


def is_window(hwnd) -> bool:
    hwnd = _as_hwnd(hwnd)
    return bool(hwnd) and bool(_api.is_window(hwnd))


def get_parent(hwnd) -> int:
    hwnd = _as_hwnd(hwnd)
    return _api.get_parent(hwnd) if hwnd else 0


def current_layer(hwnd) -> str:
    """这个窗口现在实际处在哪一档。用于把菜单勾选落在真实档位。"""
    hwnd = _as_hwnd(hwnd)
    if not hwnd or not _api.is_window(hwnd):
        return "normal"
    view = _api.find_defview()
    if view and _api.get_parent(hwnd) == view:
        return "desktop"
    return "top" if _api.is_topmost(hwnd) else "normal"


def attach_desktop(hwnd) -> bool:
    """把窗口挂进桌面图标层。返回 False 表示这次挂不上（调用方降级成普通档）。"""
    hwnd = _as_hwnd(hwnd)
    if not hwnd:
        return False
    view = _api.find_defview()
    if not view:
        return False
    if _api.get_parent(hwnd) != view:
        # 顺序有讲究：先把 WS_POPUP 换成 WS_CHILD 再 SetParent。Qt 顶层窗口是
        # POPUP，直接跨进程挂到 DefView 下时 SetParent 会失败（实测返回 0、父窗口
        # 不变），先改风格再挂就能成。
        _api.set_style(hwnd, add=WS_CHILD, remove=WS_POPUP)
        if not _api.set_parent(hwnd, view):
            return False
    return bool(
        _api.set_pos(
            hwnd, HWND_TOP,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
    )


def detach_desktop(hwnd) -> bool:
    """从桌面图标层拿回来，恢复成普通顶层窗口。"""
    hwnd = _as_hwnd(hwnd)
    if not hwnd or not _api.is_window(hwnd):
        return False
    if _api.get_parent(hwnd) == 0:
        return True
    _api.set_style(hwnd, add=WS_POPUP, remove=WS_CHILD)
    return _api.set_parent(hwnd, 0)


def set_topmost(hwnd, on: bool) -> bool:
    """置顶 / 取消置顶。已经是目标状态时不重复调 SetWindowPos。"""
    hwnd = _as_hwnd(hwnd)
    if not hwnd or not _api.is_window(hwnd):
        return False
    want = bool(on)
    if _api.is_topmost(hwnd) == want:
        return True
    anchor = HWND_TOPMOST if want else HWND_NOTOPMOST
    return bool(_api.set_pos(hwnd, anchor, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE))


def raise_window(hwnd) -> bool:
    """提到同层最前，但**不激活**。

    普通档专用。挂件是 ``WS_EX_NOACTIVATE`` 窗口（``Qt.WindowDoesNotAcceptFocus``
    在 Windows 上就是这么实现的），点它不抢焦点是好事，但副作用是系统不会像普通
    窗口那样因为点击而把它提到前面——被别的窗口压住时就一直压着。所以点下去自己
    调一次 ``SetWindowPos(HWND_TOP)``：只改 z 序，不动前台窗口。
    """
    hwnd = _as_hwnd(hwnd)
    if not hwnd or not _api.is_window(hwnd):
        return False
    return bool(_api.set_pos(hwnd, HWND_TOP, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE))


def set_noactivate(hwnd, on: bool) -> None:
    """``WS_EX_NOACTIVATE`` 开关。

    挂件建窗口时带 ``Qt.WindowDoesNotAcceptFocus``，系统就给它这个位——点它不抢
    前台是好事，但**也点不动到最前**（用户 2026-11-21 实测：被浏览器压住时点它没
    任何反应，先点一下自家窗口再点才行）。普通档要靠"点击 → 系统激活 → 提层"这条
    标准链路，所以进普通档时清掉这一位；置顶档本来就最前、桌面档是 DefView 子窗，
    都不需要（也不该抢焦点），保持原本的 NOACTIVATE。
    """
    hwnd = _as_hwnd(hwnd)
    if not hwnd or not _api.is_window(hwnd):
        return
    if on:
        _api.set_ext_style(hwnd, add=WS_EX_NOACTIVATE)
    else:
        _api.set_ext_style(hwnd, remove=WS_EX_NOACTIVATE)


def foreground_window() -> int:
    """当前前台窗口，0 表示拿不到。"""
    return _api.foreground_window()


def set_foreground(hwnd) -> bool:
    """把前台还给某个窗口（提层之后用：挂件不接键盘，不该把焦点留在自己身上）。"""
    hwnd = _as_hwnd(hwnd)
    if not hwnd or not _api.is_window(hwnd):
        return False
    return bool(_api.set_foreground(hwnd))


def set_region(hwnd, width: int, height: int, radius: int = DEFAULT_RADIUS) -> bool:
    """圆角窗口。挂件不用分层窗口（layered 挂进 DefView 整窗不渲染），
    只能靠 ``SetWindowRgn`` 把四个角切掉。"""
    hwnd = _as_hwnd(hwnd)
    if not hwnd:
        return False
    return bool(_api.set_region(hwnd, int(width), int(height), int(radius)))


def watchdog_action(*, layer: str, is_window: bool, parent: int, defview: int) -> str:
    """看门狗每一轮该做什么：``none`` / ``reattach`` / ``rebuild``。

    纯函数，不碰任何句柄，方便在 ``verify_desktop_widget.py`` 里穷举状态：

    - 非桌面档：explorer 重启不影响普通 / 置顶窗口，什么都不做；
    - 窗口句柄已失效（explorer 被杀时 DefView 的子窗口跟着销毁）：``rebuild``；
    - 句柄还在、但父窗口不是当前的 DefView（explorer 重启后换了一个 DefView）：
      ``reattach``；
    - DefView 暂时拿不到（explorer 还没起来）：``none``，下一轮再看。
    """
    if layer != "desktop":
        return "none"
    if not is_window:
        return "rebuild"
    if not defview:
        return "none"
    # parent == 0 也算"宿主丢了"：窗口还活着但已被摘成顶层窗口（跨进程父窗口
    # 被销毁时是 rebuild，这种是自己摘掉的，重挂即可）。
    return "reattach" if parent != defview else "none"


__all__ = [
    "DEFAULT_RADIUS",
    "WINDOWS",
    "attach_desktop",
    "current_layer",
    "defview",
    "detach_desktop",
    "get_parent",
    "is_window",
    "set_region",
    "set_topmost",
    "watchdog_action",
]
