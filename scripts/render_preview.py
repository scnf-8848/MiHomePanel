"""把"品类 → 卡片"的绑定结果渲染成一张可视化卡片墙，用来做视觉验收。

    .venv/Scripts/python.exe -X utf8 scripts/render_preview.py

数据来自本机快照：cloud_snapshot.json（设备列表）+ live_props.json（云端回读的真实读数，
由 verify_bindings.py 生成；没有时退回 props_sample.json）。不联网。
产出 scripts/out/catalog_preview.html。
"""

from __future__ import annotations

import datetime as dt
import html
import json
import sys
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mihome.core import catalog, tree  # noqa: E402
from mihome.core.spec import SpecRepository  # noqa: E402

OUT = ROOT / "scripts" / "out" / "catalog_preview.html"
SNAPSHOT = ROOT / "scripts" / "out" / "cloud_snapshot.json"
LIVE = ROOT / "scripts" / "out" / "live_props.json"
PROPS = ROOT / "scripts" / "out" / "props_sample.json"

# ---------------------------------------------------------------------------
# 图标（线性描边，24x24）
# ---------------------------------------------------------------------------

ICONS: dict[str, str] = {
    "light": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    "switch": '<rect x="2.5" y="6" width="19" height="12" rx="3"/><circle cx="8" cy="12" r="1.4"/><circle cx="16" cy="12" r="1.4"/>',
    "outlet": '<rect x="5" y="2.5" width="14" height="19" rx="3"/><path d="M9.5 8v3.5M14.5 8v3.5M9.5 15.5h5"/>',
    "ac": '<rect x="2.5" y="4.5" width="19" height="9.5" rx="2.5"/><path d="M6 17.5c1.4 0 1.4 2 2.8 2M11 17.5c1.4 0 1.4 2 2.8 2M16 17.5c1.4 0 1.4 2 2.8 2"/>',
    "fan": '<circle cx="12" cy="12" r="2"/><path d="M12 10c0-4 1-6 3-6s3 2 2 4-3 2-5 2zM14 12c4 0 6 1 6 3s-2 3-4 2-2-3-2-5zM10 12c-4 0-6-1-6-3s2-3 4-2 2 3 2 5z"/>',
    "purifier": '<rect x="6" y="2.5" width="12" height="19" rx="3"/><path d="M9.5 7.5h5M9.5 11h5M9.5 14.5h5"/>',
    "sensor": '<circle cx="12" cy="14" r="2"/><path d="M8.4 17.6a5 5 0 010-7.1M15.6 10.5a5 5 0 010 7.1M5.6 20.4a9 9 0 010-12.7M18.4 7.7a9 9 0 010 12.7"/>',
    "motion": '<circle cx="12" cy="9" r="2.5"/><path d="M7 20c0-2.8 2.2-5 5-5s5 2.2 5 5"/>',
    "presence": '<circle cx="12" cy="8" r="3"/><path d="M5.5 20c0-3.6 2.9-6 6.5-6s6.5 2.4 6.5 6"/>',
    "thermo": '<path d="M12 3.5a2 2 0 012 2v8.2a4 4 0 11-4 0V5.5a2 2 0 012-2z"/><path d="M12 9v6"/>',
    "lock": '<rect x="4.5" y="10.5" width="15" height="10" rx="2.5"/><path d="M8 10.5V7.5a4 4 0 018 0v3"/><circle cx="12" cy="15.5" r="1.2"/>',
    "camera": '<rect x="2.5" y="6.5" width="12.5" height="10" rx="2.5"/><path d="M15.5 10.5l5-2.5v9l-5-2.5z"/>',
    "speaker": '<rect x="8" y="2.5" width="8" height="19" rx="4"/><circle cx="12" cy="8" r="1.6"/><path d="M10.5 15h3"/>',
    "tv": '<rect x="2.5" y="4.5" width="19" height="12" rx="2.5"/><path d="M8.5 20h7"/>',
    "washer": '<rect x="4" y="2.5" width="16" height="19" rx="3"/><circle cx="12" cy="13.5" r="4.2"/><circle cx="12" cy="13.5" r="1.2"/>',
    "hood": '<path d="M3.5 5.5h17l-2.5 8h-12z"/><path d="M8.5 13.5v5M15.5 13.5v5"/>',
    "oven": '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M3.5 9.5h17M8 15.5h.01M12 15.5h.01"/>',
    "gateway": '<rect x="4" y="11" width="16" height="9.5" rx="2.5"/><path d="M12 11V6"/><circle cx="12" cy="4" r="1.5"/>',
    "remote": '<rect x="8" y="2.5" width="8" height="19" rx="4"/><circle cx="12" cy="7.5" r="1.5"/><path d="M10 13h4M10 16.5h4"/>',
    "scale": '<rect x="3.5" y="3.5" width="17" height="17" rx="4"/><path d="M8 12h8"/><circle cx="12" cy="12" r="2.2"/>',
    "vacuum": '<circle cx="12" cy="12.5" r="7.5"/><circle cx="12" cy="12.5" r="2"/><path d="M4.8 8.5h14.4"/>',
    "water": '<path d="M12 3.5c3 4 5.5 6.6 5.5 9.5a5.5 5.5 0 11-11 0c0-2.9 2.5-5.5 5.5-9.5z"/>',
    "humidifier": '<path d="M6 10.5a6 6 0 0112 0v6a3 3 0 01-3 3H9a3 3 0 01-3-3z"/><path d="M9.5 13.5h5"/>',
    "airer": '<path d="M3.5 6.5h17M6.5 6.5v11a2 2 0 004 0M17.5 6.5v4"/><path d="M9 6.5v4"/>',
    "curtain": '<path d="M3 4.5h18"/><path d="M6 4.5c0 6-1 9-2.5 15M18 4.5c0 6 1 9 2.5 15M6 4.5v15M18 4.5v15"/>',
    "heater": '<rect x="3.5" y="5" width="17" height="9" rx="2"/><path d="M7 17.5c1.2 0 1.2 2 2.4 2M12 17.5c1.2 0 1.2 2 2.4 2M17 17.5c1.2 0 1.2 2 2.4 2"/>',
    "device": '<rect x="4.5" y="4.5" width="15" height="15" rx="4"/><circle cx="12" cy="12" r="2"/>',
}


