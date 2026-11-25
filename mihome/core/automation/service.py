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

两条动作路都遵守同一条规矩：**后台线程里绝不 emit Qt 信号、绝不碰 UI**。
延迟用成员 ``QTimer`` 串动作队列，不在主线程 ``sleep``。

**唯一的例外是进程采样**（2026-11-19）：``process_sampler.ProcessSampler`` 跑在自己的
``QThread`` 里，定时读一次进程表（本机实测 ~6.7 ms）后 emit 信号回主线程做边沿判定。
那是 Qt 对象之间的跨线程信号（Qt 自动排队），和上面两条**非 Qt 线程**不是一回事：
worker 只读进程名、不碰 UI / 规则 / 引擎，共享状态没有竞态。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from .. import miot_action
from ..api import explain
from ..channel import SENT, UNCONFIRMED, Channel
from ..jobs import PRIORITY_CONTROL
from ..tree import parse_virtual_did
from . import action_catalog
from .engine import AutomationEngine, LogEntry
from .hotkeys_win import HotkeyRegistry
from .logstore import MAX_ENTRIES, LogStore
from .pc_state import PcState, process_in
from .process_sampler import Sample, ProcessSampler
from .rules import PROCESS_EVENTS, Action, RuleSet

#: 收发器的结果队列多久取一次。跟 ``jobs`` 那条 worker 的 drain 同一个节奏。
DRAIN_MS = 16

#: 进程触发多久比对一次进程名。**2026-11-19 用户拍板 2 秒**：Toolhelp32 快照本机
#: 实测 ~6.7 ms/次（405 进程）；1s→2s 把 CPU 从 0.67% 砍到 0.34% 单核，代价是
#: 2 秒内启动又退出的一次性进程会漏掉，用户明确接受。采样已挪到 worker 线程
#: （主线程零卡顿），定时器只在该机器配了进程规则时启动。
#: **别改 WMI 事件订阅**：免管理员的 intrinsic 事件本质也是快照且更重，真事件型
#: StartTrace / ETW 要管理员——实测与出处见 ``docs/reference/process-poll-vs-wmi.md``。
PROCESS_POLL_MS = 2000

