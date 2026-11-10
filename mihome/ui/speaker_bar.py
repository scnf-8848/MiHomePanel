# SPDX-License-Identifier: GPL-3.0-or-later
"""顶栏音箱指令条：左边选音箱，右边输指令，回车发出。

位置就在主界面卡片墙上方那条横带上——**和顶部警告条浮层是同一条带**。警告条是
不进布局的浮层，以后出现时会正好压在输入条上，这是 2026-11-11 定下的预期行为，
不去调解层级（``MainWindow._place_alert`` 的边距就是为了让两者同宽同高）。

几个刻意的选择
--------------
1. **整条自己画底**：48px 高的圆角条如果交给 QSS，圆角内侧那圈描边在深色主题下
   会发灰；自己画才能把 1px 描边压在半像素上，和卡片一样干净。圆角取
   ``RADIUS_SMALL``(12) 而不是半高(24)：警告条是 R=10 的圆角矩形，两者叠在一起时
   轮廓要能重合，做成胶囊就会露出四个缺口。
2. **左边那块是自绘的小控件**（``_Picker``），不是 ``QComboBox``：下拉要放产品图、
   要把离线项压暗且点不动，还得避开 combo 在 Windows 上那堆原生 popup 的坑
   （见 ``ui/combo.py`` 顶部）。
3. **下拉不是菜单，是一块横向设备面板**（``_Panel``，2026-11-11 按参考图改）：
   图标 + 名字横排，像启动器一样一眼认得出是哪台；文字列表十行里认设备太慢。
   图标 56px——产品图是"认设备"的主要线索，缩小了反而得靠读名字。
   面板左下角放「静默执行」（见下一条）。
4. **「静默」是全局偏好，不是某台音箱的属性**，但它只影响"这一条怎么发"，所以
   跟设备选择放在同一个面板里、贴着左下角，而不是塞进设置页——半夜想安静一下
   要翻两层设置，那这个开关就白做了。默认**关**（米家实测：开着问「现在几点」
   音箱一声不出，用户会以为发失败了）。状态存 ``Prefs.speaker_silent``。
   **视觉上刻意做弱**：11px 弱色文字 + 16px 勾选框（``_QuietCheck``），不用设置页
   那颗 44×24 的绿色胶囊——它只是修饰语，不该比设备方块更抢眼。
5. **面板是独立顶层窗口**（``Qt.Popup``），和 ``ui/menu.py`` 一样必须自己透明：
   否则圆角外面还有一块方底。它不进布局，所以**条高 48 与警告条几何一个字不改**。
6. **不清洗就不发**：清洗规则全在 ``core/speaker.py``，这里只负责把结果讲给用户
   （剔除字符 / 超长截断都给 toast），绝不静默改掉指令内容。
7. **发出即清空输入框**，不等云端返回；也**不做防连发**——往返几百毫秒，用户敲下
   一条至少要几秒，2026-11-11 定案，别再当漏项翻出来。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QSizePolicy, QVBoxLayout, QWidget

from ..core import speaker
from . import theme, tooltip
from .widgets import IconProvider

#: 条高。与 ``AlertBar.H`` 一致：警告条浮层要正好压住它。
H = 48
#: 圆角。见模块注释第 1 条。
R = theme.RADIUS_SMALL
#: 左侧选音箱那块的内高（条内留 8px 上下）
PICKER_H = 32
ICON = 20
#: 名字最长画多宽，超出按省略号收。200px 够放下「Xiaomi 智能家庭屏 6」这种默认名，
#: 再长（用户自己起的长名字）就让它省略——输入框比名字重要，不能被挤没
NAME_MAX = 200

# -- 面板尺寸 ---------------------------------------------------------------
#: 设备方块：上内边距 10 + 图标 56 + 间隙 8 + 名字 18
#: 2026-11-11 从 44px 图标放大到 56px——面板是"认设备"用的，产品图比名字好认，
#: 原来那档在一排里像一排小徽标，指过去还得读文字。
TILE_ICON = 56
TILE_PAD = 10
TILE_NAME_H = 18
TILE_H = TILE_PAD + TILE_ICON + 8 + TILE_NAME_H
#: 方块宽度按名字算，但夹在这个区间里：太窄放不下图标，太宽一排几台就横出屏幕
TILE_MIN_W = 88
TILE_MAX_W = 120
#: 方块自己的圆角（比面板小一档，里面套着才不打架）
TILE_R = 10
#: 面板内边距与元素间距
PANEL_PAD = 16
PANEL_GAP = 12
PANEL_R = theme.RADIUS_CARD
#: 设备方块之间的间距
TILE_GAP = 6


def _selected_bg(p: theme.Palette) -> str:
    """面板里"当前这台"的底色。

    不能直接用 ``theme.panel_row_selected``：那个是相对分区卡（``panel_bg``）算的，
    而面板压在 ``popup_bg`` 上（深色下更亮一档），照抄会变成在面板里挖个洞。
    深色改成在 ``popup_bg`` 上掺两成强调色，浅色仍用 ``accent_soft``。
    """
    if not p.is_dark:
        return p.accent_soft
    return theme.blend(theme.popup_bg(p), p.accent, 0.20)


class _Picker(QWidget):
    """「图标 + 名字 + 小三角」。点击弹面板，自己不持有列表。"""

    clicked = Signal()

    def __init__(self, palette: theme.Palette, icons: IconProvider, parent: QWidget) -> None:
        super().__init__(parent)
        self._p = palette
        self._icons = icons
        self._target: speaker.Target | None = None
        self._hover = False
        #: 没有可用音箱 / 当前音箱离线时，名字用弱色画——禁用状态要看得见原因
        self._weak = False
        self.setFixedHeight(PICKER_H)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        tooltip.watch(self, self._tip)
        self._sync_width()

    # -- 对外 ------------------------------------------------------------
    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.update()

    def set_target(self, target: speaker.Target | None, weak: bool = False) -> None:
        self._target = target
        self._weak = weak
        if target is not None:
            # 触发一次异步取图；图到了由 MainWindow 调 update()
            self._icons.ensure(target.model)
        self._sync_width()
        self.update()

    def text(self) -> str:
        if self._target is None:
            return "无可用音箱"
        name = self._target.name or self._target.model or "未命名音箱"
        return f"{name}（离线）" if not self._target.online else name

    # -- 内部 ------------------------------------------------------------
    def _tip(self) -> str:
        if self._target is None:
            return "没有找到支持「执行文本指令」的音箱"
        if not self._target.online:
            return f"{self._target.name} 当前离线，指令发不出去"
        return f"点击切换音箱：{self._target.name}"

    def _font(self) -> QFont:
        f = QFont(self.font())
        f.setPixelSize(13)
        return f

    def _sync_width(self) -> None:
        fm = QFontMetrics(self._font())
        # +2 是给 ``elidedText`` 的余量：可用宽度**正好等于**文字宽度时，Qt 仍会
        # 吞掉最后一个字换成省略号（亚像素取整），"Xiaomi 智能家庭屏 6" 会变成
        # "Xiaomi 智能家庭屏…"。宁可宽两像素。
        name_w = min(fm.horizontalAdvance(self.text()), NAME_MAX) + 2
        # 左内边距 + 图标 + 间隙 + 名字 + 间隙 + 三角 + 右内边距
        self.setFixedWidth(6 + ICON + 8 + name_w + 8 + 9 + 6)

    # -- 绘制 ------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        pal = self._p
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self._hover and self._target is not None:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.blend(pal.card, pal.text, 0.06)))
            p.drawRoundedRect(QRectF(self.rect()), 8, 8)

        cy = self.height() / 2
        x = 6
        target = self._target
        if target is not None:
            pix = self._icons.scaled(target.model, ICON)
            if pix is not None:
                p.drawPixmap(QPoint(x, int(cy - pix.height() / 2)), pix)
                x += ICON
            else:
                # 图还没到（或这类设备没有官方图）：淡圆占位，别让名字左右乱跳
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(pal.bg_soft))
                p.drawEllipse(QRectF(x, cy - ICON / 2, ICON, ICON))
                x += ICON
            x += 8

        color = pal.text_mute if (self._weak or target is None) else pal.text
        f = self._font()
        p.setFont(f)
        p.setPen(QColor(color))
        fm = QFontMetrics(f)
        avail = self.width() - x - 8 - 9 - 6
        p.drawText(
            QRectF(x, 0, max(0, avail), self.height()),
            int(Qt.AlignLeft | Qt.AlignVCenter),
            fm.elidedText(self.text(), Qt.ElideRight, max(0, avail)),
        )
        self._paint_chevron(p, self.width() - 6 - 9, cy, color)
        p.end()

    @staticmethod
    def _paint_chevron(p: QPainter, x: float, cy: float, color: str) -> None:
        path = QPainterPath()
        path.moveTo(x, cy - 2.0)
        path.lineTo(x + 9, cy - 2.0)
        path.lineTo(x + 4.5, cy + 3.0)
        path.closeSubpath()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        p.drawPath(path)

    # -- 交互 ------------------------------------------------------------
    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.clicked.emit()
            return
        super().mouseReleaseEvent(event)


class _Tile(QWidget):
    """面板里的一台音箱：产品图 + 名字。点了就选它。

    离线那台**不塞「（离线）」后缀**：方块只有 88–120px 宽，加了后缀名字就被挤成
    省略号，反而认不出是哪台。改用图标压到 42% 不透明 + 名字走弱色——和菜单里
    那句「（离线）」等价，只是换了个说法，剩下的交给 tooltip。
    """

    picked = Signal(str)

    def __init__(
        self,
        target: speaker.Target,
        palette: theme.Palette,
        icons: IconProvider,
        current: bool,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self._target = target
        self._p = palette
        self._icons = icons
        self._current = current
        self._hover = False
        icons.ensure(target.model)
        self.setFixedSize(self._width_for(), TILE_H)
        self.setCursor(Qt.PointingHandCursor if target.online else Qt.ArrowCursor)
        tooltip.watch(self, self._tip)

    # -- 对外 ------------------------------------------------------------
    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.update()

    def is_current(self) -> bool:
        return self._current

    def did(self) -> str:
        return self._target.did

    # -- 内部 ------------------------------------------------------------
    def _font(self) -> QFont:
        f = QFont(self.font())
        f.setPixelSize(13)
        return f

    def _width_for(self) -> int:
        fm = QFontMetrics(self._font())
        name = self._target.name or self._target.model or "未命名音箱"
        return max(TILE_MIN_W, min(TILE_MAX_W, fm.horizontalAdvance(name) + 20))

    def _tip(self) -> str:
        t = self._target
        name = t.name or t.model or "未命名音箱"
        if not t.online:
            return f"{name} 当前离线，指令发不出去"
        return f"切换到这面音箱：{name}"

    # -- 绘制 ------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        pal = self._p
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self._current or (self._hover and self._target.online):
            p.setPen(Qt.NoPen)
            p.setBrush(
                QColor(_selected_bg(pal) if self._current else theme.popup_row_hover(pal))
            )
            p.drawRoundedRect(QRectF(self.rect()), TILE_R, TILE_R)

        x = (self.width() - TILE_ICON) / 2
        pix = self._icons.scaled(self._target.model, TILE_ICON)
        if not self._target.online:
            p.setOpacity(0.42)
        if pix is not None:
            p.drawPixmap(QPoint(int(x), TILE_PAD), pix)
        else:
            # 图还没到：淡圆占位，名字不跟着上下跳
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(pal.bg_soft))
            p.drawEllipse(QRectF(x, TILE_PAD, TILE_ICON, TILE_ICON))
        p.setOpacity(1.0)

        f = self._font()
        p.setFont(f)
        p.setPen(QColor(pal.text if self._target.online else pal.text_mute))
        fm = QFontMetrics(f)
        avail = self.width() - 8
        p.drawText(
            QRectF(4, TILE_PAD + TILE_ICON + 8, avail, TILE_NAME_H),
            int(Qt.AlignHCenter | Qt.AlignTop),
            fm.elidedText(
                self._target.name or self._target.model or "未命名音箱",
                Qt.ElideRight,
                max(0, avail),
            ),
        )
        p.end()

    # -- 交互 ------------------------------------------------------------
    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if (
            event.button() == Qt.LeftButton
            and self._target.online
            and self.rect().contains(event.position().toPoint())
        ):
            self.picked.emit(self._target.did)
            return
        super().mouseReleaseEvent(event)


class _QuietCheck(QWidget):
    """静默开关用的小勾选框，16×16。

    为什么不用 ``ToggleSwitch``：那是设置页和卡片上"一眼看出谁开着"的主角控件，
    44×24 的绿色胶囊塞进这块面板，比下面任何一个设备方块都抢眼——而「静默」只是
    "这一条怎么发"的修饰语，不是面板的主要内容（2026-11-11 用户原话：存在感很强）。
    换成小号的勾选框后，关着只剩一圈弱描边，扫过去就过去了；开着才有色，用的还是
    与"当前设备"同源的 ``pal.accent``。

    交互上仍复用 ``_SilentRow`` 那套：整行可点，这个小方块自己也吃点击（不往父控件
    冒泡，免得一次点击切两下）。
    """

    toggled = Signal(bool)

    SIDE = 16
    R = 4

    def __init__(self, on: bool, palette: theme.Palette, parent: QWidget) -> None:
        super().__init__(parent)
        self._p = palette
        self._on = bool(on)
        self._hover = False
        self.setFixedSize(self.SIDE, self.SIDE)
        self.setCursor(Qt.PointingHandCursor)

    # -- 状态 ------------------------------------------------------------
    def is_on(self) -> bool:
        return self._on

    def set_on(self, on: bool, animate: bool = True) -> None:
        # 签名对齐 ``ToggleSwitch.set_on``：调用方（含验证脚本）不必区分是哪种控件。
        # 16px 的方块没有滑块可滑，动画没有意义，animate 只是接住不用。
        on = bool(on)
        if on == self._on:
            return
        self._on = on
        self.update()

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.update()

    # -- 绘制 ------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        pal = self._p
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.75, 0.75, -0.75, -0.75)
        if self._on:
            fill = pal.accent
            if self._hover:
                fill = theme.blend(fill, pal.text, 0.12)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(fill))
            p.drawRoundedRect(r, self.R, self.R)
            # 勾：两段折线，圆头；白色在深色/浅色的 accent 上都读得出来
            cy = r.center().y()
            path = QPainterPath()
            path.moveTo(r.left() + 3.6, cy + 0.2)
            path.lineTo(r.left() + 6.4, cy + 3.0)
            path.lineTo(r.right() - 3.2, cy - 3.0)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor("#FFFFFF"), 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(path)
        else:
            p.setPen(QPen(QColor(pal.text if self._hover else pal.text_mute), 1.2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r, self.R, self.R)
        p.end()

    # -- 交互 ------------------------------------------------------------
    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.set_on(not self._on)
            self.toggled.emit(self._on)
            return
        super().mouseReleaseEvent(event)


class _SilentRow(QWidget):
    """「静默执行」+ 小勾选框。整行可点，别逼用户去戳那 16×16 的小方块。

    文字放右边、勾选框放左边：面板的左下角是它该待的地方，与参考图一致；开关在左，
    视线从设备行落下来第一眼就撞上它，不用横着找。

    视觉权重刻意压到最低（11px + 弱色文字 + 16px 勾选框）：它是"这一条怎么发"的
    修饰语，不该和"发给哪台设备"抢同一层注意力。关着时文字走 ``text_mute``，开着才
    提到正文色——颜色本身就是状态提示，不需要再用文字重复一遍"已开启"。
    """

    toggled = Signal(bool)

    def __init__(self, palette: theme.Palette, on: bool, parent: QWidget) -> None:
        super().__init__(parent)
        self._p = palette
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(7)
        self.switch = _QuietCheck(on, palette, self)
        row.addWidget(self.switch, 0, Qt.AlignVCenter)
        # QLabel 必须带 parent：无父控件的 QWidget 就是顶层窗口（会弹小窗）
        self.label = QLabel("静默执行", self)
        row.addWidget(self.label, 0, Qt.AlignVCenter)
        # 行本身没有底色，视觉高度只有 16px；撑到 20 是为了点击目标别太窄
        # （整行仍然可点，边框看不见但不影响命中）
        self.setMinimumHeight(20)
        self.switch.toggled.connect(self._on_switch)
        self.setCursor(Qt.PointingHandCursor)
        tooltip.watch(self, self._tip)
        self._sync_label()

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.switch.set_palette(palette)
        self._sync_label()

    def is_on(self) -> bool:
        return self.switch.is_on()

    def set_on(self, on: bool, animate: bool = True) -> None:
        self.switch.set_on(on, animate)
        self._sync_label()

    def _on_switch(self, on: bool) -> None:
        self._sync_label()
        self.toggled.emit(on)

    def _sync_label(self) -> None:
        color = self._p.text if self.switch.is_on() else self._p.text_mute
        self.label.setStyleSheet(
            f"color:{color};font-size:11px;background:transparent;"
        )

    def _tip(self) -> str:
        return (
            "开：音箱只执行、不念结果（问「现在几点」不会报时）。\n"
            "关：正常出声回答。这是全局偏好，换音箱也跟着走。"
        )

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        # 点勾选框本身由 ``_QuietCheck`` 自己处理（它不往上冒泡），所以这里只管
        # 点到文字或空白的那一下，不会双重切换。
        if event.button() == Qt.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.switch.set_on(not self.switch.is_on())
            self._sync_label()
            self.toggled.emit(self.switch.is_on())
            return
        super().mouseReleaseEvent(event)


class _Panel(QWidget):
    """选音箱的横向面板：一排设备方块 + 左下角「静默执行」。

    它是 ``Qt.Popup``：点外面、按 Esc 都会自己收起（Qt 内建行为），所以这里只需要
    在"点了某台设备"时主动 ``hide()``。窗口必须自己透明（``WA_TranslucentBackground``
    + 去边框 + 去系统阴影），否则圆角外面还会冒出一块方底——和 ``ui/menu.py``
    里那段注释是同一个坑。
    """

    picked = Signal(str)
    silent_changed = Signal(bool)

    def __init__(
        self,
        palette: theme.Palette,
        icons: IconProvider,
        targets: list[speaker.Target],
        current_did: str,
        silent: bool,
        parent: QWidget,
    ) -> None:
        super().__init__(
            parent, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._p = palette

        col = QVBoxLayout(self)
        col.setContentsMargins(PANEL_PAD, PANEL_PAD, PANEL_PAD, PANEL_PAD)
        col.setSpacing(PANEL_GAP)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(TILE_GAP)
        self.tiles: list[_Tile] = []
        for t in targets:
            tile = _Tile(t, palette, icons, t.did == current_did, self)
            tile.picked.connect(self._on_pick)
            row.addWidget(tile)
            self.tiles.append(tile)
        # 一排等宽：宽度按最长的那个名字算，大家都取这个值。各自按名字算会出现
        # 「111 / 88 / 111」这种参差，图标间距忽宽忽窄，一眼看着像没对齐（参考图
        # 里那块面板就是等距网格）。
        if self.tiles:
            uniform = max(t.width() for t in self.tiles)
            for tile in self.tiles:
                tile.setFixedWidth(uniform)
        row.addStretch(1)
        col.addLayout(row)

        self.silent_row = _SilentRow(palette, silent, self)
        self.silent_row.toggled.connect(self.silent_changed)
        col.addWidget(self.silent_row, 0, Qt.AlignLeft)

    # -- 对外 ------------------------------------------------------------
    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        for tile in self.tiles:
            tile.set_palette(palette)
        self.silent_row.set_palette(palette)
        self.update()

    def silent(self) -> bool:
        return self.silent_row.is_on()

    def show_at(self, pos: QPoint) -> None:
        """在 ``pos``（全局坐标，面板左上角）弹出来，靠屏幕边缘时往里收。"""
        self.adjustSize()
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            x = min(pos.x(), area.right() - self.width() - 8)
            y = min(pos.y(), area.bottom() - self.height() - 8)
            self.move(max(x, area.left() + 8), max(y, area.top() + 8))
        else:
            self.move(pos)
        self.show()

    # -- 内部 ------------------------------------------------------------
    def _on_pick(self, did: str) -> None:
        self.picked.emit(did)
        self.hide()

    # -- 事件 ------------------------------------------------------------
    def event(self, event) -> bool:  # noqa: N802
        # 失去激活就收起来。``Qt.Popup`` 在正常桌面上点外面会自己收，但这层保险
        # 覆盖"合成事件 / 没有窗口管理器"的情况——不收掉的面板会一直浮在卡片墙上，
        # 比多收一次糟得多（用户在 2026-11-11 的截图里就踩过这种残留浮层）。
        if event.type() == QEvent.WindowDeactivate:
            self.hide()
        return super().event(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        # 每次打开都新建一块面板，收起后就该销毁，别让它们挂在指令条底下攒着
        super().hideEvent(event)
        self.deleteLater()

    def paintEvent(self, event) -> None:  # noqa: N802
        pal = self._p
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        body = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(theme.popup_border(pal)), 1))
        p.setBrush(QColor(theme.popup_bg(pal)))
        p.drawRoundedRect(body, PANEL_R, PANEL_R)
        p.end()


class _Input(QLineEdit):
    """指令输入框。多做的唯一一件事：超过 200 字符要**说出来**，别让粘贴悄悄少几个字。

    不用 ``setMaxLength``：Qt 到点就默默截断，用户看不出为什么少了一段。改成在
    ``insert`` 里判？不够——键盘输入与粘贴走的是 QWidgetLineControl 内部路径，
    绕不到 Python 的 ``insert`` 重载上。所以统一在 ``textChanged`` 里收口：
    无论怎么进来（打字 / 粘贴 / 拖放 / 输入法上屏）都能截住并提示一次。
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setFrame(False)
        self.setPlaceholderText("输入对音箱指令，按回车发送")

    def trim(self) -> bool:
        """超长就地截到 200，返回是否真的截过。"""
        text = self.text()
        if len(text) <= speaker.MAX_CHARS:
            return False
        pos = self.cursorPosition()
        self.blockSignals(True)
        self.setText(text[: speaker.MAX_CHARS])
        self.setCursorPosition(min(pos, speaker.MAX_CHARS))
        self.blockSignals(False)
        return True


