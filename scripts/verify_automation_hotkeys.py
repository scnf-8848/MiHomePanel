# SPDX-License-Identifier: GPL-3.0-or-later
"""离线验证全局热键注册层 + 虚拟事件发送层。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_automation_hotkeys.py``

**不注册任何真实全局热键、不发任何米家请求**：``user32`` 换成假的（记录调用、可模拟
注册失败），动作收发器 ``AutomationService.channel`` 也换成假的（记下 ``post`` 的入参、
回执由用例手动播放）。这样「先到先得」「被占用标红」「停用注销」「一个组合只注册一次」、
「发出去就返回、回执异步回来」都能在 CI 上验，不会抢走用户正在用的组合、也不上网。

顺带验两条核心约束：

* 中枢的 ``(siid, aiid)`` 必须**按规格里的 action 名查**，不是 ``4/1`` 常量——换个
  siid/服务名仍要找对；
* 判据（找不到设备 / 离线 / 没这个功能 / 参数空）全在**发送之前**做完，这类失败是
  **同步**回调 ``done`` 的——引擎据此区分"一个字都没发出去"和"回执报错"。
"""

from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_TMP = Path(tempfile.mkdtemp(prefix="mihome-auto-hk-"))
os.environ["MIHOME_DATA_DIR"] = str(_TMP / "data")

from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from mihome.core.automation import FIRED, Action, LogEntry, Rule, Trigger, action_catalog  # noqa: E402
from mihome.core.automation import hotkeys_win as HK  # noqa: E402
from mihome.core.automation.hotkeys import native_variants  # noqa: E402
from mihome.core.automation import service as SVC  # noqa: E402
from mihome.core.automation.service import AutomationService  # noqa: E402
from mihome.core.channel import FAILED, UNCONFIRMED  # noqa: E402

app = QApplication.instance() or QApplication([])
FAILS: list[str] = []
N = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global N
    N += 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAILS.append(f"{name} {detail}")


class FakeUser32:
    def __init__(self) -> None:
        self.registered: dict[int, tuple[int, int]] = {}
        self.unregistered: list[int] = []
        self.fail_vk: set[int] = set()

    @staticmethod
    def _num(value) -> int:
        # 真 user32 拿的是 int；这里传进来的是 ctypes 实例，要取 .value
        return int(getattr(value, "value", value))

    def RegisterHotKey(self, hwnd, hotkey_id, mods, vk):  # noqa: N802
        vk_i = self._num(vk)
        if vk_i in self.fail_vk:
            return 0
        self.registered[self._num(hotkey_id)] = (self._num(mods), vk_i)
        return 1

    def UnregisterHotKey(self, hwnd, hotkey_id):  # noqa: N802
        key = self._num(hotkey_id)
        self.unregistered.append(key)
        self.registered.pop(key, None)
        return 1


def make_state(*, siid=4, aiid=1, action="emit-virtual-event",
               service="virtual-service", did="107", name="中枢网关", online=True,
               in_piids=(1,)):
    # in_piids 与入参 property 都要给：action_catalog 判"这台设备能不能做这件事"
    # 时看的是入参形状，缺了会被判成不可用
    act = SimpleNamespace(name=action, siid=siid, aiid=aiid, in_piids=tuple(in_piids))
    prop = SimpleNamespace(name="virtual-param", piid=1, siid=siid)
    svc = SimpleNamespace(name=service, siid=siid, actions=[act], properties=[prop])
    spec = SimpleNamespace(services=[svc])
    node = SimpleNamespace(did=did, name=name, model="xiaomi.gateway.hub1")
    return SimpleNamespace(node=node, spec=spec, did=did, name=name, online=online)


def make_speaker(*, siid=3, aiid=5, did="108", name="客厅音箱", online=True):
    """一台能做「执行文本指令」的音箱。``in_piids`` 反序给，验的是**按名字**对槽。"""
    act = SimpleNamespace(name="execute-text-directive", siid=siid, aiid=aiid,
                          in_piids=(2, 1))
    text = SimpleNamespace(name="text-content", piid=1, siid=siid)
    silent = SimpleNamespace(name="silent-execution", piid=2, siid=siid)
    svc = SimpleNamespace(name="intelligent-speaker", siid=siid, actions=[act],
                          properties=[text, silent])
    spec = SimpleNamespace(services=[svc])
    node = SimpleNamespace(did=did, name=name, model="xiaomi.wifispeaker.lx06")
    return SimpleNamespace(node=node, spec=spec, did=did, name=name, online=online)


# ---------------------------------------------------------------------------
print("--- 热键注册：排他 / 去重 / 注销 / 冲突标红 ---", flush=True)
fake = FakeUser32()
HK._user32 = lambda: fake
window = QWidget()
window.resize(200, 100)
reg = HK.HotkeyRegistry(window)
got: list[str] = []
reg.fired.connect(got.append)

reg.sync(["Ctrl+Alt+F7", "F8"])
check("两个组合都注册", len(fake.registered) == 2)
check("状态都是已注册", reg.status("Ctrl+Alt+F7") == "已注册"
      and reg.status("F8") == "已注册")
check("MOD_NOREPEAT 恒带", all(mods & 0x4000
      for mods, _vk in fake.registered.values()))
check("重复 sync 不再注册第二次", (reg.sync(["Ctrl+Alt+F7", "F8"]),
      len(fake.registered) == 2)[1])

# 一个组合只注册一次：两条规则共用同一个 key 由 RuleSet 分发，注册表只有一条
check("同一个组合只占一个 id", len(set(fake.registered)) == 2)

