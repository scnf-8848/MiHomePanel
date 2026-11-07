"""详情页里的实时画面控件。

只做三件事：把 ``core/camera.py`` 给的本机 RTSP 交给 ``QMediaPlayer``、把帧画成
圆角卡片、把"为什么没有画面"说清楚。P2P、协议、子进程都在 ``core/camera.py``。

为什么用 ``QMediaPlayer`` 而不是自己解析 MJPEG：go2rtc 的 ``/api/frame.jpeg``
每次都要调 ffmpeg 转码（实测 700ms/帧），而 RTSP 是直通，由 Qt 自带的 FFmpeg
后端解码——本机实测 2960×1666 的 HEVC 能到 20fps，且不依赖系统里装没装 ffmpeg。

**进页面不直接播**：取流要先打洞，首帧 3–6 秒。直接播等于让用户盯着黑屏等，
所以打开详情页先"预热"——照常建立 RTSP 会话，收到首帧立刻 ``pause()`` 并把它
留作封面，中间压一个播放键。用户点下去才 ``play()``。实测同一路 RTSP 从暂停
恢复只要 0.01–0.08 秒（连接一直保持着，go2rtc 也在继续推流），等于瞬开。

播放中右下角常驻一个半透明暂停键，随时能停。暂停不是断开：画面停在当前帧，
再点一下立刻续上，不会重新打洞。但**暂停时设备仍在推流**（实测 ~170 KB/s），
而一台摄像头只允许 3 路并发，所以闲置久了要主动放掉连接——否则关着不看的详情页
也占着一个观看位。闲置阈值见 ``IDLE_RELEASE_SECONDS``（测试可用环境变量
``MIHOME_CAMERA_IDLE_SECONDS`` 调小）。
"""

from __future__ import annotations

import os
from collections.abc import Callable

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPolygonF,
)
from PySide6.QtMultimedia import QMediaPlayer, QVideoSink
from PySide6.QtWidgets import QPushButton, QWidget

from . import snapshots, theme
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

_VIDEO_BG = "#0E1013"
_FG = "#F5F7FA"
_FG_SOFT = "#B9C0CC"


class _RoundButton(QWidget):
    """画面上的圆形按钮：播放画三角，暂停画两道竖线。"""

    clicked = Signal()

    def __init__(self, parent: QWidget, size: int, alpha: int = 120) -> None:
        super().__init__(parent)
        self._paused_icon = False
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

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
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
        if self._paused_icon:
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


