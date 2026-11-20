"""摘要算法工具（移植自 MoviePilot ``app.foundation.crypto`` 的 HashUtils）。"""

from __future__ import annotations

import hashlib
from typing import Union


class HashUtils:
    """常用摘要算法。"""

    @staticmethod
    def md5(data: Union[str, bytes], encoding: str = "utf-8") -> str:
        """计算 MD5 十六进制摘要。

        :param data: 待摘要的文本或字节；文本按 ``encoding`` 编码
        :param encoding: 文本编码，默认 utf-8
        :return: 32 位小写十六进制摘要
        """
        if isinstance(data, str):
            data = data.encode(encoding)
        return hashlib.md5(data).hexdigest()

    @staticmethod
    def sha1(data: Union[str, bytes], encoding: str = "utf-8") -> str:
        """计算 SHA1 十六进制摘要。

        :param data: 待摘要的文本或字节；文本按 ``encoding`` 编码
        :param encoding: 文本编码，默认 utf-8
        :return: 40 位小写十六进制摘要
        """
        if isinstance(data, str):
            data = data.encode(encoding)
        return hashlib.sha1(data).hexdigest()

    @staticmethod
    def sha256(data: Union[str, bytes], encoding: str = "utf-8") -> str:
        """计算 SHA256 十六进制摘要。

        :param data: 待摘要的文本或字节；文本按 ``encoding`` 编码
        :param encoding: 文本编码，默认 utf-8
        :return: 64 位小写十六进制摘要
        """
        if isinstance(data, str):
            data = data.encode(encoding)
        return hashlib.sha256(data).hexdigest()
