"""服务层测试（Phase 2 批次 3 —— API 层业务逻辑下沉）。

此前站点 / 下载器 / 仪表盘 / 索引 / 作业历史的业务逻辑全在
:mod:`app.api.routes` 的路由层：40 处 ``session_scope`` 直写 SQL，
外加 11 处直接构造 / 调用适配器。路由层同时碰 DB 与网络，无法独立
单测——业务逻辑只能靠 API 集成测试间接覆盖。

本文件直接测**服务层**（不经 HTTP），验证下沉后的行为：

- 站点 CRUD + 自检 + 拉取测试
- 下载器 CRUD + 测连 + 原生任务操作
- 仪表盘统计
- 索引查询 + 推送
- 作业历史查询
- 统一错误契约（:class:`app.services.errors.ServiceError`）
"""

from __future__ import annotations

import pytest
from app.adapters.downloaders.base import (
    DownloaderError,
    TorrentFilter,
    TorrentInfo,
    TorrentState,
)
from app.db.models import (
    DownloaderInstance,
    Site,
    SiteUserData,
    TaskQueue,
    TorrentIndex,
)
from app.db.session import session_scope
from app.services import (
    dashboard_service,
    downloader_service,
    index_service,
    job_run_service,
    site_service,
    user_data_query,
)
from app.services.errors import ServiceError
from app.services.factory import AdapterFactory
from sqlalchemy import select

# ==================================================================== 夹具


async def _add_site(name: str = "s1", **kw: object) -> int:
    async with session_scope() as session:
        s = Site(name=name, domain=f"{name}.test", adapter="hdsky", **kw)  # type: ignore[arg-type]
        session.add(s)
        await session.flush()
        return s.id


async def _add_downloader(name: str = "dl1", **kw: object) -> int:
    async with session_scope() as session:
        d = DownloaderInstance(
            name=name,
            type="qbittorrent",
            url=f"http://{name}:8081",
            **kw,  # type: ignore[arg-type]
        )
        session.add(d)
        await session.flush()
        return d.id


# ==================================================================== 站点 CRUD


async def test_site_list_empty(db: None) -> None:
    assert await site_service.list_sites() == []


async def test_site_create_and_list(db: None) -> None:
    snap = await site_service.create_site(name="s1", adapter="hdsky", domain="x.test")
    assert snap.name == "s1"
    assert snap.adapter == "hdsky"
    assert snap.has_apikey is False

    rows = await site_service.list_sites()
    assert len(rows) == 1
    assert rows[0].id == snap.id


async def test_site_create_duplicate_name_409(db: None) -> None:
    await site_service.create_site(name="dup", adapter="hdsky")
    with pytest.raises(ServiceError) as exc:
        await site_service.create_site(name="dup", adapter="hdsky")
    assert exc.value.status == 409


async def test_site_create_invalid_adapter_400(db: None) -> None:
    with pytest.raises(ServiceError) as exc:
        await site_service.create_site(name="x", adapter="nonexistent")
    assert exc.value.status == 400
    assert "未注册的站点 adapter" in str(exc.value)


async def test_site_get_not_found_404(db: None) -> None:
    with pytest.raises(ServiceError) as exc:
        await site_service.get_site(999999)
    assert exc.value.status == 404


async def test_site_delete_cascades_user_data(db: None, factory: AdapterFactory) -> None:
    """删站点要连带删 site_user_data（否则 FK 约束 → IntegrityError → 500）。"""
    site_id = await _add_site("todelete")
    async with session_scope() as session:
        session.add(SiteUserData(site_id=site_id, upload=1, download=2))
        await session.flush()

    await site_service.delete_site(site_id)

    async with session_scope() as session:
        rows = (await session.execute(select(SiteUserData))).scalars().all()
        assert rows == []


async def test_site_delete_cascades_torrent_index(db: None, factory: AdapterFactory) -> None:
    """删站点要级联删 torrent_index（否则 FK 约束 → IntegrityError → 500）。

    2026-10-23：原实现是「站点下还有 N 条任务，请先清理」→ 409。但那是
    死胡同——``torrent_index`` 是可重建的拉取快照（全仓没有删除它的
    入口），且 ``fetch_all_sites`` 每 10 分钟就重新灌满。改为级联删。
    """
    site_id = await _add_site("s1")
    async with session_scope() as session:
        session.add(TorrentIndex(site_id=site_id, item_id="i1", name="t"))
        await session.flush()

    await site_service.delete_site(site_id)

    async with session_scope() as session:
        rows = (await session.execute(select(TorrentIndex))).scalars().all()
        assert rows == []
        assert (await session.execute(select(Site))).scalars().all() == []


