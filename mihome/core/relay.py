# SPDX-License-Identifier: GPL-3.0-or-later
"""小米摄像头的跨网中继（relay）通道。

go2rtc 的 CS2 源只会往设备局域网 IP 的 32108/UDP 发 ``LAN search``，没有中继实现。
这里补的是**传输层旁路**：先向小米云端要一份 P2P 授权，走完 relay 握手，然后在
``127.0.0.1:<本机端口>`` 上扮成"设备"接受 go2rtc 的局域网握手，再把 ``DRW`` 帧
原样搬到 relay 会话上。媒体仍是 MISS 加密负载，本模块不解密、不改包，只搬字节。

     go2rtc ──CS2 帧──> 本模块(127.0.0.1:动态端口) ──PPPP DRW──> 小米 relay ──> 摄像头

协议细节来自 mihome-mac 的 ``relay.rs`` / ``pppp.rs``，并已在
``E:\\Code\\_crossnet_probe`` 用真实账号验证过握手与出画；本文件是那份原型的工程化版本，
补上了原型没有的部分：

* 端口不再固定 32108：从 32108 往后找空闲 UDP 端口，避免和别的进程打架
* 授权里的 ``expires`` 到期会把会话标成 ``expired``，下一次取流重新授权
* relay 主动 ``CLOSE`` 时自动重握手一次；再失败就标 ``lost``，把选择权交回界面
* 后台线程可停止、可回收（``close()``），不会留孤儿线程/套接字
* 每个阶段失败都带 ``kind``，界面能区分"没响应 / 拒绝注册 / 观看位满 / 超时"

本模块只被 ``core/camera.py`` 使用，不直接碰界面。
"""

from __future__ import annotations

import json
import os
import select
import socket
import threading
import time
from dataclasses import dataclass, field

from . import timing

# ---------------------------------------------------------------- PPPP 帧

MAGIC = 0xF1

MSG_LAN_SEARCH = 0x30
MSG_PUNCH = 0x41
MSG_P2P_RDY_UDP = 0x42
MSG_P2P_RDY_TCP = 0x43
MSG_DRW = 0xD0
MSG_DRW_ACK = 0xD1
MSG_ALIVE = 0xE0
MSG_ALIVE_ACK = 0xE1
MSG_MISS_SETUP = 0x28
MSG_CLOSE = 0xF0
MSG_LIST_REQ1 = 0x67
MSG_RLY_TO = 0x82
MSG_RLY_HELLO = 0x70
MSG_RLY_HELLO_ACK = 0x71
MSG_RLY_PORT = 0x72
MSG_RLY_PORT_ACK = 0x73
MSG_RLY_REQ = 0x80
MSG_RLY_PKT = 0x83
MSG_RLY_RDY = 0x84

SERVER_PORT = 32100
RELAY_PORT = 25000
DEFAULT_BIND_PORT = 32108
BIND_SPAN = 20

KEEPALIVE_SECONDS = 2.0
DEFAULT_TIMEOUT = 15.0
#: 授权剩这么多秒时不再复用，宁可重新握手
EXPIRY_MARGIN = 60.0
#: 宣告一个不可达地址，避免摄像头绕开 relay 尝试直连（原型同值）
DEFAULT_ADVERTISE = "10.255.255.1"


class RelayError(RuntimeError):
    """relay 通道失败。``kind`` 给上层映射成用户能看懂的原因。"""

    def __init__(self, message: str, kind: str = "relay") -> None:
        super().__init__(message)
        self.kind = kind


def frame(kind: int, payload: bytes = b"") -> bytes:
    return bytes([MAGIC, kind]) + len(payload).to_bytes(2, "big") + payload


def parse_frame(data: bytes) -> tuple[int, bytes] | None:
    if len(data) < 4 or data[0] != MAGIC:
        return None
    size = int.from_bytes(data[2:4], "big")
    if size > len(data) - 4:
        return None
    return data[1], data[4 : 4 + size]


