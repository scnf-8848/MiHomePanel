"""用真实云端回读，验证"品类 → 控件"绑定的 siid/piid 是否真的有效。

    .venv/Scripts/python.exe -X utf8 scripts/verify_bindings.py

体检脚本只能证明"规格里写了这个属性"，这个脚本证明"这台设备真的认这个 (siid,piid)"。
云端对错位的寻址会返回非 0 的 code（例如 -704083036 是子设备路由错、-704002000 是
设备实际不支持该属性），所以 code 分布就是绑定表的正确率。

产出 scripts/out/live_props.json，供 render_preview.py 画真实读数。

注意：这里 import 了 mijiaAPI（GPL-3.0）只是开发期的探针，正式运行时代码不依赖它。
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mihome.core import catalog, tree  # noqa: E402
from mihome.core.api import apply_locale  # noqa: E402
from mihome.core.spec import SpecRepository  # noqa: E402

SNAPSHOT = ROOT / "scripts" / "out" / "cloud_snapshot.json"
OUT = ROOT / "scripts" / "out" / "live_props.json"
BATCH = 20


def main() -> int:
    import mijiaAPI

    rows = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["devices"]
    nodes = tree.group_devices(rows)
    repo = SpecRepository()
    specs = repo.get_many([n.spec_type for n in nodes if n.spec_type], workers=4)

    # 收集"卡片上真正要读"的寻址：只读卡片用到的，不读整台设备的 100 个属性
    wanted: list[dict] = []
    names: dict[tuple[str, int, int], str] = {}
    for n in nodes:
        if not n.online or not n.spec_type:
            continue
        s = specs.get(n.spec_type)
        if s is None:
            continue
        card = catalog.build_card(s, gang_names=n.gang_names, gang_rooms=n.gang_rooms)
        props = [c.prop for c in (*card.controls, *card.metrics) if c.prop]
        props += [g.power for g in card.gangs]
        seen: set[tuple[int, int]] = set()
        for p in props:
            if p.key in seen:
                continue
            seen.add(p.key)
            wanted.append({"did": n.did, "siid": p.siid, "piid": p.piid})
            names[(n.did, p.siid, p.piid)] = p.name

    print(f"待验证寻址 {len(wanted)} 条（{len({w['did'] for w in wanted})} 台在线设备）")

    api = mijiaAPI.mijiaAPI()
    apply_locale(api)  # 与主程序一致
    result: dict[str, dict] = defaultdict(lambda: {"by_method": {}, "by_name": {}})
    codes: Counter = Counter()
    t0 = time.time()
    for i in range(0, len(wanted), BATCH):
        chunk = wanted[i : i + BATCH]
        try:
            resp = api.get_devices_prop(chunk)
        except Exception as exc:  # noqa: BLE001 - 探针脚本，失败要看到原文
            print(f"  批次 {i // BATCH} 失败: {exc}")
            continue
        if isinstance(resp, dict):
            resp = [resp]
        for item in resp or []:
            did = str(item.get("did", ""))
            siid, piid = int(item.get("siid", 0)), int(item.get("piid", 0))
            code = item.get("code")
            codes[code] += 1
            bucket = result[did]
            bucket["by_method"][f"{siid}.{piid}"] = {
                "value": item.get("value"),
                "code": code,
                "ts": item.get("updateTime"),
            }
            nm = names.get((did, siid, piid))
            if nm:
                bucket["by_name"].setdefault(nm, {"value": item.get("value"), "code": code, "ts": item.get("updateTime")})
        print(f"  {min(i + BATCH, len(wanted))}/{len(wanted)}  {time.time() - t0:.1f}s")

    ok = codes.get(0, 0) + codes.get(1, 0)
    print(f"\ncode 分布: {dict(codes)}")
    print(f"有效 {ok}/{sum(codes.values())} = {ok / max(1, sum(codes.values())):.1%}")

    bad = [
        (did, m, v)
        for did, bucket in result.items()
        for m, v in bucket["by_method"].items()
        if v["code"] not in (0, 1)
    ]
    if bad:
        print(f"\n失败 {len(bad)} 条：")
        for did, m, v in bad[:30]:
            nm = names.get((did, int(m.split(".")[0]), int(m.split(".")[1])), "?")
            print(f"  did={did} {m} {nm}: code={v['code']}")

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n写出 {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
