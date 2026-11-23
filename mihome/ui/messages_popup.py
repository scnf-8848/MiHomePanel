# SPDX-License-Identifier: GPL-3.0-or-later
"""消息中心浮层（**只读**）。

用户 2026-11-23 定的形态：鼠标停在顶栏「消息」胶囊上就浮出一张列表，移开就收。
不用点、不当弹窗——它只是"那颗胶囊的展开态"，和悬停提示同一档次的东西，所以：

- **不抢焦点**（``WA_ShowWithoutActivating`` + ``Qt.Tool``）：停在上面滚轮能滚列表，
  但不会把主窗口的焦点抢走，也不会进任务栏；
- **不压暗主窗口、不占摄像头播放位**：它不是"一层页面"，看过就走；
- 数据在第一次浮出时读一次，之后 ``CACHE_MS`` 内复用；切家庭、超过缓存时间再读。

口径（用户定）：**只读** —— 看得到，没有标已读、没有删除，也不显示未读数
（这里从不标已读，那个数字只会永远是"全部未读"）。顶栏搜索框同一个决定里撤掉了：
几十台设备用不上搜索，位置留给这条消息入口。

新形态下能看到的仍然只是云端"最近几条"（``typelist`` 没有游标，实测 5 条设备
消息 + 每类 1 条）；单台设备的历史和场景日志在米家 App 里，这里的脚注照实写。
"""

from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLayout,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core import messages as model
from ..core.messages import Message
from ..core.store import Store
from . import theme
from .chrome import surface_bg, surface_border
from .widgets import HLine

#: 浮层宽度。再宽正文一行长得像篇文章，再窄"设备消息 · 小米智能门锁 4 Pro"会折行。
WIDTH = 400
#: 内容四周的留白。
PAD = 16
#: 列表最多这么高，超出的滚动。用户 2026-11-23 反馈"只能看到 2 条"，从 380 抬到 520：
#: 真机上一条带正文的消息约 60-75px，520 能放进 6-7 条，正好是云端的全部记录。
#: 真正生效的上限还要看屏幕（见 ``MessagePopup._place``），这里只是"再高就没意义"。
LIST_MAX_H = 520
#: 列表最矮这么高（只有一两条时不要缩成一条缝）。
LIST_MIN_H = 110
#: 摘要那行（"N 条消息"）的高度。用户要求"别占那么多"：字号降到 11、上下不留白，
#: 只当一行小注解，焦点全给列表。
SUM_H = 15
#: 胶囊底边到浮层的距离。贴一点才像"这颗胶囊展开了"。
GAP = 6
#: 移开多久收起。留出"从胶囊挪进浮层"要穿过的那 6px 的时间。
HIDE_MS = 260
#: 缓存多久。消息不是秒级变化的东西，一分钟内反复浮出不再请求。
CACHE_MS = 60_000


def _qss(p: theme.Palette) -> str:
    return (
        f"QLabel#MsgTitle{{color:{p.text};font-size:13px;font-weight:600;}}"
        f"QLabel#MsgKind{{color:{p.text_mute};font-size:11px;}}"
        f"QLabel#MsgBody{{color:{p.text_soft};font-size:12px;}}"
        f"QLabel#MsgMeta{{color:{p.text_mute};font-size:11px;}}"
        f"QLabel#MsgSum{{color:{p.text_mute};font-size:11px;}}"
        f"QLabel#MsgHint{{color:{p.text_mute};font-size:12px;}}"
        "QScrollArea{border:none;background:transparent;}"
        "QWidget#MsgCanvas{background:transparent;}"
    )


def _row(msg: Message, palette: theme.Palette) -> QWidget:
    """一条消息：标题 + 时间 / 类型 · 设备 / 正文。

    标题占最重的一行——一屏里要扫的是"发生了什么"；时间和设备是注脚。
    标题为空（云端偶尔只给 ``content``）时退成类型名，不能让那一行是空白。
    """
    row = QWidget()
    col = QVBoxLayout(row)
    col.setContentsMargins(0, 8, 0, 8)
    col.setSpacing(3)

    head = QHBoxLayout()
    head.setSpacing(8)
    title = QLabel(msg.title or msg.label)
    title.setObjectName("MsgTitle")
    title.setWordWrap(True)
    head.addWidget(title, 1)
    when = QLabel(model.ago(msg.when))
    when.setObjectName("MsgMeta")
    # 顶部对齐：标题换行成两行时，时间不该跟着沉到中间
    head.addWidget(when, 0, Qt.AlignTop)
    col.addLayout(head)

    meta = " · ".join(part for part in (msg.label, msg.device) if part)
    if meta:
        sub = QLabel(meta)
        sub.setObjectName("MsgKind")
        col.addWidget(sub)

    body = model.clip(msg.body)
    if body:
        text = QLabel(body)
        text.setObjectName("MsgBody")
        text.setWordWrap(True)
        col.addWidget(text)
    return row


