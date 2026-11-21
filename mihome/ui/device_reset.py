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


def refresh_result_text(ok: int, failed: int = 0, error: str = "") -> str:
    """强制刷新的结果提示：只说**读到了多少项**。

    目标是不让人误以为"刷新失败、得手动重试"：

    - 只要有属性读到，就只说刷新了多少项，**不出现**没读到的条数，也不带
      "失败 / 未读到 / 无响应"这类词；
    - 一项都没读到，明确告诉用户稍后会自动重试，避免反复点。

    未读到的条数与原因只写日志（``Store.refresh_capabilities``），不上界面。
    """
    if ok > 0:
        return f"已刷新 {ok} 项属性"
    return "设备暂时无响应，稍后会自动重试"


def reset_result_text(name: str) -> str:
    """重置完成的轻提示：只说结果，清哪些类目留在确认窗里。"""
    return f"已重置「{name}」的配置"


__all__ = [
    "device_reset_text",
    "all_reset_text",
    "refresh_result_text",
    "reset_result_text",
]
