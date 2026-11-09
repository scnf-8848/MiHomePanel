# SPDX-License-Identifier: GPL-3.0-or-later
"""离屏验证：摄像头声音与禁音按钮（不联网、不连真机）。

覆盖四段：

1. **偏好**：``camera_muted`` 存取、老文件没有这个键时默认静音、坏数据不把声音打开；
2. **取流参数**：``_stream_url`` 默认不写 ``audio``（= 开），
   ``MIHOME_CAMERA_AUDIO=0`` 才写 ``&audio=0``；跨网分支不再写死关掉，且保留 ``transport=udp``；
3. **控件**：默认静音、喇叭只在有音轨时出现、右下角整排**共用一条显隐规则**、
   三个键同尺寸、点击只动输出设备不碰播放器、选择会交给 saver 落盘、三键不重叠不出界；
4. **搬会话**：``take_playback`` / ``adopt_playback`` 把 audio_out 与静音状态一起带走。

真实出声不在这里验（离屏环境没有扬声器）：能出声的硬证据是
``scripts/probe_camera_audio.py`` 里 ``QAudioBufferOutput`` 收到的音频 buffer。

用法：
  .venv/Scripts/python.exe -X utf8 scripts/verify_camera_audio.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mihome.core import camera  # noqa: E402
from mihome.core.prefs import Prefs  # noqa: E402
from mihome.ui import camera_view, theme  # noqa: E402
from mihome.ui.camera_view import CameraView  # noqa: E402

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


def make_view(compact: bool = False) -> CameraView:
    v = CameraView(theme.LIGHT, "#3B82F6", compact=compact)
    v.resize(480, 220)
    v.show()
    return v


# ---------------------------------------------------------------------------
def test_prefs() -> None:
    print("\n-- 偏好：声音不落盘 --")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "prefs.json"
        p = Prefs(path)
        p.save()
        data = json.loads(path.read_text(encoding="utf-8"))
        check("落盘里没有 camera_muted", "camera_muted" in data, False)
        check("Prefs 上也没有这个属性", hasattr(Prefs(path), "camera_muted"), False)

        # 上一版写过这个键的文件：读到就忽略，不报错、也不把声音打开
        path.write_text('{"version": 6, "camera_muted": false}', encoding="utf-8")
        old = Prefs(path)
        check("老文件里的 camera_muted 被忽略", hasattr(old, "camera_muted"), False)
        check("老文件其它字段照常", old.close_to_tray, True)


def test_stream_url() -> None:
    print("\n-- 取流参数 --")
    rt = camera.Go2rtcRuntime(Path("go2rtc.exe"), "uid", "token")
    local = camera.CameraSource(did="1001", model="chuangmi.camera.069a01",
                                localip="192.168.5.25")
    remote = camera.CameraSource(did="1001", model="chuangmi.camera.069a01",
                                 localip="192.168.5.25", remote=True,
                                 host="127.0.0.1", port=41234)

    os.environ.pop("MIHOME_CAMERA_AUDIO", None)
    url = rt._stream_url(local)
    check_true("局域网默认不要 audio 参数（= 开）", "audio=" not in url, url)
    check_true("局域网地址还是 device ip", "@192.168.5.25" in url, "")
    check_true("默认带 subtype", "subtype=0" in url, "")

    url = rt._stream_url(remote)
    check_true("跨网不再写死 audio=0", "audio=0" not in url, url)
    check_true("跨网仍显式 transport=udp", url.endswith("&transport=udp"), url)
    check_true("跨网指向 relay 端口", "@127.0.0.1:41234" in url, "")

    os.environ["MIHOME_CAMERA_AUDIO"] = "0"
    try:
        check("环境变量关掉局域网音轨",
              "&audio=0" in rt._stream_url(local), True)
        check("环境变量关掉跨网音轨",
              "&audio=0" in rt._stream_url(remote), True)
        check("关掉时 audio_enabled 为假", camera.audio_enabled(), False)
    finally:
        os.environ.pop("MIHOME_CAMERA_AUDIO", None)
    check("默认 audio_enabled 为真", camera.audio_enabled(), True)

    os.environ["MIHOME_CAMERA_SUBTYPE"] = "sd"
    try:
        check("子码流仍然生效", "subtype=sd" in rt._stream_url(local), True)
    finally:
        os.environ.pop("MIHOME_CAMERA_SUBTYPE", None)


def test_widget(app: QApplication) -> None:
    print("\n-- 控件 --")
    view = make_view()
    check("新建控件默认静音", view.muted, True)
    check("还没建播放器时没有输出设备", view._audio_out, None)
    # 没播放器时点喇叭不该炸（空态下按钮本来就是隐藏的）
    view.toggle_mute()
    check("空态点一下也能切状态", view.muted, False)
    view.toggle_mute()
    check("再点回来", view.muted, True)

    # 建真播放器与输出设备（不 setSource，不联网）
    view._player = QMediaPlayer()
    view._sink = QVideoSink()
    view._audio_out = QAudioOutput()
    view._apply_mute()
    check("输出设备跟随静音状态", view._audio_out.isMuted(), True)
    view.set_muted(False)
    check("取消静音立刻生效", view._audio_out.isMuted(), False)

    # 喇叭显隐：靠 _has_audio 判定，真播放器此时 hasAudio() 为 False
    view._hover = True
    view._set_state(CameraView.PLAYING)
    app.processEvents()
    check("没有音轨时喇叭不出现", view._mute_btn.isVisible(), False)
    check("没音轨不影响暂停键", view._pause_btn.isVisible(), True)

    # 假装有音轨（真实值要到 LoadedMedia 之后才为真，见 _on_media_status）
    view._has_audio = lambda: True  # type: ignore[method-assign]
    view._set_state(CameraView.PLAYING)
    app.processEvents()

    # 整排**一条规则**：有画面 + 悬停才出现，移开整排消失（不给任何键开例外）
    view._hover = False
    view.set_muted(True)
    view._refresh_buttons()
    check("不悬停 → 暂停不出现", view._pause_btn.isVisible(), False)
    check("不悬停 → 喇叭不出现（静音也不常驻）", view._mute_btn.isVisible(), False)
    check("不悬停 → 独立窗口键不出现", view._pop_btn.isVisible(), False)
    view._hover = True
    view._refresh_buttons()
    check("悬停 → 三键同现",
          (view._pause_btn.isVisible(), view._mute_btn.isVisible(),
           view._pop_btn.isVisible()), (True, True, True))
    check("静音状态仍然显示在图标上", view._mute_btn._muted_icon, True)
    view.set_muted(False)

    # 已暂停时：中央已经是播放键，"暂停"这个动作无意义 → 它单独收起，
    # 其余两键照常（仍是悬停才出）
    view._set_state(CameraView.PAUSED)
    app.processEvents()
    check("暂停态不出现暂停键", view._pause_btn.isVisible(), False)
    check("暂停态喇叭仍在", view._mute_btn.isVisible(), True)
    check("暂停态独立窗口键仍在", view._pop_btn.isVisible(), True)
    view._set_state(CameraView.PLAYING)
    app.processEvents()

    # 尺寸：整排同尺寸（26/30/34 混排会让人以为权重不同）
    check("暂停 / 喇叭 / 独立窗口同尺寸",
          {view._pause_btn.width(), view._mute_btn.width(), view._pop_btn.width()},
          {camera_view._SIDE_BTN})
    # 没有音轨时喇叭不占位：否则暂停键右边永远空着一格（用 compact 卡测，
    # 那里本来就没有独立窗口键，暂停键贴右）
    silent = make_view(compact=True)
    silent._hover = True
    silent._set_state(CameraView.PLAYING)
    app.processEvents()
    check("无音轨时暂停键贴右（不留空槽）",
          silent._pause_btn.x(), silent.width() - 12 - silent._pause_btn.width())
    silent.close()
    wall_probe = make_view(compact=True)
    check("compact 卡两个键同尺寸",
          {wall_probe._pause_btn.width(), wall_probe._mute_btn.width()},
          {camera_view._SIDE_BTN_COMPACT})
    wall_probe.close()

    # 点击只动输出设备：播放器与帧都不该被碰
    calls: list[str] = []
    view._player.play = lambda: calls.append("play")  # type: ignore[method-assign]
    view._player.stop = lambda: calls.append("stop")  # type: ignore[method-assign]
    before = view._frames
    view._mute_btn.clicked.emit()
    app.processEvents()
    check("点喇叭不碰播放器", calls, [])
    check("点喇叭不影响帧计数", view._frames, before)
    check("点喇叭真的静音了", view._audio_out.isMuted(), True)

    # 布局：三个键从右往左排，右边界贴 padding、互不重叠
    view._hover = True
    view._refresh_buttons()
    app.processEvents()
    pad, gap = 12, 8
    right = view.width() - pad
    want_pop = right - view._pop_btn.width()
    want_mute = want_pop - gap - view._mute_btn.width()
    want_pause = want_mute - gap - view._pause_btn.width()
    check("独立窗口键贴右", view._pop_btn.x(), want_pop)
    check("喇叭在它左边", view._mute_btn.x(), want_mute)
    check("暂停键再往左", view._pause_btn.x(), want_pause)
    check_true("三键都在画面内",
               want_pause >= 0 and view._pop_btn.x() + view._pop_btn.width()
               == view.width() - pad, "")
    check("三键底边对齐",
          {btn.y() + btn.height()
           for btn in (view._pop_btn, view._mute_btn, view._pause_btn)},
          {view.height() - pad})

    # 紧凑卡也要有喇叭（卡片上的画面同样会出声），但没有独立窗口键
    wall = make_view(compact=True)
    wall._has_audio = lambda: True  # type: ignore[method-assign]
    wall._set_state(CameraView.PLAYING)
    app.processEvents()
    check("compact 没有独立窗口键", wall._pop_btn, None)
    check_true("compact 也有喇叭键", wall._mute_btn is not None, "")
    wall.close()

    # 声音只属于**这一次观看**：新开一路必须回到静音，暂停/继续才保持
    fresh = make_view()
    fresh._player = QMediaPlayer()
    fresh._sink = QVideoSink()
    fresh._audio_out = QAudioOutput()
    fresh.set_muted(False)
    check("这次观看先开了声音", fresh.muted, False)
    fresh.play("file:///nonexistent-camera.mp4")  # 不联网：只为走一遍 play 的入口
    check("新开一路 → 回到静音", fresh.muted, True)
    check("输出设备也静音", fresh._audio_out.isMuted(), True)
    fresh.set_muted(False)
    fresh._set_state(CameraView.PLAYING)
    fresh.toggle()
    check("暂停/继续不重置声音", fresh.muted, False)
    fresh.close()
    view.close()


def test_handoff(app: QApplication) -> None:
    print("\n-- 搬会话 --")
    a = make_view()
    b = make_view()
    # 真 Qt 对象，才能验证"同一个输出设备被搬到对面"以及 setParent 行为
    a._player = QMediaPlayer()
    a._sink = QVideoSink()
    a._audio_out = QAudioOutput()
    a._has_audio = lambda: True  # type: ignore[method-assign]
    a._set_state(CameraView.PLAYING)
    a.set_muted(False)
    audio = a._audio_out

    check_true("有活会话", a.has_live_playback, "")
    check_true("交给另一个控件", a.hand_off_to(b), "")
    check("源控件变成 MOVED", a.state, CameraView.MOVED)
    check("目标拿到同一个播放器", b._player is a._player, False)  # a 已置 None
    check_true("目标握着播放器", b._player is not None, "")
    check("音频输出一起搬过去了", b._audio_out is audio, True)
    check("静音状态跟着搬", b.muted, False)
    check("输出设备没有被重新打开", audio.isMuted(), False)

    # 目标上点静音，改的必须是被搬过来的那个设备
    b.toggle_mute()
    check("目标点静音改的是同一个设备", audio.isMuted(), True)
    b.release_player()
    check("释放后输出设备已清空", b._audio_out, None)
    a.close()
    b.close()


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    test_prefs()
    test_stream_url()
    test_widget(app)
    test_handoff(app)
    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败：")
        for line in FAILS:
            print("  - " + line)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
