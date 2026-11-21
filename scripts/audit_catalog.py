"""体检：用真实的设备快照，验证"品类 → 卡片"映射的覆盖率。

    .venv/Scripts/python.exe -X utf8 scripts/audit_catalog.py

它做三件事：
1. 把设备条目整理成"真机 + 虚拟子设备"（多键面板只出一张卡）；
2. 按品类绑定控件，统计每张卡真正用掉了多少属性、多少收进"更多功能"；
3. 把没映射的品类、绑不上的关键控件、没有规格的设备单独列出来。

产出 scripts/out/catalog_audit.json 供后续 UI 与回归使用。
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mihome.core import catalog, tree  # noqa: E402
from mihome.core.spec import DeviceSpec, SpecRepository  # noqa: E402

SNAPSHOT = ROOT / "scripts" / "out" / "cloud_snapshot.json"
OUT = ROOT / "scripts" / "out" / "catalog_audit.json"


def main() -> int:
    import json

    rows = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["devices"]
    nodes = tree.group_devices(rows)
    repo = SpecRepository()
    specs = repo.get_many([n.spec_type for n in nodes if n.spec_type], workers=4)

    virtual = sum(len(n.children) for n in nodes)
    print(f"设备条目 {len(rows)} → 真机 {len(nodes)}（虚拟子设备 {virtual}）")
    print(f"规格 {len(specs)} 份 / {len({n.spec_type for n in nodes if n.spec_type})} 种，"
          f"来源 {dict(Counter(s.source for s in specs.values()))}\n")

    per_cat: dict[str, list[str]] = defaultdict(list)
    cards: list[dict] = []
    problems: list[tuple[str, str]] = []

    for node in nodes:
        spec: DeviceSpec | None = specs.get(node.spec_type)
        if spec is None:
            problems.append((node.name, "无规格（只能展示，无法控制）"))
            cards.append(
                {
                    "did": node.did,
                    "name": node.name,
                    "model": node.model,
                    "room": node.room,
                    "category": "",
                    "layout": "unknown",
                    "online": node.online,
                    "spec": None,
                }
            )
            continue

        card = catalog.build_card(spec, model=node.model,
                                  gang_names=node.gang_names, gang_rooms=node.gang_rooms)
        cov = catalog.coverage(card, spec)
        per_cat[spec.category].append(node.name)
        if card.missing:
            problems.append((node.name, f"{spec.category} 绑不上：{', '.join(card.missing)}"))
        if card.layout == "generic":
            problems.append((node.name, f"品类 {spec.category} 未映射，走了通用卡片"))

        cards.append(
            {
                "did": node.did,
                "name": node.name,
                "model": node.model,
                "room": node.room,
                "category": spec.category,
                "layout": card.layout,
                "icon": card.icon,
                "accent": card.accent,
                "span": card.span,
                "online": node.online,
                "gangs": [{"siid": g.siid, "label": g.label, "room": g.room} for g in card.gangs],
                "controls": card.to_dict()["controls"],
                "metrics": card.to_dict()["metrics"],
                "advanced": [catalog.short_label(p.label) for p in card.advanced],
                "actions": [a.name for a in card.actions],
                "coverage": cov,
                "missing": card.missing,
                "spec": {
                    "urn": spec.urn,
                    "version": spec.version,
                    "services": [s.name for s in spec.services],
                    "events": [f"{e.label}({e.name})" for e in spec.events()],
                },
            }
        )

    # -- 品类汇总 ---------------------------------------------------------
    print(f"{'品类':<30}{'台数':>4}{'布局':>10}{'控件':>6}{'指标':>5}{'更多':>6}{'未用':>6}  缺失")
    print("-" * 96)
    for cat, names in sorted(per_cat.items(), key=lambda kv: -len(kv[1])):
        sub = [c for c in cards if c["category"] == cat]
        ctl = sum(len(c["controls"]) + len(c["gangs"]) for c in sub) / max(1, len(sub))
        met = sum(len(c["metrics"]) for c in sub) / max(1, len(sub))
        adv = sum(c["coverage"]["advanced"] for c in sub) / max(1, len(sub))
        unused = sum(c["coverage"]["unused"] for c in sub) / max(1, len(sub))
        missing = sorted({m for c in sub for m in c["missing"]})
        layout = sub[0]["layout"]
        print(
            f"{cat:<30}{len(names):>4}{layout:>10}{ctl:>6.1f}{met:>5.1f}{adv:>6.0f}{unused:>6.0f}  "
            f"{','.join(missing)}"
        )

    # -- 问题清单 ---------------------------------------------------------
    print(f"\n问题 {len(problems)} 条：")
    seen: set[tuple[str, str]] = set()
    for name, msg in problems:
        key = (msg.split("（")[0], msg)
        if key in seen:
            continue
        seen.add(key)
        print(f"  · {name}: {msg}")

    # 多键面板合并情况
    panels = [n for n in nodes if n.children]
    print("\n多键面板合并：")
    for n in panels:
        card = next(c for c in cards if c["did"] == n.did)
        print(
            f"  {n.name} ({n.room}) → 1 张卡 / {len(card['gangs'])} 路: "
            + ", ".join(f"s{g['siid']}={g['label']}({g['room']})" for g in card["gangs"])
        )

    OUT.write_text(
        json.dumps({"cards": cards, "problems": [list(p) for p in problems]}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"\n写出 {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
