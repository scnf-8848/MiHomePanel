# SPDX-License-Identifier: GPL-3.0-or-later
"""耗材监控的数据模型：一条监控项 = 一台设备的一个可读属性 + 两档阈值。

卡片样式**不在这条记录里**：整页共用一个（``Prefs.consumable_style``），
所以换样式是改一个标量，不用遍历全部监控项（2026-11-18 用户定）。

为什么单独一层：界面（卡片 / 配置弹窗 / 侧栏圆点）和轮询都要读同一份定义，
而阈值判定一旦在两处各写一遍，就会出现"卡片红了、圆点不红"这种事。
这里只放**纯数据与纯函数**，不 import Qt，也不认识 ``Store``。

三条刻意的选择：

1. **不按型号预设"哪些属性算耗材"**。用户手动选属性（见 ``TODO.md`` 的原始规划），
   所以这里没有任何品类 / 型号判断，换一台没见过的设备也能加。
2. **阈值只在"能比大小"的属性上开放**。``fill_percent`` 拿不到值域就退化成
   "把读数当百分数"（夹到 0–100），拿不到数值就返回 ``None``，界面只显示读数、
   不参与变色与计数——枚举 / 布尔 / 文本不该硬套两个数字阈值。
3. **两档可只开一档**。``crit`` 为空表示只有轻提醒；两档都空表示只显示读数。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

#: 卡片样式的三个 id。**只做词法校验**，真正怎么画由界面决定：
#:   bar  —— 卡片里一条横条（底部轨道不动，填充按比例缩短）
#:   fill —— 整张卡片当轨道，从左往右按比例染色
#:   ring —— 圆环，圆心放百分比（2026-11-18 用户定）
#:
#: 样式是**整页一个**（``Prefs.consumable_style``），不跟单条监控项走（2026-11-18
#: 用户定）：它表达的是"这页怎么读"，一页里多种画法只会让人以为数据来源不同。
STYLE_BAR = "bar"
STYLE_FILL = "fill"
STYLE_RING = "ring"
STYLES = (STYLE_BAR, STYLE_FILL, STYLE_RING)
STYLE_DEFAULT = STYLE_BAR

#: 两档阈值的默认值（百分数）。用户可在配置弹窗里逐条改，也可以关掉某一档。
WARN_DEFAULT = 20
CRIT_DEFAULT = 10
#: 阈值允许的范围。0 会让"降到 0 才提醒"变成永远不提醒，所以从 1 起。
THRESHOLD_MIN = 1
THRESHOLD_MAX = 100

#: 提醒档位：0 正常 / 1 轻提醒（橙） / 2 重提醒（红）
LEVEL_NONE = 0
LEVEL_WARN = 1
LEVEL_CRIT = 2

#: 还没选属性的"空壳"：``siid`` / ``piid`` 都是 0。
#:
#: 为什么允许半成品：添加分两步（2026-11-18 用户定）——先选设备、墙上落一张空卡，
#: 用户点那张卡再挑属性。空壳的 id 固定是 ``did:0.0``，所以**一台设备只可能有一张**
#: 空卡，重复添加天然被去重；选好属性后整条换成正常 id（``Prefs.reconfigure_consumable``）。
KEY_UNSET = 0


def _clamp_threshold(value: Any) -> int | None:
    """把落盘的阈值收敛成 ``None``（这一档没开）或 ``THRESHOLD_MIN..MAX``。

    坏数据一律当"没设"处理：错的阈值只会让人误以为要被提醒，或者反过来
    该提醒时不提醒，两种都不如"只显示读数"诚实。
    """
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < THRESHOLD_MIN:
        return None
    return min(number, THRESHOLD_MAX)


@dataclass(frozen=True, slots=True)
class Consumable:
    """一条监控项。``id`` 由 ``did + siid.piid`` 拼成，换标签 / 改阈值都不变。"""

    did: str
    siid: int
    piid: int
    #: 卡片与详情页上的名字。空串 = 用属性自己的中文名（``PropertySpec.display_label``），
    #: 不把属性名抄进落盘文件——云端改了注释，卡片跟着改比留个过时名字好。
    label: str = ""
    warn: int | None = WARN_DEFAULT
    crit: int | None = CRIT_DEFAULT

    @property
    def id(self) -> str:
        return f"{self.did}:{self.siid}.{self.piid}"

    @property
    def is_configured(self) -> bool:
        """选过属性没有。未配置的条目不上墙的血条、不进轮询、不计数。"""
        return self.siid > 0 and self.piid > 0

    @property
    def method(self) -> str:
        return f"{self.siid}.{self.piid}"

    @property
    def has_thresholds(self) -> bool:
        return self.warn is not None or self.crit is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "did": self.did,
            "siid": self.siid,
            "piid": self.piid,
            "label": self.label,
            "warn": self.warn,
            "crit": self.crit,
        }

    def with_changes(self, **changes: Any) -> Consumable:
        """改一份副本。``replace`` 放在这里，界面层就不用 import dataclasses。"""
        return replace(self, **changes)


def parse(value: Any) -> Consumable | None:
    """从落盘 / 界面来的字典造一条监控项。任何关键字段不对就返回 ``None``。

    ``did`` 为空说明上游数据坏了；``siid`` / ``piid`` 要么都为正（正常条目），要么
    都是 0（还没选属性的空壳）。只填了一半的当坏数据丢掉——那种条目既不能读值，
    也不该在墙上占一格。
    """
    if not isinstance(value, dict):
        return None
    did = str(value.get("did") or "").strip()
    if not did:
        return None
    try:
        siid = int(value.get("siid"))
        piid = int(value.get("piid"))
    except (TypeError, ValueError):
        return None
    if siid < 0 or piid < 0 or (siid == 0) != (piid == 0):
        return None
    warn = _clamp_threshold(value.get("warn"))
    crit = _clamp_threshold(value.get("crit"))
    if siid == 0:
        # 空壳不判档：带着阈值落盘只会让人以为"已经设好了"
        return Consumable(did=did, siid=KEY_UNSET, piid=KEY_UNSET,
                          label=str(value.get("label") or "").strip())
    # 两档都开时保证"红比橙更严"：用户把 10/20 填反了也按"橙 20 / 红 10"理解，
    # 否则会出现"变红的门槛比变橙还高"，永远到不了红色
    if warn is not None and crit is not None and crit >= warn:
        warn, crit = max(warn, crit), min(warn, crit)
        if crit >= warn:  # 两档填的是同一个数：红那档没有意义，去掉
            crit = None if warn <= THRESHOLD_MIN else warn - 1
    return Consumable(
        did=did,
        siid=siid,
        piid=piid,
        label=str(value.get("label") or "").strip(),
        warn=warn,
        crit=crit,
    )


def unset(did: str) -> Consumable:
    """造一条"还没选属性"的空壳：只记设备，两个阈值都关掉（配好属性时再给默认值）。"""
    return Consumable(did=did, siid=KEY_UNSET, piid=KEY_UNSET, warn=None, crit=None)


# ---------------------------------------------------------------------------
# 云端耗材台账（只当候选清单，不自动落卡）
# ---------------------------------------------------------------------------


def parse_prop(prop: Any) -> tuple[int, int] | None:
    """云端耗材条目的 ``prop`` → ``(siid, piid)``。对不上返回 ``None``。

    实测绝大多数是 ``prop.6.1`` / ``prop.2.1003`` 这种"siid.piid"直写；老门锁
    ``loock.lock.t1pro`` 还有两种写法：``prop.electricity``（按名字）与 ``prop.4106``
    （疑似 siid+piid 拼接，但规格里 siid 4 只有 ``4.1``）。**这两种不猜**——猜错了会把
    监控项指到另一个量上，比"不显示"糟得多。见
    ``docs/reference/mijia-api-ecosystem-research.md`` §3.4。
    """
    if not isinstance(prop, str) or not prop.startswith("prop."):
        return None
    head, sep, tail = prop[5:].partition(".")
    if not sep or not head.isdigit() or not tail.isdigit():
        return None
    siid, piid = int(head), int(tail)
    if siid < 1 or piid < 1:
        return None
    return siid, piid


def _cloud_threshold(raw: Any) -> int | None:
    """云端 ``inadeq`` / ``exhaust`` → 阈值。只收百分比。

    这两个字段是 **JSON 字符串**（不是对象）：``{"val":"20","unit":"percentage",
    "type":"range"}``。洗衣机洗衣液是 ``{"val":"1","type":"list"}``——那是档位不是
    百分比，套两个数字阈值会变成"洗衣液剩 1 档才提醒"，所以只认 ``unit == percentage``。
    ``val`` 为 0 时的结果与手工配置一致：这一档不启用（见 ``_clamp_threshold``）。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError:
            return None
    if not isinstance(raw, dict) or raw.get("unit") != "percentage":
        return None
    return _clamp_threshold(raw.get("val"))


