# SPDX-License-Identifier: GPL-3.0-or-later
"""米家自动化极客版登录码探针（一次性真机验证，不属于主程序）。

安全约定：
  * 默认只列设备，不发取码请求；必须显式给 ``--did`` 才会调云端 RPC。
  * 登录码只打印到终端，不写文件、不进日志、不落盘。
  * 先用 ``miIO.get_autowebconfig_url``（不消耗登录码）探出可用的域名 / 请求头组合，
    确定后才调用一次 ``miIO.get_central_link_passcode``，避免反复作废旧码。
  * 用户实测：一旦取了新码，旧码立即失效。所以本脚本一次运行最多调一次取码接口，
    不要接进轮询、自动刷新或自动化脚本集里定时跑。

用法（在项目根目录）：

    .venv/Scripts/python.exe -X utf8 scripts/probe_geek_passcode.py
    .venv/Scripts/python.exe -X utf8 scripts/probe_geek_passcode.py --did <中枢 did>
    .venv/Scripts/python.exe -X utf8 scripts/probe_geek_passcode.py --did <中枢 did> --url-only

依据：docs/reference/mijia-geek-login-code.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mijiaAPI.miutils import (
    decrypt,
    gen_nonce,
    generate_enc_params,
    get_signed_nonce,
)

from mihome.core.api import Client

URL_METHOD = "miIO.get_autowebconfig_url"
CODE_METHOD = "miIO.get_central_link_passcode"
BASES = (
    "https://api.mijia.tech/app",
    "https://core.api.mijia.tech/app",
)
HINTS = ("中枢", "网关", "gateway", "hub", "router", "路由")


def _candidates(client: Client) -> list[dict[str, Any]]:
    """从原始设备列表里挑疑似中枢，不用拓扑（拓扑会过滤掉没有 spec 的设备）。"""
    raw = client.raw().get_devices_list()
    out = []
    for d in raw:
        text = f"{d.get('name', '')} {d.get('model', '')}".lower()
        if any(h in text for h in HINTS):
            out.append(d)
    return out


def _show_candidates(client: Client) -> None:
    print(f"auth: {client.auth_path}")
    rows = _candidates(client)
    print(f"疑似中枢/网关设备 {len(rows)} 台：")
    for d in rows:
        keys = ("did", "name", "model", "spec_type", "home_id", "isOnline", "online", "parent_id")
        bits = [f"{k}={d[k]}" for k in keys if k in d]
        print("  " + "  ".join(bits))
    if not rows:
        print("  （没找到，试试 --all 打印全部设备的名称/型号）")


def _via_request(api: Any, did: str, method: str) -> Any:
    """最省事的路：直接复用上游的 _request（签名 + 解密都现成）。"""
    return api._request(
        f"/home/rpc/{did}", {"method": method, "params": []}
    )


def _via_manual(api: Any, did: str, method: str, base: str) -> Any:
    """自己拼签名，可换域名并补 App 才带的头。"""
    uri = f"/home/rpc/{did}"
    ssecurity = api.auth_data["ssecurity"]
    payload = {"method": method, "params": []}
    params: dict[str, Any] = {"data": json.dumps(payload, separators=(",", ":"))}
    nonce = gen_nonce()
    signed = get_signed_nonce(ssecurity, nonce)
    params = generate_enc_params(uri, "POST", signed, nonce, params, ssecurity)
    headers = {
        "MIOT-REQUEST-MODEL": "xiaomi.gateway.hub1",
        "domain-refer": base.split("//", 1)[-1].split("/", 1)[0],
        "Origin-From": "MiHome",
        "X-XIAOMI-PROTOCAL-FLAG-CLI": "PROTOCAL-HTTP2",
    }
    resp = api.session.post(base + uri, data=params, headers=headers, timeout=20)
    text = resp.text
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        obj = json.loads(decrypt(ssecurity, nonce, text))
    if isinstance(obj, dict) and obj.get("code", 0) not in (0, None):
        raise RuntimeError(f"code={obj.get('code')} message={obj.get('message') or obj.get('desc')}")
    return obj.get("result", obj) if isinstance(obj, dict) else obj


# (标签, 调用函数)
def _modes(api: Any, did: str):
    yield "mijiaAPI._request / api.mijia.tech", lambda m: _via_request(api, did, m)
    for base in BASES:
        yield f"manual+headers / {base}", lambda m, b=base: _via_manual(api, did, m, b)


def _describe(result: Any) -> str:
    if isinstance(result, dict):
        # 结果里只留结构与非敏感字段；passcode 由主流程单独打印
        return json.dumps(
            {k: ("<hidden>" if k in ("passcode", "passwd", "password", "pwd") else v)
             for k, v in result.items()},
            ensure_ascii=False,
        )[:400]
    return repr(result)[:400]


def _probe(client: Client, did: str, url_only: bool) -> None:
    api = client.raw()
    print(f"auth: {client.auth_path}    did: {did}")

    working = None
    for label, call in _modes(api, did):
        try:
            result = call(URL_METHOD)
        except Exception as exc:  # noqa: BLE001
            print(f"[地址] {label}: 失败 {type(exc).__name__}: {exc}")
            continue
        print(f"[地址] {label}: 成功 → {_describe(result)}")
        working = (label, call)
        break

    if working is None:
        print("取地址全部失败，未调用取码接口（没有消耗登录码）。")
        return

    if url_only:
        print("--url-only：已停止，未消耗登录码。")
        return

    label, call = working
    try:
        result = call(CODE_METHOD)
    except Exception as exc:  # noqa: BLE001
        print(f"[登录码] {label}: 失败 {type(exc).__name__}: {exc}")
        return
    if not isinstance(result, dict):
        print(f"[登录码] 返回结构异常：{_describe(result)}")
        return
    code = next((result[k] for k in ("passcode", "passwd", "password", "pwd") if k in result), None)
    if code is None:
        print(f"[登录码] 返回里没有 passcode 字段：{_describe(result)}")
        return
    print(f"\n=== 登录码: {code} ===")
    print("（一次性，用完即废；本脚本不会把它写入任何文件）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--did", help="中枢网关的 did；不给则只列设备")
    ap.add_argument("--url-only", action="store_true", help="只试取地址接口，不消耗登录码")
    ap.add_argument("--all", action="store_true", help="打印全部设备名称/型号，便于找 did")
    args = ap.parse_args()

    client = Client()
    if args.all:
        for d in client.raw().get_devices_list():
            print(f"  {d.get('did')}  {d.get('name')}  {d.get('model')}")
        return
    if not args.did:
        _show_candidates(client)
        return
    _probe(client, args.did, args.url_only)


if __name__ == "__main__":
    main()
