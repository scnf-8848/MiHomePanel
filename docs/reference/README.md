# 调研结论索引（先查这里，再决定"能不能做"）

这个目录存**已经花过时间调研的结论**。改代码或回答"某件事能不能做"之前，先在本页  
按关键词搜一遍，**不要从零重新推一遍**。

> **为什么有这一页**（2026-11-11 的教训）：极客版登录码怎么取，`mijia-geek-login-code.md`  
> 里 2026-11-07 就实测过了——云端两条 RPC，不用抓包、不用手机。可后来做入口时又按  
> "只能抓包 / 得另想办法"重新研判了一轮，还去问了用户。文档在、结论在，只是**没人知道  
> 它在那儿**。所以本页的作用只有一个：让"研究过"这件事**可被发现**。

## 怎么用

1. **先查表**：`grep -n "关键词" docs/reference/README.md`，或直接看下面几张表。
2. **表里有结论**：照结论做，需要细节再点进对应文档。结论变了（固件/接口/需求变了）  
   **改原文档 + 改本表状态**，不要另起一份新文档。
3. **表里没有**：才去探测。探测完必须落到三处，缺一处就等于没做：
   - `docs/reference/<主题>.md`——数据、实测过程、边界、未验证项；
   - **本页加一行**（一句话结论 + 状态 + 落地位置 + 关联待办）；
   - `TODO.md` 对应条目——`待研判` / `待排期` / `已落地` / `明确排除`。
4. **只跑通一次不算"可行"**：写成"可行"要能说清"哪条接口、什么前提、失败长什么样"。

## 状态四档

| 状态       | 含义                  | 读法                         |
| -------- | ------------------- | -------------------------- |
| **已落地**  | 代码里有，能指到文件和验证脚本     | 别重复实现，改它                   |
| **待排期**  | 结论已通，功能没做或只做了探针     | 可以直接动手，不必再调研               |
| **明确排除** | 试过 / 算过，判定收益不成立     | **不要重开**，除非前提变了（换了方案、上游变了） |
| **仅参考**  | 视觉、规格、行为契约类资料，不产生功能 | 按它对齐，别当待办                  |

## 一、已落地