reg.sync(["Ctrl+Alt+F7"])
check("停用会注销", len(fake.registered) == 1 and len(fake.unregistered) == 1)
check("被注销的键位状态清掉", reg.status("F8") == "未注册")

# 模拟「被别的程序占用」
fake.fail_vk.add(0x76)  # F7：模拟已被别的程序先注册
reg.sync([])            # 先清空我们的注册表，等会儿重新注册时才会撞上
reg.sync(["Ctrl+Alt+F7"])
check("注册失败标成被占用", "占用" in reg.status("Ctrl+Alt+F7"),
      reg.status("Ctrl+Alt+F7"))
check("失败时界面提示不猜占用者", "不告诉是谁" in reg.status("Ctrl+Alt+F7"))
check("失败不占 id", len(fake.registered) == 0)
fake.fail_vk.clear()
reg._retry()
check("启动后重试一次能恢复", reg.status("Ctrl+Alt+F7") == "已注册")

# 系统保留键不注册
reg.sync([])
reg.sync(["F12"])
check("保留键不注册并给原因", "不可用" in reg.status("F12")
      and not fake.registered, reg.status("F12"))

# WM_HOTKEY 原生消息过滤
reg.sync([])
reg.sync(["Ctrl+Alt+F7"])
hotkey_id = next(iter(fake.registered))
msg = wintypes.MSG()
msg.message = 0x0312
msg.wParam = hotkey_id
hit = reg._filter.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(msg))
QTest.qWait(10)
check("WM_HOTKEY 被过滤并转成键位信号", hit is True and got == ["Ctrl+Alt+F7"], str(got))

# 同一条消息被 Qt 分发两次（实测 PySide6 6.12：一次按键 → 两次 nativeEventFilter，
# wParam 相同、eventType 都是 windows_generic_MSG）。不去重就是"按一下跑两遍"，
# 第二遍必然撞进 1 s 冷静期，日志里那条「冷静期阻止，剩余 1 秒」就是这么来的。
QTest.qWait(int(HK._DEDUP_SECONDS * 1000) + 30)   # 先越过上一用例的窗口
got.clear()
reg._filter.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(msg))
reg._filter.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(msg))
QTest.qWait(10)
check("同一条 WM_HOTKEY 被分发两次，只触发一次", got == ["Ctrl+Alt+F7"], str(got))

# 去重不能吃掉真的连按：间隔超过窗口就是两次
QTest.qWait(int(HK._DEDUP_SECONDS * 1000) + 30)   # 同样先越过上一个用例的窗口
got.clear()
reg._filter.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(msg))
QTest.qWait(int(HK._DEDUP_SECONDS * 1000) + 40)
reg._filter.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(msg))
QTest.qWait(10)
check("隔开一段再按仍算两次（去重不误伤真连按）",
      got == ["Ctrl+Alt+F7", "Ctrl+Alt+F7"], str(got))

# 去重按 hotkey id 各记各的：另一个热键紧接着来，不能被上一个的窗口吃掉
_fired: list[int] = []
_f = HK._HotkeyFilter(_fired.append)
_a, _b = wintypes.MSG(), wintypes.MSG()
_a.message = _b.message = 0x0312
_a.wParam, _b.wParam = 0x0200, 0x0201
_f.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(_a))
_f.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(_b))
_f.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(_a))   # 重复的 A
QTest.qWait(10)
check("去重按 id 各记各的，不同热键紧挨着都能触发",
      _fired == [0x0200, 0x0201], str(_fired))
other = wintypes.MSG()
other.message = 0x0100  # WM_KEYDOWN
check("其它消息一律放行", reg._filter.nativeEventFilter(
      b"windows_generic_MSG", ctypes.addressof(other)) is False)

reg.release_all()
check("退出注销全部热键", not fake.registered and reg.status("Ctrl+Alt+F7") == "未注册")

# ---------------------------------------------------------------------------
print("\n--- 数字主键：一个 Ctrl+1 注册两份，按哪边都触发 ---", flush=True)
# 2026-11-16 用户报：设了 Ctrl+1，按小键盘 1 没反应。根因是 RegisterHotKey 认虚拟键码，
# 字母区上方 1 是 0x31、小键盘 1 是 0x61，只注册一份另一个就是哑的。
fake.registered.clear()
fake.unregistered.clear()
reg.sync([])                    # 先清掉上一段留下的 Ctrl+Alt+F7
reg.sync(["Ctrl+1"])
_vks = sorted(vk for _mods, vk in fake.registered.values())
check("Ctrl+1 注册了两个 VK（0x31 主键盘 + 0x61 小键盘）", _vks == [0x31, 0x61], str(_vks))
check("两份修饰键完全一样（都是 Ctrl + NOREPEAT）",
      len({mods for mods, _vk in fake.registered.values()}) == 1,
      str(sorted(fake.registered.values())))
check("两个 VK 都映射回同一个规范串（规则只看到一条）",
      set(reg._by_id.values()) == {"Ctrl+1"}, str(reg._by_id))
check("界面状态是已注册", reg.status("Ctrl+1") == "已注册", reg.status("Ctrl+1"))

got.clear()
_numpad_id = next(i for i, (_mods, vk) in fake.registered.items() if vk == 0x61)
_num_msg = wintypes.MSG()
_num_msg.message = 0x0312
_num_msg.wParam = _numpad_id
reg._filter.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(_num_msg))
QTest.qWait(10)
check("按小键盘那份触发的是同一个规范串 Ctrl+1", got == ["Ctrl+1"], str(got))

