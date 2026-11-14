# SPDX-License-Identifier: GPL-3.0-or-later
"""触发日志的**落盘层**：一行一条的 JSONL，重启后还能翻回上次跑过什么。

以前日志只活在 ``AutomationEngine.log`` 里（内存、退出即清空），"刚才那串到底成没成"
只能趁程序还开着看，关掉一次就全没了。这里把每一条追加进
``<data>/automation_log.jsonl``，启动时读回来交给引擎（``engine.restore_log``）。

**为什么是 JSONL 而不是一个 JSON 数组**：日志只增不改。追加一行只要一次
``open(..., "a")``；数组得整份读出来、改完、整份写回，每来一条日志重写一遍全量历史。
**为什么每次 append 都开关文件**：程序被强杀 / 断电最多丢最后一行；若攒在内存里批量
落盘，丢的就是一整段。崩溃留下的半行在 :meth:`LogStore.load` 里跳过——半行不该让
整份历史读不回来。

上限与内存一致（都是 :data:`MAX_ENTRIES` 条），但**文件攒到两倍才重写一次**：每来一条
就整份重写 500 行没有必要，攒到 1000 行再裁到 500，平均下来 500 条日志才写一次盘。
所以文件里实际留着 500~1000 条，界面只显示最近 500 条。

**批次号 ``run`` 是字符串，不是内存计数器**（用户 11-14 提的正是这一点）：以前它是
内存里从 1 递增的整数，落盘后重启又从 1 开始，昨天的第 3 次和今天的第 3 次会折进同一
行。现在由引擎按「规则 id + 触发毫秒」生成（见 ``engine.AutomationEngine._new_run``），
写在文件里本身就是稳定的，读回来照样能分组。

排序 / 中文结果名 / 折叠 / 染色一概不在这里，全在 :mod:`logbook`；本模块只管"怎么存"。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

from .. import paths
from .engine import LogEntry

#: 落在 ``<data>/`` 下的文件名
LOG_FILE = "automation_log.jsonl"

#: 界面显示 / 读回内存的条数上限，与 ``AutomationEngine.max_log`` 同一个数
MAX_ENTRIES = 500

#: 文件攒到 ``MAX_ENTRIES * TRIM_RATIO`` 行才裁一次
TRIM_RATIO = 2


def _line(entry: LogEntry) -> str:
    """一条日志 → 一行 JSON。字段名与 ``LogEntry`` 同名，方便对着文件排查。"""
    data = {
        "at": round(float(entry.at), 3),
        "rule_id": entry.rule_id,
        "rule_name": entry.rule_name,
        "keys": entry.keys,
        "outcome": entry.outcome,
        "detail": entry.detail,
    }
    # 不属于某一次触发的记录（冷静期拦截等）不写这个键，文件里少一处噪音
    if entry.run:
        data["run"] = entry.run
    return json.dumps(data, ensure_ascii=False)


def _entry(line: str) -> LogEntry | None:
    """一行 → 一条日志。认不出 / 缺关键字段就返回 ``None``（调用方跳过）。

    宽容原则同 ``rules.json``：一行坏了丢一行，别把整份历史报废。``at`` 是唯一
    必需字段——没有时刻的条目在表里既排不了序也筛不了时间范围。
    """
    line = line.strip()
    if not line:
        return None
    try:
        raw = json.loads(line)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    try:
        at = float(raw.get("at") or 0)
    except (TypeError, ValueError):
        return None
    if at <= 0:
        return None
    return LogEntry(
        at=at,
        rule_id=str(raw.get("rule_id") or ""),
        rule_name=str(raw.get("rule_name") or ""),
        keys=str(raw.get("keys") or ""),
        outcome=str(raw.get("outcome") or ""),
        detail=str(raw.get("detail") or ""),
        run=str(raw.get("run") or ""),
    )


class LogStore:
    """``automation_log.jsonl`` 的读写。**不 import Qt**，离线验证直接跑。"""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        max_entries: int = MAX_ENTRIES,
    ) -> None:
        self.path = Path(path) if path is not None else paths.data_dir() / LOG_FILE
        self.max_entries = max(1, int(max_entries))
        #: 文件里当前大概有几条（``None`` = 还没数过，第一次 append 前补数）。
        #: 不用每次 append 都读一遍文件，只在跨越上限时才重写。
        self._count: int | None = None

    # -- 读 --------------------------------------------------------------
    def load(self) -> list[LogEntry]:
        """读出最近 ``max_entries`` 条（时间正序，与 ``engine.log`` 同序）。"""
        lines = self._lines()
        self._count = len(lines)
        # **先解析再截尾**：反过来的话，末尾几行如果是崩溃留下的坏行，会把
        # 原本要留的好行挤出窗口——一次断电就让历史少一截。
        parsed = [entry for entry in (_entry(line) for line in lines) if entry is not None]
        return parsed[-self.max_entries:]

    def _lines(self) -> list[str]:
        """整份文件按行切开；读不出来就当空的（缺文件 / 没权限都走这条）。"""
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return [line for line in text.splitlines() if line.strip()]

    # -- 写 --------------------------------------------------------------
    def append(self, entry: LogEntry) -> bool:
        """追加一条。**发不出去也不抛异常**：日志写不进去不该影响规则执行。"""
        if self._count is None:
            self._count = len(self._lines())
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(_line(entry) + "\n")
        except OSError:
            return False
        self._count += 1
        if self._count >= self.max_entries * TRIM_RATIO:
            self.trim()
        return True

    def trim(self) -> bool:
        """把文件裁到最近 ``max_entries`` 条（先 ``.tmp`` 再 ``os.replace``）。"""
        kept = self.load()
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text("".join(_line(e) + "\n" for e in kept), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            return False
        return True

    def clear(self) -> bool:
        """清空文件（界面上的「清空日志」要和内存一起清，否则重启又翻回来）。"""
        self._count = 0
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

    # -- 其它 ------------------------------------------------------------
    def extend(self, entries: Iterable[LogEntry]) -> bool:
        """一次性写多条（迁移 / 验证用）。返回是否全部写成功。"""
        ok = True
        for entry in entries:
            ok = self.append(entry) and ok
        return ok


__all__ = ["LOG_FILE", "MAX_ENTRIES", "TRIM_RATIO", "LogStore"]
