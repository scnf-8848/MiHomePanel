# SPDX-License-Identifier: GPL-3.0-or-later
"""离线验证「电脑自动化」四层窗口与功能区入口：不联网、不注册真热键、不碰设备。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_automation_ui.py``

四层职责（2026-11-13 用户改版）：

* ``AutomationListDialog``  —— 只列在用的规则（名称 / 触发键 / 启停 / 编辑 / 删除），
  底部「回收站」「日志」「＋ 新建规则」，**没有「完成」**（右上 × / 点遮罩关）；
* ``AutomationDialog``      —— 只编辑一条规则；冷静期在①触发块下、**没有启停**、
  **没有日志列表 / 没有测试发送**，保存成功即关闭；
* ``AutomationTrashDialog`` —— 还原 / 彻底删除（只有后者弹确认）；
* ``AutomationLogDialog``   —— 默认跨全部规则的触发历史，四类筛选 + 三档结果色 +
  新日志增量插入。

重点是那些只有真机才会暴露、但目前没法人工反复点的交互：三段式行控件构造时信号
早连导致崩、动作行右侧图标按钮被 padding 挤没、切换动作类型残留旧值、上下移边界
禁用、手动选键折叠、标题副标题同步、保存前校验与「保存即关闭」、启停落盘与
"在信号栈里重建卡片"、软删除后不注册热键、回收站二次确认归属、嵌套模态的 parent
与压暗归属、录制态点遮罩不能整页关掉、关窗后不补 ``_apply_theme()``。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_TMP = Path(tempfile.mkdtemp(prefix="mihome-auto-ui-"))
os.environ["MIHOME_DATA_DIR"] = str(_TMP / "data")

from PySide6.QtCore import QObject, QPoint, Qt, Signal  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QDialog, QFrame, QLabel, QLineEdit, QListWidget, QPushButton, QStyle,
    QStyleOptionButton, QTableWidget, QWidget,
)

from mihome.core import speaker as SPK  # noqa: E402
from mihome.core.automation import (  # noqa: E402
    Action, Condition, LogEntry, Rule, RuleSet, Trigger, validate_rule,
)
from mihome.core.automation import action_catalog as AC  # noqa: E402
from mihome.ui import automation as A  # noqa: E402
from mihome.ui import automation_style as AS  # noqa: E402
from mihome.ui import confirm as CF  # noqa: E402
from mihome.ui import automation_list as AL  # noqa: E402
from mihome.ui import automation_log as AG  # noqa: E402
from mihome.ui import automation_trash as AT  # noqa: E402
from mihome.ui import theme  # noqa: E402
from mihome.ui.side_nav import ZONE_KEYS, SideNav  # noqa: E402
from mihome.ui.widgets import ToggleSwitch  # noqa: E402

app = QApplication.instance() or QApplication([])
FAILS: list[str] = []
N = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global N
    N += 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAILS.append(f"{name} {detail}")


HOMES = {"1001": "1103 家", "1002": "共享家庭"}

#: 选择窗的候选设备（11-14 起动作 = 设备 + 功能）。顺序即 ``collect`` 的排序：
#: 在线的先、同房间按名字。离线那台仍然列出来但点不动。
SPK_FEATS = ("wake-up", "play-text", "execute-text-directive")
HUB_FEATS = ("emit-virtual-event",)


def dev(did: str, name: str, feats=SPK_FEATS, *, room: str = "客厅",
        online: bool = True) -> AC.ActionDevice:
    return AC.ActionDevice(did=did, name=name, room=room, online=online,
                           features=tuple(AC.feature(f) for f in feats))


def sample_devices() -> list[AC.ActionDevice]:
    return [
        dev("d1", "小爱音箱Pro"),
        dev("hub-a", "客厅中枢", HUB_FEATS),
        dev("d5", "阳台音箱", room="阳台", online=False),
    ]


class FakeService(QObject):
    log_added = Signal(object)
    status_changed = Signal()
    rules_changed = Signal()

    def __init__(self, path, entries=None, home: str = "1001") -> None:
        super().__init__()
        self.rules = RuleSet(path)
        self.rules.on_changed = self._on_rules_changed
        self.engine = SimpleNamespace(log=list(entries or []))
        self.syncs = 0
        # 真实服务上是 property（家庭 id）；这里做成普通属性，方便切家庭
        self.current_home = home
        # 当前家庭里能执行动作的设备（选择窗的候选）。专门用例里换成空列表
        self.devices = sample_devices()
        # 注册状态：默认"已注册"，专门用例里改成"未注册"检查颜色
        self.registered = True
        # 编辑窗的「归属家庭」只读展示要换成显示名
        self.store = SimpleNamespace(
            homes=lambda: [SimpleNamespace(id=hid, display_name=label)
                           for hid, label in HOMES.items()],
        )

    def home_label(self, home_id: str = "") -> str:
        want = home_id or self.current_home
        return HOMES.get(want, want)

    def _on_rules_changed(self) -> None:
        self.sync()
        self.rules_changed.emit()

    def status_for(self, keys: str) -> str:
        if not keys:
            return "未设置"
        return "已注册" if self.registered else "未注册"

    def action_devices(self, home: str | None = None) -> list[AC.ActionDevice]:
        return list(self.devices)

    def sync(self) -> None:
        self.syncs += 1


def button_with(box, name: str) -> QPushButton | None:
    return next((b for b in box.findChildren(QPushButton)
                 if b.objectName() == name), None)


def button_text(box, text: str) -> QPushButton | None:
    return next((b for b in box.findChildren(QPushButton)
                 if b.text() == text), None)


def combo_index(combo, data) -> int:
    return next((i for i in range(combo.count())
                 if combo.itemData(i) == data), -1)


def trig(*keys: str) -> list[Trigger]:
    """v3 的触发列表；旧用例按单个键位写，这里折成 Trigger 省得每处都展开。"""
    return [Trigger("keyboard", k) for k in keys]


def evt_action(name: str, did: str = "hub-a", device: str = "客厅中枢") -> Action:
    """一条「发送虚拟事件」动作（v4：设备 + 功能 + 按入参名存的参数）。"""
    return Action("device_action", did=did, device=device,
                  action="emit-virtual-event", args={"virtual-param": name})


def speak_action(text: str, did: str = "d1", device: str = "小爱音箱Pro") -> Action:
    """一条「执行文本指令」动作。"""
    return Action("device_action", did=did, device=device,
                  action="execute-text-directive", args={"text-content": text})


def sample_rules() -> list[Rule]:
    return [
        Rule(id="r1", name="进门播报", triggers=trig("Ctrl+Alt+F7"), enabled=True,
             home="1001",
             actions=[evt_action("极客版按键自动化执行")]),
        Rule(id="r2", name="离开时关灯", triggers=trig("F8"), enabled=True,
             home="1001",
             actions=[evt_action("全屋关灯")]),
        Rule(id="r3", name="空规则示例", triggers=[], enabled=False,
             home="1001",
             actions=[evt_action("待填")]),
    ]


def sample_logs() -> list[LogEntry]:
    now = time.time()
    return [
        LogEntry(now - 3600, "r1", "进门播报", "Ctrl+Alt+F7", "fired", "热键触发"),
        LogEntry(now - 120, "r1", "进门播报", "Ctrl+Alt+F7", "sent", "已发送虚拟事件「开灯」"),
        LogEntry(now - 60, "r2", "离开时关灯", "F8", "failed", "发送失败：云端超时"),
        LogEntry(now - 10, "r1", "进门播报", "Ctrl+Alt+F7", "cooldown", "冷静期拦截，剩余 0.6 秒"),
    ]


# ---------------------------------------------------------------------------
print("--- 列表窗：构造 / 空态 / 底部三个按钮 ---", flush=True)
service = FakeService(_TMP / "list-rules.json", sample_logs())
listing = AL.AutomationListDialog(service, theme.LIGHT)
check("列表窗能构造", isinstance(listing, QDialog))
labels = [w.objectName() for w in listing.findChildren(QLabel)]
check("空态提示可见", "EmptyHint" in labels)
check("列表窗不画三段式",
      not any("①" in w.text() or "②" in w.text() or "③" in w.text()
              for w in listing.findChildren(QLabel)))
check("有回收站按钮", button_text(listing, "回收站") is not None
      and button_text(listing, "回收站").objectName() == "Outline")
check("有日志按钮", button_text(listing, "日志") is not None
      and button_text(listing, "日志").objectName() == "Outline")
check("有新建规则按钮", button_with(listing, "Primary") is not None)
check("去掉了「完成」按钮", button_text(listing, "完成") is None)
check("新建按钮改成圆角矩形", "QPushButton#Primary { border-radius: 10px" in listing.styleSheet())
check("列表窗没有 QListWidget", not listing.findChildren(QListWidget))
check("关窗靠标题栏 × / 点遮罩", "完成" not in
      "".join(b.text() for b in listing.findChildren(QPushButton)))
src_list = (_ROOT / "mihome/ui/automation_list.py").read_text(encoding="utf-8")
check("列表卡片样式来自公共层", "common_qss(p)" in src_list
      and "QFrame#RuleCard, QFrame#TrashCard" in listing.styleSheet())
check("列表窗按屏幕限高", "fit_height(580, reserve=2 * SHADOW_PAD)" in src_list)

print("\n--- 列表窗：卡片 / 启停落盘 / 排队重建 ---", flush=True)
service.rules.rules = sample_rules()
service.rules.save()
listing._refresh()
cards = listing.findChildren(AL.RuleCard)
check("三条规则三张卡", len(cards) == 3, str(len(cards)))
check("标题栏报规则数", "3 条规则" in listing.title_bar._sub)
check("卡片显示名称与触发键",
      any(w.objectName() == "RuleName" and w.text() == "进门播报"
          for w in cards[0].findChildren(QLabel))
      and any(w.objectName() == "RuleKeys" and w.text() == "Ctrl+Alt+F7"
              for w in cards[0].findChildren(QLabel)))
check("未设键位显示占位",
      any(w.objectName() == "RuleKeys" and w.text() == "未设置快捷键"
          for w in cards[2].findChildren(QLabel)))
# 多触发（用户 11-13 第十条）：卡片上要能看出挂了几个键
multi_card = AL.RuleCard(Rule(id="mc", name="多触发",
                              triggers=trig("F7", "F8", "F9", "F10"),
                              actions=[evt_action("A")]),
                         theme.LIGHT, lambda *_: None, lambda *_: None, lambda *_: None)
_mkeys = next(w for w in multi_card.findChildren(QLabel) if w.objectName() == "RuleKeys")
check("列表卡片多触发正确呈现",
      _mkeys.text() == "F7 / F8 / F9 / …（共 4 个）", _mkeys.text())
check("多触发卡片的 tooltip 是完整键位",
      _mkeys.toolTip() == "F7、F8、F9、F10", _mkeys.toolTip())
multi_card.deleteLater()
check("启用描边用属性选择器", cards[0].property("on") == "true"
      and cards[2].property("on") == "false")
check("每张卡有编辑与删除",
      button_with(cards[0], "RuleEdit") is not None
      and button_with(cards[0], "RuleDelete") is not None)

listing.service.rules_changed.disconnect(listing._schedule_refresh)
card = cards[0]
rid = card.rule_id
QTest.mouseClick(card.switch, Qt.LeftButton)
check("点击开关写回 RuleSet", service.rules.get(rid).enabled is False)
check("开关自身切到关", card.switch.is_on() is False)
check("描边切到停用色", card.property("on") == "false")
check("停用状态已落盘",
      '"enabled": false' in (_TMP / "list-rules.json").read_text(encoding="utf-8"))
listing._schedule_refresh()
QApplication.processEvents()
check("排队重建后卡片数不变", len(listing.findChildren(AL.RuleCard)) == 3)
check("排队重建后描边跟着落盘值",
      listing.findChildren(AL.RuleCard)[0].property("on") == "false")

# ---------------------------------------------------------------------------
print("\n--- 列表窗：规则按家庭隔离（2026-11-13 第二轮用户要求） ---", flush=True)
home_service = FakeService(_TMP / "home-rules.json", [], home="1001")
home_service.rules.rules = [
    Rule(id="a1", name="1103 的规则", triggers=trig("F1"), home="1001",
         actions=[evt_action("开灯")]),
    Rule(id="a2", name="共享家庭的规则", triggers=trig("F2"), home="1002",
         actions=[evt_action("关灯")]),
    Rule(id="a3", name="未指定家庭的规则", triggers=trig("F3"),
         actions=[evt_action("待归")]),
]
home_service.rules.save()
home_list = AL.AutomationListDialog(home_service, theme.LIGHT)


def _rule_names(dlg) -> list[str]:
    return [w.text() for w in dlg.findChildren(QLabel) if w.objectName() == "RuleName"]


def _home_badges(dlg) -> list[str]:
    return [w.text() for w in dlg.findChildren(QLabel) if w.objectName() == "RuleBadge"]


check("1103 家庭只列出自己那条 + 未指定家庭的",
      _rule_names(home_list) == ["1103 的规则", "未指定家庭的规则"],
      str(_rule_names(home_list)))
check("共享家庭的规则一条都不出现", "共享家庭的规则" not in _rule_names(home_list))
check("标题栏按本家庭计数", "2 条规则" in home_list.title_bar._sub, home_list.title_bar._sub)
_list_hint = next((w.text() for w in home_list.findChildren(QLabel)
                   if w.objectName() == "ListHint"), "")
check("提示条写明只显示本家庭（显示名）", "1103 家" in _list_hint, _list_hint)
check("提示条写明旧规则保存后会归到当前家庭", "归到当前家庭" in _list_hint, _list_hint)
check("未指定家庭的卡片有可辨认标记",
      _home_badges(home_list) == ["未指定家庭 · 保存后归入当前家庭"],
      str(_home_badges(home_list)))

home_service.current_home = "1002"
home_list._refresh()
check("切到共享家庭只剩它自己的 + 未指定家庭的",
      _rule_names(home_list) == ["共享家庭的规则", "未指定家庭的规则"],
      str(_rule_names(home_list)))
check("提示条跟着换成新家庭的显示名", "共享家庭" in home_list.hint.text(),
      home_list.hint.text())

# 未指定家庭的规则不会被自动认领：切几次家庭它还是空的
home_service.current_home = "1001"
home_list._refresh()
check("空归属不会被静默认领", home_service.rules.get("a3").home == "")

home_service.rules.trash("a2")
check("回收站也只放本家庭的", home_service.rules.trashed("1001") == []
      and [r.id for r in home_service.rules.trashed("1002")] == ["a2"])
home_list.close()

# 一条规则都没有时，空态要说明"只显示本家庭"——否则换个家庭看到 0 条会以为规则丢了
empty_service = FakeService(_TMP / "home-empty.json", [], home="1002")
empty_service.rules.rules = [Rule(id="x1", name="别家的", triggers=trig("F5"), home="1001",
                                  actions=[evt_action("开灯")])]
empty_service.rules.save()
empty_list = AL.AutomationListDialog(empty_service, theme.LIGHT)
_empty = next((w.text() for w in empty_list.findChildren(QLabel)
               if w.objectName() == "EmptyHint"), "")
check("本家庭 0 条时是空态", bool(_empty), _empty)
check("空态里写清楚只显示本家庭", "共享家庭" in _empty, _empty)
empty_list.close()

# ---------------------------------------------------------------------------
print("\n--- 启停不位移 / 滚动条常驻占位（2026-11-13 用户 2 报） ---", flush=True)
src_style = (_ROOT / "mihome/ui/automation_style.py").read_text(encoding="utf-8")
check("停用描边与启用描边同宽（只换颜色）",
      "border: 2px solid {p.border}" in src_style
      and '[on="true"] {{ border: 2px solid {p.accent}; }}' in src_style)

# 滚动条：AsNeeded 下条目一多滚动条就挤掉 10px，卡片整排变窄（用户 11-13 报）。
# 现在常驻占位，宽度恒定；不能滚时靠 idle 属性把滑块藏掉。
check("列表窗滚动条常驻占位", listing.scroll.verticalScrollBarPolicy() == Qt.ScrollBarAlwaysOn)
check("列表窗横向滚动条禁掉", listing.scroll.horizontalScrollBarPolicy() == Qt.ScrollBarAlwaysOff)
check("idle 隐藏滑块的规则拼进了窗口样式",
      'QScrollBar[idle="true"]::handle:vertical' in listing.styleSheet())


def bar_strip_colors(dlg, bar) -> set[str]:
    """滚动条那一竖条的取色：判"滑块画没画"用（轨道是透明的，像素就等于窗口底）。"""
    img = dlg.grab().toImage()
    tl = bar.mapTo(dlg, QPoint(0, 0))
    out = set()
    for x in range(tl.x() + 1, tl.x() + bar.width() - 1):
        for y in range(tl.y() + 6, tl.y() + bar.height() - 6, 5):
            out.add(img.pixelColor(x, y).name())
    return out


scroll_service = FakeService(_TMP / "scroll-rules.json", sample_logs())
scroll_service.rules.rules = [
    Rule(id=f"s{i}", name=f"规则 {i}", triggers=trig(f"Ctrl+Alt+F{i}"), enabled=(i % 2 == 0),
         actions=[evt_action("极客版按键自动化执行")])
    for i in range(8)
]
scroll_service.rules.save()
many = AL.AutomationListDialog(scroll_service, theme.LIGHT)
many.resize(640, 580)
many.show()
for _ in range(3):
    app.processEvents()
sb_bar = many.scroll.verticalScrollBar()
check("前置：8 条规则确实要滚", sb_bar.maximum() > 0
      and sb_bar.property("idle") is False, str(sb_bar.maximum()))

# 只断言 QSS 文本不够：真值要看内容区有没有跟着缩。以前 1px→2px，frameWidth
# 一变，标题 / 键位标签就地挪 1px，肉眼就是"启停一下卡片抖了一下"。
live = many.findChildren(AL.RuleCard)[0]
_lab = {w.objectName(): w for w in live.findChildren(QLabel)}


def _card_box() -> tuple:
    return (live.geometry().getRect(), live.contentsRect().getRect(),
            _lab["RuleName"].geometry().getRect(),
            _lab["RuleKeys"].geometry().getRect())


def _edge_inset(card) -> int:
    """卡片左边缘到"卡片底色"的像素距离 = 描边实际占了几 px。

    启停只该换描边的**颜色**：这个距离两个状态必须一样。以前停用 1px、启用 2px，
    这里会是 1 vs 2——内容区跟着缩 1px，就是用户看到的"启停一下卡片抖了一下"。
    """
    img = card.grab().toImage()
    y = card.height() // 2
    want = QColor(theme.panel_bg(theme.LIGHT)).name()
    for x in range(0, 6):
        if img.pixelColor(x, y).name() == want:
            return x
    return -1


toggled_from = (_card_box(), live.property("on"), _edge_inset(live))
live.set_enabled_state(live.property("on") != "true")
app.processEvents()
toggled_to = (_card_box(), live.property("on"), _edge_inset(live))
check("启停确实换了描边状态", toggled_from[1] != toggled_to[1],
      f"{toggled_from[1]} → {toggled_to[1]}")
check("两种状态的描边占位一样宽（内容区不缩）",
      toggled_from[2] == toggled_to[2] == 2,
      f"停用 {toggled_from[2]}px / 启用 {toggled_to[2]}px")
check("启停前后卡片与内部文字一格都没挪", toggled_from[0] == toggled_to[0],
      f"{toggled_from[0]} → {toggled_to[0]}")
live.set_enabled_state(True)
app.processEvents()

many_w = (many.scroll.viewport().width(), many.findChildren(AL.RuleCard)[0].width())
check("能滚时滑块画出来了",
      bar_strip_colors(many, sb_bar) != {QColor(theme.surface_bg(theme.LIGHT)).name()},
      str(bar_strip_colors(many, sb_bar)))

scroll_service.rules.rules = scroll_service.rules.rules[:2]
scroll_service.rules.save()
many._refresh()
for _ in range(3):
    app.processEvents()
few_w = (many.scroll.viewport().width(), many.findChildren(AL.RuleCard)[0].width())
check("只剩 2 条时卡片宽度不变", few_w == many_w, f"{many_w} → {few_w}")
check("不能滚时滑块收起（只剩透明轨道）",
      bar_strip_colors(many, sb_bar) == {QColor(theme.surface_bg(theme.LIGHT)).name()},
      str(bar_strip_colors(many, sb_bar)))
check("滚动范围归零时 idle 属性置上", sb_bar.property("idle") is True)

scroll_service.rules.rules += [
    Rule(id=f"t{i}", name=f"补 {i}", triggers=trig(f"F{i}"),
         actions=[Action("delay", "", 100)])
    for i in range(6)
]
scroll_service.rules.save()
many._refresh()
for _ in range(3):
    app.processEvents()
check("再加回到 8 条，卡片宽度还是那个值",
      (many.scroll.viewport().width(), many.findChildren(AL.RuleCard)[0].width()) == many_w)
check("又能滚了，滑块回来", sb_bar.property("idle") is False)
many.close()

trash_service = FakeService(_TMP / "scroll-trash.json", sample_logs())
trash_service.rules.rules = [
    Rule(id=f"d{i}", name=f"删掉的 {i}", triggers=trig(f"Ctrl+Alt+F{i}"), enabled=False,
         deleted=True, deleted_at=time.time() - i * 60,
         actions=[evt_action("极客版按键自动化执行")])
    for i in range(10)
]
trash_service.rules.save()
t_many = AT.AutomationTrashDialog(trash_service, theme.LIGHT)
t_many.resize(620, 500)
t_many.show()
for _ in range(3):
    app.processEvents()
t_bar = t_many.scroll.verticalScrollBar()
check("回收站也常驻占位", t_many.scroll.verticalScrollBarPolicy() == Qt.ScrollBarAlwaysOn)
check("前置：回收站 10 条要滚", t_bar.maximum() > 0, str(t_bar.maximum()))
t_many_w = (t_many.scroll.viewport().width(), t_many.findChildren(AT.TrashCard)[0].width())
trash_service.rules.rules = trash_service.rules.rules[:2]
trash_service.rules.save()
t_many._refresh()
for _ in range(3):
    app.processEvents()
check("回收站只剩 2 条时卡片宽度不变",
      (t_many.scroll.viewport().width(),
       t_many.findChildren(AT.TrashCard)[0].width()) == t_many_w,
      f"{t_many_w} → {(t_many.scroll.viewport().width(), t_many.findChildren(AT.TrashCard)[0].width())}")
check("回收站不能滚时滑块也收起",
      bar_strip_colors(t_many, t_bar) == {QColor(theme.surface_bg(theme.LIGHT)).name()},
      str(bar_strip_colors(t_many, t_bar)))
t_many.close()

print("\n--- 列表窗：编辑 / 新建 / 回收站 / 日志入口（不真开模态） ---", flush=True)
opened: list[QDialog] = []
_orig_exec = AL.exec_with_dim_click
AL.exec_with_dim_click = lambda dlg: opened.append(dlg) or 0
try:
    fresh = listing.findChildren(AL.RuleCard)[0]
    button_with(fresh, "RuleEdit").click()
    check("点编辑打开编辑窗", len(opened) == 1
          and isinstance(opened[-1], A.AutomationDialog))
    check("编辑窗拿到该规则", opened[-1]._draft.id == rid
          and opened[-1]._is_new is False)
    check("编辑窗 parent 是列表窗", opened[-1].parentWidget() is listing)

    button_with(listing, "Primary").click()
    check("点新建打开空白编辑窗", len(opened) == 2
          and opened[-1]._is_new is True and opened[-1]._draft.triggers == [])
    check("新建窗 parent 是列表窗", opened[-1].parentWidget() is listing)

    button_text(listing, "回收站").click()
    check("点回收站打开回收站窗", len(opened) == 3
          and isinstance(opened[-1], AT.AutomationTrashDialog))
    check("回收站窗 parent 是列表窗", opened[-1].parentWidget() is listing)

    button_text(listing, "日志").click()
    check("点日志打开独立日志窗", len(opened) == 4
          and isinstance(opened[-1], AG.AutomationLogDialog))
    check("日志窗 parent 是列表窗", opened[-1].parentWidget() is listing)
finally:
    AL.exec_with_dim_click = _orig_exec

print("\n--- 列表窗：删除 = 软删除，不再二次确认 ---", flush=True)
before = len(service.rules.rules)
target = listing.findChildren(AL.RuleCard)[0]
button_with(target, "RuleDelete").click()
check("删除后规则仍在 RuleSet（软删除）", len(service.rules.rules) == before)
check("被删规则默认 get 不到", service.rules.get(rid) is None
      and service.rules.get(rid, include_deleted=True).deleted is True)
check("被删规则不注册热键", "Ctrl+Alt+F7" not in service.rules.enabled_keys())
check("删除标记已落盘", '"deleted": true' in
      (_TMP / "list-rules.json").read_text(encoding="utf-8"))
listing._schedule_refresh()
QApplication.processEvents()
check("刷新后少一张卡", len(listing.findChildren(AL.RuleCard)) == before - 1)
check("回收站按钮 tooltip 报数量", "1 条已删除规则" in listing.trash_btn.toolTip())
check("列表删除路径没有 confirm 调用", "confirm(" not in src_list)

# ---------------------------------------------------------------------------
print("\n--- 回收站窗：还原 / 彻底删除 / 危险按钮权重 ---", flush=True)
trash = AT.AutomationTrashDialog(service, theme.LIGHT, listing)
tcards = trash.findChildren(AT.TrashCard)
check("回收站列出被删规则", len(tcards) == 1 and tcards[0].rule_id == rid)
check("标题栏报已删数", "1 条已删除规则" in trash.title_bar._sub)
check("卡上有还原与彻底删除",
      button_with(tcards[0], "Restore") is not None
      and button_with(tcards[0], "Purge") is not None)
src_trash = (_ROOT / "mihome/ui/automation_trash.py").read_text(encoding="utf-8")
check("彻底删除是红色描边幽灵按钮",
      "QPushButton#Purge" in trash.styleSheet()
      and "border: 1px solid {theme.blend(theme.DANGER" in src_trash)
check("彻底删除与还原间距拉开", "arow.setSpacing(10)" in src_trash)
check("回收站卡片与列表共用公共样式", "common_qss(p)" in src_trash)
check("回收站按屏幕限高", "fit_height(500, reserve=2 * SHADOW_PAD)" in src_trash)
button_with(trash.findChildren(AT.TrashCard)[0], "Restore").click()
QApplication.processEvents()
check("还原后规则回到列表", service.rules.get(rid) is not None)
check("还原后回收站空了", service.rules.trashed() == [])
# 这条规则在前面「启停落盘」里被关掉了；重新启用后，删除标记不该再挡着注册
service.rules.set_enabled(rid, True)
check("还原后可重新启用并注册热键", "Ctrl+Alt+F7" in service.rules.enabled_keys())

service.rules.trash(rid, at=time.time())
trash._refresh()
tcard = trash.findChildren(AT.TrashCard)[0]
_orig_confirm = AT.confirm
try:
    AT.confirm = lambda *a, **k: False
    button_with(tcard, "Purge").click()
    check("彻底删除取消时不移除", service.rules.get(rid, include_deleted=True) is not None)
    AT.confirm = lambda *a, **k: True
    button_with(tcard, "Purge").click()
    QApplication.processEvents()
    check("彻底删除确认后物理移除",
          service.rules.get(rid, include_deleted=True) is None)
    check("彻底删除后回收站空态可见",
          any(w.objectName() == "TrashEmpty" for w in trash.findChildren(QLabel)))
finally:
    AT.confirm = _orig_confirm

# ---------------------------------------------------------------------------
print("\n--- 独立日志窗：默认跨全部规则 / 筛选 / 三档色 ---", flush=True)
logdlg = AG.AutomationLogDialog(service, theme.LIGHT, listing)
check("日志窗是独立窗口", isinstance(logdlg, QDialog))
check("默认显示所有规则", logdlg.table.rowCount() == 4,
      str(logdlg.table.rowCount()))
check("默认最新的在最上面", "冷静期拦截" in logdlg.table.item(0, 4).text())
check("五列表头", [logdlg.table.horizontalHeaderItem(i).text()
                 for i in range(5)] == ["时刻", "规则", "快捷键", "结果", "说明"])
check("规则下拉首项是全部规则", logdlg.rule_combo.itemData(0) == "")
check("状态条说明日志只在内存", "只保存在内存" in logdlg.status.text())
check("日志窗不画三段式", not any(
    "①" in w.text() or "②" in w.text() or "③" in w.text()
    for w in logdlg.findChildren(QLabel)))
check("搜索框提示能搜快捷键", "快捷键" in logdlg.search.placeholderText())
check("筛选 chip 默认隐藏", logdlg.filter_chip.isHidden())
check("空态不再提「测试发送」", "测试发送" not in logdlg.empty.text())
check("日志表格竖向滚动条常驻占位（列宽不随条的出现而跳）",
      logdlg.table.verticalScrollBarPolicy() == Qt.ScrollBarAlwaysOn)
check("日志表格横向条仍保留（列多时要能滚）",
      logdlg.table.horizontalScrollBarPolicy() == Qt.ScrollBarAsNeeded)
check("表格滚动条也带 idle 属性（能滚时不隐藏滑块）",
      logdlg.table.verticalScrollBar().property("idle") is False,
      repr(logdlg.table.verticalScrollBar().property("idle")))

tone_colors = {logdlg.table.item(i, 3).text():
               logdlg.table.item(i, 3).foreground().color().name()
               for i in range(logdlg.table.rowCount())}
check("结果列三档颜色互不相同", len(set(tone_colors.values())) == 3,
      str(tone_colors))
check("冷静期拦截不是「失败」那个红",
      tone_colors["冷静期拦截"] != tone_colors["失败"])
check("成功与失败颜色不同", tone_colors["触发"] != tone_colors["失败"])

logdlg.search.setText("失败")
check("关键字搜索命中说明", logdlg.table.rowCount() == 1
      and "云端超时" in logdlg.table.item(0, 4).text())
check("筛选后 chip 出现", not logdlg.filter_chip.isHidden())
logdlg._reset()
check("重置回到全部", logdlg.table.rowCount() == 4)
check("重置后 chip 隐藏", logdlg.filter_chip.isHidden())
i_r2 = combo_index(logdlg.rule_combo, "r2")
logdlg.rule_combo.setCurrentIndex(i_r2)
check("按规则筛选", logdlg.table.rowCount() == 1
      and "离开时关灯" in logdlg.table.item(0, 1).text())
check("非默认值下拉加了 accent 描边", theme.LIGHT.accent in logdlg.rule_combo.styleSheet())
logdlg.rule_combo.setCurrentIndex(0)
check("回到默认后描边撤掉", theme.LIGHT.accent not in logdlg.rule_combo.styleSheet())
i_failed = combo_index(logdlg.outcome_combo, "failed")
logdlg.outcome_combo.setCurrentIndex(i_failed)
check("按结果筛选", logdlg.table.rowCount() == 1
      and "失败" in logdlg.table.item(0, 3).text())
logdlg.outcome_combo.setCurrentIndex(0)
i_5m = combo_index(logdlg.range_combo, 300)
logdlg.range_combo.setCurrentIndex(i_5m)
check("按时间范围筛选（最近 5 分钟）", logdlg.table.rowCount() == 3,
      str(logdlg.table.rowCount()))
check("时间范围也算筛选", not logdlg.filter_chip.isHidden())
logdlg.range_combo.setCurrentIndex(0)
logdlg.search.setText("不存在的词")
check("无结果时显示空态", logdlg.table.isHidden() and not logdlg.empty.isHidden())
logdlg._reset()
check("空态恢复后表格回来", not logdlg.table.isHidden() and logdlg.empty.isHidden())

new_entry = LogEntry(time.time(), "r1", "进门播报", "Ctrl+Alt+F7", "fired", "实时新增一条")
service.engine.log.append(new_entry)
logdlg.table.selectRow(1)
selected_before = logdlg.table.item(1, 4).text()
service.log_added.emit(new_entry)
check("新日志走增量插入", logdlg.table.rowCount() == 5
      and "实时新增一条" in logdlg.table.item(0, 4).text())
check("增量插入保留选中内容", logdlg.table.item(2, 4).text() == selected_before)
check("单元格 tooltip 有全文", "进门播报" in logdlg.table.item(0, 0).toolTip())
check("日志窗里有 QTableWidget", bool(logdlg.findChildren(QTableWidget)))

# 历史为空时不能滚：常驻的那条空轨道要看不见（idle → 滑块透明）
empty_log = AG.AutomationLogDialog(
    FakeService(_TMP / "log-empty.json", []), theme.LIGHT, listing)
check("历史为空时表格滚动条进 idle（只剩透明轨道）",
      empty_log.table.verticalScrollBar().property("idle") is True,
      repr(empty_log.table.verticalScrollBar().property("idle")))
empty_log.close()

# ---------------------------------------------------------------------------
print("\n--- 编辑窗：构造 / 四段层级 / 无启停 / 无测试发送 ---", flush=True)
service2 = FakeService(_TMP / "editor-rules.json", sample_logs())
dlg = A.AutomationDialog(service2, theme.LIGHT)
check("新建模式", dlg._is_new is True and dlg._draft.id)
badges = [b.text() for b in dlg.findChildren(QLabel) if b.objectName() == "StepBadge"]
check("四段编号圆徽章", badges == ["1", "2", "3", "4"], str(badges))
# 必须 startswith：「则执行动作」是「否则执行动作」的子串，用 in 会让 ③ 的检查
# 被 ④ 顺带满足，缺了 ③ 也测不出来
check("四段标题齐全",
      all(any(t.text().startswith(k) for t in dlg.findChildren(QLabel)
              if t.objectName() == "SectionTitle")
          for k in ("当以下情况发生时", "状态条件", "则执行动作", "否则执行动作")))
# QLabel 也继承自 QFrame，编号徽章同样带 step；这里只数四段卡片
steps = sorted(str(c.property("step")) for c in dlg.findChildren(QFrame)
               if c.objectName() == "SectionCard" and c.property("step") is not None)
check("四段卡片各有一条语义竖条", steps == ["1", "2", "3", "4"], str(steps))
check("冷静期默认 1000ms 且界面明示", dlg.cooldown.text() == "1000")
check("新建规则默认没有触发行（点按钮才加）", len(dlg._trigger_rows) == 0)
check("① 的按钮是「＋ 添加触发」（③ 改成两枚入口后不再叫「添加动作」）",
      dlg.add_trigger_btn.text() == "＋ 添加触发", dlg.add_trigger_btn.text())
check("① 添加按钮和冷静期在同一行",
      dlg.add_trigger_btn.parentWidget() is dlg.cooldown.parentWidget()
      and dlg.add_trigger_btn.parentWidget().layout().indexOf(dlg.cooldown) >= 0,
      type(dlg.cooldown.parentWidget()).__name__)
check("冷静期标签紧挨输入框（同一行、不是被推到最左）",
      dlg.cooldown.parentWidget().layout().indexOf(dlg.cooldown) ==
      dlg.cooldown.parentWidget().layout().indexOf(
          next(w for w in dlg.cooldown.parentWidget().findChildren(QLabel)
               if w.text() == "冷静期（毫秒）")) + 1)
check("冷静期单位仍是毫秒", "冷静期（毫秒）" in
      "".join(t.text() for t in dlg.findChildren(QLabel)))
check("冷静期在①触发卡内", dlg.cooldown in dlg.trigger_card.findChildren(QLineEdit))
check("冷静期不在头部卡内", dlg.cooldown not in dlg.head_card.findChildren(QLineEdit))
check("编辑窗没有启停开关", not dlg.findChildren(ToggleSwitch)
      and not hasattr(dlg, "enabled"))
check("编辑窗没有日志列表", not dlg.findChildren(QListWidget)
      and not hasattr(dlg, "log_list"))
check("编辑窗没有测试发送按钮", button_text(dlg, "测试发送") is None)
check("编辑窗没有结果回显", not hasattr(dlg, "last_result"))
check("新建规则默认没有动作行（点了按钮才加）", len(dlg._action_rows) == 0)
check("③ 空态提示指向那两枚按钮",
      not dlg.action_empty.isHidden() and dlg.action_empty.text() == A.ACTION_HINT_EMPTY,
      dlg.action_empty.text())
check("③ 两枚入口：＋ 选择设备 / ＋ 延迟",
      dlg.add_action_btn.text() == "＋ 选择设备"
      and dlg.add_delay_btn.text() == "＋ 延迟",
      f"{dlg.add_action_btn.text()} / {dlg.add_delay_btn.text()}")
check("④ 两枚入口文案与 ③ 一致（共用同一款 ActionRow）",
      dlg.add_else_btn.text() == "＋ 选择设备"
      and dlg.add_else_delay_btn.text() == "＋ 延迟",
      f"{dlg.add_else_btn.text()} / {dlg.add_else_delay_btn.text()}")
check("两枚按钮的 tooltip 说清各自加什么",
      "中枢网关" in dlg.add_action_btn.toolTip()
      and "等一段" in dlg.add_delay_btn.toolTip(),
      f"{dlg.add_action_btn.toolTip()} / {dlg.add_delay_btn.toolTip()}")
check("默认没有条件", len(dlg._cond_rows) == 0 and not dlg.cond_empty_box.isHidden())
check("0 条条件时空态块的按钮上方分隔线让位（不叠线）",
      dlg.cond_btns_divider.isHidden())
check("条件关系下拉在 ② 卡片外（标题行里），不占卡片行",
      not dlg.cond_card.isAncestorOf(dlg.mode))
check("只有 0 条条件时条件关系也常显", not dlg.mode.isHidden())
check("编辑页不再显示「中枢 / 云端返回」说明条", not hasattr(dlg, "target_label"))
_titles = [w.text() for w in dlg.findChildren(QLabel) if w.objectName() == "SectionTitle"]
check("条件标题就是「状态条件」，不再带「可以为空」",
      "状态条件" in _titles and not any("可以为空" in t for t in _titles), str(_titles))
check("「可以为空」落在卡片内的空态行上",
      dlg.cond_empty.text().startswith("可以为空：") and "执行动作" in dlg.cond_empty.text(),
      dlg.cond_empty.text())
check("归属家庭下面不再有说明小字",
      "创建后固定" not in dlg.home_value.toolTip()
      and not any(w.objectName() == "RowSub" and "创建后固定" in w.text()
                  for w in dlg.findChildren(QLabel)))
check("编辑窗不画规则列表", not dlg.findChildren(AL.RuleCard))
check("编辑窗没有新建规则按钮", button_with(dlg, "Primary") is None
      or button_with(dlg, "Primary").text() != "＋ 新建规则")

print("\n--- 编辑窗：动作行右侧控件（用户截图报「根本看不到」） ---", flush=True)
# 先加一条延迟：延迟行是**最短**的形态（只有单位 + 数值），正好用来量右侧按钮
dlg._add_action(A._new_delay())  # noqa: SLF001  内部 API，验证脚本直接用
row0 = dlg._action_rows[0]
check("加了一行后空态提示收起", dlg.action_empty.isHidden())
icons = (row0.up_btn, row0.down_btn, row0.delete_btn)
check("三枚都是独立 IconTool 样式", all(b.objectName() == "IconTool" for b in icons))
check("图标按钮命中区 ≥28px", all(b.width() >= 28 and b.height() >= 28 for b in icons))
check("IconTool 把 padding 归零", "QPushButton#IconTool" in dlg.styleSheet()
      and "padding: 0;" in dlg.styleSheet())
check("图标按钮都有 tooltip 与无障碍名",
      all(b.toolTip() and b.accessibleName() for b in icons))
check("单行时上下移也可点（用户 11-13 第十二条：不再置灰）",
      row0.up_btn.isEnabled() and row0.down_btn.isEnabled())
check("边界箭头 tooltip 说明按了也不会动", "已经是第一条" in row0.up_btn.toolTip()
      and "已经是最后一条" in row0.down_btn.toolTip())
dlg.name_edit.setText("改名测试")
check("标题副标题随名称同步", dlg.title_bar._sub == "改名测试")

print("\n--- 编辑窗：选键 / 条件 / 动作 ---", flush=True)
# ① 也改成"点按钮加一行"：加出来停在空档，下拉里目前只有「键盘快捷键」
dlg._add_trigger()
tr0 = dlg._trigger_rows[0]
check("点①的添加按钮后出现一条触发行",
      len(dlg._trigger_rows) == 1 and tr0.kind.currentData() == "")
check("触发类型下拉只有空档 + 键盘快捷键两项",
      tr0.kind.count() == 2
      and tr0.kind.itemData(1) == "keyboard"
      and tr0.kind.itemText(1) == "键盘快捷键",
      f"{tr0.kind.count()} 项")
check("空档触发行里键帽先禁用", not tr0.keycap.isEnabled())
tr0.kind.setCurrentIndex(tr0.kind.findData("keyboard"))
check("选键盘快捷键后键帽可用", tr0.keycap.isEnabled())
check("注册状态并进快捷键行", tr0.status.parentWidget() is tr0.keycap.parentWidget())
check("手动选键默认收起", tr0.manual.isHidden())
tr0.manual_toggle.setChecked(True)
check("点开才展开手动选键", not tr0.manual.isHidden())
tr0.manual_toggle.setChecked(False)
# ① 允许多条触发：再加一条不会把按钮禁掉，删回去也不影响第一条
dlg._add_trigger()
check("可以加第二条触发行（多触发 = 任一命中）", len(dlg._trigger_rows) == 2)
check("第二行是独立的空档行", dlg._trigger_rows[1].kind.currentData() == "")
dlg._remove_trigger(dlg._trigger_rows[1])
check("删掉第二条只影响它自己", len(dlg._trigger_rows) == 1
      and dlg._trigger_rows[0] is tr0 and tr0.kind.currentData() == "keyboard")

# 版式基准：③ 序号之后的下拉框 = 44px（16 内边距 + 20 序号 + 8 行距）。
# ① 的下拉、② 的条件行/按钮、三块底部按钮都用 28px 左边距对齐到这条线，
# 不再有 104px 的空占位列（用户 11-13 第六、七次调整）。
# 量的是**设备动作行**：延迟行没有设备 / 功能两列，左边第一格是单位下拉。
dlg._add_action(Action("device_action", did="d1", device="小爱音箱Pro",
                       action="execute-text-directive", args={"text-content": "开灯"}))
dev_row = dlg._action_rows[1]
dlg.resize(920, 760)
dlg.show()
app.processEvents()


def _left(widget) -> int:
    return widget.mapTo(dlg, QPoint(0, 0)).x()


_action_kind = dev_row.kind
check("①下拉 / ②条件按钮 / ③设备按钮 / 三块按钮都在同一条 44px 线",
      _left(tr0.kind) == _left(dlg.add_cond_btn) == _left(dev_row.device_btn)
      == _left(dlg.add_action_btn) == _left(dlg.add_trigger_btn),
      f"{_left(tr0.kind)} / {_left(dlg.add_cond_btn)} / "
      f"{_left(dev_row.device_btn)} / {_left(dlg.add_action_btn)}")
check("③ 序号仍在最左，只靠 20px 序号列 + 8px 行距形成 28px 偏移",
      _left(dev_row.index_label) < _left(dev_row.device_btn)
      and _left(dev_row.device_btn) - _left(dev_row.index_label) == 28,
      f"{_left(dev_row.index_label)} → {_left(dev_row.device_btn)}")
check("设备名按钮右边紧跟功能下拉（固定宽度，不随内容跳）",
      _left(_action_kind) == _left(dev_row.device_btn) + A._DEVICE_W + A._ROW_GAP,
      f"{_left(dev_row.device_btn)} / {_left(_action_kind)}")
check("② 空态提示与添加按钮同一条 44px 左缘",
      _left(dlg.cond_empty) == _left(dlg.add_cond_btn),
      f"{_left(dlg.cond_empty)} / {_left(dlg.add_cond_btn)}")
check("空态提示上下两条分隔线也缩进到 44px、右缘到卡片内边距",
      _left(dlg.cond_empty_top) == _left(dlg.cond_empty_bottom) == _left(dlg.cond_empty)
      and dlg.cond_empty_top.width() == dlg.cond_empty.width()
      == dlg.cond_empty_bottom.width(),
      f"{_left(dlg.cond_empty_top)} / {_left(dlg.cond_empty_bottom)}")
check("①底部按钮与冷静期同一行（纵向中心一致）",
      dlg.add_trigger_btn.mapTo(dlg, QPoint(0, dlg.add_trigger_btn.height() // 2)).y()
      == dlg.cooldown.mapTo(dlg, QPoint(0, dlg.cooldown.height() // 2)).y(),
      f"{dlg.add_trigger_btn.mapTo(dlg, QPoint(0, dlg.add_trigger_btn.height() // 2)).y()} / "
      f"{dlg.cooldown.mapTo(dlg, QPoint(0, dlg.cooldown.height() // 2)).y()}")
check("冷静期输入框紧跟在它自己的标签右侧",
      0 < dlg.cooldown.mapTo(dlg, QPoint(0, 0)).x()
      - max(w.mapTo(dlg, QPoint(0, 0)).x() + w.width()
            for w in dlg.cooldown.parentWidget().findChildren(QLabel)
            if w.text() == "冷静期（毫秒）") <= 32)
dlg.hide()

tr0._mod_boxes["Ctrl"].setChecked(True)
tr0._mod_boxes["Alt"].setChecked(True)
tr0.key_combo.setCurrentIndex(tr0.key_combo.findData("F7"))
check("手动选键生成规范串", tr0.value().keys == "Ctrl+Alt+F7", tr0.value().keys)
check("键位显示在按钮上", tr0.keycap.text() == "Ctrl+Alt+F7")
check("注册成功才用绿色", tr0.status.objectName() == "Ok" and "已注册" in tr0.status.text(),
      f"{tr0.status.objectName()} / {tr0.status.text()}")
dlg.service.registered = False
dlg._refresh_status()
check("未注册用主文字色（不是绿色）",
      tr0.status.objectName() == "RegOff", tr0.status.objectName())
dlg.service.registered = True
dlg._refresh_status()

# 多触发：第二条行有自己的键位，保存时两条一起进 draft（OR 语义）
dlg._add_trigger()
tr1 = dlg._trigger_rows[1]
tr1.kind.setCurrentIndex(tr1.kind.findData("keyboard"))
tr1.set_keys("F8")
check("两条触发行各记各的键位",
      [t.keys for t in dlg._collect().triggers] == ["Ctrl+Alt+F7", "F8"],
      str([t.keys for t in dlg._collect().triggers]))
tr1.set_keys("ctrl+alt+f7")
check("同一规则里重复键位被校验拦下",
      any("用了同一个快捷键" in e for e in validate_rule(dlg._collect())),
      str(validate_rule(dlg._collect())))
tr1.kind.setCurrentIndex(0)
check("空档触发行点保存会被拦，行内说「要先选触发方式」",
      dlg._save(quiet=True) is False and tr1.hint.text() == "要先选触发方式",
      tr1.hint.text())
dlg._remove_trigger(tr1)
check("删掉空档行后只剩原来那条", len(dlg._trigger_rows) == 1)
dlg._add_condition(Condition("idle_minutes", "ge", "10"))
check("添加条件后一行", len(dlg._cond_rows) == 1 and dlg.cond_empty_box.isHidden())
check("有条件时空态块隐藏、按钮上方分隔线出现",
      dlg.cond_empty_box.isHidden() and not dlg.cond_btns_divider.isHidden())
check("只有一条条件时条件关系仍常显", not dlg.mode.isHidden())
check("条件行删除按钮也是图标按钮",
      dlg._cond_rows[0].remove_btn.objectName() == "IconTool")
dlg._cond_rows[0].arg.setText("25")
check("条件行能读回参数", dlg._cond_rows[0].value().arg == "25")
dlg._add_condition(Condition("idle_minutes", "ge", "5"))
check("两条条件时条件关系仍在标题右侧（常显）", not dlg.mode.isHidden())
dlg._remove_condition(dlg._cond_rows[-1])
check("删回一条时条件关系不消失", not dlg.mode.isHidden())
dlg._remove_condition(dlg._cond_rows[0])
check("删除条件回到空态", len(dlg._cond_rows) == 0 and not dlg.cond_empty_box.isHidden())
check("0 条条件时条件关系依旧常显", not dlg.mode.isHidden())

# ② 也改成「加一行 → 落在空档「-」上 → 用户自己选类型」（用户 11-13 第八条）
dlg._add_condition(Condition(type="", op=""))
blank_cond = dlg._cond_rows[-1]
check("② 加出来的条件行停在空档「-」上",
      blank_cond._kind_key() == "" and blank_cond.kind.itemText(0) == "-",
      f"{blank_cond._kind_key()!r} / {blank_cond.kind.itemText(0)!r}")
check("条件行的空档也是第一项（和 ③ 一致）",
      blank_cond.kind.itemData(0) == "" and blank_cond.kind.count() == 7,
      f"{blank_cond.kind.count()} 项")
check("空档条件行比较符 / 参数先灰着",
      not blank_cond.op.isEnabled() and not blank_cond.arg.isEnabled())
check("空档条件行 placeholder 说明先选类型",
      "先选" in blank_cond.arg.placeholderText(), blank_cond.arg.placeholderText())
check("空档条件刚加进来不亮红字（同 ③）",
      blank_cond.hint.text() == "" and blank_cond.hint.isHidden(),
      blank_cond.hint.text())
check("空档条件报「还没选条件类型」，而不是「类型未知」",
      any("还没选条件类型" in e for e in validate_rule(dlg._collect())),
      str(validate_rule(dlg._collect())))
# ② 的行内红字（用户 11-13 第九条）：参数为空的空档先让路，等选了类型再报
# ③ 这时是「延迟 + 执行文本指令」两条都填好的行，唯一会拦保存的就是上面这个空档条件
check("空档条件点保存：行内亮「要先选条件类型」",
      dlg._save(quiet=True) is False
      and blank_cond.hint.text() == "要先选条件类型"
      and not blank_cond.hint.isHidden(),
      f"{blank_cond.hint.text()!r} / hidden={blank_cond.hint.isHidden()}")
check("行内已经说了，底部错误条不再重复", dlg.error_label.text() == "",
      dlg.error_label.text())
check("空档条件被拦时焦点在类型下拉上",
      dlg.focusWidget() is blank_cond.kind, type(dlg.focusWidget()).__name__)

blank_cond.kind.setCurrentIndex(blank_cond.kind.findData("idle_minutes"))
check("选完类型后比较符 / 参数恢复可用",
      blank_cond.op.isEnabled() and blank_cond.arg.isEnabled()
      and blank_cond.op.currentData() == "ge", str(blank_cond.op.currentData()))
check("空档的「要先选条件类型」换成「要填参数」",
      blank_cond.hint.text() == "要填参数", blank_cond.hint.text())
check("红字排在参数框之后、× 之前（同一行，不另起一行）",
      blank_cond.layout().indexOf(blank_cond.hint)
      == blank_cond.layout().indexOf(blank_cond.target) + 1
      and blank_cond.layout().indexOf(blank_cond.hint) < blank_cond.layout().indexOf(
          blank_cond.remove_btn),
      f"arg#{blank_cond.layout().indexOf(blank_cond.arg)} "
      f"hint#{blank_cond.layout().indexOf(blank_cond.hint)}")
check("红字带 tooltip 说清填什么", bool(blank_cond.hint.toolTip()),
      blank_cond.hint.toolTip())
blank_cond.arg.setText("十分钟")
check("数字类条件填中文行内报「参数要填数字」",
      blank_cond.hint.text() == "参数要填数字", blank_cond.hint.text())
blank_cond.arg.setText("25")
check("填对之后红字立刻消失",
      blank_cond.hint.text() == "" and blank_cond.hint.isHidden(),
      blank_cond.hint.text())
check("填对之后能保存（条件不再是拦路的那条）",
      dlg._save(quiet=True), dlg.error_label.text())

# 其余几条判据要和 rules.validate_rule 逐条对齐，否则会出现"行内没红字、底部却报错"
blank_cond.kind.setCurrentIndex(blank_cond.kind.findData("locked"))
blank_cond.arg.setText("也许")
check("锁屏条件填「也许」行内报「填「是」或「否」」",
      blank_cond.hint.text() == "填「是」或「否」", blank_cond.hint.text())
blank_cond.arg.setText("否")
check("锁屏条件填「否」通过", blank_cond.hint.text() == "", blank_cond.hint.text())
blank_cond.kind.setCurrentIndex(blank_cond.kind.findData("power"))
blank_cond.arg.setText("煤电")
check("供电条件填「煤电」行内报「填 ac 或 battery」",
      blank_cond.hint.text() == "填 ac 或 battery", blank_cond.hint.text())
blank_cond.arg.setText("BATTERY")
check("ac / battery 不区分大小写", blank_cond.hint.text() == "", blank_cond.hint.text())

# 「程序已运行」要两格：缺哪格就报哪格，焦点跟着走
blank_cond.kind.setCurrentIndex(blank_cond.kind.findData("process_elapsed"))
blank_cond.arg.setText("20")
blank_cond.target.setText("")
check("缺程序名时行内报「要填程序名」且只报这一条",
      blank_cond.hint.text() == "要填程序名", blank_cond.hint.text())
check("缺程序名时焦点给程序名那格",
      dlg._save(quiet=True) is False and dlg.focusWidget() is blank_cond.target,
      type(dlg.focusWidget()).__name__)
blank_cond.target.setText("python.exe")
check("程序名填上后红字消失", blank_cond.hint.text() == "", blank_cond.hint.text())

# 改比较符以前既不过红字也不标脏（关窗不会提示未保存），顺手补的连接
dlg._dirty = False
blank_cond.op.setCurrentIndex(blank_cond.op.findData("le"))
check("改比较符也算改动（关窗会提示未保存）", dlg._dirty is True)
check("改比较符会重算红字（≥ / ≤ 都收数字，没红字）",
      blank_cond.hint.text() == "", blank_cond.hint.text())
dlg._remove_condition(blank_cond)
dlg._set_error("")

# 行内红字：文字的缺失 / 超长 / 发不出的字符，判据要和 rules.validate_rule 逐条对齐
dev_row.value_edit.clear()
check("文字没填行内报「要填文字」", dev_row.hint.text() == "要填文字", dev_row.hint.text())
check("红字带 tooltip 说清该填什么", bool(dev_row.hint.toolTip()), dev_row.hint.toolTip())
dev_row.value_edit.setText("灯" * (SPK.MAX_CHARS + 3))
check("超长不静默截断，行内报「多了 3 个字」",
      dev_row.hint.text() == "多了 3 个字", dev_row.hint.text())
check("超长的 tooltip 说清上限", str(SPK.MAX_CHARS) in dev_row.hint.toolTip(),
      dev_row.hint.toolTip())
dev_row.value_edit.setText("开灯~")
check("带白名单外的字符行内报「含 1 个发不出的字符」",
      dev_row.hint.text() == "含 1 个发不出的字符", dev_row.hint.text())
dev_row.value_edit.setText("极客版按键自动化执行")
check("填回合规文字后红字消失", dev_row.hint.text() == "", dev_row.hint.text())

check("value() 读出设备 + 功能 + 参数",
      dev_row.value().to_dict() == {
          "type": "device_action", "did": "d1", "device": "小爱音箱Pro",
          "action": "execute-text-directive",
          "args": {"text-content": "极客版按键自动化执行", "silent-execution": False}},
      str(dev_row.value().to_dict()))
dev_row.silent.setCurrentIndex(dev_row.silent.findData(True))
check("静默勾上后只是个参数，不改功能",
      dev_row.value().args.get("silent-execution") is True
      and dev_row.value().action == "execute-text-directive")
dev_row.silent.setCurrentIndex(dev_row.silent.findData(False))

# 无参功能**收掉整列**，不留一个灰着的空框（用户 11-14）：灰框会被当成"这里该填点什么"
dev_row.kind.setCurrentIndex(dev_row.kind.findData("wake-up"))
check("切到唤醒：输入框与静默列整列收掉，靠占位顶住右半边",
      dev_row.value_edit.isHidden() and dev_row.silent.isHidden()
      and not dev_row.spacer.isHidden())
check("无参功能行内不亮红字（没有可填的东西）",
      dev_row.hint.text() == "" and dev_row.hint.isHidden(), dev_row.hint.text())
check("无参功能的 args 是空字典（不写空键）", dev_row.value().args == {},
      str(dev_row.value().args))
dev_row.kind.setCurrentIndex(dev_row.kind.findData("play-text"))
check("切到播报文字：输入框回来、静默列不出现、占位让位",
      not dev_row.value_edit.isHidden() and dev_row.silent.isHidden()
      and dev_row.spacer.isHidden())
check("换功能会清掉上一功能的残留文字", dev_row.value_edit.text() == "",
      dev_row.value_edit.text())
check("播报文字的 placeholder 说明要念什么",
      "念出来" in dev_row.value_edit.placeholderText(),
      dev_row.value_edit.placeholderText())
dev_row.kind.setCurrentIndex(dev_row.kind.findData("execute-text-directive"))
check("切到执行文本指令：静默列出现且默认「不静默」",
      not dev_row.silent.isHidden() and dev_row.silent.currentData() is False)
check("功能下拉只列这台设备能用的三个（中枢的虚拟事件不混进来）",
      [dev_row.kind.itemText(i) for i in range(dev_row.kind.count())]
      == ["唤醒", "播报文字", "执行文本指令"],
      str([dev_row.kind.itemText(i) for i in range(dev_row.kind.count())]))
dev_row.value_edit.setText("极客版按键自动化执行")

# 设备名是个按钮：点它重开选择窗（不另做设备下拉，两套入口迟早不一致）
check("设备名按钮显示设备名", dev_row.device_btn.text() == "小爱音箱Pro",
      dev_row.device_btn.text())
check("设备名按钮的 tooltip 带房间与「点一下重选」",
      "客厅" in dev_row.device_btn.toolTip() and "重" in dev_row.device_btn.toolTip(),
      dev_row.device_btn.toolTip())
dev_row.set_device(sample_devices()[1])
check("换到中枢后功能下拉重建成中枢的能力",
      [dev_row.kind.itemText(i) for i in range(dev_row.kind.count())] == ["发送虚拟事件"],
      str([dev_row.kind.itemText(i) for i in range(dev_row.kind.count())]))
check("换设备后设备名按钮跟着变", dev_row.device_btn.text() == "客厅中枢",
      dev_row.device_btn.text())
dev_row.set_device(sample_devices()[0])
check("换回音箱后功能回到它的能力列表", dev_row.kind.count() == 3,
      str(dev_row.kind.count()))
dev_row.kind.setCurrentIndex(dev_row.kind.findData("execute-text-directive"))
dev_row.value_edit.setText("极客版按键自动化执行")

# 顺序 / 序号 / 上下移：两行就是「延迟 + 设备动作」
check("两行时序号连续", [r.index_label.text() for r in dlg._action_rows] == ["1.", "2."])
check("两行时四枚箭头全部可点（首行 ↑ / 末行 ↓ 只是按了不动）",
      all(r.up_btn.isEnabled() and r.down_btn.isEnabled()
          for r in dlg._action_rows))


def _types() -> list[str]:
    return [r.value().type for r in dlg._action_rows]


dlg._move_action(dlg._action_rows[0], -1)
check("首行按 ↑ 顺序不变", _types() == ["delay", "device_action"], str(_types()))
dlg._move_action(dlg._action_rows[-1], 1)
check("末行按 ↓ 顺序不变", _types() == ["delay", "device_action"], str(_types()))
dlg._move_action(dlg._action_rows[1], -1)
check("上移生效", _types() == ["device_action", "delay"], str(_types()))
check("顺序变了但每行的值跟着行走",
      dlg._action_rows[1].value().ms == 1000 and dlg._action_rows[1].value().unit == "ms",
      str(dlg._action_rows[1].value()))
check("移动后序号重排", [r.index_label.text() for r in dlg._action_rows] == ["1.", "2."])
check("移动后边界箭头仍可点", all(r.up_btn.isEnabled() and r.down_btn.isEnabled()
      for r in dlg._action_rows))
dlg._move_action(dlg._action_rows[0], 1)
check("下移生效", _types() == ["delay", "device_action"], str(_types()))

# 延迟的两种录入刻度：同一份毫秒值的两把尺子，换刻度只换算不重置
delay_row = dlg._action_rows[0]
delay_row.value_edit.setText("2000")
delay_row.unit.setCurrentIndex(delay_row.unit.findData("s"))
check("毫秒换成秒：2000 → 2", delay_row.value_edit.text() == "2",
      delay_row.value_edit.text())
check("秒档 placeholder 说明最多 3 位小数",
      "3 位小数" in delay_row.value_edit.placeholderText(),
      delay_row.value_edit.placeholderText())
delay_row.value_edit.setText("1.2345")
check("秒档 4 位小数行内报「最多 3 位小数」",
      delay_row.hint.text() == "最多 3 位小数", delay_row.hint.text())
delay_row.value_edit.setText("1.5")
check("秒档读回仍折成毫秒（1500 ms，unit=s 只用来回显）",
      (delay_row.value().ms, delay_row.value().unit) == (1500, "s"),
      str(delay_row.value()))
delay_row.unit.setCurrentIndex(delay_row.unit.findData("ms"))
check("秒换回毫秒：1.5 秒 → 1500", delay_row.value_edit.text() == "1500",
      delay_row.value_edit.text())
delay_row.value_edit.setText("abc")
check("毫秒档填字母行内报「只能填整数」",
      delay_row.hint.text() == "只能填整数", delay_row.hint.text())
delay_row.value_edit.setText("")
check("延迟没填行内报「要填延迟时间」",
      delay_row.hint.text() == "要填延迟时间", delay_row.hint.text())
delay_row.value_edit.setText("601000")
check("延迟超过上限行内立刻报（不用点保存）",
      delay_row.hint.text() == "不能超过 600000 毫秒", delay_row.hint.text())
delay_row.value_edit.setText("1000")
check("填回合法值后红字消失", delay_row.hint.text() == "" and delay_row.hint.isHidden())

dlg._remove_action(dev_row)
dlg._remove_action(delay_row)
check("删到 0 行时空态提示回来", len(dlg._action_rows) == 0
      and not dlg.action_empty.isHidden())
dlg._remove_action(dev_row)  # 已经删过，这次应该什么都不做
check("重复删同一行不炸", len(dlg._action_rows) == 0)

# 重新攒一条能保存的规则：触发 + 一条「执行文本指令」
dlg._add_action(Action("device_action", did="d1", device="小爱音箱Pro",
                       action="execute-text-directive",
                       args={"text-content": "极客版按键自动化执行"}))
dev_row = dlg._action_rows[0]
check("重新加回来的行回填了设备与功能",
      dev_row.device_btn.text() == "小爱音箱Pro"
      and dev_row._kind_key() == "execute-text-directive",
      f"{dev_row.device_btn.text()} / {dev_row._kind_key()}")
check("重新加回来的行回填了文字",
      dev_row.value_edit.text() == "极客版按键自动化执行", dev_row.value_edit.text())

print("\n--- 编辑窗：选择设备窗（11-14 新入口） ---", flush=True)
picked: list = []
_orig_pick = A.DevicePicker.pick
try:
    # 「＋ 选择设备」：先弹窗，选中才加行；取消什么都不加
    A.DevicePicker.pick = staticmethod(lambda *a, **k: sample_devices()[1])
    dlg._pick_and_add()
    check("选中设备后加出一行", len(dlg._action_rows) == 2)
    check("新行就是刚选的那台设备",
          dlg._action_rows[1].device_btn.text() == "客厅中枢",
          dlg._action_rows[1].device_btn.text())
    check("新行的功能默认落在第一个（中枢只有虚拟事件）",
          dlg._action_rows[1]._kind_key() == "emit-virtual-event",
          dlg._action_rows[1]._kind_key())
    A.DevicePicker.pick = staticmethod(lambda *a, **k: None)
    dlg._pick_and_add()
    check("取消选择就什么都不加", len(dlg._action_rows) == 2)
    dlg._pick_and_add(to_else=True)
    check("④ 也一样：取消不加行", len(dlg._else_rows) == 0)
    A.DevicePicker.pick = staticmethod(lambda *a, **k: sample_devices()[2])
    dlg._add_condition(Condition("idle_minutes", "ge", "10"))
    dlg._pick_and_add(to_else=True)
    check("④ 选中后加进否则队列而不是 ③", len(dlg._else_rows) == 1
          and len(dlg._action_rows) == 2)
    check("④ 的行独立编号（也从 1 开始）", dlg._else_rows[0].index_label.text() == "1.")
    check("④ 加到离线设备也照加（离线在执行时才报）",
          "（离线）" in dlg._else_rows[0].device_btn.text(),
          dlg._else_rows[0].device_btn.text())
    dlg._remove_else_action(dlg._else_rows[0])
    dlg._remove_condition(dlg._cond_rows[0])
    dlg._remove_action(dlg._action_rows[1])
finally:
    A.DevicePicker.pick = _orig_pick

# 选择窗本体：行文本、搜索、离线点不动、空列表
picker = A.DevicePicker(sample_devices(), theme.LIGHT)
check("选择窗每台设备一行", picker.list.count() == 3, str(picker.list.count()))
check("行文本 = 设备名 + 房间",
      picker.list.item(0).text() == "小爱音箱Pro　　客厅", picker.list.item(0).text())
check("离线那台在行尾标出来",
      picker.list.item(2).text().endswith("离线"), picker.list.item(2).text())
check("离线行不可选（点不动）",
      not (picker.list.item(2).flags() & Qt.ItemIsSelectable))
picker.search.setText("阳台")
check("按房间搜得到", not picker.list.item(2).isHidden()
      and picker.list.item(0).isHidden())
picker.search.setText("不存在")
check("搜不到时给空态文案", not picker.empty.isHidden()
      and picker.empty.text() == "没有匹配的设备。", picker.empty.text())
picker.search.clear()
check("清空搜索后列表回来", picker.empty.isHidden()
      and not picker.list.item(0).isHidden())
picker._on_item(picker.list.item(2))
check("点离线那台不会被选走", picker.chosen() is None, str(picker.chosen()))
picker._on_item(picker.list.item(1))
check("点在线那台即选中", picker.chosen() is not None
      and picker.chosen().did == "hub-a", str(picker.chosen()))
picker.close()
empty_picker = A.DevicePicker([], theme.LIGHT)
check("没有设备时列表收起、给空态文案",
      empty_picker.list.isHidden() and not empty_picker.empty.isHidden()
      and empty_picker.empty.text() == "当前家庭没有可执行动作的设备。",
      empty_picker.empty.text())
empty_picker.close()

print("\n--- 编辑窗：校验 / 保存即关闭 ---", flush=True)
dlg.name_edit.setText("测试播报")
dlg._action_rows[0].value_edit.setText("")
check("空文字保存被拦", dlg._save(quiet=True) is False)
check("行内红字是唯一提示（底部错误条不重复）",
      dlg._action_rows[0].hint.text() == "要填文字" and dlg.error_label.text() == "",
      f"{dlg._action_rows[0].hint.text()!r} / {dlg.error_label.text()!r}")
dlg._action_rows[0].value_edit.setText("极客版按键自动化执行")

accepted: list[int] = []
_orig_accept = QDialog.accept
QDialog.accept = lambda self: accepted.append(1)  # type: ignore[assignment]
try:
    dlg._on_save()
    check("点保存成功后自动关闭编辑窗", len(accepted) == 1)
finally:
    QDialog.accept = _orig_accept  # type: ignore[assignment]
check("新建规则落进 RuleSet", len(service2.rules.rules) == 1)
check("rules.json 已写入", (_TMP / "editor-rules.json").exists())
check("保存后不再是 dirty / 不再是新建",
      dlg._dirty is False and dlg._is_new is False)
saved = service2.rules.rules[0]
check("落盘内容含键位与冷静期", saved.keys == "Ctrl+Alt+F7" and saved.cooldown_ms == 1000)
check("动作只有一条执行文本指令", len(saved.actions) == 1
      and saved.actions[0].action == "execute-text-directive",
      str([a.to_dict() for a in saved.actions]))
check("落盘的动作带着设备 id 与文字",
      (saved.actions[0].did, saved.actions[0].args.get("text-content"))
      == ("d1", "极客版按键自动化执行"),
      str(saved.actions[0].to_dict()))
check("保存触发热键同步", service2.syncs >= 1)
check("新建规则归到当前家庭（存的是家庭 id）",
      saved.home == service2.current_home == "1001", repr(saved.home))
check("编辑窗滚动条也常驻占位（③动作一多不会把内容挤窄）",
      dlg.area.verticalScrollBarPolicy() == Qt.ScrollBarAlwaysOn
      and dlg.area.horizontalScrollBarPolicy() == Qt.ScrollBarAlwaysOff)
check("落盘的是触发列表", [t.keys for t in saved.triggers] == ["Ctrl+Alt+F7"])

# 冲突检测（系统热键被占用 / 同键被别的规则用）：都长在触发行右侧
conflict_svc = FakeService(_TMP / "conflict-rules.json")
conflict_svc.rules.rules = [Rule(
    id="c1", name="占用者", triggers=trig("Ctrl+Alt+F7"), home="1001",
    actions=[Action("device_action", did="hub-a", device="客厅中枢",
                    action="emit-virtual-event", args={"virtual-param": "A"})])]
cdlg = A.AutomationDialog(conflict_svc, theme.LIGHT)
cdlg._add_trigger()
ctr = cdlg._trigger_rows[0]
ctr.kind.setCurrentIndex(ctr.kind.findData("keyboard"))
ctr.set_keys("Ctrl+Alt+F7")
check("同一键位被别的启用规则占用时行内提示",
      "占用者" in ctr.status.text(), ctr.status.text())
conflict_svc.rules.set_enabled("c1", False)
cdlg._refresh_status()
check("停用的规则不算冲突", "占用者" not in ctr.status.text(), ctr.status.text())
# 删到 0 行：界面允许，但保存不能当"无触发规则"存下去
cdlg._add_action(Action("device_action", did="d1", device="小爱音箱Pro",
                        action="execute-text-directive", args={"text-content": "A"}))
cdlg._remove_trigger(ctr)
check("删到 0 行允许，但保存报「至少要有一个触发方式」",
      cdlg._save(quiet=True) is False
      and "至少要有一个触发方式" in cdlg.error_label.text(),
      cdlg.error_label.text())
cdlg.close()

# 老规则（home 为空）打开时按当前家庭固定：归属只读展示，保存即落盘（开发中间态收口）
old_rule = Rule(id="old1", name="老规则", triggers=trig("F11"),
                actions=[Action("device_action", did="d1", device="小爱音箱Pro",
                                action="wake-up")])
service2.rules.rules.append(old_rule)
ed_old = A.AutomationDialog(service2, theme.LIGHT, "old1")
check("编辑老规则时归属固定为当前家庭 id",
      ed_old._draft.home == service2.current_home == "1001", repr(ed_old._draft.home))
check("归属家庭是只读文本，不是下拉",
      not hasattr(ed_old, "home_combo"), "还有 home_combo")
check("只读文本显示家庭名（display_name），不是 id",
      ed_old.home_value.text() == "1103 家", ed_old.home_value.text())
check("只读文本不是可编辑控件", isinstance(ed_old.home_value, QLabel))
check("重开编辑窗后归属仍是旧家庭（不会随查看家庭漂移）",
      service2.rules.get("old1").home == "", repr(service2.rules.get("old1").home))
ed_old._save(quiet=True)
check("保存后老规则归到当前家庭",
      service2.rules.get("old1") is not None
      and service2.rules.get("old1").home == "1001",
      repr(service2.rules.get("old1").home if service2.rules.get("old1") else None))
ed_old.close()

# 旧规则里的设备已经不在当前家庭：仍回显名字（不能变成一行空白），执行时才由服务层报原因
stale = Rule(id="stale1", name="设备没了的规则", triggers=trig("F10"),
             actions=[Action("device_action", did="gone", device="旧音箱",
                             action="execute-text-directive",
                             args={"text-content": "开灯"})])
service2.rules.rules.append(stale)
ed_stale = A.AutomationDialog(service2, theme.LIGHT, "stale1")
srow = ed_stale._action_rows[0]
check("设备不在候选里也回显原名", srow.device_btn.text() == "旧音箱",
      srow.device_btn.text())
check("设备不见了要说明白（tooltip 指出可能被移除 / 换家庭）",
      "不在当前家庭" in srow.device_btn.toolTip(), srow.device_btn.toolTip())
check("设备不在候选里时功能下拉退回整个白名单（原来选的那项还在）",
      srow._kind_key() == "execute-text-directive"
      and srow.kind.count() == len(AC.FEATURES),
      f"{srow._kind_key()} / {srow.kind.count()}")
check("设备不见了的行照样能保存（不在编辑页拦，执行时才报）",
      ed_stale._save(quiet=True) is True, ed_stale.error_label.text())
ed_stale.close()

ed2 = A.AutomationDialog(service2, theme.LIGHT, saved.id)
check("编辑既有规则回填名称", ed2.name_edit.text() == "测试播报")
check("编辑既有规则回填动作", len(ed2._action_rows) == 1
      and ed2._action_rows[0].value_edit.text() == "极客版按键自动化执行")
check("编辑既有规则不改启停字段", ed2._draft.enabled == saved.enabled)

print("\n--- 录制态 / 关窗 ---", flush=True)
reject_calls: list[int] = []
_orig_reject = QDialog.reject
QDialog.reject = lambda self: reject_calls.append(1)  # type: ignore[assignment]
try:
    dlg._toggle_record(tr0)
    check("进入录制态", dlg._recording_row is tr0)
    reject_calls.clear()
    dlg.reject()
    check("录制态点遮罩只取消录制，不关窗",
          dlg._recording_row is None and not reject_calls)

    dlg._dirty = True
    A.confirm = lambda *a, **k: False  # 「直接关闭」
    reject_calls.clear()
    dlg.reject()
    check("有未保存修改时确认后才关", len(reject_calls) == 1 and dlg._dirty is False)
finally:
    QDialog.reject = _orig_reject  # type: ignore[assignment]

print("\n--- 源码级 / 公共样式 / 功能区入口 ---", flush=True)
auto_src = (_ROOT / "mihome/ui/automation.py").read_text(encoding="utf-8")
main_src = (_ROOT / "mihome/ui/main_window.py").read_text(encoding="utf-8")
log_src = (_ROOT / "mihome/ui/automation_log.py").read_text(encoding="utf-8")
common_src = (_ROOT / "mihome/ui/automation_style.py").read_text(encoding="utf-8")
check("编辑窗不调用 _apply_theme", "self._apply_theme(" not in auto_src)
check("列表窗不调用 _apply_theme", "self._apply_theme(" not in src_list)
check("列表窗嵌套用 exec_with_dim_click", "exec_with_dim_click(dlg)" in src_list)
check("列表窗不重复压暗主窗口",
      "self._cover_dim(" not in src_list and "._cover_dim(" not in src_list)
check("保存按钮接的是 _on_save", "save.clicked.connect(self._on_save)" in auto_src)
check("编辑窗彻底去掉测试发送",
      "def _test(" not in auto_src and '"测试发送"' not in auto_src
      and "log_added.connect" not in auto_src)
check("编辑窗按屏幕限高", "fit_height(900, reserve=2 * SHADOW_PAD)" in auto_src)
check("公共卡片用 panel_bg 而非 p.card",
      "theme.panel_bg(p)" in common_src and "background: {p.card}" not in common_src)
check("公共层统一说明文字为 text_soft", "QLabel#RowSub" in common_src
      and "p.text_soft" in common_src)
check("公共层有键盘焦点外框", "QPushButton:focus" in common_src)
check("四窗都拼了 common_qss", all(
    "common_qss(p)" in (_ROOT / f"mihome/ui/{name}").read_text(encoding="utf-8")
    for name in ("automation.py", "automation_list.py",
                 "automation_trash.py", "automation_log.py")))
check("日志不做复制 / 导出 / 跟随最新",
      all(word not in log_src for word in ("复制选中", "导出 CSV", "跟随最新")))
check("日志新条目增量插入", "insertRow(0)" in log_src
      and "setRowCount" in log_src)
# 设置页「退出登录」的灰胶囊也要收成圆角矩形（用户 11-13 第二轮）：
# 20px 圆角在 40px 高的按钮上就是胶囊，和四窗主按钮的 10px 对不上。
# 同一位置的「扫码登录」（Primary）必须跟着改，否则登录 / 未登录切换时按钮会变形。
settings_src = (_ROOT / "mihome/ui/settings.py").read_text(encoding="utf-8")
_btn_qss = settings_src.split("QPushButton#Primary {{")[1].split('"""')[0]
check("设置页两个大按钮都改成圆角矩形（10px，不再有 20px 胶囊）",
      _btn_qss.count("border-radius: {theme.RADIUS_BUTTON_SOFT}px") >= 2
      and "border-radius: {theme.RADIUS_BUTTON}px" not in _btn_qss,
      f"soft×{_btn_qss.count('RADIUS_BUTTON_SOFT')}")