# ---------------------------------------------------------------- 编码器

_LUT = bytes(
    [
        0x49, 0x59, 0x43, 0x3D, 0xB5, 0xBF, 0x6D, 0xA3, 0x47, 0x53, 0x4F, 0x61,
        0x65, 0xE3, 0x71, 0xE9, 0x67, 0x7F, 0x02, 0x03, 0x0B, 0xAD, 0xB3, 0x89,
        0x2B, 0x2F, 0x35, 0xC1, 0x6B, 0x8B, 0x95, 0x97, 0x11, 0xE5, 0xA7, 0x0D,
        0xEF, 0xF1, 0x05, 0x07, 0x83, 0xFB, 0x9D, 0x3B, 0xC5, 0xC7, 0x13, 0x17,
        0x1D, 0x1F, 0x25, 0x29, 0xD3, 0xDF,
    ]
)


def decode_init_string(encoded: str) -> list[str]:
    """云端 ``init_string`` → rendezvous 服务器 IP 列表（小米自研混淆）。"""
    head = (encoded or "").split(":")[0]
    if not head or len(head) % 2:
        raise RelayError("云端没有返回可用的远程服务器地址", "auth")
    out = bytearray()
    for i in range(len(head) // 2):
        hi = ord(head[2 * i]) - ord("A")
        lo = ord(head[2 * i + 1]) - ord("A")
        if not (0 <= hi < 16 and 0 <= lo < 16):
            raise RelayError("云端返回的远程服务器地址无法解析", "auth")
        running = 0x39
        for byte in out:
            running ^= byte
        out.append(running ^ _LUT[i % len(_LUT)] ^ (hi * 16 + lo))
    text = out.decode("utf-8", "replace")
    return [part.strip() for part in text.split(",") if part.strip()]


def uid_bytes(p2p_id: str) -> bytes:
    parts = (p2p_id or "").strip().split("-")
    if len(parts) != 3:
        raise RelayError(f"云端返回的 P2P 标识格式不对：{p2p_id}", "auth")
    out = bytearray(20)
    prefix = parts[0].encode()[:8]
    out[: len(prefix)] = prefix
    out[8:12] = int(parts[1]).to_bytes(4, "big")
    check = parts[2].encode()[:8]
    out[12 : 12 + len(check)] = check
    return bytes(out)


def addr_encode(ip: str, port: int) -> bytes:
    out = bytearray(16)
    out[0] = 0x00
    out[1] = 0x02
    out[2:4] = int(port).to_bytes(2, "little")
    octets = bytes(int(x) for x in ip.split("."))
    out[4:8] = octets[::-1]
    return bytes(out)


def addr_decode(payload: bytes, offset: int = 0) -> tuple[str, int]:
    port = int.from_bytes(payload[offset + 2 : offset + 4], "little")
    octets = payload[offset + 4 : offset + 8]
    ip = ".".join(str(b) for b in octets[::-1])
    return ip, port


def setup_reply(request: bytes, local_ip: str, local_port: int) -> bytes:
    """relay 的 ``0x28`` 建链请求的确定性应答（对照 ``pppp.rs::miss_setup_reply``）。"""
    if len(request) < 88 or request[0] != 0xA1 or request[1] != 0x01:
        return b""
    out = bytearray(request)
    out[1] = 0x02
    out[56:60] = b"\xfe\xff\xff\xff"
    for i in range(4):
        out[60 + i] = request[4 + 3 - i]
    out[64:80] = addr_encode(local_ip, local_port)
    ext_ip, ext_port = addr_decode(request, 40)
    out[80] = 0x02
    out[81] = 0x00
    out[82:84] = ext_port.to_bytes(2, "big")
    out[84:88] = bytes(int(x) for x in ext_ip.split("."))
    return bytes(out)


# ---------------------------------------------------------------- 云端授权


def _expiry_deadline(expires: object) -> float:
    """把云端 ``expires`` 折算成绝对时间。

    实测版本里没有文档说明单位，所以三种都接：绝对秒、绝对毫秒、剩余秒数。
    认不出来就按一小时处理——宁可早一点重新授权，也不要拿过期凭据硬连。
    """
    now = time.time()
    try:
        value = float(expires)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return now + 3600.0
    if value <= 0:
        return now + 3600.0
    if value > 1e12:  # 毫秒时间戳
        value /= 1000.0
    if value > 1e9:  # 秒时间戳
        return value
    return now + max(60.0, value)


@dataclass
class VendorAuth:
    """一次云端的 P2P 授权结果。"""

    p2p_id: str
    servers: list[str]
    region: str = "CN"
    expires_at: float = 0.0
    vendor_id: int = 0
    raw: dict = field(default_factory=dict)


def request_vendor(api: object, did: str) -> VendorAuth:
    """请求 ``miss_get_vendor``。``api`` 是 ``mijiaAPI`` 对象（项目里走 ``client.raw()``）。

    关键点（原型实测定下来的）：host 是 ``api.mijia.tech``，但 RC4 签名的 path
    要去掉 ``/app`` 前缀。
    """
    timing.mark("miss_get_vendor 请求发出")
    try:
        from mijiaAPI.miutils import (
            decrypt,
            gen_nonce,
            generate_enc_params,
            get_signed_nonce,
        )
    except ImportError as exc:  # pragma: no cover - 只在旧版 mijiaAPI 上发生
        raise RelayError("当前 mijiaAPI 版本缺少跨网授权接口", "auth") from exc

    try:
        if not bool(getattr(api, "available", True)):
            raise RelayError("本机没有可用的米家登录凭据，请重新登录", "auth")
        auth = getattr(api, "auth_data", None) or {}
        session = getattr(api, "session", None)
    except RelayError:
        raise
    except Exception as exc:  # 凭据文件损坏 / 上游属性异常
        raise RelayError(f"读取登录凭据失败：{exc}", "auth") from exc
    if not auth or session is None:
        raise RelayError("登录凭据不完整，请重新扫码登录", "auth")
    ssecurity = str(auth.get("ssecurity", "") or "")
    if not ssecurity:
        raise RelayError("登录凭据不完整，请重新扫码登录", "auth")

    try:
        from Crypto.PublicKey import ECC

        pub = ECC.generate(curve="Curve25519").public_key().export_key(format="raw")
    except Exception:  # noqa: BLE001 - 只影响云端的绑定，退回随机值照样能拿到授权
        pub = os.urandom(32)

    body = json.dumps(
        {"did": did, "app_pubkey": pub.hex(), "support_vendors": "TUTK_CS2_MTP"},
        separators=(",", ":"),
    )
    path = "/v2/device/miss_get_vendor"
    nonce = gen_nonce()
    signed = get_signed_nonce(ssecurity, nonce)
    params = generate_enc_params(path, "POST", signed, nonce, {"data": body}, ssecurity)
    base = str(getattr(api, "api_base_url", "https://api.mijia.tech/app") or "")
    try:
        resp = session.post(base + path, data=params, timeout=8)
        try:
            data = json.loads(resp.text)
        except json.JSONDecodeError:
            data = json.loads(decrypt(ssecurity, nonce, resp.text))
    except Exception as exc:  # 网络/云端错误统一归类，避免界面莫名失败
        timing.mark("miss_get_vendor 失败", str(exc))
        raise RelayError(f"远程取流授权失败：{exc}", "auth") from exc

    if not isinstance(data, dict) or data.get("code", 0) != 0:
        detail = data if isinstance(data, dict) else {}
        timing.mark("miss_get_vendor 被拒绝", f"code={detail.get('code')}")
        raise RelayError(
            f"远程取流授权被拒绝（code={detail.get('code')}）", "auth"
        )
    result = data.get("result") or {}
    vendor = result.get("vendor") or {}
    params = dict(vendor.get("vendor_params") or {})
    p2p_id = str(params.get("p2p_id", "") or "")
    init_string = str(params.get("init_string", "") or "")
    if not p2p_id or not init_string:
        raise RelayError("云端没有返回远程通道信息", "auth")
    auth = VendorAuth(
        p2p_id=p2p_id,
        servers=decode_init_string(init_string),
        region=str(result.get("region") or params.get("region") or "CN"),
        expires_at=_expiry_deadline(
            params.get("expires") or vendor.get("expires") or result.get("expires")
        ),
        vendor_id=int(vendor.get("vendor") or 0),
        raw=params,
    )
    timing.mark(
        "miss_get_vendor 返回",
        f"servers={len(auth.servers)} expires_at={auth.expires_at:.0f}",
    )
    return auth


# ---------------------------------------------------------------- 会话


def _bind_udp(preferred: int, span: int = BIND_SPAN) -> tuple[socket.socket, int]:
    """在 ``preferred`` 往后找空闲 UDP 端口。绑定 ``0.0.0.0``：

    relay 的回复会打到本机局域网地址，绑 127.0.0.1 收不到；go2rtc 发到
    127.0.0.1 的包，通配地址也能收到。
    """
    for port in range(preferred, preferred + span):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(("0.0.0.0", port))
            sock.setblocking(False)
            return sock, port
        except OSError:
            sock.close()
    raise RelayError(
        f"本机没有可用的 UDP 端口（试过 {preferred}–{preferred + span - 1}）", "port"
    )


class RelaySession:
    """一条 relay 会话：握手、UDP 桥、keepalive、到期/重连。

    同一时刻只应该存在一个实例（一台摄像头只有有限的观看位）。
    ``start()`` 是阻塞的（握手 3–10 秒），必须从后台线程调用。
    """

    def __init__(
        self,
        vendor: VendorAuth,
        *,
        advertise: str = "",
        bind_port: int = DEFAULT_BIND_PORT,
    ) -> None:
        if not vendor.servers:
            raise RelayError("摄像头没有返回远程服务器地址", "auth")
        self.vendor = vendor
        self.uid = uid_bytes(vendor.p2p_id)
        self.servers = list(vendor.servers)
        self.advertise = advertise or DEFAULT_ADVERTISE
        self.expires_at = vendor.expires_at
        self.region = vendor.region

        self.sock: socket.socket | None = None
        self.port = 0
        self.relay: tuple[str, int] | None = None
        self.token: bytes | None = None

        #: idle / running / reconnecting / expired / lost / closed
        self.state = "idle"
        self.reason = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client: tuple[str, int] | None = None
        self._client_closed = False
        self._recoveries = 0
        self._last_rx = 0.0
        self._preferred_port = bind_port
        # 【临时探针】start() 所在线程的计时记录；后台重连时沿用它
        self._timing = 0

    # -- 状态 ------------------------------------------------------------
    @property
    def usable(self) -> bool:
        """还能直接复用吗（进程活着、没断、授权没快到期）。"""
        return (
            self.state == "running"
            and time.time() < self.expires_at - EXPIRY_MARGIN
        )

    def hint(self) -> str:
        """画面/取流失败时的“人话”，没有可解释的就返回空串。"""
        if self.state == "expired":
            return "远程授权已到期，点重试重新连接"
        if self.state == "lost":
            return "远程中继连接中断，点重试重新连接"
        return ""

    # -- 启动 ------------------------------------------------------------
    def start(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        # 【临时探针】只打时间点，不参与任何判断
        timing.mark("relay 绑定本机 UDP 端口")
        self.sock, self.port = _bind_udp(self._preferred_port)
        timing.mark("relay 代理端口就绪", f"port={self.port}")
        try:
            timing.mark("relay 握手开始")
            self._handshake(time.time() + timeout)
        except Exception:
            timing.mark("relay 握手失败")
            self._shutdown_socket()
            self.state = "closed"
            raise
        timing.mark(
            "relay 握手完成",
            f"relay={self.relay[0]}:{self.relay[1]}" if self.relay else "",
        )
        self.state = "running"
        self.reason = ""
        self._last_rx = time.time()
        self._timing = timing.current()
        self._thread = threading.Thread(
            target=self._run, name="mihome-relay", daemon=True
        )
        self._thread.start()
        timing.mark("relay 会话 running", f"proxy=0.0.0.0:{self.port}")

    def _handshake(self, deadline: float) -> None:
        self.relay = None
        self.token = None
        candidates = self._discover(deadline)
        timing.mark("RLY_TO 发现完成", f"候选={len(candidates)}")
        if not candidates:
            raise RelayError("远程中继没有响应（检查网络后重试）", "discover")
        chosen = self._greet(candidates, deadline)
        timing.mark(
            "RLY_HELLO_ACK 完成",
            f"server={chosen[0]}:{chosen[1]}" if chosen else "无响应",
        )
        if chosen is None:
            raise RelayError("远程中继拒绝注册，稍后重试", "register")
        if not self._allocate(chosen, deadline):
            raise RelayError("远程中继没有分配会话端口", "allocate")
        timing.mark(
            "RLY_PORT_ACK 端口分配",
            f"relay={self.relay[0]}:{self.relay[1]}" if self.relay else "",
        )
        if not self._join(deadline):
            raise RelayError(
                "摄像头没有加入远程中继：可能观看位已满（最多 3 路），或设备不在线",
                "join",
            )
        timing.mark("RLY_RDY 握手完成")

    # -- 握手各阶段 ------------------------------------------------------
    def _server_addrs(self) -> list[tuple[str, int]]:
        return [(server, SERVER_PORT) for server in self.servers]

    def _send(self, addr: tuple[str, int], kind: int, payload: bytes = b"") -> None:
        sock = self.sock
        if sock is None:
            return
        try:
            sock.sendto(frame(kind, payload), addr)
        except OSError:
            pass

    def _recv(self, deadline: float) -> tuple[int, bytes, tuple[str, int]] | None:
        sock = self.sock
        if sock is None:
            return None
        while not self._stop.is_set():
            left = deadline - time.time()
            if left <= 0:
                return None
            try:
                ready, _, _ = select.select([sock], [], [], min(left, 0.3))
            except (OSError, ValueError):
                return None
            if not ready:
                continue
            try:
                data, addr = sock.recvfrom(4096)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                return None
            parsed = parse_frame(data)
            if parsed is not None:
                return parsed[0], parsed[1], addr
        return None

    def _discover(self, deadline: float) -> dict[tuple[str, int], list[bytes]]:
        for addr in self._server_addrs():
            self._send(addr, MSG_LIST_REQ1, self.uid)
        hard = min(time.time() + 2.5, deadline)
        # 第一个 RLY_TO 通常几百毫秒就到；收到后只再等 600ms 的静默窗口，
        # 不在每次打开时都把 2.5 秒耗满。
        quiet = hard
        found: dict[tuple[str, int], list[bytes]] = {}
        while True:
            got = self._recv(quiet)
            if got is None:
                break
            kind, payload, _ = got
            if kind != MSG_RLY_TO or len(payload) < 20:
                continue
            ip, port = addr_decode(payload, 0)
            token = payload[16:20]
            found.setdefault((ip, port), []).append(token)
            quiet = min(time.time() + 0.6, hard)
        return found

    def _register(self, addr: tuple[str, int], tokens: list[bytes]) -> None:
        for token in tokens:
            body = token + self.uid + b"\x00\x00\x00\x00"
            for _ in range(4):
                self._send(addr, MSG_RLY_PKT, body)

    def _greet(
        self, candidates: dict[tuple[str, int], list[bytes]], deadline: float
    ) -> tuple[str, int] | None:
        for _ in range(4):
            for addr, tokens in candidates.items():
                self._register(addr, tokens)
            for addr in candidates:
                self._send(addr, MSG_RLY_HELLO)
            stop = min(time.time() + 2.5, deadline)
            while True:
                got = self._recv(stop)
                if got is None:
                    break
                if got[0] == MSG_RLY_HELLO_ACK:
                    return got[2][0], RELAY_PORT
            if time.time() >= deadline:
                break
        return None

    def _allocate(self, chosen: tuple[str, int], deadline: float) -> bool:
        for _ in range(4):
            self._send(chosen, MSG_RLY_PORT)
            stop = min(time.time() + 2.0, deadline)
            while True:
                got = self._recv(stop)
                if got is None:
                    break
                kind, payload, _ = got
                if kind == MSG_RLY_PORT_ACK and len(payload) >= 6:
                    self.token = payload[:4]
                    port = int.from_bytes(payload[4:6], "big")
                    self.relay = (chosen[0], port)
                    return True
        return False

    def _join(self, deadline: float) -> bool:
        relay, token = self.relay, self.token
        if relay is None or token is None:
            return False
        body = self.uid + addr_encode(*relay) + token
        keepalive = frame(MSG_RLY_PKT, token + self.uid + b"\x00\x00\x00\x00")
        sock = self.sock
        if sock is None:
            return False
        for _ in range(6):
            if time.time() >= deadline:
                break
            for addr in self._server_addrs():
                self._send(addr, MSG_RLY_REQ, body)
            for _ in range(24):
                try:
                    sock.sendto(keepalive, relay)
                except OSError:
                    pass
            stop = min(time.time() + 2.0, deadline)
            while True:
                got = self._recv(stop)
                if got is None:
                    break
                if got[0] == MSG_RLY_RDY:
                    return True
        return False

    # -- 运行期 ----------------------------------------------------------
    def _run(self) -> None:
        last_keepalive = 0.0
        while not self._stop.is_set():
            if self.expires_at and time.time() >= self.expires_at:
                self.state = "expired"
                self.reason = "授权到期"
                break
            now = time.time()
            if now - last_keepalive >= KEEPALIVE_SECONDS:
                self._keepalive()
                last_keepalive = now
            # 20 秒没收到 relay 的任何帧：UDP 通道已经被中间设备清掉了。
            # go2rtc 单方面还在发包不算数，只有 relay 回包才算活着。
            if now - self._last_rx > 20.0:
                if self._recover():
                    self._last_rx = time.time()
                else:
                    self.state = "lost"
                    if not self.reason:
                        self.reason = "relay 静默"
                    break
            sock = self.sock
            if sock is None:
                break
            try:
                ready, _, _ = select.select([sock], [], [], 0.2)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                data, addr = sock.recvfrom(8192)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                break
            relay = self.relay
            if relay is not None and addr[0] == relay[0]:
                self._last_rx = time.time()
                self._from_relay(data)
            elif addr[0] == "127.0.0.1":
                self._from_client(data, addr)
            # 其它来源（扫描、旧会话残留）直接丢掉
            if self.state in ("lost", "expired", "closed"):
                break

        if not self._stop.is_set() and self.state == "running":
            self.state = "lost"
            if not self.reason:
                self.reason = "通道结束"
        self._shutdown_socket()

    def _keepalive(self) -> None:
        relay, token = self.relay, self.token
        if relay is None or token is None:
            return
        self._send(relay, MSG_RLY_PKT, token + self.uid + b"\x00\x00\x00\x00")

    # -- go2rtc 侧 -------------------------------------------------------
    def _send_client(self, kind: int, payload: bytes = b"") -> None:
        if self._client is not None:
            self._send(self._client, kind, payload)

    def _from_client(self, data: bytes, addr: tuple[str, int]) -> None:
        self._client = addr
        parsed = parse_frame(data)
        if parsed is None:
            return
        kind = parsed[0]
        if kind == MSG_LAN_SEARCH:
            # 设备在 relay 侧，不需要局域网搜索。go2rtc 只认来自同 IP、
            # 总长 >= 16 的回复。
            self._send_client(MSG_PUNCH, b"\x00" * 12)
        elif kind in (MSG_PUNCH, MSG_P2P_RDY_UDP, MSG_P2P_RDY_TCP):
            self._send_client(MSG_P2P_RDY_UDP, b"\x00" * 12)
        elif kind == MSG_ALIVE:
            self._send_client(MSG_ALIVE_ACK)
        elif kind in (MSG_DRW, MSG_DRW_ACK, MSG_CLOSE):
            if kind == MSG_CLOSE:
                # go2rtc 主动断开（详情页关了）：之后 relay 的 CLOSE 不再重连
                self._client_closed = True
            if self.relay is not None:
                self._send(self.relay, kind, parsed[1])

    # -- relay 侧 --------------------------------------------------------
    def _from_relay(self, data: bytes) -> None:
        parsed = parse_frame(data)
        if parsed is None or self.relay is None:
            return
        kind, payload = parsed
        if kind == MSG_MISS_SETUP:
            reply = setup_reply(payload, self.advertise, self.port)
            if reply:
                self._send(self.relay, MSG_MISS_SETUP, reply)
        elif kind == MSG_ALIVE:
            self._send(self.relay, MSG_ALIVE_ACK)
        elif kind in (MSG_DRW, MSG_DRW_ACK):
            if self._client is not None:
                self._send(self._client, kind, payload)
        elif kind == MSG_CLOSE:
            if self._client_closed or self._stop.is_set():
                self.state = "closed"
                return
            if self._recover():
                return
            self.state = "lost"
            self.reason = "relay 断开"

    def _recover(self) -> bool:
        """relay 主动断开时重握手一次。媒体会话本来就要重建，这里只抢传输层。"""
        if self._recoveries >= 1 or time.time() >= self.expires_at:
            return False
        self._recoveries += 1
        self.state = "reconnecting"
        timing.bind(self._timing)
        timing.mark_on(self._timing, "relay 重连开始")
        try:
            self._handshake(time.time() + 12.0)
        except RelayError as exc:
            self.reason = str(exc)
            timing.mark_on(self._timing, "relay 重连失败", str(exc))
            return False
        self.state = "running"
        self.reason = ""
        timing.mark_on(self._timing, "relay 重连完成")
        return True

    # -- 停止 ------------------------------------------------------------
    def close(self) -> None:
        """幂等。可以从任意线程调用；后台线程最多等 2 秒。"""
        if self._stop.is_set() and self.state in ("closed", "expired", "lost"):
            self._shutdown_socket()
            return
        self._stop.set()
        timing.mark_on(self._timing, "relay 会话关闭")
        if self.sock is not None and self.relay is not None:
            # 尽量告诉 relay 这条会话结束了；发不出去也无所谓，keepalive 会超时
            self._send(self.relay, MSG_CLOSE)
        self._shutdown_socket()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            # 后台循环每 0.2 秒看一次 _stop，正常情况下立刻退出；限时只是兜底
            thread.join(timeout=0.5)
        self._thread = None
        if self.state not in ("expired", "lost"):
            self.state = "closed"

    def _shutdown_socket(self) -> None:
        sock = self.sock
        self.sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


__all__ = [
    "RelayError",
    "RelaySession",
    "VendorAuth",
    "addr_decode",
    "addr_encode",
    "decode_init_string",
    "frame",
    "parse_frame",
    "request_vendor",
    "setup_reply",
    "uid_bytes",
]
