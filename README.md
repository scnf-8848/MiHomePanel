# 米家控制台（MiHomePanel）

一个 Windows 桌面端的米家设备控制台。目标是**尽量贴近原版米家 App 的观感**，同时针对桌面
使用场景做取舍：横向空间用来放房间导航和更宽的卡片墙，而不是把手机界面等比放大。

> 非官方项目。通过社区逆向的米家 App 私有接口工作，账号凭据只保存在本机。

> **仍在开发中。** 手里的 56 台设备都是逐台验过的，没见过的品类只能走通用兜底——
> 控件能出、文案可能还是云端英文，欢迎反馈。界面层一直在改，配色和间距不保证稳定。

## 功能

**首页**

- 按房间分组的卡片墙：设备状态、产品图和常用开关在一屏内完成。
- 风扇宽卡可直接调扫风角度、模式和风速，不用先打开详情页。
- 卡片支持多种样式与尺寸，可按品类设置，单台修改不影响同类设备。
- 模式、档位、角度用分段轨道，亮度/风速用滑条，温度用步进器。
- 中枢网关/中控屏的极客版入口：一键获取并复制 6 位登录码，同时打开极客版页面。
- 多键面板按实体开关排布；转无线的按键会标出来并禁用，也可选择隐藏。
- 顶部场景快捷一点即执行，家庭切换可固定或自动把当前家庭排到最左。
- 设备可在首页拖动排序；家庭、房间、场景在「设置 → 排序」里调，顺序按云端 ID 保存。
- 隐藏页与排序页同构，分「家庭 → 房间 → 设备」和「家庭 → 快捷场景」两个 tab；关掉开关即隐藏，打开即恢复显示。
- 顶栏搜索按设备名、房间、品类或按键名过滤。
- 给小爱发文字指令：选择音箱与静默模式后输入一句话即可执行。
- 电脑自动化：用全局快捷键和电脑状态触发，执行米家虚拟事件、小爱指令或延迟；规则按当前家庭隔离。
- 一条规则可配多个触发，任一命中即执行；发虚拟事件时可选择当前家庭的中枢网关。
- 自动化规则可启停、编辑、删除，删除进回收站；日志可按关键字、规则、结果和时间筛选。
- 左侧房间导航与功能区；耗材、监控暂为占位，电脑自动化已可用。
- 同一时间只运行一个实例；关窗默认收进托盘，可改为退出程序。
- 按窗口是否可见自动调整刷新频率，在线/离线状态随云端回包更新；写入后只回读目标设备确认结果。
- 连接异常时顶部给出明确原因，恢复后原地转为正常提示。
- 快捷键：`Ctrl+F` 搜索、`Esc` 清空、`F5` / `Ctrl+R` 刷新、`Ctrl+,` 打开设置。

**详情页**

- 主控件、只读读数和「更多功能」分区展示，离线时控件禁用并显示数据更新时间。
- 超过 3 天未更新的读数会标出「N 个月未更新」。
- 枚举值显示中文短名，原始英文和故障码仍可在提示里看到。

**设置**

- 六个分类：外观、排序、隐藏、账号、常规、关于。
- 主题可切换浅色/深色，窗口大小、主题和当前家庭会记住。
- 账号页查看登录状态、打码账号和凭据位置，也可退出登录。
- 常规页支持开机自启、启动时进托盘、关窗行为；自启仅打包版可用。
- 关于页显示版本、GPL-3.0-or-later、源码/反馈地址和第三方组件说明。

**摄像头**

- 详情页和卡片都能播放实时画面，支持暂停、独立窗口和云台方向盘。
- 画面带声音，可开关；每次开播默认静音。
- 取流方式可选局域网优先、远程优先或仅远程。
- 打包版自带打过补丁的 go2rtc：首帧更快，并支持云台。

**界面**

- 自绘标题栏与圆角窗口，浅色/深色主题下保持一致。
- 滚轮只滚动页面，不会误改设备参数。

## 运行

