# 架构与基础

> 专题文档，入口见 [DEVELOPMENT.md](../DEVELOPMENT.md)。
> 本文覆盖：调研结论的存放方式、与参考项目的关系、数据从哪来、规格解析与型号适配、完整目录结构、开发环境、本地数据与凭据、单实例、开机启动、系统托盘、踩坑清单。

> 文中括号里的「N 项」是 2026-11-15 抄录时的验证脚本口径；脚本改动后以实际运行输出为准。

## 调研结论存在哪（下结论之前先看）

调研结论不是写在对话里就算数的，必须落到三处，**缺一处就等于没做**：

| 落点 | 写什么 | 例子 |
|---|---|---|
| `docs/reference/<主题>.md` | 数据、实测过程、接口契约、边界、**未验证项** | `mijia-geek-login-code.md` |
| `docs/reference/README.md` | 一行登记：一句话结论 + 状态 + 落地位置 + 关联待办 | 索引表 |
| `TODO.md` | 对应的待办条目（`待研判` / `待排期` / `已落地` / `明确排除`） | 「数据与账号能力」 |

**状态只有四档**，写清楚能省掉下一轮研判：

- **已落地**：代码里有，能指到文件与验证脚本——别重复实现。
- **待排期**：结论已通、功能没做（或只做了探针）——可以直接动手，不用再调研。
- **明确排除**：试过或算过，收益不成立——**不要重开**，除非前提变了。
- **仅参考**：视觉 / 规格 / 行为契约，不产生功能。

两条纪律：

1. **结论变了就改原文档 + 改索引状态**，不要另起一份新文档——同一件事有两份文档时，
   读者只会看到过期的那份（`mijia-geek-login-code.md` 里"暂不实现 / 按型号找 did"
   两处过期表述就是这么来的，2026-11-11 已就地更正）。
2. **"还没定论的"要显式写出来**（索引第五节）。留白最容易被读成"没研究过"，
   于是又从头查一遍。

## 与参考项目的关系

`huanyuejue/MiHome-Windows`（0.5.0 版）是起点参考项目，能力层写得扎实，本项目**沿用了它的三个核心判断**：

1. 所有网络调用进**单线程串行队列**（`requests.Session` 不是线程安全的，且上游自带节流）
2. 子线程**绝不直接回调 UI**，只把结果塞队列，主线程定时取走执行
3. 写入用**乐观 UI + 版本号**，避免"点一下绿了又弹回去"

界面层完全重写。参考项目是"一个规格属性 = 一个控件"堆在同一张面板上，所以每台设备
长得都一样；本项目建了一层**品类 → 卡片形态**映射，并按米家 UED 的分层原则
（高频在卡片、低频进详情、其余折叠）重排了信息。

## 数据从哪来

| 环节 | 来源 | 说明 |
|---|---|---|
| 登录 | `account.xiaomi.com` 扫码 | 凭据写到 `~/.config/mijia-api/auth.json`，与 mijiaAPI CLI 共用 |
| 家庭 / 房间 | `/v2/homeroom/gethome_merged` | 设备列表本身不带房间，只能靠 `roomlist[].dids` 反查 |
| 设备列表 | `/home/home_device_list` + 共享设备 | 云端下发的 `spec_type` 是一切能力的钥匙 |
| 设备能力 | `miot-spec.org` 原始 JSON | 自己解析，保留 `unit` / `format` / `access` / `comment` / `events` |
| 实时状态 | `/miotspec/prop/get` | 批量读，单请求实测可带 300 条 |
| 控制 | `/miotspec/prop/set` / `/miotspec/action` | 写属性用 `value`、执行方法用 `in`；混用会报 `-704220025`（见踩坑清单） |
| 场景 | `GetSimpleSceneList` / `NewRunScene` | 只给"能在智能页手动执行"的那批 |
| App 自动化 | `AppSceneService/GetSceneList` | 触发 / 条件 / 动作 / 启用状态全在，同账号共享；**必须带 `app_version:12` + `get_type:2`**，否则少给手动场景 |
| 设备事件 | `/miotspec/event/get` | 每个 `(did, siid, eiid)` 的**最近一次**发生 + `updateTime` + 参数；body 必须 `{"params":[…]}` |
| 中枢虚拟事件 | 同 `prop/action`：`siid 4 / aiid 1` | 发一个名字就能触发米家里监听它的自动化；`siid 4 / eiid 1` 是它"发生"的事件 |
| 产品图 | `/v2/productconfig/get_icon` | 302 跳转到 CDN，168×168 透明底 |
| 摄像头画面 | 本机 `go2rtc` + `xiaomi://` 源 | 局域网直连；跨网时前面多一层 `core/relay.py` 代理，见下节 |

**后三行是 2026-11-10 的调研成果**（自动化 / 事件 / 虚拟事件，完整实测见
`docs/reference/mijia-automation-events.md`）。要点：米家 App 的自动化在账号里存着，
`GetSceneList` 能读全；"米家发生了一件事"以前只能靠轮询属性猜，现在有 `event/get`
能直接拉（**只保留最近一次**，所以轮询间隔就是事件的分辨率）；中枢网关的「虚拟事件」
就是规格里的 `siid 4` 服务（**spec 版本 3 才有**，`miot-spec.org` 的 `:1` 里没有），
发一次 `action` 实测 253 ms，发出去到云端可见 356 ms。三件事都走云端账号接口，
**不是局域网直连**，与「局域网 miIO 明确排除」那条决定无关。

**我们只有轮询，但"米家没有推送"这句话是错的**，得把区别说清楚：小米云有一套 MQTT 推送
（`cn-ha.mqtt.io.mi.com:8883`，TLS；主题 `device/{did}/up/properties_changed/{siid}/{piid}`、
`device/{did}/up/event_occured/…`、`device/{did}/state/#` 是上下线），米家 App 与官方 HA 集成
用的就是它——所以按一下实体键，App 里基本 1 秒内就变，那不是"1 秒轮询"，是设备把变化推给
云端、云端推给订阅者。App 退到后台就不更新，是系统把那条长连接挂起了，回到前台重连 + 重拉
一次快照，看起来"一打开就是新的"。

**2026-11-11 之前的判断是「这条通道我们拿不到」，现在要改一半：授权已经实测能通。**
当时的两个理由：（1）它的 MQTT 密码是 OAuth 换来的 service token，client_id 只发给
HA 集成，我们的登录是米家 cookie，没有这条路；（2）那套实现（`XiaoMi/ha_xiaomi_home`）
的许可证写明"仅用于 Home Assistant、非商业"，并且**明确不授权用于开发 APP / Web 服务**
——本项目是桌面 App，属于被排除的那类。

**许可这条结论仍然成立**：只学思路、不抄码、**不进发行包**（与 README 的致谢口径一致）。
授权那条则被 2026-11-11 的实测推翻——refresh 滚动链能持续拿到 token，通道本身能通。
探针项目在仓库外（`e:\Code\_mqtt_probe`，**不属于本仓库交付物**，这里只记结论）：

- **可通**：55 台设备、约 1500 条消息、端到端 **< 1 秒**；payload 是纯 JSON，
  `siid / piid / eiid` 与 MIoT 规格一致。
- **订阅必须按设备白名单逐台订阅**：通配符会被 `SUBACK 135 Not authorized` 拒绝。
- **实投 QoS 为 0**（申请 2），不能当可靠队列；`retain` 全为 `false`。
- **拿不到设备上下线**：`state` 主题零消息，这是当前最大的缺口。
- **鉴权**：`expires_in=259200`（3 天）指的是 access_token；refresh_token 单次使用、
  每次刷新强制换发，持续运行就是一条永续滚动链，日常不需要人工授权。官方 HA 集成把
  到期时间记为 `now + expires_in × 0.7` 提前刷新，刷新失败弹持久通知要求重新认证。
- 观察到个别设备异常高频上报：蒸烤箱 `chunmi.oven.x14`（did 828487133）的腔体温度约
  每 0.4 秒一条，单窗占比 93%，订阅时会把通道淹没。
- **许可证那条判断仍然成立**：只学思路、不抄码、不进发行包。程序**本期仍未接入**。

用户已决定**保留这套 MQTT 方案记录**（仅个人自用、不分发），完整实测、订阅方式与
payload 结构另存于 `docs/reference/mijia-mqtt-push.md`（正在整理）。

上游能给我们的是 HTTP：`/miotspec/prop/get` 批量读（单请求实测可带 300 条）。所以实时性靠
两件事撑：只轮询卡片上真的会显示的属性（约 130 条，一批发完，一个往返 0.3 秒左右），
以及 3 秒一轮的间隔。**想量"我们这条路到底多快"**，用
`scripts/probe_latency.py`：它按指定间隔读一台设备的全部可读属性，把每次变化的时间、
云端 `updateTime` 距读到的秒数、每轮往返都打出来——按一下实体键就能看到"设备→云端→我们"
整条链路的真实延迟，也是判断"要不要加频率"的直接依据（往返 0.15 秒时 1 秒轮询
技术上可行，代价是请求量三倍且频控没有保证；**2026-11-10 用户已明确不做这类压测**，
所以按保守值设计，不再往 1 秒压）。

**2026-11-11 再次确认：轮询提速不做，维持前台 3 秒 / 托盘 15 秒。** 两条理由：设备上报
是**设备→云的上行**、走设备自己的配额，与客户端→云的属性轮询不是同一配额池，不能拿
"设备上报很快"推论"客户端可以读得更勤"；而且端到端 <1 秒的推送通道已经实测存在（见上），
轮询只是兜底，提速收益接近于零，不值得拿账号去压测。

> **2026-11-12 曾补过一档"只在需要时快"**：写指令之后 3 秒把轮询压到 0.5 秒。2026-11-13
> 已删除——实测被写属性有 3 秒静默期、窗口内读不到它，改为"写入确认"（回包后只读那一台，
> 见「写入确认」）。常态始终是 3 秒 / 15 秒。

**还有一条"技术上存在但我们不做"的路：局域网 miIO 订阅。** 云端设备列表里每台设备都带
32 位 `token` 和 `localip`（`tree.DeviceNode.token` / `localip` 已经在用了，摄像头取流就是
按 localip 直连），miIO 的 UDP 54321 上报是设备主动推，局域网内几十毫秒、不占云端额度。
**2026-11-10 用户决定明确排除**：它要求按品类 / 型号各写一套本地会话（握手、token 校验、
上报解析），而设备库是"每台设备一套"的，这个程序一旦装到别人家里，绝大多数型号都没有对应
实现，等于把"通用客户端"降级成"作者自己那几台设备的客户端"——收益（秒内响应）远小于代价
（永远追不上的型号清单）。落款与理由见 `TODO.md`「已作废 / 已取消（不计入待办）」。
**别再拿它当"以后能提速"的退路**：想更快只有提高 HTTP 轮询频率这一条，而那是频控风险，
不是能力问题。

轮询的**范围**由 `store.poll_refs()` 决定，是"在线设备的卡片属性"的并集：`CardSpec` 的
`controls`（卡片上那个主控件）、`metrics`（卡片上的只读读数）、`gangs`（多键开关的每一
路）、以及型号级 `extra`（只读、不渲染，但要用它判断状态，如挂灯的 `scene-mode`）。
`advanced`（"更多功能"里的属性）**不参与轮询**，打开详情页时才按需读一次。

频率换算：3 秒一轮 = 20 轮/分 = **1200 轮/小时**；`DEFAULT_CHUNK = 300` 之后，130 条属性
**一个请求就能发完**，即约 1200 个批量读/小时（原来是 2400）。三条自保：
上一轮没回来就跳过（`store.polling` 闸门，请求不堆叠）、连续失败指数退避（`1s × 失败
次数`，封顶 30 秒）、看不见的时候降频（见下节）。

> **`DEFAULT_CHUNK` 为什么从 120 提到 300**（2026-11-10 实测）：200 条真实属性各跑 3 次，
> `chunk=120` 走 2 次串行请求 510–620 ms，`chunk=300` 走 1 次 261–357 ms，返回条数一样。
> 分批是**串行**的，每多一批就多付一次往返，所以"批大一点"既省请求也快一倍。代价是
> 整批失败时影响面从"丢一半"变成"丢一整轮"，但失败本来就是网络层问题，调用方一律保留
> 旧值 + 退避，这个代价可以接受。
>
> 米家云端对非官方客户端的频控没有公开保证。这个量级对一个云服务不算高，但它是**持续**
> 的——所以托盘常驻之后必须有降频档，不然关着窗口在托盘里待一整天，等于替用户按了
> 一万多次批量读。**再往下压就不是技术问题是风险问题**，用户已明确不测、不冒这个险。

