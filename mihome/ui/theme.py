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

import colorsys
import math
from dataclasses import dataclass, replace

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QLinearGradient, QPalette


def _rgb(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def blend(hex_a: str, hex_b: str, t: float) -> str:
    """两个 #rrggbb 之间线性插值，t=0 取 a。"""
    a = _rgb(hex_a)
    b = _rgb(hex_b)
    return "#%02X%02X%02X" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


@dataclass(frozen=True)
class Surface:
    """一块背景面：窗口底 + 它上面的卡片面色。

    这里只管两件事，其它 token 由明暗模式决定（见 :class:`Palette`）：

    * 窗口底是纯色还是渐变；
    * 卡片是白色半透明还是深色半透明、透多少。

    ``card_alpha = 1.0`` 表示不透明——黑白预设用它保持改造前的观感。
    """

    mode: str = "solid"
    stops: tuple[str, ...] = ("#F1F2F4",)
    angle: float = 135.0
    card: str = "#FFFFFF"
    card_alpha: float = 1.0

    def is_gradient(self) -> bool:
        return self.mode == "gradient" and len(self.stops) >= 2

    def base(self) -> str:
        """整块背景的代表色（纯色 = 它自己，渐变 = 首末两端的中点）。

        文字对比度、弹窗 ``surface_bg``、侧栏暗一档都以它为基准；渐变两端只影响
        窗口自己那一层，不参与派生。
        """
        if not self.is_gradient():
            return self.stops[0]
        return blend(self.stops[0], self.stops[-1], 0.5)

    def nav_alpha(self) -> float:
        """纯色档的侧栏不透明度：跟卡片一起半透明，但比卡片实一点。

        只给 :func:`nav_fill` 的**纯色**分支用——渐变档不再用 ``nav_bg`` 那一支
        推导色，而是直接铺"卡面"（见 :func:`nav_fill`），所以不需要这个数。
        """
        return 1.0 if self.card_alpha >= 1.0 else min(1.0, self.card_alpha + 0.08)

    def brush(self, rect: QRectF) -> QColor | QLinearGradient:
        """给 ``WindowFrame`` 用的填充刷子。参数是面板矩形，不含阴影留白。"""
        if not self.is_gradient():
            return QColor(self.stops[0])
        radians = math.radians(self.angle)
        dx, dy = math.cos(radians), math.sin(radians)
        # 让渐变线正好斜跨矩形：按角度投影半长，起点/终点都压在矩形边界上。
        half = (abs(rect.width() * dx) + abs(rect.height() * dy)) / 2.0
        center = rect.center()
        gradient = QLinearGradient(
            center.x() - dx * half, center.y() - dy * half,
            center.x() + dx * half, center.y() + dy * half,
        )
        last = len(self.stops) - 1
        for i, color in enumerate(self.stops):
            gradient.setColorAt(i / last, QColor(color))
        return gradient


@dataclass(frozen=True)
class Preset:
    """一个背景预设：浅色 / 深色各一块面。"""

    id: str
    label: str
    light: Surface
    dark: Surface


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
    #: 当前背景预设 id（``mono`` / ``sand`` / ...），设置页靠它回填选中块。
    preset: str
    #: 卡片不透明度。1.0 = 实色；小于 1 时卡片 / 侧栏让窗口底透出来。
    card_alpha: float
    #: 当前背景面（窗口底 + 卡片色）。几何由它自己知道，颜色 token 不掺。
    surface: Surface
    is_dark: bool = False


#: 黑白预设就是改造前的两套调色板：浅色极简白、深色近黑，卡片实色。
#: 卡片 alpha 取 1.0，所以默认观感逐像素保持原样。
MONO_LIGHT = Surface("solid", ("#F1F2F4",), card="#FFFFFF", card_alpha=1.0)
MONO_DARK = Surface("solid", ("#0E0F12",), card="#1B1D22", card_alpha=1.0)

LIGHT = Palette(
    name="light",
    bg=MONO_LIGHT.stops[0],
    bg_soft="#F7F8FA",
    card=MONO_LIGHT.card,
    card_hover="#FCFCFD",
    border="#EAEBEE",
    text="#0E1116",
    text_soft="#6B7280",
    text_mute="#9CA3AF",
    accent="#3D7EFF",
    accent_soft="#EAF1FF",
    shadow="#00000014",
    overlay="#FFFFFF",
    preset="mono",
    card_alpha=MONO_LIGHT.card_alpha,
    surface=MONO_LIGHT,
)

DARK = Palette(
    name="dark",
    bg=MONO_DARK.stops[0],
    bg_soft="#15171C",
    card=MONO_DARK.card,
    card_hover="#22252B",
    border="#26282E",
    text="#F2F3F5",
    text_soft="#A0A6AF",
    text_mute="#6C727C",
    accent="#5B93FF",
    accent_soft="#1E2A42",
    shadow="#00000055",
    overlay="#1B1D22",
    preset="mono",
    card_alpha=MONO_DARK.card_alpha,
    surface=MONO_DARK,
    is_dark=True,
)


#: 24 个背景预设：1 个黑白 + 6 个纯色 + 6 个渐变 + 11 个编号渐变（体验用的候选），
#: 每个都有浅 / 深两档。
#:
#: 取色原则（也是这组数值的来源）：
#: * 浅色档**明度基本不动**，把饱和度抬上去（S 从 30%~45% 提到 70%~90%），
#:   得到的是"更实的粉彩"而不是"更白的灰"；明度再压下去，``text_soft`` 的对比度
#:   会先跌破现状（实测 #F7E6C6 那档 soft 只有 3.93，低于基线 4.15），所以浅色
#:   只能靠色相/饱和加存在感，这也是"没抬到头"的原因；
#: * 深色档反过来：明度必须抬到 HSL 12%~16%（L* 12~16），色相才认得出来——
#:   原来 #17130E 一带 HSL 明度只有 7%，肉眼就是黑。代价是正文对比度从 16.5:1
#:   降到 13.4~14.7:1，仍在 AA（4.5:1）三倍以上；
#: * 深色卡片不再共用一支近黑 #202329：底色有明确色相之后，中性的蓝灰卡片会
#:   和米黄 / 浅粉打架。每档卡片都从自己的底色往白提 12%，于是卡片和底色同色相
#:   同族，明度差反而从 11~14 提到 16~22；
#: * 渐变档 2026-11-17 重做：**统一 5 个色标、统一明度**（浅色 L84 / S88，深色
#:   L 从右上 23 递减到左下 11、S78），方向都是色标 0 在右上。原来六档都是"两端
#:   双色"，中间只能靠插值出灰白，色相跨度一小就看起来很平；改成 5 段之后每档都
#:   有一条能认出来的色相弧（3 号 77°→216°、2 号 200°→266°、6 号 186°→230°）。
#:   浅色 L84 是硬上限：``text_soft`` 在黄绿那端正好 3.04（再暗到 L80 就是 2.71，
#:   低于下限 3.83），所以**不要再给任何一档单独压明度**；
#: * 渐变档的卡片跟着透：``card_alpha`` 浅 0.42 / 深 0.50；侧栏不再单独给不透明度，
#:   渐变档直接铺"**卡面**"（``pal.card`` @ ``card_alpha``，见 :func:`nav_fill`）——
#:   侧栏占整窗 11.5%，从前压成 0.16 的暗色蒙版读起来是一块脏灰，换成和指令条、
#:   卡片同一支白卡半透明之后，才像"一整块磨砂玻璃"。
#:   纯色档保持 0.80 / 0.90，它们背景本身就是一块平色，透与不透没有区别；
#: * 描边、``bg_soft`` 是派生值，跟着 ``bg`` 走（见 :func:`palette_for`）——
#:   深色卡片抬亮之后，骨架里那支 #26282E 的描边压在底色上只剩 1.06:1，等于没画。
#:
#: 2026-11-17 追加 **11 套编号渐变**（下面 ``_NUMBERED``）：用户从网上收来一批 CSS
#: 渐变，先用数字当名字，体验一轮再决定留哪些。它们的色标不手写，由 :func:`_grade`
#: 按上面同一条语法生成——浅色 L84、深色 L23→11，只保留设计稿的色相弧与饱和度走势。
#:
#: 同日稍晚用户又定：**渐变档全部用编号命名**（具名与编号混排太乱），所以原有 6 套
#: 渐变的 ``label`` 也清空，统一由 ``GRADIENT_PRESETS`` 按显示顺序补 1–17。``id`` 不动
#: ——它是 ``settings.json`` 的持久化键。
#:
#: **CSS 角度 → ``Surface.angle``**：CSS 的 0° 指"上"、顺时针；这里 0° 指"右"、也顺时针
#: （y 轴向下），差一个 90°：``Xdeg → (X - 90) % 360``。常用对照：
#: ``to top`` = 270、``to bottom`` = 90、``to right`` = 0、``to left`` = 180、
#: ``to right top`` = 315、``to left bottom`` = 135。色标顺序**照 CSS 原样**写
#: （``stops[0]`` 就是 CSS 里的第一个颜色）；``aurora``（原"极光"）那档当年是拿
#: 135° 反着写的，两者等价，注释里已说明，不必再动。

#: 编号预设的浅色明度 / 深色明度台阶（与上面 6 档渐变同一套语法）。
_GRADE_LIGHT_L = 84.0
_GRADE_DARK_L = (23.0, 20.0, 17.0, 14.0, 11.0)
#: 色标数：和上面 6 档一样取 5 个。
_GRADE_STOPS = 5


def _hsl(hex_color: str) -> tuple[float, float, float]:
    r, g, b = (v / 255.0 for v in _rgb(hex_color))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    return h * 360.0, s * 100.0, l * 100.0


def _hsl_hex(hue: float, sat: float, light: float) -> str:
    r, g, b = colorsys.hls_to_rgb((hue % 360.0) / 360.0, light / 100.0, sat / 100.0)
    return "#%02X%02X%02X" % (round(r * 255), round(g * 255), round(b * 255))


def _grade(src: tuple[str, ...], *, light_sat, dark_sat: float):
    """把设计稿上那串颜色"翻译"成这个项目的浅 / 深两档色标。

    设计稿给的是一条**完整的渐变**（色相、明度、饱和都在变），而本项目对背景的硬
    约束只有一条：浅色档明度钉在 L84（再暗 ``text_soft`` 就跌破可读线）、深色档钉在
    L23→11 的台阶上（见上面那组取色原则）。所以这里只把**色相弧和饱和度走势**留下来：

    * 色相沿相邻两个设计色的**最短弧**插值（``d = (h2 - h1 + 540) % 360 - 180``）。
      直接在 RGB 空间取中点会出灰（青 → 品红的中点是一坨灰），按弧插值才是设计意图
      里的"经过蓝紫到粉"，深色档也不会糊成一片泥；
    * 饱和度按调用方给的走势线性插值（``light_sat`` 可以给 ``(起, 止)``）。"蓝 → 近白"
      这类设计稿的层次其实全在**明度**上，明度被钉死之后只能让饱和度来扛，于是它变成
      "饱和的蓝 → 很淡的灰蓝"——仍是同一个意思，而且卡片始终能从背景里浮出来；
    * 明度一律重写：浅色 5 个色标都是 L84，深色按 ``_GRADE_DARK_L`` 递减。

    返回 ``(浅色色标, 深色色标)``，正好喂给 ``Surface``。深色卡片按"底色往白提 16%"
    现算（与手写那 6 档的 #46426D / #2B3E3D 同一档，见 ``PRESETS`` 第 3 条原则）。
    """
    n_src = len(src)
    raw = [_hsl(c)[0] for c in src]
    hues: list[float] = []
    for i in range(_GRADE_STOPS):
        pos = (i / (_GRADE_STOPS - 1)) * (n_src - 1)
        j = min(int(pos), n_src - 2)
        delta = (raw[j + 1] - raw[j] + 540.0) % 360.0 - 180.0
        hues.append((raw[j] + delta * (pos - j)) % 360.0)

    if isinstance(light_sat, (tuple, list)):
        lo, hi = light_sat
        sats = [lo + (hi - lo) * (i / (_GRADE_STOPS - 1)) for i in range(_GRADE_STOPS)]
    else:
        sats = [float(light_sat)] * _GRADE_STOPS

    light = tuple(_hsl_hex(h, sats[i], _GRADE_LIGHT_L) for i, h in enumerate(hues))
    dark = tuple(_hsl_hex(h, dark_sat, _GRADE_DARK_L[i]) for i, h in enumerate(hues))
    card = blend(blend(dark[0], dark[-1], 0.5), "#FFFFFF", 0.16)
    return light, dark, card


def _numbered(pid: str, src: tuple[str, ...], angle: float,
              light_sat, dark_sat: float) -> Preset:
    """编号渐变预设：一次把浅 / 深两档和深色卡片都算出来。

    ``label`` 不在这里填——渐变档的编号统一在 ``GRADIENT_PRESETS`` 里按显示
    顺序补，见下面那段注释。
    """
    light, dark, card = _grade(src, light_sat=light_sat, dark_sat=dark_sat)
    return Preset(
        pid, "",
        Surface("gradient", light, angle, "#FFFFFF", 0.42),
        Surface("gradient", dark, angle, card, 0.50),
    )


def hue_gap(a: Surface, b: Surface) -> float:
    """两档渐变"最像的那一对色标"差多少度色相——0 就是有色标完全同色。

    :data:`_GRADIENT_ORDER` 拿它当排序判据（当时把 17 档两两都比了一遍）。

    为什么不比**平均色相之差**：5 个色标在色相环上是摊开的一段弧，平均值只是
    这团点的"重心"，不代表任何一个色标。3 号（薄荷→天蓝）和 16 号（暖白→天蓝）
    的重心差了 117.6°，听上去毫不相干，可两边各有一个色标落在同一处（差 0.1°），
    摆在一起肉眼就是接得上的；反过来，重心只差 9° 的两档也可能是"各占一头、
    中间隔着整条弧"。所以"像不像"要看**两个集合之间的最近距离**，不看代表色。

    只算浅色面：两档的深色面走的是同一条色相弧（``_grade`` 只重写明度和饱和
    度），顺序不会因为明暗而变。
    """
    def circular(x: float, y: float) -> float:
        d = abs(x - y) % 360.0
        return min(d, 360.0 - d)

    return min(circular(_hsl(x)[0], _hsl(y)[0])
               for x in a.stops for y in b.stops)


#: 11 套从网上收的 CSS 渐变（用户 11-17 提供），浅深两档由 ``_grade`` 补齐。
#: 注释里的"源稿 #N"是用户原文里的次序，**不是**界面上的编号。
_NUMBERED: tuple[Preset, ...] = (
    # 源稿 #1  120deg  #a1c4fd → #c2e9fb    蓝 → 淡青
    _numbered("n1", ("#a1c4fd", "#c2e9fb"), 30.0, (88, 58), 72.0),
    # 源稿 #2  to top  #a18cd1 → #fbc2eb    藕紫 → 粉
    _numbered("n2", ("#a18cd1", "#fbc2eb"), 270.0, (46, 88), 66.0),
    # 源稿 #3  120deg  #84fab0 → #8fd3f4    薄荷绿 → 天蓝
    _numbered("n3", ("#84fab0", "#8fd3f4"), 30.0, (88, 84), 70.0),
    # 源稿 #4  to top  #a8edea → #fed6e3    薄荷 → 樱粉
    _numbered("n4", ("#a8edea", "#fed6e3"), 270.0, (62, 88), 52.0),
    # 源稿 #5  to top  #30cfd0 → #330867    青 → 深紫（按深色档做，浅色是它的浅版）
    _numbered("n5", ("#30cfd0", "#330867"), 270.0, (62, 80), 78.0),
    # 源稿 #6  to top  #fff1eb → #ace0f9    暖白 → 天蓝（近白那端靠饱和度收）
    _numbered("n6", ("#fff1eb", "#ace0f9"), 270.0, (30, 76), 40.0),
    # 源稿 #7  to top  #accbee → #e7f0fd    浅蓝 → 近白
    _numbered("n7", ("#accbee", "#e7f0fd"), 270.0, (74, 22), 62.0),
    # 源稿 #8  to top  #6a85b6 → #bac8e0    钢蓝 → 浅灰蓝
    _numbered("n8", ("#6a85b6", "#bac8e0"), 270.0, (52, 18), 58.0),
    # 源稿 #9  to top  #a7a6cb → #8989ba    灰紫（中间停在 52%，本来就是一块平色）
    _numbered("n9", ("#a7a6cb", "#8989ba", "#8989ba"), 270.0, (26, 34), 46.0),
    # 源稿 #10  to right top  #42bdb2 … #f4edaf  青绿 → 黄绿（5 个色标）
    _numbered("n10", ("#42bdb2", "#74cbaa", "#a0d8a5", "#cbe3a6", "#f4edaf"), 315.0,
              (52, 70), 66.0),
    # 源稿 #11  -225deg(=135deg)  #CBBACC → #2580B3  藕灰 → 深青蓝（也按深色档做）
    _numbered("n11", ("#CBBACC", "#2580B3"), 45.0, (20, 62), 66.0),
)

#: 纯色档（窗口底是一块平色，卡片保持接近实心）。
#: 名字一律用**直白的色相词**："鼠尾草 / 藕粉 / 石板"这类设计圈叫法好看但指不准，
#: 用户 11-17 点名要"深绿"这种一眼看懂的说法。注意名字要跨明暗两档：``sage`` 的
#: 浅色底是 #EBF4E0（极浅的黄绿）、深色底是 #172C1B（墨绿），所以叫**浅绿**而不是
#: 深绿——叫深绿会和浅色档打架。
SOLID_PRESETS: tuple[Preset, ...] = (
    Preset("mono", "黑白", MONO_LIGHT, MONO_DARK),
    Preset("sand", "米黄",
           Surface("solid", ("#FBEFD9",), card="#FFFFFF", card_alpha=0.80),
           Surface("solid", ("#2E2114",), card="#473C30", card_alpha=0.90)),
    Preset("mist", "浅蓝",
           Surface("solid", ("#EBF2FE",), card="#FFFFFF", card_alpha=0.80),
           Surface("solid", ("#142438",), card="#303E50", card_alpha=0.90)),
    Preset("sage", "浅绿",
           Surface("solid", ("#EBF4E0",), card="#FFFFFF", card_alpha=0.80),
           Surface("solid", ("#172C1B",), card="#334536", card_alpha=0.90)),
    Preset("rose", "浅粉",
           Surface("solid", ("#FEEEF4",), card="#FFFFFF", card_alpha=0.80),
           Surface("solid", ("#331A23",), card="#4B353D", card_alpha=0.90)),
    Preset("lavender", "浅紫",
           Surface("solid", ("#F5EDFF",), card="#FFFFFF", card_alpha=0.80),
           Surface("solid", ("#241B38",), card="#3E3650", card_alpha=0.90)),
    Preset("slate", "灰蓝",
           Surface("solid", ("#E9F0F7",), card="#FFFFFF", card_alpha=0.80),
           Surface("solid", ("#1A2530",), card="#353F49", card_alpha=0.90)),
)

#: 手写色标的 6 套渐变。``label`` 先留空，由下面的 ``GRADIENT_PRESETS`` 按显示
#: 顺序统一补编号——编号必须等于界面次序，在构造处和排序处各写一遍迟早对不上。
#: **这里的先后不是界面次序**，界面次序见 :data:`_GRADIENT_ORDER`。
_HANDWRITTEN: tuple[Preset, ...] = (
    Preset("dawn", "",
           Surface("gradient",
                   ("#FADCB2", "#FAC4B2", "#FAB2C4", "#FAB2FA", "#CAB2FA"),
                   135.0, "#FFFFFF", 0.42),
           Surface("gradient",
                   ("#68420D", "#5B1F0B", "#4D0A1A", "#400840", "#150632"),
                   135.0, "#614B48", 0.50)),
    Preset("night", "",
           Surface("gradient",
                   ("#D1B2FA", "#B9B2FA", "#B2C1FA", "#B2D1FA", "#B2E2FA"),
                   135.0, "#FFFFFF", 0.42),
           Surface("gradient",
                   ("#350D68", "#130B5B", "#0A174D", "#082040", "#062332"),
                   135.0, "#46426D", 0.50)),
    Preset("aurora", "",
           # 试验（2026-11-17，用户给的 CSS 线性渐变）：源串是
           # ``to right top, #051937, #004d7a, #008793, #00bf72, #a8eb12``，
           # 即**左下深蓝 → 右上黄绿**。这里 135° 的色标 0 落在**右上角**，
           # 所以整串要反过来写，视觉方向才对得上。
           #
           # 浅色档：同一串色相 + 同一方向，但明度整体抬到 L84。原串明度只有
           # 12–50%，浅色模式的文字是深色（#0E1116），压上去直接读不出来；
           # 这一版保留 5 个色标的色相弧和**饱和度 100% 的青绿段**，只是把明度
           # 提到浅色背景该在的位置。卡片透明度压到 0.42，让背景真的从控件底下
           # 透出来（原来 0.76/0.84 等于没透）。
           #
           # 深色档的卡片色 #44635A —— 原来写的是 #2B3E3D，亮度 57.9，和这支渐变
           # 的代表底色（#2A4020，L 57.0）**撞在一起**，卡片、搜索框、语音指令条
           # 全部平贴在背景上（用户 11-17 报"搜索框还是不满意"）。色相没动（还是
           # H≈160 的青绿，跟 #0B5B3B / #0A464D 之间），只把明度提到 L 91.8：
           # 卡-背 34.7、搜索框-底带 12.7，和其余深色渐变档（33–36 / 12–15）同档。
           Surface("gradient",
                   ("#E4F9B3", "#ADFFDE", "#ADF8FF", "#ADE1FF", "#B4CFF8"),
                   135.0, "#FFFFFF", 0.42),
           Surface("gradient",
                   ("#4E680D", "#0B5B3B", "#0A464D", "#082B40", "#061832"),
                   135.0, "#44635A", 0.50)),
    Preset("dusk", "",
           Surface("gradient",
                   ("#FAB2D6", "#FAB2FA", "#DCB2FA", "#C4B2FA", "#B2B2FA"),
                   135.0, "#FFFFFF", 0.42),
           Surface("gradient",
                   ("#680D3B", "#5B0B5B", "#310A4D", "#160840", "#060632"),
                   135.0, "#56366D", 0.50)),
    Preset("ember", "",
           Surface("gradient",
                   ("#FAC4B2", "#FAD4B2", "#FAE2B2", "#FAD0B2", "#FABCB2"),
                   135.0, "#FFFFFF", 0.42),
           Surface("gradient",
                   ("#68190D", "#5B2C0B", "#4D370A", "#402208", "#321106"),
                   135.0, "#6D5B36", 0.50)),
    Preset("steel", "",
           Surface("gradient",
                   ("#B2BEFA", "#B2C9FA", "#B2D6FA", "#B2E5FA", "#B2F3FA"),
                   135.0, "#FFFFFF", 0.42),
           Surface("gradient",
                   ("#0D1C68", "#0B245B", "#0A2B4D", "#082F40", "#062E32"),
                   135.0, "#394E6F", 0.50)),
)

#: 17 套渐变的**界面次序**——用户 11-17 定：「颜色接近的挨着放」。
#:
#: 排序只认一个量：每档**浅色面 5 个色标的圆形平均色相**（深色面同一条色相弧，
#: 不用另排）。按色相环从橙红出发绕一圈，末尾一档正好接回第一档，整条是闭环：
#:
#:   橙红(23°) → 青绿黄绿(120°) → 薄荷天蓝(171°) → 黄绿深蓝(174°) →
#:   蓝(208°) → 钢蓝(209°) → 浅蓝(213°) → 灰蓝(219°) → 青紫(224°) →
#:   紫蓝(231°) → 灰紫(240°) → 藕灰青蓝(249°) → 薄荷樱粉(259°) →
#:   粉紫蓝(279°) → 藕紫粉(287°) → 暖白天蓝(289°) → 橙粉紫(338°) ↺ 橙红
#:
#: 括号里是平均色相。相邻两档不只看均值的差——真正决定"像不像"的是两边色标弧
#: 的**重叠**，实测这条链上多数相邻对的端点几乎同色（比如 9/10 号是 266.8° 对
#: 265.8°，16/17 号是 17.5° 对 35°），全链色相距离在 0.4–1.6 之间，没有断口。
#:
#: 13、16 号（薄荷→樱粉、暖白→天蓝）是两条 180° 宽弧彩虹，放哪都跟邻居像一半，
#: 按均值落在紫色段，两边的端点色仍能接上，所以没单独挪。
#:
#: 改这里的顺序 = 改界面编号（1 号就是第一个），但 **``id`` 别跟着改**——它才是
#: ``settings.json`` 的持久化键，用户选中的档不能因为重排就丢。
_GRADIENT_ORDER: tuple[str, ...] = (
    "ember",     # 1  橙红
    "n10",       # 2  青绿 → 黄绿
    "n3",        # 3  薄荷绿 → 天蓝
    "aurora",    # 4  黄绿 → 深蓝
    "n1",        # 5  蓝 → 淡青
    "steel",     # 6  钢蓝 → 浅青
    "n7",        # 7  浅蓝 → 近白
    "n8",        # 8  钢蓝 → 浅灰蓝（和 7 号几乎同色，必须挨着）
    "n5",        # 9  青 → 深紫
    "night",     # 10 紫 → 天蓝（起点 265.8° 接 9 号终点 266.8°）
    "n9",        # 11 灰紫
    "n11",       # 12 藕灰 → 深青蓝
    "n4",        # 13 薄荷 → 樱粉
    "dusk",      # 14 粉 → 紫 → 蓝
    "n2",        # 15 藕紫 → 粉
    "n6",        # 16 暖白 → 天蓝
    "dawn",      # 17 橙 → 粉 → 紫（回到橙红，闭环）
)

_ALL_GRADIENTS: tuple[Preset, ...] = _HANDWRITTEN + _NUMBERED
_GRADIENT_BY_ID = {p.id: p for p in _ALL_GRADIENTS}
#: 顺序表漏一档、写错 id 都会在这里当场炸掉，不用等界面渲染才发现少一个色块。
if set(_GRADIENT_ORDER) != set(_GRADIENT_BY_ID) or len(_GRADIENT_ORDER) != len(_ALL_GRADIENTS):
    raise ValueError(f"渐变顺序表与定义不符：多余/缺失 {set(_GRADIENT_ORDER) ^ set(_GRADIENT_BY_ID)}")
_GRADIENTS: tuple[Preset, ...] = tuple(_GRADIENT_BY_ID[i] for i in _GRADIENT_ORDER)

#: 渐变档**全部用编号命名**（2026-11-17 用户定：具名与编号混排时，"晨光 / 夜色 /
#: 极光"和"1 / 2 / 3"两套指法互相打架，而这 17 套里本来就有几对颜色很接近
#: ——那两套几乎同色的浅蓝／灰蓝、还有"青→紫"和"紫→天蓝"这一类——名字反而
#: 更容易指错）。**编号只由 :data:`_GRADIENT_ORDER` 决定**，这里只负责补 label；
#: 色块标签、文档里的对照表都从 ``label`` 读，别在构造处手写数字。
GRADIENT_PRESETS: tuple[Preset, ...] = tuple(
    replace(p, label=str(i + 1)) for i, p in enumerate(_GRADIENTS)
)

#: 设置页按 ``SOLID_PRESETS`` / ``GRADIENT_PRESETS`` 分两块摆（中间一条分隔线），
#: 这里保持"纯色在前、渐变在后"的显示顺序。
PRESETS: tuple[Preset, ...] = SOLID_PRESETS + GRADIENT_PRESETS

PRESET_IDS: tuple[str, ...] = tuple(p.id for p in PRESETS)
_PRESET_BY_ID = {p.id: p for p in PRESETS}


def palette_for(preset_id: str, mode: str) -> Palette:
    """按「背景预设 + 明暗模式」生成调色板。

    骨架（文字三级 / 阴影 / 压暗层 / 强调色 / ``card_hover``）整份照抄
    ``LIGHT`` / ``DARK``，**不为每个背景再造一套**；``bg`` / ``card`` /
    ``card_alpha`` / ``surface`` 跟着预设走。

    ``bg_soft`` 与 ``border`` 也必须跟着 ``bg`` 派生，原因只有一个：这两支在
    骨架里是**照黑白那对底色**定的。深色预设把底色抬到 HSL 14% 之后，骨架的
    描边 #26282E 压在底色上只剩 1.06:1（等于没画），``bg_soft`` #15171C 比底色
    还暗一截（搜索框、圆形按钮会变成几个黑洞）。所以：

    * ``bg_soft`` —— 从底色往白提一档（浅色 55%、深色 3.5%），保持"比底色亮一点"，
      和黑白下 ``#F1F2F4 → #F7F8FA`` 的层级关系一致，同时带上底色色相；
    * ``border`` —— 浅色从底色往文字压 5.5%（比 #EAEBEE 更贴底色，也更暖）；
      深色从**卡片**往文字提 9%：它要同时压在底色和卡片上，取卡片为基准才两头都看得见
      （对底色 1.7:1 以上、对卡片 1.3:1 以上）。

    认不出的 id 或 mode 直接退回「黑白 + 浅色」，不做迁移分支。
    """
    preset = _PRESET_BY_ID.get(preset_id, PRESETS[0])
    dark = mode == "dark"
    if preset.id == "mono":
        # 返回原对象：很多地方靠 ``palette is theme.LIGHT`` 判断"没换过"。
        return DARK if dark else LIGHT
    skeleton = DARK if dark else LIGHT
    surface = preset.dark if dark else preset.light
    base = surface.base()
    if dark:
        bg_soft = blend(base, "#FFFFFF", 0.035)
        border = blend(surface.card, skeleton.text, 0.09)
    else:
        bg_soft = blend(base, "#FFFFFF", 0.55)
        border = blend(base, skeleton.text, 0.055)
    return replace(
        skeleton,
        bg=base,
        bg_soft=bg_soft,
        border=border,
        card=surface.card,
        card_alpha=surface.card_alpha,
        preset=preset.id,
        surface=surface,
    )


def card_fill(color: str, palette: Palette) -> QColor:
    """卡片填充。``card_alpha = 1.0``（黑白预设）原样返回不透明色。"""
    result = QColor(color)
    if palette.card_alpha < 1.0:
        result.setAlphaF(palette.card_alpha)
    return result


def alpha(color: str, factor: float) -> QColor:
    result = QColor(color)
    result.setAlphaF(factor)
    return result


def nav_fill(palette: Palette) -> QColor:
    """侧栏底色。

    **渐变档**：直接铺"卡面"——``pal.card`` 带 ``card_alpha``，和卡片、快捷语音
    指令条、搜索框完全同一支色。从前的做法是"``nav_bg`` 压一档 + 16% 不透明度"，
    在渐变上读起来是一块发灰的蒙版（用户 2026-11-17：「左侧边栏完全没有渐变，
    是一块纯色」→ 改成 16% 之后又「颜色也相应调整成类似快捷语音指令框处的颜色」）。
    卡面方案两件事一起解决：既真的透出背景渐变，又和别的控件同族。

    **纯色档**：仍是 ``nav_bg`` 那一档（比背景亮/暗一点点）+ ``nav_alpha``，
    和改造前逐像素一致——它们的背景本来就是一块平色，铺卡面反而会变成"一条白带"。
    """
    if palette.surface.is_gradient():
        return card_fill(palette.card, palette)
    return alpha(nav_bg(palette), palette.surface.nav_alpha())


def nav_row_fill(palette: Palette) -> QColor:
    """侧栏选中行 / 功能区选中格的底。

    渐变档不能再用 ``nav_row_bg``：浅色档那支就是 ``pal.card``，和侧栏卡面**同色**，
    选中行会整个消失。所以这里在卡面基础上再实 26%，形成"栏 0.42 → 悬停 0.54 →
    选中 0.68"的三级；纯色档保持原样（浅色白卡、深色栏底提亮）。
    """
    if palette.surface.is_gradient():
        return alpha(palette.card, min(1.0, palette.card_alpha + 0.26))
    return card_fill(nav_row_bg(palette), palette)


def nav_hover_fill(palette: Palette) -> QColor:
    """侧栏悬停底（房间行、功能区、设置入口共用）。

    渐变档：卡面再实 12%（0.42 → 0.54），比选中行弱一档。纯色档：在栏底上往文字色
    混 6%，和改造前一致（那时栏底是一块实色，混色结果本来就是实色）。
    """
    if palette.surface.is_gradient():
        return alpha(palette.card, min(1.0, palette.card_alpha + 0.12))
    return QColor(blend(nav_bg(palette), palette.text, 0.06))


def nav_line(palette: Palette) -> QColor:
    """侧栏内部那两条分隔线的颜色。

    渐变档侧栏是一块浅色卡面，``pal.border``（从**背景**推的深灰）压上去会变成一条
    实线；改成"从卡面往文字色压 10%"并跟着 ``card_alpha`` 一起透，才和卡内的
    ``panel_divider`` 同一个语感。
    """
    if palette.surface.is_gradient():
        return alpha(blend(palette.card, palette.text, 0.10),
                     min(1.0, palette.card_alpha + 0.10))
    return QColor(palette.border)


def window_edge(p: Palette) -> str:
    """主窗口最外圈的 1px 描边。

    主窗口能最大化、没有浮窗留白，阴影无处可画；但去掉系统窗框之后，浅色底
    落在浅色桌面上会整块融掉。这里从**当前背景**推一条 1px 边：浅色往深推、
    深色往浅推，换任何背景预设都跟着走。最大化时不画（见 ``chrome._FrameBorder``）。
    """
    return blend(p.bg, p.text, 0.18 if p.is_dark else 0.12)


def qss_alpha(factor: float) -> float:
    """把"想要的观感 alpha"反解成 QSS 里该写的值。

    实测（Qt 6.8/PySide6，离屏渲染 ARGB32 取样）：QSS 的 ``background`` 会被画
    两遍——声明 ``rgba(..., a)`` 实际得到 ``1-(1-a)^2``。0.60 声明出来是 0.84，
    卡片就几乎不透。这里反解 ``a = 1 - sqrt(1 - factor)``，让最终观感回到设计值。
    只影响 QSS 面；窗口底色是单次绘制，不走这个函数。
    """
    target = max(0.0, min(1.0, float(factor)))
    return 1.0 - (1.0 - target) ** 0.5


def fill_qss(color: str, palette: Palette) -> str:
    """QSS 用的填充色：卡片实色时原样返回 ``#rrggbb``，半透明时给 ``rgba(...)``。

    实色分支**不进 rgba**：QSS 里 ``rgba(r,g,b,1.00)`` 虽然等价，但没必要让
    默认外观经过一次格式转换，升级后要能说清"逐字节没变"。

    只有主窗口里的 QSS 面会用到它（automation 卡片、placeholder 卡片）——
    弹窗内部一律实色，不使用 QSS 半透明。
    """
    if palette.card_alpha >= 1.0:
        return color
    return rgba(color, qss_alpha(palette.card_alpha))


#: 开关"打开"、单选选中、执行按钮用的薄荷绿（米家实测 #4CC38A）。
#: 故意不用 accent（蓝）：蓝开关看起来像"可点"，绿色才像"通电/生效"。
#: 单选圆点也归这里（见 :func:`select_dot`）——它表达的是"这一项生效了"，
#: 和开关打开是同一件事。
TOGGLE_ON = "#4CC38A"
#: 同一支绿色的别名，给"执行/确认"这类按钮用，读代码时语义更明确。
ACCENT_GREEN = TOGGLE_ON

#: 失败/危险。与警告条的 ``ERROR``、关闭键的悬停底色是同一个红——
#: 界面里只允许存在一种"出错了"的红，新加一处就直接引这里，别再抄字面量。
DANGER = "#E5484D"

#: 警告（介于正常与出错之间）。耗材余量的"该准备了"和顶部警告条的 WARNING
#: 是同一档，共用这一支橙。
WARNING = "#E8A33D"

#: 耗材余量三档颜色。绿直接借"开关打开"那支薄荷绿——同一支绿在项目里只表达
#: "正常 / 生效"，不要为耗材再造一个绿。
CONSUMABLE_LEVEL_COLORS: dict[str, str] = {
    "ok": TOGGLE_ON,
    "warn": WARNING,
    "crit": DANGER,
}

#: 灯光亮度 / 色温滑条。米家把"暖光"直接画进控件本身，比统一用强调色更有器物感。
SLIDER_WARM = "#F3BE44"

#: 左栏（房间导航）不另存颜色常量：它从**当前背景**推一档明度得出，见
#: :func:`nav_bg`。方向随明暗模式变——浅色把栏压暗（内容区已经是浅灰，再亮
#: 一档就没有层次了），深色把栏提亮（近黑底再压就成一整片黑）。

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


def bar_bg(p: Palette) -> str:
    """标题栏 / 顶栏底板的底色。

    纯色档：往卡片色混 62%（#F1F2F4 → 白里带一点灰），这就是改造前那条浅带。

    渐变档**反过来**：往文字色混 10%，得到一条"比壁纸深一档的玻璃条"。为什么不
    照纯色档往白里混——顶栏上压着一排白色半透明胶囊（搜索框 / 状态 / 圆形按钮），
    底板也偏白的话，胶囊的落差只剩十来个色级（实测 0.42 白底带上再叠 0.42 白，
    搜索框几乎看不出来），整条顶栏会糊成一块。往下压一档之后，胶囊有 20 级以上
    的落差，顶栏和内容区之间也多出一道能看见的台阶（用户 2026-11-17 要的两件事
    一次解决：线不再突兀，顶栏自己成一层）。
    """
    if p.surface.is_gradient():
        return blend(p.bg, p.text, 0.10)
    return blend(p.bg, p.card, 0.62)


def nav_bg(p: Palette) -> str:
    """左栏底色：从当前背景的 ``bg`` 推一档。

    浅色压暗 6%（#F1F2F4 → #E3E4E5，Δ ≈ 14 个明度档），深色提亮 4.5%
    （#0E0F12 → #191A1D，Δ ≈ 11）。换任何背景预设都自动跟随，不再有第二套
    写死的侧栏色。渐变档不走这里（见 :func:`nav_fill`）。
    """
    return blend(p.bg, "#FFFFFF" if p.is_dark else "#000000", 0.045 if p.is_dark else 0.06)


def nav_row_bg(p: Palette) -> str:
    """导航选中行。比栏底色再亮一层：浅色直接用卡片白（和右边的白卡同族，
    一眼看得出"当前在这间房"），深色在栏底上提 9%，否则选中块整个看不见。"""
    return p.card if not p.is_dark else blend(nav_bg(p), p.text, 0.09)


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


def field_border(p: Palette, fill: str | None = None) -> str:
    """输入控件的描边。**不能直接拿 ``p.border``**。

    ``p.border`` 是给"卡片压在窗口底上"用的（深色 #26282E 对窗口底 #0E0F12）。
    输入框长在卡片（``panel_bg`` #28292C）上，同一个 #26282E 压上去只有 **1.01:1**
    ——描边等于没画，整片输入区融进卡片，深色下只剩一个和卡片同色的方框
    （用户 11-13 截图报的"暗色主题下输入框的描边没了"）。

    所以描边要从**输入框自己的底**起算，往文字色提一档：
    深色 #333437 → #4E4F52（1.52:1），浅色 #FAFAFA → #E0E1E1（1.26:1）。
    两套都同时具备"底色差 + 描边差"两重轮廓，浅色下也比原来那支 #EAEBEE 清楚。

    ``fill`` 给"底不是 ``field_bg``"的场景用（顶栏音箱条：在线时底是 ``p.card``、
    离线时是 ``p.bg_soft``），传进来就按它起算，别再用面板底算出一支和它无关的线。
    """
    return blend(field_bg(p) if fill is None else fill, p.text,
                 0.14 if p.is_dark else 0.11)


def field_qss(
    p: Palette,
    selector: str = "QLineEdit",
    *,
    radius: int = 8,
    padding: str = "5px 9px",
    font_size: int = 12,
    base: str | None = None,
    alpha: float | None = None,
) -> str:
    """输入框（``QLineEdit`` / 只读行编辑）唯一一份配色规则。

    以前四个窗口各写一份 ``background: panel_bg; border: 1px solid p.border``，
    深色下**每一份都是隐形的**（见 :func:`field_border`）。现在底色走
    ``field_bg``、描边走 ``field_border``、焦点走 accent，谁要改就改这里。

    ``selector`` 带上 objectName（如 ``QLineEdit#Field``）能压过窗口级通用规则；
    尺寸参数留给调用方——编辑页 5/9、日志页 7/10、搜索框是胶囊，只有颜色该统一。

    ``base`` 给"底不压在卡片上"的输入框用（顶栏搜索框压在窗口底 ``p.bg`` 上）：
    传进来就从它起算，别去用面板底——深色下 #333437 压在 #0E0F12 上会亮得像
    一块凸起。不传 = 卡片上的常规输入框，走 ``field_bg``。

    ``alpha`` 让这个框跟着背景一起透（顶栏搜索框：它直接压在渐变上，做实了就是
    右上角一块白贴纸，用户第一眼看到的就是它）。QSS 的背景会画两遍，所以走
    ``qss_alpha`` 反解；描边用同一个 alpha，否则一圈实线又把它勾出来了。
    """
    fill = (field_bg(p) if base is None
            else blend(base, p.text, 0.06 if p.is_dark else 0.05))
    return _field_rules(p, selector, fill, field_border(p, fill),
                        radius=radius, padding=padding, font_size=font_size, alpha=alpha)


def card_field_qss(
    p: Palette,
    selector: str = "QLineEdit",
    *,
    radius: int = 16,
    padding: str = "0 14px",
    font_size: int = 13,
) -> str:
    """压在背景上的**卡面**输入框（顶栏搜索框）的配色规则。

    它和快捷语音指令条（``speaker_bar.SpeakerBar``）、状态胶囊、卡片是同一种东西：
    一张半透明小卡片浮在背景上。所以底色直接取 ``pal.card``，不漏一档推导——
    从 ``pal.bg`` 混 5% 文字色出来的那支"浅灰"，在渐变上既不白也不透，读起来像
    贴了一张纸（用户 2026-11-17 连问两次「搜索设备框没有半透明」）。

    半透明一律走 ``fill_qss``/``qss_alpha``（QSS 背景画两遍）；描边跟着 ``card_alpha``
    一起透、比底色实一档，和指令条那条边是同一个配方。
    """
    fill = p.card
    return _field_rules(p, selector, fill, field_border(p, fill),
                        radius=radius, padding=padding, font_size=font_size,
                        alpha=p.card_alpha)


def _field_rules(
    p: Palette,
    selector: str,
    fill: str,
    line: str,
    *,
    radius: int,
    padding: str,
    font_size: int,
    alpha: float | None,
) -> str:
    """``field_qss`` / ``card_field_qss`` 共用的那一段 QSS 文本。

    分开只为"底色从哪来"有两套说法，画法本身必须只有一份——否则改了圆角、焦点色
    必然会漏掉一处。
    """
    if alpha is None:
        bg_css, line_css = fill, line
    else:
        # 描边比底实一档：底可以全透（"框只是稍微挡一下背景"），但边界不能跟着消失，
        # 否则输入框在深色下直接看不见（实测量过：0.5 的描边压在渐变上约 1.05:1）。
        bg_css = rgba(fill, qss_alpha(alpha))
        line_css = rgba(line, qss_alpha(min(1.0, alpha + 0.22)))
    return f"""
    {selector} {{
        background: {bg_css}; color: {p.text}; font-size: {font_size}px;
        border: 1px solid {line_css}; border-radius: {radius}px;
        padding: {padding};
        selection-background-color: {p.accent}; selection-color: #FFFFFF;
    }}
    {selector}:hover {{ border-color: {blend(fill, p.text, 0.24 if p.is_dark else 0.20)}; }}
    {selector}:focus {{ border-color: {p.accent}; }}
    {selector}:disabled {{
        background: {blend(fill, p.bg, 0.45)}; color: {p.text_mute};
    }}
    """


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
#: 「圆角矩形」按钮：40px 高、圆角 20 就是胶囊，不是用户要的矩形。
#: 电脑自动化四窗 + 确认框统一用这一档（11-13 用户定）。
RADIUS_BUTTON_SOFT = 10
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
    "GRADIENT_PRESETS",
    "LIGHT",
    "PRESETS",
    "PRESET_IDS",
    "Preset",
    "RADIUS_BUTTON",
    "RADIUS_BUTTON_SOFT",
    "RADIUS_CARD",
    "RADIUS_PILL",
    "RADIUS_SMALL",
    "RADIUS_WINDOW",
    "SLIDER_WARM",
    "SOLID_PRESETS",
    "Surface",
    "TOGGLE_ON",
    "Palette",
    "app_qss",
    "apply_to_app",
    "blend",
    "card_field_qss",
    "card_hover_color",
    "category_color",
    "chip_bg",
    "chip_text",
    "field_bg",
    "field_border",
    "field_qss",
    "hue_gap",
    "nav_bg",
    "nav_fill",
    "nav_hover_fill",
    "nav_line",
    "nav_row_bg",
    "nav_row_fill",
    "palette_for",
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
    "window_edge",
]