# 真点一下按钮：clicked 会带 bool，槽函数必须能吃掉多余参数并走到 accept()
click_dlg = A.AutomationDialog(service2, theme.LIGHT)
click_dlg.name_edit.setText("点按钮保存")
click_dlg._add_trigger()
c_tr = click_dlg._trigger_rows[0]
c_tr.kind.setCurrentIndex(c_tr.kind.findData("keyboard"))
c_tr._mod_boxes["Ctrl"].setChecked(True)
c_tr.key_combo.setCurrentIndex(c_tr.key_combo.findData("F9"))
click_dlg._add_action(Action("device_action", did="hub-a", device="客厅中枢",
                             action="emit-virtual-event",
                             args={"virtual-param": "点按钮事件"}))
_orig_accept = QDialog.accept
QDialog.accept = lambda self: accepted.append(1)  # type: ignore[assignment]
try:
    accepted.clear()
    button_text(click_dlg, "保存").click()
    check("点击保存按钮真的关闭窗口", len(accepted) == 1)
finally:
    QDialog.accept = _orig_accept  # type: ignore[assignment]
check("彻底删除才有确认框", "confirm(self, \"彻底删除\"" in src_trash)

nav = SideNav(theme.LIGHT)
nav.resize(176, 700)
got: list[str] = []
nav.zone_requested.connect(got.append)
check("三个入口 key 稳定", ZONE_KEYS == ("consumables", "monitor", "automation"))
for i, key in enumerate(ZONE_KEYS):
    QTest.mouseClick(nav, Qt.LeftButton, Qt.NoModifier,
                     QPoint(nav._zone_rect(i).center()))
