# SPDX-License-Identifier: GPL-3.0-or-later
"""电脑自动化**规则列表页**：只回答「有哪些规则、开没开、怎么改」，不显示编辑表单。

2026-11-15 起这一层是主界面中间内容区的**页面**（``MainWindow.pages`` 里的
``AutomationPage``），不再是可以关掉的模态列表窗。点左侧功能区「电脑自动化」
把卡片墙整页换成它；点左栏任一房间回到卡片墙。点卡片里的「编辑」或底部
「＋ 新建规则」才叠一层 :class:`~mihome.ui.automation.AutomationDialog` 三段式
编辑器；底部另外两个按钮分别叠出「回收站」(``ui/automation_trash.py``) 和
「触发日志」(``ui/automation_log.py``)。

两个和回收站有关的约定：

* 卡片「删除」= ``RuleSet.trash()`` **软删除**，可还原，所以**不弹二次确认**；
  彻底删除只在回收站里做，那一步才确认。
* 列表只显示 ``RuleSet.active()``，回收站里的规则不出现在卡片里，也不注册热键。

**页面里的 / 从页面叠出来的弹窗**：页面自己不是模态，压暗与点遮罩关窗统一交给
主窗口的 ``_run_dialog``（构造时以 ``run_dialog`` 注入）；单独构造页面（验证脚本）
时退回 ``exec_with_dim_click``。嵌套弹窗的 ``DimClickShield`` / 挖洞逻辑不变，
页面里**不要**再 ``_cover_dim``。

刷新时机有个坑：``RuleSet.set_enabled()`` 会同步触发 ``rules_changed``，而它是从
``ToggleSwitch.toggled`` 的信号栈里发出来的。若当场销毁重建卡片，等于在发信号的对象
还活着时把它删掉。所以开关只改自己那张卡的描边，整表重建一律用成员 ``QTimer``
排到下一轮事件循环（``docs/dev/automation.md``「电脑自动化：页面 + 三层窗口」）。
"""

