# SPDX-License-Identifier: GPL-3.0-or-later
"""摄像头独立播放窗口的离屏验证（不连网、不需要设备）。

重点不是"窗口能弹出来"，而是**弹出/收回有没有重新取流**：

- 同一个 ``CameraView`` 控件被搬来搬去，``QMediaPlayer`` 对象身份不变；
- 搬动前后往 ``QVideoSink`` 里喂帧，帧号继续增长（渲染面换了，管道没换）；
- 不调用 ``play/pause/stop/setSource``，也不碰 ``core.camera`` 的会话；
- 收起后回到详情页原布局位置，尺寸策略恢复 220px；
- 窗口带右下角缩放手柄，手动拖拽真的能改大小；
- 独立窗口是弹窗的直接子窗口，``DimClickShield`` 会把它算成"洞"而不是压暗区。

跑法：``.venv\\Scripts\\python.exe -X utf8 scripts\\verify_camera_popout.py``
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_SCALE_FACTOR", "1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPoint, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from mihome.ui import theme  # noqa: E402
from mihome.ui.camera_popout import CameraPopoutWindow  # noqa: E402
from mihome.ui.camera_view import CameraView, playbacks  # noqa: E402
from mihome.ui.chrome import DimClickShield, _ResizeEdge  # noqa: E402

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


class FakePlayer:
    """只用来证明"没有重新碰播放器"。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        #: 假的音轨状态：真实播放器要到 LoadedMedia 之后 hasAudio() 才为真，
        #: 这里默认 False，需要测喇叭的用例自己打开
        self.has_audio = False

    def hasAudio(self) -> bool:  # noqa: N802
        return self.has_audio

    def play(self) -> None:
        self.calls.append("play")

    def pause(self) -> None:
        self.calls.append("pause")

    def stop(self) -> None:
        self.calls.append("stop")

    def setSource(self, url) -> None:  # noqa: N802
        self.calls.append("setSource")


class FakeFrame:
    def __init__(self, value: int) -> None:
        self._img = QImage(32, 18, QImage.Format_RGB32)
        self._img.fill(value)

    def isValid(self) -> bool:  # noqa: N802
        return True

    def toImage(self):  # noqa: N802
        return self._img


def make_view(compact: bool = False) -> CameraView:
    v = CameraView(theme.LIGHT, "#3B82F6", compact=compact)
    v.resize(480, 220)
    v.show()
    return v


def test_buttons(app: QApplication) -> None:
    print("\n-- 按钮范围 --")
    wall = make_view(compact=True)
    check("墙面 compact 卡没有独立窗口键", wall._pop_btn, None)
    wall.close()

    detail = make_view()
    check_true("详情页有独立窗口键", detail._pop_btn is not None)
    check("IDLE 时不可见", detail._pop_btn.isVisible(), False)
    detail._hover = True
    detail._set_state(CameraView.PLAYING)
    app.processEvents()
    check("播放 + 悬停时可见", detail._pop_btn.isVisible(), True)
    detail._hover = False
    detail._refresh_buttons()
    check("播放但没悬停 → 隐藏（与暂停/喇叭同一条规则）",
          detail._pop_btn.isVisible(), False)
    detail._hover = True
    detail._set_state(CameraView.PAUSED)
    app.processEvents()
    check("暂停时仍可见", detail._pop_btn.isVisible(), True)
    detail._set_state(CameraView.ERROR)
    app.processEvents()
    check("出错时隐藏", detail._pop_btn.isVisible(), False)

    seen: list[bool] = []
    detail.popout_requested.connect(lambda: seen.append(True))
    detail._set_state(CameraView.PLAYING)
    detail._pop_btn.clicked.emit()
    check("点按钮发出 popout_requested", len(seen), 1)
    detail.close()


