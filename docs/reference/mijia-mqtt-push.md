# 伪装成官方 HA 集成接米家云 MQTT 推送（个人自用存档）

> **定位（2026-11-13 用户明确）**：这套方案**留着、不删**。后续可能**仅个人使用**，
> **不分发、不进入发行包**，也不作为本项目的公开功能。
>
> **许可边界**：官方集成 `XiaoMi/ha_xiaomi_home` 是**专有许可**，其代码**不得复用**。
> 本文只记录独立实现要点与实测结论，不含任何从该仓库拷贝的代码。实际探针在仓库外
> `e:\Code\_mqtt_probe`（`xm_auth.py` / `probe.py` / `selftest.py` / `FINDINGS.md` /
> `REPORT-actions.md`），不进本仓库、不进发行包。
>
> **风险**：探针复用了该集成注册的 OAuth 客户端身份（`client_id 2882303761520251711`、
> `redirect_uri http://homeassistant.local:8123`），该身份并未授权给本项目。小米可随时
> 通过风控使该身份失效，甚至影响账号。只在本机验证 / 个人使用。

## 1. 一句话结论

**能用。** 一条 MQTT 长连接即可覆盖账号下全部设备，Wi-Fi 设备与蓝牙子设备都能收到
属性变化与事件推送，端到端 **< 1 秒**；但**拿不到上下线**，QoS 实际是 0，且必须逐设备
白名单订阅。

## 2. 通道参数（必须这样拼）

| 项 | 值 | 为什么必须是这个 |
|---|---|---|
| Broker | `cn-ha.mqtt.io.mi.com:8883` | 命名规则 `{region}-ha.mqtt.io.mi.com`；`cn` 对应中国大陆账号 |
| 协议 | **MQTTv5 + TLS** | 与官方集成一致；v3.1.1 未验证，不要假设可用 |
| `username` | OAuth `client_id` = `2882303761520251711` | broker 用「OAuth 客户端身份」定位这批发给 HA 的权限；换成别的用户名会 `135 Not authorized` |
| `password` | **access_token** | 账号级 OAuth 令牌，不是密码、不是 refresh_token；过期后 CONNECT 会被拒 |
| `client_id` | `ha.{32 位大写 hex UUID}` | 必须与换取 token 时用的 `device_id=ha.{uuid}` 同源；变了会被当成新客户端，token 与设备身份对不上 |
| TLS 信任 | **系统信任库即可** | 实测证书是 DigiCert `*.mqtt.io.mi.com`；`xiaomi_home` 里那份 Mijia 私有 CA 是**局域网直连**用的，这里用不上 |

补充：

- UUID **必须落盘复用**（`tokens.json`），不能每次启动重新生成。同一 `client_id`
  并发会**互踢**，所以同一身份只允许跑一个实例。
- MQTT `keepalive` 实测用 60 秒即可；断线重连退避 5 秒起、上限 120 秒。
- 这条通道是**纯增量**的：broker 不 retain、订阅时不补发当前状态。连上后仍需用现有
  HTTP 属性接口做一次「种子读取」，否则界面在第一条变化到来前是空的。

## 3. 鉴权：授权码 → token → 永续滚动链

### 3.1 拿 code

浏览器打开（参数照抄）：

```
https://account.xiaomi.com/oauth2/authorize
  ?redirect_uri=http://homeassistant.local:8123
  &client_id=2882303761520251711
  &response_type=code
  &device_id=ha.{uuid}
  &state=sha1("d=ha.{uuid}")
  &skip_confirm=true
```

登录并同意后，浏览器跳到 `http://homeassistant.local:8123/?code=...&state=...`。
本机没有这个域名，页面会报「无法访问」，**但地址栏里带着 code**，把整条地址复制回来
即可。想自动收 code 才需要在 hosts 里加 `127.0.0.1 homeassistant.local` 并本地监听
8123（探针的 `--callback`）。

### 3.2 换 token

不是标准 `/oauth2/token`，而是：

```
GET https://ha.api.io.mi.com/app/v2/ha/oauth/get_token
    ?data={"client_id":"2882303761520251711",
           "redirect_uri":"http://homeassistant.local:8123",
           "code":"...",                 // 或用 refresh_token 替换这一项
           "device_id":"ha.{uuid}"}
```

