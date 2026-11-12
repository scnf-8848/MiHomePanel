# SPDX-License-Identifier: GPL-3.0-or-later
"""设置窗口：外观 / 排序 / 隐藏 / 账号。

排版照 QQ 设置那套骨架（``ui/settings_rows.py``）：**一行分区标题 + 下面一张圆角卡**，
卡里一行行控件用 1px 线分开。四个页面共用同一个骨架，换页面时视线不用重新找位置。

排序没有做"在首页直接拖动"——首页卡片还兼着开关和进详情的点击，拖拽手势和点击
会互相打架；房间、场景、隐藏设备又是三类不同的东西，全塞进首页会把它变成编辑
模式。所以集中到这个窗口：左边四个分类，右边一页一类。

分类栏单独铺一块圆角底（``theme.rail_bg``）。主界面侧栏那一套 ``NAV_BG`` 不能
直接搬过来：那两档是照 ``pal.bg`` 定的，而设置窗口的内容区是 ``surface_bg``
（比主界面亮/暗一档），搬过来浅色只差 3、深色只差 2 个明度档，栏和内容区又糊成
一片。``rail_bg`` 只相对 ``surface_bg`` 推一档，方向仍随主题。

排序和隐藏都用**树**（``ui/tree.py``）：设备天然是"家庭 → 房间 → 设备"三层，
平铺时必须先选家庭、再选房间，才看得到一小截设备。树把三层摊开，一次看全，
每层都能折起来；拖动只在同一层内生效——把一台设备拖进别的房间属于"改设备归属"，
那是米家 App 的事，我们只负责顺序。

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
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from ..core import autostart
from ..core.prefs import CAMERA_NETWORKS
from ..core.store import Store
from . import theme, tray
from .chrome import TitleBar, WindowFrame, surface_bg, surface_border
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
        border-radius: {theme.RADIUS_BUTTON}px;
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
        border-radius: {theme.RADIUS_BUTTON}px;
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
                                  fill=lambda: surface_bg(self._p),
                                  border=lambda: surface_border(self._p))
        self.resize(820, 680)
        # 最小高度按"最高的一页"定：外观页是"标题 + 说明 + 三张卡"（主题预览 130 +
        # 家庭标签两行 + 预览 + 摄像头一行），实测需要 567，最小窗口 600 只给到 528，
        # 摄像头那行会被裁掉。取 640（567 + 余量）留出取整空间。
        # 没有做整页滚动——排序 / 隐藏页里本来就是可滚动的树，外面再套一层滚动
        # 会变成滚动套滚动
        self.setMinimumSize(700, 640)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.titlebar = TitleBar("设置", palette, subtitle="外观 · 排序 · 隐藏设备 · 账号",
                                 buttons=("close",), variant="dialog")
        outer.addWidget(self.titlebar)

        body = QWidget()
        row = QHBoxLayout(body)
        row.setContentsMargins(16, 16, 20, 18)
        row.setSpacing(18)

        self.nav = QListWidget()
        self.nav.setObjectName("SettingsNav")
        self.nav.setFrameShape(QFrame.NoFrame)
        self.nav.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        for text in ("外观", "排序", "隐藏设备", "账号", "常规"):
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
        for tree in (self.tree_devices, self.tree_scenes, self.tree_hidden):
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
        self._page_head(col, "排序",
                        "点左侧折角展开 / 收起。拖动只在这一层内生效（设备不会因为拖动换房间）。"
                        "松手立即生效，没排过的新设备 / 新房间会自动排在末尾。")

        self.tree_devices = ReorderTree(self._p, mode="order")
        self.tree_devices.moved.connect(self._commit_device_tree)
        self.tree_scenes = ReorderTree(self._p, mode="order")
        self.tree_scenes.moved.connect(self._commit_scene_tree)

        # 两棵树的高度让用户自己调：设备多的人把上面拉大，场景多的人反过来。
        # 分隔条留 14：两块各自是圆角卡，紧贴在一起会读成"一块被切开"
        split = QSplitter(Qt.Vertical)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(14)
        split.addWidget(self._tree_section("家庭 → 房间 → 设备", self.tree_devices,
                                           "恢复默认", self._reset_devices)[0])
        split.addWidget(self._tree_section("家庭 → 快捷场景", self.tree_scenes,
                                           "恢复默认", self._reset_scenes)[0])
        split.setSizes([420, 200])
        split.setStyleSheet("QSplitter::handle { background: transparent; }")
        col.addWidget(split, 1)
        return page

    def _tree_section(self, title: str, tree: ReorderTree, action: str,
                      reset) -> tuple[QWidget, QPushButton]:
        """分区标题（外面）+ 只装树的一张卡（里面）。

        标题放在卡片外面是 QQ 那套的重点：卡片是"一组控件的容器"，标题是"这组叫
        什么"。放进卡里会让标题读成卡内第一行，两个以上的分区就分不清主次了。
        """
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        btn = self._section(lay, title, (action, reset))
        card = SettingsCard(pad=(10, 8, 10, 8))
        card.add_unit(tree, stretch=1)
        lay.addWidget(card, 1)
        return box, btn

    def _fill_order_trees(self) -> None:
        # 第一次填（树还是空的）才全部展开；之后一律照用户折成的样子还原，
        # 包括"全折起来"——否则在隐藏页点一下开关，这里会自己撑开
        first_dev = self.tree_devices.topLevelItemCount() == 0
        first_scene = self.tree_scenes.topLevelItemCount() == 0
        keep_dev = self.tree_devices.expanded_keys()
        keep_scene = self.tree_scenes.expanded_keys()
        self.tree_devices.clear()
        self.tree_scenes.clear()

        for home in self.store.homes():
            h_item = self.tree_devices.add_row(
                None, home.display_name, {"kind": "home", "key": home.name}
            )
            rooms = self.store.room_names(home.name)
            for room in rooms:
                # 已隐藏设备不参与排序：这里**彻底不渲染**。顺序表的含义是
                # 「可见设备的顺序」，隐藏项不在表里，所以也没有"排在末尾"。
                states = [
                    st for st in self.store.device_states(home.name, room)
                    if not self.store.prefs.is_hidden(st.did)
                ]
                r_item = self.tree_devices.add_row(
                    h_item, room,
                    {"kind": "room", "home": home.name, "room": room},
                    suffix=f"{len(states)} 台",
                )
                for st in states:
                    self.tree_devices.add_row(
                        r_item, st.name,
                        {"kind": "device", "home": home.name, "room": room, "id": st.did},
                    )

            s_item = self.tree_scenes.add_row(
                None, home.display_name, {"kind": "home", "key": home.name}
            )
            scenes = self.store.scenes_for(home.name, include_hidden=True)
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
                    s_item, name, {"kind": "scene", "home": home.name, "id": sid}, suffix=suffix
                )

        self.tree_devices.restore_expanded(None if first_dev else keep_dev)
        self.tree_scenes.restore_expanded(None if first_scene else keep_scene)

    # -- 隐藏 ------------------------------------------------------------
    def _build_hidden(self) -> QWidget:
        page, col = self._page()
        self._page_head(col, "隐藏",
                        "开关打开 = 显示；关闭 = 收起来。房间 / 家庭那一层的开关是整组操作，"
                        "只藏了一部分时会停在中间。隐藏只影响首页怎么排，不影响轮询和控制。")

        self.tree_hidden = ReorderTree(self._p, mode="hidden")
        self.tree_hidden.toggle_requested.connect(self._on_toggle)
        box, self.btn_show_all = self._tree_section(
            "家庭 → 房间 → 设备 · 快捷场景", self.tree_hidden, "全部恢复显示", self._show_all
        )
        col.addWidget(box, 1)
        return page

    def _fill_hidden_tree(self) -> None:
        first = self.tree_hidden.topLevelItemCount() == 0
        keep = self.tree_hidden.expanded_keys()
        self.tree_hidden.clear()
        prefs = self.store.prefs
        for home in self.store.homes():
            states = [d for d in self.store.home_devices(home.name)]
            scenes = [
                s for s in self.store.scenes_for(home.name, include_hidden=True)
                if str(s.get("scene_id") or "") and str(s.get("name") or "")
            ]
            shown_dev = sum(1 for st in states if not prefs.is_hidden(st.did))
            shown_scene = sum(
                1 for s in scenes if not prefs.is_scene_hidden(str(s.get("scene_id") or ""))
            )
            total = len(states) + len(scenes)
            shown = shown_dev + shown_scene
            h_item = self.tree_hidden.add_row(
                None, home.display_name,
                {"kind": "home", "home": home.name},
                suffix=f"{shown}/{total}",
                switch=shown / total if total else 0.0,
            )

            for room in self.store.room_names(home.name):
                room_states = self.store.device_states(home.name, room)
                if not room_states:
                    continue
                visible = sum(1 for st in room_states if not prefs.is_hidden(st.did))
                r_item = self.tree_hidden.add_row(
                    h_item, room,
                    {"kind": "room", "home": home.name, "room": room},
                    suffix=f"{visible}/{len(room_states)}",
                    switch=visible / len(room_states),
                )
                for st in room_states:
                    self.tree_hidden.add_row(
                        r_item, st.name,
                        {"kind": "device", "home": home.name, "room": room, "id": st.did},
                        suffix="已离线" if not st.online else "",
                        switch=0.0 if prefs.is_hidden(st.did) else 1.0,
                    )

            if scenes:
                s_item = self.tree_hidden.add_row(
                    h_item, "快捷场景",
                    {"kind": "scene_group", "home": home.name},
                    suffix=f"{shown_scene}/{len(scenes)}",
                    switch=shown_scene / len(scenes),
                )
                for scene in scenes:
                    sid = str(scene.get("scene_id") or "")
                    self.tree_hidden.add_row(
                        s_item, str(scene.get("name") or ""),
                        {"kind": "scene", "home": home.name, "id": sid},
                        suffix="" if scene.get("enable", True) else "已停用",
                        switch=0.0 if prefs.is_scene_hidden(sid) else 1.0,
                    )

        self.tree_hidden.restore_expanded(None if first else keep)

    def _on_toggle(self, data: dict) -> None:
        """行尾开关：按层级决定改哪些，改完重建（保留展开状态）。"""
        kind = str(data.get("kind") or "")
        home = str(data.get("home") or "")
        prefs = self.store.prefs

        if kind == "device":
            did = str(data.get("id") or "")
            self._apply_hidden(hide=[did] if not prefs.is_hidden(did) else [],
                               show=[did] if prefs.is_hidden(did) else [])
        elif kind == "scene":
            sid = str(data.get("id") or "")
            self.store.set_scene_hidden(sid, not prefs.is_scene_hidden(sid))
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
        elif kind == "scene_group":
            scenes = [
                str(s["scene_id"]) for s in self.store.scenes_for(home, include_hidden=True)
                if str(s.get("scene_id") or "")
            ]
            shown = [sid for sid in scenes if not prefs.is_scene_hidden(sid)]
            want_hidden = bool(shown) and len(shown) == len(scenes)
            for sid in scenes:
                prefs.set_scene_hidden(sid, want_hidden)
            self.store.refresh_layout()

        self._fill_hidden_tree()
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
            self._fill_hidden_tree()
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

    def _commit_device_tree(self) -> None:
        if self._loading:
            return
        tree = self.tree_devices
        homes: list[str] = []
        rooms: dict[str, list[str]] = {}
        devices: dict[tuple[str, str], list[str]] = {}
        for i in range(tree.topLevelItemCount()):
            h = tree.topLevelItem(i)
            home = str(self._row_data(h).get("key") or "")
            if not home:
                continue
            homes.append(home)
            rooms[home] = []
            for j in range(h.childCount()):
                r = h.child(j)
                room = str(self._row_data(r).get("room") or "")
                rooms[home].append(room)
                devices[(home, room)] = [
                    str(self._row_data(r.child(k)).get("id") or "")
                    for k in range(r.childCount())
                ]
        self.store.apply_order_snapshot(homes=homes, rooms=rooms, devices=devices)

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
    def _reset_devices(self) -> None:
        for home in self.store.home_names():
            self.store.prefs.reset_rooms(home)
            for room in self.store.room_names(home):
                self.store.prefs.reset_devices(home, room)
        self.store.set_home_order([])
        self._fill_order_trees_silently()

    def _reset_scenes(self) -> None:
        for home in self.store.home_names():
            self.store.prefs.scene_order.pop(home, None)
        self.store.prefs.save()
        self.store.refresh_layout()
        self._fill_order_trees_silently()

    def _show_all(self) -> None:
        # 设备：清隐藏 + 逐台追加到所在房间末尾 + 重建（走 Store 保证两端口径一致）
        self.store.clear_hidden()
        if self.store.prefs.clear_hidden_scenes():
            self.store.refresh_layout()
        self._fill_hidden_tree()
        self._fill_order_trees_silently()


__all__ = ["SettingsDialog", "dialog_qss"]
