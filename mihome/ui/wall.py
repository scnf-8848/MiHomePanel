"""房间墙：首页。

结构照原版米家：**按房间分组的卡片墙**。不是"顶部一排房间标签 + 一个混合网格"
——那种做法会让人失去空间感。

房间标题只在**造物**上，右侧不挂任何批量按钮。以前挂过常驻的「全开 / 全关」，
但一个房间里的品类太杂：客厅一刀切会把摄像头、路由器、净化器一起关掉，
传感器房间更是没有"开关"这个概念。真要一键，用顶部的场景栏——场景是用户自己
定义过语义的，点错的风险由用户自己控制。

工程上有三条约束：

* 卡片对象必须复用。轮询每 3 秒回来一次，如果每次重建 QWidget，滚动位置和
  鼠标悬停都会被打断，长跑还会内存抖动。
* 房间分组只在**拓扑变化**时重建。设备状态变化走 ``update_cards(dids)``。
* 搜索过滤不能重建卡片，只切 ``setVisible``；``FlowLayout`` 会自动跳过隐藏项。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core.camera import is_camera
from ..core.store import DeviceState, RoomView, Store
from . import theme
from .camera_card import CameraCard
from .camera_view import CameraView
from .camera_wall import WallCameraController
from .card_grid import CardGridLayout
from .card_templates import available_styles, resolve_card_style
from .device_card import DeviceCard
from .dnd import DragGesture
from .menu import Menu, add_choice, submenu
from .scene_bar import SceneBar
from .widgets import IconProvider, draw_text


def matches(state: DeviceState, query: str) -> bool:
    """设备名 / 房间 / 品类 / 型号 / 按键名 任一命中即算匹配。"""
    if not query:
        return True
    fields = [state.name, state.room, state.card.profile.label, state.node.model]
    fields += [g.label for g in state.card.gangs]
    return any(query in f.lower() for f in fields if f)


class _RoomHeader(QWidget):
    """房间标题行：房间名（已隐藏设备分组后面再跟台数）。

    高度 38 是给它自己和卡片流之间留出的呼吸位——卡片墙密，分区之间必须
    有一行"不说话"的标题，否则整面墙会读成一张网格。

    ``collapsible`` 只有首页末尾的「已隐藏设备」分区用：那一组是"留个后路"的
    东西，平时不想看，收起后只留这一行。整行可点，disclosure 三角（收起 ▶ /
    展开 ▼）**紧跟在标题后面**——放最右边离标题隔着一整行空白，没人会想到
    那一行能点，也就谈不上"发现得了"。
    """

    H = 38
    #: 三角的半边长 / 厚度 / 与标题之间 / 与台数之间
    _CHEV_HALF = 4.5
    _CHEV_DEPTH = 2.6
    _CHEV_GAP = 8
    _NOTE_GAP = 8

    toggled = Signal()

    def __init__(
        self,
        view: RoomView,
        palette: theme.Palette,
        note: str = "",
        *,
        collapsible: bool = False,
        collapsed: bool = False,
    ) -> None:
        super().__init__()
        self._p = palette
        self._room = view.room
        self._note = note
        self._subtle = view.kind == "hidden"
        self._collapsible = collapsible
        self._collapsed = collapsed
        self._hover = False
        self.setFixedHeight(self.H)
        # 可点的行得自己接鼠标；不可点的照旧透传，别去挡下面卡片的事件
        self.setAttribute(Qt.WA_TransparentForMouseEvents, not collapsible)
        if collapsible:
            self.setCursor(Qt.PointingHandCursor)

    def set_collapsed(self, collapsed: bool) -> None:
        if self._collapsed == collapsed:
            return
        self._collapsed = collapsed
        self.update()

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.update()

    def set_note(self, note: str) -> None:
        if self._note == note:
            return
        self._note = note
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        # 用 release 而不是 press：按下后拖开再松手不该触发折叠
        if self._collapsible and event.button() == Qt.LeftButton \
                and self.rect().contains(event.position().toPoint()):
            self.toggled.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def enterEvent(self, event) -> None:  # noqa: N802
        if self._collapsible:
            self._hover = True
            self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._collapsible:
            self._hover = False
            self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self._p
        size = theme.FS_TAB if self._subtle else theme.FS_SECTION
        color = pal.text_mute if self._subtle else pal.text
        if self._hover:
            color = pal.text_soft  # 悬停提亮一档，暗示整行可点

        # 三角和台数都跟着标题走：先量出标题宽度，再依次排下去
        font = QFont(p.font())
        font.setPixelSize(size)
        font.setWeight(QFont.DemiBold)
        title_w = QFontMetrics(font).horizontalAdvance(self._room)
        title_rect = QRect(2, 0, max(0, self.width() - 4), self.height())
        x = 2 + title_w
        if self._collapsible:
            title_rect.setWidth(max(0, min(title_rect.width(), title_w + 2)))
            x += self._CHEV_GAP
            self._paint_chevron(p, pal, x)
            x += 2 * self._CHEV_HALF
        draw_text(p, title_rect, self._room, size=size, color=color,
                  weight=QFont.DemiBold)
        if self._note:
            note_x = x + (self._NOTE_GAP if self._collapsible else 6)
            draw_text(p, QRect(note_x, 0, max(0, self.width() - note_x - 8),
                               self.height()),
                      self._note, size=11, color=pal.text_mute)
        p.end()

    def _paint_chevron(self, p: QPainter, pal: theme.Palette, x: float) -> None:
        """紧跟标题的 disclosure 三角：收起 ▶ / 展开 ▼。"""
        color = pal.text_soft if self._hover else pal.text_mute
        p.setPen(QPen(QColor(color), 1.7, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        cx = x + self._CHEV_HALF
        cy = self.height() / 2
        half, depth = self._CHEV_HALF, self._CHEV_DEPTH
        path = QPainterPath()
        if self._collapsed:  # ▶
            path.moveTo(QPointF(cx - depth, cy - half))
            path.lineTo(QPointF(cx + depth, cy))
            path.lineTo(QPointF(cx - depth, cy + half))
        else:  # ▼
            path.moveTo(QPointF(cx - half, cy - depth))
            path.lineTo(QPointF(cx, cy + depth))
            path.lineTo(QPointF(cx + half, cy - depth))
        p.drawPath(path)


class _GroupLabel(QWidget):
    """「已隐藏设备」分区内部的房间小标题（如「客厅 · 3 台」）。

    隐藏设备常常来自好几个房间，而智能开关 / 音箱这类设备在不同房间里名字
    完全一样，混在一张流里根本认不出哪个是哪个。这行只做定位，不交互。
    """

    H = 20

    def __init__(self, text: str, palette: theme.Palette) -> None:
        super().__init__()
        self._text = text
        self._p = palette
        self.setFixedHeight(self.H)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        draw_text(p, self.rect().adjusted(2, 0, -8, 0), self._text, size=12,
                  color=self._p.text_soft, weight=QFont.DemiBold)
        p.end()


def _rooms_in_order(devices) -> list[str]:
    """按首次出现顺序列出这批设备所属的房间。

    隐藏分组的顺序来自 ``prefs.order_hidden``（用户拖过的在前），用首现顺序
    分组就自然沿用了那个顺序，不用再单独排一次房间。
    """
    seen: list[str] = []
    for d in devices:
        room = d.node.room or "未分组"
        if room not in seen:
            seen.append(room)
    return seen


class RoomSection(QWidget):
    """一个房间：标题行 + 卡片流。

    「已隐藏设备」分区会**再按房间分一层**：隐藏设备往往来自好几个房间，
    智能开关 / 音箱这类名字在不同房间里一模一样，平铺在一张流里认不出来。
    只有一个房间时不分组——多一行标题纯属噪音。
    """

    #: 用户点了标题行（只有可折叠的「已隐藏设备」分区会发）
    collapse_changed = Signal(bool)

    def __init__(
        self,
        view: RoomView,
        *,
        palette: theme.Palette,
        note: str = "",
        collapsed: bool = False,
    ) -> None:
        super().__init__()
        self._p = palette
        self.view = view
        self.cards: dict[str, DeviceCard] = {}
        self._note = note
        self._collapsible = view.kind == "hidden"
        self._collapsed = bool(collapsed) and self._collapsible
        #: 每张卡片流；非分组分区只有一条
        self.flows: list[CardGridLayout] = []
        #: 房间 → (小标题, 卡片流宿主, 卡片流)，只有分组时才有
        self._groups: dict[str, tuple[_GroupLabel, QWidget, CardGridLayout]] = {}

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(12)

        self.header = _RoomHeader(view, palette, note,
                                  collapsible=self._collapsible,
                                  collapsed=self._collapsed)
        self.header.toggled.connect(self._on_header_clicked)
        col.addWidget(self.header)

        body = QWidget()
        self._body = body
        rooms = _rooms_in_order(view.devices) if self._collapsible else []
        if len(rooms) > 1:
            outer = QVBoxLayout(body)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.setSpacing(4)
            for room in rooms:
                count = sum(1 for d in view.devices
                            if (d.node.room or "未分组") == room)
                label = _GroupLabel(f"{room} · {count} 台", palette)
                host = QWidget()
                # 卡片流要能把 heightForWidth 传给父布局，否则分组后高度算不出来
                policy = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
                policy.setHeightForWidth(True)
                host.setSizePolicy(policy)
                # 卡片间距 8dp：米家首页网格的实测值（见风格参考 3.2）
                flow = CardGridLayout(host, margin=0, h_gap=8, v_gap=8)
                outer.addWidget(label)
                outer.addWidget(host)
                self._groups[room] = (label, host, flow)
                self.flows.append(flow)
            outer.addStretch(1)
        else:
            # 卡片间距 8dp：米家首页网格的实测值（见风格参考 3.2）
            self.flows.append(CardGridLayout(body, margin=0, h_gap=8, v_gap=8))
        col.addWidget(body)
        self._body.setVisible(not self._collapsed)

    @property
    def flow(self) -> CardGridLayout:
        """主卡片流（非分组分区就是唯一那一条）。"""
        return self.flows[0]

    @property
    def draggable(self) -> bool:
        """只有普通房间的单一卡片流支持拖动排序。

        分组（已隐藏设备）分区里一条 flow 只装一个房间的一部分，跨组拖动
        会让"顺序"和"房间归属"两件事混在一起；等隐藏列表也有独立排序入口
        再放开。搜索过滤期间顺序不可见，同样不允许拖。
        """
        return not self._groups and self.view.kind != "hidden"

    # -- 折叠 ------------------------------------------------------------
    @property
    def collapsible(self) -> bool:
        return self._collapsible

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def set_collapsed(self, collapsed: bool) -> None:
        """只改界面，**不落盘**。

        写偏好是 ``WallView`` 的事：搜索时为了让人看到命中项会临时展开，
        那种展开不该被记住。
        """
        collapsed = bool(collapsed) and self._collapsible
        if self._collapsed == collapsed:
            return
        self._collapsed = collapsed
        self._body.setVisible(not collapsed)
        self.header.set_collapsed(collapsed)
        self.sync_note()

    def sync_note(self) -> None:
        """标题后面常驻台数（展开 / 收起都一样）。

        收起时它就是"里面还有几台"的唯一线索；展开时也能一眼看出这一组有多
        少东西。原来收起态才显示、且附带一句「可在设置里恢复」——现在这组里
        右键就能取消隐藏，那句话只会把人往设置页支，删掉。
        """
        if self._collapsible:
            self.header.set_note(f"{len(self.cards)} 台")

    def _on_header_clicked(self) -> None:
        self.set_collapsed(not self._collapsed)
        self.collapse_changed.emit(self._collapsed)

    def add_card(self, card: DeviceCard) -> None:
        self.cards[card.state.did] = card
        room = card.state.room or "未分组"
        group = self._groups.get(room)
        (group[2] if group is not None else self.flow).addWidget(card)

    def invalidate_flows(self) -> None:
        """所有卡片流重排（改样式、搜索过滤后都要走这一下）。"""
        for flow in self.flows:
            flow.invalidate()
        self._body.updateGeometry()

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.header.set_palette(palette)
        for label, _host, _flow in self._groups.values():
            label.set_palette(palette)

    def clear_filter(self) -> None:
        """撤掉搜索：所有卡片和房间小标题都回来。"""
        for card in self.cards.values():
            card.setVisible(True)
        for label, host, _flow in self._groups.values():
            label.setVisible(True)
            host.setVisible(True)
        self.invalidate_flows()

    def apply_filter(self, query: str) -> int:
        """按查询词显示/隐藏卡片，返回可见卡片数。"""
        shown = 0
        per_room: dict[str, int] = {}
        for card in self.cards.values():
            hit = matches(card.state, query)
            card.setVisible(hit)
            if hit:
                shown += 1
                room = card.state.room or "未分组"
                per_room[room] = per_room.get(room, 0) + 1
        # 没有命中项的房间整块消失，不留一行孤零零的小标题
        for room, (label, host, _flow) in self._groups.items():
            on = per_room.get(room, 0) > 0
            label.setVisible(on)
            host.setVisible(on)
        if shown:
            self.invalidate_flows()
        return shown


class WallView(QScrollArea):
    """可滚动的房间墙。"""

    card_clicked = Signal(str)
    card_toggled = Signal(str, int, int)
    scene_run = Signal(str)
    hide_requested = Signal(str, bool)  # did, 是否隐藏
    scene_hide_requested = Signal(str, bool)  # scene_id, 是否隐藏
    card_value_committed = Signal(str, int, int, object)  # did, siid, piid, value
    card_style_requested = Signal(str, str)  # did, 样式 id（宽度由样式自带）

    def __init__(self, store: Store, icons: IconProvider, palette: theme.Palette) -> None:
        super().__init__()
        self._store = store
        self._icons = icons
        self._p = palette
        self._sections: list[RoomSection] = []
        self._cards: dict[str, DeviceCard] = {}
        self._home = ""
        self._filter = ""
        self._no_match: QWidget | None = None

        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.scene_bar: SceneBar | None = None

        self._root = QWidget()
        self._root.setObjectName("Root")
        self._col = QVBoxLayout(self._root)
        self._col.setContentsMargins(28, 4, 28, 28)
        self._col.setSpacing(24)
        self.setWidget(self._root)

        # 摄像头：同一时刻只允许一张卡播放，取址在工作线程里
        self._camera_ctl = WallCameraController(store, self)
        # 滚出可视区域就停播（滚动条动一下就检查，成本只是一次矩形相交）
        self.verticalScrollBar().valueChanged.connect(self._check_active_visible)

        # 拖动排序的状态。事件在每张卡片上过滤，真正移动的是布局里的项顺序。
        self._drag_gesture = DragGesture()
        self._drag_card: DeviceCard | None = None
        self._drag_section: RoomSection | None = None
        self._drag_origin = -1
        self._drag_started = False
        self._drag_ghost: QLabel | None = None
        self._drag_grab = QPoint()

    # -- 构建 ------------------------------------------------------------
    def _clear(self) -> None:
        self._cancel_drag()
        # 卡片马上要被 deleteLater，先把墙上的取流会话和播放器收掉
        self._camera_ctl.stop_active("重建卡片墙")
        while self._col.count():
            item = self._col.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._sections.clear()
        self._cards.clear()
        self.scene_bar = None
        self._no_match = None

    def build(self) -> None:
        """拓扑 / 家庭 / 排序变化后重建整个墙。"""
        self._clear()

        scenes = self._store.scenes_for(self._home) if self._home else self._store.scenes
        if scenes:
            self.scene_bar = SceneBar(self._p)
            self.scene_bar.scene_run.connect(self.scene_run)
            self.scene_bar.scene_menu.connect(self._on_scene_menu)
            # 先入布局再加内容：set_scenes 末尾会 setVisible(True)，此时若还没有父控件，
            # Qt 会把它当成独立顶层窗口先 show 一次——切主题重建时就是那一闪的小窗。
            self._col.addWidget(self.scene_bar)
            self.scene_bar.set_scenes(scenes)

        for view in self._store.rooms(self._home or None):
            # 只有「已隐藏设备」分区可折叠；其它房间的标题行照旧是一行静字
            collapsed = view.kind == "hidden" and self._store.prefs.hidden_collapsed
            section = RoomSection(view, palette=self._p, collapsed=collapsed)
            section.collapse_changed.connect(self._on_section_collapsed)
            for st in view.devices:
                cls = (
                    CameraCard
                    if is_camera(st.node.model, st.card.category)
                    else DeviceCard
                )
                card = cls(
                    st,
                    self._icons,
                    self._p,
                    resolve_card_style(self._store.prefs, st.did, st.card),
                )
                if isinstance(card, CameraCard):
                    card.play_requested.connect(
                        lambda c=card: self._camera_ctl.request_play(c)
                    )
                    card.view_state_changed.connect(
                        lambda state, c=card: self._camera_ctl.on_view_state(c, state)
                    )
                card.clicked.connect(self.card_clicked)
                card.toggled.connect(self.card_toggled)
                card.menu_requested.connect(self._on_card_menu)
                card.value_committed.connect(self.card_value_committed)
                card.installEventFilter(self)
                section.add_card(card)
                self._cards[st.did] = card
            # 卡片装完才知道台数，标题后面的「N 台」要在这里补一次
            section.sync_note()
            self._col.addWidget(section)
            self._sections.append(section)

        if not self._sections:
            self._col.addWidget(self._placeholder("没有找到设备"))
        else:
            self._col.addStretch(1)
        if self._filter:
            self.set_filter(self._filter)

    def _relayout(self) -> None:
        """让根布局重新分配各分区的高度。

        ``_body`` 是 ``CardGridLayout``（带 ``heightForWidth``）的宿主，收起时把它
        hide 掉会切断那条链；父 ``QVBoxLayout`` 不会自己重算——实测收起后分区
        仍然保持展开时的高度，只是卡片不画了，下面多出一大片空白。必须显式
        ``activate()``。
        """
        self._col.invalidate()
        self._col.activate()

    def _on_section_collapsed(self, collapsed: bool) -> None:
        """「已隐藏设备」分区收起 / 展开：只落盘，**不重建墙**。

        变化只发生在这一个 section 内部，重建整墙会把滚动位置、鼠标悬停和
        别的分区状态一起丢掉。
        """
        self._store.prefs.set_hidden_collapsed(collapsed)
        self._relayout()
        self._check_active_visible()

    def _on_card_menu(self, did: str, pos: QPoint) -> None:
        """打开详情 / 卡片样式 / 隐藏设备。

        样式是**每台设备各自**的选择，按 did 落进 ``prefs.card_styles``；宽度
        由样式自带，菜单里直接标出「小卡 1×1 / 宽卡 2×1」，不再单列「大小」。
        子菜单只列这台设备能力上真的能用的样式——灯才有亮度 / 色温，传感器
        只有「标准」，不列一堆点了没反应的灰项。第一项就是默认，所以也不再
        需要单独的「恢复默认样式」。
        """
        hidden = self._store.prefs.is_hidden(did)
        st = self._store.devices.get(did)
        menu = Menu(self, self._p)
        act_open = menu.addAction("打开详情")
        menu.addSeparator()

        if st is not None:
            current = resolve_card_style(self._store.prefs, did, st.card)
            style_menu = submenu(menu, "卡片样式", self._p)
            choices = available_styles(st.card)
            for preset in choices:
                add_choice(style_menu, preset.menu_label, ("style", preset.id),
                           ("style", current.id), self._p)
            if len(choices) <= 1:
                hint = style_menu.addAction("该品类暂无其它样式")
                hint.setEnabled(False)
        menu.addSeparator()
        act_hide = menu.addAction("取消隐藏" if hidden else "隐藏设备")

        chosen = menu.exec(pos)  # 子菜单里被点的 action 也会作为返回值
        if chosen is None:
            return
        if chosen is act_open:
            self.card_clicked.emit(did)
        elif chosen is act_hide:
            self.hide_requested.emit(did, not hidden)
        else:
            data = chosen.data()
            if isinstance(data, tuple) and len(data) == 2 and data[0] == "style":
                self.card_style_requested.emit(did, str(data[1]))

    def _on_scene_menu(self, scene_id: str, pos: QPoint) -> None:
        hidden = self._store.prefs.is_scene_hidden(scene_id)
        menu = Menu(self, self._p)
        act_run = menu.addAction("执行场景")
        menu.addSeparator()
        act_hide = menu.addAction("恢复显示" if hidden else "隐藏场景")
        chosen = menu.exec(pos)
        if chosen is act_run:
            self.scene_run.emit(scene_id)
        elif chosen is act_hide:
            self.scene_hide_requested.emit(scene_id, not hidden)

    def _placeholder(self, text: str) -> QWidget:
        lab = QLabel(text)
        lab.setAlignment(Qt.AlignCenter)
        lab.setFixedHeight(180)
        lab.setStyleSheet(f"color:{self._p.text_mute}; font-size:14px;")
        return lab

    def show_placeholder(self, text: str) -> None:
        """数据还没回来时的空状态。"""
        self._clear()
        self._col.addWidget(self._placeholder(text))
        self._col.addStretch(1)

    # -- 搜索 ------------------------------------------------------------
    def set_filter(self, text: str) -> None:
        q = (text or "").strip().lower()
        self._filter = q
        if self.scene_bar is not None:
            self.scene_bar.setVisible(not q)
        if not q:
            for section in self._sections:
                section.setVisible(True)
                # 搜索时的临时展开到这里收回，恢复到用户自己选的折叠状态
                if section.collapsible:
                    section.set_collapsed(self._store.prefs.hidden_collapsed)
                section.clear_filter()
            self._show_no_match(False)
            self._relayout()
            self._check_active_visible()
            return
        visible = 0
        for section in self._sections:
            # 收起的「已隐藏设备」里也可能有命中项：搜索期间临时展开，否则
            # 搜到了却看不见。这个展开不写偏好，清空搜索后会自己收回去。
            if section.collapsible:
                section.set_collapsed(False)
            shown = section.apply_filter(q)
            section.setVisible(shown > 0)
            visible += shown
        self._show_no_match(visible == 0 and bool(self._sections))
        self._relayout()
        # 搜索期间一律停播（顺序和可见性都在变，继续播没有意义）
        self._check_active_visible()

    def _show_no_match(self, on: bool) -> None:
        """搜不到东西时给一句提示，而不是留一片空白。"""
        if on and self._no_match is None:
            w = self._placeholder("没有匹配的设备")
            self._col.insertWidget(max(0, self._col.count() - 1), w)
            self._no_match = w
        elif not on and self._no_match is not None:
            self._no_match.setParent(None)
            self._no_match.deleteLater()
            self._no_match = None

    @property
    def filter_active(self) -> bool:
        return bool(self._filter)

    # -- 增量刷新 --------------------------------------------------------
    def update_cards(self, dids: set[str]) -> None:
        for did in dids:
            card = self._cards.get(did)
            st = self._store.devices.get(did)
            if card is not None and st is not None:
                if not st.online:
                    # 离线卡片不能继续占着观看位；先让控制器把会话和播放器收掉
                    self._camera_ctl.stop_if_card(card, "设备离线")
                card.refresh(st)

    def apply_card_style(self, did: str) -> None:
        """右键改完样式后**只**刷新这一张卡。

        设备集合和顺序都没变，重建整面墙会把滚动位置、其它卡上的悬停、
        甚至正在拖动的滑条一起丢掉；这里只换样式再让 CardGridLayout 重排一次。
        """
        card = self._cards.get(did)
        st = self._store.devices.get(did)
        if card is None or st is None:
            return
        # 2×2 实时画面的卡片换样式时，正在播的那一路要先停掉
        self._camera_ctl.stop_if_card(card, "改卡片样式")
        card.set_style(resolve_card_style(self._store.prefs, did, st.card))
        for section in self._sections:
            if did in section.cards:
                section.invalidate_flows()
                break
        # 这里只标记失效、不强制 activate：卡片自己的 CardGridLayout 会重排，
        # 立刻重算根布局反而会把正在拖动的滑条/滚动位置带着抖一下。
        self._col.invalidate()

    # -- 摄像头生命周期 ---------------------------------------------------
    def stop_playback(self, reason: str = "") -> None:
        """墙面上任何"看不见了"的动作都走这里：停播 + 放播放器 + 归还观看位。"""
        self._camera_ctl.stop_active(reason)

    def hand_off_active_to(self, target: CameraView | None, did: str) -> bool:
        """把墙上正在播的这一路交给详情页（同一个播放器，不重连）。

        返回 False 表示没交出去（没在播 / 播的是别的设备 / 目标控件不可用），
        调用方应退回"先停再重开"。逻辑在 ``WallCameraController`` 里，这里只是
        和 ``stop_playback`` 一样开个门——主窗口拿的是 ``WallView``，不持有 controller。
        """
        return self._camera_ctl.hand_off_active_to(target, did)

    def _check_active_visible(self) -> None:
        card = self._camera_ctl.active_card
        if card is None:
            return
        if not card.isVisible() or self._filter:
            self._camera_ctl.stop_active("卡片不可见")
            return
        vp = self.viewport().rect()
        top_left = card.mapTo(self.viewport(), QPoint(0, 0))
        if not vp.intersects(QRect(top_left, card.size())):
            self._camera_ctl.stop_active("滚出可视区域")

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # 窗口变窄可能触发 2×2 → 1×1 的降级；播放中的卡片若因此不可见也要收
        self._check_active_visible()

    # -- 拖动排序 ---------------------------------------------------------
    def _section_of(self, card: DeviceCard) -> RoomSection | None:
        for section in self._sections:
            if card.state.did in section.cards:
                return section
        return None

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if not isinstance(obj, DeviceCard):
            return super().eventFilter(obj, event)
        et = event.type()
        if et == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            self._on_drag_press(obj, event)
        elif et == QEvent.MouseMove and self._drag_card is obj:
            if self._on_drag_move(obj, event):
                return True
        elif et == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
            if self._drag_card is obj:
                if self._drag_started:
                    self._finish_drag(commit=True)
                    return True
                self._cancel_drag()
        return super().eventFilter(obj, event)

    def _on_drag_press(self, card: DeviceCard, event) -> None:
        section = self._section_of(card)
        usable = (
            section is not None
            and section.draggable
            and not self._filter
            and not self._drag_started
            and not card.interactive_at(event.position().toPoint())
        )
        if not usable:
            self._cancel_drag()
            return
        self._drag_card = card
        self._drag_section = section
        self._drag_origin = section.flow.index_of(card)
        self._drag_started = False
        self._drag_gesture.press(event.globalPosition().toPoint())

    def _on_drag_move(self, card: DeviceCard, event) -> bool:
        global_pos = event.globalPosition().toPoint()
        if not self._drag_started:
            if not self._drag_gesture.move(global_pos):
                return False
            self._begin_drag(card, global_pos)
            return True
        self._update_drag(global_pos)
        return True

    def _begin_drag(self, card: DeviceCard, global_pos: QPoint) -> None:
        self._drag_started = True
        # 拖动时画面没有意义，而且会一直占着观看位
        self._camera_ctl.stop_active("拖动排序")
        pix = card.grab()
        card_pos = card.mapTo(self.viewport(), QPoint(0, 0))
        vp_pos = self.viewport().mapFromGlobal(global_pos)
        self._drag_grab = vp_pos - card_pos
        ghost = QLabel(self.viewport())
        ghost.setPixmap(pix)
        ghost.setAttribute(Qt.WA_TransparentForMouseEvents)
        ghost.setGeometry(card_pos.x(), card_pos.y(), card.width(), card.height())
        ghost.show()
        ghost.raise_()
        self._drag_ghost = ghost
        # 被拖的卡片自己变成占位框：继续占 span×rows 个格子，网格不会塌
        card.set_drag_placeholder(True)
        card.setCursor(Qt.ClosedHandCursor)

    def _update_drag(self, global_pos: QPoint) -> None:
        if self._drag_card is None or self._drag_section is None:
            return
        vp = self.viewport()
        vp_pos = vp.mapFromGlobal(global_pos)
        if self._drag_ghost is not None:
            self._drag_ghost.move(vp_pos - self._drag_grab)
        # 靠近上下边缘时自动滚，长房间也能把卡拖到屏外
        bar = self.verticalScrollBar()
        if vp_pos.y() < 28:
            bar.setValue(bar.value() - 24)
        elif vp_pos.y() > vp.height() - 28:
            bar.setValue(bar.value() + 24)
        flow = self._drag_section.flow
        host = flow.parentWidget()
        if host is None:
            return
        flow_pos = host.mapFrom(vp, vp_pos)
        source = self._drag_card
        target = flow.drop_index(flow_pos, source)
        current = flow.index_of(source)
        if target != current and flow.move_item(current, target):
            flow.invalidate()
            flow.activate()
            self._relayout()

    def _finish_drag(self, *, commit: bool) -> None:
        card, section = self._drag_card, self._drag_section
        origin = self._drag_origin
        if card is None:
            self._cancel_drag()
            return
        card.set_drag_placeholder(False)
        card.unsetCursor()
        if self._drag_ghost is not None:
            self._drag_ghost.hide()
            self._drag_ghost.deleteLater()
            self._drag_ghost = None
        if section is not None:
            flow = section.flow
            if not commit:
                # 取消：把布局项挪回按下时的位置，偏好一点都不写
                current = flow.index_of(card)
                if 0 <= origin < len(flow.widgets()) and current != origin:
                    flow.move_item(current, origin)
            else:
                dids = [
                    w.state.did
                    for w in flow.widgets()
                    if isinstance(w, DeviceCard)
                ]
                if dids:
                    self._store.set_device_order(
                        section.view.home,
                        section.view.room,
                        dids,
                        notify=False,
                    )
            flow.invalidate()
            flow.activate()
        self._drag_gesture.reset()
        self._drag_card = None
        self._drag_section = None
        self._drag_origin = -1
        self._drag_started = False
        self._relayout()

    def _cancel_drag(self) -> None:
        if self._drag_started:
            self._finish_drag(commit=False)
            return
        self._drag_gesture.reset()
        self._drag_card = None
        self._drag_section = None
        self._drag_origin = -1
        self._drag_started = False

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape and self._drag_started:
            self._finish_drag(commit=False)
            event.accept()
            return
        super().keyPressEvent(event)

    def scroll_to_room(self, home: str, room: str) -> None:
        """滚到指定家庭的指定房间。``room == "*"`` 回到顶部。"""
        bar = self.verticalScrollBar()
        if not room or room == "*":
            bar.setValue(0)
            return
        for section in self._sections:
            if section.view.room == room and (not home or section.view.home == home) and section.isVisible():
                y = section.mapTo(self._root, QPoint(0, 0)).y()
                bar.setValue(max(0, y - 10))
                return

    def set_home(self, home: str) -> None:
        changed = home != self._home
        self._home = home
        self.build()
        if changed:
            self.verticalScrollBar().setValue(0)

    def home(self) -> str:
        return self._home

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        if self.scene_bar is not None:
            self.scene_bar.set_palette(palette)
        # body 里的滑条是真实子控件，不会跟着外壳的重绘换色
        for card in self._cards.values():
            card.set_palette(palette)
        # 分区标题 / 房间小标题同样是自绘的
        for section in self._sections:
            section.set_palette(palette)

    def rebuild_palette(self, palette: theme.Palette) -> None:
        """切主题：卡片是自绘的，换色最省事的方式是整墙重建一次。"""
        self.set_palette(palette)
        if self._cards:
            self.build()


__all__ = ["WallView", "matches"]
