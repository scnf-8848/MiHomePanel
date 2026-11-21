"""适配规则：型号级（按厂商分文件）+ 类别级（``categories.py``）。

查找键是设备节点原样上报的 **model**，例如 ``yeelink.light.lamp22``，精确匹配、
不做任何模糊匹配。这里的每一条都是"某台设备实测出来的行为"，**不能按品类推广**。

唯一的例外是**文案**（``TextLayer``）：纯显示、认不出就退回云端原文、没有 IO，
所以允许按类别给（见 ``categories.py``）。别把这个例外扩大到 ``extra`` /
``mode_active`` 这类行为字段上。

为什么按厂商分文件，而不是按品类（灯 / 插座 / 风扇…）：

* 查找键是型号，型号的第一段就是厂商，``yeelink.py`` 与 ``yeelink.*`` 天然对得上；
* 按品类分会让"灯都差不多"这件事被文件名反复暗示，而这正是本项目明确禁止的思路
  （行为特判一律按型号，见 ``base.ModelAdapter`` 的说明）；
* 文件数与厂商数同阶，不会被型号数撑爆。

新增一台设备的适配：

1. 打开对应厂商的文件（没有就新建 ``<厂商>.py``，照 ``yeelink.py`` 的写法）；
2. 加一条 ``"完整型号": ModelAdapter(extra=(...), mode_active=..., text=..., note="为什么")``；
3. 如果新建了文件，在下面 ``_MODULES`` 里加一行 import 与一项——**必须显式 import**，
   不要改成 ``pkgutil`` 自动发现：打包成 exe 时静态分析看不到动态导入的模块，
   会出现"开发机有适配、装完却没适配"这种最难查的故障。

只想改文案、不涉及设备行为时，别动型号表：跨品类的词加进
``core/enum_text.py`` 的全局表，某个品类独有的加进 ``categories.py``，
只有"这台型号独有"的才写进 ``ModelAdapter.text``。

规则本身不写盘、不是用户配置：每次启动按设备上报的 model 重建，改规则要发版。

安全退化：``extra`` 里的属性在 spec 里找不到、或运行时读不到，就当没登记，
一切回到默认行为（模式高亮按 ``mode``(2,5)），不报错、不空高亮。文案同理，
查不到就退回云端原文。
"""

from __future__ import annotations

from . import categories, dmaker, xiaomi, yeelink
from .base import (
    ModelAdapter,
    TextLayer,
    check_adapter,
    check_batteries,
    check_model,
    check_text_layer,
    collect,
)

#: 参与汇总的厂商模块。新增厂商文件后在这里登记，顺序不影响结果（重复型号会报错）。
_MODULES = (yeelink, dmaker, xiaomi)

#: 型号 → 适配规则（唯一查找表）
MODEL_ADAPTERS: dict[str, ModelAdapter] = collect(
    (module.__name__.rsplit(".", 1)[-1], module.ADAPTERS) for module in _MODULES
)


#: 能力字段 → 人话（清单与诊断输出用；键必须与 ``ModelAdapter.fields`` 一致）
FIELD_LABELS: dict[str, str] = {
    "extra": "多读属性",
    "mode_active": "模式判据",
    "speed_by_mode": "按模式换控件",
    "batteries": "电量拆行",
    "text": "文案覆盖",
    "geek": "极客版入口",
}


def describe_all() -> list[dict[str, object]]:
    """**已适配型号总清单**（按型号排序），一条适配一行。

    这是回答"这台设备适配过没有 / 为什么这么适配"的唯一入口：

    * 自己用：准备适配一台新设备前先跑 ``scripts/list_adapters.py``，看到型号或
      同厂商同形态的先例，就别重复造规则，直接按先例的字段加；
    * 别的开发者：拿到源码想知道"我的门锁该怎么适配"，从这里找同品类先例，
      再顺着 ``doc`` 去读实测结论。

    ``fields`` 是英文键（与 ``ModelAdapter.fields`` 一致，方便脚本判断），
    展示层用 :data:`FIELD_LABELS` 翻成人话。
    """
    return [
        {
            "model": model,
            "vendor": model.split(".", 1)[0],
            "device": adapter.device,
            "fields": list(adapter.fields),
            "note": adapter.note,
            "doc": adapter.doc,
        }
        for model, adapter in sorted(MODEL_ADAPTERS.items())
    ]


def find_adapter(model: str | None) -> ModelAdapter | None:
    """按型号取适配规则；没登记（或 model 为空）返回 ``None``。

    ``build_card`` 用它代替直接查表，调用方不必关心表放在哪个模块里。
    """
    if not model:
        return None
    return MODEL_ADAPTERS.get(model)


__all__ = [
    "CATEGORY_TEXT",
    "MODEL_ADAPTERS",
    "FIELD_LABELS",
    "ModelAdapter",
    "TextLayer",
    "categories",
    "check_adapter",
    "describe_all",
    "check_batteries",
    "check_model",
    "check_text_layer",
    "collect",
    "find_adapter",
]

