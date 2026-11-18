"""过期数据清理作业的注册与入口测试。"""

from __future__ import annotations

from app.services import job_registry


def test_cleanup_jobs_registered_with_cron() -> None:
    """两个清理作业必须在注册表里，且各自有合法 cron。"""
    names = {m.name for m in job_registry.JOB_META}
    assert "cleanup_link_cache" in names
    assert "purge_dav_auth_cache" in names
    for name in ("cleanup_link_cache", "purge_dav_auth_cache"):
        cron = job_registry.DEFAULT_CRON[name]
        assert len(cron.split()) == 5, f"{name} 的 cron 不是 5 字段：{cron!r}"


async def test_link_cache_cleanup_entry(monkeypatch) -> None:
    """入口调用 LinkCache.cleanup_expired() 并回传删除条数。"""
    calls: list[int] = []

    class FakeCache:
        async def cleanup_expired(self) -> int:
            calls.append(1)
            return 7

    monkeypatch.setattr(
        "app.services.svc_factory.get_link_cache", lambda: FakeCache()
    )
    result = await job_registry._run_cleanup_link_cache()
    assert calls == [1]
    assert result == {"deleted": 7}


async def test_dav_auth_purge_entry(monkeypatch) -> None:
    """入口调用 dav_authenticator.purge_expired() 并回传淘汰条数。"""
    calls: list[int] = []

    def fake_purge() -> int:
        calls.append(1)
        return 3

    monkeypatch.setattr(
        "app.services.dav_auth.dav_authenticator.purge_expired", fake_purge
    )
    result = await job_registry._run_purge_dav_auth_cache()
    assert calls == [1]
    assert result == {"purged": 3}