def test_move_without_reconnect(app: QApplication) -> None:
    print("\n-- 搬窗口不重连 --")
    host = QWidget()
    host.resize(520, 360)
    lay = QVBoxLayout(host)
    lay.addWidget(QLabel("实时画面"))
    view = CameraView(theme.LIGHT, "#3B82F6")
    view.resize(480, 220)
    lay.addWidget(view)
    host.show()
    app.processEvents()

    fake = FakePlayer()
    view._player = fake
    view._set_state(CameraView.PLAYING)
    before_frames = view._frames
    view._on_frame(FakeFrame(0x336699))
    app.processEvents()
    check("搬之前能收到帧", view._frames, before_frames + 1)
    check("搬之前有图", view._image is not None, True)
    host_index = lay.indexOf(view)

    popup = CameraPopoutWindow(view, host, theme.LIGHT, "测试摄像头")
    check("初始是独立顶层窗口", bool(popup.windowFlags() & Qt.Window), True)
    popup.present()
    app.processEvents()

    check_true("画面控件已搬进独立窗口", view.parent() is popup)
    check("独立窗口可见", popup.isVisible(), True)
    check_true("详情页原位换成占位", lay.itemAt(host_index).widget() is not view)
    check("播放器还是同一个对象", view._player is fake, True)
    check("搬窗口没有碰播放器", fake.calls, [])
    check("播放状态没变", view._state, CameraView.PLAYING)
    check("独立窗口里控件被撑开", view.height() > 220, True)
    check("独立窗口按 contain 显示", view._contain, True)

    view._on_frame(FakeFrame(0x993366))
    app.processEvents()
    check("搬之后帧继续进来", view._frames, before_frames + 2)
    check("搬之后仍有图", view._image is not None, True)

    popup.close()
    app.processEvents()
    check_true("关闭独立窗口后回到详情页", view.parent() is host)
    check_true("回到原来的布局位置", lay.itemAt(host_index).widget() is view)
    check("独立窗口已隐藏", popup.isVisible(), False)
    check("收回后仍是同一个播放器", view._player is fake, True)
    check("收回没有碰播放器", fake.calls, [])
    check("收回恢复 220 高", view.height(), 220)
    check("收回详情页恢复 cover", view._contain, False)

    view._on_frame(FakeFrame(0x669933))
    app.processEvents()
    check("收回后帧仍在继续", view._frames, before_frames + 3)
    host.close()


def test_resize_grip(app: QApplication) -> None:
    print("\n-- 右下角缩放 --")
    host = QWidget()
    host.resize(400, 300)
    lay = QVBoxLayout(host)
    view = CameraView(theme.LIGHT, "#3B82F6")
    lay.addWidget(view)
    host.show()
    view._set_state(CameraView.PLAYING)

    popup = CameraPopoutWindow(view, host, theme.DARK, "测试摄像头")
    popup.present()
    popup.resize(800, 500)
    app.processEvents()

    corners = [
        e for e in popup.findChildren(_ResizeEdge)
        if e._edges == (Qt.RightEdge | Qt.BottomEdge)
    ]
    check("有右下角缩放手柄", len(corners), 1)
    if corners:
        edge = corners[0]
        w0, h0 = popup.width(), popup.height()
        c = edge.rect().center()
        QTest.mousePress(edge, Qt.LeftButton, Qt.NoModifier, c)
        QTest.mouseMove(edge, c + QPoint(120, 80))
        QTest.mouseRelease(edge, Qt.LeftButton, Qt.NoModifier, c + QPoint(120, 80))
        app.processEvents()
        check_true("拖右下角能放大", popup.width() > w0 and popup.height() > h0,
                   f"{w0}x{h0} → {popup.width()}x{popup.height()}")
    popup.close()
    host.close()


def test_dim_shield_hole(app: QApplication) -> None:
    print("\n-- 与遮罩点击的边界 --")
    main = QWidget()
    main.resize(900, 600)
    main.move(0, 0)
    main.show()
    dlg = QDialog(main)
    dlg.resize(400, 300)
    dlg.move(300, 150)
    dlg.show()
    app.processEvents()

    shield = DimClickShield(dlg)
    shield.show()
    app.processEvents()

    popup = QWidget(dlg, Qt.Window)
    popup.setGeometry(40, 40, 180, 120)
    popup.show()
    app.processEvents()
    shield._place()

    hole = popup.frameGeometry().translated(-shield.x(), -shield.y())
    check("独立窗口区域不在遮罩点击区", shield.mask().contains(hole.center()), False)
    check("压暗区仍可点", shield.mask().contains(QPoint(20, 20)), True)
    check("独立窗口算「层内」",
          shield._over_dialog(popup.mapToGlobal(popup.rect().center())), True)

    popup.close()
    dlg.close()
    shield.hide()
    main.close()


def test_contain_fit(app: QApplication) -> None:
    """独立窗口 contain：整幅可见、上下留黑边；内嵌视图仍是 cover。"""
    print("\n-- 独立窗口 contain --")
    view = CameraView(theme.LIGHT, "#3B82F6")
    view.resize(200, 200)
    view.show()
    app.processEvents()
    img = QImage(100, 50, QImage.Format_RGB32)
    img.fill(QColor("#FF0000"))
    view._image = img
    view._hover = False
    view._set_state(CameraView.PLAYING)

    view.set_contain_fit(True)
    app.processEvents()
    view._hover = False
    canvas = QImage(200, 200, QImage.Format_ARGB32)
    canvas.fill(Qt.transparent)
    view.render(canvas)
    top = canvas.pixelColor(100, 10)
    mid = canvas.pixelColor(100, 100)
    check("contain 上边留黑边", top.name(), view.video_bg.lower())
    check("黑边不透明", top.alpha(), 255)
    check("contain 画面完整可见", mid.name(), "#ff0000")

    view.set_contain_fit(False)
    app.processEvents()
    view._hover = False
    cover = QImage(200, 200, QImage.Format_ARGB32)
    cover.fill(Qt.transparent)
    view.render(cover)
    check("内嵌视图仍是 cover（没有上黑边）", cover.pixelColor(100, 10).name(), "#ff0000")
    view.close()


