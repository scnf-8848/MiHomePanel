"""拖动排序的几何与中断验证。

    .venv/Scripts/python.exe -X utf8 scripts/verify_drag_sort.py

用户 2026-11-18 报的两个问题：

1. **落位不准**：幽灵已经比目标卡更靠左，占位框还在它右侧。旧判据对同一行的卡也用
   y 判定（"中线以下就 +1"），鼠标在 B 左半边但越过中线时会被算成排在 B 之后；
   而且探针用的是鼠标点，抓取点在哪纯属随机。修法见 ``dnd.grid_drop_index``。
2. **切程序后幽灵残留**：Alt+Tab / 截图快捷键让窗口失活，Qt 不再派发
   ``MouseMove`` / ``MouseButtonRelease``，手写状态机停在半路。修法是
   ``dnd.DragGuard``。

端到端的两处收尾（主墙 / 耗材页）分别在 ``verify_card_style`` 与
``verify_consumables`` 里，这里管几何和看门狗本身。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPoint, QRect, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from mihome.ui.card_grid import CardGridLayout  # noqa: E402
from mihome.ui.dnd import DragGuard, grid_drop_index  # noqa: E402

class _Card(QWidget):
    """最小卡片替身：布局只看 ``style.span/rows`` 和 ``set_grid_cells``。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.style = SimpleNamespace(span=1, rows=1)
        self.grid_span = 1
        self.grid_rows = 1

    def set_grid_cells(self, span: int, rows: int) -> None:
        self.grid_span, self.grid_rows = span, rows


PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}{('  — ' + str(detail)) if detail != '' else ''}")


def same(name: str, got: object, want: object) -> None:
    check(name, got == want, f"{got}（期望 {want}）")


def _old_index(pos: QPoint, boxes: list[QRect]) -> int:
    """旧实现，留在测试里当对照：证明修复点确实存在。"""
    idx = 0
    for r in boxes:
        if pos.y() > r.center().y():
            idx += 1
        elif abs(pos.y() - r.center().y()) <= r.height() / 4 and pos.x() > r.center().x():
            idx += 1
    return idx


