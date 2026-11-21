# SPDX-License-Identifier: GPL-3.0-or-later
"""把"平时看不见的东西"落到 ``<data>/logs/`` 下。

**为什么需要它**：打包成 ``--windowed`` 的 exe 没有控制台，``_stdio.ensure_streams()``
把 ``sys.stdout`` / ``sys.stderr`` 指到 devnull——于是 Qt 的 ``qWarning`` 和我们的
诊断打印**全部消失**。2026-11-21 查"托盘里没有图标"时，整条链路上最关键的证据
（``Shell_NotifyIcon`` 到底成没成功）就是这样被吞掉的：代码里 ``show()`` 之后直接
``_shown = True``，谁也没去问系统一句，图标不在也没有任何痕迹。

两条通道，各有分工：

* ``log(名字, 文字)``——自己的诊断，按功能分文件（``tray.log`` 等），带时间戳。
* ``install_qt_handler()``——接管 Qt 的 warning 及以上级别，落到 ``qt.log``。
  只收 Warning/Critical/Fatal：``qDebug`` 级别的量太大，会把有用的一行淹掉。

**写不进去就算了**：日志是排障用的，绝不能因为磁盘满 / 目录只读把主流程带崩，
所以每次写入都吞 ``OSError``。文件按 256 KB 截断到最近若干行，不会无限长。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from . import paths

#: 单个日志文件的上限；超了就只留最后这些行
_MAX_BYTES = 256 * 1024
_KEEP_LINES = 400

_qt_installed = False


def _log_path(name: str) -> Path | None:
    try:
        return paths.log_dir() / f"{name}.log"
    except OSError:  # 数据目录建不出来（只读盘 / 权限）
        return None


def _trim(path) -> None:
    """超限就砍掉前半段，保留最近 ``_KEEP_LINES`` 行。"""
    try:
        if path.stat().st_size <= _MAX_BYTES:
            return
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        path.write_text("\n".join(lines[-_KEEP_LINES:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def log(name: str, message: str) -> None:
    """往 ``logs/<name>.log`` 追加一行，形如 ``2026-11-21 03:58:12  内容``。

    ``name`` 只当文件名用，别传路径——``log_dir()`` 已经决定了落点。
    """
    path = _log_path(name)
    if path is None:
        return
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        _trim(path)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp}  {message}\n")
    except OSError:
        pass


def install_qt_handler() -> None:
    """接管 Qt 消息，Warning 及以上写进 ``logs/qt.log``。重复调用无副作用。"""
    global _qt_installed
    if _qt_installed:
        return
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except ImportError:  # 没有 PySide6 的脚本不该因为这条挂掉
        return

    levels = {
        QtMsgType.QtWarningMsg: "WARN",
        QtMsgType.QtCriticalMsg: "CRIT",
        QtMsgType.QtFatalMsg: "FATAL",
    }

    def handler(msg_type, _context, message: str) -> None:
        tag = levels.get(msg_type)
        if tag is None:
            return
        log("qt", f"{tag}  {message}")
        # 控制台调试时仍希望能看见（devnull 时 write 是空操作）
        stream = sys.stderr
        if stream is not None:
            try:
                stream.write(f"[qt {tag}] {message}\n")
            except (OSError, ValueError):
                pass

    qInstallMessageHandler(handler)
    _qt_installed = True


__all__ = ["install_qt_handler", "log"]
