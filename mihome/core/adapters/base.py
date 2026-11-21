"""适配层的公共类型与登记表合并逻辑。

这里**只有数据结构、校验和合并**，不放任何具体型号——具体规则在 ``adapters``
包里按厂商分文件（``yeelink.py``、``dmaker.py`` …），类别级的文案在
``categories.py``。

适配分两类，风险等级不同，允许的粒度也不同：

* **行为适配**（``extra`` / ``mode_active``）——写错会让设备拒收命令或状态错乱，
  所以**一律按型号**，绝不按品类推广；
* **文案适配**（``TextLayer``）——纯显示，认不出来就退回云端原文，没有任何 IO，
  所以允许按**类别**给。这是本项目"设备特判一律按型号"的**唯一例外**，
  理由见 ``categories.py`` 顶部。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

#: 界面短名的字数上限，与 ``core/enum_text.py`` 的默认 ``limit`` 一致
SHORT_LIMIT = 12

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


@dataclass(frozen=True, slots=True)
class TextLayer:
    """一层文案覆盖：把云端原文换成人话。**只影响显示，不参与任何设备 IO。**

    字段名刻意区分"枚举值"和"属性名"，因为两者的键不是一回事：

    * ``enums``——``(属性名, 云端原话, 短名)``，属性名用 **URN 名**（``wash-mode``）；
    * ``words``——``(云端原话, 短名)``，只放这一层独有的词；
    * ``props``——``(云端属性名, 中文行名)``。

    ``enums`` 只对那个属性生效，比 ``words`` 精确；但**精确度只在层内起作用**——
    跨层永远是层优先（型号 > 类别 > 全局）。所以型号层写 ``words`` 也能盖住全局
    的属性级条目：为一台设备专门写的规则，不该被一条更通用的词条悄悄压掉。
    词只在一个属性上有意义时才写 ``enums``，其余写 ``words`` 就够。

    属性名必须是 URN 名、不能写云端 ``comment``（"洗涤流程"）：comment 是给开发者
    看的描述，米家改一个字，键就失效了，而且失效时不会有任何报错。导入期会拦下
    带中文的键（见 :func:`check_text_layer`）。
    """

    enums: tuple[tuple[str, str, str], ...] = ()
    words: tuple[tuple[str, str], ...] = ()
    props: tuple[tuple[str, str], ...] = ()
    #: 一句"这批词为什么放在这一层"（写给下一个人看）；不参与任何逻辑
    note: str = ""

    @property
    def empty(self) -> bool:
        return not (self.enums or self.words or self.props)


@dataclass(frozen=True, slots=True)
class ModelAdapter:
    """**型号级**适配记录：这台机器多读哪个属性、模式看哪个属性、文案怎么覆盖。

    一定要按设备节点上报的 model 精确命中——不要按品类（light）泛化，
    也不要靠属性名去猜设备行为。没登记的型号完全走默认路径。
    """

    #: 额外参与轮询的属性名（只读、不渲染）；空 = 不追加任何请求
    extra: tuple[str, ...] = ()
    #: 模式激活判据用的属性名；空 = 仍用 ``mode``(2,5) 自己的值（默认行为）
    mode_active: str = ""
    #: **同一功能两套属性**时，哪个模式值用哪个槽位：``("mode", {0: "fan_speed"})``。
    #: 写成 ``(模式属性名, {模式值: 槽位})``——槽位名来自 ``catalog.SLOTS`` /
    #: 品类画像，这里只回答"此刻谁说了算"。
    #: 为什么必须按型号给：两个属性在规格里**同时存在、都可写**，谁生效是固件语义。
    #: 米家风扇实测就是 ``mode=0``（直吹风）认 ``speed-level``(1–100)、
    #: ``mode=1``（自然风）认 ``fan-level``(1–4 档)，猜不出来。
    speed_by_mode: tuple[str, dict[int, str]] | None = None
    #: 电量槽位拆成多行：``((siid.piid, 行名), ...)``，**元组顺序就是显示顺序**。
    #:
    #: 为什么必须按型号：一台设备装两块电池时，规格里两块电量**属性名、服务名完全
    #: 一样**（都叫 ``battery-level`` / ``battery``），``find`` 只能按 siid 升序取一条。
    #: 想"两块都显示"就必须知道**哪条是哪块、该叫什么**，而这是设备的物理形态
    #: （4 Pro 是锂电 + 干电池备用，V6 Max 是主电池 + 猫眼电池），猜不出来。
    #:
    #: 顺序有意义：``summary_line`` 只取前两个有值的指标且不带标签，第一条就进卡片
    #: 副标题——"首要看锂电池"是靠排第一实现的，不是靠隐藏另一块。
    #:
    #: 定位写 ``siid.piid``（如 ``"13.1"``）而不是属性名，因为两块属性同名；也不用
    #: 服务注释（"锂电池"）——那是云端文案，改一个字就静默失效。固件换了规格导致
    #: 某个定位不存在时，**那一行直接跳过**，不报错、不错位显示。
    #:
    #: 空 = 不拆，沿用"一条 ``battery-level``"的老行为（绝大多数设备如此）。
    batteries: tuple[tuple[str, str], ...] = ()
    #: 文案覆盖（只影响显示）；空 = 退回类别级与全局词典
    text: TextLayer = TextLayer()
    #: 这台型号带**米家自动化极客版**（局域网后台 ``http://<设备 IP>/``）。
    #:
    #: 为什么必须是型号级白名单，而不是"每台设备探一下"：这项能力是**型号属性**，
    #: 同一型号不会一台有一台没有；MIoT 规格里又没有它（中枢网关的 spec 只有网络 /
    #: 指示灯 / 虚拟事件），所以只能由人登记。原来的做法是给**每台设备**发一条
    #: ``miIO.get_autowebconfig_url`` 去问，实测代价是 43 条请求只换来 1 台设备显示
    #: 入口（38 个型号判否，且 7 天后还要重探）——用一条条网络请求去赌一个本该写死
    #: 的事实。现在改成：登记过的型号**零请求**直接显示入口；没登记的只有
    #: gateway / router / screen / central-control 这四类才会发一条探测兜底
    #: （见 ``core/geek.py:candidates``），漏登记了新中枢设备也不至于永远看不到入口。
    geek: bool = False
    #: 一句"为什么需要这条适配"（实测结论，写给下一个人看）；不参与任何逻辑
    note: str = ""
    #: 人类可读的设备名（如「小米智能门锁 4 Pro」）。清单与诊断靠它认人——
    #: 型号串 `xiaomi.lock.s1` 记不住是哪台，翻代码找适配的人也先想起设备名。
    device: str = ""
    #: 这条规则的实测/调研记录（仓库相对路径，如
    #: ``docs/reference/lock-dual-battery.md``）。让别人能顺着看到"为什么这么写"。
    doc: str = ""

    @property
    def fields(self) -> tuple[str, ...]:
        """这条记录实际用到了哪些能力（按固定顺序，供清单与诊断展示）。"""
        out = []
        if self.extra:
            out.append("extra")
        if self.mode_active:
            out.append("mode_active")
        if self.speed_by_mode:
            out.append("speed_by_mode")
        if self.batteries:
            out.append("batteries")
        if not self.text.empty:
            out.append("text")
        if self.geek:
            out.append("geek")
        return tuple(out)

    @property
    def empty(self) -> bool:
        """一条规则都没配。这种记录没有意义，导入期会拦下。"""
        return not self.fields


def check_text_layer(owner: str, layer: TextLayer) -> TextLayer:
    """校验一层文案覆盖，返回原值。导入期跑，写错当场报错。

    拦的是三种手写时最容易犯、线上又最难发现的错：

    * 属性名写成云端中文 ``comment``（"洗涤流程"）——云端改文案就静默失效；
    * 键或值留空（空键会把所有空文案一起吃掉）；
    * 短名比 ``SHORT_LIMIT`` 还长——那一层存在的意义就是收短，写了等于没写。
    """
    for prop, text, short in layer.enums:
        for label, value in (("属性名", prop), ("原话", text), ("短名", short)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{owner}：文案覆盖的{label}不能为空（{prop!r} / {text!r}）")
        if _CJK_RE.search(prop):
            raise ValueError(
                f"{owner}：文案覆盖的属性名 {prop!r} 含中文——属性名要用 URN 名"
                "（如 wash-mode），不要用云端 comment（如 洗涤流程），后者会被云端改动"
            )
        if len(short) > SHORT_LIMIT:
            raise ValueError(f"{owner}：{text!r} 的短名 {short!r} 超过 {SHORT_LIMIT} 字")
    for text, short in layer.words:
        if not text.strip() or not short.strip():
            raise ValueError(f"{owner}：文案覆盖的键/值不能为空（{text!r}）")
        if len(short) > SHORT_LIMIT:
            raise ValueError(f"{owner}：{text!r} 的短名 {short!r} 超过 {SHORT_LIMIT} 字")
    for prop, name in layer.props:
        if not prop.strip() or not name.strip():
            raise ValueError(f"{owner}：属性名映射的键/值不能为空（{prop!r}）")
        if _CJK_RE.search(prop):
            raise ValueError(
                f"{owner}：属性名映射的键 {prop!r} 含中文——这里映射的是云端英文属性名"
                "（如 Battery Level），中文名本来就该原样显示"
            )
    return layer


_ADDR_RE = re.compile(r"^[0-9]+\.[0-9]+$")


def check_batteries(owner: str, rows: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    """校验一条电量拆行规则，返回原值。导入期跑，写错当场报错。

    拦的是三类"线上最难发现"的错：定位不是 ``siid.piid``（写成属性名或服务注释，
    运行时永远匹配不到，静默少一行）、行名为空、同一个定位写了两遍（两行显示同一个数）。
    """
    seen: set[str] = set()
    for where, label in rows:
        if not isinstance(where, str) or not _ADDR_RE.match(where):
            raise ValueError(
                f"{owner}：电量行定位 {where!r} 不是「siid.piid」形式"
                "（如 13.1）——不要写属性名（两块电池同名）或服务注释（会被云端改）"
            )
        if where in seen:
            raise ValueError(f"{owner}：电量行定位 {where!r} 写了两次")
        seen.add(where)
        if not isinstance(label, str) or not label.strip() or label != label.strip():
            raise ValueError(f"{owner}：电量行 {where!r} 的行名不能为空或带前后空格")
        if len(label) > SHORT_LIMIT:
            raise ValueError(f"{owner}：电量行名 {label!r} 超过 {SHORT_LIMIT} 字")
    return rows


def check_adapter(owner: str, adapter: ModelAdapter) -> ModelAdapter:
    """校验一条型号适配记录，返回原值。导入期跑，写错当场报错。

    为什么逼着写 ``device`` 与 ``note``：这张表是**唯一**能回答"这台设备适配过没有、
    为什么这么适配"的地方。少一句 note，下一个人（或几个月后的自己）就得把整份规格
    和代码重新推一遍，甚至重复适配。两条都只是文字，不参与逻辑，成本极低。
    """
    if adapter.empty:
        raise ValueError(
            f"{owner}：登记了适配却一条规则都没配（extra / mode_active / "
            "speed_by_mode / batteries / text / geek 全空）——要么删掉这条，要么补上"
        )
    if not adapter.device.strip() or adapter.device != adapter.device.strip():
        raise ValueError(f"{owner}：没写设备名 device（如「小米智能门锁 4 Pro」）")
    if not adapter.note.strip():
        raise ValueError(f"{owner}：没写 note——为什么需要这条适配，是给下一个人看的")
    if adapter.doc and not (adapter.doc.startswith("docs/") and adapter.doc.endswith(".md")):
        raise ValueError(
            f"{owner}：doc 要写仓库内的 Markdown 相对路径"
            f"（如 docs/reference/xxx.md），实际是 {adapter.doc!r}"
        )
    if not adapter.text.empty:
        check_text_layer(owner, adapter.text)
    if adapter.batteries:
        check_batteries(owner, adapter.batteries)
    if not isinstance(adapter.geek, bool):
        # 写成 "yes" / 1 在 Python 里也"像真的"，但台账里混进字符串之后
        # 谁也不知道它到底是白名单还是打错了字，导入期拦掉最省事
        raise ValueError(f"{owner}：geek 只能是 True / False，实际是 {adapter.geek!r}")
    return adapter


def check_model(model: str) -> str:
    """校验一条型号键，返回原值。

    型号必须是设备节点**原样上报**的点分字符串。这里只拦手写时最容易犯的错：
    前后空格、段数不对、把品类名（``light``）当成型号写进来。校验在导入期跑，
    写错了当场报错，不会拖到某台设备「莫名没有适配」才发现。
    """
    if not model or model != model.strip():
        raise ValueError(f"型号键不能为空或带前后空格：{model!r}")
    parts = model.split(".")
    if len(parts) < 3 or not all(parts):
        raise ValueError(
            f"型号键 {model!r} 不是合法形式，期望「厂商.品类.型号」"
            "（如 yeelink.light.lamp22），且必须与设备上报的 model 完全一致"
        )
    return model


def collect(
    named: Iterable[tuple[str, Mapping[str, ModelAdapter]]],
) -> dict[str, ModelAdapter]:
    """把各厂商文件里的 ``ADAPTERS`` 合成一张表，并守住两条约束。

    * 型号必须能通过 :func:`check_model`；
    * 同一型号只能登记一次——重复会让"哪条生效"取决于 import 顺序，
      宁可导入期就报错，也不要线上随机表现；
    * 每条都要过 :func:`check_adapter`（设备名、note、各字段的格式）。
    """
    table: dict[str, ModelAdapter] = {}
    origin: dict[str, str] = {}
    for name, mapping in named:
        for model, adapter in mapping.items():
            check_model(model)
            if model in table:
                raise ValueError(
                    f"型号 {model!r} 登记了两次：{origin[model]}.py 和 {name}.py。"
                    "一条型号只该有一条规则，请合并。"
                )
            check_adapter(model, adapter)
            table[model] = adapter
            origin[model] = name
    return table


__all__ = [
    "SHORT_LIMIT",
    "ModelAdapter",
    "TextLayer",
    "check_adapter",
    "check_batteries",
    "check_model",
    "check_text_layer",
    "collect",
]
