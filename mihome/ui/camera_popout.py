"""摄像头的独立播放窗口。

这里**不新建播放器、不重新取流**：把详情页那块 ``CameraView`` 整个搬到新窗口里，
打开/关闭只做 ``setParent`` + 尺寸策略切换。``QMediaPlayer`` / ``QVideoSink`` 和
``core.camera`` 里的会话都保持原对象，帧继续往同一个 sink 里来，所以画面是连贯的。
"""

from __future__ import annotations

import threading
import weakref
from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..core.camera import shared as camera_service
from . import theme
from .camera_view import CameraView, playbacks
from .chrome import TitleBar, WindowFrame
from .ptz_pad import PtzPad

MIN_WIDTH = 640
MIN_HEIGHT = 420
#: 方向盘离画面左边/下边各留多少（与画面里那排圆钮的 pad=12 对齐）
PTZ_MARGIN = 12

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
    #: 后台探测「这个 go2rtc 构建带不带云台接口」的结果，回到 UI 线程用
    _ptz_probed = Signal(bool)

    def __init__(
        self,
        view: CameraView,
        parent: QWidget,
        palette: theme.Palette,
        title: str,
        *,
        did: str = "",
        accent: str = "",
        online_provider: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(parent, Qt.Window)
        self._view = view
        self._p = palette
        self._did = did
        self._accent = accent
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
        #: 叠在画面左下角的云台方向盘；这个构建不支持（官方 go2rtc）时始终保持 None
        self._ptz: PtzPad | None = None
        #: 云台能力探测结果：None = 还没问出来
        self._ptz_ok: bool | None = None
        self._state_hooked = False
        self._ptz_probed.connect(self._on_ptz_probed)

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
        # 同一个窗口可以被"关掉再打开"复用（详情页只在脱离后重建）：
        # 不复位的话，第二次进来 `_closing` 还是 True，出错时就不会自动关窗了
        self._closing = False
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
        self._hook_state()
        self._ensure_ptz()
        self._sync_ptz()

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
        self._hook_state()
        if self._online is not None:
            self._net.start()
        playbacks().set_surface(self._view, "popout")
        self.show()
        self.raise_()
        self.activateWindow()
        self._sync_ptz()
        return True

    def dock(self) -> None:
        """把画面放回详情页原位。不碰播放器，继续播。"""
        if not self._staged:
            return
        if self._ptz is not None:
            # 方向盘别在窗口已经空了以后还攥着定时器
            self._ptz.stop()
            self._ptz.hide()
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
        self._sync_ptz()
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

    # -- 云台 ------------------------------------------------------------
    def _hook_state(self) -> None:
        """接管画面状态（`present` 与 `detach_from_parent` 都走这里，只连一次）。"""
        if self._state_hooked:
            return
        self._state_hooked = True
        self._view.state_changed.connect(self._on_view_state)

    def _ensure_ptz(self) -> None:
        """建方向盘（先藏着，探测回来再决定要不要显出来）。

        探测要在后台做：``ptz_supported()`` 是一条 HTTP，未命中缓存时最长 0.8 秒；
        开窗不能为了一个可能根本不存在的控件卡这么久。
        """
        if self._ptz is not None or not self._did:
            return
        self._ptz = PtzPad(
            self._did,
            self._p,
            self._accent,
            self,
            size=PtzPad.OVERLAY_SIZE,
            overlay=True,
        )
        self._ptz.hide()
        QTimer.singleShot(0, self._probe_ptz)

    def _probe_ptz(self) -> None:
        def work() -> None:
            try:
                ok = camera_service().ptz_supported()
            except Exception:  # noqa: BLE001 - 探测失败按"不支持"处理
                ok = False
            try:
                self._ptz_probed.emit(ok)
            except RuntimeError:
                # 窗口已经销毁：没必要把结果送回来了
                pass

        threading.Thread(target=work, name="mihome-ptz-probe", daemon=True).start()

    def _on_ptz_probed(self, ok: bool) -> None:
        self._ptz_ok = bool(ok)
        self._sync_ptz()

    def _sync_ptz(self) -> None:
        """半透明方向盘什么时候露出来。

        与详情页那条规则同一套：**构建支不支持**（官方 go2rtc 没有这条路由）
        和**现在能不能转**（没在播时 go2rtc 那边没有 producer）。区别只有一条：
        详情页把它排进布局，这里叠在画面左下角，没画面时干脆不露。
        """
        pad = self._ptz
        if pad is None:
            return
        state = self._view.state
        live = state in (CameraView.LOADING, CameraView.PLAYING, CameraView.PAUSED)
        ready = self._ptz_ok is True and live
        if not ready:
            pad.stop()
        pad.set_ready(ready)
        pad.setVisible(self._staged and self._ptz_ok is True and live)
        self._place_ptz()

    def _place_ptz(self) -> None:
        """贴在画面左下角。用 ``_view`` 的几何而不是窗口的：上面还有一条标题栏。"""
        pad = self._ptz
        if pad is None or not pad.isVisible():
            return
        geo = self._view.geometry()
        pad.move(
            geo.left() + PTZ_MARGIN,
            geo.top() + geo.height() - pad.height() - PTZ_MARGIN,
        )
        # 画面控件后 show 过就会压在它上面，每次定位顺手抬一下
        pad.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._place_ptz()
        # 这一轮布局可能还没把画面撑开，下一轮再量一次（成本就是一次 move）
        QTimer.singleShot(0, self._place_ptz)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._closing = True
        self._net.stop()
        if self._ptz is not None:
            # 关窗就等于不想看了：别让 12 秒兜底或者按住的补发继续往摄像头发指令
            self._ptz.stop()
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
