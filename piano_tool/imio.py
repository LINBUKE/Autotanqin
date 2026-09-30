"""Unicode 安全的图像读写。

Windows 上 cv2.imread / cv2.imwrite 走 ANSI 文件接口，
路径含中文（如本项目目录「原神琴」）时会静默失败。
改用 np.fromfile + cv2.imdecode / cv2.imencode + tofile 即可支持任意路径。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np


def imread_unicode(path):
    """cv2.imread 的 Unicode 安全替代。失败返回 None。"""
    import cv2

    p = Path(path)
    if not p.exists():
        return None
    data = np.fromfile(str(p), dtype=np.uint8)
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def imwrite_unicode(path, img) -> bool:
    """cv2.imwrite 的 Unicode 安全替代。返回是否成功。"""
    import cv2

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ext = p.suffix or ".png"
    ok, buf = cv2.imencode(ext, img)
    if not ok:
        return False
    buf.tofile(str(p))
    return True
