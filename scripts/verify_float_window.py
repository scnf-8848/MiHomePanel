# SPDX-License-Identifier: LGPL-3.0-or-later
"""浮窗形态：外圈阴影 / 面板几何 / 点击层挖洞 / 各窗口内容尺寸不被挤。

用法：``PYTHONPATH=. .venv/Scripts/python.exe -X utf8 scripts/verify_float_window.py``

为什么值得单独测：浮窗的坑全在"窗口比面板大一圈"这件事上——内容要是还挂在
窗口的布局上，就白白少掉两圈留白（小屏上直接挤掉一行）；点击层要是还按
``frameGeometry`` 挖洞，点阴影会当成"点面板里"，看着就是不关窗。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QPoint, QRect, Signal  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget  # noqa: E402

from mihome.core.automation import RuleSet  # noqa: E402
from mihome.ui import chrome, theme  # noqa: E402
from mihome.ui.confirm import ConfirmDialog  # noqa: E402
from mihome.ui.detail import DetailDialog  # noqa: E402
from mihome.ui.settings import SettingsDialog  # noqa: E402
import verify_card_style as V  # noqa: E402

FAILS: list[str] = []
N = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global N
    N += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(f"{name} {detail}")


class Store(V.LooseStore):
    """设置页要的方法，``LooseStore.__getattr__`` 会返回 None（循环直接崩）。"""

    def homes(self):
        return [SimpleNamespace(name="我的家", display_name="我的家")]

    def room_names(self, home: str) -> list[str]:
        return ["客厅"]

    def device_states(self, home: str, room: str):
        return list(self.devices.values())

    def home_devices(self, home: str):
        return list(self.devices.values())

    def scenes_for(self, home: str, include_hidden: bool = False):
        return []


class FakeService(QObject):
    log_added = Signal(object)
    status_changed = Signal()
    rules_changed = Signal()

    def __init__(self, path) -> None:
        super().__init__()
        self.rules = RuleSet(path)
        self.engine = None

    def status_for(self, keys: str) -> str:
        return "已注册" if keys else "未设置"

    def target_label(self) -> str:
        return "家庭中枢"

    def sync(self) -> None:
        pass


def plain_window(pal: theme.Palette, shadow: int,
                 radius: int = theme.RADIUS_CARD) -> tuple[QWidget, chrome.WindowFrame]:
    win = QWidget()
    win.resize(400, 300)
    frame = chrome.WindowFrame(
        win, resizable=True, radius=radius, shadow=shadow,
        shadow_alpha=chrome.shadow_alpha(pal),
        fill=lambda: chrome.surface_bg(pal),
        border=lambda: chrome.surface_border(pal),
    )
    lay = QVBoxLayout(frame.body)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(chrome.TitleBar("浮窗", pal, buttons=("close",),
                                  variant="dialog", frame=frame))
    lay.addWidget(QLabel("正文"))
    win.show()
    QApplication.processEvents()
    return win, frame


def main() -> int:
    app = QApplication(sys.argv[:1])  # noqa: F841
    tmp = Path(tempfile.mkdtemp(prefix="mihome-float-"))
    prefs = V.fresh_prefs(tmp)
    store = Store(prefs, [V.make_state(f"d{i}") for i in range(4)])

    print("1) 几何：面板 = 窗口内缩一圈留白")
    check("floating_size 加两圈",
          chrome.floating_size(480, 700) == (480 + 2 * chrome.SHADOW_PAD,
                                             700 + 2 * chrome.SHADOW_PAD),
          str(chrome.floating_size(480, 700)))
    win, frame = plain_window(theme.LIGHT, chrome.SHADOW_PAD)
    pad = chrome.SHADOW_PAD
    check("shadow_pad 就是传进去的值", frame.shadow_pad == pad, str(frame.shadow_pad))
    check("panel_rect 内缩", frame.panel_rect() == QRect(pad, pad,
                                                         win.width() - 2 * pad,
                                                         win.height() - 2 * pad),
          str(frame.panel_rect()))
    check("body 跟面板同几何", frame.body.geometry() == frame.panel_rect(),
          f"{frame.body.geometry()} vs {frame.panel_rect()}")
    # 反向：不传 shadow 的老形态不能动（camera_popout / 主窗口都靠这个）
    win0, frame0 = plain_window(theme.LIGHT, 0)
    check("shadow=0 时 panel_rect 就是整窗", frame0.panel_rect() == win0.rect(),
          str(frame0.panel_rect()))
    check("shadow=0 时没有 body（内容还挂在窗口上）", frame0.body is None)

    print("\n2) 阴影位图：贴边浓、外圈淡、最外不等于 0")
    img = frame._shadow_img
    check("阴影图有缓存", img is not None)
    mid = win.height() // 2
    # 阴影图是半分辨率（``_build_shadow`` 里 ratio = dpr * 0.5），取样要按比例换算，
    # 并且取 3×3 平均——单像素在半分辨率上是模糊过的，会抖 ±1 层
    sx = img.width() / max(1, win.width())

    def band(x: int, y: int) -> float:
        vals = [QColor(img.pixelColor(min(img.width() - 1, max(0, int((x + dx) * sx))),
                                      min(img.height() - 1, max(0, int((y + dy) * sx))))
                      ).alpha()
                for dx in (-1, 0, 1) for dy in (-1, 0, 1)]
        return sum(vals) / len(vals)

    near = band(pad - 1, mid)
    half = band(pad // 2, mid)
    out = band(0, mid)
    check("贴面板处最浓", near > half, f"贴边 {near} / 半程 {half}")
    check("半程还有淡影（过渡是渐变的）", half > 0, str(half))
    live = sum(1 for x in range(2, pad) if band(x, mid) > 0)
    check("外圈有连续若干层非零（不是一刀切）", live >= pad // 3, f"{live}/{pad - 2}")
    check("窗口最外一圈几乎透明（阴影没被硬切）", out <= 4, str(out))
    check("浓度跟着主题走", chrome.shadow_alpha(theme.DARK) > chrome.shadow_alpha(theme.LIGHT),
          f"{chrome.shadow_alpha(theme.DARK)} vs {chrome.shadow_alpha(theme.LIGHT)}")
    want = int(chrome.shadow_alpha(theme.LIGHT) * 255)
    check("贴边浓度接近设定值", abs(near - want) <= 20, f"{near:.0f} vs {want}")

    print("\n3) 缓存：不动就不重建，改了才重建")
    key_before = frame._shadow_key
    img_before = frame._shadow_img
    win.repaint()
    app.processEvents()
    check("同尺寸重绘不重建", frame._shadow_key == key_before
          and frame._shadow_img is img_before)
    win.resize(460, 340)
    app.processEvents()
    win.repaint()
    check("resize 后重建", frame._shadow_key != key_before)
    frame.set_shadow_alpha(0.1)
    check("改浓度清缓存", frame._shadow_key is None)

    print("\n4) 画面：四角透明 / 描边压在面板边上")
    win2, frame2 = plain_window(theme.LIGHT, pad)
    img2 = win2.grab().toImage()
    corners = [(0, 0), (img2.width() - 1, 0), (0, img2.height() - 1),
               (img2.width() - 1, img2.height() - 1)]
    check("四个窗口角是透明的", all(QColor(img2.pixelColor(x, y)).alpha() < 40
                                    for x, y in corners),
          str([QColor(img2.pixelColor(x, y)).alpha() for x, y in corners]))
    # 面板左边缘（x=pad）应当是描边色，而不是"什么都没有"
    edge = QColor(img2.pixelColor(pad, img2.height() // 2))
    check("面板边缘有描边", edge.alpha() > 200
          and edge.name().upper() != QColor(chrome.surface_bg(theme.LIGHT)).name().upper(),
          f"{edge.name()} a={edge.alpha()}")

    print("\n5) 标题栏半径跟着窗口走（18 的浮窗不能还用 12）")
    bar = win2.findChild(chrome.TitleBar)
    check("TitleBar 用 frame.radius", bar._radius == frame2.radius,
          f"{bar._radius} vs {frame2.radius}")
    check("浮窗半径是 18（卡片档）", frame2.radius == theme.RADIUS_CARD, str(frame2.radius))

    print("\n6) 点击层：阴影算「外侧」，面板算「里面」")
    dlg = ConfirmDialog("确认", "正文", theme.LIGHT, None)
    dlg.show()
    app.processEvents()
    shield = chrome.DimClickShield(dlg)
    shield.show()
    app.processEvents()
    geo = dlg.frameGeometry()
    hit = chrome.DimClickShield._hit_rect(dlg)
    check("挖洞矩形 = 面板（不是整个窗口）", hit.width() == dlg._frame.panel_rect().width(),
          f"{hit} vs {dlg._frame.panel_rect()}")
    check("洞从面板左上角起算", hit.topLeft() == geo.topLeft() + dlg._frame.panel_rect().topLeft(),
          f"{hit.topLeft()} vs {geo.topLeft()}")
    shadow_pt = geo.topLeft() + QPoint(4, 4)
    check("点阴影 = 不算点进弹窗", not shield._over_dialog(shadow_pt), str(shadow_pt))
    center = hit.center()
    check("点面板 = 算点进弹窗", shield._over_dialog(center), str(center))

    print("\n7) 各窗口：面板尺寸仍等于设计的内容尺寸")
    cases = [
        ("详情", DetailDialog(V.make_state(), store, theme.LIGHT), (480, 700)),
        ("设置", SettingsDialog(store, theme.LIGHT), (820, 680)),
    ]
    for name, d, want in cases:
        d.show()
        app.processEvents()
        got = d._frame.panel_rect()
        check(f"{name}窗面板 {want[0]}x{want[1]}", (got.width(), got.height()) == want,
              f"{got.width()}x{got.height()}")
        check(f"{name}窗总尺寸多两圈", d.width() == want[0] + 2 * pad,
              f"{d.width()} vs {want[0] + 2 * pad}")

    confirm_dlg = ConfirmDialog("t", "正文", theme.LIGHT, None)
    confirm_dlg.show()
    app.processEvents()
    # 400 是 confirm 的最小宽度（内容），浮窗要在此之上加留白
    check("确认框宽度按内容算再补留白",
          confirm_dlg._frame.panel_rect().width() >= 400, str(confirm_dlg.width()))
    check("确认框没有 layout 挂在窗口上", confirm_dlg.layout() is None)
    check("确认框的 layout 挂在 body 上", confirm_dlg._frame.body.layout() is not None)

    # 登录窗一构造就去拉二维码（要网络），只查源码：固定尺寸必须走 floating_size
    login_src = Path("mihome/ui/login.py").read_text(encoding="utf-8")
    check("登录窗固定尺寸也走 floating_size",
          "setFixedSize(*floating_size(380, 508))" in login_src)

    svc = FakeService(tmp / "rules.json")
    from mihome.ui.automation import AutomationDialog
    from mihome.ui.automation_list import AutomationListDialog
    auto = AutomationListDialog(svc, theme.LIGHT)
    auto.show()
    app.processEvents()
    check("列表窗面板 640x580（离屏短屏会被 fit_height 压，这里按内容尺寸传）",
          auto._frame.panel_rect().width() == 640, str(auto._frame.panel_rect()))
    editor = AutomationDialog(svc, theme.LIGHT, None)
    editor.show()
    app.processEvents()
    check("编辑窗面板宽 860", editor._frame.panel_rect().width() == 860,
          str(editor._frame.panel_rect().width()))
    check("编辑窗最小尺寸也补了留白",
          editor.minimumWidth() == 760 + 2 * pad, str(editor.minimumWidth()))

    print("\n8) 限高要扣掉留白（否则小屏顶出任务栏）")
    from mihome.ui.automation_style import fit_height
    plain = fit_height(900)
    reserved = fit_height(900, reserve=2 * pad)
    check("reserve 真的扣了", reserved <= plain and plain - reserved in (0, 2 * pad),
          f"{plain} → {reserved}")

    print()
    if FAILS:
        print(f"合计 {N} 项，失败 {len(FAILS)} 项")
        for line in FAILS:
            print("  !! " + line)
        return 1
    print(f"合计 {N} 项，全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
