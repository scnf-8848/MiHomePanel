# 桌面挂件：调研与方案（2026-11-20）

> **状态：待拍板 / 待排期，未写代码。**
> 参考项目：`huanyuejue/MiHome-Windows` 0.5.0（GPL-3.0-or-later，仓库里已有的
> 「起点参考项目」）。本文只参考它的**交互与取舍**，代码按本项目的窗口 / 卡片 /
> 数据体系重写——直接搬会带来第二套状态同步与配色，反而破坏一致性。
> 需要复用的思路在文末标了出处，若最终照抄了某段实现，按 `README.md`「开源致谢」
> 登记（GPL 兼容，但仍要与代码同轮提交）。

---

## 0. 一句话方案

一块**常驻桌面的紧凑控制面板**：无边框、**实心底板 + 圆角**（不做半透明，理由见 §3.2.2）、
**默认挂进桌面（置底，永不挡窗口）**，一行 1–6 张 56 px 高的设备卡（名称 + 房间/读数 + 电源键），
点卡片唤起主窗口并打开详情，点电源直接写设备；可收起成 32 px 状态条；头部带耗材
两档计数圆点。配置进 `prefs.json`（v17），不新建第二套配置文件。

层级不是"图钉开关"，是一个**图层按钮**：按下弹菜单，里面三档任选（贴桌面 / 普通 /
置顶），当前档打勾。默认「贴桌面」。

**明确不做**：壁纸层（桌面图标之下）、挂件里播视频、逐路多键开关、第二套设备详情。

---

## 1. 参考项目怎么做的（读代码，不是看 README）

### 1.1 体量与分工

| 文件 | 行数 | 职责 |
|---|---|---|
| `app/ui/widgets_board.py` | 2190 | 窗口、卡片、折叠条、拖拽、透明度、层级、重排全在里面 |
| `app/ui/board_manager.py` | 905 | 管理对话框：设备 / 场景 / 外观三页，勾选 + 实时预览 |
| `app/core/board_store.py` | 315 | `board.json` 读写，逐字段钳制与迁移 |
| `tests/board_test.py` | 1413 | 十组回归：存盘、几何、透明度、折叠、拖拽、接线、背景色 |

它的挂件与主界面是**两套视图**：主窗口把 `DeviceInfo` 列表与 `known_power` 推给挂件，
挂件自己建一套紧凑卡片；开关成功后回传 `power_changed`，让主窗口的 `_known_power`
同步（否则轮询会拿旧记忆覆盖刚点的结果）。

### 1.2 窗口与层级（最容易踩坑的一段）

- 标志：`FramelessWindowHint | Tool`（不占任务栏）+ `WA_TranslucentBackground`。
- **默认不置顶**（用户明确反馈过"常驻置顶会挡住别的窗口"）。非置顶时再加
  `WindowDoesNotAcceptFocus`，点挂件不抢当前窗口的焦点。
  （本项目 11-20 的结论是**默认挂进桌面层（置底）**——同样"永不挡窗口"，但比它彻底：
  按 Win+D 还在，见 §3.2）
- Qt 的 `WindowStaysOnBottomHint` 在 Windows 上**不生效**：非置顶窗口一旦被点击或
  重新 `show()` 就会浮上来。它直接调 Win32：

  ```
  SetWindowPos(hwnd, HWND_BOTTOM / HWND_TOPMOST, 0,0,0,0,
               SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
  ```

  在 `showEvent` 后 `QTimer.singleShot(0, ...)` 归位，并在
  `ActivationChange`（窗口被激活）后再压一次。
- **Qt 在 Windows 上改 `setWindowFlags()` 会销毁并重建原生窗口**：窗口被隐藏、
  位置尺寸回默认。所以切换置顶必须"记住可见性与几何 → 改标志 → 重新 show →
  还原几何 → 恢复透明度 → 归位 z-order"，少一步就跳到屏幕左上角。

### 1.3 几何：窗口大小由内容算，不允许手动拉

- 宽度：`148 × 列数 + 6 × (列数-1) + 20`，再按屏幕可用宽收住（最小 320）。
- 高度：头部（22 px 把手 + 边距）+ `56 × 行数 + 6 × (行数-1)`；超过屏高 −80 就封顶，
  由 `QScrollArea` 接管纵向滚动。
- 卡片数量变化 → 重算宽高；`setMinimumSize(0, 0)` 必须显式设：布局算出的最小宽会被
  Qt 回写并缓存，上一轮宽状态留下的 165 px 会把单列 148 px 顶回去（实测复现）。
- 落盘几何要 **sanitize**：`screenAt(x+40, y+10)` 找不到屏就贴回主屏右上角；部分越界
  只把位置钳回可见范围。拔副屏 / 改分辨率 / 改缩放的场景，窗口是无边框 Tool，
  跑出屏幕后用户既看不见也点不到，只能删配置。
- 拖动时位置**防抖 400 ms 落盘**（`moveEvent` 每帧写会拖慢跟随）。

### 1.4 卡片与交互

- 卡片 `148 × 56`：设备名（单行省略）+ 副标题（房间 / 读数 / 离线）+ 28 px 电源钮。
  电源钮在探测确认设备**有可写开关**之后才创建，避免留一个永远点不动的按钮。
- 点卡片空白 → 打开详情；点电源钮 → 直接写，且卡片自己消费这次点击。
- 场景卡同构，右侧换成"执行"钮；设备与场景按一条 `order` 混排。
- **折叠条 32 px**：宽度锁定为展开态宽度（曾经按台数加宽，20 台时横条 497 px 把桌面
  分区撑开，而展开态只有 188 px）。里面每台一个状态点，9/7/5 px 逐级缩，仍放不下改
  聚合比例条。单击展开、拖动移动，靠 5 px 位移阈值区分——一按就展开的话永远拖不动。
- 透明度 `0.30–1.00`（默认 0.92），**鼠标进入恢复 1.0**（180 ms 过渡），移开回落；
  管理对话框打开期间暂停"悬停恢复"，否则预览值会被顶回 1.0。
- 锁定：隐藏标题栏、禁止拖动与折叠，悬停时右上角浮出小锁作为唯一可见解锁入口。
- 卡片可直接拖动排序：8 px 阈值、幽灵标签 + 落点高亮 + 松手落盘。

### 1.5 配置：一个 `board.json`

字段：`enabled` / `devices` / `columns`(1–9, 默认 3) / `opacity`(0.30–1.00, 0.92) /
`geom` / `collapsed` / `bg`(`#rrggbb` 或 `null`=跟随主题) / `topmost`(默认 false) /
`scenes` / `order` / `locked`。旧版的 `autostart` + `visible` 会被折算成唯一的
`enabled`——两个开关叠在一起时"勾了自动显示却关不掉"的歧义就从这里来。

**点 × 只是本次隐藏，不改 `enabled`**：下次启动仍按设置出现。

### 1.6 它踩过、我们也会踩的坑

1. 无边框 Tool 窗口丢到屏幕外 → 只能删配置；
2. 改窗口标志把几何打回默认；
3. 折叠条被旧的 layout 最小宽顶大（148 → 165）；
4. 非置顶窗口被点一下就浮到最前（Qt 的 bottom hint 在 Windows 无效）；
5. 原生 `QToolTip` 在半透明窗口里渲染成全透明 / 黑底深字，只能自绘；
6. 全局 `windowOpacity` 会把**文字一起**变淡（我们打算只淡化背景，见 3.6）；
7. 折叠条按台数加宽会撑开桌面分区；
8. 管理对话框打开时"悬停恢复"把预览透明度顶回去。

第 5 条本项目已经有解：`ui/tooltip.py` 就是统一的自绘提示，挂件直接接它；
第 6 条是我们准备偏离参考项目的地方。

---

## 2. 我们和它的结构差异（决定了不能照抄）

| 维度 | 参考项目 | 本项目 | 对方案的影响 |
|---|---|---|---|
| 数据 | 主窗口把设备列表 + `known_power` 推给挂件，自己维护版本号防回弹 | `Store` 是唯一数据源，`on_changed` 统一通知，乐观写入已在 core 里 | 挂件**直接持 `Store`**，不复制状态、不做版本号；开关走 `store.toggle` 即可 |
| 卡片 | 自己的 `_BoardCard` | `DeviceCard` 166×168，模板 / 多键 / 滑条，太重 | 新建 158×56 紧凑卡；副标题复用 `catalog.summary_line`，多键摘要抽成共享函数 |
| 详情 | 独立 `DeviceDialog` | `DetailDialog` 是模态，`_run_dialog` 会压暗主窗口并停画面 | 点卡片**先唤起主窗口**再开详情，不做第二套弹窗生命周期 |
| 轮询 | 主窗口按"可见性"改定时器 | `poll_demand` 需求登记表，已为挂件预留 | 挂件登记 `owner="widget"`，展开 3 s / 折叠 8 s / 隐藏撤销 |
| 偏好 | 每功能一个 JSON | `prefs.json`（v16）+ `QSettings`（窗口几何 / 主题） | 挂件配置进 `prefs` v17，测试用临时 `Prefs(path)` 最省事 |
| 窗口骨架 | 自绘 | `chrome.WindowFrame` 带 **24 px 阴影留白** | **不能直接套**：透明留白仍属于窗口，会变成一圈不可见点击热区 |
| 图标 | qtawesome | 全项目自绘，不引新依赖 | 头部按钮用 QPainter 画，或复用 `theme` 里的图形 |
| 耗材提醒 | 环境胶囊里一个数 | 侧栏两档圆点，`ConsumablesPage.counts()` 已有 | 头部画两枚圆点，主窗口转发即可（不开新提醒通道） |

---

## 3. 方案

### 3.1 产品边界