| 文档                                                   | 研究的问题                | 一句话结论                                                                                         | 落地位置                                                                                                                           |
| ---------------------------------------------------- | -------------------- | --------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| [mijia-geek-login-code.md](mijia-geek-login-code.md) | 不掏手机能不能拿到极客版 6 位登录码  | **能**：`POST /app/home/rpc/<did>` + `miIO.get_central_link_passcode`，复用现有登录态，不用抓包。取新码 = 旧码立即失效 | `core/geek.py`、`ui/geek_button.py`（按钮）、`ui/device_card.py`（卡片外壳）、`Store.fetch_geek_passcode`；验证 `scripts/verify_geek.py`；卡片见 `docs/dev/ui.md`「中枢网关：极客版入口」 |
| [drag-sort-spec.md](drag-sort-spec.md)               | 拖动排序的行为契约与几何算法（平台无关） | 四段状态机 + 落点规则（单列中线 / 列优先网格 / **本项目行优先网格：行带 + 行内中线，探针取卡片中心**）+ 中断清理；落点在 `ui/dnd.py`（`grid_drop_index` / `DragGuard`）/ `ui/wall.py` | 已验证（拖家庭/房间/设备/场景）；2026-11-18 修「落位错格」「切程序残影」 |
| [fan-purifier-speed.md](fan-purifier-speed.md) | 风扇 / 净化器的"风速"到底是枚举还是区间 | **三种形状**：风扇 `dmaker.fan.p5c` 是 `speed-level` 1–100（range，就是"0–1100"归一化后的结果）+ `fan-level` 4 档，按 `mode` 切换；净化器 `zhimi.airp.rma2` 只有 15 档枚举（无连续量）；浴霸未测。**15 档铺胶囊会"看得见点不到"**。**"按模式换控件"能做**，落地形态是"格子固定、换格子里那个控件" | `ui/fan_body.py`、`card_templates.py`（`BODY_FAN` / `FAN_CONTROLS` / `MAX_CHIPS`）、`core/catalog.py`（`Control.widget` / `kind` / `search_all`）、`core/adapters/dmaker.py`（`speed_by_mode`）；验证 `scripts/verify_fan_card.py`；见 `docs/dev/ui.md`「风扇卡片：两列四格」 |
| [pc-state-api.md](pc-state-api.md) | 系统级 API 能拿到哪些「电脑状态」、要不要外接程序；顺带记三段式规则范围 | **绝大多数不用装东西**：进程 / 运行时长 / 前台窗口 / 空闲 / 锁屏 / 电源纯 ctypes 可读；CPU 封装功率走 EMI 也能读但 **v1 不做**；整机墙上功率没有系统 API。其余契约（条件可空、不做设备条件、三层防重、冷静期默认 1000 ms）见原文 1.4 / 4.1 | **v1 已落地**：`core/automation/pc_state.py`（6 类条件求值，「不可用 ≠ 不成立」）、`engine.py`（`MOD_NOREPEAT` + 规则级 `cooldown_ms` + 重入丢弃，拦截一律记日志）、`logbook.py` + `ui/automation_log.py`（独立日志窗，跨全部规则 + 四类筛选 + `outcome_tone()` 三档结果色 + 增量插入）、`ui/automation.py` + `ui/automation_list.py` + `automation_trash.py` + `automation_log.py`（四层窗口）+ `ui/automation_style.py`（公共卡片 / 说明文字 / 焦点 / 限高）；验证 `verify_automation_rules.py` / `verify_automation_ui.py`；实现取舍见 `docs/dev/automation.md`「电脑自动化：四层窗口」 |
| [automation-hotkeys-and-ui.md](automation-hotkeys-and-ui.md) | 全局热键冲突怎么裁决、热键要不要单独文件、v1 页面形态、规则编辑器选型；**第 7 节：动作时延与"发出去就不等回执"**；**第 8 节：触发日志落盘与批次号** | **`RegisterHotKey` 会话内排他、先到先得**，后注册失败且静默；**键位是每条规则的字段，Windows 不需要文件注册**（撤回 `hotkeys.json`）；规则编辑器定为**三段式表单**、节点画布后置（**11-14 补第 ④ 段「否则执行动作」→ 四段式模板；动作改成「设备 + 功能 + 参数」，旧的 `virtual_event` / `speaker` 不留兼容**）；**11-14 晚：连续动作卡顿的真因是 `SerialWorker` 只有一条线程，`http.client.request()` 只写不读 → 收发分离，`-2` 没发出去 / `-3` 没等到回执必须是两个码**；**11-14 23:xx：日志落盘用 JSONL（一行一条），批次号改字符串 `规则id@触发毫秒-序号`——整数计数器重启归零会让两天的第 3 次折进同一行**；**11-17：编辑页打开时退掉本条规则的热键（`WM_HOTKEY` 是系统层投递，不退就录不到同一个键），同键多条规则各触发一次（排他按组合、不按规则）；11-17 已落地「一个键只分配给一条规则」——停用的也占坑、回收站不占、跨家庭唯一，约束放在保存时的 `key_owner`、运行时一层没动；配套允许"0 条触发"的规则（`restore()` 撞键就清掉那条触发并弹提示）；触发类型规格化成 `TRIGGER_SPECS`，6 处 `type == "keyboard"` 收敛成 `trigger_keys` / `trigger_label`，加新触发源只改一张表**；不做宏 / 键鼠录制、v1 连脚本动作也不做。第 6 节仍是唯一契约索引与明确不做清单 | **v1 已落地**：`core/automation/hotkeys.py` + `hotkeys_win.py`（恒带 `MOD_NOREPEAT`、冲突标红、内存 `id → [rule_id]` 分发）、`rules.py`（`rules.json` 原子写、唯一真相；软删除回收站）、`service.py`（常驻，状态不挂对话框）、`ui/automation.py` / `automation_list.py` / `automation_trash.py` / `automation_log.py`；入口 `SideNav.zone_requested` → `MainWindow._on_zone`；**`core/channel.py`（收发分离，已落地、未真机验）**；验证 `verify_automation_hotkeys.py` / `verify_channel.py`；见 `docs/dev/automation.md`「电脑自动化：四层窗口」 |
| [lock-dual-battery.md](lock-dual-battery.md) | 门锁同时装锂电池和干电池，读数怎么显示 | **型号级适配，只有 4 Pro 拆行**。两块电池的属性名、服务名**完全一样**（都是 `battery-level` / `battery`），`find` 按 siid 升序只能拿到干电池（siid 6 比锂电 13 小） → 卡片一直显示备用的 100%。`adapters/xiaomi.py` 给 `xiaomi.lock.s1` 登记 `batteries=(('13.1','锂电池'),('6.1003','干电池'))`，`catalog.apply_adapter`（型号适配唯一入口）转给 `_expand_batteries` 按 `siid.piid` 执行；顺序即显示顺序，第一条进副标题（`summary_line` 只取前两个指标）。试过通用判据「唯一带 `charging-state` 的是主电池」——语义错，V6 Max 两块都是可充电锂电（用户补充），已撤回；**V6 Max / t1pro 保持改动前的单行** | `core/adapters/xiaomi.py`、`core/adapters/base.py`（`batteries` + `check_batteries`）、`core/catalog.py`（`_expand_batteries`）；探针 `scripts/probe_lock_battery.py`；回归 `scripts/verify_battery_binding.py`（21 项） |
| [camera-wall-layout.md](camera-wall-layout.md) | 监控页的多画面布局：参考谁、本机能开几路 | **参考 DD_Monitor_CE（LGPL-2.1，只学布局不抄码）**，取"左列缩略图 + 一大几小"；**不取** 16 路与自由划格。本机硬约束：每台 1 条 P2P（设备侧 3 个观看位是每台的，不撞）、软解一路约 40% 单核 → **上限 6 路**。两个"只允许一条"的假设必须改：① go2rtc 改配置要重启进程（会掐断在播的流）→ 进页面 `prepare()` 一次预置全量源（**实测未消费的源不拨号**：`consumers:null`、日志零 miss 行）；② relay 单槽 → 按 did 多会话（端口 32108 往后找空位）。远程多路**未在异地实测**；**作者设备不足（摄像头比 `1+5` 的格数少），6 路并发与满配手感没有实测数据**，只在 README / camera.md / ui.md 标注为已知局限 | `core/camera.py`（`prepare` + `_relays`）、`ui/monitor_page.py`、`ui/camera_view.py`（`PlaybackOwnership` 多槽）、`ui/thumbs.py`（模糊静帧）；验证 `scripts/verify_monitor_page.py`；见 `docs/dev/ui.md`「监控页」与 `docs/dev/camera.md`「多路同屏」 |
| [model-adapters.md](model-adapters.md) | 哪些型号被单独适配过、要适配自己的设备该照着哪台写 | **型号适配台账**：现役 3 条（显示器挂灯 1S / 落地扇 1X / 智能门锁 4 Pro），每条写设备名、用了哪些能力、为什么；另记「评估过但没做」的型号（V6 Max、t1pro），避免过几个月又评估一遍。规则本身在 `adapters/<厂商>.py`，台账由 `list_adapters.py --md` 生成；漏更新会判失败 | `core/adapters/*`（`describe_all()` 总接口 + `FIELD_LABELS`）；`scripts/list_adapters.py`、`scripts/verify_adapters.py`；规程见 `docs/dev/conventions.md`「新增型号适配必须登记台账」 |

