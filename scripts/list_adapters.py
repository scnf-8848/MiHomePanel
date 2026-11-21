# SPDX-License-Identifier: GPL-3.0-or-later
"""打印「已适配哪些型号」的总清单。

为什么要有它：型号适配规则散在 ``mihome/core/adapters/<厂商>.py`` 里，不翻到那个文件
就不知道某台设备是否已经适配过——结果是重复适配、或者别的开发者（拿到 GPL 源码想适配
自己的门锁）找不到同类先例。这个脚本把 :func:`adapters.describe_all` 的结果打出来，
代码是唯一事实来源，清单永远与代码同步。

用法：

    .venv/Scripts/python.exe scripts/list_adapters.py          # 详细（含原因）
    .venv/Scripts/python.exe scripts/list_adapters.py --md     # Markdown 表格（贴台账用）
    .venv/Scripts/python.exe scripts/list_adapters.py --vendor xiaomi

台账文档：``docs/reference/model-adapters.md``（`verify_adapters.py` 会检查它没漏型号）。
"""

from __future__ import annotations

import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mihome.core.adapters import FIELD_LABELS, describe_all  # noqa: E402


def width(text: str) -> int:
    """终端显示宽度：中日韩全角算 2 格。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, cells: int) -> str:
    return text + " " * max(0, cells - width(text))


def detail(rows: list[dict]) -> None:
    print(f"已适配型号 {len(rows)} 台（规则在 mihome/core/adapters/<厂商>.py）\n")
    vendor = ""
    for row in rows:
        if row["vendor"] != vendor:
            vendor = str(row["vendor"])
            print(f"[{vendor}]")
        fields = "、".join(FIELD_LABELS.get(f, f) for f in row["fields"])
        print(f"  {row['device']}  ({row['model']})")
        print(f"      适配：{fields}")
        print(f"      原因：{row['note']}")
        print(f"      文档：{row['doc'] or '—'}")
        print()
    print("要给自己那台设备加规则：找同厂商/同形态的先例，照字段加一条，"
          "别改通用逻辑。步骤见 docs/reference/model-adapters.md。")


def markdown(rows: list[dict]) -> None:
    print("| 型号 | 设备 | 适配内容 | 为什么 | 文档 |")
    print("|---|---|---|---|---|")
    for row in rows:
        fields = "、".join(FIELD_LABELS.get(f, f) for f in row["fields"])
        doc = f"[{Path(row['doc']).name}]({row['doc'].removeprefix('docs/reference/')})" \
            if row["doc"].startswith("docs/reference/") else (row["doc"] or "—")
        print(f"| `{row['model']}` | {row['device']} | {fields} | {row['note']} | {doc} |")


def main(argv: list[str]) -> int:
    rows = describe_all()
    if "--vendor" in argv:
        want = argv[argv.index("--vendor") + 1]
        rows = [r for r in rows if r["vendor"] == want]
        if not rows:
            print(f"没有 {want} 厂商的适配记录", file=sys.stderr)
            return 1
    if "--md" in argv:
        markdown(rows)
    else:
        detail(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
