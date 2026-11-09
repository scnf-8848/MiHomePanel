"""视觉基调。

目标不是"好看的工具"，是"像米家 App"。抄的是官方 UED 里能查到的几条硬规则：

* 背景用中性浅灰，让白色卡片浮起来；
* 卡片圆角大（18px）、**无描边、无阴影**，层级只靠底色明度差——
  描边一旦加上，整面卡片墙会显得"格子化"，和米家的浮层感正好相反；
* 设备开启时不是"换个边框"，而是整张卡片的底色染上品类色（很淡），
  同时产品图保持原样——产品图本身是白底实拍图，染色后会显得像"点亮了"；
* 文字只有两级：名称（主）与状态（次），绝不超过两级；
* 离线设备整卡降低不透明度并显示"已离线"，而不是画一个红叉。

品类色不全靠 catalog 里的 ``accent``，这里再收敛一次，避免红绿蓝乱飞。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor, QPalette


@dataclass(frozen=True)
class Palette:
    name: str
    bg: str
    bg_soft: str
    card: str
    card_hover: str
    border: str
    text: str
    text_soft: str
    text_mute: str
    accent: str
    accent_soft: str
    shadow: str
    overlay: str
    is_dark: bool = False


LIGHT = Palette(
    name="light",
    bg="#F1F2F4",
    bg_soft="#F7F8FA",
    card="#FFFFFF",
    card_hover="#FCFCFD",
    border="#EAEBEE",
    text="#0E1116",
    text_soft="#6B7280",
    text_mute="#9CA3AF",
    accent="#3D7EFF",
    accent_soft="#EAF1FF",
    shadow="#00000014",
    overlay="#FFFFFF",
)

DARK = Palette(
    name="dark",
    bg="#0E0F12",
    bg_soft="#15171C",
    card="#1B1D22",
    card_hover="#22252B",
    border="#26282E",
    text="#F2F3F5",
    text_soft="#A0A6AF",
    text_mute="#6C727C",
    accent="#5B93FF",
    accent_soft="#1E2A42",
    shadow="#00000055",
    overlay="#1B1D22",
    is_dark=True,
)

#: 开关"打开"、单选选中、执行按钮用的薄荷绿（米家实测 #4CC38A）。
#: 故意不用 accent（蓝）：蓝开关看起来像"可点"，绿色才像"通电/生效"。
#: 单选圆点也归这里（见 :func:`select_dot`）——它表达的是"这一项生效了"，
#: 和开关打开是同一件事。
TOGGLE_ON = "#4CC38A"
#: 同一支绿色的别名，给"执行/确认"这类按钮用，读代码时语义更明确。
ACCENT_GREEN = TOGGLE_ON

#: 灯光亮度 / 色温滑条。米家把"暖光"直接画进控件本身，比统一用强调色更有器物感。
SLIDER_WARM = "#F3BE44"

#: 左栏（房间导航）底色。米家没有侧栏可抄，这里按桌面软件的惯例做：栏是"机身"、
#: 卡片墙是"台面"，两者差一档明度就能分开。**方向随主题变**——浅色把栏压暗
#: （内容区已经是浅灰，再亮一档就没有层次了），深色把栏提亮（#0E0F12 已经接近
#: 纯黑，再压就成一整片黑）。两套都只差 8–12 个明度档：要"看得出是两块"，
#: 不要读成"卡了一条色带"。
NAV_BG_LIGHT = "#E1E4E9"
NAV_BG_DARK = "#15171C"

# 品类色：只用来做"开启状态"的染色，饱和度都压低，避免卡片墙上花成一团。
# light 直接用滑条那一支琥珀黄，和米家"灯亮时整卡泛暖"的观感对上。
CATEGORY_COLOR: dict[str, str] = {
    "light": SLIDER_WARM,
    "switch": "#6E8BB5",
    "outlet": "#4E9BE8",
    "air-conditioner": "#4AB0E8",
    "air-purifier": "#3FBFB0",
    "fan": "#5FB6C9",
    "bath-heater": "#E8A86B",
    "airer": "#B08AD6",
    "washer": "#6C8FD6",
    "hood": "#8A94A6",
    "oven": "#E08A6B",
    "camera": "#5E7085",
    "lock": "#D9A03F",
    "sensor": "#8E9AA8",
    "motion-sensor": "#8E9AA8",
    "occupancy-sensor": "#8E9AA8",
    "temperature-humidity-sensor": "#8E9AA8",
    "scale": "#8E9AA8",
    "speaker": "#7A6FD0",
    "television": "#5A6579",
    "gateway": "#7B8FA1",
    "remote-control": "#7B8FA1",
    "curtain": "#9C86C4",
    "generic": "#8A93A0",
}


def category_color(category: str) -> str:
    return CATEGORY_COLOR.get(category, CATEGORY_COLOR["generic"])


def blend(hex_a: str, hex_b: str, t: float) -> str:
    """两个 #rrggbb 之间线性插值，t=0 取 a。"""
    a = _rgb(hex_a)
    b = _rgb(hex_b)
    return "#%02X%02X%02X" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def rgba(color: str, alpha: float) -> str:
    """``#rrggbb`` + 透明度 → ``rgba(...)``。

    不能写 ``{color}66``：Qt 的 8 位十六进制是 #AARRGGBB，把 alpha 放在后面会得到
    一个完全不同的颜色（文字灰会变成橄榄绿）。
    """
    r, g, b = _rgb(color)
    return f"rgba({r}, {g}, {b}, {alpha:.2f})"


