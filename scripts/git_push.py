# SPDX-License-Identifier: GPL-3.0-or-later
"""按「谁能通就用谁」的顺序推送到 GitHub，不再赌单一配置。

## 为什么要有这个脚本

这台机器上"能不能连上 github.com"取决于**三件互不相干的事**，而它们各自都会坏：

1. **用户的 Clash Verge**（`verge-mihomo`，混合端口 7897）——关掉时端口直接没有，
   开着时也可能是"端口在听、上游全超时"（2026-11-17 实测过：TCP 能连，三条外网
   全在 5.0 秒整超时，那说明选中的节点是死的，不是 git 的问题）。
2. **直连**——`api.github.com` 一直 200（0.2 秒），但 `github.com:443` 会被断，
   git 自己的 `Failed to connect ... after 21s` 就是这么来的。
3. **WorkBuddy 沙箱给每条命令注入的出口代理**（`sandbox-cli`，`127.0.0.1:116xx`）——
   端口每次重启都变，而且见过它对 `github.com` 单独报 `CONNECT tunnel 502`。

所以**没有任何一个固定配置能在"Clash 开 / 关"两种状态下都对**：写死 7897，Clash 一关
就全挂；写死直连，Clash 开了但系统走了 TUN/规则时又可能不通。这个脚本改成每次推送前
**探一次再走**。

## 探测为什么不是"TCP 能不能连"

Clash 的端口开着不代表链路通（上面第 1 条），`connect_ex == 0` 会给出假阳性。这里用
真正的 `HEAD https://github.com/`（经代理时先 `CONNECT` 再 TLS），4 秒超时，失败的路
零成本跳过——不会出现"等 git 自己耗 21 秒再报错"。

## 用法

```
python scripts/git_push.py                    # 等价于 git push origin main
python scripts/git_push.py origin main        # 原样透传给 git push
python scripts/git_push.py --dry-run origin main
python scripts/git_push.py --cmd fetch --tags # 其它要联网的子命令
```

退出码：`0` 成功；`1` 探到活路但命令都失败；`2` 三条线路全探不通（改网络，别改代码）。

## 全局配置不要设 `http.proxy ""`

2026-11-17 曾按"去掉本地代理"的要求设过 `git config --global http.proxy ""`，**已撤销**：
空值在 git 里表示"明确不走代理"，优先级**高于** `HTTPS_PROXY` 环境变量，而 GitHub Desktop
正是靠注入这对环境变量把系统代理（Clash 7897）交给它自带的 git 的。实测（自带 git 2.56.1）：
设空值时它无视 env 直接连 `20.247.166.64:443`，30 秒后 `Connection died`；撤销后立刻通。
本脚本对每条线路都显式传 `-c http.proxy=...`，不受全局值影响，所以两边互不干扰。
"""

from __future__ import annotations

import http.client
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
#: Clash Verge 的混合端口（HTTP 与 SOCKS 同一个口）
CLASH = "http://127.0.0.1:7897"
#: 探测的超时。4 秒足够区分"路不通"和"慢"——实测 Clash 死节点是 5.0 秒整超时，
#: 直连被断是 21 秒，都远在这之上。
PROBE_TIMEOUT = 4.0


def _host_port(url: str) -> tuple[str, int]:
    parts = urlsplit(url if "//" in url else f"//{url}")
    return parts.hostname or "127.0.0.1", parts.port or 80


def probe(proxy: str | None) -> str:
    """探一次 ``https://github.com/``，通了返回 ``"ok"``，否则返回原因。"""
    try:
        if proxy:
            host, port = _host_port(proxy)
            conn = http.client.HTTPSConnection(host, port, timeout=PROBE_TIMEOUT)
            # set_tunnel 先发 CONNECT，成功后才套 TLS——这一步能抓住"端口在听、
            # 上游是死的"那种假阳性
            conn.set_tunnel("github.com", 443)
        else:
            conn = http.client.HTTPSConnection("github.com", 443, timeout=PROBE_TIMEOUT)
        conn.request("HEAD", "/", headers={"User-Agent": "MiHomePanel-push-probe"})
        status = conn.getresponse().status
        conn.close()
        # github.com 对 HEAD / 回 301，落在 5xx 之外都算链路活着
        return "ok" if status < 500 else f"HTTP {status}"
    except Exception as exc:  # noqa: BLE001 - 任何异常都只意味着这条路不好走
        return f"{type(exc).__name__}: {exc}"


def routes() -> list[tuple[str, str | None]]:
    """候选线路，按"最省事"排序：直连 → Clash → 沙箱注入的代理。

    同一个口可能以两种名义出现（比如 env 已经指到 Clash），按 ``(host, port)`` 去重。
    """
    out: list[tuple[str, str | None]] = []
    seen: set[tuple[str, int] | None] = {None}  # None 代表直连

    def add(label: str, proxy: str | None) -> None:
        if proxy is not None:
            try:
                key: tuple[str, int] | None = _host_port(proxy)
            except ValueError:
                return
            if key in seen:
                return
            seen.add(key)
        out.append((label, proxy))

    add("直连", None)
    add(f"Clash {CLASH}", CLASH)
    env = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or ""
    if env:
        add(f"沙箱注入 {env}", env)
    return out


def main(argv: list[str]) -> int:
    argv = list(argv)
    # `--cmd fetch` 这类：默认只推，但拉取/查远端同样会撞线路问题，得复用同一套探活
    cmd = "push"
    if "--cmd" in argv:
        i = argv.index("--cmd")
        try:
            cmd = argv[i + 1]
        except IndexError:
            print("--cmd 后面要跟子命令，比如 --cmd fetch")
            return 1
        del argv[i : i + 2]
    args = argv or ["origin", "main"]

    alive: list[tuple[str, str | None]] = []
    for label, proxy in routes():
        result = probe(proxy)
        print(f"{'✓' if result == 'ok' else '✗'} {label:<28} {result}", flush=True)
        if result == "ok":
            alive.append((label, proxy))

    if not alive:
        print("\n三条线路都探不通：先看 Clash 的节点/订阅，或者换个网络再说。")
        return 2

    for label, proxy in alive:
        # 每条线路都显式给 `-c http.proxy=...`，既不吃全局配置也不吃 env：
        # env 里那对是沙箱注入的（端口每次重启都变、还见过只对 github 报 502），
        # 全局又被设成过空值；而 `-c` 的优先级高于两者。
        cfg = ["-c", f"http.proxy={proxy or ''}"]
        print(f"\n→ 用「{label}」执行：git {cmd} {' '.join(args)}", flush=True)
        code = subprocess.run(["git", *cfg, cmd, *args], cwd=ROOT).returncode
        if code == 0:
            print(f"✓ 完成（线路：{label}）")
            return 0
        print(f"✗ 「{label}」失败，换下一条", flush=True)

    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
