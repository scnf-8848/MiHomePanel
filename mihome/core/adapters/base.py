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
    #: 文案覆盖（只影响显示）；空 = 退回类别级与全局词典
    text: TextLayer = TextLayer()
    #: 一句"为什么需要这条适配"（实测结论，写给下一个人看）；不参与任何逻辑
    note: str = ""


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
    * 每条 ``text`` 都要过 :func:`check_text_layer`（空层直接跳过，不校验）。
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
            if not adapter.text.empty:
                check_text_layer(model, adapter.text)
            table[model] = adapter
            origin[model] = name
    return table


__all__ = [
    "SHORT_LIMIT",
    "ModelAdapter",
    "TextLayer",
    "check_model",
    "check_text_layer",
    "collect",
]