## 二、待排期（结论已通，功能未做）

| 文档                                                       | 研究的问题                          | 一句话结论                                                                                                                                                           | 关联待办                                                        |
| -------------------------------------------------------- | ------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| [desktop-widget.md](desktop-widget.md) | 桌面挂件怎么做：参考项目怎么实现、本项目要不要照它的窗口/卡片/透明度 | **只参考交互与取舍，不照抄**（它 GPL-3.0，但有两套状态同步与窗口骨架，搬过来破坏我们的一致性）。方案：无边框 Tool + **三档层级「贴桌面」（置底，默认）/「普通」/「置顶」，入口是头部图层按钮 + 菜单三选（不循环切）** + 158×56 紧凑卡 + 32 px 折叠条；窗口大小由列数 1–6 / 行数算，不允许手动拉；**不做半透明**（layered 窗口挂进 `DefView` 整窗不渲染 → 实心底板 + `SetWindowRgn` 圆角）；配置进 prefs v17，几何 400 ms 防抖；轮询登记 `widget` 展开 3 s / 折叠 8 s；点卡片唤起主窗口开详情（详情是模态且要压暗主窗口，不另做一套）。**层级实测（2026-11-20）**：置顶/普通原地 `SetWindowPos(HWND_TOPMOST/HWND_NOTOPMOST)` 切，不重建原生窗口；**「贴桌面」= `SetParent` 到 `SHELLDLL_DefView`（图标层）+ `SetWindowRgn` 圆角 + 提顶** → 能画、排在图标之上、Win+D 仍可见、点击可达、四角不遮挡桌面图标；挂 `Progman`/`WorkerW` 则**不画**（GDI 三种窗口都验过，壁纸软件能画是因为用 D3D）。**贴桌面档不能带 `WS_EX_LAYERED`**（Qt 的 `WA_TranslucentBackground` 必然带它）——带了**整窗不渲染**，去掉立刻正常，整窗 alpha 也救不回来；所以该档只能**不透明底板 + 圆角裁剪**，`widget_opacity` 在该档不生效，且 `desktop ↔ 其它` 切档必须重建原生窗口。菜单不用管：Qt popup 自带 `WS_EX_TOPMOST`，Win+D 下也能点。**桌面窗口只许读**：唯一允许的写操作是把自己的窗口挂进 `DefView`，反过来写它（改 z 序/尺寸）会把 `SHELLDLL_DefView`/`SysListView32` 写成 0×0、用户桌面图标全消失（修法：给 `Progman` 发 `WM_SIZE`）；拍板已定（紧凑卡 / 默认贴桌面 / 图层菜单 / 场景卡 P2 / 圆点 P1 挪到底部状态行 / 不做半透明（实心底板））。**最小 demo 已跑（2026-11-20 05:10）**：点击可用、菜单可用（含 Win+D）、**真杀 explorer 实测**：贴桌面档的窗口 0.03 s 就被一起销毁、Qt 察觉不到（`isVisible` 仍 True、`winId()` 是死句柄）→ 看门狗要 `IsWindow` + 重建、每轮重新解析 DefView；普通/置顶档不受影响；`AutoRestartShell=1` 也不会让 shell 自己回来。透明那项推翻了原设计——05:2x 用户拍板**整体放弃半透明**，三档统一实心底板 + 圆角 region，`widget_opacity` 取消，切档也不再重建原生窗口 | `TODO.md` B 段「桌面挂件」；代码 `mihome/ui/desktop_widget.py` / `desktop_card.py` / `desktop_zorder.py` / `widget_manager.py`；`scripts/verify_desktop_widget.py`。**P0 已落地（2026-11-20），v18 按用户 7 条意见收敛入口与默认档（默认「普通」、设置只留「启动时开启」、管理挂件独立成窗、底部两枚耗材圆点）；v19 再改宫格槽位（头部齿轮直开管理窗、「桌面挂件」入口挪到分隔线下方、家庭→房间→设备三层选择、排列方式 + 槽位拖动、空槽留空）——见该文档 §8 / §8.1 / §8.2 / §8.3** |
| [mijia-automation-events.md](mijia-automation-events.md) | 自动化列表 / 事件通道 / 中枢虚拟事件能不能读、能不能发 | **都能，且不需要局域网**：`GetSceneList`（`app_version:12` + `get_type:2`）；`/miotspec/event/get` 只给"最近一次 + updateTime"→ 只能做触发、不能计数；虚拟事件 = `siid 4 / aiid 1` 发、`4.1` 收（全局单槽）。**转无线键的"按下"不能伪造**（云端无 event/set，只有 `event/get` 读最近一次），要联动只能本地直控/重放/虚拟事件，2026-11-12 定先不做，见该文第 11 节；2026-11-13 用真实事件名**端到端验证通过**（用户确认音箱播报），见 4.2.1；**虚拟事件发送端 11-13 已落地**（2026-11-14 动作重构后位于 `core/automation/action_catalog.py` + `core/miot_action.py`，按 action 名动态查 `siid/aiid`、payload 键是 `in`），事件轮询 / 米家 → 电脑仍待排期 | 历史快照「核心特性」二期部分；探针 `scripts/probe_automations.py` |
| [pc-brightness-follow.md](pc-brightness-follow.md) | 灯跟随屏幕亮度：电脑亮度读得到吗、读到了之后要改哪 | **状态：搁置（2026-11-13 用户定不做）**，调研保留。**本机读不到**（同日实测）：`WmiMonitorBrightness` 整类不支持（台式机）、DDC/CI 报 `0xC0262582`（I2C 通路不通）、电源方案 75% 是死值。**前置条件是显示器 OSD 里开 DDC/CI 或换笔记本**，否则这个场景无从谈起。即使读得到，代码侧还要：`Rule.triggers` 类型（schema 3，`keys` 不再是必填）、轮询 + **防抖/阈值/最小写入间隔**（用户说的"延迟 n 秒"应是触发侧防抖，不是动作延迟）、动作传值（推荐复合动作）或变量系统、`store.set_value` 写通道注入、映射与 0 值/锁屏/多屏边界。**备选：画面平均亮度跟随**——纯软件可做、任何机器都能跑，观感反而更贴合 | `TODO.md`「已作废 / 已取消」里的搁置条目；探针 `scripts/probe_screen_brightness.py`（带 `--watch` 判死值） |