async def test_site_delete_cascades_task_queue(db: None, factory: AdapterFactory) -> None:
    """删站点要级联删该站的推送队列行（idem_key 前缀含 site_id）。

    SQLite 删行后会**复用 id**——新建站点可能拿到旧站的 id，若 item_id
    撞上（m-team 是站内数字 id），老队列行会让推送被误判成"已推过"而
    永久跳过。
    """
    site_id = await _add_site("s1")
    other_id = await _add_site("s2")
    async with session_scope() as session:
        session.add_all([
            TaskQueue(
                idem_key=f"add_torrent|{site_id}|123|dl=1",
                kind="add_torrent",
                payload={"index_id": 1, "downloader_id": 1},
                status="done",
            ),
            TaskQueue(
                idem_key=f"add_torrent|{other_id}|123|dl=1",
                kind="add_torrent",
                payload={"index_id": 2, "downloader_id": 1},
                status="done",
            ),
        ])
        await session.flush()

    await site_service.delete_site(site_id)

    async with session_scope() as session:
        rows = (await session.execute(select(TaskQueue))).scalars().all()
        # 本站的删了，其他站的保留
        assert [r.idem_key for r in rows] == [f"add_torrent|{other_id}|123|dl=1"]


async def test_site_delete_queue_prefix_not_ambiguous(
    db: None, factory: AdapterFactory
) -> None:
    """删 site_id=1 不能误删 site_id=11 的队列行。

    前缀是 ``add_torrent|{site_id}|``——**必须带结尾的 ``|``**，否则
    ``add_torrent|11|...`` 会被 ``add_torrent|1`` 前缀误伤。
    """
    site_1 = await _add_site("s1")
    site_11 = await _add_site("s11")
    assert site_11 != site_1
    async with session_scope() as session:
        session.add_all([
            TaskQueue(
                idem_key=f"add_torrent|{site_1}|555|dl=1",
                kind="add_torrent",
                payload={"index_id": 1, "downloader_id": 1},
                status="done",
            ),
            TaskQueue(
                idem_key=f"add_torrent|{site_11}|555|dl=1",
                kind="add_torrent",
                payload={"index_id": 2, "downloader_id": 1},
                status="done",
            ),
        ])
        await session.flush()

    await site_service.delete_site(site_1)

    async with session_scope() as session:
        rows = (await session.execute(select(TaskQueue))).scalars().all()
        # 只剩 site_id=11 那条
        assert [r.idem_key for r in rows] == [f"add_torrent|{site_11}|555|dl=1"]


# ------------------------------------------------------- 更新的三态语义


async def test_site_update_unset_credential_keeps_value(db: None, factory: AdapterFactory) -> None:
    """凭据字段未提供（UNSET）→ 保留原值。"""
    site_id = await _add_site("s1", apikey="secret")
    snap = await site_service.update_site(site_id, domain="new.test")
    assert snap.domain == "new.test"
    assert snap.has_apikey is True


async def test_site_update_empty_string_clears_credential(
    db: None, factory: AdapterFactory
) -> None:
    """凭据字段传空串 → 清空。"""
    site_id = await _add_site("s1", apikey="secret")
    snap = await site_service.update_site(site_id, apikey="")
    assert snap.has_apikey is False


async def test_site_update_rename_duplicate_409(db: None, factory: AdapterFactory) -> None:
    await _add_site("a")
    site_id = await _add_site("b")
    with pytest.raises(ServiceError) as exc:
        await site_service.update_site(site_id, name="a")
    assert exc.value.status == 409


async def test_site_update_invalid_adapter_400(db: None, factory: AdapterFactory) -> None:
    site_id = await _add_site("s1")
    with pytest.raises(ServiceError) as exc:
        await site_service.update_site(site_id, adapter="nonexistent")
    assert exc.value.status == 400


# ==================================================================== 站点自检


async def test_site_self_test_not_found_404(db: None) -> None:
    with pytest.raises(ServiceError) as exc:
        await site_service.self_test_site(999999)
    assert exc.value.status == 404


