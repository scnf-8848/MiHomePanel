# 「某程序」触发：每秒轮询 vs WMI 事件订阅（2026-11-19 实测评估）

> 用户问：每秒轮询进程名会不会影响性能？WMI 事件订阅是不是能避免轮询？
> **一句话结论：换 WMI 是倒退，不是优化。** 现役 Toolhelp32 采样本机实测约
> **6.7 ms/次**；微软官方实测「每秒一次的 `__InstanceCreationEvent` 订阅」让 WMI 服务
> 在 133 个进程的机器上吃掉 **5.8%–7.0% CPU**——高一个数量级。真正的事件型
> （`Win32_ProcessStartTrace` / ETW `Microsoft-Windows-Kernel-Process`）**都要管理员**，
> 与本项目「非管理员、零依赖」的承诺冲突。
>
> **2026-11-19 用户拍板后已落地两项**：采样挪进独立 `QThread`（主线程一次都不读进程表），
> 间隔 1s → 2s（用户明确接受漏掉 2 秒内启动又退出的短命进程）。CPU 从 0.67% 砍到
> 0.34% 单核，主线程周期卡顿归零。
> 索引状态 = **明确排除（WMI / ETW 路线）**；现役「worker 采样 + 2s 间隔」已落地。

## 1. 现役实现到底做了什么

`ProcessSampler`（`core/automation/process_sampler.py`）跑在独立 `QThread` 上，
间隔 `PROCESS_POLL_MS = 2000`，**只在有启用规则用到进程触发时**启动；名单清空就
`quit()` + `wait()`。每次 tick：

1. worker 线程里 `pc_state.process_names()` → `CreateToolhelp32Snapshot(...)` +
   `Process32FirstW/NextW` 逐条遍历，取小写 exe 名集合；不读前台窗口 / 锁屏 / 电源。
2. 结果（或 `None` = 读不到）经 Qt 跨线程信号送回主线程；主线程只比对规则里出现过的
   (`_watched_processes()`) 名字，不关心其余进程。
3. 主线程边沿检测：上次不在、这次在 = 启动；反之 = 退出；读不到那轮什么都不判。

规则改动后新增的名字先放 `None`，再 `request_sample` 让 worker 立即采一次建基线——
旧版是在主线程同步采样建基线，那一下同样要 6.7 ms。**边沿判定、规则匹配、日志始终在
主线程**，所以 `_process_seen` / `engine` 没有跨线程竞态。

## 2. 本机实测（2026-11-19，405 个进程 / 168 个不同 exe 名）

复跑：`PYTHONPATH=. .venv/Scripts/python.exe -X utf8 scripts/probe_process_poll.py`

| 路径 | 平均 | 中位 | P95 | 最大 | 每秒一次占单核 |
| --- | --- | --- | --- | --- | --- |
| 现役 `process_names()`（Toolhelp32） | **6.745 ms** | 6.470 ms | 8.275 ms | 13.739 ms | **0.67%** |
| `NtQuerySystemInformation` 一次取回 | 4.827 ms | 4.698 ms | 5.507 ms | 12.027 ms | 0.48% |
| 完整 `PcState.read()`（含前台 / 锁屏 / 电源） | 7.551 ms | — | — | — | 0.76% |

`set()` 复制 5000 次共 4.9 ms（≈0.001 ms/次），**集合操作不是瓶颈**，成本几乎全在
内核快照 + 逐条 `Process32NextW` 的 Python/ctypes 往返（405 次）。

怎么读这个数：

- **整机口径**：0.67% 是**单核**占比；按 8 核算约占整机 0.08%。常规使用无感。
- **主线程口径（1s + 主线程时代）**：每秒有约 6.7 ms 被采样占住，一帧 16.7 ms，
  不会稳定掉帧但可能有偶发微小卡顿。**2026-11-19 两件事都治了**：采样进 worker →
  主线程 0 次读进程表；间隔 2s → CPU 总量减半。
- **按需启停**：没配进程触发的机器，worker 线程根本不启动，上述开销为 0。

### 2.1 1s → 2s 的收益量化

采样成本是固定的单次 6.7 ms，改间隔只改频率，**收益与代价都能算死**：

| 指标 | 1 秒 | 2 秒 | 变化 |
| --- | --- | --- | --- |
| 单核 CPU 占比 | 0.67% | **0.34%** | 减半 |
| 一天累计采样 CPU 时间 | 约 9.6 分钟 | 约 4.8 分钟 | 省约 4.8 分钟/天 |
| 主线程占用（采样已进 worker 后） | 0 | 0 | 无差别 |
| 检测延迟（平均 / 最坏） | 0.5 s / 1 s | 1 s / 2 s | 平均 +0.5 s |
| 漏掉的短命进程 | 存活 <1 s | 存活 <2 s | 用户 11-19 明确接受 |

**净效果**：CPU 减半是实打实的；主线程卡顿那一项在搬线程时已经归零，跟 1s/2s 无关。
所以改 2s 买的只是 CPU，付的是最多 1 秒额外延迟 + 更多短命进程漏检——用户拍板接受。

## 3. 三条替代路径，逐条判