**请求语言钉死 `zh_CN`。** 不钉的话，中文 Windows 上 `locale.getlocale()` 返回的是一个
显示名（`Chinese (Simplified)_China`），服务端认不出就回退英文，卡片上全变成
"Xiaomi Smart Wall Switch (3 Gang)" 这类厂商默认名。

**目前是单账号。** 凭据只有一份 `auth.json`，换号要退出再扫码。多账号同时在线是可行的
（上游 `mijiaAPI(auth_data_path=...)` 支持自定义路径），方案与隔离细节记在 [TODO](../../TODO.md)。

## 规格解析与型号适配

### 型号级适配：只有挂灯会把「无」显示出来

米家显示器挂灯 1S 有**两个**和模式有关的属性，含义不一样：

| 属性 | 可写 | 实际含义 |
|---|---|---|
| `mode`(2,5) | 是 | 用户**上次点过**的预设。手调亮度/色温后它不会变（实测一直停在 5/6/7），拿它当"当前生效模式"会让下拉一直假亮着 |
| `scene-mode`(3,9) | 否 | **真正生效**的预设，会被任何亮度/色温写入清成 0（0 = 无激活项） |

所以读 `scene-mode` 当判据、写仍然走 `mode`。数值 5–9 两边一致，可以直接当枚举值用。

**这件事按型号做，不按品类。** "手调亮度后 `mode` 不变"是这台设备实测出来的行为，
其它灯的 `mode` 会不会跟着变完全没验过；按品类推给所有灯会把别人的选中态弄坏。所以
`schema` 之外单独有一张"型号 → 属性"的表，放在 `mihome/core/adapters/`，**一个厂商一个
文件**：

```
mihome/core/adapters/
  __init__.py   汇总各厂商文件 → MODEL_ADAPTERS，对外只暴露 find_adapter(model)
  base.py       ModelAdapter / TextLayer 结构 + 型号格式校验 + 合并时的重复检查
  categories.py 类别级**文案**覆盖（按 URN 的 category，如 lock）——见下一节
  yeelink.py    yeelink.* 的适配规则
```

一条规则就是一条记录，键是 `DeviceNode.model` 的**精确字符串**：

```python
"yeelink.light.lamp22": ModelAdapter(
    extra=("scene-mode",),          # 额外参与轮询、不在卡片上渲染
    mode_active="scene-mode",       # 模式高亮的判据
    note="mode(2,5) 手调亮度后不变，真实预设看只读的 scene-mode(3,9)",
)
```

按厂商分而不是按品类分，因为查找键就是型号、而型号第一段就是厂商：`yeelink.py` 只管
`yeelink.*`，不会让文件名天天暗示"灯都差不多"。导入期会校验型号格式（必须
「厂商.品类.型号」、不能带前后空格）和重复登记（同一型号写两遍直接报错）——写错了当场
炸，不会拖成"某台设备莫名没有适配"。

新增一个型号 = 在对应厂商文件里加一条，通用逻辑一行不用改。新增一个厂商 = 新建
`<厂商>.py`，再在 `__init__.py` 的 `_MODULES` 里加一行 import 与一项（**不要改成
`pkgutil` 自动发现**：打包成 exe 时静态分析看不到动态导入的模块）。

`build_card(model=...)` 用 `find_adapter()` 命中后把它解析进 `CardSpec.extra`（只读、要轮询）和
`CardSpec.mode_active`（界面判据）。`extra` 由 `store._card_props` 自动并入轮询与寻址，
所以加一个型号适配不用碰 store。属性在 spec 里找不到、或运行时读不到时，两者都是空的，
一切退回默认行为（按 `mode` 自己高亮），不报错也不空高亮。

### 「无」不是一个枚举项，是一段客户端占位文案

往 `value_list` 里塞一个假选项看着省事，但下拉的每一项都能被 `currentIndexChanged` 写出去
——设备不认这个值，它还会被当成枚举值回读回来。所以做法是 `setPlaceholderText("无")` +
`setCurrentIndex(-1)`：框里显示「无」，列表里仍然只有设备给的那几项，用户点任何一项发出的
都是正常写请求。不在枚举里的自定义场景（1–4）和流光（10）同理，显示「其他」。

卡片上的模式胶囊走同一套判据（`CardChips.set_active`）：值为 0 时整排不选中，而不是
硬选某个值。

### 小爱音箱的文字指令：能力在，但 aiid 按型号不同（2026-11-11 落地）

顶栏那条指令条（`ui/speaker_bar.py`）就是这块的界面，底下是 `core/speaker.py`。
设备规格里：

- `wake-up`：**无入参**，只负责唤醒（让音箱进入聆听）。
- `execute-text-directive`：入参是 **`[文本, 是否静默]`** 两个。

**aiid 跨型号不一样**，所以一条 iid 都不许写死，只能按 action 的**名字**查：

| 型号 | `spec_type` | 服务 siid / 文本 aiid | wake-up aiid |
|---|---|---|---|
| 小爱音箱 Pro | `lx06` | 5 / 5 | 3 |
| Xiaomi 智能音箱 Pro | `oh2p` | 7 / 4 | 1 |
| 智能家庭屏 6 | `x6a` | 7 / 4 | 1 |

三处踩过的细节，改这块前先看：

1. **`wake-up` 也跨型号**（3 vs 1）。只按 `execute-text-directive` 查会漏掉它，两个
   action 都只能按名字解析。
2. **`text-content`(piid 1) 与 `silent-execution`(piid 2) 的 `access` 是空数组**。它们
   只作为 action 入参存在，不是可读可写属性 → 能力探测走 `spec.writeable()` /
   `prop_names()` 一个都认不出来，必须看 `ActionSpec.in_piids` 再回服务里按 piid 取名字。
3. **实参顺序不能假设**。`in_piids` 就是实参顺序，但它是数据不是常量：按 piid→名字
   映射后再拼 `in`，否则换个固件版本会把"静默"塞进"文本"里。名字全认不出时退回米家
   的实际顺序 `[文本, 是否静默]`。

服务名在三个型号上都叫 `intelligent-speaker`（URN 第 4 段）。固件改名的退路是：
先搜这个服务，找不到再全服务搜同名 action。

**发出去的时候参数键必须叫 `in`**（`{"did","siid","aiid","in":[文本, false]}`）。
2026-11-11 首次真机实测就是栽在这：`Client.run_action` 沿用了属性那套 `value`，
云端在 action 上只认 `in`，于是"0 个参数 vs 需要 2 个"→ `-704220025`，界面提示
「发送失败」，看着像设备不支持，实际 key 名错。细节见文末踩坑清单。

改完当天真机验收通过：`lx06` 发「现在几点了」`code 0`、203 ms（静默一次），
随后用户在界面上发同一条，**音箱收到并开口回复**——默认非静默路径也通。
所以这条链路只剩"唤醒"（`wake-up`）没在真机上按过一次，文字指令本身已无未知。

**文本清洗**（`speaker.clean`）不是可选步骤：米家 App 的自定义指令只允许中文、英文与
四种中文标点，我们按白名单清洗并**把剔除的字符报给用户**（toast），不静默吞掉。
数字刻意放行——「音量调到 20」离了数字说不通，剔掉会静默改掉语义。200 字符的上限在
`textChanged` 里收口：`setMaxLength` 到点会**静默**截断，用户看不出为什么少了一段；
重载 Python 的 `QLineEdit.insert()` 也拦不住——键盘输入与 `paste()` 走的是
QWidgetLineControl 的内部路径，绕不到 Python 层。

界面侧的三个约定：

- **指令条与警告条占同一条横带**（`ALERT_MARGIN_X/TOP` + `AlertBar.H`）。警告条是不进
  布局的浮层，出现时会正好压住指令条——这是 2026-11-11 定下的预期，别去调解层级。
  左右边距取 28（不是原来的 24），为了和 `WallView` 的 28px 内容边距对齐。
- **目标音箱离线时只禁输入框，不禁左侧选择器**。禁选择器会让用户卡在离线设备上，
  连切走都做不到；名字直接标「（离线）」说明原因。
- **提示条复用 `tooltip.TipBubble`**：新增了 `show_toast(text, anchor, ms)` 走"给位置 +
  定时自己收"的用法，不再依赖鼠标 hover。`TipManager.show_toast` 是它的入口。
- **一次性提示有两条路，别混用**（`tooltip.toast` / `tooltip.toast_top`）：
  - `toast(anchor, ...)` 锚在控件下方，是"这个控件刚才做了什么"的回声（指令条发送
    反馈走这条）。它**程序失焦即收**（`TipBubble._on_app_state`）——它是 `Qt.ToolTip`
    顶层窗口，**会盖在别的程序上面**：用户切走了，这条提示就没有读者了。
  - `toast_top(anchor, ..., kind=)` 落在**屏幕上沿往下 18%、横向居中**，是"刚才那件事
    的结果"（极客版取码走这条）。它**钉住不收**（`_pinned`）、13px + 左侧圆点定色
    （成功薄荷绿 / 失败警告红，边框跟着染）。用户点完按钮浏览器立刻抢焦点，锚在卡片
    上的提示只活在那 0.2 秒里，等于没有。
  - **钉住期间是独占的**：`TipManager._enter` 不再 `_dismiss()`、`_on_timeout` 不弹悬停
    提示、滚轮/点击/`WindowDeactivate` 也不收。这条是实测出来的——`processEvents()`
    里一个排队中的 `Enter` 就能把刚弹出来的通知踢掉，而代码里没人"叫它隐藏"。
  - 程序失焦也看不见的那次教训留下的规矩没变：**提示要么跟着读者走，要么别挡路**。
- **选音箱是一块横向设备面板，不是菜单**（`_Panel`，2026-11-11 按参考图改）：产品图
  56px + 名字横排，像启动器一样一眼认得出是哪台；文字菜单十行里认设备太慢。图标从
  44px 放大到 56px（用户反馈"小得像一排徽标"）：产品图才是认设备的主线索。一排方块
  **等宽**（按最长的名字统一），各自按名字算会出现 111/88/111 这种参差，图标间距忽宽
  忽窄。它是带 `Qt.Popup` 的独立顶层窗口，必须自己透明（`WA_TranslucentBackground` +
  去边框 + 去系统阴影），否则圆角外面还有一块方底——和 `ui/menu.py` 顶部那段是同一个坑。
  **它不进布局**，所以条高 48 与警告条那套几何（`_place_alert`）一个字都不用改。
- **「静默执行」是全局偏好，不是某台音箱的属性**，所以不塞进 picker 面板：那会读成
  "这台的静默"。它只影响"这一条怎么发"，所以就在条上、贴着设备选择器。状态存
  `Prefs.speaker_silent`（v10），默认**关**（米家实测：默认开着问「现在几点」音箱
  一声不出，用户会以为发失败了）。
- **11-13 从小勾选框改成下拉**（用户要求与自动化动作行同一套结构）：整条变成
  `[选音箱 v] [不静默/静默 v] [指令输入框]` 三格。原来的 `_QuietCheck` 与
  `_SilentRow` 两个类**整块删掉了**，勾选框也不在面板左下角了——两个控件管一个状态
  只会互相打脸。
- **下拉要自己压成"弱"的样子**：`Combo` 自带描边和底色，直接放上去像第四个按钮。
  做法是 `combo_qss(pal) + 覆盖块`（透明底、无边框、12px、`text_soft`），
  而且**每次 `set_palette` 都要重设**——`Combo.set_palette()` 会整份重写 stylesheet，
  漏一次切主题就变回白底方块。
- **`set_silent()` 回填要挡信号**：先 `QSignalBlocker` 设索引再判等发
  `silent_changed`，否则"点面板选音箱 → 面板关掉 → 回填静默"会绕成一次假的用户操作。

## 目录结构