async def test_site_self_test_returns_probe(db: None, monkeypatch) -> None:
    """自检返回连通性 + 窗口解析探针结果。"""
    from app.adapters.sites.base import SiteHealth

    site_id = await _add_site("s1")

    class _FakeAdapter:
        def __init__(self, profile: object, **_: object) -> None:
            pass

        async def check_reachability(self) -> SiteHealth:
            return SiteHealth(reachable=True, logged_in=True, detail="ok")

        async def close(self) -> None:
            return None

    monkeypatch.setattr(
        site_service, "resolve_site_adapter", lambda _name: _FakeAdapter
    )
    result = await site_service.self_test_site(site_id)
    assert result.reachable is True
    assert result.logged_in is True
    # 探针文案"剩余 2 天 6 小时" → 应解析出限时免费 + 结束时刻
    assert result.free_type == "freeleech_timed"
    assert result.free_end_at is not None


async def test_site_self_test_closes_adapter(db: None, monkeypatch) -> None:
    """自检完必须关连接（不污染连接池）。"""
    from app.adapters.sites.base import SiteHealth

    site_id = await _add_site("s1")
    closed: list[bool] = []

    class _FakeAdapter:
        def __init__(self, profile: object, **_: object) -> None:
            pass

        async def check_reachability(self) -> SiteHealth:
            return SiteHealth(reachable=True)

        async def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(
        site_service, "resolve_site_adapter", lambda _name: _FakeAdapter
    )
    await site_service.self_test_site(site_id)
    assert closed == [True]


# ==================================================================== 站点用户数据


async def test_site_user_data_no_row(db: None) -> None:
    site_id = await _add_site("s1")
    snap = await site_service.get_site_user_data(site_id)
    assert snap.has_data is False
    assert snap.site_name == "s1"


async def test_site_user_data_with_row(db: None) -> None:
    site_id = await _add_site("s1")
    async with session_scope() as session:
        session.add(
            SiteUserData(
                site_id=site_id,
                upload=1024,
                download=2048,
                seeding_count=10,
                leeching_count=2,
            )
        )
        await session.flush()

    snap = await site_service.get_site_user_data(site_id)
    assert snap.has_data is True
    assert snap.upload == 1024
    assert snap.seeding_count == 10
    assert snap.leeching_count == 2


async def test_site_user_data_not_found_404(db: None) -> None:
    with pytest.raises(ServiceError) as exc:
        await site_service.get_site_user_data(999999)
    assert exc.value.status == 404


async def test_user_data_query_lists_all(db: None) -> None:
    site_id = await _add_site("s1")
    async with session_scope() as session:
        session.add(SiteUserData(site_id=site_id, upload=7))
        await session.flush()

    rows = await user_data_query.list_user_data()
    assert len(rows) == 1
    assert rows[0].upload == 7
    assert rows[0].adapter == "hdsky"
    assert rows[0].site_name == "s1"

    trends = await user_data_query.list_user_data_trends()
    assert len(trends) == 1
    assert trends[0].upload == 7


# ==================================================================== 下载器


async def test_downloader_list_empty(db: None) -> None:
    assert await downloader_service.list_downloaders() == []


async def test_downloader_create_and_list(db: None) -> None:
    snap = await downloader_service.create_downloader(
        name="dl1", type="qbittorrent", url="http://dl:8081", username="u", password="p"
    )
    assert snap.name == "dl1"
    assert snap.has_password is True
    assert snap.username == "u"

    rows = await downloader_service.list_downloaders()
    assert len(rows) == 1
    assert rows[0].id == snap.id


async def test_downloader_create_duplicate_name_409(db: None) -> None:
    await downloader_service.create_downloader(
        name="dl1", type="qbittorrent", url="http://dl:8081"
    )
    with pytest.raises(ServiceError) as exc:
        await downloader_service.create_downloader(
            name="dl1", type="qbittorrent", url="http://dl2:8081"
        )
    assert exc.value.status == 409


async def test_downloader_create_unknown_type_400(db: None) -> None:
    with pytest.raises(ServiceError) as exc:
        await downloader_service.create_downloader(
            name="dl1", type="nonexistent", url="http://dl:8081"
        )
    assert exc.value.status == 400


async def test_downloader_create_unimplemented_type_501(db: None) -> None:
    """deluge/aria2 是合法类型但尚未实现 → 501（不是 400）。"""
    with pytest.raises(ServiceError) as exc:
        await downloader_service.create_downloader(
            name="dl1", type="deluge", url="http://dl:8081"
        )
    assert exc.value.status == 501


