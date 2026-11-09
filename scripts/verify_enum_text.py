# SPDX-License-Identifier: GPL-3.0-or-later
"""枚举短名映射（``core/enum_text.py``）的回归：表本身 + 四层查找 + 接线。

跑法：.venv/Scripts/python.exe -X utf8 scripts/verify_enum_text.py

不联网、不依赖数据目录：所有用例都是构造的；只有在本地存在规格缓存时，
末尾会额外做一次真实规格的覆盖率体检（缓存不存在就跳过）。
"""

from __future__ import annotations

import json
import re
import sys
from types import SimpleNamespace

from mihome.core import adapters, enum_text, paths
from mihome.core.adapters.categories import CATEGORY_TEXT
from mihome.core.catalog import format_value
from mihome.core.enum_text import (
    ENUM_ALIAS,
    ENUM_ALIAS_BY_PROP,
    PROP_ALIAS,
    enum_short,
    prop_short,
)
from mihome.core.spec import localize, parse_raw
from mihome.core.store import Store

PASS = 0
FAIL = 0

LIMIT = 12
#: 允许保留原样的非中文短名（品牌 / 接口名 / 单位）
ASCII_OK = {"HDMI1", "HDMI2", "HDMI 1", "HDMI 2", "NFC", "HomeKit", "IoT", "PM2.5", "MAC 地址"}
#: 允许「原话 = 短名」的键：品牌名，翻译反而怪（NFC / HomeKit）
IDENTITY_OK = {"nfc", "homekit"}

_CJK = re.compile(r"[\u4e00-\u9fff]")


def check(name: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"PASS  {name}  {got!r}")
    else:
        FAIL += 1
        print(f"FAIL  {name}  得到 {got!r} 期望 {want!r}")