```
mihome/
  core/
    adapters/   适配：型号级（一个厂商一个文件，键是精确型号）+ 类别级文案（categories.py）
    automation/ 电脑自动化：纯逻辑与 Qt 适配层混放，__init__.py 只导出前五个纯逻辑模块
      rules.py       Rule / Condition / Action / RuleSet，校验、rules.json 原子写、热键去重、两种动作（设备动作 / 延迟）
      hotkeys.py     组合键规范串 ↔ Win32 mods/vk，保留键拦截，MOD_NOREPEAT
      pc_state.py    纯 ctypes 电脑状态快照 + 条件求值（进程 / 前台窗口 / 空闲 / 锁屏 / 供电）
      engine.py      纯逻辑引擎：条件、冷静期、动作队列、重入、日志；run / call_later 外部注入
      logbook.py     触发日志中文结果名、五列、全文与筛选、按批次折叠成「一次触发一行」
      action_catalog.py 设备动作目录：白名单四功能、可用性判据（入参形状）、参数清洗、家庭筛选
      hotkeys_win.py Qt 适配层：RegisterHotKey + WM_HOTKEY 原生事件过滤器（依赖 PySide6）
      miot_action.py 按 action 名查 siid/aiid、翻入参名、拼 ``in``（音箱 / 中枢 / 动作目录共用）
      service.py     Qt 适配层：常驻 AutomationService，串起引擎 / 热键 / 执行通道 + 成员 QTimer
    api.py      云端访问：批量读写、错误码分类、能力学习、静默期、子设备路由
    health.py   轮询健康账本：失败分四类、不刷屏聚合、快照落盘（重启后可见上次结论）
    alerts.py   异常提醒分级：门槛 / 同类去重 / 冷却，纯逻辑不依赖 Qt
    dev_fault.py 开发用故障注入（MIHOME_DEV_FAULT，默认关闭，只作用于属性轮询）
    camera.py   摄像头取流：找 go2rtc、生成配置、启停子进程（可选外部组件）
    relay.py    摄像头跨网：云端 P2P 授权、小米 relay 握手、本机 UDP 代理（无新依赖）
    catalog.py  品类 → 卡片形态映射（26 个品类 + 槽位候选表）
    enum_text.py 云端文案 → 界面短名：全局词汇 + 规则 + 兜底，三层查找的入口（原文保留在 ValueOption.label）
    freshness.py 读数陈旧判定：updateTime 距今多久，阈值与文案（阈值见 scripts/probe_staleness.py）
    spec.py     MIoT-Spec 原始规格解析（单位、事件、中文 comment、access）
    store.py    运行时状态机：卡片状态、乐观写入、轮询集合、房间分组
    jobs.py     单线程串行执行器（三级优先级 + 主线程回调）
    tree.py     64 条设备条目 → 56 台真机（多键面板的虚拟子设备折叠）
    prefs.py    用户偏好：排序、隐藏（设备 / 场景，含首页分组折叠）、家庭标签样式、卡片样式（prefs.json）
    icons.py    产品图缓存
    paths.py    数据目录约定
    single_instance.py 单实例：命名互斥量 + 注册消息广播（不依赖 QtNetwork），再启动只把已有窗口叫到前台
    autostart.py 开机启动：HKCU 启动项读写 + 路径自愈（只写一个值、只在冻结态开放）
    poll_demand.py 轮询需求登记：属性 / 事件各一张表，取最严的间隔，1000 毫秒硬下限（MIN_INTERVAL_MS = 1000，只约束轮询定时器）
    speaker.py  小爱「执行文本指令」：按 action 名字解析能力号、拼入参、清洗文本（不依赖 Qt）
    geek.py     自动化极客版：两条云端 RPC（取地址 / 取码）+ 能力探测与缓存（geek_ability.json），不依赖 Qt
    timing.py   摄像头取流计时日志（诊断用，**默认关闭**；MIHOME_CAMERA_TIMING=1 打开；单文件 2 MB 自动轮转）
  ui/
    main_window.py  主窗口与三个定时器
    chrome.py       自绘标题栏 + 无边框窗口（圆角 / 拖动 / 双击最大化 / 边缘缩放 / 压暗蒙版）
    confirm.py      自绘确认框（退出登录这类危险操作用）
    home_tabs.py    家庭切换栏（排列方式 × 激活样式，含位移动画）
    wall.py         房间墙
    card_grid.py    卡片墙布局：按单位格占位，1×1 / 2×1 / 2×2 混排，行优先首次适配
    dnd.py          主界面拖动排序的手势状态机（阈值、幽灵、落点下标）
    device_card.py  设备卡片（外壳自绘；宽度与 body 都由卡片样式决定）
    card_templates.py 卡片样式注册表 + CardBody：尺寸表（通用）+ 品类模板（标准 / 亮度+色温 / 亮度 / 状态 / 温度控制 / 扫风+风速 / 实时画面 / 极客版入口 / 隐藏转无线），品类可声明自己默认用哪个；槽位可用性是"全绑"`slots` 或"有其一个"`at_least_one`；枚举超过 `MAX_SEGMENTS` 就退步进器、单选默认是分段轨道
    card_controls.py 卡片内联控件：滑条 / 步进器 / 分段轨道 / 胶囊组 / 开关行（步进器吃到枚举时按下标走）+ make_control()：**唯一的选型入口**，两个 body 都只调它
    fan_body.py     风扇 2×1 的内容带：两列四格（左列扫风、右列模式 + 风速），右下风速格按模式换控件
    detail.py       设备详情页（模式行以 scene-mode 为准的型号适配也在这里）
    camera_view.py  详情页实时画面：QMediaPlayer + 自绘圆角，含加载 / 错误态
    camera_card.py  摄像头卡片：外壳还是 DeviceCard，内容带换成 CameraView（2×2 样式）
    camera_wall.py  墙面取流仲裁：同时只允许一张卡片播放，滚出屏 / 切换即 stop
    camera_popout.py 独立播放窗口：把同一个 CameraView 搬过去，不重新取流
    ptz_pad.py      云台方向盘：点一下一格、按住连转，详情页 / 独立窗口两种形态
    geek_card.py    中枢网关卡片：左半产品图、右半「打开极客版 + 自动复制登录码」大按钮（2×1 样式）
    automation.py         单规则四段式编辑窗（AutomationDialog）
    automation_list.py    规则列表窗（AutomationListDialog / RuleCard）
    automation_log.py     独立触发日志窗（AutomationLogDialog）
    automation_style.py   四个自动化窗口共用的卡片常量 / 样式片段 / 限高
    automation_trash.py   回收站（AutomationTrashDialog / TrashCard）
    scene_bar.py    场景快捷条
    side_nav.py     房间导航
    settings.py     设置窗口：外观 / 排序 / 隐藏 / 账号 / 常规
    settings_rows.py 设置页的骨架：分区标题 + 圆角卡 + 卡内行
    radio_dot.py   只有圆点的单选按钮（名称在左列，圆点在右）
    tray.py         系统托盘：自绘图标 + 菜单（打开主窗口 / 退出），关窗进托盘
    alert_bar.py    顶部警告条：浮层不占布局、不自动消失、恢复时原地变绿
    speaker_bar.py  顶部音箱指令条：[选音箱 v][不静默/静默 v][指令]，回车发给小爱；
                    选音箱展开的是横向设备面板（与警告条同一条横带）
    combo.py        全局下拉框：自绘箭头 + 真透明圆角浮层 + 不遮挡控件 + 禁滚轮 +
                    当前值打勾 + **弹出层按内容撑宽（与控件宽度分开算）**
    menu.py         自绘圆角右键菜单（真透明顶层窗口 + 自建子菜单）
    tooltip.py      自绘悬停提示 / toast：独立计时、圆角、跨程序置顶
    login.py        扫码登录
    theme.py        配色、品类色、QPalette 映射
    widgets.py      流式布局、图标缓存、文本绘制、切换开关
    wheel_guard.py  滚轮护栏：滚轮只滚页面，不改设备参数（WheelSafe / SafeSlider）
scripts/
  probe_cloud.py     只读探测云端结构
  probe_staleness.py 只读探测每条属性的 updateTime 分布（定"读数陈旧"阈值的依据）
  probe_latency.py   只读测量"设备→云端→我们"的延迟：按指定间隔读一台设备，打印每次
                     变化的时间、云端戳距今秒数、每轮往返（判断"能不能靠加频率追上 App"）
  probe_enums.py     只读导出规格里全部枚举文案（离线读缓存），报映射覆盖率与未映射清单
  probe_fan_spec.py  只读探测风扇 / 净化器的「风速」是枚举还是 range（决定卡片上铺胶囊还是滑条），
                     默认只打「1103-强」房间、共享家庭整户跳过
  probe_speaker.py   手动探针（**会出声**）：list 只看支持的音箱与解析出的 iid；
                     wake / send 真的调一次 wake-up / 执行文本指令，验"云端收不收"
  probe_automations.py 米家自动化的三个新接口：list（读全家自动化/场景，只读）、
                     events（盯事件变化，只读）、emit（发中枢虚拟事件，**写**）、
                     latency（发随机名并量"发出→云端可见"，不触发任何自动化）
  probe_pc_state.py  只读探测本机系统级「电脑状态」能力：进程 / 前台窗口 / 空闲 / 锁屏 / 供电，
                     给自动化条件块判断可行性（不注册热键、不锁屏、不发米家请求）
  probe_screen_brightness.py 只读探测屏幕亮度可读路径：WMI 内置屏 / DDC-CI 外接屏 / 电源方案
  verify_api.py      验证读取链路与错误码分类
  verify_store.py    验证状态机（--write 会真的开关一次灯）
  verify_batch.py    房间批量开关的离线验证（假 Client，不碰真设备）
  verify_prefs.py    排序 / 隐藏 / 家庭标签样式的离线验证（临时数据目录）
  verify_freshness.py 读数陈旧提示：阈值/文案边界 + updateTime 落到 DeviceState.stamps 的两处接线（18 项）
  verify_enum_text.py 枚举短名映射：表自检（重复键/长度/原样照抄/类别层键必须英文）+ 三层优先级（层压过精确度）+ 型号级重绑定与 store 记忆 + 解析后接线 + 真实规格覆盖率（92 项）
  verify_tabs.py     家庭标签两种排列 × 三种激活样式 + 胶囊盒模型像素验证（离屏，不连网）
  verify_card_controls.py 卡片内联控件：拖动中不接外部赋值、松手才提交、禁用后不发命令（离屏）
  verify_card_layout.py 设备卡片排版几何断言：标题让位 / 电源位置 / 内容带起点 / 多键按键边界
  verify_locale.py   请求 locale 必须是合法串（zh_CN），否则设备名回退英文
  verify_power_glyph.py 卡片电源符号 ⏻ 的像素级验证（缺口宽度、圆环比例）
  verify_scenes.py   场景隐藏 + 圆角右键菜单（偏好往返、过滤、四角透明）
  verify_theme.py    视觉 token：圆角 / 字号 / 强调色 / 开关尺寸 / 状态胶囊
  verify_tooltip.py  自绘悬停提示：节奏、控件间互不影响、圆角浮层
  verify_tree.py     设置页排序（家庭 / 房间 / 场景，设备层已移除）与隐藏页（设备 / 场景两个 tab）四棵树：结构、展开状态、同层拖动落盘、跨层拖动被拒、恢复默认、聚合开关
  verify_window_corners.py 窗口圆角抗锯齿：四角 alpha 必须是渐变而非 0/255
  audit_catalog.py   品类映射覆盖率体检
  verify_bindings.py 回读验证 siid/piid 是否真有效
  verify_combo.py    下拉框验证：箭头/圆角/滚轮/接线/主题/当前值打勾/弹出层按内容撑宽；
                     --native 查 WS_EX_LAYERED / alpha
  verify_wheel.py    滚轮验证：滑条/卡片/滚动区三种投递路径 + 拖动仍可调值 + 步进器/SafeSlider（离屏）
  verify_dim_click.py 点压暗区关闭子窗口：点遮罩 / 点内部不关 / Esc 一致 / 两种拖拽不关 / 滚轮不关 / 多层只关最上层 / 关掉即销毁（不攒窗口、不留悬空过滤器，含对照）（29 项，离屏）
  verify_close_flash.py 关子窗口不整墙重建：直接关窗 `_apply_theme` / `rebuild_palette` 必须 0 次、设置页里改主题恰好 1 次、关窗后 `WA_UpdatesDisabled` 已放开且压暗层已撤（9 项，离屏；把 `_apply_theme()` 加回去会红 6 项）
  verify_card_style.py 卡片样式：偏好往返与 v3 迁移、尺寸/内容维度分离、注册表自检、品类默认样式、只读读数 body、空调温度控制 body（温度步进器 + 模式分段轨道）与接线、几何、轨道像素、菜单结构、局部刷新、浅色卡片底色可读性、温度 0.5 档、主窗口 → 详情页的接口契约、挂灯模式（型号级适配 + 占位文案 + 适配表校验）（202 项）
  verify_fan_card.py 风扇 2×1 卡：`Control.widget`/`kind` 声明校验、藏在 `dm-service` 里的连续风速也能绑上、同名候选靠 kind 分开、双速度规则只在真绑上时生效且未知模式值不藏风速、枚举步进器收发设备值、选型（声明优先 / 上限 / 窄了也不消失）、分段轨道的选中态像素与段宽分配、两列四格不重叠且换控件不换格、**接线（点轨道/开关/滑条后 `value_committed` 真发出 siid/piid/值，摘掉接线再点一次归零）**、**副标题替第二行报数（开且右值才报 / 自然风只报档位 / 关机与没值时标签与数值都让回来 / 数值那格并进轨道后轨道正好长 48px）**（9 组，离屏，纯几何不读文字）
  verify_card_click.py 点卡片不误开详情页：真实 hit-test 点控件的标签区 / 轨道缝隙 / 滑条整行都不触发 `clicked`，点标题与内容带空白照旧触发；拆掉第一道闸后兜底层仍拦住；两道闸都拆掉时确认 bug 真会发生（离屏，`QTest.mouseClick` 注入）
  verify_float_window.py 浮窗形态：外圈阴影 / 面板几何 / 点击层挖洞 / 七窗内容尺寸 / 限高扣留白（36 项，离屏）
  verify_automation_rules.py 电脑自动化规则：schema / 校验 / v4 动作落盘 / 家庭隔离 / 引擎「提交即下一步 + 失败不拦后续」/ ④ 否则分支（245 项，纯逻辑）
  verify_action_catalog.py 设备动作目录：四功能白名单 / 可用性两道判据 / 参数清洗 / 按 home_id 筛候选 / `refreshes` 只给文本指令（35 项，假 spec）
  verify_miot_action.py 按 action 名字查 siid/aiid 与入参顺序、按名字拼 in（21 项，假 spec）
  verify_channel.py 云端收发器：post 立刻返回 / 发送顺序 / 第 3 条在 1 条回执之前就发完 / 四种结果码 / 密文回执 / 请求形状 / 线程全退出（25 项，假连接，全程离线）
  verify_automation_ui.py 四层窗口 UI：按钮 / 动作行 / 选择设备窗 / 日志筛选 / 日志折叠 / 家庭隔离 / 行内红字（468 项，假 service，离屏）
  verify_automation_hotkeys.py 全局热键注册 + 设备动作通道：假 `user32` + 假收发器，不抢真实热键、不发米家请求（70 项）
  verify_speaker.py  小爱文字指令：三型号能力解析 / 清洗 / 入参拼装 / 指令条三格 / 家庭隔离（139 项，假 spec + 假 API）
  verify_gang_wireless.py 多键面板「转无线」：规格识别（逐路 / 只认同服务 / 值不写死）、卡片改成不可点且不发写请求、详情页行锁住但不写死 `write`（离屏，`--live` 才读一次真设备）
  verify_switch_wired_style.py 「隐藏转无线」样式：只画实际控制通断的那几路、按剩余数量重排、一路不剩退回产品图、全有线时等同标准卡（离屏）
  verify_hidden_fold.py 「已隐藏设备」折叠与按房间分层：只有该分组可折、三角紧贴标题、台数常驻、收起后只剩标题行高、落盘与重建、搜索临时展开、坏数据当展开（59 项）
  verify_single_instance.py 单实例：空着能拿 / 第二个拿不到 / 广播真到子进程窗口并 emit / 进程退出即释放 / 换数据目录互不影响（真子进程，不 mock）
  verify_autostart.py 开机启动：命令格式与旧值解析、四种状态、heal 只在已启用时改写、关掉后值消失、模拟打包态、三种启动形态、设置页第 5 页（46 项，只碰临时注册表键）
  verify_tray.py 系统托盘：图标多尺寸与像素、单击/双击打开而右键不打开、菜单只有「打开主窗口 / 退出」、
                 菜单是透明圆角层（属性 + 样式表）、关窗藏起来但没走退出清理、关掉偏好真退出、
                 托盘不可用时照旧退出、退出前摘图标、自启直接进托盘 / 没有托盘时退回最小化、
                 轮询降频（托盘 / 最小化 / 没显示过 → 15s，回前台补一轮）、
                 挂件要 1 秒时窗口收进托盘顶不掉它、真事件循环里关窗会退出（77 项，
                 离屏没有真通知区域，所以"能不能用托盘"是传进去的参数）
  verify_poll_demand.py 轮询需求登记：取最严、结果不变不回调（否则互相重置）、1 秒硬下限（只约束定时器）、
                 回落（撤最严 → 次严 → 默认）、退订后不再回调、describe 排障文案、
                 写入确认两条序列（目标写 mute+0.2 起、同设备重算不叠加、4 台上限、
                 第 5 次全量读；无目标写 1/2.5/4 秒、固定跑满 3 次、有变化也不早停；
                 1 秒去重、不进健康账本、不动 _poll_timer、reraise 已删除）（69 项）
  verify_poll_health.py 轮询健康账本：四类异常分类与原始码保留、同一错误不刷屏、
                 5 分钟聚合行、类别变化立刻补行、重启后读到上次运行结论（31 项，纯逻辑无 Qt）
  verify_alerts.py 异常提醒：分级门槛、同类去重与冷却、关闭后不再出现、恢复文案、
                 警告条尺寸与关闭信号、"整批失败不能算成功"的判据（99 项，离屏 + 纯逻辑）
  render_preview.py  用真实数据渲染卡片墙 HTML 预览
  grab_mihome.py     米家 App 安卓端只读采集：截图 / UI 层级 XML / 窗口焦点 / 点击坐标（需 USB 调试授权）
  shot.py            主窗口截图（--dark / --filter 关键词）
  shot_detail.py     详情页截图（关键词 / --dark）
  shot_ui.py         多视图截图：主窗口 / 设置页 / 详情页 / 下拉框 / 胶囊标签 / 卡片样式 / 存在传感器状态卡 / 空调温度控制卡 / 已隐藏设备折叠（34 张）
  shot_states.py     交互态截图：房间标题悬停、详情页底部、离线禁用
  install_go2rtc.py  下载可选的 go2rtc（MIT，7.7MB，走 GitHub assets API）：默认官方构建，--patched 取项目 Release 的补丁版（校验 ref / SHA256SUMS）
  build_go2rtc.py    自己编带补丁的 go2rtc（开播 + 云台两处；取快照 + 打补丁 + 下载 Go + 编译安装）
  build_exe.py       打包成可发布的 Windows 目录：PyInstaller onedir、go2rtc 摆到 exe 旁边并附许可证、裁掉用不到的 Qt 模块、产物自带自检 + 无控制台启动冒烟
  pe_imports.py      读 PE 导入表：某个 DLL 依赖谁 / 谁依赖它（判断打包产物里哪些模块能删）
  patches/go2rtc-keyframe.patch  那 12 行补丁本体（probe 之后重起一次视频流）
  patches/go2rtc-ptz.patch       云台那部分：`Client.Motor()` + 命令通道常驻读者 + `GET /api/xiaomi/ptz`
  verify_camera.py   摄像头验证：预加载 / 播放 / 暂停 / 恢复 / 停止 / 离线 / 闲置释放 / 断流归因 / 孤儿清理 / 打包后路径（31 项，离屏）
  probe_ptz.py       真机云台探针：探能力 → 播放等首帧 → 四条指令逐条下发 → 前后帧差异（会真的转，需登录 + 在线摄像头）
  probe_ptz_step.py  真机量步长：单条 / 连发三条各转画面宽度的百分之几（列灰度剖面 SAD 估水平位移）
  probe_concurrent.py 探测同一台摄像头能否被两个客户端同时取流：两个独立 go2rtc 实例各开一路 P2P，只看数据是否还在流
  verify_camera_ptz.py 云台方向盘：落点判定 / 中心孔与圆外不响应 / 置灰不发 / 按住补发 / 不叠在飞指令 / 12 秒兜底 / 详情页显隐（25 项，离屏）
  verify_camera_ptz_ui.py 真机端到端：详情页里按住方向盘 → 指令真被 go2rtc 受理、松手停、控件排在画面下方（需自建构建 + 在线摄像头）
  verify_camera_ptz_popout.py 独立窗口的方向盘：两种形态尺寸 / 四角真的透出去（读像素）/ 贴画面左下角 / 缩放跟随 / 状态驱动显隐 / 关窗停手 / 关掉再打开（40 项，离屏）
  verify_camera_ptz_popout_ui.py 真机端到端：独立窗口里点一下方向盘 → 只发一条、「上」、go2rtc 受理；位置贴画面左下（需自建构建 + 在线摄像头）
  verify_camera_audio.py 摄像头声音：声音不进偏好文件 / `audio` 参数开关 / 每次开播回到静音 / 右下角整排显隐规则 / 三键同尺寸 / 点喇叭不碰播放器 / 搬会话连输出设备一起走（离屏，不联网）
  verify_camera_handback.py 会话交还：关掉详情页后会话搬回卡片（同一播放器、帧号连续）/ 结束通知唤醒 MOVED 卡 / 易主判定（离屏，不联网）
  verify_camera_card_return.py 真机端到端：卡片播 → 双击进详情 → 关掉 → 卡片继续播（走主窗口真正那条路径，需登录 + 在线摄像头）
  verify_camera_popout.py 摄像头独立窗口：按钮可见性 / 搬窗口不重连（同一播放器、帧号连续） / 收回原位 / 右下角缩放 / 与遮罩点击的边界（67 项，离屏）
  shot_camera.py     摄像头详情页截图：就绪态、播放态、闲置释放、错误态（4 张）
  shot_camera_popout.py 独立窗口截图：方向盘常态 / 悬停两张（核对半透明与大小）
  probe_camera_audio.py 音频探针（真机）：流里有没有音轨 / Qt 能不能解出音频 buffer / 开音频是否拖慢首帧（`--audio 0` 是对照组）
  measure_camera_startup.py 首帧计时：把 5–7 秒拆成进程启动 / 云端换票 / 握手 / 等 IDR / 解码
  measure_camera_coldstart.py 冷启动首帧对比：--binary 指到不同 go2rtc 构建（官方 5.3 s / 补丁版 2.2 s）
  measure_app_play.py 走真实详情页点播放计时，--lane lan/remote/keep 分通道对照（局域网 2.1 s / 中继 4.2 s）
```

