# SPDX-License-Identifier: GPL-3.0-or-later
"""程序入口。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from .core import autostart
from .core.single_instance import SingleInstance, focus_window
from .ui import theme
from .ui.main_window import MainWindow


def _binary_version(binary: Path) -> str:
    """跑一下 ``go2rtc -v``：文件在不代表能跑（架构不对、被截断都会在这里露馅）。"""
    try:
        proc = subprocess.run(
            [str(binary), "-v"],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=20,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"跑不起来：{exc}"
    if proc.returncode != 0:
        return f"退出码 {proc.returncode}"
    first = (proc.stdout or proc.stderr).strip().splitlines()
    return first[0].strip() if first else ""


def self_check() -> int:
    """发版自检：不建窗口、不联网，只回答三件事。

    为什么要在**打包后的 exe** 上做：go2rtc 是外部进程，ffmpeg 后端是 Qt 插件，
    两者都不会被 PyInstaller 自动带上。缺了的时候程序照样启动、日志也干净，只有
    用户点播放才黑屏或报"没有找到 go2rtc"——所以要有一条命令能提前问出来。
    ``--windowed`` 的 exe 没有 stdout，结果同时落一份到 ``<data>/logs/self-check.txt``。
    """
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtMultimedia import QMediaFormat

    from .core import paths
    from .core.camera import find_binary, missing_binary_hint, program_dirs

    lines: list[str] = []
    ok = True

    def say(text: str) -> None:
        lines.append(text)

    say(f"程序目录：{program_dirs()[0]}")
    say(f"数据目录：{paths.data_dir()}")
    say(f"打包形态：{'frozen' if getattr(sys, 'frozen', False) else '源码运行'}")

    binary = find_binary()
    if binary is None:
        ok = False
        say(f"[FAIL] go2rtc：{missing_binary_hint()}")
    else:
        version = _binary_version(binary)
        good = version.startswith("go2rtc version")
        ok = ok and good
        say(f"[{'OK' if good else 'FAIL'}] go2rtc：{binary}")
        say(f"       版本：{version or '（没有输出）'}")
        # 打了开播补丁的构建会留一份 BUILD.txt，用它区分"2 秒"和"5 秒"
        build_txt = binary.parent / "BUILD.txt"
        if build_txt.is_file():
            patch = [
                ln for ln in build_txt.read_text(encoding="utf-8").splitlines()
                if ln.startswith("patch=")
            ]
            say(f"       开播补丁：{patch[0][6:] if patch else '（BUILD.txt 里没写）'}")
        else:
            say("       开播补丁：没有 → 首帧会是 4.8–6.3 秒（见 docs/DEVELOPMENT.md「开播补丁」）")

    app = QCoreApplication.instance() or QCoreApplication([])
    try:
        codecs = {
            c.name
            for c in QMediaFormat().supportedVideoCodecs(
                QMediaFormat.ConversionMode.Decode
            )
        }
    except Exception as exc:  # noqa: BLE001 - 后端缺失时抛什么都有可能
        codecs = set()
        say(f"       查询失败：{type(exc).__name__}: {exc}")
    del app
    has_hevc = "H265" in codecs
    ok = ok and has_hevc
    say(
        f"[{'OK' if has_hevc else 'FAIL'}] Qt 多媒体后端：HEVC "
        f"{'可解' if has_hevc else '解不了'}（共 {len(codecs)} 种视频编码）"
    )
    if not has_hevc:
        say("       ffmpeg 后端的 dll 没进包，或 Qt 插件目录没被收集")

    say("自检" + ("通过" if ok else "未通过"))
    report = paths.log_dir() / "self-check.txt"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if sys.stdout is not None:
        # 打包成 --windowed 后这个管道是系统代码页，中文会变乱码；报告文件才是准的
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
        print("\n".join(lines))
        print(f"\n报告：{report}")
    return 0 if ok else 1


def probe_camera(*, rounds: int = 1, lane: str = "", timeout: float = 25.0) -> int:
    """真机冒烟：起界面 → 进摄像头详情页 → **点一次播放** → 量首帧。

    和 ``--self-check`` 互补。自检只回答"能起进程、能解 HEVC"，那两件事在**没有
    摄像头**的机器上也能通过；这条回答的是"点下去到底有没有画面、等了多久"，把
    go2rtc 找不找得到、Qt 插件在不在、局域网通不通、开播补丁有没有生效一次验完。

    做成 exe 自带的开关而不是外部脚本：打包产物里的 Python 是嵌进去的，外面的脚本
    驱动不了它，而"开发机好好的、装完没画面"恰恰是最常见的打包事故。
    报告写 ``<data>/logs/probe-camera.txt``（``--windowed`` 没有可读 stdout），
    而且是**边跑边写**——中途崩掉时最后一行就是现场，不会一条都不留。
    """
    import time
    import traceback

    from .core import paths

    # 冒烟要的是"窗口开了又关"的确定行为：留着托盘，close() 会把窗口收起来、
    # 图标在通知区域一闪，量出来的时间线反而不好对上
    os.environ.setdefault("MIHOME_NO_TRAY", "1")

    report = paths.log_dir() / "probe-camera.txt"
    lines: list[str] = []
    times: list[float] = []

    def flush() -> None:
        try:
            report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass

    def say(text: str) -> None:
        lines.append(text)
        flush()

    say(f"数据目录：{paths.data_dir()}")
    say(f"打包形态：{'frozen' if getattr(sys, 'frozen', False) else '源码运行'}")

    app: QApplication | None = None
    window: MainWindow | None = None
    try:
        from .core.camera import find_binary

        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
        app = QApplication.instance() or QApplication(sys.argv[:1])
        app.setStyle("Fusion")
        font = QFont("Microsoft YaHei UI")
        font.setPixelSize(13)
        app.setFont(font)
        theme.apply_to_app(theme.LIGHT)
        say(f"界面后端：{app.platformName()}  go2rtc：{find_binary() or '（没找到）'}")

        window = MainWindow(theme.LIGHT)
        window.palette_ref = theme.LIGHT
        window._apply_theme()
        window.resize(1360, 900)
        window.show()
        window.start()

        deadline = time.time() + 45
        while time.time() < deadline:
            app.processEvents()
            if not window.store.loading and window.store.devices:
                break
            time.sleep(0.02)

        devices = window.store.devices
        if not devices:
            say("[FAIL] 设备列表空：没登录，或者云端请求失败")
            return _finish(lines, ok=False, report=report)

        cam = next(
            (d for d in devices.values() if d.online and "camera" in d.node.model.lower()),
            None,
        )
        if cam is None:
            say(f"[FAIL] {len(devices)} 台设备里没有在线摄像头")
            return _finish(lines, ok=False, report=report)

        if lane:
            window.store.prefs.camera_network = lane  # 只改内存，不动用户设置
        say(f"摄像头：{cam.name}（{cam.node.model}）localip={getattr(cam.node, 'localip', '')}")
        say(f"通道偏好：{window.store.camera_network}  轮数：{rounds}")

        from .ui.detail import DetailDialog

        for i in range(rounds):
            dialog = DetailDialog(
                cam, window.store, window.palette_ref, window, icons=window.icon_provider
            )
            dialog.show()
            view = dialog._camera
            if view is None:
                say("[FAIL] 详情页没建出画面控件")
                dialog.close()
                break
            # 和真人一样：进页面先看一眼（后台起 go2rtc），再点播放
            settle = time.time() + 0.6
            while time.time() < settle:
                app.processEvents()
                time.sleep(0.005)

            started = time.time()
            view.toggle()  # 等价于点播放键
            while time.time() - started < timeout:
                app.processEvents()
                if view.state == view.PLAYING:
                    break
                time.sleep(0.005)
            took = time.time() - started

            if view.state == view.PLAYING:
                times.append(took)
                size = f"{view._image.width()}×{view._image.height()}" if view._image else "?"
                say(f"  #{i + 1} [OK] 首帧 {took:5.2f}s  画面 {size}")
            else:
                say(f"  #{i + 1} [FAIL] {timeout:.0f}s 没出画面，状态 {view.state}，"
                    f"提示 {view._hint!r}")

            dialog.close()
            dialog.deleteLater()
            app.processEvents()
            # 让生产者自然停掉，下一轮才更接近真正的冷启动
            settle = time.time() + 2.5
            while time.time() < settle:
                app.processEvents()
                time.sleep(0.02)

        ok = len(times) == rounds
        if times:
            say("")
            say(f"首帧：{min(times):.2f}–{max(times):.2f}s，均值 "
                f"{sum(times) / len(times):.2f}s（{len(times)}/{rounds} 次成功）")
        return _finish(lines, ok=ok, report=report)
    except Exception as exc:  # noqa: BLE001 - 探针本身也不能哑火
        say(f"[FAIL] 探针自身出错：{type(exc).__name__}: {exc}")
        lines.extend("    " + ln for ln in traceback.format_exc().splitlines()[-8:])
        flush()
        return _finish(lines, ok=False, report=report)
    finally:
        if window is not None:
            try:
                window.close()
            except RuntimeError:  # 已经被销毁
                pass
        if app is not None:
            app.processEvents()


def _finish(lines: list[str], *, ok: bool, report: Path) -> int:
    """收尾：落一份报告，有 stdout 就顺带打印（窗口化 exe 里没有）。"""
    lines.append("结论：" + ("有画面" if ok else "没拿到画面"))
    try:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        lines.append(f"（报告写不进去：{exc}）")
    if sys.stdout is not None:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
        print("\n".join(lines))
        print(f"\n报告：{report}")
    return 0 if ok else 1


def autostart_requested(argv: list[str] | None = None) -> bool:
    """这次是不是被系统「启动项」拉起来的。

    开机时最不该做的就是抢焦点：用户可能正在看别的东西，一个全屏窗口弹出来
    会直接把他的工作顶掉。所以自启时窗口最小化出现，设备列表照常温热。
    """
    return autostart.FLAG in (sys.argv[1:] if argv is None else argv)


def present_window(window, autostart_mode: bool) -> None:
    """把主窗口摆出来。

    开机自启有两种形态，见设置页「常规 → 启动设置」：默认**直接进托盘**（登录后
    桌面上什么都不弹，托盘里有个图标）；用户把「启动时最小化到系统托盘」关掉，
    就退回最小化到任务栏。托盘不可用的环境（远程会话、精简系统）也只能退回
    最小化——不能"藏"成一个点不开的程序。
    """
    if autostart_mode:
        if window.start_in_tray():
            return
        window.showMinimized()
    else:
        window.show()


def _report_instance_blocked() -> None:
    """单实例挡下时把话说清楚。

    以前这里直接 ``return 0``：命令行里看起来"什么都没发生"，用故障注入测试时
    尤其容易被误判成"注入没生效"。这里只打印，不去动正在运行的实例。
    """
    from .core import dev_fault

    raw = os.environ.get(dev_fault.ENV, "").strip()
    injector = dev_fault.current() if raw else None
    if not raw:
        state = "本次没有设置故障注入。"
    elif injector is None:
        state = f"环境变量 {dev_fault.ENV}={raw!r} 已设置，但取值无效，注入未开启。"
    else:
        state = (
            f"环境变量 {dev_fault.ENV}={raw!r} 已设置（{injector.summary}），"
            "但它只对新启动的进程有效，没有作用到正在运行的这个实例。"
        )
    lines = [
        "[MiHomePanel] 已有一个实例在运行，本次启动已取消。",
        "[MiHomePanel] 要测试故障注入：请从托盘图标右键菜单选择「退出」"
        "（点窗口的关闭按钮只是收进托盘），确认程序完全退出后再运行。",
        f"[MiHomePanel] 注入状态：{state}",
    ]
    if sys.stderr is None:  # pythonw / 打包成 --windowed 时没有控制台
        return
    for line in lines:
        print(line, file=sys.stderr)
    sys.stderr.flush()


def _mark_test_mode(window: MainWindow) -> None:
    """注入开启时在标题栏留个标记，避免把假故障当成真故障。"""
    from .core import dev_fault

    injector = dev_fault.current()
    if injector is None:
        return
    title = f"米家控制台 · 测试模式（{injector.kind} 故障注入）"
    window.setWindowTitle(title)
    window.titlebar.set_title(title)


def main() -> int:
    args = sys.argv[1:]
    if "--self-check" in args:
        return self_check()
    if "--probe-camera" in args:
        import argparse

        parser = argparse.ArgumentParser(prog="MiHomePanel --probe-camera")
        parser.add_argument("--probe-camera", action="store_true")
        parser.add_argument("--rounds", type=int, default=1, help="跑几次（每次真冷启动）")
        parser.add_argument("--lane", choices=["", "lan", "remote"], default="",
                            help="临时覆盖取流通道；默认用设置里的值")
        parser.add_argument("--timeout", type=float, default=25.0, help="单轮等首帧上限")
        ns = parser.parse_args(args)
        return probe_camera(rounds=ns.rounds, lane=ns.lane, timeout=ns.timeout)

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("米家控制台")
    app.setApplicationDisplayName("米家控制台")
    app.setOrganizationName("MiHomePanel")
    app.setStyle("Fusion")
    # 系统可能是深色模式，但界面有自己的两套配色，先把调色板盖掉
    theme.apply_to_app(theme.LIGHT)

    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(13)
    app.setFont(font)

    # 同一份数据目录只允许一个窗口：第二个进程"叫醒"第一个然后自己退出。
    # 放在 MainWindow 之前——先抢令牌再建窗口，否则两个进程会各刷一轮设备列表。
    guard = SingleInstance()
    if not guard.acquire():
        guard.notify_existing()
        _report_instance_blocked()
        return 0

    # 启动项里存的是绝对路径，重新打包 / 挪目录之后会指向旧 exe（下次登录弹
    # "找不到文件"）。只在该功能本来就开着的时候改写，绝不顺手帮人打开。
    autostart.heal()

    window = MainWindow(theme.LIGHT)
    _mark_test_mode(window)

    def bring_forward() -> None:
        try:
            focus_window(window)
        except RuntimeError:  # 窗口已经销毁（正在退出）
            pass

    guard.activated.connect(bring_forward)
    present_window(window, autostart_requested())
    window.start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