def icon(key: str, size: int = 26) -> str:
    body = ICONS.get(key, ICONS["device"])
    return (
        f'<svg class="ic" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
        f'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" '
        f'stroke-linejoin="round">{body}</svg>'
    )


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------


def esc(x: object) -> str:
    return html.escape(str(x), quote=True)


def load():
    rows = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["devices"]
    live = json.loads(LIVE.read_text(encoding="utf-8")) if LIVE.exists() else {}
    sample = json.loads(PROPS.read_text(encoding="utf-8")) if PROPS.exists() else {}
    nodes = tree.group_devices(rows)

    repo = SpecRepository()
    specs = repo.get_many([n.spec_type for n in nodes if n.spec_type], workers=4)

    cards: dict[str, dict] = {}
    for n in nodes:
        s = specs.get(n.spec_type)
        if s is None:
            cards[n.did] = {"layout": "unknown", "icon": "device", "accent": "#9AA0A6", "category": "", "controls": [], "metrics": [], "gangs": [], "advanced": 0, "actions": []}
            continue
        c = catalog.build_card(s, model=n.model,
                               gang_names=n.gang_names, gang_rooms=n.gang_rooms)
        cards[n.did] = {
            "layout": c.layout,
            "icon": c.icon,
            "accent": c.accent,
            "span": c.span,
            "category": c.category,
            "controls": [
                {"slot": x.slot, "label": x.label, "role": x.role, "method": x.prop.method if x.prop else None,
                 "name": x.prop.name if x.prop else None, "unit": x.prop.unit_text if x.prop else "",
                 "unit_raw": x.prop.unit if x.prop else None, "kind": x.prop.kind if x.prop else None,
                 "options": {o.value: o.display for o in x.prop.value_list} if x.prop else {}}
                for x in c.controls
            ],
            "metrics": [
                {"slot": x.slot, "label": x.label, "method": x.prop.method if x.prop else None,
                 "name": x.prop.name if x.prop else None, "unit": x.prop.unit_text if x.prop else "",
                 "unit_raw": x.prop.unit if x.prop else None,
                 "options": {o.value: o.display for o in x.prop.value_list} if x.prop else {}}
                for x in c.metrics
            ],
            "gangs": [{"siid": g.siid, "label": catalog.gang_label(g.label, i + 1), "room": g.room,
                       "method": g.power.method, "name": g.power.name} for i, g in enumerate(c.gangs)],
            "advanced": len(c.advanced),
            "actions": [a.label for a in c.actions],
        }
    return nodes, cards, live, sample