## 开发环境与依赖

开发和发布都用 Python 3.13，依赖只有 `requirements.txt` 里的四个包：

| 包 | 版本 | 用途 |
|---|---|---|
| mijiaAPI | >=5.0,<6 | 云端签名、加解密、token 刷新（GPL-3.0，决定本项目也是 GPL-3.0） |
| PySide6 | >=6.7 | 整个界面 |
| qrcode / pillow | >=7.4 / >=10 | 扫码登录的二维码渲染与图片处理 |

## 本地数据与凭据路径

程序数据默认在 `%LOCALAPPDATA%\MiHomePanel\`（macOS 是
`~/Library/Application Support/MiHomePanel/`，Linux 是 `$XDG_DATA_HOME/MiHomePanel/`
或 `~/.local/share/MiHomePanel/`），可用 `MIHOME_DATA_DIR` 整体改掉。

| 路径 | 内容 |
|---|---|
| `prefs.json` | 排序、隐藏、卡片样式、主题、摄像头通道等偏好 |
| `cache/spec/` | MIoT-Spec 原始规格缓存，离线时也能画出控件 |
| `cache/icons/` | 产品图缓存 |
| `cache/devices.json` | 设备列表快照，首屏秒开用 |
| `cache/capabilities.json` | 能力学习账本：只记「已确认不支持」的属性；读入时按当前分类自愈，设备规格指纹变了才清该设备的旧账 |
| `logs/` | 自检、崩溃、轮询健康日志；摄像头计时默认不写 |
| `go2rtc/` | 可选取流组件、`BUILD.txt`、许可证与运行日志 |

登录凭据**不在**数据目录的常规路径里：`Client.auth_path` 优先使用上游
`mijiaAPI.auth_data_path`，默认是 `Path.home() / ".config" / "mijia-api" / "auth.json"`；
Windows 上即 `C:\Users\<用户名>\.config\mijia-api\auth.json`。这是 mijiaAPI 的约定，
和程序安装目录无关，也不随安装包分发。`paths.auth_file()`（`<data>/auth.json`）只是
拿不到上游路径时的兜底，正常扫码登录不会写那里。

## 能力账本：读入自愈、设备指纹与手动重置

`cache/capabilities.json` 只回答一件事："这台设备的这个属性，云端明确说过不支持吗"。
记错一条的代价是那个控件**永远**被跳过，所以写入口径很窄：只有
`classify(code) == "unsupported"` 才记，离线、超时、限频一律不记。

- **读入自愈**：每次加载都按**当前**分类规则复查存量记录，不属于"不支持"的直接丢弃
  并回写。这是常态规则，不是一次性迁移——以后分类规则再改，老账本也会跟着收敛。
  2026-11-15 实测：本机 84 条里自愈掉 55 条（30 条离线码、25 条旧分类的
  `-704002000`），剩 29 条。
- **设备指纹**：账本按 did 记 `spec.urn`（含型号与规格版本）。每次拓扑加载后比对，
  **只有指纹变了的设备才 `forget_device`**；没规格的设备整个跳过，不猜。
  实测两次启动：比对期间 HTTP 请求增量 **0**，指纹不变时清账列表为空。
- **手动入口**：设备卡片右键「强制刷新」（清该设备账本 + 立刻重查全部可读属性，
  不弹确认）与「重置设备配置」（账本 + 卡片样式 + 隐藏 + 房间内排序，二次确认）；
  设置页「设备配置」板块的「全部重置」按同一口径清所有设备。三处都不动凭据、
  不向云端写任何东西，见 [UI 与卡片](ui.md)。

## 单实例：再点一次图标只把窗口叫到前台

同一份数据目录只允许一个进程。第二个进程**不启动界面、不连云端**，只广播一句
"我来了"然后退出（退出码 0）。

**判据是数据目录，不是程序名**：`prefs.json`、`cache/`、`auth.json` 全在
`data_dir()` 下，按它算 SHA-1 前 10 位当名字。所以换 `MIHOME_DATA_DIR`（便携版、
第二个账号）可以并行开两个，而同一份数据绝不会被两个进程同时轮询。

**为什么不用 `QLocalServer`**（它是最省事的标准答案）：那要 `PySide6.QtNetwork`，
而 `dist` 里**没有 `Qt6Network.dll`**——打包清单根本没收集 Qt 的网络模块。为了
喊一声多带一个 DLL 不划算，改成只依赖 Win32 的两样东西（`core/single_instance.py`）：

- **命名互斥量** `Local\MiHomePanel-<hash>` 回答"是不是已经有人在跑"。用
  `CreateMutexW(None, False, name)`，只看 `ERROR_ALREADY_EXISTS`，**不要初始拥有者**
  （要了就在同线程重复调用时叠加计数，退出还得 `ReleaseMutex`）。内核对象随进程
  消失，主实例崩了也不会像文件锁那样留下死锁。
- **注册消息** 主实例 `RegisterWindowMessageW("MiHomePanel.Activate-<hash>")` 拿到一个
  会话内唯一的消息号，后来的实例 `PostMessageW(HWND_BROADCAST, 该号)`。字符串相同
  就得到同一个号，不必按窗口标题找窗口（标题会随家庭、设备变，靠不住）。

几个刻意的选择：

- **先抢令牌，再建 `MainWindow`**（`app.py`）。反过来的话两个进程会各刷一轮设备列表、
  各抢摄像头会话（设备侧并发上限 3 路）。
- **过滤器装在 `QApplication` 上**，不是某个窗口上：`QAbstractNativeEventFilter`
  不依赖窗口存在，主实例还在建窗口时消息也不会漏。
- 过滤器里只 `QTimer.singleShot(0, ...)` 转一道再 emit：`nativeEventFilter` 处在消息
  分发中途，直接 `show()/raise_()` 不安全。
- 广播发 **3 次**、间隔 120ms：对面可能正处在启动过程中，单发一次容易丢；总共多等
  240ms，用户点完图标就退出，无感。
- **拿不到令牌一律放行**（返回 True 继续启动）：`CreateMutexW` 失败是异常现场，
  这时最该做的是让程序起来，而不是弹一句"已经有一个实例"。
- `SetForegroundWindow` 在前台锁下会失败并返回 0，Windows 只让任务栏闪烁——不重试、
  不提示，这是系统限制不是 bug。最小化状态要先 `showNormal()`，否则 `raise_()` 只在
  任务栏闪一下。

边界：只有 Windows 实现，其它平台 `acquire()` 直接返回 True（不做检测）。
`--self-check` / `--probe-camera` 在 `main()` 里排在这段之前，所以自检不受"已有实例"
影响——它们是排障工具，必须随时能跑。

**挡下时不能再无声 `return 0`**（2026-11-11）。命令行里"什么都没发生"会让用户、尤其是
用故障注入测链路的人误判成"注入没生效"。现在 `app._report_instance_blocked()` 会明确
打印三件事：已有一个实例在运行、请从托盘菜单选择「退出」（点窗口关闭只是收进托盘）、
以及当前 `MIHOME_DEV_FAULT` 的取值与解析结果（没设 / 无效 / 有效但只对新进程生效）。
打包成 `--windowed` 时没有 stderr，这段提示自然消失——那正是系统通知与顶部横幅的用武之地。

验证：`scripts/verify_single_instance.py`，用**真子进程**占令牌（内核对象假不了），
依次验：空着能拿、第二个拿不到、广播真能到子进程窗口并 emit、子进程一死令牌立刻
能再拿、换数据目录互不影响。子进程不显示窗口，只 `WA_NativeWindow` + `createWinId()`
造一个真句柄——离屏平台没有 HWND，测不出广播。

## 开机启动：一条 HKCU 启动项，加上一个自愈

设置页「常规 → 开机启动」写的是
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 里的一个字符串值
（`core/autostart.py`）：

```
"C:\...\MiHomePanel.exe" --autostart
```

**只写 HKCU，不碰 HKLM**：不需要管理员、不弹 UAC，用户随时能在"任务管理器 → 启动"
里自己禁用。绿色版没有卸载器，可控比"装上就走不掉"重要——所以也没用计划任务
（`schtasks /sc onlogon`）：那需要管理员、残留最难清、杀软最敏感，换来的"延迟启动"
这里用不上。

**只在打包后的 exe 上开放**：源码运行得写 `pythonw.exe -m mihome` 再加工作目录，
venv 一换路径就废。开发态 `supported()` 为假，设置页把开关**藏起来**而不是置灰
——自绘开关看不出"灰"，点了没反应更像坏了；状态行直接写"源码运行时不支持"。

**`--autostart` 是给自己看的**：被系统拉起来时窗口最小化出现，不抢焦点（`present_window`）。
最小化而不是直接收进托盘：Win11 默认把**新的**托盘图标塞进折叠区里，"登录后什么都看不见"
很容易被当成没启动。设备列表照常在后台预热；用户点关闭时才按偏好进托盘。

**路径自愈**：启动项存的是绝对路径，重新打包到别的目录、`dist/` 改名之后旧值就指向
一个不存在的 exe，下次登录弹"找不到文件"。所以每次启动 `autostart.heal()` 比一次：
**只在该功能本来就开着**（`STATE_STALE`）的时候改写成当前路径，`STATE_OFF` 一律不碰
——"启动时顺手把功能打开"是最讨厌的一类自作聪明。

**状态读注册表，不信 `prefs.json`**：用户在任务管理器里禁用之后，偏好里没有任何变化，
开关必须跟着变。四种状态：`on` / `off` / `stale`（开着但指向别的 exe，仍显示为开）/
`unsupported`。写失败（注册表被策略锁住）抛 `AutostartError`，设置页把开关拨回真实
状态并把原因写在状态行里，不静默吞掉。

已知代价：用户直接把目录删掉时，启动项会残留，下次登录 Windows 自己弹"找不到文件"。
绿色版没有卸载器，这是必然的；能做的是**只写一个值、只在一个位置**，别到处撒。

验证：`scripts/verify_autostart.py`（46 项）。**全部读写指到
`HKCU\Software\MiHomePanel-selftest-<pid>\Run` 这个临时键，跑完整个删掉**，既不要
管理员，也不会把用户的"启动"页搞脏。覆盖命令格式与旧值解析、四种状态、`heal` 只在
已启用时改写、关掉后值真的消失、源码运行时抛错，以及用 `sys.frozen` + 假
`sys.executable` 模拟打包态跑一遍真路径。

## 系统托盘常驻：关窗不退出

设置页「常规 → 关闭面板时」默认选**最小化到托盘**：点关闭按钮把窗口藏起来，程序留在
通知区域继续轮询（`ui/tray.py` + `MainWindow.closeEvent`）。菜单目前只有两项：
**打开主窗口** / **退出**（中间一条分隔线）。曾经放过「常用场景」子菜单和「刷新设备」，
用户看过之后要求先去掉——托盘菜单要加什么等想清楚再加，不提前堆功能。

**菜单必须用 `ui/menu.py` 的 `Menu`，不能是裸 `QMenu`**。裸的那层是独立原生窗口，默认
带不透明实心底，QSS 的 `border-radius` 只画出圆角图案，四角外面还是方块底色——真机上
就是"圆角背后有四个黑角"。`Menu` 在构造时就设 `WA_TranslucentBackground` + 去边框 +
去系统阴影（**顺序很关键**：属性必须在原生窗口创建之前设，晚一步就按不透明建好了），
再配自己的 `menu_qss`。菜单是独立窗口，不会继承主窗口的 QSS，所以 `set_palette` 里要
显式给它换一份样式表，否则切主题后是一张白底配深色字的菜单。

**看不看得见，决定轮询多快**（`MainWindow._apply_poll_mode`）：前台 3 秒，收进托盘 /
最小化到任务栏 / 开机直接进托盘（从没 show 过）降到 `POLL_HIDDEN_MS = 15 秒`，切回
前台立刻补一轮。判据是 `isVisible() and not isMinimized()`——挂在 `showEvent` /
`hideEvent` / `changeEvent(WindowStateChange)` 三处，**不去 `hide_to_tray` 里单独调**：
收进托盘是 `hide()`、最小化是窗口状态，两种"看不见"走的是不同事件，只挂一处总会漏。

> 15 秒是折中：再降（30/60 秒）每小时只少几十次请求，但点开时第一眼更容易看到旧值
> （虽然点开就补一轮，那一轮也要几百毫秒才回来）。降频只动 `_poll_timer` 的间隔，
> 请求/失败退避那套逻辑一行没改；`setInterval` 会重新计时，正好是"从后台开始数 15 秒"。

**以后加桌面挂件 / 自动化时，这个判断要换位置**（2026-11-10 **已经换好了**）：

* **桌面挂件是另一个窗口**。主窗口收进托盘、挂件还贴在桌面上时，3 秒那一档不能停。
  一个进程里会有两个"可见性"，各自登记自己的需求才对。
* **自动化不看画面，但有最低频率**。规则要靠轮询喂值，窗口和挂件都关着也得继续跑
  （否则"门开了就锁屏"永远等不到）。它要的是"3 秒、常在"，而不是"看得见才 3 秒"。
  **v1 还没登记**：`core/automation/service.py` 不 import `poll_demand`，所以属性表里
  目前只有 `window` 一条；窗口收进托盘时是 15 秒。写后不再提速，改为对单台设备做
  写入确认（见下一节）。等自动化真需要
  "常在 3 秒"喂值（比如设备条件触发）时，再登记一条 `("automation", ...)`。

所以形态是**需求登记**（`core/poll_demand.py`）：每个消费者报一份"我要多快"
（`(owner, interval_ms, reason)`），`PollDemandBoard` 取最严的那个统一生效，
`_apply_poll_mode` 只是其中一条输入、不再是唯一裁判。

三条容易写错的地方：

- **属性与事件各一张表**。`prop/get` 一轮按 `DEFAULT_CHUNK`（300 条）分片——当前 151 条
  就是 1 个请求；`event/get` 一个请求能带几十个源，频率本来就该各自算。合成一张表就会出现
  "事件要 1 秒，结果把 130 条属性也拖到 1 秒"。
- **结果没变就不回调**。否则挂件要的 1 秒会被"窗口切到后台"那一下重置回 15 秒
  （`verify_tray` 有这条回归）。
- **`MIN_INTERVAL_MS` 是轮询定时器的硬下限，2026-11-13 起是 1000**（2026-11-12 曾降到
  500 给"写后高频窗口"用，窗口被写入确认取代后失去用途，已改回）。谁报 100 毫秒都会被
  收敛。它**只约束 `_poll_timer`**，不约束写入确认的一次性 job（0.5 秒定向读、
  1/2.5/4 秒全量读照旧）。注意它和 `Store.FULL_READ_MIN_GAP`（1.0 秒）是两层规则：
  前者管定时器间隔，后者管"两次全量读至少隔 1 秒"，两者不能互相替代。
  当前现存需求只有 3000 / 15000，所以这次改回是**零行为变化**，属于给未来更快需求的兜底。

主窗口这边：`poll_demand` 的属性表登记 `("window", 3s/15s)`，`_on_poll_interval` 按表结果
设定时器；`poll_events` 已建好但 `idle_ms=None`——**没人登记就不起请求**，不凭空多一路频率。
自动化 v1 没有登记，所以它拿到的就是窗口这条：前台 3 秒、
托盘 / 最小化 15 秒。

## 踩坑清单

这些是实测确认、写进代码注释的：

- **`:focus` 加边框 = 文字被裁 + "按下去没弹回来"**。`sizeHint` 是照没焦点算的，
  `:focus` 把边框从 0 变 2px，内容区当场缩 4px；松手后焦点还在，那个框就一直留着。
  焦点只许换 `border-color`（无描边的按钮常态留透明等宽描边），鼠标点击别给焦点
  （`Qt.TabFocus`）。详见「按钮：焦点只换颜色」。
- **按钮底色必须按"它压在谁身上"算**。同一个错误犯了两次：压卡片的按窗口底算
  （`blend(p.bg, text, 0.12)` 深色 `#292A2D` vs 卡片 `#28292C` = 1.01:1），压窗口底的
  又按 `p.bg` 算（浅色 1.16:1）。卡片上用 `chip_fill`，窗口底上用 `surface_fill`，
  对号入座，别混。
