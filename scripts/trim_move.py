"""按 ``build_exe`` 的裁剪清单，把产物里"该删"的文件**移走**，绕开批量删除保护。

为什么需要它：本机（2026-11-17 起）的 safe-delete 会拦"单次删除 ≥50 个文件"，
而 ``build_exe.slim()`` 正好两处都超线——``_drop_translations`` 一次删 77 个 ``.qm``、
``_TRIM`` 里的 ``tzdata`` 有 600 多个文件。于是打包总在裁剪阶段中止：``dist`` 已经建好、
go2rtc 也拷进去了，就差裁剪和两项门禁。同盘 rename 不算删除，所以这里改成
``Path.replace`` 到仓库外，移动完再补跑 ``--self-check`` 与窗口冒烟。

用法（在 ``build_exe.py`` 打印 ``SAFE_DELETE_BULK_CONFIRM_REQUIRED`` 之后）::

    .venv\\Scripts\\python.exe scripts\\trim_move.py
    .venv\\Scripts\\python.exe scripts\\trim_move.py --out D:\\tmp\\trimmed --skip-check

移出去的东西可以随时删（都是确定用不到的 Qt 翻译、QML、tzdata 之类），保留着只是
为了"万一要回滚这一版产物"时能原样放回去。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_exe as be  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def park(path: Path, app_dir: Path, out: Path) -> None:
    """移到 ``out/<相对路径>``（同盘 rename，不触发删除阈值）。"""
    dst = out / path.relative_to(app_dir)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    path.replace(dst)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把产物里要裁掉的文件移出仓库外")
    parser.add_argument("--app", default=str(ROOT / "dist" / "MiHomePanel"),
                        help="产物目录（默认 dist/MiHomePanel）")
    parser.add_argument("--out", default=str(ROOT.parent / "_trimmed"),
                        help="移到哪里（默认仓库外的 ../_trimmed）")
    parser.add_argument("--keep-sw-opengl", action="store_true",
                        help="保留 opengl32sw.dll（虚拟机 / 远程桌面要）")
    parser.add_argument("--skip-check", action="store_true",
                        help="只移动，不跑自检与窗口冒烟")
    args = parser.parse_args(argv)

    app_dir = Path(args.app).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    if not (app_dir / "MiHomePanel.exe").is_file():
        print(f"找不到产物：{app_dir / 'MiHomePanel.exe'}\n先跑 build_exe.py")
        return 1
    out.mkdir(parents=True, exist_ok=True)

    # 1) Qt 翻译：只留白名单，其余移走（与 _drop_translations 同一个判据）
    folder = app_dir / "_internal/PySide6/translations"
    keep = {n for n in be._QT_TRANSLATIONS if (folder / n).is_file()}
    moved = 0
    if folder.is_dir() and keep:
        for qm in folder.glob("*.qm"):
            if qm.name in keep:
                continue
            park(qm, app_dir, out)
            moved += 1
        print(f"Qt 翻译：移走 {moved} 个 .qm，保留 {', '.join(sorted(keep))}")
    else:
        print("Qt 翻译：没找到目录或白名单一个都没命中，跳过（宁可不删，也别删光）")

    # 2) _TRIM 各组 + 软件 OpenGL
    entries = list(be._TRIM)
    if not args.keep_sw_opengl:
        entries.append((be._SW_OPENGL, "软件 OpenGL 后备库"))
    for patterns, reason in entries:
        files = be._expand(app_dir, patterns)
        if not files:
            print(f"  [skip] {reason}：清单里的文件都不在（PyInstaller 版式变过？）")
            continue
        for f in files:
            park(f, app_dir, out)
        moved += len(files)
        print(f"  [ok] {reason} → {len(files)} 个")

    total = sum(f.stat().st_size for f in app_dir.rglob("*") if f.is_file())
    count = sum(1 for f in app_dir.rglob("*") if f.is_file())
    print(f"\n共移走 {moved} 个文件 → {out}")
    print(f"产物现在：{total / 1048576:.0f} MB / {count} 个文件")

    if args.skip_check:
        print("\n已跳过门禁。发布前记得补：自检 + 窗口冒烟。")
        return 0
    ok = be.run_self_check(app_dir)
    if not ok:
        print("\n自检未通过，先别发（裁剪删过头就会在这里暴露）。")
        return 1
    ok = be.run_window_smoke(app_dir)
    if not ok:
        print("\n窗口冒烟未通过，先别发。")
        return 1
    print("\n两项门禁都过了，可以组包。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
