# SPDX-License-Identifier: GPL-3.0-or-later
"""电脑自动化**独立触发日志窗**：默认跨全部规则，可筛选、可搜索。

从列表页的「日志」按钮叠出来。以前日志塞在编辑页底部 56px 的小列表里，只能看两行、
还只看得见当前打开的那一条规则；排查「刚才那串动作跑到哪一步」得逐个规则点开。
这里改成一张五列表格（时刻 / 规则 / 快捷键 / 结果 / 说明），**默认显示所有规则**。

**一次触发只占一行**：触发行带一个圆角展开按钮，展开后缩进列出这次触发里每一步动作
的记录（``LogEntry.run`` 是同一批次号）。不折叠的话按几次键就刷满一屏，"哪几步属于
哪一次"根本看不出来。父子行的文字 / 颜色 / 筛选口径全在 ``core/automation/logbook.py``，
本文件只负责画树和收集条件；这样换主题 / 换窗口不会悄悄长出第二套语义。

**展开按钮是自己画的**（``_LogTree.drawBranches``）：Qt 默认那枚三角由当前 style 画，
9px 宽、``text_soft`` 色，浅色下压在白底上几乎看不出是"可以点的地方"（用户 11-14 报）。
现在画成 accent 淡底 + 描边的圆角小按钮，展开朝下、收起朝右，鼠标移上去变手型。

**展开状态不记忆**（用户 11-14 定）：每次打开窗口、每次换筛选都是全部收起——记住的
展开态会在"新日志插到顶部、行号整体下移"之后挂到别的行上，看着像串了行；一次触发
想看细节点一下就行，不值得为它养一套跨重载的恢复逻辑。

筛选的判定全在纯逻辑层 ``core/automation/logbook.py``，本文件只负责画表格和收集
条件；这样换主题 / 换窗口不会悄悄长出第二套筛选语义，离线验证也能直接跑。

**日志落盘**（``core/automation/logstore.py``）：每条都追加到
``<data>/automation_log.jsonl``，启动时 ``AutomationService`` 读回最近 500 条，所以
关掉程序再打开还能翻到上次那一串。窗口状态条写明这一点，并给一枚「日志文件」按钮
直接打开该文件所在的文件夹。

**分页**（``PAGE_ROWS = 50`` 行/页，页脚 ``‹ 第 x / y 页 ›``）：分页不是为了"看着方便"，
而是**列宽要量文字**——不分页的话日志攒到几千条，每次重载都要量上千段文字，迟早卡。
一次只量当前这一页，代价是不同页的列宽可能不一样（正是要的"每页独立分配"）。
翻页重建 + 列宽重算 + 回顶；新日志到达时只有第一页跟着长，**翻着旧页不打断**。

新日志到来时：**无筛选走增量**（只把子行挂到已有的父行上，不动滚动位置）；
有筛选时整表重载，但会把滚动值 / 当前行原样恢复——一串动作几十毫秒内连发好几条，
全量重建 500×5 个 item 会把用户正在看的那行顶掉。
"""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QSignalBlocker, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.automation import (
    HEAD_OUTCOMES,
    OUTCOME_LABELS,
    OUTCOME_ORDER,
    TIME_RANGES,
    LogRow,
    child_columns,
    full_text,
    group_entries,
    outcome_tone,
    row_columns,
    row_tone,
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
from .confirm import confirm
from .settings import dialog_qss

#: 行高兜底值。真实行高由 QSS 的 padding + 字号决定（约 21px），增量插入后补滚动值时
#: 优先问表格自己（``sizeHintForRow``），这个数只在表格还没有行的时候用
ROW_H = 26
#: 每页显示多少次触发（顶层行）。**分页不是为了"看着方便"，而是为了列宽**：列宽要量
#: 文字，量的是**当前这一页**的行；不翻页的话日志越攒越多，每次重载都要量几百上千段
#: 文字，迟早会卡（用户 11-15 提的正是这一点）。
PAGE_ROWS = 50
#: 父行上存批次号的 role（``run`` 现在是字符串，这里只用来找父行、不做状态）
_RUN_ROLE = Qt.UserRole + 1
#: 分支区宽度 = 展开按钮能用的地方。**必须比按钮大一圈**，否则点不着
INDENT = 22
#: 展开按钮的边长。表格行高实际约 21px（QSS 上下各 4px padding + 12px 字），
#: 18px 的方块上下各留 1~2px，再大就顶到分隔线了
CHIP = 18
#: 「时刻」列宽的兜底下限（样本取最宽的数字）。时刻是定长文本，按它打底之后，
#: 列宽不会因为"屏幕上恰好没有某一天"而忽宽忽窄
_TIME_SAMPLE = "88-88 88:88:88"
#: 每一列的左右留白：QSS ``::item { padding: 4px 8px }`` 左右各 8px，另留 12px 余量
_COL_PAD = 2 * 8 + 12
#: 子行在**时刻**列里的左偏移。**是两级缩进，不是一级**：``rootIsDecorated`` 让父行自己
#: 也占掉一级（实测父行 ``rect.left() = INDENT``、子行 ``2 × INDENT``），少算一级就会把
#: 子行的时刻裁成"11-14 23:0…"（用户 11-14 23:4x 实测报的就是这个）
_CHILD_INDENT = INDENT * 2


def _chip_rect(branch: QRect) -> QRect:
    """展开按钮在分支区（``drawBranches`` 给的 rect）里的位置：靠左、竖直居中。"""
    return QRect(branch.left() + 3, branch.top() + (branch.height() - CHIP) // 2,
                 CHIP, CHIP)


def _paint_chip(p: QPainter, box: QRect, expanded: bool, pal: theme.Palette,
                hot: bool) -> None:
    """圆角小按钮 + 一道 chevron。同 ``ui/tree.py`` 的折角，但多一层底色当"按钮"。"""
    amount = (0.34 if hot else 0.24) if pal.is_dark else (0.24 if hot else 0.15)
    p.setPen(QPen(QColor(theme.blend(pal.border, pal.accent, 0.70)), 1.0))
    p.setBrush(QColor(theme.blend(theme.panel_bg(pal), pal.accent, amount)))
    p.drawRoundedRect(box, 5, 5)

    pen = QPen(QColor(pal.accent), 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    c = box.center()
    cx, cy = c.x() + 0.5, c.y() + 0.5
    if expanded:   # 朝下：展开后"能再收回去"
        p.drawPolyline([QPoint(int(cx - 3), int(cy - 2)), QPoint(int(cx), int(cy + 1)),
                        QPoint(int(cx + 3), int(cy - 2))])
    else:          # 朝右：收起时"点进去看细节"
        p.drawPolyline([QPoint(int(cx - 2), int(cy - 3)), QPoint(int(cx + 1), int(cy)),
                        QPoint(int(cx - 2), int(cy + 3))])


class _LogTree(QTreeWidget):
    """日志表。唯一自己画的东西是左侧那枚展开按钮（其余交给 QSS / 列宽）。

    **点击由 Qt 处理**：``setItemsExpandable(True)`` 下点分支区（宽度 = ``indentation``）
    Qt 自己会展开/收起，这里不再自己判一遍——两处都做会变成"点一下展开又被收回去"。
    本类只负责把那个区域画成一个看得见的按钮，并让鼠标移上去变手型。
    """

    def __init__(self, palette: theme.Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._p = palette
        self._hot: QTreeWidgetItem | None = None
        #: 视口尺寸变了要回头调「说明」列宽（窗口变宽 → 它撑满；变窄 → 开始横向滚）
        self.on_resized = None
        self._resizing = False
        self.setIndentation(INDENT)
        self.setRootIsDecorated(True)      # 分支区 = 展开按钮的位置
        self.setItemsExpandable(True)
        self.setMouseTracking(True)        # 不用按住也能收到 move（悬停手型）

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # setColumnWidth 会再触发布局，必须防重入，否则窗口拖动时会互相打架
        if self._resizing or self.on_resized is None:
            return
        self._resizing = True
        try:
            self.on_resized()
        finally:
            self._resizing = False

    def drawBranches(self, painter: QPainter, rect: QRect, index) -> None:  # noqa: N802
        """只画父行的展开按钮；子行不画任何东西（没有第三层）。

        **选中行的分支区要自己补底色**：Qt 把缩进区交给 ``drawBranches`` 画，不补的话
        选中一行时左边会留一块和选中色不一样的"缺口"。
        """
        item = self.itemFromIndex(index)
        if item is None:
            return
        model = self.selectionModel()
        if model is not None and model.isSelected(index):
            painter.fillRect(rect, QColor(self._p.accent_soft))
        if item.childCount() == 0 or item.parent() is not None:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        _paint_chip(painter, _chip_rect(rect), item.isExpanded(), self._p,
                    hot=item is self._hot)
        painter.restore()

    def chip_rect(self, item: QTreeWidgetItem) -> QRect:
        """父行展开按钮在视口里的位置，与 ``drawBranches`` 拿到的区域对齐。

        **不能直接用 ``visualItemRect().left()``**：那是文字区的左边缘，已经越过了整段
        缩进，而按钮画在缩进区里，两者正好差一个 ``INDENT``——按文字区算出来的命中区
        会整体右移一格，鼠标在按钮上不算热点、在文字上反倒算。
        """
        rect = self.visualItemRect(item)
        return _chip_rect(QRect(0, rect.top(), INDENT, rect.height()))

    def _chip_at(self, pos: QPoint) -> QTreeWidgetItem | None:
        item = self.itemAt(pos)
        if item is None or item.childCount() == 0 or item.parent() is not None:
            return None
        return item if self.chip_rect(item).contains(pos) else None

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        hot = self._chip_at(event.position().toPoint())
        if hot is not self._hot:
            self._hot = hot
            self.viewport().update()
        self.viewport().setCursor(Qt.PointingHandCursor if hot is not None else Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hot is not None:
            self._hot = None
            self.viewport().update()
        super().leaveEvent(event)


def _log_qss(p: theme.Palette) -> str:
    return dialog_qss(p) + theme.field_qss(
        p, "QLineEdit#Field", radius=9, padding="7px 10px", font_size=12
    ) + f"""
    QLabel#LogHint {{ color: {p.text_soft}; font-size: 11px; }}
    QLabel#LogStatus {{ color: {p.text_soft}; font-size: 12px; }}
    QLabel#LogPage {{ color: {p.text}; font-size: 12px; }}
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
    QTreeWidget#LogTable {{
        background: {theme.panel_bg(p)}; color: {p.text}; font-size: 12px;
        border: none; border-radius: 10px; gridline-color: transparent;
        outline: none;
    }}
    QTreeWidget#LogTable::item {{
        padding: 4px 8px; border-bottom: 1px solid {theme.panel_divider(p)};
    }}
    QTreeWidget#LogTable::item:selected {{
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

        #: 各列"最长文字宽度"的运行值（``_sync_widths`` / 增量插入都往这里并）。
        #: 必须在建 table 之前建好——``on_resized`` 可能在构造期间就被调到。
        self._col_w = [0, 0, 0, 0, 0]
        self.table = _LogTree(self._p, body)
        self.table.setObjectName("LogTable")
        self.table.on_resized = self._fit_last
        self.table.setHeaderLabels(("时刻", "规则", "快捷键", "结果", "说明"))
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setExpandsOnDoubleClick(True)
        self.table.setUniformRowHeights(True)    # 行高统一，增量插入才好补滚动值
        self.table.setAllColumnsShowFocus(True)
        hh = self.table.header()
        # **五列全部自己管宽度**（见 ``_sync_widths`` / ``_fit_last``）：``ResizeToContents``
        # 会把子行缩进算进列宽、还只看当前展开的行；``Stretch`` 又会在前面几列很宽时
        # 把「说明」压成省略号。这里前四列按内容钉死，「说明」按内容给宽度、窗口有富余
        # 才撑满，总宽超出视口就交给横向滚动条。
        for i in range(5):
            hh.setSectionResizeMode(i, QHeaderView.Fixed)
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
        # 翻页：列宽**按页算**（见 ``_sync_widths``），所以日志攒到再多，一次也只量
        # 这一页的文字。箭头按钮做窄（30px），把宽度让给左边的状态文案——这一行还要放
        # 「日志文件」和「已筛选」两枚按钮，720px 最小宽度下不能挤。
        self.prev_page = QPushButton("‹", status_row)
        self.prev_page.setObjectName("Tool")
        self.prev_page.setFixedWidth(30)
        self.prev_page.setCursor(Qt.PointingHandCursor)
        self.prev_page.setFocusPolicy(Qt.TabFocus)
        self.prev_page.setToolTip("上一页（更早的触发）")
        self.prev_page.setAccessibleName("上一页")
        self.prev_page.clicked.connect(lambda: self._goto_page(self._page - 1))
        srow.addWidget(self.prev_page)
        self.page_label = QLabel("", status_row)
        self.page_label.setObjectName("LogPage")
        self.page_label.setAlignment(Qt.AlignCenter)
        self.page_label.setMinimumWidth(78)   # 固定住，1/9 → 10/99 时不挤动两边按钮
        srow.addWidget(self.page_label)
        self.next_page = QPushButton("›", status_row)
        self.next_page.setObjectName("Tool")
        self.next_page.setFixedWidth(30)
        self.next_page.setCursor(Qt.PointingHandCursor)
        self.next_page.setFocusPolicy(Qt.TabFocus)
        self.next_page.setToolTip("下一页（更新的触发）")
        self.next_page.setAccessibleName("下一页")
        self.next_page.clicked.connect(lambda: self._goto_page(self._page + 1))
        srow.addWidget(self.next_page)
        # 「日志文件」：日志已经落盘，给一个直达入口。打开**所在文件夹**而不是文件本身
        # ——双击 .jsonl 会拿记事本打开，用户要的多半是"看看文件在哪、有多大"。
        self.reveal = QPushButton("日志文件", status_row)
        self.reveal.setObjectName("Tool")
        self.reveal.setCursor(Qt.PointingHandCursor)
        self.reveal.setFocusPolicy(Qt.TabFocus)
        self.reveal.setAccessibleName("打开日志文件所在的文件夹")
        log_path = self._log_path()
        self.reveal.setToolTip(f"打开所在的文件夹\n{log_path or '（尚未配置日志文件）'}")
        self.reveal.clicked.connect(self._reveal_log)
        self.reveal.setVisible(log_path is not None)
        srow.addWidget(self.reveal)
        # 「清空」：日志现在真的落盘了，攒多了得有个地方清。**不可恢复**，所以弹一次确认
        # （和回收站「彻底删除」同一个口径；保存 / 重置这类不弹的仍不弹）。
        self.clear_btn = QPushButton("清空", status_row)
        self.clear_btn.setObjectName("Tool")
        self.clear_btn.setCursor(Qt.PointingHandCursor)
        self.clear_btn.setFocusPolicy(Qt.TabFocus)
        self.clear_btn.setAccessibleName("清空触发日志")
        self.clear_btn.setToolTip("清空触发日志（内存和 automation_log.jsonl 一起删）")
        self.clear_btn.clicked.connect(self._clear_log)
        srow.addWidget(self.clear_btn)
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
        #: 当前页（0 = 最新的一页）与总页数；``_row_count`` 是**筛选后**的总行数
        self._page = 0
        self._pages = 1
        self._row_count = 0
        # 所有控件都建好之后再连：填 Combo 会发 currentIndexChanged
        self.search.textChanged.connect(self._on_filter_changed)
        self.rule_combo.currentIndexChanged.connect(self._on_filter_changed)
        self.outcome_combo.currentIndexChanged.connect(self._on_filter_changed)
        self.range_combo.currentIndexChanged.connect(self._on_filter_changed)
        reset.clicked.connect(self._reset)
        self.service.log_added.connect(self._on_log)
        # 日志被整体清空（内存 + 文件）时要整表重载，否则表里还留着已经不存在的行
        if hasattr(self.service, "log_cleared"):
            self.service.log_cleared.connect(self._on_filter_changed)
        self._reload()
        tab_focus_only(self)

    # -- 列宽 ------------------------------------------------------------
    def _sync_widths(self, rows) -> None:
        """按**当前这一页**重算前四列要多宽，然后钉死（只有「说明」列吃剩余宽度）。

        为什么要自己算、不用 ``ResizeToContents``：树会把**子行的缩进**算进列宽，而且
        ``sizeHintForColumn`` 只看**当前展开的行**——展开一次，被折起来的子行（"发送 /
        延迟"这类比父行"触发 · 1 步"更长的文字）才进入计算，列宽就跳一下，右边的列全跟着
        右移（用户 11-14 报的"展开和收起来回变""第 5 列往右偏一点"都是这个）。

        **只量传进来的这一页**（用户 11-15 提的性能问题）：日志攒到几千条时，全量量文字
        会越来越慢；分页之后一次最多 ``PAGE_ROWS`` 行 × 每行几步，翻页时重算一次即可。
        代价是不同页的列宽可能不一样——这正是用户要的"每页独立分配"。
        口径：**列宽 = 这一页（父行 + 子行）里最长的那段文字**，与展开了哪几行无关，
        所以展开 / 收起不会挪位。

        **表头也算内容**：快捷键列的内容常常只有 "F3"，不把表头"快捷键"算进去，
        表头就会被裁成"快捷…"（用户 11-15 报）。
        """
        self.table.ensurePolished()      # QSS 的 font-size: 12px 要 polish 完才在 font() 里
        widths = [self._text_width(self.table.headerItem().text(i)) for i in range(5)]
        # 时刻列打底：即使一条记录都没有、或这一页恰好没有某一天，宽度也稳定
        widths[0] = max(widths[0], self._text_width(_TIME_SAMPLE) + _CHILD_INDENT)
        for row in rows:
            self._grow_row(widths, row_columns(row), INDENT)
            for child in row.children:
                self._grow_row(widths, child_columns(child), _CHILD_INDENT)
        self._col_w = widths
        self._apply_widths()

    def _text_width(self, text: str) -> int:
        return self.table.fontMetrics().horizontalAdvance(text)

    def _grow_row(self, widths: list[int], cols, indent: int) -> None:
        """把一行的文字宽度并入 ``widths``（缩进只算进第 0 列）。

        **「说明」列也一起量**：它以前是 ``Stretch``，前面几列一宽就被挤成省略号；
        现在按内容给宽度，放不下就横向滚（用户 11-15 报的正是这个）。
        """
        for i, text in enumerate(cols[:5]):
            if not text:
                continue
            w = self._text_width(text) + (indent if i == 0 else 0)
            widths[i] = max(widths[i], w)

    def _apply_widths(self) -> None:
        for i in range(4):
            self.table.setColumnWidth(i, self._col_w[i] + _COL_PAD)
        self._fit_last()

    def _fit_last(self) -> None:
        """「说明」列：内容需要多宽就多宽，窗口有富余才把它撑满。

        这样两头都对：内容短时右边不留一条空白（像原来的 Stretch）；前面几列很宽时
        也不会把它压成 `…`——总宽超出视口就让横向滚动条出来。
        """
        avail = self.table.viewport().width()
        if avail <= 0:                       # 还没布局，先按内容给
            self.table.setColumnWidth(4, self._col_w[4] + _COL_PAD)
            return
        used = sum(self.table.columnWidth(i) for i in range(4))
        self.table.setColumnWidth(4, max(self._col_w[4] + _COL_PAD, avail - used))

    def _note_entry(self, cols, indent: int) -> None:
        """增量来一条时只量这一行：比它宽才改列宽（只增不减，不会来回抖）。"""
        widths = list(self._col_w)
        self._grow_row(widths, cols, indent)
        if widths == self._col_w:
            return
        self._col_w = widths
        self._apply_widths()

    # -- 落盘 ------------------------------------------------------------
    def _log_path(self) -> Path | None:
        """日志文件路径。取不到（离线验证的假 service / 未配置）就返回 ``None``。"""
        raw = getattr(self.service, "log_path", None)
        return Path(raw) if raw else None

    def _reveal_log(self) -> None:
        path = self._log_path()
        if path is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    def _clear_log(self) -> None:
        """清空触发日志（内存 + 文件）。**不可恢复**，先确认一次。

        实际清理由 ``AutomationService.clear_log()`` 做（两处一起清，否则下次启动
        ``restore_log`` 又把文件读回来）；清完它会发 ``log_cleared``，本窗口借此重载。
        """
        clear = getattr(self.service, "clear_log", None)
        if not callable(clear):
            return
        if not confirm(self, "清空日志",
                       "触发日志会从内存和 automation_log.jsonl 里一起删掉，无法恢复。"
                       "确定继续？", self._p, ok_text="清空", danger=True):
            return
        clear()

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
        self._on_filter_changed()

    # -- 树 --------------------------------------------------------------
    def _tone_color(self, tone: str) -> QColor:
        # 三档：真失败红 / 被拦下琥珀 / 正常流程绿
        return QColor({
            "bad": theme.DANGER,
            "neutral": self._amber,
            "ok": theme.ACCENT_GREEN,
        }[tone])

    def _make_top_item(self, row) -> QTreeWidgetItem:
        item = QTreeWidgetItem([str(t) for t in row_columns(row)])
        full = full_text(row.head)
        for col in range(5):
            item.setToolTip(col, full)
        item.setForeground(3, self._tone_color(row_tone(row)))
        if row.run:
            item.setData(0, _RUN_ROLE, str(row.run))
        for child in row.children:
            item.addChild(self._make_child_item(child))
        return item

    def _make_child_item(self, entry) -> QTreeWidgetItem:
        item = QTreeWidgetItem([str(t) for t in child_columns(entry)])
        full = full_text(entry)
        for col in range(5):
            item.setToolTip(col, full)
        item.setForeground(3, self._tone_color(outcome_tone(entry.outcome)))
        return item

    def _item_for_run(self, run: str) -> QTreeWidgetItem | None:
        for i in range(self.table.topLevelItemCount()):
            item = self.table.topLevelItem(i)
            if str(item.data(0, _RUN_ROLE) or "") == run:
                return item
        return None

    def _refresh_counts(self, shown: int) -> None:
        total = len(self.service.engine.log)
        path = self._log_path()
        # 文案只留数字：范围（全部规则 / 某条规则）在下拉和「已筛选」chip 上已经写了，
        # 这里再写一遍会把翻页 / 清空 / 日志文件几枚按钮挤出去（720px 最小宽度实测）。
        # 「落在哪个文件、重启后还在」放 tooltip——真要找文件的人会去点「日志文件」。
        self.status.setText(f"共 {total} 条 · 本页 {shown} 行")
        self.status.setToolTip(
            f"日志文件：{path}\n（每条都即时写入，重启后仍可查看）" if path else "日志只保存在内存")
        self.title_bar.set_subtitle(f"{shown} / {total} 行")
        self._pages = self._page_count(self._row_count)
        self._page = min(self._page, self._pages - 1)
        self.page_label.setText(f"第 {self._page + 1} / {self._pages} 页")
        self.prev_page.setEnabled(self._page > 0)
        self.next_page.setEnabled(self._page + 1 < self._pages)
        # 只有一页时整组藏起来：不占位置，状态文案也不用为了躲它而缩写
        multi = self._pages > 1
        for widget in (self.prev_page, self.page_label, self.next_page):
            widget.setVisible(multi)

    def _page_rows(self) -> list:
        """当前页要显示的行（先按条件分组，再切出这一页）。"""
        rows = group_entries(self.service.engine.log, **self._conditions())
        self._row_count = len(rows)
        self._pages = self._page_count(self._row_count)
        self._page = max(0, min(self._page, self._pages - 1))
        start = self._page * PAGE_ROWS
        return rows[start:start + PAGE_ROWS]

    @staticmethod
    def _page_count(rows: int) -> int:
        return max(1, (rows + PAGE_ROWS - 1) // PAGE_ROWS)

    def _goto_page(self, page: int) -> None:
        """翻页：重建这一页，列宽按这一页重算（`_reload` 里做）。"""
        self._page = max(0, min(page, self._pages - 1))
        self._reload(top=True)

    def _on_filter_changed(self, *_args) -> None:  # noqa: ANN002  搜索 / 三个下拉
        """换筛选条件时回到第一页——新的结果集从头看起。"""
        self._page = 0
        self._reload()

    def _reload(self, *_args, top: bool = False) -> None:  # noqa: ANN002
        rows = self._page_rows()
        # 列宽按**当前这一页**算：日志再多，一次也只量这一页的文字（见 ``_sync_widths``）
        self._sync_widths(rows)
        bar = self.table.verticalScrollBar()
        scroll = bar.value()
        current = self.table.currentItem()
        current_run = str(current.data(0, _RUN_ROLE) or "") if current is not None else ""

        # clear() 会连选中一起丢掉；重建期间挡信号，免得中途发一堆 itemCollapsed
        with QSignalBlocker(self.table):
            self.table.clear()
            for row in rows:
                self.table.addTopLevelItem(self._make_top_item(row))
            if current_run and not top:
                found = self._item_for_run(current_run)
                if found is not None:
                    self.table.setCurrentItem(found)
        bar.setValue(0 if top else scroll)

        self.empty.setVisible(not rows)
        self.table.setVisible(bool(rows))
        self._sync_filter_marks()
        self._refresh_counts(len(rows))

    def _on_log(self, entry) -> None:
        """新来一条日志。

        **筛选态整表重载**：组是否可见取决于"父行或任一步命中"，增量插入很难保证
        与重载一致；重载会把滚动值、当前行恢复，用户看不出闪（展开态一律收起，
        见模块文档：记住的展开态会在行号整体下移后挂到别的行上）。
        **无筛选走增量**：把子行挂到已有父行上，不动其它任何东西——这是常态路径，
        一串动作连发时也不会打断用户正在看的位置。
        **人在翻旧页时不打断**：只有第一页跟着新日志长，其它页保持原样（否则每来一条
        就把正在看的那页整体挤走一行）。
        """
        if self._is_filtered():
            self._reload()
            return
        if self._page != 0:
            # 只在"新的一次触发 / 独立记录"时多一行，子行不改变行数
            if not entry.run or entry.outcome in HEAD_OUTCOMES:
                self._row_count += 1
            self._refresh_counts(self.table.topLevelItemCount())
            return
        if entry.run:
            item = self._item_for_run(str(entry.run))
            if item is not None:
                item.addChild(self._make_child_item(entry))
                self._note_entry(child_columns(entry), _CHILD_INDENT)
                self._refresh_summary(item)
                self._refresh_counts(self.table.topLevelItemCount())
                return
            if entry.outcome not in HEAD_OUTCOMES:
                # 带批次号却找不到父行（父行被本页行数挤到下一页 / 窗口刚打开时序异常）：
                # 宁可重建一次，也别在顶上插一条孤立的动作行——那会让人以为动作没头没尾。
                self._reload()
                return
        # 新的一次触发（或独立记录）：插到最上面，补一行高度保住视口内容
        self._prepend_row(entry)

    def _refresh_summary(self, item: QTreeWidgetItem) -> None:
        """子行加进来后重算父行的结果列（步数 / 失败数 / 颜色）。

        「步数」变长（"触发 · 1 步"→"触发 · 12 步（3 步失败）"）会把结果列撑宽，
        所以这里也要顺手量一次，否则新到的失败原因一长就被裁。
        """
        run = str(item.data(0, _RUN_ROLE) or "")
        row = next((r for r in group_entries(self.service.engine.log) if r.run == run), None)
        if row is None:
            return
        cols = row_columns(row)
        for col, text in enumerate(cols):
            item.setText(col, str(text))
        item.setForeground(3, self._tone_color(row_tone(row)))
        self._note_entry(cols, INDENT)

    def _prepend_row(self, entry) -> None:
        bar = self.table.verticalScrollBar()
        scroll = bar.value()
        current = self.table.currentItem()
        item = self._make_top_item(LogRow(entry))
        self.table.insertTopLevelItem(0, item)
        self._note_entry(row_columns(LogRow(entry)), INDENT)
        if current is not None:
            self.table.setCurrentItem(current)
        # 视口内容整体下移一行，补回一行高度 = 用户看到的还是原来那几条
        step = self.table.sizeHintForRow(0) or ROW_H
        bar.setValue(scroll if scroll == 0 else scroll + step)
        self._row_count += 1
        # 本页只留 PAGE_ROWS 行；多出来的推到后面几页（下拉/翻页时重建）
        while self.table.topLevelItemCount() > PAGE_ROWS:
            self.table.takeTopLevelItem(self.table.topLevelItemCount() - 1)
        self.empty.setVisible(False)
        self.table.setVisible(True)
        self._refresh_counts(self.table.topLevelItemCount())

    def closeEvent(self, event) -> None:  # noqa: N802
        event.accept()


__all__ = ["AutomationLogDialog"]
