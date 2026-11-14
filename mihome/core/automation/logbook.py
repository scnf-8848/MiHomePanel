# SPDX-License-Identifier: GPL-3.0-or-later
"""触发日志的**纯逻辑层**：中文结果名、筛选 / 查找、表格列、结果语义分档。

日志本体是 ``AutomationEngine.log``（``list[LogEntry]``，只活内存、上限 ``max_log``）。
独立日志窗口 ``ui/automation_log.py`` 只负责把这里筛出来的条目画成表格；筛选规则放在
core 里是为了能离线验证，也避免"换个窗口筛选条件就变了一套"。

默认**显示所有规则的触发历史**（不按当前打开的规则过滤）——排查时最常问的是
"刚才那一串到底跑到哪一步了"，按规则先切一遍反而找不到。想只看某条规则，用窗口里的
规则下拉。
"""

from __future__ import annotations

import time
from typing import Iterable

from .engine import (
    CONDITION,
    COOLDOWN,
    DONE,
    ELSE,
    FAILED,
    FIRED,
    REENTRY,
    SENT,
    LogEntry,
)

#: outcome → 中文短名。下拉和表格共用一份，别在 UI 里另写一份。
OUTCOME_LABELS: dict[str, str] = {
    FIRED: "触发",
    ELSE: "否则分支",
    SENT: "发送 / 延迟",
    DONE: "执行完毕",
    COOLDOWN: "冷静期拦截",
    REENTRY: "重入丢弃",
    CONDITION: "条件不成立",
    FAILED: "失败",
}

#: 下拉顺序：先正常流程，再各类拦截 / 异常
OUTCOME_ORDER: tuple[str, ...] = (
    FIRED, ELSE, SENT, DONE, COOLDOWN, REENTRY, CONDITION, FAILED,
)

#: 时间范围下拉：(显示的文字, 秒数)。0 = 全部时间。
TIME_RANGES: tuple[tuple[str, int], ...] = (
    ("全部时间", 0),
    ("最近 5 分钟", 5 * 60),
    ("最近 1 小时", 60 * 60),
    ("最近 24 小时", 24 * 60 * 60),
)

#: 需要提醒用户的 outcome（通用的"非正常流程"判定，别拿它直接染表格色）
WARN_OUTCOMES: frozenset[str] = frozenset({COOLDOWN, REENTRY, CONDITION, FAILED})

#: 表格「结果」列的三档语义。以前只分红 / 绿，「冷静期拦截」和「发送失败」都是红，
#: 排查"按了没反应"时分不清是被拦了还是真出错。
#: * ``ok``      触发 / 发送 / 执行完毕 → 绿
#: * ``neutral`` 冷静期、重入丢弃、条件不成立 → 琥珀（**程序按设计没执行**，不是故障）
#: * ``bad``     云端报错 / 后台异常 → 红
#: **④「否则分支」不在 neutral 里**：它真的执行了一串动作，只是走另一条路，按 ``ok`` 染绿。
NEUTRAL_OUTCOMES: frozenset[str] = frozenset({COOLDOWN, REENTRY, CONDITION})
FAIL_OUTCOMES: frozenset[str] = frozenset({FAILED})


def outcome_tone(outcome: str) -> str:
    """给结果列染色用：``ok`` / ``neutral`` / ``bad``。染色的具体色值归 UI。"""
    if outcome in FAIL_OUTCOMES:
        return "bad"
    if outcome in NEUTRAL_OUTCOMES:
        return "neutral"
    return "ok"


def outcome_label(outcome: str) -> str:
    return OUTCOME_LABELS.get(outcome, outcome or "未知")


def date_text(entry: LogEntry) -> str:
    """表格里的完整时刻（``time_text`` 只有时分秒，跨天排查会撞车）。"""
    return time.strftime("%m-%d %H:%M:%S", time.localtime(entry.at))


def full_text(entry: LogEntry) -> str:
    """一行全文，给 tooltip / 关键字搜索用；别在 UI 里再拼一遍，容易漏字段。

    **必须包含 ``keys``**：排查时最先想起的往往是「刚才按的是 Ctrl+K」，
    少了这一列就搜不到，而表格里明明看得见快捷键。
    """
    return (f"{date_text(entry)}  [{entry.rule_name}]  {entry.keys}  "
            f"{outcome_label(entry.outcome)}  {entry.detail}")


def log_columns(entry: LogEntry) -> tuple[str, str, str, str, str]:
    """表格五列：时刻 / 规则 / 快捷键 / 结果 / 说明。"""
    return (
        date_text(entry),
        entry.rule_name or "(未命名)",
        entry.keys or "—",
        outcome_label(entry.outcome),
        entry.detail,
    )


def matches(
    entry: LogEntry,
    *,
    keyword: str = "",
    rule_id: str = "",
    outcome: str = "",
    since: float = 0.0,
) -> bool:
    """一个条目是否符合当前筛选条件。空条件 = 不过滤。

    * ``keyword``  不区分大小写，匹配规则名 / 键位 / 结果名 / 说明 / 时刻；
    * ``rule_id``  精确匹配（``""`` = 全部规则）；
    * ``outcome``  精确匹配（``""`` = 全部结果）；
    * ``since``    epoch 秒下界（``0`` = 全部时间）。
    """
    if rule_id and entry.rule_id != rule_id:
        return False
    if outcome and entry.outcome != outcome:
        return False
    if since and entry.at < since:
        return False
    if keyword:
        needle = keyword.strip().casefold()
        if needle and needle not in full_text(entry).casefold():
            return False
    return True


def filter_entries(
    entries: Iterable[LogEntry],
    *,
    keyword: str = "",
    rule_id: str = "",
    outcome: str = "",
    since: float = 0.0,
    newest_first: bool = True,
) -> list[LogEntry]:
    """筛出条目。引擎里的 log 是时间正序；表格默认**倒序**（最新的在最上面）。"""
    out = [e for e in entries
           if matches(e, keyword=keyword, rule_id=rule_id,
                      outcome=outcome, since=since)]
    if newest_first:
        out.reverse()
    return out


__all__ = [
    "FAIL_OUTCOMES",
    "NEUTRAL_OUTCOMES",
    "OUTCOME_LABELS",
    "OUTCOME_ORDER",
    "TIME_RANGES",
    "WARN_OUTCOMES",
    "date_text",
    "filter_entries",
    "full_text",
    "log_columns",
    "matches",
    "outcome_label",
    "outcome_tone",
]
