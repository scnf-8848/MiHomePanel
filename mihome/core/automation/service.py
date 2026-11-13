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

from .. import speaker
from ..geek import supports as geek_supports
from ..jobs import PRIORITY_CONTROL
from .engine import AutomationEngine, LogEntry
from .hotkeys_win import HotkeyRegistry
from .pc_state import PcState
from .rules import Action, RuleSet
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
        self.store = store
        self.rules = RuleSet(path)
        self.rules.on_changed = self._on_rules_changed
        self.sender = VirtualEventSender(
            lambda: client.raw(), lambda: store.devices.values())
        self.engine = AutomationEngine(
            self.rules,
            state=PcState(),
            emit=self._emit,
            say=self._say,
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
    @property
    def current_home(self) -> str:
        """当前在看家庭的**云端 id**（未加载 / 未登录时是空串）。

        规则的 ``home`` 与它同一口径。要显示给用户看时用 :meth:`home_label`，
        **别在内部拿 id 和家庭名做匹配**。
        """
        return str(getattr(self.store, "active_home", "") or "")

    def home_label(self, home_id: str = "") -> str:
        """家庭 id → 显示名。只给界面 / 文案用；匹配一律用 id。"""
        want = self.current_home if not home_id else home_id
        label = getattr(self.store, "home_label", None)
        return str(label(want)) if callable(label) else want

    def home_has_hub(self, home_id: str = "") -> bool:
        """这个家庭有没有中枢网关（「发虚拟事件」的前提）。

        判据直接复用极客版入口那套 ``Store.home_has_hub``（``core.geek`` 的探测
        结论），不另维护型号名单。``home_id`` 收家庭 id，与规则归属同一口径。
        """
        want = self.current_home if not home_id else home_id
        check = getattr(self.store, "home_has_hub", None)
        return bool(check(want)) if callable(check) else False

    def hub_choices(self, home: str = "") -> list[tuple[str, str, bool]]:
        """这个家庭里可作「发虚拟事件」中枢的设备：``[(did, 显示名, 在线), ...]``。

        判据与 :meth:`home_has_hub` 完全相同（``geek.supports`` 的探测结论），
        所以"有没有中枢"和"下拉里列几台"永远是同一份名单。这里只给编辑页做候选；
        真正发送时再按 did 解析 ``siid/aiid``（``sender.resolve_did``），避免在
        规格没加载完时把坐标固化进 rules.json。
        """
        want = self.current_home if not home else home
        store = self.store
        checker = getattr(store, "home_has_hub", None)
        if not callable(checker):
            return []
        out: list[tuple[str, str, bool]] = []
        for st in getattr(store, "devices", {}).values():
            node = getattr(st, "node", None)
            did = str(getattr(st, "did", "") or getattr(node, "did", ""))
            if not did:
                continue
            info = getattr(store, "device_room", None)
            home_id = ""
            if callable(info):
                got = info(did)
                home_id = got[0] if got else ""
            if home_id != want:
                continue
            if not geek_supports(did, str(getattr(node, "model", "") or "")):
                continue
            name = str(getattr(st, "name", "") or getattr(node, "name", "") or did)
            out.append((did, name, bool(getattr(st, "online", True))))
        return sorted(out, key=lambda item: (not item[2], item[1]))

    def status_for(self, keys: str) -> str:
        return self.registry.status(keys)

    def speaker_targets(self, home: str | None = None) -> list[speaker.Target]:
        """某个家庭里能「执行文本指令」的音箱。默认 = 当前家庭。

        ``home`` 收家庭 **id**（与规则归属同一口径）；``speaker.collect_home``
        的筛选语义是**显示名**，所以只在这一层把 id 换成名字——这是用户可见的
        筛选条件，不是规则归属匹配。编辑页的「发音箱指令」候选就从这里来。
        """
        want = self.home_label(self.current_home if home is None else home)
        return speaker.collect_home(getattr(self.store, "devices", {}).values(), want)

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

    def _emit(self, act: Action, done: Callable[[str], None]) -> None:
        """主线程解析中枢 → 后台线程发请求 → 主线程回调继续动作队列。

        规则里存了 ``hub`` 就只认那台（多中枢家庭里用户选的是哪台就发哪台）；
        ``hub`` 为空是旧规则，才回落"上次用过 / 第一台在线中枢"。
        """
        target, why = (self.sender.resolve_did(act.hub) if act.hub
                       else self.sender.resolve())
        if target is None:
            done(why)
            return

        def finished(result: Any, error: BaseException | None) -> None:
            if error is not None:
                done(f"后台任务失败：{error}")
                return
            ok, text = result if isinstance(result, tuple) else (False, "返回结果格式不对")
            # code 0 仅代表云端收下这条 action；极客版本地规则有没有真的执行，
            # 云端不会回报，只能靠设备端的反馈确认（见 sender 模块 docstring）。
            done("" if ok else text)

        self.worker.submit(
            self.sender.call, (target, act.name),
            callback=finished, priority=PRIORITY_CONTROL, label="发虚拟事件",
        )

    # -- 音箱指令 --------------------------------------------------------
    def _speaker_by_did(self, did: str) -> speaker.Target | None:
        """按 did 找音箱。**不按当前家庭过滤**：规则是全局的，家庭只决定界面上
        能选哪些候选；执行时那台音箱在哪个家庭都照样能喊。"""
        return next((t for t in speaker.collect(getattr(self.store, "devices", {}).values())
                     if t.did == did), None)

    def _say(self, act: Action, done: Callable[[str], None]) -> None:
        """执行一条「发音箱指令」。清洗 / 拼入参都在 ``core/speaker.py``。

        和设备动作走同一条 ``Store.run_action``：后台线程发请求、主线程回调。
        ``done`` 只收一句中文原因，空串 = 成功（和 ``emit`` 的语义对齐）。
        """
        target = self._speaker_by_did(act.did)
        if target is None:
            done(f"找不到音箱「{act.name or act.did}」（可能已从家庭移除）")
            return
        if not target.online:
            done(f"「{target.name}」当前离线")
            return
        got = speaker.clean(act.text)
        if got.empty:
            done("指令为空（字符都被清洗掉了）")
            return

        def finished(code: int, reason: str) -> None:
            done("" if code == 0 else (reason or f"错误码 {code}"))

        self.store.run_action(
            target.did, target.siid, target.aiid,
            speaker.directive(target, got.text, act.silent),
            on_done=finished,
            untargeted=True,
        )


__all__ = ["AutomationService"]