def value_at(did: str, method: str | None, name: str | None, live: dict, sample: dict):
    entry = live.get(did)
    if entry and method:
        item = entry.get("by_method", {}).get(method)
        if item and item.get("code") in (0, 1):
            return item.get("value")
    if name and did in sample and name in sample[did]:
        return sample[did][name].get("value")
    return None


def last_seen(did: str, live: dict, sample: dict) -> int | None:
    ts: list[int] = []
    entry = live.get(did)
    if entry:
        ts += [v.get("ts") for v in entry.get("by_method", {}).values() if v.get("ts")]
    if did in sample:
        ts += [v.get("updateTime") for v in sample[did].values() if v.get("updateTime")]
    return max(ts) if ts else None


def fmt(value, unit: str, unit_raw: str | None, options: dict | None = None) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "开" if value else "关"
    if options and value in options:
        return str(options[value])
    if unit_raw == "percentage" or unit == "%":
        return f"{value:g}%"
    if unit_raw == "celsius" or unit == "°C":
        return f"{value:g}°"
    if unit == "K":
        return f"{int(value)}K"
    if isinstance(value, float):
        if abs(value) >= 100:
            return f"{value:.0f}{unit}"
        return f"{value:g}{unit}"
    return f"{value}{unit}"


def age_text(ts: int | None, now: int) -> str:
    if not ts:
        return ""
    sec = max(0, now - ts)
    if sec < 180:
        return "刚刚"
    if sec < 3600:
        return f"{sec // 60} 分钟前"
    if sec < 86400:
        return f"{sec // 3600} 小时前"
    return f"{sec // 86400} 天前"


# ---------------------------------------------------------------------------
# 卡片
# ---------------------------------------------------------------------------


