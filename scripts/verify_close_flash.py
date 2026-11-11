# SPDX-License-Identifier: GPL-3.0-or-later
"""验证"关掉子窗口"这一下不再整墙重建、也不重复应用主题。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_close_flash.py``

## 这条回归是因为什么（2026-11-12 用户报）

关设置页时主窗口闪一下，截图中卡片全都变矮、只剩标题，body 是空的。原因不是
渲染，是**关窗后又把主题应用了一遍**：

``open_settings`` 在 ``_run_dialog`` 之后无条件调 ``_apply_theme()``，而
``_apply_theme`` 里有 ``wall.rebuild_palette``——那会把整墙卡片拆掉重造（它自己的
注释就写着"卡片是自绘的，换色靠整墙重建"）。于是在三处"整窗失效"（弹窗销毁 /
点击层隐藏 / 压暗蒙版隐藏）之上又叠了一次全量重建，中间的帧就是"父控件画完、
子控件还没画"。

设置页里点主题时 ``theme_selected`` → ``set_theme`` 已经就地应用过了，关窗那次
是纯重复：什么都没改也照样重建一次。

## 这里断言什么

1. 什么都没改直接关窗：``_apply_theme`` 与 ``wall.rebuild_palette`` 都是 **0 次**；
2. 设置页里改主题：**恰好 1 次**（由 ``set_theme`` 触发，不是关窗那次）；
3. 关窗后主窗口的更新已恢复（``WA_UpdatesDisabled`` 为假）——``_run_dialog`` 用
   关更新把积压失效合成一次 paint，忘了放开就整窗冻结，比闪烁更糟；
4. 压暗蒙版确实撤掉了。

真正的"看不看得见闪"只能在真机上看，离屏 QPA 不做逐段提交，这里守的是**根因**。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 主窗口会建 Prefs，别让它落到真实用户目录
_TMP = Path(tempfile.mkdtemp(prefix="mihome-flash-"))
os.environ["MIHOME_DATA_DIR"] = str(_TMP / "data")

from PySide6.QtCore import Qt, Signal  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from mihome.ui import settings as settings_mod  # noqa: E402
from mihome.ui import theme  # noqa: E402
from mihome.ui.main_window import MainWindow  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAILS.append(name)


def check_eq(name: str, got, want) -> None:
    check(name, got == want, f"实际 {got!r}，应为 {want!r}")


class FakeSettings(QDialog):
    """替身：不画界面、执行期间可以"点一下主题"，然后立刻关。"""

    theme_selected = Signal(str)
    account_action = Signal(str)

    #: 每次 exec 时要做的事（由测试用例挂上去）
    during_exec = None

    def __init__(self, *_args, **_kwargs) -> None:
        super().__init__(_kwargs.get("parent"))
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)

    def exec(self) -> int:  # noqa: A003
        if FakeSettings.during_exec is not None:
            FakeSettings.during_exec(self)
        return QDialog.Rejected


# ---------------------------------------------------------------------------
def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(13)
    app.setFont(font)

    win = MainWindow(theme.DARK, tray_available=False)
    # 别把测试写进用户真实的 QSettings（窗口几何 / 当前家庭 / 主题）
    win._save_settings = lambda: None  # type: ignore[method-assign]  # noqa: SLF001
    win.palette_ref = theme.DARK
    win.resize(1200, 800)
    win.show()
    app.processEvents()

    calls = {"apply": 0, "rebuild": 0}

    orig_apply = win._apply_theme

    def spy_apply() -> None:
        calls["apply"] += 1
        orig_apply()

    orig_rebuild = win.wall.rebuild_palette

    def spy_rebuild(pal) -> None:
        calls["rebuild"] += 1
        orig_rebuild(pal)

    win._apply_theme = spy_apply  # type: ignore[method-assign]  # noqa: SLF001
    win.wall.rebuild_palette = spy_rebuild  # type: ignore[method-assign]  # noqa: SLF001

    real_dialog = settings_mod.SettingsDialog
    settings_mod.SettingsDialog = FakeSettings  # type: ignore[assignment]
    try:
        # -- 1) 什么都没改，直接关 -----------------------------------------
        calls.update(apply=0, rebuild=0)
        FakeSettings.during_exec = None
        win.open_settings()
        app.processEvents()
        check_eq("直接关窗不重新应用主题", calls["apply"], 0)
        check_eq("直接关窗不整墙重建", calls["rebuild"], 0)
        check("关窗后主窗口更新已放开",
              not win.testAttribute(Qt.WA_UpdatesDisabled), "")
        check_eq("关窗后压暗蒙版已撤", win._dim.isVisibleTo(win), False)  # noqa: SLF001

        # -- 2) 设置页里点主题：只该应用一次 --------------------------------
        calls.update(apply=0, rebuild=0)

        def switch_theme(dlg) -> None:
            dlg.theme_selected.emit("light")

        FakeSettings.during_exec = switch_theme
        win.open_settings()
        app.processEvents()
        check_eq("设置页里改主题：应用 1 次（不是关窗那次再来一次）", calls["apply"], 1)
        check_eq("设置页里改主题：整墙重建 1 次", calls["rebuild"], 1)
        check("改完主题，主窗口当前是浅色", win.palette_ref is theme.LIGHT, "")

        # -- 3) 再关一次：这次什么都没改，也不该重建 ------------------------
        calls.update(apply=0, rebuild=0)
        FakeSettings.during_exec = None
        win.open_settings()
        app.processEvents()
        check_eq("改过主题之后再关一次，仍然不重建", calls["apply"], 0)
        check_eq("改过主题之后再关一次，仍然不重建（墙）", calls["rebuild"], 0)
    finally:
        settings_mod.SettingsDialog = real_dialog  # type: ignore[assignment]
        win._apply_theme = orig_apply  # type: ignore[method-assign]  # noqa: SLF001
        win.wall.rebuild_palette = orig_rebuild  # type: ignore[method-assign]  # noqa: SLF001
        win.close()

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败：" + "、".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
