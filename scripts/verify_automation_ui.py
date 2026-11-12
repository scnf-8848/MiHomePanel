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

from mihome.core.automation import (  # noqa: E402
    Action, Condition, LogEntry, Rule, RuleSet,
)
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


class FakeService(QObject):
    log_added = Signal(object)
    status_changed = Signal()
    rules_changed = Signal()

    def __init__(self, path, entries=None) -> None:
        super().__init__()
        self.rules = RuleSet(path)
        self.rules.on_changed = self._on_rules_changed
        self.engine = SimpleNamespace(log=list(entries or []))
        self.syncs = 0

    def _on_rules_changed(self) -> None:
        self.sync()
        self.rules_changed.emit()

    def status_for(self, keys: str) -> str:
        return "已注册" if keys else "未设置"

    def target_label(self) -> str:
        return "测试中枢（假）"

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


def sample_rules() -> list[Rule]:
    return [
        Rule(id="r1", name="进门播报", keys="Ctrl+Alt+F7", enabled=True,
             actions=[Action("virtual_event", "极客版按键自动化执行", 0)]),
        Rule(id="r2", name="离开时关灯", keys="F8", enabled=True,
             actions=[Action("virtual_event", "全屋关灯", 0)]),
        Rule(id="r3", name="空规则示例", keys="", enabled=False,
             actions=[Action("virtual_event", "待填", 0)]),
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
check("列表窗按屏幕限高", "fit_height(580)" in src_list)

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
          and opened[-1]._is_new is True and opened[-1]._draft.keys == "")
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
check("回收站按屏幕限高", "fit_height(500)" in src_trash)
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

# ---------------------------------------------------------------------------
print("\n--- 编辑窗：构造 / 三段层级 / 无启停 / 无测试发送 ---", flush=True)
service2 = FakeService(_TMP / "editor-rules.json", sample_logs())
dlg = A.AutomationDialog(service2, theme.LIGHT)
check("新建模式", dlg._is_new is True and dlg._draft.id)
badges = [b.text() for b in dlg.findChildren(QLabel) if b.objectName() == "StepBadge"]
check("三段编号圆徽章", badges == ["1", "2", "3"], str(badges))
check("三段标题齐全",
      all(any(k in t.text() for t in dlg.findChildren(QLabel)
              if t.objectName() == "SectionTitle")
          for k in ("当以下情况发生时", "状态条件", "则执行动作")))
# QLabel 也继承自 QFrame，编号徽章同样带 step；这里只数三段卡片
steps = sorted(str(c.property("step")) for c in dlg.findChildren(QFrame)
               if c.objectName() == "SectionCard" and c.property("step") is not None)
check("三段卡片各有一条语义竖条", steps == ["1", "2", "3"], str(steps))
check("冷静期默认 1000ms 且界面明示", dlg.cooldown.text() == "1000")
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
check("默认一条发虚拟事件动作", len(dlg._action_rows) == 1)
check("默认没有条件", len(dlg._cond_rows) == 0 and not dlg.cond_empty.isHidden())
check("状态条写明 code 0 的边界", "code 0" in dlg.target_label.text())
check("编辑窗不画规则列表", not dlg.findChildren(AL.RuleCard))
check("编辑窗没有新建规则按钮", button_with(dlg, "Primary") is None
      or button_with(dlg, "Primary").text() != "＋ 新建规则")

print("\n--- 编辑窗：动作行右侧控件（用户截图报「根本看不到」） ---", flush=True)
row0 = dlg._action_rows[0]
icons = (row0.up_btn, row0.down_btn, row0.delete_btn)
check("三枚都是独立 IconTool 样式", all(b.objectName() == "IconTool" for b in icons))
check("图标按钮命中区 ≥28px", all(b.width() >= 28 and b.height() >= 28 for b in icons))
check("IconTool 把 padding 归零", "QPushButton#IconTool" in dlg.styleSheet()
      and "padding: 0;" in dlg.styleSheet())
check("图标按钮都有 tooltip 与无障碍名",
      all(b.toolTip() and b.accessibleName() for b in icons))
check("单行时上下移都禁用", not row0.up_btn.isEnabled()
      and not row0.down_btn.isEnabled())
check("禁用态 tooltip 说明原因", "已经是第一条" in row0.up_btn.toolTip())
check("注册状态并进快捷键行", dlg.key_status.parentWidget() is dlg.keycap.parentWidget())
check("手动选键默认收起", dlg.manual_keys_widget.isHidden())
dlg.manual_toggle.setChecked(True)
check("点开才展开手动选键", not dlg.manual_keys_widget.isHidden())
dlg.manual_toggle.setChecked(False)
dlg.name_edit.setText("改名测试")
check("标题副标题随名称同步", dlg.title_bar._sub == "改名测试")

