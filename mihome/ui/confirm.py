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
from .chrome import (
    SHADOW_PAD,
    TitleBar,
    WindowFrame,
    exec_with_dim_click,
    shadow_alpha,
    surface_bg,
    surface_border,
)

#: 两个按钮的外框高度。**必须写死**（2026-11-18 用户截图）：QSS 的上下 padding
#: 会和字体行高相加，离屏测试用的字体行高 13px → 按钮 35 高，看着正常；真机
#: 微软雅黑 13px 行高接近 32 → 按钮被撑到 54 高、110 宽，成了两颗方块。同尺寸的
#: 耗材弹窗按钮是 ``min-height:32px + padding:0 16px``（不受行高影响），这里是
#: 同一个数，顺带和详情页 / 设置页的按钮对齐。
BTN_H = 32


def _qss(p: theme.Palette, danger: bool) -> str:
    ok = "#E5484D" if danger else theme.ACCENT_GREEN
    radius = theme.RADIUS_BUTTON_SOFT
    return (
        "QDialog{background:transparent;}"
        f"QLabel{{color:{p.text};}}"
        # 「圆角矩形」不是胶囊：32px 高的按钮给 10px 圆角（用户 11-13 定）
        # 高度由代码里的 ``setFixedHeight(BTN_H)`` 定，这里只管左右内边距——
        # 上下 padding 一旦非 0，真机字体的行高会把它加进高度里。
        f"QPushButton{{border:none;border-radius:{radius}px;"
        f"padding:0 22px;font-size:13px;}}"
        f"QPushButton#Primary{{background:{ok};color:#FFFFFF;font-weight:600;}}"
        f"QPushButton#Primary:hover{{background:{theme.blend(ok, '#FFFFFF', 0.12)};}}"
        f"QPushButton#Primary:pressed{{background:{theme.blend(ok, p.bg, 0.18)};}}"
        f"QPushButton#Primary:focus{{background:{theme.blend(ok, '#FFFFFF', 0.22)};}}"
    ) + ghost_qss(p) + (
        # ``ghost_qss`` 里带 ``padding: 10px 21px``（它的用途是"和实心主按钮用
        # 内边距对齐"），这里要覆盖掉上下那 10px。QSS 里 ID 选择器比类型选择器
        # 更具体，所以不能靠上面那条通用 ``QPushButton`` 规则，必须再写一遍 #Ghost。
        "QPushButton#Ghost{padding:0 21px;}"
    )


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
        # 浮窗：圆角 18（不是主窗口的 12）+ 外圈阴影留白，见 ``chrome.SHADOW_PAD``
        self._frame = WindowFrame(self, resizable=False,
                                  radius=theme.RADIUS_CARD,
                                  shadow=SHADOW_PAD,
                                  shadow_alpha=shadow_alpha(palette),
                                  fill=lambda: surface_bg(palette),
                                  border=lambda: surface_border(palette))
        self.setStyleSheet(_qss(palette, danger))

        # 内容挂 ``body``（= 可见面板），别挂窗口：窗口比面板大一圈
        outer = QVBoxLayout(self._frame.body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(TitleBar(title, palette, buttons=("close",),
                                 variant="dialog", frame=self._frame))

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
            cancel.setFixedHeight(BTN_H)
            cancel.clicked.connect(self.reject)
            row.addWidget(cancel)
        ok = QPushButton(ok_text)
        ok.setObjectName("Primary")
        ok.setCursor(Qt.PointingHandCursor)
        ok.setFixedHeight(BTN_H)
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        row.addWidget(ok)
        col.addLayout(row)

        # 窗口没有自己的布局了（挂在 body 上），``adjustSize()`` 算不出东西，
        # 只能按面板的 sizeHint 显式加回两圈留白。
        outer.activate()
        hint = self._frame.body.sizeHint()
        pad2 = 2 * SHADOW_PAD
        self.resize(max(400, hint.width()) + pad2, hint.height() + pad2)
        self.setFixedWidth(self.width())
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
