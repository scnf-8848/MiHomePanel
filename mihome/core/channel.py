# SPDX-License-Identifier: GPL-3.0-or-later
"""云端请求收发器：**发送不等回执，回执异步回来**。

**状态：实验性、默认不启用**（11-14 22:0x 定案，见下）。``service.DEFAULT_TRANSPORT``
现在是 ``"serial"``（原名单线程机制）；要把自动化动作切到本模块，改那一个词或设
``MIHOME_AUTOMATION_TRANSPORT=channel``。

为什么要单独一层
----------------
自动化的连续动作之间本来没有依赖关系（米家里也是顺序执行、不是条件链），但从前
每条动作都要等 ``POST /miotspec/action`` 的完整往返才走下一步。

**但这层不是"唤醒延迟"的解药——11-14 实测推翻了这个假设**：用户报的
"唤醒 → 发虚拟事件之间几秒空档"，真因是 ``wake-up`` 让音箱进**聆听态**，之后到音箱的
指令要等聆听结束才执行，那是**设备/云端侧的顺序**，换哪条通道都救不了。他在 serial
下复测效果满意，所以默认不启用本模块。

本模块真正解决的是另一件事：``jobs.SerialWorker`` **只有一条线程**，第一条的 HTTP
不返回，第二条连发都发不出去——**"发"这一步被堵**。只有真出现这种程序侧排队卡顿
（例如一串里多个动作都不回执）才值得切过来。

怎么做到的
----------
``http.client`` 的 ``conn.request()`` 内部是 ``putrequest → putheader →
endheaders → send``，**只写 socket，不读响应**，写完就返回。于是：

* **发送线程只有一条**：按提交顺序逐条 ``connect + request()``，几毫秒一条，
  顺序严格（比开一堆线程更符合"按顺序执行"）。
* **读回执 N 条线程**（默认 4）：谁先回谁先读，唤醒卡 3 秒不会拖住后面动作的回执。
  它们是**池子不是固定槽**——读完一条 ``getresponse()`` 就 ``close()`` 并回去从
  ``_read_q`` 取下一条，所以"第 1 条归 0 号线程"这种绑定不存在。动作数 > N 时：
  **N 条并发读，其余的排在 ``_read_q`` 里，谁读完谁接**。注意这不影响"发"——
  发送线程照旧一条不落地全发出去，只是第 N+1 条的回执要等有人空出来才被读。
  4 是"同时有几条回执在等"的上限，不是"一次最多能发几个动作"的上限。
* **等回执的超时从"有人开始读"算起**（``settimeout`` 作用于阻塞调用），不是从发出算起。
  所以排队等读的回执总等待可能超过 ``REPLY_TIMEOUT``；我们关心的是"有没有回音"，
  不是毫秒级时刻，这样反而更稳（不会因为排在后面就被误判成没回）。
* 每条请求**独占一条连接**（不等响应就不能复用，否则响应会串）。
* 结果统一进队列，由主线程 ``drain()`` 执行回调——读线程**绝不碰 UI**
  （跨线程 emit Qt 信号在参考项目里直接崩过，见 ``core/jobs.py``）。

四类结果
--------
=====  ==================================  ============================
码     含义                                什么时候知道
=====  ==================================  ============================
``0``  发出去了，云端也回了成功             收到回执时
``-2`` **没发出去**（连不上 / 写不出去）    **立刻**（connect 阶段）
``-3`` 发出去了，但没等到回执（读超时）      超时之后，**不算失败**
其它   云端的错误码（原样带回，附一句人话）   收到回执时
=====  ==================================  ============================

``-2`` 和 ``-3`` 必须分开：``-2`` 是请求根本没到云端（该告警），``-3`` 是到了
但设备没回执（命令已经在路上了，只是我们没等到话）。混成同一个"失败"会让用户
收到一堆假警报。

只做"动作"这一条车道
--------------------
程序里有两条云端通道，分工按**调用方要不要拿返回值**切，不是按新旧切
（`docs/dev/automation.md`「两条云端通道：使用范围与差异」有完整对照表）：

* **A 单线程同步**（``Client`` + ``jobs.SerialWorker`` + ``requests``）：轮询读、
  写属性、场景、顶栏文本指令条——它们要拿返回值才能继续（比对目标值、报错给界面）。
* **B 收发分离**（本模块）：**只有**电脑自动化的设备动作。它不需要返回值，只需要
  "发出去"和"回执报错时提醒我"，而且要几毫秒一条地连着发。

别往 B 上搬：同步语义的那批调用改成异步，等于把回调套回调；轮询读一次批量 130 条，
搬过来反而每 3 秒多一次 TLS 握手。

``post(uri, data)`` 的 ``data`` 是**完整请求体**，本模块不加壳也不认 uri。``/miotspec/*``
的形状是 ``{"params": <单条|列表>}``（上游 ``apis.py`` 每个入口都是
``self._request(uri, {"params": param})``）。11-14 踩过一次：漏了这层壳，唤醒和发
虚拟事件**全都**回 ``-6 invalid request``——错在外壳，所以跟具体动作、先后顺序都无关。
"""

