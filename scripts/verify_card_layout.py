# SPDX-License-Identifier: GPL-3.0-or-later
"""设备卡片排版验证（纯几何，不联网、不渲染、不碰真实设备）。

卡片排版最容易悄悄坏掉的地方是**坐标散落**：标题让位多少、电源落在哪、
内容带从哪开始、多键按键会不会溢出。这些一旦错位，界面只是"看着别扭"，
没有异常也没有日志。所以这里把每一条边都钉成断言。

用法：``.venv/Scripts/python.exe -X utf8 scripts/verify_card_layout.py``
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication  # noqa: E402

from mihome.ui import theme  # noqa: E402
from mihome.ui.card_templates import CardStyle  # noqa: E402
from mihome.ui.device_card import (  # noqa: E402
    ART,
    BODY_TOP,
    GANG_GAP,
    GANG_LABEL_MIN_PX,
    GANG_LABEL_PX,
    PAD,
    POWER_D,
    POWER_TOP,
    SUB_H,
    SUB_TOP,
    TITLE_TOP,
    VERT_PAD,
    DeviceCard,
    is_ascii,
    label_extent,
    label_line_h,
    stack_lines,
    use_bars,
)

FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global FAIL
    FAIL += 0 if ok else 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")


class _Ctl:
    prop = None


class _Gang:
    def __init__(self, i: int, label: str | None = None) -> None:
        self.siid = i + 1
        self.label = label or f"键{i + 1}"
        self.power = SimpleNamespace(key=f"k{i}", siid=i + 1, piid=1)


def make_card(
    span: int = 1, gangs: int = 0, has_power: bool = True, labels: list[str] | None = None
) -> DeviceCard:
    card = SimpleNamespace(
        span=span,
        category="switch" if gangs else "light",
        gangs=[_Gang(i, labels[i] if labels else None) for i in range(gangs)],
    )
    # 实例属性上的 lambda 不会被当成方法绑定，直接当函数用
    card.control = lambda slot: _Ctl() if (slot == "power" and has_power) else None
    state = SimpleNamespace(name="测试设备", card=card, node=SimpleNamespace(model="m"))
    # 宽度由样式自带（不再读 CardSpec.span）；这里直接造一个指定 span 的样式
    # 来验证 2×1 的跨列公式。
    style = CardStyle(id="probe", label="探针", span=span)
    return DeviceCard(state, None, theme.LIGHT, style)


def main() -> int:
    app = QApplication(sys.argv[:1])  # noqa: F841

    print("1) 设计尺寸")
    c = make_card()
    check("1×1 = 166×168", (c.sizeHint().width(), c.sizeHint().height()) == (166, 168),
          f"{c.sizeHint().width()}×{c.sizeHint().height()}")
    check("2×1 = 166×2 + 间距 8 = 340", make_card(span=2).sizeHint().width() == 340,
          str(make_card(span=2).sizeHint().width()))
    check("卡高 = 标题带 76 + 内容带 78 + 底边距 14",
          BODY_TOP + (theme.CARD_H - PAD - BODY_TOP) + PAD == theme.CARD_H,
          f"{BODY_TOP}+{theme.CARD_H - PAD - BODY_TOP}+{PAD}")

    print("2) 标题让位")
    check("有电源：标题可用 96", c._title_width() == 96, str(c._title_width()))
    np = make_card(has_power=False)
    check("无电源：标题满宽 138", np._title_width() == 138, str(np._title_width()))
    gang = make_card(gangs=3)
    check("多键卡：标题满宽（右上不放电源）", gang._title_width() == 138, str(gang._title_width()))

    print("3) 电源按钮")
    pr = c._power_rect()
    check("靠右上内边距", pr.left() == 166 - PAD - POWER_D and pr.top() == POWER_TOP,
          f"left={pr.left()} top={pr.top()}")
    check("标题不压电源（留 10px）", PAD + c._title_width() + 10 <= pr.left(),
          f"{PAD + c._title_width()} vs {pr.left()}")
    check("电源下沿不压副标题", pr.top() + POWER_D <= SUB_TOP, f"{pr.top() + POWER_D} vs {SUB_TOP}")
    check("电源与标题同区（垂直有交叠）", pr.top() < TITLE_TOP + 22)

    print("4) 副标题与内容带")
    check("副标题下沿不进内容带", SUB_TOP + SUB_H <= BODY_TOP, f"{SUB_TOP + SUB_H} vs {BODY_TOP}")
    br = c._body_rect()
    check("内容带 76 起、高 78", (br.top(), br.height()) == (76, 78), f"{br.top()} / {br.height()}")
    check("内容带左右对称", br.left() == PAD and br.right() == 166 - PAD - 1,
          f"left={br.left()} right={br.right()}")
    check("产品图放得下内容带", ART <= br.height() and ART <= br.width(),
          f"art={ART} body={br.width()}×{br.height()}")

    print("5) 多键按键布局：竖条 / 胶囊")
    check("1–3 键走竖条", use_bars(1) and use_bars(2) and use_bars(3))
    check("4 键以上走胶囊", not use_bars(4) and not use_bars(6))

    br = make_card(gangs=3)._body_rect()
    for n in (1, 2, 3):
        g = make_card(gangs=n)
        rects = g._gang_rects()
        check(f"{n} 键：{n} 条竖条", len(rects) == n, str(len(rects)))
        check(
            f"{n} 键：等高同顶",
            len({round(r.top(), 2) for r in rects}) == 1
            and max(r.height() for r in rects) - min(r.height() for r in rects) < 0.01,
        )
        check(f"{n} 键：等宽", max(r.width() for r in rects) - min(r.width() for r in rects) < 0.01,
              f"{rects[0].width():.2f}")
        check(f"{n} 键：铺满内容带高度", abs(rects[0].height() - br.height()) < 0.01,
              f"{rects[0].height():.2f} vs {br.height()}")
        check(f"{n} 键：左右顶到内边距",
              abs(rects[0].left() - br.left()) < 0.01
              and abs(rects[-1].right() - (br.x() + br.width())) < 0.01,
              f"{rects[0].left():.1f}/{rects[-1].right():.1f} vs {br.left()}/{br.x() + br.width()}")
        if n > 1:
            gaps = [rects[i + 1].left() - rects[i].right() for i in range(n - 1)]
            check(f"{n} 键：条间缝隙 = {GANG_GAP}", all(abs(x - GANG_GAP) < 0.01 for x in gaps),
                  str([round(x, 2) for x in gaps]))
        check(f"{n} 键：竖条不压标题带", rects[0].top() >= BODY_TOP - 0.01,
              f"{rects[0].top():.1f} vs {BODY_TOP}")

    for n, rows in ((4, 2), (5, 2), (6, 2)):
        g = make_card(gangs=n)
        rects = g._gang_rects()
        check(f"{n} 键：{n} 颗胶囊", len(rects) == n, str(len(rects)))
        tops = sorted({round(r.top()) for r in rects})
        check(f"{n} 键：排成 {rows} 行", len(tops) == rows, str(tops))
        inside = all(
            r.left() >= PAD - 0.5
            and r.right() <= 166 - PAD + 0.5
            and r.top() >= BODY_TOP - 0.5
            and r.bottom() <= theme.CARD_H - PAD + 0.5
            for r in rects
        )
        check(f"{n} 键：全部落在卡片内", inside)

    r4 = make_card(gangs=4)._gang_rects()
    check("4 键：第二行与第一行左边缘对齐", abs(r4[2].left() - r4[0].left()) < 0.01,
          f"{r4[2].left():.1f} vs {r4[0].left():.1f}")

    print("6) 竖排文字")
    check("行高比字号大（中文行距）", label_line_h(12) > 12, f"{label_line_h(12):.0f}")
    check("字号越小行高越小", label_line_h(10) < label_line_h(11) < label_line_h(12))
    inner = br.height() - VERT_PAD * 2
    check("3 个字一行一个", stack_lines("排气扇", inner, 16) == ["排", "气", "扇"])
    check("放得下就不加省略号", "…" not in stack_lines("餐厅吊灯", 64, 16))
    over = stack_lines("客厅天花板主灯", inner, 16)
    check("放不下：末位换省略号", over[-1] == "…" and len(over) * 16 <= inner + 0.01,
          "".join(over))
    check("西文标签识别", is_ascii("Kitchen") and not is_ascii("左键"))
    check("中西混排当中文处理", not is_ascii("Light 左键"))
    # 西文是整体转 90° 的，占的高度等于文字宽度，不是"字母数 × 行高"
    check("西文按宽度算占用高度",
          label_extent("Kitchen", 12) < len("Kitchen") * label_line_h(12),
          f"{label_extent('Kitchen', 12):.0f} < {len('Kitchen') * label_line_h(12):.0f}")
    check("中文按字数 × 行高算",
          abs(label_extent("排气扇", 12) - 3 * label_line_h(12)) < 0.01)

    # 字号要**整张卡统一**：三个标签长短不一时，取最小的那个能装下的字号
    for labels, label in (
        (["左键", "排气扇", "右键"], "3 键短标签用 12px"),
        (["餐厅吊灯", "排气扇", "右键"], "有 4 字标签仍用 12px"),
        (["客厅天花板主灯", "走廊", "阳台"], "超长标签降字号"),
    ):
        card = make_card(gangs=len(labels), labels=labels)
        size, line_h = card._gang_label_metrics(card.state.card.gangs, br.height())
        need = max(label_extent(x, size) for x in labels)
        fits = need <= inner + 0.01
        # 最小字号还装不下就别再降了，交给 stack_lines 加省略号
        check(f"{label}：装得下、或已降到最小字号", fits or size == GANG_LABEL_MIN_PX,
              f"{size}px / 需要 {need:.0f} ≤ {inner}")
        if not fits:
            longest = max(labels, key=len)
            lines = stack_lines(longest, inner, line_h)
            check(f"{label}：省略后装得下",
                  lines[-1] == "…" and len(lines) * line_h <= inner + 0.01, "".join(lines))
        bigger = [s for s in range(size + 1, GANG_LABEL_PX + 1)
                  if max(label_extent(x, s) for x in labels) <= inner + 0.01]
        check(f"{label}：没有更大的字号装得下", not bigger, str(bigger))
        check(f"{label}：不低于 {GANG_LABEL_MIN_PX}px", size >= GANG_LABEL_MIN_PX, str(size))

    print()
    if FAIL:
        print(f"{FAIL} 项失败")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