class MessageList(QWidget):
    """消息列表本体：一行摘要 + 滚动区，加载 / 失败 / 空三种态都在这里。

    不继承任何窗口类：浮层只负责定位与收放，列表自己管内容。

    高度有两种模式：``auto``（浮层用，跟着条数长，夹在 ``min_h`` / ``max_h`` 之间）
    和固定（设备历史对话框用，给多少就是多少）。
    """

    def __init__(
        self,
        palette: theme.Palette,
        parent: QWidget | None = None,
        *,
        fixed_height: int | None = None,
    ) -> None:
        super().__init__(parent)
        self._p = palette
        self._items: list[Message] = []
        #: 内容高度跟着条数走（浮层）还是钉死（对话框）
        self._fixed_h = fixed_height
        self.min_h = LIST_MIN_H
        self.max_h = LIST_MAX_H

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        # 摘要与列表之间只留一点缝：用户 2026-11-23 说"最上面那行占的高度太大"
        col.setSpacing(6)

        self.summary = QLabel("正在读取…")
        self.summary.setObjectName("MsgSum")
        self.summary.setFixedHeight(SUM_H)
        col.addWidget(self.summary)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFixedHeight(fixed_height or LIST_MIN_H)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.canvas = QWidget()
        self.canvas.setObjectName("MsgCanvas")
        self.rows = QVBoxLayout(self.canvas)
        # 右边留 6px 给滚动条，别让它压住时间列
        self.rows.setContentsMargins(0, 0, 6, 0)
        self.rows.setSpacing(0)
        self.scroll.setWidget(self.canvas)
        col.addWidget(self.scroll)

    # -- 三态 ------------------------------------------------------------
    def _reset(self) -> None:
        while self.rows.count():
            item = self.rows.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # 先摘父再 deleteLater：只 deleteLater 的话它这一帧还留在原位画，
                # 新行压在旧行上（截图里那串重影就是这么来的）
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()

    def _placeholder(self, text: str, color: str = "") -> QLabel:
        lab = QLabel(text)
        lab.setObjectName("MsgHint")
        lab.setWordWrap(True)
        lab.setAlignment(Qt.AlignCenter)
        lab.setMinimumHeight(max(60, (self._fixed_h or self.min_h) - 30))
        if color:
            lab.setStyleSheet(f"color:{color};font-size:12px;")
        return lab

    def set_max_height(self, height: int) -> None:
        """浮层按屏幕余量再收一次上限（``_place`` 每次都会调）。"""
        self.max_h = max(self.min_h, int(height))
        self._fit_scroll()

    def _content_width(self) -> int:
        view = self.scroll.viewport().width()
        return view if view > 40 else WIDTH - 2 * PAD - 6

    def _fit_scroll(self) -> None:
        """让滚动区高度跟着内容走（夹在 ``min_h`` 与 ``max_h`` 之间）。

        **不能只信 ``sizeHint()``**：行是刚 new 出来的，QSS 里的字号要等 polish
        才生效，第一次量出来的高度会偏小（真机上就出现过"只显示两条"）。所以再问
        一次 ``heightForWidth``——按内容宽度算，跟窗口有没有显示过无关。
        """
        if self._fixed_h is not None:
            self.scroll.setFixedHeight(self._fixed_h)
            return
        self.canvas.adjustSize()
        self.rows.activate()
        want = self.rows.sizeHint().height()
        width = self._content_width()
        if self.rows.hasHeightForWidth():
            want = max(want, self.rows.heightForWidth(width))
        self.scroll.setFixedHeight(max(self.min_h, min(want, self.max_h)))

    def refit(self) -> None:
        """重新量一次（浮层显示后由 ``MessagePopup`` 调，纠正第一遍的偏差）。"""
        self._fit_scroll()

    def set_loading(self) -> None:
        self._reset()
        self.summary.setText("正在读取…")
        self.rows.addWidget(self._placeholder("正在向云端要消息…"))
        self.rows.addStretch(1)
        self._fit_scroll()

    def set_error(self, text: str) -> None:
        self._reset()
        self.summary.setText("读取失败")
        # 失败必须和"没有消息"分开：否则用户会以为手机里也没消息，去 App 里白找一圈
        self.rows.addWidget(self._placeholder(f"没有读到消息：{text}", self._p.text_soft))
        self.rows.addStretch(1)
        self._fit_scroll()

    def set_messages(self, items: list[Message]) -> None:
        self._items = list(items)
        self._reset()
        self.summary.setText(model.summarise(self._items))
        if not self._items:
            self.rows.addWidget(
                self._placeholder(
                    "还没有消息。设备完成任务、滤芯到期、场景执行失败时会出现在这里。"
                )
            )
            self.rows.addStretch(1)
            self._fit_scroll()
            return
        for index, msg in enumerate(self._items):
            if index:
                self.rows.addWidget(HLine(self._p.border))
            self.rows.addWidget(_row(msg, self._p))
        self.rows.addStretch(1)
        self._fit_scroll()
        self.scroll.verticalScrollBar().setValue(0)

    def messages(self) -> list[Message]:
        return list(self._items)

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        for i in range(self.rows.count()):
            widget = self.rows.itemAt(i).widget()
            if isinstance(widget, HLine):
                widget.set_color(palette.border)


