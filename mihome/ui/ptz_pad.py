# SPDX-License-Identifier: GPL-3.0-or-later
"""云台方向盘：一个圆，上下左右四条指令。

为什么是圆形控件而不是四个按钮：这台摄像头（``chuangmi.camera.069a01``）是 360°
云台，用户在米家 App 里习惯了"拖方向盘"，四个并排的方形按钮既占宽度，又要额外的
间距与描边。圆里切成四块，按下哪一块就往哪边转，跟硬件形态对得上。

交互是「按住连转」，但要先区分"点一下"和"按住"：

- **点一下 = 一条指令**。设备每条指令转的是固定角度（实测约画面宽度的 10–14%，
  ~15°），固件不接受任何步长参数（`step` / `duration` / `speed` / `time` 全被忽略，
  见 docs/dev/camera.md「云台」）。所以单击只会发一条，响应用户"点一下走一格"。
- **按住超过** :attr:`HOLD_MS` **才开始补发**，之后每 :attr:`INTERVAL_MS` 一条。
  这两段必须分开：只有一条 220ms 的节拍时，一次普通点击（按下到松开常常 300ms 上下）
  会顺手多发一条，观感就是"点一下转了两格"。
- **上一条没回来不补下一条**——网络一堵就积压一串指令、松手后还在转，是这类控件最容易
  踩的坑。:attr:`MAX_HOLD_S` 是兜底：鼠标卡住 / 收不到松开事件时，12 秒必须停。

能不能用由承载它的页面决定（``set_ready``）：没在播放时 go2rtc 那边没有 producer，
指令会被拒（``stream has no ptz producer``），所以画面没起来时整块置灰。

两种形态
--------
* **详情页**：默认形态，132 px 的不透明底盘，占布局位置（画面下方，和卡片下边距对齐）。
* **独立窗口**（``overlay=True``）：96 px，**叠在画面上**、常驻半透明。独立窗口里
  画面是主角，一个不透明的圆压在画面上比"没有这个键"更碍事；所以底盘改用与右下角
  那排圆钮同一套的黑色半透明（``_RoundButton`` 的 110），鼠标移上去再加 40。四角
  必须真的透出去，因此 overlay 形态要 ``WA_TranslucentBackground``。
"""

from __future__ import annotations

import math
import threading
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from ..core.camera import PTZ_DOWN, PTZ_LEFT, PTZ_RIGHT, PTZ_UP
from ..core.camera import shared as camera_service
from . import theme

#: 方向 → 旋转角度（基准三角形朝上，Qt 的 rotate 正值是顺时针）
_ROTATE = {PTZ_UP: 0.0, PTZ_RIGHT: 90.0, PTZ_DOWN: 180.0, PTZ_LEFT: 270.0}


