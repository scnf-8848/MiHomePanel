# SPDX-License-Identifier: GPL-3.0-or-later
"""离屏截图：启动主窗口，等数据到位后保存成 PNG。

用法：
  .venv/Scripts/python.exe -X utf8 scripts/shot.py [输出路径] [--dark] [--filter 关键词]
"""

from __future__ import annotations

import os
import sys

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

# 这些脚本自己驱动窗口（建 → 截 → close → 退）：让托盘接管 close 的话，
# 关闭只会把窗口藏起来，脚本等的是一个不会结束的事件循环。
os.environ.setdefault("MIHOME_NO_TRAY", "1")

from mihome.ui import theme  # noqa: E402
from mihome.ui.main_window import MainWindow  # noqa: E402


def main() -> int:
    args = [a for a in sys.argv[1:]]
    out = Path("scripts/out/app.png")
    if args and not args[0].startswith("--"):
        out = Path(args.pop(0))
    dark = "--dark" in args
    flt = ""
    if "--filter" in args:
        flt = args[args.index("--filter") + 1]

    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    f = QFont("Microsoft YaHei UI")
    f.setPixelSize(13)
    app.setFont(f)

    win = MainWindow(theme.DARK if dark else theme.LIGHT)
    # QSettings 里存的主题会覆盖构造参数，截图脚本要以命令行参数为准
    win.palette_ref = theme.DARK if dark else theme.LIGHT
    win._apply_theme()  # noqa: SLF001
    win.resize(1360, 900)
    win.show()
    win.start()
    if flt:
        win.search.setText(flt)

    state = {"done": 0}

    def tick() -> None:
        """等数据加载完并且布局稳定两帧后再截图，避免拍到中间态。"""
        ready = (
            not win.store.loading
            and bool(win.wall._cards)  # noqa: SLF001
            and not win.icon_provider._pending  # noqa: SLF001  等产品图下载完
        )
        if ready:
            state["done"] += 1
            if state["done"] >= 8:  # 约 1.2 秒，足够 FlowLayout 重排两次
                out.parent.mkdir(parents=True, exist_ok=True)
                pix = win.grab()
                pix.save(str(out))
                print(f"已保存 {out}  {pix.width()}x{pix.height()}")
                app.quit()
                return
        QTimer.singleShot(150, tick)

    QTimer.singleShot(600, tick)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