**Windows 直接双击 `run.bat`。** 它会检查虚拟环境和 PySide6，缺什么装什么，然后启动。

```bash
# 等价的手动步骤
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m mihome
```

首次运行会弹二维码，用米家 App 扫码。凭据写在 `~/.config/mijia-api/auth.json`，
和 mijiaAPI 的命令行工具共用，不用重复登录。

依赖只有四个（`requirements.txt`，开发与发布用 Python 3.13）：

| 包 | 版本 | 用途 |
|---|---|---|
| mijiaAPI | >=5.0,<6 | 云端签名、加解密、token 刷新（GPL-3.0，决定了本项目也是 GPL-3.0） |
| PySide6 | >=6.7 | 整个界面 |
| qrcode / pillow | >=7.4 / >=10 | 扫码登录的二维码渲染与图片处理 |

「设置 → 账号」里能**退出登录**：会删掉那个 `auth.json`，然后直接拉起扫码窗口。
米家 App 和云端的登录状态不受影响。目前**同一时间只支持一个账号**，换号就是
「退出登录 → 重新扫码」。

摄像头画面需要额外装一个取流组件（可选，不装只是没有画面），见下文「摄像头」。

## 数据与隐私

全部本地文件都在 `%LOCALAPPDATA%\MiHomePanel\`：

| 路径 | 内容 |
|---|---|
| `prefs.json` | 用户偏好：排序、隐藏设备、卡片样式、主题、摄像头通道等 |
| `cache/spec/` | 设备规格 JSON 缓存（离线时也能画出控件） |
| `cache/icons/` | 产品图缓存 |
| `cache/capabilities.json` | 能力学习账本：确认不支持的属性记下来，之后跳过不再重试 |
| `logs/` | 自检结果、崩溃日志；轮询健康日志 `poll_health.log` 与上次运行快照 `poll_health.last.json`；摄像头取流计时（**默认不写**，见「摄像头 → 已知边界」） |
| `go2rtc/` | 可选取流组件与它的构建记录、许可证 |

登录凭据单独放在 `~/.config/mijia-api/auth.json`（Windows 上是 `C:\Users\<用户名>\.config\mijia-api\`），
由上游 mijiaAPI 管理，**不随安装包分发**，也不在程序目录里。

删掉 `prefs.json` 等于把界面设置恢复默认，不影响登录和设备缓存。

## 摄像头

米家摄像头没有公开的码流地址，走的是私有 P2P，厂商 SDK 有商业授权、不能进本项目
（GPL-3.0）的发行包。所以取流交给可选的独立进程 [go2rtc](https://github.com/AlexxIT/go2rtc)
（MIT），程序只消费 `rtsp://127.0.0.1:<port>/cam_<did>`。

**装一次即可**（约 7.7 MB，走 GitHub assets API 下载）：

```bash
.venv\Scripts\python.exe scripts\install_go2rtc.py             # 官方构建，首帧约 4.8–6.3 秒，**没有云台**
.venv\Scripts\python.exe scripts\install_go2rtc.py --patched   # 项目 Release 里的补丁版，首帧约 2 秒，带云台
.venv\Scripts\python.exe scripts\build_go2rtc.py               # 本机自己编同一份补丁版（需要 Go + git）
```

`--patched` 从本项目的组件 Release（tag 由 `build_go2rtc.REF` 推导为 `go2rtc-<ref>`，
当前 `go2rtc-990f4de`，已发布在
<https://github.com/scnf-8848/MiHomePanel/releases/tag/go2rtc-990f4de>）下载
`go2rtc_win64_patched.zip`，装 `go2rtc.exe` + `BUILD.txt`
+ MIT `LICENSE` 三件；Release 带 `SHA256SUMS` 时会核对哈希。Release 不存在、网络失败、
资产缺失、ref 不符或哈希不符都会给中文原因并回退：本地已有 go2rtc 就保留不动，
没有就尝试装官方版。发布与上游重新对齐的步骤见 [DEVELOPMENT.md](docs/DEVELOPMENT.md) 的「补丁版 Release」小节。

