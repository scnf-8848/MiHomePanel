# SPDX-License-Identifier: GPL-3.0-or-later
"""监控页左侧列表的缩略图：**这一路播过的最后一帧**，缩小、模糊、存盘。

为什么不是实时小画面：缩略图也取流等于每台设备再多一路解码。监控页本来就要同屏
6 路（每路软解约 40% 单核），再加 6 路缩略图就是翻倍，还会各占一个观看位。而列表的
用途只有两个——认出是哪台、并且能拖到右边；一张"上次看到的画面"足够，且不占资源。

为什么模糊（2026-11-19 用户定）：2960×1666 的监控画面缩到 168×94 会满屏锯齿，
看起来像画质坏了；模糊之后更像"索引缩略图"，而不是一台缩得很小的监视器。

怎么模糊：**降到 1/8 再平滑放大回来**。这是最便宜的模糊（几次缩放，纯 CPU memcpy
量级），也顺带给了我们一个只有 21×12 的中间图。不用 ``QGraphicsBlurEffect``——
它得把图塞进 ``QGraphicsScene`` 渲染一遍，为了 40 KB 的缩略图引入整套场景对象，
还要处理 DPI 缩放，代价远大于收益。

存盘而不是只放内存：程序重启后列表里仍然"认得出是哪路"，比一片灰图标有用。
一张 JPEG 约 6–10 KB，十几台设备不到 100 KB，且它在 ``cache/`` 下，随时可删。

同一张图有**两个用处**（2026-11-19 用户定）：左侧列表的小缩略图，以及没在播时
格子里的整幅封面（"模糊保存画面 + 播放键"）。所以存档尺寸取**大的那个**
（``POSTER_W×POSTER_H``，够铺满 1+5 的主窗），列表那边画的时候缩到
``THUMB_W×THUMB_H``——只存一份，不必为一个画面留两个文件。
"""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QImage, QPixmap

from ..core import paths

#: 列表里那张图的**显示**尺寸（16:9）。宽度对齐缩略图栏的内宽，别再按控件宽缩放
THUMB_W = 168
THUMB_H = 94
#: 落盘 / 内存里存的尺寸：既是列表缩略图的源，也是没在播时格子里的整幅封面。
#: 480 宽够铺满 1+5 的主窗（约 500×280）；再大只是让 JPEG 变胖。
POSTER_W = 480
POSTER_H = 270
#: 模糊因子：先缩到这个比例再放大回去。8 已经糊成一片，4 还看得出轮廓
BLUR_DIVISOR = 8
#: JPEG 质量：缩略图不需要细节，70 一张约 6 KB
JPEG_QUALITY = 70

_SAFE = re.compile(r"[^0-9A-Za-z_-]")


def _safe_name(did: str) -> str:
    """did → 文件名。did 实测是纯数字，但仍要挡掉路径分隔符（防写穿目录）。"""
    return _SAFE.sub("_", str(did or ""))[:64]


def blurred(image: QImage) -> QImage:
    """把一帧裁成 16:9、缩到封面大小再模糊。返回的图可直接存盘或画。

    输出是 ``POSTER_W×POSTER_H``（列表缩略图由绘制端缩小，见模块顶部说明）。
    """
    if image.isNull():
        return image
    # 先按 cover 裁：画面比例和缩略图不一致时（超宽 / 竖屏）留黑边很难看，
    # 裁掉两边反而更像"这一路"的取景
    target = POSTER_W / POSTER_H
    w, h = image.width(), image.height()
    if w <= 0 or h <= 0:
        return image
    if w / h > target:
        keep = int(round(h * target))
        image = image.copy(QRect((w - keep) // 2, 0, keep, h))
    elif w / h < target:
        keep = int(round(w / target))
        image = image.copy(QRect(0, (h - keep) // 2, w, keep))
    small = image.scaled(
        POSTER_W, POSTER_H, Qt.KeepAspectRatio, Qt.SmoothTransformation
    )
    tiny = small.scaled(
        max(1, POSTER_W // BLUR_DIVISOR),
        max(1, POSTER_H // BLUR_DIVISOR),
        Qt.IgnoreAspectRatio,
        Qt.SmoothTransformation,
    )
    return tiny.scaled(
        POSTER_W, POSTER_H, Qt.IgnoreAspectRatio, Qt.SmoothTransformation
    )


class ThumbCache:
    """``did → 模糊缩略图``，内存优先、磁盘兜底。**只在主线程用**（QPixmap 不是线程安全的）。"""

    def __init__(self, directory: Path | None = None) -> None:
        if directory is None:
            self._dir = paths.thumb_cache_dir()  # 这个函数自己会 mkdir
        else:
            self._dir = Path(directory)
            # 显式传目录时也要建：测试与打包环境里没人替我们先建好，
            # 少了这一步 save() 会静默失败（只进内存、不落盘）
            self._dir.mkdir(parents=True, exist_ok=True)
        self._mem: dict[str, QPixmap] = {}

    def path(self, did: str) -> Path:
        return self._dir / f"{_safe_name(did)}.jpg"

    def get(self, did: str) -> QPixmap | None:
        """内存 → 磁盘 → ``None``（没有就是没播过，界面画图标占位）。"""
        name = _safe_name(did)
        if not name:
            return None
        pix = self._mem.get(name)
        if pix is not None:
            return pix
        f = self.path(did)
        if not f.exists():
            return None
        pix = QPixmap(str(f))
        if pix.isNull():
            return None
        self._mem[name] = pix
        return pix

    def put(self, did: str, image: QImage) -> QPixmap | None:
        """吃掉一帧：缩小模糊后存内存与磁盘。失败（磁盘只读等）只影响下次启动。"""
        name = _safe_name(did)
        out = blurred(image)
        if not name or out.isNull():
            return None
        pix = QPixmap.fromImage(out)
        self._mem[name] = pix
        try:
            out.save(str(self.path(did)), "JPG", JPEG_QUALITY)
        except (OSError, ValueError):
            # 缓存写不进去不是错误：内存里这张仍然能用，只是重启后回到图标
            pass
        return pix

    def drop(self, did: str) -> None:
        name = _safe_name(did)
        self._mem.pop(name, None)
        try:
            self.path(did).unlink(missing_ok=True)
        except OSError:
            pass

    def prune(self, keep: set[str]) -> int:
        """删掉已经不在设备列表里的缩略图，返回清掉几张（换家庭 / 换账号后跑的）。"""
        keep_names = {_safe_name(d) for d in keep}
        removed = 0
        try:
            files = list(self._dir.glob("*.jpg"))
        except OSError:
            return 0
        for f in files:
            if f.stem in keep_names:
                continue
            try:
                f.unlink()
                removed += 1
            except OSError:
                continue
        for name in list(self._mem):
            if name not in keep_names:
                self._mem.pop(name, None)
        return removed


__all__ = [
    "BLUR_DIVISOR",
    "POSTER_H",
    "POSTER_W",
    "THUMB_H",
    "THUMB_W",
    "ThumbCache",
    "blurred",
]
