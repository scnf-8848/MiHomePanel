# SPDX-License-Identifier: GPL-3.0-or-later
"""验证小爱「执行文本指令」与顶栏指令条。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_speaker.py``

分三段，全部离线：

* **能力与入参**（``core/speaker.py``，纯逻辑）：同一个 action 在三个真实型号上的
  ``siid/aiid`` 不同，验证"按名字查"能各自查对；服务改名也还能找到；不带该能力的
  设备返回 ``None``；``in_piids`` 换个顺序不会把"静默"塞进"文本"。
* **文本清洗**：白名单保留什么、剔除什么，200 字符截断，空白收敛。
* **界面**（``ui/speaker_bar.py`` + 主窗口接线）：回车发送与清空、空输入不响应、
  离线禁用输入但**不禁用选音箱**、记住的目标还在列表里就不动、指令条占的正是
  警告条那条横带。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 主窗口会建 Prefs，别让它落到真实用户目录
_TMP = Path(tempfile.mkdtemp(prefix="mihome-speaker-"))
os.environ["MIHOME_DATA_DIR"] = str(_TMP / "data")

from PySide6.QtCore import QPoint, QRect, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from mihome.core import speaker as S  # noqa: E402
from mihome.core.spec import parse_raw  # noqa: E402
from mihome.ui import speaker_bar as SB  # noqa: E402
from mihome.ui import theme  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(
        f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}",
        flush=True,
    )
    if not ok:
        FAILS.append(f"{name} {detail}")


# ---------------------------------------------------------------------------
# 造规格：三份真实型号的关键差异（iid 一个都不写死，这里只是"输入数据"）
# ---------------------------------------------------------------------------

def _urn(kind: str, name: str, model: str) -> str:
    return f"urn:miot-spec-v2:{kind}:{name}:0000A043:{model}:1"


def make_spec(*, service: str = "intelligent-speaker", siid: int = 7, act: int = 4,
              wake: int = 1, args: tuple[int, ...] = (1, 2), text: bool = True) -> object:
    raw = {
        "type": _urn("device", "speaker", "demo"),
        "services": [
            {
                "iid": 2,
                "type": _urn("service", "device-information", "demo"),
                "properties": [],
                "actions": [],
                "events": [],
            },
            {
                "iid": siid,
                "type": _urn("service", service, "demo"),
                "properties": [
                    {"iid": 1, "type": _urn("property", "text-content", "demo"),
                     "format": "string", "access": []},
                    {"iid": 2, "type": _urn("property", "silent-execution", "demo"),
                     "format": "bool", "access": []},
                ],
                "actions": (
                    [
                        {"iid": wake, "type": _urn("action", "wake-up", "demo"), "in": []},
                        {"iid": act,
                         "type": _urn("action", "execute-text-directive", "demo"),
                         "in": list(args)},
                    ]
                    if text
                    else [
                        {"iid": act, "type": _urn("action", "toggle", "demo"),
                         "in": list(args)},
                    ]
                ),
                "events": [],
            },
        ],
    }
    return parse_raw(raw)


def make_state(did: str, name: str, spec, *, online: bool = True, room: str = "客厅",
               home: str = ""):
    return SimpleNamespace(
        did=did, name=name, spec=spec, online=online, room=room,
        node=SimpleNamespace(model="xiaomi-demo", did=did, name=name, home=home),
    )


# ---------------------------------------------------------------------------
def test_capability() -> None:
    lx06 = make_spec(siid=5, act=5, wake=3)
    oh2p = make_spec(siid=7, act=4, wake=1)
    x6a = make_spec(siid=7, act=4, wake=1)

    a = S.target_for("d1", "小爱音箱Pro", "xiaomi-lx06", lx06)
    b = S.target_for("d2", "智能音箱", "xiaomi-oh2p", oh2p)
    c = S.target_for("d3", "家庭屏", "xiaomi-x6a", x6a)
    check("lx06 解析到 5/5", a is not None and (a.siid, a.aiid) == (5, 5),
          f"{a and (a.siid, a.aiid)}")
    check("oh2p 解析到 7/4", b is not None and (b.siid, b.aiid) == (7, 4),
          f"{b and (b.siid, b.aiid)}")
    check("x6a 解析到 7/4", c is not None and (c.siid, c.aiid) == (7, 4),
          f"{c and (c.siid, c.aiid)}")
    check("三台的 siid/aiid 确实不同（没写死常量）", a and b and a.siid != b.siid)
    check("入参名字按 piid 取到", a is not None and a.args == (S.ARG_TEXT, S.ARG_SILENT),
          str(a and a.args))
    check("ref 是 (did, siid, aiid)", a is not None and a.ref == ("d1", 5, 5))

    check("wake-up lx06 是 5/3", S.wake_ref(lx06) == (5, 3), str(S.wake_ref(lx06)))
    check("wake-up oh2p 是 7/1", S.wake_ref(oh2p) == (7, 1), str(S.wake_ref(oh2p)))

    # 没这个服务 / 没这个 action
    plain = make_spec(service="air-conditioner", siid=2, text=False)
    check("别的服务名里没有该 action → None", S.target_for("d", "空调", "m", plain) is None)
    check("没有 spec → None", S.target_for("d", "?", "m", None) is None)

    # 服务被改名：退一步全服务搜同名 action
    renamed = make_spec(service="smart-speaker", siid=9, act=6, wake=2)
    t = S.target_for("d", "改名音箱", "m", renamed)
    check("服务改名后仍能找到（按名字兜底）", t is not None and (t.siid, t.aiid) == (9, 6),
          f"{t and (t.siid, t.aiid)}")

    # 入参顺序换成 (2, 1)：文本/静默不能错位
    swapped = make_spec(args=(2, 1))
    t2 = S.target_for("d", "换序", "m", swapped)
    check("in_piids 换序后名字也跟着换序", t2 is not None and t2.args ==
          (S.ARG_SILENT, S.ARG_TEXT), str(t2 and t2.args))
    check("换序后实参仍按名字给",
          t2 is not None and S.directive(t2, "关灯", True) == [True, "关灯"],
          str(t2 and S.directive(t2, "关灯", True)))

    # 入参名全认不出 → 退回米家实际顺序
    blind = S.Target("d", "x", "m", 7, 4, args=("", ""))
    check("认不出入参名时退回 [文本, 静默]",
          S.directive(blind, "关灯", True) == ["关灯", True],
          str(S.directive(blind, "关灯", True)))
    check("静默参数被收敛成 bool", S.directive(a, "关灯", 1) == ["关灯", True])
    check("默认不静默", S.directive(a, "现在几点了") == ["现在几点了", False])


def test_collect() -> None:
    spec = make_spec()
    states = [
        make_state("d1", "离线音箱", spec, online=False, room="卧室"),
        make_state("d2", "在线音箱", spec, online=True, room="客厅"),
        make_state("d3", "在线 B", spec, online=True, room="客厅"),
        SimpleNamespace(did="d4", name="灯", spec=make_spec(service="light", text=False),
                        online=True, room="客厅",
                        node=SimpleNamespace(model="m", did="d4", name="灯")),
        SimpleNamespace(did="d5", name="无规格", spec=None, online=True, room="客厅",
                        node=SimpleNamespace(model="m", did="d5", name="无规格")),
    ]
    got = S.collect(states)
    ids = [t.did for t in got]
    check("只收支持文本指令的设备", set(ids) == {"d1", "d2", "d3"}, str(ids))
    check("离线的那台排在最后", ids[-1:] == ["d1"], str(ids))
    check("在线的排前面", got and got[0].online and got[-1].online is False)
    check("房间名带过来了", got and got[0].room == "客厅", str(got and got[0].room))
    check("空列表不炸", S.collect([]) == [])


def test_collect_home() -> None:
    """不同家庭互相独立：1103 的页面里选音箱只该看到 1103 的音箱。

    顶栏指令条和自动化编辑页共用这一份判据——分两处写筛选，早晚会出现
    「指令条看不到、编辑页能选到」这种对不上的情况。
    """
    spec = make_spec()
    states = [
        make_state("d1", "客厅音箱", spec, home="1103 家"),
        make_state("d2", "卧室音箱", spec, home="1103 家"),
        make_state("d3", "老家的音箱", spec, home="老家"),
        make_state("d4", "没挂家庭的音箱", spec, home=""),
    ]
    ids = [t.did for t in S.collect_home(states, "1103 家")]
    check("只返回当前家庭的音箱（+ 不挂家庭的那台）", set(ids) == {"d1", "d2", "d4"},
          str(ids))
    check("别的家庭一个都不漏进来", "d3" not in ids)
    check("没挂家庭的设备哪个家庭都可见（云端没给 home 字段）", "d4" in ids)
    check("切到另一个家庭就换成它自己的",
          set(t.did for t in S.collect_home(states, "老家")) == {"d3", "d4"},
          str([t.did for t in S.collect_home(states, "老家")]))
    check("home 传空串 = 只留没挂家庭的",
          [t.did for t in S.collect_home(states, "")] == ["d4"])
    check("家庭里没有音箱时不抛错", all(t.did == "d4" for t in S.collect_home(states, "不存在的家")),
          str([t.did for t in S.collect_home(states, "不存在的家")]))
    check("collect_home([]) 不炸", S.collect_home([], "1103 家") == [])
    # node 缺 home 字段（旧缓存 / 别的设备类型）不能把整条链路炸掉
    bare = SimpleNamespace(did="d9", name="x", spec=spec, online=True, room="客厅",
                           node=SimpleNamespace(model="m", did="d9", name="x"))
    check("node 没有 home 字段时按不挂家庭处理",
          [t.did for t in S.collect_home([bare], "1103 家")] == ["d9"])


def test_clean() -> None:
    got = S.clean("把客厅灯关掉")
    check("纯中文原样保留", got.text == "把客厅灯关掉" and not got.dropped, got.text)

    got = S.clean("音量调到20")
    check("数字保留（剔除会改语义）", got.text == "音量调到20", got.text)

    got = S.clean("Hello World 打开台灯")
    check("英文与空格保留", got.text == "Hello World 打开台灯", got.text)

    got = S.clean("关灯，谢谢。好的？真的！")
    check("四种中文标点保留", got.text == "关灯，谢谢。好的？真的！", got.text)

    got = S.clean("关灯,谢谢")
    check("半角逗号被剔除", got.text == "关灯谢谢" and got.dropped == ",", f"{got.text} / {got.dropped}")
    check("剔除的字符会报回来（去重保序）",
          S.clean("a#b#c%").dropped == "#%", S.clean("a#b#c%").dropped)

    got = S.clean("打开\u200b台灯")
    check("零宽字符被剔除", got.text == "打开台灯" and got.dropped == "\u200b",
          f"{got.text} / {got.dropped!r}")

    got = S.clean("把   灯\t关掉")
    check("连续空白收敛成一个空格", got.text == "把 灯 关掉", repr(got.text))
    check("首尾空白被去掉", S.clean("  关灯  ").text == "关灯", repr(S.clean("  关灯  ").text))

    long = "灯" * 260
    got = S.clean(long)
    check("超长截到 200", len(got.text) == S.MAX_CHARS and got.truncated, str(len(got.text)))
    check("恰好 200 不算截断",
          not S.clean("灯" * 200).truncated and len(S.clean("灯" * 200).text) == 200)

    check("空串就是空", S.clean("").empty)
    check("只有空格也算空", S.clean("   ").empty)
    check("全是非法字符 → 空 + 报告", S.clean("@@@").empty and S.clean("@@@").dropped == "@")


# ---------------------------------------------------------------------------
# 请求形状 —— 2026-11-11 真机报 -704220025 的直接产物
# ---------------------------------------------------------------------------

def test_wire() -> None:
    """action 的参数必须放在 ``in`` 里。

    事故现场：跑到音箱上发「现在几点了」，界面回 ``-704220025``。型号、siid、aiid
    全部核对无误，错在参数键名——写成了属性那套 ``value``，云端在 action 上只认
    ``in``，于是认为"一个参数都没给"，而这个动作要 2 个，正好报"参数个数不匹配"。
    """
    import threading

    from mihome.core.api import Client, classify, explain

    sent: list[dict] = []

    class FakeAPI:
        """只接住 ``run_action`` 的那一层，不碰网络。"""

        def run_action(self, item: object) -> dict:
            assert isinstance(item, dict)
            sent.append(dict(item))
            return {"code": 0}

    client = object.__new__(Client)  # 跳过 __init__：不登录、不建会话
    client._lock = threading.RLock()  # noqa: SLF001
    client._api = FakeAPI()  # noqa: SLF001
    client.last_error = ""

    code = client.run_action(("d1", 5, 5), ["现在几点了", False])
    check("带参动作返回 0", code == 0, str(code))
    check("参数放在 in 里", sent[-1].get("in") == ["现在几点了", False], str(sent[-1]))
    check("action 上绝不再带 value", "value" not in sent[-1], str(sent[-1]))
    check("siid / aiid 原样带过去",
          (sent[-1]["siid"], sent[-1]["aiid"]) == (5, 5), str(sent[-1]))

    client.run_action(("d1", 5, 3))  # wake-up，无参
    check("无参动作不带 in", "in" not in sent[-1] and "value" not in sent[-1], str(sent[-1]))

    # 错误码要能说人话，且必须留着原始码（对文档靠它）
    words = explain(-704220025)
    check("参数个数不匹配能读懂", "参数个数" in words and "-704220025" in words, words)
    check("成功码没有文案", explain(0) == "" and explain(1) == "", repr(explain(1)))
    check("不认识的码仍带原始号", explain(-704299999) == "错误码 -704299999",
          explain(-704299999))
    check("参数错误不归成「不支持」", classify(-704220025) == "unknown",
          classify(-704220025))


# ---------------------------------------------------------------------------
# 界面
# ---------------------------------------------------------------------------

class FakeIcons:
    """只实现 SpeakerBar 用到的两个方法：不给图（走淡圆占位）。"""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def ensure(self, model: str):
        self.asked.append(model)
        return None

    def scaled(self, model: str, size: int):
        return None


def _notes() -> list[str]:
    """把 toast 换成记账，方便断言"提示说了什么"。"""
    box: list[str] = []
    SB.tooltip.toast = lambda anchor, text, pal=None, **kw: box.append(text)  # type: ignore[assignment]
    return box


def test_bar(app: QApplication) -> None:
    icons = FakeIcons()
    bar = SB.SpeakerBar(theme.LIGHT, icons)
    sent: list[str] = []
    changed: list[str] = []
    bar.sent.connect(sent.append)
    bar.target_changed.connect(changed.append)
    notes = _notes()

    check("占位文案原文照抄",
          bar.input.placeholderText() == "输入对音箱指令，按回车发送",
          bar.input.placeholderText())
    check("没有候选时输入被禁用", not bar.input.isEnabled())
    check("没有候选时说清原因", bar.picker.text() == "无可用音箱", bar.picker.text())

    spec = make_spec()
    a = S.target_for("d1", "小爱音箱Pro", "xiaomi-lx06", spec)
    b = S.target_for("d2", "客厅音箱", "xiaomi-oh2p", spec)
    offline = S.target_for("d3", "卧室音箱", "xiaomi-x6a", spec, online=False)
    assert a and b and offline

    bar.set_targets([a, b, offline])
    check("默认选第一台在线的", bar.target() is not None and bar.target().did == "d1")
    check("有目标后输入可用", bar.input.isEnabled())
    check("切换目标会发 target_changed", changed == ["d1"], str(changed))

    # -- 回车发送 ---------------------------------------------------------
    sent.clear()
    bar.input.setText("把客厅灯关掉")
    bar.input.returnPressed.emit()
    check("回车发出清洗后的文本", sent == ["把客厅灯关掉"], str(sent))
    check("发出即清空", bar.input.text() == "", repr(bar.input.text()))
    check("正常文本不弹提示", notes == [], str(notes))

    sent.clear()
    bar.input.setText("   ")
    bar.input.returnPressed.emit()
    check("空输入不发送", sent == [], str(sent))
    check("空输入也不弹提示", notes == [], str(notes))

    bar.input.setText("关灯,谢谢")
    bar.input.returnPressed.emit()
    check("含不支持字符仍会发出（清洗后）", sent == ["关灯谢谢"], str(sent))
    check("并说明剔除了什么", notes and "已忽略不支持的字符" in notes[-1], str(notes))

    notes.clear()
    bar.input.setText("灯" * 260)  # 截断提示发生在"输入时"，不是"发送时"
    notes.clear()
    bar.input.returnPressed.emit()
    check("超长按 200 发出", sent and len(sent[-1]) == 200, str(len(sent[-1]) if sent else 0))
    check("发送时不再重复提示截断", notes == [], str(notes))

    # -- 超长输入（打字 / 粘贴同一条路）：就地截，并提示一次 ------------------
    notes.clear()
    sent.clear()
    bar.input.clear()
    bar.input.setText("灯" * 250)
    check("超长输入截到 200", len(bar.input.text()) == S.MAX_CHARS, str(len(bar.input.text())))
    check("截断会提示", notes and "最多 200" in notes[-1], str(notes))

    # 真·剪贴板粘贴：Qt 走的是内部控制路径，验证它同样被截住
    notes.clear()
    app.clipboard().setText("开" * 240)
    bar.input.clear()
    bar.input.paste()
    check("粘贴超长也截到 200", len(bar.input.text()) == S.MAX_CHARS,
          str(len(bar.input.text())))
    check("粘贴截断也提示", notes and "最多 200" in notes[-1], str(notes))
    check("光标停在末尾而不是开头", bar.input.cursorPosition() == S.MAX_CHARS,
          str(bar.input.cursorPosition()))
    bar.input.clear()

    # -- 目标变化 ---------------------------------------------------------
    changed.clear()
    bar.set_targets([a, b, offline], preferred="d2")
    check("当前目标仍在列表里就不动（优先于 preferred）",
          bar.target().did == "d1", bar.target().did)
    check("没变就不重复发 target_changed", changed == [], str(changed))

    bar.set_targets([b, offline], preferred="d2")
    check("当前目标消失后回落到 preferred", bar.target().did == "d2", bar.target().did)

    # 手动选过之后，即使它离线也不自动换台（详见 TODO 的离线处理）
    b_off = S.target_for("d2", "客厅音箱", "xiaomi-oh2p", spec, online=False)
    assert b_off
    bar.set_targets([a, b_off], preferred="d3")
    check("目标离线后仍留在原位", bar.target().did == "d2", bar.target().did)
    check("目标离线则输入禁用", not bar.input.isEnabled())
    check("但选音箱仍可用（得能切走）", bar.picker.isEnabled())
    check("名字上标出离线", "离线" in bar.picker.text(), bar.picker.text())

    bar.set_targets([a, b], preferred="d2")
    check("目标回来就恢复可用", bar.input.isEnabled())
    check("恢复后目标优先回 preferred（原来的已不在列表）",
          bar.target().did == "d2", bar.target().did)

    # -- 离线项排在选择列表里也点不动 ---------------------------------------
    bar.set_targets([b, offline])
    check("离线项在列表里保留（能看到但不给选）",
          [t.did for t in bar._targets] == ["d2", "d3"],  # noqa: SLF001
          str([t.did for t in bar._targets]))  # noqa: SLF001

    # -- 候选面板：横向设备方块、置灰、静默开关 ------------------------------
    bar.set_targets([b, offline])
    panel = bar._build_panel()  # noqa: SLF001
    tiles = panel.tiles
    check("面板列出全部候选（含离线的）", len(tiles) == 2, str(len(tiles)))
    check("在线那台可点", tiles[0].did() == "d2", tiles[0].did())
    check("当前目标在面板里高亮", tiles[0].is_current() and not tiles[1].is_current(),
          f"{tiles[0].is_current()} / {tiles[1].is_current()}")
    check("离线那台点不动", not tiles[1]._target.online,  # noqa: SLF001
          str(tiles[1]._target.online))  # noqa: SLF001
    click_off: list[str] = []
    tiles[1].picked.connect(click_off.append)
    QTest.mouseClick(tiles[1], Qt.LeftButton)
    check("真点一下离线方块也不发 picked", click_off == [], str(click_off))
    check("方块宽度夹在 88–120 之间",
          all(SB.TILE_MIN_W <= t.width() <= SB.TILE_MAX_W for t in tiles),
          str([t.width() for t in tiles]))
    check("一排方块等宽（图标间距才均匀）", len({t.width() for t in tiles}) == 1,
          str([t.width() for t in tiles]))
    check("设备图标放大到 56px（产品图比名字更好认）", SB.TILE_ICON >= 56,
          str(SB.TILE_ICON))

    # 点方块 → 选出这台，并收起面板
    got: list[str] = []
    panel.picked.connect(got.append)
    panel._on_pick("d2")  # noqa: SLF001
    check("点某台会发出 picked 并收起面板", got == ["d2"] and not panel.isVisible(), str(got))

    # -- 静默：条上下拉（2026-11-13 从小勾选框改的）--------------------------
    bar.set_silent(False)
    check("静默下拉默认「不静默」", bar.silent() is False
          and bar.silent_combo.currentText() == "不静默",
          bar.silent_combo.currentText())
    check("下拉只有两档（静默 / 不静默）", bar.silent_combo.count() == 2,
          str(bar.silent_combo.count()))
    seen: list[bool] = []
    bar.silent_changed.connect(seen.append)
    bar.silent_combo.setCurrentIndex(bar.silent_combo.findData(True))
    check("条上选「静默」会传到 MainWindow", bar.silent() is True and seen == [True],
          str(seen))
    bar.set_silent(False)
    check("同值重复设置不再发信号", seen == [True, False], str(seen))
    check("下拉无边框透明底（不套双层框）",
          "border: none" in bar.silent_combo.styleSheet(), bar.silent_combo.styleSheet()[:60])
    # 三格顺序：选音箱 → 静默 → 输入框
    bar.resize(860, SB.H)
    bar.show()
    app.processEvents()
    check("三格从左到右：音箱 / 静默 / 输入框",
          bar.picker.geometry().left() < bar.silent_combo.geometry().left()
          < bar.input.geometry().left(),
          f"{bar.picker.geometry().left()} / {bar.silent_combo.geometry().left()} "
          f"/ {bar.input.geometry().left()}")
    check("静默下拉与选音箱那格等高",
          bar.silent_combo.height() == bar.picker.height(),
          f"{bar.silent_combo.height()} / {bar.picker.height()}")
    # 同一个状态不给第二个开关：面板里那一行已经删掉了
    check("面板里不再有第二个静默开关", not hasattr(panel, "silent_row"))

    # 面板尺寸：只有一排设备方块，条本身仍是 48（不参与布局）
    panel = bar._build_panel()  # noqa: SLF001
    panel.adjustSize()
    check("面板至少容纳一排方块",
          panel.height() >= SB.TILE_H + SB.PANEL_PAD * 2, str(panel.height()))
    # 真正画一遍：面板/方块两处 paintEvent 有异常会在这里冒出来
    shot = panel.grab()
    check("面板能画出来（圆角底 + 方块）", not shot.isNull() and shot.width() > 0,
          f"{shot.width()}×{shot.height()}")

    bar.set_palette(theme.DARK)
    check("换主题不炸", bar.input.isEnabled())
    check("换主题后静默下拉的覆盖样式还在",
          "background: transparent" in bar.silent_combo.styleSheet())


def test_panel_flow(app: QApplication) -> None:
    """面板真的弹出来以后的收场：Esc / 失去激活 / 选设备，三条路都得收干净。

    用可见窗口跑（离屏也走同一套 Qt 窗口层），因为这正是 ``event()``/``hideEvent``
    里的逻辑，光量尺寸测不到。
    """
    from PySide6.QtCore import QEvent

    icons = FakeIcons()
    host = QWidget()
    host.resize(900, 400)
    bar = SB.SpeakerBar(theme.DARK, icons, host)
    spec = make_spec()
    ts = [t for t in (S.target_for("d1", "小爱音箱Pro", "xiaomi-lx06", spec),
                      S.target_for("d2", "客厅音箱", "xiaomi-oh2p", spec)) if t]
    bar.set_targets(ts)
    bar.setGeometry(20, 20, 860, SB.H)
    host.show()
    app.processEvents()

    def shown() -> list:
        return [p for p in bar.findChildren(SB._Panel) if p.isVisible()]

    def open_one() -> object:
        # 先把上一块收掉，免得拿到旧的
        for p in shown():
            p.hide()
        app.processEvents()
        bar._open_panel()  # noqa: SLF001
        app.processEvents()
        got = shown()
        return got[0] if got else None

    p = open_one()
    check("点 picker 能弹出面板", p is not None)
    check("面板落在指令条正下方（同一条左边缘）",
          p is not None and abs(p.x() - bar.mapToGlobal(QPoint(0, 0)).x()) <= 1,
          str(p.x() if p else None))

    QTest.keyClick(p, Qt.Key_Escape)
    app.processEvents()
    check("按 Esc 收起面板", shown() == [], str(len(shown())))

    p = open_one()
    p.event(QEvent(QEvent.WindowDeactivate))
    app.processEvents()
    check("失去激活也收起（没有窗口管理器时的保险）", shown() == [], str(len(shown())))

    p = open_one()
    before = bar.target().did
    QTest.mouseClick(p.tiles[1], Qt.LeftButton)
    app.processEvents()
    check("点方块切换目标", bar.target().did == "d2" and before == "d1",
          f"{before} -> {bar.target().did}")
    check("选完就收起", shown() == [], str(len(shown())))

    p = open_one()
    check("面板里没有第二个静默开关（状态只在条上那一格）",
          not hasattr(p, "silent_row"))
    # 条上的下拉在面板开着时也能用，且不会把面板收掉（选完设备才收）
    bar.silent_combo.setCurrentIndex(bar.silent_combo.findData(True))
    app.processEvents()
    check("条上切静默不收起面板", bar.silent() is True and shown() != [], str(len(shown())))
    bar.set_silent(False)
    host.close()


def test_main_window(app: QApplication) -> None:
    from mihome.ui.main_window import ALERT_MARGIN_TOP, ALERT_MARGIN_X, MainWindow

    win = MainWindow(theme.LIGHT, tray_available=False)
    win._save_settings = lambda: None  # type: ignore[method-assign]  # noqa: SLF001
    try:
        win.show()
        app.processEvents()
        bar = win.speaker_bar
        check("主窗口有指令条", bar is not None)
        check("指令条挂在右侧栏里", bar.parent() is not None and bar.parent() is not win)

        at = bar.mapTo(win._body, bar.rect().topLeft())  # noqa: SLF001
        x, y = at.x(), at.y()
        check("指令条左边距与警告条一致", x == win.nav.width() + ALERT_MARGIN_X, f"x={x}")
        check("指令条与警告条同高同带",
              (y, bar.height()) == (ALERT_MARGIN_TOP, 48), f"y={y} h={bar.height()}")
        check("指令条铺满卡片区宽度",
              bar.width() == win._body.width() - win.nav.width() - ALERT_MARGIN_X * 2,  # noqa: SLF001
              f"{bar.width()} / {win._body.width()}")  # noqa: SLF001

        # 占位提示：空商店时输入是禁用的，但文案不变
        check("空设备列表下输入禁用", not bar.input.isEnabled())
        check("占位文案不含临时句子",
              bar.input.placeholderText() == "输入对音箱指令，按回车发送",
              bar.input.placeholderText())

        # 警告条一出现就该压住指令条（同一矩形）
        win._on_poll_health({  # noqa: SLF001
            "healthy": False,
            "streak": 3,
            "last_failure": {"kind": "network", "code": "", "text": "", "seconds": 9},
            "episode": {"seconds": 9, "count": 3},
        })
        app.processEvents()
        host = win._alert_host  # noqa: SLF001
        want = QRect(at, bar.size())
        check("警告条出现时正好压在指令条上",
              host.geometry() == want, f"{host.geometry()} / {want}")
        top = win._body.childAt(host.geometry().center())  # noqa: SLF001
        check("警告条确实盖在最上层", top is host or host.isAncestorOf(top), str(top))

        # 记住的目标：写进 prefs，重启后还能读到
        check("默认没有记住的音箱", win.prefs.speaker_command_did == "")
        win._on_speaker_target("d42")  # noqa: SLF001
        check("选中后写入 prefs", win.prefs.speaker_command_did == "d42",
              win.prefs.speaker_command_did)
        from mihome.core.prefs import Prefs
        again = Prefs(win.prefs.path)
        check("落盘后重新读回同一个 did", again.speaker_command_did == "d42",
              again.speaker_command_did)

        # 静默开关：默认关、打开后落盘、重启还在
        check("静默默认关", win.prefs.speaker_silent is False and not win.speaker_bar.silent())
        win.speaker_bar.set_silent(True)
        check("打开静默会写进 prefs", win.prefs.speaker_silent is True)
        check("落盘后重新读回静默", Prefs(win.prefs.path).speaker_silent is True)
        win.speaker_bar.set_silent(False)
        check("关掉静默同样落盘", Prefs(win.prefs.path).speaker_silent is False)

        # -- 发送链路：不真的发，只验证"交给谁、带什么参数" -------------------
        calls: list[tuple] = []
        notes = _notes()

        def fake_action(did, siid, aiid, value=None, *, on_done=None, untargeted=False):
            calls.append((did, siid, aiid, value, on_done, untargeted))

        win.store.run_action = fake_action  # type: ignore[method-assign]  # noqa: SLF001
        spec2 = make_spec(siid=5, act=5, wake=3)  # 按 lx06 的实号造
        t = S.target_for("real", "小爱音箱Pro", "xiaomi-lx06", spec2)
        assert t
        win.speaker_bar.set_targets([t])
        win._on_speaker_send("把客厅灯关掉")  # noqa: SLF001
        check("主窗口按能力号发动作",
              calls and calls[0][:4] == ("real", 5, 5, ["把客厅灯关掉", False]),
              str(calls[0][:4] if calls else None))
        check("默认不静默（第二个实参是 False）",
              bool(calls) and calls[0][3][1] is False, str(calls and calls[0][3]))
        check("音箱指令走无目标确认（untargeted）",
              bool(calls) and calls[0][5] is True, str(calls[0][5] if calls else None))

        notes.clear()
        calls[0][4](0, "")
        check("静默关闭时提示不加尾巴",
              notes and "（静默）" not in notes[-1], str(notes))

        # 开关打开后，同一条指令的第二个实参要变成 True
        calls.clear()
        win.speaker_bar.set_silent(True)
        win._on_speaker_send("把客厅灯关掉")  # noqa: SLF001
        check("静默打开后实参变成 True",
              calls and calls[0][3] == ["把客厅灯关掉", True], str(calls and calls[0][3]))
        notes.clear()
        calls[0][4](0, "")
        check("静默发送的成功提示标出（静默）",
              notes and "（静默）" in notes[-1], str(notes))
        win.speaker_bar.set_silent(False)

        # -- 成功 / 失败提示（重新发一条，静默已关回去）------------------------
        calls.clear()
        win._on_speaker_send("把客厅灯关掉")  # noqa: SLF001
        notes.clear()
        calls[0][4](0, "")
        check("成功提示带设备名与指令",
              notes and "小爱音箱Pro" in notes[-1] and "把客厅灯关掉" in notes[-1], str(notes))

        notes.clear()
        calls[0][4](-10030, "无效的 token")
        check("失败提示带原因", notes and "发送失败" in notes[-1] and "token" in notes[-1],
              str(notes))

        # -- Esc 先照顾正在输入的那一格 --------------------------------------
        win.search.setText("abc")
        win.speaker_bar.input.setEnabled(True)  # 空设备列表下本来是禁用的
        win.speaker_bar.input.setFocus()
        app.processEvents()
        check("焦点确实在指令条里", win.speaker_bar.input.hasFocus())
        win.speaker_bar.input.setText("关灯")
        win._clear_search()  # noqa: SLF001
        check("Esc 先清指令条，不动搜索框",
              win.speaker_bar.input.text() == "" and win.search.text() == "abc",
              f"{win.speaker_bar.input.text()!r} / {win.search.text()!r}")
        win._clear_search()  # noqa: SLF001
        check("再按一次交出焦点",
              not win.speaker_bar.input.hasFocus() and win.search.text() == "abc",
              win.search.text())
        win.search.clear()

        calls.clear()
        off = S.target_for("real", "小爱音箱Pro", "xiaomi-lx06", spec2, online=False)
        assert off
        win.speaker_bar.set_targets([off])
        win._on_speaker_send("把客厅灯关掉")  # noqa: SLF001
        check("目标离线时一个请求都不发", calls == [], str(calls))
    finally:
        win.shutdown()


def main() -> int:
    test_capability()
    test_collect()
    test_collect_home()
    test_clean()
    test_wire()
    app = QApplication.instance() or QApplication([])
    test_bar(app)
    test_panel_flow(app)
    test_main_window(app)
    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败：")
        for f in FAILS:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
