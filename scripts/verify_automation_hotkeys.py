# SPDX-License-Identifier: GPL-3.0-or-later
"""离线验证全局热键注册层 + 虚拟事件发送层。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_automation_hotkeys.py``

**不注册任何真实全局热键、不发任何米家请求**：``user32`` 换成假的（记录调用、可模拟
注册失败），``api.run_action`` 也换成假的。这样「先到先得」「被占用标红」「停用注销」
「一个组合只注册一次」都能在 CI 上验，不会抢走用户正在用的组合。

顺带验一条核心约束：中枢的 ``(siid, aiid)`` 必须**按规格里的 action 名查**，
不是 ``4/1`` 常量——换个 siid/服务名仍要找对。
"""

from __future__ import annotations

import ctypes
import os
import sys
import tempfile
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

from mihome.core.automation import hotkeys_win as HK  # noqa: E402
from mihome.core.automation.sender import VirtualEventSender, target_for  # noqa: E402
from mihome.core.automation.service import AutomationService  # noqa: E402

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
               service="virtual-service", did="107", name="中枢网关", online=True):
    act = SimpleNamespace(name=action, siid=siid, aiid=aiid)
    svc = SimpleNamespace(name=service, actions=[act], properties=[])
    spec = SimpleNamespace(services=[svc])
    node = SimpleNamespace(did=did, name=name, model="xiaomi.gateway.hub1")
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
other = wintypes.MSG()
other.message = 0x0100  # WM_KEYDOWN
check("其它消息一律放行", reg._filter.nativeEventFilter(
      b"windows_generic_MSG", ctypes.addressof(other)) is False)

reg.release_all()
check("退出注销全部热键", not fake.registered and reg.status("Ctrl+Alt+F7") == "未注册")

print("\n--- 虚拟事件：动态查 iid / 入参键 / 结果判定 ---", flush=True)
hub = make_state(siid=9, aiid=2)  # 故意不是 4/1
target = target_for(hub)
check("按 action 名找到坐标（不写死 4/1）", target is not None
      and (target.siid, target.aiid) == (9, 2))
check("服务改名仍能找到", target_for(make_state(service="renamed-hub")) is not None)
check("没有该 action 的设备不算中枢", target_for(make_state(action="other-action")) is None)
check("没有规格的设备不算中枢", target_for(SimpleNamespace(
      node=SimpleNamespace(did="x", name="x", model="x"), spec=None,
      did="x", name="x", online=True)) is None)


class FakeApi:
    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.payload: dict = {}

    def run_action(self, payload):  # noqa: N802
        self.payload = payload
        return {"code": self.code, "message": "成功" if self.code == 0 else "设备不支持"}


api = FakeApi()
sender = VirtualEventSender(lambda: api, lambda: [hub])
chosen, why = sender.resolve()
check("解析出在线中枢", chosen is not None and chosen.did == "107", why)
ok, text = sender.call(chosen, "极客版按键自动化执行")
check("code 0 = 成功且提示末端仍要确认", ok and "末端动作" in text)
check("参数键是 in 不是 value", api.payload.get("in") == ["极客版按键自动化执行"]
      and "value" not in api.payload, str(api.payload))
check("带上动态查到的 siid/aiid", api.payload.get("siid") == 9
      and api.payload.get("aiid") == 2)
bad = FakeApi(code=-704220025)
sender2 = VirtualEventSender(lambda: bad, lambda: [hub])
ok2, text2 = sender2.call(chosen, "X")
check("非 0 码翻成人话且保留原始码", not ok2 and "-704220025" in text2, text2)
check("空事件名不发请求", not sender.call(chosen, "  ")[0])
offline = VirtualEventSender(lambda: api, lambda: [make_state(online=False)])
picked, why2 = offline.resolve()
check("离线中枢不被使用", picked is None and "离线" in why2, why2)
empty = VirtualEventSender(lambda: api, lambda: [])
picked2, why3 = empty.resolve()
check("没中枢给中文原因", picked2 is None and "没找到" in why3, why3)

print("\n--- 常驻服务：后台投递 / 成员定时器 ---", flush=True)


class FakeWorker:
    def __init__(self) -> None:
        self.jobs: list[tuple] = []

    def submit(self, fn, args=(), *, callback=None, priority=0, label=""):
        self.jobs.append((fn, args, callback, priority, label))


class FakeClient:
    def __init__(self, api) -> None:
        self._api = api

    def raw(self):
        return self._api


store = SimpleNamespace(devices={"107": hub})
worker = FakeWorker()
service = AutomationService(store=store, client=FakeClient(api), worker=worker,
                            window=None, path=_TMP / "svc-rules.json")
done: list[str] = []
service._emit("测试事件", done.append)
check("云端请求丢进后台队列", len(worker.jobs) == 1)
fn, args, callback, priority, label = worker.jobs[0]
check("用 CONTROL 优先级插队", priority == 0 and label == "发虚拟事件")
callback(fn(*args), None)
check("结果回主线程继续动作队列", done == [""], str(done))

worker.jobs.clear()
done.clear()
service._emit("测试事件", done.append)
fn, args, callback, _p, _l = worker.jobs[0]
callback(None, RuntimeError("模拟后台异常"))
check("后台异常不会卡住队列", done and "失败" in done[0], str(done))

fired: list[int] = []
service._call_later(1, lambda: fired.append(1))
check("延迟定时器挂在成员字典里", len(service._timers) == 1)
QTest.qWait(30)
check("延迟回调执行且引用被释放", fired == [1] and not service._timers)
service.close()
check("close 清空队列与注册表", not service.engine.running)

print(f"\n合计 {N} 项，失败 {len(FAILS)} 项")
for line in FAILS:
    print("  !! " + line)
raise SystemExit(1 if FAILS else 0)
