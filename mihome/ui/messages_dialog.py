# SPDX-License-Identifier: GPL-3.0-or-later
"""消息中心弹窗（**只读**）。

用户 2026-11-23 的试验：先在顶栏放一颗"消息"胶囊，点开看看云端到底给得出什么。
不标已读、不删除——那是写账号数据。

后续形态（用户已经说了，这轮先不做）：用它替掉顶栏那个搜索框，触发方式从"点击弹窗"
改成"鼠标悬停自动浮出一张列表"（自己手里几十台设备，搜索框没什么必要）。
所以列表本体抽成 :class:`MessageList`，它不依赖 ``QDialog``，将来塞进浮层能直接复用。

数据在打开时异步读一次（``Store.fetch_messages``），**不挂轮询**：空闲时零请求，
未读数角标也就没有——那颗角标要一路常驻请求，而现在的目标只是"点开看看"。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core import messages as model
from ..core.messages import Message
from ..core.store import Store
from . import theme
from .automation_style import tab_focus_only
from .chrome import (
    SHADOW_PAD,
    TitleBar,
    WindowFrame,
    exec_with_dim_click,
    shadow_alpha,
    surface_bg,
    surface_border,
)
from .widgets import HLine

#: 弹窗内容宽度（不含 ``SHADOW_PAD`` 那圈阴影留白）。再宽正文一行就长得像篇文章，
#: 再窄"设备 · 类型"会被挤断。
WIDTH = 540
#: 列表可视高度。约 6 行带正文的消息；超出的滚动，窗口本身不再长高。
LIST_H = 360


def _qss(p: theme.Palette) -> str:
    return (
        "QDialog{background:transparent;}"
        f"QLabel#MsgTitle{{color:{p.text};font-size:13px;font-weight:600;}}"
        f"QLabel#MsgKind{{color:{p.text_mute};font-size:11px;}}"
        f"QLabel#MsgBody{{color:{p.text_soft};font-size:12px;}}"
        f"QLabel#MsgMeta{{color:{p.text_mute};font-size:11px;}}"
        f"QLabel#MsgHint{{color:{p.text_mute};font-size:12px;}}"
        "QScrollArea{border:none;background:transparent;}"
        "QWidget#MsgCanvas{background:transparent;}"
    )


class _Dot(QWidget):
    """未读点。6px 的实心圆，用主题强调色——和卡片墙上"未读"是同一个意思。"""

    def __init__(self, palette: theme.Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._p = palette
        self.setFixedSize(6, 6)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self._p.accent))
        p.drawEllipse(QRectF(self.rect()))
        p.end()


def _row(msg: Message, palette: theme.Palette) -> QWidget:
    """一条消息：标题 + 时间 / 类型 · 设备 / 正文。

    标题占最重的一行——一屏里要扫的是"发生了什么"；时间和设备是注脚。
    标题为空（云端偶尔只给 ``content``）时退成类型名，不能让那一行是空白。
    """
    row = QWidget()
    col = QVBoxLayout(row)
    col.setContentsMargins(0, 9, 0, 9)
    col.setSpacing(3)

    head = QHBoxLayout()
    head.setSpacing(7)
    if msg.unread:
        head.addWidget(_Dot(palette), 0, Qt.AlignVCenter)
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

    故意不继承任何窗口类：将来换成"悬停浮层"时直接塞进浮层容器即可。
    """

    def __init__(self, palette: theme.Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._p = palette
        self._items: list[Message] = []

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)

        self.summary = QLabel("正在读取…")
        self.summary.setObjectName("MsgHint")
        col.addWidget(self.summary)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFixedHeight(LIST_H)
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
                widget.deleteLater()

    def _placeholder(self, text: str, color: str = "") -> QLabel:
        lab = QLabel(text)
        lab.setObjectName("MsgHint")
        lab.setWordWrap(True)
        lab.setAlignment(Qt.AlignCenter)
        lab.setMinimumHeight(LIST_H - 40)
        if color:
            lab.setStyleSheet(f"color:{color};font-size:12px;")
        return lab

    def set_loading(self) -> None:
        self._reset()
        self.summary.setText("正在读取…")
        self.rows.addWidget(self._placeholder("正在向云端要消息…"))
        self.rows.addStretch(1)

    def set_error(self, text: str) -> None:
        self._reset()
        self.summary.setText("读取失败")
        # 失败必须和"没有消息"分开：否则用户会以为手机里也没消息，去 App 里白找一圈
        self.rows.addWidget(self._placeholder(f"没有读到消息：{text}", self._p.text_soft))
        self.rows.addStretch(1)

    def set_messages(self, items: list[Message]) -> None:
        self._items = list(items)
        self._reset()
        self.summary.setText(model.summarise(self._items))
        if not self._items:
            self.rows.addWidget(
                self._placeholder("还没有消息。设备完成任务、滤芯到期、场景执行失败时会出现在这里。")
            )
            self.rows.addStretch(1)
            return
        for index, msg in enumerate(self._items):
            if index:
                self.rows.addWidget(HLine(self._p.border))
            self.rows.addWidget(_row(msg, self._p))
        self.rows.addStretch(1)
        self.scroll.verticalScrollBar().setValue(0)

    def messages(self) -> list[Message]:
        return list(self._items)


