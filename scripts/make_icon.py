# SPDX-License-Identifier: GPL-3.0-or-later
"""生成程序图标：一台电脑显示器，屏幕里是一栋房子（"电脑中的米家"）。

跑法（全部版本 → ``scripts/out/icon_*.png``）::

    .venv/Scripts/python.exe scripts/make_icon.py            # 出全部
    .venv/Scripts/python.exe scripts/make_icon.py v2         # 只出 v2

**为什么要脚本而不是丢一张 PNG 进仓库**：图标得能复现、能改色、能按需压成多尺寸
``.ico``。手绘 PNG 换个品牌色就要重画，而且没人知道原始分层在哪。

**三档细节**（``_render`` 里的 ``detail``）——这是本文件最要紧的一条：

===========  ==============================================================
``detail``   画法
===========  ==============================================================
2（≥ 48 px）  完整版：细边框屏幕 + 颈 + 底座 + 带门的房子
1（24–47）    中等版：实心屏幕 + 放大房子 + 短颈 + 宽底座，**不画门**
0（< 24）     极小版：**只画房子 + 短颈 + 底座，不画屏幕**
===========  ==============================================================

为什么 16 px 要砍掉屏幕：细边框只剩 1.5 个物理像素，四个圆角全是灰边；"白屏里嵌
一栋对比色房子"这种嵌套在小尺寸下必然糊成一团。16 px 只认**大色块关系**——一栋
强对比的房子压在一根短柱上，就足够读成"屏幕里的家"。实测 v1 的 16 px 如果不砍，
缩出来是一坨脏色块（见 ``icon_small_strip.png`` 的原始版本）。

**别用 alpha 色去"擦除"**：PIL 画在单层位图上，填 ``(0,0,0,0)`` 得到的是**真正的
透明洞**（存成 PNG 在浅色背景上露出白底），不是"露出下层绿色"。所以门一律填实色。

**坐标写法**：几何都在 256 见方的设计稿上写（``u = big / 256``），改一处比例不用换算
第二遍。
"""

from __future__ import annotations

import io
import struct
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)
#: 落到程序里的那份（``mihome/ui/app_icon.py`` 按这个名字找）。
ASSETS = ROOT / "assets"
#: 线上使用的方案。换图标只改这一处，``--ico`` 默认就导它。
CURRENT = "v8"

#: 超采样倍数。3 就够（圆角 + 斜屋顶的抗锯齿余量），4 更稳，代价只是几十毫秒。
SS = 4

#: 米家主色（青绿）。从米家 App 图标取样的单色是 ``#18CC8E``，官方并没有做渐变。
#: 这里只围绕它做 ±7% 的"上亮下深"，渐变中点落在 ``#17C68A``——再提饱和或提亮就会
#: 从"米家绿"滑到荧光薄荷绿（试过 ``#2BE0A3``，四个方案一起刺眼）。
GREEN_HI = (34, 212, 150)
GREEN_LO = (12, 184, 126)
#: 压在**白屏**上的房子用它。白屏对主色的对比只有 2.5:1，会发飘；这一档到 4.4:1。
GREEN_DEEP = (6, 140, 96)
#: 米白底那一族里"显示器本体"的绿（v4/v5/v6 共用同一支，只换房子颜色才比得出来）。
#: 它比主色深一档不是随手取的：房子要在同一块屏上同时压得住黑和白，屏的相对亮度
#: 得落在 0.27 附近——黑对它是 6.4:1、白对它是 3.3:1，两边都过非文字图形的 3:1。
GREEN_MON = (12, 162, 108)
WHITE = (255, 255, 255)
INK = (24, 28, 36)
DEEP_HI = (32, 41, 56)
DEEP_LO = (13, 17, 23)
CREAM = (247, 248, 250)
CREAM_LO = (232, 235, 240)


def _u(big: int) -> float:
    """设计稿 256 见方 → 当前画布边长的换算因子。"""
    return big / 256.0


