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


# ------------------------------------------------------------ 扩展名过滤


def test_collect_files_filters_by_extension(tmp_path: Path) -> None:
    """只要指定扩展名（跳过 nfo / 字幕 / 海报）。"""
    _make(tmp_path, "movie.mkv", 100)
    _make(tmp_path, "clip.mp4", 100)
    _make(tmp_path, "movie.nfo", 10)
    _make(tmp_path, "sub.srt", 10)
    _make(tmp_path, "poster.jpg", 10)
    got = collect_files(
        tmp_path,
        include_subdirs=True,
        min_bytes=0,
        max_bytes=0,
        exclude_disc=False,
        exts={"mkv", "mp4"},
    )
    assert {f.rel_path for f in got} == {"movie.mkv", "clip.mp4"}


def test_collect_files_empty_exts_means_all(tmp_path: Path) -> None:
    """空集合 = 不限（全传）。"""
    _make(tmp_path, "movie.mkv", 100)
    _make(tmp_path, "movie.nfo", 10)
    got = collect_files(
        tmp_path,
        include_subdirs=True,
        min_bytes=0,
        max_bytes=0,
        exclude_disc=False,
        exts=set(),
    )
    assert {f.rel_path for f in got} == {"movie.mkv", "movie.nfo"}


def test_collect_files_ext_filter_is_case_insensitive(tmp_path: Path) -> None:
    """扩展名**大小写不敏感**（``.MKV`` 也算 ``mkv``）。"""
    _make(tmp_path, "Movie.MKV", 100)
    _make(tmp_path, "other.mp4", 100)
    got = collect_files(
        tmp_path,
        include_subdirs=True,
        min_bytes=0,
        max_bytes=0,
        exclude_disc=False,
        exts={"mkv"},
    )
    assert {f.rel_path for f in got} == {"Movie.MKV"}


def test_collect_files_ext_filter_handles_no_extension(tmp_path: Path) -> None:
    """无扩展名的文件被过滤掉（不匹配任何 ext）。"""
    _make(tmp_path, "README", 100)
    _make(tmp_path, "movie.mkv", 100)
    got = collect_files(
        tmp_path,
        include_subdirs=True,
        min_bytes=0,
        max_bytes=0,
        exclude_disc=False,
        exts={"mkv"},
    )
    assert {f.rel_path for f in got} == {"movie.mkv"}


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


# ---------------------------------------------------------------- 流式扫描


def test_iter_files_matches_collect_files(tmp_path: Path) -> None:
    """流式版与全量版产出**同一集合**（重构防回归）。"""
    import app.services.offline_download as mod

    (tmp_path / "a.mkv").write_bytes(b"x" * 10)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.mp4").write_bytes(b"y" * 20)
    (tmp_path / "sub" / "c.nfo").write_bytes(b"z" * 5)
    (tmp_path / "BDMV").mkdir()
    (tmp_path / "BDMV" / "x.m2ts").write_bytes(b"w" * 30)

    kw = {"include_subdirs": True, "min_bytes": 0, "max_bytes": 0, "exclude_disc": True}
    from_iter = sorted(f.rel_path for f in mod.iter_files(tmp_path, **kw))  # type: ignore[arg-type]
    from_collect = [f.rel_path for f in mod.collect_files(tmp_path, **kw)]  # type: ignore[arg-type]
    # 未传 exts → 全传（nfo 也在内）；BDMV 被排除
    assert from_iter == from_collect == ["a.mkv", "sub/b.mp4", "sub/c.nfo"]


def test_iter_files_is_lazy() -> None:
    """``iter_files`` 返回**生成器**——没消费就不扫盘（流水线的前提）。"""
    import inspect

    import app.services.offline_download as mod

    assert inspect.isgeneratorfunction(mod.iter_files)


# ---------------------------------------------------------------- 执行流水线


class _FakeDriver:
    """最小光鸭驱动桩：提交后目标目录立刻出现同名同大小文件（模拟下载完成）。"""

    def __init__(self, sizes: dict[str, int]) -> None:
        self.sizes = sizes  # 文件名 → 字节数（本地文件的真实大小）
        self.files: dict[str, int] = {}  # 目标目录当前内容
        self.submitted: list[str] = []

    async def list_files(self, file_id: str) -> dict[str, int]:
        return dict(self.files)

    async def create_offline_task(self, url: str, file_id: str) -> str:
        from urllib.parse import unquote

        self.submitted.append(url)
        # 「任务结束 → 目标目录出现同名同大小文件」
        name = unquote(url.rsplit("/", 1)[-1])
        self.files[name] = self.sizes[name]
        return f"task-{len(self.submitted)}"

    async def list_active_offline_tasks(self) -> list[dict[str, object]]:
        return []  # 任务立即从活跃列表消失 → watcher 放行

    async def list_failed_offline_tasks(self) -> list[dict[str, object]]:
        return []


@pytest.mark.asyncio
async def test_run_offline_download_pipeline_succeeds(tmp_path: Path) -> None:
    """扫描→提交流水线跑通：文件全部提交、校验落盘、计数正确。"""
    import app.services.offline_download as mod

    (tmp_path / "a.mkv").write_bytes(b"x" * 1000)
    (tmp_path / "b.mp4").write_bytes(b"y" * 2000)
    (tmp_path / "c.nfo").write_bytes(b"z" * 5)  # 不在 exts → 不传

    driver = _FakeDriver(sizes={"a.mkv": 1000, "b.mp4": 2000})

    async def fake_load(driver_id: int) -> _FakeDriver:
        return driver

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mod, "_load_target_driver", fake_load)
    try:
        params = OfflineParams(
            target_driver_id=1,
            local_root=str(tmp_path),
            exts=("mkv", "mp4"),
            concurrency=2,
            submit_interval_sec=0,
            public_base="http://example.test:8092",
            secret=SECRET,
        )
        job = mod.OfflineJob(id="t1", params=params, seq=1)
        await mod.run_offline_download(job)
    finally:
        monkeypatch.undo()

    assert job.state == "success"
    assert job.discovered == 2
    assert job.success == 2
    assert job.failed == 0
    assert len(driver.submitted) == 2
    # URL 里的路径经过编码且带 token（两个文件各一个）
    assert len({u.split("/")[-2] for u in driver.submitted}) == 2


@pytest.mark.asyncio
async def test_run_offline_download_empty_dir_succeeds(tmp_path: Path) -> None:
    """空目录 → success + 「没有需要传的文件」（流水线零文件路径不挂死）。"""
    import app.services.offline_download as mod

    driver = _FakeDriver(sizes={})

    async def fake_load(driver_id: int) -> _FakeDriver:
        return driver

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mod, "_load_target_driver", fake_load)
    try:
        params = OfflineParams(
            target_driver_id=1,
            local_root=str(tmp_path),
            public_base="http://example.test:8092",
            secret=SECRET,
        )
        job = mod.OfflineJob(id="t2", params=params, seq=1)
        await mod.run_offline_download(job)
    finally:
        monkeypatch.undo()

    assert job.state == "success"
    assert job.discovered == 0
    assert any("没有需要传的文件" in line for line in job.logs)