# 一份被占用：仍然算「已注册」（另一份按下去照样触发），但要说清是哪一份缺
reg.sync([])
fake.fail_vk.add(0x61)
reg.sync(["Ctrl+1"])
check("小键盘被占用时状态仍含「已注册」并点明是哪一份",
      reg.status("Ctrl+1") == "已注册（小键盘被占用）", reg.status("Ctrl+1"))
check("这时只注册上主键盘那一份",
      [vk for _mods, vk in fake.registered.values()] == [0x31], str(fake.registered))
fake.fail_vk.clear()
reg._retry()
check("启动重试把缺的那份补齐（且旧 id 先退掉）",
      sorted(vk for _mods, vk in fake.registered.values()) == [0x31, 0x61],
      str(fake.registered))
reg.sync([])
check("清空后两份都注销", not fake.registered, str(fake.registered))

# 别名写法进来也要落到同一个串：否则同一物理按键会被再注册一遍、占四个 id
reg.sync(["Ctrl+Num1"])
check("sync 收到别名写法时不会重复注册",
      list(reg._ids) == ["Ctrl+1"] and len(fake.registered) == 2, str(reg._ids))
reg.sync([])

_stray = wintypes.MSG()
_stray.message = 0x0312
_stray.wParam = 0x7777          # 没分配过的 id
got.clear()
check("未知 hotkey id 不崩", reg._filter.nativeEventFilter(
      b"windows_generic_MSG", ctypes.addressof(_stray)) is True)
QTest.qWait(10)
check("未知 id 不会产出键位", got == [], str(got))
reg.release_all()

# ---------------------------------------------------------------------------
print("\n--- 试注册探测：键能不能存只看系统答不答应 ---", flush=True)
# 编辑页要回答"这个键存不存得进去"，而这件事跟规则启停无关，只跟"有没有被别人占"
# 有关。_status 缓存答不上来（新录的键从没注册过），只能真的试一次 RegisterHotKey。
fake_p = FakeUser32()
HK._user32 = lambda: fake_p
prov = HK.HotkeyRegistry(window)
check("空键不探", prov.probe("") == "")
check("系统保留键直接给原因，不去碰系统",
      prov.probe("Ctrl+Alt+Delete").startswith("系统保留"),
      prov.probe("Ctrl+Alt+Delete"))
free_vk = native_variants("Ctrl+Alt+F12")[0][1]
fake_p.fail_vk.add(free_vk)
check("被别的程序占着：探出原因（而不是一句「未注册」）",
      prov.probe("Ctrl+Alt+F12") == "被其它程序占用，换个键",
      prov.probe("Ctrl+Alt+F12"))
check("探测不写注册状态（缓存那套不被污染）",
      prov.status("Ctrl+Alt+F12") == "未注册", prov.status("Ctrl+Alt+F12"))
fake_p.fail_vk.discard(free_vk)
before = dict(fake_p.registered)
check("没人占的组合探出空串", prov.probe("Ctrl+Alt+F12") == "",
      prov.probe("Ctrl+Alt+F12"))
check("探完立刻退掉，不留注册项", fake_p.registered == before,
      str(fake_p.registered))
check("探测不消耗分配 id", prov._next_id == HK._ID_FIRST, str(prov._next_id))
# 数字主键有两份，任意一份能注册就算可用
num_vk = native_variants("Ctrl+1")[0][1]
fake_p.fail_vk.add(num_vk)   # 主键盘那份被占，小键盘还空着
check("数字主键：主键盘被占但小键盘能注册 → 仍算可用",
      prov.probe("Ctrl+1") == "", prov.probe("Ctrl+1"))
fake_p.fail_vk.clear()
prov.release_all()

# ---------------------------------------------------------------------------
print("\n--- 一条规则挂两个键，其中一个被占：另一个照常工作 ---", flush=True)
# 注册是**按组合**做的，不是按规则：F7 成功、F8 失败，只有 F8 哑掉，规则本身
# 照样能被 F7 唤起来（用户 11-17 问的"剩余触发还能不能触发"）。
fake_m = FakeUser32()
HK._user32 = lambda: fake_m
window_m = QWidget()
reg_m = HK.HotkeyRegistry(window_m)
fake_m.fail_vk.add(native_variants("F8")[0][1])
reg_m.sync(["F7", "F8"])
check("F7 注册上了", "F7" in reg_m._ids, str(reg_m._ids))
check("F8 没注册上，状态说被占",
      reg_m.status("F8").startswith("被其它程序占用"), reg_m.status("F8"))
check("一个失败不拖累另一个（不是全有或全无）", len(reg_m._ids) == 1,
      str(reg_m._ids))
got_m: list[str] = []
reg_m.fired.connect(got_m.append)
reg_m._dispatch(reg_m._ids["F7"][0])
check("按下 F7 照样派发出去", got_m == ["F7"], str(got_m))
# 占用方退出后，启动时排的那一次重试能把它补上（_retried 闸门只放一次）
fake_m.fail_vk.clear()
reg_m.retry_once(0)
QTest.qWait(30)
check("失败的那个键能在重试里补上", "F8" in reg_m._ids, str(reg_m._ids))
reg_m.release_all()

# ---------------------------------------------------------------------------
print("\n--- 编辑页暂停：热键临时退掉，状态说清是「暂停」不是「未注册」 ---", flush=True)
# 编辑一条启用中的规则时，它的热键必须先退掉：全局热键在系统层先于对话框拿到按键，
# 不退掉的话在键帽上重录同一个组合根本录不到（2026-11-17 用户实测）。
fake_e = FakeUser32()
HK._user32 = lambda: fake_e
window_e = QWidget()
reg_e = HK.HotkeyRegistry(window_e)

