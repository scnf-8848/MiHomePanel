# SPDX-License-Identifier: GPL-3.0-or-later
"""功能区里**还没实现**的页面：明确的「尚未开放」占位。

为什么要有它：侧栏「耗材」「监控」两个入口已经能点，点进去若是空白，会被当成
加载失败或程序坏了。所以各给一页说明：标题沿用侧栏入口名，中间一张卡片写清楚
现在没有、以及临时从哪里做同一件事。页面常驻在 ``MainWindow.pages`` 里，切页
只换 ``QStackedWidget`` 的索引，不重建。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

from . import theme
from .settings import dialog_qss


def _qss(p: theme.Palette) -> str:
    return dialog_qss(p) + f"""
    QFrame#PlaceholderCard {{
        /* 这是主窗口里的一页（压在渐变底上），按主窗口卡片那一档，不是弹窗面板档 */
        background: {theme.fill_qss(theme.panel_bg(p), p)};
        border: 1px solid {p.border};
        border-radius: {theme.RADIUS_CARD}px;
    }}
    QLabel#PlaceholderBadge {{
        background: {p.accent_soft}; color: {p.accent};
        border-radius: 11px; padding: 0 12px;
        font-size: 12px; font-weight: 600;
    }}
    QLabel#PlaceholderTitle {{ color: {p.text}; font-size: 16px; font-weight: 700; }}
    QLabel#PlaceholderBody {{ color: {p.text_soft}; font-size: 12px; }}
    """


class PlaceholderPage(QWidget):
    """一页「尚未开放」：大标题 + 居中说明卡片。"""

    def __init__(
        self,
        title: str,
        palette: theme.Palette,
        *,
        detail: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._p = palette
        self.setObjectName("PlaceholderPage")
        # 外边距与卡片墙 / 自动化页同一套（wall 内容边距 28，切页不跳格）
        col = QVBoxLayout(self)
        col.setContentsMargins(28, 8, 28, 24)
        col.setSpacing(10)

        head = QLabel(title, self)
        head.setObjectName("PageTitle")
        col.addWidget(head)
        col.addStretch(1)

        card = QFrame(self)
        card.setObjectName("PlaceholderCard")
        card.setFixedWidth(520)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(32, 28, 32, 30)
        inner.setSpacing(10)

        badge = QLabel("尚未开放", card)
        badge.setObjectName("PlaceholderBadge")
        badge.setAlignment(Qt.AlignCenter)
        # QSS 只给横向 padding：竖向靠固定高度，避免 padding 不进 sizeHint 被裁
        badge.setFixedHeight(22)
        inner.addWidget(badge, 0, Qt.AlignHCenter)

        msg = QLabel(f"{title}页还没有做", card)
        msg.setObjectName("PlaceholderTitle")
        msg.setAlignment(Qt.AlignCenter)
        inner.addWidget(msg)

        # 正文自己换好行、不用 wordWrap：QLabel 的 wordWrap + QSS padding 在
        # 高度上算不准，容易被布局挤成一行裁掉；卡片宽度固定，手写换行更稳。
        body = QLabel(detail, card)
        body.setObjectName("PlaceholderBody")
        body.setAlignment(Qt.AlignCenter)
        inner.addWidget(body)

        col.addWidget(card, 0, Qt.AlignHCenter)
        col.addStretch(2)
        self.setStyleSheet(_qss(palette))

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.setStyleSheet(_qss(palette))


__all__ = ["PlaceholderPage"]
