# SPDX-License-Identifier: GPL-3.0-or-later
"""「电脑自动化」四个窗口共用的卡片常量、样式片段与窗口限高。

以前列表窗 / 回收站 / 编辑窗各写一份 QSS，卡片高度 108 / 96、圆角 14 / 13、
底色 ``p.card`` / ``theme.panel_bg`` 全不一样；深色下 ``p.card`` 对对话框底
只有 1.03:1，边界几乎看不出来（``theme.py:214`` 早就写过"深色不能照抄 p.card"）。
这里把公共部分收成一份，各窗口在 ``dialog_qss(p)`` 之后**拼在末尾**。

拼在末尾还有一个作用：盖掉 ``dialog_qss`` 里 ``QLabel#RowSub -> text_mute``。
浅色 ``text_mute``(#9CA3AF) 压在白卡上只有 2.54:1，WCAG AA 正文要 4.5:1；
统一提到 ``text_soft``(#6B7280, 4.83:1)。**不改 ``dialog_qss`` 本身**，避免
波及设置页。

实现取舍见 ``docs/DEVELOPMENT.md``「电脑自动化：四层窗口」。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractScrollArea, QApplication, QWidget, QPushButton

from . import theme

#: 规则卡 / 回收站卡统一高度与圆角。以前 108 / 96、r14 / r13 各一套。
CARD_H = 108
CARD_RADIUS = 14

#: 三段语义色：①触发=强调蓝、②条件=暖琥珀、③动作=薄荷绿
STEP_COLORS = ("accent", "SLIDER_WARM", "ACCENT_GREEN")


def fit_height(default_h: int, ratio: float = 0.85, reserve: int = 0) -> int:
    """窗口默认高度不能写死：小屏幕上 900px 会顶出任务栏。

    ``reserve`` 是给浮窗阴影留白扣的（窗口比内容大一圈，不扣就顶出去）。
    离屏渲染 / 没有屏幕对象时退回默认，验证脚本不受影响。
    """
    screen = QApplication.primaryScreen()
    if screen is None:
        return default_h
    available = int(screen.availableGeometry().height() * ratio) - reserve
    return max(420, min(default_h, available))


def chip_fill(p: theme.Palette, amount: float) -> str:
    """压在**卡片**上的小按钮底色。

    ``blend(p.bg, p.text, 0.12)`` 在深色下算出来是 ``#292A2D``，而卡片底
    ``panel_bg`` 是 ``#28292C``——1.01:1，按钮等于融进卡片，看不见边界
    （用户 11-13 报的回收站「还原」）。所以压在卡片上的按钮要从 panel_bg
    起算，而不是窗口底。
    """
    return theme.blend(theme.panel_bg(p), p.text, amount)


def surface_fill(p: theme.Palette, amount: float) -> str:
    """压在**对话框底**（``surface_bg``）上的按钮底色。

    和 ``chip_fill`` 是一对：那个按 panel_bg 起算（按钮长在卡片上），这个按
    surface_bg 起算（按钮直接长在窗口底上）。弄反了就是一个近乎看不见的按钮：
    浅色下从 #FFFFFF 起算的 #E0E2E3 压在 #E6E7E9 上只有 1.05:1。
    """
    return theme.blend(theme.surface_bg(p), p.text, amount)


def chip_border(p: theme.Palette) -> str:
    """小按钮描边：浅色白卡上靠它撑出边界，深色下补一点层次。"""
    return theme.blend(p.border, p.text, 0.30)


def ghost_qss(p: theme.Palette) -> str:
    """次要按钮（取消 / 直接关闭 / 保存并关闭的另一半）唯一一份样式。

    以前 ``confirm.py`` 里写的是 ``background: bg_soft``：深色下 #15171C 压在
    对话框底 #191A1D 上只有 **1.03:1**，等于没画——用户 11-13 第二次报"这个弹窗的
    按钮又是区分度低"。现在统一从 surface_bg 起算提亮（深色 18% / 浅色 14%，
    1.6:1 / 1.4:1）+ 1px 描边，文字用主文字色。

    **尺寸必须和实心主按钮一样**（用户 11-13 报"右下角两个按钮尺寸没统一"）：
    主按钮 ``padding: 11px 22px``、无边框；这里 ``1px 边框 + 10px 21px``，外框
    正好相等。改一边就要改另一边，验证脚本按 sizeHint 量着。
    """
    fill = surface_fill(p, 0.18 if p.is_dark else 0.14)
    hover = surface_fill(p, 0.26 if p.is_dark else 0.22)
    press = surface_fill(p, 0.32 if p.is_dark else 0.28)
    return f"""
    QPushButton#Ghost {{
        background: {fill}; color: {p.text};
        border: 1px solid {chip_border(p)};
        border-radius: {theme.RADIUS_BUTTON_SOFT}px;
        padding: 10px 21px; font-size: 13px;
    }}
    QPushButton#Ghost:hover {{
        background: {hover}; color: {p.accent}; border-color: {p.accent};
    }}
    QPushButton#Ghost:pressed {{ background: {press}; }}
    QPushButton#Ghost:focus {{ border-color: {p.accent}; }}
    """


def tab_focus_only(root: QWidget) -> None:
    """把窗口里所有按钮的焦点策略收成 ``TabFocus``。

    ``QPushButton`` 默认 ``StrongFocus``：鼠标点一下按钮就把焦点拿走了，而窗口级
    QSS 又给 ``:focus`` 画了描边——点完那个框还留在按钮上，看着像"按下去没弹回来"
    （用户 11-13 报）。收成 ``TabFocus`` 之后鼠标点击不再抢焦点，Tab 键照样能走到、
    键盘用户照样看得见框。**动态创建的按钮不在 ``findChildren`` 范围里**，
    要自己再调一次（见 ``automation._icon_button``）。
    """
    for btn in root.findChildren(QPushButton):
        btn.setFocusPolicy(Qt.TabFocus)


def reserve_scrollbar(scroll: QAbstractScrollArea, *, lock_horizontal: bool = True) -> None:
    """竖向滚动条**常驻占位**：内容宽度不随「条目多没多到要滚」跳 10px。

    默认的 ``ScrollBarAsNeeded`` 下，条目一多滚动条从无到有，viewport 当场被挤掉
    10px，卡牌整排缩一圈；删掉两条又弹回原宽——滚动前后卡片宽度不一样（用户 11-13 报）。
    改成常驻后宽度恒定，卡片不再跟着列表长短变。

    代价是"内容不够高时轨道也在"。Qt 在滚动范围为零时会把滑块拉满整条，透明轨道
    上就多出一根竖线，比没有滚动条还显眼。所以按 ``rangeChanged`` 给滚动条挂
    ``idle`` 属性，``common_qss`` 里把 idle 的 handle 画成透明：

    * 能滚 —— 正常滑块；
    * 不能滚 —— 只剩 10px 透明轨道，看不出任何东西。

    ``QScrollArea`` 和 ``QTableWidget`` / ``QAbstractItemView`` 都是
    ``QAbstractScrollArea``，三者都能接（列表、回收站、编辑窗、日志表格）。

    ``lock_horizontal`` 默认把横向条关掉——竖向滚动区里横向本来就不该出现。
    表格例外：列多到放不下时横向条是必需的，传 ``False`` 保留 ``AsNeeded``
    （横向条只在底下占高度，不影响列宽）。

    四窗都在 ``common_qss`` 末尾拼了那条 idle 规则，所以**只有走 ``common_qss`` 的
    窗口**能调这个函数；别处要占位得先确认 QSS 里有 ``QScrollBar[idle="true"]``。
    """
    scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
    if lock_horizontal:
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    bar = scroll.verticalScrollBar()

    def _sync(*_args: object) -> None:
        idle = bar.maximum() <= bar.minimum()
        current = bar.property("idle")
        if current is not None and bool(current) == idle:
            return
        bar.setProperty("idle", idle)
        # 动态属性改了必须重新 polish，否则 QSS 属性选择器不重算（同 StepBadge）
        bar.style().unpolish(bar)
        bar.style().polish(bar)

    bar.rangeChanged.connect(_sync)
    _sync()


def common_qss(p: theme.Palette) -> str:
    """四窗共用片段，必须拼在各窗口 stylesheet 的**末尾**。"""
    warm = theme.SLIDER_WARM
    green = theme.ACCENT_GREEN
    return f"""
    /* 卡片：深色用相对 surface_bg 提亮的 panel_bg，不能直接用 p.card。
       **描边宽度两个状态必须一样**（停用 2px 中性色 / 启用 2px accent，只换颜色）：
       以前停用写 1px、启用写 2px，描边一换宽，QSS 的 frameWidth 就变，内容区
       跟着缩 1px——启停一下卡片里的文字就挪一下（用户 11-13 报，和按钮
       「焦点只换颜色不换宽度」是同一条规矩）。 */
    QFrame#RuleCard, QFrame#TrashCard {{
        background: {theme.panel_bg(p)};
        border: 2px solid {p.border}; border-radius: {CARD_RADIUS}px;
    }}
    QFrame#RuleCard[on="true"] {{ border: 2px solid {p.accent}; }}
    /* 滚动条常驻占位（见 reserve_scrollbar）：不能滚时把滑块藏掉，
       只留 10px 透明轨道，否则零范围下 Qt 会把滑块拉满整条。 */
    QScrollBar[idle="true"]::handle:vertical,
    QScrollBar[idle="true"]::handle:vertical:hover {{ background: transparent; }}
    /* 说明文字统一 text_soft（浅色 text_mute 对白卡只有 2.54:1） */
    QLabel#RowSub, QLabel#PageHint, QLabel#Hint, QLabel#LogHint,
    QLabel#LogStatus, QLabel#TrashMeta, QLabel#TrashHint, QLabel#TrashEmpty,
    QLabel#RuleKeys, QLabel#ListHint, QLabel#EmptyHint {{ color: {p.text_soft}; }}
    /* 大按钮改「圆角矩形」：RADIUS_BUTTON(20) 在 40px 高的按钮上是胶囊。
       用户 11-13 定，四窗 + 确认框统一这一档。 */
    QPushButton#Primary {{ border-radius: {theme.RADIUS_BUTTON_SOFT}px; }}
    /* 键盘焦点要有可见外框。QSS 特异性：ID 规则会压掉纯 QPushButton:focus，
       所以要把带 objectName 的按钮逐个列出，别只写一条通用的。
       **焦点只换颜色，绝不换边框宽度**：写成 ``border: 2px solid`` 会让内容区
       缩掉 4px，「编辑」「回收站」「还原」这种刚好占满的短文字两侧就被裁掉了
       （用户 11-13 报）。所以无描边的按钮常态就留一条同宽的透明边框，
       焦点只改 border-color；实心按钮（Primary）改成提亮一档。 */
    QPushButton:focus,
    QPushButton#Tool:focus, QPushButton#Outline:focus, QPushButton#Keycap:focus,
    QPushButton#IconTool:focus, QPushButton#RuleEdit:focus,
    QPushButton#RuleDelete:focus, QPushButton#Restore:focus,
    QPushButton#Purge:focus, QPushButton#Disclosure:focus,
    QPushButton#FilterChip:focus {{
        border-color: {p.accent};
    }}
    /* 实心按钮没有描边可用，焦点改成"提亮一档"：同样不动尺寸，键盘用户看得出来。 */
    QPushButton#Primary:focus {{
        background: {theme.blend(green, "#FFFFFF", 0.22)};
    }}
    /* 按下态写在这里（焦点规则之后）：两者特异性相同、写在后面的赢，
       键盘用户 Tab 到按钮再按空格也才有"按下去"的反馈。 */
    QPushButton#Primary:pressed {{
        background: {theme.blend(green, p.bg, 0.18)};
    }}
    /* 三段式：卡片左侧 3px 语义竖条 + 编号圆徽章，段与段不再连成一片 */
    QFrame#SectionCard[step="1"] {{ border-left: 3px solid {p.accent}; }}
    QFrame#SectionCard[step="2"] {{ border-left: 3px solid {warm}; }}
    QFrame#SectionCard[step="3"] {{ border-left: 3px solid {green}; }}
    /* ④ 否则分支：中性色。它不是主流程，别用第四种彩色去和前三段抢注意力 */
    QFrame#SectionCard[step="4"] {{ border-left: 3px solid {p.text_soft}; }}
    QLabel#StepBadge {{
        background: {p.accent_soft}; color: {p.accent};
        border-radius: 11px; font-size: 12px; font-weight: 700;
    }}
    QLabel#StepBadge[step="2"] {{
        background: {theme.blend(p.bg, warm, 0.24)}; color: {p.text};
    }}
    QLabel#StepBadge[step="3"] {{
        background: {theme.blend(p.bg, green, 0.24)}; color: {p.text};
    }}
    QLabel#StepBadge[step="4"] {{
        background: {theme.blend(p.bg, p.text_soft, 0.20)}; color: {p.text};
    }}
    /* 禁用态必须自己写：外观全由 QSS 接管后，Qt 不会再把按钮自动画灰。
       用在 ② 没有条件时的 ④「＋ 选择设备 / ＋ 延迟」。颜色用 text_soft 而不是 mute：
       深色下 mute 和卡片底几乎同色，按钮会"消失"，看不出这里有个入口。 */
    QPushButton#Tool:disabled {{
        color: {p.text_soft}; border-color: {p.border}; background: transparent;
    }}
    """ + ghost_qss(p)


__all__ = [
    "CARD_H",
    "CARD_RADIUS",
    "STEP_COLORS",
    "chip_border",
    "chip_fill",
    "common_qss",
    "fit_height",
    "ghost_qss",
    "reserve_scrollbar",
    "surface_fill",
    "tab_focus_only",
]

