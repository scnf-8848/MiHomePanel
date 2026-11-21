# SPDX-License-Identifier: GPL-3.0-or-later
"""验证单实例守卫（`mihome/core/single_instance.py`）。

跑法：``.venv/Scripts/python.exe -X utf8 scripts/verify_single_instance.py``

用一个**真子进程**跑，不打桩——互斥量是内核对象，假不了：

1. 空着的时候能拿到令牌；
2. 子进程占着同一份数据目录时，第二个"实例"拿不到；
3. 第二个实例广播之后，子进程的窗口真的收到消息并 emit `activated`
   （这是"再点一次图标把窗口叫到前台"的全部机制）；
4. 子进程一退出，令牌立刻可以再拿（内核对象随进程消失，不会像文件锁那样留死锁）；
5. 换一份数据目录就是另一个实例（便携版 / 第二个账号各跑一个是合理的）。

子进程**不显示窗口**，只建一个原生句柄：`HWND_BROADCAST` 要发到真正的
顶层窗口，离屏平台（offscreen）没有 HWND，测不出东西。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAILS.append(f"{name} {detail}")


# -- 子进程：占住令牌 + 等广播 ------------------------------------------------
def run_child(data_dir: str, log_path: str) -> int:
    """占住令牌，建一个隐藏的原生窗口，等广播来了写一行日志再退出。"""
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication, QWidget

    from mihome.core.single_instance import SingleInstance

    log = Path(log_path)

    def say(text: str) -> None:
        with log.open("a", encoding="utf-8") as fp:
            fp.write(text + "\n")

    app = QApplication(sys.argv[:1])
    guard = SingleInstance(Path(data_dir))
    if not guard.acquire():
        say("BUSY")
        return 2
    # 不 show 也要真 HWND：广播只发给顶层窗口，没有句柄就收不到
    win = QWidget()
    win.setAttribute(Qt.WA_NativeWindow, True)
    win.createWinId()
    guard.activated.connect(lambda: (say("ACTIVATED"), app.quit()))
    say("READY")
    if not guard._filter:  # noqa: SLF001 - 测试要看机制有没有装上
        say("NO-FILTER")
    QTimer.singleShot(20000, app.quit)  # 兜底：谁也不来激活就自己退
    return app.exec()


def wait_for(log: Path, word: str, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if log.is_file() and word in log.read_text(encoding="utf-8").split():
            return True
        time.sleep(0.05)
    return False


# -- 父进程：整个流程 --------------------------------------------------------
def main() -> int:
    from mihome.core.single_instance import SingleInstance, instance_suffix

    if "--child" in sys.argv:
        i = sys.argv.index("--child")
        return run_child(sys.argv[i + 1], sys.argv[i + 2])

    if sys.platform != "win32":
        print("跳过：单实例守卫只有 Windows 实现")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="mihome-single-"))
    data = tmp / "data"
    data.mkdir()
    data2 = tmp / "data2"
    data2.mkdir()
    log = tmp / "child.log"

    check("同一个数据目录 → 同一个名字", instance_suffix(data) == instance_suffix(data))
    check("不同数据目录 → 不同名字", instance_suffix(data) != instance_suffix(data2),
          f"{instance_suffix(data)} vs {instance_suffix(data2)}")

    empty = SingleInstance(data)
    check("空着的时候能拿到令牌", empty.acquire() is True, empty.mutex_name)
    empty.release()
    check("release 之后处于未持有状态", empty.held is False)

    # 这一条专抓 CloseHandle 签名没设对：句柄被 32 位截断时释放会静默失败，
    # 本进程还占着名字，表现是"关掉程序再打开，说已经有一个实例"
    again0 = SingleInstance(data)
    check("自己 release 之后能立刻再拿（句柄真的关掉了）", again0.acquire() is True)
    again0.release()

    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(Path(__file__)), "--child", str(data), str(log)],
        cwd=str(ROOT),
        env={**os.environ, "MIHOME_DATA_DIR": str(data)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        if not wait_for(log, "READY"):
            err = b""
            if proc.poll() is not None and proc.stderr is not None:
                err = proc.stderr.read()[-800:]
            check("子进程拿到令牌并建出窗口", False, err.decode("utf-8", "replace"))
            return 1
        check("子进程拿到令牌并建出窗口", "NO-FILTER" not in log.read_text(encoding="utf-8"))

        second = SingleInstance(data)
        check("第二个实例拿不到令牌", second.acquire() is False)

        check("广播发得出去", second.notify_existing() is True)
        check("子进程窗口收到广播并 emit activated", wait_for(log, "ACTIVATED", 5.0))
        second.release()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=8)
        if proc.stderr is not None:
            proc.stderr.close()

    again = SingleInstance(data)
    check("子进程退出后令牌立刻能再拿（不留死锁）", again.acquire() is True)
    other = SingleInstance(data2)
    check("另一份数据目录同时可用", other.acquire() is True)
    other.release()
    again.release()

    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：")
        for line in FAILS:
            print("  -", line)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