两个脚本都装到 `%LOCALAPPDATA%\MiHomePanel\go2rtc\go2rtc.exe`，也可以用环境变量
`MIHOME_GO2RTC` 指向别的副本；想回退到官方构建，把留存的 `go2rtc-official.exe` 改名
回 `go2rtc.exe` 即可。没装时画面区会直接写「没有找到 go2rtc」并给出安装命令，不会静默失败。

已知边界：

- 只做**实时画面 + 声音 + 云台方向**。画质切换、SD 卡回放、云存储录像已明确取消
  （理由见 [TODO.md](TODO.md)「摄像头二期」）
- **云台**是画面上的一个圆，上下左右四块：**点一下走一格，按住超过约半秒才开始连转**，
  松手停。详情页里排在画面下方；**独立窗口里是半透明、贴在画面左下角**（常驻，压在
  画面上但能看见底下的内容）。一格转多少由摄像头固件决定（约 15°），程序这边没有更细的
  步长可调。**要求用 `build_go2rtc.py` 装的那份 go2rtc**——官方构建里没有云台接口，
  方向盘整块不出现（不是灰的、不可点，是干脆不显示）。只做方向：预置位、巡航、
  跟随、变焦、拖动画面定位都没做
- **声音**每次开播都从静音开始（点播放不会突然出声），点右下角喇叭开；只控有无，
  没有音量、没有对讲。**这个选择只对这一次观看有效**：暂停/继续、换窗口（详情页 ↔
  独立窗口）会保持，重新点播放或重启程序都回到静音。静音只是关输出，不重建会话，
  点一下就出声，不用重新等 GOP
- 对一台摄像头只开**一条** P2P 会话；设备侧最多允许 **3 路并发**（实测第 4 路被设备
  掐断），所以和米家 App 同看没问题
- **暂停不等于断开**：暂停后设备仍在上行推流，也仍占一个观看位。所以详情页闲置
  3 分钟会自动释放连接，点播放重新取流
- 局域网首帧实测 **4.8–6.3 秒**（8 次实测，2960×1666 HEVC、官方构建；主要是等一个
  完整 GOP），装了开播补丁约 **2 秒**；跨网走 relay，首帧通常 **6–10 秒**
  （异地网络未做真实回归）
- 画面是 2960×1666 HEVC，默认**软件解码**：显存一紧时硬解会让进程直接消失，
  代价约单核 12%。想换回硬解设 `MIHOME_CAMERA_HW_DECODE=1`
- 音频疑似影响流畅度时可以设 `MIHOME_CAMERA_AUDIO=0` **整条音轨不取**（连喇叭键一起
  消失）。这是排障开关，不是静音开关
- 画面一直转圈 / 首帧特别慢时要排查，设 `MIHOME_CAMERA_TIMING=1` 再复现一次：
  `logs/camera_timing.log` 会记下从点播放到首帧上屏的每一步（含网卡、SSID、网段，
  用来判断走的是局域网还是中继）。这个日志**平时不写**，开着时 go2rtc 会以 trace
  级别运行、写得更啰嗦；单文件上限 2 MB，超了自动改名成 `.log.1`，只留一代。
  内容**只在本机、不上传**

## 打包成 exe

```bash
.venv\Scripts\python.exe scripts\build_exe.py
```

产物是 `dist\MiHomePanel\`（约 80 MB / 132 个文件，zip 后约 45 MB），**整个目录一起发**：

```
dist\MiHomePanel\
  MiHomePanel.exe
  _internal\            Qt / Python 运行时（PyInstaller 收集后裁掉了用不到的部分）
  go2rtc\               摄像头取流组件（外部进程，PyInstaller 不会自动带）
  LICENSE  NOTICE.txt   本项目 GPL-3.0 + 第三方组件说明