def render_card(node: tree.DeviceNode, spec: dict, live: dict, sample: dict, now: int) -> str:
    did = node.did
    layout = spec.get("layout", "unknown")
    accent = spec.get("accent") or "#9AA0A6"

    def val(method=None, name=None):
        return value_at(did, method, name, live, sample)

    metrics = spec.get("metrics", [])
    offline = not node.online
    classes = "card"
    if spec.get("span") == 2:
        classes += " wide"
    if offline:
        classes += " offline"

    state_bits = []
    max_bits = 2 if spec.get("span") == 2 else 1
    for m in metrics:
        v = val(m["method"], m["name"])
        if v is None or isinstance(v, bool):
            continue
        state_bits.append(f"{m['label']} {fmt(v, m['unit'], m['unit_raw'], m.get('options'))}")
        if len(state_bits) >= max_bits:
            break
    hint = " · ".join(state_bits)

    on = False
    ctrl = ""

    if layout == "switch" and spec.get("gangs"):
        chips = []
        for g in spec["gangs"]:
            v = val(g["method"], g["name"])
            on = on or bool(v)
            dot = "●" if v else ("○" if v is False else "◌")
            cls = " on" if v else ""
            chips.append(f'<span class="gang{cls}"><i>{dot}</i>{esc(g["label"])}</span>')
        ctrl = f'<div class="gangs">{"".join(chips)}</div>'

    elif layout == "light":
        on = bool(val(name="on"))
        bar = ""
        b = val(name="brightness")
        if isinstance(b, (int, float)):
            bar = f'<div class="bar"><i style="width:{max(0, min(100, int(b)))}%"></i></div>'
        k = val(name="color-temperature")
        ktxt = f"{int(k)}K" if isinstance(k, (int, float)) and k else ""
        ctrl = (
            f'<div class="row"><span class="hint">{esc(ktxt or hint or ("已开启" if on else "已关闭"))}</span>'
            f'<span class="sw{" on" if on else ""}"></span></div>{bar}'
        )

    elif layout == "climate":
        on = bool(val(name="on"))
        temp = val(name="target-temperature")
        big = f'<span class="big">{int(temp)}<em>°</em></span>' if isinstance(temp, (int, float)) else ""
        ctrl = (
            f'<div class="row"><span class="bigwrap">{big}<span class="hint">{esc(hint)}</span></span>'
            f'<span class="sw{" on" if on else ""}"></span></div>'
        )

    elif layout == "ir_climate":
        temp = val(name="ir-temperature")
        mode = val(name="ir-mode")
        big = f'<span class="big">{int(temp)}<em>°</em></span>' if isinstance(temp, (int, float)) else ""
        ctrl = f'<div class="row"><span class="bigwrap">{big}<span class="hint">{esc("红外发射器 · 状态需本地记录" if mode is None else f"模式 {mode}")}</span></span></div>'

    elif layout == "fan":
        on = bool(val(name="on"))
        lv = val(name="fan-level")
        ctrl = (
            f'<div class="row"><span class="hint">{esc(f"风速 {lv}" if lv is not None else "")}</span>'
            f'<span class="sw{" on" if on else ""}"></span></div>'
        )

    elif layout == "purifier":
        on = bool(val(name="on"))
        ctrl = (
            f'<div class="row"><span class="hint">{esc(hint)}</span>'
            f'<span class="sw{" on" if on else ""}"></span></div>'
        )

    elif layout in ("outlet", "generic", "camera"):
        on = bool(val(name="on"))
        ctrl = (
            f'<div class="row"><span class="hint">{esc(hint or ("已开启" if on else "已关闭"))}</span>'
            f'<span class="sw{" on" if on else ""}"></span></div>'
        )

    elif layout == "media":
        on = bool(val(name="is-on"))
        v = val(name="volume")
        ctrl = (
            f'<div class="row"><span class="hint">{esc(hint or (f"音量 {v}%" if isinstance(v, (int, float)) else ""))}</span>'
            f'<span class="sw{" on" if on else ""}"></span></div>'
        )

    elif layout == "cover":
        pos = val(name="target-position")
        if pos is None:
            pos = val(name="current-position")
        p = 0 if pos is None else max(0, min(100, int(pos)))
        ctrl = f'<div class="row"><span class="hint">{p}%</span></div><div class="bar"><i style="width:{p}%"></i></div>'

    elif layout == "sensor":
        cells = []
        for m in metrics[:3]:
            v = val(m["method"], m["name"])
            cells.append(f'<span class="cell"><b>{fmt(v, m["unit"], m["unit_raw"], m.get("options"))}</b><i>{esc(m["label"])}</i></span>')
        ctrl = f'<div class="cells">{"".join(cells)}</div>' if cells else '<div class="hint">无读数</div>'

    elif layout == "lock":
        ds = val(name="door-state")
        bt = val(name="battery-level")
        txt = "门已关" if ds is True else ("门未关" if ds is False else "状态未知")
        if isinstance(bt, (int, float)):
            txt += f" · 电量 {int(bt)}%"
        ctrl = f'<div class="row"><span class="hint">{esc(txt)}</span></div>'

    elif layout == "appliance":
        left = val(name="left-time")
        if left is None:
            left = val(name="left-time-2")
        st_metric = next((m for m in metrics if m["slot"] == "status"), None)
        st = val(name="status")
        if isinstance(left, (int, float)) and left:
            txt = f"剩余 {int(left)} 分钟"
        elif st is not None and st_metric:
            txt = fmt(st, st_metric["unit"], st_metric["unit_raw"], st_metric.get("options"))
        else:
            txt = "—"
        btn = f'<span class="mini">{esc(spec["actions"][0])}</span>' if spec.get("actions") else ""
        ctrl = f'<div class="row"><span class="hint">{esc(txt)}</span>{btn}</div>'

    else:
        ctrl = f'<div class="row"><span class="hint">{esc(hint or "仅展示")}</span></div>'

    if on:
        classes += " on"

    more = f'<span class="more">⋯{spec["advanced"]}</span>' if spec.get("advanced") else '<span class="more">⋯</span>'
    room = f'<span class="room">{esc(node.room)}</span>' if node.room else ""
    cat = esc(spec.get("category") or "无规格")

    return f"""<article class="{classes}" style="--accent:{accent}">
  <div class="top">{icon(spec.get("icon", "device"))}{more}</div>
  <div class="nm">{esc(node.name)}</div>
  <div class="meta">{room}<span class="cat">{cat}</span></div>
  <div class="ctrl">{ctrl}</div>
</article>"""


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#F1F2F4;color:#17181A;
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei UI","Microsoft YaHei",sans-serif;
 -webkit-font-smoothing:antialiased}
