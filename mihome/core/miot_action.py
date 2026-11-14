# SPDX-License-Identifier: GPL-3.0-or-later
"""按**名字**解析设备动作：查 action、翻入参名、拼 ``in``。

为什么单独一层
--------------
同一份固件能力，方法号在型号之间完全不同：``wake-up`` 在 ``xiaomi-lx06`` 上是
``aiid 3``、在 ``xiaomi-oh2p`` / ``xiaomi-x6a`` 上是 ``aiid 1``；``play-text``
同样 1 vs 3；``execute-text-directive`` 是 5 vs 4。所以一条 iid 都不许写死，只能按
action 的**名字**在 ``DeviceSpec`` 里查。

``core/speaker.py``（音箱三条动作）、``core/automation/sender.py``（中枢虚拟事件）
和自动化编辑页的「设备动作」都需要这件事，从前各写一份，现在收敛到这里。

三条踩过的细节（前两条在 ``docs/DEVELOPMENT.md``「小爱音箱的文字指令」一节）：

1. ``text-content`` / ``silent-execution`` / ``virtual-param`` 这些入参**只作为 action
   的 ``in`` 存在**，``access`` 是空数组，不是可读可写属性 → 能力探测不能走
   ``spec.writeable()`` / ``prop_names()``，只能看 ``ActionSpec.in_piids`` 再回服务里
   按 piid 取名字，否则一个都识别不到。
2. ``in_piids`` 的顺序就是**实参顺序**，但别假设它一定是 ``(1, 2)``：按 piid 映射到
   名字再拼 ``in``，换型号也不会把"静默"塞进"文本"里。
3. 服务名多数是 ``intelligent-speaker`` / ``virtual-service``，但固件可能改名，所以
   先按提示的服务找，找不到再全服务搜同名 action。

本模块不 import Qt、不碰网络：能离线用假 spec 验（``scripts/verify_miot_action.py``）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def find_action(spec: Any, name: str, service_hint: Iterable[str] | str = ()) -> Any:
    """在规格里按名字找 action；找不到返回 ``None``。

    ``service_hint`` 给一个服务名或一串服务名，**顺序即优先级**：先在它们里面找，
    再退到全服务搜同名。这样 ``intelligent-speaker`` 与 ``virtual-service`` 共用一份
    实现，又不会因为别的服务里恰好有个同名 action 就先命中它。

    ``spec`` 传 ``None``（还没加载完）时返回 ``None``，让调用方走"找不到"这一支，
    不要偷偷换成别的设备。
    """
    if spec is None or not name:
        return None
    hints = (service_hint,) if isinstance(service_hint, str) else tuple(service_hint)
    services = getattr(spec, "services", ()) or ()
    preferred = [s for s in services if getattr(s, "name", "") in hints]
    rest = [s for s in services if getattr(s, "name", "") not in hints]
    for svc in (*preferred, *rest):
        for act in getattr(svc, "actions", ()) or ():
            if getattr(act, "name", "") == name:
                return act
    return None


def arg_names(spec: Any, act: Any) -> tuple[str, ...]:
    """把 ``in_piids`` 翻成入参名字。取不到名字的槽留空串，**保持位置**。

    只按 ``siid`` 匹配服务：入参 property 一定和 action 在同一个服务里，跨服务找
    反而可能撞上同名 property。
    """
    if act is None:
        return ()
    by_piid: dict[int, str] = {}
    for svc in getattr(spec, "services", ()) or ():
        if getattr(svc, "siid", None) != getattr(act, "siid", None):
            continue
        for prop in getattr(svc, "properties", ()) or ():
            by_piid[getattr(prop, "piid", 0)] = getattr(prop, "name", "")
    return tuple(by_piid.get(piid, "") for piid in getattr(act, "in_piids", ()) or ())


def payload(names: Sequence[str], values: Mapping[str, Any],
            fallback: Sequence[Any]) -> list[Any]:
    """按入参名拼 ``in``。**任何一个名字认不出就整条退回** ``fallback``。

    ``fallback`` 是"米家的实际顺序"（调用方给），只在名字对不上时用。宁可退回一个
    已知能跑的次序，也不要猜着往某个槽里塞——猜错的后果是把静默值当文本发出去。
    """
    if not names:
        return list(fallback)
    out: list[Any] = []
    for name in names:
        if name not in values:
            return list(fallback)
        out.append(values[name])
    return out


def ref(act: Any) -> tuple[int, int]:
    """``(siid, aiid)``，直接喂 ``Client.run_action``。"""
    return (int(getattr(act, "siid", 0)), int(getattr(act, "aiid", 0)))


__all__ = ["arg_names", "find_action", "payload", "ref"]
