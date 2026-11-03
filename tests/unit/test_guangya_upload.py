"""光鸭上传驱动的单元测试。

**不连真网盘**：用假的 ``guangyaclient`` 替身，验证编排逻辑：

- 小文件走「带 md5 的 upload_token」+ OSS 单 PUT
- 大文件走分片
- ``upload_info`` 轮询直到拿到 fileId
- 轮询超时 → 报错
- 删除接口错误码 → 报错
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, ClassVar

import pytest
from app.adapters.drivers.base import DriverError
from app.adapters.drivers.guangya_upload import (
    CODE_UPLOADING,
    SMALL_FILE_THRESHOLD,
    GuangyaUploader,
)


class FakeClient:
    """假的 ``guangyaclient.GuangyaClient``。"""

    def __init__(self) -> None:
        #: ``upload_token`` 的调用记录
        self.token_calls: list[dict[str, Any]] = []
        #: OSS 单 PUT 的调用记录
        self.put_calls: list[dict[str, Any]] = []
        #: 分片上传的调用记录
        self.cdn_calls: list[dict[str, Any]] = []
        #: 删除的 fileId
        self.deleted: list[str] = []
        #: ``upload_info`` 的轮询序列（每次调用弹一个）
        self.info_queue: list[dict[str, Any]] = []
        #: ``upload_token`` 的返回覆盖
        self.token_response: dict[str, Any] | None = None
        #: 各方法的错误注入
        self.errors: dict[str, Exception] = {}
        #: **同时在飞的 OSS 请求数**（峰值）——测并发用
        self.max_inflight = 0
        self._inflight = 0
        #: 每个分片 PUT 的**完成顺序**（partNumber 序列）
        self.part_order: list[int] = []
        #: 分片 PUT 的**开始顺序**（partNumber 序列）——测并发用
        self.part_start_order: list[int] = []
        #: 每次分片 PUT 的耗时（秒），用于制造乱序完成
        self.part_delay: dict[int, float] = {}
        #: 合并请求的 XML body
        self.complete_body: bytes = b""

    def upload_token(
        self,
        name: str,
        file_size: int,
        parent_id: str | None = None,
        md5: str | None = None,
    ) -> dict[str, Any]:
        """取上传凭证。"""
        if "upload_token" in self.errors:
            raise self.errors["upload_token"]
        self.token_calls.append(
            {"name": name, "size": file_size, "parent_id": parent_id, "md5": md5}
        )
        if self.token_response is not None:
            return self.token_response
        return {
            "code": 0,
            "msg": "success",
            "data": {
                "taskId": "task-1",
                "creds": {
                    "accessKeyID": "ak",
                    "secretAccessKey": "sk",
                    "sessionToken": "st",
                },
                "objectPath": "upload_tmp/obj",
                "bucketName": "bucket",
                "fullEndPoint": "https://oss.example.com",
            },
        }

    def oss_request(self, method: str, url: str, *args: Any, **kwargs: Any) -> Any:
        """OSS 请求（单 PUT / 分片初始化 / 分片上传 / 合并）。"""
        if "oss_request" in self.errors:
            raise self.errors["oss_request"]
        self.put_calls.append({"method": method, "url": url, **kwargs})
        if method == "POST" and "uploads" in url:
            # 分片初始化 → 返回 UploadId
            return _FakeResponse(
                content=b'<?xml version="1.0"?><InitiateMultipartUploadResult>'
                b"<UploadId>upload-id-1</UploadId>"
                b"</InitiateMultipartUploadResult>"
            )
        if method == "PUT" and "partNumber=" in url:
            # 分片 PUT：记录并发情况 + 制造乱序完成
            pn = int(url.split("partNumber=")[1].split("&")[0])
            self.part_start_order.append(pn)
            self._inflight += 1
            self.max_inflight = max(self.max_inflight, self._inflight)
            delay = self.part_delay.get(pn, 0.0)
            if delay:
                time.sleep(delay)
            self._inflight -= 1
            self.part_order.append(pn)
            return _FakeResponse()
        if method == "POST" and "uploadId=" in url:
            self.complete_body = kwargs.get("content", b"")
            return _FakeResponse()
        return _FakeResponse()

    def cdn_upload(self, path: Any, token_data: Any, **kwargs: Any) -> str:
        """OSS 分片上传。"""
        if "cdn_upload" in self.errors:
            raise self.errors["cdn_upload"]
        self.cdn_calls.append({"path": path, "token": token_data, **kwargs})
        return "etag-1"

    def upload_info(self, task_id: str) -> dict[str, Any]:
        """轮询上传状态。"""
        if "upload_info" in self.errors:
            raise self.errors["upload_info"]
        if self.info_queue:
            return self.info_queue.pop(0)
        return {
            "code": 0,
            "msg": "success",
            "data": {"fileId": "fid-1", "fileName": "a.bin", "fileSize": 10},
        }

    def fs_delete(self, file_ids: list[str]) -> dict[str, Any]:
        """删除文件。"""
        if "fs_delete" in self.errors:
            raise self.errors["fs_delete"]
        self.deleted.extend(file_ids)
        return {"code": 0, "msg": "success", "data": {"taskId": "t"}}  # type: ignore[return-value]


class _FakeResponse:
    """假的 OSS 响应。"""

    status_code = 200
    text = ""
    headers: ClassVar[dict[str, str]] = {"etag": '"abc"'}

    def __init__(self, content: bytes = b"") -> None:
        self.content = content


@pytest.fixture
def client() -> FakeClient:
    """假 client。"""
    return FakeClient()


@pytest.fixture
def uploader(client: FakeClient) -> GuangyaUploader:
    """上传器。"""
    return GuangyaUploader(client)


async def test_小文件带md5并单PUT(uploader: GuangyaUploader, client: FakeClient, tmp_path: Path):
    """小文件 → upload_token 带 md5 → OSS 单 PUT → 轮询拿 fileId。"""
    path = tmp_path / "a.bin"
    path.write_bytes(b"x" * 100)

    result = await uploader.upload_file(path, "dir-1")

    # token 带了 md5
    call = client.token_calls[0]
    assert call["name"] == "a.bin"
    assert call["parent_id"] == "dir-1"
    expected_md5 = base64.b64encode(hashlib.md5(b"x" * 100).digest()).decode()
    assert call["md5"] == expected_md5

    # 走了单 PUT
    assert len(client.put_calls) == 1
    assert client.put_calls[0]["method"] == "PUT"
    assert client.cdn_calls == []

    assert result.file_id == "fid-1"
    assert result.size_uploaded == 100


async def test_大文件走分片(uploader: GuangyaUploader, client: FakeClient, tmp_path: Path):
    """大文件 → 分片上传（初始化 → 逐片 → 合并）。"""
    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * (SMALL_FILE_THRESHOLD + 1))

    await uploader.upload_file(path, "dir-1")

    # 不带 md5（大文件走 OSS 分片）
    assert client.token_calls[0]["md5"] is None
    # 三步：初始化（POST uploads）→ 分片（PUT partNumber）→ 合并（POST uploadId）
    methods = [c["method"] for c in client.put_calls]
    assert methods[0] == "POST"
    assert "partNumber=1" in client.put_calls[1]["url"]
    assert methods[-1] == "POST"
    assert "uploadId=upload-id-1" in client.put_calls[-1]["url"]


async def test_分片上传有进度回调(uploader: GuangyaUploader, client: FakeClient, tmp_path: Path):
    """**回归测试**：大文件上传必须汇报进度。

    没有进度 → 大文件传几分钟，用户以为卡死了。
    """
    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * (SMALL_FILE_THRESHOLD * 2))

    progress: list[int] = []
    await uploader.upload_file(path, "dir-1", progress=progress.append)

    assert progress, "没有收到任何进度回调"
    assert progress[-1] == SMALL_FILE_THRESHOLD * 2
    # 单调递增
    assert progress == sorted(progress)


async def test_读文件不阻塞事件循环(
    uploader: GuangyaUploader, client: FakeClient, tmp_path: Path
):
    """**回归测试**：读本地文件必须**放线程池**，不能阻塞事件循环。

    prod 实测事故：分片读在事件循环里同步做（``source.read()``），
    CIFS 上的阻塞把**整个事件循环**卡住——healthz 无响应、API 全部
    超时、容器停止时收不到退出事件（日志完全停更 5 分钟以上）。

    本地目录在 CIFS 上，读是**同步阻塞**的，必须挪到线程。
    """
    from app.adapters.drivers.guangya_upload import CHUNK_SIZE

    size = CHUNK_SIZE * 3
    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * size)

    # **模拟 CIFS 的慢读**——本地 tmpfs 读太快，测不出阻塞。
    # 真实环境里目录在 CIFS 上，读是网络往返（几十~几百 ms）。
    real_open = Path.open

    def _slow_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        fh = real_open(self, *args, **kwargs)
        real_read = fh.read

        def _slow_read(n: int = -1) -> bytes:
            data = real_read(n)
            time.sleep(0.05)   # 每次读阻塞 50ms
            return data

        fh.read = _slow_read  # type: ignore[method-assign]
        return fh

    # 并行跑一个「探针」协程——若事件循环被阻塞，它就不会推进
    ticks = 0

    async def _probe() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    Path.open = _slow_open  # type: ignore[method-assign]
    probe = asyncio.create_task(_probe())
    try:
        # 先让探针跑起来（否则它一次都没被调度，ticks 恒为 0）
        await asyncio.sleep(0.05)
        before = ticks
        await uploader.upload_file(path, "dir-1")
    finally:
        Path.open = real_open  # type: ignore[method-assign]
        probe.cancel()
        with suppress(asyncio.CancelledError):
            await probe

    # 传 3 片（每片慢读 50ms）期间，探针应该跑了几十次
    moved = ticks - before
    assert moved > 3, f"事件循环被阻塞（探针只跑了 {moved} 次）"


async def test_分片是串行的(uploader: GuangyaUploader, client: FakeClient, tmp_path: Path):
    """**回归测试**：分片上传必须**串行**，不能并发。

    实测 prod：光鸭 OSS **不支持同一 uploadId 的并发分片**——
    并发 2 起就直接卡死（200s 无响应，报
    ``The write operation timed out``）。

    实测记录（100MB / 20 片）：
      纯串行 4 轮：24.28s / 26.89s / 25.70s / 17.08s  ← 稳定
      并发 2 一次：❌ 卡死
      并发 4：第 1 轮 44.72s（比串行慢一倍），第 2 轮 ❌ 卡死
    """
    from app.adapters.drivers.guangya_upload import CHUNK_CONCURRENCY, CHUNK_SIZE

    assert CHUNK_CONCURRENCY == 1, "分片并发必须关着（光鸭 OSS 不支持）"

    size = CHUNK_SIZE * 5
    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * size)

    client.part_delay = dict.fromkeys(range(1, 99), 0.01)

    await uploader.upload_file(path, "dir-1")

    assert client.max_inflight == 1, f"分片不应并发（max_inflight={client.max_inflight}）"


async def test_合并按分片号排序(uploader: GuangyaUploader, client: FakeClient, tmp_path: Path):
    """**回归测试**：合并 XML 必须按分片号升序。

    OSS 的 ``CompleteMultipartUpload`` 要求 Part 按 PartNumber 升序——
    顺序错了服务端会拒绝合并（或合并出损坏文件）。
    """
    from app.adapters.drivers.guangya_upload import CHUNK_SIZE

    size = CHUNK_SIZE * 4
    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * size)

    await uploader.upload_file(path, "dir-1")

    body = client.complete_body.decode("utf-8")
    numbers = [
        int(x.split("</PartNumber>")[0])
        for x in body.split("<PartNumber>")[1:]
    ]
    assert numbers == sorted(numbers), f"合并顺序错了：{numbers}"
    assert numbers == list(range(1, len(numbers) + 1)), f"分片号不连续：{numbers}"


async def test_分片全部上传(uploader: GuangyaUploader, client: FakeClient, tmp_path: Path):
    """不能丢片——每个分片都要发出去。"""
    from app.adapters.drivers.guangya_upload import CHUNK_SIZE

    size = CHUNK_SIZE * 5
    path = tmp_path / "big.bin"
    path.write_bytes(b"x" * size)

    await uploader.upload_file(path, "dir-1")

    puts = [c for c in client.put_calls if "partNumber=" in c["url"]]
    assert len(puts) == size // CHUNK_SIZE


async def test_轮询直到完成(uploader: GuangyaUploader, client: FakeClient, tmp_path: Path):
    path = tmp_path / "a.bin"
    path.write_bytes(b"x")
    client.info_queue = [
        {"code": CODE_UPLOADING, "msg": "文件上传中"},
        {"code": CODE_UPLOADING, "msg": "文件上传中"},
        {
            "code": 0,
            "msg": "success",
            "data": {"fileId": "fid-9", "fileName": "a.bin", "fileSize": 1},
        },
    ]

    result = await uploader.upload_file(path, "dir-1")

    assert result.file_id == "fid-9"
    assert result.name == "a.bin"


async def test_上传失败抛错(uploader: GuangyaUploader, client: FakeClient, tmp_path: Path):
    """``upload_info`` 报错码 → 抛 DriverError。"""
    path = tmp_path / "a.bin"
    path.write_bytes(b"x")
    client.info_queue = [{"code": 999, "msg": "失败了"}]

    with pytest.raises(DriverError, match="上传失败"):
        await uploader.upload_file(path, "dir-1")


async def test_无taskId抛错(uploader: GuangyaUploader, client: FakeClient, tmp_path: Path):
    """token 没返回 taskId → 抛错。"""
    path = tmp_path / "a.bin"
    path.write_bytes(b"x")
    client.token_response = {"code": 0, "data": {"creds": {}}}

    with pytest.raises(DriverError, match="taskId"):
        await uploader.upload_file(path, "dir-1")


async def test_凭证缺字段抛错(uploader: GuangyaUploader, client: FakeClient, tmp_path: Path):
    """OSS 目标信息不全 → 抛错。"""
    path = tmp_path / "a.bin"
    path.write_bytes(b"x")
    client.token_response = {
        "code": 0,
        "data": {"taskId": "t", "creds": {"accessKeyID": "ak"}},
    }

    with pytest.raises(DriverError, match="凭证不完整"):
        await uploader.upload_file(path, "dir-1")


async def test_token报错抛错(uploader: GuangyaUploader, client: FakeClient, tmp_path: Path):
    """``upload_token`` 报错码 → 抛错。"""
    path = tmp_path / "a.bin"
    path.write_bytes(b"x")
    client.token_response = {"code": 146, "msg": "文件不存在"}

    with pytest.raises(DriverError, match="取上传凭证失败"):
        await uploader.upload_file(path, "dir-1")


async def test_文件不存在抛错(uploader: GuangyaUploader, tmp_path: Path):
    """本地文件读不到 → 抛错。"""
    with pytest.raises(DriverError, match="读取本地文件失败"):
        await uploader.upload_file(tmp_path / "nope.bin", "dir-1")


async def test_删除成功(uploader: GuangyaUploader, client: FakeClient):
    """删除 → 调 fs_delete。"""
    await uploader.delete_file("fid-1")
    assert client.deleted == ["fid-1"]


async def test_删除失败抛错(uploader: GuangyaUploader, client: FakeClient):
    """删除报错码 → 抛错。"""
    client.errors["fs_delete"] = RuntimeError("网络错误")
    with pytest.raises(DriverError, match="fs_delete"):
        await uploader.delete_file("fid-1")
