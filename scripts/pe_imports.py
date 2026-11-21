# SPDX-License-Identifier: GPL-3.0-or-later
"""读 PE 导入表，回答「这个 DLL 还有谁要」。

打包体积能压下来，靠的是删掉产物里没人引用的模块（QML、PDF、OpenGL…）。判断
「没人引用」不能凭文件名猜——猜错的后果是别人机器上白屏，而开发机上一切正常。
这个脚本直接读 PE 的导入表（Import Directory），只依赖标准库。

两种用法：

    # 这些文件自己依赖谁（看一个 DLL 拉了哪些兄弟）
    python scripts/pe_imports.py dist/MiHomePanel/_internal/PySide6/Qt6Multimedia.dll

    # 反过来：整个目录里，谁引用了这个名字（删之前必跑）
    python scripts/pe_imports.py --who Qt6Qml.dll dist/MiHomePanel/_internal

``--who`` 输出为空 = 没有任何 PE 静态引用它，可以进 ``build_exe.py`` 的裁剪清单。
注意这只能证明**静态导入**没人要；如果一个模块是靠 ``LoadLibrary`` 按名字在运行时
加载的（Qt 的插件就是这么找的），导入表里看不到——那种情况要连带把插件一起删
（例如 Qt6Pdf 只被 plugins/imageformats/qpdf.dll 引用，两个一起删才安全）。

换 PySide6 版本后清单要重跑一遍：上游随时可能把某个模块拆开或合并，凭旧的结论
删文件，报应会晚到而且难查。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

MACHINE_AMD64 = 0x20B
MACHINE_I386 = 0x10B


def imports(path: Path) -> list[str]:
    """返回该 PE 文件静态导入的 DLL 名（不含路径）。解析失败抛 ``ValueError``。"""
    data = path.read_bytes()
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise ValueError("不是 PE 文件")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe : pe + 4] != b"PE\0\0":
        raise ValueError("PE 头不对")
    coff = pe + 4
    (nsec,) = struct.unpack_from("<H", data, coff + 2)
    (opt_size,) = struct.unpack_from("<H", data, coff + 16)
    opt = coff + 20
    (magic,) = struct.unpack_from("<H", data, opt)
    if magic not in (MACHINE_AMD64, MACHINE_I386):
        raise ValueError(f"未知的可选头 magic {magic:#x}")
    # 数据目录第 1 项 = 导入表；32/64 位可选头前面长度不同
    data_dir = opt + (112 if magic == MACHINE_AMD64 else 96)
    rva, _size = struct.unpack_from("<II", data, data_dir + 8)
    if not rva:
        return []

    sections = []
    for i in range(nsec):
        off = opt + opt_size + i * 40
        vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, off + 8)
        sections.append((vaddr, max(vsize, rawsize), rawptr))

    def offset_of(rva_: int) -> int | None:
        for vaddr, size, rawptr in sections:
            if vaddr <= rva_ < vaddr + size:
                return rva_ - vaddr + rawptr
        return None

    names: list[str] = []
    pos = offset_of(rva)
    while pos is not None and pos + 20 <= len(data):
        entry = struct.unpack_from("<IIIII", data, pos)
        if not entry[3]:  # Name 字段为空 = 表结束
            break
        name_off = offset_of(entry[3])
        if name_off is None:
            break
        end = data.find(b"\0", name_off)
        names.append(data[name_off:end].decode("ascii", "replace"))
        pos += 20
    return names


def _pe_files(targets: list[str]) -> list[Path]:
    """把命令行参数（文件或目录）展开成 PE 文件列表。"""
    out: list[Path] = []
    for raw in targets:
        p = Path(raw)
        if p.is_dir():
            out.extend(x for x in p.rglob("*") if x.suffix.lower() in (".dll", ".pyd", ".exe"))
        elif p.is_file():
            out.append(p)
    return out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0
    if argv[0] == "--who":
        if len(argv) < 3:
            print("用法：pe_imports.py --who <DLL 名> <目录或文件>…")
            return 2
        needle = argv[1].lower()
        files = _pe_files(argv[2:])
        hits: list[tuple[str, list[str]]] = []
        unreadable = 0
        for f in files:
            try:
                deps = imports(f)
            except (ValueError, OSError, struct.error):
                unreadable += 1
                continue
            if any(d.lower() == needle for d in deps):
                hits.append((str(f), deps))
        print(f"扫描 {len(files)} 个 PE 文件（{unreadable} 个读不了）")
        if not hits:
            print(f"没有任何文件静态引用 {needle} → 可以删")
            return 0
        print(f"{len(hits)} 个文件引用了 {needle}：")
        for name, _ in hits:
            print(f"  {name}")
        return 1

    for raw in argv:
        for f in _pe_files([raw]) or [Path(raw)]:
            try:
                deps = imports(f)
            except (ValueError, OSError, struct.error) as exc:
                print(f"{f.name}: 读不了（{exc}）")
                continue
            print(f"{f.name}: {len(deps)} 个依赖")
            for d in deps:
                print(f"    {d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
