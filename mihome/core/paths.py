"""统一的目录约定。

所有落盘数据集中在一处，方便排障与清理：
  <data>/auth.json         米家账号凭据（由登录流程写入）
  <data>/prefs.json        用户偏好：家庭/房间/设备/场景顺序、隐藏设备
  <data>/devices/          设备档案：型号 / 规格 / 固件的当前值与变更流（不可重建）
  <data>/cache/spec/       MIoT-Spec 原始规格缓存（可按 urn 重新拉取）
  <data>/cache/icons/      设备图标
  <data>/logs/             运行日志
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "MiHomePanel"


def data_dir() -> Path:
    """数据根目录。可用 MIHOME_DATA_DIR 覆盖（测试 / 便携模式）。"""
    override = os.environ.get("MIHOME_DATA_DIR")
    if override:
        p = Path(override).expanduser()
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        p = Path(base) / APP_NAME
    elif sys.platform == "darwin":
        p = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
        p = Path(base) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir() -> Path:
    p = data_dir() / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def spec_cache_dir() -> Path:
    p = cache_dir() / "spec"
    p.mkdir(parents=True, exist_ok=True)
    return p


def icon_cache_dir() -> Path:
    p = cache_dir() / "icons"
    p.mkdir(parents=True, exist_ok=True)
    return p


def devices_dir() -> Path:
    """设备档案目录（``catalog.json`` + ``changes.jsonl``）。

    放 ``data/`` 而不是 ``cache/``，这一条是**语义**不是习惯：``cache/`` 里的东西
    随时可以删、删了只是下次重新拉（规格正文可按 urn 重取、图标可重下、缩略图可
    重截）；而"这台设备半年是什么固件"云端只给现在，**删了就永远没了**。所以它
    和 ``prefs.json`` 同级，属于不可重建的用户数据。
    """
    p = data_dir() / "devices"
    p.mkdir(parents=True, exist_ok=True)
    return p


def log_dir() -> Path:
    p = data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def auth_file() -> Path:
    return data_dir() / "auth.json"


def thumb_cache_dir() -> Path:
    """监控页左侧列表的缩略图（播过的那一帧，模糊后存 JPEG）。

    放 ``cache/`` 而不是 ``data/``：纯缓存，随时可删，删了只是回到图标占位。
    """
    p = cache_dir() / "thumbs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def legacy_mijia_cache_dir() -> Path:
    """mijiaAPI 的缓存目录，仅作离线兜底读取。"""
    return Path.home() / ".config" / "mijia-api"
