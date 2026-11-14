# SPDX-License-Identifier: GPL-3.0-or-later
"""造一份**假的**触发日志，用来手测日志窗（分页 / 列宽 / 折叠 / 筛选）。

真机手测时"按几下键"根本攒不出几百条历史，而日志窗的分页、按页算列宽、长文本折叠
都要有量才看得出来。这个脚本按 ``logstore`` 的格式造一份 ``automation_log.jsonl``：

    # 先写到临时目录看看长什么样（不碰真数据）
    .venv/Scripts/python.exe -X utf8 scripts/make_fake_automation_log.py --out D:/tmp/demo.jsonl

    # 直接给程序手测用：写到真实数据目录（**写前自动备份原文件**）
    .venv/Scripts/python.exe -X utf8 scripts/make_fake_automation_log.py `
        --out "$env:LOCALAPPDATA/MiHomePanel/automation_log.jsonl"

    # 想要更多 / 更少的量
    ... --entries 900

**`--out` 是必填的，故意不设默认值**：这个脚本最早默认直接覆盖真实日志，虽然会备份，
但"一执行就动用户数据"不该是默认行为。要手测就把路径显式指过去。

几条要注意的：

* 程序**只在启动时**读这份文件（``AutomationService`` 构造时 ``restore_log``），
  所以换完文件要**重启程序**才看得到；程序开着的时候按快捷键也会往同一个文件里追加。
* 进程内上限是 ``logstore.MAX_ENTRIES = 500`` 条，写多了也只读回最后 500 条。
  一页 50 行（一次触发一行），所以 500 条大约 3~5 页——够测翻页了。
* 写之前会把原文件复制成 ``<文件名>.bak-<时间戳>``，``--no-backup`` 可关。
  想恢复：把 ``.bak-*`` 改回原名即可（同样要重启程序），确认无误后再删备份。
* 规则 id 是假的（``fake-*``），不会出现在 ``rules.json`` 里；日志窗的规则下拉会从
  历史条目里把这些名字补进来，所以照样能按"规则"筛。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import time
from pathlib import Path

#: (规则 id, 规则名, 快捷键)——名字/键位都刻意做长短不一，好看出列宽差异
RULES: tuple[tuple[str, str, str], ...] = (
    ("fake-home", "进门播报", "Ctrl+Alt+F7"),
    ("fake-leave", "离开时关灯", "Ctrl+Alt+F8"),
    ("fake-movie", "客厅影院模式", "Ctrl+Shift+Space"),
    ("fake-quiet", "夜间静音", "F3"),
    ("fake-long", "下班回家自动开灯并播报天气路况外加一句晚安的一条特别长的规则名",
     "Ctrl+Alt+Shift+F12"),
    ("fake-hub", "客厅中枢 · 虚拟事件测试", "Ctrl+Alt+K"),
)

#: 每条规则被抽中的权重。**长名字那条刻意给 1**：它只该出现在某一页上，
#: 这样翻页时能看出"列宽是按页算的、别的页不会被它撑宽"（全都撑开就看不出区别了）
RULE_WEIGHTS: tuple[int, ...] = (26, 22, 18, 16, 1, 17)

#: 正常动作步骤。**大部分是短文案，长的按 12% 掺**：真实日志里绝大多数就是
#: "唤醒 → 「小爱音箱Pro」"这种，长播报词 / 长文本指令只是偶尔出现。如果每条都长，
#: 「说明」列会整页都撑到 700px、横向滚动条常驻，反而看不出"短的时候右边不留空白"。
STEPS_SHORT: tuple[str, ...] = (
    "唤醒 → 「小爱音箱Pro」",
    "播报文字 → 「小爱音箱Pro」（中午好）",
    "发送虚拟事件 → 「客厅中枢」（回家模式）",
    "延迟 500 ms",
    "延迟 1.5 秒",
    "延迟 30 秒",
)
STEPS_LONG: tuple[str, ...] = (
    "播报文字 → 「小爱音箱Pro」（今天的天气不错，最高气温二十二度，傍晚有阵雨，"
    "出门记得带伞，路上注意安全）",
    "执行文本指令 → 「客厅音箱」（把客厅的灯关掉，把空调调到二十六度，"
    "顺便播放一张轻音乐专辑）",
)

#: 失败说明（回执报错 / 没发出去两种口径，文案都长）
FAILED_TEXTS: tuple[str, ...] = (
    "播报文字未成功（「小爱音箱Pro」）：云端超时（回执报错，code -704220025）",
    "唤醒未成功（「小爱音箱Pro」）：设备当前离线（没发出去）",
    "执行文本指令未成功（「客厅音箱」）：参数个数对不上（多半是参数没送到）"
    "（回执报错，code -704220025）",
    "发送虚拟事件未成功（「客厅中枢」）：还没有打开【局域网通信协议】授权（没发出去）",
)

SINGLE_TEXTS: tuple[str, ...] = (
    "冷静期拦截，剩余 0.6 秒（本规则 1000 ms）",
    "冷静期拦截，剩余 0.9 秒（本规则 1500 ms）",
    "上一串动作还没跑完，本次触发已丢弃（不排队）",
    "条件（全部）不成立；进程「WeChat.exe」未运行",
    "条件（任一）不成立；前台窗口不是「PotPlayer」，已锁屏",
)


def _run_id(rule_id: str, at: float, seq: int) -> str:
    return f"{rule_id}@{int(at * 1000)}-{seq}"


def _entry(at: float, rule: tuple[str, str, str], outcome: str, detail: str,
           run: str = "") -> dict:
    out = {
        "at": round(at, 3),
        "rule_id": rule[0],
        "rule_name": rule[1],
        "keys": rule[2],
        "outcome": outcome,
        "detail": detail,
    }
    if run:
        out["run"] = run
    return out


def build(entries: int, seed: int = 20261115) -> list[dict]:
    """造出"时间正序"的日志（与 ``engine.log`` 同序，日志窗会自己倒过来显示）。"""
    rnd = random.Random(seed)
    out: list[dict] = []
    now = time.time()
    # 从三天前开始，一路排到刚才
    span = 3 * 24 * 3600
    step_at = now - span
    seq = 0
    used_long = False
    while len(out) < entries:
        # 长名字那条**只造一次**：让它单独落在一页上，翻到那一页才能看出"列宽按页算"
        if not used_long and rnd.random() < 0.02:
            rule = RULES[4]
            used_long = True
        else:
            rule = rnd.choices(RULES[:4] + RULES[5:],
                               weights=RULE_WEIGHTS[:4] + RULE_WEIGHTS[5:])[0]
        step_at += rnd.uniform(240, 2400)        # 两次触发之间隔几分钟到半小时
        if step_at > now - 30:
            step_at = now - rnd.uniform(1, 30)
        seq += 1
        run = _run_id(rule[0], step_at, seq)
        kind = rnd.choices(
            ("ok", "failed", "else", "cooldown", "reentry", "condition"),
            weights=(30, 12, 8, 26, 12, 12),
        )[0]

        if kind in ("cooldown", "reentry", "condition"):
            # 拦截类没有批次号：一条记录自己占一行（与引擎一致）
            detail = {
                "cooldown": rnd.choice(SINGLE_TEXTS[:2]),
                "reentry": SINGLE_TEXTS[2],
                "condition": rnd.choice(SINGLE_TEXTS[3:]),
            }[kind]
            out.append(_entry(step_at, rule, kind, detail))
            continue

        head = {"ok": "fired", "failed": "fired", "else": "else"}[kind]
        head_detail = "热键触发" if kind != "else" else "条件（全部）不成立；改走否则动作"
        out.append(_entry(step_at, rule, head, head_detail, run))

        n_steps = rnd.randint(1, 4)
        offset = 0.0
        for i in range(n_steps):
            offset += rnd.uniform(0.01, 0.4)
            text = rnd.choice(STEPS_LONG) if rnd.random() < 0.03 else rnd.choice(STEPS_SHORT)
            if text.startswith("延迟"):
                # 延迟那条与引擎文案一致（按用户填的刻度写）
                out.append(_entry(step_at + offset, rule, "sent", text, run))
                continue
            out.append(_entry(step_at + offset, rule, "sent", text, run))
            if kind == "failed" and i == n_steps - 1:
                out.append(_entry(step_at + offset + 0.02, rule, "failed",
                                  rnd.choice(FAILED_TEXTS), run))
        out.append(_entry(step_at + offset + 0.05, rule, "done",
                          f"{'否则' if kind == 'else' else ''}动作已发出（共 {n_steps} 步）",
                          run))
    return out[:entries]


def write(rows: list[dict], path: Path, *, backup: bool = True) -> Path | None:
    """写 JSONL（先 .tmp 再替换，同 rules.json 的写法）。返回备份文件路径。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    bak = None
    if backup and path.exists():
        bak = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, bak)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                   encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return bak