check("三个按钮各发一次信号", got == list(ZONE_KEYS), str(got))
check("主窗口只接 automation 页",
      "if key == \"automation\"" in main_src
      and "self.nav.zone_requested.connect(self._on_zone)" in main_src)
check("主窗口弹的是列表窗",
      "from .automation_list import AutomationListDialog" in main_src
      and "self._run_dialog(dlg)" in main_src)

# ---------------------------------------------------------------------------
# 13) 按钮：圆角矩形 / 焦点不吃尺寸 / 点完不留状态 / 压卡片也不糊
# ---------------------------------------------------------------------------
print("\n13) 按钮：圆角矩形 + 焦点不吃尺寸 + 点完不留状态")


def rule_body(qss: str, selector: str) -> str:
    """取某条 QSS 规则的规则体（第一个 { 到配对的 } 之间）。"""
    i = qss.find(selector)
    if i < 0:
        return ""
    j = qss.find("{", i)
    k = qss.find("}", j)
    return qss[j + 1:k]


def content_rect(btn: QPushButton, focused: bool) -> object:
    """按钮在"有焦点 / 没焦点"时留给文字的内容区（QSS 的 border+padding 都算在内）。"""
    opt = QStyleOptionButton()
    opt.initFrom(btn)
    if focused:
        opt.state |= QStyle.State_HasFocus
    return btn.style().subElementRect(QStyle.SE_PushButtonContents, opt, btn)


