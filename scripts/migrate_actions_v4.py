# SPDX-License-Identifier: GPL-3.0-or-later
"""一次性迁移：把 ``rules.json`` 里 v3 的旧动作改写成 v4 的「设备 + 功能 + 参数」。

**这是一次性工具，不属于程序**（程序只认 v4，见 ``core/automation/rules.py`` 的
「只认当前结构」）。用户 11-14 确认本机只有一条测试规则，所以宁可在程序里不养兼容
分支，改用一个跑一次就丢的脚本来平移历史数据。

改写规则（纯字典搬运，不需要联网、不需要登录）::

    {"type": "virtual_event", "name": "X", "hub": "did", "hub_name": "名"}
      → {"type": "device_action", "did": "did", "device": "名",
         "action": "emit-virtual-event", "args": {"virtual-param": "X"}}

    {"type": "speaker", "did": "did", "name": "名", "text": "话"[, "silent": true]}
      → {"type": "device_action", "did": "did", "device": "名",
         "action": "execute-text-directive",
         "args": {"text-content": "话"[, "silent-execution": true]}}

认不出的动作（人手改过、或以后又改过结构）**原样留着并报出来**，不猜、不删——那种
数据交给人在编辑页重选一次，比脚本瞎猜安全。

跑法::

    .venv/Scripts/python.exe -X utf8 scripts/migrate_actions_v4.py --dry-run   # 先看
    .venv/Scripts/python.exe -X utf8 scripts/migrate_actions_v4.py            # 真写
    ... --path D:/some/rules.json    # 指定文件（默认用程序的 data_dir）

写盘前会把原文件复制成 ``rules.json.bak-<时间戳>``；写盘本身和程序一样是
「先 .tmp 再 os.replace」。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mihome.core import paths  # noqa: E402

RULES_FILE = "rules.json"


def convert(action: Any) -> tuple[dict[str, Any] | None, str]:
    """一条旧动作 → 新动作。返回 ``(新动作 或 None, 说明)``。"""
    if not isinstance(action, dict):
        return None, "不是字典，跳过"
    kind = str(action.get("type") or "")
    if kind == "delay":
        return None, "延迟不用改"
    if kind == "device_action":
        return None, "已经是 v4"
    if kind == "virtual_event":
        name = str(action.get("name") or "")
        did = str(action.get("hub") or "")
        device = str(action.get("hub_name") or "")
        if not did:
            return None, "旧「发虚拟事件」没记中枢 did，只能丢给人工重选"
        return {
            "type": "device_action", "did": did, "device": device,
            "action": "emit-virtual-event", "args": {"virtual-param": name},
        }, f"发虚拟事件「{name}」→ {device or did}"
    if kind == "speaker":
        did = str(action.get("did") or "")
        device = str(action.get("name") or "")
        text = str(action.get("text") or "")
        if not did:
            return None, "旧「发音箱指令」没记设备 did，只能丢给人工重选"
        args: dict[str, Any] = {"text-content": text}
        if action.get("silent"):
            args["silent-execution"] = True
        return {
            "type": "device_action", "did": did, "device": device,
            "action": "execute-text-directive", "args": args,
        }, f"发音箱指令「{text}」→ {device or did}"
    return None, f"认不出的动作类型「{kind or '空'}」，原样留着"


def walk(data: dict[str, Any]) -> tuple[list[str], int, int]:
    """就地改写所有规则的 actions / else_actions。返回 (日志行, 改了几条, 留给人工几条)。"""
    notes: list[str] = []
    changed = kept = 0
    rules = data.get("rules")
    if not isinstance(rules, list):
        return ["文件里没有 rules 数组"], 0, 0
    for i, rule in enumerate(rules, 1):
        if not isinstance(rule, dict):
            continue
        label = str(rule.get("name") or rule.get("id") or f"第 {i} 条")
        for key in ("actions", "else_actions"):
            acts = rule.get(key)
            if not isinstance(acts, list):
                continue
            out: list[Any] = []
            for act in acts:
                kind = str(act.get("type") or "") if isinstance(act, dict) else ""
                if kind in ("", "delay", "device_action"):
                    out.append(act)  # 延迟与 v4 动作不动
                    continue
                new, why = convert(act)
                if new is None:
                    kept += 1
                    notes.append(f"  ! 「{label}」{key}：{why}（程序会丢弃这一条）")
                    out.append(act)
                    continue
                changed += 1
                notes.append(f"  · 「{label}」{key}：{why}")
                out.append(new)
            rule[key] = out
    return notes, changed, kept


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把 rules.json 的旧动作改写成 v4 结构")
    ap.add_argument("--path", default="", help="rules.json 路径（默认程序的 data_dir）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写盘")
    args = ap.parse_args(argv)

    path = Path(args.path) if args.path else paths.data_dir() / RULES_FILE
    if not path.exists():
        print(f"找不到 {path}（程序还没建过规则文件？）")
        return 1
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"读不了 {path}：{exc}")
        return 1
    if not isinstance(data, dict):
        print(f"{path} 不是规则文件（顶层不是对象）")
        return 1

    notes, changed, kept = walk(data)
    print(f"文件：{path}")
    for line in notes:
        print(line)
    print(f"合计：改写 {changed} 条，留给人工 {kept} 条")
    if not changed:
        print("没有需要改写的动作。")
        return 0
    if args.dry_run:
        print("--dry-run：没有写盘。")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    print(f"已写入 {path}\n备份：{backup}")
    if kept:
        print("留给人工的那几条，在编辑页里重新选一次设备 / 功能即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