- **透明快照贴到 `QLabel` 上 = 白贴**。`QLabel` 是普通子控件，绘制前 Qt 会按调色板
  把它的矩形铺一遍（离屏 `#EFEFEF`、深色下近黑），pixmap 里 alpha=0 的角露的就是这层
  底色 → "抠了半天还是黑角"。浮层用 `wall.DragGhost`（`WA_TranslucentBackground`）。
- **`QWidget.grab()` 是不透明位图**，而且卡片自绘有 0.5px 内缩，边缘像素**已经在底色上
  混过一次**了 —— 事后再抠角只会得到一圈毛边。正确顺序是"先备透明画布、`setClipPath`
  圆角、`widget.render(..., RenderFlags(DrawChildren))`（**不带** `DrawWindowBackground`）"。
- **"关窗时顺手再应用一次主题"就是闪烁**。`_apply_theme` 里有 `wall.rebuild_palette`
  （整墙 56 张卡拆了重造），叠加在"弹窗销毁 + 点击层隐藏 + 压暗层隐藏"三处整窗
  失效上；主窗口是 `WA_TranslucentBackground`，Qt 在这种窗口上逐控件分段提交，
  于是看得见"卡片变矮、body 全空"的中间帧。主题只在**真的变了**的时候应用，
  收尾动作统一冻在 `_run_dialog` 的 `setUpdatesEnabled(False)` 里合成一次重绘。

