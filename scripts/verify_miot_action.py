# SPDX-License-Identifier: GPL-3.0-or-later
"""验证 ``core/miot_action.py``：按名字查动作、翻入参名、拼 ``in``。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_miot_action.py``

全部离线。这三件事是"一条 iid 都不许写死"的地基，错了会在真机上表现为
「设备不支持」或者把静默值当文本发出去，所以单独钉一遍：

* 查动作：服务名提示优先、固件改名仍能找到、没有就是 ``None``；
* 翻入参：``in_piids`` → 名字且**保持顺序**，认不出的槽留空串占位；
* 拼 ``in``：按名字取值，名字对不上**整条退回实际顺序**，绝不猜着往某个槽里塞。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 本脚本不建 Qt 对象，但 spec 解析会读缓存目录，指到临时目录免污染
os.environ["MIHOME_DATA_DIR"] = str(Path(tempfile.mkdtemp(prefix="mihome-act-")) / "data")

from mihome.core import miot_action as M  # noqa: E402
from mihome.core.spec import parse_raw  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(
        f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}",
        flush=True,
    )
    if not ok:
        FAILS.append(f"{name} {detail}")


def _urn(kind: str, name: str, model: str) -> str:
    return f"urn:miot-spec-v2:{kind}:{name}:0000A043:{model}:1"


def make_spec(*, service: str = "intelligent-speaker", siid: int = 7,
              text_iid: int = 4, silent_iid: int = 2,
              wake_iid: int = 1, play_iid: int = 3,
              in_order: tuple[int, ...] = (1, 2)) -> object:
    raw = {
        "type": _urn("device", "speaker", "demo"),
        "services": [
            {
                "iid": siid,
                "type": _urn("service", service, "demo"),
                "properties": [
                    {"iid": 1, "type": _urn("property", "text-content", "demo"),
                     "format": "string", "access": []},
                    {"iid": 2, "type": _urn("property", "silent-execution", "demo"),
                     "format": "bool", "access": []},
                ],
                "actions": [
                    {"iid": wake_iid, "type": _urn("action", "wake-up", "demo"), "in": []},
                    {"iid": play_iid, "type": _urn("action", "play-text", "demo"), "in": [1]},
                    {"iid": text_iid,
                     "type": _urn("action", "execute-text-directive", "demo"),
                     "in": list(in_order)},
                ],
                "events": [],
            },
        ],
    }
    return parse_raw(raw)


def test_find() -> None:
    spec = make_spec(siid=7, text_iid=4, wake_iid=1, play_iid=3)
    act = M.find_action(spec, "execute-text-directive", "intelligent-speaker")
    check("按名字查到动作", act is not None and act.aiid == 4, f"{act and act.aiid}")
    check("play-text 也按名字查", (M.find_action(spec, "play-text") or None) is not None)
    wake = M.find_action(spec, "wake-up")
    check("wake-up 无入参", wake is not None and tuple(wake.in_piids) == (),
          f"{wake and wake.in_piids}")
    check("查不到就是 None", M.find_action(spec, "no-such-action") is None)
    check("spec 还没加载完也是 None", M.find_action(None, "wake-up") is None)
    check("名字为空直接 None", M.find_action(spec, "") is None)

    # 服务改名：提示的服务不存在时退到全服务搜同名
    renamed = make_spec(service="smart-speaker", siid=9, text_iid=6)
    got = M.find_action(renamed, "execute-text-directive", "intelligent-speaker")
    check("服务改名仍能找到", got is not None and got.siid == 9,
          f"{got and got.siid}")

    # 两个服务里都有同名动作：提示哪个就命中哪个
    raw = {
        "type": _urn("device", "speaker", "demo"),
        "services": [
            {"iid": 3, "type": _urn("service", "other-service", "demo"),
             "properties": [],
             "actions": [{"iid": 1, "type": _urn("action", "wake-up", "demo"), "in": []}],
             "events": []},
            {"iid": 7, "type": _urn("service", "intelligent-speaker", "demo"),
             "properties": [],
             "actions": [{"iid": 2, "type": _urn("action", "wake-up", "demo"), "in": []}],
             "events": []},
        ],
    }
    both = parse_raw(raw)
    hit = M.find_action(both, "wake-up", "intelligent-speaker")
    check("服务提示优先（不是第一个服务里那个）", hit is not None and hit.siid == 7,
          f"{hit and hit.siid}")


def test_args() -> None:
    spec = make_spec(in_order=(1, 2))
    act = M.find_action(spec, "execute-text-directive")
    check("入参名按 piid 翻出来", M.arg_names(spec, act) == ("text-content", "silent-execution"),
          str(M.arg_names(spec, act)))

    flipped = make_spec(in_order=(2, 1))
    f_act = M.find_action(flipped, "execute-text-directive")
    check("in 顺序颠倒时名字跟着颠倒",
          M.arg_names(flipped, f_act) == ("silent-execution", "text-content"),
          str(M.arg_names(flipped, f_act)))

    # 一个 piid 在服务里没有对应属性 → 留空串占位，不塌成更短的一条
    broken = make_spec(in_order=(1, 9))
    b_act = M.find_action(broken, "execute-text-directive")
    check("认不出的槽留空串占位", M.arg_names(broken, b_act) == ("text-content", ""),
          str(M.arg_names(broken, b_act)))
    check("act 为 None 时返回空 tuple", M.arg_names(spec, None) == ())


def test_payload() -> None:
    names = ("text-content", "silent-execution")
    values = {"text-content": "关灯", "silent-execution": True}
    check("按名字拼 in", M.payload(names, values, ["关灯", True]) == ["关灯", True],
          str(M.payload(names, values, ["关灯", True])))

    flipped = ("silent-execution", "text-content")
    check("名字颠倒也拼得对", M.payload(flipped, values, ["关灯", True]) == [True, "关灯"],
          str(M.payload(flipped, values, ["关灯", True])))

    check("名字对不上就整条退回实际顺序",
          M.payload(("what-is-this",), values, ["关灯", True]) == ["关灯", True],
          str(M.payload(("what-is-this",), values, ["关灯", True])))
    check("空名字退回实际顺序",
          M.payload((), values, ["关灯", True]) == ["关灯", True])
    check("认不出时不会只发出一半", len(M.payload(("text-content", "?"), values,
                                                  ["关灯", True])) == 2)
    check("无参动作就是空 in", M.payload((), {"x": 1}, []) == [])

    # 多出来的值被忽略：只按 names 取，不按 values 全发
    check("多给的入参不会混进 in",
          M.payload(("text-content",), {"text-content": "关灯", "silent-execution": True},
                    ["关灯", True]) == ["关灯"])


def test_ref() -> None:
    spec = make_spec(siid=5, text_iid=5)
    act = M.find_action(spec, "execute-text-directive")
    check("ref 给出 (siid, aiid)", M.ref(act) == (5, 5), str(M.ref(act)))
    check("ref 对 None 不炸并给 (0, 0)", M.ref(None) == (0, 0), str(M.ref(None)))


def main() -> int:
    test_find()
    test_args()
    test_payload()
    test_ref()
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