@dataclass(frozen=True, slots=True)
class CloudItem:
    """云端耗材台账里的一条。

    和 :class:`Consumable` 的关系：这条**不是用户配的**，是米家 App「耗材」页的数据。
    只当候选清单——用户点它才经 :meth:`consumable` 转成监控项落盘（2026-11-24 用户
    选定的形态）。**别拿它直接替换耗材页的数据源**：只有百分比、没有时间序列，
    还会丢掉用户自定义的阈值与排序。
    """

    did: str
    siid: int
    piid: int
    #: 云端 description：比规格属性名细得多（同一块 ``battery-level`` 会写「5号电池」
    #: 「锂电池」「小电池」「大电池」，洗衣机写「洗衣液」）。
    label: str = ""
    #: 剩余量，**字符串**百分比（``"17"``）。空串 = 这次没有读数（``state`` 2/3 那两组）。
    value: str = ""
    warn: int | None = None
    crit: int | None = None

    @property
    def key(self) -> tuple[int, int]:
        return self.siid, self.piid

    @property
    def method(self) -> str:
        return f"{self.siid}.{self.piid}"

    def consumable(self) -> Consumable:
        """转成一条可直接落盘的监控项（名称与阈值都带上，用户可再改）。"""
        return Consumable(
            did=self.did, siid=self.siid, piid=self.piid,
            label=self.label, warn=self.warn, crit=self.crit,
        )

    def summary(self) -> str:
        """一行「滤芯 · 剩余 17% · 不足 5%」，给候选按钮用。"""
        parts = [self.label or self.method]
        if self.value:
            parts.append(f"剩余 {self.value}%")
        if self.warn is not None:
            parts.append(f"不足 {self.warn}%")
        return " · ".join(parts)


