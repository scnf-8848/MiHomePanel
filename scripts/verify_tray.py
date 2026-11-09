# SPDX-License-Identifier: GPL-3.0-or-later
"""验证系统托盘常驻（`mihome/ui/tray.py` 与主窗口接线）。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_tray.py``

离屏平台**没有真的通知区域**，所以这里的做法是把"能不能用托盘"当作参数传进去
（``TrayController(available=True)`` / ``MainWindow(tray_available=True)``），
验证的是**判断与接线**，不是 Windows 托盘本身。真正"图标出现在右下角、单击能
把窗口叫回来"只能在真机上人工看一次。

覆盖三段：
* 图标与控制器的机制（多尺寸、信号、菜单结构、场景子菜单的重建）
* 主窗口的关闭语义：收进托盘 ≠ 退出、关掉偏好后真的退出、退出前摘图标
* 设置页「常规 → 收到托盘」这一行（可用/不可用两种形态）
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_TMP = Path(tempfile.mkdtemp(prefix="mihome-tray-"))
os.environ["MIHOME_DATA_DIR"] = str(_TMP / "data")
os.environ.pop("MIHOME_NO_TRAY", None)

from PySide6.QtCore import QSettings, Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QSystemTrayIcon  # noqa: E402

from mihome.ui import theme, tray  # noqa: E402
from mihome.ui.main_window import POLL_HIDDEN_MS, POLL_MS  # noqa: E402
from mihome.ui.menu import Menu  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAILS.append(f"{name} {detail}")


def _texts(menu) -> list[str]:
    return [a.text() for a in menu.actions()]


def _find(menu, text: str):
    for act in menu.actions():
        if act.text() == text:
            return act
    return None


def _emitted(signal, action) -> bool:
    """触发一下 action，回头看信号有没有出来。"""
    hits: list[object] = []

    def slot(*_args) -> None:  # triggered(bool) 会带一个参数过来
        hits.append(1)
    signal.connect(slot)
    try:
        action.trigger()
    finally:
        signal.disconnect(slot)
    return bool(hits)


# -- 图标 ------------------------------------------------------------------
def _icon_checks() -> None:
    for pal in (theme.LIGHT, theme.DARK):
        icon = tray.tray_icon(pal)
        sizes = {s.width() for s in icon.availableSizes()}
        check(f"{pal.name}：图标是多尺寸的（不靠缩小大图）",
              sizes == {16, 20, 24, 32, 48}, str(sorted(sizes)))
        pix = icon.pixmap(32, 32)
        img = pix.toImage()
        center = img.pixelColor(16, 16)
        corner = img.pixelColor(0, 0)
        check(f"{pal.name}：中间有内容", center.alpha() > 200, str(center.alpha()))
        # 四个角必须透：方块是圆角的，角上要是实色，16px 下看起来就是一个正方形
        check(f"{pal.name}：左上角透明（圆角）", corner.alpha() == 0, str(corner.alpha()))


# -- 控制器 ----------------------------------------------------------------
def _controller_checks(app: QApplication) -> None:
    check("MIHOME_NO_TRAY=1 时托盘算不可用",
          (os.environ.__setitem__("MIHOME_NO_TRAY", "1"),
           tray.tray_available())[1] is False,
          "环境变量是排障开关，必须能一刀切掉")
    os.environ.pop("MIHOME_NO_TRAY", None)

    # 不可用：install 返回 False，且不能顺手改掉"关最后一个窗口就退出"
    dead = tray.TrayController(theme.LIGHT, available=False)
    before = app.quitOnLastWindowClosed()
    check("不可用时 install 返回 False", dead.install() is False)
    check("不可用时不算挂着", dead.active is False)
    check("不可用时不动 quitOnLastWindowClosed",
          app.quitOnLastWindowClosed() == before,
          f"{before} → {app.quitOnLastWindowClosed()}")
    dead.show_message("x", "y")  # 不可用时也不该炸
    check("不可用时发气泡不报错", True)

    t = tray.TrayController(theme.LIGHT, available=True)
    check("可用时 install 返回 True", t.install() is True)
    check("可用时算挂着", t.active is True)
    # 关键：隐藏最后一个窗口不是"关闭"，默认的自动退出会把程序带走
    check("可用时关掉 quitOnLastWindowClosed", app.quitOnLastWindowClosed() is False)

    # -- 信号 --------------------------------------------------------------
    check("单击图标要求打开窗口",
          _activation_emits(t, QSystemTrayIcon.ActivationReason.Trigger))
    check("双击图标也要求打开窗口",
          _activation_emits(t, QSystemTrayIcon.ActivationReason.DoubleClick))
    check("右键不当作打开（要出菜单）",
          _activation_emits(t, QSystemTrayIcon.ActivationReason.Context) is False)

    # -- 菜单结构 ----------------------------------------------------------
    m = t.menu()
    texts = [x for x in _texts(m) if x]
    check("菜单只有「打开主窗口」「退出」", texts == ["打开主窗口", "退出"], str(texts))
    check("「常用场景」「刷新设备」已去掉",
          _find(m, "常用场景") is None and _find(m, "刷新设备") is None)
    check("菜单是自绘圆角菜单（透明底 + 去边框 + 去系统阴影）",
          isinstance(m, Menu)
          and m.testAttribute(Qt.WA_TranslucentBackground)
          and bool(m.windowFlags() & Qt.FramelessWindowHint)
          and bool(m.windowFlags() & Qt.NoDropShadowWindowHint))
    check("菜单样式自带圆角（独立窗口，不跟主窗口 QSS 走）",
          f"border-radius: {theme.RADIUS_SMALL}px" in m.styleSheet())

    check("菜单里的「打开主窗口」能发信号",
          _emitted(t.open_requested, _find(m, "打开主窗口")))
    check("菜单里的「退出」能发信号",
          _emitted(t.quit_requested, _find(m, "退出")))

    # 换主题时菜单要跟着换（它自己带一份样式表，不换就是白底配深色文字）
    t.set_palette(theme.DARK)
    check("换主题后菜单底色跟着变", theme.DARK.card in m.styleSheet(), m.styleSheet()[:60])
    t.set_palette(theme.LIGHT)

    # -- 提示与摘除 --------------------------------------------------------
    t.set_status("已连接 · 12 台设备")
    check("悬停提示带状态行",
          "12 台设备" in t._icon.toolTip(), t._icon.toolTip())  # noqa: SLF001
    t.set_status("")
    check("状态为空时只留程序名", t._icon.toolTip() == "米家控制台", t._icon.toolTip())  # noqa: SLF001

    t.remove()
    check("remove 之后不再算挂着", t.active is False)


def _activation_emits(controller: tray.TrayController, reason) -> bool:
    hits: list[int] = []
    slot = lambda: hits.append(1)  # noqa: E731
    controller.open_requested.connect(slot)
    try:
        controller._on_activated(reason)  # noqa: SLF001
    finally:
        controller.open_requested.disconnect(slot)
    return bool(hits)


# -- 主窗口 ----------------------------------------------------------------
def _window_checks(app: QApplication) -> None:
    from mihome.ui.main_window import MainWindow

    # 关掉偏好：关窗就真的退出（托盘可用也不拦）
    win = MainWindow(theme.LIGHT, tray_available=True)
    try:
        check("主窗口的数据目录是临时的",
              str(win.prefs.path).startswith(str(_TMP)), str(win.prefs.path))
        check("默认就是收进托盘", win.prefs.close_to_tray is True)
        check("托盘已挂上", win.tray.active is True)

        # -- 轮询降频：看不见的时候不该还按 3 秒一敲 ----------------------
        polls: list[int] = []
        win._poll = lambda: polls.append(1)  # type: ignore[method-assign]  # noqa: SLF001
        check("从没显示过就用降频档",
              win._poll_timer.interval() == POLL_HIDDEN_MS,  # noqa: SLF001
              f"{win._poll_timer.interval()}ms")  # noqa: SLF001

        win.show()
        app.processEvents()
        check("显示出来是 3 秒一轮",
              win._poll_timer.interval() == POLL_MS,  # noqa: SLF001
              f"{win._poll_timer.interval()}ms")  # noqa: SLF001
        check("切到前台立刻补一轮", polls[-1:] == [1], str(len(polls)))
        check("最小化也降频",
              (win.showMinimized(), app.processEvents(),
               win._poll_timer.interval())[2] == POLL_HIDDEN_MS,  # noqa: SLF001
              f"{win._poll_timer.interval()}ms")  # noqa: SLF001
        polls.clear()
        win.showNormal()
        app.processEvents()
        check("还原回来恢复 3 秒并补一轮",
              win._poll_timer.interval() == POLL_MS and polls,  # noqa: SLF001
              f"{win._poll_timer.interval()}ms / {len(polls)} 次")  # noqa: SLF001

        win.close()
        app.processEvents()
        check("关窗后窗口藏起来", win.isVisible() is False)
        check("收进托盘后降频到 15 秒",
              win._poll_timer.interval() == POLL_HIDDEN_MS,  # noqa: SLF001
              f"{win._poll_timer.interval()}ms")  # noqa: SLF001
        # 这是整件事的要点：关窗不等于退出
        check("关窗后没走退出清理", win._shutdown_done is False)  # noqa: SLF001
        check("关窗后没退程序", app.closingDown() is False)
        check("第一次收进托盘会提示一次", win._tray_hint_shown is True)  # noqa: SLF001

        # 从托盘叫回来
        win.show_from_tray()
        app.processEvents()
        check("托盘单击能把窗口叫回来", win.isVisible() is True)
        check("叫回来恢复 3 秒一轮",
              win._poll_timer.interval() == POLL_MS)  # noqa: SLF001

        # -- 多消费者：窗口不能盖掉别人要的频率 --------------------------
        # 这是 poll_demand 这一层存在的**唯一理由**。将来桌面挂件要 1 秒时，
        # 窗口切后台不能把它顶回 15 秒。
        win.poll_demand.set("widget", 1000, "桌面挂件（假）")
        check("别人要 1 秒时立刻按 1 秒跑",
              win._poll_timer.interval() == 1000,  # noqa: SLF001
              f"{win._poll_timer.interval()}ms")  # noqa: SLF001
        win.close()  # 收进托盘 = 窗口自己退到 15 秒档
        app.processEvents()
        check("窗口收进托盘也顶不掉挂件的 1 秒",
              win._poll_timer.interval() == 1000,  # noqa: SLF001
              f"{win._poll_timer.interval()}ms")  # noqa: SLF001
        win.poll_demand.clear("widget")
        check("撤掉挂件后回到窗口自己的 15 秒",
              win._poll_timer.interval() == POLL_HIDDEN_MS,  # noqa: SLF001
              f"{win._poll_timer.interval()}ms")  # noqa: SLF001
        win.show_from_tray()
        app.processEvents()
        check("叫回来后仍是 3 秒", win._poll_timer.interval() == POLL_MS)  # noqa: SLF001
        check("事件轮询表存在但没人要时是停的",
              win.poll_events.interval_ms is None,  # noqa: SLF001
              str(win.poll_events.interval_ms))  # noqa: SLF001

        # 关掉偏好之后，关闭按钮就是退出。
        # 这里要盯的是"程序真的退"，不是"清理跑过"：托盘把
        # quitOnLastWindowClosed 关了，只 shutdown + accept 会让窗口消失、进程
        # 和图标却留在托盘里——用户看到的就是"选了退出程序，结果还是缩到托盘"。
        win.prefs.close_to_tray = False
        quit_calls: list[str] = []
        real_quit = app.quit

        def spy_quit() -> None:
            quit_calls.append("quit")
        app.quit = spy_quit  # type: ignore[method-assign]
        try:
            win.close()
            app.processEvents()
        finally:
            app.quit = real_quit  # type: ignore[method-assign]
        check("偏好关掉后关窗就是退出", win._shutdown_done is True)  # noqa: SLF001
        check("关窗真的请求了退出（不是只把窗口销毁）",
              quit_calls == ["quit"], str(quit_calls))
        check("退出时把托盘图标摘了", win.tray.active is False)
        check("退出清理是幂等的", (win.shutdown(), win._shutdown_done)[1] is True)  # noqa: SLF001
    finally:
        win.deleteLater()
        app.processEvents()

    # 托盘不可用：一切照旧（关窗即退出），不能出现"藏起来了但没有入口"
    win2 = MainWindow(theme.LIGHT, tray_available=False)
    try:
        check("托盘不可用时没挂图标", win2.tray.active is False)
        # 偏好开着也不该拦：托盘不可用时"藏起来"等于程序失踪
        win2.prefs.close_to_tray = True
        win2.show()
        app.processEvents()
        win2.close()
        app.processEvents()
        check("托盘不可用时关窗直接退出", win2._shutdown_done is True)  # noqa: SLF001
        check("退出后窗口真的关了", win2.isVisible() is False)
    finally:
        win2.deleteLater()
        app.processEvents()

    # 开机自启：默认直接进托盘（不显示窗口），进不去就老实退回最小化
    win5 = MainWindow(theme.LIGHT, tray_available=True)
    try:
        check("默认就配成「自启时进托盘」", win5.prefs.start_in_tray is True)
        check("自启 + 进得了托盘：不显示窗口",
              win5.start_in_tray() is True and win5.isVisible() is False)
        # 从没显示过的窗口不该把几何落盘：否则开机进托盘、随后退出，会把用户
        # 上次调好的窗口大小覆盖成默认值
        stored = QSettings("MiHomePanel", "MiHomePanel")
        before = stored.value("window/geometry")
        win5._save_settings()  # noqa: SLF001
        check("没显示过的窗口不覆盖已存的几何",
              QSettings("MiHomePanel", "MiHomePanel").value("window/geometry") == before)

        win5.prefs.start_in_tray = False
        check("关掉这一项就不再进托盘", win5.start_in_tray() is False)
    finally:
        win5.deleteLater()
        app.processEvents()

    win6 = MainWindow(theme.LIGHT, tray_available=False)
    try:
        win6.prefs.start_in_tray = True
        # 没有托盘还"藏"起来 = 程序在哪都看不见，必须退回去
        check("没有托盘时不会进托盘", win6.start_in_tray() is False)
    finally:
        win6.deleteLater()
        app.processEvents()

    # 退出：先摘图标再退
    win3 = MainWindow(theme.LIGHT, tray_available=True)
    try:
        win3.show()
        app.processEvents()
        win3.quit_app()
        check("退出时走了清理", win3._shutdown_done is True)  # noqa: SLF001
        check("退出时把托盘图标摘了", win3.tray.active is False)
        check("退出时窗口不再显示", win3.isVisible() is False)
    finally:
        win3.deleteLater()
        app.processEvents()

    # 系统注销/关机：不能因为它拦着不退
    win4 = MainWindow(theme.LIGHT, tray_available=True)
    try:
        check("正在退出时不再收进托盘",
              (setattr(win4, "_quitting", True),
               win4._should_hide_to_tray())[1] is False)  # noqa: SLF001
    finally:
        win4.deleteLater()
        app.processEvents()


# -- 设置页 ----------------------------------------------------------------
class _EmptyStore:
    def __init__(self, prefs) -> None:
        self.prefs = prefs

    def __getattr__(self, name: str):
        return getattr(self.prefs, name)

    def homes(self):
        return []

    def room_names(self, *_a, **_k):
        return []

    def device_states(self, *_a, **_k):
        return []

    def home_devices(self, *_a, **_k):
        return []

    def scenes_for(self, *_a, **_k):
        return []


def _settings_checks(app: QApplication) -> None:
    from mihome.core.prefs import Prefs
    from mihome.ui.settings import SettingsDialog

    prefs = Prefs(_TMP / "settings-prefs.json")
    store = _EmptyStore(prefs)

    dlg = SettingsDialog(store, theme.LIGHT, None, account={}, tray_available=True)
    try:
        dlg.nav.setCurrentRow(4)
        # 关闭行为是两个圆点单选，不是在右边挂一个开关
        check("托盘可用时「最小化到托盘」被选中", dlg.rb_tray.isChecked() is True)
        check("另一个「退出程序」没选中", dlg.rb_quit.isChecked() is False)
        check("两个圆点都能点", dlg.rb_quit.isEnabled() and dlg.rb_tray.isEnabled())
        check("没有副标题（正常状态不写字）", dlg.row_tray.sub.isVisible() is False)

        dlg.rb_quit.setChecked(True)
        dlg._on_close_mode_toggled(True)  # noqa: SLF001 - 直接走槽，避开点击
        check("选「退出程序」写进偏好", prefs.close_to_tray is False)
        check("互斥：另一个圆点自动取消", dlg.rb_tray.isChecked() is False)
        check("落盘里真的记了 False",
              '"close_to_tray": false' in prefs.path.read_text(encoding="utf-8"))

        dlg.sw_start_in_tray.set_on(False)
        dlg._on_start_in_tray_toggled(False)  # noqa: SLF001
        check("关掉「启动时最小化到托盘」写进偏好", prefs.start_in_tray is False)
        check("落盘里也记了",
              '"start_in_tray": false' in prefs.path.read_text(encoding="utf-8"))
    finally:
        dlg.close()
        dlg.deleteLater()
        app.processEvents()

    dlg2 = SettingsDialog(store, theme.LIGHT, None, account={}, tray_available=False)
    try:
        dlg2.nav.setCurrentRow(4)
        check("托盘不可用时两个圆点都点不动",
              dlg2.rb_quit.isEnabled() is False and dlg2.rb_tray.isEnabled() is False)
        check("并写明这个环境没有托盘",
              "没有系统托盘" in dlg2.row_tray.sub.text(), dlg2.row_tray.sub.text())
        check("「启动时最小化到托盘」也跟着灰",
              dlg2.sw_start_in_tray.isEnabled() is False)
    finally:
        dlg2.close()
        dlg2.deleteLater()
        app.processEvents()


def _loop_checks(app: QApplication) -> None:
    """真跑一次事件循环：关窗之后 ``exec()`` 必须自己返回。

    前面那些断言都是"清理跑过、信号发过"，证明不了进程会不会退——这套 bug 的
    表现恰恰是清理全跑完了、窗口也没了，进程却带着托盘图标赖在那里。只有真的
    进事件循环、看 ``exec`` 有没有返回，才算验到"退出"。
    """
    from mihome.ui.main_window import MainWindow

    win = MainWindow(theme.LIGHT, tray_available=True)
    win.prefs.close_to_tray = False
    win.prefs.save()
    win.show()
    timed_out = {"v": False}

    def failed_to_exit() -> None:
        timed_out["v"] = True
        app.exit(9)

    QTimer.singleShot(200, win.close)
    QTimer.singleShot(4000, failed_to_exit)
    code = app.exec()
    check("关窗后事件循环自己返回（进程真的退了）",
          code == 0 and not timed_out["v"], f"code={code}")
    check("退出后托盘图标摘了", win.tray.active is False)
    win.deleteLater()
    app.processEvents()


def main() -> int:
    app = QApplication.instance() or QApplication([])
    # 主窗口会写窗口几何 / 主题到 QSettings，跑完原样还原，别动用户日常配置
    settings = QSettings("MiHomePanel", "MiHomePanel")
    backup = {key: settings.value(key) for key in settings.allKeys()}
    was_quit_on_close = app.quitOnLastWindowClosed()
    try:
        _icon_checks()
        _controller_checks(app)
        _window_checks(app)
        _settings_checks(app)
        _loop_checks(app)
    finally:
        app.setQuitOnLastWindowClosed(was_quit_on_close)
        settings.clear()
        for key, value in backup.items():
            settings.setValue(key, value)
        settings.sync()

    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：")
        for line in FAILS:
            print("  -", line)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