| 方案 | 权限 | 是不是真事件 | 判定 |
| --- | --- | --- | --- |
| WMI `__InstanceCreationEvent` / `__InstanceDeletionEvent WITHIN 1` | 非管理员可用 | **否**，WMI 内部每秒快照比对 | **排除**：照样轮询，且更重（见第 4 节） |
| WMI `Win32_ProcessStartTrace` / `StopTrace` | **要管理员**（非提权报 Access denied） | 是（extrinsic / ETW 支撑） | **排除**：项目承诺非管理员运行 |
| ETW `Microsoft-Windows-Kernel-Process` 实时会话 | **要管理员**（`StartTrace` 需 `SeSystemProfilePrivilege`），会话进程退出后还会残留 | 是 | **排除**：同上，且纯 ctypes 写 ETW 解码是另一个量级 |
| `NtQuerySystemInformation(SystemProcessInformation)` | 非管理员 | 否 | **不换**：只快约 28%，是未公开 API，结构体随 Windows 版本变 |

`NtQuery` 的补充实测：结果比 Toolhelp32 少 `[system process]`（PID 0 的 `ImageName`
为空），要额外处理边界；为 2 ms 引入一个未文档化的系统调用 + 版本兼容风险，不划算。

## 4. 微软官方对 WMI 订阅开销的实测（关键证据）

微软 Windows SDK 团队博客 *Accidental Denial of Service through Inefficient Program
Design Part 1 – Watching WMI Events for Object Creation* 直接测了这个场景：8 vCPU /
32 GB / SSD，133 个基础进程，订阅 `__InstanceCreationEvent`（`WITHIN 1`）：

| 同时订阅的用户数 | WMI 服务 CPU（两分钟平均） |
| --- | --- |
| 2 | 5.8% – 7.0% |
| 6 | 13.7% – 14.5% |
| 15 | 49.6% – 50.8% |

单个订阅就 5.8%–7.0%，而现役 Toolhelp 轮询只有 0.67% 单核。官方原文还给了两条结论：

> "Because the class being monitored does not have a corresponding event provider,
> WMI polling mechanism is used to periodically check if an intrinsic event has occurred."
> （`WITHIN` 就是轮询间隔，不是事件推送。）

> "When running on a local machine, using WMI is just adding overhead from a resource
> standpoint versus calling the native APIs yourself."

另外 MSDN 的 WMI notification query 文档明确说：intrinsic 事件的 `WITHIN` **低于 30 秒
通常算过短**，会产生额外开销——我们要的 1 秒正好落在官方不建议的区间。短 `WITHIN`
和「不漏短命进程」在 WMI 里是死结。

## 5. 结论与落点

**保持现役 Toolhelp 轮询，不上 WMI / ETW。** 理由不是"没时间做"，是算过账：

- 真正零轮询的两条路都要管理员，直接违反项目约束；
- 不需要管理员的两条 WMI 路本质还是轮询，而且比自己调原生 API 更重；
- 现役实现按需启停 + 轻量快照 + 边沿检测，且只在配置了该触发的机器上运行。

**2026-11-19 已落地两项优化**（不再待排期）：

1. `process_sampler.ProcessSampler` 在独立 `QThread` 里采样，主线程只收结果判边沿；
   主线程从此一次都不读进程表。
2. `PROCESS_POLL_MS` 1s → 2s，单核 0.67% → 0.34%，用户接受漏 2s 内短命进程。
   `verify_automation_process.py` 用假快照钉住：主线程零采样、worker 采样后信号回主线程
   建基线、启停正确。

**落点**：`core/automation/process_sampler.py`（worker）、
`core/automation/service.py`（`PROCESS_POLL_MS` / `_on_process_sample` / `_sync_process_watch`）、
`core/automation/pc_state.py`（`process_names`）；实现说明 `docs/dev/automation.md`
「触发源 · 某程序」；探针 `scripts/probe_process_poll.py`。

## 6. 未验证 / 边界

- [x] ~~后台线程采样尚未实现~~ → **11-19 已落地**，验证脚本钉住主线程零采样；
- [ ] 6.7 ms 是本机 405 进程的数字。进程数更多（例如开发机开一堆服务）会线性上升，
      换机器可跑探针重测；
- [ ] WMI 的 5.8%–7.0% 来自微软官方在另一台机器上的实测，**不是本机复测**——本机
      已通过"要管理员 / 本质是轮询"两条硬约束判定排除，不依赖这个数字；
- [ ] 1 秒轮询会漏掉两次采样之间启动又退出的短命进程，这是现役方案的已知边界，
      换 WMI intrinsic 事件（同样 1 秒快照）并不会改善。

## 7. 资料

| 内容 | 链接 |
| --- | --- |
| 微软实测 WMI 进程事件订阅 CPU 开销、`WITHIN` 是内部轮询、原生 API 更省 | https://learn.microsoft.com/zh-cn/archive/blogs/winsdk/accidental-denial-of-service-through-inefficient-program-design-part-1-watching-wmi-events-for-object-creation-e-g-win32_process |
| MSDN：WMI notification query 中 `WITHIN` 的语义与 <30 秒的开销警告 | https://msdn.microsoft.com/magazine/cc160917.aspx |
| `Win32_ProcessStartTrace` 需管理员；`__InstanceCreationEvent` 免管理员但靠轮询、最小 1 秒 | https://stackoverflow.com/questions/75931782/how-do-i-wait-for-an-application-to-start-without-administrator-permissions-in-p |
| 创建 ETW 实时会话需提权（`SeSystemProfilePrivilege`），会话可能残留 | https://www.nuget.org/packages/Nefarius.Utilities.ETW/2.4.0 |
| 本项目能力边界与零依赖约定 | `docs/reference/pc-state-api.md`、`docs/dev/automation.md` |