def sheet_host(qss: str) -> QWidget:
    host = QWidget()
    host.setStyleSheet(qss)
    host.resize(420, 260)
    host.show()
    app.processEvents()
    return host


def luminance(hex_color: str) -> float:
    c = QColor(hex_color)
    chans = [c.red() / 255, c.green() / 255, c.blue() / 255]

    def lin(v: float) -> float:
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(v) for v in chans)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


page_sheet = A._page_qss(theme.DARK)
confirm_sheet = CF._qss(theme.DARK, False)
check("编辑页保存按钮改圆角矩形",
      "QPushButton#Primary { border-radius: 10px" in page_sheet)
check("列表窗主按钮改圆角矩形",
      "QPushButton#Primary { border-radius: 10px" in listing.styleSheet())
check("确认框两个按钮都是圆角矩形",
      "border-radius:10px" in confirm_sheet and "border-radius:20px" not in confirm_sheet)
check("圆角矩形不是胶囊（40px 高按钮半径 10 ≠ 20）",
      theme.RADIUS_BUTTON_SOFT == 10 and theme.RADIUS_BUTTON == 20)

common_rendered = AS.common_qss(theme.DARK)
focus_rule = rule_body(common_rendered, "QPushButton:focus")
check("焦点只换颜色，不改边框宽度",
      "border-color" in focus_rule and "border:" not in focus_rule, focus_rule.strip())
