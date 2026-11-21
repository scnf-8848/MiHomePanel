# SPDX-License-Identifier: GPL-3.0-or-later
"""只读探测：把所有设备规格里的枚举项（``value-list``）导出来，看哪些需要短名。

为什么要有这个脚本：米家云端给枚举项的 ``comment`` 常常是**给开发者看的整句描述**
（"0关闭待机息屏，待机时屏幕一直亮；1开启待机息屏…"），而 ``description`` 是英文
（"Off" / "Automatic"）。详情页的下拉框最宽 240px、按 10 个字算宽，直接用整句会被
硬切；现在只是机械截到 14 个字加省略号，切出来的词经常没有意义（"0关闭待机息屏，待…"）。

这个脚本不改任何东西，只做三件事：

1. 从**本地规格缓存**读出全部枚举项（不联网、不读设备、不发写指令）
2. 按长度排序，列出"长到需要人工起短名"的那些，附带它出现在哪些设备的哪个属性上
3. 给出当前 UI 的机械截断结果，方便对比人工短名与截断的差别

用法：
    .venv/Scripts/python.exe -X utf8 scripts/probe_enums.py
    .venv/Scripts/python.exe -X utf8 scripts/probe_enums.py --limit 12 --all
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from mihome.core import enum_text, paths
from mihome.core.spec import parse_raw


def load_specs(cache_dir: Path | None = None) -> list[tuple[str, dict]]:
    """读出缓存里所有规格，返回 [(urn, raw_spec)]。"""
    d = cache_dir or paths.spec_cache_dir()
    out: list[tuple[str, dict]] = []
    for f in sorted(d.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        raw = data.get("raw") or {}
        urn = data.get("urn") or raw.get("type") or f.stem
        out.append((urn, raw))
    return out


def collect(specs: list[tuple[str, dict]]) -> tuple[dict[str, dict], int, int]:
    """汇总每个枚举文案：出现次数、最长长度、上下文样例。

    返回 ``({display: {"count", "max_len", "where", "descriptions", "cats"}}, 属性数, 选项数)``。
    ``cats`` 是这段文案出现过的品类（URN 的 category 段）——同一个词在不同品类
    含义可能不同，判断"是否已映射"时要带上它。
    """
    seen: dict[str, dict] = defaultdict(
        lambda: {
            "count": 0,
            "max_len": 0,
            "where": set(),
            "descriptions": set(),
            "cats": set(),
            "props": set(),
        }
    )
    prop_count = 0
    opt_count = 0
    for urn, raw in specs:
        model = urn.rsplit(".", 1)[-1] if "." in urn else urn
        category = _category(urn)
        for svc in raw.get("services") or []:
            slabel = svc.get("comment") or svc.get("description") or ""
            for prop in svc.get("properties") or []:
                values = prop.get("value-list") or []
                if not values:
                    continue
                prop_count += 1
                pname = _urn_name(prop.get("type", ""), prop.get("description", ""))
                plabel = prop.get("comment") or prop.get("description") or ""
                for opt in values:
                    display = (opt.get("comment") or opt.get("description") or "").strip()
                    opt_count += 1
                    if not display:
                        continue
                    rec = seen[display]
                    rec["count"] += 1
                    rec["max_len"] = max(rec["max_len"], len(display))
                    rec["where"].add(f"{model}/{slabel}.{plabel}")
                    rec["cats"].add(category)
                    rec["props"].add(pname)
                    desc = (opt.get("description") or "").strip()
                    if desc and desc != display:
                        rec["descriptions"].add(desc)
    return seen, prop_count, opt_count


def _category(urn: str) -> str:
    """从 ``urn:miot-spec-v2:device:<category>:<catid>:...`` 取 category 段。"""
    parts = urn.split(":")
    return parts[3] if len(parts) > 3 and parts[0] == "urn" else ""


def _urn_name(urn: str, fallback: str = "") -> str:
    """``urn:miot-spec-v2:property:wash-mode:0000006A:1`` → ``wash-mode``。

    和 ``spec._urn_name`` 同一套规则，但这里不 import 私有函数：探测脚本要能
    单独跑，规格层的内部改名不该把脚本带崩。
    """
    parts = (urn or "").split(":")
    return parts[3] if len(parts) > 3 and parts[0] == "urn" else fallback


def coverage(specs: list[tuple[str, dict]], limit: int) -> dict[str, Counter]:
    """按 ``core/enum_text.py`` 的映射跑一遍，看还剩多少"英文 / 超长 / 英文属性名"。

    这是维护这个映射表时唯一需要盯的数字：故障码（``C1`` / ``E2.1``）本来就不该翻，
    所以单独统计出来，避免"剩 44 种英文"看起来像没做完。
    """
    cjk = lambda s: bool(re.search(r"[\u4e00-\u9fff]", s))
    left_en: Counter = Counter()
    left_long: Counter = Counter()
    left_prop: Counter = Counter()
    left_by_cat: Counter = Counter()
    total_by_cat: Counter = Counter()
    stat = {"enum": 0, "en": 0, "cjk": 0, "long": 0, "prop_en": 0, "prop_cjk": 0}
    for _urn, raw in specs:
        spec = parse_raw(raw)
        for p in spec.properties:
            label = p.label
            if label and not cjk(label):
                stat["prop_en"] += 1
                if cjk(p.display_label):
                    stat["prop_cjk"] += 1
                else:
                    left_prop[label] += 1
            for o in p.value_list:
                stat["enum"] += 1
                total_by_cat[spec.category] += 1
                if o.display and not cjk(o.display) and re.search(r"[A-Za-z]", o.display):
                    stat["en"] += 1
                    if cjk(o.display_short):
                        stat["cjk"] += 1
                    else:
                        left_en[o.display] += 1
                        left_by_cat[spec.category] += 1
                if len(o.display) > limit:
                    stat["long"] += 1
                    if len(o.display_short) > limit:
                        left_long[o.display] += 1
    return {
        "stat": stat,  # type: ignore[dict-item]
        "left_en": left_en,
        "left_long": left_long,
        "left_prop": left_prop,
        "left_by_cat": left_by_cat,
        "total_by_cat": total_by_cat,
    }


def cut(text: str, limit: int = 14) -> str:
    """详情页当前的机械截断（detail.py 里那段）。"""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def main() -> int:
    ap = argparse.ArgumentParser(description="导出规格里的枚举文案，找需要短名的项")
    ap.add_argument("--limit", type=int, default=14, help="超过这个长度算「需要短名」（默认 14）")
    ap.add_argument("--all", action="store_true", help="连短文案也一起列出来")
    ap.add_argument("--cache", type=Path, default=None, help="规格缓存目录（默认取数据目录）")
    args = ap.parse_args()

    specs = load_specs(args.cache)
    if not specs:
        print(f"规格缓存为空：{paths.spec_cache_dir()}")
        print("先正常启动一次客户端（会拉规格并落盘），再跑这个脚本。")
        return 1

    seen, prop_count, opt_count = collect(specs)
    print(f"规格 {len(specs)} 份 · 含枚举的属性 {prop_count} 个 · 枚举项 {opt_count} 条 · 去重文案 {len(seen)} 条")

    cov = coverage(specs, args.limit)
    st = cov["stat"]
    print(
        f"映射后：枚举 {st['enum']} 条 · 英文 {st['en']} → 中文 {st['cjk']}"
        f"（剩 {sum(cov['left_en'].values())} 条 / {len(cov['left_en'])} 种）"
    )
    codes = [t for t in cov["left_en"] if re.fullmatch(r"[A-Z]{1,3}\d*(?:\.\d+)?", t)]
    print(
        f"        超 {args.limit} 字 {st['long']} 条 → 收进 {args.limit} 字"
        f"（剩 {sum(cov['left_long'].values())} 条）"
        f" · 英文属性名 {st['prop_en']} → 中文 {st['prop_cjk']}"
    )
    if codes:
        print(f"        剩余英文里 {len(codes)} 种是故障码（C1 / E2.1…），按设计保留原样")
    if cov["left_long"]:
        print("        仍超长的：" + "、".join(list(cov["left_long"])[:5]))
    if cov["left_by_cat"]:
        # 按品类看还剩多少：新加一段文案时，先看这段该落在哪一层
        # （跨品类 → enum_text 全局表；某品类独有 → adapters/categories.py）。
        print("\n剩余英文按品类（分子=仍有英文的项，分母=该品类的枚举项总数）：")
        for cat, n in cov["left_by_cat"].most_common(8):
            print(f"        {cat:<26}{n:>4} / {cov['total_by_cat'][cat]:>4}")

    dist = Counter()
    for text in seen:
        n = len(text)
        bucket = "<6" if n < 6 else "6-9" if n < 10 else "10-14" if n <= 14 else "15-24" if n <= 24 else ">=25"
        dist[bucket] += 1
    order = ["<6", "6-9", "10-14", "15-24", ">=25"]
    print("长度分布（按去重文案）：" + "  ".join(f"{b}:{dist[b]}" for b in order if dist[b]))

    rows = sorted(seen.items(), key=lambda kv: (-len(kv[0]), kv[0]))
    long_rows = [r for r in rows if len(r[0]) > args.limit]

    def _alias_cats(text: str, rec: dict) -> bool:
        """这段文案在它出现过的任一（品类, 属性）组合里已经有人工映射就算映射过。"""
        cats = rec.get("cats") or {""}
        props = rec.get("props") or {""}
        return any(
            enum_text.has_alias(text, prop, category=c) for c in cats for prop in props
        )

    uncovered = [(t, rec) for t, rec in long_rows if not _alias_cats(t, rec)]
    print(
        f"\n超 {args.limit} 字的文案：{len(long_rows)} 条"
        f"（已人工映射 {len(long_rows) - len(uncovered)} 条，靠截断 {len(uncovered)} 条）"
    )
    if uncovered:
        print("下面这些还没映射，现在显示的是截断结果：")
        for text, rec in uncovered:
            print(f"\n  [{len(text):>3} 字 × {rec['count']} 处] {text}")
            print(f"      截断后：{cut(text)}")
            if rec["descriptions"]:
                print(f"      英文/原名：{' / '.join(sorted(rec['descriptions'])[:3])}")
            where = sorted(rec["where"])[:3]
            print(f"      出现于：{'；'.join(where)}" + ("…" if len(rec["where"]) > 3 else ""))

    if args.all:
        print(f"\n全部文案 {len(rows)} 条及其显示结果：")
        for text, rec in rows:
            mark = "✓" if _alias_cats(text, rec) else " "
            cat = next(iter(rec.get("cats") or {""}), "")
            prop = next(iter(rec.get("props") or {""}), "")
            short = enum_text.enum_short(text, prop, category=cat, limit=args.limit)
            print(f"  {mark} [{len(text):>2} 字 × {rec['count']}] {text}  →  {short}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