def check_true(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS  {name}  {detail}")
    else:
        FAIL += 1
        print(f"FAIL  {name}  {detail}")


def section(n: int, title: str) -> None:
    print(f"\n{n}) {title}")


def main() -> int:
    section(1, "映射表本身")
    for table_name, table in (("ENUM_ALIAS", ENUM_ALIAS), ("PROP_ALIAS", PROP_ALIAS)):
        folded = {}
        dup = []
        for key in table:
            k = enum_text._fold(key)
            if k in folded:
                dup.append(k)
            folded[k] = key
        check_true(f"{table_name} 折叠后无重复键", not dup, f"{len(table)} 条")
        check_true(
            f"{table_name} 没有空键 / 空值",
            all(k.strip() and v.strip() for k, v in table.items()),
            "",
        )
        check_true(
            f"{table_name} 的值都不超过 {LIMIT} 字",
            all(len(v) <= LIMIT for v in table.values()),
            f"最长 {max(len(v) for v in table.values())} 字",
        )
        ident = {
            enum_text._fold(k)
            for k, v in table.items()
            if enum_text._fold(k) == enum_text._fold(v)
        }
        check_true(
            f"{table_name} 里只有白名单允许「原样照抄」",
            ident <= IDENTITY_OK,
            f"原样：{sorted(ident) or '无'}",
        )

    check_true(
        "属性级覆盖的键都是 (URN 名, 原话) 两元组",
        all(isinstance(k, tuple) and len(k) == 2 for k in ENUM_ALIAS_BY_PROP),
        f"{len(ENUM_ALIAS_BY_PROP)} 条",
    )
    check_true(
        "属性级覆盖的键里没有中文（有就是拿 comment 当键）",
        not any(_CJK.search(k[0]) for k in ENUM_ALIAS_BY_PROP),
        "",
    )
    check_true(
        "属性级覆盖的值都不超过 12 字且非空",
        all(v.strip() and len(v) <= LIMIT for v in ENUM_ALIAS_BY_PROP.values()),
        "",
    )

    # 类别层：键必须是 URN 的 category 段（英文、连字符），条目必须过同一套长度/空值检查
    check_true(
        "类别层的键都是 URN category（英文小写连字符）",
        all(re.fullmatch(r"[a-z][a-z0-9-]*", c) for c in CATEGORY_TEXT),
        f"{len(CATEGORY_TEXT)} 个品类",
    )
    check_true(
        "类别层的属性名键里没有中文（有就是拿 comment 当键）",
        not any(_CJK.search(prop) for layer in CATEGORY_TEXT.values() for prop, _, _ in layer.enums),
        "",
    )
    check_true(
        "类别层的短名都不超过 12 字且非空",
        all(
            short.strip() and len(short) <= LIMIT
            for layer in CATEGORY_TEXT.values()
            for _, _, short in layer.enums
        ),
        "",
    )
    check_true(
        "每条类别覆盖都写了 note（说明为什么放在这一层）",
        all(layer.note.strip() for layer in CATEGORY_TEXT.values()),
        "",
    )

    section(2, "查表：大小写 / 空白归一")
    check("On → 开", enum_short("On"), "开")
    check("ON 同一项", enum_short("ON"), "开")
    check("on 同一项", enum_short(" on "), "开")
    check("多余空白不影响（Ac  Mode  With Outlet）", enum_short("Ac  Mode  With Outlet"), "有出风口")
    check("off 小写", enum_short("off"), "关")
    check("null 与 None 都是「无」", (enum_short("null"), enum_short("None")), ("无", "无"))

    section(3, "属性级覆盖：键是 URN 名，不是中文 comment")
    check("Dry@空调 mode → 除湿", enum_short("Dry", "mode", category="air-conditioner"), "除湿")
    check("Dry@wash-mode → 烘干", enum_short("Dry", "wash-mode", category="washer"), "烘干")
    check("Normal@manual-check-results → 正常", enum_short("Normal", "manual-check-results"), "正常")
    check("Normal@其它属性仍是标准", enum_short("Normal", "mode"), "标准")
    check("Open@door-state → 已开", enum_short("Open", "door-state"), "已开")
    check("Open@其它属性仍是开", enum_short("Open", "on"), "开")
    check("属性名传空串也能走全局词表", enum_short("Dry", ""), "除湿")
    # 旧实现用中文 comment 当键（"洗涤流程"），云端改文案就静默失效。现在中文键不再命中，
    # 万一有人照着旧写法加表，这里会立刻红。
    check("中文 label 不再当键（退回全局词表）", enum_short("Dry", "洗涤流程"), "除湿")

    section(4, "位运算前缀：云端的位置标记，界面不要")
    check("（1<<4）前缀被剥掉", enum_short("（1<<4）已上锁(反锁：0；童锁：0)", "door-state", category="lock"), "已上锁")
    check("(2<<4) 半角前缀", enum_short("(2<<4)已开锁", "door-state", category="lock"), "已开锁")
    check("前缀剥掉后才查得到表", enum_short("（3<<4）门未关（门超时未关）", "door-state", category="lock"), "门未关")

    section(5, "类别级覆盖：门锁状态机的压缩")
    lock_raw = {
        "type": "urn:miot-spec-v2:device:lock:0000A038:demo:1",
        "services": [
            {
                "iid": 2,
                "type": "urn:miot-spec-v2:service:door:00007820:demo:1",
                "description": "Door",
                "comment": "门",
                "properties": [
                    {
                        "iid": 1,
                        "type": "urn:miot-spec-v2:property:door-state:00000021:demo:1",
                        "description": "Door State",
                        "comment": "门状态",
                        "format": "uint8",
                        "access": ["read", "notify"],
                        "value-list": [
                            {"value": 0, "description": "Locked", "comment": "（1<<4）已上锁(反锁：0；童锁：0)"},
                            {"value": 1, "description": "Locked Child", "comment": "已上锁(反锁：0；童锁：1)"},
                            {"value": 2, "description": "Locked Bolt", "comment": "已上锁(反锁：1；童锁：0)"},
                            {"value": 3, "description": "Locked Both", "comment": "已上锁(反锁：1；童锁：1)"},
                            {"value": 4, "description": "Open", "comment": "(2<<4)已开锁"},
                            {"value": 5, "description": "Timeout", "comment": "（3<<4）门未关（门超时未关）"},
                        ],
                    }
                ],
            }
        ],
    }
    lock_spec = parse_raw(lock_raw)
    door = lock_spec.properties[0]
    check(
        "门锁门状态压成短名（反锁/童锁是关键信息）",
        [o.display_short for o in door.value_list],
        ["已上锁", "已上锁·童锁", "已上锁·反锁", "已上锁·反锁+童锁", "已开锁", "门未关"],
    )
    check("原文一个字都没丢", door.value_list[1].label, "已上锁(反锁：0；童锁：1)")
    # 类别层只对自己那一类生效：同样一句话在烤箱/洗衣机上不该被改写
    check_true(
        "类别覆盖不串到别的品类",
        enum_short("门未关（门超时未关）", "door-state", category="oven") == "门未关（门超时未关）",
        enum_short("门未关（门超时未关）", "door-state", category="oven"),
    )
    check("品类对上了才生效", enum_short("门未关（门超时未关）", "door-state", category="lock"), "门未关")

    section(6, "三层优先级：型号 > 类别 > 全局；层内属性级 > 裸词")
    probe_raw = {
        "type": "urn:miot-spec-v2:device:air-conditioner:0000A004:demo:1",
        "services": [
            {
                "iid": 2,
                "type": "urn:miot-spec-v2:service:air-conditioner:0000780A:demo:1",
                "description": "Air Conditioner",
                "comment": "空调",
                "properties": [
                    {
                        "iid": 1,
                        "type": "urn:miot-spec-v2:property:mode:00000008:demo:1",
                        "description": "Mode",
                        "comment": "模式",
                        "format": "uint8",
                        "access": ["read", "write"],
                        "value-list": [{"value": 2, "description": "Dry", "comment": "Dry"}],
                    },
                    {
                        "iid": 6,
                        "type": "urn:miot-spec-v2:property:fan-level:00000016:demo:1",
                        "description": "Fan Level",
                        "comment": "",
                        "format": "uint8",
                        "access": ["read"],
                        "value-list": [{"value": 1, "description": "Low", "comment": "Low"}],
                    },
                ],
            }
        ],
    }
    probe = parse_raw(probe_raw)
    model = "demo.airconditioner.m1"
    adapters.MODEL_ADAPTERS[model] = adapters.ModelAdapter(
        text=adapters.TextLayer(
            enums=(("mode", "Dry", "除湿·型号"),),
            props=(("Fan Level", "风量"),),
        ),
        note="测试用：验证型号级覆盖优先级，跑完删掉",
    )
    enum_text._lexicon.cache_clear()
    try:
        check(
            "型号级盖过全局（Dry）",
            enum_short("Dry", "mode", category="air-conditioner", model=model),
            "除湿·型号",
        )
        check(
            "同一个词对别的型号仍是全局结果",
            enum_short("Dry", "mode", category="air-conditioner", model="demo.airconditioner.m2"),
            "除湿",
        )
        check(
            "型号级也能改属性行名",
            prop_short("Fan Level", model=model),
            "风量",
        )
        check("没登记的型号走全局属性表", prop_short("Fan Level"), "风速档位")

        # 层压过精确度：型号层写的是裸词，也要盖住全局/类别层的属性级条目。
        # 反例（曾经的真 bug）：三层摊平成"属性级一张 + 裸词一张"两张表、先查属性级，
        # 于是全局的 ("door-state", "Open") 会排在型号裸词前面，型号规则静默失效。
        word_model = "demo.airconditioner.m3"
        adapters.MODEL_ADAPTERS[word_model] = adapters.ModelAdapter(
            text=adapters.TextLayer(
                words=(("Open", "开启侧"), ("门未关（门超时未关）", "门开着")),
                note="测试用：验证层优先于精确度，跑完删掉",
            ),
            note="测试用：验证层优先于精确度，跑完删掉",
        )
        enum_text._lexicon.cache_clear()
        check(
            "型号裸词压过全局属性级（全局是 已开）",
            enum_short("Open", "door-state", model=word_model),
            "开启侧",
        )
        check(
            "型号裸词压过类别属性级（类别 lock 有这条）",
            enum_short("门未关（门超时未关）", "door-state", category="lock", model=word_model),
            "门开着",
        )
        check("同一个词在没登记的型号上仍是全局结果", enum_short("Open", "door-state"), "已开")
        # 同一层内部仍是"带属性名 > 裸词"：精确度只在层内起作用
        adapters.MODEL_ADAPTERS[word_model] = adapters.ModelAdapter(
            text=adapters.TextLayer(
                words=(("Dry", "裸词"),),
                enums=(("mode", "Dry", "属性级"),),
                note="测试用：层内精确度，跑完删掉",
            ),
            note="测试用：层内精确度，跑完删掉",
        )
        enum_text._lexicon.cache_clear()
        check("同层内属性级优先于裸词", enum_short("Dry", "mode", model=word_model), "属性级")
        check("同层内换个属性就落到裸词", enum_short("Dry", "wash-mode", model=word_model), "裸词")

        local = localize(probe, model)
        check_true("型号级文案会生成新的规格对象", local is not probe, "")
        check("重绑定后的短名", local.properties[0].value_list[0].display_short, "除湿·型号")
        check("重绑定不改云端原文", local.properties[0].value_list[0].label, "Dry")
        check("重绑定后的行名", local.properties[1].display_label, "风量")
        check("源规格没被就地改", probe.properties[0].value_list[0].display_short, "除湿")
        check_true("没登记文案的型号返回原对象（不是副本）", localize(probe, "demo.airconditioner.m2") is probe, "")
        check_true("空型号也返回原对象", localize(probe, None) is probe, "")

        # 记忆在 Store 那边（localize 本身是纯函数）：同一份源规格 + 同一型号必须返回
        # 同一个对象，否则 ``apply_topology`` 的 ``prev.spec is not spec`` 永远为真，
        # 每次拓扑刷新都会把所有卡片重建一遍。
        holder = Store.__new__(Store)
        holder._text_specs = {}
        node = SimpleNamespace(spec_type=probe.urn, model=model)
        first = holder._text_spec(node, {probe.urn: probe})
        check_true("store 记住重绑定结果（同一对象）", first is holder._text_spec(node, {probe.urn: probe}), "")
        check("store 记住的是套过文案的规格", first.properties[0].value_list[0].display_short, "除湿·型号")
        check_true(
            "源规格换了就重算（不能拿旧对象的缓存）",
            holder._text_spec(node, {probe.urn: parse_raw(probe_raw)}) is not first,
            "",
        )
        # 没登记文案的型号，store 直接透传源对象（零成本、零内存）
        plain = SimpleNamespace(spec_type=probe.urn, model="demo.airconditioner.m2")
        check_true(
            "没登记文案时 store 透传源对象",
            holder._text_spec(plain, {probe.urn: probe}) is probe,
            "",
        )
    finally:
        for key in (model, "demo.airconditioner.m3"):
            adapters.MODEL_ADAPTERS.pop(key, None)
        enum_text._lexicon.cache_clear()

    section(7, "规则：故障码 / 档位 / 区域")
    check("C1 CLEAR", enum_short("C1 CLEAR"), "C1 已解除")
    check("E2.1 CLEAR", enum_short("E2.1 CLEAR"), "E2.1 已解除")
    check("PA CLEAR（无数字的码）", enum_short("PA CLEAR"), "PA 已解除")
    check("故障码本身保持原样", enum_short("E2.1"), "E2.1")
    check("Level3", enum_short("Level3"), "3 档")
    check("Lever9（云端拼写错误）", enum_short("Lever9"), "9 档")
    check("Area 5", enum_short("Area 5"), "区域5")
    check("Area3（无空格）", enum_short("Area3"), "区域3")
    check("Room1", enum_short("Room1"), "房间1")

    section(8, "兜底：不认识就不猜")
    check("空串", enum_short(""), "")
    check("None 之外的空", enum_short("   "), "")
    check("表外短词原样", enum_short("Sub"), "Sub")
    check("表外长词截断", enum_short("Unknown thing here"), "Unknown thi…")
    check("短句（未超上限）原样保留，不按逗号切", enum_short("某设备支持，其他不支持"), "某设备支持，其他不支持")
    check("超长中文按逗号切在逗号前", enum_short("某设备支持这种模式，其他设备不支持这种模式"), "某设备支持这种模式")
    check("超长中文按括号切", enum_short("甲（乙丙丁戊己庚辛壬癸子丑寅）"), "甲")
    check("分隔符位置超过上限时退回截断", enum_short("一二三四五六七八九十十一十二，后面"), "一二三四五六七八九十十…")
    check("长度正好等于上限不截", enum_short("一二三四五六七八九十十一", limit=12), "一二三四五六七八九十十一")

    section(9, "幂等：短名再过一遍不该变")
    for sample in ("Auto", "C1 CLEAR", "空场景（灯不在任何情况模式时，上报该属性值）", "Unknown thing here"):
        once = enum_short(sample)
        check(f"{sample[:12]} → 再查一次", enum_short(once), once)

    section(10, "属性名")
    check("英文属性名转中文", prop_short("Abnormal Condition"), "异常情况")
    check("大小写归一", prop_short("battery"), "电量")
    check("中文属性名原样（长也不切）", prop_short("内屏显示视频位置(7档)"), "内屏显示视频位置(7档)")
    check("表外英文短词原样", prop_short("a"), "a")
    check("空串", prop_short(""), "")

    section(11, "接线：解析后的规格已经带短名")
    raw = {
        "type": "urn:miot-spec-v2:device:air-conditioner:0000A004:demo:1",
        "services": [
            {
                "iid": 2,
                "type": "urn:miot-spec-v2:service:air-conditioner:0000780A:demo:1",
                "description": "Air Conditioner",
                "comment": "空调",
                "properties": [
                    {
                        "iid": 1,
                        "type": "urn:miot-spec-v2:property:mode:00000008:demo:1",
                        "description": "Mode",
                        "comment": "模式",
                        "format": "uint8",
                        "access": ["read", "write", "notify"],
                        "value-list": [
                            {"value": 0, "description": "Auto", "comment": "Auto"},
                            {"value": 1, "description": "Cool", "comment": "Cool"},
                            {"value": 2, "description": "Dry", "comment": "Dry"},
                            {"value": 3, "description": "Heat", "comment": "Heat"},
                            {"value": 4, "description": "Fan", "comment": "Fan"},
                        ],
                    },
                    {
                        "iid": 6,
                        "type": "urn:miot-spec-v2:property:fan-level:00000016:demo:1",
                        "description": "Fan Level",
                        "comment": "",
                        "format": "uint8",
                        "access": ["read", "write"],
                        "value-list": [
                            {"value": 0, "description": "Auto"},
                            {"value": 1, "description": "Low"},
                            {"value": 2, "description": "High"},
                        ],
                    },
                ],
            }
        ],
    }
    spec = parse_raw(raw)
    mode = spec.properties[0]
    fan = spec.properties[1]
    check("解析后仍保留云端原文", mode.value_list[1].label, "Cool")
    check("短名已就位", [o.display_short for o in mode.value_list], ["自动", "制冷", "除湿", "制热", "送风"])
    check("空 comment 时退回 description 也能映射", [o.display_short for o in fan.value_list], ["自动", "低", "高"])
    check("没有中文 comment 的属性名走属性表", fan.display_label, "风速档位")
    check("读数格式化用短名（format_value）", format_value(mode, 2), "除湿")
    check("读数格式化别的值", format_value(mode, 4), "送风")

    section(12, "真实规格覆盖率（有缓存才跑）")
    cache = paths.spec_cache_dir()
    files = sorted(cache.glob("*.json")) if cache.exists() else []
    if not files:
        print(f"      跳过：没有规格缓存（{cache}）")
    else:
        total = en = en_ok = long_ = long_ok = 0
        leftover: set[str] = set()
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            s = parse_raw(data.get("raw") or {})
            for p in s.properties:
                for o in p.value_list:
                    total += 1
                    if o.display and not _CJK.search(o.display) and re.search(r"[A-Za-z]", o.display):
                        en += 1
                        if _CJK.search(o.display_short):
                            en_ok += 1
                        else:
                            leftover.add(o.display)
                    if len(o.display) > LIMIT:
                        long_ += 1
                        if len(o.display_short) <= LIMIT:
                            long_ok += 1
        print(f"      规格 {len(files)} 份 · 枚举项 {total} 条")
        check_true(
            "英文文案多数已转中文（≥ 80%）",
            en == 0 or en_ok / en >= 0.8,
            f"{en_ok}/{en}",
        )
        check_true("超长文案全部收进上限", long_ == long_ok, f"{long_ok}/{long_}")
        odd = {t for t in leftover if not re.fullmatch(r"[A-Z]{1,3}\d*(?:\.\d+)?", t)} - ASCII_OK
        check_true("剩余的英文只有故障码与品牌名", not odd, f"例外：{sorted(odd) or '无'}")

    print(f"\n{PASS} 项通过，{FAIL} 项失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
