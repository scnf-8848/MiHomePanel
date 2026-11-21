# SPDX-License-Identifier: GPL-3.0-or-later
"""右键菜单。

``QMenu`` 是独立的一层窗口（``Qt.Popup``），默认带系统边框和一层不透明的实心底，
所以 QSS 里的 ``border-radius`` 只会画出圆角**图案**，四角外面还是那块矩形底色——
看起来就是"圆角背后还有方块"。

要真正圆角，必须让这一层自己透明：``WA_TranslucentBackground`` + 去边框 + 去系统阴影。
之后 QSS 的圆角才落在透明像素上，四个角就真的透出下面的东西了。

样式也在这里自己写，不依赖 ``app_qss``：菜单可能在任意窗口里弹出，父窗口的样式表
未必带着这一份。

**子菜单是另一层原生窗口**。用 ``QMenu.addMenu("标题")`` 让 Qt 自己建的话，
那一层既没有 ``WA_TranslucentBackground`` 也没有去边框，圆角背后又会冒出方块底
——和主菜单当初一模一样的坑。所以「卡片样式」要用 ``submenu()`` 显式建，
它返回的是同一个 ``Menu`` 类。

勾选用自绘的 QPainterPath ✓ 而不是 ``QAction.setCheckable(True)``：后者画的是
系统 check indicator，深色主题下是一块带方底的默认图案，跟这套圆角菜单不搭；
也不用 "✓" 字符，中文黑体缺字形会变豆腐块。未选中项放一枚**同尺寸透明图标**，
文字左边缘才不会参差不齐。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QWidget

from . import theme


def menu_qss(p: theme.Palette) -> str:
    return f"""
    QMenu {{
        background: {p.card};
        border: 1px solid {p.border};
        border-radius: {theme.RADIUS_SMALL}px;
        padding: 6px;
    }}
    QMenu::item {{
        padding: 7px 20px 7px 16px;
        border-radius: 7px;
        font-size: 13px;
        color: {p.text};
    }}
    QMenu::item:selected {{
        background: {p.accent_soft};
        color: {p.text};
    }}
    QMenu::item:disabled {{ color: {p.text_mute}; }}
    QMenu::separator {{
        height: 1px;
        background: {p.border};
        margin: 5px 10px;
    }}
    QMenu::icon {{ padding-left: 8px; }}
    """


class Menu(QMenu):
    """圆角右键菜单。用法和 ``QMenu`` 一样，只是多要一个 palette。"""

    def __init__(self, parent: QWidget | None, palette: theme.Palette) -> None:
        super().__init__(parent)
        self._p = palette
        self.setWindowFlags(
            self.windowFlags() | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet(menu_qss(palette))

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.setStyleSheet(menu_qss(palette))


# ---------------------------------------------------------------------------
# 单选项：自绘勾选图标 + 子菜单
# ---------------------------------------------------------------------------


def choice_icon(p: theme.Palette, checked: bool, size: int = 16) -> QIcon:
    """一枚 accent 色的 ✓；未选中是同样尺寸的透明图，用来占位对齐。"""
    app = QApplication.instance()
    # PySide6 的 QApplication 实例只有 devicePixelRatio()（没有 F 后缀那个）；
    # 两种都试一下，别因为版本差异让整个菜单弹不出来。
    getter = getattr(app, "devicePixelRatioF", None) or getattr(app, "devicePixelRatio", None)
    dpr = float(getter()) if getter is not None else 1.0
    pix = QPixmap(round(size * dpr), round(size * dpr))
    pix.setDevicePixelRatio(dpr)
    pix.fill(Qt.transparent)
    if checked:
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor(p.accent), 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        path = QPainterPath()
        path.moveTo(3.5, 8.5)
        path.lineTo(6.8, 12.0)
        path.lineTo(12.5, 4.5)
        painter.drawPath(path)
        painter.end()
    return QIcon(pix)


def add_choice(
    menu: QMenu,
    text: str,
    data: object,
    current: object,
    palette: theme.Palette,
) -> QAction:
    """往菜单里加一条带勾选态的单选项；``data`` 原样塞进 ``QAction.data()``。"""
    act = QAction(text, menu)
    act.setData(data)
    act.setIcon(choice_icon(palette, data == current))
    act.setIconVisibleInMenu(True)
    menu.addAction(act)
    return act


def submenu(parent: QMenu, title: str, palette: theme.Palette) -> Menu:
    """建一层**同样透明**的子菜单。别用 ``parent.addMenu(str)``。"""
    sub = Menu(parent, palette)
    sub.setTitle(title)
    parent.addMenu(sub)
    return sub


__all__ = ["Menu", "add_choice", "choice_icon", "menu_qss", "submenu"]
