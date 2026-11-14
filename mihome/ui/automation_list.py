# SPDX-License-Identifier: GPL-3.0-or-later
"""电脑自动化**规则列表窗**：只回答「有哪些规则、开没开、怎么改」，不显示编辑表单。

点左侧功能区「电脑自动化」弹的是这一层；点卡片里的「编辑」或底部「＋ 新建规则」
才再叠一层 :class:`~mihome.ui.automation.AutomationDialog` 三段式编辑器。底部另外两个
按钮分别叠出「回收站」(``ui/automation_trash.py``) 和「触发日志」
(``ui/automation_log.py``)。**没有「完成」按钮**：右上角的 × 或点背景遮罩就能关。

两个和回收站有关的约定：

* 卡片「删除」= ``RuleSet.trash()`` **软删除**，可还原，所以**不弹二次确认**；
  彻底删除只在回收站里做，那一步才确认。
* 列表只显示 ``RuleSet.active()``，回收站里的规则不出现在卡片里，也不注册热键。

**嵌套弹窗不用自己造机制**：``exec_with_dim_click`` 给每层各挂一个
``DimClickShield``，``_stack()`` 会把链路上所有可见弹窗一起挖洞，所以编辑窗开着时
点主窗口压暗区只会关掉编辑窗，列表窗还在；主窗口的压暗由 ``MainWindow._run_dialog``
管一次，这里**不要**再 ``_cover_dim``。

刷新时机有个坑：``RuleSet.set_enabled()`` 会同步触发 ``rules_changed``，而它是从
``ToggleSwitch.toggled`` 的信号栈里发出来的。若当场销毁重建卡片，等于在发信号的对象
还活着时把它删掉。所以开关只改自己那张卡的描边，整表重建一律用成员 ``QTimer``
排到下一轮事件循环（``docs/dev/automation.md``「电脑自动化：四层窗口」）。
"""

from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core.automation import Rule
from ..core.automation.service import AutomationService
from . import theme
from .automation_style import (
    CARD_H, common_qss, fit_height, reserve_scrollbar, tab_focus_only,
)
from .chrome import (
    SHADOW_PAD,
    TitleBar,
    WindowFrame,
    exec_with_dim_click,
    floating_size,
    shadow_alpha,
    surface_bg,
    surface_border,
)
from .settings import dialog_qss
from .widgets import ToggleSwitch


def _list_qss(p: theme.Palette) -> str:
    return dialog_qss(p) + f"""
    QLabel#RuleName {{ color: {p.text}; font-size: 16px; font-weight: 700; }}
    QLabel#RuleKeys {{ color: {p.text_soft}; font-size: 12px; }}
    QLabel#RuleBadge {{ color: {theme.DANGER}; font-size: 12px; }}
    QLabel#ListHint {{ color: {p.text_soft}; font-size: 12px; }}
    QLabel#EmptyHint {{ color: {p.text_soft}; font-size: 13px; line-height: 180%; }}
    QPushButton#RuleEdit {{
        background: transparent; color: {p.accent};
        font-size: 13px; font-weight: 600; padding: 4px 8px;
        /* 常态就留一条透明描边占位：焦点只换颜色，宽度不变，文字才不会被裁 */
        border: 2px solid transparent;
    }}
    QPushButton#RuleEdit:hover {{ color: {theme.blend(p.accent, "#FFFFFF", 0.18)}; }}
    QPushButton#RuleEdit:pressed {{ color: {theme.blend(p.accent, p.bg, 0.32)}; }}
    QPushButton#RuleDelete {{
        background: transparent; color: {theme.DANGER};
        font-size: 13px; font-weight: 600; padding: 4px 8px;
        border: 2px solid transparent;
    }}
    QPushButton#RuleDelete:hover {{ color: {theme.blend(theme.DANGER, "#FFFFFF", 0.18)}; }}
    QPushButton#RuleDelete:pressed {{ color: {theme.blend(theme.DANGER, p.bg, 0.32)}; }}
    /* 「回收站」「日志」：圆角描边按钮，靠左放，和右边实心的「＋新建规则」区分开 */
    QPushButton#Outline {{
        background: transparent; color: {p.text};
        border: 1px solid {theme.blend(p.border, p.text, 0.32)}; border-radius: 10px;
        padding: 9px 18px; font-size: 12px;
    }}
    QPushButton#Outline:hover {{
        color: {p.accent}; border-color: {p.accent};
        background: {theme.blend(p.bg, p.accent, 0.08)};
    }}
    QPushButton#Outline:pressed {{ background: {theme.blend(p.bg, p.accent, 0.18)}; }}
    /* 用户 11-13 要求「改圆角矩形」：绿色主按钮不要直角，也不要胶囊，给一个中等圆角。
       圆角本身写在 ``common_qss`` 里（四窗 + 编辑页保存按钮共用一份）。 */
    QScrollArea {{ background: transparent; border: none; }}
    QWidget#RuleListBody {{ background: transparent; }}
    """ + common_qss(p)


