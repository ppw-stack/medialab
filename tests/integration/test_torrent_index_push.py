"""批量手动推送（``POST /torrent-index/push-batch``）的集成测试。

契约：

- **只入队不同步执行**——执行由 ``drain_task_queue`` 按节流消化
  （每轮 50 条、条间 2s），执行时自动更新 ``push_status``
- **不限数量**——一次勾选几百条是常见操作
- 单条推不了（已推过 / 队列里已有 / 无可用链接 / 索引不存在）→ 计入
  ``skipped``，**不影响其它条目**
- 走与单推**同一份**校验 / 幂等 / 入队逻辑（``enqueue_index_entry``）
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from app.core.config import AppSettings
from app.db.models import DownloaderInstance, Site, TaskQueue, TorrentIndex
from app.db.session import session_scope
from app.main import create_app
from sqlalchemy import select


@pytest_asyncio.fixture
async def client(
    app_settings: AppSettings, db: None
) -> AsyncIterator[httpx.AsyncClient]:
    """已登录的 API 客户端（走 lifespan 初始化）。"""
    from app.db.session import dispose_db

    app = create_app(app_settings)
    await dispose_db()

    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as c,
        app.router.lifespan_context(app),
    ):
        resp = await c.post(
            "/api/v1/auth/login",
            json={"username": "tester", "password": "test-pass-123"},
        )
        assert resp.status_code == 200, resp.text
        yield c


async def _seed(
    *,
    count: int = 3,
    pushed: int = 0,
    no_link: int = 0,
    site_enabled: bool = True,
) -> list[int]:
    """建站点 + 若干拉取索引，返回索引 id 列表。

    :param pushed: 其中多少条标记为已推送（``push_status='pushed'``）
    :param no_link: 其中多少条缺 ``item_id``（无可用下载链接）
    :param site_enabled: 站点是否启用（``False`` = 已停用）
    """
    async with session_scope() as session:
        session.add(
            Site(
                name="site1",
                domain="s1.test",
                adapter="hdsky",
                enabled=site_enabled,
            )
        )
        session.add(
            DownloaderInstance(
                name="dl1",
                type="qbittorrent",
                url="http://dl.test:8080",
                is_default=True,
                enabled=True,
            )
        )
        await session.flush()
        site_id = (
            await session.execute(select(Site.id).where(Site.name == "site1"))
        ).scalar_one()
        ids: list[int] = []
        for i in range(count):
            # 前 pushed 条 = 已推送；接着 no_link 条 = 缺 item_id；其余全新
            no_id = pushed <= i < (pushed + no_link)
            pushed_flag = i < pushed
            item = TorrentIndex(
                site_id=site_id,
                item_id=None if no_id else f"item-{i}",
                name=f"t{i}",
                free_type="freeleech_permanent",
                push_status="pushed" if pushed_flag else None,
            )
            session.add(item)
            await session.flush()
            ids.append(item.id)
        return ids


async def test_batch_push_enqueues_all(client: httpx.AsyncClient) -> None:
    """全新的一批 → 全部入队（task_queue 里出现对应任务）。"""
    ids = await _seed(count=3)

    resp = await client.post(
        "/api/v1/torrent-index/push-batch",
        json={"ids": ids},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert body["enqueued"] == 3
    assert body["skipped"] == 0
    assert body["failed"] == 0
    assert all(i["state"] == "enqueued" for i in body["items"])

    async with session_scope() as session:
        tasks = (
            await session.execute(select(TaskQueue).where(TaskQueue.kind == "add_torrent"))
        ).scalars().all()
        assert len(tasks) == 3
        assert {t.status for t in tasks} == {"pending"}


async def test_batch_push_classifies_skips(client: httpx.AsyncClient) -> None:
    """混合批次：已推送 / 无链接 / 不存在的 id → 分类计数正确，其余照常入队。"""
    ids = await _seed(count=4, pushed=1, no_link=1)
    # 第 4 条全新；再加一个不存在的 id
    missing = 999999

    resp = await client.post(
        "/api/v1/torrent-index/push-batch",
        json={"ids": [ids[0], ids[1], ids[2], ids[3], missing]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    # ids[0]=已推送、ids[1]=无链接 → 跳过 2；missing → 跳过 1
    assert body["skipped"] == 3
    assert body["enqueued"] == 2
    assert body["failed"] == 0

    states = {i["id"]: i["state"] for i in body["items"]}
    assert states[ids[0]] == "skipped_pushed"
    assert states[ids[1]] == "skipped_no_link"
    assert states[missing] == "skipped_not_found"
    assert states[ids[2]] == "enqueued"
    assert states[ids[3]] == "enqueued"


async def test_batch_push_is_idempotent(client: httpx.AsyncClient) -> None:
    """同一批推两次 → 第二次全部「已在队列中」，**不会重复入队**。"""
    ids = await _seed(count=2)

    first = await client.post(
        "/api/v1/torrent-index/push-batch", json={"ids": ids}
    )
    assert first.json()["enqueued"] == 2

    second = await client.post(
        "/api/v1/torrent-index/push-batch", json={"ids": ids}
    )
    body = second.json()
    assert body["enqueued"] == 0
    assert body["skipped"] == 2
    assert all(i["state"] == "skipped_queued" for i in body["items"])

    async with session_scope() as session:
        tasks = (
            await session.execute(select(TaskQueue).where(TaskQueue.kind == "add_torrent"))
        ).scalars().all()
        assert len(tasks) == 2  # 没有重复行（idem_key 唯一约束）


async def test_batch_push_has_no_count_limit(client: httpx.AsyncClient) -> None:
    """**不限数量**——120 条一次全部入队。"""
    ids = await _seed(count=120)

    resp = await client.post(
        "/api/v1/torrent-index/push-batch", json={"ids": ids}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 120
    assert body["enqueued"] == 120

    async with session_scope() as session:
        tasks = (
            await session.execute(select(TaskQueue).where(TaskQueue.kind == "add_torrent"))
        ).scalars().all()
        assert len(tasks) == 120


async def test_batch_push_empty_ids_rejected(client: httpx.AsyncClient) -> None:
    """空列表 → 参数校验拒绝（422）。"""
    resp = await client.post("/api/v1/torrent-index/push-batch", json={"ids": []})
    assert resp.status_code in (400, 422)


async def test_batch_push_without_downloader_400(client: httpx.AsyncClient) -> None:
    """未配置默认下载器 → 整批 400（不是逐条失败）。"""
    ids = await _seed(count=1)
    # 删掉默认下载器
    async with session_scope() as session:
        dl = (
            await session.execute(
                select(DownloaderInstance).where(DownloaderInstance.name == "dl1")
            )
        ).scalar_one()
        await session.delete(dl)

    resp = await client.post(
        "/api/v1/torrent-index/push-batch", json={"ids": ids}
    )
    assert resp.status_code == 400
    assert "未配置默认下载器" in resp.json()["detail"]


async def test_batch_push_does_not_mark_pushed(client: httpx.AsyncClient) -> None:
    """入队**不**改 ``push_status``——执行器执行后才更新。

    这样列表上「推送状态」在队列消化前保持原样，不假装已推送。
    """
    ids = await _seed(count=1)

    resp = await client.post(
        "/api/v1/torrent-index/push-batch", json={"ids": ids}
    )
    assert resp.status_code == 200

    async with session_scope() as session:
        item = (
            await session.execute(
                select(TorrentIndex).where(TorrentIndex.id == ids[0])
            )
        ).scalar_one()
        assert item.push_status is None
        assert item.pushed_at is None


async def test_batch_push_skips_disabled_site(client: httpx.AsyncClient) -> None:
    """**站点已停用 → 跳过，不入队**（2026-11-12）。

    站点停用的语义是「这个站不再推种子」。若还入队，任务会被 drain
    执行成 fatal 死信——既没推成，又占住幂等键（重新启用站点后该种子
    要等队列清理才能重推）。故在**入队前**就拦掉。
    """
    ids = await _seed(count=2, site_enabled=False)

    resp = await client.post(
        "/api/v1/torrent-index/push-batch", json={"ids": ids}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["enqueued"] == 0
    assert body["skipped"] == 2
    assert body["failed"] == 0
    assert all(i["state"] == "skipped_site_disabled" for i in body["items"])
    # 文案带站点名，用户知道是谁停了
    assert "已停用" in body["items"][0]["detail"]

    # 队列里一个任务都没有
    async with session_scope() as session:
        tasks = (
            await session.execute(select(TaskQueue).where(TaskQueue.kind == "add_torrent"))
        ).scalars().all()
        assert tasks == []


async def test_single_push_of_disabled_site_is_rejected(
    client: httpx.AsyncClient,
) -> None:
    """单条手动推送停用站点的种子 → **400**（不是 502 推送失败）。

    400 = 请求本身不该发（站点已停用）；502 是「尝试推送但失败了」——
    两者对用户的含义不同，故在入队校验层就拦。
    """
    ids = await _seed(count=1, site_enabled=False)

    resp = await client.post(f"/api/v1/torrent-index/{ids[0]}/push", json={})

    assert resp.status_code == 400
    assert "已停用" in resp.json()["detail"]
