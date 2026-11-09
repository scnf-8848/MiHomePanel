# SPDX-License-Identifier: GPL-3.0-or-later
"""验证开机启动（`mihome/core/autostart.py`）。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_autostart.py``

**不碰真实启动项**：所有读写都指到 `HKCU\\Software\\MiHomePanel-selftest-<pid>\\Run`，
跑完把这个临时键整个删掉。这样验证脚本既不需要管理员，也不会把用户的"任务管理器 →
启动"页搞脏。

覆盖：命令格式（引号 + `--autostart`）、解析旧值、四种状态、heal 只在已启用时改写、
关掉之后值真的消失、失败路径抛 `AutostartError`、启动参数识别。
"""

from __future__ import annotations

import os
import sys
import winreg
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QObject  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from mihome.core import autostart  # noqa: E402

FAILS: list[str] = []

FAKE_A = r"C:\Program Files\MiHomePanel\MiHomePanel.exe"
FAKE_B = r"D:\portable\MiHomePanel\MiHomePanel.exe"
TOP = rf"Software\MiHomePanel-selftest-{os.getpid()}"
KEY = TOP + r"\Run"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAILS.append(f"{name} {detail}")


def _read_raw() -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, autostart.VALUE_NAME)
            return str(value)
    except (FileNotFoundError, OSError):
        return None


def _cleanup() -> None:
    for path in (KEY, TOP):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except (FileNotFoundError, OSError):
            pass


def main() -> int:
    _cleanup()  # 上一轮崩在中间时残留的
    try:
        _run()
    finally:
        _cleanup()

    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：")
        for line in FAILS:
            print("  -", line)
        return 1
    print("全部通过")
    return 0


