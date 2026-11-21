"""`assets/app.ico` 的形状检查：这份文件是生成出来的，最容易出现的错是"忘了重导"。

跑法（项目惯例，要带 ``PYTHONPATH``）::

    PYTHONPATH=. .venv/Scripts/python.exe scripts/verify_app_icon.py

判据分两层：

1. **文件本身**：ICO 头 / 目录项 / 7 档尺寸 / 每档 PNG 能解码。
2. **各档不是同一张图缩出来的**：16、32、64 三档分别走"极小 / 中等 / 完整"三种
   几何（``_tiny`` / ``_fat_screen`` / 完整版），把它们缩到同一尺寸后应当**明显
   不同**。若有人图省事改成"渲染一次 256 再让 PIL 缩出 7 档"，这三档会几乎一样，
   判据当场失败——那正是整份生成脚本从第一版起就在躲的糊法。

另外顺手确认 ``mihome.ui.app_icon`` 在源码运行下找得到文件、``QIcon`` 不空。
"""

from __future__ import annotations

import io
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ICO = ROOT / "assets" / "app.ico"
WANT = [16, 24, 32, 48, 64, 128, 256]

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'[ OK ]' if ok else '[FAIL]'} {name}{('  ' + detail) if detail else ''}")
    if not ok:
        fails.append(name)


def main() -> int:
    from PIL import Image

    if not ICO.is_file():
        check("assets/app.ico 存在", False, "先跑 scripts/make_icon.py --ico")
        return 1
    check("assets/app.ico 存在", True, f"{ICO.stat().st_size} 字节")

    raw = ICO.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", raw[:6])
    check("ICO 头合法", reserved == 0 and kind == 1, f"type={kind}")
    check("目录项 7 个", count == 7, f"实际 {count}")

    entries = []
    for i in range(count):
        off = 6 + 16 * i
        w, h, _pal, _res, _planes, bits, size, data_off = struct.unpack(
            "<BBBBHHII", raw[off:off + 16])
        # ICO 目录项里宽高字段只有 1 字节，256 存不下，规范规定写 0 —— 不是"零尺寸"
        dim = 256 if w == 0 else w
        entries.append((dim, bits, size, data_off))

    got = sorted(e[0] for e in entries)
    check("尺寸齐全", got == WANT, f"{got}")

    bad_bits = [e[0] for e in entries if e[1] != 32]
    check("全部 32 位色", not bad_bits, f"异常档：{bad_bits}" if bad_bits else "")

    # 每档的 PNG 是否真能解出对应尺寸
    imgs: dict[int, Image.Image] = {}
    broken: list[str] = []
    for dim, _bits, size, data_off in entries:
        blob = raw[data_off:data_off + size]
        try:
            im = Image.open(io.BytesIO(blob))
            im.load()
        except Exception as exc:  # noqa: BLE001 - 只为报告，不吞后续逻辑
            broken.append(f"{dim}: {exc}")
            continue
        if im.size != (dim, dim):
            broken.append(f"{dim}: 实际 {im.size}")
            continue
        imgs[dim] = im.convert("RGBA")
    check("每档都能解码且尺寸自洽", not broken, "; ".join(broken[:3]))

    # 判据：每档必须比"256 缩到该尺寸"更像自己——也就是**不是**从 256 缩出来的。
    # 16 档砍了屏幕、32 档放大房子，两档都该和缩小版差得远；64 档和 256 是同一套
    # 完整几何，缩下来本该几乎一样，拿它当反向对照（证明判据不是永真）。
    def diff(img_a: Image.Image, img_b: Image.Image) -> float:
        ba, bb = img_a.convert("RGB").tobytes(), img_b.convert("RGB").tobytes()
        return sum(abs(x - y) for x, y in zip(ba, bb)) / len(ba)

    if 256 in imgs:
        big = imgs[256]
        # 实测：16 档差 38 级、32 档 8.2 级，而同一几何的 64 档只有 3.0 级。
        # 阈值取 5 正好落在两者中间，两边都有余量（32 档的 8.2 是这份判据的下限）。
        for dim, floor in ((16, 8.0), (32, 5.0)):
            if dim not in imgs:
                continue
            shrink = big.resize((dim, dim), Image.LANCZOS)
            got = diff(imgs[dim], shrink)
            check(f"{dim} 档不是 256 缩出来的", got > floor,
                  f"与缩小版差 {got:.1f} 级（阈值 {floor}）")
        if 64 in imgs:
            shrink64 = big.resize((64, 64), Image.LANCZOS)
            got = diff(imgs[64], shrink64)
            check("64 档与 256 缩版基本一致（判据反向对照）", got < 5.0,
                  f"差 {got:.1f} 级")

    # 运行时能不能找到 / 读出来
    try:
        import os

        os.environ.setdefault("MIHOME_NO_TRAY", "1")
        sys.path.insert(0, str(ROOT))
        from PySide6.QtWidgets import QApplication

        from mihome.ui import app_icon

        _app = QApplication.instance() or QApplication([])
        found = app_icon.path()
        check("app_icon.path() 找得到", found == ICO, str(found))
        icon = app_icon.load()
        sizes = {(s.width(), s.height()) for s in icon.availableSizes()}
        check("QIcon 不空且带 7 档", not icon.isNull() and len(sizes) == 7,
              f"{sorted(sizes)}")
    except Exception as exc:  # noqa: BLE001 - 环境缺 Qt 也要给出可读结论
        check("Qt 侧加载", False, repr(exc))

    print()
    if fails:
        print(f"失败 {len(fails)} 项：{fails}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
