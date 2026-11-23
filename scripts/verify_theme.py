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


def contrast(a: str, b: str) -> float:
    """WCAG 对比度。输入框描边这种 1px 细线只能靠它判断"看不看得见"。"""
    def _lum(c: QColor) -> float:
        def _f(v: float) -> float:
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        return 0.2126 * _f(c.redF()) + 0.7152 * _f(c.greenF()) + 0.0722 * _f(c.blueF())

    la, lb = _lum(QColor(a)), _lum(QColor(b))
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


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

    # -- 8. 输入框在深色下要看得见（用户 11-13 第三轮报的） -----------------
    print("\n8) 输入框的底与描边")
    for pal in (theme.LIGHT, theme.DARK):
        fill = theme.field_bg(pal)
        line = theme.field_border(pal)
        card = theme.panel_bg(pal)
        if pal.is_dark:
            # 深色：卡片本身已经比窗口亮一档，输入框必须再亮一档才读得出"凹陷/可输入"
            check_true(f"{pal.name} 输入框底与卡片分得开（≥1.12:1）",
                       contrast(fill, card) >= 1.12, f"{fill} vs {card} "
                       f"{contrast(fill, card):.2f}:1")
        else:
            # 浅色：白卡上再压深一档会读成"禁用"，轮廓交给描边（见下一条）
            check_true("light 底与卡片几乎同色，轮廓交给描边",
                       contrast(fill, card) < 1.08, f"{fill} vs {card} "
                       f"{contrast(fill, card):.2f}:1")
        check_true(f"{pal.name} 输入框描边看得见（≥1.2:1）",
                   contrast(line, fill) >= 1.2, f"{line} vs {fill} "
                   f"{contrast(line, fill):.2f}:1")
    # 反向证明：旧写法（p.border 直接上输入框）在深色下确实等于没画
    check_true("深色下 p.border 压在卡片上几乎不可见（旧写法）",
               contrast(theme.DARK.border, theme.panel_bg(theme.DARK)) < 1.05,
               f"{contrast(theme.DARK.border, theme.panel_bg(theme.DARK)):.2f}:1")
    check_true("深色下新描边比 p.border 明显（≥1.3:1 的差距）",
               contrast(theme.field_border(theme.DARK), theme.panel_bg(theme.DARK))
               >= contrast(theme.DARK.border, theme.panel_bg(theme.DARK)) + 0.3)
    qss = theme.field_qss(theme.DARK)
    check_true("field_qss 用的是 field_bg + field_border",
               theme.field_bg(theme.DARK) in qss
               and theme.field_border(theme.DARK) in qss
               and "border: 1px solid" in qss)

    # -- 9. 24 档预设：卡面 / 顶栏 / 搜索框的明暗层级 ----------------------
    # 2026-11-17：极光深色曾出现 card #2B3E3D(L57.9) 撞 bg #2A4020(L57.0)，
    # 卡片、搜索框、语音指令条全部平贴在背景上（搜索框与顶栏底带 **-3.7 级**，
    # 用户只说了句"搜索框还是不满意"）。这一节把"每一档都浮得起来"钉住。
    print("\n9) 预设矩阵（24 档 × 明暗两维）")
    check("预设共 24 档", len(theme.PRESETS), 24)
    check("纯色档 7 个", len(theme.SOLID_PRESETS), 7)
    check("渐变档 17 个", len(theme.GRADIENT_PRESETS), 17)
    check_true("渐变档的浅深两面都是渐变",
               all(p.light.is_gradient() and p.dark.is_gradient()
                   for p in theme.GRADIENT_PRESETS))
    check_true("两组拼起来正好是 PRESETS、纯色在前",
               theme.PRESETS == theme.SOLID_PRESETS + theme.GRADIENT_PRESETS)
    # 渐变档**全部**用编号命名（2026-11-17 用户定）。标签必须是按顺序的 1..17：
    # 混进一个"极光"、或者编号跳号，界面上的色块就会和文档/口头说法对不上。
    got = [p.label for p in theme.GRADIENT_PRESETS]
    check_true("渐变档标签是连续的 1..17", got == [str(i + 1) for i in range(17)],
               str(got))
    check_true("纯色档标签不带设计圈旧名",
               not ({"鼠尾草", "藕粉", "石板", "暖沙", "雾蓝", "薰衣草"}
                    & {p.label for p in theme.SOLID_PRESETS}))
    # 编号来源只认 n1–n11；``startswith("n")`` 会把手写的 night 也算进来
    numbered = [p for p in theme.PRESETS if p.id[:1] == "n" and p.id[1:].isdigit()]
    check("编号来源 11 套", len(numbered), 11)

    def _over(base, top, alpha: float) -> QColor:
        """把 ``top`` 按 ``alpha`` 叠到 ``base`` 上——和 ``chrome`` 画卡面同一公式。"""
        b, t = QColor(base), QColor(top)
        mix = lambda k: round(getattr(b, k)() + (getattr(t, k)() - getattr(b, k)()) * alpha)
        return QColor(mix("red"), mix("green"), mix("blue"))

    def _lum(color) -> float:
        c = QColor(color)
        return 0.2126 * c.red() + 0.7152 * c.green() + 0.0722 * c.blue()

    flat_stuck: list[str] = []      # 卡面贴着背景
    bar_flat: list[str] = []        # 顶栏和背景没分开
    below_bar: list[str] = []       # 卡面沉到顶栏底板底下
    field_dull: list[str] = []      # 搜索框糊在顶栏里
    nav_order: list[str] = []       # 侧栏三层顺序乱
    for preset in theme.PRESETS:
        for mode in ("light", "dark"):
            pal = theme.palette_for(preset.id, mode)
            tag = f"{preset.id}/{mode}"
            gap = _lum(pal.card) - _lum(pal.bg)
            # 12 是两档共用的底线：纯色档实测最小 13.1（mono），渐变档最小 30.4
            if gap < 12:
                flat_stuck.append(f"{tag} Δ={gap:.1f}")
            f_, h_, r_ = (theme.nav_fill(pal), theme.nav_hover_fill(pal),
                          theme.nav_row_fill(pal))
            # 渐变档靠 alpha 分三层（0.42 / +0.12 / +0.26）；纯色档靠明度，且
            # 悬停方向随明暗走（浅色变暗、深色变亮），只有"选中必须最亮"两档通用
            if pal.surface.is_gradient():
                ok = f_.alpha() < h_.alpha() < r_.alpha() and f_ != h_ != r_
            else:
                ok = (_lum(r_) > max(_lum(f_), _lum(h_))
                      and _lum(f_) != _lum(h_))
            if not ok:
                nav_order.append(f"{tag} {f_.alpha()}/{h_.alpha()}/{r_.alpha()}")
            if not pal.surface.is_gradient():
                continue
            bar = _lum(theme.bar_bg(pal))
            # 顶栏自成一层：浅色压深、深色提亮，两个方向都是"离开背景"，≥ 8 级
            if abs(bar - _lum(pal.bg)) < 8:
                bar_flat.append(f"{tag} Δ={bar - _lum(pal.bg):.1f}")
            # 卡面要压在顶栏底板之上，否则搜索框（卡面）会沉进顶栏
            if _lum(pal.card) <= bar:
                below_bar.append(f"{tag} {_lum(pal.card):.1f} ≤ {bar:.1f}")
            base = _over(pal.bg, theme.bar_bg(pal), pal.card_alpha)
            field = _over(base, theme.card_fill(pal.card, pal).name(), pal.card_alpha)
            d = _lum(field) - _lum(base)
            # 10 是底线：实测最小 n10 深 11.3 / n3 深 11.7
            if d < 10:
                field_dull.append(f"{tag} Δ={d:.1f}")

    check_true("每档卡面都浮在背景上（Δ ≥ 12）", not flat_stuck, "; ".join(flat_stuck[:4]))
    check_true("渐变档卡面比顶栏底板亮", not below_bar, "; ".join(below_bar[:4]))
    check_true("渐变档顶栏与背景分得开（≥ 8 级）", not bar_flat, "; ".join(bar_flat[:4]))
    check_true("渐变档搜索框从底带上浮起来（≥ 10 级）", not field_dull,
               "; ".join(field_dull[:4]))
    check_true("侧栏三层 常态 ≠ 悬停 ≠ 选中、且选中恒最亮", not nav_order,
               "; ".join(nav_order[:4]))

    # -- 9b. 顶栏底板的两种口径 -------------------------------------------
    print("\n9b) 顶栏底板 bar_bg")
    grad = theme.palette_for("aurora", "light")
    solid = theme.palette_for("mono", "light")
    check_true("纯色档往卡片色混（变成白里带灰）",
               _lum(theme.bar_bg(solid)) > _lum(solid.bg), theme.bar_bg(solid))
    check_true("渐变档反过来往文字色混（压深一档）",
               _lum(theme.bar_bg(grad)) < _lum(grad.bg), theme.bar_bg(grad))
    check_true("渐变档顶栏底板明显压深（≥ 15 级）",
               _lum(grad.bg) - _lum(theme.bar_bg(grad)) >= 15,
               f"Δ={_lum(grad.bg) - _lum(theme.bar_bg(grad)):.1f}")

    # -- 9c. 编号档的色标结构 ---------------------------------------------
    # 原 9c（顶栏搜索框的 card_field_qss）随搜索框一起在 2026-11-23 撤掉
    print("\n9c) 编号渐变档的色标")
    stop_bad: list[str] = []
    for preset in numbered:
        for mode, surf in (("light", preset.light), ("dark", preset.dark)):
            tag = f"{preset.id}/{mode}"
            if len(surf.stops) != 5:
                stop_bad.append(f"{tag} 色标 {len(surf.stops)} 个")
            # 深色档必须逐档变暗（保住方向感，拉平就成了糖果彩虹）。
            # 比的是 **HSL 的 L**（_GRADE_DARK_L 就是百分比），不是 Rec.709 加权亮度
            # ——黄色 L23% 的加权亮度远高于蓝色 L23%，拿后者判会误报"没递减"。
            lums = [QColor(s).lightness() for s in surf.stops]
            if mode == "dark" and lums != sorted(lums, reverse=True):
                stop_bad.append(f"{tag} 明度没递减 {lums}")
    check_true("编号档每个都是 5 色标、深色档逐档变暗", not stop_bad,
               "; ".join(stop_bad[:4]))

    # -- 9e. 渐变档的排列：颜色接近的挨着 --------------------------------
    # 用户 11-17："渐变色里，能不能把颜色接近的挨着放"。判据是
    # ``theme.hue_gap``——相邻两档 5 个色标里最像的那一对差多少度。
    # 这条比"编号看着顺眼"实在：以后加一档、或者手滑调了顺序表，这里当场报警。
    print("\n9e) 渐变档按色相邻排")
    grad_list = list(theme.GRADIENT_PRESETS)
    chain = [theme.hue_gap(a.light, b.light)
             for a, b in zip(grad_list, grad_list[1:])]
    worst = max(chain)
    # 20° 是留了余量的门线：实测最差 13.7°（1 号橙红 ↔ 2 号青绿，闭环换色处）
    check_true("相邻两档的最近色相差 ≤ 20°", worst <= 20.0,
               f"最差 {worst:.1f}°（第 {chain.index(worst) + 1}/{chain.index(worst) + 2} 号之间）")
    check_true("闭环回绕（17 → 1）也算相邻",
               theme.hue_gap(grad_list[-1].light, grad_list[0].light) <= 20.0,
               f"{theme.hue_gap(grad_list[-1].light, grad_list[0].light):.1f}°")
    # 顺序表本身：漏 id / 写重 theme 里已经 raise 过一次，这里再钉一层——
    # 万一有人把那段 raise 当成"开发期调试代码"删掉，界面会静默少一块色卡。
    order = list(theme._GRADIENT_ORDER)
    check("顺序表 17 条", len(order), 17)
    check("顺序表无重复 id", len(set(order)), 17)
    check_true("顺序表覆盖全部渐变 id、且界面次序就是它",
               tuple(order) == tuple(p.id for p in theme.GRADIENT_PRESETS))
    # 反向对照：重排前的老次序（手写 6 套 + 11 套编号原样拼）有多乱
    old = list(theme._ALL_GRADIENTS)
    old_worst = max(theme.hue_gap(a.light, b.light)
                    for a, b in zip(old, old[1:]))
    check_true("老次序的相邻色相差至少是新次序的 4 倍", old_worst >= worst * 4,
               f"老 {old_worst:.1f}° vs 新 {worst:.1f}°")
    check_true("field_qss 焦点换成 accent",
               f"QLineEdit:focus {{ border-color: {theme.DARK.accent}; }}" in qss)
    check_true("不带 base 时不出现面板底当底色",
               theme.panel_bg(theme.DARK) not in qss)
    # 顶栏搜索框压在窗口底上：底色必须从 bg 起算，不能亮成一块凸起
    top = theme.field_qss(theme.DARK, radius=16, base=theme.DARK.bg)
    check_true("传 base 时底色按窗口底重算",
               theme.field_bg(theme.DARK) not in top
               and theme.blend(theme.DARK.bg, theme.DARK.text, 0.06) in top)
    # 顶栏搜索框跟背景一起透（2026-11-17）：底色出 rgba，描边也要 rgba 且更实，
    # 否则一圈实线把半透明胶囊重新勾成"贴纸"。
    pal = theme.palette_for("aurora", "light")
    see = theme.field_qss(pal, radius=16, base=pal.bg, alpha=pal.card_alpha)
    check_true("传 alpha 时底色走 rgba（跟着背景透）",
               "background: rgba(" in see, see.splitlines()[1].strip())
    check_true("描边也是 rgba、且比底色更实",
               "border: 1px solid rgba(" in see
               and theme.qss_alpha(min(1.0, pal.card_alpha + 0.22))
               > theme.qss_alpha(pal.card_alpha))
    plain = theme.field_qss(pal, radius=16, base=pal.bg)
    check_true("不传 alpha 时不出现 rgba（弹窗里的输入框保持实色）",
               "rgba(" not in plain)

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
