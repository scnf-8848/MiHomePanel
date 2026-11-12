# SPDX-License-Identifier: GPL-3.0-or-later
"""常驻服务层：把纯逻辑引擎、全局热键、云端发送、Qt 定时器接起来。

**必须挂在主窗口 / Store 这一级常驻对象上**：``exec_with_dim_click`` 关掉弹窗会
``deleteLater()``，规则编辑态、冷却表、热键注册表挂在对话框实例上就会关一次窗全丢
（``docs/reference/automation-hotkeys-and-ui.md`` 3.1 第 5 条）。

线程模型沿用 ``core/jobs.py``：云端请求全部丢进 ``SerialWorker`` 串行队列，结果由主线程
``drain()`` 回调；**worker 里绝不 emit Qt 信号、绝不碰 UI**。延迟用成员 ``QTimer``
串动作队列，不在主线程 ``sleep``。
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer, Signal

from ..jobs import PRIORITY_CONTROL
from .engine import AutomationEngine, LogEntry
from .hotkeys_win import HotkeyRegistry
from .pc_state import PcState
from .rules import RuleSet
from .sender import VirtualEventSender


class AutomationService(QObject):
    """一条规则集 + 一台引擎 + 一张热键表。窗口只是它的视图。"""

    log_added = Signal(object)        # LogEntry
    status_changed = Signal()        # 热键注册状态变了
    rules_changed = Signal()         # 规则增删改（页面刷新列表）

    def __init__(
        self,
        *,
        store: Any,
        client: Any,
        worker: Any,
        window: Any = None,
        path=None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.rules = RuleSet(path)
        self.sender = VirtualEventSender(
            lambda: client.raw(), lambda: store.devices.values())
        self.engine = AutomationEngine(
            self.rules,
            state=PcState(),
            emit=self._emit,
            call_later=self._call_later,
            on_log=self._on_log,
            # 独立日志窗要能翻到「昨天那一串」；日志只在内存，放宽上限几乎不花内存
            max_log=500,
        )
        self.registry = HotkeyRegistry(window, self)
        self.registry.fired.connect(self._on_hotkey)
        self.registry.status_changed.connect(self.status_changed)
        self.worker = worker
        self._timers: dict[int, QTimer] = {}
        self.rules.on_changed = self._on_rules_changed
        self.sync()

    # -- 生命周期 --------------------------------------------------------
    def start(self) -> None:
        """主窗口构造完、设备列表可用之后调一次：启动后延迟重试一次被占用的组合。"""
        self.sync()
        self.registry.retry_once()

    def close(self) -> None:
        self.engine.close()
        for timer in list(self._timers.values()):
            timer.stop()
        self._timers.clear()
        self.registry.release_all()

    # -- 查询（给页面） --------------------------------------------------
    def status_for(self, keys: str) -> str:
        return self.registry.status(keys)

    def target_label(self) -> str:
        target, why = self.sender.resolve()
        return target.label if target is not None else why

    def test(self, rule_id: str) -> bool:
        """按 id 立即触发一次（不走热键、绕过冷静期）。

        **UI 已不暴露入口**：11-13 用户拍板去掉「测试发送」——那个按钮会先把草稿
        静默写进 ``rules.json`` 再发云端事件，点「取消」也撤不回。保留这个能力是
        给引擎的离线验证 / 将来越明确的入口用，页面不要直接调。
        """
        return self.engine.test(rule_id)

    # -- 内部 ------------------------------------------------------------
    def sync(self) -> None:
        self.registry.sync(self.rules.enabled_keys())
        self.status_changed.emit()

    def _on_rules_changed(self) -> None:
        self.sync()
        self.rules_changed.emit()

    def _on_hotkey(self, keys: str) -> None:
        self.engine.trigger(keys)

    def _on_log(self, entry: LogEntry) -> None:
        self.log_added.emit(entry)

    def _call_later(self, ms: int, fn: Callable[[], None]) -> None:
        """成员 QTimer，Python 引用必须留住，否则 GC 掉后延迟永远不来。"""
        timer = QTimer(self)
        timer.setSingleShot(True)
        key = id(timer)
        self._timers[key] = timer

        def fire() -> None:
            self._timers.pop(key, None)
            fn()

        timer.timeout.connect(fire)
        timer.start(max(0, int(ms)))

    def _emit(self, name: str, done: Callable[[str], None]) -> None:
        """主线程解析中枢 → 后台线程发请求 → 主线程回调继续动作队列。"""
        target, why = self.sender.resolve()
        if target is None:
            done(why)
            return

        def finished(result: Any, error: BaseException | None) -> None:
            if error is not None:
                done(f"后台任务失败：{error}")
                return
            ok, text = result if isinstance(result, tuple) else (False, "返回结果格式不对")
            done("" if ok else text)

        self.worker.submit(
            self.sender.call, (target, name),
            callback=finished, priority=PRIORITY_CONTROL, label="发虚拟事件",
        )


__all__ = ["AutomationService"]
