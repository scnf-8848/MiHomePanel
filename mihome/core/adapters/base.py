"""型号适配的公共类型与登记表合并逻辑。

这里**只有数据结构、校验和合并**，不放任何具体型号——具体规则在 ``adapters``
包里按厂商分文件（``yeelink.py``、``dmaker.py`` …）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelAdapter:
    """**型号级**适配记录：只描述"这台机器多读哪个属性、模式看哪个属性"。

    一定要按设备节点上报的 model 精确命中——不要按品类（light）泛化，
    也不要靠属性名去猜设备行为。没登记的型号完全走默认路径。
    """

    #: 额外参与轮询的属性名（只读、不渲染）；空 = 不追加任何请求
    extra: tuple[str, ...] = ()
    #: 模式激活判据用的属性名；空 = 仍用 ``mode``(2,5) 自己的值（默认行为）
    mode_active: str = ""
    #: 一句"为什么需要这条适配"（实测结论，写给下一个人看）；不参与任何逻辑
    note: str = ""


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
      宁可导入期就报错，也不要线上随机表现。
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
            table[model] = adapter
            origin[model] = name
    return table


__all__ = ["ModelAdapter", "check_model", "collect"]
