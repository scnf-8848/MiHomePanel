# SPDX-License-Identifier: GPL-3.0-or-later
"""设置窗口：外观 / 排序 / 隐藏 / 账号。

排版照 QQ 设置那套骨架（``ui/settings_rows.py``）：**一行分区标题 + 下面一张圆角卡**，
卡里一行行控件用 1px 线分开。四个页面共用同一个骨架，换页面时视线不用重新找位置。

设备顺序在首页直接拖动卡片调整（拖拽手势与点击已经分开）；设置页不再重复提供
设备层。家庭、房间、快捷场景的顺序和"恢复默认"只有这里能改，所以保留一页专门做
这件事：左边四个分类，右边一页一类。

分类栏单独铺一块圆角底（``theme.rail_bg``）。主界面侧栏那一套 ``NAV_BG`` 不能
直接搬过来：那两档是照 ``pal.bg`` 定的，而设置窗口的内容区是 ``surface_bg``
（比主界面亮/暗一档），搬过来浅色只差 3、深色只差 2 个明度档，栏和内容区又糊成
一片。``rail_bg`` 只相对 ``surface_bg`` 推一档，方向仍随主题。

排序和隐藏都用**树**（``ui/tree.py``）。两页同一套骨架：顶部两个互斥的分段标签，
下面一张卡里只放当前那一棵树，各自吃满卡片高度。排序页是"家庭 → 房间"和
"家庭 → 快捷场景"两棵（设备顺序由首页拖动负责，这里不渲染第三层）；隐藏页是
"家庭 → 房间 → 设备"和"家庭 → 快捷场景"两棵，开关打开 = 显示、关闭 = 收起。
树把层级摊开，一次看全，每层都能折起来；拖动只在排序页的同层内生效——把设备
拖进别的房间属于"改设备归属"，那是米家 App 的事，我们只负责顺序。

窗口本身也是无边框的，标题栏和主窗口同一套实现（只是少了最大化按钮）。
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from .. import __version__
from ..core import autostart
from ..core.prefs import CAMERA_NETWORKS
from ..core.store import Store
from . import theme, tray
from .chrome import (
    SHADOW_PAD,
    TitleBar,
    WindowFrame,
    floating_size,
    shadow_alpha,
    surface_bg,
    surface_border,
)
from .combo import Combo
from .home_tabs import (
    PLACEMENT_ACTIVE_FIRST,
    PLACEMENT_FIXED,
    STYLE_PILL,
    STYLE_SIZE,
    STYLE_WEIGHT,
    HomeTabs,
)
from .radio_dot import RadioDot
from .settings_rows import SettingsCard, link_button, section_header
from .tree import ReorderTree
from .widgets import ElidedLabel, ToggleSwitch, draw_text

PAGE_MARGIN = 26
#: 下拉框统一宽度。QQ 的右对齐控件也是定宽的，宽窄随文字变会让同一张卡里的
#: 控件边缘参差不齐。
COMBO_W = 168


def dialog_qss(p: theme.Palette) -> str:
    """所有模态子窗口共用的行式 QSS（设置页 / 电脑自动化页）。

    抽成公开函数是为了让新页面复用同一套 ``SectionCard`` / ``SettingsRow`` 配色，
    别各抄一份——切主题时整张表重算，抄漏一条就留着上个主题的颜色。
    """
    return theme.app_qss(p) + f"""
    QDialog {{ background: transparent; }}
    /* 下拉框的样式在 ui/combo.py 里（含自绘箭头、弹出层的真圆角、禁滚轮）。
       这里不再写 QComboBox 规则：控件自己的 styleSheet 和窗口级的会互相盖，
       留一份在 Combo 上最省心，设置页 / 设备详情页也才能完全一致。 */
    QFrame#SettingsRail {{
        background: {theme.rail_bg(p)};
        border: none;
        border-radius: {theme.RADIUS_CARD}px;
    }}
    QListWidget#SettingsNav {{
        background: transparent;
        border: none;
        border-radius: 0;
        padding: 0;
        outline: none;
    }}
    QListWidget#SettingsNav::item {{
        height: 38px;
        padding-left: 14px;
        border-radius: 9px;
        color: {p.text_soft};
    }}
    QListWidget#SettingsNav::item:hover {{
        background: {theme.blend(theme.rail_bg(p), p.text, 0.06)};
        color: {p.text};
    }}
    QListWidget#SettingsNav::item:selected {{
        background: {theme.rail_row_bg(p)};
        color: {p.text};
        font-weight: 600;
    }}
    QSplitter::handle {{ background: transparent; }}
    QPushButton#Link {{
        background: transparent;
        border: none;
        color: {p.text_mute};
        font-size: 12px;
        padding: 0 2px;
    }}
    QPushButton#Link:hover {{ color: {p.accent}; }}
    /* -- 行式排版（见 ui/settings_rows.py） ----------------------------
       颜色全写在这里、按 objectName 生效：切主题是整张表重新 setStyleSheet，
       QSS 规则会跟着重算，不必逐个控件刷一遍（那样漏一个就留着上个主题的色）。 */
    QLabel#PageTitle {{ color: {p.text}; font-size: 20px; font-weight: 700; }}
    QLabel#PageHint {{ color: {p.text_mute}; font-size: 12px; }}
    /* 关于页的说明段落：这段是"维护立场"，比一般提示更该看清，
       所以用 text_soft（浅色下 text_mute 对白底只有 2.54:1，不够正文可读性） */
    QLabel#AboutNote {{ color: {p.text_soft}; font-size: 12px; }}
    QLabel#SectionTitle {{ color: {p.text}; font-size: 14px; font-weight: 600; }}
    QLabel#RowTitle {{ color: {p.text}; font-size: 13px; }}
    QLabel#RowSub {{ color: {p.text_mute}; font-size: 11px; }}
    QLabel#RowValue {{ color: {p.text}; font-size: 13px; }}
    QLabel#RowValue[state="on"] {{ color: {theme.ACCENT_GREEN}; font-weight: 600; }}
    QLabel#RowValueSoft {{ color: {p.text_soft}; font-size: 12px; }}
    QFrame#SectionCard {{
        background: {theme.panel_bg(p)};
        border: none;
        border-radius: {theme.RADIUS_CARD}px;
    }}
    QFrame#RowDivider {{ background: {theme.panel_divider(p)}; border: none; }}
    QPushButton#Primary {{
        background: {theme.ACCENT_GREEN};
        color: #FFFFFF;
        border: none;
        /* 圆角矩形不是胶囊（与四窗主按钮同一档，见 common_qss）。
           退出登录那颗是同位置的 Ghost，两者必须同一形状，否则登录 / 未登录
           切换时按钮会"变形"。 */
        border-radius: {theme.RADIUS_BUTTON_SOFT}px;
        padding: 11px 22px;
        font-size: 13px;
        font-weight: 600;
    }}
    QPushButton#Primary:hover {{
        background: {theme.blend(theme.ACCENT_GREEN, "#FFFFFF", 0.12)};
    }}
    QPushButton#Ghost {{
        background: {theme.blend(p.bg, p.text, 0.22 if p.is_dark else 0.52)};
        color: rgba(255, 255, 255, 0.94);
        border: none;
        /* 圆角矩形，不是胶囊：RADIUS_BUTTON(20) 在 40px 高的按钮上就是胶囊，
           与四窗里的主按钮（10px）不一致。用户 11-13 要求统一。 */
        border-radius: {theme.RADIUS_BUTTON_SOFT}px;
        padding: 11px 22px;
        font-size: 13px;
    }}
    QPushButton#Ghost:hover {{
        background: {theme.blend(p.bg, p.text, 0.30 if p.is_dark else 0.62)};
        color: #FFFFFF;
    }}
    """


class _ThemeCard(QWidget):
    """主题预览卡：左半是界面底色，右半是卡片色，底下写名字。"""

    picked = Signal(str)

    def __init__(self, key: str, label: str, preview: theme.Palette, palette: theme.Palette) -> None:
        super().__init__()
        self.key = key
        self._label = label
        self._preview = preview
        self._p = palette
        self._selected = False
        self.setFixedSize(150, 106)
        self.setCursor(Qt.PointingHandCursor)

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.update()

    def set_selected(self, on: bool) -> None:
        if on != self._selected:
            self._selected = on
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self._p
        pv = self._preview
        r = QRect(self.rect()).adjusted(1, 1, -1, -1)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(pv.bg))
        p.drawRoundedRect(r, 14, 14)

        # 两张"卡片"，展示底色 / 卡片色的对比
        for x, y, w, h in ((14, 14, 56, 34), (76, 14, 56, 34), (14, 54, 118, 22)):
            p.setBrush(QColor(pv.card))
            p.drawRoundedRect(QRect(r.x() + x, r.y() + y, w, h), 7, 7)
        p.setBrush(QColor(pv.accent))
        p.drawRoundedRect(QRect(r.x() + 22, r.y() + 60, 26, 4), 2, 2)

        # 标签底色是"预览主题"的底色，所以文字也要用预览主题的文字色，
        # 否则深色模式下"浅色"那张卡的标签会白字白底
        draw_text(p, QRect(r.x(), r.bottom() - 26, r.width(), 20), self._label, size=12,
                  color=pv.text, weight=QFont.Medium, align=Qt.AlignHCenter | Qt.AlignVCenter)

        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(pal.accent if self._selected else pal.border),
                      2 if self._selected else 1))
        p.drawRoundedRect(r, 14, 14)
        if self._selected:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(pal.accent))
            p.drawEllipse(r.right() - 16, r.top() + 10, 8, 8)
        p.end()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.picked.emit(self.key)


class SettingsDialog(QDialog):
    theme_selected = Signal(str)
    #: "logout" = 用户点了退出登录；"login" = 未登录状态下点了扫码登录。
    #: 真正的动作交给主窗口做（登录窗口、清空设备墙都在那边），这里只发信号。
    account_action = Signal(str)

    def __init__(
        self,
        store: Store,
        palette: theme.Palette,
        parent: QWidget | None = None,
        account: dict[str, str] | None = None,
        tray_available: bool | None = None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self._p = palette
        self._account = account or {}
        self._loading = False
        # 托盘能不能用由主窗口传：它那边真的装过一次，比这里再问一次系统可靠
        self._tray_ok = tray_available if tray_available is not None else tray.tray_available()

        self.setWindowTitle("设置")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self._frame = WindowFrame(self, resizable=True,
                                  radius=theme.RADIUS_CARD,
                                  shadow=SHADOW_PAD,
                                  shadow_alpha=shadow_alpha(self._p),
                                  fill=lambda: surface_bg(self._p),
                                  border=lambda: surface_border(self._p))
        # 尺寸写的是**内容**尺寸，``floating_size`` 负责加回两圈阴影留白
        self.resize(*floating_size(820, 680))
        # 最小高度按"最高的一页"定：外观页是"标题 + 说明 + 三张卡"（主题预览 130 +
        # 家庭标签两行 + 预览 + 摄像头一行），实测需要 567，最小窗口 600 只给到 528，
        # 摄像头那行会被裁掉。取 640（567 + 余量）留出取整空间。
        # 没有做整页滚动——排序 / 隐藏页里本来就是可滚动的树，外面再套一层滚动
        # 会变成滚动套滚动
        self.setMinimumSize(*floating_size(700, 640))

        outer = QVBoxLayout(self._frame.body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.titlebar = TitleBar("设置", palette, subtitle="外观 · 排序 · 隐藏 · 账号",
                                 buttons=("close",), variant="dialog",
                                 frame=self._frame)
        outer.addWidget(self.titlebar)

        body = QWidget()
        row = QHBoxLayout(body)
        row.setContentsMargins(16, 16, 20, 18)
        row.setSpacing(18)

        self.nav = QListWidget()
        self.nav.setObjectName("SettingsNav")
        self.nav.setFrameShape(QFrame.NoFrame)
        self.nav.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        for text in ("外观", "排序", "隐藏", "账号", "常规", "关于"):
            self.nav.addItem(QListWidgetItem(text))

        # 分类栏单独一块圆角底（``theme.rail_bg``）。不直接给 QListWidget 上底色：
        # 列表本身是矩形，圆角得画在容器上，否则四角会被 viewport 的直角盖掉。
        rail = QFrame()
        rail.setObjectName("SettingsRail")
        rail.setFrameShape(QFrame.NoFrame)
        rail.setFixedWidth(132)
        col = QVBoxLayout(rail)
        col.setContentsMargins(6, 6, 6, 6)
        col.setSpacing(0)
        col.addWidget(self.nav)
        self._rail = rail
        row.addWidget(rail)

        self.pages = QStackedWidget()
        row.addWidget(self.pages, 1)
        outer.addWidget(body, 1)

        self.pages.addWidget(self._build_appearance())
        self.pages.addWidget(self._build_order())
        self.pages.addWidget(self._build_hidden())
        self.pages.addWidget(self._build_account())
        self.pages.addWidget(self._build_general())
        self.pages.addWidget(self._build_about())

        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setCurrentRow(0)

        self._apply_palette(palette)
        self._reload_all()

    # -- 公共 ------------------------------------------------------------
    def _apply_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        theme.apply_to_app(palette)
        # 行式排版的颜色全在 QSS 里（PageTitle / RowTitle / SectionCard ...），
        # 重新 setStyleSheet 就全跟着换了，不必逐个控件设色
        self.setStyleSheet(dialog_qss(palette))
        self.titlebar.set_palette(palette)
        # 浮窗阴影不跟 QSS：深色要更黑（见 ``chrome.shadow_alpha``）。设置页是唯一
        # 能在"窗口开着的时候"换主题的子窗口，这一句必须补。
        self._frame.set_shadow_alpha(shadow_alpha(palette))
        for box in (
            getattr(self, "cb_placement", None),
            getattr(self, "cb_style", None),
            getattr(self, "cb_camera_network", None),
        ):
            if box is not None:
                box.set_palette(palette)
        for card in self._theme_cards:
            card.set_palette(palette)
        # 常规页的圆点、开关是自绘的（颜色不走 QSS），逐个通知一遍。
        # 用 findChildren 而不是列名字：以后再加一行不会再漏掉
        for cls in (RadioDot, ToggleSwitch):
            for widget in self.findChildren(cls):
                widget.set_palette(palette)
        for tree in (self.tree_rooms, self.tree_scenes,
                     self.tree_hidden_devices, self.tree_hidden_scenes):
            tree.set_palette(palette)
        preview = getattr(self, "preview", None)
        if preview is not None:
            preview.set_palette(palette)
        self._sync_theme_cards()
        self.update()

    def _page(self) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(PAGE_MARGIN, 6, PAGE_MARGIN, 10)
        col.setSpacing(10)
        return page, col

    def _page_head(self, col: QVBoxLayout, title: str, hint: str = "") -> None:
        """页面顶部：大标题 + 一行灰说明。QQ 的内容区也是这么起头的——
        左边分类已经写了名字，这里再写一遍是为了"视线落在正文里也知道自己在哪"。"""
        head = QLabel(title)
        head.setObjectName("PageTitle")
        col.addWidget(head)
        if hint:
            lab = QLabel(hint)
            lab.setObjectName("PageHint")
            lab.setWordWrap(True)
            col.addWidget(lab)
        col.addSpacing(2)

    def _section(self, col: QVBoxLayout, title: str, action: tuple[str, object] | None = None
                 ) -> QPushButton | None:
        """分区标题（卡片上方那行）。``action`` = (按钮文字, 回调)，返回那颗按钮，
        库里/测试里要直接点它时用得上。"""
        head = section_header(title)
        btn = None
        if action is not None:
            btn = link_button(action[0])
            btn.clicked.connect(action[1])
            head.addWidget(btn)
        col.addLayout(head)
        return btn

    # -- 外观 ------------------------------------------------------------
    def _build_appearance(self) -> QWidget:
        page, col = self._page()
        self._page_head(col, "外观", "浅色适合白天，深色适合夜里看。窗口大小和主题都会记住。")

        self._section(col, "主题")
        card = SettingsCard(pad=(16, 12, 16, 12))
        self._theme_cards = [
            _ThemeCard("light", "浅色", theme.LIGHT, self._p),
            _ThemeCard("dark", "深色", theme.DARK, self._p),
        ]
        for item in self._theme_cards:
            item.picked.connect(self._on_theme_picked)
        card.add_unit(self._theme_row(self._theme_cards))
        col.addWidget(card)

        col.addSpacing(8)
        self._section(col, "家庭标签")
        tags = SettingsCard()
        self.cb_placement = Combo(self._p)
        self.cb_placement.setFixedWidth(COMBO_W)
        self.cb_placement.addItem("保持设定顺序", PLACEMENT_FIXED)
        self.cb_placement.addItem("激活的排到最左", PLACEMENT_ACTIVE_FIRST)
        self.cb_placement.currentIndexChanged.connect(self._on_layout_option)
        tags.add_row("排列", self.cb_placement, sub="决定激活的家庭要不要挪到最左")

        self.cb_style = Combo(self._p)
        self.cb_style.setFixedWidth(COMBO_W)
        self.cb_style.addItem("字号变大", STYLE_SIZE)
        self.cb_style.addItem("仅加粗变色", STYLE_WEIGHT)
        self.cb_style.addItem("胶囊底色", STYLE_PILL)
        self.cb_style.currentIndexChanged.connect(self._on_layout_option)
        tags.add_row("激活样式", self.cb_style, sub="用什么方式把当前家庭标出来")

        # 预览用真的 HomeTabs（只是不接点击），所以看到的和主窗口一字不差
        self.preview = HomeTabs(self._p, interactive=False)
        self.preview.set_homes(
            [("p1", "我的家"), ("p2", "共享家庭"), ("p3", "办公室")], "p2"
        )
        tags.add_unit(self._preview_unit(self.preview), stretch=0)
        col.addWidget(tags)

        col.addSpacing(8)
        self._section(col, "摄像头")
        camera = SettingsCard()
        self.cb_camera_network = Combo(self._p)
        self.cb_camera_network.setFixedWidth(COMBO_W)
        for value, label in (
            ("lan_first", "局域网优先"),
            ("remote_first", "远程优先"),
            ("remote_only", "仅远程"),
        ):
            if value in CAMERA_NETWORKS:
                self.cb_camera_network.addItem(label, value)
        self.cb_camera_network.currentIndexChanged.connect(self._on_camera_network)
        camera.add_row(
            "取流方式",
            self.cb_camera_network,
            sub="局域网优先：先直连，失败自动改远程；"
            "远程优先：先远程，失败退回局域网；仅远程：始终走中继",
        )
        col.addWidget(camera)
        col.addStretch(1)
        return page

    def _theme_row(self, cards: list[_ThemeCard]) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(14)
        for item in cards:
            row.addWidget(item)
        row.addStretch(1)
        return holder

    def _preview_unit(self, preview: HomeTabs) -> QWidget:
        """预览行：上面是活的标签栏，下面一行灰字说明它不是真的家庭。"""
        holder = QWidget()
        col = QVBoxLayout(holder)
        col.setContentsMargins(0, 8, 0, 6)
        col.setSpacing(6)
        # 标签栏放进一个固定高的条里，自己手动定位：Qt 会把负的 layout
        # contentsMargins 悄悄换成样式默认值（实测 -14 → 9/11），抵消不了那 14px。
        # 子 widget 画到父条左边界之外 Qt 不裁剪，条本身又在卡片的 16px 内边距里，
        # 所以胶囊左缘仍在卡内，不会被切。
        strip = QWidget(holder)
        strip.setFixedHeight(preview.height())
        preview.setParent(strip)
        self._preview_strip = strip
        self._sync_preview_inset()
        col.addWidget(strip)
        note = QLabel("示例家庭名仅用于预览，改动会立刻作用到主窗口顶部。")
        note.setObjectName("RowSub")
        note.setWordWrap(True)
        col.addWidget(note)
        return holder

    def _sync_preview_inset(self) -> None:
        """胶囊档的 HomeTabs 首项自带 14px 左内边距，把控件往左挪这么多抵消。

        不抵消的话整排标签会相对卡里的标题、说明文字向右缩进一截——间距是均匀了，
        但跟卡片里的其它文字又对不齐了。切样式后 inset 会变，必须重设。
        """
        strip = getattr(self, "_preview_strip", None)
        preview = getattr(self, "preview", None)
        if strip is not None and preview is not None:
            preview.move(-preview.content_inset, 0)

    def _on_layout_option(self) -> None:
        if self._loading:
            return
        placement = str(self.cb_placement.currentData() or "")
        style = str(self.cb_style.currentData() or "")
        self.store.set_home_layout(placement=placement, style=style)
        self.preview.configure(placement, style)
        self._sync_preview_inset()

    def _sync_layout_controls(self) -> None:
        self._loading = True
        try:
            i = self.cb_placement.findData(self.store.home_placement)
            if i >= 0:
                self.cb_placement.setCurrentIndex(i)
            j = self.cb_style.findData(self.store.home_active_style)
            if j >= 0:
                self.cb_style.setCurrentIndex(j)
            k = self.cb_camera_network.findData(self.store.camera_network)
            if k >= 0:
                self.cb_camera_network.setCurrentIndex(k)
            self.preview.configure(self.store.home_placement, self.store.home_active_style)
            self._sync_preview_inset()
        finally:
            self._loading = False

    def _on_camera_network(self) -> None:
        if self._loading:
            return
        self.store.set_camera_network(str(self.cb_camera_network.currentData() or ""))

    def _on_theme_picked(self, key: str) -> None:
        palette = theme.DARK if key == "dark" else theme.LIGHT
        self.theme_selected.emit(key)
        self._apply_palette(palette)

    def _sync_theme_cards(self) -> None:
        for card in self._theme_cards:
            card.set_selected(card.key == self._p.name)

    # -- 排序 ------------------------------------------------------------
    def _build_order(self) -> QWidget:
        page, col = self._page()
        self._page_head(
            col, "排序",
            "点左侧折角展开 / 收起。这里排家庭、房间和快捷场景；设备顺序在首页"
            "直接拖动卡片调整。拖动只在同一层内生效，松手立即生效，没排过的"
            "新家庭 / 新房间 / 新场景自动排在末尾。「恢复默认」连同各房间的设备"
            "顺序一起清空。",
        )

        self.tree_rooms = ReorderTree(self._p, mode="order")
        self.tree_rooms.moved.connect(self._commit_room_tree)
        self.tree_scenes = ReorderTree(self._p, mode="order")
        self.tree_scenes.moved.connect(self._commit_scene_tree)

        # 两块排序不再上下各占一半：同一时间只看一块，各自吃满卡片高度。
        # 切换沿用首页家庭标签那套分段控件的胶囊样式。
        self.order_tabs = HomeTabs(self._p)
        self.order_tabs.configure(PLACEMENT_FIXED, STYLE_PILL)
        self.order_tabs.set_homes(
            [("rooms", "家庭 → 房间"), ("scenes", "家庭 → 快捷场景")], "rooms",
        )
        self.order_tabs.home_selected.connect(self._switch_order_tab)

        self.btn_reset_rooms = link_button("恢复默认")
        self.btn_reset_rooms.clicked.connect(self._reset_layout)
        self.btn_reset_scenes = link_button("恢复默认")
        self.btn_reset_scenes.clicked.connect(self._reset_scenes)

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(8)
        bar.addWidget(self.order_tabs)
        bar.addStretch(1)
        bar.addWidget(self.btn_reset_rooms)
        bar.addWidget(self.btn_reset_scenes)
        col.addLayout(bar)

        self.order_stack = QStackedWidget()
        self.order_stack.addWidget(self.tree_rooms)
        self.order_stack.addWidget(self.tree_scenes)
        card = SettingsCard(pad=(10, 8, 10, 8))
        card.add_unit(self.order_stack, stretch=1)
        col.addWidget(card, 1)
        self._switch_order_tab("rooms")
        return page

    def _switch_order_tab(self, key: str) -> None:
        scenes = key == "scenes"
        self.order_stack.setCurrentIndex(1 if scenes else 0)
        self.btn_reset_rooms.setVisible(not scenes)
        self.btn_reset_scenes.setVisible(scenes)

    def _fill_order_trees(self) -> None:
        # 第一次填（树还是空的）才全部展开；之后一律照用户折成的样子还原，
        # 包括"全折起来"——否则在隐藏页点一下开关，这里会自己撑开
        first_rooms = self.tree_rooms.topLevelItemCount() == 0
        first_scene = self.tree_scenes.topLevelItemCount() == 0
        keep_rooms = self.tree_rooms.expanded_keys()
        keep_scene = self.tree_scenes.expanded_keys()
        self.tree_rooms.clear()
        self.tree_scenes.clear()

        for home in self.store.homes():
            h_item = self.tree_rooms.add_row(
                None, home.display_name, {"kind": "home", "key": home.id}
            )
            for room_id, room_name in self.store.room_entries(home.id):
                # 台数只数可见设备：顺序表的含义是「可见设备的顺序」，
                # 隐藏项不在表里，也不该算进这个数字里。
                states = [
                    st for st in self.store.device_states(home.id, room_id)
                    if not self.store.prefs.is_hidden(st.did)
                ]
                self.tree_rooms.add_row(
                    h_item, room_name,
                    {"kind": "room", "home": home.id, "room": room_id},
                    suffix=f"{len(states)} 台",
                )

            s_item = self.tree_scenes.add_row(
                None, home.display_name, {"kind": "home", "key": home.id}
            )
            scenes = self.store.scenes_for(home.id, include_hidden=True)
            for scene in scenes:
                sid = str(scene.get("scene_id") or "")
                name = str(scene.get("name") or "")
                if not sid or not name:
                    continue
                suffix = ""
                if self.store.prefs.is_scene_hidden(sid):
                    suffix = "已隐藏"
                elif not scene.get("enable", True):
                    suffix = "已停用"
                self.tree_scenes.add_row(
                    s_item, name, {"kind": "scene", "home": home.id, "id": sid}, suffix=suffix
                )

        self.tree_rooms.restore_expanded(None if first_rooms else keep_rooms)
        self.tree_scenes.restore_expanded(None if first_scene else keep_scene)

    # -- 隐藏 ------------------------------------------------------------
    def _build_hidden(self) -> QWidget:
        page, col = self._page()
        self._page_head(col, "隐藏",
                        "开关打开 = 显示；关闭 = 收起来。房间 / 家庭那一层的开关是整组操作，"
                        "只藏了一部分时会停在中间。隐藏只影响首页怎么排，不影响轮询和控制。")

        # 和排序页同一套骨架：顶部两个互斥 tab，下面一张卡里只放当前那棵树，
        # 各自吃满卡片高度。两棵树常驻，切 tab 只换索引，展开状态和滚动位置都不会丢。
        self.tree_hidden_devices = ReorderTree(self._p, mode="hidden")
        self.tree_hidden_devices.toggle_requested.connect(self._on_toggle)
        self.tree_hidden_scenes = ReorderTree(self._p, mode="hidden")
        self.tree_hidden_scenes.toggle_requested.connect(self._on_toggle)

        self.hidden_tabs = HomeTabs(self._p)
        self.hidden_tabs.configure(PLACEMENT_FIXED, STYLE_PILL)
        self.hidden_tabs.set_homes(
            [("devices", "家庭 → 房间 → 设备"), ("scenes", "家庭 → 快捷场景")], "devices",
        )
        self.hidden_tabs.home_selected.connect(self._switch_hidden_tab)

        # 「全部恢复显示」按 tab 分开：在设备页顺手清掉场景的隐藏状态会很意外
        self.btn_show_devices = link_button("全部恢复显示")
        self.btn_show_devices.clicked.connect(self._show_all_devices)
        self.btn_show_scenes = link_button("全部恢复显示")
        self.btn_show_scenes.clicked.connect(self._show_all_scenes)

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(8)
        bar.addWidget(self.hidden_tabs)
        bar.addStretch(1)
        bar.addWidget(self.btn_show_devices)
        bar.addWidget(self.btn_show_scenes)
        col.addLayout(bar)

        self.hidden_stack = QStackedWidget()
        self.hidden_stack.addWidget(self.tree_hidden_devices)
        self.hidden_stack.addWidget(self.tree_hidden_scenes)
        card = SettingsCard(pad=(10, 8, 10, 8))
        card.add_unit(self.hidden_stack, stretch=1)
        col.addWidget(card, 1)
        self._switch_hidden_tab("devices")
        return page

    def _switch_hidden_tab(self, key: str) -> None:
        scenes = key == "scenes"
        self.hidden_stack.setCurrentIndex(1 if scenes else 0)
        self.btn_show_devices.setVisible(not scenes)
        self.btn_show_scenes.setVisible(scenes)

    def _fill_hidden_devices(self) -> None:
        """家庭 → 房间 → 设备。开关 1.0 = 显示，0.0 = 隐藏。"""
        tree = self.tree_hidden_devices
        first = tree.topLevelItemCount() == 0
        keep = tree.expanded_keys()
        scroll = tree.verticalScrollBar().value()
        tree.clear()
        prefs = self.store.prefs
        for home in self.store.homes():
            states = list(self.store.home_devices(home.id))
            shown = sum(1 for st in states if not prefs.is_hidden(st.did))
            h_item = tree.add_row(
                None, home.display_name,
                {"kind": "home", "home": home.id},
                suffix=f"{shown}/{len(states)}",
                switch=shown / len(states) if states else 0.0,
            )
            for room_id, room_name in self.store.room_entries(home.id):
                room_states = self.store.device_states(home.id, room_id)
                if not room_states:
                    continue
                visible = sum(1 for st in room_states if not prefs.is_hidden(st.did))
                r_item = tree.add_row(
                    h_item, room_name,
                    {"kind": "room", "home": home.id, "room": room_id},
                    suffix=f"{visible}/{len(room_states)}",
                    switch=visible / len(room_states),
                )
                for st in room_states:
                    tree.add_row(
                        r_item, st.name,
                        {"kind": "device", "home": home.id, "room": room_id, "id": st.did},
                        suffix="已离线" if not st.online else "",
                        switch=0.0 if prefs.is_hidden(st.did) else 1.0,
                    )
        tree.restore_expanded(None if first else keep)
        tree.verticalScrollBar().setValue(scroll)

    def _fill_hidden_scenes(self) -> None:
        """家庭 → 快捷场景。场景是叶子，不再有「快捷场景」中间分组行。"""
        tree = self.tree_hidden_scenes
        first = tree.topLevelItemCount() == 0
        keep = tree.expanded_keys()
        scroll = tree.verticalScrollBar().value()
        tree.clear()
        prefs = self.store.prefs
        for home in self.store.homes():
            scenes = [
                s for s in self.store.scenes_for(home.id, include_hidden=True)
                if str(s.get("scene_id") or "") and str(s.get("name") or "")
            ]
            shown = sum(
                1 for s in scenes if not prefs.is_scene_hidden(str(s.get("scene_id") or ""))
            )
            h_item = tree.add_row(
                None, home.display_name,
                {"kind": "scene_home", "home": home.id},
                suffix=f"{shown}/{len(scenes)}",
                switch=shown / len(scenes) if scenes else 0.0,
            )
            for scene in scenes:
                sid = str(scene.get("scene_id") or "")
                tree.add_row(
                    h_item, str(scene.get("name") or ""),
                    {"kind": "scene", "home": home.id, "id": sid},
                    suffix="" if scene.get("enable", True) else "已停用",
                    switch=0.0 if prefs.is_scene_hidden(sid) else 1.0,
                )
        tree.restore_expanded(None if first else keep)
        tree.verticalScrollBar().setValue(scroll)

    def _on_toggle(self, data: dict) -> None:
        """行尾开关：按层级决定改哪些，改完只重建对应的那棵树（保留展开状态）。"""
        kind = str(data.get("kind") or "")
        home = str(data.get("home") or "")
        prefs = self.store.prefs

        if kind == "device":
            did = str(data.get("id") or "")
            hidden = prefs.is_hidden(did)
            self._apply_hidden(hide=[did] if not hidden else [],
                               show=[did] if hidden else [])
            self._fill_hidden_devices()
        elif kind == "scene":
            sid = str(data.get("id") or "")
            self.store.set_scene_hidden(sid, not prefs.is_scene_hidden(sid))
            self._fill_hidden_scenes()
        elif kind in ("room", "home"):
            room = str(data.get("room") or "")
            if room:
                group = list(self.store.device_states(home, room))
            else:
                group = list(self.store.home_devices(home))
            shown = [st for st in group if not prefs.is_hidden(st.did)]
            # 还有没显示的 → 全部显示；已经全显示了 → 全部收起来
            want_hidden = bool(shown) and len(shown) == len(group)
            ids = [st.did for st in group]
            self._apply_hidden(hide=ids if want_hidden else [], show=[] if want_hidden else ids)
            self._fill_hidden_devices()
        elif kind == "scene_home":
            scenes = [
                str(s["scene_id"]) for s in self.store.scenes_for(home, include_hidden=True)
                if str(s.get("scene_id") or "")
            ]
            shown = [sid for sid in scenes if not prefs.is_scene_hidden(sid)]
            want_hidden = bool(shown) and len(shown) == len(scenes)
            changed = False
            for sid in scenes:
                changed |= prefs.set_scene_hidden(sid, want_hidden)
            if changed:
                self.store.refresh_layout()
            self._fill_hidden_scenes()

        self._fill_order_trees_silently()

    def _apply_hidden(self, *, show: list[str], hide: list[str]) -> None:
        prefs = self.store.prefs
        changed = False
        for did in show:
            # 恢复显示 = 追加到所在房间末尾（房间归属只有 Store 知道）
            slot = self.store.device_room(did)
            home, room = slot if slot is not None else ("", "")
            changed |= prefs.set_hidden(did, False, home=home, room=room)
        for did in hide:
            changed |= prefs.set_hidden(did, True)
        if changed:
            self.store.refresh_layout()

    # -- 账号 ------------------------------------------------------------
    def _build_account(self) -> QWidget:
        page, col = self._page()
        self._page_head(col, "账号",
                        "凭据只保存在这台电脑上（和 mijia-api 命令行工具共用同一个文件）。"
                        "退出登录会删掉它，之后需要重新扫码才能继续控制设备。")

        self._section(col, "登录状态")
        card = SettingsCard()
        self.lbl_state = QLabel("—")
        self.lbl_state.setObjectName("RowValue")
        self.lbl_user = QLabel("—")
        self.lbl_user.setObjectName("RowValue")
        self.lbl_path = ElidedLabel("—")
        self.lbl_path.setObjectName("RowValueSoft")
        card.add_row("状态", self.lbl_state, expand=True)
        card.add_row("账号", self.lbl_user, expand=True)
        card.add_row("凭据文件", self.lbl_path, expand=True)
        col.addWidget(card)

        # 米家的退出登录是**页面最底部居中的灰胶囊**，不是红色危险按钮：
        # 这个动作可逆（重新扫码就回来了），用红色反而像不可挽回的操作。
        self.btn_account = QPushButton("退出登录")
        self.btn_account.setObjectName("Ghost")
        self.btn_account.setCursor(Qt.PointingHandCursor)
        self.btn_account.setFixedSize(240, 44)
        self.btn_account.clicked.connect(self._on_account_action)

        # 先撑开，把按钮推到页面最底部——和米家"滚到底才看到退出登录"一致
        col.addStretch(1)
        self.note_account = QLabel(
            "目前同一时间只支持一个账号。想换号就先退出登录再扫码；"
            "多账号同时在线（比如自己的家和父母家各一个号）还在计划里。"
        )
        self.note_account.setObjectName("PageHint")
        self.note_account.setWordWrap(True)
        col.addWidget(self.note_account)
        col.addSpacing(16)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.btn_account)
        row.addStretch(1)
        col.addLayout(row)
        return page

    # -- 常规 ------------------------------------------------------------
    def _build_general(self) -> QWidget:
        page, col = self._page()
        self._page_head(col, "常规", "程序自己的行为，和你的米家设备无关。")

        self._section(col, "启动设置")
        start = SettingsCard()
        self.sw_autostart = ToggleSwitch(on=False, palette=self._p, size="sm")
        self.sw_autostart.toggled.connect(self._on_autostart_toggled)
        # 状态挂在副标题上而不是单开一行"当前状态"：这里是注册表的实际值，
        # 用户在任务管理器里禁用了也得跟着变（不能只信 prefs）
        self.row_autostart = start.add_row(
            "开机自启动", self.sw_autostart, sub="未启用"
        )

        self.sw_start_in_tray = ToggleSwitch(
            on=self.store.prefs.start_in_tray, palette=self._p, size="sm"
        )
        self.sw_start_in_tray.toggled.connect(self._on_start_in_tray_toggled)
        self.row_start_in_tray = start.add_row(
            "启动时最小化到系统托盘",
            self.sw_start_in_tray,
            sub="自启时直接待在托盘里，不弹窗口",
        )
        col.addWidget(start)

        col.addSpacing(8)
        self._section(col, "关闭面板时")
        close = SettingsCard()
        # 单选：名称在左（和设置页其它行一致），圆点在右
        self.rb_quit = RadioDot(self._p)
        self.rb_tray = RadioDot(self._p)
        # 互斥交给 QButtonGroup——QAbstractButton 默认不互斥，那是 QRadioButton 的默认
        self.close_group = QButtonGroup(self)
        for dot in (self.rb_quit, self.rb_tray):
            self.close_group.addButton(dot)
            dot.toggled.connect(self._on_close_mode_toggled)
        close.add_row("退出程序", self.rb_quit)
        self.row_tray = close.add_row("最小化到托盘，不退出程序", self.rb_tray)
        col.addWidget(close)

        col.addStretch(1)
        return page

    # -- 关于 ------------------------------------------------------------
    @staticmethod
    def _about_text(text: str, *, soft: bool = True) -> QLabel:
        """关于页的只读值：可选中复制，长 URL 自动换行。"""
        lab = QLabel(text)
        lab.setObjectName("RowValueSoft" if soft else "RowValue")
        lab.setWordWrap(True)
        lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
        return lab

    @staticmethod
    def _about_note(text: str) -> QLabel:
        """关于页的说明段落：可选中复制、自动换行。

        用 ``AboutNote``（``text_soft``）而不是 ``PageHint``（``text_mute``）：
        维护立场这段是要让人认真读的，浅色下 mute 对白底只有 2.54:1。
        """
        lab = QLabel(text)
        lab.setObjectName("AboutNote")
        lab.setWordWrap(True)
        lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
        return lab

    def _build_about(self) -> QWidget:
        page, col = self._page()
        self._page_head(col, "关于", "版本、许可与第三方组件；个人项目，按需维护。")

        self._section(col, "程序")
        card = SettingsCard()
        # 版本值只放 "v0.1.0"，产品名挪到左侧小字：设置行的值列只拿 sizeHint 宽，
        # "米家控制台（MiHomePanel） v0.1.0" 塞不进一行会被折成三行、行高又固定，
        # 最下面的版本号就被裁掉了（用户 11-15 01:0x 报）
        card.add_row("版本", self._about_text(f"v{__version__}", soft=False),
                     sub="米家控制台（MiHomePanel）")
        card.add_row("许可", self._about_text("GPL-3.0-or-later"),
                     sub="完整条款见程序目录的 LICENSE")
        col.addWidget(card)

        # 维护立场：这里写清楚"不保证持续更新 + 设备适配没法代劳"，
        # 免得别人把它当成长期有人维护的项目来提需求（用户 11-15 定）。
        # 源码 / 反馈地址写在说明段里而不是卡片行里：卡片的值列很窄，长 URL 会被
        # 折成三四行、还右对齐，读起来像乱码；说明段是整行宽度，折一次就够。
        col.addSpacing(8)
        self._section(col, "维护与反馈")
        col.addWidget(self._about_note(
            "这是个人自用的项目，不承诺持续更新，也可能在满足个人需求之后停止维护。\n"
            "设备适配是按我手上有的型号逐台做的：你的设备没适配、或适配不正常时，"
            "我这边没有那台设备，就无法为你调试和验证。需要调整的话，建议下载源码自己改"
            "（GPL-3.0-or-later 允许修改与再分发，不必事先征求同意），或交给你的 AI 助手"
            "按你的设备改。\n"
            "源码：https://github.com/scnf-8848/MiHomePanel\n"
            "严重问题（凭据泄露、数据丢失、程序无法启动、影响系统安全）欢迎到 "
            "https://github.com/scnf-8848/MiHomePanel/issues 反馈。"))

        col.addSpacing(8)
        self._section(col, "第三方组件")
        third = SettingsCard()
        third.add_row(
            "go2rtc", self._about_text("MIT · 本地补丁版"),
            sub="首帧优化与云台补丁；组件发布："
                "https://github.com/scnf-8848/MiHomePanel/releases/tag/go2rtc-990f4de",
        )
        third.add_row("mijiaAPI", self._about_text("GPL-3.0-or-later"),
                      sub="云端登录、签名与设备接口")
        col.addWidget(third)

        col.addSpacing(8)
        self._section(col, "致谢")
        col.addWidget(self._about_note(
            "感谢 AlexxIT/go2rtc、Do1e/mijia-api、Google Material Icons"
            "（Apache-2.0），以及 Reign9201/mihome-mac、Songmanual/MijiaDeck"
            " 等项目的公开资料。"))
        col.addStretch(1)
        return page

    # -- 常规页的三种状态 -------------------------------------------------
    @staticmethod
    def _set_checked(dot: RadioDot, checked: bool) -> None:
        """改选中态但**不触发** toggled。

        刷新时直接 ``setChecked`` 会仿真成"用户点了一下"：关闭行为那项会把偏好存
        一遍——读一次界面就改一次设置，这种 bug 很难查。
        """
        blocker = QSignalBlocker(dot)
        dot.setChecked(checked)
        del blocker

    def _refresh_general(self) -> None:
        self._refresh_autostart()
        self._refresh_close_mode()

    def _refresh_autostart(self) -> None:
        info = autostart.status()
        usable = info.state != autostart.STATE_UNSUPPORTED
        self.sw_autostart.set_on(info.is_on, animate=False)
        # 源码运行时置灰而不是藏起来：藏起来这一页会只剩一个卡片标题，更让人困惑
        self.sw_autostart.setEnabled(usable)
        self.row_autostart.set_sub({
            autostart.STATE_ON: "已写入系统启动项",
            autostart.STATE_OFF: "未启用",
            autostart.STATE_STALE:
                f"指向的程序不是当前这个：{info.target or '（空）'}，下次启动会改回来",
            autostart.STATE_UNSUPPORTED: "源码运行时不支持，打包成 exe 后才能用",
        }.get(info.state, str(info.state)))

        # 「启动时最小化到托盘」只管自启那一次：自启用不了、或这个环境没托盘，
        # 这一项都没有落地的可能
        self.sw_start_in_tray.set_on(self.store.prefs.start_in_tray, animate=False)
        self.sw_start_in_tray.setEnabled(usable and self._tray_ok)
        if not self._tray_ok:
            self.row_start_in_tray.set_sub("这个环境下没有系统托盘，只能最小化到任务栏")
        elif not info.is_on:
            self.row_start_in_tray.set_sub("打开上面一项后才会生效")
        else:
            self.row_start_in_tray.set_sub("自启时直接待在托盘里，不弹窗口")

    def _refresh_close_mode(self) -> None:
        hide = self.store.prefs.close_to_tray
        # 只设 True 那一个：QButtonGroup 互斥，另一个会被自动取消
        self._set_checked(self.rb_tray if hide else self.rb_quit, True)
        # 没有托盘就没有"最小化到托盘"这个选项，两个圆点都点不动
        self.rb_quit.setEnabled(self._tray_ok)
        self.rb_tray.setEnabled(self._tray_ok)
        self.row_tray.set_sub(
            "" if self._tray_ok else "这个环境下没有系统托盘，关窗一律退出程序"
        )

    def _on_close_mode_toggled(self, checked: bool) -> None:
        # 一次点击会来两条（新的 True、旧的 False），只认 True 那条
        if not checked or self._loading:
            return
        hide = self.rb_tray.isChecked()
        if self.store.prefs.close_to_tray == hide:
            return
        self.store.prefs.close_to_tray = hide
        self.store.prefs.save()

    def _on_start_in_tray_toggled(self, on: bool) -> None:
        if self._loading:
            return
        self.store.prefs.start_in_tray = on
        self.store.prefs.save()

    def _on_autostart_toggled(self, on: bool) -> None:
        if self._loading:
            return
        try:
            autostart.enable() if on else autostart.disable()
        except autostart.AutostartError as exc:
            # 失败就把开关拨回真实状态，再把原因写在副标题里——不要静默吞掉
            self._refresh_autostart()
            self.row_autostart.set_sub(f"设置失败：{exc}")
            return
        self._refresh_autostart()

    def _refresh_account(self) -> None:
        info = self._account
        signed = bool(info.get("signed_in"))
        self.lbl_state.setText("已登录" if signed else "未登录")
        # 颜色走 QSS 的属性选择器（QLabel#RowValue[state="on"]），切主题不用再刷一遍；
        # 改了动态属性必须 unpolish/polish 才会重新匹配规则
        self.lbl_state.setProperty("state", "on" if signed else "off")
        self._repolish(self.lbl_state)
        self.lbl_user.setText(info.get("user") or "—")
        self.lbl_path.set_full(info.get("path") or "—")
        self.btn_account.setText("退出登录" if signed else "扫码登录")
        self.btn_account.setObjectName("Ghost" if signed else "Primary")
        # 换了 objectName 要重新走一遍样式表，否则颜色不会变
        self._repolish(self.btn_account)

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        """改 ``objectName`` / 动态属性之后重新匹配一遍 QSS。"""
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    def _on_account_action(self) -> None:
        signed = bool(self._account.get("signed_in"))
        self.account_action.emit("logout" if signed else "login")
        self.accept()

    # -- 数据加载 --------------------------------------------------------
    def _reload_all(self) -> None:
        self._loading = True
        try:
            self._fill_order_trees()
            self._fill_hidden_devices()
            self._fill_hidden_scenes()
        finally:
            self._loading = False
        self._sync_layout_controls()
        self._refresh_account()
        self._refresh_general()

    def _fill_order_trees_silently(self) -> None:
        self._loading = True
        try:
            self._fill_order_trees()
        finally:
            self._loading = False

    # -- 提交 ------------------------------------------------------------
    @staticmethod
    def _row_data(item) -> dict:
        from .tree import ROLE_DATA

        return dict(item.data(0, ROLE_DATA) or {})

    def _commit_room_tree(self) -> None:
        if self._loading:
            return
        tree = self.tree_rooms
        homes: list[str] = []
        rooms: dict[str, list[str]] = {}
        for i in range(tree.topLevelItemCount()):
            h = tree.topLevelItem(i)
            home = str(self._row_data(h).get("key") or "")
            if not home:
                continue
            homes.append(home)
            rooms[home] = [
                str(self._row_data(h.child(j)).get("room") or "") for j in range(h.childCount())
            ]
        self.store.apply_order_snapshot(homes=homes, rooms=rooms)

    def _commit_scene_tree(self) -> None:
        if self._loading:
            return
        tree = self.tree_scenes
        scenes: dict[str, list[str]] = {}
        for i in range(tree.topLevelItemCount()):
            h = tree.topLevelItem(i)
            home = str(self._row_data(h).get("key") or "")
            if not home:
                continue
            scenes[home] = [
                str(self._row_data(h.child(j)).get("id") or "") for j in range(h.childCount())
            ]
        self.store.apply_order_snapshot(scenes=scenes)

    # -- 重置 ------------------------------------------------------------
    def _reset_layout(self) -> None:
        """恢复家庭 / 房间顺序，并清空各房间的设备顺序。

        设备层已经不在排序页上了，但"恢复默认"仍要能清掉设备顺序——否则用户把
        某个房间排乱之后没有回到默认的入口。按房间逐个清，与改版前同一口径。
        """
        for home_id in self.store.home_ids():
            self.store.prefs.reset_rooms(home_id)
            for room_id in self.store.room_ids(home_id):
                self.store.prefs.reset_devices(home_id, room_id)
        self.store.set_home_order([])
        self._fill_order_trees_silently()

    def _reset_scenes(self) -> None:
        for home_id in self.store.home_ids():
            self.store.prefs.scene_order.pop(home_id, None)
        self.store.prefs.save()
        self.store.refresh_layout()
        self._fill_order_trees_silently()

    def _show_all_devices(self) -> None:
        # 设备：清隐藏 + 逐台追加到所在房间末尾 + 重建（走 Store 保证两端口径一致）
        self.store.clear_hidden()
        self._fill_hidden_devices()
        self._fill_order_trees_silently()

    def _show_all_scenes(self) -> None:
        if self.store.prefs.clear_hidden_scenes():
            self.store.refresh_layout()
        self._fill_hidden_scenes()
        self._fill_order_trees_silently()


__all__ = ["SettingsDialog", "dialog_qss"]
