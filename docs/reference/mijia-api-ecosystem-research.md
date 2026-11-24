# 米家第三方接口生态调研（2026-11-23）

> **调研范围**：只读。没有登录真实账号、没有对任何设备发起写操作、没有安装依赖、没有修改仓库文件。
> 依据分三类：① 本项目已实测的调研笔记与代码；② 当前 `.venv` 内 `mijiaAPI 5.0.0` 的实际源码；
> ③ 公开的 GitHub / PyPI / 小米官方文档。
>
> **可信度标记**：
> - **本项目实测**：仓库里有探针、日志或结论文档，能指到具体路径；
> - **官方公开**：小米官方文档、官方仓库 README / LICENSE；
> - **社区逆向**：第三方库或社区项目已实现，但小米没有公开承诺；
> - **推测**：只有间接证据，尚未验证。
>
> **本项目硬约束**：共享家庭中的设备一律禁止任何状态改变。后文凡涉及写操作，都会单独标注边界。

---

## 0. 一页结论

### 0.1 两个问题的直接回答

**问题 1：有没有统一的、被广泛采用的上游项目？**

**没有跨层的统一上游。** 这个生态天然分成四层，每层各有事实标准：

| 层 | 事实标准 | 说明 |
| --- | --- | --- |
| 个人米家账号的云端 HTTP RPC | **mijiaAPI（Do1e）** | Python 社区里最完整、最活跃的云封装之一；本项目正在用。但它只是“Python 云封装”这一层的常用选择，不是全生态标准。 |
| 局域网 miIO / 本地 MIoT | **python-miio** | 本地控制层的事实标准；GPL-3.0，2026 年仍在提交，0.6.0 预发布已支持 genericmiot。 |
| 云端 MQTT 推送 + 本地中枢网关 | **XiaoMi/ha_xiaomi_home（官方 HA 集成）** | 唯一官方实现；但许可证是**专有许可**，不能复用进本项目发行包。 |
| HA 生态的“云 + 本地 + spec 自动映射” | **al-one/hass-xiaomi-miot** | Apache-2.0，6.2k 星，2026-11 仍活跃；但它是 HA 专用集成，不是通用 SDK。 |

**结论**：本项目继续用 `mijiaAPI` 是合理的；没有“换一个上游就能统一云端 + 局域网 + 推送”的选项。真正该考虑的是“在现有上游之上补哪几个接口”，而不是“换上游”。

**问题 2：有没有“接口实际存在、但本项目还没用上”的能力？**

有，按对本项目的价值 / 实现代价 / 风险排序：

| 优先级 | 能力 | 状态 | 价值 | 代价 | 风险 |
| --- | --- | --- | --- | --- | --- |
| **P0** | `/miotspec/event/get` 设备事件轮询 | **本项目已实测，生产未接**（`poll_events` 表已留位、无消费者） | 把“米家 → 电脑”从 3 秒属性轮询提升到“轮询间隔 + 0.4 秒”，能拿到单击 / 开锁 / 门铃 / 人体等**瞬时事件** | 低～中：一个只读 RPC + 去重 + 规格映射 | 只保留“最近一次”，不能计数 / 回看；频控未知；共享家庭只读，安全 |
| **P0** | `GetSceneList` 读全家自动化 | **探针已通，生产未接**（生产只用 `GetSimpleSceneList` 读手动场景） | 展示已有自动化、看清虚拟事件被谁监听、辅助“发虚拟事件” | 低：一个只读 RPC | 私有接口，`app_version` 可能变；不要用它改自动化 |
| **P1** | `/v2/user/statistics` 统计接口 | **已实测（2026-11-23，只读）**：接口可用，但覆盖有限 | 能耗 / 电量历史；另有 1 台温湿度传感器能取到温湿度历史 | 中：按型号 / key 建能力表、300 点翻页、单位校准 | 多数温度 / 湿度 / 电池 / 电压没有统计；同一属性名的数值单位不统一，必须按型号校准 |
| **P1** | `/v2/home/standard_consumable_items` 耗材清单 | **已实测（2026-11-24，只读）**：可用；2 家 14 台 / 17 条，`prop` 15 条能对到 `siid.piid`，自带阈值 | 自动发现“哪些设备有耗材”、给双电池分名字（5号电池 / 锂电池）、给默认阈值 | 低：一个只读 RPC，两家 3 秒内 | 覆盖只到“云端登记过耗材”的型号（本机 14 / 64，其余本来无耗材）；两种 `prop` 写法未解。**已落地（2026-11-24）：当“候选清单”，不推翻“用户自选属性”** |
| **P2** | 房间 / 设备管理写接口（改名、搬家、房间 CRUD） | **同类项目已验证，本项目未用** | 补全“管理”能力 | 中 | 写账号配置，风险高于写设备属性；**只能用于用户自己创建的家庭** |
| **P2** | 中枢网关本地 MQTT | **官方 HA 已实现，本项目未验证** | 局域网内低延迟推送 / 控制，覆盖 BLE / ZigBee 子设备 | 高：官方协议未公开、实现专有许可、需要中枢网关 | 许可证与逆向风险都高；不建议现在做 |
| **不做** | 云端插件包 `/v2/plugin/fetch_plugin`（仅枚举文案） | **已实测（2026-11-23，只读）**：16 个型号中 11 个有包；spec 的 `comment` 已覆盖枚举中文 | 插件只多 UI 文案 / 多语言 / 故障码；故障码本次不做 | 低～中：按型号离线提取 + 人工筛选 | 覆盖按型号；文案不是协议枚举的 1:1 映射；专有内容不可分发。**枚举文案不值得做，故障码另议** |
| **不做** | 场景 / 自动化的创建与编辑 | 社区声称存在，**本项目未验证** | 看似能“替用户建自动化” | 高 | 改错没有回滚；社区实测新建需要模板、任意触发被 `-998` 拒；风险远大于收益 |
| **不做** | 用 MQTT 拿设备上下线 | **本项目实测拿不到** | — | — | 官方云 MQTT 的 `state/#` 窗口内 0 条；上下线仍只能读设备清单 |
| **不做** | 局域网 miIO 全面替代云端 | **本项目已明确排除** | — | 极高 | 覆盖率不成立，每类设备一条补丁；官方 HA 也建议不要用 LAN control |
| **不做** | 伪造设备事件（“模拟按下”） | **协议层不成立** | — | — | 云端没有 event/set，只有 event/get 读最近一次；虚拟事件是另一回事 |

### 0.2 一句话建议

先把 **事件轮询（P0）** 接进现有 `poll_events` 需求表，再把 **GetSceneList（P0）** 作为只读能力接上。统计接口已完成逐设备只读探针：**能源历史只对 4～6 台有计量配置的设备成立；温湿度历史只有独立温湿度传感器成立**，空调 / 净化器 / 开关的温度和湿度、电池电量、灯的电量都没有统计。插件包也已完成只读探针和 locale 对比：**spec 的 `comment` 已经够枚举中文，`locale=zh_CN` 只影响设备列表名；故障码之外，插件包不值得做**。**耗材清单已实测可用**（§3.4）：能自动发现耗材、`prop` 多数能对到 `siid.piid`、还自带阈值，当天已落地为**候选清单**（配置弹窗里给建议，不自动落卡）。场景 / 自动化写、本地 MQTT、LAN miIO 都不建议动。

---

## 1. 生态地图：没有统一上游，只有分层事实标准

### 1.1 对照表

星数 / 活跃度取自 2026-11-23 的 GitHub API 或仓库提交页；许可证取自仓库或 PyPI 元数据。

