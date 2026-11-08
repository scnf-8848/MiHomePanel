"""摄像头的独立播放窗口。

这里**不新建播放器、不重新取流**：把详情页那块 ``CameraView`` 整个搬到新窗口里，
打开/关闭只做 ``setParent`` + 尺寸策略切换。``QMediaPlayer`` / ``QVideoSink`` 和
``core.camera`` 里的会话都保持原对象，帧继续往同一个 sink 里来，所以画面是连贯的。
"""

from __future__ import annotations

import weakref
from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..core.camera import shared as camera_service
from . import theme
from .camera_view import CameraView, playbacks
from .chrome import TitleBar, WindowFrame

MIN_WIDTH = 640
MIN_HEIGHT = 420

#: 当前存在的独立窗口。主窗口最小化/退出时要连它一起收掉。
_OPEN_POPOUTS: list[weakref.ReferenceType[CameraPopoutWindow]] = []


def close_all_popouts() -> None:
    """关掉所有独立窗口（含已经脱离详情页自己持有的）。"""
    alive = [w for w in (ref() for ref in _OPEN_POPOUTS) if w is not None]
    _OPEN_POPOUTS[:] = [weakref.ref(w) for w in alive]
    for window in alive:
        window.close()


class CameraPopoutWindow(QWidget):
    """一个较大的、可拖动/缩放（含右下角）的独立窗口，里面放详情页同一个画面控件。"""

    closed = Signal()

    def __init__(
        self,
        view: CameraView,
        parent: QWidget,
        palette: theme.Palette,
        title: str,
        *,
        did: str = "",
        online_provider: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(parent, Qt.Window)
        self._view = view
        self._p = palette
        self._did = did
        #: 详情页关掉后没人轮询设备状态，独立窗口自己隔一会儿问一次
        self._online = online_provider
        self._net = QTimer(self)
        self._net.setInterval(2000)
        self._net.timeout.connect(self._check_online)
        self._staged = False
        #: 详情页已经关了，本窗口自己持有会话（不再收回到任何布局里）
        self._standalone = False
        self._closing = False
        _OPEN_POPOUTS.append(weakref.ref(self))
        self._host: QWidget | None = None
        self._host_layout = None
        self._index = -1
        self._placeholder: QLabel | None = None

        self.setWindowTitle(f"{title} · 实时画面")
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)
        # 大窗口但不至于铺满：默认约 62% 屏宽，并受屏高约束
        screen = parent.screen() if parent is not None else None
        screen = screen or QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            width = min(1120, max(760, int(avail.width() * 0.62)))
            height = min(int(avail.height() * 0.82), max(520, int(width * 9 / 16) + 48))
        else:
            width, height = 960, 600
        self.resize(width, height)
        if parent is not None:
            fg = parent.frameGeometry()
            self.move(fg.center().x() - width // 2, fg.center().y() - height // 2)

        self._frame = WindowFrame(
            self,
            resizable=True,
            # 留边是画面底色：窗口自身也用它，圆角处不会露出浅色底
            fill=lambda: self._view.video_bg,
            border=lambda: self._p.border,
        )
        self._box = QVBoxLayout(self)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._box.setSpacing(0)
        self._title = TitleBar(
            f"{title} · 实时画面",
            palette,
            buttons=("close",),
            variant="dialog",
        )
        self._box.addWidget(self._title)

    # -- 外部接口 --------------------------------------------------------
    @property
    def is_staged(self) -> bool:
        return self._staged

    @property
    def is_standalone(self) -> bool:
        """详情页已经关了，本窗口自己持有会话。"""
        return self._standalone

    def present(self) -> None:
        """把画面搬进独立窗口；已经在里面就只抬到前面。"""
        if self._staged:
            self.raise_()
            self.activateWindow()
            return
        self._detach()
        self._box.addWidget(self._view, 1)
        self._view.set_stage_mode(True)
        # 独立窗口要看到整幅画面：短边铺满、其余留黑边（窗口缩放时实时重算）
        self._view.set_contain_fit(True)
        self._view.show()
        self._staged = True
        playbacks().set_surface(self._view, "popout")
        self.show()
        self.raise_()
        self.activateWindow()

    def detach_from_parent(self) -> bool:
        """详情页要关了，但画面还在播：窗口挂到主窗口上继续，自己持有会话。

        只改父窗口，不碰播放器；之后 ``closeEvent`` 由本窗口负责停播和归还
        观看位，而不是把画面塞回一个正在销毁的详情页。
        """
        if not self._staged or self._standalone:
            return False
        host = self.parentWidget()
        root = host.window() if host is not None else None
        outer = root.parentWidget() if root is not None else None
        self.setParent(outer)
        self._host = None
        self._host_layout = None
        self._index = -1
        self._standalone = True
        # 详情页已经不再负责这次播放：独立窗口的按钮改为关窗（关窗即停播）。
        # 详情页那边的连接会随它销毁自动断开，这里不主动 disconnect，避免
        # 在"没有接收者"时触发 PySide 的 RuntimeWarning。
        self._view.popout_requested.connect(self.close)
        # 独立持有会话时没有详情页帮忙收错误：播放器一进错误/停止态就关窗，
        # 由 closeEvent 统一归还观看位，避免窗口留着一块黑屏还占着 relay。
        self._view.state_changed.connect(self._on_view_state)
        if self._online is not None:
            self._net.start()
        playbacks().set_surface(self._view, "popout")
        self.show()
        self.raise_()
        self.activateWindow()
        return True

    def dock(self) -> None:
        """把画面放回详情页原位。不碰播放器，继续播。"""
        if not self._staged:
            return
        self._box.removeWidget(self._view)
        self._view.set_stage_mode(False)
        # 回到详情页内嵌画面：恢复原来的 cover 呈现
        self._view.set_contain_fit(False)
        if self._host is not None:
            self._view.setParent(self._host)
        if self._host_layout is not None and self._index >= 0:
            self._host_layout.insertWidget(self._index, self._view)
        if self._placeholder is not None:
            if self._host_layout is not None:
                self._host_layout.removeWidget(self._placeholder)
            self._placeholder.deleteLater()
            self._placeholder = None
        self._view.show()
        self._staged = False

    def _on_view_state(self, state: str) -> None:
        if (
            self._standalone
            and not self._closing
            and state in (CameraView.ERROR, CameraView.RELEASED, CameraView.STOPPED)
        ):
            self.close()

    def _check_online(self) -> None:
        if self._online is not None and not self._online():
            # 设备掉线后不再抱着一条已经死掉的会话占观看位
            self.close()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._closing = True
        self._net.stop()
        if self._standalone:
            # 详情页已经关了：这次关窗就是"用户不想看了"，停播并归还观看位
            self._view.set_contain_fit(False)
            self._release_session()
            self._staged = False
        else:
            self.dock()
            playbacks().set_surface(self._view, "detail")
        super().closeEvent(event)
        self.closed.emit()

    def _release_session(self) -> None:
        view = self._view
        if playbacks().release(view, "关闭独立窗口"):
            return
        # 没有所有者登记（旧路径 / claim 失败）：仍然把这条会话收干净
        view.stop()
        view.release_player()
        if self._did:
            camera_service().release(self._did)

    # -- 内部 ------------------------------------------------------------
    def _detach(self) -> None:
        host = self._view.parentWidget()
        if host is None or host.layout() is None:
            return
        self._host = host
        self._host_layout = host.layout()
        index = self._host_layout.indexOf(self._view)
        if index >= 0:
            self._index = index
            self._host_layout.removeWidget(self._view)
        # 占位保持详情页原高度，收回时不会整页上下跳
        ph = QLabel("实时画面已在独立窗口中播放\n关闭独立窗口即可回到这里", host)
        ph.setAlignment(Qt.AlignCenter)
        ph.setFixedHeight(220)
        ph.setStyleSheet(
            f"color:{self._p.text_mute};font-size:12px;"
            f"background:{self._p.bg_soft};border-radius:{theme.RADIUS_SMALL}px;"
        )
        self._host_layout.insertWidget(max(0, self._index), ph)
        self._placeholder = ph
