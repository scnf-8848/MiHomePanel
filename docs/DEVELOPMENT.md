# 开发说明（MiHomePanel）

面向要改这份代码的人。用户向的安装、使用与功能列表在 [README](../README.md)，
全部文档的入口在 [docs/README](README.md)。

这份文件只回答三件事：**代码分成哪几块、动手前必须守哪几条、细节去哪看**。
专题实现细节已经拆到 `docs/dev/`，本文不再重复。

## 架构总览

单进程桌面程序。PySide6 主线程只画界面，云端访问全部收在一条串行队列里。

```
                小米云 HTTP
                    ▲
                    │ core/api.py        批量读写 / 错误码分类 / 静默期
                    │
core/jobs.py ── 单线程串行队列 ──► 回调进队列
                    │
                    ├─ core/store.py        运行时状态机：卡片状态、乐观写入、轮询集合
                    ├─ core/poll_demand.py  轮询需求登记：取最严间隔，1000 ms 硬下限
                    └─ core/automation/*    规则引擎 / 全局热键 / 设备动作
                    │
                    ▼ 主线程定时器取走（16 ms）
              mihome/ui/*   主窗口、卡片墙、详情页、设置、托盘
```

三个常驻定时器：**16 ms** 排空回调、**3 s** 轮询（退到后台 15 s）、**5 min** 刷新设备清单。
摄像头是唯一的外部进程：可选的 go2rtc，不随发行包分发。

## 模块地图

`mihome/core/`——不认识 Qt 的纯逻辑与云端访问：

| 模块 | 职责 |
|---|---|
| `api.py` | 云端读写、错误码分类、能力学习、子设备路由 |
| `store.py` | 运行时状态机：卡片状态、乐观写入、轮询集合、房间分组 |
| `jobs.py` | 单线程串行执行器（三级优先级 + 主线程回调） |
| `spec.py` | MIoT-Spec 原始规格解析（单位、事件、中文 comment、access） |
| `catalog.py` | 品类 → 卡片形态映射，26 个品类 + 槽位候选表 |
| `adapters/` | 型号级行为适配（一厂商一文件，键是精确型号）+ 类别级文案 |
| `enum_text.py` / `freshness.py` | 云端文案 → 界面短名；读数陈旧判定 |
| `prefs.py` / `paths.py` / `icons.py` | 偏好落盘、数据目录约定、产品图缓存 |
| `health.py` / `alerts.py` / `poll_demand.py` | 轮询健康账本、异常提醒分级、轮询需求登记 |
| `automation/` | 电脑自动化：纯逻辑（rules / engine / pc_state / hotkeys / logbook / action_catalog）与 Qt 适配层（service / hotkeys_win） |
| `camera.py` / `relay.py` | go2rtc 取流、跨网中继 |
| `single_instance.py` / `autostart.py` | 单实例、开机启动 |

`mihome/ui/`——PySide6 界面层：

| 模块 | 职责 |
|---|---|
| `main_window.py` | 主窗口与三个定时器 |
| `chrome.py` | 自绘标题栏 + 无边框窗口（圆角 / 拖动 / 缩放 / 压暗蒙版） |
| `wall.py` / `card_grid.py` / `dnd.py` | 房间墙、混排布局、拖动排序手势 |
| `device_card.py` / `card_templates.py` / `card_controls.py` / `fan_body.py` | 卡片外壳、样式注册表、控件选型、风扇 2×1 内容带 |
| `detail.py` / `camera_view.py` / `ptz_pad.py` | 详情页、实时画面、云台方向盘 |
| `automation*.py` | 规则列表 / 编辑 / 回收站 / 日志四层窗口与公共样式 |
| `settings.py` / `settings_rows.py` | 设置窗口与卡片式骨架 |
| `side_nav.py` / `home_tabs.py` / `scene_bar.py` | 左侧导航与功能区、家庭切换、场景条 |
| `theme.py` / `widgets.py` / `combo.py` / `menu.py` / `tooltip.py` | 配色、基础控件、下拉、右键菜单、悬停提示 |
| `wheel_guard.py` | 滚轮护栏（见「关键契约」第 5 条） |

完整目录（含 `scripts/` 每个脚本的用途）见
[架构与基础 → 目录结构](dev/architecture.md#目录结构)。

## 关键契约

改代码前先认下这几条。它们不是风格偏好，是踩过坑之后定下来的：

1. **所有云端调用走一条单线程串行队列**（`core/jobs.py`）。`requests.Session` 不是线程
   安全的，上游自带节流。唯一例外是 `core/channel.py`（收发分离），默认未启用，只在
   自动化动作上可选，见 [两条云端通道](dev/automation.md#两条云端通道使用范围与差异2026-11-14-定案)。
2. **子线程绝不直接回调 UI**，只把结果塞队列，主线程定时取走执行。
3. **写入用乐观 UI + 版本号**（`core/store.py`），否则"点一下绿了又弹回去"。
4. **设备特判一律按型号**（`core/adapters/<厂商>.py`，键是 `DeviceNode.model` 精确串）。
   **唯一例外是文案**，允许按品类，理由见
   [枚举文案层](dev/ui.md#枚举文案层云端原话--界面短名)与
   [工程约定](dev/conventions.md)。
5. **滚轮只滚页面，不改设备参数**。新控件也必须守，见
   [交互约定：滚轮只滚页面](dev/ui.md#交互约定滚轮只滚页面)。
6. **不确定能不能删的东西，先证明"没人要"**（`scripts/pe_imports.py`），见
   [打包与发布](dev/packaging-release.md#打包成-exe-的实现)。
7. **下"做不到 / 只能抓包 / 得换方案"这类结论之前，先翻
   [调研结论索引](reference/README.md)**（四档状态 + "还没定论的"清单）。2026-11-11
   栽过一次：极客版取码 11-07 就实测通了，做入口时又按"只能抓包"重新研判了一遍。

## 专题文档

| 文档 | 讲什么 |
|---|---|
| [架构与基础](dev/architecture.md) | 数据来源、规格解析与型号适配、完整目录结构、开发环境、本地凭据、单实例、开机启动、系统托盘、踩坑清单 |
| [UI 与卡片](dev/ui.md) | 品类 → 卡片形态、控件库、多键面板、风扇卡、中枢网关、枚举文案、滚轮 / 点压暗区 / 浮窗约定、排序与隐藏 |
| [电脑自动化](dev/automation.md) | 规则模型、热键、条件求值、两条执行通道、四层窗口、日志与回收站 |
| [摄像头画面](dev/camera.md) | go2rtc 取流、首帧耗时拆解、补丁构建、独立窗口、声音、云台、已知边界 |
| [数据与轮询](dev/data-polling.md) | 在线状态、写入确认、轮询健康与异常提醒、轮询频率的取舍 |
| [打包与发布](dev/packaging-release.md) | PyInstaller 打包、裁剪与体积、GitHub Releases 发版流程 |
| [测试与验证](dev/testing.md) | 离屏验证脚本、真机端到端脚本、截图脚本 |
| [工程约定](dev/conventions.md) | 开源致谢、参考材料不入库、git 凭据、文案按类别 / 行为按型号、删打包产物 |

## 其它入口

- **调研结论**：[docs/reference/README.md](reference/README.md)——先查这里再决定"能不能做"。
- **当前待办**：[TODO.md](../TODO.md)——只放还没落地、需要拍板的事。
- **历史记录**：[docs/archive/](archive/)——2026-11-15 之前的完成记录与需求池快照。
- **发布说明**：[docs/release-notes/](release-notes/)——每个版本改了什么，**唯一源**，
  其它文档只引用不复述。
