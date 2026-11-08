"""自绘卡片的拖动排序手势。

设置页的 ``ui/tree.py`` 用的是 Qt 原生 ``QTreeWidget`` 拖放（``InternalMove``），
由 item view 自己画落点、自己搬 item；主界面卡片是自绘 ``QWidget``，没有 item
view 可以借，只能自己维护一个很薄的手势状态机。两边**只共享语义**（多少像素
算拖动、列表插入怎么算），不强行合并成一套：合并会让 tree 放弃原生的落点绘制
和跨平台手感，得不偿失。

这里的 ``DragGesture`` 不关心 Qt 的事件循环，只回答一个问题：
「这次按下后来变成拖动了吗？」——由 ``WallView.eventFilter`` 驱动。
"""

from __future__ import annotations

from typing import MutableSequence, TypeVar

from PySide6.QtCore import QPoint

#: 超过这个位移才算拖动；低于它仍是一次点击（打开详情）。
DRAG_THRESHOLD = 8

T = TypeVar("T")


class DragGesture:
    """按下 → 移动 → 松开的最小状态机。"""

    def __init__(self, threshold: int = DRAG_THRESHOLD) -> None:
        self._threshold = max(1, int(threshold))
        self._start: QPoint | None = None
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def press(self, pos: QPoint) -> None:
        self._start = QPoint(pos)
        self._active = False

    def move(self, pos: QPoint) -> bool:
        """返回 ``True`` 表示**本次移动刚刚**跨过阈值（只发一次）。"""
        if self._start is None or self._active:
            return False
        if (pos - self._start).manhattanLength() < self._threshold:
            return False
        self._active = True
        return True

    def reset(self) -> None:
        self._start = None
        self._active = False


def move_in_list(seq: MutableSequence[T], index_from: int, index_to: int) -> bool:
    """把 ``index_from`` 的项移到 ``index_to``（按移除后的列表计）。

    ``index_to`` 的语义和 ``list.insert`` 一致：``CardGridLayout.drop_index``
    算出来的就是这个值。越界会被夹到合法范围。
    """
    if not (0 <= index_from < len(seq)):
        return False
    item = seq.pop(index_from)
    index_to = max(0, min(index_to, len(seq)))
    if index_to == index_from:
        seq.insert(index_from, item)
        return False
    seq.insert(index_to, item)
    return True


__all__ = ["DRAG_THRESHOLD", "DragGesture", "move_in_list"]