**做**：常驻桌面的控制面板；自选设备；电源开关；点卡片进详情；耗材两档计数；
收起成状态条；背景透明度 / 列数 / 置顶 / 锁定 / 背景色；位置与状态记住。

**不做**（写进 README「已知限制」或本节即可）：

- **壁纸层**（压在桌面图标下面）：Qt 窗口做不到，需要注入 Explorer，明确排除；
- **挂件里放视频**：每路软解约 40% 单核、还要占一个观看位，与"扫一眼"的用途相反；
  要看画面去监控页（`docs/dev/camera.md`）；
- **多键面板逐路控制**：紧凑卡放不下 3 路按键；只显示「3 路 · 1 开」，要点进去；
- **第二套详情弹窗**：详情必须走主窗口那一条，避免两处各自处理摄像头交接与压暗；
- **键盘操作 / 输入**：挂件不抢焦点，不做文本输入，不承诺 Tab 导航。

### 3.2 窗口形态与层级

- `FramelessWindowHint | Tool` + **实心底板**，圆角 14 靠 `SetWindowRgn` 裁，1 px 描边；**不套 `WindowFrame`**
  （阴影留白 24 px 会变成不可见点击区），需要一点投影就在自绘里留 6 px 并画淡影。
- 三档层级，**默认「贴桌面」（置底）**（细节与实测依据见 §3.2.1）：

  | 档位 | 行为 | Win+D | 实现 |
  |---|---|---|---|
  | **贴桌面（置底，默认）** | 挂进桌面图标层，只在桌面露出来时可见，**永不挡窗口** | **可见** | `SetParent(hwnd, SHELLDLL_DefView)` + `SetWindowRgn` 圆角 + 提到图标列表之上 |
  | 普通 | 和别的窗口一样排队，不压人；点它不抢焦点 | 被桌面盖住，再按一次原样回来 | 不加任何 z-order 标志 |
  | 置顶 | 一直在最前，像贴在屏幕上的便签 | **仍可见** | `SetWindowPos(HWND_TOPMOST)` |

  切档入口统一是**头部那个图层按钮**：按下弹三项菜单、当前档打勾（不是循环切，见 §3.5）。
  三档的窗口形态**完全一样**（不透明 + 圆角 region），所以切档只动父子关系与 z-order，
  **不需要重建原生窗口**。

### 3.2.1 层级与「显示桌面」：三档的边界（2026-11-20 重测，结论已翻新）

**一句话结论**：置顶 = `WS_EX_TOPMOST`；普通 = 什么都不加；**贴桌面（置底，默认档）= 把
窗口挂进 `SHELLDLL_DefView`（图标层）**。关键纠正：**挂到 `Progman` 或 `WorkerW` 里画不出来，
挂到 `SHELLDLL_DefView` 里能画出来**。上一版文档把"挂桌面"整条判死，是宿主找错了。

| 档位 | 屏幕可见 | Win+D 之后 | 点击能否到挂件 | 挡不挡别的窗口 |
|---|---|---|---|---|
| **贴桌面（默认）** | 桌面露出时可见 | **可见**（两次切换都取到自己的颜色） | 桌面激活时到 | **永不挡** |
| 普通 | 是 | 被桌面盖住，再按一次原样回来 | 到 | 不挡 |
| 置顶 | 是 | **仍可见**（取色不变） | 到 | 压在普通窗口之上 |

#### 宿主对照：同样 SetParent，宿主不同结果完全相反

| 宿主 | 画面 | 点击命中 | 说明 |
|---|---|---|---|
| `Progman`（提到子窗口堆顶） | **不画**（取到壁纸色） | 命中是我们 | 自建 GDI 窗 / 系统 `Static` / Qt 窗口，三种都不画 |
| `Progman` 下那个全屏 `WorkerW`（壁纸画布） | **不画** | 命中是 `SysListView32` | 壁纸软件在这个宿主上能画，是因为它们用 D3D 交换链，不是 GDI |
| **`SHELLDLL_DefView`（图标层）** | **能画** | **命中是我们** | 排在 `SysListView32` 之上；唯一可行宿主 |

Win+D 的真实机制（和实测一致）：它不隐藏、不最小化任何窗口，只把桌面窗口**抬到 z 序最上**。
普通窗口因此被"盖住"——`IsIconic` 仍 False、`DWMWA_CLOAKED` 仍 0、`IsWindowVisible` 仍 True、
几何一点没变；再按一次桌面沉回去，挂件原样露面。贴桌面档本身就是桌面的一部分，所以全程都在。

#### 「贴桌面」档的硬约束（照做才安全、才画得出来）

1. **只 SetParent 自己的窗口 + `SetWindowPos(自己, HWND_TOP)`**。对 `SHELLDLL_DefView` /
   `SysListView32` 做任何 `SetWindowPos`（改 z 序或尺寸）都不许——上一轮把用户桌面写坏，
   就是那句"把 DefView 压到我下面"。
2. **SetParent 之后必须让 Qt 事件循环跑一次**（`update()` + `processEvents()`）。否则重绘
   事件不派发，看起来"挂进去画不出来"——我们为此误判了一整轮，还错怪了 Qt 的渲染后端。
3. **坐标是 DefView 的客户区坐标**（它覆盖整个虚拟桌面，原点即屏幕 0,0）；多屏要按虚拟桌面
   边界换算，不是按主屏。
4. **必须有看门狗，而且两条判据都要**（explorer 重启实测，见下一节）：
   - `IsWindow(hwnd) == False` → 窗口**已被销毁**，必须**重建**（重挂不回来，句柄都没了）；
   - `GetParent(hwnd) != 当前 DefView` → 宿主换了，重挂 + 提顶。
   **每轮都要重新解析 DefView**（`FindWindowEx(Progman, "SHELLDLL_DefView")`），不要在挂载时
   缓存句柄：实测一次 explorer 重启过程里 DefView 换了两次句柄（`0x205A6 → 0x3062C → 0x4050E`），
   缓存会漏。节奏 2 s 一次就够（两次 API 调用，开销可忽略）。实测 **F5 刷新桌面、点桌面空白
   都不会把它顶掉**。
5. **菜单不用管**：`Menu` 是 Qt 的 popup，本身带 `WS_EX_TOPMOST`，Win+D 状态下不被桌面
   盖住、菜单项能点、关掉后挂件不被抬升（都实测过）。owner 就是那个 `WS_CHILD` 挂件，
   位置也对（偏差 dx=0 dy=1），不需要改无 parent + 屏幕坐标。
6. **点击前提是桌面已激活**：实测桌面未激活时第一次点击只做激活，Qt 收不到
   `mousePressEvent`；桌面激活后点击正常到达（`presses=1`）。日常"先点桌面再用挂件"够用。
7. **退出或切档前先 `SetParent(hwnd, None)`** 还原成普通窗口再销毁，别把子窗口留在桌面里。
8. **整个挂件都不能带 `WS_EX_LAYERED`**（`WA_TranslucentBackground` 必然带它）：带了挂进
   `DefView` 就**整窗不渲染**，去掉立刻能画，整窗 alpha 也救不回来。所以本项目不做半透明，
   圆角靠 `SetWindowRgn`。详见下面「绘制：整个挂件都不做半透明」。

#### explorer 重启：挂件会被一起销毁，普通/置顶档不受影响（2026-11-20 05:0x 真重启实测）

两个探针真跑了 `taskkill /f /im explorer.exe`（`scripts/out/probe_reattach.py`、
`probe_reattach2.py`，日志 `*.log`），而不是模拟：

| 观测 | 结果 |
|---|---|
| 挂进 `DefView` 的窗口，explorer 一死 | **0.03 s 内被销毁**：`IsWindow(hwnd) = False`、`GetParent = 0`。跨进程子窗口会跟着父窗口一起销毁，没有"提前摘父"的机会 |
| 宿主进程（我们的 Qt 程序） | **没崩**，但 Qt 侧完全没察觉：`isVisible()` 仍 True、`winId()` 仍返回那个**已死的句柄**、`paints` 还在涨 |
| 普通档 / 置顶档的窗口 | **不受影响**：`IsWindow` 为真、几何不变 `(2450,525,525,158)`、`isVisible` 真 |
| `AutoRestartShell=1` 时 `taskkill /f` | **20–25 s 内 shell 没有自己回来**（两次都没），要手动 `explorer.exe` 才恢复 → 不能指望自动重启 |
| 新 shell 起来后 | `DefView` / `ListView` 都是**新句柄**；`FindWindowEx(Progman, "SHELLDLL_DefView")` 照常能拿到 |
| 重建路径 | 旧 QWidget `hide() + deleteLater()` → 新建 → `SetParent(新 DefView)` → **重设 region** → 画面正确（中心 `(28,30,36)`、圆角外透出壁纸、`paints` 增长） |
| `TaskbarCreated` 广播（`RegisterWindowMessage` = `0xC0CB`） | 注册得到，但广播只发给**活着的**顶层窗口，我们的窗口此刻已经死了 → **收不到**，要靠它就得另开一个隐藏顶层窗口，不值 |

由此定下的看门狗写法（`ui/desktop_widget.py` 里的 `QTimer`，2 s）：

1. 先解析 `DefView`；**解析不到**（shell 还没回来）→ 什么都不做，保持隐藏，下一轮再试
   （**不要**在这期间退回普通档显示：桌面都没了，让挂件飘在黑屏上更怪）；
2. `IsWindow(hwnd)` 假 → **重建**：`hide() + deleteLater()` → 建新窗口 → attach → 重设 region；
3. `GetParent(hwnd) != DefView` → 直接重挂 + 提顶（窗口还活着，不需要重建）；
4. 只在 `desktop` 档跑这套；`normal` / `top` 档的窗口不随 explorer 消失，进这两档前**必须**
   `SetParent(None)` 把窗口还原成顶层窗口，否则它就变成"explorer 一死就消失"的状态。

