# SPDX-License-Identifier: GPL-3.0-or-later
"""场景隐藏 + 圆角右键菜单的离线验证。

三件事：

1. ``Prefs`` 的 ``hidden_scenes`` 能存能读，和设备隐藏互不干扰；
2. ``Store.scenes_for`` 默认滤掉隐藏场景，设置页用的 ``include_hidden=True`` 全留；
3. 右键菜单是**真的**圆角（四角像素透明，边缘有抗锯齿渐变），不是"圆角图案
   底下垫一块矩形"。

用法：``.venv/Scripts/python.exe -X utf8 scripts/verify_scenes.py``
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from mihome.core.prefs import Prefs  # noqa: E402
from mihome.core.store import Store  # noqa: E402
from mihome.ui import theme  # noqa: E402
from mihome.ui.menu import Menu  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(f"{name} {detail}")


def make_store() -> Store:
    class _Caps:
        def save(self) -> None:
            pass

    class _Client:
        capabilities = _Caps()

    class _Worker:
        pass

    store = Store(_Client(), _Worker(), None)  # type: ignore[arg-type]
    store.prefs = Prefs(Path(tempfile.gettempdir()) / "mihome_verify_scenes.json")
    store.prefs.hidden_scenes = []
    store.scenes_by_home = {
        "家": [
            {"scene_id": "s1", "name": "回家", "home_id": "h1", "enable": True},
            {"scene_id": "s2", "name": "离家", "home_id": "h1", "enable": True},
            {"scene_id": "s3", "name": "关灯", "home_id": "h1", "enable": True},
        ]
    }
    return store


def main() -> int:
    app = QApplication(sys.argv[:1])  # noqa: F841

    print("1) Prefs.hidden_scenes")
    p = Prefs(Path(tempfile.gettempdir()) / "mihome_verify_scenes_prefs.json")
    p.hidden = []
    p.hidden_scenes = []
    first = p.set_scene_hidden("s1", True)
    second = p.set_scene_hidden("s1", True)
    check("重复隐藏只记一次", first and not second, f"{first}/{second}")
    check("已记录的能查出来", p.is_scene_hidden("s1"))
    check("取消隐藏返回变化", p.set_scene_hidden("s1", False) is True)
    check("取消后没了", not p.is_scene_hidden("s1"))
    p.set_scene_hidden("s2", True)
    again = Prefs(p.path)
    check("落盘后能读回", again.hidden_scenes == ["s2"], str(again.hidden_scenes))
    check("设备隐藏与场景隐藏分开", p.hidden == [])
    again.clear_hidden_scenes()
    check("清空后为空", Prefs(p.path).hidden_scenes == [])
    p.path.unlink(missing_ok=True)

    print("\n2) scenes_for 过滤")
    store = make_store()
    check("默认全在", len(store.scenes_for("家")) == 3)
    store.prefs.set_scene_hidden("s2", True)
    check("隐藏后首页少一个", [s["scene_id"] for s in store.scenes_for("家")] == ["s1", "s3"])
    check("设置页要的全都在",
          [s["scene_id"] for s in store.scenes_for("家", include_hidden=True)] == ["s1", "s2", "s3"])
    store.prefs.set_scene_hidden("s2", False)
    check("恢复后回到列表", len(store.scenes_for("家")) == 3)
    store.prefs.path.unlink(missing_ok=True)

    print("\n3) 右键菜单是真圆角")
    host = QWidget()
    host.resize(400, 300)
    menu = Menu(host, theme.LIGHT)
    for text in ("打开详情", "隐藏设备"):
        menu.addAction(text)
    menu.popup(host.mapToGlobal(host.rect().center()))
    app.processEvents()
    img = menu.grab().toImage()
    w, h = img.width(), img.height()
    check("菜单尺寸正常", w > 40 and h > 20, f"{w}x{h}")
    corner = QColor(img.pixelColor(0, 0)).alpha()
    check("左上角是透明的（不是矩形底）", corner < 40, f"alpha={corner}")
    mid = QColor(img.pixelColor(w // 2, h // 2)).alpha()
    check("中间是实心的", mid > 240, f"alpha={mid}")
    partial = 0
    for y in range(0, min(14, h)):
        for x in range(0, min(14, w)):
            a = QColor(img.pixelColor(x, y)).alpha()
            partial += 1 if 8 < a < 248 else 0
    check("圆角边缘有渐变（不是一刀切）", partial >= 6, f"{partial} 个中间态")
    menu.close()

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败")
        for line in FAILS:
            print("  -", line)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