from __future__ import annotations

import http.client
import json
import queue
import threading
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode, urlsplit

from mijiaAPI.miutils import (
    decrypt,
    gen_nonce,
    generate_enc_params,
    get_signed_nonce,
)

__all__ = ["Channel", "FAILED", "SENT", "UNCONFIRMED"]

#: 发出去了，云端也回了 ``code 0``
SENT = 0
#: **没发出去**（建连 / 写 socket 失败）。立刻就能知道，要马上告诉用户。
FAILED = -2
#: 发出去了但没等到回执。命令已经在云端了，**不当失败**，只记为"未确认"。
UNCONFIRMED = -3

#: 建连超时。国内到米家云端通常 30~80 ms，给 6 秒是留给掉包重传的。
CONNECT_TIMEOUT = 6.0
#: 等回执的超时。唤醒这类动作云端要等设备回应，3~5 秒是常态；超过这个还没回，
#: 说明设备压根不回执，继续等也没意义。
REPLY_TIMEOUT = 20.0

Reply = Callable[[int, str], None]


class _Job:
    """一条待发请求。``on_reply`` 在主线程执行，可能为 ``None``（发完不管）。"""

    __slots__ = ("uri", "data", "label", "on_reply")

    def __init__(self, uri: str, data: dict, label: str, on_reply: Reply | None) -> None:
        self.uri = uri
        self.data = data
        self.label = label
        self.on_reply = on_reply


