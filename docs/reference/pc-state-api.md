# 电脑自动化：规则结构 与「电脑状态」系统级 API 可行性

> 2026-11-13：用户按米家 App「创建自动化」页给出规则结构（截图存
> `docs/reference/mihome-app/31_create_automation.jpg`），并问「系统级 API 能拿到哪些电脑
> 状态，会不会必须外接其它程序」。本页记**设计契约 + 能力调研 + 本机实测**。
> 探针 `scripts/probe_pc_state.py`（纯标准库 ctypes、只读、不装依赖、不发米家请求）。
> 索引状态 = **已落地**（2026-11-13 电脑自动化 v1 开工，6 类零依赖条件、
> 三层防重、冷静期均已实现）。本文仍是能力边界与契约依据；实现取舍见
> `docs/dev/automation.md`「电脑自动化：四层窗口（2026-11-13 落地）」。
>
> **一句话结论**：进程 / 前台窗口 / 空闲 / 锁屏 / 电源这些**纯 Windows API 就能读，不用装
> 任何东西**；「整机墙上功率」确实拿不到，但 **CPU 封装功率能读**（本机实测 17 W，
> 走 Windows 内置 EMI，不需要管理员、不需要第三方驱动）。
>
> **2026-11-13 01:00 用户拍板**：① **电脑条件做**，把实测零依赖的条件都放进 v1；
> ② **CPU 功率不做**（"意义不大"）——第 3 节 EMI 调研**保留备查，但明确 v1 不落地**；
> ③「否则就执行」v1 不做（**11-14 已补做**，见 1.2）；④ 新增热键重复触发的
> **冷静期 / 保护期**设计问题 → 见 1.4。以下涉及「待定 / 建议」的旧措辞按这四条读。

## 1. 规则结构（2026-11-13 用户定，第一版范围）

参考米家 App 的三段式，截图里从上到下是「当以下情况发生」→「且满足全部状态条件」→
「就执行」。用户明确说**不用完整照搬**，只借这个分块逻辑。

| 块 | 米家原文 | 第一版支持 | 第一版明确不做 |
| --- | --- | --- | --- |
| **① 触发** | 当以下（任一）情况发生时 | **键盘快捷键**，可单键、可 2–3 键组合 | 设备触发（交给米家自己的自动化）；「启动某程序」等后续触发 |
| **② 条件** | 满足**全部**状态条件 / 满足**任一**状态条件 | 两种模式（AND / OR），**条件可以为空**；**电脑条件做**（v1 放实测零依赖的那几类，清单见 4.1） | **设备条件判断**（理由见 4.2）；**CPU / 整机功率**（11-13 01:00 用户定「意义不大」）；温度 / 风扇 / 电压等要外接程序的 |
| **③ 动作** | 就执行 | 可添加多个，**按从上到下依次执行**；两类动作：**发虚拟事件**、**延迟** | 直接给设备发动作指令（待定） |

用户举例：「全部」= 吸顶灯开着 **且** 人在传感器无人；「任一」= 吸顶灯开着 **或**
卫生间灯开着。这两个例子是设备条件，**第一版不做**，只是用来说明 AND / OR 的语义。
**2026-11-13 01:00 拍板**：电脑条件**做**，CPU 功率**不做**；详见 4.1。

### 1.1 与米家云端结构的对应

`GetSceneList` 的字段刚好能一一对上（见 `mijia-automation-events.md` 2.2）：

```
scene_trigger.triggers[]           ↔ ① 触发，express=1 就是「任一」
scene_condition.{express,conditions[]} ↔ ② 条件，express 0=全部 / 1=任一
scene_action.actions[]             ↔ ③ 动作，数组顺序即执行顺序
```

我们自己不写云端场景（`AppSceneService/Edit` 已判定不做），但 `rules.json` 借这套字段名
能让「读别人的自动化」和「配自己的规则」用同一套词汇，省一份概念。

### 1.2 截图里还有第四块：「否则就执行」

用户描述只提了三块，但截图底部确实有「否则就执行」+「创建」。
**2026-11-13 01:00 定 v1 不做；2026-11-14 用户拍板补做，已落地为编辑页第 ④ 段。**
当时悬着的两个问题已定：**只有「条件不成立」走 else**，「动作执行失败」不走
（那是故障，改系统通知 + 日志）；**冷静期共用**——它针对的是触发动作，冷静期内
不往下走、不经过②，真跑了 else 也同样占冷静期与重入锁。至于「条件为空时它没有
意义」，落成两条：② 为空时禁用 ④ 的添加按钮、保存时由 `validate_rule` 拦下。

