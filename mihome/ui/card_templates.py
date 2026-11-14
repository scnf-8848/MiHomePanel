"""卡片样式注册表：右键菜单「卡片样式」的唯一事实来源。

**一次选择同时决定宽度和内容**。旧版把「大小」和「模板」当成两个独立属性，
实际上它们从来就不独立：

* 模板的 ``min_span`` 会悄悄把用户设的 1×1 撑成 2×1，选完宽度就是个
  只在切回标准时才复活的"影子状态"；
* 2×1 + 标准没有 body，只是把 76px 的产品图放进一张更宽的空卡里，
  唯一差别是标题能多显示几个字；
* 1×1 + 双滑块根本不可达（会被自动撑宽）。

四种组合里真正有区别的只有三种，于是把宽度直接写进样式：
``standard`` 1×1 / ``light_dual`` 2×1 / ``light_bright`` 2×1。选一次就定死，
没有 ``auto_widened``，也不需要单独的「恢复默认样式」——菜单第一项就是默认，
点它会顺手删掉这条偏好记录。

**合并的是"用户的那一次选择"，不是数据模型里的两个维度。** 尺寸和内容是两件
不同性质的事，代码里必须分得开：

* **尺寸（``span`` + ``rows``）全品类通用** —— 1×1 / 2×1 / 2×2，定义在
  ``SIZES`` 表里，菜单前段「宽卡 2×1」完全由它生成。加尺寸、改叫法都只动那张表。
  2×2 目前只给摄像头实时画面用（``camera_live``），其它品类仍是等高小卡。
* **内容（``label`` + ``slots``）跟品类走** —— 灯是「亮度 + 色温」，空调以后是
  「温度控制」，别的品类就没有。``label`` 只写内容名，**禁止**把尺寸抄进去
  （``__post_init__`` 会直接报错），否则同一段尺寸会有两个来源。

菜单文字是两者在**展示层**的合成：``宽卡 2×1：亮度 + 色温``。用户看到的是一个
完整选项，不用先想"要多宽"再想"放什么"；实现里仍是「通用尺寸 + 品类模板」。

**同一内容可以出现在多个尺寸上**，不需要给名字加后缀：两条记录 ``id`` 不同、
``label`` 相同、``span`` 不同，菜单自然读成「小卡 1×1：温度」/「宽卡 2×1：温度」。
``validate_styles()`` 只拦**同一台设备上会同时出现**的那种撞名（品类相同，或
任一方是通用样式）；品类不同的两条样式永远不会同框，允许同名。

**``standard`` 不是某个品类的模板，是所有设备的兜底外壳**：``category=None``、
``slots=()``，含义是"不挂内容带，产品图 / 多键面板由外壳自绘"。它永远是菜单
里的一项、也是最后一道兜底，任何品类里看到「小卡 1×1：标准」都是同一件事
（回到产品图外观），不会和某品类的"标准版式"混淆。

**「默认」和「标准」不是一回事**（``default_style()``）：品类可以通过
``catalog`` 的 ``default_style`` 声明自己"没有偏好时"用哪个样式——存在传感器
默认就是状态卡，卡片正中直接写「有人」。``standard`` 因此从"默认值"退成
"一个具体选项"，用户想退回产品图随时能选。

可见性判据只有一条链路（``available()`` →``available_styles()``）：

1. ``category`` 对不上就出局（``None`` = 全品类通用）；
2. ``slots`` 里每个槽位都得在这台设备上真的绑到属性（``card.bound()`` 先查
   可写控件再查只读指标）。滑条还要求 ``value_range``——没有刻度就映射不了
   轨道像素；读数不要求。卫生间那种只能调亮度的平板灯，``color_temp`` 绑不上，
   菜单里就只有「标准」和「亮度」，不会出现「亮度 + 色温」。

骨架（别改）：

* 所有卡片共用 ``theme.CARD_H = 168`` 的行高：1×1 / 2×1 是等高网格，
  2×2 占两行（高 = ``2 × CARD_H + CARD_GAP``）。高度一变，等高网格就散了，
  ``CardGridLayout`` 的「放不下就换行」也会失效。
* 外壳（标题带 / 电源键 / 圆角底色）由 ``DeviceCard`` 画；样式**只管**
  ``BODY_RECT = (14, 76, W−28, 78)`` 这块内容带。两行 36 高滑条 + 4 间距填满。
* 带滑条的样式一律 2×1：1×1 的 138 宽里排不下「属性名 + 轨道 + 数值」。
* 灯的两条滑条：亮度是**单色 accent 填充**（灯品类色 ``#F3BE44``），
  色温是**冷暖渐变轨道**（左暖橙 → 右冷白），不是同一支控件的两种配色。

样式按品类逐个加：往 ``STYLES`` 里插一项即可，外壳一行都不用改——能力不够的
设备由 ``available()`` 自己过滤掉，菜单里不会出现选了也无效的项。目前已落地：
灯的「亮度 + 色温」/「亮度」、存在传感器的「状态」、空调的「温度控制」。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from PySide6.QtCore import QPoint, QSize, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ..core import geek
from ..core.catalog import format_value
from . import theme
from . import card_controls
from .card_controls import (
    BODY_W,
    ROW_H,
    CardControl,
    CardReadout,
    CardSlider,
    make_control,
)

if TYPE_CHECKING:
    from ..core.catalog import CardSpec
    from ..core.store import DeviceState

STANDARD = "standard"
LIGHT_DUAL = "light_dual"
LIGHT_BRIGHT = "light_bright"
#: 存在传感器（人在传感器）的状态卡。品类默认指向它——见 ``catalog`` 里
#: ``occupancy-sensor`` 的 ``default_style``，两处是同一个 id 字面量。
PRESENCE_STATE = "presence_state"
#: 空调「温度控制」：大数字步进器 + 模式胶囊。
TEMP_CONTROL = "temp_control"
#: 风扇「扫风 + 风速」：2×1，两列四格。左列左（右）扫风 + 角度，右列模式 + 风速；
#: 右下的风速按当前模式换成 0–100 滑条或档位胶囊（型号规则见 ``dmaker`` 适配）。
FAN_CONTROLS = "fan_controls"
#: 摄像头「实时画面」：2×2，body 里直接铺视频。**不是**摄像头品类的默认样式，
#: 用户要在右键「卡片样式」里主动选；默认仍是 1×1 标准卡（产品图）。
CAMERA_LIVE = "camera_live"
#: 中枢网关「极客版入口」：2×1，左产品图、右一个「打开极客版 + 复制登录码」的大按钮。
#: 哪些设备有这项能力是**探测**出来的（见 ``ABILITY_GEEK``），不是写死的型号名单。
GATEWAY_GEEK = "gateway_geek"
#: 多键面板「隐藏转无线」：body 只画**实际控制通断**的那几路，按键按剩余数量重排
#: （3 键里 1 路转无线 → 两键布局）；一路都不剩就退回产品图。判据是每路的
#: ``Gang.mode`` + 运行时值，见 ``catalog.Gang.is_wireless``。
SWITCH_WIRED = "switch_wired"

#: 需要**实测确认**的能力才出现的样式，用 ``CardStyle.ability`` 声明。
#: ``geek``：能给出极客版地址（``core.geek.probe`` 探过、结论落在
#: ``geek_ability.json``）。目前只有这一种，写成常量是为了让样式声明读起来
#: 是一句话而不是一个裸字符串。
ABILITY_GEEK = "geek"

#: body 区两行控件之间的间距。2×36 + 4 = 76，正好是 ``DeviceCard._body_rect()`` 的高。
BODY_GAP = 4
#: 色温用渐变轨道，其余滑条用单色填充
GRADIENT_SLOTS = frozenset({"color_temp"})

#: body 的呈现方式。决定 ``CardBody`` 给每个槽位建什么控件，也决定一个槽位
#: 「算不算可用」：
#:
#: * ``sliders`` —— 可写、要刻度。滑条得把值映射到轨道像素上，所以属性必须带
#:   ``value_range``；没有刻度可言的属性（只读枚举）在这里天然出局。
#: * ``readout`` —— 只读、要大字号。只要属性绑上了就能显示，不要求 range。
#: * ``controls`` —— **按属性类型各配一种控件**：布尔 → 开关行、枚举 → 胶囊组、
#:   数值 → 步进器（大数字 ±）。空调是第一个用户（温度 + 模式），风扇、浴霸
#:   能直接复用。这里的"可用"要求更细：数值要 ``value_range``、枚举要有
#:   ``value_list``，布尔只要有属性。
BODY_SLIDERS = "sliders"
BODY_READOUT = "readout"
BODY_CONTROLS = "controls"
#: 风扇 2×1：两列 × 两行的格子（左列扫风、右列模式 + 风速），由 ``FanBody`` 铺。
#: 和 ``controls`` 的区别只在布局：那个是一行一个槽位，风扇要把四件事摆成两栏。
BODY_FAN = "fan"
#: 实时画面：body 不建 ``CardBody``，由 ``CameraCard`` 铺一个 ``CameraView``。
#: 不占任何槽位，``slots`` 为空是正常的。
BODY_VIDEO = "video"
#: 极客版入口：body 是「左边产品图 + 右边一个大按钮」，由 ``GeekCard`` 铺一个
#: ``GeekButton``。同样不占槽位——它调的是云端 RPC，不是设备属性。
BODY_GEEK = "geek"
BODY_KINDS = (
    BODY_SLIDERS,
    BODY_READOUT,
    BODY_CONTROLS,
    BODY_FAN,
    BODY_VIDEO,
    BODY_GEEK,
)

#: 分段控件最多切几段、超过改用步进器（枚举档位）。
#: 原来的名字是 ``MAX_CHIPS``（胶囊上限），但单选枚举的默认控件已经换成
#: ``CardSeg``（见 ``card_controls.make_control``），真正的上限也归到那边了——
#: 这里只是转发，别在两处各定一个数。
MAX_SEGMENTS = card_controls.MAX_SEGMENTS


@dataclass(frozen=True, slots=True)
class CardSize:
    """卡片尺寸：**全品类通用**的一维，跟卡片里放什么内容无关。

    ``span`` 占几列、``rows`` 占几行。宽度 = ``span × CARD_MIN_W + (span−1) × GAP``，
    高度 = ``rows × CARD_H + (rows−1) × GAP``。尺寸文案只在这张表里写一次，
    各样式的菜单文字自动跟着变。
    """

    span: int
    label: str
    rows: int = 1


#: 尺寸表的顺序就是菜单里同尺寸项的分组顺序。
SIZES: tuple[CardSize, ...] = (
    CardSize(1, "小卡 1×1"),
    CardSize(2, "宽卡 2×1"),
    CardSize(2, "大卡 2×2", rows=2),
)
_SIZE_BY_CELL = {(s.span, s.rows): s for s in SIZES}
#: ``label`` 里出现这些词，说明把尺寸抄进了内容名，会和 ``span`` 生成的前缀打架。
_SIZE_WORDS = ("小卡", "宽卡", "大卡", "×1", "×2", "x1", "x2", "X1", "X2")


def size_of(span: int, rows: int = 1) -> CardSize | None:
    """``(span, rows)`` → 通用尺寸定义；不在表里（比如误写成 3）返回 ``None``。"""
    return _SIZE_BY_CELL.get((span, rows))


#: 能力判据表：``CardStyle.ability`` 写名字，这里给"这台设备能不能用"的判断。
#: 加一项新能力 = 这里登记一条 + 样式里写名字，样式声明本身不用改。
_ABILITIES: dict[str, Callable[[CardSpec], bool]] = {
    # ``getattr``：能力判据会被 ``available()`` 调用，而那是纯判据函数，历史上
    # 被验证脚本用 ``SimpleNamespace`` 鸭子类型喂过（没有 did 字段）。缺字段就
    # 当作"没探过"，别让一个样式开关把整张菜单炸掉。
    ABILITY_GEEK: lambda card: geek.supports(
        getattr(card, "did", "") or "", getattr(card, "model", "") or ""
    ),
}


def _ability_ready(name: str, card: CardSpec) -> bool:
    """这台设备有没有 ``name`` 这项能力（没探过 = 没有）。"""
    test = _ABILITIES.get(name)
    # 名字写错时当成不可用：宁可不显示这个样式，也别让一个点了就报错的入口
    # 出现在菜单里（注册期还会在 ``__post_init__`` 里直接拦下来）。
    return bool(test and test(card))


@dataclass(frozen=True, slots=True)
class CardStyle:
    """一个卡片样式的**声明**；解析结果就是它本身，不再包一层。

    两个维度在数据上分开，只在菜单文字里合成：

    * ``span`` —— 尺寸，**全品类通用**，取值必须在 ``SIZES`` 里；
    * ``label`` —— 内容名，**品类相关**，只写「亮度 + 色温」这类描述，不带尺寸；
    * ``body`` —— 内容带长什么样（``sliders`` 滑条 / ``readout`` 大字读数）。

    ``slots`` 为空表示没有 body 子控件，内容带仍由外壳自绘（产品图 / 多键面板）。
    槽位可以是可写控件（``brightness``），也可以是只读指标（``occupancy``）——
    ``CardSpec.bound()`` 两边都查。
    """

    id: str
    label: str
    span: int = 1
    rows: int = 1
    slots: tuple[str, ...] = ()
    #: **至少绑上一个**就算可用（``slots`` 仍然要求全绑）。给"功能可多可少"
    #: 的品类用：风扇有没有上下扫风、有没有模式、风速是档位还是连续量，
    #: 每台都不一样，写成 ``slots`` 会让样式在缺一项的设备上整条消失。
    #: 两个字段同时写时是"且"：``slots`` 全有 **且** ``at_least_one`` 有其一个。
    at_least_one: tuple[str, ...] = ()
    category: str | None = None
    body: str = BODY_SLIDERS
    #: 需要设备**实测确认**的某项能力（见 ``ABILITY_GEEK``）。空 = 不设门槛。
    #: 为什么不用型号前缀：极客版这项能力在 MIoT 规格里根本不存在，能给出地址的
    #: 设备也不止一个型号（中枢网关 / 带中枢的路由器 / 中控屏），写死前缀等于让
    #: 人肉维护一份名单，而且漏一个就永远看不到入口。判据改成"程序问过这台设备"
    #: ——``core.geek.probe`` 发一条不耗码的请求，结论按 did 落盘。
    ability: str = ""
    #: 多键面板专用：body 里**过滤掉转无线的路**，剩下的按键按数量重排；
    #: 全被过滤掉就退回产品图。只在"至少一路具备有线/无线模式属性"的面板上出现
    #: ——没有这个属性的插排/老面板选它等于选标准卡，菜单里不必多一条。
    #: 判据是**静态能力**（规格里有没有 ``mode``），不看运行时值：样式菜单不能
    #: 跟着轮询变，否则设备一侧改设置，菜单里就凭空多出/少掉一项。
    hide_wireless: bool = False
    #: 副标题自己拼一句话（``(card, values) -> str``）。空 = 走
    #: ``catalog.summary_line`` 的指标摘要，再没有就"已开启 / 已关闭"。
    #: 风扇是第一个用户：2×1 的第二行要塞「角度 + 风速」两个控件，标签各吃掉
    #: 30px，五段轨道的每段只剩 26px。把这句"这两格是什么"挪到副标题之后，
    #: 第二行就不画标签了（见 ``summary_slots`` 与 ``FanBody._hide_label``）。
    summary: Callable[[Any, Mapping[tuple[int, int], Any]], str] | None = None
    #: 副标题**已经在说**的槽位。body 不画这些槽位的标签，把宽度让给控件。
    #: 只在摘要真的在显示时生效（判据收在 ``summary_text`` 里，别在两处各写一遍）。
    summary_slots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # 声明期就炸，别等菜单里显示成"宽卡 3×1"才发现。注册表是模块级常量，
        # 写错的样式在 import 时就定位得到，比运行时静默兜底好查。
        if size_of(self.span, self.rows) is None:
            raise ValueError(
                f"样式 {self.id!r} 的尺寸 {self.span}×{self.rows} 不在尺寸表里"
                f"（可选 {[(s.span, s.rows) for s in SIZES]}）"
            )
        for word in _SIZE_WORDS:
            if word in self.label:
                raise ValueError(
                    f"样式 {self.id!r} 的 label 里不要写尺寸（命中 {word!r}）："
                    "尺寸由 span 生成，两处都写会重复"
                )
        if self.body not in BODY_KINDS:
            raise ValueError(
                f"样式 {self.id!r} 的 body={self.body!r} 不认识（可选 {list(BODY_KINDS)}）"
            )
        if self.ability and self.ability not in _ABILITIES:
            # 写错名字的表现是"样式永远不出现"，静默得让人查半天，所以注册期就炸
            raise ValueError(
                f"样式 {self.id!r} 的 ability={self.ability!r} 没有登记"
                f"（可选 {list(_ABILITIES)}）"
            )

    @property
    def size(self) -> CardSize:
        """这个样式占的尺寸（通用定义，含菜单里的尺寸文案）。"""
        return _SIZE_BY_CELL[(self.span, self.rows)]

    @property
    def menu_label(self) -> str:
        """``宽卡 2×1：亮度 + 色温`` —— 尺寸（通用）在前，内容（品类）在后。

        尺寸是「这个选项会占用多大格子」的硬信息，先读到它才好判断要不要选；
        内容描述是补充。同一尺寸的几项因此前缀完全一致，纵向扫一眼就能分组。
        前段完全由 ``span`` 生成，样式自己只贡献冒号后面那一截。
        """
        return f"{self.size.label}：{self.label}"

    @property
    def has_body(self) -> bool:
        # 画面样式没有槽位，但内容带确实被一个子控件占满，产品图不能再画。
        return bool(self.slots) or bool(self.at_least_one) or self.body in (
            BODY_VIDEO,
            BODY_GEEK,
            BODY_FAN,
        )

    def summary_text(self, state: DeviceState) -> str:
        """副标题这一帧要写的话；空串 = 没有，调用方退回"已开启 / 已关闭"。

        **关着的时候不报**：风扇关了还写「扫风角度 60° · 风速 3 档」，像是它正在
        吹。所以"关机不报"这条判断放在这里，而不是散在调用方——问这句话的有两处：
        ``DeviceCard._subtitle``（决定写什么）和 ``FanBody._hide_label``（决定要不
        要让出标签宽度），两处各写一遍必然漂。
        """
        if self.summary is None:
            return ""
        if state.power is False:
            return ""
        return self.summary(state.card, state.values) or ""

    def available(self, card: CardSpec) -> bool:
        """这台设备能不能用这个样式。

        三条判据：品类对得上；需要的能力已确认（``ability`` 非空时）；``slots``
        里每个槽位都在这台设备上**绑到了属性**。

        槽位查的是 ``card.bound(slot)``（先控件后读数），所以样式既能引用可写
        的 ``brightness``，也能引用只读的 ``occupancy``——登记在哪个 list 是
        ``catalog`` 的事，样式不必关心。

        "绑到了"之外还要看 body 类型：滑条没有刻度就映射不了像素，所以额外
        要求 ``value_range``；读数只要属性存在即可。卫生间那种只能调亮度的
        平板灯，``color_temp`` 绑不上，菜单里就不会出现「亮度 + 色温」。
        """
        if self.category is not None and card.category != self.category:
            return False
        if self.ability and not _ability_ready(self.ability, card):
            return False
        if self.hide_wireless and not any(
            g.mode is not None and g.wireless_value is not None for g in card.gangs
        ):
            # 没有一路带"有线/无线"模式属性（插排、老面板）→ 没有可隐藏的东西
            return False
        if self.at_least_one and not any(
            self._slot_ready(card.bound(slot)) for slot in self.at_least_one
        ):
            # 一个都没绑上：这台设备没有这个样式要展示的东西，菜单里别出现
            return False
        return all(self._slot_ready(card.bound(slot)) for slot in self.slots)

    def _slot_ready(self, ctl: Any) -> bool:
        if ctl is None or ctl.prop is None:
            return False
        if self.body == BODY_READOUT:
            return True
        if self.body in (BODY_CONTROLS, BODY_FAN):
            # 建什么控件由属性类型决定，能不能建也跟着类型走：枚举没有可选项
            # 就是一排空胶囊，数值没有刻度就是一对不知该加减多少的按钮。
            kind = ctl.prop.kind
            if kind == "bool":
                return True
            if kind == "enum":
                return bool(ctl.prop.value_list)
            return bool(ctl.prop.value_range)
        return bool(ctl.prop.value_range)


def fan_summary(card: CardSpec, values: Mapping[tuple[int, int], Any]) -> str:
    """风扇副标题：「扫风角度 60° · 风速 3 档」。

    为什么搬到副标题：2×1 的内容带只有两行，第二行是「角度 + 风速」。两个标签各
    吃掉 30px 之后，五段角度的轨道只剩 134 宽（每段 26px，字号掉到 10）。这两格
    叫什么一旦由副标题讲清楚（``summary_slots``），第二行就能不画标签，轨道拿满
    整列 168（每段 33px，字回到 12）。

    **风速按当前模式挑槽位**，和 ``FanBody`` 的双选格用同一个判据
    （``card.speed_slot``）：不这么做，自然风下会同时报出「档位 3」和「风速 40」
    两个数，用户没法判断哪个在生效。
    """
    parts: list[str] = []
    angle = _slot_text(card, values, "swing_angle", "扫风角度")
    if angle:
        parts.append(angle)
    mode = card.bound("mode")
    mode_value = values.get(mode.prop.key) if mode is not None and mode.prop else None
    slot = card.speed_slot(mode_value)
    if slot is None:
        # 没有型号规则（或模式值读不到）：和 FanBody 一样"档位优先"，不猜
        slot = "fan_level" if card.bound("fan_level") is not None else "fan_speed"
    speed = _slot_text(card, values, slot, "风速")
    if speed:
        parts.append(speed)
    return " · ".join(parts)


def _slot_text(card: CardSpec, values: Mapping[tuple[int, int], Any],
               slot: str, name: str) -> str:
    """``名字 值``；槽位没绑上或这一轮没读到值就返回空串（别写「角度 —」）。"""
    ctl = card.bound(slot)
    prop = ctl.prop if ctl is not None else None
    if prop is None:
        return ""
    value = values.get(prop.key)
    if value is None:
        return ""
    return f"{name} {format_value(prop, value)}"


#: 同尺寸内按这里的顺序排列（``available_styles()`` 会先按尺寸升序分组）。
#: ``standard`` 永远排第一且永远可用，也是所有设备的默认。
STYLES: tuple[CardStyle, ...] = (
    CardStyle(id=STANDARD, label="标准"),
    # 多键面板的第二选择：只留"按下去真的会切继电器"的那几路。吸顶灯开关那种
    # 整块都转无线的单开，选了它就等于一张产品图卡。
    CardStyle(id=SWITCH_WIRED, label="隐藏转无线", category="switch", hide_wireless=True),
    CardStyle(
        id=LIGHT_DUAL,
        label="亮度 + 色温",
        span=2,
        slots=("brightness", "color_temp"),
        category="light",
    ),
    CardStyle(
        id=LIGHT_BRIGHT,
        label="亮度",
        span=2,
        slots=("brightness",),
        category="light",
    ),
    # 人在传感器唯一要回答的问题是"有没有人"，产品图没有信息量。
    # 只读 body：不建滑条，直接建一个大字读数（见 ``CardReadout``）。
    # ``occupancy`` 在这类设备上登记为 metrics（只读指标），不是 controls——
    # ``CardSpec.bound()`` 两处都查，所以样式声明不用关心这个区别。
    CardStyle(
        id=PRESENCE_STATE,
        label="状态",
        slots=("occupancy",),
        category="occupancy-sensor",
        body=BODY_READOUT,
    ),
    # 空调日常最高频的两件事：调温度、换模式。``target_temp`` 是 stepper、
    # ``mode`` 是 select，两者在详情页各占一行；这里按属性类型重建一遍，
    # 大数字比滑条更符合"温度是一个精确的小整数"这个事实（见 ``BODY_CONTROLS``）。
    CardStyle(
        id=TEMP_CONTROL,
        label="温度控制",
        span=2,
        slots=("target_temp", "mode"),
        category="air-conditioner",
        body=BODY_CONTROLS,
    ),
    # 风扇：一天里最常碰的是"往哪儿吹"和"吹多大"，模式反而是一次性设置，
    # 但模式和风速的控件是同一个位置（直吹风给 0–100 滑条、自然风给 4 档），
    # 所以两行两列：左列扫风、右列模式 + 风速。
    #
    # **槽位一个都不"必须"**（``at_least_one``）：风扇之间的差异太大——有的只有
    # 3 档没有连续量、有的有上下扫风、有的没有模式。写死 ``slots`` 会让这张卡在
    # 缺一项的设备上整条消失，所以只要求"至少有一项能用"，具体摆什么由规格答。
    CardStyle(
        id=FAN_CONTROLS,
        label="扫风 + 风速",
        span=2,
        at_least_one=(
            "swing_h",
            "swing_v",
            "swing_angle",
            "mode",
            "fan_speed",
            "fan_level",
        ),
        category="fan",
        body=BODY_FAN,
        # 第二行（角度 / 风速）的文字搬到副标题：这两格是 312 宽里最缺宽度的两处，
        # 标签一让就是 30px（五段角度从每段 26 → 33）。关灯时副标题退回「已关闭」，
        # 标签自己会回来，所以关机状态下不会出现"两个控件都没名字"。
        summary=fan_summary,
        summary_slots=("swing_angle", "fan_level", "fan_speed"),
    ),
    # 摄像头默认仍是 1×1 标准卡（产品图）；2×2 是用户主动选的查看方式。
    # 没有槽位，body 由 ``CameraCard`` 铺一个 ``CameraView``，播放控件与详情页共用。
    CardStyle(
        id=CAMERA_LIVE,
        label="实时画面",
        span=2,
        rows=2,
        category="camera",
        body=BODY_VIDEO,
    ),
    # 中枢网关平时没有任何可点的地方（没有开关、没有读数），卡片就是一张产品图。
    # 真正高频的动作只有一个：进极客版写规则。而登录码**只能现取**（一次性，
    # 取新码会让旧码失效），所以做成卡片上一个大按钮：点一下 = 取地址 + 取码 +
    # 复制 + 打开浏览器。
    #
    # **不写 category**：能不能用只由能力决定。写 ``category="gateway"`` 会把
    # 带中枢的路由器（``router``）、中控屏（``screen``）挡在门外，而它们恰恰是
    # 用户点名的"另一种中枢设备"——判据只能有一个，能力已经是那个判据了。
    CardStyle(
        id=GATEWAY_GEEK,
        label="极客版入口",
        span=2,
        ability=ABILITY_GEEK,
        body=BODY_GEEK,
    ),
)


def validate_styles(styles: Sequence[CardStyle]) -> None:
    """注册表自检：``id`` 唯一；同一台设备上可能同时出现的两项不能同名同尺寸。

    **同一个 label 允许出现在不同尺寸上**——这正是「温度」既有 1×1 精简版又有
    2×1 滑块版时的写法：两条记录的 ``id`` 不同（偏好各存各的），菜单文字靠尺寸
    前缀区分成「小卡 1×1：温度」/「宽卡 2×1：温度」，不需要给 label 加后缀。

    **同一个 label 也允许出现在品类不同的样式上**。灯的「亮度」和浴霸的「亮度」
    永远不会出现在同一台设备上，菜单里不可能撞行，没有理由拦。判重因此按
    "单台设备可见的集合"来做：两个样式的 ``category`` 相同，或任一方是通用的
    ``None``，它们才可能同时可见，这时 ``(label, span)`` 撞车就必须拒——菜单里
    两行一模一样的选项，用户只能靠点一下看效果来分辨。
    """
    seen_id: dict[str, CardStyle] = {}
    for s in styles:
        if s.id in seen_id:
            raise ValueError(
                f"样式 id 重复：{s.id!r}（两条记录的 label 都是 {s.label!r}）"
            )
        seen_id[s.id] = s
    for i, a in enumerate(styles):
        for b in styles[i + 1:]:
            if (a.label, a.span, a.rows) != (b.label, b.span, b.rows):
                continue
            if a.category is None or b.category is None or a.category == b.category:
                raise ValueError(
                    f"样式 {a.id!r} 与 {b.id!r} 的 label 和尺寸都一样"
                    f"（{a.label!r} / {a.span}×{a.rows}），菜单里会出现两行无法分辨的选项"
                    "；内容确有差别就把差别写进 label"
                )


validate_styles(STYLES)
_BY_ID = {s.id: s for s in STYLES}


def validate_profile_defaults() -> None:
    """``catalog`` 里品类声明的 ``default_style`` 必须真的注册过。

    这是个跨层的字符串契约（core 不 import ui，只能写字面量）。拼错**不会**
    在运行时炸，只会静默退回 ``standard``——用户点了"默认"却得到产品图，
    很难查。所以 import 时就校验一遍，和 ``validate_styles`` 同样是声明期报错。
    """
    from ..core.catalog import PROFILES

    for prof in PROFILES.values():
        if prof.default_style and prof.default_style not in _BY_ID:
            raise ValueError(
                f"品类 {prof.category!r} 的 default_style={prof.default_style!r} "
                f"没有在 STYLES 里注册（已注册 {sorted(_BY_ID)}）"
            )


validate_profile_defaults()


def style_by_id(style_id: str) -> CardStyle | None:
    return _BY_ID.get(style_id)


def available_styles(card: CardSpec) -> tuple[CardStyle, ...]:
    """右键菜单里该设备能看到哪些样式。**默认样式永远排第一**。

    排序键 ``(不是默认, span, rows)``：先按"是否为默认"分组，再按尺寸升序。默认项
    必须第一，不只是好看——菜单第一项就是用户点「回默认」的落点，它要是排在
    中间，用户点完还得回想自己选的是哪一项。

    尺寸相同时保持 ``STYLES`` 的注册顺序（``sorted`` 稳定）。这样注册时不必
    操心把「温度 1×1」「温度 2×1」「亮度 2×1」写得多交错，菜单里也不会出现
    「小卡、宽卡、小卡」这种要来回扫的顺序。
    """
    fallback = default_style(card)
    return tuple(sorted(
        (s for s in STYLES if s.available(card)),
        key=lambda s: (s.id != fallback.id, s.span, s.rows),
    ))


def default_style(card: CardSpec) -> CardStyle:
    """这台设备**没有偏好时**该用的样式。

    优先品类自己声明的 ``default_style``（存在传感器 → 状态卡），它没声明、
    或者声明了但这台具体设备能力不够时，退回全品类通用的 ``standard``。
    兜底必须有：同一个品类下不同型号能绑到的属性不一样，声明是"这一类设备的
    意图"，不是"这一台设备的保证"。
    """
    style = style_by_id(card.default_style) if card.default_style else None
    if style is None or not style.available(card):
        return _BY_ID[STANDARD]
    return style


def resolve_card_style(prefs: Any, did: str, card: CardSpec) -> CardStyle:
    """把「用户偏好 + 设备能力」合成最终外观。

    三道兜底，任意一道落下都用 ``default_style(card)`` 而不是硬编码标准：
    没设过偏好、不认识的样式 id、能力不够的样式。设备换型号、规格缺属性时
    不会留下一张空卡。

    ``prefs.card_style`` 对"没设过"返回 ``None``，所以"没选过"能走到品类默认，
    而"显式选了标准"会真正拿到 ``standard``——这两件事在偏好层是分开的。
    """
    stored = prefs.card_style(did)
    picked = style_by_id(stored) if stored else None
    if picked is not None and picked.available(card):
        return picked
    return default_style(card)


# ---------------------------------------------------------------------------
# body：样式里那 78 高的真实控件
# ---------------------------------------------------------------------------


class CardBody(QWidget):
    """样式的 body 区，持有若干控件。

    用真实 ``QWidget`` 而不是 ``QPainter`` 手绘：拖动捕获、移出控件后的追踪、
    「点空白开详情 / 点滑条调亮度」的事件分流都是白送的（见 ``card_controls.py``）。

    **建什么控件由 ``style.body`` 决定**，不由调用方判断：

    * ``sliders`` → ``CardSlider``（可写，接线到 ``value_committed``）；
    * ``readout`` → ``CardReadout``（只读大字，不接线）。

    两者接口一致（``set_value`` / ``setEnabled`` / ``apply_theme``），所以
    ``sync`` 和 ``apply_theme`` 不需要分支。以后加摄像头画面控件也是同一个位置：
    这里多一个分支、多一个 ``body`` 取值，外壳一行不用改。
    """

    #: siid, piid, value —— 松手才发；拖动中只有 ``previewed``
    value_committed = Signal(int, int, object)
    previewed = Signal()
    menu_requested = Signal(QPoint)  # 全局坐标，交给卡片统一弹右键菜单

    def __init__(
        self,
        style: CardStyle,
        state: DeviceState,
        palette: theme.Palette,
        accent: str,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self._style = style
        self._accent = accent
        self._palette = palette
        self.controls: dict[str, CardControl] = {}
        self._ctl: dict[str, Any] = {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(BODY_GAP)
        # 首尾各一段弹性空白：两行（36+4+36=76）在 78 高的内容带里上下各留 1px，
        # 只有一行（36）或一个大字读数（78）时整体垂直居中，不会贴着顶边。
        lay.addStretch(1)

        for slot in style.slots:
            ctl = state.card.bound(slot)
            if ctl is None or ctl.prop is None:
                continue
            widget = self._make(ctl, slot)
            if widget is None:
                continue
            lay.addWidget(widget)
            self.controls[slot] = widget
            self._ctl[slot] = ctl

        lay.addStretch(1)
        self.sync(state)

    def _make(self, ctl: Any, slot: str) -> CardControl | None:
        """按 ``style.body`` 建一个控件，并把它的信号接到 body 上。

        * ``readout`` 只读，不接线；
        * ``sliders`` 接 ``previewed``（拖动中让卡片即时重绘）+ ``committed``；
        * ``controls`` 按属性类型选控件（见 ``_pick``），都接 ``committed``。

        ``committed`` 统一在这里接：四类控件的信号签名都是"一个值"，
        差别只在控件自己怎么产生它，body 不该为每种控件各写一遍。
        """
        prop = ctl.prop
        if self._style.body == BODY_READOUT:
            return CardReadout(
                prop, self._palette, self._accent, label=ctl.label, parent=self
            )

        if self._style.body == BODY_CONTROLS:
            widget = self._pick(ctl)
        else:
            widget = CardSlider(
                prop,
                self._palette,
                self._accent,
                label=ctl.label,
                gradient=slot in GRADIENT_SLOTS,
                parent=self,
            )
        if widget is None:
            return None
        if isinstance(widget, CardSlider):
            widget.previewed.connect(self.previewed)
        widget.committed.connect(
            lambda value, p=prop: self.value_committed.emit(p.siid, p.piid, value)
        )
        return widget

    def _pick(self, ctl: Any) -> CardControl | None:
        """``controls`` body 的选型：**转给控件库**，这里只提供"用多宽"。

        选型的全部知识（声明优先、枚举默认分段、放不下退步进器、range 默认步进器）
        都在 ``card_controls.make_control``。这里原先自己写了一遍，``FanBody`` 又写
        一遍，两份已经开始漂（一处改了 MAX_CHIPS，另一处没动）——见
        ``docs/dev/ui.md``「卡片控件库」。
        """
        return make_control(
            ctl, self._palette, self._accent,
            # 不能用 ``self.width()``：CardBody 还挂在父控件上、几何没定下来时
            # 宽度是 QWidget 的默认 100，会把 5 段模式判成"放不下"而退步进器。
            # 内容带的宽度是尺寸档定死的（2×1 / 2×2 都是 312），直接用那个。
            avail=BODY_W,
            label=ctl.label, centered=True, parent=self,
        )

    # -- 数据 ------------------------------------------------------------
    def sync(self, state: DeviceState) -> None:
        """轮询回来 / 切样式后同步数值与可用态。

        拖动中的滑条自己会忽略 ``set_value``（``CardSlider._dragging``），
        所以设备上报晚到不会把用户手里的滑块拽回去。

        只读读数不参与"关着就置灰"那套：传感器本来就没有开关态，``any_on``
        对它恒为假，照搬会把唯一的信息压成灰字。
        """
        readout = self._style.body == BODY_READOUT
        for slot, widget in self.controls.items():
            ctl = self._ctl.get(slot)
            prop = ctl.prop if ctl is not None else None
            value = state.get_prop(prop) if prop is not None else None
            active = state.mode_active_value() if slot == "mode" else None
            if active is not None:
                # 型号适配：有 mode_active（如挂灯的 scene-mode）时，胶囊的激活项
                # 看它而不是 mode(2,5)——mode 只用于写入，手调亮度/色温后它不会变，
                # 拿它当激活判据会让胶囊一直假亮着。值为 0 表示无激活项，0 不在
                # items 里，所有胶囊自然是未选中态。
                # 没适配 / 还没读到时返回 None，走下面的默认路径（mode），不空高亮。
                widget.set_active(active)
            elif value is not None:
                widget.set_value(value)
            if readout:
                widget.setEnabled(bool(state.online))
                continue
            writable = bool(prop is not None and prop.writeable and ctl and ctl.write)
            widget.setEnabled(bool(state.online and writable))
            # 设备关着时控件仍在（米家也保留亮度条 / 温控），只是压一档透明度
            widget.set_dimmed(not state.any_on)

    def apply_theme(self, palette: theme.Palette, accent: str) -> None:
        self._palette = palette
        self._accent = accent
        for widget in self.controls.values():
            widget.apply_theme(palette, accent)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        """在滑条上右键也要能改卡片样式。

        ``CardSlider`` 默认忽略 ``ContextMenu``，Qt 会把它传给父 body；
        这里接住并转给卡片，和卡片空白区走同一套菜单。
        """
        self.menu_requested.emit(event.globalPos())
        event.accept()

    @property
    def row_h(self) -> int:
        return ROW_H

    def sizeHint(self) -> QSize:  # noqa: N802
        if self._style.body == BODY_READOUT:
            return QSize(120, CardReadout.HEIGHT)
        # 高度按控件真实高度累加：``controls`` body 里的胶囊是 26 高、开关行是 36，
        # 一律按 ROW_H 算会把两行（66）高估成 76，居中时偏下。
        h = sum(w.height() for w in self.controls.values())
        return QSize(120, h + BODY_GAP * max(0, len(self.controls) - 1))


__all__ = [
    "ABILITY_GEEK",
    "BODY_CONTROLS",
    "BODY_FAN",
    "BODY_GAP",
    "BODY_GEEK",
    "BODY_KINDS",
    "BODY_READOUT",
    "BODY_SLIDERS",
    "BODY_VIDEO",
    "CAMERA_LIVE",
    "FAN_CONTROLS",
    "GATEWAY_GEEK",
    "GRADIENT_SLOTS",
    "LIGHT_BRIGHT",
    "LIGHT_DUAL",
    "MAX_SEGMENTS",
    "PRESENCE_STATE",
    "TEMP_CONTROL",
    "SIZES",
    "STANDARD",
    "STYLES",
    "CardBody",
    "CardSize",
    "CardStyle",
    "available_styles",
    "default_style",
    "resolve_card_style",
    "size_of",
    "style_by_id",
    "validate_profile_defaults",
    "validate_styles",
]
