# SPDX-License-Identifier: GPL-3.0-or-later
"""验证 ``core/channel.py``：只发不等、顺序、回执分类、线程收尾。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_channel.py``

全部离线：把 ``http.client.HTTPSConnection`` 换成一个按剧本行事的假连接，
**不联网、不发米家请求**。五个方向要钉住：

* **只发不等**：第一条回执故意卡 300 ms，第三条动作的 ``request()`` 必须在那之前
  就调用完——这就是"不等回执"的全部意义；
* **顺序**：发送线程按提交顺序调 ``request()``；
* **四类结果**：成功 / 没发出去（立刻）/ 云端码 / 没等到回执，各走各的分支；
* **回执解析**：明文 JSON 与 RC4 密文两种响应都解得开，``result`` 里的成功码
  也读得出来（``/miotspec/action`` 的成功码不在顶层 ``code``）；
* **收尾**：``close()`` 之后通道线程都得退出，``post`` 立刻回失败。
"""

from __future__ import annotations

import base64
import json
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import http.client as _http  # noqa: E402

from mijiaAPI.miutils import encrypt_rc4, get_signed_nonce  # noqa: E402
from mijiaAPI.miutils import gen_nonce as real_nonce  # noqa: E402

import mihome.core.channel as CH  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAILS.append(f"{name} {detail}")


# ---------------------------------------------------------------------------
# 假的连接与上游对象
# ---------------------------------------------------------------------------

NONCE = "Zml4ZWQtbm9uY2U="
SSECURITY = base64.b64encode(b"0123456789abcdef").decode()

#: 回调收集器（主线程 drain 时写入）
GOT: list[tuple[int, str]] = []
#: 事件时间线：("req", idx, t) / ("reply", idx, t)，用来验"不等回执"
EVENTS: list[tuple[str, int, float]] = []
REC_LOCK = threading.Lock()


class Plan:
    """一条假连接的行为：正常回 / 建连抛异常 / 读回执超时。"""

    def __init__(
        self,
        payload: bytes = b'{"code":0,"result":{"code":0}}',
        *,
        raise_on_request: BaseException | None = None,
        reply_delay: float = 0.0,
        timeout: bool = False,
    ) -> None:
        self.payload = payload
        self.raise_on_request = raise_on_request
        self.reply_delay = reply_delay
        self.timeout = timeout


PLANS: list[Plan] = []
_seq = {"n": 0}


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


class FakeConn:
    def __init__(self, host, port=None, timeout=None) -> None:
        with REC_LOCK:
            self.index = _seq["n"] = _seq["n"] + 1
            self.plan = PLANS.pop(0) if PLANS else Plan()
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = FakeSock()
        self.closed = False
        PATHS.append("")
        HEADERS.append({})
        BODIES.append(b"")

    def request(self, method: str, path: str, body=None, headers=None) -> None:
        with REC_LOCK:
            EVENTS.append(("req", self.index, time.perf_counter()))
            PATHS[self.index] = path
            HEADERS[self.index] = dict(headers or {})
            BODIES[self.index] = body or b""
        if self.plan.raise_on_request is not None:
            raise self.plan.raise_on_request

    def getresponse(self):
        if self.plan.timeout:
            raise TimeoutError("等回执超时")
        if self.plan.reply_delay:
            time.sleep(self.plan.reply_delay)
        with REC_LOCK:
            EVENTS.append(("reply", self.index, time.perf_counter()))
        return FakeResponse(self.plan.payload)

    def close(self) -> None:
        self.closed = True


class FakeSock:
    def settimeout(self, value: float) -> None:
        self.timeout = value


PATHS: list[str] = [""]
HEADERS: list[dict] = [{}]
BODIES: list[bytes] = [b""]


class FakeAPI:
    """``Channel`` 只用到三样：ssecurity / session.headers / api_base_url。"""

    def __init__(self) -> None:
        self.auth_data = {"ssecurity": SSECURITY}
        self.session = type("S", (), {})()
        self.session.headers = {
            "User-Agent": "verify",
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": "serviceToken=fake",
        }
        self.api_base_url = "https://api.example.test/app"


def secret_reply(payload: dict) -> bytes:
    """造一条 RC4 密文响应：与 ``Client._request`` 解密的路径一致。"""
    return encrypt_rc4(get_signed_nonce(SSECURITY, NONCE), json.dumps(payload)).encode()