### 1.3 实现时要守住的几条

- **条件在触发瞬间求值一次**，不要为条件另起轮询 / timer。这既省请求，也符合项目
  「别另起 timer、往 `poll_demand` 登记」的约定。只有将来做「启动某程序」这类*触发*
  才需要持续监听（WMI 事件或进程快照 diff），那是另一个量级。
- **延迟不能 `time.sleep()`**：会卡死 Qt 主线程。用成员 `QTimer` 把动作队列串成
  「执行一个 → 等 delay → 下一个」，并想清楚窗口关闭 / 进托盘时队列怎么办。
- **组合热键**用 `RegisterHotKey` + `WM_HOTKEY`（`MOD_CONTROL/SHIFT/ALT/WIN` + VK，
  天然支持 2–3 键），只有「按下」没有「松手」——做触发够用。**注册必须带
  `MOD_NOREPEAT`（见 1.4）**。单键（如裸 `F7`）会抢全局按键，注册失败 / 被占用
  要给可见提示。骨架可复用 `core/single_instance.py` 的原生事件过滤器。
- **发虚拟事件**已端到端验证（`mijia-automation-events.md` 4.2.1，
  **验的是云端能力；新页面这条链路尚未真机跑过**）：
  `siid 4 / aiid 1 / in:["事件名"]`，`code 0` 只代表云端收下。规则里存事件名，
  中枢 did 按 `core/geek.py` 的思路「程序自己问出来」，不写死型号 / iid；
  落地时 `siid/aiid` 也是按 action 名 `emit-virtual-event` 从规格动态查的。
- 当前结构（**schema 3**，以 `core/automation/rules.py` 为准；v1 契约索引见
  `automation-hotkeys-and-ui.md` 6.1，明确不做清单见 6.2）。
  **2026-11-14 动作重构后，旧的 `virtual_event` / `speaker` 两种动作已删除**：
  发虚拟事件只是设备动作的一个功能名（`emit-virtual-event`），动作统一是
  `device_action` / `delay`：
  ```jsonc
  {
    "version": 3,
    "rules": [
      {
        "id": "…", "name": "示例", "enabled": true,
        "triggers": [{"type": "hotkey", "keys": "Ctrl+Alt+F7"}],
        "cooldown_ms": 1000,                 // 0=关闭；挂在规则上，见 1.4
        "condition_mode": "all",             // all=全部 / any=任一
        "conditions": [],                    // 空数组=无条件
        "actions": [
          {"type": "device_action", "did": "…", "device": "客厅中枢",
           "action": "emit-virtual-event", "args": {"virtual-param": "极客版按键自动化执行"}},
          {"type": "delay", "ms": 2000},
          {"type": "device_action", "did": "…", "device": "客厅中枢",
           "action": "emit-virtual-event", "args": {"virtual-param": "另一个事件"}}
        ]
      }
    ]
  }
  ```

### 1.4 触发去重与冷静期（2026-11-13 01:00 用户提问）

用户担心「键盘按下时可能会反复触发按键」，问这个限制该**明示在界面 / 由用户自定**，
还是**内置在代码里**。结论：**两者都要，但分工不同**。

**先核实事实：`RegisterHotKey` 默认确实会连发。** 按键自动重复会持续产生
`WM_HOTKEY`（微软 Old New Thing 明确写了）。Win7 起 `fsModifiers` 可带
`MOD_NOREPEAT = 0x4000`，让自动重复不再产生热键通知、初次按下仍触发一次。
一份 Windows 11 build 26300（**与本机同 build**）的实测还给出例外：**主键已经按住时
才注册热键，随后第一次自动重复仍会触发，带 `MOD_NOREPEAT` 也一样**；同份实测另记
「修饰键集合在主键按下那一刻比较」（先按主键后按修饰键不触发）、`VK_RCONTROL`
从不触发、低级键盘钩子吞掉主键会让热键不触发。

所以防重是**三层**，各解决不同来源：