> `fan-purifier-speed.md` 原在此表，**2026-11-12 已落地**，移到「一、已落地」。
> `pc-state-api.md`、`automation-hotkeys-and-ui.md` 原在此表，**2026-11-13 用户「开工」、
> 电脑自动化 v1 落地**，一并移到「一、已落地」；`mijia-automation-events.md` 只落地了
> **发送端**，事件轮询与「米家 → 电脑」仍整块待排期，留在本表。
> `font-swap.md` 原在此表：A 案 11-16 落地、**B 案 11-17 用户定不做**，已闭环，
> **2026-11-17 移到「三、明确排除」**。

## 三、明确排除

| 文档              | 研究的问题       | 为什么排除                        |
| --------------- | ----------- | ---------------------------- |
| [mijia-api-ecosystem-research.md](mijia-api-ecosystem-research.md) | 米家第三方接口生态全图 + 未使用能力逐项评估（事件轮询 / 统计接口 / 插件包 / 云端写接口 / 中枢本地 MQTT） | **统计历史与插件文案运行时拉取已否决**：统计接口只覆盖少数计量设备（7 天窗口仅 4 台插座 + 2 台空调有耗电，温湿度只有独立传感器有），插件包与 spec 非 1:1 映射且专有内容不可分发；云端写接口可行但暂不排期。**2026-11-24 补：耗材清单 `/v2/home/standard_consumable_items` 已只读实测可用**（2 家 14 台 / 17 条，`prop` 15 条能对到 `siid.piid`，自带 `inadeq`/`exhaust` 阈值，还能给双电池分名字）→ **当天已落地为“候选清单”**（空卡弹窗里列建议、点一下填好属性/名称/阈值），见原文 §3.4。**不要重开统计 / 插件那两项**，先看原文 §3 |
| [card-frosted-glass.md](card-frosted-glass.md) | 卡片磨砂玻璃（背景模糊）怎么做、值不值得 | **当前形态看不见，放弃**。卡片本来就是半透明（`card_alpha` 0.76～0.90 压在窗口自绘背景上），而背景只有纯色 / 渐变——离屏实测模糊前后最大差：纯色 **0/255**、渐变 **1～3/255**，等于没做；有细节的图才有 20+ /255。**截图 + 模糊那条路另有结构性缺陷**：卡片父链不画底色，`grab()` 拿不到纯背景（抓顶层窗口会把卡片自己糊进去），逐帧成本几十 ms、缓存后滚动错位。**2026-11-15 用户定不做**（不是"暂缓"）。**万一将来非做不可**（先有「自选图片壁纸」或卡片背后有实时画面），唯一形态是「模糊背景源 + 卡片按 `mapTo` 取切片」，模糊只算一次、每卡一次 blit；系统级 API（Win11 Mica / Acrylic）只能整窗糊桌面壁纸，与 24 预设的设计相反，不采用 |
| [font-swap.md](font-swap.md) | 13 px 下的「笔画毛刺」能不能靠换字体解决、要花多少 | **B 案（换字体）2026-11-17 用户定不做，已闭环**：毛刺是 ClearType 子像素彩边，雅黑 / MiSans / Noto 一样脏，换字体只改字形、彩边一个字都修不了。真正有效的是换字体引擎，**A 案 11-16 已落地**（`ui/fonts.py`，彩边 176→9、布局零位移，`MIHOME_FONT_ENGINE=default` 可退回，回归 `scripts/verify_fonts.py`），落地后灰度字已能接受，"换不换字形"不再排期；子集体积与许可成本只作技术备忘 |
| [process-poll-vs-wmi.md](process-poll-vs-wmi.md) | 「某程序」触发每秒轮询程序名会不会拖慢电脑、WMI 事件订阅能不能避免轮询 | **换 WMI 是倒退，不是优化**：现役 Toolhelp32 快照本机实测 6.7 ms/次；免管理员的 `__InstanceCreationEvent WITHIN 1` 本质仍是每秒快照，微软实测单订阅让 WMI 服务吃 5.8%–7% CPU；真事件型 `Win32_ProcessStartTrace` / ETW **要管理员**，与项目「非管理员」承诺冲突。**11-19 已落地**：采样进 `QThread`（主线程零读进程表）+ 间隔 1s→2s（0.67%→0.34% 单核，用户接受漏 2s 内短命进程） | `core/automation/process_sampler.py`（worker）、`service.py`（`PROCESS_POLL_MS` / `_on_process_sample`）、`pc_state.py`；探针 `scripts/probe_process_poll.py`；验证 `verify_automation_process.py` |
| （散记在 `docs/archive/todo-2026-11-15-full.md`） | 局域网 miIO 直连 | 覆盖率不成立：绝大多数设备没有本地协议入口，不是难度问题。**有实现可比对**：OpenMexia 真做了，代价是每类设备一条补丁（Yeelight 传统协议、BLE 换端点、跨网段超时），见 [openmexia-notes.md](openmexia-notes.md) 第 4 节。注意口径：`ip`/`token` 在设备列表里**拿得到**（`api.py:509`），排除的是"划算"，不是"拿不到" |