def _run() -> None:
    # -- 命令的写法与解析 --------------------------------------------------
    command = autostart.command_for(FAKE_A)
    check("命令带引号（路径有空格）+ 标记参数",
          command == f'"{FAKE_A}" --autostart', command)
    check("解析带引号的命令", autostart.exe_from_command(command) == FAKE_A)
    check("解析不带引号的旧值",
          autostart.exe_from_command(r"C:\mi\MiHomePanel.exe --autostart")
          == r"C:\mi\MiHomePanel.exe")
    check("解析只有 exe 的值",
          autostart.exe_from_command(r"C:\mi\MiHomePanel.exe") == r"C:\mi\MiHomePanel.exe")
    check("空值解析成空串", autostart.exe_from_command("") == "")
    check("大小写 / 斜杠不同也算同一个路径",
          autostart._same_path(FAKE_A, FAKE_A.lower().replace("\\", "/"))  # noqa: SLF001
          is True)

    # -- 源码运行时不支持 --------------------------------------------------
    check("源码运行：current_exe 为 None", autostart.current_exe() is None)
    check("源码运行：supported 为假", autostart.supported() is False)
    check("源码运行：status 报 unsupported",
          autostart.status(root=winreg.HKEY_CURRENT_USER, key_path=KEY).state
          == autostart.STATE_UNSUPPORTED)
    try:
        autostart.enable(root=winreg.HKEY_CURRENT_USER, key_path=KEY)
        check("源码运行时 enable 抛 AutostartError", False, "没有抛")
    except autostart.AutostartError as exc:
        check("源码运行时 enable 抛 AutostartError", "打包" in str(exc), str(exc))

    where = {"root": winreg.HKEY_CURRENT_USER, "key_path": KEY}

    # -- 模拟打包态 --------------------------------------------------------
    # 源码运行永远走不到"能开"这一支，而打包后才真正用到的正是它：把
    # sys.frozen / sys.executable 临时改成 exe，测完原样还回去。
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="mihome-frozen-"))
    fake_exe = tmp / "MiHomePanel.exe"
    fake_exe.write_bytes(b"")
    saved = (getattr(sys, "frozen", None), sys.executable)
    sys.frozen = True  # type: ignore[attr-defined]
    sys.executable = str(fake_exe)
    try:
        check("打包态：supported 为真", autostart.supported() is True)
        check("打包态：current_exe 指到 exe 本身", autostart.current_exe() == fake_exe)
        wrote = autostart.enable(**where)
        check("打包态：enable 认的是 sys.executable",
              autostart.exe_from_command(wrote.command) == str(fake_exe), wrote.command)
        check("打包态：开关能开也能关",
              autostart.status(**where).state == autostart.STATE_ON
              and autostart.disable(**where).state == autostart.STATE_OFF)
    finally:
        sys.executable = saved[1]
        if saved[0] is None:
            del sys.frozen  # type: ignore[attr-defined]
        else:
            sys.frozen = saved[0]  # type: ignore[attr-defined]
        fake_exe.unlink(missing_ok=True)
        tmp.rmdir()

    # -- 空 → 开 -----------------------------------------------------------
    check("没写过时是 off", autostart.status(exe=FAKE_A, **where).state == autostart.STATE_OFF)
    info = autostart.enable(exe=FAKE_A, **where)
    check("enable 之后是 on", info.state == autostart.STATE_ON)
    check("注册表里真有这一行", _read_raw() == command, str(_read_raw()))
    check("再读一次还是 on",
          autostart.status(exe=FAKE_A, **where).state == autostart.STATE_ON)

    # -- 指向旧路径 → stale ------------------------------------------------
    stale = autostart.status(exe=FAKE_B, **where)
    check("换了程序路径就是 stale", stale.state == autostart.STATE_STALE)
    check("stale 仍算开着（开关要显示为开）", stale.is_on is True)
    check("stale 里带着旧路径", stale.target == FAKE_A, stale.target)

    healed = autostart.heal(exe=FAKE_B, **where)
    check("heal 把旧路径改成当前 exe", healed.state == autostart.STATE_ON)
    check("heal 后注册表指向新路径",
          autostart.exe_from_command(_read_raw() or "") == FAKE_B, str(_read_raw()))

    # -- 关 → 值消失 -------------------------------------------------------
    autostart.disable(**where)
    check("disable 之后是 off", autostart.status(exe=FAKE_B, **where).state == autostart.STATE_OFF)
    check("注册表里那一行被删掉了", _read_raw() is None, str(_read_raw()))
    try:
        autostart.disable(**where)
        check("重复 disable 不报错", True)
    except autostart.AutostartError as exc:
        check("重复 disable 不报错", False, str(exc))

    # -- heal 不会把没开的功能打开 -----------------------------------------
    autostart.heal(exe=FAKE_B, **where)
    check("heal 不会凭空创建启动项", _read_raw() is None, str(_read_raw()))

    # -- 启动参数 ----------------------------------------------------------
    app = QApplication.instance() or QApplication([])
    from PySide6.QtWidgets import QWidget

    from mihome.app import autostart_requested, present_window

    check("认得出 --autostart", autostart_requested(["--autostart"]) is True)
    check("普通启动不算自启", autostart_requested([]) is False)
    check("其它参数不算自启", autostart_requested(["--rounds", "2"]) is False)

    class _FakeWindow(QWidget):
        """只实现 present_window 用到的那几下：show / showMinimized / start_in_tray。"""

        def __init__(self, in_tray: bool) -> None:
            super().__init__()
            self._in_tray = in_tray

        def start_in_tray(self) -> bool:
            return self._in_tray

    hidden = _FakeWindow(True)
    present_window(hidden, True)
    check("自启 + 进得了托盘：窗口根本不显示", hidden.isVisible() is False)

    auto = _FakeWindow(False)
    present_window(auto, True)
    check("自启 + 进不了托盘：退回最小化，留一格任务栏", auto.isMinimized() is True)

    normal = _FakeWindow(False)
    present_window(normal, False)
    check("普通启动：窗口正常显示",
          normal.isVisible() is True and normal.isMinimized() is False)

    _ui_checks()
    del app


class _EmptyStore:
    """设置页只需要这几个方法；返回空列表就能把整页建出来。

    其余属性（``home_placement`` / ``camera_network`` 这些）真 Store 也是转发给
    ``prefs`` 的，这里用 ``__getattr__`` 一样转发，省得逐个补。
    """

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


class _ShowSpy(QObject):
    """记下构造期间有多少"不该出现的顶层窗口"被显示出来。

    ``QWidget`` 没有父控件时**本身就是一个顶层窗口**，这时 ``setVisible(True)``
    会当场在屏幕左上角弹一个几十像素的小窗。设置页十几行一起建，用户看到的就是
    "许多小窗口闪一下才打开设置页"（`SettingsRow.sub` 原本就是这个写法，只把
    ``setVisible`` 挪到 ``addWidget`` 之后还不够——那时布局还没挂到任何 widget 上，
    父控件仍然是 ``None``）。
    """

    def __init__(self) -> None:
        super().__init__()
        self.hits: list[str] = []

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Show and isinstance(obj, QWidget) and obj.isWindow():
            self.hits.append(f"{type(obj).__name__}#{obj.objectName()}")
        return False


