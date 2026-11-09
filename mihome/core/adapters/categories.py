"""类别级文案覆盖（按 MIoT 设备类别）。

键是 URN 里的 category 段：``urn:miot-spec-v2:device:<category>:<catid>:...``，
例如 ``washer`` / ``air-conditioner`` / ``lock``。**不是**中文品类名，也**不是**型号。

## 为什么文案可以按类别，行为不行

``adapters/<厂商>.py`` 的规矩是"设备特判一律按型号"，因为行为适配（多读一个属性、
换一个判据）写错会让设备拒收命令、状态错乱。文案不是这一类：

* 纯显示，不产生任何 IO，认不出来就退回云端原文（见 ``core/enum_text.py`` 的兜底）；
* 写错了最坏结果是一台设备上某个选项显示得不准，且原文还在 tooltip 里；
* 米家规格里同一个词在不同品类含义确实不同（``Dry`` 在空调是除湿、在洗衣流程是烘干），
  而"品类"这个上下文是 URN 自带、不用额外维护的。

所以这里是"设备特判一律按型号"的**唯一例外**，理由就是上面三条。别把行为规则搬进来。

## 放什么、不放什么

* 优先写 ``enums``（``(属性名, 原话, 短名)``）：带上属性名就不会误伤同品类的其他属性；
* 只有该品类**独有**的词才写 ``words``（裸词表），否则一旦别的品类也有这个词，
  就会继承一个未必对的短名；
* 跨品类通用的词**不要**抄到这里——那属于 ``enum_text.ENUM_ALIAS``，抄一份就会漂移。

新增一条：往对应类别的 ``TextLayer`` 里加一行，然后跑
``scripts/verify_enum_text.py``（导入期也会校验：属性名不能写中文、短名不能超长）。
"""

from __future__ import annotations

from .base import TextLayer, check_text_layer

#: 类别（URN 的 category 段）→ 文案覆盖
CATEGORY_TEXT: dict[str, TextLayer] = {
    # 门锁的 door-state 用位运算描述"反锁/童锁"，云端的 comment 是
    # ``已上锁(反锁：0；童锁：1)`` 这种 12–16 字的状态机写法，界面只能截成
    # 「已上锁(反锁：0」。这些值只在门锁上有意义，所以放品类层，不往全局表里堆。
    # ``（1<<4）`` 这类位运算前缀由 ``enum_text`` 的规则统一剥掉，这里写剥完的形式。
    "lock": TextLayer(
        enums=(
            ("door-state", "已上锁(反锁：0；童锁：0)", "已上锁"),
            ("door-state", "已上锁(反锁：0；童锁：1)", "已上锁·童锁"),
            ("door-state", "已上锁(反锁：1；童锁：0)", "已上锁·反锁"),
            ("door-state", "已上锁(反锁：1；童锁：1)", "已上锁·反锁+童锁"),
            ("door-state", "门未关（门超时未关）", "门未关"),
        ),
        note="门锁状态机写法太长，反锁/童锁是关键信息，压成 4 个字",
    ),
}

for _category, _layer in CATEGORY_TEXT.items():
    check_text_layer(f"categories.py:{_category}", _layer)


def find_category_text(category: str | None) -> TextLayer | None:
    """按类别取文案覆盖；没登记返回 ``None``。"""
    if not category:
        return None
    return CATEGORY_TEXT.get(category)


__all__ = ["CATEGORY_TEXT", "find_category_text"]