## 四、仅参考

| 文档                                               | 内容                              | 用在哪                                             |
| ------------------------------------------------ | ------------------------------- | ----------------------------------------------- |
| [米家App-UI风格参考.md](米家App-UI风格参考.md)               | 真机截图蒸馏出的配色 / 间距 / 层级（px ÷ 2.75） | `ui/theme.py`、`ui/device_card.py` 等视觉对齐；采集原件不入库 |
| [openmexia-notes.md](openmexia-notes.md)                     | 另一个米家桌面端（Electron/TS，AGPL-3.0）的实现取舍：**在线状态随探测回写**、**本机 HTTP 联动接口的发现机制**（随机端口 + 启动令牌 + 发现文件 + Bearer）、**纯规则的中文一句话解析**、**卡片 body 逐控件对照**（第 7 节）、**轮询频率与 BLE 边界**（第 8 节：每台设备一条请求 / 5 秒、电脑蓝牙完全不参与） | 前两条对应历史快照「设备在线状态只在启动时读一次」「电脑接入 · 第 7 步本机接口」；第三条（纯规则一句话解析）**明确排除**（2026-11-14：与顶栏音箱指令执行域重叠、程序内动作鼠标更快，详见该文档第 3 节结论）；第 7 节是风扇 2×1 卡的照抄对象，**已落地**（`docs/dev/ui.md`「风扇卡片：两列四格」，.seg 那条更正过）；第 8 节回答"BLE 走谁"且给了云端频率对照。**只取思路不取码**（AGPL 不能并进 GPL 发行包） |
| [mihome-pc-unofficial.md](mihome-pc-unofficial.md) | 另一个米家桌面端（Python 3.8 纯标准库 + 本地网页 UI，窗口借系统 Chromium `--app=`，MIT，8 MB zip）：**spec 自动生成控件**的对立面样本、**插件文案 / 故障码运行时拉取**、可用性矩阵置灰、局域网 TCP 探测补在线判定，以及**云端写接口的可行性结论**（房间/搬家可做；场景/定时新建要模板；自动化任意触发被 `-998` 拒；房间排序服务端不支持）；**消息中心接口族** `/v2/message/v2/*` 的五个端点与三个坑（`list` 的 `timestamp` 只在 0 响应、已读按 did 分组、`typelist` 无游标），与我们的 `/miotspec/event/get` 不是一套数据 | 结论写进 `TODO.md` B / C 段；**只取思路不取码**（他们不分发 APK 派生的型号目录与卡片布局，我们也不引）。**spec 自动生成 UI 明确不借**（`core/catalog.py` 开头那段就是这条的取舍说明）。**消息中心已落地**（2026-11-23，`core/messages.py` + `ui/messages_popup.py`，悬停浮层 / 当前家庭 / 只读）；详情页左下角同一天试过「单设备消息记录」（只有"会发通知"的 4 台设备有内容）和 `/miotspec/event/get` 的"最新一条"（规格里有事件源的 35 台，但实测不理想），**两版都撤了、位置留空**，代码也删了；接口结论留在 `docs/dev/ui.md`「详情页底部」。§4.5 另补了**我们自己账号的实测**：`typelist` 恒 6 条且 `limit`/`type`/`home_id`/`timestamp`/`offset` 全无效、`list` 无法向后翻页（`type` 必填）、「智能日志」四个猜的路径全 404（插件 SDK 那个 `loadScenesHistoryForDevice` 无 URL）；**家庭 id 只在 `params.body.home_id`，顶层恒 0**（第一版按顶层过滤等于不过滤） |
| [mihome-app/](mihome-app/)（本地目录，**已 gitignore**） | 采集到的原始截图 / 层级 dump；`31_create_automation.jpg` = 创建自动化三段式（11-13 规则结构参考，见 [pc-state-api.md](pc-state-api.md) 第 1 节） | 只在采集者本机，需要时用 `scripts/grab_mihome.py` 重采        |

