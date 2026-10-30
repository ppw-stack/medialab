"""扫描生成 STRM 的集成测试（新模型：配置在 ``drivers`` 表上）。

覆盖：生成、幂等、后缀分类、最小体积、递归开关、孤儿清理。
"""

from __future__ import annotations

import time

import pytest
from app.db.models import Driver, MediaFile
from app.services.media import scan_driver
from sqlalchemy import select

#: 假的 cid —— 不打真实网盘
FAKE_CID = "3116168171472551604"


@pytest.fixture
async def driver_row(db_session) -> Driver:
    """已配置媒体库的存储源。"""
    row = Driver(
        id=1,
        type="fake",
        name="测试源",
        credential=None,
        device_type=None,
        enabled=True,
        media_cid=FAKE_CID,
        media_path="/影视",
        output_dir=None,  # 各用例自己设
        video_exts="mkv,mp4",
        image_exts="jpg,png",
        data_exts="nfo",
        min_size_mb=0,
        include_subdirs=True,
        created_at=int(time.time()),
        updated_at=int(time.time()),
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


class TestScanDriver:
    async def test_creates_strm_files(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        """扫描产出 strm 文件，内容是中转 URL。"""
        driver_row.output_dir = str(tmp_path)
        await db_session.commit()
        fake_driver.add_file("pc1", "movie.mkv", path="/影视/movie.mkv")

        stats = await scan_driver(db_session, settings, driver_row)

        assert stats.created == 1
        strms = list(tmp_path.rglob("*.strm"))
        assert len(strms) == 1
        assert strms[0].read_text().strip().startswith("http://localhost:8080/302/")

    async def test_rescan_is_idempotent(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        """重复扫描不重复写文件（媒体服务器靠 mtime 判断要不要重扫库）。"""
        driver_row.output_dir = str(tmp_path)
        await db_session.commit()
        fake_driver.add_file("pc1", "movie.mkv", path="/影视/movie.mkv")

        first = await scan_driver(db_session, settings, driver_row)
        second = await scan_driver(db_session, settings, driver_row)

        assert first.created == 1
        assert second.created == 0
        assert second.unchanged == 1

    async def test_media_row_upserted(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        """媒体条目落库，且不重复。"""
        driver_row.output_dir = str(tmp_path)
        await db_session.commit()
        fake_driver.add_file("pc1", "movie.mkv", path="/影视/movie.mkv")

        await scan_driver(db_session, settings, driver_row)
        await scan_driver(db_session, settings, driver_row)

        rows = (await db_session.execute(select(MediaFile))).scalars().all()
        assert len(rows) == 1
        assert rows[0].remote_id == "pc1"

    async def test_filename_kept_as_is(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        """**文件名沿用网盘原样**（不重命名）——加 .strm 即可。

        原文件名已含完整发行信息，播放器对标准命名解析最准。
        """
        driver_row.output_dir = str(tmp_path)
        await db_session.commit()
        src_name = "唐探1900 (2025) - 2160p.WEB-DL.mkv"
        fake_driver.add_file("pc1", src_name, path=f"/影视/{src_name}")

        await scan_driver(db_session, settings, driver_row)

        strms = list(tmp_path.rglob("*.strm"))
        assert len(strms) == 1
        assert strms[0].name == f"{src_name}.strm"

    async def test_subdir_structure_preserved(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        """相对媒体库根的目录结构保留。"""
        driver_row.output_dir = str(tmp_path)
        await db_session.commit()
        fake_driver.add_file("pc1", "a.mkv", path="/影视/电影/华语/a.mkv")

        await scan_driver(db_session, settings, driver_row)

        strms = list(tmp_path.rglob("*.strm"))
        assert len(strms) == 1
        assert strms[0].relative_to(tmp_path).as_posix() == "电影/华语/a.mkv.strm"


class TestExtensionClassification:
    async def test_video_generated(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        driver_row.output_dir = str(tmp_path)
        await db_session.commit()
        fake_driver.add_file("pc1", "a.mkv", path="/影视/a.mkv")

        stats = await scan_driver(db_session, settings, driver_row)
        assert stats.created == 1

    async def test_image_synced_as_attachment(
        self, db_session, settings, driver_row, fake_driver, tmp_path, http_mock
    ):
        """图片后缀**下载到本地**，与 strm 分开计数。"""
        driver_row.output_dir = str(tmp_path)
        await db_session.commit()
        fake_driver.add_file(
            "pc1", "poster.jpg", path="/影视/poster.jpg", size=4
        )

        async with http_mock(b"JPEG") as http:
            stats = await scan_driver(
                db_session, settings, driver_row, http_client=http
            )

        assert stats.created == 0
        assert stats.attachment_synced == 1
        assert stats.skipped_unknown == 0
        assert (tmp_path / "poster.jpg").read_bytes() == b"JPEG"

    async def test_data_synced_as_attachment(
        self, db_session, settings, driver_row, fake_driver, tmp_path, http_mock
    ):
        """数据后缀（nfo / 字幕）同样下载到本地。"""
        driver_row.output_dir = str(tmp_path)
        await db_session.commit()
        fake_driver.add_file("pc1", "movie.nfo", path="/影视/movie.nfo", size=3)

        async with http_mock(b"NFO") as http:
            stats = await scan_driver(
                db_session, settings, driver_row, http_client=http
            )

        assert stats.created == 0
        assert stats.attachment_synced == 1
        assert (tmp_path / "movie.nfo").read_bytes() == b"NFO"

    async def test_unknown_ext_counted_separately(
        self, db_session, settings, driver_row, fake_driver, tmp_path, http_mock
    ):
        """未知后缀只计数、不下载——与图片/数据分开。"""
        driver_row.output_dir = str(tmp_path)
        await db_session.commit()
        fake_driver.add_file("pc1", "readme.txt", path="/影视/readme.txt")

        async with http_mock(b"nope") as http:
            stats = await scan_driver(
                db_session, settings, driver_row, http_client=http
            )

        assert stats.skipped_unknown == 1
        assert stats.attachment_synced == 0
        assert stats.attachment_failed == 0

    async def test_configurable_video_exts(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        """后缀列表是**配置**——不在配置里的视频后缀不生成。"""
        driver_row.output_dir = str(tmp_path)
        driver_row.video_exts = "mkv"  # 只认 mkv
        await db_session.commit()
        fake_driver.add_file("pc1", "a.mp4", path="/影视/a.mp4")

        stats = await scan_driver(db_session, settings, driver_row)

        assert stats.created == 0
        assert stats.skipped_unknown == 1


class TestMinSize:
    async def test_small_files_skipped(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        """小于「最小视频」的文件跳过（过滤样片）。"""
        driver_row.output_dir = str(tmp_path)
        driver_row.min_size_mb = 10
        await db_session.commit()
        fake_driver.add_file("pc1", "sample.mkv", path="/影视/sample.mkv", size=1024)

        stats = await scan_driver(db_session, settings, driver_row)

        assert stats.created == 0
        assert stats.skipped_small == 1

    async def test_large_files_kept(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        driver_row.output_dir = str(tmp_path)
        driver_row.min_size_mb = 1
        await db_session.commit()
        fake_driver.add_file("pc1", "movie.mkv", path="/影视/movie.mkv", size=5 * 1024 * 1024)

        stats = await scan_driver(db_session, settings, driver_row)
        assert stats.created == 1

    async def test_zero_means_no_filter(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        """0 = 不过滤。"""
        driver_row.output_dir = str(tmp_path)
        driver_row.min_size_mb = 0
        await db_session.commit()
        fake_driver.add_file("pc1", "tiny.mkv", path="/影视/tiny.mkv", size=1)

        stats = await scan_driver(db_session, settings, driver_row)
        assert stats.created == 1


class TestPrune:
    async def test_prune_removes_orphans(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        """孤儿清理：源端消失的文件，其 strm 被删。"""
        driver_row.output_dir = str(tmp_path)
        await db_session.commit()
        fake_driver.add_file("pc1", "movie.mkv", path="/影视/movie.mkv")
        await scan_driver(db_session, settings, driver_row)
        assert len(list(tmp_path.rglob("*.strm"))) == 1

        fake_driver.files.clear()
        await scan_driver(db_session, settings, driver_row, prune=True)

        assert len(list(tmp_path.rglob("*.strm"))) == 0
        rows = (await db_session.execute(select(MediaFile))).scalars().all()
        assert len(rows) == 0


class TestConfigValidation:
    async def test_missing_cid_raises(self, db_session, settings, driver_row, tmp_path):
        from app.services.media import MediaServiceError

        driver_row.media_cid = None
        driver_row.output_dir = str(tmp_path)
        await db_session.commit()

        with pytest.raises(MediaServiceError):
            await scan_driver(db_session, settings, driver_row)

    async def test_missing_output_dir_raises(
        self, db_session, settings, driver_row, tmp_path
    ):
        from app.services.media import MediaServiceError

        driver_row.output_dir = None
        await db_session.commit()

        with pytest.raises(MediaServiceError):
            await scan_driver(db_session, settings, driver_row)