def main() -> int:
    ap = argparse.ArgumentParser(description="造一份假的触发日志（automation_log.jsonl）")
    ap.add_argument("--out", required=True,
                    help="写到哪个文件。**必填**：要直接手测就指到 "
                         "<data>/automation_log.jsonl（脚本会先备份原文件）；"
                         "写别处只用于离线预览")
    ap.add_argument("--entries", type=int, default=500,
                    help="写多少条（进程内上限 500，写多了也只读回最后 500 条）")
    ap.add_argument("--seed", type=int, default=20261115, help="随机种子，便于复现")
    ap.add_argument("--no-backup", action="store_true", help="不备份原文件")
    args = ap.parse_args()

    rows = build(max(10, args.entries), args.seed)
    path = Path(args.out)
    bak = write(rows, path, backup=not args.no_backup)

    triggers = sum(1 for r in rows if "run" in r and r["outcome"] in ("fired", "else"))
    singles = sum(1 for r in rows if "run" not in r)
    span_h = (rows[-1]["at"] - rows[0]["at"]) / 3600 if rows else 0
    print(f"已写入 {path}")
    print(f"  条数 {len(rows)}（触发 {triggers} 次 + 独立记录 {singles} 条，约 "
          f"{(triggers + singles + 49) // 50} 页）")
    print(f"  时间跨度 {span_h:.1f} 小时（{time.strftime('%m-%d %H:%M', time.localtime(rows[0]['at']))}"
          f" → {time.strftime('%m-%d %H:%M', time.localtime(rows[-1]['at']))}）")
    print(f"  备份 {bak}" if bak else "  原文件不存在，未做备份")
    print("重启程序后生效（日志只在启动时读取）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
