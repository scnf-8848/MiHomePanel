# SPDX-License-Identifier: GPL-3.0-or-later
"""验证「滚轮只滚页面，不改设备参数」。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_wheel.py``

覆盖三条最容易回归的路径：

1. 滚轮打在卡片 body 的滑条上 → 值不变，事件冒泡给外层 ``QScrollArea`` 继续滚；
2. 滚轮打在卡片空白 / 滚动区域上 → 页面照常滚；
3. 滑条点击后拖动 → 仍然能调值（不能把正常调节一起废掉）。

另外用原生 ``QSlider`` 做对照，确认"值不变"不是事件没送到，而是控件主动忽略。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent, QWheelEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from mihome.core.spec import PropertySpec  # noqa: E402
from mihome.core.store import DeviceState  # noqa: E402
from mihome.core.tree import DeviceNode  # noqa: E402
from mihome.ui import theme  # noqa: E402
from mihome.ui.card_controls import CardSlider, CardStepper  # noqa: E402
from mihome.ui.card_templates import LIGHT_DUAL, style_by_id  # noqa: E402
from mihome.ui.device_card import DeviceCard  # noqa: E402
from mihome.ui.wheel_guard import SafeSlider  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(f"{name} {detail}")


class Icons:
    """卡片墙只用到这两个方法，测试里给空实现即可。"""

    def ensure(self, model: str) -> None:
        pass

    def scaled(self, model: str, size: int):
        return None


def prop(siid: int, piid: int, rng=None, unit: str | None = None) -> PropertySpec:
    return PropertySpec(
        siid=siid, piid=piid, name="p", urn="", description="", comment="",
        format="uint16", access=("read", "write", "notify"), unit=unit,
        value_range=rng, value_list=(), service="", service_label="",
    )


def make_light() -> DeviceState:
    ctls = {
        "power": SimpleNamespace(slot="power", label="开关", prop=prop(2, 1),
                                 write=True, role="switch"),
        "brightness": SimpleNamespace(slot="brightness", label="亮度",
                                      prop=prop(2, 2, (1, 100, 1), "percentage"),
                                      write=True, role="slider"),
        "color_temp": SimpleNamespace(slot="color_temp", label="色温",
                                      prop=prop(2, 3, (2700, 6500, 1), "kelvin"),
                                      write=True, role="slider"),
    }
    card = SimpleNamespace(
        category="light", span=1, gangs=[], controls=list(ctls.values()),
        metrics=[], layout="light", power_actions=None, default_style="",
        control=lambda slot: ctls.get(slot),
    )
    card.bound = lambda slot: card.control(slot)
    node = DeviceNode("d1", "客厅吸顶灯", "m", "light.x", home="家", room="客厅")
    return DeviceState(
        node=node, spec=None, card=card,
        values={(2, 1): True, (2, 2): 60, (2, 3): 4000},
    )


def wheel_event(pos: QPointF, delta: int = -120) -> QWheelEvent:
    return QWheelEvent(pos, pos, QPoint(0, 0), QPoint(0, delta), Qt.NoButton,
                       Qt.NoModifier, Qt.ScrollUpdate, False)


def wheel_to(app: QApplication, target: QWidget) -> QWheelEvent:
    """直接投递给控件，用来断言它自己有没有接受事件。"""
    ev = wheel_event(QPointF(target.rect().center()))
    app.sendEvent(target, ev)
    app.processEvents()
    return ev


def wheel_to_window(app: QApplication, top: QWidget, pos_in_top: QPoint) -> None:
    """交给顶层窗口，走 Qt 真实的命中子控件 → 忽略后逐级向上传播路径。"""
    QTest.wheelEvent(top.windowHandle(), QPointF(pos_in_top), QPoint(0, -120))
    app.processEvents()


def mouse(app: QApplication, target: QWidget, kind, local: QPoint) -> None:
    ev = QMouseEvent(kind, QPointF(local), QPointF(target.mapToGlobal(local)),
                     Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    app.sendEvent(target, ev)
    app.processEvents()


def test_scroll_area(app: QApplication) -> None:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    content = QWidget()
    lay = QVBoxLayout(content)
    lay.setContentsMargins(12, 12, 12, 12)
    card = DeviceCard(make_light(), Icons(), theme.LIGHT, style_by_id(LIGHT_DUAL))
    lay.addWidget(card, 0, Qt.AlignTop)
    lay.addSpacing(1600)
    scroll.setWidget(content)
    scroll.resize(700, 420)
    scroll.show()
    app.processEvents()

    slider = card._body.controls["brightness"]
    check("卡片 body 里找到了亮度滑条", slider is not None)
    check("滑条已布局出尺寸", slider.width() > 40 and slider.height() > 0,
          f"{slider.width()}x{slider.height()}")

    bar = scroll.verticalScrollBar()

    # 1) 滚轮直接发给滑条：值不变、事件不被接受
    before = slider.value()
    ev = wheel_to(app, slider)
    check("发给滑块：值不变", slider.value() == before, str(slider.value()))
    check("发给滑块：事件被 ignore", not ev.isAccepted())

    # 1a) 真实窗口路径滚轮打在滑条上：值不变，页面照常滚
    bar.setValue(0)
    wheel_to_window(app, scroll, slider.mapTo(scroll, slider.rect().center()))
    check("发给滑块：页面仍能滚", bar.value() > 0, str(bar.value()))
    check("发给滑块：滚完值仍是原值", slider.value() == before, str(slider.value()))

    # 2) 滚轮打在卡片标题区（滑条以外）：值不变、页面能滚
    bar.setValue(0)
    wheel_to_window(app, scroll, card.mapTo(scroll, QPoint(card.width() // 2, 18)))
    check("发给卡片：滑条值不变", slider.value() == before, str(slider.value()))
    check("发给卡片：页面滚动了", bar.value() > 0, str(bar.value()))

    # 3) 滚轮打在滚动区域空白处：页面能滚
    bar.setValue(0)
    vp = scroll.viewport()
    wheel_to_window(app, scroll, vp.mapTo(scroll, QPoint(20, vp.height() - 20)))
    check("发给滚动区域：页面滚动了", bar.value() > 0, str(bar.value()))

    # 4) 点住滑条拖动：仍然能调值、松手仍然提交
    bar.setValue(0)
    committed: list[int] = []
    slider.committed.connect(committed.append)
    start = QPoint(56, slider.height() // 2)
    end = QPoint(max(60, slider.width() - 56), slider.height() // 2)
    mouse(app, slider, QEvent.MouseButtonPress, start)
    mouse(app, slider, QEvent.MouseMove, end)
    mouse(app, slider, QEvent.MouseButtonRelease, end)
    check("点击后拖动仍能调值", slider.value() != before, str(slider.value()))
    check("拖动松手仍会提交", bool(committed), str(committed))
    slider.committed.disconnect()
    scroll.close()


def test_controls(app: QApplication) -> None:
    # 原生 QSlider 做对照：确认事件对象本身能改值
    native = QSlider(Qt.Horizontal)
    native.setRange(0, 100)
    native.setValue(50)
    native.show()
    app.processEvents()
    wheel_to(app, native)
    check("对照：原生 QSlider 会被滚轮改值", native.value() != 50, str(native.value()))
    native.close()

    # 步进器（空调温度）：滚轮不改值，点击仍能加减
    stepper = CardStepper(prop(2, 4, (16, 30, 1)), theme.LIGHT, "#F3BE44", label="温度")
    stepper.resize(220, 36)
    stepper.show()
    app.processEvents()
    before = stepper.value()
    ev = wheel_to(app, stepper)
    check("步进器：滚轮不改值", stepper.value() == before, str(stepper.value()))
    check("步进器：事件被 ignore", not ev.isAccepted())
    plus = QPoint(stepper.width() - 18, stepper.height() // 2)
    mouse(app, stepper, QEvent.MouseButtonPress, plus)
    mouse(app, stepper, QEvent.MouseButtonRelease, plus)
    check("步进器：点击加号仍能加", stepper.value() == before + 1, str(stepper.value()))
    stepper.close()

    # 原生 QSlider 对照组（详情页已改用自绘 CardSlider，SafeSlider 保留作
    # 「必须用原生控件时」的兜底参考）：滚轮不改值，点击仍能调值
    safe = SafeSlider(Qt.Horizontal)
    safe.setRange(0, 100)
    safe.setValue(40)
    safe.resize(240, 24)
    safe.show()
    app.processEvents()
    ev = wheel_to(app, safe)
    check("SafeSlider：滚轮不改值", safe.value() == 40, str(safe.value()))
    check("SafeSlider：事件被 ignore", not ev.isAccepted())
    mouse(app, safe, QEvent.MouseButtonPress, QPoint(20, safe.height() // 2))
    mouse(app, safe, QEvent.MouseButtonRelease, QPoint(20, safe.height() // 2))
    check("SafeSlider：点击仍能调值", safe.value() < 40, str(safe.value()))
    safe.close()


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    test_scroll_area(app)
    test_controls(app)
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
