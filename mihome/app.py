# SPDX-License-Identifier: GPL-3.0-or-later
"""程序入口。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from .core import applog, autostart
from .core.single_instance import SingleInstance, focus_window
from .ui import app_icon, fonts, theme
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

    from .core import diag, paths
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
        # 打了补丁的构建会留一份 BUILD.txt，用它区分"2 秒"和"5 秒"、有没有云台
        build_txt = binary.parent / "BUILD.txt"
        if build_txt.is_file():
            # 老格式 `patch=<一个补丁>`，新格式 `patches=<a>,<b>`
            line = next(
                (
                    ln for ln in build_txt.read_text(encoding="utf-8").splitlines()
                    if ln.startswith(("patch=", "patches="))
                ),
                "",
            )
            say(f"       本地补丁：{line.split('=', 1)[1] if line else '（BUILD.txt 里没写）'}")
        else:
            say("       本地补丁：没有 → 首帧会是 4.8–6.3 秒、云台不可用（见 docs/dev/camera.md「开播补丁」）")

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
    # 报告本身也可能写不进去（2026-11-21：数据目录被拒写时，连自检报告都落不下，
    # `_launcher` 只好弹一个"MiHomePanel 启动失败"——而真正的原因就是写不了盘，
    # 那条弹窗把"环境问题"说成了"程序坏了"）。落不下去就退到 %TEMP%。
    try:
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        where = str(report)
    except OSError as exc:
        where = f"%TEMP%/{diag.REPORT_NAME}（{type(exc).__name__}: {exc}）"

    diag.write_report(
        {
            "mode": "self-check",
            "self_check_ok": "是" if ok else "否",
            "self_check_report": where,
            "data_dir": str(paths.data_dir()),
            "log_dir": str(paths.log_dir()),
        }
    )
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
        # 必须在 QApplication 之前：平台插件只在构造时读一次环境变量
        fonts.prepare_engine()
        app = QApplication.instance() or QApplication(sys.argv[:1])
        app.setStyle("Fusion")
        fonts.apply_to(app)
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


def _report_startup(window) -> None:
    """把这次启动的现场写进 ``%TEMP%/MiHomePanel-diag.txt``。

    为什么不信 ``logs/``：2026-11-21 那次"设置存不住"里，``logs/prefs.log``
    **一条失败记录都没有**——因为日志和数据文件在同一个目录，一起写不进去。
    诊断落 ``%TEMP%``，至少有一份东西能看：进程是 Medium 还是 High、
    有没有被塞进 AppContainer、``LOCALAPPDATA`` 指向哪。
    """
    from .core import applog, diag

    prefs = getattr(window, "prefs", None)
    extra: dict[str, str] = {}
    if prefs is not None:
        extra["prefs_path"] = str(prefs.path)
        extra["prefs_writable"] = "是" if prefs.writable else "否"
        if getattr(prefs, "probe_error", ""):
            extra["probe_error"] = prefs.probe_error
    report = diag.write_report(extra)
    if report is not None:
        applog.log("startup", f"启动现场已写入 {report}")


def _report_runtime(window) -> None:
    """跑起来几秒后再记一份现场：托盘的真实状态 + 挂件在不在。

    启动瞬间记的那份没有意义——``install()`` 的重试链最长 12 秒才认输，
    那之前 `active` 一定是 False。
    """
    from .core import diag

    extra: dict[str, str] = {}
    tray = getattr(window, "tray", None)
    if tray is not None:
        try:
            extra["tray"] = tray.describe_state()
        except Exception as exc:  # noqa: BLE001 - 诊断不该影响运行
            extra["tray"] = f"取状态失败：{type(exc).__name__}: {exc}"
    panel = getattr(window, "widget_panel", None)
    if panel is not None:
        extra["widget_visible"] = "是" if panel.isVisible() else "否"
        extra["main_visible"] = "是" if window.isVisible() else "否"
    diag.write_report(extra)


def _warn_if_readonly(window) -> None:
    """数据目录写不进去时**当面说**，别让用户改了半天设置、重启全没了。

    2026-11-21：普通权限启动的 exe 里 ``prefs.json`` 写不进去（``save()`` 把
    ``OSError`` 吞了），表现是"设置重启就还原""挂件里加的设备记不住"。现在
    ``Prefs.probe_writable()`` 在启动时真写一个小文件探一次，失败就走这里。

    用非模态 ``open()``：这条提示不该卡住启动，用户看完继续用就是——他只是需要
    知道"这次的改动不会留下"。
    """
    prefs = getattr(window, "prefs", None)
    if prefs is None or prefs.writable:
        return
    from PySide6.QtWidgets import QMessageBox

    from .core import applog, diag

    if diag.low_integrity():
        # 2026-11-21 的真根因：exe（或其所在目录）带 Mandatory Label\Low，
        # Windows 于是以 Low 完整性启动它。这时"数据目录写不进去"只是表象，
        # 同一原因还让托盘挂不上、关窗把挂件带走。给一条能直接照做的路，
        # 比列一堆"常见原因"有用。
        _warn_low_integrity(window, prefs, diag)
        return

    detail = getattr(prefs, "probe_error", "") or "（没有记录到具体错误）"
    box = QMessageBox(window)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle("设置无法保存")
    box.setText("数据目录写不进去，这次的改动不会保存。")
    box.setInformativeText(
        f"{prefs.path.parent}\n\n"
        f"系统返回：{detail}\n"
        f"进程身份：{diag.identity_summary()}\n\n"
        "常见原因：安全软件（受控文件夹访问 / 勒索防护）拦了写入、目录权限被改过、"
        "或者程序用管理员身份启动过而数据目录属于另一个账户。\n\n"
        "现场：logs/prefs.log，以及 %TEMP%/MiHomePanel-diag.txt（含进程权限级别）。"
    )
    box.setStandardButtons(QMessageBox.Ok)
    box.open()  # 非模态：不挡启动

    # 一次误判不该判死整个会话（2026-11-21 第二轮）：启动那一瞬间杀软 / 搜索
    # 索引器正好在扫这个目录，探针会拿到一次 EACCES，可用户之后改的每一样东西
    # 其实都存得进去。3 秒后复查一次，写得了就把话收回去。
    def _recheck() -> None:
        if not prefs.probe_writable():
            return
        prefs.writable = True
        box.close()
        applog.log("startup", "复查：数据目录其实可写，撤回「设置无法保存」")

    QTimer.singleShot(3000, _recheck)


def _warn_low_integrity(window, prefs, diag) -> None:
    """被 Windows 以降级身份跑起来了——说清原因，并给一条能照做的修法。

    这时用户的每一处"存不住"都是同一个原因，跟他有没有权限、装没装杀软无关：
    文件（或它所在的目录）带 ``Mandatory Label\\Low Mandatory Level``，Windows
    就以 Low 启动它。Low 进程写 ``%LOCALAPPDATA%`` 是 no-write-up（一律拒绝访问），
    也不能向 Medium 的 explorer 发窗口消息，所以托盘图标同样挂不上。

    改 SACL 要 ``SeSecurityPrivilege``（管理员），程序自己改不了自己，只能告诉
    用户这一条命令。顺带把命令塞进"复制"按钮——手打 ``icacls`` 那串容易错。
    """
    import sys
    from pathlib import Path

    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QMessageBox

    from .core import applog

    exe_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
    command = f'icacls "{exe_dir}" /setintegritylevel (OI)(CI)Medium /T /C'
    applog.log(
        "startup",
        f"进程完整性级别是 Low —— 就是这个 exe（{sys.executable}）被 Windows 降级启动，"
        f"所以数据目录写不进去、托盘也挂不上。修法：{command}",
    )

    box = QMessageBox(window)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle("程序被以低权限运行")
    box.setText("这个程序被 Windows 以「低」完整性级别启动了。")
    box.setInformativeText(
        "后果：设置存不住、挂件里加的设备记不住、托盘图标也挂不上。\n"
        "这不是权限设置的问题，是 exe 所在的目录带了 Low 完整性标签，exe 继承了下来。\n\n"
        f"· 这个 exe：{sys.executable}\n"
        f"· 进程身份：{diag.identity_summary()}\n"
        f"· 数据目录：{prefs.path.parent}\n\n"
        "修法（管理员 cmd 跑一次，然后重新打开程序）：\n"
        f"{command}\n\n"
        "如果你是从源码目录重新打包的，把上面那个路径换成本仓库根目录再跑一遍——\n"
        "dist 的标签是从根继承来的，只改 dist 的话下次重建又继承回去。\n\n"
        "现场：%TEMP%/MiHomePanel-diag.txt"
    )
    copy = box.addButton("复制修复命令", QMessageBox.ActionRole)
    box.setStandardButtons(QMessageBox.Ok)

    def _on_clicked(button) -> None:
        if button is copy:
            QGuiApplication.clipboard().setText(command)

    box.buttonClicked.connect(_on_clicked)
    box.open()



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

    # Windows 系统通知（后台提醒 / 收进托盘那条气泡）头部的图标取自 AUMID 的
    # IconUri，不是托盘图标——不显式注册的话 Windows 会按 exe 路径生成一个
    # "第一次弹通知时的图标快照"，换图标后一直显示旧的。必须在建窗口、挂托盘
    # 之前做，所以放在 QApplication 之前；源码运行与打包版走同一条路径。
    if not app_icon.install_notification_identity("米家控制台"):
        applog.log("tray", "通知身份（AUMID）没注册上，系统通知会退回按 exe 生成的图标")

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    # 必须在 QApplication 之前：平台插件只在构造时读一次环境变量
    fonts.prepare_engine()
    app = QApplication(sys.argv)
    # 窗口化的 exe 没有控制台，Qt 的 qWarning 一直是"写完就没了"（2026-11-21 查
    # 托盘图标查不出来，就是因为最关键的那句提示被吞掉了）。装一个 handler 落
    # logs/qt.log，代价只有几条文件写入。
    applog.install_qt_handler()
    app.setApplicationName("米家控制台")
    app.setApplicationDisplayName("米家控制台")
    app.setOrganizationName("MiHomePanel")
    # 任务栏 / Alt-Tab / 窗口左上角用的那一枚。必须在建窗口之前设：晚于窗口
    # 创建时，任务栏上的按钮已经用默认空白图标注册过了。
    app.setWindowIcon(app_icon.load())
    app.setStyle("Fusion")
    fonts.apply_to(app)
    # 系统可能是深色模式，但界面有自己的两套配色，先把调色板盖掉
    theme.apply_to_app(theme.LIGHT)

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
    _report_startup(window)
    _warn_if_readonly(window)

    def bring_forward() -> None:
        try:
            focus_window(window)
        except RuntimeError:  # 窗口已经销毁（正在退出）
            pass

    guard.activated.connect(bring_forward)
    present_window(window, autostart_requested())
    window.start()
    # 3 秒后再记一份"跑起来之后"的现场：托盘到底挂上没有、挂件在不在屏幕上。
    # 这一段必须延后——`install()` 的重试链最长要 12 秒，启动瞬间的结论会误导。
    QTimer.singleShot(3000, lambda: _report_runtime(window))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
