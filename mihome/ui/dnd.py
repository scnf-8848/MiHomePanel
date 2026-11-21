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

from typing import MutableSequence, Sequence, TypeVar

from PySide6.QtCore import QObject, QPoint, QRect, QTimer, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

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


def grid_drop_index(pos: QPoint, boxes: Sequence[QRect]) -> int:
    """网格里的插入下标：**先按行带、行内再按中线**。``boxes`` 已排除被拖的那张。

    为什么不是"中线以下就 +1"（旧实现，用户 2026-11-18 报"落位不准"）：

    旧判据对**同一行**的卡片也用 y 判定——鼠标在 B 卡左半边、但 y 已越过 B 的中线，
    B 也算"排在它后面"，于是占位框跳到 B 的右侧。单行 3 列时这个错位刚好一格，
    表现就是"幽灵明明在它左边，落点却在它右边"。

    现在的判据分两步：

    * ``pos.y() >= r.bottom()`` —— 整张卡完全在探针上方，一定在它后面，+1；
    * 探针落在卡的垂直范围里（同一行）→ **只看左右**：越过中线才算排在它后面。

    两行之间的空隙属于上一行末尾（探针在两行之间时，上一行的卡都满足第一条），
    所以不会出现"缝隙里落点乱跳"。

    ``pos`` 用**被拖卡片的中心**而不是鼠标点：抓取点在哪是随机的，同一位置换个
    抓法就会得出不同落点；按卡片中心判定才是"这张卡要去哪"的直觉。
    """
    idx = 0
    for r in boxes:
        if pos.y() >= r.bottom():
            idx += 1
        elif pos.y() > r.top() and pos.x() > r.center().x():
            idx += 1
    return idx


class DragGuard(QObject):
    """拖动途中的"外部中断"看门狗。

    手写拖动状态机依赖 ``MouseMove`` / ``MouseButtonRelease`` 回到被拖的卡片，
    但两种常见操作会让这两个事件**永远不来**：

    * 切到别的程序（Alt+Tab、截图快捷键、锁屏）—— 窗口失活，Qt 不再派发鼠标事件；
    * 在窗口外松手（比如拖到屏幕边缘后切走再回来）。

    结果就是幽灵窗和虚线占位框留在画面上（用户 2026-11-18 报的第二个 bug），
    而且下一次按下时状态已经是脏的。

    这里给两条判据，任何一条成立就发 ``aborted``：

    * ``QApplication.applicationStateChanged`` 变成非 Active（覆盖 Alt+Tab / 截图工具）；
    * 定时检查 ``mouseButtons``——左键已经松开（覆盖"松手事件丢了"）。

    为什么不用 ``WindowDeactivate``：那是宿主窗口自己的事件，两处宿主一个在
    ``QScrollArea`` 里、一个在页面里，都要转一手；应用级状态变化覆盖同样的场景，
    而且捕获的是"整个应用都不在前台"这一个语义。
    """

    aborted = Signal(str)

    def __init__(self, parent: QObject | None = None, interval_ms: int = 150) -> None:
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(max(30, int(interval_ms)))
        self._timer.timeout.connect(self._tick)
        self._hooked = False
        self._fired = False

    def start(self) -> None:
        self._fired = False
        app = QApplication.instance()
        if app is not None and not self._hooked:
            app.applicationStateChanged.connect(self._on_app_state)
            self._hooked = True
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        app = QApplication.instance()
        if app is not None and self._hooked:
            try:
                app.applicationStateChanged.disconnect(self._on_app_state)
            except (RuntimeError, TypeError):
                # 应用正在退出时连接可能已经断了，不该在这里再抛一次
                pass
            self._hooked = False

    def _on_app_state(self, state: Qt.ApplicationState) -> None:
        if state != Qt.ApplicationActive:
            self._fire("应用切到后台")

    def _tick(self) -> None:
        # 已经停表就别再动：stop() 与队列里还没派发的那个 timeout 之间有竞态，
        # 拖着不放的定时器会在收尾之后又发一次 aborted（宿主已经清过一遍了）
        if not self._timer.isActive():
            return
        if not (QGuiApplication.mouseButtons() & Qt.LeftButton):
            self._fire("左键已松开（漏了 release 事件）")

    def _fire(self, reason: str) -> None:
        """只发一次，并立刻停表：宿主收到 ``aborted`` 后会自己收尾。"""
        if self._fired:
            return
        self._fired = True
        self.stop()
        self.aborted.emit(reason)


__all__ = ["DRAG_THRESHOLD", "DragGesture", "DragGuard", "grid_drop_index", "move_in_list"]
