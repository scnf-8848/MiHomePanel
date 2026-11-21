# SPDX-License-Identifier: GPL-3.0-or-later
"""全局下拉框。

三个毛病都出在 Qt 自己身上，不是样式没写够：

1. **没有 "v" 展开提示**。一旦给 ``QComboBox`` 写了 QSS，``::down-arrow`` 就没人画
   了；原来两处只留了个空的 ``::drop-down`` 区，控件看起来像一枚不能点的标签。
   这里在 ``paintEvent`` 末尾自己画一支抗锯齿的 chevron，展开时翻成 "^"。
   QSS 图片要带资源文件、还要按主题准备两套，自绘反而更省。

2. **展开后方底 / 四角黑块**。popup 不是 combo 的子控件，而是独立的顶层窗口
   ``QComboBoxPrivateContainer``（一个 QFrame），它会自己画一层方形框底；
   ``QComboBox QAbstractItemView { border-radius: ... }`` 只画在里层的 view 上，
   四角外面仍是那块方底。和右键菜单（``ui/menu.py``）、悬停提示
   （``ui/tooltip.py``）是同一个病。

   但要留意一个**只在真机上出现的坑**：``QMenu`` 在构造时（窗口还没创建）就设了
   ``WA_TranslucentBackground``，所以原生窗口自带 ``WS_EX_LAYERED`` + 8bit alpha；
   而 combo 的 container 是 ``QComboBox::showPopup()`` 内部懒创建并**立刻显示**的，
   等我们拿到它再补属性，原生窗口早按不透明建好了——Windows 上 alpha 通道是
   ``-1``，四角的"透明像素"被合成成纯黑方块。离屏 ``grab()`` 只抓控件自身，
   四角 alpha=0 一片绿，真机却是黑的，典型的假绿。所以第一次展开必须
   hide → 设属性 → 把 QWindow 的 format 改成 8bit alpha → 销毁旧原生窗口 →
   ``setWindowFlags`` 触发重建 → show，把它变成和 QMenu 一样的 layered 窗口。

   绘制层也不交给 QSS 了：``_PopupSurface`` 接管 container 的 Paint，先用
   ``CompositionMode_Clear`` 清出透明，再 ``fillPath`` 画抗锯齿圆角。这样
   QFrame 自己那层方底、当前项的方框、QSS ``border`` 补的斜接外框一起消失。

3. **滚轮直接切选项**。``QComboBox::wheelEvent`` 默认一格换一项；鼠标停在详情页
   的下拉框上想滚页面，设备属性就被悄悄改了。这里整个忽略滚轮事件，让它冒泡给
   父级 ``QScrollArea`` 去滚页面——只去掉"改值"，不去掉"滚动"。
   键盘上下键仍然可用（那是明确的、有意的输入）。

4. **展开后盖住控件**。``QComboBox::showPopup()`` 会把列表的**当前项**对齐到控件
   本身（Qt 的老式组合框行为），于是弹出层正好糊在控件上，用户看不到自己原来
   选的是什么。这里在 show 之后重新摆位：控件下方留 8px 空隙、右边缘对齐，
   下方放不下才翻到上方。控件和 "^" 箭头全程可见。

5. **高亮会跟着鼠标跑，认不出原本选的是哪项**。``QStyle.State_Selected`` 在下拉
   里表示**列表视图的临时当前项**：Qt 的 ``QComboBoxPrivateContainer`` 每次鼠标
   划过就 ``view->setCurrentIndex()``，所以那块高亮根本不是"已选值"，只是"鼠标
   在哪儿"。真正的值在 ``combo.currentIndex()`` 里，弹出期间不会变。这里把两件事
   拆开：``currentIndex`` 那一行画稳定的选中底 + 一支 accent 色的 ✓，并且**所有
   行都预留同一个右侧勾槽**（文字 elide 边界不随鼠标跳动）；临时高亮只用一层更淡
   的中性色跟着鼠标走。这样鼠标移到别处，勾仍钉在原选项上。

6. **弹出层宽度跟着控件走，长选项被省略成「…」**。Qt 让 container 和 combo 同宽，
   可控件宽度是布局给的：编辑页一行四格，单位那格 80px，「毫秒」写不下；音箱条
   上那格 88px，「不静默」变成「不…」。**两者本来就该分开算**——控件窄是为了不
   挤邻居，弹出层是浮层，没人跟它抢地方。``showPopup`` 里按最长一项把 container
   和 view 一起撑宽（上限 ``POPUP_MAX_EXTRA``、且不出屏幕），再按新宽度对右边缘。

样式集中在 ``combo_qss()`` / ``popup_qss()``，设置窗口和设备详情页共用同一份。
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEvent,
    QModelIndex,
    QObject,
    QPoint,
    QPointF,
    QRectF,
    QSize,
    Qt,
)
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from . import theme

#: 箭头尺寸与离右边缘的距离（px）。右内边距留 38px：详情页的枚举项常常是一整句
#: 中文，Qt 会把它 elide 到 editField 边界，只留 30px 时最后一个字几乎贴着箭头。
ARROW_W = 9.0
ARROW_H = 5.0
ARROW_RIGHT = 13.0
ARROW_PEN = 1.7
POPUP_RADIUS = theme.RADIUS_SMALL
#: 弹出层与控件的间隙：不要贴着，也不要盖住控件（箭头和当前项要一直看得见）
POPUP_GAP = 8
#: 列表行。Qt 默认只给 21px，中文 13px 字挤在里面，还给当前项画了个方框
#: （QStyle 的 focus/current 框），不接管 delegate 就消不掉。
ITEM_H = 34
ITEM_PAD_X = 10
ITEM_INSET_X = 4
ITEM_RADIUS = 8
#: 右侧"当前值"勾槽。**每一行都预留**，否则鼠标划到别的行时文字的 elide 边界
#: 会随之变宽/变窄，看起来像字在抖；勾与收起态右侧那支 chevron 大致同列。
CHECK_W = 20
CHECK_MARK_W = 11.0
CHECK_MARK_H = 6.0
CHECK_PEN = 1.9

#: 弹出层**允许比控件本身宽出多少**（px）。控件宽度是一行格子让出来的——编辑页
#: 一行四格，「秒」那格只有 80px，把「毫秒」写进去就只剩「…」。但弹出层是浮层，
#: 往右多长出去一点不会挤别的控件，宁可让它长出去，也别把选项缩成省略号
#: （用户 11-13 第二轮报：宽度跟控件绑死是根本原因）。
POPUP_MAX_EXTRA = 220
#: 算宽度时先给竖向滚动条留位置：留少了，条一出现文字区又变窄，照样省略。
POPUP_BAR_W = 14
#: view 自己的 QSS padding（``combo_qss`` 里写的 6px，四边），算宽度要加回来。
VIEW_PAD = 6


def combo_qss(p: theme.Palette) -> str:
    """主控件 + 弹出列表里每一项的样式。"""
    field = theme.field_bg(p)
    # 描边与输入框同一支（theme.field_border）：一行里四格挨着，下拉有边框、
    # 输入框没边框会读成两种控件。以前这里单独算 0.10，深色下比输入框更淡。
    line = theme.field_border(p)
    return f"""
    QComboBox {{
        background: {field};
        color: {p.text};
        border: 1px solid {line};
        border-radius: 9px;
        padding: 6px 38px 6px 12px;
        font-size: 13px;
    }}
    QComboBox:hover {{
        border-color: {theme.blend(field, p.text, 0.24 if p.is_dark else 0.20)};
    }}
    QComboBox:disabled {{
        color: {p.text_mute};
        background: {theme.blend(field, p.bg, 0.45)};
        border-color: {line};
    }}
    /* 箭头由 paintEvent 自绘；把原生那一块彻底关掉，免得冒出第二个 */
    QComboBox::drop-down {{ border: none; width: 0px; }}
    QComboBox::down-arrow {{ width: 0px; height: 0px; }}
    /* 条目由 PopupDelegate 自绘，这里只负责把 view 自身的底和选中底抹干净：
       Qt 默认会用 palette.highlight() 在列表里画一块方底（树里的蓝方块同源）。 */
    QComboBox QAbstractItemView {{
        background: transparent;
        border: none;
        outline: none;
        padding: 6px;
        selection-background-color: transparent;
        show-decoration-selected: 0;
    }}
    """


def popup_qss(p: theme.Palette) -> str:  # noqa: ARG001 - 颜色由 _PopupSurface 画
    """容器本身保持全透明，连 QSS 圆角都不写。

    背景交给 ``_PopupSurface`` 用 ``QPainterPath`` 画（``popup_bg``，比承载它的
    ``panel_bg`` 卡片再高一档，否则菜单会隐身进同色卡片里）。这里一旦写
    ``background`` 或 ``border``，QFrame 就会在圆角之外再补一层方底/斜接外框，
    高 DPI 下尤其明显；层级只靠底色明度差（``theme.py`` 开头定的规矩）。
    """
    return (f"QFrame {{ background: transparent; border: none; "
            f"border-radius: {POPUP_RADIUS}px; }}")


class _PopupSurface(QObject):
    """container 的背景自绘。

    拦截 Paint 并返回 True，``QFrame::paintEvent`` 就不会再画那层方底。先
    ``CompositionMode_Clear`` 把整块 backing store 清成 alpha=0，再填一个抗锯齿
    圆角路径——四角是不是真透明由这一步保证，不靠 QSS、也不靠 Qt 每帧帮忙清屏。
    """

    def __init__(self, palette: theme.Palette, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._p = palette

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if event.type() != QEvent.Paint:
            return False
        p = QPainter(watched)
        p.setRenderHint(QPainter.Antialiasing)
        p.setCompositionMode(QPainter.CompositionMode_Clear)
        p.fillRect(watched.rect(), Qt.transparent)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        path = QPainterPath()
        path.addRoundedRect(QRectF(watched.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
                            POPUP_RADIUS, POPUP_RADIUS)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.popup_bg(self._p)))
        p.drawPath(path)
        # 同一条路径再描一圈淡边：浮层压在同族色的分区卡上，只靠底色差在
        # 浅色下边界偏弱。自绘描边是抗锯齿的，不会像 QSS border 那样长出
        # 一圈方角斜接外框。
        p.setPen(QPen(QColor(theme.popup_border(self._p)), 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.end()
        return True


class PopupDelegate(QStyledItemDelegate):
    """列表里的每一行自己画。

    ``QStyledItemDelegate::paint`` 会请 QStyle 画 focus/current 框和
    ``palette.highlight()`` 的方底——正是"圆角背后还有矩形"的另一种长相。
    这里不调 super，只画一块圆角高亮 + 一行字 + （当前值那一行的）✓。

    两块底色语义不同，**别合并**：

    * ``checked`` —— ``combo.currentIndex()``，用户真正选中的值。带蓝的
      ``panel_row_selected`` 底 + 右侧一支 accent 色的 ✓，弹出期间不随鼠标移动；
    * ``highlighted`` —— ``State_Selected`` / ``State_MouseOver``，Qt 会随鼠标
      ``view->setCurrentIndex()`` 改它，只是"光标在哪儿"。用更淡的中性色。

    之前拿 ``State_Selected`` 当"已选值"，高亮就跟着鼠标跑，鼠标一移开就认不出
    原本选的是哪项了。
    """

    def __init__(self, combo: Combo) -> None:
        super().__init__(combo)
        self._combo = combo

    def sizeHint(self, option: QStyleOptionViewItem,  # noqa: N802
                 index: QModelIndex) -> QSize:
        hint = super().sizeHint(option, index)
        return QSize(hint.width(), ITEM_H)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,  # noqa: N802
              index: QModelIndex) -> None:
        p = self._combo._p  # noqa: SLF001 - 同模块内的组件内部
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        r = QRectF(option.rect).adjusted(ITEM_INSET_X, 1, -ITEM_INSET_X, -1)

        # 当前值按 combo 的索引判定，**不看视图状态**：后者随鼠标/键盘的临时高亮变。
        # 项目里的下拉都是 flat list（addItems / setItemData），row 与 currentIndex 一一对应。
        checked = index.row() == self._combo.currentIndex()
        highlighted = bool(option.state
                           & (QStyle.State_Selected | QStyle.State_MouseOver))
        if checked:
            fill = theme.panel_row_selected(p)
        elif highlighted:
            fill = theme.popup_row_hover(p)
        else:
            fill = None
        if fill is not None:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(fill))
            painter.drawRoundedRect(r, ITEM_RADIUS, ITEM_RADIUS)

        enabled = bool(option.state & QStyle.State_Enabled)
        painter.setPen(QColor(p.text if enabled else p.text_mute))
        text = str(index.data(Qt.DisplayRole) or "")
        fm = QFontMetrics(option.font)
        # 勾槽对所有行都扣掉：文字的左右边界是同一条竖线，鼠标划过时不会忽长忽短
        box = r.adjusted(ITEM_PAD_X, 0, -(ITEM_PAD_X + CHECK_W), 0)
        elided = fm.elidedText(text, Qt.ElideRight, int(box.width()))
        painter.drawText(box, Qt.AlignLeft | Qt.AlignVCenter, elided)

        if checked:
            self._draw_check(painter, r, p.accent if enabled else p.text_mute)
        painter.restore()

    @staticmethod
    def _draw_check(painter: QPainter, r: QRectF, color: str) -> None:
        """画一支抗锯齿的 ✓。不走字体：中文黑体缺这个字形时会渲染成豆腐块。"""
        slot = QRectF(r.right() - ITEM_PAD_X - CHECK_W, r.top(),
                      CHECK_W, r.height())
        cx, cy = slot.center().x(), slot.center().y()
        x0 = cx - CHECK_MARK_W / 2
        path = QPainterPath()
        path.moveTo(QPointF(x0, cy - CHECK_MARK_H * 0.06))
        path.lineTo(QPointF(x0 + CHECK_MARK_W * 0.36, cy + CHECK_MARK_H * 0.44))
        path.lineTo(QPointF(cx + CHECK_MARK_W / 2, cy - CHECK_MARK_H * 0.46))
        painter.setPen(QPen(QColor(color), CHECK_PEN, Qt.SolidLine,
                            Qt.RoundCap, Qt.RoundJoin))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)


class Combo(QComboBox):
    """项目里所有下拉框都用它。用法同 ``QComboBox``，只是多要一个 palette。"""

    def __init__(self, palette: theme.Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._p = palette
        self._open = False
        self._popup: QFrame | None = None
        self._surface = _PopupSurface(palette, self)
        self.setItemDelegate(PopupDelegate(self))
        self.set_palette(palette)

    def set_palette(self, palette: theme.Palette) -> None:
        self._p = palette
        self.setStyleSheet(combo_qss(palette))
        self._surface.set_palette(palette)
        self.update()
        if self._popup is not None:
            self._popup.setStyleSheet(popup_qss(palette))
            self._popup.update()

    # -- 滚轮 ------------------------------------------------------------
    def wheelEvent(self, event) -> None:  # noqa: N802
        """不调 super：基类会按一格换一项。ignore 后事件冒泡给父级滚动区。"""
        event.ignore()

    # -- 弹出层 ----------------------------------------------------------
    def showPopup(self) -> None:  # noqa: N802
        super().showPopup()
        self._open = True
        self._style_popup()
        if self._popup is not None:
            self._widen_popup()          # 先按内容撑宽，再据此摆位
            self._place_below(self._popup)
        self.update()  # 箭头翻成 "^"

    def hidePopup(self) -> None:  # noqa: N802
        self._open = False
        super().hidePopup()
        self.update()

    def _style_popup(self) -> None:
        """接管 container 的窗口属性、绘制与背景。

        container 是懒创建的（第一次 ``showPopup`` 才有），所以不能在 ``__init__``
        里做。第一次拿到它时它**已经显示过了**，原生窗口已按不透明创建；必须
        走一遍 ``_make_layered`` 把它重建为带 alpha 的窗口，否则真机四角是黑块。
        之后切主题只换样式表、不再碰窗口。
        """
        view = self.view()
        cont = view.parentWidget() if view is not None else None
        if cont is None:
            return
        if self._popup is not cont:
            self._make_layered(cont)
            self._popup = cont
        cont.setStyleSheet(popup_qss(self._p))

    def _make_layered(self, cont: QFrame) -> None:
        """把 Qt 私有的 popup 窗口改成真正带 alpha 的分层窗口。

        ``QMenu`` 在构造时就设了 ``WA_TranslucentBackground``，Windows 平台于是
        给它 ``WS_EX_LAYERED`` + 8bit alpha；combo 的 container 是 show 之后才
        拿得到的，补属性只会改 Qt 侧的布尔值，不会回溯改原生窗口的像素格式。
        顺序很重要：先 hide 并设属性 → 把 QWindow 的 format 改成带 alpha →
        ``destroy()`` 扔掉旧 HWND → ``setWindowFlags`` 触发一次重建 → show。
        少一步都还是 ``alphaBufferSize == -1``（实测四种组合的对照记在
        ``scripts/verify_combo.py`` 的注释里）。
        """
        geo = cont.geometry()
        cont.hide()
        cont.setAttribute(Qt.WA_TranslucentBackground, True)
        wh = cont.windowHandle()
        if wh is not None:
            fmt = wh.format()
            fmt.setAlphaBufferSize(8)
            wh.setFormat(fmt)
            wh.destroy()
        cont.setWindowFlags(
            cont.windowFlags() | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint
        )
        # 绘制层也一起接管：eventFilter 返回 True 拦掉 QFrame 自己那层方底
        cont.installEventFilter(self._surface)
        cont.setGeometry(geo)
        cont.show()
        cont.raise_()

    def _popup_min_width(self) -> int:
        """弹出层要多宽才不被省略号吃掉。

        Qt 默认让弹出层和控件同宽（老式组合框的"影子"行为）。控件宽度是布局给
        的——编辑页一行四格，单位那格只有 80px，「毫秒」根本放不下，于是列表里
        变成「…」；音箱条上那格 88px，「不静默」变成「不…」。**控件和弹出层本来
        就该分开算**：控件窄是为了不挤邻居，弹出层是浮层，没人跟它抢地方。

        宽度 = 最长一项的文字 + 行内左右留白 + 右侧勾槽 + 竖向滚动条 + view 的
        内边距。滚动条那 14px 必须**先留出来**——不算它，条一出现文字区又缩回去，
        长选项照样省略。
        """
        view = self.view()
        fm = QFontMetrics(view.font() if view is not None else self.font())
        text = max((fm.horizontalAdvance(self.itemText(i)) for i in range(self.count())),
                   default=0)
        need = (text + 2 * ITEM_INSET_X + 2 * ITEM_PAD_X + CHECK_W
                + POPUP_BAR_W + 2 * VIEW_PAD)
        cap = self.width() + POPUP_MAX_EXTRA
        screen = (QApplication.screenAt(self.mapToGlobal(QPoint(0, 0)))
                  or QApplication.primaryScreen())
        if screen is not None:  # 别长到屏幕外面去
            cap = min(cap, screen.availableGeometry().width() - 16)
        return max(self.width(), min(need, cap))

    def _widen_popup(self) -> None:
        """把 container 和里面的 view 一起撑到 ``_popup_min_width()``。

        改 view 的 ``minimumWidth`` 不管用：container 的宽度是 Qt 在
        ``showPopup`` 里按 combo 宽度直接 ``setGeometry`` 的，不会回头问 view。
        而单纯 ``resize`` 也不够——view 的 ``minimumSizeHint`` 是按
        ``sizeHintForColumn``（最长一项）算的，container 的布局随后会把它顶回
        原尺寸，超长项时能一直撑到整屏宽。所以两处都用 ``setFixedWidth``
        **钉死**：宽度只能由这里决定。下次展开会重算一遍，缩回来也没问题。
        """
        cont, view = self._popup, self.view()
        if cont is None or view is None:
            return
        want = self._popup_min_width()
        view.setFixedWidth(want)
        cont.setFixedWidth(want)
        if cont.width() > want:  # 布局若仍不让步，至少别溢出
            cont.resize(want, cont.height())
        view.setGeometry(0, 0, want, cont.height())

    def _place_below(self, cont: QFrame) -> None:
        """把弹出层挪到控件下方、留一条缝，不要盖住控件。

        Qt 默认按"当前项对齐 combo"摆位（老式可编辑组合框的行为），列表会正好
        糊在控件上。改成右边缘对齐 + 下方 ``POPUP_GAP``；下方空间不够才翻到
        上方，最后再夹进屏幕可用区域。
        """
        tl = self.mapToGlobal(QPoint(0, 0))
        br = self.mapToGlobal(QPoint(self.width(), self.height()))
        screen = QApplication.screenAt(tl) or QApplication.primaryScreen()
        area = screen.availableGeometry() if screen is not None else None
        w, h = cont.width(), cont.height()
        x, y = br.x() - w, br.y() + POPUP_GAP
        if area is not None:
            # right() 是 inclusive，+1 才能贴到屏幕右边；这里不留余量，否则
            # 靠屏幕左侧的控件会被推得和弹出层错开几个像素，右边缘对不齐
            x = max(area.left(), min(x, area.right() - w + 1))
            if y + h > area.bottom() - 3:
                above = tl.y() - POPUP_GAP - h
                y = (above if above >= area.top() + 3
                     else max(area.top() + 3, area.bottom() - h - 3))
        cont.move(x, y)

    # -- 箭头 ------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)  # QSS 的底、文字、边框先画
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.isEnabled():
            color = QColor(self._p.text_mute)
        elif self.underMouse() or self._open:
            color = QColor(self._p.text)
        else:
            color = QColor(self._p.text_soft)
        p.setPen(QPen(color, ARROW_PEN, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)

        cx = self.width() - ARROW_RIGHT - ARROW_W / 2
        cy = self.height() / 2
        half = ARROW_W / 2
        # 收起 = v（中点在下），展开 = ^（中点在上）
        d = -ARROW_H / 2 if self._open else ARROW_H / 2
        path = QPainterPath()
        path.moveTo(QPointF(cx - half, cy - d))
        path.lineTo(QPointF(cx, cy + d))
        path.lineTo(QPointF(cx + half, cy - d))
        p.drawPath(path)
        p.end()

    @property
    def is_popup_open(self) -> bool:
        return self._open


__all__ = [
    "ARROW_H",
    "ARROW_PEN",
    "ARROW_RIGHT",
    "ARROW_W",
    "CHECK_W",
    "ITEM_H",
    "ITEM_INSET_X",
    "ITEM_PAD_X",
    "POPUP_BAR_W",
    "POPUP_GAP",
    "POPUP_MAX_EXTRA",
    "POPUP_RADIUS",
    "VIEW_PAD",
    "Combo",
    "PopupDelegate",
    "combo_qss",
    "popup_qss",
]
