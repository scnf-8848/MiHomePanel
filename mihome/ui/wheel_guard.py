"""滚轮只滚页面，不改设备参数。

项目约定（详见 docs/dev/ui.md「交互约定：滚轮只滚页面」）：鼠标停在任何设备控件上
滚动，都只应该滚动外层页面，不能顺手把亮度 / 色温 / 温度 / 枚举值改掉。

做法只有一条：``wheelEvent`` 里 ``event.ignore()``，不调 ``super()``、不
``accept()``。事件被忽略后 Qt 会继续往父级投递，最终落在 ``QScrollArea`` 上。

* 自定义控件 → 混入 ``WheelSafe``；
* 必须用 Qt 原生控件 → 用这里的 ``SafeSlider``，或在子类里覆写 ``wheelEvent``。

纯滚动容器（``QScrollArea``）、列表 / 树 / 侧栏导航、下拉框展开后的选项列表
不在此列：它们滚的是"看哪儿"，不是"设备状态"。
"""

from __future__ import annotations

from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QSlider


class WheelSafe:
    """混入后滚轮一律放过，交给父级滚动区。"""

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        event.ignore()


class SafeSlider(WheelSafe, QSlider):
    """数值滑条：点击 / 拖动照常调值，滚轮只滚页面。"""


__all__ = ["SafeSlider", "WheelSafe"]
