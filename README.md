# 米家控制台（MiHomePanel）

一个 Windows 桌面端的米家设备控制台，非官方项目。

> **个人项目，按需维护。**
>
> 适配只能覆盖手上实际有的设备，作者没有的设备、型号只能走通用兜底规则——控件能出但只有基本功能、文案可能还是云端英文。
> 想要适配自己的设备，建议下载源码丢给 AI 改；理由和做法见文末[「维护与反馈」](#维护与反馈)。

## 下载与安装

到 [**Releases**](https://github.com/scnf-8848/MiHomePanel/releases/tag/v0.1.9) 下载
**`MiHomePanel-v0.1.9.zip`**（Windows 10 / 11 x64），解压后双击 `MiHomePanel.exe` 即可。
**不需要安装 Python，也不需要编译**：压缩包已自带打过补丁的 go2rtc，摄像头开箱即用。
下载页同时提供 `SHA256SUMS`，可用于核对压缩包完整性。

## 首次登录

首次启动会弹二维码，用米家 App 扫码。凭据由上游 mijiaAPI 写在用户主目录的
`.config\mijia-api` 下（Windows 上是 `C:\Users\<用户名>\.config\mijia-api\auth.json`），
与程序安装目录无关，也不会随安装包分发。

> 账号凭据只保存在本机。

「设置 → 账号」可以退出登录（会删掉这份凭据）再重新扫码。目前同一时间只支持一个账号，换号就是退出后重新扫码。

## 功能

### 首页

- 顶部导航栏切换家庭，左侧边栏切换家庭下的房间。
- 主窗口内显示按房间分组的卡片墙及快捷场景。
- 左侧下方是功能区：监控室、电脑自动化；分隔线下方是「耗材」、「桌面挂件」开关与「设置」——耗材不跟顶栏家庭，按全部家庭统计。
- 同一时间只运行一个实例；关窗默认收进托盘，可改为退出程序。
- 快捷键：`Esc` 收起消息浮层 / 清空指令条、`F5` / `Ctrl+R` 刷新、`Ctrl+,` 打开设置。
- 卡片墙上方是小爱文本指令条：左边选音箱，中间选「静默 / 不静默」，右边输指令回车发出。
- 顶部状态行显示当前家庭的设备总数与开启数，离线数和已隐藏数在非零时追加；连接出问题时上方会出横幅提示。
- 设备总数右侧显示**全屋温湿度范围**（如 `21.5-22.8°C`、`46-51%`），取当前家庭里各设备自己报的环境读数（温湿度计、空调室温、净化器、浴霸都算，被隐藏的设备也算），只有一台或数值相同时写单个值；一台都报不出来时这两颗胶囊整体不出现。鼠标停上去按设备列明细（数值在前、名字在后）。
- 顶栏这三颗胶囊左边各有一枚小图标：房子（在线设备）、温度计（温度）、水滴（湿度）。
- 顶栏这三块（在线设备 / 温度 / 湿度）都能在「设置 → 外观 → 顶栏展示」里单独关掉；窗口太窄时温湿度会先临时收起，拉宽自动回来。
- 顶栏右侧有一颗「消息」胶囊（铃铛）：**鼠标停上去就浮出**一张通知列表，移开自动收起——设备完成任务、滤芯到期、场景执行失败都在里面，只列**当前家庭**的。**只读**：不标已读、不删除，也不显示未读数（这里从不标已读，那个数字只会永远是"全部未读"）。云端只给最近几条，更早的记录和各场景的完整日志请到米家 App 看。
- 数据靠向米家云端轮询：窗口可见时 3 秒一轮，收进托盘 / 最小化后降到 15 秒。在线 / 离线状态跟着更新，发指令后几秒内能看到是否生效。

### 设备卡片

- 设备卡片可在首页拖动排序；家庭、房间、场景的顺序在「设置 → 排序」里调整。顺序按设备 id 记，**改名不会丢**。
- 卡片包含设备状态、产品图和常用开关。多键开关按实体结构排布按键，转无线的那几路会标出来并禁用。
- 右键卡片可以换**样式**，尺寸跟着样式走（1×1 / 2×1 / 2×2），单台修改不影响同类设备。不同品类可选的样式不同，例如：
  - 灯具：亮度、亮度 + 色温；
  - 空调：温度控制（未适配所有空调）；
  - 风扇：扫风 + 风速（未适配所有风扇）；
  - 多键开关：隐藏转无线（只留真正切继电器的那几路）；
  - 摄像头：实时画面（2×2，选它才在卡片里播，默认 1×1 是产品图）；
  - 中枢网关：极客版入口（小卡 / 宽卡）——一键取 6 位登录码并复制，同时打开极客版页面。
- 右键还有「强制刷新」（重查这台设备的云端能力，不是刷读数）与「重置设备配置」（清掉它的样式 / 隐藏 / 顺序）。
- 不需要显示的卡片可以隐藏：右键 → 隐藏设备，或在「设置 → 隐藏」里整组收起。
- 隐藏的设备统一放在卡片墙底部的「已隐藏设备」分组里，可收起或展开。

### 详情页

- 点击设备卡片弹出详情（模态窗，点压暗区或按 Esc 关闭），显示比卡片更多的数据与控件。
- 分三块：主控件、只读读数、「更多功能」（其余可写属性，折叠）。
- 长期未更新的读数会标出「N 天未更新」，避免把旧值当实时值。
- 枚举值显示中文短名，原始英文和故障码仍可在提示里看到。
- 摄像头在详情页里可以开关声音、开独立窗口。
- 部分未适配设备的显示效果可能不理想。

### 耗材

- 给部分设备做耗材余量提醒：由用户手动添加要监视的属性，一张卡对应一台设备的某一个数值属性。
- 三种卡片样式：横条 / 整卡染色 / 圆环（整页一个样式）。
- 三种排序：按余量、按房间分组、自定义拖动。
- 两档阈值：轻提醒 / 重提醒，可每台单独设定，留空 = 不启用那一档。
- 余量降到阈值时卡片变色，侧栏「耗材」旁边出现圆点计数。
- **不分家庭**：所有家庭一起显示，卡片副标题写「家庭 · 房间」；添加设备时按家庭→房间→设备选。
- 离线设备的读数会在右下角标出更新时间。

### 监控室

> 作者手里只有 1 台摄像头，满配效果无法实际验证。

- 多路摄像头同屏显示，五种预设：单窗口 / 左右两分 / 四宫格 / 一大两小 / 一大五小。
- 左侧一列画面源：每台摄像头一张卡（封面是它最后一次播放的模糊帧，存在本地，重启后仍在），拖到右边对应窗口内，单击放进第一个空格。
- **进页面不自动播**：格子里是封面 + 播放键，点了才连；地址会提前预热，点播放约 2 秒出画。
- 切布局、把画面拖到另一格都不重新取流，画面直接接着播；双击格子放大，「全部停止」一次停完。
- 同一台设备在详情页 / 独立窗口打开时，只腾出监控页那一格，其余继续播。

### 摄像头

- 打包版自带打过补丁的 [go2rtc](https://github.com/AlexxIT/go2rtc)（MIT），开箱即用：首帧约 **2–3 秒**（官方构建约 5 秒），云台方向盘可用。
- 支持实时画面、声音开关和独立窗口。声音默认静音，每次重新播放都回到静音（「我刚才开了声音」不会跨会话保留）。
- 多路同屏最多 6 路。
- 中继 / 通道策略、并发与暂停行为、解码与诊断日志等细节见[摄像头画面](docs/dev/camera.md)。

### 电脑自动化

- 参照米家 App 的自动化，用电脑上的动作触发，按规则执行设定的动作；规则每个家庭独立。
- 触发：键盘快捷键（单键或组合键）、本程序启动时、某程序启动 / 退出时。
- 动作：中枢网关类设备「发送虚拟事件」、智能音箱类设备「唤醒 / 播报文字 / 执行文本指令」、时间延迟；按顺序执行。
- 一条规则可配多个触发，任一命中即执行。
- 规则可启停、编辑、删除；删除后进回收站，可还原或彻底删除。
- 自动化日志记录每次触发执行，可按关键字、规则、结果和时间筛选；日志在本地（`%LOCALAPPDATA%\MiHomePanel\automation_log.jsonl`）。

### 桌面挂件

- 常驻桌面的快捷面板，通过左栏分隔线下方的「桌面挂件」一键开关。
- 三档层级：「普通」（默认）、贴到桌面图标层的「贴桌面」、「置顶」。
- 挂件内是紧凑卡片，可开关设备、点击进入详情页。
- 右上按钮打开管理窗，按「行 × 列」宫格编辑布局：拖拽换位、空槽留空。
- 设备可跨家庭选择。
- 右下角显示全部家庭的耗材圆点（灰 = 轻提醒，红 = 重提醒）。
- 挂件展开时按 3 秒刷新。

### 设置

- 七个分类：外观、排序、隐藏、设备、账号、常规、关于。
- **外观**：背景色（纯色 / 渐变两组合集）、浅色 / 深色模式、顶部家庭导航栏的样式。
- **排序**：两棵树——「家庭 → 房间」与「家庭 → 快捷场景」；设备顺序由首页拖动负责。
- **隐藏**：同样两棵树，开关打开 = 显示、关闭 = 收起；家庭 / 房间那一层的开关是整组批量。
- **设备**：部分类型设备的可选特性，目前只有摄像头取流方式：局域网优先 / 远程优先 / 仅远程。
- **账号**：登录状态、账号、凭据文件位置、退出登录，以及「全部重置」（清掉所有设备的本地样式 / 隐藏 / 顺序，不动云端与账号）。
- **常规**：开机自启、启动时最小化到托盘（只作用于开机自启那一次）、启动时恢复上次退出页面、启动时开启桌面挂件、关窗行为（最小化到托盘 / 退出程序；没有托盘时收进挂件，挂件也没开则退出）。
- **关于**：版本、许可、源码与反馈地址、第三方组件与致谢。

## 数据与隐私

程序数据都在 `%LOCALAPPDATA%\MiHomePanel\`：

```
prefs.json      界面偏好：家庭 / 房间 / 设备 / 场景顺序、隐藏、卡片样式
cache\          MIoT-Spec 原始规格、设备图标、监控封面——删了会重新拉
logs\           运行日志（启动 / 托盘 / 设置各一份）
devices\        设备档案：型号、规格、三条固件线的当前值与变更流（不可重建）
go2rtc\         可选取流组件
```

登录凭据单独放在 `C:\Users\<用户名>\.config\mijia-api\auth.json`。两者都只在本机，
不上传；删除 `prefs.json` 只会恢复界面默认，不影响登录和设备缓存。
具体目录结构与各文件用途见[架构与基础](docs/dev/architecture.md#目录结构)。

`devices\` 里的档案是唯一**重建不回来**的一份：云端只给「现在是什么版本」，半年后想知道某台设备当时是什么固件就查不到了。它记型号、规格与固件，不记房间、界面上没有入口，排查「某台设备突然不正常」时用 `scripts/device_history.py` 查。

**仓库里出现的家庭名、房间名、设备名、设备 / 家庭 id 一律是示例占位**（`示例家庭`、
`示例-客厅`、`1000000001` 这种，口径见 [AGENTS.md](AGENTS.md) 第三节），不是任何真实账号
的数据。米家 App 截图、`uiautomator` dump 这类采集原件不入库，仓库只留蒸馏后的 `.md`；
提交前跑 `python scripts/audit_privacy.py --staged`。

## 已知限制

- 电脑 → 米家自动化已落地（含「某程序启动 / 退出」触发）；米家 → 电脑、条件表达式 / 动态取值仍是二期。
- 同一时间只支持一个米家账号。
- 桌面挂件只做到 P0+（三档窗口、紧凑卡、开关、宫格槽位与管理窗、几何记忆）；折叠状态条、
  背景色 / 锁定、多屏夹回还没做。
- 设备事件（开锁记录、人体触发的次数统计）不展示：云端接口只回答"最近一次发生在什么时候"，
  不是历史；试过放在详情页左下角，效果不理想，撤掉了。读取米家 App 自动化列表也尚未实现。
- **设备清单只读一页（`limit=200`），超过 200 台会静默少设备**：云端这个接口的翻页游标没有公开文档，作者的两个家庭一共 64 台，远在限制内，所以没做翻页；超出的设备不会出现在卡片墙上，也不会报错。
- 消息中心只读、且只有"最近几条"：云端这个接口没有翻页游标（`limit` / `type` / `timestamp` 怎么传都不改变结果，实测只回 6 条），更早的记录、单台设备的历史和场景日志都要回米家 App 看。
- 没有搜索框（`Ctrl+F` 一并取消）：按名字找设备只能靠左侧房间导航和卡片顺序。
- 局域网直连明确不做。
- 摄像头只做实时画面、声音和云台方向；画质切换、回放、云存储不做。
- **监控页的多路效果没被完整验证过**：作者手上没有足够多的摄像头，`1+5` 六格满配、6 路
  并发时的 CPU 与首次全亮时长、异地 6 条中继同时跑，都没有实测数据。代码路径与上限
  有单元验证，但真机手感请以你自己的设备为准。

完整的待办、理由和边界见 [TODO.md](TODO.md)，完成记录见
[历史快照](docs/archive/todo-2026-11-15-full.md)；实现细节从
[DEVELOPMENT.md](docs/DEVELOPMENT.md) 进。

## 开发者方式：从源码运行

需要 Python 3.13。Windows 直接双击 `run.bat`，它会检查虚拟环境和 PySide6，缺什么装什么；
也可以手动：

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m mihome
```

依赖只有 `requirements.txt` 里的四个包（mijiaAPI、PySide6、qrcode、pillow），版本与用途见
[DEVELOPMENT.md](docs/DEVELOPMENT.md)。源码运行默认不自带取流组件；需要摄像头画面时运行
`.venv\Scripts\python.exe scripts\install_go2rtc.py --patched`（打包版已自带，可跳过）。
打包 exe、自检和真机探针见[打包与发布](docs/dev/packaging-release.md)与[摄像头画面](docs/dev/camera.md)。

## 开发文档

仓库分成四块，改哪块先看对应目录的模块说明（完整清单在开发文档里）：

```
mihome/core     云端访问、规格解析、状态机、适配规则等（除 automation 的 Qt 适配层外，无 Qt 依赖）
mihome/ui       全部界面，纯代码自绘，没有 .ui / .qss 文件
scripts         云端探测、离线验证、截图、打包、go2rtc 构建
docs            UI 规范与开发文档
```

注：`mihome/core/automation/service.py` 与 `mihome/core/automation/hotkeys_win.py`
直接 import PySide6，属于 **Qt 适配层**；其余 core 模块仍保持无 Qt 依赖。

架构、数据来源、适配层与枚举文案的设计、UI 约束、摄像头实现细节、打包裁剪依据，
以及一长串实测踩过的坑，都从 **[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)** 进。
改代码前建议先看[交互约定：滚轮只滚页面](docs/dev/ui.md#交互约定滚轮只滚页面)
与[踩坑清单](docs/dev/architecture.md#踩坑清单)。

想知道"某个功能技术上到底能不能做"（云端有没有这条接口、要不要抓包、局域网行不行），
先看 **[docs/reference/README.md](docs/reference/README.md)**——那是已经调研过的结论索引，
每条都标了状态（已落地 / 待排期 / 明确排除 / 仅参考）和落点。

## 开源致谢 / Credits

本项目不是从零写的：传输层、几个架构判断、规格解析思路，以及一批待办功能的可行性
结论都来自别人的开源工作。这里按**具体贡献了什么**登记，不按知名度排；许可证也一并
核实过，因为本项目是 GPL-3.0，能不能抄代码、能不能进发行包全看这一栏。

**登记规则**：新增依赖 / 移植 / 借鉴 / 协议参考，在引入那一轮就补这张表，流程见
[TODO.md](TODO.md) 末尾的「工程约定：开源致谢流程」。状态只用这五种：
`已依赖` = 运行时代码真的在用；`已沿用` = 吸收了架构判断、未复制代码；
`仅参考` = 读过实现或文档、还没写进代码；`拟定方案` = 打算照它做、还没实现；
`明确排除` = 读过但不纳入发行包。

### 已经在代码里

| 项目 | 许可证 | 具体贡献 | 状态 |
|---|---|---|---|
| [Do1e/mijia-api](https://github.com/Do1e/mijia-api)（PyPI `mijiaAPI`） | GPL-3.0-or-later | 云端传输层：扫码登录、`gen_nonce` / RC4 签名与加解密、`serviceToken` 刷新、`/miotspec/*` 封装；`core/api.py` 和 `scripts/probe_geek_passcode.py` 直接调用。**它决定了本项目也必须是 GPL-3.0** | `已依赖` |
| [huanyuejue/MiHome-Windows](https://github.com/huanyuejue/MiHome-Windows) | GPL-3.0 | 起点参考项目：沿用单线程串行队列、子线程不直接回调 UI、乐观 UI + 版本号这三个判断，以及 16ms / 3s / 5min 定时器的分工。界面层完全重写，差异见[开发文档](docs/dev/architecture.md#与参考项目的关系)。TODO 里的小爱文字指令、定时开关、托盘、桌面挂件也以它的功能清单为参照 | `已沿用` |
| PySide6 / Qt for Python | LGPL-3.0 / GPL-2.0 / GPL-3.0 三选一 | 整个 UI 框架 | `已依赖` |
| [Material Icons](https://github.com/google/material-design-icons) | Apache-2.0 | 顶栏「刷新」按钮的图标形状：`ui/main_window.py::_refresh_path` 按它的 `refresh` 字形（24×24 视口）逐段抄写坐标后缩放，形状未改 | `已沿用` |
| `qrcode`（BSD）、`Pillow`（MIT-CMU）、`pycryptodome`（BSD-2-Clause / Unlicense / MIT） | 见 `requirements.txt` 与各发行包 | 二维码渲染、图片处理、X25519（探测 `miss_get_vendor` 时用过，正式引入要显式声明）。经 pip 安装，不随本项目分发 | `已依赖`；X25519 那条是 `仅参考` |

> 参考副本**不在本仓库里**（它自己的 UI 依赖 `H1DDENADM1N/PySide6-SiliconUI`、`qtawesome`
> 等，本项目**没有**沿用，故不单独登记）。本机留了一份在 `..\_refs\MiHome-Windows-0.5.0\`
> 供核对致谢，已被 `.gitignore` 排除，不进版本库也不进发行包。
> 设备能力数据取自小米官方 [MIoT-Spec / miot-spec.org](https://miot-spec.org) 与米家 App
> 私有接口——它们是规范和数据服务、不是开源项目，在此注明来源。

### 摄像头画面（已实现）

最终实现**没有自研协议栈**：由 go2rtc 负责 P2P/CS2 → 本机 RTSP，Qt 侧只解码渲染，
代码在 `core/camera.py` 与 `ui/camera_view.py`。下表按实际采用情况登记：

| 项目 | 许可证 | 具体贡献 | 状态 |
|---|---|---|---|
| [AlexxIT/go2rtc](https://github.com/AlexxIT/go2rtc) | MIT | **实际采用的取流组件**：可选外部进程，把 P2P/CS2 码流转成本机 RTSP；程序只消费 `rtsp://127.0.0.1:<port>/cam_<did>`。由 `scripts/install_go2rtc.py` 下载官方构建，或 `scripts/build_go2rtc.py` 按 `scripts/patches/` 里两个补丁（`go2rtc-keyframe.patch` 12 行开播、`go2rtc-ptz.patch` 云台接口）**自行编译到 `<data>/go2rtc/`**，**不随仓库分发**（打包时可随程序目录附带，需一并附上其许可声明） | `已依赖` |
| [Reign9201/mihome-mac](https://github.com/Reign9201/mihome-mac) | GPL-3.0 | 协议蓝本：`core/src/` 的 X25519→ChaCha20、PPPP 帧、relay 状态机、MISS 认证与取流，以及 `docs/04`、`docs/06` 里的字节序和坑；**`chuangmi.camera.069a01` 由该项目真机验证出画**。本次只借判断、没有移植代码 | `仅参考` |
| [DHJ999/mi-home](https://github.com/DHJ999/mi-home) | 注意：应用层**无开源许可**（GitHub 标 MIT 是误判，仓库只有 `LICENSE-go2rtc.txt`） | 只借接口约定、不抄码：`app/go2rtc.mjs` 的 `xiaomi://` 流源、`/api/streams`、`/api/xiaomi/recordings` 回放参数，以及云台按住连转、回放时间轴的交互 | `仅参考` |
| [Songmanual/MijiaDeck](https://github.com/Songmanual/MijiaDeck) | GPL-3.0 | 同源 Python/PySide6 的 `camera_preview.py` / `camera_access.py` 集成形态；它的请求写法补上了我们缺的 `app_pubkey`，P2P 授权接口因此打通 | `仅参考` |
| [dashi45/mihome-pc](https://github.com/dashi45/mihome-pc) | MIT | `camhls.py` 的 go2rtc → HLS 播放链路 | `仅参考` |
| [LiuLiuxc/FnDepot](https://github.com/LiuLiuxc/FnDepot)（miNVR） | 无 LICENSE、闭源 | 只看产品形态：九宫格、事件时间轴、分段录像 | `仅参考` |
| [al-one/hass-xiaomi-miot](https://github.com/al-one/hass-xiaomi-miot) | Apache-2.0 | 云录像链路（`get/eventlist` → `m3u8` → `mp4` / `img` 与 AES 解密），以及「`stream_address` 路线不适用」的判定依据 | `仅参考` |
| IMILAB「IMI Camera」Windows 客户端 | 闭源商业软件 | 静态分析其日志与 PE 字符串，确认 `sid=xiaomiio` 换领、`/device/devicepass`、TUTK/MISS 打洞的完整链路 | `明确排除` |
| [eugeneRover/micam](https://github.com/eugeneRover/micam)（MijiaDeck 的 Java 后端） | 无 LICENSE | 上游再分发许可未确认 | `明确排除` |

> TUTK/MISS 厂商 SDK、micam JAR、`go2rtc-cs2.exe` 一律**不随 GPL 发行包分发**。
> 这里只用官方 go2rtc（MIT）当外部进程，二进制由安装脚本按需下载。

### 同一批项目对其它待办的贡献

链接见上表，这里只记落点（除注明者外均为 `仅参考`，尚未写进代码）：

- **mihome-mac**：多账号隔离（`docs/09-multi-account.md` 的 accounts.json 0600、
  自动化按 `sha256(uid)` 分开、切换账号前停摄像头）；局域网设备 token 取自
  `cloud.rs::device_network()` 的 `/home/device_list`（局域网直连已于 2026-11-10 明确排除，
  这条只作为"token 确实在设备列表里"的佐证留档）。
- **MijiaDeck**：**小爱文字指令的 `execute-text-directive` action 用法 —— `已沿用`
  （2026-11-11 落地为 `core/speaker.py`：只借"有这么一个 action、入参是文本+静默"
  这个事实，能力号与入参顺序全部由本机规格自行解析，没有引入对方代码）**、
  `app/core/diagnostics.py` 脱敏诊断报告、`app/core/spec_zh.py` 枚举中文短名、悬浮托盘。
- **dashi45/mihome-pc**：能耗数据源 `/v2/user/statistics` + `get_device_data_raw`、
  `cardsort.js` 长按拖动、`tray.py` / `suspend.js`、`evtlog.py` / `scheduler.py` /
  `procman.py` / `netguard.py` 的规则引擎触发源与重试策略、能力三态与分层轮询。
- **FnDepot / miNVR**：按天回放与事件时间轴的产品形态。

### 调研文档引用过的项目

`docs/reference/mijia-geek-login-code.md` 第 8 节的资料表同样属于致谢范围，登记在此免得漏：

- [HZDavy/OpenMexia](https://github.com/HZDavy/OpenMexia)（**AGPL-3.0-or-later**）：另一个米家桌面端
  （Electron/TS）。读它的实现得出三个判断：在线状态该随探测回写（含"云端对离线设备回放缓存值"
  这个坑）、本机 HTTP 联动接口的发现机制（随机端口 + 启动令牌 + 发现文件 + Bearer）、
  中文一句话解析不需要模型（**这条已明确排除**，2026-11-14：与顶栏音箱指令执行域重叠、
  程序内动作点鼠标更快）。**只取思路、不复制代码**——AGPL 并进 GPL 发行包会让整体协议升级，
  所以它不能变成依赖。记录见 `docs/reference/openmexia-notes.md`，状态 `仅参考`
- [eyaeya/xiaomi-central-hub-gateway-cli](https://github.com/eyaeya/xiaomi-central-hub-gateway-cli)（GPL-3.0）：云端取址 / 取码 RPC 契约；极客版节点目录里的 `deviceInput` 事件语义（`did/siid/eiid + arguments` 过滤）
- [ChishFoxcat/MIJIA_GEEK](https://github.com/ChishFoxcat/MIJIA_GEEK)（无 LICENSE）：油猴脚本抓包重放思路
- [609293964/MIJIA_GEEK-for-fnos](https://github.com/609293964/MIJIA_GEEK-for-fnos)（无 LICENSE）：飞牛版 HAR 自动解析
- [weieast1314/oh-my-sage](https://github.com/weieast1314/oh-my-sage) / [allocnode/oh-my-sage](https://github.com/allocnode/oh-my-sage)（均 MIT）：网关本地协议与「常驻服务只登录一次」
- `docs/reference/mijia-automation-events.md` 第 10 节的资料表（虚拟事件用法、`GetSceneList` 抓包整理）同样在此致谢；其中 `imkero.net` 与 `phuker.github.io` 是个人博客文章，`tsight.io` 是分析文章，**只取事实、不抄码**
- 小米官方 MIoT-Spec RPC 文档、`asmslight.com` 操作路径：非开源项目，仅作资料

`docs/reference/drag-sort-spec.md` 是用户提供的《拖动排序实现方案》行为契约，原始出处
未标注；只作规范参考，真要照它抄实现时先补出处并登记。`docs/reference/米家App-UI风格参考.md`
是从自家真机采集的，不属于第三方成果；其中引用的米家 App 原始截图**不随仓库分发**
（他人作品版权 + 截图含账号/家庭信息）。

## 维护与反馈

这是一个**个人自用**的项目：代码是按我手上有的设备写的，维护节奏也按我自己的需要来。
我不能保证持续更新，也不排除在满足个人需求之后就此停更。

项目里有大量**针对具体设备的适配**（型号差异、固件差异、云端字段差异都得逐台对）。
你手上的设备没被适配、或适配不正常时，我这边没有那台设备，就没法复现、没法验证，这类
问题通常只能由你自己解决。更实际的路径是**下载源码自己改**：项目是 GPL-3.0-or-later，
允许修改与再分发，**不必事先征求同意**；把源码交给你的 AI 助手、按你的需求去调整，
通常比等我有设备快得多。适配入口集中在 `mihome/core/adapters/` 与
`mihome/core/automation/action_catalog.py`，取舍与踩过的坑记在 `docs/dev/automation.md`。

**动手前先看 [已适配型号台账](docs/reference/model-adapters.md)**：里面列了每个适配过的
型号、用了哪种适配方式、为什么，以及"评估过但决定不做"的型号（避免重复评估）。
找同厂商/同形态的那台当模板，照字段加一条即可，通用逻辑不用改；
`python scripts/list_adapters.py` 也能直接打印这份清单。

例外是**严重问题**——凭据泄露、数据丢失、程序起不来、影响系统安全这一类，欢迎到
[Issues](https://github.com/scnf-8848/MiHomePanel/issues) 反馈。

## 许可证

GPL-3.0-or-later。依赖的 [mijiaAPI](https://github.com/Do1e/mijia-api) 是 GPL-3.0，
本项目直接复用它的传输层，所以整体也必须是 GPL-3.0。第三方项目、各自许可证与
再分发边界见上节「开源致谢 / Credits」。
