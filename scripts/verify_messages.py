# SPDX-License-Identifier: GPL-3.0-or-later
"""验证消息中心：字段口径、时间文字、请求形状、弹窗三态与"只读"。

跑法：``PYTHONPATH=. .venv/Scripts/python.exe scripts/verify_messages.py``

全部离线：假 payload、假 api 对象、假 store；offscreen 起 QApplication。
口径（2026-11-23 用户定）：**只读** —— 看得到、能关窗，没有标已读、没有删除。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_TMP = Path(tempfile.mkdtemp(prefix="mihome-messages-"))
os.environ["MIHOME_DATA_DIR"] = str(_TMP / "data")

from mihome.core import messages as M  # noqa: E402
from mihome.core.api import Client  # noqa: E402

FAILS: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {got!r}")
    if not ok:
        FAILS.append(f"{name} → 期望 {want!r}，实际 {got!r}")


def check_true(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(f"{name}{('  ' + detail) if detail else ''}")


# -- 夹具 ----------------------------------------------------------------
NAMES = {"d1": "洗衣机", "d2": "压力锅", "d3": "洗碗机"}


def record(msg_id: str, kind: int, title: str, *, content: str = "", did: str = "",
           when: int = 0, new: int = 0) -> dict:
    return {"msg_id": msg_id, "type": kind, "title": title, "content": content,
            "did": did, "ctime": when, "is_new": new}


NOW = int(time.time())
PAYLOAD = {
    "messages": [
        record("11", 6, "洗涤完成", content="洗衣机", did="d1", when=NOW - 3600, new=1),
        record("12", 13, "观影模式执行失败", when=NOW - 90, new=1),
        record("13", 6, "滤芯即将到期", content="滤芯剩余 3%", did="d3", when=NOW - 86400),
    ]
}


# -- 1. 字段口径 ---------------------------------------------------------
def test_parse() -> None:
    print("\n1) parse：字段、排序、脏数据")
    rows = M.parse(PAYLOAD, NAMES)
    check("条数", len(rows), 3)
    check("按时间倒序（新的在前）", [r.msg_id for r in rows], ["12", "11", "13"])
    check("类型 6 有中文标签", rows[1].label, "设备消息")
    check("类型 13 有中文标签", rows[0].label, "场景通知")
    check("did 换成设备名", rows[1].device, "洗衣机")
    check("没有 did 的消息设备名为空", rows[0].device, "")
    check("is_new=1 算未读", [r.unread for r in rows], [True, True, False])
    check("ctime 是秒：3600 秒前 = 1 小时前", M.ago(rows[1].when), "1 小时前")
    # 正文与设备名重复时不再显示第二遍（实测这批消息的 content 常就是设备名）
    check("正文等于设备名时不重复显示", rows[1].body, "")
    check("正文有独立内容时保留", rows[2].body, "滤芯剩余 3%")

    print("\n2) parse：未知类型与脏数据")
    unknown = M.parse({"messages": [record("21", 99, "谁也不知道这是什么")]}, {})
    check("未知类型保留数字", unknown[0].kind, 99)
    check("未知类型给中性标签、不瞎猜", unknown[0].label, "消息")
    dirty = M.parse({"messages": [
        record("", 6, "没有 id"),
        "不是 dict",
        None,
        {"msg_id": "22", "title": None, "ctime": "abc", "is_new": None},
    ]}, {})
    check("没有 msg_id 的丢掉", len(dirty), 1)
    check("标题为 None 变空串", dirty[0].title, "")
    check("时间转不动当 0（不炸）", dirty[0].when, 0)
    check("is_new 为 None 当未读 False", dirty[0].unread, False)
    check("payload 不是 dict 时返回空表", M.parse(None, NAMES), [])


# -- 2. 文字 -------------------------------------------------------------
def test_text() -> None:
    print("\n3) 摘要 / 相对时间 / 截断")
    rows = M.parse(PAYLOAD, NAMES)
    check("摘要写条数与未读", M.summarise(rows), "3 条消息 · 2 条未读")
    check(
        "全读过就不写后半句",
        M.summarise(M.parse({"messages": [record("31", 6, "完成", when=NOW)]}, {})),
        "1 条消息",
    )
    check("空表", M.summarise([]), "没有消息")

    check("89 秒内写刚刚", M.ago(1000, now=1080), "刚刚")
    check("2 分钟", M.ago(1000, now=1120), "2 分钟前")
    check("3 小时", M.ago(1000, now=1000 + 3 * 3600 + 60), "3 小时前")
    check("2 天", M.ago(1000, now=1000 + 2 * 86400 + 60), "2 天前")
    check("没有时间写空串（不写 1970）", M.ago(0), "")
    check("未来时间不写负数", M.ago(2000, now=1000), "刚刚")

    check("短正文原样", M.clip("滤芯剩余 3%"), "滤芯剩余 3%")
    long_text = "嗯" * 200
    out = M.clip(long_text, 20)
    check("长正文截到上限并带省略号", (len(out), out.endswith("…")), (20, True))
    check("换行被压成空格（别把一行撑成五行）", M.clip("a\nb"), "a b")


# -- 3. 请求形状 ---------------------------------------------------------
class FakeApi:
    """只实现 :meth:`Client.messages` 用到的那一处。"""

    locale = "zh_CN"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def _request(self, path: str, data: dict) -> dict:
        self.calls.append((path, data))
        return PAYLOAD


def test_client() -> None:
    print("\n4) Client：请求路径与 body")
    api = FakeApi()
    client = Client(api=api)  # type: ignore[arg-type]
    out = client.messages(30)
    check("只发一个请求", len(api.calls), 1)
    path, data = api.calls[0]
    check("路径", path, "/v2/message/v2/typelist")
    check("body 带 limit", data.get("limit"), 30)
    check_true("timestamp 是毫秒（13 位，不是秒）",
               len(str(data.get("timestamp"))) == 13, str(data.get("timestamp")))
    check("返回原始 payload（归一化不在这层）", out, PAYLOAD)

    api2 = FakeApi()

    def boom(_path: str, _data: dict):
        raise RuntimeError("网络断了")

    api2._request = boom  # type: ignore[method-assign]
    client2 = Client(api=api2)  # type: ignore[arg-type]
    raised = ""
    try:
        client2.messages()
    except RuntimeError as exc:
        raised = str(exc)
    # 失败必须抛出去："读失败"和"没有消息"在界面上长得一样，用户会去 App 里白找
    check("失败抛异常，不吞成空 dict", raised, "网络断了")


# -- 4. 弹窗 -------------------------------------------------------------
class FakeStore:
    """``MessageDialog`` 只用到 ``fetch_messages`` 一个方法。"""

    def __init__(self, items=None, error=None) -> None:
        self.calls = 0
        self.items = items if items is not None else []
        self.error = error

    def fetch_messages(self, done, *, limit: int = M.DEFAULT_LIMIT) -> None:
        self.calls += 1
        if self.error is not None:
            done(None, self.error)
        else:
            done(list(self.items), None)


def test_dialog() -> None:
    print("\n5) 弹窗：三态与只读")
    from PySide6.QtWidgets import QApplication  # noqa: PLC0415

    from mihome.ui import theme  # noqa: PLC0415
    from mihome.ui.messages_dialog import MessageDialog  # noqa: PLC0415

    app = QApplication.instance() or QApplication(sys.argv)
    rows = M.parse(PAYLOAD, NAMES)

    store = FakeStore(rows)
    dlg = MessageDialog(store, theme.LIGHT)
    check("打开前不读（构造不发请求）", store.calls, 0)
    dlg.show()
    app.processEvents()
    check("打开时读一次", store.calls, 1)
    check("行数", len(dlg.list.messages()), 3)
    check("标题栏摘要", dlg.list.summary.text(), "3 条消息 · 2 条未读")
    check_true("只读：窗上没有已读/删除按钮",
               not [b for b in dlg.findChildren(object) if hasattr(b, "text")
                    and callable(getattr(b, "text", None))
                    and str(getattr(b, "text")()).strip() in ("全部已读", "标为已读", "删除")])
    dlg.close()
    app.processEvents()

    empty = MessageDialog(FakeStore([]), theme.LIGHT)
    empty.show()
    app.processEvents()
    check("空态摘要", empty.list.summary.text(), "没有消息")
    empty.close()
    app.processEvents()

    failed = MessageDialog(FakeStore(error=RuntimeError("网络断了")), theme.LIGHT)
    failed.show()
    app.processEvents()
    # "读失败"不能显示成"没有消息"：那会让人去手机里白找一圈
    check("失败态摘要", failed.list.summary.text(), "读取失败")
    check_true("失败态不是空态文案", failed.list.messages() == [])
    failed.close()
    app.processEvents()

    # 关掉以后回调才到：不能去碰已经销毁的控件
    late = MessageDialog(FakeStore(rows), theme.LIGHT)
    late.show()
    app.processEvents()
    late._on_finished(0)  # noqa: SLF001
    late._on_loaded(rows, None)  # noqa: SLF001  _alive 已置 False，应当直接返回
    check_true("关窗后迟到的回调被丢弃", late._alive is False)  # noqa: SLF001
    late.close()
    app.processEvents()


def test_header() -> None:
    print("\n6) 顶栏入口：可点胶囊 + 接线")
    from PySide6.QtCore import Qt  # noqa: PLC0415
    from PySide6.QtWidgets import QApplication  # noqa: PLC0415

    from mihome.ui import theme  # noqa: PLC0415
    from mihome.ui.main_window import MainWindow  # noqa: PLC0415

    app = QApplication.instance() or QApplication(sys.argv)
    win = MainWindow(theme.LIGHT, tray_available=False)
    win._save_settings = lambda: None  # noqa: SLF001
    win.tray = FakeTray()  # type: ignore[assignment]
    try:
        btn = win.btn_messages
        check("带铃铛图标、不画绿点", (btn._icon, btn._dot), ("bell", False))  # noqa: SLF001
        check("文字就是「消息」（没有未读数：那要一路常驻请求）", btn.text(), "消息")
        check_true("可点（手型光标）", btn.cursor().shape() == Qt.PointingHandCursor)
        # 用信号模拟点击：真点要跑 event loop 且窗口得显示出来
        seen: list[object] = []
        win._run_dialog = lambda dlg: seen.append(dlg)  # noqa: SLF001
        btn.clicked.emit()
        check("点了会开消息窗", [type(d).__name__ for d in seen], ["MessageDialog"])
        check("开着的那颗不是只读胶囊类（说明确实换成了可点版本）",
              type(btn).__name__, "ChipButton")
    finally:
        win.close()
        app.processEvents()


class FakeTray:
    active = False

    def set_status(self, _text: str) -> None:
        pass

    def show_message(self, *_a, **_k) -> bool:
        return True

    def remove(self) -> None:
        pass


def main() -> int:
    test_parse()
    test_text()
    test_client()
    test_dialog()
    test_header()
    print()
    if FAILS:
        print(f"共 {len(FAILS)} 项失败：")
        for line in FAILS:
            print("  -", line)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
