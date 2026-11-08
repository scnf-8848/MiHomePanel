"""卡片墙的取流仲裁：同一时刻只允许一张卡片播放。

一台摄像头只给约 3 路观看位，米家 App 还要抢；墙上要是让每张卡各自建会话，
开两三张就把位子占满，详情页反而打不开。所以墙面**硬限 1 路**：

* 点另一张卡的播放键 → 先停掉当前这张（停播 + 释放播放器 + 放掉 relay），
  再给新卡取地址；
* 滚出可视区域、切房间、搜索隐藏、最小化、打开详情 → 一律 ``stop``，
  不做 ``pause``——暂停时设备仍在推流，观看位没还回去；
* 取址在工作线程里跑，主线程只收结果。用户刚点播放就进详情时，
  ``CameraService.open(guard=...)`` 会在拿到锁之后重新确认任务是否还有效，
  旧任务不会把详情页刚接管的 relay 改走。

取流链路（探测 / 局域网优先回落 / 远程优先 / 仅远程 / 9 秒看门狗）与详情页
保持同一套判断，只是没有详情页的日志打点和预热——墙面不预热，避免看不见的
卡片偷偷占位。
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any

from PySide6.QtCore import QObject, QTimer

from ..core.camera import CameraError, probe_local
from ..core.camera import shared as camera_service
from ..core.store import Store
from .camera_card import CameraCard
from .camera_view import CameraView, playbacks

#: 局域网优先时，首帧前明确失败就不在同一个地址上重连，交给外层切远程
#: 与详情页一致：没报错但 9 秒没出帧也换一次通道
WATCHDOG_SECONDS = 9.0

#: 任务在取址途中被丢弃（切卡 / 进详情）。不是给用户看的错误，
#: 只是让 ``_collect`` 能清掉 ``_busy`` 并启动被挂起的下一路。
_CANCELLED = "已取消"


class WallCameraController(QObject):
    def __init__(self, store: Store, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self._active: CameraCard | None = None
        self._token = 0
        self._remote = False
        self._fallback = False
        self._probe_failed = False
        self._probe: tuple[bool, float, str] | None = None
        self._url = ""
        self._live = False
        self._started = 0.0
        self._error_at = 0.0

        self._job_token = 0
        self._busy = False
        self._pending: tuple[int, CameraCard, bool, bool] | None = None
        self._results: queue.SimpleQueue[
            tuple[int, str, str, str, bool, tuple[bool, float, str] | None]
        ] = queue.SimpleQueue()

        self._poll = QTimer(self)
        self._poll.setInterval(80)
        self._poll.timeout.connect(self._collect)

        self._watchdog = QTimer(self)
        self._watchdog.setInterval(1000)
        self._watchdog.timeout.connect(self._watch)

    # -- 对外 -------------------------------------------------------------
    @property
    def active_card(self) -> CameraCard | None:
        return self._active

    def request_play(self, card: CameraCard) -> None:
        if self._active is card and card.view_state in (
            CameraView.LOADING,
            CameraView.PLAYING,
            CameraView.PAUSED,
        ):
            return
        self.stop_active("切换播放")
        self._active = card
        self._token += 1
        self._remote = self._remote_wanted()
        self._fallback = False
        self._probe_failed = False
        self._probe = None
        self._url = ""
        self._live = False
        self._started = time.monotonic()
        self._error_at = 0.0
        self._watchdog.start()
        self._spawn(self._token, card, self._remote, allow_remote=True)

    def stop_active(self, reason: str = "") -> None:
        """停掉当前会话并归还观看位；重复调用是安全的。"""
        card = self._active
        self._active = None
        self._token += 1
        self._pending = None
        self._watchdog.stop()
        self._live = False
        self._url = ""
        if card is None:
            return
        # 有所有者登记时由它统一停播 + 放播放器 + 归还 relay；没登记过
        # （例如 claim 已经失败）仍然按原来的方式收尾。
        view = card.view
        if view is not None and playbacks().release(view, reason):
            return
        if view is not None and playbacks().active_view is not None:
            # 会话已经被别的承载面持有：只收卡片自己的画面，别把别人的 relay 放掉
            card.stop_playback()
            return
        card.stop_playback()
        camera_service().release(card.state.did)

    def stop_if_card(self, card: CameraCard, reason: str = "") -> None:
        if self._active is card:
            self.stop_active(reason)

    def hand_off_active_to(self, target: CameraView, did: str) -> bool:
        """把墙上正在播的这一路交给 ``target``（详情页），不重连。

        只搬 ``QMediaPlayer`` / ``QVideoSink``：卡片自己的画面控件回到
        ``MOVED`` 提示态，墙上不再有"活动卡"，滚出视野/重建墙都不会误停这条
        会话。目标接管失败时所有权保持不变，调用方应退回"先停再重开"。
        """
        card = self._active
        if card is None or card.view is None or target is None or target is card.view:
            return False
        if card.state.did != did:
            # 打开的是另一台设备：不能把 A 的画面接到 B 的详情页
            return False
        if card.view.state not in (
            CameraView.LOADING,
            CameraView.PLAYING,
            CameraView.PAUSED,
        ):
            return False
        if not playbacks().move(target, "detail"):
            return False
        self._active = None
        self._token += 1
        self._pending = None
        self._watchdog.stop()
        self._live = False
        self._url = ""
        return True

    # -- 通道选择（与详情页同一套判断，不写计时日志）---------------------
    def _pref(self) -> str:
        return str(self._store.camera_network or "")

    def _remote_wanted(self) -> bool:
        pref = self._pref()
        if pref == "remote_only":
            return True
        if pref == "remote_first":
            return not self._fallback
        if self._fallback:
            return True
        if self._probe_failed:
            return True
        card = self._active
        if card is None:
            return False
        st = self._store.devices.get(card.state.did, card.state)
        return not str(getattr(st.node, "localip", "") or "")

    # -- 后台取址 ---------------------------------------------------------
    def _alive(self, token: int) -> bool:
        return token == self._token and self._active is not None

    def _spawn(
        self, token: int, card: CameraCard, remote: bool, *, allow_remote: bool
    ) -> None:
        if self._busy:
            # 上一路还在 open（可能正卡在 relay 握手）：串行执行，别让两条
            # 会话同时改 go2rtc 的源
            self._pending = (token, card, remote, allow_remote)
            return
        st = self._store.devices.get(card.state.did, card.state)
        self._busy = True
        self._job_token = token
        self._poll.start()
        threading.Thread(
            target=self._work,
            args=(token, self._store.client, st.node, st.did, remote, allow_remote),
            daemon=True,
        ).start()

    def _work(
        self,
        token: int,
        client: Any,
        node: Any,
        did: str,
        remote: bool,
        allow_remote: bool,
    ) -> None:
        probe: tuple[bool, float, str] | None = None
        localip = str(getattr(node, "localip", "") or "")
        if not remote and localip:
            probe = probe_local(localip)
            if not probe[0]:
                if not allow_remote:
                    self._results.put((token, did, "", "局域网不可达", remote, probe))
                    return
                remote = True
        if not self._alive(token):
            # 探测期间被切卡 / 进详情：也要回一条结果，否则 ``_busy`` 永远
            # 挂着，被挂起的下一路（``_pending``）就再也起不来。
            self._results.put((token, did, "", _CANCELLED, remote, probe))
            return
        try:
            url = camera_service().open(
                client, node, remote=remote, guard=lambda: self._alive(token)
            )
        except CameraError as exc:
            if exc.kind == "cancelled":
                self._results.put((token, did, "", _CANCELLED, remote, probe))
                return
            self._results.put((token, did, "", str(exc), remote, probe))
        except Exception as exc:  # noqa: BLE001 - 外部进程 + 网络，兜底别让墙崩掉
            self._results.put((token, did, "", f"取流失败：{exc}", remote, probe))
        else:
            self._results.put((token, did, url, "", remote, probe))

    def _collect(self) -> None:
        while True:
            try:
                token, did, url, error, remote, probe = self._results.get_nowait()
            except queue.Empty:
                break
            if token == self._job_token:
                self._busy = False
                self._job_token = 0
            card = self._active
            if not self._alive(token) or card is None or did != card.state.did:
                # 过期任务：结果不再接画面，也**不要**顺手 release(did)——用户
                # 刚点播放就进详情时，详情页可能已经接管了同一台摄像头的
                # relay，这里再放一次会把它的画面掐掉。relay 由"谁停下谁归还"：
                # stop_active() 已按 did 释放，且它的 release 会等在
                # CameraService 的锁上，覆盖"任务刚好卡在 open 里"的窗口。
                self._start_pending()
                continue
            if error == _CANCELLED:
                self._start_pending()
                continue
            if probe is not None:
                self._probe = probe
                self._probe_failed = not probe[0]
            if not url:
                if (
                    probe is not None
                    and not probe[0]
                    and self._try_switch("可达性探测不通")
                ):
                    self._start_pending()
                    continue
                self._live = False
                self._url = ""
                card.show_error(error)
                self._start_pending()
                continue
            view = card.view
            if view is None or not playbacks().claim(
                view, card.state.did, url, remote, "wall"
            ):
                # 详情页 / 独立窗口已经握着另一路：墙上这一路不能开第二路
                self._live = False
                self._url = ""
                self._active = None
                self._watchdog.stop()
                if view is not None:
                    view.show_error("另一路摄像头正在播放，先把它停掉")
                self._start_pending()
                continue
            self._remote = remote
            self._url = url
            self._live = True
            self._started = time.monotonic()
            self._watchdog.start()
            card.view and card.view.set_immediate_fallback(
                self._pref() == "lan_first" and not remote
            )
            card.play_video(url)
            self._start_pending()

    def _start_pending(self) -> None:
        pending = self._pending
        self._pending = None
        if pending is None:
            if not self._busy:
                self._poll.stop()
            return
        token, card, remote, allow_remote = pending
        if self._alive(token):
            self._spawn(token, card, remote, allow_remote=allow_remote)

    # -- 播放器状态 / 看门狗 ---------------------------------------------
    def on_view_state(self, card: CameraCard, state: str) -> None:
        if card is not self._active:
            return
        if state != CameraView.LOADING:
            self._watchdog.stop()
        if state in (CameraView.ERROR, CameraView.STOPPED):
            self._live = False
        if state == CameraView.ERROR:
            self._url = ""
            self._error_at = time.monotonic()
            self._try_switch("播放器报错")

    def _watch(self) -> None:
        if not self._live:
            self._watchdog.stop()
            return
        if time.monotonic() - self._started < WATCHDOG_SECONDS:
            return
        self._watchdog.stop()
        self._try_switch("9 秒看门狗：无报错但没出帧")

    def _try_switch(self, reason: str = "") -> bool:
        """只换一次通道；``remote_only`` 从不自动换。"""
        if self._fallback or self._busy:
            return False
        pref = self._pref()
        if pref == "remote_only":
            return False
        card = self._active
        if card is None:
            return False
        st = self._store.devices.get(card.state.did, card.state)
        if self._remote:
            if pref != "remote_first":
                return False
            if not str(getattr(st.node, "localip", "") or ""):
                return False
            target_remote = False
        else:
            target_remote = True
        self._fallback = True
        if not target_remote:
            self._probe_failed = False
        self._url = ""
        self._live = False
        self._started = time.monotonic()
        if target_remote:
            card.show_progress("局域网直连不可用", "正在尝试远程中继…")
        else:
            card.show_progress("远程中继不可用", "正在尝试局域网直连…")
        self._token += 1
        self._watchdog.start()
        self._spawn(self._token, card, target_remote, allow_remote=target_remote)
        return True


__all__ = ["WallCameraController", "WATCHDOG_SECONDS"]
