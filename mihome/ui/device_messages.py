# SPDX-License-Identifier: GPL-3.0-or-later
"""某台设备的消息记录（**只读**）。

用户 2026-11-23 定的位置：设备详情页左下角，原来那颗「隐藏此设备」的位置——
"这台设备最近发生过什么"是看设备时会想顺手确认的事，藏进首页卡片的右键菜单里
没人找得到。隐藏设备仍然可以在首页卡片右键完成，所以那颗按钮让位。

数据走 ``/v2/message/v2/list``（``Store.fetch_device_messages``）：**只有最近一批**。
``type`` 必填、``timestamp`` 只认 0、没有游标，所以"更早的"这页拿不到，脚注照实写。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout, QWidget

from ..core.messages import Message
from ..core.store import DeviceState, Store
from . import theme
from .automation_style import tab_focus_only
from .chrome import (
    SHADOW_PAD,
    TitleBar,
    WindowFrame,
    shadow_alpha,
    surface_bg,
    surface_border,
)
from .messages_popup import MessageList

#: 内容宽度（不含外圈阴影留白）。比浮层（400）宽：这里是"专门看一台设备"，
#: 一行能放下"标题 + 时间 + 设备 · 类型"。
WIDTH = 520
#: 列表可视高度。设备的历史记录条数本来就不多（实测最近 6 条），给到 360 足够，
#: 再多的部分自己滚。
LIST_H = 360
PAD_X = 22
PAD_TOP = 14
PAD_BOTTOM = 16


class DeviceMessagesDialog(QDialog):
    """一台设备的消息记录。只读，没有任何写操作。"""

    def __init__(
        self,
        store: Store,
        state: DeviceState,
        palette: theme.Palette,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._state = state
        self._p = palette
        self._did = state.did
        #: 窗口关掉以后 worker 里的请求可能才回来：没有它就会去碰已销毁的控件
        self._alive = True
        self._asked = False

        self.setWindowTitle(f"{state.name} · 消息记录")
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
        self.setStyleSheet(
            "QDialog{background:transparent;}"
            f"QLabel#MsgSum{{color:{palette.text_mute};font-size:11px;}}"
            f"QLabel#MsgTitle{{color:{palette.text};font-size:13px;font-weight:600;}}"
            f"QLabel#MsgKind{{color:{palette.text_mute};font-size:11px;}}"
            f"QLabel#MsgBody{{color:{palette.text_soft};font-size:12px;}}"
            f"QLabel#MsgMeta{{color:{palette.text_mute};font-size:11px;}}"
            f"QLabel#MsgHint{{color:{palette.text_mute};font-size:12px;}}"
            "QScrollArea{border:none;background:transparent;}"
            "QWidget#MsgCanvas{background:transparent;}"
        )

        outer = QVBoxLayout(self._frame.body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(
            TitleBar(
                "消息记录",
                palette,
                subtitle=state.name,
                buttons=("close",),
                variant="dialog",
                frame=self._frame,
            )
        )

        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(PAD_X, PAD_TOP, PAD_X, PAD_BOTTOM)
        col.setSpacing(10)
        outer.addWidget(body)

        self.list = MessageList(palette, body, fixed_height=LIST_H)
        col.addWidget(self.list)

        self.note = QLabel("只读 · 云端只保留这台设备最近一批记录，更早的请在米家 App 里看")
        self.note.setObjectName("MsgHint")
        self.note.setWordWrap(True)
        content_w = WIDTH - 2 * PAD_X
        note_h = self.note.heightForWidth(content_w) or self.note.sizeHint().height()
        # 钉死高度：否则布局拿 minimumSizeHint（按最窄宽度折行算）当最小高度，
        # 窗口下面会白出一大块（浮层那边同一个坑）
        self.note.setFixedHeight(note_h)
        col.addWidget(self.note)
        body.setFixedHeight(PAD_TOP + LIST_H + col.spacing() + note_h + PAD_BOTTOM)

        outer.activate()
        pad2 = 2 * SHADOW_PAD
        self.resize(WIDTH + pad2, outer.sizeHint().height() + pad2)
        self.setFixedWidth(self.width())
        tab_focus_only(self)
        self.finished.connect(self._on_finished)

    # -- 数据 ------------------------------------------------------------
    def _on_finished(self, _code: int) -> None:
        # exec() 返回（含 Esc / 点外侧）时触发；之后回调一律丢弃
        self._alive = False

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # 放在 showEvent 而不是构造里：构造完到真正显示之间还有一层动画，
        # 那时候发请求，回调可能在窗口还没画出来时就到了
        if not self._asked:
            self._asked = True
            self.list.set_loading()
            self._store.fetch_device_messages(self._did, self._on_loaded)

    def _on_loaded(self, items: list[Message] | None, error: BaseException | None) -> None:
        if not self._alive:
            return
        try:
            if error is not None:
                self.list.set_error(str(error))
            else:
                self.list.set_messages(items or [])
                if not items:
                    self.list.summary.setText("还没有记录")
        except RuntimeError:
            # C++ 侧已经销毁（关闭与回调撞在同一帧）：什么都不做
            self._alive = False


def open_device_messages(
    store: Store,
    state: DeviceState,
    palette: theme.Palette,
    parent: QWidget | None = None,
) -> None:
    """模态打开（详情页里点「消息记录」走这条）。"""
    DeviceMessagesDialog(store, state, palette, parent).exec()


__all__ = ["DeviceMessagesDialog", "open_device_messages"]
