"""dmaker（``dmaker.*``）型号适配。

现在只有风扇。这类设备的共同点：规格里**同时**有档位风速和连续风速两个可写
属性，谁生效取决于 ``mode``——这不是"品类差不多"，而是同一条固件约定的不同
实现，所以按型号写、一台一台补。
"""

from __future__ import annotations

from .base import ModelAdapter

ADAPTERS: dict[str, ModelAdapter] = {
    # 米家智能直流变频落地扇 1X（2026-11-12 实测，房间 1103-强，只读探测）：
    #   2.2 fan-level  enum 4 项（1–4 挡）
    #   2.3 mode       enum 2 项（0 直吹风 / 1 自然风）
    #   8.1 speed-level range 1–100，在厂家自定义服务 dm-service 里
    # 4 挡（最高档）时读到 speed-level=100 → 两者是同一件事的两种刻度，
    # 云端已经归一化成 1–100（设备端 0–1100 的说法不进代码，写 1100 会越界）。
    # 所以卡片上：直吹风给 0–100 滑条，自然风给 4 个胶囊，同一个位置按 mode 换。
    "dmaker.fan.p5c": ModelAdapter(
        speed_by_mode=("mode", {0: "fan_speed", 1: "fan_level"}),
        note="mode 0=直吹风认 speed-level(1–100)，1=自然风认 fan-level(1–4)，两者都可写",
    ),
}

__all__ = ["ADAPTERS"]
