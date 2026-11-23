# SPDX-License-Identifier: GPL-3.0-or-later
"""验证消息中心：字段口径、家庭过滤、时间文字、请求形状、浮层三态与"只读"。

跑法：``PYTHONPATH=. .venv/Scripts/python.exe scripts/verify_messages.py``

全部离线：假 payload、假 api 对象、假 store；offscreen 起 QApplication。
口径（2026-11-23 用户定）：**只读** + **悬停浮出** + **不显示未读**。
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
           when: int = 0, home: int | None = 1, body: bool = True,
           top_home: int = 0) -> dict:
    """一条云端记录。

    家庭 id 照真机放 ``params.body.home_id``；``top_home`` 是顶层那个字段——实测
    **恒为 0**，所以默认就给 0，专门用来钉住"别拿它当家庭"。
    """
    row = {"msg_id": msg_id, "type": kind, "title": title, "content": content,
           "did": did, "ctime": when, "is_new": 0, "home_id": top_home}
    if home is not None and body:
        row["params"] = {"body": {"home_id": home}}
    else:
        row["params"] = {}
    return row


NOW = int(time.time())
PAYLOAD = {
    "messages": [
        record("11", 6, "洗涤完成", content="洗衣机", did="d1", when=NOW - 3600),
        record("12", 13, "观影模式执行失败", when=NOW - 90),
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
    check("家庭 id 从 params.body.home_id 收成字符串", rows[0].home, "1")
    check("ctime 是秒：3600 秒前 = 1 小时前", M.ago(rows[1].when), "1 小时前")
    # 正文与设备名重复时不再显示第二遍（实测这批消息的 content 常就是设备名）
    check("正文等于设备名时不重复显示", rows[1].body, "")
    check("正文有独立内容时保留", rows[2].body, "滤芯剩余 3%")

    print("\n2) parse：未知类型、脏数据、家庭过滤")
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
    check("payload 不是 dict 时返回空表", M.parse(None, NAMES), [])

    mixed = {"messages": [
        record("31", 6, "本家", when=NOW, home=111000000000001),
        record("32", 6, "别家", when=NOW - 60, home=222000000000002),
        record("33", 13, "没有家庭字段", when=NOW - 120, home=None),
    ]}
    kept = M.parse(mixed, {}, home="111000000000001")
    check("只留当前家庭", [r.msg_id for r in kept], ["31", "33"])
    check("不传 home 就不过滤", len(M.parse(mixed, {})), 3)
    check("home 认字符串与整数两种写法",
          len(M.parse(mixed, {}, home=111000000000001)), 2)
    # 判断不了家庭的照留：宁可多显示一条，也别把自己家的悄悄藏掉
    check_true("缺家庭字段的记录不被误删", any(r.msg_id == "33" for r in kept))

    # 顶层 home_id 恒为 0：拿它当家庭就会"永远过滤不掉"（用户实测两个家都能看到
    # 全部消息，根因就是这个）——真正的 id 只在 params.body.home_id 里
    zero = {"messages": [
        record("41", 6, "本家", when=NOW, home=111000000000001, top_home=0),
        record("42", 6, "别家", when=NOW - 60, home=222000000000002, top_home=0),
    ]}
    check("顶层 0 不参与判断（按 body 过滤）",
          [r.msg_id for r in M.parse(zero, {}, home="111000000000001")], ["41"])
    check_true("顶层 0 不会被当成一个家庭 id",
               all(r.home != "0" for r in M.parse(zero, {})))
    # 万一将来顶层真给了非 0 值，也认（body 缺、顶层有）
    fallback = {"messages": [record("43", 6, "只有顶层", top_home=9, home=None)]}
    check("body 缺时退回顶层", [r.home for r in M.parse(fallback, {})], ["9"])


# -- 2. 文字 -------------------------------------------------------------
def test_text() -> None:
    print("\n3) 摘要 / 相对时间 / 截断")
    rows = M.parse(PAYLOAD, NAMES)
    check("摘要只写条数（不写未读）", M.summarise(rows), "3 条消息")
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


class _Sub:
    """``Store.fetch_messages`` / ``fetch_device_messages`` 用到的那几样。"""

    devices: dict = {}
    last_dev_call: list = []

    class worker:
        @staticmethod
        def submit(fn, args, **kwargs):
            kwargs["callback"](fn(*args), None)

    class client:
        @staticmethod
        def messages(limit: int):
            return PAYLOAD

        @classmethod
        def device_messages(cls, did: str, limit: int):
            _Sub.last_dev_call.append((did, limit))
            return {"messages": [
                record("51", 6, "这台设备的历史", content="", did=did, when=NOW),
            ]}

    @staticmethod
    def message_names():
        return NAMES


def test_store_home() -> None:
    print("\n5) Store.fetch_messages：把当前家庭传进 parse")
    from mihome.core.store import Store  # noqa: PLC0415

    seen: list = []
    sub = _Sub()
    Store.fetch_messages(sub, lambda items, err: seen.append((items, err)), home="1")  # type: ignore[arg-type]
    items, err = seen[0]
    check("成功回调拿到 3 条", (len(items or []), err), (3, None))
    check("设备名照样补上", (items or [])[1].device, "洗衣机")

    seen.clear()
    Store.fetch_messages(sub, lambda items, err: seen.append((items, err)), home="9")  # type: ignore[arg-type]
    check("别家的全被滤掉", len(seen[0][0] or []), 0)

    seen.clear()
    Store.fetch_device_messages(sub, "d1", lambda items, err: seen.append((items, err)))  # type: ignore[arg-type]
    check("单设备取数把 did / limit 传下去", _Sub.last_dev_call, [("d1", 50)])
    check("单设备回调拿到 1 条", (len(seen[0][0] or []), seen[0][1]), (1, None))


# -- 4. 浮层 -------------------------------------------------------------
class FakeStore:
    """``MessagePopup`` 只用到 ``active_home`` 与 ``fetch_messages``。"""

    def __init__(self, items=None, error=None, home: str = "1") -> None:
        self.calls: list[str] = []
        self.items = items if items is not None else []
        self.error = error
        self.active_home = home

    def fetch_messages(self, done, *, limit: int = M.DEFAULT_LIMIT, home: str = "") -> None:
        self.calls.append(home)
        if self.error is not None:
            done(None, self.error)
        else:
            done(list(self.items), None)


def test_popup() -> None:
    print("\n6) 浮层：三态、缓存、家庭、收起")
    from PySide6.QtCore import QEvent, QPoint, Qt  # noqa: PLC0415
    from PySide6.QtWidgets import QApplication, QWidget  # noqa: PLC0415

    from mihome.ui import messages_popup as P  # noqa: PLC0415
    from mihome.ui import theme  # noqa: PLC0415

    app = QApplication.instance() or QApplication(sys.argv)
    rows = M.parse(PAYLOAD, NAMES)

    anchor = QWidget()
    anchor.resize(70, 30)
    anchor.show()

    store = FakeStore(rows)
    pop = P.MessagePopup(store, theme.LIGHT)
    pop.attach(anchor)
    check("挂上胶囊但还没浮出", (pop.isVisible(), store.calls), (False, []))
    check_true("不抢焦点（不激活就能显示）",
               pop.testAttribute(Qt.WA_ShowWithoutActivating))
    check_true("是 Tool 窗口（不进任务栏）", bool(pop.windowFlags() & Qt.Tool))
    check("宽度固定", pop.width(), P.WIDTH)

    pop.show_for()
    app.processEvents()
    check("浮出时读一次，带当前家庭", store.calls, ["1"])
    check("行数", len(pop.list.messages()), 3)
    check("摘要不写未读", pop.list.summary.text(), "3 条消息")
    check_true("只读：没有已读 / 删除按钮",
               not [b for b in pop.findChildren(object)
                    if callable(getattr(b, "text", None))
                    and str(getattr(b, "text")()).strip() in ("全部已读", "标为已读", "删除")])
    check_true("浮层在胶囊下方",
               pop.geometry().top() >= anchor.mapToGlobal(QPoint(0, 0)).y() + anchor.height())
    check_true("列表高度夹在上下限之间",
               P.LIST_MIN_H <= pop.list.scroll.height() <= P.LIST_MAX_H,
               str(pop.list.scroll.height()))
    # 用户 2026-11-23 反馈"只看到两条"：列表必须按条数长高，摘要那行只占一行
    check_true("列表跟着条数长高（不是缩在最小高度）",
               pop.list.scroll.height() > P.LIST_MIN_H,
               f"{pop.list.scroll.height()} > {P.LIST_MIN_H}")
    check("摘要行只占 SUM_H", pop.list.summary.height(), P.SUM_H)
    lay2 = pop.layout()
    want_h = (lay2.contentsMargins().top() + pop.list.height() + lay2.spacing()
              + pop.note.height() + lay2.contentsMargins().bottom())
    check_true("窗口高度刚好装下内容（不多出空白）",
               abs(pop.height() - want_h) <= 2, f"{pop.height()} vs {want_h}")

    print("\n7) 悬停：Enter 浮出、Leave 起计时、缓存命中不再请求")
    pop.hide()
    app.processEvents()
    QApplication.sendEvent(anchor, QEvent(QEvent.Enter))
    app.processEvents()
    check_true("Enter 就浮出（不用点）", pop.isVisible())
    check("还在缓存期内，不再请求", store.calls, ["1"])

    QApplication.sendEvent(anchor, QEvent(QEvent.Leave))
    check_true("Leave 只是起计时，不是立刻收", pop._hide_timer.isActive())  # noqa: SLF001
    QApplication.sendEvent(anchor, QEvent(QEvent.Enter))
    check_true("移回来就取消计时", not pop._hide_timer.isActive())  # noqa: SLF001
    app.processEvents()

    print("\n8) 收起：光标不在里面才真的关")
    pop.show_for()
    app.processEvents()
    inside = pop.mapToGlobal(pop.rect().center())
    far = QPoint(inside.x() + 2000, inside.y() + 2000)

    class _Cursor:
        at = far

        @classmethod
        def pos(cls):
            return cls.at

    real_cursor = P.QCursor
    P.QCursor = _Cursor  # type: ignore[assignment]
    try:
        _Cursor.at = inside
        pop._on_hide_timeout()  # noqa: SLF001
        check_true("光标还在浮层里就不收", pop.isVisible())
        _Cursor.at = far
        pop._on_hide_timeout()  # noqa: SLF001
        check_true("光标走远了才收", not pop.isVisible())
    finally:
        P.QCursor = real_cursor  # type: ignore[assignment]

    print("\n9) 家庭切换 / 失败 / 空态")
    store.active_home = "2"
    pop.show_for()
    app.processEvents()
    check("切家庭会重新读，并带上新家庭", store.calls, ["1", "2"])
    pop.hide()

    empty = P.MessagePopup(FakeStore([]), theme.LIGHT)
    empty.attach(anchor)
    empty.show_for()
    app.processEvents()
    check("空态摘要", empty.list.summary.text(), "没有消息")
    empty.hide()

    failed = P.MessagePopup(FakeStore(error=RuntimeError("网络断了")), theme.LIGHT)
    failed.attach(anchor)
    failed.show_for()
    app.processEvents()
    # "读失败"不能显示成"没有消息"：那会让人去手机里白找一圈
    check("失败态摘要", failed.list.summary.text(), "读取失败")
    check_true("失败态不冒充空态", failed.list.messages() == [])
    failed.hide()

    # 有旧数据时失败：留着旧的，不要擦掉已经看到的东西
    keep = P.MessagePopup(FakeStore(rows), theme.LIGHT)
    keep.attach(anchor)
    keep.show_for()
    app.processEvents()
    keep._store.error = RuntimeError("断了")  # noqa: SLF001
    keep._fetched_at = 0.0  # noqa: SLF001  让缓存过期
    keep.show_for()
    app.processEvents()
    check("刷新失败时保留上一轮结果", len(keep.list.messages()), 3)
    keep.hide()
    anchor.close()
    app.processEvents()


class FakeListApi:
    """``Client.device_messages`` 用：按 type 返回不同的一批。"""

    locale = "zh_CN"

    def __init__(self, rows_by_type: dict[int, list[dict]]) -> None:
        self.rows_by_type = rows_by_type
        self.calls: list[tuple[str, dict]] = []
        self.fail: set[int] = set()

    def _request(self, path: str, data: dict) -> dict:
        self.calls.append((path, data))
        if data.get("type") in self.fail:
            raise RuntimeError("boo")
        return {"messages": self.rows_by_type.get(int(data.get("type")), [])}


def test_device_client() -> None:
    print("\n11) Client.device_messages：type 必填、两种类型合并、一种炸了不算失败")
    api = FakeListApi({
        6: [record("61", 6, "门锁开了", did="d1", when=NOW)],
        11: [record("62", 11, "设备离线", did="d1", when=NOW - 60)],
    })
    client = Client(api=api)  # type: ignore[arg-type]
    out = client.device_messages("d1", 50)
    check("两种类型各问一次", [c[1]["type"] for c in api.calls], [6, 11])
    check("都问同一个 did", {c[1]["did"] for c in api.calls}, {"d1"})
    check("timestamp 只认 0", {c[1]["timestamp"] for c in api.calls}, {0})
    check("只读：force_read 恒 False", {c[1]["force_read"] for c in api.calls}, {False})
    check("路径", {c[0] for c in api.calls}, {"/v2/message/v2/list"})
    check("两批合起来", sorted(r["msg_id"] for r in out["messages"]), ["61", "62"])

    api.fail = {11}
    out2 = client.device_messages("d1")
    check("一种类型炸了，另一种照给", [r["msg_id"] for r in out2["messages"]], ["61"])

    api.fail = {6, 11}
    raised = ""
    try:
        client.device_messages("d1")
    except RuntimeError as exc:
        raised = str(exc)
    # 两种都失败才算失败：界面据此显示"读取失败"，而不是冒充"没有记录"
    check("两种都失败才抛", raised, "boo")


def test_device_dialog() -> None:
    print("\n12) 设备消息记录窗：固定高度、只读、失败与空分开")
    from types import SimpleNamespace  # noqa: PLC0415

    from PySide6.QtWidgets import QApplication  # noqa: PLC0415

    from mihome.ui import device_messages as D  # noqa: PLC0415
    from mihome.ui import theme  # noqa: PLC0415
    from mihome.ui.detail import DetailDialog  # noqa: PLC0415

    app = QApplication.instance() or QApplication(sys.argv)
    rows = M.parse(PAYLOAD, NAMES)

    class Store2:
        active_home = "1"

        def __init__(self, items, error=None) -> None:
            self.items = items
            self.error = error
            self.calls: list[tuple[str, int]] = []

        def fetch_device_messages(self, did, done, *, limit=50) -> None:
            self.calls.append((did, limit))
            done(None, self.error) if self.error else done(list(self.items), None)

    state = SimpleNamespace(did="d1", name="洗衣机")
    st = Store2(rows)
    dlg = D.DeviceMessagesDialog(st, state, theme.LIGHT)
    check("构造时还没请求", st.calls, [])
    dlg.show()
    app.processEvents()
    check("显示后读一次，带 did", st.calls, [("d1", 50)])
    check("列表高度是钉死的", dlg.list.scroll.height(), D.LIST_H)
    check("摘要写条数", dlg.list.summary.text(), "3 条消息")
    check("窗口宽度固定", dlg.width(), D.WIDTH + 2 * D.SHADOW_PAD)
    check_true("只读：没有已读 / 删除按钮",
               not [b for b in dlg.findChildren(object)
                    if callable(getattr(b, "text", None))
                    and str(getattr(b, "text")()).strip() in ("全部已读", "标为已读", "删除")])
    dlg.close()

    bad = D.DeviceMessagesDialog(Store2([], error=RuntimeError("网络断了")), state, theme.LIGHT)
    bad.show()
    app.processEvents()
    check("失败态摘要", bad.list.summary.text(), "读取失败")
    bad.close()

    empty = D.DeviceMessagesDialog(Store2([]), state, theme.LIGHT)
    empty.show()
    app.processEvents()
    check("空态摘要", empty.list.summary.text(), "还没有记录")
    empty.close()

    # 详情页左下角：入口换成「消息记录」，隐藏按钮撤掉
    check_true("详情页有 _open_messages（左下角那颗按钮接的）",
               callable(getattr(DetailDialog, "_open_messages", None)))
    check_true("详情页不再有 _toggle_hidden（隐藏改在卡片右键）",
               not hasattr(DetailDialog, "_toggle_hidden"))


def test_header() -> None:
    print("\n10) 顶栏：消息在右侧、搜索框已撤、点击也能浮出")
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
        check("文字就是「消息」（没有未读数）", btn.text(), "消息")
        check_true("搜索框已经撤掉", not hasattr(win, "search"))
        # 窗口没 show 时 layout 还没跑，geometry 都是 0；从布局顺序判更可靠：
        # 温湿度 → 弹性留白 → 消息 → 主题/刷新（消息占的是原搜索框的位置）
        head = win._head  # noqa: SLF001
        check_true("消息在温湿度之后、圆按钮之前（原搜索框的位置）",
                   head.indexOf(win.climate) < head.indexOf(btn)
                   < head.indexOf(win.btn_theme),
                   f"{head.indexOf(win.climate)} / {head.indexOf(btn)} / "
                   f"{head.indexOf(win.btn_theme)}")
        check_true("浮层认下了这颗胶囊", win.messages._anchor is btn)  # noqa: SLF001
        check("快捷键里没有 Ctrl+F",
              [s.key().toString() for s in win._shortcuts],  # noqa: SLF001
              ["F5", "Esc", "Ctrl+R", "Ctrl+,"])
        # 用信号模拟点击：真点要跑 event loop 且窗口得显示出来
        seen: list[bool] = []
        win.messages.show_for = lambda *_a: seen.append(True)  # type: ignore[method-assign]
        btn.clicked.emit()
        check("点了也会浮出", seen, [True])
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
    test_store_home()
    test_popup()
    test_header()
    test_device_client()
    test_device_dialog()
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
