"""设备产品图缓存。

米家 App 的卡片右上角是**产品本体渲染图**（168×168 透明底正视图），不是通用
线框图标——这是"像不像原版"最直观的差别。这张图只能从私有接口拿：

    POST /v2/productconfig/get_icon   {"icon_name":"icon_real","model":"<model>"}

它返回 **302**，真正的图片地址在 ``Location`` 头里（带签名的 CDN 链接，会过期）。
所以不能走 mijiaAPI 的 ``_request``（它默认跟随重定向并解析 JSON），必须自己
拼签名、关闭重定向、读 header。

按 model 缓存到磁盘，同一型号只下载一次。下载走工作队列的 IDLE 优先级，
不阻塞首屏。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable

from . import paths

ICON_URI = "/v2/productconfig/get_icon"


class IconCache:
    """model → 本地 png 路径。"""

    def __init__(self, api: object, worker: object | None = None) -> None:
        self._api = api
        self._worker = worker
        self._dir = paths.icon_cache_dir()
        self._lock = threading.Lock()
        self._inflight: set[str] = set()
        self._urls: dict[str, str] = {}
        self._load_index()

    # -- 索引 ------------------------------------------------------------
    def _index_path(self) -> Path:
        return self._dir / "index.json"

    def _load_index(self) -> None:
        try:
            raw = json.loads(self._index_path().read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._urls = {k: v for k, v in raw.items() if isinstance(v, str)}
        except (OSError, ValueError):
            pass

    def _save_index(self) -> None:
        try:
            self._index_path().write_text(
                json.dumps(self._urls, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    # -- 查询 ------------------------------------------------------------
    def path_for(self, model: str) -> Path | None:
        if not model:
            return None
        p = self._dir / f"{_safe(model)}.png"
        return p if p.exists() else None

    # -- 拉取 ------------------------------------------------------------
    def request(self, model: str, callback: Callable[[str, Path | None], None]) -> None:
        """确保 model 的图标可用。命中缓存立刻回调，否则进队列下载。"""
        if not model:
            callback(model, None)
            return
        hit = self.path_for(model)
        if hit is not None:
            callback(model, hit)
            return
        with self._lock:
            if model in self._inflight:
                return
            self._inflight.add(model)

        def job() -> Path | None:
            return self._download(model)

        def done(result: object, error: BaseException | None) -> None:
            with self._lock:
                self._inflight.discard(model)
            path = result if isinstance(result, Path) else None
            callback(model, path)

        if self._worker is None:
            callback(model, job())
            return
        self._worker.submit(job, label=f"icon:{model}", callback=done, priority=2)

    def _download(self, model: str) -> Path | None:
        api = self._api
        try:
            from mijiaAPI.miutils import generate_enc_params, gen_nonce, get_signed_nonce

            api._refresh_token()  # type: ignore[attr-defined]
            auth = api.auth_data  # type: ignore[attr-defined]
            data = {"data": json.dumps({"icon_name": "icon_real", "model": model}, separators=(",", ":"))}
            nonce = gen_nonce()
            signed = get_signed_nonce(auth["ssecurity"], nonce)
            params = generate_enc_params(ICON_URI, "POST", signed, nonce, data, auth["ssecurity"])
            resp = api.session.post(  # type: ignore[attr-defined]
                api.api_base_url + ICON_URI,  # type: ignore[attr-defined]
                data=params,
                allow_redirects=False,
                timeout=20,
            )
            if resp.status_code != 302:
                return None
            url = resp.headers.get("Location")
            if not url:
                return None
        except Exception:  # noqa: BLE001
            return None

        try:
            import requests

            img = requests.get(url, timeout=20)
            if img.status_code != 200 or not img.content:
                return None
            path = self._dir / f"{_safe(model)}.png"
            path.write_bytes(img.content)
        except Exception:  # noqa: BLE001
            return None

        with self._lock:
            self._urls[model] = url.split("?")[0]
            self._save_index()
        return path


def _safe(model: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in model)


__all__ = ["IconCache"]
