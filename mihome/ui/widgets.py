"""通用控件与布局工具。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLayout, QSizePolicy, QWidget

from ..core.climate import SLOTS, Climate
from ..core.icons import IconCache
from . import theme, tooltip


class FlowLayout(QLayout):
    """自动换行布局：宽度变化时重排列数，卡片墙靠它自适应窗口大小。"""

    def __init__(self, parent: QWidget | None = None, margin: int = 0, h_gap: int = 12, v_gap: int = 12) -> None:
        super().__init__(parent)
        self._items: list = []
        self._h_gap = h_gap
        self._v_gap = v_gap
        self.setContentsMargins(margin, margin, margin, margin)

    # -- QLayout 接口 ----------------------------------------------------
    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._layout(rect, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    # -- 排布 ------------------------------------------------------------
    def _layout(self, rect: QRect, apply: bool) -> int:
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = area.x(), area.y(), 0

        for item in self._items:
            # 搜索过滤时卡片是隐藏的，布局必须跳过它们，否则会留出空洞
            w = item.widget()
            if w is not None and w.isHidden():
                continue
            hint = item.sizeHint()
            if x + hint.width() > area.right() + 1 and line_h > 0:
                x = area.x()
                y += line_h + self._v_gap
                line_h = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._h_gap
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()


class IconProvider(QWidget):
    """把一个 model 的产品图变成 QPixmap，带内存缓存与异步回填。

    继承 QWidget 只是为了拿到对象生命周期与信号机制，不参与布局。
    """

    ready = Signal(str)

    def __init__(self, cache: IconCache, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_DontShowOnScreen, True)
        self.setFixedSize(1, 1)
        self._cache = cache
        self._pix: dict[str, QPixmap | None] = {}
        self._scaled: dict[tuple[str, int], QPixmap] = {}
        self._pending: set[str] = set()

    def ensure(self, model: str) -> QPixmap | None:
        if not model:
            return None
        if model in self._pix:
            return self._pix[model]
        path = self._cache.path_for(model)
        if path is not None:
            pm = QPixmap(str(path))
            self._pix[model] = pm if not pm.isNull() else None
            return self._pix[model]
        if model not in self._pending:
            self._pending.add(model)
            self._cache.request(model, self._loaded)
        return None

    def _loaded(self, model: str, path: Path | None) -> None:
        # 回调发生在主线程（由 worker.drain 执行），可以直接改状态
        self._pending.discard(model)
        if path is None:
            self._pix[model] = None
            return
        pm = QPixmap(str(path))
        self._pix[model] = pm if not pm.isNull() else None
        self._scaled = {k: v for k, v in self._scaled.items() if k[0] != model}
        if self._pix[model] is not None:
            QTimer.singleShot(0, lambda m=model: self.ready.emit(m))

    def scaled(self, model: str, size: int) -> QPixmap | None:
        """等比缩放到 size 见方（不拉伸）。结果按 (model,size) 缓存，轮询重绘不重复缩放。"""
        key = (model, size)
        hit = self._scaled.get(key)
        if hit is not None:
            return hit
        pm = self.ensure(model)
        if pm is None:
            return None
        out = pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._scaled[key] = out
        return out

    def has(self, model: str) -> bool:
        return self._pix.get(model) is not None


# ---------------------------------------------------------------------------
# 绘制小工具
# ---------------------------------------------------------------------------


def rounded_path(rect: QRect, radius: int) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


def draw_text(
    painter: QPainter,
    rect: QRect,
    text: str,
    *,
    size: int,
    color: str,
    weight: QFont.Weight = QFont.Normal,
    align: Qt.AlignmentFlag = Qt.AlignLeft | Qt.AlignVCenter,
    elide: bool = True,
) -> None:
    if not text:
        return
    font = QFont(painter.font())
    font.setPixelSize(size)
    font.setWeight(weight)
    painter.setFont(font)
    painter.setPen(QColor(color))
    if elide:
        fm = QFontMetrics(font)
        text = fm.elidedText(text, Qt.ElideRight, rect.width())
    painter.drawText(rect, int(align), text)


def draw_text_block(
    painter: QPainter,
    rect: QRect,
    text: str,
    *,
    size: int,
    color: str,
    weight: QFont.Weight = QFont.Normal,
    max_lines: int = 2,
    line_gap: int = 3,
) -> None:
    """多行文本，超出行数时末行省略。

    设备名常常是英文默认名（"Xiaomi Smart Wall Switch (3 Gang)"），单行必然被
    截成 "Xiaomi Sm…" 毫无信息量。给两行能把大部分型号名放完。
    """
    if not text:
        return
    font = QFont(painter.font())
    font.setPixelSize(size)
    font.setWeight(weight)
    painter.setFont(font)
    painter.setPen(QColor(color))
    fm = QFontMetrics(font)

    line_h = fm.height() + line_gap
    lines: list[str] = []
    rest = text
    for i in range(max_lines):
        if not rest:
            break
        if i == max_lines - 1:
            lines.append(fm.elidedText(rest, Qt.ElideRight, rect.width()))
            break
        # 找能放下的最长前缀
        cut = len(rest)
        while cut > 1 and fm.horizontalAdvance(rest[:cut]) > rect.width():
            cut -= 1
        if cut < len(rest):
            # 英文不能在单词中间断（"Purif / ying" 这种最难读）。
            # 回退到最近的空格，但不能退掉半行以上，否则不如硬断。
            sp = rest.rfind(" ", 1, cut)
            if sp > 0 and cut - sp <= 12:
                cut = sp
        lines.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()

    y = rect.y()
    for line in lines:
        painter.drawText(QRect(rect.x(), y, rect.width(), fm.height()),
                         int(Qt.AlignLeft | Qt.AlignVCenter), line)
        y += line_h


class ElidedLabel(QLabel):
    """单行、宽度不够时打省略号的标签。

    ``QLabel`` 的 minimumSizeHint 是整段文字的宽度，属性名一长就会把整行撑宽，
    详情页甚至会被撑出横向滚动条。这里把宽度让出来，按当前宽度裁文字；完整内容
    放在 tooltip 里。
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        tooltip.watch(self, lambda: self._full)
        super().setText(text)

    def set_full(self, text: str) -> None:
        self._full = text
        self._apply()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply()

    def _apply(self) -> None:
        width = self.width() - 2
        if width <= 0:
            return
        text = self.fontMetrics().elidedText(self._full, Qt.ElideRight, width)
        if text != super().text():
            super().setText(text)