def luma(color: str) -> float:
    """感知亮度（0–255）。权重是 Rec.601 的，够用来判断"两块颜色分不分得开"。"""
    r, g, b = _rgb(color)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def tint(color: str, strength: float, palette: Palette, min_gap: float = 0.0) -> str:
    """把品类色按强度混进卡片底色——深色模式往暗里混，浅色往白里混。

    ``min_gap`` 是**浅色模式下卡片与背景的最小亮度差**。浅色背景本来就贴近白卡
    （#F1F2F4 vs #FFFFFF，只差 13），再混一层品类色只剩 1–3，电视 / 摄像头这种
    深灰品类混完甚至比背景还暗——卡片边界彻底消失。给了 ``min_gap`` 就把染色强度
    往小收到刚好够看为止。

    亮度是混合强度的线性函数，所以直接解方程，不用二分。深色模式的差本来就有
    20 以上，不参与收窄。
    """
    base = palette.card
    t = max(0.0, min(1.0, strength))
    if min_gap > 0 and not palette.is_dark:
        l_base = luma(base)
        l_color = luma(color)
        target = luma(palette.bg) + min_gap
        if l_color < l_base and target < l_base:
            t = min(t, max(0.0, (target - l_base) / (l_color - l_base)))
    return blend(base, color, t)


def nav_bg(p: Palette) -> str:
    """左栏底色。见 ``NAV_BG_LIGHT`` / ``NAV_BG_DARK`` 的注释。"""
    return NAV_BG_DARK if p.is_dark else NAV_BG_LIGHT


def nav_row_bg(p: Palette) -> str:
    """导航选中行。比栏底色再亮一层：浅色直接用卡片白（和右边的白卡同族，
    一眼看得出"当前在这间房"），深色在近黑的栏底上提 9%，否则选中块整个看不见。"""
    return p.card if not p.is_dark else blend(NAV_BG_DARK, p.text, 0.09)


def surface_bg(p: Palette) -> str:
    """对话框 / 设置窗口的底色。

    比主界面的 ``pal.bg`` 再推 4.5%：主界面在下、对话框在上，靠这一档明度把两层
    叠出来。``chrome.surface_bg`` 只是这里的转发，公式只留一份。
    """
    return blend(p.bg, "#FFFFFF" if p.is_dark else "#000000", 0.045)


def rail_bg(p: Palette) -> str:
    """对话框内分类栏（设置窗口左侧）底色。

    不能直接复用 ``nav_bg``：那两档是照 ``pal.bg`` 定的。设置窗口的内容区是
    ``surface_bg``（比 ``pal.bg`` 亮/暗一档），同一支 ``NAV_BG`` 落上去几乎同色
    ——浅色差 3、深色差 2 个明度档，等于没分。所以这里**只相对 surface_bg** 推一档，
    方向仍随主题：浅色压暗、深色提亮，幅度取 7.5%（比主界面侧栏略大，因为对话框
    的栏更窄，明度差不够就只剩一条色带）。
    """
    return blend(surface_bg(p), "#FFFFFF" if p.is_dark else "#000000", 0.075)


def rail_row_bg(p: Palette) -> str:
    """分类栏选中行。浅色直接用卡片白（和右边内容里的白卡同族），
    深色在栏底上提 9%——否则选中块会整块融进栏底。"""
    return p.card if not p.is_dark else blend(rail_bg(p), p.text, 0.09)


