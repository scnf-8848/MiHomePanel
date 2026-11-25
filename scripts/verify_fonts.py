# SPDX-License-Identifier: GPL-3.0-or-later
"""验证字体模块：引擎开关、平台串拼装、灰度抗锯齿是否真的落到了控件上。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_fonts.py``

**这里钉的是踩过的坑**：Windows 下 Qt 默认走 DirectWrite，``NoSubpixelAntialias``
被静默忽略，13 px 中文带 ClearType 红蓝彩边（像"笔画毛刺"）。唯一有效的是换
FreeType 引擎，而 ``QT_QPA_PLATFORM`` 只在 QApplication 构造时读一次——顺序错了
就完全没效果，也不报错。另有两个必须保住的边界：

* ``QT_QPA_PLATFORM=offscreen`` 是 50 多个验证脚本的前提，绝不能被我改成 windows；
* QSS 的 ``* { font-family }`` 会重建每个控件的字体，别让它把抗锯齿策略吃掉。

渲染质量本身（彩边像素差）测不了——offscreen 没有字体库，见
``docs/reference/font-swap.md``。
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from mihome.ui import fonts, theme  # noqa: E402

FAILS: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {got!r}")
    if not ok:
        FAILS.append(f"{name} → 期望 {want!r}，实际 {got!r}")


def check_true(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(f"{name} {detail}")


print("== 1. 平台串拼装 ==")
check("空 → 换 FreeType", fonts._with_freetype(""), "windows:fontengine=freetype")
check("裸 windows → 追加", fonts._with_freetype("windows"), "windows:fontengine=freetype")
check(
    "已带选项 → 保留选项",
    fonts._with_freetype("windows:darkmode=2"),
    "windows:fontengine=freetype:darkmode=2",
)
check(
    "用户已指定 fontengine → 不覆盖",
    fonts._with_freetype("windows:fontengine=gdi"),
    "windows:fontengine=gdi",
)
check("offscreen → 原样（离屏验证的前提）", fonts._with_freetype("offscreen"), "offscreen")
check("minimal → 原样", fonts._with_freetype("minimal"), "minimal")

# 非 Windows 不该动平台串：临时把 fonts 模块看到的 sys 换掉，不污染真实 sys
_real_sys = fonts.sys
fonts.sys = types.SimpleNamespace(platform="linux")
try:
    check("非 win32 → 原样", fonts._with_freetype("xcb"), "xcb")
finally:
    fonts.sys = _real_sys

print()
print("== 2. prepare_engine 的开关与幂等 ==")
_real_env = os.environ.get("QT_QPA_PLATFORM", "")
_real_engine = os.environ.get(fonts.ENGINE_ENV)
try:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    before = os.environ["QT_QPA_PLATFORM"]
    changed = fonts.prepare_engine()
    check("offscreen 下不改环境", os.environ["QT_QPA_PLATFORM"], before)
    check_true("offscreen 下返回 False", changed is False, f"got={changed!r}")

    os.environ.pop("QT_QPA_PLATFORM", None)
    os.environ[fonts.ENGINE_ENV] = "default"
    changed = fonts.prepare_engine()
    check("ENGINE_ENV=default 时不设平台串", os.environ.get("QT_QPA_PLATFORM"), None)
    check_true("ENGINE_ENV=default 时返回 False", changed is False, f"got={changed!r}")

    os.environ.pop(fonts.ENGINE_ENV, None)
    if sys.platform == "win32":
        first = fonts.prepare_engine()
        check("默认开：平台串含 freetype", os.environ.get("QT_QPA_PLATFORM"), "windows:fontengine=freetype")
        check_true("默认开：返回 True", first is True, f"got={first!r}")
        second = fonts.prepare_engine()
        check_true("再调一次不再改动", second is False, f"got={second!r}")
        check("再调一次平台串不变", os.environ.get("QT_QPA_PLATFORM"), "windows:fontengine=freetype")
    else:
        print("SKIP  非 Windows，跳过真实引擎切换")
finally:
    if _real_env:
        os.environ["QT_QPA_PLATFORM"] = _real_env
    else:
        os.environ.pop("QT_QPA_PLATFORM", None)
    if _real_engine is None:
        os.environ.pop(fonts.ENGINE_ENV, None)
    else:
        os.environ[fonts.ENGINE_ENV] = _real_engine

print()
print("== 3. 基准字体 ==")
font = fonts.make_font()
check("字体名 = fonts.FAMILY", font.family(), fonts.FAMILY)
check("字号", font.pixelSize(), fonts.PIXEL_SIZE)
check_true(
    "带 NoSubpixelAntialias",
    font.styleStrategy() == QFont.NoSubpixelAntialias,
    f"got={font.styleStrategy()}",
)
# 2026-11-25：QSS 里**不许**再出现 font-family。那个 `* { font-family: 字体栈 }`
# 会让 Qt 给每个控件单独解析字体栈、各建一套字体引擎——真实主窗口实测多占
# 31 MB 私有内存。字体只能由 `fonts.apply_to()` 下发。
check_true(
    "全局 QSS 里没有 font-family",
    "font-family" not in theme.app_qss(theme.LIGHT),
    theme.app_qss(theme.LIGHT)[:60].replace("\n", " "),
)

print()
print("== 4. 装到应用后控件是否继承（QSS 不能吃掉策略）==")
app = QApplication.instance() or QApplication(sys.argv[:1])
app.setStyle("Fusion")
fonts.apply_to(app)
check_true(
    "QApplication 字体策略",
    app.font().styleStrategy() == QFont.NoSubpixelAntialias,
    f"got={app.font().styleStrategy()}",
)

# 真实路径：theme.apply_to_app 装调色板 + app 字体，QSS 里不再写 font-family
theme.apply_to_app(theme.LIGHT)
app.setStyleSheet(theme.app_qss(theme.LIGHT))

lbl = QLabel("客厅吸顶灯 亮度68%")
lbl.show()
check_true(
    "QSS 之后控件仍带 NoSubpixelAntialias",
    lbl.font().styleStrategy() == QFont.NoSubpixelAntialias,
    f"got={lbl.font().styleStrategy()}",
)
check("QSS 之后字体名仍是 FAMILY", lbl.font().family(), fonts.FAMILY)
check("QSS 之后字号不被改", lbl.font().pixelSize(), fonts.PIXEL_SIZE)
lbl.close()

print()
if FAILS:
    print(f"{len(FAILS)} 项失败：")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("全部通过")