class Channel:
    """云端请求的收发口。``post()`` 立刻返回，回执由 ``drain()`` 在主线程回调。

    ``api`` 是上游 ``mijiaAPI`` 实例（``Client.raw()``）：只要它的 ``auth_data``
    里的 ``ssecurity``、``session.headers``（Cookie 是 ``_init_session`` 时拼好的
    固定串，不需要 cookie jar）和 ``api_base_url``。签名 / 加解密都复用
    ``mijiaAPI.miutils`` 里的同一套函数，本模块不碰协议细节。
    """

    def __init__(
        self,
        api: Any,
        *,
        readers: int = 4,
        connect_timeout: float = CONNECT_TIMEOUT,
        reply_timeout: float = REPLY_TIMEOUT,
    ) -> None:
        self._api = api
        self._readers = max(1, int(readers))
        self._connect_timeout = connect_timeout
        self._reply_timeout = reply_timeout

        self._send_q: queue.Queue[_Job | None] = queue.Queue()
        self._read_q: queue.Queue[tuple[_Job, http.client.HTTPSConnection, str, str] | None] = (
            queue.Queue()
        )
        #: 读线程和发送线程都往这里放，主线程 ``drain()`` 取。元素是 (job, code, reason)
        self._replies: queue.Queue[tuple[_Job, int, str]] = queue.Queue()
        self._live = 0
        self._live_lock = threading.Lock()
        self._closed = False

        self._sender = threading.Thread(target=self._send_loop, name="mihome-send", daemon=True)
        self._sender.start()
        self._read_pool = [
            threading.Thread(target=self._read_loop, name=f"mihome-reply{i}", daemon=True)
            for i in range(self._readers)
        ]
        for th in self._read_pool:
            th.start()

    # -- 对外 ------------------------------------------------------------
    @property
    def pending(self) -> int:
        """还在路上（含已发未回）的条数，仅供界面 / 日志展示。"""
        with self._live_lock:
            return self._live

    def post(
        self,
        uri: str,
        data: dict,
        *,
        label: str = "",
        on_reply: Reply | None = None,
    ) -> None:
        """提交一条请求，**立刻返回**。

        ``on_reply(code, reason)`` 在主线程执行（由 ``drain()`` 触发）：
        ``0`` 成功 / ``FAILED`` 没发出去 / ``UNCONFIRMED`` 没等到回执 / 其它是云端码。
        ``on_reply=None`` 时连回执都不解析——纯通知型请求用它。
        """
        if self._closed:
            if on_reply is not None:
                on_reply(FAILED, "程序正在退出")
            return
        with self._live_lock:
            self._live += 1
        self._send_q.put(_Job(uri, data, label, on_reply))

    def drain(self, limit: int = 64) -> int:
        """**主线程**调用：执行已经到齐的回调。返回处理的条数。"""
        n = 0
        while n < limit:
            try:
                job, code, reason = self._replies.get_nowait()
            except queue.Empty:
                break
            n += 1
            with self._live_lock:
                self._live = max(0, self._live - 1)
            if job.on_reply is None:
                continue
            try:
                job.on_reply(code, reason)
            except Exception:  # noqa: BLE001  回调出错不能拖垮 drain
                pass
        return n

    def close(self) -> None:
        """停线程。程序退出时调；之后 ``post`` 一律立刻回 ``FAILED``。"""
        self._closed = True
        self._send_q.put(None)
        for _ in range(self._readers):
            self._read_q.put(None)

    # -- 发送线程 --------------------------------------------------------
    def _send_loop(self) -> None:
        while True:
            job = self._send_q.get()
            if job is None:
                return
            self._send_one(job)

    def _send_one(self, job: _Job) -> None:
        """建连 + 写请求，**不读响应**。写完之后把连接交给读线程。"""
        try:
            api = self._api
            ssecurity = api.auth_data["ssecurity"]
            nonce = gen_nonce()
            signed = get_signed_nonce(ssecurity, nonce)
            params = {"data": json.dumps(job.data, separators=(",", ":"))}
            params = generate_enc_params(job.uri, "POST", signed, nonce, params, ssecurity)
            body = urlencode(params).encode()

            parts = urlsplit(api.api_base_url)
            conn = http.client.HTTPSConnection(
                parts.hostname, parts.port, timeout=self._connect_timeout
            )
            path = parts.path.rstrip("/") + job.uri
            headers = dict(api.session.headers)
            headers["Content-Length"] = str(len(body))
            headers["Connection"] = "close"
            conn.request("POST", path, body=body, headers=headers)
        except Exception as exc:  # noqa: BLE001  建连 / 写 socket 失败 = 没发出去
            self._replies.put((job, FAILED, _why(exc)))
            return

        # 请求已经完整写进 socket。剩下的（云端可能正在等设备）交给读线程。
        try:
            conn.sock.settimeout(self._reply_timeout)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001  sock 可能已被关掉，交给 getresponse 报
            pass
        self._read_q.put((job, conn, nonce, ssecurity))

    # -- 读线程 ----------------------------------------------------------
    def _read_loop(self) -> None:
        while True:
            item = self._read_q.get()
            if item is None:
                return
            job, conn, nonce, ssecurity = item
            try:
                code, reason = self._read_one(conn, nonce, ssecurity)
            except Exception as exc:  # noqa: BLE001  读 / 解析失败都算"没等到回执"
                code, reason = UNCONFIRMED, _why(exc)
            finally:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            self._replies.put((job, code, reason))

    def _read_one(
        self, conn: http.client.HTTPSConnection, nonce: str, ssecurity: str
    ) -> tuple[int, str]:
        resp = conn.getresponse()
        raw = resp.read()
        text = raw.decode("utf-8", "replace")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            # 云端对 ``miot-accept-encoding: GZIP`` 的响应是 RC4 密文，解密要
            # **同一个 nonce**——所以它跟着连接一起传下来。
            payload = json.loads(decrypt(ssecurity, nonce, text))
        if not isinstance(payload, Mapping):
            return UNCONFIRMED, "回执不是 JSON 对象"
        code = int(payload.get("code") or 0)
        if code != 0:
            return code, str(payload.get("message") or payload.get("desc") or "")
        inner = _action_code(payload.get("result"))
        return (inner, "" if inner == 0 else "动作没有执行成功")


def _action_code(result: Any) -> int:
    """``/miotspec/action`` 的成功码藏在 ``result`` 里，位置有两种形状。

    上游 ``Client.run_action`` 就是这么解的（见 ``core/api.py``），这里照抄一份，
    免得两处各写一个解析、以后只改了一边。
    """
    if isinstance(result, list) and result and isinstance(result[0], Mapping):
        return int(result[0].get("code") or 0)
    if isinstance(result, Mapping):
        return int(result.get("code") or 0)
    return 0


def _why(exc: BaseException) -> str:
    """把底层异常翻成一句能放进日志的话。**保留原始文本**方便对文档。"""
    name = type(exc).__name__
    text = str(exc).strip()
    if isinstance(exc, TimeoutError):
        return "等云端回执超时（设备没有回应）"
    return f"{name}: {text}" if text else name
