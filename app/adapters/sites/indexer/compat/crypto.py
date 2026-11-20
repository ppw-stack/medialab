"""摘要算法工具（移植自 MoviePilot ``app.foundation.crypto`` 的 HashUtils）。"""

from __future__ import annotations

import hashlib
from typing import Union


class HashUtils:
    """常用摘要算法。"""

    @staticmethod
    def md5(data: Union[str, bytes], encoding: str = "utf-8") -> str:
        if isinstance(data, str):
            data = data.encode(encoding)
        return hashlib.md5(data).hexdigest()

    @staticmethod
    def sha1(data: Union[str, bytes], encoding: str = "utf-8") -> str:
        if isinstance(data, str):
            data = data.encode(encoding)
        return hashlib.sha1(data).hexdigest()

    @staticmethod
    def sha256(data: Union[str, bytes], encoding: str = "utf-8") -> str:
        if isinstance(data, str):
            data = data.encode(encoding)
        return hashlib.sha256(data).hexdigest()
