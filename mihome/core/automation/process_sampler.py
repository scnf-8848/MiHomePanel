# SPDX-License-Identifier: GPL-3.0-or-later
"""进程名采样 worker：在**自己的 QThread** 里定时读进程表，结果用信号送回主线程。

为什么单独一个线程（2026-11-19 实测后定）：``CreateToolhelp32Snapshot`` 在本机
405 个进程时要 ~6.7 ms，原先 ``QTimer`` 挂在 Qt 主线程，每秒卡掉一帧的 40%。
挪到 worker 后主线程一次采样都不做，CPU 总量不变但不再有周期卡顿；
数据与排除 WMI 的理由见 ``docs/reference/process-poll-vs-wmi.md``。

线程模型（与 ``service.py`` 顶部那条"后台线程不 emit 信号"的规矩不冲突）：

* 这里是 **Qt 线程**（``QThread`` + ``QObject``），``Signal.emit`` 由 Qt 自动排队到
  接收对象所在线程，是 Qt 官方支持的跨线程通信；那条规矩管的是 ``jobs`` /
  ``channel`` 那几条**非 Qt 线程**，它们不能直接 emit。
* worker 只做一件事：调用 ``read_names()``（纯 Win32 读，不碰 UI、不写规则），
  把结果（``set[str] | None``）交给主线程。**边沿检测、规则匹配、日志全在主线程**，
  所以 ``_process_seen`` / ``engine`` 没有跨线程竞态。

``AutomationService`` 用 ``_read_process_names`` 作回调而不是持有 ``PcState``：
验证脚本替换 ``svc.pc_state`` 后，worker 下一次采样自动读新的那份。
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal, Slot

#: 采样结果：``set[str]`` = 读到的 exe 名集合；``None`` = 读不到（非 Windows / API 失败）。
Sample = set[str] | None


class ProcessSampler(QObject):
    """定时采样进程名，``sampled`` 信号跨线程送到主线程。

    对象本身在构造线程（主线程）创建，必须 ``moveToThread()`` 之后再调
    :meth:`start_polling` —— ``QTimer`` 属于创建它的线程，不能在原线程建好再搬过去。
    """

    sampled = Signal(object)     # Sample
    request_sample = Signal()    # 主线程 → worker：立即采一次（规则刚改完建基线用）

    def __init__(
        self,
        read_names: Callable[[], Sample],
        interval_ms: int,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._read_names = read_names
        self._interval_ms = max(1, int(interval_ms))
        self._timer: QTimer | None = None

    @Slot()
    def start_polling(self) -> None:
        """在 worker 线程里建表并启动；连到 ``QThread.started``，不主动跨线程调。

        **同一个 sampler 会被反复启动**：规则删光时 ``quit()``、之后又加回进程触发就
        再 ``start()``，而 ``started`` 每次都会走到这里。旧的 ``QTimer`` 必须先停掉
        （2026-11-19 实测：不加这一步，每启停一轮就多攒一个 QTimer 子对象；它们虽然
        不会重复采样，但对象只增不减）。
        """
        if self._timer is not None:
            self._timer.stop()
            self._timer.deleteLater()
        self._timer = QTimer(self)
        self._timer.setInterval(self._interval_ms)
        self._timer.timeout.connect(self.sample_once)
        self._timer.start()

    @Slot()
    def sample_once(self) -> None:
        """采一次并 emit。异常在 worker 内部吞掉：一次读失败不该让线程死掉。"""
        try:
            names = self._read_names()
        except Exception:  # noqa: BLE001  快照偶尔失败，当"读不到"交给主线程不判边沿
            names = None
        self.sampled.emit(names)


__all__ = ["ProcessSampler"]
