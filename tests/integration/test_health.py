"""库健康检查的集成测试。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from app.db.models import Driver
from app.services.health import check_library
from app.services.media import scan_driver
from app.services.rotation import rotate_secret
from app.services.signing import sign

SECRET = "test-signing-secret"


@pytest.fixture
async def driver_row(db_session) -> Driver:
    row = Driver(
        id=1,
        type="fake",
        name="测试源",
        credential=None,
        device_type=None,
        enabled=True,
        created_at=int(time.time()),
        updated_at=int(time.time()),
    )
    db_session.add(row)
    await db_session.commit()
    return row


async def _driver(db_session, output_dir: Path) -> Driver:
    """带媒体库配置的存储源。"""
    row = Driver(
        id=2,
        type="fake",
        name="测试源2",
        credential=None,
        device_type=None,
        enabled=True,
        media_cid="12345",
        media_path="/media",
        output_dir=str(output_dir),
        video_exts="mkv,mp4",
        created_at=int(time.time()),
        updated_at=int(time.time()),
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


class TestHealthCheck:
    async def test_healthy_library(self, db_session, settings, driver_row, fake_driver, tmp_path):
        """正常的库：全部通过。"""
        fake_driver.add_file("pc1", "movie.mkv", path="/media/movie.mkv")
        driver = await _driver(db_session, tmp_path)
        await scan_driver(db_session, settings, driver)

        report = await check_library(db_session, settings)

        assert report.healthy
        assert report.missing_files == []
        assert report.orphan_files == []
        assert report.invalid_signature == []

    async def test_missing_file_detected(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        """DB 有条目但磁盘文件没了 → missing_files。"""
        fake_driver.add_file("pc1", "movie.mkv", path="/media/movie.mkv")
        driver = await _driver(db_session, tmp_path)
        await scan_driver(db_session, settings, driver)

        for strm in tmp_path.rglob("*.strm"):
            strm.unlink()

        report = await check_library(db_session, settings)

        assert not report.healthy
        assert len(report.missing_files) == 1

    async def test_orphan_file_detected(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        """磁盘有 strm 但 DB 没记录 → orphan_files。"""
        fake_driver.add_file("pc1", "movie.mkv", path="/media/movie.mkv")
        driver = await _driver(db_session, tmp_path)
        await scan_driver(db_session, settings, driver)

        orphan = tmp_path / "orphan.strm"
        orphan.write_text("http://x/302/1/abc\n", encoding="utf-8")

        report = await check_library(db_session, settings)

        assert not report.healthy
        assert len(report.orphan_files) == 1

    async def test_invalid_signature_detected(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        """签名与当前密钥不符 → invalid_signature。"""
        fake_driver.add_file("pc1", "movie.mkv", path="/media/movie.mkv")
        driver = await _driver(db_session, tmp_path)
        await scan_driver(db_session, settings, driver)

        # 篡改 DB 里的签名（模拟密钥变了但没重签）
        media = (await db_session.execute(_select_media())).scalar_one()
        media.signature = sign("totally-different-secret", media.id)
        await db_session.commit()

        report = await check_library(db_session, settings)

        assert not report.healthy
        assert len(report.invalid_signature) == 1

    async def test_stale_content_detected_after_rotation(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        """密钥轮换后文件内容未更新 → stale。

        用例里手动改文件内容模拟「轮换后没重写」。
        """
        fake_driver.add_file("pc1", "movie.mkv", path="/media/movie.mkv")
        driver = await _driver(db_session, tmp_path)
        await scan_driver(db_session, settings, driver)

        strm = next(tmp_path.rglob("*.strm"))
        strm.write_text("http://localhost:8080/302/1/obsolete\n", encoding="utf-8")

        report = await check_library(db_session, settings)

        assert len(report.stale) == 1

    async def test_missing_secret_reports_config_error_not_invalid_signature(
        self, db_session, settings_copy, driver_row, fake_driver, tmp_path
    ):
        """空密钥 ≠ 全库签名失效——不能诱导运维批量重签。"""
        fake_driver.add_file("pc1", "movie.mkv", path="/media/movie.mkv")
        driver = await _driver(db_session, tmp_path)
        await scan_driver(db_session, settings_copy, driver)

        settings_copy.signing_secret = None
        report = await check_library(db_session, settings_copy)

        assert report.configuration_error
        assert "SIGNING_SECRET" in report.configuration_error
        assert report.invalid_signature == []
        assert report.stale == []
        assert not report.healthy
        assert "configuration_error" in report.to_dict()

    async def test_rotation_fixes_stale(
        self, db_session, settings_copy, driver_row, fake_driver, tmp_path
    ):
        """轮换后重新检查应恢复健康。

        ⚠️ 用 ``settings_copy``（副本）而非共享的 ``settings``——
        ``rotate_secret`` 会**就地**改 ``settings.signing_secret``，而
        ``settings`` 是 session 级共享对象，改动会泄漏到后续所有用例
        （表现为后面随机冒出的 403「签名无效」）。
        """
        fake_driver.add_file("pc1", "movie.mkv", path="/media/movie.mkv")
        driver = await _driver(db_session, tmp_path)
        await scan_driver(db_session, settings_copy, driver)

        await rotate_secret(db_session, settings_copy)

        report = await check_library(db_session, settings_copy)
        assert report.healthy


async def test_library_report_scans_in_worker_thread(db, app_settings, monkeypatch):
    """磁盘扫描必须离开事件循环（CIFS 上同步 rglob 会冻结整个服务）。"""
    import asyncio
    import threading

    from app.services import health as health_mod

    main_thread = threading.current_thread()
    seen: list[threading.Thread] = []
    real_to_thread = asyncio.to_thread

    async def spy(func, /, *args, **kwargs):
        # 记录「函数真正执行的线程」，不是调用 to_thread 的主线程
        def in_worker():
            seen.append(threading.current_thread())
            return func(*args, **kwargs)

        return await real_to_thread(in_worker)

    monkeypatch.setattr(health_mod, "_to_thread", spy)

    report = await health_mod.library_health_report()

    assert len(seen) == 1, "扫盘没有走线程池"
    assert seen[0] is not main_thread
    assert report.checked_at > 0


def _select_media():
    from app.db.models import MediaFile as MF
    from sqlalchemy import select

    return select(MF)
