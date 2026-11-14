# SPDX-License-Identifier: GPL-3.0-or-later
"""自己编一版带补丁的 go2rtc（MIT），装到用户数据目录。

**为什么需要它**：有两件事官方构建做不到。

1. **开播补丁（首帧 5 秒 → 2 秒）**：go2rtc 的米家源在
   ``pkg/xiaomi/miss/producer.go::Dial()`` 里先 ``StartMedia()`` 再 ``probe()``。
   probe 为了拿到 VPS/SPS/PPS 会把开播那一包读走，而 IDR 切片和参数集在同一个
   access unit 里——于是消费者永远得等满一个 GOP 才有可解码的帧。实测官方版首帧
   4.8–6.3 秒，其中约 3 秒就是这一项，而且随 GOP 相位抖动。
   ``scripts/patches/go2rtc-keyframe.patch`` 在 probe 之后把流停掉重起一次
   （摄像头每次 video start 都从 IDR 开头），首帧降到 1.85–3.15 秒
   （均值 2.18），抖动也几乎没了。

2. **云台接口（``/api/xiaomi/ptz``）**：``chuangmi.camera.069a01`` 这类机型的
   MIoT 规格里**没有 ptz 服务**，云台只能走和取流同一条 MISS 连接的 ``0x112``
   指令。上游 ``pkg/xiaomi/miss/client.go`` 只定义了常量（``cmdMotorReq``），
   没实现、也没有 API 入口。``go2rtc-ptz.patch`` 补上 ``Client.Motor()``、
   把命令通道的回包常驻丢掉（缓冲只有 10 格，连发会撑爆并断流），再挂一个
   ``GET /api/xiaomi/ptz?name=<流名>&op=<1-5>``。没打这个补丁的构建里这条路由
   直接 404，程序据此把方向键隐藏掉——也就是官方构建照旧能看画面。

补丁都不大；**上游哪天自己实现了，删掉对应补丁即可**，程序会按能力探测自动退化。

用法：
  .venv/Scripts/python.exe scripts/build_go2rtc.py
  .venv/Scripts/python.exe scripts/build_go2rtc.py --go E:/Code/_go/go/bin/go.exe
  .venv/Scripts/python.exe scripts/build_go2rtc.py --keep       # 留下源码目录
  .venv/Scripts/python.exe scripts/build_go2rtc.py --ref <sha>  # 换上游快照

依赖：能联网（api.github.com 取源码、go.dev 取工具链、模块代理取依赖）＋ 本机有 git
（用来打补丁）。首次编译约 1–2 分钟（含下载），之后复用 ``<data>/go`` 里的工具链与
Go 模块缓存。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

# 从项目根直接 `python scripts/xxx.py` 时，sys.path[0] 是 scripts/ 而不是项目根
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mihome.core import paths

REPO = "AlexxIT/go2rtc"
#: 打过补丁并被实测验证过的上游快照。换它之前先跑一遍 scripts/out/measure_patch.py。
REF = "990f4de"
#: 按顺序应用；后面的补丁的上下文按"前面的已打上"来算
PATCHES = (
    ROOT / "scripts" / "patches" / "go2rtc-keyframe.patch",
    ROOT / "scripts" / "patches" / "go2rtc-ptz.patch",
)
#: 每个补丁打完后必须能在源码里看到的关键点（防止"补丁说成功、其实没生效"）
PATCH_MARKS = {
    "go2rtc-keyframe.patch": ("pkg/xiaomi/miss/producer.go", "StopMedia()"),
    "go2rtc-ptz.patch": ("pkg/xiaomi/miss/client.go", "func (c *Client) Motor("),
}
GO_VERSION = "1.27.3"
UA = "MiHomePanel-builder"
#: go2rtc 的 go.mod 要求 1.26+；goproxy.cn 在本机网络下可用，官方代理常超时。
GOPROXY = "https://goproxy.cn,direct"


def log(msg: str) -> None:
    print(msg, flush=True)


def download(url: str, *, accept: str = "application/octet-stream") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=600) as resp:
        return resp.read()


def fetch_source(ref: str, workdir: Path) -> Path:
    """取上游快照并解包，返回源码根目录。"""
    log(f"取源码 {REPO}@{ref}…")
    # 注意：zipball 不能带 Accept: application/octet-stream（会回 415），
    # 要交给 GitHub 自己 302 到 codeload。
    data = download(
        f"https://api.github.com/repos/{REPO}/zipball/{ref}", accept="*/*"
    )
    log(f"  收到 {len(data) / 1048576:.1f} MB")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(workdir)
    roots = [p for p in workdir.iterdir() if p.is_dir()]
    if len(roots) != 1:
        raise SystemExit(f"解包后不是单一目录：{[p.name for p in roots]}")
    return roots[0]


def ensure_go(explicit: str, cache: Path) -> Path:
    """按「显式 → <data>/go → PATH」找 Go，都没有就下一个便携版。"""
    exe = "go.exe" if sys.platform == "win32" else "go"
    if explicit:
        given = Path(explicit).expanduser()
        if not given.is_file():
            raise SystemExit(f"--go 指的文件不存在：{given}")
        return given
    if (cache / "go" / "bin" / exe).is_file():
        # 上次解压没拍平（Go 的 zip 里套了一层 go/）
        return _flatten_go(cache)
    if (cache / "bin" / exe).is_file():
        return cache / "bin" / exe
    found = shutil.which("go")
    if found:
        return Path(found)

    if sys.platform != "win32":
        raise SystemExit("没找到 go：先装 Go 1.26+，或用 --go 指过去")
    name = f"go{GO_VERSION}.windows-amd64.zip"
    log(f"本机没有 Go，下载便携版 {name}（~76 MB，只下一次）…")
    data = download(f"https://go.dev/dl/{name}")
    log(f"  收到 {len(data) / 1048576:.1f} MB，解压到 {cache}")
    cache.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(cache)
    return _flatten_go(cache)


def _flatten_go(cache: Path) -> Path:
    """Go 的 zip 里套了一层 go/，解开后搬到 cache 根，统一成 <cache>/bin/go。"""
    exe = "go.exe" if sys.platform == "win32" else "go"
    nested = cache / "go"
    if (nested / "bin" / exe).is_file():
        for item in nested.iterdir():
            target = cache / item.name
            if target.exists():
                continue
            shutil.move(str(item), str(target))
        nested.rmdir()
    go = cache / "bin" / exe
    if not go.is_file():
        raise SystemExit(f"解压后没找到 {go}")
    return go


def find_git() -> str | None:
    """找 git：PATH → GitHub Desktop 自带的那个。

    GitHub Desktop 的 git 不写系统 PATH，但装过它的机器通常没另装 Git for Windows；
    按版本目录倒序取最新的 app-*，路径和 GitHub Desktop 自己的用法一致。
    """
    found = shutil.which("git")
    if found:
        return found
    desktop = Path(os.environ.get("LOCALAPPDATA", "")) / "GitHubDesktop"
    if desktop.is_dir():
        candidates = sorted(
            desktop.glob("app-*/resources/app/git/cmd/git.exe"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
    return None


def apply_patch(src: Path) -> None:
    """依次用 git apply 打补丁，再逐个核对补丁点确实生效。"""
    git = find_git()
    if git is None:
        raise SystemExit(
            "没找到 git：打补丁需要它。装 Git for Windows（或 GitHub Desktop），"
            "或按 patch 文件手工改"
        )
    for patch in PATCHES:
        log(f"打补丁 {patch.name}…")
        proc = subprocess.run(
            [git, "apply", "-p1", "--whitespace=nowarn", str(patch)],
            cwd=src,
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
        )
        if proc.returncode != 0:
            raise SystemExit(
                "补丁打不上（上游这段代码变过了）。\n"
                f"  {patch.name}\n"
                f"  git apply 说：{(proc.stderr or proc.stdout).strip()}\n"
                "  先打开它对照源码手工改，或换 --ref。"
            )
        rel, mark = PATCH_MARKS[patch.name]
        text = (src / rel).read_text(encoding="utf-8")
        if mark not in text:
            raise SystemExit(f"{patch.name} 应用后 {rel} 里没看到 {mark}，拒绝继续")


def build(src: Path, go: Path, out: Path) -> None:
    log("编译中（首次要下模块依赖，约 1 分钟）…")
    env = dict(os.environ)
    env.update({"CGO_ENABLED": "0", "GOPROXY": GOPROXY, "GOSUMDB": "off",
                "GOFLAGS": "-mod=mod"})
    proc = subprocess.run(
        [str(go), "build", "-trimpath", "-ldflags=-s -w", "-o", str(out), "."],
        cwd=src,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=1200,
    )
    if proc.returncode != 0 or not out.is_file():
        raise SystemExit(f"编译失败：\n{(proc.stderr or proc.stdout).strip()[-2000:]}")


def verify(exe: Path) -> str:
    proc = subprocess.run(
        [str(exe), "-v"], check=False, capture_output=True, text=True, errors="replace", timeout=20
    )
    version = (proc.stdout or proc.stderr).strip().splitlines()
    if proc.returncode != 0 or not version:
        raise SystemExit(f"编出来的二进制跑不起来：{proc.stdout} {proc.stderr}")
    return version[0]


def install(exe: Path, target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    name = "go2rtc.exe" if sys.platform == "win32" else "go2rtc"
    dest = target / name
    backup = target / ("go2rtc-official.exe" if sys.platform == "win32" else "go2rtc-official")
    try:
        if dest.is_file() and not backup.exists():
            # 原样留一份官方构建：想回退把它改名回去就行
            shutil.move(dest, backup)
            log(f"  原版留底：{backup.name}")
        shutil.copy2(exe, dest)
    except PermissionError as exc:
        raise SystemExit(f"写不进去（程序是不是正在跑？先退出再编译）：{exc}") from exc
    digest = ":".join(hashlib.sha1(p.read_bytes()).hexdigest()[:8] for p in PATCHES)
    (target / "BUILD.txt").write_text(
        f"ref={REF}\n"
        f"patches={','.join(p.name for p in PATCHES)} sha1:{digest}\n"
        f"go={GO_VERSION}\nbuilt={datetime.now().isoformat(timespec='seconds')}\n",
        encoding="utf-8",
    )
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description="编译带开播补丁的 go2rtc（可选外部组件，MIT）")
    parser.add_argument("--ref", default=REF, help=f"上游快照，默认 {REF}")
    parser.add_argument("--go", default="", help="go 可执行文件路径（默认自动找/下载）")
    parser.add_argument("--dir", default="", help="安装目录，默认 <data>/go2rtc")
    parser.add_argument("--keep", action="store_true", help="保留源码目录（排障用）")
    parser.add_argument("--out", default="", help="只把二进制放这里，不安装")
    args = parser.parse_args()

    target = Path(args.dir).expanduser() if args.dir else paths.data_dir() / "go2rtc"
    workdir = Path(tempfile.mkdtemp(prefix="mihome-g2r-"))
    try:
        src = fetch_source(args.ref, workdir)
        apply_patch(src)
        go = ensure_go(args.go, paths.data_dir() / "go")
        log(f"  Go：{go}")
        built = workdir / ("go2rtc.exe" if sys.platform == "win32" else "go2rtc")
        build(src, go, built)
        log(f"  产物 {built.stat().st_size / 1048576:.1f} MB")
        log(f"  版本 {verify(built)}")
        if args.out:
            dest = Path(args.out).expanduser()
            shutil.copy2(built, dest)
        else:
            dest = install(built, target)
        log(f"\n已安装：{dest}")
        log("程序会优先用这个；想回退成官方构建，把 go2rtc-official.exe 改名回 go2rtc.exe 即可。")
    except SystemExit as exc:
        log(f"\n失败：{exc}")
        if args.keep:
            log(f"源码保留在 {workdir}")
        return 1
    except Exception as exc:  # noqa: BLE001 - 构建脚本要把失败原因原样打给人看
        log(f"\n失败：{type(exc).__name__}: {exc}")
        if args.keep:
            log(f"源码保留在 {workdir}")
        return 1
    finally:
        if not args.keep and workdir.is_dir():
            shutil.rmtree(workdir, ignore_errors=True)

    log("\n许可证：go2rtc 为 MIT，源码 https://github.com/AlexxIT/go2rtc")
    log(
        f"本版本附带 {len(PATCHES)} 个改动（{', '.join(p.name for p in PATCHES)}），"
        "说明见 docs/dev/camera.md「摄像头画面」。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
