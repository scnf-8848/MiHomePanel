# SPDX-License-Identifier: GPL-3.0-or-later
"""验证渐变背景的抖动：随机取整（stochastic rounding），而不是"渐变 + 噪点"。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_backdrop_dither.py``

**钉的是踩过的坑**：``QLinearGradient`` 光栅化到 8-bit，两 stop 拉满整屏时量化台阶能宽到
120 物理像素（深色「夜色」#142438→#0B1521 只差 9～23 级，斜跨 2380×1575），在平滑底色上
就是一条肉眼可见的斜分界线——用户 11-16 截图上那一条条"渐变带"就是它。

第一版修法（白黑各半的 α3 噪点）**没能去掉它**：恒定密度的噪点只改变方差的纹理，不改均值，
低通之后台阶照旧。所以这里的关键判据不是"有没有噪点"，而是：

* 沿渐变方向每 16 物理像素取一条窗，窗内**均值**与理想渐变的差 ≤ 0.2 级（台阶会被判出来）；
* 整图均值不许比理想值漂（不许靠"整体提亮"盖台阶）。

两条都要，且第三条要求用"直接四舍五入"跑同一套判据**必须失败**——否则判据是永真的空话。

2026-11-17 起 ``_dithered_image`` 支持任意角度（不再只认 45°/135° 那一族），所以这里的
几何检查也从"四种对角角度 × 四角"扩成"十来种角度（含竖直 / 水平 / 30° / 270°）"，
窗均值那条判据也改成**按投影分箱**（不再假设 ``t = x - y``）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from mihome.ui import chrome, theme  # noqa: E402

FAILS: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {got!r}")
    if not ok:
        FAILS.append(f"{name} → 期望 {want!r}，实际 {got!r}")


def check_true(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(f"{name} {detail}")


app = QApplication.instance() or QApplication(sys.argv[:1])

#: 深色「夜色」原来的两色值——通道只差 9～23 级，是量化台阶最宽（最容易翻车）的一档。
#: **故意写死不读预设**：预设从 2026-11-17 起是 5 个色标，而下面"整图均值 == 中点
#: 理想值"这条判据只在**两端线性**渐变上成立（多色标时均值不等于中点插值，会误判成
#: 3.47 级的漂移）。这里测的是抖动算法，不是配色，所以把输入固定住。
SURFACE = theme.Surface("gradient", ("#142438", "#0B1521"), 135.0, "#2D3745", 0.88)
FLAT = theme.palette_for("mono", "dark").surface
PW, PH = 600, 400
#: 覆盖三种几何：对角（45/135/225/315）、斜角（任意 dx/dy 比）、纯竖直 / 纯水平。
ANGLES = (0.0, 30.0, 45.0, 60.0, 90.0, 120.0, 135.0, 150.0, 225.0, 270.0, 315.0, 330.0)


def ideal_fn(surface, pw: int, ph: int):
    """理想色值函数。

    渐变线几何取自 ``Surface.brush``（和实现共享同一个来源），**插值自己写**：
    这样它不会因为实现里那套 proj 公式改了就跟着一起错。
    """
    grad = surface.brush(QRectF(0, 0, pw, ph))
    p0, p1 = grad.start(), grad.finalStop()
    vx, vy = p1.x() - p0.x(), p1.y() - p0.y()
    denom = (vx * vx + vy * vy) or 1.0
    stops = [QColor(c) for c in surface.stops]
    last = len(stops) - 1

    def at(x: float, y: float) -> tuple[float, float, float]:
        proj = ((x - p0.x()) * vx + (y - p0.y()) * vy) / denom
        proj = 0.0 if proj < 0.0 else (1.0 if proj > 1.0 else proj)
        pos = proj * last
        j = min(int(pos), last - 1) if last else 0
        f = pos - j if last else 0.0
        a, b = stops[j], stops[j + 1 if last else 0]
        return (a.red() + (b.red() - a.red()) * f,
                a.green() + (b.green() - a.green()) * f,
                a.blue() + (b.blue() - a.blue()) * f)

    return at


print("== 1. 任意角度都要和 Surface.brush 的渐变几何吻合 ==")
CORNERS = ((6, 6), (PW - 30, 6), (6, PH - 30), (PW - 30, PH - 30), (PW // 2 - 12, PH // 2 - 12))
for angle in ANGLES:
    surf = theme.Surface("gradient", ("#1E3A8A", "#DC2626"), angle, "#2D3745", 0.88)
    img = chrome._dithered_image(PW, PH, surf)
    want_at = ideal_fn(surf, PW, PH)
    worst = 0.0
    for cx, cy in CORNERS:
        got = [0.0, 0.0, 0.0]
        for y in range(cy, cy + 24, 2):
            for x in range(cx, cx + 24, 2):
                c = img.pixelColor(x, y)
                got[0] += c.red()
                got[1] += c.green()
                got[2] += c.blue()
        got = [v / 144 for v in got]
        want = want_at(cx + 12, cy + 12)
        worst = max(worst, max(abs(got[ch] - want[ch]) for ch in range(3)))
    check_true(f"{angle:5.1f}°：四角 + 中心色值与渐变几何吻合（≤ 2 级）",
               worst <= 2.0, f"最大偏差 {worst:.2f} 级")

print()
print("== 2. 抖动图本身：尺寸 / 不透明 / 缓存 ==")
img = chrome._dithered_image(PW, PH, SURFACE)
check("尺寸就是物理像素", (img.width(), img.height()), (PW, PH))
check("格式", img.format(), QImage.Format_ARGB32_Premultiplied)
alphas = {QColor(img.pixelColor(x, y)).alpha()
          for y in range(0, PH, 37) for x in range(0, PW, 41)}
check("整张图不透明（不是半透明叠层）", alphas, {255})

pm = chrome._backdrop_pixmap(PW, PH, 1.75, SURFACE)
check_true("同一 key 命中缓存（同一对象）",
           chrome._backdrop_pixmap(PW, PH, 1.75, SURFACE) is pm)
check("pixmap 带 dpr", round(pm.devicePixelRatio(), 2), 1.75)
for w in ((PW - 7, PH), (PW, PH - 5), (PW + 9, PH)):
    chrome._backdrop_pixmap(w[0], w[1], 1.75, SURFACE)
check_true("缓存只留最近几张（不随 resize 无限涨）",
           len(chrome._backdrop_cache) <= chrome._BACKDROP_CACHE_MAX,
           f"现在 {len(chrome._backdrop_cache)} 张")
chrome._backdrop_cache.clear()


def window_error(image, surface, width: int = 16) -> tuple[float, float]:
    """沿**渐变方向**按 ``width`` 像素分箱，返回（箱均值和理想值的最大偏差, 最大箱的投影位置）。

    投影自己算（和 ``ideal_fn`` 同一套），箱子按投影等分——这样对角 / 斜角 / 竖直
    三种几何用同一条判据，不再假设 ``t = x - y``。
    """
    grad = surface.brush(QRectF(0, 0, image.width(), image.height()))
    p0, p1 = grad.start(), grad.finalStop()
    vx, vy = p1.x() - p0.x(), p1.y() - p0.y()
    denom = (vx * vx + vy * vy) or 1.0
    want_at = ideal_fn(surface, image.width(), image.height())
    # 渐变线在整图上跨过的长度（像素）决定该切多少箱
    span = (abs(image.width() * vx) + abs(image.height() * vy)) / (vx * vx + vy * vy) ** 0.5
    bins = max(4, int(round(span / width)))
    acc: dict[int, list[float]] = {}
    for y in range(2, image.height() - 2, 2):
        for x in range(2, image.width() - 2, 2):
            proj = ((x - p0.x()) * vx + (y - p0.y()) * vy) / denom
            proj = 0.0 if proj < 0.0 else (1.0 if proj > 1.0 else proj)
            c = image.pixelColor(x, y)
            slot = acc.setdefault(min(bins - 1, int(proj * bins)), [0.0, 0.0, 0.0, 0])
            slot[0] += c.red()
            slot[1] += c.green()
            slot[2] += c.blue()
            slot[3] += 1
    worst, worst_u = 0.0, 0.0
    for idx, (r, g, b, n) in acc.items():
        # 采样太少的箱不参与：贴角的那一两箱只有二三十个像素，±0.5 级均匀抖动本身的
        # 标准误就是 0.29/√n（n=20 时 0.065），统计噪声会盖过台阶判据。
        # 角落的几何正确性由第 1 节（144 像素均值、四角 + 中心）单独盯。
        if n < 80:
            continue
        u = (idx + 0.5) / bins
        want = want_at(p0.x() + u * vx, p0.y() + u * vy)
        err = max(abs(r / n - want[0]), abs(g / n - want[1]), abs(b / n - want[2]))
        if err > worst:
            worst, worst_u = err, u
    return worst, worst_u


print()
print("== 3. 关键判据：沿渐变方向 16px 窗均值要贴着理想值（台阶会被抓出来）==")
for angle in (135.0, 30.0, 270.0):
    surf = theme.Surface("gradient", ("#142438", "#0B1521"), angle, "#2D3745", 0.88)
    image = chrome._dithered_image(PW, PH, surf)
    err, u = window_error(image, surf)
    print(f"        {angle:5.1f}° 最差窗偏差 {err:.3f} 级（投影 {u:.2f} 处）")
    check_true(f"{angle:5.1f}° 随机取整：窗均值偏差 ≤ 0.2 级", err <= 0.2, f"{err:.3f}")


def render_naive(surface) -> QImage:
    """对照：理想渐变直接四舍五入（= ``QLinearGradient`` 光栅化的效果），不做随机取整。"""
    want_at = ideal_fn(surface, PW, PH)
    out = QImage(PW, PH, QImage.Format_ARGB32_Premultiplied)
    for y in range(PH):
        for x in range(PW):
            r, g, b = want_at(x + 0.5, y + 0.5)
            out.setPixelColor(x, y, QColor(round(r), round(g), round(b)))
    return out


naive = render_naive(SURFACE)
err_naive, u_naive = window_error(naive, SURFACE)
print(f"        直接四舍五入最差窗偏差 {err_naive:.3f} 级（投影 {u_naive:.2f} 处）")
check_true("判据不是永真：四舍五入会被同一条判据判失败", err_naive > 0.35, f"{err_naive:.3f}")

print()
print("== 4. 平均色不许漂（不能靠整体提亮盖台阶）==")
mid = ideal_fn(SURFACE, PW, PH)(PW / 2, PH / 2)


def mean_rgb(image) -> tuple[float, float, float]:
    tot = [0.0, 0.0, 0.0]
    n = 0
    for y in range(2, image.height() - 2, 3):
        for x in range(2, image.width() - 2, 3):
            c = image.pixelColor(x, y)
            tot[0] += c.red()
            tot[1] += c.green()
            tot[2] += c.blue()
            n += 1
    return (tot[0] / n, tot[1] / n, tot[2] / n)


got = mean_rgb(img)
drift = max(abs(got[ch] - mid[ch]) for ch in range(3))
print(f"        整图均值 {tuple(round(v, 2) for v in got)}  理想中点 {tuple(round(v, 2) for v in mid)}")
check_true("新画法平均色偏离 ≤ 0.6 级", drift <= 0.6, f"{drift:.2f}")

print()
print("== 5. 纯色背景不生成抖动图（白费一次全窗绘制）==")
w = QWidget()
frame = chrome.WindowFrame(w, fill=lambda: FLAT.stops[0], backdrop=lambda: FLAT)
check_true("纯色 surface 不给 shot",
           frame._backdrop_shot(FLAT, QRectF(0, 0, 320, 240)) is None)
w.resize(320, 240)
w.show()
app.processEvents()
row = [QColor(w.grab().toImage().pixelColor(x, 120)).red() for x in range(40, 280)]
w.close()
check_true("纯色背景相邻像素完全平坦", len(set(row)) == 1, f"{len(set(row))} 种取值")

print()
print("== 6. 源码级：渐变分支必须走抖动图 + 圆角裁剪 ==")
src = (Path(__file__).resolve().parent.parent / "mihome/ui/chrome.py").read_text(encoding="utf-8")
check_true("_paint_fill 渐变分支取 shot", "shot = self._backdrop_shot(surface, rect)" in src)
check_true("非最大化用 path 裁剪（圆角不会糊成直角）",
           "p.setClipPath(path)" in src and "p.setClipping(False)" in src)
check_true("尺寸用 ceil（否则右/下会漏一条底色）", "math.ceil(rect.width() * dpr)" in src)
check_true("抖动是随机取整（±0.5 级）而不是半黑半白噪点",
           "rng.random() - 0.5" in src, "见 _dithered_image")
check_true("每帧只 drawPixmap（不是每帧重算渐变）", "p.drawPixmap(rect.topLeft(), shot)" in src)
check_true("不再有'只认对角角度'的分支（任意角度都抖动）",
           "_diagonal_axis" not in src and "_DITHER_TILE" not in src)

print()
if FAILS:
    print(f"{len(FAILS)} 项失败：")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("全部通过")
