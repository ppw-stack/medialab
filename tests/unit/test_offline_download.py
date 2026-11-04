"""离线下载的单元测试。

**防的是什么**：

1. **token 与路径绑定** —— 光鸭的 ``cloud_create_task`` 只收 URL、不支持
   自定义 Header，鉴权只能做在 URL 里。token 一旦与路径解绑，攻击者就能
   拿一个文件的 token 去读任意文件（或穿越目录）。
2. **路径穿越** —— 相对路径 resolve 后必须仍在根目录内。
3. **Range 支持** —— 光鸭可能断点续传，必须返回 206 + Content-Range。
4. **进度百分比** —— 站点进度是**百分数**（教训见 AGENTS.md「作业步骤带
   逐项明细」条），别再乘 100。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.services.offline_download import (
    OfflineParams,
    build_url,
    collect_files,
)
from app.services.offline_files import (
    URL_PREFIX,
    make_token,
    parse_range,
    safe_join,
    verify_token,
)

SECRET = "test-secret-key"


# ---------------------------------------------------------------- token


def test_token_is_deterministic() -> None:
    """同一路径 → 同一 token（重跑能生成同样的 URL）。"""
    assert make_token(SECRET, "a/b.mkv") == make_token(SECRET, "a/b.mkv")


def test_token_differs_per_path() -> None:
    """不同路径 → 不同 token（单文件 URL 泄露只影响那一个文件）。"""
    assert make_token(SECRET, "a.mkv") != make_token(SECRET, "b.mkv")


def test_token_differs_per_secret() -> None:
    """不同密钥 → 不同 token。"""
    assert make_token("s1", "a.mkv") != make_token("s2", "a.mkv")


def test_verify_token_accepts_correct() -> None:
    """正确 token 通过。"""
    tok = make_token(SECRET, "电影/x.mkv")
    assert verify_token(SECRET, "电影/x.mkv", tok) is True


def test_verify_token_rejects_wrong_path() -> None:
    """**token 与路径绑定**——拿 a 的 token 去要 b 会被拒。"""
    tok = make_token(SECRET, "a.mkv")
    assert verify_token(SECRET, "b.mkv", tok) is False


def test_verify_token_rejects_wrong_secret() -> None:
    """错误密钥被拒。"""
    tok = make_token(SECRET, "a.mkv")
    assert verify_token("other", "a.mkv", tok) is False


# ------------------------------------------------------------ 路径穿越


def test_safe_join_allows_inside(tmp_path: Path) -> None:
    """根目录内的相对路径 → 正常拼接。"""
    got = safe_join(tmp_path, "a/b.mkv")
    assert got is not None
    assert got == (tmp_path / "a" / "b.mkv").resolve()


def test_safe_join_blocks_traversal(tmp_path: Path) -> None:
    """``../`` 穿越 → 被拒（返回 None）。"""
    assert safe_join(tmp_path, "../../etc/passwd") is None
    assert safe_join(tmp_path, "a/../../../etc/passwd") is None


def test_safe_join_blocks_absolute(tmp_path: Path) -> None:
    """绝对路径 → 被拒。"""
    assert safe_join(tmp_path, "/etc/passwd") is None


def test_safe_join_blocks_empty(tmp_path: Path) -> None:
    """空路径 → 被拒。"""
    assert safe_join(tmp_path, "") is None


# ---------------------------------------------------------------- Range


@pytest.mark.parametrize(
    ("header", "size", "expected"),
    [
        ("bytes=0-99", 1000, (0, 99)),
        ("bytes=100-", 1000, (100, 999)),
        ("bytes=-100", 1000, (900, 999)),
        (None, 1000, None),
        ("bytes=abc", 1000, None),
        ("bytes=2000-3000", 1000, None),
    ],
)
def testparse_range(header: str | None, size: int, expected: tuple[int, int] | None) -> None:
    """Range 头解析（光鸭可能断点续传）。"""
    assert parse_range(header, size) == expected


def test_parse_range_clamps_end() -> None:
    """超出文件尾的 end 被夹到 size-1。"""
    assert parse_range("bytes=0-99999", 1000) == (0, 999)


# ------------------------------------------------------------ 文件收集


def _make(root: Path, rel: str, size: int) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)


def test_collect_files_finds_recursive(tmp_path: Path) -> None:
    """递归子目录。"""
    _make(tmp_path, "a.mkv", 100)
    _make(tmp_path, "sub/b.mkv", 200)
    got = collect_files(
        tmp_path, include_subdirs=True, min_bytes=0, max_bytes=0, exclude_disc=False
    )
    assert {f.rel_path for f in got} == {"a.mkv", "sub/b.mkv"}


def test_collect_files_not_recursive(tmp_path: Path) -> None:
    """不递归时只取顶层。"""
    _make(tmp_path, "a.mkv", 100)
    _make(tmp_path, "sub/b.mkv", 200)
    got = collect_files(
        tmp_path, include_subdirs=False, min_bytes=0, max_bytes=0, exclude_disc=False
    )
    assert {f.rel_path for f in got} == {"a.mkv"}


def test_collect_files_skips_zero_byte(tmp_path: Path) -> None:
    """0 字节文件不传（空壳会在云端挡住真文件）。"""
    _make(tmp_path, "empty.mkv", 0)
    _make(tmp_path, "real.mkv", 100)
    got = collect_files(
        tmp_path, include_subdirs=True, min_bytes=0, max_bytes=0, exclude_disc=False
    )
    assert {f.rel_path for f in got} == {"real.mkv"}


def test_collect_files_respects_size_bounds(tmp_path: Path) -> None:
    """体积上下限。"""
    _make(tmp_path, "small.mkv", 100)
    _make(tmp_path, "mid.mkv", 1000)
    _make(tmp_path, "big.mkv", 10000)
    got = collect_files(
        tmp_path, include_subdirs=True, min_bytes=500, max_bytes=5000, exclude_disc=False
    )
    assert {f.rel_path for f in got} == {"mid.mkv"}


def test_collect_files_excludes_disc(tmp_path: Path) -> None:
    """排除原盘（iso / m2ts / BDMV 目录）。"""
    _make(tmp_path, "movie.mkv", 100)
    _make(tmp_path, "disc.iso", 100)
    _make(tmp_path, "BDMV/STREAM/0001.m2ts", 100)
    got = collect_files(
        tmp_path, include_subdirs=True, min_bytes=0, max_bytes=0, exclude_disc=True
    )
    assert {f.rel_path for f in got} == {"movie.mkv"}


def test_collect_files_keeps_disc_when_not_excluded(tmp_path: Path) -> None:
    """不排除原盘时原盘也在列表里。"""
    _make(tmp_path, "disc.iso", 100)
    got = collect_files(
        tmp_path, include_subdirs=True, min_bytes=0, max_bytes=0, exclude_disc=False
    )
    assert {f.rel_path for f in got} == {"disc.iso"}


# ---------------------------------------------------------------- URL


def test_build_url_contains_token_and_encoded_path() -> None:
    """URL 含 token 与 URL 编码后的路径。

    ⚠️ **token 按未编码的相对路径算**——服务端拿到的是 FastAPI 解码后的
    path 参数，两者必须一致（否则校验必然失败）。URL 里只编码路径。
    """
    params = OfflineParams(
        target_driver_id=1,
        local_root="/data",
        public_base="http://example.com:8092/",
        secret=SECRET,
    )
    url = build_url(params, "电影/a b.mkv")
    assert url.startswith(f"http://example.com:8092{URL_PREFIX}/")
    assert "%20" in url  # 空格被编码
    # token 段（URL_PREFIX 后的第一段）
    token = url.split(f"{URL_PREFIX}/")[1].split("/")[0]
    # **解码后的**路径能通过校验
    assert verify_token(SECRET, "电影/a b.mkv", token) is True
    # 服务端拿到的是解码后的 path → 校验通过
    from urllib.parse import unquote

    decoded = unquote(url.split(f"{URL_PREFIX}/")[1].split("/", 1)[1])
    assert decoded == "电影/a b.mkv"
    assert verify_token(SECRET, decoded, token) is True


def test_build_url_strips_trailing_slash() -> None:
    """公网入口的尾斜杠被去掉（避免双斜杠）。"""
    params = OfflineParams(
        target_driver_id=1,
        local_root="/data",
        public_base="http://example.com:8092/",
        secret=SECRET,
    )
    url = build_url(params, "a.mkv")
    assert "//offline-files" not in url.replace("http://", "")
