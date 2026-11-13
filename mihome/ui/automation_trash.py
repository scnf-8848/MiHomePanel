# SPDX-License-Identifier: GPL-3.0-or-later
"""电脑自动化**回收站**：还原 / 彻底删除被软删除的规则。

列表窗点「删除」只是 ``RuleSet.trash()`` 打标记（可恢复，**不弹二次确认**），规则
仍留在 ``rules.json`` 里；这一层才提供两条真正的出路：

* 「还原」—— 回到列表，原来的键位 / 条件 / 动作 / 启停状态全都在；
* 「彻底删除」—— ``RuleSet.purge()`` 物理移除，**不可恢复**，所以只有这一步弹确认。

和列表窗同一个坑：``rules_changed`` 是从按钮 ``clicked`` 的信号栈里同步发出的，
当场销毁重建卡片等于删掉正在发信号的对象。整表重建一律排到下一轮事件循环。
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer
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
from .automation_list import _home_label, trigger_text
from .automation_style import (
    CARD_H, chip_border, chip_fill, common_qss, fit_height, reserve_scrollbar,
    tab_focus_only,
)
from .chrome import (
    SHADOW_PAD,
    TitleBar,
    WindowFrame,
    floating_size,
    shadow_alpha,
    surface_bg,
    surface_border,
)
from .confirm import confirm
from .settings import dialog_qss



def _trash_qss(p: theme.Palette) -> str:
    return dialog_qss(p) + f"""
    QLabel#TrashName {{ color: {p.text}; font-size: 15px; font-weight: 700; }}
    QLabel#TrashMeta {{ color: {p.text_soft}; font-size: 11px; }}
    QLabel#TrashHint {{ color: {p.text_soft}; font-size: 12px; }}
    QLabel#TrashEmpty {{ color: {p.text_soft}; font-size: 13px; line-height: 180%; }}
    /* 「还原」压在回收站卡片（panel_bg）上：底色必须从 panel_bg 起算。
       以前用 blend(p.bg, …) 深色下算出 #292A2D、卡片底 #28292C——1.01:1，
       按钮和卡片糊成一片（用户 11-13 报）。 */
    QPushButton#Restore {{
        background: {chip_fill(p, 0.14)}; color: {p.text};
        border: 1px solid {chip_border(p)}; border-radius: 9px;
        padding: 6px 15px; font-size: 12px;
    }}
    QPushButton#Restore:hover {{
        background: {chip_fill(p, 0.22)}; color: {p.accent}; border-color: {p.accent};
    }}
    QPushButton#Restore:pressed {{ background: {chip_fill(p, 0.30)}; }}
    /* 不可恢复的操作要有明确危险外观：红字 + 红描边，权重不低于「还原」 */
    QPushButton#Purge {{
        background: transparent; color: {theme.DANGER};
        border: 1px solid {theme.blend(theme.DANGER, p.bg, 0.42)};
        border-radius: 9px; padding: 6px 15px; font-size: 12px;
    }}
    QPushButton#Purge:hover {{
        background: {theme.blend(p.bg, theme.DANGER, 0.16)}; border-color: {theme.DANGER};
    }}
    QPushButton#Purge:pressed {{ background: {theme.blend(p.bg, theme.DANGER, 0.26)}; }}
    QScrollArea {{ background: transparent; border: none; }}
    QWidget#TrashBody {{ background: transparent; }}
    """ + common_qss(p)


def _deleted_text(rule: Rule) -> str:
    if not rule.deleted_at:
        return "删除时间未知"
    return "删除于 " + time.strftime("%m-%d %H:%M", time.localtime(rule.deleted_at))


class TrashCard(QFrame):
    """回收站里的一行：名称 / 键位 / 删除时间 + 还原 / 彻底删除。"""

    def __init__(self, rule: Rule, on_restore, on_purge,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.rule_id = rule.id
        self.setObjectName("TrashCard")
        self.setFixedHeight(CARD_H)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        grid = QGridLayout(self)
        grid.setContentsMargins(18, 12, 16, 12)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(3)
        grid.setColumnStretch(0, 1)

        name = QLabel(rule.name or "未命名规则", self)
        name.setObjectName("TrashName")
        grid.addWidget(name, 0, 0, 1, 2, Qt.AlignLeft | Qt.AlignVCenter)
        meta = QLabel(f"{trigger_text(rule)}　·　{_deleted_text(rule)}", self)
        meta.setObjectName("TrashMeta")
        grid.addWidget(meta, 1, 0, 1, 1, Qt.AlignLeft | Qt.AlignVCenter)

        actions = QWidget(self)
        arow = QHBoxLayout(actions)
        arow.setContentsMargins(0, 0, 0, 0)
        arow.setSpacing(10)
        restore = QPushButton("还原", actions)
        restore.setObjectName("Restore")
        restore.setFocusPolicy(Qt.TabFocus)
        restore.setCursor(Qt.PointingHandCursor)
        restore.clicked.connect(lambda: on_restore(rule.id))
        purge = QPushButton("彻底删除", actions)
        purge.setObjectName("Purge")
        purge.setFocusPolicy(Qt.TabFocus)
        purge.setCursor(Qt.PointingHandCursor)
        purge.clicked.connect(lambda: on_purge(rule.id))
        arow.addWidget(restore)
        arow.addWidget(purge)
        grid.addWidget(actions, 0, 1, 2, 1, Qt.AlignRight | Qt.AlignVCenter)


class AutomationTrashDialog(QDialog):
    """回收站层。自身不持有规则状态，全部读写常驻 ``AutomationService``。"""

    def __init__(
        self,
        service: AutomationService,
        palette: theme.Palette,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self._p = palette
        self.setWindowTitle("回收站")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self._frame = WindowFrame(
            self, resizable=True,
            radius=theme.RADIUS_CARD,
            shadow=SHADOW_PAD,
            shadow_alpha=shadow_alpha(self._p),
            fill=lambda: surface_bg(self._p),
            border=lambda: surface_border(self._p),
        )
        self.resize(*floating_size(620, fit_height(500, reserve=2 * SHADOW_PAD)))
        self.setMinimumSize(*floating_size(500, 360))
        self.setStyleSheet(_trash_qss(palette))

        outer = QVBoxLayout(self._frame.body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.title_bar = TitleBar("回收站", palette, buttons=("close",),
                                  variant="dialog", frame=self._frame)
        outer.addWidget(self.title_bar)

        body = QWidget(self._frame.body)  # 内容区，别挂窗口（窗口大一圈）
        col = QVBoxLayout(body)
        col.setContentsMargins(18, 12, 18, 12)
        col.setSpacing(10)
        hint = QLabel("这里的规则不会注册热键，也不会被触发。还原后回到列表；彻底删除不可恢复。", body)
        hint.setObjectName("TrashHint")
        hint.setWordWrap(True)
        col.addWidget(hint)

        self.scroll = QScrollArea(body)
        self.scroll.setWidgetResizable(True)
        # 同列表窗：滚动条常驻占位，卡片宽度不随条目多少跳
        reserve_scrollbar(self.scroll)
        self.list_body = QWidget(self.scroll)
        self.list_body.setObjectName("TrashBody")
        self.list_col = QVBoxLayout(self.list_body)
        self.list_col.setContentsMargins(2, 2, 6, 2)
        self.list_col.setSpacing(10)
        self.scroll.setWidget(self.list_body)
        col.addWidget(self.scroll, 1)
        outer.addWidget(body, 1)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._refresh)
        self.service.rules_changed.connect(self._schedule_refresh)
        self._refresh()
        tab_focus_only(self)

    def _schedule_refresh(self) -> None:
        self._refresh_timer.start(0)

    def _refresh(self) -> None:
        while self.list_col.count():
            item = self.list_col.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        # 规则按家庭隔离：回收站也只放**当前家庭**删掉的规则（home 是家庭 id）
        home = str(getattr(self.service, "current_home", "") or "")
        rules = self.service.rules.trashed(home)
        self.title_bar.set_subtitle(f"{len(rules)} 条已删除规则")
        if not rules:
            label = _home_label(self.service, home)
            extra = f"\n只收「{label}」这个家庭删掉的规则" if label else ""
            empty = QLabel("回收站是空的\n列表页点「删除」的规则会先放到这里" + extra,
                           self.list_body)
            empty.setObjectName("TrashEmpty")
            empty.setAlignment(Qt.AlignCenter)
            self.list_col.addWidget(empty, 1)
            return
        for rule in rules:
            self.list_col.addWidget(
                TrashCard(rule, self._on_restore, self._on_purge, self.list_body))
        self.list_col.addStretch(1)

    # -- 交互 ------------------------------------------------------------
    def _on_restore(self, rule_id: str) -> None:
        self.service.rules.restore(rule_id)
        self._schedule_refresh()

    def _on_purge(self, rule_id: str) -> None:
        rule = self.service.rules.get(rule_id, include_deleted=True)
        if rule is None:
            return
        if not confirm(self, "彻底删除",
                       f"「{rule.name}」将被永久删除，无法还原。确定继续？",
                       self._p, ok_text="彻底删除", danger=True):
            return
        self.service.rules.purge(rule_id)
        self._schedule_refresh()

    def closeEvent(self, event) -> None:  # noqa: N802
        event.accept()


__all__ = ["AutomationTrashDialog", "TrashCard"]
