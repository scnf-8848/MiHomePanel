# SPDX-License-Identifier: GPL-3.0-or-later
"""独立窗口里的云台方向盘：离屏验证（不连网、不需要设备）。

重点四件事：

- **两种形态**：详情页 132 px 不透明、进布局；独立窗口 96 px 半透明、叠在画面上。
  四角必须真的透出去，否则会在画面左下角糊出一块方块。
- **位置**：贴画面左下角，与画面里那排圆钮的 12 px 内边距对齐；缩放窗口要跟着走。
- **显隐**：没画面（IDLE/ERROR）不露，播放/暂停露出且可点；官方 go2rtc 构建整块不建。
- **收尾**：关窗 / 收回详情页时停掉连转，别让定时器继续往摄像头发指令。

跑法：``.venv\\Scripts\\python.exe -X utf8 scripts\\verify_camera_ptz_popout.py``
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_SCALE_FACTOR", "1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget  # noqa: E402

from mihome.ui import theme  # noqa: E402
from mihome.ui.camera_popout import PTZ_MARGIN, CameraPopoutWindow  # noqa: E402
from mihome.ui.camera_view import CameraView  # noqa: E402
from mihome.ui.ptz_pad import PtzPad  # noqa: E402

FAILS: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {got!r}")
    if not ok:
        FAILS.append(f"{name} → 期望 {want!r}，实际 {got!r}")


def check_true(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(f"{name} {detail}")


def pump(app: QApplication, seconds: float = 0.05) -> None:
    QTest.qWait(int(seconds * 1000))


def make_popup(app: QApplication, did: str = "dev1") -> tuple[QWidget, CameraView, CameraPopoutWindow]:
    host = QWidget()
    host.resize(560, 400)
    lay = QVBoxLayout(host)
    lay.addWidget(QLabel("实时画面"))
    view = CameraView(theme.DARK, "#3B82F6")
    view.resize(520, 220)
    lay.addWidget(view)
    host.show()
    app.processEvents()
    view._set_state(CameraView.PLAYING)
    popup = CameraPopoutWindow(
        view, host, theme.DARK, "测试摄像头", did=did, accent="#3B82F6"
    )
    # 别真的去问 go2rtc（离屏环境里没有 go2rtc，会走 0.8s 超时）
    popup._probe_ptz = lambda: None  # type: ignore[method-assign]
    popup.present()
    app.processEvents()
    return host, view, popup


def _render(pad: PtzPad) -> QImage:
    img = QImage(pad.width(), pad.height(), QImage.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    pad.render(img)
    return img


def test_shapes(app: QApplication) -> None:
    print("\n-- 两种形态 --")
    big = PtzPad("dev1", theme.DARK, "#3B82F6")
    small = PtzPad("dev1", theme.DARK, "#3B82F6", overlay=True)
    check("详情页形态 132", big.diameter, PtzPad.SIZE)
    check("叠加形态 104", small.diameter, PtzPad.OVERLAY_SIZE)
    check_true("叠加形态真的更小", small.width() < big.width(), f"{small.width()} < {big.width()}")
    check("叠加形态真的偏小（不超过 110）", small.diameter <= 110, True)
    check_true("叠加形态要透明底", small.testAttribute(Qt.WA_TranslucentBackground))
    check_true("详情页形态不动（保持原样）", not big.testAttribute(Qt.WA_TranslucentBackground))
    big.close()
    small.close()


def test_translucent(app: QApplication) -> None:
    print("\n-- 半透明：四角透出去、盘面不挡画面 --")
    pad = PtzPad("dev1", theme.DARK, "#3B82F6", overlay=True)
    pad.set_ready(True)
    img = _render(pad)
    w, h = pad.width(), pad.height()
    check("左上角完全透明", img.pixelColor(1, 1).alpha(), 0)
    check("右上角完全透明", img.pixelColor(w - 2, 1).alpha(), 0)

    # 45° 方向 0.5r 处：圆内、两根箭头之间，只能看到半透明黑底盘
    face = img.pixelColor(int(w / 2 + h * 0.25), int(h / 2 - h * 0.25))
    check_true(
        "盘面是半透明（能看见底下的画面）",
        0 < face.alpha() < 255,
        f"alpha={face.alpha()}",
    )
    check_true("盘面偏暗（黑底压住亮画面）", face.red() < 40, f"rgb=({face.red()},{face.green()},{face.blue()})")

    # "上"箭头中心：可点时是亮的白色，不能点时压暗
    arrow = img.pixelColor(w // 2, 16)
    check_true("可用时箭头是亮的", arrow.red() > 200 and arrow.alpha() > 200,
               f"rgb={arrow.red()} alpha={arrow.alpha()}")

    idle = PtzPad("dev1", theme.DARK, "#3B82F6", overlay=True)
    idle_img = _render(idle)
    idle_arrow = idle_img.pixelColor(w // 2, 16)
    check_true("不可用时箭头变暗", idle_arrow.red() < 200 and idle_arrow.alpha() < 200,
               f"rgb={idle_arrow.red()} alpha={idle_arrow.alpha()}")
    pad.close()
    idle.close()


def test_place(app: QApplication) -> None:
    print("\n-- 位置：贴画面左下角 --")
    host, view, popup = make_popup(app)
    pad = popup._ptz
    check_true("建了方向盘", pad is not None)
    if pad is None:
        host.close()
        return

    pad.set_ready(True)
    popup._ptz_ok = True
    popup._sync_ptz()
    pump(app)

    geo = view.geometry()
    check("有画面 → 露出", pad.isVisible(), True)
    check("贴左边（12px）", pad.x(), geo.left() + PTZ_MARGIN)
    check(
        "贴底部（12px）",
        pad.y(),
        geo.top() + geo.height() - pad.height() - PTZ_MARGIN,
    )
    check_true("整块落在画面里", geo.contains(pad.geometry()), f"{pad.geometry()} ⊂ {geo}")
    check_true(
        "在左半边（不压右下角那排键）",
        pad.geometry().center().x() < geo.center().x(),
        f"{pad.geometry().center().x()} < {geo.center().x()}",
    )
    check("父控件是独立窗口（叠在画面上，不进布局）", pad.parent() is popup, True)

    print("\n-- 缩放窗口要跟着走 --")
    popup.resize(980, 660)
    pump(app)
    geo2 = view.geometry()
    check("缩放后仍贴左", pad.x(), geo2.left() + PTZ_MARGIN)
    check(
        "缩放后仍贴底",
        pad.y(),
        geo2.top() + geo2.height() - pad.height() - PTZ_MARGIN,
    )
    check_true("缩放后仍在画面内", geo2.contains(pad.geometry()))

    print("\n-- 显隐跟着画面状态 --")
    view._set_state(CameraView.IDLE)
    pump(app)
    check("没画面 → 藏起来", pad.isVisible(), False)
    check("没画面 → 不可点", pad._ready, False)

    view._set_state(CameraView.PAUSED)
    pump(app)
    check("暂停仍露出（producer 还在）", pad.isVisible(), True)
    check("暂停可点", pad._ready, True)

    view._set_state(CameraView.PLAYING)
    pump(app)
    popup._ptz_ok = False
    popup._sync_ptz()
    pump(app)
    check("官方构建（没有这条路由）→ 整块不露", pad.isVisible(), False)
    popup._ptz_ok = True
    popup._sync_ptz()
    pump(app)
    check("自建构建 → 又回来", pad.isVisible(), True)

    print("\n-- 收起 / 关窗要停手 --")
    pad._active = 3
    pad._timer.start(50)
    popup.close()
    pump(app)
    check("关窗后定时器停", pad._timer.isActive(), False)
    check("关窗后不再有方向", pad._active, 0)
    check("收回详情页后藏起来", pad.isVisible(), False)
    host.close()


def test_no_did(app: QApplication) -> None:
    print("\n-- 没有 did 就不建（拿不到该发给谁）--")
    host, _view, popup = make_popup(app, did="")
    check("没有方向盘", popup._ptz, None)
    popup.close()
    host.close()


def test_probe_signal(app: QApplication) -> None:
    print("\n-- 探测结果回到 UI 线程才生效 --")
    host, view, popup = make_popup(app)
    pad = popup._ptz
    if pad is None:
        host.close()
        return
    check("还没探测 → 先藏着", pad.isVisible(), False)
    popup._ptz_probed.emit(True)
    pump(app)
    check("探测到支持 → 露出", pad.isVisible(), True)
    check("此时可点（画面在播）", pad._ready, True)
    popup._ptz_probed.emit(False)
    pump(app)
    check("探测到不支持 → 收起", pad.isVisible(), False)
    popup.close()
    host.close()


def test_reopen(app: QApplication) -> None:
    print("\n-- 关掉再打开同一个窗口（详情页会复用它）--")
    host, _view, popup = make_popup(app)
    pad = popup._ptz
    if pad is None:
        host.close()
        return
    popup._ptz_ok = True
    popup._sync_ptz()
    pump(app)
    popup.close()
    pump(app)
    check("关窗后 _closing 为 True", popup._closing, True)
    popup.present()
    pump(app)
    check("再次打开时 _closing 复位（否则第二次出错不会自动关窗）", popup._closing, False)
    check("方向盘跟着回来", pad.isVisible(), True)
    check("仍然可点", pad._ready, True)
    popup.close()
    host.close()


def main() -> int:
    app = QApplication(sys.argv)
    test_shapes(app)
    test_translucent(app)
    test_place(app)
    test_no_did(app)
    test_probe_signal(app)
    test_reopen(app)
    print(f"\n结果：{len(FAILS)} 项失败" if FAILS else "\n全部通过")
    for f in FAILS:
        print(" -", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
