# SPDX-License-Identifier: GPL-3.0-or-later
"""读数陈旧判定：一条读数有多旧。

依据是云端每个属性自带的 ``updateTime``（``ReadValue.changed_ago`` 用的同一个
时间戳）。实测口径（``scripts/probe_staleness.py``，2026-11-09，两轮共 320 个读点）：

- **每个属性一份**，不是整台设备一份：所以开关一次灯不会把"室温多旧"清零
- **绝大多数时间戳不随读取推进**（75/85 连读两遍不动），所以它能当"这条读数有多旧"用，
  **不能**当数据新鲜度用（新鲜度看 ``DeviceState.age``，即底部"数据更新于 N 秒前"）
- 少数属性**每次读都会刷新**（实测摄像机 10/85：宽动态范围、录制模式、时间水印、
  存储空间）——这类永远不会被判陈旧。属于保守方向（少提示），不会误报
- 静态元数据（厂商 / 型号 / 固件版本）压根不带这个字段，读出来是 0 → 不提示

因此这里只负责"这条读数有多旧"这一件事，且**默认阈值给到 3 天**：实测一个真实家庭里
1–7 天没变的读点有 63 条、>7 天 61 条，其中大部分是天生不变的量（摆风分区、
aQi 同步时间、滤芯剩余）。阈值太低会把正常读数淹没成一片提示。

文案用"**未更新**"而不是"未变化"：同一个字段在不同属性上含义并不统一（有的记"值最后
一次变化"，有的记"最后一次上报"），我们没法从外面区分"值一直没变"和"设备一直没上报"
——这两件事对用户的意义不同（前者正常，后者可能是设备/网关出问题）。所以只陈述
可证的事实：这个读数是 N 天前的。
"""

from __future__ import annotations

import time

#: 超过这个时长没变化才提示。3 天：温度/湿度/照度这类量在有人活动的房子里
#: 不会三天一点不动，而"本来就不常变"的量（滤芯、电量）提示出来也说得通。
STALE_AFTER = 3 * 86400.0

#: 超过这个天数改用"个月"量级，免得出现"253 天未更新"这种没人能读的数字。
_STALE_MONTH = 30 * 86400.0

#: 离线设备的读数超过这个时长就算**不新鲜**（2026-11-18 用户定：一天）。
#: 与 ``STALE_AFTER`` 是两回事：那个给详情页"3 天没变"打提示，这里回答的是
#: "卡片上这个数是什么时候读到的"。离线设备只会越来越旧，一天足够区分。
OFFLINE_STALE_AFTER = 86400.0


def stale_text(
    update_time: float | None,
    *,
    now: float | None = None,
    threshold: float = STALE_AFTER,
) -> str:
    """``update_time``（云端 epoch 秒）距今多久没更新 → "4 天未更新"。

    不满足条件时返回空串，调用方直接当"没有提示"用：

    - 没有时间戳（0 / ``None``，静态元数据就是这样）
    - 还没超过阈值（含时间戳在未来——设备时钟不准时只可能是这头，不提示）
    """
    if not update_time:
        return ""
    age = (time.time() if now is None else now) - float(update_time)
    if age < threshold:
        return ""
    if age >= _STALE_MONTH:
        months = int(age // _STALE_MONTH)
        return f"{months} 个月未更新"
    if age >= 86400:
        return f"{int(age // 86400)} 天未更新"
    # 阈值调到一天以内时（测试 / 将来想做"半小时没变"）用小时兜住，别显示"0 天"
    return f"{max(1, int(age // 3600))} 小时未更新"


def read_note(
    update_time: float | None,
    *,
    now: float | None = None,
) -> tuple[str, bool]:
    """离线设备卡片右下角的读数时间 → ``("3 天前更新", True)``。

    第二个返回值是"不新鲜"（超过 :data:`OFFLINE_STALE_AFTER` = 一天），卡片据此
    换颜色。**只给拿不到新读数的设备用**：在线设备每次轮询都会刷新读数，标个
    "今天更新"是废话（用户 2026-11-18 定）。

    「精确到天」是用户的原话：耗材页看的是"这个数还能不能信"，几小时几分的差别
    没有意义，也不该让人去读一串时间戳。

    - 没有时间戳 → ``("", False)``：静态元数据本来就不带 ``updateTime``，别写
      "未知"（那会被读成"出问题了"）
    - 时间戳在未来（设备时钟不准）→ 当成"今天"，不提示
    """
    if not update_time:
        return "", False
    age = (time.time() if now is None else now) - float(update_time)
    if age < OFFLINE_STALE_AFTER:
        return "今天更新", False
    if age >= _STALE_MONTH:
        return f"{int(age // _STALE_MONTH)} 个月前更新", True
    return f"{int(age // 86400)} 天前更新", True
