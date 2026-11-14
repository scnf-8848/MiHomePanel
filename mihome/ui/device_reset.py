# SPDX-License-Identifier: GPL-3.0-or-later
"""设备配置重置 / 强制刷新的说明文案。

文案独立成纯函数，是为了让验证脚本能直接断言"确认窗里到底写了什么"，
不用起 Qt 窗口去读控件文本。调用方（主窗口、设置页）只负责弹窗与执行。
"""

from __future__ import annotations


def _rows(counts: dict[str, int]) -> list[str]:
    return [
        f"· 能力缓存：{counts.get('caps', 0)} 条「已确认不支持」记录",
        f"· 卡片样式：{counts.get('style', 0)} 条",
        f"· 隐藏状态：{counts.get('hidden', 0)} 条",
        f"· 房间内排序：{counts.get('order', 0)} 条",
    ]


NO_IMPACT = "不影响米家 App 里的设备、场景与自动化，也不影响登录状态。"


def device_reset_text(name: str, counts: dict[str, int]) -> str:
    """单设备重置的确认文案：逐项列出会清什么，并说明隐藏状态恢复的后果。"""
    lines = [f"会清除「{name}」的这些本地配置：", *_rows(counts), ""]
    if counts.get("hidden"):
        lines.append("隐藏状态清除后，该设备会重新出现在卡片墙原位置。")
    lines.append(NO_IMPACT)
    return "\n".join(lines)


def all_reset_text(counts: dict[str, int]) -> str:
    """全部重置的确认文案：与单设备同口径，多一行受影响设备数。"""
    lines = [
        "会清除所有设备的本地配置（与单设备重置同口径）：",
        f"· 有本地配置的设备：{counts.get('devices', 0)} 台",
        *_rows(counts),
        "",
        "被隐藏的设备会重新出现在卡片墙原位置。",
        NO_IMPACT,
    ]
    return "\n".join(lines)


def refresh_result_text(name: str, removed: int, read: int, error: str = "") -> str:
    """强制刷新的结果提示。必须说清"清的是不支持记录、不是刷新数据"。"""
    if error:
        return (
            f"「{name}」已清除 {removed} 条「已确认不支持」记录，"
            f"但立刻重查失败：{error}"
        )
    return (
        f"「{name}」已清除 {removed} 条「已确认不支持」记录，"
        f"并立刻重查了 {read} 项属性"
    )


__all__ = ["device_reset_text", "all_reset_text", "refresh_result_text"]
