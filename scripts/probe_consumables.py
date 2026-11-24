# SPDX-License-Identifier: GPL-3.0-or-later
"""只读探测：``/v2/home/standard_consumable_items``（米家云端耗材清单）。

只发这一个读接口（外加家庭 / 设备列表和公开的设备规格），不写任何东西。要回答四件事：

1. 云端到底认哪些设备有耗材（覆盖率），耗材条目长什么样；
2. ``items`` 是按什么分组的（实测按 ``state`` 分三档）；
3. 条目里的 ``prop`` 能不能对到设备规格的 ``siid.piid`` —— 能对就说明可以直接接进
   现有耗材页的轮询体系；
4. ``filter_ignore`` 这个参数是否真的改变返回条数。

用法：.venv/Scripts/python.exe -X utf8 scripts/probe_consumables.py
输出：终端摘要 + scripts/out/consumable_items.json（原始结构，含 did，仅本机使用）
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from mijiaAPI import mijiaAPI

from mihome.core.api import apply_locale
from mihome.core.spec import SpecRepository

URI = "/v2/home/standard_consumable_items"
OUT = Path(__file__).resolve().parent / "out"
#: ``state`` 的实际语义云端没有文档。实测只出现 1/2/3，且 2/3 的那两条 ``value`` 为空，
#: 所以打印时按"有值 / 无值"并列出来，别当成结论。
STATE_GUESS = {1: "有读数", 2: "无读数（?）", 3: "无读数（?）"}


def dump(name: str, data) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  -> 已写入 {path}")


def ask(api: mijiaAPI, home: dict, filter_ignore: bool) -> dict:
    data = {
        "home_id": int(home["id"]),
        "owner_id": int(home["uid"]),
        "filter_ignore": filter_ignore,
    }
    try:
        return api._request(URI, data)  # noqa: SLF001  探针就是要看原始返回
    except Exception as exc:  # noqa: BLE001
        print(f"    请求失败: {type(exc).__name__}: {exc}")
        return {}


def parse_prop(prop: str) -> tuple[int, int] | None:
    """``prop.6.1`` / ``prop.2.1003`` → (siid, piid)。对不上的形式返回 None。

    实测还有两种非数字写法：``prop.electricity``（老门锁，按名字）与 ``prop.4106``
    （疑似 siid+piid 拼接）。它们只在一台老门锁上出现，先不猜，原样打出来。
    """
    if not prop.startswith("prop."):
        return None
    body = prop[5:]
    if re.fullmatch(r"\d+\.\d+", body):
        siid, piid = body.split(".")
        return int(siid), int(piid)
    return None


def threshold(raw: str) -> str:
    """``inadeq`` / ``exhaust`` 是 JSON 字符串，摘出「值 + 类型」方便看。"""
    try:
        d = json.loads(raw or "{}")
    except ValueError:
        return raw or "-"
    if not isinstance(d, dict) or not d:
        return "-"
    return f"{d.get('val')}{'%' if d.get('unit') == 'percentage' else ''}({d.get('type')})"


def main() -> None:
    t0 = time.time()
    api = mijiaAPI()
    apply_locale(api)
    print(f"认证可用: {api.available}  (auth: {api.auth_data_path})")
    if not api.available:
        print("登录态不可用，先跑主程序扫码。")
        return

    homes = api.get_homes_list()
    devices = api.get_devices_list() + api.get_shared_devices_list()
    info = {str(d["did"]): d for d in devices}
    print(f"家庭 {len(homes)} 个 / 设备 {len(devices)} 台")

    raw: dict[str, dict] = {}
    rows: list[dict] = []
    for home in homes:
        hid, name = str(home["id"]), home.get("name", "")
        print(f"\n[家庭] {name!r} id={hid} uid={home.get('uid')}")
        for flag in (True, False):
            ret = ask(api, home, flag)
            if not ret:
                continue
            wraps = ret.get("items") or []
            total = sum(len(w.get("consumes_data") or []) for w in wraps)
            ignored = sum(int(w.get("ignore_count") or 0) for w in wraps)
            print(f"  filter_ignore={flag}: 分组 {len(wraps)} 个 / 条目 {total} / "
                  f"已忽略 {ignored}")
            if flag:
                raw[hid] = ret
                for w in wraps:
                    st = w.get("state")
                    print(f"    分组 state={st}（{STATE_GUESS.get(st, '?')}）"
                          f"count={w.get('count')} 条目 {len(w.get('consumes_data') or [])}")
                    for item in w.get("consumes_data") or []:
                        item["_home_id"] = hid
                        item["_state"] = st
                        rows.append(item)

    print(f"\n[汇总] 耗材条目 {len(rows)} 条 / 涉及设备 "
          f"{len({str(r.get('did')) for r in rows})} 台")

    repo = SpecRepository()
    matched = 0
    print("  逐条（设备 / 耗材名 / 值 / 云端阈值 / prop → 规格）:")
    for r in rows:
        dets = r.get("details")
        dets = dets if isinstance(dets, list) else [dets]
        did = str(r.get("did"))
        spec = repo.get(info.get(did, {}).get("spec_type") or "")
        for d in dets:
            if not isinstance(d, dict):
                continue
            prop = d.get("prop") or ""
            pair = parse_prop(prop)
            hit = ""
            if spec and pair:
                svc = next((s for s in spec.services if s.siid == pair[0]), None)
                pr = next((p for p in (svc.properties if svc else []) if p.piid == pair[1]), None)
                if pr:
                    matched += 1
                    hit = f"→ {pr.siid}.{pr.piid} {pr.name}"
                else:
                    hit = f"→ siid {pair[0]} 无 piid {pair[1]}"
            elif prop:
                hit = "→ prop 非 siid.piid 形式，未映射"
            print(f"    - {r.get('name')!r} | {d.get('description')!r} "
                  f"value={d.get('value')!r} 不足={threshold(d.get('inadeq'))} "
                  f"耗尽={threshold(d.get('exhaust'))} | {prop} {hit}")
    print(f"  可映射 {matched} / {sum(1 for r in rows for _ in (r.get('details') or []))} 条详情")

    have = {str(r.get("did")) for r in rows}
    missing = [d for d in devices if str(d["did"]) not in have]
    print(f"  清单外设备 {len(missing)} 台，涉及型号 "
          f"{len({d.get('model') for d in missing})} 个")

    dump("consumable_items.json", {
        "homes": [{"id": h["id"], "name": h.get("name")} for h in homes],
        "items": rows,
        "raw": raw,
    })
    print(f"\n完成，用时 {time.time() - t0:.1f}s")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    main()
