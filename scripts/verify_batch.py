# SPDX-License-Identifier: GPL-3.0-or-later
"""房间批量开关的离线验证：不联网、不碰真实设备。

用假 Client 检查三件事：

1. 一次请求打多条属性（不是每台发一次）；
2. 失败时逐条回滚到原值并留下"操作失败"提示；
3. 已经是目标状态的设备不下发（避免无意义的写入）。

用法：``.venv/Scripts/python.exe -X utf8 scripts/verify_batch.py``
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mihome.core.api import PropRef  # noqa: E402
from mihome.core.catalog import build_card  # noqa: E402
from mihome.core.jobs import SerialWorker  # noqa: E402
from mihome.core.spec import SpecRepository  # noqa: E402
from mihome.core.store import DeviceState, Store  # noqa: E402
from mihome.core.tree import DeviceNode  # noqa: E402

FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global FAIL
    FAIL += 0 if ok else 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")


class _Caps:
    def save(self) -> None:
        pass


class FakeClient:
    """只实现 write，其余访问都会因为没网而暴露成明确的 AttributeError。"""

    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.calls: list[list[tuple[PropRef, object]]] = []
        self.mutes: list[float | None] = []
        self.capabilities = _Caps()

    def write(self, pairs, mute=None):
        self.calls.append(list(pairs))
        self.mutes.append(mute)
        return {ref: self.code for ref, _ in pairs}


def make_state(did: str, spec, online: bool = True) -> DeviceState:
    node = DeviceNode(did=did, name=f"设备{did}", model=spec.model,
                      spec_type=spec.urn, room="测试", online=online)
    card = build_card(spec)
    st = DeviceState(node=node, spec=spec, card=card)
    for ctl in card.controls:
        if ctl.prop is not None:
            st.keys[ctl.prop.key] = PropRef(did, ctl.prop.siid, ctl.prop.piid)
            if ctl.slot == "power":
                st.values[ctl.prop.key] = False
    for g in card.gangs:
        st.keys[g.power.key] = PropRef(did, g.power.siid, g.power.piid)
        st.values[g.power.key] = False
    return st


def run_worker(worker: SerialWorker, timeout: float = 3.0) -> None:
    """worker 是独立线程，要等它执行完再把回调 drain 出来。"""
    end = time.monotonic() + timeout
    while worker.pending and time.monotonic() < end:
        time.sleep(0.01)
    worker.drain()


def build_store(code: int, urns: list[str]) -> tuple[Store, FakeClient, SerialWorker]:
    repo = SpecRepository()
    store = Store(FakeClient(code), SerialWorker(), repo)  # type: ignore[arg-type]
    client = store.client  # type: ignore[assignment]
    for i, urn in enumerate(urns):
        spec = repo.get(urn)
        assert spec is not None, f"规格没缓存：{urn}"
        st = make_state(f"dev{i}", spec)
        store.devices[st.did] = st
    return store, client, store.worker


def main() -> int:
    snap = json.loads((Path(__file__).parent / "out" / "cloud_snapshot.json").read_text("utf-8"))
    urns = sorted({
        d["spec_type"] for d in snap["devices"]
        if d.get("spec_type") and ":light:" in d["spec_type"]
    })[:2]
    print(f"用两份真实灯规格：{len(urns)} 份")

    print("\n1) 成功路径：两台灯从关到开")
    store, client, worker = build_store(0, urns)
    dids = list(store.devices)
    n = store.set_power_for_devices(dids, True)
    check("一条请求按属性条数计", n >= 1, f"下发 {n} 条，调用 {len(client.calls)} 次")
    check("乐观值立刻生效", all(d.any_on for d in store.devices.values()))
    run_worker(worker)
    check("只发了一次请求", len(client.calls) == 1, f"{len(client.calls)} 次")
    check("回调后乐观队列清空", not any(d.optimistic for d in store.devices.values()))
    check("失败提示为空", not any(d.error for d in store.devices.values()))

    print("\n2) 幂等：已经是开启状态，不应再下发")
    client.calls.clear()
    n = store.set_power_for_devices(dids, True)
    check("没有下发任何属性", n == 0 and not client.calls, f"n={n} calls={len(client.calls)}")

    print("\n3) 失败路径：云端返回不支持，必须逐条回滚")
    store2, client2, worker2 = build_store(-704030013, urns)
    store2.set_power_for_devices(list(store2.devices), True)
    check("乐观值先变成开", all(d.any_on for d in store2.devices.values()))
    run_worker(worker2)
    check("全部回滚为关", all(not d.any_on for d in store2.devices.values()))
    check("卡片留下失败提示", all(d.error for d in store2.devices.values()),
          next(iter(store2.devices.values())).error)

    print("\n4) 离线设备不参与批量")
    store3, client3, _ = build_store(0, urns)
    for d in store3.devices.values():
        d.node.online = False
    n = store3.set_power_for_devices(list(store3.devices), True)
    check("离线设备一条都不下发", n == 0 and not client3.calls, f"n={n}")

    print(f"\n{'全部通过' if not FAIL else f'{FAIL} 项失败'}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