for name in ("RuleEdit", "RuleDelete", "Disclosure"):
    body = rule_body(listing.styleSheet() if name != "Disclosure" else page_sheet,
                     f"QPushButton#{name} {{")
    check(f"#{name} 常态就留了透明等宽描边", "border: 2px solid transparent" in body, body.strip())

# 量出来：焦点前后内容区必须一样宽。旧写法（:focus 才给 2px 边框）会窄 4px，
# 「编辑」26px 的文字挤在 22px 里，两侧被裁掉——就是用户截图里那个样子。
measured = 0
for name, text in (("Tool", "＋ 添加条件"), ("IconTool", "×"), ("Keycap", "Ctrl+Alt+F7")):
    btn = QPushButton(text)
    btn.setObjectName(name)
    btn.setStyleSheet(page_sheet)
    btn.resize(btn.sizeHint())
    app.processEvents()
    plain, focused = content_rect(btn, False), content_rect(btn, True)
    measured += 1
    check(f"#{name} 焦点不吃内容宽度",
          plain.width() == focused.width(),
          f"{plain.width()} -> {focused.width()}")
check("量了三类行内按钮", measured == 3)

host = sheet_host(
    "QPushButton#Demo { background:#333; color:#fff; border:none;"
    " padding:4px 8px; font-size:13px; }"
    "QPushButton#Demo:focus { border: 2px solid #5B93FF; }")