class MessageDialog(QDialog):
    """消息中心：一把看完就走，没有任何写操作。"""

    def __init__(
        self,
        store: Store,
        palette: theme.Palette,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._p = palette
        # 关掉以后 worker 里那个请求可能才回来；没有这个标志就会去碰已销毁的控件
        self._alive = True
        self._asked = False

        self.setWindowTitle("消息")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self._frame = WindowFrame(
            self,
            resizable=False,
            radius=theme.RADIUS_CARD,
            shadow=SHADOW_PAD,
            shadow_alpha=shadow_alpha(palette),
            fill=lambda: surface_bg(palette),
            border=lambda: surface_border(palette),
        )
        self.setStyleSheet(_qss(palette))

        outer = QVBoxLayout(self._frame.body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(
            TitleBar("消息", palette, buttons=("close",), variant="dialog", frame=self._frame)
        )

        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(22, 14, 22, 16)
        col.setSpacing(10)
        outer.addWidget(body)

        self.list = MessageList(palette, body)
        col.addWidget(self.list)

        note = QLabel(
            "只读 · 列的是云端返回的最近 %d 条，更早的记录请到米家 App 的消息中心看"
            % model.DEFAULT_LIMIT
        )
        note.setObjectName("MsgHint")
        note.setWordWrap(True)
        col.addWidget(note)

        outer.activate()
        hint = self._frame.body.sizeHint()
        pad2 = 2 * SHADOW_PAD
        self.resize(WIDTH + pad2, hint.height() + pad2)
        self.setFixedWidth(self.width())
        tab_focus_only(self)
        # exec() 返回（含 Esc / 点外侧）时触发；之后回调一律丢弃
        self.finished.connect(self._on_finished)

    def _on_finished(self, _code: int) -> None:
        self._alive = False

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # 放在 showEvent 而不是 __init__：构造完到真正显示之间还有一层压暗动画，
        # 那时候发请求，回调可能在窗口还没画出来时就到了。
        if not self._asked:
            self._asked = True
            self.list.set_loading()
            self._store.fetch_messages(self._on_loaded)

    def _on_loaded(self, items: list[Message] | None, error: BaseException | None) -> None:
        if not self._alive:
            return
        try:
            if error is not None:
                self.list.set_error(str(error))
            else:
                self.list.set_messages(items or [])
        except RuntimeError:
            # 兜底：C++ 侧已经销毁（关闭与回调撞在同一帧），什么都不做
            self._alive = False


def open_messages(store: Store, palette: theme.Palette, parent: QWidget | None) -> None:
    """独立开一次（脚本 / 测试用）。

    主窗口走的是 ``MainWindow._run_dialog``——那里还要压暗主窗口、让出摄像头
    播放位，这两件事不该由弹窗自己知道。
    """
    exec_with_dim_click(MessageDialog(store, palette, parent))


__all__ = ["MessageDialog", "MessageList", "open_messages"]
