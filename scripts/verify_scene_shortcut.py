# SPDX-License-Identifier: GPL-3.0-or-later
"""验证「快捷场景 → 桌面快捷方式」（`mihome/core/scene_shortcut.py`）。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_scene_shortcut.py``

全程用临时目录，**不碰真实桌面**：``.lnk`` 建在 ``%TEMP%`` 里、待执行队列落在
``MIHOME_DATA_DIR`` 指向的临时目录。真机那一步（用户点一下，看气泡）只能人工验，
见 ``docs/reference/scene-shortcut.md`` 第 8 节。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAILS.append(f"{name} {detail}")


def run_waiter(log_path: str) -> int:
    """子进程：占住令牌、装好广播过滤器，收到消息就把待执行条目写进日志。

    这就是"程序已经在跑"那一侧的完整行为（除了真去执行场景）。
    """
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtWidgets import QApplication, QWidget

    from mihome.core import scene_shortcut
    from mihome.core.single_instance import SingleInstance

    log = Path(log_path)

    def say(text: str) -> None:
        log.write_text(text, encoding="utf-8")

    app = QApplication(sys.argv[:1])
    guard = SingleInstance()
    if not guard.acquire():
        say("BUSY")
        return 2
    # 不 show 也要真 HWND：``HWND_BROADCAST`` 只发给顶层窗口
    win = QWidget()
    win.setAttribute(Qt.WA_NativeWindow, True)
    win.createWinId()

    state = {"done": False}

    def fired() -> None:
        # 一次点击会连发三条广播（见 ``notify_existing``），只认第一条：
        # 主实例那边的节流（``SCENE_DRAIN_GRACE``）解决同一件事。
        if state["done"]:
            return
        state["done"] = True
        say(json.dumps(scene_shortcut.drain(), ensure_ascii=False))
        QTimer.singleShot(0, app.quit)

    guard.activated.connect(fired)
    say("READY")
    QTimer.singleShot(20000, app.quit)  # 兜底：没人来就自己退
    return app.exec()


def wait_until(path: Path, pred, timeout: float = 15.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if text and pred(text):
            return text
        time.sleep(0.05)
    return ""


def main() -> int:
    if "--waiter" in sys.argv:
        return run_waiter(sys.argv[sys.argv.index("--waiter") + 1])

    if sys.platform != "win32":
        print("跳过：桌面快捷方式只有 Windows 实现")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="mihome-shortcut-"))
    os.environ["MIHOME_DATA_DIR"] = str(tmp / "data")

    from mihome.core import scene_shortcut as sc

    # -- 1) 文件名清洗 ----------------------------------------------------
    print("1) 文件名清洗", flush=True)
    check("非法字符换成下划线", sc.sanitize_name('回/家: "晚安"?.') == "回_家_ _晚安__")
    check("首尾空格与句点去掉", sc.sanitize_name("  回家 . ") == "回家")
    check("超长截到 60", len(sc.sanitize_name("场" * 200)) == 60)
    check("空名回落成「场景 + id 后 6 位」", sc.sanitize_name("  ", "123456789") == "场景456789")
    check("没有 id 时回落成「快捷场景」", sc.sanitize_name("", "") == "快捷场景")
    check("正常名字原样保留", sc.sanitize_name("回家模式") == "回家模式")

    # -- 2) 参数拼装 ------------------------------------------------------
    print("\n2) 参数拼装", flush=True)
    spec = sc.launch_spec("111", "222", "回家")
    check("源码模式用 pythonw", Path(spec.target).name.lower() == "pythonw.exe", spec.target)
    check("源码模式带 -m mihome", spec.arguments.startswith("-m mihome --scene 111 --home 222"),
          spec.arguments)
    check("源码模式工作目录是仓库根", Path(spec.workdir) == ROOT, spec.workdir)
    check("描述里带场景名", "回家" in spec.description, spec.description)

    had_frozen = "frozen" in vars(sys)
    old_frozen = getattr(sys, "frozen", None)
    sys.frozen = True
    try:
        frozen = sc.launch_spec("111", "222", "回家")
    finally:
        if had_frozen:
            sys.frozen = old_frozen
        else:
            del sys.frozen
    check("打包模式目标是 exe 自己", frozen.target == sys.executable, frozen.target)
    check("打包模式不带 -m", frozen.arguments == "--scene 111 --home 222", frozen.arguments)
    check("打包模式工作目录是 exe 所在目录",
          Path(frozen.workdir) == Path(sys.executable).parent, frozen.workdir)

    # -- 3) .lnk 往返 -----------------------------------------------------
    print("\n3) .lnk 往返", flush=True)
    desk = tmp / "desktop"
    desk.mkdir()
    icon = ROOT / "assets" / "app.ico"
    one = sc.create("111", "222", "回家", desktop=desk, icon=str(icon))
    check("文件名就是场景名", one.name == "回家.lnk", one.name)
    back = sc.read_lnk(one)
    check("目标读回一致", back["target"] == spec.target, back["target"])
    check("参数读回一致", back["arguments"] == spec.arguments, back["arguments"])
    check("工作目录读回一致", Path(back["workdir"]) == ROOT, back["workdir"])
    check("图标读回一致", Path(back["icon"]) == icon, back["icon"])

    again = sc.create("111", "222", "回家", desktop=desk, icon=str(icon))
    check("同场景再建一次是覆盖，不产生 (2)", again == one and not (desk / "回家 (2).lnk").exists())
    other = sc.create("333", "222", "回家", desktop=desk, icon=str(icon))
    check("同名但不同场景另起 (2)", other.name == "回家 (2).lnk", other.name)
    check("(2) 绑的是新场景", "--scene 333" in sc.read_lnk(other)["arguments"])
    check("同名文件都在", len(list(desk.glob("*.lnk"))) == 2)

    weird = sc.create("444", "222", "晚/安: 模式", desktop=desk)
    check("非法字符落到文件名上", weird.name == "晚_安_ 模式.lnk", weird.name)

    try:
        sc.read_lnk(desk / "不存在.lnk")
        bad = False
    except OSError:
        bad = True
    check("读不存在的 .lnk 抛 OSError", bad)

    # -- 4) 待执行队列 ----------------------------------------------------
    print("\n4) 待执行队列", flush=True)
    pending = sc.pending_dir()
    first = sc.queue("111", "222")
    time.sleep(0.012)
    second = sc.queue("333", "222")
    got = sc.drain()
    check("两条都取到且按时间升序",
          [i["scene_id"] for i in got] == ["111", "333"], str(got))
    check("取走之后文件被删", not first.exists() and not second.exists())

    sc.queue("555", "222")
    half = pending / "scene-999999-0-dead.tmp"
    half.write_text(json.dumps({"scene_id": "999", "home_id": "222",
                                "at": int(time.time() * 1000)}), encoding="utf-8")
    got = sc.drain()
    check("写一半的 .tmp 不会被取走", [i["scene_id"] for i in got] == ["555"], str(got))
    half.unlink(missing_ok=True)

    old = pending / "scene-1-0-old.json"
    old.write_text(json.dumps({"scene_id": "777", "home_id": "222",
                               "at": int((time.time() - 120) * 1000)}), encoding="utf-8")
    got = sc.drain()
    check("超龄（>60 s）的条目丢弃、不执行", got == [], str(got))
    check("超龄文件被清掉", not old.exists())

    broken = pending / "scene-2-0-broken.json"
    broken.write_text("{不是 json", encoding="utf-8")
    got = sc.drain()
    check("写坏的条目丢弃", got == [] and not broken.exists())

    for i in range(7):
        sc.queue(str(1000 + i), "222")
    got = sc.drain(limit=5)
    check("一轮最多取 5 条", len(got) == 5, str(len(got)))
    check("多出来的留给下一轮", len(sc.drain()) == 2)
    check("清空后为空", sc.drain() == [])

    sc.queue("888", "222")
    got = sc.drain(now=time.time() + 3600)
    check("now 往后拨一小时也能把条目判超龄", got == [], str(got))

    # -- 5) 命令行 --------------------------------------------------------
    print("\n5) 命令行解析", flush=True)
    check("--scene/--home 分开写", sc.parse_request(["--scene", "12", "--home", "34"]) == ("12", "34"))
    check("--scene=12 也认", sc.parse_request(["--scene=12", "--home=34"]) == ("12", "34"))
    check("没有 --scene 就不是快捷方式调用", sc.parse_request(["--probe-camera"]) is None)
    check("requested 认出 --scene=", sc.requested(["--scene=1"]) and not sc.requested([]))
    check("缺 --home 拒绝", sc.parse_request(["--scene", "12"]) is None)
    check("--scene 空值拒绝", sc.parse_request(["--scene", "", "--home", "34"]) is None)
    check("带空格的 id 拒绝", sc.parse_request(["--scene", "1 2", "--home", "34"]) is None)
    check("带引号的 id 拒绝", sc.parse_request(['--scene', '1" --home 2', "--home", "34"]) is None)
    check("超长 id 拒绝", sc.parse_request(["--scene", "1" * 65, "--home", "34"]) is None)

    # -- 6) 交接链路（真广播 + 真子进程） --------------------------------
    # "程序已经在跑"那一侧的完整行为：第二进程落文件 → 广播 → 主进程读走。
    # 不打桩——互斥量和全局消息都是内核对象，假不了。
    print("\n6) 交接链路（子进程 + 真广播）", flush=True)
    from mihome.core.single_instance import SingleInstance

    log = tmp / "waiter.log"
    child = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--waiter", str(log)],
        env=dict(os.environ),
    )
    try:
        ready = wait_until(log, lambda t: t.strip() == "READY")
        check("等待进程占住了令牌并装好过滤器", bool(ready), ready.strip()[:20])
        if ready:
            sc.queue("42", "7")
            # 这就是 app.py 里第二实例那两行：先落文件，再广播
            SingleInstance().notify_existing()
            got = wait_until(log, lambda t: t.lstrip().startswith("["))
            items = json.loads(got) if got else []
            check("广播到达后读到了待执行条目", [i.get("scene_id") for i in items] == ["42"],
                  got[:80])
        else:
            check("广播链路", False, "子进程没就绪")
    finally:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()

    # -- 7) 接线 ----------------------------------------------------------
    # 不建窗口（那要联网刷拓扑），只确认信号 / 入口 / 启动分支真的连上了：
    # 这类"拼错一个名字就整条路不通"的地方，比逻辑更容易坏。
    print("\n7) 接线", flush=True)
    from mihome.ui.main_window import MainWindow
    from mihome.ui.wall import WallView

    check("墙面暴露了 scene_shortcut_requested 信号",
          hasattr(WallView, "scene_shortcut_requested"))
    check("主窗口有广播入口", callable(getattr(MainWindow, "on_activation_broadcast", None)))
    check("主窗口有「创建快捷方式」处理", callable(getattr(MainWindow, "_on_scene_shortcut", None)))
    check("主窗口有启动补捞", callable(getattr(MainWindow, "_drain_scene_requests", None)))

    app_src = (ROOT / "mihome" / "app.py").read_text(encoding="utf-8")
    check("启动时把广播接到窗口（而不是直接抢焦点）",
          "guard.activated.connect(window.on_activation_broadcast)" in app_src)
    queued_at = app_src.find("scene_shortcut.queue(*scene_request)")
    broadcast_at = app_src.find(
        "guard.notify_existing()", app_src.find("if not guard.acquire()")
    )
    check("第二实例先落文件再广播", -1 < queued_at < broadcast_at,
          f"queue@{queued_at} notify@{broadcast_at}")
    check("场景请求时不进托盘（显式启动）",
          "autostart_requested() and scene_request is None" in app_src)

    # -- 汇总 -------------------------------------------------------------
    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：")
        for line in FAILS:
            print("  -", line)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
