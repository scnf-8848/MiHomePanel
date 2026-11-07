# 米家自动化极客版登录码：免手机获取的可行性调研

> 2026-11-07 调研 + 当日 19:27 真机验证。主程序代码未改；
> 验证只通过独立探针脚本 `scripts/probe_geek_passcode.py` 发过一次取码请求。
> 关联待办：`TODO.md` →「数据与账号能力 → 免手机获取米家自动化极客版登录码」。
>
> **结论（已实测）**：可行。用现有 `Client.raw()._request()` 直接调云端 RPC，
> 不用抓包、不用手机、不用补任何额外请求头，一次调用即可拿到网关地址和 6 位登录码。
>
> **当前状态：暂不实现**（用户 19:37 决定）。入口位置待定，见第 5 节。
> 取新码会让旧码立即失效，实现时务必只读一次、别放进轮询。

## 1. 先厘清概念：和扫码登录不是一回事

| | 本程序的扫码登录 | 极客版登录码 |
| --- | --- | --- |
| 作用 | 登录**米家账号**，拿到账号级凭据，用来读家庭 / 房间 / 设备列表和属性 | 打开**中枢网关自带的局域网后台**时的一次性凭证 |
| 对象 | 小米云端账号（`userId` / `ssecurity` / `deviceId` / `serviceToken`） | 家里的某一台主中枢网关 |
| 形态 | 二维码，登录一次长期有效、自动续期 | 6 位数字，**只能用一次**，用过或失败即作废 |
| 入口 | 程序里的登录页（`QRlogin()`） | 米家 App → 中枢网关 → 中枢功能 / 自动化极客版 |

「自动化极客版」是中枢网关里一个相对独立的本地自动化中枢：浏览器打开
`http://<网关 IP>/`（用户家里的实测地址 `http://192.168.1.10`），
用拖拽卡片、连线的方式写自动化，支持变量、分支、循环、先后顺序，
规则在网关本地执行、不依赖外网。官方刻意把它限制在局域网内。

**痛点**：每次要打开这个网页，都得掏手机 → 打开米家 App → 找到中枢网关 →
中枢功能 → 记下 IP 和 6 位登录码；用完再开又得重来一遍。

## 2. 结论：可行，而且本项目的条件比社区现成方案更好

关键事实：**登录码不是网关本地生成的，而是米家云端的一条 RPC 返回的**。
App 里那几层菜单，本质只是替用户发了这条云端请求。既然是云端请求，
就可以用程序已有的账号凭据自己发，手机和抓包都能省掉。

社区已有三条路，都停在「模拟 App 请求」这一步，但都要额外成本：

| 方案 | 做法 | 代价 |
| --- | --- | --- |
| `ChishFoxcat/MIJIA_GEEK`（油猴脚本） | 抓包后手动填 URL / Cookie / POST body，脚本代为重放 | 要先抓包，参数要手填，Cookie 会过期 |
| `609293964/MIJIA_GEEK-for-fnos`（飞牛 NAS 版） | 导入 HAR 自动识别请求参数，本地代理转发 | 仍要抓一次包、要额外部署服务 |
| `eyaeya/xiaomi-central-hub-gateway-cli` | 直接用米家账号 Cookie + RC4 签名协议云端取码，CLI 一步完成 | 已接近「零抓包」，但是独立 CLI |

本程序**已经持有账号凭据和那套 RC4 签名协议的实现**，所以不需要抓包、
不需要额外服务，在程序里加一个按钮就能取码。

**19:27 实测**：账号里找到两台网关类设备，其中
`did=1000000001 / Xiaomi 中枢网关 / xiaomi.gateway.hub1` 是主中枢
（另一台 `lumi.gateway.mcn001` 是普通多模网关，**不是**中枢）。
对主中枢依次调用两个 RPC 均一次成功：

- 取地址 → `{"url": "http://192.168.1.10"}`
- 取登录码 → `{"passcode": "<6 位数字>"}`

登录码按约定不写入任何文件，只在终端输出给使用者。

## 3. 接口细节（公开资料 + 本机实测）

```
Base:  https://api.mijia.tech/app
POST   /home/rpc/<did>
Auth:  米家账号 Cookie + RC4 签名表单（_nonce / data / signature / ssecurity）
```

抓包实现里也出现过 `https://core.api.mijia.tech/app/home/rpc/<did>` 的写法，
属于同一族接口的不同域名/网关前缀。**实测 `api.mijia.tech` 直接可用**，
备用域名没有走到就成功了。

请求体（JSON，经签名加密后放进表单 `data` 字段）：