- **子设备 did 不可用**。多键面板派生的 `1087931746.s3` 对它发请求要拖到 6 秒才返回
  `-704083036`。云端只认父 did + 该路 siid。`.sN` 的 N 就是 siid。
- **"规格里有"不等于"设备认"**。红外空调的 `ir-mode`、部分门锁的 `lock-action` 回读
  一律 `-704002000`。所以有一层能力学习账本，把确认不支持的记下来，之后跳过而不是每
  3 秒重试一次。**但离线/超时绝不能记为不支持**，否则设备掉线一次就永久失去控制。
- **错误码要看后三位**。格式是 `-70 + HTTP 状态码 + 出错位置 + 错误码`，
  `013` = 属性不可读、`043` = 属性值错误、`011` = 设备离线。硬编码整个码值会漏。
- **`prop/action` 的参数键是 `in`，不是 `value`。** 这是本项目真机翻过车的一处：
  顶栏指令条发「现在几点了」回 `-704220025`（Action 参数个数不匹配），型号、`siid`、
  `aiid` 全对，错在把参数塞进了 `value`——`value` 是**属性**（`prop/set`）的键，
  action 上云端只认 `in`，于是它认为"一个参数都没给"，而这个动作要 2 个。
  正确形状：`{"did","siid","aiid","in":["现在几点了", false]}`（虚拟事件
  `siid 4 / aiid 1` 用 `in:["事件名"]` 实测 `code 0`，是同一条规矩的现成证据）。
  排障第一步永远是 `probe_speaker.py ... --dry`——把 body 原样打出来，不碰设备。
  `-704220025` 的释义来自上游 `mijiaAPI/errors.py`（注释给了 ha_xiaomi_home 的 i18n 出处）；
  我们在 `core/api.py` 的 `_CODE_TEXT` 里只收常用几条，`explain(code)` 翻成人话并**保留原始码**。
- **`updateTime` 不随读取推进**。从没变过状态的插座会显示"3 天前"，所以它只能表示
  "多久没变过"，不能拿来判断数据新鲜度。（现状：用来做「读数陈旧」提示，措辞是
  "未更新"而不是"未变化"，见 `core/freshness.py`。）
  它是**每个属性各自**一份的：`scripts/probe_staleness.py` 采样 320 个读点，绝大多数
  不随读取推进（75/85），少数"每次上报"的量会刷新、那类永远判不出陈旧——属于保守方向；
  32/234 个静态元数据没有这个字段。阈值定 3 天有分布依据。
- **控件的视觉自更新 ≠ 写请求发出去了**。`CardSeg` / `CardToggle` / `CardSlider` 都是
  "先改自己的状态、再 emit `committed`"——接线漏了的话，UI 照样跟着手变，看着像生效了，
  设备一动不动（`FanBody` 就这么漏了一整个版本，见「卡片控件库」第 4 条约定）。
  凡是 body，验证要从 `body.value_committed` 那头断言，**直接连 `widget.committed`
  测的是零件，绕过的正是出事的那一层**。
- **多键面板的按键不能一律用胶囊网格**。2–3 路的按键条只有 28px 宽，竖排文字会被压成
  一条缝，所以 1 路给一整块、2–3 路并排成竖条（标签一个字一行竖排），4 路以上才退回
  胶囊网格。
- **`run.bat` 必须保存为 CRLF 换行 + GBK 编码**。cmd.exe 解析批处理的括号块时要求
  CRLF，用 LF 会把 `(...)` 里的每一行拆成独立命令，报 "'...' 不是内部或外部命令" 和
  "'errorlevel' 不是内部或外部命令"。仓库里靠 `.gitattributes` 的
  `run.bat text eol=crlf` 兜住换行，**但编码靠编辑器自律**——改脚本时别让它存成
  UTF-8，否则中文提示会变乱码。
- **人体传感器大多没有 `occupancy` 属性**，只有 `motion-detected` 事件，属性轮询拿不到。
- **QSS 里的 8 位十六进制是 `#AARRGGBB`**，不是 `#RRGGBBAA`。写 `#9CA3AF66` 想要
  "灰色 40% 透明"，实际得到的是一块橄榄绿。要透明度就用 `rgba()`。
- **系统深色模式会漏进界面**。QSS 只画写了规则的控件，普通容器背景仍来自 QPalette；
  只在 QSS 里写 `QDialog{background:...}` 时，设置页正文会是一块系统深灰。所以
  `theme.qpalette()` 会显式把调色板也设一遍。
- **"取色板里那一档"是个陷阱**。同一支 `card`（深色 #1B1D22）压在设置窗口底
  `surface_bg`（#191A1D）上只差 2–5 个明度档，肉眼看就是一块；浅色主题下反而差 23 档
  白得干净。所以凡是要"浮起来"的容器，都得相对**它所在的那一层背景**推一档——
  `nav_bg` / `rail_bg` / `panel_bg` 三支分开写，谁也别想着复用谁的数值。
  反向也成立：面板比 `card` 亮之后，行悬停 / 选中（`panel_row_hover` /
  `panel_row_selected`）必须重新基于面板底算，否则深色下会变成"挖洞"。
- **没有父控件的 `QWidget` 就是一个顶层窗口**。`QLabel("说明")` 这种写法在
  `setVisible(True)` 的那一刻会真的弹出一个几十像素的窗口（屏幕左上角，一闪而过）。
  设置页十几行一起建，用户看到的就是"点设置有个小窗闪一下 / 许多小窗快速闪现才打开
  设置页"。**把 `setVisible` 挪到 `addWidget` 之后并不能解决**——那时布局本身还没挂到
  任何 widget 上，父控件仍然是 `None`（`SettingsRow.sub` 就踩过这个，见
  `settings_rows.py` 里两个 `QLabel(..., self)`）。凡是"先建再按需显示"的控件，
  构造时一律显式传 parent。验收方式：在 `QApplication` 上装一个拦 `QEvent.Show` 的
  过滤器，断言 `isWindow()` 的命中为空（`verify_autostart.py` 的 `_ShowSpy`）。
- **`QLineEdit` 的长度限制要挂在 `textChanged`，不能只重载 `insert()`**。
  键盘输入、`paste()`、输入法上屏都走 QWidgetLineControl 的内部实现，Python 里覆盖
  `insert()` 只对"自己调 `input.insert(...)`"有效，真实粘贴照样超长。而且 `setMaxLength`
  是静默截断，用户不知道少了什么。做法：`textChanged` 里判长度、就地截断（`blockSignals`
  围住，免得递归）再弹一条 toast。
- **QSS 里写 `qproperty-alignment: AlignRight | AlignVCenter;` 会炸掉整张样式表**。
  Qt 的解析器读到这种带 `|` 的枚举值就停，**后面所有规则一起失效**——表现是卡片
  底色、圆角统统没生效，看着像 objectName 写错了。对齐这类属性老老实实在 Python
  里 `setAlignment()`。
- **`addWidget()` 一旦带 alignment，布局就按 sizeHint 摆放而不铺满单元格**。
  `ElidedLabel` 的 sizeHint 被 `Ignored` 策略压成十几像素，于是"凭据文件"那行的值
  整块消失。要铺满就别传 alignment，把右对齐交给控件自己的对齐属性。
- **无边框窗口的"前后层"要自己造**。系统窗口自带阴影和边框，`FramelessWindowHint` 之后
  就只剩两块同色方块。现在四件事叠着用：子窗口底色离开主窗口一档（浅色更深、深色更浅）、
  外圈 1px 描边、弹出时主窗口整体压暗、**窗口四边留 24px 自己画阴影**（见「子窗口的
  浮窗形态」）。`QGraphicsDropShadowEffect` **对顶层窗口无效**，投影只能开
  `WA_TranslucentBackground` 自己画进窗口矩形里——这就是为什么窗口得比面板大一圈。
- **QSS 的 `border` 不会自动让出内容区**。给 `QDialog` 写完 `border:1px` 之后，布局
  margins 还是 0，子控件会正好盖住那一圈，看起来像没生效。要 `setContentsMargins(1,1,1,1)`。
- **窗口圆角不能靠遮罩**（2026-11-13 更正，这段以前写的是反的）。无边框窗口没有系统
  帮忙裁圆角，QSS 的 `border-radius` 也不会裁子控件；但 `setMask` 只有 1 bit，12dp 的弧
  在 1.75 倍屏上是 21 个物理像素的台阶，跟卡片那边抗锯齿画的圆角放一起明显更毛。
  现在 `WindowFrame` 用 `WA_TranslucentBackground` + `fillPath(抗锯齿路径)`，只有
  ``DimClickShield`` 还在用 `setMask`（它要的是命中区域，不是画出来的边）。
- **浮层（菜单 / 提示 / 下拉列表）的圆角是另一套，不是遮罩**。它们都是独立的顶层
  窗口，平台先铺一层不透明的方底，QSS 的 `border-radius` 只画在方底上，四角外
  仍然是方的。三处统一用 `FramelessWindowHint + NoDropShadowWindowHint +
  WA_TranslucentBackground` 让窗口自己透明（见 `menu.py` / `tooltip.py` /
  `combo.py`）。列表行改自绘 delegate，免得 QStyle 再画当前项的方框和
  `palette.highlight()` 方底。