def test_session_handoff(app: QApplication) -> None:
    """卡片 body 的会话交给详情页：同一 player/sink，不重新开流。"""
    print("\n-- 会话换承载面（卡片 → 详情页）--")
    src = make_view(compact=True)
    dst = make_view()
    src._ensure_player()
    player, sink = src._player, src._sink
    source_changes: list[QUrl] = []
    player.sourceChanged.connect(source_changes.append)
    src._url = "rtsp://fake/live"
    src._frames = 5
    src._image = QImage(32, 18, QImage.Format_RGB32)
    src._set_state(CameraView.PLAYING)

    check_true("墙上登记成功",
               playbacks().claim(src, "dev1", src._url, False, "wall"))
    check("已有会话时拒绝第二路",
          playbacks().claim(dst, "dev2", "rtsp://fake/2", False, "detail"), False)

    check_true("搬到详情页成功", playbacks().move(dst, "detail"))
    check("所有者变成了详情页", playbacks().active_view is dst, True)
    check("承载面记为详情页", playbacks().surface, "detail")
    check("播放器还是同一个对象", dst._player is player, True)
    check("sink 还是同一个对象", dst._sink is sink, True)
    check("播放器没有重新 setSource", source_changes, [])
    check("帧号跟着会话走", dst._frames, 5)
    check("墙上控件显示「画面在别处」", src._state, CameraView.MOVED)
    check("墙上控件不再握播放器", src._player, None)
    check("墙上控件不能开第二路", src.has_live_playback, False)

    dst._on_frame(FakeFrame(0x112233))
    app.processEvents()
    check("接管后帧继续增长", dst._frames, 6)

    src.show_idle()
    check("卡片刷新不会擦掉「画面在别处」", src._state, CameraView.MOVED)

    check_true("搬回卡片成功", playbacks().move(src, "wall"))
    check("卡片重新握住同一个播放器", src._player is player, True)
    check("播放状态续上", src._state, CameraView.PLAYING)
    check("帧号接着涨", src._frames, 6)

    occupied = make_view()
    occupied._player = FakePlayer()  # 目标已有播放器：接管必须失败
    ok = src.hand_off_to(occupied)
    check("目标被占用时搬移失败", ok, False)
    check("失败后源控件继续握着播放器", src._player is player, True)
    check("失败后源控件仍在播", src._state, CameraView.PLAYING)
    occupied._player = None

    host = QWidget()
    lay = QVBoxLayout(host)
    lay.addWidget(src)
    host.show()
    app.processEvents()
    popup = CameraPopoutWindow(src, host, theme.LIGHT, "测试摄像头", did="dev1")
    popup.present()
    app.processEvents()
    check_true("独立窗口从详情页脱离", popup.detach_from_parent())
    check("脱离后窗口仍是顶层", bool(popup.windowFlags() & Qt.Window), True)
    popup.close()
    app.processEvents()
    check("关独立窗口后所有权清空", playbacks().active, False)
    check("关独立窗口后播放器已释放", src._player, None)

    # 会话彻底结束后，墙上卡片的「画面在别处」提示要能回到空闲，不能一直挂着
    back = make_view(compact=True)
    back._ensure_player()
    back._url = "rtsp://fake/back"
    back._set_state(CameraView.PLAYING)
    check_true("再次登记成功",
               playbacks().claim(back, "dev3", back._url, False, "wall"))
    other = make_view()
    check_true("再次搬到详情页", playbacks().move(other, "detail"))
    check("离开后是 MOVED", back._state, CameraView.MOVED)
    playbacks().release(other, "测试收尾")
    back.show_idle()
    check("会话结束后卡片回到空闲", back._state, CameraView.IDLE)
    back.close()
    other.close()

    src.close()
    dst.close()
    host.close()


def main() -> int:
    app = QApplication(sys.argv)
    test_buttons(app)
    test_move_without_reconnect(app)
    test_resize_grip(app)
    test_dim_shield_hole(app)
    test_session_handoff(app)
    test_contain_fit(app)
    print(f"\n结果：{len(FAILS)} 项失败" if FAILS else "\n全部通过")
    for f in FAILS:
        print(" -", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