class SpeakerBar(QWidget):
    """顶栏指令条。``sent`` 是清洗后的文本，发不发得出去由上层决定。"""

    sent = Signal(str)
    target_changed = Signal(str)
    silent_changed = Signal(bool)

    def __init__(
        self,
        palette: theme.Palette,
        icons: IconProvider,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._p = palette
        self._icons = icons
        self._targets: list[speaker.Target] = []
        self._target: speaker.Target | None = None
        self._focus = False
        #: 当前目标在线（= 输入可发）。禁用态要画得更暗，见 ``paintEvent``
        self._live = False
        #: 静默执行。全局偏好，由 ``MainWindow`` 从 ``Prefs`` 灌进来、变化时回写
        self._silent = False
        self.setFixedHeight(H)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 0, 16, 0)
        row.setSpacing(6)

        self.picker = _Picker(palette, icons, self)
        self.picker.clicked.connect(self._open_panel)
        row.addWidget(self.picker)

        self.input = _Input(self)
        self.input.returnPressed.connect(self._send)
        self.input.textChanged.connect(self._on_text_changed)
        self.input.installEventFilter(self)
        row.addWidget(self.input, 1)

        self._apply_input_style()
        self._refresh_enabled()

    # -- 对外 ------------------------------------------------------------
    def target(self) -> speaker.Target | None:
        return self._target

    def silent(self) -> bool:
        return self._silent

    def set_silent(self, on: bool) -> None:
        """设静默开关（面板里点一下，或启动时从偏好读进来）。"""
        on = bool(on)
        if on == self._silent:
            return
        self._silent = on
        self.silent_changed.emit(on)

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.picker.set_palette(palette)
        self._apply_input_style()
        self.update()

    def set_targets(self, targets: list[speaker.Target], preferred: str = "") -> None:
        """换一批候选。当前选中的仍在列表里就**保持不动**（含它变离线的情况）。

        只有当前这台从列表里消失（换了家庭 / 设备被移除）才回落到 ``preferred``
        （上次记住的），再不行就挑第一台在线的，最后才退回第一台。
        """
        self._targets = list(targets)
        keep = self._target.did if self._target is not None else ""
        chosen: speaker.Target | None = None
        for cand in (keep, preferred):
            if not cand:
                continue
            chosen = next((t for t in self._targets if t.did == cand), None)
            if chosen is not None:
                break
        if chosen is None and self._targets:
            chosen = next((t for t in self._targets if t.online), self._targets[0])
        same = (chosen is None and self._target is None) or (
            chosen is not None
            and self._target is not None
            and chosen.did == self._target.did
            and chosen.online == self._target.online
        )
        self._target = chosen
        if not same:
            self._apply_target(emit=True)

    def notice(self, text: str) -> None:
        """在输入框下方弹一条轻提示（发给云端之后的结果由上层调这里）。"""
        tooltip.toast(self.input, text, self._p)

    def update_icon(self) -> None:
        """产品图异步到位后叫一声，重画左侧那块（图没到之前画的是淡圆占位）。"""
        self.picker.update()

    # -- 内部 ------------------------------------------------------------
    def _apply_target(self, *, emit: bool) -> None:
        target = self._target
        self.picker.set_target(target, weak=target is None or not target.online)
        self._refresh_enabled()
        if emit and target is not None:
            self.target_changed.emit(target.did)

    def _refresh_enabled(self) -> None:
        live = self._target is not None and self._target.online
        self._live = live
        # 只禁输入框，**不禁选择器**：音箱离线时用户还得能切到另一台
        self.input.setEnabled(live)
        if not live and self.input.hasFocus():
            self.input.clearFocus()
        self.update()

    def _apply_input_style(self) -> None:
        pal = self._p
        self.input.setStyleSheet(
            f"QLineEdit{{background:transparent;border:none;padding:0 2px 0 4px;"
            f"font-size:13px;color:{pal.text};"
            f"selection-background-color:{pal.accent};}}"
        )
        qp = self.input.palette()
        qp.setColor(QPalette.PlaceholderText, QColor(pal.text_mute))
        qp.setColor(QPalette.Text, QColor(pal.text))
        self.input.setPalette(qp)

    def _on_text_changed(self, _text: str) -> None:
        if self.input.trim():
            self.notice(f"最多 {speaker.MAX_CHARS} 个字符，多的没写进去")

    def _send(self) -> None:
        got = speaker.clean(self.input.text())
        if got.empty:
            # 空输入按回车不响应：占位提示已经说明怎么用，再弹一条只是噪声
            return
        self.input.clear()
        notes = []
        if got.truncated:
            notes.append(f"已截断到 {speaker.MAX_CHARS} 个字符")
        if got.dropped:
            notes.append(f"已忽略不支持的字符：{got.dropped}")
        if notes:
            self.notice("；".join(notes))
        self.sent.emit(got.text)

    def _build_panel(self) -> _Panel:
        """候选面板。拆出来是为了能离线验内容（``exec``/``show`` 之前就能量尺寸）。"""
        return _Panel(
            self._p,
            self._icons,
            self._targets,
            self._target.did if self._target is not None else "",
            self._silent,
            self,
        )

    def _open_panel(self) -> None:
        if not self._targets:
            return
        panel = self._build_panel()
        panel.picked.connect(self._on_panel_pick)
        panel.silent_changed.connect(self.set_silent)
        # 挂到**整条**的左下角，不是 picker 的左下角：面板是"这条输入框的下拉"，
        # 与条同宽起步，视觉上才像从条里抽出来的一张纸（参考图就是这个关系）
        panel.show_at(self.mapToGlobal(QPoint(0, self.height() + 6)))

    def _on_panel_pick(self, did: str) -> None:
        for t in self._targets:
            if t.did == did:
                self._target = t
                self._apply_target(emit=True)
                return

    # -- 事件 ------------------------------------------------------------
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.input and event.type() in (QEvent.FocusIn, QEvent.FocusOut):
            self._focus = event.type() == QEvent.FocusIn
            self.update()
        return super().eventFilter(obj, event)

    def paintEvent(self, event) -> None:  # noqa: N802
        pal = self._p
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        body = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if not self._live:
            # 没有可用音箱 / 目标离线：底色压一档，免得白得像个还能点的框
            p.setPen(QPen(QColor(pal.border), 1))
            p.setBrush(QColor(pal.bg_soft))
        else:
            p.setPen(QPen(QColor(pal.accent if self._focus else pal.border), 1))
            p.setBrush(QColor(pal.card))
        p.drawRoundedRect(body, R, R)
        p.end()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        # 点条上空白处 = 聚焦输入
        if event.button() == Qt.LeftButton and self.input.isEnabled():
            self.input.setFocus()
        super().mouseReleaseEvent(event)


__all__ = ["SpeakerBar"]
