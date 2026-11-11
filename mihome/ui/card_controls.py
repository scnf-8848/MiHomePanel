"""卡片内联控件：直接摆进设备卡片的 body 区，不必打开详情窗。

四条约定，四个控件都遵守：

1. **高度写死，宽度交给模板**。滑条/步进器 36 高，胶囊 26 高；内部栅格按
   ``width()`` 实时重算，所以同一控件在 1 列卡和 2 列卡里都不用改代码。
2. **拖动只改本地视觉，松手才发一次命令**。mijiaAPI 走云端 HTTP，一次往返几百
   毫秒；边拖边发会把请求队列堵满，设备反应还会滞后于手。拖动过程中只发
   ``previewed``（让卡片能即时染色/更新数字），``committed`` 在松手时发一次。
3. **拖动期间忽略外部赋值**。设备上报可能正好在拖动中到达，直接 set_value 会让
   滑块跳回去。``set_value()`` 在 ``_dragging`` 为真时自动跳过。
4. **不吞掉卡片的点击语义**。这些都是真实 QWidget，落在它们身上的鼠标事件不会
   冒泡给卡片，所以「点空白开详情、点滑条调亮度」不需要额外判断——这是不用
   QPainter 手绘滑条的主要理由（拖动捕获、移出控件后的追踪都是白送的）。
5. **滚轮只滚页面，不改参数**。滑条和步进器都混入 ``WheelSafe``，滚轮事件
   ``ignore()`` 后冒泡给外层 ``QScrollArea``；点击、拖动、长按加减不受影响。
"""

from __future__ import annotations

import math
from typing import Any

from PySide6.QtCore import QPointF, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..core.catalog import format_value
from ..core.spec import PropertySpec
from . import theme
from .wheel_guard import WheelSafe
from .widgets import ToggleSwitch, draw_text

#: 一行控件的高度。卡片 body 区 76 高，正好放两行（36 + 4 + 36）。
ROW_H = 36
#: 滑条左侧属性名、右侧数值的固定宽度
LABEL_W = 40
VALUE_W = 48
TRACK_H = 6
THUMB_D = 14
#: 胶囊控件的高度
CHIP_H = 26
#: 分段控件最多切几段，超过就退步进器。15 档（净化器）切出来每段只有 20px，
#: "26-45" 这种文案一个也塞不下，等于把可选项藏起来。
MAX_SEGMENTS = 8
#: 卡片内容带的标称宽度（2×1 / 2×2 都是 340 − 14×2）。造控件的时候 Qt 还没给
#: 几何，所以"这组标签放不放得下"只能按这个数回答，见 :func:`make_control`。
BODY_W = 312
#: 电容触摸的最小可点高度。桌面端鼠标可以更小，但卡片在触摸屏上也要能用。
MIN_HIT_H = 24


def kelvin_color(kelvin: float) -> QColor:
    """色温 → RGB（Tanner Helland 近似）。

    纯物理换算下 6500K 已经接近纯白，整条轨道会失去"冷暖方向感"，所以冷端
    再往蓝偏一点（最多 55%），让渐变一眼能看出哪头是冷。
    """
    t = max(1000.0, min(40000.0, float(kelvin))) / 100.0
    if t <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(t) - 161.1195681661
        b = 0.0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    else:
        r = 329.698727446 * ((t - 60) ** -0.1332047592)
        g = 288.1221695283 * ((t - 60) ** -0.0755148492)
        b = 255.0
    color = QColor(
        max(0, min(255, int(r))),
        max(0, min(255, int(g))),
        max(0, min(255, int(b))),
    )
    if kelvin > 4500:
        # 纯物理色温到 6500K 已经是纯白，轨道冷端会"消失"。这里最多往蓝偏 80%，
        # 让冷端在浅色卡片上也能被一眼认出来。
        mix = min(0.80, (kelvin - 4500) / 2000 * 0.80)
        color = QColor(theme.blend(color.name(), "#AFD0FF", mix))
    return color


def _numeric_range(
    prop: PropertySpec, *, fine_step: float = 0.0
) -> tuple[float, float, float]:
    lo, hi, step = prop.value_range or (0, 100, 1)
    lo, hi, step = float(lo), float(hi), float(step or 1)
    # 有些属性是 uint32 全域（0..4294967295），滑条按 int32 夹住才不丢精度到溢出
    hi = min(hi, 2**31 - 1)
    lo = max(lo, -(2**31))
    if hi <= lo:
        hi = lo + 1
    if step <= 0:
        step = 1.0
    if fine_step > 0:
        # 规格里的 step 是云端校验的最小合法档位。0.5 只有落在 step 的整数倍
        # 上才启用（0.1/0.25/0.5 这类规格）；int32/step=1 的设备保持 1 档，
        # 否则写 23.5 会被云端判为非法值。
        ratio = fine_step / step
        if ratio >= 1 and abs(ratio - round(ratio)) < 1e-6:
            step = fine_step
    else:
        step = max(1.0, step)
    return lo, hi, step


def _clean_number(value: float) -> int | float:
    """抹掉浮点累加噪声：24.0 → 24，23.500000000001 → 23.5。"""
    v = round(float(value), 6)
    return int(v) if abs(v - round(v)) < 1e-6 else v