```

脚本结尾会**在产物上**跑两项检查，都过才算打完：`--self-check`（找得到 go2rtc、
HEVC 解得了）和一次**无控制台启动冒烟**。这两项都不碰摄像头，所以还要手动真播一次：

```
dist\MiHomePanel\MiHomePanel.exe --probe-camera            # 跑一次
dist\MiHomePanel\MiHomePanel.exe --probe-camera --rounds 3 # 冷启动三次看区间
```

`--console` 打带控制台的版本排障，`--onefile` 打单文件（启动慢），`--no-slim` 打完全不裁
的对照版，`--keep-sw-opengl` 保留软件 OpenGL 后备（**发到虚拟机 / 远程桌面时务必加上**）。

打包产物不含任何个人信息：凭据在用户目录，偏好和缓存在首次启动时才生成。

## 目前没做的

完整的待办在 [TODO.md](TODO.md)。近期明确排在后面的是：

- **把电脑接进全屋智能**：「电脑 → 米家」这半边已落地（上面「电脑自动化」）；
  「米家 → 电脑」、条件表达式 / 动态取值、进程启动类触发等仍是二期设想，
  草案与分期在 [TODO.md](TODO.md)
- **多账号切换**
- **桌面挂件**（复用现有卡片组件）
- 门锁开锁记录、人体触发这类**设备事件**：接口已经找到了（`/miotspec/event/get`），
  但**功能还没做**；读米家 App 自动化列表同样停在调研阶段（发"虚拟事件"触发自动化
  这一侧已经能用）
- **耗材监控页 / 独立监控页**：左侧功能区还留着两个占位按钮，页面本身还在规划。
  耗材页的定位是"用户自己添加设备与属性"，不做全屋自动识别；
  多摄像头监控页的位置也在这一区
- **局域网直连**（miIO / MIoT LAN）：**明确不做**。虽然能把响应压到几十毫秒，但要按设备型号
  各写一套本地会话，换台设备就失效——装到别人家里大部分设备都会失灵，不划算
- 摄像头**只想做实时画面 + 声音 + 云台方向**，到此为止；画质切换、回放、云存储
  都不在计划内（多路同屏还挂在 [TODO.md](TODO.md) 的低优先级里，家里只有一台能用的
  摄像头，没法验）
- **轮询提速**：常态**不做**。前台 3 秒 / 托盘 15 秒维持不变——设备上报走的是设备自己的
  上行配额，和客户端读属性不是同一配额池，不能互相推论；而且端到端 <1 秒的推送通道
  已经实测存在，轮询只是兜底。唯一的例外是写完之后对**那一台**设备做几次定向回读
  （写入确认），不改变全量轮询的 3 秒 / 15 秒
- **米家云 MQTT 推送**：仓库外的探针已经验证**能通**（55 台设备、端到端 <1 秒），
  但订阅要按设备白名单逐台来、实投 QoS 0，而且**拿不到设备上下线**（`state` 主题零
  消息）。缺口没补上之前不接进主程序

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
以及一长串实测踩过的坑，都在 **[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)**。
改代码前建议先看那份文档的「交互约定」与「踩坑清单」两节。

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
| [huanyuejue/MiHome-Windows](https://github.com/huanyuejue/MiHome-Windows) | GPL-3.0 | 起点参考项目：沿用单线程串行队列、子线程不直接回调 UI、乐观 UI + 版本号这三个判断，以及 16ms / 3s / 5min 定时器的分工。界面层完全重写，差异见[开发文档](docs/DEVELOPMENT.md#与参考项目的关系)。TODO 里的小爱文字指令、定时开关、托盘、桌面挂件也以它的功能清单为参照 | `已沿用` |
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
  中文一句话解析不需要模型。**只取思路、不复制代码**——AGPL 并进 GPL 发行包会让整体协议升级，
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

## 许可证

GPL-3.0-or-later。依赖的 [mijiaAPI](https://github.com/Do1e/mijia-api) 是 GPL-3.0，
本项目直接复用它的传输层，所以整体也必须是 GPL-3.0。第三方项目、各自许可证与
再分发边界见上节「开源致谢 / Credits」。
