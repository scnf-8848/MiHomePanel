# SPDX-License-Identifier: GPL-3.0-or-later
"""耗材监控项的配置弹窗：第一步只选设备，第二步才配这一条。

**两步走**（2026-11-18 用户定）：

1. ``item`` 为空 = 只选设备。选完在卡片墙上落一张"还没有属性"的空卡，别的一概
   不问；用户点那张空卡再进来配。把属性 / 阈值一起塞进第一步，等于让人在没看到
   卡片之前先做三个决定。
2. ``item`` 给了 = 配这一条。**属性一直可改**（2026-11-18 用户改的口径）：挑错了
   不该让人删了重来。保存时若属性变了，走 ``reconfigure_consumable`` 按旧 id 原地
   替换，卡片位置、名称、阈值都留着。

为什么不复用设备详情页：一条监控项 = 一个属性 + 两档阈值，和设备详情页（整台设备的
控件清单）不是一回事；同一台设备有两条耗材时，从详情页进还得先选是哪一条，多绕一步。
（卡片**画法**不在这条记录里：整页一个，在耗材页右上角的下拉里设。）

设备为什么用树而不是下拉：设备是"家庭 → 房间 → 设备"三层结构，几十台平铺进
下拉框只能一行行扫，同名设备还得靠拖在后面的房间名区分。控件和设置页的排序 /
隐藏那两棵是同一个（``ui/tree.py``），只是 ``draggable=False``——这里是选一行，
不是排序。空房间、没有可监控属性的设备直接不出现。

属性清单来自 ``DeviceSpec.properties`` 的 ``readable``（不是 ``card.advanced``：
那里只收"可写且卡片没用到的"，电量 / 滤芯这类只读量根本不在里面）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core import consumables as model
from ..core.consumables import Consumable
from ..core.spec import prop_labels
from ..core.store import Store
from . import theme
from .automation_style import tab_focus_only
from .chrome import SHADOW_PAD, TitleBar, WindowFrame, shadow_alpha, surface_bg, surface_border
from .combo import Combo
from .confirm import confirm
from .tree import ROLE_DATA, ReorderTree

#: 设备树的高度：7 行（家庭 / 房间 / 设备混排），再多就把弹窗推到屏幕下面去了
TREE_H = 224
FIELD_W = 300


def _qss(p: theme.Palette) -> str:
    return f"""
    QLabel#FieldLabel {{ color: {p.text_soft}; font-size: 12px; }}
    QLabel#Hint {{ color: {p.text_mute}; font-size: 11px; }}
    QLabel#Error {{ color: {theme.DANGER}; font-size: 11px; }}
    QLabel#Fixed {{ color: {p.text}; font-size: 13px; }}
    QLineEdit#Field {{
        background: {p.bg_soft}; color: {p.text}; border: 1px solid {p.border};
        border-radius: 8px; padding: 6px 9px; font-size: 13px;
        selection-background-color: {p.accent}; selection-color: #FFFFFF;
    }}
    QLineEdit#Field:focus {{ border-color: {p.accent}; }}
    QPushButton#Primary {{
        background: {theme.ACCENT_GREEN}; color: #FFFFFF; border: none;
        border-radius: 9px; padding: 0 16px; min-height: 32px;
        font-size: 13px; font-weight: 600;
    }}
    QPushButton#Primary:hover {{ background: {theme.blend(theme.ACCENT_GREEN, "#FFFFFF", 0.12)}; }}
    QPushButton#Primary:disabled {{
        background: {theme.blend(p.bg, p.text, 0.10)}; color: {p.text_mute};
    }}
    QPushButton#Ghost {{
        background: transparent; color: {p.text_soft}; border: 1px solid {p.border};
        border-radius: 9px; padding: 0 16px; min-height: 32px; font-size: 13px;
    }}
    QPushButton#Ghost:hover {{ color: {p.text}; border-color: {theme.blend(p.border, p.text, 0.18)}; }}
    QPushButton#Danger {{
        background: transparent; color: {theme.DANGER}; border: 1px solid transparent;
        border-radius: 9px; padding: 0 12px; min-height: 32px; font-size: 13px;
    }}
    QPushButton#Danger:hover {{ border-color: {theme.DANGER}; }}
    QPushButton#Seg {{
        background: transparent; color: {p.text_soft}; border: 1px solid {p.border};
        border-radius: 8px; padding: 0 14px; min-height: 28px; font-size: 13px;
    }}
    QPushButton#Seg:hover {{ color: {p.text}; }}
    QPushButton#Seg:checked {{
        background: {theme.blend(p.bg, p.accent, 0.12)}; color: {p.accent};
        border-color: {p.accent}; font-weight: 600;
    }}
    """


def _numeric_props(state) -> list:
    """这台设备**能比大小**的可读属性：数值 / 有值域的才算。

    枚举（充足 / 将尽 / 耗尽）、布尔、文本一律不收：两个数字阈值套不到它们身上，
    收进来只会让人配出一个永远不触发的提醒。
    """
    if state is None or state.spec is None:
        return []
    out = []
    for prop in state.spec.properties:
        if not prop.readable:
            continue
        if prop.kind not in ("number", "range"):
            continue
        out.append(prop)
    return out


class ConsumableDialog(QDialog):
    """``item`` 为空 = 第一步（只选设备）；给了条目 = 配这一条。"""

    def __init__(
        self,
        store: Store,
        palette: theme.Palette,
        *,
        item: Consumable | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self._p = palette
        self._item = item
        #: 第一步选中的设备；点「下一步」时填，取消时为空
        self.selected_did = ""
        #: 保存成功后的那条（空壳配好属性也算），取消或删除时为 ``None``
        self.added: Consumable | None = None
        #: 用户点了删除
        self.deleted = False
        #: 窗已经关掉。取数回调可能晚到，那时再碰控件就是访问已销毁对象
        self._closed = False

        if item is None:
            self.setWindowTitle("添加设备")
        elif item.is_configured:
            self.setWindowTitle("监控项设置")
        else:
            self.setWindowTitle("设置监控项")

        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self._frame = WindowFrame(
            self, resizable=False, radius=theme.RADIUS_CARD, shadow=SHADOW_PAD,
            shadow_alpha=shadow_alpha(palette),
            fill=lambda: surface_bg(palette),
            border=lambda: surface_border(palette),
        )
        self.setStyleSheet(_qss(palette))

        outer = QVBoxLayout(self._frame.body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # 和详情页同一个坑（见 docs/dev/ui.md「详情页」）：``SetDefaultConstraint`` 会
        # 把窗口最小高钉在第一次 activate 时的值上，之后再 show/hide 行也长不回来——
        # 枚举耗材只显示一行「不足档位」、数值耗材显示两行阈值，切属性时窗口要跟着变。
        outer.setSizeConstraint(QLayout.SetNoConstraint)
        outer.addWidget(TitleBar(self.windowTitle(), palette, buttons=("close",),
                                 variant="dialog", frame=self._frame))

        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(24, 18, 24, 18)
        col.setSpacing(12)
        outer.addWidget(body)

        #: did → 设备行。点家庭 / 房间行时要靠它把选中态挪回去
        self._device_items: dict[str, QTreeWidgetItem] = {}
        self._selected_did = ""
        self.tree_device: ReorderTree | None = None
        self.cb_prop: Combo | None = None
        self.ed_name: QLineEdit | None = None
        self.ed_warn: QLineEdit | None = None
        self.ed_crit: QLineEdit | None = None
        self.btn_save: QPushButton | None = None
        self.lbl_err: QLabel | None = None
        #: 「米家耗材」tab 的行数据与当前选中项（``_cloud_rows[index]``）
        self._cloud_rows: list[model.CloudItem] = []
        self._cloud_index = -1
        self._cloud_note: QLabel | None = None
        #: 枚举项的"不足档"下拉（洗衣液这类），数值项没有这个控件
        self.cb_low: Combo | None = None
        self._low_row: QHBoxLayout | None = None

        if item is None:
            self._build_picker(body, col)
        else:
            self._build_form(body, col)

        self._fit()
        tab_focus_only(self)

    # -- 第一步：选设备 ---------------------------------------------------
    #
    # 两个 tab 各走各的路（2026-11-24 用户定，推翻了上一版"在自选设备的表单里塞
    # 一条云端候选"的做法）：
    #
    # * 「米家耗材」——直接列 ``/v2/home/standard_consumable_items`` 里云端认为
    #   有的耗材，点一条就落卡。名字用云端的（「5号电池」「大电池」「洗衣液」），
    #   规格里两块电池都叫「电池电量」，靠属性名根本分不出来。
    # * 「自选设备」——原来那条路，一个字都没动：选设备 → 落空卡 → 点空卡配属性
    #   和阈值。云端数据**不参与**这里，用户自己挑的就是自己挑的。
    #
    # 为什么必须分开：上一版把云端名字套在自选属性上，于是"洗衣液"配到了
    # 「漂洗次数」、"大电池"配到了小电池的属性——名字对了、数据是错的。两条路
    # 各自完整，比勉强合并成一条少一半歧义。
    def _build_picker(self, body: QWidget, col: QVBoxLayout) -> None:
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self._tab_group = QButtonGroup(self)
        self._tab_group.setExclusive(True)
        self.btn_tab_cloud = self._seg_button("米家耗材", body)
        self.btn_tab_self = self._seg_button("自选设备", body)
        for index, btn in enumerate((self.btn_tab_cloud, self.btn_tab_self)):
            self._tab_group.addButton(btn, index)
            bar.addWidget(btn)
        bar.addStretch(1)
        col.addLayout(bar)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_cloud_page(body))
        self.stack.addWidget(self._build_self_page(body))
        col.addWidget(self.stack)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch(1)
        cancel = QPushButton("取消", body)
        cancel.setObjectName("Ghost")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        # 两个 tab 共用一个位置：自选是"下一步"，米家耗材是"添加"。
        # 都建出来再显隐，切换时窗口宽度不会因为按钮字数变来变去
        self.btn_add = QPushButton("添加到耗材页", body)
        self.btn_add.setObjectName("Primary")
        self.btn_add.setCursor(Qt.PointingHandCursor)
        self.btn_add.setDefault(True)
        self.btn_add.setEnabled(False)
        self.btn_add.clicked.connect(self._on_add_cloud)
        row.addWidget(self.btn_add)
        self.btn_next = QPushButton("下一步", body)
        self.btn_next.setObjectName("Primary")
        self.btn_next.setCursor(Qt.PointingHandCursor)
        self.btn_next.setDefault(True)
        self.btn_next.setEnabled(bool(self._device_items))
        self.btn_next.clicked.connect(self._on_next)
        row.addWidget(self.btn_next)
        col.addLayout(row)

        self._tab_group.idClicked.connect(self._on_tab)
        self.btn_tab_cloud.setChecked(True)
        self._on_tab(0)

    @staticmethod
    def _seg_button(text: str, parent: QWidget) -> QPushButton:
        btn = QPushButton(text, parent)
        btn.setObjectName("Seg")
        btn.setCheckable(True)
        btn.setCursor(Qt.PointingHandCursor)
        return btn

    def _on_tab(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.btn_next.setVisible(index == 1)
        self.btn_add.setVisible(index == 0)
        self._fit()

    def _fit(self) -> None:
        """按当前内容重算窗宽高。两个 tab / 数值与档位两种表单高度不同。

        **不能读 ``body.sizeHint()`` 也不能读布局的 ``sizeHint()``**：``body`` 被
        ``WindowFrame`` 用 ``setGeometry`` 钉住，布局报的是"当前窗口能给的"，不是内容
        要的（实测内容 315 它报 269，结果把「不足档位」那一行压成一条缝）。这里逐项
        累加可见行自己的 hint——隐藏的阈值行跳过，TitleBar 的 sizeHint 是 (-1,-1)，
        改用它的固定高度。
        """
        body = self._frame.body
        lay = body.layout()
        if lay is None:
            return
        lay.invalidate()
        height = width = 0
        for i in range(lay.count()):
            widget = lay.itemAt(i).widget()
            if widget is None or widget.isHidden():
                continue
            hint = widget.sizeHint()
            height += hint.height() if hint.height() >= 0 else widget.height()
            width = max(width, hint.width())
        margin = lay.contentsMargins()
        height += margin.top() + margin.bottom() + lay.spacing() * max(0, lay.count() - 1)
        width += margin.left() + margin.right()
        pad2 = 2 * SHADOW_PAD
        self.resize(max(460, width) + pad2, height + pad2)

    def _build_self_page(self, body: QWidget) -> QWidget:
        page = QWidget(body)
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)

        self.tree_device = ReorderTree(self._p, draggable=False)
        self.tree_device.setMinimumWidth(400)
        self.tree_device.setFixedHeight(TREE_H)
        self._fill_devices()
        self.tree_device.itemClicked.connect(self._on_device_clicked)
        self.tree_device.currentItemChanged.connect(self._on_device_current)
        box.addWidget(self.tree_device)

        tip = QLabel(
            "选一台设备，墙上会先落一张空卡；点那张卡再挑属性和阈值。"
            "按家庭列出所有含可监控数值属性的设备，离线设备也能选。",
            page,
        )
        tip.setObjectName("Hint")
        tip.setWordWrap(True)
        box.addWidget(tip)

        self._select_first_device()
        return page

    def _build_cloud_page(self, body: QWidget) -> QWidget:
        page = QWidget(body)
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)

        self.tree_cloud = ReorderTree(self._p, draggable=False)
        self.tree_cloud.setMinimumWidth(400)
        self.tree_cloud.setFixedHeight(TREE_H)
        self.tree_cloud.itemClicked.connect(self._on_cloud_clicked)
        box.addWidget(self.tree_cloud)

        self._cloud_note = QLabel("读取中…", page)
        self._cloud_note.setObjectName("Hint")
        self._cloud_note.setWordWrap(True)
        box.addWidget(self._cloud_note)

        # 打开就读一次，不缓存：电池几天才动一个百分点，缓存只会在配完回看时给旧值
        self.store.fetch_consumable_items(self._on_cloud_loaded)
        return page

    def _on_cloud_loaded(
        self,
        rows: dict[str, list[model.CloudItem]] | None,
        error: BaseException | None,
    ) -> None:
        """云端台账 → 列表。一条耗材一行，设备名做父节点。"""
        if self._closed:
            return
        tree = self.tree_cloud
        tree.clear()
        self._cloud_rows = []
        if error is not None:
            self._cloud_note.setText("读取失败。可以切到「自选设备」手动添加。")
            return
        groups = rows or {}
        if not groups:
            self._cloud_note.setText("云端没有登记任何耗材。")
            return
        # 已经在耗材页上的不再列：点了也只会撞"这个属性已经有一条了"
        taken = {(c.did, c.siid, c.piid) for c in self.store.prefs.consumables}
        skipped = 0     # 云端有记录、但 prop 拆不出可读属性（老门锁那两条）
        for did, items in groups.items():
            st = self.store.devices.get(did)
            if st is None:
                continue
            usable = [c for c in items if c.readable]
            skipped += len(items) - len(usable)
            usable = [c for c in usable if (c.did, c.siid, c.piid) not in taken]
            if not usable:
                continue
            parent = tree.add_row(
                None, st.name, {"kind": "cloud-device"},
                suffix=f"{st.room} · {len(usable)} 项" if st.room else f"{len(usable)} 项",
            )
            for cand in usable:
                tree.add_row(
                    parent, cand.summary(),
                    {"kind": "cloud-item", "index": len(self._cloud_rows)},
                )
                self._cloud_rows.append(cand)
            parent.setExpanded(True)
        if not self._cloud_rows:
            if skipped:
                # 一条都落不了卡，但云端确实认为有耗材：把原因说清楚，别写成"已经
                # 都在清单里"（那是另一件事）
                self._cloud_note.setText(
                    f"云端登记了 {skipped} 项耗材，但读不到对应的设备属性，"
                    "请切到「自选设备」手工选。"
                )
            else:
                self._cloud_note.setText(
                    "云端的耗材都已经在清单里了。要加别的，切到「自选设备」。"
                )
            return
        note = "这些名字来自米家 App 的耗材页，直接按云端登记的名称添加。"
        if skipped:
            # 不静默少几条：那台设备的耗材在 App 里有，只是 prop 不是 siid.piid，
            # 猜错会指到另一个量上，所以宁可说清楚
            note += f"另有 {skipped} 项云端有记录但读不到对应属性，请用「自选设备」手工选。"
        self._cloud_note.setText(note)

    def _on_cloud_clicked(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        data = item.data(0, ROLE_DATA) or {}
        if data.get("kind") != "cloud-item":
            if item.childCount():
                item.setExpanded(not item.isExpanded())
            return
        self._cloud_index = int(data.get("index") or 0)
        self.btn_add.setEnabled(True)

    def _on_add_cloud(self) -> None:
        rows = getattr(self, "_cloud_rows", [])
        if not (0 <= self._cloud_index < len(rows)):
            self.btn_add.setEnabled(False)
            return
        cand = rows[self._cloud_index]
        item = cand.consumable()
        if not self.store.prefs.add_consumable(item):
            self._cloud_note.setText("这条已经在清单里了。")
            return
        self.added = item
        self.accept()

    def _on_next(self) -> None:
        if not self._selected_did:
            return
        self.selected_did = self._selected_did
        self.accept()

    def _fill_devices(self) -> None:
        """家庭 → 房间 → 设备，**全部家庭**（2026-11-21 起，和桌面挂件的选设备树一致）。

        为什么不再只列当前家庭：耗材页本身已经跨家庭统计了（见
        ``consumables_page._rows_from_store``）。树若还锁在当前家庭，就会出现
        "页面显示两个家的耗材，却只能往当前这家加"的不对称。

        空家庭、空房间、没有数值属性的设备都不进树；一个都没有时给一句空态，
        不留一片空白。家庭节点默认展开（一共就两三个家，不需要先点一下）。
        """
        tree = self.tree_device
        # 先清映射再清树：clear() 会发 currentItemChanged(None)，
        # 那时字典里若还剩已删除的 item，回调会拿它去 setCurrentItem 而崩
        self._device_items.clear()
        tree.clear()
        for home in self.store.homes():
            home_id = home.id
            rooms: list[tuple[QTreeWidgetItem, list]] = []
            for room_id, room_name in self.store.room_entries(home_id):
                states = [
                    st for st in self.store.device_states(home_id, room_id)
                    if _numeric_props(st)
                ]
                if states:
                    rooms.append((room_id, room_name, states))
            if not rooms:
                continue
            h_item = tree.add_row(
                None, self.store.home_label(home_id),
                {"kind": "home", "home": home_id, "room": "", "id": home_id},
                suffix=f"{sum(len(s) for _, _, s in rooms)} 台",
            )
            for room_id, room_name, states in rooms:
                r_item = tree.add_row(
                    h_item, room_name,
                    {"kind": "room", "home": home_id, "room": room_id},
                    suffix=f"{len(states)} 台",
                )
                for st in states:
                    item = tree.add_row(
                        r_item, st.name,
                        {"kind": "device", "home": home_id, "room": room_id, "id": st.did},
                        suffix="" if st.online else "已离线",
                    )
                    self._device_items[st.did] = item
        if not self._device_items:
            tree.add_row(None, "没有可监控的设备", {"kind": "empty"})
        tree.restore_expanded(None)

    def _select_first_device(self) -> None:
        first = next(iter(self._device_items), "")
        if first:
            self._select_device(first)

    def _select_device(self, did: str) -> None:
        item = self._device_items.get(did)
        if item is None:
            return
        self._selected_did = did
        self.tree_device.setCurrentItem(item)

    def _restore_selection(self) -> None:
        item = self._device_items.get(self._selected_did)
        if item is not None:
            self.tree_device.setCurrentItem(item)

    def _on_device_clicked(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        data = item.data(0, ROLE_DATA) or {}
        if data.get("kind") == "device":
            self._select_device(str(data.get("id") or ""))
            return
        # 点家庭 / 房间行只折角，不改已选设备：否则选好的设备会被这一下清掉
        if item.childCount():
            item.setExpanded(not item.isExpanded())
        self._restore_selection()

    def _on_device_current(self, current, _previous) -> None:
        # 点树里的空白处会把 current 清空（选中块随之消失），把已选设备选回来
        if current is None and self._device_items:
            self._restore_selection()

    # -- 第二步：配这一条 -------------------------------------------------
    def _build_form(self, body: QWidget, col: QVBoxLayout) -> None:
        item = self._item
        state = self.store.devices.get(item.did)
        if state is None:
            name = f"{item.did}（设备已不存在）"
        elif state.room:
            name = f"{state.name} · {state.room}"
        else:
            name = state.name
        col.addLayout(self._row("设备", self._fixed(name)))

        # 属性一律给下拉，配好之后也还能改：门锁那种「两块电池都叫电量」的设备，
        # 第一次多半是猜的，锁死等于让人删了重来
        self.cb_prop = Combo(self._p)
        self.cb_prop.setFixedWidth(FIELD_W)
        self.cb_prop.currentIndexChanged.connect(self._sync_value_rows)
        col.addLayout(self._row("属性", self.cb_prop))
        current = (item.siid, item.piid) if item.is_configured else None
        self._fill_props(item.did, current)

        self.ed_name = QLineEdit()
        self.ed_name.setObjectName("Field")
        self.ed_name.setFixedWidth(FIELD_W)
        self.ed_name.setPlaceholderText("留空就用属性名")
        col.addLayout(self._row("名称", self.ed_name))

        # 卡片样式不在这里：整页一个，在耗材页右上角的下拉里统一设（2026-11-18 用户定）
        #
        # 判档有两套：数值属性用两个百分比阈值，**枚举属性（洗衣液）只有档位**。
        # 两套控件都建出来，按当前属性显隐——换属性时窗口高度跟着变（见 ``_fit``）。
        self.ed_warn = self._ed_pct("20")
        self._warn_widget = self._wrap(
            col, self._row("轻提醒（橙）", self.ed_warn, tail=self._pct())
        )
        self.ed_crit = self._ed_pct("10")
        self._crit_widget = self._wrap(
            col, self._row("重提醒（红）", self.ed_crit, tail=self._pct())
        )

        self.cb_low = Combo(self._p)
        self.cb_low.setFixedWidth(120)
        self._low_widget = self._wrap(col, self._row("不足档位", self.cb_low))
        self._low_widget.hide()

        self._hint_num = QLabel(
            "余量降到阈值（含等于）时变色，并计入侧栏圆点。留空或填 0 = 不启用这一档。",
            body,
        )
        self._hint_num.setObjectName("Hint")
        self._hint_num.setWordWrap(True)
        col.addWidget(self._hint_num)

        self._hint_enum = QLabel(
            "这条属性只有档位、没有百分比。读到选中的那一档时变色，并计入侧栏圆点。",
            body,
        )
        self._hint_enum.setObjectName("Hint")
        self._hint_enum.setWordWrap(True)
        self._hint_enum.hide()
        col.addWidget(self._hint_enum)

        # 只在"这个属性已经有一条了"时出现：那种情况保存会静默失败，
        # 不说一句话的话用户只会觉得按钮坏了
        self.lbl_err = QLabel("这台设备的这个属性已经有一条了，换一个属性，或先删掉那条。", body)
        self.lbl_err.setObjectName("Error")
        self.lbl_err.setWordWrap(True)
        self.lbl_err.hide()
        col.addWidget(self.lbl_err)

        self.ed_name.setText(item.label)
        # 空壳的两个阈值是关着的，但这里要给个能直接用的默认值：用户是来配的，
        # 空白输入框会让人以为必须自己算一个数
        warn = item.warn if item.warn is not None else model.WARN_DEFAULT
        crit = item.crit if item.crit is not None else model.CRIT_DEFAULT
        self.ed_warn.setText(str(warn))
        self.ed_crit.setText(str(crit))
        # 档位项的默认选中值：落盘过的 low 优先，否则取云端给的那档
        self._sync_value_rows()

        row = QHBoxLayout()
        row.setSpacing(10)
        danger = QPushButton("删除", body)
        danger.setObjectName("Danger")
        danger.setCursor(Qt.PointingHandCursor)
        danger.clicked.connect(self._on_delete)
        row.addWidget(danger)
        row.addStretch(1)
        cancel = QPushButton("取消", body)
        cancel.setObjectName("Ghost")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        self.btn_save = QPushButton("保存", body)
        self.btn_save.setObjectName("Primary")
        self.btn_save.setCursor(Qt.PointingHandCursor)
        self.btn_save.setDefault(True)
        self.btn_save.clicked.connect(self._on_save)
        row.addWidget(self.btn_save)
        col.addLayout(row)

    # -- 构件 ------------------------------------------------------------
    @staticmethod
    def _fixed(text: str) -> QLabel:
        lab = QLabel(text)
        lab.setObjectName("Fixed")
        return lab

    @staticmethod
    def _pct() -> QLabel:
        lab = QLabel("%")
        lab.setObjectName("FieldLabel")
        return lab

    @staticmethod
    def _ed_pct(placeholder: str) -> QLineEdit:
        ed = QLineEdit()
        ed.setObjectName("Field")
        ed.setFixedWidth(88)
        ed.setPlaceholderText(placeholder)
        return ed

    @staticmethod
    def _wrap(col: QVBoxLayout, row: QHBoxLayout) -> QWidget:
        """把一行布局包成独立 widget：``QLayout`` 自己没有 show/hide。"""
        box = QWidget()
        box.setLayout(row)
        col.addWidget(box)
        return box

    def _current_prop(self):
        """属性下拉当前选中的那个 ``PropertySpec``（拿不到返回 ``None``）。"""
        if self.cb_prop is None or self._item is None:
            return None
        key = self.cb_prop.currentData()
        state = self.store.devices.get(self._item.did)
        if key is None or state is None or state.spec is None:
            return None
        return next((p for p in state.spec.properties if p.key == key), None)

    def _sync_value_rows(self, *_args) -> None:
        """按属性类型显隐判档控件：数值 → 两档百分比；枚举 → 「不足档位」。

        洗衣液（``detergent-left-level``，1=低 / 2=标准）没有百分比，两档阈值套在它
        身上只会得到"变红的门槛比变橙还高"这种永远不触发的东西。
        """
        prop = self._current_prop()
        is_enum = prop is not None and prop.kind == "enum"
        for widget in (getattr(self, "_warn_widget", None),
                       getattr(self, "_crit_widget", None)):
            if widget is not None:
                widget.setVisible(not is_enum)
        if getattr(self, "_low_widget", None) is not None:
            self._low_widget.setVisible(is_enum)
        if getattr(self, "_hint_num", None) is not None:
            self._hint_num.setVisible(not is_enum)
        if getattr(self, "_hint_enum", None) is not None:
            self._hint_enum.setVisible(is_enum)
        if is_enum:
            self._fill_low(prop)
        if getattr(self, "_frame", None) is not None:
            self._fit()

    def _fill_low(self, prop) -> None:
        """档位下拉：列 ``value_list`` 的短名，默认选中落盘过的 ``low``。"""
        if self.cb_low is None:
            return
        want = self._item.low if self._item is not None else None
        self.cb_low.blockSignals(True)
        self.cb_low.clear()
        for opt in prop.value_list:
            self.cb_low.addItem(opt.display_short, opt.value)
        index = next(
            (i for i in range(self.cb_low.count()) if self.cb_low.itemData(i) == want), -1
        )
        self.cb_low.setCurrentIndex(index if index >= 0 else 0)
        self.cb_low.blockSignals(False)

    @staticmethod
    def _row(label: str, widget, *, tail: QWidget | None = None,
             top: bool = False) -> QHBoxLayout:
        """一排「标签 + 控件」。``widget`` 也可以是布局（设备那排要竖着叠树和提示）。"""
        row = QHBoxLayout()
        row.setSpacing(10)
        title = QLabel(label)
        title.setObjectName("FieldLabel")
        title.setFixedWidth(96)
        row.addWidget(title, 0, Qt.AlignTop if top else Qt.AlignVCenter)
        if isinstance(widget, QLayout):
            row.addLayout(widget, 0)
        else:
            row.addWidget(widget, 0, Qt.AlignTop if top else Qt.AlignVCenter)
        if tail is not None:
            row.addWidget(tail, 0, Qt.AlignTop if top else Qt.AlignVCenter)
        row.addStretch(1)
        return row

    def _fill_props(self, did: str, selected: tuple[int, int] | None = None) -> None:
        """把这台设备能比的属性填进下拉，并选中 ``selected``（有的话）。

        光看属性名会撞车：小米门锁 4 Pro 有两块电池，属性名都是 ``battery-level``
        → 都显示成「电池电量（%）」，选错了也看不出错在哪。同名的那几个补一句服务
        注释（「· 锂电池」/「· 干电池」）；服务注释也撞（或压根没有）才退回
        ``siid.piid``——规则本身在 ``core/spec.py`` 的 ``prop_labels``，
        和卡片墙上的名字同一处出处。
        """
        if self.cb_prop is None:
            return
        self.cb_prop.clear()
        state = self.store.devices.get(did)
        props = _numeric_props(state)
        all_props = state.spec.properties if state is not None and state.spec else []
        if selected is not None:
            current = next((p for p in all_props if p.key == selected), None)
            if current is not None and current not in props:
                # 枚举耗材（洗衣液）不在"能比大小"的清单里，但它就是当前这条属性：
                # 不列出来，用户点进来只会看到「规格里已没有这个属性」。
                props = [*props, current]
        labels = prop_labels(all_props)
        for prop in props:
            text = labels.get(prop.key, prop.display_label)
            if prop.unit_text:
                text = f"{text}（{prop.unit_text}）"
            self.cb_prop.addItem(text, prop.key)

        keys = [prop.key for prop in props]
        if selected is not None and selected not in keys:
            # 规格更新过 / 设备不在了：旧属性已读不到。**别静默换成第一个**，
            # 那会在用户没动的情况下把监控项指向另一个量
            self.cb_prop.addItem(self._stale_text(state, selected), selected)
            keys.append(selected)
        if not keys:
            self.cb_prop.addItem("这台设备没有可监控的数值属性", None)
            return
        for index in range(self.cb_prop.count()):
            if self.cb_prop.itemData(index) == selected:
                self.cb_prop.setCurrentIndex(index)
                return
        self.cb_prop.setCurrentIndex(0)

    @staticmethod
    def _stale_text(state, key: tuple[int, int]) -> str:
        """旧属性已经不在可选清单里（规格更新 / 变成不可读）：尽量把名字写出来。"""
        if state is not None and state.spec is not None:
            for prop in state.spec.properties:
                if prop.key == key:
                    return f"{prop.display_label}（{prop.method}，当前不可读）"
        return f"{key[0]}.{key[1]}（规格里已没有这个属性）"

    # -- 动作 ------------------------------------------------------------
    @staticmethod
    def _parse_threshold(edit: QLineEdit) -> int | None:
        text = edit.text().strip()
        if not text:
            return None
        try:
            number = int(text)
        except ValueError:
            return None
        if number <= 0:
            return None
        return min(number, model.THRESHOLD_MAX)

    def _build(self) -> Consumable | None:
        if self._item is None:
            return None
        key = self.cb_prop.currentData() if self.cb_prop is not None else None
        if not key:
            return None
        siid, piid = key
        prop = self._current_prop()
        is_enum = prop is not None and prop.kind == "enum"
        low = None
        if is_enum and self.cb_low is not None:
            low = self.cb_low.currentData()
        return model.parse(
            {
                "did": self._item.did,
                "siid": siid,
                "piid": piid,
                "label": self.ed_name.text() if self.ed_name is not None else "",
                # 枚举项只存"不足档"，两档百分比留空（见 ``model.parse``）
                "warn": None if is_enum else self._parse_threshold(self.ed_warn),
                "crit": None if is_enum else self._parse_threshold(self.ed_crit),
                "low": low,
            }
        )

    def _show_error(self, text: str) -> None:
        if self.lbl_err is None:
            return
        self.lbl_err.setText(text)
        self.lbl_err.show()

    def _on_save(self) -> None:
        item = self._build()
        if item is None:
            self._show_error("先选一个属性。")
            return
        if self.lbl_err is not None:
            self.lbl_err.hide()
        if item.id == self._item.id:
            # 改完和原来一模一样时 update 返回 False——那不是失败，只是想关窗
            self.store.prefs.update_consumable(item)
        else:
            # 属性变了（含空壳第一次配）：id 跟着变，只能按旧 id 原地替换，
            # 卡片位置不动。新属性已被另一条占着时留在窗里报错，而不是顶掉别人
            if not self.store.prefs.reconfigure_consumable(self._item.id, item):
                self._show_error("这台设备的这个属性已经有一条了，换一个属性，或先删掉那条。")
                return
        self.added = item
        self.accept()

    def _on_delete(self) -> None:
        if self._item is None:
            return
        if not confirm(
            self,
            "删除这条监控项？",
            "只是不再监控这个属性，设备本身和它的卡片不受影响。",
            self._p,
            ok_text="删除",
            danger=True,
        ):
            return
        self.store.prefs.remove_consumable(self._item.id)
        self.deleted = True
        self.accept()

    def done(self, result: int) -> None:
        """保存 / 取消 / Esc / 关闭都走这里：先立旗，晚到的取数回调就不会再碰控件。"""
        self._closed = True
        super().done(result)
