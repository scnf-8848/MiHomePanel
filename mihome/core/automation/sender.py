# SPDX-License-Identifier: GPL-3.0-or-later
"""发一条中枢网关「虚拟事件」：按**规格里的服务名 / action 名**动态找坐标。

一条 iid 都不写死（``core/speaker.py`` 的同一条教训）：``xiaomi.gateway.hub1`` 的
spec **版本 3** 才有 ``virtual-service``，版本 1 没有；中枢还可能内置在路由器 / 屏幕里。
所以「哪台是中枢」的判据是**它有没有 ``emit-virtual-event`` 这个 action**，不是型号
白名单也不是名字匹配（``docs/reference/mijia-automation-events.md`` 4.1）。

``/miotspec/action`` 的入参键是 ``in`` 不是 ``value``（混用报 ``-704220025``，
看着像「设备不支持」）。本模块不 import Qt、不碰线程：``call()`` 是**同步**的，
由 ``SerialWorker`` 在后台线程跑；解析 target 要在主线程先做完，避免边遍历
``Store.devices`` 边被主线程改。

``code 0`` 只代表云端收下，**不代表极客版本地规则真的执行了**——文案里要留住这层
边界，验证链路见参考文档 4.2.1。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

SERVICE = "virtual-service"
ACTION = "emit-virtual-event"


@dataclass(frozen=True, slots=True)
class Target:
    """一台能发虚拟事件的中枢。``(siid, aiid)`` 来自规格，不来自常量表。"""

    did: str
    name: str
    model: str
    siid: int
    aiid: int
    online: bool = True

    @property
    def label(self) -> str:
        return self.name or self.did


def _find_action(spec: Any, name: str) -> Any:
    """先在 ``virtual-service`` 里找；固件改了服务名再全服务搜同名 action。"""
    if spec is None:
        return None
    services = getattr(spec, "services", ()) or ()
    ordered = [s for s in services if s.name == SERVICE]
    ordered += [s for s in services if s.name != SERVICE]
    for svc in ordered:
        for act in getattr(svc, "actions", ()) or ():
            if act.name == name:
                return act
    return None


def target_for(st: Any) -> Target | None:
    """从一条 ``DeviceState`` 解析出中枢坐标；不是中枢返回 ``None``。"""
    act = _find_action(getattr(st, "spec", None), ACTION)
    node = getattr(st, "node", None)
    did = getattr(st, "did", "") or getattr(node, "did", "")
    if act is None or not did:
        return None
    return Target(
        did=str(did),
        name=str(getattr(st, "name", "") or getattr(node, "name", "") or ""),
        model=str(getattr(node, "model", "") or ""),
        siid=int(act.siid), aiid=int(act.aiid),
        online=bool(getattr(st, "online", True)),
    )


class VirtualEventSender:
    """解析中枢 + 发请求。``api_getter`` / ``states_getter`` 都是延迟调用的可调用对象。"""

    def __init__(
        self,
        api_getter: Callable[[], Any],
        states_getter: Callable[[], Iterable[Any]],
    ) -> None:
        self._api_getter = api_getter
        self._states_getter = states_getter
        #: 上次解析成功的中枢。只在内存里，重启重新探（型号 / 主中枢可能变）
        self._preferred: str = ""

    def resolve(self) -> tuple[Target | None, str]:
        """在主线程调用：挑一台能发虚拟事件的**在线**中枢。

        只给 ``Action.hub`` 为空的旧规则兜底：新规则都按界面选中的中枢走
        :meth:`resolve_did`。绝不退回「写死 siid 4 / aiid 1」：那样固件一变就会
        静默发到错误的服务上。
        """
        found: list[Target] = []
        for st in self._states_getter():
            target = target_for(st)
            if target is not None:
                found.append(target)
        if not found:
            return None, "没找到支持虚拟事件的中枢网关（设备规格可能还没加载完）"
        found.sort(key=lambda t: (t.did != self._preferred, not t.online, t.label))
        chosen = found[0]
        self._preferred = chosen.did
        if not chosen.online:
            return None, f"中枢网关「{chosen.label}」当前离线"
        return chosen, ""

    def resolve_did(self, did: str) -> tuple[Target | None, str]:
        """按规则里存的中枢 did 解析坐标——**多中枢时只认用户选的那台**。

        找不到 / 离线都给一句中文原因。spec 还没加载完时也走"找不到"这一支，
        让日志写明原因，而不是偷换去另一台中枢。
        """
        if not did:
            return None, "这条动作还没有选中枢网关"
        for st in self._states_getter():
            target = target_for(st)
            if target is None or target.did != did:
                continue
            if not target.online:
                return None, f"中枢网关「{target.label}」当前离线"
            return target, ""
        return None, f"找不到中枢网关「{did}」（可能已从家庭移除，或设备规格还没加载完）"

    def call(self, target: Target, name: str) -> tuple[bool, str]:
        """**同步**调用云端，跑在 SerialWorker 线程里。返回 ``(成功?, 中文说明)``。"""
        if not name.strip():
            return False, "事件名是空的"
        payload = {
            "did": target.did,
            "siid": target.siid,
            "aiid": target.aiid,
            "in": [name],
        }
        try:
            res = self._api_getter().run_action(payload)
        except Exception as exc:  # noqa: BLE001  上游抛什么的都有
            return False, f"{type(exc).__name__}: {exc}"
        if isinstance(res, dict) and res.get("code") == 0:
            return True, f"云端已收下（{res.get('message') or 'ok'}）；末端动作请听设备反馈"
        if isinstance(res, dict):
            return False, f"云端返回 code={res.get('code')} {res.get('message') or ''}".strip()
        return False, f"云端返回了看不懂的结果：{str(res)[:120]}"


__all__ = ["ACTION", "SERVICE", "Target", "VirtualEventSender", "target_for"]
