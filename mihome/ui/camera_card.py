"""摄像头卡片：外壳还是 ``DeviceCard``，内容带换成一块 ``CameraView``。

只在用户把卡片样式选成「大卡 2×2：实时画面」时出现。默认仍是 1×1 标准卡
（产品图），所以摄像头不会因为"品类是 camera"就突然变大一格。

三条和普通卡片不同的行为：

* **默认只有一个播放键**，不建会话、不占观看位；点播放才由
  ``WallCameraController`` 去准备地址；
* 视频区单击不打开详情（避免看画面时误触），双击才进详情；
* 设备离线、滚出屏幕、切房间、最小化时由外层调 ``stop_playback``，
  直接把 ``CameraView`` 的播放器释放掉，而不是暂停占着观看位。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, Qt, Signal

from ..core.store import DeviceState
from . import theme
from .camera_view import CameraView
from .card_templates import BODY_VIDEO, CardStyle
from .device_card import DeviceCard
from .widgets import IconProvider


class CameraCard(DeviceCard):
    #: 用户点了画面里的播放键（地址还没准备）
    play_requested = Signal()
    #: 画面状态变化；外层据此判断 9 秒看门狗 / 是否要切通道
    view_state_changed = Signal(str)

    def __init__(
        self,
        state: DeviceState,
        icons: IconProvider,
        palette: theme.Palette,
        style: CardStyle | None = None,
    ) -> None:
        # 父类构造里会调 ``_build_body``，这里必须先有字段
        self._view: CameraView | None = None
        super().__init__(state, icons, palette, style)

    # -- 内容带 ----------------------------------------------------------
    def _build_body(self) -> None:
        if self._style.body != BODY_VIDEO:
            self._drop_view()
            super()._build_body()
            return
        # 视频样式不建 CardBody；之前若是滑条样式，先把子控件清掉
        if self._body is not None:
            old = self._body
            old.setParent(None)
            old.deleteLater()
            self._body = None
        if self._view is None:
            self._view = CameraView(
                self._p,
                theme.category_color(self.state.card.category),
                self,
                compact=True,
            )
            self._view.play_requested.connect(self.play_requested)
            self._view.state_changed.connect(self.view_state_changed)
            self._view.installEventFilter(self)
        self._view.setGeometry(self._body_rect())
        self._view.show()

    def _drop_view(self) -> None:
        if self._view is None:
            return
        self._view.stop()
        self._view.removeEventFilter(self)
        self._view.setParent(None)
        self._view.deleteLater()
        self._view = None

    @property
    def view(self) -> CameraView | None:
        return self._view

    @property
    def view_state(self) -> str:
        return self._view.state if self._view is not None else CameraView.STOPPED

    def play_video(self, url: str) -> None:
        if self._view is not None:
            self._view.play(url)

    def show_progress(self, hint: str, sub: str = "") -> None:
        if self._view is not None:
            self._view.show_progress(hint, sub)

    def show_error(self, message: str) -> None:
        if self._view is not None:
            self._view.show_error(message)

    def stop_playback(self) -> None:
        """停播并销毁播放器；外层负责再放掉 relay 会话。"""
        if self._view is None:
            return
        self._view.stop()
        self._view.release_player()

    # -- 外壳联动 --------------------------------------------------------
    def set_style(self, style: CardStyle) -> None:
        if style == self._style:
            return
        self.stop_playback()
        super().set_style(style)
        if self._view is not None:
            self._view.setGeometry(self._body_rect())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._view is not None:
            self._view.setGeometry(self._body_rect())

    def set_palette(self, palette: theme.Palette) -> None:
        super().set_palette(palette)
        if self._view is not None:
            self._view.set_palette(
                palette, theme.category_color(self.state.card.category)
            )

    def set_drag_placeholder(self, on: bool) -> None:
        super().set_drag_placeholder(on)
        if self._view is not None:
            self._view.setVisible(not on)

    def refresh(self, state: DeviceState | None = None) -> None:
        super().refresh(state)
        view = self._view
        if view is None:
            return
        if not self.state.online:
            if view.state not in (CameraView.STOPPED, CameraView.IDLE):
                self.stop_playback()
            view.show_offline("设备离线，无法取流")
        elif view.state in (CameraView.STOPPED, CameraView.MOVED):
            # MOVED 的会话如果已经结束（详情页/独立窗口都关了），这里回到空闲；
            # 还在别处播时 show_idle 自己会拒绝。
            view.show_idle()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        # 视频区双击 = 进详情。单击由下面的 press/release 接管，不会变成卡片点击。
        if obj is self._view and event.type() == QEvent.MouseButtonDblClick:
            self.clicked.emit(self.state.did)
            return True
        return super().eventFilter(obj, event)

    def _in_video(self, pos: QPoint) -> bool:
        view = self._view
        return view is not None and view.isVisible() and view.geometry().contains(pos)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # 画面的 press 由卡片接住：一是让墙面的拖动过滤器在卡片层看得到，
        # 从画面起拖也有效；二是别让 ``super()`` 把它当成卡片点击的起点。
        if event.button() == Qt.LeftButton and self._in_video(event.position().toPoint()):
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        # 单击画面（空白处 / 正在播的视频）不打开详情。播放键、暂停键是
        # ``CameraView`` 的子控件，自己会把事件吃掉，到不了这里。
        if event.button() == Qt.LeftButton and self._in_video(event.position().toPoint()):
            event.accept()
            return
        super().mouseReleaseEvent(event)


__all__ = ["CameraCard"]
