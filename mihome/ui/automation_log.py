# SPDX-License-Identifier: GPL-3.0-or-later
"""电脑自动化**独立触发日志窗**：默认跨全部规则，可筛选、可搜索。

从列表页的「日志」按钮叠出来。以前日志塞在编辑页底部 56px 的小列表里，只能看两行、
还只看得见当前打开的那一条规则；排查「刚才那串动作跑到哪一步」得逐个规则点开。
这里改成一张五列表格（时刻 / 规则 / 快捷键 / 结果 / 说明），**默认显示所有规则**。

筛选的判定全在纯逻辑层 ``core/automation/logbook.py``，本文件只负责画表格和收集
条件；这样换主题 / 换窗口不会悄悄长出第二套筛选语义，离线验证也能直接跑。
日志只活内存（``AutomationEngine.log``，上限 500 条，重启清空），界面里如实写明，
别让用户以为关掉程序还能翻回来。

新日志走 ``insertRow(0)`` **增量插入**并保留滚动位置 / 选中行：一串动作几十毫秒内
连发好几条，全量重建 500×5 个 item 会把用户正在看的那行顶掉。
"""

from __future__ import annotations

import time

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.automation import (
    OUTCOME_LABELS,
    OUTCOME_ORDER,
    TIME_RANGES,
    filter_entries,
    full_text,
    log_columns,
    matches,
    outcome_tone,
)
from ..core.automation.service import AutomationService
from . import theme
from .automation_style import (
    chip_border, common_qss, fit_height, reserve_scrollbar, surface_fill, tab_focus_only,
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
from .combo import Combo, combo_qss
from .settings import dialog_qss

#: 表格行高，增量插入后补回滚动值要用同一个数
ROW_H = 26
MAX_ROWS = 500


def _log_qss(p: theme.Palette) -> str:
    return dialog_qss(p) + theme.field_qss(
        p, "QLineEdit#Field", radius=9, padding="7px 10px", font_size=12
    ) + f"""
    QLabel#LogHint {{ color: {p.text_soft}; font-size: 11px; }}
    QLabel#LogStatus {{ color: {p.text_soft}; font-size: 12px; }}
    /* 这一排按钮长在**对话框底**上（不在卡片里），底色必须从 surface_bg 起算：
       以前用 blend(p.bg,…)，浅色下是 #DEDFE1 压在 #E6E7E9 上——1.09:1，只剩一圈
       描边，和确认框「直接关闭」是同一个毛病（用户 11-13 让顺带查一遍）。 */
    QPushButton#Tool {{
        background: {surface_fill(p, 0.14)}; color: {p.text};
        border: 1px solid {chip_border(p)}; border-radius: 9px;
        padding: 7px 13px; font-size: 12px;
    }}
    QPushButton#Tool:hover {{
        background: {surface_fill(p, 0.22)}; color: {p.accent}; border-color: {p.accent};
    }}
    QPushButton#Tool:pressed {{ background: {surface_fill(p, 0.28)}; }}
    /* 「已筛选」状态 chip：一眼看出结果不是全量，点一下就清掉条件。
       底色用 accent 染色 + 1px accent 描边，文字用主文字色——以前是
       accent_soft 底 + accent 字，浅色下两者只有 3.29:1，压在白卡边上几乎糊成一片。 */
    QPushButton#FilterChip {{
        background: {theme.blend(theme.surface_bg(p), p.accent, 0.22 if p.is_dark else 0.12)};
        color: {p.text}; border: 1px solid {p.accent};
        border-radius: 9px; padding: 4px 12px; font-size: 11px;
    }}
    QPushButton#FilterChip:hover {{
        background: {theme.blend(theme.surface_bg(p), p.accent, 0.32 if p.is_dark else 0.20)};
        color: {p.accent};
    }}
    QPushButton#FilterChip:pressed {{
        background: {theme.blend(theme.surface_bg(p), p.accent, 0.40 if p.is_dark else 0.28)};
    }}
    QTableWidget#LogTable {{
        background: {theme.panel_bg(p)}; color: {p.text}; font-size: 12px;
        border: none; border-radius: 10px; gridline-color: transparent;
        outline: none;
    }}
    QTableWidget#LogTable::item {{
        padding: 4px 8px; border-bottom: 1px solid {theme.panel_divider(p)};
    }}
    QTableWidget#LogTable::item:selected {{
        background: {p.accent_soft}; color: {p.text};
    }}
    QHeaderView::section {{
        background: {theme.panel_bg(p)}; color: {p.text_soft}; font-size: 11px;
        border: none; border-bottom: 1px solid {p.border}; padding: 7px 8px;
    }}
    """ + common_qss(p)


class AutomationLogDialog(QDialog):
    """日志层。只读 ``AutomationService.engine.log``，不写规则、不发云端请求。"""

    def __init__(
        self,
        service: AutomationService,
        palette: theme.Palette,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self._p = palette
        self.setWindowTitle("触发日志")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self._frame = WindowFrame(
            self, resizable=True,
            radius=theme.RADIUS_CARD,
            shadow=SHADOW_PAD,
            shadow_alpha=shadow_alpha(self._p),
            fill=lambda: surface_bg(self._p),
            border=lambda: surface_border(self._p),
        )
        self.resize(*floating_size(900, fit_height(620, reserve=2 * SHADOW_PAD)))
        self.setMinimumSize(*floating_size(720, 420))
        self.setStyleSheet(_log_qss(palette))
        # 浅色下纯 #F3BE44 对白底只有 1.7:1，压深一点才读得清
        self._amber = (theme.SLIDER_WARM if palette.is_dark
                       else theme.blend(theme.SLIDER_WARM, palette.text, 0.35))

        outer = QVBoxLayout(self._frame.body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.title_bar = TitleBar("触发日志", palette, buttons=("close",),
                                  variant="dialog", frame=self._frame)
        outer.addWidget(self.title_bar)

        body = QWidget(self._frame.body)  # 内容区，别挂窗口（窗口大一圈）
        col = QVBoxLayout(body)
        col.setContentsMargins(18, 12, 18, 14)
        col.setSpacing(10)

        # 筛选条：关键字 + 规则 + 结果 + 时间范围 + 重置
        filters = QWidget(body)
        frow = QHBoxLayout(filters)
        frow.setContentsMargins(0, 0, 0, 0)
        frow.setSpacing(8)
        self.search = QLineEdit(filters)
        self.search.setObjectName("Field")
        self.search.setPlaceholderText("搜索规则名 / 快捷键 / 事件名 / 说明…")
        self.search.setClearButtonEnabled(True)
        frow.addWidget(self.search, 1)

        self.rule_combo = Combo(self._p, filters)
        self.rule_combo.setFixedWidth(150)
        frow.addWidget(self.rule_combo)
        self.outcome_combo = Combo(self._p, filters)
        self.outcome_combo.setFixedWidth(132)
        frow.addWidget(self.outcome_combo)
        self.range_combo = Combo(self._p, filters)
        self.range_combo.setFixedWidth(126)
        frow.addWidget(self.range_combo)
        reset = QPushButton("重置", filters)
        reset.setObjectName("Tool")
        reset.setFocusPolicy(Qt.TabFocus)
        reset.setCursor(Qt.PointingHandCursor)
        frow.addWidget(reset)
        col.addWidget(filters)

        self.table = QTableWidget(0, 5, body)
        self.table.setObjectName("LogTable")
        self.table.setHorizontalHeaderLabels(("时刻", "规则", "快捷键", "结果", "说明"))
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        hh = self.table.horizontalHeader()
        for i in range(4):
            hh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        # 竖向滚动条常驻占位（同列表窗）：记录少的时候"时刻/规则"那几列不会因为
        # 滚动条忽有忽无而左右跳。横向条**保留**——列多到放不下时它还得能滚，
        # 而且它在底部只占高度，不影响列宽。
        reserve_scrollbar(self.table, lock_horizontal=False)
        col.addWidget(self.table, 1)

        self.empty = QLabel(
            "还没有触发记录\n按下配置好的快捷键后，触发与动作执行过程会显示在这里", body)
        self.empty.setObjectName("LogHint")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        # **必须带 stretch**：表格空态是隐藏的，它那份 stretch 也跟着没了，剩下的
        # 富余高度会按 sizePolicy 均摊给筛选条和这句说明——空态时筛选条被顶到
        # 窗口中间、说明离底边还有一大截（用户 11-13 报）。给说明 stretch=1，
        # 富余高度就全落在它身上，文字在中间那块区域里居中。
        col.addWidget(self.empty, 1)

        status_row = QWidget(body)
        srow = QHBoxLayout(status_row)
        srow.setContentsMargins(0, 0, 0, 0)
        srow.setSpacing(10)
        self.status = QLabel("", status_row)
        self.status.setObjectName("LogStatus")
        srow.addWidget(self.status, 1)
        self.filter_chip = QPushButton("已筛选 · 点击清除", status_row)
        self.filter_chip.setObjectName("FilterChip")
        self.filter_chip.setCursor(Qt.PointingHandCursor)
        self.filter_chip.setFocusPolicy(Qt.TabFocus)
        self.filter_chip.clicked.connect(self._reset)
        self.filter_chip.setVisible(False)
        srow.addWidget(self.filter_chip)
        col.addWidget(status_row)
        outer.addWidget(body, 1)

        self._fill_combos()
        # 所有控件都建好之后再连：填 Combo 会发 currentIndexChanged
        self.search.textChanged.connect(self._reload)
        self.rule_combo.currentIndexChanged.connect(self._reload)
        self.outcome_combo.currentIndexChanged.connect(self._reload)
        self.range_combo.currentIndexChanged.connect(self._reload)
        reset.clicked.connect(self._reset)
        self.service.log_added.connect(self._on_log)
        self._reload()
        tab_focus_only(self)

    # -- 筛选项 ----------------------------------------------------------
    def _fill_combos(self) -> None:
        # 规则下拉要包含「已经彻底删掉、但历史里还留着名字」的规则，否则那段历史筛不出来
        names: dict[str, str] = {}
        home = str(getattr(self.service, "current_home", "") or "")
        for rule in self.service.rules.rules:
            # 规则按家庭隔离：下拉只列本家庭的；历史里别的家庭留下的记录由下面的
            # setdefault 补进来，那段日志仍筛得出来
            if home and rule.home and rule.home != home:
                continue
            names[rule.id] = rule.name or "未命名规则"
        for entry in self.service.engine.log:
            names.setdefault(entry.rule_id, entry.rule_name or "未命名规则")

        with QSignalBlocker(self.rule_combo):
            self.rule_combo.addItem("全部规则", "")
            for rid, name in names.items():
                self.rule_combo.addItem(name, rid)
        with QSignalBlocker(self.outcome_combo):
            self.outcome_combo.addItem("全部结果", "")
            for outcome in OUTCOME_ORDER:
                self.outcome_combo.addItem(OUTCOME_LABELS[outcome], outcome)
        with QSignalBlocker(self.range_combo):
            for label, seconds in TIME_RANGES:
                self.range_combo.addItem(label, seconds)

    def _conditions(self) -> dict:
        seconds = int(self.range_combo.currentData() or 0)
        return {
            "keyword": self.search.text(),
            "rule_id": str(self.rule_combo.currentData() or ""),
            "outcome": str(self.outcome_combo.currentData() or ""),
            "since": time.time() - seconds if seconds else 0.0,
        }

    def _is_filtered(self) -> bool:
        return bool(
            self.search.text()
            or self.rule_combo.currentData()
            or self.outcome_combo.currentData()
            or self.range_combo.currentIndex() != 0
        )

    def _mark_combo(self, combo: Combo, active: bool) -> None:
        """非默认值的下拉加 accent 描边；Combo 自带 stylesheet，得整份重设。

        顺带把 ``:hover`` 一起覆盖，否则鼠标一放上去 ``combo_qss`` 的 hover 规则
        特异性更高，激活态边框就闪没了。
        """
        extra = (f"QComboBox, QComboBox:hover {{ border: 1px solid {self._p.accent}; }}"
                 if active else "")
        combo.setStyleSheet(combo_qss(self._p) + extra)

    def _sync_filter_marks(self) -> None:
        cond = self._conditions()
        self._mark_combo(self.rule_combo, bool(cond["rule_id"]))
        self._mark_combo(self.outcome_combo, bool(cond["outcome"]))
        self._mark_combo(self.range_combo, self.range_combo.currentIndex() != 0)
        self.filter_chip.setVisible(self._is_filtered())

    def _reset(self, *_args) -> None:  # noqa: ANN002  clicked / 手动调用都行
        with QSignalBlocker(self.search):
            self.search.clear()
        for combo in (self.rule_combo, self.outcome_combo, self.range_combo):
            with QSignalBlocker(combo):
                combo.setCurrentIndex(0)
        self._reload()

    # -- 表格 ------------------------------------------------------------
    def _fill_row(self, row: int, entry) -> None:
        for col, text in enumerate(log_columns(entry)):
            item = QTableWidgetItem(text)
            item.setToolTip(full_text(entry))
            if col == 3:
                # 三档：真失败红 / 被拦下琥珀 / 正常流程绿
                color = {
                    "bad": theme.DANGER,
                    "neutral": self._amber,
                    "ok": theme.ACCENT_GREEN,
                }[outcome_tone(entry.outcome)]
                item.setForeground(QColor(color))
            self.table.setItem(row, col, item)
        self.table.setRowHeight(row, ROW_H)

    def _refresh_counts(self, shown: int) -> None:
        total = len(self.service.engine.log)
        scope = ("全部规则" if not self.rule_combo.currentData()
                 else str(self.rule_combo.currentText()))
        self.status.setText(
            f"{scope} · 共 {total} 条记录，当前显示 {shown} 条"
            "　·　日志只保存在内存，重启程序后清空")
        self.title_bar.set_subtitle(f"{shown} / {total} 条")

    def _reload(self, *_args) -> None:  # noqa: ANN002  textChanged / currentIndexChanged
        shown = filter_entries(self.service.engine.log, **self._conditions())
        self.table.setRowCount(len(shown))
        for row, entry in enumerate(shown):
            self._fill_row(row, entry)
        self.empty.setVisible(not shown)
        self.table.setVisible(bool(shown))
        self._sync_filter_marks()
        self._refresh_counts(len(shown))

    def _on_log(self, entry) -> None:
        """新来一条 = 增量插到最上面，不整表重建、不打断用户正在看的位置。"""
        if not matches(entry, **self._conditions()):
            return
        self._prepend_entry(entry)

    def _prepend_entry(self, entry) -> None:
        bar = self.table.verticalScrollBar()
        scroll = bar.value()
        selected = self.table.currentRow()
        self.table.insertRow(0)
        self._fill_row(0, entry)
        if selected >= 0:
            self.table.selectRow(selected + 1)
        # 视口内容整体下移一行，补回一行高度 = 用户看到的还是原来那几条
        bar.setValue(scroll if scroll == 0 else scroll + ROW_H)
        if self.table.rowCount() > MAX_ROWS:
            self.table.removeRow(self.table.rowCount() - 1)
        self.empty.setVisible(False)
        self.table.setVisible(True)
        self._refresh_counts(self.table.rowCount())

    def closeEvent(self, event) -> None:  # noqa: N802
        event.accept()


__all__ = ["AutomationLogDialog"]