print("\n--- 编辑窗：选键 / 条件 / 动作 ---", flush=True)
dlg._mod_boxes["Ctrl"].setChecked(True)
dlg._mod_boxes["Alt"].setChecked(True)
dlg.key_combo.setCurrentIndex(dlg.key_combo.findData("F7"))
check("手动选键生成规范串", dlg._draft.keys == "Ctrl+Alt+F7", dlg._draft.keys)
check("键位显示在按钮上", dlg.keycap.text() == "Ctrl+Alt+F7")

dlg._add_condition(Condition("idle_minutes", "ge", "10"))
check("添加条件后一行", len(dlg._cond_rows) == 1 and dlg.cond_empty.isHidden())
check("条件行删除按钮也是图标按钮",
      dlg._cond_rows[0].remove_btn.objectName() == "IconTool")
dlg._cond_rows[0].arg.setText("25")
check("条件行能读回参数", dlg._cond_rows[0].value().arg == "25")
dlg._remove_condition(dlg._cond_rows[0])
check("删除条件回到空态", len(dlg._cond_rows) == 0 and not dlg.cond_empty.isHidden())

dlg._action_rows[0].value_edit.setText("极客版按键自动化执行")
dlg._add_action(Action("delay", "", 2000))
check("添加延迟后两行", len(dlg._action_rows) == 2)
check("序号连续", [r.index_label.text() for r in dlg._action_rows] == ["1.", "2."])
check("两行时首行禁上移 / 末行禁下移",
      not dlg._action_rows[0].up_btn.isEnabled()
      and not dlg._action_rows[1].down_btn.isEnabled())
check("两行时中间方向可用", dlg._action_rows[0].down_btn.isEnabled()
      and dlg._action_rows[1].up_btn.isEnabled())
dlg._move_action(dlg._action_rows[1], -1)
check("上移生效", dlg._action_rows[0].value().type == "delay")
check("延迟值读回", dlg._action_rows[0].value().ms == 2000)
check("移动后边界重新计算", not dlg._action_rows[0].up_btn.isEnabled()
      and not dlg._action_rows[1].down_btn.isEnabled())
dlg._move_action(dlg._action_rows[0], 1)
check("下移生效", dlg._action_rows[0].value().type == "virtual_event")
dlg._remove_action(dlg._action_rows[1])
check("删除动作回到一行", len(dlg._action_rows) == 1)
check("回到单行后箭头重新禁用", not dlg._action_rows[0].down_btn.isEnabled())

# 换类型残留旧值会让 int() 失败，用户看到"填了东西却报校验错"
dlg._action_rows[0].kind.setCurrentIndex(
    dlg._action_rows[0].kind.findData("delay"))
check("切到延迟重置成 1000 ms", dlg._action_rows[0].value_edit.text() == "1000")
dlg._action_rows[0].kind.setCurrentIndex(
    dlg._action_rows[0].kind.findData("virtual_event"))
check("切回事件清空事件名", dlg._action_rows[0].value_edit.text() == "")

print("\n--- 编辑窗：校验 / 保存即关闭 ---", flush=True)
dlg.name_edit.setText("测试播报")
dlg._action_rows[0].value_edit.setText("")
check("空事件名保存被拦", dlg._save(quiet=True) is False)
check("错误提示可见", bool(dlg.error_label.text()))
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
check("动作只有一条虚拟事件", len(saved.actions) == 1
      and saved.actions[0].name == "极客版按键自动化执行")
check("保存触发热键同步", service2.syncs >= 1)

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
    dlg._toggle_record()
    check("进入录制态", dlg._recording is True)
    reject_calls.clear()
    dlg.reject()
    check("录制态点遮罩只取消录制，不关窗",
          dlg._recording is False and not reject_calls)

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
check("编辑窗按屏幕限高", "fit_height(900)" in auto_src)
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
# 真点一下按钮：clicked 会带 bool，槽函数必须能吃掉多余参数并走到 accept()
click_dlg = A.AutomationDialog(service2, theme.LIGHT)
click_dlg.name_edit.setText("点按钮保存")
click_dlg._mod_boxes["Ctrl"].setChecked(True)
click_dlg.key_combo.setCurrentIndex(click_dlg.key_combo.findData("F9"))
click_dlg._action_rows[0].value_edit.setText("点按钮事件")
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
                          keys="Ctrl+Alt+F9"), lambda *_: None, lambda *_: None)
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

print(f"\n合计 {N} 项，失败 {len(FAILS)} 项")
for line in FAILS:
    print("  !! " + line)
raise SystemExit(1 if FAILS else 0)
