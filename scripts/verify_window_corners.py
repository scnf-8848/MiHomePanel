# SPDX-License-Identifier: GPL-3.0-or-later
"""窗口圆角是否抗锯齿：直接看四角的 alpha 渐变，不需要联网。

用法：``.venv/Scripts/python.exe -X utf8 scripts/verify_window_corners.py``

判断标准很土但有效：一个 12dp 的圆弧在 1x 屏上有 13 个像素，**边缘像素的 alpha
必须是渐变的**（0 < a < 255 的点数 ≥ 8）。setMask 方案下这里全是 0/255。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget  # noqa: E402

from mihome.ui import chrome, theme  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(f"{name} {detail}")


def corner_alphas(img, rect: tuple[int, int, int, int]) -> list[int]:
    x0, y0, w, h = rect
    out = []
    for y in range(y0, y0 + h):
        for x in range(x0, x0 + w):
            out.append(QColor(img.pixelColor(x, y)).alpha())
    return out


def sample_luma(img, x: int, y: int, r: int = 2) -> float:
    """一小块区域的 luma 均值。

    背景叠了 ±1 级抖动噪点（见 ``chrome._DITHER_ALPHA``），单点取样会落在量化台阶
    边上，差 1～2 级就能把判据掀翻。看"这块区域多亮"才是这条断言的本来意思。
    """
    vals = []
    for yy in range(y - r, y + r + 1):
        for xx in range(x - r, x + r + 1):
            if 0 <= xx < img.width() and 0 <= yy < img.height():
                vals.append(theme.luma(QColor(img.pixelColor(xx, yy)).name()))
    return sum(vals) / len(vals) if vals else 0.0


def build_plain(pal: theme.Palette, *, old: bool, bordered: bool = False):
    """一个最小的"主窗口"：同样的内容，只换 WindowFrame 的画法。

    ``old=True`` 走重构前的纯色路径（只传 ``fill``）；``old=False`` 走现在的
    ``Surface`` 背景路径。两组必须逐像素一致（第 5 节）。
    """
    w = QWidget()
    w.resize(360, 240)
    if old:
        frame = chrome.WindowFrame(w, resizable=True, fill=lambda: pal.bg)
    else:
        frame = chrome.WindowFrame(
            w, resizable=True, fill=lambda: pal.bg,
            border=(lambda: theme.window_edge(pal)) if bordered else None,
            backdrop=lambda: pal.surface,
        )
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(chrome.TitleBar("测试", pal, frame=frame))
    lay.addWidget(QLabel("正文"))
    w.show()
    for _ in range(3):
        QApplication.processEvents()
    return w, frame


def main() -> int:
    app = QApplication(sys.argv[:1])  # noqa: F841

    pal = theme.LIGHT
    win = QWidget()
    win.resize(320, 200)
    frame = chrome.WindowFrame(win, resizable=True, fill=lambda: pal.bg)
    lay = QVBoxLayout(win)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(chrome.TitleBar("测试", pal, frame=frame))
    lay.addWidget(QLabel("正文"))
    win.show()
    app.processEvents()

    img = win.grab().toImage()
    print(f"1) 角部 alpha（{img.width()}x{img.height()}）")
    r = chrome.WINDOW_RADIUS
    for name, rect in (
        ("左上", (0, 0, r, r)),
        ("右上", (img.width() - r, 0, r, r)),
        ("左下", (0, img.height() - r, r, r)),
        ("右下", (img.width() - r, img.height() - r, r, r)),
    ):
        alphas = corner_alphas(img, rect)
        partial = sum(1 for a in alphas if 8 < a < 248)
        check(f"{name}角有渐变像素", partial >= 8, f"{partial} 个中间态")

    print("\n2) 角外必须是透明的（否则窗口会是一块方角）")
    for name, (x, y) in (
        ("左上", (0, 0)),
        ("右上", (img.width() - 1, 0)),
        ("左下", (0, img.height() - 1)),
        ("右下", (img.width() - 1, img.height() - 1)),
    ):
        a = QColor(img.pixelColor(x, y)).alpha()
        check(f"{name}角尖透明", a < 40, f"alpha={a}")

    print("\n3) 标题栏顶部两角也跟着圆（否则会盖掉窗口的弧）")
    # 角尖已经透明（上一条），这里看的是"弧内第一块颜色应当是标题栏的"
    inside = QColor(img.pixelColor(5, 5))
    check("弧内像素是标题栏底色", inside.alpha() > 200, f"alpha={inside.alpha()} {inside.name()}")
    check("弧内不是窗口底色（标题栏没被裁掉）",
          inside.name().upper() != QColor(pal.bg).name().upper(), inside.name())

    print("\n4) 直边不能发虚（0.5 内缩的意义）")
    edge = [QColor(img.pixelColor(x, img.height() // 2)).alpha() for x in range(0, 3)]
    check("左边缘第一列就是实心", edge[0] > 240, f"{edge}")

    print("\n5) 纯色路径：旧画法（只有 fill）与新画法（Surface 背景）逐像素一致")
    for p in (theme.LIGHT, theme.DARK):
        wa, _fa = build_plain(p, old=True)
        wb, _fb = build_plain(p, old=False)
        a = wa.grab().toImage()
        b = wb.grab().toImage()
        diff = 0
        for y in range(min(a.height(), b.height())):
            for x in range(min(a.width(), b.width())):
                if a.pixel(x, y) != b.pixel(x, y):
                    diff += 1
        check(f"{p.name}：新旧纯色路径无差异", diff == 0, f"{diff} 个像素")
        wa.close()
        wb.close()

    print("\n6) 主窗口四种组合：1px 外框 + 圆角 + 最大化分支")
    for preset in ("mono", "dawn"):
        for mode in ("light", "dark"):
            p = theme.palette_for(preset, mode)
            w, frame = build_plain(p, old=False, bordered=True)
            im = w.grab().toImage()
            y = im.height() // 2
            border_px = QColor(im.pixelColor(0, y))
            inner_px = QColor(im.pixelColor(4, y))
            check(f"{preset}/{mode}：最外一列是外框色",
                  border_px.name().upper() == QColor(theme.window_edge(p)).name().upper(),
                  f"{border_px.name()} vs {theme.window_edge(p)}")
            check(f"{preset}/{mode}：外框与内部拉得开（ΔL ≥ 8）",
                  abs(border_px.lightness() - inner_px.lightness()) >= 8,
                  f"{border_px.lightness()} vs {inner_px.lightness()}")
            corners = [(0, 0), (im.width() - 1, 0),
                       (0, im.height() - 1), (im.width() - 1, im.height() - 1)]
            check(f"{preset}/{mode}：非最大化四角透明",
                  all(QColor(im.pixelColor(x, yy)).alpha() < 40 for x, yy in corners),
                  str([QColor(im.pixelColor(x, yy)).alpha() for x, yy in corners]))
            if p.surface.is_gradient():
                # 135° 的起点在右上（第一段颜色）、终点在左下（最后一段），
                # 所以取样点是"右上 / 左下"，不是左上 / 右下。
                # 判据用 Rec.601 luma，不用 QColor.lightness()：粉彩渐变两端
                # （暖奶油 vs 雾蓝）HSL 明度几乎一样，方向主要靠色相 + luma 区分，
                # 拿 HSL 明度当判据会把"一眼看得出方向"的渐变判成平的。
                # **取 5×5 均值而不是单点**：背景叠了 ±1 级抖动噪点
                # （``chrome._DITHER_ALPHA``），单点落在量化台阶边上会差 1～2 级，
                # 这条判据本来就该看"这块区域多亮"，不该被一颗噪点掀翻。
                light_end = sample_luma(im, im.width() - 8, chrome.BAR_H + 16)
                dark_end = sample_luma(im, 8, im.height() - 16)
                check(f"{preset}/{mode}：渐变方向可见（对角 Δluma ≥ 6）",
                      abs(light_end - dark_end) >= 6, f"{light_end:.1f} vs {dark_end:.1f}")

            frame.set_maximized(True)
            for _ in range(3):
                app.processEvents()
            im2 = w.grab().toImage()
            y2 = im2.height() // 2
            e2 = QColor(im2.pixelColor(0, y2))
            i2 = QColor(im2.pixelColor(4, y2))
            check(f"{preset}/{mode}：最大化时不画外框（贴边）",
                  abs(e2.lightness() - i2.lightness()) <= 2,
                  f"{e2.lightness()} vs {i2.lightness()}")
            check(f"{preset}/{mode}：最大化四角不透明",
                  all(QColor(im2.pixelColor(x, yy)).alpha() > 240
                      for x, yy in ((0, 0), (im2.width() - 1, 0),
                                    (0, im2.height() - 1),
                                    (im2.width() - 1, im2.height() - 1))),
                  "")
            frame.set_maximized(False)
            w.close()

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
