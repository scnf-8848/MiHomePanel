# SPDX-License-Identifier: GPL-3.0-or-later
"""离屏验证：摄像头画面控件能不能真的收到帧、点播放与暂停是否按预期工作。

不打开主窗口、不改设备状态，只做取流链路与交互状态机的验证：

  1) 用本机凭据挑一台在线摄像头（默认第一台，可 ``--did`` 指定）
  2) 走 ``CameraService`` 起 go2rtc，拿本机 rtsp 地址
  3) 默认停在 IDLE：没点播放就不该取流（帧数保持 0）
  4) 点播放（``toggle()``）→ 外层拿到地址后 ``play()``；首帧到达应进入 PLAYING
  5) 再点应暂停（PAUSED）、帧数不再增长；再点恢复
  6) ``stop()`` 之后不再增长；离线设备的报错要可读
  7) 截 ``camera_view_idle.png`` / ``camera_view.png`` 供人工核对
  8) 暂停够久要放掉连接（``MIHOME_CAMERA_IDLE_SECONDS`` 调小来测），点播放会重新请求取流
  9) 断流原因解析：日志里新鲜的 ``cs2: EOF`` 要能认出来，旧的不算
 10) 崩溃留下的孤儿 go2rtc 要在下次启动时被清掉，且不误杀别的进程

用法：
  .venv/Scripts/python.exe -X utf8 scripts/verify_camera.py
  .venv/Scripts/python.exe -X utf8 scripts/verify_camera.py --did 1000000002 --seconds 15
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 从项目根直接 `python scripts/xxx.py` 时，sys.path[0] 是 scripts/ 而不是项目根
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mijiaAPI import mijiaAPI
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from mihome.core import paths
from mihome.core.camera import CameraError, Go2rtcRuntime, _log_age, cleanup_stale
from mihome.core.camera import shared as camera_service
from mihome.ui import theme
from mihome.ui.camera_view import CameraView

OUT = Path(__file__).resolve().parent / "out"
CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {name}{('  ' + detail) if detail else ''}")


def pump(app: QApplication, seconds: float) -> None:
    """跑指定时长的事件循环（比反复 exec() 灵活，也方便中途取样）。"""
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def pick_camera(devices: list[dict], did: str) -> dict:
    cams = [d for d in devices if "camera" in str(d.get("model") or "").lower()]
    if not cams:
        raise SystemExit("账号里没有摄像头设备")
    if did:
        hit = next((d for d in cams if str(d.get("did")) == did), None)
        if hit is None:
            raise SystemExit(f"没找到 did={did} 的摄像头")
        return hit
    online = [d for d in cams if d.get("isOnline")]
    return (online or cams)[0]


def as_node(dev: dict) -> SimpleNamespace:
    return SimpleNamespace(
        did=str(dev.get("did") or ""),
        name=str(dev.get("name") or ""),
        model=str(dev.get("model") or ""),
        localip=str(dev.get("localip") or ""),
        online=bool(dev.get("isOnline")),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="验证摄像头画面控件")
    parser.add_argument("--did", default="", help="指定摄像头 did，默认第一台在线的")
    parser.add_argument("--seconds", type=float, default=12.0, help="连续播放观察时长（秒）")
    args = parser.parse_args()

    api = mijiaAPI()
    devices = api.get_devices_list()
    dev = pick_camera(devices, args.did)
    node = as_node(dev)
    hw_override = os.environ.get("MIHOME_CAMERA_HW_DECODE", "").strip().lower() in (
        "1", "true", "yes", "on"
    )
    backend = "硬件（D3D11，与显存相关）" if hw_override else "软件（YUV420P，与显存无关）"
    print(f"目标：{node.name} / {node.model} / did={node.did} / online={node.online}")
    print(f"      localip={node.localip or '(无)'}")
    print(f"      解码后端：{backend}")

    # 必须是 QApplication：CameraView 是 QWidget，QGuiApplication 下建子控件会直接崩
    app = QApplication(sys.argv)
    # 画面上那几行字是用 painter.font() 画的，不设字体在离屏环境里会变成方框
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(13)
    app.setFont(font)
    view = CameraView(theme.DARK, theme.category_color("camera"))
    view.resize(436, 220)
    view.show()
    states: list[str] = []
    view.state_changed.connect(states.append)

    client = SimpleNamespace(raw=lambda: api)
    print("\n[1] 起 go2rtc 并登记流")
    t0 = time.time()
    try:
        url = camera_service().open(client, node)
    except CameraError as exc:
        view.show_error(str(exc))
        app.processEvents()
        OUT.mkdir(parents=True, exist_ok=True)
        view.grab().save(str(OUT / "camera_error.png"))
        print(f"  取流失败（kind={exc.kind}）：{exc}")
        check("能拿到 rtsp 地址", False, exc.kind)
        return 1
    startup = time.time() - t0
    print(f"  {url}  （{startup:.1f}s）")
    check("go2rtc 启动并就绪", True, f"{startup:.1f}s")
    check("地址只指向本机", url.startswith("rtsp://127.0.0.1:"), url.split("@")[-1])
    if not hw_override:
        # 回归护栏：camera_view 导入时就该把硬解关掉。开着的话显存一紧张，
        # 纹理建不出来（0x8007000e），重则 toImage() 里直接崩、接不住
        check(
            "解码不依赖显存（硬解已关）",
            os.environ.get("QT_FFMPEG_DECODING_HW_DEVICE_TYPES") == "none",
            os.environ.get("QT_FFMPEG_DECODING_HW_DEVICE_TYPES", "(未设置)"),
        )

    print("\n[2] 空态：没点播放就不该取流")
    OUT.mkdir(parents=True, exist_ok=True)
    pump(app, 1.5)
    check("默认停在 IDLE", view.state == CameraView.IDLE, view.state)
    check("空态没有帧", view.frames == 0, f"{view.frames} 帧")
    view.grab().save(str(OUT / "camera_view_idle.png"))

    print("\n[3] 点播放：请求取流并等首帧")
    requested: list[bool] = []

    def on_play_requested() -> None:
        requested.append(True)
        view.play(url)

    view.play_requested.connect(on_play_requested)
    t1 = time.time()
    view.toggle()
    while time.time() - t1 < 16 and view.state != CameraView.PLAYING:
        pump(app, 0.1)
    first = time.time() - t1 if view.state == CameraView.PLAYING else None
    check("点播放会请求取流", bool(requested), f"{len(requested)} 次")
    check("首帧耗时 ≤ 10s", first is not None and first <= 10.0,
          f"{first:.1f}s" if first is not None else "没收到")
    check("首帧到达进入播放态", view.state == CameraView.PLAYING, view.state)
    view.grab().save(str(OUT / "camera_view.png"))

    print(f"\n[4] 连续播放 {args.seconds:.0f} 秒")
    before = view.frames
    pump(app, args.seconds)
    total = view.frames - before
    fps = total / args.seconds if args.seconds else 0.0
    check("持续收到有效帧", total >= 20, f"{total} 帧, {fps:.1f} fps")

    print("\n[5] 点暂停：画面应停住")
    view.toggle()
    pump(app, 0.3)
    paused = view.frames
    pump(app, 2.0)
    check("进入暂停态", view.state == CameraView.PAUSED, view.state)
    check("暂停后不再增长", view.frames == paused, f"{paused} → {view.frames}")

    print("\n[6] 再点继续：应立刻续上")
    t3 = time.time()
    view.toggle()
    while time.time() - t3 < 3 and view.frames <= paused:
        pump(app, 0.05)
    again = time.time() - t3 if view.frames > paused else None
    check("回到播放态", view.state == CameraView.PLAYING, view.state)
    check("续播延迟 ≤ 1s", again is not None and again <= 1.0,
          f"{again:.2f}s" if again is not None else "没收到")

    print("\n[7] 状态序列应完整")
    want = ["loading", "playing", "paused", "playing"]
    seq: list[str] = []
    for s in states:
        if s in want and (not seq or seq[-1] != s):
            seq.append(s)  # 自动重连会重复一次 loading，合并掉
    check("状态流转正确", seq[: len(want)] == want, " → ".join(seq[:8]))

    print("\n[8] 停止后不应继续收帧")
    view.stop()
    before = view.frames
    pump(app, 1.5)
    check("stop() 后帧数不再增长", view.frames == before, f"{before} → {view.frames}")

    print("\n[9] 离线设备的错误要可读")
    offline = next((d for d in devices if "camera" in str(d.get("model") or "").lower()
                    and not d.get("isOnline")), None)
    if offline is None:
        print("  （账号里没有离线摄像头，跳过）")
    else:
        try:
            camera_service().open(client, as_node(offline))
            check("离线设备被拒绝", False, "居然没报错")
        except CameraError as exc:
            check("离线设备被拒绝", exc.kind == "offline", f"kind={exc.kind} / {exc}")

    print("\n[10] 暂停够久应放掉连接（阈值临时调到 2 秒）")
    os.environ["MIHOME_CAMERA_IDLE_SECONDS"] = "2"
    idle_view = CameraView(theme.DARK, theme.category_color("camera"))
    idle_view.resize(436, 220)
    idle_view.show()
    retried: list[bool] = []
    idle_view.play_requested.connect(lambda: retried.append(True))
    idle_url = camera_service().open(client, node)
    idle_view.play(idle_url)
    t0 = time.time()
    while time.time() - t0 < 20 and idle_view.state != CameraView.PLAYING:
        pump(app, 0.2)
    check("起播进入播放态", idle_view.state == CameraView.PLAYING, idle_view.state)
    idle_view.toggle()  # 暂停，开始计闲置
    t1 = time.time()
    while time.time() - t1 < 6 and idle_view.state != CameraView.RELEASED:
        pump(app, 0.2)
    check("闲置后自动释放", idle_view.state == CameraView.RELEASED, idle_view.state)
    check("释放文案写清了几分钟", "连接已释放" in idle_view._hint, idle_view._hint)
    frozen = idle_view.frames
    pump(app, 1.5)
    check("释放后不再收帧", idle_view.frames == frozen, f"{frozen} → {idle_view.frames}")
    idle_view.toggle()
    check("点播放会请求重新取流", bool(retried), f"retry {len(retried)} 次")
    idle_view.stop()
    os.environ.pop("MIHOME_CAMERA_IDLE_SECONDS", None)

    print("\n[11] 断流原因：新鲜的 cs2: EOF 要认出来，旧的不算")
    scratch = Path(tempfile.mkdtemp(prefix="mihome-log-"))
    probe = Go2rtcRuntime(Path("go2rtc.exe"), "uid", "token")
    probe._log_path = scratch / "go2rtc.log"
    fresh = datetime.now().strftime("%H:%M:%S.") + "000"
    probe._log_path.write_text(
        f"{fresh} WRN [rtsp] error=\"streams: xiaomi: probe: miss: read media: cs2: EOF\""
        "\n",
        encoding="utf-8",
    )
    check("新鲜 EOF 判定为观看位被占", probe.recent_failure() == "eof", probe.recent_failure())
    old = (datetime.now() - timedelta(minutes=10)).strftime("%H:%M:%S.") + "000"
    probe._log_path.write_text(
        f"{old} WRN [rtsp] error=\"miss: read media: cs2: EOF\"\n", encoding="utf-8"
    )
    check("十分钟前的 EOF 不算", probe.recent_failure() == "", probe.recent_failure() or "(空)")
    check("日志时间戳能解析", _log_age(f"{fresh} INF hello", datetime.now()) is not None)
    shutil.rmtree(scratch, ignore_errors=True)

    print("\n[12] 崩溃留下的孤儿进程要被清掉")
    camera_service().shutdown()
    record = paths.data_dir() / "go2rtc" / "runtime.json"
    if record.exists():
        record.unlink()
    scratch = Path(tempfile.mkdtemp(prefix="mihome-orphan-"))
    cfg = scratch / "go2rtc.yaml"
    with socket.socket() as probe_sock:
        probe_sock.bind(("127.0.0.1", 0))
        spare = probe_sock.getsockname()[1]
    cfg.write_text(
        "log:\n  level: error\n"
        f'api:\n  listen: "127.0.0.1:{spare}"\n'
        f'rtsp:\n  listen: "127.0.0.1:{spare + 1}"\n',
        encoding="utf-8",
    )
    orphan = subprocess.Popen(
        [str(camera_service().binary), "-config", str(cfg)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    pump(app, 1.0)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(
        json.dumps({"pid": orphan.pid, "config": str(cfg)}), encoding="utf-8"
    )
    killed = cleanup_stale()
    pump(app, 0.5)
    check("孤儿 go2rtc 被清掉", killed == orphan.pid and orphan.poll() is not None,
          f"killed={killed} / pid={orphan.pid}")
    check("进程记录已删除", not record.exists())
    if orphan.poll() is None:
        orphan.kill()

    # 反例：记录指向一个非 go2rtc 的进程，不能动它
    record.write_text(json.dumps({"pid": os.getpid(), "config": str(cfg)}), encoding="utf-8")
    check("不误杀非 go2rtc 进程", cleanup_stale() is None and not record.exists())
    check("本进程还活着", True, f"pid={os.getpid()}")
    shutil.rmtree(scratch, ignore_errors=True)

    failed = [c for c in CHECKS if not c[1]]
    print(f"\n结果：{len(CHECKS) - len(failed)}/{len(CHECKS)} 通过")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