class MessagePopup(QWidget):
    """顶栏「消息」胶囊的展开态：悬停浮出、移开就收。

    **为什么用 ``Qt.Tool`` 而不是 ``Qt.Popup``**：Popup 一显示就抓鼠标，胶囊会
    立刻收到 Leave，浮层自己把自己关掉。Tool + ``WA_ShowWithoutActivating`` 既能
    悬停滚轮，又不抢焦点、不进任务栏。

    收起只认三件事：光标离开浮层、光标离开胶囊、程序失去激活。**点外面不关**——
    它不接焦点，没有"点外面"这回事，光标一动就走了。
    """

    def __init__(
        self,
        store: Store,
        palette: theme.Palette,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            parent, Qt.Tool | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint
        )
        self._store = store
        self._p = palette
        self._anchor: QWidget | None = None
        self._items: list[Message] = []
        self._home = ""
        self._fetched_at = 0.0
        #: 每次请求 +1。回调只认自己那一轮，避免切家庭时旧结果盖掉新结果。
        self._serial = 0
        self._asked = False
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setStyleSheet(_qss(palette))
        self.setFixedWidth(WIDTH)

        col = QVBoxLayout(self)
        col.setContentsMargins(PAD, PAD - 2, PAD, PAD)
        col.setSpacing(8)
        # 高度由 ``_fit`` 自己算。默认的 SetDefaultConstraint 会让"上一次内容更大"时的
        # 最小高度钉在窗口上：数据晚到、行变短以后窗口缩不回去，下面白一大块。
        col.setSizeConstraint(QLayout.SetNoConstraint)

        self.list = MessageList(palette, self)
        col.addWidget(self.list)

        self.note = QLabel("只读 · 更早的记录与场景日志见米家 App")
        self.note.setObjectName("MsgHint")
        self.note.setWordWrap(True)
        col.addWidget(self.note)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(HIDE_MS)
        self._hide_timer.timeout.connect(self._on_hide_timeout)
        app = QGuiApplication.instance()
        if app is not None:
            app.applicationStateChanged.connect(self._on_app_state)

    # -- 挂到胶囊上 ------------------------------------------------------
    def attach(self, anchor: QWidget) -> None:
        """认下"展开的是哪颗胶囊"：Enter 浮出、Leave 延时收起、胶囊藏了跟着藏。"""
        self._anchor = anchor
        anchor.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self._anchor:
            kind = event.type()
            if kind == QEvent.Enter:
                self._cancel_hide()
                self.show_for(obj)
            elif kind == QEvent.Leave:
                self._schedule_hide()
            elif kind == QEvent.Hide:
                self.hide()
        return False

    # -- 显示 ------------------------------------------------------------
    def show_for(self, anchor: QWidget | None = None) -> None:
        """在胶囊正下方浮出（下面放不下就翻到上面），必要时读一次数据。"""
        if anchor is not None:
            self._anchor = anchor
        anchor = self._anchor
        if anchor is None:
            return
        self._refresh_data()
        self._place(anchor)
        self.show()
        self.raise_()
        # 第一遍量高度可能偏小（行是刚 new 的，QSS 字号要等 polish）：显示后下一轮
        # 事件循环再量一次、再定位一次。用户 2026-11-23 报"只看到两条"就是这个。
        QTimer.singleShot(0, self._refit)

    def _refit(self) -> None:
        if not self.isVisible():
            return
        self.list.refit()
        if self._anchor is not None:
            self._place(self._anchor)

    def _place(self, anchor: QWidget) -> None:
        screen = (
            QGuiApplication.screenAt(anchor.mapToGlobal(anchor.rect().center()))
            or QGuiApplication.primaryScreen()
        )
        area = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        # 先按屏幕余量收一次列表上限：宁可滚动，也别让浮层顶出屏幕下沿
        chrome = 2 * PAD + SUM_H + 12 + 40  # 留白 + 摘要 + 两处间距 + 脚注
        self.list.set_max_height(min(LIST_MAX_H, area.height() - chrome - 60))
        self._fit()
        origin = anchor.mapToGlobal(QPoint(0, 0))
        w, h = self.width(), self.height()
        # 右边对齐胶囊：它是"这颗胶囊的展开"，往右伸出窗口会莫名其妙
        x = origin.x() + anchor.width() - w
        x = max(area.left() + 6, min(x, area.right() - w - 6))
        y = origin.y() + anchor.height() + GAP
        if y + h > area.bottom() - 6:
            y = origin.y() - h - GAP
        self.move(x, max(area.top() + 6, y))

    def _fit(self) -> None:
        """按内容算高度。

        **不用 ``sizeHint()``**：脚注是 word-wrap 的，``sizeHint`` 会按"最宽那行"算，
        多出来的几十像素被布局平摊到列表上下——真机上就是"摘要在中间、上下两大块空地"。
        这里把两块都钉死：列表高度 = 摘要 + 间距 + 滚动区，窗口高度 = 留白 + 列表 +
        间距 + 脚注（脚注按真实宽度量）。
        """
        lay = self.layout()
        if lay is None:
            return
        list_lay = self.list.layout()
        slack = list_lay.spacing() if list_lay is not None else 6
        self.list.setFixedHeight(
            self.list.summary.height() + slack + self.list.scroll.height()
        )
        margins = lay.contentsMargins()
        width = WIDTH - margins.left() - margins.right()
        # ``sizeHint`` 对 word-wrap 的标签会多报一行（实测 16 → 32），只认 heightForWidth
        note_h = self.note.heightForWidth(width) or self.note.sizeHint().height()
        # 还得钉死：不然布局拿它的 minimumSizeHint（按"最窄宽度折行"算，141px）当
        # 最小高度，窗口缩不下去，下面白一大块
        self.note.setFixedHeight(note_h)
        self.resize(
            WIDTH,
            margins.top() + self.list.height() + lay.spacing() + note_h + margins.bottom(),
        )

    # -- 数据 ------------------------------------------------------------
    def _refresh_data(self) -> None:
        home = str(getattr(self._store, "active_home", "") or "")
        fresh = (
            bool(self._items)
            and home == self._home
            and (time.monotonic() - self._fetched_at) * 1000 < CACHE_MS
        )
        if fresh:
            return
        if self._items and home == self._home:
            # 有上一轮的结果：先显示着，后台换新，别让浮层闪一下"正在读取"
            self.list.set_messages(self._items)
        else:
            self.list.set_loading()
        if self._asked:
            return
        self._asked = True
        self._serial += 1
        serial = self._serial
        self._fetched_at = time.monotonic()
        self._store.fetch_messages(
            lambda items, error: self._on_loaded(serial, home, items, error), home=home
        )

    def _on_loaded(
        self,
        serial: int,
        home: str,
        items: list[Message] | None,
        error: BaseException | None,
    ) -> None:
        if serial != self._serial:
            return
        self._asked = False
        if error is not None:
            # 有上一次的结果就留着：一次网络抖动不该把已经看到的东西擦掉
            if not self._items:
                self.list.set_error(str(error))
                self._fit()
            return
        self._items = list(items or [])
        self._home = home
        self._fetched_at = time.monotonic()
        self.list.set_messages(self._items)
        self._fit()
        # 数据到得比"显示后那一轮"晚时，同样再量一次
        QTimer.singleShot(0, self._refit)

    # -- 收放 ------------------------------------------------------------
    def _schedule_hide(self) -> None:
        self._hide_timer.start()

    def _cancel_hide(self) -> None:
        self._hide_timer.stop()

    def _on_hide_timeout(self) -> None:
        pos = QCursor.pos()
        if _inside(self, pos) or _inside(self._anchor, pos):
            return
        self.hide()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._cancel_hide()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._schedule_hide()
        super().leaveEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        self._hide_timer.stop()
        super().hideEvent(event)

    def _on_app_state(self, state: Qt.ApplicationState) -> None:
        if state != Qt.ApplicationState.ApplicationActive:
            self.hide()

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.setStyleSheet(_qss(palette))
        self.list.set_palette(palette)
        self.update()

    # -- 绘制 ------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(surface_border(self._p)), 1))
        p.setBrush(QColor(surface_bg(self._p)))
        p.drawRoundedRect(r, theme.RADIUS_CARD, theme.RADIUS_CARD)
        p.end()


def _inside(widget: QWidget | None, pos: QPoint) -> bool:
    """光标是不是在这块控件（全局坐标）上。控件可能已经销毁，一律当"不在"。"""
    if widget is None:
        return False
    try:
        if not widget.isVisible():
            return False
        return QRect(widget.mapToGlobal(QPoint(0, 0)), widget.size()).contains(pos)
    except RuntimeError:
        return False


__all__ = ["MessageList", "MessagePopup"]