reg_e.sync([], paused=["Ctrl+Alt+F7"])
check("暂停的组合不注册", not fake_e.registered, str(fake_e.registered))
check("状态说清是编辑中暂停", reg_e.status("Ctrl+Alt+F7") == "编辑中已暂停，关闭后恢复",
      reg_e.status("Ctrl+Alt+F7"))
reg_e.sync(["Ctrl+Alt+F7"])
check("恢复后真的注册上了", len(fake_e.registered) == 1, str(fake_e.registered))
check("恢复后状态回到已注册", reg_e.status("Ctrl+Alt+F7") == "已注册")

# 同一个组合还被别的启用规则用着时，注册必须留着——那本来就该触发别的规则
reg_e.sync(["Ctrl+Alt+F7"], paused=["Ctrl+Alt+F7"])
check("同一组合有别的规则在用，仍注册且不误标成暂停",
      len(fake_e.registered) == 1 and reg_e.status("Ctrl+Alt+F7") == "已注册",
      f"{fake_e.registered} {reg_e.status('Ctrl+Alt+F7')}")

# 别名写法也得认得出是同一个组合，否则暂停的键会显示成「未注册」
reg_e.sync([], paused=["Ctrl+Num1"])
check("暂停判断同样先规范化（Num1 与 1 是同一个组合）",
      reg_e.status("Ctrl+1") == "编辑中已暂停，关闭后恢复", reg_e.status("Ctrl+1"))
reg_e.sync([])
check("暂停清掉后回到未注册", reg_e.status("Ctrl+1") == "未注册", reg_e.status("Ctrl+1"))
reg_e.release_all()

print("\n--- 设备动作：按名字查坐标，发出去就不等回执 ---", flush=True)
hub = make_state(siid=9, aiid=2)  # 故意不是 4/1
check("规格里认得出中枢的「发送虚拟事件」",
      action_catalog.action_for(hub.spec,
                               action_catalog.feature("emit-virtual-event")) is not None)


class FakeUpstream:
    """上游 ``mijiaAPI`` 的形状。真通道会被换掉，这几样只在构造时读得到就行。"""

    def __init__(self) -> None:
        self.auth_data = {"ssecurity": "0" * 32}
        self.session = SimpleNamespace(headers={})
        self.api_base_url = "https://api.io.mi.com/app"


class FakeClient:
    """只实现 service 用到的 ``raw()``；``last_error`` 由用例自己设。"""

    def __init__(self) -> None:
        self.last_error = ""

    def raw(self):
        return FakeUpstream()


class FakeChannel:
    """假的收发器：记下 ``post`` 的入参，回执由用例手动 ``reply`` 播放。

    ``post`` 立刻返回、回执之后才来——正是真通道的契约。所以「发送不等回执」在这里
    是能验的：``_run`` 返回时回调一次都还没被调过。
    """

    def __init__(self) -> None:
        self.posts: list[dict] = []
        self.closed = False

    def post(self, uri, data, *, label="", on_reply=None):
        self.posts.append({"uri": uri, "data": data, "label": label, "cb": on_reply})

    def drain(self, limit: int = 64) -> int:
        return 0

    def close(self) -> None:
        self.closed = True

    def reply(self, code: int, reason: str = "") -> None:
        """模拟云端回执：叫起最近一条的 ``on_reply``。"""
        cb = self.posts[-1]["cb"]
        if cb is not None:
            cb(code, reason)


confirmed: list[int] = []


def store_with(*states) -> SimpleNamespace:
    """设备表 + ``schedule_confirm``（真 ``Store`` 上的那个全量回读入口）。"""
    return SimpleNamespace(devices={s.did: s for s in states},
                           schedule_confirm=lambda: confirmed.append(1))


service = AutomationService(store=store_with(hub), client=FakeClient(), window=None,
                            path=_TMP / "svc-rules.json", transport="channel")
service.channel.close()      # 真通道不联网，先把它的线程停掉
channel = FakeChannel()
service.channel = channel

act = Action("device_action", did="107", device="中枢网关",
             action="emit-virtual-event", args={"virtual-param": "测试事件"})
done: list[str] = []
service._run(act, done.append)
check("请求交给收发器就返回，不等回执", len(channel.posts) == 1 and done == [], str(done))
sent = channel.posts[0]
check("走 /miotspec/action", sent["uri"] == "/miotspec/action", sent["uri"])
# 外壳只许有 params 一个键。少了这层包装云端回 *invalid request*——11-14 真机踩过，
# 唤醒和虚拟事件**同时**报错，因为错的不是某个动作的参数而是这层外壳。
check("请求体包在 params 里（这是 /miotspec/* 的约定）",
      list(sent["data"]) == ["params"], str(sent["data"]))
body = sent["data"]["params"]
check("坐标是按 action 名查出来的 9/2，不是写死的 4/1",
      (body["did"], body["siid"], body["aiid"]) == ("107", 9, 2), str(body))
check("单一入参按名字拼成 [事件名]", body["in"] == ["测试事件"])
check("标签用功能名（日志里看得懂）", sent["label"] == "发送虚拟事件", sent["label"])

channel.reply(0)
check("回执成功时 done 不调（引擎早走下一步了）", done == [], str(done))
check("虚拟事件不做全量回读", confirmed == [])

channel.posts.clear()
service._run(act, done.append)
channel.reply(-704220025)
check("非 0 码翻成人话且保留原始码", done and "-704220025" in done[0], str(done))

done.clear()
service._run(act, done.append)
channel.reply(UNCONFIRMED, "等云端回执超时（设备没有回应）")
check("发出去了没等到回执不算失败", done == [], str(done))