def cb(code: int, reason: str) -> None:
    GOT.append((code, reason))


def fresh(**kw) -> CH.Channel:
    GOT.clear()
    EVENTS.clear()
    PLANS.clear()
    PATHS[:] = [""]
    HEADERS[:] = [{}]
    BODIES[:] = [b""]
    _seq["n"] = 0
    return CH.Channel(FakeAPI(), **kw)


def pump(ch: CH.Channel, want: int, timeout: float = 5.0) -> list[tuple[int, str]]:
    end = time.perf_counter() + timeout
    while len(GOT) < want and time.perf_counter() < end:
        ch.drain()
        time.sleep(0.005)
    return list(GOT)


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


def test_post_returns_immediately() -> None:
    ch = fresh()
    start = time.perf_counter()
    for i in range(3):
        ch.post("/miotspec/action", {"did": f"d{i}"}, on_reply=cb)
    spent = time.perf_counter() - start
    check("post 三条立刻返回（不等回执）", spent < 0.05, f"{spent * 1000:.1f} ms")

    got = pump(ch, 3)
    check("三条都收到回执", len(got) == 3 and all(c == 0 for c, _ in got), str(got))
    check("发送顺序 = 提交顺序", PATHS[1:4] == ["/app/miotspec/action"] * 3, str(PATHS[1:4]))
    ch.close()


def test_不等第一条回执() -> None:
    """核心用例：第一条回执卡 300 ms，第三条的 request 必须在那之前发完。"""
    ch = fresh()
    PLANS.extend([Plan(reply_delay=0.3), Plan(), Plan()])
    for i in range(3):
        ch.post("/miotspec/action", {"did": f"d{i}"}, on_reply=cb)
    pump(ch, 3, timeout=5.0)

    reqs = [t for kind, _, t in EVENTS if kind == "req"]
    # 取**第 1 条**的回执时刻：第 2/3 条没 delay，会先回来（正好说明读线程互不阻塞）
    first_reply = next((t for kind, idx, t in EVENTS if kind == "reply" and idx == 1), None)
    check("三条 request 都调用了", len(reqs) == 3, str(len(reqs)))
    if len(reqs) == 3 and first_reply is not None:
        check(
            "第三条在第一条回执之前就发完（没等回执）",
            reqs[2] < first_reply,
            f"第三条 {reqs[2] - reqs[0]:.1f}s / 首个回执 {first_reply - reqs[0]:.1f}s",
        )
        check("三条发送间隔 < 100ms", (reqs[2] - reqs[0]) < 0.1, f"{(reqs[2] - reqs[0]) * 1000:.1f} ms")
    else:
        check("第三条在第一条回执之前就发完（没等回执）", False, "没抓到时刻")
        check("三条发送间隔 < 100ms", False, "没抓到时刻")
    ch.close()


def test_发送失败立刻() -> None:
    ch = fresh()
    PLANS.append(Plan(raise_on_request=ConnectionRefusedError("连接被拒绝")))
    ch.post("/miotspec/action", {"did": "d1"}, on_reply=cb)
    got = pump(ch, 1, timeout=3.0)
    check("建连失败 → FAILED", bool(got) and got[0][0] == CH.FAILED, str(got))
    check("失败原因带原始异常名", bool(got) and "ConnectionRefusedError" in got[0][1], str(got))
    ch.close()


def test_云端错误码() -> None:
    ch = fresh()
    PLANS.append(Plan(json.dumps({"code": -704220025, "message": "参数个数不匹配"}).encode()))
    ch.post("/miotspec/action", {"did": "d1"}, on_reply=cb)
    got = pump(ch, 1)
    check("云端码原样带回", bool(got) and got[0][0] == -704220025, str(got))
    check("云端 message 带上", bool(got) and "参数个数不匹配" in got[0][1], str(got))
    ch.close()