## 五、还没定论的（谁想动谁先看一眼这块）

这些是**调研过但没结论**或**故意留着没测**的，别当成"没研究过"：

- **极客版登录码放多久会过期**：纯好奇，不是落地前置。2026-11-13 已从待办清单
  移入 [mijia-geek-login-code.md](mijia-geek-login-code.md) **第 6.1 节**（测法一并存档）。
  **测完回填该节，并在本页改状态。**
- **哪些型号真能出极客版地址**：不靠人记，`geek.probe()` 会自己问（判据是"能给出  
  http 地址"）。用户家里只有 `xiaomi.gateway.hub1` 实测过；中控屏 / 带中枢的路由器  
  属于"程序会试、结论等真机"。
- **事件通道能不能做计数 / 历史**：**不能，也不能回看**（只留最近一次）——事件是推送，  
  要统计只能在程序里自己攒。这与统计接口那条结论无关：那个接口只覆盖少数计量设备，已否决。
- **温湿度 / 能源历史有没有云端接口**：**已查（2026-11-23）：可用但覆盖不足**——7 天窗口只有  
  4 台插座 + 2 台空调有耗电统计，温湿度只有独立传感器（`linp.sensor_ht.ks2bb`）有，  
  **已否决、不再作为待查项**。依据：[调研报告 §3.3](mijia-api-ecosystem-research.md)。