def panel_bg(p: Palette) -> str:
    """对话框里"浮起来"的分区容器底（设置 → 排序页那两块）。

    浅色直接给卡片白：对话框底 ``surface_bg`` 是 #E6E7E9 一带，白纸压在灰底上，
    边界一眼就看得见。

    深色**不能照抄 ``p.card``**：``surface_bg`` 是 #191A1D、``card`` 是 #1B1D22，
    只差 2–5 个明度档——和当初分类栏、主界面侧栏踩的是同一个坑。所以这里只相对
    ``surface_bg`` 往文字色提 7%，得到 #28292C 一带（Δ ≈ 15），比卡片再亮一层：
    这种"容器比卡片亮"的顺序只在对话框里成立，主界面卡片墙仍是卡片更亮。
    """
    return p.card if not p.is_dark else blend(surface_bg(p), p.text, 0.07)


def panel_row_hover(p: Palette) -> str:
    """面板里行的悬停底：相对**面板底**推一档，不是相对 ``card``。

    直接用 ``blend(card, text, 4%)`` 的话，深色下面板(#28292C)比 card(#1B1D22)亮，
    悬停块反而比面板暗——鼠标划过去像挖了个洞。
    """
    return blend(panel_bg(p), p.text, 0.05 if p.is_dark else 0.035)


def panel_divider(p: Palette) -> str:
    """卡内行与行之间的分隔线。

    只挪一点点明度：要"能分出行"，不能变成表格线。浅色从白卡往文字色压 7%
    （#FFFFFF → #EEEEEF），深色从面板底提 9%（#28292C → #3B3C41）。两档都比
    ``p.border`` 更贴近面板底色——``border`` 是给"卡片压在窗口底上"用的，
    卡内部分行用它会重得像一张表。
    """
    return blend(panel_bg(p), p.text, 0.09 if p.is_dark else 0.07)


def field_bg(p: Palette) -> str:
    """卡内输入控件（下拉框）的底色。

    不能直接拿 ``bg_soft``：浅色下它压在窗口底上正好，深色下 #15171C 压在比它亮的
    卡片 #28292C 上就是一个黑洞——和行选中色踩的是同一个坑。所以也是"相对卡片推一
    档"：浅色压暗 2%（#FFFFFF → #FAFAFA），深色提亮 5.5%（#28292C → #33343A）。
    """
    return blend(panel_bg(p), p.text, 0.055 if p.is_dark else 0.022)


def popup_bg(p: Palette) -> str:
    """下拉浮层的底：必须和它压着的那张分区卡拉开。

    ``panel_bg`` 是设置页卡片的底色；浮层照抄一次，等四角黑块修好、边界不再
    由黑块提供时，整张菜单会直接隐身进卡片里。所以相对面板再推一档：深色往
    文字色提 10%（#28292C → #3C3D40），浅色卡片已是纯白、只能往黑压 4.5%
    （#FFFFFF → #F4F4F4）。方向相反，但都与卡片差 ≥ 10 个明度档。
    """
    if p.is_dark:
        return blend(panel_bg(p), p.text, 0.10)
    return blend(panel_bg(p), "#000000", 0.045)


def popup_border(p: Palette) -> str:
    """浮层一圈极淡的自绘描边（画在同一条抗锯齿圆角路径上）。

    卡片之间靠明度差分层，但浮层是独立顶层窗口、又压在同族色上，浅色下只靠
    底色差还是偏弱，补一条 1px 淡边。**绝不能写进 QSS 的 ``border``**：那样
    QFrame 会在圆角外再画一圈带斜接的方角框（1.75 倍屏上特别明显）。
    """
    return blend(popup_bg(p), p.text if p.is_dark else "#000000", 0.10)


def popup_row_hover(p: Palette) -> str:
    """下拉浮层里**鼠标临时划过**的行底，相对 ``popup_bg`` 推一档。

    不能复用 ``panel_row_hover``：那个是相对分区卡（``panel_bg``）算的，而浮层
    比卡片还亮/暗一档。深色下 ``panel_row_hover`` 落在 #313235，比浮层底
    #3C3D40 更暗——鼠标划过去像在菜单上挖了个洞。这里一律往文字色推：
    浅色压暗、深色提亮，方向由文字色自带，两套都不会反向。
    """
    return blend(popup_bg(p), p.text, 0.05 if p.is_dark else 0.035)


def panel_row_selected(p: Palette) -> str:
    """面板里选中行。

    浅色沿用 ``accent_soft``（#EAF1FF 铺在白卡上很干净）。深色那支是 #1E2A42，
    比面板底 #28292C 还暗，同样是"挖洞"。改成在面板底上掺两成强调色，得到
    #323E56 一带——蓝色仍然在，但比面板亮，读起来是"这一行浮起来了"。
    """
    return p.accent_soft if not p.is_dark else blend(panel_bg(p), p.accent, 0.20)


