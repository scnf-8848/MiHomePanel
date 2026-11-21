# SPDX-License-Identifier: GPL-3.0-or-later
"""真机探针：**一条** 0x112 指令到底让画面挪多少（估算百分比）。

界面上的抱怨是"点一下转太多"。要知道是"一条指令本身就转得多"还是"一次点击发了
好几条"，得先把单条的位移量量出来：

1. 取一帧做基准；
2. 发 **1 条** op=1（左），等画面稳定，再截一帧，用列灰度剖面的 SAD 找出最佳水平
   位移（缩到 1/5 宽再搜，得到像素后乘回去）；
3. 再连发 2 条（共 3 条），量一次——若位移大致线性，说明每条是固定步长；
4. 用同样条数的 op=2（右）转回去，尽量回到起始位置。

输出的百分比是**画面宽度的占比**，可直接对人眼感受：10% 大约是一屏的十分之一。

用法：
  .venv/Scripts/python.exe -X utf8 scripts/probe_ptz_step.py --go2rtc <自建 go2rtc.exe>
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mijiaAPI import mijiaAPI  # noqa: E402
from PySide6.QtGui import QFont, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mihome.core.camera import CameraError, Go2rtcRuntime  # noqa: E402
from mihome.core.camera import shared as camera_service  # noqa: E402
from mihome.ui import theme  # noqa: E402
from mihome.ui.camera_view import CameraView  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_ptz import OUT, pick_camera, pump, ptz_call  # noqa: E402

#: 分析用的宽度：原图 2960 宽，缩到 1/10 够判 0.3% 的位移，也够快（纯 Python 搜索）
ANALYZE_W = 296


def column_profile(img: QImage) -> list[float]:
    """按列求灰度均值，得到一条宽度剖面（水平平移会让整条剖面整体移动）。"""
    h = max(1, round(img.height() * ANALYZE_W / img.width()))
    small = img.scaled(ANALYZE_W, h).convertToFormat(QImage.Format.Format_Grayscale8)
    data = small.bits().tobytes()
    stride = small.bytesPerLine()
    cols = [0.0] * ANALYZE_W
    for y in range(h):
        row = data[y * stride:y * stride + ANALYZE_W]
        for x, v in enumerate(row):
            cols[x] += v
    return [c / h for c in cols]


def best_shift(a: list[float], b: list[float], limit: int = 60) -> tuple[int, float]:
    """在 ±limit 内找让 b 对齐 a 的位移（b 相对 a 往右为正），返回 (位移, 残差)。"""
    best = (0, float("inf"))
    n = len(a)
    for s in range(-limit, limit + 1):
        lo = max(0, -s)
        hi = min(n, n - s)
        if hi - lo < n // 3:
            continue
        cost = 0.0
        for x in range(lo, hi):
            cost += abs(a[x] - b[x + s])
        cost /= hi - lo
        if cost < best[1]:
            best = (s, cost)
    return best


def shift_percent(before: QImage, after: QImage, full_width: int) -> tuple[float, float]:
    """返回 (位移占画面宽的百分比, 残差)。"""
    a = column_profile(before)
    b = column_profile(after)
    s, cost = best_shift(a, b)
    return s / ANALYZE_W * 100.0, cost


def main() -> int:
    parser = argparse.ArgumentParser(description="量一条云台指令的画面位移")
    parser.add_argument("--go2rtc", required=True, help="带云台接口的自建 go2rtc 路径")
    parser.add_argument("--did", default="", help="指定摄像头 did，默认第一台在线的")
    parser.add_argument("--settle", type=float, default=2.2, help="每条指令之后等画面稳定（秒）")
    args = parser.parse_args()

    binary = Path(args.go2rtc).expanduser()
    if not binary.is_file():
        raise SystemExit(f"找不到 {binary}")
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
    print(f"目标：{node.name} / {node.model} / did={node.did}")

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
    runtime = getattr(camera_service(), "_runtime", None)
    port = int(getattr(runtime, "api_port", 0) or 0)
    if not port:
        print("拿不到 go2rtc 的 api 端口")
        return 1

    view.play_requested.connect(lambda: view.play(url))
    view.toggle()
    deadline = time.time() + 25
    while time.time() < deadline and view.state != CameraView.PLAYING:
        app.processEvents()
        time.sleep(0.01)
    if view.state != CameraView.PLAYING:
        print(f"没出画面，状态 {view.state}")
        return 1
    pump(app, 1.8)

    OUT.mkdir(parents=True, exist_ok=True)
    name = Go2rtcRuntime.stream_name(node.did)

    def snap(tag: str) -> QImage:
        pump(app, 0.25)
        img = view.grab().toImage()
        img.save(str(OUT / f"ptzstep_{tag}.png"))
        return img

    base = snap("0_base")
    print("\n[1] 单条 op=1（左）")
    print(f"  {ptz_call(port, 1, name)}")
    pump(app, args.settle)
    one = snap("1_one")
    pct1, cost1 = shift_percent(base, one, base.width())
    print(f"  画面位移 {abs(pct1):.1f}%（残差 {cost1:.1f}）")

    print("\n[2] 连发 3 条 op=1，看是否线性")
    for _ in range(3):
        ptz_call(port, 1, name)
        pump(app, 0.45)
    pump(app, args.settle)
    three = snap("2_three")
    pct3, cost3 = shift_percent(base, three, base.width())
    print(f"  画面位移 {abs(pct3):.1f}%（残差 {cost3:.1f}）")
    if abs(pct1) > 0.2:
        print(f"  平均每条 ≈ {abs(pct3) / 4:.1f}%（单条 {abs(pct1):.1f}%）")

    print("\n[3] 用同样条数转回去（op=2 右）")
    for _ in range(4):
        ptz_call(port, 2, name)
        pump(app, 0.45)
    pump(app, args.settle)
    back = snap("3_back")
    pct_back, _ = shift_percent(base, back, base.width())
    print(f"  与起始帧相比还差 {abs(pct_back):.1f}%")

    view.stop()
    pump(app, 0.3)
    print(f"\n截图在 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
