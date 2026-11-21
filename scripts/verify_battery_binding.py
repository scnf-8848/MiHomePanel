# SPDX-License-Identifier: GPL-3.0-or-later
"""门锁「两块电池」读数行的回归（型号级适配）。

背景：小米智能门锁 4 Pro（``xiaomi.lock.s1``）装两块电池——锂电池（主用，54%）+
5 号干电池（备用，100%）。规格里两块都叫 ``battery-level``、所在服务都叫
``battery``，``spec.find`` 只能按 siid/piid 升序取一条 → 干电池 siid 6 排在锂电
siid 13 前面，卡片一直显示备用的 100%。

做法（2026-11-16 定稿）：**规则按型号写**，`adapters/xiaomi.py` 里给这一款登记
``batteries=(("13.1", "锂电池"), ("6.1003", "干电池"))``，``catalog.apply_adapter``
是型号适配的唯一入口（电量拆行实现在同文件的 ``_expand_batteries``）。理由见
`docs/reference/lock-dual-battery.md`：

* 两块电池属性名/服务名完全一样，能定位的只有 ``siid.piid``；
* "哪块是哪块、哪块排前面"是设备物理形态（4 Pro = 锂电 + 干电池，V6 Max =
  主电池 + 猫眼电池），规格里没有结构性字段能说明，猜不出来；
* 试过"唯一带 ``charging-state`` 的是主电池"，用户当场给了反例（V6 Max 两块都是
  可充电锂电），判据语义不成立。

顺序是功能不是排版：``summary_line`` 只取前两个有值的指标且不带标签，第一条进
卡片副标题，"首要看锂电池"靠排第一实现。

跑法：``PYTHONPATH=. .venv/Scripts/python.exe -X utf8 scripts/verify_battery_binding.py``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mihome.core import paths  # noqa: E402
from mihome.core.adapters import MODEL_ADAPTERS  # noqa: E402
from mihome.core.adapters.base import check_batteries  # noqa: E402
from mihome.core.catalog import apply_adapter, build_card, summary_line  # noqa: E402
from mihome.core.spec import parse_raw  # noqa: E402

FAILS: list[str] = []

PRO = "xiaomi.lock.s1"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAILS.append(f"{name} {detail}")


def check_eq(name: str, got, want) -> None:
    check(name, got == want, f"{got!r}（期望 {want!r}）")


# ---------------------------------------------------------------------------
# 合成规格：服务 / 属性名与米家规格同形（4 Pro 的 siid 坐标）
# ---------------------------------------------------------------------------
DEV = "synthetic-lock"


def _svc(iid: int, name: str, desc: str, props: list[tuple[int, str]]) -> dict:
    return {
        "iid": iid,
        "type": f"urn:miot-spec-v2:service:{name}:00007800:{DEV}:1",
        "description": desc,
        "properties": [
            {
                "iid": piid,
                "type": f"urn:miot-spec-v2:property:{pname}:00000000:{DEV}:1",
                "description": pname,
                "format": "uint8",
                "access": ["read", "notify"],
                "unit": "percentage" if pname == "battery-level" else None,
                "value-range": [0, 100, 1] if pname == "battery-level" else None,
            }
            for piid, pname in props
        ],
    }


def make_spec(dry: bool = True, lithium: bool = True) -> object:
    """造一台跟 4 Pro 同坐标的门锁：干电池 6.1003、锂电池 13.1。"""
    services = [
        {
            "iid": 3,
            "type": f"urn:miot-spec-v2:service:door:00007856:{DEV}:1",
            "description": "Door",
            "properties": [
                {
                    "iid": 1021,
                    "type": f"urn:miot-spec-v2:property:door-state:00000000:{DEV}:1",
                    "description": "door-state",
                    "format": "uint8",
                    "access": ["read", "notify"],
                }
            ],
        }
    ]
    if dry:
        services.append(_svc(6, "battery", "Dry Battery", [(1003, "battery-level")]))
    if lithium:
        services.append(_svc(13, "battery", "Lithium Battery", [(1, "battery-level")]))
    raw = {
        "type": f"urn:miot-spec-v2:device:lock:0000A038:{DEV}:1",
        "description": "Lock",
        "services": services,
    }
    return parse_raw(raw)


def rows(spec, model: str = PRO) -> list[tuple[str, str, str]]:
    """走 build_card 的真实路径，返回电量行 ``[(slot, label, 方法号)]``。"""
    card = build_card(spec, did="x", model=model)
    return [
        (m.slot, m.label, m.prop.method if m.prop is not None else "—")
        for m in card.metrics
        if m.slot == "battery" or m.slot.startswith("battery_")
    ]


# ---------------------------------------------------------------------------
print("== 1. 适配表：只有 4 Pro 这一款登记了拆行 ==")
check_eq("4 Pro 的规则 = 锂电在前、干电池在后",
         MODEL_ADAPTERS[PRO].batteries, (("13.1", "锂电池"), ("6.1003", "干电池")))
others = [m for m, a in MODEL_ADAPTERS.items() if a.batteries and m != PRO]
check("其它型号一条都没登记（V6 Max / t1pro 回到老行为）", not others, str(others))

print("\n== 2. 4 Pro：两块都显示，锂电在前 ==")
spec = make_spec()
got = rows(spec)
check_eq("行数 = 2", len(got), 2)
check_eq("第一行是锂电 13.1", got[0], ("battery", "锂电池", "13.1"))
check_eq("第二行是干电池 6.1003", got[1], ("battery_1", "干电池", "6.1003"))

print("\n== 3. 副标题：首要看锂电，靠的是顺序 ==")
card = build_card(spec, did="x", model=PRO)
heads = [m.slot for m in card.metrics if m.prop is not None][:2]
check_eq("前两个读数行 = 门状态、电量（与改前一致的排版）", heads, ["door_state", "battery"])
vals = {m.prop.key: (54 if m.prop.siid == 13 else 100)
        for m in card.metrics
        if m.prop is not None and m.slot.startswith("battery")}
text = summary_line(card, vals)
check_eq("没有门状态读数时两格都填，锂电在前", text.split(" · "), ["54%", "100%"])
door = next(m for m in card.metrics if m.slot == "door_state")
vals[door.prop.key] = 1
text = summary_line(card, vals)
check("有门状态时是「已上锁 · 54%」，干电池被截掉",
      text.split(" · ")[1:] == ["54%"] and "100%" not in text, repr(text))

print("\n== 4. 没登记的型号：一行都不多（V6 Max / t1pro 的回退）==")
check_eq("同一份规格、换成没登记的型号 → 回到单条 find（干电池 6.1003）",
         rows(spec, model="loock.lock.401"), [("battery", "电量", "6.1003")])
check_eq("model 为空 → 也不拆", rows(spec, model=""), [("battery", "电量", "6.1003")])

# 2026-11-17：型号适配收成一个入口（catalog.apply_adapter）之后，直接把 None
# 喂进去也不能动任何字段——这是"别的设备不会误用这台规则"的最后一道保险，
# 比走 build_card 更能定位到出错的那一层。
plain = build_card(spec, did="x", model="loock.lock.401")
before = (
    [(m.slot, m.label) for m in plain.metrics],
    [p.name for p in plain.extra],
    plain.mode_active,
    dict(plain.speed_by_mode),
    [p.name for p in plain.advanced],
)
apply_adapter(spec, plain, None)
after = (
    [(m.slot, m.label) for m in plain.metrics],
    [p.name for p in plain.extra],
    plain.mode_active,
    dict(plain.speed_by_mode),
    [p.name for p in plain.advanced],
)
check_eq("apply_adapter(…, None) 一个字段都不碰", after, before)

print("\n== 5. 规格少一块（固件变更）：行数跟着少，标签绝不张冠李戴 ==")


def drop(siid: int):
    """把某个服务整个拿掉，模拟固件换了规格版本。"""
    return parse_raw({
        "type": f"urn:miot-spec-v2:device:lock:0000A038:{DEV}:1",
        "description": "Lock",
        "services": [
            {"iid": s.siid, "type": s.urn, "description": s.description,
             "properties": [{"iid": p.piid, "type": p.urn, "description": p.description,
                             "format": p.format, "access": list(p.access),
                             "unit": p.unit,
                             "value-range": list(p.value_range) if p.value_range else None}
                            for p in s.properties]}
            for s in make_spec().services
            if s.siid != siid
        ],
    })


check_eq("只有锂电（干电池 6 没了）→ 一行，仍叫「锂电池」",
         rows(drop(6)), [("battery", "锂电池", "13.1")])
check_eq("只有干电池（锂电 13 没了）→ 一行，仍叫「干电池」，没冒充成锂电",
         rows(drop(13)), [("battery", "干电池", "6.1003")])

print("\n== 7. 适配层校验：写错导入期就报错 ==")
for bad, why in (
    ((("battery-level", "锂电池"),), "定位写成属性名"),
    ((("13", "锂电池"),), "定位少一半"),
    ((("13.1", "锂电池"), ("13.1", "干电池")), "同一坐标写两遍"),
    ((("13.1", ""),), "行名为空"),
):
    try:
        check_batteries("test", bad)
    except ValueError:
        ok = True
    else:
        ok = False
    check(f"拦下{why}", ok)

print("\n== 8. 旧写法在同一用例上必须漏掉一块（防判据永真）==")
old = spec.find("battery-level")
check("旧的单条 find 只能拿到 6.1003，看不到锂电", old.method == "6.1003" and len(rows(spec)) == 2,
      f"旧 {old.method} / 新 {len(rows(spec))} 行")

print("\n== 9. 真机规格快照（读本地缓存，不联网、不读设备）==")
EXPECT = {
    "urn:miot-spec-v2:device:lock:0000A038:xiaomi-s1:3":
        ("xiaomi.lock.s1", [("battery", "锂电池", "13.1"), ("battery_1", "干电池", "6.1003")]),
    "urn:miot-spec-v2:device:lock:0000A038:loock-401:2":
        ("loock.lock.401", [("battery", "电量", "4.1003")]),
    "urn:miot-spec-v2:device:lock:0000A038:loock-t1pro:1":
        ("loock.lock.t1pro", [("battery", "电量", "4.1")]),
}
cache = paths.spec_cache_dir()
found = {}
for f in sorted(cache.glob("*.json")) if cache.is_dir() else []:
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        continue
    raw = data.get("raw") or {}
    urn = data.get("urn") or raw.get("type") or ""
    if urn in EXPECT:
        found[urn] = parse_raw(raw)

if not found:
    print("SKIP  本地没有门锁规格缓存（换机器 / 清过缓存），跳过真机快照")
for urn, (model, want) in EXPECT.items():
    cached = found.get(urn)
    if cached is None:
        print(f"SKIP  {urn.rsplit(':', 2)[-2]} 不在缓存里")
        continue
    got = rows(cached, model=model)
    check(f"{urn.rsplit(':', 2)[-2]:<12} → {len(want)} 行", got == want, f"实得 {got}")

print()
if FAILS:
    print(f"{len(FAILS)} 项失败：")
    for f in FAILS:
        print("  -", f)
    raise SystemExit(1)
print("全部通过")
