# SPDX-License-Identifier: GPL-3.0-or-later
"""验证设备档案：白名单、变更判定、采集不完整时的保护、坏文件留档。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_device_catalog.py``

钉住的是四条**会悄悄出错**的规则，每一条出错都不会报异常：

1. **白名单**——``get_devices_list()`` 的原始记录里有 token / mac / bssid / 经纬度 /
   internet_ip / uid。档案落盘文件里出现任何一个都是事故，所以在假数据里塞满这些
   字段，再搜落盘文件。
2. **"这次没取到" ≠ "变了"**——10/64 台设备的 ``extra`` 没有 ``fw_version``。
   如果把它当成"固件被清空"，变更流会被每台蓝牙开关的噪音淹掉。
3. **采集不完整不算删设备**——云端返回被截断时会表现成"少了一大半"。这种情况下
   绝不能把移除写进变更流，也不能让快照被削掉一半。
4. **坏文件留档不覆盖**——和 prefs 一个口径：读不懂的先改名留住。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mihome.core import device_catalog  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('：' + detail) if detail else ''}")
    if not ok:
        FAILS.append(name)


SPEC = "urn:miot-spec-v2:device:switch:0000A003:linp-e3sbw3:1:0000C810"


def dev(
    did: str,
    *,
    name: str = "灯",
    model: str = "linp.switch.e3sbw3",
    fw: str | None = "3.0.2_0017",
    mcu: str | None = None,
    ble: str | None = None,
    spec: str = SPEC,
) -> dict:
    """一条云端原始记录。**故意把敏感字段塞满**——档案不该带走任何一个。"""
    extra: dict[str, str] = {}
    if fw is not None:
        extra["fw_version"] = fw
    if mcu is not None:
        extra["mcu_version"] = mcu
    if ble is not None:
        extra["ble_fw_ver"] = ble
    return {
        "did": did,
        "name": name,
        "model": model,
        "pid": 16,
        "spec_type": spec,
        "extra": extra,
        "isOnline": True,
        "token": "TOKEN-SHOULD-NOT-LEAK",
        "mac": "AA:BB:CC:DD:EE:FF",
        "bssid": "11:22:33:44:55:66",
        "localip": "LOCALIP-SHOULD-NOT-LEAK",
        "internet_ip": "INTERNET-IP-SHOULD-NOT-LEAK",
        "longitude": "LONGITUDE-SHOULD-NOT-LEAK",
        "latitude": "LATITUDE-SHOULD-NOT-LEAK",
        "uid": "UID-SHOULD-NOT-LEAK",
        "home_id": "HOME-ID-SHOULD-NOT-LEAK",
    }


def kinds(events: list[dict]) -> list[str]:
    return [str(e.get("kind")) for e in events]


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="mihome-catalog-"))
    cat = device_catalog.Catalog(root=root)

    # -- 白名单 ----------------------------------------------------------
    raw = dev("d1")
    item = device_catalog.from_cloud(raw)
    check("from_cloud 取到了 did", bool(item and item.get("did") == "d1"))
    allowed = {"did", "name", "model", "pid", "spec_type", "fw", "mcu", "ble"}
    check("from_cloud 只吐出白名单里的键", set(item or {}) == allowed, str(sorted(item or {})))
    forbidden_keys = {"token", "mac", "bssid", "localip", "internet_ip", "longitude", "latitude", "uid"}
    check("from_cloud 里没有任何敏感键", not (set(item or {}) & forbidden_keys))
    check("from_cloud 对没有 did 的记录返回 None", device_catalog.from_cloud({"name": "x"}) is None)

    # -- 首次采集：全部算新增 --------------------------------------------
    events = cat.observe([dev("d1"), dev("d2", name="插座", model="zimi.plug.dhkg01")])
    check("首次采集两台 → 两条 added", kinds(events) == ["added", "added"], str(kinds(events)))
    snap = cat.load()
    check("快照里有两条记录", len(snap) == 2, str(len(snap)))
    check(
        "快照带 first_seen / last_seen",
        bool(snap.get("d1", {}).get("first_seen")) and bool(snap.get("d1", {}).get("last_seen")),
    )
    text = (root / "catalog.json").read_text(encoding="utf-8")
    csv = (root / "changes.jsonl").read_text(encoding="utf-8")
    leaks = (
        "TOKEN-SHOULD-NOT-LEAK",
        "AA:BB:CC:DD:EE:FF",
        "11:22:33:44:55:66",
        "LOCALIP-SHOULD-NOT-LEAK",
        "INTERNET-IP-SHOULD-NOT-LEAK",
        "LONGITUDE-SHOULD-NOT-LEAK",
        "LATITUDE-SHOULD-NOT-LEAK",
        "UID-SHOULD-NOT-LEAK",
        "HOME-ID-SHOULD-NOT-LEAK",
    )
    leaked = [t for t in leaks if t in text or t in csv]
    check("落盘文件里搜不到 token / mac / ip / home_id", not leaked, str(leaked))

    # -- 没有变化：不产生事件 --------------------------------------------
    events = cat.observe([dev("d1"), dev("d2", name="插座", model="zimi.plug.dhkg01")])
    check("原样再采一次 → 零事件", events == [], str(kinds(events)))

    # -- 固件变化 --------------------------------------------------------
    events = cat.observe([dev("d1", fw="3.0.5_0021"), dev("d2", name="插座", model="zimi.plug.dhkg01")])
    check("固件变了 → 一条 changed", kinds(events) == ["changed"], str(kinds(events)))
    ev = events[0] if events else {}
    check(
        "changed 记的是 fw 字段且新旧值正确",
        ev.get("field") == "fw" and ev.get("old") == "3.0.2_0017" and ev.get("new") == "3.0.5_0021",
        str(ev),
    )

    # -- 铁律 2：新值为空不算变更 ----------------------------------------
    events = cat.observe([dev("d1", fw=None), dev("d2", name="插座", model="zimi.plug.dhkg01")])
    check("这次取不到固件 → 不记变更", events == [], str(kinds(events)))
    check(
        "取不到时快照保留旧值",
        cat.load().get("d1", {}).get("fw") == "3.0.5_0021",
        str(cat.load().get("d1", {}).get("fw")),
    )

    # -- 型号没变 --------------------------------------------------------
    cur_fw = "3.0.5_0021"  # d1 现在的固件；后面每轮都得带上，否则会多记一次 fw 变更
    events = cat.observe(
        [dev("d1", fw=cur_fw, model="linp.switch.e3sbw3"), dev("d2", name="插座", model="zimi.plug.dhkg01")]
    )
    check("型号没变 → 零事件", events == [], str(kinds(events)))

    # -- 新增 / 移除 ------------------------------------------------------
    events = cat.observe(
        [dev("d1", fw=cur_fw), dev("d2", name="插座", model="zimi.plug.dhkg01"), dev("d3", name="新加的")]
    )
    check("多一台 → 一条 added", kinds(events) == ["added"], str(kinds(events)))
    events = cat.observe([dev("d1", fw=cur_fw), dev("d2", name="插座", model="zimi.plug.dhkg01")])
    check("少一台 → 一条 removed", kinds(events) == ["removed"], str(kinds(events)))
    check("removed 之后快照里没有它", "d3" not in cat.load())

    # -- 采集不完整：不记移除，快照不缩水 --------------------------------
    many = [dev(f"m{i}", name=f"设备{i}") for i in range(20)]
    cat.observe(many)
    few = many[:5]  # 只回来四分之一，模拟返回被截断
    events = cat.observe(few)
    check("采到不足一半 → 记 suspect 而不是成片 removed", kinds(events) == ["suspect"], str(kinds(events)))
    check("suspect 之后快照没被削掉", len(cat.load()) == 20, str(len(cat.load())))

    # -- 空列表：什么都不能动 --------------------------------------------
    before = (root / "catalog.json").read_text(encoding="utf-8")
    events = cat.observe([])
    after = (root / "catalog.json").read_text(encoding="utf-8")
    check("空列表 → 零事件且不重写快照", events == [] and before == after)

    # -- 坏 JSON 留档 ----------------------------------------------------
    bad_root = Path(tempfile.mkdtemp(prefix="mihome-catalog-bad-"))
    bad = device_catalog.Catalog(root=bad_root)
    (bad_root / "catalog.json").write_text("{不是 json", encoding="utf-8")
    loaded = bad.load()
    broken = list(bad_root.glob("catalog.json.broken-*"))
    check("坏 JSON → 返回空字典", loaded == {})
    check("坏 JSON → 已改名留档", len(broken) == 1, str([p.name for p in broken]))

    # -- 变更流坏行跳过 --------------------------------------------------
    good_root = Path(tempfile.mkdtemp(prefix="mihome-catalog-lines-"))
    lines = device_catalog.Catalog(root=good_root)
    (good_root / "changes.jsonl").write_text(
        '{"kind": "added", "at": 1}\n'
        "这一行坏了\n"
        '{"kind": "changed", "at": 2}\n',
        encoding="utf-8",
    )
    got = lines.changes()
    check("变更流跳过坏行、保留好行", kinds(got) == ["added", "changed"], str(kinds(got)))
    check("limit 取的是最近几条", kinds(lines.changes(limit=1)) == ["changed"])

    # -- 版本号不认识 → 留档重来 -----------------------------------------
    ver_root = Path(tempfile.mkdtemp(prefix="mihome-catalog-ver-"))
    ver = device_catalog.Catalog(root=ver_root)
    (ver_root / "catalog.json").write_text(
        json.dumps({"version": 999, "devices": {"x": {}}}), encoding="utf-8"
    )
    check("版本不认识 → 返回空", ver.load() == {})
    check("版本不认识 → 旧文件留档", len(list(ver_root.glob("catalog.json.broken-*"))) == 1)

    if FAILS:
        print(f"\n{len(FAILS)} 条失败：" + "；".join(FAILS))
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