#### 绘制：整个挂件都不做半透明（2026-11-20 05:10 实测 + 用户 05:2x 拍板）

原设计是"底板半透明、文字不透明"。三个最小 demo 把它推翻了
（`scripts/out/probe_widget_demo.py` / `probe_widget_alpha.py` / `probe_widget_opaque.py`）：

| 组合 | 实测结果 |
|---|---|
| `WA_TranslucentBackground`（必然带 `WS_EX_LAYERED`）+ 挂进 `DefView` | **整窗不渲染**——窗口里画的不透明红块/白块，取到的全是壁纸色 |
| 挂进去后手动去掉 `WS_EX_LAYERED` | **立刻正常渲染**：三块色标、圆角底板、文字全部可见 |
| 加回 layered + `SetLayeredWindowAttributes(hwnd, 0, 180, LWA_ALPHA)` | **又不渲染**（整窗退回壁纸色）→ 整窗 alpha 这条路也死 |
| 去掉 layered + `SetWindowRgn(CreateRoundRectRgn(…))` | **能画且圆角正确**：四角外侧像素与挂件出现前**完全一致**（桌面图标不被这块矩形抹掉），中心是底板色 |
| 在 `DefView` 里再设一次 region | 返回 1、幂等；跨进程 `SetParent` **不会**把 region 清掉 |
| Win+D 之后 | 底板色仍在（`(28,30,36)`），四角仍透出桌面 |

**用户 05:2x 拍板：不要分档，整体放弃半透明效果。** 于是窗口形态三档统一：

- **实心底板 + `SetWindowRgn` 圆角**（圆角外真的透出桌面，图标不受影响，也不挡命中）；
- **不设 `WA_TranslucentBackground`**（因此永远没有 `WS_EX_LAYERED`），也就没有逐像素透明、
  没有整窗 alpha；
- 底板色 = `p.card` 或用户自定义 `widget_bg`；悬停时**提亮 6%**（改颜色，不改 alpha）；
- **`widget_opacity` 字段取消**，透明度滑块整条删掉（不留一个永远不生效的开关）；
- 改列数 / 改 DPI 后必须**重设 region**（尺寸变了 region 还是旧的，会露出直角）；
- region 交给系统（`SetWindowRgn` 成功后不要 `DeleteObject`）。

**好处是切档变简单**：三档窗口形态完全一样，`desktop ↔ normal/top` 只需要
`SetParent` + 置顶位，**不用重建原生窗口**（原方案里那段"存几何 → 改标志 → 重建 → 还原"
彻底不需要了）。

**顺带记一笔**：上一轮"贴桌面档能显示挂件"的结论没错，但**测错了对象**——那次探针的窗口
没设 `WA_TranslucentBackground`（纯不透明 `fillRect`），所以能画；半透明挂件挂进去其实
什么都不显示。这一轮才把它分开验清楚，也正好是用户接受的那个形态。

#### 菜单：不用自己加置顶（实测）

`ui/menu.Menu` 弹出的 popup **本身就带 `WS_EX_TOPMOST`**（实测 `topmost=True`），在 Win+D
状态下不被桌面盖住、菜单项能被真鼠标点中（`picked=['top']`）、关掉后挂件仍在 `DefView`
首位、父窗口未变、**挂件自身没被置顶**（`topmost=False`）。所以：

- `QMenu` 那条不需要"popup 前给菜单加 `WS_EX_TOPMOST`"的补救（原设计写了，实测多余）；
- 菜单的 owner 就是那个 `WS_CHILD` 挂件，位置也准：`mapToGlobal` 与 `GetWindowRect` 的
  偏差 **dx=0 dy=1**（物理像素），不用改成无 parent + 屏幕坐标。

#### 2026-11-20 事故记录（必须记住）

跑 `scripts/out/probe_desktop_parent.py` 里那句"把 DefView 压到我下面"
（`SetWindowPos(defview, 我们的窗口, …)`）之后，**用户桌面只剩壁纸、图标一个都看不见**。
查下来图标数据还在（`LVM_GETITEMCOUNT` = 71、`HideIcons` = 0），但 `SHELLDLL_DefView`
与 `SysListView32` 的 rect 都是 `(0,0)-(0,0)`——尺寸被写成 0，等于没有画布。修复：给
`Progman` 发一次 `WM_SIZE(SIZE_RESTORED, MAKELPARAM(宽, 高))`，Explorer 自己把两个窗口
撑回整屏，图标立刻回来（`scripts/out/fix_desktop_icons.py`，内含 `SetWindowPos` 兜底）。
**结论：桌面窗口只许读；唯一允许的写操作是"把挂件窗口 SetParent 到 `SHELLDLL_DefView`"，
且不得反过来动它。** 这一轮五组探针全程只 SetParent 自己的窗口，桌面每次 guard 都是 OK。

**切档是原地改标志，不要 `setWindowFlags()`**：

```
SetWindowPos(hwnd, HWND_TOPMOST / HWND_NOTOPMOST, 0, 0, 0, 0,
             SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)
```

实测返回 1、`WS_EX_TOPMOST` 位翻转、`hwnd` 与几何全不变。参考项目那条"改标志 →
销毁重建原生窗口 → 还原几何/透明度"的流程**在这里不需要**，少掉一段最容易把窗口
丢到屏幕左上角的代码。坑：`hWndInsertAfter` 必须按 64 位 `HWND` 传——`-1` 当普通
`int` 传进去会变成 `0x00000000FFFFFFFF`，`SetWindowPos` 返回 0、置顶位静默没写上
（本轮就被它骗出过"原地切档不可行"的假结论，`ctypes` 里要 `argtypes` 声明或传
`c_void_p(-1)`）。

#### 切档实现（三档之间怎么换）

- 统一入口 `set_layer(name)`，`name ∈ {"desktop", "normal", "top"}`，**不是循环**：
  - `normal ↔ top`：原地 `SetWindowPos(HWND_TOPMOST / HWND_NOTOPMOST)`，**不重建原生窗口**。
    实测返回 1、`WS_EX_TOPMOST` 位翻转、`hwnd` 与几何全不变。坑：`hWndInsertAfter` 必须按
    64 位 `HWND` 传（`c_void_p(-1)` 或声明 `argtypes`）——`-1` 当 int 传会变成 `0xFFFFFFFF`，
    函数返回 0、置顶位**静默**没写上（本轮因此误判过"原地切档不可行"）。
  - `desktop ↔ normal|top`：`SetParent` + 置顶位，**不重建原生窗口**（三档窗口形态一样，
    region、底板、控件全不变）。顺序是：
    离开 → `SetParent(hwnd, None)` + 改回 `WS_POPUP` + `SetWindowPos(按目标档设/撤 topmost)`
    + 重设一次 region（保险）；进入 → `SetParent(hwnd, DefView)` + 改 `WS_CHILD` +
    `SetWindowPos(HWND_TOP)` + 写回几何。
  - **attach / detach 之后要同步 Qt 侧的可见性**：跨进程 `SetParent` 会让 Qt 认为窗口
    状态变了，挂完必须 `show()` + `update() + processEvents()`。`showEvent` 里还有几何落盘
    与轮询登记，用一个 `_switching` 标志挡住递归，别让"切档 → showEvent → 再切档"转起来。
  - **attach 失败（找不到 `SHELLDLL_DefView`，例如非 Windows / Explorer 异常）→ 落到
    `normal`**，返回值给调用方，菜单里「贴桌面」一项置灰并注明"当前不可用"，不许乱设父。
- **默认档 `desktop`，启动就 attach**：`show()` 之后再挂（窗口没建好时没有 `hwnd`），
  挂完 `update() + processEvents()`。启动时 attach 失败不弹窗打扰，静默降级成 `normal`，
  状态栏/菜单里如实显示当前档。
- 切完档立刻写 `prefs.widget_layer`；`desktop` 档在降级时**不覆写**配置（用户选的是桌面，
  只是这次没挂上），下次启动再试。
- 平台代码放 `ui/desktop_zorder.py`：`set_topmost(on)` / `attach_desktop()` /
  `detach_desktop()` / `current_layer()`，非 Windows 全 no-op（照
  `core/automation/hotkeys_win.py` 的写法，可被验证脚本导入）。
- 已被实测否掉、不要再提的两条：500 ms 巡检自动恢复（Win+D 三个状态位全不变，没有判据）、
  常驻 `HWND_BOTTOM`（点一下就浮上来，还得靠巡检纠回去）。

### 3.3 布局与尺寸

- 卡片 `158 × 56`，间距 6，面板左右留白 10、底部 6，头部 30。
- 列数 1–6：`宽 = 158×n + 6×(n-1) + 20`，再按屏幕可用宽 −24 收住；面板最小宽 `MIN_W = 200`
  （单列内容 178，2026-11-21 从 320 降下来——旧值时 3×1 面板会留一大片空），空态另抬到 260。
- 高度：`30（头部）+ 6 + 行数×56 + 6×(行数−1) + 20（底部状态行）+ 8`，超过屏高 −80 封顶，内部滚动
  （滚动条 6 px，同参考项目）。
- 窗口不可手动 resize：宽由列数、高由行数决定；改列数是设置项，不是拖边框。
- 位置与尺寸都进 prefs；恢复时先 sanitize（屏幕外 → 主屏右上角 24 px）。

### 3.4 卡片

```
┌──────────────────────────────┐
│ 示例-吸顶灯          ( ⏻ ) │   名称 13px/600，单行省略
│ 示例-房间A · 已开启          │   副标题 11px，房间 + 读数/状态
└──────────────────────────────┘
```

