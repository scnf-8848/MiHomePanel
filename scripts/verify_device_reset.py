# SPDX-License-Identifier: GPL-3.0-or-later
"""离线验证：能力账本自愈、设备指纹、三个手动重置入口的清除范围与文案。

不联网、不起窗口。跑法：``.venv/Scripts/python.exe scripts/verify_device_reset.py``
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mihome.core.api import CapabilityBook, PropRef  # noqa: E402
from mihome.core.prefs import Prefs  # noqa: E402
from mihome.core.store import Store  # noqa: E402
from mihome.ui.device_reset import (  # noqa: E402
    all_reset_text,
    device_reset_text,
    refresh_result_text,
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
        self._props = props

    def properties(self) -> list[FakeProp]:
        return self._props


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
        callback({PropRef("A", p.siid, p.piid): object() for p in props}, "")

    store.read_props = fake_read  # type: ignore[method-assign]
    store.refresh_capabilities("A", lambda removed, read, err: done.append((removed, read, err)))

    check(book.count_for("A") == 0, "刷新应先清掉 A 的能力缓存")
    check(calls == [("A", 2)], f"应立刻重查全部可读属性（不可读的不查），实际 {calls}")
    check(done == [(1, 2, "")], f"回调应报出清除/重查条数，实际 {done}")


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

    ok = refresh_result_text("客厅灯", 3, 40)
    check("已确认不支持" in ok and "重查" in ok and "40" in ok,
          "强制刷新提示要说清清的是不支持记录、并立刻重查")
    bad = refresh_result_text("客厅灯", 3, 0, "设备离线")
    check("失败" in bad and "设备离线" in bad, "重查失败时提示要带原因")


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
        test_existing_ledger()

    print(f"\n{CHECKS} 项检查，失败 {len(FAILS)} 项")
    for f in FAILS:
        print("  FAIL:", f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
