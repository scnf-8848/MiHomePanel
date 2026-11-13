"""设备详情页。

首页卡片只放高频操作，低频的全部收进这里——这是米家 UED 明确要求的分层，
也是参考项目最大的毛病：它把规格里所有可写属性一次性铺在同一个面板上。

这里按 ``CardSpec`` 的三层结构排：

* ``controls`` —— 主控件（开关、亮度、色温、温度、风速…）
* ``metrics`` —— 只读读数（功率、电量、PM2.5…）
* ``advanced`` —— 其余可写属性，折叠在"更多功能"里
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any

from PySide6.QtCore import QPointF, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core import timing
from ..core.camera import CameraError, is_camera, probe_local
from ..core.camera import shared as camera_service
from ..core.catalog import format_value
from ..core.enum_text import prop_short
from ..core.freshness import stale_text
from ..core.prefs import HIDDEN_ROOM
from ..core.spec import PropertySpec
from ..core.store import DeviceState, Store
from . import theme, tooltip
from .camera_popout import CameraPopoutWindow
from .camera_view import CameraView, PlaybackOwnership, playbacks
from .card_controls import MAX_SEGMENTS, CardSeg, CardSlider, CardStepper
from .chrome import (
    SHADOW_PAD,
    TitleBar,
    WindowFrame,
    floating_size,
    shadow_alpha,
    surface_bg,
    surface_border,
)
from .combo import Combo
from .ptz_pad import PtzPad
from .widgets import ElidedLabel, IconProvider, ToggleSwitch, draw_text

PAD = 22
#: 枚举行用轨道时允许的最大自然宽度。按**最小窗口**（内容宽 400）算一次：
#: 400 − 两侧 PAD 22×2 − 组内边距 18×2 − 竖滚动条 14 − 行内间距 12 = 294，
#: 再给左侧属性名留四个汉字（56）——剩下 238。
#: 判据是**静态**的：建行时算一次，不随窗口 resize 重新选控件（重建 editor
#: 会丢焦点、抖滚动条）。超过就退回下拉：轨道放不下只能截字，下拉能列全。
SEG_MAX_W = 238

# 【取流计时】画面状态的中文名，只用于 core/timing.py 的日志
_CAM_STATE_NAMES = {
    CameraView.IDLE: "空闲",
    CameraView.LOADING: "加载中",
    CameraView.PLAYING: "播放中",
    CameraView.PAUSED: "已暂停",
    CameraView.RELEASED: "已释放",
    CameraView.ERROR: "错误",
    CameraView.STOPPED: "已停止",
}


#: 下拉框的默认占位文案（"还没有值"）。模式行在"无激活项 / 值不在枚举里"时
#: 会临时换成「无」「其他」，回到正常值时必须还原成这个——否则下次 index 变回
#: -1 会露出上一轮的旧文案。
_PLACEHOLDER = "—"


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

        self.name = ElidedLabel(prop_short(label))
        self.name.setStyleSheet(f"color:{palette.text}; font-size:14px;")
        row.addWidget(self.name, 1)

        self.editor: QWidget | None = None
        #: 读数陈旧提示（只有 readout 行有）；``None`` = 这一行不放提示位
        self.hint: QLabel | None = None
        if role == "switch" or role == "toggle":
            sw = ToggleSwitch(palette=palette, accent=accent, size="md")
            sw.setEnabled(write)
            sw.toggled.connect(lambda v: self.changed.emit(prop.siid, prop.piid, v))
            self.editor = sw
        elif role == "select" and prop.value_list:
            seg = self._build_seg(prop, palette, accent, write)
            if seg is not None:
                self.editor = seg
            else:
                # 退回下拉框。**不是降级体验**：轨道放不下（枚举项多、文案长）时
                # 下拉才是对的——它能把二十项都列出来，轨道只能截字。
                # 下拉框走全局 Combo：箭头、弹出层圆角、禁滚轮都和设置页一致，
                # 样式表也在那一处维护，这里不再各自写一份（曾经常年漏改深色）。
                box = Combo(palette)
                # 没有值可显示时（未读到 / 客户端"无激活项"）由 placeholder 兜底，
                # 不往列表里塞假选项：假选项既能被写出去，也会被当成枚举值回读。
                box.setPlaceholderText(_PLACEHOLDER)
                # 枚举项的 comment 常常是一整句中文（"空场景（灯不在任何情况模式时…）"），
                # 按内容撑宽会顶出卡片，所以固定按 10 个字宽度算，多余部分由控件自己截。
                box.setMinimumContentsLength(10)
                box.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
                box.setMaximumWidth(240)
                for o in prop.value_list:
                    # 短名来自 core/enum_text.py 的人工映射（云端原文常是整句中文或英文）；
                    # 没映射过的由那一层截断。完整说明仍然挂在 tooltip 上，信息不丢。
                    box.addItem(o.display_short, o.value)
                    if o.display != o.display_short:
                        box.setItemData(box.count() - 1, o.display, Qt.ToolTipRole)
                box.setEnabled(write)
                box.currentIndexChanged.connect(
                    lambda i: self.changed.emit(prop.siid, prop.piid, box.itemData(i))
                )
                self.editor = box
        elif role == "stepper" and prop.value_range:
            self.editor = self._build_stepper(prop, palette, accent, write)
        elif role == "slider" and prop.value_range:
            self.editor = self._build_slider(prop, palette, accent, write)
        elif role == "readout":
            # "多久没变"的提示位：平时整段是空字符串且隐藏，行宽与基线都不变，
            # 只有云端时间戳真的超阈值才显示（判据见 core/freshness.py）
            hint = QLabel("")
            hint.setStyleSheet(f"color:{palette.text_mute}; font-size:12px;")
            hint.setVisible(False)
            self.hint = hint
            row.addWidget(hint, 0, Qt.AlignRight)
            lab = QLabel("—")
            lab.setStyleSheet(f"color:{palette.text}; font-size:15px; font-weight:600;")
            lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.readout = lab
            self.editor = lab

        if self.editor is not None:
            row.addWidget(self.editor, 0, Qt.AlignRight)

    def _build_seg(self, prop: PropertySpec, palette: theme.Palette, accent: str, write: bool) -> QWidget | None:
        """单选枚举用卡片同款轨道分段（``CardSeg``），放不下返回 ``None``。

        为什么详情页也换：同一个属性在卡片上是轨道、在详情页是下拉，两处手感
        不一致，用户得记两套。而且轨道的"选中段实心染色 + 反色字"比下拉框那
        一行小字更容易一眼看出当前是哪一档。

        判据是 ``sizeHint()``（按 12px 字号量的自然宽度）不超过 ``SEG_MAX_W``：
        超了就是"段里的字要截"，那还不如让下拉把所有项列清楚。**不试字号**——
        详情页和卡片的字号必须一样，为了塞下而在详情页缩到 10px 只会显得没做完。
        """
        if len(prop.value_list) > MAX_SEGMENTS:
            return None
        seg = CardSeg(prop, palette, accent, label="", parent=self)
        if seg.sizeHint().width() > SEG_MAX_W:
            # 这枚轨道不进布局、马上丢弃。**必须先 hide 再 deleteLater**：延迟删除
            # 绑在调用它的事件循环层级上——详情页是在主循环的点击回调里建、再
            # exec() 进嵌套循环，DeferredDelete 要等 exec 返回才处理，期间这枚
            # 100×26 的孤儿控件会画在行左上角，和右边的下拉叠成"一行两种样式"
            # （2026-11-13 用户报挂灯「模式」行）。
            seg.hide()
            seg.deleteLater()
            return None
        # 按量出来的宽度钉死：``CardControl`` 的 size policy 是 Expanding，
        # 交给布局分配的话会被左侧属性名挤窄（实测 254 → 159，段里的字就开始截），
        # 而这一行的语义是"名字让位给控件"——名字本来就有省略号（``ElidedLabel``）
        seg.setFixedWidth(seg.sizeHint().width())
        seg.setEnabled(write)
        seg.committed.connect(
            lambda v: self.changed.emit(prop.siid, prop.piid, v)
        )
        self.seg = seg
        return seg

    def _build_stepper(self, prop: PropertySpec, palette: theme.Palette, accent: str, write: bool) -> QWidget:
        """温度这类精确刻度用卡片 body 同款步进器：``− 数值 +``。

        沿用 ``CardStepper`` 的按钮尺寸、行高和字号，不再单独造一套详情页样式。
        """
        stepper = CardStepper(prop, palette, accent, centered=True)
        stepper.setEnabled(write)
        stepper.committed.connect(
            lambda v: self.changed.emit(prop.siid, prop.piid, v)
        )
        self.stepper = stepper
        return stepper

    def _build_slider(self, prop: PropertySpec, palette: theme.Palette, accent: str, write: bool) -> QWidget:
        """亮度 / 色温用卡片 body 同款自绘滑条：整条轨道，色温整条渐变。

        ``compact=True`` 去掉控件内部的属性名（这一行左侧已经有名字了），
        轨道从最左边开始；点击任意位置跳值、拖动松手提交，滚轮只滚页面不改值。
        键盘不参与（方向键 / PageUp/Down / Home/End 都不改值、不发写请求），
        这些都由 ``CardSlider`` 自己保证。
        """
        slider = CardSlider(
            prop,
            palette,
            accent,
            gradient=prop.unit == "kelvin",
            compact=True,
        )
        slider.setFixedWidth(210)
        slider.setEnabled(write)
        slider.committed.connect(
            lambda v: self.changed.emit(prop.siid, prop.piid, v)
        )
        self.slider = slider
        return slider

    def select_value(self, value: Any) -> bool:
        """按值选中；``value`` 不在列表里返回 ``False``（不改变显示）。

        给"客户端语义值"用：例如挂灯的 scene-mode=0 在 ``mode`` 的枚举里没有
        对应项，这时不能伪造一个选项，只能让控件显示占位文案。
        """
        ed = self.editor
        if isinstance(ed, CardSeg):
            # 轨道没有"占位项"，只能"一段都不点亮"；值不在枚举里就什么都不动
            # （保留上一次的选择），由调用方决定要不要提示
            for o in self.prop.value_list:
                if o.value == value:
                    ed.set_value(value)
                    return True
            return False
        if not isinstance(ed, QComboBox):
            return False
        idx = ed.findData(value)
        if idx < 0:
            return False
        ed.blockSignals(True)
        ed.setCurrentIndex(idx)
        ed.setPlaceholderText(_PLACEHOLDER)
        ed.blockSignals(False)
        return True

    def set_no_selection(self, text: str) -> None:
        """显示一段**客户端**占位文案，且不选中任何真实枚举项。

        ``text`` 只是"当前值不属于这个枚举"的说明（如"无"），不能被写出去：
        下拉停在 ``currentIndex() == -1``，选任何一项仍然是正常写请求。

        **轨道上没有能放一行小字的地方**，所以只能"一段都不点亮 + 把说明挂
        tooltip"。宁可空着，也不要"保留上一次的选中"——那会显示一个设备其实现
        在不在的状态，比不显示更糟。闪烁由 ``_refresh_mode`` 的 ``_mode_pending``
        挡住（刚点完的那几百毫秒不去动它）。
        """
        ed = self.editor
        if isinstance(ed, CardSeg):
            ed.set_unknown()
            ed.setToolTip(text)
            return
        if not isinstance(ed, QComboBox):
            return
        ed.blockSignals(True)
        ed.setPlaceholderText(text)
        ed.setCurrentIndex(-1)
        ed.blockSignals(False)

    def set_enabled(self, online: bool, *, locked: bool = False) -> None:
        """离线时控件不可操作；只读读数（QLabel）不受影响。

        ``locked`` 是"设备在线但这一行本来就不该操作"（多键面板转无线的那一路）：
        和离线一样变灰、关掉编辑器，区别只在这不是故障。
        """
        active = online and not locked
        self.name.setStyleSheet(
            f"color:{self._p.text if active else self._p.text_mute}; font-size:14px;"
        )
        if self.editor is None or isinstance(self.editor, QLabel):
            return
        self.editor.setEnabled(active and self._write)

    def set_value(self, value: Any) -> None:
        ed = self.editor
        if isinstance(ed, QLabel):
            text = format_value(self.prop, value)
            ed.setText(text)
            # 显示的是短名时，把云端原文（可能是整句告警，如"大电池电量耗尽…"）挂到
            # tooltip：行宽放不下整句，但信息不该丢。
            opt = self.prop.option(value) if self.prop is not None else None
            full = opt.display if opt is not None else ""
            ed.setToolTip(full if full and full != text else "")
            return
        if value is None:
            return
        if isinstance(ed, ToggleSwitch):
            ed.blockSignals(True)
            ed.set_on(bool(value), animate=False)
            ed.blockSignals(False)
        elif isinstance(ed, CardSeg):
            ed.set_value(value)
        elif isinstance(ed, QComboBox):
            idx = ed.findData(value)
            if idx >= 0:
                ed.blockSignals(True)
                ed.setCurrentIndex(idx)
                # 离开「无 / 其他」占位态：不复位的话，下一次 index 变回 -1
                # 会露出上一轮的旧文案（例如值已经正常了还写着"其他"）。
                ed.setPlaceholderText(_PLACEHOLDER)
                ed.blockSignals(False)
        elif ed is not None and hasattr(self, "stepper"):
            self.stepper.set_value(value)
        elif ed is not None and hasattr(self, "slider"):
            self.slider.set_value(value)

    def set_stale(self, text: str) -> None:
        """给读数行标注"多久没变"，空串 = 不提示。

        只作用于 readout 行；开关 / 滑条 / 下拉都是用户可写的量，陈旧与否由用户自己
        的动作决定，标出来只会让人以为坏了。
        """
        hint = self.hint
        if hint is None:
            return
        if hint.text() != text:
            hint.setText(text)
        hint.setVisible(bool(text))

    def set_unknown(self) -> None:
        """值没读到（还没读、读取失败、设备不返回）时不要伪装成默认值。

        开关停在"关"、下拉停在第一项、滑条停在最小值，看上去都像读成功了；
        这里把控件禁用并清空显示，配合分组标题上的读取状态一起说明"还不知道"。
        """
        ed = self.editor
        if ed is None or isinstance(ed, QLabel):
            return
        ed.setEnabled(False)
        if isinstance(ed, ToggleSwitch):
            ed.blockSignals(True)
            ed.set_on(False, animate=False)
            ed.blockSignals(False)
        elif isinstance(ed, CardSeg):
            ed.set_unknown()
        elif isinstance(ed, QComboBox):
            ed.blockSignals(True)
            ed.setPlaceholderText(_PLACEHOLDER)  # 清掉上一轮可能留下的"无"等客户端文案
            ed.setCurrentIndex(-1)
            ed.blockSignals(False)
        elif hasattr(self, "slider"):
            self.slider.set_unknown()


def _session_taken_by_other(
    owner: PlaybackOwnership, own: CameraView | None, did: str
) -> bool:
    """本页这台设备的会话是不是已经在别的承载面手里了。

    两种来源：卡片墙刚把画面收回去（``about_to_stop`` 之后），或者独立窗口还在播。
    这一支必须抢在兜底的 ``release(did)`` 之前——否则本页关窗会把别人的画面掐掉。
    ``closeEvent`` 与 ``done`` 各调一次 ``_stop_camera``，第二次就落在这里。
    """
    return owner.active and owner.did == did and owner.active_view is not own


class DetailDialog(QDialog):
    #: 页面真的要关、会话还没结束时发出（参数是 did）：给卡片墙一次接管机会，
    #: 它能不收尾就把画面收回去继续播。接过之后本页只清自己的界面。
    about_to_stop = Signal(str)

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
        #: 只读读数行（卡片 metrics 那一组）：只有它们会带"多久没变"的提示
        self._readout_rows: list[ControlRow] = []
        #: 挂灯这类机型的"模式"行：激活项看 scene-mode(3,9)，见 ``_sync_mode_row``
        self._mode_row: ControlRow | None = None
        #: 多键面板：siid → 那一行的控件。转无线的那一路要随时能改回"不可操作"，
        #: 所以行要留下来按 siid 找（模式在设备侧改了，轮询会读到新值）。
        self._gang_rows: dict[int, ControlRow] = {}
        #: 同一功能两套属性的行（风扇的风速/档位）：槽位 → 行。按当前模式只留一行，
        #: 规则来自 ``CardSpec.speed_by_mode``（型号级），没有规则的设备这里是空的。
        self._speed_rows: dict[str, ControlRow] = {}
        #: 用户刚点过模式、设备还没把 scene-mode 改过来的过渡态
        self._mode_pending = False
        self.setWindowTitle(state.name)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        # 浮窗：圆角 18 + 外圈阴影（窗口比可见面板大一圈，内容挂 ``body``）
        self._frame = WindowFrame(self, resizable=True,
                                  radius=theme.RADIUS_CARD,
                                  shadow=SHADOW_PAD,
                                  shadow_alpha=shadow_alpha(self._p),
                                  fill=lambda: surface_bg(self._p),
                                  border=lambda: surface_border(self._p))
        self.resize(*floating_size(480, 700))
        self.setMinimumSize(*floating_size(400, 460))

        outer = QVBoxLayout(self._frame.body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 自绘标题栏：设备名 + 房间，和主窗口同一套观感（没有最大化）
        self.titlebar = TitleBar(state.name, palette, subtitle=state.room or "",
                                 buttons=("close",), variant="dialog",
                                 frame=self._frame)
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
        #: 云台方向盘；只有自建 go2rtc（带 /api/xiaomi/ptz）才有
        self._ptz: PtzPad | None = None
        #: 包住方向盘的那层（自带 10px 上边距），跟着方向盘一起显隐
        self._ptz_host: QWidget | None = None
        #: None = 还没问过 go2rtc 这个构建支不支持云台
        self._cam_ptz_ok: bool | None = None
        self._cam_popout: CameraPopoutWindow | None = None
        self._cam_live = False
        #: 卡片墙已经把一条正在播的会话交给了本页：不再预热、不再重新 open
        self._cam_adopted = False
        # 取流要起 go2rtc 子进程（实测 0.85s），放后台线程做，结果用队列回主线程。
        # 不用跨线程信号：窗口关掉之后线程还在跑，回调到已销毁的控件会出事。
        self._cam_url = ""
        self._cam_wants_play = False
        #: 当前地址是不是远程中继通道；以及本次打开是否已经自动换过一次通道
        self._cam_remote = False
        self._cam_fallback = False
        #: 最近一次可达性探测的结果 (是否可达, 耗时 ms, 说明)；没探测过是 None
        self._cam_probe: tuple[bool, float, str] | None = None
        #: 探测判定不通、于是直接走了远程（只影响日志里的通道名）
        self._cam_probe_failed = False
        #: 播放器报错的时刻，用来算"报错→回落"的间隔
        self._cam_error_at = 0.0
        # 【取流计时】core/timing.py 的记录号，不参与取流逻辑
        self._cam_play_token = 0
        self._cam_prefetch_token = 0
        self._cam_job_token = 0
        self._cam_job: threading.Thread | None = None
        self._cam_results: queue.SimpleQueue[
            tuple[str, str, str, bool, tuple[bool, float, str] | None]
        ] = queue.SimpleQueue()
        self._cam_poll = QTimer(self)
        self._cam_poll.setInterval(80)
        self._cam_poll.timeout.connect(self._collect_camera)
        # 局域网看门狗：实测首帧最慢 6.5–6.7 秒（LoadingMedia 2.5–4.1 s + 等 GOP
        # 2.6–4.0 s），所以 9 秒还没画面就判定"没报错但卡住"，主动换通道。
        # 只用来看住不报错的僵死；明确失败由播放器的 errorOccurred 立即处理。
        self._cam_started = 0.0
        self._cam_watchdog = QTimer(self)
        self._cam_watchdog.setInterval(1000)
        self._cam_watchdog.timeout.connect(self._watch_camera)
        self._build_camera()
        self._build_controls()
        self._build_metrics()
        self._adv_header: _AdvancedHeader | None = None  # 没有 advanced 的设备不建这一组
        self._adv_rows: list[ControlRow] = []
        self._build_advanced()
        self._col.addStretch(1)
        self._sync_hide_button()
        # 建完立刻按 store 里的当前值填一遍。只靠下面那个 600ms 的定时器，第一帧
        # 会是控件的"出厂态"——滑条停在最左、轨道一段都不亮、开关默认关着——用户
        # 看到的是"先空一下才变成真实值"（2026-11-12 用户报）。而 store 里明明
        # 已经有这些数了：轮询一直在跑，卡片上也是对的，没道理进详情页先空一帧。
        self._sync()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._sync)
        self._timer.start(600)  # 详情页跟着轮询走，不用自己发请求

    # -- 构建 ------------------------------------------------------------
    def _build_camera(self) -> None:
        """摄像头品类才有的实时画面。

        取流是重活（外部进程 + P2P 打洞 + 等一个 GOP），所以**打开页面不取流**：
        只把 go2rtc 和本机 RTSP 地址在后台准备好（这一步不建立 P2P 会话、不占观看位），
        画面区停在一个播放键上。用户点播放才真正连 RTSP。

        没有 go2rtc 时不假装能播，点了播放直接把"缺什么"写在画面上
        （``CameraError`` 的文案）。
        """
        if not is_camera(self.state.node.model, self.state.card.category):
            return
        lay = self._group("实时画面")
        self._camera = CameraView(self._p, self._accent)
        # 点播放 / 重试都只发这一个信号：用户点这一下就是想看到画面
        self._camera.play_requested.connect(self._start_camera)
        self._camera.state_changed.connect(self._on_camera_state)
        # 独立窗口只搬控件，不重新取流（见 ui/camera_popout.py）
        self._camera.popout_requested.connect(self._toggle_camera_popout)
        # 【取流计时】首帧真正画到控件上时结束一条计时记录
        self._camera.first_frame_shown.connect(self._on_camera_first_frame)
        # 断流时问一句 go2rtc 日志：观看位被占满和设备掉线在画面上长得一样，
        # 但用户能做的事完全不同（关掉别的查看端 vs 等等）
        self._camera.set_hint_provider(camera_service().stream_hint)
        # 状态提示都画在画面里（加载 / 错误），外面再加一行说明只会重复，
        # 而且显隐切换会让下面整块内容上下跳
        lay.addWidget(self._camera)
        # 云台方向盘：画面下方居中的一个圆。**能不能转要先问 go2rtc**——云台指令
        # 走的是取流那条 MISS 连接，只有自建构建暴露了 /api/xiaomi/ptz；官方构建
        # 直接 404，这时整块不出现（而不是给一个点了没反应的灰圆）。
        # 探测在后台取流线程里做（_camera_open_worker），这里先建好藏起来。
        #
        # 圆盘与画面之间要和「圆盘到卡片下沿」一样宽（`_group` 的下边距 14）。
        # 用一个带 10px 上边距的容器包住圆盘：布局 spacing 4 + 容器上边距 10 = 14。
        # 不能直接把 addSpacing 塞进 lay——官方构建里圆盘不建，那段空白会留在
        # 卡片底部；容器整体显隐，间隔就跟着一起消失。
        self._ptz_host = QWidget()
        host = QVBoxLayout(self._ptz_host)
        host.setContentsMargins(0, 10, 0, 0)
        host.setSpacing(0)
        self._ptz = PtzPad(self.state.did, self._p, self._accent)
        self._ptz.hide()
        host.addWidget(self._ptz, 0, Qt.AlignHCenter)
        self._ptz_host.hide()
        lay.addWidget(self._ptz_host, 0, Qt.AlignHCenter)
        QTimer.singleShot(0, self._prefetch_camera)

    def _cam_begin(self, label: str) -> int:
        """【取流计时】开一条记录，顺带把摄像头地址和偏好交给「环境」行。"""
        return timing.begin(
            label,
            self.state.did,
            context={
                "camera_ip": str(getattr(self.state.node, "localip", "") or ""),
                "preference": str(self.store.camera_network or ""),
            },
        )

    def _prefetch_camera(self) -> None:
        """页面刚打开：后台把 go2rtc 和本机地址准备好，但不建立会话。

        只是省掉子进程启动那 0.85 秒；摄像头那边没有观看位、没有上行流量，
        和"自动加载画面"是两回事。失败也先不打扰用户，等点了播放再说。

        远程不预热：relay 握手会真的让摄像头加入会话、占掉一个观看位，那就违背了
        "点播放才加载"。异地用户点播放时多花一次握手（2–4 秒），比开着页面偷偷
        占位更合理。
        """
        if self._camera is None:
            return
        if self._cam_adopted:
            # 画面是从卡片墙接过来的：再预热一次会把正在用的通道改走
            return
        self._cam_wants_play = False
        self._cam_prefetch_token = self._cam_begin("后台预热")
        if self._remote_wanted():
            timing.mark_on(
                self._cam_prefetch_token, "预热跳过", "远程模式不预热，避免占观看位"
            )
            timing.end_on(self._cam_prefetch_token, "跳过")
            self._cam_prefetch_token = 0
            return
        timing.set_label("后台预热（局域网）", token=self._cam_prefetch_token)
        # 预热只探路 + 起 go2rtc，探测不通也不擅自建远程会话（那要占观看位）
        self._spawn_camera_open(
            remote=False, token=self._cam_prefetch_token, allow_remote=False
        )

    def _pref(self) -> str:
        """取流方式偏好：lan_first / remote_first / remote_only。"""
        return str(self.store.camera_network or "")

    def _remote_wanted(self) -> bool:
        """这一次该不该走远程：仅远程、远程优先（未回落后）、已回落过、或没有局域网地址。"""
        pref = self._pref()
        if pref == "remote_only":
            return True
        if pref == "remote_first":
            # 远程优先：自动换过一次通道之后就换到局域网
            return not self._cam_fallback
        if self._cam_fallback:  # lan_first 回落过一次
            return True
        if self._cam_probe_failed:
            return True
        st = self.store.devices.get(self.state.did, self.state)
        return not str(getattr(st.node, "localip", "") or "")

    def _lane_text(self, remote: bool) -> str:
        """【取流计时】日志里的通道名；只读偏好与本次状态，不参与选择。"""
        pref = self._pref()
        if remote:
            if pref == "remote_only":
                return "远程中继（仅远程）"
            if pref == "remote_first" and not self._cam_fallback:
                return "远程中继（远程优先）"
            if self._cam_probe_failed:
                return "远程中继（探测不通）"
            if self._cam_fallback:
                return "远程中继（局域网回落）"
            return "远程中继"
        if self._cam_fallback:
            return "局域网直连（远程回落）"
        return "局域网直连"

    def _probe_text(self) -> str:
        """【取流计时】把最近一次可达性探测结果写成一句话。"""
        probe = self._cam_probe
        if probe is None:
            return "无结果"
        ok, ms, detail = probe
        return f"{'可达' if ok else '不通'} {ms:.0f} ms（{detail}）"

    def _start_camera(self) -> None:
        """用户点了播放：地址已就绪就直接连，否则后台取完再连。"""
        if self._camera is None:
            return
        # 单路仲裁要在这里就抢：等后台取完址再抢，可能已经把别人的 relay 改道了
        if not playbacks().claim(
            self._camera, self.state.did, self._cam_url, self._cam_remote, "detail"
        ):
            self._cam_wants_play = False
            self._camera.show_error("另一路摄像头正在播放，先把它停掉")
            return
        if self._cam_play_token:
            timing.end_on(self._cam_play_token, "被新的播放请求替代")
        lane = self._lane_text(self._remote_wanted())
        self._cam_play_token = self._cam_begin(lane)
        # UI 线程的后续打点（播放器状态 / 首帧）归这条记录
        timing.bind(self._cam_play_token)
        self._cam_error_at = 0.0
        timing.mark("点下播放", f"地址已就绪={bool(self._cam_url)}")
        if self._cam_probe is not None:
            probe_text = self._probe_text()
        elif self._remote_wanted():
            probe_text = "跳过（本次不走局域网）"
        else:
            probe_text = "地址未就绪，取流线程里探测"
        timing.mark("快速可达性预判", probe_text)
        if self._cam_url:
            timing.mark("使用已准备好的地址")
            timing.mark("实际通道", self._lane_text(self._cam_remote))
            self._start_playback()
            return
        self._cam_wants_play = True
        if self._cam_job is not None:
            timing.mark("等待后台取流结果")
            return
        self._spawn_camera_open(
            remote=self._remote_wanted(), token=self._cam_play_token
        )

    def _start_playback(self) -> None:
        """把已就绪的地址交给播放器，并告诉它这次失败后要不要本地重连。

        局域网优先的局域网会话：明确失败直接交给外层切远程中继（只切一次），
        不再对同一个局域网地址 ``setSource`` 重连。
        """
        if self._camera is None:
            return
        playbacks().set_url(self._camera, self._cam_url, self._cam_remote)
        self._camera.set_immediate_fallback(
            self._pref() == "lan_first" and not self._cam_remote
        )
        self._cam_live = True
        self._cam_started = time.monotonic()
        self._cam_watchdog.start()
        self._camera.play(self._cam_url)

    def _spawn_camera_open(
        self, remote: bool | None = None, token: int = 0, allow_remote: bool = True
    ) -> None:
        if self._cam_job is not None:
            return  # 已经在取了；结果回来时会看 _cam_wants_play 决定要不要接画面
        if remote is None:
            remote = self._remote_wanted()
        if token:
            self._cam_job_token = token
        st = self.store.devices.get(self.state.did, self.state)
        self._cam_job = threading.Thread(
            target=self._camera_open_worker,
            args=(self.store.client, st.node, st.did, bool(remote), token, allow_remote),
            daemon=True,
        )
        self._cam_job.start()
        self._cam_poll.start()

    def _camera_open_worker(
        self,
        client: Any,
        node: Any,
        did: str,
        remote: bool,
        token: int = 0,
        allow_remote: bool = True,
    ) -> None:
        """后台线程：起 go2rtc（必要时先建 relay）、拿 RTSP 地址。只碰 core，不碰控件。"""
        timing.bind(token)
        timing.mark("后台取流线程开始", f"remote={remote}")
        probe: tuple[bool, float, str] | None = None
        if not remote:
            # 局域网优先，或远程优先失败后换回局域网：都先探一下通不通
            localip = str(getattr(node, "localip", "") or "")
            if localip:
                # 几百毫秒的 UDP 探测，不通就别让用户先等一轮局域网失败
                probe = probe_local(localip)
                timing.mark("可达性探测", self._probe_text_of(probe))
                if not probe[0]:
                    if not allow_remote:
                        # 预热阶段探测不通：只汇报，不偷偷建远程会话（那会占观看位）
                        timing.mark("探测不通：预热到此为止，等点播放再走远程")
                        self._cam_results.put(
                            (did, "", "局域网不可达", False, probe)
                        )
                        return
                    remote = True
                    timing.mark("探测不通：直接走远程中继")
        try:
            url = camera_service().open(client, node, remote=remote)
        except CameraError as exc:
            timing.mark("后台取流失败", str(exc))
            camera_service().release(did)  # 取流失败别把远程会话留在后台占位
            self._cam_results.put((did, "", str(exc), remote, probe))
        except Exception as exc:  # noqa: BLE001 - 外部进程 + 网络，兜底别让详情页崩掉
            timing.mark("后台取流异常", str(exc))
            camera_service().release(did)
            self._cam_results.put((did, "", f"取流失败：{exc}", remote, probe))
        else:
            timing.mark("后台取流完成", url)
            # 顺手问一次这个构建支不支持云台（本机 HTTP，几毫秒；官方构建 404）
            self._cam_ptz_ok = camera_service().ptz_supported()
            self._cam_results.put((did, url, "", remote, probe))

    @staticmethod
    def _probe_text_of(probe: tuple[bool, float, str]) -> str:
        ok, ms, detail = probe
        return f"{'可达' if ok else '不通'} {ms:.0f} ms（{detail}）"

    def _collect_camera(self) -> None:
        """主线程：收后台取流结果，按用户有没有点播放决定接不接画面。"""
        while True:
            try:
                did, url, error, remote, probe = self._cam_results.get_nowait()
            except queue.Empty:
                break
            self._cam_job = None
            self._cam_poll.stop()
            job_token = self._cam_job_token
            self._cam_job_token = 0
            if job_token:
                if url:
                    timing.mark_on(job_token, "取流结果：地址可用")
                else:
                    timing.mark_on(job_token, "取流结果：失败", error)
                if job_token == self._cam_prefetch_token:
                    timing.end_on(job_token, "预热成功" if url else "预热失败")
                    self._cam_prefetch_token = 0
            play = self._cam_wants_play
            self._cam_wants_play = False
            if self._camera is None or did != self.state.did:
                continue
            self._cam_url = url
            self._cam_remote = remote
            if probe is not None:
                self._cam_probe = probe
                self._cam_probe_failed = not probe[0]
            if job_token and url:
                timing.mark_on(job_token, "实际通道", self._lane_text(bool(remote)))
            if not url:
                if play:
                    self._cam_live = False
                    # 探测就判了不通：不算播放器报错，直接换远程，不闪一下错误
                    if (
                        probe is not None
                        and not probe[0]
                        and self._try_channel_switch("可达性探测不通")
                    ):
                        continue
                    self._camera.show_error(error)
                    # show_error 会同步触发 ERROR，外层可能已经自动换了通道（新任务已起）；
                    # 那条记录留给新通道，别在这里结束掉
                    if self._cam_job is None and self._cam_play_token:
                        timing.end_on(self._cam_play_token, "失败", error)
                        self._cam_play_token = 0
                continue
            if play:
                timing.mark("开始播放 RTSP")
                self._start_playback()

    def _on_camera_state(self, state: str) -> None:
        self._sync_ptz(state)
        if state != CameraView.LOADING:
            self._cam_watchdog.stop()
        timing.mark("画面状态", _CAM_STATE_NAMES.get(state, state))
        if state in (CameraView.RELEASED, CameraView.ERROR, CameraView.STOPPED):
            self._cam_live = False
            # 画面已经停了/出错了，大窗口没有继续存在的意义；收回详情页看错误与重试
            if self._cam_popout is not None:
                self._cam_popout.close()
        if state == CameraView.RELEASED:
            # 闲置释放：远程会话要一起放掉，否则观看位还占着。
            # 控件在 `_release_idle()` 里已经把播放器停掉、画面清空、文案摆好了，
            # 这里只放"所有权 + go2rtc 会话"。不能走 `playbacks().release()`——
            # 它还会 stop() + release_player()，把控件重置回空态，那句"闲置 N 分钟
            # 连接已释放"就永远看不到，而且重置瞬间漏出来的尾帧会留在画面上。
            if self._cam_play_token:
                timing.end_on(self._cam_play_token, "闲置释放")
                self._cam_play_token = 0
            playbacks().forget(self._camera)
            camera_service().release(self.state.did)
            if self._cam_remote:
                self._cam_url = ""
        if state == CameraView.PLAYING:
            timing.mark("进入播放态（首帧已解码）")
        if state == CameraView.ERROR:
            # 下次重试让外层重新确认 go2rtc 和地址（进程还活着时这一步几乎不耗时）
            self._cam_url = ""
            self._cam_error_at = time.monotonic()
            if self._try_channel_switch("播放器报错"):
                return
            # 有在途任务时（例如刚切了通道）记录归它，别在这里提前收尾
            if self._cam_play_token and self._cam_job is None:
                timing.end_on(self._cam_play_token, "失败", "播放器报错")
                self._cam_play_token = 0
            # 已经没有下一次自动重试：把观看位放掉，别让别处一直看到"另一路正在播放"。
            # 只清所有权，不 stop/release_player——错误界面要留着给用户点重试。
            did = playbacks().forget(self._camera)
            if did:
                camera_service().release(did)

    def _probe_ptz(self) -> None:
        """后台线程：问一次 go2rtc 支不支持云台，结果由主线程 ``_sync_ptz`` 应用。"""
        self._cam_ptz_ok = camera_service().ptz_supported()

    def _sync_ptz(self, state: str | None = None) -> None:
        """方向盘的可见性与可用性。

        两条独立的判断：**构建支不支持**（官方 go2rtc 没有这条路由，整块不出现）
        和**现在能不能转**（没在播时 go2rtc 那边没有 producer，指令会被拒，置灰）。
        """
        pad = self._ptz
        if pad is None:
            return
        supported = self._cam_ptz_ok is True
        cur = state if state is not None else (
            self._camera.state if self._camera is not None else ""
        )
        playing = cur in (CameraView.LOADING, CameraView.PLAYING, CameraView.PAUSED)
        # 显隐挂在外层容器上：容器里还有"和画面之间的间隔"，不能只藏圆盘
        host = getattr(self, "_ptz_host", None)
        if host is not None:
            host.setVisible(supported)
        pad.setVisible(supported)
        ready = supported and playing
        if not ready:
            pad.stop()
        pad.set_ready(ready)

    def _on_camera_first_frame(self) -> None:
        """【取流计时】首帧真正画到控件上：这条取流记录到这里算成功。"""
        if self._cam_play_token:
            timing.end_on(self._cam_play_token, "成功")
            self._cam_play_token = 0

    def _watch_camera(self) -> None:
        """没报错却长时间没出帧：兜底换一次通道，而不是干等播放器超时。

        明确失败（errorOccurred / Could not open file / InvalidMedia）不走这里，
        那条路径在 ``_on_camera_state(ERROR)`` 里立刻换。
        """
        if not self._cam_live:
            self._cam_watchdog.stop()
            return
        if time.monotonic() - self._cam_started < 9.0:
            return
        self._cam_watchdog.stop()
        self._try_channel_switch("9 秒看门狗：无报错但没出帧")

    def _try_channel_switch(self, reason: str = "") -> bool:
        """自动换一次通道：局域网→远程（局域网优先），或远程→局域网（远程优先）。

        只换一次（``_cam_fallback`` 记住），``remote_only`` 从不自动换。
        换通道是重新取一个地址、再 ``play()``，不会对同一个地址反复 ``setSource``。
        """
        if self._cam_fallback or self._cam_job is not None:
            return False
        pref = self._pref()
        if pref == "remote_only":
            return False
        st = self.store.devices.get(self.state.did, self.state)
        if self._cam_remote:
            if pref != "remote_first":
                return False
            if not str(getattr(st.node, "localip", "") or ""):
                return False
            target_remote = False
        else:
            target_remote = True
        self._cam_fallback = True
        if not target_remote:
            self._cam_probe_failed = False
        if self._cam_play_token:
            lane = self._lane_text(target_remote)
            timing.set_label(lane, token=self._cam_play_token)
            gap = ""
            if self._cam_error_at:
                gap = f"，距报错 {(time.monotonic() - self._cam_error_at) * 1000:.0f} ms"
                self._cam_error_at = 0.0
            timing.mark("触发通道切换", f"{reason or '未知原因'}{gap} → {lane}")
        self._cam_wants_play = True
        if self._camera is not None:
            if target_remote:
                self._camera.show_progress(
                    "局域网直连不可用", "正在尝试远程中继…"
                )
            else:
                self._camera.show_progress("远程中继不可用", "正在尝试局域网直连…")
        # 换到局域网时如果探测不通，就到此为止：远程刚刚已经试过了，别再兜回去
        self._spawn_camera_open(
            remote=target_remote,
            token=self._cam_play_token,
            allow_remote=target_remote,
        )
        return True

    @property
    def camera_view(self) -> CameraView | None:
        """详情页当前承载画面的控件（给卡片墙交接用）。"""
        return self._camera

    def adopt_playing_camera(self) -> bool:
        """把卡片墙正在播的那一路接到本页画面上（只搬 player/sink）。

        由 ``MainWindow._on_card_clicked`` 在构造完本页、进入模态前调用。
        成功后本页不预热、不重新 open，用户直接看到接续的画面。
        """
        if self._camera is None:
            return False
        owner = playbacks()
        if owner.active_view is not self._camera:
            if owner.did != self.state.did:
                return False
            if not owner.move(self._camera, "detail"):
                return False
        elif owner.did != self.state.did:
            # 墙上那一路属于别的设备，不能张冠李戴
            return False
        else:
            owner.set_surface(self._camera, "detail")
        self._cam_adopted = True
        self._cam_live = True
        self._cam_wants_play = False
        self._cam_url = playbacks().url
        self._cam_remote = playbacks().remote
        # 会话是从卡片接过来的，说明 go2rtc 已经在跑：后台确认一下云台能力
        # （不在此处调，接口第一次要发本机 HTTP，别卡在打开页面的那一下）
        threading.Thread(
            target=self._probe_ptz, name="mihome-ptz-probe", daemon=True
        ).start()
        self._cam_started = time.monotonic()
        self._cam_watchdog.stop()
        # 预热的计时记录不再有用（如果已经被调度起来，结果是空操作）
        if self._cam_prefetch_token:
            timing.end_on(self._cam_prefetch_token, "接续卡片播放，取消预热")
            self._cam_prefetch_token = 0
        timing.mark("接续卡片播放", "搬 player/sink，不重新 open")
        return True

    def _toggle_camera_popout(self) -> None:
        """详情页 ↔ 独立窗口。只换控件父窗口，不重新 open / 不重建流。"""
        if self._camera is None:
            return
        if self._cam_popout is not None and self._cam_popout.is_staged:
            self._cam_popout.close()
            return
        if self._cam_popout is not None and self._cam_popout.is_standalone:
            # 已经脱离详情页自己播过一轮并关掉了：不能拿旧窗口再 present
            self._cam_popout = None
        if self._cam_popout is None:
            did = self.state.did
            store = self.store

            def online() -> bool:
                # 只闭包 store，不闭包 self：详情页关掉后别被这个回调拖着不放
                cur = store.devices.get(did)
                return bool(cur is not None and cur.online)

            self._cam_popout = CameraPopoutWindow(
                self._camera,
                self,
                self._p,
                self.state.name,
                did=did,
                accent=self._accent,
                online_provider=online,
            )
        self._cam_popout.present()

    def _stop_camera(self) -> None:
        if self._ptz is not None:
            # 页面要关了（或画面停了）：立刻停止连转，别让定时器继续发指令
            self._ptz.stop()
        if self._cam_popout is not None and self._cam_popout.is_standalone:
            # 第一次 _stop_camera 已经把会话交给独立窗口了（closeEvent/done 会
            # 各调一次），重复调用不能再把窗口关掉。
            return
        timing.mark("停止取流/关闭页面")
        # 独立窗口还在播、且没有在途取流任务：把会话交给窗口，本页只停自己的
        # 看门狗/轮询。窗口关掉时才真正停播 + 归还观看位。
        hand_over = (
            self._cam_popout is not None
            and self._cam_popout.is_staged
            and self._camera is not None
            and self._camera.has_live_playback
            and self._cam_job is None
        )
        if hand_over:
            hand_over = self._cam_popout is not None and self._cam_popout.detach_from_parent()
        if not hand_over and self._cam_popout is not None:
            # 页面内播放（没开独立窗口）或交接失败：按原行为停掉
            self._cam_popout.close()
        if self._cam_play_token:
            timing.end_on(
                self._cam_play_token,
                "交独立窗口继续播放" if hand_over else "用户停止",
            )
            self._cam_play_token = 0
        if self._cam_prefetch_token:
            timing.end_on(self._cam_prefetch_token, "关闭页面")
            self._cam_prefetch_token = 0
        self._cam_live = False
        self._cam_wants_play = False
        self._cam_job_token = 0
        self._cam_poll.stop()
        self._cam_watchdog.stop()
        self._timer.stop()
        if hand_over:
            return
        # 页面真要关了：先给卡片墙一次机会把画面收回去继续播（同一个播放器，
        # 不重连）。它接走之后这条会话就不归本页了，下面必须放手。
        if self._camera is not None and self._camera.has_live_playback:
            self.about_to_stop.emit(self.state.did)
        if self._camera is not None:
            owner = playbacks()
            if _session_taken_by_other(owner, self._camera, self.state.did):
                # 会话已经在别的承载面手里（卡片墙刚接回去 / 独立窗口还在播）：
                # 只清本页界面。``closeEvent`` 与 ``done`` 各会调一次，第二次就
                # 落在这一支上——绝不能走兜底的 release(did)，那会掐掉别人的画面。
                self._camera.stop()
                return
            # 有所有者登记时统一收尾；没有（例如只是预热过）就退回原路径
            if not owner.release(self._camera, "关闭详情页"):
                self._camera.stop()
                camera_service().release(self.state.did)
        else:
            camera_service().release(self.state.did)

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

    @staticmethod
    def _gang_text(gang, wireless: bool) -> str:
        """多键面板的行名。转无线那一路在后面标一句，否则用户会以为开关坏了。"""
        return f"{gang.label}（转无线）" if wireless else gang.label

    def _build_controls(self) -> None:
        card = self.state.card
        if not card.controls and not card.gangs:
            return
        lay = self._group("")
        if card.gangs:
            for g in card.gangs:
                wireless = g.is_wireless(self.state.values)
                # 不给 ``write=False``：这个属性本身是可写的，**是 mode 不让写**。
                # 两者混用会让"设备侧改成有线"之后这一行永远解不开（write 定死）。
                row = ControlRow(
                    g.power, self._gang_text(g, wireless), self._p, self._accent, "switch"
                )
                row.changed.connect(self._on_change)
                row.set_enabled(self.state.online, locked=wireless)
                self._rows.append(row)
                self._gang_rows[g.siid] = row
                lay.addWidget(row)
            return
        for ctl in card.controls:
            if ctl.prop is None:
                continue
            row = ControlRow(ctl.prop, ctl.label, self._p, self._accent, ctl.role, write=ctl.write)
            row.changed.connect(self._on_change)
            self._rows.append(row)
            lay.addWidget(row)
            if ctl.slot == "mode":
                self._mode_row = row
            # ``getattr``：验证脚本用 ``SimpleNamespace`` 鸭子类型喂过卡片
            if getattr(card, "speed_by_mode", None) and (
                ctl.slot in card.speed_by_mode.values()
            ):
                self._speed_rows[ctl.slot] = row
        self._sync_speed_rows()

    def _sync_speed_rows(self) -> None:
        """按当前模式只留一行风速（直吹风 = 0–100 连续量，自然风 = 档位）。

        规则来自 ``CardSpec.speed_by_mode``（型号级，见 ``adapters/dmaker.py``）：
        两个属性在规格里**同时存在、都可写**，谁生效是固件语义，猜不出来。
        没登记规则的设备（``speed_by_mode`` 为空）两行都留着——宁可多一行，
        也别把用户真能用的属性藏掉。

        模式值读不到（刚打开、设备离线）时 ``speed_slot`` 返回 ``None``，
        同样两行都留：先让人看见有什么，等值到了再收。
        """
        if not self._speed_rows:
            return
        card = self.state.card
        mode_ctl = card.bound("mode")
        mode_value = (
            self.state.get_prop(mode_ctl.prop) if mode_ctl is not None else None
        )
        rule = getattr(card, "speed_slot", None)
        want = rule(mode_value) if callable(rule) else None
        for slot, row in self._speed_rows.items():
            row.setVisible(want is None or slot == want)

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
            self._readout_rows.append(row)
            lay.addWidget(row)

    def _build_advanced(self) -> None:
        card = self.state.card
        if not card.advanced:
            return
        # 默认收起：这些属性不在轮询里，打开详情页就全读一遍等于白花一次批量读。
        # 行控件也等第一次展开才建，36 行下拉框的弹出层没必要提前造。
        self._adv_props: list[PropertySpec] = list(card.advanced)
        self._adv_rows: list[ControlRow] = []
        self._adv_open = False
        self._adv_loading = False
        self._adv_results: queue.SimpleQueue[tuple[dict[Any, Any], str]] = queue.SimpleQueue()

        lay = self._group("")
        self._adv_header = _AdvancedHeader(
            f"更多功能 · {len(self._adv_props)} 项", self._p
        )
        self._adv_header.toggled.connect(self._toggle_advanced)
        lay.addWidget(self._adv_header)

        self._adv_body = QWidget()
        body_lay = QVBoxLayout(self._adv_body)
        body_lay.setContentsMargins(0, 4, 0, 0)
        body_lay.setSpacing(0)
        lay.addWidget(self._adv_body)
        self._adv_body.setVisible(False)

    def _toggle_advanced(self) -> None:
        if self._adv_open:
            self._adv_open = False
            self._adv_header.set_open(False)
            self._adv_body.setVisible(False)
            return
        self._adv_open = True
        self._build_advanced_rows()
        self._adv_header.set_open(True)
        self._adv_body.setVisible(True)
        self._request_advanced()

    def _build_advanced_rows(self) -> None:
        if self._adv_rows:
            return
        st = self.store.devices.get(self.state.did)
        lay = self._adv_body.layout()
        for prop in self._adv_props:
            role = "switch" if prop.kind == "bool" else ("select" if prop.kind == "enum" else "slider")
            # 行名用 ``display_label``：解析期/型号级已经算好短名（含 category 上下文），
            # 这里再按字符数硬切会把「内屏显示视频位置(7档)」这种名字切坏；
            # 放不下由 ElidedLabel 自己省略，原文在 tooltip 里。
            row = ControlRow(prop, prop.display_label, self._p, self._accent, role)
            row.changed.connect(self._on_change)
            self._rows.append(row)
            self._adv_rows.append(row)
            lay.addWidget(row)
            # 先用已缓存的值（上次展开读到的）；没有就显示"未知"，别停在默认值
            value = st.values.get((prop.siid, prop.piid)) if st is not None else None
            if value is None:
                row.set_unknown()
            else:
                row.set_value(value)

    def _request_advanced(self) -> None:
        st = self.store.devices.get(self.state.did)
        if st is None:
            return
        if not st.online:
            self._adv_header.set_status("设备离线")
            return
        if self._adv_loading:
            self._adv_header.set_status("读取中…")
            return
        self._adv_loading = True
        self._adv_header.set_status("读取中…")
        # 走工作线程读，结果放队列回主线程：详情页关掉后回调也不碰控件（同摄像头那条路）。
        # 这里只捕获队列本身，回调里不再碰 self，窗口销毁后也不会炸。
        results = self._adv_results
        self.store.read_props(
            st.did,
            self._adv_props,
            lambda res, error: results.put((res, error)),
        )

    def _collect_advanced(self) -> None:
        while True:
            try:
                res, error = self._adv_results.get_nowait()
            except queue.Empty:
                break
            self._adv_loading = False
            if error:
                self._adv_header.set_status(error)
                continue
            ok = sum(1 for rv in res.values() if getattr(rv, "ok", False))
            total = len(self._adv_props)
            if ok >= total:
                self._adv_header.set_status(f"已读取 {ok} 项")
            elif ok:
                self._adv_header.set_status(f"{ok}/{total} 项可读")
            else:
                self._adv_header.set_status("读取失败，收起再展开可重试")

    # -- 数据 ------------------------------------------------------------
    def _on_change(self, siid: int, piid: int, value: object) -> None:
        self.store.set_value(self.state.did, (siid, piid), value)
        if self._mode_row is not None and (siid, piid) == self._mode_row.prop.key:
            # 用户刚选了模式：等 device 把 scene-mode 改过来之前，别被"无"擦掉
            self._mode_pending = True

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
        self._sync_ptz()
        if self._camera is not None and self._cam_live and not st.online:
            # 设备掉线时 P2P 会先断流再超时，主动收掉比等播放器报错快。
            # 先结束会话（含独立窗口），再画错误，避免又触发一次通道切换。
            self._cam_live = False
            self._cam_wants_play = False
            self._cam_poll.stop()
            self._cam_watchdog.stop()
            if not playbacks().release(self._camera, "设备离线"):
                self._camera.stop()
                camera_service().release(self.state.did)
            self._camera.show_error("设备离线，画面已断开")
        adv = set(self._adv_rows)
        gang_rows = set(self._gang_rows.values())
        # 模式变了就把另一条风速收起来（直吹风/自然风各管一个属性）
        self._sync_speed_rows()
        for row in self._rows:
            if row in gang_rows:
                continue  # 多键行单独处理：转无线时要跟着 mode 变灰
            row.set_enabled(st.online)
            if row is self._mode_row and st.card.mode_active is not None:
                self._sync_mode_row(st, row)
                continue
            value = st.values.get((row.prop.siid, row.prop.piid))
            if value is None and row in adv:
                # 「更多功能」的未读数不伪装成默认值；主控件保持原来的可点状态
                row.set_unknown()
            else:
                row.set_value(value)
        for g in st.card.gangs:
            row = self._gang_rows.get(g.siid)
            if row is None:
                continue
            # 模式可能在设备侧被改（米家 App 里点了转无线），每轮按最新值重算
            wireless = g.is_wireless(st.values)
            row.set_enabled(st.online, locked=wireless)
            row.set_value(st.values.get(g.power.key))
            want = prop_short(self._gang_text(g, wireless))
            if row.name.text() != want:
                row.name.setText(want)
        if self._adv_header is not None:
            self._collect_advanced()
        # 读数陈旧提示：只看 metrics（卡片上那几个数），用云端每个属性自己的
        # updateTime；离线时 footer 已经写明，不再叠一层。
        stamps = st.stamps
        for row in self._readout_rows:
            row.set_stale(stale_text(stamps.get(row.prop.key)) if st.online else "")
        self.titlebar.set_title(st.name)
        self.titlebar.set_subtitle(st.room or "")
        self._sync_hide_button()
        if not st.online:
            self._foot.setText("设备已离线，控制不可用")
        elif st.age > 1e6:
            self._foot.setText("尚未读取到数据")
        else:
            self._foot.setText(f"数据更新于 {int(st.age)} 秒前")

    def _sync_mode_row(self, st: DeviceState, row: ControlRow) -> None:
        """模式行的激活项以 ``scene-mode``(3,9) 为准，``mode``(2,5) 只用于写入。

        ``mode`` 在手调亮度/色温后不会变（实测一直是 5/6/7），拿它当激活判据
        会让下拉一直停在某个其实已经失效的预设上。数值对应关系两者一致（5–9），
        所以 scene 直接当 mode 的枚举值用；0 是客户端语义"无激活项"，枚举里
        并没有这一项，只能用占位文案表示，不能伪造一个能被写出去的选项。
        """
        try:
            v = int(st.mode_active_value())
        except (TypeError, ValueError):
            # 还没读到 / 值不合法：退回默认行为（按 mode 自己的值高亮），
            # 既不报错也不空高亮。
            row.set_value(st.values.get((row.prop.siid, row.prop.piid)))
            return
        if v == 0:
            # 用户刚选完、设备还没把 scene 改过来：先保留他的选择，
            # 否则下拉会在 600ms 内先跳回"无"、再跳回所选模式，闪两下。
            if not self._mode_pending:
                row.set_no_selection("无")
            return
        self._mode_pending = False
        if not row.select_value(v):
            # 1–4 是自定义场景、10 是流光，都不在 mode 枚举里
            row.set_no_selection("其他")


class _AdvancedHeader(QWidget):
    """「更多功能」的折叠标题：▶/▼ + 标题，右侧显示这次按需读取的状态。"""

    toggled = Signal()

    H = 30

    def __init__(self, title: str, palette: theme.Palette) -> None:
        super().__init__()
        self._p = palette
        self._title = title
        self._status = ""
        self._open = False
        self._hover = False
        self.setFixedHeight(self.H)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)

    def set_open(self, open_: bool) -> None:
        if self._open != open_:
            self._open = open_
            self.update()

    def set_status(self, text: str) -> None:
        if self._status != text:
            self._status = text
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        # 同卡片墙的房间标题：用 release，按下后拖开再松手不算点击
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.toggled.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self._p
        color = pal.text_soft if self._hover else pal.text_mute
        draw_text(p, QRect(0, 0, max(0, self.width() - 140), self.H), self._title,
                  size=12, color=color, weight=QFont.DemiBold)

        # 三角跟在标题后面：收起 ▶ / 展开 ▼
        font = QFont(p.font())
        font.setPixelSize(12)
        font.setWeight(QFont.DemiBold)
        x = QFontMetrics(font).horizontalAdvance(self._title) + 8
        cy = self.H / 2
        p.setPen(QPen(QColor(color), 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        path = QPainterPath()
        if self._open:
            path.moveTo(QPointF(x - 4, cy - 2))
            path.lineTo(QPointF(x, cy + 2))
            path.lineTo(QPointF(x + 4, cy - 2))
        else:
            path.moveTo(QPointF(x - 2, cy - 4))
            path.lineTo(QPointF(x + 2, cy))
            path.lineTo(QPointF(x - 2, cy + 4))
        p.drawPath(path)

        if self._status:
            draw_text(p, QRect(0, 0, self.width(), self.H), self._status,
                      size=11, color=pal.text_mute,
                      align=Qt.AlignRight | Qt.AlignVCenter)
        p.end()


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
