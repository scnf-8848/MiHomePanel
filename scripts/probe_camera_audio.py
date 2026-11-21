# SPDX-License-Identifier: GPL-3.0-or-later
"""摄像头音频可行性探针：流里有没有音轨、Qt 能不能解出来、拖不拖慢首帧。

用法（要有一台在线的摄像头、并且和电脑在同一局域网）：

    .venv/Scripts/python.exe -X utf8 scripts/probe_camera_audio.py
    .venv/Scripts/python.exe -X utf8 scripts/probe_camera_audio.py --audio 0   # 对照组
    .venv/Scripts/python.exe -X utf8 scripts/probe_camera_audio.py --repeat 2  # 每档跑两遍

量三件事：

1. **go2rtc 侧**：``/api/streams`` 里这台流的 ``medias`` 有没有 audio 轨、codec 是什么
   （OPUS / PCMA），以及生产者字节数——用来确认 ``audio`` 参数真的改变了吗。
2. **Qt 侧**：``QAudioBufferOutput`` 能收到解码后的音频 buffer 就说明"能出声"，
   不依赖扬声器是否被别的程序占着（离屏也能验证）。同时记 ``hasAudio()`` 在
   ``mediaStatus`` 每一档的值，回答"按钮显隐会不会判太早"。
3. **代价**：首个视频帧的时刻（开/关音频各跑一遍对比），确认 ``probe()`` 不会为了
   等音频轨而拖慢画面。

不碰真实数据目录：go2rtc 配置写到临时目录，与正在运行的 App 互不影响。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mijiaAPI import mijiaAPI  # noqa: E402

from mihome.core.camera import CameraSource, Go2rtcRuntime, find_binary  # noqa: E402


def _medias(api_port: int, name: str) -> list[dict]:
    """读一台流的轨道列表；没起来返回空。

    go2rtc 把 ``medias`` 放在 **producer** 上（不在流对象顶层），
    且懒启动：没人拉流时连 producers 都是空的。
    """
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{api_port}/api/streams", timeout=2
        ) as resp:
            info = json.loads(resp.read() or b"{}").get(name, {})
    except (urllib.error.URLError, OSError, ValueError):
        return []
    for prod in info.get("producers") or []:
        medias = prod.get("medias") or []
        if medias:
            return list(medias)
    return []


def _fmt_medias(medias: list) -> str:
    """go2rtc 的 ``producers[].medias`` 是字符串数组（``H265:2960x1666`` / ``OPUS:48000:2``）。"""
    if not medias:
        return "（无）"
    parts = []
    for m in medias:
        if isinstance(m, str):
            parts.append(m)
        elif isinstance(m, dict):  # 某些版本是对象，宽松处理
            kind = m.get("kind") or "?"
            codec = m.get("codec") or "?"
            extra = []
            if m.get("samplerate"):
                extra.append(f"{m['samplerate']}Hz")
            if m.get("channels"):
                extra.append(f"{m['channels']}ch")
            if m.get("clockrate"):
                extra.append(f"clock={m['clockrate']}")
            parts.append(f"{kind}:{codec}" + (f"({' '.join(extra)})" if extra else ""))
        else:
            parts.append(str(m))
    return " · ".join(parts)


#: 音轨 codec 关键字（go2rtc 的 medias 写成 ``audio, recvonly, OPUS/48000/2``）
_AUDIO_CODECS = ("OPUS", "PCMA", "PCMU", "AAC", "MP3", "G711", "PCML", "L16")


def _has_audio(medias: list) -> bool:
    """流里有没有**可收**的音轨。

    只认 ``recvonly``：``sendonly`` 那条是对讲（backchannel）的声明，
    go2rtc 默认会挂上，它不代表这台摄像头有麦克风音频可听。
    """
    for m in medias:
        text = m if isinstance(m, str) else str((m or {}).get("codec") or "")
        if "recvonly" not in text:
            continue
        if any(codec in text.upper() for codec in _AUDIO_CODECS):
            return True
    return False


def _byte_size(api_port: int, name: str) -> int:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{api_port}/api/streams", timeout=2
        ) as resp:
            info = json.loads(resp.read() or b"{}").get(name, {})
        prods = info.get("producers") or []
        return int(prods[0].get("bytes_recv") or 0) if prods else 0
    except (urllib.error.URLError, OSError, ValueError):
        return 0


def run_qt(
    url: str,
    t0: float,
    limit: float,
    *,
    with_audio: bool,
    api_port: int = 0,
    name: str = "",
) -> dict:
    """走真的 QMediaPlayer，量首帧与音频解码。返回一条结果记录。

    注意 go2rtc 是**懒启动**的：没人拉流时 ``/api/streams`` 里连生产者都没有，
    所以轨道信息只能在播放期间取，不能在开播前"先查一下"。
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QUrl
    from PySide6.QtMultimedia import (
        QAudioBufferOutput,
        QAudioOutput,
        QMediaPlayer,
        QVideoSink,
    )
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv[:1])
    player = QMediaPlayer()
    sink = QVideoSink()
    player.setVideoSink(sink)

    # 音频输出：真机上有默认设备；没有就跳过（离屏 CI 也不该失败）
    audio_out = QAudioOutput() if with_audio else None
    if audio_out is not None:
        player.setAudioOutput(audio_out)
        audio_out.setVolume(1.0)

    buf_out = QAudioBufferOutput() if with_audio else None
    if buf_out is not None:
        player.setAudioBufferOutput(buf_out)

    rec: dict = {
        "audio": with_audio,
        "status": [],  # [(相对秒, 状态名, hasAudio)]
        "first_video": None,
        "first_audio": None,
        "audio_frames": 0,
        "audio_format": "",
        "error": "",
        "muted_works": None,
        "medias": [],
        "medias_at": None,
        "bytes": 0,
    }
    started = time.time()

    def on_status(status) -> None:
        rec["status"].append((time.time() - t0, status.name, player.hasAudio()))

    def on_frame(frame) -> None:
        if frame.isValid() and rec["first_video"] is None:
            rec["first_video"] = time.time() - t0
            rec["video_size"] = (frame.width(), frame.height())

    def on_audio_buffer(buf) -> None:
        if rec["first_audio"] is None:
            rec["first_audio"] = time.time() - t0
            f = buf.format()
            rec["audio_format"] = (
                f"{f.sampleRate()}Hz {f.channelCount()}ch {f.sampleFormat().name}"
            )
        rec["audio_frames"] += 1

    def on_error(_err, message) -> None:
        rec["error"] = message

    player.mediaStatusChanged.connect(on_status)
    player.errorOccurred.connect(on_error)
    sink.videoFrameChanged.connect(on_frame)
    if buf_out is not None:
        buf_out.audioBufferReceived.connect(on_audio_buffer)

    player.setSource(QUrl(url))
    player.play()

    # 等到首帧 + 至少 20 个音频 buffer，再多跑 3 秒量码率
    settle_at = None
    while time.time() - started < limit:
        app.processEvents()
        if settle_at is None and rec["first_video"] is not None:
            if not with_audio or rec["audio_frames"] > 20:
                settle_at = time.time() + 3.0
        if settle_at is not None and time.time() > settle_at:
            break
        # 生产者信息只能在播放期间取（见 docstring）
        if rec["medias_at"] is None and api_port and name:
            medias = _medias(api_port, name)
            if medias:
                rec["medias"] = medias
                rec["medias_at"] = time.time() - t0
        time.sleep(0.005)

    if api_port and name:
        rec["bytes"] = _byte_size(api_port, name)

    # 再让音频跑一会儿，验证 muted 不打断解码
    if with_audio and audio_out is not None:
        before = rec["audio_frames"]
        audio_out.setMuted(True)
        t_mute = time.time()
        while time.time() - t_mute < 1.0:
            app.processEvents()
            time.sleep(0.005)
        rec["muted_works"] = rec["audio_frames"] - before > 0
        audio_out.setMuted(False)

    rec["seconds"] = time.time() - started
    player.stop()
    player.setSource(QUrl())
    if buf_out is not None:
        player.setAudioBufferOutput(None)
    return rec


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", type=int, default=1, help="1 = 流里要音频轨，0 = 不要")
    parser.add_argument("--subtype", type=int, default=0)
    parser.add_argument("--seconds", type=float, default=25.0)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--qt-seconds", type=float, default=18.0)
    args = parser.parse_args()
    want_audio = bool(args.audio)

    api = mijiaAPI()
    dev = next(
        d
        for d in api.get_devices_list()
        if "camera" in str(d.get("model") or "").lower() and d.get("isOnline")
    )
    src = CameraSource(
        did=str(dev["did"]),
        model=str(dev["model"]),
        localip=str(dev.get("localip") or ""),
        name=str(dev.get("name") or ""),
    )
    auth = api.auth_data
    binary = find_binary()
    if binary is None:
        print("找不到 go2rtc")
        return 1

    print(f"设备：{src.name} · {src.model} · {src.localip}")
    print(f"本次请求：audio={'开' if want_audio else '关'} · subtype={args.subtype}")

    workdir = Path(tempfile.mkdtemp(prefix="mihome-probe-audio-"))
    rt = Go2rtcRuntime(binary, str(auth["userId"]), str(auth["passToken"]))
    rt._workdir = workdir
    rt._log_path = workdir / "go2rtc.log"
    rt._stream_url = lambda s: (
        f"xiaomi://{rt.user_id}:{rt.region}@{s.localip}"
        f"?did={s.did}&model={s.model}&channel=0&subtype={args.subtype}"
        + ("" if want_audio else "&audio=0")
    )

    t0 = time.time()
    rt.start([src])
    print(f"{time.time() - t0:6.2f}s  go2rtc 就绪  api={rt.api_port} rtsp={rt.rtsp_port}")

    url = rt.url(src.did)
    results = []
    for i in range(max(1, args.repeat)):
        rec = run_qt(
            url,
            t0,
            args.qt_seconds,
            with_audio=want_audio,
            api_port=rt.api_port,
            name=rt.stream_name(src.did),
        )
        results.append(rec)
        head = f"  第 {i + 1} 遍"
        print(f"{head}：首个视频帧 {rec['first_video'] and round(rec['first_video'], 2)}s"
              f" · 首个音频 {"-" if rec["first_audio"] is None else round(rec["first_audio"], 2)}s"
              f" · 音频 buffer {rec['audio_frames']} 个"
              f" · {rec['audio_format'] or '无音频格式'}")
        if rec["error"]:
            print(f"        Qt 报错：{rec['error']}")
        if rec.get("muted_works") is not None:
            print(f"        setMuted 之后仍在解码：{rec['muted_works']}")
        print(f"        轨道：{_fmt_medias(rec['medias'])}"
              f"（{'-' if rec['medias_at'] is None else round(rec['medias_at'], 2)}s 取到）"
              f" · 有音轨={_has_audio(rec['medias'])}")
        if rec["bytes"]:
            span = max(0.1, rec["seconds"])
            print(f"        生产者收 {rec['bytes'] // 1024} KB / {span:.1f}s"
                  f" ≈ {rec['bytes'] * 8 / span / 1000:.0f} kbps（视频+音频）")
        print("        mediaStatus : " + " → ".join(
            f"{n}(hasAudio={h})" for _t, n, h in rec["status"]
        ))
        time.sleep(1.0)

    rt.stop()

    firsts = [r["first_video"] for r in results if r["first_video"] is not None]
    if firsts:
        print(f"\n首帧：最快 {min(firsts):.2f}s · 平均 {sum(firsts) / len(firsts):.2f}s")
    if want_audio:
        audios = [r["first_audio"] for r in results if r["first_audio"] is not None]
        if audios:
            print(f"首包音频：平均 {sum(audios) / len(audios):.2f}s · "
                  f"比首帧{'早' if audios[0] < firsts[0] else '晚'} "
                  f"{abs(audios[0] - firsts[0]):.2f}s")
        else:
            print("没收到任何音频 buffer —— Qt 没解出音轨，需要换验证方式")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