| 层 | 解决什么 | 实现 | 用户可见 / 可配 |
| --- | --- | --- | --- |
| **L0 系统层** | 按住不放的自动重复刷屏 | 注册时**恒带** `MOD_NOREPEAT`；必要时再吞 30–50 ms 内同键重复 | **不可配、不展示**（正确性，不是策略） |
| **L1 规则冷静期** | 手抖连按 / 短时间重复触发 / 将来别的触发源（测试按钮、本机 HTTP）注入 | 每条规则 `cooldown_ms`，默认 **1000 ms**、`0` = 关闭 | **必须明示 + 用户自定** |
| **L2 动作队列重入** | 上一串带延迟的动作还没跑完又来一次触发 | v1 写死「丢弃新触发 + 记日志」；不排队、不中断重跑 | 不配，但日志里要看得见 |

**为什么 L1 不能内置一个数了事**：不同规则差异极大——语音播报 / 开灯这类希望
3–5 秒别被刷屏，切歌 / 翻页这类可能希望 200 ms 内连点有效。硬编码一个值必然有人嫌
迟钝、有人嫌吵。反过来 **L0 不能交给用户配**：配错的表现是「按了没反应」或「照样连发」，
用户无从判断是配置问题还是程序坏了。

**界面明示的三个要素**（缺一个，用户就会以为按了没生效）：

1. 规则卡片 / 编辑表单上**显示当前值**（如「1.0 秒内只触发一次」），且能改；
2. 被冷静期拦下时**不静默**：触发日志记一条「被冷静期拦截（剩余 0.6 s）」，
   需要时用 `ui/tooltip.py` 的 `toast(anchor, …)` 轻提示——**别用 `toast_top`**
   （那是「要跨程序、要用户看见」才用的）；
3. 「测试发送」要**明确**是绕过冷静期还是同样受限，两者择一并写清楚。

**几个容易写错的语义**：

- 冷却挂在**规则**上，不是热键上：一次触发 = 一条规则的一次执行；一个热键可绑多条
  规则 → 各自独立冷却，同一规则多个热键 → 共享一个冷却表。
- **从「触发被接受」那一刻起算**，不是等动作队列跑完。否则一条「发事件 → 延迟 5 s →
  再发事件」的规则，在动作执行期间会被反复重新触发，延迟顺序全乱。
- 冷却表**只活在内存**：不落盘、重启不残留（否则重启后「按了没反应」很难解释）；
  规则被禁用 / 编辑 / 删除时清掉对应条目。
- 别拿 `GetAsyncKeyState` 轮询判松手：那是给已放弃的「按住说话」准备的，要 5 ms 级
  timer，成本与误判都更高。真要「按住只触发一次、松开才能再触发」的严格语义，
  `RegisterHotKey` 拿不到 key-up，得上 `WH_KEYBOARD_LL`（DLL 钩子、杀软敏感），v1 不做。
- 旧草案 `rules.json` 已有 `cooldown_sec: 60` / `hold_sec`，那是**防设备状态抖动**
  （灯被关了又触发）的二期字段；v1 这个冷静期是**防按键重复**，默认要短得多，
  语义不同，别直接沿用 60。

**待验证**（实现时在本机做，不动设备）：`MOD_NOREPEAT` 在本机注册后的实际行为
（尤其「主键已按住才注册」那个例外）、被拦截时的日志文案、动作队列跑完前重入是否
真被丢弃。

## 2. 电脑状态：系统级 API 能力表

`[实测]` = 2026-11-13 在用户这台机器上跑 `probe_pc_state.py` 的结果。
全部只用 `ctypes` + Windows 自带 DLL，**没有 import 任何第三方库**。

