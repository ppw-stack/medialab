"""302 中转端点的集成测试。

覆盖：签名校验、UA 分桶、缓存命中、错误码、单飞。
"""

from __future__ import annotations

import time

from app.db.models import Driver, MediaFile
from app.services.signing import sign

SECRET = "test-signing-secret"


async def _seed_media(db, driver_id: int = 1, remote_id: str = "pc_abc") -> MediaFile:
    """插入测试用的存储源与媒体条目。"""
    db.add(
        Driver(
            id=driver_id,
            type="fake",
            name="测试源",
            credential=None,
            device_type=None,
            enabled=True,
            created_at=int(time.time()),
            updated_at=int(time.time()),
        )
    )
    media = MediaFile(
        driver_id=driver_id,
        remote_id=remote_id,
        path="/media/movie.mkv",
        name="movie.mkv",
        size=1024,
        strm_path="/library/movie.mkv.strm",
        signature=None,
        created_at=int(time.time()),
        updated_at=int(time.time()),
    )
    db.add(media)
    await db.commit()
    await db.refresh(media)
    return media


class TestRedirectEndpoint:
    async def test_valid_signature_returns_302(self, app_client, db_session, fake_driver):
        media = await _seed_media(db_session)
        fake_driver.add_file(media.remote_id, "movie.mkv")
        sig = sign(SECRET, media.id)

        resp = await app_client.get(f"/302/{media.id}/{sig}")

        assert resp.status_code == 302
        assert resp.headers["location"].startswith("https://cdn.example.com/")

    async def test_invalid_signature_returns_403(self, app_client, db_session, fake_driver):
        media = await _seed_media(db_session)
        fake_driver.add_file(media.remote_id, "movie.mkv")

        resp = await app_client.get(f"/302/{media.id}/wrongsignature0000000000")

        assert resp.status_code == 403
        assert resp.json()["error"]["kind"] == "forbidden"

    async def test_unknown_media_returns_404(self, app_client):
        sig = sign(SECRET, 9999)
        resp = await app_client.get(f"/302/9999/{sig}")
        assert resp.status_code == 404
        assert resp.json()["error"]["kind"] == "not_found"

    async def test_tampered_media_id_fails_signature(self, app_client, db_session, fake_driver):
        """改 media_id → 签名不匹配 → 403。"""
        media = await _seed_media(db_session)
        fake_driver.add_file(media.remote_id, "movie.mkv")
        sig = sign(SECRET, media.id)

        resp = await app_client.get(f"/302/{media.id + 1}/{sig}")

        assert resp.status_code == 403

    async def test_driver_error_returns_502(self, app_client, db_session, fake_driver):
        """驱动调用失败 → 502，**不返回 0 或空**。"""
        media = await _seed_media(db_session)
        # 不添加文件 → 驱动抛 not_found
        sig = sign(SECRET, media.id)

        resp = await app_client.get(f"/302/{media.id}/{sig}")

        assert resp.status_code == 502
        assert resp.json()["error"]["kind"] == "upstream"

    async def test_second_request_hits_cache(self, app_client, db_session, fake_driver):
        """第二次请求走缓存——不重复调驱动。"""
        media = await _seed_media(db_session)
        fake_driver.add_file(media.remote_id, "movie.mkv")
        sig = sign(SECRET, media.id)

        await app_client.get(f"/302/{media.id}/{sig}")
        await app_client.get(f"/302/{media.id}/{sig}")

        assert fake_driver.call_counts[media.remote_id] == 1

    async def test_different_ua_triggers_new_direct_link(
        self, app_client, db_session, fake_driver
    ):
        """不同 UA → 不同缓存分桶 → 分别换直链。"""
        media = await _seed_media(db_session)
        fake_driver.add_file(media.remote_id, "movie.mkv")
        sig = sign(SECRET, media.id)

        await app_client.get(f"/302/{media.id}/{sig}", headers={"user-agent": "PlayerA/1.0"})
        await app_client.get(f"/302/{media.id}/{sig}", headers={"user-agent": "PlayerB/2.0"})

        assert fake_driver.call_counts[media.remote_id] == 2

    async def test_same_ua_reuses_cached_link(self, app_client, db_session, fake_driver):
        media = await _seed_media(db_session)
        fake_driver.add_file(media.remote_id, "movie.mkv")
        sig = sign(SECRET, media.id)

        await app_client.get(f"/302/{media.id}/{sig}", headers={"user-agent": "PlayerA/1.0"})
        await app_client.get(f"/302/{media.id}/{sig}", headers={"user-agent": "PlayerA/1.0"})

        assert fake_driver.call_counts[media.remote_id] == 1

    async def test_no_ua_client_uses_default_ua(self, app_client, db_session, fake_driver):
        """无 UA 客户端（Infuse 等）按 ua_policy 兜底为默认 UA。"""
        media = await _seed_media(db_session)
        fake_driver.add_file(media.remote_id, "movie.mkv")
        sig = sign(SECRET, media.id)

        resp = await app_client.get(f"/302/{media.id}/{sig}")

        assert resp.status_code == 302

    async def test_concurrent_requests_call_driver_once(
        self, app_client, db_session, fake_driver
    ):
        """并发请求同一条目 → 单飞 → 只调一次驱动。"""
        import asyncio

        media = await _seed_media(db_session)
        fake_driver.add_file(media.remote_id, "movie.mkv")
        sig = sign(SECRET, media.id)

        responses = await asyncio.gather(
            *[
                app_client.get(f"/302/{media.id}/{sig}", headers={"user-agent": "P/1.0"})
                for _ in range(8)
            ]
        )

        assert all(r.status_code == 302 for r in responses)
        assert fake_driver.call_counts[media.remote_id] == 1


class TestMediaFileModel:
    async def test_signature_stored_and_readable(self, db_session):
        media = await _seed_media(db_session)
        from sqlalchemy import select as sel

        found = (
            await db_session.execute(sel(MediaFile).where(MediaFile.id == media.id))
        ).scalar_one()
        assert found.signature is None