- 副标题优先级：离线 → `房间 · 已离线`；无规格 → `不支持控制`；否则
  `房间 · {summary_line}`，没有读数时退回 `已开启 / 已关闭`。
- 多键面板：`房间 · 3 路 · 1 开`（把 `DeviceCard._gang_subtitle` 的文案抽到
  `core/catalog.gang_summary()`，两处共用，别复制一份）。
- 电源键 26 px，三态（开 / 关 / 未知），**只在 `card.control("power")` 存在时出现**：
  离线禁用、多键面板不画（避免"点一下到底关哪几路"的歧义）、传感器没有。
- 点卡片空白 → `open_device_requested(did)`；点电源 → `store.toggle(did, siid, piid)`，
  卡片自己消费这次点击（与 `DeviceCard` 同一套"按下在控件上就不算点击"的判定）。
- 空态：一块面板 + 一行说明「在主界面右键设备卡片 → 加入桌面挂件」+ 一个
  「打开主界面」按钮；不要只给一块空白。

### 3.5 头部、底部状态行与折叠条

- 头部 30 px：`米家控制台 · 挂件`（可省略）+ 右侧按钮：**图层** / 收起 / 管理 / 关闭
  （各 20 px）。**头部不放耗材圆点**（四个按钮已经占满，再塞两个点会挤标题）。
- **图层按钮（2026-11-20 用户改：不要循环切，要菜单选）**：
  - 按钮图标反映当前档：贴桌面 = 桌面轮廓、普通 = 窗口、置顶 = 图钉。**三种都 QPainter
    自绘**（照 `tray._drawn_icon` 的写法，`QPainterPath` 描边），不引外部 svg——项目没有
    图标资源目录，多一条资源打包路径不值当；
  - 按下弹菜单，三项：`贴桌面（置底）· 不挡窗口` / `普通` / `置顶 · 压在最前`，
    **直接用 `ui/menu.py` 现成的单选项 API：`Menu(parent, pal)` + `add_choice(menu, 文本,
    "desktop" 或 "normal" 或 "top", 当前档, pal)`**——它已经自绘了 ✓（注释里明确不用
    `QAction.setCheckable(True)`，系统 check indicator 在深色主题下是块方底）。
    说明文字写在标题里，不做双行 `QWidgetAction`；右键面板那条「图层 ▸」用同模块的
    `submenu()` 建，别用 `parent.addMenu(str)`（那一层没有透明底，圆角背后会冒方块）；
  - 选中即 `set_layer()`，**不循环、不连续点**；选中的就是当前档时只关菜单、不做任何事；
  - 菜单是独立的顶层 popup，**Qt 自己就把它设成 `WS_EX_TOPMOST`**：实测 Win+D 状态下
    不被桌面盖住、菜单项能点、关掉后挂件不被抬升、挂件自身也没被置顶。所以这里不需要
    额外加/撤置顶位，也不要给菜单改 parent（owner 就是那个 `WS_CHILD` 挂件，位置偏差
    只有 1 物理像素）；
  - 「贴桌面」attach 失败时该项置灰（拿 `add_choice()` 返回的 `QAction` 调
    `act.setEnabled(False)`）+ 标题后加"（当前不可用）"，勾落在实际档上。
- **底部状态行 20 px（2026-11-20 用户定）**：左端两枚圆点（轻 / 重，`0` 不画）+
  「耗材」二字，右端一行小字 `2 项该准备了`（两档都有才写 `2 项该准备 / 1 项急需换`）。
  点击整行 → 唤起主窗口并切到「耗材」页。字号 11px、`p.mute` 色，底边与面板留 8 px。
  放底部的好处：和卡片列表同一个视觉列，不跟头部按钮抢位置；设备多到要滚动时
  它也不动（在滚动区外面）。
- 折叠态 32 px：抓握纹 + 标题 + 每台一个状态点（9/7/5 逐级缩，放不下改聚合条）+
  **两枚圆点（这时它只能在这儿）** + 展开按钮。宽度锁定为展开态宽度；单击展开、
  拖动移动（6 px 阈值，与监控页 `DRAG_SLOP` 同一个数）。
- 折叠态与展开态的圆点是**同一份数据、两个位置**：收起时画在状态条上，展开时画在
  底部状态行；不要两处各算一遍（`_refresh_dots()` 一处更新两个控件）。
- 折叠时也跟随开关状态（圆点颜色），展开要**重建卡片**——参考项目在这里出过
  "收着有状态点、展开却说没添加"的 bug。

### 3.6 外观：实心底板 + 圆角，不做半透明

参考项目用 `windowOpacity`（整窗 alpha，文字跟着淡，靠"悬停恢复 1.0"补救）。本项目原方案是
"只淡底板、文字不淡"，但 **2026-11-20 05:10 实测证明这条路走不通**（§3.2.2：layered 挂进
`DefView` 整窗不渲染），用户 05:2x 拍板**整体放弃半透明**。于是：

- 底板：**实色**，默认 `p.card`；可选预设色板 + `#rrggbb` 自定义（`widget_bg`）；
- 悬停：底板**提亮 6%**（`theme.blend(bg, p.text, 0.06)`），160 ms 过渡 —— 改颜色，不改 alpha；
- 圆角 14：`SetWindowRgn`（`CreateRoundRectRgn`）裁形状，**不是** QSS 的 `border-radius`；
  描边 1 px `p.border` 画在自绘里；
- 不加投影、不套 `WindowFrame`（阴影留白 24 px 会变成不可见点击区），要立体感就在自绘里
  留 6 px 画一圈淡影；
- **没有透明度滑块**：prefs 里不再有 `widget_opacity`，管理窗/右键菜单也不放这一项。
  少一个永远不生效的开关，比留个灰掉的好。
- 渐变预设 v1 不做——挂件面积小，渐变看不出来，反而增加取色分支。

### 3.7 交互总表

| 操作 | 结果 |
|---|---|
| 左键拖头部 / 面板空白 | 移动窗口（6 px 阈值；松手防抖落盘） |
| 单击卡片空白 | 唤起主窗口并打开这台设备的详情 |
| 单击电源键 | `store.toggle`（乐观更新，轮询回来自然校正） |
| 拖动卡片 | 排序（8 px 阈值、幽灵 + 落点高亮） |
| 右键卡片 | 打开详情 / 从挂件移除 |
| 单击图层按钮 | 弹出档位菜单：贴桌面（置底）/ 普通 / 置顶，当前档打勾；选中即切 |
| 右键面板 | 管理挂件… / 收起 / **图层 ▸**（同一个三项子菜单）/ 锁定 / 隐藏 |
| 单击折叠条 | 展开 |
| 拖动折叠条 | 移动（不展开） |
| 单击耗材圆点 | 打开主窗口「耗材」页 |
| 点 × | 本次隐藏（不改启用开关） |

### 3.8 数据与轮询

- 挂件持 `Store` 引用，`update_states(dids)` 只刷新受影响卡片（与耗材页同一模式），
  `on_loaded` / `on_layout_changed` 时整块重建。
- 轮询登记：

  | 状态 | 需求 |
  |---|---|
  | 展开可见 | `widget, 3000, "挂件展开"` |
  | 折叠可见 | `widget, 8000, "挂件收起"` |
  | 隐藏 / 未启用 | `clear("widget")` |

  主窗口在托盘时是 15 s，挂件 3 s 会取最严——这正是 `poll_demand` 存在的理由。
- 写操作只调 `store`，不自己发请求；不在挂件里做第二套乐观状态。
- 耗材计数由主窗口 `_on_consumable_counts` 转发 `set_badges(warn, crit)`；
  耗材页在隐藏时也会更新计数（`update_cards` 不判可见性），数据是新的。

### 3.9 持久化（prefs v19：宫格槽位）

挂件是**固定宫格**：`行×列` 由 `widget_layout` 决定，每格一个槽位（`widget_slots`），
槽位里是设备 did 或空串。空槽不把后面的设备挤上来——位置是用户摆的，不是队列。

| 字段 | 类型 / 默认 | 校验 |
|---|---|---|
| `widget_autostart` | bool / `False` | 只认 `True`；"启动时开启"（v18 前叫 `widget_enabled`，旧键不再认） |
| `widget_layout` | str / `"1x3"` | 只认 `WIDGET_LAYOUTS`（1x1…6x6 共 36 种），否则回默认 |
| `widget_slots` | list[str] / `["", "", ""]` | 长度 = 行×列；非字符串置空、同一台设备只留第一次、超格截断 |
| `widget_geom` | list[int] / `[]` | 4 个整数且宽高 > 0，否则空 |
| `widget_collapsed` | bool / `False` | 只认 `True` |
| `widget_layer` | str / `"normal"` | 只认 `"desktop"` / `"normal"` / `"top"`，其它一律回 `"normal"` |
| `widget_locked` | bool / `False` | 只认 `True` |
| `widget_bg` | str / `""` | 只认 `#rrggbb`，否则跟随主题 |

- `widget_rows` / `widget_cols` 是**推导属性**（`widget_layout_size()`），不落盘。
- 读写 API：`set_widget_layout()`（换布局按行优先保留，多截少补）、
  `set_widget_slots()`（管理窗整表保存）、`set_widget_slot(index, did)`（同设备在别处
  会被先清掉）、`clear_widget_slot(index)`、`remove_widget(did)`。旧的
  `widget_devices` / `widget_columns` / `add_widget_device` 等已删。
- `PREFS_VERSION` 18 → 19；`verify_consumables.py` / `verify_monitor_page.py` /
  `verify_card_style.py` 的版本断言同步改。
