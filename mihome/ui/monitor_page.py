# SPDX-License-Identifier: GPL-3.0-or-later
"""监控页：多路摄像头同屏。布局参照 DD_Monitor_CE 的"画面墙 + 左侧选源"。

三条设计（2026-11-19 用户定）：

1. **预设布局，不做自由划格**：单窗口 / 左右两分 / 四宫格 / 一大两小 / 一大五小。
   几何只有这一张表（``LAYOUT_SPECS``）——"主"用跨行跨列表达，不写第二套算法。
2. **左侧一列缩略图**，拖到右边某个格子就换那一路；单击则放进第一个空格。
   缩略图是**播过的那一帧**（``ui/thumbs.py``，模糊处理），不额外取流——否则
   6 路同屏再加 6 路缩略图，软解 CPU 直接翻倍。
3. **拖动是手写状态机 + ``DragGuard``**，与卡片墙、耗材页同一套（Qt 原生 DnD 在
   无边框窗口里松手丢事件时没有兜底）。

**进页面不自动播**（2026-11-19 用户定）：格子里放"上次那一帧的模糊图 + 播放键"，
点了才取流。因为监控页是"进来看一眼"的页面，一进去就 6 路同时软解，风扇会先响起来；
而"上次那一帧"已经足够认出是哪台、画面里大概有什么。代价是点播放要等一下，所以
进来时先把**源和地址**备好（``prepare_streams`` + ``prefetch_urls``）：go2rtc 里
源已登记（不用为第一路重启进程）、RTSP 地址已到手（不必等一轮取址），点下去只剩
播放器连流这一件事。

封面怎么来：播放中每 ``THUMB_REFRESH_SECONDS`` 秒把当前帧模糊存一次；停播 / 换布局 /
关页面之前，把最后一帧存一次。落盘所以在重启程序后仍然有（``cache/thumbs/``）。

为什么不是"把卡片墙 2×2 放大"：卡片墙是**按房间浏览设备**，一张卡一件事；监控页是
**同时看几路画面**，左右两边（选源 / 画面）是两种不同的交互，混在一起会把"看画面"
变成"找卡片"。所以它有独立页面、独立布局与独立的播放调度。

取流：进页面时把当前家庭**全部摄像头的源一次性登记进 go2rtc**
（``CameraService.prepare``），之后每格只是拿地址——因为改 go2rtc 配置要重启进程，
重启会掐断正在播的流（详见 ``core/camera.py`` 的 ``prepare``）。

已知边界（都在 ``docs/dev/camera.md`` 的「多路同屏」一节）：
每台设备仍只有 3 个观看位（我们每台只开 1 路，不受影响）；软解每路约 40% 单核，
所以上限 6 格；局域网失败**不自动切远程**（6 路各自回落的复杂度不值），失败时格子里
给重试提示。
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core.camera import CameraError, is_camera
from ..core.camera import shared as camera_service
from ..core.prefs import MONITOR_LAYOUT_DEFAULT, MONITOR_LAYOUTS
from ..core.store import DeviceState, Store
from . import theme
from .camera_view import SURFACE_MONITOR, CameraView, playbacks
from .combo import Combo
from .dnd import DragGuard
from .thumbs import THUMB_H, THUMB_W, ThumbCache
from .wall import DragGhost, rounded_shot

PAGE_MARGIN = 28
#: 选源卡片的内边距（左右各一份；上下同值）
RAIL_PAD = 8
#: 左侧选源栏宽度：缩略图 + 卡片内边距 + 边框 + 滚动条。滚动条是全局样式里的
#: 10px（见 ``theme``）：摄像头多到要滚动时它会占掉 10px，不预留就会把缩略图
#: 挤掉一点——所以这里按"一直有滚动条"留够宽度。
RAIL_W = THUMB_W + RAIL_PAD * 2 + 2 + 20
#: 格子间距。比卡片墙（12）小：画面之间只需要一条分界，不需要呼吸位
CELL_GAP = 8
#: 按下后移动超过这个距离才算"拖动"，否则算单击（放第一个空格）
DRAG_SLOP = 6
#: 正在播的一格每隔多久把当前帧刷成封面（用户 2026-11-19 定 10 秒）。
#: 控件那边每 2 秒就会发一张（``CameraView.SNAPSHOT_INTERVAL``），这里只挑其中
#: 每 10 秒的那一张去模糊 + 写盘——6 路时落盘是 0.6 次/秒，主线程完全吃得下。
THUMB_REFRESH_SECONDS = 10.0


@dataclass(frozen=True)
class LayoutSpec:
    """一套预设：``列数 / 行数`` + 每格的 ``(row, col, rowspan, colspan)``。

    字段顺序是 ``(cols, rows)``（和 ``LayoutSpec("左右两分", 2, 1, ...)`` 读起来一致）。
    ``cols`` / ``rows`` 只用来设拉伸，必须**覆盖到格子的最大下标**——多设一列就会
    多出一条空白，少设一列则最后一列挤在一起。

    ``col_stretch`` / ``row_stretch`` 是**每一列 / 每一行的宽度比例**，缺省全是 1
    （等分）。"一大两小"要主窗宽是小窗的两倍，就写 ``col_stretch=(2, 1)``——拉伸比
    是 QGridLayout 唯一能表达"这列更宽"的途径，跨列做不到（跨列只是合并格子，
    宽度仍由列拉伸决定）。
    """

    label: str
    cols: int
    rows: int
    cells: tuple[tuple[int, int, int, int], ...]
    col_stretch: tuple[int, ...] = ()
    row_stretch: tuple[int, ...] = ()


#: 五种预设。"主"用**跨行跨列**表达（1+5 是 3×3 里左上 2×2，1+2 是左列跨两行），
#: 需要不成比例的宽度再补 ``col_stretch``——一套网格描述全部，加新预设只是往这里
#: 加一行，页面代码不用动。
LAYOUT_SPECS: dict[str, LayoutSpec] = {
    "1x1": LayoutSpec("单窗口", 1, 1, ((0, 0, 1, 1),)),
    "1x2": LayoutSpec("左右两分", 2, 1, ((0, 0, 1, 1), (0, 1, 1, 1))),
    "2x2": LayoutSpec(
        "四宫格", 2, 2,
        ((0, 0, 1, 1), (0, 1, 1, 1), (1, 0, 1, 1), (1, 1, 1, 1)),
    ),
    # 一大两小：主窗占左列两行、宽是小窗的 2 倍（用户 2026-11-19 定）。只靠跨列
    # 拿不到 2:1——跨列不改列宽，所以这里必须配 col_stretch=(2, 1)。
    "1+2": LayoutSpec(
        "一大两小", 2, 2,
        ((0, 0, 2, 1), (0, 1, 1, 1), (1, 1, 1, 1)),
        col_stretch=(2, 1),
    ),
    # 一大五小按参考项目（DD_Monitor_CE）的画法：整块切 3×3，**左上 2×2 是主窗**，
    # 剩下 5 格（右上 / 右中 / 底行三格）各放一路。比"一大 + 右列 5 个窄条"更像
    # 常见监看台，小窗的宽高比也更接近摄像头的 16:9。
    "1+5": LayoutSpec(
        "一大五小", 3, 3,
        (
            (0, 0, 2, 2),   # 主窗：左上 2×2
            (0, 2, 1, 1),   # 右上
            (1, 2, 1, 1),   # 右中
            (2, 0, 1, 1),   # 左下
            (2, 1, 1, 1),   # 中下
            (2, 2, 1, 1),   # 右下
        ),
    ),
}

LAYOUT_LABELS = tuple((key, LAYOUT_SPECS[key].label) for key in MONITOR_LAYOUTS)


def _qss(p: theme.Palette) -> str:
    # PageTitle / PageHint 与耗材页**逐字相同**：这两条以前本页没写，标题就退回
    # 13px 正文大小，和「耗材」「电脑自动化」差一截（用户 2026-11-19 报"标题字体
    # 不一样"）。三页标题的字号、字重、颜色只认这两条，别再各页自定义。
    return f"""
    QLabel#PageTitle {{ color: {p.text}; font-size: 20px; font-weight: 700; }}
    QLabel#PageHint {{ color: {p.text_mute}; font-size: 12px; }}
    /* 选源栏的卡片：外壳画底，里面的滚动区与 viewport 一律透明，否则会把底盖住。
       底色用 card 而不是 bg_soft——深色下 bg_soft(#15171C) 和页面底(#0E0F12)
       只差一点点，围起来也看不出来；card 才是和卡片墙设备卡同一档的"这是张卡"。 */
    QFrame#RailCard {{
        background: {p.card};
        border: 1px solid {p.border};
        border-radius: {theme.RADIUS_CARD}px;
    }}
    QScrollArea#MonitorRail, QScrollArea#MonitorRail > QWidget {{
        background: transparent; border: none;
    }}
    QScrollArea#MonitorRail > QWidget > QWidget {{ background: transparent; }}
    QLabel#SourceName {{ color: {p.text}; font-size: 12px; }}
    QLabel#SourceMeta {{ color: {p.text_mute}; font-size: 11px; }}
    QLabel#CellName {{
        color: #FFFFFF; font-size: 12px; background: rgba(0, 0, 0, 108);
        border-radius: 6px; padding: 2px 8px;
    }}
    QPushButton#CellClose {{
        background: rgba(0, 0, 0, 108); color: #FFFFFF; border: none;
        border-radius: 11px; font-size: 14px;
    }}
    QPushButton#CellClose:hover {{ background: {theme.DANGER}; }}
    /* 顶栏按钮与右侧下拉排在同一行，所以用下拉那一套皮（同一个 field 底色、
       同一支 field_border 描边、同一个 9 圆角）。原先写的是"透明底 + 通用 border"，
       在深色下几乎看不出是按钮（用户 2026-11-19 报"按钮样式要调"）。高度不写在
       QSS 里——离屏与真机的字体行高差得多，交给代码按下拉的实测高度固定。 */
    QPushButton#Ghost {{
        background: {theme.field_bg(p)}; color: {p.text_soft};
        border: 1px solid {theme.field_border(p)};
        border-radius: 9px; padding: 0 16px; font-size: 13px;
    }}
    QPushButton#Ghost:hover {{
        color: {p.text};
        border-color: {theme.blend(theme.field_bg(p), p.text, 0.24 if p.is_dark else 0.20)};
    }}
    QPushButton#Ghost:pressed {{
        background: {theme.blend(theme.field_bg(p), p.bg, 0.35)};
    }}
    """


# ---------------------------------------------------------------------------
# 左侧：一路摄像头
# ---------------------------------------------------------------------------


class _Thumb(QWidget):
    """缩略图框：有缓存就画那张模糊图，没有就画一个深色底 + 镜头剪影。"""

    def __init__(self, parent: QWidget, palette: theme.Palette) -> None:
        super().__init__(parent)
        self._p = palette
        self._pix: QPixmap | None = None
        self.setFixedSize(THUMB_W, THUMB_H)

    def set_pixmap(self, pix: QPixmap | None) -> None:
        self._pix = pix
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt 回调
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, 8, 8)
        p.setClipPath(path)
        if self._pix is not None and not self._pix.isNull():
            # cover：缓存已经是 16:9，这里只兜一个像素级的边
            p.drawPixmap(self.rect(), self._pix)
        else:
            p.fillRect(self.rect(), QColor(self._p.bg_soft))
            self._lens(p, rect)
        p.setClipping(False)
        p.setPen(QPen(QColor(self._p.border), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(rect, 8, 8)
        p.end()

    def _lens(self, p: QPainter, rect: QRectF) -> None:
        """没播过时画一枚摄像头剪影：矩形机身 + 镜头圆。"""
        cx, cy = rect.center().x(), rect.center().y()
        w, h = 34.0, 22.0
        body = QRectF(cx - w / 2, cy - h / 2, w, h)
        p.setPen(QPen(QColor(self._p.text_mute), 1.4))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(body, 5, 5)
        p.drawEllipse(QRectF(cx - 6, cy - 6, 12, 12))
        p.drawLine(int(cx + 8), int(cy - 13), int(cx + 14), int(cy - 13))


class SourceItem(QWidget):
    """选源栏的一项：缩略图 + 名称 + 房间 / 在线状态。可拖动、可单击。"""

    #: 拖动阈值已过（页面接管幽灵与落点）
    drag_started = Signal(object)
    drag_moved = Signal(QPoint)
    drag_finished = Signal(object, QPoint)
    clicked = Signal(str)

    H = THUMB_H + 44

    def __init__(
        self,
        state: DeviceState,
        palette: theme.Palette,
        thumbs: ThumbCache,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._p = palette
        self.did = state.did
        self._pressing = False
        self._dragging = False
        self._press_at = QPoint()
        self.setCursor(Qt.OpenHandCursor)
        self.setToolTip(f"{state.name} · {state.room or '未分组'}")
        self.setFixedHeight(self.H)

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 4)
        col.setSpacing(4)
        self.thumb = _Thumb(self, palette)
        col.addWidget(self.thumb, 0, Qt.AlignHCenter)
        self.lbl_name = QLabel(state.name, self)
        self.lbl_name.setObjectName("SourceName")
        self.lbl_name.setFixedWidth(THUMB_W)
        col.addWidget(self.lbl_name, 0, Qt.AlignHCenter)
        self.lbl_meta = QLabel(self._meta(state), self)
        self.lbl_meta.setObjectName("SourceMeta")
        self.lbl_meta.setFixedWidth(THUMB_W)
        col.addWidget(self.lbl_meta, 0, Qt.AlignHCenter)

        self.refresh(state)

    @staticmethod
    def _meta(state: DeviceState) -> str:
        room = state.room or "未分组"
        return room if state.online else f"{room} · 离线"

    def refresh(self, state: DeviceState) -> None:
        self.lbl_name.setText(state.name)
        # 名字过长就省略：一列里出现两行会顶掉下面的项，也没有更多信息
        metrics = self.lbl_name.fontMetrics()
        self.lbl_name.setText(
            metrics.elidedText(state.name, Qt.ElideRight, THUMB_W)
        )
        self.lbl_meta.setText(self._meta(state))
        tint = self._p.text_mute if state.online else theme.DANGER
        self.lbl_meta.setStyleSheet(f"color: {tint}; font-size: 11px;")
        self.setToolTip(f"{state.name} · {state.room or '未分组'}"
                        + ("" if state.online else " · 已离线"))

    def set_thumb(self, pix: QPixmap | None) -> None:
        self.thumb.set_pixmap(pix)

    # -- 拖动 ------------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        self._pressing = True
        self._dragging = False
        self._press_at = event.position().toPoint()
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self._pressing:
            super().mouseMoveEvent(event)
            return
        delta = event.position().toPoint() - self._press_at
        if not self._dragging:
            if delta.manhattanLength() < DRAG_SLOP:
                return
            self._dragging = True
            self.setCursor(Qt.ClosedHandCursor)
            self.drag_started.emit(self)
        self.drag_moved.emit(event.globalPosition().toPoint())
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            super().mouseReleaseEvent(event)
            return
        was_dragging = self._dragging
        self._pressing = False
        self._dragging = False
        self.setCursor(Qt.OpenHandCursor)
        if was_dragging:
            self.drag_finished.emit(self, event.globalPosition().toPoint())
        else:
            self.clicked.emit(self.did)
        event.accept()

    def cancel_drag(self) -> None:
        """外部中断（切程序 / 松手丢事件）时复位，别留一个"一直按着"的项。"""
        self._pressing = False
        self._dragging = False
        self.setCursor(Qt.OpenHandCursor)


# ---------------------------------------------------------------------------
# 右侧：一格画面
# ---------------------------------------------------------------------------


class CameraCell(QWidget):
    """一格。空态 = 虚线框 + 提示；有设备 = ``CameraView`` 铺满 + 名字条。

    **有设备就一定建出 ``CameraView``**（2026-11-19 改）：它的空态是"封面 + 播放键"，
    而"停播后只剩一个黑框"和"这一格本来就没放东西"长得一模一样——用户点过"全部
    停止"再换布局，就会以为画面丢了、要重新拖。控件本身不取流，只有点了播放才有
    会话，所以多建几个不花什么。
    """

    clear_requested = Signal(object)
    zoom_requested = Signal(object)
    play_requested = Signal(object)
    #: 播放中每几秒来一张当前帧（``CameraView.snapshot`` 转出来的），页面拿它存封面
    frame_ready = Signal(object)

    def __init__(self, palette: theme.Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._p = palette
        self.did = ""
        self.state: DeviceState | None = None
        self.view: CameraView | None = None
        self._poster: QPixmap | None = None
        self._hover = False
        self._highlight = False
        self._message = ""
        self.setMinimumSize(120, 70)
        self.setMouseTracking(True)

        self.lbl_name = QLabel(self)
        self.lbl_name.setObjectName("CellName")
        self.lbl_name.hide()
        self.btn_close = QPushButton("×", self)
        self.btn_close.setObjectName("CellClose")
        self.btn_close.setCursor(Qt.PointingHandCursor)
        self.btn_close.setFixedSize(22, 22)
        self.btn_close.hide()
        self.btn_close.clicked.connect(lambda: self.clear_requested.emit(self))

    # -- 设备 ------------------------------------------------------------
    def set_device(self, state: DeviceState | None) -> None:
        self.state = state
        self.did = state.did if state is not None else ""
        self._message = ""
        if state is None:
            self._teardown()
            self.lbl_name.hide()
            self.update()
            return
        self.lbl_name.setText(state.name)
        self.lbl_name.adjustSize()
        self.lbl_name.show()
        self.ensure_view()
        self.update()

    def _teardown(self) -> None:
        view = self.view
        self.view = None
        # 封面属于"这一格放的这台设备"：换掉 / 清空时一起丢，否则会拿上一台
        # 摄像头的画面当新设备的封面
        self._poster = None
        if view is not None:
            playbacks().release(view, "格子清空")
            view.setParent(None)
            view.deleteLater()
        self.release_player_only()

    def release_player_only(self) -> None:
        """只停播、不销毁 view（切走页面时用：回来要能原地重播）。"""
        if self.view is not None:
            self.view.stop()
            self.view.release_player()

    def ensure_view(self) -> CameraView:
        if self.view is None:
            accent = theme.category_color(
                self.state.card.category if self.state is not None else "camera"
            )
            self.view = CameraView(self._p, accent, self, compact=True)
            # 整幅可见：格子宽高比跟摄像头（16:9）对不上，按 cover 画会把画面两侧
            # 裁掉一大块（1+2 / 单窗口是接近 1:1 的格子，裁掉将近一半宽度）。多路
            # 同看时"看全"比"填满"重要，留边用画面底色兜住，深色下看不出来。
            self.view.set_contain_fit(True)
            self.view.setMinimumSize(1, 1)
            self.view.set_poster(self._poster)
            self.view.play_requested.connect(lambda: self.play_requested.emit(self))
            self.view.snapshot.connect(self._on_snapshot)
            self.view.show()
            # 画面铺满整格，名字条和关闭键必须压在它上面（否则前者不可见、
            # 后者点了没反应——子控件的层叠顺序就是创建顺序）
            self.lbl_name.raise_()
            self.btn_close.raise_()
        self._place_children()
        return self.view

    def _on_snapshot(self, image) -> None:
        self.frame_ready.emit(image)

    def set_poster(self, pix: QPixmap | None) -> None:
        self._poster = pix
        if self.view is not None:
            self.view.set_poster(pix)
        self.update()

    @property
    def poster(self) -> QPixmap | None:
        return self._poster

    def stop(self) -> None:
        self._message = ""
        view = self.view
        if view is not None:
            playbacks().release(view, "用户停止")
            view.show_idle()
        self.update()

    def show_message(self, text: str) -> None:
        self._message = text
        view = self.view
        if view is not None:
            view.show_error(text)
        self.update()

    def show_progress(self, hint: str, sub: str = "") -> None:
        view = self.view
        if view is not None:
            view.show_progress(hint, sub)
        self.update()

    # -- 交互 ------------------------------------------------------------
    def set_highlight(self, on: bool) -> None:
        if on == self._highlight:
            return
        self._highlight = on
        self.update()

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.update()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.did:
            self.zoom_requested.emit(self)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.btn_close.setVisible(bool(self.did))
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.btn_close.hide()
        super().leaveEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._place_children()

    def _place_children(self) -> None:
        view = self.view
        if view is not None:
            view.setGeometry(self.rect())
        self.lbl_name.move(8, 8)
        self.lbl_name.resize(self.lbl_name.sizeHint())
        self.btn_close.move(self.width() - self.btn_close.width() - 6, 6)

    # -- 绘制 ------------------------------------------------------------
    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = theme.RADIUS_CARD
        p.setBrush(QColor(self._p.bg_soft))
        p.setPen(QPen(QColor(self._p.border), 1))
        p.drawRoundedRect(rect, radius, radius)
        if self._highlight:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(self._p.accent), 2))
            p.drawRoundedRect(rect.adjusted(1, 1, -1, -1), radius - 1, radius - 1)
        if self.did or self.view is not None:
            # 有设备但还没建出画面（离线 / 取址失败 / 已被别的窗口接管）：
            # 至少把原因写在格子里，别只留一个黑块
            if self.view is None and self._message:
                p.setPen(QColor(self._p.text_mute))
                font = QFont(self.font())
                font.setPixelSize(12)
                p.setFont(font)
                p.drawText(self.rect(), Qt.AlignCenter, self._message)
            p.end()
            return
        pen = QPen(QColor(self._p.border), 1, Qt.DashLine)
        pen.setDashPattern([4, 4])
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(rect.adjusted(6, 6, -6, -6), radius - 4, radius - 4)
        p.setPen(QColor(self._p.text_mute))
        font = QFont(self.font())
        font.setPixelSize(12)
        p.setFont(font)
        p.drawText(
            self.rect(),
            Qt.AlignCenter,
            self._message or "拖一路到这里",
        )
        p.end()


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------


@dataclass
class _Job:
    """一次取址任务（后台线程只带它需要的字段，别把 Qt 控件递进去）。"""

    did: str
    token: int
    remote: bool
    allow_remote: bool
    cell: CameraCell = field(repr=False)


class MonitorPage(QWidget):
    def __init__(
        self,
        store: Store,
        palette: theme.Palette,
        parent: QWidget | None = None,
        *,
        thumbs: ThumbCache | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("MonitorPage")
        self.setStyleSheet(_qss(palette))
        self.store = store
        self._p = palette
        self._cells: list[CameraCell] = []
        self._sources: dict[str, SourceItem] = {}
        self._thumbs = thumbs if thumbs is not None else ThumbCache()
        #: 正在取址的格子：did → token。切格 / 切页 / 停播都靠它作废旧任务
        self._pending: dict[str, int] = {}
        self._token = 0
        self._results: queue.SimpleQueue[tuple[str, int, str, str, bool]] = queue.SimpleQueue()
        #: 预热到的 RTSP 地址：did → (url, remote, go2rtc 的 epoch)。epoch 变了就
        #: 说明子进程重起过、端口可能换了，这份缓存作废（见 ``core/camera.py``）
        self._urls: dict[str, tuple[str, bool, int]] = {}
        #: 正在预热的 did → token（与 ``_pending`` 分开：预热失败不影响点播放）
        self._prefetching: dict[str, int] = {}
        self._prefetch_token = 0
        self._prefetch_results: queue.SimpleQueue[
            tuple[str, int, str, bool, int]
        ] = queue.SimpleQueue()
        #: 每台设备上次刷封面的时间（monotonic），按 ``THUMB_REFRESH_SECONDS`` 节流
        self._snap_at: dict[str, float] = {}
        self._poll = QTimer(self)
        self._poll.setInterval(80)
        self._poll.timeout.connect(self._collect)
        #: 拖动状态：被拖的项 + 幽灵 + 高亮中的格子
        self._drag_item: SourceItem | None = None
        self._ghost: DragGhost | None = None
        self._drag_cell: CameraCell | None = None
        #: 双击放大的那一格（``None`` = 正常网格）
        self._zoomed: CameraCell | None = None
        #: 当前预设 + 它的行列数。放大要按「整块」跨全部行列重排，而
        #: ``QGridLayout.rowCount()`` 在摘掉控件后不会缩回去（见 ``_build_cells``
        #: 里清拉伸那段），不能拿它当行列数用
        self._spec = LAYOUT_SPECS[MONITOR_LAYOUT_DEFAULT]
        self._grid_rows = self._spec.rows
        self._grid_cols = self._spec.cols
        self._guard = DragGuard(self)
        self._guard.aborted.connect(self._on_drag_aborted)

        col = QVBoxLayout(self)
        col.setContentsMargins(PAGE_MARGIN, 8, PAGE_MARGIN, 18)
        col.setSpacing(12)

        # -- 顶栏：标题 + 统计 + 布局 / 全部停止 -------------------------
        head = QHBoxLayout()
        head.setSpacing(10)
        title_row = QWidget(self)
        trow = QHBoxLayout(title_row)
        trow.setContentsMargins(0, 0, 0, 0)
        trow.setSpacing(10)
        title = QLabel("监控", title_row)
        title.setObjectName("PageTitle")
        trow.addWidget(title)
        self.lbl_hint = QLabel("", title_row)
        self.lbl_hint.setObjectName("PageHint")
        trow.addWidget(self.lbl_hint)
        head.addWidget(title_row, 0, Qt.AlignTop)
        head.addStretch(1)

        lbl_layout = QLabel("布局", self)
        lbl_layout.setObjectName("PageHint")
        head.addWidget(lbl_layout, 0, Qt.AlignVCenter)
        self.cb_layout = Combo(palette, self)
        # 与耗材页右上那两个下拉同宽：三页顶栏的右半部分看起来是同一排控件
        self.cb_layout.setFixedWidth(132)
        for key, label in LAYOUT_LABELS:
            self.cb_layout.addItem(label, key)
        current = store.prefs.monitor_layout or MONITOR_LAYOUT_DEFAULT
        self.cb_layout.setCurrentIndex(max(0, self.cb_layout.findData(current)))
        self.cb_layout.currentIndexChanged.connect(self._on_layout)
        head.addWidget(self.cb_layout)

        self.btn_stop = QPushButton("全部停止", self)
        self.btn_stop.setObjectName("Ghost")
        self.btn_stop.setCursor(Qt.PointingHandCursor)
        # 高度跟着下拉实测高度走，而不是在 QSS 里写 min-height：
        # 12px/13px 字在离屏与真机上的行高差很多（确认框那次量出 13 vs 32），
        # 写死数字只在一边对得上，排在同一行会一高一低。
        self.btn_stop.setFixedHeight(self.cb_layout.sizeHint().height())
        self.btn_stop.clicked.connect(self.stop_all)
        head.addWidget(self.btn_stop, 0, Qt.AlignVCenter)
        col.addLayout(head)

        # -- 主体：左选源 / 右画面 ---------------------------------------
        body = QHBoxLayout()
        body.setSpacing(12)

        # 选源栏单独围一张卡：一列缩略图直接浮在页面底色上时，分不清它是"可拖的源"
        # 还是页面背景；围起来之后左（源）右（画面）是两个区域，拖动的方向也更清楚。
        self.rail_card = QFrame(self)
        self.rail_card.setObjectName("RailCard")
        self.rail_card.setFixedWidth(RAIL_W)
        card_col = QVBoxLayout(self.rail_card)
        card_col.setContentsMargins(RAIL_PAD, RAIL_PAD, RAIL_PAD, RAIL_PAD)
        card_col.setSpacing(0)

        self.rail = QScrollArea(self.rail_card)
        self.rail.setObjectName("MonitorRail")
        self.rail.setWidgetResizable(True)
        self.rail.setFrameShape(QScrollArea.NoFrame)
        self.rail.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._rail_body = QWidget()
        self._rail_col = QVBoxLayout(self._rail_body)
        self._rail_col.setContentsMargins(0, 0, 0, 0)
        self._rail_col.setSpacing(8)
        self._rail_col.addStretch(1)
        self.rail.setWidget(self._rail_body)
        card_col.addWidget(self.rail)
        body.addWidget(self.rail_card, 0)

        self._grid_host = QWidget(self)
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(CELL_GAP)
        body.addWidget(self._grid_host, 1)
        col.addLayout(body, 1)

        self._build_cells(current)
        self.refresh()

    # -- 布局 ------------------------------------------------------------
    def _layout_id(self) -> str:
        key = self.cb_layout.currentData()
        return key if key in LAYOUT_SPECS else MONITOR_LAYOUT_DEFAULT

    def _on_layout(self, _index: int = 0) -> None:
        layout = self._layout_id()
        if layout == self.store.prefs.monitor_layout and self._cells:
            return
        self._save_cells()
        self.store.prefs.set_monitor_layout(layout)
        # handoff=True：正在播的那几路把播放器原样搬给新格子，不重新取址。
        # 换布局只是换一种看法，画面不该黑一下再重新连（2026-11-19 用户提的）。
        self._build_cells(layout, handoff=True)
        self.refresh()

    def _build_cells(self, layout: str, *, handoff: bool = False) -> None:
        """按预设重建格子；每格按顺序接 ``prefs.monitor_cells`` 里的 did。

        ``handoff=True`` 时**不**调 ``stop_all()``：先把在播的 view 按 did 记下来，
        新格子建好后把播放器整份搬过去（``playbacks().replace_view``）——和卡片墙
        → 详情页 → 独立窗口那条接续是同一个机制。``handoff=False`` 用于首次构造。
        """
        self._zoomed = None
        carried: dict[str, CameraView] = {}
        if handoff:
            # 只取消"取址中"的任务，**不** release 会话：那些播放器马上要换控件
            self._pending.clear()
            self._poll.stop()
            for cell in self._cells:
                view = cell.view
                if view is None:
                    continue
                session = playbacks().session_of(view)
                if session is not None:
                    carried[session.did] = view
        else:
            self.stop_all()
        old_cells = self._cells
        self._cells = []
        for cell in old_cells:
            # 先隐藏再摘父级：setParent(None) 会让它短暂变成顶层窗口，隐藏后不会闪。
            # **先不删**——里面的播放器还要搬给新格子，删早了就把会话一起带走了。
            cell.hide()
            cell.setParent(None)
        # 布局里的项已随 cell 一起摘出来，这里只清布局自己的引用
        while self._grid.count():
            self._grid.takeAt(0)
        # **先把行列拉伸全部归零**：takeAt 只移走 item，setRowStretch / setColumnStretch
        # 是网格自己的属性、不会跟着消失。1x2 设过「第 1 列也拉伸 1」，切回 1x1 时
        # 这半截拉伸还在，单窗口就只占左半边、右边一条空白（2026-11-19 用户报）。
        # rowCount / columnCount 在 takeAt 后不会缩小，正好覆盖到上一次的范围。
        for i in range(self._grid.rowCount()):
            self._grid.setRowStretch(i, 0)
        for i in range(self._grid.columnCount()):
            self._grid.setColumnStretch(i, 0)
        spec = LAYOUT_SPECS.get(layout, LAYOUT_SPECS[MONITOR_LAYOUT_DEFAULT])
        self._spec = spec
        self._grid_rows = spec.rows
        self._grid_cols = spec.cols
        for i, (row, col, rspan, cspan) in enumerate(spec.cells):
            cell = CameraCell(self._p, self._grid_host)
            cell.clear_requested.connect(self._on_clear_cell)
            cell.zoom_requested.connect(self._on_zoom_cell)
            cell.play_requested.connect(self._on_cell_play)
            cell.frame_ready.connect(lambda img, c=cell: self._on_snapshot(c, img))
            self._grid.addWidget(cell, row, col, rspan, cspan)
            self._cells.append(cell)
        # 拉伸比按预设给（缺省全 1 = 等分）。归一化到 spec 的行列数，写多了忽略、
        # 写少了补 1，别让比例表本身成为越界来源。
        rows = spec.row_stretch or (1,) * spec.rows
        cols = spec.col_stretch or (1,) * spec.cols
        for i in range(spec.rows):
            self._grid.setRowStretch(i, rows[i] if i < len(rows) else 1)
        for i in range(spec.cols):
            self._grid.setColumnStretch(i, cols[i] if i < len(cols) else 1)
        self._apply_cells()
        if carried:
            self._carry_playbacks(carried)
        for cell in old_cells:
            cell.deleteLater()
        self._update_hint()

    def _carry_playbacks(self, carried: dict[str, CameraView]) -> None:
        """把旧格子在播的会话搬给新格子；搬不掉的（格数变少 / 设备被换）真停掉。"""
        for cell in self._cells:
            old = carried.pop(cell.did, None) if cell.did else None
            if old is None:
                continue
            if not playbacks().replace_view(old, cell.ensure_view()):
                # 极少见（旧会话刚好断了）：退回"重新开一路"，别留个黑格子
                self._start(cell)
        for view in carried.values():
            playbacks().release(view, "换布局时这一格没了")

    def _apply_cells(self) -> None:
        """把 prefs 里的 did 灌进格子；不属于当前家庭的当空格显示（不删记录）。"""
        dids = list(self.store.prefs.monitor_cells)
        for i, cell in enumerate(self._cells):
            did = dids[i] if i < len(dids) else ""
            state = self.store.devices.get(did) if did else None
            if state is not None and not self._in_home(state):
                state = None
            cell.set_device(state)
            # 封面在 ``set_device`` 之后给：格子换了设备就是换一张脸，别留在上一张上
            cell.set_poster(self._thumbs.get(state.did) if state is not None else None)
        if self._zoomed is not None and not self._zoomed.did:
            # 放大那一格被清空 / 换了家庭：留一块铺满画面区的空框没意义，还原成网格
            self._unzoom()

    def _save_cells(self) -> None:
        dids = [cell.did for cell in self._cells]
        self.store.prefs.set_monitor_cells(dids)

    def _in_home(self, state: DeviceState) -> bool:
        home = self.store.active_home
        if not home:
            return True
        return self.store._home_id_of(state) == home  # noqa: SLF001

    # -- 选源栏 ----------------------------------------------------------
    def refresh(self) -> None:
        """重建左侧列表，并把格子里的设备对一遍（改名 / 换房间 / 设备没了）。"""
        self._fill_rail()
        self._apply_cells()
        self._update_hint()
        # 格子里可能刚换了设备（含从别的家庭切回来）：把地址补预热上
        self.prefetch_urls()

    def _home_cameras(self) -> list[DeviceState]:
        home = self.store.active_home
        out: list[DeviceState] = []
        for room_id, _name in self.store.room_entries(home):
            for state in self.store.device_states(home, room_id):
                category = state.card.category if state.card is not None else ""
                if is_camera(state.node.model, category):
                    out.append(state)
        return out

    def _fill_rail(self) -> None:
        while self._rail_col.count() > 1:
            item = self._rail_col.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._sources.clear()
        cameras = self._home_cameras()
        if not cameras:
            empty = QLabel("这个家庭没有摄像头", self._rail_body)
            empty.setObjectName("PageHint")
            empty.setWordWrap(True)
            self._rail_col.insertWidget(0, empty)
            return
        for state in cameras:
            item = SourceItem(state, self._p, self._thumbs, self._rail_body)
            item.set_thumb(self._thumbs.get(state.did))
            item.drag_started.connect(self._on_drag_started)
            item.drag_moved.connect(self._on_drag_moved)
            item.drag_finished.connect(self._on_drag_finished)
            item.clicked.connect(self._on_source_clicked)
            self._rail_col.insertWidget(self._rail_col.count() - 1, item)
            self._sources[state.did] = item
        # 清掉已经不存在于任何家庭的封面（换账号 / 删设备后留下的）。留空判空：
        # 启动早期 ``store.devices`` 可能还是空的，那一下会把整目录删光。
        if self.store.devices:
            self._thumbs.prune(set(self.store.devices))

    def update_states(self, dids: set[str] | None = None) -> None:
        """设备状态变了：只改选源栏的文字 / 缩略图，不重建（否则拖动会被打断）。"""
        for did, item in self._sources.items():
            if dids is not None and did not in dids:
                continue
            state = self.store.devices.get(did)
            if state is not None:
                item.refresh(state)
                item.set_thumb(self._thumbs.get(did))

    def _update_hint(self) -> None:
        total = len(self._sources)
        playing = playbacks().count(SURFACE_MONITOR)
        offline = sum(1 for s in self._sources.values() if not self._online(s.did))
        parts = [f"{total} 台摄像头"]
        if playing:
            parts.append(f"{playing} 路在播")
        if offline:
            parts.append(f"{offline} 台离线")
        self.lbl_hint.setText(" · ".join(parts))

    def _online(self, did: str) -> bool:
        state = self.store.devices.get(did)
        return bool(state is not None and state.online)

    # -- 拖动 ------------------------------------------------------------
    def _on_drag_started(self, item: SourceItem) -> None:
        self._drag_item = item
        self._guard.start()
        # 拖影只取缩略图那一块（168×94）：整项连标题一起拖会挡住落点，
        # 也让人分不清"拖的是这一路还是一张卡"
        shot = rounded_shot(item.thumb)
        self._ghost = DragGhost(self, shot)
        self._ghost.show()

    def _on_drag_moved(self, pos: QPoint) -> None:
        ghost = self._ghost
        if ghost is not None:
            # ghost 是页面自己的子控件，坐标要用页面内坐标（不是屏幕坐标）
            local = self.mapFromGlobal(pos)
            ghost.move(local - QPoint(ghost.width() // 2, ghost.height() // 2))
        cell = self._cell_at(pos)
        if cell is not self._drag_cell:
            if self._drag_cell is not None:
                self._drag_cell.set_highlight(False)
            self._drag_cell = cell
            if cell is not None:
                cell.set_highlight(True)

    def _on_drag_finished(self, item: SourceItem, pos: QPoint) -> None:
        cell = self._cell_at(pos)
        self._end_drag()
        if cell is not None:
            self._assign(cell, item.did)

    def _end_drag(self) -> None:
        self._guard.stop()
        ghost = self._ghost
        self._ghost = None
        if ghost is not None:
            ghost.hide()
            ghost.deleteLater()
        if self._drag_cell is not None:
            self._drag_cell.set_highlight(False)
        self._drag_cell = None
        if self._drag_item is not None:
            self._drag_item.cancel_drag()
        self._drag_item = None

    def _on_drag_aborted(self, _reason: str) -> None:
        self._end_drag()

    def _cell_at(self, global_pos: QPoint) -> CameraCell | None:
        for cell in self._cells:
            if not cell.isVisible():
                continue
            rect = QRect(cell.mapToGlobal(QPoint(0, 0)), cell.size())
            if rect.contains(global_pos):
                return cell
        return None

    # -- 分配 ------------------------------------------------------------
    def _on_source_clicked(self, did: str) -> None:
        """单击 = 放进第一个空格；已经在某格里就什么都不做（别把它搬来搬去）。"""
        if self._zoomed is not None:
            # 放大时只有一格看得见：点源就是换这一路（拖放同理——``_cell_at``
            # 跳过隐藏的格子，落点必然也是这一格）。还按"第一个空格"来，就会
            # 塞进某个藏在后面的格子，画面上什么都没发生。
            if self._zoomed.did != did:
                self._assign(self._zoomed, did)
            return
        if any(cell.did == did for cell in self._cells):
            return
        for cell in self._cells:
            if not cell.did:
                self._assign(cell, did)
                return
        # 全满：替换**第一格**（最显眼的那格），比什么都不做更容易理解
        if self._cells:
            self._assign(self._cells[0], did)

    def _assign(self, cell: CameraCell, did: str) -> None:
        if not did:
            return
        state = self.store.devices.get(did)
        if state is None:
            cell.show_message("设备已不存在")
            return
        # 同一台设备已经在别的格：一台只占一格
        other = next((c for c in self._cells if c is not cell and c.did == did), None)
        carried = None
        if other is not None and other.view is not None:
            if playbacks().session_of(other.view) is not None:
                carried = other.view
        if cell.did != did:
            # 目标格自己的那一台（如果有）真停：它被换掉了，不该继续占观看位
            self._stop_cell(cell)
            cell.set_device(state)
            # 换进来先给封面，再决定要不要取流：离线 / 正在取址的这几百毫秒里
            # 格子里也不是空的
            cell.set_poster(self._thumbs.get(did))
        if carried is not None:
            # 原来那格正在播：播放器整份搬过来，不重新取址、不等 GOP
            if playbacks().replace_view(carried, cell.ensure_view()):
                if other is not None:
                    other.set_device(None)
                self._save_cells()
                self._update_hint()
                return
        if other is not None:
            other.stop()
            other.set_device(None)
        self._save_cells()
        self._start(cell)

    def _on_clear_cell(self, cell: CameraCell) -> None:
        self._stop_cell(cell)
        cell.set_device(None)
        if self._zoomed is cell:
            # 把放大那一格清空 = 剩一块巨大的空框，先还原成网格再让用户继续
            self._unzoom()
        self._save_cells()
        self._update_hint()

    def _on_cell_play(self, cell: CameraCell) -> None:
        self._start(cell)

    def _on_zoom_cell(self, cell: CameraCell) -> None:
        """双击放大：这一格**拉满整块画面区**，再双击还原。

        2026-11-25 用户报「双击后其它格是藏起来了，但画面还是原来那么大」——
        旧实现只 ``setVisible(False)``，而藏起来的格子仍占着网格里自己的位置，
        放大那一格的尺寸一点没变，等于双击白按。这里改成真的重排：把所有格从
        布局里摘出来、只把这一格按「跨全部行列」加回去。

        只动画面区，顶部工具栏与左侧选源栏保留——那两个是「换布局 / 全部停止 /
        拖源」的唯一入口，藏起来用户就没法退出了（还靠再双击）。
        """
        if self._zoomed is cell:
            self._unzoom()
            return
        if self._zoomed is not None:
            self._unzoom()
        self._zoomed = cell
        for item in self._cells:
            # 从布局摘出来：留着一个 hidden 的兄弟挂在同格位上，QGridLayout 仍按
            # 旧坐标给整块分配空间，画面就还是原来那么大
            self._grid.removeWidget(item)
            item.setVisible(item is cell)
        self._grid.addWidget(cell, 0, 0, self._grid_rows, self._grid_cols)

    def _unzoom(self) -> None:
        """把放大那一格放回预设坐标，其余格恢复显示。"""
        self._zoomed = None
        for item in self._cells:
            self._grid.removeWidget(item)
        spec = self._spec
        for cell, (row, col, rspan, cspan) in zip(self._cells, spec.cells):
            self._grid.addWidget(cell, row, col, rspan, cspan)
            cell.setVisible(True)

    # -- 播放 ------------------------------------------------------------
    def _stop_cell(self, cell: CameraCell) -> None:
        did = cell.did
        if did:
            self._pending.pop(did, None)
            self._prefetching.pop(did, None)
            # 停之前把手上这一帧存成封面：``stop()`` 会清掉 ``_image``，之后就只剩
            # 上一张（也许是十分钟前的）了。用户要的是"最后一帧"
            if cell.view is not None:
                self._save_frame(did, cell.view.last_image)
        cell.stop()

    def _start(self, cell: CameraCell) -> None:
        did = cell.did
        if not did:
            return
        state = self.store.devices.get(did)
        if state is None:
            cell.show_message("设备已不存在")
            return
        if not state.online:
            cell.show_message("设备离线，无法取流")
            return
        if self._pending.get(did) is not None:
            return  # 已经在取址
        view = cell.ensure_view()
        if playbacks().session_of(view) is not None:
            return  # 这一格已经在播，重复点播放键不做任何事（暂停/继续由控件自己管）

        # 预热过就直接开播：省掉一次线程 + 取址锁 + 80ms 轮询的往返。
        # 地址只在 go2rtc 没重起过时可信——端口是动态挑的（``_urls`` 里带着 epoch）
        cached = self._urls.pop(did, None)
        if cached is not None and cached[2] == camera_service().epoch:
            self._play_url(cell, did, cached[0], cached[1])
            return
        # 缓存没命中：作废这一台的预热（还在跑的会自己取消），别再并行取一次
        self._prefetching.pop(did, None)

        self._token += 1
        token = self._token
        self._pending[did] = token
        remote = self._wants_remote(state)
        cell.show_progress("正在取流…", f"{state.name}")
        # 取流在子线程做，结果进 _results 队列，靠这个 80ms 的定时器在主线程取出来
        # 再 claim + 播放。**这里必须 start**：漏了它线程照样跑完、地址也拿到了，但
        # 没人取 → 画面永远停在「正在取流…」（2026-11-19 真机报的就是这个）。
        self._poll.start()
        threading.Thread(
            target=self._work,
            args=(_Job(did=did, token=token, remote=remote,
                       allow_remote=remote or self._allow_fallback(), cell=cell),),
            daemon=True,
        ).start()

    def _play_url(self, cell: CameraCell, did: str, url: str, remote: bool) -> None:
        """地址到手 → 登记会话 → 开播。取址两条路径（现取 / 预热）共用这一步。"""
        view = cell.ensure_view()
        if playbacks().session_of(view) is not None:
            return
        if not playbacks().claim(view, did, url, remote, SURFACE_MONITOR):
            cell.show_message("这台设备已在其他画面里播放")
            camera_service().release(did)
            return
        view.play(url)
        self._update_hint()

    def _allow_fallback(self) -> bool:
        return str(self.store.camera_network or "") != "remote_only"

    def _wants_remote(self, state: DeviceState) -> bool:
        pref = str(self.store.camera_network or "")
        if pref == "remote_only":
            return True
        localip = str(getattr(state.node, "localip", "") or "")
        if pref == "remote_first":
            return True
        return not localip

    def _alive(self, did: str, token: int) -> bool:
        return self._pending.get(did) == token

    def _work(self, job: _Job) -> None:
        state = self.store.devices.get(job.did)
        if state is None:
            self._results.put((job.did, job.token, "", "设备已不存在", job.remote))
            return
        node = state.node
        remote = job.remote
        try:
            url = camera_service().open(
                self.store.client, node,
                remote=remote,
                guard=lambda: self._alive(job.did, job.token),
            )
        except CameraError as exc:
            if exc.kind == "cancelled":
                self._results.put((job.did, job.token, "", "", remote))
                return
            if remote or not job.allow_remote:
                self._results.put((job.did, job.token, "", str(exc), remote))
                return
            # 局域网明确失败：只在取址阶段回落到远程一次（与详情页同一策略），
            # 不做首帧后的看门狗——6 路各自看门狗互相打断的复杂度不值
            try:
                url = camera_service().open(
                    self.store.client, node,
                    remote=True,
                    guard=lambda: self._alive(job.did, job.token),
                )
                remote = True
            except CameraError as exc2:
                text = "" if exc2.kind == "cancelled" else str(exc2)
                self._results.put((job.did, job.token, "", text, True))
                return
        except Exception as exc:  # noqa: BLE001 - 外部进程 + 网络，兜底别让页面崩
            self._results.put((job.did, job.token, "", f"取流失败：{exc}", remote))
            return
        self._results.put((job.did, job.token, url, "", remote))

    def _collect(self) -> None:
        while True:
            try:
                did, token, url, error, remote = self._results.get_nowait()
            except queue.Empty:
                break
            if not self._alive(did, token):
                continue  # 用户已经切格 / 停止 / 离开页面
            self._pending.pop(did, None)
            cell = next((c for c in self._cells if c.did == did), None)
            if cell is None:
                camera_service().release(did)
                continue
            if not url:
                if error:
                    cell.show_message(error)
                continue
            self._play_url(cell, did, url, remote)
        self._collect_prefetch()
        if not self._pending and not self._prefetching:
            self._poll.stop()

    # -- 封面 ------------------------------------------------------------
    def _on_snapshot(self, cell: CameraCell, image) -> None:
        """播放中的一格每 2 秒会给一张帧，这里只挑每 ``THUMB_REFRESH_SECONDS`` 的那张。"""
        did = cell.did
        if not did:
            return
        now = time.monotonic()
        if now - self._snap_at.get(did, 0.0) < THUMB_REFRESH_SECONDS:
            return
        self._snap_at[did] = now
        self._save_frame(did, image)

    def _save_frame(self, did: str, image) -> None:
        """一帧原图 → 模糊封面：写盘 + 更新左侧列表 + 更新格子（播放中看不见）。"""
        if not did or image is None:
            return
        try:
            if image.isNull():
                return
        except (AttributeError, RuntimeError):
            return
        pix = self._thumbs.put(did, image)
        if pix is None:
            return
        item = self._sources.get(did)
        if item is not None:
            item.set_thumb(pix)
        for cell in self._cells:
            if cell.did == did:
                cell.set_poster(pix)

    # -- 预热 ------------------------------------------------------------
    def prefetch_urls(self) -> None:
        """把格子里这些设备的 RTSP 地址先要到手（**不取流**）。

        进来时跑一次，点播放时 ``_start`` 直接命中缓存，少一次线程 + 取址锁 +
        一轮 80ms 轮询。只预热局域网：远程要真建一条 P2P 会话，用户还没点播放就
        先占设备一个观看位不合适——而远程那步本来也没法靠预热省掉。
        """
        if not self.isVisible():
            return
        epoch = camera_service().epoch
        # 已经在播的不要再预热：``open(remote=False)`` 会顺手关掉这台设备的远程
        # 会话（``CameraService._open`` 的局域网分支），把别人正在看的画面掐掉。
        # 详情页/卡片墙与监控页共用一台设备的情况真的会发生。
        playing = {
            session.did
            for session in (playbacks().session_of(v) for v in playbacks().views())
            if session is not None
        }
        for cell in self._cells:
            did = cell.did
            if not did or did in playing or did in self._pending or did in self._prefetching:
                continue
            cached = self._urls.get(did)
            if cached is not None and cached[2] == epoch:
                continue
            state = self.store.devices.get(did)
            if state is None or not state.online or self._wants_remote(state):
                continue
            self._prefetch_token += 1
            token = self._prefetch_token
            self._prefetching[did] = token
            self._poll.start()
            threading.Thread(
                target=self._prefetch_work, args=(did, token), daemon=True
            ).start()

    def _prefetch_work(self, did: str, token: int) -> None:
        state = self.store.devices.get(did)
        if state is None:
            self._prefetch_results.put((did, token, "", False, 0))
            return
        try:
            url = camera_service().open(
                self.store.client, state.node,
                remote=False,
                guard=lambda: self._prefetching.get(did) == token,
            )
        except Exception:  # noqa: BLE001 - 预热失败不打扰用户，点播放时再取一次
            self._prefetch_results.put((did, token, "", False, 0))
            return
        # epoch 在 open 之后取：open 自己也可能重启 go2rtc，那样地址才刚算对
        self._prefetch_results.put((did, token, url, False, camera_service().epoch))

    def _collect_prefetch(self) -> None:
        while True:
            try:
                did, token, url, remote, epoch = self._prefetch_results.get_nowait()
            except queue.Empty:
                break
            if self._prefetching.get(did) != token:
                continue  # 已经停播 / 换格 / 离开页面
            self._prefetching.pop(did, None)
            if url:
                self._urls[did] = (url, remote, epoch)

    # -- 生命周期 --------------------------------------------------------
    def stop_all(self) -> None:
        """停掉本页所有画面并归还观看位（离开页面 / 换布局 / 退出时调）。"""
        self._pending.clear()
        self._prefetching.clear()
        self._poll.stop()
        # **先存封面再停**：停播会清掉控件里的最后一帧，之后就只剩上一次（也许
        # 十分钟前）存的那张。这也是用户说的"用最后一帧当封面"。
        for cell in self._cells:
            if cell.did and cell.view is not None:
                self._save_frame(cell.did, cell.view.last_image)
        playbacks().release_surface(SURFACE_MONITOR, "离开监控页")
        for cell in self._cells:
            cell.release_player_only()
        self._update_hint()

    def prepare_streams(self) -> int:
        """进页面时把当前家庭的摄像头源一次性登记进 go2rtc。"""
        nodes = [s.node for s in self._home_cameras()]
        if not nodes:
            return 0
        try:
            return camera_service().prepare(self.store.client, nodes)
        except Exception:  # noqa: BLE001 - 预置失败只影响首格多等一次重启
            return 0

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        # 换主题必须重下发页面样式表：_qss 里写死了颜色，不重发的话标题、
        # 提示、顶栏按钮会停在上一个主题（耗材页一直是这么做的）。
        self.setStyleSheet(_qss(palette))
        self.cb_layout.set_palette(palette)
        self.btn_stop.setFixedHeight(self.cb_layout.sizeHint().height())
        for cell in self._cells:
            cell.set_palette(palette)
        self.update()

    def hideEvent(self, event) -> None:  # noqa: N802
        self.stop_all()
        super().hideEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # 不自动播（用户 2026-11-19 定）：只把源与地址备好，画面等用户点播放键。
        self.prepare_streams()
        self.prefetch_urls()


__all__ = [
    "CELL_GAP",
    "LAYOUT_SPECS",
    "CameraCell",
    "MonitorPage",
    "SourceItem",
]
