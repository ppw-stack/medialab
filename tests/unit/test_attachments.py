"""附件同步测试。

覆盖点：

- 新文件下载落盘、大小校验、``.part`` 清理
- 幂等：本地已有且大小一致 → 跳过（**不**再换直链，省 115 的 1s/次配额）
- 失败隔离：换直链失败 / HTTP 错误 / 大小不符都只返回 ``failed``，不抛异常
- UA 固定：115 直链 ``f=1`` 绑 UA，下载用什么 UA 就得用什么
"""

from __future__ import annotations

import httpx
from app.adapters.drivers.fake import FakeDriver
from app.services import attachments
from app.services.attachments import (
    ATTACHMENT_USER_AGENT,
    PART_SUFFIX,
    sync_attachment,
)


def _add(fake_driver: FakeDriver, *, size: int = 4, name: str = "poster.jpg"):
    return fake_driver.add_file("pc1", name, path=f"/media/x/{name}", size=size)


class TestSyncAttachment:
    async def test_downloads_new_file(
        self, fake_driver: FakeDriver, http_mock, tmp_path
    ):
        cloud_file = _add(fake_driver, size=4)
        target = tmp_path / "x" / "poster.jpg"

        async with http_mock(b"JPEG") as http:
            result = await sync_attachment(fake_driver, cloud_file, target, http=http)

        assert result.status == attachments.SYNCED
        assert result.error is None
        # 父目录自动创建
        assert target.read_bytes() == b"JPEG"

    async def test_skips_when_local_size_matches(
        self, fake_driver: FakeDriver, http_mock, tmp_path
    ):
        """本地已有且大小一致 → 不下载、**也不换直链**。"""
        cloud_file = _add(fake_driver, size=4)
        target = tmp_path / "poster.jpg"
        target.write_bytes(b"JPEG")

        async with http_mock("改成别的".encode()) as http:
            result = await sync_attachment(fake_driver, cloud_file, target, http=http)

        assert result.status == attachments.UNCHANGED
        # 关键：省掉了 1s/次的换直链配额
        assert "pc1" not in fake_driver.call_counts
        assert target.read_bytes() == b"JPEG"

    async def test_redownloads_when_size_differs(
        self, fake_driver: FakeDriver, http_mock, tmp_path
    ):
        """本地文件大小与云盘不一致 → 视为过期，重下。"""
        cloud_file = _add(fake_driver, size=4)
        target = tmp_path / "poster.jpg"
        target.write_bytes(b"old")  # 3 字节 ≠ 4

        async with http_mock(b"JPEG") as http:
            result = await sync_attachment(fake_driver, cloud_file, target, http=http)

        assert result.status == attachments.SYNCED
        assert target.read_bytes() == b"JPEG"

    async def test_size_mismatch_fails_without_leaving_file(
        self, fake_driver: FakeDriver, http_mock, tmp_path
    ):
        """下载字节数与云盘 size 不符 → 失败，且**不留下半个文件**。"""
        cloud_file = _add(fake_driver, size=100)
        target = tmp_path / "poster.jpg"

        async with http_mock("短".encode()) as http:
            result = await sync_attachment(fake_driver, cloud_file, target, http=http)

        assert result.status == attachments.FAILED
        assert "大小不符" in (result.error or "")
        assert not target.exists()
        assert not target.with_name(target.name + PART_SUFFIX).exists()

    async def test_http_error_fails(
        self, fake_driver: FakeDriver, http_mock, tmp_path
    ):
        cloud_file = _add(fake_driver)
        target = tmp_path / "poster.jpg"

        async with http_mock(b"not found", status=404) as http:
            result = await sync_attachment(fake_driver, cloud_file, target, http=http)

        assert result.status == attachments.FAILED
        assert "404" in (result.error or "")
        assert not target.exists()

    async def test_direct_link_error_fails(
        self, fake_driver: FakeDriver, http_mock, tmp_path
    ):
        """换直链失败（UA 不匹配）→ failed，不抛异常。"""
        cloud_file = _add(fake_driver)
        fake_driver.bound_ua["pc1"] = "SomeOtherPlayer/9.9"
        target = tmp_path / "poster.jpg"

        async with http_mock(b"JPEG") as http:
            result = await sync_attachment(fake_driver, cloud_file, target, http=http)

        assert result.status == attachments.FAILED
        assert "换直链失败" in (result.error or "")

    async def test_cleans_stale_part_file(
        self, fake_driver: FakeDriver, http_mock, tmp_path
    ):
        """上次中断留下的 ``.part`` 会在这次失败时被清掉。"""
        cloud_file = _add(fake_driver, size=100)
        target = tmp_path / "poster.jpg"
        part = target.with_name(target.name + PART_SUFFIX)
        part.write_bytes("半截".encode())

        async with http_mock("短".encode()) as http:
            result = await sync_attachment(fake_driver, cloud_file, target, http=http)

        assert result.status == attachments.FAILED
        assert not part.exists()

    async def test_uses_fixed_user_agent(
        self, fake_driver: FakeDriver, http_mock, tmp_path
    ):
        """UA 必须与换直链时一致——115 ``f=1`` 绑定 UA。

        把驱动绑定到「附件专用 UA」后下载应当成功；
        绑定到别的 UA 则失败（上一条用例已覆盖反向）。
        """
        cloud_file = _add(fake_driver)
        fake_driver.bound_ua["pc1"] = ATTACHMENT_USER_AGENT
        target = tmp_path / "poster.jpg"

        async with http_mock(b"JPEG") as http:
            result = await sync_attachment(fake_driver, cloud_file, target, http=http)

        assert result.status == attachments.SYNCED