| 用途 | body | result |
| --- | --- | --- |
| 取极客版地址 | `{"method":"miIO.get_autowebconfig_url","params":[]}` | `{"url":"http://<网关 IP>:<port>"}` |
| 取登录码 | `{"method":"miIO.get_central_link_passcode","params":[]}` | `{"passcode":"123456"}` |

前端抓包工具里出现的请求头 —— **实测这些都不是必需的**，
`mijiaAPI` session 自带的 Cookie / `x-xiaomi-protocal-flag-cli` /
`Content-Type` 已经够用：

```
MIOT-REQUEST-MODEL: xiaomi.gateway.hub1
domain-refer: core.api.mijia.tech
Origin-From: MiHome
X-XIAOMI-PROTOCAL-FLAG-CLI: PROTOCAL-HTTP2
operate-common: <抓包中的值>
Content-Type: application/x-www-form-urlencoded
Cookie: cUserId=...; serviceToken=...; PassportDeviceId=...; locale=...
```

响应可能被 RC4 加密，`mijiaAPI._request()` 内部的 `decrypt()` 会处理，
调用方拿到的就是干净的 `result`。

**`<did>` 怎么选**（最容易踩的坑）：必须是承载中枢功能那台设备的 did。
中枢可能是独立网关，也可能内置在路由器、带屏音箱、智能存储里，
**不能只按 model 含 `gateway` 过滤**；多个中枢时要选**主中枢**。
本机实测：名字含「中枢网关」、model 为 `xiaomi.gateway.hub1` 的那台就是，
正好和抓包里的 `MIOT-REQUEST-MODEL: xiaomi.gateway.hub1` 对上；
同家庭里另一台 `lumi.gateway.mcn001` 不要选。

网关本地侧（取到码之后）是另一套协议：`ws://<IP>/centrallinkws/`，
WebSocket + ECJPAKE 握手 + AES-GCM 加密 + deflateRaw 压缩 + JSON-RPC 2.0。
登录码就是这次握手的输入。这部分只有想「连码都不让用户看见」时才需要做。

## 4. 本项目的落点：几乎不用写新代码

核对过的现有条件：

- `.venv/Lib/site-packages/mijiaAPI/apis.py:29`
  `self.api_base_url = "https://api.mijia.tech/app"` —— 与上面 Base **完全一致**
- 同文件 `:312` 的 `mijiaAPI._request(uri, data)` 已经封装完整链路：
  组 `data` → `gen_nonce()` → `get_signed_nonce()` → `generate_enc_params()`
  （`_nonce` / `data` / `signature` / `ssecurity` 的 RC4 签名表单）
  → POST → 必要时解密响应 → 校验 `code` → 返回 `result`
- `mihome/core/api.py:368` 的 `Client.raw()` 已把这个上游实例暴露出来
  （注释写的就是「逃生口」）
- `mihome/core/icons.py:99-112` 有「自己拼签名打私有接口」的先例，
  真要自定义 header 时可照它手写一次（本次实测用不上）

所以核心逻辑就是两行（**已按此跑通**）：

```python
api = client.raw()
url = api._request(f"/home/rpc/{did}",
                   {"method": "miIO.get_autowebconfig_url", "params": []})["url"]
code = api._request(f"/home/rpc/{did}",
                    {"method": "miIO.get_central_link_passcode", "params": []})["passcode"]
```

`MIOT-REQUEST-MODEL` / `domain-refer` 之类的头实测**不需要补**；
`_request()` 用的 `self.session` 原样就够了。探针脚本里仍保留了
「手动签名 + 备用域名 + 额外头」的兜底路径，用于将来接口变动时排障。

探针脚本：`scripts/probe_geek_passcode.py`

```
.venv/Scripts/python.exe -X utf8 scripts/probe_geek_passcode.py                       # 列候选设备
.venv/Scripts/python.exe -X utf8 scripts/probe_geek_passcode.py --did <did> --url-only  # 只取地址，不耗码
.venv/Scripts/python.exe -X utf8 scripts/probe_geek_passcode.py --did <did>           # 取地址 + 取一次码
```

设计上先用「取地址」探出可用组合，确定后才调一次取码，避免反复作废旧码；
登录码只打印到终端，不写文件、不进日志。

## 5. 产品形态建议（**暂不实现**，入口待定）

> 用户 2026-11-07 19:37 决定：先留着，不急着做。入口**明确不放设置页**
> （多一层跳转、获取流程变长），大概在主页面某个位置，但具体位置还没想好。
> **入口定下来之前不要动手做 UI**；下面三档只作为到时可选的形态。

- **A · 最小可用**：主页面某处一个「取极客版登录码」入口 →
  显示 6 位码 + 复制按钮 + 一键用默认浏览器打开 `http://<网关 IP>/`。
  把「掏手机点五层菜单」压成一次点击。**前置条件已验证完毕**。