#: 「本程序启动时」的规则延迟多久触发（毫秒）。设备表刚就绪就发，会和首屏轮询、
#: 卡片构建挤在一条串行队列上；2 秒后跑，日志里也看得清是启动触发的。
START_TRIGGER_DELAY_MS = 2000

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
        #: 状态提供者只建一份：② 条件求值与「某程序」触发共用（验证脚本可整份替换）
        self.pc_state = PcState()
        self.engine = AutomationEngine(
            self.rules,
            state=self.pc_state,
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
        #: 「某程序」触发：只盯规则里用到的名字，值是上次是否在运行；
        #: ``None`` = 读不到进程表、还没建基线（那一轮不判边沿）。
        self._process_seen: dict[str, bool | None] = {}
        # 采样放 worker 线程：主线程只接结果做边沿判定。QTimer 必须在 worker 线程
        # 里创建，所以走 QThread.started → start_polling，不在构造时不小心建到主线程。
        self._process_thread = QThread(self)
        self._process_thread.setObjectName("automation-process-poll")
        self._sampler = ProcessSampler(self._read_process_names, PROCESS_POLL_MS)
        self._sampler.moveToThread(self._process_thread)
        self._process_thread.started.connect(self._sampler.start_polling)
        self._sampler.sampled.connect(self._on_process_sample)
        self._sampler.request_sample.connect(self._sampler.sample_once)
        #: 正开在编辑页里的规则 id：它们的**热键临时退掉**（启用状态不动），
        #: 见 :meth:`suspend_hotkeys`。界面只管进出，恢复一律走 ``sync()``。
        self._editing: set[str] = set()
        #: 「本程序启动时」一个进程只触发一次：重登 / 重新加载设备表都会再走一遍
        #: ``Store.on_loaded``，不能每次都把那条规则叫起来。
        self._start_fired = False
        self.sync()

    # -- 生命周期 --------------------------------------------------------
    def start(self) -> None:
        """主窗口构造完、设备列表可用之后调一次：启动后延迟重试一次被占用的组合。

        **不在这里触发「本程序启动时」的规则**：这时设备表还没加载（``MainWindow.start``
        先调本方法再 ``store.load()``，未登录甚至直接弹登录框）。那个由主窗口在
        ``Store.on_loaded`` 里调 :meth:`fire_start_rules`。
        """
        self.sync()
        self.registry.retry_once()

    def fire_start_rules(self) -> None:
        """「本程序启动时」触发，**一个进程只跑一次**（内部幂等）。

        由主窗口在设备表就绪（``Store.on_loaded``）后调：那一刻 ``store.devices`` 里
        才有设备，规则里的设备动作才发得出去。未登录 / 加载失败不会走到这里——那种
        情况下"启动时"本来也没有可用的设备，规则等下次启动。

        再延 2 秒才真跑：``on_loaded`` 那一刻主窗口正在铺卡片、发首屏轮询，紧接着跑
        设备动作会和它们挤在一条串行队列上，日志也会显得"启动就慢了半拍"。
        """
        if self._start_fired:
            return
        self._start_fired = True
        self._call_later(START_TRIGGER_DELAY_MS, self._on_app_start)

    def _on_app_start(self) -> None:
        # 编辑中的规则不跑：`_editing` 的语义就是"这条暂时别动"。启动事件碰上编辑页
        # 的机会很小（要开机 2 秒内点进去），但那正是用户在改它的时候。
        rules = [r for r in self.rules.rules_for_trigger("app_start")
                 if r.id not in self._editing]
        if rules:
            self.engine.trigger_rules(rules, source="程序启动")

    def close(self) -> None:
        self.engine.close()
        self._drain_timer.stop()
        if self._process_thread.isRunning():
            self._process_thread.quit()
            self._process_thread.wait(3000)
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

    def scene_options(self, home: str | None = None) -> list[dict[str, Any]]:
        """某个家庭里的快捷场景（给「＋ 选择快捷场景」用）。默认 = 当前家庭。

        **连隐藏的场景一起返回**：隐藏只是首页那排胶囊不画它（2026-11-24 定的口径），
        场景本身还在。动作指向一个"首页看不见"的场景完全合法——用户可能正是为了
        不让它占首页才藏起来，却仍要能被热键唤起。隐藏 / 停用由界面标注，这里不筛。

        ``store`` 没有这个方法 / 场景还没加载时返回空表：编辑窗不能因为取不到场景
        就打不开（离线验证的假 store 也没有）。
        """
        want = self.current_home if home is None else home
        getter = getattr(self.store, "scenes_for", None)
        if not callable(getter):
            return []
        try:
            scenes = list(getter(want, include_hidden=True))
        except Exception:  # noqa: BLE001
            return []
        out: list[dict[str, Any]] = []
        prefs = getattr(self.store, "prefs", None)
        is_hidden = getattr(prefs, "is_scene_hidden", None)
        for scene in scenes:
            sid = str(scene.get("scene_id") or "").strip()
            name = str(scene.get("name") or "").strip()
            if not sid or not name:
                continue
            out.append({
                "scene_id": sid,
                "name": name,
                "enabled": bool(scene.get("enable", True)),
                "hidden": bool(is_hidden(sid)) if callable(is_hidden) else False,
            })
        return out

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

    # -- 编辑页临时停用 --------------------------------------------------
    def suspend_hotkeys(self, rule_id: str) -> None:
        """进编辑页时调：**只退热键、不改启用状态**，退出编辑页自动恢复。

        为什么非得这样：全局热键是系统层的，比对话框先拿到按键——编辑页里想在键帽
        上重新录「这条规则正在用的那个组合」，按键在系统层就被拦走了，对话框的
        ``keyPressEvent`` 永远收不到，用户只看到规则被执行了（2026-11-17 实测）。

        退掉的是"这条规则贡献的注册"：同一个组合若还被别的启用规则用着，注册表
        仍然留着它（那本来就该触发别的规则），此时分发层由
        :meth:`_on_hotkey` 按 id 把编辑中的这条挡掉。
        """
        if not rule_id or rule_id in self._editing:
            return
        self._editing.add(rule_id)
        self.sync()

    def resume_hotkeys(self, rule_id: str) -> None:
        """离开编辑页（保存 / 取消 / 直接关窗都算）：把热键按当前启用状态复原。

        **幂等**：没暂停过（新建规则、规则已被删）也照常 sync 一次，不会报错。
        """
        if rule_id:
            self._editing.discard(rule_id)
        self.sync()

    @property
    def suspended_rules(self) -> set[str]:
        """正在编辑页里、热键被临时退掉的规则 id（给验证脚本与调试用）。"""
        return set(self._editing)

    # -- 内部 ------------------------------------------------------------
    def sync(self) -> None:
        # 编辑页里的规则不参与注册；同时把"本应注册却没注册"的组合报给注册表，
        # 让触发行的状态小字说清是「编辑中暂停」而不是「未注册」。
        full = self.rules.enabled_keys()
        want = self.rules.enabled_keys(skip_ids=self._editing)
        self.registry.sync(want, paused=[k for k in full if k not in want])
        self._sync_process_watch()
        self.status_changed.emit()

    # -- 进程触发（边沿检测） ---------------------------------------------
    def _watched_processes(self) -> set[str]:
        """当前启用规则里出现的进程名（小写，保留用户填写的 .exe 后缀与否）。"""
        out: set[str] = set()
        for rule in self.rules.rules_for_trigger("process"):
            for trig in rule.triggers:
                if trig.type == "process" and trig.arg.strip():
                    out.add(trig.arg.strip().lower())
        return out

    def _read_process_names(self) -> Sample:
        """给 worker 的采样回调：**读此刻的** ``self.pc_state``。

        用回调而不是让 worker 持有 PcState 实例，是为了验证脚本替换
        ``svc.pc_state`` 后 worker 下一次就读新的那份（真机上这个引用不变）。
        """
        return self.pc_state.process_names()

    def _sync_process_watch(self) -> None:
        """规则变动后更新关注名单、按需启停 worker。

        新增名字先放 ``None``（未知），再让 worker **立即采一次**（``request_sample``）
        回来建基线——旧实现是在主线程同步采样建基线，那一下也要 6.7 ms。
        基线没建好之前那次采样只建基线、不判边沿，所以保存一条「notepad 启动」时
        notepad 本来开着也不会在下一 tick 白触发。名单空了就停 worker。
        """
        want = self._watched_processes()
        for name in want - set(self._process_seen):
            self._process_seen[name] = None
        for name in [n for n in self._process_seen if n not in want]:
            self._process_seen.pop(name, None)
        if want:
            if not self._process_thread.isRunning():
                self._process_thread.start()
            self._sampler.request_sample.emit()
        elif self._process_thread.isRunning():
            self._process_thread.quit()
            self._process_thread.wait(3000)

    def _poll_processes(self) -> None:
        """同步采一次再判定——**只给验证脚本用**，生产路径走 worker 的 sampled 信号。

        保留这个入口是为了让边沿断言不必等线程调度：测试里塞假快照，直接调它模拟
        "一次采样结果回来了"。真机上主线程不调它，一次进程表都不读。
        """
        self._on_process_sample(self.pc_state.process_names())

    def _on_process_sample(self, names: Sample) -> None:
        """主线程收到一次采样结果：只在"上次不在、这次在"时算启动，反过来算退出。

        ``None`` = 采样失败：这一轮**什么都不判**，也不写回基线——下次读到了仍先
        建基线，避免把"刚能读到"误当成启动。
        """
        if not self._process_seen or names is None:
            return
        events: list[tuple[str, str]] = []
        for name, prev in list(self._process_seen.items()):
            now = process_in(names, name)
            if prev is None:
                self._process_seen[name] = now
            elif prev != now:
                self._process_seen[name] = now
                events.append((name, "start" if now else "exit"))
        # 一条规则挂多条进程触发时，同一轮可能**多个关注名同时翻边沿**（两条独立
        # 的进程都起来、或 notepad.exe / NOTEPAD 两种写法指向同一进程）。多触发是
        # OR 语义——任一命中即执行——所以本轮按规则 id 去重，别把同一条规则叫两遍。
        # 冷静期挡不住这个（默认可能为 0），引擎的重入锁在快速跑完时也已释放。
        fired: set[str] = set()
        for name, event in events:
            self._fire_process_event(name, event, fired)

    def _fire_process_event(self, name: str, event: str, fired: set[str]) -> None:
        """按进程名 + 事件找规则，条件 / 冷静期 / 重入照旧交给引擎。

        ``fired`` 是**本轮采样**已经决定要跑的规则 id，由 :meth:`_on_process_sample`
        跨多个事件共享——同一条规则的两个触发行同时命中只跑一次（OR 语义），
        下一轮采样会换一个新的空集合，真正的"退出后又启动"照常再触发。
        编辑中的规则跳过（同启动触发的口径）。
        """
        rules = [r for r in self.rules.rules_for_trigger("process")
                 if r.id not in fired
                 and r.id not in self._editing
                 and any(t.type == "process" and t.op == event
                         and t.arg.strip().lower() == name for t in r.triggers)]
        if not rules:
            return
        fired.update(r.id for r in rules)
        label = PROCESS_EVENTS.get(event, event)
        self.engine.trigger_rules(rules, source=f"进程 {name} {label}")

    def _on_rules_changed(self) -> None:
        self.sync()
        self.rules_changed.emit()

    def _on_hotkey(self, keys: str) -> None:
        # 编辑页里的规则不执行：热键层已经退掉它，但同一个组合可能还挂在别的启用
        # 规则上（那时注册还在），这里再按 id 挡一次，否则编辑中的那条照样会响。
        self.engine.trigger(keys, skip_ids=self._editing)

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
        """执行一条动作：设备动作走 ``_run_device``，快捷场景走 ``_run_scene``。

        ``done`` 只收一句中文原因，**空串 = 成功**。但注意引擎已经不用它推进队列了
        （见 ``engine._step``）：这里发出去就返回，回执之后 ``done`` 才会被调一次
        （失败时）。
        """
        if act.type == "scene":
            self._run_scene(act, done)
            return
        self._run_device(act, done)

    def _run_scene(self, act: Action, done: Callable[[str], None]) -> None:
        """执行一个快捷场景。

        **不在发送前核对"场景还在不在"**：那要读 store 的场景缓存，而场景可能刚被
        别的端改过。云端那边场景 id 不存在时会回失败，按同一套 ``done`` 报出去，
        日志里写明原因——与设备动作"设备移除后才报错"同一口径。

        ``act.home`` 记的是"当初选这个场景时所在的家"。场景 id 只在家庭内唯一，
        规则换过家庭后不能拿新家庭去猜；老数据没有这个字段才回落到当前家庭。
        """
        sid = str(act.scene_id or "").strip()
        if not sid:
            done("没有选快捷场景")
            return
        if self.worker is None:
            done("没有配置执行通道（serial 模式需要显式传 worker）")
            return
        home = str(act.home or "") or self.current_home

        def finished(result: Any, error: BaseException | None) -> None:
            if error is not None:
                done(f"后台任务失败：{error}")
                return
            if result is False:
                # ``Client.run_scene`` 失败固定返回 False，原因在 ``last_error``
                detail = getattr(self.client, "last_error", "") or ""
                done(detail or "云端没有执行这个场景")
                return
            # 场景改到哪几台说不准：让界面那串全量读跑起来，卡片才跟着变
            confirm = getattr(self.store, "schedule_confirm", None)
            if callable(confirm):
                confirm()
            done("")

        self.worker.submit(
            lambda: self.client.run_scene(sid, home),
            callback=finished,
            priority=PRIORITY_CONTROL,
            label="场景",
        )

    def _run_device(self, act: Action, done: Callable[[str], None]) -> None:
        """执行一条设备动作：找设备 → 按名字查坐标 → 按入参名拼 ``in`` → 交给收发器。

        判据全在这里做，而且都在**发送之前**：找不到设备 / 没这个功能 / 离线 /
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
