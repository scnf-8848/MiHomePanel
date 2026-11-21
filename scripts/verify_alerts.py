# SPDX-License-Identifier: GPL-3.0-or-later
"""验证轮询异常提醒：分级门槛、去重、恢复与关闭、顶部警告条、成功判据。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_alerts.py``

全程离线：策略与判据部分不建 Client、不发请求；警告条用 offscreen 平台起一个
QApplication，只验尺寸与关闭信号，不做真实渲染比对。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 主窗口会建 Prefs，别让它落到真实用户目录
_TMP = Path(tempfile.mkdtemp(prefix="mihome-alerts-"))
os.environ["MIHOME_DATA_DIR"] = str(_TMP / "data")

from mihome.core import alerts as A  # noqa: E402
from mihome.core.api import Client  # noqa: E402
from mihome.core.health import Failure, PollHealth  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAILS.append(name)


def snap(streak: int = 0, kind: str = "network", code: str = "", text: str = "", seconds: float = 0.0) -> dict:
    if streak == 0:
        return {"healthy": True, "streak": 0, "last_failure": None, "episode": None}
    return {
        "healthy": False,
        "streak": streak,
        "last_failure": {"kind": kind, "code": code, "text": text},
        "episode": {"seconds": seconds, "count": streak},
    }


# ---------------------------------------------------------------------------
def test_thresholds() -> None:
    p = A.AlertPolicy()
    check("网络第 1 次不打扰", not p.observe(snap(1, seconds=3), 0).bar)
    check("网络第 2 次不打扰", not p.observe(snap(2, seconds=6), 3).bar)
    u = p.observe(snap(3, seconds=9), 6)
    check("网络第 3 次出现横幅", u.bar is not None and u.bar.level == A.WARNING)
    check("网络第 3 次弹一次通知", u.toast is not None)
    check("横幅标题是连接异常", u.bar.title == "连接异常", u.bar.title)
    check("横幅正文带次数与时长", "已连续失败 3 次" in u.bar.detail and "9 秒" in u.bar.detail, u.bar.detail)

    p2 = A.AlertPolicy()
    u = p2.observe(snap(1, kind="auth", code="-10030", text="code: -10030, message: 无效的token（HTTP）"), 0)
    check("授权失效第一次就报", u.bar is not None and u.bar.level == A.ERROR)
    check("授权文案提示重新登录", "重新登录" in u.bar.detail, u.bar.detail)
    check("授权文案带原始错误码", "code=-10030" in u.bar.detail, u.bar.detail)
    check("授权第一次就弹通知", u.toast is not None)

    p3 = A.AlertPolicy()
    u = p3.observe(snap(1, kind="server", code="-704040992", text="请求过于频繁（本轮 138/138 条全部失败）"), 0)
    check("限流第一次就报", u.bar is not None)
    check("限流文案保留服务端说明", "请求过于频繁" in u.bar.detail, u.bar.detail)
    check("限流第一次就弹通知", u.toast is not None)

    p4 = A.AlertPolicy()
    check("协议错误第 1 次不报", not p4.observe(snap(1, kind="protocol"), 0).bar)
    check("协议错误第 2 次报", p4.observe(snap(2, kind="protocol"), 3).bar is not None)


def test_dedupe() -> None:
    p = A.AlertPolicy()
    first = p.observe(snap(3, seconds=9), 0)
    again = p.observe(snap(4, seconds=12), 3)
    check("同段失败不重复出横幅", again.bar is None)
    check("同段失败不重复弹通知", again.toast is None)
    check("末次失败次数被记下", first.bar is not None)

    refreshed = p.observe(snap(9, seconds=30), 20)
    check("超过刷新间隔才更新计数", refreshed.bar is not None)
    check("更新后的文案是新次数", "已连续失败 9 次" in refreshed.bar.detail, refreshed.bar.detail)

    # 恢复 → 绿条
    rec = p.observe(snap(0), 21)
    check("恢复后横幅变绿", rec.bar is not None and rec.bar.level == A.SUCCESS)
    check("恢复时不弹通知", rec.toast is None)
    check("恢复文案带本次失败次数", "连续失败 9 次" in rec.bar.detail, rec.bar.detail)
    check("恢复后不重复出绿条", p.observe(snap(0), 24).bar is None)

    # 抖动：恢复后立刻又坏，通知要受冷却压制
    flap = p.observe(snap(3, seconds=9), 30)
    check("再次失败横幅重新出现", flap.bar is not None)
    check("冷却期内不再弹通知", flap.toast is None)
    p2 = A.AlertPolicy()
    p2.observe(snap(3, seconds=9), 0)
    p2.observe(snap(0), 5)
    late = p2.observe(snap(3, seconds=9), 130)
    check("冷却结束后可再弹一次", late.toast is not None)

    # 冷却按类别分开算：刚弹过网络异常，紧接着授权失效仍要立刻弹
    mixed = A.AlertPolicy()
    mixed.observe(snap(3, seconds=9), 0)
    auth = mixed.observe(snap(1, kind="auth", code="-10030"), 3)
    check("别的类别的通知不被冷却压掉", auth.toast is not None)
    check("切换类别时横幅也跟着换", auth.bar is not None and auth.bar.level == A.ERROR)


def test_dismiss() -> None:
    p = A.AlertPolicy()
    p.observe(snap(3, seconds=9), 0)
    p.dismiss()
    after = p.observe(snap(4, seconds=12), 3)
    check("关掉后同类失败不再冒出来", after.bar is None and after.toast is None)
    check("关掉后恢复也不再冒出来", p.observe(snap(0), 6).bar is None)
    # 恢复之后再坏，是新的问题，要重新提示
    later = p.observe(snap(3, kind="network", seconds=9), 200)
    check("恢复正常后新故障重新提示", later.bar is not None)

    # 关掉的是绿条：不算屏蔽，新的失败仍然提示
    q = A.AlertPolicy()
    q.observe(snap(3, seconds=9), 0)
    q.observe(snap(0), 3)
    q.dismiss()
    check("关掉绿条后新故障仍提示", q.observe(snap(3, seconds=9), 200).bar is not None)

    # 同类失败期间换成另一种错误：是新问题
    r = A.AlertPolicy()
    r.observe(snap(3, seconds=9), 0)
    r.dismiss()
    switch = r.observe(snap(4, kind="auth", code="-10030", seconds=12), 3)
    check("关掉网络提示后授权失效仍要提示", switch.bar is not None and switch.bar.level == A.ERROR)


def test_widget() -> None:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from mihome.ui import theme
    from mihome.ui.alert_bar import AlertBar

    app = QApplication.instance() or QApplication([])
    bar = AlertBar(theme.DARK)
    recalls: list[int] = []
    bar.dismissed.connect(lambda: recalls.append(1))
    bar.set_alert(A.WARNING, "连接异常", "已连续失败 3 次（9 秒），正在自动重试")
    bar.resize(900, 48)
    bar.show()
    bar.repaint()
    QTest.qWait(20)

    check("警告条高度固定 48", bar.height() == 48)
    check("警告条内容可读", bar.alert() == (A.WARNING, "连接异常", "已连续失败 3 次（9 秒），正在自动重试"))
    check("警告条没有自动消失的定时器", not bar.findChildren(QTimer))

    QTest.mouseClick(bar, Qt.LeftButton, pos=bar._close_rect.center())  # noqa: SLF001
    check("点 × 发出 dismissed", bool(recalls))

    # 没有定时器 = 不会自己消失：等一会儿仍然是显示的
    QTest.qWait(100)
    check("等待后仍然显示（不自动消失）", bar.isVisible())

    bar.set_alert(A.SUCCESS, "已恢复", "连接已恢复正常，本次连续失败 3 次，持续 9 秒")
    check("可以原地切成恢复态", bar.alert()[0] == A.SUCCESS)
    bar.close()
    app.processEvents()


def test_window() -> None:
    """主窗口这一层的接线：快照 → 策略 → 横幅 / 通知，以及点 × 的后续。"""
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QApplication

    from mihome.ui import theme
    from mihome.ui.main_window import ALERT_MARGIN_X, MainWindow

    app = QApplication.instance() or QApplication([])

    class FakeTray:
        def __init__(self) -> None:
            self.messages: list[tuple[str, str, int, bool]] = []

        def set_status(self, _text: str) -> None:
            pass

        def show_message(self, title: str, body: str, msecs: int = 4000, *, warning: bool = False) -> bool:
            self.messages.append((title, body, msecs, warning))
            return True

    win = MainWindow(theme.LIGHT, tray_available=False)
    # 别把测试写进用户真实的 QSettings（窗口几何 / 当前家庭 / 主题）
    win._save_settings = lambda: None  # type: ignore[method-assign]  # noqa: SLF001
    tray = FakeTray()
    win.tray = tray  # type: ignore[assignment]
    try:
        check("测试用的是临时数据目录", str(win.prefs.path).startswith(str(_TMP)), str(win.prefs.path))
        check("没有异常时警告条不占位", win._alert_host.isVisibleTo(win) is False)  # noqa: SLF001

        win.show()
        app.processEvents()
        wall_before = win.wall.geometry()

        # 网络类：前两次不该出现，第三次才出
        win._on_poll_health(snap(1, seconds=3))  # noqa: SLF001
        win._on_poll_health(snap(2, seconds=6))  # noqa: SLF001
        check("容忍期内不出横幅", win._alert_host.isVisibleTo(win) is False)  # noqa: SLF001
        check("容忍期内不弹通知", not tray.messages)

        win._on_poll_health(snap(3, seconds=9))  # noqa: SLF001
        check("第 3 次失败显示横幅", win._alert_host.isVisibleTo(win) is True)  # noqa: SLF001

        # -- 浮层语义：不进布局、压在内容上、只在自身矩形里接管事件 ----------
        host = win._alert_host  # noqa: SLF001
        check("出现横幅时卡片墙位置完全没动", win.wall.geometry() == wall_before,
              f"{wall_before} → {win.wall.geometry()}")
        check("浮层的父控件是内容区", host.parent() is win._body)  # noqa: SLF001
        check("浮层左右留边一致", host.x() == win.nav.width() + ALERT_MARGIN_X
              and host.width() == win._body.width() - win.nav.width() - ALERT_MARGIN_X * 2,  # noqa: SLF001
              f"x={host.x()} w={host.width()} body={win._body.width()}")  # noqa: SLF001
        check("浮层只占卡片自身高度", host.height() == 48, str(host.height()))
        at_card = win._body.childAt(host.geometry().center())  # noqa: SLF001
        check("浮层压在卡片墙之上", at_card is host or host.isAncestorOf(at_card), str(at_card))
        below = QPoint(win.nav.width() + 40, host.geometry().bottom() + 12)
        at_below = win._body.childAt(below)  # noqa: SLF001
        check("浮层矩形之外仍归内容区（可滚动/点击）",
              at_below is not host and not host.isAncestorOf(at_below), str(at_below))
        win.resize(1000, 700)
        app.processEvents()
        check("窗口缩放后浮层跟着重新定位",
              host.width() == win._body.width() - win.nav.width() - ALERT_MARGIN_X * 2,  # noqa: SLF001
              f"{host.width()} / {win._body.width()}")  # noqa: SLF001

        level, title, detail = win.alert_bar.alert()
        check("横幅内容是连接异常", (level, title) == (A.WARNING, "连接异常"), f"{level} / {title}")
        check("横幅正文带次数", "已连续失败 3 次" in detail, detail)
        check("同时弹一次系统通知", len(tray.messages) == 1, str(len(tray.messages)))
        if tray.messages:
            _t, body, _ms, warning = tray.messages[0]
            check("通知文案与横幅一致", "连接异常" in body and warning is True, body)

        # 同一段失败继续：每秒一轮也不该反复弹
        for n in range(4, 10):
            win._on_poll_health(snap(n, seconds=3 * n))  # noqa: SLF001
        check("持续失败不再弹通知", len(tray.messages) == 1, str(len(tray.messages)))
        check("持续失败横幅还挂着", win._alert_host.isVisibleTo(win) is True)  # noqa: SLF001

        # 用户点 ×：收起，并且这段失败结束前不许自己冒回来
        win.alert_bar.dismissed.emit()
        app.processEvents()
        check("点 × 后横幅收起", win._alert_host.isVisibleTo(win) is False)  # noqa: SLF001
        win._on_poll_health(snap(10, seconds=30))  # noqa: SLF001
        check("关掉后同类失败不再冒出来", win._alert_host.isVisibleTo(win) is False)  # noqa: SLF001
        check("关掉后也不再弹通知", len(tray.messages) == 1, str(len(tray.messages)))

        # 恢复：已经关掉了就不出绿条
        win._on_poll_health(snap(0))  # noqa: SLF001
        check("关掉后恢复也不出绿条", win._alert_host.isVisibleTo(win) is False)  # noqa: SLF001

        # 新的一段故障：换了类别（授权失效）要立刻提示，且允许再弹
        win._on_poll_health(snap(1, kind="auth", code="-10030", text="code: -10030, message: 无效的token"))  # noqa: SLF001
        check("授权失效立刻出横幅", win._alert_host.isVisibleTo(win) is True)  # noqa: SLF001
        check("授权失效用红色", win.alert_bar.alert()[0] == A.ERROR, win.alert_bar.alert()[0])
        check("授权失效弹通知", len(tray.messages) == 2, str(len(tray.messages)))

        # 恢复：原地变绿，不自动消失；用户关掉之后才算翻篇
        win._on_poll_health(snap(0))  # noqa: SLF001
        check("恢复后横幅还在", win._alert_host.isVisibleTo(win) is True)  # noqa: SLF001
        check("恢复后变绿并写明原因", win.alert_bar.alert()[0] == A.SUCCESS and "已恢复" in win.alert_bar.alert()[1],
              str(win.alert_bar.alert()[:2]))
        check("恢复不弹通知", len(tray.messages) == 2, str(len(tray.messages)))
        win.alert_bar.dismissed.emit()
        app.processEvents()
        check("关掉绿条后收起", win._alert_host.isVisibleTo(win) is False)  # noqa: SLF001
        check("关掉绿条后仍不弹", len(tray.messages) == 2, str(len(tray.messages)))
    finally:
        win._poll_timer.stop()  # noqa: SLF001
        win._drain_timer.stop()  # noqa: SLF001
        win._topo_timer.stop()  # noqa: SLF001
        win.hide()
        win.deleteLater()
        app.processEvents()



def test_poll_ok() -> None:
    class FakeHealth:
        def __init__(self) -> None:
            self.failures: list[Failure] = []
            self.successes: list[int] = []

        def failure(self, fail: Failure) -> None:
            self.failures.append(fail)

        def success(self, *, items: int = 0) -> None:
            self.successes.append(items)

    class FakeClient:
        last_poll_ok: bool | None = None

        def __init__(self) -> None:
            self.health = FakeHealth()

    c = FakeClient()
    from mihome.core.api import PropRef, ReadValue

    refs = [PropRef("d1", 2, i) for i in (1, 2)]
    ok = {r: ReadValue(ref=r, value=1, ok=True) for r in refs}
    Client._record_poll(c, ok, None, 2)  # type: ignore[arg-type]
    check("读到东西 → 成功", c.last_poll_ok is True)

    dead = {r: ReadValue(ref=r, code=-704042011) for r in refs}
    Client._record_poll(c, dead, None, 2)  # type: ignore[arg-type]
    check("整批被拒 → 失败", c.last_poll_ok is False)

    Client._record_poll(c, {}, None, 0)  # type: ignore[arg-type]
    check("没有可读属性 → 成功（不误报）", c.last_poll_ok is True)

    Client._record_poll(c, {}, Failure("network", "", "timed out"), 2)  # type: ignore[arg-type]
    check("请求层异常 → 失败", c.last_poll_ok is False)


def test_store_criterion() -> None:
    from mihome.core.store import Store

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        health = PollHealth(
            log_path=folder / "h.log", state_path=folder / "h.json"
        )

        class FakeClient:
            last_error = ""
            last_poll_ok: bool | None = None

            def __init__(self) -> None:
                self.health = health

        st = Store.__new__(Store)
        st.client = FakeClient()  # type: ignore[assignment]
        st.devices = {}
        st._fail_streak = 0
        st._next_poll_at = 0.0
        st.last_error = ""
        st.on_status = None
        st.on_changed = None

        st.client.last_poll_ok = False
        st._apply_poll({}, None)
        check("空 dict + 标记失败 → 计入退避", st._fail_streak == 1, str(st._fail_streak))
        check("空 dict 失败后安排重试", st._next_poll_at > 0)
        check("失败原因不再写成 None", "None" not in st.last_error, st.last_error)

        st._apply_poll({}, None)
        check("连续失败继续累加", st._fail_streak == 2, str(st._fail_streak))
        check("退避间隔进健康快照", (health.snapshot()["backoff"] or {}).get("seconds") == 12.0)

        st.client.last_poll_ok = True
        st._apply_poll({}, None)
        check("空读但标记成功 → 不判失败", st._fail_streak == 0)
        check("恢复后清掉重试时间", st._next_poll_at == 0.0)
        health.close()


def test_dev_fault() -> None:
    """开发用故障注入：默认关闭、四类形态真实、按轮数自愈、只打轮询。"""
    import os

    from mihome.core import dev_fault
    from mihome.core.health import AUTH, NETWORK, PROTOCOL, SERVER, classify_error

    try:
        check("没设环境变量时默认关闭", dev_fault.parse("") is None)
        check("off / 0 显式关闭", dev_fault.parse("off") is None and dev_fault.parse("network:0") is None)
        check("拼错的类型不生效", dev_fault.parse("netwrok") is None)

        for kind, expect in (
            ("network", NETWORK),
            ("auth", AUTH),
            ("server", SERVER),
            ("protocol", PROTOCOL),
        ):
            inj = dev_fault.parse(kind)
            factory = inj.next_poll_error() if inj is not None else None
            got = classify_error(factory()).kind if factory is not None else "—"
            check(f"{kind} 注入的异常被分到 {expect}", got == expect, got)

        limited = dev_fault.parse("network:2")
        rounds = [limited.next_poll_error() is not None for _ in range(3)]
        check("指定轮数后自愈", rounds == [True, True, False], str(rounds))

        # -- 走真实 read 路径：注入 → 请求层异常 → 健康记账 ------------------
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            health = PollHealth(log_path=folder / "h.log", state_path=folder / "h.json")

            class FakeApi:
                locale = "zh_CN"

                def get_devices_prop(self, _payload):
                    return [{"did": "d1", "siid": 2, "piid": 1, "code": 0, "value": 7}]

            from mihome.core.api import Client, PropRef

            refs = [PropRef("d1", 2, 1)]
            os.environ[dev_fault.ENV] = "auth:1"
            dev_fault.reset()
            c = Client(api=FakeApi(), health=health)  # type: ignore[arg-type]
            out = c.read(refs, source="poll")
            snap = health.snapshot()
            check("轮询注入：整轮判失败", c.last_poll_ok is False)
            check("轮询注入：返回空结果", out == {})
            check("轮询注入：健康账本记授权类",
                  snap["streak"] == 1 and (snap["last_failure"] or {}).get("kind") == AUTH,
                  str(snap["last_failure"]))
            check("轮询注入：保留真实错误码",
                  (snap["last_failure"] or {}).get("code") == "-10030",
                  str((snap["last_failure"] or {}).get("code")))
            check("注入文案带标记，日志里一眼能认出",
                  "注入故障" in str((snap["last_failure"] or {}).get("text")),
                  str((snap["last_failure"] or {}).get("text")))

            out = c.read(refs, source="poll")
            check("轮完 N 次后自愈：真实请求通过", c.last_poll_ok is True)
            check("自愈后读到值", out[refs[0]].value == 7, str(out.get(refs[0])))
            check("自愈后健康账本归零", health.snapshot()["streak"] == 0)

            # 非轮询路径不注入：写后补读 / 详情页按需读必须照常
            os.environ[dev_fault.ENV] = "network"
            dev_fault.reset()
            out = c.read(refs)
            check("非轮询读不受注入影响", out[refs[0]].value == 7, str(out.get(refs[0])))
            check("非轮询读不记健康账本", health.snapshot()["streak"] == 0)
            health.close()
    finally:
        os.environ.pop(dev_fault.ENV, None)
        dev_fault.reset()


def main() -> int:
    test_thresholds()
    test_dedupe()
    test_dismiss()
    test_poll_ok()
    test_store_criterion()
    test_dev_fault()
    test_widget()
    test_window()
    print()
    if FAILS:
        print(f"FAIL {len(FAILS)} 项：" + "；".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
