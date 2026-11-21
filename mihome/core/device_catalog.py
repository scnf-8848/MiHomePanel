# SPDX-License-Identifier: GPL-3.0-or-later
"""设备档案：这台设备曾经是什么型号、什么规格、什么固件，以及什么时候变过。

**为什么要有它**（2026-11-21）：固件是这个项目里唯一"代码推不出来、事后也查不到"
的信息。型号级行为明确绑定固件语义——``adapters/dmaker.py`` 那种"两个属性在规格里
同时存在、都可写，谁生效取决于固件"就是活例子。一旦哪天某台设备"某一行突然不见了"
或"某个控件点不动"，第一件要问的事就是"它最近更没更新过固件"；现在这个答案完全
没有留痕，只能靠感觉。中枢网关 2026-11-21 更新到 ``3.5.1_0005`` 时就是这样：手里
没有任何历史基线，只能靠官方更新说明反推。

**成本是零**：``api.Client.load_topology()`` 启动时本来就调 ``get_devices_list()``，
64 台设备的完整记录就在手边，只是挑 11 个字段建树之后把 ``extra`` 整个丢了。
档案直接吃这份原始数据，**不发任何额外请求、不读任何属性**。

**只记硬件事实**：model / pid / spec_type / fw / mcu / ble。不记离线时长（每次启动
都能重算，且真正的在线判定另有来源）、不记房间（那是 ``prefs`` 的排序偏好）、
不记属性值（那是 1403 条可读属性的时序库，成本高一个量级，和耗材监控项也重叠）。

两条铁律
--------
**1. 白名单取字段，绝不整体 dump。** ``get_devices_list()`` 的原始字典里有
``token`` / ``mac`` / ``bssid`` / ``longitude`` / ``latitude`` / ``internet_ip`` /
``uid``。整体落盘就是一次凭据与位置的泄露，而这些东西对"追踪固件变化"零价值。
所以这里只有 :func:`from_cloud` 一张白名单，新增字段必须手动加进去。

**2. "这次没取到" 不等于 "变了"。** 64 台里有 10 台的 ``extra`` 根本没有
``fw_version``（领普 / 紫米那类蓝牙子设备，云端就不给）。所以对比时**新值为空一律
跳过**——宁可漏记一次"固件版本变得不可读了"，也不能把每台蓝牙开关都记成"固件被
清空了"，那样变更流会被噪音淹掉，等于没记。

文件
----
``<data>/devices/catalog.json``
    当前快照，``did → 档案``。整份重写（原子替换），语义是"现在有哪些设备"。

``<data>/devices/changes.jsonl``
    只追加的变更流，一行一个事件。用 jsonl 而不是一个大数组，是为了让任何一次写入
    失败**最多丢最后一行**，不会毁掉已有历史；也让 ``grep`` 能直接用。

时间一律存 epoch 秒，显示时才格式化——存本地时间字符串会在跨时区 / 夏令时上出岔子。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from . import applog, paths

#: 快照文件版本。结构变化时加一，``load()`` 里按版本决定是迁移还是重来。
#: 目前只有 1，没有迁移分支——第一版还没发布过，不需要兼容谁。
VERSION = 1

#: 参与比对的字段，顺序即变更流里的记录顺序。
#: ``name`` 故意不在里面：它是"当时的标签"，改个备注不该产生一条硬件变更事件。
TRACKED: tuple[str, ...] = ("fw", "mcu", "ble", "spec_type", "model")

#: 字段的技术名 → 人读的名字，只用于日志和查询脚本的输出。
FIELD_LABEL = {
    "fw": "固件",
    "mcu": "MCU 固件",
    "ble": "蓝牙固件",
    "spec_type": "规格",
    "model": "型号",
}

#: 一次采集少掉多少台就认为"这次数据不完整"，不记移除。
#: 判据是**连上次的一半都不到**：正常删设备不会一次删掉一半（64 台里删 30 台）。
#: 云端返回被截断、翻页中途失败、账号被切到别的家庭，都会表现成"设备少了一大截"。
#: 命中时只记一条 ``suspect``，快照保留旧记录，等下次采集正常了再判断。
SUSPECT_RATIO = 0.5

#: 设备总数少于这个数时不启用上面的比例判断——3 台变 1 台也满足"不到一半"，
#: 但那是正常的删设备，不能因为样本太小就把所有移除都吞掉。
SUSPECT_MIN_TOTAL = 8


def _text(value: Any) -> str:
    """云端字段 → 落盘用的字符串。``None`` 和空串在这里合并：都表示"没有值"。"""
    if value is None:
        return ""
    return str(value).strip()


def from_cloud(dev: Mapping[str, Any]) -> dict[str, str] | None:
    """从 ``get_devices_list()`` 的一条原始记录里取出口径内的字段。

    **这是唯一的入口，也是唯一的白名单**。返回值里出现的键就是会落盘的键；
    想加字段必须改这里，而不是在别处往字典里塞。
    """
    did = _text(dev.get("did"))
    if not did:
        return None
    extra = dev.get("extra")
    if not isinstance(extra, Mapping):
        extra = {}
    return {
        "did": did,
        "name": _text(dev.get("name")),
        "model": _text(dev.get("model")),
        "pid": _text(dev.get("pid")),
        "spec_type": _text(dev.get("spec_type")),
        # 三条独立的版本线，不是所有设备都有：主固件 54/64、MCU 16/64、蓝牙 2/64
        "fw": _text(extra.get("fw_version")),
        "mcu": _text(extra.get("mcu_version")),
        "ble": _text(extra.get("ble_fw_ver")),
    }


class Catalog:
    """``catalog.json`` + ``changes.jsonl`` 的读写。

    不用模块级全局状态，测试与验证脚本可以指向临时目录（``root=``）。
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else paths.devices_dir()
        self.snapshot_path = self.root / "catalog.json"
        self.changes_path = self.root / "changes.jsonl"

    # -- 读 --------------------------------------------------------------
    def load(self) -> dict[str, dict[str, Any]]:
        """读当前快照。文件不存在 / 读不出来时返回空字典。

        坏 JSON 会被改名成 ``catalog.json.broken-<时间>`` 留档，而不是就地覆盖——
        和 ``prefs.py`` 一个口径：读不懂的东西先留住，别让下一次写入把它抹掉。
        宁可多一个奇怪的文件，也不要"用户的设备历史无声消失"。
        """
        try:
            raw = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            self._quarantine(exc)
            return {}
        if not isinstance(raw, Mapping):
            self._quarantine(ValueError("顶层不是对象"))
            return {}
        if raw.get("version") != VERSION:
            # 没有历史版本要兼容：结构一改就整体重来，旧文件留档。
            self._quarantine(ValueError(f"版本 {raw.get('version')} != {VERSION}"))
            return {}
        devices = raw.get("devices")
        if not isinstance(devices, Mapping):
            return {}
        return {str(k): dict(v) for k, v in devices.items() if isinstance(v, Mapping)}

    def changes(self, limit: int | None = None) -> list[dict[str, Any]]:
        """读变更流，**旧 → 新**。坏行跳过（一行坏了不该让整份历史不可读）。"""
        out: list[dict[str, Any]] = []
        try:
            lines = self.changes_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return out
        except OSError as exc:
            applog.log("device", f"读变更流失败：{exc}")
            return out
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                out.append(item)
        if limit is not None and limit > 0:
            return out[-limit:]
        return out

    # -- 写 --------------------------------------------------------------
    def _quarantine(self, exc: BaseException) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = self.snapshot_path.with_name(f"catalog.json.broken-{stamp}")
        try:
            self.snapshot_path.replace(target)
            applog.log("device", f"快照读不出来（{exc}），已改名留档：{target.name}")
        except OSError as move_exc:
            applog.log("device", f"快照读不出来（{exc}），改名也失败：{move_exc}")

    def _write_snapshot(self, devices: Mapping[str, Mapping[str, Any]], now: int) -> None:
        payload = {"version": VERSION, "updated": now, "devices": devices}
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.snapshot_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            tmp.replace(self.snapshot_path)
        except OSError as exc:
            # 档案是排障用的，写不进去绝不能把拓扑加载带崩（和 applog 同一原则）。
            applog.log("device", f"快照写入失败：{exc}")

    def _append(self, events: list[dict[str, Any]]) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.changes_path.open("a", encoding="utf-8") as fh:
                for ev in events:
                    fh.write(json.dumps(ev, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError as exc:
            applog.log("device", f"变更流追加失败：{exc}")

    # -- 核心 ------------------------------------------------------------
    def observe(self, devices: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """采集一次，必要时写盘；返回本次产生的事件（供日志 / 验证使用）。

        调用方是 :meth:`api.Client.load_topology`，一次启动一次（手动刷新拓扑也会
        再走一次）。没有变化时不写变更流，但**总会刷新快照里的 ``last_seen``**，
        这样"最后一次确认它还在"这个时间也是准的。
        """
        current: dict[str, dict[str, str]] = {}
        for dev in devices:
            item = from_cloud(dev)
            if item is None:
                continue
            did = item.pop("did")
            current[did] = item
        if not current:
            # 一台都没取到 = 这次调用没拿到数据，绝不能理解成"设备全删了"。
            return []

        previous = self.load()
        now = int(time.time())
        events: list[dict[str, Any]] = []

        for did, rec in current.items():
            old = previous.get(did)
            if old is None:
                events.append(
                    {
                        "at": now,
                        "kind": "added",
                        "did": did,
                        "name": rec["name"],
                        "model": rec["model"],
                        "spec_type": rec["spec_type"],
                        "fw": rec["fw"],
                    }
                )
                continue
            for field in TRACKED:
                before = _text(old.get(field))
                after = rec.get(field) or ""
                if not after:
                    # 铁律 2：这次没取到 ≠ 变了。见模块文档。
                    continue
                if before != after:
                    events.append(
                        {
                            "at": now,
                            "kind": "changed",
                            "did": did,
                            "name": rec["name"],
                            "model": rec["model"],
                            "field": field,
                            "old": before,
                            "new": after,
                        }
                    )

        missing = [did for did in previous if did not in current]
        suspect = bool(missing) and (
            len(previous) >= SUSPECT_MIN_TOTAL and len(current) < len(previous) * SUSPECT_RATIO
        )
        if suspect:
            events.append(
                {
                    "at": now,
                    "kind": "suspect",
                    "before": len(previous),
                    "after": len(current),
                    "missing": len(missing),
                }
            )
        else:
            for did in missing:
                old = previous[did]
                events.append(
                    {
                        "at": now,
                        "kind": "removed",
                        "did": did,
                        "name": _text(old.get("name")),
                        "model": _text(old.get("model")),
                    }
                )

        # 快照：正常情况就是这次的清单（缺失的自然消失）；可疑时把旧记录合进来，
        # 免得一次不完整采集把十年档案削掉一半。
        merged: dict[str, dict[str, Any]] = dict(previous) if suspect else {}
        for did, rec in current.items():
            prev = previous.get(did, {})
            # 这次没取到的字段沿用上一次已知值：快照的语义是"最后一次**确认过**的值"，
            # 不是"这次返回了什么"。否则一台设备偶发不报 fw_version，档案里的固件
            # 就被擦成空，下次恢复时又会记一条"（无）→ x"，凭空制造一次变更。
            merged[did] = {
                **{k: (v if v else _text(prev.get(k))) for k, v in rec.items()},
                "first_seen": int(prev.get("first_seen") or now),
                "last_seen": now,
            }

        self._write_snapshot(merged, now)
        if events:
            self._append(events)
            applog.log("device", f"采集 {len(current)} 台，产生 {len(events)} 条变更")
        return events


_default: Catalog | None = None


def observe(devices: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """模块级入口，用默认目录；出错一律吞掉，绝不向上抛。

    调用点在拓扑加载的主路径上，档案坏了不能变成"程序打不开"。
    """
    global _default
    try:
        if _default is None:
            _default = Catalog()
        return _default.observe(devices)
    except Exception as exc:  # noqa: BLE001 - 主路径上的旁路功能，必须全吞
        applog.log("device", f"设备档案采集异常：{type(exc).__name__}: {exc}")
        return []