| 想知道的状态 | 系统级 API | 本机实测 | 要外接程序吗 |
| --- | --- | --- | --- |
| 某个应用是否在运行 | `CreateToolhelp32Snapshot` + `Process32First/NextW` | `[实测]` 166 个不同 exe 名，按名可查 | 不要 |
| 应用已运行多久 | `OpenProcess` + `GetProcessTimes`（creation FILETIME） | `[实测]` 前台进程 79.6 分钟 | 不要 |
| 前台窗口标题 / 前台进程 | `GetForegroundWindow` + `GetWindowTextW` + `GetWindowThreadProcessId` + `QueryFullProcessImageNameW` | `[实测]` WorkBuddy / `D:\Program\WorkBuddy\WorkBuddy.exe` | 不要 |
| 用户空闲多久 | `GetLastInputInfo` + `GetTickCount64` | `[实测]` 0.1 秒 | 不要（只当前会话） |
| 锁屏 / 解锁 | `OpenInputDesktop` + `GetUserObjectInformationW(UOI_NAME)`；或 `WTSRegisterSessionNotification` 收 `WM_WTSSESSION_CHANGE` | `[实测]` 输入桌面 = `Default`（未锁） | 不要 |
| 交流 / 电池 / 电量 | `GetSystemPowerStatus` | `[实测]` 交流供电、本机无电池 | 不要 |
| 会话 ID / 是否管理员 | `ProcessIdToSessionId` / `IsUserAnAdmin` | `[实测]` 会话 1、非管理员 | 不要 |
| **CPU 封装功率** | **EMI**（`IOCTL_EMI_GET_MEASUREMENT`） | `[实测]` 17.01 W，见第 3 节；**11-13 01:00 用户定 v1 不做，调研仅备查** | **不要** |
| 整机墙上功率（插座口径） | **没有通用系统 API** | 拿不到 | 要（见 3.3） |
| 音量 / 静音 | `IAudioEndpointVolume` COM 接口 | 未测 | 不要，但 ctypes 写 COM 较繁 |
| CPU / 内存占用 | `GetSystemTimes` / `GlobalMemoryStatusEx` | 未测 | 不要 |
| GPU 占用（N 卡） | `nvml.dll`（随显卡驱动） | 未测 | 不要额外装，但要厂商驱动 |
| 温度 / 风扇 / 主板电压 / 磁盘 SMART | Windows 不提供 | — | **要**：LibreHardwareMonitor / HWiNFO，且需 Ring 0 驱动 + 管理员 |

用户担心的「是不是要外接其它程序」，对**上面绝大多数状态不成立**。真正要外接的是
硬件传感器那一类（温度、风扇、主板电压），而不是「在跑什么程序 / 人在不在」。

### 2.1 两个容易踩的坑

- `GetLastInputInfo` 给的是**当前会话**的最后输入，不是全系统；服务进程（session 0）
  永远读到「从没输入」。本项目是交互桌面应用，正常会话，不受影响。
- `GetTickCount` 是 32 位、49.7 天回绕；用 `GetTickCount64`，但 `LASTINPUTINFO.dwTime`
  仍是 32 位，做差时要取低 32 位再按无符号回绕处理（探针里已这么写）。
- 进程名匹配建议按 exe 文件名（大小写不敏感），别按窗口标题——标题会变、可为空。

## 3. 「功率」单独说：能拿到的是哪一种

> **v1 不落地（11-13 01:00 用户定「意义不大」）**：本节只作为「将来若要做硬件类条件」
> 的备查，不要因为这里写了实现细节就顺手加进第一版。EMI 探针保留，换机器可复跑。

### 3.1 Windows 没有「整机实时瓦数」这个 API

- `GetSystemPowerStatus` 只给交流/电池状态、电量百分比、剩余续航秒数，**没有瓦数**；
  台式机没有电池时基本只剩「交流供电」这一项（本机实测 `BatteryFlag & 128`）。
- WMI `Win32_PowerMeter` / `Win32_Battery.BatteryRate` 需要驱动 / 硬件上报，不是通用值，
  而且台式机通常没有电池。
- `powercfg /batteryreport` 是**历史 / 平均**，不是瞬时。

### 3.2 但 EMI 能读 CPU 封装能耗，本机实测通了

Windows 10 起有 **Energy Meter Interface（EMI）**：驱动暴露 `GUID_DEVICE_ENERGY_METER`
设备接口，客户端 `CreateFile` + `DeviceIoControl` 读**累计能量**（单位 pWh）与时间戳
（100 ns），两次采样相减再除以时间就是平均功率。CTL_CODE 来自 WDK 的 `emi.h`：

```
CTL_CODE(FILE_DEVICE_UNKNOWN=0x22, function, METHOD_BUFFERED=0, FILE_READ_ACCESS=1)
  GET_VERSION=0 / GET_METADATA_SIZE=1 / GET_METADATA=2 / GET_MEASUREMENT=3
  → 0x224000 / 0x224004 / 0x224008 / 0x22400C
```

本机实测（`probe_pc_state.py`，**非管理员**、纯 ctypes）：

```
EMI 版本 V2    计量硬件 Microsoft PPM   4 个通道：
  RAPL_Package0_PKG   17.01 W     （能量差 4,727,720,277 pWh）
  RAPL_Package0_DRAM   0.00 W
  RAPL_Package0_PP0    6.51 W
  RAPL_Package0_PP1    0.00 W
  计时差 1.000 s —— 与墙钟一致，证明 100ns/pWh 换算没搞错
```

