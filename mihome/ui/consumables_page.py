# SPDX-License-Identifier: GPL-3.0-or-later
"""耗材页：把"这台设备的这个属性还剩多少"摆成一面卡片墙。

这一页的用户模型和卡片墙不同：卡片墙上的一格代表**一台设备**，这里的每一格代表
**一条监控项（设备 + 属性）**。所以同一台净化器可以有"滤芯"和"滤网"两张卡，
也可以只有一张——由用户在添加时自己决定（见 ``ui/consumable_dialog.py``）。

三条刻意的选择：

1. **不自动扫描全屋**。谁算耗材由用户点「＋ 添加设备」自己挑（``TODO.md`` 的
   原始规划，2026-11-18 用户重申）。自动猜"哪些属性是耗材"一定猜错，而且猜错的
   卡片会挤掉用户真正关心的那几条。
2. **值来自 ``Store``，本页不自己读云端**。耗材属性已经被 ``Store.poll_refs``
   并进同一套 3 秒 / 15 秒轮询（见那里的注释），这页只是消费者；再开一份定时器
   就会出现"卡片和详情页数字不一致"。
3. **排序只改显示，不改 ``prefs.consumables`` 的顺序**。只有自定义模式才开放拖动
   （2026-11-18 用户改的口径）：其余模式卡片的顺序是算出来的，拖动没有意义，
   所以连拖动手势都不启动，更不会偷偷把用户的排序方式改掉。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QPainter, QPainterPath, QPen
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

from ..core import consumables as model
from ..core.consumables import Consumable
from ..core.freshness import read_note
from ..core.spec import prop_labels
from ..core.store import Store
from . import theme
from .combo import Combo
from .dnd import DragGesture, DragGuard, grid_drop_index
from .wall import DragGhost, rounded_shot
from .widgets import draw_text

#: 卡片尺寸。比设备墙的一格（166）宽一圈：这里要同时放名字、设备、百分比和一条
#: 血条，压到 166 只能显示一半。高度按"三行 + 一条血条"定。
CARD_W = 268
CARD_H = 100
CARD_GAP = 12
PAD = 16
PAGE_MARGIN = 28

#: 排序方式的界面文案 ↔ ``prefs.CONSUMABLE_SORTS``
SORT_LABELS = (
    ("level", "按余量排序"),
    ("room", "按房间分组"),
    ("custom", "自定义排序"),
)

#: 卡片画法的界面文案 ↔ ``consumables.STYLES``。整页一个（见右上角下拉），
#: 不跟单条监控项走。
STYLE_LABELS = (
    (model.STYLE_BAR, "横条"),
    (model.STYLE_FILL, "整卡染色"),
    (model.STYLE_RING, "圆环"),
)

#: 血条高度与它到卡片底的距离。比纯血条时多留了 6px：右下角要给离线设备的
#: 读数时间腾一行（见 ``ConsumableCard.paintEvent``）。
BAR_H = 6
BAR_BOTTOM = 20

#: 圆环样式是**方形卡**（用户 2026-11-18 给的结构图）：上面两行写"这是谁 / 在哪"，
#: 中间一个占主体的大环，环心是百分比 + 属性名，右下角仍是读数时间。整页一个样式
#: （见下拉），所以一页里不会出现方形卡和横卡混排。
#: 2026-11-18 用户嫌 240 太大，缩到 3/4 = 180。环 / 线宽 / 圆心字同比缩，
#: 但标题字号不做 0.75 倍（那会小到读不清），只收 1~2px。
RING_CARD = 180
#: 环的外径 / 线宽 / 12 点方向的开口半角（度）。开口是结构图里明确画出来的，
#: 满格也留着——它不是"没画完"，是这类环的视觉惯例，别在 ratio==1 时补成整圆。
RING_D = 108
RING_W = 12
RING_GAP = 7
RING_X = (RING_CARD - RING_D) // 2
RING_Y = 52
RING_CENTER_SIZE = 18
#: 环心那两行：百分比上抬 ``RING_CENTER_LIFT``，属性名再往下 ``RING_LABEL_DY``。
#: **为什么要上抬**：名字要占一行，两行整体压在环心上会让下半圆显得空；上抬后
#: 数字那行的窗口（83–107）与名字那行（109–123）在直径 108 的环里上下留白均匀。
#: 名字以前写在第二行副标题里，跨家庭后前面还要挤「家庭 · 房间」（180 方卡只剩
#: 148px，实测 11px 要 154px 会被截），搬进环里之后副标题只写位置，字号能回到 11。
RING_CENTER_LIFT = 11
RING_LABEL_DY = 11
RING_LABEL_H = 14
RING_LABEL_SIZE = 10


def _ring_chord(dy: float) -> float:
    """环**内缘**在离圆心 ``dy`` 像素处能放文字的宽度（弦长）。

    名字写在环里，越往下圆越窄：按环心那条线算宽度，长名字（「电池电量 · 干电池」
    10px = 78px）会压到环线上。调用方按标签**下沿**那一层取宽度。
    """
    inner = RING_D / 2 - RING_W / 2
    if dy <= 0:
        return inner * 2
    if dy >= inner:
        return 0.0
    return 2 * math.sqrt(inner * inner - dy * dy)


def card_size(style: str) -> tuple[int, int]:
    """卡片尺寸跟着**整页样式**走：圆环是方形大卡，横条 / 整卡染色是横卡。

    换样式必须重建（见 ``ConsumablesPage._on_style``），否则只改绘制不改尺寸，
    方形环会被塞在 100px 高的横卡里挤成一团。
    """
    return (RING_CARD, RING_CARD) if style == model.STYLE_RING else (CARD_W, CARD_H)


def _qss(p: theme.Palette) -> str:
    return f"""
    QWidget#ConsumablePage {{ background: transparent; }}
    QLabel#PageTitle {{ color: {p.text}; font-size: 20px; font-weight: 700; }}
    QLabel#PageHint {{ color: {p.text_mute}; font-size: 12px; }}
    QLabel#RoomHeader {{ color: {p.text_soft}; font-size: 13px; font-weight: 600; }}
    QLabel#EmptyHint {{
        color: {p.text_mute}; font-size: 13px;
        background: transparent;
    }}
    QScrollArea#ConsumableScroll {{ background: transparent; border: none; }}
    QPushButton#Primary {{
        background: {theme.ACCENT_GREEN};
        color: #FFFFFF; border: none; border-radius: 9px;
        padding: 0 16px; min-height: 32px; font-size: 13px; font-weight: 600;
    }}
    QPushButton#Primary:hover {{ background: {theme.blend(theme.ACCENT_GREEN, "#FFFFFF", 0.12)}; }}
    QPushButton#Primary:pressed {{ background: {theme.blend(theme.ACCENT_GREEN, p.bg, 0.18)}; }}
    """


@dataclass(slots=True)
class Row:
    """一条监控项在界面上的样子。构造一次给卡片用，卡片自己不查 store。"""

    item: Consumable
    name: str
    device: str
    room: str
    percent: float | None
    level: int
    #: 读数本身（``"78%"`` / ``"—"``）。拿不到数值时只显示这个，不参与变色。
    value_text: str
    #: 卡片画法。**整页一个**（``prefs.consumable_style``），放在行里只是让卡片
    #: 绘制时不用回头查 store；换样式时页面把每行的这个字段统一改掉再重画。
    style: str = model.STYLE_DEFAULT
    #: 离线设备读数的更新时间（「3 天前更新」）。在线设备每次轮询都会刷新读数，
    #: 标出来是废话，所以这里是**空串**（用户 2026-11-18 定）。
    updated: str = ""
    #: 上面那个读数超过一天没更新 → 右下角换警示色，提醒"这个数可能已经不准了"。
    stale: bool = False
    #: 家庭显示名 / 家庭 id / 房间 id。**分组与排序认 id，显示认名字**：两个家
    #: 都可能有"客厅"，按名字分组会把它们并成一组（2026-11-21 跨家庭改造）。
    home: str = ""
    home_id: str = ""
    room_id: str = ""

    @property
    def place_text(self) -> str:
        """「家庭 · 房间」。跨家庭后这是卡片副标题、分组头、tooltip 的统一前缀。

        - 有家有房 → ``示例家庭 · 客厅``
        - 有家没房（设备没分配房间）→ ``示例家庭 · 未分房间``，别只写个家庭名让人
          以为这间房就叫"示例家庭"
        - 都没有（设备已不存在）→ 空串，调用方各自写「设备已不存在」

        分隔符用 `` · `` 而不是 ``-``：房间名本身可能带连字符，而项目里「·」已经
        是"并列项"的固定写法（挂件页脚「8 台设备 · 2 台离线」）。
        """
        if self.home:
            return f"{self.home} · {self.room or '未分房间'}"
        return self.room

    @property
    def place_key(self) -> tuple[str, str]:
        """分组用的键：``(家庭 id, 房间 id)``。房间改名不影响分组。"""
        return (self.home_id, self.room_id)

    @property
    def color(self) -> str:
        return theme.CONSUMABLE_LEVEL_COLORS[model.level_color_key(self.level)]

    @property
    def subtitle(self) -> str:
        """卡片第二行：「家庭 · 房间 | 属性名」。

        位置为空（设备已不存在 / 设备没分房间）就只写属性名，别留一根孤零零的竖线。
        """
        place = self.place_text
        return f"{place} | {self.name}" if place else self.name

    @property
    def is_shell(self) -> bool:
        """还没选属性的空卡（设备选好了，属性待定）。"""
        return not self.item.is_configured

    @property
    def sort_key(self) -> tuple:
        # 没有读数的排最后：它们不该占着"最该换"的位置；空壳再往后一层，
        # 因为连"该不该换"都还谈不上
        if self.is_shell:
            return (2, 999.0)
        if self.percent is None:
            return (1, 999.0)
        return (0, self.percent)


class ConsumableCard(QWidget):
    """一张耗材卡。整块自绘（和 ``DeviceCard`` 同一套理由：内容带只有几行字）。"""

    clicked = Signal(str)          # 监控项 id
    drag_started = Signal(str)
    drag_moved = Signal(QPoint)     # 全局坐标
    drag_dropped = Signal(str, QPoint)

    def __init__(
        self,
        row: Row,
        palette: theme.Palette,
        *,
        draggable: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._row = row
        self._p = palette
        self._hover = False
        self._press: QPoint | None = None
        self._dragging = False
        #: 只有「自定义排序」才允许搬卡（2026-11-18 用户定）。关掉时按下 /
        #: 移动仍按普通点击处理，不进入拖动手势、不发 drag_* 信号。
        #: **默认关**：安全侧。唯一该打开的地方是页面 ``_reflow()``，它按当前
        #: 排序方式显式传 ``draggable=mode == "custom"``——将来新增调用点忘了传，
        #: 结果是"拖不动"（能解释），而不是"按余量排序也能把顺序拖乱"（很难查）。
        self._draggable = bool(draggable)
        #: 和主页卡片墙共用同一套拖动状态机（8px 阈值），别再自己写一份
        self._gesture = DragGesture()
        self._placeholder = False
        self.setFixedSize(*card_size(row.style))
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)
        self._sync_tooltip()

    def _sync_tooltip(self) -> None:
        """卡片写不下的部分（长设备名 / 长属性名）在悬停时给全。

        空卡还没有属性名，副标题是一句引导语，别再重复一遍设备名。
        """
        row = self._row
        if row.is_shell:
            parts = [row.device] + ([row.place_text] if row.place_text else [])
            parts.append("点击设置要监控的属性")
            self.setToolTip("｜".join(parts))
            return
        # 卡片上写不下的部分在悬停时给全：设备 / 位置 / 属性三段用同一个分隔符
        # （副标题那条里 ``|`` 只隔位置和属性名，混进 tooltip 会有两种竖线）
        tip = "｜".join(p for p in (row.device, row.place_text, row.name) if p)
        # 卡片右下角只写「3 天前更新」，悬停补一句说清是哪个数
        if row.updated:
            tip += f"｜读数：{row.updated}"
        self.setToolTip(tip)

    @property
    def row(self) -> Row:
        return self._row

    def set_row(self, row: Row) -> None:
        if row != self._row:
            old_size = self.size()
            self._row = row
            want = card_size(row.style)
            if (old_size.width(), old_size.height()) != want:
                self.setFixedSize(*want)
            self._sync_tooltip()
            self.update()

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.update()

    def set_draggable(self, on: bool) -> None:
        """切换排序方式时跟着更新：只有自定义模式能拖。

        顺手清掉可能残留的按下状态——万一在拖动途中排序被外部改了，别让下一次
        移动凭旧手势又启动一次拖动。这里**不**发 ``drag_dropped``：排序变了不该
        把半途的位置写回盘上。
        """
        on = bool(on)
        if self._draggable == on:
            return
        self._draggable = on
        self.reset_drag_state()

    def reset_drag_state(self) -> None:
        """清掉按下/拖动中的手势状态，**不重排、不发信号**。

        拖动被外部打断时（切程序、漏了松手事件）调用：卡片的 ``_press`` 还留着的话，
        下一次移动会按旧的按下点判阈值，一挪就"凭空"又进入拖动。
        """
        self._press = None
        self._dragging = False
        self._gesture.reset()
        self.setCursor(Qt.PointingHandCursor)

    # -- 绘制 ------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        row = self._row
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self._placeholder:
            # 和主页卡片墙同款占位框：拖动时这张卡继续占着格子，网格不塌，
            # 虚线框本身也是"它会落在这里"的提示。
            p.setPen(QPen(QColor(self._p.accent), 2, Qt.DashLine))
            p.setBrush(QColor(theme.blend(self._p.card, self._p.accent, 0.08)))
            p.drawRoundedRect(rect, theme.RADIUS_CARD, theme.RADIUS_CARD)
            p.end()
            return
        base = self._p.card
        if self._hover:
            base = theme.card_hover_color(base, self._p)

        # 圆角路径先算好：整卡染色样式要拿它裁切，别裁到圆角外面去
        path = QPainterPath()
        path.addRoundedRect(rect, theme.RADIUS_CARD, theme.RADIUS_CARD)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.card_fill(base, self._p))
        p.drawPath(path)

        ratio = 0.0 if row.percent is None else row.percent / 100.0
        if row.is_shell:
            self._paint_shell(p, rect, row)
            p.end()
            return
        if row.style == model.STYLE_FILL:
            # 整卡当轨道：从左往右按比例染，颜色跟着档位走。底色压得很淡
            # （0.16），卡上的字仍然是正文色——染得太重会读不清，而这页的字
            # 就是读数本身。
            p.save()
            p.setClipPath(path)
            if ratio > 0:
                p.setBrush(QColor(theme.blend(base, row.color, 0.16)))
                p.drawRect(QRectF(rect.left(), rect.top(), rect.width() * ratio, rect.height()))
            p.restore()

        if row.style == model.STYLE_RING:
            # 方形卡按结构图走：设备名 / 位置置顶两行，下面整块留给环，属性名在环里
            # （见 ``_paint_ring``）。**不画右上角原始读数**——那两个值是横卡时代的
            # 布局，这里圆心百分比就是这页要读的那个数；带单位的值仍在 tooltip 里。
            draw_text(p, QRect(PAD, 12, self.width() - PAD * 2, 20), row.device,
                      size=13, color=self._p.text, weight=QFont.DemiBold)
            # 第二行只写位置：属性名搬进环里后这里不用再挤「位置 | 属性」，字号从
            # 10 回到 11（实测「示例家庭 · 客厅」11px = 75px / 方卡可用 148px）。
            draw_text(p, QRect(PAD, 32, self.width() - PAD * 2, 16), row.place_text,
                      size=11, color=self._p.text_soft)
        else:
            # 第一行：**设备名** + 右侧读数（读数用档位色，扫一眼就知道哪张红了）。
            # 主副标题的分工是 2026-11-18 用户定的：同一台设备的两块电池并排时，
            # 一眼先看到"这是哪台设备"，再看副标题是哪一间、哪个属性。
            draw_text(p, QRect(PAD, 12, self.width() - PAD * 2 - 56, 20), row.device,
                      size=14, color=self._p.text, weight=QFont.DemiBold)
            draw_text(p, QRect(self.width() - PAD - 60, 12, 60, 20), row.value_text,
                      size=14, color=row.color, weight=QFont.DemiBold,
                      align=Qt.AlignRight | Qt.AlignVCenter)
            # 第二行：家庭 · 房间 | 属性名（位置为空的条目就只有属性名）
            draw_text(p, QRect(PAD, 34, self.width() - PAD * 2, 18), row.subtitle,
                      size=12, color=self._p.text_soft)

        if row.style == model.STYLE_BAR:
            self._paint_bar(p, ratio, row)
        elif row.style == model.STYLE_RING:
            self._paint_ring(p, ratio, row)

        if row.percent is None and row.style != model.STYLE_RING:
            # 横卡读不到值：给一句话，别让空白像"坏了"。圆环卡不需要——圆心那个
            # 大破折号本身就是"没读到"，再塞一句话反而挤掉环。
            draw_text(p, QRect(PAD, self.height() - BAR_BOTTOM - 24, 160, 16),
                      "暂时读不到这个属性", size=11, color=self._p.text_mute)

        # 右下角：**这条读数是哪天的**。只有离线设备才写——在线设备每次轮询都会
        # 刷新读数，标一句"今天更新"等于废话（用户 2026-11-18 定）。
        # 超过一天没更新就用警示色：这个数已经不能当真了。
        if row.updated:
            # 方卡比原来矮：环底在 RING_Y+RING_D，时间再往下挪一点，别压到环上。
            time_y = self.height() - (16 if row.style == model.STYLE_RING else 19)
            draw_text(p, QRect(self.width() - PAD - 100, time_y, 100, 15),
                      row.updated, size=11,
                      color=theme.WARNING if row.stale else self._p.text_mute,
                      weight=QFont.DemiBold if row.stale else QFont.Normal,
                      align=Qt.AlignRight | Qt.AlignVCenter)
        p.end()

    def _paint_shell(self, p: QPainter, rect: QRectF, row: Row) -> None:
        """空卡（设备选好了、属性还没挑）。

        用**虚线框**把它和配好的卡分开：它不是"暂时读不到数据"，是"还没开始配"，
        画成实心卡会让人以为坏了。右下角一个小加号暗示这里还能填东西。
        """
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(theme.blend(self._p.card, self._p.text, 0.24)), 1, Qt.DashLine))
        p.drawRoundedRect(rect.adjusted(1, 1, -1, -1), theme.RADIUS_CARD, theme.RADIUS_CARD)

        inner = self.width() - PAD * 2
        draw_text(p, QRect(PAD, 12, inner - 56, 20), row.device,
                  size=14, color=self._p.text, weight=QFont.DemiBold)
        draw_text(p, QRect(self.width() - PAD - 60, 12, 60, 20), row.value_text,
                  size=12, color=self._p.text_mute,
                  align=Qt.AlignRight | Qt.AlignVCenter)
        # 还没有属性名可写，第二行沿用引导语；位置照旧放在前面
        place = row.place_text
        hint = f"{place} | 点击设置属性" if place else "点击设置属性"
        draw_text(p, QRect(PAD, 34, inner, 18), hint,
                  size=12, color=self._p.text_soft)

        # 加号按卡片底边定位，不跟血条那个 ``BAR_BOTTOM`` 走：空卡本来就没有血条，
        # 跟着血条上移会浮在半空
        cx = self.width() - PAD - 9
        cy = self.height() - 20
        p.setPen(QPen(QColor(self._p.text_mute), 1.4, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(cx - 5, cy, cx + 5, cy)
        p.drawLine(cx, cy - 5, cx, cy + 5)

    def _paint_bar(self, p: QPainter, ratio: float, row: Row) -> None:
        """底部横条（"血条"）。**轨道永远画满**，只有填充随比例缩短。"""
        track = QRectF(PAD, self.height() - BAR_BOTTOM - BAR_H,
                       self.width() - PAD * 2, BAR_H)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.blend(self._p.card, self._p.text, 0.12)))
        p.drawRoundedRect(track, BAR_H / 2, BAR_H / 2)
        if ratio <= 0:
            return
        fill = QRectF(track.left(), track.top(), max(BAR_H, track.width() * ratio), BAR_H)
        p.setBrush(QColor(row.color))
        p.drawRoundedRect(fill, BAR_H / 2, BAR_H / 2)

    def _paint_ring(self, p: QPainter, ratio: float, row: Row) -> None:
        """圆环进度：方形卡居中大环、12 点留开口、环心两行（百分比 + 属性名）。

        结构由用户 2026-11-18 给的图定：环占卡片主体，粗线宽，顶部一道缺口——
        满格也**不补成整圆**，那道口是这类环的视觉惯例，不是没画完。
        百分比按 ``fill_percent`` 折算成 0–100；带单位的原始读数不画在卡上
        （结构图里没有），仍在悬停提示里。

        轨道永远画满，前景弧按 ratio 走；读不到值时只画空轨道 + 环心破折号，
        不假装成 0%。开口同时绕开了 Qt 把 ``drawArc`` 的 360° 当空弧的坑。

        属性名 2026-11-21 从第二行副标题搬进环里、写在数字**下方**：跨家庭后副标题
        前面还要挤「家庭 · 房间」，180 方卡只剩 148px，同一句 11px 要 154px 必截；
        搬进来之后副标题只写位置，字号回到 11，名字的可用宽度反而从 148 变成
        环内弦长（数字那行 96、名字那行 ~90），长名字还有余量。
        """
        outer = QRectF(RING_X, RING_Y, RING_D, RING_D)
        track = outer.adjusted(RING_W / 2, RING_W / 2, -RING_W / 2, -RING_W / 2)
        # Qt 的 drawArc 角度**逆时针**增长、0° 在 3 点，所以 12 点是 +90°
        # （写成 -90 会把缺口画到正下方）。负 span = 顺时针走，符合进度环习惯：
        # 从缺口右侧起顺时针绕一圈，回到缺口左侧。
        start = 90 - RING_GAP
        span = -(360 - 2 * RING_GAP)
        p.setBrush(Qt.NoBrush)
        # 端点用平切（FlatCap）：结构图里 12 点那道口是平直切开的。RoundCap 的
        # 圆头两端各外扩半个线宽，会把 14° 的缺口整个补上，看起来就没口了。
        p.setPen(QPen(QColor(theme.blend(self._p.card, self._p.text, 0.12)),
                      RING_W, Qt.SolidLine, Qt.FlatCap))
        p.drawArc(track, start * 16, span * 16)
        if row.percent is not None and ratio > 0:
            p.setPen(QPen(QColor(row.color), RING_W, Qt.SolidLine, Qt.FlatCap))
            p.drawArc(track, start * 16, int(span * 16 * ratio))

        # 环心两行：百分比在上、属性名在下，整体相对环心上抬（见 RING_CENTER_LIFT）。
        cx = outer.center().x()
        cy = outer.center().y()
        text = "—" if row.percent is None else f"{row.percent:.0f}%"
        draw_text(p, QRect(int(cx) - 60, int(cy - RING_CENTER_LIFT) - 12, 120, 24), text,
                  size=RING_CENTER_SIZE,
                  color=(self._p.text_mute if row.percent is None else row.color),
                  weight=QFont.DemiBold, align=Qt.AlignCenter, elide=False)
        if row.name:
            # 宽度取标签**下沿**那一层的弦长再留 4px：环越往下越窄，按中线算会把
            # 长名字（「电池电量 · 干电池」10px = 78px）压到环线上。
            dy = RING_LABEL_DY + RING_LABEL_H / 2
            half = int(_ring_chord(dy)) - 2
            name_top = int(cy + RING_LABEL_DY - RING_LABEL_H / 2)
            draw_text(p, QRect(int(cx) - half, name_top, half * 2, RING_LABEL_H),
                      row.name, size=RING_LABEL_SIZE, color=self._p.text_soft,
                      align=Qt.AlignCenter)

    def set_drag_placeholder(self, on: bool) -> None:
        """拖动中把自己画成虚线占位框（和主页 ``DeviceCard`` 同一语义）。"""
        on = bool(on)
        if self._placeholder != on:
            self._placeholder = on
            self.update()

    # -- 交互 ------------------------------------------------------------
    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._press = event.globalPosition().toPoint()
            self._dragging = False
            self._gesture.press(self._press)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._press is None or not self._draggable:
            # 按余量 / 按房间时卡片顺序是算出来的，拖动没有落点可言：
            # 直接当普通鼠标移动，连阈值判定都不做。
            return
        pos = event.globalPosition().toPoint()
        if not self._dragging:
            if not self._gesture.move(pos):
                return
            self._dragging = True
            self.setCursor(Qt.ClosedHandCursor)
            self.drag_started.emit(self._row.item.id)
        self.drag_moved.emit(pos)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton or self._press is None:
            super().mouseReleaseEvent(event)
            return
        pos = event.globalPosition().toPoint()
        dragging = self._dragging
        self._press = None
        self._dragging = False
        self._gesture.reset()
        self.setCursor(Qt.PointingHandCursor)
        if dragging:
            self.drag_dropped.emit(self._row.item.id, pos)
        elif self.rect().contains(self.mapFromGlobal(pos)):
            self.clicked.emit(self._row.item.id)
        event.accept()


class ConsumablesPage(QWidget):
    """耗材页。

    ``run_dialog`` 由主窗口传入（那里统一压暗背景、套模态栈），本页不自己
    ``exec``——和自动化页同一条约定。
    """

    #: 两档提醒的条目数变化（轻提醒, 重提醒）。主窗口拿它更新侧栏圆点。
    counts_changed = Signal(int, int)

    def __init__(
        self,
        store: Store,
        palette: theme.Palette,
        *,
        run_dialog=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("ConsumablePage")
        self.store = store
        self._p = palette
        self._run_dialog = run_dialog
        self._cards: dict[str, ConsumableCard] = {}
        self._rows: list[Row] = []
        self._cols = 0
        self._last_counts: tuple[int, int] | None = None
        self._drag_id = ""
        #: 拖动排序（和主页卡片墙同款）：幽灵 + 拖动中实时重排 + 松手落盘。
        self._drag_ghost: DragGhost | None = None
        self._drag_grab = QPoint()
        self._drag_moved = False
        #: 拖动开始时的行顺序，中断时整段还原（拖动中只改内存，盘上还没动）
        self._drag_backup: list[Row] | None = None
        # 拖动途中的外部中断看门狗，见 dnd.DragGuard
        self._drag_guard = DragGuard(self)
        self._drag_guard.aborted.connect(self._on_drag_aborted)
        #: 切家庭时跨重建保留的滚动位置。None = 不保位（普通刷新照旧回顶部）。
        self._pending_scroll: int | None = None

        col = QVBoxLayout(self)
        col.setContentsMargins(PAGE_MARGIN, 8, PAGE_MARGIN, 18)
        col.setSpacing(12)

        # 顶栏：标题 + 一句统计 + 右侧「卡片样式 / 排序」两个下拉
        head = QHBoxLayout()
        head.setSpacing(10)
        # 标题 + 统计先包一层再按顶边放进 head。
        # 原因：右侧两个下拉（padding 6px + 13px 字 → 33 高）比 20px 标题的
        # 行高（26）高 7px，直接把 QLabel 放进 head 会被拉伸到 33 再垂直居中，
        # 标题文字比「监控」「电脑自动化」两页低约 3.5px（用户 2026-11-18 报
        # "标题位置有一点偏差"）。包一层后容器高度取自身 sizeHint（26），
        # 用 AlignTop 贴住 8px 顶边，三页标题就在同一条线上；统计文字仍在
        # 同一个容器里，和标题共中线。
        title_row = QWidget(self)
        trow = QHBoxLayout(title_row)
        trow.setContentsMargins(0, 0, 0, 0)
        trow.setSpacing(10)
        title = QLabel("耗材", title_row)
        title.setObjectName("PageTitle")
        trow.addWidget(title)
        self.lbl_hint = QLabel("", title_row)
        self.lbl_hint.setObjectName("PageHint")
        trow.addWidget(self.lbl_hint)
        head.addWidget(title_row, 0, Qt.AlignTop)
        head.addStretch(1)
        # 卡片样式是**整页一个**（2026-11-18 用户定）：它表达"这页怎么读"，
        # 不是某条监控项的属性，所以不放进配置弹窗、不给每条各设一次。
        lbl_style = QLabel("卡片样式", self)
        lbl_style.setObjectName("PageHint")
        head.addWidget(lbl_style, 0, Qt.AlignVCenter)
        self.cb_style = Combo(palette, self)
        self.cb_style.setFixedWidth(132)
        for key, label in STYLE_LABELS:
            self.cb_style.addItem(label, key)
        self.cb_style.setCurrentIndex(max(0, self.cb_style.findData(store.prefs.consumable_style)))
        self.cb_style.currentIndexChanged.connect(self._on_style)
        head.addWidget(self.cb_style)
        self.cb_sort = Combo(palette, self)
        self.cb_sort.setFixedWidth(132)
        for key, label in SORT_LABELS:
            self.cb_sort.addItem(label, key)
        self.cb_sort.currentIndexChanged.connect(self._on_sort)
        head.addWidget(self.cb_sort)
        col.addLayout(head)

        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("ConsumableScroll")
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._canvas = QWidget()
        self._canvas.setObjectName("ConsumableCanvas")
        self._grid = QGridLayout(self._canvas)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(CARD_GAP)
        self._grid.setVerticalSpacing(CARD_GAP)
        self.scroll.setWidget(self._canvas)
        col.addWidget(self.scroll, 1)

        self._empty = QLabel(
            "还没有监控项。\n点右下角「＋ 添加设备」挑一台设备，墙上会先落一张空卡；"
            "点那张空卡再挑属性和阈值。",
            self,
        )
        self._empty.setObjectName("EmptyHint")
        self._empty.setAlignment(Qt.AlignCenter)
        col.addWidget(self._empty, 1)

        foot = QHBoxLayout()
        foot.addStretch(1)
        self.btn_add = QPushButton("＋ 添加设备", self)
        self.btn_add.setObjectName("Primary")
        self.btn_add.setCursor(Qt.PointingHandCursor)
        self.btn_add.clicked.connect(self.add_item)
        foot.addWidget(self.btn_add)
        col.addLayout(foot)

        self.setStyleSheet(_qss(palette))
        self.cb_sort.set_palette(palette)
        self.refresh()

    # -- 数据 ------------------------------------------------------------
    def _rows_from_store(self) -> list[Row]:
        """把监控项翻译成界面行。顺序 = ``prefs.consumables`` 的顺序。

        **跨全部家庭**（2026-11-21 用户定，推翻 11-18 的"只取当前家庭"）：
        这一页已经不跟顶栏家庭走了，因为耗材是"整栋房子的东西该换了"，不是
        "这一套房的东西该换了"。两个家的"客厅"靠 ``Row.place_text`` 的
        「家庭 · 房间」前缀区分，靠 ``place_key`` 的 id 对分组，不会串。

        连带效果：``counts()`` 自动变成全家庭计数，侧栏圆点与桌面挂件角标
        从此同源（见 ``_on_consumable_counts``）。
        """
        rows: list[Row] = []
        style = self.store.prefs.consumable_style
        #: did → (siid.piid) → 界面名。同一台设备可能有多条监控项，算一次就够
        name_cache: dict[str, dict[tuple[int, int], str]] = {}
        #: 家庭 id → 显示名。``store.home_label`` 每次都要遍历拓扑，这里缓存一份
        home_names: dict[str, str] = {}
        for item in self.store.prefs.consumables:
            st = self.store.devices.get(item.did)
            if st is None:
                # 设备被删 / 换了家庭：**配置保留**，并且仍然画一张卡——否则这条
                # 监控项既看不见也点不进去，用户就永远删不掉它了。卡片会写
                # 「设备已不存在」，点开照样能删除。
                rows.append(
                    Row(
                        item=item,
                        name=item.label or (item.method if item.is_configured else "空卡"),
                        device="设备已不存在",
                        room="",
                        percent=None,
                        level=model.LEVEL_NONE,
                        value_text="" if item.is_configured else "未设置",
                        style=style,
                    )
                )
                continue
            # 设备还在就一定能解析出归属（``device_room`` 只在这种情况返回 None）
            loc = self.store.device_room(item.did)
            home_id, room_id = loc if loc is not None else ("", st.room_id)
            home = home_names.get(home_id)
            if home is None:
                home = home_names[home_id] = self.store.home_label(home_id)
            if not item.is_configured:
                # 空卡：只记了设备，属性还没挑。点它才进配置，别在这里替用户猜
                rows.append(
                    Row(
                        item=item,
                        name=st.name,
                        device=st.name,
                        room=st.room,
                        percent=None,
                        level=model.LEVEL_NONE,
                        value_text="未设置",
                        style=style,
                        home=home,
                        home_id=home_id,
                        room_id=room_id,
                    )
                )
                continue
            prop = next(
                (p for p in st.spec.properties if p.key == (item.siid, item.piid)),
                None,
            ) if st.spec is not None else None
            value = st.get(item.siid, item.piid)
            span = prop.value_range[:2] if prop is not None and prop.value_range else None
            percent = model.fill_percent(value, span)
            names = name_cache.get(item.did)
            if names is None:
                names = name_cache[item.did] = (
                    prop_labels(st.spec.properties) if st.spec is not None else {}
                )
            name = item.label or (names.get(prop.key, prop.display_label)
                                  if prop is not None else item.method)
            # 只有离线设备才标读数时间：在线设备的读数每轮都在刷新，标了没信息
            note, stale = ("", False)
            if not st.online:
                note, stale = read_note(st.stamps.get((item.siid, item.piid)))
            rows.append(
                Row(
                    item=item,
                    name=name,
                    device=st.name,
                    room=st.room,
                    percent=percent,
                    level=model.level_of(percent, item.warn, item.crit),
                    value_text="—" if percent is None else f"{percent:.0f}%",
                    style=style,
                    updated=note,
                    stale=stale,
                    home=home,
                    home_id=home_id,
                    room_id=room_id,
                )
            )
        return rows

    def _ordered(self, rows: list[Row]) -> list[Row]:
        mode = self.store.prefs.consumable_sort
        if mode == "level":
            return sorted(rows, key=lambda r: (r.sort_key, r.name))
        if mode == "room":
            order = self._room_order()
            return sorted(rows, key=lambda r: (order.get(r.place_key, 999), r.sort_key, r.name))
        return rows

    def _room_order(self) -> dict[tuple[str, str], int]:
        """``(家庭 id, 房间 id)`` → 序号，顺序 = 家庭顺序 → 家内房间顺序。

        用 ``store.rooms()`` **不传参**：它返回全部家庭，而且已经按
        ``prefs.order_homes`` + 每家的 ``prefs.order_rooms`` 排过——和顶栏、侧栏
        是同一套顺序，不用自己拼。

        键必须是 id 对，不能是房间名：两个家都可能有"客厅"，按名字分组会把它们
        并成一组（2026-11-21 前就是按名字）。

        注意 ``rooms()`` 只返回**有设备**的房间（且过滤了隐藏设备），所以被隐藏
        设备的耗材在这里查不到 → 落到 999 排最后。这是既有行为，不是这次引入的。
        """
        order: dict[tuple[str, str], int] = {}
        try:
            for index, room in enumerate(self.store.rooms()):
                order.setdefault((room.home, room.room), index)
        except Exception:  # 拓扑还没加载完：分组退化成"同序"，不影响显示
            return {}
        return order

    def counts(self) -> tuple[int, int]:
        warn = sum(1 for r in self._rows if r.level == model.LEVEL_WARN)
        crit = sum(1 for r in self._rows if r.level == model.LEVEL_CRIT)
        return warn, crit

    # -- 刷新 ------------------------------------------------------------
    def refresh(self, *, preserve_scroll: bool = False) -> None:
        """全量重建：监控项增删、排序方式变了、换样式改了卡片尺寸都走这里。

        ``preserve_scroll=True`` 只给**换卡片样式**用（圆环是方卡，尺寸一变整页
        重排）：那次重建不是用户想回到顶部，只是卡片换了画法；把滚动值原样带过去，
        新布局更长就停在相应位置、更短就夹到末尾。其余刷新（加 / 删监控项、拓扑
        重建）仍按原样回顶部——那种时候回到开头才对。

        2026-11-21 起**切家庭不再调这里**：这一页按全部家庭统计，内容跟顶栏家庭
        无关（见 ``_rows_from_store``）。
        """
        keep = self.scroll.verticalScrollBar().value() if preserve_scroll else None
        rows = self._rows_from_store()
        self._rows = self._ordered(rows)
        self._rebuild()
        if keep is not None:
            self._pending_scroll = keep
            self._restore_scroll()
            # 重建后的布局可能要等下一轮事件循环才把 maximum() 撑开；再排一次确保
            # 目标值没被后到的 clamp 打回 0。
            QTimer.singleShot(0, self._restore_scroll)

    def _restore_scroll(self) -> None:
        value = self._pending_scroll
        if value is None:
            return
        # 刚 addWidget 完，滚动条量程还停在旧值（0）。先让画布布局生效，
        # 否则下面 setValue 会被立刻 clamp 回 0，定时器的第二次机会也没了。
        self._grid.activate()
        self._canvas.adjustSize()
        bar = self.scroll.verticalScrollBar()
        if bar.maximum() <= 0:
            # 量程还没就绪（不是"没有滚动"）：别把待恢复值清掉，下一轮再试。
            # 真没长到要滚动时，下面这个兜底会把它丢掉，免得留到下次切家误伤。
            QTimer.singleShot(100, self._drop_scroll_pending)
            return
        bar.setValue(min(value, bar.maximum()))
        self._pending_scroll = None

    def _drop_scroll_pending(self) -> None:
        self._pending_scroll = None

    def update_cards(self, dids: set[str]) -> None:
        """值变了：只改受影响的那几张卡，别整页重建（重建会让拖动 / 悬停断掉）。"""
        if not dids:
            return
        rows = self._rows_from_store()
        changed = False
        fresh: dict[str, Row] = {r.item.id: r for r in rows}
        for cid, card in self._cards.items():
            row = fresh.get(cid)
            if row is not None and row.item.did in dids:
                card.set_row(row)
                changed = True
        if changed:
            self._rows = self._ordered(rows)
            self._maybe_reorder()
            self._emit_counts()

    def _maybe_reorder(self) -> None:
        """按余量 / 按房间排序时，值变了可能要换位置；位置没变就不重建。"""
        if self.store.prefs.consumable_sort == "custom":
            return
        want = [r.item.id for r in self._rows]
        have = [cid for cid in self._cards]
        if want != have:
            self._rebuild()

    def _rebuild(self) -> None:
        for card in self._cards.values():
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        rows = self._rows
        self._empty.setVisible(not rows)
        self.scroll.setVisible(bool(rows))
        if rows:
            self._reflow()
        self.lbl_hint.setText(self._hint_text())
        self._emit_counts()

    def _hint_text(self) -> str:
        total = len(self._rows)
        if not total:
            return "还没添加监控项"
        shells = sum(1 for r in self._rows if r.is_shell)
        warn, crit = self.counts()
        # 跨家庭后这句要说明范围：用户切顶栏家庭时这一页纹丝不动，
        # 「共 N 项 · M 个家庭」是唯一能解释"为什么没变"的地方。
        homes = {r.home_id for r in self._rows if r.home_id}
        head = f"共 {total} 项"
        if len(homes) > 1:
            head += f" · {len(homes)} 个家庭"
        parts = [head]
        if shells:
            parts.append(f"{shells} 项待设置")
        if crit:
            parts.append(f"{crit} 项急需更换")
        if warn:
            parts.append(f"{warn} 项该准备了")
        if len(parts) == 1:
            parts.append("全部充足")
        return " · ".join(parts)

    def _emit_counts(self) -> None:
        counts = self.counts()
        if counts != self._last_counts:
            self._last_counts = counts
            self.counts_changed.emit(*counts)

    # -- 布局 ------------------------------------------------------------
    def _columns(self) -> int:
        """按页面宽度算列数。**不用 ``scroll.viewport().width()``**：页面还没
        上屏 / 刚 resize 的那一刻视口还是旧尺寸，会算成 1 列并一直卡在 1 列
        （2026-11-18 首次渲染踩到，卡片全挤到中间）。"""
        card_w = card_size(self.store.prefs.consumable_style)[0]
        usable = max(card_w, self.width() - PAGE_MARGIN * 2)
        return max(1, (usable + CARD_GAP) // (card_w + CARD_GAP))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._cards and self._columns() != self._cols:
            self._reflow()

    def showEvent(self, event) -> None:  # noqa: N802
        """第一次上屏时再对一次列数：构造期算出的列数基于默认尺寸，不可信。"""
        super().showEvent(event)
        if self._cards and self._columns() != self._cols:
            self._reflow()

    def _reflow(self) -> None:
        cols = self._columns()
        self._cols = cols
        # 清空布局（保留控件，稍后重新 addWidget）
        while self._grid.count():
            self._grid.takeAt(0)
        for column in range(self._grid.columnCount() + 1):
            self._grid.setColumnStretch(column, 0)

        mode = self.store.prefs.consumable_sort
        #: 拖动只在自定义排序开放；按余量 / 按房间的顺序是算出来的，搬一张卡
        #: 没有稳定落点（下一轮刷新就会被排序规则塞回去）。
        draggable = mode == "custom"
        last_key: tuple[str, str] | None = None
        row_index = 0
        col_index = 0
        for row in self._rows:
            # 分组认 id 对：两个家都可能有"客厅"，按名字比会把两组并成一组
            if mode == "room" and row.place_key != last_key:
                last_key = row.place_key
                header = QLabel(row.place_text or "设备已不存在", self._canvas)
                header.setObjectName("RoomHeader")
                if col_index:
                    row_index += 1
                    col_index = 0
                self._grid.addWidget(header, row_index, 0, 1, cols)
                row_index += 1
                col_index = 0
            card = self._cards.get(row.item.id)
            if card is None:
                card = ConsumableCard(row, self._p,
                                      draggable=draggable, parent=self._canvas)
                card.clicked.connect(self._edit)
                card.drag_started.connect(self._on_drag_start)
                card.drag_moved.connect(self._on_drag_move)
                card.drag_dropped.connect(self._on_drop)
                self._cards[row.item.id] = card
            else:
                # 卡片跨排序切换复用，开关要跟着当前模式走
                card.set_draggable(draggable)
            # 不给对齐方式的话 QGridLayout 会把定宽控件**居中**摆进整列，
            # 卡片看起来像浮在页面中间（第一版就是这样）
            self._grid.addWidget(card, row_index, col_index,
                                 Qt.AlignLeft | Qt.AlignTop)
            col_index += 1
            if col_index >= cols:
                col_index = 0
                row_index += 1
        # 末尾的弹性列 / 行吃掉剩余空间：卡片从左侧开始排，不被拉宽也不居中
        self._grid.setColumnStretch(cols, 1)
        self._grid.setRowStretch(row_index + 1, 1)

    # -- 交互 ------------------------------------------------------------
    def _on_sort(self, _index: int) -> None:
        key = str(self.cb_sort.currentData() or "")
        if self.store.prefs.set_consumable_sort(key):
            self.refresh()

    def _on_style(self, _index: int) -> None:
        """换卡片画法：尺寸没变就只重画；**尺寸变了必须重建**（圆环是方卡），
        但仍用 ``preserve_scroll`` 保住滚动，不让用户正看的卡跳回顶部。"""
        key = str(self.cb_style.currentData() or "")
        old_size = card_size(self.store.prefs.consumable_style)
        if not self.store.prefs.set_consumable_style(key):
            return
        style = self.store.prefs.consumable_style
        if card_size(style) != old_size:
            self.refresh(preserve_scroll=True)
            return
        # 卡片的 row 与 ``self._rows`` 里的对象**不一定是同一个**：``update_cards``
        # 会给变过的那几张换新对象，没变的那几张还指着旧的。所以按 id 重新发一次，
        # 别只改 ``self._rows`` 就指望卡片跟着变。
        by_id = {r.item.id: r for r in self._rows}
        for row in self._rows:
            row.style = style
        for cid, card in self._cards.items():
            row = by_id.get(cid)
            if row is not None:
                card.set_row(row)
            # set_row 遇到"同一个对象"会早退，这里补一次重绘兜底
            card.update()

    def add_item(self) -> None:
        """第一步只选设备：选完在墙上落一张空卡，属性等用户点它再配。"""
        from .consumable_dialog import ConsumableDialog

        dialog = ConsumableDialog(self.store, self._p, parent=self.window())
        if self._run_dialog is not None:
            self._run_dialog(dialog)
        else:
            dialog.exec()
        did = dialog.selected_did
        if not did:
            return
        shell = model.unset(did)
        if not self.store.prefs.add_consumable(shell):
            # 这台设备已经有一张空卡了：直接打开它，别在墙上再摞一张同样的空壳
            self.refresh()
            self._edit(shell.id)
            return
        self.refresh()

    def _edit(self, cid: str) -> None:
        from .consumable_dialog import ConsumableDialog

        item = self.store.prefs.consumable(cid)
        if item is None:
            return
        dialog = ConsumableDialog(self.store, self._p, item=item, parent=self.window())
        if self._run_dialog is not None:
            self._run_dialog(dialog)
        else:
            dialog.exec()
        if dialog.deleted or dialog.added is not None:
            self.refresh()

    # -- 拖动排序（和主页卡片墙同一套交互）-------------------------------
    # 卡片墙用 CardGridLayout + eventFilter；这页有房间分组头、用的是 QGridLayout，
    # 不能直接换布局类，所以复用它的三样东西：DragGesture（卡片里）、DragGhost /
    # rounded_shot（这里），落点判定照 CardGridLayout.drop_index 的阅读顺序语义。
    # 拖动中只重排内存里的 self._rows + _reflow()——**不重建卡片、不写盘**；
    # 重建会 deleteLater() 掉正在拖的那张卡，幽灵和占位框当场失效。松手才落盘。
    # **只有自定义排序才开放拖动**（2026-11-18 用户改的口径）：别的模式连手势
    # 都不启动，也不会在拖动时偷偷替用户把排序方式改成 custom。

    def _on_drag_start(self, cid: str) -> None:
        if self.store.prefs.consumable_sort != "custom":
            # 卡片层在非自定义模式下不会发这个信号；这里是给绕过界面的直接调用
            # 兜底，别让它把半途的位置写回 prefs。
            return
        card = self._cards.get(cid)
        if card is None:
            return
        self._drag_id = cid
        self._drag_moved = False
        self._drag_backup = list(self._rows)

        # 幽灵对准按下那一刻鼠标抓在卡上的位置，不是死死对齐左上角
        self._drag_grab = QCursor.pos() - card.mapToGlobal(QPoint(0, 0))
        card_pos = card.mapTo(self._canvas, QPoint(0, 0))
        ghost = DragGhost(self._canvas, rounded_shot(card))
        ghost.setGeometry(card_pos.x(), card_pos.y(), card.width(), card.height())
        ghost.show()
        ghost.raise_()
        self._drag_ghost = ghost
        card.set_drag_placeholder(True)   # 原位置留虚线框 = 落点提示
        self._drag_guard.start()

    def _on_drag_move(self, global_pos: QPoint) -> None:
        if not self._drag_id:
            return
        self._drag_moved = True
        if self._drag_ghost is not None:
            self._drag_ghost.move(self._canvas.mapFromGlobal(global_pos) - self._drag_grab)

        # 靠上下边缘自动滚，长列表也能把卡拖到屏外
        viewport = self.scroll.viewport()
        vp_pos = viewport.mapFromGlobal(global_pos)
        if vp_pos.y() < 28:
            self.scroll.verticalScrollBar().setValue(
                self.scroll.verticalScrollBar().value() - 24)
        elif vp_pos.y() > viewport.height() - 28:
            self.scroll.verticalScrollBar().setValue(
                self.scroll.verticalScrollBar().value() + 24)

        # 落点看**被拖卡片的中心**：抓取点在哪是随机的，用鼠标点判定会让同一位置
        # 换个抓法得出不同结果（主页卡片墙同一条约定，见 dnd.grid_drop_index）
        probe = global_pos
        if self._drag_ghost is not None:
            probe = self._drag_ghost.mapToGlobal(self._drag_ghost.rect().center())
        target = self._drop_index(probe, self._drag_id)
        current = next((i for i, r in enumerate(self._rows)
                        if r.item.id == self._drag_id), -1)
        if current >= 0 and target != current:
            self._rows.insert(target, self._rows.pop(current))
            self._reflow()   # 只摆位置，不删卡不落盘

    def _drop_index(self, probe_global_pos: QPoint, source_id: str) -> int:
        """落点索引：先按行带、行内按左右中线（判据在 ``dnd.grid_drop_index``）。

        ``probe_global_pos`` 应当传**被拖卡片的中心**（全局坐标），不是鼠标点。
        返回的是排除被拖卡片之后的插入下标，可直接当 ``list.insert`` 的下标。
        """
        boxes = []
        for row in self._rows:
            if row.item.id == source_id:
                continue
            card = self._cards.get(row.item.id)
            if card is None:
                continue
            boxes.append(card.geometry())
        return grid_drop_index(self._canvas.mapFromGlobal(probe_global_pos), boxes)

    def _on_drop(self, cid: str, global_pos: QPoint) -> None:
        """松手：收幽灵、还原占位框，把内存里排好的顺序写回 prefs。"""
        if not self._drag_id:
            return
        self._drag_guard.stop()
        self._drag_id = ""
        self._drag_backup = None
        card = self._cards.get(cid)
        if card is not None:
            card.set_drag_placeholder(False)
            card.unsetCursor()
        if self._drag_ghost is not None:
            self._drag_ghost.hide()
            self._drag_ghost.deleteLater()
            self._drag_ghost = None

        if self._drag_moved:
            target = next((i for i, r in enumerate(self._rows)
                           if r.item.id == cid), -1)
        else:
            # 没收到 move 的调用（离线验证 / 外部直接调用）按旧口径取落点
            target = self._index_at(global_pos)
        self._drag_moved = False
        if target >= 0:
            self.store.prefs.move_consumable(cid, target)
        self.refresh()

    def _on_drag_aborted(self, reason: str) -> None:
        """拖动被外部打断（切程序 / 截图 / 漏了松手事件）：**整段还原**。

        拖动期间只改了内存里的 ``_rows``、没写盘，所以还原就是把开始时那份顺序
        摆回去；不 refresh（那会重建卡片），只重排几何。
        """
        if not self._drag_id:
            return
        cid = self._drag_id
        self._drag_guard.stop()
        self._drag_id = ""
        self._drag_moved = False
        card = self._cards.get(cid)
        if card is not None:
            card.set_drag_placeholder(False)
            card.reset_drag_state()
        if self._drag_ghost is not None:
            self._drag_ghost.hide()
            self._drag_ghost.deleteLater()
            self._drag_ghost = None
        if self._drag_backup is not None:
            self._rows = list(self._drag_backup)
            self._drag_backup = None
            self._reflow()

    def _index_at(self, global_pos: QPoint) -> int:
        """落点落在哪张卡上 → 那条的序号。落在空白处就取最近的一张。"""
        order = [r.item.id for r in self._rows]
        best, best_dist = -1, None
        for cid in order:
            card = self._cards.get(cid)
            if card is None:
                continue
            local = card.mapFromGlobal(global_pos)
            if card.rect().contains(local):
                return order.index(cid)
            center = card.mapToGlobal(card.rect().center())
            dist = (center - global_pos).manhattanLength()
            if best_dist is None or dist < best_dist:
                best, best_dist = order.index(cid), dist
        return best

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.setStyleSheet(_qss(palette))
        self.cb_sort.set_palette(palette)
        self.cb_style.set_palette(palette)
        for card in self._cards.values():
            card.set_palette(palette)
        self._last_counts = None
        self.refresh()