- **极客版常驻会话**（ECJPAKE + AES-GCM，程序内直接登录、不再显示码）：技术上可行，  
  工作量明显更大，`mijia-geek-login-code.md` 第 5 节 C 档，**暂不做**。
- **风扇两个速度属性在"错"的模式下写会怎样**：直吹风写 `fan-level`、自然风写
  `speed-level` 是否生效、是否被固件忽略，**都要真改转速才能验**（要写设备，得先申请）。
  见 [fan-purifier-speed.md](fan-purifier-speed.md) 第 6 节。
- **浴霸的风速形状**：家里那台在共享家庭，按测试纪律没碰，仍是空白。

## 六、个人自用存档（不进发行包）

> 这一档不算待办、也不算"明确排除"：方案本身验证可用，但受许可 / 账号风险限制，
> **只保留给个人使用，不分发、不进入发行包、不写进公开功能列表**。要捡起来时按文档
> 从头复现，不要当成项目功能来排期。

| 文档 | 研究的问题 | 一句话结论 | 存放位置 |
| --- | --- | --- | --- |
| [mijia-mqtt-push.md](mijia-mqtt-push.md) | 能不能伪装成官方 HA 集成、直连米家云 MQTT 拿设备推送 | **能**：`cn-ha.mqtt.io.mi.com:8883` + OAuth access_token，一条连接覆盖全账号设备，端到端 < 1 秒；但**拿不到上下线**、QoS 实际为 0、必须逐设备白名单订阅。**仅个人自用、不分发、不复用官方集成代码** | 文档：`docs/reference/mijia-mqtt-push.md`（含通道参数 / 鉴权 / 订阅 / payload / 实测数字 / 缺口 / 最小实现步骤）；探针在仓库外 `e:\Code\_mqtt_probe` |