- v18 → v19 迁移：按旧 `widget_columns` + 设备数推出 `rows = ceil(设备数 / cols)`
  （上限 3 行），拼成 `"{rows}x{cols}"`；旧设备表按行优先灌进槽位。老 `widget_enabled`
  仍不读，老 `desktop` 档仍迁 `normal`（v17 的语义不变）。
- `widget_layer` 是**三值字符串**，不是两个布尔。
- 运行时降级（`desktop` 挂不上 → `normal`）**不写回配置**：用户选的是桌面，写回去就
  永远变普通了。菜单按实际档打勾，配置保持用户意愿。
- 拖动几何 **400 ms 防抖**；`hideEvent` 再强制存一次。
- 不新建 `board.json`：挂件的设备清单 / 外观 / 几何是一组强相关配置，进 prefs
  才能整体备份与迁移；测试也只需要临时的 `Prefs(path)`。

### 3.10 启动、关闭、主题、多屏

- **启动**：`MainWindow` 构造时建挂件（不显示）；`prefs.widget_autostart` 为真才
  `show()`，`show()` 之后再按 `prefs.widget_layer` 落档（默认 `normal`，show 即可见）。
  开机直接进托盘（主窗口从未 show）时挂件照样出现——它不依赖主窗口可见。
- **关闭**：严格按「关闭面板时」的设置：
  - 选「最小化到托盘」→ 主窗口藏起来，挂件留存，轮询按挂件 3 s；
  - 选「退出程序」→ 真退出，挂件一起消失。
- **主题**：`_apply_theme` 里调 `widget.set_palette(pal)`；挂件是独立顶层窗口，
  不继承主窗口 QSS，所有自绘颜色都要重刷（与 `CameraPopoutWindow` 同一套注意）。
- **多屏**：恢复时 sanitize；监听 `screenAdded` / `screenRemoved` /
  `primaryScreenChanged`，把窗口夹回可见范围（参考项目只在启动时夹，拔屏后会丢）。
- **全屏应用时自动隐藏**：P2 可选（`GetForegroundWindow` 矩形是否覆盖屏幕），
  v1 不做，避免又一处平台特判。

### 3.11 入口与管理界面

**2026-11-20 深夜定稿（v19，用户 m02862）**：挂件跨家庭，所以入口从功能区挪到分隔线
下方、与设置同组；头部「…」改成齿轮按钮，直接开管理窗，不再弹菜单。

- **显隐**：左栏分隔线下方第一行「桌面挂件」（`FOOT_WIDGET = "widget"`），点一下弹、
  再点收起；高亮跟随挂件实际可见性（`visibility_changed` → `nav.set_widget_on`）。
  这里**不写** `widget_autostart`。
- **启动时开启**：设置 → 常规 → 启动设置里的「启动时开启桌面挂件」，绑
  `prefs.widget_autostart`；设置页不再有启用开关与「预览挂件」按钮。
- **头部三键**：齿轮（管理）/ 图层 / 关闭。齿轮直接 `manage_requested.emit()` 开管理窗；
  图层自己弹三档菜单；面板右键菜单保留「管理挂件… / 图层▸」。
- **管理窗**（`ui/widget_manager.py`，v19 重做）：上方「排列方式」下拉选 `行×列`；
  下方 `SlotGrid` 按槽位画宫格，空格中央「＋ 添加设备」，点开**家庭 → 房间 → 设备**
  三层树选择（跨家庭）；两格之间拖动**交换**（8 px 阈值，拖到空格即移过去，空出的格
  留空）；悬停设备格右上角出现 × 移除。点「完成」时 `set_widget_layout()` +
  `set_widget_slots()` 一起落盘，主窗口比较前后再 `rebuild()`。
  **不再有**「主界面卡片右键 → 加入桌面挂件」这个路径，也不再有上移/下移列表。
- **底部耗材**：右下角「耗材：[轻提醒圆点] [重提醒圆点]」并排画，样式与左栏耗材
  入口的圆点同源（灰 = 轻提醒，红 = `theme.DANGER` 重提醒），只画 count > 0 的。

### 3.12 计划中的文件

| 文件 | 作用 |
|---|---|
| `mihome/ui/desktop_widget.py`（新） | 窗口、头部（含图层菜单）、底部状态行、折叠条、拖拽、几何、轮询登记 |
| `mihome/ui/desktop_card.py`（新） | 158×56 紧凑卡（自绘） |
| `mihome/ui/desktop_zorder.py`（新） | Win32 层级四件：`set_topmost(on)`、`attach_desktop()`（挂进 `SHELLDLL_DefView` + 提顶）、`detach_desktop()`、`current_layer()`，非 Windows 全 no-op |
| `mihome/ui/desktop_manager.py`（P1，新） | 管理对话框 |
| `mihome/core/prefs.py` | v17 字段 + 校验 |
| `mihome/core/catalog.py` | 抽出 `gang_summary()` 共用 |
| `mihome/ui/main_window.py` | 创建 / 显隐 / 主题 / 轮询 / 转发计数 / 打开详情 |
| `mihome/ui/wall.py` | 右键「加入 / 移出挂件」 |
| `mihome/ui/settings.py` | 常规页分区 |
| `scripts/verify_desktop_widget.py`（新） | 见第 6 节 |
| `scripts/out/shot_widget.py`（新） | 离屏截图（展开 / 折叠 / 三档各一张） |

---

## 4. 分阶段

**P0：能用的最小闭环**

窗口壳（**三档：贴桌面（默认）/ 普通 / 置顶**，头部图层按钮弹菜单选、不循环；
层级实现按 §3.2.1 的硬约束，尤其"只 SetParent 自己、绝不写 DefView"）+ 紧凑卡 + 电源 +
点卡片进详情 + 右键加入/移除 + 启用开关（**含首次启用的 2.5 s 浮起预览**，否则在设置页
点开看不到任何东西，见 §3.11）+ prefs v17 + 几何记忆 + 轮询登记 + 空态。
验收：**装完默认就是贴桌面档**——桌面露出时能看到挂件、按 Win+D 它还在、点任何普通
窗口时完全不挡；关掉主窗口只留挂件，3 秒内能看到别的设备状态变化；图层菜单切到
「普通」后被窗口盖住（再按 Win+D 原样回来）、切到「置顶」后始终在最前，三档来回切
不丢几何、不跳到屏幕左上角。
**另外要手工重启一次 explorer**（任务管理器 → 结束 `explorer.exe` → 文件 → 运行新任务
`explorer`）：贴桌面档的挂件在 shell 回来后 **2 s 内自己重建并回到桌面层**（不是消失
就再也不回来），普通/置顶档的挂件全程不受影响。这是唯一需要"真把 shell 杀掉"的验收项。

**P1：像一块"挂件"**

折叠状态条 + 耗材圆点（底部状态行）+ 列数 / 图层 / 锁定 / 背景色 +
卡片拖动排序 + 独立管理窗 + 多屏夹回。

**P2：锦上添花（可不做）**

场景卡（`store.run_scene`，成本低但会引入第二种卡片）、摄像头"最后一张封面"静态卡
（复用 `cache/thumbs/<did>.jpg`，不取流）、全屋温湿度胶囊、全屏应用自动隐藏、
大卡模式（复用 `DeviceCard`）。

---

## 5. 明确不做

| 项 | 原因 |
|---|---|
| 壁纸层（与壁纸同层、被壁纸替换） | 要走 `IDesktopWallpaper` 那条路，和"挂件压在所有普通窗口之下"不是一回事 |
| 把窗口挂到 `Progman` / `WorkerW` 里 | 2026-11-20 重测：结构对、命中对，但**画面不出来**（自建 GDI 窗、系统 `Static`、Qt 窗口三种都不画）；壁纸软件能画是因为走 D3D 交换链。可用的宿主只有 `SHELLDLL_DefView`，见 §3.2.1 |
| 巡检 `IsWindowVisible` 自动恢复 | 实测 Win+D 之后 `IsWindowVisible` / `IsIconic` / `DWMWA_CLOAKED` 全都不变，没有判据；而且"先消失再回来"用户已明确不要 |
| 挂件内播视频 | 解码 + 观看位成本，与"扫一眼"冲突；监控页已覆盖 |
| 多键逐路开关 | 紧凑卡放不下；点进详情 |
| 挂件内独立详情窗 | 会与主窗口的压暗、摄像头交接、单例逻辑打架 |
| 键盘导航 / 输入 | 不抢焦点是无边框 Tool 窗口的核心取舍 |
| 每设备独立配色 / 独立字体 | 没有明确收益，先把全局那几档做稳 |

---

## 6. 风险与验证

**最小 demo + 真重启都跑完了（2026-11-20 05:10 / 05:2x，`scripts/out/probe_widget_demo.py` +
`probe_widget_alpha.py` + `probe_widget_opaque.py` + `probe_reattach.py` + `probe_reattach2.py`）**，
三件事的结论：

1. **点击**：贴桌面档下点头部按钮，第 1 次就进到 Qt（`presses=1`），菜单正常弹出、
   菜单项能被真鼠标点中（`picked` 拿到值）→ 可用。剩下没验的只有"真挂件上按钮图标 /
   菜单勾选 / 层级实现三处会不会脱节"，那是回归的事。
2. **透明**：贴桌面档**不能 layered，只能不透明圆角**（§3.2.2）。这不是"未知项"了，
   而是直接改了外观设计——用户 05:2x 拍板**整体放弃半透明**，三档统一实心底板。
3. **explorer 重启（真重启，不是模拟）**：桌面档的窗口**会被一起销毁**（0.03 s），
   Qt 侧察觉不到（`isVisible` 仍 True、`winId()` 返回死句柄）→ 看门狗必须 `IsWindow` + 重建；
   普通/置顶档窗口不受影响；`AutoRestartShell=1` 也不保证 shell 自己回来（两次都要手动拉）。
   重建后画面正确、region 要重设。详见 §3.2.1「explorer 重启」那节。

