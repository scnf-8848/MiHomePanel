# SPDX-License-Identifier: GPL-3.0-or-later
"""给 ``sys.stdout`` / ``sys.stderr`` 兜底，专门对付窗口化的 exe。

``--windowed`` 打出来的 exe 没有控制台，CPython 启动时会把 ``sys.stdout`` 和
``sys.stderr`` 直接置成 ``None``（不是 "控制台没输出"，是对象不存在）。而第三方库
很喜欢在**模块级**就碰它们：mijiaAPI 的 ``ColorFormatter.__init__`` 里有一句
``sys.stdout.isatty()``，而它又在 ``logger.py`` 结尾立刻构造了一个 logger ——
于是 ``import mijiaAPI`` 这一步就抛 ``'NoneType' object has no attribute 'isatty'``，
连 QApplication 都还没建起来，用户只看到 PyInstaller 的 Unhandled exception 弹窗。

``print()`` 在 stdout 为 None 时是安全的（内置实现直接返回），所以真正需要的只是
让 ``.isatty()`` / ``.write()`` 这类调用有个能接住的流。
"""

from __future__ import annotations

import os
import sys
from typing import IO

_sinks: list[IO[str]] = []


def ensure_streams() -> None:
    """缺哪个就补哪个；源码运行（有控制台）时是空操作。"""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is not None:
            continue
        try:
            sink = open(os.devnull, "w", encoding="utf-8", errors="replace")  # noqa: SIM115
        except OSError:  # pragma: no cover - 连 devnull 都开不了就别硬来了
            return
        _sinks.append(sink)
        setattr(sys, name, sink)
