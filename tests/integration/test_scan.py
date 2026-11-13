"""扫描生成 STRM 的集成测试（新模型：配置在 ``drivers`` 表上）。

覆盖：生成、幂等、后缀分类、最小体积、递归开关、孤儿清理。
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from app.db.models import Driver, MediaFile, TaskQueue
from app.services import media as media_mod
from app.services.media import scan_driver
from app.services.strm_writer import StrmWriteResult
from sqlalchemy import select, text

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

    async def test_video_extension_stripped(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        """**strm 文件名去掉视频扩展名**——目录与标题仍沿用网盘原样。

        理由：媒体服务器按「同目录同 stem」匹配 nfo——
        ``x.mkv.strm`` 的 stem 是 ``x.mkv``，网盘侧的 nfo 多数叫 ``x.nfo``
        （stem ``x``）→ 对不上，元数据刮不出来。
        """
        driver_row.output_dir = str(tmp_path)
        await db_session.commit()
        src_name = "唐探1900 (2025) - 2160p.WEB-DL.mkv"
        fake_driver.add_file("pc1", src_name, path=f"/影视/{src_name}")

        await scan_driver(db_session, settings, driver_row)

        strms = list(tmp_path.rglob("*.strm"))
        assert len(strms) == 1
        assert strms[0].name == "唐探1900 (2025) - 2160p.WEB-DL.strm"

    async def test_ext_not_configured_not_generated(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        """**没配进 video_exts 的后缀不生成 strm**（剥离用的是驱动配置）。

        ``.ts`` 不在 ``video_exts="mkv,mp4"`` 里 → 该文件不是视频。
        剥离逻辑据此「用户配什么剥什么」，而不是硬编码一份后缀表。
        """
        driver_row.output_dir = str(tmp_path)
        await db_session.commit()
        fake_driver.add_file("pc1", "a.ts", path="/影视/a.ts")

        stats = await scan_driver(db_session, settings, driver_row)

        assert stats.skipped_unknown == 1
        assert list(tmp_path.rglob("*.strm")) == []

    async def test_stale_strm_removed_on_rename(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        """**改名后旧 strm 被删掉**——否则变成无主文件（Emby 重复条目）。

        模拟存量库：DB 里记的是旧命名（``x.mkv.strm``），重扫后新命名
        （``x.strm``）生效，旧文件必须消失。
        """
        driver_row.output_dir = str(tmp_path)
        await db_session.commit()
        src_name = "movie.mkv"
        fake_driver.add_file("pc1", src_name, path=f"/影视/{src_name}")

        # 先按旧规则落库 + 落盘
        old_strm = tmp_path / "movie.mkv.strm"
        old_strm.write_text("http://localhost:8080/302/1/sig\n", encoding="utf-8")
        db_session.add(
            MediaFile(
                driver_id=driver_row.id,
                remote_id="pc1",
                path=f"/影视/{src_name}",
                name=src_name,
                size=100,
                strm_path=str(old_strm),
                signature="sig",
                mtime=None,
                created_at="2026-01-01 00:00:00",
                updated_at="2026-01-01 00:00:00",
            )
        )
        await db_session.commit()

        await scan_driver(db_session, settings, driver_row)

        # 旧文件已删，新文件已生成
        assert not old_strm.exists()
        strms = list(tmp_path.rglob("*.strm"))
        assert [p.name for p in strms] == ["movie.strm"]
        # DB 路径同步更新
        row = (
            await db_session.execute(
                select(MediaFile).where(MediaFile.remote_id == "pc1")
            )
        ).scalar_one()
        assert row.strm_path == str(tmp_path / "movie.strm")

    async def test_same_stem_different_ext_conflict_reported(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        """**同名不同扩展名的冲突要报告**（去掉扩展名后新增的风险）。

        ``x.mp4`` 与 ``x.mkv`` 映射到同一个 ``x.strm`` → 媒体服务器只会
        看到一个条目。允许覆盖（播放仍指向正确源文件），但必须点名。
        """
        driver_row.output_dir = str(tmp_path)
        await db_session.commit()
        fake_driver.add_file("pc1", "movie.mp4", path="/影视/movie.mp4")
        fake_driver.add_file("pc2", "movie.mkv", path="/影视/movie.mkv")

        stats = await scan_driver(db_session, settings, driver_row)

        assert stats.failed == 1
        assert len(stats.errors) == 1
        assert "strm 文件名冲突" in stats.errors[0]
        assert "movie.mp4" in stats.errors[0]
        assert "movie.mkv" in stats.errors[0]

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
        assert strms[0].relative_to(tmp_path).as_posix() == "电影/华语/a.strm"


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


class TestScanLockWindow:
    """扫描**不得**长时间持有 SQLite 写锁（2026-11-12 prod 事故回归）。

    事故机制：扫描的事务横跨「写 strm（CIFS）+ 等附件下载」→ 写锁被
    持有几十秒 → ``drain_task_queue`` / ``push_free_torrents`` /
    ``refresh_user_data`` 的写事务撞 ``database is locked``。

    这里用「第二个连接写库」复现同一机制：写 strm 慢（模拟 CIFS
    卡顿）时，第二个连接必须能立刻写进去。
    """

    async def test_other_writes_not_starved_while_writing_strm(
        self,
        db_session,
        settings,
        driver_row,
        fake_driver,
        tmp_path,
        db_engine,
        monkeypatch,
    ):
        driver_row.output_dir = str(tmp_path)
        await db_session.commit()
        fake_driver.add_file("pc1", "movie.mkv", path="/影视/movie.mkv")

        started = threading.Event()

        def _slow_write(path, url):
            """模拟 CIFS 卡顿的 strm 写入（同步函数，跑在线程池里）。"""
            started.set()
            # 卡顿时长必须**明显大于**探针连接的 busy_timeout——
            # 否则探针恰好等到扫描提交，测试会侥幸通过（测不出回归）。
            time.sleep(1.0)
            return StrmWriteResult.CREATED

        monkeypatch.setattr("app.services.media.write_strm", _slow_write)

        scan_task = asyncio.create_task(
            scan_driver(db_session, settings, driver_row)
        )
        # 等扫描进入「写 strm」阶段
        await asyncio.to_thread(started.wait, 5.0)
        assert started.is_set(), "扫描未进入写 strm 阶段"

        # 第二个连接此刻写库——改造前会被扫描的写锁挡住（busy_timeout
        # 到期抛 database is locked），改造后必须立刻写进去
        factory = db_engine[1]
        async with factory() as probe:
            # busy_timeout 必须**短于**扫描的卡顿时长——
            # 改造前探针会等不到锁而抛 database is locked（测出回归）。
            await probe.execute(text("PRAGMA busy_timeout=300"))
            await probe.commit()
            probe.add(
                TaskQueue(
                    idem_key="lock-window-probe",
                    kind="add_torrent",
                    payload={"probe": True},
                )
            )
            await probe.commit()  # 改造前这里抛 OperationalError

        await scan_task

    async def test_strm_written_after_commit(
        self, db_session, settings, driver_row, fake_driver, tmp_path, monkeypatch
    ):
        """写 strm 发生在**提交之后**——不在持锁事务里。

        判据：写 strm 的时刻，该 session 上**没有未提交的写事务**
        （``in_transaction()`` 为假或只含读事务）。
        """
        driver_row.output_dir = str(tmp_path)
        await db_session.commit()
        fake_driver.add_file("pc1", "movie.mkv", path="/影视/movie.mkv")

        observed: list[bool] = []
        real_write = media_mod.write_strm

        def _spy_write(path, url):
            # in_transaction() 只说明「有事务」，不说明「持写锁」——
            # 这里的判据是**没有待提交的写**：new/dirty 对象为空
            observed.append(not db_session.new and not db_session.dirty)
            return real_write(path, url)

        monkeypatch.setattr("app.services.media.write_strm", _spy_write)

        await scan_driver(db_session, settings, driver_row)

        assert observed == [True], "写 strm 时仍有未提交的写（持着 SQLite 写锁）"
        assert len(list(tmp_path.rglob("*.strm"))) == 1


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
