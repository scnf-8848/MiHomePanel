# SPDX-License-Identifier: GPL-3.0-or-later
"""对几个交互态单独截图，用来核对细节：

* 详情页底部"数据更新于 N 秒前"
* 设备离线时详情页控件禁用

用法：``.venv/Scripts/python.exe -X utf8 scripts/shot_states.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mihome.ui import theme  # noqa: E402
from mihome.ui.detail import DetailDialog  # noqa: E402
from mihome.ui.main_window import MainWindow  # noqa: E402

OUT = Path("scripts/out")


def main() -> int:
    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    f = QFont("Microsoft YaHei UI")
    f.setPixelSize(13)
    app.setFont(f)

    win = MainWindow(theme.LIGHT)
    win.palette_ref = theme.LIGHT
    win._apply_theme()  # noqa: SLF001
    win.resize(1360, 900)
    win.show()
    win.start()

    state = {"ticks": 0, "dialogs": []}

    def step() -> None:
        ready = not win.store.loading and bool(win.wall._cards)  # noqa: SLF001
        if not ready:
            QTimer.singleShot(150, step)
            return
        state["ticks"] += 1
        if state["ticks"] < 6:
            QTimer.singleShot(150, step)
            return

        # 1) 详情页：找一台控件最多的设备
        OUT.mkdir(parents=True, exist_ok=True)
        target = max(win.store.devices.values(), key=lambda d: len(d.card.controls))
        dlg = DetailDialog(target, win.store, win.palette_ref, win, icons=win.icon_provider)
        dlg.show()
        state["dialogs"].append(dlg)

        def shoot_detail() -> None:
            dlg.grab().save(str(OUT / "detail_footer.png"))
            print(f"已保存 {OUT / 'detail_footer.png'}（{target.name}）")
            dlg.close()

            # 3) 离线态：把同一台设备标成离线再打开
            target.node.online = False
            dlg2 = DetailDialog(target, win.store, win.palette_ref, win, icons=win.icon_provider)
            dlg2.show()
            state["dialogs"].append(dlg2)

            def shoot_offline() -> None:
                dlg2.grab().save(str(OUT / "detail_offline.png"))
                print(f"已保存 {OUT / 'detail_offline.png'}")
                dlg2.close()
                target.node.online = True
                app.quit()

            QTimer.singleShot(1400, shoot_offline)

        QTimer.singleShot(1400, shoot_detail)

    QTimer.singleShot(600, step)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
