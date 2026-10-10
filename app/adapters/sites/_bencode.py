"""bencode 编解码共享工具。

从 m-team 适配器提取，供所有需要解析 .torrent 文件提取 infohash 的站点适配器复用。
"""

from __future__ import annotations

import hashlib
from typing import Any


def bdecode_info(data: bytes) -> dict[bytes, Any]:
    """解析 torrent 文件的 info dict。"""
    def _decode(idx: int) -> tuple[Any, int]:
        c = data[idx:idx + 1]
        if c == b'd':
            idx += 1
            d: dict[bytes, Any] = {}
            while data[idx:idx + 1] != b'e':
                k, idx = _decode(idx)
                v, idx = _decode(idx)
                d[k] = v
            return d, idx + 1
        if c == b'l':
            idx += 1
            lst: list[Any] = []
            while data[idx:idx + 1] != b'e':
                v, idx = _decode(idx)
                lst.append(v)
            return lst, idx + 1
        if c == b'i':
            idx += 1
            end = data.index(b'e', idx)
            return int(data[idx:end]), end + 1
        if c in b'0123456789':
            end = data.index(b':', idx)
            n = int(data[idx:end])
            return data[end + 1:end + 1 + n], end + 1 + n
        raise ValueError(f"bad bencode char {c!r} at {idx}")

    root, _ = _decode(0)
    if not isinstance(root, dict) or b'info' not in root:
        raise ValueError("missing info dict")
    return root[b'info']  # type: ignore[no-any-return]


def bencode(obj: Any) -> bytes:
    """把 Python 对象重新编码为 bencode bytes。"""
    if isinstance(obj, int):
        return b'i' + str(obj).encode() + b'e'
    if isinstance(obj, bytes):
        return str(len(obj)).encode() + b':' + obj
    if isinstance(obj, str):
        return bencode(obj.encode())
    if isinstance(obj, dict):
        items = sorted(obj.items(), key=lambda x: x[0])
        return b'd' + b''.join(bencode(k) + bencode(v) for k, v in items) + b'e'
    if isinstance(obj, list):
        return b'l' + b''.join(bencode(v) for v in obj) + b'e'
    raise ValueError(f"bad bencode type {type(obj)}")


def infohash_from_torrent(torrent_bytes: bytes) -> str | None:
    """从 torrent 文件解析 infohash（info dict 的 bencoded SHA1）。"""
    try:
        info = bdecode_info(torrent_bytes)
    except (ValueError, IndexError):
        return None
    encoded = bencode(info)
    return hashlib.sha1(encoded).hexdigest()


__all__ = ["bdecode_info", "bencode", "infohash_from_torrent"]
