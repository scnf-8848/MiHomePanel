# SPDX-License-Identifier: GPL-3.0-or-later
"""把程序打成一个可以直接发给别人的 Windows 目录（PyInstaller onedir）。

打包只有三件事最容易漏，脚本逐个兜住：

1. **go2rtc 不会被自动带上**。它是外部进程（MIT），PyInstaller 只收集 Python
   依赖。少了它摄像头**完全不可用**（不是慢）。脚本把它复制到
   ``<dist>/MiHomePanel/go2rtc/go2rtc.exe`` —— 也就是 exe 旁边，`find_binary()`
   的第一顺位；顺手把它的 MIT 许可证一并放进去（再分发要求）。
2. **Qt 的 ffmpeg 多媒体后端是插件**，靠 PySide6 的 hook 收集；这里显式声明
   QtMultimedia / QtMultimediaWidgets，防止某个版本被裁掉。
3. **打完必须在产物上自检**：跑 ``MiHomePanel.exe --self-check``，确认找得到
   go2rtc、HEVC 解得开。开发机上"能跑"和包里"能跑"是两件事。
4. **窗口化 exe 的 stdout 是 ``None``**（见 ``mihome/_stdio.py``）。自检那次是带管道
   跑的，stdout 反而是有效句柄，所以**自检通过不代表双击能开**。打完还要按"无控制台"
   再拉一次窗口（``DETACHED_PROCESS``，等价于资源管理器双击），读它的窗口标题：
   出现 "Unhandled exception" 就是启动就崩。

用法：
  .venv/Scripts/python.exe scripts/build_exe.py
  .venv/Scripts/python.exe scripts/build_exe.py --console      # 带控制台，排障用
  .venv/Scripts/python.exe scripts/build_exe.py --onefile      # 单文件（启动慢些）
  .venv/Scripts/python.exe scripts/build_exe.py --go2rtc <path> --skip-check
  .venv/Scripts/python.exe scripts/build_exe.py --no-slim      # 不裁剪，排障用

体积：PyInstaller 收全量约 119 MB，裁剪后约 81 MB，压成 zip 约 45 MB。
裁剪**只删文件、不改代码**，清单在 ``_TRIM`` 里，每条写明依据；删错了的表现是
自检或冒烟失败，所以两者都在裁剪之后跑。

产物：``dist/MiHomePanel/``（整个目录一起发，不是只发 exe）。
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mihome.core import paths
from mihome.core.camera import find_binary

APP_NAME = "MiHomePanel"
GO2RTC_REPO = "AlexxIT/go2rtc"
GO2RTC_LICENSE_API = f"https://api.github.com/repos/{GO2RTC_REPO}/license"
UA = "MiHomePanel-builder"


def log(msg: str) -> None:
    print(msg, flush=True)


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        log("没装 PyInstaller，先装一份（构建工具，不进 requirements）…")
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "pyinstaller"],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
        )
        if proc.returncode != 0:
            raise SystemExit(f"装 PyInstaller 失败：\n{(proc.stderr or proc.stdout)[-1500:]}")


def resolve_go2rtc(explicit: str) -> Path:
    binary = Path(explicit).expanduser() if explicit else find_binary()
    if binary is None or not Path(binary).is_file():
        raise SystemExit(
            "没找到 go2rtc。先跑 scripts/build_go2rtc.py（补丁版，首帧 2 秒）"
            "或 scripts/install_go2rtc.py（官方版，首帧 5 秒），或用 --go2rtc 指过去。"
        )
    binary = Path(binary)
    record = binary.parent / "BUILD.txt"
    patched = record.is_file()
    log(f"go2rtc：{binary}")
    if patched:
        text = record.read_text(encoding="utf-8", errors="replace")
        has_ptz = "ptz" in text
        log(
            "  本地补丁：有（首帧约 2 秒"
            + ("，含云台）" if has_ptz else "；**没有云台补丁**，详情页不会建方向盘）")
        )
    else:
        log("  本地补丁：没有（首帧 4.8–6.3 秒，也没有云台）")
        log("  提醒：装的是官方构建，打出去别人也是 5 秒。要 2 秒先跑 build_go2rtc.py。")
    return binary


def fetch_go2rtc_license(dest: Path, fallback: Path | None = None) -> bool:
    """取 go2rtc 的 MIT 原文放进包里。取不到就用本地已有的原文；
    再没有就写个指向仓库的说明，不编造版权行。"""
    try:
        req = urllib.request.Request(
            GO2RTC_LICENSE_API,
            headers={"User-Agent": UA, "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
        text = base64.b64decode(data["content"]).decode("utf-8")
        dest.write_text(text, encoding="utf-8")
        log(f"  许可证：{data.get('name')}（{(data.get('license') or {}).get('spdx_id')}）")
        return True
    except Exception as exc:  # noqa: BLE001 - 取不到不该让打包失败，但要留痕
        if fallback is not None and fallback.is_file():
            shutil.copy2(fallback, dest)
            log(f"  许可证：API 不可用，用本地原文 {fallback.name}")
            return True
        dest.write_text(
            f"本目录的 go2rtc.exe 来自 https://github.com/{GO2RTC_REPO}\n"
            "许可证：MIT。原文见该仓库的 LICENSE 文件。\n"
            f"（打包时没能取到原文：{type(exc).__name__}: {exc}）\n",
            encoding="utf-8",
        )
        log(f"  许可证：没取到原文，写了指向仓库的说明（{type(exc).__name__}）")
        return False


def write_go2rtc_notice(dest: Path, go2rtc: Path) -> None:
    """go2rtc 子目录自己的来源说明，和程序根目录的 NOTICE 分开。"""
    record = go2rtc.parent / "BUILD.txt"
    patched = record.is_file()
    build = record.read_text(encoding="utf-8").strip() if patched else ""
    dest.write_text(
        "go2rtc —— 第三方组件（MIT）\n"
        f"来源：https://github.com/{GO2RTC_REPO}\n"
        "用途：把小米摄像头的 P2P 码流转成本机 RTSP；由 MiHomePanel 作为独立进程启动。\n"
        + (
            "本发行包内的这一份带有本地补丁：\n"
            "  · pkg/xiaomi/miss/producer.go::Dial()：probe 之后重起一次视频流，\n"
            "    消掉「等一个 GOP」的首帧延迟；\n"
            "  · pkg/xiaomi/miss/client.go：云台控制（0x112 帧 + 命令通道常驻消费），\n"
            "    对外暴露 GET /api/xiaomi/ptz。\n"
            f"构建信息：\n{build}\n" if patched else "本发行包内的是上游官方构建。\n"
        )
        + "\n许可证原文：同目录 LICENSE。\n"
        "本项目 MiHomePanel 以 GPL-3.0-or-later 发布，见上一级 LICENSE / NOTICE.txt。\n",
        encoding="utf-8",
    )


def write_notice(dest: Path, go2rtc: Path) -> None:
    patched = (go2rtc.parent / "BUILD.txt").is_file()
    build = ""
    if patched:
        build = (go2rtc.parent / "BUILD.txt").read_text(encoding="utf-8").strip()
    dest.write_text(
        "MiHomePanel —— 米家设备桌面控制台\n"
        "本程序以 GPL-3.0-or-later 发布，完整条款见同目录的 LICENSE。\n"
        "\n"
        f"同目录的 go2rtc/go2rtc.exe 是第三方组件 {GO2RTC_REPO}（MIT），\n"
        "用于把小米摄像头的 P2P 码流转成本机 RTSP。它是独立进程，由本程序启动。\n"
        + (
            "本发行包内的这一份带有本地补丁：\n"
            "  · pkg/xiaomi/miss/producer.go::Dial()：probe 之后重起一次视频流，\n"
            "    消掉「等一个 GOP」的首帧延迟；\n"
            "  · pkg/xiaomi/miss/client.go：云台控制（0x112 帧 + 命令通道常驻消费），\n"
            "    对外暴露 GET /api/xiaomi/ptz。\n"
            f"构建信息：\n{build}\n" if build else "本发行包内的是上游官方构建。\n"
        )
        + "\n许可证原文：go2rtc/LICENSE；本项目：LICENSE。\n",
        encoding="utf-8",
    )


LAUNCHER = '''# 由 scripts/build_exe.py 生成，仅用于打包
"""打包入口：先兜住 stdout，再把启动期的异常写成日志。