**已经验过的（2026-11-20，别再重复踩）**：

- 普通档（不加任何 z-order 标志）在 Win+D 之后 **`IsIconic` / `DWMWA_CLOAKED` /
  `IsWindowVisible` 全不变、几何不变**，只是被抬起来的桌面盖住；再按一次 Win+D
  原样回来。置顶档全程取色不变 → 完全免疫。
- 切档可以原地做：`SetWindowPos(hwnd, HWND_TOPMOST / HWND_NOTOPMOST, …)` 返回 1、
  `WS_EX_TOPMOST` 位翻转、`hwnd` 与几何都不变 → **不需要 `setWindowFlags()` 重建原生窗口**。
  但 `hWndInsertAfter` 要按 64 位 `HWND` 传（`c_void_p(-1)` 或声明 `argtypes`）。
- **explorer 重启的连带**：挂进 `DefView` 的窗口跟着销毁、Qt 不知情、普通/置顶档不受影响，
  见 §3.2.1 那张表；`AutoRestartShell=1` 也没能让 shell 自己回来。
- **宿主决定生死**：挂 `Progman` 或 `WorkerW` → 不画（GDI 窗口 / 系统 `Static` / Qt 窗口
  三种都不画，但命中测试正常）；挂 **`SHELLDLL_DefView`** → **能画**、排在 `SysListView32`
  之上、命中可达、Win+D 后仍可见、F5 刷新与点桌面空白都不掉。Qt 窗口挂完必须
  `update() + processEvents()`，否则不重绘（我们为此误判过一轮）。
- **但"能画"只对不透明窗口成立**：带 `WS_EX_LAYERED` 的窗口（Qt 的
  `WA_TranslucentBackground` 必然带）挂进 `DefView` 后**整窗不渲染**，画什么都没有；
  去掉 layered 立刻正常，整窗 `SetLayeredWindowAttributes` 也救不回来。圆角改用
  `SetWindowRgn(CreateRoundRectRgn)`，跨进程 `SetParent` 不会清掉它，四角外侧不遮挡
  桌面图标（四角像素与挂件出现前完全一致）。详见 §3.2.2。
- 菜单：`ui/menu.Menu` 的 popup 自带 `WS_EX_TOPMOST`，贴桌面档 + Win+D 下也能正常显示、
  点中、关掉后挂件不被抬升（`picked` 拿到 `'top'`、挂件 `topmost=False`）。
- 点击：非 layered 的默认档（实心底板 + region + `WindowDoesNotAcceptFocus`）下，
  **第一次点击就直接进 Qt**（`clicks=1`），比 layered 版还顺（那版第一次只做激活）。
  所以"放弃半透明"顺带把最烦的激活一跳也去掉了。
- 桌面窗口只许读。把 `SetWindowPos(defview, …)` 用来改 z 序会把 `SHELLDLL_DefView` /
  `SysListView32` 的 rect 写成 `(0,0)-(0,0)`，用户桌面图标全消失。**修复办法**：给
  `Progman` 发一次 `WM_SIZE(SIZE_RESTORED, MAKELPARAM(宽, 高))`（`scripts/out/fix_desktop_icons.py`）。
- 任务栏 `WM_COMMAND 419` 等价于 Win+M，**会跳过没有 `WS_MINIMIZEBOX` 的无边框窗口**，
  拿它测 Win+D 会得到假结果；`416`（UndoMinimizeAll）实测无效，恢复用逐窗 `SW_RESTORE`。
  真 Win+D 要用 `(New-Object -ComObject Shell.Application).ToggleDesktop()`。
- 屏幕取样的小坑：`Progman` 带 `WS_EX_NOREDIRECTIONBITMAP`（raised desktop），
  **DWM 缩略图对它取不到内容**；要取样就 `PrintWindow(Progman, PW_RENDERFULLCONTENT)`
  （含子窗口）或直接抓屏（需先把遮挡窗口最小化，`Win+D` 期间抓到的可能是合成快照）。

**还要人工看一眼的两件事**：

1. 置顶档 / 贴桌面档 + 全屏应用（视频、游戏）：置顶会压在游戏上，贴桌面则被全屏盖住
   （符合预期）；置顶要不要"全屏自动隐藏"（P2 已列）；
2. 多显示器：贴桌面档要按**虚拟桌面**边界换算坐标（DefView 覆盖整块虚拟桌面，
   不是主屏）；置顶/普通档跨屏拖动后落点与拔插夹回。

**`scripts/verify_desktop_widget.py` 计划覆盖**：

1. prefs v17：默认值、越界钳制、坏值回退、设备去重；
2. 几何公式：n 列 / m 行的宽高；屏幕外几何被夹回；
3. 卡片状态：在线 / 离线 / 无开关 / 多键 / 传感器 / 无规格；
4. 电源：在线只发一次 `toggle`，离线与无开关不发；
5. 点击卡片发 `open_device_requested` 且不触发 `toggle`；
6. 折叠 / 展开；单击不移动、拖动移动（6 px 阈值）；
7. 拖动排序落盘；重复 did 不重复建卡；
8. 轮询需求：展开 3000、折叠 8000、隐藏后撤销；主窗口在托盘时仍是 3000；
9. 主题切换后自绘颜色更新（离屏像素断言，刷 4 轮 `processEvents`）；
10. 耗材圆点转发与 `0` 不画；
11. 关闭主窗口的两种设置：托盘 → 挂件留存；退出 → 挂件销毁；
12. 空态与"设备被删 / 换家庭"后的降级；
13. `desktop_zorder`（纯逻辑，不碰真窗口）：非 Windows 全 no-op；
    `set_topmost(True/False)` 各调一次对应 `SetWindowPos`、重复设同值不重复调、
    `hwnd == 0` 时安全返回不抛；`attach_desktop()` 在找不到 `SHELLDLL_DefView` 时返回
    False 且不改窗口状态、`current_layer()` 报 `"normal"`（找不到就退回普通档，不许乱设父）；
    `detach_desktop()` 幂等，未 attach 时调用不抛；`desktop → top → normal → desktop`
    来回切后 `current_layer()` 每次都正确、不留两个原生窗口。真窗口那部分只能人工验
    （上面第 2、3 条）。
14. **看门狗状态机**（把窗口操作抽成可注入的假实现再测，别真挂窗口）：
    `IsWindow` 假 → 调**重建**（而不是重挂）；`GetParent != DefView` → 调**重挂**；
    `DefView` 解析不到 → **两步都不做**、不抛、不反复重建；同一状态连跑 5 轮只动作一次；
    只在 `desktop` 档跑（`normal`/`top` 档每轮都不碰窗口）。这条挡的是"explorer 已经
    死了还在那儿 SetParent 一个死句柄"这类循环。

截图：`scripts/out/shot_widget.py` 出展开 / 折叠 / 三档各一张，沿用"多刷几轮
`processEvents` 再 `grab()`"（离屏只刷一轮会被画成压扁的串，监控页踩过）。

---

## 7. 拍板结果（2026-11-20 用户定）

1. **卡片密度**：用 **158×56 紧凑卡**，不复用 `DeviceCard`。（定）
2. **层级**：**三档**——「贴桌面」（置底）/「普通」（**默认**）/「置顶」；入口是头部
   **图层按钮 + 弹出菜单**（不循环切，用户 11-20 04:44 定）。
   演变过程留档：11-20 先定两档（置顶 / 普通）、默认普通，因为当时把"挂桌面"判死了；
   当天重测发现**挂进 `SHELLDLL_DefView` 能画、Win+D 可见、点击可达**（挂 `Progman`/
   `WorkerW` 才不画），「贴桌面」复活；随后（04:44）用户要求把它设为默认档；**当晚
   22:2x 又改回默认「普通」**——理由：从主界面点左栏「桌面挂件」时主窗口正挡在前面，
   默认贴桌面等于"点了看不到"，默认普通则当场出现在右上角。档位仍是三选、用户可手改，
   配置里显式选的 `desktop` 保留（v18 只把老文件里的默认值迁成 `normal`）。
3. **场景卡**：指主界面「场景」列表里的快捷场景（点一下 `store.run_scene`），
   **放 P2**。（定）
4. **耗材圆点**：位置在**底部状态行**右侧「耗材：[轻提醒] [重提醒]」并排两枚，
   样式与左栏耗材入口同源；只画 count > 0 的。（定）
5. ~~**透明度**：先做只淡背景、文字不淡~~ → **05:2x 作废**。实测 layered 窗口挂进
   `DefView` 整窗不渲染（§3.2.2），用户拍板**整体放弃半透明**：底板实色 + 圆角 region，
   `widget_opacity` 字段取消，透明度滑块不做了。（11-20 05:2x）
6. **「显示桌面」后留在桌面**：「贴桌面」档满足——它本来长在桌面上，而且永不挡窗口；
   「置顶」档同样满足但会压窗口；默认的「普通」档不满足（被盖住，再按一次回来）。
   11-20 04:44 曾把默认从「普通」改成「贴桌面」，当晚 22:2x 又改回「普通」（见第 2 条）。
7. **切档入口**：头部**图层按钮 + 菜单三选**，不做循环点击、不做开关、不做两个图钉。
   选当前档无副作用；`desktop` 当前不可用时该项置灰并落在实际档上。（11-20 04:44 定）
8. **绘制形态（05:10 实测 + 05:2x 拍板）**：三档统一 **实心底板 + `SetWindowRgn` 圆角**，
   **不做半透明**（layered 挂进 `DefView` 整窗不渲染，整窗 alpha 也救不回来，见 §3.2.2）。
   连带简化：`desktop ↔ 普通/置顶` 切档只动 `SetParent` + 置顶位，**不重建原生窗口**。
   默认档按第 2 条「普通」。（定）

