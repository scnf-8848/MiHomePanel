# SPDX-License-Identifier: GPL-3.0-or-later
"""离屏验证：云台方向盘（不连真机、不发真指令）。

覆盖三段：

1. **几何**：四个方向各自命中哪条指令、中心孔和圆外不响应；
2. **按住连转**：按下立刻发一条；按住超过 450ms 才开始每 220ms 补一条（点一下只有
   一条）、**上一条没回来不补**、松手立刻停、按住时拖到另一侧会换方向、12 秒兜底一定停；
3. **详情页的显隐**：官方 go2rtc（没有 ``/api/xiaomi/ptz``）整块不出现；
   自建构建但没点播放时可见而置灰；播放中才可点。

真实的"画面会不会动"不在这里验——那是 ``scripts/probe_ptz.py``（真机，比像素差）。

用法：
  .venv/Scripts/python.exe -X utf8 scripts/verify_camera_ptz.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mihome.core.camera import PTZ_DOWN, PTZ_LEFT, PTZ_RIGHT, PTZ_UP  # noqa: E402
from mihome.ui import ptz_pad, theme  # noqa: E402
from mihome.ui.ptz_pad import PtzPad  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    if not ok:
        FAILS.append(f"{name}  {detail}")
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {name}{('  ' + detail) if detail else ''}")


def pump(app: QApplication, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


class FakeService:
    """替掉 camera_service()：只记指令，不碰网络。"""

    def __init__(self, delay: float = 0.005) -> None:
        self.calls: list[tuple[str, int]] = []
        self.delay = delay

    def ptz(self, did: str, op: int) -> bool:
        self.calls.append((did, op))
        time.sleep(self.delay)
        return True


def _press(pad: PtzPad, op: int, app: QApplication) -> None:
    """在某个方向上按下（不发松开）。就绪状态由调用方决定。"""
    pt = _point(pad, op)
    ev = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        pt,
        pad.mapToGlobal(pt),
        Qt.LeftButton,
        Qt.LeftButton,
        Qt.NoModifier,
    )
    pad.mousePressEvent(ev)


def _release(pad: PtzPad, app: QApplication) -> None:
    pt = QPointF(pad.width() / 2.0, pad.height() / 2.0)
    ev = QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease,
        pt,
        pad.mapToGlobal(pt),
        Qt.LeftButton,
        Qt.NoButton,
        Qt.NoModifier,
    )
    pad.mouseReleaseEvent(ev)


def _point(pad: PtzPad, op: int) -> QPointF:
    """取某个方向扇区内的一点（沿该方向 0.7R 处）。"""
    cx, cy = pad.width() / 2.0, pad.height() / 2.0
    r = pad.SIZE / 2.0 - 1.0
    dx, dy = {PTZ_UP: (0, -1), PTZ_RIGHT: (1, 0), PTZ_DOWN: (0, 1), PTZ_LEFT: (-1, 0)}[op]
    return QPointF(cx + dx * r * 0.7, cy + dy * r * 0.7)


def test_geometry(app: QApplication) -> None:
    print("\n[1] 几何：落点 → 指令")
    did = "did-1"
    pad = PtzPad(did, theme.DARK, theme.DARK.accent)
    pad.show()
    pump(app, 0.05)

    check("控件是正方形", pad.width() == pad.height() == pad.SIZE, f"{pad.width()}×{pad.height()}")

    for op, name in ((PTZ_UP, "上"), (PTZ_RIGHT, "右"), (PTZ_DOWN, "下"), (PTZ_LEFT, "左")):
        check(f"{name} 命中 op={op}", pad._hit(_point(pad, op)) == op)

    center = QPointF(pad.width() / 2.0, pad.height() / 2.0)
    check("中心孔不响应", pad._hit(center) == 0)
    check("圆外不响应", pad._hit(QPointF(2, 2)) == 0)

    # 45° 边界：落在两个扇区之间时不能返回 0（否则有一小段"点不动"的死角）
    r = pad.SIZE / 2.0 - 1.0
    diag = QPointF(center.x() + r * 0.7 * 0.7071, center.y() - r * 0.7 * 0.7071)
    check("45° 方向也有归属", pad._hit(diag) in (PTZ_UP, PTZ_RIGHT), str(pad._hit(diag)))
    pad.close()


def test_hold(app: QApplication, monkeypatch: FakeService) -> None:
    print("\n[2] 按住连转")
    ptz_pad.camera_service = lambda: monkeypatch  # type: ignore[assignment]

    pad = PtzPad("did-1", theme.DARK, theme.DARK.accent)
    pad.show()
    pad.set_ready(True)
    pump(app, 0.05)

    # 未就绪时按了也不该发
    pad.set_ready(False)
    _press(pad, PTZ_UP, app)
    pump(app, 0.3)
    check("置灰时按下不发指令", not monkeypatch.calls, str(monkeypatch.calls))
    _release(pad, app)
    pad.set_ready(True)

    monkeypatch.calls.clear()
    _press(pad, PTZ_UP, app)
    pump(app, 0.05)
    check("按下立刻发第一条", monkeypatch.calls == [("did-1", PTZ_UP)], str(monkeypatch.calls))

    # 普通点击（按住约 0.3 秒就松开）只能有一条。设备每一步转多少是固件定的
    # （实测约画面宽度的 10–14%），多发一条就是多转一格，用户会直接感觉到
    pump(app, 0.25)
    _release(pad, app)
    pump(app, 0.5)
    check("点一下（0.3 秒）只发一条", len(monkeypatch.calls) == 1, f"{len(monkeypatch.calls)} 条")

    monkeypatch.calls.clear()
    _press(pad, PTZ_UP, app)
    pump(app, 0.67)
    n = len(monkeypatch.calls)
    check("按住 0.67s 才开始补发（先等 450ms）", 2 <= n <= 5, f"共 {n} 条")
    check("补发的都是同一个方向", {op for _, op in monkeypatch.calls} == {PTZ_UP})

    _release(pad, app)
    pump(app, 0.1)
    at_release = len(monkeypatch.calls)
    pump(app, 0.6)
    check("松手后不再发", len(monkeypatch.calls) == at_release, f"{at_release} → {len(monkeypatch.calls)}")

    # 按住时拖到右边：换方向，不用松开重按
    monkeypatch.calls.clear()
    _press(pad, PTZ_UP, app)
    pump(app, 0.05)
    pt = _point(pad, PTZ_RIGHT)
    move = QMouseEvent(
        QMouseEvent.Type.MouseMove, pt, pad.mapToGlobal(pt), Qt.NoButton, Qt.LeftButton, Qt.NoModifier
    )
    pad.mouseMoveEvent(move)
    pump(app, 0.05)
    check("按住拖到另一侧会换方向", monkeypatch.calls[-1] == ("did-1", PTZ_RIGHT), str(monkeypatch.calls[-1:]))
    _release(pad, app)
    pump(app, 0.3)

    # 上一条没回来不补下一条：把一次调用拖到比间隔更长
    slow = FakeService(delay=0.5)
    ptz_pad.camera_service = lambda: slow  # type: ignore[assignment]
    monkeypatch.calls.clear()
    _press(pad, PTZ_LEFT, app)
    pump(app, 0.9)
    check("在飞的时候不叠指令", len(slow.calls) <= 2, f"0.9s 只发了 {len(slow.calls)} 条")
    _release(pad, app)
    pump(app, 0.6)

    # 12 秒兜底：临时改小，验证"收不到松开也会停"
    ptz_pad.camera_service = lambda: monkeypatch  # type: ignore[assignment]
    pad.MAX_HOLD_S = 0.4
    monkeypatch.calls.clear()
    _press(pad, PTZ_UP, app)
    pump(app, 0.9)
    stopped = len(monkeypatch.calls)
    pump(app, 0.5)
    check("超时兜底后停止", len(monkeypatch.calls) == stopped, f"{stopped} → {len(monkeypatch.calls)}")
    check("兜底停的是内部状态", pad._active == 0, str(pad._active))
    pad.stop()
    pad.close()


def test_detail_visibility(app: QApplication) -> None:
    print("\n[3] 详情页显隐（不建整页，只调 _sync_ptz）")
    from mihome.ui.camera_view import CameraView
    from mihome.ui.detail import DetailDialog

    pad = PtzPad("did-1", theme.DARK, theme.DARK.accent)
    pad.show()
    pump(app, 0.05)

    def fake(ok, state=None):
        cam = SimpleNamespace(state=state) if state else None
        return SimpleNamespace(_ptz=pad, _cam_ptz_ok=ok, _camera=cam)

    DetailDialog._sync_ptz(fake(False), None)
    check("官方构建：整块隐藏", pad.isHidden())

    DetailDialog._sync_ptz(fake(None), None)
    check("还没探测：先不出现", pad.isHidden())

    DetailDialog._sync_ptz(fake(True, CameraView.IDLE), None)
    check("自建构建 + 没播放：可见", not pad.isHidden())
    check("但不可点（置灰）", not pad._ready)

    DetailDialog._sync_ptz(fake(True, CameraView.PLAYING), None)
    check("播放中：可点", pad._ready)

    DetailDialog._sync_ptz(fake(True, CameraView.PAUSED), None)
    check("暂停仍然可点（画面还在）", pad._ready)

    DetailDialog._sync_ptz(fake(True, CameraView.ERROR), None)
    check("出错：重新置灰", not pad._ready)
    pad.close()


def main() -> int:
    app = QApplication(sys.argv)
    fake = FakeService()
    test_geometry(app)
    test_hold(app, fake)
    test_detail_visibility(app)
    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败：")
        for line in FAILS:
            print("  - " + line)
        return 1
    print(f"全部通过（{len(CHECKS)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
