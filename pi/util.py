"""小工具：配置读取、JPEG 编码、HTTP 打点。"""

from __future__ import annotations

import io
import logging
import threading
import time
import urllib.request

import numpy as np
from PIL import Image

log = logging.getLogger("vision")


# --------------------------------------------------------------------------- 配置
def cget(tree, path: str, default=None):
    """按 "a.b.c" 取值，缺失则返回 default。"""
    cur = tree
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


# --------------------------------------------------------------------------- 图像
def encode_jpeg(rgb: np.ndarray, quality: int = 78) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(rgb)).save(
        buf, format="JPEG", quality=int(quality), optimize=False, subsampling=2
    )
    return buf.getvalue()


def yuv420_to_jpeg(yuv: np.ndarray, w: int, h: int, quality: int = 85) -> bytes:
    """把 libcamera 的 I420 平面数据直接封成 JPEG。

    关键：不做 YUV→RGB 矩阵转换，而是交给 PIL 的 "YCbCr" 模式——
    JPEG 内部本来就是 YCbCr，这一步等于"原样搬运"，色彩与
    libcamera 自带编码器一致，也绕开了 RGB888 输出路径的偏色问题。

    yuv 形状为 (h*3//2, w) 的 uint8：前 h 行 Y，接着 h/4 行 U，再 h/4 行 V。
    """
    y = yuv[:h]
    u = yuv[h:h + h // 4].reshape(h // 2, w // 2)
    v = yuv[h + h // 4:h + h // 2].reshape(h // 2, w // 2)
    # 色度 2x 上采样（最近邻足够：JPEG 保存时还会再降采样）
    u2 = np.repeat(np.repeat(u, 2, axis=0), 2, axis=1)[:h, :w]
    v2 = np.repeat(np.repeat(v, 2, axis=0), 2, axis=1)[:h, :w]

    packed = np.empty((h, w, 3), dtype=np.uint8)
    packed[..., 0] = y
    packed[..., 1] = u2
    packed[..., 2] = v2

    buf = io.BytesIO()
    Image.frombytes("YCbCr", (w, h), packed.tobytes()).save(
        buf, format="JPEG", quality=int(quality), subsampling=2
    )
    return buf.getvalue()


def fit_size(w: int, h: int, max_w: int, max_h: int) -> tuple[int, int]:
    """等比缩放，返回 (w, h)，偶数化（很多编码器要求）。"""
    scale = min(max_w / w, max_h / h, 1.0)
    nw, nh = int(w * scale), int(h * scale)
    return (nw - nw % 2, nh - nh % 2)


# --------------------------------------------------------------------------- 杂项
def stamp(ts: float | None = None) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(ts))


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def pulse_url(url: str, timeout: float = 3.0) -> None:
    """异步 GET 一个 URL（用于叫 ESP32 开补光灯），失败只记日志。"""

    def _run():
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                r.read(64)
        except Exception as exc:  # noqa: BLE001
            log.warning("pulse_url %s 失败: %s", url, exc)

    threading.Thread(target=_run, daemon=True, name="pulse").start()
