# SPDX-License-Identifier: GPL-3.0-or-later
"""验证详情页"打开即自动起流"（`DetailDialog._auto_start_camera`）。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_camera_autoplay.py``

只验路由，不碰真实摄像头 / go2rtc：给一个假 Win 挂上真方法，
把 `_start_camera` 换成计数器，检查各状态下起流 / 跳过是否正确。
另检查 prefs 里已无 `camera_preheat`、取流默认 `remote_first`。
"""

from __future__ import annotations

import inspect
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_TMP = Path(tempfile.mkdtemp(prefix="mihome-autoplay-"))
os.environ["MIHOME_DATA_DIR"] = str(_TMP / "data")

from mihome.core.prefs import (  # noqa: E402
    CAMERA_NETWORK_DEFAULT,
    PREFS_VERSION,
    Prefs,
)
from mihome.ui.detail import DetailDialog  # noqa: E402
from mihome.ui.main_window import MainWindow  # noqa: E402
from mihome.ui.settings import SettingsDialog  # noqa: E402

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


class Win:
    """挂真 `_auto_start_camera`，其余状态自己造。"""

    _auto_start_camera = DetailDialog._auto_start_camera

    def __init__(
        self,
        *,
        camera: bool = True,
        adopted: bool = False,
        url: str = "",
        job: bool = False,
        wants_play: bool = False,
    ) -> None:
        self._camera = object() if camera else None
        self._cam_adopted = adopted
        self._cam_auto_started = False
        self._cam_url = url
        self._cam_job = object() if job else None
        self._cam_wants_play = wants_play
        self.started = 0

    def _start_camera(self) -> None:
        self.started += 1


def main() -> None:
    # 详情页打开就起流
    win = Win()
    win._auto_start_camera()
    check("正常打开：起一次流", win.started, 1)
    win._auto_start_camera()
    check("重复调用：一次性守卫挡住", win.started, 1)

    # 从卡片墙接过来的在播画面：不重复 open，否则把正在播的会话改道
    adopted = Win(adopted=True)
    adopted._auto_start_camera()
    check("卡片墙接续：不起流", adopted.started, 0)

    # 已有地址 / 正在后台取流 / 用户已点播放：都不该再起一条
    cases = {
        "已有地址": Win(url="rtsp://127.0.0.1:8554/cam_test"),
        "后台取流中": Win(job=True),
        "用户已点播放": Win(wants_play=True),
        "无画面控件": Win(camera=False),
    }
    for name, case in cases.items():
        case._auto_start_camera()
        check(f"{name}：跳过", case.started, 0)

    # 偏好层：预热字段已删、取流默认远程优先、设置页无摄像头开关
    prefs = Prefs(_TMP / "prefs.json")
    check("camera_preheat 字段已移除", hasattr(prefs, "camera_preheat"), False)
    check("取流默认远程优先", CAMERA_NETWORK_DEFAULT, "remote_first")
    check("取流字段默认", prefs.camera_network, "remote_first")
    check("偏好版本 v22", PREFS_VERSION, 22)

    # 设置页已无两个开关的信号 / 控件方法
    check("设置页无预热信号", hasattr(SettingsDialog, "camera_preheat_changed"), False)
    check("设置页无取流回调", hasattr(SettingsDialog, "_on_camera_network"), False)

    # 详情页已无旧预热方法（防止有人把进程级预热又加回来）
    check("无旧 _prefetch_camera", hasattr(DetailDialog, "_prefetch_camera"), False)
    check("有 _auto_start_camera", callable(getattr(DetailDialog, "_auto_start_camera", None)), True)

    # 进程级预热：启动时把 go2rtc 拉起来（只登记源，不取流）。2026-11-28 加，
    # 配合自建 go2rtc 的换票缓存补丁，把"进程启动 + 云端换票"挪出点击后的关键路径。
    warm = getattr(MainWindow, "_warm_camera_service", None)
    check("主窗口有进程级预热", callable(warm), True)
    src = inspect.getsource(MainWindow)
    check("加载完就预热", "_warm_camera_service()" in src, True)
    if callable(warm):
        body = inspect.getsource(warm)
        check("只跑一次（先查标志）", body.index("_camera_warmed:") < body.index("Thread("), True)
        check("设备表空时不置位", body.index("if not nodes:") < body.index("_camera_warmed = True"), True)
        check("用 prepare()，不取流", "prepare(" in body and ".open(" not in body, True)

    print(f"\n通过 {OK} 项，失败 {FAIL} 项")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
