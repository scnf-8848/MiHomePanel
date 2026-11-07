# SPDX-License-Identifier: GPL-3.0-or-later
"""详情页截图。

用法：
  .venv/Scripts/python.exe -X utf8 scripts/shot_detail.py [关键词] [--dark]
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


def main() -> int:
    args = sys.argv[1:]
    keyword = next((a for a in args if not a.startswith("--")), "")
    dark = "--dark" in args
    out = Path("scripts/out/detail.png")

    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    f = QFont("Microsoft YaHei UI")
    f.setPixelSize(13)
    app.setFont(f)

    pal = theme.DARK if dark else theme.LIGHT
    win = MainWindow(pal)
    win.resize(1360, 900)
    win.show()
    win.start()

    def open_detail() -> None:
        store = win.store
        target = None
        for st in store.devices.values():
            if not st.node.online or st.spec is None:
                continue
            if keyword and keyword.lower() not in st.name.lower():
                continue
            if not keyword and not (st.card.controls or st.card.gangs):
                continue
            target = st
            break
        if target is None:
            print("没找到匹配设备")
            app.quit()
            return
        print(f"详情页: {target.name}  控件={len(target.card.controls)} 键={len(target.card.gangs)} 更多={len(target.card.advanced)}")
        dlg = DetailDialog(target, store, pal, win, icons=win.icon_provider)
        dlg.show()

        def grab() -> None:
            out.parent.mkdir(parents=True, exist_ok=True)
            pix = dlg.grab()
            pix.save(str(out))
            print(f"已保存 {out}  {pix.width()}x{pix.height()}")
            app.quit()

        QTimer.singleShot(2500, grab)

    QTimer.singleShot(6500, open_detail)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
