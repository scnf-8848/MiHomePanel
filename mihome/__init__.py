"""MiHomePanel — 桌面端米家控制面板。"""

from ._stdio import ensure_streams

# 必须在任何子模块之前：导入链里就有库在模块级碰 sys.stdout（见 _stdio.py）
ensure_streams()

__version__ = "0.1.0"