done.clear()
service._run(act, done.append)
channel.reply(FAILED, "ConnectionRefusedError: 拒绝连接")
check("没发出去要立刻报", done and "拒绝连接" in done[0], str(done))

# -- 判据在发送之前：同步失败、一条请求都不发 ---------------------------------
channel.posts.clear()
done.clear()
service._run(Action("device_action", did="nope", device="客厅中枢",
                    action="emit-virtual-event", args={"virtual-param": "x"}), done.append)
check("找不到设备给中文原因", done and "找不到设备" in done[0], str(done))
check("找不到设备时不发请求", not channel.posts)

done.clear()
service._run(Action("device_action", did="107", device="中枢网关",
                    action="no-such-feature"), done.append)
check("不在白名单的动作直接拒", done and "未知功能" in done[0], str(done))
check("未知功能时不发请求", not channel.posts)

done.clear()
service.store = store_with(make_state(online=False))
service._run(act, done.append)
check("离线设备不发请求", done and "离线" in done[0] and not channel.posts, str(done))

done.clear()
service.store = store_with(make_state(action="other-action"))
service._run(act, done.append)
check("型号没有该功能时明说", done and "没有" in done[0] and not channel.posts, str(done))

done.clear()
service.store = store_with(hub)
service._run(Action("device_action", did="107", device="中枢网关",
                    action="emit-virtual-event", args={"virtual-param": "   "}),
             done.append)
check("空事件名不发请求", done and "不能为空" in done[0] and not channel.posts, str(done))

# -- 文本指令：两个槽按名字对（in_piids 反序给的），回执成功才回读 -------------
service.store = store_with(make_speaker())
done.clear()
channel.posts.clear()
service._run(Action("device_action", did="108", device="客厅音箱",
                    action="execute-text-directive",
                    args={"text-content": "打开客厅灯", "silent-execution": False}),
             done.append)
check("文本指令同样不等回执", len(channel.posts) == 1 and done == [], str(done))
tell = channel.posts[0]["data"]["params"]
check("两个槽按 in_piids 的实际顺序排（(2,1) → 静默在前）",
      (tell["siid"], tell["aiid"], tell["in"]) == (3, 5, [False, "打开客厅灯"]),
      str(tell))
check("回执没来之前不回读", confirmed == [])
channel.reply(0)
check("文本指令回执成功后才安排全量回读", confirmed == [1], str(confirmed))

# -- 无参动作：不发 in 键（空列表也不行） -------------------------------------
service.store = store_with(make_state(action="wake-up", service="intelligent-speaker",
                                     in_piids=()))
done.clear()
confirmed.clear()
channel.posts.clear()
service._run(Action("device_action", did="107", device="中枢网关",
                    action="wake-up"), done.append)
check("无参动作也能发出去", len(channel.posts) == 1 and done == [], str(done))
wake = channel.posts[0]["data"]["params"]
check("无参动作不带 in 键（连空列表都不发）", "in" not in wake, str(wake))
channel.reply(0)
check("唤醒不做全量回读", confirmed == [])

fired: list[int] = []
service._call_later(1, lambda: fired.append(1))
check("延迟定时器挂在成员字典里", len(service._timers) == 1)
QTest.qWait(30)
check("延迟回调执行且引用被释放", fired == [1] and not service._timers)
service.close()
check("close 清空队列与注册表", not service.engine.running)
check("close 也停收发器", channel.closed)

# -- 另一条通道：serial（原名机制，扔进共享的 SerialWorker） --------------------
# 11-14 21:4x 用户要 A/B 对比"延迟到底是唤醒本身慢还是串行排队"，默认切成 serial。
# 这一段把 serial 那条路的接线钉住：入队 / 优先级 / 标签 / 结果翻译 / 没有 worker。
print("\n--- serial 通道：进共享 worker，发完才轮到下一条 ---", flush=True)

from mihome.core.automation.service import DEFAULT_TRANSPORT, _pick_transport  # noqa: E402
from mihome.core.jobs import PRIORITY_CONTROL  # noqa: E402

check("默认走原名机制（serial）", DEFAULT_TRANSPORT == "serial", DEFAULT_TRANSPORT)
check("显式参数优先", _pick_transport("channel") == "channel")
check("未知值退回默认，不炸", _pick_transport("bogus") == DEFAULT_TRANSPORT)
os.environ["MIHOME_AUTOMATION_TRANSPORT"] = "channel"
check("环境变量能覆盖（不用改代码就能对比两次）", _pick_transport() == "channel")
del os.environ["MIHOME_AUTOMATION_TRANSPORT"]


class FakeWorker:
    """只实现 ``submit``：记下要跑什么，由用例决定何时「跑完」。"""

    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def submit(self, fn, args=(), *, kwargs=None, callback=None, priority=0, label=""):
        self.jobs.append({"fn": fn, "args": args, "cb": callback,
                          "priority": priority, "label": label})


class SerialClient:
    """``Client.run_action`` 的替身：``code`` 可改，异常按 ``boom`` 抛。"""

    def __init__(self, code: int = 0, boom: BaseException | None = None) -> None:
        self.code = code
        self.boom = boom
        self.calls: list[tuple] = []
        #: ``run_scene`` 的返回：False = 云端拒绝（原因进 ``last_error``）
        self.scene_ok = True
        self.scene_calls: list[tuple] = []
        self.last_error = ""

    def raw(self):
        return FakeUpstream()

    def run_action(self, ref, value=None):  # noqa: N802
        self.calls.append((ref, value))
        if self.boom is not None:
            self.last_error = f"动作异常: {self.boom}"
            return -1
        return self.code

    def run_scene(self, scene_id, home_id):  # noqa: N802
        """真 ``Client.run_scene``：成功 True、失败 False（**不是**错误码）。"""
        self.scene_calls.append((scene_id, home_id))
        if self.boom is not None:
            self.last_error = f"场景异常: {self.boom}"
            return False
        return self.scene_ok


