"""每台摄像头的「上次画面」，用来掩盖首帧那几秒的黑屏。

为什么要这个：实测接入一路摄像头到真出画面要 5–7 秒，其中 **3 秒左右纯粹在等摄像
头的下一个关键帧**（GOP 3.05 秒，而会话起点那个关键帧到不了我们手里），剩下的是
云端换票 + P2P 握手 + 一个 177 KB 的关键帧本身要传近 1 秒。这一段没法从客户端消
掉，米家 App 也是这么过的——它只是**立刻显示上次的静止画面**，所以"感觉"是秒开。

所以这里存一张缩到 960 宽的 JPEG（几十 KB），下次进详情页先把这张垫在画面区，
标注「上次的画面」，实时流一到就替换掉。只在本地 `<data>/snapshots/` 落盘，
不上传、不进仓库。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from ..core import paths

SNAPSHOT_WIDTH = 960
JPEG_QUALITY = 80


def path_for(did: str) -> Path:
    return paths.data_dir() / "snapshots" / f"{did}.jpg"


def load(did: str) -> QImage | None:
    """读上次的画面。没有、读坏了都返回 None——占位图不值得让页面报错。"""
    if not did:
        return None
    file = path_for(did)
    if not file.is_file():
        return None
    image = QImage(str(file))
    return None if image.isNull() else image


def save(did: str, image: QImage) -> None:
    """把当前帧缩图存下来。写临时文件再替换，避免下次启动读到半张图。"""
    if not did or image.isNull():
        return
    scaled = image
    if image.width() > SNAPSHOT_WIDTH:
        scaled = image.scaledToWidth(SNAPSHOT_WIDTH, Qt.SmoothTransformation)
    file = path_for(did)
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        tmp = file.with_name(file.name + ".tmp")
        if not scaled.save(str(tmp), "JPEG", JPEG_QUALITY):
            return
        tmp.replace(file)
    except OSError:
        # 磁盘满 / 目录只读：占位图存不上不影响正常播放
        pass


__all__ = ["load", "path_for", "save"]
