# SPDX-License-Identifier: GPL-3.0-or-later
"""程序入口。"""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from .ui import theme
from .ui.main_window import MainWindow


def main() -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("米家控制台")
    app.setApplicationDisplayName("米家控制台")
    app.setOrganizationName("MiHomePanel")
    app.setStyle("Fusion")
    # 系统可能是深色模式，但界面有自己的两套配色，先把调色板盖掉
    theme.apply_to_app(theme.LIGHT)

    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(13)
    app.setFont(font)

    window = MainWindow(theme.LIGHT)
    window.show()
    window.start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