class CardControl(QWidget):
    """载体：卡片内所有控件的公共部分（主题、置灰、尺寸策略）。

    ``CardBody`` 用这个类型持有它们——滑条、步进、读数对 ``sync`` 来说都是
    "能 ``set_value``、能置灰"的东西，具体是哪个不影响装配逻辑。
    """

    def __init__(
        self,
        prop: PropertySpec,
        palette: theme.Palette,
        accent: str,
        *,
        label: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.prop = prop
        self._p = palette
        self._accent = accent
        # 空串 = **故意不画标签**（卡片里的格子自己画，不再重复一遍）；
        # None / 省略 = 用属性自己的行名。两者不能混：早先写 ``label or``
        # 会让 ``label=""`` 悄悄退回去，格子里就出现两遍同样的字。
        self._label = prop.display_label if label is None else label
        self._dimmed = False
        self._write = prop.writeable
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)

    # -- 主题 ------------------------------------------------------------
    def apply_theme(self, palette: theme.Palette, accent: str) -> None:
        self._p = palette
        self._accent = accent
        self.update()

    def set_dimmed(self, dim: bool) -> None:
        """设备关着但控件还留在卡片上（灯的亮度条）时压一档透明度。"""
        if dim != self._dimmed:
            self._dimmed = dim
            self.update()

    def _ink(self) -> str:
        return self._p.text_mute if self._dimmed else self._p.text_soft

    def _fmt(self, value: float) -> str:
        return format_value(self.prop, _clean_number(value))

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(120, self.height())

    # -- 鼠标 ------------------------------------------------------------
    # 控件**必须把落在自己身上的左键吞掉**，包括"点了什么都不做"的分支。
    #
    # 为什么这条要写在基类：``DeviceCard.mouseReleaseEvent`` 只要"按下、松开都
    # 在卡片内"就 ``clicked.emit``（打开详情页），而 Qt 默认会把没被 accept 的
    # press 沿父链传上去。所以子控件里任何一个"没命中就 return"的早退，都会让
    # 松手冒泡到卡片——2026-11-12 用户报的"点 body 里的控件，结果打开了详情页"
    # 就是这个：值没写出去，详情页开了。
    #
    # 正确的边界是"控件矩形内的事归控件"：滑条的整行、轨道的整条、开关的整行
    # 都是控件的地盘。点空白处不改变值，但也不该被解释成"点了一下卡片"。
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            event.accept()
            return
        super().mouseReleaseEvent(event)


# ---------------------------------------------------------------------------
# 滑条：亮度、色温、窗帘开合度
# ---------------------------------------------------------------------------


class CardSlider(WheelSafe, CardControl):
    """图标 + 轨道 + 数值。``gradient=True`` 时轨道本身是色带（色温用）。"""

    previewed = Signal(float)  # 拖动中的实时值（只更新界面，别发命令）
    committed = Signal(int)  # 松手之后才发

    HEIGHT = ROW_H

    def __init__(
        self,
        prop: PropertySpec,
        palette: theme.Palette,
        accent: str,
        *,
        label: str | None = None,
        gradient: bool = False,
        compact: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(prop, palette, accent, label=label, parent=parent)
        self._lo, self._hi, self._step = _numeric_range(prop)
        self._value = self._lo
        self._gradient = gradient
        #: ``compact`` 给详情页用：左侧不画属性名（那一行已经有名字了），
        #: 轨道从最左边开始，只给右侧数值留固定宽度。
        self._compact = compact
        self._unknown = False
        self._dragging = False
        self.setFixedHeight(self.HEIGHT)
        # 滑条只认鼠标：点击跳值、拖动松手提交。键盘（方向键 / PageUp/Down /
        # Home/End）一律不接，也不抢焦点——误触方向键不该悄悄改设备参数。

    # -- 值 --------------------------------------------------------------
    @property
    def dragging(self) -> bool:
        return self._dragging

    def value(self) -> float:
        return self._value

    def set_value(self, value: object, animate: bool = False) -> None:
        # 拖动中不接外部值：设备上报晚到会让滑块往回跳一格，手感像卡住
        if self._dragging or value is None:
            return
        try:
            v = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        self._unknown = False
        v = min(self._hi, max(self._lo, v))
        if abs(v - self._value) > 1e-6:
            self._value = v
            self.update()

    def set_unknown(self) -> None:
        """值没读到：轨道置灰、不画滑块、数值显示「—」。

        详情页「更多功能」里的行是懒读的，没读到时不能停在最小值假装读成功。
        """
        self._unknown = True
        self.update()

    # -- 栅格 ------------------------------------------------------------
    #: thumb 的最大半径（拖动时比静止大 1px）。轨道两端和「圆心可用范围」都按它算：
    #: 圆点的**圆心**必须离控件边缘至少一个半径，否则圆心落在 x0 时圆有一半画到
    #: 控件外面（详情页滑条最左端就是这么被裁的）。
    _THUMB_R = THUMB_D / 2 + 1

    def _span(self) -> tuple[float, float]:
        """thumb **圆心** 的可用范围（左右各内缩一个半径）。

        注意这里返回的不是轨道两端：轨道要贴边画，见 :meth:`_track_span`。
        点击 / 拖动换算也走这个范围，所以轨道最外侧那几像素会被 clamp 到端点值，
        行为与「整条轨道可点」一致。
        """
        r = self._THUMB_R
        if self._compact:
            x0 = 1.0 + r
            return x0, float(max(x0 + 40, self.width() - VALUE_W - 2 - r))
        x0 = float(LABEL_W + 8 + r)
        x1 = float(max(x0 + 40, self.width() - VALUE_W - 8 - r))
        return x0, x1

    def _track_span(self) -> tuple[float, float]:
        """轨道左右端：由圆心范围外扩一个半径，回到原来的视觉位置（贴边）。"""
        r = self._THUMB_R
        x0, x1 = self._span()
        return x0 - r, x1 + r

    def _ratio(self) -> float:
        return (self._value - self._lo) / (self._hi - self._lo)

    def _value_at(self, x: float) -> float:
        x0, x1 = self._span()
        r = min(1.0, max(0.0, (x - x0) / max(1.0, x1 - x0)))
        raw = self._lo + r * (self._hi - self._lo)
        raw = self._lo + round((raw - self._lo) / self._step) * self._step
        return min(self._hi, max(self._lo, raw))

    # -- 绘制 ------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        opacity = 0.55 if self._dimmed else 1.0
        if not self.isEnabled():
            opacity *= 0.45
        p.setOpacity(opacity)
        h = self.height()
        cy = h / 2.0

        if not self._compact:
            draw_text(p, QRect(0, 0, LABEL_W, h), self._label, size=12, color=self._ink())

        x0, x1 = self._span()
        # 轨道画到贴边位置，圆心范围已经从两端内缩过——两套坐标不要混用
        tx0, tx1 = self._track_span()
        track = QRect(int(tx0), int(cy - TRACK_H / 2), int(tx1 - tx0), TRACK_H)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.blend(self._p.card, self._p.text, 0.13)))
        p.drawRoundedRect(track, TRACK_H / 2, TRACK_H / 2)

        ratio = max(0.0, min(1.0, self._ratio()))
        if not self._unknown:
            if self._gradient:
                grad = QLinearGradient(tx0, 0.0, tx1, 0.0)
                # 只插两端的话，中间会被线性插值拉成灰白（橙→淡蓝的中点没有色相）。
                # 按色温本身取 5 个档，才是「橙→暖白→白→冷白」的真实过渡。
                for i in range(5):
                    t = i / 4.0
                    grad.setColorAt(t, kelvin_color(self._lo + (self._hi - self._lo) * t))
                p.setBrush(grad)
                p.drawRoundedRect(track, TRACK_H / 2, TRACK_H / 2)
            else:
                filled = int(track.width() * ratio)
                if filled > 1:
                    p.setBrush(QColor(self._accent))
                    p.drawRoundedRect(
                        QRect(track.x(), track.y(), filled, track.height()),
                        TRACK_H / 2,
                        TRACK_H / 2,
                    )

            thumb = QPointF(x0 + (x1 - x0) * ratio, cy)
            p.setBrush(QColor("#FFFFFF"))
            p.setPen(QPen(QColor(theme.blend(self._p.text, "#000000", 0.12 if not self._p.is_dark else 0.0)), 1))
            radius = THUMB_D / 2 + (1 if self._dragging else 0)
            p.drawEllipse(thumb, radius, radius)

        draw_text(
            p,
            QRect(self.width() - VALUE_W, 0, VALUE_W, h),
            "—" if self._unknown else self._fmt(self._value),
            size=12,
            color=self._p.text if not self._dimmed and not self._unknown else self._ink(),
            align=Qt.AlignRight | Qt.AlignVCenter,
        )
        p.end()

    # -- 交互 ------------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        # 禁用态也要吞掉（见 ``CardControl.mousePressEvent``）：不然点一下灰条
        # 就等于点了整张卡片，详情页会自己弹出来。
        if event.button() != Qt.LeftButton or not self.isEnabled():
            super().mousePressEvent(event)
            return
        self._dragging = True
        self._update_from(event.position().x())
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging and self.isEnabled():
            self._update_from(event.position().x())
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton or not self._dragging:
            super().mouseReleaseEvent(event)
            return
        self._dragging = False
        self._update_from(event.position().x())
        self.committed.emit(int(round(self._value)))
        event.accept()

    def _update_from(self, x: float) -> None:
        v = self._value_at(x)
        if abs(v - self._value) < 1e-6:
            return
        self._value = v
        self.update()
        self.previewed.emit(v)


