# SPDX-License-Identifier: GPL-3.0-or-later
"""离线验证：能力账本自愈、设备指纹、三个手动重置入口的清除范围与文案。

不联网、不起窗口。跑法：``.venv/Scripts/python.exe scripts/verify_device_reset.py``
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mihome.core.api import CapabilityBook, PropRef  # noqa: E402
from mihome.core.prefs import Prefs  # noqa: E402
from mihome.core.store import Store  # noqa: E402
from mihome.ui.device_reset import (  # noqa: E402
    all_reset_text,
    device_reset_text,
    refresh_result_text,
    reset_result_text,
)

FAILS: list[str] = []
CHECKS = 0


def check(ok: bool, what: str) -> None:
    global CHECKS
    CHECKS += 1
    if not ok:
        FAILS.append(what)


def section(title: str) -> None:
    print(f"\n== {title}")


class StrictClient:
    """只允许访问 capabilities；碰任何别的东西都说明"零请求"被破坏了。"""

    def __init__(self, book: CapabilityBook) -> None:
        self.capabilities = book

    def __getattr__(self, name: str):
        raise AssertionError(f"不该访问 client.{name}（应当零请求）")


class FakeProp:
    def __init__(self, siid: int, piid: int, readable: bool = True) -> None:
        self.siid = siid
        self.piid = piid
        self.readable = readable


class FakeSpec:
    def __init__(self, urn: str, props: list[FakeProp]) -> None:
        self.urn = urn
        self.properties = tuple(props)  # 真实 DeviceSpec 是属性不是方法


def bare_store(book: CapabilityBook, prefs: Prefs) -> Store:
    """不跑 ``__init__``：只装验证用到的那几样成员。"""
    store = Store.__new__(Store)
    store.client = StrictClient(book)
    store.prefs = prefs
    store.devices = {}
    return store


def test_self_heal(tmp: Path) -> None:
    section("账本自愈：只保留当前分类下的『不支持』")
    path = tmp / "heal.json"
    entries = {
        "A|2|1": {"code": -704030013, "at": 1},   # 属性不可读 → 保留
        "A|2|2": {"code": -704002000, "at": 1},   # 旧分类误记 → 丢弃
        "B|2|1": {"code": -704042011, "at": 1},   # 设备离线 → 丢弃
        "B|2|2": {"code": -704220043, "at": 1},   # 暂时性 → 丢弃
        "B|2|3": {"code": "abc", "at": 1},        # 脏数据 → 丢弃
        "B|2|4": "not-a-dict",                    # 脏数据 → 丢弃
    }
    path.write_text(json.dumps({"version": 1, "entries": entries}), encoding="utf-8")

    book = CapabilityBook(path)
    check(len(book) == 1, "自愈后只应剩 1 条")
    check(book.unsupported(PropRef("A", 2, 1)), "不可读的那条应保留")
    check(not book.unsupported(PropRef("A", 2, 2)), "旧分类的 -704002000 应丢弃")
    check(not book.unsupported(PropRef("B", 2, 1)), "离线码应丢弃")
    check(not book.unsupported(PropRef("B", 2, 2)), "暂时性错误码应丢弃")

    raw = json.loads(path.read_text(encoding="utf-8"))
    check(len(raw.get("entries") or {}) == 1, "丢弃结果应回写文件（常态规则，不是一次性迁移）")

    again = CapabilityBook(path)
    check(len(again) == 1, "再次读入应保持稳定（幂等）")


def test_fingerprint(tmp: Path) -> None:
    section("设备指纹：只清指纹变了的设备")
    path = tmp / "fp.json"
    book = CapabilityBook(path)

    check(book.sync_devices({"A": "urn:v1", "B": "urn:v1"}) == [], "首次登记不应清任何账")
    book.mark_unsupported(PropRef("A", 2, 1), -704030013)
    book.mark_unsupported(PropRef("B", 2, 1), -704030013)
    book.save()

    check(book.sync_devices({"A": "urn:v1", "B": "urn:v1"}) == [], "指纹不变不应清账")
    check(book.count_for("A") == 1 and book.count_for("B") == 1, "指纹不变时两台的账都还在")

    cleared = book.sync_devices({"A": "urn:v2", "B": "urn:v1"})
    check(cleared == ["A"], "只有 A 的指纹变了，只应清 A")
    check(book.count_for("A") == 0, "A 的账应清空")
    check(book.count_for("B") == 1, "B 的账不能被误清")

    before = len(book)
    book.sync_devices({"C": ""})
    check(len(book) == before, "空指纹（没有规格）应跳过")
    check(book.count_for("C") == 0, "没有规格的设备不应留下任何记录")

    book.save()
    reloaded = CapabilityBook(path)
    check(reloaded.sync_devices({"B": "urn:v1"}) == [], "指纹要落盘，重启后仍能比对")
    check(reloaded.sync_devices({"A": "urn:v2"}) == [], "A 的新指纹已登记，不应重复清")


def test_zero_requests(tmp: Path) -> None:
    section("启动比对：零额外请求")
    path = tmp / "zero.json"
    book = CapabilityBook(path)
    prefs = Prefs(tmp / "zero-prefs.json")
    store = bare_store(book, prefs)
    store.devices = {
        "A": SimpleNamespace(spec=FakeSpec("urn:a", [FakeProp(2, 1)])),
        "B": SimpleNamespace(spec=None),  # 没规格 → 跳过
    }
    # client 是 StrictClient：除 capabilities 外任何访问都会抛 AssertionError
    try:
        store.sync_capability_fingerprints()
        first_ok = True
    except AssertionError:
        first_ok = False
    check(first_ok, "指纹比对不应碰 client 的其它方法（零请求）")
    check("A" in json.loads(path.read_text(encoding="utf-8")).get("devices", {}),
          "有规格的设备应登记指纹")
    check("B" not in json.loads(path.read_text(encoding="utf-8")).get("devices", {}),
          "没规格的设备不应登记")


def test_reset_scopes(tmp: Path) -> None:
    section("手动入口：单设备 / 全部重置的清除范围")
    path = tmp / "reset.json"
    book = CapabilityBook(path)
    prefs = Prefs(tmp / "reset-prefs.json")
    store = bare_store(book, prefs)
    store._layout_changed = lambda: None

    prefs.set_card_style("A", "compact")
    prefs.set_hidden("A", True)
    prefs.set_card_style("B", "wide")
    prefs.set_device_order("h", "r", ["B"])
    book.mark_unsupported(PropRef("A", 2, 1), -704030013)
    book.mark_unsupported(PropRef("B", 2, 1), -704030013)

    counts = store.device_config_counts("A")
    check(counts == {"style": 1, "hidden": 1, "order": 0, "caps": 1},
          f"A 的计数应准确，实际 {counts}")

    got = store.reset_device_config("A")
    check(got["style"] == 1 and got["hidden"] == 1 and got["caps"] == 1,
          f"单设备重置应清样式/隐藏/能力缓存，实际 {got}")
    check(prefs.card_style("A") is None, "A 的卡片样式应回默认")
    check(not prefs.is_hidden("A"), "A 的隐藏状态应清除（回到卡片墙原位置）")
    check(book.count_for("A") == 0, "A 的能力缓存应清空")
    check(prefs.card_style("B") == "wide" and book.count_for("B") == 1,
          "重置 A 不能动到 B")

    counts_all = store.all_device_config_counts()
    check(counts_all["devices"] == 1 and counts_all["style"] == 1
          and counts_all["order"] == 1 and counts_all["caps"] == 1,
          f"全部重置的计数应准确，实际 {counts_all}")

    got_all = store.reset_all_device_config()
    check(got_all["style"] == 1 and got_all["order"] == 1 and got_all["caps"] == 1,
          f"全部重置应清样式/排序/能力缓存，实际 {got_all}")
    check(not prefs.card_styles and not prefs.hidden and not prefs.device_order,
          "全部重置后偏好三项都应归零")
    check(len(book) == 0, "全部重置后能力账本应为空")


def test_refresh(tmp: Path) -> None:
    section("强制刷新：先清缓存，再重查全部可读属性")
    path = tmp / "refresh.json"
    book = CapabilityBook(path)
    prefs = Prefs(tmp / "refresh-prefs.json")
    store = bare_store(book, prefs)
    book.mark_unsupported(PropRef("A", 2, 1), -704030013)

    spec = FakeSpec("urn:a", [FakeProp(2, 1), FakeProp(2, 2), FakeProp(3, 1, readable=False)])
    store.devices = {"A": SimpleNamespace(spec=spec)}
    calls: list[tuple[str, int]] = []
    done: list[tuple[int, int, str]] = []

    def fake_read(did, props, callback):
        calls.append((did, len(props)))
        callback({PropRef("A", p.siid, p.piid): SimpleNamespace(ok=True) for p in props}, "")

    store.read_props = fake_read  # type: ignore[method-assign]
    store.refresh_capabilities("A", lambda ok, failed, err: done.append((ok, failed, err)))

    check(book.count_for("A") == 0, "刷新应先清掉 A 的能力缓存")
    check(calls == [("A", 2)], f"应立刻重查全部可读属性（不可读的不查），实际 {calls}")
    check(done == [(2, 0, "")], f"回调应报出成功/失败项数，实际 {done}")

    # 清除 0 条也必须回调（不能被"没有记录"提前吞掉），否则界面静默无反馈
    calls.clear()
    done.clear()
    store.refresh_capabilities("A", lambda ok, failed, err: done.append((ok, failed, err)))
    check(done == [(2, 0, "")], f"清除 0 条也要触发提示，实际 {done}")
    check(calls == [("A", 2)], "清除 0 条时同样要立刻重查")

    # 部分失败
    def partial(did, props, callback):
        got = {PropRef("A", p.siid, p.piid): SimpleNamespace(ok=(i == 0))
               for i, p in enumerate(props)}
        callback(got, "")

    store.read_props = partial  # type: ignore[method-assign]
    done.clear()
    store.refresh_capabilities("A", lambda ok, failed, err: done.append((ok, failed, err)))
    check(done == [(1, 1, "")], f"部分失败应报出成功/失败项数，实际 {done}")

    # 全部失败
    store.read_props = lambda did, props, cb: cb({}, "设备离线")  # type: ignore[method-assign]
    done.clear()
    store.refresh_capabilities("A", lambda ok, failed, err: done.append((ok, failed, err)))
    check(done == [(0, 2, "设备离线")], f"全失败要按总项数计失败，实际 {done}")


def test_texts() -> None:
    section("确认窗文案：逐项列出 + 关键说明")
    one = device_reset_text("客厅灯", {"style": 1, "hidden": 1, "order": 1, "caps": 2})
    for kw in ("客厅灯", "能力缓存", "卡片样式", "隐藏状态", "房间内排序",
               "原位置", "米家 App", "登录", "2 条"):
        check(kw in one, f"单设备文案缺少「{kw}」")

    everything = all_reset_text({"devices": 3, "style": 2, "hidden": 1, "order": 4, "caps": 9})
    for kw in ("所有设备", "有本地配置的设备：3 台", "卡片样式", "隐藏状态",
               "房间内排序", "原位置", "米家 App", "登录"):
        check(kw in everything, f"全部重置文案缺少「{kw}」")

    ok = refresh_result_text(30, 0)
    check(ok == "已刷新 30 项属性", f"全成功文案不对：{ok}")
    part = refresh_result_text(26, 4)
    check(part == "已刷新 26 项属性", f"有未读到的也只说刷新了多少项：{part}")
    bad = refresh_result_text(0, 30, "设备离线")
    check(bad == "设备暂时无响应，稍后会自动重试", f"全没读到文案不对：{bad}")
    check("自动重试" in bad, "全没读到要说明会自动重试")
    bad2 = refresh_result_text(0, 4)
    check("自动重试" in bad2, f"即使没有原因也要说明会自动重试：{bad2}")

    reset = reset_result_text("客厅灯")
    check(reset == "已重置「客厅灯」的配置", f"重置文案不对：{reset}")

    for text in (ok, part, bad, bad2, reset):
        for word in ("记录", "能力缓存", "账本", "失败", "未读到"):
            check(word not in text, f"界面文案不应出现「{word}」：{text}")
    for text in (ok, part):
        for word in ("无响应", "稍后", "重试"):
            check(word not in text, f"读到属性时不应暗示异常：{text}")


def test_feedback_channel() -> None:
    section("提示通道：主窗口内的说话条 toast，不按屏幕坐标定位")
    from mihome.ui import main_window as mw

    class FakeBar:
        def __init__(self) -> None:
            self.notes: list[str] = []
            self.kinds: list[str] = []

        def notice(self, text: str, kind: str = "ok") -> None:
            self.notes.append(text)
            self.kinds.append(kind)

    class FakeStore:
        def __init__(self, ok: int, failed: int, error: str) -> None:
            self._o, self._f, self._e = ok, failed, error

        def refresh_capabilities(self, did, done):
            done(self._o, self._f, self._e)

    class FakeWin:
        def __init__(self, store) -> None:
            self.store = store
            self.speaker_bar = FakeBar()

        def _device_name(self, did: str) -> str:
            return "客厅灯"

    top_calls: list[object] = []
    real_top = mw.tooltip.toast_top
    mw.tooltip.toast_top = lambda *a, **kw: top_calls.append(a)  # type: ignore[assignment]

    def run(ok: int, failed: int, error: str) -> tuple[str, str]:
        win = FakeWin(FakeStore(ok, failed, error))
        mw.MainWindow._on_capability_refresh(win, "d1")
        check(len(win.speaker_bar.notes) == 1, "每次刷新都应弹一条窗口内提示")
        return (win.speaker_bar.notes[0] if win.speaker_bar.notes else "",
                win.speaker_bar.kinds[0] if win.speaker_bar.kinds else "")

    try:
        good, kind = run(30, 0, "")
        check(good == "已刷新 30 项属性", f"全成功提示不对：{good}")
        check(kind == "ok", f"全成功应用普通色，实际 {kind}")
        part, kind = run(26, 4, "")
        check(part == "已刷新 26 项属性", f"有未读到的也只说刷新了多少项：{part}")
        check(kind == "ok", f"读到属性就用普通色，实际 {kind}")
        bad, kind = run(0, 30, "设备离线")
        check(bad == "设备暂时无响应，稍后会自动重试", f"全没读到文案不对：{bad}")
        check(kind == "fail", f"全没读到用警示色，实际 {kind}")
        for text in (good, part, bad):
            for word in ("记录", "能力缓存", "账本", "失败", "未读到"):
                check(word not in text, f"提示里不应出现「{word}」：{text}")
        check(not top_calls, "不应再走 toast_top（屏幕绝对坐标）通道")
    finally:
        mw.tooltip.toast_top = real_top  # type: ignore[assignment]


def test_inline_toast() -> None:
    section("窗口内浮层：对齐极客版提示的视觉参数、3 秒自收、鼠标不影响、只留一条")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QColor
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QWidget

    from mihome.ui import theme
    from mihome.ui.inline_toast import (
        DOT_R, FONT_PX, InlineToast, MS, PAD_X, RADIUS,
    )
    from mihome.ui.alert_bar import ALERT_MARGIN_TOP, ALERT_MARGIN_X

    app = QApplication.instance() or QApplication([])
    win = QWidget()
    win.resize(900, 600)
    # 模拟主窗口几何：左侧栏 176，内容区从 x=176 起；卡片墙时说话条占顶部 60
    content = QWidget(win)
    content.setGeometry(176, 60, 724, 540)
    win.show()
    content.show()
    app.processEvents()

    toast = InlineToast(content, theme.LIGHT)
    toast.show_message("已刷新 12 项属性")
    check(toast.isVisible() and toast.is_showing(), "提示应立刻可见并在计时")
    check(toast.parentWidget() is win, "应是窗口内子控件，不是顶层 tooltip")
    check(not (toast.windowFlags() & Qt.ToolTip), "不应再使用 Qt.ToolTip 顶层窗口")
    check(toast.y() == content.geometry().top() + ALERT_MARGIN_TOP,
          f"应贴在内容区顶部 +{ALERT_MARGIN_TOP}，实际 y={toast.y()}")
    check(abs((toast.x() + toast.width() / 2)
              - (content.geometry().left() + content.width() / 2)) <= 1.5,
          f"应相对内容区水平居中，实际 x={toast.x()} w={toast.width()}")
    check(toast.x() >= content.geometry().left() + ALERT_MARGIN_X,
          f"左侧应保住 {ALERT_MARGIN_X}px 内边距，实际 x={toast.x()}")

    # 窗口 resize 时提示还在，位置应跟着内容区重新居中
    win.resize(600, 600)
    content.setGeometry(120, 60, 480, 540)
    app.processEvents()
    check(abs((toast.x() + toast.width() / 2)
              - (content.geometry().left() + content.width() / 2)) <= 1.5,
          f"resize 后应按新内容区居中，实际 x={toast.x()} w={toast.width()}")
    # 内容区比提示还窄：按内边距夹住，不越出窗口
    content.setGeometry(176, 60, 120, 540)
    app.processEvents()
    check(toast.x() >= content.geometry().left() + ALERT_MARGIN_X,
          f"窄内容区下应保住左内边距，实际 x={toast.x()}")
    check(toast.x() + toast.width() <= win.width(),
          f"窄内容区下也不应越出窗口，实际 x={toast.x()} w={toast.width()}")
    win.resize(900, 600)
    content.setGeometry(176, 60, 724, 540)
    toast.show_message("已刷新 12 项属性")
    app.processEvents()

    # 视觉参数逐项对齐 tooltip.toast_top（极客版登录码那条）
    check(RADIUS == 10 and PAD_X == 18 and FONT_PX == 13 and DOT_R == 4.0,
          "圆角 / 内边距 / 字号 / 圆点半径应与极客版提示一致")
    check(toast.graphicsEffect() is None, "极客版提示没有阴影，这里也不做投影")
    check(not hasattr(toast, "_card"), "不应再保留图标层")
    check(toast.dot_color() == theme.ACCENT_GREEN, "成功用绿点")
    check(toast.bg_color() == theme.blend(theme.LIGHT.card, theme.ACCENT_GREEN, 0.09),
          "底色应参照警告条：状态色按 9% 混进卡面色")
    check(toast.border_color()
          == theme.blend(theme.LIGHT.card, theme.ACCENT_GREEN, 0.38),
          "描边应参照警告条：状态色按 38% 混进卡面色")

    img = toast.grab().toImage()
    cy = toast.height() // 2
    check(img.pixelColor(toast.width() // 2, toast.height() - 5)
          == QColor(toast.bg_color()), "内边距应是染过色的实心底")
    check(img.pixelColor(int(PAD_X + DOT_R + 1), cy) == QColor(theme.ACCENT_GREEN),
          "左侧应画出状态圆点")

    # 失败：红点，文字仍用主文字色（不再整句染色）
    toast.show_message("设备暂时无响应，稍后会自动重试", kind="fail")
    check(toast.dot_color() == theme.DANGER, "失败用红点")
    img2 = toast.grab().toImage()
    check(img2.pixelColor(int(PAD_X + DOT_R + 1), toast.height() // 2)
          == QColor(theme.DANGER), "失败态应画红点")
    check(not hasattr(toast, "color_text"), "文字不再按状态染色")

    # 语义只认两档：未知值按失败，不再用 kind != "fail" 静默当成功
    toast.show_message("未知语义", kind="warning")
    check(toast.dot_color() == theme.DANGER, "未知 kind 应按失败画红点")

    # 鼠标移动 / 悬停 / 离开都不该缩短或收起它
    toast.show_message("已刷新 12 项属性")
    QTest.mouseMove(content, QPoint(10, 10))
    QTest.mouseMove(win, QPoint(400, 300))
    app.processEvents()
    check(toast.isVisible() and toast._timer.isActive(), "鼠标移动不应收起提示")
    check(toast._timer.remainingTime() > MS - 1200, "鼠标移动不应重新计时或缩短时长")

    # 同一时间只有一条，后到的替换先到的并重新计时
    toast.show_message("后到的一条")
    check(len(win.findChildren(InlineToast)) == 1, "同一时间只应存在一个提示浮层")
    check(toast.text() == "后到的一条", "后到的应替换先到的")
    check(toast._timer.remainingTime() > MS - 600, "替换后应重新计时")

    # 点击只收起提示，不穿透到底下的控件
    QTest.mouseClick(toast, Qt.LeftButton)
    app.processEvents()
    check(not toast.isVisible(), "点击提示应只收起它自己")

    # 到点自己消失（没有淡出动画，直接隐藏）
    toast.show_message("倒计时", ms=120)
    QTest.qWait(400)
    check(not toast.isVisible(), "到点应自动隐藏")

    win.close()


def test_existing_ledger() -> None:
    section("本机存量账本（只统计，不写）")
    path = Path.home() / "AppData" / "Local" / "MiHomePanel" / "cache" / "capabilities.json"
    if not path.is_file():
        print("  跳过：本机没有账本")
        return
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = raw.get("entries") or {}
    from mihome.core.api import classify

    keep = sum(1 for v in entries.values() if isinstance(v, dict)
               and classify(int(v.get("code") or 0)) == "unsupported")
    print(f"  当前 {len(entries)} 条，按现行规则自愈后应剩 {keep} 条")


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        test_self_heal(tmp)
        test_fingerprint(tmp)
        test_zero_requests(tmp)
        test_reset_scopes(tmp)
        test_refresh(tmp)
        test_texts()
        test_feedback_channel()
        test_inline_toast()
        test_existing_ledger()

    print(f"\n{CHECKS} 项检查，失败 {len(FAILS)} 项")
    for f in FAILS:
        print("  FAIL:", f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
