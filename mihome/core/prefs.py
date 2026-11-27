# SPDX-License-Identifier: GPL-3.0-or-later
"""用户偏好：排序、隐藏设备、家庭标签、卡片样式。

卡片样式是「宽度 + 内容」的一个选择（`did → style_id`），不再单独存大小；
``card_styles`` 是稀疏字典，只记显式改过的设备。


云端给的家庭 / 房间 / 设备 / 场景顺序是它自己的逻辑（多半是创建时间），
用起来未必顺手。这里只做一件事：把"云端顺序"和"用户顺序"合成一份最终列表。

合并规则很简单，也是唯一一种不会丢条目的规则：

    用户排过的按用户顺序在前，没排过的（新增的设备、刚建的房间）按云端顺序追加。

这样即使换设备、加房间，也不会出现"新设备被藏起来找不到"。

隐藏设备是另一条独立偏好。被隐藏的设备不进入原房间，而是集中到每个家庭末尾的
"已隐藏设备"分组。它解决的是一个很实际的问题：56 台设备里真正天天要点的可能不到
二十台，剩下的网关、传感器、路由子设备铺在墙上只会稀释注意力。隐藏不是删除——
卡片还在、轮询范围也不变（隐藏分组里仍然能看实时状态），只是从主视图里挪开。

文件位置：``<data>/prefs.json``，和凭据、缓存放在一起。写入是原子的，掉了也不会
留下半个 JSON。

**落盘失败不许再无声无息**（2026-11-21）：以前 ``save()`` 只用一句
``except OSError: pass`` 收尾，结果是"改完设置、重启就还原"，用户和我们都拿不到
任何线索。现在每次读 / 写都往 ``logs/prefs.log`` 记一行（路径 + 字节数 + 关键
字段），失败时记原始异常；原子替换失败还会**兜底直写一次**（杀软 / 索引器短暂
占用 ``prefs.json`` 时非常常见）。读不出来的坏 JSON 会被改名成
``prefs.json.broken-<时间>`` 留档，绝不让下一次保存把它盖掉。
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import applog, paths
from .consumables import STYLES as CONSUMABLE_STYLES
from .consumables import STYLE_DEFAULT as CONSUMABLE_STYLE_DEFAULT
from .consumables import Consumable, parse as parse_consumable

#: 隐藏分组（首页每个家庭末尾那一块）的显示名。用中文常量而不是枚举，因为它要
#: 直接画在界面上。叫「已隐藏设备」而不是「隐藏设备」：这一组里装的是**已经藏起来
#: 的**设备，是结果不是动作；「隐藏设备」读起来像右键菜单里那个动作，容易混。
#: 它只用于显示；隐藏分组的内部标识是 ``HIDDEN_ROOM_ID``。隐藏项的顺序走
#: ``order_hidden``（``self.hidden`` 列表），不按房间落盘，所以改名不会丢数据。
HIDDEN_ROOM = "已隐藏设备"
#: 隐藏分组的内部房间 id。它不是云端房间，只用于 SideNav / WallView 的定位；
#: 隐藏项的真正顺序走 ``self.hidden``，不落进 ``device_order``。
HIDDEN_ROOM_ID = "__hidden__"

#: 偏好文件版本。当前结构：排序键一律是云端 ``home_id`` / ``room_id``。v11 改完这
#: 一代后不再按版本分支读：本机文件已经规范成 v11，旧文件名 / 旧结构的分支全部删除。
#: v12（2026-11-15）新增 ``last_page`` / ``restore_last_page``；v13 同一功能细化到
#: 房间，新增 ``last_home`` / ``last_room``。v14（2026-11-18）新增耗材页的
#: ``consumables``（监控项清单）与 ``consumable_sort``；v15 把卡片样式从每条监控项
#: 里提出来，改成整页一个 ``consumable_style``；v16（2026-11-19）新增监控页的
#: ``monitor_layout`` 与 ``monitor_cells``；v17（2026-11-20）新增桌面挂件的
#: ``widget_*`` 一组；v18（2026-11-20 晚）把 ``widget_enabled`` 改名
#: ``widget_autostart``（只管启动时是否自动打开），并把挂件默认档从 ``desktop``
#: 改成 ``normal`` —— 这一版**有**一条按版本号的迁移分支，见 ``load()`` 里
#: 对 ``widget_layer`` 的处理（老文件的 ``desktop`` 是默认值，不是用户手选）。
#: v19（2026-11-20 深夜）把挂件从「按设备数自动排行」改成**固定宫格**：
#: ``widget_columns`` + ``widget_devices`` → ``widget_layout``（行×列）+
#: ``widget_slots``（按下标存，空串 = 空槽）。老文件在 ``load()`` 里按行优先
#: 灌进新槽位。v20（2026-11-22）新增顶栏三块胶囊的显示开关 ``header_show``
#: （``HEADER_ITEMS``），默认三块全开 —— 老文件缺这个键时就是默认值，不用迁移。
PREFS_VERSION = 20


def describe_oserror(exc: OSError, target: Path | None = None) -> str:
    """把一次 ``OSError`` 压成一行能贴进弹窗、也能写进日志的话。

    只写 ``str(exc)`` 往往只有"拒绝访问"四个字，分不清是权限还是被安全软件拦；
    把 ``WinError`` / ``errno`` 一起写出来，才能一眼对上"错误 5 = 拒绝访问"
    "错误 32 = 文件被占用""错误 112 = 磁盘满"。
    """
    bits = [f"{type(exc).__name__}: {exc.strerror or exc}"]
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        bits.append(f"WinError {winerror}")
    if exc.errno is not None:
        bits.append(f"errno {exc.errno}")
    if target is not None:
        bits.append(f"目标 {target}")
    return "，".join(bits)

# 家庭切换栏的两种排列方式：
#   fixed        —— 位置只由用户设定的顺序决定，激活与否不动位置（靠样式区分）
#   active_first —— 激活的家庭自动提到最左，其余保持设定顺序
HOME_PLACEMENTS = ("fixed", "active_first")
HOME_PLACEMENT_DEFAULT = "fixed"

# 激活态的表达方式（见 ui/home_tabs.py 的绘制分支）
HOME_STYLES = ("size", "weight", "pill")
HOME_STYLE_DEFAULT = "size"

# 摄像头取流方式：
#   lan_first    —— 先走局域网直连（更快、更省摄像头带宽），明确失败或长时间无帧
#                   时自动切远程中继，只切一次
#   remote_first —— 先走远程中继；远程建不起来时退回局域网一次（异地/公司网络常用，
#                   但远程比局域网慢 2–4 秒、且占一个观看位）
#   remote_only  —— 始终走远程中继，绝不碰局域网（只想走中继时用）
CAMERA_NETWORKS = ("lan_first", "remote_first", "remote_only")
CAMERA_NETWORK_DEFAULT = "lan_first"

# 顶栏（家庭标签右边那排胶囊）显示哪几块。三块各自独立：
#   online      —— 当前家庭的「N 台设备 · M 台开启 · …」
#   temperature —— 全屋温度范围
#   humidity    —— 全屋湿度范围
# 温湿度两颗共用顶栏余量（窄窗口会**临时**逐颗收起，见
# ``ui/main_window.MainWindow._fit_climate``），所以"关掉"和"被窄窗口收起"是两件事：
# 前者是用户偏好，后者只是让位，拉宽就回来。
HEADER_ITEMS = ("online", "temperature", "humidity")
HEADER_ITEM_DEFAULT = True

# 耗材页的卡片排序：
#   level  —— 余量低的排前面（默认：这页就是来看"哪个快用完了"）
#   room   —— 按房间顺序分组显示（默认模式不分房间）
#   custom —— 用户拖拽出来的顺序（``Prefs.consumables`` 的列表顺序）
CONSUMABLE_SORTS = ("level", "room", "custom")
CONSUMABLE_SORT_DEFAULT = "level"

# 耗材卡的画法**整页一个**（``consumable_style``，取值见 ``consumables.STYLES``）：
# 横条 / 整卡染色表达的是"这页怎么读"，不是某条监控项的属性。原先存在每条监控项
# 里，等于让用户为十张卡做十次同样的选择（2026-11-18 用户定：右上角下拉统一设）。

# 监控页的布局预设。几何不在这里：``ui/monitor_page.py`` 的 ``LAYOUT_SPECS`` 才是
# 唯一出处（这里只做"存的是不是合法 id"的词法校验，core 不认识行列）。
#   1x1 单窗口 / 1x2 左右两分 / 2x2 四宫格 / 1+2 一大两小 / 1+5 一大五小
MONITOR_LAYOUTS = ("1x1", "1x2", "2x2", "1+2", "1+5")
MONITOR_LAYOUT_DEFAULT = "1x1"
#: 监控页最多几格（= 1+5 的 6 格），与 ``PlaybackOwnership.MONITOR_MAX`` 同一上限
MONITOR_CELLS_MAX = 6

# 桌面挂件的层级（见 ui/desktop_zorder.py）：
#   desktop —— 挂进 SHELLDLL_DefView（桌面图标层），只在桌面露出时可见、永不挡窗口
#   normal  —— 普通窗口，谁先谁后按系统默认
#   top     —— 置顶，始终压在普通窗口之上
#: 三值字符串而不是两个布尔：档位互斥，"置顶开关 + 贴桌面开关"能组合出四种状态，
#: 其中两种是矛盾的。默认「贴桌面」：挂件本来就该长在桌面上，而不是挡在窗口前。
WIDGET_LAYERS = ("desktop", "normal", "top")
WIDGET_LAYER_DEFAULT = "normal"
#: 挂件宫格布局（行×列）。挂件上每个槽位固定占一格、空槽就是空的——所以行列由
#: 用户显式选，不再按设备数自动排，换布局时也不能把设备挤到前面去
#: （``set_widget_layout`` 按行优先保留，超出新格数的槽位丢弃）。
#: 宫格行列的取值范围：管理窗用两个下拉框各选 1–6，拼成 ``"行x列"``。
#: 上界取 6 是"一屏还能看清"的经验值：6 列 = 6×158+5×6+20 ≈ 998 px，1080p 放得下；
#: 行数超过可视高度的部分由挂件自己滚轮滚动（``_max_rows`` / ``_max_scroll``）。
WIDGET_ROWS_MAX = 6
WIDGET_COLS_MAX = 6
WIDGET_LAYOUTS = tuple(
    f"{rows}x{cols}"
    for rows in range(1, WIDGET_ROWS_MAX + 1)
    for cols in range(1, WIDGET_COLS_MAX + 1)
)
WIDGET_LAYOUT_DEFAULT = "1x3"
#: 槽位数上限：6×6 = 36 格
WIDGET_CELLS_MAX = WIDGET_ROWS_MAX * WIDGET_COLS_MAX


def widget_layout_size(layout: str) -> tuple[int, int]:
    """``"2x3"`` → ``(2, 3)``；非法值一律回退默认布局。"""
    if layout not in WIDGET_LAYOUTS:
        layout = WIDGET_LAYOUT_DEFAULT
    rows, cols = layout.split("x")
    return int(rows), int(cols)


#: 槽位里"快捷场景"的前缀。设备槽位直接存 did（米家 did 是纯数字串），场景槽位
#: 存 ``scene:<home_id>:<scene_id>``——``scene_id`` 只在家庭内唯一，所以 ``home_id``
#: 必须随行。用前缀而不是另开一张表，是因为槽位表就是"哪一格放什么"的唯一出处，
#: 拆成两张表迟早对不上（删场景 / 换布局 / 截断三处都要同步维护）。
WIDGET_SCENE_PREFIX = "scene:"


def widget_slot_is_scene(slot: str) -> bool:
    return str(slot).startswith(WIDGET_SCENE_PREFIX)


def widget_scene_slot(home_id: str, scene_id: str) -> str:
    """场景槽位的规范写法。"""
    return f"{WIDGET_SCENE_PREFIX}{home_id}:{scene_id}"


def parse_widget_scene_slot(slot: str) -> tuple[str, str] | None:
    """``scene:<home>:<scene_id>`` → ``(home, scene_id)``；不是场景槽位给 ``None``。"""
    raw = str(slot)
    if not raw.startswith(WIDGET_SCENE_PREFIX):
        return None
    home, sep, scene_id = raw[len(WIDGET_SCENE_PREFIX):].partition(":")
    if not sep or not home or not scene_id:
        return None
    return home, scene_id


def _clean_slots(slots: Iterable[Any], cells: int) -> list[str]:
    """规范化槽位表：只留字符串、同一台设备只留第一次出现、按格数补齐 / 截断。"""
    out: list[str] = [""] * cells
    seen: set[str] = set()
    for index, raw in enumerate(slots):
        if index >= cells:
            break
        did = raw if isinstance(raw, str) else ""
        did = did.strip()
        if not did or did in seen:
            continue
        seen.add(did)
        out[index] = did
    return out
#: 挂件自定义底板色。只认 #rrggbb；其它一律跟随主题
_WIDGET_BG_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

#: 侧栏上次停留的页面：卡片墙 + 功能区三个入口。只是字符串白名单，页面实例与
#: 索引映射在 ``ui/main_window.py``（core 不反向依赖 ui）。
PAGES = ("wall", "automation", "consumables", "monitor")
PAGE_DEFAULT = "wall"

#: 启动时是否回到上次退出页面。默认**关**：面板的主视图是卡片墙，功能区里的
#: 「耗材」「监控」当前还是占位页；开机就落在不常用的页面上比"每次都从墙开始"
#: 更意外。关掉只影响启动落点，``last_page`` 照常记录（见 ``set_last_page``）。
RESTORE_PAGE_DEFAULT = False

# 摄像头声音**不进偏好文件**（2026-11-10 用户实测后定）：每次开一路新的播放都从
# 静音开始，只在这一次观看里由用户决定要不要出声。理由与实现见
# ``core/camera_view.py`` 顶部"静音不跨会话保留"。

#: 点关闭按钮时收进托盘（而不是退出）。默认开：不这样的话"看一眼设备"每次都要
#: 重新启动、重新读凭据、重新拉设备列表。托盘不可用的环境会自动退回退出。
CLOSE_TO_TRAY_DEFAULT = True

#: 开机自启时**不进桌面、直接进托盘**。默认开：这一项本来就是给"登录后静默常驻"
#: 用的，关掉它自启就只能最小化到任务栏，登录后照样占一格任务栏。托盘不可用
#: （远程会话、精简系统）时自动退回"最小化到任务栏"，见 ``app.present_window``。
START_IN_TRAY_DEFAULT = True

#: 卡片样式的默认 id（= 1×1 标准小卡）。样式自带宽度，所以这里不再有
#: 「大小」这个独立偏好。id 只做词法校验；哪些 id 真的存在由
#: ``ui/card_templates.py`` 的样式注册表决定，免得 core 反向依赖 ui。
CARD_STYLE_DEFAULT = "standard"

_SEP = "\x1f"  # 用作 (home_id, room_id) 组合键的分隔符


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item)
        if text and text not in out:
            out.append(text)
    return out


def _order_map(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, items in value.items():
        texts = _str_list(items)
        if texts:
            out[str(key)] = texts
    return out


def _card_style_map(value: Any) -> dict[str, str]:
    """校验 ``card_styles``：``did → 样式 id``，坏数据整条丢掉而不是让界面崩。

    存的是**稀疏**字典：只有显式改过样式的设备才留一条，选回 ``standard``
    就删记录。这样以后调默认外观，没手动设过的卡片会自动跟上。
    """
    if not isinstance(value, dict):
        return {}
    sid = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
    out: dict[str, str] = {}
    for did, candidate in value.items():
        key = str(did)
        if not key:
            continue
        if isinstance(candidate, str) and sid.match(candidate) \
                and candidate != CARD_STYLE_DEFAULT:
            out[key] = candidate
    return out


def apply_order(order: Sequence[str], items: Iterable[str]) -> list[str]:
    """按 ``order`` 重排 ``items``；不在 order 里的追加在后面，保持原相对顺序。"""
    rank = {key: i for i, key in enumerate(order)}
    indexed = list(enumerate(items))
    indexed.sort(key=lambda pair: (rank.get(pair[1], len(order)), pair[0]))
    return [key for _, key in indexed]


class Prefs:
    """一份可读可写的偏好集合。所有方法都在主线程调用。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else paths.data_dir() / "prefs.json"
        #: 数据目录能不能写。False 时任何改动都不会落盘，调用方（主窗口）要当面
        #: 告诉用户，不能让人以为"改完就好了"
        self.writable = True
        self.home_order: list[str] = []
        self.room_order: dict[str, list[str]] = {}
        self.device_order: dict[str, list[str]] = {}
        self.scene_order: dict[str, list[str]] = {}
        self.hidden: list[str] = []
        self.hidden_scenes: list[str] = []
        self.home_placement = HOME_PLACEMENT_DEFAULT
        self.home_active_style = HOME_STYLE_DEFAULT
        #: 摄像头取流方式，见 ``CAMERA_NETWORKS``
        self.camera_network = CAMERA_NETWORK_DEFAULT
        #: 顶栏三块胶囊各自显不显示，见 ``HEADER_ITEMS``。每次读都走
        #: ``shows_header``，别直接下标取——老文件 / 手工改坏时不该出 KeyError
        self.header_show: dict[str, bool] = dict.fromkeys(HEADER_ITEMS, HEADER_ITEM_DEFAULT)
        #: 顶栏指令条上次选中的音箱 did。只记 did：名字会变、型号会换，
        #: 下次启动只要这台还在"支持执行文本指令"的列表里就沿用（见 ``ui/speaker_bar.py``）
        self.speaker_command_did = ""
        #: 顶栏指令条「静默执行」开关。默认**关**：米家实测开着问「现在几点」
        #: 音箱一声不出，用户会以为指令没发出去（2026-11-11 定案）
        self.speaker_silent = False
        #: 点关闭按钮时收进托盘，见 ``CLOSE_TO_TRAY_DEFAULT``
        self.close_to_tray = CLOSE_TO_TRAY_DEFAULT
        #: 开机自启时直接进托盘、不显示窗口，见 ``START_IN_TRAY_DEFAULT``
        self.start_in_tray = START_IN_TRAY_DEFAULT
        #: 侧栏上次停留的页面（``PAGES`` 之一）。**无论恢复开关是否打开都记录**：
        #: 关掉开关只表示"启动不恢复"，不是"不要记录"。
        self.last_page = PAGE_DEFAULT
        #: 上次停留的家庭 + 房间（都是云端 id，``"*"`` 表示"全部设备"）。和
        #: ``last_page`` 一起记录；停留在功能区页面时不会被清掉，回来仍能落到原房间。
        #: 房间可能因删除 / 换家庭而失效，读取方必须按当前拓扑兜底（见主窗口）。
        self.last_home = ""
        self.last_room = ""
        #: 启动时是否回到 ``last_page`` / ``last_home`` / ``last_room``，见
        #: ``RESTORE_PAGE_DEFAULT``
        self.restore_last_page = RESTORE_PAGE_DEFAULT
        #: did → 样式 id，只存显式改过的设备（默认 standard 不落盘）
        self.card_styles: dict[str, str] = {}
        #: 耗材监控项。**列表顺序 = custom 排序下的显示顺序**，所以拖动只改这一份
        self.consumables: list[Consumable] = []
        #: 耗材页排序方式，见 ``CONSUMABLE_SORTS``
        self.consumable_sort = CONSUMABLE_SORT_DEFAULT
        #: 耗材卡画法（整页统一），见 ``CONSUMABLE_STYLES``
        self.consumable_style = CONSUMABLE_STYLE_DEFAULT
        #: 监控页布局预设，见 ``MONITOR_LAYOUTS``
        self.monitor_layout = MONITOR_LAYOUT_DEFAULT
        #: 监控页每格放了哪台设备（空串 = 空格）。**按下标对应格子**，
        #: 所以切布局时前面几格的位置不会漂；长度不必等于格数（多出来的忽略）
        self.monitor_cells: list[str] = []
        #: 首页末尾「已隐藏设备」分组是否收起（只留标题行）
        self.hidden_collapsed = False
        #: 启动时是否自动打开桌面挂件（设置 → 启动设置）。它只管"下次启动"，
        #: 左栏「桌面挂件」那个开关只管当次显隐，两者互不写对方。
        self.widget_autostart = False
        #: 挂件宫格布局，见 ``WIDGET_LAYOUTS``（行×列）
        self.widget_layout = WIDGET_LAYOUT_DEFAULT
        #: 挂件的槽位表：**按下标对应宫格格子**，空串 = 这格空着。长度固定为
        #: 行×列（多出来的截掉、不够的补空），所以换布局时前面的槽位不会漂
        self.widget_slots: list[str] = [""] * (
            widget_layout_size(WIDGET_LAYOUT_DEFAULT)[0]
            * widget_layout_size(WIDGET_LAYOUT_DEFAULT)[1]
        )
        #: 挂件窗口位置与尺寸 ``[x, y, w, h]``。空列表 = 还没摆过，走默认位置
        self.widget_geom: list[int] = []
        #: 挂件是否收起成一条状态条（P1 才画，先留字段）
        self.widget_collapsed = False
        #: 挂件层级，见 ``WIDGET_LAYERS``
        self.widget_layer = WIDGET_LAYER_DEFAULT
        #: 挂件锁定后不能拖动 / 改列数（P1 才实现）
        self.widget_locked = False
        #: 挂件自定义底板色（``#rrggbb``）；空串 = 跟随主题
        self.widget_bg = ""
        self.load()
        #: 上次探测失败的原因（一句话）。只写"写不进去"等于让用户去猜是权限、
        #: 杀软还是磁盘满——2026-11-21 就卡在这一句上，查了一整轮。
        self.probe_error = ""
        self.writable = self.probe_writable()

    def probe_writable(self) -> bool:
        """在数据目录里真写一个小文件再删掉。写不了就别再假装能存。

        2026-11-21 的现场：普通权限启动的 exe 改完设置、重启就还原，挂件里加的
        设备也记不住——因为 ``prefs.json`` 根本写不进去，而 ``save()`` 把异常吞了。
        一次探测（一个几字节的文件）就能把这件事变成**启动时看得见的一句话**。

        失败时把 ``type/strerror/WinError/errno`` 都抄进 ``probe_error``：这三样
        凑一起才能区分"拒绝访问（13/5）"、""磁盘满（28/112）""、""路径不存在（2/3）"。
        """
        probe = self._path.with_name(self._path.name + ".probe")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            self.probe_error = describe_oserror(exc, probe)
            applog.log("prefs", f"数据目录不可写（{self._path.parent}）：{self.probe_error}")
            return False
        self.probe_error = ""
        applog.log("prefs", f"数据目录可写：{self._path.parent}")
        return True

    # -- 落盘 ------------------------------------------------------------
    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        """读盘。失败一律记一笔 —— 静默退回默认值正是"设置还原"的根源。"""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            applog.log("prefs", f"没有 {self._path}，按默认值起")
            return
        except OSError as exc:
            applog.log("prefs", f"读不出来 {self._path}: {exc!r} —— 本次按默认值起")
            return
        try:
            data = json.loads(raw)
        except ValueError as exc:
            # 坏文件**改名留档**再走默认值：直接覆盖会让"到底存了什么"永远查不清
            broken = self._path.with_name(
                f"{self._path.name}.broken-{time.strftime('%Y%m%d-%H%M%S')}"
            )
            try:
                self._path.replace(broken)
                moved = f"已改名留档 {broken}"
            except OSError as move_exc:
                moved = f"改名也失败：{move_exc!r}"
            applog.log("prefs", f"{self._path} 不是合法 JSON（{exc}）；{moved}")
            return
        if not isinstance(data, dict):
            applog.log("prefs", f"{self._path} 顶层不是对象，按默认值起")
            return
        slots = data.get("widget_slots")
        applog.log(
            "prefs",
            f"载入 {self._path}（{len(raw)} 字节，version={data.get('version')}）"
            f" close_to_tray={data.get('close_to_tray')}"
            f" widget_slots={len(slots) if isinstance(slots, list) else '?'} 格",
        )
        # 排序键一律是云端 id（本机文件已规范到当前结构，不再按版本分支读）。
        # 按 did 存的数据（hidden / card_styles / speaker_command_did）照常保留。
        self.home_order = _str_list(data.get("home_order"))
        self.room_order = _order_map(data.get("room_order"))
        self.device_order = _order_map(data.get("device_order"))
        self.scene_order = _order_map(data.get("scene_order"))
        self.hidden = _str_list(data.get("hidden"))
        # 顺序表**故意不清隐藏项**（2026-11-24 口径）：隐藏只是不在卡片墙上
        # 显示，设备还在、房间还在，用户排过的位置也该留着。读入时清掉的话，
        # 一重启就丢，恢复显示只能落到末尾。``apply_order`` 只排传入的 items，
        # 表里多出来的 did 被忽略，带着无害。
        self.hidden_scenes = _str_list(data.get("hidden_scenes"))
        self.card_styles = _card_style_map(data.get("card_styles"))
        # 耗材监控项：坏条目直接丢，重复 id 只留第一条（同一个属性两张卡会抢值）
        seen: set[str] = set()
        self.consumables = []
        for raw in data.get("consumables") or []:
            item = parse_consumable(raw)
            if item is None or item.id in seen:
                continue
            seen.add(item.id)
            self.consumables.append(item)
        sort_mode = str(data.get("consumable_sort") or "")
        if sort_mode in CONSUMABLE_SORTS:
            self.consumable_sort = sort_mode
        style = str(data.get("consumable_style") or "")
        if style in CONSUMABLE_STYLES:
            self.consumable_style = style
        # 监控页：布局只认白名单；格子是 did 字符串，长度按上限裁掉，
        # 越界的旧数据（比如手工改过文件）不该让页面多出几格没处放
        monitor = str(data.get("monitor_layout") or "")
        if monitor in MONITOR_LAYOUTS:
            self.monitor_layout = monitor
        cells = data.get("monitor_cells")
        if isinstance(cells, list):
            self.monitor_cells = [
                c if isinstance(c, str) else "" for c in cells[:MONITOR_CELLS_MAX]
            ]
        # 只认真正的 true：老文件里没有这个键，坏数据（"yes" / 1）也不该让分组
        # 一开就收起——那是用户看不到自己的设备，比"没记住"糟得多。
        self.hidden_collapsed = data.get("hidden_collapsed") is True
        # 桌面挂件：开关只认真正的 true；设备表去重去空、按上限裁掉；列数越界
        # 退回默认而不是钳到边界（用户手改文件改成 99，更可能是想要默认布局）；
        # 几何必须是 4 个整数且宽高为正，否则当作"没摆过"——半截坐标会让窗口
        # 恢复到一个 0×0 的鬼地方。层级 / 底色都只认白名单
        self.widget_autostart = data.get("widget_autostart") is True
        layout = data.get("widget_layout")
        if isinstance(layout, str) and layout in WIDGET_LAYOUTS:
            self.widget_layout = layout
        else:
            # v18 及更早只存列数（列数之外的行数按设备数自动算）。迁移时按旧
            # 数据推出一个等价布局：设备数 / 列数向上取整就是行数，至少 1 行。
            old_cols = data.get("widget_columns")
            if (
                isinstance(old_cols, int)
                and not isinstance(old_cols, bool)
                and 1 <= old_cols <= 4
            ):
                old_devices = len(_str_list(data.get("widget_devices")))
                rows = max(1, math.ceil(old_devices / old_cols)) if old_devices else 1
                candidate = f"{min(rows, 3)}x{old_cols}"
                if candidate in WIDGET_LAYOUTS:
                    self.widget_layout = candidate
        rows, cols = widget_layout_size(self.widget_layout)
        cells = rows * cols
        raw_slots = data.get("widget_slots")
        if isinstance(raw_slots, list):
            slots = [s if isinstance(s, str) else "" for s in raw_slots[:cells]]
        else:
            # v18 及更早：设备是一张按顺序排的表，按行优先灌进槽位
            slots = _str_list(data.get("widget_devices"))[:cells]
        self.widget_slots = _clean_slots(slots, cells)
        geom = data.get("widget_geom")
        if (
            isinstance(geom, list)
            and len(geom) == 4
            and all(isinstance(v, int) and not isinstance(v, bool) for v in geom)
            and geom[2] > 0
            and geom[3] > 0
        ):
            self.widget_geom = list(geom)
        self.widget_collapsed = data.get("widget_collapsed") is True
        self.widget_locked = data.get("widget_locked") is True
        layer = str(data.get("widget_layer") or "")
        if layer in WIDGET_LAYERS:
            self.widget_layer = layer
        # v18 起默认档从「贴桌面」改成「普通」：老文件里的 "desktop" 多半不是用户
        # 手动选的（当时这是默认值），迁移成普通档——否则升级后点开关看不到挂件
        version = data.get("version")
        if (
            isinstance(version, int) and not isinstance(version, bool) and version < 18
            and self.widget_layer == "desktop"
        ):
            self.widget_layer = WIDGET_LAYER_DEFAULT
        bg = str(data.get("widget_bg") or "")
        if _WIDGET_BG_RE.match(bg):
            self.widget_bg = bg.lower()
        network = str(data.get("camera_network") or "")
        if network in CAMERA_NETWORKS:
            self.camera_network = network
        self.speaker_command_did = str(data.get("speaker_command_did") or "")
        # 只认真正的 bool：坏数据时"出声回答"是更安全的方向——把静默读成 True 会
        # 让用户以为指令根本没发出去（音箱一声不出），比"多响一次"难查得多
        speaker_silent = data.get("speaker_silent")
        if isinstance(speaker_silent, bool):
            self.speaker_silent = speaker_silent
        # 摄像头声音不落盘（v8 起取消；老文件里那个键读到时直接忽略）。每次开播
        # 从静音开始，见 ``core/camera_view.py``。
        # 同上：只认真正的 bool。坏数据时"关窗就退出"是更安全的那个方向——
        # 用户以为关掉了，程序却还在后台跑，是比"多启动一次"严重得多的问题
        close_to_tray = data.get("close_to_tray")
        if isinstance(close_to_tray, bool):
            self.close_to_tray = close_to_tray
        # 同上，只认真正的 bool：坏数据时"进托盘"也是更轻的方向——大不了窗口没
        # 按预期出现，点一下图标就回来了；反过来把用户的偏好读成 False 更莫名
        start_in_tray = data.get("start_in_tray")
        if isinstance(start_in_tray, bool):
            self.start_in_tray = start_in_tray
        # 页面记录只认白名单；坏值 / 老文件缺键都退回卡片墙，不猜"是不是功能区某页"
        page = str(data.get("last_page") or "")
        if page in PAGES:
            self.last_page = page
        # 家庭 / 房间都是云端 id，core 不认识具体列表，只做字符串兜底；
        # 校验 id 是否还存在是主窗口按当前拓扑做的事
        self.last_home = str(data.get("last_home") or "")
        self.last_room = str(data.get("last_room") or "")
        # 只认真正的 bool：坏数据时"不恢复"是更安全的方向——至少每次都从卡片墙
        # 开始，不会莫名其妙停在一个用户没预期的页面
        restore = data.get("restore_last_page")
        if isinstance(restore, bool):
            self.restore_last_page = restore
        layout = data.get("layout")
        if isinstance(layout, dict):
            placement = str(layout.get("home_placement") or "")
            style = str(layout.get("home_active_style") or "")
            if placement in HOME_PLACEMENTS:
                self.home_placement = placement
            if style in HOME_STYLES:
                self.home_active_style = style
        # 顶栏三块开关：只认真正的 bool，未知键丢弃、缺键保默认（全开）
        header = data.get("header_show")
        if isinstance(header, dict):
            for key in HEADER_ITEMS:
                value = header.get(key)
                if isinstance(value, bool):
                    self.header_show[key] = value

    def save(self) -> None:
        payload = {
            "version": PREFS_VERSION,
            "card_styles": self.card_styles,
            "consumables": [c.to_dict() for c in self.consumables],
            "consumable_sort": self.consumable_sort,
            "consumable_style": self.consumable_style,
            "monitor_layout": self.monitor_layout,
            "monitor_cells": self.monitor_cells,
            "widget_autostart": self.widget_autostart,
            "widget_layout": self.widget_layout,
            "widget_slots": self.widget_slots,
            "widget_geom": self.widget_geom,
            "widget_collapsed": self.widget_collapsed,
            "widget_layer": self.widget_layer,
            "widget_locked": self.widget_locked,
            "widget_bg": self.widget_bg,
            "home_order": self.home_order,
            "room_order": self.room_order,
            "device_order": self.device_order,
            "scene_order": self.scene_order,
            "hidden": self.hidden,
            "hidden_scenes": self.hidden_scenes,
            "hidden_collapsed": self.hidden_collapsed,
            "camera_network": self.camera_network,
            "header_show": dict(self.header_show),
            "speaker_command_did": self.speaker_command_did,
            "speaker_silent": self.speaker_silent,
            "close_to_tray": self.close_to_tray,
            "start_in_tray": self.start_in_tray,
            "last_page": self.last_page,
            "last_home": self.last_home,
            "last_room": self.last_room,
            "restore_last_page": self.restore_last_page,
            "layout": {
                "home_placement": self.home_placement,
                "home_active_style": self.home_active_style,
            },
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            # 原子替换被拒是**真的会发生**的：杀软 / 搜索索引器 / 同步盘短暂占着
            # prefs.json 时 ``os.replace`` 会以 WinError 5 / 32 失败。以前的
            # ``pass`` 让"设置改完就还原"变成了一个查不出来的谜；现在先记一笔，
            # 再退回"直接覆盖写"——丢掉原子性，但保住用户刚改的那一下。
            applog.log("prefs", f"原子写入失败（{exc!r}），改试直接覆盖 {self._path}")
            try:
                self._path.write_text(text, encoding="utf-8")
            except OSError as exc2:
                applog.log(
                    "prefs",
                    f"直接覆盖也失败（{exc2!r}）—— 本次改动不会落盘：{self._path}",
                )
                return
            applog.log("prefs", f"兜底直写成功 {self._path}（{len(text)} 字节）")
            return
        applog.log("prefs", f"已保存 {self._path}（{len(text)} 字节）")

    # -- 排序：读 --------------------------------------------------------
    def order_homes(self, home_ids: Sequence[str]) -> list[str]:
        return apply_order(self.home_order, home_ids)

    def order_rooms(self, home_id: str, room_ids: Sequence[str]) -> list[str]:
        return apply_order(self.room_order.get(home_id, ()), room_ids)

    def order_devices(
        self, home_id: str, room_id: str, dids: Sequence[str],
    ) -> list[str]:
        key = self.room_key(home_id, room_id)
        return apply_order(self.device_order.get(key, ()), dids)

    def order_scenes(self, home_id: str, scene_ids: Sequence[str]) -> list[str]:
        return apply_order(self.scene_order.get(home_id, ()), scene_ids)

    def order_hidden(self, dids: Sequence[str]) -> list[str]:
        return apply_order(self.hidden, dids)

    # -- 排序：写 --------------------------------------------------------
    def set_home_order(self, home_ids: Sequence[str]) -> None:
        self.home_order = list(home_ids)
        self.save()

    def set_room_order(self, home_id: str, room_ids: Sequence[str]) -> None:
        self.room_order[home_id] = list(room_ids)
        self.save()

    def set_device_order(
        self, home_id: str, room_id: str, dids: Sequence[str],
    ) -> None:
        key = self.room_key(home_id, room_id)
        self.device_order[key] = self._merge_device_order(key, dids)
        self.save()

    def set_scene_order(self, home_id: str, scene_ids: Sequence[str]) -> None:
        self.scene_order[home_id] = list(scene_ids)
        self.save()

    def set_orders(
        self,
        *,
        home_order: Sequence[str] | None = None,
        room_orders: dict[str, Sequence[str]] | None = None,
        device_orders: dict[tuple[str, str], Sequence[str]] | None = None,
        scene_orders: dict[str, Sequence[str]] | None = None,
    ) -> None:
        """一次写入多份顺序，只落一次盘。

        设置页的树是"拖一下重排整棵树"，如果逐层调用 ``set_*_order``，一次拖动会写
        十几遍 JSON、还会触发十几遍界面重建。这里合并成一次。
        """
        if home_order is not None:
            self.home_order = list(home_order)
        if room_orders:
            for home_id, room_ids in room_orders.items():
                self.room_order[home_id] = list(room_ids)
        if device_orders:
            for (home_id, room_id), dids in device_orders.items():
                key = self.room_key(home_id, room_id)
                self.device_order[key] = self._merge_device_order(key, dids)
        if scene_orders:
            for home_id, ids in scene_orders.items():
                self.scene_order[home_id] = list(ids)
        self.save()

    def reset_rooms(self, home_id: str) -> None:
        self.room_order.pop(home_id, None)
        self.save()

    def reset_devices(self, home_id: str, room_id: str) -> None:
        self.device_order.pop(self.room_key(home_id, room_id), None)
        self.save()

    # -- 设备配置重置 ----------------------------------------------------
    def reset_device(self, did: str) -> dict[str, int]:
        """清掉一台设备的本地配置：卡片样式、隐藏、房间内排序。

        能力缓存不在 ``Prefs`` 里（由 ``CapabilityBook`` 管），调用方自己去清。
        返回清掉的条数，给确认窗和提示用。
        """
        did = str(did)
        style = 1 if self.card_styles.pop(did, None) is not None else 0
        hidden = 0
        if did in self.hidden:
            self.hidden.remove(did)
            hidden = 1
        order = self._forget_device_order(did)
        if style or hidden or order:
            self.save()
        return {"style": style, "hidden": hidden, "order": order}

    def device_order_hits(self, did: str) -> int:
        """只统计、不改动：这台设备在几张房间顺序表里。"""
        return sum(1 for dids in self.device_order.values() if did in dids)

    def reset_all_devices(self) -> dict[str, int]:
        """清掉所有设备的本地配置，口径与 :meth:`reset_device` 相同。"""
        affected: set[str] = set(self.card_styles) | set(self.hidden)
        for dids in self.device_order.values():
            affected.update(dids)
        counts = {
            "devices": len(affected),
            "style": len(self.card_styles),
            "hidden": len(self.hidden),
            "order": sum(len(dids) for dids in self.device_order.values()),
        }
        if counts["style"] or counts["hidden"] or counts["order"]:
            self.card_styles.clear()
            self.hidden = []
            self.device_order.clear()
            self.save()
        return counts

    # -- 家庭标签的排列 / 激活样式 ---------------------------------------
    def set_home_placement(self, value: str) -> bool:
        if value not in HOME_PLACEMENTS or value == self.home_placement:
            return False
        self.home_placement = value
        self.save()
        return True

    def set_home_active_style(self, value: str) -> bool:
        if value not in HOME_STYLES or value == self.home_active_style:
            return False
        self.home_active_style = value
        self.save()
        return True

    # -- 摄像头取流方式 --------------------------------------------------
    def set_camera_network(self, value: str) -> bool:
        if value not in CAMERA_NETWORKS or value == self.camera_network:
            return False
        self.camera_network = value
        self.save()
        return True

    # -- 顶栏三块胶囊的显隐 ----------------------------------------------
    def shows_header(self, key: str) -> bool:
        """这一块要不要显示。未知键当"显示"——宁多画一块，也别因为读了个新键
        就整块消失（早期版本写的 prefs.json 里没有这个键）。"""
        if key not in HEADER_ITEMS:
            return True
        return bool(self.header_show.get(key, HEADER_ITEM_DEFAULT))

    def set_header_show(self, key: str, on: bool) -> bool:
        """改一块的显隐。返回是否真的变了（没变就不写盘）。"""
        if key not in HEADER_ITEMS:
            return False
        on = bool(on)
        if self.shows_header(key) == on:
            return False
        self.header_show[key] = on
        self.save()
        return True

    # -- 顶栏指令条的目标音箱 --------------------------------------------
    def set_speaker_command_did(self, did: str) -> bool:
        if did == self.speaker_command_did:
            return False
        self.speaker_command_did = did
        self.save()
        return True

    def set_speaker_silent(self, on: bool) -> bool:
        """静默执行开关。返回是否真的变了（没变就不落盘）。"""
        on = bool(on)
        if on == self.speaker_silent:
            return False
        self.speaker_silent = on
        self.save()
        return True

    # -- 上次停留的页面 --------------------------------------------------
    def set_last_page(self, page: str) -> bool:
        """记录侧栏当前页面。与 ``restore_last_page`` 无关，总是记录。"""
        if page not in PAGES or page == self.last_page:
            return False
        self.last_page = page
        self.save()
        return True

    def set_last_room(self, home: str, room: str) -> bool:
        """记录上次停留的家庭 + 房间（云端 id，``"*"`` = 全部设备）。

        与 ``last_page`` 一样，不管恢复开关开没开都记；开关只决定启动时用不用。
        这里不做存在性校验——房间可能已被删，启动时由主窗口按当前拓扑兜底。
        """
        home = str(home or "")
        room = str(room or "")
        if (home, room) == (self.last_home, self.last_room):
            return False
        self.last_home = home
        self.last_room = room
        self.save()
        return True

    def set_restore_last_page(self, on: bool) -> bool:
        """启动是否恢复上次页面。返回是否真的变了。"""
        on = bool(on)
        if on == self.restore_last_page:
            return False
        self.restore_last_page = on
        self.save()
        return True

    # -- 卡片外观：一个样式同时决定宽度与内容 -----------------------------
    def card_style(self, did: str) -> str | None:
        """这台设备**显式**选过的样式 id；没设过返回 ``None``。

        不能在这里兜底成 ``standard``：品类可以有自己声明的默认样式（人在
        传感器默认显示有人/无人），"没选过"和"显式选了标准"必须是两种状态，
        否则品类默认永远轮不到。兜底逻辑在 ``ui/card_templates.default_style``。
        """
        return self.card_styles.get(did)

    def set_card_style(
        self, did: str, style_id: str | None, default_id: str | None = None
    ) -> bool:
        """``None`` 表示回默认；``style_id == default_id`` 也不落盘。

        ``default_id`` 是该品类**当前**的默认样式（由调用方从 ``CardSpec`` 取）。
        在默认=标准的品类上点「标准」就等于回默认、删记录；而在默认是
        「状态」的存在传感器上点「标准」是一次真实的偏好，必须存下来——否则
        用户点了"标准"却又回到"状态"，菜单会像失灵一样。

        返回是否真的产生了变化，让调用方决定要不要局部刷新那张卡。
        """
        did = str(did)
        if not did:
            return False
        if style_id is not None and (
            not isinstance(style_id, str)
            or not re.match(r"^[a-z][a-z0-9_]{0,31}$", style_id)
        ):
            return False
        target = None if style_id in (None, default_id or CARD_STYLE_DEFAULT) else style_id
        if self.card_styles.get(did) == target:
            return False
        if target is None:
            self.card_styles.pop(did, None)
        else:
            self.card_styles[did] = target
        self.save()
        return True

    def clear_card_style(self, did: str) -> bool:
        """和 ``set_card_style(did, None)`` 等价，留给"恢复默认"这类调用方。"""
        return self.set_card_style(did, None)

    # -- 耗材监控：用户手动订阅的"设备 + 属性"清单 -------------------------
    def consumable(self, cid: str) -> Consumable | None:
        """按 id 取一条监控项；没有返回 ``None``。"""
        for item in self.consumables:
            if item.id == cid:
                return item
        return None

    def add_consumable(self, item: Consumable) -> bool:
        """新增一条。**同一属性只允许一条**（重复添加会让两张卡抢同一个值）。"""
        if self.consumable(item.id) is not None:
            return False
        self.consumables.append(item)
        self.save()
        return True

    def update_consumable(self, item: Consumable) -> bool:
        """按 id 原地替换（改标签 / 样式 / 阈值）。没这条返回 ``False``。

        不换位置：id 没变，顺序就是用户排过的顺序，改个阈值不该把卡片挪走。
        """
        for index, old in enumerate(self.consumables):
            if old.id == item.id:
                if old == item:
                    return False
                self.consumables[index] = item
                self.save()
                return True
        return False

    def reconfigure_consumable(self, old_id: str, item: Consumable) -> bool:
        """把一条换成**换了属性**的另一条（空壳 ``did:0.0`` → 选好属性）。

        不能复用 ``update_consumable``：那是按新 id 找旧项，而这里的 id 会变。
        按旧 id 定位、原地替换，**位置不动**——用户排过的顺序不该因为填了个属性
        就跳到末尾。目标 id 已经存在（同一属性已有另一条）时返回 ``False``，
        让界面留在原地提示，而不是悄悄顶掉别人。
        """
        index = next(
            (i for i, c in enumerate(self.consumables) if c.id == old_id), -1
        )
        if index < 0:
            return False
        if item.id != old_id and self.consumable(item.id) is not None:
            return False
        if self.consumables[index] == item:
            return False
        self.consumables[index] = item
        self.save()
        return True

    def remove_consumable(self, cid: str) -> bool:
        keep = [c for c in self.consumables if c.id != cid]
        if len(keep) == len(self.consumables):
            return False
        self.consumables = keep
        self.save()
        return True

    def move_consumable(self, cid: str, index: int) -> bool:
        """拖拽排序：把这条挪到 ``index``（越界自动收敛到两端）。"""
        items = list(self.consumables)
        src = next((i for i, c in enumerate(items) if c.id == cid), -1)
        if src < 0:
            return False
        target = max(0, min(int(index), len(items) - 1))
        if target == src:
            return False
        items.insert(target, items.pop(src))
        self.consumables = items
        self.save()
        return True

    def set_consumable_sort(self, mode: str) -> bool:
        if mode not in CONSUMABLE_SORTS or mode == self.consumable_sort:
            return False
        self.consumable_sort = mode
        self.save()
        return True

    def set_consumable_style(self, style: str) -> bool:
        """换耗材卡的画法（整页统一）。和排序一样：值没变就不写盘。"""
        if style not in CONSUMABLE_STYLES or style == self.consumable_style:
            return False
        self.consumable_style = style
        self.save()
        return True

    # -- 监控页 ----------------------------------------------------------
    def set_monitor_layout(self, layout: str) -> bool:
        """换监控页布局。**不动格子里的设备**：切回原布局时它们还在原位。"""
        if layout not in MONITOR_LAYOUTS or layout == self.monitor_layout:
            return False
        self.monitor_layout = layout
        self.save()
        return True

    def set_monitor_cells(self, cells: list[str]) -> bool:
        """记下每格放了哪台设备。空串表示空格；超出格数上限的截掉。"""
        clean = [c if isinstance(c, str) else "" for c in list(cells)[:MONITOR_CELLS_MAX]]
        while clean and not clean[-1]:
            clean.pop()  # 末尾的空格不落盘，文件里不出现一长串 ""
        if clean == self.monitor_cells:
            return False
        self.monitor_cells = clean
        self.save()
        return True

    # -- 桌面挂件 --------------------------------------------------------
    def set_widget_autostart(self, on: bool) -> bool:
        """挂件总开关。关掉**不清空设备表**：再打开还是原来那几台。"""
        on = bool(on)
        if self.widget_autostart == on:
            return False
        self.widget_autostart = on
        self.save()
        return True

    #: 挂件的行数 / 列数（由 ``widget_layout`` 推）
    @property
    def widget_rows(self) -> int:
        return widget_layout_size(self.widget_layout)[0]

    @property
    def widget_cols(self) -> int:
        return widget_layout_size(self.widget_layout)[1]

    def widget_dids(self) -> list[str]:
        """槽位表里真正放了**设备**的 did（按行优先顺序，跳过空格与场景槽位）。"""
        return [
            d for d in self.widget_slots
            if d and not widget_slot_is_scene(d)
        ]

    def widget_scene_slots(self) -> list[str]:
        """槽位表里的场景槽位（原样返回，解析用 ``parse_widget_scene_slot``）。"""
        return [s for s in self.widget_slots if widget_slot_is_scene(s)]

    def set_widget_layout(self, layout: str) -> bool:
        """换宫格布局。按行优先保留现有设备：新格数少了就截掉尾部，多了补空槽。"""
        if layout not in WIDGET_LAYOUTS or layout == self.widget_layout:
            return False
        rows, cols = widget_layout_size(layout)
        cells = rows * cols
        self.widget_layout = layout
        self.widget_slots = _clean_slots(self.widget_slots[:cells], cells)
        self.save()
        return True

    def set_widget_slots(self, slots: Sequence[str]) -> bool:
        """整表替换（管理窗保存时走这里）。去重 / 补空 / 截断都在 ``_clean_slots``。"""
        cells = self.widget_rows * self.widget_cols
        clean = _clean_slots(slots, cells)
        if clean == self.widget_slots:
            return False
        self.widget_slots = clean
        self.save()
        return True

    def set_widget_slot(self, index: int, did: str) -> bool:
        """把某台设备放进某一格。它原先在别的格子里会被挪走（一台只占一格）。"""
        cells = self.widget_rows * self.widget_cols
        if isinstance(index, bool) or not isinstance(index, int):
            return False
        if not 0 <= index < cells:
            return False
        did = str(did)
        slots = list(self.widget_slots)
        if did:
            if slots[index] == did:
                return False
            slots = ["" if s == did else s for s in slots]
        elif not slots[index]:
            return False
        slots[index] = did
        return self.set_widget_slots(slots)

    def clear_widget_slot(self, index: int) -> bool:
        return self.set_widget_slot(index, "")

    def remove_widget(self, did: str) -> bool:
        """按槽位值清格（卡片右键「从挂件移除」/ 设备或场景被删时用）。

        参数可以是设备 did，也可以是 ``scene:<home>:<scene_id>``——这里只做
        字符串相等比较，不关心它是哪种卡。
        """
        if did not in self.widget_slots:
            return False
        return self.set_widget_slots(["" if s == did else s for s in self.widget_slots])

    def set_widget_geom(self, geom: Sequence[int]) -> bool:
        """记下挂件窗口的 ``[x, y, w, h]``。非法几何直接拒绝，不写半截值。"""
        vals = list(geom)[:4]
        if len(vals) != 4:
            return False
        if any(isinstance(v, bool) or not isinstance(v, int) for v in vals):
            return False
        if vals[2] <= 0 or vals[3] <= 0:
            return False
        if vals == self.widget_geom:
            return False
        self.widget_geom = vals
        self.save()
        return True

    def set_widget_collapsed(self, collapsed: bool) -> bool:
        collapsed = bool(collapsed)
        if self.widget_collapsed == collapsed:
            return False
        self.widget_collapsed = collapsed
        self.save()
        return True

    def set_widget_layer(self, layer: str) -> bool:
        if layer not in WIDGET_LAYERS or layer == self.widget_layer:
            return False
        self.widget_layer = layer
        self.save()
        return True

    def set_widget_locked(self, locked: bool) -> bool:
        locked = bool(locked)
        if self.widget_locked == locked:
            return False
        self.widget_locked = locked
        self.save()
        return True

    def set_widget_bg(self, color: str) -> bool:
        """底板色。空串 = 跟随主题；其它只认 ``#rrggbb``。"""
        color = str(color or "").strip().lower()
        if color and not _WIDGET_BG_RE.match(color):
            return False
        if color == self.widget_bg:
            return False
        self.widget_bg = color
        self.save()
        return True

    # -- 隐藏 ------------------------------------------------------------
    @property
    def hidden_set(self) -> set[str]:
        return set(self.hidden)

    def is_hidden(self, did: str) -> bool:
        return did in self.hidden

    def set_hidden(
        self, did: str, hidden: bool, *, home: str = "", room: str = ""
    ) -> bool:
        """返回是否真的产生了变化，方便界面判断要不要重建。

        隐藏只改 ``hidden`` 这一个列表，**不动顺序表**：设备还在、房间还在、
        用户排过的位置也在，墙上不画而已。写顺序表时由 ``_merge_device_order``
        把隐藏项接在可见项后面，两边不会打架。``home`` / ``room`` 由 ``Store``
        提供（``Prefs`` 不知道设备归属），恢复时要靠它把"换过房间"的设备补进
        新房间的表。
        """
        did = str(did)
        if not did:
            return False
        if hidden:
            if did in self.hidden:
                return False
            self.hidden.append(did)
        else:
            if did not in self.hidden:
                return False
            self.hidden.remove(did)
            if home:
                self._restore_device_order(home, room, did)
        self.save()
        return True

    def clear_hidden(self) -> bool:
        if not self.hidden:
            return False
        self.hidden = []
        self.save()
        return True

    def set_hidden_collapsed(self, collapsed: bool) -> bool:
        """「已隐藏设备」分组收起 / 展开。返回是否真的变了，让界面决定要不要刷新。"""
        collapsed = bool(collapsed)
        if self.hidden_collapsed == collapsed:
            return False
        self.hidden_collapsed = collapsed
        self.save()
        return True

    # -- 隐藏：场景 ------------------------------------------------------
    # 场景和设备的隐藏分两个列表：场景没有房间、也不进"已隐藏设备"分组，
    # 混在一起会让人以为隐藏一个列表就同时管住了两种东西。
    @property
    def hidden_scene_set(self) -> set[str]:
        return set(self.hidden_scenes)

    def is_scene_hidden(self, scene_id: str) -> bool:
        return scene_id in self.hidden_scenes

    def set_scene_hidden(self, scene_id: str, hidden: bool) -> bool:
        scene_id = str(scene_id)
        if not scene_id:
            return False
        if hidden:
            if scene_id in self.hidden_scenes:
                return False
            self.hidden_scenes.append(scene_id)
        else:
            if scene_id not in self.hidden_scenes:
                return False
            self.hidden_scenes.remove(scene_id)
        self.save()
        return True

    def clear_hidden_scenes(self) -> bool:
        if not self.hidden_scenes:
            return False
        self.hidden_scenes = []
        self.save()
        return True

    # -- 顺序表与隐藏项 --------------------------------------------------
    def _merge_device_order(self, key: str, dids: Sequence[str]) -> list[str]:
        """写入口：可见设备按传入顺序在前，表里原有的隐藏设备接在后面。

        **隐藏只是不在卡片墙上显示，不是"这台设备不存在"**（2026-11-24 定的
        口径）。所以隐藏期间用户在墙上拖了别的卡，也不能把藏起来那几台的顺序
        位置抹掉——否则一"恢复显示"，它就莫名其妙跳到末尾，等于偷偷改了用户
        排过的东西。

        精确原位是不可能的（墙上根本没有它这一格，插入点无从得知），统一接在
        可见项后面：顺序本身保住了，位置可预期。

        传入的 ``dids`` 原样保留（调用方可能传全量，比如设置页的树）；只补
        旧表里有、这次没传的隐藏项。
        """
        hidden = self.hidden_set
        wanted = list(dids)
        seen = set(wanted)
        kept = [
            d for d in self.device_order.get(key, ())
            if d in hidden and d not in seen
        ]
        return wanted + kept

    def _append_device_order(self, home_id: str, room_id: str, did: str) -> None:
        """把一台设备追加到房间末尾；先去掉可能残留的同一 did。"""
        if not did:
            return
        key = self.room_key(home_id, room_id)
        order = [d for d in self.device_order.get(key, ()) if d != did]
        order.append(did)
        self.device_order[key] = order

    def _restore_device_order(self, home_id: str, room_id: str, did: str) -> None:
        """恢复显示：顺序表里还留着它就原地不动。

        只有"换过房间"（新房间的表里没有它）或"本来就不在表里"（新设备、顺序
        被重置过）才追加到当前房间末尾；无论哪种，别的房间表里的残留都要清掉，
        否则同一台设备会在两张表里各占一个位置。
        """
        if not did:
            return
        key = self.room_key(home_id, room_id)
        if did in self.device_order.get(key, ()):
            self._forget_device_order(did, keep=key)
            return
        self._forget_device_order(did)
        self._append_device_order(home_id, room_id, did)

    def _forget_device_order(self, did: str, *, keep: str = "") -> int:
        """把这台设备从各房间的顺序表里删掉，返回命中了几张表。

        ``keep`` 指定的那张表跳过——恢复显示时要留住它原来的位置。
        """
        hits = 0
        for key, dids in list(self.device_order.items()):
            if key == keep:
                continue
            if did in dids:
                self.device_order[key] = [d for d in dids if d != did]
                hits += 1
        return hits

    def append_device_orders(self, slots: Iterable[tuple[str, str, str]]) -> None:
        """批量恢复显示（「全部恢复显示」用），只落一次盘。

        注意不是无脑追加：表里还留着位置的设备保持原位，只有真正缺位置的才
        补到房间末尾（见 :meth:`_restore_device_order`）。
        """
        for home, room, did in slots:
            self._restore_device_order(home, room, did)
        self.save()

    # -- 内部 ------------------------------------------------------------
    @staticmethod
    def room_key(home: str, room: str) -> str:
        return f"{home}{_SEP}{room}"

    #: 老名字，内部还在用
    _room_key = room_key


__all__ = [
    "CAMERA_NETWORKS",
    "CAMERA_NETWORK_DEFAULT",
    "CARD_STYLE_DEFAULT",
    "CONSUMABLE_SORTS",
    "CONSUMABLE_SORT_DEFAULT",
    "CONSUMABLE_STYLES",
    "CONSUMABLE_STYLE_DEFAULT",
    "HIDDEN_ROOM",
    "HOME_PLACEMENTS",
    "HOME_PLACEMENT_DEFAULT",
    "HOME_STYLES",
    "HOME_STYLE_DEFAULT",
    "MONITOR_CELLS_MAX",
    "MONITOR_LAYOUTS",
    "MONITOR_LAYOUT_DEFAULT",
    "PAGES",
    "PAGE_DEFAULT",
    "Prefs",
    "RESTORE_PAGE_DEFAULT",
    "apply_order",
]
