"""品类 → 卡片形态映射。

这是本项目"像不像原版米家"的核心。参考项目做的是 spec 驱动的通用面板：
bool 画开关、range 画滑块、value-list 画下拉——结果每台设备长得一样，一堆
用不到的属性把面板塞满（洗衣机 70 个属性、门锁 117 个）。

原版米家的做法是"按品类一套面板 + 厂商只改配置"（官方叫公版插件）。所以这里
按 **spec_type 里的品类段**（``urn:miot-spec-v2:device:<category>:...``）建表，
每个品类定义：

* ``layout``  —— 卡片长什么样（灯带亮度条、空调温控、多键面板、纯读数…）
* ``controls`` —— 卡片上直接给的高频控件（按属性名候选绑定，绑不上就不显示）
* ``metrics``  —— 副标题读数（温度/湿度/电量/功率…）
* ``advanced`` —— 没绑定的可写属性自动收进"更多功能"，而不是堆在面板上

绑定的关键点：**厂商会在属性名后加 ``-2`` / ``-3`` 后缀**（洗衣机里同时有
``target-temperature`` 和 ``target-temperature-2``），``spec.squash`` 负责兼容。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .adapters import MODEL_ADAPTERS, ModelAdapter, find_adapter
from .enum_text import enum_short
from .spec import ActionSpec, DeviceSpec, EventSpec, PropertySpec, squash, unit_symbol

# ---------------------------------------------------------------------------
# 属性槽位：一个"功能"可能对应多个可能的属性名
# ---------------------------------------------------------------------------

SLOTS: dict[str, tuple[str, ...]] = {
    "power": ("on",),
    # 电视的开关是 speaker-mode 服务里的 is-on；音箱根本没有开关，只能靠 play/pause
    "is_on": ("on", "is-on", "power"),
    "brightness": ("brightness",),
    "color_temp": ("color-temperature",),
    "rgb": ("color",),
    "mode": ("mode",),
    "target_temp": ("target-temperature",),
    "ir_mode": ("ir-mode",),
    "ir_temp": ("ir-temperature",),
    "fan_level": ("fan-level", "fan-speed", "fan-percent", "speed-level"),
    # 连续风速（这台风扇是 1–100）。和 ``fan_level`` 是**两个独立属性**，
    # 直吹风/自然风各管一个，谁的算数由型号适配说了算（``adapters/dmaker.py``
    # 的 ``speed_by_mode``），别在这里按名字猜。
    "fan_speed": ("speed-level", "fan-speed", "fan-percent"),
    "swing_v": ("vertical-swing", "vertical-swing-included-angle"),
    "swing_h": ("horizontal-swing", "horizontal-swing-included-angle"),
    # 扫风角度（30/60/90/120/140 这类离散角度）。和 ``swing_h`` 的名字有重叠，
    # 但它是**枚举**、那个是布尔，靠 ``Control.kind`` 分开，不会互相抢
    "swing_angle": (
        "horizontal-swing-included-angle",
        "vertical-swing-included-angle",
        "swing-angle",
    ),
    "position": ("target-position",),
    "current_position": ("current-position",),
    "battery": ("battery-level",),
    "temperature": ("temperature",),
    "humidity": ("relative-humidity", "humidity"),
    "pm25": ("pm2.5-density", "pm25-density"),
    "aqi": ("air-quality", "aqi"),
    "illumination": ("illumination", "brightness-of-illumination"),
    # 大部分人体传感器不上报"有人"属性，只发 motion-detected 事件（见 event_slots）
    "occupancy": ("occupancy-status", "motion-state", "has-someone", "people-exist"),
    "power_now": ("electric-power",),
    "energy": ("power-consumption",),
    "status": ("status", "run-status", "working-status"),
    "fault": ("fault",),
    # 门锁没有 lock-state，状态在 door-state。lock-action 是"本次开锁动作"，
    # 实测多数锁对它回读返回 -704002000（设备不支持），不能当状态用。
    "lock_state": ("lock-state",),
    "door_state": ("door-state",),
    "child_lock": ("physical-controls-locked", "child-lock"),
    "volume": ("volume",),
    "mute": ("mute",),
    "playing": ("playing-state",),
    "filter_life": ("filter-life-level",),
    "left_time": ("left-time", "left-time-2", "left-time-8"),
    "target_humidity": ("target-humidity",),
    "eco": ("eco",),
    "sleep": ("sleep-mode",),
}

# 品类 → 需要关注的事件名（属性拿不到的实时变化，只能靠事件）
EVENT_SLOTS: dict[str, tuple[str, ...]] = {
    "motion-sensor": ("motion-detected", "no-motion-detected"),
    "occupancy-sensor": ("motion-detected",),
    "lock": ("lock-event", "exception-occurred", "doorbell-pressed"),
    "remote-control": ("click", "double-click", "long-press"),
    "gateway": ("alarm",),
    "camera": ("motion-detected",),
    "washer": ("wash-completed", "fault"),
    "airer": ("fault",),
}


@dataclass(frozen=True, slots=True)
class Control:
    """卡片上的一个控件声明（未绑定 siid/piid）。"""

    slot: str  # SLOTS 的键
    label: str  # 中文名
    role: str  # switch | slider | stepper | select | toggle | readout
    write: bool = True
    service: str | None = None  # 优先在哪个服务里找
    show_when_off: bool = False  # 关机时是否仍显示（灯的亮度条需要）
    critical: bool = False  # 绑定失败要告警（说明映射表写错了）
    #: 这个槽位**只认某种形状**的属性：``"range"`` / ``"enum"`` / ``"bool"``。
    #: 空 = 不限。名字撞车的槽位必须写它：``swing_h`` 的候选名里既有布尔
    #: ``horizontal-swing`` 又有枚举 ``horizontal-swing-included-angle``，
    #: 没有这一条时绑到哪个取决于服务顺序。
    kind: str = ""
    #: 显式指定卡片上用什么控件：``"slider"`` / ``"stepper"`` / ``"chips"`` /
    #: ``"seg"`` / ``"toggle"``。空 = 按属性类型自动挑（见
    #: ``ui/card_controls.make_control``，**选型只有那一份**）。
    #: 为什么需要它：自动挑选只看类型，于是"1–100 的连续风速"和"16–30 的温度"
    #: 都成了步进器——前者点二十下才到头。类型相同、语义不同，只能由声明方点名。
    widget: str = ""
    #: 首选服务里找不到时，再去**所有服务**里找一遍（含 ``INTERNAL_SERVICES``）。
    #: 厂商常把正经属性塞进自定义服务：风扇的连续风速就在 ``dm-service`` 里，
    #: 而那个服务整体被当成"内部服务"不出现在"更多功能"里。
    search_all: bool = False

    def __post_init__(self) -> None:
        # 声明期报错：这两个字段都是"写错了不会崩、只会静默变丑"的类型
        if self.widget and self.widget not in WIDGET_KINDS:
            raise ValueError(
                f"控件 {self.slot!r} 的 widget={self.widget!r} 不认识"
                f"（可选 {list(WIDGET_KINDS)}）"
            )
        if self.kind and self.kind not in PROP_KINDS:
            raise ValueError(
                f"控件 {self.slot!r} 的 kind={self.kind!r} 不认识"
                f"（可选 {list(PROP_KINDS)}）"
            )


#: ``Control.widget`` 允许的取值，与 ``ui/card_templates.CardBody._pick`` 一一对应
WIDGET_KINDS = ("slider", "stepper", "seg", "chips", "toggle")
#: ``Control.kind`` 允许的取值，即 ``PropertySpec.kind`` 的三种形状
PROP_KINDS = ("range", "enum", "bool")


@dataclass(frozen=True, slots=True)
class CategoryProfile:
    category: str
    label: str
    layout: str
    icon: str
    accent: str
    span: int = 1  # 网格跨度：1=小卡 2=宽卡
    # 没有偏好记录时用哪个卡片样式。空串 = 全局 standard（1×1 产品图外壳）。
    # 只写 id 字面量：具体样式表在 ui/card_templates.py，core 不反向依赖 ui，
    # 也不校验这里的拼写——不认识的 id 会被 resolve_card_style 兜回 standard。
    default_style: str = ""
    primary_service: str | None = None
    controls: tuple[Control, ...] = ()
    metrics: tuple[Control, ...] = ()
    quick_actions: tuple[str, ...] = ()  # 直接暴露的动作名（如 start-wash）
    # 没有"开/关"属性的设备（电视、音箱）用一对动作代替
    power_actions: tuple[str, str] | None = None

    def control(self, slot: str) -> Control | None:
        for c in self.controls:
            if c.slot == slot:
                return c
        return None


def _c(slot: str, label: str, role: str, **kw: Any) -> Control:
    return Control(slot=slot, label=label, role=role, **kw)


# ---------------------------------------------------------------------------
# 品类表
# ---------------------------------------------------------------------------

PROFILES: dict[str, CategoryProfile] = {
    # --- 灯 -------------------------------------------------------------
    "light": CategoryProfile(
        category="light",
        label="灯",
        layout="light",
        icon="light",
        accent="#F0A93B",
        controls=(
            _c("power", "开关", "switch", critical=True),
            _c("brightness", "亮度", "slider", show_when_off=True),
            _c("color_temp", "色温", "slider", show_when_off=True),
            _c("mode", "模式", "select"),
        ),
        metrics=(_c("illumination", "照度", "readout", write=False),),
    ),
    # --- 开关面板（多键）------------------------------------------------
    "switch": CategoryProfile(
        category="switch",
        label="开关",
        layout="switch",
        icon="switch",
        accent="#4A82F7",
        controls=(_c("power", "开关", "switch"),),
        metrics=(),
    ),
    # --- 插座 -----------------------------------------------------------
    "outlet": CategoryProfile(
        category="outlet",
        label="插座",
        layout="outlet",
        icon="outlet",
        accent="#4A82F7",
        controls=(_c("power", "开关", "switch", critical=True),),
        metrics=(
            _c("power_now", "功率", "readout", write=False),
            _c("energy", "用电量", "readout", write=False),
        ),
    ),
    # --- 空调 -----------------------------------------------------------
    "air-conditioner": CategoryProfile(
        category="air-conditioner",
        label="空调",
        layout="climate",
        icon="ac",
        accent="#39A0F5",
        # 空调的日常操作就两件：调温度、换模式，产品图在这两件事上帮不上忙。
        # 红外空调是另一个 Profile（IR_CLIMATE），属性是 ir-*，这条不影响它。
        default_style="temp_control",
        primary_service="air-conditioner",
        controls=(
            _c("power", "开关", "switch", critical=True),
            _c("target_temp", "温度", "stepper"),
            _c("mode", "模式", "select", critical=True),
            _c("fan_level", "风速", "select", service="fan-control"),
            _c("swing_v", "扫风", "toggle", service="fan-control"),
        ),
        metrics=(
            _c("temperature", "室温", "readout", write=False),
            _c("humidity", "湿度", "readout", write=False),
        ),
    ),
    # --- 风扇 -----------------------------------------------------------
    # 登记的是"可能有的功能"，**有没有由规格回答**（绑不上就跳过）。所以这
    # 一台设备有 4 档 + 连续风速 + 左右扫风 + 角度 + 童锁 + 定时，另一台只有
    # 3 档，同一张表都成立，不需要按型号分支，也不需要谁去维护"哪些风扇有上下
    # 扫风"。唯一按型号的是"哪个模式下哪个风速算数"，见 ``speed_by_mode``。
    "fan": CategoryProfile(
        category="fan",
        label="风扇",
        layout="fan",
        icon="fan",
        accent="#2FBFAF",
        controls=(
            _c("power", "开关", "switch", critical=True),
            # 连续风速先绑：它比档位更"精确"，两个都有时卡片默认展示它
            _c(
                "fan_speed", "风速", "slider",
                kind="range", widget="slider", search_all=True,
            ),
            _c("fan_level", "档位", "select", kind="enum"),
            # 模式和角度写成分段轨道：都是"互斥的几档"，选中态是实心品类色，
            # 比分立胶囊更容易一眼看出当前是哪档（原来的 24% 混色在开启态的
            # 卡片上几乎看不出来，2026-11-12 实测）。
            _c("mode", "模式", "select", kind="enum", widget="seg"),
            _c("swing_h", "左右扫风", "toggle", kind="bool"),
            _c("swing_v", "上下扫风", "toggle", kind="bool"),
            _c("swing_angle", "角度", "select", kind="enum", widget="seg"),
            _c("child_lock", "童锁", "toggle", kind="bool"),
        ),
    ),
    # --- 净化器 ---------------------------------------------------------
    "air-purifier": CategoryProfile(
        category="air-purifier",
        label="空气净化器",
        layout="purifier",
        icon="purifier",
        accent="#2FBFAF",
        primary_service="air-purifier",
        controls=(
            _c("power", "开关", "switch", critical=True),
            _c("mode", "模式", "select", critical=True),
            _c("fan_level", "风速", "select"),
        ),
        metrics=(
            _c("pm25", "PM2.5", "readout", write=False),
            _c("temperature", "室温", "readout", write=False),
            _c("humidity", "湿度", "readout", write=False),
            _c("filter_life", "滤芯", "readout", write=False),
        ),
    ),
    # --- 晾衣架 ---------------------------------------------------------
    "airer": CategoryProfile(
        category="airer",
        label="晾衣架",
        layout="cover",
        icon="airer",
        accent="#6E7BF2",
        primary_service="airer",
        controls=(
            _c("position", "位置", "slider", critical=True),
            _c("current_position", "当前位置", "readout", write=False),
        ),
        metrics=(
            _c("status", "状态", "readout", write=False),
            _c("fault", "故障", "readout", write=False),
        ),
    ),
    # --- 浴霸 -----------------------------------------------------------
    "bath-heater": CategoryProfile(
        category="bath-heater",
        label="浴霸",
        layout="climate",
        icon="heater",
        accent="#E8834A",
        primary_service="ptc-bath-heater",
        controls=(
            _c("mode", "模式", "select", critical=True),
            _c("target_temp", "目标温度", "stepper"),
            _c("fan_level", "换气", "select", service="fan-control"),
        ),
        metrics=(_c("temperature", "浴室温度", "readout", write=False),),
    ),
    # --- 窗帘（规格里通常归属 curtain 品类）-----------------------------
    "curtain": CategoryProfile(
        category="curtain",
        label="窗帘",
        layout="cover",
        icon="curtain",
        accent="#6E7BF2",
        controls=(
            _c("position", "开合度", "slider", critical=True),
            _c("current_position", "当前", "readout", write=False),
        ),
    ),
    # --- 传感器 ---------------------------------------------------------
    "motion-sensor": CategoryProfile(
        category="motion-sensor",
        label="人体传感器",
        layout="sensor",
        icon="motion",
        accent="#8A8F99",
        metrics=(
            _c("occupancy", "有人移动", "readout", write=False),
            _c("illumination", "照度", "readout", write=False),
            _c("battery", "电量", "readout", write=False),
        ),
    ),
    "occupancy-sensor": CategoryProfile(
        category="occupancy-sensor",
        label="存在传感器",
        layout="sensor",
        icon="presence",
        accent="#8A8F99",
        # 这类设备卡片上空着一块产品图没有意义——它唯一要回答的就是"有没有人"。
        # 默认挂只读读数，用户仍可在右键菜单里选回「标准」（产品图）。
        default_style="presence_state",
        metrics=(
            # 指标名要是**名词**（它同时是只读读数的上方小字标签）。写成"有人"
             # 的话，无人状态会变成大字「无人」+ 小字「有人」，自相矛盾。
            _c("occupancy", "人体状态", "readout", write=False),
            _c("illumination", "照度", "readout", write=False),
            _c("battery", "电量", "readout", write=False),
        ),
    ),
    "temperature-humidity-sensor": CategoryProfile(
        category="temperature-humidity-sensor",
        label="温湿度计",
        layout="sensor",
        icon="thermo",
        accent="#8A8F99",
        metrics=(
            _c("temperature", "温度", "readout", write=False),
            _c("humidity", "湿度", "readout", write=False),
            _c("battery", "电量", "readout", write=False),
        ),
    ),
    "environment-sensor": CategoryProfile(
        category="environment-sensor",
        label="环境传感器",
        layout="sensor",
        icon="thermo",
        accent="#8A8F99",
        metrics=(
            _c("temperature", "温度", "readout", write=False),
            _c("humidity", "湿度", "readout", write=False),
            _c("pm25", "PM2.5", "readout", write=False),
        ),
    ),
    # --- 门锁 -----------------------------------------------------------
    "lock": CategoryProfile(
        category="lock",
        label="门锁",
        layout="lock",
        icon="lock",
        accent="#E8A33D",
        primary_service="lock",
        controls=(_c("child_lock", "童锁", "toggle"),),
        metrics=(
            _c("door_state", "门状态", "readout", write=False),
            _c("lock_state", "最近动作", "readout", write=False),
            _c("battery", "电量", "readout", write=False),
        ),
    ),
    # --- 摄像头 ---------------------------------------------------------
    "camera": CategoryProfile(
        category="camera",
        label="摄像头",
        layout="camera",
        icon="camera",
        accent="#5A6B85",
        primary_service="camera-control",
        controls=(_c("power", "开关", "switch"),),
    ),
    # --- 音箱 / 电视 ----------------------------------------------------
    "speaker": CategoryProfile(
        category="speaker",
        label="音箱",
        layout="media",
        icon="speaker",
        accent="#7C6BD6",
        controls=(_c("volume", "音量", "slider"),),
        metrics=(
            _c("playing", "播放状态", "readout", write=False),
            _c("mute", "静音", "readout", write=False),
        ),
        quick_actions=("play", "pause", "next", "previous"),
        power_actions=("play", "pause"),
    ),
    "television": CategoryProfile(
        category="television",
        label="电视",
        layout="media",
        icon="tv",
        accent="#4A5568",
        primary_service="television",
        controls=(
            _c("is_on", "开关", "switch"),
            _c("volume", "音量", "slider"),
        ),
        metrics=(_c("playing", "播放状态", "readout", write=False),),
        quick_actions=("turn-on", "turn-off", "press-home"),
        power_actions=("turn-on", "turn-off"),
    ),
    # --- 网关 / 遥控器 --------------------------------------------------
    "gateway": CategoryProfile(
        category="gateway",
        label="网关",
        layout="hub",
        icon="gateway",
        accent="#8A8F99",
        metrics=(),
    ),
    "remote-control": CategoryProfile(
        category="remote-control",
        label="遥控器",
        layout="hub",
        icon="remote",
        accent="#8A8F99",
        metrics=(_c("battery", "电量", "readout", write=False),),
    ),
    # --- 家电 -----------------------------------------------------------
    "washer": CategoryProfile(
        category="washer",
        label="洗衣机",
        layout="appliance",
        icon="washer",
        accent="#4A82F7",
        primary_service="washer",
        controls=(
            _c("power", "开关", "switch"),
            _c("mode", "模式", "select"),
            _c("target_temp", "水温", "stepper"),
        ),
        metrics=(
            _c("status", "状态", "readout", write=False),
            _c("left_time", "剩余", "readout", write=False),
            _c("fault", "故障", "readout", write=False),
        ),
        quick_actions=("start-wash", "pause", "stop-washing"),
    ),
    "hood": CategoryProfile(
        category="hood",
        label="油烟机",
        layout="appliance",
        icon="hood",
        accent="#4A5568",
        primary_service="hood",
        controls=(
            _c("power", "开关", "switch"),
            _c("fan_level", "风量", "select"),
        ),
    ),
    "oven": CategoryProfile(
        category="oven",
        label="烤箱",
        layout="appliance",
        icon="oven",
        accent="#E8834A",
        primary_service="oven",
        controls=(
            _c("power", "开关", "switch"),
            _c("mode", "模式", "select"),
            _c("target_temp", "温度", "stepper"),
        ),
        metrics=(_c("status", "状态", "readout", write=False),),
    ),
    "scale": CategoryProfile(
        category="scale",
        label="体脂秤",
        layout="sensor",
        icon="scale",
        accent="#8A8F99",
        primary_service="scale",
        metrics=(_c("battery", "电量", "readout", write=False),),
    ),
    # --- 其他常见品类（家里暂时没有，先备好，避免出现"通用卡片"）--------
    "humidifier": CategoryProfile(
        category="humidifier",
        label="加湿器",
        layout="purifier",
        icon="humidifier",
        accent="#4FB0C6",
        controls=(
            _c("power", "开关", "switch", critical=True),
            _c("mode", "模式", "select"),
            _c("target_humidity", "目标湿度", "stepper"),
        ),
        metrics=(
            _c("humidity", "湿度", "readout", write=False),
            _c("temperature", "温度", "readout", write=False),
        ),
    ),
    "vacuum": CategoryProfile(
        category="vacuum",
        label="扫地机",
        layout="appliance",
        icon="vacuum",
        accent="#4A82F7",
        controls=(
            _c("power", "开关", "switch", critical=True),
            _c("mode", "模式", "select"),
        ),
        metrics=(
            _c("status", "状态", "readout", write=False),
            _c("battery", "电量", "readout", write=False),
        ),
        quick_actions=("start-sweep", "stop-sweeping", "return-to-charge"),
    ),
    "water-purifier": CategoryProfile(
        category="water-purifier",
        label="净水器",
        layout="appliance",
        icon="water",
        accent="#4FB0C6",
        controls=(_c("power", "开关", "switch"),),
        metrics=(
            _c("status", "状态", "readout", write=False),
            _c("filter_life", "滤芯", "readout", write=False),
        ),
    ),
}


# 空调的红外遥控版本（米家万能遥控/空调伴侣）没有 on 属性，只有 ir-mode / ir-temperature
IR_CLIMATE = CategoryProfile(
    category="air-conditioner",
    label="空调（红外）",
    layout="ir_climate",
    icon="ac",
    accent="#39A0F5",
    primary_service="ir-aircondition-control",
    controls=(
        _c("ir_mode", "模式", "select", critical=True),
        _c("ir_temp", "温度", "stepper", critical=True),
    ),
)


# 这些服务里的可写属性属于"厂商调试/内部参数"，不进"更多功能"
INTERNAL_SERVICES = {
    "panel",
    "system-parm",
    "machine-state",
    "air-conditioner-dev-mode",
    "custom-service",
    "dm-service",
    "direct-connect",
    "flag-bit",
    "debug",
    "sensor-settings",
    "scene",
    "device-group",
    "user-management",
    "online-data",
    "offline-data",
}

# ---------------------------------------------------------------------------
# 型号级适配
# ---------------------------------------------------------------------------
# 规则本身在 ``mihome/core/adapters/``：一个厂商一个文件，一条型号一条记录，
# 增删改都只动那个包，这里不用变。消费链路仍是四步：
#
#   1. ``find_adapter(model)`` 精确命中型号（查不到就整条跳过）；
#   2. 命中的规则解析进 ``CardSpec.extra`` / ``CardSpec.mode_active``；
#   3. store 的 ``_card_props`` 把 ``extra`` 并入轮询与寻址登记；
#   4. 界面统一用 ``st.mode_active_value()`` 取值，不自己拼 siid/piid。
#
# 属性在 spec 里找不到、或运行时读不到，``extra`` / ``mode_active`` 就是空的，
# 一切回到默认行为（模式高亮按 ``mode``(2,5)），不报错、不空高亮。

# 属性名里出现这些词，基本是调试/参数，不暴露
_INTERNAL_NAME_HINTS = (
    "parm",
    "debug",
    "bitmap",
    "serial",
    "version",
    "miro",
    "threshold",
    "trigger-history",
    "utctime",
)

GENERIC = CategoryProfile(
    category="__generic__",
    label="设备",
    layout="generic",
    icon="device",
    accent="#8A8F99",
    controls=(_c("power", "开关", "switch"),),
    metrics=(
        _c("temperature", "温度", "readout", write=False),
        _c("humidity", "湿度", "readout", write=False),
        _c("battery", "电量", "readout", write=False),
    ),
)


def profile_for(category: str, spec: DeviceSpec | None = None) -> CategoryProfile:
    """按品类取面板。同一品类可能有多种形态，用能力特征区分。"""
    if category == "air-conditioner" and spec is not None and spec.find("ir-mode"):
        return IR_CLIMATE
    return PROFILES.get(category, GENERIC)


def known_categories() -> tuple[str, ...]:
    return tuple(sorted(PROFILES))


# ---------------------------------------------------------------------------
# 绑定结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BoundControl:
    slot: str
    label: str
    role: str
    prop: PropertySpec | None
    write: bool
    #: 声明方点名的控件类型（``Control.widget``），空 = 按属性类型自动挑
    widget: str = ""

    @property
    def siid(self) -> int | None:
        return self.prop.siid if self.prop else None

    @property
    def piid(self) -> int | None:
        return self.prop.piid if self.prop else None

    @property
    def key(self) -> str:
        return f"{self.slot}:{self.prop.method}" if self.prop else f"{self.slot}:unbound"


@dataclass(frozen=True, slots=True)
class Gang:
    """多键面板的一路。siid 就是虚拟子设备 did 里 ``.sN`` 的 N。"""

    siid: int
    label: str
    power: PropertySpec
    room: str = ""
    #: 这一路的 ``mode`` 属性（"有线开关 / 无线开关"，即米家设置里的"转无线"）。
    #: 规格里没有就是 ``None``——纯插座、老面板、部分单火开关都没有这个属性。
    mode: PropertySpec | None = None
    #: ``mode`` 的取值里表示"仅无线"的那一项（见 :func:`wireless_value`）。
    #: 设备上报值等于它时，这一路是转无线：继电器常通，按键不再切换。
    wireless_value: int | None = None

    def is_wireless(self, values: Mapping[tuple[int, int], Any]) -> bool:
        """这一路当前是不是"转无线"。

        判据是**设备上报的 mode 值**，不是名字、不是型号：同一块面板上可以只有
        其中两三路转无线（实测：三开单控只有左键是 1）。
        """
        if self.mode is None or self.wireless_value is None:
            return False
        return values.get(self.mode.key) == self.wireless_value


def wireless_value(prop: PropertySpec | None) -> int | None:
    """在 ``mode`` 的值列表里认出"仅无线"那一项，认不出返回 ``None``。

    只能用文案判：各家写的不一样（"无线开关" / "无线模式" / "无线"），而"有线"
    那项一定带"有线"（"有线开关" / "有线和无线开关"），所以判据是
    **含"无线"且不含"有线"**。要求恰好一项命中——认不准就当没有这个能力，
    宁可不锁点击，也不能把有线键误锁成不能操作。
    """
    if prop is None:
        return None
    hits = [
        o.value
        for o in prop.value_list
        if ("无线" in o.display or "wireless" in o.display.lower())
        and ("有线" not in o.display and "wired" not in o.display.lower())
    ]
    return hits[0] if len(hits) == 1 else None


@dataclass
class CardSpec:
    category: str
    profile: CategoryProfile
    #: 设备 did。样式能不能用有时要查**这台设备**的实测结论（极客版入口就靠
    #: ``core.geek`` 的探测缓存，见 ``CardStyle.ability``），光有型号不够。
    did: str = ""
    #: 设备型号（``DeviceNode.model``）。品类太粗的地方要靠它收窄：``gateway``
    #: 这一类里混着普通多模网关、遥控器，只有中枢网关才支持极客版那两条 RPC
    #: （见 ``CardStyle.ability``）。
    model: str = ""
    controls: list[BoundControl] = field(default_factory=list)
    metrics: list[BoundControl] = field(default_factory=list)
    gangs: list[Gang] = field(default_factory=list)
    advanced: list[PropertySpec] = field(default_factory=list)
    #: 只参与轮询、**不在卡片上渲染**的属性（型号级适配，见 ``MODEL_ADAPTERS``）。
    #: 典型是"当前实际生效的预设"这类只读状态：卡片没位置放它，但界面判断
    #: 激活态时必须实时。未适配的机型这里是空列表，轮询集合与以前完全一致。
    extra: list[PropertySpec] = field(default_factory=list)
    #: 模式激活判据用的属性（型号适配；``None`` = 用 ``mode`` 自己的值，默认行为）
    mode_active: PropertySpec | None = None
    #: **型号级**：同一功能有两套属性时，"哪个模式下用哪个槽位"。键是 ``mode``
    #: 的取值，值是槽位名（``fan_level`` / ``fan_speed``）。空 = 没有这条规则，
    #: 卡片按"档位优先、有连续量才用连续量"的通用顺序挑。
    #: 为什么放这里而不是样式里：这台风扇的规格里 ``speed-level`` 与 ``fan-level``
    #: **同时存在**且都可写，哪个算数完全取决于固件语义——这是型号事实，
    #: 不是品类约定（见 ``adapters/dmaker.py``）。
    speed_by_mode: dict[int, str] = field(default_factory=dict)
    actions: list[ActionSpec] = field(default_factory=list)
    events: list[EventSpec] = field(default_factory=list)
    power_actions: tuple[ActionSpec, ActionSpec] | None = None
    missing: list[str] = field(default_factory=list)  # 声明了但没绑上的关键槽位

    @property
    def layout(self) -> str:
        return self.profile.layout

    @property
    def accent(self) -> str:
        return self.profile.accent

    @property
    def icon(self) -> str:
        return self.profile.icon

    @property
    def span(self) -> int:
        return self.profile.span

    @property
    def default_style(self) -> str:
        """品类声明的默认样式 id；空串表示用全局 standard。"""
        return self.profile.default_style

    def control(self, slot: str) -> BoundControl | None:
        for c in self.controls:
            if c.slot == slot:
                return c
        return None

    def bound(self, slot: str) -> BoundControl | None:
        """按槽位取绑定结果，**先控件后读数**。

        ``CardStyle`` 的 body 既能放可写控件（亮度滑条），也能放只读读数
        （有人/无人）。这两类在 ``CardSpec`` 里分属两个 list，但样式只关心
        "这个槽位在这台设备上绑到了什么"，不该被迫知道它当初登记在哪一边。
        """
        hit = self.control(slot)
        if hit is not None:
            return hit
        for m in self.metrics:
            if m.slot == slot:
                return m
        return None

    def speed_slot(self, mode_value: Any = None) -> str | None:
        """按当前模式值返回"风速该用哪个槽位"；没有双速度规则时返回 ``None``。

        只在 ``speed_by_mode`` 非空时才有答案（那是型号级事实）。模式值读不到、
        或不在表里（固件加了新模式）时也返回 ``None``——调用方退到通用顺序，
        别因为一个未知值把风速整块藏起来。
        """
        if not self.speed_by_mode:
            return None
        try:
            key = int(mode_value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return self.speed_by_mode.get(key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "layout": self.layout,
            "icon": self.icon,
            "accent": self.accent,
            "controls": [
                {
                    "slot": c.slot,
                    "label": c.label,
                    "role": c.role,
                    "siid": c.siid,
                    "piid": c.piid,
                    "write": c.write,
                    "kind": c.prop.kind if c.prop else None,
                    "range": list(c.prop.value_range) if c.prop and c.prop.value_range else None,
                    "unit": c.prop.unit_text if c.prop else "",
                }
                for c in self.controls
            ],
            "metrics": [
                {"slot": m.slot, "label": m.label, "siid": m.siid, "piid": m.piid, "unit": m.prop.unit_text if m.prop else ""}
                for m in self.metrics
            ],
            "gangs": [
                {
                    "siid": g.siid,
                    "label": g.label,
                    "room": g.room,
                    "mode": f"{g.mode.method}" if g.mode else None,
                    "wireless_value": g.wireless_value,
                }
                for g in self.gangs
            ],
            "advanced": [
                {"method": f"{p.siid}.{p.piid}", "label": short_label(p.label), "raw": p.label, "kind": p.kind}
                for p in self.advanced
            ],
            "actions": [a.name for a in self.actions],
            "events": [e.name for e in self.events],
            "power_actions": [a.name for a in self.power_actions] if self.power_actions else None,
            "missing": self.missing,
        }


# ---------------------------------------------------------------------------
# 绑定
# ---------------------------------------------------------------------------


def _primary_service_name(spec: DeviceSpec, prof: CategoryProfile) -> str | None:
    if prof.primary_service:
        return prof.primary_service
    if spec.services_named(prof.category):
        return prof.category
    # 退一步：第一个带可写属性的服务
    for s in spec.services:
        if s.name == "device-information":
            continue
        if any(p.writeable for p in s.properties):
            return s.name
    return None


def _bind(spec: DeviceSpec, ctl: Control, primary: str | None) -> BoundControl:
    names = SLOTS.get(ctl.slot, (ctl.slot,))
    prop = spec.find(
        *names, service=ctl.service or primary, need_write=ctl.write, kind=ctl.kind
    )
    # 四条兜底，顺序有意义：
    # 1) 声明了服务 → 整个规格里再找一遍（服务名因型号而异，写死的可能不存在）；
    # 2) ``search_all`` → 连"内部服务"也翻（``dm-service`` 里的连续风速）；
    # 3) 只读兜底：有些设备的"开关"是只读上报的，此时按只读读数展示。
    if prop is None and ctl.service:
        prop = spec.find(*names, need_write=ctl.write, kind=ctl.kind)
    if prop is None and ctl.search_all:
        # 这一轮**不带 service**：``find`` 的 service 只是"优先"，但主机服务里
        # 没有时它仍会返回候选里的第一条；真正挡住 ``dm-service`` 的不是这里，
        # 是 ``_advanced`` 的服务过滤。所以这里显式关掉优先项即可。
        prop = spec.find(*names, need_write=ctl.write, kind=ctl.kind)
    write = ctl.write
    if prop is None and ctl.write:
        prop = spec.find(*names, service=ctl.service or primary, kind=ctl.kind)
        write = False
    return BoundControl(
        slot=ctl.slot,
        label=ctl.label,
        role=ctl.role,
        prop=prop,
        write=write,
        widget=ctl.widget,
    )


def build_card(
    spec: DeviceSpec,
    *,
    did: str | None = None,
    model: str | None = None,
    gang_names: Mapping[int, str] | None = None,
    gang_rooms: Mapping[int, str] | None = None,
) -> CardSpec:
    """把一份 DeviceSpec 绑定成一张卡片描述。

    ``did``: 设备 id。只有需要**按这台设备**的实测结论决定样式时才用得上
    （极客版入口靠 ``core.geek`` 的探测缓存），其余地方可以不传。
    ``model``: 设备节点上报的型号（``DeviceNode.model``，如
    ``yeelink.light.lamp22``），是 ``MODEL_ADAPTERS`` 的唯一查找键。
    不传 = 不做型号适配（走默认路径）；别拿 ``spec.model`` 顶替——
    那是 URN 里的型号段，形式不同（``yeelink-lamp22``）。
    ``gang_names``: siid → 该路的名字（来自 ``did.sN`` 虚拟设备），决定按键标签。
    """
    prof = profile_for(spec.category, spec)
    primary = _primary_service_name(spec, prof)
    card = CardSpec(
        category=spec.category, profile=prof, did=did or "", model=model or ""
    )

    for ctl in prof.controls:
        bound = _bind(spec, ctl, primary)
        if bound.prop is None:
            if ctl.critical:
                card.missing.append(ctl.slot)
            continue
        card.controls.append(bound)

    for ctl in prof.metrics:
        bound = _bind(spec, ctl, primary)
        if bound.prop is not None:
            card.metrics.append(bound)

    if prof.layout == "switch":
        card.gangs = _gangs(spec, gang_names, gang_rooms)
        card.controls = []

    all_actions = {a.name: a for a in spec.actions()}
    if prof.quick_actions:
        card.actions = [all_actions[n] for n in prof.quick_actions if n in all_actions]
    if prof.power_actions:
        on_name, off_name = prof.power_actions
        if on_name in all_actions and off_name in all_actions:
            card.power_actions = (all_actions[on_name], all_actions[off_name])

    wanted_events = EVENT_SLOTS.get(spec.category, ())
    if wanted_events:
        card.events = [e for e in spec.events() if e.name in wanted_events]

    card.advanced = _advanced(spec, card)

    # 型号级适配：**精确命中 model 字符串**才生效（规则在 mihome/core/adapters/）。
    # 属性名在 spec 里找不到就当作没登记，安全退化到默认行为。
    adapter = find_adapter(model)
    if adapter is not None:
        for name in adapter.extra:
            found = spec.find_all(name)
            if found:
                card.extra.append(found[0])
        active = next((p for p in card.extra if p.name == adapter.mode_active), None)
        if active is not None and any(c.slot == "mode" for c in card.controls):
            card.mode_active = active
        if adapter.speed_by_mode:
            _, table = adapter.speed_by_mode
            # 只留下**真的绑上了**的槽位：这台设备缺某个属性时，规则表里指向
            # 空槽的那一条留着也没用，还会让卡片去显示一个不存在的控件。
            card.speed_by_mode = {
                value: slot
                for value, slot in table.items()
                if (b := card.control(slot)) is not None and b.prop is not None
            }
    return card


def _gangs(
    spec: DeviceSpec,
    names: Mapping[int, str] | None,
    rooms: Mapping[int, str] | None,
) -> list[Gang]:
    gangs: list[Gang] = []
    for svc in spec.services:
        if svc.name not in ("switch", "switch-sensor"):
            continue
        on = next((p for p in svc.properties if p.name == "on" and p.writeable), None)
        if on is None:
            continue
        # 只看**同一个服务**里的 mode：switch-sensor 也有个叫 mode 的属性
        # （"极速单击模式/多功能模式"），混着找会把按键手势模式当成有线无线。
        mode = next(
            (p for p in svc.properties if p.name == "mode" and p.value_list), None
        )
        raw = (names or {}).get(svc.siid, "")
        label = gang_label(raw, len(gangs) + 1)
        room = (rooms or {}).get(svc.siid, "")
        gangs.append(
            Gang(
                siid=svc.siid,
                label=label,
                power=on,
                room=room,
                mode=mode,
                wireless_value=wireless_value(mode),
            )
        )
    return gangs


def _advanced(spec: DeviceSpec, card: CardSpec) -> list[PropertySpec]:
    """没被卡片用到的可写属性 → "更多功能"页，而不是堆在面板上。"""
    used = {c.prop.key for c in (*card.controls, *card.metrics) if c.prop}
    used |= {g.power.key for g in card.gangs}
    # 每一路的 mode（有线/转无线）已经用在按键判断上了，别再重复出现在"更多功能"里
    used |= {g.mode.key for g in card.gangs if g.mode is not None}
    out: list[PropertySpec] = []
    seen_names: set[str] = set()  # 同一属性名在多服务里重复（厂家复制的），只留一份
    for p in spec.properties:
        if not p.writeable or p.key in used:
            continue
        if p.service in INTERNAL_SERVICES:
            continue
        low = p.name.lower()
        if any(h in low for h in _INTERNAL_NAME_HINTS):
            continue
        key = squash(p.name)
        if key in seen_names:
            continue
        seen_names.add(key)
        out.append(p)
    return out


def short_label(text: str, limit: int = 12) -> str:
    """规格里的 comment 常是一整句话（"0关闭待机息屏，待机时屏幕一直亮…"），
    卡片上要截断到能放下的长度。

    具体的映射与截断规则都在 ``core/enum_text.py``：先查人工映射表，再套有规律的
    规则（``C1 CLEAR`` / ``Level3`` / ``Area 5``），最后才切分隔符或截断。
    """
    if not text:
        return ""
    return enum_short(text, limit=limit)


_GANG_PREFIX_RE = re.compile(r"^(左键|右键|中键|上键|下键|按键\s*\d+)\s*[-—·－]\s*")


def gang_label(name: str, index: int) -> str:
    """多键面板每一路的标签。

    用户在米家里没改过名的按键，云端给的是自动生成的 ``左键-小米米家智能开关（三开单控）``，
    直接铺在卡片上会撑爆。这种情况只留"左键"；改过名的（"厨房灯"）原样保留。
    """
    text = (name or "").strip()
    text = re.split(r"[（(]", text)[0].strip()
    m = _GANG_PREFIX_RE.match(text)
    if m and len(text) > len(m.group(0)) + 2:
        return m.group(1)
    if not text:
        return f"按键{index}"
    return text


# ---------------------------------------------------------------------------
# 值格式化（卡片副标题、详情页读数共用）
# ---------------------------------------------------------------------------


def format_value(prop: PropertySpec | None, value: Any) -> str:
    """把云端原始值变成人能看懂的短文本。"""
    if prop is None or value is None:
        return "—"
    if prop.format == "bool":
        return "开" if value else "关"
    if prop.value_list:
        opt = prop.option(value)
        # 短名：云端原文常是整句或英文（见 core/enum_text.py），读数行只有 15px 一个字
        return opt.display_short if opt else str(value)
    if isinstance(value, (int, float)):
        unit = prop.unit_text
        if prop.unit == "percentage":
            return f"{value:g}%"
        if prop.unit == "celsius":
            return f"{value:g}°C"
        if prop.unit == "kelvin":
            return f"{int(value)}K"
        if unit in ("分钟", "小时", "秒", "天"):
            return f"{value:g}{unit}"
        if isinstance(value, float):
            return f"{value:g}{(' ' + unit) if unit else ''}"
        return f"{value}{(' ' + unit) if unit else ''}"
    return str(value)


def summary_line(card: CardSpec, values: Mapping[tuple[int, int], Any]) -> str:
    """卡片副标题：用前两个有值的指标拼一句。

    只认 ``card.metrics``，不关心卡片当前挂了什么 body——同一台设备换个样式，
    副标题不动。这样详情页（``DeviceState.subtitle``）和卡片读的是同一个实现，
    不需要在两处各维护一份"哪些项已经被画过"的账。
    """
    parts: list[str] = []
    for m in card.metrics:
        if m.prop is None:
            continue
        v = values.get(m.prop.key)
        if v is None:
            continue
        parts.append(format_value(m.prop, v))
        if len(parts) >= 2:
            break
    return " · ".join(parts)


def coverage(card: CardSpec, spec: DeviceSpec) -> dict[str, int]:
    """给体检报告用的覆盖率统计。"""
    used = {c.prop.key for c in (*card.controls, *card.metrics) if c.prop}
    used |= {g.power.key for g in card.gangs}
    used |= {g.mode.key for g in card.gangs if g.mode is not None}
    used |= {p.key for p in card.advanced}
    total = len(spec.properties)
    return {
        "total": total,
        "on_card": len({c.prop.key for c in (*card.controls, *card.metrics) if c.prop}),
        "advanced": len(card.advanced),
        "unused": total - len(used),
    }


__all__ = [
    "GENERIC",
    "MODEL_ADAPTERS",
    "PROFILES",
    "SLOTS",
    "BoundControl",
    "CardSpec",
    "CategoryProfile",
    "Control",
    "Gang",
    "ModelAdapter",
    "build_card",
    "coverage",
    "find_adapter",
    "format_value",
    "gang_label",
    "known_categories",
    "profile_for",
    "short_label",
    "summary_line",
    "unit_symbol",
    "wireless_value",
]
