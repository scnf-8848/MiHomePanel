# SPDX-License-Identifier: GPL-3.0-or-later
"""验证 ``core/automation/action_catalog.py``：白名单、可用性判据、参数清洗。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_action_catalog.py``

全部离线，用假规格。四个方向要钉住：

* **白名单**：音箱只有三个功能、中枢只有一个，别的 action（``play`` / ``tv-switchon``）
  一个都不许漏出来；
* **可用性**：设备少入参、多入参都不显示这个功能——宁可少一个功能，也不发错参数的请求；
* **家庭**：只列当前家庭的设备，没挂家庭的算任何家庭可见；
* **参数**：文本走音箱白名单、事件名不走，必填为空要报错，bool 恒为 bool。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["MIHOME_DATA_DIR"] = str(Path(tempfile.mkdtemp(prefix="mihome-cat-")) / "data")

from mihome.core.automation import action_catalog as C  # noqa: E402
from mihome.core.spec import parse_raw  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(
        f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}",
        flush=True,
    )
    if not ok:
        FAILS.append(f"{name} {detail}")


def _urn(kind: str, name: str, model: str = "demo") -> str:
    return f"urn:miot-spec-v2:{kind}:{name}:0000A043:{model}:1"


def _prop(iid: int, name: str, fmt: str) -> dict:
    return {"iid": iid, "type": _urn("property", name), "format": fmt, "access": []}


def _act(iid: int, name: str, ins: list[int]) -> dict:
    return {"iid": iid, "type": _urn("action", name), "in": ins}


def speaker_spec(*, siid: int = 7, directive_in: list[int] | None = None,
                 play_in: list[int] | None = None,
                 extra_action: str = "") -> object:
    """一台音箱。三个目标动作的 iid 按 oh2p 的真实值造（1/3/4）。"""
    actions = [
        _act(1, "wake-up", []),
        _act(3, "play-text", [1] if play_in is None else play_in),
        _act(4, "execute-text-directive", [1, 2] if directive_in is None else directive_in),
        # 白名单之外的动作：一个都不该冒出来
        _act(5, "play-music", []),
        _act(6, "stop-alarm", []),
        _act(7, "tv-switchon", []),
    ]
    if extra_action:
        actions.append(_act(8, extra_action, []))
    raw = {
        "type": _urn("device", "speaker", "xiaomi-oh2p"),
        "services": [{
            "iid": siid,
            "type": _urn("service", "intelligent-speaker"),
            "properties": [_prop(1, "text-content", "string"),
                           _prop(2, "silent-execution", "bool")],
            "actions": actions,
            "events": [],
        }],
    }
    return parse_raw(raw)


def hub_spec() -> object:
    raw = {
        "type": _urn("device", "gateway", "xiaomi-hub1"),
        "services": [{
            "iid": 4,
            "type": _urn("service", "virtual-service"),
            "properties": [_prop(1, "virtual-param", "string")],
            "actions": [_act(1, "emit-virtual-event", [1])],
            "events": [],
        }],
    }
    return parse_raw(raw)


def dumb_spec() -> object:
    """一台没有任何 action 的设备（灯）。"""
    raw = {
        "type": _urn("device", "light", "demo-light"),
        "services": [{
            "iid": 2, "type": _urn("service", "light"), "properties": [],
            "actions": [], "events": [],
        }],
    }
    return parse_raw(raw)


def state(did: str, name: str, spec, *, online: bool = True, room: str = "客厅",
          home: str = "", model: str = "xiaomi-demo") -> object:
    return SimpleNamespace(
        did=did, name=name, spec=spec, online=online, room=room,
        node=SimpleNamespace(did=did, name=name, home=home, home_id=home, model=model),
    )


# ---------------------------------------------------------------------------
def test_whitelist() -> None:
    feats = C.features_for(speaker_spec())
    names = [f.name for f in feats]
    check("音箱只有三个功能", names == ["wake-up", "play-text", "execute-text-directive"],
          str(names))
    check("中文标签取本地表", [f.label for f in feats] == ["唤醒", "播报文字", "执行文本指令"],
          str([f.label for f in feats]))
    check("白名单外的动作不冒出来",
          "play-music" not in names and "tv-switchon" not in names)

    hub = C.features_for(hub_spec())
    check("中枢只有一个功能", [f.name for f in hub] == ["emit-virtual-event"],
          str([f.name for f in hub]))
    check("中枢的功能标签", hub[0].label == "发送虚拟事件" if hub else False)

    check("没有动作的设备是空的", C.features_for(dumb_spec()) == ())

    wake = C.feature("wake-up")
    check("唤醒没有参数", wake is not None and wake.params == ())
    check("不是白名单的名字返回 None", C.feature("play-music") is None)


def test_usable() -> None:
    # 少一个必填入参：执行文本指令不显示
    short = speaker_spec(directive_in=[1])
    names = [f.name for f in C.features_for(short)]
    check("少入参的功能不显示", "execute-text-directive" not in names, str(names))
    check("别的功能不受影响", "wake-up" in names and "play-text" in names, str(names))

    # 多一个不认识的入参：形状变了，不猜
    fat = speaker_spec(directive_in=[1, 2, 3])
    names = [f.name for f in C.features_for(fat)]
    check("多入参的功能也不显示", "execute-text-directive" not in names, str(names))

    # 播报的入参数量变了
    odd = speaker_spec(play_in=[1, 2])
    check("播报入参变多也不显示",
          "play-text" not in [f.name for f in C.features_for(odd)])

    feat = C.feature("execute-text-directive")
    ok_spec = speaker_spec()
    check("action_for 给得出 action",
          C.action_for(ok_spec, feat) is not None)
    check("action_for 对用不了的功能给 None",
          C.action_for(short, feat) is None)
    check("action_for 对非白名单功能给 None",
          C.action_for(ok_spec, C.feature("nope")) is None)


def test_collect() -> None:
    states = [
        state("d1", "小爱音箱Pro", speaker_spec(), room="客厅", home="h1"),
        state("d2", "客厅中枢", hub_spec(), room="客厅", home="h1"),
        state("d3", "卧室灯", dumb_spec(), room="卧室", home="h1"),
        state("d4", "别的家的音箱", speaker_spec(), room="书房", home="h2"),
        state("d5", "没挂家庭的音箱", speaker_spec(), online=False, room="阳台", home=""),
    ]
    got = C.collect(states, "h1")
    check("只列当前家庭 + 没挂家庭的", [d.did for d in got] == ["d2", "d1", "d5"],
          str([d.did for d in got]))
    check("没有动作的设备不进列表", "d3" not in [d.did for d in got])
    check("在线的排前面", [d.online for d in got][-1] is False, str([d.online for d in got]))

    every = C.collect(states)
    check("home 为空不过滤", len(every) == 4, str(len(every)))

    only_hub = C.collect([state("d2", "客厅中枢", hub_spec())])
    check("中枢也能被选到", [d.did for d in only_hub] == ["d2"])
    check("设备名缺失时退回型号", C.collect(
        [state("d9", "", speaker_spec(), model="xiaomi-lx06")])[0].label == "xiaomi-lx06",
        C.collect([state("d9", "", speaker_spec(), model="xiaomi-lx06")])[0].label)


def test_params() -> None:
    play = C.feature("play-text")
    values, err = C.clean_values(play, {"text-content": "  有人按门铃  "})
    check("文本去掉首尾空白", values["text-content"] == "有人按门铃", repr(values))
    check("文本没报错", err == "", err)

    # 音箱白名单：emoji 与尖括号被剔
    values, err = C.clean_values(play, {"text-content": "门铃响了🔔<b>"})
    check("播报文字走字符白名单", values["text-content"] == "门铃响了b", repr(values))

    bad, err = C.clean_values(play, {"text-content": "  "})
    check("必填文本为空要报错", err != "" and bad.get("text-content", "") == "", err)

    event = C.feature("emit-virtual-event")
    values, err = C.clean_values(event, {"virtual-param": "极客版_按键(测试)<1>"})
    check("事件名不走音箱白名单",
          values["virtual-param"] == "极客版_按键(测试)<1>", repr(values))
    check("事件名照常判空",
          C.clean_values(event, {"virtual-param": ""})[1] != "")

    directive = C.feature("execute-text-directive")
    values, _ = C.clean_values(directive, {"text-content": "关灯"})
    check("静默缺省是 False", values["silent-execution"] is False, repr(values))
    values, _ = C.clean_values(directive, {"text-content": "关灯",
                                           "silent-execution": 1})
    check("静默被收成 bool", values["silent-execution"] is True, repr(values))

    long_text = "字" * 260
    values, _ = C.clean_values(play, {"text-content": long_text})
    check("文本按 200 字符截断", len(values["text-content"]) == 200,
          str(len(values["text-content"])))

    # 无参功能：不许崩，也不许多出键
    values, err = C.clean_values(C.feature("wake-up"), {})
    check("无参功能给空字典", values == {} and err == "", repr(values))


def test_payload() -> None:
    spec = speaker_spec()
    feat = C.feature("execute-text-directive")
    act = C.action_for(spec, feat)
    got = C.payload_for(feat, act, spec, {"text-content": "关灯",
                                          "silent-execution": True})
    check("按名字拼 in", got == ["关灯", True], str(got))

    # 入参顺序颠倒的固件：名字映射后仍然对（先静默后文本）
    flipped = speaker_spec(directive_in=[2, 1])
    act2 = C.action_for(flipped, feat)
    got2 = C.payload_for(feat, act2, flipped, {"text-content": "关灯",
                                               "silent-execution": True})
    check("顺序颠倒也拼得对（名字映射）", got2 == [True, "关灯"], str(got2))

    hub_feat = C.feature("emit-virtual-event")
    hub = hub_spec()
    got3 = C.payload_for(hub_feat, C.action_for(hub, hub_feat), hub,
                         {"virtual-param": "按键自动化执行"})
    check("中枢只发一个事件名", got3 == ["按键自动化执行"], str(got3))

    wake_feat = C.feature("wake-up")
    got4 = C.payload_for(wake_feat, C.action_for(spec, wake_feat), spec, {})
    check("唤醒发空 in", got4 == [], str(got4))


def main() -> int:
    test_whitelist()
    test_usable()
    test_collect()
    test_params()
    test_payload()
    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：")
        for item in FAILS:
            print("  -", item)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