demo = QPushButton("编辑", host)
demo.setObjectName("Demo")
demo.resize(demo.sizeHint())
app.processEvents()
plain, focused = content_rect(demo, False), content_rect(demo, True)
check("反向证明：旧写法焦点框吃掉 4px 内容宽",
      focused.width() == plain.width() - 4, f"{plain.width()} -> {focused.width()}")

# 点一下按钮：鼠标不该把焦点拿过去（那个焦点框就是"按下去没弹回来"）
host2 = sheet_host(listing.styleSheet())
tabbed = QPushButton("编辑", host2)
tabbed.setObjectName("RuleEdit")
tabbed.setFocusPolicy(Qt.TabFocus)
tabbed.resize(tabbed.sizeHint())
tabbed.move(20, 20)
tabbed.show()
app.processEvents()
QTest.mouseClick(tabbed, Qt.LeftButton, Qt.NoModifier, tabbed.rect().center())
app.processEvents()
check("鼠标点完按钮不抢焦点", not tabbed.hasFocus())
plain_btn = QPushButton("编辑", host2)
plain_btn.setObjectName("RuleEdit")
plain_btn.resize(plain_btn.sizeHint())
plain_btn.move(20, 70)
plain_btn.show()
app.processEvents()
QTest.mouseClick(plain_btn, Qt.LeftButton, Qt.NoModifier, plain_btn.rect().center())
app.processEvents()
check("反向证明：默认策略下点一下就拿走焦点", plain_btn.hasFocus())