- **`WA_TranslucentBackground` 必须在窗口创建前设，事后补是假透明**。`QMenu` 在
  构造函数里设，Windows 平台就给它 `WS_EX_LAYERED` + 8bit alpha；下拉框的
  `QComboBoxPrivateContainer` 却是 `showPopup()` 内部懒创建并**立刻显示**的，
  等拿到它再补属性，原生窗口已经按不透明建好（`alphaBufferSize() == -1`），
  四角的"透明像素"在屏幕上合成成纯黑方块。离屏 `QWidget.grab()` 只抓控件自己的
  backing store，四角 `alpha=0` 一片绿——**这类问题必须查原生窗口属性**。
  `Combo._make_layered()` 第一次展开时 hide → 设属性 → 把 `QWindow.format()`
  改成 8bit alpha → `destroy()` 旧 HWND → `setWindowFlags` 重建 → show；
  `verify_combo.py --native` 就是拿 `WS_EX_LAYERED` / alpha 做断言，离屏那套
  和真机这套**都要跑**。
- **浮层底色不能照抄承载它的卡片**。四角黑块没修之前，边界其实一直是黑块在提供；
  修好之后弹出层和设置页分区卡同为 `panel_bg`，整张菜单直接隐身。现在底色走
  `theme.popup_bg()`（深色比卡片亮、浅色比卡片略暗），边界再补一条**自绘**
  抗锯齿圆角描边 `theme.popup_border()`——绝不能写进 QSS 的 `border`，否则
  `QFrame` 会在圆角外又画一圈带斜接的方角框。
- **`QComboBox::showPopup()` 会把当前项对齐到控件本身**，列表正好盖住控件，用户看不到
  自己选的是什么。`Combo._place_below()` 改成右边缘对齐、控件下方留 8px，
  下方放不下才翻到上方，控件和 `^` 箭头全程可见。
- **弹出层宽度必须和控件宽度分开算**。Qt 让 `QComboBoxPrivateContainer` 和 combo
  同宽（老式组合框的"影子"行为），可控件宽度是布局给的：自动化编辑页一行四格，
  单位那格 80px、音箱条上静默那格 88px，「毫秒」「不静默」在列表里只能显示成
  「…」「不…」（用户 11-13 第二轮报）。控件窄是为了不挤邻居，弹出层是浮层，
  没人跟它抢地方——`Combo._widen_popup()` 在 `showPopup` 之后按**最长一项**算宽度：
  文字 + 行内左右留白 + 勾槽 + 竖向滚动条 14px + view 内边距，再用 `setFixedWidth`
  把 container 和 view 一起钉死（上限 `POPUP_MAX_EXTRA=220`，再夹到屏幕内）。
  **两种"看起来对"的写法都不行**：`view.setMinimumWidth` 没用（container 宽度是 Qt
  直接 `setGeometry` 的，不问 view）；只 `resize` 也没用（view 的 `minimumSizeHint`
  来自 `sizeHintForColumn`，布局随后把它顶回去，超长项能一路撑到整屏宽）。
  宽度算完再 `_place_below`，右边缘才对得上新的宽度。
- **下拉列表里的 `State_Selected` 不是"已选值"，是"鼠标/键盘临时停在哪"**。
  Qt 的 `QComboBoxPrivateContainer` 每次鼠标划过都会 `view->setCurrentIndex()`，
  那块跟着跑的高亮只是光标位置；真正的值在 `combo.currentIndex()`，弹出期间不变。
  以前把两者混为一谈，用户一移到别的选项就忘了原本选的是哪项。现在
  `PopupDelegate` 按 `currentIndex()` 给那一行画稳定的选中底 + 一支自绘的
  accent 色 ✓（不用字体，中文黑体缺这个字形会渲染成豆腐块），临时高亮只用
  `theme.popup_row_hover()` 的淡中性底；**每一行都预留同一个右侧勾槽**，否则文字的
  elide 边界会随鼠标忽长忽短。悬停色必须相对 `popup_bg()` 算，沿用相对分区卡的
  `panel_row_hover()` 会在深色下挖出一块暗洞。
