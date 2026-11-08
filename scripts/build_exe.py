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
    patched = (binary.parent / "BUILD.txt").is_file()
    log(f"go2rtc：{binary}")
    log(f"  开播补丁：{'有（首帧约 2 秒）' if patched else '没有（首帧 4.8–6.3 秒）'}")
    if not patched:
        log("  提醒：装的是官方构建，打出去别人也是 5 秒。要 2 秒先跑 build_go2rtc.py。")
    return binary


def fetch_go2rtc_license(dest: Path) -> bool:
    """取 go2rtc 的 MIT 原文放进包里。取不到就写个指向仓库的说明，不编造版权行。"""
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
        dest.write_text(
            f"本目录的 go2rtc.exe 来自 https://github.com/{GO2RTC_REPO}\n"
            "许可证：MIT。原文见该仓库的 LICENSE 文件。\n"
            f"（打包时没能取到原文：{type(exc).__name__}: {exc}）\n",
            encoding="utf-8",
        )
        log(f"  许可证：没取到原文，写了指向仓库的说明（{type(exc).__name__}）")
        return False


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
            "本发行包内的这一份带有本地补丁（pkg/xiaomi/miss/producer.go::Dial()：\n"
            "probe 之后重起一次视频流，消掉「等一个 GOP」的首帧延迟）。\n"
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
    fetch_go2rtc_license(target / "LICENSE")
    write_notice(app_dir / "NOTICE.txt", go2rtc)
    ours = ROOT / "LICENSE"
    if ours.is_file():
        shutil.copy2(ours, app_dir / "LICENSE")
    return exe


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

    size = sum(f.stat().st_size for f in app_dir.rglob("*") if f.is_file()) / 1048576
    log(f"\n产物：{app_dir}（{size:.0f} MB）")
    log(f"  go2rtc：{exe}")

    if args.skip_check:
        log("\n跳过自检（--skip-check）。发布前请手动跑一次 --self-check。")
        return 0
    if not run_self_check(app_dir):
        log("\n打包完成但自检未通过，先别发。常见原因见 README「打包成 exe」。")
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
