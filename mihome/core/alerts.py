# SPDX-License-Identifier: GPL-3.0-or-later
"""轮询异常的提醒决策：什么时候打扰用户、打扰什么、什么时候不再打扰。

纯逻辑，不 import Qt —— 这样离线脚本能直接把规则跑一遍，不用起窗口。

三条克制
--------
1. **分级**：授权失效和限流是"必须让人知道"的，第一次失败就报；纯网络故障
   给一拍缓冲（连续 3 次或持续 8 秒），拔一下网线、切个 Wi-Fi 不该弹窗。
2. **同类只弹一次**：按错误类别去重，恢复后重置。持续断网一晚上只有一条通知。
3. **同类冷却两分钟**：治疗"失败→恢复→失败"的抖动。只按"恢复后重置"的话，
   抖一次弹一次，比不提示还烦。冷却按类别分开算——刚弹过网络异常，不该把随后
   的授权失效一起压掉。

另外这里只管"要不要显示"，怎么显示（顶部横幅 / 系统通知）由界面层决定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: 横幅级别。界面层按它取色与图标
ERROR = "error"
WARNING = "warning"
SUCCESS = "success"

#: 一出现就必须报的类别（连续失败次数门槛）。没列出的走默认门槛
_STREAK_GATE = {"auth": 1, "server": 1, "protocol": 2}
#: 网络类：次数或时长任一满足即可
_NETWORK_STREAK = 3
_NETWORK_SECONDS = 8.0
#: 兜底门槛（未知类别）
_DEFAULT_STREAK = 3

#: 同类系统通知之间的最小间隔（秒）
TOAST_COOLDOWN = 120.0
#: 失败期间刷新横幅内计数的间隔（秒）。每 3 秒改一次文字会让人眼晕
TEXT_REFRESH = 15.0

#: 各类别的标题。正文见 :func:`_detail`
_TITLES = {
    "auth": "授权已失效",
    "server": "云端拒绝了读取",
    "network": "连接异常",
    "protocol": "返回数据异常",
}

_CODE_PREFIX = re.compile(r"^code:\s*(-?\d+),\s*message:\s*")


@dataclass(frozen=True)
class Alert:
    """要给用户看的一条提示。"""

    level: str
    title: str
    detail: str
    #: 去重键：同一段失败（同类错误）共用一个 key
    key: str


@dataclass(frozen=True)
class Update:
    """一次观察带来的变化。全为空表示"什么都不用做"。"""

    #: 需要（重新）绘制的横幅；``None`` = 内容没变，别重绘
    bar: Alert | None = None
    #: 收起横幅（用户关掉的那条已经不该显示了）
    hide: bool = False
    #: 需要弹一次系统通知
    toast: Alert | None = None


def _short(text: str, limit: int = 64) -> str:
    """把原始异常文本压成一行。``code: X, message: Y`` 这种前缀只留 Y。"""
    text = _CODE_PREFIX.sub("", (text or "").strip())
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    return f"{seconds // 3600} 小时 {seconds % 3600 // 60} 分"


def _level(kind: str) -> str:
    # 授权失效不自己好，红色等于"必须点一下"；其余都在自动重试，用琥珀色
    return ERROR if kind == "auth" else WARNING


def _detail(kind: str, failure: dict[str, Any], streak: int, seconds: float) -> str:
    code = str(failure.get("code") or "")
    suffix = f" · code={code}" if code else ""
    if kind == "auth":
        return f"米家登录状态已过期，读取已停止，请重新登录{suffix}"
    if kind == "server":
        text = _short(str(failure.get("text") or "")) or "云端拒绝了请求"
        return f"{text}，正在自动重试{suffix}"
    if kind == "protocol":
        return f"云端返回的内容无法解析，正在重试{suffix}"
    text = _short(str(failure.get("text") or ""))
    head = f"已连续失败 {streak} 次（{_duration(seconds)}），正在自动重试"
    return f"{head} · {text}" if text else head


def _reached(kind: str, streak: int, seconds: float) -> bool:
    if kind == "network":
        return streak >= _NETWORK_STREAK or seconds >= _NETWORK_SECONDS
    return streak >= _STREAK_GATE.get(kind, _DEFAULT_STREAK)


class AlertPolicy:
    """把 ``PollHealth.snapshot()`` 翻译成"该不该提醒"。

    只在主线程用（跟着 ``Store`` 的轮询回调走），不需要加锁。
    """

    def __init__(self, *, toast_cooldown: float = TOAST_COOLDOWN) -> None:
        self._cooldown = toast_cooldown
        #: 当前横幅上的内容（失败条或恢复条）
        self._shown: Alert | None = None
        #: 用户关掉的那条对应的问题；这段失败结束前不再冒出来
        self._dismissed: str | None = None
        #: 本次运行已经弹过通知的类别。恢复时清空
        self._notified: set[str] = set()
        #: 每个类别最近一次弹通知的时间。抖动时靠它压住同类，不连坐别的类别
        self._last_toast: dict[str, float] = {}
        self._last_text = -1e9
        #: 最后一次失败时的（连续次数, 持续秒数），恢复文案要用
        self._last_fail: tuple[int, float] | None = None

    # -- 对外 ------------------------------------------------------------
    @property
    def showing(self) -> Alert | None:
        """给验证脚本查内部状态用。"""
        return self._shown

    def dismiss(self) -> None:
        """用户点了 ×。同一类问题在这段失败结束前不再出现。"""
        if self._shown is not None and self._shown.level != SUCCESS:
            self._dismissed = self._shown.key
        elif self._shown is not None:
            # 关的是"已恢复"绿条：这段故障已经翻篇，下次再坏要重新提示
            self._dismissed = None
        self._shown = None

    def observe(self, snapshot: dict[str, Any], now: float) -> Update:
        if not snapshot:
            return Update()
        return self._healthy(now) if snapshot.get("healthy") else self._failing(snapshot, now)

    # -- 内部 ------------------------------------------------------------
    def _failing(self, snapshot: dict[str, Any], now: float) -> Update:
        failure = snapshot.get("last_failure") or {}
        kind = str(failure.get("kind") or "")
        key = kind or "unknown"
        streak = int(snapshot.get("streak") or 0)
        seconds = float((snapshot.get("episode") or {}).get("seconds") or 0)
        self._last_fail = (streak, seconds)

        # 上一段已经恢复、绿条还挂着，现在又坏了：这是新的一段，重新开始计数
        if self._shown is not None and self._shown.level == SUCCESS:
            self._shown = None
            self._dismissed = None

        if not _reached(kind, streak, seconds):
            return Update()
        if self._dismissed == key:
            # 用户已经关掉这类提示。继续失败也不打扰，恢复正常后才解除
            return Update()

        alert = Alert(_level(kind), _TITLES.get(kind, "读取异常"), _detail(kind, failure, streak, seconds), key)
        bar: Alert | None = None
        if self._shown is None or self._shown.key != key:
            self._shown = alert
            self._last_text = now
            bar = alert
        elif now - self._last_text >= TEXT_REFRESH and alert.detail != self._shown.detail:
            # 同一段失败里只更新计数，且限频，避免每 3 秒重排一次文字
            self._shown = alert
            self._last_text = now
            bar = alert

        toast: Alert | None = None
        last = self._last_toast.get(key, -1e9)
        if key not in self._notified and now - last >= self._cooldown:
            self._notified.add(key)
            self._last_toast[key] = now
            toast = alert
        return Update(bar=bar, toast=toast)

    def _healthy(self, now: float) -> Update:
        self._notified.clear()  # 恢复正常 → 同类异常下次可以再提醒一次
        previous = self._shown
        streak, seconds = self._last_fail or (0, 0.0)
        self._last_fail = None
        if previous is None:
            # 没显示过（或用户已经关掉）：这段失败到此为止，解除屏蔽
            self._dismissed = None
            return Update()
        if previous.level == SUCCESS:
            return Update()
        alert = Alert(
            SUCCESS,
            "已恢复",
            f"连接已恢复正常，本次连续失败 {streak} 次，持续 {_duration(seconds)}",
            previous.key,
        )
        self._shown = alert
        return Update(bar=alert)