class CameraView(QWidget):
    """自绘视频画面。``retry`` 由外层接到重新取流的逻辑上。"""

    PRELOADING = "preloading"
    READY = "ready"
    PLAYING = "playing"
    PAUSED = "paused"
    RELEASED = "released"
    ERROR = "error"
    STOPPED = "stopped"

    retry = Signal()
    state_changed = Signal(str)

    def __init__(
        self, palette: theme.Palette, accent: str, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._p = palette
        self._accent = accent
        self._image: QImage | None = None
        # 上次成功播放时的静止画面。预热那 5–7 秒垫在底下，别让用户盯着纯黑等
        self._stale: QImage | None = None
        self._did = ""
        self._frames = 0
        self._hint = "正在连接摄像头…"
        self._sub = "首次连接约 3–6 秒"
        self._state = CameraView.STOPPED
        self._freeze = True
        self._autoplay = False
        self._stopping = False
        self._url = ""
        self._retries = 1
        self._hover = False
        self._hint_provider: Callable[[], str] | None = None
        self.setFixedHeight(220)
        self.setMinimumWidth(240)
        self.setMouseTracking(True)

        # 闲置自动放连接：就绪（预热完没点播放）和暂停都算闲置
        self._idle_ms = int(
            max(1.0, float(os.environ.get("MIHOME_CAMERA_IDLE_SECONDS", IDLE_RELEASE_SECONDS)))
            * 1000
        )
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.timeout.connect(self._release_idle)

        self._player = QMediaPlayer(self)
        self._sink = QVideoSink(self)
        self._player.setVideoSink(self._sink)
        self._sink.videoFrameChanged.connect(self._on_frame)
        self._player.errorOccurred.connect(self._on_error)

        # 中央播放键：只在"就绪 / 暂停"时出现
        self._play_btn = _RoundButton(self, 58, 150)
        self._play_btn.set_playing_icon(False)
        self._play_btn.clicked.connect(self.toggle)
        self._play_btn.hide()

        # 右下角暂停键：播放中鼠标在画面上时出现
        self._pause_btn = _RoundButton(self, 34, 110)
        self._pause_btn.set_playing_icon(True)
        self._pause_btn.clicked.connect(self.toggle)
        self._pause_btn.hide()

        # 错误态的重试按钮，文案型
        self._btn = QPushButton("重试", self)
        self._btn.setCursor(Qt.PointingHandCursor)
        self._btn.setStyleSheet(
            "QPushButton{background:rgba(255,255,255,0.14);color:#FFFFFF;border:none;"
            f"border-radius:{theme.RADIUS_SMALL}px;padding:7px 18px;font-size:13px;}}"
            "QPushButton:hover{background:rgba(255,255,255,0.24);}"
        )
        self._btn.clicked.connect(self.retry.emit)
        self._btn.hide()

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
        if state in (CameraView.READY, CameraView.PAUSED):
            self._idle.start(self._idle_ms)
        else:
            self._idle.stop()
        self._refresh_buttons()
        self.state_changed.emit(state)
        self.update()

    def _refresh_buttons(self) -> None:
        ready = self._state in (CameraView.READY, CameraView.PAUSED, CameraView.RELEASED)
        playing = self._state == CameraView.PLAYING
        self._play_btn.setVisible(ready)
        self._pause_btn.setVisible(playing and self._hover)
        self._btn.setVisible(self._state == CameraView.ERROR)
        self._place_buttons()
        self.setCursor(Qt.PointingHandCursor if ready else Qt.ArrowCursor)

    # -- 播放控制 --------------------------------------------------------
    def preload(self, url: str, *, autoplay: bool = False, did: str = "") -> None:
        """建立连接但不给用户看实时画面：首帧一到就暂停，留作封面。

        ``autoplay=True`` 时不暂停，直接连续播放（验证脚本和"自动播放"选项用）。
        ``did`` 用来找上次存的静止画面当占位；没有就还是纯黑底 + 提示。
        """
        self._autoplay = autoplay
        self._url = url
        self._retries = 1
        self._image = None
        if did and did != self._did:
            self._did = did
            self._stale = snapshots.load(did)
        elif did:
            self._did = did
        self._frames = 0
        self._hint = "正在连接摄像头…"
        self._sub = "显示的是上次的画面" if self._stale else "首次连接约 3–6 秒"
        self.setToolTip("")
        self._freeze = False
        self.update()
        # 同一地址重播也要走 stop，否则 QMediaPlayer 认为源没变、不会重连
        self._stopping = True
        self._player.stop()
        self._stopping = False
        self._set_state(CameraView.PRELOADING)
        self._player.setSource(QUrl(url))
        self._player.play()

    def play(self, url: str) -> None:
        """直接连续播放，不等用户点播放键。"""
        self.preload(url, autoplay=True)

    def toggle(self) -> None:
        """播放键 / 暂停键共用的开关。"""
        if self._state in (CameraView.READY, CameraView.PAUSED):
            self._freeze = False
            self._player.play()
            self._set_state(CameraView.PLAYING)
        elif self._state == CameraView.PLAYING:
            self._freeze = True
            self._save_stale()
            self._player.pause()
            self._set_state(CameraView.PAUSED)
        elif self._state == CameraView.RELEASED:
            # 连接已经放掉了，得整条重开：交给外层重新取流（会走一次 3–6 秒预热）
            self._hint = "正在重新连接…"
            self._sub = "首次连接约 3–6 秒"
            self._set_state(CameraView.PRELOADING)
            self.retry.emit()

    def _release_idle(self) -> None:
        """闲置够久就放掉连接。

        不这么做的话，"开着详情页但没在看"也会一直占一个观看位、一直走摄像头的
        上行带宽——一台设备只允许 3 路并发，这个位子该留给真在看的人。
        """
        if self._state not in (CameraView.READY, CameraView.PAUSED):
            return
        self.stop()
        minutes = max(1, round(self._idle_ms / 60_000))
        self._hint = f"闲置 {minutes} 分钟，连接已释放"
        self._sub = "点播放重新连接"
        self._set_state(CameraView.RELEASED)
        self.update()

    def _save_stale(self) -> None:
        """把当前这帧留作下次打开时的占位图。没有设备 id 或还没收到画面就不写。"""
        if self._image is None:
            return
        # 本进程里也留着：闲置释放 / 报错时垫在下面，比纯黑好认
        self._stale = self._image
        if self._did:
            snapshots.save(self._did, self._image)

    def set_hint_provider(self, provider: Callable[[], str] | None) -> None:
        """装一个"为什么断了"的查询函数（外层用 go2rtc 日志判断观看位是否被占满）。"""
        self._hint_provider = provider

    def stop(self) -> None:
        # `stop()` 是异步的：已经排队的最后一两帧还会到 sink。留着 `_stopping`
        # 让 `_on_frame` 直接丢，否则停了之后画面还会跳一下、帧计数也会涨。
        self._save_stale()
        self._stopping = True
        self._player.stop()
        self._player.setSource(QUrl())
        self._image = None
        self._freeze = True
        self._url = ""
        self._set_state(CameraView.STOPPED)
        self.update()

    def show_error(self, message: str) -> None:
        """取流前的错误（没装 go2rtc、设备离线、没局域网地址）。"""
        self.stop()
        self._hint = message
        self._sub = ""
        self.setToolTip(message)
        self._set_state(CameraView.ERROR)
        self.update()

    # -- 事件 ------------------------------------------------------------
    def _on_frame(self, frame: object) -> None:
        if self._stopping or self._freeze:
            return
        try:
            if not frame.isValid():  # type: ignore[attr-defined]
                return
            img = frame.toImage()  # type: ignore[attr-defined]
        except (AttributeError, TypeError, RuntimeError):
            return
        if img.isNull():
            return
        self._image = img
        self._frames += 1
        # 每 300 帧（约 15 秒）刷新一次占位图：程序被强杀时也能留个近照
        if self._frames % 300 == 0:
            self._save_stale()
        self._hint = ""
        self._sub = ""
        self.update()
        if self._state == CameraView.PRELOADING:
            if self._autoplay:
                self._set_state(CameraView.PLAYING)
            else:
                # 首帧到手 = 这一路已经打通，立刻冻住当封面，等用户点播放
                self._freeze = True
                self._player.pause()
                self._set_state(CameraView.READY)

    def _on_error(self, _err: object, message: str) -> None:
        if self._stopping:
            return
        # 首帧之前出错多半是瞬时的（go2rtc 刚起、RTSP 协商撞了一下），自己重连一次，
        # 别让用户为一次握手失败去点重试
        if self._retries > 0 and self._url:
            self._retries -= 1
            self._freeze = True
            self._image = None
            self._hint = "正在重新连接…"
            self._sub = ""
            self._set_state(CameraView.PRELOADING)
            self.update()
            QTimer.singleShot(400, self._reconnect)
            return
        self._freeze = True
        self._image = None
        self._hint = self._reason() or (
            "画面中断，点重试重新连接" if self._frames else "无法打开画面"
        )
        self._sub = ""
        self.setToolTip(message or "")
        self._set_state(CameraView.ERROR)
        self.update()

    def _reason(self) -> str:
        """问外层"刚才为什么断了"。取不到就返回空串，用默认文案。"""
        if self._hint_provider is None:
            return ""
        try:
            return (self._hint_provider() or "").strip()
        except Exception:  # noqa: BLE001 - 提示拿不到不该影响报错本身
            return ""

    def _reconnect(self) -> None:
        if not self._url or self._state != CameraView.PRELOADING:
            return
        self._freeze = False
        self._stopping = True
        self._player.stop()
        self._stopping = False
        self._player.setSource(QUrl(self._url))
        self._player.play()

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
        self._pause_btn.move(
            self.width() - self._pause_btn.width() - pad,
            self.height() - self._pause_btn.height() - pad,
        )
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

        img = self._image if self._image is not None else self._stale
        live = self._image is not None and not self._image.isNull()
        if img is not None and not img.isNull():
            iw, ih = img.width(), img.height()
            if iw > 0 and ih > 0:
                # 等比 cover：填满画布，多出来的裁掉（摄像头画面比例和控件不一定一致）
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
            if not live:
                # 占位图：整体压暗 + 中间再压一条渐变带，保证字在亮背景上也读得清；
                # 不压成纯黑是因为"上次的画面"本身有信息量（门口有没有人）
                painter.fillRect(rect, QColor(0, 0, 0, 120))
                lifted = self._state == CameraView.RELEASED
                # 释放/错误态的文案比加载态高（要给中央播放键让位），渐变带跟着上移
                top = rect.center().y() - (108 if lifted else 60)
                band = QRect(rect.x(), top, rect.width(), 172 if lifted else 124)
                grad = QLinearGradient(band.topLeft(), band.bottomLeft())
                grad.setColorAt(0.0, QColor(0, 0, 0, 0))
                grad.setColorAt(0.45, QColor(0, 0, 0, 128))
                grad.setColorAt(1.0, QColor(0, 0, 0, 0))
                painter.fillRect(band, grad)
                self._draw_status(painter, rect, lifted)
                painter.end()
                return
            if self._state in (CameraView.READY, CameraView.PAUSED):
                painter.fillRect(rect, QColor(0, 0, 0, 105))
            elif self._state == CameraView.PLAYING and self._hover:
                painter.fillRect(rect, QColor(0, 0, 0, 28))
            if self._state == CameraView.READY:
                # 画面内容不可控（栏杆、逆光），裸文字读不清，垫一个半透明胶囊
                tip = "已就绪，点击播放"
                font = QFont(self.font())
                font.setPixelSize(12)
                box_w = QFontMetrics(font).horizontalAdvance(tip) + 26
                box = QRect(
                    rect.center().x() - box_w // 2,
                    rect.y() + rect.height() // 2 + 26,
                    box_w,
                    22,
                )
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(0, 0, 0, 128))
                painter.drawRoundedRect(box, 11, 11)
                draw_text(
                    painter, box, tip, size=12, color=_FG, align=Qt.AlignCenter
                )
            painter.end()
            return

        # 空态 / 加载态 / 错误态（也可能是占位图上面盖一层字）
        self._draw_status(painter, rect, self._state == CameraView.RELEASED)
        painter.end()

    def _draw_status(self, painter: QPainter, rect: QRect, lifted: bool) -> None:
        """画面区中央的几行字。``lifted`` = 中央有播放键，文字要让开别压上去。"""
        mid = rect.y() + rect.height() // 2
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
        if self._state == CameraView.PRELOADING:
            # 一条随 accent 呼吸的细线，表示"还在连"，避免用户以为卡死
            w = int(rect.width() * 0.22)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(self._accent))
            painter.drawRoundedRect(
                QRect((rect.width() - w) // 2, line.bottom() + (44 if self._sub else 30), w, 3),
                2,
                2,
            )


__all__ = ["CameraView"]
