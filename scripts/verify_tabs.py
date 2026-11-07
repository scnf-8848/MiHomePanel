# SPDX-License-Identifier: GPL-3.0-or-later
"""验证家庭标签栏的两种排列 × 三种激活样式，不需要真实账号。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_tabs.py``

重点不是"画得好不好看"（那是截图脚本的事），而是几件容易写错的事：

* ``active_first`` 只改绘制顺序，**不改** ``Store.homes()`` 的设定顺序；
* 重排之后点击命中的必须是眼前那个家庭，不能点 A 切到 B；
* 三种样式下标签宽度不同，但都不应出现负宽度或重叠；
* 设置页里的预览实例不该响应点击；
* **胶囊档所有标签必须共用同一个盒模型**：以前只有激活项把文字居中、其余左对齐，
  激活项平白多出 14px 左偏移（用户看到"激活项明显偏右"），切换家庭时文字还会在
  居中/左对齐之间来回跳。这里直接量每个标签文字墨迹相对自己 rect 的偏移。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt  # noqa: E402
from PySide6.QtGui import QColor, QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mihome.ui import theme  # noqa: E402
from mihome.ui.home_tabs import (  # noqa: E402
    PILL_H,
    PILL_PAD,
    PLACEMENT_ACTIVE_FIRST,
    PLACEMENT_FIXED,
    STYLE_PILL,
    STYLE_SIZE,
    STYLE_WEIGHT,
    HomeTabs,
)

FAILS: list[str] = []
HOMES = [("1103", "1103"), ("h2", "共享家庭"), ("h3", "爸妈家")]


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {got!r}")
    if not ok:
        FAILS.append(f"{name} → 期望 {want!r}，实际 {got!r}")


def check_true(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(f"{name} {detail}")


def click(tabs: HomeTabs, x: int) -> None:
    pos = QPointF(x, tabs.height() // 2)
    tabs.mouseReleaseEvent(
        QMouseEvent(QEvent.MouseButtonRelease, pos, pos, Qt.LeftButton, Qt.LeftButton,
                    Qt.NoModifier)
    )


def _diff(a: QColor, b: QColor) -> int:
    return max(abs(a.red() - b.red()), abs(a.green() - b.green()),
               abs(a.blue() - b.blue()))


def _text_span(img, rect: QRect, pal, active: bool) -> tuple[int, int]:
    """量一个标签里文字墨迹的最左/最右列。

    不读文字内容（offscreen 平台中文会渲染成豆腐块），只找接近文字色的像素。
    每行**只认一支墨色**：激活项认 accent 蓝，其余认 text_mute 灰。两种一起认会
    把胶囊圆角的抗锯齿混合色（如深色下 #757c8a）当成灰字，整块胶囊都命中。
    """
    colors = (QColor(pal.accent if active else pal.text_mute),)
    xs: list[int] = []
    for x in range(max(0, rect.left()), min(img.width(), rect.right() + 1)):
        for y in range(2, max(3, rect.height() - 2)):
            px = img.pixelColor(x, y)
            if px.alpha() and any(_diff(px, c) <= 55 for c in colors):
                xs.append(x)
                break
    return (min(xs), max(xs)) if xs else (-1, -1)


def _rects(tabs: HomeTabs) -> list[QRect]:
    return [QRect(int(round(x)), 0, w, tabs.height())  # noqa: SLF001
            for x, w in zip(tabs._x, tabs._widths)]  # noqa: SLF001


def _spans(tabs: HomeTabs, rects: list[QRect], pal,
           active_idx: int) -> list[tuple[int, int]]:
    img = tabs.grab().toImage()
    return [_text_span(img, r, pal, i == active_idx)
            for i, r in enumerate(rects)]


def section_pill(app: QApplication) -> None:
    """胶囊档的盒模型。

    修之前：布局给每个 rect 都留了左右 ``PILL_PAD``，但只有激活项把文字居中，
    其余左对齐 → 激活项相对自己 rect 多出 14px 偏移，看起来"明显偏右"；
    换激活项时文字还会在居中/左对齐之间跳 14px。这里全部量像素。
    """
    print("\n4) 胶囊档：统一盒模型 / 激活项不偏右 / 切家庭不跳字")
    check("胶囊高度常量", PILL_H, 34)
    for pal in (theme.LIGHT, theme.DARK):
        tabs = HomeTabs(pal, interactive=False)
        tabs.configure(placement=PLACEMENT_FIXED, style=STYLE_PILL)
        tabs.set_homes(HOMES, "h2")  # 激活中间那项，复现用户截图
        tabs.show()
        app.processEvents()
        rects = _rects(tabs)
        spans = _spans(tabs, rects, pal, 1)
        check_true(f"{pal.name}：三个标签都量到文字",
                   len(spans) == 3 and all(s[0] >= 0 for s in spans), f"{spans}")

        offsets = [s[0] - r.left() for s, r in zip(spans, rects)]
        check_true(f"{pal.name}：所有标签文字偏移一致（激活项不偏右）",
                   max(offsets) - min(offsets) <= 2, f"{offsets}")
        gaps = [spans[i + 1][0] - spans[i][1] for i in range(len(spans) - 1)]
        check_true(f"{pal.name}：相邻文字间距均匀",
                   max(gaps) - min(gaps) <= 2, f"{gaps}")
        # 胶囊就是 rect，文字居中 ⇒ 左右留白相等且约等于 PILL_PAD。
        # 容差 5：布局按 QFontMetrics.horizontalAdvance 居中，而量的是实际墨迹，
        # 字形的 advance 本来就比墨迹宽几像素（中文在 offscreen 下尤甚）。
        lp = spans[1][0] - rects[1].left()
        rp = rects[1].right() - spans[1][1]
        check_true(f"{pal.name}：激活项文字在胶囊里居中",
                   abs(lp - rp) <= 5 and lp >= PILL_PAD - 5,
                   f"左 {lp} / 右 {rp} / PILL_PAD={PILL_PAD}")

        # 换激活项：文字一个像素都不该动（原来会在居中 ↔ 左对齐之间跳）
        tabs.set_active("h3")
        app.processEvents()
        after = _spans(tabs, rects, pal, 2)
        check_true(f"{pal.name}：切换家庭后文字不跳",
                   all(abs(a[0] - b[0]) <= 1 and abs(a[1] - b[1]) <= 1
                       for a, b in zip(spans, after)),
                   f"{spans} → {after}")
        tabs.close()

    # 对照：字号统一的那一档本来就是全左对齐，切激活也不该跳
    tabs = HomeTabs(theme.LIGHT, interactive=False)
    tabs.configure(placement=PLACEMENT_FIXED, style=STYLE_WEIGHT)
    tabs.set_homes(HOMES, "h2")
    tabs.show()
    app.processEvents()
    rects = _rects(tabs)
    before = _spans(tabs, rects, theme.LIGHT, 1)
    tabs.set_active("h3")
    app.processEvents()
    after = _spans(tabs, rects, theme.LIGHT, 2)
    check_true("weight 档对照：切换家庭后文字左边缘不动",
               all(abs(a[0] - b[0]) <= 1 for a, b in zip(before, after)),
               f"{before} → {after}")
    tabs.close()

    # -- 父布局抵消：胶囊档自带 14px 左内边距，不能让整排标签相对卡片文字右移 --
    probe = HomeTabs(theme.LIGHT)
    for style, want in ((STYLE_SIZE, 0), (STYLE_WEIGHT, 0),
                        (STYLE_PILL, PILL_PAD)):
        probe.configure(style=style)
        check(f"{style}：content_inset", probe.content_inset, want)
    probe.close()

    import verify_tree as vt  # noqa: PLC0415 - 复用它的 Store + 设置页 fixture

    _, dlg = vt.fresh(theme.LIGHT)
    dlg.show()
    app.processEvents()
    for style, want in ((STYLE_PILL, -PILL_PAD), (STYLE_SIZE, 0),
                        (STYLE_WEIGHT, 0)):
        idx = dlg.cb_style.findData(style)
        check_true(f"设置页能找到 {style} 选项", idx >= 0)
        if idx >= 0:
            dlg.cb_style.setCurrentIndex(idx)
            app.processEvents()
            check(f"设置页预览 {style}：控件左移抵消内缩",
                  dlg.preview.x(), want)  # noqa: SLF001
    dlg.close()

    mw = (ROOT / "mihome/ui/main_window.py").read_text(encoding="utf-8")
    st = (ROOT / "mihome/ui/settings.py").read_text(encoding="utf-8")
    check_true("主窗口顶栏按 content_inset 扣掉左边距",
               "24 - self.tabs.content_inset" in mw)
    check_true("设置页切样式 / 同步控件时都重算边距",
               st.count("self._sync_preview_inset()") >= 3)


def main() -> int:
    app = QApplication(sys.argv[:1])  # noqa: F841

    tabs = HomeTabs(theme.LIGHT)
    tabs.set_homes(HOMES, "1103")
    names = lambda: [n for n, _ in tabs._view]  # noqa: E731, SLF001
    check("fixed 保持设定顺序", names(), ["1103", "h2", "h3"])

    tabs.configure(placement=PLACEMENT_FIXED, style=STYLE_SIZE)
    tabs.set_active("h3")
    check("fixed：激活项在末尾也不重排", names(), ["1103", "h2", "h3"])

    tabs.configure(placement=PLACEMENT_ACTIVE_FIRST, style=STYLE_SIZE)
    check("active_first：激活项提到最左", names(), ["h3", "1103", "h2"])

    first = QPoint(tabs._widths[0] // 2, tabs.height() // 2)  # noqa: SLF001
    check("命中第一个标签是 h3", tabs._view[tabs._hit(first)][0], "h3")  # noqa: SLF001
    second = QPoint(tabs._widths[0] + 30, tabs.height() // 2)  # noqa: SLF001
    check("命中第二个标签是 1103", tabs._view[tabs._hit(second)][0], "1103")  # noqa: SLF001

    for style in (STYLE_SIZE, STYLE_WEIGHT, STYLE_PILL):
        tabs.configure(style=style)
        widths = tabs._widths  # noqa: SLF001
        ok = bool(widths) and all(w > 0 for w in widths) and tabs.width() >= widths[-1]
        check(f"{style}：宽度合理", ok, True)

    # 点击发出的家庭名必须和眼前看到的一致（重排后最容易点错）
    got: list[str] = []
    tabs.home_selected.connect(got.append)
    tabs.configure(placement=PLACEMENT_FIXED, style=STYLE_SIZE)
    tabs.set_active("1103")
    check("重排后顺序", names(), ["1103", "h2", "h3"])
    x = tabs._widths[0] + tabs._widths[1] + 30  # noqa: SLF001
    click(tabs, x)
    check("点第三个位置发出的是 h3", got, ["h3"])

    preview = HomeTabs(theme.LIGHT, interactive=False)
    preview.set_homes(HOMES, "1103")
    got.clear()
    preview.home_selected.connect(got.append)
    click(preview, 40)
    check("预览模式不响应点击", got, [])

    section_pill(app)

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败：")
        for line in FAILS:
            print("  -", line)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
