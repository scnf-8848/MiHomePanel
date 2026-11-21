# SPDX-License-Identifier: GPL-3.0-or-later
"""只读探测脚本：把米家云端返回的真实结构导出成样本。

只调用读接口（家庭列表 / 设备列表 / 属性读取），不发任何写指令。
输出：scripts/out/cloud_snapshot.json（原始结构，含 did，仅本机使用）
      scripts/out/props_sample.json（一小批设备的最新属性读数）

用法：.venv/Scripts/python.exe -X utf8 scripts/probe_cloud.py
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from mijiaAPI import get_device_info, mijiaAPI

from mihome.core.api import apply_locale

OUT = Path(__file__).resolve().parent / "out"


def dump(name: str, data) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  -> 已写入 {path}")


def main() -> None:
    t0 = time.time()
    api = mijiaAPI()
    apply_locale(api)  # 与主程序一致：不带这一句，导出的设备名是英文默认名
    print(f"认证可用: {api.available}  (auth: {api.auth_data_path})")

    print("\n[1] 家庭与房间")
    homes = api.get_homes_list()
    print(f"  家庭数: {len(homes)}")
    for h in homes:
        rooms = h.get("roomlist") or []
        print(f"  - {h.get('name')!r} id={h.get('id')} uid={h.get('uid')} 房间={len(rooms)}")
        for r in rooms:
            print(f"      {r.get('name')!r}  dids={len(r.get('dids') or [])}  "
                  f"keys={sorted(k for k in r if k != 'dids')}")

    print("\n[2] 设备列表")
    devices = api.get_devices_list() + api.get_shared_devices_list()
    print(f"  设备数: {len(devices)}（共享 {len(api.get_shared_devices_list())}）")

    loc: dict[str, tuple[str, str]] = {}
    for h in homes:
        for r in h.get("roomlist") or []:
            for did in r.get("dids") or []:
                loc[str(did)] = (h.get("name", ""), r.get("name", ""))

    rows = []
    for d in devices:
        did = str(d["did"])
        home, room = loc.get(did, ("未知", "未知"))
        rows.append({
            "did": did, "name": d.get("name"), "model": d.get("model"),
            "spec_type": d.get("spec_type") or "", "online": bool(d.get("isOnline")),
            "home": home, "room": room,
        })
    rows.sort(key=lambda x: (x["home"], x["room"], x["name"] or ""))
    print(f"  {'设备':<18}{'房间':<10}{'型号':<34}在线  spec")
    for r in rows:
        print(f"  {(r['name'] or '')[:17]:<18}{(r['room'] or '')[:9]:<10}"
              f"{(r['model'] or '')[:33]:<34}{'是' if r['online'] else '否':<4}"
              f"{'有' if r['spec_type'] else '无'}")

    print("\n[3] 设备能力（读本地缓存，缺的从 miot-spec.org 拉）")
    cache_dir = api.auth_data_path.parent
    spec_map: dict[str, dict] = {}
    for st in {r["spec_type"] for r in rows if r["spec_type"]}:
        try:
            spec_map[st] = get_device_info(st, cache_path=cache_dir)
        except Exception as exc:  # noqa: BLE001
            print(f"  规格拉取失败 {st}: {exc}")
    for r in rows:
        spec = spec_map.get(r["spec_type"])
        if spec:
            r["spec_name"] = spec.get("name")
            r["spec_props"] = [p.get("name") for p in spec.get("properties") or []]
            r["spec_actions"] = [a.get("name") for a in spec.get("actions") or []]
    print(f"  已解析规格: {len(spec_map)}")

    print("\n[4] 属性读数采样（只读，批量 20 条/请求）")
    queries, owners = [], []
    for r in rows:
        spec = spec_map.get(r["spec_type"])
        if not spec or not r["online"]:
            continue
        by_name = {p.get("name"): p for p in spec.get("properties") or []}
        for name in ("on", "brightness", "color-temperature", "temperature",
                     "relative-humidity", "power", "battery-level"):
            prop = by_name.get(name)
            if prop and "r" in prop.get("rw", ""):
                queries.append({"did": r["did"], **prop["method"]})
                owners.append((r["did"], name))

    readings: dict[str, dict] = {}
    for start in range(0, len(queries), 20):
        batch = queries[start:start + 20]
        try:
            rets = api.get_devices_prop(batch)
        except Exception as exc:  # noqa: BLE001
            print(f"  批量读取失败: {exc}")
            break
        print(f"  批次 {start // 20 + 1}: 请求 {len(batch)} 条 -> 响应 {len(rets)} 条")
        if rets:
            print(f"    响应样例: {json.dumps(rets[0], ensure_ascii=False)}")
        for item in rets:
            key = (str(item.get("did")), item.get("siid"), item.get("piid"))
            for (did, name), q in zip(owners, queries):
                if (q["did"], q["siid"], q["piid"]) == key:
                    readings.setdefault(did, {})[name] = {
                        "value": item.get("value"), "code": item.get("code"),
                        "updateTime": item.get("updateTime"),
                    }
                    break

    for did, vals in list(readings.items())[:8]:
        name = next((r["name"] for r in rows if r["did"] == did), did)
        print(f"  {name}: {json.dumps(vals, ensure_ascii=False)}")

    dump("cloud_snapshot.json", {"homes": homes, "devices": rows})
    dump("props_sample.json", readings)
    print(f"\n完成，用时 {time.time() - t0:.1f}s")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    main()