serial_client = SerialClient()
worker = FakeWorker()
serial = AutomationService(store=store_with(hub), client=serial_client, worker=worker,
                           window=None, path=_TMP / "svc-serial.json", transport="serial")
serial.channel.close()   # 不走它，但别让它留着线程
check("serial 模式不启动 drain 定时器", not serial._drain_timer.isActive())

done = []
serial._run(act, done.append)
check("serial 把请求丢进共享 worker（一条）", len(worker.jobs) == 1, str(len(worker.jobs)))
job = worker.jobs[0]
check("用 CONTROL 优先级插队", job["priority"] == PRIORITY_CONTROL, str(job["priority"]))
check("标签用功能名（worker 日志里看得懂）", job["label"] == "发送虚拟事件", job["label"])
check("提交完不等结果", done == [], str(done))
check("serial 不碰收发器", not hasattr(serial.channel, "posts"))

# 假的 worker 不会自己跑；这里扮演 worker 线程那一侧：先执行，再把结果交给回调。
code = job["fn"]()
ref, value = serial_client.calls[0]
check("坐标仍是按 action 名查出来的 9/2", ref == ("107", 9, 2), str(ref))
check("入参按名字拼成 [事件名]", value == ["测试事件"], str(value))
job["cb"](code, None)
check("成功时 done 空串", done == [""], str(done))
check("虚拟事件不做全量回读", confirmed == [])

# 无参动作：value 传 None，让 Client.run_action 自己不发 in
worker.jobs.clear()
done.clear()
serial.store = store_with(make_state(action="wake-up", service="intelligent-speaker",
                                     in_piids=()))
serial._run(Action("device_action", did="107", device="中枢网关", action="wake-up"),
            done.append)
worker.jobs[-1]["fn"]()
check("无参动作的 value 是 None（不发 in 键）",
      serial_client.calls[-1][1] is None, str(serial_client.calls[-1]))
worker.jobs[-1]["cb"](0, None)

# 文本指令成功 → 全量回读；失败 → done 带人话
serial.store = store_with(make_speaker())
worker.jobs.clear()
done.clear()
confirmed.clear()
serial._run(Action("device_action", did="108", device="客厅音箱",
                   action="execute-text-directive",
                   args={"text-content": "打开客厅灯", "silent-execution": False}),
            done.append)
worker.jobs[-1]["cb"](0, None)
check("文本指令成功后才安排全量回读", confirmed == [1], str(confirmed))

done.clear()
worker.jobs.clear()
serial.store = store_with(hub)      # 回到中枢，后面几条才找得到设备
serial.client = SerialClient(code=-704220025)
serial._run(act, done.append)
worker.jobs[-1]["cb"](-704220025, None)
check("非 0 码翻成人话且保留原始码", done and "-704220025" in done[0], str(done))

done.clear()
worker.jobs.clear()
serial.client = SerialClient(boom=RuntimeError("模拟后台异常"))
serial._run(act, done.append)
worker.jobs[-1]["cb"](None, RuntimeError("模拟后台异常"))
check("后台异常带原因给用户", done and "模拟后台异常" in done[0], str(done))

# -1 才是"Client 自己出错"，别把云端负数码（-704220025）也当成它——那样会拿上次残留的
# last_error 去解释一个设备侧的错误，用户看到的原因是错的。
done.clear()
worker.jobs.clear()
stale = SerialClient()
stale.last_error = "上次残留的原因"
serial.client = stale
serial._run(act, done.append)
worker.jobs[-1]["cb"](-1, None)
check("code -1 用 Client.last_error 解释", done and "上次残留的原因" in done[0], str(done))
done.clear()
worker.jobs.clear()
serial._run(act, done.append)
worker.jobs[-1]["cb"](-704220025, None)
check("云端的负数码仍走 explain，不抢 last_error",
      done and "-704220025" in done[0] and "上次残留" not in done[0], str(done))

# 快捷场景（2026-11-25）：同一条串行队列，但调的是 ``Client.run_scene``。
# 它不是设备动作（没有 did / aiid），判据也不能照抄 run_action 那套——后者失败返回 -1，
# 前者失败返回 False，写成 ``code < 0`` 会把 False 当成功放过去。
print("\n--- 快捷场景：进 worker、调 run_scene、成功后全量回读 ---", flush=True)
done.clear()
worker.jobs.clear()
confirmed.clear()
serial.client = SerialClient()
serial._run(Action("scene", scene_id="s9", scene="示例-回家", home="1001"), done.append)
check("场景也丢进共享 worker（一条）", len(worker.jobs) == 1, str(len(worker.jobs)))
check("场景任务标签是「场景」", worker.jobs[-1]["label"] == "场景",
      worker.jobs[-1]["label"])
check("场景用 CONTROL 优先级", worker.jobs[-1]["priority"] == PRIORITY_CONTROL,
      str(worker.jobs[-1]["priority"]))
check("提交完不等结果", done == [], str(done))
ret = worker.jobs[-1]["fn"]()
check("调的是 run_scene，家庭用动作里存的那个",
      serial.client.scene_calls == [("s9", "1001")], str(serial.client.scene_calls))
check("返回值是 True/False，不是错误码", ret is True, repr(ret))
worker.jobs[-1]["cb"](ret, None)
check("场景成功后安排全量回读（改了哪几台说不准）", confirmed == [1], str(confirmed))
check("场景成功 done 是空串", done == [""], str(done))