async def test_downloader_default_is_exclusive(db: None) -> None:
    """设默认会把其他默认置 False（同一时刻只能有一个默认）。"""
    first = await downloader_service.create_downloader(
        name="dl1", type="qbittorrent", url="http://dl1:8081", is_default=True
    )
    second = await downloader_service.create_downloader(
        name="dl2", type="qbittorrent", url="http://dl2:8081", is_default=True
    )
    rows = {d.id: d for d in await downloader_service.list_downloaders()}
    assert rows[first.id].is_default is False
    assert rows[second.id].is_default is True


async def test_downloader_get_not_found_404(db: None) -> None:
    with pytest.raises(ServiceError) as exc:
        await downloader_service.get_downloader(999999)
    assert exc.value.status == 404


async def test_downloader_delete(db: None, factory: AdapterFactory) -> None:
    dl_id = await _add_downloader("dl1")
    await downloader_service.delete_downloader(dl_id)
    assert await downloader_service.list_downloaders() == []


async def test_downloader_update_clears_password(db: None, factory: AdapterFactory) -> None:
    dl_id = await _add_downloader("dl1", password="p")
    snap = await downloader_service.update_downloader(dl_id, password="")
    assert snap.has_password is False


async def test_downloader_update_returns_fresh_updated_at(
    db: None, factory: AdapterFactory
) -> None:
    """更新后返回的 ``updated_at`` 必须与**落库值**一致。

    ``updated_at`` 由 ``onupdate`` 在 flush 时写入——若在事务内读快照，
    拿到的是**旧值**（提交前的），与 DB 里的实际值对不上。
    """
    import asyncio

    dl_id = await _add_downloader("dl1")
    await asyncio.sleep(1.1)  # 跨秒——字符串精度只有秒

    snap = await downloader_service.update_downloader(dl_id, url="http://new:8081")

    async with session_scope() as session:
        db_value = (
            await session.execute(
                select(DownloaderInstance).where(DownloaderInstance.id == dl_id)
            )
        ).scalar_one().updated_at

    assert snap.updated_at == db_value
    assert snap.updated_at != ""


async def test_site_update_returns_fresh_updated_at(
    db: None, factory: AdapterFactory
) -> None:
    """同 :func:`test_downloader_update_returns_fresh_updated_at`。"""
    import asyncio

    site_id = await _add_site("s1")
    await asyncio.sleep(1.1)

    snap = await site_service.update_site(site_id, domain="new.test")

    async with session_scope() as session:
        db_value = (
            await session.execute(select(Site).where(Site.id == site_id))
        ).scalar_one().updated_at

    assert snap.updated_at == db_value


# ------------------------------------------------------------ hash 校验


@pytest.mark.parametrize(
    "bad",
    ["all", "|", "abc", "", "z" * 40, "a" * 39],
)
def test_validate_hashes_rejects_bad(bad: str) -> None:
    with pytest.raises(ServiceError) as exc:
        downloader_service.validate_hashes([bad])
    assert exc.value.status == 400


def test_validate_hashes_accepts_valid() -> None:
    downloader_service.validate_hashes(["a" * 40, "0123456789abcdef" * 2 + "01234567"])


# ------------------------------------------------------- 原生任务（假适配器）


