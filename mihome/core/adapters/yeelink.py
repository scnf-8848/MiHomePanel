"""yeelink（``yeelink.*``）型号适配。

米家显示器挂灯、部分台灯/灯带都在这个厂商下。新增型号就往 ``ADAPTERS`` 里加一条。
"""

from __future__ import annotations

from .base import ModelAdapter

ADAPTERS: dict[str, ModelAdapter] = {
    # 米家显示器挂灯 1S：``scene-mode``(3,9) 是"当前实际生效的预设"，0 = 无激活项。
    # 它会被任何亮度/色温写入清 0，所以只能读来当判据；写入仍然走 ``mode``(2,5)。
    "yeelink.light.lamp22": ModelAdapter(
        extra=("scene-mode",),
        mode_active="scene-mode",
        note="mode(2,5) 手调亮度/色温后不变，真实预设要看只读的 scene-mode(3,9)",
    ),
}

__all__ = ["ADAPTERS"]
