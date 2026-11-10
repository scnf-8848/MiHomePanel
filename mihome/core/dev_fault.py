# SPDX-License-Identifier: GPL-3.0-or-later
"""**开发用**故障注入：只为把「失败 → 分级 → 横幅 → 通知」这条链路亲眼看一遍。

默认完全关闭。只有进程启动前设置了环境变量 ``MIHOME_DEV_FAULT`` 才生效：

    network        一直失败（连接超时）
    network:3      失败 3 轮轮询后自愈（横幅会原地转绿）
    auth           一直失败（真实的授权失效码 -10030）
    server         一直失败（真实的限流码 -704040992）
    protocol       一直失败（响应内容无法解析）
    off            显式关闭

注入点在请求层：异常从 ``Client._read_batch`` 真正发起请求的那一行抛出，之后走的是
和真实故障完全相同的 ``classify_error`` → ``PollHealth`` → ``AlertPolicy`` 链路。
这里不碰横幅、不碰通知，也没有"直接显示"的捷径。

只作用于属性轮询（``Client.read(source="poll")``）；写后补读、详情页按需读不受影响。
没设环境变量时 ``current()`` 返回 ``None``，热路径上只多一次模块变量判断。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Callable

#: 开关名。带 DEV 前缀是故意的：一眼看出这是测试用的，别带进生产环境
ENV = "MIHOME_DEV_FAULT"

#: 注入类型 → （异常工厂, 给启动提示用的说明）
_FACTORIES: dict[str, tuple[Callable[[], BaseException], str]] = {
    "network": (
        lambda: TimeoutError(
            "HTTPSConnectionPool(host='api.io.mi.com', port=443): Max retries exceeded "
            "with url: /app/v2/miotspec/prop/get (Caused by ReadTimeoutError("
            "'Read timed out. (read timeout=5)'))（注入故障）"
        ),
        "连接超时",
    ),
    "auth": (
        lambda: _login_error(-10030, "无效的token（HTTP）（注入故障）"),
        "授权失效 code=-10030",
    ),
    "server": (
        lambda: _api_error(-704040992, "请求过于频繁（注入故障）"),
        "限流 code=-704040992",
    ),
    "protocol": (
        lambda: json.JSONDecodeError(
            "Expecting value（注入故障）",
            "<html><body>502 Bad Gateway</body></html>",
            0,
        ),
        "响应内容无法解析",
    ),
}

#: 显式关闭的写法
_OFF = frozenset({"", "0", "off", "none", "false", "no"})


def _login_error(code: int, message: str) -> BaseException:
    # 延迟导入：只有真的注入了才需要上游的错误类型
    from mijiaAPI.errors import LoginError

    return LoginError(code, message)


def _api_error(code: int, message: str) -> BaseException:
    from mijiaAPI.errors import APIError

    return APIError(code, message)


class FaultInjector:
    """按「轮询轮次」发异常。``remaining`` 为 ``None`` 表示一直失败。"""

    def __init__(self, kind: str, remaining: int | None = None) -> None:
        self.kind = kind
        self._remaining = remaining
        self._factory, self._label = _FACTORIES[kind]

    @property
    def remaining(self) -> int | None:
        return self._remaining

    @property
    def summary(self) -> str:
        count = "一直失败" if self._remaining is None else f"失败 {self._remaining} 轮后自愈"
        return f"{self.kind}（{self._label}），{count}"

    def next_poll_error(self) -> Callable[[], BaseException] | None:
        """一轮轮询开始时调一次。返回异常工厂，或 ``None`` 表示这轮放行。"""
        if self._remaining == 0:
            return None
        if self._remaining is not None:
            self._remaining -= 1
        return self._factory


def parse(spec: str) -> FaultInjector | None:
    """解析 ``MIHOME_DEV_FAULT`` 的取值。无法识别时提示并关闭。"""
    text = (spec or "").strip().lower()
    if text in _OFF:
        return None
    kind, _, count = text.partition(":")
    if kind not in _FACTORIES:
        print(
            f"[dev_fault] 无法识别的注入类型 {kind!r}，可选："
            f"{'、'.join(_FACTORIES)}；本次不注入",
            file=sys.stderr,
        )
        return None
    remaining: int | None = None
    if count:
        try:
            remaining = int(count)
        except ValueError:
            print(f"[dev_fault] 轮数 {count!r} 不是整数，本次不注入", file=sys.stderr)
            return None
        if remaining <= 0:
            return None
    return FaultInjector(kind, remaining)


_INSTANCE: FaultInjector | None = None
_LOADED = False


def current() -> FaultInjector | None:
    """当前进程的注入器；没开就是 ``None``。结果缓存，热路径不做环境变量解析。"""
    global _INSTANCE, _LOADED
    if not _LOADED:
        _INSTANCE = parse(os.environ.get(ENV, ""))
        _LOADED = True
        if _INSTANCE is not None:
            print(f"[dev_fault] 已开启故障注入：{_INSTANCE.summary}", file=sys.stderr)
    return _INSTANCE


def reset() -> None:
    """重新读取环境变量。只有验证脚本需要（正常运行不会中途改这个开关）。"""
    global _INSTANCE, _LOADED
    _INSTANCE = None
    _LOADED = False


__all__ = ["ENV", "FaultInjector", "current", "parse", "reset"]
