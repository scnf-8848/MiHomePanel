"""设备卡片。

一张卡片只回答三个问题：**这是什么、现在什么状态、我能立刻做什么**。

卡片被切成两条横带，1×1 和 2×1 **共用同一套坐标**：

    ┌──────────────────────────┐
    │ 设备名              ╭──╮ │   ← 标题带 y=14..76
    │ 状态/读数            ╰──╯ │      （名称左上，电源右上）
    ├──────────────────────────┤
    │                          │
    │       产品图 / 控件        │   ← 内容带 y=76..154
    │                          │
    └──────────────────────────┘

**为什么标题必须顶置、产品图下沉**：1×1 卡片如果学米家把名字沉底，
和同排的 2×1（名字在顶）混在一起时，两个标题会差 80px，肉眼一眼就看歪。
统一顶置后两种卡的标题同基线、内容带同起点，一行里混排完全整齐。
代价是 1×1 的标题可用宽只有 94px（电源键占了右上），所以标题**单行 + 省略号**，
靠悬停提示（``ui/tooltip.py``）兜全名。

几个决定观感的细节：

* **开启状态靠底色染品类色，不靠边框。** 边框一旦变色，整面墙会显得脏；
  淡色底 + 白卡片交错，才是米家的观感。
* **产品图不染色、不加圆底。** 官方图本身就是白底实拍，染色反而假。
* **读数和状态文字只用一档字号。** 卡片上塞第二档小字就会变成"工具感"。
* **离线不是画红叉**，是整卡降透明度 + 状态文字改成"已离线"。

外壳和内容是分开的：外壳（底色 / 标题 / 电源键）始终在这里自绘，内容带
交给 ``ui/card_templates.py`` 的样式。标准样式（1×1）不创建子控件，产品图
和多键面板还是自绘；带滑条的样式（2×1）在 ``_body_rect()`` 里挂一个真实
``CardBody``。两者共用同一套坐标，所以 1×1 和 2×1 混排时标题、内容带的
基线不会错开。宽度由样式自带，卡片本身不再接受单独的"大小"参数。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..core.catalog import summary_line
from ..core.store import DeviceState
from . import theme, tooltip
from .card_templates import BODY_VIDEO, STYLES, CardBody, CardStyle
from .widgets import IconProvider, draw_text

PAD = 14
#: 标题带：名称单行（22），副标题（18）。副标题**用满宽**——
#: 它从电源键下沿才开始，不再受右上角那个圆让位的限制，
#: 否则 "3 W · 4238.9 lx" 这类读数会被截掉最关键的后半段。
TITLE_TOP = 14
TITLE_H = 22
SUB_TOP = 44
SUB_H = 18
BODY_TOP = 76
#: 产品图边长。内容带高 78，图 76 居中后上下各留 1px。
ART = 76
#: 电源键。32 而不是 34：省出来的 2px 让标题多放半个字，
#: 同时下沿正好落在副标题起点之上，两者不抢空间。
POWER_D = 32
POWER_TOP = 12
GANG_H = 32
GANG_GAP = 8
#: 2–3 键改用"竖条"：整条铺满内容带、并排而立，模仿实体开关被分成几个模块的样子。
#: 4 键及以上仍用胶囊网格——四个竖条只有 28px 宽，竖排文字会被压成一条缝。
GANG_BAR_MAX = 3
#: 竖条的圆角比胶囊小一档。12 的圆角落在 40px 宽的条上会变成胶囊，
#: 而实体开关模块的边角是接近直角的。
GANG_BAR_RADIUS = 10
#: 竖排文字上下留白。78 高的条留 12 之后，12px 字号正好排得下 4 个字
#: （4×16 = 64），再长才降字号或省略。
VERT_PAD = 6
GANG_LABEL_PX = 12
GANG_LABEL_MIN_PX = 10


def use_bars(count: int) -> bool:
    """这一路数该画竖条还是胶囊。

    1–3 键都用"整块面板"的画法：单开就是一块满宽的按键，三开就是切成三格。
    4 键以上竖条只剩 28px 宽，竖排文字排不开，退回胶囊网格。
    """
    return 1 <= count <= GANG_BAR_MAX


def _label_font(size: int) -> QFont:
    font = QFont()
    font.setPixelSize(size)
    font.setWeight(QFont.Medium)
    return font


def label_line_h(size: int) -> float:
    """竖排时每个字的步进高度 = 行高 + 1px 字距。

    不能用 ``size`` 当步进：中文行高比字号大 30% 左右，按字号算会挤成一团。
    """
    return QFontMetrics(_label_font(size)).height() + 1


def stack_lines(label: str, inner_h: float, line_h: float) -> list[str]:
    """把标签拆成逐字竖排的行；装不下时末位换成省略号。"""
    fit = max(1, int(inner_h // line_h)) if line_h > 0 else 1
    if len(label) <= fit:
        return list(label)
    return [*label[: max(1, fit - 1)], "…"]


def is_ascii(label: str) -> bool:
    return all(ord(ch) < 128 for ch in label)


def label_extent(label: str, size: int) -> float:
    """这个标签在竖条里排下来需要多少**高度**。

    中文按"字数 × 步进"，纯西文整体转 90°（见 ``paint_vertical_label``），
    所以它占的高度等于它的文字宽度。
    """
    if not label:
        return 0.0
    if is_ascii(label):
        return QFontMetrics(_label_font(size)).horizontalAdvance(label)
    return len(label) * label_line_h(size)


def paint_vertical_label(
    p: QPainter, rect: QRectF, label: str, color: str, size: int, line_h: float
) -> None:
    """在竖条里画标签。

    中文按**一个字一行**竖排（竖排是中文的原生写法）；纯西文则整体转 -90°——
    把 "Ceiling" 拆成七个字母竖着码，读起来就是一串散字母。
    """
    if not label:
        return
    font = _label_font(size)
    p.setFont(font)
    p.setPen(QColor(color))

    if is_ascii(label):
        text = QFontMetrics(font).elidedText(label, Qt.ElideRight, int(rect.height()) - VERT_PAD * 2)
        p.save()
        p.translate(rect.center())
        p.rotate(-90)
        p.drawText(
            QRectF(-rect.height() / 2, -rect.width() / 2, rect.height(), rect.width()),
            Qt.AlignCenter,
            text,
        )
        p.restore()
        return

    lines = stack_lines(label, rect.height() - VERT_PAD * 2, line_h)
    y = rect.center().y() - len(lines) * line_h / 2
    for ch in lines:
        p.drawText(QRectF(rect.x(), y, rect.width(), line_h), Qt.AlignCenter, ch)
        y += line_h


class DeviceCard(QWidget):
    clicked = Signal(str)  # did
    toggled = Signal(str, int, int)  # did, siid, piid
    menu_requested = Signal(str, QPoint)  # did, 全局坐标
    value_committed = Signal(str, int, int, object)  # did, siid, piid, value（松手才发）

    def __init__(
        self,
        state: DeviceState,
        icons: IconProvider,
        palette: theme.Palette,
        style: CardStyle | None = None,
    ) -> None:
        super().__init__()
        self.state = state
        self._icons = icons
        self._p = palette
        # 不传 style 的调用方（验证脚本、详情页预览）拿到默认 1×1 标准小卡
        self._style = style or STYLES[0]
        self._body: CardBody | None = None
        #: 布局在窄窗口里做的降级覆盖；``None`` = 用样式自带的尺寸
        self._grid_span: int | None = None
        self._grid_rows: int | None = None
        #: 拖动排序时这张卡就是"占位框"：保留格子、不画内容
        self._placeholder = False
        #: 按下落在电源键 / 多键上时，松手不要再当成"打开详情"
        self._press_on_control = False
        self._hover = False
        self._hot: tuple[str, int] | None = None  # ("power",0) 或 ("gang", siid)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._apply_geometry()
        self._build_body()
        # 标题是单行 + 省略号，全名靠悬停提示兜着。用自绘的而不是 ``setToolTip``：
        # Qt 原生的唤醒机制是全局的，一旦弹过一次，之后每张卡片都秒出，且四角
        # 外面还有一块系统画的方形底（见 ui/tooltip.py）。
        # 命中区只给标题带：内容带里可能挂着真实滑条 / 多键按键，气泡盖上去
        # 既挡读数又挡拖动。hit_test 是动态的，卡片 1×1 ↔ 2×1 改宽度后仍然准。
        tooltip.watch(self, lambda: self.state.name, hit_test=self._title_hit)

    # -- 尺寸与模板 -------------------------------------------------------
    @property
    def span(self) -> int:
        return self._style.span

    @property
    def rows(self) -> int:
        return self._style.rows

    @property
    def style(self) -> CardStyle:
        return self._style

    @property
    def grid_span(self) -> int:
        """当前实际占的列数。窄窗口把 2×2 降级时由布局覆盖。"""
        return self._grid_span if self._grid_span is not None else self._style.span

    @property
    def grid_rows(self) -> int:
        """当前实际占的行数。"""
        return self._grid_rows if self._grid_rows is not None else self._style.rows

    def set_grid_cells(self, span: int, rows: int) -> None:
        """布局按列数做的降级：只改几何，不改样式偏好。

        用户在右键菜单里选的仍是 2×2；窗口拉宽后布局会再次调回 2×2。
        """
        span, rows = max(1, int(span)), max(1, int(rows))
        if (span, rows) == (self.grid_span, self.grid_rows):
            return
        self._grid_span, self._grid_rows = span, rows
        self._apply_geometry()
        if self._body is not None:
            self._body.setGeometry(self._body_rect())
        self.update()

    def _apply_geometry(self) -> None:
        # 2×1 = 两张 1×1 加一条列间距，跨列卡和单列卡才能严丝合缝地对齐
        w = theme.CARD_MIN_W * self.grid_span + theme.CARD_GAP * (self.grid_span - 1)
        # 2×2 = 两行等高卡加一条行间距
        h = theme.CARD_H * self.grid_rows + theme.CARD_GAP * (self.grid_rows - 1)
        # 缓存设计尺寸：sizeHint 必须回这个固定值，不能回 self.width()——
        # 后者在当前尺寸被布局拉伸后会返回错误值，形成"越来越大"的反馈循环。
        self._design = QSize(w, h)
        self.setFixedSize(self._design)

    def sizeHint(self) -> QSize:  # noqa: N802
        # CardGridLayout 用 sizeHint 决定每行放几张，必须是稳定的设计尺寸
        return self._design

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # 改宽度（1×1 ↔ 2×1）后 body 要跟着重摆，否则滑条还留在旧的 138 宽里
        if self._body is not None:
            self._body.setGeometry(self._body_rect())

    # -- 模板 body --------------------------------------------------------
    def _build_body(self) -> None:
        if self._body is not None:
            old = self._body
            old.setParent(None)
            old.deleteLater()
            self._body = None
        if not self._style.has_body or self._style.body == BODY_VIDEO:
            # 画面样式由 CameraCard 自己铺视频控件，这里不建 CardBody
            return
        body = CardBody(
            self._style,
            self.state,
            self._p,
            theme.category_color(self.state.card.category),
            self,
        )
        body.setGeometry(self._body_rect())
        body.value_committed.connect(self._on_body_committed)
        body.previewed.connect(self.update)
        body.menu_requested.connect(
            lambda pos: self.menu_requested.emit(self.state.did, pos)
        )
        body.show()  # 子控件默认 hidden，父已 show 时也必须显式 show
        self._body = body

    def _on_body_committed(self, siid: int, piid: int, value: object) -> None:
        self.value_committed.emit(self.state.did, siid, piid, value)

    def set_style(self, style: CardStyle) -> None:
        """右键菜单改样式（宽度随样式一起换）：只动这一张卡，不重建整面墙。"""
        if style == self._style:
            return
        self._style = style
        # 新样式有自己的尺寸；之前窄窗口的 1×1 降级不能继续压着它
        self._grid_span = self._grid_rows = None
        self._apply_geometry()
        self._build_body()
        self.update()

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        if self._body is not None:
            self._body.apply_theme(palette, theme.category_color(self.state.card.category))
        self.update()

    def refresh(self, state: DeviceState | None = None) -> None:
        if state is not None:
            self.state = state
        self._icons.ensure(self.state.node.model)
        self._apply_geometry()
        if self._body is not None:
            # 拖动中的滑条会自己跳过外部赋值，不会把用户手里的值拽回去
            self._body.sync(self.state)
        self.update()

    # -- 绘制 ------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        st = self.state
        card = st.card
        pal = self._p
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        if self._placeholder:
            # 拖动中的落点：保留原来的格子，只画一个虚线框；内容在
            # ``set_drag_placeholder`` 里已隐藏，网格不会因为少一张卡而塌。
            p.setPen(QPen(QColor(pal.accent), 2, Qt.DashLine))
            p.setBrush(QColor(theme.blend(pal.card, pal.accent, 0.08)))
            p.drawRoundedRect(rect, theme.RADIUS_CARD, theme.RADIUS_CARD)
            p.end()
            return

        on = st.any_on
        base = theme.category_color(card.category)
        # 开启时染一层很淡的品类色；悬停只挪一档明度，**不加描边**。
        # 浅色模式下混色会把卡片压得和背景一样暗（电视、摄像头这类深灰品类甚至
        # 更暗），所以给它一个"不许低于背景 +10 亮度"的下限，见 theme.tint
        strength = 0.10 if on else 0.0
        if strength:
            bg = theme.tint(base, strength, pal, min_gap=6)
        else:
            bg = pal.card
        if self._hover:
            bg = theme.card_hover_color(bg, pal)

        p.setOpacity(1.0 if st.online else 0.45)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(bg))
        p.drawRoundedRect(rect, theme.RADIUS_CARD, theme.RADIUS_CARD)

        # 内容带：挂了模板 body 就整块让给子控件。它们是真实 QWidget，画在
        # 父窗口之上；这里再画一次产品图只会从滑条缝里透出来，显得脏。
        if not self._style.has_body:
            if card.gangs:
                self._paint_gangs(p, base, bg)
            else:
                self._paint_art(p)

        # 标题：**单行 + 省略号**。电源键占住右上角，标题只能让出它的宽度
        # （166 宽 - 两侧 14 - 电源 32 - 间距 10 = 96），刚好 5 个半中文字；
        # 再长的型号名交给 tooltip。两行会顶到内容带，所以不放开。
        draw_text(p, QRect(PAD, TITLE_TOP, self._title_width(), TITLE_H), st.name,
                  size=theme.FS_CARD_TITLE, color=pal.text, weight=QFont.Medium)
        # 副标题在电源键下沿之下展开，用满宽
        draw_text(p, QRect(PAD, SUB_TOP, self.width() - PAD * 2, SUB_H), self._subtitle(),
                  size=theme.FS_CARD_SUB, color=pal.text_soft)

        if not card.gangs:
            self._paint_power(p, base, on, pal)

        p.end()

    def _subtitle(self) -> str:
        st = self.state
        if not st.online:
            return "已离线"
        if st.error:
            return st.error
        if not st.node.spec_type:
            return "不支持控制"
        if st.card.gangs:
            on = sum(1 for g in st.card.gangs if st.values.get(g.power.key))
            return f"{len(st.card.gangs)} 路 · {on} 路开启" if on else f"{len(st.card.gangs)} 路 · 全部关闭"
        text = summary_line(st.card, st.values)
        if text:
            return text
        if st.power is None:
            return "—"
        return "已开启" if st.power else "已关闭"

    # -- 布局 ------------------------------------------------------------
    def _title_hit(self, pos: QPoint) -> bool:
        """悬停提示的有效区：名称 + 状态文字那一条，右上角电源键除外。

        内容带（y ≥ BODY_TOP）整个不算——那里是产品图、多键按键或模板 body
        里的亮度/色温滑条。提示框从光标右下 16px 展开，一旦在内容带弹出就会
        压住控件。电源键虽然也在标题带，但它自身是热点，扣掉后不会在瞄准
        开关时糊上一层字。
        """
        band = QRect(PAD, TITLE_TOP, self.width() - PAD * 2,
                     SUB_TOP + SUB_H - TITLE_TOP)
        if not band.contains(pos):
            return False
        if (not self.state.card.gangs
                and self.state.card.control("power") is not None
                and self._power_rect().adjusted(-2, -2, 2, 2).contains(pos)):
            return False
        return True

    def _title_width(self) -> int:
        """标题可用宽。电源键占住右上角，标题给它让位。"""
        w = self.width() - PAD * 2
        if not self.state.card.gangs and self.state.card.control("power") is not None:
            w -= POWER_D + 10
        return max(48, w)

    def _body_rect(self) -> QRect:
        """内容带：产品图、多键胶囊、以后的控件模板都只能画在这个矩形里。"""
        return QRect(PAD, BODY_TOP, self.width() - PAD * 2, self.height() - PAD - BODY_TOP)

    def _power_rect(self) -> QRect:
        return QRect(self.width() - PAD - POWER_D, POWER_TOP, POWER_D, POWER_D)

    def _gang_rects(self) -> list[QRectF]:
        """多键的按键矩形。

        * **1–3 键：竖条。** 等宽等高、铺满内容带、彼此留 GANG_GAP，像一块被
          分成几格的实体开关面板；单开就是一块满宽的按键。2 键以上文字竖排
          （见 ``paint_vertical_label``），单开只有一块，横排更好读。
        * **4 键及以上：胶囊网格。** 竖条会被压到 28px 宽，竖排文字排不开；
          1–2 键一行反而更宽松（3 键时"排风扇"会被截成"排…"，所以 3–4 键走 2×2）。
        """
        gangs = self.state.card.gangs
        if not gangs:
            return []
        area = self._body_rect()
        n = len(gangs)
        if use_bars(n):
            cw = (area.width() - GANG_GAP * (n - 1)) / n
            return [
                QRectF(area.x() + i * (cw + GANG_GAP), area.y(), cw, area.height())
                for i in range(n)
            ]
        if n <= 2:
            rows, cols = 1, n
        elif n <= 4:
            rows, cols = 2, 2
        else:
            rows, cols = 2, 3
        cw = (area.width() - GANG_GAP * (cols - 1)) / cols
        ch = min(GANG_H, (area.height() - GANG_GAP * (rows - 1)) / rows)
        total_h = ch * rows + GANG_GAP * (rows - 1)
        y0 = area.y() + (area.height() - total_h) / 2
        out: list[QRectF] = []
        for i in range(min(n, rows * cols)):
            r, c = divmod(i, cols)
            # 末行不满时整行居中，避免最后一个孤零零贴在左边
            filled = min(cols, n - r * cols)
            row_w = filled * cw + GANG_GAP * (filled - 1)
            x = area.x() + (area.width() - row_w) / 2 + c * (cw + GANG_GAP)
            out.append(QRectF(x, y0 + r * (ch + GANG_GAP), cw, ch))
        return out

    def _paint_art(self, p: QPainter) -> None:
        """产品图：在内容带里居中，不染色、不加圆底。"""
        st = self.state
        pal = self._p
        area = self._body_rect()
        size = min(ART, area.height(), area.width())
        x = area.x() + (area.width() - size) // 2
        y = area.y() + (area.height() - size) // 2
        pix = self._icons.scaled(st.node.model, size)
        if pix is None:
            # 图还没到 / 这类设备本来就没有官方图：画个淡圆占位，别让内容带空着
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(pal.bg_soft))
            d = min(size, 56)
            p.drawEllipse(QRectF(area.center().x() - d / 2, area.center().y() - d / 2, d, d))
            return
        pos = QPoint(x + (size - pix.width()) // 2, y + (size - pix.height()) // 2)
        if pal.is_dark:
            # 官方产品图是白底实拍，直接贴在深色卡片上会刺眼，压一点亮度
            op = p.opacity()
            p.setOpacity(op * 0.88)
            p.drawPixmap(pos, pix)
            p.setOpacity(op)
        else:
            p.drawPixmap(pos, pix)

    def _paint_power(self, p: QPainter, base: str, on: bool, pal: theme.Palette) -> None:
        st = self.state
        if st.card.control("power") is None:
            return
        # 右上角，和标题同一行起——1×1 和 2×1 位置完全一致。
        rect = QRectF(self._power_rect())
        # 关闭时是一枚"半透明中性圆"，开启时实心品类色——都不描边，
        # 描边会让一排电源键看起来像一圈圈网格。
        p.setPen(Qt.NoPen)
        if on:
            p.setBrush(QColor(base))
        else:
            p.setBrush(QColor(theme.blend(pal.card, pal.text, 0.08)))
        p.drawEllipse(rect)
        self._paint_power_glyph(p, rect, "#FFFFFF" if on else pal.text_soft)

    @staticmethod
    def _paint_power_glyph(p: QPainter, rect: QRectF, color: str) -> None:
        """电源符号 ⏻：缺口朝上的一段圆环，中间一竖穿过缺口。

        两个尺寸必须一起调，否则会出现"圆圈加一根棍子"而不是电源符号：

        * **缺口要比竖线宽得多。** 缺口在正上方，竖线正好从中间穿过去，所以肉眼
          看到的是"缺口弦宽 - 竖线宽"这点缝。原来缺口 40°（弦宽 4.4px）减掉 1.8px
          笔宽后两边各剩 1.3px，等于没有缺口。现在 76°，弦宽 8.3px，两边各 3px。
        * **竖线不能比圆环还长。** 原来从 1.7r 画到 0.2r，长 1.5r ≈ 圆的 3/4，
          看着像根天线；而且弧的矩形容器还整体偏下 0.1r，竖线和环不同心，更显长。
          现在同心，从 1.18r 画到圆心，长度等于半径——和 iOS / 米家的符号同比例。
        """
        c = rect.center()
        r = rect.width() * 0.21
        p.setPen(QPen(QColor(color), 1.7, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)
        # 90° 在正上方；缺口 76° → 从右侧 128° 起、逆时针画 284°
        p.drawArc(QRectF(c.x() - r, c.y() - r, r * 2, r * 2), 128 * 16, 284 * 16)
        p.drawLine(round(c.x()), round(c.y() - r * 1.18), round(c.x()), round(c.y()))

    def _paint_gangs(self, p: QPainter, base: str, bg: str) -> None:
        gangs = self.state.card.gangs
        rects = self._gang_rects()
        if not gangs:
            return
        pal = self._p
        # 关闭态从**卡片当前底色**推一档（和右上角电源键同一套算法）。
        # 原来固定用 pal.bg_soft，可是卡片一旦有键开着就染了淡品类色，
        # 浅色下 bg_soft(#F7F8FA) 反而比染色后的卡底(#F2F4F7)更亮，
        # 关闭的键看着像"浮起来"，和开启的键分不清主次。
        off = theme.blend(bg, pal.text, 0.08)
        bars = use_bars(len(gangs))
        # 竖排只在"并排好几块"时才有意义：单开只有一块 138 宽的按键，
        # 把"按键1"竖着码反而别扭
        vertical = bars and len(gangs) > 1
        size, line_h = self._gang_label_metrics(gangs, rects[0].height()) if vertical else (0, 0.0)
        for g, rect in zip(gangs, rects):
            on = bool(self.state.values.get(g.power.key))
            hot = self._hot == ("gang", g.siid)
            if on:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(base if not hot else theme.blend(base, "#FFFFFF", 0.12)))
            else:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(off if not hot else theme.blend(off, pal.text, 0.06)))
            radius = GANG_BAR_RADIUS if bars else theme.RADIUS_SMALL
            p.drawRoundedRect(rect, radius, radius)
            color = "#FFFFFF" if on else pal.text_soft
            if vertical:
                paint_vertical_label(p, rect, g.label, color, size, line_h)
            else:
                draw_text(
                    p,
                    rect.adjusted(8, 0, -8, 0).toRect(),
                    g.label,
                    size=12,
                    color=color,
                    weight=QFont.Medium,
                    align=Qt.AlignCenter,
                )

    @staticmethod
    def _gang_label_metrics(gangs, bar_h: float) -> tuple[int, float]:
        """挑一个**整张卡统一**的竖排字号：从 12 往小试，装得下最长的标签就停。

        逐键各算各的会让"左键"(2 字) 用 12px、"餐厅吊灯"(4 字) 用 10px，
        并排一看就是三个大小不一的标签。
        """
        inner = bar_h - VERT_PAD * 2
        labels = [g.label for g in gangs] or [""]
        for size in range(GANG_LABEL_PX, GANG_LABEL_MIN_PX - 1, -1):
            if max(label_extent(x, size) for x in labels) <= inner:
                return size, label_line_h(size)
        return GANG_LABEL_MIN_PX, label_line_h(GANG_LABEL_MIN_PX)

    # -- 交互 ------------------------------------------------------------
    def _hit(self, pos: QPoint) -> tuple[str, int] | None:
        st = self.state
        if st.card.gangs:
            pt = QPointF(pos)
            for g, rect in zip(st.card.gangs, self._gang_rects()):
                if rect.contains(pt):
                    return ("gang", g.siid)
            return None
        if st.card.control("power") is None:
            return None
        # 命中区放大 8px，鼠标不用瞄准
        if self._power_rect().adjusted(-8, -8, 8, 8).contains(pos):
            return ("power", 0)
        return None

    def interactive_at(self, pos: QPoint) -> bool:
        """这个点是不是电源键 / 多键——落在上面时不要开始拖动。"""
        return self._hit(pos) is not None

    def set_drag_placeholder(self, on: bool) -> None:
        """拖动排序时把自己画成占位框，同时把内容藏起来。"""
        on = bool(on)
        if self._placeholder == on:
            return
        self._placeholder = on
        if self._body is not None:
            self._body.setVisible(not on)
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._press_on_control = False
        if event.button() == Qt.LeftButton:
            hit = self._hit(event.position().toPoint())
            if hit is not None:
                self._press_on_control = True
                st = self.state
                if hit[0] == "power":
                    ctl = st.card.control("power")
                    if ctl and ctl.prop:
                        self.toggled.emit(st.did, ctl.prop.siid, ctl.prop.piid)
                else:
                    gang = next((g for g in st.card.gangs if g.siid == hit[1]), None)
                    if gang:
                        self.toggled.emit(st.did, gang.power.siid, gang.power.piid)
                event.accept()
                return
            # 卡片主体不再在按下时打开详情：先让墙面的拖动排序有机会接管。
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            if self._press_on_control:
                self._press_on_control = False
                event.accept()
                return
            # 按下在卡片上、松开仍在卡片内才算一次点击；拖出去松手是取消
            if not self._placeholder and self.rect().contains(event.position().toPoint()):
                self.clicked.emit(self.state.did)
                event.accept()
                return
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        hit = self._hit(event.position().toPoint())
        if hit != self._hot:
            self._hot = hit
            self.update()
        self.setCursor(Qt.PointingHandCursor if hit else Qt.ArrowCursor)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        # 右键菜单由墙面统一弹（隐藏 / 取消隐藏 / 打开详情）
        self.menu_requested.emit(self.state.did, event.globalPos())

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        # 鼠标从卡片移到 body 里的滑条时，父卡片**也会**收到 leave。光标其实
        # 还在卡片矩形内，这时保持 hover 底色，别在滑条边缘来回闪一下。
        self._hot = None
        if self._body is not None and self.rect().contains(
            self.mapFromGlobal(QCursor.pos())
        ):
            self.update()
            return
        self._hover = False
        self.update()


def card_for(state: DeviceState, icons: IconProvider, palette: theme.Palette) -> DeviceCard:
    return DeviceCard(state, icons, palette)


__all__ = ["DeviceCard", "card_for"]