返回 `result.access_token` / `result.refresh_token` / `result.expires_in`。
后续 HTTP 调用另需请求头 `X-Client-BizId: haapi`、`X-Client-AppId: <client_id>`、
`Authorization: Bearer<token>`（官方实现里 **Bearer 后没有空格**，照抄）、
以及固定 `User-Agent: ha_xiaomi_home/... client/ha.{uuid}`。

### 3.3 有效期与刷新（关键）

- `expires_in = 259200` 秒 = **3 天**，指 **access_token**。
- **refresh_token 单次使用、每次刷新强制换发**，旧的立即失效。重复使用旧值实测返回
  **`96009 invalid refresh token`**。所以持续运行就是一条**永续滚动链**：
  每次刷新都必须把新的 refresh_token 原子落盘，丢了就只剩重新走浏览器授权。
  > 早期 `FINDINGS.md` 曾记成「refresh_token 仍保留、可重复用」，后续实测纠正为单次使用。
- **刷新后不必强制断开重连**：实测旧 access_token 仍能完成 CONNECT（broker 未立刻
  吊销）。但**下一次重连必须用新 token**，不要把旧值继续传。
- **无效 token 在 CONNECT 阶段被 `135 Not authorized` 拒绝**，不是先连上再踢。
  所以 `135` 应归类为「凭据问题」，而不是网络问题。
- 建议的工程量（照官方集成的做法，复现时值得照做）：
  - 到期时间按 `now + expires_in × 0.7` 记账，**提前刷新**（3 天 → 约 2.1 天）；
  - 定时刷新失败要**持久提示**（不能只写日志），否则链断了没人知道；
  - 错误分类：`96009` = refresh token 已废 → 必须重新授权；`135` = access token
    无效/被吊销 → 先刷新，仍失败再重新授权；网络类 → 退避重试，不要消耗 refresh token；
  - token 落盘用**临时文件 + `os.replace` 原子写**，进程内用**锁**保证同一时刻只有
    一个刷新者——两处并发刷新会让后一个拿到 `96009`，等于自己把链掐断；
  - 刷新与重连也要串行：重连读到的必须是刷新后的 token。

## 4. 订阅规则：逐设备白名单

broker 对主题做**逐设备白名单**，任何通配跨设备都会被拒：

| 主题 | 结果 |
|---|---|
| `device/#` | `SUBACK 135 Not authorized` |
| `device/+/state` | `135` |
| `device/{did}/#` | `135` |
| `device/{did}/down/#` | `135` |
| `device/{did}/up/#` | **OK（granted QoS2）** |
| `device/{did}/up/properties_changed/#` | **OK** |
| `device/{did}/up/event_occured/#` | **OK** |
| `device/{did}/state/#` | **OK**（但实测收不到消息，见第 7 节） |

因此：**每台设备 2 条订阅**（`up/#` + `state/#`），N 台设备 = 2N 条，一次 CONNECT
内全部订阅。实测 55 台 / 110 条全部 `SUBACK [2]`，无一条被拒。

> `down/#` 被拒是好事也是边界：这条通道**只能读**，不能借它下发控制。写设备仍走
> 现有的云端 `prop/set` / `action` 接口。

## 5. payload 格式：纯 UTF-8 JSON，无 MIPS 封装

wire 上就是 JSON 文本。探针保留了 MIPS 解包兜底，但**本次窗口一条都没用上**。

### 5.1 属性变化

主题：`device/{did}/up/properties_changed/{siid}/{piid}`

```jsonc
{"method":"properties_changed",
 "params":{"did":"685621106","siid":2,"piid":2,"value":36,"parent":"70039278863"}}
```

开关类会多带上一跳，用于去重 / 补状态：

```jsonc
{"method":"properties_changed",
 "params":{"did":"685621106","siid":2,"piid":1,"value":false},
 "last_timestamp":1794319742,"last_value":true}
```

蓝牙子设备经网关转发，多带 `tid` / `from` / `parent`：

```jsonc
{"method":"properties_changed",
 "params":{"from":"1073257947","did":"2102730237","siid":2,"piid":1,
           "value":true,"tid":130,"parent":"1073257947"}}
```

