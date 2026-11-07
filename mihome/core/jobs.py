"""单线程串行执行器。

上游 mijiaAPI 内部是一个 ``requests.Session`` 加 token 状态，**不是线程安全的**。
参考项目踩过这个坑：并发调用会互相踩 token 刷新和 session 状态。所以所有网络
调用都排进同一条队列，一个线程串行执行。

设计上有两条约束：

1. **子线程绝不直接回调 UI。** worker 只把结果塞进结果队列，由主线程定时
   ``drain()`` 取出执行回调。跨线程 emit Qt 信号在参考项目里直接导致过崩溃。
2. **优先级插队。** 用户点开关必须立刻发出去，不能被正在排队的 60 个轮询请求
   堵在后面。所以队列按 ``(priority, seq)`` 排序，``CONTROL`` 永远在 ``DATA`` 前。

用法::

    worker = SerialWorker()
    worker.submit(fn, args, callback=on_done, priority=PRIORITY_CONTROL)
    # 主线程 QTimer 每 16ms 调一次
    worker.drain()
"""

from __future__ import annotations

import itertools
import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

PRIORITY_CONTROL = 0  # 用户点击：插队
PRIORITY_DATA = 1  # 轮询
PRIORITY_IDLE = 2  # 抓图标、查名字等后台杂务

Callback = Callable[..., None]


@dataclass(order=True)
class Job:
    priority: int
    seq: int
    fn: Callable[..., Any] = field(compare=False)
    args: tuple = field(compare=False, default=())
    kwargs: dict = field(compare=False, default_factory=dict)
    callback: Callback | None = field(compare=False, default=None)
    label: str = field(compare=False, default="")


@dataclass
class Done:
    job: Job
    result: Any = None
    error: BaseException | None = None
    elapsed: float = 0.0


class SerialWorker:
    def __init__(self, *, name: str = "mihome-net") -> None:
        self._jobs: queue.PriorityQueue[Job] = queue.PriorityQueue()
        self._done: queue.Queue[Done] = queue.Queue()
        self._seq = itertools.count()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._stop = threading.Event()
        self._started = False
        self.pending = 0  # 仅供展示
        self.last_error: str = ""

    # -- 生命周期 --------------------------------------------------------
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._jobs.put(Job(-1, next(self._seq), lambda: None))

    # -- 投递 ------------------------------------------------------------
    def submit(
        self,
        fn: Callable[..., Any],
        args: tuple = (),
        *,
        kwargs: dict | None = None,
        callback: Callback | None = None,
        priority: int = PRIORITY_DATA,
        label: str = "",
    ) -> None:
        if not self._started:
            self.start()
        self.pending += 1
        self._jobs.put(
            Job(priority, next(self._seq), fn, args, kwargs or {}, callback, label)
        )

    # -- worker ----------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            if job.priority < 0:
                break
            t0 = time.perf_counter()
            done = Done(job=job)
            try:
                done.result = job.fn(*job.args, **job.kwargs)
            except BaseException as exc:  # noqa: BLE001  worker 绝不能死
                done.error = exc
                self.last_error = f"{job.label or job.fn.__name__}: {exc}"
                traceback.print_exc()
            done.elapsed = time.perf_counter() - t0
            self.pending = max(0, self.pending - 1)
            self._done.put(done)

    # -- 主线程 ----------------------------------------------------------
    def drain(self, limit: int = 64) -> int:
        """在主线程执行已完成的回调。返回处理的条数。"""
        n = 0
        while n < limit:
            try:
                done = self._done.get_nowait()
            except queue.Empty:
                break
            n += 1
            cb = done.job.callback
            if cb is None:
                continue
            try:
                if done.error is not None:
                    cb(None, done.error)
                else:
                    cb(done.result, None)
            except Exception:  # noqa: BLE001  UI 回调出错不能拖垮 drain
                traceback.print_exc()
        return n

    @property
    def idle(self) -> bool:
        return self.pending == 0

    def clear_pending(self) -> None:
        """丢弃还没执行的任务（切换家庭 / 重连时用）。"""
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                return
            self.pending = max(0, self.pending - 1)
            if job.callback:
                try:
                    job.callback(None, RuntimeError("cancelled"))
                except Exception:  # noqa: BLE001
                    pass


__all__ = [
    "Done",
    "Job",
    "PRIORITY_CONTROL",
    "PRIORITY_DATA",
    "PRIORITY_IDLE",
    "SerialWorker",
]
