# SPDX-License-Identifier: GPL-3.0-or-later
"""设置页的骨架：一行标题 + 圆角卡里放具体控件（照 QQ 设置的排版）。

四个页面的内容完全不同（主题预览 / 两个下拉框 / 两棵树 / 账号信息），但骨架是同一个：
**分区标题 → 圆角卡 → 卡内一行行控件，行间一条分隔线**。以前每页各摆各的
``QVBoxLayout``，结果排序页有卡片、隐藏页是裸树、外观页是裸控件、账号页是一张
信息卡——四个页面放在一起不像同一个程序。

这里只提供三个小部件，颜色一律交给 ``settings._dialog_qss`` 里的 objectName 规则：

* :class:`SettingsCard` —— 圆角卡，``add_row`` 加带标题的行，``add_unit`` 加整行铺满
  的控件（主题预览、树、标签栏）；
* :class:`SettingsRow` —— 左边标题（可带一行灰色说明），右边控件；
* :func:`section_header` / :func:`link_button` —— 卡片上面那一行"分区标题 + 右侧文字按钮"。

**为什么颜色不写在这里**：设置窗口切主题是重新 ``setStyleSheet`` 整张表（见
``SettingsDialog._apply_palette``），QSS 里的规则会跟着重算；写在 Python 里的
``setStyleSheet("color:...")`` 就得每个控件都手动重刷一遍，漏一个就变成上一个主题的色。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

#: 一行的最小高度。QQ 的设置行在 52 上下：一行标题 + 一行说明也塞得下。
#: 名字里带 MIN 是为了别和 ``ui/tree.py`` 的行高 ``ROW_H`` 撞——那是树行（32）。
ROW_MIN_H = 52


class SettingsRow(QWidget):
    """一行设置：左标题（可选说明），右控件。"""

    def __init__(
        self,
        title: str,
        control: QWidget | None = None,
        *,
        sub: str = "",
        expand: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 6)
        lay.setSpacing(16)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        self.title = QLabel(title)
        self.title.setObjectName("RowTitle")
        col.addWidget(self.title)
        self.sub: QLabel | None = None
        if sub:
            self.sub = QLabel(sub)
            self.sub.setObjectName("RowSub")
            self.sub.setWordWrap(True)
            col.addWidget(self.sub)

        # expand 的行（比如"凭据文件"这种长文本）让控件吃掉右边剩余宽度，
        # 文字自己右对齐并打省略号；否则控件只占自己的 sizeHint，贴着右边。
        lay.addLayout(col, 0 if expand else 1)
        if control is not None:
            if expand:
                # **不能**再传对齐方式：addWidget 一旦带 alignment，布局就按 sizeHint
                # 摆放而不是铺满单元格；ElidedLabel 的 sizeHint 被 Ignored 策略压成
                # 十几个像素，整行值会"消失"。右对齐交给控件自己的 alignment。
                lay.addWidget(control, 1)
                if isinstance(control, QLabel):
                    control.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            else:
                lay.addWidget(control, 0, Qt.AlignVCenter)
        self.setMinimumHeight(ROW_MIN_H)


def _divider(parent: QWidget) -> QFrame:
    line = QFrame(parent)
    line.setObjectName("RowDivider")
    line.setFrameShape(QFrame.NoFrame)
    line.setFixedHeight(1)
    return line


class SettingsCard(QFrame):
    """一块圆角卡，里面按行放控件；行与行之间自动补一条分隔线。

    分隔线是**布局里的一个 1px 子控件**而不是 ``border-top``：QSS 的 border 只会
    画在控件自己的边上，卡内部分行得靠自己插线；插线还能让它跟着卡的左右内边距
    自动缩进——QQ 那几条线也不是顶到卡片边缘的。
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        pad: tuple[int, int, int, int] = (16, 6, 16, 6),
        spacing: int = 0,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("SectionCard")
        self.setFrameShape(QFrame.NoFrame)
        self._col = QVBoxLayout(self)
        self._col.setContentsMargins(*pad)
        self._col.setSpacing(spacing)
        self._rows = 0

    def add_row(
        self,
        title: str,
        control: QWidget | None = None,
        *,
        sub: str = "",
        expand: bool = False,
        stretch: int = 0,
    ) -> SettingsRow:
        self._separate()
        row = SettingsRow(title, control, sub=sub, expand=expand)
        self._col.addWidget(row, stretch)
        return row

    def add_unit(self, widget: QWidget, *, stretch: int = 0) -> QWidget:
        """整行铺满的控件：主题预览、树、家庭标签预览。不加左侧标题。"""
        self._separate()
        self._col.addWidget(widget, stretch)
        return widget

    def _separate(self) -> None:
        if self._rows:
            self._col.addWidget(_divider(self))
        self._rows += 1


def section_header(title: str) -> QHBoxLayout:
    """卡片上方那一行分区标题。返回的布局末尾已经加了 stretch，
    调用方接着 ``addWidget`` 就会靠右（放"恢复默认"这类文字按钮）。"""
    row = QHBoxLayout()
    row.setContentsMargins(4, 0, 4, 0)
    row.setSpacing(8)
    label = QLabel(title)
    label.setObjectName("SectionTitle")
    row.addWidget(label)
    row.addStretch(1)
    return row


def link_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setObjectName("Link")
    btn.setCursor(Qt.PointingHandCursor)
    return btn


__all__ = ["ROW_MIN_H", "SettingsCard", "SettingsRow", "link_button", "section_header"]
