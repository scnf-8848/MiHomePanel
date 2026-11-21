"""xiaomi（``xiaomi.*``）型号适配。

和 ``dmaker`` 一样按型号写、一台一台补，**不按品类推广**：这里每一条都是某台
设备实测出来的形态，不是"小米的锁都差不多"。
"""

from __future__ import annotations

from .base import ModelAdapter

ADAPTERS: dict[str, ModelAdapter] = {
    # Xiaomi 中枢网关（2026-11-07 实测取码，2026-11-16 补白名单）：
    #   POST miIO.get_autowebconfig_url     → http://<网关 IP>（不耗码）
    #   POST miIO.get_central_link_passcode → 6 位码，用一次即失效
    # 这项能力**不在 MIoT 规格里**，规格里只有网络 / 指示灯 / 虚拟事件，所以
    # 只能按型号登记。以前是给每台设备发一条探测去问，43 条请求才换来这一台
    # 显示入口；现在命中白名单就零请求，开机第一帧就是极客版小卡。
    "xiaomi.gateway.hub1": ModelAdapter(
        geek=True,
        device="Xiaomi 中枢网关",
        doc="docs/reference/mijia-geek-login-code.md",
        note="极客版入口（2×1 / 1×1 两种卡）只靠这条白名单出现，不再逐台探测",
    ),
    # 小米智能门锁 4 Pro（2026-11-16 真机只读探测，房间 示例-客厅）：
    #   siid 6   6.1003 battery-level  干电池（备用）      实测 100%
    #   siid 13  13.1   battery-level  锂电池（主用）      实测 54%
    #   siid 13  13.2   charging-state 锂电池可充电；干电池服务里没有这条
    # 两块电量**属性名与服务名完全一样**，``find`` 按 siid 升序只能拿到干电池
    # （siid 6 更小），于是卡片一直显示备用的 100%。米家 App 是两块并排显示的，
    # 用户要求详情页也两块都看（"备用电池不意味着不需要查看"），副标题首要看锂电。
    #
    # 顺序 = 显示顺序，第一条进卡片副标题（``summary_line`` 只取前两个有值的指标
    # 且不带标签）。所以锂电必须写在前——这是功能，不是排版偏好。
    "xiaomi.lock.s1": ModelAdapter(
        batteries=(("13.1", "锂电池"), ("6.1003", "干电池")),
        device="小米智能门锁 4 Pro",
        doc="docs/reference/lock-dual-battery.md",
        note="两块电池属性/服务同名；锂电 siid 13 更靠后被干电池 6 抢走，按 siid.piid 显式拆行",
    ),
}

__all__ = ["ADAPTERS"]
