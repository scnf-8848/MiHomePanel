# SPDX-License-Identifier: GPL-3.0-or-later
"""字体：字形、字号、抗锯齿的唯一出口。

**为什么要换字体引擎**：Windows 下 Qt 6 默认走 DirectWrite，13 px 中文会带 ClearType 的
红蓝彩边，肉眼看就是"笔画毛刺"。``QFont.setStyleStrategy(NoSubpixelAntialias)`` 在
DirectWrite 后端被**静默忽略**（实测渲染图逐像素相同），把字体换成 MiSans / Noto 也一样脏。
唯一有效的是把引擎换成 FreeType 再关子像素，实测彩边 20.09 → 0.00。

``QT_QPA_PLATFORM`` 只在 QApplication 构造时读一次，所以 :func:`prepare_engine` 必须赶在
建 app 之前调；想退回 DirectWrite（带彩边）就启动前设 ``MIHOME_FONT_ENGINE=default``。

实测数据与备选方案见 ``docs/reference/font-swap.md``。
"""

from __future__ import annotations

import os
import sys

from PySide6.QtGui import QFont

#: 界面统一字体与字号。**这里是字体的唯一来源**（经 ``apply_to`` 设成 app 字体）。
#: 2026-11-25 起 QSS 里不再写 ``font-family``：那个 ``*`` 规则会让 Qt 为每个控件
#: 单独解析字体栈、各建一套字体引擎，143 个控件的主窗口因此多占 31 MB 私有内存
#: （实测见 ``docs/dev/ui.md``「字体只由 app 下发」）。换字体只改这两个常量。
FAMILY = "Microsoft YaHei UI"
PIXEL_SIZE = 13

#: 设成 ``default`` 就退回 DirectWrite，不改任何代码。
ENGINE_ENV = "MIHOME_FONT_ENGINE"


def _with_freetype(current: str) -> str:
    """算出应该在 ``QT_QPA_PLATFORM`` 里写什么。

    三种情况原样返回：非 Windows、用户已经指定了 ``fontengine=``、用户把平台换成了
    ``offscreen`` / ``minimal`` 之类的非 windows 平台（离屏验证与无头环境靠这个）。
    """
    if sys.platform != "win32":
        return current
    base, _, options = current.partition(":")
    if base and base != "windows":
        return current
    if "fontengine=" in options:
        return current
    base = base or "windows"
    rest = f":{options}" if options else ""
    return f"{base}:fontengine=freetype{rest}"


def prepare_engine() -> bool:
    """在 QApplication 构造**之前**调，把字体引擎换成 FreeType。

    返回是否真的改了 —— 调用方可以据此在自检里如实说明当前用的哪条渲染路径。
    """
    if os.environ.get(ENGINE_ENV, "").strip().lower() == "default":
        return False
    current = os.environ.get("QT_QPA_PLATFORM", "")
    wanted = _with_freetype(current)
    if wanted == current:
        return False
    os.environ["QT_QPA_PLATFORM"] = wanted
    return True


def make_font() -> QFont:
    """界面基准字体。开抗锯齿但关子像素——前者保证笔画不锯齿，后者去掉彩色边。"""
    font = QFont(FAMILY)
    font.setPixelSize(PIXEL_SIZE)
    font.setStyleStrategy(QFont.NoSubpixelAntialias)
    return font


def apply_to(app) -> None:
    """装到 QApplication 上。晚于 :func:`prepare_engine` 即可，与引擎无关。"""
    app.setFont(make_font())
