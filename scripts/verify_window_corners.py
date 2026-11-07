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