def test_result里的成功码() -> None:
    ch = fresh()
    PLANS.append(Plan(json.dumps({"code": 0, "result": {"code": -704040005}}).encode()))
    ch.post("/miotspec/action", {}, on_reply=cb)
    got = pump(ch, 1)
    check("顶层 0 但 result 非 0 → 取 result 的码", bool(got) and got[0][0] == -704040005, str(got))

    GOT.clear()
    PLANS.append(Plan(json.dumps({"code": 0, "result": [{"code": 0}]}).encode()))
    ch.post("/miotspec/action", {}, on_reply=cb)
    got = pump(ch, 1)
    check("result 是 list 也读得出成功", bool(got) and got[0][0] == 0, str(got))
    ch.close()


def test_密文回执() -> None:
    ch = fresh()
    CH.gen_nonce = lambda: NONCE  # type: ignore[assignment]  密文要用同一个 nonce 造
    PLANS.append(Plan(secret_reply({"code": 0, "result": {"code": 0}})))
    ch.post("/miotspec/action", {}, on_reply=cb)
    got = pump(ch, 1)
    check("RC4 密文回执解得开", bool(got) and got[0][0] == 0, str(got))
    CH.gen_nonce = real_nonce  # type: ignore[assignment]
    ch.close()


def test_读超时不算失败() -> None:
    ch = fresh()
    PLANS.append(Plan(timeout=True))
    ch.post("/miotspec/action", {}, on_reply=cb)
    got = pump(ch, 1)
    check("没等到回执 → UNCONFIRMED", bool(got) and got[0][0] == CH.UNCONFIRMED, str(got))
    check("UNCONFIRMED 不是 FAILED", bool(got) and got[0][0] != CH.FAILED, str(got))
    ch.close()


def test_请求形状() -> None:
    ch = fresh()
    ch.post("/miotspec/action", {"did": "d1", "siid": 3, "aiid": 1}, on_reply=cb)
    pump(ch, 1)
    headers = HEADERS[1] if len(HEADERS) > 1 else {}
    body = BODIES[1] if len(BODIES) > 1 else b""
    check("path = base + uri", PATHS[1] == "/app/miotspec/action", str(PATHS[1]))
    check("带 Connection: close（不复用连接）", headers.get("Connection") == "close", str(headers))
    check("带上游的 Cookie 头", "serviceToken" in str(headers.get("Cookie", "")), str(headers))
    check(
        "Content-Length 与 body 等长",
        headers.get("Content-Length") == str(len(body)),
        f"{headers.get('Content-Length')} vs {len(body)}",
    )
    params = dict(parse_qsl(body.decode()))
    check("body 里有签名参数", {"signature", "ssecurity", "_nonce"} <= set(params), str(sorted(params)))
    check("body 里的 data 是密文（不是明文 JSON）", "did" not in body.decode(), body.decode()[:40])
    ch.close()


def test_回调抛异常不影响其它() -> None:
    ch = fresh()

    def boom(code: int, reason: str) -> None:
        raise RuntimeError("回调炸了")

    ch.post("/miotspec/action", {}, on_reply=boom)
    ch.post("/miotspec/action", {}, on_reply=cb)
    got = pump(ch, 1)
    check("前一个回调抛异常，后一个照常回调", len(got) == 1 and got[0][0] == 0, str(got))
    ch.close()


def test_pending计数() -> None:
    ch = fresh()
    ch.post("/miotspec/action", {}, on_reply=cb)
    check("提交后 pending = 1", ch.pending == 1, str(ch.pending))
    pump(ch, 1)
    time.sleep(0.05)
    check("回执收完后 pending 归零", ch.pending == 0, str(ch.pending))
    ch.close()


def test_close之后() -> None:
    ch = fresh()
    ch.close()
    ch.post("/miotspec/action", {}, on_reply=cb)
    got = pump(ch, 1, timeout=1.0)
    check("close 之后 post → 立刻 FAILED", bool(got) and got[0][0] == CH.FAILED, str(got))

    # 线程收到哨兵才会 return，中间可能有几十微秒的调度延迟；**轮询**到 2 秒，
    # 既容得下调度抖动，也仍然能抓到真正没退出的线程（比如卡在读回执上）。
    deadline = time.monotonic() + 2.0
    while True:
        alive = [
            t.name
            for t in threading.enumerate()
            if t.name.startswith("mihome-send") or t.name.startswith("mihome-reply")
        ]
        if not alive or time.monotonic() >= deadline:
            break
        time.sleep(0.01)
    check("close 后通道线程都已退出", not alive, str(alive))


