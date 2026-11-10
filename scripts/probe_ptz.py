# SPDX-License-Identifier: GPL-3.0-or-later
"""真机探针：云台指令（MISS 0x112）到底能不能让画面真的动。

为什么要单独一个探针：这台摄像头（``chuangmi.camera.069a01``）的 MIoT 规格里
**没有 ptz 服务**，云台只能走和取流同一条 PSS/MISS 连接的 ``0x112`` 指令。
go2rtc 上游只定义了常量（``cmdMotorReq``）没有实现，所以先要一个带云台接口的
自建二进制（``scripts/build_go2rtc.py`` 打 ``go2rtc-ptz.patch``）。

流程：起 go2rtc → 播放并等首帧 → 截一帧 → 发一条指令 → 再截一帧 → 比像素差。
"接口返回 ok" 只能证明指令发出去了，**画面位移才算真的转了**。

用法：
  .venv/Scripts/python.exe -X utf8 scripts/probe_ptz.py --go2rtc <自建 go2rtc.exe>
  .venv/Scripts/python.exe -X utf8 scripts/probe_ptz.py --go2rtc ... --did 1000000002
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mijiaAPI import mijiaAPI
from PySide6.QtGui import QFont, QImage
from PySide6.QtWidgets import QApplication

from mihome.core.camera import CameraError, Go2rtcRuntime
from mihome.core.camera import shared as camera_service
from mihome.ui import theme
from mihome.ui.camera_view import CameraView

OUT = Path(__file__).resolve().parent / "out"

#: 方向名，和 go2rtc 那边的 op 编号对齐：1 左 2 右 3 上 4 下 5 校准
OPS = {1: "左", 2: "右", 3: "上", 4: "下", 5: "校准"}


def pump(app: QApplication, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def ptz_call(port: int, op: int = 0, name: str = "", timeout: float = 3.0) -> dict:
    """调本机 go2rtc 的云台接口；``op=0`` 只探能力。"""
    query = f"?name={urllib.request.quote(name)}&op={op}" if op else ""
    url = f"http://127.0.0.1:{port}/api/xiaomi/ptz{query}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"HTTP {exc.code}"}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


def grab_frames(view: CameraView) -> QImage:
    return view.grab().toImage()


def diff_ratio(a: QImage, b: QImage) -> float:
    """缩到 80×60 后比字节，返回"不同的采样点占比"（0~1）。

    不用逐像素：云台转一下整幅画面都变，缩图足够区分"动了"和"没动"，
    而噪声（自动曝光、噪点）在缩图后基本被平均掉。
    """
    size = (80, 60)
    ba = a.scaled(*size).convertToFormat(QImage.Format.Format_Grayscale8).bits().tobytes()
    bb = b.scaled(*size).convertToFormat(QImage.Format.Format_Grayscale8).bits().tobytes()
    if not ba or len(ba) != len(bb):
        return 0.0
    changed = sum(1 for x, y in zip(ba, bb, strict=True) if abs(x - y) > 12)
    return changed / len(ba)


def pick_camera(devices: list[dict], did: str) -> dict:
    cams = [d for d in devices if "camera" in str(d.get("model") or "").lower()]
    online = [d for d in cams if d.get("isOnline")]
    if did:
        hit = next((d for d in cams if str(d.get("did")) == did), None)
        if hit is None:
            raise SystemExit(f"没找到 did={did} 的摄像头")
        return hit
    if not online:
        raise SystemExit("账号里没有在线摄像头")
    return online[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="真机验证云台指令是否真的让画面移动")
    parser.add_argument("--go2rtc", required=True, help="带云台接口的自建 go2rtc 路径")
    parser.add_argument("--did", default="", help="指定摄像头 did，默认第一台在线的")
    parser.add_argument("--settle", type=float, default=2.5, help="每条指令之后等画面稳定（秒）")
    args = parser.parse_args()

    binary = Path(args.go2rtc).expanduser()
    if not binary.is_file():
        raise SystemExit(f"找不到 {binary}")
    # 必须在 camera.py 查找二进制之前设好
    os.environ["MIHOME_GO2RTC"] = str(binary)

    api = mijiaAPI()
    dev = pick_camera(api.get_devices_list(), args.did)
    node = SimpleNamespace(
        did=str(dev.get("did") or ""),
        name=str(dev.get("name") or ""),
        model=str(dev.get("model") or ""),
        localip=str(dev.get("localip") or ""),
        online=bool(dev.get("isOnline")),
    )
    print(f"目标：{node.name} / {node.model} / did={node.did} / localip={node.localip or '(无)'}")

    app = QApplication(sys.argv)
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(13)
    app.setFont(font)

    view = CameraView(theme.DARK, theme.category_color("camera"))
    view.resize(436, 220)
    view.show()
    pump(app, 0.3)

    client = SimpleNamespace(raw=lambda: api)
    try:
        url = camera_service().open(client, node)
    except CameraError as exc:
        print(f"取流失败（kind={exc.kind}）：{exc}")
        return 1
    service = camera_service()
    runtime = getattr(service, "_runtime", None)
    port = int(getattr(runtime, "api_port", 0) or 0)
    print(f"rtsp={url}")
    print(f"go2rtc api=127.0.0.1:{port}")
    if not port:
        print("拿不到 go2rtc 的 api 端口，先看看是不是没起来")
        return 1

    print("\n[1] 能力探测（不带参数）")
    caps = ptz_call(port)
    print(f"  {caps}")
    if not caps.get("ok"):
        print("  这个二进制没有云台接口 —— 是不是用了官方构建？")
        return 1

    print("\n[2] 播放并等首帧")
    # toggle() 只发 play_requested，地址要由外层（详情页）取好后交回来
    view.play_requested.connect(lambda: view.play(url))
    view.toggle()
    deadline = time.time() + 25
    while time.time() < deadline and view.state != CameraView.PLAYING:
        app.processEvents()
        time.sleep(0.01)
    if view.state != CameraView.PLAYING:
        print(f"  没出画面，状态 {view.state}，提示 {view._hint!r}")
        return 1
    pump(app, 1.5)  # 让画面稳下来（自动曝光收敛）
    print("  首帧已上屏")

    OUT.mkdir(parents=True, exist_ok=True)
    shots: list[tuple[str, QImage]] = []

    def snap(tag: str) -> QImage:
        pump(app, 0.2)
        img = grab_frames(view)
        shots.append((tag, img))
        img.save(str(OUT / f"ptz_{tag}.png"))
        return img

    print("\n[3] 逐条下发，比前后两帧")
    baseline = snap("00_before")
    last = baseline
    # 左→右、上→下 成对发，尽量回到起始位置（左右行程不对称，回不准是已知的）
    for op in (1, 2, 3, 4):
        reply = ptz_call(port, op, Go2rtcRuntime.stream_name(node.did))
        pump(app, args.settle)
        current = snap(f"{op:02d}_{OPS[op]}")
        moved = diff_ratio(last, current)
        print(
            f"  op={op} {OPS[op]}：接口 {reply.get('ok')}"
            f"{(' ' + str(reply.get('error'))) if reply.get('error') else ''}"
            f"  画面差异 {moved * 100:5.1f}%"
        )
        last = current

    total = diff_ratio(baseline, last)
    print(f"\n  与起始帧相比：{total * 100:.1f}%（成对发完应该接近原位，但行程不对称）")
    print(f"  截图在 {OUT}")

    view.stop()
    pump(app, 0.3)

    moved_any = any(
        diff_ratio(shots[i][1], shots[i + 1][1]) > 0.05 for i in range(len(shots) - 1)
    )
    print("\n结论：" + ("画面确实移动过" if moved_any else "画面没动 —— 指令没生效"))
    return 0 if moved_any else 1


if __name__ == "__main__":
    raise SystemExit(main())
