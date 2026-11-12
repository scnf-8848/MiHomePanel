# SPDX-License-Identifier: GPL-3.0-or-later
"""统一样式的确认框。

为什么不用 ``QMessageBox``：它带系统标题栏、按钮是系统样式，和我们这套无边框 +
自绘按钮的窗口放在一起像两个程序拼起来的。这个文件只需要"一句话 + 两个按钮"，
自己搭反而更省事，也能保证危险操作（退出登录）的按钮是红的。

调用方只需要：

    if confirm(self, "退出登录", "……", palette, ok_text="退出登录", danger=True):
        ...
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from . import theme
# 焦点策略与次要按钮样式都是四个自动化窗口 + 这里共用的（理由见各自 docstring）
from .automation_style import ghost_qss, tab_focus_only
from .chrome import TitleBar, WindowFrame, exec_with_dim_click, surface_bg, surface_border


def _qss(p: theme.Palette, danger: bool) -> str:
    ok = "#E5484D" if danger else theme.ACCENT_GREEN
    radius = theme.RADIUS_BUTTON_SOFT
    return (
        "QDialog{background:transparent;}"
        f"QLabel{{color:{p.text};}}"
        # 「圆角矩形」不是胶囊：40px 高的按钮给 20px 圆角就成胶囊了（用户 11-13 定）
        # padding 11/22 + 无边框，是和 ``ghost_qss`` 里 1px 边框 + 10/21 对齐的
        # 外框尺寸——两半按钮看起来一样大（用户 11-13 报"尺寸没统一"）。
        f"QPushButton{{border:none;border-radius:{radius}px;"
        f"padding:11px 22px;font-size:13px;}}"
        f"QPushButton#Primary{{background:{ok};color:#FFFFFF;font-weight:600;}}"
        f"QPushButton#Primary:hover{{background:{theme.blend(ok, '#FFFFFF', 0.12)};}}"
        f"QPushButton#Primary:pressed{{background:{theme.blend(ok, p.bg, 0.18)};}}"
        f"QPushButton#Primary:focus{{background:{theme.blend(ok, '#FFFFFF', 0.22)};}}"
    ) + ghost_qss(p)


class ConfirmDialog(QDialog):
    def __init__(
        self,
        title: str,
        text: str,
        palette: theme.Palette,
        parent: QWidget | None = None,
        *,
        ok_text: str = "确定",
        cancel_text: str = "取消",
        danger: bool = False,
    ) -> None:
        super().__init__(parent)
        self._p = palette
        self.setWindowTitle(title)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self._frame = WindowFrame(self, resizable=False,
                                  fill=lambda: surface_bg(palette),
                                  border=lambda: surface_border(palette))
        self.setStyleSheet(_qss(palette, danger))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(TitleBar(title, palette, buttons=("close",), variant="dialog"))

        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(26, 20, 26, 20)
        col.setSpacing(18)
        outer.addWidget(body)

        msg = QLabel(text)
        msg.setWordWrap(True)
        msg.setStyleSheet(f"color:{palette.text_soft}; font-size:13px;")
        msg.setMinimumWidth(320)
        col.addWidget(msg)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch(1)
        if cancel_text:
            cancel = QPushButton(cancel_text)
            cancel.setObjectName("Ghost")
            cancel.setCursor(Qt.PointingHandCursor)
            cancel.clicked.connect(self.reject)
            row.addWidget(cancel)
        ok = QPushButton(ok_text)
        ok.setObjectName("Primary")
        ok.setCursor(Qt.PointingHandCursor)
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        row.addWidget(ok)
        col.addLayout(row)

        self.adjustSize()
        self.setFixedWidth(max(400, self.sizeHint().width()))
        tab_focus_only(self)


def confirm(
    parent: QWidget | None,
    title: str,
    text: str,
    palette: theme.Palette,
    *,
    ok_text: str = "确定",
    cancel_text: str = "取消",
    danger: bool = False,
) -> bool:
    """一句话 + 两个按钮。返回 True 表示用户点了确认。"""
    dlg = ConfirmDialog(
        title, text, palette, parent, ok_text=ok_text, cancel_text=cancel_text, danger=danger
    )
    return exec_with_dim_click(dlg) == QDialog.Accepted


def notice(
    parent: QWidget | None,
    title: str,
    text: str,
    palette: theme.Palette,
    *,
    ok_text: str = "知道了",
) -> None:
    """只有"知道了"一个按钮的提示框，用于"做了一半没做成"的情况。"""
    dlg = ConfirmDialog(title, text, palette, parent, ok_text=ok_text, cancel_text="")
    exec_with_dim_click(dlg)


__all__ = ["ConfirmDialog", "confirm", "notice"]