class TestRetry:
    async def test_retries_then_succeeds(
        self, fake_driver: FakeDriver, tmp_path
    ):
        """前两次失败、第三次成功——**重新换直链**再试。

        实测光鸭 CDN 会中途掐断连接，不重试就会丢文件。
        """
        cloud_file = _add(fake_driver)
        target = tmp_path / "poster.jpg"
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] < 3:
                # 模拟连接被掐断
                raise httpx.ReadError("peer closed connection", request=request)
            return httpx.Response(200, content=b"JPEG")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http:
            result = await sync_attachment(
                fake_driver,
                cloud_file,
                target,
                http=http,
                max_retries=3,
            )

        assert result.status == attachments.SYNCED
        assert attempts["n"] == 3
        assert target.read_bytes() == b"JPEG"
        # 每次重试都重新换了直链
        assert fake_driver.call_counts["pc1"] == 3

    async def test_gives_up_after_max_retries(
        self, fake_driver: FakeDriver, tmp_path
    ):
        """一直失败 → 返回 failed，且**不留下半个文件**。"""
        cloud_file = _add(fake_driver)
        target = tmp_path / "poster.jpg"

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadError("peer closed connection", request=request)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http:
            result = await sync_attachment(
                fake_driver,
                cloud_file,
                target,
                http=http,
                max_retries=2,
            )

        assert result.status == attachments.FAILED
        assert not target.exists()
        assert not target.with_name(target.name + PART_SUFFIX).exists()

    async def test_no_retry_when_already_up_to_date(
        self, fake_driver: FakeDriver, http_mock, tmp_path
    ):
        """本地已是最新 → 连直链都不换，更不会重试。"""
        cloud_file = _add(fake_driver, size=4)
        target = tmp_path / "poster.jpg"
        target.write_bytes(b"JPEG")

        async with http_mock(b"X") as http:
            result = await sync_attachment(fake_driver, cloud_file, target, http=http)

        assert result.status == attachments.UNCHANGED
        assert "pc1" not in fake_driver.call_counts


class TestAttachmentTimeout:
    """附件下载的独立超时（2026-11-12 生产实测后固化）。

    附件是 KB~MB 级小文件，共享 client 的 60s 超时会让一个卡死的连接
    占住下载 worker 60s（3 次重试 = 3 分钟长尾）——实测把扫描速率拖到
    21 个/分钟。故附件按请求覆盖成 15s。
    """

    async def test_uses_short_timeout(
        self, fake_driver: FakeDriver, tmp_path
    ):
        cloud_file = _add(fake_driver, size=4)
        target = tmp_path / "x" / "poster.jpg"
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["timeout"] = request.extensions.get("timeout")
            return httpx.Response(200, content=b"JPEG")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ) as http:
            result = await sync_attachment(fake_driver, cloud_file, target, http=http)

        assert result.status == attachments.SYNCED
        timeout = seen.get("timeout")
        assert isinstance(timeout, dict)
        assert timeout["read"] == attachments.ATTACHMENT_TIMEOUT.read
        assert timeout["connect"] == attachments.ATTACHMENT_TIMEOUT.connect
        # 比共享 client 的 60s 短，但够慢 CDN 节点（15s 实测失败率 17%）
        assert timeout["read"] == 45.0
