# SPDX-License-Identifier: GPL-3.0-or-later
"""看背景渐变的真实渲染（1:1，不是截图）：按键切换色板 / 新旧画法。

跑法：``.venv/Scripts/python.exe scripts/demo_backdrop.py``

不是回归脚本，是**肉眼验收工具**：背景抖动这类"看着有界线"的问题，数值判据只能证明
"窗均值贴理想值"，最终还得人眼看。``scripts/out/`` 整体被 gitignore（存截图），所以
这个文件放在 ``scripts/`` 下一起入库。

为什么不直接开主程序：要复现"斜带分界线"得用一个**色差很大**的渐变（蓝→红），
主程序的预设都是刻意做淡的。这里借真实 ``WindowFrame`` + 真实 ``theme.Surface``
画同一个窗口，键盘随时对比。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget  # noqa: E402

from mihome.ui import chrome, fonts, theme  # noqa: E402

BACKDROPS = {
    "1": ("蓝 → 红（色差放大 10 倍，30° 斜角）",
          theme.Surface("gradient", ("#1E3A8A", "#DC2626"), 30.0, "#2D3745", 0.88)),
    "2": ("夜色 · 深", theme.palette_for("night", "dark").surface),
    "3": ("暮色 · 浅", theme.palette_for("dusk", "light").surface),
    "4": ("极光 · 深", theme.palette_for("aurora", "dark").surface),
}

HINT = (
    "1 蓝→红(30°)   2 夜色·深   3 暮色·浅   4 极光·深\n"
    "空格：切换 新画法(随机取整) / 旧画法(光栅渐变，能看到台阶)\n"
    "F 全屏放大看   Q 退出"
)


class Demo(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("背景渐变的抖动对比")
        self.resize(1200, 760)
        self._key = "1"
        self._old = False
        self._frame = chrome.WindowFrame(
            self,
            resizable=True,
            fill=lambda: BACKDROPS[self._key][1].stops[0],
            border=lambda: theme.window_edge(theme.palette_for("night", "dark")),
            backdrop=lambda: BACKDROPS[self._key][1],
        )
        body = QWidget()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.title = chrome.TitleBar("背景抖动对比", theme.palette_for("night", "dark"),
                                    subtitle="Space 切换新旧画法", frame=self._frame)
        lay.addWidget(self.title)
        lay.addWidget(body)
        bl = QVBoxLayout(body)
        bl.setContentsMargins(40, 20, 40, 40)
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.label.setTextFormat(Qt.PlainText)
        self.label.setWordWrap(True)
        bl.addWidget(self.label)
        bl.addStretch(1)
        self._restyle()

    def _restyle(self) -> None:
        pal = theme.palette_for("night", "dark")
        self.label.setStyleSheet(
            f"color: {pal.text_soft}; background: transparent;"
            f"font-size: 13px; line-height: 170%;")
        self.label.setText(self._state())
        self.setWindowTitle(self._state().splitlines()[0])

    def _state(self) -> str:
        name = BACKDROPS[self._key][0]
        mode = "旧画法：光栅渐变（有台阶）" if self._old else "新画法：随机取整抖动"
        return f"{name}   |   {mode}\n\n{HINT}"

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key_Space:
            self._old = not self._old
        elif key in (Qt.Key_F, Qt.Key_F11):
            self.showNormal() if self.isFullScreen() else self.showFullScreen()
        elif key in (Qt.Key_Q, Qt.Key_Escape):
            self.close()
            return
        elif event.text() in BACKDROPS:
            self._key = event.text()
        else:
            return
        # 旧画法 = 不给抖动图，退回改动前那条"直接用 QLinearGradient 铺"的路
        chrome._backdrop_pixmap = _no_shot if self._old else _real_shot
        chrome._backdrop_cache.clear()
        self._frame.win.update()
        self._restyle()


_real_shot = chrome._backdrop_pixmap


def _no_shot(*_args, **_kwargs):
    return None

if __name__ == "__main__":
    fonts.prepare_engine()
    app = QApplication(sys.argv)
    fonts.apply_to(app)
    win = Demo()
    win.show()
    print("背景抖动演示：", " / ".join(v[0] for v in BACKDROPS.values()))
    sys.exit(app.exec())
