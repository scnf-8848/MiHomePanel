# 测试与验证

> 专题文档，入口见 [DEVELOPMENT.md](../DEVELOPMENT.md)。
> 本文覆盖：离屏验证脚本、真机端到端脚本、截图脚本各自的用途与运行方式。

## 验证与截图脚本

所有 `scripts/verify_*.py` 都是**离线**的（假 Client 或离屏 Qt，不碰真设备、不连网），
改完代码先跑一遍。统一用法：

```bash
set PYTHONPATH=.
.venv\Scripts\python.exe -X utf8 scripts\verify_card_style.py
```

`verify_api.py` / `verify_store.py` / `verify_bindings.py` 是例外，它们要真账号：

- `verify_api.py` 只读，验证读取链路与错误码分类
- `verify_bindings.py` 回读 siid/piid 是否真有效
- `verify_store.py --write` 会**真的开关一次灯**，别无脑跑

截图脚本（`shot*.py`）会临时改主题 / 家庭，跑完自动还原偏好；`shot_ui.py` 一次出 34 张。
`-X utf8` 不能省：脚本里有中文路径与中文输出，Windows 默认编码会炸。