# ---------------------------------------------------------------------------
# 步进器：空调温度
# ---------------------------------------------------------------------------


class CardStepper(WheelSafe, CardControl):
    """``− 26°  +`` 步进器（卡片 body 与详情页同款）。按住不放连续加减，16°→30° 不用点十四下。

    ``centered=True`` 时把「减号 + 数值 + 加号」当成**一整组**居中，并把减号挪到
    数字左侧（``− 26° +``）。卡片 body 有 312 宽（2×1），靠左贴右会在中间留出
    200px 空白，看着像少画了一块。数字两边各一枚按钮，也是温控面板 / 空调遥控器
    的固有样子——「左边调低、右边调高」比「两个按钮都挤在右边」好按得多，
    手指不用在同一个位置分辨哪个是减号。
    """

    committed = Signal(object)

    HEIGHT = ROW_H
    BTN = 26
    BTN_GAP = 6
    #: 数值与减号之间的距离。居中排布时这段也计入整组宽度。
    TEXT_GAP = 10
    #: 数值字号。居中排布（卡片 body）是卡片的主角，放大一档。
    VALUE_PX = 20
    VALUE_PX_CENTERED = 24
    HOLD_MS = 380
    REPEAT_MS = 90

    def __init__(
        self,
        prop: PropertySpec,
        palette: theme.Palette,
        accent: str,
        *,
        label: str | None = None,
        centered: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(prop, palette, accent, label=label, parent=parent)
        # 温度类属性（unit=celsius）尝试 0.5 的半度档位；规格 step 不允许时
        # ``_numeric_range`` 会沿用规格自己的最小合法档位（通常是 1）。
        fine = 0.5 if prop.unit == "celsius" else 0.0
        self._lo, self._hi, self._step = _numeric_range(prop, fine_step=fine)
        #: 枚举档位（扫风角度 30/60/90/120/140、净化器 15 档）。非空时内部一律
        #: 用**下标**当数值：``_lo=0``、``_hi=len-1``、``_step=1``，对外仍然收发
        #: 设备用的那个值。为什么让步进器吃枚举：离散小集合本来就该"上一下 /
        #: 下一下"，而胶囊在半个卡片宽（≈140px）里铺五个根本放不下。
        self._values: list[int] = (
            [o.value for o in prop.value_list]
            if prop.kind == "enum" and prop.value_list
            else []
        )
        self._labels: dict[int, str] = (
            {o.value: o.display_short for o in prop.value_list}
            if self._values
            else {}
        )
        if self._values:
            self._lo, self._hi, self._step = 0.0, float(len(self._values) - 1), 1.0
        self._value = self._lo
        self._hot = 0  # -1 减、+1 加、0 无
        self._centered = centered
        self.setFixedHeight(self.HEIGHT)
        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.setInterval(self.HOLD_MS)
        self._hold.timeout.connect(self._start_repeat)
        self._repeat = QTimer(self)
        self._repeat.setInterval(self.REPEAT_MS)
        self._repeat.timeout.connect(lambda: self._bump(self._hot))

    # -- 值 --------------------------------------------------------------
    def value(self) -> float:
        """当前值。枚举档位返回**设备那个值**（不是内部下标）。"""
        if self._values:
            return self._values[int(round(self._value))]
        return self._value

    def set_value(self, value: object, animate: bool = False) -> None:
        if value is None:
            return
        if self._values:
            # 收到的可能是字符串或不在档位里的值（固件加了新档）。不在表里就
            # 保持原状，别把指针甩到第一档去——那会让用户看到"自己跳回去了"。
            try:
                raw = int(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return
            if raw not in self._values:
                return
            idx = float(self._values.index(raw))
            if abs(idx - self._value) > 1e-6:
                self._value = idx
                self.update()
            return
        try:
            v = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        v = min(self._hi, max(self._lo, v))
        if abs(v - self._value) > 1e-6:
            self._value = v
            self.update()

    # -- 栅格 ------------------------------------------------------------
    def _value_px(self) -> int:
        return self.VALUE_PX_CENTERED if self._centered else self.VALUE_PX

    def _text_font(self) -> QFont:
        """量数值宽度用的字体，必须和 ``draw_text`` 实际画的一致（同族同字号）。"""
        f = QFont(self.font())
        f.setPixelSize(self._value_px())
        f.setWeight(QFont.DemiBold)
        return f

    def _text_at(self, v: float) -> str:
        """内部数值 → 显示文案。枚举档位时内部存的是下标。"""
        if self._values:
            return self._labels.get(self._values[int(round(v))], "")
        return self._fmt(v)

    def _layout(self) -> tuple[QRect, QRect, QRect]:
        """返回 (数值区, 减号, 加号) 三个矩形。居中/靠右的差别只在这里。

        居中（卡片 body 和详情页）是 ``− 数值 +``：整组居中，数字两侧各一枚，
        TEXT_GAP 左右对称。``centered=False`` 保留 ``数值 …… − +`` 的紧凑排布，
        给单独控件预览这类窄场景用。
        """
        y = (self.height() - self.BTN) // 2
        if not self._centered:
            plus = QRect(self.width() - self.BTN, y, self.BTN, self.BTN)
            minus = QRect(plus.x() - self.BTN_GAP - self.BTN, y, self.BTN, self.BTN)
            return QRect(0, 0, max(40, minus.x() - 8), self.height()), minus, plus

        fm = QFontMetrics(self._text_font())
        tw = fm.horizontalAdvance(self._text_at(self._value))
        # 数值矩形比字形宽 2px（给抗锯齿留一点余量），扣掉这 2px 后
        # 两侧间距才在**视觉上**相等：量的是矩形边到边，多出的 1px 会翻倍。
        text_w = tw + 2
        gap = self.TEXT_GAP - 1
        group = self.BTN * 2 + gap * 2 + text_w
        x = max(0, (self.width() - group) // 2)
        minus = QRect(x, y, self.BTN, self.BTN)
        text = QRect(minus.right() + 1 + gap, 0, text_w, self.height())
        plus = QRect(text.right() + 1 + gap, y, self.BTN, self.BTN)
        return text, minus, plus

    def _buttons(self) -> tuple[QRect, QRect]:
        _, minus, plus = self._layout()
        return minus, plus

    def sizeHint(self) -> QSize:  # noqa: N802
        """居中排布时按「两枚按钮 + 最宽数值」的整组宽度留位。

        详情页那一行是 ``标签 …… [− 数值 +]``，布局按 sizeHint 分配宽度；
        默认 120 放不下 24px 的 "23.5°C"（整组约 216px），会被裁掉右侧加号。
        按区间内会出现的文案（含半度值）取最大宽度，拖动时整组宽度也不会跳。
        """
        if not self._centered:
            return QSize(120, self.height())
        fm = QFontMetrics(self._text_font())
        # 半度值（16.5°C）比两端的整数文案更宽，别只看 lo/hi
        if self._values:
            # 枚举档位：文案长度不重要（"140" 比 "30" 宽），但取所有档位的最大值
            # 才能保证加减时整组宽度不跳
            samples = tuple(float(i) for i in range(len(self._values)))
        else:
            samples = (
                self._lo,
                self._hi,
                min(self._hi, self._lo + self._step),
                max(self._lo, self._hi - self._step),
            )
        text_w = max(fm.horizontalAdvance(self._text_at(v)) for v in samples) + 2
        gap = self.TEXT_GAP - 1
        return QSize(self.BTN * 2 + gap * 2 + text_w, self.height())

    # -- 绘制 ------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(0.55 if self._dimmed else 1.0)
        text_rect, minus, plus = self._layout()
        draw_text(
            p,
            text_rect,
            self._text_at(self._value),
            size=self._value_px(),
            color=self._p.text if self.isEnabled() else self._ink(),
            weight=QFont.DemiBold,
            align=(Qt.AlignCenter if self._centered
                   else Qt.AlignLeft | Qt.AlignVCenter),
        )
        for rect, glyph, hot, muted in (
            (minus, "\u2212", self._hot == -1, self._value <= self._lo),
            (plus, "+", self._hot == 1, self._value >= self._hi),
        ):
            p.setPen(Qt.NoPen)
            if hot:
                p.setBrush(QColor(theme.blend(self._p.card, self._accent, 0.30)))
            else:
                p.setBrush(QColor(theme.blend(self._p.card, self._p.text, 0.10)))
            p.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
            draw_text(
                p,
                rect,
                glyph,
                size=15,
                color=self._p.text_mute if muted else self._p.text,
                weight=QFont.Medium,
                align=Qt.AlignCenter,
                elide=False,
            )
        p.end()

    # -- 交互 ------------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton or not self.isEnabled():
            super().mousePressEvent(event)
            return
        minus, plus = self._buttons()
        pos = event.position().toPoint()
        if minus.adjusted(-6, -6, 6, 6).contains(pos):
            self._hot = -1
        elif plus.adjusted(-6, -6, 6, 6).contains(pos):
            self._hot = 1
        else:
            # 点在"−"和"+"中间那截数字上：不改值，但也不让事件冒泡出去
            super().mousePressEvent(event)
            return
        self._bump(self._hot)
        self._hold.start()
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._hold.stop()
        self._repeat.stop()
        if self._hot:
            self._hot = 0
            self.update()
        event.accept()

    def _start_repeat(self) -> None:
        if self._hot:
            self._repeat.start()

    def _snap(self, value: float) -> float:
        """落到规格档位网格上，避免 0.5 步长累加出 23.500000001。"""
        n = round((value - self._lo) / self._step)
        raw = self._lo + n * self._step
        return min(self._hi, max(self._lo, raw))

    def _bump(self, direction: int) -> None:
        target = self._snap(self._value + direction * self._step)
        if abs(target - self._value) < 1e-6:
            return
        self._value = target
        self.update()
        if self._values:
            # 枚举：发出去的是**设备那个值**，不是内部下标
            self.committed.emit(self._values[int(round(target))])
            return
        self.committed.emit(_clean_number(target))


# ---------------------------------------------------------------------------
# 胶囊组：模式、风速
# ---------------------------------------------------------------------------


class CardChips(CardControl):
    """横排胶囊。放不下时先压内边距、再截字，最后才丢弃多余项。

    ``spread=True`` 时把**余宽均分给每一项**，整排铺满控件宽度（分段控件的观感）。
    卡片 body 有 312 宽，五个两字模式只占 230，靠左排会在右边留一条空档；铺满
    之后每个胶囊一样宽，点哪儿都不会点错。
    """

    committed = Signal(int)

    HEIGHT = CHIP_H
    GAP = 6
    PAD = 11

    def __init__(
        self,
        prop: PropertySpec,
        palette: theme.Palette,
        accent: str,
        *,
        label: str | None = None,
        spread: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(prop, palette, accent, label=label, parent=parent)
        self._items: list[tuple[int, str]] = [
            (o.value, o.display_short) for o in prop.value_list
        ]
        # 初始**不选中任何一项**：以前默认选中第一项，真实值还没同步到时第一帧
        # 会假高亮成"第一个模式已激活"。现在只有 ``set_value`` 收到真实值才点亮，
        # 没有激活项（值不在 items 里，例如 0）时整排都是未选中态。
        self._current: int | None = None
        #: 上一次从 store 同步到的"激活判据"值（型号适配时是 scene-mode，否则为空）
        self._active_seen: int | None = None
        self._hover = -1
        self._spread = spread
        self.setFixedHeight(self.HEIGHT)
        self.setMouseTracking(True)
        self.setVisible(bool(self._items))

    # -- 值 --------------------------------------------------------------
    def value(self) -> int | None:
        return self._current

    def set_value(self, value: object, animate: bool = False) -> None:
        if value is None:
            return
        try:
            v = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        if v != self._current:
            self._current = v
            self.update()

    def set_active(self, value: object) -> None:
        """同步"当前激活判据"（型号适配后由 store 给出，如挂灯的 scene-mode）。

        和 ``set_value`` 的区别只在 0 的处理：0 是客户端语义"无激活项"，枚举里
        没有这一项，所以是"整排不选中"而不是"选中 0"。为了不让用户点完立刻灭，
        从某个非 0 值掉到 0 才清空；一直停在 0（开机就是"无"）时不覆盖本地刚
        点出来的乐观高亮——下一次真值变化会纠正。
        """
        if value is None:
            return
        try:
            v = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        prev, self._active_seen = self._active_seen, v
        if v == prev:
            return
        if v == 0:
            if prev is not None:
                self._current = 0
                self.update()
            return
        if v != self._current:
            self._current = v
            self.update()

    # -- 栅格 ------------------------------------------------------------
    def _font(self) -> QFont:
        f = QFont()
        f.setPixelSize(12)
        f.setWeight(QFont.Medium)
        return f

    def _layout(self) -> list[tuple[QRect, int, str]]:
        n = len(self._items)
        if n == 0:
            return []
        fm = QFontMetrics(self._font())
        avail = max(60, self.width())
        pad = self.PAD
        texts = [t for _, t in self._items]
        widths = [fm.horizontalAdvance(t) + pad * 2 for t in texts]
        total = sum(widths) + self.GAP * (n - 1)

        if total > avail:
            # 先挤内边距，通常挤完就够了（5 个两字模式 ≤ 300 宽）
            pad = max(6, pad - (total - avail) // max(1, n * 2))
            widths = [fm.horizontalAdvance(t) + pad * 2 for t in texts]
            total = sum(widths) + self.GAP * (n - 1)
        if total > avail:
            budget = max(26, (avail - self.GAP * (n - 1)) // n)
            widths = [min(w, budget) for w in widths]
            texts = [
                fm.elidedText(t, Qt.ElideRight, max(10, w - pad * 2))
                for t, w in zip(texts, widths)
            ]

        if self._spread and total < avail:
            # 有余宽才铺：紧的时候上面已经压过内边距/截过字，再撑开就又溢出了
            extra = (avail - total) // n
            widths = [w + extra for w in widths]

        out: list[tuple[QRect, int, str]] = []
        x = 0
        for (value, _), w, text in zip(self._items, widths, texts):
            if x + w > avail and out:
                break
            out.append((QRect(x, 0, w, self.height()), value, text))
            x += w + self.GAP
        return out

    # -- 绘制 ------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(0.55 if self._dimmed else 1.0)
        radius = self.height() / 2
        for i, (rect, value, text) in enumerate(self._layout()):
            on = value == self._current
            if on:
                p.setBrush(QColor(theme.blend(self._p.card, self._accent, 0.24)))
            elif i == self._hover:
                p.setBrush(QColor(theme.blend(self._p.card, self._p.text, 0.12)))
            else:
                p.setBrush(QColor(theme.blend(self._p.card, self._p.text, 0.07)))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(rect, radius, radius)
            draw_text(
                p,
                rect,
                text,
                size=12,
                color=self._accent if on else self._p.text_soft,
                weight=QFont.Medium if on else QFont.Normal,
                align=Qt.AlignCenter,
                elide=False,
            )
        p.end()

    # -- 交互 ------------------------------------------------------------
    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        hit = self._hit(event.position().toPoint())
        if hit != self._hover:
            self._hover = hit
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover != -1:
            self._hover = -1
            self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton or not self.isEnabled():
            super().mousePressEvent(event)
            return
        idx = self._hit(event.position().toPoint())
        if idx < 0:
            super().mousePressEvent(event)
            return
        _, value, _ = self._layout()[idx]
        if value != self._current:
            self._current = value
            self.update()
        self.committed.emit(value)
        event.accept()

    def _hit(self, point) -> int:
        for i, (rect, _, _) in enumerate(self._layout()):
            if rect.contains(point):
                return i
        return -1


# ---------------------------------------------------------------------------
# 分段控件：一条轨道 + 内嵌按钮
# ---------------------------------------------------------------------------


class CardSeg(CardControl):
    """一条轨道里切几段，单选（OpenMexia 的 ``.seg`` 那种观感）。

    和 ``CardChips`` 的差别不只是外观——胶囊是"各自独立的块"，轨道是"一个容器
    里切段"，三处实际差别：

    * **选中态是实心品类色 + 反色字**，不再是"在卡片底色上再混一层"。卡片开启
      时底色本身已经染过同一个品类色（``theme.tint``，10%），激活态若也只是
      混色（原来 24%），在 26px 的高度上几乎看不出来——2026-11-12 实测：风扇
      正在直吹风，模式那一排看着像一项都没选中。
    * **宽度均分**，不按文字量分配。5 段角度（30/60/90/120/140）在半张卡
      130px 里也放得下，胶囊排同样内容要 162px。
    * **永远铺满**（``CardChips(spread=True)`` 是"有余宽才铺"）。分段控件的语义
      是"这些是互斥的几档"，右边留一条空档会显得少了一段。

    字号是这里唯一的自由度（宽度已经均分完了）：按最宽那项自动在 12/11/10 里
    挑。一个都放不下就由 :func:`make_control` 退回步进器，**不在控件内部截字**。
    """

    committed = Signal(int)

    HEIGHT = CHIP_H
    #: 轨道内边距、段间距
    TRACK_PAD = 2
    GAP = 2
    TRACK_RADIUS = 9
    BTN_RADIUS = 7
    #: 依次尝试的字号
    SIZES = (12, 11, 10)
    #: 段内左右各留多少（文字与段边）
    TEXT_PAD = 3

    def __init__(
        self,
        prop: PropertySpec,
        palette: theme.Palette,
        accent: str,
        *,
        label: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(prop, palette, accent, label=label, parent=parent)
        self._items: list[tuple[int, str]] = [
            (o.value, o.display_short) for o in prop.value_list
        ]
        # 与胶囊同一套语义：初始不选中任何一项，等真实值到了再点亮
        self._current: int | None = None
        self._active_seen: int | None = None
        self._hover = -1
        self._px = self.SIZES[0]
        self.setFixedHeight(self.HEIGHT)
        self.setMouseTracking(True)
        self.setVisible(bool(self._items))

    # -- 值（接口与 CardChips 一致，body.sync 不用分支）------------------
    def value(self) -> int | None:
        return self._current

    def set_value(self, value: object, animate: bool = False) -> None:
        if value is None:
            return
        try:
            v = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        if v != self._current:
            self._current = v
            self.update()

    def set_active(self, value: object) -> None:
        """同步"当前激活判据"（型号适配后由 store 给出）。语义见 ``CardChips``。"""
        if value is None:
            return
        try:
            v = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        prev, self._active_seen = self._active_seen, v
        if v == prev:
            return
        if v == 0:
            if prev is not None:
                self._current = 0
                self.update()
            return
        if v != self._current:
            self._current = v
            self.update()

    def set_unknown(self) -> None:
        """值没读到：**一段都不点亮**。

        轨道没有"占位项"（那是下拉框才有的东西），所以"未知"只能表现为
        "没有一段是激活的"。别退回去点亮第一段——那看起来像"值就是第一项"，
        和下拉框停在 ``currentIndex() == -1`` 是同一个原则（见 ``ControlRow``）。
        """
        if self._current is not None:
            self._current = None
            self._active_seen = None
            self.update()

    # -- 栅格 ------------------------------------------------------------
    @staticmethod
    def _proto_font(px: int) -> QFont:
        """量尺寸用的字体。和 ``paintEvent`` 里必须同族同字号，否则量出来的
        宽度和画出来的对不上（离屏平台尤其容易差一档）。"""
        f = QFont()
        f.setPixelSize(px)
        f.setWeight(QFont.Medium)
        return f

    def _font(self, px: int | None = None) -> QFont:
        return self._proto_font(px or self._px)

    def _inner(self) -> int:
        n = len(self._items)
        return max(1, self.width() - self.TRACK_PAD * 2 - self.GAP * (n - 1))

    def _widths(self, px: int) -> list[int] | None:
        """按字号算每段的宽度；放不下返回 ``None``。

        **按文字分配，不搞等宽**：角度那组是 30/60/90/120/140，"140" 比 "30"
        宽一半，等宽的结果只能是把字号压到 9px 或者干脆放不下（半张卡里 5 段
        等宽 = 每段 20px，装不下三个数字）。所以先按"文字 + 内边距"给出最小
        宽度，剩下的余量**均分**给每一段——短的那几段因此略宽一点，看着仍像
        等分的轨道。
        """
        n = len(self._items)
        if n == 0:
            return None
        inner = self._inner()
        fm = QFontMetrics(self._font(px))
        need = [fm.horizontalAdvance(t) + self.TEXT_PAD * 2 for _, t in self._items]
        total = sum(need)
        if total > inner:
            return None
        extra = (inner - total) // n
        return [w + extra for w in need]

    def _fit_font(self) -> int:
        for px in self.SIZES:
            if self._widths(px) is not None:
                return px
        return self.SIZES[-1]

    def _seg_rects(self) -> list[tuple[QRect, int, str]]:
        n = len(self._items)
        if n == 0:
            return []
        widths = self._widths(self._px)
        if widths is None:
            # 最小字号都放不下（工厂本该拦住，这里只做最后兜底）：退回等宽
            w = self._inner() // n
            widths = [w] * n
        h = max(1, self.height() - self.TRACK_PAD * 2)
        out: list[tuple[QRect, int, str]] = []
        x = self.TRACK_PAD
        for i, ((value, text), w) in enumerate(zip(self._items, widths, strict=False)):
            # 最后一段吃掉整除余数，右边缘才不会差 1–2px
            ww = self.width() - self.TRACK_PAD - x if i == n - 1 else w
            out.append((QRect(x, self.TRACK_PAD, max(1, ww), h), value, text))
            x += w + self.GAP
        return out

    # -- 绘制 ------------------------------------------------------------
    def _ink_on_accent(self) -> str:
        """实心品类色上的字色：亮底配深字，暗底配白字。"""
        return "#101317" if theme.luma(self._accent) > 140 else "#FFFFFF"

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(0.55 if self._dimmed else 1.0)
        self._px = self._fit_font()
        # 轨道：整块容器，比卡片底色深一档（开启态卡片已经染了品类色，
        # 轨道要"沉"下去才看得出这是容器的底）
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.blend(self._p.card, self._p.text, 0.10)))
        p.drawRoundedRect(
            QRect(0, 0, self.width(), self.height()),
            self.TRACK_RADIUS,
            self.TRACK_RADIUS,
        )
        for i, (rect, value, text) in enumerate(self._seg_rects()):
            on = value == self._current
            if on:
                p.setBrush(QColor(self._accent))
            elif i == self._hover:
                p.setBrush(QColor(theme.blend(self._p.card, self._p.text, 0.10)))
            else:
                p.setBrush(Qt.NoBrush)
            if on or i == self._hover:
                p.drawRoundedRect(rect, self.BTN_RADIUS, self.BTN_RADIUS)
            draw_text(
                p,
                rect,
                text,
                size=self._px,
                color=self._ink_on_accent() if on else self._p.text_soft,
                weight=QFont.Medium if on else QFont.Normal,
                align=Qt.AlignCenter,
                elide=False,
            )
        p.end()

    def sizeHint(self) -> QSize:  # noqa: N802
        """按 12px 字号量一遍"文字 + 内边距 + 段间距"，再给余量均分留 8px。"""
        fm = QFontMetrics(self._font(self.SIZES[0]))
        need = sum(fm.horizontalAdvance(t) + self.TEXT_PAD * 2 for _, t in self._items)
        need += self.GAP * max(0, len(self._items) - 1) + self.TRACK_PAD * 2
        return QSize(need + 8, self.HEIGHT)

    # -- 交互 ------------------------------------------------------------
    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        hit = self._hit(event.position().toPoint())
        if hit != self._hover:
            self._hover = hit
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover != -1:
            self._hover = -1
            self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton or not self.isEnabled():
            super().mousePressEvent(event)
            return
        idx = self._hit(event.position().toPoint())
        if idx < 0:
            super().mousePressEvent(event)
            return
        _, value, _ = self._seg_rects()[idx]
        if value != self._current:
            self._current = value
            self.update()
        self.committed.emit(value)
        event.accept()

    def _hit(self, point) -> int:
        # 命中区用整条轨道的高度：段本身只有 22 高，上下各 2px 的内边距
        # 也应该是有效点击区，否则贴着轨道边缘点会落空。
        for i, (rect, _, _) in enumerate(self._seg_rects()):
            if QRect(rect.x(), 0, rect.width(), self.height()).contains(point):
                return i
        return -1


# ---------------------------------------------------------------------------
# 开关行：摇头、扫风、离子
# ---------------------------------------------------------------------------


class CardToggle(CardControl):
    """左侧属性名 + 右侧 44×24 开关。"""

    committed = Signal(bool)

    HEIGHT = ROW_H

    def __init__(
        self,
        prop: PropertySpec,
        palette: theme.Palette,
        accent: str,
        *,
        label: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(prop, palette, accent, label=label, parent=parent)
        self._value = False
        self.setFixedHeight(self.HEIGHT)
        self.switch = ToggleSwitch(
            False, palette=palette, parent=self, size="sm", accent=accent
        )
        self.switch.toggled.connect(self._on_toggle)

    # -- 值 --------------------------------------------------------------
    def value(self) -> bool:
        return self._value

    def set_value(self, value: object, animate: bool = False) -> None:
        if value is None:
            return
        on = bool(value)
        if on != self._value:
            self._value = on
            self.switch.blockSignals(True)
            self.switch.set_on(on, animate=False)
            self.switch.blockSignals(False)
            self.update()

    def apply_theme(self, palette: theme.Palette, accent: str) -> None:
        super().apply_theme(palette, accent)
        self.switch._p = palette  # noqa: SLF001 — 同包内的轻量主题同步
        self.switch._accent = accent  # noqa: SLF001
        self.switch.update()

    # -- 绘制 ------------------------------------------------------------
    def resizeEvent(self, event) -> None:  # noqa: N802
        self.switch.move(
            self._switch_x(),
            (self.height() - self.switch.height()) // 2,
        )

    def _switch_x(self) -> int:
        """开关摆哪儿。

        **有标签就靠右、没标签就靠左**。``label=""`` 意味着标签由外面的格子
        画（见 ``CardControl`` 的注释），那它就在控件的左边紧挨着；这时再把开关
        推到行尾，中间会空出一大截，看起来像开关贴到了隔壁那一格——风扇卡
        「左右扫风」和右列的「模式」就是这么看着别扭的（2026-11-12 用户指出）。
        """
        if self._label:
            return self.width() - self.switch.width()
        return 0

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(0.55 if self._dimmed else 1.0)
        # 有标签时文字占左边、给右边的开关留位；没标签时整行都是空的
        w = self.width() - (self.switch.width() + 8) if self._label else self.width()
        draw_text(
            p,
            QRect(0, 0, max(40, w), self.height()),
            self._label,
            size=12,
            color=self._ink() if self.isEnabled() else self._p.text_mute,
        )
        p.end()

    def _on_toggle(self, on: bool) -> None:
        self._value = on
        self.update()
        self.committed.emit(on)


# ---------------------------------------------------------------------------
# 只读读数：有人/无人、门开/关
# ---------------------------------------------------------------------------


class CardReadout(CardControl):
    """只读大字读数。占满整块内容带，居中排「大字值 + 小字属性名」。

    和滑条共用 ``set_value`` / ``setEnabled`` 接口，``CardBody.sync`` 不必为它
    开分支；区别只在于它**没有** ``committed``——它不写设备。

    不按值染色。``occupancy`` 这种属性 1=有人、0=无人，看着可以直接"非零就
    高亮"，但同一套映射套到门锁的 ``lock-state`` 上就反了。状态语义各品类
    不同，这里只负责把云端已经翻好的中文（``format_value``）放大，颜色交给
    外壳的品类色。
    """

    HEIGHT = 78

    def __init__(
        self,
        prop: PropertySpec,
        palette: theme.Palette,
        accent: str,
        *,
        label: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(prop, palette, accent, label=label, parent=parent)
        self._text = "—"
        self.setFixedHeight(self.HEIGHT)
        self.setCursor(Qt.ArrowCursor)  # 不可点，别给手型

    def set_value(self, value: object, animate: bool = False) -> None:
        if value is None:
            return
        text = format_value(self.prop, value)
        if text != self._text:
            self._text = text
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # 只读控件没有"不可写"这回事，唯一的灰态是设备离线
        p.setOpacity(1.0 if self.isEnabled() else 0.45)
        w = self.width()
        # 26px→30px：内容带 78 高、卡宽 138，两字状态用 30px 才压得住这块空白；
        # 四字（"有人移动"）也才 120px，仍在一行内。
        draw_text(
            p,
            QRect(0, 6, w, 40),
            self._text,
            size=30,
            color=self._p.text,
            weight=QFont.DemiBold,
            align=Qt.AlignCenter,
            elide=False,
        )
        draw_text(
            p,
            QRect(0, 48, w, 16),
            self._label,
            size=11,
            color=self._p.text_soft,
            align=Qt.AlignCenter,
            elide=False,
        )
        p.end()


__all__ = [
    "BODY_W",
    "CHIP_H",
    "MAX_SEGMENTS",
    "MIN_HIT_H",
    "ROW_H",
    "CardChips",
    "CardControl",
    "CardReadout",
    "CardSeg",
    "CardSlider",
    "CardStepper",
    "CardToggle",
    "fits_chips",
    "fits_segments",
    "kelvin_color",
    "make_control",
]


# ---------------------------------------------------------------------------
# 控件库：一处决定"这个槽位画成什么"
# ---------------------------------------------------------------------------


def fits_chips(prop: PropertySpec, avail: int) -> bool:
    """这组标签铺成分立胶囊，放得下吗（判据同样是"会不会出现省略号"）。

    复现 ``CardChips._layout`` 的两步挤压（先压内边距，再按预算截字）。只有
    显式声明 ``widget="chips"`` 时才会问到这里——默认的枚举单选已经走分段
    控件了（见 :func:`make_control`）。
    """
    items = [o.display_short for o in prop.value_list]
    n = len(items)
    if n <= 0:
        return False
    fm = QFontMetrics(CardSeg._proto_font(12))
    gap, pad = CardChips.GAP, CardChips.PAD
    widths = [fm.horizontalAdvance(t) + pad * 2 for t in items]
    total = sum(widths) + gap * (n - 1)
    if total > avail:
        pad = max(6, pad - (total - avail) // max(1, n * 2))
        widths = [fm.horizontalAdvance(t) + pad * 2 for t in items]
        total = sum(widths) + gap * (n - 1)
    return total <= avail


def fits_segments(prop: PropertySpec, avail: int) -> bool:
    """这组标签切成段之后，还能不能把字放全。

    判据和 ``CardChips`` 那套一样是"会不会出现省略号"，但算的是分段控件的
    几何（按文字给最小宽度 + 余量均分 + 最小字号）。``CardSeg`` 内部会自己
    往下降字号，所以这里用它的**最小**字号判：连 10px 都放不下就不该用分段
    控件，该退步进器。
    """
    items = [o.display_short for o in prop.value_list]
    n = len(items)
    if n <= 0:
        return False
    inner = avail - CardSeg.TRACK_PAD * 2 - CardSeg.GAP * (n - 1)
    if inner <= 0:
        return False
    fm = QFontMetrics(CardSeg._proto_font(CardSeg.SIZES[-1]))
    need = sum(fm.horizontalAdvance(t) + CardSeg.TEXT_PAD * 2 for t in items)
    return need <= inner


def make_control(
    ctl: Any,
    palette: theme.Palette,
    accent: str,
    *,
    avail: int = BODY_W,
    label: str | None = None,
    compact: bool = False,
    centered: bool = True,
    parent: QWidget | None = None,
) -> CardControl | None:
    """**卡片控件的唯一入口**：``Control.widget`` 声明 > 属性形状。

    ``ctl`` 是 ``BoundControl``（有 ``prop`` / ``widget`` / ``label``）；用鸭子
    类型是为了详情页预览这类只有 ``SimpleNamespace`` 的调用方也能用。

    为什么要有这一层：卡片 body 有两套布局（一行一个槽位的 ``CardBody``、
    两列四格的 ``FanBody``），各自实现一遍选型等于把同一张表抄两份——改一处、
    漂一处。现在**选型的知识只有这一份**，布局只管"摆哪儿"：

    * 声明了 ``widget`` 就按声明建（形状不符就退回按类型挑，不报错、不消失）；
    * 枚举默认是 :class:`CardSeg`（轨道分段，单选语义），段数超
      ``MAX_SEGMENTS`` 或放不下就退 :class:`CardStepper`；
    * ``range`` 默认仍是步进器（温度语义），要滑条得显式声明
      ``widget="slider"``——这一条不能反过来，1–100 的风速和 16–30 的温度都
      是 range，猜不出来。

    ``avail`` 是留给控件的宽度（调用方按布局扣掉标签/列宽）。``label=""``
    表示"这格自己画名字"，见 ``CardControl`` 的注释。
    """
    prop = getattr(ctl, "prop", None)
    if prop is None:
        return None
    want = getattr(ctl, "widget", "") or ""
    kind = prop.kind
    args = (prop, palette, accent)

    def slider() -> CardSlider:
        return CardSlider(*args, label=label, compact=compact, parent=parent)

    def stepper() -> CardStepper:
        return CardStepper(*args, label=label, centered=centered, parent=parent)

    def seg() -> CardSeg:
        return CardSeg(*args, label=label, parent=parent)

    def chips() -> CardChips:
        return CardChips(*args, label=label, spread=True, parent=parent)

    def toggle() -> CardToggle:
        return CardToggle(*args, label=label, parent=parent)

    items = prop.value_list
    seg_ok = bool(items) and len(items) <= MAX_SEGMENTS and fits_segments(prop, avail)

    # 1) 显式声明。放不下/形状不符时不在这里返回 None——继续走下面的按类型
    #    分支，等于"声明失效但控件还在"，比整格消失好。
    if want == "slider" and prop.value_range:
        return slider()
    if want == "stepper" and (prop.value_range or items):
        return stepper()
    if want == "chips" and items and fits_chips(prop, avail):
        return chips()
    if want == "seg" and items and seg_ok:
        return seg()
    if want == "toggle" and kind == "bool":
        return toggle()

    # 2) 按属性形状
    if kind == "bool":
        return toggle()
    if kind == "enum":
        return seg() if seg_ok else stepper()
    if prop.value_range:
        return stepper()
    return None