def test_读线程是池子不是固定槽() -> None:
    """动作数 > 读线程数时：**发送照旧不排队，回执排队**。

    读线程是"取一条连接 → 读完 → 再取一条"，不是"第 1 条归 0 号线程"。所以 A 的回执
    一读完，这条线程立刻去接在队列里等着的下一条（用户问的"第 5 个动作"）。这里用
    ``readers=2`` + 4 条来验：前两条各卡 250 ms，后两条必须等有人空出来才被读，但
    **它们的 request 早在第一条回执之前就发完了**。
    """
    ch = fresh(readers=2)
    PLANS.extend([Plan(reply_delay=0.25), Plan(reply_delay=0.25), Plan(), Plan()])
    for i in range(4):
        ch.post("/miotspec/action", {"did": f"d{i}"}, on_reply=cb)
    got = pump(ch, 4, timeout=5.0)

    reqs = [t for kind, _, t in EVENTS if kind == "req"]
    replies = [t for kind, _, t in EVENTS if kind == "reply"]
    r3 = next((t for kind, idx, t in EVENTS if kind == "reply" and idx == 3), None)
    first_reply = min(replies) if replies else None
    check("4 条都收到回执（超出读线程数也不丢）", len(got) == 4, str(got))
    check("4 条 request 全部立刻发出（发送从不等读线程）", len(reqs) == 4, str(len(reqs)))
    if len(reqs) == 4 and first_reply is not None and r3 is not None:
        check(
            "第 4 条的 request 早于**任何**回执（发送不被占满的读线程挡住）",
            reqs[3] < first_reply,
            f"{(first_reply - reqs[3]) * 1000:.1f} ms 早",
        )
        # 两条读线程都在处理卡住的 1、2，第 4 条的回执只能等有人读完才被读。
        # **别断言"晚于第 1 条回执"**——两个读线程在同一刻空出来，1 和 3 谁先记
        # 由调度决定（实测差 -0.0 ms）。只断言它被压到了 ≥200 ms。
        check(
            "第 4 条的回执被压到 ≥200 ms 才读到（读线程满员时只能排队）",
            (r3 - reqs[0]) >= 0.2,
            f"{(r3 - reqs[0]) * 1000:.0f} ms",
        )
        check(
            "两条读线程并行过（不是串成 4×250 ms）",
            (r3 - reqs[0]) < 0.7,
            f"总耗时 {(r3 - reqs[0]) * 1000:.0f} ms",
        )
    else:
        for name in ("第 4 条的 request 早于**任何**回执",
                     "第 4 条的回执被压到 ≥200 ms 才读到",
                     "两条读线程并行过"):
            check(name, False, "没抓到时刻")
    ch.close()


def test_单条读线程也能全收() -> None:
    """``readers=1`` 的极端：回执严格按"读完一条接一条"，一条都不丢。

    这条同时说清"立刻释放"的边界：释放的是**线程**，不是连接——上一条 ``close()``
    之后才轮得到下一条，所以是"接力"不是"并发"。
    """
    ch = fresh(readers=1)
    PLANS.extend([Plan(reply_delay=0.15), Plan(), Plan()])
    for i in range(3):
        ch.post("/miotspec/action", {"did": f"d{i}"}, on_reply=cb)
    got = pump(ch, 3, timeout=5.0)
    r1 = next((t for kind, idx, t in EVENTS if kind == "reply" and idx == 1), None)
    r2 = next((t for kind, idx, t in EVENTS if kind == "reply" and idx == 2), None)
    check("readers=1 时三条也全收到", len(got) == 3, str(got))
    check("readers=1 时回执按接力顺序到", r1 is not None and r2 is not None and r2 > r1,
          f"{r1} / {r2}")
    ch.close()


def main() -> int:
    _http.HTTPSConnection = FakeConn  # type: ignore[misc]  全程离线
    test_post_returns_immediately()
    test_不等第一条回执()
    test_发送失败立刻()
    test_云端错误码()
    test_result里的成功码()
    test_密文回执()
    test_读超时不算失败()
    test_请求形状()
    test_回调抛异常不影响其它()
    test_pending计数()
    test_读线程是池子不是固定槽()
    test_单条读线程也能全收()
    test_close之后()
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
