# SPDX-License-Identifier: GPL-3.0-or-later
"""轮询健康记录 —— 让"3 秒一轮的读到底还通不通"有据可查。

为什么单独一个模块
------------------
轮询链路上的异常在 ``core/api.py`` 里会被吞成**空结果**（见 ``_read_batch``），
到 ``Store`` 那层已经分不清"这轮没属性可读"和"整个请求挂了"。所以记账放在
**请求层**：只有那里拿得到原始错误码、异常类型和原始文本。``Store`` 只额外补一句
"这次触发了几秒退避"，其余一概不管。

分类
----
四类，对应排障时四种完全不同的处理方式：

* ``network`` —— 网络类。连接失败、超时、DNS、TLS。等一会儿多半自己好。
* ``auth`` —— 授权失效。token 过期 / 无效 / 未授权，必须重新扫码。
* ``server`` —— 限流与其它服务端返回码（例如 992 请求过于频繁）。要降速或等。
* ``protocol`` —— 协议 / 解析。JSON 解析不了、响应结构不是预期的形状。

``code`` 与 ``text`` **原样保存**，不做二次翻译：错误码是排障的第一手材料，
翻成中文反而对不上米家文档。

不刷屏
------
同一个错误持续时**只写一行**，之后每 ``SUMMARY_SECONDS`` 写一条聚合行
（"这段时间又失败 N 次"）；错误类型或错误码变了才立刻再写一行；恢复时写一行
"恢复"，带连续失败次数与历时。所以断网一整夜 = 1 行失败 + 每 5 分钟 1 行 +
1 行恢复，而不是每 3 秒一行。写文件只在状态变化时发生，轮询热路径上不做 IO。

重启可见
--------
``poll_health.last.json`` 存本次运行的健康快照（只在状态变化时写）。下次启动
第一件事就是把它读出来写进日志开头，所以"上一次运行结束时到底好不好"一眼可见。
日志单文件 1 MB，超了轮转成 ``.log.1``，只留一代（与 ``core/timing.py`` 同一口径）。
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import paths

#: 四类错误。值直接进日志与快照，改字面量等于改格式，验证脚本按它断言
NETWORK = "network"
AUTH = "auth"
SERVER = "server"
PROTOCOL = "protocol"

LABELS = {
    NETWORK: "网络",
    AUTH: "授权",
    SERVER: "服务端",
    PROTOCOL: "协议",
}

#: 连续失败期间写聚合行的最小间隔（秒）
SUMMARY_SECONDS = 300.0
#: 一切正常时刷新状态快照的最小间隔（秒）。正常时每轮都写没必要，
#: 但完全不写的话：进程被强杀 / 崩溃后，下次启动看到的是更早那次的旧结论。
STATE_SAVE_SECONDS = 60.0
#: 单文件上限，超过就轮转成 ``.log.1``（只留一代）
MAX_BYTES = 1024 * 1024

#: 授权类错误码。来源：mijiaAPI/errors.py 的 ERROR_CODE 表
#: -10020 未授权OAuth2 / -10030 无效的token（HTTP） / -704010000 未授权（设备可能被删除）
_AUTH_CODES = frozenset({-10020, -10030, -704010000})
#: 米家错误码格式 ``-70 + HTTP 状态码 + 出错位置 + 错误码``，后三位才是语义。
#: 992 = 请求过于频繁（唯一有明确定义的限流码）
RATE_LIMIT_TAIL = 992

_CODE_RE = re.compile(r"code:\s*(-?\d+)")


@dataclass(frozen=True)
class Failure:
    """一次失败的原始现场。``code``/``text`` 一定原样保留。"""

    kind: str
    code: str = ""
    text: str = ""

    @property
    def label(self) -> str:
        return LABELS.get(self.kind, self.kind)


def _code_of(exc: BaseException) -> str:
    """从异常里捞原始错误码。

    ``mijiaAPI.errors.APIError`` / ``LoginError`` 只把码塞进了 message
    （``f"code: {code}, message: {message}"``），没有留属性，只能从文本里取。
    """
    found = _CODE_RE.search(str(exc))
    return found.group(1) if found else ""


def classify_error(exc: BaseException) -> Failure:
    """把异常归成四类之一，原始文本一字不改地带上。"""
    text = str(exc) or type(exc).__name__
    # 延迟导入：health 会被 api 导入，这里再反向导入 mijiaAPI 只发生在调用时，
    # 且 mijiaAPI 本来就已经在 api.py 顶层导入了，不会多付一次导入开销
    from mijiaAPI.errors import APIError, LoginError

    if isinstance(exc, LoginError):
        # token 刷新失败 / 登录态失效：只有重新扫码能解决
        return Failure(AUTH, _code_of(exc), text)

    if isinstance(exc, APIError):
        code = _code_of(exc)
        try:
            value = int(code)
        except (TypeError, ValueError):
            return Failure(SERVER, code, text)
        if value in _AUTH_CODES:
            return Failure(AUTH, code, text)
        # 其余（含 992 请求过于频繁）都算服务端明确拒绝，原码保留，排障时再细分
        return Failure(SERVER, code, text)

    if isinstance(exc, (json.JSONDecodeError, ValueError, TypeError)):
        # json.loads / decrypt 失败：响应根本不是我们以为的那个格式
        return Failure(PROTOCOL, "", text)

    # 兜底归网络：这条链路上非解析类异常基本都是传输层抖上来的
    # （requests 的 Timeout / ConnectionError 都从 IOError 派生）
    return Failure(NETWORK, "", text)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""


def _ago(ts: float) -> str:
    if not ts:
        return "从未"
    delta = max(0, int(time.time() - ts))
    if delta < 60:
        return f"{delta} 秒前"
    if delta < 3600:
        return f"{delta // 60} 分钟前"
    if delta < 86400:
        return f"{delta // 3600} 小时前"
    return f"{delta // 86400} 天前"


def _stamp(value: Any) -> str:
    """状态文件里时间既可能是 epoch（float），也可能是快照里的可读串。"""
    if isinstance(value, str):
        return value
    try:
        return _iso(float(value or 0))
    except (TypeError, ValueError):
        return ""


def _count(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    return f"{seconds // 3600} 小时 {seconds % 3600 // 60} 分"


class PollHealth:
    """轮询健康账本。线程安全：worker 线程记失败，主线程记退避与快照。"""

    def __init__(
        self,
        *,
        log_path: Path | None = None,
        state_path: Path | None = None,
    ) -> None:
        self.log_path = log_path or (paths.log_dir() / "poll_health.log")
        self.state_path = state_path or (paths.log_dir() / "poll_health.last.json")
        self._lock = threading.RLock()  # 重入：_save/close 会在持锁时再取一次快照
        self._run_started = time.time()

        #: 本次运行的状态
        self.streak = 0
        self.last_success = 0.0
        self.last_failure: Failure | None = None
        self.last_failure_at = 0.0
        self.episode_start = 0.0
        self.episode_count = 0
        self.backoff_seconds = 0.0
        self.backoff_start = 0.0

        self._last_log_at = 0.0
        self._last_backoff = 0.0
        self._last_save = 0.0
        self._prev = self._load_prev()
        self._open_episode()

    # -- 落盘 ------------------------------------------------------------
    def _load_prev(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save(self) -> None:
        """写本次运行的健康快照。状态变化时立刻写，正常态按 ``STATE_SAVE_SECONDS`` 节流。"""
        self._last_save = time.time()
        self._write_state(self.snapshot())

    def _write_state(self, payload: dict[str, Any]) -> None:
        payload.pop("previous", None)
        # "多久以前"这类相对时间只在内存里有意义，落盘会立刻过期
        payload.pop("last_success_ago", None)
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            tmp.replace(self.state_path)
        except OSError:
            pass

    def _line(self, text: str) -> None:
        """追加一行日志。调用方必须已持有 ``self._lock``。"""
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            path = self.log_path
            if path.exists() and path.stat().st_size >= MAX_BYTES:
                older = path.with_name(path.name + ".1")
                if older.exists():
                    older.unlink()
                path.replace(older)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"{stamp}  {text}\n")
        except OSError:
            # 日志写不进去绝不能反过来影响轮询
            pass

    def _open_episode(self) -> None:
        """开头写一行本次启动 + 上次运行结束时的状态。"""
        with self._lock:
            head = datetime.fromtimestamp(self._run_started).strftime("%Y-%m-%d %H:%M:%S")
            self._line(f"==== 启动 {head} ====")
            if self._prev:
                self._line(f"上次运行：{self._describe(self._prev)}")
            else:
                self._line("上次运行：没有记录（首次运行或日志被清理）")

    @staticmethod
    def _describe(state: dict[str, Any]) -> str:
        ended = _stamp(state.get("ended_at"))
        started = _stamp(state.get("run_started"))
        streak = _count(state.get("streak"))
        head = f"{started or '未知'} 起，{ended or '未正常退出'}"
        if streak:
            failure = state.get("last_failure") or {}
            code = failure.get("code") or "—"
            kind = LABELS.get(str(failure.get("kind") or ""), str(failure.get("kind") or ""))
            return (
                f"{head} · 结束时轮询异常：连续失败 {streak} 次，"
                f"最近错误 [{kind}] code={code} {failure.get('text') or ''}"
            )
        last = _stamp(state.get("last_success"))
        return f"{head} · 结束时轮询正常，最后一次成功 {last or '无'}"

    # -- 记账 ------------------------------------------------------------
    def success(self, *, items: int = 0) -> None:
        """一轮轮询成功。只在从"失败"回到"正常"时写日志与快照。"""
        with self._lock:
            now = time.time()
            self.last_success = now
            was = self.streak
            total = self.episode_count
            self.streak = 0
            self.episode_start = 0.0
            self.episode_count = 0
            self.backoff_seconds = 0.0
            self.backoff_start = 0.0
            self._last_backoff = 0.0
            if not was:
                # 一直正常：不写日志，只按节流刷新一次快照，保住"最近成功时间"
                if now - self._last_save >= STATE_SAVE_SECONDS:
                    self._save()
                return
            self._line(
                f"恢复正常  连续失败 {was} 次后恢复，最近一次失败在 "
                f"{_ago(self.last_failure_at)}（共 {total} 次失败），"
                f"本轮读到 {items} 条"
            )
            self._last_log_at = now
            self._save()

    def failure(self, fail: Failure) -> None:
        """一轮轮询失败。同状态持续时按 ``SUMMARY_SECONDS`` 聚合，不逐条刷。"""
        with self._lock:
            now = time.time()
            first = self.streak == 0
            self.streak += 1
            if first:
                self.episode_start = now
                self.episode_count = 0
            self.episode_count += 1
            changed = (
                self.last_failure is None
                or self.last_failure.kind != fail.kind
                or self.last_failure.code != fail.code
            )
            self.last_failure = fail
            self.last_failure_at = now

            if first or changed:
                self._line(
                    f"轮询失败  连续 {self.streak} 次  [{fail.label}] "
                    f"code={fail.code or '—'}  原文：{fail.text or '（无）'}"
                )
                self._last_log_at = now
                self._save()
                return
            if now - self._last_log_at >= SUMMARY_SECONDS:
                span = _duration(now - self.episode_start)
                self._line(
                    f"仍在失败  已持续 {span}，累计失败 {self.episode_count} 次"
                    f"（连续 {self.streak} 次）  "
                    f"最近 [{fail.label}] code={fail.code or '—'}  {fail.text or ''}"
                )
                self._last_log_at = now
                self._save()

    def backoff(self, *, streak: int, seconds: float) -> None:
        """记录 ``Store`` 刚触发的退避。只在这一档真变化时写一行。"""
        with self._lock:
            now = time.time()
            seconds = float(seconds or 0.0)
            if seconds <= 0:
                self.backoff_seconds = 0.0
                self.backoff_start = 0.0
                self._last_backoff = 0.0
                return
            if seconds == self._last_backoff and self.backoff_start:
                return
            first = self.backoff_start == 0.0
            if first:
                self.backoff_start = now
            self.backoff_seconds = seconds
            self._last_backoff = seconds
            since = "本次失败开始" if first else f"{_iso(self.backoff_start)} 起"
            self._line(
                f"触发退避  连续失败 {streak} 次，下一轮延后 {seconds:.0f} 秒"
                f"（{since}，上限 30 秒）"
            )
            self._save()

    # -- 查询 ------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """给"以后可能有的诊断面板"用的结构化快照。主线程可随时调用。"""
        with self._lock:
            failure = None
            if self.last_failure is not None:
                failure = {
                    "kind": self.last_failure.kind,
                    "label": self.last_failure.label,
                    "code": self.last_failure.code,
                    "text": self.last_failure.text,
                    "at": _iso(self.last_failure_at),
                }
            return {
                "run_started": _iso(self._run_started),
                "healthy": self.streak == 0,
                "streak": self.streak,
                "last_success": _iso(self.last_success),
                "last_success_ago": _ago(self.last_success),
                "last_failure": failure,
                "episode": (
                    {
                        "start": _iso(self.episode_start),
                        "seconds": int(time.time() - self.episode_start),
                        "count": self.episode_count,
                    }
                    if self.episode_start
                    else None
                ),
                "backoff": (
                    {
                        "seconds": self.backoff_seconds,
                        "since": _iso(self.backoff_start),
                    }
                    if self.backoff_seconds
                    else None
                ),
                "previous": self._prev,
            }

    # -- 收尾 ------------------------------------------------------------
    def close(self) -> None:
        """退出时落盘：把本次运行的健康状况留在 ``poll_health.last.json``。"""
        with self._lock:
            now = time.time()
            if self.streak:
                failure = self.last_failure
                self._line(
                    f"==== 退出 {_iso(now)}  本次运行结束时轮询异常："
                    f"连续失败 {self.streak} 次，最近 [{failure.label if failure else '—'}] "
                    f"code={failure.code if failure and failure.code else '—'} ===="
                )
            else:
                self._line(
                    f"==== 退出 {_iso(now)}  本次运行轮询正常，最后一次成功 "
                    f"{_iso(self.last_success) or '无'} ===="
                )
            payload = self.snapshot()
            payload["ended_at"] = _iso(now)
            self._write_state(payload)


__all__ = [
    "AUTH",
    "Failure",
    "LABELS",
    "MAX_BYTES",
    "NETWORK",
    "PROTOCOL",
    "PollHealth",
    "RATE_LIMIT_TAIL",
    "SERVER",
    "SUMMARY_SECONDS",
    "classify_error",
]
