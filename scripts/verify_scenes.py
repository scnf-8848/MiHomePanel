# SPDX-License-Identifier: GPL-3.0-or-later
"""场景隐藏 + 圆角右键菜单的离线验证。

三件事：

1. ``Prefs`` 的 ``hidden_scenes`` 能存能读，和设备隐藏互不干扰；
2. ``Store.scenes_for`` 默认滤掉隐藏场景，设置页用的 ``include_hidden=True`` 全留；
3. 右键菜单是**真的**圆角（四角像素透明，边缘有抗锯齿渐变），不是"圆角图案
   底下垫一块矩形"。

用法：``.venv/Scripts/python.exe -X utf8 scripts/verify_scenes.py``
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from mihome.core.prefs import Prefs  # noqa: E402
from mihome.core.store import Store  # noqa: E402
from mihome.ui import theme  # noqa: E402
from mihome.ui.menu import Menu  # noqa: E402
from mihome.ui.wall import WallView  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(f"{name} {detail}")


def make_store() -> Store:
    class _Caps:
        def save(self) -> None:
            pass

    class _Client:
        capabilities = _Caps()

    class _Worker:
        pass

    store = Store(_Client(), _Worker(), None)  # type: ignore[arg-type]
    store.prefs = Prefs(Path(tempfile.gettempdir()) / "mihome_verify_scenes.json")
    store.prefs.hidden_scenes = []
    store.scenes_by_home = {
        "家": [
            {"scene_id": "s1", "name": "回家", "home_id": "h1", "enable": True},
            {"scene_id": "s2", "name": "离家", "home_id": "h1", "enable": True},
            {"scene_id": "s3", "name": "关灯", "home_id": "h1", "enable": True},
        ]
    }
    return store


def checks_refresh(app: QApplication) -> None:
    """``WallView.refresh_scenes``：5 分钟那轮拉到新场景后条要跟着换。

    整墙重建（``set_home``）也"能"刷新场景，但它会把滚动位置、卡片对象、正在播的
    画面全丢——所以刷新必须走这条只换条的路。
    """

    class _Icons:
        def ensure(self, model: str) -> None:
            pass

        def scaled(self, model: str, size: int):  # noqa: ANN201
            return None

    store = make_store()
    store.scenes = store.scenes_for("家")
    wall = WallView(store, _Icons(), theme.LIGHT)
    wall.resize(900, 600)
    wall.set_home("家")
    wall.show()
    app.processEvents()

    bar = wall.scene_bar
    check("建墙时条在", bar is not None and len(bar._pills) == 3)  # noqa: SLF001
    check("条在布局第 0 位", bar is not None and wall._col.indexOf(bar) == 0)  # noqa: SLF001
    names = [p.name for p in bar._pills] if bar is not None else []  # noqa: SLF001
    check("胶囊顺序按排序规则", names == ["回家", "离家", "关灯"], str(names))

    # 手机里改了一个名、加了一个新的：数据换成新的，条只换胶囊
    fresh = [
        {"scene_id": "s1", "name": "回家（改）", "home_id": "h1", "enable": True},
        {"scene_id": "s2", "name": "离家", "home_id": "h1", "enable": True},
        {"scene_id": "s3", "name": "关灯", "home_id": "h1", "enable": True},
        {"scene_id": "s4", "name": "看电影", "home_id": "h1", "enable": True},
    ]
    same_wall = wall
    same_bar = bar
    wall.refresh_scenes(fresh)
    app.processEvents()
    check("同一个墙对象（没重建）", wall is same_wall)
    check("同一个条对象（没重建）", wall.scene_bar is same_bar)
    names = [p.name for p in wall.scene_bar._pills]  # noqa: SLF001
    check("改名跟上了", "回家（改）" in names, str(names))
    check("新增跟上了", names == ["回家（改）", "离家", "关灯", "看电影"], str(names))

    # 2026-11-27 的真事故：条已经可见时新建的胶囊**默认是隐藏的**（Qt 规定
    # setParent() 之后必须 show()），于是只有标题和占位高度、一个按钮都不画；
    # 首次建墙看不出来——那时条还没显示，子控件跟着父一起出现。切一次家庭
    # 会整墙重建，所以"切家庭再切回来就好了"。
    pills = wall.scene_bar._pills  # noqa: SLF001
    check("刷新后每个胶囊都真的在显示（父已可见时新建子控件不会自动 show）",
          bool(pills) and all(p.isVisible() and not p.isHidden() for p in pills),
          str([(p.name, p.isVisible(), p.isHidden()) for p in pills]))
    check("刷新后胶囊有正常尺寸（不是 0×0）",
          all(p.height() > 0 and p.width() >= 100 for p in pills),
          str([(p.width(), p.height()) for p in pills]))

    # 停用的场景不进条：手机里停用等于"不显示"，与 build() 的口径一致
    fresh[1] = dict(fresh[1], enable=False)
    wall.refresh_scenes(fresh)
    app.processEvents()
    names = [p.name for p in wall.scene_bar._pills]  # noqa: SLF001
    check("停用的不画", "离家" not in names, str(names))

    # 全删：条自己隐藏、高度归零，控件留着复用
    wall.refresh_scenes([])
    app.processEvents()
    check("全删后条隐藏", not wall.scene_bar.isVisible())
    check("全删后不占高度", wall.scene_bar.height() == 0, str(wall.scene_bar.height()))
    check("控件还留着（下次直接复用）", wall.scene_bar is same_bar)

    # 又有了：同一条复用，不用重新插布局
    wall.refresh_scenes(fresh[:1])
    app.processEvents()
    check("恢复后条重新可见", wall.scene_bar.isVisible())
    check("还是同一个条", wall.scene_bar is same_bar)
    check("胶囊回来了", len(wall.scene_bar._pills) == 1)  # noqa: SLF001
    check("恢复后胶囊也真的在显示",
          all(p.isVisible() and not p.isHidden() for p in wall.scene_bar._pills))  # noqa: SLF001
    wall.close()

    # 原本一个场景都没有的墙（条根本没建）：新场景到了要插在第 0 位
    store2 = make_store()
    store2.scenes_by_home = {}
    store2.scenes = []
    wall2 = WallView(store2, _Icons(), theme.LIGHT)
    wall2.resize(900, 600)
    wall2.set_home("家")
    wall2.show()
    app.processEvents()
    check("没场景时条不存在", wall2.scene_bar is None)
    wall2.refresh_scenes([])
    check("空数据不会凭空造一个条", wall2.scene_bar is None)
    wall2.refresh_scenes([{"scene_id": "s9", "name": "新场景", "enable": True}])
    app.processEvents()
    check("有了才建条", wall2.scene_bar is not None)
    check("补建时插在布局第 0 位（场景条在墙顶部）",
          wall2.scene_bar is not None and wall2._col.indexOf(wall2.scene_bar) == 0)  # noqa: SLF001
    check("补建的条是可见的", wall2.scene_bar is not None and wall2.scene_bar.isVisible())
    check("补建时胶囊也是显示的",
          wall2.scene_bar is not None
          and all(p.isVisible() for p in wall2.scene_bar._pills))  # noqa: SLF001
    wall2.close()


def checks_client_scenes() -> None:
    """``Client.scenes`` 必须把"结构不符"和"真没有场景"分开。

    2026-11-26 的真事故：库内 ``_get_scenes_list`` 的判据是
    ``if ret and "manual_scene_info_list" in ret``，不满足就 ``return []``，
    于是云端换键 / 返回别的结构时调用方拿到的是**合法空表**——界面照实清空，
    首页整排场景消失，而日志里一条错误都没有。
    """
    from mijiaAPI.errors import APIError

    from mihome.core.api import Client

    class _Raw:
        locale = "zh_CN"
        service_login_url = ""
        auth_data: dict = {}

        def __init__(self, ret=None, exc=None) -> None:
            self.ret = ret
            self.exc = exc

        def _request(self, uri, data):  # noqa: ANN001, ANN202
            if self.exc is not None:
                raise self.exc
            return self.ret

        def _get_home_owner(self, hid):  # noqa: ANN001, ANN202
            return 1

        def get_homes_list(self):  # noqa: ANN202
            return [{"id": "111", "uid": 1}]

    def call(**kw):  # noqa: ANN003, ANN202
        return Client(api=_Raw(**kw)).scenes("111")

    check("真空表照实返回（云端真删光了）",
          call(ret={"manual_scene_info_list": []}) == [])
    check("result 里没有 manual_scene_info_list → None",
          call(ret={}) is None)
    check("键名换了（scene_list）→ None，不能当空表",
          call(ret={"scene_list": [{"scene_id": "1", "name": "x"}]}) is None)
    check("请求抛异常 → None", call(exc=APIError(-1, "boom")) is None)
    got = call(ret={"manual_scene_info_list": [
        {"scene_id": "1", "name": "a"}, {"scene_id": "2", "name": "b"}]})
    check("正常两条，且每条都补了 home_id",
          isinstance(got, list) and len(got) == 2
          and all(s.get("home_id") == "111" for s in got),
          f"{got}")


def checks_load_fallback() -> None:
    """``Store._load_job``：某家场景拉失败时沿用上一轮的表。

    ``Client.scenes`` 失败返回 ``None`` 而不是空表——空表会让首页那排场景整排消失，
    用户以为场景被删了，而且要到下次成功刷新（5 分钟后）才回来。
    """
    from types import SimpleNamespace

    class _Caps:
        def save(self) -> None:
            pass

    class _Topo:
        homes = [SimpleNamespace(id="h1")]

        @staticmethod
        def real_devices() -> list:
            return []

    class _Client:
        capabilities = _Caps()
        answer: list | None = None

        @staticmethod
        def load_topology() -> _Topo:
            return _Topo()

        def scenes(self, home_id: str):  # noqa: ANN201
            return self.answer

    class _Worker:
        pass

    st = Store(_Client(), _Worker(), None)  # type: ignore[arg-type]
    st.prefs = Prefs(Path(tempfile.gettempdir()) / "mihome_verify_scenes_fallback.json")
    st.scenes_by_home = {"h1": [{"scene_id": "old", "name": "旧场景"}]}
    prev = {k: list(v) for k, v in st.scenes_by_home.items()}

    _Client.answer = None  # 网络挂了
    _, _, scenes = st._load_job(prev)
    check("拉失败（None）沿用旧表", [s["scene_id"] for s in scenes["h1"]] == ["old"],
          str(scenes))

    _Client.answer = [{"scene_id": "new", "name": "新场景"}]
    _, _, scenes = st._load_job(prev)
    check("拉成功换成新表", [s["scene_id"] for s in scenes["h1"]] == ["new"], str(scenes))

    _Client.answer = []
    _, _, scenes = st._load_job(prev)
    check("空表原样返回给上层（防抖在 _merge_scenes 做）", scenes["h1"] == [], str(scenes))

    # 空表防抖：2026-11-26 真事故——刚在 App 里建完场景，那一轮云端回空表，
    # 首页整排胶囊当场消失。空表先沿用一轮，连续两轮空才当真。
    st.scenes_by_home = {"h1": [{"scene_id": "old", "name": "旧场景"}]}
    st._scenes_empty.clear()
    merged = st._merge_scenes({"h1": []})
    check("第一轮空表 → 沿用旧表", [s["scene_id"] for s in merged["h1"]] == ["old"],
          str(merged))
    merged = st._merge_scenes({"h1": []})
    check("连续第二轮空表 → 真清空", merged["h1"] == [], str(merged))
    merged = st._merge_scenes({"h1": [{"scene_id": "new", "name": "新"}]})
    check("来数据后计数清零", not st._scenes_empty and len(merged["h1"]) == 1, str(merged))
    merged = st._merge_scenes({"h1": []})
    check("计数清零后再遇空表，又是'先沿用'", len(merged["h1"]) == 1, str(merged))

    st.scenes_by_home = {}
    st._scenes_empty.clear()
    merged = st._merge_scenes({"h1": []})
    check("旧表本来就空 → 空表照收，不报错", merged["h1"] == [], str(merged))

    _Client.answer = None
    st.scenes_by_home = {}
    _, _, scenes = st._load_job({})
    check("第一次就失败 → 空表，不是崩", scenes["h1"] == [], str(scenes))
    st.prefs.path.unlink(missing_ok=True)


def main() -> int:
    app = QApplication(sys.argv[:1])  # noqa: F841

    print("1) Prefs.hidden_scenes")
    p = Prefs(Path(tempfile.gettempdir()) / "mihome_verify_scenes_prefs.json")
    p.hidden = []
    p.hidden_scenes = []
    first = p.set_scene_hidden("s1", True)
    second = p.set_scene_hidden("s1", True)
    check("重复隐藏只记一次", first and not second, f"{first}/{second}")
    check("已记录的能查出来", p.is_scene_hidden("s1"))
    check("取消隐藏返回变化", p.set_scene_hidden("s1", False) is True)
    check("取消后没了", not p.is_scene_hidden("s1"))
    p.set_scene_hidden("s2", True)
    again = Prefs(p.path)
    check("落盘后能读回", again.hidden_scenes == ["s2"], str(again.hidden_scenes))
    check("设备隐藏与场景隐藏分开", p.hidden == [])
    again.clear_hidden_scenes()
    check("清空后为空", Prefs(p.path).hidden_scenes == [])
    p.path.unlink(missing_ok=True)

    print("\n2) scenes_for 过滤")
    store = make_store()
    check("默认全在", len(store.scenes_for("家")) == 3)
    store.prefs.set_scene_hidden("s2", True)
    check("隐藏后首页少一个", [s["scene_id"] for s in store.scenes_for("家")] == ["s1", "s3"])
    check("设置页要的全都在",
          [s["scene_id"] for s in store.scenes_for("家", include_hidden=True)] == ["s1", "s2", "s3"])
    store.prefs.set_scene_hidden("s2", False)
    check("恢复后回到列表", len(store.scenes_for("家")) == 3)
    store.prefs.path.unlink(missing_ok=True)

    print("\n3) 右键菜单是真圆角")
    host = QWidget()
    host.resize(400, 300)
    menu = Menu(host, theme.LIGHT)
    for text in ("打开详情", "隐藏设备"):
        menu.addAction(text)
    menu.popup(host.mapToGlobal(host.rect().center()))
    app.processEvents()
    img = menu.grab().toImage()
    w, h = img.width(), img.height()
    check("菜单尺寸正常", w > 40 and h > 20, f"{w}x{h}")
    corner = QColor(img.pixelColor(0, 0)).alpha()
    check("左上角是透明的（不是矩形底）", corner < 40, f"alpha={corner}")
    mid = QColor(img.pixelColor(w // 2, h // 2)).alpha()
    check("中间是实心的", mid > 240, f"alpha={mid}")
    partial = 0
    for y in range(0, min(14, h)):
        for x in range(0, min(14, w)):
            a = QColor(img.pixelColor(x, y)).alpha()
            partial += 1 if 8 < a < 248 else 0
    check("圆角边缘有渐变（不是一刀切）", partial >= 6, f"{partial} 个中间态")
    menu.close()

    print("\n4) 场景条单独刷新（不重建墙）")
    checks_refresh(app)

    print("\n5) 场景拉失败不清空旧表")
    checks_load_fallback()

    print("\n6) 场景表：结构不符与真删光必须分开")
    checks_client_scenes()

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败")
        for line in FAILS:
            print("  -", line)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
