# SPDX-License-Identifier: GPL-3.0-or-later
"""组合键文本 ↔ Win32 ``RegisterHotKey`` 参数。

**为什么单独一层**：规则文件里存的是人能读的字符串（``Ctrl+Alt+F7``），而系统要的是
``(modifiers, vk)`` 两个整数；录制控件又是从整数反推字符串。三处各写一遍必然出现
「显示 Ctrl+Alt+F7、实际注册 Alt+Ctrl+F7、比较时又对不上」这类问题。这里定一份
规范形式：修饰键按 ``Ctrl / Alt / Shift / Win`` 固定顺序，主键用表里的规范名，
``normalize()`` 是唯一入口。

不 import Qt、不 import ctypes：常量只是整数，Linux / CI 上也能跑验证脚本。
真正的 ``RegisterHotKey`` 调用在 ``ui/hotkeys_win.py``，那里才碰 Win32。
"""

from __future__ import annotations

# -- Win32 常量 -------------------------------------------------------------
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
#: Win7+；抑制「按住不放随系统自动重复连发 WM_HOTKEY」。**内置、不给用户配**
MOD_NOREPEAT = 0x4000

_MOD_BY_NAME = {"ctrl": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}
_MOD_ORDER = ("Ctrl", "Alt", "Shift", "Win")
_MOD_ALIASES = {
    "control": "Ctrl", "ctrl": "Ctrl", "ctr": "Ctrl",
    "alt": "Alt", "menu": "Alt",
    "shift": "Shift",
    "win": "Win", "windows": "Win", "super": "Win", "meta": "Win",
}

_VK_BY_NAME: dict[str, int] = {}
for _i in range(1, 25):
    _VK_BY_NAME[f"F{_i}"] = 0x6F + _i          # F1=0x70 … F24=0x87
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    _VK_BY_NAME[_c] = ord(_c)
for _d in "0123456789":
    _VK_BY_NAME[_d] = ord(_d)
for _i in range(10):
    _VK_BY_NAME[f"Num{_i}"] = 0x60 + _i
_VK_BY_NAME.update({
    "Esc": 0x1B, "Tab": 0x09, "Enter": 0x0D, "Return": 0x0D, "Space": 0x20,
    "Backspace": 0x08, "Delete": 0x2E, "Insert": 0x2D,
    "Home": 0x24, "End": 0x23, "PageUp": 0x21, "PageDown": 0x22,
    "Up": 0x26, "Down": 0x28, "Left": 0x25, "Right": 0x27,
    "PrintScreen": 0x2C, "ScrollLock": 0x91, "Pause": 0x13,
    "CapsLock": 0x14, "NumLock": 0x90,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
})
#: 系统保留 / 强烈不建议（注册基本必失败或抢系统功能）。值是给用户的中文原因。
_RESERVED_EXACT = {
    "Win+L": "系统保留的锁屏组合",
    "Ctrl+Alt+Delete": "系统保留（安全注意序列）",
    "Ctrl+Shift+Esc": "系统保留（任务管理器）",
    "Ctrl+Esc": "系统保留（开始菜单）",
    "Alt+Tab": "系统保留（切换窗口）",
    "Alt+F4": "系统保留（关闭窗口）",
    "PrintScreen": "系统默认截图键，容易被覆盖或抢走",
    "F12": "微软文档说明保留给调试器",
}


def _split(raw: str) -> list[str]:
    """按 ``+`` 切；空格键写作 ``Space``，所以不会出现 ``Ctrl + Space`` 的歧义。"""
    return [part.strip() for part in str(raw or "").split("+") if part.strip()]


def normalize(raw: str) -> str:
    """把任意大小写 / 别名 / 顺序的写法收敛成规范串；不合法返回空串。

    ``Control+alt+f7`` → ``Ctrl+Alt+F7``；``F7+Ctrl`` → ``Ctrl+F7``。
    主键必须是 ``_VK_BY_NAME`` 里的名字，修饰键至少一个也可以没有（裸单键允许）。
    """
    parts = _split(raw)
    if not parts:
        return ""
    mods: set[str] = set()
    main = ""
    for part in parts:
        alias = _MOD_ALIASES.get(part.lower())
        if alias:
            if alias in mods:
                return ""  # 同一个修饰键写两遍没意义
            mods.add(alias)
            continue
        if main:
            return ""  # 两个主键
        # 表里大小写混杂（F7 / Space / Num0），逐个按小写索引找规范名
        found = next((k for k in _VK_BY_NAME if k.lower() == part.lower()), None)
        if found is None:
            return ""
        main = found
    if not main:
        return ""
    ordered = [m for m in _MOD_ORDER if m in mods]
    return "+".join([*ordered, main])


def to_native(raw: str) -> tuple[int, int] | None:
    """``(modifiers, vk)``；``modifiers`` 已带 ``MOD_NOREPEAT``。不合法返回 ``None``。

    带 ``MOD_NOREPEAT`` 是**L0 内置防重**，不给用户配也不展示；规则级冷静期是另一层
    （见 ``docs/reference/pc-state-api.md`` 1.4）。
    """
    text = normalize(raw)
    if not text:
        return None
    parts = _split(text)
    mods = 0
    vk = 0
    for part in parts[:-1]:
        mods |= _MOD_BY_NAME[part.lower()]
    vk = _VK_BY_NAME[parts[-1]]
    return mods | MOD_NOREPEAT, vk


def parse_native(modifiers: int, vk: int) -> str:
    """录制控件用：``WM_HOTKEY`` / ``WM_KEYDOWN`` 的 modifiers + 虚拟键码 → 规范串。"""
    names: list[str] = []
    for name, bit in (("Ctrl", MOD_CONTROL), ("Alt", MOD_ALT),
                      ("Shift", MOD_SHIFT), ("Win", MOD_WIN)):
        if modifiers & bit:
            names.append(name)
    main = next((k for k, v in _VK_BY_NAME.items() if v == vk), "")
    if not main:
        return ""
    return "+".join([*names, main])


def reserved_reason(raw: str) -> str:
    """返回不建议注册的原因，空串表示没拦到。

    这里只拦**确定**的保留键与官方明说的 ``F12``；其它「多半被占」的组合不预先判死，
    交给 ``RegisterHotKey`` 的返回值（它才是对的，且失败要在界面标红）。
    """
    text = normalize(raw)
    if not text:
        return "快捷键格式不对"
    if text in _RESERVED_EXACT:
        return _RESERVED_EXACT[text]
    parts = _split(text)
    main = parts[-1]
    if "Win" in parts[:-1] and len(main) == 1 and main.isalpha():
        return "Win+字母大多被系统占用，建议改用 Ctrl+Alt+… 或 F13–F24"
    return ""


def key_choices() -> tuple[str, ...]:
    """录制界面下拉用的主键清单（组合键的修饰键单独给复选框）。"""
    return tuple(_VK_BY_NAME.keys())


__all__ = [
    "MOD_ALT",
    "MOD_CONTROL",
    "MOD_NOREPEAT",
    "MOD_SHIFT",
    "MOD_WIN",
    "key_choices",
    "normalize",
    "parse_native",
    "reserved_reason",
    "to_native",
]