class PtzPad(QWidget):
    """按住某个方向就连转，松手停。"""

    #: 外圆直径（详情页，占布局）
    SIZE = 132
    #: 叠在画面上的半透明版直径（独立窗口）——比详情页小一号，别把画面压掉一块。
    #: 96 时真机截图里箭头细到看不清（画面是白墙那一段尤其），104 是"小而不糊"的下限
    OVERLAY_SIZE = 104
    #: 半透明底盘的黑色 alpha。110（和右下角那排圆钮一样）在亮画面上太淡，
    #: 白色箭头会糊进白墙里；150 还能看见底下的画面，但对比度够
    OVERLAY_ALPHA = 150
    #: 中心孔半径占比：这块是"轴心"，不响应点击
    HUB = 0.36
    #: 箭头中心距圆心的比例
    ARROW_AT = 0.68
    #: 箭头大小（外接圆半径）占直径的比例（132 → 9.0）
    ARROW_RATIO = 0.068
    #: 按下后要按住这么久才开始补发（短于它的松开算"点一下"，只发一条）
    HOLD_MS = 450
    #: 连转时的补发间隔（米家 App 那种"走一格停一下"的节奏）
    INTERVAL_MS = 220
    #: 兜底：最多连续转这么久，防止收不到松开事件
    MAX_HOLD_S = 12.0

    def __init__(
        self,
        did: str,
        palette: theme.Palette,
        accent: str = "",
        parent: QWidget | None = None,
        *,
        size: int | None = None,
        overlay: bool = False,
    ) -> None:
        super().__init__(parent)
        self._did = did
        self._p = palette
        self._accent = accent
        self._overlay = bool(overlay)
        self._size = int(size or (self.OVERLAY_SIZE if self._overlay else self.SIZE))
        self._hover: int = 0
        self._active: int = 0
        self._ready = False
        self._inflight = False
        self._repeating = False
        self._inside = False
        self._deadline = 0.0
        self.setFixedSize(self._size, self._size)
        self.setMouseTracking(True)
        self.setCursor(Qt.ArrowCursor)
        self.setToolTip("按住方向键转动摄像头")
        if self._overlay:
            # 圆外必须透出画面：不设的话四角会被父控件的底色填成一块方块
            self.setAttribute(Qt.WA_TranslucentBackground)

        self._timer = QTimer(self)
        self._timer.setInterval(self.INTERVAL_MS)
        self._timer.timeout.connect(self._tick)

    # -- 外部接口 --------------------------------------------------------
    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.update()

    def set_ready(self, ready: bool) -> None:
        """画面起来了才能转：go2rtc 那边没有 producer 时会拒掉指令。"""
        if self._ready == ready:
            return
        self._ready = ready
        if not ready:
            self.stop()
        self.setCursor(Qt.PointingHandCursor if ready else Qt.ArrowCursor)
        self.setToolTip(
            "按住方向键转动摄像头" if ready else "点播放后才能转动摄像头"
        )
        self.update()

    def stop(self) -> None:
        """松开 / 关页面 / 画面停止：立刻不发新指令（在飞的那条让它自己回来）。"""
        self._timer.stop()
        # 回到"还没开始连转"的状态，下一次按下重新按 HOLD_MS 计时
        self._repeating = False
        self._timer.setInterval(self.INTERVAL_MS)
        if self._active:
            self._active = 0
            self.update()

    # -- 几何 ------------------------------------------------------------
    @property
    def diameter(self) -> int:
        """外圆直径（承载面做定位时用，别去读 ``SIZE``——overlay 形态不是它）。"""
        return self._size

    def _center(self) -> QPointF:
        return QPointF(self.width() / 2.0, self.height() / 2.0)

    def _radius(self) -> float:
        return self._size / 2.0 - 1.0

    def _arrow_r(self) -> float:
        # 箭头跟着直径缩，两种形态的相对大小才一样
        return max(6.0, self._size * self.ARROW_RATIO)

    def _hit(self, pos: QPointF) -> int:
        """落点在哪个方向；中心孔和圆外都返回 0。"""
        c = self._center()
        dx = pos.x() - c.x()
        dy = c.y() - pos.y()  # 换成数学坐标系（y 向上），atan2 的角度才是"上=90"
        dist = math.hypot(dx, dy)
        r = self._radius()
        if dist < r * self.HUB or dist > r:
            return 0
        angle = math.degrees(math.atan2(dy, dx)) % 360.0
        if 45.0 <= angle < 135.0:
            return PTZ_UP
        if 135.0 <= angle < 225.0:
            return PTZ_LEFT
        if 225.0 <= angle < 315.0:
            return PTZ_DOWN
        return PTZ_RIGHT

    @staticmethod
    def _sector_span(op: int) -> tuple[int, int]:
        """方向 → QPainter.drawPie 的 (起始 1/16 度, 跨度)，0° 在 3 点钟、逆时针。"""
        return {
            PTZ_UP: (45 * 16, 90 * 16),
            PTZ_LEFT: (135 * 16, 90 * 16),
            PTZ_DOWN: (225 * 16, 90 * 16),
            PTZ_RIGHT: (315 * 16, 90 * 16),
        }[op]

    # -- 绘制 ------------------------------------------------------------
    def _tones(self) -> dict[str, QColor]:
        """盘面配色。叠在画面上时不能依赖卡片底色（底下是亮的画面），
        统一改成"黑半透明 + 白图形"，跟右下角那排圆钮同源。"""
        if not self._overlay:
            return {
                "face": QColor(self._p.bg_soft),
                "edge": QColor(self._p.border),
                "arrow": QColor(self._p.text if self._ready else self._p.text_mute),
                "hot": QColor(self._accent),
                "hub": QColor(self._p.card),
                "hub_edge": QColor(self._p.border),
            }
        alpha = min(225, self.OVERLAY_ALPHA + (45 if self._inside else 0))
        return {
            "face": QColor(0, 0, 0, alpha),
            "edge": QColor(255, 255, 255, 60),
            "arrow": QColor(255, 255, 255, 235 if self._ready else 105),
            "hot": QColor(255, 255, 255),
            "hub": QColor(255, 255, 255, 26),
            "hub_edge": QColor(255, 255, 255, 44),
        }

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = self._center()
        r = self._radius()
        box = QRectF(c.x() - r, c.y() - r, r * 2, r * 2)
        tone = self._tones()

        # 底盘：普通形态比卡片略浅一档、靠 1px 描边和画面区分开；
        # 叠加形态是半透明黑，靠画面自己透出来
        p.setPen(QPen(tone["edge"], 1.0))
        p.setBrush(tone["face"])
        p.drawEllipse(box)

        # 高亮：悬停淡一点、按下更实，和右下角那排按钮同一套语言
        lit = self._active or self._hover
        if lit and self._ready:
            start, span = self._sector_span(lit)
            if self._overlay:
                tone["hot"].setAlpha(78 if self._active else 34)
            else:
                tone["hot"].setAlpha(90 if self._active else 44)
            p.setPen(Qt.NoPen)
            p.setBrush(tone["hot"])
            p.drawPie(box, start, span)

        # 四条箭头
        arrow = tone["arrow"]
        hot = QColor(tone["hot"])
        hot.setAlpha(255)
        for op in (PTZ_UP, PTZ_RIGHT, PTZ_DOWN, PTZ_LEFT):
            p.save()
            angle = math.radians(_ROTATE[op])
            p.translate(c.x() + math.sin(angle) * r * self.ARROW_AT,
                        c.y() - math.cos(angle) * r * self.ARROW_AT)
            p.rotate(_ROTATE[op])
            path = QPainterPath()
            at = self._arrow_r()
            path.addPolygon(
                QPolygonF(
                    [
                        QPointF(0, -at),
                        QPointF(-at * 0.78, at * 0.62),
                        QPointF(at * 0.78, at * 0.62),
                    ]
                )
            )
            path.closeSubpath()
            p.setPen(Qt.NoPen)
            on = self._ready and (self._active == op or self._hover == op)
            p.setBrush(hot if on else arrow)
            p.drawPath(path)
            p.restore()

        # 中心轴心：画一个小圆，让"圆环 + 三角"看起来是一个方向盘而不是四个按钮
        hub = r * self.HUB
        p.setPen(QPen(tone["hub_edge"], 1.0))
        p.setBrush(tone["hub"])
        p.drawEllipse(QRectF(c.x() - hub, c.y() - hub, hub * 2, hub * 2))
        p.end()

    # -- 交互 ------------------------------------------------------------
    def enterEvent(self, event) -> None:  # noqa: N802
        self._inside = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._inside = False
        self._hover = 0
        self.stop()
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        op = self._hit(event.position()) if self._ready else 0
        if op != self._hover:
            self._hover = op
            self.update()
        # 按住时拖到另一个方向就换方向，不用松开重按
        if self._active and op and op != self._active:
            self._active = op
            self._send()
            self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton or not self._ready:
            return
        op = self._hit(event.position())
        if not op:
            return
        self._active = op
        self._deadline = time.monotonic() + self.MAX_HOLD_S
        self._send()
        # 第一拍是 HOLD_MS：短于它的松开就是"点一下"，只会发出上面那一条
        self._repeating = False
        self._timer.start(self.HOLD_MS)
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            return
        self.stop()
        self.update()

    # -- 发指令 ----------------------------------------------------------
    def _tick(self) -> None:
        if not self._active:
            self._timer.stop()
            return
        if time.monotonic() > self._deadline:
            # 12 秒兜底：正常松开一定会停，走到这里说明松开事件丢了
            self.stop()
            return
        if not self._repeating:
            # 第一次补发：用户确实按住了，节拍从 HOLD_MS 切回 INTERVAL_MS
            self._repeating = True
            self._timer.setInterval(self.INTERVAL_MS)
        self._send()

    def _send(self) -> None:
        if self._inflight or not self._active:
            return
        self._inflight = True
        op = self._active
        threading.Thread(
            target=self._send_worker, args=(op,), name="mihome-ptz", daemon=True
        ).start()

    def _send_worker(self, op: int) -> None:
        try:
            camera_service().ptz(self._did, op)
        except Exception:  # noqa: BLE001 - 云台失败不该把详情页带崩
            pass
        finally:
            self._inflight = False


__all__ = ["PtzPad"]