def _home_label(service, home_id: str) -> str:
    """家庭 id → 显示名（只用于文案；规则归属匹配仍用 id）。"""
    label = getattr(service, "home_label", None)
    return str(label(home_id) or "") if callable(label) else home_id


def trigger_text(rule: Rule, limit: int = 3) -> str:
    """卡片上那行触发摘要。多触发用「 / 」连起来；超过 ``limit`` 个只留前缀。

    规则可以挂多条触发（OR），卡片行宽有限：全塞进去会和右边的编辑 / 删除挤在
    一起。超过上限时退成「A / B / …（共 N 个）」，完整键位放 tooltip。
    """
    keys = [t.keys for t in rule.triggers if t.type == "keyboard" and t.keys]
    if not keys:
        return "未设置快捷键"
    if len(keys) <= limit:
        return " / ".join(keys)
    return " / ".join(keys[:limit]) + f" / …（共 {len(keys)} 个）"


class RuleCard(QFrame):
    """一条规则 = 名称 + 触发键 + 编辑 / 删除 + 启停开关。

    描边颜色跟着启用状态走：启用是主题强调蓝、停用是中性边框色，扫一眼就知道
    哪些热键真的注册着。颜色由 QSS 的属性选择器给，切主题不用逐个控件重刷。
    """

    def __init__(
        self,
        rule: Rule,
        palette: theme.Palette,
        on_edit,
        on_delete,
        on_toggle,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.rule_id = rule.id
        self.setObjectName("RuleCard")
        self.setFixedHeight(CARD_H)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setProperty("on", "true" if rule.enabled else "false")

        grid = QGridLayout(self)
        grid.setContentsMargins(20, 14, 18, 14)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(0, 1)

        name = QLabel(rule.name or "未命名规则", self)
        name.setObjectName("RuleName")
        grid.addWidget(name, 0, 0, 1, 2, Qt.AlignLeft | Qt.AlignVCenter)

        keys = QLabel(trigger_text(rule), self)
        keys.setObjectName("RuleKeys")
        keys.setToolTip("、".join(t.keys for t in rule.triggers
                                 if t.type == "keyboard" and t.keys) or "未设置快捷键")
        second = QWidget(self)
        srow = QHBoxLayout(second)
        srow.setContentsMargins(0, 0, 0, 0)
        srow.setSpacing(8)
        srow.addWidget(keys)
        if not rule.home:
            # 旧文件的空归属是开发中间态：卡片上标出来，打开保存一次就归到当前家庭
            # （在那之前它不注册热键、不会运行）
            badge = QLabel("未指定家庭 · 保存后归入当前家庭", second)
            badge.setObjectName("RuleBadge")
            srow.addWidget(badge)
        srow.addStretch(1)
        grid.addWidget(second, 1, 0, 1, 1, Qt.AlignLeft | Qt.AlignVCenter)

        actions = QWidget(self)
        arow = QHBoxLayout(actions)
        arow.setContentsMargins(0, 0, 0, 0)
        arow.setSpacing(2)
        edit = QPushButton("编辑", actions)
        edit.setObjectName("RuleEdit")
        edit.setFocusPolicy(Qt.TabFocus)
        edit.setCursor(Qt.PointingHandCursor)
        edit.clicked.connect(lambda: on_edit(rule.id))
        delete = QPushButton("删除", actions)
        delete.setObjectName("RuleDelete")
        delete.setFocusPolicy(Qt.TabFocus)
        delete.setCursor(Qt.PointingHandCursor)
        delete.clicked.connect(lambda: on_delete(rule.id))
        arow.addWidget(edit)
        arow.addWidget(delete)
        grid.addWidget(actions, 1, 1, 1, 1, Qt.AlignRight | Qt.AlignVCenter)

        self.switch = ToggleSwitch(rule.enabled, palette, self, size="md")
        self.switch.toggled.connect(lambda on: on_toggle(rule.id, on))
        grid.addWidget(self.switch, 0, 2, 2, 1, Qt.AlignRight | Qt.AlignVCenter)

    def set_enabled_state(self, on: bool) -> None:
        """只改这一张卡的描边；不整表重建，也不让开关的回填再发一次 toggled。"""
        self.setProperty("on", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        with QSignalBlocker(self.switch):
            self.switch.set_on(on, animate=False)


class AutomationListDialog(QDialog):
    """规则列表层。自身不持有规则状态，全部读写常驻 ``AutomationService``。"""

    def __init__(
        self,
        service: AutomationService,
        palette: theme.Palette,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self._p = palette
        self.setWindowTitle("电脑自动化")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self._frame = WindowFrame(
            self, resizable=True,
            radius=theme.RADIUS_CARD,
            shadow=SHADOW_PAD,
            shadow_alpha=shadow_alpha(self._p),
            fill=lambda: surface_bg(self._p),
            border=lambda: surface_border(self._p),
        )
        self.resize(*floating_size(640, fit_height(580, reserve=2 * SHADOW_PAD)))
        self.setMinimumSize(*floating_size(520, 420))
        self.setStyleSheet(_list_qss(palette))

        outer = QVBoxLayout(self._frame.body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.title_bar = TitleBar("电脑自动化", palette, buttons=("close",),
                                  variant="dialog", frame=self._frame)
        outer.addWidget(self.title_bar)

        body = QWidget(self._frame.body)  # 内容区，别挂窗口（窗口大一圈）
        col = QVBoxLayout(body)
        col.setContentsMargins(18, 12, 18, 12)
        col.setSpacing(10)

        hint = QLabel("每条规则 = 一个全局热键 → 电脑条件 → 米家虚拟事件。停用后热键立即释放。", body)
        hint.setObjectName("ListHint")
        hint.setWordWrap(True)
        col.addWidget(hint)
        self.hint = hint

        self.scroll = QScrollArea(body)
        self.scroll.setWidgetResizable(True)
        # 滚动条常驻占位：条目多到要滚时卡片不会突然变窄（见 automation_style）
        reserve_scrollbar(self.scroll)
        self.list_body = QWidget(self.scroll)
        self.list_body.setObjectName("RuleListBody")
        self.list_col = QVBoxLayout(self.list_body)
        self.list_col.setContentsMargins(2, 2, 6, 2)
        self.list_col.setSpacing(12)
        self.scroll.setWidget(self.list_body)
        col.addWidget(self.scroll, 1)

        footer = QWidget(body)
        frow = QHBoxLayout(footer)
        frow.setContentsMargins(0, 2, 0, 0)
        frow.setSpacing(10)
        trash = QPushButton("回收站", footer)
        trash.setObjectName("Outline")
        trash.setCursor(Qt.PointingHandCursor)
        trash.clicked.connect(self._open_trash)
        self.trash_btn = trash
        log = QPushButton("日志", footer)
        log.setObjectName("Outline")
        log.setCursor(Qt.PointingHandCursor)
        log.clicked.connect(self._open_log)
        frow.addWidget(trash)
        frow.addWidget(log)
        frow.addStretch(1)
        add = QPushButton("＋ 新建规则", footer)
        add.setObjectName("Primary")
        add.setCursor(Qt.PointingHandCursor)
        add.clicked.connect(lambda: self._open_editor(None))
        frow.addWidget(add)
        col.addWidget(footer)
        outer.addWidget(body, 1)

        # 增删改会同步从控件信号栈里发出来，重建一律排到下一轮
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._refresh)
        self.service.rules_changed.connect(self._schedule_refresh)
        self._refresh()
        # 鼠标点按钮不再抢焦点，焦点框只在 Tab 时出现（见 automation_style.tab_focus_only）
        tab_focus_only(self)

    # -- 列表 ------------------------------------------------------------
    def _schedule_refresh(self) -> None:
        self._refresh_timer.start(0)

    def _refresh(self) -> None:
        while self.list_col.count():
            item = self.list_col.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        # 规则按家庭隔离：这一层只列出**当前家庭**的规则；home 是家庭 id，
        # 文案里换成 display_name（用户 11-13 第三轮：内部匹配一律用 id）
        home = str(getattr(self.service, "current_home", "") or "")
        rules = self.service.rules.active(home)
        trashed = self.service.rules.trashed(home)
        label = _home_label(self.service, home)
        unassigned = sum(1 for r in rules if not r.home)
        text = "每条规则 = 一个全局热键 → 电脑条件 → 米家虚拟事件。停用后热键立即释放。"
        if label:
            text += f"\n只显示「{label}」这个家庭的规则。"
        if unassigned:
            text += f"\n有 {unassigned} 条旧规则还没有家庭归属，打开并保存后会归到当前家庭。"
        self.hint.setText(text)
        self.title_bar.set_subtitle(f"{len(rules)} 条规则")
        self.trash_btn.setToolTip(
            f"回收站：{len(trashed)} 条已删除规则" if trashed else "这是本家庭的回收站，目前是空的")
        if not rules:
            extra = (f"\n列表只显示「{label}」这个家庭的规则" if label else "")
            empty = QLabel("还没有自动化规则\n点下面的「＋ 新建规则」开始配置" + extra,
                           self.list_body)
            empty.setObjectName("EmptyHint")
            empty.setAlignment(Qt.AlignCenter)
            self.list_col.addWidget(empty, 1)
            return
        for rule in rules:
            self.list_col.addWidget(
                RuleCard(rule, self._p, self._open_editor, self._on_delete,
                         self._on_toggle, self.list_body)
            )
        self.list_col.addStretch(1)

    # -- 交互 ------------------------------------------------------------
    def _on_toggle(self, rule_id: str, on: bool) -> None:
        self.service.rules.set_enabled(rule_id, on)
        # 只更新这一张卡：整表重建由 rules_changed 排队做，这里避免删掉信号源
        for card in self.findChildren(RuleCard):
            if card.rule_id == rule_id:
                card.set_enabled_state(on)
                break

    def _on_delete(self, rule_id: str) -> None:
        """软删除：直接进回收站，**不弹二次确认**（用户 11-13 定，可还原）。"""
        if self.service.rules.get(rule_id) is None:
            return
        self.service.rules.trash(rule_id)
        self._schedule_refresh()

    def _open_editor(self, rule_id: str | None) -> None:
        """再叠一层三段式编辑窗。父窗口是列表窗，压暗仍只由主窗口管一次。"""
        from .automation import AutomationDialog

        dlg = AutomationDialog(self.service, self._p, rule_id, self)
        exec_with_dim_click(dlg)
        self._schedule_refresh()

    def _open_trash(self, *_args) -> None:  # noqa: ANN002  clicked 会带一个 bool
        from .automation_trash import AutomationTrashDialog

        dlg = AutomationTrashDialog(self.service, self._p, self)
        exec_with_dim_click(dlg)
        self._schedule_refresh()

    def _open_log(self, *_args) -> None:  # noqa: ANN002  clicked 会带一个 bool
        from .automation_log import AutomationLogDialog

        dlg = AutomationLogDialog(self.service, self._p, self)
        exec_with_dim_click(dlg)

    def closeEvent(self, event) -> None:  # noqa: N802
        # 关窗不做任何整墙重建（11-12 关窗闪烁的根因）
        event.accept()


__all__ = ["AutomationListDialog", "RuleCard"]
