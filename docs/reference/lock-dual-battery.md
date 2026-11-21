# 门锁的两块电池：读数怎么显示（2026-11-16 落地，当天返工两次）

> **状态：已落地**，且是**型号级**方案——只有 `xiaomi.lock.s1`（小米智能门锁 4 Pro）
> 登记了拆行规则，其余设备一行不动。
> 改动面：`core/adapters/base.py`（`ModelAdapter.batteries` + `check_batteries`）、
> `core/adapters/xiaomi.py`（新）、`core/catalog.py`（`apply_adapter` 入口 / `_expand_batteries` 执行）、
> `core/adapters/__init__.py`（登记厂商模块）。
> 回归 `scripts/verify_battery_binding.py`；探针 `scripts/probe_lock_battery.py`。

## 1. 问题

小米智能门锁 4 Pro（`xiaomi.lock.s1`）同时装**两块电池**：锂电池（主用）+ 5 号干电池
（备用）。米家 App 的设备页把它们并排显示（2026-11-16 截图：干电池 100%、
锂电池 54%）。本程序只有一个「电量」读数，跟的是**干电池**。

用户的要求分两条，都不是"只显示一块"：

1. **首要看锂电** → 卡片副标题里出现的必须是锂电；
2. **详情页两块都要** → 「备用电池不意味着不需要查看」。

## 2. 实测：三台门锁的电池服务结构

数据来自真机规格（`SpecRepository` 缓存）+ 只读属性读取
（`scripts/probe_lock_battery.py --room 示例家庭-示例-客厅 --read`）。只有 4 Pro 在线，
另两台用于对照结构。

| 型号 | 服务 | 属性 | 服务注释 | 有 `charging-state` | 实测值 |
|---|---|---|---|---|---|
| `xiaomi.lock.s1`（4 Pro） | siid 6 | 6.1003 `battery-level` | 干电池 | 否 | **100%** |
| | siid 13 | 13.1 `battery-level` | 锂电池 | 是 | **54%** |
| `loock.lock.401`（V6 Max） | siid 4 | 4.1003 `battery-level` | 电池 | 是 | — |
| | siid 10 | 10.1 `battery-level` | 猫眼电池 | 是 | — |
| `loock.lock.t1pro` | siid 4 | 4.1 `battery-level` | Battery | 否 | 66% |

两条结构事实：

1. 两块电池的**属性名与所在服务名完全相同**（都叫 `battery-level` / `battery`）。
   同名不等于同物，**能唯一定位的只有 `siid.piid`**。
2. **`charging-state` 不等于"这是锂电池"**（2026-11-16 用户补充）：V6 Max 两块都是
   可充电锂电，这条在两块上都成立；t1pro 只有一块干电池，两块都没有。它只能说明
   "这块能充电"。
3. 两块电池的**关系**也不同：4 Pro 是「锂电主用 + 干电池备用」，V6 Max 是
   「主电池 + 猫眼电池」。规格里没有任何字段说明这种关系——它是设备的物理形态。

## 3. 根因

`core/spec.py` 的 `DeviceSpec.find()`：服务名精确命中优先，没有命中就返回候选**第一条**，
候选顺序 = 规格里的服务 iid 升序。门锁的主服务是 `lock`，两块电池都在 `battery`
服务里 → 永远精确命中不到 → 取 siid 小的那条。

4 Pro 的干电池 siid 6 排在锂电 siid 13 前面，所以**"谁 siid 小就显示谁"**，
跟主用 / 备用毫无关系。V6 Max 恰好主电池 siid 4 更小，所以一直是"对的"——纯侥幸。

## 4. 做法：按型号登记「哪几块、叫什么、谁在前」

规则写在 `core/adapters/xiaomi.py`：

```python
"xiaomi.lock.s1": ModelAdapter(
    batteries=(("13.1", "锂电池"), ("6.1003", "干电池")),
    note="…",
)
```

`catalog.apply_adapter(spec, card, adapter)` 是型号适配的唯一入口，电量拆行是它内部的
第一步（`_expand_batteries`）：按 `siid.piid` 取属性、按登记顺序排、第一行占用原
`battery` 槽位（后续 `battery_1`）。`adapter is None` 时它第一行就返回，没登记
`batteries` 的型号——**包括 V6 Max 与 t1pro**——完全不进这个分支。