def _ui_checks() -> None:
    """设置页第 5 页：分类在、开关在、源码运行时是藏起来而不是灰着。"""
    import tempfile

    from mihome.core.prefs import Prefs
    from mihome.ui import theme
    from mihome.ui.settings import SettingsDialog

    tmp = Path(tempfile.mkdtemp(prefix="mihome-autostart-"))
    app = QApplication.instance()
    spy = _ShowSpy()
    app.installEventFilter(spy)
    try:
        dlg = SettingsDialog(_EmptyStore(Prefs(tmp / "prefs.json")), theme.LIGHT, None, account={})
    finally:
        app.removeEventFilter(spy)
    check("建设置窗口时不闪多余的顶层小窗", spy.hits == [], "、".join(spy.hits[:8]))
    try:
        titles = [dlg.nav.item(i).text() for i in range(dlg.nav.count())]
        check("设置页有五个分类", dlg.nav.count() == 5, str(titles))
        check("最后一个分类是「常规」", titles[-1] == "常规", str(titles))

        dlg.nav.setCurrentRow(4)
        check("导航切到常规页", dlg.pages.currentIndex() == 4, str(dlg.pages.currentIndex()))

        # 分区标题在**卡片外面**（和外观、排序页一个格式），卡片里不再有标题、
        # 标题前也没有图标：CardTitle 这个 objectName 应该一个都不剩
        from PySide6.QtWidgets import QLabel

        page = dlg.pages.currentWidget()
        sections = [lab.text() for lab in page.findChildren(QLabel, "SectionTitle")]
        check("常规页的分区标题在卡片外", sections == ["启动设置", "关闭面板时"], str(sections))
        check("卡片里没有卡内标题",
              page.findChildren(QLabel, "CardTitle") == [], "CardTitle 还有残留")

        # 选中圆点必须是"生效绿"（和开关同色），不能是主题蓝：蓝在本项目里表示
        # "能点 / 有焦点"，和旁边打开状态的绿开关并排就成了两种强调色
        dot = dlg.rb_tray
        dot.setEnabled(True)  # 源码环境没有托盘时会置灰，这里只看颜色
        dot.setChecked(True)
        shot = dot.grab().toImage()
        scale = shot.width() / max(1, dot.width())
        # 白心在正中间，往外 7px（按缩放折算）才是那圈绿
        px = shot.pixelColor(int(shot.width() / 2 - 7 * scale), shot.height() // 2)
        check("选中圆点是绿（与开关同色），不是主题蓝",
              px.green() > px.blue() and px.green() > px.red(), px.name())
        # 源码运行时：开关留在原位但置灰（藏起来这一页会只剩一个卡片标题）
        check("源码运行时开关是灰的", dlg.sw_autostart.isEnabled() is False)
        check("副标题写明源码运行不支持",
              "源码运行" in dlg.row_autostart.sub.text(), dlg.row_autostart.sub.text())
        check("开关处于关", dlg.sw_autostart.is_on() is False)
        check("「启动时进托盘」也跟着灰（自启都用不了）",
              dlg.sw_start_in_tray.isEnabled() is False)

        # 模拟打包态：开关必须露出来（`usable` 这个判断最容易写反）
        saved = (getattr(sys, "frozen", None), sys.executable)
        fake = tmp / "MiHomePanel.exe"
        fake.write_bytes(b"")
        sys.frozen = True  # type: ignore[attr-defined]
        sys.executable = str(fake)
        try:
            dlg._refresh_autostart()  # noqa: SLF001
            check("打包态：开关可用（不再灰掉）", dlg.sw_autostart.isEnabled() is True)
            check("打包态：状态不再写「源码运行」",
                  "源码运行" not in dlg.row_autostart.sub.text(),
                  dlg.row_autostart.sub.text())
        finally:
            sys.executable = saved[1]
            if saved[0] is None:
                del sys.frozen  # type: ignore[attr-defined]
            else:
                sys.frozen = saved[0]  # type: ignore[attr-defined]
            fake.unlink(missing_ok=True)
    finally:
        dlg.close()
        dlg.deleteLater()
        for path in sorted(tmp.glob("*")):
            path.unlink()
        tmp.rmdir()


if __name__ == "__main__":
    sys.exit(main())