def chip_bg(p: Palette) -> str:
    """状态胶囊底色。

    米家那支深蓝黑是为"压在照片壁纸上"设计的；我们的顶栏是纯色底，照搬的结果是
    深色主题下胶囊和背景糊在一起，切主题几乎看不出变化。所以改成跟卡片同族的
    表面色：它就是一张缩小的卡片，深浅两套各给一档。
    """
    return p.card


def chip_text(p: Palette) -> str:
    """胶囊文字：跟着主题走，取次级文字色（它是"一眼扫"的信息块，不该比标题抢眼）。"""
    return p.text_soft


def select_dot(p: Palette) -> str:
    """单选圆点"选中"用的颜色：和开关同一个绿，浅色主题下压深一档。

    **不用 ``accent``（蓝）**：这两个色在本项目里分工明确——蓝是"能点 / 有焦点"
    （链接悬停、输入框焦点、家庭标签激活、下拉框里的对勾），绿是"这一项生效了"
    （开关打开、树里的开关列、状态点、确认按钮）。"关闭面板时"选中的那个圆点是
    **结果**不是提示，所以跟开关走；同一页里只剩一种强调色，也不会出现"绿开关
    挨着蓝圆点、视线反而被次要的那一项吸走"。

    深浅只差很小一档：**目标是"和开关同色"，不是"另一种绿"**。浅色白卡上原色
    ``#4CC38A`` 只有 2.2:1，20px 的实心圆看着发虚（开关有轨道和滑块位置帮忙，
    圆点没有），压深 8% 到 ``#46B37F``（约 2.6:1）就够稳，肉眼仍读成同一支绿；
    再往下压（15% 到 ``#41A675``）对比度更好，但两处绿会开始被看成两个色。
    深色主题本来就是 7.5:1，用原色最接近米家。
    """
    return TOGGLE_ON if p.is_dark else blend(TOGGLE_ON, "#000000", 0.08)


def card_hover_color(base: str, p: Palette) -> str:
    """悬停只挪一档明度：深色提亮、浅色压暗（白卡没法再亮）。

    米家靠明度差区分层次，卡片上没有描边也没有投影，悬停自然也不该长出边框。
    """
    return blend(base, "#FFFFFF" if p.is_dark else "#000000", 0.06 if p.is_dark else 0.035)