- **`QComboBox` 默认吃滚轮**：鼠标停在下拉框上想滚页面，会顺手把设备属性改掉。
  `Combo.wheelEvent` 整个 ignore（键盘上下键保留，那是明确的输入），未接受的
  滚轮事件留给父级滚动区。设置页和设备详情页共用同一个 `Combo`，样式只在
  `combo_qss()` 里写一份。这条已经上升为全局规则，见
  [交互约定：滚轮只滚页面](ui.md#交互约定滚轮只滚页面)——卡片滑条 / 步进器同样不吃滚轮。
- **自绘标签行的盒模型必须所有项一致**。胶囊档曾给每个 rect 都算上左右
  `PILL_PAD`，却只把激活项文字居中、其余左对齐——激活项平白多出 14px 左偏移，
  看起来"明显偏右"，切换家庭时文字还会在居中/左对齐之间来回跳。改成所有项统一
  居中后间距均匀、文字不动。但整排会因此右移 14px，父容器得抵消：
  **Qt 会把负的 `setContentsMargins` 静默换成样式默认值**（实测 `-14` → `9`/`11`），
  所以顶栏改用正边距扣减（`24 - inset`），设置页预览把标签控件当作子控件
  `move(-inset, 0)`（Qt 不裁剪子 widget 的绘制，卡片本身还有 16px 内边距兜底）。
  `HomeTabs.content_inset` 是这个缩进的唯一来源，切样式后必须重新读取。
- **自绘滑条的 thumb 圆心要按半径内缩，轨道不用内缩**。`CardSlider` 原来让轨道和
  圆心共用一组 `x0/x1`：轨道贴边画（详情页 compact 直接是 `x=1`），thumb 圆心也落在
  `x0`，半径 7 的圆有 6px 画到控件外面。`QWidget` 不会裁剪子控件，但**会裁自己的
  `paintEvent`**，所以最左端（`0 分钟` 这种很常见的初值）圆点被切掉一块，看着像被
  遮罩挡了。现在分成两套坐标：`_span()` 返回**圆心**可用范围（左右各内缩
  `_THUMB_R`，含拖动时那 1px 放大），`_track_span()` 再外扩回贴边位置给轨道用；
  填充仍按「轨道宽 × 比例」算，满值时不会在右端留缺口。卡片 body 的滑条同样受益：
  圆心变成贴轨道左端，而不是压在左侧标签区上。
- **截图脚本里 `grab()` 出来的 QImage 带着 1.75 的 `devicePixelRatio`**。直接拿它当
  画布、又按物理像素坐标 `drawImage`，会被再缩放一次，图层缩小甚至跑出画布。
  拼板前先把所有图层的 DPR 抹成 1（`shot_ui.py` 里的 `step_combo()`）。
  同理，**直接 `grab()` 一张自绘圆角卡片，四角外会被填成不透明黑**：按圆角裁剪
  叠上去，抗锯齿边缘已经和黑色混过，浅色底上会留一圈暗描边。正确做法是给它套一层
  填了 `pal.bg` 的父容器再 `grab(容器)`，让边缘和真实界面一样与背景混合。
- **截图脚本的定时回调链会吞异常**：`shot_ui.py` 全靠 `QTimer.singleShot` 串联，
  PySide6 对回调里的异常默认只把 traceback 打到 stderr 然后**继续跑事件循环**，
  脚本就挂在那里等一个永远不会来的 `done()`——不设外层 timeout 会永久卡死，设了
  也只是 `124`，和「图漏截了但正常退出」一样看不出原因。现在装了 `sys.excepthook`
  统一收尾（还原 prefs → 退出循环 → `main()` 返回 1），**只看 exit code 就能判断
  这轮截图是否完整**。这也是上次那批图少几张、却只能靠文件时间戳发现的原因。
  同理，**等 30 秒仍拿不到设备数据也算失败**（`failed=True`）：这时截出来只是一面
  空墙，脚本会直接提示「先运行 run.bat 登录」而不是生成一堆空白图。这条降级路径
  原来直接 `app.quit()` 跳过了 `done()`，会把测试改过的家庭/主题和 `prefs.json`
  留在日常配置里。
- **收起一个装着 `FlowLayout` 的分区，父 `QVBoxLayout` 不会自己收高度**。`_body` 是
  带 `heightForWidth` 的布局宿主，`setVisible(False)` 之后分区仍旧保持展开时的
  高度（实测 746px 纹丝不动，只是卡片不画了，下面多出一大片空白）。`invalidate()`
  也不够，必须 `invalidate()` + `activate()` 显式重排（`WallView._relayout()`）。
  反过来，`apply_card_style()` 里**只** `invalidate()` 不做 activate：那里换的是卡片
  自身宽度，`FlowLayout` 会自己重排，立刻重算根布局反而让滚动位置跟着跳一下
  （`verify_card_style.py` 的「局部刷新自己就会重排」就是盯这件事的）。
- **嵌套的 `FlowLayout` 要显式把 `heightForWidth` 传上去**。「已隐藏设备」按房间分层后，
  卡片流不再直接挂在 `_body` 上，而是各有一层宿主 `QWidget`。宿主必须
  `setSizePolicy(...)` 且 `policy.setHeightForWidth(True)`，否则父 `QVBoxLayout`
  按 `sizeHint` 给高度，分组后卡片被压成一条或撑出空白。
- **`wall.grab()` 是透明的**：墙的底色由主窗口铺，单独抓这个控件只得到一张全透明图
  （`alpha=0`，看着像"全白/全黑"，很容易误判成没渲染）。截图要么抓整个窗口，要么先
  把主题底色铺上去再 `drawPixmap`。
- **小图标要按像素判断，不能用平滑放大**。顶栏刷新图标只有 ~19px，放大 6 倍看
  "还行"的三个候选，换成 `Qt.FastTransformation`（最近邻）一放大全是糊的。另外
  1.7–2.0px 的**描边弧**在这么小的尺寸上会被抗锯齿摊成灰圈，箭头根本读不出来。
- **小尺寸的箭头别自己拿三角拼**。三角箭头的底边是沿**半径**方向的，缩到 20px 后
  那条 6–7px 的底边会读成"环上鼓起一根竖刺"，而它真正指的方向反而看不见——两次
  自绘都是这个毛病（第一版是 290° 弧 + 一根斜线）。改成 Material `refresh` 字形的
  **方头箭头**（一整块实心色块，缩小后仍是一个色块）才读得出来，路径见
  `main_window._refresh_path` 与「开源致谢」里的 Material Icons 一行。教训是：
  这个尺寸下要判断"缩小后还剩几个可辨识的色块"，而不是"放大后线条好不好看"。
- **浅色主题的卡片底色要守一条下限**。`bg #F1F2F4` 只比白卡暗 13，开启态再混 10%
  品类色就只剩 1–3；电视、摄像头这类深灰品类混完**比背景还暗**，卡片整个糊进背景
  （用户报的「电脑」就是这个）。`theme.tint(..., min_gap=6)` 会把染色强度往小收到
  刚好够看为止——亮度是强度的线性函数，解方程即可，不用二分。
- **卡片模板的 body 必须是真实 `QWidget`，不是 `QPainter` 手绘**。拖动捕获、
  移出控件后的追踪、「点空白开详情 / 点滑条调亮度」的事件分流都是白送的。代价是
  三条要自己兜：鼠标移到子控件时**父卡片也会收到 `leaveEvent`**（用
  `QCursor.pos()` 判断光标是否还在卡片 rect 内，别让悬停底色闪一下）；子控件默认
  `ignore()` 的 `ContextMenu` 要在 body 上接住转发给卡片；切主题时子控件不会跟着
  外壳重绘，得显式 `apply_theme()`。
- **右键菜单的子菜单是另一层原生窗口**。`parent.addMenu("标题")` 让 Qt 自己建的那一层
  既没有 `WA_TranslucentBackground` 也没去边框，圆角背后又会冒出方块底——和主菜单
  当初一模一样。用 `menu.submenu()` 显式建同一个 `Menu` 类；勾选也别用
  `QAction.setCheckable(True)`（系统 check indicator 带方底），改成自绘 `QPainterPath` ✓，
  未选项放一枚同尺寸透明图标占位，文字左边缘才齐。
- **悬停提示只在卡片标题带触发**。`tooltip.watch()` 默认把整个控件矩形算命中区，
  可 2×1 灯具的亮度/色温滑条是真实子控件，气泡从光标右下 16px 展开会压住轨道。
  现在 `watch(..., hit_test=...)` 让卡片只认「名称 + 状态文字」那一条（右上电源键
  也扣掉）；Enter / Leave / 定时到点 / MouseMove 四处共用 `_still_inside()` 这一份
  判据——**卡片内部从标题移到内容带只有 MouseMove**，不补这一条提示不会收。
- **提示文字里的 `\n` 必须在测量阶段吃掉**。`TipBubble._wrap()` 是按字宽逐字断行的，
  整串丢进去时 `\n` 会被当成一个普通字符塞进某行中间；可 `drawText` 见到 `\n` 照样
  换行，而每行给的矩形只有 `fm.height()` 高——第二行溢出矩形、和下一行叠在一起，
  看起来就是文字挤成一团（2026-11-11 用户实测截图）。现在先按 `\n` 分段、段内再折行，
  空段保留成空行。provider 里写换行是"作者意图"，别指望折行逻辑替你保留。
- **卡片「大小」和「模板」曾是两份偏好，其实不独立**：模板的 `min_span` 会悄悄
  把用户设的 1×1 撑成 2×1，宽度变成只在切回标准时才复活的影子状态；而
  `2×1 + 标准` 只是把 76px 产品图放进一张更宽的空卡，`1×1 + 双滑块` 根本不可达。
  现在宽度写进样式（`standard` 1×1 / `light_dual` 2×1 / `light_bright` 2×1），
  `prefs.card_styles` 一条记录就是一个 `did → style_id` 字符串。v3 的
  `{span,template}` 在读取时迁移：`template` 沿用为样式 id，只设过 span 的记录
  回默认小卡。默认样式由品类声明（多数设备是 1×1 标准，存在传感器是状态卡），
  菜单第一项就是当前默认（点它会删掉偏好记录），
  所以「恢复默认样式」也删了。改样式仍走 `WallView.apply_card_style(did)` 局部刷新
  ——重建整面墙会连滚动位置、其它卡上的悬停、正在拖动的滑条一起丢掉。
  菜单文字的格式统一成 **`宽卡 2×1：亮度 + 色温`**：尺寸是「占多大格子」的硬信息，
  放在冒号前先读到；同尺寸的项前缀完全一致，纵向扫一眼就能分组，不用逐条读内容。
- **合并的是"用户那一次选择"，不是数据模型里的两个维度**。尺寸（`span`）是**全品类
  通用**的：只有 1×1 / 2×1，定义在 `SIZES` 表里，菜单前段完全由它生成；内容
  （`label` + `slots`）跟品类走，灯是「亮度 + 色温」，空调是「温度控制」。
  `CardStyle.label` **只准写内容名**，写了尺寸会在 `__post_init__` 直接报错——否则
  同一段尺寸就有 `span` 和字符串两个来源。`span` 也必须在 `SIZES` 里（写 3 会在
  import 时报错，而不是菜单上显示成"宽卡 3×1"）。加一个品类的模板只需声明
  `CardStyle(id=..., label="温度控制", span=2, category="ac", slots=...)`，
  尺寸文案、菜单前缀、几何宽度都不用碰。
- **同一内容可以出现在多个尺寸上，靠 id 区分、label 不用加后缀**：两条记录
  `id` 不同、`label` 相同、`span` 不同，菜单自然读成「小卡 1×1：温度」/
  「宽卡 2×1：温度」。**同名判重按"单台设备可见的集合"来做**：两个样式的
  `category` 相同、或任一方是通用的 `None`，它们才可能同时出现在一张菜单里，
  这时 `(label, span)` 撞车必须拒；品类不同的两条（灯的「亮度」和浴霸的
  「亮度」）永不同框，放行。id 若重复会静默覆盖偏好，同样拦掉。
- **`standard` 不是某品类的模板，是所有设备的兜底外壳**：`category=None`、
  `slots=()`，含义是"不挂内容带，产品图 / 多键面板由外壳自绘"。任何品类里
  看到「小卡 1×1：标准」都是同一件事——**回到产品图外观**。
- **「默认」和「标准」不是一回事**。品类可以在 `catalog` 的 `CategoryProfile`
  上声明 `default_style`（存在传感器 → `presence_state`、空调 → `temp_control`），
  表示"没有偏好时用
  哪个样式"，`resolve_card_style()` 三道兜底（没设过 / id 不认识 / 能力不够）
  都落到 `default_style(card)`，而不是硬编码标准。所以：
  - `prefs.card_style(did)` 对"没设过"必须返回 **`None`**，不能预填 `standard`
    ——两者混成一个值，品类默认永远轮不到；
  - 菜单里默认项排第一（排序键 `(不是默认, span)`），且"点默认项"= 删记录；
  - 在默认≠标准的设备上点「标准」是一条**真实偏好**，必须落盘（`set_card_style`
    带 `default_id` 参数），否则用户点完又回到状态卡，菜单像失灵。
  - `catalog` 是 core、样式表在 ui，两处只能靠 id 字面量对齐；
    `validate_profile_defaults()` 在 import 时校验拼写，拼错会当场报错而不是静默
    退回标准。
- **body 不只有滑条**（`CardStyle.body`）：`sliders` 建 `CardSlider`，
  `readout` 建 `CardReadout`（只读大字），`controls` 则**按属性类型各配一种控件**
  （布尔 → 开关行、枚举 → 分段轨道（2026-11-12 前是胶囊组）、数值 → 步进器）；
  空调「温度控制」是第一个用户：2×1 里一行「− 26°C +」加一行五个模式
  （模式那行现在是轨道，见「模式和角度：轨道分段控件」）。**减号在数字左侧**是刻意的：
  卡片 body 有 312 宽，两个按钮都挤在右边时手指要在同一处分辨减/加；
  数字两边各一枚和空调遥控器一致，左边调低、右边调高。详情页那一行左侧还要
  放属性名，仍是「数值 …… − +」。存在传感器的卡片正中一个「有人」，
  副标题仍然是「有人 · 49%」——**不为了去重去动副标题**：`skip` 需要知道 body
  实际建出了哪些控件（声明了但没绑上的槽位会被 body 丢掉），而副标题和 body 是
  两条独立的渲染路径，耦合后详情页（`DeviceState.subtitle`）那份还得另算一遍。
  让副标题只认 `card.metrics`，换样式时它一个字都不变，代价只是重读一次状态。
  两种 body 的可用性判据不同：滑条要求 `value_range`（没刻度映射不了轨道像素），
  读数只要属性存在。槽位查的是 `CardSpec.bound()`（**先控件后读数**），
  所以样式可以引用 `occupancy` 这种登记在 `metrics` 里的只读指标，不必知道
  它当初登记在哪一边。
- **2×1 宽卡的 body 要主动用掉宽度**。卡片内容带是 312×78，而「数字 + 两个按钮」
  只有 ~130 宽：按详情页那套「数字靠左、按钮贴右」排，中间会空出 200px，看着像
  少画了一块。所以给 `CardStepper` 加了 `centered`（整组居中）、给 `CardChips`
  加了 `spread`（余宽均分给每一项，变成等宽分段控件），两个开关**只在卡片 body
  里打开**，详情页那套默认行为一个字没动。温度用步进器不用滑条，是因为它是个
  精确的小整数（16–32），拖拽反而选不准——这也是米家的做法。
- **可见性只有一条链路**：`CardStyle.available()` 先卡 `category`（`None` = 通用），
  再按 body 类型要求 `slots` 里每个槽位都真绑上属性。`catalog.build_card()`
  会把没绑上的槽位直接丢掉（只对 `critical` 的记进 `missing`），所以卫生间那种
  只能调亮度的平板灯 `card.bound("color_temp")` 返回 `None`，菜单里就没有
  「亮度 + 色温」，只剩「标准」和「亮度」；如果它以前存过双滑块，`resolve_card_style()`
  会兜回默认样式，不会留一张空卡。
- **`QMediaPlayer` 重播同一个 RTSP 地址必须先 `stop()`**。直接 `setSource(同一 URL)`
  会被判定成「源没变」而不重连；表现是点重试毫无反应、画面一直停在错误态。
- **Qt 的 RTSP 第一次 `SETUP` 会收到 `461 Unsupported transport`**，然后自己回退 TCP。
  日志里那行不是错误，别照着它去改传输方式。
- **`QGuiApplication` 下建 `QWidget` 会直接把进程带走**：退出码 127、没有任何 traceback。
  截图 / 验证脚本只要碰控件就必须用 `QApplication`，哪怕平台是 `offscreen`。
- **go2rtc 的 `frame.jpeg` 每帧都要调 ffmpeg 转码**（实测 700ms/帧），适合「抓一张图」，
  不适合当视频源。实时播放走 RTSP 直通，解码交给 Qt 自带的 FFmpeg 后端。
- **go2rtc 的 API 比协议模块先就绪**。`xiaomi` 排在 `main.go` 的 `modules` 列表最后，
  `GET /api` 已经 200 的时候它可能还没 `HandleFunc("xiaomi")`；此时发来的第一条 RTSP
  请求会报 `unsupported scheme: xiaomi://…`，而且这个错误会被 stream 缓存下来，后面
  再连也一直失败。所以 `start()` 在 API 就绪后要再等 300ms，控件首帧前的报错也自动
  重连一次。症状很怪：同一个配置手动跑必成功、从程序里跑约 1/3 失败。
- **Windows 上不能用 `SO_REUSEADDR` 探测端口空闲**。它允许 `bind` 到别人已经 `listen`
  的端口（Linux 不允许），于是「端口占用检测」永远返回空闲，新 go2rtc 抢不到 8554 却
  以为起来了，Qt 连上旧实例、错误还对不上。要用 `SO_EXCLUSIVEADDRUSE`。顺带把就绪
  探测从 `/api` 换成 `/api/streams` 并核对流名，避免连到别人的实例还以为是自己。
- **`offscreen` 平台下 Qt 的字体库是空的**，`QFont("Microsoft YaHei UI")` 拿不到字形，
  `draw_text` 画的中文全是方块（`QFontInfo().family()` 返回空串）。离屏脚本的截图
  只能用来看布局，核对文字得用 `shot_camera.py` 这种真实窗口。
- **"返回值是 `Mapping`"不能当成功判据**。整轮网络失败时 `Client.read()` 返回的是空
  dict——那也是 `Mapping`，于是失败被记成成功，指数退避 `min(6 × 次数, 30)` 永远不生效，
  状态栏还会白写一句"已重新连接"。现在由请求层的 `Client.last_poll_ok` 回答"这轮到底
  读回来东西没有"，`Store` 只看它；没有可读属性、全部确认不支持的空转仍算正常。
- **轮询异常在 `api.py` 就被吞掉了**，到 `Store` 只剩空结果，分不清"没属性可读"和
  "请求挂了"。所以健康记账（原始错误码 / 异常文本 / 分类）必须放在请求层，`Store`
  只补一句"触发了多少秒退避"。
- **MQTT 订阅不能通配**。`device/{did}/…` 用通配符会被 `SUBACK 135 Not authorized`
  拒绝，只能按设备白名单逐台订阅；实投 QoS 0（申请 2）、`retain=false`，不能当可靠
  队列。`state` 主题实测零消息，**上下线拿不到**。
- **`expires_in=259200` 是 access_token 的 3 天，不是 refresh_token 的有效期**。
  refresh_token 单次使用、每次刷新强制换发，持续运行才是永续滚动链；官方 HA 集成按
  `now + expires_in × 0.7` 提前刷新，刷新失败必须重新认证。
- **小爱音箱的 `execute-text-directive` 不能写死 siid/aiid**：`lx06` 是 5/5，
  `oh2p` / `x6a` 是 7/4，入参固定为 `[文本, 是否静默]`，只能按 `spec_type` 解析
  （详见「规格解析与型号适配」一节）。
- **自绘 `Qt.Popup` 面板要自己管生命周期**。它不像 `QMenu.exec()` 会替你跑完再返回，
  `hide()` 之后对象还在；每次点开就新建一块的话，不处理就会一直挂在指令条底下。
  现在的收口是 `hideEvent` 里 `deleteLater()`——选设备、点外面、按 Esc 三条路都会走到
  它。另外**整行可点的开关**要确认子控件吃掉了自己的点击：`ToggleSwitch` 处理鼠标后
  不调 `super()`，所以行级 `mouseReleaseEvent` 不会和它双重切换。
