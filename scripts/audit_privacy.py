"""隐私审计：扫描受版本控制的文件，命中真实凭据 / 个人标识 / 截图。

用法::

    python scripts/audit_privacy.py                  # 整仓（读工作区）
    python scripts/audit_privacy.py --staged         # 只查暂存区（提交前）
    python scripts/audit_privacy.py --rev v0.1.4     # 查某个提交 / tag
    python scripts/audit_privacy.py --write-baseline # 把当前命中登记为基线
    python scripts/audit_privacy.py --json           # 机器可读（不含原文）

判定分三级：``FAIL`` 拦合并、``WARN`` 只提示、基线内命中直接跳过。
退出码：0 无新增 FAIL；1 有 FAIL；2 环境错误（不在 git 仓库 / git 不可用）。

规则口径见 ``AGENTS.md`` 与 ``docs/dev/conventions.md``；本文件自身与
``AGENTS.md`` 是"占位文档"，整份跳过，避免规则里的示例值反过来命中自己。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / ".privacy-baseline.json"
WORDS = ROOT / ".privacy-words.txt"

#: 自指文件：本脚本自身与派生基线不扫描；AGENTS.md / conventions.md 是策略文档，
#: 仍要扫描（占位值由规则自身豁免），否则把真实值贴进规范里反而查不出来
SELF_FILES = {
    "scripts/audit_privacy.py",
    ".privacy-baseline.json",
}

#: 不做文本扫描的扩展名（二进制 / 字体 / Qt 翻译）
SKIP_EXT = {
    ".pyc", ".pyo", ".pyd", ".dll", ".exe", ".zip", ".ico", ".qm",
    ".ttf", ".otf", ".woff", ".woff2", ".bin", ".pak", ".so", ".dylib",
}

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

#: 允许入库的图片（应用图标，构建要用）
IMAGE_ALLOW = {"assets/app.ico"}

#: 常见房名：单独出现只是普通词，WARN 提示人工确认
GENERIC_ROOMS = (
    "客厅", "主卧", "次卧", "书房", "厨房", "卫生间", "儿童房", "阳台", "餐厅",
)

RE_CRED = re.compile(
    r"(?i)\b(passToken|serviceToken|refreshToken|access_token|ssecurity|cUserId|cPassToken)"
    r"\b\s*[:=]\s*[\"']?([A-Za-z0-9+/=_\-]{8,})"
)
RE_COOKIE = re.compile(r"(?i)cookie\s*[:=].{0,200}?serviceToken=[A-Za-z0-9]{8,}")
RE_IP = re.compile(
    r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"
)
RE_USERPATH = re.compile(
    r"""[A-Za-z]:[\\/]Users[\\/]([^\\/\s"'`]+)|/(?:home|Users)/([^/\s"'`]+)"""
)
RE_DIGITS = re.compile(r"\b\d{9,12}\b")

#: 米家 BLE 设备 did：``blt.<版本>.<信标串>``，每台唯一，不是型号
RE_BLE_DID = re.compile(r"\bblt\.\d+\.[a-z0-9]{6,}\b", re.I)
RE_PHONE = re.compile(r"(?<![\d.])1[3-9]\d{9}(?!\d)")
RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
RE_HOME = re.compile(
    r"(?:家庭|home)(?:_id)?\s*[=:：]?\s*(\d{4,5})(?!\d)|(?<!\d)(\d{3,5})\s*(?:家庭)"
)
RE_ROOM = re.compile("|".join(GENERIC_ROOMS))

#: 房名要同行出现这些词才算"在描述真实房间"，否则「打开客厅灯」这类示例只是举例
ROOM_CTX = re.compile(r"房间|room|实测|该家|这个家", re.I)

#: 占位值：出现在凭据键后面也放行
PLACEHOLDER = {"fake", "tok", "token", "xxx", "none", "null", "empty", "test"}

#: 路径里允许出现的账户段（系统目录 / 文档占位）
PATH_OK = {
    "<用户名>", "%userprofile%", "public", "default", "default user",
    "runner", "user", "example", "username", "用户名",
}

#: 邮箱白名单
MAIL_OK = ("noreply@github.com", "users.noreply.github.com", "example.com")

#: URL 路由里长得像 /home/<用户名> 的段（米家 RPC 路径），不算主机路径
URL_SEG = {"rpc", "api", "device_list", "home_device_list", "home"}

#: 非个人地址：relay 宣告的不可达地址（协议常量，不属于任何人的局域网）
NON_PERSONAL_IPS = {"10.255.255.1"}

#: 文档占位 IP 段：AGENTS.md 第三节把「192.168.1.x」定为唯一合法的内网 IP 占位
PLACEHOLDER_IP = "192.168.1."

RE_URL = re.compile(r"https?://\S+")

#: DID 判定关键词：同行 ±40 字符内出现才算"确认"
DID_WORDS = re.compile(r"did|设备|家庭|home|parent|子设备|room|uid", re.I)

#: 数字本身是这些键的值时才豁免（索引 / 错误码），同一行其它数字照常判定
DIGIT_VALUE_KEY = re.compile(r"(?i)(?:\"?(?:siid|piid|aiid|code)\"?\s*[:=]\s*[\"']?)$")

EXEMPT_LINE = re.compile(r"privacy:\s*ignore")


@dataclass
class Hit:
    path: str
    line: int
    rule: str
    level: str
    masked: str
    raw: str = ""

    @property
    def key(self) -> str:
        digest = hashlib.sha1(self.raw.encode("utf-8")).hexdigest()[:12]
        return f"{self.rule}|{self.path}|{digest}"

    def text(self) -> str:
        return f"{self.path}:{self.line}  [{self.rule}]  {self.masked}"


def mask(value: str) -> str:
    """打码：长度 >=8 保留前 4 后 2；4-7 位保留前 2；其余保留首字。"""
    value = value.strip()
    n = len(value)
    if n >= 8:
        return f"{value[:4]}***{value[-2:]}"
    if n >= 4:
        return f"{value[:2]}***"
    if n:
        return f"{value[0]}***"
    return "***"


def git(*args: str) -> bytes:
    proc = subprocess.run(
        ["git", *args], cwd=str(ROOT), capture_output=True
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip())
    return proc.stdout


def split_z(raw: bytes) -> list[str]:
    return [p.decode("utf-8", "surrogateescape") for p in raw.split(b"\0") if p]


def read_sources(mode: str, rev: str) -> list[tuple[str, bytes]]:
    """按模式取 (路径, 字节)。读的是 git 对象，不是磁盘，避免"改回去"漏检。"""
    if mode == "staged":
        paths = split_z(git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"))
        return [(p, git("show", f":{p}")) for p in paths]
    if mode == "rev":
        paths = split_z(git("ls-tree", "-r", "--name-only", "-z", rev))
        return [(p, git("cat-file", "blob", f"{rev}:{p}")) for p in paths]
    out: list[tuple[str, bytes]] = []
    for p in split_z(git("ls-files", "-z")):
        f = ROOT / p
        if f.is_file():
            out.append((p, f.read_bytes()))
    return out


def sequential_digits(val: str) -> bool:
    """严格逐位 +1 的数字串（如 ``0123456789``）：键码表，不是设备标识。"""
    return len(val) >= 6 and all(
        ord(val[i + 1]) == ord(val[i]) + 1 for i in range(len(val) - 1)
    )


def url_param_exempt(line: str, m: re.Match[str]) -> bool:
    """URL 查询参数的值（``...?mid=2247487322``）：属于链接参数，不是设备标识。"""
    for url in RE_URL.finditer(line):
        if url.start() <= m.start() < url.end():
            return m.start() > 0 and line[m.start() - 1] == "="
    return False


def github_path_exempt(line: str, m: re.Match[str]) -> bool:
    """GitHub 数字用户 id（``609293964/仓库名``）：账号域内的仓库路径，不是设备标识。"""
    return line[m.end(): m.end() + 1] == "/" and "github.com/" in line.lower()


def digit_exempt(line: str, m: re.Match[str]) -> bool:
    val = m.group(0)
    if val.startswith("10000000") or val.startswith("000000"):
        return True  # AGENTS.md 规定的占位 did / uid
    if sequential_digits(val) or url_param_exempt(line, m) or github_path_exempt(line, m):
        return True  # 键码表 / URL 参数 / GitHub 仓库路径
    if len(val) == 10 and 1600000000 <= int(val) <= 1899999999:
        return True  # Unix 秒
    if len(val) == 13:
        return True  # 毫秒时间戳
    prev = line[m.start() - 1] if m.start() else ""
    nxt = line[m.end()] if m.end() < len(line) else ""
    if prev in "-.#":  # 错误码 / 版本 / 颜色
        return True
    if nxt == "%":  # 百分比
        return True
    # 只有"这个数字本身就是键值"才豁免，避免整行带 siid 就漏掉真实 did
    return bool(DIGIT_VALUE_KEY.search(line[: m.start()]))


def scan_text(path: str, text: str, hits: list[Hit]) -> None:
    for no, line in enumerate(text.splitlines(), 1):
        if EXEMPT_LINE.search(line):
            continue

        for m in RE_CRED.finditer(line):
            value = m.group(2)
            if value.lower() in PLACEHOLDER or value.startswith("<") or set(value) <= set("xX*."):
                continue
            hits.append(Hit(path, no, "CRED-1", "FAIL", mask(value), value))

        if RE_COOKIE.search(line) and "serviceToken=..." not in line:
            hits.append(Hit(path, no, "CRED-2", "FAIL", mask(line.strip()), line))

        for m in RE_IP.finditer(line):
            ip = m.group(0)
            if ip.startswith("127.") or ip in NON_PERSONAL_IPS:
                continue
            if ip.startswith(PLACEHOLDER_IP):
                hits.append(Hit(path, no, "NET-2", "WARN", mask(ip), ip))
                continue
            hits.append(Hit(path, no, "NET-1", "FAIL", mask(ip), ip))

        for m in RE_USERPATH.finditer(line):
            seg = (m.group(1) or m.group(2) or "").strip()
            if not seg or seg.lower() in PATH_OK or seg.startswith("<"):
                continue
            # URL 路由（/home/device_list、/home/rpc/<did>）和中文占位不算用户名
            if seg.lower() in URL_SEG or "_" in seg or "." in seg or not seg.isascii():
                continue
            hits.append(Hit(path, no, "PATH-1", "FAIL", f"...Users/{mask(seg)}", seg))

        for m in RE_DIGITS.finditer(line):
            if digit_exempt(line, m):
                continue
            around = line[max(0, m.start() - 40): m.end() + 40]
            level = "FAIL" if DID_WORDS.search(around) else "WARN"
            hits.append(Hit(path, no, "DID-1" if level == "FAIL" else "DID-2",
                            level, mask(m.group(0)), m.group(0)))

        for m in RE_BLE_DID.finditer(line):
            val = m.group(0)
            # 占位写法与规范示例不算泄露；其余一律拦（这个形状本身就是设备 did）
            if val.lower().startswith("blt.0.") or "占位" in line or "示例" in line:
                continue
            hits.append(Hit(path, no, "DID-3", "FAIL", mask(val), val))

        for m in RE_PHONE.finditer(line):
            hits.append(Hit(path, no, "PII-1", "FAIL", mask(m.group(0)), m.group(0)))

        for m in RE_EMAIL.finditer(line):
            mail = m.group(0)
            if any(ok in mail for ok in MAIL_OK):
                continue
            hits.append(Hit(path, no, "PII-2", "WARN", mask(mail), mail))

        for m in RE_HOME.finditer(line):
            num = m.group(1) or m.group(2) or ""
            if len(num) == 4 and num[:2] in ("19", "20"):
                continue  # 年份
            # 家庭名无法从形状可靠识别：这里只提示，确认真实值靠本地词表（WORD-1）
            hits.append(Hit(path, no, "HOME-1", "WARN", mask(num), num))

        for m in RE_ROOM.finditer(line):
            if not ROOM_CTX.search(line):
                continue
            hits.append(Hit(path, no, "ROOM-1", "WARN", m.group(0), m.group(0)))


def scan_words(path: str, text: str, words: list[str], hits: list[Hit]) -> None:
    if not words:
        return
    pattern = re.compile("|".join(re.escape(w) for w in words if w))
    for no, line in enumerate(text.splitlines(), 1):
        if EXEMPT_LINE.search(line):
            continue
        for m in pattern.finditer(line):
            # 词表是用户确认过的真实值，命中即拦
            hits.append(Hit(path, no, "WORD-1", "FAIL", mask(m.group(0)), m.group(0)))


def scan_files(files: list[tuple[str, bytes]], words: list[str], max_size: int) -> tuple[list[Hit], int]:
    hits: list[Hit] = []
    skipped = 0
    for path, data in files:
        if path in SELF_FILES:
            skipped += 1
            continue
        ext = Path(path).suffix.lower()
        if ext in IMAGE_EXT:
            if path not in IMAGE_ALLOW:
                hits.append(Hit(path, 0, "IMG-1", "WARN", f"{len(data) // 1024} KB"))
            continue
        if ext in SKIP_EXT or b"\0" in data[:8000]:
            skipped += 1
            continue
        if len(data) > max_size:
            hits.append(Hit(path, 0, "SIZE-1", "WARN", f"{len(data) // 1024} KB"))
        text = data.decode("utf-8", "replace")
        scan_text(path, text, hits)
        scan_words(path, text, words, hits)

    for path, _ in files:
        name = Path(path).name.lower()
        if name in ("auth.json", "pause.yaml") or name.endswith(".cookie"):
            hits.append(Hit(path, 0, "CRED-3", "FAIL", name))
    return hits, skipped


def load_baseline() -> set[str]:
    if not BASELINE.exists():
        return set()
    try:
        return set(json.loads(BASELINE.read_text(encoding="utf-8")).get("keys", []))
    except (OSError, ValueError):
        return set()


def load_words() -> list[str]:
    """读本地词表：``#`` 开头是注释，空行忽略；按长度倒序，长词先匹配（打码才准）。"""
    if not WORDS.exists():
        return []
    try:
        lines = WORDS.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    words = [w.strip() for w in lines if w.strip() and not w.lstrip().startswith("#")]
    return sorted(words, key=len, reverse=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="扫描受版本控制文件里的隐私 / 凭据泄露")
    ap.add_argument("--staged", action="store_true", help="只查暂存区（提交前用）")
    ap.add_argument("--rev", default="", help="查某个提交 / tag 的树")
    ap.add_argument("--json", action="store_true", help="输出 JSON（不含原文）")
    ap.add_argument("--write-baseline", action="store_true", help="把当前命中登记为基线")
    ap.add_argument("--max-size", type=int, default=200 * 1024, help="体积告警阈值（字节）")
    args = ap.parse_args()

    mode = "staged" if args.staged else ("rev" if args.rev else "worktree")
    try:
        files = read_sources(mode, args.rev)
    except RuntimeError as exc:
        print(f"git 读取失败：{exc}")
        return 2

    hits, skipped = scan_files(files, load_words(), args.max_size)
    if args.write_baseline:
        BASELINE.write_text(
            json.dumps({"keys": sorted({h.key for h in hits})}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        print(f"已写基线：{BASELINE.name}（{len(hits)} 条，文件已被 .gitignore 忽略）")
        return 0

    baseline = load_baseline()
    fresh = [h for h in hits if h.key not in baseline]
    failed = [h for h in fresh if h.level == "FAIL"]
    warned = [h for h in fresh if h.level == "WARN"]
    known = len(hits) - len(fresh)

    if args.json:
        print(json.dumps(
            {
                "mode": mode,
                "fail": [{"path": h.path, "line": h.line, "rule": h.rule, "masked": h.masked} for h in failed],
                "warn": [{"path": h.path, "line": h.line, "rule": h.rule, "masked": h.masked} for h in warned],
                "baselined": known,
                "skipped_files": skipped,
            },
            ensure_ascii=False,
            indent=1,
        ))
    else:
        for h in sorted(fresh, key=lambda h: (h.path, h.line, h.rule)):
            print(h.text())
        print()
        print(f"FAIL {len(failed)} / WARN {len(warned)} / 基线内 {known} / 跳过文件 {skipped}")
        if not load_words():
            print("提示：未找到 .privacy-words.txt，真实家庭名 / 设备名规则未启用。")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
