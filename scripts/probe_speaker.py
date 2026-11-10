# SPDX-License-Identifier: GPL-3.0-or-later
"""探测：小爱音箱的「执行文本指令」与「唤醒」直调能不能用（**手动、会出声**）。

程序里已经按名字解析能力号（``core/speaker.py``），但"云端收不收、音箱动不动"只能
真机验一次。这个脚本就是那次验证，白天跑：

    # 1) 先看看家里哪几台支持、各自解析到什么 (siid, aiid)——只读，不出声
    .venv/Scripts/python.exe -X utf8 scripts/probe_speaker.py list

    # 2) 唤醒（会亮灯 / 可能回「我在」，约 1 秒）
    .venv/Scripts/python.exe -X utf8 scripts/probe_speaker.py wake --device 主卧

    # 3) 执行文本指令（默认**不静默**，会开口回答）
    .venv/Scripts/python.exe -X utf8 scripts/probe_speaker.py send "现在几点了" --device 主卧

    # 4) 静默执行（不发声，用来验"指令确实被收下了"而不打扰人）
    .venv/Scripts/python.exe -X utf8 scripts/probe_speaker.py send "音量调到 20" --silent

    # 0) 先只看要发什么、一个请求都不发（排"参数形状"用）
    .venv/Scripts/python.exe -X utf8 scripts/probe_speaker.py send "现在几点了" --dry

判据：``code 0`` **且**音箱真的有反应才算通。只听 code 不够——`silent-execution`
那一路 2026-11-11 实测过"零反应但 code 0"。

**``--dry`` 是排障第一站**：2026-11-11 真机报 ``-704220025``（参数个数不匹配），
根因是参数键写成了属性那套 ``value``；action 只认 ``in``。``--dry`` 能把实际 body
原样打出来，形状对不对一眼可见，且完全不碰设备。

避坑（见 TODO 的实测方案）：别用「播放音乐」这类长时间出声的指令；两条之间留 2 秒，
连续指令会被吞。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mihome.core import speaker
from mihome.core.api import Client, explain
from mihome.core.spec import SpecRepository


def _targets(client: Client) -> list[tuple[speaker.Target, object]]:
    """拉一次拓扑与规格，挑出所有支持执行文本指令的音箱（连着它的规格一起给）。"""
    topo = client.load_topology()
    nodes = list(topo.real_devices())
    specs = SpecRepository().get_many([n.spec_type for n in nodes], workers=4)
    out: list[tuple[speaker.Target, object]] = []
    for node in nodes:
        spec = specs.get(node.spec_type)
        got = speaker.target_for(
            node.did,
            node.name,
            node.model,
            spec,
            online=node.online,
            room=node.room,
        )
        if got is not None:
            out.append((got, spec))
    return out


def _pick(targets: list[speaker.Target], keyword: str) -> speaker.Target:
    if not targets:
        raise SystemExit("没有找到支持「执行文本指令」的音箱")
    if not keyword:
        online = [t for t in targets if t.online]
        if not online:
            raise SystemExit("所有音箱都离线")
        return online[0]
    hits = [t for t in targets if keyword in t.name]
    if not hits:
        names = "、".join(t.name for t in targets)
        raise SystemExit(f"没匹配到「{keyword}」。可选：{names}")
    if len(hits) > 1:
        print(f"匹配到 {len(hits)} 台，用第一台：{hits[0].name}（{hits[0].room}）")
        for t in hits:
            print(f"  候选：{t.name} 房间={t.room or '未分配'} did={t.did}")
    return hits[0]


def _run(client: Client, ref: tuple[str, int, int], value, note: str) -> int:
    began = time.monotonic()
    code = client.run_action(ref, value)
    cost = (time.monotonic() - began) * 1000
    print(f"{note} → code={code}  {cost:.0f} ms")
    if code != 0:
        print(f"  失败原因：{client.last_error or explain(code)}")
    return code


def _preview(target: speaker.Target, aiid: int, payload: list | None) -> None:
    """把**将要发出去**的 body 原样打出来，一个请求都不发。"""
    body: dict = {"did": target.did, "siid": target.siid, "aiid": aiid}
    if payload is not None:
        body["in"] = payload
    print("（--dry：只打印，不发送）")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="小爱音箱直调探针（会出声）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="只读：列出支持的音箱与解析出的能力号")
    for name, text in (("wake", "唤醒"), ("send", "执行文本指令")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--device", default="", help="按名字模糊匹配；不给就用第一台在线的")
        p.add_argument("--room", default="", help="只在某个房间（模糊匹配）里找，避免误动别人房间")
        p.add_argument("--dry", action="store_true", help="只打印将发送的 body，不真的发")
        if name == "send":
            p.add_argument("text", help="指令内容")
            p.add_argument("--silent", action="store_true", help="静默执行（不播放文字回复）")
    args = parser.parse_args()

    client = Client()
    if not client.available:
        print("未登录：先在程序里扫码，或确认凭据文件存在")
        return 2

    found = _targets(client)
    if args.cmd == "list":
        if not found:
            print("没有设备支持「执行文本指令」")
            return 1
        for target, spec in found:
            flag = "在线" if target.online else "离线"
            print(f"{target.name:<28} {target.model:<18} [{target.room or '未分配'}] {flag}  "
                  f"siid={target.siid} aiid={target.aiid} args={list(target.args)}")
            print(f"  服务 wake-up: {speaker.wake_ref(spec)}")
        return 0

    room = str(getattr(args, "room", "") or "")
    if room:
        found = [(t, s) for t, s in found if room in (t.room or "")]
        if not found:
            print(f"「{room}」里没有支持执行文本指令的音箱")
            return 1

    targets = [t for t, _ in found]
    spec_by_did = {t.did: s for t, s in found}
    target = _pick(targets, args.device)
    print(f"目标：{target.name}（{target.model}）房间={target.room or '未分配'} "
          f"siid={target.siid} aiid={target.aiid}")
    if not target.online:
        print("这台离线，先换一台（--device）或等它上线")
        return 1

    if args.cmd == "wake":
        ref = speaker.wake_ref(spec_by_did.get(target.did))
        if ref is None:
            print("这台没有 wake-up 能力")
            return 1
        if args.dry:
            _preview(target, ref[1], None)
            return 0
        code = _run(client, (target.did, ref[0], ref[1]), None,
                    f"wake-up {ref[0]}/{ref[1]}")
        return 0 if code == 0 else 1

    cleaned = speaker.clean(args.text)
    if cleaned.empty:
        print("清洗后是空的，不发")
        return 1
    if cleaned.truncated or cleaned.dropped:
        print(f"清洗：{cleaned.text!r}"
              + ("（已截断到 200）" if cleaned.truncated else "")
              + (f"（忽略 {cleaned.dropped}）" if cleaned.dropped else ""))
    payload = speaker.directive(target, cleaned.text, args.silent)
    if args.dry:
        _preview(target, target.aiid, payload)
        return 0
    code = _run(client, target.ref, payload,
                f"execute-text-directive {target.siid}/{target.aiid} in={payload!r}")
    return 0 if code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