### 5.2 设备事件

主题：`device/{did}/up/event_occured/{siid}/{eiid}`

```jsonc
{"method":"event_occured",
 "params":{"did":"1073257947","siid":4,"eiid":1,
           "arguments":[{"piid":1,"value":"挂灯关闭"}]}}
```

`arguments[].value` 是**中文文案**（云端已翻译），可直接展示；要拿结构化语义仍应回
本地规格缓存查 `eiid`。

### 5.3 字段约定

- `did/siid/piid/value/parent` 恒有；`value` 可能是 int / bool / string。
- `from/tid/last_tid/last_tid_cnt/ts` 只在网关转发的子设备消息里出现。
- `ts` 与 `last_timestamp` 都是**秒级 epoch**。
- `parent` 在蓝牙子设备上是**网关 did**；在 Wi-Fi 设备上实测是「云端父节点标识」，
  不在设备清单里，不能当网关用。
- `siid/piid/eiid` 与 MIoT 规格一致，可直接用本项目已有的规格缓存翻译成中文属性名
  / 事件名，不需要额外映射表。

## 6. 实测数字（2026-11-11 凌晨）

| 项 | 实测 |
|---|---|
| 设备数 / 订阅数 | 55 个 did / 110 条订阅，全部一次 SUBACK 成功 |
| 窗口 | 00:08:16–00:18:16（10 分钟）；另一份统计窗口 00:08:16–00:17:02，1299 条 |
| 消息量 | 单窗约 1500 条（1504）；属性 1495 + 事件 9 |
| 端到端延迟 | `last_timestamp` 与消息到达时间精确到同一秒 → **< 1 秒**（秒级精度无法再细） |
| QoS | 申请 2、SUBACK 也是 2，但**实投 `qos=0`** |
| retain | 全部 `false` |
| 稳定性 | 8.7 分钟窗口：连接 1 次、断开 0 次、重连 0 次、SUBACK 失败 0 条 |
| 峰值速率 | 蒸烤箱持续约 **2.4 msg/s**，broker 未降速、未断开 |

> 设备数口径：同一晚 `FINDINGS.md` 记 55 个 did；`REPORT-actions.md` 的云端清单记
> 47 台（多家庭 / 共享家庭 / 分页差异）。复现时**以实际拉到的全部 did 为准**，
> 不要把 55 或 47 当常量。

## 7. 已知缺口与坑

1. **`state` 主题全程 0 条**。订阅被放行，但 10 分钟窗口内（含拔电重上电）没有收到
   任何上下线消息 → **这条通道拿不到在线状态**。在线判定仍要走现有属性轮询 +
   离线错误码，别指望 MQTT。
2. **拿不到 model / name 映射**。`get_dev_room_page` 只返回 did（按房间分组），
   MQTT payload 里也没有型号与名称。需要另调 `device_list_page` / `gethome_merged`
   补设备名 / 型号，再和 did 对齐。
3. **同一 `client_id` 并发会互踢**。UUID 必须固定、落盘，只跑一个实例；调试时开两个
   探针会互相把对方踢下线。
4. **token 刷新与重连并发会丢链**。刷新会轮换 refresh_token 且旧的立即失效；两处
   同时刷新，后一个必得 `96009`。必须进程内加锁 + 原子落盘（见 3.3）。
5. **个别设备上传极高频**。蒸烤箱 `chunmi.oven.x14`（did `828487133`）的
   `siid=2 / piid=8`（腔体温度）约 **每 0.4 秒一条**，占单窗 93%（1398/1504）。
   它会在 24/25℃ 之间抖动，属于设备固件周期上报，不是云端重复投递。
   接入时要**按 (did, siid, piid) 过滤或降采样**，否则一条设备就能打满处理队列。
6. **QoS0 无投递保证**。不能假设「没收到 = 没发生」；对关键状态仍要保留 HTTP 轮询
   兜底，MQTT 只当加速层。
7. **只有单向**。`down/#` 被拒，控制仍走原有 `prop/set` / `action`。

## 8. 最小实现步骤（照着做，不贴代码）