done.clear()
worker.jobs.clear()
rejecting = SerialClient()
rejecting.scene_ok = False
rejecting.last_error = "场景已不存在"
serial.client = rejecting
serial._run(Action("scene", scene_id="s9"), done.append)
worker.jobs[-1]["cb"](False, None)
check("run_scene 返回 False 当失败（别当成功放过去）",
      done and "场景已不存在" in done[0], str(done))

done.clear()
worker.jobs.clear()
serial._run(Action("scene"), done.append)
check("没选场景时同步报错、一条都不入队",
      done == ["没有选快捷场景"] and not worker.jobs, f"{done} / {len(worker.jobs)}")

done.clear()
worker.jobs.clear()
serial.client = SerialClient()
serial.worker = None
serial._run(act, done.append)
check("没注入 worker 时明确报，不静默", done and "没有配置执行通道" in done[0], str(done))
check("没 worker 就不入队", not worker.jobs)
done.clear()
serial._run(Action("scene", scene_id="s9"), done.append)
check("场景同样要 worker（不静默跳过）",
      done and "没有配置执行通道" in done[0], str(done))
serial.close()

print("\n--- 日志落盘：服务重启后还能翻回上次那一串 ---", flush=True)
log_file = _TMP / "svc-log" / "automation_log.jsonl"


def new_service(path: Path) -> AutomationService:
    svc = AutomationService(store=store_with(hub), client=SerialClient(), worker=FakeWorker(),
                            window=None, path=path, log_path=log_file, transport="serial")
    svc.channel.close()      # 用例不走收发器，别留 5 条空转线程
    return svc


svc_a = new_service(_TMP / "svc-log-a.json")
check("文件还没有时启动，日志是空的", svc_a.engine.log == [] and not log_file.exists())
check("服务把日志路径暴露给界面", svc_a.log_path == log_file)
first = LogEntry(time.time(), "r1", "进门播报", "Ctrl+Alt+F7", FIRED, "热键触发",
                 run="r1@1763000000000-1")
svc_a._on_log(first)
check("每条日志当场追进文件", log_file.exists() and "热键触发" in
      log_file.read_text(encoding="utf-8"))
svc_a.close()

svc_b = new_service(_TMP / "svc-log-b.json")
check("重启后把上次的日志读回内存",
      len(svc_b.engine.log) == 1 and svc_b.engine.log[0].detail == "热键触发",
      str(svc_b.engine.log))
check("读回的批次号没变（折叠分组靠它）",
      svc_b.engine.log[0].run == "r1@1763000000000-1", svc_b.engine.log[0].run)

svc_b.clear_log()
check("清空日志时内存和文件一起清",
      svc_b.engine.log == [] and not log_file.exists())
svc_b.close()
svc_c = new_service(_TMP / "svc-log-c.json")
check("清空后再重启不会翻回来", svc_c.engine.log == [], str(svc_c.engine.log))
svc_c._on_log(LogEntry(time.time(), "r1", "进门播报", "Ctrl+Alt+F7", FIRED, "清空后新的一条",
                       run="r1@1763000009000-1"))
check("清空后照常继续记新日志",
      log_file.exists() and "清空后新的一条" in log_file.read_text(encoding="utf-8"),
      log_file.read_text(encoding="utf-8") if log_file.exists() else "没写文件")
svc_c.close()

# ---------------------------------------------------------------------------
print("\n--- 编辑页暂停：服务层把编辑中的规则从注册表和分发里一起摘出去 ---", flush=True)
# 只退热键、不动 enabled；关掉编辑页（保存 / 取消 / 关窗）复原。
fake_s = FakeUser32()
HK._user32 = lambda: fake_s
svc_window = QWidget()
svc_e = AutomationService(store=store_with(hub), client=SerialClient(), worker=FakeWorker(),
                          window=svc_window, path=_TMP / "svc-edit.json",
                          log_path=_TMP / "svc-edit-log.jsonl", transport="serial")
svc_e.channel.close()        # 用例不走收发器，别留空转线程


def add_rule(rid: str, keys: str) -> None:
    svc_e.rules.add(Rule(id=rid, name=rid, triggers=[Trigger("keyboard", keys)],
                         home="h1", actions=[], enabled=True))


add_rule("e1", "Ctrl+Alt+F7")
check("启用规则注册了热键", len(fake_s.registered) == 1, str(fake_s.registered))

svc_e.suspend_hotkeys("e1")
check("进编辑页：热键退掉", not fake_s.registered, str(fake_s.registered))
check("进编辑页：状态说清是暂停",
      svc_e.status_for("Ctrl+Alt+F7") == "编辑中已暂停，关闭后恢复",
      svc_e.status_for("Ctrl+Alt+F7"))
check("进编辑页：记着是哪一条", svc_e.suspended_rules == {"e1"}, str(svc_e.suspended_rules))
before = len(svc_e.engine.log)
svc_e._on_hotkey("Ctrl+Alt+F7")
check("暂停期间按键不执行这条规则", len(svc_e.engine.log) == before,
      str(svc_e.engine.log[before:]))

svc_e.resume_hotkeys("e1")
check("离开编辑页：热键注册回来", len(fake_s.registered) == 1, str(fake_s.registered))
check("离开编辑页：状态回到已注册", svc_e.status_for("Ctrl+Alt+F7") == "已注册",
      svc_e.status_for("Ctrl+Alt+F7"))
check("离开编辑页：暂停记录清空", svc_e.suspended_rules == set(), str(svc_e.suspended_rules))
before = len(svc_e.engine.log)
svc_e._on_hotkey("Ctrl+Alt+F7")
check("恢复后按键照常执行", len(svc_e.engine.log) > before,
      str(svc_e.engine.log[before:]))
