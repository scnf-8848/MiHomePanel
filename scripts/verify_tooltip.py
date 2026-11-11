# SPDX-License-Identifier: GPL-3.0-or-later
"""验证自绘悬停提示（``ui/tooltip.py``）的三件事：节奏、独立、圆角。

为什么不用 ``QToolTip``：它的计时是**全局**的（第一次等 700ms，之后 20 秒内换个
控件秒出），而且窗口底色由平台画，``border-radius`` 外面还有一块方底。这两条
都在这里用断言钉住，免得以后有人图省事改回 ``setToolTip``。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_tooltip.py``

截图（``scripts/out/tooltip.png``）由 ``shot_ui.py`` 出：离屏平台的
``QFontDatabase`` 是空的，中文会渲染成豆腐块，得用真实平台截图。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, QRect, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor, QFont, QFontMetrics, QGuiApplication, QMouseEvent,
)
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from mihome.ui import theme, tooltip  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILS.append(f"{name} {detail}")


def make_widget(x: int, y: int, w: int = 120, h: int = 80) -> QWidget:
    widget = QWidget()
    widget.move(x, y)
    widget.resize(w, h)
    widget.show()
    return widget


def send_move(widget: QWidget, local: QPoint) -> None:
    """发一个真的 QMouseEvent——卡片自己的 mouseMoveEvent 要读 position()。"""
    where = QPointF(local)
    QApplication.sendEvent(
        widget,
        QMouseEvent(QEvent.MouseMove, where, where,
                    Qt.NoButton, Qt.NoButton, Qt.NoModifier),
    )


def main() -> int:
    app = QApplication(sys.argv[:1])
    # 离屏平台默认字体没有中文，不设这一句对照图里全是豆腐块
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(13)
    app.setFont(font)
    mgr = tooltip.manager()
    mgr.set_palette(theme.LIGHT)

    left = make_widget(0, 0)
    right = make_widget(300, 0)
    QApplication.processEvents()

    # 光标位置在离屏平台不可靠，注入一个固定点来驱动"离没离开控件"的判断
    cursor = {"pos": QPoint(20, 20)}
    mgr._cursor_pos = lambda: cursor["pos"]

    tooltip.watch(left, lambda: "小米米家智能开关")
    tooltip.watch(right, lambda: "客厅吸顶灯")

    # -- 1. 延迟 ----------------------------------------------------------
    print("\n1) 悬停延迟")
    check("延迟明显短于 Qt 默认的 700ms", 0 < tooltip.DELAY <= 450, f"{tooltip.DELAY}ms")
    QApplication.sendEvent(left, QEvent(QEvent.Enter))
    QTest.qWait(tooltip.DELAY - 140)
    check(f"不到 {tooltip.DELAY}ms 不弹", not mgr._bubble.isVisible())
    QTest.qWait(240)
    check(f"过了 {tooltip.DELAY}ms 弹出来", mgr._bubble.isVisible())

    # -- 2. 每台设备独立 --------------------------------------------------
    print("\n2) 换控件要重新计时（原生 tooltip 会秒出）")
    cursor["pos"] = QPoint(320, 20)
    QApplication.sendEvent(left, QEvent(QEvent.Leave))
    QApplication.processEvents()
    check("离开就收起", not mgr._bubble.isVisible())

    QApplication.sendEvent(right, QEvent(QEvent.Enter))
    QTest.qWait(tooltip.DELAY - 160)
    check("刚进新控件时还没到时间", not mgr._bubble.isVisible(),
          f"等了 {tooltip.DELAY - 160}ms")
    QTest.qWait(260)
    check("新控件自己等满才弹", mgr._bubble.isVisible())

    # -- 3. 卡片内部的子控件不该把它晃没了 --------------------------------
    print("\n3) 卡片内部移动不闪")
    cursor["pos"] = QPoint(20, 20)
    QApplication.sendEvent(right, QEvent(QEvent.Leave))
    QApplication.processEvents()
    QApplication.sendEvent(left, QEvent(QEvent.Enter))
    QTest.qWait(tooltip.DELAY + 120)
    check("先弹出来", mgr._bubble.isVisible())

    # 鼠标移到卡片里的电源键上时，卡片自己会收到 Leave，但光标还在卡片矩形内
    QApplication.sendEvent(left, QEvent(QEvent.Leave))
    QApplication.processEvents()
    check("光标仍在卡片内时不收起", mgr._bubble.isVisible())
    check("target 也没被清掉", mgr._target is left)

    # 从子控件移回来会再收到一次 Enter，不能因此重新计时
    QApplication.sendEvent(left, QEvent(QEvent.Enter))
    check("移回卡片不重置计时（计时器仍停着）", not mgr._timer.isActive())

    # -- 4. 圆角 ----------------------------------------------------------
    print("\n4) 圆角（原生 tooltip 的方底问题）")
    mgr._bubble.show_for("客厅灯", QPoint(100, 100))
    QApplication.processEvents()
    img = mgr._bubble.grab().toImage()
    check("抓得到气泡", img.width() > 40 and img.height() > 10,
          f"{img.width()}x{img.height()}")
    corners = [(0, 0), (img.width() - 1, 0), (0, img.height() - 1),
               (img.width() - 1, img.height() - 1)]
    alphas = [img.pixelColor(x, y).alpha() for x, y in corners]
    check("四角是透明的（真圆角，不是画上去的图案）", all(a == 0 for a in alphas),
          str(alphas))
    center = img.pixelColor(img.width() // 2, img.height() // 2)
    check("中心是不透明的卡片色",
          center.alpha() >= 250 and center.lightness() == QColor(theme.LIGHT.card).lightness(),
          f"{center.name()}")
    edge = img.pixelColor(img.width() // 2, 1)
    check("上边沿也画到了（不是只有中间一块）", edge.alpha() >= 200,
          f"alpha={edge.alpha()}")

    # -- 5. 折行 ----------------------------------------------------------
    print("\n5) 长名字折行")
    mgr._bubble.show_for("米家智能直流变频落地扇 1X 升级版 支持自然风与睡眠模式", QPoint(100, 100))
    QApplication.processEvents()
    check("宽度不超过上限", mgr._bubble.width() <= tooltip.MAX_W + tooltip.PAD_X * 2 + 4,
          f"{mgr._bubble.width()}px")
    lines = len(mgr._bubble._lines)
    check("长文本折成多行", lines >= 2, f"{lines} 行")
    tall = mgr._bubble.height()

    mgr._bubble.show_for("客厅灯", QPoint(100, 100))
    QApplication.processEvents()
    check("短文本单行", len(mgr._bubble._lines) == 1)
    check("单行比多行矮", mgr._bubble.height() < tall,
          f"{mgr._bubble.height()} < {tall}")

    # provider 里自己写的换行必须原样保留：整串丢给按字宽折行的话，"\n" 会被
    # 当成普通字符塞进一行中间，drawText 在那里再换一次行，第二行溢出单行高度
    # 的矩形、和后一行叠在一起（2026-11-11 实测截图：提示文字挤成一团）。
    two = "打开极客版并把一枚新登录码复制到剪贴板。\n登录码只能用一次；取新码会让旧的立即失效。"
    mgr._bubble.show_for(two, QPoint(100, 100))
    QApplication.processEvents()
    got = list(mgr._bubble._lines)
    check("自己写的换行不被折行吞掉", len(got) == 2, str(got))
    check("没有一行里还藏着 \\n", all("\n" not in line for line in got), str(got))
    check("两行各是完整的一句",
          got == two.split("\n"), str(got))
    check("两行的高度真的翻倍",
          mgr._bubble.height() == 2 * QFontMetrics(mgr._bubble._font).height()
          + tooltip.LINE_GAP + tooltip.PAD_Y * 2 + 2,
          f"{mgr._bubble.height()}px")

    # 换行 + 其中一段超长：既保换行，又要在段内折
    mgr._bubble.show_for(
        "动作：" + "把客厅的灯关掉并打开风扇" * 6 + "\n后果：旧的立即失效", QPoint(100, 100))
    QApplication.processEvents()
    got = list(mgr._bubble._lines)
    check("换行 + 超长段：第一段被折成多行", len(got) > 2, f"{len(got)} 行")
    check("最后一行是换行后的第二句", got[-1] == "后果：旧的立即失效", got[-1])
    check("每行都不超宽",
          all(QFontMetrics(mgr._bubble._font).horizontalAdvance(line) <= tooltip.MAX_W
              for line in got))

    # toast 会盖在**别的程序**上面（Qt.ToolTip 是顶层窗口），所以程序一失去激活
    # 就得收：点完卡片按钮打开浏览器，那一刻这条提示已经没有读者了。
    print("\n5.5) 一次性提示：失焦即收")
    mgr.show_toast("登录码 123456 已复制", QRect(100, 100, 200, 40))
    QApplication.processEvents()
    check("toast 起来了（计时器在跑）", mgr._bubble._toast_timer.isActive())  # noqa: SLF001

    # 真的走信号，而不是直接调方法：这条路径才是"浏览器抢焦点"时发生的
    QApplication.instance().applicationStateChanged.emit(
        Qt.ApplicationState.ApplicationInactive
    )
    check("程序失焦就收（不挂在浏览器上面）", mgr._bubble.isHidden())
    check("连带把 toast 的计时也停了", not mgr._bubble._toast_timer.isActive())  # noqa: SLF001

    mgr.show_toast("登录码 123456 已复制", QRect(100, 100, 200, 40))
    QApplication.processEvents()
    QApplication.instance().applicationStateChanged.emit(
        Qt.ApplicationState.ApplicationActive
    )
    check("还在前台就不收", mgr._bubble._toast_timer.isActive())  # noqa: SLF001
    mgr._bubble.hide()

    # 居中置顶那条：位置在屏幕上沿往下一点、切到浏览器也不收、成功失败两种颜色。
    # 它和上面两条的**唯一区别就是这三件事**，所以逐条钉住。
    print("\n5.6) 居中置顶提示")
    bubble = mgr._bubble
    area = QGuiApplication.primaryScreen().availableGeometry()
    mgr.show_message("登录码 123456 已复制，已打开极客版", QRect(100, 100, 200, 40),
                     theme.LIGHT, kind=tooltip.TOAST_OK)
    QApplication.processEvents()
    check("横向居中（不是锚在卡片上）",
          abs(bubble.x() + bubble.width() // 2 - area.center().x()) <= 2,
          f"中心 {bubble.x() + bubble.width() // 2} vs 屏幕 {area.center().x()}")
    expect_y = area.top() + int(area.height() * tooltip.TOAST_TOP_RATIO)
    check("落在屏幕上沿往下 18%（中间偏上，不贴边也不压住内容）",
          bubble.y() == expect_y, f"{bubble.y()} vs {expect_y}")
    check("字号比悬停提示大一档", bubble._font.pixelSize() == tooltip.MSG_FONT,  # noqa: SLF001
          f"{bubble._font.pixelSize()}px")  # noqa: SLF001
    check("弹出来了，且状态是 pinned", bubble.isVisible() and bubble.pinned())

    img = bubble.grab().toImage()
    dot_x = int(tooltip.MSG_PAD_X + tooltip.DOT_R)
    ok_dot = QColor(img.pixel(dot_x, bubble.height() // 2)).name().lower()
    check("成功：圆点是薄荷绿", ok_dot == theme.ACCENT_GREEN.lower(), ok_dot)
    ok_border = bubble.border_color()

    QApplication.instance().applicationStateChanged.emit(
        Qt.ApplicationState.ApplicationInactive
    )
    check("切到浏览器也不收（它就是给那一刻看的）", bubble.isVisible(),
          f"visible={bubble.isVisible()} pinned={bubble._pinned}")  # noqa: SLF001
    check("计时器还在跑（到点才收）", bubble._toast_timer.isActive())  # noqa: SLF001

    mgr.show_message("获取登录码失败：token 过期", QRect(100, 100, 200, 40),
                     theme.LIGHT, kind=tooltip.TOAST_FAIL)
    QApplication.processEvents()
    img = bubble.grab().toImage()
    fail_dot = QColor(img.pixel(dot_x, bubble.height() // 2)).name().lower()
    check("失败：圆点转警告红", fail_dot == theme.DANGER.lower(), fail_dot)
    fail_border = bubble.border_color()
    check("失败：连边框也跟着染（两种结果一眼分得开）", fail_border != ok_border,
          f"{fail_border} vs {ok_border}")

    # 复位：锚定 toast 仍然失焦即收，两套行为互不影响
    bubble.hide()
    mgr.show_toast("指令已发送", QRect(100, 100, 200, 40))
    QApplication.processEvents()
    QApplication.instance().applicationStateChanged.emit(
        Qt.ApplicationState.ApplicationInactive
    )
    check("锚定提示仍失焦即收（pinned 没有漏到别处）", bubble.isHidden())
    QApplication.instance().applicationStateChanged.emit(
        Qt.ApplicationState.ApplicationActive
    )

    # -- 6. 空文本 / 销毁 --------------------------------------------------
    print("\n6) 边界")    # 先把光标挪到没有控件的地方再收气泡：气泡隐藏时 Qt 会把 hover 重新派发给
    # 下面的控件，凭空产生一个 Enter（tooltip 里靠"光标真的在里面"挡掉了）。
    cursor["pos"] = QPoint(600, 600)
    mgr._dismiss()
    QApplication.processEvents()

    cursor["pos"] = QPoint(20, 220)
    empty = make_widget(0, 200)
    tooltip.watch(empty, lambda: "")
    QApplication.sendEvent(empty, QEvent(QEvent.Enter))
    QTest.qWait(tooltip.DELAY + 80)
    check("文本为空就不弹", not mgr._bubble.isVisible())

    cursor["pos"] = QPoint(20, 340)
    keep = make_widget(0, 320)
    tooltip.watch(keep, lambda: "会被销毁的卡片")
    QApplication.sendEvent(keep, QEvent(QEvent.Enter))
    QTest.qWait(tooltip.DELAY + 80)
    check("先弹出来", mgr._bubble.isVisible())
    keep.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QApplication.processEvents()
    check("目标被销毁后自动清理",
          keep not in mgr._providers and not mgr._bubble.isVisible())

    # -- 7. 真实卡片：提示只认标题带 ----------------------------------------
    print("\n7) 真实卡片：提示只认标题带")
    from types import SimpleNamespace

    from mihome.core.store import DeviceState
    from mihome.core.tree import DeviceNode
    from mihome.ui.device_card import (  # noqa: E402
        BODY_TOP, SUB_H, SUB_TOP, TITLE_TOP, DeviceCard,
    )

    class _Icons:
        def ensure(self, model) -> None:  # noqa: D102
            pass

        def scaled(self, model, size):  # noqa: D102
            return None

    prop = SimpleNamespace(key=(2, 1), siid=2, piid=1)
    ctl = SimpleNamespace(slot="power", prop=prop)
    spec = SimpleNamespace(
        category="light", gangs=[], span=1, controls=[ctl], metrics=[],
        layout="light", power_actions=None,
        control=lambda slot: ctl if slot == "power" else None,
    )
    node = DeviceNode("d1", "小米米家智能开关（三开单控）", "m", "zigm.switch",
                      home="我的家", room="客厅")
    state = DeviceState(node=node, spec=None, card=spec, values={(2, 1): True})
    card = DeviceCard(state, _Icons(), theme.LIGHT)
    card.move(400, 500)
    card.show()
    QApplication.processEvents()

    check("卡片注册了悬停提示", card in mgr._providers)
    check("卡片不再挂原生 tooltip（否则两层一起弹）", card.toolTip() == "",
          repr(card.toolTip()))
    check("卡片注册了命中区判据", card in mgr._hit_tests)

    # 命中区几何：标题/状态文字算，内容带（产品图 / 滑条 / 按键）一律不算
    check("标题文字在有效区", card._title_hit(QPoint(24, TITLE_TOP + 8)), True)
    check("副标题在有效区", card._title_hit(QPoint(24, SUB_TOP + SUB_H - 4)), True)
    check("内容带不在有效区", not card._title_hit(QPoint(24, BODY_TOP + 20)))
    check("卡片左上角空白不算", not card._title_hit(QPoint(2, 2)))
    check("电源键不算", not card._title_hit(card._power_rect().center()))
    mgr._dismiss()
    cursor["pos"] = card.mapToGlobal(QPoint(24, TITLE_TOP + 8))
    QApplication.sendEvent(card, QEvent(QEvent.Enter))
    QTest.qWait(tooltip.DELAY + 100)
    shown = "".join(mgr._bubble._lines)
    check("提示内容是设备全名", shown == node.name, shown)
    check("气泡落在卡片附近而不是屏幕角落",
          abs(mgr._bubble.pos().x() - cursor["pos"].x()) < 60,
          f"{mgr._bubble.pos()} vs 光标 {cursor['pos']}")

    # 光标从标题移到亮度滑条：父卡片只收到 MouseMove（没有 Leave/Enter），
    # 提示必须立刻收起，且 target 清掉，否则回到标题会秒弹。
    cursor["pos"] = card.mapToGlobal(QPoint(24, BODY_TOP + 20))
    send_move(card, QPoint(24, BODY_TOP + 20))
    QApplication.processEvents()
    check("移到内容带立刻收起", not mgr._bubble.isVisible())
    check("离开有效区后 target 清空", mgr._target is None)

    cursor["pos"] = card.mapToGlobal(QPoint(24, TITLE_TOP + 8))
    send_move(card, QPoint(24, TITLE_TOP + 8))
    check("回到标题重新开始计时", mgr._timer.isActive(), "")
    check("回标题不会秒弹", not mgr._bubble.isVisible())
    QTest.qWait(tooltip.DELAY + 100)
    check("等满才重新弹出", mgr._bubble.isVisible())

    # 光标从卡片外直接落到内容带（跳过标题）时，Enter 也不该启动计时
    mgr._dismiss()
    cursor["pos"] = card.mapToGlobal(QPoint(24, BODY_TOP + 20))
    QApplication.sendEvent(card, QEvent(QEvent.Enter))
    check("落在内容带的 Enter 不启动计时", not mgr._timer.isActive(), "")
    check("落在内容带不会弹提示", not mgr._bubble.isVisible())

    # 滑条是子控件：光标进去时父卡片收到 Leave，延后一拍也要按有效区收起
    mgr._dismiss()
    cursor["pos"] = card.mapToGlobal(QPoint(24, TITLE_TOP + 8))
    QApplication.sendEvent(card, QEvent(QEvent.Enter))
    QTest.qWait(tooltip.DELAY + 100)
    check("先弹出来", mgr._bubble.isVisible())
    cursor["pos"] = card.mapToGlobal(QPoint(24, BODY_TOP + 20))
    QApplication.sendEvent(card, QEvent(QEvent.Leave))
    QApplication.processEvents()
    check("光标进子控件后提示收起", not mgr._bubble.isVisible())
    mgr._dismiss()

    # -- 8. 通用命中区：同一控件里只认一部分 --------------------------------
    print("\n8) 通用 hit_test（局部坐标）")
    half = make_widget(680, 0, 120, 80)
    tooltip.watch(half, lambda: "只在左半边", hit_test=lambda pos: pos.x() < 60)
    cursor["pos"] = QPoint(690, 20)  # 局部 x=10
    QApplication.sendEvent(half, QEvent(QEvent.Enter))
    QTest.qWait(tooltip.DELAY + 80)
    check("有效区内会弹", mgr._bubble.isVisible())
    mgr._dismiss()
    cursor["pos"] = QPoint(770, 20)  # 局部 x=90，仍在控件矩形内
    QApplication.sendEvent(half, QEvent(QEvent.Enter))
    QTest.qWait(tooltip.DELAY + 80)
    check("同一控件但落在区外不弹", not mgr._bubble.isVisible())
    check("区外也没启动计时", not mgr._timer.isActive())

    for widget in (left, right, empty, half):
        widget.close()

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
