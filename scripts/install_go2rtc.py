# SPDX-License-Identifier: GPL-3.0-or-later
"""下载可选的 go2rtc 外部进程（MIT），放到用户数据目录供程序直接调用。

摄像头实时画面依赖 go2rtc 把米家 P2P 码流转成本机 RTSP。它是独立进程、MIT 许可，
默认从上游官方 Release 下载；``--patched`` 则从本项目的组件 Release
（tag ``go2rtc-<build_go2rtc.REF>``）取带两个本地补丁的构建：首帧约 2 秒，并带
``/api/xiaomi/ptz`` 云台接口。补丁版 Release 不存在或下载失败时，会明确报错并
回退到官方构建（本地已有 go2rtc 则保留不动）。

**为什么走 assets API 而不是 releases/download 链接**：后者会 302 到
``objects.githubusercontent.com``。本机网络实测 ``github.com`` 和几个常见镜像都不通，
只有 ``api.github.com`` 可用；给它加 ``Accept: application/octet-stream`` 会直接回
资产内容（8.1MB 正常下完）。这不是通用加速手段，只是在本机网络下能走通的那条。

用法：
  .venv/Scripts/python.exe scripts/install_go2rtc.py
  .venv/Scripts/python.exe scripts/install_go2rtc.py --patched
  .venv/Scripts/python.exe scripts/install_go2rtc.py --force
  .venv/Scripts/python.exe scripts/install_go2rtc.py --patched --dir D:/tools/go2rtc
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

# 从项目根直接 `python scripts/xxx.py` 时，sys.path[0] 是 scripts/ 而不是项目根
ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
for _p in (ROOT, SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from mihome.core import paths  # noqa: E402

#: 补丁版 Release 的 tag 从编译脚本的 REF 推导，避免两处各写一份版本号
from build_go2rtc import REF  # noqa: E402

OFFICIAL_REPO = "AlexxIT/go2rtc"
#: 本项目的组件 Release 发布在这里（只发 go2rtc，不发程序本身）
PROJECT_REPO = "scnf-8848/MiHomePanel"
OFFICIAL_API = f"https://api.github.com/repos/{OFFICIAL_REPO}/releases/latest"
PATCHED_TAG = f"go2rtc-{REF}"
PATCHED_API = f"https://api.github.com/repos/{PROJECT_REPO}/releases/tags/{PATCHED_TAG}"
UA = "MiHomePanel-installer"


class InstallError(Exception):
    """带中文说明的安装失败；main() 负责打印并走回退路径。"""


def platform_key() -> tuple[str, str]:
    """(平台, 架构)。和官方 Release 的资产命名保持一致。"""
    if sys.platform == "win32":
        return "win", "64" if platform.machine().endswith("64") else "32"
    if sys.platform == "darwin":
        return "mac", "arm64" if platform.machine() == "arm64" else "amd64"
    return "linux", "arm64" if platform.machine() in ("aarch64", "arm64") else "amd64"


def asset_name() -> str:
    """官方 Release 里的资产名。"""
    plat, arch = platform_key()
    if plat == "win":
        return f"go2rtc_win{arch}.zip"
    if plat == "mac":
        return f"go2rtc_mac_{arch}.zip"
    return f"go2rtc_linux_{arch}"


def patched_asset_name() -> str:
    """本项目组件 Release 里的资产名：go2rtc_<平台>_<架构>_patched.zip。"""
    plat, arch = platform_key()
    if plat == "win":
        return f"go2rtc_win{arch}_patched.zip"
    return f"go2rtc_{plat}_{arch}_patched.zip"


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


def _download_checked(url: str, what: str) -> bytes:
    try:
        return download(url)
    except urllib.error.HTTPError as exc:
        raise InstallError(f"下载{what}失败（HTTP {exc.code}）：{url}") from exc
    except Exception as exc:  # noqa: BLE001 - 网络错误种类太多，统一转中文
        raise InstallError(f"下载{what}失败：{type(exc).__name__}: {exc}") from exc


def _fetch_release() -> dict:
    try:
        return get_json(PATCHED_API)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise InstallError(
                f"补丁版 Release {PATCHED_TAG} 不存在（404）。"
                "需要先在能发布的机器上按 docs/DEVELOPMENT.md「补丁版 Release」建这个 tag。"
            ) from exc
        if exc.code == 403:
            raise InstallError(
                "GitHub API 返回 403（通常是匿名限流，或仓库 / Release 不是公开的）。"
                "稍后重试，或先在本机跑 scripts/build_go2rtc.py 自己编。"
            ) from exc
        raise InstallError(f"查询补丁版 Release 失败（HTTP {exc.code}）：{PATCHED_API}") from exc
    except Exception as exc:  # noqa: BLE001
        raise InstallError(
            f"查询补丁版 Release 失败：{type(exc).__name__}: {exc}（{PATCHED_API}）"
        ) from exc


def _pick_asset(rel: dict, name: str) -> dict:
    asset = next((a for a in rel.get("assets", []) if a.get("name") == name), None)
    if asset is not None:
        return asset
    have = ", ".join(str(a.get("name") or "?") for a in rel.get("assets", [])) or "无"
    tag = str(rel.get("tag_name") or PATCHED_TAG)
    raise InstallError(f"Release {tag} 里没有资产 {name}（现有：{have}）")


def _verify_sums(rel: dict, name: str, data: bytes) -> None:
    """Release 带 SHA256SUMS 就核对；没带则跳过（HTTPS 第 1 层兜底）。"""
    sums = next((a for a in rel.get("assets", []) if a.get("name") == "SHA256SUMS"), None)
    if sums is None:
        return
    text = _download_checked(str(sums["url"]), " SHA256SUMS").decode("utf-8", "replace")
    want = ""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == name:
            want = parts[0].strip().lower()
            break
    if not want:
        raise InstallError(f"SHA256SUMS 里没有 {name} 的记录")
    got = hashlib.sha256(data).hexdigest()
    if got != want:
        raise InstallError(f"{name} 哈希不符：期望 {want[:12]}…，实际 {got[:12]}…")
    print(f"  哈希校验通过（sha256 {got[:12]}…）")


def _read_ref(path: Path) -> str:
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("ref="):
            return line.split("=", 1)[1].strip()
    return ""


def _extract_patched(data: bytes, exe: str) -> dict[str, bytes]:
    """从 zip 里取 exe / BUILD.txt / LICENSE；只认文件名，避免路径穿越。"""
    wanted = {exe, "BUILD.txt", "LICENSE"}
    found: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                base = PurePosixPath(info.filename).name
                if info.is_dir() or base not in wanted or base in found:
                    continue
                found[base] = zf.read(info)
    except zipfile.BadZipFile as exc:
        raise InstallError(f"补丁版资产不是有效的 zip：{exc}") from exc

    missing = sorted(wanted - found.keys())
    if missing:
        raise InstallError(f"补丁版资产缺少文件：{', '.join(missing)}")
    return found


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def install(target: Path, force: bool) -> Path:
    """官方构建（原有行为，未改动）。"""
    exe = "go2rtc.exe" if sys.platform == "win32" else "go2rtc"
    dest = target / exe
    if dest.is_file() and not force:
        print(f"已存在：{dest}")
        print("要重新下载加 --force")
        return dest

    print("查询 latest release…")
    rel = get_json(OFFICIAL_API)
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
    # 覆盖掉补丁版时要清掉构建记录，否则程序仍会按「2 秒 + 云台」对待官方二进制
    (target / "BUILD.txt").unlink(missing_ok=True)
    print(f"已安装：{dest}")
    return dest


def install_patched(target: Path, force: bool) -> Path:
    """从本项目组件 Release 安装补丁版。"""
    exe = "go2rtc.exe" if sys.platform == "win32" else "go2rtc"
    dest = target / exe
    record = target / "BUILD.txt"
    if dest.is_file() and not force:
        ref = _read_ref(record)
        if ref == REF:
            print(f"已存在补丁版：{dest}（ref={ref}）")
        elif ref:
            print(f"已存在补丁版：{dest}（ref={ref}，不是 {REF}）")
        else:
            print(f"已存在 go2rtc：{dest}（官方版）")
        print("要换成/重新下载补丁版加 --force")
        return dest

    print(f"查询组件 Release {PATCHED_TAG}（{PROJECT_REPO}）…")
    rel = _fetch_release()
    name = patched_asset_name()
    asset = _pick_asset(rel, name)
    size = int(asset.get("size") or 0)
    print(f"  {PATCHED_TAG} / {name}  {size / 1048576:.1f} MB")
    print("下载中（走 assets API）…")
    data = _download_checked(str(asset["url"]), name)
    print(f"  收到 {len(data) / 1048576:.1f} MB")
    _verify_sums(rel, name, data)

    files = _extract_patched(data, exe)
    ref = ""
    for line in files["BUILD.txt"].decode("utf-8", "replace").splitlines():
        if line.startswith("ref="):
            ref = line.split("=", 1)[1].strip()
            break
    if ref != REF:
        raise InstallError(f"BUILD.txt 里的 ref={ref or '<空>'}，与期望的 {REF} 不一致")

    try:
        target.mkdir(parents=True, exist_ok=True)
        # 只把原来的官方构建留底，补丁版之间覆盖不备份
        backup = target / ("go2rtc-official.exe" if sys.platform == "win32" else "go2rtc-official")
        if dest.is_file() and not record.is_file() and not backup.exists():
            shutil.move(str(dest), str(backup))
            print(f"  原版留底：{backup.name}")
        _write_atomic(dest, files[exe])
        _write_atomic(record, files["BUILD.txt"])
        _write_atomic(target / "LICENSE", files["LICENSE"])
    except OSError as exc:
        raise InstallError(f"写入 {target} 失败（程序是不是正在运行？）：{exc}") from exc
    if sys.platform != "win32":
        dest.chmod(0o755)

    print(f"已安装补丁版：{dest}（ref={ref}）")
    print("  首帧约 2 秒，详情页会探测 /api/xiaomi/ptz 决定是否显示方向盘")
    return dest


def _fallback_official(target: Path, force: bool) -> tuple[Path, str]:
    """补丁版失败时尽量退到官方版 / 本地已有的那份。"""
    exe = "go2rtc.exe" if sys.platform == "win32" else "go2rtc"
    dest = target / exe
    if dest.is_file() and not force:
        ref = _read_ref(target / "BUILD.txt")
        if ref:
            return dest, f"保留本地现有补丁版（ref={ref}，不是本次期望的 {REF}）"
        return dest, "保留本地现有官方版（首帧 4.8–6.3 秒、没有云台）"
    dest = install(target, force)
    return dest, "已回退为官方构建（首帧 4.8–6.3 秒、没有云台）"


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 go2rtc（可选外部组件，MIT）")
    parser.add_argument("--patched", action="store_true",
                        help="从我项目 Release 安装补丁版（首帧约 2 秒、含云台）")
    parser.add_argument("--force", action="store_true", help="已存在也重新下载")
    parser.add_argument("--dir", default="", help="安装目录，默认 <data>/go2rtc")
    args = parser.parse_args()
    target = Path(args.dir).expanduser() if args.dir else paths.data_dir() / "go2rtc"
    try:
        if args.patched:
            try:
                dest = install_patched(target, args.force)
            except InstallError as exc:
                print(f"\n补丁版安装失败：{exc}")
                print("回退：")
                try:
                    dest, how = _fallback_official(target, args.force)
                except Exception as exc2:  # noqa: BLE001
                    print(f"  官方版也装不上：{type(exc2).__name__}: {exc2}")
                    print("  可手动下载官方 go2rtc 放到上述目录，"
                          "或先跑 scripts/build_go2rtc.py 自己编。")
                    return 1
                print(f"  {how}")
        else:
            dest = install(target, args.force)
    except Exception as exc:  # noqa: BLE001 - 安装脚本要把失败原因原样打给人看
        print(f"失败：{type(exc).__name__}: {exc}")
        return 1
    print("\n许可证：go2rtc 为 MIT，源码 https://github.com/AlexxIT/go2rtc")
    print(f"程序会自动在 {dest} 找它；也可以用环境变量 MIHOME_GO2RTC 指定。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
