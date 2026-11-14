# SPDX-License-Identifier: GPL-3.0-or-later
"""常驻服务层：把纯逻辑引擎、全局热键、云端发送、Qt 定时器接起来。

**必须挂在主窗口 / Store 这一级常驻对象上**：``exec_with_dim_click`` 关掉弹窗会
``deleteLater()``，规则编辑态、冷却表、热键注册表挂在对话框实例上就会关一次窗全丢
（``docs/reference/automation-hotkeys-and-ui.md`` 3.1 第 5 条）。

线程模型有**两条**，由 :data:`DEFAULT_TRANSPORT` / 环境变量
``MIHOME_AUTOMATION_TRANSPORT`` 选：

* ``"serial"``（**定案默认**）—— 原名机制：动作丢进 ``jobs.SerialWorker``，跟轮询读、
  写属性**共用同一条线程**，发完等云端回执才发下一条。
* ``"channel"`` —— **实验性、未启用**：``core/channel.Channel``，``post()`` 发出去就返回，
  回执由它自己的读线程带回来，主线程按 16 ms 的节奏 ``drain()``。

**为什么默认是 serial（11-14 22:0x 用户实测定案）**：他之前报的"唤醒 → 发虚拟事件之间
有明显延迟"，真因是**唤醒这个动作本身**让音箱进聆听态，后续到音箱的指令会等到聆听结束
才执行——那是**设备侧**的顺序，换哪条通道都一样。serial 下他复测效果满意，所以不拿
新机制做默认。``Channel`` 那套完整保留：哪天真的出现"本程序内部排队"导致的卡顿
（例如一串里全是唤醒这类），改 :data:`DEFAULT_TRANSPORT` 一个词就切回去。

两条路都遵守同一条规矩：**后台线程里绝不 emit Qt 信号、绝不碰 UI**。
延迟用成员 ``QTimer`` 串动作队列，不在主线程 ``sleep``。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer, Signal

from .. import miot_action
from ..api import explain
from ..channel import SENT, UNCONFIRMED, Channel
from ..jobs import PRIORITY_CONTROL
from ..tree import parse_virtual_did
from . import action_catalog
from .engine import AutomationEngine, LogEntry
from .hotkeys_win import HotkeyRegistry
from .logstore import MAX_ENTRIES, LogStore
from .pc_state import PcState
from .rules import Action, RuleSet

#: 收发器的结果队列多久取一次。跟 ``jobs`` 那条 worker 的 drain 同一个节奏。
DRAIN_MS = 16

#: 自动化动作走哪条收发通道。**默认 ``serial``（原名机制），11-14 22:0x 定案**：
#: 用户实测确认"唤醒 → 发虚拟事件"之间那几秒是**唤醒这个动作本身**造成的（音箱进聆听态，
#: 之后的指令要等聆听结束），不是本程序内部排队——serial 下复测效果满意，于是不启用新机制。
#: ``channel`` 那套代码**完整保留**（实验性），改这一个词就切回去。
#:
#: 也可以用环境变量 ``MIHOME_AUTOMATION_TRANSPORT=serial|channel`` 覆盖，
#: 不用改代码就能对比两次（``run.bat`` 前 ``set`` 一下即可）。
DEFAULT_TRANSPORT = "serial"

_TRANSPORTS = ("serial", "channel")


def _pick_transport(override: str | None = None) -> str:
    """定下走哪条通道：显式参数 > 环境变量 > :data:`DEFAULT_TRANSPORT`。"""
    for raw in (override, os.environ.get("MIHOME_AUTOMATION_TRANSPORT")):
        want = (raw or "").strip().lower()
        if want in _TRANSPORTS:
            return want
        if want:  # 写错了就说清楚，别默默退回默认值
            print(f"[automation] 未知传输方式 {want!r}，已忽略（可用：{_TRANSPORTS}）")
    return DEFAULT_TRANSPORT


class AutomationService(QObject):
    """一条规则集 + 一台引擎 + 一张热键表 + 一条动作收发通道。窗口只是它的视图。

    日志除了留在引擎内存里，每条都追加到 ``<data>/automation_log.jsonl``
    （:class:`logstore.LogStore`），启动时读回来——关掉程序再打开还能翻到上次那一串。
    """

    log_added = Signal(object)        # LogEntry
    log_cleared = Signal()           # 日志被整体清空（内存 + 文件）
    status_changed = Signal()        # 热键注册状态变了
    rules_changed = Signal()         # 规则增删改（页面刷新列表）

    def __init__(
        self,
        *,
        store: Any,
        client: Any,
        worker: Any = None,
        window: Any = None,
        path=None,
        transport: str | None = None,
        log_path: Path | str | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.client = client
        #: ``"serial"`` 走 ``worker``（主窗口那条串行队列）；``"channel"`` 走下面的收发器。
        self.worker = worker
        self.transport = _pick_transport(transport)
        #: 动作专用收发器：**发出去就不等回执**，回执走另一条线程回来（见 ``core/channel``）。
        #: 两条通道都建好它，切回去只是改 :data:`DEFAULT_TRANSPORT` 一个词。serial 模式下
        #: 它只是 5 条空转的 daemon 线程（都阻塞在队列上，不占 CPU、不发包），drain 定时器
        #: 也不启动——代价可以忽略，换来"改一个常量就能 A/B"。
        self.channel = Channel(client.raw())
        self._drain_timer = QTimer(self)
        self._drain_timer.setInterval(DRAIN_MS)
        self._drain_timer.timeout.connect(self.channel.drain)
        if self.transport == "channel":
            self._drain_timer.start()
        self.rules = RuleSet(path)
        self.rules.on_changed = self._on_rules_changed
        #: 日志落盘：``_on_log`` 里每条追一次，启动时 ``restore_log`` 读回来。
        #: 上限和引擎内存同一个数（``MAX_ENTRIES``），三处各写一个 500 迟早对不上。
        self.logstore = LogStore(log_path, max_entries=MAX_ENTRIES)
        self.engine = AutomationEngine(
            self.rules,
            state=PcState(),
            run=self._run,
            call_later=self._call_later,
            on_log=self._on_log,
            # 独立日志窗要能翻到「昨天那一串」：内存留最近 500 条（与落盘文件同口径）
            max_log=MAX_ENTRIES,
        )
        self.engine.restore_log(self.logstore.load())
        self.registry = HotkeyRegistry(window, self)
        self.registry.fired.connect(self._on_hotkey)
        self.registry.status_changed.connect(self.status_changed)
        self._timers: dict[int, QTimer] = {}
        self.sync()

    # -- 生命周期 --------------------------------------------------------
    def start(self) -> None:
        """主窗口构造完、设备列表可用之后调一次：启动后延迟重试一次被占用的组合。"""
        self.sync()
        self.registry.retry_once()

    def close(self) -> None:
        self.engine.close()
        self._drain_timer.stop()
        # 还没回来的回执不再等了：通道线程是 daemon，停掉队列就退出
        self.channel.close()
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

    def action_devices(self, home: str | None = None) -> list[action_catalog.ActionDevice]:
        """某个家庭里能执行动作的设备（中枢 + 音箱）。默认 = 当前家庭。

        ``home`` 收家庭 **id**，与规则归属同一口径；筛哪几台、每台能做什么，全在
        ``action_catalog`` 里判（白名单 + 入参形状），这一层只做"当前家庭"的默认值。
        编辑页的「选择设备」窗和行内功能下拉都读这一份，两处各自筛一遍迟早对不上。
        """
        want = self.current_home if home is None else home
        return action_catalog.collect(getattr(self.store, "devices", {}).values(), want)

    def status_for(self, keys: str) -> str:
        return self.registry.status(keys)

    def test(self, rule_id: str) -> bool:
        """按 id 立即触发一次（不走热键、绕过冷静期）。

        **UI 已不暴露入口**：11-13 用户拍板去掉「测试发送」——那个按钮会先把草稿
        静默写进 ``rules.json`` 再发云端事件，点「取消」也撤不回。保留这个能力是
        给引擎的离线验证 / 将来越明确的入口用，页面不要直接调。
        """
        return self.engine.test(rule_id)

    @property
    def log_path(self) -> Path:
        """日志文件路径（界面里显示 / 打开文件夹用）。"""
        return self.logstore.path

    def clear_log(self) -> None:
        """清空触发日志：**内存和文件一起清**。

        单清内存的话下次启动 ``restore_log`` 又把文件读回来，用户会觉得"清了个寂寞"。
        """
        self.engine.clear_log()
        self.logstore.clear()
        self.log_cleared.emit()

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
        """每条日志：先落盘，再通知界面。

        落盘失败（磁盘满 / 没权限）**不抛**——日志写不进去不该让规则执行中断；
        ``logstore.append`` 自己吞掉 ``OSError`` 并返回 ``False``，这里也不提示：
        界面上一条日志照常出现，只是重启后翻不到，比弹窗打断动作好。
        """
        self.logstore.append(entry)
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

    def _device_by_did(self, did: str) -> Any:
        """按 did 找设备状态。**不按当前家庭过滤**：规则是全局的，家庭只决定界面上
        能选哪些候选；执行时那台设备在哪个家庭都照样能用。"""
        got = getattr(self.store, "devices", {}) or {}
        st = got.get(did)
        if st is not None:
            return st
        for item in got.values():
            node = getattr(item, "node", None)
            if str(getattr(node, "did", "") or "") == did:
                return item
        return None

    def _run(self, act: Action, done: Callable[[str], None]) -> None:
        """执行一条设备动作：找设备 → 按名字查坐标 → 按入参名拼 ``in`` → 交给收发器。

        ``done`` 只收一句中文原因，**空串 = 成功**。但注意引擎已经不用它推进队列了
        （见 ``engine._step``）：这里发出去就返回，回执之后 ``done`` 才会被调一次
        （失败时）。

        判据仍然全在这里做，而且都在**发送之前**：找不到设备 / 没这个功能 / 离线 /
        参数不合法，这些 ``done(reason)`` 是同步调的——引擎据此知道"这一步一个字都
        没发出去"，日志文案也据此区分"没发出去"和"回执报错"。
        """
        st = self._device_by_did(act.did)
        if st is None:
            done(f"找不到设备「{act.device or act.did}」（可能已从家庭移除）")
            return
        feat = act.feature
        if feat is None:
            done(f"未知功能「{act.action or '空'}」")
            return
        spec = getattr(st, "spec", None)
        target = action_catalog.action_for(spec, feat)
        if target is None:
            # 存盘时不校验"这台设备支不支持"（那要读 spec），所以可能到这一步才发现
            done(f"「{act.device or act.did}」没有「{feat.label}」这个功能"
                 f"（型号或固件不支持）")
            return
        if not getattr(st, "online", True):
            done(f"「{act.device or act.did}」当前离线")
            return
        values, why = action_catalog.clean_values(feat, act.args)
        if why:
            done(why)
            return
        in_list = action_catalog.payload_for(feat, target, spec, values)
        siid, aiid = miot_action.ref(target)
        self._post(act, feat, siid, aiid, in_list, done)

    def _post(self, act: Action, feat: action_catalog.Feature, siid: int, aiid: int,
              in_list: Any, done: Callable[[str], None]) -> None:
        """按 :data:`DEFAULT_TRANSPORT` 选一条通道发出去。"""
        if self.transport != "channel":
            self._post_serial(act, feat, siid, aiid, in_list, done)
            return
        did = act.did
        virt = parse_virtual_did(did)
        if virt is not None:  # 子设备 did 要换成父 did + 该路 siid（同 ``Client.run_action``）
            did, siid = virt[0], virt[1]
        item: dict[str, Any] = {"did": did, "siid": siid, "aiid": aiid}
        if in_list:
            # 空列表**不发** `in`：无参动作（唤醒）要的是"没有这个键"，发一个 `[]`
            # 等于说"我带了零个参数"，和 `Client.run_action(ref)` 的形状就不一致了。
            item["in"] = list(in_list)
        # **必须包一层 ``params``**：``/miotspec/*`` 的请求体都是
        # ``{"params": <单条|列表>}``（见上游 ``mijiaAPI/apis.py`` 的
        # ``run_action`` / ``get_prop``：``self._request(uri, {"params": param})``）。
        # 直接把 item 当请求体会被云端判成 ``invalid request``——11-14 实测踩过
        # （唤醒和虚拟事件**全都**这么报，因为错的不是具体动作而是外壳）。
        data: dict[str, Any] = {"params": item}

        def on_reply(code: int, reason: str) -> None:
            if code == UNCONFIRMED:
                # 命令已经到云端了，只是设备没回执（唤醒这类常态）。**不当失败**：
                # 报成失败只会给用户一堆假警报，而他其实听得见音箱有没有动静。
                reason = ""
            if code not in (SENT, UNCONFIRMED):
                done(reason or explain(code))
                return
            if feat.refreshes:
                # 效果可能落在别的设备上（文本指令），只有全量读才看得到
                self.store.schedule_confirm()

        self.channel.post("/miotspec/action", data, label=feat.label, on_reply=on_reply)

    def _post_serial(self, act: Action, feat: action_catalog.Feature, siid: int, aiid: int,
                     in_list: Any, done: Callable[[str], None]) -> None:
        """**原名机制**：丢进 ``jobs.SerialWorker``，和轮询 / 写属性共用那一条线程。

        和 ``Channel`` 的差别只有一处，但正是这轮要 A/B 的那一处：``submit`` 只是入队，
        worker 真正发第二条的时机是**第一条的 HTTP 完整返回之后**。所以如果"唤醒"这次
        往返要 3 秒，后面那条虚拟事件就得等 3 秒才出网——不取决于引擎是否已经推进。

        ``Client.run_action`` 是 v3 就在用、真机验过的同步路径：它自己按名字解析坐标、
        自己处理子设备 did、``value`` 为空时不发 ``in``。
        """
        if self.worker is None:
            done("没有配置执行通道（serial 模式需要显式传 worker）")
            return
        ref = (act.did, siid, aiid)
        # 空入参传 None：``Client.run_action`` 据此**不发** ``in`` 键（无参动作）。
        value = list(in_list) if in_list else None

        def finished(result: Any, error: BaseException | None) -> None:
            if error is not None:
                done(f"后台任务失败：{error}")
                return
            code = int(result or 0)
            if code == 0:
                if feat.refreshes:
                    self.store.schedule_confirm()
                done("")
                return
            # ``Client.run_action`` 自己出错时**固定返回 -1** 并把原因放进 ``last_error``。
            # 注意别写成 ``code < 0``：云端错误码本身就是负数（``-704220025`` 之类），
            # 那样会把"设备报错"误读成上次残留的 ``last_error``。
            detail = getattr(self.client, "last_error", "") if code == -1 else ""
            done(detail or explain(code))

        self.worker.submit(
            lambda: self.client.run_action(ref, value),
            callback=finished,
            priority=PRIORITY_CONTROL,
            label=feat.label,
        )


__all__ = ["AutomationService"]
