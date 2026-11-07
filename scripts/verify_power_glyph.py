# SPDX-License-Identifier: GPL-3.0-or-later
"""设备卡片上电源符号 ⏻ 的离线验证。

这个符号只有 13px 大，画错了肉眼一眼能看出但很难用语言描述清楚，所以直接量像素。
分两种口径量，各管一件事：

* **真实尺寸（32px）上量缺口看得见吗。** 缺口在正上方，竖线正好从中间穿过去，
  肉眼看到的是"缺口弦宽 − 竖线宽"这点缝。原来的 40° 缺口弦宽 4.4px，扣掉 1.8px
  笔宽后每边只剩 1.3px，等于没有缺口。这一条只能在 1:1 上看，放大就失真了。
* **放大后量几何比例。** 竖线长度、圆环上下左右半径是否一致——这些跟像素大小无关，
  但 1:1 下抗锯齿会把 0.3px 的差糊成 1px，所以放到 8 倍再量。

脚本里保留了**改动前的画法**作对照，跑一遍能看到它在这几条上全线不过，
说明断言不是摆设。

用法：``.venv/Scripts/python.exe -X utf8 scripts/verify_power_glyph.py``
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPen  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mihome.core.store import DeviceState  # noqa: E402
from mihome.core.tree import DeviceNode  # noqa: E402
from mihome.ui import theme  # noqa: E402
from mihome.ui.device_card import POWER_D, DeviceCard  # noqa: E402

FAILS: list[str] = []
D = POWER_D
ZOOM = 8


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(f"{name} {detail}")


# ---------------------------------------------------------------------------
# 两种画法
# ---------------------------------------------------------------------------
def glyph_old(p: QPainter, rect: QRectF, color: str) -> None:
    """改动前：缺口 40°、竖线 1.5r、弧容器整体偏下 0.1r。"""
    c = rect.center()
    r = rect.width() * 0.20
    p.setPen(QPen(QColor(color), 1.8, Qt.SolidLine, Qt.RoundCap))
    p.setBrush(Qt.NoBrush)
    p.drawArc(QRectF(c.x() - r, c.y() - r * 0.9, r * 2, r * 2), 110 * 16, 320 * 16)
    p.drawLine(QPoint(round(c.x()), round(c.y() - r * 1.7)),
               QPoint(round(c.x()), round(c.y() - r * 0.2)))


def glyph_new(p: QPainter, rect: QRectF, color: str) -> None:
    """现在用的真实现。"""
    DeviceCard._paint_power_glyph(p, rect, color)


# ---------------------------------------------------------------------------
# 渲染与测量
# ---------------------------------------------------------------------------
class Shot:
    """一张 32×32 的电源键（``zoom`` 倍），可以量了。"""

    def __init__(self, img: QImage, pal: theme.Palette, on: bool, zoom: int) -> None:
        self.img = img
        self.zoom = zoom
        self.cx = img.width() // 2
        self.cy = img.height() // 2
        self.disc = Shot.disc(pal, on)
        self._bbox: tuple[int, int, int, int] | None = None

    @staticmethod
    def disc(pal: theme.Palette, on: bool) -> QColor:
        return QColor(theme.category_color("light")) if on else QColor(
            theme.blend(pal.card, pal.text, 0.08)
        )

    def ink(self, x: int, y: int) -> bool:
        if not (0 <= x < self.img.width() and 0 <= y < self.img.height()):
            return False
        c = self.img.pixelColor(x, y)
        return max(abs(c.red() - self.disc.red()),
                   abs(c.green() - self.disc.green()),
                   abs(c.blue() - self.disc.blue())) > 60

    def inside(self, x: int, y: int) -> bool:
        """只在圆盘里量。圆外是卡片色，开启态下和圆底的品类色差得远，会被误判成墨迹。"""
        r = (D / 2 - 1) * self.zoom
        return (x - self.cx) ** 2 + (y - self.cy) ** 2 <= r * r

    def bbox(self) -> tuple[int, int, int, int]:
        """圆盘内所有墨迹的包围盒。竖线只往**上**出头，所以左右和底部就是圆环本身。"""
        if self._bbox is None:
            xs: list[int] = []
            ys: list[int] = []
            for y in range(self.img.height()):
                if not self.inside(self.cx, y):
                    continue
                for x in range(self.img.width()):
                    if self.inside(x, y) and self.ink(x, y):
                        xs.append(x)
                        ys.append(y)
            self._bbox = (min(xs), min(ys), max(xs), max(ys)) if xs else (0, 0, 0, 0)
        return self._bbox

    # -- 几何（放大口径） -------------------------------------------------
    def radius_h(self) -> float:
        x0, _, x1, _ = self.bbox()
        return (x1 - x0) / 2 / self.zoom

    def radius_bottom(self) -> float:
        _, _, _, y1 = self.bbox()
        return (y1 - self.cy) / self.zoom

    def stem(self) -> float:
        """中轴列上**最上面那段**连续墨迹的长度 = 竖线（含 RoundCap 的圆头）。

        中轴列上其实有两段：竖线，和更下方的圆环底部。取第一段。
        不假设竖线连到圆心——"没连到"本身就是它显得像根天线的原因之一。
        """
        ys = [y for y in range(self.img.height())
              if self.inside(self.cx, y) and self.ink(self.cx, y)]
        if not ys:
            return 0.0
        top = prev = ys[0]
        for y in ys[1:]:
            if y != prev + 1:
                break
            prev = y
        return (prev - top + 1) / self.zoom

    def vcenter_shift(self) -> float:
        _, y0, _, y1 = self.bbox()
        return abs((y0 + y1) / 2 - self.cy) / self.zoom

    # -- 缺口（1:1 口径） -------------------------------------------------
    def gap_visible(self) -> tuple[bool, bool]:
        """环顶那一行、缺口中心左右各 2px 是背景吗。"""
        y = int(round(self.cy - self.radius_h() * self.zoom + self.zoom))
        return (not self.ink(self.cx - 2 * self.zoom, y),
                not self.ink(self.cx + 2 * self.zoom, y))


def render(fn, *, on: bool, pal: theme.Palette, zoom: int = 1) -> Shot:
    img = QImage(D * zoom, D * zoom, QImage.Format_ARGB32_Premultiplied)
    img.fill(QColor(pal.card))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(zoom, zoom)
    rect = QRectF(0, 0, D, D)
    p.setPen(Qt.NoPen)
    p.setBrush(Shot.disc(pal, on))
    p.drawEllipse(rect)
    fn(p, rect, "#FFFFFF" if on else pal.text_soft)
    p.end()
    return Shot(img, pal, on, zoom)


def audit(tag: str, fn, pal: theme.Palette, on: bool, *, expect_broken: bool = False) -> None:
    """测一版画法。``expect_broken=True`` 时期望每条都**不过**——验断言有区分度。"""
    real = render(fn, on=on, pal=pal, zoom=1)
    big = render(fn, on=on, pal=pal, zoom=ZOOM)

    left_gap, right_gap = real.gap_visible()
    rh = big.radius_h()
    rb = big.radius_bottom()
    stem = big.stem()
    ratio_stem = stem / rh if rh else 0
    ratio_ring = rb / rh if rh else 0
    shift = big.vcenter_shift()

    state = "开启" if on else "关闭"
    print(f"  · {tag}（{state}）：缺口 {'可见' if left_gap and right_gap else '不可见'}；"
          f"半径 {rh:.2f}px / 底 {rb:.2f}px；竖线 {stem:.2f}px = {ratio_stem:.2f}·r；"
          f"垂直重心偏 {shift:.2f}px")

    def record(name: str, ok: bool, detail: str = "") -> None:
        check(("改前·" if expect_broken else "") + f"{tag}·{state}：{name}",
              (not ok) if expect_broken else ok, detail)

    record("环顶两侧都露出缺口", left_gap and right_gap,
           f"左{'有' if left_gap else '无'}缝 / 右{'有' if right_gap else '无'}缝")
    record("竖线长度在 0.9~1.6 个半径之间", 0.90 <= ratio_stem <= 1.60, f"{ratio_stem:.2f}·r")
    record("圆环上下等宽（底/水平 0.95~1.06）", 0.95 <= ratio_ring <= 1.06, f"{ratio_ring:.3f}")
    record("符号垂直重心不偏（≤ 1.5px）", shift <= 1.5, f"{shift:.2f}px")


def section_card() -> None:
    """端到端：造一张真的 1×1 卡片，走一遍 ``paintEvent``，再从抓图里量缺口。

    前面几节是直接调 ``_paint_power_glyph``；这里验的是"它真的被画到卡片右上角了"，
    而且圆底和图标在两种主题下都还在。卡片只借来跑一遍绘制路径，不是被测对象，
    所以用 SimpleNamespace 拼一个最小可用的 CardSpec，不去碰真实的规格仓库。
    """
    print("3) 卡片上的实际效果")

    class _Icons:
        def ensure(self, model) -> None:  # noqa: D102
            pass

        def scaled(self, model, size):  # noqa: D102
            return None  # 没有产品图 → 卡片画一个淡圆占位，不影响电源键

    def state(on: bool):
        prop = SimpleNamespace(key=(2, 1), siid=2, piid=1)
        ctl = SimpleNamespace(slot="power", prop=prop)
        card = SimpleNamespace(
            category="light", gangs=[], span=1, controls=[ctl], metrics=[],
            layout="light", power_actions=None,
            control=lambda slot: ctl if slot == "power" else None,
        )
        node = DeviceNode("d1", "客厅吸顶灯", "m", "light.x", home="我的家", room="客厅")
        return DeviceState(node=node, spec=None, card=card, values={(2, 1): on})

    panes: list[QImage] = []
    for pal in (theme.LIGHT, theme.DARK):
        for on in (False, True):
            card = DeviceCard(state(on), _Icons(), pal)
            card.show()
            QApplication.processEvents()
            img = card.grab().toImage()
            crop = img.copy(card._power_rect())
            shot = Shot(crop, pal, on, 1)
            left_gap, right_gap = shot.gap_visible()
            state_text = "开启" if on else "关闭"
            # 卡片的底色和圆底差得远，圆外会被算成墨迹，所以只看缺口不看半径
            check(f"卡片·{pal.name}·{state_text}：右上角图标露着缺口",
                  left_gap and right_gap,
                  f"左{'有' if left_gap else '无'}缝 / 右{'有' if right_gap else '无'}缝")
            panes.append(img)
            card.close()

    z = 3
    w, h = panes[0].width(), panes[0].height()
    out = QImage((w * z + 24) * 2 + 24, (h * z + 24) * 2 + 24, QImage.Format_ARGB32)
    out.fill(QColor("#EDEFF2"))
    p = QPainter(out)
    for i, img in enumerate(panes):
        r, col = divmod(i, 2)
        p.drawImage(24 + col * (w * z + 24), 24 + r * (h * z + 24),
                    img.scaled(w * z, h * z, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    p.end()
    cards_path = Path("scripts/out/power_glyph_cards.png")
    out.save(str(cards_path))
    print(f"  · 卡片效果图 {cards_path}（左上浅色关 / 右上浅色开 / 左下深色关 / 右下深色开）")


def main() -> int:
    app = QApplication(sys.argv[:1])  # noqa: F841

    print("1) 符号本身（8 倍量几何，1:1 量缺口）")
    for pal in (theme.LIGHT, theme.DARK):
        print(f"{pal.name} 主题")
        audit("改前", glyph_old, pal, on=False, expect_broken=True)
        for on in (False, True):
            audit("现在", glyph_new, pal, on)
        print()

    print("2) 对照图")
    # 真实尺寸渲染后平滑放大，左二=改前 关/开，右二=现在 关/开
    z = 8
    out = QImage(D * z * 4 + 60, D * z, QImage.Format_ARGB32)
    out.fill(QColor("#FFFFFF"))
    p = QPainter(out)
    x0 = 0
    for fn in (glyph_old, glyph_new):
        for on in (False, True):
            img = render(fn, on=on, pal=theme.LIGHT, zoom=1).img
            p.drawImage(x0, 0, img.scaled(D * z, D * z, Qt.KeepAspectRatio,
                                          Qt.SmoothTransformation))
            x0 += D * z + 20
    p.end()
    path = Path("scripts/out/power_glyph.png")
    path.parent.mkdir(parents=True, exist_ok=True)
    out.save(str(path))
    print(f"  · {path}\n")

    section_card()

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败")
        for line in FAILS:
            print("  -", line)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