def _vgrad(img: Image.Image, top, bottom, radius: float) -> None:
    """在 ``img`` 上铺一条竖向渐变，按 ``radius`` 裁圆角。

    逐行画线而不是 ``Image.linear_gradient``，是因为后者只有灰度、还得再套蒙版。
    """
    w, h = img.size
    band = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        band.putpixel((0, y), tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    full = band.resize((w, h), Image.BILINEAR).convert("RGBA")
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    img.paste(full, (0, 0), mask)


def _house(d: ImageDraw.ImageDraw, box, color, *, roof: float = 0.38,
           inset: float = 0.12) -> None:
    """在 ``box`` 里画一栋房子：尖屋顶 + 房体。

    ``box`` 是 ``(left, top, right, bottom)``：峰顶在上边中点，屋檐落在
    ``top + (bottom - top) * roof``，房体从屋檐压到下边。

    ``roof`` 是屋顶占总高的比例，``inset`` 是房体相对屋顶每侧收窄的比例（即**出檐**）。
    两个都得跟着房子大小调：房子占满画面（v4）时 0.38 / 0.12 会让屋顶扁成"帐篷"、
    房体宽成一块砖，整枚图标读起来像奖杯——那版用 0.44 / 0.18，房体才回到近似正方形。

    **不画门**：256 设计稿里门还像门，到 48 px 只剩 3–4 个像素，读起来只是一个脏点，
    不如留干净的实心剪影。米家官方那枚小房子也不带门。
    """
    x0, y0, x1, y1 = box
    cx = (x0 + x1) / 2.0
    eave = y0 + (y1 - y0) * roof
    d.polygon([(cx, y0), (x1, eave), (x0, eave)], fill=color)
    d.rectangle([x0 + (x1 - x0) * inset, eave - 1,
                 x1 - (x1 - x0) * inset, y1], fill=color)


def _inner_house(d: ImageDraw.ImageDraw, box, color, *, roof: float = 0.44,
                 inset: float = 0.13) -> None:
    """在 ``box``（屏幕内框）里按固定留白放一栋房子，各版本共用同一个构图。"""
    x0, y0, x1, y1 = box
    pad_x = (x1 - x0) * 0.13
    pad_top = (y1 - y0) * 0.11
    pad_bot = (y1 - y0) * 0.07
    _house(d, (x0 + pad_x, y0 + pad_top, x1 - pad_x, y1 - pad_bot), color,
           roof=roof, inset=inset)


def _monitor(d: ImageDraw.ImageDraw, big: int, *, stroke, stroke_w: float = 13.0):
    """显示器：细边框屏幕 + 颈 + 底座。返回屏幕内框，供画房子用。

    ``stroke_w`` 是边框粗细（256 设计稿单位）。默认 13 是完整版（≥ 48 px）的口径；
    32 px 档要传 ~26，否则 13/256×32 只剩 1.6 个像素，边框糊成一片灰。
    """
    u = _u(big)
    x0, y0, x1, y1 = 40 * u, 44 * u, 216 * u, 168 * u
    w = stroke_w * u
    d.rounded_rectangle([x0, y0, x1, y1], radius=20 * u,
                        outline=stroke, width=round(w))
    d.rounded_rectangle([116 * u, y1 - 1, 140 * u, 194 * u], radius=5 * u, fill=stroke)
    d.rounded_rectangle([86 * u, 192 * u, 170 * u, 212 * u], radius=10 * u, fill=stroke)
    inset = w + 6 * u
    return x0 + inset, y0 + inset, x1 - inset, y1 - inset


def _fat_screen(d: ImageDraw.ImageDraw, big: int, *, fill, house, sharp: bool = False):
    """中等版（24–47 px）：实心屏幕 + 放大的房子 + 短颈 + 宽底座。

    ``sharp`` 把屏幕/颈/底座三个块的圆角全部归零（直角矩形试验用）。
    """
    u = _u(big)
    d.rounded_rectangle([40 * u, 44 * u, 216 * u, 168 * u], radius=0 if sharp else 20 * u, fill=fill)
    d.rounded_rectangle([112 * u, 168 * u, 144 * u, 192 * u], radius=0 if sharp else 4 * u, fill=fill)
    d.rounded_rectangle([78 * u, 190 * u, 178 * u, 212 * u], radius=0 if sharp else 10 * u, fill=fill)
    _house(d, (66 * u, 60 * u, 190 * u, 158 * u), house)


def _tiny(d: ImageDraw.ImageDraw, big: int, *, fill, sharp: bool = False):
    """极小版（< 24 px）：只画房子 + 短颈 + 底座，屏幕整块砍掉。

    房子要顶到画面上部（顶 34、底 168），16 px 下才有 8 px 以上的白块高度。
    """
    u = _u(big)
    d.rounded_rectangle([118 * u, 166 * u, 138 * u, 190 * u], radius=0 if sharp else 4 * u, fill=fill)
    d.rounded_rectangle([74 * u, 186 * u, 182 * u, 212 * u], radius=0 if sharp else 11 * u, fill=fill)
    _house(d, (34 * u, 34 * u, 222 * u, 168 * u), fill)


def _render(size: int, body) -> Image.Image:
    """按 ``size`` 选细节档位，超采样后缩回目标尺寸。"""
    big = size * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    detail = 2 if size >= 48 else (1 if size >= 24 else 0)
    body(img, ImageDraw.Draw(img), big, detail)
    return img.resize((size, size), Image.LANCZOS)


# --------------------------------------------------------------------- 版本 1
def v1(size: int) -> Image.Image:
    """描边标准款：绿底 + 白色细框显示器 + 白房子。

    最直白的一版，也是"描边"这个特征的唯一持有者——所以 32 px 档要**保留边框**
    （``stroke_w=26``），不能像其它版本那样退化成实心屏。退化了用户选的 1 号在
    任务栏里会长成 2 号的样子（踩过）。
    """
    def body(img, d, big, detail):
        _vgrad(img, GREEN_HI, GREEN_LO, 56 * _u(big))
        if detail == 0:
            _tiny(d, big, fill=WHITE)
        elif detail == 1:
            _inner_house(d, _monitor(d, big, stroke=WHITE, stroke_w=26), WHITE, roof=0.44)
        else:
            _inner_house(d, _monitor(d, big, stroke=WHITE), WHITE)
    return _render(size, body)


# --------------------------------------------------------------------- 版本 2
def v2(size: int) -> Image.Image:
    """实心扁平款：绿底 + 白色实心圆角屏 + 深绿房子。

    和 v1 的"细线描边"是两种气质：这一版色块大、圆角大，像系统扁平图标，
    32 px 下比描边版立得住（描边版那档要退化成实心屏才能看）。
    """
    def body(img, d, big, detail):
        u = _u(big)
        _vgrad(img, GREEN_HI, GREEN_LO, 56 * u)
        if detail == 0:
            _tiny(d, big, fill=WHITE)
        elif detail == 1:
            _fat_screen(d, big, fill=WHITE, house=GREEN_DEEP)
        else:
            d.rounded_rectangle([40 * u, 44 * u, 216 * u, 168 * u], radius=26 * u, fill=WHITE)
            d.rounded_rectangle([114 * u, 166 * u, 142 * u, 194 * u], radius=5 * u, fill=WHITE)
            d.rounded_rectangle([82 * u, 192 * u, 174 * u, 212 * u], radius=10 * u, fill=WHITE)
            _inner_house(d, (46 * u, 50 * u, 210 * u, 162 * u), GREEN_DEEP)
    return _render(size, body)


# --------------------------------------------------------------------- 版本 3
def v3(size: int) -> Image.Image:
    """深色款：深蓝黑底 + 亮绿显示器。

    和 App 深色主题同一套明度关系（``#202938 → #0D1117``），进深色任务栏不会比
    旁边的原生程序亮出一档。外圈要描一道暗绿，否则深底在暗任务栏里没有边界。
    """
    def body(img, d, big, detail):
        u = _u(big)
        _vgrad(img, DEEP_HI, DEEP_LO, 56 * u)
        # 外圈这道暗绿是"防融进深色任务栏"的唯一边界，必须够粗：2 单位在 32 px
        # 下只剩 0.25 个像素，和没画一样（任务栏模拟里那一版直接消失）。
        # 亮度按原暗橙那档取（约 103），比 ``DEEP_HI`` 亮约 70 级，够当边界。
        d.rounded_rectangle([2 * u, 2 * u, 254 * u, 254 * u], radius=54 * u,
                            outline=(24, 128, 94), width=round(4 * u))
        if detail == 0:
            _tiny(d, big, fill=GREEN_HI)
        elif detail == 1:
            _fat_screen(d, big, fill=GREEN_HI, house=DEEP_LO)
        else:
            _inner_house(d, _monitor(d, big, stroke=GREEN_HI), GREEN_HI)
    return _render(size, body)


# ------------------------------------------------------- 版本 4 / 5 / 6（米白底一族）
def _cream_monitor(size: int, *, screen, house, tiny) -> Image.Image:
    """米白底 + 实心屏 + 屏里一栋房子。v4 / v5 / v6 共用这**一套几何**，只换两个颜色。

    共用几何是刻意的：三个变体要比的是"谁配谁"，动了比例就比不出来了。

    ``tiny`` 单独给是因为 < 24 px 那一档只剩单色剪影（``_tiny`` 只接一个 fill），
    要挑这枚图标在小尺寸下最该被认出的那个色。
    """
    def body(img, d, big, detail):
        u = _u(big)
        _vgrad(img, CREAM, CREAM_LO, 56 * u)
        # 描边要再深一档 + 加粗一档：``(214,218,226)`` 那版在 48 px 只剩 0.4 像素，
        # 落到浅色任务栏（约 #F3F4F6）上整枚读不出圆角。这一档和底差约 40 级。
        d.rounded_rectangle([2 * u, 2 * u, 254 * u, 254 * u], radius=54 * u,
                            outline=(198, 205, 215), width=round(3 * u))
        if detail == 0:
            _tiny(d, big, fill=tiny)
        elif detail == 1:
            _fat_screen(d, big, fill=screen, house=house)
        else:
            d.rounded_rectangle([40 * u, 44 * u, 216 * u, 168 * u], radius=26 * u, fill=screen)
            d.rounded_rectangle([114 * u, 166 * u, 142 * u, 194 * u], radius=5 * u, fill=screen)
            d.rounded_rectangle([82 * u, 192 * u, 174 * u, 212 * u], radius=10 * u, fill=screen)
            _inner_house(d, (46 * u, 50 * u, 210 * u, 162 * u), house)
    return _render(size, body)


def _cream_monitor(size: int, *, screen, house, tiny,
                   sharp: bool = False) -> Image.Image:
    """米白底 + 实心屏 + 屏里一栋房子。v4 / v5 / v6 共用这**一套几何**，只换两个颜色。

    共用几何是刻意的：三个变体要比的是"谁配谁"，动了比例就比不出来了。

    ``tiny`` 单独给是因为 < 24 px 那一档只剩单色剪影（``_tiny`` 只接一个 fill），
    要挑这枚图标在小尺寸下最该被认出的那个色。

    ``sharp`` 把**显示器**的三个块（屏幕 / 颈 / 底座）的圆角归零，底板仍是 54 单位的
    圆角方块——用户 11-17 要的"构成显示器的几个块全变直角矩形"就是这个。
    """
    def body(img, d, big, detail):
        u = _u(big)
        _vgrad(img, CREAM, CREAM_LO, 56 * u)
        # 描边要再深一档 + 加粗一档：``(214,218,226)`` 那版在 48 px 只剩 0.4 像素，
        # 落到浅色任务栏（约 #F3F4F6）上整枚读不出圆角。这一档和底差约 40 级。
        d.rounded_rectangle([2 * u, 2 * u, 254 * u, 254 * u], radius=54 * u,
                            outline=(198, 205, 215), width=round(3 * u))
        if detail == 0:
            _tiny(d, big, fill=tiny, sharp=sharp)
        elif detail == 1:
            _fat_screen(d, big, fill=screen, house=house, sharp=sharp)
        else:
            d.rounded_rectangle([40 * u, 44 * u, 216 * u, 168 * u],
                                radius=0 if sharp else 26 * u, fill=screen)
            d.rounded_rectangle([114 * u, 166 * u, 142 * u, 194 * u],
                                radius=0 if sharp else 5 * u, fill=screen)
            d.rounded_rectangle([82 * u, 192 * u, 174 * u, 212 * u],
                                radius=0 if sharp else 10 * u, fill=screen)
            _inner_house(d, (46 * u, 50 * u, 210 * u, 162 * u), house)
    return _render(size, body)


def v4(size: int) -> Image.Image:
    """浅色实心款：米白底 + 深灰实心屏 + 亮绿房子。

    给浅色任务栏 / 开始菜单用。底色是米白（不是纯白），外围补一道浅灰描边——
    否则落在白色系统背景上整枚图标会"没有边界"。
    """
    return _cream_monitor(size, screen=INK, house=GREEN_HI, tiny=INK)


def v5(size: int) -> Image.Image:
    """米白底 + 绿显示器 + **黑**房子（v4 的反色版）。"""
    return _cream_monitor(size, screen=GREEN_MON, house=INK, tiny=GREEN_MON)


def v6(size: int) -> Image.Image:
    """米白底 + 绿显示器 + **白**房子。"""
    return _cream_monitor(size, screen=GREEN_MON, house=WHITE, tiny=GREEN_MON)


def v7(size: int) -> Image.Image:
    """v5 的直角版：绿显示器 + 黑房子，屏幕/颈/底座全直角矩形。"""
    return _cream_monitor(size, screen=GREEN_MON, house=INK, tiny=GREEN_MON, sharp=True)


def v8(size: int) -> Image.Image:
    """v6 的直角版：绿显示器 + 白房子，屏幕/颈/底座全直角矩形。"""
    return _cream_monitor(size, screen=GREEN_MON, house=WHITE, tiny=GREEN_MON, sharp=True)


VARIANTS = {"v1": v1, "v2": v2, "v3": v3, "v4": v4, "v5": v5, "v6": v6, "v7": v7, "v8": v8}

#: 输出尺寸。256 给预览，48/32/24/16 是任务栏与实际落地的档位。
SIZES = (256, 128, 64, 48, 32, 24, 16)


def write_ico(variant: str, path: Path, sizes=SIZES) -> None:
    """把各尺寸的 PNG 原样拼成 `.ico`。

    **不能交给 PIL 的 `save(format="ICO")`**：它会拿一张大图往下缩，而本脚本的
    16/24/32 三档是**换了几何**的（砍屏幕、加粗描边），缩下来会把这三档全部还原成
    "缩小的大图"——正是这份脚本从第一版起就在躲的那种糊法。

    格式：6 字节文件头 + 每尺寸 16 字节目录项 + 各尺寸 PNG 数据（Vista 起 ICO
    允许直接内嵌 PNG，Windows 10/11 全支持）。宽高写 256 时目录项里要填 0。
    """
    blobs: list[tuple[int, bytes]] = []
    for s in sizes:
        buf = io.BytesIO()
        VARIANTS[variant](s).save(buf, format="PNG")
        blobs.append((s, buf.getvalue()))

    n = len(blobs)
    offset = 6 + 16 * n
    entries = bytearray()
    for s, data in blobs:
        dim = 0 if s >= 256 else s
        # 宽、高、调色板数、保留位、色平面、位深、数据长度、数据偏移
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<HHH", 0, 1, n) + bytes(entries)
                     + b"".join(d for _, d in blobs))


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["--ico"]:
        variant = argv[1] if len(argv) > 1 else CURRENT
        if variant not in VARIANTS:
            print(f"没有这个版本：{variant}（可选 {list(VARIANTS)}）")
            return 1
        target = ASSETS / "app.ico"
        write_ico(variant, target)
        print(f"已保存 {target}（{variant}，{len(SIZES)} 档："
              f"{', '.join(map(str, SIZES))}）")
        return 0

    which = argv or list(VARIANTS)
    bad = [w for w in which if w not in VARIANTS]
    if bad:
        print(f"没有这个版本：{bad}（可选 {list(VARIANTS)}）")
        return 1
    for name in which:
        for size in SIZES:
            VARIANTS[name](size).save(OUT / f"icon_{name}_{size}.png")
        print(f"已保存 {name}: {', '.join(map(str, SIZES))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
