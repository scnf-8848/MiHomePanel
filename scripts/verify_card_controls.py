# SPDX-License-Identifier: GPL-3.0-or-later
"""验证卡片内联控件：滑条 / 步进器 / 胶囊组 / 开关行。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_card_controls.py``

除了尺寸与信号，这里重点回归三条**会亲手感崩掉**的行为：

1. 拖动中不接外部赋值——否则设备上报晚到，滑块往回跳一格；
2. ``committed`` 只在松手时发一次、拖动中只发 ``previewed``——否则云端请求被拖满；
3. 置灰/禁用后不再发命令——离线设备不该还能点。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QFont, QFontDatabase, QMouseEvent, QWheelEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QVBoxLayout  # noqa: E402

from mihome.core.spec import PropertySpec, ValueOption  # noqa: E402
from mihome.ui import theme  # noqa: E402
from mihome.ui.card_controls import (  # noqa: E402
    CardChips,
    CardSlider,
    CardStepper,
    CardToggle,
    kelvin_color,
)

FAILS: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {got!r}")
    if not ok:
        FAILS.append(f"{name} → 期望 {want!r}，实际 {got!r}")


def check_true(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(f"{name}{('  ' + detail) if detail else ''}")


def prop(
    *,
    siid: int = 2,
    piid: int = 2,
    unit: str | None = None,
    rng: tuple[float, float, float] | None = None,
    opts: tuple[ValueOption, ...] = (),
    comment: str = "",
    fmt: str = "uint8",
) -> PropertySpec:
    return PropertySpec(
        siid=siid,
        piid=piid,
        name="p",
        urn="",
        description="",
        comment=comment,
        format=fmt,
        access=("read", "write", "notify"),
        unit=unit,
        value_range=rng,
        value_list=opts,
        service="",
        service_label="",
    )


def mouse(widget, kind, x: float, y: float, *, buttons: Qt.MouseButton = Qt.LeftButton) -> None:
    ev = QMouseEvent(
        kind,
        QPointF(x, y),
        QPointF(x, y),
        Qt.LeftButton,
        buttons,
        Qt.NoModifier,
    )
    QApplication.sendEvent(widget, ev)


def drag(widget, x_from: float, x_to: float, y: float | None = None) -> None:
    y = widget.height() / 2 if y is None else y
    mouse(widget, QMouseEvent.Type.MouseButtonPress, x_from, y)
    mouse(widget, QMouseEvent.Type.MouseMove, (x_from + x_to) / 2, y)
    mouse(widget, QMouseEvent.Type.MouseMove, x_to, y)
    mouse(widget, QMouseEvent.Type.MouseButtonRelease, x_to, y, buttons=Qt.NoButton)


BRIGHT = prop(siid=2, piid=2, unit="percentage", rng=(1, 100, 1), comment="亮度")
CCT = prop(siid=2, piid=7, unit="kelvin", rng=(2700, 6500, 1), comment="色温")
TEMP = prop(siid=2, piid=2, unit="celsius", rng=(16, 30, 1), comment="目标温度")
MODE = prop(
    siid=2,
    piid=3,
    comment="模式",
    opts=(
        ValueOption(0, "自动", "auto"),
        ValueOption(1, "制冷", "cool"),
        ValueOption(2, "制热", "heat"),
        ValueOption(3, "除湿", "dry"),
        ValueOption(4, "送风", "fan"),
    ),
)
SWING = prop(siid=2, piid=5, fmt="bool", comment="摇头")


def test_slider(app: QApplication) -> None:
    w = CardSlider(BRIGHT, theme.LIGHT, theme.SLIDER_WARM)
    w.resize(300, w.height())
    w.show()
    app.processEvents()
    check("滑条高度", w.height(), 36)
    check("滑条初始值取区间下限", w.value(), 1.0)

    got_preview: list[float] = []
    got_commit: list[int] = []
    w.previewed.connect(got_preview.append)
    w.committed.connect(got_commit.append)

    x0, x1 = w._span()
    drag(w, x0, x1)
    check("拖到最右提交满量程", got_commit[-1], 100)
    check("拖动中不发 committed", len(got_commit), 1)
    check_true("拖动中发了 previewed", len(got_preview) >= 2, f"{len(got_preview)} 次")

    # 拖动中设备上报不该把滑块拽回去
    mouse(w, QMouseEvent.Type.MouseButtonPress, x1, w.height() / 2)
    w.set_value(5)
    check_true("拖动中忽略外部赋值", w.value() > 90, f"value={w.value():g}")
    mouse(w, QMouseEvent.Type.MouseButtonRelease, x1, w.height() / 2, buttons=Qt.NoButton)

    w.set_value(40)
    check("外部赋值生效", int(w.value()), 40)

    ev = QWheelEvent(
        QPointF(150, 18),
        QPointF(150, 18),
        QPoint(0, 0),
        QPoint(0, 120),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.ScrollUpdate,
        False,
    )
    QApplication.sendEvent(w, ev)
    check("滚轮上滚 +1 步", int(w.value()), 41)

    w.setEnabled(False)
    before = int(w.value())
    drag(w, x0, x1)
    check("禁用后拖不动", int(w.value()), before)
    w.setEnabled(True)

    # 渐变模式（色温）在两端都不能崩
    g = CardSlider(CCT, theme.LIGHT, theme.SLIDER_WARM, gradient=True)
    g.resize(300, g.height())
    g.show()
    for v in (2700, 6500):
        g.set_value(v)
        g.grab()
    check("色温滑条区间", (int(g._lo), int(g._hi)), (2700, 6500))
    g.hide()
    w.hide()


def test_stepper(app: QApplication) -> None:
    w = CardStepper(TEMP, theme.LIGHT, "#39A0F5")
    w.resize(300, w.height())
    w.show()
    app.processEvents()
    w.set_value(26)
    got: list[int] = []
    w.committed.connect(got.append)

    minus, plus = w._buttons()
    QTest.mouseClick(w, Qt.LeftButton, pos=plus.center())
    check("加号 +1", got[-1], 27)
    QTest.mouseClick(w, Qt.LeftButton, pos=minus.center())
    check("减号 -1", got[-1], 26)

    w.set_value(30)
    n = len(got)
    QTest.mouseClick(w, Qt.LeftButton, pos=plus.center())
    check("到上限不再发命令", len(got), n)

    w.set_value(16)
    QTest.mouseClick(w, Qt.LeftButton, pos=minus.center())
    check("到下限不再发命令", len(got), n)

    check("步进器高度", w.height(), 36)
    w.hide()


def test_chips(app: QApplication) -> None:
    w = CardChips(MODE, theme.LIGHT, "#39A0F5")
    w.resize(312, w.height())
    w.show()
    app.processEvents()
    got: list[int] = []
    w.committed.connect(got.append)

    chips = w._layout()
    check("五个模式全部排得下", len(chips), 5)
    check("胶囊高度", w.height(), 26)

    QTest.mouseClick(w, Qt.LeftButton, pos=chips[2][0].center())
    check("点第三项提交值 2", got[-1], 2)
    check("选中态跟着变", w.value(), 2)

    narrow = CardChips(MODE, theme.LIGHT, "#39A0F5")
    narrow.resize(150, narrow.height())
    narrow.show()
    app.processEvents()
    rows = narrow._layout()
    overflow = False
    for rect, _, _ in rows:
        if rect.right() > narrow.width():
            overflow = True
    check_true("窄宽度下不溢出", not overflow, f"{len(rows)} 项 / 宽 {narrow.width()}")
    check_true("窄宽度下丢弃多余项", len(rows) <= 5)

    empty = CardChips(prop(comment="无枚举"), theme.LIGHT, "#39A0F5")
    check_true("枚举为空时自动隐藏", empty.isHidden())
    w.hide()
    narrow.hide()


def test_toggle(app: QApplication) -> None:
    w = CardToggle(SWING, theme.LIGHT, theme.TOGGLE_ON)
    w.resize(300, w.height())
    w.show()
    app.processEvents()
    got: list[bool] = []
    w.committed.connect(got.append)
    QTest.mouseClick(w.switch, Qt.LeftButton)
    check("点开关发 True", got, [True])
    QTest.mouseClick(w.switch, Qt.LeftButton)
    check("再点发 False", got, [True, False])
    check("开关右对齐", w.switch.geometry().right(), w.width() - 1)
    w.hide()


def render(app: QApplication, palette: theme.Palette, path: Path) -> None:
    sheet = QFrame()
    sheet.setObjectName("sheet")
    sheet.setStyleSheet(f"QFrame#sheet{{background:{palette.bg};}}")
    col = QVBoxLayout(sheet)
    col.setContentsMargins(16, 14, 16, 16)
    col.setSpacing(8)

    def title(text: str) -> None:
        lab = QLabel(text)
        lab.setStyleSheet(f"color:{palette.text_mute}; font-size:11px;")
        col.addWidget(lab)
        col.addSpacing(2)

    title("滑条 · 亮度")
    bright = CardSlider(BRIGHT, palette, theme.SLIDER_WARM)
    bright.set_value(78)
    col.addWidget(bright)
    title("滑条 · 色温（渐变轨道）")
    cct = CardSlider(CCT, palette, theme.SLIDER_WARM, gradient=True)
    cct.set_value(4200)
    col.addWidget(cct)
    title("步进器 · 空调温度")
    temp = CardStepper(TEMP, palette, "#39A0F5")
    temp.set_value(26)
    col.addWidget(temp)
    title("胶囊 · 空调模式")
    mode = CardChips(MODE, palette, "#39A0F5")
    mode.set_value(1)
    col.addWidget(mode)
    title("开关行 · 风扇摇头")
    swing = CardToggle(SWING, palette, "#2FBFAF")
    swing.set_value(True)
    col.addWidget(swing)

    sheet.resize(340, sheet.sizeHint().height())
    sheet.show()
    app.processEvents()
    sheet.grab().save(str(path))
    sheet.close()


def main() -> None:
    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    # offscreen 平台在这台机器上字体库是空的（families() 返回 0），不手动挂一个
    # 中文字体，截图里所有文字都会是方块。真机运行时不存在这个问题。
    if not QFontDatabase.families():
        for face in (
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/msyhl.ttc",
            "C:/Windows/Fonts/simhei.ttf",
        ):
            if Path(face).exists():
                QFontDatabase.addApplicationFont(face)
                break
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(13)
    app.setFont(font)
    out = Path(__file__).resolve().parent / "out"
    out.mkdir(exist_ok=True)

    test_slider(app)
    test_stepper(app)
    test_chips(app)
    test_toggle(app)

    # 配色合理性：色温两端必须"左暖右冷"
    warm, cool = kelvin_color(2700), kelvin_color(6500)
    check_true("2700K 偏暖", warm.red() > warm.blue(), f"{warm.name()}")
    check_true("6500K 偏冷", cool.blue() > cool.red(), f"{cool.name()}")

    render(app, theme.LIGHT, out / "card_controls_light.png")
    render(app, theme.DARK, out / "card_controls_dark.png")

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败：")
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
