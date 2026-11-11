# SPDX-License-Identifier: GPL-3.0-or-later
"""验证"点卡片上的控件不会穿透成打开详情页"。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_card_click.py``

为什么单独一个脚本：这是个**纯事件路由**的问题，`verify_card_controls.py`
测的是控件自己的值与信号，测不到"事件从控件冒泡到卡片"这一段。

回归的那条 bug（2026-11-12 用户报的"点 body 里的控件，结果打开了详情页"）：
`DeviceCard.mouseReleaseEvent` 只要"按下、松开都在卡片内"就 ``clicked.emit``，
而 Qt 会把**没被 accept 的 press 沿父链传上去**。于是控件里任何一个"没命中就
return"的早退（点轨道空白、点标签、禁用态）都会让松手冒泡到卡片，值没写成、
详情页倒开了。

两道闸，这里各测一遍：
1. ``CardControl.mousePressEvent`` 把落在控件矩形内的左键一律吞掉；
2. ``DeviceCard._control_at`` 兜底：万一控件漏了，卡片也不把这点当"点卡片"。

最后一节用 monkeypatch 把第 1 道闸拆掉，确认脚本**真能**抓到这条 bug——不然
它只是个永远绿的摆设。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from mihome.core.catalog import build_card  # noqa: E402
from mihome.core.spec import parse_raw  # noqa: E402
from mihome.core.store import DeviceState  # noqa: E402
from mihome.core.tree import DeviceNode  # noqa: E402
from mihome.ui import card_templates as ct  # noqa: E402
from mihome.ui import theme  # noqa: E402
from mihome.ui.card_controls import (  # noqa: E402
    CardControl,
    CardSeg,
    CardSlider,
    CardToggle,
)
from mihome.ui.device_card import DeviceCard  # noqa: E402

FAILS: list[str] = []


def check(what: str, got, want) -> None:
    if got != want:
        FAILS.append(f"{what}：期望 {want!r}，实际 {got!r}")
        print(f"  FAIL {what}：期望 {want!r}，实际 {got!r}")
    else:
        print(f"  ok   {what} = {got!r}")


def check_true(what: str, ok: bool, detail: str = "") -> None:
    check(what + (f"（{detail}）" if detail and not ok else ""), bool(ok), True)


# ---------------------------------------------------------------------------
# 夹具：一台风扇的 2×1 卡（四个控件，两种对齐，覆盖三套事件路径）
# ---------------------------------------------------------------------------

FAN_RAW = {
    "type": "urn:miot-spec-v2:device:fan:0000A005:dmaker-p5c:1",
    "description": "风扇",
    "services": [
        {
            "iid": 2,
            "type": "urn:miot-spec-v2:service:fan:0000780A:dmaker-p5c:1",
            "description": "风扇",
            "properties": [
                {"iid": 1, "type": "urn:miot-spec-v2:property:on:00000006:dmaker-p5c:1",
                 "description": "开关", "format": "bool", "access": ["read", "write", "notify"]},
                {"iid": 2, "type": "urn:miot-spec-v2:property:fan-level:00000016:dmaker-p5c:1",
                 "description": "风速档位", "format": "uint8", "access": ["read", "write", "notify"],
                 "value-list": [
                     {"value": 1, "description": "1挡"},
                     {"value": 2, "description": "2挡"},
                     {"value": 3, "description": "3挡"},
                     {"value": 4, "description": "4挡"},
                 ]},
                {"iid": 3, "type": "urn:miot-spec-v2:property:mode:00000008:dmaker-p5c:1",
                 "description": "模式", "format": "uint8", "access": ["read", "write", "notify"],
                 "value-list": [
                     {"value": 0, "description": "直吹风"},
                     {"value": 1, "description": "自然风"},
                 ]},
                {"iid": 4, "type": "urn:miot-spec-v2:property:horizontal-swing:0000000A:dmaker-p5c:1",
                 "description": "左右扫风", "format": "bool", "access": ["read", "write", "notify"]},
                {"iid": 5,
                 "type": "urn:miot-spec-v2:property:horizontal-swing-included-angle:0000000B:dmaker-p5c:1",
                 "description": "左右扫风角度", "format": "uint8",
                 "access": ["read", "write", "notify"],
                 "value-list": [
                     {"value": 30, "description": "30°"},
                     {"value": 60, "description": "60°"},
                     {"value": 90, "description": "90°"},
                     {"value": 120, "description": "120°"},
                     {"value": 140, "description": "140°"},
                 ]},
            ],
        },
        {
            "iid": 8,
            "type": "urn:miot-spec-v2:service:dm-service:00007FFF:dmaker-p5c:1",
            "description": "自定义服务",
            "properties": [
                {"iid": 1, "type": "urn:miot-spec-v2:property:speed-level:00000040:dmaker-p5c:1",
                 "description": "风速", "format": "uint8", "access": ["read", "write", "notify"],
                 "value-range": [1, 100, 1]},
            ],
        },
    ],
}


class StubIcons:
    """产品图异步抓取跟这里无关，给个空实现就行。"""

    def ensure(self, model: str) -> None:
        return None

    def scaled(self, model: str, size: int) -> None:
        return None


def make_card(values: dict) -> DeviceCard:
    spec = parse_raw(FAN_RAW)
    card = build_card(spec, did="f1", model="dmaker.fan.p5c")
    node = DeviceNode(
        did="f1", name="风扇", model="dmaker.fan.p5c",
        spec_type=FAN_RAW["type"], home="家", room="1103-强",
    )
    state = DeviceState(node=node, spec=spec, card=card, values=values)
    w = DeviceCard(state, StubIcons(), theme.DARK, ct.style_by_id(ct.FAN_CONTROLS))
    w.resize(340, 200)
    w.show()
    QApplication.processEvents()
    w.refresh(state)
    QApplication.processEvents()
    return w


def click(card: QWidget, widget: QWidget | None, local: QPoint) -> None:
    """在 ``card`` 上点 ``widget`` 里的一点（转成卡片坐标后注入真实鼠标事件）。

    用 ``QTest.mouseClick(card, ...)`` 而不是直接发给控件：直接发就绕过了
    hit-test 与冒泡，那正是这条 bug 所在的环节。
    """
    app = QApplication.instance()
    pos = local if widget is None else widget.mapTo(card, local)
    QTest.mouseClick(card, Qt.LeftButton, Qt.NoModifier, pos)
    app.processEvents()


# ---------------------------------------------------------------------------
# 1. 控件矩形内的左键一律被吞掉
# ---------------------------------------------------------------------------


def test_events_swallowed(card: DeviceCard) -> None:
    print("\n1) 控件把落在自己身上的左键吞掉（不再冒泡到卡片）")
    body = card._body  # noqa: SLF001
    cases: list[tuple[str, QWidget, QPoint]] = []

    # 开关的**标签区**（左边一长条）：点它不该改值，但更不该开详情页
    toggle = body.controls.get("swing_h")
    if isinstance(toggle, CardToggle):
        cases.append(("开关的标签区", toggle, QPoint(4, toggle.height() // 2)))
        cases.append(("开关的滑块本体", toggle.switch,
                      QPoint(toggle.switch.width() // 2, toggle.switch.height() // 2)))

    # 轨道的**段间空隙**：`_hit` 命中不了任何一段
    for slot in ("mode", "swing_angle"):
        seg = body.controls.get(slot)
        if isinstance(seg, CardSeg):
            rects = seg._seg_rects()  # noqa: SLF001
            gap_x = rects[0][0].right() + 1
            cases.append((f"轨道 {slot} 两段之间的缝", seg,
                          QPoint(gap_x, seg.height() // 2)))
            cases.append((f"轨道 {slot} 最左边的内边距", seg,
                          QPoint(0, seg.height() // 2)))
            break

    slider = body.controls.get("fan_speed")
    if isinstance(slider, CardSlider):
        cases.append(("滑条的整行（拖动起点）", slider, QPoint(6, slider.height() // 2)))

    if not cases:
        FAILS.append("夹具没建出预期的控件，这条测试等于没跑")
        print("  FAIL 夹具没建出预期的控件")
        return

    for name, widget, point in cases:
        before = widget.isEnabled()
        click(card, widget, point)
        check(f"{name}：点完仍在卡片上（没开详情页）", getattr(card, "opened", 0), 0)
        check(f"{name}：控件状态没被弄坏", widget.isEnabled(), before)


# ---------------------------------------------------------------------------
# 2. 该打开详情的地方照旧打开
# ---------------------------------------------------------------------------


def test_card_still_clickable(card: DeviceCard) -> None:
    print("\n2) 点卡片空白处照旧开详情页")
    click(card, None, QPoint(160, 20))  # 标题带
    check("标题带点一下 = 开详情", getattr(card, "opened", 0), 1)
    card.opened = 0  # type: ignore[attr-defined]

    body = card._body  # noqa: SLF001
    click(card, body, QPoint(4, 4))  # 内容带的空白（不是控件）
    check("内容带空白点一下 = 开详情", getattr(card, "opened", 0), 1)


# ---------------------------------------------------------------------------
# 3. 第二道闸：控件漏了 accept 也不开详情
# ---------------------------------------------------------------------------


def test_fallback_gate(card: DeviceCard) -> None:
    print("\n3) 兜底层：把第一道闸拆掉，卡片仍不把控件上的点击当「点卡片」")
    body = card._body  # noqa: SLF001
    toggle = body.controls.get("swing_h")
    if not isinstance(toggle, CardToggle):
        FAILS.append("没有开关控件，第 3 节没法测")
        return

    saved = (CardControl.mousePressEvent, CardControl.mouseReleaseEvent)
    # 换回 QWidget 的默认实现：press/release 都不 accept（正是修之前的样子）
    CardControl.mousePressEvent = QWidget.mousePressEvent  # type: ignore[assignment]
    CardControl.mouseReleaseEvent = QWidget.mouseReleaseEvent  # type: ignore[assignment]
    try:
        card.opened = 0  # type: ignore[attr-defined]
        before = toggle.value()
        click(card, toggle, QPoint(4, toggle.height() // 2))
        check("控件不吞事件时，卡片自己兜住（没开详情页）",
              getattr(card, "opened", 0), 0)
        check("兜住之后也不误发写请求（标签区不该 toggle）", toggle.value(), before)
    finally:
        CardControl.mousePressEvent, CardControl.mouseReleaseEvent = saved


# ---------------------------------------------------------------------------
# 3b. 两道闸都拆掉，确认这条 bug 是真的
# ---------------------------------------------------------------------------


def test_bug_is_real(card: DeviceCard) -> None:
    print("\n3b) 两道闸都拆掉时确实会误开详情页（证明这个脚本抓得住回归）")
    body = card._body  # noqa: SLF001
    toggle = body.controls.get("swing_h")
    if not isinstance(toggle, CardToggle):
        FAILS.append("没有开关控件，第 3b 节没法测")
        return

    saved = (CardControl.mousePressEvent, CardControl.mouseReleaseEvent)
    saved_gate = DeviceCard._control_at
    CardControl.mousePressEvent = QWidget.mousePressEvent  # type: ignore[assignment]
    CardControl.mouseReleaseEvent = QWidget.mouseReleaseEvent  # type: ignore[assignment]
    DeviceCard._control_at = lambda self, pos: None  # type: ignore[assignment]
    try:
        card.opened = 0  # type: ignore[attr-defined]
        click(card, toggle, QPoint(4, toggle.height() // 2))
        check("没有闸时：点控件标签区 = 误开详情页", getattr(card, "opened", 0), 1)
    finally:
        CardControl.mousePressEvent, CardControl.mouseReleaseEvent = saved
        DeviceCard._control_at = saved_gate  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 4. `_control_at` 的边界
# ---------------------------------------------------------------------------

def test_control_at(card: DeviceCard) -> None:
    print("\n4) `_control_at` 认得出控件、认不出空白")
    body = card._body  # noqa: SLF001
    toggle = body.controls.get("swing_h")
    if isinstance(toggle, CardToggle):
        check("开关本体", card._control_at(toggle.mapTo(card, QPoint(2, 2))), toggle)
        # 开关里的滑块是它的子控件：childAt 给的是滑块，得沿父链找到 CardToggle
        inner = toggle.switch.mapTo(card, QPoint(2, 2))
        check("开关里的滑块（子控件）", card._control_at(inner), toggle)
    check("标题带空白", card._control_at(QPoint(160, 20)), None)
    check("内容带空白", card._control_at(body.mapTo(card, QPoint(2, 2))), None)


# ---------------------------------------------------------------------------


def main() -> int:
    QApplication(sys.argv)
    card = make_card({(2, 1): True, (2, 3): 0, (8, 1): 45, (2, 4): True, (2, 5): 60})
    card.opened = 0  # type: ignore[attr-defined]
    card.clicked.connect(lambda _did: setattr(card, "opened", card.opened + 1))

    test_events_swallowed(card)
    test_card_still_clickable(card)
    test_fallback_gate(card)
    test_bug_is_real(card)
    test_control_at(card)

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败：")
        for line in FAILS:
            print(" -", line)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