关键点：**不需要管理员、不需要装驱动、不需要 LibreHardwareMonitor**。社区资料说
Windows 11 内核直接把 RAPL 计数器通过 EMI 暴露出来（Win10 一般只有带物理计量芯片的
设备如 Surface 才有）；本机是 Windows 10.0 build 26300（Win11 内核），与这个说法吻合。
但这是**硬件 / 系统版本相关能力**，换机器要重新探一遍，不能当成保证。

### 3.3 语义必须分清

| 用户说的「电脑当前功率」可能指 | 能不能拿 | 说明 |
| --- | --- | --- |
| CPU 忙不忙 / 是否在跑重负载 | **能** | 用上面的 PKG 通道；做条件判断够用 |
| 整机从插座吃了多少瓦 | **不能**（系统侧） | 不含显卡、显示器、电源损耗；要智能插座 / UPS 计量 |

如果真想要「整机瓦数」，本项目反而有一条现成路：**米家智能插座本身有功率属性**，
但那就属于「设备条件」，而第一版明确不做设备条件。所以这条暂不进第一版。
另一条是 LibreHardwareMonitor / HWiNFO 的 WMI 或 HTTP 接口——正是用户说「麻烦」的
外接程序：要 Ring 0 驱动（WinRing0 有 CVE-2020-14979）、要管理员、要常驻，且与
内核反作弊可能冲突。**建议不引入**。

## 4. 对第一版的建议

### 4.1 v1 条件清单（2026-11-13 01:00 用户定：做，CPU 功率不做）

用户拍板「电脑条件做吧，但是 CPU 功率就不用了，意义不大，先把其他能直接获取条件
放进去」。所以 v1 把**实测零依赖、纯 ctypes、触发瞬间一次查询就能判定**的条件全放进去，
不再只挑 2–3 个。每条统一形状 `{type, op, arg}`，AND / OR 由条件块的 `mode` 决定。

| # | 条件 | 判据 / 参数 | 来源 | 备注 |
| --- | --- | --- | --- | --- |
| 1 | 某程序正在运行 / 没运行 | exe 名（大小写不敏感） | `CreateToolhelp32Snapshot` | 别按窗口标题匹配——标题会变、可为空 |
| 2 | 某程序已运行 ≥ / ≤ N 分钟 | exe 名 + 分钟数 | `GetProcessTimes` | 取最早匹配的那个实例 |
| 3 | 前台是某程序 / 前台窗口标题包含 | exe 名或子串 | `GetForegroundWindow` + `QueryFullProcessImageNameW` | 标题匹配要说清大小写规则 |
| 4 | 用户空闲 ≥ / < N 分钟 | 分钟数 | `GetLastInputInfo` + `GetTickCount64` | 只当前会话；32 位回绕按 2.1 处理 |
| 5 | 已锁屏 / 未锁屏 | — | `OpenInputDesktop` + `UOI_NAME` | 本机实测 `Default` = 未锁；UAC / 远程桌面要能退化 |
| 6 | 交流供电 / 电池供电；电量 ≥ / ≤ N% | 供电方式或百分比 | `GetSystemPowerStatus` | **台式机无电池 → 电量项不可用**，UI 要禁用并说明，不能静默判 false |

**明确不放进 v1**（都不是「拿不到」，而是成本 / 价值不划算）：

- **CPU 封装功率（EMI）、整机墙上功率**：用户定「意义不大」。第 3 节调研保留备查；
  真想要整机瓦数得靠米家智能插座，而那已属于被排除的设备条件。
- CPU / 内存占用、音量 / 静音（COM）、GPU 占用（要厂商驱动）：本机未实测，
  且要么需要采样窗口、要么 ctypes 写 COM 较繁，先不做。
- 温度 / 风扇 / 主板电压 / SMART：必须外接 LibreHardwareMonitor / HWiNFO
  （Ring 0 驱动 + 管理员 + 常驻），正是用户说「麻烦」的那一类，**建议永不引入**。

边界：所有条件**只在热键触发瞬间求值一次**，不另起 timer、不加云端请求；某个 API
在本机不可用时该条件要**灰掉并说明原因**，不要当成「条件不成立」静默跳过，
否则用户会以为规则写错了。

### 4.2 「不做设备条件」这个判断成立

设备条件完全可以在米家 App 里由虚拟事件触发的那条自动化去验证——链路已经端到端
验证过：程序发事件 → 米家自动化检查「吸顶灯开着 且 传感器无人」→ 执行动作。
程序侧既不用读设备状态，也不用多一次云端轮询。**把条件推给米家，比在本程序里
复刻一套设备状态判断更稳**（本程序的属性快照还有 3s/15s 轮询延迟）。

