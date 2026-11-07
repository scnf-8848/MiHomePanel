# SPDX-License-Identifier: GPL-3.0-or-later
"""验证 UI 视觉 token（P1·对齐米家）：圆角、字号、强调色、开关尺寸、状态胶囊。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_theme.py``

这些数值全部来自真机采集（``docs/reference/米家App-UI风格参考.md``），改动它们等于
改设计规范，所以用断言钉住。另有两类**踩过的坑**在这里回归：

* ``QColor("rgba(20, 35, 55, 0.68)")`` 会静默返回**纯黑**——状态胶囊曾经整根变黑条；
* 深色主题下那支深蓝黑落在近黑底上会看不见，必须往文字色提一档。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mihome.ui import theme  # noqa: E402
from mihome.ui.widgets import StatusChip, ToggleSwitch  # noqa: E402

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


def lightness(color: str) -> int:
    return QColor(color).lightness()


def rgb_gap(a: str, b: str) -> int:
    """最大通道差。已选底与悬停底在浅色下明度几乎相同，靠色相分开，
    只看 lightness 会误判成"又糊成一块"。"""
    ca, cb = QColor(a), QColor(b)
    return max(abs(ca.red() - cb.red()), abs(ca.green() - cb.green()),
               abs(ca.blue() - cb.blue()))


def main() -> int:
    app = QApplication(sys.argv[:1])  # noqa: F841

    # -- 1. 圆角三档 ------------------------------------------------------
    print("1) 圆角 token")
    check("卡片 18dp", theme.RADIUS_CARD, 18)
    check("胶囊 16dp", theme.RADIUS_PILL, 16)
    check("大按钮 20dp", theme.RADIUS_BUTTON, 20)
    check_true("三档互不相同且递增",
               theme.RADIUS_CARD < theme.RADIUS_BUTTON and theme.RADIUS_PILL < theme.RADIUS_CARD,
               f"{theme.RADIUS_PILL}/{theme.RADIUS_CARD}/{theme.RADIUS_BUTTON}")
    check_true("小标签 12dp 小于胶囊", theme.RADIUS_SMALL < theme.RADIUS_PILL)

    # -- 2. 强调色 --------------------------------------------------------
    print("\n2) 强调色")
    check("开关绿 = #4CC38A", theme.TOGGLE_ON, "#4CC38A")
    check("执行绿与开关绿同一支", theme.ACCENT_GREEN, theme.TOGGLE_ON)
    check("灯滑条 = #F3BE44", theme.SLIDER_WARM, "#F3BE44")
    check("灯品类色跟滑条一致", theme.CATEGORY_COLOR["light"], theme.SLIDER_WARM)

    # -- 3. 状态胶囊 ------------------------------------------------------
    print("\n3) 状态胶囊")
    light_chip = theme.chip_bg(theme.LIGHT)
    dark_chip = theme.chip_bg(theme.DARK)
    check_true("浅色胶囊不是纯黑（QColor 不认 rgba 字符串）",
               QColor(light_chip).name().upper() != "#000000", light_chip)
    check_true("两套主题的胶囊底色不同", light_chip != dark_chip,
               f"{light_chip} / {dark_chip}")
    for pal, fill in ((theme.LIGHT, light_chip), (theme.DARK, dark_chip)):
        d = abs(lightness(fill) - lightness(pal.bg))
        check_true(f"{pal.name} 胶囊与顶栏底色可辨（Δ ≥ 10）", d >= 10, f"Δ={d}")
    check_true("胶囊文字跟主题走",
               theme.chip_text(theme.LIGHT) != theme.chip_text(theme.DARK),
               f"{theme.chip_text(theme.LIGHT)} / {theme.chip_text(theme.DARK)}")
    check_true("浅色胶囊文字比底深",
               lightness(theme.chip_text(theme.LIGHT)) < lightness(light_chip))
    check_true("深色胶囊文字比底亮",
               lightness(theme.chip_text(theme.DARK)) > lightness(dark_chip))

    # -- 3b. 侧栏底色 ------------------------------------------------------
    print("\n3b) 侧栏底色")
    for pal in (theme.LIGHT, theme.DARK):
        nav, bg = theme.nav_bg(pal), pal.bg
        d = abs(lightness(nav) - lightness(bg))
        check_true(f"{pal.name} 侧栏与内容区不同色（Δ ≥ 8）", d >= 8, f"{nav} Δ={d}")
        # 选中行必须比栏底再亮一层，否则深色下选中块会整块消失
        check_true(f"{pal.name} 选中行比栏底亮",
                   lightness(theme.nav_row_bg(pal)) > lightness(nav),
                   f"{theme.nav_row_bg(pal)}")
        check_true(f"{pal.name} 选中行不至于盖过卡片",
                   abs(lightness(theme.nav_row_bg(pal)) - lightness(pal.card)) <= 20,
                   f"{theme.nav_row_bg(pal)} vs card {pal.card}")

    # -- 3c. 对话框分类栏 --------------------------------------------------
    print("\n3c) 对话框分类栏")
    # 设置窗口的内容区是 surface_bg，不是主界面的 pal.bg。NAV_BG 那两档照 pal.bg
    # 定的，直接搬过来浅色只差 3、深色只差 2 个明度档——栏和内容区又糊成一片。
    for pal in (theme.LIGHT, theme.DARK):
        canvas, rail = theme.surface_bg(pal), theme.rail_bg(pal)
        d = abs(lightness(rail) - lightness(canvas))
        check_true(f"{pal.name} 分类栏与对话框底色可辨（Δ ≥ 12）", d >= 12,
                   f"{rail} Δ={d}")
        check_true(f"{pal.name} 分类栏没有照搬主界面 NAV_BG（差 ≥ 8）",
                   abs(lightness(rail) - lightness(theme.nav_bg(pal))) >= 8,
                   f"{rail} vs nav {theme.nav_bg(pal)}")
        row = theme.rail_row_bg(pal)
        check_true(f"{pal.name} 选中行比栏底亮一层（Δ ≥ 8）",
                   lightness(row) - lightness(rail) >= 8,
                   f"{row} L={lightness(row)} vs {lightness(rail)}")

    # -- 3d. 对话框分区面板 ------------------------------------------------
    print("\n3d) 对话框分区面板")
    # 排序页那两块（设备树 / 场景树）曾经直接摊在窗口底色上，深色下几乎看不出边界。
    # 这一节量的是"面板底 vs 对话框底"这一对差值，以及行悬停/选中相对面板底的方向。
    for pal in (theme.LIGHT, theme.DARK):
        canvas, panel = theme.surface_bg(pal), theme.panel_bg(pal)
        d = abs(lightness(panel) - lightness(canvas))
        check_true(f"{pal.name} 面板与对话框底色可辨（Δ ≥ 12）", d >= 12,
                   f"{panel} Δ={d}")
        if pal.is_dark:
            # 浅色用卡片白是**故意**的（白纸压灰底，Δ 有 24）。深色才是那个坑：
            # card #1B1D22 压在 surface #191A1D 上只差 2–5 个明度档。
            check_true("深色面板没有照抄 card（差 ≥ 5）",
                       abs(lightness(panel) - lightness(pal.card)) >= 5,
                       f"{panel} vs card {pal.card}")
        hover = theme.panel_row_hover(pal)
        check_true(f"{pal.name} 行悬停与面板可辨（Δ ≥ 4）",
                   abs(lightness(hover) - lightness(panel)) >= 4,
                   f"{hover} Δ={abs(lightness(hover) - lightness(panel))}")
        sel = theme.panel_row_selected(pal)
        check_true(f"{pal.name} 选中行与面板可辨（Δ ≥ 8）",
                   abs(lightness(sel) - lightness(panel)) >= 8, f"{sel}")
        # 深色下面板的明度比 card 高，选中行若还拿 accent_soft(#1E2A42) 就会比面板
        # 更暗——鼠标点上去像挖了个洞。所以要求"两套主题里选中行都比面板底亮"之外，
        # 深色单独锁一条方向。
        if pal.is_dark:
            check_true("深色选中行比面板底亮（不许挖洞）",
                       lightness(sel) > lightness(panel),
                       f"{sel} L={lightness(sel)} vs {lightness(panel)}")

        # 行间分隔线：能分出行，但不能重成表格线
        line = theme.panel_divider(pal)
        d_line = abs(lightness(line) - lightness(panel))
        check_true(f"{pal.name} 行分隔线可辨且不抢眼（6 ≤ Δ ≤ 30）", 6 <= d_line <= 30,
                   f"{line} Δ={d_line}")

        # 卡内输入框：浅色靠边框分界（底色只差一档），深色必须**比卡片亮**——
        # 照搬 bg_soft(#15171C) 会是个黑洞
        field = theme.field_bg(pal)
        d_field = lightness(field) - lightness(panel)
        if pal.is_dark:
            check_true("深色输入框比卡片亮（不是洞）", d_field >= 6,
                       f"{field} Δ={d_field}")
        else:
            check_true("浅色输入框比卡片略深一档（1 ≤ Δ ≤ 8）", 1 <= -d_field <= 8,
                       f"{field} Δ={d_field}")

    # -- 3e. 下拉浮层 -----------------------------------------------------
    print("\n3e) 下拉浮层与承载它的分区卡")
    # 四角黑块修好之后，弹出层若继续用 panel_bg 就会整张隐身进同色卡片里：
    # 以前是黑块在提供边界。现在边界必须由底色差 + 自绘淡描边承担。
    for pal in (theme.LIGHT, theme.DARK):
        panel, pop = theme.panel_bg(pal), theme.popup_bg(pal)
        d = abs(lightness(pop) - lightness(panel))
        check_true(f"{pal.name} 浮层与分区卡可辨（8 ≤ Δ ≤ 30）", 8 <= d <= 30,
                   f"{pop} vs {panel} Δ={d}")
        if pal.is_dark:
            check_true("深色浮层比卡片亮", lightness(pop) > lightness(panel),
                       f"{pop} L={lightness(pop)} vs {lightness(panel)}")
        else:
            check_true("浅色浮层比卡片略暗（白卡已是最亮）",
                       lightness(pop) < lightness(panel), f"{pop} vs {panel}")
        edge = theme.popup_border(pal)
        d_edge = abs(lightness(edge) - lightness(pop))
        check_true(f"{pal.name} 浮层描边与底色可辨（10 ≤ Δ ≤ 35）",
                   10 <= d_edge <= 35, f"{edge} Δ={d_edge}")

        # 列表行的三种底：当前值（带蓝 + 勾）、鼠标临时路过（淡中性）、什么都没有。
        # 悬停色必须**相对浮层底**算，沿用 panel_row_hover 会在深色下挖出一块暗洞。
        hover = theme.popup_row_hover(pal)
        d_hover = lightness(hover) - lightness(pop)
        check_true(f"{pal.name} 悬停行与浮层底可辨（5 ≤ ΔL ≤ 25）",
                   5 <= abs(d_hover) <= 25, f"{hover} vs {pop} Δ={d_hover}")
        check_true(f"{pal.name} 悬停行方向正确（深色提亮、浅色压暗）",
                   d_hover > 0 if pal.is_dark else d_hover < 0, f"Δ={d_hover}")
        selected = theme.panel_row_selected(pal)
        check_true(f"{pal.name} 已选行与临时悬停行可区分",
                   rgb_gap(selected, hover) >= 8, f"{selected} vs {hover}")
        check_true(f"{pal.name} 已选行与浮层底可区分",
                   rgb_gap(selected, pop) >= 8, f"{selected} vs {pop}")
        check_true(f"{pal.name} 勾色与已选底拉得开",
                   rgb_gap(pal.accent, selected) >= 40,
                   f"{pal.accent} vs {selected}")

    # -- 4. 开关尺寸 ------------------------------------------------------
    print("\n4) 开关尺寸")
    sm = ToggleSwitch(False, theme.LIGHT)
    md = ToggleSwitch(False, theme.LIGHT, size="md")
    check("sm 44×24", (sm.width(), sm.height()), (44, 24))
    check("md 49×32", (md.width(), md.height()), (49, 32))
    md.set_on(True)
    check_true("md 打开后状态正确", md.is_on())
    check_true("未知尺寸回落到 sm",
               ToggleSwitch(False, theme.LIGHT, size="oops").size() == ToggleSwitch().size())

    # -- 5. 字号 ----------------------------------------------------------
    print("\n5) 字号")
    for name, got, want in (
        ("家庭名 24", theme.FS_TITLE, 24),
        ("分区标题 18", theme.FS_SECTION, 18),
        ("激活标签 20", theme.FS_TAB_ACTIVE, 20),
        ("未激活标签 16", theme.FS_TAB, 16),
        ("卡片标题 16", theme.FS_CARD_TITLE, 16),
        ("卡片副标题 13", theme.FS_CARD_SUB, 13),
        ("指标数字 28", theme.FS_METRIC, 28),
        ("单位 11", theme.FS_UNIT, 11),
    ):
        check(name, got, want)
    check_true("标签激活字号大于未激活", theme.FS_TAB_ACTIVE > theme.FS_TAB)

    # -- 6. 卡片放得下两行标题 + 开关 --------------------------------------
    print("\n6) 卡片高度")
    need = 10 + 46 + 18 + 8 + 34 + 16  # 上边距 + 两行标题 + 副标题 + 间隙 + 电源键 + 下边距
    check_true(f"卡片高 {theme.CARD_H} 容得下两行 16px 标题与电源键",
               theme.CARD_H >= need, f"需要 {need}")

    # -- 7. 状态胶囊宽度跟随文字 ------------------------------------------
    print("\n7) 胶囊宽度")
    short = StatusChip("1 台", theme.LIGHT)
    longer = StatusChip("47 台设备 · 15 台开启 · 4 台离线", theme.LIGHT)
    check_true("文字长了宽度跟着涨", longer.width() > short.width(),
               f"{short.width()} → {longer.width()}")
    check_true("最短也有 56px", short.width() >= 56, f"{short.width()}")
    short.setText("1 台设备 · 1 台开启")
    check_true("setText 之后宽度重算", short.width() > 56)

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