class _FakeDownloaderAdapter:
    """记录调用的假下载器适配器。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.paused: list[tuple[list[str], bool]] = []
        self.removed: list[tuple[list[str], bool]] = []
        self.listed: list[TorrentFilter] = []

    async def list_torrents(self, filt: object = None) -> list[TorrentInfo]:
        if self.fail:
            raise DownloaderError("boom")
        self.listed.append(filt)  # type: ignore[arg-type]
        return [
            TorrentInfo(
                hash="a" * 40,
                name="t1",
                state=TorrentState.SEEDING,
                progress=1.0,
                size=1024,
                downloaded=1024,
                uploaded=2048,
            )
        ]

    async def set_pause(self, hashes: list[str], paused: bool) -> bool:
        self.paused.append((hashes, paused))
        return True

    async def remove(self, hashes: list[str], *, delete_data: bool) -> bool:
        self.removed.append((hashes, delete_data))
        return True


def _install_fake_downloader(monkeypatch, adapter: object) -> None:
    """把工厂的 ``downloader()`` 换成假适配器。"""
    from app.services import factory as factory_mod

    monkeypatch.setattr(
        factory_mod.AdapterFactory, "downloader", lambda self, row: adapter
    )


async def test_list_downloader_torrents(db: None, monkeypatch, factory: AdapterFactory) -> None:
    fake = _FakeDownloaderAdapter()
    _install_fake_downloader(monkeypatch, fake)
    dl_id = await _add_downloader("dl1")

    items = await downloader_service.list_downloader_torrents(dl_id)
    assert len(items) == 1
    assert items[0].state == "seeding"
    assert items[0].hash == "a" * 40


async def test_list_downloader_torrents_invalid_state_400(
    db: None, monkeypatch
, factory: AdapterFactory) -> None:
    """非法 state → 400（不是静默返全量）。"""
    fake = _FakeDownloaderAdapter()
    _install_fake_downloader(monkeypatch, fake)
    dl_id = await _add_downloader("dl1")

    with pytest.raises(ServiceError) as exc:
        await downloader_service.list_downloader_torrents(dl_id, state="foobar")
    assert exc.value.status == 400


async def test_list_downloader_torrents_adapter_error_502(
    db: None, monkeypatch
, factory: AdapterFactory) -> None:
    fake = _FakeDownloaderAdapter(fail=True)
    _install_fake_downloader(monkeypatch, fake)
    dl_id = await _add_downloader("dl1")

    with pytest.raises(ServiceError) as exc:
        await downloader_service.list_downloader_torrents(dl_id)
    assert exc.value.status == 502


async def test_pause_downloader_torrents(db: None, monkeypatch, factory: AdapterFactory) -> None:
    fake = _FakeDownloaderAdapter()
    _install_fake_downloader(monkeypatch, fake)
    dl_id = await _add_downloader("dl1")

    result = await downloader_service.pause_downloader_torrents(
        dl_id, ["a" * 40], paused=True
    )
    assert result.requested == 1
    assert result.succeeded == 1
    assert result.failed == 0
    assert fake.paused == [(["a" * 40], True)]


async def test_remove_downloader_torrents(db: None, monkeypatch, factory: AdapterFactory) -> None:
    fake = _FakeDownloaderAdapter()
    _install_fake_downloader(monkeypatch, fake)
    dl_id = await _add_downloader("dl1")

    result = await downloader_service.remove_downloader_torrents(
        dl_id, ["a" * 40], delete_data=True
    )
    assert result.succeeded == 1
    assert fake.removed == [(["a" * 40], True)]


async def test_batch_op_rejects_bad_hash(db: None, monkeypatch, factory: AdapterFactory) -> None:
    """批量操作必须拒绝 ``all`` / 通配（不误删全部任务）。"""
    fake = _FakeDownloaderAdapter()
    _install_fake_downloader(monkeypatch, fake)
    dl_id = await _add_downloader("dl1")

    with pytest.raises(ServiceError) as exc:
        await downloader_service.remove_downloader_torrents(dl_id, ["all"])
    assert exc.value.status == 400
    assert fake.removed == []


async def test_batch_op_rejects_too_many(db: None, monkeypatch, factory: AdapterFactory) -> None:
    fake = _FakeDownloaderAdapter()
    _install_fake_downloader(monkeypatch, fake)
    dl_id = await _add_downloader("dl1")

    with pytest.raises(ServiceError) as exc:
        await downloader_service.pause_downloader_torrents(
            dl_id, ["a" * 40] * (downloader_service.BATCH_LIMIT + 1)
        )
    assert exc.value.status == 400


async def test_disabled_downloader_rejected(db: None, monkeypatch, factory: AdapterFactory) -> None:
    """已禁用的下载器 → 400（不给操作入口）。"""
    fake = _FakeDownloaderAdapter()
    _install_fake_downloader(monkeypatch, fake)
    dl_id = await _add_downloader("dl1", enabled=False)

    with pytest.raises(ServiceError) as exc:
        await downloader_service.list_downloader_torrents(dl_id)
    assert exc.value.status == 400


# ==================================================================== 仪表盘


async def test_dashboard_counts(db: None) -> None:
    await _add_site("s1")
    await _add_site("s2", enabled=False)
    await _add_downloader("dl1")
    await _add_downloader("dl2", enabled=False)

    site_total, site_enabled, dl_total, dl_enabled = (
        await dashboard_service.site_and_downloader_counts()
    )
    assert (site_total, site_enabled) == (2, 1)
    assert (dl_total, dl_enabled) == (2, 1)


async def test_dashboard_index_total(db: None) -> None:
    site_id = await _add_site("s1")
    async with session_scope() as session:
        session.add(TorrentIndex(site_id=site_id, item_id="i1", name="t"))
        await session.flush()

    assert await dashboard_service.count_index_total() == 1


async def test_dashboard_inflight_zero_without_downloaders(
    db: None, factory: AdapterFactory
) -> None:
    assert await dashboard_service.count_inflight_torrents() == 0


# ==================================================================== 索引


async def test_index_list_and_filter(db: None) -> None:
    site_id = await _add_site("s1")
    async with session_scope() as session:
        session.add(
            TorrentIndex(
                site_id=site_id,
                item_id="i1",
                name="alpha",
                free_type="freeleech_permanent",
            )
        )
        session.add(
            TorrentIndex(site_id=site_id, item_id="i2", name="beta", free_type="freeleech_timed")
        )
        await session.flush()

    page = await index_service.list_index()
    assert page.total == 2
    assert len(page.items) == 2

    by_type = await index_service.list_index(free_type="freeleech_timed")
    assert by_type.total == 1
    assert by_type.items[0].name == "beta"

    by_q = await index_service.list_index(q="alpha")
    assert by_q.total == 1
    assert by_q.items[0].name == "alpha"


async def test_index_entry_not_found_404(db: None) -> None:
    with pytest.raises(ServiceError) as exc:
        await index_service.get_index_entry(999999)
    assert exc.value.status == 404


async def test_index_stats(db: None) -> None:
    site_id = await _add_site("s1")
    async with session_scope() as session:
        session.add(
            TorrentIndex(site_id=site_id, item_id="i1", name="a", free_type="freeleech_timed")
        )
        session.add(
            TorrentIndex(site_id=site_id, item_id="i2", name="b", free_type="freeleech_timed")
        )
        session.add(
            TorrentIndex(site_id=site_id, item_id="i3", name="c", free_type="freeleech_permanent")
        )
        await session.flush()

    stats = await index_service.index_stats()
    assert stats.total == 3
    assert stats.by_type == {"freeleech_timed": 2, "freeleech_permanent": 1}


async def test_index_free_types(db: None) -> None:
    site_id = await _add_site("s1")
    async with session_scope() as session:
        session.add(
            TorrentIndex(site_id=site_id, item_id="i1", name="a", free_type="freeleech_timed")
        )
        await session.flush()

    assert await index_service.list_free_types() == ["freeleech_timed"]
    sites = await index_service.list_index_sites()
    assert sites[0].name == "s1"


async def test_index_push_without_default_downloader_400(db: None, factory: AdapterFactory) -> None:
    """未配置默认下载器 → 400（错误从 push_service 透传）。"""
    site_id = await _add_site("s1")
    async with session_scope() as session:
        session.add(
            TorrentIndex(site_id=site_id, item_id="i1", name="t", free_type="freeleech_permanent")
        )
        await session.flush()
        index_id = (
            await session.execute(
                select(TorrentIndex.id).where(TorrentIndex.item_id == "i1")
            )
        ).scalar_one()

    with pytest.raises(ServiceError) as exc:
        await index_service.push_entry(index_id)
    assert exc.value.status == 400
    assert "未配置默认下载器" in str(exc.value)


# ==================================================================== 作业历史


async def test_job_runs_empty(db: None) -> None:
    page = await job_run_service.list_job_runs()
    assert page.total == 0
    assert page.items == []


async def test_job_run_not_found_404(db: None) -> None:
    with pytest.raises(ServiceError) as exc:
        await job_run_service.get_job_run(999999)
    assert exc.value.status == 404


async def test_job_run_parses_result_json(db: None) -> None:
    from app.db.models import JobRun

    async with session_scope() as session:
        session.add(
            JobRun(
                job_name="j1",
                trigger="manual",
                started_at="2026-01-01 00:00:00",
                ok=True,
                result_json='{"summary": "ok"}',
            )
        )
        await session.flush()

    page = await job_run_service.list_job_runs()
    assert page.total == 1
    assert page.items[0].result == {"summary": "ok"}

    row = await job_run_service.get_job_run(page.items[0].id)
    assert row.result == {"summary": "ok"}


async def test_job_run_bad_json_is_none(db: None) -> None:
    """损坏的 result_json → None（不抛异常，前端显示空）。"""
    from app.db.models import JobRun

    async with session_scope() as session:
        session.add(
            JobRun(
                job_name="j1",
                trigger="cron",
                started_at="2026-01-01 00:00:00",
                ok=True,
                result_json="{not json",
            )
        )
        await session.flush()

    page = await job_run_service.list_job_runs()
    assert page.items[0].result is None
