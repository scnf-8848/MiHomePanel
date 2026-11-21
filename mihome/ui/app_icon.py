"""程序图标（任务栏 / Alt-Tab / 窗口左上角 / exe 自身）。

图标本体是 ``assets/app.ico``，由 ``scripts/make_icon.py --ico`` 生成。那份脚本里
**每个尺寸是分别画的**（16/24/32 三档换了几何：砍掉屏幕、加粗描边），所以这里只
负责"把文件找到、读出来"，绝不在运行时缩放——拿 256 缩到 16 会把三档细节全抹平，
正是生成脚本一直在躲的糊法。

路径不能只按仓库根算。PyInstaller 打包后代码在 ``_internal``（onedir）或临时解包
目录（onefile），``Path(__file__)`` 那条链直接失效——开发机上好好的，打包版图标
一片空白。候选顺序照抄 ``core/camera.py: search_dirs()`` 那一套：exe 旁边优先
（用户能替换），其次解包目录，最后才是源码树。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon

_cache: QIcon | None = None


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


__all__ = ["load", "path", "search_dirs"]
