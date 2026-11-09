"""详情页里的实时画面控件。

只做三件事：把 ``core/camera.py`` 给的本机 RTSP 交给 ``QMediaPlayer``、把帧画成
圆角卡片、把"为什么没有画面"说清楚。P2P、协议、子进程都在 ``core/camera.py``。

为什么用 ``QMediaPlayer`` 而不是自己解析 MJPEG：go2rtc 的 ``/api/frame.jpeg``
每次都要调 ffmpeg 转码（实测 700ms/帧），而 RTSP 是直通，由 Qt 自带的 FFmpeg
后端解码——本机实测 2960×1666 的 HEVC 能到 20fps，且不依赖系统里装没装 ffmpeg。

**进页面不取流**：默认只有一个播放键，用户点下去才建立 RTSP 会话（``play()``），
同时用 ``play_requested`` 通知外层去准备地址。不自动取流，是因为一台摄像头只允许
3 路并发、推流还要占上行带宽——没人看的时候不该占着。

**首帧要等一个完整 GOP**：摄像头 GOP 实测 3–4 秒，而 go2rtc 的 ``probe()`` 会吃掉
会话起点那个 IDR，消费者只能等下一个 IDR 才解得出画面。这一段在客户端消不掉，别在
这里加"多试几次"之类的无效重连。加载态只说"加载中…"加一条流动的进度条：**不给
秒数、也不做百分比**——链路上没有一处能给出可靠的剩余量（等 IDR 的相位完全随机），
编一个数字反而会让用户以为卡在 60% 了。

播放中右下角常驻一个半透明暂停键，随时能停。暂停不是断开：画面停在当前帧，
再点一下立刻续上，不会重新打洞。但**暂停时设备仍在推流**（实测 ~170 KB/s），
而一台摄像头只允许 3 路并发，所以暂停久了要主动放掉连接——否则关着不看的详情页
也占着一个观看位。闲置阈值见 ``IDLE_RELEASE_SECONDS``（测试可用环境变量
``MIHOME_CAMERA_IDLE_SECONDS`` 调小）。

**声音**（2026-11-10 接入）：go2rtc 取的流里本来就有音轨（局域网实测
``audio, recvonly, OPUS/48000/2``），Qt 侧只要给 ``QMediaPlayer`` 配一个
``QAudioOutput`` 就能出声。所以这里只做两件事：建播放器时配上输出设备、右下角
给一个喇叭键。**静音不重建流**——``QAudioOutput.setMuted()`` 只是把声音关掉，
音轨照收，点一下立刻有声，不用再等一个 GOP。喇叭的显隐看 ``hasAudio()``，
而它要到 ``LoadedMedia`` 之后才为真（加载期恒为 False），所以显隐必须挂在
``mediaStatusChanged`` 上，不能建播放器时判一次。

**静音不跨会话保留**（2026-11-10 用户实测后定）：每次调 :meth:`CameraView.play`
（也就是真的新开一路）都回到静音；而**暂停/继续**和**换承载面**（卡片 ↔ 详情页 ↔
独立窗口，见 :meth:`CameraView.hand_off_to`）都保持当前状态。理由是不写进偏好文件，
程序重启、换设备、隔天再开都不会突然把家里的声音放出来——"我刚才开了声音"只对
那一次观看成立。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PySide6.QtWidgets import QPushButton, QSizePolicy, QWidget

from ..core import timing
from . import theme
from .widgets import draw_text

# **默认关掉硬件解码**，这一步必须在 QMediaPlayer 之前执行（模块导入期）。
#
# Qt 的 ffmpeg 后端在 Windows 上默认走 D3D11 硬解，交付给我们的帧是显存里的
# 纹理转出来的 NV12。显存被别的程序（游戏）吃满时实测两种后果，都不是我们能
# 接住的：轻则 `AVHWFramesContext: Could not create the texture (8007000e)`
# （E_OUTOFMEMORY）→ 一帧都收不到；重则 `frame.toImage()` 里直接
# access violation，**整个面板进程被杀**，Python 层连异常都看不到。
#
# 关掉之后帧变成内存里的 YUV420P，代价实测很小：同一路 2960×1666 HEVC 播 14 秒，
# 进程 CPU 4.0s → 5.7s（约单核 12%），帧率不变。换"显存再紧张也不会崩"很值。
# 想换回硬解：设 MIHOME_CAMERA_HW_DECODE=1（或在环境里自己指定
# QT_FFMPEG_DECODING_HW_DEVICE_TYPES）。
_HW_DECODE_ENV = "MIHOME_CAMERA_HW_DECODE"
if os.environ.get(_HW_DECODE_ENV, "").strip().lower() not in ("1", "true", "yes", "on"):
    os.environ.setdefault("QT_FFMPEG_DECODING_HW_DEVICE_TYPES", "none")

# 上面关掉硬解之后，Qt 每建一个播放器都会打一条
# `qt.multimedia.playbackengine.codec: "No HW decoder found"`。这是**预期行为**，
# 不是故障，但和 ffmpeg 那些真正的解码报错混在一起就很难看。只关这一个类别
# （别的一概不动），让终端里留下的都是有信息量的行。
_LOG_CATEGORY = "qt.multimedia.playbackengine.codec"
_rules = os.environ.get("QT_LOGGING_RULES", "").strip()
if _LOG_CATEGORY not in _rules:
    _off = f"{_LOG_CATEGORY}=false"
    os.environ["QT_LOGGING_RULES"] = f"{_rules};{_off}" if _rules else _off

# 闲置多久放掉连接。一台摄像头最多 3 路并发，挂着不看的详情页不该一直占位
IDLE_RELEASE_SECONDS = 180

# -- 声音 ---------------------------------------------------------------
# 静音状态**按控件、按会话**存在：每次 ``play()`` 都回到静音，只在这一次观看里由
# 用户决定要不要出声。所以它没有"全局默认值"也不落盘——上一个会话开过声音，
# 下次打开不该继承（家里/店里的声音不该因为昨天点过一次就自己响起来）。
_MUTED_DEFAULT = True

#: 右下角操作键的边长。三个键（暂停 / 喇叭 / 独立窗口）必须同尺寸——大小不一
#: 会让人以为它们权重不同，也让这一排像随手摆的。中央播放键是主操作，故意大一号。
_SIDE_BTN = 34
_SIDE_BTN_COMPACT = 30

_VIDEO_BG = "#0E1013"
_FG = "#F5F7FA"
_FG_SOFT = "#B9C0CC"

# 加载态那条流动进度条：多久走完一趟、多久重画一次
_PROGRESS_CYCLE_MS = 1300
_PROGRESS_FRAME_MS = 40

# 【取流计时】Qt 媒体状态 → 日志里的阶段名，只用于 core/timing.py
_MEDIA_STATUS_NAMES = {
    QMediaPlayer.NoMedia: "NoMedia",
    QMediaPlayer.LoadingMedia: "LoadingMedia",
    QMediaPlayer.LoadedMedia: "LoadedMedia（RTSP 建流成功）",
    QMediaPlayer.StalledMedia: "StalledMedia",
    QMediaPlayer.BufferingMedia: "BufferingMedia",
    QMediaPlayer.BufferedMedia: "BufferedMedia（已有缓冲）",
    QMediaPlayer.EndOfMedia: "EndOfMedia",
    QMediaPlayer.InvalidMedia: "InvalidMedia",
}
_PLAYBACK_NAMES = {
    QMediaPlayer.StoppedState: "Stopped",
    QMediaPlayer.PlayingState: "Playing",
    QMediaPlayer.PausedState: "Paused",
}

# 【取流计时】错误类型：区分"资源打不开"和"网络瞬断"，排查时很有用
_ERROR_NAMES = {
    QMediaPlayer.NoError: "NoError",
    QMediaPlayer.ResourceError: "ResourceError",
    QMediaPlayer.FormatError: "FormatError",
    QMediaPlayer.NetworkError: "NetworkError",
    QMediaPlayer.AccessDeniedError: "AccessDeniedError",
}


@dataclass
class PlaybackHandoff:
    """一次"换渲染面"要带走的东西：播放器、音频输出、sink 和全部播放状态。

    它**不包含 URL 之外的会话信息**：RTSP 连接、relay、go2rtc 都由
    ``QMediaPlayer`` / ``core.camera`` 持有，这里只把同一个 player 交给下一个
    控件。拿到它的控件用 ``adopt_playback`` 接管，源控件回到 ``MOVED``。
    """

    player: QMediaPlayer
    sink: QVideoSink
    url: str
    state: str
    frames: int
    image: QImage | None
    painted: bool
    hint: str
    sub: str
    retries: int
    lan_first_fallback: bool
    buf_bucket: int
    pos_logged: bool
    meta_logged: bool
    #: 音频输出也是独立 QObject，**必须跟着 player 一起搬**：不搬的话声音还留在
    #: 旧控件上，新控件按静音键改的却是自己那个没人用的输出设备。
    #: 老会话没有音频（``MIHOME_CAMERA_AUDIO=0``）时是 ``None``。
    audio_out: QAudioOutput | None = None
    #: 静音状态跟着会话走，换承载面时不该突然出声或突然静音
    muted: bool = True


class _RoundButton(QWidget):
    """画面上的圆形按钮：播放画三角，暂停画两道竖线，喇叭键画音箱。"""

    clicked = Signal()

    def __init__(self, parent: QWidget, size: int, alpha: int = 120) -> None:
        super().__init__(parent)
        self._paused_icon = False
        self._popout_icon = False
        self._mute_icon = False
        self._muted_icon = False
        self._alpha = alpha
        self._hover = False
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_TranslucentBackground)

    def set_playing_icon(self, playing: bool) -> None:
        """``playing=True`` 画两道竖线（正在播，按钮的动作是暂停）。"""
        if self._paused_icon != playing:
            self._paused_icon = playing
            self.update()

    def set_popout_icon(self) -> None:
        """画一个"从方框里指出去"的箭头：在独立窗口中播放。"""
        self._popout_icon = True
        self.update()

    def set_mute_icon(self, muted: bool) -> None:
        """画喇叭；``muted=True`` 时加一道斜线。

        静音态**不换图形**（还是那个喇叭），只加斜线：换成一个叉会让人以为
        "声音坏了"，加斜线是"关掉了、还能开"。
        """
        self._mute_icon = True
        if self._muted_icon != muted:
            self._muted_icon = bool(muted)
            self.update()

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        # 圆钮是自绘 QWidget，默认的 press 会被忽略并冒泡到卡片，
        # 松手时卡片就会把这次点击判成"打开详情"。按下/松开都在这里吃掉。
        if event.button() == Qt.LeftButton:
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            if self.rect().contains(event.position().toPoint()):
                self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        alpha = min(230, self._alpha + (40 if self._hover else 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, alpha))
        painter.drawEllipse(r)
        painter.setBrush(QColor("#FFFFFF"))

        size = self.width()
        if self._popout_icon:
            # 小方框 + 右上角指出去的箭头，比四角箭头更像"另开一扇窗"
            pen = QColor("#FFFFFF")
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            box = QRectF(size * 0.26, size * 0.40, size * 0.34, size * 0.28)
            painter.drawRoundedRect(box, 2.5, 2.5)
            painter.drawLine(QPointF(size * 0.50, size * 0.50),
                             QPointF(size * 0.74, size * 0.26))
            painter.drawLine(QPointF(size * 0.57, size * 0.26),
                             QPointF(size * 0.74, size * 0.26))
            painter.drawLine(QPointF(size * 0.74, size * 0.26),
                             QPointF(size * 0.74, size * 0.43))
        elif self._mute_icon:
            self._paint_speaker(painter, size)
        elif self._paused_icon:
            bar_w = max(3.0, size * 0.09)
            bar_h = size * 0.34
            gap = size * 0.09
            top = (size - bar_h) / 2
            for dx in (-gap - bar_w, gap):
                painter.drawRoundedRect(
                    QRectF(size / 2 + dx, top, bar_w, bar_h), bar_w / 2, bar_w / 2
                )
        else:
            # 视觉重心偏右一点，三角形才不像歪的
            triangle = QPolygonF(
                [
                    QPoint(int(size * 0.40), int(size * 0.31)),
                    QPoint(int(size * 0.40), int(size * 0.69)),
                    QPoint(int(size * 0.70), int(size * 0.50)),
                ]
            )
            path = QPainterPath()
            path.addPolygon(triangle)
            painter.fillPath(path, QColor("#FFFFFF"))
        painter.end()

    def _paint_speaker(self, painter: QPainter, size: float) -> None:
        """喇叭：方箱 + 张开的口；静音时把两道声波换成一道斜线。

        尺寸都按按钮边长的比例算，卡片墙的 30px 与详情页的 34px 用同一套坐标，
        不然小按钮上的喇叭会糊成一团白点。箱体别画太窄：窄了像一个"1"旁边挂了
        两道弧，看不出是喇叭；斜线也别顶到圆的边缘，那样像"划掉了整个按钮"，
        而这里要表达的是"这个喇叭关掉了"。
        """
        white = QColor("#FFFFFF")
        painter.setPen(Qt.NoPen)
        painter.setBrush(white)
        painter.drawRect(QRectF(size * 0.27, size * 0.41, size * 0.12, size * 0.18))
        cone = QPolygonF(
            [
                QPointF(size * 0.39, size * 0.41),
                QPointF(size * 0.53, size * 0.29),
                QPointF(size * 0.53, size * 0.71),
                QPointF(size * 0.39, size * 0.59),
            ]
        )
        path = QPainterPath()
        path.addPolygon(cone)
        painter.fillPath(path, white)
        painter.setBrush(Qt.NoBrush)
        if self._muted_icon:
            painter.setPen(QPen(white, max(1.4, size * 0.065), Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(QPointF(size * 0.30, size * 0.68),
                             QPointF(size * 0.70, size * 0.30))
            return
        # 两道声波：圆心落在喇叭口**外侧**，只画朝右的那半边。圆心太靠左会跟
        # 锥形口叠在一起，看着像"旗子加括号"。
        painter.setPen(QPen(white, max(1.2, size * 0.055), Qt.SolidLine, Qt.RoundCap))
        cx, cy = size * 0.60, size * 0.50
        for radius in (0.115, 0.195):
            r = size * radius
            painter.drawArc(QRectF(cx - r, cy - r, r * 2, r * 2), -50 * 16, 100 * 16)


class CameraView(QWidget):
    """自绘视频画面。``play_requested`` 由外层接到"准备地址并开播"的逻辑上。"""

    IDLE = "idle"
    LOADING = "loading"
    PLAYING = "playing"
    PAUSED = "paused"
    RELEASED = "released"
    ERROR = "error"
    STOPPED = "stopped"
    #: 播放器被搬到另一个承载面（详情页 / 独立窗口 / 卡片）：本控件只剩一个提示
    MOVED = "moved"

    play_requested = Signal()
    state_changed = Signal(str)
    #: 用户点了"在独立窗口中播放"（详情页接线；墙面 compact 卡没有这个按钮）
    popout_requested = Signal()
    # 【取流计时】首帧真正画到控件上（发一次），外层用它结束取流计时
    first_frame_shown = Signal()

    def __init__(
        self,
        palette: theme.Palette,
        accent: str,
        parent: QWidget | None = None,
        *,
        compact: bool = False,
    ) -> None:
        super().__init__(parent)
        self._p = palette
        self._accent = accent
        #: 卡片墙上的一小块画面：不强制 220 高，交给卡片布局定尺寸
        self._compact = bool(compact)
        self._image: QImage | None = None
        #: 独立窗口用 contain（整幅可见、留黑边）；卡片/详情页仍用 cover
        self._contain = False
        self._frames = 0
        self._painted = False
        self._hint = ""
        self._sub = ""
        self._state = CameraView.IDLE
        self._stopping = False
        #: 控件当前被搬到独立窗口里播放：只影响尺寸策略，播放器/会话不动
        self._staged = False
        self._url = ""
        self._retries = 1
        #: 这次是"局域网优先的局域网会话"：明确失败时不再重连同一地址，交给外层切远程
        self._lan_first_fallback = False
        #: 加载态进度条的相位（0..1），由 ``_clock`` 推进
        self._progress = 0.0
        self._hover = False
        self._hint_provider: Callable[[], str] | None = None
        if not self._compact:
            self.setFixedHeight(220)
            self.setMinimumWidth(240)
        self.setMouseTracking(True)

        # 加载态靠一条流动的进度条表示"还在连"：光有静态文字，几秒不动就像卡死了
        self._clock = QTimer(self)
        self._clock.setInterval(_PROGRESS_FRAME_MS)
        self._clock.timeout.connect(self._tick)

        # 闲置自动放连接：只对"暂停"生效——暂停时设备仍在推流、还占着观看位
        self._idle_ms = int(
            max(1.0, float(os.environ.get("MIHOME_CAMERA_IDLE_SECONDS", IDLE_RELEASE_SECONDS)))
            * 1000
        )
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.timeout.connect(self._release_idle)

        # **播放器懒创建**：卡片墙上的画面控件一屏可能有好几个，但大多数时候
        # 没人点播放。构造时就建 QMediaPlayer + QVideoSink 会白占解码上下文，
        # 拖动排序重建卡片时还会频繁分配/释放。第一次 ``play`` 才建，
        # ``release_player`` 彻底放掉。
        self._player: QMediaPlayer | None = None
        self._sink: QVideoSink | None = None
        self._audio_out: QAudioOutput | None = None
        #: 声音状态只属于**这次观看**：``play()`` 每次都会把它重置成静音，
        #: 暂停/继续、换承载面（handoff）才保持。``_audio_out`` 是它的执行端。
        self._muted = _MUTED_DEFAULT
        self._buf_bucket = -1
        self._pos_logged = False
        self._meta_logged = False

        # 中央播放键：没在播的时候都在（空态 / 暂停 / 释放）
        self._play_btn = _RoundButton(self, 48 if self._compact else 58, 150)
        self._play_btn.set_playing_icon(False)
        self._play_btn.clicked.connect(self.toggle)
        self._play_btn.show()

        # 右下角那一排（暂停 / 喇叭 / 独立窗口）**同尺寸**：大小不一会让人以为
        # 它们的权重不同，也让这一排看起来像随手摆的。中央播放键是主操作，故意大一号。
        side = _SIDE_BTN_COMPACT if self._compact else _SIDE_BTN

        # 右下角暂停键
        self._pause_btn = _RoundButton(self, side, 110)
        self._pause_btn.set_playing_icon(True)
        self._pause_btn.clicked.connect(self.toggle)
        self._pause_btn.hide()

        # 详情页右下角的"独立窗口"键。墙面卡不加（compact）。
        self._pop_btn: _RoundButton | None = None
        if not self._compact:
            self._pop_btn = _RoundButton(self, side, 110)
            self._pop_btn.set_popout_icon()
            self._pop_btn.setToolTip("在独立窗口中播放")
            self._pop_btn.clicked.connect(self._request_popout)
            self._pop_btn.hide()

        # 右下角喇叭：只在流里真有音轨时出现。卡片墙也放一个——卡片上的画面同样会
        # 出声，没有关掉的地方就只能去关系统音量了。
        self._mute_btn = _RoundButton(self, side, 110)
        self._mute_btn.set_mute_icon(self._muted)
        self._mute_btn.setToolTip("取消静音" if self._muted else "静音")
        self._mute_btn.clicked.connect(self.toggle_mute)
        self._mute_btn.hide()

        # 错误态的重试按钮，文案型
        self._btn = QPushButton("重试", self)
        self._btn.setCursor(Qt.PointingHandCursor)
        self._btn.setStyleSheet(
            "QPushButton{background:rgba(255,255,255,0.14);color:#FFFFFF;border:none;"
            f"border-radius:{theme.RADIUS_SMALL}px;padding:7px 18px;font-size:13px;}}"
            "QPushButton:hover{background:rgba(255,255,255,0.24);}"
        )
        self._btn.clicked.connect(self.toggle)
        self._btn.hide()

        self._refresh_buttons()

    # -- 播放器生命周期 ---------------------------------------------------
    def _ensure_player(self) -> QMediaPlayer:
        """第一次播放时才建播放器；释放后再次播放会重新建一套。"""
        if self._player is not None:
            return self._player
        self._sink = QVideoSink(self)
        self._player = QMediaPlayer(self)
        self._player.setVideoSink(self._sink)
        # 音频输出：Qt6 不给 QMediaPlayer 设 audioOutput 就是**无声**（不是"有声音但
        # 被系统静音"），所以必须显式配一个。音量固定 100%——这一版只做"有没有声"，
        # 不做音量滑杆，设置音量反而多一个要解释的旋钮。
        self._audio_out = QAudioOutput(self)
        self._audio_out.setVolume(1.0)
        self._apply_mute()
        self._player.setAudioOutput(self._audio_out)
        self._sink.videoFrameChanged.connect(self._on_frame)
        self._player.errorOccurred.connect(self._on_error)
        # 【取流计时】只记录 Qt 的握手/缓冲阶段，不改变播放行为
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.playbackStateChanged.connect(self._on_playback_state)
        self._player.bufferProgressChanged.connect(self._on_buffer_progress)
        self._player.positionChanged.connect(self._on_position)
        self._player.metaDataChanged.connect(self._on_metadata)
        return self._player

    def release_player(self) -> None:
        """停播并**彻底销毁**播放器，回到默认的播放键空态。

        卡片墙的离开可视区域 / 切房间 / 最小化都走这里：看不见的卡片不该
        继续占解码器和显存。详情页不用这个接口，它保留播放器以便暂停续播。
        """
        self._clock.stop()
        self._idle.stop()
        self._stopping = True
        player = self._player
        self._player = None
        if player is not None:
            try:
                player.stop()
                player.setSource(QUrl())
                # 先断开对音频输出的引用，再两个一起丢；否则 deleteLater 的先后
                # 顺序没人保证，播放器可能短暂指向一个已经析构的输出设备
                player.setAudioOutput(None)
            except RuntimeError:
                pass
            player.deleteLater()
        sink = self._sink
        self._sink = None
        if sink is not None:
            sink.deleteLater()
        audio = self._audio_out
        self._audio_out = None
        if audio is not None:
            try:
                audio.setMuted(True)  # 别在销毁的空隙里漏出半秒声音
            except RuntimeError:
                pass
            audio.deleteLater()
        self._stopping = False
        self._image = None
        self._frames = 0
        self._painted = False
        self._url = ""
        self._hint = ""
        self._sub = ""
        self._buf_bucket = -1
        self._pos_logged = False
        self._meta_logged = False
        self.setToolTip("")
        self._state = CameraView.IDLE
        self._refresh_buttons()
        self.update()

    def set_palette(self, palette: theme.Palette, accent: str | None = None) -> None:
        self._p = palette
        if accent:
            self._accent = accent
        self.update()

    # -- 状态 ------------------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def frames(self) -> int:
        """已渲染的帧数（验证脚本用，也是判断"有没有真的连上"的唯一硬指标）。"""
        return self._frames

    def _set_state(self, state: str) -> None:
        if self._state == state:
            return
        self._state = state
        if state == CameraView.PAUSED:
            self._idle.start(self._idle_ms)
        else:
            self._idle.stop()
        self._refresh_buttons()
        self.state_changed.emit(state)
        self.update()

    def _refresh_buttons(self) -> None:
        if self._state == CameraView.MOVED:
            # 画面已经交给别的承载面：这里再放播放键会误导用户开第二路
            self._play_btn.hide()
            self._pause_btn.hide()
            self._mute_btn.hide()
            self._btn.hide()
            if self._pop_btn is not None:
                self._pop_btn.hide()
            self.setCursor(Qt.ArrowCursor)
            self._place_buttons()
            return
        waiting = self._state in (CameraView.IDLE, CameraView.PAUSED, CameraView.RELEASED)
        # 右下角那一排**共用一条规则**：有画面（播放 / 暂停）且鼠标在画面上才整排出现，
        # 移开就整排消失。不给某个键单独开例外——只要有一个键常驻，用户就会以为
        # "这里的按钮是常驻的"，然后在别处找不到暂停；而常驻的键还得一直挡着画面。
        # 整排里再按语义各让一步：暂停键只在播放中出现（已暂停时中央已经是播放键，
        # 再放一个"暂停"是无意义的）；喇叭只在流里真有音轨时出现。
        has_audio = self._has_audio()
        row = self._hover and self._state in (CameraView.PLAYING, CameraView.PAUSED)
        self._play_btn.setVisible(waiting)
        self._pause_btn.setVisible(row and self._state == CameraView.PLAYING)
        self._btn.setVisible(self._state == CameraView.ERROR)
        if self._pop_btn is not None:
            self._pop_btn.setVisible(row)
        self._mute_btn.setVisible(row and has_audio)
        self._mute_btn.set_mute_icon(self._muted)
        self._mute_btn.setToolTip("取消静音" if self._muted else "静音")
        self._place_buttons()
        self.setCursor(Qt.PointingHandCursor if waiting else Qt.ArrowCursor)

    # -- 独立窗口 --------------------------------------------------------
    def set_stage_mode(self, staged: bool) -> None:
        """在详情页和独立窗口之间切换尺寸策略。

        只动控件自己的 min/max/sizePolicy，**不碰 QMediaPlayer / QVideoSink /
        CameraService**，所以画面不会重连，帧继续进来。
        """
        if self._compact:
            return
        self._staged = bool(staged)
        if self._staged:
            self.setMinimumSize(0, 0)
            self.setMaximumSize(16777215, 16777215)
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        else:
            self.setMinimumWidth(240)
            self.setMaximumHeight(16777215)
            self.setFixedHeight(220)
            self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        if self._pop_btn is not None:
            self._pop_btn.setToolTip("收回详情页" if staged else "在独立窗口中播放")
        self._refresh_buttons()
        self._place_buttons()

    def _request_popout(self) -> None:
        self.popout_requested.emit()

    @property
    def video_bg(self) -> str:
        """画面底色（独立窗口拿它做留边/圆角底色，避免露出窗口的白底）。"""
        return _VIDEO_BG

    def set_contain_fit(self, contain: bool) -> None:
        """``True``：短边铺满、整幅可见（独立窗口）；``False``：长边铺满、裁边。"""
        contain = bool(contain)
        if self._contain == contain:
            return
        self._contain = contain
        self.update()

    # -- 换渲染面（卡片 / 详情页 / 独立窗口共用一个播放器）----------------
    @property
    def has_live_playback(self) -> bool:
        """当前控件是否握着一条活会话（可被另一个承载面接管）。"""
        return (
            self._player is not None
            and self._sink is not None
            and self._state in (CameraView.LOADING, CameraView.PLAYING, CameraView.PAUSED)
        )

    def take_playback(self) -> PlaybackHandoff | None:
        """把播放器 / sink / 播放状态从本控件摘下来，交给另一个承载面。

        只做 Qt 对象的解绑和重绑：**不 stop、不 setSource、不碰 camera_service**。
        ``QMediaPlayer`` 的解码线程和 RTSP 会话都原样保留，所以目标控件接管后
        下一帧就能继续画。摘不下来（没有活会话）时返回 ``None``，调用方应保持
        原状，不要重新 open。
        """
        if not self.has_live_playback:
            return None
        player = self._player
        sink = self._sink
        assert player is not None and sink is not None
        audio_out = self._audio_out
        handoff = PlaybackHandoff(
            player=player,
            sink=sink,
            url=self._url,
            state=self._state,
            frames=self._frames,
            image=self._image,
            painted=self._painted,
            hint=self._hint,
            sub=self._sub,
            retries=self._retries,
            lan_first_fallback=self._lan_first_fallback,
            buf_bucket=self._buf_bucket,
            pos_logged=self._pos_logged,
            meta_logged=self._meta_logged,
            audio_out=audio_out,
            muted=self._muted,
        )
        self._clock.stop()
        self._idle.stop()
        self._detach_handlers(player, sink)
        # 先脱离旧控件，目标接管时再挂上去；这一步不销毁任何东西
        player.setParent(None)
        sink.setParent(None)
        if audio_out is not None:
            audio_out.setParent(None)
        self._player = None
        self._sink = None
        self._audio_out = None
        self._image = None
        self._frames = 0
        self._painted = False
        self._url = ""
        self._hint = "画面已在其他窗口中播放"
        self._sub = "关掉那个窗口即可回到这里"
        self._state = CameraView.MOVED
        self._refresh_buttons()
        self.update()
        return handoff

    def adopt_playback(self, handoff: PlaybackHandoff) -> bool:
        """接管 ``take_playback`` 摘下来的会话。已有播放器时拒绝，避免两路并存。"""
        if self._player is not None or self._sink is not None:
            return False
        self._player = handoff.player
        self._sink = handoff.sink
        self._player.setParent(self)
        self._sink.setParent(self)
        # 音频输出跟着会话一起接管，并把静音状态按原样套回去
        self._audio_out = handoff.audio_out
        if self._audio_out is not None:
            self._audio_out.setParent(self)
        self._muted = handoff.muted
        self._apply_mute()
        if self._audio_out is not None:
            try:
                self._player.setAudioOutput(self._audio_out)
            except RuntimeError:
                pass
        self._attach_handlers(self._player, self._sink)
        self._url = handoff.url
        self._frames = handoff.frames
        self._image = handoff.image
        self._painted = handoff.painted
        self._hint = handoff.hint
        self._sub = handoff.sub
        self._retries = handoff.retries
        self._lan_first_fallback = handoff.lan_first_fallback
        self._buf_bucket = handoff.buf_bucket
        self._pos_logged = handoff.pos_logged
        self._meta_logged = handoff.meta_logged
        self._stopping = False
        self._state = handoff.state
        if handoff.state == CameraView.LOADING:
            self._clock.start()
        elif handoff.state == CameraView.PAUSED:
            self._idle.start(self._idle_ms)
        self._refresh_buttons()
        self.update()
        # 新承载面的外层控制器要靠这条信号接上看门狗 / 状态栏
        self.state_changed.emit(self._state)
        return True

    def hand_off_to(self, target: CameraView) -> bool:
        """把会话交给 ``target``；目标接管失败时留在本控件继续播。"""
        if target is self:
            return True
        handoff = self.take_playback()
        if handoff is None:
            return False
        try:
            adopted = target.adopt_playback(handoff)
        except RuntimeError:
            # 目标控件的 C++ 对象已经销毁
            adopted = False
        if adopted:
            return True
        # 目标已销毁 / 已有播放器：立刻收回，画面不中断
        self.adopt_playback(handoff)
        return False

    def _detach_handlers(self, player: QMediaPlayer, sink: QVideoSink) -> None:
        for sig, slot in self._handler_pairs(player, sink):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    def _attach_handlers(self, player: QMediaPlayer, sink: QVideoSink) -> None:
        for sig, slot in self._handler_pairs(player, sink):
            try:
                sig.connect(slot)
            except (RuntimeError, TypeError):
                pass

    def _handler_pairs(self, player: QMediaPlayer, sink: QVideoSink) -> tuple:
        return (
            (sink.videoFrameChanged, self._on_frame),
            (player.errorOccurred, self._on_error),
            (player.mediaStatusChanged, self._on_media_status),
            (player.playbackStateChanged, self._on_playback_state),
            (player.bufferProgressChanged, self._on_buffer_progress),
            (player.positionChanged, self._on_position),
            (player.metaDataChanged, self._on_metadata),
        )

    # -- 声音 ------------------------------------------------------------
    @property
    def muted(self) -> bool:
        return self._muted

    def toggle_mute(self) -> None:
        """切声音。**只动输出设备，不碰播放器**：不用重建流、不用再等一个 GOP。"""
        self.set_muted(not self._muted)

    def set_muted(self, muted: bool) -> None:
        """只改这一路的声音，不落盘、不影响别的控件或下一次播放。"""
        self._muted = bool(muted)
        self._apply_mute()
        self._refresh_buttons()

    def _apply_mute(self) -> None:
        audio = self._audio_out
        if audio is None:
            return
        try:
            audio.setMuted(self._muted)
        except RuntimeError:
            pass

    def _has_audio(self) -> bool:
        """流里有没有可听的音轨。

        实测它**要到 ``LoadedMedia`` 之后才为真**，加载期恒 ``False``——所以按钮
        显隐必须挂 ``mediaStatusChanged``，建播放器时判一次会把按钮永久藏掉。
        """
        player = self._player
        if player is None:
            return False
        try:
            # 用 getattr：验证脚本用鸭子类型的假播放器，不该为它实现全部接口
            query = getattr(player, "hasAudio", None)
            return bool(query()) if callable(query) else False
        except RuntimeError:
            return False

    # -- 播放控制 --------------------------------------------------------
    def play(self, url: str) -> None:
        """建立 RTSP 会话并连续播放。只应在用户点过播放（或重试）之后调用。"""
        # 新开一路就回到静音：上一次开过声音不继承。暂停/继续（``toggle``）和换承载面
        # （``hand_off_to`` / ``adopt_playback``）都不经过这里，所以它们保持原状态。
        self._muted = _MUTED_DEFAULT
        player = self._ensure_player()
        self._apply_mute()
        self._refresh_buttons()
        self._url = url
        self._retries = 1
        self._image = None
        self._frames = 0
        self._painted = False
        self._buf_bucket = -1
        self._pos_logged = False
        self._meta_logged = False
        timing.mark("RTSP 播放请求", url)
        self._progress = 0.0
        self._hint = "加载中…"
        self._sub = ""
        self.setToolTip("")
        self._clock.start()
        self.update()
        # 同一地址重播也要走 stop，否则 QMediaPlayer 认为源没变、不会重连
        self._stopping = True
        player.stop()
        self._stopping = False
        self._set_state(CameraView.LOADING)
        player.setSource(QUrl(url))
        player.play()

    def set_immediate_fallback(self, enabled: bool) -> None:
        """局域网优先的局域网会话：明确失败后不再重连同一地址。

        外层会在 ``play()`` 之前设置它；不带这个标记时保留原来的"首帧前自动重连一次"。
        """
        self._lan_first_fallback = bool(enabled)

    def toggle(self) -> None:
        """播放键 / 暂停键共用的开关。"""
        if self._state == CameraView.PLAYING and self._player is not None:
            self._player.pause()
            self._set_state(CameraView.PAUSED)
        elif self._state == CameraView.PAUSED and self._player is not None:
            self._player.play()
            self._set_state(CameraView.PLAYING)
        elif self._state in (CameraView.IDLE, CameraView.RELEASED, CameraView.ERROR):
            # 地址还没取到：先切到加载态给反馈，外层准备好再调 play()
            self._begin()

    def _begin(self) -> None:
        """用户点了播放。先亮加载态，再让外层准备地址。"""
        if self._state == CameraView.MOVED:
            # 会话在别的承载面手里：这里再开就是第二路
            return
        timing.mark("点下播放（控件）")
        self._retries = 1
        self._progress = 0.0
        self._hint = "加载中…"
        self._sub = ""
        self.setToolTip("")
        self._clock.start()
        self._set_state(CameraView.LOADING)
        self.update()
        self.play_requested.emit()

    def _tick(self) -> None:
        """推进加载态进度条的相位（不数秒、不报百分比）。"""
        if self._state != CameraView.LOADING:
            self._clock.stop()
            return
        self._progress = (self._progress + _PROGRESS_FRAME_MS / _PROGRESS_CYCLE_MS) % 1.0
        self.update()

    def _release_idle(self) -> None:
        """暂停够久就放掉连接。

        不这么做的话，"开着详情页但没在看"也会一直占一个观看位、一直走摄像头的
        上行带宽——一台设备只允许 3 路并发，这个位子该留给真在看的人。
        """
        if self._state != CameraView.PAUSED:
            return
        timing.mark("闲置释放连接")
        self._clock.stop()
        player = self._player
        self._stopping = True
        if player is not None:
            player.stop()
            player.setSource(QUrl())
        self._image = None
        minutes = max(1, round(self._idle_ms / 60_000))
        self._hint = f"闲置 {minutes} 分钟，连接已释放"
        self._sub = "点播放重新连接"
        self._set_state(CameraView.RELEASED)
        self.update()

    def set_hint_provider(self, provider: Callable[[], str] | None) -> None:
        """装一个"为什么断了"的查询函数（外层用 go2rtc 日志判断观看位是否被占满）。"""
        self._hint_provider = provider

    def stop(self) -> None:
        # `stop()` 是异步的：已经排队的最后一两帧还会到 sink。留着 `_stopping`
        # 让 `_on_frame` 直接丢，否则停了之后画面还会跳一下、帧计数也会涨。
        self._clock.stop()
        self._idle.stop()
        self._stopping = True
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QUrl())
        self._image = None
        self._url = ""
        self._set_state(CameraView.STOPPED)
        self.update()

    def show_idle(self) -> None:
        """回到"只有一个播放键"的空态（设备重新上线 / 停播后）。"""
        if self._state == CameraView.MOVED and playbacks().active:
            # 会话还在别的承载面上播：卡片刷新不能把"画面在别处"的提示擦掉
            return
        self._clock.stop()
        self._idle.stop()
        self._image = None
        self._frames = 0
        self._painted = False
        self._hint = ""
        self._sub = ""
        self.setToolTip("")
        self._set_state(CameraView.IDLE)
        self.update()

    def show_offline(self, message: str) -> None:
        """设备离线：不给重试按钮，避免用户对着一个连不上的设备反复点。"""
        self.stop()
        self._hint = message
        self._sub = ""
        self.setToolTip(message)
        self._state = CameraView.STOPPED
        self._refresh_buttons()
        self.update()

    def show_error(self, message: str) -> None:
        """取流前的错误（没装 go2rtc、设备离线、没局域网地址）。"""
        self.stop()
        self._hint = message
        self._sub = ""
        self.setToolTip(message)
        self._set_state(CameraView.ERROR)
        self.update()

    def show_progress(self, hint: str, sub: str = "") -> None:
        """换取流通道时的过渡态（局域网失败、正在试远程中继）。"""
        # 先把旧会话彻底停掉：`stop()` 会把 `_stopping` 留在 True，这里要恢复，
        # 否则新会话的第一帧会被 `_on_frame` 当成残留帧丢掉
        self.stop()
        self._stopping = False
        self._progress = 0.0
        self._hint = hint
        self._sub = sub
        self.setToolTip("")
        self._clock.start()
        self._set_state(CameraView.LOADING)
        self.update()

    # -- 事件 ------------------------------------------------------------
    def _on_frame(self, frame: object) -> None:
        # 暂停/停止之后 sink 里可能还排着一两帧，画上去会让静止画面跳一下。
        # 另外只有"正在连"和"正在播"才该收帧：`release_player()` 把 `_stopping`
        # 复位成 False 之后，拆会话期间漏出来的尾帧会被当成新画面画上去，
        # 于是空态/已释放态底下多出一张静止截图（播放键还盖在上面）。
        if self._stopping or self._state not in (CameraView.LOADING, CameraView.PLAYING):
            return
        try:
            if not frame.isValid():  # type: ignore[attr-defined]
                return
            img = frame.toImage()  # type: ignore[attr-defined]
        except (AttributeError, TypeError, RuntimeError):
            return
        if img.isNull():
            return
        first = self._frames == 0
        self._image = img
        self._frames += 1
        if first:
            timing.mark(
                "首个关键帧解码完成", f"{img.width()}x{img.height()}"
            )
        if self._state == CameraView.LOADING:
            # 能解出帧就说明已经越过 GOP、追上关键帧了
            self._clock.stop()
            self._hint = ""
            self._sub = ""
            self._set_state(CameraView.PLAYING)
        self.update()

    def _on_error(self, _err: object, message: str) -> None:
        if self._stopping:
            return
        timing.mark(
            "播放器报错",
            f"{_ERROR_NAMES.get(_err, str(_err))}: {message or ''}",
        )
        if self._lan_first_fallback and not self._frames:
            # 局域网优先的局域网会话，首帧前只要报错就是明确失败（InvalidMedia /
            # Could not open file / 连接被拒）：不重连同一个地址，直接进错误态，
            # 由外层立刻切远程中继（只切一次）
            self._retries = 0
            timing.mark("明确失败：不重连同一地址", "交给外层切远程中继")
        # 首帧之前出错多半是瞬时的（go2rtc 刚起、RTSP 协商撞了一下），自己重连一次，
        # 别让用户为一次握手失败去点重试
        if self._retries > 0 and self._url:
            timing.mark("首帧前自动重连")
            self._retries -= 1
            self._image = None
            self._hint = "正在重新连接…"
            self._sub = ""
            self._clock.start()
            self._set_state(CameraView.LOADING)
            self.update()
            QTimer.singleShot(400, self._reconnect)
            return
        self._clock.stop()
        self._image = None
        self._hint = self._reason() or (
            "画面中断，点重试重新连接" if self._frames else "无法打开画面"
        )
        self._sub = ""
        self.setToolTip(message or "")
        self._set_state(CameraView.ERROR)
        self.update()

    def _on_media_status(self, status: object) -> None:
        """【取流计时】Qt 的媒体状态：DESCRIBE / 首包 / 缓冲。"""
        try:
            buffer = f" buffer={self._player.bufferProgress():.0%}"
        except (AttributeError, RuntimeError):
            buffer = ""
        timing.mark(
            "媒体状态", f"{_MEDIA_STATUS_NAMES.get(status, str(status))}{buffer}"
        )
        if status == QMediaPlayer.InvalidMedia and self._lan_first_fallback:
            # 真正的通道切换交给 errorOccurred（它总会跟着来）；这里只标一下，
            # 免得两条信号各触发一次回落。万一只有状态没有报错，9 秒看门狗兜底。
            timing.mark("明确失败（InvalidMedia）", "等待报错后切远程")
        # ``hasAudio()`` 要到 ``LoadedMedia`` 之后才为真，喇叭的显隐得跟着状态走；
        # 建播放器时判一次会把有音轨的流上的按钮永久藏掉（实测确认）。
        if status in (
            QMediaPlayer.LoadedMedia,
            QMediaPlayer.InvalidMedia,
            QMediaPlayer.NoMedia,
        ):
            self._refresh_buttons()

    def _on_buffer_progress(self, value: float) -> None:
        """【取流计时】按 25% 档记录缓冲进度，避免刷屏。"""
        pct = max(0, min(100, round(float(value) * 100)))
        bucket = min(100, (pct // 25) * 25)
        if bucket <= self._buf_bucket:
            return
        self._buf_bucket = bucket
        timing.mark("缓冲进度", f"{pct}%")

    def _on_position(self, ms: int) -> None:
        """【取流计时】第一次出现非零播放位置 = 媒体时钟真的在走。"""
        if self._pos_logged or ms <= 0:
            return
        self._pos_logged = True
        timing.mark("播放位置开始前进", f"{ms} ms")

    def _on_metadata(self) -> None:
        """【取流计时】元数据就绪通常早于 LoadedMedia，用它定位 DESCRIBE 完成。"""
        if self._meta_logged:
            return
        self._meta_logged = True
        try:
            duration = self._player.duration()
        except RuntimeError:
            duration = -1
        timing.mark("媒体元数据就绪", f"duration={duration}ms")

    def _on_playback_state(self, state: object) -> None:
        """【取流计时】Qt 的播放状态：Stopped / Playing / Paused。"""
        timing.mark("播放器状态", _PLAYBACK_NAMES.get(state, str(state)))

    def _reason(self) -> str:
        """问外层"刚才为什么断了"。取不到就返回空串，用默认文案。"""
        if self._hint_provider is None:
            return ""
        try:
            return (self._hint_provider() or "").strip()
        except Exception:  # noqa: BLE001 - 提示拿不到不该影响报错本身
            return ""

    def _reconnect(self) -> None:
        if not self._url or self._state != CameraView.LOADING or self._player is None:
            return
        timing.mark("自动重连：重新 setSource")
        self._buf_bucket = -1
        self._pos_logged = False
        self._meta_logged = False
        self._stopping = True
        self._player.stop()
        self._stopping = False
        self._player.setSource(QUrl(self._url))
        self._player.play()

    # 视频区不接 press/release：让它冒泡到卡片，卡片既能把拖动排序接住，
    # 也能统一压掉"单击画面 = 打开详情"。双击由 ``CameraCard`` 的事件过滤器接。
    def enterEvent(self, event) -> None:
        self._hover = True
        self._refresh_buttons()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        # 鼠标移到子按钮上时父控件也会收到 Leave，用真实坐标判断才不闪
        self._hover = self.rect().contains(self.mapFromGlobal(QCursor.pos()))
        self._refresh_buttons()
        super().leaveEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_buttons()

    def _place_buttons(self) -> None:
        size = self._play_btn.width()
        self._play_btn.move(
            (self.width() - size) // 2, (self.height() - size) // 2 - 6
        )
        pad = 12
        gap = 8
        # 右下角从右往左排：独立窗口 → 喇叭 → 暂停。**按这套按钮里会出现哪些累加**，
        # 不写死两段式坐标：加键不用重算，某个键不参与（没有音轨 / 卡片墙没有独立窗口）
        # 时右边也不会留一个空槽。整排是同时出现、同时消失的，所以位置一旦定了就不跳。
        row = [self._pop_btn]
        if self._has_audio():
            row.append(self._mute_btn)
        row.append(self._pause_btn)
        right = self.width() - pad
        bottom = self.height() - pad
        for btn in row:
            if btn is None:
                continue
            right -= btn.width()
            btn.move(right, bottom - btn.height())
            right -= gap
        self._btn.adjustSize()
        bs = self._btn.size()
        self._btn.move((self.width() - bs.width()) // 2, self.height() // 2 + 40)

    # -- 绘制 ------------------------------------------------------------
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        rect = self.rect()
        path = QPainterPath()
        # 圆角比卡片小一档：它是卡片里的一个"屏幕"，不是卡片本身
        path.addRoundedRect(
            QRect(rect.x(), rect.y(), rect.width(), rect.height()),
            theme.RADIUS_SMALL,
            theme.RADIUS_SMALL,
        )
        painter.setClipPath(path)
        painter.fillRect(rect, QColor(_VIDEO_BG))

        img = self._image
        if img is not None and not img.isNull():
            iw, ih = img.width(), img.height()
            if iw > 0 and ih > 0:
                # cover：填满画布，多出来的裁掉；contain：短边铺满，剩余留黑边
                if self._contain:
                    scale = min(rect.width() / iw, rect.height() / ih)
                else:
                    scale = max(rect.width() / iw, rect.height() / ih)
                w, h = max(1, int(iw * scale)), max(1, int(ih * scale))
                painter.drawImage(
                    QRect(
                        rect.x() + (rect.width() - w) // 2,
                        rect.y() + (rect.height() - h) // 2,
                        w,
                        h,
                    ),
                    img,
                )
            if not self._painted:
                self._painted = True
                timing.mark("首帧已上屏", f"{iw}x{ih}")
                self.first_frame_shown.emit()
            if self._state == CameraView.PAUSED:
                painter.fillRect(rect, QColor(0, 0, 0, 105))
            elif self._state == CameraView.PLAYING and self._hover:
                painter.fillRect(rect, QColor(0, 0, 0, 28))
            painter.end()
            return

        # 空态 / 加载态 / 错误态
        self._draw_status(painter, rect, self._state in (CameraView.IDLE, CameraView.RELEASED))
        painter.end()

    def _draw_status(self, painter: QPainter, rect: QRect, lifted: bool) -> None:
        """画面区中央的几行字。``lifted`` = 中央有播放键，文字要让开别压上去。"""
        mid = rect.y() + rect.height() // 2
        if self._state == CameraView.IDLE:
            # 空态只有播放键 + 键下一句提示，不在画面上方写大字
            tip = "点击播放查看实时画面"
            font = QFont(self.font())
            font.setPixelSize(12)
            box_w = QFontMetrics(font).horizontalAdvance(tip) + 26
            box = QRect(rect.center().x() - box_w // 2, mid + 26, box_w, 22)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 128))
            painter.drawRoundedRect(box, 11, 11)
            draw_text(painter, box, tip, size=12, color=_FG, align=Qt.AlignCenter)
            return
        line = QRect(rect.x() + 20, mid - (92 if lifted else 26), rect.width() - 40, 24)
        draw_text(
            painter,
            line,
            self._hint,
            size=14,
            color=_FG,
            align=Qt.AlignHCenter | Qt.AlignVCenter,
        )
        if self._sub:
            draw_text(
                painter,
                QRect(
                    rect.x() + 20,
                    mid + 34 if lifted else line.bottom() + 8,
                    rect.width() - 40,
                    20,
                ),
                self._sub,
                size=12,
                color=_FG_SOFT,
                align=Qt.AlignHCenter | Qt.AlignVCenter,
            )
        if self._state == CameraView.LOADING:
            # 流动的细条：表示"还在连"，但**不给秒数也不假装百分比**——等 IDR 的
            # 相位随机，编出来的进度条只会在某一格上停住，看着更像卡死
            track_w = max(120, min(int(rect.width() * 0.34), 260))
            track = QRect(
                (rect.width() - track_w) // 2,
                line.bottom() + (44 if self._sub else 30),
                track_w,
                3,
            )
            painter.setPen(Qt.NoPen)
            painter.setOpacity(0.18)
            painter.setBrush(QColor(_FG_SOFT))
            painter.drawRoundedRect(track, 2, 2)
            painter.setOpacity(1.0)
            seg_w = max(28, int(track_w * 0.34))
            x = track.x() + int((track_w - seg_w) * self._progress)
            painter.setBrush(QColor(self._accent))
            painter.drawRoundedRect(QRect(x, track.y(), seg_w, 3), 2, 2)


class PlaybackOwnership:
    """进程内唯一的活动播放所有者。

    卡片 body / 详情页 / 独立窗口只是三个承载面，谁都不能自己再开一路。规则：

    * ``claim``：新会话建立后登记，已有别的会话时拒绝（单路仲裁）；
    * ``move``：同一会话换承载面，只搬 player/sink，不动 RTSP；
    * ``release``：真正结束会话，停播 + 放播放器 + 归还 relay。

    ``did`` / ``url`` / ``remote`` 跟着所有者走，便于独立窗口在详情页关掉后
    继续持有，也便于失败时知道该归还哪台设备。
    """

    def __init__(self) -> None:
        self._view: CameraView | None = None
        self._did = ""
        self._url = ""
        self._remote = False
        self._surface = ""
        #: 会话结束时通知谁（卡片墙用它把停在 MOVED 的卡放回空闲）。用回调而不是
        #: Qt 信号：这里不是 QObject，也不想为了一个通知把整条链路改成 QObject。
        self._listeners: list[Callable[[], None]] = []

    @property
    def active(self) -> bool:
        return self._view is not None

    @property
    def active_view(self) -> CameraView | None:
        return self._view

    @property
    def did(self) -> str:
        return self._did

    @property
    def url(self) -> str:
        return self._url

    @property
    def remote(self) -> bool:
        return self._remote

    @property
    def surface(self) -> str:
        return self._surface

    def on_finish(self, callback: Callable[[], None]) -> None:
        """登记"会话结束"回调（``release`` / ``forget`` 之后调用一次）。

        卡片墙靠它把停在 ``MOVED`` 的卡片放回空闲：会话被搬走后卡片已经不再是
        "活动卡"，也不再有状态推送，没人叫醒它就会一直显示"画面已在其他窗口中播放"。
        """
        self._listeners.append(callback)

    def _notify_finished(self) -> None:
        for callback in tuple(self._listeners):
            callback()

    def claim(
        self,
        view: CameraView,
        did: str,
        url: str,
        remote: bool,
        surface: str,
    ) -> bool:
        """登记一条新会话。已有别的承载面在播时返回 False，调用方必须先停它。"""
        if self._view is not None and self._view is not view:
            return False
        self._view = view
        self._did = did
        self._url = url
        self._remote = bool(remote)
        self._surface = surface
        return True

    def move(self, target: CameraView, surface: str) -> bool:
        """把当前会话换到 ``target``；失败时源控件继续播，所有权不变。"""
        source = self._view
        if source is None:
            return False
        if source is target:
            self._surface = surface
            return True
        if not source.hand_off_to(target):
            return False
        self._view = target
        self._surface = surface
        return True

    def set_surface(self, view: CameraView, surface: str) -> bool:
        """只改承载面名字（独立窗口弹出/收回），不动播放器。"""
        if self._view is None or self._view is not view:
            return False
        self._surface = surface
        return True

    def set_url(self, view: CameraView, url: str, remote: bool | None = None) -> bool:
        """地址是点播放后才取到的：补登记到当前所有者上，供独立窗口接管。"""
        if self._view is None or self._view is not view:
            return False
        self._url = url
        if remote is not None:
            self._remote = bool(remote)
        return True

    def forget(self, view: CameraView | None = None) -> str:
        """只清所有权、不碰控件画面：用于"已经报错、但错误界面要留着"的场合。"""
        if self._view is None:
            return ""
        if view is not None and self._view is not view:
            return ""
        did = self._did
        self._view = None
        self._did = ""
        self._url = ""
        self._remote = False
        self._surface = ""
        self._notify_finished()
        return did

    def release(self, view: CameraView | None = None, reason: str = "") -> bool:
        """结束会话并归还观看位。``view`` 不是当前所有者时什么都不做。"""
        active = self._view
        if active is None:
            return False
        if view is not None and active is not view:
            return False
        did = self._did
        self._view = None
        self._did = ""
        self._url = ""
        self._remote = False
        self._surface = ""
        timing.mark("结束播放会话", reason or "未说明原因")
        active.stop()
        active.release_player()
        if did:
            from ..core.camera import shared as camera_service

            camera_service().release(did)
        self._notify_finished()
        return True


_OWNERSHIP = PlaybackOwnership()


def playbacks() -> PlaybackOwnership:
    """全局唯一的活动播放所有者。"""
    return _OWNERSHIP


__all__ = [
    "CameraView",
    "PlaybackHandoff",
    "PlaybackOwnership",
    "playbacks",
]
