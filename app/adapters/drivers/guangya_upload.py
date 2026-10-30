"""光鸭盘**上传**能力（本地文件 → 光鸭盘）。

与 :mod:`app.adapters.drivers.guangya` 拆开：那个模块管「读」（列目录 /
换直链 / 离线下载），本模块管「写」（上传 / 删除）。

**实测结论（2026-10-30，真实账号）**——这些数字别凭记忆改：

=========================================================  ==========================
事项                                                        实测结果
=========================================================  ==========================
``upload_token(name, size, parent_id, md5=)``               返回 ``data.taskId`` +
                                                             ``data.creds``（OSS STS）
                                                             + ``data.objectPath`` /
                                                             ``data.bucketName`` /
                                                             ``data.fullEndPoint``
``upload_info(taskId)``                                     轮询用。**进行中返回
                                                             ``code=147 msg=文件上传中``**，
                                                             完成返回 ``code=0`` 且
                                                             ``data.fileId`` 有值
**上传完成后必须轮询**                                        不轮询就不知道 fileId，
                                                             也无法确认文件真的落盘
**小文件（< 1MB）**                                          ``upload_token`` 必须带
                                                             ``md5``（base64），否则
                                                             服务端不认
**OSS 单 PUT**                                               ``oss_request("PUT", ...)``
                                                             直传小文件，返 ``ETag``
**OSS 分片**                                                 ``cdn_upload()``：5MB/片，
                                                             实测 12MB 用 2.3 秒
**同名上传不会覆盖**                                          光鸭自动改名成
                                                             ``xxx(1).bin``！要「覆盖」
                                                             必须**先删旧再传新**
**秒传不可用**                                                ``check_can_flash_upload``
                                                             返回体**没有** ``data``
                                                             字段（只有 ``msg``），
                                                             无法判定是否命中；且
                                                             ``upload_token`` 返回的
                                                             ``gcid`` 末尾带 ``TEMP``
                                                             与本地算出的 GCID 不同。
                                                             故**不依赖秒传**
**``fs_delete`` 立即生效**                                    返回 ``taskId`` 但实测
                                                             < 0.5 秒目录里就没了。
                                                             仍建议删后**重列确认**
=========================================================  ==========================

**为什么不用 ``guangyaclient.file_upload()``**：它内部对 >1MB 的文件会调
``check_can_flash_upload`` 并在返回体里读 ``data.canFlashUpload``——实测该
字段不存在，会抛 ``KeyError``；且它**不返回 taskId**，调用方无法轮询确认。
故本模块自己编排：``upload_token`` → OSS 传字节 → ``upload_info`` 轮询。

**目录浏览不在本模块**：列目录 / 建目录用
:class:`app.adapters.drivers.guangya.GuangyaDriver`（它已有
``list_files`` / ``ensure_dir``）。本模块只管「传字节」与「删文件」。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.adapters.drivers.base import DriverError
from app.adapters.drivers.guangya import PROVIDER
from app.core.logging import get_logger
from app.core.rate_limit import rate_limiter

logger = get_logger(__name__)

#: 小文件阈值（字节）：小于它走 OSS 单 PUT，不分片
#:
#: 与 ``guangyaclient.file_upload`` 的阈值一致（1MB）。低于此值
#: ``upload_token`` **必须**带 ``md5``（base64），否则服务端不认。
SMALL_FILE_THRESHOLD = 1024 * 1024

#: OSS 分片大小（字节）
CHUNK_SIZE = 5 * 1024 * 1024

#: OSS 请求超时（秒）
#:
#: **必须调长**：``guangyaclient`` 内部 httpx Client 用默认 timeout
#: （5 秒），OSS 分片 PUT 在慢速链路上经常超过 → 报
#: ``The write operation timed out``，大文件传到一半失败（实测）。
OSS_TIMEOUT_SEC = 300.0

#: ``upload_info`` 返回「文件上传中」的业务码
CODE_UPLOADING = 147

#: 轮询间隔（秒）
POLL_INTERVAL_SEC = 1.0

#: 轮询上限（秒）——超时即判失败
POLL_TIMEOUT_SEC = 3600.0


@dataclass(frozen=True)
class UploadResult:
    """一次上传的结果。

    Attributes:
        file_id: 光鸭侧的文件 id（上传完成后由 ``upload_info`` 返回）。
        name: 云端文件名（**可能与请求名不同**——同名时光鸭会自动加
            ``(1)`` 后缀，故这个值才是真实落地的名字）。
        size: 字节数。
        size_uploaded: 实际传输的字节数。
    """

    file_id: str
    name: str
    size: int
    size_uploaded: int = 0


class GuangyaUploader:
    """光鸭上传器（包 ``guangyaclient`` 的同步能力为异步）。"""

    def __init__(self, client: Any) -> None:
        """构造上传器。

        Args:
            client: ``guangyaclient.GuangyaClient`` 实例（**已登录**）。
        """
        self._client = client
        self._ensure_oss_timeout()

    def _ensure_oss_timeout(self) -> None:
        """把 OSS 请求的超时调长（**默认 5 秒不够**）。

        ``guangyaclient`` 内部的 httpx Client 用的是**默认 timeout（5 秒）**。
        OSS 分片 PUT 一个 5MB 片在慢速链路上经常超过 5 秒 → 报
        ``The write operation timed out``，大文件传到一半失败。

        调到 300 秒。**不影响业务接口**（那些请求本来就快）。
        """
        inner = getattr(self._client, "_client", None)
        if inner is None:
            return
        try:
            inner.timeout = httpx.Timeout(OSS_TIMEOUT_SEC, connect=30.0)
        except Exception:  # pragma: no cover - 防御性
            logger.debug("设置 OSS 超时失败（用默认值）", exc_info=True)

    # -- 底层调用 --

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """调一次业务接口（限速 + 错误映射）。"""
        await rate_limiter.acquire(PROVIDER)
        try:
            result = await asyncio.to_thread(
                getattr(self._client, method), *args, **kwargs
            )
        except Exception as exc:
            raise DriverError(f"光鸭接口 {method} 调用失败：{exc}") from exc
        return result if isinstance(result, dict) else {}

    async def _call_oss(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """调 OSS 相关方法（同样限速——OSS 是光鸭的资源）。"""
        await rate_limiter.acquire(PROVIDER)
        try:
            return await asyncio.to_thread(
                getattr(self._client, method), *args, **kwargs
            )
        except Exception as exc:
            raise DriverError(f"光鸭 OSS 调用失败：{exc}") from exc

    # -- 上传 --

    async def upload_file(
        self,
        path: Path,
        parent_id: str,
        *,
        name: str | None = None,
        progress: Callable[[int], None] | None = None,
    ) -> UploadResult:
        """把本地文件上传到光鸭的 ``parent_id`` 目录。

        流程（实测）::

            upload_token → OSS 传字节 → upload_info 轮询到 fileId

        Args:
            path: 本地文件路径。
            parent_id: 目标目录的 fileId。
            name: 云端文件名。默认用本地文件名。
            progress: 进度回调（已传字节数）——大文件上传要几分钟。

        Returns:
            上传结果（含**真实落地**的文件名）。

        Raises:
            DriverError: 取 token / 传字节 / 轮询确认任一步失败。
        """
        try:
            stat = path.stat()
        except OSError as exc:
            raise DriverError(f"读取本地文件失败：{path}（{exc}）") from exc

        size = stat.st_size
        file_name = name or path.name

        token = await self._upload_token(file_name, size, parent_id, path)
        data = token.get("data") or {}
        task_id = str(data.get("taskId") or "")
        if not task_id:
            raise DriverError("光鸭上传未返回 taskId")

        uploaded = 0
        if data.get("creds"):
            # 需要真的传字节
            uploaded = await self._transfer(path, token, size, progress=progress)
        # 没给 creds 说明服务端认为不需要传——仍要轮询确认落盘

        info = await self._wait_for_upload(task_id)
        result_data = info.get("data") or {}
        file_id = str(result_data.get("fileId") or "")
        if not file_id:
            raise DriverError("光鸭上传完成但未返回 fileId")

        return UploadResult(
            file_id=file_id,
            name=str(result_data.get("fileName") or file_name),
            size=int(result_data.get("fileSize") or size),
            size_uploaded=uploaded,
        )

    async def _upload_token(
        self, name: str, size: int, parent_id: str, path: Path
    ) -> dict[str, Any]:
        """取上传凭证。

        **小文件必须带 md5**（base64）——不带服务端不认（实测）。
        """
        kwargs: dict[str, Any] = {"parent_id": str(parent_id or "")}
        if size < SMALL_FILE_THRESHOLD:
            kwargs["md5"] = await self._file_md5(path)
        payload = await self._call("upload_token", name, size, **kwargs)
        code = payload.get("code")
        if code not in (None, 0):
            raise DriverError(
                f"光鸭取上传凭证失败：{payload.get('msg') or payload}",
                kind=_map_code(code),
            )
        return payload

    async def _file_md5(self, path: Path) -> str:
        """算文件的 base64 MD5（小文件直传要用）。"""

        def _calc() -> str:
            digest = hashlib.md5()
            with path.open("rb") as fh:
                while chunk := fh.read(1024 * 1024):
                    digest.update(chunk)
            return base64.b64encode(digest.digest()).decode("ascii")

        try:
            return await asyncio.to_thread(_calc)
        except OSError as exc:
            raise DriverError(f"计算 MD5 失败：{path}（{exc}）") from exc

    async def _transfer(
        self,
        path: Path,
        token: dict[str, Any],
        size: int,
        progress: Callable[[int], None] | None = None,
    ) -> int:
        """把字节传到 OSS。返回实际传输字节数。

        Args:
            progress: 进度回调（已传字节数）——大文件上传要几分钟，
                没有进度用户会以为卡死了。
        """
        data = token.get("data") or {}
        creds = data.get("creds") or {}
        endpoint = str(data.get("fullEndPoint") or data.get("endPoint") or "")
        object_path = str(data.get("objectPath") or "")
        bucket = str(data.get("bucketName") or "")
        if not endpoint or not object_path or not bucket:
            raise DriverError("光鸭上传凭证不完整（缺 OSS 目标信息）")

        access_key = str(creds.get("accessKeyID") or "")
        secret_key = str(creds.get("secretAccessKey") or "")
        session_token = str(creds.get("sessionToken") or "")
        if not access_key or not secret_key or not session_token:
            raise DriverError("光鸭上传凭证不完整（缺 STS 凭据）")

        if size < SMALL_FILE_THRESHOLD:
            # 小文件：单 PUT 直传
            await self._call_oss(
                "oss_request",
                "PUT",
                f"{endpoint}/{object_path}",
                bucket,
                object_path,
                access_key,
                secret_key,
                session_token,
                content=path.read_bytes(),
                content_type="application/octet-stream",
            )
            if progress is not None:
                progress(size)
            return size

        # 大文件：分片上传
        #
        # **不用 ``cdn_upload``**：它没有进度回调（传完才知道结果），
        # 大文件传几分钟没有进度 → 用户以为卡死了。故自己分片，
        # 每片传完报一次进度。
        await self._multipart_upload(
            path,
            data,
            size,
            endpoint=endpoint,
            object_path=object_path,
            bucket=bucket,
            access_key=access_key,
            secret_key=secret_key,
            session_token=session_token,
            progress=progress,
        )
        return size

    async def _multipart_upload(
        self,
        path: Path,
        data: dict[str, Any],
        size: int,
        *,
        endpoint: str,
        object_path: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        session_token: str,
        progress: Callable[[int], None] | None = None,
    ) -> None:
        """自己分片上传（每片 5MB），并汇报进度。

        与 ``cdn_upload`` 的区别：**有进度回调**。

        分片上传的 OSS 协议：``POST ?uploads`` 拿到 uploadId → 逐片
        ``PUT ?partNumber=N&uploadId=...`` → ``POST ?uploadId=...``
        带 CompleteMultipartUpload 的 XML 合并。
        """
        import xml.etree.ElementTree as ET

        # ① 初始化分片上传
        init_url = f"{endpoint}/{object_path}?uploads"
        init_resp = await self._call_oss(
            "oss_request",
            "POST",
            init_url,
            bucket,
            object_path,
            access_key,
            secret_key,
            session_token,
            content=b"",
            content_type="application/octet-stream",
            sub_resources={"uploads": ""},
        )
        upload_id = _xml_value(init_resp.content, "UploadId")
        if not upload_id:
            raise DriverError("光鸭分片上传初始化失败（未返回 UploadId）")

        # ② 逐片上传
        parts: list[tuple[int, str]] = []
        sent = 0
        with path.open("rb") as source:
            part_number = 1
            while sent < size:
                chunk = source.read(CHUNK_SIZE)
                if not chunk:
                    break
                resp = await self._call_oss(
                    "oss_request",
                    "PUT",
                    f"{endpoint}/{object_path}?partNumber={part_number}&uploadId={upload_id}",
                    bucket,
                    object_path,
                    access_key,
                    secret_key,
                    session_token,
                    content=chunk,
                    content_type="application/octet-stream",
                    sub_resources={"partNumber": str(part_number), "uploadId": upload_id},
                )
                etag = str(resp.headers.get("etag") or resp.headers.get("ETag") or "")
                if not etag:
                    raise DriverError(f"光鸭分片上传失败（第 {part_number} 片无 ETag）")
                parts.append((part_number, etag))
                sent += len(chunk)
                if progress is not None:
                    progress(sent)
                part_number += 1

        if not parts:
            raise DriverError("光鸭分片上传失败：没有传任何分片")

        # ③ 合并分片
        root = ET.Element("CompleteMultipartUpload")
        for number, etag in parts:
            part = ET.SubElement(root, "Part")
            ET.SubElement(part, "PartNumber").text = str(number)
            ET.SubElement(part, "ETag").text = etag
        body = ET.tostring(root, encoding="utf-8")
        await self._call_oss(
            "oss_request",
            "POST",
            f"{endpoint}/{object_path}?uploadId={upload_id}",
            bucket,
            object_path,
            access_key,
            secret_key,
            session_token,
            content=body,
            content_type="application/xml",
            sub_resources={"uploadId": upload_id},
        )

    async def _wait_for_upload(self, task_id: str) -> dict[str, Any]:
        """轮询上传任务直到完成（拿到 fileId）。"""
        deadline = time.monotonic() + POLL_TIMEOUT_SEC
        last_msg = ""
        while time.monotonic() < deadline:
            payload = await self._call("upload_info", task_id)
            code = payload.get("code")
            if code != CODE_UPLOADING:
                if code not in (None, 0):
                    raise DriverError(
                        f"光鸭上传失败：{payload.get('msg') or payload}",
                        kind=_map_code(code),
                    )
                return payload
            last_msg = str(payload.get("msg") or "")
            await asyncio.sleep(POLL_INTERVAL_SEC)
        raise DriverError(
            f"光鸭上传超时（{int(POLL_TIMEOUT_SEC)}s 未完成）：{last_msg}"
        )

    # -- 删除 --

    async def delete_file(self, file_id: str) -> None:
        """删除一个文件（**进回收站**，非永久删除）。

        实测：接口立即返回 ``taskId``，目录里 < 0.5 秒就看不到该文件了。
        但仍要**重列确认**——「接口返回了」不等于「云端真的没了」。

        Raises:
            DriverError: 删除失败。
        """
        payload = await self._call("fs_delete", [str(file_id)])
        code = payload.get("code")
        if code not in (None, 0):
            raise DriverError(
                f"光鸭删除文件失败：{payload.get('msg') or payload}",
                kind=_map_code(code),
            )


def _xml_value(content: bytes, tag: str) -> str:
    """从 XML 响应里取某个标签的文本（空串 = 没有）。

    OSS 的分片上传响应是 XML（``<UploadId>…</UploadId>``）。
    """
    import xml.etree.ElementTree as ET

    try:
        return (ET.fromstring(content).findtext(tag) or "").strip()
    except ET.ParseError:
        return ""


def _map_code(code: Any) -> str:
    """把光鸭业务错误码映射成 :class:`DriverError` 的 kind。

    与 :func:`app.adapters.drivers.guangya._map_code` 同口径——这里单独一份
    是为了避免驱动模块之间互相 import（``guangya.py`` 不该被 ``guangya_upload``
    反向依赖）。**如需改口径，两处一起改。**
    """
    try:
        value = int(code)
    except (TypeError, ValueError):
        return "unknown"
    if value in (111, 117, 430):
        return "auth"
    if value == 123:
        return "config"
    if value == 146:
        return "not_found"
    return "unknown"


def build_guangya_uploader(client: Any) -> GuangyaUploader:
    """工厂函数。"""
    return GuangyaUploader(client)


__all__ = [
    "CHUNK_SIZE",
    "CODE_UPLOADING",
    "POLL_INTERVAL_SEC",
    "POLL_TIMEOUT_SEC",
    "SMALL_FILE_THRESHOLD",
    "GuangyaUploader",
    "UploadResult",
    "build_guangya_uploader",
]