def main() -> int:
    app = QApplication.instance() or QApplication([])

    # 单行三卡：宽 166、高 168、间距 8（和真实卡片一致）
    w, h, gap = 166, 168, 8
    row1 = [QRect(i * (w + gap), 0, w, h) for i in range(3)]
    # 两行六卡（第二行 y 从 176 起）
    rows2 = [QRect(i * (w + gap), 0 if r == 0 else h + gap, w, h)
             for r in range(2) for i in range(3)]

    print("\n== 落点：行带 + 行内中线 ==")
    # 被拖的是第一张，boxes 里只剩第 2、3 张
    boxes = row1[1:]
    second = row1[1]
    left_half_below_mid = QPoint(second.center().x() - 40, second.center().y() + 40)
    same("鼠标在第 2 张左半边、y 已过中线 → 仍排在它前面",
         grid_drop_index(left_half_below_mid, boxes), 0)
    # 旧实现只要 y 越过中线就给**整行**每张卡 +1 → 落点直接跳到队尾，
    # 正是截图里"幽灵在第 2 格左边、占位框却在第 3 格"的成因
    same("（旧实现会把整行都算在后面 → 2，跳到队尾）",
         _old_index(left_half_below_mid, boxes), 2)
    same("第 2 张右半边 → 排在它后面",
         grid_drop_index(QPoint(second.center().x() + 40, second.center().y() + 40), boxes), 1)
    third = row1[2]
    same("第 3 张左半边（下半）→ 仍在它前面",
         grid_drop_index(QPoint(third.center().x() - 40, third.center().y() + 60), boxes), 1)
    same("第 3 张右半边 → 排到最后",
         grid_drop_index(QPoint(third.center().x() + 40, third.center().y() + 60), boxes), 2)
    same("整行上方 → 排最前", grid_drop_index(QPoint(300, -20), boxes), 0)
    same("整行下方 → 排最后", grid_drop_index(QPoint(300, h + 40), boxes), 2)
    same("行内上半部也算同一行（看在左右）",
         grid_drop_index(QPoint(second.center().x() + 40, second.center().y() - 40), boxes), 1)

    print("\n== 落点：两行 ==")
    # 拖第一张，剩余 5 张
    rest = rows2[1:]
    gap_y = h + gap // 2          # 两行之间的缝隙
    same("探针落在两行之间的缝里 → 归到上一行末尾（源卡已排除，剩 2 张）",
         grid_drop_index(QPoint(300, gap_y), rest), 2)
    same("探针在第二行第 1 张左半边 → 上一行末尾之后、它之前",
         grid_drop_index(QPoint(rest[2].center().x() - 40, rest[2].center().y()), rest), 2)
    same("探针在第二行第 1 张右半边 → 排在它后面",
         grid_drop_index(QPoint(rest[2].center().x() + 40, rest[2].center().y()), rest), 3)
    same("探针在最后一行下方 → 队尾",
         grid_drop_index(QPoint(300, 2 * h + gap + 60), rest), 5)

    print("\n== 落点：混排不崩 ==")
    mixed = [QRect(0, 0, 340, 344), QRect(348, 0, 166, 168), QRect(348, 176, 166, 168)]
    for probe in (QPoint(100, 100), QPoint(400, 40), QPoint(400, 300), QPoint(0, 0)):
        idx = grid_drop_index(probe, mixed)
        check(f"2×2 与 1×1 混排：{probe.x()},{probe.y()} → {idx} 在范围内",
              0 <= idx <= len(mixed))

    print("\n== CardGridLayout 的实际落点（离屏三卡一行）==")
    host = QWidget()
    flow = CardGridLayout(host, margin=0, h_gap=gap, v_gap=gap)
    cards = []
    for _ in range(3):
        c = _Card(host)
        c.setFixedSize(w, h)
        flow.addWidget(c)
        cards.append(c)
    host.resize(3 * w + 2 * gap, h)
    host.show()
    app.processEvents()
    check("三张卡摆成了一行", len(flow.placement) == 3 and flow.placement[0].rect.y() == 0)
    second_rect = flow.placement[1].rect
    probe = QPoint(second_rect.center().x() - 40, second_rect.center().y() + 40)
    same("同一处探针，布局给出的索引也是 0",
         flow.drop_index(probe, cards[0]), 0)
    same("旧布局判据在同一处会跳到队尾",
         _old_index(probe, [s.rect for s in flow.placement[1:]]), 2)
    flow.move_item(0, 2)
    flow.invalidate()
    flow.activate()
    host.repaint()
    app.processEvents()
    same("move_item 之后顺序变了（被拖的卡到末尾）", flow.widgets()[-1] is cards[0], True)
    host.hide()

    print("\n== DragGuard：漏了松手 / 切到后台 ==")
    hits: list[str] = []
    guard = DragGuard()
    guard.aborted.connect(hits.append)
    guard.start()
    check("起拖后定时器在跑", guard._timer.isActive())
    guard._tick()                       # 测试环境里没有真按下 → 等于"左键已松开"
    same("左键松开的判据会触发一次", len(hits), 1)
    check("原因写清楚了", "松开" in hits[0], hits[0])
    guard._tick()
    same("同一次拖动不会重复触发", len(hits), 1)
    check("触发后自己停表", not guard._timer.isActive())

    hits.clear()
    guard.start()
    guard._on_app_state(Qt.ApplicationActive)
    same("应用仍在前台时不打断", len(hits), 0)
    guard._on_app_state(Qt.ApplicationInactive)
    same("应用切到后台 → 触发", len(hits), 1)
    check("原因指出是切后台", "后台" in hits[0], hits[0])

    hits.clear()
    guard.start()
    guard.stop()
    guard._tick()
    same("停表之后再 tick 不再触发", len(hits), 0)
    check("重复 start/stop 不出错（连接去重）", (guard.start(), guard.stop(), True)[-1])

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for name in FAIL:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