### 4.3 与 11-10 旧草案的冲突，以本页为准

`docs/archive/todo-2026-11-15-full.md`「核心特性：把电脑接进全屋智能」里 2026-11-10 的草案写了很多第一版**不做**的
东西（进程/窗口/系统状态触发、锁屏/静音/启动程序等电脑侧动作、二级表达式）。
那些是当时的设想，**11-13 用户已把第一版收窄为**：触发只有热键、动作只有
发虚拟事件 + 延迟（11-13 夜补发音箱指令、11-14 补否则分支）、条件**做电脑条件但不做
设备条件也不做功率**，并补了热键防重（1.4）。动手前以本页第 1 节 + 4.1 为准，别照旧草案开工。

## 5. 未验证 / 待定

- [ ] **冷静期默认值**（推荐 1000 ms）要不要改；`MOD_NOREPEAT` 在本机注册后的实际行为
      （尤其「主键已按住才注册」那个例外），见 1.4
- [ ] 热键与系统 / 其它程序冲突时的提示文案，以及「裸单键」要不要限制；
      录制组合键时是否要暂停已注册热键
- [ ] 延迟期间窗口关闭 / 进托盘 / 程序退出，动作队列要不要继续（1.3）
- [ ] 六个条件在**别的账号 / 别的机器**上的退化表现：无电池的台式机、远程桌面、
      锁屏时 UAC 切到安全桌面、进程名重名
- [ ] 别的机器（尤其 Win10 / 虚拟机 / AMD 平台）是否同样暴露 EMI 设备；**v1 不需要**，
      仅备查，换机器跑一次 `probe_pc_state.py` 即可
- [ ] 锁屏的**实时事件**通知（`WTSRegisterSessionNotification`）未实测，本页只验了
      主动查询输入桌面；将来做「锁屏时触发」才需要它
- [ ] 音量 / 静音的 COM 实现（`IAudioEndpointVolume`）与 GPU 占用未测——v1 不需要
- [x] ~~「否则就执行」v1 已定不做~~ → **11-14 已补做**，见 1.2 与 `docs/dev/automation.md`
- [ ] 若日后重新启用 CPU 功率条件：瞬时 1 秒抖动大，可能要 5~10 秒滑动平均

## 6. 资料

| 内容 | 链接 |
| --- | --- |
| EMI 官方说明（驱动实现、IOCTL 流程、pWh 单位） | https://learn.microsoft.com/en-us/windows-hardware/drivers/powermeter/energy-meter-interface |
| `IOCTL_EMI_GET_MEASUREMENT` 官方页 | https://learn.microsoft.com/en-us/windows/win32/api/emi/ni-emi-ioctl_emi_get_measurement |
| `emi.h` 的 CTL_CODE 与结构体定义（mingw-w64） | https://raw.githubusercontent.com/mingw-w64/mingw-w64/master/mingw-w64-headers/include/emi.h |
| Python ctypes 读 EMI 的现成实现与 RAPL 通道命名 | https://ml.energy/zeus/reference/device/cpu/emi/ |
| Win11 内核经 EMI 暴露 RAPL、无需管理员 / 第三方驱动（社区实测） | https://github.com/mlco2/codecarbon/issues/457 |
| `GetLastInputInfo` 只给当前会话（微软 Old New Thing） | https://devblogs.microsoft.com/oldnewthing?p=112444 |
| 哪些状态 Windows 自带、哪些必须 LibreHardwareMonitor | https://lib.rs/crates/rigtop-gui |
| LibreHardwareMonitor 的 Ring 0 驱动 / 管理员要求 | https://librehardwaremonitor.net/ |
| **`RegisterHotKey` 会随自动重复连发；Win7+ 用 `MOD_NOREPEAT` 抑制**（Old New Thing） | https://devblogs.microsoft.com/oldnewthing/20171026-00/?p=97295 |
| `RegisterHotKey` 官方页（`MOD_NOREPEAT = 0x4000`、Vista 不支持） | https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-registerhotkey |
| Win11 build 26300 热键实测（主键已按住才注册仍会漏一次自动重复、修饰键比较时机等） | https://github.com/Ray-lea/Argus/blob/main/docs/design/hotkey.md |
| 本项目相关结论 | `mijia-automation-events.md`（4.2.1 端到端发送已验证）、`mihome-app/31_create_automation.jpg`（截图，本地已 gitignore） |