### 第 1 步：浏览器授权拿 code

- 拼出第 3.1 节的授权 URL，用默认浏览器打开，登录并同意。
- 回调地址打不开没关系，从地址栏复制整条 URL，解析出 `code`（`state` 应等于
  `sha1("d=ha.{uuid}")`，不一致只告警、不阻断）。
- 注意：`uuid` 首次生成后必须落盘，后面所有步骤都用同一个。

### 第 2 步：用 code 换 token

- 调 `GET /app/v2/ha/oauth/get_token`，`data` 里带 `client_id` / `redirect_uri` /
  `code` / `device_id`。
- 落盘 `access_token` / `refresh_token` / `expires_in` / 到期时间；写入用临时文件 +
  原子替换。
- 换完可调 `open.account.xiaomi.com/user/profile` 验一次 token（探针的 `whoami`）。
- 注意：token 文件含凭据，不要外传、不要写日志、不要进仓库。

### 第 3 步：拉设备清单

- `POST /app/v2/homeroom/get_dev_room_page`，body `{"start_id":"","limit":150}`，
  按 `result.has_more` / `result.max_id` 分页，直到拿全。
- 从返回里抠出每个房间的 `dids`；这就是订阅白名单。
- 注意：这里**没有 model / name**；需要名称时再调 `device_list_page` /
  `gethome_merged` 按 did 合并（两份数据的家庭口径可能不同，要按 did 对齐）。

### 第 4 步：逐台订阅

- 对每个 did 订阅 `device/{did}/up/#` 与 `device/{did}/state/#`，一次 CONNECT 内
  全部发完；申请 QoS2。
- 逐条检查 SUBACK：`[2]` 为成功，`135` 说明主题形态不对（大概率用了通配或
  `down/#`）。
- 注意：`state/#` 即使成功也收不到消息，保留订阅只是为了将来可能开放，不要据此
  判断设备在线。

### 第 5 步：收消息并落 JSONL

- 按第 5 节解析 `properties_changed` / `event_occured`；主题里的 `siid/piid`
  与 payload 里的通常一致，但以 payload 为准。
- 每条记录至少落：接收时间、主题、qos、retain、payload、解析后的
  `did/siid/piid|eiid/value`；排障时再算延迟（`recv - ts`）。
- 翻译用本地规格缓存（`siid/piid → 属性名`、`eiid → 事件名`），不要维护第二套表。
- 注意：先按 `did` 与 `(siid,piid)` 做过过滤 / 降采样，特别是蒸烤箱那类高频设备。

### 第 6 步：定时刷新 token

- 按 `now + expires_in × 0.7` 定下一次刷新；刷新成功后**原子替换** refresh_token。
- 刷新失败分类处理（第 3.3 节）；`96009` 或多次 `135` 后回到第 1 步重新授权。
- 注意：刷新与重连串行；同一时刻只允许一个刷新者在跑。

## 9. 复现产物（仓库外）

`e:\Code\_mqtt_probe`：

```powershell
.\.venv\Scripts\python probe.py login          # 浏览器授权，粘贴回调地址
.\.venv\Scripts\python probe.py devices        # 拉 did 清单
.\.venv\Scripts\python probe.py watch --all    # 订阅全部，Ctrl+C 看汇总
.\.venv\Scripts\python analyze.py --detail     # 离线分析最新 messages.jsonl
.\.venv\Scripts\python selftest.py             # 离线自检
```

产物：`tokens.json`（**含凭据，勿外传**）、`devices.json`、`logs/*-watch.log`、
`logs/*-messages.jsonl`。`FINDINGS.md` 是总记录，`REPORT-actions.md` 是同一窗口的
逐设备动作清单。

## 10. 结论与使用边界

- 这是目前唯一实测到的「**米家 → 电脑** 亚秒级推送」通道，优于 3 秒属性轮询，
  但**不能替代**轮询（无 retain / 无上下线 / QoS0）。
- 仅个人自用：**不分发、不进发行包、不写进公开功能列表、不复用官方集成代码**。
- 若以后要产品化，必须先解决 OAuth 客户端身份的合法来源；在拿到授权之前，
  这套方案只能停留在本机实验与个人使用。