- **B · 顺手**：同时调 `miIO.get_autowebconfig_url` 自动发现网关地址，
  IP 记进配置，连手输 IP 都省掉（同样已验证）。
- **C · 进阶**：程序内直接完成本地 WebSocket 登录并**常驻会话**，
  连码都不必出现。参考 `oh-my-sage` 的「本机常驻服务只登录一次」思路，
  但要实现 ECJPAKE + AES-GCM 那一套，工作量明显更大，先不做。

另外，即使不自动取码，「常驻会话」本身也是一个独立的降摩擦方向：
网关只登录一次，之后所有调用复用连接，取码频率大幅下降。

实现时：候选设备按 `name` 含「中枢网关」或 `model == xiaomi.gateway.hub1` 找，
多个候选时让用户选一次并把 did 记进 `prefs.json`；找不到就退回手动输码。

## 6. 验证结果与剩余待确认

已验证（2026-11-07 19:27，用户账号 + 家中 `Xiaomi 中枢网关`）：

- [x] 主中枢 did 可直接从 `get_devices_list()` 找到；普通多模网关要排除
- [x] 当前固件支持两个 RPC；`result` 字段确为 `passcode` / `url`
- [x] `https://api.mijia.tech/app/home/rpc/<did>` 直接通，无需备用域名
- [x] `_request()` 的 session 头够用，不必补 `MIOT-REQUEST-MODEL` 等
- [x] 复用现有 `auth.json` 登录态即可，不需要重新扫码或抓包
- [x] 响应解密由 `_request()` 处理，调用方拿到干净的 dict
- [x] **取新码会让旧码立即失效**（用户 19:37 的实际使用经验，非本次实测）

还没验证（不影响 A 方案落地）：

- [ ] 取到的码在浏览器里能否正常完成登录（本次只验证到「云端给出码」）
- [ ] 码失效 / 不同网 / 网关离线时，网页侧的错误表现如何归类
- [ ] 账号 token 过期后沿用现有 `QRlogin()` 重登是否平滑
- [ ] 别的型号（路由器 / 屏幕 / 存储内置中枢）是否同样支持这两个 RPC

另外有一条**不是落地前置、纯属好奇**的疑问：码放着不用，多久会自己过期？
用户 19:43 明确说「没有落到程序上的需求，但我好奇」，已单列为
`TODO.md` →「纯好奇 / 非功能疑问」，等有条件再测；测出结论回填本节即可，
不要为它加 UI、配置项或轮询。

## 7. 风险与边界

- 这是**私有接口**，米家 App / 网关固件更新随时可能变，实现时要留降级到手动输码的口子
- 登录码是「物理邻近 + 一次性」的安全设计，自动化取码只用于自己账号、自己家里的网关
- **取新码 = 旧码作废**：取码只在用户主动触发时调一次，不要放进轮询、
  自动刷新、主题切换 / 窗口重建这类会重复触发的路径，也不要做后台预取
- 码与账号凭据都是高敏信息：只在内存里传，不写日志、不落盘、不上传；
  `auth.json` 本来就存在，不要因为这个功能扩大它的分发范围
- 极客版刻意只开放局域网，不要把网关地址或取码能力暴露到公网 / 内网穿透
- 探针脚本会真实消耗一次取码额度，别加进自动化验证脚本集里定时跑

## 8. 资料

| 内容 | 链接 |
| --- | --- |
| 云端取址 / 取码 RPC 契约（2026-10-13 实机，路由器集成中枢） | https://github.com/eyaeya/xiaomi-central-hub-gateway-cli/blob/main/docs/api/cloud-gateway.md |
| 油猴脚本（抓包重放） | https://github.com/ChishFoxcat/MIJIA_GEEK |
| 飞牛版 HAR 自动解析 + 本地代理 | https://github.com/609293964/MIJIA_GEEK-for-fnos |
| 极客版网关本地协议（WebSocket / ECJPAKE / 规则结构） | https://github.com/weieast1314/oh-my-sage/blob/main/ref/GUIDE.md |
| AI Agent / MCP 与「常驻服务只登录一次」 | https://github.com/allocnode/oh-my-sage |
| 官方操作路径（中枢功能里看 IP 与登录码） | https://www.asmslight.com/?p=41/ |
| MIoT-Spec RPC 报文格式 | https://iot.mi.com/v2/new/doc/embedded-dev/module-dev/rpc |

项目内对照：`scripts/probe_geek_passcode.py`、`mihome/core/api.py:368`、
`mihome/core/icons.py:99`、`.venv/Lib/site-packages/mijiaAPI/apis.py:29,312`、
`.venv/Lib/site-packages/mijiaAPI/miutils.py:50`。