**为什么不做通用判据**（这一天试过两版，都被推翻）：

| 版本 | 做法 | 为什么不行 |
|---|---|---|
| 第一版 | `_main_battery()`：从多块里**挑**唯一带 `charging-state` 的 | 只满足第 1 条；用户要求详情页两块都看 |
| 第二版 | `_expand_batteries()` 通用展开：候选 >1 就拆，唯一带 `charging-state` 的排第一，行标签取服务注释 | 判据语义错：`charging-state` 不证明"这是锂电池"（V6 Max 两块都有）；行标签依赖云端注释；**所有**双电池设备都会被拆成多行，包括那些形态不同、我没验证过的 |

第三版（当前）退回到项目一贯的规矩：**设备形态特判一律按型号**。判据只在需要它的
那一款上生效，代价是以后每台双电池设备要补一条——这是刻意的，见
`core/adapters/base.py` 顶部的分工说明。

**为什么定位写 `siid.piid` 而不是名字**：

- 属性名 `battery-level` 两块重名，没用；
- 服务注释「锂电池」是云端文案，改一个字规则就静默少一行——这正是
  `check_text_layer` 拦的事；
- `siid.piid` 是米家规格里最精确的坐标，也是写属性时的寻址方式（`PropertySpec.key`）。
  固件换了规格版本导致坐标不存在时，**那一行跳过**，宁可少一行也不把干电池的读数
  标成「锂电池」（`verify_battery_binding.py` 第 5 节两个方向都钉住了）。

**顺序是功能不是排版**：`summary_line()` 只取前两个有值的指标、且不带标签。
锂电排第一，副标题才拿到 54%；顺序反了副标题就变成干电池的 100%。

## 5. 行为

| 设备 | 副标题（有门状态读数时） | 详情页读数行 |
|---|---|---|
| 4 Pro | 已上锁 · **54%** | 门状态 / **锂电池 54%** / **干电池 100%** |
| 4 Pro（无门状态读数） | 54% · 100% | 同上 |
| V6 Max | 已上锁 · 4.1003 的值 | 门状态 / 电量 4.1003（**与改动前一致**） |
| t1pro | 已上锁 · 66% | 门状态 / 电量 66%（**与改动前一致**） |

全量扫描 49 份缓存规格：读数行有变化的**只有 4 Pro 一份**。

轮询集合是 `card.controls + card.metrics`（`store.poll_refs`），两行都在里面，
所以 4 Pro 每轮会多读一条电池属性——这是显示两块电量的必要成本。

## 6. 没做的 / 边界

- **V6 Max 明确不适配**（用户 2026-11-16：那是另一个家庭的锁，用不上）。它保持改动前
  的单行「电量」，**不再被通用逻辑顺手拆成两行**——没验证过的变化不该塞给它。
  真要适配时补一条 `batteries=(("4.1003", "电池"), ("10.1", "猫眼电池"))` 即可，
  两行会并排，副标题自然变成 `x% · y%`。
- 三块以上电池没有先例；要加就在 `batteries` 里继续加元组，行数自动跟上。
- 电量值域都是 0–100 百分比、`uint8`，不需要换算。

## 7. 怎么复验

```
.venv/Scripts/python.exe -X utf8 scripts/probe_lock_battery.py --room 示例家庭-示例-客厅 --read
.venv/Scripts/python.exe -X utf8 scripts/verify_battery_binding.py
```

探针只读（`--dry` 连读数都不发）；默认只看「示例家庭-示例-次卧」，`--all` 才列全部门锁。
`verify_battery_binding.py` 21 项：适配表只有 4 Pro 一条、合成规格两块都显示且锂电在前、
副标题首项 54%（有/无门状态两种）、**没登记的型号与 model 为空都回到单行**、
`apply_adapter(…, None)` 一个字段都不碰、规格少一块时行数减少且标签不张冠李戴、
适配层格式校验四种写错、真机规格快照三台，以及"旧的单条 `find` 在同一用例上必须漏掉
一块"（防判据永真）。
