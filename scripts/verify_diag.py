# SPDX-License-Identifier: GPL-3.0-or-later
"""验证启动现场诊断：进程身份取得出来、报告能追加、写不进去也不抛。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_diag.py``

盯的是 2026-11-21 那次事故里最缺的东西：**能证明"这个进程是谁"的一行字**。
当时 exe 被 Windows 以 Low 完整性启动，写不了数据目录、挂不上托盘，而日志全落
在写不进去的那个目录里，等于一条线索都没有。这里钉住三件事：

1. 完整性级别能读出来，且是已知取值；
2. 报告落在 ``%TEMP%``（不是数据目录），追加而不是覆盖；
3. 连 ``%TEMP%`` 都写不进去时返回 None，**不抛异常**（诊断自己不能变成故障）。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mihome.core import diag  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('：' + detail) if detail else ''}")
    if not ok:
        FAILS.append(name)


def main() -> int:
    level = diag.integrity_level()
    known = ("Untrusted", "Low", "Medium", "MediumPlus", "High", "System")
    check("完整性级别读得出来且是已知取值", level in known, level or "(空)")
    check(
        "low_integrity() 与 integrity_level() 一致",
        diag.low_integrity() is (level == "Low"),
        f"{diag.low_integrity()} vs {level}",
    )

    summary = diag.identity_summary()
    check("身份摘要不是空字符串", bool(summary.strip()), summary)
    check("摘要里有完整性级别", level in summary or "取不到" in summary, summary)

    # 报告必须落在 %TEMP%，而且追加：跨启动对比"改完设置重启又还原"要靠这个
    tmp = Path(tempfile.mkdtemp(prefix="mihome-diag-"))
    old_temp = os.environ.get("TEMP")
    os.environ["TEMP"] = str(tmp)
    try:
        first = diag.write_report({"note": "第一段"})
        second = diag.write_report({"note": "第二段"})
        text = first.read_text(encoding="utf-8") if first else ""
        check("报告路径在 %TEMP% 下", first is not None and first.parent == tmp, str(first))
        check("两次写的是同一个文件", first == second, str(second))
        check("是追加不是覆盖", "第一段" in text and "第二段" in text, f"{len(text)} 字节")
        check("报告里带 pid 与 frozen", "pid=" in text and "frozen=" in text)
        check("报告里带完整性级别", "完整性级别=" in text)

        # 写不进去时返回 None，绝不抛
        blocked = tmp / "blocked"
        blocked.mkdir()
        os.environ["TEMP"] = str(blocked / "missing" / "deep")
        try:
            result = diag.write_report({"note": "写不进去"})
            raised = ""
        except Exception as exc:  # noqa: BLE001 - 这里就是要抓它
            result, raised = "threw", f"{type(exc).__name__}: {exc}"
        check("写不进去时返回 None 且不抛", result is None and not raised, raised or "None")
    finally:
        if old_temp is None:
            os.environ.pop("TEMP", None)
        else:
            os.environ["TEMP"] = old_temp

    if FAILS:
        print(f"\n{len(FAILS)} 条失败：" + "；".join(FAILS))
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