.wrap{max-width:1480px;margin:0 auto;padding:38px 30px 56px}
header h1{font-size:25px;font-weight:600;margin:0 0 6px;letter-spacing:-.3px}
header p{margin:0;color:#6E7480;font-size:13px}
.stats{display:flex;gap:26px;margin:20px 0 0;flex-wrap:wrap}
.stat b{display:block;font-size:21px;font-weight:600;letter-spacing:-.5px}
.stat span{font-size:12px;color:#6E7480}
section{margin-top:34px}
.rh{display:flex;align-items:baseline;gap:10px;margin:0 0 13px}
.rh h2{font-size:16.5px;font-weight:600;margin:0}
.rh .cnt{font-size:12px;color:#8A8F99}
.rh .env{margin-left:auto;font-size:12px;color:#8A8F99;font-variant-numeric:tabular-nums}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(214px,1fr));gap:13px}
.card{background:#fff;border-radius:18px;padding:15px 15px 13px;position:relative;
 border:1px solid rgba(17,20,26,.05);box-shadow:0 1px 2px rgba(17,20,26,.04);
 display:flex;flex-direction:column;min-height:158px;transition:box-shadow .18s,transform .18s}
.card.wide{grid-column:span 2}
@media (max-width:1180px){.card.wide{grid-column:span 1}}
.card:hover{box-shadow:0 8px 22px rgba(17,20,26,.10);transform:translateY(-1px)}
.card .top{display:flex;align-items:flex-start;justify-content:space-between;color:#A8ADB5}
.card.on .top .ic{color:var(--accent)}
.card.offline{background:#FAFAFB}
.card.offline .ic{color:#C6CAD1}
.card.offline .nm{color:#9096A0}
.more{font-size:11px;color:#C3C7CE;letter-spacing:.5px;padding-top:2px}
.nm{font-size:14.5px;font-weight:500;margin-top:11px;line-height:1.35;
 overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.meta{display:flex;gap:8px;margin-top:5px;font-size:11px;color:#B0B5BD}
.ctrl{margin-top:auto;padding-top:12px}
.row{display:flex;align-items:center;justify-content:space-between;gap:10px}
.hint{font-size:12.5px;color:#8A8F99;font-variant-numeric:tabular-nums;
 overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sw{width:34px;height:20px;border-radius:10px;background:#E3E5E9;position:relative;flex:none;transition:background .2s}
.sw::after{content:"";position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:8px;
 background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.18);transition:transform .2s}
.sw.on{background:var(--accent)}
.sw.on::after{transform:translateX(14px)}
.bar{height:4px;border-radius:2px;background:#EDEFF2;margin-top:10px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--accent);border-radius:2px}
.big{font-size:30px;font-weight:600;letter-spacing:-1px;font-variant-numeric:tabular-nums}
.big em{font-style:normal;font-size:15px;font-weight:500;color:#8A8F99;margin-left:1px}
.bigwrap{display:flex;align-items:baseline;gap:10px;min-width:0}
.cells{display:flex;gap:20px}
.cell{display:flex;flex-direction:column;gap:3px}
.cell b{font-size:22px;font-weight:600;letter-spacing:-.6px;font-variant-numeric:tabular-nums}
.cell i{font-style:normal;font-size:11px;color:#9AA0A6}
.gangs{display:flex;flex-wrap:wrap;gap:6px}
.gang{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;color:#6E7480;
 background:#F3F4F6;border-radius:9px;padding:5px 9px;max-width:100%}
.gang i{font-style:normal;color:#BFC4CC;font-size:9px}
.gang.on{background:color-mix(in srgb,var(--accent) 13%,#fff);color:#2C2F35}
.gang.on i{color:var(--accent)}
.mini{font-size:11px;color:#6E7480;background:#F3F4F6;border-radius:8px;padding:4px 10px;flex:none}
footer{margin-top:48px;padding-top:18px;border-top:1px solid rgba(17,20,26,.07);
 font-size:12px;color:#A0A5AD;line-height:1.9}
footer code{background:#E9EBEF;border-radius:4px;padding:1px 4px;font-size:11px}
"""


def main() -> int:
    nodes, cards, live, sample = load()
    now = int(dt.datetime.now().timestamp())

    rooms: "OrderedDict[str, list[tree.DeviceNode]]" = OrderedDict()
    for n in nodes:
        rooms.setdefault(n.room or "未分房间", []).append(n)

    body: list[str] = []
    for room, items in rooms.items():
        online = sum(1 for n in items if n.online)
        on_cnt = 0
        for n in items:
            c = cards.get(n.did, {})
            if c.get("layout") == "switch":
                if any(bool(value_at(n.did, g["method"], g["name"], live, sample)) for g in c.get("gangs", [])):
                    on_cnt += 1
            elif value_at(n.did, None, "on", live, sample):
                on_cnt += 1

        t = h = None
        for n in items:
            if t is None:
                v = value_at(n.did, None, "temperature", live, sample)
                if isinstance(v, (int, float)):
                    t = v
            if h is None:
                v = value_at(n.did, None, "relative-humidity", live, sample)
                if isinstance(v, (int, float)):
                    h = v
        env = " · ".join(x for x in (f"{t:g}°C" if t is not None else "", f"{h:g}%" if h is not None else "") if x)

        body.append(
            f'<section><div class="rh"><h2>{esc(room)}</h2>'
            f'<span class="cnt">{len(items)} 台 · {on_cnt} 台运行 · {online} 在线</span>'
            f'<span class="env">{esc(env)}</span></div><div class="grid">'
        )
        for n in items:
            body.append(render_card(n, cards.get(n.did, {}), live, sample, now))
        body.append("</div></section>")

    total = len(nodes)
    cats = len({c.get("category") for c in cards.values() if c.get("category")})
    online = sum(1 for n in nodes if n.online)
    unknown = sum(1 for c in cards.values() if c.get("layout") == "unknown")

    doc = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>米家桌面端 · 卡片形态验证</title>
<style>{CSS}</style></head>
<body><div class="wrap">
<header>
  <h1>卡片形态验证</h1>
  <p>按品类绑定后的真实设备墙 · 读数取自 {dt.datetime.fromtimestamp(now):%m-%d %H:%M} 的云端回读，未填充任何假数据</p>
  <div class="stats">
    <div class="stat"><b>{total}</b><span>台真机</span></div>
    <div class="stat"><b>{cats}</b><span>个品类</span></div>
    <div class="stat"><b>{online}</b><span>在线</span></div>
    <div class="stat"><b>{len(rooms)}</b><span>个房间</span></div>
    <div class="stat"><b>{unknown}</b><span>仅展示</span></div>
  </div>
</header>
{''.join(body)}
<footer>
  卡片全部由品类映射生成：多键面板聚合成 1 张卡 + N 个按键（按键名来自 <code>did.sN</code> 虚拟设备，
  自动名如"左键-小米米家智能开关（三开单控）"折叠成"左键"）；灯带亮度条；空调给温控；传感器只显示读数。<br>
  右上角 <b>⋯N</b> 是"更多功能"入口，N 表示该设备未被卡片使用、但可写的属性数量 —— 参考项目是把这些全部铺在面板上。
</footer>
</div></body></html>"""
    OUT.write_text(doc, encoding="utf-8")
    print(f"写出 {OUT}  ({len(doc) // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