留档（免得又翻出来讨论）：

- ~~挂桌面这条路整条作废~~：**翻案了**。挂 `Progman`/`WorkerW` 确实不画（三种窗口都验过），
  但挂 `SHELLDLL_DefView`（图标层）能画、能量在图标之上、Win+D 可见、点击可达 —— 这就是
  「贴桌面」档。前提是**只 SetParent 自己的窗口，绝不反过来写 DefView**（写它会把桌面搞坏）。
- ~~逐像素透明在子窗口下是否还在~~：**已验，答案是"不在"**。layered 窗口挂进 `DefView`
  整窗不渲染（连不透明色块都不显示），去掉 layered 才画；整窗 alpha 同样无效。
  → 半透明整条放弃，改实心底板 + `SetWindowRgn` 圆角（§3.2.2）。
- ~~Win+D 之后靠巡检把挂件捞回来~~：三个状态位都不变，没有判据，已放弃（贴桌面档不需要）。
- ~~切档要销毁重建原生窗口~~：三档窗口形态完全一样（实心底板 + region），所以哪一档之间
  都不需要重建——普通 ↔ 置顶 原地 `SetWindowPos`，跨贴桌面档只是 `SetParent`。

---

## 8. P0 落地记录（2026-11-20）

代码（对不上就是没落地）：

- 新建 `mihome/ui/desktop_widget.py`（窗口壳 / 头部自绘与按钮 / 三档 / 面板与卡片菜单 /
  看门狗 / 几何记忆 / 轮询登记 / 首次启用预览）、`mihome/ui/desktop_card.py`（158×56
  紧凑卡、副标题与电源可用性）、`mihome/ui/desktop_zorder.py`（Win32 门面：DefView 解析、
  `attach_desktop` / `detach_desktop` / `set_topmost` / `set_region`，以及纯函数
  `watchdog_action`）。
- `mihome/core/prefs.py` 升 v17：8 个 `widget_*` 字段 + 逐字段校验 + 9 个 setter。
- `mihome/core/catalog.py` 抽 `gang_summary()`（多键副标题两处共用）；`mihome/ui/device_card.py`
  改用它，行为不变。
- `mihome/ui/main_window.py`：建挂件、7 个信号接线、`_set_widget_enabled` /
  `_on_widget_device_toggle` / `_rebuild_desktop_widget`；`mihome/ui/wall.py` 卡片右键
  「加入桌面挂件 / 从挂件中移除」；`mihome/ui/settings.py` 常规页「桌面挂件」开关 +
  「预览挂件」。
- `scripts/verify_desktop_widget.py`（14 组，离屏，`zorder._api` 换成假的）。

P0 范围内的偏差（如实记；下文第 2、3 条已被 §8.2 的 v18 调整取代）：

1. 头部只有 **图层 / 「…」 / 关闭** 三个按钮；折叠条、锁定不在 P0（随 P1）。
2. 设置页的按钮 P0 叫 **「预览挂件」**，不是「管理挂件…」——P0 没有管理窗，点它就是把
   挂件浮起 2.5 s 再落回原档；P1 做管理窗时再改名。
3. 「…」面板菜单里「管理挂件…」暂时打开设置页，「收起」「锁定」未实现（P1）。

验证（2026-11-20，本机）：

- `scripts/verify_desktop_widget.py` 14 组全绿；`ruff check --select F,E9 mihome scripts` 全绿。
- 真桌面冒烟（`scripts/out/probe_widget_live.py`）：默认贴桌面档 `parent=DefView`；
  Win+D 后挂件仍在桌面（全屏截图确认，`scripts/out/widget_desktop_live.png`）；
  切「普通」后 `parent=0`、被浏览器窗口盖住；再切「贴桌面」能回去。
- 模拟宿主丢失（`detach_desktop` 把窗口摘成顶层）：看门狗 2 s 内重挂回 DefView。
  另外踩到一个坑：**attach 时必须先把窗风格改成 `WS_CHILD` 再 `SetParent`**，Qt 顶层窗口
  是 `WS_POPUP`，直接跨进程 `SetParent` 会失败（`GetParent` 仍为 0 → 静默降级普通档）。
- 真杀 `explorer.exe` 的「整窗被销毁 → 重建」路径：**2026-11-20 21:4x 真杀验收通过**
  （`scripts/out/probe_widget_kill.py`，假 store，复刻主窗口 `_rebuild_desktop_widget()`
  的重建路径）。时间线：旧挂件窗口在 kill 后 **0.11 s** 被一起销毁（`IsWindow=False`）→
  看门狗 **1.07 s** 判死并发 `rebuild_needed` → 新窗口建出来时 shell 还没回来，`attach_desktop`
  失败、静默降级普通档但**开着重试** → 手动拉起 `explorer.exe` 后 **3.14 s**（重建后 2.08 s）
  自己贴回新 `SHELLDLL_DefView`（`WS_CHILD`、`IsWindowVisible=True`、rect 正确）。全程
  `prefs.widget_layer` 保持 `"desktop"`、没被降级写回。
  验证中补的两个真问题（已改）：
  1. 降级后如果不再重试，explorer 回来了挂件也永远停在普通档 → `apply_layer()` 失败时置
     `_retry_desktop` 并按 **500 ms** 节拍重试，`_tick_watchdog` 里贴回就恢复桌面档；
  2. `main_window._rebuild_desktop_widget()` 原来拿「旧窗口的实际档」重建，旧窗口刚降级成
     普通就会把新窗口也建在普通档 → 改成拿 `prefs.widget_layer`（用户的目标档）。
  另：`AutoRestartShell=1` 时 `taskkill /f` 之后 shell **不会自己回来**（等 3 s 后仍没有），
  脚本里是手动 `explorer.exe` 拉起的；产品侧对应用户自己开任务管理器，不影响挂件行为。
- 视觉确认的局限：这次验收时机器上有一个全屏 `UnrealWindow`（游戏）盖住桌面，Win+D 也
  露不出桌面，**没法截桌面图**；改用「窗口自绘 `widget.grab()` + 窗口状态（`WS_CHILD` /
  `IsWindowVisible` / rect / parent=DefView）」确认画面正确。裸桌面截图见上一条 11-20 早些
  时候的 `widget_desktop_live.png`。
- 多屏 / 全屏应用 / 置顶压游戏仍按 §6 末尾列为人工项。

### 8.1 接进主窗口后又踩到的两个坑（2026-11-20 晚）

1. **挂件不能是主窗口的子控件**。`DesktopWidget(..., parent=self)` 会让它带
   `Qt.Tool` + 原生窗口；Qt 6.12 一旦给挂件建出原生窗口，会把主窗口里同级的
   `_FrameBorder` / `_ResizeEdge` / central widget 也全改成原生窗口——铺满整窗的
   1px 描边层于是吃掉所有鼠标事件，主界面表现成"点哪都没反应"（用户报的就是这个）。
   改成 `DesktopWidget(..., None)`（独立顶层窗口）后兄弟控件全部回到非原生，
   点击恢复正常。挂件本来就是顶层窗口，不挂 parent 不影响它跟主窗口一起 show/hide。
   **别再给它传 parent**；真要传，先确认 `_FrameBorder` 的 `WA_NativeWindow` 还是 False。
2. **挂进 `DefView` 后要显式带 `SWP_SHOWWINDOW`**。`SetParent` 前后 Qt 可能把子窗口的
   `WS_VISIBLE` 清掉（实测挂上后 `style=0x46000000`：`WS_CHILD` 有、`WS_VISIBLE` 没有，
   Qt 侧 `isVisible()` 仍是 True、`IsWindowVisible` 却是 False），结果是"挂是挂上了，
   桌面上什么都没有"。`attach_desktop()` 里 `SetWindowPos` 的 flags 已加
   `SWP_SHOWWINDOW`。
3. 顺带：`_tick_watchdog` 只认 `IsWindowVisible`/`IsWindow` 不够，重挂之后必须再看
   `WS_VISIBLE`；两个坑都属于"程序认为在、屏幕上看不见"，验收只看 `isVisible()` 会漏。

### 8.2 v18 调整（2026-11-20 晚，用户 7 条意见）

用户用完 P0 提了 7 条，已全部落地：

1. **开关只留左栏功能区**：设置页不再有启用开关；左栏第四格「桌面挂件」点一下弹、
   再点收起，高亮跟实际可见性走。设置页只保留「启动时开启桌面挂件」这个启动偏好。
2. **功能区文案**：「监控」→「监控室」，「挂件」→「桌面挂件」。
3. **默认层级改「普通」**：从主界面点开关时主窗口挡在前面，默认贴桌面等于"点了看不到"；
   默认普通则当场出现在右上角。老配置里 `widget_layer == "desktop"` 在 v17→v18 迁移时
   改成 `normal`（那时 `desktop` 是默认值，多半不是用户手选）；之后用户手选的桌面档保留。
4. **「…」菜单去掉「隐藏挂件」**：和右上角 × 重复，且会让人以为有"关闭 / 隐藏"两种状态。
5. **管理挂件独立成窗**：`ui/widget_manager.py`（添加 / 移除 / 上移 / 下移 / 完成），
   不再跳设置页；**卡片右键「加入桌面挂件」整条路径删掉**；菜单宽度 `sizeHint + 24`
   （否则「管理挂件…」被 QSS 内边距截断显示不全）。
6. **启动偏好改名**：`widget_enabled` → `widget_autostart`，设置 → 常规 → 启动设置里
   新增「启动时开启桌面挂件」。