def _rgb(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


# 字体：优先系统中文黑体。QSS 里的 font-family 只写一个名字就够，
# Qt 会自己往下找。
FONT_STACK = '"Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Noto Sans SC", sans-serif'

# ---------------------------------------------------------------------------
# 设计 token（数值来自真机采集，见 docs/reference/米家App-UI风格参考.md）
# ---------------------------------------------------------------------------

#: 圆角**三档**。米家不是"越圆越好"：60dp 高的按钮圆角也只有 20dp，
#: 所以这里没有"全圆胶囊"这一档，需要圆形的场合自己传 h/2。
RADIUS_CARD = 18  # 卡片 / 分组卡 / 浮窗
RADIUS_PILL = 16  # 状态胶囊、小标签
RADIUS_BUTTON = 20  # 大按钮（场景胶囊）
RADIUS_SMALL = 12  # 卡内小按钮、导航选中块
RADIUS_WINDOW = 12  # 窗口外框（无边框窗口的 setMask 遮罩）

#: 字号（Qt 像素 = dp，两者在这里等价）
FS_TITLE = 24  # 家庭名 / 页面标题
FS_SECTION = 18  # 分区标题（房间名）
FS_TAB_ACTIVE = 20  # 激活的标签
FS_TAB = 16  # 未激活的标签
FS_CARD_TITLE = 16  # 卡片标题、列表行
FS_CARD_SUB = 13  # 卡片副标题、列表副行
FS_METRIC = 28  # 指标大数字
FS_UNIT = 11  # 指标单位

#: 卡片基准宽 = 米家 1×1 卡片的列宽（真机 163dp，取 166 对齐 16px 字号网格）。
#: 卡片**不等宽**地随窗口拉伸：列数由 FlowLayout 按这个宽度算，窗口变宽只会多塞
#: 一列，不会把卡片拉变形 —— 卡片是"图 + 名字"的方块，跟手机桌面图标同理。
CARD_MIN_W = 166
#: 卡片高。168 = 标题带 76 + 内容带 78 + 下边距 14。
#: 所有卡片等高是硬约束：一旦混入 132 和 168，等高网格就散了，
#: FlowLayout 的"放不下换行、有洞就填"也会失效。
CARD_H = 168
#: 卡片之间的列间距（米家真机 8dp）
CARD_GAP = 8


def qpalette(p: Palette) -> QPalette:
    """把主题映射成 QPalette。

    光有 QSS 是不够的：QSS 只画写了规则的那些控件，普通 QWidget 的背景仍然来自
    QPalette。Windows 11 开着深色模式时，系统调色板是深灰，于是"没写样式的容器"
    会变成一块黑——设置窗口、对话框正文最容易踩到。统一设一份调色板，两边就对上了。
    """
    qp = QPalette()
    qp.setColor(QPalette.Window, QColor(p.bg))
    qp.setColor(QPalette.WindowText, QColor(p.text))
    qp.setColor(QPalette.Base, QColor(p.card))
    qp.setColor(QPalette.AlternateBase, QColor(p.bg_soft))
    qp.setColor(QPalette.Text, QColor(p.text))
    qp.setColor(QPalette.Button, QColor(p.bg_soft))
    qp.setColor(QPalette.ButtonText, QColor(p.text))
    qp.setColor(QPalette.BrightText, QColor("#FFFFFF"))
    qp.setColor(QPalette.ToolTipBase, QColor(p.card))
    qp.setColor(QPalette.ToolTipText, QColor(p.text))
    qp.setColor(QPalette.Highlight, QColor(p.accent))
    qp.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
    qp.setColor(QPalette.PlaceholderText, QColor(p.text_mute))
    for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
        qp.setColor(QPalette.Disabled, role, QColor(p.text_mute))
    qp.setColor(QPalette.Disabled, QPalette.Base, QColor(p.bg_soft))
    return qp


def apply_to_app(p: Palette) -> None:
    """把主题装进 QApplication：所有没写 QSS 的控件也跟着变色。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is not None:
        app.setPalette(qpalette(p))


def app_qss(p: Palette) -> str:
    return f"""
    * {{
        font-family: {FONT_STACK};
        outline: none;
    }}
    /* 窗口底色由 WindowFrame 抗锯齿地铺（见 chrome.rounded_path），
       这里不能再给容器铺一块方形的底，否则四角又会变方。 */
    QScrollArea, QScrollArea > QWidget > QWidget {{
        background: transparent;
        border: none;
    }}
    QScrollBar:vertical {{
        background: transparent;
        width: 10px;
        margin: 4px 2px 4px 0;
    }}
    QScrollBar::handle:vertical {{
        background: {rgba(p.text_mute, 0.42)};
        border-radius: 4px;
        min-height: 36px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {rgba(p.text_mute, 0.68)};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
        height: 0; background: none; border: none;
    }}
    QLabel {{
        color: {p.text};
        background: transparent;
    }}
    /* 右键菜单的样式在 ui/menu.py 里：它必须自己带透明背景才画得出真圆角，
       见那边的注释。这里不再重复一份，免得两处打架。 */
    QToolTip {{
        background: {p.card};
        color: {p.text};
        border: 1px solid {p.border};
        border-radius: 8px;
        padding: 6px 9px;
    }}
    """


__all__ = [
    "ACCENT_GREEN",
    "CARD_H",
    "CARD_MIN_W",
    "CATEGORY_COLOR",
    "DARK",
    "FONT_STACK",
    "FS_CARD_SUB",
    "FS_CARD_TITLE",
    "FS_METRIC",
    "FS_SECTION",
    "FS_TAB",
    "FS_TAB_ACTIVE",
    "FS_TITLE",
    "FS_UNIT",
    "LIGHT",
    "NAV_BG_DARK",
    "NAV_BG_LIGHT",
    "RADIUS_BUTTON",
    "RADIUS_CARD",
    "RADIUS_PILL",
    "RADIUS_SMALL",
    "RADIUS_WINDOW",
    "SLIDER_WARM",
    "TOGGLE_ON",
    "Palette",
    "app_qss",
    "apply_to_app",
    "blend",
    "card_hover_color",
    "category_color",
    "chip_bg",
    "chip_text",
    "field_bg",
    "nav_bg",
    "nav_row_bg",
    "panel_bg",
    "panel_divider",
    "panel_row_hover",
    "panel_row_selected",
    "popup_bg",
    "popup_border",
    "popup_row_hover",
    "qpalette",
    "rail_bg",
    "rail_row_bg",
    "rgba",
    "select_dot",
    "surface_bg",
    "tint",
]