窗口化的 exe 没有控制台，任何 import 期异常都只会弹出 PyInstaller 那句
"Unhandled exception in script"（英文、字小、还不能复制），用户和你都拿不到
有效信息。所以这里自己接住，落一份 crash.log 再给个能读懂的对话框。
"""

import multiprocessing
import os
import sys
import traceback
from pathlib import Path


def _log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "MiHomePanel" / "logs"


def _report(exc: BaseException) -> None:
    text = "".join(traceback.format_exception(exc))
    try:
        target = _log_dir() / "crash.log"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        where = "完整记录：" + str(target)
    except OSError:
        where = "（日志文件也写不进去）"
    tail = text.strip().splitlines()[-1] if text.strip() else str(exc)
    message = "MiHomePanel 启动失败：" + tail + "\\n\\n" + where
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "MiHomePanel", 0x10)
        except Exception:  # noqa: BLE001 - 弹不出窗也别再抛
            pass


if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        from mihome.app import main

        code = main()
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - 兜住一切，落盘比 traceback 有用
        _report(exc)
        code = 1
    sys.exit(code)
'''


def write_launcher(work: Path) -> Path:
    launcher = work / "_launcher.py"
    launcher.write_text(LAUNCHER, encoding="utf-8")
    return launcher


def run_pyinstaller(args: argparse.Namespace, launcher: Path, dist: Path, work: Path) -> None:
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--name",
        APP_NAME,
        "--distpath",
        str(dist),
        "--workpath",
        str(work / "pyinstaller"),
        "--specpath",
        str(work),
        "--paths",
        str(ROOT),
        "--hidden-import",
        "PySide6.QtMultimedia",
        "--hidden-import",
        "PySide6.QtMultimediaWidgets",
    ]
    if args.force_clean:
        # 默认不开：它会清 `%LOCALAPPDATA%\pyinstaller` 的缓存（几十个目录），
        # 有些环境的安全策略会拦掉这种批量删除。产物是干净的，一般不需要。
        cmd.append("--clean")
    cmd.append("--console" if args.console else "--windowed")
    if args.onefile:
        cmd.append("--onefile")
    if args.icon:
        cmd.extend(["--icon", args.icon])
    cmd.append(str(launcher))
    log("\n" + " ".join(cmd) + "\n")
    proc = subprocess.run(cmd, check=False, cwd=str(ROOT))
    if proc.returncode != 0:
        raise SystemExit(f"PyInstaller 失败（退出码 {proc.returncode}）")


def stage_go2rtc(app_dir: Path, go2rtc: Path) -> Path:
    """放到 exe 旁边：find_binary() 的第一顺位，用户也能看见/替换它。"""
    target = app_dir / "go2rtc"
    target.mkdir(parents=True, exist_ok=True)
    exe = target / ("go2rtc.exe" if sys.platform == "win32" else "go2rtc")
    shutil.copy2(go2rtc, exe)
    # 构建记录跟着走：自检靠它区分"补丁版 2 秒"和"官方版 5 秒"，用户也能看到来源
    record = go2rtc.parent / "BUILD.txt"
    if record.is_file():
        shutil.copy2(record, target / "BUILD.txt")
    fetch_go2rtc_license(target / "LICENSE", go2rtc.parent / "LICENSE")
    write_go2rtc_notice(target / "NOTICE.txt", go2rtc)
    write_notice(app_dir / "NOTICE.txt", go2rtc)
    ours = ROOT / "LICENSE"
    if ours.is_file():
        shutil.copy2(ours, app_dir / "LICENSE")
    return exe


#: 打包后可以从产物里删掉的文件。每条都写**为什么能删**。
#:
#: 这一节最容易退化成迷信式清理：删错了不会在开发机上暴露，只在别人机器上白屏或
#: 启动就崩，而且极难远程定位。所以删除依据统一是「读 PE 导入表，确认没有任何别的
#: .dll / .pyd 引用它」，不是「看起来没用」。核对方法见 docs/DEVELOPMENT.md「体积」。
_TRIM: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            "_internal/PySide6/Qt6Qml.dll",
            "_internal/PySide6/Qt6QmlModels.dll",
            "_internal/PySide6/Qt6QmlMeta.dll",
            "_internal/PySide6/Qt6QmlWorkerScript.dll",
            "_internal/PySide6/Qt6Quick.dll",
            "_internal/PySide6/Qt6OpenGL.dll",
        ),
        "QML / Quick / OpenGL 运行时（只用 Widgets，PE 导入表里没有上游）",
    ),
    (
        ("_internal/PySide6/Qt6Pdf.dll", "_internal/PySide6/plugins/imageformats/qpdf.dll"),
        "PDF：只被 qpdf 图片插件引用，插件一起删",
    ),
    (
        (
            "_internal/PySide6/Qt6VirtualKeyboard.dll",
            "_internal/PySide6/plugins/platforminputcontexts/*.dll",
        ),
        "虚拟键盘：物理键盘输入，输入法插件一起删",
    ),
    (
        (
            "_internal/PySide6/plugins/imageformats/qtiff.dll",
            "_internal/PySide6/plugins/imageformats/qwebp.dll",
            "_internal/PySide6/plugins/imageformats/qicns.dll",
            "_internal/PySide6/plugins/imageformats/qtga.dll",
            "_internal/PySide6/plugins/imageformats/qwbmp.dll",
        ),
        "图片格式：设备图标是 PNG/JPEG，qpng 内建于 Qt6Gui",
    ),
    (
        (
            "_internal/PySide6/plugins/platforms/qdirect2d.dll",
            "_internal/PySide6/plugins/platforms/qminimal.dll",
            "_internal/PySide6/plugins/platforms/qoffscreen.dll",
        ),
        "平台插件：只用 qwindows（offscreen 只有开发机截图脚本会设）",
    ),
    (
        ("_internal/PySide6/plugins/tls",),
        "Qt 侧 TLS：云端走 Python 的 requests，Qt 只连本机 go2rtc",
    ),
    (
        ("_internal/PySide6/plugins/generic/*.dll",),
        "输入设备插件：触控板/触摸屏的 TUIO，桌面端不用",
    ),
    (
        (
            "_internal/PIL/_avif*.pyd",
            "_internal/PIL/_webp*.pyd",
            "_internal/PIL/_imagingcms*.pyd",
            "_internal/PIL/_imagingtk*.pyd",
        ),
        "PIL 编解码扩展：只用来存设备图标 PNG（AVIF 一个就 1.8M）",
    ),
    (
        ("_internal/tzdata",),
        "时区数据库：Windows 上 tzlocal 走系统时区 API，不读 zoneinfo 数据",
    ),
)

#: 软件 OpenGL 后备库单独列：它是**兼容性**保险，不是多余文件。
#: 有显卡驱动的机器永远用不到它；虚拟机 / 远程桌面 / 老驱动才会走到这里。
#: 默认删（省 5.3M），`--keep-sw-opengl` 保留。
_SW_OPENGL = ("_internal/PySide6/opengl32sw.dll",)

#: Qt 自带翻译只留这些。程序没有 QTranslator，理论上一份都不用留；
#: 留着是给系统标准对话框（消息框按钮等）用的，以后接 QTranslator 也不用回改。
_QT_TRANSLATIONS = ("qt_zh_CN.qm", "qtbase_zh_CN.qm", "qtmultimedia_zh_CN.qm", "qt_en.qm")


def _expand(app_dir: Path, patterns: tuple[str, ...]) -> list[Path]:
    """把清单里的路径 / glob 展开成实际存在的文件与目录。"""
    found: list[Path] = []
    for pattern in patterns:
        if "*" in pattern:
            found.extend(p for p in app_dir.glob(pattern) if p.is_file())
        else:
            p = app_dir / pattern
            if p.is_file():
                found.append(p)
            elif p.is_dir():
                found.extend(x for x in p.rglob("*") if x.is_file())
    return found


def _drop_translations(app_dir: Path) -> tuple[float, int]:
    """只留中文与英文兜底，其余 120 多个 .qm 删掉。返回 (省下的字节, 文件数)。"""
    folder = app_dir / "_internal/PySide6/translations"
    if not folder.is_dir():
        return 0.0, 0
    keep = {n for n in _QT_TRANSLATIONS if (folder / n).is_file()}
    if not keep:
        # 白名单一个都没命中，说明这版布局改了；宁可不删，也不要删光
        return 0.0, 0
    saved, count = 0.0, 0
    for qm in folder.glob("*.qm"):
        if qm.name in keep:
            continue
        saved += qm.stat().st_size
        qm.unlink()
        count += 1
    return saved, count


def slim(app_dir: Path, *, keep_sw_opengl: bool = False) -> list[tuple[str, float]]:
    """删掉产物里确定用不到的文件，返回 ``[(说明, 省下的 MB)]``（按省得多排前面）。"""
    rows: list[tuple[str, float]] = []

    saved, count = _drop_translations(app_dir)
    if count:
        rows.append((f"Qt 翻译：只留中文 + 英文，删掉 {count} 个 .qm", saved / 1048576))

    entries = list(_TRIM)
    if not keep_sw_opengl:
        entries.append((_SW_OPENGL, "软件 OpenGL 后备库（--keep-sw-opengl 可留）"))
    for patterns, reason in entries:
        files = _expand(app_dir, patterns)
        if not files:
            continue
        saved = sum(f.stat().st_size for f in files)
        for f in files:
            f.unlink(missing_ok=True)
        rows.append((reason, saved / 1048576))

    # 删完留下的空目录（比如整个 platforminputcontexts）对 Qt 无害，但看着脏
    for folder in (app_dir / "_internal/PySide6/plugins").rglob("*"):
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()

    return sorted(rows, key=lambda r: -r[1])


def _window_titles(pid: int) -> list[str]:
    """列出属于该进程的可见顶层窗口标题（Windows）。"""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    titles: list[str] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _callback(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                titles.append(buf.value)
        return True

    user32.EnumWindows(callback_type(_callback), 0)
    return titles


BAD_TITLES = ("Unhandled exception", "启动失败", "Fatal error", "Error")


def run_window_smoke(app_dir: Path, wait: float = 12.0) -> bool:
    """按"双击"的方式启动一次：不带控制台、不继承标准句柄。

    自检那次是用管道抓输出的，stdout 反而是有效句柄；真正的双击没有任何句柄，
    PyInstaller 会把 ``sys.stdout`` 置成 None —— 那是完全另一条路径。所以这里用
    ``DETACHED_PROCESS`` 起，然后看它有没有弹出错误窗口。
    """
    if sys.platform != "win32":
        log("窗口冒烟：跳过（只在 Windows 上做）")
        return True
    exe = app_dir / f"{APP_NAME}.exe"
    log(f"窗口冒烟：无控制台启动 {exe.name}")
    crash = paths.log_dir() / "crash.log"
    # 先清掉上次的：这次启动要是又生成了它，说明启动期就崩了（启动器写的）
    crash.unlink(missing_ok=True)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0)
    proc = subprocess.Popen([str(exe)], cwd=str(app_dir), creationflags=flags, close_fds=True)
    titles: list[str] = []
    bad = ""
    deadline = time.time() + wait
    try:
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.6)
            titles = _window_titles(proc.pid)
            if titles:
                bad = next((t for t in titles if any(b in t for b in BAD_TITLES)), "")
                break
        code = proc.poll()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    if bad or crash.is_file():
        log(f"  [FAIL] 启动期崩溃（窗口标题：{titles or '无'}）")
        if crash.is_file():
            tail = crash.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-3:]
            log("  crash.log：\n    " + "\n    ".join(tail))
        return False
    if titles:
        log(f"  [OK] 窗口已出现：{' / '.join(titles)}")
        return True
    log(f"  [FAIL] {wait:.0f} 秒内没出现窗口（退出码 {code}）")
    return False


def run_self_check(app_dir: Path) -> bool:
    exe = app_dir / f"{APP_NAME}.exe"
    report = paths.log_dir() / "self-check.txt"
    report.unlink(missing_ok=True)
    log(f"自检：{exe.name} --self-check")
    try:
        proc = subprocess.run(
            [str(exe), "--self-check"], check=False, capture_output=True, text=True,
            errors="replace", timeout=180,
        )
    except subprocess.TimeoutExpired:
        log("  自检超时（180 秒）")
        return False
    text = report.read_text(encoding="utf-8") if report.is_file() else ""
    if text:
        log("\n" + text)
    else:
        log(f"  没有拿到报告文件 {report}")
        log(f"  进程输出：{(proc.stdout or '')[-800:]}{(proc.stderr or '')[-800:]}")
    ok = proc.returncode == 0 and "自检通过" in text
    log(f"  退出码 {proc.returncode} → {'通过' if ok else '未通过'}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="打包成可发布的 Windows 目录")
    parser.add_argument("--onefile", action="store_true", help="单文件（启动慢，默认关）")
    parser.add_argument("--console", action="store_true", help="带控制台窗口（排障用）")
    parser.add_argument("--go2rtc", default="", help="要打包的 go2rtc.exe（默认自动找）")
    parser.add_argument("--icon", default="", help=".ico 图标")
    parser.add_argument("--dist", default="", help="产物目录，默认 dist/")
    parser.add_argument("--work", default="", help="中间目录，默认 build/")
    parser.add_argument("--skip-check", action="store_true", help="跳过产物自检")
    parser.add_argument("--skip-smoke", action="store_true", help="跳过窗口冒烟（无控制台启动）")
    parser.add_argument(
        "--no-slim",
        action="store_true",
        help="不裁剪用不到的文件（排障用：怀疑是裁剪搞坏的就用这个对比）",
    )
    parser.add_argument(
        "--keep-sw-opengl",
        action="store_true",
        help="保留软件 OpenGL 后备库（虚拟机 / 远程桌面里才需要，多 5.3 MB）",
    )
    parser.add_argument(
        "--force-clean",
        action="store_true",
        help="让 PyInstaller 清掉它的缓存再编（有些环境会拦批量删除，默认不开）",
    )
    args = parser.parse_args()

    dist = Path(args.dist).expanduser() if args.dist else ROOT / "dist"
    work = Path(args.work).expanduser() if args.work else ROOT / "build"
    work.mkdir(parents=True, exist_ok=True)

    ensure_pyinstaller()
    go2rtc = resolve_go2rtc(args.go2rtc)
    if dist.exists():
        log(f"清掉旧产物 {dist}")
        try:
            shutil.rmtree(dist)
        except Exception as exc:  # noqa: BLE001 - 删不掉不该炸，但要让人知道
            # 有些环境的安全策略会拦大量文件的删除；换个空目录重打更省事
            log(
                f"  没删干净（{type(exc).__name__}: {exc}）。"
                "换个新目录重打：--dist dist2"
            )

    run_pyinstaller(args, write_launcher(work), dist, work)
    app_dir = dist if args.onefile else dist / APP_NAME
    exe = stage_go2rtc(app_dir, go2rtc)

    def size_of() -> float:
        return sum(f.stat().st_size for f in app_dir.rglob("*") if f.is_file()) / 1048576

    raw = size_of()
    if args.no_slim:
        log("\n跳过裁剪（--no-slim）：产物保持 PyInstaller 收进来的原样")
    else:
        rows = slim(app_dir, keep_sw_opengl=args.keep_sw_opengl)
        saved = sum(mb for _, mb in rows)
        log(f"\n裁剪：{len(rows)} 组，省 {saved:.0f} MB（{raw:.0f} → {size_of():.0f} MB）")
        for reason, mb in rows:
            log(f"  -{mb:5.1f} MB  {reason}")
        if not args.keep_sw_opengl:
            log("  提醒：删了软件 OpenGL 后备库。虚拟机 / 远程桌面里可能没画面，")
            log("        要发到那种环境请加 --keep-sw-opengl 重打（多 5.3 MB）。")

    log(f"\n产物：{app_dir}（{size_of():.0f} MB）")
    log(f"  go2rtc：{exe}")
    log("  分发：整个目录压成 zip（约 45 MB）。go2rtc 不要单独 UPX——")
    log("        压完启动慢 400ms 还容易被杀软误报，而 zip 后只差 2 MB。")

    if args.skip_check:
        log("\n跳过自检（--skip-check）。发布前请手动跑一次 --self-check。")
        return 0
    if not run_self_check(app_dir):
        log("\n打包完成但自检未通过，先别发。常见原因见 docs/DEVELOPMENT.md「打包成 exe 的实现」。")
        if not args.no_slim:
            log("如果加裁剪之前是好的，先用 --no-slim 重打一份对比——可能是清单删过头了。")
        return 1
    if not args.skip_smoke and not run_window_smoke(app_dir):
        log(
            "\n打包完成但双击会崩（自检发现不了这类问题）。"
            f"\n先看 {paths.log_dir() / 'crash.log'}，常见原因是某个库在导入期碰了"
            "\n sys.stdout / sys.stderr —— 窗口化 exe 里它们是 None，见 mihome/_stdio.py。"
        )
        return 1
    log("\n可以发了：整个目录一起压缩发给别人。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
