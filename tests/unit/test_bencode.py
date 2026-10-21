"""bencode / infohash 解析测试（Phase 0）。

推送链路的核心：``.torrent`` 字节 → infohash。解析错一个字节，
推送到下载器的就是**错误的种子**。这里把边界都钉死。
"""

from __future__ import annotations

import hashlib

import pytest
from app.adapters.sites._bencode import bdecode_info, bencode, infohash_from_torrent


def _torrent_bytes(info: dict[bytes, object]) -> bytes:
    """构造一个最小合法 torrent（只含 info dict）。"""
    return b"d" + bencode(b"info") + bencode(info) + b"e"


def test_bdecode_info_basic() -> None:
    data = _torrent_bytes({b"name": b"file.txt", b"length": 1024})
    info = bdecode_info(data)
    assert info[b"name"] == b"file.txt"
    assert info[b"length"] == 1024


def test_bdecode_info_nested_dict() -> None:
    data = _torrent_bytes({b"files": [{b"length": 1, b"path": [b"a", b"b"]}]})
    info = bdecode_info(data)
    files = info[b"files"]
    assert isinstance(files, list)
    assert files[0][b"path"] == [b"a", b"b"]


def test_bdecode_info_missing_info_dict_raises() -> None:
    with pytest.raises(ValueError, match="missing info dict"):
        bdecode_info(b"d4:name4:teste")


def test_bdecode_info_unterminated_dict_raises_not_hang() -> None:
    """未闭合 dict 必须抛错而非死循环（回归：曾有无 'e' 守卫的死循环）。"""
    with pytest.raises(ValueError, match="unterminated dict"):
        bdecode_info(b"d4:info4:name")


def test_bdecode_info_unterminated_list_raises_not_hang() -> None:
    with pytest.raises(ValueError, match="unterminated list"):
        bdecode_info(b"d4:info4:listli1e")


def test_bdecode_info_truncated_value_raises() -> None:
    """字符串值被截断（声明长度 > 实际剩余）→ 抛错，不是死循环。"""
    with pytest.raises(ValueError):
        bdecode_info(b"d4:info4:name4:abc")


def test_bdecode_info_bad_char_raises() -> None:
    with pytest.raises(ValueError, match="bad bencode char"):
        bdecode_info(b"d4:infoz")


def test_infohash_is_sha1_of_reencoded_info() -> None:
    info = {b"name": b"file.txt", b"length": 1024}
    data = _torrent_bytes(info)
    got = infohash_from_torrent(data)
    assert got is not None
    expected = hashlib.sha1(bencode(info)).hexdigest()
    assert got == expected
    assert len(got) == 40


def test_infohash_lowercase_hex() -> None:
    got = infohash_from_torrent(_torrent_bytes({b"name": b"x"}))
    assert got is not None
    assert got == got.lower()


def test_infohash_none_on_garbage() -> None:
    assert infohash_from_torrent(b"not a torrent at all") is None
    assert infohash_from_torrent(b"") is None


def test_infohash_none_on_truncated() -> None:
    assert infohash_from_torrent(b"d4:info4:name") is None


def test_infohash_none_on_missing_info() -> None:
    assert infohash_from_torrent(b"d4:name4:teste") is None


def test_bencode_roundtrip_dict() -> None:
    obj = {b"b": 2, b"a": 1}
    encoded = bencode(obj)
    # dict key 必须排序（bencode 规范）
    assert encoded == b"d1:ai1e1:bi2ee"


def test_bencode_types() -> None:
    assert bencode(42) == b"i42e"
    assert bencode(b"abc") == b"3:abc"
    assert bencode("abc") == b"3:abc"
    assert bencode([1, 2]) == b"li1ei2ee"


def test_bencode_bad_type_raises() -> None:
    with pytest.raises(ValueError, match="bad bencode type"):
        bencode(object())


def test_bencode_empty_containers() -> None:
    assert bencode({}) == b"de"
    assert bencode([]) == b"le"