def cloud_items(payload: Any) -> dict[str, list[CloudItem]]:
    """云端耗材清单 → ``{did: [CloudItem, ...]}``。

    ``payload`` 是 ``Client.consumable_items()`` 的返回（``{home_id: 原始 payload}``）。

    **必须遍历所有 ``items`` 分组**：云端按 ``state`` 把条目分成几组（本机实测 1/2/3
    三组，2/3 组的 ``value`` 是空串），上游 mijiaAPI 只取 ``items[0]``，会漏掉另外两组。
    ``details`` 是**列表**（单电池 1 条、门锁双电池 2 条）。同一 ``(siid, piid)`` 只留一条，
    免得候选里出现两个一样的按钮。
    """
    out: dict[str, list[CloudItem]] = {}
    if not isinstance(payload, dict):
        return out
    for ret in payload.values():
        if not isinstance(ret, dict):
            continue
        for group in ret.get("items") or []:
            if not isinstance(group, dict):
                continue
            for row in group.get("consumes_data") or []:
                if not isinstance(row, dict):
                    continue
                did = str(row.get("did") or "")
                if not did:
                    continue
                details = row.get("details")
                if isinstance(details, dict):   # 上游会把"只有一条"的拆成对象
                    details = [details]
                if not isinstance(details, list):
                    continue
                for detail in details:
                    if not isinstance(detail, dict):
                        continue
                    key = parse_prop(detail.get("prop"))
                    if key is None:
                        continue
                    rows = out.setdefault(did, [])
                    if any(c.key == key for c in rows):
                        continue
                    rows.append(
                        CloudItem(
                            did=did,
                            siid=key[0],
                            piid=key[1],
                            label=str(detail.get("description") or "").strip(),
                            value=str(detail.get("value") or "").strip(),
                            warn=_cloud_threshold(detail.get("inadeq")),
                            crit=_cloud_threshold(detail.get("exhaust")),
                        )
                    )
    return out



def fill_percent(value: Any, span: tuple[float, float] | None) -> float | None:
    """把读数换算成 0–100 的余量。拿不到数值就返回 ``None``。

    ``span`` 是属性的值域 ``(lo, hi)``（``PropertySpec.value_range`` 的前两项）。
    没有值域时**把读数当百分数**：电量 / 滤芯这类云端统一给 0–100，这条兜底
    覆盖了绝大多数真实属性；真给了别的量纲（比如 0–1000 的寿命）会显示 100%，
    用户看到不对自然会去别处看——比"整页空白"好排查。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    lo, hi = span if span else (0.0, 100.0)
    if hi <= lo:
        lo, hi = 0.0, 100.0
    ratio = (number - lo) / (hi - lo)
    return max(0.0, min(100.0, ratio * 100.0))


def level_of(percent: float | None, warn: int | None, crit: int | None) -> int:
    """按余量判档。**低到阈值就算**（``<=``）：写 20 就是"降到 20% 时提醒"。

    两档都没设 → 永远是 ``LEVEL_NONE``（只显示读数，不变色、不计数）。
    """
    if percent is None:
        return LEVEL_NONE
    if crit is not None and percent <= crit:
        return LEVEL_CRIT
    if warn is not None and percent <= warn:
        return LEVEL_WARN
    return LEVEL_NONE


def level_color_key(level: int) -> str:
    """档位 → 语义色名。界面按自己的调色板取具体色值，core 不碰颜色。"""
    return {LEVEL_CRIT: "crit", LEVEL_WARN: "warn"}.get(level, "ok")


__all__ = [
    "CRIT_DEFAULT",
    "CloudItem",
    "Consumable",
    "KEY_UNSET",
    "LEVEL_CRIT",
    "LEVEL_NONE",
    "LEVEL_WARN",
    "STYLES",
    "STYLE_BAR",
    "STYLE_DEFAULT",
    "STYLE_FILL",
    "THRESHOLD_MAX",
    "THRESHOLD_MIN",
    "WARN_DEFAULT",
    "cloud_items",
    "fill_percent",
    "level_color_key",
    "level_of",
    "parse",
    "parse_prop",
    "unset",
]