| 项目 | 覆盖层 | 语言 | 许可证 | 活跃度（证据） | 与本项目的关系 |
| --- | --- | --- | --- | --- | --- |
| **Do1e/mijia-api**（`mijiaAPI`） | 云端 HTTP（个人米家账号）、扫码登录、CLI、MCP | Python | **GPL-3.0** | 984 星 / 121 fork；创建 2022-08；最近 push 2026-10-10，仓库 2026-11-18 仍有更新；v5.0.0 把规格源迁到 miot-spec.org（[repo](https://github.com/Do1e/mijia-api)、[PyPI](https://pypi.org/project/mijiaAPI/)） | **当前依赖**；传输层（签名 / RC4 / token 刷新）直接复用 |
| **rytilahti/python-miio** | 局域网 miIO / 本地 MIoT、`miiocli`、token 获取（micloud） | Python | **GPL-3.0-only** | 2026-06-27 仍有提交；PyPI 稳定版 0.5.12（2022），0.6.0.dev0 预发布已带 `genericmiot`，可本地支持所有 MIoT 设备（[repo](https://github.com/rytilahti/python-miio)、[releases](https://github.com/rytilahti/python-miio/releases)、[PyPI](https://pypi.org/project/python-miio/)） | 本项目**明确排除** LAN 直连；但它证明“本地 MIoT 通用化”是可行的，只是覆盖与成本不划算 |
| **XiaoMi/ha_xiaomi_home** | 官方 HA 集成；云端 MQTT 推送 + 云端 HTTP 控制；本地中枢网关 MQTT | Python | **专有许可**（仅限非商业使用 HA；明确不授权开发 APP / Web 服务） | 19,225 星 / 964 fork；提交页显示 2026-09-28 仍有提交（[repo](https://github.com/XiaoMi/ha_xiaomi_home)、[LICENSE](https://github.com/XiaoMi/ha_xiaomi_home/blob/main/LICENSE.md)） | **不可复用代码**；它的推送架构是本项目 MQTT 调研的参照 |
| **al-one/hass-xiaomi-miot** | HA 集成；云端 + 本地，按 miot-spec 自动生成实体 | Python | **Apache-2.0** | 6,263 星 / 898 fork；2026-11-18 仍在提交（[repo](https://github.com/al-one/hass-xiaomi-miot)） | 自动化实体映射的参照；但它是 HA 专用，且 spec 自动生成 UI 正是本项目明确不做的路线 |
| **PiotrMachowski/Xiaomi-cloud-tokens-extractor** | 只做云端设备列表 / token / BLE key 提取 | Python | **MIT** | 5,005 星 / 464 fork；最近 push 2025-11-08（[repo](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor)） | 只解决 token 获取；本项目已有登录态，不需要 |
| **Yonsm/MiService**（`miservice`） | 云端 MiIO/MIoT + 小爱音箱 TTS | Python | **MIT** | 3.0.1，2026-07-03 发布（[PyPI](https://pypi.org/project/miservice/)、[repo](https://github.com/Yonsm/MiService)） | 可作为“轻量云客户端 + 音箱播报”的对照；本项目已用 `mijiaAPI` + 自己的音箱动作目录 |
| **maxinminax/node-mihome** | 局域网 miIO + Aqara + 云端 MIoT | Node.js | **MIT** | 最后发布 5 年前，0.0.32（[npm](https://www.npmjs.com/package/node-mihome)） | Node 生态已基本停更；OpenMexia 用的就是这一类 |
| **aholstenson/miio** | 局域网 miIO | Node.js | **MIT** | 最后提交 2018-05（[repo](https://github.com/aholstenson/miio/commits/master)） | 仅历史参考 |
| **@zythum02/mijia-api** | 云端 MIoT（Node） | TypeScript | **AGPL-3.0-or-later**（据本项目 OpenMexia 笔记） | 由 OpenMexia 使用，版本 v0.1.12 时期 | 许可证与本项目 GPL-3.0 组合会把整体变成 AGPL；**只取思路，不取代码** |
| **Go 生态** | 本地 miIO 为主 | Go | 各不相同 | `vkorn/go-miio`、`icepie/miio.go` 等均为小项目（星数十几 / 个位数），未发现被广泛采用的云端 Go 实现（[pkg.go.dev](https://pkg.go.dev/github.com/vkorn/go-miio)、[repo.uno 列表](https://repo.uno/l/go/t/xiaomi/1/)） | 不适用 |
| **小米官方企业接入** | 小米 IoT 控制端 API（`api.home.mi.com`） | HTTP | 官方，需企业 / 直连接入资质 | 面向“自己用小米模组接入”的厂商，不是个人米家账号（[官方文档](https://iot.mi.com/new/doc/accesses/direct-access/other-platform-access/control-api)） | 不适用 |

### 1.2 为什么说 `mijiaAPI` 不是“全生态标准”

它确实是**中文 Python 社区里个人米家云端封装的事实常用项**：有 CLI、MCP、扫码登录、批量属性读写、场景执行、耗材、统计、消息等较完整封装；本项目也验证了它的传输层足够稳。

但它不具备“标准”的三个特征：

1. **没有官方地位**：米家 App 的私有接口随时可能变；`mijiaAPI` v5 就因规格源迁移做过破坏性变更（`get_device_info` 参数从 model 改成 spec_type）。
2. **只覆盖云端一层**：没有局域网 miIO、没有云端 MQTT 推送、没有本地中枢网关 MQTT。这三块分别由 `python-miio` 和官方 HA 集成占据。
3. **采用量是“社区级”而非“平台级”**：984 星、121 fork，和官方 HA 集成的 19k 星不在一个量级。它的价值是“够用、活跃、GPL 兼容”，不是“所有项目都依赖它”。

### 1.3 对本项目的含义

- **不建议为了“统一上游”迁移**：本项目已经把 `mijiaAPI` 包成面向面板的 `Client`，并补了能力学习、子设备路由、批量读、静默期、locale 等五件事（`mihome/core/api.py` 开头注释）。换上游的收益很低，风险很高。
- **该做的是“在 Client 上加只读能力”**：事件、自动化列表、统计都可以继续走 `Client.raw()._request()`，不需要新依赖。
- **许可证要一直盯着**：`mijiaAPI` GPL-3.0 与本项目 GPL-3.0 兼容；官方 HA 集成专有许可，**一行代码都不能进发行包**；OpenMexia / `@zythum02/mijia-api` 是 AGPL，只能看思路。

---

## 2. 本项目的能力基线（已经用到什么）

| 能力 | 实际接口 | 代码位置 | 备注 |
| --- | --- | --- | --- |
| 家庭 / 房间 / 设备拓扑 | `POST /v2/homeroom/gethome_merged` + `POST /home/home_device_list` | `mihome/core/api.py:577-651` | 通过 `get_homes_list()` / `get_devices_list()`；共享家庭用 `shareflag` 标记 |
| 在线状态 | `POST /home/home_device_list` 的 `isOnline` | `mihome/core/api.py:654-693` | 每 15 秒重读一次，挂在轮询 tick 上 |
| 批量读属性 | `POST /miotspec/prop/get`，`datasource=1`，单片 300 条 | `mihome/core/api.py:696-846` | 实测 300 条约 261–357 ms；离线设备也会回缓存旧值 + `code=0` |
| 批量写属性 | `POST /miotspec/prop/set` | `mihome/core/api.py:849-907` | 写入成功后 3 秒静默期，避免滑块回弹 |
| 设备动作 | `POST /miotspec/action` | `mihome/core/api.py:910-940` | 参数键是 `in`，不是 `value`；虚拟事件也走这里 |
| 手动场景 | `GetSimpleSceneList` + `NewRunScene` | `mihome/core/api.py:942-956` | 首页胶囊与场景执行 |
| 极客版登录码 | `POST /home/rpc/<did>` + `miIO.get_autowebconfig_url` / `get_central_link_passcode` | `mihome/core/geek.py:297` | 已落地 |
| MIoT 规格 | `GET https://miot-spec.org/miot-spec-v2/instance?type=<spec_type>` | `mihome/core/spec.py:54`、`:765` | 保留 `unit` / `access` / `comment` / `events`，磁盘缓存 30 天 |
| 消息中心 | `POST /v2/message/v2/typelist` | `mihome/core/api.py:958-980` | 只读；无游标，不能翻历史 |
| 云 MQTT 推送 | 仓库外个人存档 | `docs/reference/mijia-mqtt-push.md` | **不进发行包**；复用 HA OAuth 身份，有风控风险 |
| 自动化列表 / 事件 | 只有探针 | `scripts/probe_automations.py:45-46` | 生产未接 |

**关键判断**：本项目的“云端读 / 写 / 动作 / 场景 / 规格 / 消息”已经覆盖得比较完整；真正的缺口只有两个方向——**瞬时事件**和**历史统计**。

---

## 3. 未使用能力逐项评估

### 3.1 P0：设备事件轮询 `/miotspec/event/get`

**状态：确认存在，且本项目已实测；生产未接。**

证据：
- 本项目实测结论：`docs/reference/mijia-automation-events.md` 第 3 节。请求体必须是 `{"params":[{"did","siid","eiid"}, ...]}`；一次请求可带 48 个事件源，约 150 ms；返回该事件**最近一次**的参数和 `updateTime`；`code=-9` 表示“从未发生”，不是错误。
- 代码已留位但无消费者：`mihome/ui/main_window.py:466-470` 的 `poll_events = PollDemandBoard("事件轮询", idle_ms=None)`；`mihome/core/poll_demand.py:13-14` 明确写了“属性轮询与事件轮询是两个接口、两套频率”。
- 消息模块也注明这条链路“目前还没有消费者”：`mihome/core/messages.py:11`。

**对本项目的价值**：
- 属性轮询只能看“现在是什么状态”，看不到“刚刚发生了一次单击 / 开锁 / 门铃 / 人体移动”。事件通道补的正是这块。
- 实测延迟：事件源批量读约 110–150 ms；1 秒轮询下，“米家 → 电脑”约 1.4 秒；属性轮询 3 秒一轮，最坏 3 秒才看到变化。
- 事件源之间**各自独立**：三键开关的左 / 中 / 右单击是不同 `eiid`，不会互相覆盖；这比虚拟事件的全局单槽可靠。

**实现代价（低～中）**：
1. 在 `Client` 加一个只读方法：`POST /miotspec/event/get`，返回 `(did, siid, eiid) → (value, update_time, code)`。
2. 事件源清单不要手写：从 `GetSceneList` 的触发器里取 `event.S.E`（现有探针已这么做），再补上规格里的 `events` 与中枢虚拟事件 `4.1`。
3. 去重靠 `updateTime`（整秒），同一 `(did,siid,eiid)` 只在时间戳变化时回调。
4. 接进 `poll_events` 需求表，**不要另起 timer**；1 秒是现有 `MIN_INTERVAL_MS` 下限，符合项目规则。
5. 测试可完全离线：假 API 返回 `code=0 / -9 / -704220043` 三种情况，断言去重与“从未发生”不被当失败。

**风险与边界**：
- 只保留“最近一次”，不能做计数、不能回看；两次轮询之间连按两次，只能看到后一次。
- 频控未知：1 秒一次是 3600 次/小时，量级与属性轮询同档，但米家对非官方客户端没有公开保证。建议只在用户显式启用相关功能时登记需求。
- 事件读取是**只读**，对共享家庭也安全；但如果事件触发后续动作（比如锁屏、播报），动作本身不能作用到共享家庭设备。

**结论**：这是当前**性价比最高**的缺口，建议优先做；它不能替代属性轮询，但能替代“为了等一个按键而高频轮询属性”的做法。

### 3.2 P0：自动化列表 `GetSceneList`

**状态：确认存在，本项目探针已通过；生产未接。**

证据：
- `docs/reference/mijia-automation-events.md` §2：`POST /appgateway/miot/appsceneservice/AppSceneService/GetSceneList`，必须带 `home_id`、`owner_uid`、`app_version:12`、`get_type:2`；一次读全家 38 条（11 条可手动执行 + 27 条自动化），83 KB。
- 生产只读手动场景：`mihome/core/api.py:942-956` 走 `get_scenes_list()`（上游实际是 `GetSimpleSceneList`）。
- 探针：`scripts/probe_automations.py:45`。

**对本项目的价值**：
- 让用户看到“家里已经有哪些自动化”，而不是只能看到手动场景。
- 发虚拟事件前，能核对“到底哪条自动化在监听这个事件名”，减少“发了没反应”的排查成本。
- 能读出触发条件 / 动作 / 启用状态，为将来的“电脑触发 → 米家动作”提供只读解释。

**实现代价（低）**：一个只读 RPC + 解析器。注意两个坑（文档已记）：
- `type` 字段不能当“手动 / 自动化”的判据；可靠判据是触发器里是否有 `key == "user.click"`。
- 同一个 `scene_id` 在两个接口里的 `name` 可能不同，别按名字对齐，按 `scene_id`。

**风险与边界**：
- 私有接口，`app_version` 可能变；失败要能降级为“读不到就不显示”。
- **只读**。不要用它做 `AppSceneService/Edit`；本项目文档已明确“不建议替用户改自动化，改错没有回滚手段”。

### 3.3 P1：统计接口 `/v2/user/statistics`

**状态：已实测（2026-11-23，只读，本机登录态；自有家庭 1 个，共享家庭 1 个整组跳过，未读其设备）。**

实测文件（探针脚本与原始输出未入库，仅本地留存）：统计接口探针、7 天全量矩阵、
定向粒度 / 翻页结果、汇总表。

#### 3.3.1 接口本身：确认可用

- 鉴权正常；`POST /app/v2/user/statistics` 返回 HTTP 200，业务码在 JSON 里：`code=0` 成功，`code=-8 invalid params` 表示 key / model 没配置或参数不对。
- 参数格式：`did`、`data_type`、`key`、`time_start`、`time_end`、`limit`。`home_id` 可选，带上也能正常返回。
- **只有纯 `siid.piid` 形式的 key 可用**；试过 `prop.2.1001`、`event.2.1001`、`prop.4.7` 全部 `-8`。旧版 `stat_day` / `stat_hour` / `stat_month` + `powerCost` 也全部 `-8`。
- `code=0` + 空 `result` 是“这个窗口没数据”，不是错误；必须再拉长窗口确认。
- 按 `did` 单设备查询，没有批量参数。55 台设备、32 台有候选属性，全量矩阵共 90 次请求。
- 代价：p50 49 ms，最大 126 ms；定向测试（含翻页 / 大 limit）p50 72 ms，最大 391 ms。

#### 3.3.2 当前账号下真的有数据的设备（7 天窗口，9 组）

| 设备 | 型号 | key | 属性 | 7 天结果 | 说明 |
| --- | --- | --- | --- | --- | --- |
| dev06 | `cuco.plug.v3` | 11.1 `power-consumption` | 耗电 | 7 个日点，`[69]/[86]/[84]` | 规格注释写 `0.01kWh`，即约 0.69/0.86/0.84 kWh/天 |
| dev06 | `cuco.plug.v3` | 11.2 `electric-power` | 功率 | 7 个小时点，`[0,44]` 这类 min/max | **`stat_day_v3` 下是小时粒度**，不是日 |
| dev20 | `uwize.plug.yzs01` | 3.1 `power-consumption` | 耗电 | 7 个日点，`[4704.39,4724.69]` | 数值明显不是 kWh，**单位/缩放不可信** |
| dev21 | `uwize.plug.yzs01` | 3.1 `power-consumption` | 耗电 | 7 个日点，`[46074.98,46216.09]` | 同上，且量级和 dev20 差 10 倍 |
| dev22 | `wiing.plug.v8icm` | 4.1 `power-consumption` | 耗电 | 7 个日点，`[52]/[72]/[72]` | 周 / 月也可用；数值语义同样需校准 |
| dev24 | `xiaomi.aircondition.m9` | 20.1 `power-consumption` | 耗电 | 仅 1 个日点，`[0.42]` | 只在 11-17 有记录，之后空 |
| dev25 | `xiaomi.aircondition.m9` | 20.1 `power-consumption` | 耗电 | 2 个日点，`[0.05]/[0.08]` | 11-18、11-20；明显稀疏 |
| dev41 | `linp.sensor_ht.ks2bb` | 2.1001 `temperature` | 温度 | 7 个日点，`[22,24.4]` | 日 / 小时 / 月都有；**唯一有温度历史的设备** |
| dev41 | `linp.sensor_ht.ks2bb` | 2.1002 `relative-humidity` | 湿度 | 7 个日点，`[64,73]` | 同上；**唯一有湿度历史的设备** |

另有一组“近期空、历史有”：

| 设备 | 型号 | key | 属性 | 结果 | 说明 |
| --- | --- | --- | --- | --- | --- |
| dev05 | `cuco.plug.cp5prd` | 10.1 `power-consumption` | 耗电 | 7 天 / 90 天为空，**3 年窗口有 300 点** | 2025-08-24 到 2026-06-19；设备之后不再上报 |
| dev05 | `cuco.plug.cp5prd` | 10.4 `electric-power` | 功率 | 7 天 / 90 天为空，3 年窗口 300 点 | 2026-06-09 到 2026-06-21，小时点 |

#### 3.3.3 明确没有数据的属性（`-8` 或空）

- **空调的温度 / 湿度 / 管温**：`xiaomi.aircondition.m4`、`m9` 的 `4.7 temperature`、`4.9 relative-humidity`、`10.9 tp-and-humidity` 全部 `-8`。
- **净化器温湿度**：`zhimi.airp.rma2` 的 `3.1 humidity`、`3.7 temperature` 全部 `-8`。
- **开关 / 插座的本机温度**：`linp.switch.e3sbw2/e3sbw3`、`xiaomi.switch.w1` 全部 `-8`。
- **电池电量**：门锁、人体、门窗、遥控器、体脂秤的 `battery-level` / `battery` 全部 `-8`。
- **灯的电量字符串**：`linp.light.lp2bc/lx2bcw` 的 `dynamic-energy` / `static-energy` 全部 `-8`。
- **电压 / 电流**：插座、空调室外机的 `voltage` / `electric-current` 全部 `-8`。
- **洗衣机 / 烤箱温度**：`target-temperature`、`water-temperature`、`dry-temperature` 全部 `-8`。
- **旧版 `powerCost`**：`-8`。

结论：**“温湿度历史”不是通用能力，只有独立温湿度传感器成立**；空调、净化器、开关都没有。不要把“设备有温度属性”误读成“云端有温度历史”。

另外，`-8` **不是鉴权拒绝**：同一账号、同一批请求里，能耗 / 温湿度 key 能成功返回，说明登录态和签名正常；`-8` 是业务层“这个 model + key 没配置统计，或参数不被接受”。`code=0` + 空结果是“配置可能存在，但该时间窗口没有上报”，两者要分开处理。

#### 3.3.4 时间粒度、回溯范围、单次上限

| `data_type` | 谁支持 | 实际行为 |
| --- | --- | --- |
| `stat_hour_v3` | 只有温湿度传感器 dev41 | 24 个小时点，min/max；插座 / 空调 `-8` |
| `stat_day_v3`（耗电） | dev06 / dev20 / dev21 / dev22 / dev24 / dev25 / dev05 历史 | 1 天 1 点；数值是当天总量（或 min/max，视 key） |
| `stat_day_v3`（功率） | 只有 dev06 `electric-power` | **1 小时 1 点**，`[min,max]` 瓦；名字叫 day，实际是小时表 |
| `stat_day_v3`（温湿度） | dev41 | 1 天 1 点，`[min,max]` |
| `stat_week_v3` | 不稳定：dev06 功率、dev22 耗电支持 | 8 个周点；dev20、dev24、dev41 都 `-8` |
| `stat_month_v3` | 所有测试过的有数据 key | 6 个点 = 最近 6 个月；温度是 min/max，耗电是月总量 |

- **单次最多 300 点**。`limit=500` 和 `limit=0` 都只返回 300 点；文档写“超出默认 300”，实测一致。
- **可以往前翻页**：把 `time_end` 设成上一页最早点之前，再取 300 点。传感器日粒度翻 3 页，拿到 2025-01-09 到 2026-11-23 的完整日历史；月度取 36 个月只返回 23 个点，最早 2025-01-01，说明 2024 年没有数据。
- 各设备起始时间不同，不是全局保留期：
  - dev06 耗电：2026-05-31 起，177 天；
  - dev20 耗电：2026-05-22 起，149 点（中间有缺天）；
  - dev22 耗电：2026-02-23 起，272 点；
  - dev24 空调：2025-08-29 起，300 点封顶（可能还有更早，需继续翻页）；
  - dev05：2025-08-24 到 2026-06-19，之后断。
- `stat_day_v3` 90 天窗口下 dev20 只有 73 个点，说明**不是每天都有上报**；不要补零或插值。

#### 3.3.5 数值语义与单位（最大的产品风险）

- 温湿度传感器：`[min,max]`，单位 °C / %，语义清楚。
- 耗电 `power-consumption`：单值总量，但**单位 / 缩放随型号变化**：
  - dev06：`[69]`，规格注释 `0.01kWh` → 约 0.69 kWh；
  - dev20：`[4704.39,4724.69]`，规格单位写 kWh，但数值显然不是 kWh；
  - dev21：`[46074.98,46216.09]`，和 dev20 同型号但量级差 10 倍；
  - dev22：`[52]/[72]/[72]`；
  - dev24 空调：`[0.42]/[1]/[2.15]`，看起来才像 kWh。
- 功率 `electric-power`：`[min,max]`，基本是瓦，但 dev05 历史值出现 `418.78` 这类，需按型号核对。
- 结论：**不能把 spec 的 `unit` 直接当展示单位**。必须用“同型号 + 米家 App 同窗口数值”做一次校准；否则会显示错 100 倍 / 10000 倍。

#### 3.3.6 对项目的建议

1. **做“能力表”，不做“全设备历史”**：按 `model + key` 记录是否有数据、粒度、单位缩放；只对有数据的设备画历史。
2. **首期可落地的范围**：dev06 插座的日耗电 / 小时功率、dev20/21/22 插座的日耗电、dev24/25 空调的日/月耗电、dev41 温湿度传感器的日/小时/月曲线。
3. **`ok_empty` 必须重试长窗口**：dev05 就是 90 天空、3 年有；不能一次空就判“没配置”。
4. **翻页**：每次 300 点，按 `time_end` 往前翻；日粒度 1 年约 2 页，历史成本可接受。
5. **不要轮询统计接口**：它是历史接口，不是实时接口；按需加载 + 缓存。
6. **共享家庭**：本次整组跳过，没有结论；即使只读，也可能因 owner 归属拿不到数据。若将来要读，仍只读，不写。

**结论**：统计接口**确认可用**，但覆盖是“按型号 + key 配置”的，不是通用历史。对本项目真正有价值的是 **4～6 台计量设备的能耗历史 + 1 台温湿度传感器的温湿度历史**；其余温度 / 湿度 / 电池 / 电压都没有。实现上最大的坑不是鉴权，而是**单位缩放校准**和**空窗口误判**。

### 3.4 P1：耗材清单 `/v2/home/standard_consumable_items`

**状态：2026-11-24 只用只读探针实测通过，当天按「候选清单」落地**——配置弹窗在空卡那一步
列出云端认为的耗材，点一下把属性 / 名称 / 阈值填好，仍由用户按保存才落卡（见
`docs/dev/ui.md`「第二步的『云端耗材』建议」）。

上一版这一节只读了上游源码就下"建议不做"的结论，那次是错的判断依据——实测后发现三条当时不知道的事实（见下"实测"第 3、4、5 条），所以结论从"建议不做"改成"能力已确认，形态待定"。

接口：`POST /v2/home/standard_consumable_items`，body `{home_id: int, owner_id: int, filter_ignore: bool}`。
`owner_id` 就是家庭列表里的 `uid`（共享家庭也是，直接取 `home["uid"]`，别用账号自己的 uid）。
探针：`scripts/probe_consumables.py`（只读，落 `scripts/out/consumable_items.json`）。

**实测（2026-11-24，2 个家庭 / 64 台设备）**

1. 返回 `{"items": [...]}`。`items` **按 `state` 分组**（不是按设备、也不是按房间），每组
   `{state, count, ignore_count, consumes_data: [...]}`。本机三组：`state=1` 12 条（有读数）、
   `state=2` 1 条、`state=3` 1 条（后两组 `value` 都是空串，语义云端没有文档，**别当结论**）。
   → 上游 `apis.py:385` 只取 `items[0]`，**会漏掉另外两组**；正确做法是遍历所有 `items`。
2. 条目 `consumes_data[i]`：`did / name / model / sub_class / room_id / is_online / is_ignore /
   skip_rpc / ble_gateway / start_time / time_stamp / details`。
3. `details` **是列表**（一台设备一块电池时 1 条，门锁双电池时 2 条），每项：
   `id / description / value / update_time / state / inadeq / exhaust / prop / consumable_type /
   total_life / intro / pic_urls / reset_method / change_instruction / reset_state /
   type_name / extra_url / left_time / wechat_path`。
   - `value` 是**百分比字符串**（`"17"` / `"100"`），不是数值。
   - `inadeq` / `exhaust` 是**JSON 字符串**（`{"val":"20","unit":"percentage","type":"range"}`），
     即云端自带的"不足 / 耗尽"阈值——正好能当 `warn` / `crit` 的默认值。
   - `description` 比规格属性名细得多：门锁同一块 `battery-level` 分别写「5号电池」「锂电池」
     「小电池」「大电池」，洗衣机写「洗衣液」（规格里是 `detergent-left-level`）。
4. **`prop` 能对到设备规格的 `siid.piid`**：`prop.6.1` / `prop.2.1003` → 直接拆。
   本机 17 条详情里 **15 条命中**，属性名全部是 `battery-level` / `filter-life-level` /
   `detergent-left-level`，与 `description` 语义一致 → **可以直接喂给现有耗材页的
   `did:siid.piid` 模型**。两种对不上的写法都在老门锁 `loock.lock.t1pro`：`prop.electricity`
   （按名字）与 `prop.4106`（疑似 siid+piid 拼接，规格里 siid 4 只有 `4.1`）。
5. **覆盖 14 台 / 64 台**，剩下 50 台涉及的 38 个型号是灯、插座、摄像头、浴霸这类
   **本来就没有耗材**的设备——所以"覆盖面小"不是缺陷；真正的耗材设备（净化器滤芯、
   各类电池、门锁双电池、洗衣机洗衣液、空调滤网）基本都在里面。
6. `filter_ignore` 实测**无差异**（`True` / `False` 都是 12 + 2 条），因为本账号
   `ignore_count` 全为 0，**没有忽略项样本**，参数语义仍未验证。

**判断**：这是"云端耗材台账"，和耗材页现在做的事（用户手挑 spec 属性 + 自己填阈值）
不是替代关系，而是能补上后者最费劲的三步：**找哪些属性算耗材、给双电池分名字、给阈值**。
落地形态是**当候选清单**——空卡弹窗里多一列「云端认为的耗材」，点一下自动填好
`did:siid.piid` + 名称 + 阈值，仍然由用户按下保存才落卡（不推翻"不做全屋自动扫描"
的决策）；**没有**整页改成直读云端台账——那样会丢掉用户自定义阈值、排序、三种卡片
样式，而且 `value` 只有百分比、没有时间序列。

**已知限制**：`prop` 的两种非标准写法没解；`state` 语义未知；`filter_ignore` 未验证；
条目没有 `room_name`（只有 `room_id`，要自己映射）。

### 3.5 P2：房间 / 设备管理写接口

**状态：社区声称存在且同类项目已实现；本项目未用。**

证据：
- `docs/reference/mihome-pc-unofficial.md` §4.4：房间新建 / 改名 / 删除 / 设备搬家“写接口已通”；但场景 / 定时新建需要模板，自动化任意触发被 `-998` 拒，房间排序服务端不支持。
- `TODO.md:29-34` 已把“云端管理：房间 CRUD / 设备搬家”列为待排期。

**价值**：补全“管理”能力；但本项目定位是“桌面控制台”，不是“米家 App 移植”，优先级低。

**风险与边界**：
- 这是**写账号配置**，不是写设备属性；影响面比开关灯大。
- **只能用于用户自己创建的家庭**；共享家庭一律禁止任何状态改变。
- 需要二次确认 + 失败回滚提示；没有回滚手段的操作（删除房间）建议不做。

### 3.6 P2：中枢网关本地 MQTT

**状态：官方 HA 已实现；本项目未验证；不建议现在做。**

证据：
- 官方 HA README：本地模式依赖“小米中枢网关”（固件 3.3.0_0023 及以上）或内置中枢功能的其他设备（软件 0.8.9 及以上）；中枢网关内是标准 MQTT Broker；BLE / ZigBee 子设备也可经中枢转发；**中枢网关只在中国大陆可用**；官方也提供“Xiaomi LAN control”，但明确“可能造成异常，建议不要用”。
- 官方集成的本地代码在 `miot/miot_lan`、`miot/miot_mdns`，但整个仓库是**专有许可**，不能复用。
- 本项目已有云 MQTT 个人存档（`docs/reference/mijia-mqtt-push.md`），但那条走的是云端 `cn-ha.mqtt.io.mi.com`，不是本地中枢。

**判断**：技术上确实存在一条“局域网内低延迟 + 覆盖 BLE/ZigBee”的路，但：
1. 需要中枢网关（用户家里有 `xiaomi.gateway.hub1`，具备前提）；
2. 官方没有公开本地 MQTT 的鉴权 / 主题 / 报文契约，只能逆向；
3. 官方实现是专有许可，不能抄；
4. 本项目已有云端事件轮询可把延迟压到约 1.4 秒，收益没有大到值得承担逆向 + 许可风险。

**结论**：记录为“确认存在、暂不做”。除非将来“亚秒级 + 断外网可用”成为硬需求，再单独立项。

### 3.7 已确认不成立 / 不可做的

| 能力 | 结论 | 证据 |
| --- | --- | --- |
| 伪造设备事件（“模拟按下”） | **协议层不成立** | 本项目实测：云端只有 `/miotspec/event/get` 读最近一次，没有 event/set；虚拟事件是中枢网关的 `siid 4 / aiid 1`，不是设备按键事件。见 `docs/reference/mijia-automation-events.md` §11 |
| 场景 / 自动化的创建与编辑 | **高风险，不建议** | `AppSceneService/Edit` 契约未验证；同类项目实测新建需要同类模板（`sc_id` 校验），任意触发被 `-998` 拒；改错没有回滚。见 `docs/reference/mihome-pc-unofficial.md` §4.4、`docs/reference/mijia-automation-events.md` §2.3 |
| 用 MQTT 拿设备上下线 | **拿不到** | 本项目实测：订阅 `device/{did}/state/#` 被放行，但 10 分钟窗口（含拔电重上电）0 条消息。见 `docs/reference/mijia-mqtt-push.md` §7.1 |
| 局域网 miIO 全面替代云端 | **明确排除** | 覆盖率不成立：Yeelight 传统协议、BLE 换端点、ZigBee 只能云端、跨网段超时；OpenMexia 的实现全是补丁。见 `docs/reference/openmexia-notes.md` §4、`docs/reference/README.md` “明确排除” |
| 用 MIoT spec 自动生成 UI 替代逐设备适配 | **只能替代“控件形状”，替代不了“语义”** | 本项目明确选择“按品类手写卡片 + 型号适配”，因为 spec 驱动的通用面板会把 70–117 个属性全铺出来，且动作语义（toggle / start / pause）仍要按型号解释。见 `mihome/core/catalog.py:1-18` |
| 事件通道做计数 / 历史 | **做不到** | 只保留最近一次；要历史只能自己攒。见 `docs/reference/mijia-automation-events.md` §3.3 |

### 3.8 看似是缺口、实际不是

| 项 | 为什么不是缺口 |
| --- | --- |
| 批量读写 | 本项目已经做：一次 `prop/get` 最多 300 条，`prop/set` 也支持批量；实测 300 条比 120 条更快。见 `mihome/core/api.py:81-86`、`:849-907` |
| 共享设备列表 | `get_homes_list()` 已经带 `fetch_share` / `fetch_share_dev`，共享家庭用 `shareflag` 标记；不需要再单独调 `get_shared_devices_list()` |
| `check_new_msg` 未读数 | 本项目已定“不做未读”（不标已读，红点会永远亮）；消息中心已用 `typelist` 只读展示。见 `mihome/core/messages.py:38-39` |
| MCP / CLI | 是 `mijiaAPI` 的开发者便利功能，不是面板产品能力；本项目用自己的 UI |
| `datasource=2`（`ds=2`） | 本项目已实测：`ds=1` 对离线设备也回缓存旧值 + `code=0`；`ds=2` 才会回 `-704042011`，但 150 条批量要 1.05 秒（约 9 倍），不能挂 3 秒轮询。在线判定继续用设备清单的 `isOnline` 更划算。见 `mihome/core/api.py:653-693` |
| `get_device_info()` 上游封装 | 本项目故意不用：它丢掉了 `unit` / `format` / `access` / `comment` / `events`；`core/spec.py` 直接吃 miot-spec.org 原始 JSON。见 `mihome/core/spec.py:1-12` |

### 3.9 P2：云端插件包 `/v2/plugin/fetch_plugin`（本次只读实测）

**状态：接口可用；能取到部分型号的 UI 文案与故障码表；不建议作为项目文案的运行时来源。**

#### 3.9.1 端点、请求方式与鉴权

- 端点：`POST https://api.mijia.tech/app/v2/plugin/fetch_plugin`
- 可用请求体（最小形态）：

```json
{"latest_req":{"api_version":10070,"plugins":[{"model":"cuco.plug.v3"}]}}
```

- 复用 `mijiaAPI` 的现有登录态与 RC4 签名（`gen_nonce` / `get_signed_nonce` / `generate_enc_params`），请求头沿用 `mijiaAPI` 默认（含 `auth.json` 里的 `ua`、`miot-encrypt-algorithm: ENCRYPT-RC4`）。上游没有封装这个接口，探针是复用其 `miutils` 自行签名。
- **不需要额外签名、插件版本号或特殊 UA**。`api_version` 用 10070 即可；另外 4 种变体（`version: 0`、扁平 body、`plugin_id`、`min_version/max_version`）返回与主变体一致或为空，均非必需。
- 元数据请求实测 **46–82 ms、152–696 B**，HTTP 200、`code=0`。
- `download_url` 指向 `iot-cdn.io.mi.com`；**下载包本身不再需要登录态**（裸 `urllib.request` + 普通 UA、未带 Cookie 即可），鉴权只发生在元数据请求。
- 响应关键字段：`plugin_id`、`plugin_package_id`、`version`、`sdk_version`、`change_log`、`download_url`、`size`、`md5sum`、`platform`、`status`、`gray_info`。`latest_info` 与 `full_define_plugin_info` 会返回同一个 URL（本次已按 URL 去重，避免重复下载）。

#### 3.9.2 包里有什么

三个型号的实测（型号保留，其余标识不落盘）：

| 型号 | 包大小 | ZIP 条目 | `main.bundle` | 下载耗时 | 关键内容 |
| --- | --- | --- | --- | --- | --- |
| `cuco.plug.v3` | 1,169,436 B（1.12 MiB） | 347 | 2,223,147 B | 696–752 ms | RN bundle + 图片 + `project.json` + 4 份协议 HTML；无故障表 |
| `zhimi.airp.rma2` | 3,078,547 B（2.94 MiB） | 415 | 5,150,844 B | 874–1,013 ms | bundle 内嵌多语言 i18n 扁平表；有故障枚举 |
| `xiaomi.aircondition.m9` | 6,161,617 B（5.88 MiB） | 335 | 3,167,694 B | 1,361–1,788 ms | 22 份 zh/en i18n JSON + 独立 `errorcode.json` / `errorcodeen.json` |

通用结构：

```text
android/main.bundle          React Native 打包产物（indexed-ram-bundle）
android/project.json         包元信息：package_path、min_sdk_api_level、sdk_api_level、version、bundle_type...
android/bundle.cert          签名证书
android/bundle.sign          签名
android/drawable-*/...       图片资源
android/raw/...              可选：i18n JSON、errorcode JSON、协议 HTML
```

`project.json` 样例（`cuco.plug.v3`）：

```json
{"package_path":"com.v3","min_sdk_api_level":10066,"sdk_api_level":10076,"version":"1.0.76","bundle_type":"indexed-ram-bundle","bundle_minify":true}
```

文案是**中文明文**，但多数不在独立的 `value-list` 字段里：JS bundle 用 `\uXXXX` 转义，需先解码；解码后可直接按中文检索。包内是否采用独立 JSON，取决于型号的打包方式。

#### 3.9.3 故障码表：AC m9 有独立表，净化器/插座没有

`xiaomi.aircondition.m9` 的 `android/raw/projects_comxiaomibaseplugin_src_assets_raw_errorcode.json`：

- 9,684 B，**71 条**；英文版 `errorcodeen.json` 12,584 B，同样 71 条。
- 结构：对象，key 是负数内部码；value 是 `{text, code, desc?}`。
- 71 条全部有 `text`，38 条带 `desc`（排查建议）。
- `code` 前缀分布：`F` 13、`U` 22、`E` 16、`P` 9、`R` 5，另有 `FB` / `FC` / `PA` / `PB` / `PC` / `Eb` 各 1。
- 中文样例：

```json
"-500": {"text":"室内环境温度传感器故障","code":"F1.1"}
"-550": {"text":"室外排气温度过高保护","code":"P1","desc":"请排除室外机进出风口是否被异物挡住，并断电后重新上电"}
```

- 同时有 22 份 zh/en 对照 i18n JSON（25 B–29,206 B）。例如 `control_modezh.json` 的 `制冷/制热/风扇/除湿/睡眠/节能/AI节能/辅热`，`control_optionzh.json` 的 `左右扫风/上下扫风`。这些是 UI 级文案，不只是 spec 的开发者注释。

`zhimi.airp.rma2`：

- 没有独立 errorcode JSON；故障枚举在 `main.bundle` 的扁平 i18n 表里，key 形如 `'service:002:property:002:valuelist:001'`，value 按中/英/德/西/日等语言分块。
- 故障属性（`property:002`）每个语言块 3–4 条：`无错误` / `颗粒物传感器异常` / `温度传感器异常` / `湿度传感器异常`；另一份规格写作 `温湿度传感器异常` / `未安装滤芯`。
- 模式属性（`property:004`）同样是枚举文案：`自动/睡眠/最爱/挡位`。
- 全 bundle 共 746 个 `valuelist:` 键出现（含多语言分块与重复），只能按块解析，不能当成 746 个唯一枚举。

`cuco.plug.v3`：

- 没有 `value-list`、`i18n`、`故障` 文案；只有通用设置页文案（579 个中文串）和协议 HTML。这类“无枚举、无故障码”的设备，插件包对文案补全没有价值。

**方法学坑**：直接检索 `fault` 会命中 `default`。`cuco.plug.v3` 的 6,952 次、`zhimi.airp.rma2` 的 9,484 次命中几乎全是 `default`；必须用引号键（`"fault"`）、词边界或中文关键词。净化器真正可用的入口是 `valuelist:` 键。

#### 3.9.4 覆盖：按型号有/无，不是“App 打开过才有”

- 取包只依赖 `model`，请求体没有 `did`；不需要设备在账号里。
- 16 个型号实测：**11 个有包，5 个空**。
  - 有包：`cuco.plug.v3`、`xiaomi.aircondition.m9`、`zhimi.airp.rma2`、`lumi.remote.mcn001`、`zhimi.airp.mb5`、`dmaker.fan.p15`、`lumi.gateway.mgl03`、`yeelink.light.color8`、`mijia.camera.v3`、`zhimi.heater.za2`、`xiaomi.router.r4`。
  - 空：`linp.sensor_ht.ks2bb`、`yunmai.scales.ms103`、`xiaomi.aircondition.m8`、`roborock.vacuum.a70`、`xiaomi.washer.v33`。
- `zhimi.airp.mb5`、`dmaker.fan.p15`、`lumi.gateway.mgl03`、`yeelink.light.color8`、`mijia.camera.v3`、`zhimi.heater.za2`、`xiaomi.router.r4` **不在本账号设备清单里**，仍返回了包 → “只有 App 里打开过才有”不成立；覆盖取决于该型号是否配置了云端插件。
- 空结果不是鉴权失败：HTTP 200、`code=0`、`latest_info: []`、其余字段 `null` / `[]`。同品类也可能一个有（`m9`）一个没有（`m8`），不能按品类推断。

#### 3.9.5 卡点与替代途径

本次没有“取不到”的卡点：端点和鉴权都已通。真正的限制是**覆盖与内容**：

- 有些型号根本没有云端插件包（空结果），不是签名 / UA 问题。
- 有包也不一定有故障表：AC m9 有独立表；净化器只有 `value-list` 故障枚举；cuco 完全没有。
- 包是小米签名 / 专有内容，**不应整份分发或提交**；本项目最多在本地解析、缓存文案，不把包或大段原文放进发行物。
- 端点、`api_version`、包结构都是私有契约，随 App 版本变化；必须失败降级。

**别的只读途径**（能补一部分，但补不了故障码）：

1. **MIoT spec**：本项目已经在用，`value-list[].comment` 有中文，但它是开发者注释，存在英文、整句、编号、条件告警；`mihome/core/enum_text.py:4-15` 已记录 49 份规格 / 1850 个枚举项的实测。适合做“原文兜底”，不适合直接上屏。
2. **手写三层表**：现状（型号适配层 → 品类表 → 全局表），可控、可审、无许可风险；缺点是故障码没有表、新型号要人工补。
3. **插件包**：能拿到 UI 级枚举文案和部分型号的故障码表，适合做“离线提取 → 人工筛选 → 落成本地词表”，不适合运行时依赖。
4. **米家 App 资源 / 抓包**：不可分发、不可审计，不建议。

**结论**：插件包是**真实可用的一手文案来源**，但它是“按型号、按版本、UI 导向”的，不是通用字典。补完 3.10 的 locale 对比后，**故障码之外这条不值得做**：spec 的 `comment` 已经覆盖枚举中文，现有三层表负责短名与纠错；插件包的增量主要是 UI 文案 / 多语言 / 故障码。可接受的用法只有一个：某个具体型号的 spec 注释缺失 / 错误时，做一次该型号的离线提取，人工审核后落进现有三层表。运行时不要直接依赖该接口。

#### 3.9.6 证据

探针脚本与原始输出未入库，仅本地留存（插件探针、发现结果、插件包与汇总、内容统计）。

### 3.10 `zh` / locale 与插件包文案的对比（本次只读）

**结论先说：`zh` 不是 miot-spec 的参数，而是米家云端会话 Cookie 里的 `locale=zh_CN`；它只影响设备列表里的展示名，不会把 spec 的 `description` / `value-list` 变成中文。spec 的中文来自厂商预先写好的 `comment`，与 `locale` 无关。故障码之外，插件包在枚举文案上不值得做。**

#### 3.10.1 `zh` 到底作用在哪个请求上

仓库里只有一处显式设置语言：`mihome/core/api.py:89-124` 的 `API_LOCALE = "zh_CN"` 与 `apply_locale()`。它不是给某个请求加 query / body 参数，而是改 `mijiaAPI` 会话的 Cookie：

- 上游 `mijiaAPI/apis.py:50-70` 在 `_init_session()` 里把 `locale={self.locale}`、`countryCode=...` 拼进 `Cookie`；
- 本项目在 `Client` 构造时调用 `apply_locale`（`mihome/core/api.py:483-485`），把上游按系统猜出来的 `Chinese (Simplified)_China` 改成合法串 `zh_CN`；
- 受影响的是 `api.mijia.tech/app` 下的所有云端 RPC，但代码和实测只确认了**设备列表里的展示名**会受影响：`load_topology()` 调 `get_homes_list()` / `get_devices_list()`（`mihome/core/api.py:577-584`；上游 `/v2/homeroom/gethome_merged`、设备列表见 `mijiaAPI/apis.py:404-458`）。
- 属性读取 `/miotspec/prop/get` 只返回数值 / 状态码，没有可翻译文本（`mijiaAPI/apis.py:632`），`locale` 对它没有可见影响。
- **miot-spec.org 的请求没有带 locale**：`mihome/core/spec.py:54` 只有 `?type={urn}`。

仓库内可确认这件事的位置：

| 位置 | 说明 |
| --- | --- |
| `mihome/core/api.py:21-23`、`:89-124` | 为什么要钉死 `zh_CN`，以及它改的是 Cookie |
| `mihome/core/api.py:483-485` | `Client` 构造时实际调用 `apply_locale` |
| `mihome/core/api.py:577-584` | `load_topology()` 用家庭 / 设备列表拿展示名 |
| `scripts/verify_locale.py:1-13`、`:129-144` | 现场复现“英文默认名”和 `--live` 中文占比检查 |
| `docs/dev/architecture.md:150-152` | 同一结论的文档记录 |
| `mihome/core/spec.py:54` | spec 端点只有 `type`，没有 locale |

现场只读复测（对比输出未入库，仅本地留存）：对 `zhimi-rma2` 的 spec 端点分别加 `locale=zh`、`locale=zh_CN`、`locale=zh-CN`、`lang=zh`、`locale=cn`、`Accept-Language: zh-CN`，**7 种变体全部 200、8,954 B、内容逐字节相同**，`description` 仍是 `Air Purifier`。`/miot-spec-v2/instances?status=all` 加 `locale=zh` / `zh_CN` 也是同样的 6,947,356 B，没有变化。**miot-spec.org 不认 locale。**

#### 3.10.2 它返回的中文覆盖到什么粒度

`locale=zh_CN` 的可见效果是**设备列表的展示名**（尤其是用户没改过名时的厂商默认名）；家庭名、房间名是用户自定义文本，不靠 locale 翻译。它**不改变** spec 里的任何字段。

spec 自己的字段分两层，和 locale 无关：

| 字段 | 语言 | 例子（`zhimi.airp.rma2` / `xiaomi.aircondition.m9`） |
| --- | --- | --- |
| `description` | 英文 | `Air Purifier`、`Device Fault`、`Mode`、`Cool` |
| `comment` | 厂商写什么就是什么 | `空气净化器`、`故障`、`模式`、`制冷` |
| `value-list[].description` | 英文 | `No Faults`、`Motor Stop`、`Auto`、`Favorite` |
| `value-list[].comment` | 厂商写什么就是什么 | `无错误`、`马达故障`、`自动`、`最爱` |
| 项目里的 `name` | URN 英文标识 | `mode`、`fault-value`、`on` |

所以“改成 zh 后全是汉字”并不成立。实际有三类缺口：

1. **`comment` 是英文**：AC m9 的 `Favorite Type`，`description` 和 `comment` 都是 `Cloud Recommendation` / `Custom`；项目的手写表把它映射成“云端推荐 / 自定义”（`mihome/core/enum_text.py:122`）。
2. **`comment` 缺失**：AC m9 的 `Cooling Effect` / `Heating Effect` 五个档位 `comment` 全是 `null`，只剩英文 `description`。
3. **`comment` 与值对不上**：某摄像机 `recording-mode` 的 `value-list` 是 `All Record` → “一直录制”、`Motion Record` → “停止录制”、`No Record` → “移动录制”，注释明显错位。这类文本直接上屏会比英文更糟。

这正是 `mihome/core/enum_text.py:4-15` 记录的那 49 份规格 / 1850 个枚举项的实测背景：spec 的 `comment` 是**开发者注释**，不是 UI 文案。

#### 3.10.3 与插件包逐项对比

同一台净化器 `zhimi.airp.rma2`、同一个故障属性：

| 来源 | 值 | 文案 |
| --- | --- | --- |
| spec `Device Fault` | 0 / 2 / 3 | 无错误 / 马达故障 / 颗粒物传感器异常 |
| 插件 `service:002:property:002:valuelist`（中文块 A） | 000 / 001 / 002 / 003 / 004 | 无错误 / 颗粒物传感器异常 / 温度传感器异常 / 湿度传感器异常 / 未安装滤芯 |
| 插件（中文块 B） | 000 / 001 / 002 | 无错误 / 马达故障 / 颗粒物传感器异常 |

- 两边**不完全一致**：插件把温度 / 湿度传感器拆成独立项，还多了“未安装滤芯”；另一个中文块又回到 spec 的马达故障，且值编号不同。插件包里还有英 / 德 / 西 / 法 / 意 / 韩 / 波 / 俄 / 泰 / 土 / 越 / 繁体等十余个语言块。
- 同一台净化器的 `Mode`：spec 是 0/1/2 = 自动 / 睡眠 / 最爱；插件块多出 003 = 挡位。插件把 UI 上的“挡位”也放进同一个选择器，**不是协议枚举的 1:1 映射**。
- AC m9 更明显：spec 的 `mode` 只有 4 个值（制冷 / 除湿 / 风扇 / 制热）；插件的 `control_modezh.json` 有 23 个键，除了模式还有睡眠 / 节能 / AI节能 / 辅热 / 干燥 / 定时 / 灯光 / 防直吹——这些在 spec 里是**别的属性或动作**，不是 `mode` 的枚举项。

插件包多出来的东西可以归成四类：

1. **UI 语义与分组标题**：AC 的 `control_modezh` / `control_optionzh` 是界面按钮和选项，不是协议枚举；
2. **设备专属叫法**：例如净化器把“温度 / 湿度传感器异常”拆开、AC 用“AI节能 / 防直吹”；
3. **多语言**：十余个语言块，spec 只有英 `description` + 厂商 `comment` 两层；
4. **故障码文本**：AC m9 的 71 条 `errorcode.json` 把内部负数码映射成 `F1.1` / `P1` 等显示码和中文说明；spec 的 `fault-value` 只给码（`F2.4`、`P1`），没有文字。这一项按本次要求不纳入结论。

反过来，spec 有而插件没有的：`format`、`access`、`value-range`、`unit`、服务 / 属性 / 方法的完整层级和 URN。插件包不是规格的替代品。

#### 3.10.4 三层手写表还需要吗

需要，但它的角色已经不是“唯一的中文来源”，而是**显示层归一化**：

- `spec.py` 本来就把 `comment` 优先当原文：`ValueOption.label` 是云端原话，`PropertySpec.label = comment or description or name`（`mihome/core/spec.py:178-192`、`:224-231`）；
- 手写表把原话映射成**短名**：`ValueOption.short` / `display_short`，原文不丢，tooltip 仍用 `display`；
- 三层优先级是“型号 > 品类 > 全局”，键用 URN 属性名而不是中文注释，避免云端改一个字就静默失效（`mihome/core/enum_text.py:17-43`）；
- 它解决的是 spec 解决不了的三件事：**宽度**（详情页下拉最宽 240px）、**一致性**（同一词跨品类统一）、**错误 / 缺失注释**（英文、null、错位）。
- 反过来，如果某个型号的 spec 注释又短又准，三层表可以完全不写，`display_short` 会退化成原文——这就是现在“加映射只会更好，不会更坏”的设计。

所以 spec 中文、插件包、手写表不是三选一，而是三层：

```text
spec comment / description  →  原始中文 / 英文（兜底）
插件包 i18n / value-list    →  可选的 UI 文案与多语言（按型号）
三层手写表                  →  最终上屏的短名与纠错（型号 > 品类 > 全局）
```

#### 3.10.5 结论：故障码之外，插件包值不值得做

**不值得做。**

- 如果目标只是“枚举项显示中文”，**spec 的 `comment` 已经够了**，而且项目已经在用；`locale=zh_CN` 只是让设备列表名变中文，不是 spec 文案的来源。
- spec 的中文不够“好上屏”时，现有三层手写表已经承担了短名和纠错；插件包并不能替代这一层。
- 插件包的枚举文案不是一份干净的字典：AC 是 UI 键，不是协议枚举；净化器同一属性有多个中文块且条目 / 编号不一致；覆盖还只有 11/16 个抽测型号。
- 它真正独有且 spec 没有的，是**故障码说明**和**多语言 UI 文案**；前者本次明确不做，后者只有在产品确定要做多语言时才成立，且要先解决专有内容的许可问题。
- 可接受的用法只有一个：某个具体型号的 spec 注释缺失 / 错误，且用户确实看到坏文案时，做一次**该型号的离线提取**，人工审核后落进现有三层表；不做通用流水线，不在运行时依赖插件接口。

#### 3.10.6 证据

- 语言现场对比与 spec / 插件逐项对比：原始输出未入库，仅本地留存。
- 代码位置：`mihome/core/api.py:89-124`、`:483-485`、`:577-584`；`mihome/core/spec.py:54`、`:178-192`、`:224-231`；`mihome/core/enum_text.py:4-15`、`:17-43`；`scripts/verify_locale.py:1-13`、`:129-144`
- 上游 Cookie 构造：`mijiaAPI/apis.py:50-70`；设备 / 家庭列表：`mijiaAPI/apis.py:404-458`

---

## 4. 三个关键问题的深入回答

### 4.1 `mijiaAPI` 的地位：够用，但不是“全生态标准”

- **它是“云端 HTTP 层”的常用选择**，不是“米家接口标准”。米家没有面向个人开发者的公开 API；所有第三方库都是逆向。
- **它的优势**：扫码登录、token 自动刷新、RC4 签名 / 解密、批量读写、场景、耗材、统计、消息、CLI / MCP 都有；GPL-3.0 与本项目兼容；维护活跃。
- **它的边界**：没有推送、没有局域网、没有本地中枢；私有接口变动时只能跟着改。
- **替代品各自只覆盖一层**：`python-miio` 强在本地，官方 HA 强在推送 + 本地中枢但许可封闭，`hass-xiaomi-miot` 强在 HA 自动映射但只服务 HA，token extractor 只做 token。
- **结论**：本项目继续用 `mijiaAPI`，在 `Client` 上补事件 / 自动化 / 统计，是成本最低、风险最小的路线。

### 4.2 事件订阅能不能替代轮询？

**不能整体替代，只能补“事件”这一块。**

| 方案 | 能拿到什么 | 延迟 | 覆盖 | 许可 / 风险 |
| --- | --- | --- | --- | --- |
| 属性轮询 `prop/get` | 当前状态（开关、亮度、温度…） | 3 秒一轮（前台） | 全部可读属性 | 已用；无额外风险 |
| 事件轮询 `event/get` | 每台设备每个事件的**最近一次**（单击、开锁、门铃、人体、虚拟事件） | 轮询间隔 + 0.4 秒 | 有事件源的设备；BLE 也能读 | 已实测；只读；频控未知 |
| 云端 MQTT（官方 HA） | `properties_changed` + `event_occured` 推送 | < 1 秒 | 几乎全部设备 | 需要 HA OAuth 身份；**专有许可 + 风控风险**；QoS0、无 retain、拿不到上下线 |
| 本地中枢 MQTT（官方 HA） | 本地属性变化 + 事件 | 局域网级 | 中枢下挂设备（含 BLE / ZigBee） | 需要中枢；协议未公开；实现专有许可 |

**关键区别**：
- **HTTP 侧没有 `property_changed` 订阅接口**。属性变化只能靠轮询，或者 MQTT。这是“事件轮询不能替代属性轮询”的根本原因。
- `event/get` 只适合“触发型”用途（门铃响了、锁开了、按键按了），不适合“状态型”（灯现在亮不亮）或“计数型”（今天开了几次门）。
- 如果将来要做“米家 → 电脑”的低延迟触发，**事件轮询是当前唯一不需要额外许可、且本项目已验证的路线**；MQTT 只能停留在个人自用存档。

### 4.3 `ds=1` / `ds=2` 与在线判定

本项目已实测（`mihome/core/api.py:653-693`）：

- `datasource=1`（默认，轮询用的）：对**离线设备也返回云端缓存的旧值**，`code=0`。所以“读成功”不能证明在线。
- `datasource=2`（强制找设备）：离线设备会回 `-704042011`，但 150 条批量要 1.05 秒，约是 `datasource=1` 的 9 倍；值一模一样。
- 因此在线判定**不要**用属性读的成功 / 失败，而是每 15 秒重读一次设备清单的 `isOnline`。
- 同类项目 OpenMexia 还试过“读失败计数 → 判离线”，同样踩了“缓存旧值”的坑；本项目选择的“重读设备清单”更直接。

**结论**：`ds=1/2` 的差别已经查清，不需要改。若将来要做“离线诊断”，可以把 `ds=2` 作为一次性探针，而不是轮询。

### 4.4 MIoT spec 通用调用能绕过多少逐设备适配？

**能绕过“寻址”，绕不过“语义”。**

- 已经绕过的部分：本项目用 `spec_type` 从 miot-spec.org 拉规格，属性 / 动作 / 事件都按 `(siid, piid)` / `(siid, aiid)` / `(siid, eiid)` 通用寻址；`Client.run_action()` 本身就是通用调用。所以“每台设备写一套协议代码”是不需要的。
- 绕不过的部分：
  - **UI 语义**：spec 只告诉你“这是 bool / range / value-list”，不告诉你“这张卡上该放哪几个、哪个是主开关、哪个该收进更多功能”。本项目选择按品类手写卡片 + 型号适配，正是为了避免“洗衣机 70 个属性全铺出来”。
  - **动作语义**：`toggle` / `start` / `pause` 这些动作名是型号相关的；通用 UI 只能列出名字，不能替用户判断该点哪个。
  - **文案**：spec 的 `comment` 有官方中文名，但不是所有型号都完整；本项目还有 `enum_text.py` 三层短名兜底。
- **可补的小缺口**：spec 里已经有 `events` 与 `value-list` 的 `comment`；事件轮询接入时可以顺势用它们做事件名 / 参数名的展示，不需要另建映射表。

### 4.5 写操作与共享家庭边界

本项目硬规矩：**共享家庭中的设备一律禁止任何状态改变**。按这条规则分类：

| 操作 | 是否写 | 共享家庭是否允许 | 说明 |
| --- | --- | --- | --- |
| `prop/get`、`event/get`、`GetSceneList`、统计读、耗材读、消息读 | 否 | ✅ 允许 | 纯读 |
| `prop/set`、`action` | 是 | ❌ 禁止 | 设备状态改变 |
| `NewRunScene` 执行场景 | 是（间接） | ⚠️ 需检查 | 场景动作里可能包含共享家庭设备；执行前要解析 `GetSceneList` / `GetSimpleSceneList` 的动作，确认没有共享 did |
| 发虚拟事件 `siid 4 / aiid 1` | 是（事件） | ⚠️ 需确认 | 只影响监听该事件名的自动化；如果自动化动作落到共享家庭设备，等于间接改变 |
| 房间 / 设备管理写 | 是（账号配置） | ❌ 禁止 | 只允许用户自己创建的家庭 |
| 消息标已读 / 删除 | 是（账号数据） | ❌ 不做 | 本项目消息中心已定只读 |

**建议**：把“共享家庭 did 集合”作为写通道的统一闸门，而不是在每个功能里各判一次。现有代码已有 `DeviceNode.home_id` 与 `Topology.homes[].shared`（`mihome/core/api.py:409-450`、`mihome/core/tree.py:51-101`），可以在 `Store.set_value` / `run_action` / `run_scene` 前统一拦截。

---

## 5. 若要落地：建议路线

> 以下是“如果用户拍板要做”的实施顺序，不是本次调研的执行动作。本次没有改任何代码。

### P0-1：事件轮询（只读）

1. `Client` 增加 `poll_events(refs)`，走 `/miotspec/event/get`；返回 `code` / `value` / `updateTime`。
2. 事件源从 `GetSceneList` 触发器 + 规格 `events` + 中枢 `4.1` 生成；不要手写清单。
3. 去重：`(did, siid, eiid)` + `updateTime`；`code=-9` 当“没发生”。
4. 接 `poll_events` 需求表（`1 秒`），不另起 timer；只在相关功能启用时登记。
5. 离线测试：假 API 三种 code；验证“只回调一次”“-9 不报错”“共享家庭只读不触发写”。

### P0-2：自动化列表（只读）

1. `Client.automations(home_id)` 走 `GetSceneList`，带 `app_version:12` / `get_type:2`。
2. 解析触发 / 条件 / 动作 / 启用状态；按 `scene_id` 对齐，不按名字。
3. UI 先做“只读列表 + 说明”，不做编辑。
4. 失败降级为“读不到”，不影响主界面。

### P1：统计可行性探针（只读）

1. 选一台有计量属性的设备，用已知 `key` 调 `stat_day_v3` / `stat_month_v3`。
2. 空结果先换 `key` / 旧版 `data_type` 再判，不直接判“接口不可用”。
3. 记录“型号 → key → 是否有数据”的小表，再决定是否做 UI。
4. 解析 `value` 用 `ast.literal_eval`，不用 `eval`。

### P2：管理写接口（仅在用户明确需要时）

1. 先做“自有家庭”判定；共享家庭直接拒绝。
2. 每个破坏性操作（删除房间）都要二次确认；能不做就不做。
3. 不碰场景 / 自动化写。

---

## 6. 证据索引与可信度

### 6.1 官方公开

| 来源 | 用途 | 链接 |
| --- | --- | --- |
| 小米 IoT MIoT Spec 协议详解 | 属性 / 方法 / 事件 / 服务的定义 | https://iot.mi.com/v2/new/doc/introduction/knowledge/spec |
| 小米 IoT 获取统计量接口 | `app/v2/user/statistics` 的参数、返回、限制 | https://iot.mi.com/new/doc/accesses/direct-access/extension-development/extension-functions/statistical-interface |
| 小米 IoT 控制端 API | 企业 / 直连接入，不适用于个人米家账号 | https://iot.mi.com/new/doc/accesses/direct-access/other-platform-access/control-api |
| Xiaomi Home Integration README | 云端 MQTT 推送、本地中枢网关、LAN control 边界 | https://github.com/XiaoMi/ha_xiaomi_home |
| Xiaomi Home Integration LICENSE | 专有许可，禁止用于开发 APP / Web 服务 | https://github.com/XiaoMi/ha_xiaomi_home/blob/main/LICENSE.md |

### 6.2 社区逆向 / 第三方实现

| 来源 | 用途 | 链接 |
| --- | --- | --- |
| Do1e/mijia-api | 云端封装、CLI / MCP、GPL-3.0、v5 规格迁移 | https://github.com/Do1e/mijia-api |
| mijiaAPI PyPI | 版本 / 元数据 | https://pypi.org/project/mijiaAPI/ |
| rytilahti/python-miio | 本地 miIO / MIoT、genericmiot、GPL-3.0 | https://github.com/rytilahti/python-miio |
| python-miio releases | 0.6.0.dev0 genericmiot | https://github.com/rytilahti/python-miio/releases |
| al-one/hass-xiaomi-miot | 云 + 本地 + spec 自动映射、Apache-2.0 | https://github.com/al-one/hass-xiaomi-miot |
| Xiaomi-cloud-tokens-extractor | token 提取、MIT | https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor |
| Yonsm/MiService | 云端 MiIO / MIoT + 音箱 TTS、MIT | https://github.com/Yonsm/MiService |
| node-mihome | Node 本地 + 云端；最后发布 5 年前 | https://www.npmjs.com/package/node-mihome |
| aholstenson/miio | Node 本地 miIO；2018 停更 | https://github.com/aholstenson/miio |
| mijia-api issue #46 | 统计接口旧版 `powerCost` / `data_type` 差异 | https://github.com/Do1e/mijia-api/issues/46 |

### 6.3 本项目实测

| 结论 | 文档 / 代码 |
| --- | --- |
| 自动化列表、事件通道、虚拟事件 | `docs/reference/mijia-automation-events.md` |
| 极客版登录码 | `docs/reference/mijia-geek-login-code.md` |
| 云 MQTT 推送（个人存档） | `docs/reference/mijia-mqtt-push.md` |
| 在线判定与 `ds=1/2` | `mihome/core/api.py:653-693` |
| 批量读写与静默期 | `mihome/core/api.py:81-86`、`:696-907` |
| 规格缓存与事件解析 | `mihome/core/spec.py:54`、`:765`；`mihome/core/spec.py:598-619` |
| 事件轮询占位 | `mihome/ui/main_window.py:466-470`；`mihome/core/poll_demand.py:13-14` |
| 消息中心只读 | `mihome/core/messages.py`、`mihome/core/api.py:958-980` |
| 统计接口实测（可用性 / 覆盖 / 粒度 / 翻页 / 单位） | 探针脚本与原始输出未入库，仅本地留存 |
| 耗材清单实测（§3.4） | `scripts/probe_consumables.py`；原始结构落 `scripts/out/consumable_items.json`（含 did，已 gitignore） |
| 插件包实测（端点 / 覆盖 / 包结构 / 故障码表 / i18n） | 探针脚本与原始输出未入库，仅本地留存 |
| locale / spec 对比（`zh` 作用域、spec 字段语言、spec vs 插件） | 原始输出未入库，仅本地留存 |
| 同类项目对照 | `docs/reference/mihome-pc-unofficial.md`、`docs/reference/openmexia-notes.md` |

### 6.4 “社区声称存在但本项目未验证”的清单

- **统计接口在共享家庭 / 其他账号 / 其他地区**：本次只测了自有家庭 1 个，共享家庭整组跳过；其他账号、地区、型号的覆盖未验证。
- **耗材清单的 `state` / `filter_ignore`**：`state` 2 / 3 两组只有 2 条样本且 `value` 为空，语义无文档；`filter_ignore` 因本账号无忽略项而看不出差异。
- **耗材 `prop` 的两种非标准写法**：`prop.electricity`（按名字）与 `prop.4106`（疑似拼接）只在老门锁 `loock.lock.t1pro` 出现，按要求没有去猜。
- **房间 / 设备管理写接口**：同类项目说“已通”，本项目没有自己验证；共享家庭边界也没有在代码里统一拦截。
- **中枢网关本地 MQTT**：官方 HA 已实现，但官方没有公开协议；本项目没有验证过鉴权、主题、报文。
- **场景 / 自动化编辑**：社区提到 `AppSceneService/Edit`，本项目明确未验证，且不建议做。
- **`mijiaAPI` 的耗材接口在 5.0.0 的实际返回**：只读了源码，没有用真实账号调用；返回结构可能随云端变化。
- **统计数值的准确缩放**：已确认不能直接信 spec 的 `unit`，但每个型号的准确缩放仍需用米家 App 同窗口数值校准。
- **插件包在全量型号上的覆盖**：本次只抽测 16 个型号（11 有 / 5 空），没有做全量枚举；故障表是否存在于其他品类也未验证。
- **插件包文案的许可边界**：只做了本地读取与结构分析，没有评估再分发 / 商用条款；报告不摘录大段原文。

---

## 7. 实测结论与剩余未验证项

### 7.1 已经从“可能”变成“确定”

- `/v2/user/statistics` **可用**；鉴权、参数格式、`code=0/-8`、空结果语义都已确认。
- **有数据的设备**：4 台插座 / 2 台空调的耗电，1 台温湿度传感器的温度 / 湿度；另有 1 台插座只有历史数据（近 90 天空）。
- **没有数据的属性**：空调 / 净化器 / 开关的温度和湿度、电池电量、灯的电量字符串、电压 / 电流、洗衣机 / 烤箱温度。
- **粒度**：小时仅传感器；耗电是日 / 月；功率在 `stat_day_v3` 下是小时；周粒度按 key 配置，不能依赖。
- **单次 300 点封顶**，可用 `time_end` 翻页；传感器日历史最早 2025-01-09，月度最早 2025-01-01。
- **代价**：按 did 逐个查，p50 约 50–70 ms；90 次请求可以覆盖 32 台候选设备的日粒度矩阵。
- `/v2/plugin/fetch_plugin` **可用**：现有登录态 + `mijiaAPI` 的 RC4 签名即可，不需要额外签名 / UA / 插件版本号；元数据 46–82 ms。
- **取包只依赖 `model`**：不需要 `did`；16 个型号中 11 个有包、5 个空，空结果是 `code=0` + `latest_info: []`，不是鉴权失败。
- **“只在 App 里打开过才有”不成立**：7 个不在本账号设备清单里的型号仍返回了包；覆盖取决于型号是否配置了云端插件。
- **包内容**：RN bundle + `project.json` + 签名 + 图片；部分型号有独立 i18n JSON / errorcode JSON，文案是解码后可读的中文明文。
- **故障码表不是通用能力**：AC m9 有独立 `errorcode.json`（71 条，`{text, code, desc?}`，38 条带排查建议）；净化器只有 value-list 故障枚举；插座完全没有。
- **代价**：包体积 1.1–5.9 MiB，下载 0.7–1.8 s；包为专有内容，只适合离线提取，不适合运行时依赖或再分发。
- **`zh` 不是 spec 参数**：`locale=zh_CN` 是云端会话 Cookie，只影响设备列表展示名；miot-spec.org 对 `locale=zh` / `zh_CN` / `lang=zh` / `Accept-Language` 全部忽略，返回逐字节相同。
- **spec 的中文来自 `comment`**：`description` 恒英文，`comment` 可能中文、英文、缺失或错位；项目用 `comment or description or name` 兜底。
- **枚举文案不需要插件包**：spec `comment` 已覆盖中文基线，三层手写表负责短名 / 纠错；插件包只多 UI 文案、多语言和故障码。**故障码之外，插件包不值得做。**

### 7.2 仍需验证

- **共享家庭 / 其他账号 / 其他地区**的统计覆盖：本次整组跳过，没有结论。
- **单位与缩放**：已确认不能直接信 `spec.unit`；每个型号的准确缩放必须用米家 App 同窗口数值对照。
- **统计配置的长期稳定性**：厂商可能改 key / 粒度 / 保留期；需要失败降级，不要把能力表写死。
- **更早历史**：dev24 空调 300 点封顶，可能还有更早数据；dev06 / dev22 已确认翻页到起点为空。
- **其他型号**：本次只覆盖自有家庭 55 台设备；新设备需要重新探针，不能靠型号猜测。
- **插件包的故障表覆盖**：只确认 AC m9 有独立表；净化器是 value-list；cuco 没有。其他品类、其他型号是否有表，未验证。
- **插件包的版本稳定性**：包是否随 App / SDK 版本变化、旧版本是否长期可下载，未验证；不能把当前包结构当永久契约。
- **插件包文案的许可**：本地读取已做，再分发 / 商用条款未评估。
- **`locale` 对设备列表之外文本的影响**：只确认了设备名与 spec 不受影响；家庭名 / 房间名 / 场景名 / 耗材名是否有个别字段随 locale 变化，未逐项验证。
- **多语言需求**：插件包有十余个语言块，但只有在产品明确要做多语言时才值得评估；本次没有做语言质量审核。

### 7.3 如果要做产品化

1. 先做“能力表”（model + key + 粒度 + 单位缩放 + 起始日期），只对有数据的设备显示历史。
2. 首期只做能耗 + 温湿度传感器，不做“全设备历史”。
3. 历史按需加载，300 点一页，缓存结果；不要轮询统计接口。
4. 空窗口先拉长再判死；单位必须校准后再展示。
5. 全程只读；共享家庭即使将来要读，也只读，不写。
6. 文案层继续用“spec `comment` 兜底 + 三层手写短名”，不要把插件包接成运行时依赖；只有具体型号出现坏文案时才做一次性离线提取。

---

## 8. 报告没有做的事

- 没有发起新的登录；只使用本机已有的 `auth.json` 做只读调用。
- 没有对任何设备发 `prop/set`、`action`、场景执行或虚拟事件。
- 没有安装 / 升级任何依赖。
- 没有修改 `mihome/` 代码、没有提交；只在本地新增了探针与脱敏输出（未入库），并更新了本报告。
- 没有把任何凭据、token、真实 did、家庭 id、家庭名、房间名、内网 IP 写进报告或输出（设备统一用 devNN，家庭用 own-home-N）。
- 没有把插件包整份提交或整份贴进报告；原始包只留在本地（未入库），报告只摘结构、计数与短样例。
- 没有因为插件包探针安装新依赖；只用了项目 venv 的标准库与现有 `mijiaAPI`。
- 没有改代码、没有提交；locale 对比只新增了本地对比输出与临时只读脚本（均未入库）。
