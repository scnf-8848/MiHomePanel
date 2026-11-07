# SPDX-License-Identifier: GPL-3.0-or-later
"""验证"设备名为什么是英文"的根因修好了：请求语言必须是合法的 locale。

背景：中文 Windows 上 ``locale.getlocale()[0]`` 返回的是 ``"Chinese (Simplified)_China"``
——一个**显示名**，不是 locale 串。mijiaAPI 原样塞进 Cookie，还用
``split('_')[1]`` 去算 countryCode，于是两个值都坏了：

    locale=Chinese (Simplified)_China
    countryCode=China                      # 该是 CN

服务端解析不了就回退英文，设备列表返回的是厂商默认英文名
（"Xiaomi Smart Wall Switch (3 Gang)"）。改成 ``zh_CN`` 后同一份凭据下 55 台设备
全部返回中文名。

跑法：

    .venv/Scripts/python.exe -X utf8 scripts/verify_locale.py          # 离线
    .venv/Scripts/python.exe -X utf8 scripts/verify_locale.py --live    # 再验真实云端
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mihome.core.api import API_LOCALE, Client, apply_locale  # noqa: E402

FAILS: list[str] = []
#: 中文 Windows 上 ``locale.getlocale()[0]`` 的实际取值，也是这次问题的源头。
BROKEN = "Chinese (Simplified)_China"
#: 合法的 locale 串：语言两位小写 + 下划线 + 地区两位大写。
LOCALE_RE = re.compile(r"[a-z]{2}_[A-Z]{2}")
#: 合法的 countryCode：两位大写字母。
REGION_RE = re.compile(r"[A-Z]{2}")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILS.append(f"{name} {detail}")


class FakeApi:
    """按 mijiaAPI 的字段和行为伪造，只保留 ``apply_locale`` 会碰到的部分。

    ``_init_session`` 是关键：locale 是拼进 Cookie 的，不重建设置就等于没改。
    """

    def __init__(self, locale: str = BROKEN, *, signed_in: bool = True) -> None:
        self.locale = locale
        self.auth_data = {"cUserId": "123", "serviceToken": "tok"} if signed_in else {}
        self.service_login_url = (
            "https://account.xiaomi.com/pass/serviceLogin"
            f"?_json=true&sid=mijia&_locale={locale}"
        )
        self.inits = 0
        self.cookie = ""

    def _init_session(self) -> None:  # noqa: N802 - 上游就是这个名字
        self.inits += 1
        region = self.locale.split("_")[1] if "_" in self.locale else "CN"
        self.cookie = f"countryCode={region};locale={self.locale}"


def section_broken() -> None:
    """先复现坏值，证明后面的断言有区分度（不然全是自说自话）。"""
    print("\n1) 复现：坏 locale 拼出来的两个值都不合法")
    api = FakeApi()
    api._init_session()
    code = api.cookie.split("countryCode=")[1].split(";")[0]
    check("locale 串不合法", not LOCALE_RE.fullmatch(api.locale), api.locale)
    check("countryCode 不合法（是 China 不是 CN）", not REGION_RE.fullmatch(code), code)
    check("Cookie 里带空格，进不了请求头", " " in api.cookie)


def section_fix() -> None:
    print("\n2) apply_locale 修好了它")
    api = FakeApi()
    apply_locale(api)
    check("locale 换成 zh_CN", api.locale == API_LOCALE, api.locale)
    check("语言串合法（xx_XX）", bool(LOCALE_RE.fullmatch(api.locale)), api.locale)
    check("重建了一次 session（locale 在 Cookie 里）", api.inits == 1, f"{api.inits} 次")
    check("Cookie 的 countryCode = CN", "countryCode=CN;" in api.cookie, api.cookie)
    check("登录页 URL 的 _locale 也换了",
          api.service_login_url.endswith(f"_locale={API_LOCALE}"), api.service_login_url)

    print("\n3) 幂等：已经对了就别再重建")
    again = FakeApi()
    apply_locale(again)
    apply_locale(again)
    check("只建一次 session", again.inits == 1, f"{again.inits} 次")

    print("\n4) 未登录时没有 session 可建")
    anon = FakeApi(signed_in=False)
    apply_locale(anon)
    check("locale 照样改正", anon.locale == API_LOCALE, anon.locale)
    check("不碰不存在的 session", anon.inits == 0, f"{anon.inits} 次")

    print("\n5) Client 构造时就应用（不用调用方记得）")
    api = FakeApi()
    Client(api=api)
    check("构造后 locale 已经是 zh_CN", api.locale == API_LOCALE, api.locale)
    check("Cookie 已重建", "countryCode=CN;" in api.cookie, api.cookie)


def section_upstream() -> None:
    """真上游对象也过一遍：只读凭据文件，不联网。"""
    print("\n6) 真实 mijiaAPI 对象")
    try:
        from mijiaAPI import mijiaAPI
    except Exception as exc:  # noqa: BLE001
        print(f"  [SKIP] 没装 mijiaAPI：{exc}")
        return
    api = mijiaAPI()
    if not api.auth_data:
        print("  [SKIP] 本地没有登录凭据")
        return
    before = api.locale
    apply_locale(api)
    cookie = api.session.headers.get("Cookie", "")
    check("locale 从系统值改成 zh_CN", api.locale == API_LOCALE, f"{before} → {api.locale}")
    check("请求头 Cookie 里 countryCode = CN", "countryCode=CN;" in cookie)
    check("请求头 Cookie 里 locale = zh_CN", "locale=zh_CN" in cookie)


def section_live() -> None:
    """联网验一次"名字真的是中文"。只在 --live 下跑。"""
    print("\n7) 真实云端设备名（--live）")
    client = Client()
    if not client.signed_in:
        print("  [SKIP] 未登录")
        return
    devices = client.load_topology().real_devices()
    if not devices:
        print("  [SKIP] 没拉到设备")
        return
    zh = [d for d in devices if re.search(r"[\u4e00-\u9fff]", d.name)]
    check("含中文的设备名占比 ≥ 80%", len(zh) / len(devices) >= 0.8,
          f"{len(zh)}/{len(devices)}")
    for node in devices[:8]:
        print(f"    · {node.name}  ({node.model})")


def main() -> int:
    section_broken()
    section_fix()
    section_upstream()
    if "--live" in sys.argv:
        section_live()

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败：")
        for line in FAILS:
            print("  -", line)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
