# SPDX-License-Identifier: GPL-3.0-or-later
"""探测：米家 **App 自动化** 能不能读、**事件**能不能拉、**中枢网关的虚拟事件**能不能收发。

背景与结论（2026-11-10 实测）见 ``docs/reference/mijia-automation-events.md``。三条要点：

1. ``GetSceneList`` 能读到全家的自动化（含触发条件、动作、启用状态），手动场景也在里面，
   ``type`` 用 0 / 1 区分。数据在账号里，任何登录同一账号的客户端看到的是同一份。
2. ``/miotspec/event/get`` 是**事件拉取**接口：按 (did, siid, eiid) 返回该事件的
   **最近一次**发生，带 ``updateTime`` 与参数。这就是"事件通道"，只轮询属性拿不到它。
3. 中枢网关的「虚拟事件」不是私有协议，而是规格里的普通服务：
   ``siid 4 / aiid 1 {in: ["事件名"]}`` 产生，``siid 4 / eiid 1`` 上报。
   所以发虚拟事件 = 一次 ``prop/action``，**走云端、不需要局域网**。

用法：

    # 1) 看全家有哪些自动化（只读）
    .venv/Scripts/python.exe -X utf8 scripts/probe_automations.py list

    # 2) 盯事件（只读）：自动化里出现的触发源 + 中枢虚拟事件，变一次打一行
    .venv/Scripts/python.exe -X utf8 scripts/probe_automations.py events --interval 0.5

    # 3) 发一个虚拟事件（**写操作**，会真的触发监听该名字的自动化）
    .venv/Scripts/python.exe -X utf8 scripts/probe_automations.py emit 我的事件名

    # 4) 量「发出 → 云端可见」的往返（写一个唯一名字，不会命中任何自动化）
    .venv/Scripts/python.exe -X utf8 scripts/probe_automations.py latency

``events`` / ``list`` 只读；``emit`` / ``latency`` 会调一次网关动作。虚拟事件按名字精确匹配，
``latency`` 用的是随机名字，不会触发你家里任何自动化。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mihome.core.api import Client

SCENE_LIST = "/appgateway/miot/appsceneservice/AppSceneService/GetSceneList"
EVENT_GET = "/miotspec/event/get"

#: 中枢网关「虚拟服务」在规格里的坐标（``xiaomi.gateway.hub1`` spec v3 实测）
VIRTUAL_SERVICE = 4
VIRTUAL_ACTION = 1
VIRTUAL_EVENT = 1


def _homes(api) -> list[tuple[str, str, str]]:
    """``[(home_id, owner_uid, name)]``。"""
    out = []
    for home in api.get_homes_list():
        hid = str(home.get("id") or "")
        if not hid:
            continue
        out.append((hid, str(api._get_home_owner(hid)), str(home.get("name") or hid)))
    return out


def _scenes(api, hid: str, owner: str) -> list[dict]:
    # ``app_version`` / ``get_type`` 不能省：只传 home_id + owner_uid 会少给一部分手动场景
    # （实测 6 条 vs 10 条），自动化不受影响
    return list(
        api._request(
            SCENE_LIST,
            {"home_id": hid, "owner_uid": owner, "app_version": 12, "get_type": 2},
        )["scene_info_list"]
    )


def _trigger_text(t: dict) -> str:
    src = str(t.get("src") or "")
    key = str(t.get("key") or "")
    name = str(t.get("name") or "")
    extra = t.get("extra_json") or {}
    dev = str(extra.get("device_name") or "")
    val = t.get("value")
    val_txt = "" if val in (None, "", []) else f" = {val}"
    who = f"{dev} " if dev else ""
    return f"{who}[{src}.{key}] {name}{val_txt}".strip()


def _action_text(a: dict) -> str:
    pj = a.get("payload_json") or {}
    cmd = str(pj.get("command") or a.get("type") or "")
    dev = str(pj.get("device_name") or "")
    val = pj.get("value")
    if cmd == "set_properties" and isinstance(val, list):
        props = "、".join(
            f"{p.get('siid')}.{p.get('piid')}={p.get('value')}"
            for p in val
            if isinstance(p, dict)
        )
        return f"{dev} 设属性 {props}"
    if cmd == "action" and isinstance(val, dict):
        args = [x.get("value") for x in (val.get("in") or []) if isinstance(x, dict)]
        return f"{dev} 动作 {val.get('siid')}.{val.get('aiid')} {args}"
    if cmd == "tts":
        return f"{dev} 播报 {val}"
    return f"{cmd} {val if val is not None else ''}".strip()


def _event_refs(api, hid: str, owner: str) -> dict[tuple[str, int, int], str]:
    """自动化触发源里出现的 ``(did, siid, eiid)`` → 说明文字。"""
    refs: dict[tuple[str, int, int], str] = {}
    for scene in _scenes(api, hid, owner):
        for t in (scene.get("scene_trigger") or {}).get("triggers") or []:
            key = str(t.get("key") or "")
            did = str((t.get("extra_json") or {}).get("did") or "")
            if not (key.startswith("event.") and did):
                continue
            parts = key.split(".")
            if len(parts) != 3:
                continue
            refs[(did, int(parts[1]), int(parts[2]))] = (
                f"{(t.get('extra_json') or {}).get('device_name') or did} / {t.get('name') or key}"
            )
    return refs


def _hub_did(api) -> str:
    """名字带「中枢网关」或型号是 ``xiaomi.gateway.hub1`` 的那台。"""
    for home in api.get_homes_list():
        for dev in api._get_devices_list(str(home.get("id"))):
            if str(dev.get("model")) == "xiaomi.gateway.hub1" or "中枢网关" in str(
                dev.get("name") or ""
            ):
                return str(dev.get("did") or "")
    return ""


def _emit(api, did: str, name: str) -> tuple[bool, float, str]:
    t0 = time.perf_counter()
    try:
        res = api.run_action(
            {
                "did": did,
                "siid": VIRTUAL_SERVICE,
                "aiid": VIRTUAL_ACTION,
                "in": [name],
            }
        )
    except Exception as exc:  # noqa: BLE001
        return False, time.perf_counter() - t0, f"{type(exc).__name__}: {exc}"
    ms = (time.perf_counter() - t0) * 1000
    if isinstance(res, dict) and res.get("code") == 0:
        return True, ms, str(res.get("message") or "ok")
    return False, ms, json.dumps(res, ensure_ascii=False)[:200]


def _last_events(
    api, refs: list[tuple[str, int, int]]
) -> dict[tuple[str, int, int], tuple[object, int]]:
    """``{(did, siid, eiid): (参数列表, updateTime)}``；读不到的条目直接跳过。"""
    got: dict[tuple[str, int, int], tuple[object, int]] = {}
    if not refs:
        return got
    rows = api._request(
        EVENT_GET, {"params": [{"did": d, "siid": s, "eiid": e} for d, s, e in refs]}
    )
    for row in rows or []:
        key = (
            str(row.get("did") or ""),
            int(row.get("siid") or 0),
            int(row.get("eiid") or 0),
        )
        if row.get("code") not in (0, None):
            continue
        try:
            args = json.loads(row.get("value") or "[]")
        except (TypeError, ValueError):
            args = row.get("value")
        got[key] = (args, int(row.get("updateTime") or 0))
    return got


def _fmt_args(args: object) -> str:
    if isinstance(args, list):
        if not args:
            return "(无参数)"
        return "、".join(
            f"{a.get('piid')}={a.get('value')!r}" for a in args if isinstance(a, dict)
        )
    return str(args)


# -- 子命令 ---------------------------------------------------------------
def _is_manual(scene: dict) -> bool:
    """能不能在智能页手动点/语音执行：看触发器里有没有 ``user.click``。"""
    for t in (scene.get("scene_trigger") or {}).get("triggers") or []:
        if str(t.get("key")) == "user.click":
            return True
    return False


def cmd_list(_args) -> int:
    client = Client()
    if not client.available:
        print("没有可用的米家凭据，先运行主程序扫码登录一次")
        return 1
    api = client.raw()
    for hid, owner, hname in _homes(api):
        scenes = _scenes(api, hid, owner)
        # ``type`` 字段不能当"手动 / 自动化"的判据（0 和 1 两组里都混着两种），
        # 可靠判据是触发器里有没有 ``user.click``——这类正好等于 GetSimpleSceneList 那批
        manual = [s for s in scenes if _is_manual(s)]
        autos = [s for s in scenes if not _is_manual(s)]
        print(
            f"\n=== 家庭「{hname}」(home_id={hid})："
            f"自动化 {len(autos)} 条 / 可手动执行 {len(manual)} 条（共 {len(scenes)}）"
        )
        for tag, group in (("自动化", autos), ("可手动", manual)):
            for s in group:
                flags = "启用" if s.get("enable") else "停用"
                print(f"\n[{tag}·{flags}] {s.get('name')}")
                trig = (s.get("scene_trigger") or {}).get("triggers") or []
                mode = (
                    "任一条件"
                    if (s.get("scene_trigger") or {}).get("express") == 1
                    else "全部条件"
                )
                for t in trig:
                    print(f"    触发（{mode}）: {_trigger_text(t)}")
                for c in (s.get("scene_condition") or {}).get("conditions") or []:
                    print(f"    条件: {_trigger_text(c)}")
                for a in (s.get("scene_action") or {}).get("actions") or []:
                    print(f"    动作: {_action_text(a)}")
                for a in (s.get("scene_else_action") or {}).get("actions") or []:
                    print(f"    否则: {_action_text(a)}")
    return 0


def cmd_events(args) -> int:
    client = Client()
    if not client.available:
        print("没有可用的米家凭据，先运行主程序扫码登录一次")
        return 1
    api = client.raw()
    hid, owner, hname = _homes(api)[0]
    refs = _event_refs(api, hid, owner)
    hub = _hub_did(api)
    if hub:
        refs.setdefault(
            (hub, VIRTUAL_SERVICE, VIRTUAL_EVENT), f"{hub} / 中枢网关虚拟事件"
        )
    if not refs:
        print("这个家庭的自动化里没有事件类触发源，也没有找到中枢网关")
        return 1
    if args.did:
        refs = {k: v for k, v in refs.items() if k[0] == args.did}
    keys = list(refs)
    print(f"家庭「{hname}」，盯 {len(keys)} 个事件源，每 {args.interval:.2f}s 读一次：")
    for k in keys:
        print(f"  {k[0]:<24} {k[1]}.{k[2]:<5} {refs[k]}")
    print(
        "\n按 Ctrl+C 结束。去按一下实体按键、开一次门锁，或从米家 App 触发一个虚拟事件。\n",
        flush=True,
    )

    started = time.monotonic()
    seen: dict[tuple[str, int, int], int] = {}
    rounds = 0
    try:
        while args.seconds <= 0 or time.monotonic() - started < args.seconds:
            t0 = time.perf_counter()
            try:
                got = _last_events(api, keys)
            except Exception as exc:  # noqa: BLE001
                print(f"  读事件失败：{type(exc).__name__}: {exc}", flush=True)
                time.sleep(args.interval)
                continue
            rtt = (time.perf_counter() - t0) * 1000
            rounds += 1
            at = time.monotonic() - started
            for key, (ev_args, ts) in got.items():
                before = seen.get(key)
                if before is None:
                    seen[key] = ts
                    print(
                        f"{at:7.2f}s  基线  {refs[key]}  ← {_fmt_args(ev_args)}  "
                        f"（云端戳 {time.strftime('%H:%M:%S', time.localtime(ts)) if ts else '无'}）"
                    )
                    continue
                if ts <= before:
                    continue
                seen[key] = ts
                age = (time.time() - ts) if ts else 0.0
                print(
                    f"{at:7.2f}s  变化  {refs[key]}  → {_fmt_args(ev_args)}   "
                    f"云端戳 +{age:.2f}s   往返 {rtt:.0f}ms",
                    flush=True,
                )
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n（手动中断）")
    print(f"\n共 {rounds} 轮")
    return 0


def cmd_emit(args) -> int:
    client = Client()
    if not client.available:
        print("没有可用的米家凭据，先运行主程序扫码登录一次")
        return 1
    api = client.raw()
    hub = args.did or _hub_did(api)
    if not hub:
        print("账号里没找到中枢网关（model=xiaomi.gateway.hub1，或名字含「中枢网关」）")
        print("可以用 --did 指定承载中枢功能的那台设备")
        return 1
    ok, ms, msg = _emit(api, hub, args.name)
    print(
        f"{'成功' if ok else '失败'}：did={hub} 事件名「{args.name}」 往返 {ms:.0f}ms  {msg}"
    )
    return 0 if ok else 1


def cmd_latency(args) -> int:
    client = Client()
    if not client.available:
        print("没有可用的米家凭据，先运行主程序扫码登录一次")
        return 1
    api = client.raw()
    hub = args.did or _hub_did(api)
    if not hub:
        print("账号里没找到中枢网关，可用 --did 指定")
        return 1
    name = f"mihome-probe-{uuid.uuid4().hex[:10]}"
    key = (hub, VIRTUAL_SERVICE, VIRTUAL_EVENT)

    before = _last_events(api, [key]).get(key, ([], 0))[1]
    t0 = time.perf_counter()
    ok, ms, msg = _emit(api, hub, name)
    print(f"发出「{name}」：{'成功' if ok else '失败'}，动作往返 {ms:.0f}ms  {msg}")
    if not ok:
        return 1
    deadline = t0 + args.timeout
    while time.perf_counter() < deadline:
        got = _last_events(api, [key]).get(key)
        if (
            got
            and got[0]
            and str((got[0] or [{}])[0].get("value")) == name
            and got[1] > before
        ):
            print(
                f"云端可见：发出后 {(time.perf_counter() - t0) * 1000:.0f}ms"
                f"（查询间隔 {args.interval:.2f}s，所以真实值在两者之间）"
            )
            return 0
        time.sleep(args.interval)
    print(f"{args.timeout:.0f}s 内没读到这次事件，检查网关在线状态")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="米家自动化 / 事件 / 中枢虚拟事件探测")
    sub = ap.add_subparsers(dest="cmd")

    p_list = sub.add_parser("list", help="列出全家的自动化与手动场景（只读）")
    p_list.set_defaults(func=cmd_list)

    p_ev = sub.add_parser("events", help="盯事件变化（只读）")
    p_ev.add_argument(
        "--interval", type=float, default=0.5, help="轮询间隔，秒（默认 0.5）"
    )
    p_ev.add_argument(
        "--seconds", type=float, default=0.0, help="跑多久，秒（0=一直跑）"
    )
    p_ev.add_argument("--did", default="", help="只看某台设备的事件")
    p_ev.set_defaults(func=cmd_events)

    p_emit = sub.add_parser("emit", help="发一个虚拟事件（写操作）")
    p_emit.add_argument("name", help="事件名，必须和自动化里配的完全一致")
    p_emit.add_argument("--did", default="", help="中枢网关 did（默认自动找）")
    p_emit.set_defaults(func=cmd_emit)

    p_lat = sub.add_parser("latency", help="量发出→云端可见的延迟（写一个唯一名字）")
    p_lat.add_argument(
        "--interval", type=float, default=0.2, help="查询间隔，秒（默认 0.2）"
    )
    p_lat.add_argument(
        "--timeout", type=float, default=6.0, help="最长等多久，秒（默认 6）"
    )
    p_lat.add_argument("--did", default="", help="中枢网关 did（默认自动找）")
    p_lat.set_defaults(func=cmd_latency)

    args = ap.parse_args()
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
