# SPDX-License-Identifier: GPL-3.0-or-later
"""全屋温湿度：把各台设备**已有的读数**聚成一个范围，给顶栏那两颗胶囊用。

口径（2026-11-22 用户定）：

* **哪些设备** —— 只看品类自己声明的读数：``CardSpec.metrics`` 里绑到
  ``temperature`` / ``humidity`` 槽位的那两个。温湿度计、空调的室温、净化器、
  浴霸的浴室温度都算，因为它们本来就在卡片副标题上显示这两个数。
  **不按属性名满地找，也不维护品类白名单**——品类表（``core/catalog.py``）
  就是白名单，加一个新品类只要它声明了读数就自动进范围。
* **哪套房子** —— 当前家庭。温度是"这套房子此刻"，跨家庭混算没有意义
  （和旁边的「N 台设备」同一口径）。
* **多台怎么聚合** —— **最小-最大范围**，如 ``21.5-22.8°C`` / ``46-51%``；
  只有一台、或取整后两端相同就写单值。不做平均：平均会把"有一间 32 °C"
  这个最该被看见的事实抹掉。

读不到、离线、被隐藏、数值离谱（传感器抽风写 255）的样本一律不参与——
宁可显示"暂无"也不要让一颗坏读数把整条范围拉飞。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

#: 参与聚合的两个槽位，顺序即顶栏从左到右的顺序。
SLOTS: tuple[str, str] = ("temperature", "humidity")

#: 读数合理性区间。超出就当"这不是室内空气"，不参与聚合。
#: 上界 80 °C 留给浴霸 / 烤箱附近的设备，别把烤箱腔体温度当成室温；
#: 湿度 0–100 之外的一定是坏值（见过潮湿传感器报 255）。
PLAUSIBLE: dict[str, tuple[float, float]] = {
    "temperature": (-40.0, 80.0),
    "humidity": (0.0, 100.0),
}

#: 槽位 → 单位。温度用 ``°C`` 与本项目其它读数（卡片副标题 / 详情页）一致，
#: 不跟米家 App 的 ``℃`` 走——同一个程序里两种写法比抄得像更重要。
UNITS: dict[str, str] = {"temperature": "°C", "humidity": "%"}


@dataclass(frozen=True, slots=True)
class Sample:
    """一台设备贡献的一个读数。"""

    did: str
    name: str
    room: str
    value: float

    @property
    def where(self) -> str:
        """「房间 设备名」，只用于悬停提示。"""
        if self.room and self.name:
            return f"{self.room} {self.name}"
        return self.room or self.name


@dataclass(slots=True)
class Climate:
    """一套房子此刻的温湿度样本（每个槽位一条列表，按数值升序）。"""

    samples: dict[str, list[Sample]] = field(default_factory=dict)

    def get(self, slot: str) -> list[Sample]:
        return self.samples.get(slot, [])

    def text(self, slot: str) -> str:
        return span_text(self.get(slot), slot)

    def texts(self) -> tuple[str, str]:
        """(温度, 湿度) 两颗胶囊的文字，空串表示这颗该隐藏。"""
        first, second = SLOTS
        return self.text(first), self.text(second)

    def tooltip(self, slot: str) -> str:
        """悬停提示：「客厅 温湿度计 22.3°C · 主卧 空调 22.8°C」。

        超出 6 台就收尾成「… 等 N 台」——两颗胶囊的提示最多两行，
        九台空调挨个列出来没人读得完。
        """
        rows = self.get(slot)
        if not rows:
            return ""
        unit = UNITS[slot]
        dec = _decimals([s.value for s in rows]) if slot == "temperature" else 0
        head = [f"{s.where} {s.value:.{dec}f}{unit}" for s in rows[:6]]
        if len(rows) > 6:
            head.append(f"… 等 {len(rows)} 台")
        return " · ".join(head)


def span_text(samples: Iterable[Sample], slot: str) -> str:
    """样本 → 胶囊文字：单值 / 范围，空样本返回空串（胶囊据此隐藏）。

    温度一律保留 1 位小数（米家那种「21.5-22.8℃」），但如果所有样本本来就是
    整数（空调报整数室温很常见）就不补 ``.0``，写成「22-23°C」。
    湿度取整，不写小数——46% 和 46.4% 对"该不该开除湿机"没有区别。
    """
    values = [s.value for s in samples]
    if not values:
        return ""
    unit = UNITS[slot]
    dec = _decimals(values) if slot == "temperature" else 0
    lo = f"{min(values):.{dec}f}"
    hi = f"{max(values):.{dec}f}"
    if lo == hi:
        return f"{lo}{unit}"
    # 负温时用「~」当分隔符：`-5.0--3.0°C` 会被读成减号连写
    sep = "~" if min(values) < 0 else "-"
    return f"{lo}{sep}{hi}{unit}"


def _decimals(values: list[float]) -> int:
    """有任何一个数带小数就留 1 位，否则 0 位。"""
    return 1 if any(abs(v - round(v)) > 1e-9 for v in values) else 0


def _number(value: object, slot: str) -> float | None:
    """云端原始值 → 可参与聚合的浮点数；不是数、是坏值就返回 ``None``。

    ``bool`` 要单独挡：Python 里 ``True`` 是 ``int`` 的实例，不挡会让某个
    布尔属性混进温度（历史上把 ``on`` 当 1 的事踩过一次）。
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    num = float(value)
    low, high = PLAUSIBLE[slot]
    if not low <= num <= high:
        return None
    return num


def collect(
    devices: Iterable[object],
    home: str = "",
    *,
    is_hidden: Callable[[str], bool] | None = None,
) -> Climate:
    """把当前家庭里所有能报环境温湿度的设备收成一份 :class:`Climate`。

    参数是 ``DeviceState`` 的迭代（用 ``Store.devices.values()``），但只用到
    ``node`` / ``online`` / ``card`` / ``values`` 这几样，方便测试塞假对象。

    跳过四类：别的家庭的、离线的、用户隐藏的、以及卡片的指标里没绑上温度/湿度
    的（README 里"没声明的读数不猜"是同一条原则）。
    """
    out: Climate = Climate({s: [] for s in SLOTS})
    seen: set[tuple[str, str]] = set()
    for st in devices:
        node = getattr(st, "node", None)
        if node is None:
            continue
        home_id = getattr(node, "home_id", "") or ""
        if home and home_id and home_id != home:
            continue
        if not getattr(st, "online", False):
            continue
        did = getattr(node, "did", "")
        if is_hidden is not None and is_hidden(did):
            continue
        card = getattr(st, "card", None)
        values = getattr(st, "values", None)
        if card is None or not isinstance(values, dict):
            continue
        for metric in getattr(card, "metrics", ()) or ():
            slot = getattr(metric, "slot", "")
            if slot not in SLOTS:
                continue
            prop = getattr(metric, "prop", None)
            if prop is None:
                continue
            num = _number(values.get(prop.key), slot)
            if num is None:
                continue
            mark = (did, slot)
            if mark in seen:
                continue
            seen.add(mark)
            out.samples[slot].append(
                Sample(did=did, name=getattr(node, "name", ""),
                       room=getattr(node, "room", ""), value=num)
            )
    for slot in SLOTS:
        out.samples[slot].sort(key=lambda s: (s.value, s.room, s.name))
    return out
