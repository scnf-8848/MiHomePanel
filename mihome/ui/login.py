"""扫码登录。

mijiaAPI 的 ``QRlogin()`` 是**阻塞**的（内部会一直轮询到用户扫码或超时 120 秒），
而且自带一个终端版二维码打印。在 GUI 里不能直接用，所以拆成两步：

1. 主线程调 ``_get_qr_login_data()`` 拿到 ``loginUrl``，自己渲染成图片；
2. 后台线程调 ``_complete_qr_login()`` 阻塞等待，结果通过工作队列回到主线程。

凭据由 mijiaAPI 自己写进 ``~/.config/mijia-api/auth.json``，和 CLI / 参考项目共用，
不需要我们再存一份。
"""

from __future__ import annotations

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout, QWidget

from . import theme
from .chrome import TitleBar, WindowFrame, surface_bg, surface_border



def make_qr_pixmap(text: str, size: int = 220) -> QPixmap | None:
    try:
        import qrcode
        from io import BytesIO

        img = qrcode.make(text, box_size=8, border=1)
        buf = BytesIO()
        img.save(buf, format="PNG")
        pm = QPixmap()
        if not pm.loadFromData(buf.getvalue(), "PNG"):
            return None
        return pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    except Exception:  # noqa: BLE001
        return None


class LoginDialog(QDialog):
    """二维码登录窗口。登录成功后 ``self.auth`` 有值。"""

    succeeded = Signal()

    def __init__(self, api, worker, palette: theme.Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._api = api
        self._worker = worker
        self._p = palette
        self.auth: dict | None = None
        self._state = "正在获取二维码…"
        self.setWindowTitle("登录米家账号")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self._frame = WindowFrame(self, resizable=False,
                                  fill=lambda: surface_bg(palette),
                                  border=lambda: surface_border(palette))
        self.setFixedSize(380, 508)
        self.setStyleSheet("QDialog{background:transparent;}")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.titlebar = TitleBar("登录米家账号", palette, subtitle="扫码",
                                 buttons=("close",), variant="dialog")
        outer.addWidget(self.titlebar)

        content = QWidget()
        col = QVBoxLayout(content)
        col.setContentsMargins(30, 22, 30, 26)
        col.setSpacing(14)
        outer.addWidget(content, 1)

        self.qr_label = QLabel()
        self.qr_label.setFixedSize(240, 240)
        self.qr_label.setAlignment(Qt.AlignCenter)
        self.qr_label.setStyleSheet(f"background:{palette.card}; border-radius:18px;")
        col.addWidget(self.qr_label, 0, Qt.AlignHCenter)

        self.status = QLabel("正在获取二维码…")
        self.status.setAlignment(Qt.AlignCenter)
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color:{palette.text_soft}; font-size:13px;")
        col.addWidget(self.status)

        self.hint = QLabel("打开米家 App → 我的 → 右上角扫码")
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setStyleSheet(f"color:{palette.text_mute}; font-size:12px;")
        col.addWidget(self.hint)

        self._fetch()

    def _fetch(self) -> None:
        def job():
            return self._api._get_qr_login_data()

        def done(result, error):
            if error is not None or not isinstance(result, dict):
                self._set_state("获取二维码失败，检查网络后重试")
                return
            if result.get("refreshed"):
                self.auth = self._api.auth_data
                self.succeeded.emit()
                self.accept()
                return
            url = result.get("loginUrl") or ""
            pm = make_qr_pixmap(url, 216)
            if pm is None:
                self._set_state("二维码渲染失败（缺少 qrcode 库）")
                return
            self.qr_label.setPixmap(pm)
            self._set_state("等待扫码…")
            self._wait(result)

        self._worker.submit(job, priority=0, label="qr-fetch", callback=done)

    def _wait(self, login_data: dict) -> None:
        def job():
            return self._api._complete_qr_login(login_data)

        def done(result, error):
            if error is not None:
                self._set_state(f"登录失败：{error}")
                return
            self.auth = result
            self._set_state("登录成功")
            self.succeeded.emit()
            QTimer.singleShot(400, self.accept)

        self._worker.submit(job, priority=0, label="qr-wait", callback=done)

    def _set_state(self, text: str) -> None:
        self._state = text
        self.status.setText(text)


__all__ = ["LoginDialog", "make_qr_pixmap"]
