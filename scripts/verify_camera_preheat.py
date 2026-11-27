# SPDX-License-Identifier: GPL-3.0-or-later
"""验证主窗口级摄像头预热（`MainWindow._maybe_preheat_cameras`）。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_camera_preheat.py``

只验路由，不碰真实摄像头 / go2rtc：把模块里的 `camera_service` 和 `Thread` 换掉，
让后台那一步同步跑完。关键点：

* 开关默认关；关着 / 窗口不可见 / 没有在线摄像头都不动；
* ``lan_first`` 只走 `prepare()`（不拨号、不占观看位）；
* ``remote_first`` / ``remote_only`` 走 `open(remote=True)`（占一个观看位，
  和"打开就起流"的对方程序同口径）；前者失败退回局域网；
* 一次进程内只预热一次，设置里再打开会强制补跑。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_TMP = Path(tempfile.mkdtemp(prefix="mihome-preheat-"))
os.environ["MIHOME_DATA_DIR"] = str(_TMP / "data")

import mihome.ui.main_window as mw_mod  # noqa: E402
from mihome.core.camera import CameraError  # noqa: E402
from mihome.ui.main_window import MainWindow  # noqa: E402

OK = 0
FAIL = 0


def check(name: str, got: object, want: object) -> None:
    global OK, FAIL
    if got == want:
        OK += 1
        print(f"PASS  {name}: {got!r}")
    else:
        FAIL += 1
        print(f"FAIL  {name}: 得到 {got!r}，期望 {want!r}")


def node(did: str = "1000000001", *, online: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        did=did,
        model="chuangmi.camera.069a01",
        online=online,
        localip="192.168.1.x",
    )


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.fail_remote = False

    def prepare(self, client: object, nodes: list[object]) -> None:
        self.calls.append(("prepare", tuple(getattr(n, "did", "") for n in nodes)))

    def open(self, client: object, item: object, *, remote: bool = False) -> str:
        self.calls.append(("open", getattr(item, "did", ""), remote))
        if remote and self.fail_remote:
            raise CameraError("relay 不通", "relay")
        return "rtsp://127.0.0.1:8554/cam_test"

    def release(self, did: str) -> None:
        self.calls.append(("release", did))


class Win:
    _maybe_preheat_cameras = MainWindow._maybe_preheat_cameras
    _on_camera_preheat_setting = MainWindow._on_camera_preheat_setting

    def __init__(self, svc: FakeService, *, on: bool, network: str,
                 nodes: list[object], visible: bool = True) -> None:
        self.svc = svc
        self.client = object()
        self._visible = visible
        self._camera_preheat_done = False
        self.store = SimpleNamespace(
            camera_preheat=on,
            camera_network=network,
            devices={getattr(n, "did", ""): SimpleNamespace(node=n) for n in nodes},
        )

    def isVisible(self) -> bool:
        return self._visible


def main() -> None:
    svc = FakeService()
    original_service = mw_mod.camera_service
    original_threading = mw_mod.threading
    mw_mod.camera_service = lambda: svc
    mw_mod.threading = SimpleNamespace(
        Thread=lambda target, **kw: SimpleNamespace(start=lambda: target())
    )
    try:
        # 开关关着：什么都不做
        off = Win(svc, on=False, network="lan_first", nodes=[node()])
        off._maybe_preheat_cameras()
        check("开关关时不预热", svc.calls, [])

        # 局域网档：只 prepare，不 open
        lan = Win(svc, on=True, network="lan_first", nodes=[node()])
        lan._maybe_preheat_cameras()
        check("局域网只登记源", svc.calls, [("prepare", ("1000000001",))])
        svc.calls.clear()
        lan._maybe_preheat_cameras()
        check("同进程只预热一次", svc.calls, [])

        # 仅远程：走 relay，占观看位；设置里再开一次强制补跑
        svc.calls.clear()
        remote = Win(svc, on=True, network="remote_only", nodes=[node()])
        remote._maybe_preheat_cameras()
        check("仅远程走 relay", svc.calls, [("open", "1000000001", True)])
        svc.calls.clear()
        remote._on_camera_preheat_setting(True)
        check("设置里再开强制补跑", svc.calls, [("open", "1000000001", True)])

        # 远程优先失败：释放会话 + 退回局域网
        svc.calls.clear()
        svc.fail_remote = True
        fallback = Win(svc, on=True, network="remote_first", nodes=[node()])
        fallback._maybe_preheat_cameras()
        check(
            "远程优先失败退回局域网",
            svc.calls,
            [("open", "1000000001", True), ("release", "1000000001"),
             ("prepare", ("1000000001",))],
        )
        svc.fail_remote = False

        # 不可见 / 没设备 / 只有离线设备：都不动
        svc.calls.clear()
        Win(svc, on=True, network="lan_first", nodes=[node()],
            visible=False)._maybe_preheat_cameras()
        Win(svc, on=True, network="lan_first", nodes=[])._maybe_preheat_cameras()
        Win(svc, on=True, network="lan_first",
            nodes=[node(online=False)])._maybe_preheat_cameras()
        check("不可见 / 空表 / 离线不预热", svc.calls, [])
    finally:
        mw_mod.camera_service = original_service
        mw_mod.threading = original_threading

    print(f"\n通过 {OK} 项，失败 {FAIL} 项")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
