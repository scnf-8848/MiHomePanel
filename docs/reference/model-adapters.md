# 型号适配台账：哪些设备被单独适配过

> **这份表回答两个问题**：这台设备适配过没有？我的设备该照着哪台写？
>
> **代码是唯一事实来源**：规则在 `mihome/core/adapters/<厂商>.py`，本表由
> `scripts/list_adapters.py --md` 的输出贴过来。`scripts/verify_adapters.py` 会检查
> **表里漏了型号就报失败**，所以别担心它过期——忘了更新会被全套验证逮住。
>
> 刷新：`.venv/Scripts/python.exe scripts/list_adapters.py --md`

## 一、已适配（现役）

| 型号 | 设备 | 适配内容 | 为什么 | 文档 |
|---|---|---|---|---|
| `dmaker.fan.p5c` | 米家智能直流变频落地扇 1X | 按模式换控件 | mode 0=直吹风认 speed-level(1–100)，1=自然风认 fan-level(1–4)，两者都可写 | [fan-purifier-speed.md](fan-purifier-speed.md) |
| `xiaomi.gateway.hub1` | Xiaomi 中枢网关 | 极客版入口 | 极客版入口（2×1 / 1×1 两种卡）只靠这条白名单出现，不再逐台探测 | [mijia-geek-login-code.md](mijia-geek-login-code.md) |
| `xiaomi.lock.s1` | 小米智能门锁 4 Pro | 电量拆行 | 两块电池属性/服务同名；锂电 siid 13 更靠后被干电池 6 抢走，按 siid.piid 显式拆行 | [lock-dual-battery.md](lock-dual-battery.md) |
| `yeelink.light.lamp22` | 米家显示器挂灯 1S | 多读属性、模式判据 | mode(2,5) 手调亮度/色温后不变，真实预设要看只读的 scene-mode(3,9) | [architecture.md](../dev/architecture.md) |

## 二、评估过但**没有**适配

记在这里是为了**别再评估一遍**。状态分两种：明确不做 / 等真机。

| 型号 | 设备 | 结论 | 理由 |
|---|---|---|---|
| `loock.lock.401` | 鹿客 V6 Max（双电池：主电池 + 猫眼电池） | **明确不做** | 2026-11-16 用户确认：另一家庭的锁，用不上。真要适配只需加一条 `batteries=(("4.1003","电池"),("10.1","猫眼电池"))`，两行会自动并排、副标题自动变 `x% · y%` |
| `loock.lock.t1pro` | 小米全自动门锁 Pro（单块干电池） | **不需要** | 只有一块电池，`find` 取到的就是它，无歧义。用户确认只用干电池 |

## 三、给自己的设备加一条：照这四步

### 1. 先确认真的需要

打开 `adapters/` 对应厂商文件（没有就新建），看有没有同型号或同形态的先例。**只有
"通用逻辑解释不了"才该写规则**——判据是：不写这条，某台设备会显示错、点不动、或者
少一块数据。前面两种"能不能省事"的诱惑已经踩过坑：

- 别按品类推广（"米家的锁都差不多"）——型号第一段是厂商，第二段是品类，**记录键必须
  是完整型号**；
- 别用云端中文 `comment` 当键或判据（「锂电池」这种），米家改一个字就静默失效；
- 别为了"看起来更通用"去改 `catalog.py` 的通用顺序——那会顺手改掉没验证过的设备。

### 2. 选字段（一条记录可同时用多个）

| 字段 | 什么时候用 | 风险 |
|---|---|---|
| `extra=("属性名", …)` | 某个属性要参与轮询但不在卡片上显示 | 多读一条，低 |
| `mode_active="属性名"` | 模式高亮的判据不是 `mode`(2,5)，而是另一个只读属性 | 低，只影响高亮 |
| `speed_by_mode=("mode", {值: 槽位})` | 同一功能有**两套都可写**的属性，谁生效由模式决定 | **高**：写错会发错命令 |
| `batteries=((siid.piid, 行名), …)` | 一个槽位要拆成多行显示（如两块电池） | 低，只读显示 |
| `geek=True` | 这台型号**带米家自动化极客版**（局域网后台），要在卡片上给「极客版入口」 | 无 IO；判据是白名单，命中就零请求，见下 |
| `text=TextLayer(...)` | 只改文案。**优先用类别级**，这里只放这台设备独有的词 | 无 IO |

`geek=True` 是**唯一一个"能力"型字段**：它不读也不写设备，只回答"这台型号有没有
极客版"。以前这件事靠给每台设备各发一条探测去问（实测 43 条请求只换来 1 台设备
显示入口），现在登记一次就够；没登记的型号只有 gateway / router / screen /
central-control 这四类才会发一条兜底探测。**新买的中枢设备没看到入口，先来这里
加一行**，比等探测快也可靠。

### 3. 写全 `device` / `note` / `doc`

导入期硬校验，缺一个就报错：

```python
"xiaomi.lock.s1": ModelAdapter(
    batteries=(("13.1", "锂电池"), ("6.1003", "干电池")),
    device="小米智能门锁 4 Pro",                    # 人认得出是哪台
    doc="docs/reference/lock-dual-battery.md",     # 实测结论在哪
    note="两块电池属性/服务同名；锂电 siid 13 更靠后被干电池 6 抢走，按 siid.piid 显式拆行",
)
```

- `note` 写**为什么**，不写"做了什么"（做了什么字段里就有）；
- `doc` 指向 `docs/reference/` 的调研记录；没有调研文档就指到 `docs/dev/architecture.md`
  对应小节。

### 4. 更新本表并跑验证

```
.venv/Scripts/python.exe scripts/list_adapters.py --md   # 把输出的表格行贴进第一节
.venv/Scripts/python.exe scripts/verify_adapters.py
```

`verify_adapters.py` 检查：每个型号在本表出现过、`doc` 指向的文件真实存在、`device` /
`note` 非空、`FIELD_LABELS` 覆盖所有字段、导入期校验真的会拦错。**漏更新本表会直接
判失败**，这是刻意设计的——台账的价值全在"不会忘"。

## 四、字段速查（给写代码的人）

`ModelAdapter.fields` 返回这条记录实际用到的字段键，`FIELD_LABELS` 给中文：

```python
from mihome.core.adapters import describe_all, FIELD_LABELS

for row in describe_all():
    print(row["model"], row["device"], [FIELD_LABELS[f] for f in row["fields"]])
```

`describe_all()` 就是"总接口"：程序里要展示"已适配设备"（比如诊断页）时用它，
不要另外维护一份清单。