7. **底部耗材改成两枚圆点**：右下角「耗材：[轻提醒] [重提醒]」，样式与左栏耗材入口同源
   （灰 / `theme.DANGER`），只画 count > 0。

连带：`PREFS_VERSION` 17 → 18；`verify_desktop_widget.py` 新增 manager 与旧配置迁移两组；
`verify_card_style` / `verify_autostart` / `verify_close_flash` / `verify_poll_demand`
的断言同步；全量 62 个 `scripts/verify_*.py` 与 ruff `F,E9` 全绿。

### 8.3 v19 调整（2026-11-20 深夜，用户 m02862 五条）

用户又提五条，核心是"挂件跨家庭，入口和配置得分开，设备要能按位置摆"：

1. **头部「…」换齿轮**：既然图层按钮已经在外面，菜单里再放一份图层是冗余；齿轮直接
   开管理窗，不再弹菜单。齿轮 8 个齿用"外/内半径交替"多边形画——早先"细环 + 8 根细
   辐条"在 20 px 下像太阳（实测改了两版）。
2. **入口挪到分隔线下方**：`FOOT_H` 46 → 80，两行（桌面挂件 / 设置）与"这个程序"的
   配置同组；功能区回到三格，语义只剩"当前这个家"的功能。
3. **添加设备改三层树**：家庭 → 房间 → 设备，空的房间跳过；候选里排除已占槽位的设备。
4. **电源图标重画**：紧凑卡原来半径 5、缺口朝右、没有开态实心底；现在与
   `DeviceCard._paint_power_glyph` 同比例（`r = 宽×0.21`、`128°` 起、`284°` 扫），
   开 = 实心 accent 圆 + 白字形，关 = 中性圆 + `text_soft`。
5. **管理窗改宫格**：`SlotGrid` 自绘槽位，空格「＋ 添加设备」、悬停 ×、拖动 8 px 阈值
   交换；排列方式最初 12 种（1x1…3x4），2026-11-21 扩到 36 种（1x1…6x6）+ 行/列两个独立下拉；落盘 `widget_layout` + `widget_slots`，空槽在挂件
   上原样留空（不再"挤上来"）。

连带：`PREFS_VERSION` 18 → 19；`prefs` 删 `widget_devices/widget_columns` 与
`add/remove_widget_device`，改 `set_widget_layout/set_widget_slots/set_widget_slot/
clear_widget_slot/remove_widget`；`desktop_widget` 的 `_cards` 改为**按槽位下标**存；
`verify_desktop_widget` 重写 prefs/manager 两组并新增"空槽不挤位"断言；
`verify_automation_ui` 断言功能区三键 + 分隔线下挂件行。全量 62 个脚本与 ruff 全绿。

### 8.4 三处手感修正（2026-11-21 凌晨，用户 m03313 三条）

1. **左栏「桌面挂件」的启用点**：挂件显示时在标签右侧画一个直径 7 的主色实心点
   （`SideNav._paint_widget_dot`），位置按文字实际宽度算。**不**复用功能区那两枚计数
   pill：pill 的形状和"有 N 项提醒"绑定，挂件没有数字可显示，会读成耗材告警。
2. **头部第四个按钮**：`打开主窗口`（↖ 左上箭头，纯箭头不套方框——早先的
   "方框 + 右上外链箭头"在 20 px 下和「全屏」撞脸，用户当晚指出后改掉），点击 emit `open_main_requested`
   （主窗口接 `show_from_tray`）。按钮顺序从左到右 `打开 / 图层 / 管理 / ×`，× 仍贴右
   上角（`_button_rect` 把下标翻过来）。四个按钮都是自绘、没有 QWidget 可挂
   `setToolTip`，所以悬停时手动 `QToolTip.showText` + 离开时 `hideText`。
3. **管理窗宽度不跟列数变**：根因两条——`SlotGrid.__init__` 里 `setMinimumWidth(3 列宽)`
   把父布局的最小宽度钉死（4 列时窗口涨不上去，切回去又被它 / 旧的 `minimumSizeHint`
   顶着不缩），以及 `_activate_size()` 在换布局的同一次事件里读 `sizeHint()` 拿到的是
   **上一轮**的值（Qt 布局要到下一轮事件循环才重算）。修法：删掉固定最小宽、让
   `minimumSizeHint()` 等于 `sizeHint()`；尺寸改成"先立即算一次 + `QTimer.singleShot(0)`
   再算一次"，切到几列就多宽，切回去就收窄。

### 8.5 单列收窄与 6×6 布局（2026-11-21 凌晨，用户 m03533 两条）

1. **单列面板不再撑在 320**：`MIN_W` 320 → 200。320 是早期为了空态那句提示定的，
   有卡片时单列只需 `158 + 20 = 178`，多出来的 142 px 全是空的（用户截图 3×1 一眼看出）。
   现在宽度跟内容走、空态单独抬到 260。
2. **排列方式扩到 6×6 + 拆成两个下拉**：`WIDGET_ROWS_MAX = WIDGET_COLS_MAX = 6`，
   `WIDGET_LAYOUTS` 由常量生成（36 种），`WIDGET_CELLS_MAX = 36`。管理窗从"一个组合框
   选 12 种预设"改成"行 / 列两个下拉框各 1–6"，两轴独立选才能只加行不加列，上界也
   只改常量一处。行数超过屏高时挂件本来就会滚轮滚动（`_max_rows` / `_max_scroll`），
   所以纵向加大不需要额外处理；6 列 = 998 px，1080p 放得下。

### 8.6 紧凑卡去掉描边（2026-11-21 凌晨，用户 m03648）

`WidgetCard` 早先自绘了 `QPen(pal.border, 1)` 的一圈描边，浅色底板上会读成"卡片框"，
和主窗口 `DeviceCard` 不是一套语言——主界面的卡片**没有描边也没有投影**，只靠填充色
与背景的明度差分层次，悬停也只挪一档明度（`theme.card_hover_color`）。现在紧凑卡
改成同一套：`setPen(Qt.NoPen)` + `theme.card_fill(pal.card, pal)`，悬停换
`card_hover_color`。深色主题下卡片比面板底板亮一档，仍然分得清；`card_alpha < 1` 的
渐变预设也照旧透出底板。

### 8.7 底板改用预设 surface（2026-11-21 凌晨，用户 m03677/m03678）

挂件底板早先是 `theme.blend(pal.card, pal.bg, 0.30)`：纯白卡往浅灰底上混，落点
L≈247，卡片 255，只差 8 级——卡片和底板糊成一片。主窗口那片"墙"是 `WindowFrame`
直接铺 `palette.surface`（渐变档是一整块渐变），卡片压上去的明度差是设计好的。

现在底板改成同源：`self.prefs.widget_bg` 有自定义色就用自己的，否则
`QBrush(pal.surface.brush(rect))`——渐变预设连角度/两端色都一致，纯色档就是
`surface.stops[0]`。深色同理（底板取深色停靠点，卡片 `pal.card` 亮一档）。
`scripts/out/widget_sage_light.png` / `widget_night_dark.png` 是对比图。

### 8.8 普通档点击浮前（2026-11-21 凌晨，用户 m03757）

挂件窗口带 `Qt.WindowDoesNotAcceptFocus`——在 Windows 上就是 `WS_EX_NOACTIVATE`。
点它不抢前台、不打断用户正在做的事，这是刻意的；代价是系统**也不会**像普通窗口那样
因为点击而把它提到最前，被别的窗口压住时点半天没反应（用户实测）。

修法：`desktop_zorder.raise_window(hwnd)` 对普通档窗口补一次
`SetWindowPos(HWND_TOP, SWP_NOMOVE|SWP_NOSIZE|SWP_NOACTIVATE)`——只改 z 序、不动
前台窗口。触发点有两处：挂件自身 `mousePressEvent` / `contextMenuEvent`，以及卡片的
按下事件（卡片是独立子控件，事件不冒泡，用 `installEventFilter` 接）。桌面档是
DefView 子窗口、置顶档本来就最前，都不做。

验证：`scripts/out/probe_raise.py`（真窗口 + `WindowFromPoint`）——遮挡窗口压住时
`hit=cover`，点一下 `hit=widget`；去掉这次修复就复现用户说的"点了没反应"。
`scripts/verify_desktop_widget.py` 新增 `test_bring_to_front`（普通档发 `set_pos:0`、
桌面/置顶不发、按下先走这条路）。

#### 补记：只补 SetWindowPos 还不够（同日，用户 m03847 实测）

用户复测："直接点挂件没反应；先点一下主窗口或跑 run.bat 的终端，再点挂件就能上浮。"
说明 `SetWindowPos(HWND_TOP)` 本身没失败（后台进程也能把窗口提到同层最前，
`scripts/out/probe_raise_bg.py` 验过），问题在**那次点击根本没走到挂件的按下处理**：
`WS_EX_NOACTIVATE` 的窗口不参与"点击 → 激活"这条链，窗口管理器在某些前台组合下
不会把这次点击当成交互。

所以普通档现在**允许被点击激活**：`apply_layer()` 里
`zorder.set_noactivate(hwnd, self._layer != "normal")`——普通档清掉
`WS_EX_NOACTIVATE`，桌面档/置顶档保持（它们不需要也不该抢前台）。激活之后立刻把
前台还回去，键盘不会留在挂件上：`nativeEvent()` 在 `WM_MOUSEACTIVATE`（0x0021，
激活之前）里记下当时的前台窗口，`_bring_to_front()` 提层后如果自己成了前台就
`SetForegroundWindow(prev)`。

时序：点击 → 系统激活（因此提层）→ 我们 `SetWindowPos(HWND_TOP)` 兜底 → 前台还给
原来的窗口。挂件没有可聚焦控件（`Qt.NoFocus`），整个过程不会吞键盘。