from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtWidgets import (
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

from ..core.automation import Rule, trigger_keys, trigger_label
from ..core.automation.service import AutomationService
from . import theme
from .automation_style import (
    CARD_H,
    common_qss,
    reserve_scrollbar,
    tab_focus_only,
)
from .chrome import exec_with_dim_click
from .confirm import notice
from .settings import dialog_qss
from .widgets import ToggleSwitch


def _page_qss(p: theme.Palette) -> str:
    return dialog_qss(p) + f"""
    QLabel#RuleName {{ color: {p.text}; font-size: 16px; font-weight: 700; }}
    QLabel#RuleKeys {{ color: {p.text_soft}; font-size: 12px; }}
    QLabel#RuleBadge {{ color: {theme.DANGER}; font-size: 12px; }}
    QLabel#ListHint {{ color: {p.text_soft}; font-size: 12px; }}
    QLabel#PageCount {{ color: {p.text_soft}; font-size: 12px; }}
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


def hotkey_failures(service: AutomationService, rule: Rule) -> list[tuple[str, str]]:
    """启用了却**没能注册上**的键位触发：``[(键, 原因)]``；空列表 = 都正常。

    **逐个触发看**，不是只看 ``rule.keys``（那只是第一个有键的触发）：一条规则挂了
    F7 / F8 两个键时，可能只有一个失败，另一个照常工作——这恰恰是不能"全有或全无"
    的地方。

    只报"明确的失败"：

    * 停用 / 没有家庭归属 —— 本来就不该注册，不报；
    * ``未注册`` —— 可能是刚启动还没重试，中间态，不报；
    * ``编辑中已暂停`` —— 那是编辑页临时退掉的，不是故障；
    * ``已注册（小键盘被占用）`` —— 部分成功，按下去照样触发，不报。

    剩下的（被别的程序占着、系统保留、不支持、数量上限）才是真的按了没反应。
    没有键的触发（"本程序启动时"那种）不归热键管，直接跳过。
    """
    if not rule.enabled or not rule.home:
        return []
    out: list[tuple[str, str]] = []
    for trig in rule.triggers:
        keys = trigger_keys(trig)
        if not keys:
            continue
        try:
            state = str(service.status_for(keys))
        except Exception:  # noqa: BLE001  验证用的假 service 可能没有这方法
            return []
        if state.startswith("已注册") or state == "未注册" or "暂停" in state:
            continue
        out.append((keys, state))
    return out


def hotkey_note(service: AutomationService, rule: Rule) -> str:
    """失败原因的展示文本；空串 = 正常 / 不用报。卡片 tooltip 用它（可多行）。"""
    return "\n".join(f"{k}：{v}" for k, v in hotkey_failures(service, rule))


def trigger_text(rule: Rule, limit: int = 3) -> str:
    """卡片上那行触发摘要。多触发用「 / 」连起来；超过 ``limit`` 个只留前缀。

    规则可以挂多条触发（OR），卡片行宽有限：全塞进去会和右边的编辑 / 删除挤在
    一起。超过上限时退成「A / B / …（共 N 个）」，完整内容放 tooltip。

    文案由 ``trigger_label`` 给：快捷键是键名，其它类型是类型名（"本程序启动时"）。
    **没有任何触发**（用户 11-17 允许保存这种规则）说「未设置触发」——它不会被
    任何事件唤起，这比"未设置快捷键"更准确。
    """
    labels = [s for t in rule.triggers if (s := trigger_label(t))]
    if not labels:
        return "未设置触发"
    if len(labels) <= limit:
        return " / ".join(labels)
    return " / ".join(labels[:limit]) + f" / …（共 {len(labels)} 个）"


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
        *,
        hotkey_note: str = "",
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
        keys.setToolTip("、".join(s for t in rule.triggers
                                 if (s := trigger_label(t))) or "未设置触发")
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
        if hotkey_note:
            # 开关是"启用中"，但这个键没在系统里注册上 —— 按下去什么都不会发生。
            # 不标出来用户只能猜（原因多半是别的程序先占了，见 hotkey_note 注释）。
            warn = QLabel("热键没注册上", second)
            warn.setObjectName("RuleBadge")
            warn.setToolTip(hotkey_note)
            srow.addWidget(warn)
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


class AutomationPage(QWidget):
    """规则列表页。自身不持有规则状态，全部读写常驻 ``AutomationService``。

    页面常驻在 ``MainWindow.pages`` 里，不随切页销毁：``rules_changed`` 只连一次，
    切家庭时调 :meth:`refresh` 换一份列表即可。
    """

    def __init__(
        self,
        service: AutomationService,
        palette: theme.Palette,
        parent: QWidget | None = None,
        *,
        run_dialog=None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self._p = palette
        #: 页面自己不是模态：编辑 / 回收站 / 日志这些子窗口由主窗口统一压暗
        #: （``MainWindow._run_dialog``）。单独构造页面时退回 ``exec_with_dim_click``。
        self._run_dialog = run_dialog
        self.setObjectName("AutomationPage")
        # 和卡片墙同一套外边距：切页时标题、卡片左缘不跳格（wall 内容边距 28）
        col = QVBoxLayout(self)
        col.setContentsMargins(28, 8, 28, 24)
        col.setSpacing(10)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(10)
        title = QLabel("电脑自动化", self)
        title.setObjectName("PageTitle")
        head.addWidget(title)
        self.count = QLabel("", self)
        self.count.setObjectName("PageCount")
        head.addWidget(self.count)
        head.addStretch(1)
        col.addLayout(head)

        # 页头副标题：只在需要时显示"本家庭 / 未归属规则"的补充说明。
        # 具体文案由 ``_refresh`` 按当前家庭拼，初始先留空，避免闪一下旧说明。
        hint = QLabel("", self)
        hint.setObjectName("ListHint")
        hint.setWordWrap(True)
        hint.setVisible(False)
        col.addWidget(hint)
        self.hint = hint

        self.scroll = QScrollArea(self)
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

        frow = QHBoxLayout()
        frow.setContentsMargins(0, 2, 0, 0)
        frow.setSpacing(10)
        trash = QPushButton("回收站", self)
        trash.setObjectName("Outline")
        trash.setCursor(Qt.PointingHandCursor)
        trash.clicked.connect(self._open_trash)
        self.trash_btn = trash
        log = QPushButton("日志", self)
        log.setObjectName("Outline")
        log.setCursor(Qt.PointingHandCursor)
        log.clicked.connect(self._open_log)
        frow.addWidget(trash)
        frow.addWidget(log)
        frow.addStretch(1)
        add = QPushButton("＋ 新建规则", self)
        add.setObjectName("Primary")
        add.setCursor(Qt.PointingHandCursor)
        add.clicked.connect(lambda: self._open_editor(None))
        frow.addWidget(add)
        col.addLayout(frow)

        self.setStyleSheet(_page_qss(palette))
        # 增删改会同步从控件信号栈里发出来，重建一律排到下一轮
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._refresh)
        self.service.rules_changed.connect(self._schedule_refresh)
        # 注册状态变化也要重画：卡片上那枚「热键没注册上」跟着它走（重试成功就得消失）
        self.service.status_changed.connect(self._schedule_refresh)
        self._refresh()
        # 鼠标点按钮不再抢焦点，焦点框只在 Tab 时出现（见 automation_style.tab_focus_only）
        tab_focus_only(self)

    # -- 外部 ------------------------------------------------------------
    def refresh(self) -> None:
        """立刻按当前家庭重画（切家庭 / 进入页面时调）。"""
        self._refresh()

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.setStyleSheet(_page_qss(palette))
        # 开关是自绘的，颜色不走 QSS；卡片描边由属性选择器跟着新样式表走
        for card in self.findChildren(RuleCard):
            card.switch.set_palette(palette)

    def showEvent(self, event) -> None:  # noqa: N802
        """每次被切到前台都按当前家庭重画一次，避免显示离开时的旧列表。

        重画前先 ``service.sync()`` 一次：开机时被别的程序抢走的键、占用方退出后
        一直哑着的键，都会在这里重试一遍（``sync`` 对"本该注册却没注册"的组合会
        重新 ``RegisterHotKey``）。用户进这个页面本来就是要看/改自动化，是重试最
        合适的时机——顺带让卡片上「热键没注册上」显示的是**这一次**的真实结果。
        """
        super().showEvent(event)
        sync = getattr(self.service, "sync", None)
        if callable(sync):
            sync()
        self._schedule_refresh()

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
        lines: list[str] = []
        if label:
            lines.append(f"只显示「{label}」这个家庭的规则。")
        if unassigned:
            lines.append(f"有 {unassigned} 条旧规则还没有家庭归属，打开并保存后会归到当前家庭。")
        text = "\n".join(lines)
        self.hint.setText(text)
        # 没有补充说明就整块收起，别留一行空白把列表往下顶
        self.hint.setVisible(bool(text))
        self.count.setText(f"{len(rules)} 条规则")
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
                         self._on_toggle, self.list_body,
                         hotkey_note=hotkey_note(self.service, rule))
            )
        self.list_col.addStretch(1)

    # -- 交互 ------------------------------------------------------------
    def _exec(self, dlg) -> None:
        """子窗口统一入口：主窗口注入 ``_run_dialog``（负责压暗 + 点遮罩关窗）。"""
        if self._run_dialog is not None:
            self._run_dialog(dlg)
        else:
            exec_with_dim_click(dlg)

    def _on_toggle(self, rule_id: str, on: bool) -> None:
        self.service.rules.set_enabled(rule_id, on)
        # 只更新这一张卡：整表重建由 rules_changed 排队做，这里避免删掉信号源
        for card in self.findChildren(RuleCard):
            if card.rule_id == rule_id:
                card.set_enabled_state(on)
                break
        if on:
            self._warn_unregistered(rule_id)

    def _warn_unregistered(self, rule_id: str) -> None:
        """刚启用，可有键没注册上——说一句，别让人对着哑掉的规则猜。

        注册失败几乎只有一种原因：**别的程序先占了这个组合**（``RegisterHotKey``
        先到先得，Windows 也不告诉你是谁占的）。卡片第二行有红标，但那是小字，
        用户按了没反应时未必看得见，所以这里再弹一次。

        **一条规则挂多个键时不是全有或全无**：按下能注册的那个照样触发，所以提示
        只点名失败的键，并说清其余的还可用（用户 11-17 问的）。

        **延到下一轮再弹**：``toggled`` 是 ``ToggleSwitch`` 自己的信号栈，在里面
        exec 一个模态框会把开关的收尾动作卡住（同 `docs/dev/automation.md`
        「三层防重」里那条"别在信号栈里重建界面"）。
        """
        rule = self.service.rules.get(rule_id)
        if rule is None:
            return
        fails = hotkey_failures(self.service, rule)
        if not fails:
            return
        keys_total = sum(1 for t in rule.triggers if trigger_keys(t))
        lines = "\n".join(f"「{k}」{v}。" for k, v in fails)
        which = "这些快捷键" if len(fails) > 1 else "这个键"
        tail = ("规则本身是启用的，其余快捷键照常可用。"
                if keys_total > len(fails) else "规则本身是启用的。")
        QTimer.singleShot(0, lambda: notice(
            self, "规则已启用，快捷键没注册上",
            f"{lines}\n\n{tail}没注册上的{which}按下去不会有反应：换一个快捷键，"
            "或者关掉占着它的那个程序后重新进这个页面。",
            self._p))

    def _on_delete(self, rule_id: str) -> None:
        """软删除：直接进回收站，**不弹二次确认**（用户 11-13 定，可还原）。"""
        if self.service.rules.get(rule_id) is None:
            return
        self.service.rules.trash(rule_id)
        self._schedule_refresh()

    def _open_editor(self, rule_id: str | None) -> None:
        """再叠一层三段式编辑窗。父控件是页面，压暗仍只由主窗口管一次。"""
        from .automation import AutomationDialog

        dlg = AutomationDialog(self.service, self._p, rule_id, self)
        self._exec(dlg)
        self._schedule_refresh()

    def _open_trash(self, *_args) -> None:  # noqa: ANN002  clicked 会带一个 bool
        from .automation_trash import AutomationTrashDialog

        dlg = AutomationTrashDialog(self.service, self._p, self)
        self._exec(dlg)
        self._schedule_refresh()

    def _open_log(self, *_args) -> None:  # noqa: ANN002  clicked 会带一个 bool
        from .automation_log import AutomationLogDialog

        dlg = AutomationLogDialog(self.service, self._p, self)
        self._exec(dlg)


__all__ = ["AutomationPage", "RuleCard"]
