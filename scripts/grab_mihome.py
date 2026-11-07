"""米家 App（安卓端）界面采集工具。

只做只读采集：截图、UI 层级 XML、窗口焦点、屏幕参数。
绝不修改设备状态——不点开关、不触发自动化。

用法：
    python scripts/grab_mihome.py info
    python scripts/grab_mihome.py shot 01_home
    python scripts/grab_mihome.py ui 01_home
    python scripts/grab_mihome.py grab 01_home      # 截图 + XML 一起
    python scripts/grab_mihome.py tap 540 1200
    python scripts/grab_mihome.py swipe 540 1800 540 600
    python scripts/grab_mihome.py back
    python scripts/grab_mihome.py focus
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "reference" / "mihome-app"

ADB_CANDIDATES = [
    shutil.which("adb"),
    r"C:\Users\63153\platform-tools\adb.exe",
]


def adb_path() -> str:
    for cand in ADB_CANDIDATES:
        if cand and Path(cand).exists():
            return str(cand)
    sys.exit("找不到 adb，请把 platform-tools 加到 PATH")


ADB = adb_path()


def run(args: list[str], binary: bool = False) -> bytes | str:
    proc = subprocess.run(
        [ADB, *args], capture_output=True, timeout=60
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"adb {' '.join(args)} 失败：{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc.stdout if binary else proc.stdout.decode("utf-8", "replace")


def ensure_device() -> None:
    out = str(run(["devices"]))
    lines = [ln for ln in out.splitlines()[1:] if ln.strip()]
    if not lines:
        sys.exit("没有检测到设备，请确认 USB 已连接且已开启 USB 调试")
    if any("unauthorized" in ln for ln in lines):
        sys.exit("设备未授权：请在平板上点「允许 USB 调试」")
    if not any(re.search(r"\bdevice\b", ln) for ln in lines):
        sys.exit(f"设备状态异常：{lines}")


def cmd_info(_: argparse.Namespace) -> None:
    size = str(run(["shell", "wm", "size"])).strip()
    density = str(run(["shell", "wm", "density"])).strip()
    focus = str(run(["shell", "dumpsys", "window", "displays"])).strip()
    m = re.search(r"mCurrentFocus=(\S+)", focus)
    print(size)
    print(density)
    print("focus:", m.group(1) if m else "?")
    ver = str(
        run(["shell", "dumpsys", "package", "com.xiaomi.smarthome"])
    )
    v = re.search(r"versionName=(\S+)", ver)
    print("米家版本:", v.group(1) if v else "未安装/未取到")


def _clean(xml: str) -> str:
    """dump 会附带 "UI hierchary dumped to..." 之类的尾巴，截到 </hierarchy>。"""
    end = xml.find("</hierarchy>")
    if end < 0:
        return xml
    xml = xml[: end + len("</hierarchy>")]
    start = xml.find("<?xml")
    if start < 0:
        start = xml.find("<hierarchy")
    return xml[start:]


def _ui_xml() -> str:
    """优先走 /dev/tty，避免在设备上落文件。"""
    try:
        out = _clean(str(run(["exec-out", "uiautomator", "dump", "/dev/tty"])))
        if "</hierarchy>" in out:
            return out
    except SystemExit:
        pass
    tmp = "/data/local/tmp/_wb_ui.xml"
    run(["shell", "uiautomator", "dump", tmp])
    return _clean(str(run(["exec-out", "cat", tmp])))


def cmd_grab(ns: argparse.Namespace) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    png = run(["exec-out", "screencap", "-p"], binary=True)
    target = OUT / f"{ns.name}.png"
    target.write_bytes(png)
    xml = _ui_xml()
    (OUT / f"{ns.name}.xml").write_text(xml, encoding="utf-8")
    nodes = xml.count("<node")
    print(f"{target.relative_to(ROOT)}  {len(png) // 1024}KB  nodes={nodes}")


def cmd_shot(ns: argparse.Namespace) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    png = run(["exec-out", "screencap", "-p"], binary=True)
    target = OUT / f"{ns.name}.png"
    target.write_bytes(png)
    print(target.relative_to(ROOT), f"{len(png) // 1024}KB")


def cmd_ui(ns: argparse.Namespace) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    xml = _ui_xml()
    target = OUT / f"{ns.name}.xml"
    target.write_text(xml, encoding="utf-8")
    print(target.relative_to(ROOT), "nodes=", xml.count("<node"))


def cmd_tap(ns: argparse.Namespace) -> None:
    run(["shell", "input", "tap", str(ns.x), str(ns.y)])
    print("tapped", ns.x, ns.y)


def cmd_swipe(ns: argparse.Namespace) -> None:
    run(
        [
            "shell", "input", "swipe",
            str(ns.x1), str(ns.y1), str(ns.x2), str(ns.y2), str(ns.ms),
        ]
    )
    print("swiped")


def cmd_back(_: argparse.Namespace) -> None:
    run(["shell", "input", "keyevent", "4"])
    print("back")


def cmd_focus(_: argparse.Namespace) -> None:
    dump = str(run(["shell", "dumpsys", "window", "displays"]))
    m = re.search(r"mCurrentFocus=(\S+)", dump)
    print(m.group(1) if m else "?")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info").set_defaults(func=cmd_info)
    sub.add_parser("focus").set_defaults(func=cmd_focus)
    sub.add_parser("back").set_defaults(func=cmd_back)

    p = sub.add_parser("grab"); p.add_argument("name"); p.set_defaults(func=cmd_grab)
    p = sub.add_parser("shot"); p.add_argument("name"); p.set_defaults(func=cmd_shot)
    p = sub.add_parser("ui"); p.add_argument("name"); p.set_defaults(func=cmd_ui)

    p = sub.add_parser("tap"); p.add_argument("x", type=int); p.add_argument("y", type=int)
    p.set_defaults(func=cmd_tap)

    p = sub.add_parser("swipe")
    for k in ("x1", "y1", "x2", "y2"):
        p.add_argument(k, type=int)
    p.add_argument("--ms", type=int, default=300)
    p.set_defaults(func=cmd_swipe)

    ns = ap.parse_args()
    ensure_device()
    ns.func(ns)


if __name__ == "__main__":
    main()
