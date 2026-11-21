# SPDX-License-Identifier: GPL-3.0-or-later
"""只有圆点、没有文字的单选按钮。

设置页的行是「左边名称、右边控件」的结构，所以单选的点单独放在右边一列，
名称由 ``settings_rows.SettingsRow`` 出。Qt 自带的 ``QRadioButton`` 带着自己的
文字和焦点框，QSS 又改不出"实心圆 + 白心"这个米家的选中态（indicator 只能改
边框和背景），所以自己画二十来行。

**颜色跟着开关走**（``theme.select_dot``），不是跟 ``accent``：选中态表达的是
"这一项生效了"，和开关打开是同一件事；用主题蓝会和旁边的绿开关并排成两种强调色。

互斥交给 ``QButtonGroup``：``QAbstractButton`` 默认并不互斥——那是
``QRadioButton`` 才有的行为。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QWidget

from . import theme


class RadioDot(QAbstractButton):
    """一个可点的圆点。选中 = 实心绿 + 白心，未选中 = 一圈中性灰。"""

    #: 控件边长（比圆点大 2px，留出抗锯齿的余量，也顺手放大一点点击热区）
    SIZE = 22
    #: 圆点直径
    DOT = 20

    def __init__(
        self,
        palette: theme.Palette | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.PointingHandCursor)
        # 键盘 Tab 过来能用空格选；设 NoFocus 就没法用键盘了
        self.setFocusPolicy(Qt.StrongFocus)
        self._p = palette or theme.LIGHT
        self._hover = False

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        on = self.isChecked()
        enabled = self.isEnabled()

        # 名字不叫 accent：这个色现在和开关的绿是同一支，叫 accent 会让人以为
        # 还是主题的强调蓝（调色板里已经有同名同义的字段）
        tone = QColor(theme.select_dot(self._p) if enabled else self._p.text_mute)
        # 未选中时悬停染上同一支色，暗示"这里能点"；选中态不需要再强调
        edge = QColor(self._p.text_mute)
        if enabled and self._hover and not on:
            edge = tone

        box = QRectF(
            (self.width() - self.DOT) / 2.0,
            (self.height() - self.DOT) / 2.0,
            self.DOT,
            self.DOT,
        )
        if on:
            p.setPen(Qt.NoPen)
            p.setBrush(tone)
            p.drawEllipse(box)
            # 实心圆 + 白心：小尺寸下比"粗描边"更容易一眼看出是选中的圆
            p.setBrush(QColor("#FFFFFF"))
            inner = box.adjusted(self.DOT * 0.32, self.DOT * 0.32,
                                 -self.DOT * 0.32, -self.DOT * 0.32)
            p.drawEllipse(inner)
        else:
            p.setPen(QPen(edge, 1.6))
            p.setBrush(Qt.NoBrush)
            # 描边往里收半个笔宽，否则圆弧的抗锯齿会被控件边界切掉
            p.drawEllipse(box.adjusted(0.8, 0.8, -0.8, -0.8))
        p.end()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()


__all__ = ["RadioDot"]
