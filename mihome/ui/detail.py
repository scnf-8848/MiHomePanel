"""设备详情页。

首页卡片只放高频操作，低频的全部收进这里——这是米家 UED 明确要求的分层，
也是参考项目最大的毛病：它把规格里所有可写属性一次性铺在同一个面板上。

这里按 ``CardSpec`` 的三层结构排：

* ``controls`` —— 主控件（开关、亮度、色温、温度、风速…）
* ``metrics`` —— 只读读数（功率、电量、PM2.5…）
* ``advanced`` —— 其余可写属性，折叠在"更多功能"里
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..core.camera import CameraError, is_camera
from ..core.camera import shared as camera_service
from ..core.catalog import format_value
from ..core.prefs import HIDDEN_ROOM
from ..core.spec import PropertySpec
from ..core.store import DeviceState, Store
from . import theme, tooltip
from .camera_view import CameraView
from .chrome import TitleBar, WindowFrame, surface_bg, surface_border
from .combo import Combo
from .widgets import ElidedLabel, IconProvider, ToggleSwitch, draw_text

PAD = 22


class ControlRow(QWidget):
    """一行"标签 + 右侧控件"。"""

    changed = Signal(int, int, object)  # siid, piid, value

    def __init__(
        self,
        prop: PropertySpec,
        label: str,
        palette: theme.Palette,
        accent: str,
        role: str,
        *,
        write: bool = True,
    ) -> None:
        super().__init__()
        self._p = palette
        self.prop = prop
        self._role = role
        self._write = write
        self.setFixedHeight(52)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)

        self.name = ElidedLabel(label)
        self.name.setStyleSheet(f"color:{palette.text}; font-size:14px;")
        row.addWidget(self.name, 1)

        self.editor: QWidget | None = None
        if role == "switch" or role == "toggle":
            sw = ToggleSwitch(palette=palette, accent=accent, size="md")
            sw.setEnabled(write)
            sw.toggled.connect(lambda v: self.changed.emit(prop.siid, prop.piid, v))
            self.editor = sw
        elif role == "select" and prop.value_list:
            # 下拉框走全局 Combo：箭头、弹出层圆角、禁滚轮都和设置页一致，
            # 样式表也在那一处维护，这里不再各自写一份（曾经常年漏改深色）。
            box = Combo(palette)
            # 枚举项的 comment 常常是一整句中文（"空场景（灯不在任何情况模式时…）"），
            # 按内容撑宽会顶出卡片，所以固定按 10 个字宽度算，多余部分由控件自己截。
            box.setMinimumContentsLength(10)
            box.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            box.setMaximumWidth(240)
            for o in prop.value_list:
                # comment 常常是整句中文，在 200px 宽的下拉框里会被硬切；截短并把
                # 完整说明放进 tooltip
                label = o.display if len(o.display) <= 14 else o.display[:13] + "…"
                box.addItem(label, o.value)
                box.setItemData(box.count() - 1, o.display, Qt.ToolTipRole)
            box.setEnabled(write)
            box.currentIndexChanged.connect(
                lambda i: self.changed.emit(prop.siid, prop.piid, box.itemData(i))
            )
            self.editor = box
        elif role in ("slider", "stepper") and prop.value_range:
            self.editor = self._build_slider(prop, palette, accent, write)
        elif role == "readout":
            lab = QLabel("—")
            lab.setStyleSheet(f"color:{palette.text}; font-size:15px; font-weight:600;")
            lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.readout = lab
            self.editor = lab

        if self.editor is not None:
            row.addWidget(self.editor, 0, Qt.AlignRight)

    def _build_slider(self, prop: PropertySpec, palette: theme.Palette, accent: str, write: bool) -> QWidget:
        lo, hi, step = prop.value_range or (0, 100, 1)
        lo, hi = int(lo), int(hi)
        # 有些属性是 uint32 全域（0..4294967295），QSlider 只吃 int32，直接 setRange 会抛
        # OverflowError。这里夹到 int32 范围内，滑块仍然可用（只是精度差一点）。
        hi = min(hi, 2**31 - 1)
        lo = max(lo, -(2**31))
        if hi <= lo:
            hi = lo + 1
        step = max(1, int(step))
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.value_label = QLabel("—")
        self.value_label.setStyleSheet(f"color:{palette.text_soft}; font-size:13px; min-width:52px;")
        self.value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        slider = QSlider(Qt.Horizontal)
        slider.setRange(lo, hi)
        slider.setSingleStep(step)
        slider.setFixedWidth(170)
        slider.setEnabled(write)
        slider.setStyleSheet(
            f"QSlider::groove:horizontal{{height:5px;background:{palette.bg_soft};border-radius:3px;}}"
            f"QSlider::sub-page:horizontal{{background:{accent};border-radius:3px;}}"
            f"QSlider::handle:horizontal{{width:16px;height:16px;margin:-6px 0;border-radius:8px;"
            f"background:#FFFFFF;border:1px solid {palette.border};}}"
            f"QSlider::sub-page:horizontal:disabled{{background:{palette.text_mute};}}"
            f"QSlider::handle:horizontal:disabled{{background:{palette.bg_soft};"
            f"border:1px solid {palette.border};}}"
        )
        slider.valueChanged.connect(lambda v: self.value_label.setText(f"{v}{prop.unit_text if prop.unit == 'percentage' else ''}"))
        slider.sliderReleased.connect(lambda: self.changed.emit(prop.siid, prop.piid, slider.value()))
        self.slider = slider
        lay.addWidget(self.value_label)
        lay.addWidget(slider)
        return wrap

    def set_enabled(self, online: bool) -> None:
        """离线时控件不可操作；只读读数（QLabel）不受影响。"""
        self.name.setStyleSheet(
            f"color:{self._p.text if online else self._p.text_mute}; font-size:14px;"
        )
        if self.editor is None or isinstance(self.editor, QLabel):
            return
        self.editor.setEnabled(online and self._write)

    def set_value(self, value: Any) -> None:
        ed = self.editor
        if isinstance(ed, QLabel):
            ed.setText(format_value(self.prop, value))
            return
        if value is None:
            return
        if isinstance(ed, ToggleSwitch):
            ed.blockSignals(True)
            ed.set_on(bool(value), animate=False)
            ed.blockSignals(False)
        elif isinstance(ed, QComboBox):
            idx = ed.findData(value)
            if idx >= 0:
                ed.blockSignals(True)
                ed.setCurrentIndex(idx)
                ed.blockSignals(False)
        elif ed is not None and hasattr(self, "slider"):
            self.slider.blockSignals(True)
            self.slider.setValue(max(self.slider.minimum(),
                                     min(self.slider.maximum(), int(value))))
            self.slider.blockSignals(False)
            self.value_label.setText(format_value(self.prop, value))


class DetailDialog(QDialog):
    def __init__(
        self,
        state: DeviceState,
        store: Store,
        palette: theme.Palette,
        parent: QWidget | None = None,
        icons: IconProvider | None = None,
    ) -> None:
        super().__init__(parent)
        self.state = state
        self.store = store
        self._p = palette
        self._accent = theme.category_color(state.card.category)
        self._rows: list[ControlRow] = []
        self.setWindowTitle(state.name)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self._frame = WindowFrame(self, resizable=True,
                                  fill=lambda: surface_bg(self._p),
                                  border=lambda: surface_border(self._p))
        self.resize(480, 700)
        self.setMinimumSize(400, 460)

        # 外面留 1px 给描边（QSS 的 border 画在窗口最外圈，内容要缩进来才看得见）
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 自绘标题栏：设备名 + 房间，和主窗口同一套观感（没有最大化）
        self.titlebar = TitleBar(state.name, palette, subtitle=state.room or "",
                                 buttons=("close",), variant="dialog")
        outer.addWidget(self.titlebar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer.addWidget(scroll, 1)

        # 底部固定一行：左边"隐藏此设备"，右边数据新鲜度。不跟着内容滚走。
        foot = QWidget()
        foot.setFixedHeight(36)
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(PAD, 0, PAD, 0)
        fl.setSpacing(10)
        self.btn_hide = QPushButton("隐藏此设备")
        self.btn_hide.setObjectName("Link")
        self.btn_hide.setCursor(Qt.PointingHandCursor)
        self.btn_hide.clicked.connect(self._toggle_hidden)
        tooltip.watch(self.btn_hide, self._hide_tip)
        fl.addWidget(self.btn_hide)
        fl.addStretch(1)
        self._foot = QLabel("")
        self._foot.setStyleSheet(f"color:{palette.text_mute}; font-size:11px;")
        fl.addWidget(self._foot)
        outer.addWidget(foot)

        self.setStyleSheet(
            "QDialog{background:transparent;}"
            f"QPushButton#Link{{background:transparent;border:none;color:{palette.text_mute};"
            f"font-size:12px;padding:2px 0;}}"
            f"QPushButton#Link:hover{{color:{palette.accent};}}"
        )

        body = QWidget()
        body.setStyleSheet(f"background:{surface_bg(palette)};")
        self._col = QVBoxLayout(body)
        self._col.setContentsMargins(PAD, PAD, PAD, PAD)
        self._col.setSpacing(16)
        scroll.setWidget(body)

        self._col.addWidget(_Header(state, palette, self._accent, icons))
        self._camera: CameraView | None = None
        self._cam_live = False
        self._build_camera()
        self._build_controls()
        self._build_metrics()
        self._build_advanced()
        self._col.addStretch(1)
        self._sync_hide_button()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._sync)
        self._timer.start(600)  # 详情页跟着轮询走，不用自己发请求

    # -- 构建 ------------------------------------------------------------
    def _build_camera(self) -> None:
        """摄像头品类才有的实时画面。

        取流是重活（外部进程 + P2P 打洞 + 首帧 3–6 秒），所以只在详情页打开时连一次，
        离开就断——同一台摄像头只允许一个 P2P 会话，挂着不放会占住设备。

        打开页面只做**预热**：连接建立、首帧到手就冻住，等用户点播放。这样用户不用
        盯着黑屏等 3–6 秒，点下去是即时的。

        没有 go2rtc 时不假装能播，直接把"缺什么"写在画面上（``CameraError`` 的文案）。
        """
        if not is_camera(self.state.node.model, self.state.card.category):
            return
        lay = self._group("实时画面")
        self._camera = CameraView(self._p, self._accent)
        # 重试 / 释放后重连都直接播：用户点这一下就是想看到画面，不该再让他点第二次
        self._camera.retry.connect(self._reconnect_camera)
        self._camera.state_changed.connect(self._on_camera_state)
        # 断流时问一句 go2rtc 日志：观看位被占满和设备掉线在画面上长得一样，
        # 但用户能做的事完全不同（关掉别的查看端 vs 等等）
        self._camera.set_hint_provider(camera_service().stream_hint)
        # 状态提示都画在画面里（加载 / 就绪 / 错误），外面再加一行说明只会重复，
        # 而且显隐切换会让下面整块内容上下跳
        lay.addWidget(self._camera)
        QTimer.singleShot(120, self._open_camera)

    def _open_camera(self, autoplay: bool = False) -> None:
        if self._camera is None:
            return
        st = self.store.devices.get(self.state.did, self.state)
        if not st.online:
            self._cam_live = False
            self._camera.show_error("设备离线，无法取流")
            return
        try:
            url = camera_service().open(self.store.client, st.node)
        except CameraError as exc:
            self._cam_live = False
            self._camera.show_error(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - 外部进程 + 网络，兜底别让详情页崩掉
            self._cam_live = False
            self._camera.show_error(f"取流失败：{exc}")
            return
        self._cam_live = True
        self._camera.preload(url, autoplay=autoplay, did=st.did)

    def _reconnect_camera(self) -> None:
        self._open_camera(autoplay=True)

    def _on_camera_state(self, state: str) -> None:
        if state in (CameraView.RELEASED, CameraView.ERROR, CameraView.STOPPED):
            self._cam_live = False

    def _stop_camera(self) -> None:
        self._cam_live = False
        if self._camera is not None:
            self._camera.stop()

    def closeEvent(self, event) -> None:
        self._stop_camera()
        super().closeEvent(event)

    def done(self, result: int) -> None:
        self._stop_camera()
        super().done(result)

    def _group(self, title: str) -> QVBoxLayout:
        box = QWidget()
        box.setStyleSheet(
            f"background:{self._p.card}; border-radius:{theme.RADIUS_CARD}px;"
        )
        lay = QVBoxLayout(box)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(4)
        if title:
            cap = QLabel(title)
            cap.setStyleSheet(f"color:{self._p.text_mute}; font-size:12px;")
            lay.addWidget(cap)
        self._col.addWidget(box)
        return lay

    def _build_controls(self) -> None:
        card = self.state.card
        if not card.controls and not card.gangs:
            return
        lay = self._group("")
        if card.gangs:
            for g in card.gangs:
                row = ControlRow(g.power, g.label, self._p, self._accent, "switch")
                row.changed.connect(self._on_change)
                self._rows.append(row)
                lay.addWidget(row)
            return
        for ctl in card.controls:
            if ctl.prop is None:
                continue
            row = ControlRow(ctl.prop, ctl.label, self._p, self._accent, ctl.role, write=ctl.write)
            row.changed.connect(self._on_change)
            self._rows.append(row)
            lay.addWidget(row)

    def _build_metrics(self) -> None:
        card = self.state.card
        if not card.metrics:
            return
        lay = self._group("")
        for m in card.metrics:
            if m.prop is None:
                continue
            row = ControlRow(m.prop, m.label, self._p, self._accent, "readout", write=False)
            self._rows.append(row)
            lay.addWidget(row)

    def _build_advanced(self) -> None:
        card = self.state.card
        if not card.advanced:
            return
        lay = self._group(f"更多功能 · {len(card.advanced)} 项")
        for prop in card.advanced:
            role = "switch" if prop.kind == "bool" else ("select" if prop.kind == "enum" else "slider")
            row = ControlRow(prop, prop.label[:24], self._p, self._accent, role)
            row.changed.connect(self._on_change)
            self._rows.append(row)
            lay.addWidget(row)

    # -- 数据 ------------------------------------------------------------
    def _on_change(self, siid: int, piid: int, value: object) -> None:
        self.store.set_value(self.state.did, (siid, piid), value)

    def _hide_tip(self) -> str:
        """悬停提示的文案（惰性取，隐藏状态变了不用重新注册）。"""
        hidden = self.store.prefs.is_hidden(self.state.did)
        return ("重新显示在所属房间" if hidden
                else f"隐藏后收进首页末尾的「{HIDDEN_ROOM}」分组，不影响控制")

    def _toggle_hidden(self) -> None:
        hidden = self.store.prefs.is_hidden(self.state.did)
        self.store.set_hidden(self.state.did, not hidden)
        self._sync_hide_button()

    def _sync_hide_button(self) -> None:
        hidden = self.store.prefs.is_hidden(self.state.did)
        self.btn_hide.setText("取消隐藏" if hidden else "隐藏此设备")

    def _sync(self) -> None:
        st = self.store.devices.get(self.state.did)
        if st is None:
            return
        self.state = st
        if self._camera is not None and self._cam_live and not st.online:
            # 设备掉线时 P2P 会先断流再超时，主动收掉比等播放器报错快
            self._cam_live = False
            self._camera.show_error("设备离线，画面已断开")
        for row in self._rows:
            row.set_enabled(st.online)
            row.set_value(st.values.get((row.prop.siid, row.prop.piid)))
        self.titlebar.set_title(st.name)
        self.titlebar.set_subtitle(st.room or "")
        self._sync_hide_button()
        if not st.online:
            self._foot.setText("设备已离线，控制不可用")
        elif st.age > 1e6:
            self._foot.setText("尚未读取到数据")
        else:
            self._foot.setText(f"数据更新于 {int(st.age)} 秒前")


class _Header(QWidget):
    def __init__(self, state: DeviceState, palette: theme.Palette, accent: str, icons: IconProvider | None = None) -> None:
        super().__init__()
        self.state = state
        self._p = palette
        self._accent = accent
        self._icons = icons
        self.setFixedHeight(108)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self._p
        r = self.rect().adjusted(0, 0, -1, -1)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.tint(self._accent, 0.12, pal, min_gap=6)))
        p.drawRoundedRect(r, theme.RADIUS_CARD, theme.RADIUS_CARD)
        if self._icons is not None:
            pm = self._icons.scaled(self.state.node.model, 76)
            if pm is not None:
                p.drawPixmap(self.width() - pm.width() - 16,
                             (self.height() - pm.height()) // 2, pm)

        text_w = self.width() - (110 if self._icons is not None else 40)
        draw_text(p, QRect(20, 18, text_w, 34), self.state.name, size=theme.FS_TITLE,
                  color=pal.text, weight=QFont.DemiBold)
        sub = "已离线" if not self.state.online else (self.state.subtitle or "")
        draw_text(p, QRect(20, 56, text_w, 20), sub, size=theme.FS_CARD_SUB, color=pal.text_soft)
        draw_text(p, QRect(20, 78, text_w, 18), self.state.node.model, size=theme.FS_UNIT,
                  color=pal.text_mute)
        p.end()


__all__ = ["DetailDialog"]