for label, dlg in (("列表窗", listing), ("日志窗", logdlg), ("编辑窗", click_dlg)):
    btns = dlg.findChildren(QPushButton)
    loose = [b.objectName() or b.text() for b in btns if b.focusPolicy() != Qt.TabFocus]
    check(f"{label} {len(btns)} 个按钮都只在 Tab 时拿焦点", not loose, str(loose[:3]))
# 回收站容器本身没有常驻按钮，还原 / 彻底删除长在卡片上，直接造一张卡来验
tcard = AT.TrashCard(Rule(id="focustest", name="回收站里的规则",
                          triggers=trig("Ctrl+Alt+F9")), lambda *_: None, lambda *_: None)
tbtns = tcard.findChildren(QPushButton)
check("回收站卡片 2 个按钮都只在 Tab 时拿焦点",
      len(tbtns) == 2 and all(b.focusPolicy() == Qt.TabFocus for b in tbtns),
      str([b.objectName() for b in tbtns]))

check("按下状态有专门样式（不再借用焦点框）",
      ":pressed" in common_rendered
      and all(":pressed" in s for s in (page_sheet, listing.styleSheet(),
                                        trash.styleSheet(), logdlg.styleSheet(),
                                        confirm_sheet)))

# 「还原」压在回收站卡片上：底色必须相对 panel_bg 有区分度
restore_fill = AS.chip_fill(theme.DARK, 0.14)
card_bg = theme.panel_bg(theme.DARK)
check("深色下「还原」在卡片上看得出来",
      contrast(restore_fill, card_bg) >= 1.3, f"{contrast(restore_fill, card_bg):.2f}:1")
old_fill = theme.blend(theme.DARK.bg, theme.DARK.text, 0.12)
check("反向证明：从窗口底起算会糊成一片",
      contrast(old_fill, card_bg) < 1.05, f"{contrast(old_fill, card_bg):.2f}:1")
check("浅色下「还原」靠描边撑边界",
      contrast(AS.chip_border(theme.LIGHT), theme.panel_bg(theme.LIGHT)) >= 1.3)

# 输入框那套底/描边（theme.field_bg / field_border）在深色下是唯一看得见的轮廓，
# 用户 11-13 第三轮报"暗色下输入框的描边没了"。键帽也算一种输入框（点它进入录制）。
page_sheet_dark = A._page_qss(theme.DARK)
check("编辑页输入框用 field_bg + field_border",
      theme.field_bg(theme.DARK) in page_sheet_dark
      and theme.field_border(theme.DARK) in page_sheet_dark)
check("键帽（手动选键）也用同一套，不是 panel_bg + p.border",
      f"QPushButton#Keycap {{\n        background: {theme.field_bg(theme.DARK)}"
      in page_sheet_dark
      and f"border: 1px solid {theme.field_border(theme.DARK)}" in page_sheet_dark)
log_sheet_dark = AG._log_qss(theme.DARK)
check("日志窗搜索框也走同一套",
      theme.field_bg(theme.DARK) in log_sheet_dark
      and theme.field_border(theme.DARK) in log_sheet_dark)

# 日志空态：筛选条贴顶、说明居中、状态行贴底（以前富余高度被两份 sizePolicy 均摊）
# 必须用**一条日志都没有**的窗口量：有日志时表格是显示的，说明标签根本不参与布局。
empty_svc = FakeService(_TMP / "empty-log.json")
empty_dlg = AG.AutomationLogDialog(empty_svc, theme.DARK, None)
empty_dlg.resize(900, 620)
empty_dlg.show()
app.processEvents()
log_body = empty_dlg.empty.parentWidget()
filters = empty_dlg.search.parentWidget()
status_row = empty_dlg.status.parentWidget()
check("空态：说明标签确实在显示", empty_dlg.empty.isVisible())
# 判据不能只看 top()：老写法下 top 也是 12，**变高的是行本身**（筛选条被撑到 200px 高，
# 里面的输入框就跟着沉到中间，底部状态行同理往上浮）。所以比高度是否被撑开。
check("空态筛选条没被撑高", filters.height() == filters.sizeHint().height(),
      f"{filters.height()} vs hint {filters.sizeHint().height()}")
check("空态状态行没被撑高", status_row.height() == status_row.sizeHint().height(),
      f"{status_row.height()} vs hint {status_row.sizeHint().height()}")
check("富余高度全给了空态说明",
      empty_dlg.empty.height() > empty_dlg.empty.sizeHint().height() * 3,
      f"{empty_dlg.empty.height()} vs hint {empty_dlg.empty.sizeHint().height()}")
check("空态说明垂直居中",
      abs(empty_dlg.empty.geometry().center().y() - log_body.rect().center().y()) <= 40,
      f"{empty_dlg.empty.geometry().center().y()} vs {log_body.rect().center().y()}")
check("空态底部说明贴着窗口底",
      log_body.rect().bottom() - status_row.geometry().bottom() <= 20,
      str(log_body.rect().bottom() - status_row.geometry().bottom()))
empty_dlg.close()

# 14) 次要按钮：底色可见 + 与主按钮同尺寸
print("\n14) 次要按钮（取消 / 直接关闭）：底色可见、和主按钮一样大")
cdlg = CF.ConfirmDialog("有未保存的修改", "要保存后关闭吗？", theme.DARK,
                        ok_text="保存并关闭", cancel_text="直接关闭")
cdlg.show()
app.processEvents()
cbtns = {b.objectName(): b for b in cdlg.findChildren(QPushButton)
         if b.objectName() in ("Ghost", "Primary")}
check("确认框两个按钮都在", set(cbtns) == {"Ghost", "Primary"}, str(sorted(cbtns)))
ghost_btn, ok_btn = cbtns.get("Ghost"), cbtns.get("Primary")
check("确认框两按钮同高（配角不再矮一截）",
      ghost_btn is not None and ok_btn is not None
      and ghost_btn.height() == ok_btn.height(),
      f"{ghost_btn.height() if ghost_btn else '-'} vs {ok_btn.height() if ok_btn else '-'}")
check("确认框两按钮 sizeHint 也同高",
      ghost_btn is not None and ok_btn is not None
      and ghost_btn.sizeHint().height() == ok_btn.sizeHint().height(),
      f"{ghost_btn.sizeHint().height() if ghost_btn else '-'} "
      f"vs {ok_btn.sizeHint().height() if ok_btn else '-'}")

# 底色：必须从**对话框底**起算。以前写 bg_soft，深色下 1.03:1 = 没画
ghost_fill = AS.surface_fill(theme.DARK, 0.18)
check("深色下「直接关闭」看得出来",
      contrast(ghost_fill, theme.surface_bg(theme.DARK)) >= 1.5,
      f"{contrast(ghost_fill, theme.surface_bg(theme.DARK)):.2f}:1")
check("反向证明：bg_soft 压在对话框底上等于没画",
      contrast(theme.DARK.bg_soft, theme.surface_bg(theme.DARK)) < 1.05,
      f"{contrast(theme.DARK.bg_soft, theme.surface_bg(theme.DARK)):.2f}:1")
light_fill = AS.surface_fill(theme.LIGHT, 0.14)
check("浅色下「直接关闭」也撑得住",
      contrast(light_fill, theme.surface_bg(theme.LIGHT)) >= 1.3,
      f"{contrast(light_fill, theme.surface_bg(theme.LIGHT)):.2f}:1")
check("确认框样式里已经没有 bg_soft 那版 Ghost",
      theme.DARK.bg_soft not in CF._qss(theme.DARK, False)
      and "QPushButton#Ghost" in CF._qss(theme.DARK, False))
cdlg.close()

# 编辑页页脚：保存（实心）与取消（次要）必须一样大
foot = {b.text(): b for b in click_dlg.findChildren(QPushButton)
        if b.text() in ("保存", "取消")}
check("编辑页页脚两个按钮都在", set(foot) == {"保存", "取消"}, str(sorted(foot)))
check("编辑页「取消」用的是次要按钮样式",
      foot.get("取消") is not None and foot["取消"].objectName() == "Ghost",
      foot["取消"].objectName() if "取消" in foot else "-")
check("编辑页「保存」「取消」同高",
      len(foot) == 2 and foot["保存"].height() == foot["取消"].height(),
      f"{foot['保存'].height() if '保存' in foot else '-'} vs "
      f"{foot['取消'].height() if '取消' in foot else '-'}")
check("编辑页「保存」「取消」sizeHint 同高",
      len(foot) == 2 and foot["保存"].sizeHint().height() == foot["取消"].sizeHint().height(),
      f"{foot['保存'].sizeHint().height() if '保存' in foot else '-'} vs "
      f"{foot['取消'].sizeHint().height() if '取消' in foot else '-'}")

# 日志窗「重置」长在对话框底上，底色要按 surface 算（以前按 p.bg，浅色只剩描边）
check("日志窗「重置」底色按窗口底算",
      AS.surface_fill(theme.LIGHT, 0.14) in logdlg.styleSheet(),
      "没找到 surface_fill 算出来的颜色")
check("浅色下「重置」看得出来",
      contrast(AS.surface_fill(theme.LIGHT, 0.14), theme.surface_bg(theme.LIGHT)) >= 1.3,
      f"{contrast(AS.surface_fill(theme.LIGHT, 0.14), theme.surface_bg(theme.LIGHT)):.2f}:1")
check("反向证明：按 p.bg 起算浅色下只剩一圈描边",
      contrast(theme.blend(theme.LIGHT.bg, theme.LIGHT.text, 0.12),
               theme.surface_bg(theme.LIGHT)) < 1.2,
      f"{contrast(theme.blend(theme.LIGHT.bg, theme.LIGHT.text, 0.12), theme.surface_bg(theme.LIGHT)):.2f}:1")

# 15) ③ 的两枚入口（用户 11-14）：动作 = 对某台设备做一件事，或者等一会。
#     「＋ 选择设备」先弹窗、选中才加行；「＋ 延迟」直接加一行延迟。
print("\n15) ③ 两枚入口「＋ 选择设备 / ＋ 延迟」+ 延迟刻度与精度")
dsvc = FakeService(_TMP / "delay-units.json")
ddlg = A.AutomationDialog(dsvc, theme.LIGHT)
ddlg.resize(860, 900)
ddlg.show()
app.processEvents()
texts = {b.text() for b in ddlg.findChildren(QPushButton)}
check("③ 只有两枚入口（选择设备 / 延迟）",
      "＋ 选择设备" in texts and "＋ 延迟" in texts
      and not any(t in texts for t in ("＋ 添加动作", "＋ 发虚拟事件", "＋ 发音箱指令")),
      str(sorted(texts)))
check("延迟不再按毫秒 / 秒拆成两枚按钮",
      not any("延迟（" in t for t in texts), str(sorted(texts)))
check("②③ 的添加按钮都是 #Tool，长得一样",
      ddlg.add_action_btn.objectName() == "Tool"
      and button_text(ddlg, "＋ 添加条件").objectName() == "Tool")

QTest.mouseClick(ddlg.add_delay_btn, Qt.LeftButton)
app.processEvents()
row = ddlg._action_rows[0]
check("点「＋ 延迟」直接加出一条延迟（不是空档行）",
      len(ddlg._action_rows) == 1 and row.value().type == "delay",
      f"{len(ddlg._action_rows)} / {row.value().type}")
check("毫秒是默认单位，默认值 1000",
      row._unit() == "ms" and row.unit.currentText() == "毫秒"
      and row.value_edit.text() == "1000",
      f"{row.unit.currentText()} / {row.value_edit.text()}")
check("延迟行只露单位那一格（设备 / 功能 / 静默都收起）",
      not row.unit.isHidden() and row.device_btn.isHidden()
      and row.kind.isHidden() and row.silent.isHidden(),
      f"{row.device_btn.isHidden()}/{row.kind.isHidden()}/{row.silent.isHidden()}")
check("刚加的延迟没有红字（默认值就是合法的）",
      row.hint.text() == "" and row.hint.isHidden(), row.hint.text())
check("加完焦点直接在数值框上（不用再点一次）",
      ddlg.focusWidget() is row.value_edit, type(ddlg.focusWidget()).__name__)

# 单位下拉就是原来那两把尺子：同一份 ms，只换刻度
row.unit.setCurrentIndex(row.unit.findData("s"))
check("切到秒只换算不重置（1000 ms → 1 秒）",
      row.value_edit.text() == "1" and row.value().ms == 1000, row.value_edit.text())
