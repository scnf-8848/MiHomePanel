# SPDX-License-Identifier: GPL-3.0-or-later
"""离屏验证：关掉详情页之后，卡片上的画面能回来（不联网、不连真机）。

2026-11-10 实测的 bug：卡片 body 正在播 → 双击进详情（会话被搬走，卡片停在
"画面已在其他窗口中播放"）→ 关掉详情页 → 卡片**永久停在提示**上。原因是会话
搬走时卡片已经不是"活动卡"，之后没有任何状态推送会叫醒它。

修复分三段，这个脚本逐段验证：

1. **还回去**：``PlaybackOwnership.move`` 能把 MOVED 的卡片重新接上（同一个播放器，
   帧号与静音状态连续，不重开）；
2. **叫醒它**：会话真正结束（``release`` / ``forget``）会通知订阅者——卡片墙据此把
   还停在 MOVED 的卡片放回空闲，不再留一个假提示；
3. **不杀错人**：详情页关窗时若会话已被卡片接走，``_session_moved_away`` 必须判成
   "已易主"，否则兜底的 ``release(did)`` 会把刚还回去的画面掐掉。

用法：
  .venv/Scripts/python.exe -X utf8 scripts/verify_camera_handback.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mihome.ui import theme  # noqa: E402
from mihome.ui.camera_view import CameraView, playbacks  # noqa: E402
from mihome.ui.detail import _session_taken_by_other  # noqa: E402

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


def make_view() -> CameraView:
    v = CameraView(theme.LIGHT, "#3B82F6")
    v.resize(480, 220)
    v.show()
    return v


def give_session(view: CameraView, did: str = "cam1") -> QAudioOutput:
    """给控件装一套真播放器对象并登记成当前会话（不 setSource，不联网）。"""
    view._player = QMediaPlayer()
    view._sink = QVideoSink()
    view._audio_out = QAudioOutput()
    view._set_state(CameraView.PLAYING)
    playbacks().claim(view, did, "rtsp://127.0.0.1/x", False, "wall")
    return view._audio_out


# ---------------------------------------------------------------------------
def test_handback(app: QApplication) -> None:
    print("\n-- 关掉详情页：会话还回卡片 --")
    card = make_view()
    detail = make_view()
    saved_muted = playbacks()
    saved_muted.release(None, "测试前清场")
    audio = give_session(card)
    card.set_muted(False)          # 用户在这次观看里开了声音
    card._frames = 42              # 已播了 42 帧，接回来时不该归零
    card._painted = True

    check_true("卡片握着会话", card.has_live_playback, "")
    check_true("交给详情页（连所有权一起搬）", playbacks().move(detail, "detail"), "")
    check("卡片进入 MOVED", card.state, CameraView.MOVED)
    check("详情页接手同一个播放器", detail._player is not None, True)
    check("会话所有者换成详情页", playbacks().active_view is detail, True)

    # 关页面时卡片墙调用的就是这一步
    check_true("move 能把 MOVED 的卡片接回来", playbacks().move(card, "wall"), "")
    check("所有者回到卡片", playbacks().active_view is card, True)
    check("卡片重新播放中", card.state, CameraView.PLAYING)
    check("帧号连续（没有重新计数）", card._frames, 42)
    check("画面仍在（没被清成空态）", card._painted, True)
    check("音频输出还是同一个", card._audio_out is audio, True)
    check("声音状态跟着回来（用户开过）", card.muted, False)
    check("输出设备没有被重新打开", audio.isMuted(), False)
    check("承载面记为墙面", playbacks().surface, "wall")

    # 卡片接回来之后，"滚出视野就停"应该继续管着它：登记成活动卡
    print("        （活动卡登记由 WallCameraController.take_back 负责，见 docstring）")


def test_finish_notice(app: QApplication) -> None:
    print("\n-- 会话结束：通知墙面叫醒 MOVED 的卡片 --")
    playbacks().release(None, "测试前清场")
    calls: list[int] = []
    playbacks().on_finish(lambda: calls.append(1))

    card = make_view()
    detail = make_view()
    give_session(card)
    playbacks().move(detail, "detail")
    check("搬走后卡片是 MOVED", card.state, CameraView.MOVED)
    check("此时还没有结束通知", calls, [])

    check_true("结束会话成功", playbacks().release(detail, "关闭详情页"), "")
    check("结束触发了一次通知", len(calls), 1)
    check("已经没有活动会话", playbacks().active, False)
    check("卡片本身不会自己回去（要靠通知）", card.state, CameraView.MOVED)

    # 墙面收到通知后做的事（WallView._on_session_finished 的等价动作）
    if not playbacks().active and card.state == CameraView.MOVED:
        card.show_idle()
    check("叫醒之后回到空闲态（播放键回来）", card.state, CameraView.IDLE)
    check("提示文案已清掉", card._hint, "")

    # forget（报错但保留错误界面）也要通知，否则同样会卡住
    calls.clear()
    give_session(card)
    playbacks().move(detail, "detail")
    playbacks().forget(detail)
    check("forget 也通知", len(calls), 1)
    card.close()
    detail.close()


def test_taken_by_other(app: QApplication) -> None:
    print("\n-- 易主判定：接走之后不能再 release --")
    playbacks().release(None, "测试前清场")
    card = make_view()
    detail = make_view()
    other = make_view()

    give_session(detail)
    check("会话在自己手里 → 不算易主",
          _session_taken_by_other(playbacks(), detail, "cam1"), False)

    check_true("卡片接回去", playbacks().move(card, "wall"), "")
    check("接走之后 → 算易主（本页必须放手）",
          _session_taken_by_other(playbacks(), detail, "cam1"), True)

    playbacks().release(None, "清场")
    give_session(other, "cam2")
    check("别处播的是另一台设备 → 不算易主（本页可照常收尾）",
          _session_taken_by_other(playbacks(), detail, "cam1"), False)

    playbacks().release(None, "清场")
    check("根本没有会话 → 不算易主",
          _session_taken_by_other(playbacks(), detail, "cam1"), False)

    card.close()
    detail.close()
    other.close()


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    print("-- 清场 --")
    playbacks().release(None, "脚本开始")
    test_handback(app)
    test_finish_notice(app)
    test_taken_by_other(app)
    playbacks().release(None, "脚本结束")
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
