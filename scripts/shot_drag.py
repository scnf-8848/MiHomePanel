# SPDX-License-Identifier: GPL-3.0-or-later
"""拖动中的落点截图：证明"幽灵在目标卡左边，占位框也在它左边"。

    .venv/Scripts/python.exe -X utf8 scripts/shot_drag.py [输出路径] [--dark]

用真实卡片墙（要能登录米家），把第一张卡往第二张卡的**左半边**拖，然后把整窗截下来。
修好之前这张图会露出破绽：幽灵已经压在第二张卡左半边，占位框（第一格）却已经跳到它右边。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QPoint, QTimer  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

os.environ.setdefault("MIHOME_NO_TRAY", "1")

from mihome.ui import theme  # noqa: E402
from mihome.ui.main_window import MainWindow  # noqa: E402


def main() -> int:
    args = sys.argv[1:]
    out = Path("scripts/out/drag_drop.png")
    if args and not args[0].startswith("--"):
        out = Path(args.pop(0))
    dark = "--dark" in args

    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    f = QFont("Microsoft YaHei UI")
    f.setPixelSize(13)
    app.setFont(f)

    win = MainWindow(theme.DARK if dark else theme.LIGHT)
    win.palette_ref = theme.DARK if dark else theme.LIGHT
    win._apply_theme()  # noqa: SLF001
    win.resize(1100, 820)
    win.show()
    win.start()

    state = {"n": 0}

    def tick() -> None:
        wall = win.wall
        ready = not win.store.loading and len(wall._cards) >= 3
        if not ready:
            QTimer.singleShot(150, tick)
            return
        section = None
        cards = []
        for sec in wall._sections:
            ws = [c for c in sec.cards.values() if c.isVisible()]
            if len(ws) >= 3 and sec.draggable:
                section, cards = sec, ws
                break
        if section is None:
            print("没有可拖的分区")
            app.quit()
            return
        source, second = cards[0], cards[1]
        wall._drag_card = source
        wall._drag_section = section
        wall._drag_origin = section.flow.index_of(source)
        # 抓在卡片**下半部**（用户实际多半这么抓）：鼠标 y 会越过该行中线，
        # 正是旧判据把整行都算到后面的场景
        start = source.mapToGlobal(QPoint(source.width() // 2, source.height() - 30))
        wall._begin_drag(source, start)
        app.processEvents()
        # 幽灵压到第二张卡的**左半边**：让幽灵左上角落在第二张卡左边 60px，
        # 于是幽灵中心落在该卡中线左侧（这正是用户报错位的那个位置）
        second_vp = second.mapTo(wall.viewport(), QPoint(0, 0))
        mouse_vp = second_vp + QPoint(-60, 30) + wall._drag_grab
        wall._update_drag(wall.viewport().mapToGlobal(mouse_vp))
        app.processEvents()
        if state["n"] < 3:          # 等两帧，让占位框与幽灵都画完
            state["n"] += 1
            QTimer.singleShot(120, tick)
            return
        out.parent.mkdir(parents=True, exist_ok=True)
        win.grab().save(str(out))
        ghost = wall._drag_ghost
        print(f"已保存 {out}  {win.width()}x{win.height()}")
        if ghost is not None:
            print(f"  幽灵 rect={ghost.geometry()}  中心={ghost.geometry().center()}")
            print(f"  占位（第 1 格）rect={section.flow.placement[0].rect}")
            print(f"  第 2 张卡 rect={section.flow.placement[1].rect}")
            flow_pos = section.flow.parentWidget().mapFrom(
                wall.viewport(), ghost.geometry().center())
            boxes = [s.rect for s in section.flow.placement[1:]]
            old = sum(
                1 for r in boxes
                if flow_pos.y() > r.center().y()
                or (abs(flow_pos.y() - r.center().y()) <= r.height() / 4
                    and flow_pos.x() > r.center().x())
            )
            print(f"  落点：新 {section.flow.drop_index(flow_pos, source)} / 旧 {old}"
                  "（旧值会跳到队尾，就是用户看到的占位框跑到右边）")
        wall._finish_drag(commit=False)
        app.quit()

    QTimer.singleShot(600, tick)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
