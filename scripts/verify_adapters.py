# SPDX-License-Identifier: GPL-3.0-or-later
"""型号适配台账的一致性回归。

防的是"加了规则但没人知道"和"记了台账但代码里没有"两类漂移。台账文档
``docs/reference/model-adapters.md`` 第一节（已适配）与 ``MODEL_ADAPTERS`` 必须
**互相覆盖**：漏一个型号就失败，多一个也失败。

这套检查存在的原因：适配规则散在 ``adapters/<厂商>.py``，台账是给人看的索引。
索引一过期，下一个人就会把同一台设备再适配一遍，或者照着不存在的规则找代码。

跑法：``PYTHONPATH=. .venv/Scripts/python.exe -X utf8 scripts/verify_adapters.py``
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mihome.core.adapters import (  # noqa: E402
    FIELD_LABELS,
    MODEL_ADAPTERS,
    ModelAdapter,
    TextLayer,
    check_adapter,
    describe_all,
)

FAILS: list[str] = []

LEDGER = ROOT / "docs/reference/model-adapters.md"
#: 台账第一节里的型号（行首的表格单元格里用反引号包住）
MODEL_RE = re.compile(r"^\|\s*`([a-z0-9-]+\.[a-z0-9-]+\.[A-Za-z0-9._-]+)`", re.MULTILINE)


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAILS.append(f"{name} {detail}")


def check_eq(name: str, got, want) -> None:
    check(name, got == want, f"{got!r}（期望 {want!r}）")


def rejects(name: str, adapter: ModelAdapter) -> None:
    """导入期校验必须拦下这条错误记录。"""
    try:
        check_adapter("test.model.x", adapter)
    except ValueError:
        check(f"拦下{name}", True)
    else:
        check(f"拦下{name}", False, "居然通过了")


print("== 1. 每条现役记录都写清了设备名、原因、文档 ==")
rows = describe_all()
check("清单非空（至少一条真机适配）", bool(rows), f"{len(rows)} 条")
for row in rows:
    model = str(row["model"])
    check(f"{model} 有设备名", bool(str(row["device"]).strip()), repr(row["device"]))
    check(f"{model} 有 note", bool(str(row["note"]).strip()))
    doc = str(row["doc"])
    if doc:
        path = ROOT / doc
        check(f"{model} 的 doc 文件存在", path.is_file(), doc)
    else:
        check(f"{model} 的 doc 不为空", False, "空；没有调研文档就指到 architecture.md")

print("\n== 2. describe_all 与适配表是同一份数据 ==")
check_eq("型号集合一致（按型号排序）",
         [r["model"] for r in rows], sorted(MODEL_ADAPTERS))
check_eq("字段清单与 ModelAdapter.fields 一致",
         {r["model"]: r["fields"] for r in rows},
         {m: list(a.fields) for m, a in sorted(MODEL_ADAPTERS.items())})
check_eq("每条记录至少用到一个字段", [r["model"] for r in rows if not r["fields"]], [])

print("\n== 3. 字段标签完备（新加字段必须补人话）==")
used = {f for row in rows for f in row["fields"]}
check_eq("FIELD_LABELS 覆盖所有实际用到的字段",
         sorted(used - set(FIELD_LABELS)), [])
check_eq("FIELD_LABELS 里没有代码已不存在的字段",
         [f for f in FIELD_LABELS if not hasattr(ModelAdapter(), f)], [])

print("\n== 4. 台账第一节与代码互相覆盖（防漏记 / 防记了没写）==")
text = LEDGER.read_text(encoding="utf-8")
part1 = text.split("## 二、")[0]
listed = set(MODEL_RE.findall(part1))
check_eq("代码里有、台账里也列了",
         sorted(set(MODEL_ADAPTERS) - listed), [])
check_eq("台账里有、代码里也有",
         sorted(listed - set(MODEL_ADAPTERS)), [])

print("\n== 5. 台账第二节记的必须是「没适配」的型号 ==")
part2 = text.split("## 二、")[1].split("## 三、")[0] if "## 二、" in text else ""
not_adapted = set(MODEL_RE.findall(part2))
check_eq("第二节列出的型号确实不在适配表里（否则该挪到第一节）",
         sorted(not_adapted & set(MODEL_ADAPTERS)), [])
check("第二节有内容（评估过但没做的也要记，避免重复评估）", bool(not_adapted), str(not_adapted))

print("\n== 6. 导入期校验真的会拦（不然上面几条都是摆设）==")
rejects("空记录", ModelAdapter())
rejects("缺设备名", ModelAdapter(extra=("x",), note="n"))
rejects("缺 note", ModelAdapter(extra=("x",), device="d"))
rejects("doc 不是 docs/ 下的 md", ModelAdapter(extra=("x",), device="d", note="n", doc="foo.md"))
rejects("文案短名超长", ModelAdapter(
    device="d", note="n", text=TextLayer(words=(("a", "这个短名实在是太长了超过十二个字"),))))
# geek 是白名单判据，写成 "yes" / 1 在 Python 里也"像真的"，导入期必须拦掉
rejects("geek 写成字符串", ModelAdapter(
    device="d", note="n", doc="docs/reference/model-adapters.md", geek="yes"))

print("\n== 7. 清单工具能跑（它是给人看的总入口）==")
import subprocess  # noqa: E402

for args in (["--md"], ["--vendor", "xiaomi"]):
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(ROOT / "scripts/list_adapters.py"), *args],
        capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
        env={**__import__("os").environ, "PYTHONPATH": "."},
    )
    check(f"list_adapters.py {' '.join(args)} 退出 0 且有点输出",
          proc.returncode == 0 and bool(proc.stdout.strip()),
          f"rc={proc.returncode} {proc.stderr.strip()[:80]}")

print()
if FAILS:
    print(f"{len(FAILS)} 项失败：")
    for f in FAILS:
        print("  -", f)
    raise SystemExit(1)
print("全部通过")
