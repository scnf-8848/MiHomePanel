# SPDX-License-Identifier: GPL-3.0-or-later
"""验证轮询健康记录（`mihome/core/health.py` + `api.Client._record_poll`）。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_poll_health.py``

全程离线：只用临时目录里的日志文件，不建 Client、不发任何网络请求。验的是四件事：

* **分类**：四种异常分别落到 network / auth / server / protocol，原始码与文本不丢；
* **不刷屏**：同一错误连续 N 次只有 1 行失败记录，恢复时 1 行；错误类型变化会立刻补 1 行；
* **聚合**：持续失败按 ``SUMMARY_SECONDS`` 才补聚合行，不是每轮一行；
* **重启可见**：``close()`` 之后新开一个实例，日志开头能读到上次运行结束时好不好。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mijiaAPI.errors import APIError, LoginError  # noqa: E402

from mihome.core import health as H  # noqa: E402
from mihome.core.api import Client, PropRef, ReadValue  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(
        f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}",
        flush=True,
    )
    if not ok:
        FAILS.append(f"{name} {detail}")


def lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def count(path: Path, key: str) -> int:
    return sum(1 for line in lines(path) if key in line)


def _classify() -> None:
    auth = H.classify_error(APIError(-10030, "无效的token（HTTP）"))
    check("APIError -10030 → 授权", auth.kind == H.AUTH, auth.kind)
    check("授权码原样保留", auth.code == "-10030", auth.code)

    rate = H.classify_error(APIError(-704040992, "请求过于频繁"))
    check("APIError 992 → 限流/服务端", rate.kind == H.SERVER, rate.kind)
    check("限流原文完整", "请求过于频繁" in rate.text and "-704040992" in rate.text, rate.text)

    login = H.classify_error(LoginError(-1, "刷新Token失败，请重新登录"))
    check("LoginError → 授权", login.kind == H.AUTH, login.kind)

    proto = H.classify_error(ValueError("Expecting value: line 1 column 1 (char 0)"))
    check("JSON 解析失败 → 协议", proto.kind == H.PROTOCOL, proto.kind)

    net = H.classify_error(ConnectionError("[WinError 10065] 无法访问主机"))
    check("连接失败 → 网络", net.kind == H.NETWORK, net.kind)

    unknown = H.classify_error(APIError(-704042011, "设备离线"))
    check("其它服务端码 → 服务端且保留原码", unknown.kind == H.SERVER and unknown.code == "-704042011")


def _skeleton(path: Path) -> H.PollHealth:
    return H.PollHealth(
        log_path=path / "poll_health.log", state_path=path / "poll_health.last.json"
    )


def _flood() -> None:
    """同一错误连报 30 次，日志只应出现 1 行失败。"""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        log = folder / "poll_health.log"
        h = _skeleton(folder)
        fail = H.Failure(H.NETWORK, "", "ConnectionError: timed out")
        for _ in range(30):
            h.failure(fail)
        check("30 次同错只写 1 行", count(log, "轮询失败") == 1, str(count(log, "轮询失败")))
        check("连续失败次数记满 30", h.snapshot()["streak"] == 30, str(h.snapshot()["streak"]))

        h.success(items=138)
        check("恢复写 1 行", count(log, "恢复正常") == 1)
        check("恢复后 healthy", h.snapshot()["healthy"] is True)
        check("恢复后连续失败清零", h.snapshot()["streak"] == 0)

        # 错误类型变了：必须立刻补一行，不能等聚合窗口
        h.failure(H.Failure(H.NETWORK, "", "ConnectionError: timed out"))
        h.failure(H.Failure(H.AUTH, "-10030", "无效的token（HTTP）"))
        check("类型变化立刻再写 1 行", count(log, "轮询失败") == 3, str(count(log, "轮询失败")))


def _aggregate() -> None:
    """把聚合窗口压到 0，验证"持续失败"会补聚合行而不是每轮一行。"""
    original = H.SUMMARY_SECONDS
    H.SUMMARY_SECONDS = 0.0
    try:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            log = folder / "poll_health.log"
            h = _skeleton(folder)
            fail = H.Failure(H.SERVER, "-704040992", "请求过于频繁")
            for _ in range(10):
                h.failure(fail)
            check("聚合行按窗口出现", count(log, "仍在失败") == 9, str(count(log, "仍在失败")))
            check("聚合不重复写失败头行", count(log, "轮询失败") == 1)
    finally:
        H.SUMMARY_SECONDS = original


def _backoff() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        log = folder / "poll_health.log"
        h = _skeleton(folder)
        h.backoff(streak=1, seconds=6.0)
        h.backoff(streak=1, seconds=6.0)  # 同档重复不写
        h.backoff(streak=2, seconds=12.0)
        check("退避同档不重复写", count(log, "触发退避") == 2, str(count(log, "触发退避")))
        check("退避间隔进快照", (h.snapshot()["backoff"] or {}).get("seconds") == 12.0)
        h.success(items=1)
        check("恢复后退避清零", h.snapshot()["backoff"] is None)


def _restart() -> None:
    """关闭后重开：日志开头要看得到上次运行的结论。"""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        log = folder / "poll_health.log"
        first = _skeleton(folder)
        for _ in range(3):
            first.failure(H.Failure(H.NETWORK, "", "ConnectionError: timed out"))
        first.close()

        second = _skeleton(folder)
        text = "\n".join(lines(log))
        check("重启后写上次运行结论", "上次运行：" in text)
        check("结论里带连续失败次数", "连续失败 3 次" in text, text.splitlines()[-1])
        check("新实例从 0 开始", second.snapshot()["streak"] == 0)
        check("快照带上次运行原始状态", bool(second.snapshot()["previous"]))
        second.close()

        _skeleton(folder)  # 再建一个实例，只为让上次的正常退出写进日志
        healthy = "\n".join(lines(log)[-6:])
        check("上次正常退出时写明正常", "轮询正常" in healthy, healthy[-160:])


def _record_poll() -> None:
    """`Client._record_poll` 的三条分支，不建真 Client。"""

    class Fake:
        def __init__(self, health: H.PollHealth) -> None:
            self.health = health

    refs = [PropRef("d1", 2, i) for i in (1, 2, 3)]

    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        log = folder / "poll_health.log"
        fake = Fake(_skeleton(folder))

        ok = {r: ReadValue(ref=r, value=1, ok=True) for r in refs}
        Client._record_poll(fake, ok, None, 3)
        check("全成功不写失败行", count(log, "轮询失败") == 0)

        dead = {r: ReadValue(ref=r, code=-704042011) for r in refs}
        Client._record_poll(fake, dead, None, 3)
        check("整批失败算失败", count(log, "轮询失败") == 1, str(count(log, "轮询失败")))
        check(
            "整批失败保留错误码",
            "-704042011" in "\n".join(lines(log)) and "设备离线" in "\n".join(lines(log)),
        )
        check("整批失败计入连续次数", fake.health.streak == 1, str(fake.health.streak))

        Client._record_poll(fake, ok, None, 3)
        check("随后成功即恢复", fake.health.streak == 0)

        Client._record_poll(fake, {}, H.Failure(H.PROTOCOL, "", "响应结构异常"), 3)
        check("请求层失败被记录", count(log, "轮询失败") == 2, str(count(log, "轮询失败")))
        check("协议类标签正确", "[协议]" in "\n".join(lines(log)))


def main() -> int:
    _classify()
    _flood()
    _aggregate()
    _backoff()
    _restart()
    _record_poll()
    print()
    if FAILS:
        print(f"FAIL {len(FAILS)} 项：" + "；".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
