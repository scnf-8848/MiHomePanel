# SPDX-License-Identifier: GPL-3.0-or-later
"""摄像头独立窗口截图（看云台方向盘的位置、大小、半透明效果）。

详情页那几张由 ``scripts/shot_camera.py`` 负责；这里只走"播放 → 开独立窗口"这条，
截两张：

* ``camera_popout_ptz.png``：常态，方向盘半透明贴在画面左下角
* ``camera_popout_ptz_hover.png``：鼠标停在方向盘上的样子（底盘会加深一档）

用法：``.venv/Scripts/python.exe -X utf8 scripts/shot_camera_popout.py``
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

os.environ.setdefault("MIHOME_NO_TRAY", "1")

from mihome.ui import theme
from mihome.ui.detail import DetailDialog
from mihome.ui.main_window import MainWindow

OUT = Path("scripts/out")
START_TIMEOUT_MS = 20000
PTZ_WAIT_MS = 5000


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(13)
    app.setFont(font)

    win = MainWindow(theme.DARK)
    win.palette_ref = theme.DARK
    win._apply_theme()
    win.resize(1360, 900)
    win.show()
    win.start()

    st: dict = {"tries": 0, "ms": 0, "dlg": None}

    def look() -> None:
        st["tries"] += 1
        if win.store.loading or not win.store.devices:
            if st["tries"] > 60:
                print("设备列表没加载出来")
                app.quit()
                return
            QTimer.singleShot(500, look)
            return
        cam = next(
            (d for d in win.store.devices.values()
             if d.online and "camera" in d.node.model.lower()),
            None,
        )
        if cam is None:
            print("没有在线摄像头，跳过")
            app.quit()
            return
        print(f"打开 {cam.name} / {cam.node.model}")
        dlg = DetailDialog(cam, win.store, win.palette_ref, win, icons=win.icon_provider)
        dlg.show()
        st["dlg"] = dlg
        QTimer.singleShot(600, lambda: dlg._camera.toggle())
        QTimer.singleShot(900, wait_playing)

    def wait_playing() -> None:
        dlg = st["dlg"]
        view = dlg._camera
        if view.state != view.PLAYING:
            st["ms"] += 400
            if st["ms"] > START_TIMEOUT_MS:
                print(f"等不到画面，当前状态 {getattr(view, 'state', '?')}")
                dlg.close()
                app.quit()
                return
            QTimer.singleShot(400, wait_playing)
            return
        # 没有真实鼠标：手动把 hover 打开，右下角那排键才会出现
        view._hover = True
        view._refresh_buttons()
        app.processEvents()
        dlg._toggle_camera_popout()
        QTimer.singleShot(600, wait_ptz)

    def wait_ptz() -> None:
        dlg = st["dlg"]
        popup = dlg._cam_popout
        if popup is None:
            print("独立窗口没建起来")
            dlg.close()
            app.quit()
            return
        pad = popup._ptz
        shown = pad is not None and pad.isVisible()
        if not shown and st["ms"] < START_TIMEOUT_MS + PTZ_WAIT_MS:
            st["ms"] += 300
            app.processEvents()
            QTimer.singleShot(300, wait_ptz)
            return
        print(
            f"方向盘：{'在' if shown else '不在'}  探测={popup._ptz_ok}  "
            f"尺寸={getattr(pad, 'diameter', '-')}  位置={pad.pos() if pad else '-'}"
        )
        view = popup._view
        view._hover = True
        view._refresh_buttons()
        app.processEvents()
        pix = popup.grab()
        pix.save(str(OUT / "camera_popout_ptz.png"))
        print(f"已保存 camera_popout_ptz.png（常态）  {pix.width()}x{pix.height()}")

        if pad is not None:
            pad._inside = True
            pad.update()
            app.processEvents()
            pix = popup.grab()
            pix.save(str(OUT / "camera_popout_ptz_hover.png"))
            print(f"已保存 camera_popout_ptz_hover.png（悬停）  {pix.width()}x{pix.height()}")

        popup.close()
        dlg.close()
        app.quit()

    QTimer.singleShot(800, look)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