check("重复恢复不炸（幂等）", svc_e.resume_hotkeys("e1") is None)

# 同一个组合还被另一条启用规则用着：注册不能退（那本来就该触发另一条），
# 但分发要挡掉编辑中的那条——否则编辑页开着、按一下两个规则一起响。
add_rule("e2", "Ctrl+Alt+F7")
svc_e.suspend_hotkeys("e1")
check("同组合还有启用规则时注册保留",
      len(fake_s.registered) == 1 and svc_e.status_for("Ctrl+Alt+F7") == "已注册",
      f"{fake_s.registered} {svc_e.status_for('Ctrl+Alt+F7')}")
before = len(svc_e.engine.log)
svc_e._on_hotkey("Ctrl+Alt+F7")
fresh = svc_e.engine.log[before:]
check("按键只执行没在编辑的那条",
      bool(fresh) and all(e.rule_id == "e2" for e in fresh),
      str([e.rule_id for e in fresh]))
svc_e.resume_hotkeys("e1")
check("两条都恢复后注册表仍是那一份（不重复占 id）",
      len(fake_s.registered) == 1, str(fake_s.registered))

# 两条规则用同一个快捷键、都启用（用户 11-17 问"调试时建多条同键规则会怎样"）：
# 系统层按组合去重只注册一份，按一次**两条都执行**，各记各的日志与批次号。
has_ids = set(svc_e.registry._ids)
add_rule("s1", "F9")
add_rule("s2", "F9")
check("两条规则用同一个键：注册表里只多出一个组合",
      set(svc_e.registry._ids) - has_ids == {"F9"}, str(svc_e.registry._ids))
before = len(svc_e.engine.log)
svc_e._on_hotkey("F9")
fresh9 = svc_e.engine.log[before:]
# 注：界面**存不进**两条同键的规则了（键全局唯一，停用的也算占着，见
# rules.key_owner）。这里直接塞进 RuleSet 是在测"旧数据 / 手改 rules.json"的
# 兜底行为：引擎不再重复校验，仍然是两条都跑、各一个批次号。
check("旧数据里两条同键：按一次都执行（不是只跑第一条）",
      sorted(e.rule_id for e in fresh9 if e.outcome == FIRED) == ["s1", "s2"],
      str([e.rule_id for e in fresh9]))
check("两条各占一个批次号（日志窗是两行，不合并成一次）",
      len({e.run for e in fresh9 if e.outcome == FIRED}) == 2,
      str([e.run for e in fresh9]))
svc_e.close()

# ---------------------------------------------------------------------------
print("\n--- 「本程序启动时」：只触发一次，且不占任何热键 ---", flush=True)
start_path = _TMP / "svc-start.json"
# 前面几段共用同一个日志文件，这里先清掉：要数的是"这一段产生了哪几条 FIRED"
log_file.unlink(missing_ok=True)
svc_s = new_service(start_path)
svc_s.rules.add(Rule(
    id="st1", name="开机播报", triggers=[Trigger("app_start", "")], home="1001",
    cooldown_ms=0,   # 下面要连续触发几次，别让冷静期把结论搅混
    actions=[Action("device_action", did="hub-x", device="客厅中枢",
                    action="emit-virtual-event", args={"virtual-param": "A"})]))
check("启动规则不占键位、不注册热键", svc_s.rules.enabled_keys() == [],
      str(svc_s.rules.enabled_keys()))
_orig_delay = SVC.START_TRIGGER_DELAY_MS
SVC.START_TRIGGER_DELAY_MS = 0
try:
    svc_s.fire_start_rules()
    QTest.qWait(30)
    first = [e for e in svc_s.engine.log if e.outcome == FIRED]
    check("真触发了，日志写「程序启动触发」",
          len(first) == 1 and "程序启动触发" in first[0].detail,
          str([e.detail for e in svc_s.engine.log]))
    check("日志的快捷键列留空", first and first[0].keys == "",
          repr(first[0].keys) if first else "")
    check("同一个进程再调 fire_start_rules 不会重复触发",
          (svc_s.fire_start_rules(), QTest.qWait(30),
           len([e for e in svc_s.engine.log if e.outcome == FIRED]))[2] == 1)
finally:
    SVC.START_TRIGGER_DELAY_MS = _orig_delay
svc_s.rules.set_enabled("st1", False)
check("停用后按类型也找不到它",
      svc_s.rules.rules_for_trigger("app_start") == [],
      str(svc_s.rules.rules_for_trigger("app_start")))
# 编辑页开着时不该被启动事件叫起来（_editing 的语义就是"这条暂时别动"）
svc_s.rules.set_enabled("st1", True)
svc_s.suspend_hotkeys("st1")
n_fired = len([e for e in svc_s.engine.log if e.outcome == FIRED])
svc_s._on_app_start()
check("编辑中的规则不被启动事件叫起来",
      len([e for e in svc_s.engine.log if e.outcome == FIRED]) == n_fired,
      str([e.detail for e in svc_s.engine.log[-2:]]))
svc_s.resume_hotkeys("st1")
svc_s._on_app_start()
check("关掉编辑页后再来一次就正常",
      len([e for e in svc_s.engine.log if e.outcome == FIRED]) == n_fired + 1,
      str([e.detail for e in svc_s.engine.log[-2:]]))
svc_s.close()

print(f"\n合计 {N} 项，失败 {len(FAILS)} 项")
for line in FAILS:
    print("  !! " + line)
raise SystemExit(1 if FAILS else 0)