check("秒档 placeholder 说的是秒",
      row.value_edit.placeholderText().startswith("秒"), row.value_edit.placeholderText())
row.value_edit.setText("30")
check("秒档 30 → 30000 ms", row.value().ms == 30000, str(row.value().ms))
row.unit.setCurrentIndex(row.unit.findData("ms"))
check("秒切回毫秒还是同一个量",
      row.value_edit.text() == "30000" and row.value().unit == "ms",
      f"{row.value_edit.text()} / {row.value().unit}")
row.unit.setCurrentIndex(row.unit.findData("s"))
check("再切回秒还是 30（没被重置成 1）", row.value_edit.text() == "30",
      row.value_edit.text())

row.value_edit.setText("601")
check("秒档超上限被拦", row.value().ms > 600_000
      and bool(validate_rule(Rule(id="u", name="n", triggers=trig("Ctrl+Alt+F7"),
                                  actions=[evt_action("A"), row.value()]))),
      str(row.value().ms))
# 越界以前只有点保存后由 validate_rule 在底部说一句，现在是**就地**红字
check("秒档超上限行内就说「不能超过 600 秒」",
      "600 秒" in row.hint.text() and row.hint.isVisible(), row.hint.text())
check("越界提示写明了多少秒",
      any("600 秒" in e for e in validate_rule(Rule(
          id="u", name="n", triggers=trig("Ctrl+Alt+F7"),
          actions=[evt_action("A"), row.value()]))))

# 精度（用户 11-13 专门问的）：执行精度是 1 ms → 毫秒档不收小数、秒档收到 3 位，
# 超了要**明说**，不能静默四舍五入成另一个数。
row.value_edit.setText("0.5")
check("秒档 1 位小数（0.5 秒 = 500 ms）", row.value().ms == 500, str(row.value().ms))
row.value_edit.setText("1.234")
check("秒档 3 位小数照收（1.234 秒 = 1234 ms）",
      row.value().ms == 1234 and row.format_error() == "",
      f"{row.value().ms} / {row.format_error()!r}")
row.value_edit.setText("1.2345")
check("秒档第 4 位小数被拦", "3 位小数" in row.format_error(), row.format_error())
# 行内红字（用户 11-13 第六条反馈）：照①「注册状态：…」的样子长在本行右侧，
# 边打边出，不再等点保存、也不在底部错误条重复。
check("超位数立刻在行内出红字",
      row.hint.isVisible() and "3 位小数" in row.hint.text(), row.hint.text())
check("行内红字挂在输入框右边（同一行，不是下面一行）",
      row.hint.geometry().left() >= row.value_edit.geometry().right(),
      f"edit.right={row.value_edit.geometry().right()} hint.left={row.hint.geometry().left()}")
check("行内红字带 tooltip 说明为什么", bool(row.hint.toolTip()), row.hint.toolTip())
check("超位数不是静默舍入（拦下来才算数）", ddlg._save(quiet=True) is False)
check("保存被拦时底部错误条不再重复一遍", ddlg.error_label.text() == "",
      ddlg.error_label.text())
# 点真按钮走一遍：不弹 notice、窗口不关（坏值不许保存，但也不能把编辑内容关掉）
_poped: list[tuple] = []
_orig_notice = A.notice
A.notice = lambda *a, **k: _poped.append(a)
try:
    ddlg._on_save()
    app.processEvents()
finally:
    A.notice = _orig_notice
check("坏值点「保存」不弹 notice", not _poped, str(_poped))
check("坏值点「保存」窗口也不关（内容还在）", ddlg.isVisible())
check("保存被拦时焦点回到出错那行的输入框",
      ddlg.focusWidget() is row.value_edit, type(ddlg.focusWidget()).__name__)
row.value_edit.setText("1.2")
check("改对之后红字立刻消失",
      not row.hint.isVisible() and row.hint.text() == "", row.hint.text())
row.value_edit.setText("abc")
check("秒档乱填不会静默变 0", row.value().ms < 0, str(row.value().ms))
check("秒档乱填报「要填数字」", "数字" in row.format_error(), row.format_error())
row.value_edit.setText("1.5")
check("秒档 1.5 秒 = 1500 ms", row.value().ms == 1500, str(row.value().ms))

row.unit.setCurrentIndex(row.unit.findData("ms"))
row.value_edit.setText("1.5")
check("毫秒档不收小数点", "整数" in row.format_error(), row.format_error())
check("毫秒档小数点不静默取整", row.value().ms < 0 and ddlg._save(quiet=True) is False)
check("毫秒档行内红字说「只能填整数」而不是范围",
      "整数" in row.hint.text() and "600000" not in row.hint.text(), row.hint.text())
row.value_edit.setText("-5")
check("毫秒档负号不当格式错", row.format_error() == "", row.format_error())
check("负数行内报「不能小于 0」",
      "小于 0" in row.hint.text() and ddlg._save(quiet=True) is False, row.hint.text())
row.value_edit.setText("")
check("空值行内报「要填延迟时间」", "延迟时间" in row.hint.text(), row.hint.text())
check("空值时保存同样被拦", ddlg._save(quiet=True) is False)
row.value_edit.setText("2000")
check("毫秒档正常值无格式问题", row.format_error() == "" and row.value().ms == 2000)
check("正常值行内没有红字", not row.hint.isVisible(), row.hint.text())

# 四格宽度是写死的（设备 132 / 功能 118 / 静默 92 / 单位 80），字体一变（系统缩放、
# 换主题）文字就可能被省略号吃掉。留 40px 给左右内边距 + 下拉箭头。
for _name, _combo in (("功能", row.kind), ("静默", row.silent), ("单位", row.unit)):
    _fm = _combo.fontMetrics()
    _need = max(_fm.horizontalAdvance(_combo.itemText(i)) for i in range(_combo.count()))
    check(f"{_name}下拉放得下最长那一条（不被省略号吃掉）",
          _need + 40 <= _combo.width(), f"需要 {_need}+40，实际 {_combo.width()}")

# 3 位小数不是随便挑的：执行精度 1 ms，所以这个精度**往返无损**，再少就会出现
# "存 1234 ms → 重开显示 1.2 秒 → 变成 1200 ms"这种自己改自己数据的事。
lossless = A.ActionRow(Action("delay", ms=1234, unit="s"), theme.LIGHT, lambda: None,
                       lambda *_: None, lambda *_: None)
check("秒档 3 位小数往返无损（1234 ms ⇄ 1.234 秒）",
      lossless.value_edit.text() == "1.234" and lossless.value().ms == 1234,
      f"{lossless.value_edit.text()} → {lossless.value().ms}")
check("这个值本身不算格式错（能保存）",
      lossless.format_error() == "", lossless.format_error())
lossless.deleteLater()

# 落盘再打开：秒档仍然是秒档（unit 只影响显示，不该在重开时丢）
saved_rule = Rule(id="unit1", name="秒档落盘", triggers=trig("Ctrl+Alt+F7"), actions=[
    evt_action("极客版按键自动化执行"),
    Action("delay", ms=30000, unit="s"),
    Action("delay", ms=2000),
])
check("秒档落盘带 unit:s", '"unit": "s"' in json.dumps(saved_rule.to_dict()))
dsvc.rules.add(saved_rule)
reopened = A.AutomationDialog(dsvc, theme.LIGHT, "unit1")
reopened.resize(860, 900)
reopened.show()
app.processEvents()
check("重开后第一条是设备动作",
      reopened._action_rows[0].value().type == "device_action"
      and reopened._action_rows[0].device_btn.text() == "客厅中枢",
      reopened._action_rows[0].device_btn.text())
check("重开后秒档行还是延迟",
      reopened._action_rows[1].value().type == "delay"
      and reopened._action_rows[1].unit.currentText() == "秒",
      reopened._action_rows[1].unit.currentText())
check("重开后秒档显示 30 而不是 30000",
      reopened._action_rows[1].value_edit.text() == "30",
      reopened._action_rows[1].value_edit.text())
check("同一份规则里毫秒档行没被带偏",
      reopened._action_rows[2].unit.currentText() == "毫秒"
      and reopened._action_rows[2].value_edit.text() == "2000",
      f"{reopened._action_rows[2].unit.currentText()} / "
      f"{reopened._action_rows[2].value_edit.text()}")
check("重开后读回的值仍是 30000 ms",
      reopened._action_rows[1].value().ms == 30000)
reopened.close()
ddlg.close()

# 16) ④ 否则分支（用户 11-14）：结构复用 ③，跟着 ② 走，删条件不清空
print("\n16) ④ 否则分支：跟着 ② 走、删条件不清空 ④、保存必须拦")
esvc = FakeService(_TMP / "else-rules.json")
edlg = A.AutomationDialog(esvc, theme.LIGHT)
edlg.resize(860, 900)
edlg.show()
app.processEvents()
check("② 没条件时 ④ 的两枚按钮都是灰的",
      not edlg.add_else_btn.isEnabled() and not edlg.add_else_delay_btn.isEnabled())
check("灰着时 tooltip 说明原因",
      "永远不会触发" in edlg.add_else_btn.toolTip(), edlg.add_else_btn.toolTip())
check("④ 空态先说「先加条件」", "先加一条状态条件" in edlg.else_hint.text(),
      edlg.else_hint.text())

edlg._add_condition(Condition(type="idle_minutes", op="ge", arg="10"))
check("加一条条件后两枚按钮都恢复可用",
      edlg.add_else_btn.isEnabled() and edlg.add_else_delay_btn.isEnabled())
check("空态改成「可以留空」", "可以留空" in edlg.else_hint.text(),
      edlg.else_hint.text())

QTest.mouseClick(edlg.add_else_delay_btn, Qt.LeftButton)
app.processEvents()
check("④ 加出来一行延迟",
      len(edlg._else_rows) == 1 and edlg._else_rows[0].value().type == "delay",
      str([r.value().type for r in edlg._else_rows]))
check("有行之后空态提示让位", edlg.else_hint.isHidden())
erow = edlg._else_rows[0]
check("④ 的延迟行和 ③ 是同一款行（同样的三枚图标按钮）",
      erow.delete_btn.objectName() == "IconTool"
      and A.ActionRow is type(erow))
erow.value_edit.setText("")
check("④ 的行内红字同样生效（点保存后报「要填延迟时间」）",
      edlg._save(quiet=True) is False and erow.error_text() == "要填延迟时间",
      erow.error_text())
erow.value_edit.setText("500")
check("填完值后没有行内红字", erow.error_text() == "", erow.error_text())
edlg.close()

# 落盘一条「有条件 + 有 ④」的规则，重开后检查两段编号互不干扰
else_rule = Rule(
    id="else1", name="否则落盘", triggers=trig("Ctrl+Alt+F7"),
    conditions=[Condition("idle_minutes", "ge", "10")],
    actions=[evt_action("主一"), evt_action("主二")],
    else_actions=[speak_action("还没到点"), Action("delay", ms=500)],
)
check("有条件的规则带 ④ 能通过校验", validate_rule(else_rule) == [],
      str(validate_rule(else_rule)))
check("④ 落盘成一个 else_actions 键",
      len(else_rule.to_dict().get("else_actions") or []) == 2)
check("没有 ④ 的规则不写这个键",
      "else_actions" not in Rule(id="n1", name="x").to_dict())

esvc.rules.add(else_rule)
edlg2 = A.AutomationDialog(esvc, theme.LIGHT, "else1")
edlg2.resize(860, 900)
edlg2.show()
app.processEvents()
check("重开后 ④ 的动作一条不少", len(edlg2._else_rows) == 2)
check("重开后 ④ 的动作顺序也对",
      [r.value().action or r.value().type for r in edlg2._else_rows]
      == ["execute-text-directive", "delay"],
      str([r.value().action or r.value().type for r in edlg2._else_rows]))
check("③ 与 ④ 的序号各自从 1 开始",
      [r.index_label.text() for r in edlg2._action_rows] == ["1.", "2."]
      and [r.index_label.text() for r in edlg2._else_rows] == ["1.", "2."],
      f"{[r.index_label.text() for r in edlg2._action_rows]} / "
      f"{[r.index_label.text() for r in edlg2._else_rows]}")
edlg2._move_else_action(edlg2._else_rows[1], -1)
check("④ 里上移只在自己那段内换位",
      [r.value().action or r.value().type for r in edlg2._else_rows]
      == ["delay", "execute-text-directive"],
      str([r.value().action or r.value().type for r in edlg2._else_rows]))
check("④ 上移不影响 ③ 的顺序",
      [r.value().args.get("virtual-param") for r in edlg2._action_rows] == ["主一", "主二"],
      str([r.value().args.get("virtual-param") for r in edlg2._action_rows]))

# 删光 ② 的条件：④ 的动作**留着**，但按钮变灰、保存被拦
edlg2._remove_condition(edlg2._cond_rows[0])
check("删光条件后 ④ 的动作不清空", len(edlg2._else_rows) == 2)
check("删光条件后 ④ 的两枚按钮变灰",
      not edlg2.add_else_btn.isEnabled()
      and not edlg2.add_else_delay_btn.isEnabled())
check("删光条件后不能保存", edlg2._save(quiet=True) is False)
check("错误条说清两条出路",
      "要么给它加一条条件，要么清空这一段" in edlg2.error_label.text(),
      edlg2.error_label.text())
while edlg2._else_rows:
    edlg2._remove_else_action(edlg2._else_rows[0])
check("清空 ④ 后同一条规则就能存", edlg2._save(quiet=True) is True,
      edlg2.error_label.text())
check("清空后落盘也不再写 else_actions 键",
      "else_actions" not in (esvc.rules.get("else1") or Rule(id="x")).to_dict())
edlg2.close()
print(f"\n合计 {N} 项，失败 {len(FAILS)} 项")
for line in FAILS:
    print("  !! " + line)
raise SystemExit(1 if FAILS else 0)
