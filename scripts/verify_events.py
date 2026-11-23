# SPDX-License-Identifier: GPL-3.0-or-later
"""设备事件验证：规格解析 → 请求形状 → 归一化 → 详情页文字块。

    PYTHONPATH=. .venv/Scripts/python.exe -X utf8 scripts/verify_events.py

覆盖的都是"线上会静默出错"的地方：

1. **规格里的字段名是复数 `arguments`**——只读单数会让所有事件参数静默为空；
2. `event/get` 的 `value` 是**字符串**，里面才是 `[{piid, value}]`，要二次解析；
3. `code == -9` 是"从来没发生过"，**不是失败**；界面要显示"还没有记录"，不是"读取失败"；
4. 参数只翻**翻得出来**的，时间戳这种丢掉——不然一行变成"锁事件 · 当前时间 1795410088"；
5. 详情页只在打开时读一次，且规格里没有事件源就**不发请求、不占地方**。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mihome.core import events as E  # noqa: E402
from mihome.core.api import Client  # noqa: E402
from mihome.core.catalog import build_card  # noqa: E402
from mihome.core.spec import parse_raw  # noqa: E402
from mihome.core.store import DeviceState, Store  # noqa: E402
from mihome.core.tree import DeviceNode  # noqa: E402

FAIL = 0
NOW = int(time.time())


def check(name: str, got, want) -> None:
    global FAIL
    ok = got == want
    FAIL += 0 if ok else 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"  got={got!r} want={want!r}"))


def check_true(name: str, ok: bool, detail: str = "") -> None:
    global FAIL
    FAIL += 0 if ok else 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail and not ok else ""))


def raw_spec(singular: bool = False) -> dict:
    """一台最小可用的门锁规格：一个带两个参数的事件 + 一个无参事件。"""
    arg_key = "argument" if singular else "arguments"
    return {
        "type": "urn:miot-spec-v2:device:lock:0000A038:test-x1:1",
        "description": "Test Lock",
        "services": [
            {
                "iid": 2,
                "type": "urn:miot-spec-v2:service:door-lock:00007800:test-x1:1",
                "description": "Door Lock",
                "properties": [
                    {
                        "iid": 1,
                        "type": "urn:miot-spec-v2:property:position:00000000:test-x1:1",
                        "format": "uint8",
                        "access": ["read"],
                        "comment": "操作位置",
                        "value-list": [
                            {"value": 1, "description": "Indoor", "comment": "室内"},
                            {"value": 2, "description": "Outdoor", "comment": "室外"},
                        ],
                    },
                    {
                        "iid": 5,
                        "type": "urn:miot-spec-v2:property:timestamp:00000001:test-x1:1",
                        "format": "uint32",
                        "access": ["read"],
                        "comment": "当前时间",
                    },
                    {
                        "iid": 7,
                        "type": "urn:miot-spec-v2:property:name:00000002:test-x1:1",
                        "format": "string",
                        "access": ["read"],
                        "comment": "名称",
                    },
                ],
                "events": [
                    {
                        "iid": 1020,
                        "type": "urn:miot-spec-v2:event:lock-event:00005001:test-x1:1",
                        "description": "Lock Event",
                        "comment": "锁事件",
                        arg_key: [1, 5],
                    },
                    {
                        "iid": 1007,
                        "type": "urn:miot-spec-v2:event:fault:00005002:test-x1:1",
                        "description": "Fault",
                        "comment": "发生异常",
                        arg_key: [],
                    },
                ],
            }
        ],
    }


def payload(*rows) -> list[dict]:
    return list(rows)


def event_row(siid: int, eiid: int, when: int, value=None, code: int = 0) -> dict:
    return {"did": "d1", "siid": siid, "eiid": eiid, "code": code,
            "updateTime": when or None, "value": value}


def test_spec() -> None:
    print("\n1) 规格：事件参数读的是 arguments（复数），也认旧的单数写法")
    spec = parse_raw(raw_spec(), source="test")
    evs = spec.events()
    check("两个事件都解析出来", [(e.siid, e.eiid) for e in evs], [(2, 1020), (2, 1007)])
    check("事件名取 comment", [e.label for e in evs], ["锁事件", "发生异常"])
    check("复数 arguments 进 arg_piids", evs[0].arg_piids, (1, 5))
    check("无参事件是空元组", evs[1].arg_piids, ())

    old = parse_raw(raw_spec(singular=True), source="test")
    check("旧缓存里的单数 argument 也认", old.events()[0].arg_piids, (1, 5))

    check("sources 按规格顺序给 (siid, eiid)", E.sources(spec), [(2, 1020), (2, 1007)])
    check("没有 spec 就是空表", E.sources(None), [])


def test_parse() -> None:
    print("\n2) 归一化：-9 当作没发生、value 二次解析、翻不出的参数丢掉")
    spec = parse_raw(raw_spec(), source="test")
    rows = E.parse(payload(
        event_row(2, 1007, 0, None, code=-9),
        event_row(2, 1020, NOW - 300, '[{"piid":1,"value":2},{"piid":5,"value":1795000000}]'),
    ), spec)

    check("规格顺序被打乱后按时间倒序", [(r.siid, r.eiid) for r in rows], [(2, 1020), (2, 1007)])
    lock = rows[0]
    check("事件名来自规格", lock.label, "锁事件")
    check("参数翻成中文值", lock.detail, "操作位置 室外")
    check_true("时间戳参数被丢掉（翻不出来的不硬塞）", "1795000000" not in lock.detail, lock.detail)
    check_true("有记录", lock.occurred)
    check("相对时间", lock.text(), f"锁事件 · {E.ago(lock.when)}（操作位置 室外）")

    fault = rows[1]
    check("-9 = 没发生过（不是失败）", (fault.occurred, fault.code), (False, -9))
    check("-9 的时间归零，不冒充最近一次", fault.when, 0)
    check("没发生过的文案", fault.text(), "发生异常 · 还没有记录")

    # 只要发生过的：界面据此判断"有没有东西可看"
    check("occurred() 只留有记录的", [r.eiid for r in E.occurred(rows)], [1020])

    print("\n3) 坏数据与未知源")
    bad = E.parse(payload(
        event_row(2, 1020, NOW, "不是 JSON"),
        event_row(2, 1020, NOW - 10, '[{"piid":1,"value":1}]'),
        event_row(9, 9, NOW - 5, None),
        "不是字典",
    ), spec)
    check("重复源只认第一条（4 条进 2 条出）", len(bad), 2)
    check("坏 value 不炸，只是没参数", bad[0].detail, "")
    check("规格里没有的源照实写方法号", bad[-1].label, "事件 9.9")
    check("坏 payload 返回空表", E.parse(None, spec), [])
    check("payload 是 dict 也认（兜底 events 键）",
          len(E.parse({"events": [event_row(2, 1007, 0, None, -9)]}, spec)), 1)

    text = E.parse(payload(event_row(2, 1020, NOW - 5, '[{"piid":7,"value":"阶段关灯判定"}]')), spec)
    check("字符串参数直接显示", text[0].detail, "名称 阶段关灯判定")

    print("\n4) latest / line：界面只画最新那一条")
    many = E.parse(payload(*[
        event_row(2, 1020, NOW - i * 60, '[{"piid":1,"value":2}]') for i in range(5)
    ]), spec)
    # 同一个源重复给只算一条（去重靠 updateTime），所以这里要造不同的源
    check("同一源重复只留一条", len(E.occurred(many)), 1)

    three = [
        E.DeviceEvent(2, 1020, "锁事件", when=NOW - 600),
        E.DeviceEvent(2, 1007, "发生异常", when=NOW - 60),
        E.DeviceEvent(2, 1008, "门铃", when=NOW - 3600),
    ]
    newest = E.latest(three)
    check("取时间最大的一条，不是列表第一条", newest.eiid if newest else None, 1007)
    check("一条都没发生过 → None",
          E.latest([E.DeviceEvent(2, 1, "X", when=0, code=-9)]), None)
    only = E.line(three)
    check("正文就是那一条的原文", only, newest.text() if newest else "")
    check_true("不含另外两条（用户要的是唯一那一条）",
               "锁事件" not in only and "门铃" not in only, only)
    check("没有记录时的文案", E.line([]), "还没有记录")
    check("只有 -9 时也算没有记录",
          E.line([E.DeviceEvent(2, 1, "X", when=0, code=-9)]), "还没有记录")


class FakeApi:
    """``Client.device_events`` 用：记下请求，按需返回 list / dict / 别的东西。"""

    locale = "zh_CN"

    def __init__(self, reply=None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.reply = reply

    def _request(self, path: str, data: dict):
        self.calls.append((path, data))
        if callable(self.reply):
            return self.reply(data)
        return self.reply


def test_client() -> None:
    print("\n5) Client.device_events：params 包一层、分片、整体失败要抛")
    api = FakeApi([{"did": "d1", "siid": 2, "eiid": 1020, "code": 0, "updateTime": 1}])
    client = Client(api=api)  # type: ignore[arg-type]
    out = client.device_events("d1", [(2, 1020), (2, 1007)])
    check("路径", {c[0] for c in api.calls}, {"/miotspec/event/get"})
    body = api.calls[0][1]
    check("必须包在 params 里", list(body), ["params"])
    check("每条带 did / siid / eiid", body["params"],
          [{"did": "d1", "siid": 2, "eiid": 1020}, {"did": "d1", "siid": 2, "eiid": 1007}])
    check("返回原样透传", out, [{"did": "d1", "siid": 2, "eiid": 1020, "code": 0, "updateTime": 1}])

    api2 = FakeApi(lambda data: [{"code": 0}] * len(data["params"]))
    client2 = Client(api=api2)  # type: ignore[arg-type]
    client2.device_events("d1", [(2, i) for i in range(5)], chunk=2)
    check("超长自动分片", [len(c[1]["params"]) for c in api2.calls], [2, 2, 1])

    api3 = FakeApi([])
    client3 = Client(api=api3)  # type: ignore[arg-type]
    check("没有事件源就不发请求", client3.device_events("d1", []), [])
    check("确实一次都没问", len(api3.calls), 0)

    api4 = FakeApi({"code": -8, "message": "params is invalid"})
    client4 = Client(api=api4)  # type: ignore[arg-type]

    def boom() -> str:
        try:
            client4.device_events("d1", [(2, 1020)])
        except Exception as exc:  # noqa: BLE001
            return type(exc).__name__
        return "没抛"

    # 顶层 dict = 整体失败：不能吞成"没有记录"，否则用户看到的是"这台设备没出过事"
    check("整体失败抛 APIError", boom(), "APIError")

    api5 = FakeApi("oops")
    client5 = Client(api=api5)  # type: ignore[arg-type]
    try:
        client5.device_events("d1", [(2, 1020)])
        raised = "没抛"
    except Exception as exc:  # noqa: BLE001
        raised = type(exc).__name__
    check("结构异常也抛", raised, "APIError")


class _Sub:
    """``Store.fetch_device_events`` 用到的那几样。"""

    def __init__(self, spec) -> None:
        self.devices = {"d1": SimpleNamespace(spec=spec)}
        self.calls: list = []
        sub = self

        class Worker:
            @staticmethod
            def submit(fn, args, **kwargs):
                sub.calls.append(args)
                kwargs["callback"](fn(*args), None)

        class Client_:
            @staticmethod
            def device_events(did, sources):
                return [{"did": did, "siid": sources[0][0], "eiid": sources[0][1],
                         "code": 0, "updateTime": NOW, "value": "[]"}]

        self.worker = Worker()
        self.client = Client_()


def test_store() -> None:
    print("\n6) Store.fetch_device_events：没有事件源就不请求")
    spec = parse_raw(raw_spec(), source="test")
    sub = _Sub(spec)
    seen: list = []
    Store.fetch_device_events(sub, "d1", lambda rows, err: seen.append((rows, err)))  # type: ignore[arg-type]
    check("请求带上了这台设备的事件源", sub.calls, [("d1", [(2, 1020), (2, 1007)])])
    check("回调拿到归一化后的行", [r.label for r in (seen[0][0] or [])], ["锁事件"])

    empty = parse_raw({"type": "urn:miot-spec-v2:device:light:0000A001:test-x1:1",
                       "description": "L", "services": []}, source="test")
    sub2 = _Sub(empty)
    seen2: list = []
    Store.fetch_device_events(sub2, "d1", lambda rows, err: seen2.append((rows, err)))  # type: ignore[arg-type]
    check("规格里没事件：不发请求", sub2.calls, [])
    # 空表 ≠ 失败：界面据此整块不显示，而不是挂一句"读取失败"
    check("直接回调空表、没有错误", seen2, [([], None)])


class FakeStore:
    """详情页用：只实现事件取数，其余方法一律 no-op。"""

    def __init__(self, rows=None, error=None) -> None:
        self.devices: dict = {}
        self.rows = rows if rows is not None else []
        self.error = error
        self.calls: list[str] = []

    def fetch_device_events(self, did, done) -> None:
        self.calls.append(did)
        done(None, self.error) if self.error else done(list(self.rows), None)

    def __getattr__(self, name):
        return lambda *a, **k: None


def _dialog(spec, store, pal):
    from mihome.ui.detail import DetailDialog  # noqa: PLC0415

    node = DeviceNode(did="d1", name="示例门锁", model="test.lock.x1",
                      spec_type=spec.urn, room="示例房间", online=True)
    state = DeviceState(node=node, spec=spec, card=build_card(spec))
    store.devices = {state.did: state}
    return DetailDialog(state, store, pal)


def test_detail() -> None:
    print("\n7) 详情页：文字块三态、只读一次、没有弹窗")
    from PySide6.QtWidgets import QApplication  # noqa: PLC0415

    from mihome.ui import theme  # noqa: PLC0415
    from mihome.ui.detail import DetailDialog  # noqa: PLC0415

    app = QApplication.instance() or QApplication(sys.argv)
    spec = parse_raw(raw_spec(), source="test")
    # 故意给三条：两条发生过（一新一旧）+ 一条从来没发生。界面只该显示最新那条，
    # 而且**不能**退化成"列全部"或"按规格顺序取第一条"。
    rows = [
        E.DeviceEvent(2, 1020, "锁事件", when=NOW - 600, detail="操作位置 室外"),
        E.DeviceEvent(2, 1007, "发生异常", when=NOW - 60),
        E.DeviceEvent(2, 1009, "滤芯到期", when=0, code=-9),
    ]

    store = FakeStore(rows=rows)
    dlg = _dialog(spec, store, theme.LIGHT)
    check("构造时还没请求（等窗口上屏）", store.calls, [])
    check_true("构造时整块不占地方", not dlg.events_box.isVisible())
    dlg.show()
    # 不先 processEvents：这里要断言的是"结果还没到时的样子"，而收集结果靠 100ms 的
    # 临时定时器——机器忙的时候 processEvents 可能就跨过那 100ms，断言会偶发失败
    check("等结果时写读取中", dlg._events_text, "读取中…")  # noqa: SLF001
    app.processEvents()
    check("打开页面读一次", store.calls, ["d1"])
    dlg._collect_events()  # noqa: SLF001  不等 100ms 定时器
    check_true("事件块出现", dlg.events_box.isVisible())
    body = dlg._events_text  # noqa: SLF001
    check_true("只有一行文字", "\n" not in body, repr(body))
    check_true("显示的是**最新**那条，不是列表第一条", "发生异常" in body, body)
    check_true("旧的锁事件不显示", "锁事件" not in body, body)
    check_true("没发生过的不占行", "滤芯到期" not in body, body)
    check("只占一行的高度", dlg.events_box.height(), dlg.EVENT_H)
    check("全文确实写进了标签", dlg.events_body._full, body)  # noqa: SLF001

    before = len(store.calls)
    from PySide6.QtGui import QShowEvent  # noqa: PLC0415
    dlg.showEvent(QShowEvent())
    check("再显示一次不重复请求", len(store.calls), before)
    dlg.close()

    print("\n8) 失败与没有事件源分得开")
    bad = _dialog(spec, FakeStore(error=RuntimeError("网络断了")), theme.LIGHT)
    bad.show()
    app.processEvents()
    bad._collect_events()  # noqa: SLF001
    check("失败写读取失败", bad._events_text, "读取失败")  # noqa: SLF001
    bad.close()

    empty_spec = parse_raw({"type": "urn:miot-spec-v2:device:light:0000A001:test-x1:1",
                            "description": "L", "services": []}, source="test")
    estore = FakeStore()
    light = _dialog(empty_spec, estore, theme.LIGHT)
    light.show()
    app.processEvents()
    check("没有事件源的设备不请求", estore.calls, [])
    check_true("整块不出现（不是摆一行没有事件）", not light.events_box.isVisible())
    light.close()

    print("\n9) 不再有「消息记录」弹窗")
    check_true("详情页没有 _open_messages",
               not hasattr(DetailDialog, "_open_messages"))
    check_true("没有「消息记录」按钮",
               not [b for b in dlg.findChildren(object)
                    if callable(getattr(b, "text", None))
                    and str(getattr(b, "text")()).strip() == "消息记录"])


def main() -> int:
    print("设备事件验证")
    test_spec()
    test_parse()
    test_client()
    test_store()
    test_detail()
    print(f"\n{'全部通过' if not FAIL else f'{FAIL} 项失败'}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
