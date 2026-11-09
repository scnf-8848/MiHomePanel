# SPDX-License-Identifier: GPL-3.0-or-later
"""云端文案 → 界面短名（枚举项与属性名）。

**为什么需要这一层**：米家规格里 ``value-list[].comment`` 是给开发者看的，不是给人看的。
实测 49 份规格、1850 个枚举项里，文案长这样：

- 英文：``Quick Single Click``、``Frequent Wrong Password``、``Ac Mode With Outlet``
- 整句中文：``空场景（灯不在任何情况模式时，上报该属性值）``
- 带条件的告警：``大电池耗尽且小电池电量低 (大电池 =0% &小电池<=20%)``
- 纯编号：``C1 CLEAR``、``Level3``、``Area 5``

而界面上的位置只有：详情页下拉框（最宽 240px、按 10 个字算宽）、读数行右侧一个
``QLabel``。直接铺原文的结果是硬切（"0关闭待机息屏，待…"）或者把左边属性名挤没。
所以这里做一次**人工短名映射**，原文不丢——``ValueOption.label`` 仍然是云端原话，
短名单独放在 ``ValueOption.short``，需要的时候可以拿原文当 tooltip。

## 三层查找

优先级从高到低，**层与层之间是纯粹的"谁更具体谁说话"**：

1. **型号级**——``adapters/<厂商>.py`` 里那台设备自己的 ``ModelAdapter.text``；
2. **类别级**——``adapters/categories.py``，键是 URN 里的 category（``washer``）；
3. **全局词汇**——本模块的两张表，跨品类通用的词（``On`` / ``Auto`` / ``Success``）。

每层内部先查"属性 + 原话"、再查裸词：带属性名的条目只对那个属性生效，比裸词精确。
但**层永远压过精确度**——型号层写了个裸词，也照样盖住全局的属性级条目。宁可让
"我为一台设备专门写的规则"生效，也不要让它被一条更通用的词条悄悄压掉。

之后才是 ``_RULES``（``C1 CLEAR`` / ``LevelN`` / ``Area N`` 这类有规律的），
最后按分隔符切一刀、超长截断。三层里都没有的就退化成加入这一层之前的行为——
**新增映射只会让文案更好，不会让某个原本正常的选项变坏。**

为什么低优先级不覆盖高优先级：三层是"更具体的先说话"。合并时**按层保留**（见
:func:`_lexicon`），查找逐层问、层内"先属性级、再裸词"，最多六次 dict 命中，
仍在微秒级。**不能把三层摊平成两张表**（属性级一张、裸词一张）——那样"全局的
属性级条目"会排到"型号的裸词"前面：某台设备明明有自己的规则，却会被一条更通用的
词条盖掉，而且从调用方完全看不出来。

## 键为什么是 URN 名，不是中文属性名

属性级覆盖早期用中文 ``comment`` 当键（``("洗涤流程", "Dry")``），云端改一个字
就静默失效——而且失效时界面只是"突然又变英文了"，没有任何报错。现在一律用
``PropertySpec.name``（``wash-mode``）：那是协议里的标识，不会变。

## 和型号行为适配的关系

行为适配（多读属性、换模式判据）写错会让设备拒收命令，所以**一律按型号**；
文案是纯显示、可退化，允许按**类别**给。这是那条规矩的唯一例外，理由写在
``adapters/categories.py`` 顶部，别把它扩大化。

维护方式：跨品类的词加进本模块的 ``ENUM_ALIAS``，某个品类独有的加进
``adapters/categories.py``，只有某台型号独有的才写进 ``ModelAdapter.text``。
改完跑 ``scripts/verify_enum_text.py``（表自检、三层优先级、接线）与
``scripts/probe_enums.py``（看真实规格里还剩多少没映射）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache

from .adapters import find_adapter
from .adapters.base import TextLayer
from .adapters.categories import find_category_text

# ---------------------------------------------------------------------------
# 枚举项文案
# ---------------------------------------------------------------------------
#: ``云端原话 → 短名``。查表时忽略大小写与多余空白（``On`` / ``ON`` / ``on`` 同一项），
#: 所以同一个词不要写两遍。
ENUM_ALIAS: dict[str, str] = {
    # -- 开关态 ---------------------------------------------------------
    "On": "开",
    "Off": "关",
    "Open": "开",
    "Close": "关",
    "Closed": "已关",
    "Enable": "开启",
    "Disable": "关闭",
    "Always-Off": "常关",
    "Close-Close": "关→关",
    "Close-Open": "关→开",
    "On-or-brightness": "开/亮度",
    "On-or-colortemp": "开/色温",
    "RADAR-ON": "雷达开",
    "RADAR-OFF": "雷达关",
    # -- 档位 / 程度 -----------------------------------------------------
    "Auto": "自动",
    "High": "高",
    "Middle": "中",
    "Medium": "中",
    "Low": "低",
    "Max": "最大",
    "Normal": "标准",
    "Standard": "标准",
    "Custom": "自定义",
    "User-defined": "自定义",
    "Default": "默认",
    "None": "无",
    "null": "无",
    "Few": "少",
    "Many": "多",
    "Lots Of": "大量",
    # -- 空调 -----------------------------------------------------------
    "Cool": "制冷",
    "Heat": "制热",
    "Dry": "除湿",
    "Fan": "送风",
    "Weak Cold": "微冷",
    "Small Cold": "小冷",
    "Comfort": "舒适",
    "Big Cold": "大冷",
    "Strong Cold": "强冷",
    "Weak Heat": "微暖",
    "Small Heat": "小暖",
    "Big Heat": "大暖",
    "Strong Heat": "强暖",
    "Ac Mode With Outlet": "有出风口",
    "Ac Mode Without Outlet": "无出风口",
    "Cloud Recommendation": "云端推荐",
    # -- 灯具模式 / 灯效 -------------------------------------------------
    "Anti Blue-light Mode": "防蓝光",
    "Flow Mode": "流光",
    "Free Mode": "自由",
    "Movie Mode": "影院",
    "Reading Mode": "阅读",
    "Warm Mode": "暖光",
    "Working Mode": "工作",
    "Gradient": "渐变",
    "Immediately": "立即",
    "Blink": "闪烁",
    # -- 按键动作 --------------------------------------------------------
    "Brightness-switch": "亮度切换",
    "Colortemperature-add": "色温+",
    "Colortemperatre-dec": "色温−",
    "Colortemperature-sw": "色温切换",
    "Start Delay Off": "延时关",
    "Start Pomodoro Mode": "番茄钟",
    "Quick Single Click": "单击",
    "Multiple Click": "连击",
    "Control": "互控",
    "Decontrol": "解除互控",
    "Add": "添加",
    "Del": "删除",
    "Get": "获取",
    "Remote-add": "添加遥控",
    "Reset All Params": "恢复默认",
    # -- 上报状态 --------------------------------------------------------
    "Default-state": "默认状态",
    "Professional-setting": "专业设置",
    "My-mode": "自定义",
    "My Mode 1": "自定义1",
    "My Mode 2": "自定义2",
    "My Mode 3": "自定义3",
    "My Mode 4": "自定义4",
    # -- 绑定 / 匹配 -----------------------------------------------------
    "Bind": "绑定",
    "Unbind": "解绑",
    "Matching": "已匹配",
    "Out Of Matching": "未匹配",
    # -- 洗衣 / 清洁 -----------------------------------------------------
    "Start Wash": "开始洗涤",
    "Finish Wash": "完成洗涤",
    "Quick Wash": "快洗",
    "Stain Wash": "去渍洗",
    "Rinse Spin": "漂洗脱水",
    "Spin": "脱水",
    "Drum Clean": "筒清洁",
    "Pleased Wash Dry": "洗烘",
    "Silk": "真丝",
    "Silk Stockings": "丝袜",
    "Modal": "莫代尔",
    "Lace": "蕾丝",
    "Mix": "混合",
    "Bra": "内衣",
    "Underpants": "内裤",
    "Swimsuit": "泳衣",
    "Yoga Clothes": "瑜伽服",
    "Slip Dress": "吊带裙",
    "Pillow Case": "枕套",
    "Sock Wash": "袜子",
    "Towel Wash": "毛巾",
    "Laundry Detergent": "洗衣液",
    # -- 雷达 / 感应 -----------------------------------------------------
    "Radar-only": "仅雷达",
    "Pir-only": "仅红外",
    "Radar-and-Pir": "雷达+红外",
    "Radar-and-Pir-High-Performance": "雷达+红外·高灵敏",
    "Radar-and-Pir-Standard": "雷达+红外·标准",
    "One Region": "单区域",
    "Two Region": "双区域",
    "Three Region": "三区域",
    # -- 门锁告警（原文是整句，读数行放不下）-----------------------------
    "Lockpicking": "疑似撬锁",
    "Door Stuck": "门卡住",
    "Open Over Time": "开门超时",
    "Reset Button Pressed": "重置键被按下",
    "Abnormal Fingerprint Sensor": "指纹传感器异常",
    "Frequent Wrong Password": "密码错误频繁",
    "Frequent Wrong Fingerprint": "指纹错误频繁",
    "Frequent Wrong NFC": "NFC 错误频繁",
    "Frequent Wrong Unlock": "开锁尝试频繁",
    "Arming State Unlocked Indoor": "室内未布防",
    "Duplicate Credentials": "凭据重复",
    "Duplicate Parameter": "参数重复",
    "Received Command Successfully Executing": "指令执行中",
    "Device Busy": "设备忙",
    "Internal Error": "内部错误",
    "Parameter Error": "参数错误",
    "Memory Full": "存储已满",
    "Not Found": "未找到",
    "Timeout": "超时",
    "Success": "成功",
    "Fail": "失败",
    # -- 开锁方式 / 用户 --------------------------------------------------
    "Fingerprint": "指纹",
    "Password": "密码",
    "One-time Password": "一次性密码",
    "NFC": "NFC",
    "BLE": "蓝牙",
    "HomeKit": "HomeKit",
    "Double Verification": "双重验证",
    "Manual Indoor": "室内手动",
    "Indoor": "室内",
    "Outdoor": "室外",
    "Owner": "主人",
    "Manager": "管理员",
    "Normal User": "普通用户",
    "All Users": "全部用户",
    "All Normal Users": "全部普通用户",
    "Frequent Visitors": "常访客",
    "Visitor": "访客",
    "Virtual-user": "虚拟用户",
    "Primary-user": "一级账户",
    "Man": "男",
    "Woman": "女",
    # -- 状态 / 媒体 -----------------------------------------------------
    "Online": "在线",
    "Offline": "离线",
    "Unknown": "未知",
    "Idle": "空闲",
    "Running": "运行中",
    "Reset": "复位",
    "Playing": "播放中",
    "Pause": "暂停",
    "Stop": "停止",
    "Full": "已满",
    "Not Full": "未满",
    "All": "全部",
    "Horizon": "水平",
    "Vertical": "垂直",
    "NotSupport": "不支持",
    "Sleep": "睡眠",
    "Quick": "快速",
    "Tone": "提示音",
    "Default Area": "默认区域",
    "Type 1": "类型1",
    "Type 2": "类型2",
    "Running15": "运行15分",
    "HDMI 1": "HDMI1",
    "HDMI 2": "HDMI2",
    "Temp-C": "摄氏",
    "Temp-F": "华氏",
    "Error": "错误",
    "Others": "其他",
    "Other": "其他",
    "No": "否",
    "Abnormal": "异常",
    "No Failure": "无故障",
    "No Faults": "无故障",
    "AlwaysOn": "常开",
    "Stateful": "有状态",
    "Stateless": "无状态",
    "Smart": "智能",
    "Timing": "定时",
    # -- 连接 / 供电 -----------------------------------------------------
    "Avaliable": "已连接",
    "FullConnection": "已连接",
    "Invalid": "未连接",
    "Battery": "电池",
    "BatteryMode": "电池供电",
    "PowerDirect": "直连供电",
    "SolarMode": "太阳能",
    "SolarBoard": "太阳能板",
    "Battery-empty": "电量耗尽",
    "Battery-low": "电量低",
    "Battery-enough": "电量充足",
    "AutoActivate": "自动激活",
    "ButtonActivate": "按键激活",
    "Neutral Wire": "有零线",
    "No Neutral": "无零线",
    "LongTime": "长期",
    "ShortTime": "短期",
    "UltraTime": "超长",
    "Permanent": "永久",
    # -- 感应 / 灵敏度 ---------------------------------------------------
    "Factory-Mode": "工厂模式",
    "High Performance": "高性能",
    "Middle Performance": "中性能",
    "Low Performance": "低性能",
    "High Light": "高亮",
    "Middle Light": "中亮",
    "Hihg": "高",
    # -- 开关类型 / 单位 / 其他 -------------------------------------------
    "Hand": "手动",
    "Key": "按键",
    "Stove": "灶具",
    "Jin": "斤",
    "kg": "千克",
    "Cloud": "云端",
    "Official": "官方",
    "Get History": "获取记录",
    "Start Pub": "开始发布",
    "Stop Pub": "停止发布",
    "SafePower": "断电保护",
    # -- 洗衣机工作模式 ---------------------------------------------------
    "Baby-mode": "婴儿",
    "Normal-mode": "标准",
    "Once-mode": "单次",
    "Visitor-mode": "访客",
    "Balance-mode": "均衡",
    # -- 门锁异常的整句（短名 + 原文留在 tooltip）-------------------------
    "空场景（灯不在任何情况模式时，上报该属性值）": "空场景",
    "电视模式（影院模式）": "电视模式",
    "大电池电量耗尽，请及时更换或充电（大电池 = 0% )": "大电池耗尽",
    "小电池电量耗尽，请及时更换或充电（小电池 = 0% ）": "小电池耗尽",
    "小电池耗尽且大电池电量低（小电池 = 0%  & 0 < 大电池 <=10%）": "大小电池均低",
    "大电池耗尽且小电池电量低 (大电池 =0% &小电池<=20%)": "大小电池均低",
    "门锁传感器频繁触发，请将摄像头工作模式调整为省电模式或联系客服处理": "传感器频繁触发",
    "锂电池电量即将耗尽，人脸、掌静脉开锁及摄像头功能不可用": "锂电池将耗尽",
    "锂电池已充满，电量低于85%后再充电有利于电池寿命": "锂电池已充满",
    "锂电池被取下，人脸、掌静脉开锁及摄像头功能不可用": "锂电池被取下",
    "门锁摄像头电量即将耗尽（一般指低于百分之五）": "摄像头电量将尽",
    "门锁摄像头电量低（一般指低于百分之十）": "摄像头电量低",
    "门锁电量即将耗尽（一般指低于百分之五）": "门锁电量将尽",
    "面容识别异常频繁启动，已智能调低灵敏度": "面容识别频繁触发",
    "电量不足，猫眼功能和门内屏暂停使用": "电量不足",
    "负载电流低，仅无线功能可用，部分功能异常，需提升负载功率。": "负载电流低",
    "（1<<4）已上锁(反锁：0；童锁：0)": "已上锁",
    "（3<<4）门未关（门超时未关）": "门未关·超时",
    "门锁电量低（一般指低于百分之十）": "门锁电量低",
    "5号电池电量低，请及时更换电池": "5号电池电量低",
    "检测到门锁超时未关，请注意查看": "门锁超时未关",
    "中（识别距离适中，约0.8米）": "中",
}

#: 同一个词在不同属性下含义不同时的覆盖：``(属性名, 云端原话) → 短名``。
#: **属性名是 URN 名**（``PropertySpec.name``，如 ``wash-mode``），不是云端 ``comment``
#: （"洗涤流程"）——后者会被云端改动，改了就静默失效。
#: 只有"跨品类都会遇到、且确实冲突"的才写在这里；某个品类独有的放
#: ``adapters/categories.py``，某台型号独有的放 ``ModelAdapter.text``。
ENUM_ALIAS_BY_PROP: dict[tuple[str, str], str] = {
    # ``Dry``：空调 ``mode`` 里是"除湿"，洗衣机 ``wash-mode`` 里是"烘干"
    ("wash-mode", "Dry"): "烘干",
    # ``Normal``：摄像头/烤箱的手动检测结果是"正常"，洗衣机工作模式是"标准"
    ("manual-check-results", "Normal"): "正常",
    # ``Default``：空调房间面积里指"默认面积"
    ("room-size", "Default"): "默认面积",
    # ``Open``：门状态是"已开"，与开关态的"开"区分（同一个词，语义更具体）
    ("door-state", "Open"): "已开",
}

# ---------------------------------------------------------------------------
# 属性名
# ---------------------------------------------------------------------------
#: 云端属性名 → 中文行名。只收录**确实会出现在界面上**的那些（读数行 / 更多功能），
#: ``Device Manufacturer`` 这类静态元数据也一并转，免得"更多功能"里冒出英文。
PROP_ALIAS: dict[str, str] = {
    "Device Manufacturer": "厂商",
    "Device Model": "型号",
    "Current Firmware Version": "固件版本",
    "Device Serial Number": "序列号",
    "SN": "序列号",
    "Mac": "MAC 地址",
    "firmwareversion": "固件版本",
    "Battery Level": "电量",
    "battery": "电量",
    "toggle": "开关",
    "Switch Status": "开关状态",
    "Status": "状态",
    "Mode": "模式",
    "Mode for IR": "红外模式",
    "Target Temperature": "目标温度",
    "Temperature for IR": "红外温度",
    "Fan Level": "风速档位",
    "Vertical Swing": "上下扫风",
    "Match State": "匹配状态",
    "Ac Work Mode": "空调工作模式",
    "Ac State": "空调状态",
    "Ac Type": "空调类型",
    "Electric Power": "功率",
    "Brightness": "亮度",
    "Volume": "音量",
    "Mute": "静音",
    "Playing State": "播放状态",
    "Text Content": "文本内容",
    "Silent Execution": "静默执行",
    "Sleep Mode": "睡眠模式",
    "Current Time": "当前时间",
    "Abnormal Condition": "异常情况",
    "Operation Method": "开锁方式",
    "Operation ID": "操作编号",
    "Brand Id": "品牌编号",
    "Remote Id": "遥控器编号",
    "Audio Id": "语音编号",
    "Ac Ctrl Library": "空调码库",
    "Ac Ctrl Library Crc32": "码库校验",
}

# ---------------------------------------------------------------------------
# 规则
# ---------------------------------------------------------------------------
#: 有规律的一类文案。顺序敏感：先匹配到的先用。
_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # 故障码的"已解除"（C1 CLEAR / E2.1 CLEAR / PA CLEAR / U8 CLEAR…）——
    # 码本身是行业码，保留原样，只把后缀翻译掉
    (re.compile(r"^([A-Z]{1,3}\d*(?:\.\d+)?)\s+CLEAR$"), r"\1 已解除"),
    # AI 抗干扰 / 风机档位：Level1…Level8、Lever0…Lever9（云端把 Level 拼成了 Lever）
    (re.compile(r"^Leve[lr]\s*(\d+)$"), r"\1 档"),
    # 内屏显示区域 / 房间面积
    (re.compile(r"^Area\s*(\d+)$"), r"区域\1"),
    (re.compile(r"^Room\s*(\d+)$"), r"房间\1"),
)

#: 认不出来的长文案按这些分隔符切第一段（切点必须落在 ``limit`` 之前）
_SEPARATORS = ("，", "；", "。", "（", "(", "：", ":")

#: 云端给位运算状态加的前缀：``（1<<4）已上锁`` / ``(2<<4)已开锁``。
#: 它是写给开发者的位置说明，界面上一律不要；先剥掉再查表，表里的键写的是剥完的形态。
_BIT_PREFIX = re.compile(r"^[（(]\s*\d+\s*<<\s*\d+\s*[）)]\s*")


def _norm(text: str) -> str:
    """入口归一化：去首尾空白、剥掉位运算前缀（表里的键都是剥完的形态）。"""
    return _BIT_PREFIX.sub("", (text or "").strip())


def _fold(text: str) -> str:
    """查表用的归一化：去掉首尾空白、压缩内部空白、统一小写。"""
    return re.sub(r"\s+", " ", text.strip()).lower()


_LOOKUP: dict[str, str] = {_fold(k): v for k, v in ENUM_ALIAS.items()}
_PROP_LOOKUP: dict[str, str] = {_fold(k): v for k, v in PROP_ALIAS.items()}
_BY_PROP: dict[tuple[str, str], str] = {
    (_fold(prop), _fold(text)): short for (prop, text), short in ENUM_ALIAS_BY_PROP.items()
}


@dataclass(frozen=True, slots=True)
class _Tier:
    """一层展开后的查表：属性级 + 裸词 + 属性名。"""

    by_prop: Mapping[tuple[str, str], str]
    by_text: Mapping[str, str]
    props: Mapping[str, str]

    def hit(self, prop: str, text: str) -> str:
        """本层命中：带属性名时先查属性级，没有再退到裸词。"""
        if prop:
            short = self.by_prop.get((prop, text))
            if short:
                return short
        return self.by_text.get(text, "")


@dataclass(frozen=True, slots=True)
class _Lexicon:
    """按"从具体到通用"排好的层，查表就是逐层问一遍。"""

    tiers: tuple[_Tier, ...]
    prop_name: Mapping[str, str]

    def enum(self, prop: str, text: str) -> str:
        for tier in self.tiers:
            short = tier.hit(prop, text)
            if short:
                return short
        return ""


def _tier(layer: TextLayer | None) -> _Tier | None:
    """把一层 ``TextLayer`` 摊成查表用的 dict；空层返回 ``None``。

    层内用 ``setdefault``：同一层里写了重复键时"先写的赢"，与手写顺序一致。
    """
    if layer is None or layer.empty:
        return None
    by_prop: dict[tuple[str, str], str] = {}
    by_text: dict[str, str] = {}
    props: dict[str, str] = {}
    for prop, text, short in layer.enums:
        by_prop.setdefault((_fold(prop), _fold(text)), short)
    for text, short in layer.words:
        by_text.setdefault(_fold(text), short)
    for prop, name in layer.props:
        props.setdefault(_fold(prop), name)
    return _Tier(by_prop, by_text, props)


#: 全局词汇（跨品类通用词 + 属性级覆盖）。键在模块加载时已经是 ``_fold`` 形态。
_GLOBAL_TIER = _Tier(_BY_PROP, _LOOKUP, _PROP_LOOKUP)


@lru_cache(maxsize=512)
def _lexicon(category: str, model: str) -> _Lexicon:
    """把型号级 / 类别级 / 全局三层排成"从具体到通用"的序列。

    **每层各自保留**，不合并成一张大表：合并会丢掉"层"这个维度，让全局的属性级
    条目排到型号的裸词前面（见模块文档）。层最多三层，每层最多两次 dict 命中，
    代价可以忽略；换来的是"型号写了就一定生效"这个能讲清楚的规则。
    """
    adapter = find_adapter(model)
    tiers = tuple(
        tier
        for tier in (
            _tier(adapter.text if adapter is not None else None),
            _tier(find_category_text(category)),
            _GLOBAL_TIER,
        )
        if tier is not None
    )
    prop_name: dict[str, str] = {}
    for tier in tiers:
        for prop, name in tier.props.items():
            prop_name.setdefault(prop, name)
    return _Lexicon(tiers, prop_name)


def enum_short(
    text: str,
    prop: str = "",
    *,
    category: str = "",
    model: str = "",
    limit: int = 12,
) -> str:
    """枚举项短名：三层覆盖 → 规则 → 机械截断。

    ``prop`` 传属性的 **URN 名**（``PropertySpec.name``）；``category`` 传 URN 里的
    category 段，``model`` 传设备节点上报的型号。三个上下文都是可选的——
    不传就只有全局词汇表生效，和只有这一层时一样。
    """
    raw = _norm(text)
    if not raw:
        return ""
    hit = _lexicon(category, model).enum(_fold(prop) if prop else "", _fold(raw))
    if hit:
        return hit
    for pattern, repl in _RULES:
        if pattern.match(raw):
            out = pattern.sub(repl, raw)
            return out if len(out) <= limit else _cut(out, limit)
    if len(raw) <= limit:
        return raw
    return _cut(raw, limit)


def prop_short(text: str, *, category: str = "", model: str = "", limit: int = 12) -> str:
    """属性名短名：查属性名表，没有就退回 ``enum_short``。

    中文属性名**原样返回、不截断**——行名控件（``ElidedLabel``）自己会省略，
    这里先切一刀反而丢信息（"内屏显示视频位置(7档)" 这种长名字还要靠 tooltip 看全）。
    只有纯英文属性名才需要映射/截断。
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    hit = _lexicon(category, model).prop_name.get(_fold(raw))
    if hit:
        return hit
    if _has_cjk(raw):
        return raw
    return enum_short(raw, category=category, model=model, limit=limit)


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _cut(text: str, limit: int) -> str:
    """按分隔符切第一段；切不出来就截断加省略号。"""
    for sep in _SEPARATORS:
        idx = text.find(sep)
        if 0 < idx <= limit:
            return text[:idx]
    return text[: limit - 1] + "…"


def has_alias(text: str, prop: str = "", *, category: str = "", model: str = "") -> bool:
    """这条文案是不是已经人工映射过（给探测脚本统计覆盖率用）。"""
    raw = _norm(text)
    if not raw:
        return False
    if _lexicon(category, model).enum(_fold(prop) if prop else "", _fold(raw)):
        return True
    return any(p.match(raw) for p, _ in _RULES)


__all__ = [
    "ENUM_ALIAS",
    "ENUM_ALIAS_BY_PROP",
    "PROP_ALIAS",
    "enum_short",
    "has_alias",
    "prop_short",
]
