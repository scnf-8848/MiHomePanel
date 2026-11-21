# SPDX-License-Identifier: GPL-3.0-or-later
"""看设备档案与变更流（开发者工具，不属于发行包）。

程序在每次启动拉拓扑时顺手记一份设备档案（``core/device_catalog.py``），
这里只负责把 ``<data>/devices/`` 里的两份文件读成人能看的文字。
**界面上没有任何入口**——它是排查用的，不是给日常使用看的。

用法::

    python scripts/device_history.py                  # 当前档案全表
    python scripts/device_history.py --model linp      # 只看某个型号（子串匹配）
    python scripts/device_history.py --changes        # 全部变更流，旧 → 新
    python scripts/device_history.py --changes 20     # 最近 20 条
    python scripts/device_history.py --changes --since 7   # 最近 7 天

排查姿势：某台设备"表现突然不对"时先跑 ``--changes --since 7``，如果最近有一条
``固件`` 变更落在它头上，方向就明确了；没有变更再去看别的原因（离线、规格漂移、
适配层没覆盖）。

数据目录跟随程序：默认 ``%LOCALAPPDATA%\\MiHomePanel\\devices``，可用
``MIHOME_DATA_DIR`` 指到别处。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mihome.core import device_catalog  # noqa: E402


def _when(ts: object) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError):
        return "?"


def _spec_brief(spec_type: str) -> str:
    """``urn:miot-spec-v2:device:switch:0000A003:linp-e3sbw3:1:0000C810``
    → ``linp-e3sbw3:v1``。第三个模型段之后的版本号才是"规格版本"，
    整串留着会把表格撑爆，而排查时关心的只是"型号 + 版本变了没有"。
    """
    parts = (spec_type or "").split(":")
    if len(parts) >= 7:
        return f"{parts[5]}:v{parts[6]}"
    return spec_type or "-"


def _row(name: str, model: str, fw: str, mcu: str, ble: str, spec: str) -> str:
    bits = [f"固件={fw or '-'}"]
    if mcu:
        bits.append(f"MCU={mcu}")
    if ble:
        bits.append(f"蓝牙={ble}")
    bits.append(f"规格={_spec_brief(spec)}")
    return f"  {name or '(无名)'}  [{model}]  " + "  ".join(bits)


def show_catalog(cat: device_catalog.Catalog, model_filter: str) -> int:
    devices = cat.load()
    if not devices:
        print("还没有档案。跑一次程序（或任意会拉拓扑的入口）之后再来。")
        return 1
    items = [v for v in devices.values() if model_filter in str(v.get("model") or "")]
    if not items:
        print(f"没有匹配 “{model_filter}” 的设备。")
        return 1
    items.sort(key=lambda v: (str(v.get("model") or ""), str(v.get("name") or "")))
    print(f"共 {len(items)} 台（档案里 {len(devices)} 台）")
    for v in items:
        print(
            _row(
                str(v.get("name") or ""),
                str(v.get("model") or ""),
                str(v.get("fw") or ""),
                str(v.get("mcu") or ""),
                str(v.get("ble") or ""),
                str(v.get("spec_type") or ""),
            )
        )
        print(
            f"      首次 {_when(v.get('first_seen'))}   最近确认 {_when(v.get('last_seen'))}"
        )
    return 0


def show_changes(cat: device_catalog.Catalog, limit: int | None, model_filter: str, since: int) -> int:
    events = cat.changes()
    if since:
        cutoff = time.time() - since * 86400
        events = [e for e in events if float(e.get("at") or 0) >= cutoff]
    if model_filter:
        events = [e for e in events if model_filter in str(e.get("model") or "")]
    if limit:
        events = events[-limit:]
    if not events:
        print("变更流为空（或这个范围内没有变更）。")
        return 0
    for e in events:
        kind = str(e.get("kind") or "?")
        when = _when(e.get("at"))
        who = f"{e.get('name') or '(无名)'}（{e.get('model') or '?'}）"
        if kind == "changed":
            field = str(e.get("field") or "")
            label = device_catalog.FIELD_LABEL.get(field, field)
            old = e.get("old") or "(无)"
            new = e.get("new") or "(无)"
            print(f"{when}  {label:<8} {who}  {old} → {new}")
        elif kind == "added":
            extra = f"固件={e.get('fw') or '-'}  规格={_spec_brief(str(e.get('spec_type') or ''))}"
            print(f"{when}  新增     {who}  {extra}")
        elif kind == "removed":
            print(f"{when}  移除     {who}")
        elif kind == "suspect":
            print(
                f"{when}  采集存疑  上次 {e.get('before')} 台、这次 {e.get('after')} 台"
                f"（少 {e.get('missing')}），未记录移除"
            )
        else:
            print(f"{when}  {kind}  {who}")
    print(f"\n共 {len(events)} 条")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="看设备档案与固件变更流")
    ap.add_argument(
        "--changes",
        nargs="?",
        type=int,
        const=0,
        default=None,
        help="显示变更流；带数字则只显示最近 N 条",
    )
    ap.add_argument("--model", default="", help="按型号子串过滤")
    ap.add_argument("--since", type=int, default=0, help="只显示最近 N 天的变更")
    args = ap.parse_args()

    cat = device_catalog.Catalog()
    print(f"档案目录：{cat.root}")
    print()
    if args.changes is None:
        return show_catalog(cat, args.model)
    return show_changes(cat, args.changes or None, args.model, args.since)


if __name__ == "__main__":
    raise SystemExit(main())
