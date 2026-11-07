# SPDX-License-Identifier: GPL-3.0-or-later
"""下载可选的 go2rtc 外部进程（MIT），放到用户数据目录供程序直接调用。

摄像头实时画面依赖 go2rtc 把米家 P2P 码流转成本机 RTSP。它是独立进程、MIT 许可，
不随本项目分发；GitHub Releases 里的就是官方构建。

**为什么走 assets API 而不是 releases/download 链接**：后者会 302 到
``objects.githubusercontent.com``。本机网络实测 ``github.com`` 和几个常见镜像都不通，
只有 ``api.github.com`` 可用；给它加 ``Accept: application/octet-stream`` 会直接回
资产内容（8.1MB 正常下完）。这不是通用加速手段，只是在本机网络下能走通的那条。

用法：
  .venv/Scripts/python.exe scripts/install_go2rtc.py
  .venv/Scripts/python.exe scripts/install_go2rtc.py --force
  .venv/Scripts/python.exe scripts/install_go2rtc.py --dir D:/tools/go2rtc
"""

from __future__ import annotations

import argparse
import io
import json
import platform
import sys
import urllib.request
import zipfile
from pathlib import Path

# 从项目根直接 `python scripts/xxx.py` 时，sys.path[0] 是 scripts/ 而不是项目根
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mihome.core import paths  # noqa: E402

REPO = "AlexxIT/go2rtc"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
UA = "MiHomePanel-installer"


def asset_name() -> str:
    if sys.platform == "win32":
        return "go2rtc_win64.zip" if platform.machine().endswith("64") else "go2rtc_win32.zip"
    if sys.platform == "darwin":
        return "go2rtc_mac_arm64.zip" if platform.machine() == "arm64" else "go2rtc_mac_amd64.zip"
    return "go2rtc_linux_arm64" if platform.machine() in ("aarch64", "arm64") else "go2rtc_linux_amd64"


def get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download(url: str) -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/octet-stream"}
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return resp.read()


def install(target: Path, force: bool) -> Path:
    exe = "go2rtc.exe" if sys.platform == "win32" else "go2rtc"
    dest = target / exe
    if dest.is_file() and not force:
        print(f"已存在：{dest}")
        print("要重新下载加 --force")
        return dest

    print("查询 latest release…")
    rel = get_json(API)
    tag = str(rel.get("tag_name", "?"))
    name = asset_name()
    asset = next((a for a in rel.get("assets", []) if a.get("name") == name), None)
    if asset is None:
        raise SystemExit(f"release {tag} 里没有 {name}")
    size = int(asset.get("size") or 0)
    print(f"  {tag} / {name}  {size / 1048576:.1f} MB")
    print("下载中（走 assets API）…")
    data = download(str(asset["url"]))
    print(f"  收到 {len(data) / 1048576:.1f} MB")

    target.mkdir(parents=True, exist_ok=True)
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            if exe not in zf.namelist():
                raise SystemExit(f"压缩包里没有 {exe}：{zf.namelist()}")
            dest.write_bytes(zf.read(exe))
    else:
        dest.write_bytes(data)
    if sys.platform != "win32":
        dest.chmod(0o755)
    print(f"已安装：{dest}")
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 go2rtc（可选外部组件，MIT）")
    parser.add_argument("--force", action="store_true", help="已存在也重新下载")
    parser.add_argument("--dir", default="", help="安装目录，默认 <data>/go2rtc")
    args = parser.parse_args()
    target = Path(args.dir).expanduser() if args.dir else paths.data_dir() / "go2rtc"
    try:
        dest = install(target, args.force)
    except Exception as exc:  # noqa: BLE001 - 安装脚本要把失败原因原样打给人看
        print(f"失败：{type(exc).__name__}: {exc}")
        return 1
    print("\n许可证：go2rtc 为 MIT，源码 https://github.com/AlexxIT/go2rtc")
    print(f"程序会自动在 {dest} 找它；也可以用环境变量 MIHOME_GO2RTC 指定。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