#: 胶囊左侧小图标的占位尺寸（宽 × 高）。高度贴着 12px 文字的视觉高度：再大就会
#: 把 30px 的胶囊撑满，看着像一颗按钮而不是一条读数。
CHIP_GLYPH_W = 10
CHIP_GLYPH_H = 13
#: 图标到文字的距离。和状态胶囊里"圆点 → 文字"的 7px 差不多，两排胶囊并排时
#: 文字起点不会差一截
CHIP_GLYPH_GAP = 6


def paint_chip_glyph(p: QPainter, box: QRectF, kind: str, color: QColor) -> None:
    """在 ``box`` 里画一枚胶囊小图标。

    ``kind`` 三选一：``"house"`` 房子（在线设备）/ ``"thermometer"`` 温度计 /
    ``"droplet"`` 水滴。

    为什么自己画、不用 emoji 或图标字体：胶囊是自绘的，混进来的字形带着自己的
    颜色和基线，深浅两个主题里对不齐，颜色也不会跟着 ``theme.chip_text`` 变。
    这里只用线条和路径，``color`` 由调用方给（跟随主题）。

    房子 = 尖顶 + 两面坡 + 两堵墙 + 平地的**描边**五边形；温度计 = 一根圆头竖管 +
    底端空心圆泡 + 泡心的实心水银；水滴 = 顶尖锐、底端圆的闭合路径、整块实心。
    三者都刻意留一点上下留白：贴到 13px 边上的曲线在 100% 缩放下会糊成一条竖线。
    房子只描边不填：10px 宽里填实了就是一块墨点，认不出是房子。
    """
    if kind == "house":
        # 边距 0.9 = 线宽 1.4 的一半，整条描边才落在框内不被裁；坡顶比墙顶高
        # 5.3px、墙高 6.1px，是这个尺寸下"看得出是房子"的最短比例
        left = box.left() + 0.9
        right = box.right() - 0.9
        base = box.bottom() - 0.9
        eave = box.top() + 6.2
        cx = box.center().x()
        roof = QPainterPath()
        # 坡顶留 1.0：0.7 时 1.4px 的线宽正好把尖顶顶到框外，100% 缩放下尖头被切平
        roof.moveTo(cx, box.top() + 1.0)
        roof.lineTo(left, eave)
        roof.lineTo(left, base)
        roof.lineTo(right, base)
        roof.lineTo(right, eave)
        roof.closeSubpath()
        p.setPen(QPen(color, 1.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        p.drawPath(roof)
        return
    if kind == "thermometer":
        cx = box.center().x()
        bulb_r = 2.7
        bulb_cy = box.bottom() - bulb_r - 0.2
        top = box.top() + 0.6
        # 管子里那截"水银"：一根圆头竖线，下端伸进泡里，接缝被泡的描边盖住。
        # 1.8 是下限：再细一档，13px 高、100% 缩放下会被抗锯齿摊成两列灰点
        p.setPen(QPen(color, 1.8, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(cx, top), QPointF(cx, bulb_cy))
        p.setPen(QPen(color, 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(cx, bulb_cy), bulb_r, bulb_r)
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawEllipse(QPointF(cx, bulb_cy), 1.15, 1.15)
        return
    if kind == "droplet":
        cx = box.center().x()
        r = 3.4
        cy = box.bottom() - r - 0.2
        top = box.top() + 0.6
        side = (cy - top) * 0.34
        drop = QPainterPath()
        drop.moveTo(cx, top)
        drop.cubicTo(cx + 1.3, top + side, cx + r * 0.98, cy - r * 0.78, cx + r, cy)
        # 0° → -180°：从右侧绕**下方**到左侧（屏幕坐标里顺时针，就是水滴的圆底）
        drop.arcTo(QRectF(cx - r, cy - r, r * 2, r * 2), 0, -180)
        drop.cubicTo(cx - r * 0.98, cy - r * 0.78, cx - 1.3, top + side, cx, top)
        drop.closeSubpath()
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawPath(drop)


class StatusChip(QWidget):
    """状态胶囊（米家首页那排"28.3-29.5°""灯亮 5"）。

    米家那支深蓝黑是给"压在照片壁纸上"用的；我们顶栏是纯色底，照搬会出问题：
    深色主题下深色胶囊和背景几乎同色，切主题看不出变化。所以这里把它做成一张
    **缩小的卡片**——底色取 ``palette.card``、文字取次级色，深浅两套各一档。

    左边可以放一枚小图标（``icon``，见 :func:`paint_chip_glyph`）：三颗胶囊各挂
    一枚——房子是设备、温度计是室温、水滴是湿度。``"48%"`` 单看只有百分号，
    分不出湿度还是占空比；``"9 台设备 · 6 台开启"`` 前面那个绿点亮着时又容易被
    读成"这一栏在线"，换成房子说的才是"这是设备"。
    """

    H = 30
    PAD = 14
    DOT = 6

    def __init__(
        self,
        text: str,
        palette: theme.Palette,
        parent: QWidget | None = None,
        *,
        icon: str = "",
    ) -> None:
        super().__init__(parent)
        self._p = palette
        self._text = text
        # 圆点和图标是**二选一**的：圆点表示"开 / 在线"，图标表示"这是什么读数"，
        # 一颗胶囊同时挂两个既挤又含混。给了图标就默认不带圆点
        self._dot = not icon
        self._icon = icon
        # 描边默认关：真顶栏上胶囊压在背景 / 色带上，本来就有色差；只有垫在
        # 同色底上的场合（设置页的样式展示）才需要一条轮廓（见 ``set_outline``）
        self._outline = False
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setFixedHeight(self.H)
        self._apply_width()

    def setText(self, text: str) -> None:  # noqa: N802
        if text == self._text:
            return
        self._text = text
        self._apply_width()
        self.update()

    def text(self) -> str:
        return self._text

    def set_dot(self, on: bool) -> None:
        if on != self._dot:
            self._dot = on
            self._apply_width()
            self.update()

    def set_outline(self, on: bool) -> None:
        """沿自己的圆角描一条 1px 的 ``border``。

        只给"胶囊和底同色"的场合用（设置页的样式展示）：同尺寸同色的垫底会被
        胶囊 100% 盖住，垫大一圈又会在四周漏出多余的一圈。描边由胶囊自己画，
        所以和它的圆角逐像素重合，不会多出一分。
        """
        if on != self._outline:
            self._outline = on
            self.update()

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.update()

    def _font(self) -> QFont:
        f = QFont(self.font())
        f.setPixelSize(12)
        return f

    def _apply_width(self) -> None:
        fm = QFontMetrics(self._font())
        w = fm.horizontalAdvance(self._text) + self.PAD * 2
        if self._dot:
            w += self.DOT + 7
        elif self._icon:
            w += CHIP_GLYPH_W + CHIP_GLYPH_GAP
        self.setFixedWidth(max(56, w))

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(Qt.NoPen)
        # 它是"一张缩小的卡片"，所以和卡片一样跟着 card_alpha 一起透（见 theme.card_fill）
        p.setBrush(theme.card_fill(theme.chip_bg(self._p), self._p))
        p.drawRoundedRect(r, theme.RADIUS_PILL, theme.RADIUS_PILL)
        if self._outline:
            p.setPen(QPen(QColor(self._p.border), 1))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r, theme.RADIUS_PILL, theme.RADIUS_PILL)

        x = self.PAD
        if self._dot:
            p.setBrush(QColor(theme.TOGGLE_ON))
            p.drawEllipse(QPointF(x + self.DOT / 2, self.H / 2), self.DOT / 2, self.DOT / 2)
            x += self.DOT + 7
        elif self._icon:
            paint_chip_glyph(
                p,
                QRectF(x, (self.H - CHIP_GLYPH_H) / 2, CHIP_GLYPH_W, CHIP_GLYPH_H),
                self._icon,
                QColor(theme.chip_text(self._p)),
            )
            x += CHIP_GLYPH_W + CHIP_GLYPH_GAP
        draw_text(p, QRect(int(x), 0, self.width() - int(x) - 4, self.H), self._text,
                  size=12, color=theme.chip_text(self._p))
        p.end()


class ClimateStrip(QWidget):
    """顶栏那两颗温湿度胶囊（全屋范围，见 ``core/climate.py``）。

    做成一个容器、而不是往顶栏里直接塞两个 ``StatusChip``：没有数据时要**整块
    消失**，连中间那 8px 间距一起——`addSpacing` 加出来的缝不会跟着胶囊藏。

    两颗都不带那个绿色圆点：圆点在这套界面里表示"开着 / 在线"，温湿度是读数不是
    状态，点亮会读成"温湿度传感器在线"。取而代之的是左边一枚小图标（温度计 /
    水滴）——``"48%"`` 单独看只有一个百分号，不悬停根本分不出是湿度还是别的百分比。
    悬停给逐台读数（见 ``Climate.tooltip``）。

    **"关掉"和"没数据"是两件事**：``set_shown`` 记用户的偏好（设置 → 外观 →
    顶栏展示），``set_climate`` 记事实。两者相与才决定这颗画不画，所以关掉再打开
    不会丢数，窄窗口临时收起（主窗口那边做的）也不会把偏好改成关。
    """

    #: 胶囊之间（以及和左边状态胶囊之间）的留白。顶栏算余量时要用同一个数。
    GAP = 8
    #: 槽位 → 左边那枚图标的画法名（见 :func:`paint_chip_glyph`）
    ICONS: dict[str, str] = {"temperature": "thermometer", "humidity": "droplet"}

    def __init__(self, palette: theme.Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._p = palette
        self._climate = Climate()
        self._show: dict[str, bool] = dict.fromkeys(SLOTS, True)
        self.temp = StatusChip("", palette, icon=self.ICONS["temperature"])
        self.hum = StatusChip("", palette, icon=self.ICONS["humidity"])
        for chip, slot in zip((self.temp, self.hum), SLOTS, strict=True):
            chip.set_dot(False)
            tooltip.watch(chip, lambda slot=slot: self._climate.tooltip(slot))
        lay = QHBoxLayout(self)
        # 左边那 8px 留白做在容器里：整块隐藏时它跟着消失；放顶栏上做 addSpacing 会留一条空缝
        lay.setContentsMargins(self.GAP, 0, 0, 0)
        lay.setSpacing(self.GAP)
        lay.addWidget(self.temp)
        lay.addWidget(self.hum)
        self.setVisible(False)

    def climate(self) -> Climate:
        return self._climate

    def set_climate(self, climate: Climate) -> None:
        """换一份样本：有值就显示、没值就藏。文字与可见性一起变。"""
        self._climate = climate
        self._sync()

    def set_shown(self, slot: str, on: bool) -> None:
        """用户关掉 / 打开这一颗（设置 → 外观 → 顶栏展示）。"""
        if slot not in SLOTS:
            return
        on = bool(on)
        if self._show[slot] == on:
            return
        self._show[slot] = on
        self._sync()

    def shown(self, slot: str) -> bool:
        return self._show.get(slot, True)

    def present(self) -> list[StatusChip]:
        """**此刻该画**的胶囊：有值 + 没被用户关掉，按 温度 → 湿度 顺序。

        主窗口算顶栏余量时按这个列表量（不是按 ``text()``），否则"设置里关掉的
        那颗"会被算进所需宽度，白收别的胶囊。
        """
        return [
            chip
            for slot, chip in zip(SLOTS, (self.temp, self.hum), strict=True)
            if chip.text() and self._show[slot]
        ]

    def _sync(self) -> None:
        temp, hum = self._climate.texts()
        show_temp = bool(temp) and self._show["temperature"]
        show_hum = bool(hum) and self._show["humidity"]
        self.temp.setText(temp)
        self.temp.setVisible(show_temp)
        self.hum.setText(hum)
        self.hum.setVisible(show_hum)
        self.setVisible(show_temp or show_hum)

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.temp.set_palette(palette)
        self.hum.set_palette(palette)


class HLine(QWidget):
    """1px 分隔线，用背景色画而不是 QFrame，避免 Qt 的默认长度策略。"""

    def __init__(self, color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = color
        self.setFixedHeight(1)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_color(self, color: str) -> None:
        self._color = color
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(self._color))
        p.end()


class ToggleSwitch(QWidget):
    """米家风格的开关：胶囊轨道 + 白色滑块。

    自绘而不是 ``QCheckBox`` + QSS：QSS 画不出"滑块从一边滑到另一边"的中间态，
    也没法单独给滑块加阴影。绿色只在"开"的时候出现（见 ``theme.TOGGLE_ON``），
    关的时候是一条中性灰轨道，这样一列开关扫一眼就知道谁开着。

    两种尺寸（米家实测）：``sm`` 44×24 用于设置行，``md`` 49×32 用于列表/卡片。
    """

    toggled = Signal(bool)

    #: size → (宽, 高)
    SIZES: dict[str, tuple[int, int]] = {"sm": (44, 24), "md": (49, 32)}
    W = 44
    H = 24

    def __init__(
        self,
        on: bool = False,
        palette: theme.Palette | None = None,
        parent: QWidget | None = None,
        size: str = "sm",
        accent: str = "",
    ) -> None:
        super().__init__(parent)
        self._p = palette or theme.LIGHT
        self._on = bool(on)
        self._t = 1.0 if on else 0.0
        self._hover = False
        #: 设备详情页用设备的品类色（灯是琥珀、插座是蓝），列表里用默认薄荷绿
        self._accent = accent or theme.TOGGLE_ON
        self._w, self._h = self.SIZES.get(size, self.SIZES["sm"])
        self.setFixedSize(self._w, self._h)
        self.setCursor(Qt.PointingHandCursor)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._tick)

    # -- 状态 ------------------------------------------------------------
    def is_on(self) -> bool:
        return self._on

    def set_on(self, on: bool, animate: bool = True) -> None:
        on = bool(on)
        if on == self._on:
            return
        self._on = on
        target = 1.0 if on else 0.0
        self._anim.stop()
        if animate and self.isVisible():
            self._anim.setStartValue(self._t)
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self._t = target
            self.update()

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.update()

    def _tick(self, value: object) -> None:
        self._t = float(value)
        self.update()

    # -- 绘制 ------------------------------------------------------------
    def _track_off(self) -> str:
        return theme.blend(self._p.bg, self._p.text, 0.16)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0, 0, -1, -1)
        radius = r.height() / 2
        t = self._t
        enabled = self.isEnabled()

        track = QColor(theme.blend(self._track_off(), self._accent, t))
        if self._hover and enabled:
            track = QColor(theme.blend(track.name(), self._p.overlay, 0.10))
        if not enabled:
            track.setAlphaF(track.alphaF() * 0.55)
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(r, radius, radius)

        margin = 3.0 if r.height() < 30 else 4.0
        d = r.height() - margin * 2
        x = r.left() + margin + (r.width() - d - margin * 2) * t
        knob = QRectF(x, r.top() + margin, d, d)
        # 一层极淡的投影，让滑块从轨道上"浮"起来一点点
        p.setBrush(QColor(0, 0, 0, 34))
        p.drawEllipse(knob.translated(0, 0.8))
        p.setBrush(QColor("#FFFFFF" if enabled else "#E6E8EB"))
        p.drawEllipse(knob)
        p.end()

    # -- 交互 ------------------------------------------------------------
    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton or not self.rect().contains(
            event.position().toPoint()
        ):
            return
        self.set_on(not self._on)
        self.toggled.emit(self._on)


__all__ = [
    "ElidedLabel",
    "FlowLayout",
    "HLine",
    "IconProvider",
    "StatusChip",
    "ToggleSwitch",
    "draw_text",
    "draw_text_block",
    "rounded_path",
    "theme",
]
