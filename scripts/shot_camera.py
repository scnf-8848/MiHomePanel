# SPDX-License-Identifier: GPL-3.0-or-later
"""摄像头详情页截图（空态 + 加载态 + 播放态 + 释放态 + 错误态）。

主程序的 ``shot_ui.py`` 截的是灯品类，不会触发取流；这条单独跑，用来检查画面卡片
的尺寸、圆角、状态文字和按钮位置。

打开详情页**不取流**，所以第一张截的是空态：底色 + 中央播放键 + 一句提示。然后模拟
点一下播放，先截加载态（「加载中…」+ 流动细条，不写秒数），等首帧到达后截播放态：
右下角的暂停键。第四张是"闲置释放"——暂停后把连接放掉，画面区变成一句提示 + 播放键
（点一下会重新取流）；第五张是错误态，用来核对"观看位已满"这类长文案有没有压到
重试按钮上。

用法：.venv/Scripts/python.exe -X utf8 scripts/shot_camera.py
"""

from __future__ import annotations

import os
import sys

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

# 这些脚本自己驱动窗口（建 → 截 → close → 退）：让托盘接管 close 的话，
# 关闭只会把窗口藏起来，脚本等的是一个不会结束的事件循环。
os.environ.setdefault("MIHOME_NO_TRAY", "1")

from mihome.ui import theme
from mihome.ui.detail import DetailDialog
from mihome.ui.main_window import MainWindow

OUT = Path("scripts/out")
START_TIMEOUT_MS = 20000


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

    tries = {"n": 0}
    waited = {"ms": 0}

    def look() -> None:
        tries["n"] += 1
        if win.store.loading or not win.store.devices:
            if tries["n"] > 60:
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

        # 必须先把回调定义出来再调度：这两个函数是 look() 的局部名，若在定义之前
        # 引用，闭包拿到的还是未绑定的局部变量，定时器一触发就 UnboundLocalError。
        def shoot_idle() -> None:
            """空态：还没点播放，画面区只有一个播放键。"""
            view = dlg._camera
            if view is None:
                print("没有画面控件")
                dlg.close()
                app.quit()
                return
            app.processEvents()
            pix = dlg.grab()
            pix.save(str(OUT / "camera_detail.png"))
            print(f"已保存 camera_detail.png（空态）  {pix.width()}x{pix.height()}")
            view.toggle()  # 点播放
            waited["ms"] = 0
            QTimer.singleShot(300, shoot_loading)
            QTimer.singleShot(400, wait_playing)

        def shoot_loading() -> None:
            """加载态：一句「加载中…」加一条流动的细条，不给秒数。"""
            view = dlg._camera
            if view.state != view.LOADING:
                return
            app.processEvents()
            pix = dlg.grab()
            pix.save(str(OUT / "camera_loading.png"))
            print(f"已保存 camera_loading.png（加载态）  {pix.width()}x{pix.height()}")

        def wait_playing() -> None:
            view = dlg._camera
            if view.state != view.PLAYING:
                waited["ms"] += 400
                if waited["ms"] > START_TIMEOUT_MS:
                    print(f"等不到画面，当前状态 {getattr(view, 'state', '?')}")
                    print(f"  控件提示：{view.toolTip()}")
                    dlg.close()
                    app.quit()
                    return
                QTimer.singleShot(400, wait_playing)
                return
            # 没有真实鼠标，手动把 hover 打开，才能截到右下角的暂停键
            view._hover = True
            view._refresh_buttons()
            app.processEvents()
            pix = dlg.grab()
            pix.save(str(OUT / "camera_playing.png"))
            print(f"已保存 camera_playing.png（播放态）  {pix.width()}x{pix.height()}")
            # 暂停 → 主动触发一次闲置释放，截"连接已释放"的样子
            view.toggle()
            view._idle.stop()
            print(f"  暂停后状态 {view.state}")
            view._release_idle()
            app.processEvents()
            print(
                f"  释放后 状态 {view.state}  残留画面 {view._image is not None}"
                f"  提示 {view._hint!r}"
            )
            pix = dlg.grab()
            pix.save(str(OUT / "camera_released.png"))
            print(f"已保存 camera_released.png（闲置释放）  {pix.width()}x{pix.height()}")
            # 错误态：长文案 + 重试按钮，最容易挤出问题，单独截一张核对
            view.show_error("画面中断：观看位可能已满（最多 3 路）")
            app.processEvents()
            pix = dlg.grab()
            pix.save(str(OUT / "camera_error_state.png"))
            print(f"已保存 camera_error_state.png（错误态）  {pix.width()}x{pix.height()}")
            dlg.close()
            app.quit()

        QTimer.singleShot(600, shoot_idle)

    QTimer.singleShot(800, look)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
