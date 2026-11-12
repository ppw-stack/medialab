"""附件同步：把网盘里的图片 / nfo / 字幕下载到本地媒体库。

**为什么需要**：``.strm`` 只能表达「视频在哪」。Emby / Jellyfin 对海报和
元数据只认**真实文件**——没有本地文件时它会自己按 TMDB 刮一套，与网盘里
MoviePilot 之类生成的（中文标题、豆瓣评分、自制海报）对不上。

**为什么单遍而不是先只生成 strm**：Emby 开了实时监控时，看到 ``.strm``
就立刻刮削并存下元数据；等几小时后本地 nfo 再落地，Emby **默认不会重刮**。
单遍让每条内容「上线即完整」。

设计要点：

- **不建表**：附件是派生数据，增量判断只用「本地文件存在且大小与云盘一致」。
  塞进 ``media_files`` 会把「7861 条 / 21TB」这类统计搞乱。
- **``.part`` 临时文件**：中断不留半个 jpg 被当成已同步（Emby 会显示破图）。
- **固定 UA**：115 直链 ``f=1`` 绑定 UA，下载用什么 UA 就得用什么。
  这与播放器的 UA 分桶是两条互不影响的路径。
- **失败隔离**：单个附件失败只返回失败状态，不抛异常、不中断整轮扫描。
- **磁盘 IO 全部在线程池**（2026-11-12 加入）：输出目录在 CIFS 上——
  事件循环里出现同步 ``stat`` / ``write`` / ``rename``，挂载拥塞时会把
  整个事件循环卡死（2026-11-06 prod 假死事故的同款故障模式——
  ``write_strm`` / ``delete_strm`` 当时就改了线程池，附件这条路径漏了）。
  读响应流是异步的留在事件循环，每个落盘动作逐块丢 ``to_thread``。
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.adapters.drivers.base import CloudDriver, CloudFile, DriverError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 下载附件时用的 UA。固定值即可——只要换直链与取数据用的是同一个。
ATTACHMENT_USER_AGENT = "strmmanger/attachment"

#: 临时文件后缀（下载中）
PART_SUFFIX = ".part"

#: 单次读取的字节数
CHUNK_SIZE = 64 * 1024

#: 下载失败时的最大尝试次数（含首次）
#:
#: 不同网盘的 CDN 稳定性差别很大——实测光鸭的 CDN 会中途掐断连接
#: （``peer closed connection without sending complete message body``），
#: 115 就没这问题。重试时**重新换直链**（旧的 URL 可能已失效）。
DEFAULT_MAX_RETRIES = 3

#: 重试退避基数（秒）——第 n 次失败后等 ``n * RETRY_BACKOFF_SEC``
RETRY_BACKOFF_SEC = 1.0

#: 结果状态
SYNCED = "synced"
UNCHANGED = "unchanged"
FAILED = "failed"


@dataclass(frozen=True)
class AttachmentResult:
    """单个附件的同步结果。"""

    #: ``synced`` / ``unchanged`` / ``failed``
    status: str
    #: 失败原因（成功时为 ``None``）
    error: str | None = None


def target_path(output_dir: Path, relative: Path) -> Path:
    """拼出附件的落盘路径。"""
    return output_dir / relative


async def sync_attachment(
    driver: CloudDriver,
    cloud_file: CloudFile,
    target: Path,
    *,
    http: httpx.AsyncClient,
    max_retries: int = 3,
) -> AttachmentResult:
    """把单个附件下载到 ``target``。

    已存在且大小一致时直接返回 ``unchanged``（**不**重复换直链）。

    **会重试**：不同网盘的 CDN 稳定性差别很大——实测光鸭的 CDN 会中途
    掐断连接（``peer closed connection without sending complete message
    body``），115 就没这问题。故下载失败时重新换直链再试，最多
    :data:`DEFAULT_MAX_RETRIES` 次。

    Args:
        driver: 存储源驱动（用于换直链）。
        cloud_file: 源文件（含大小，用于完整性校验）。
        target: 落盘路径（父目录不存在会自动创建）。
        http: HTTP 客户端（由调用方复用，跟随重定向）。
        max_retries: 最大尝试次数（含首次）。

    Returns:
        同步结果。**不抛异常**——失败折叠成 ``status=FAILED``。
    """
    if await asyncio.to_thread(_is_up_to_date, target, cloud_file.size):
        return AttachmentResult(UNCHANGED)

    last_error = ""
    for attempt in range(1, max(1, max_retries) + 1):
        result = await _download_once(driver, cloud_file, target, http=http)
        if result.status != FAILED:
            return result
        last_error = result.error or ""
        if attempt < max_retries:
            logger.warning(
                "附件下载失败（第 %d/%d 次），重试：%s — %s",
                attempt,
                max_retries,
                cloud_file.path,
                last_error,
            )
            # 退避——CDN 抽风时立刻重试大概率还是失败
            await asyncio.sleep(RETRY_BACKOFF_SEC * attempt)

    return AttachmentResult(FAILED, last_error)


async def _download_once(
    driver: CloudDriver,
    cloud_file: CloudFile,
    target: Path,
    *,
    http: httpx.AsyncClient,
) -> AttachmentResult:
    """下载一次（换直链 → 流式写入 → 校验 → rename）。

    **磁盘 IO 全部在线程池**（输出目录在 CIFS 上，同步 ``stat`` /
    ``write`` / ``rename`` 卡事件循环 = 2026-11-06 假死事故同款）：
    读响应流是异步的留在事件循环，落盘动作逐块丢 ``to_thread``。
    """
    try:
        link = await driver.get_direct_link(
            cloud_file.remote_id, ATTACHMENT_USER_AGENT
        )
    except DriverError as exc:
        return AttachmentResult(FAILED, f"换直链失败：{exc}")

    part = target.with_name(target.name + PART_SUFFIX)
    try:
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        headers = {"User-Agent": ATTACHMENT_USER_AGENT, **link.extra_headers}
        async with http.stream("GET", link.url, headers=headers) as response:
            if response.status_code >= 400:
                return AttachmentResult(FAILED, f"HTTP {response.status_code}")
            written = await _drain_to_file(response, part)

        if cloud_file.size and written != cloud_file.size:
            await asyncio.to_thread(_remove, part)
            return AttachmentResult(
                FAILED, f"大小不符：下载 {written} 字节，期望 {cloud_file.size}"
            )
        # rename 是原子的：要么是完整文件，要么没有
        await asyncio.to_thread(part.replace, target)
        return AttachmentResult(SYNCED)
    except (httpx.HTTPError, OSError) as exc:
        await asyncio.to_thread(_remove, part)
        return AttachmentResult(FAILED, f"下载失败：{exc}")


async def _drain_to_file(response: httpx.Response, part: Path) -> int:
    """把响应流写进 ``part`` 文件，返回写入字节数。

    每个块的 ``fh.write`` 都丢线程池——CIFS 上单次写可达几十毫秒，
    挂载拥塞时同步写会无限等待，把事件循环一起拖死。
    """
    fh = await asyncio.to_thread(part.open, "wb")
    written = 0
    try:
        async for chunk in response.aiter_bytes(CHUNK_SIZE):
            await asyncio.to_thread(fh.write, chunk)
            written += len(chunk)
    finally:
        with contextlib.suppress(OSError):  # pragma: no cover - 写入已报错时关闭失败仅兜底
            await asyncio.to_thread(fh.close)
    return written


def _is_up_to_date(target: Path, expected_size: int) -> bool:
    """本地文件是否已经是最新的。

    只用大小判断：图片 / 字幕 / nfo 被改动时大小几乎必然变化，
    而网盘侧 mtime 并不总是可靠。大小相同即认为无需重下。
    """
    try:
        if not target.is_file():
            return False
        if expected_size and target.stat().st_size != expected_size:
            return False
    except OSError:
        return False
    return True


def _remove(path: Path) -> None:
    """尽力删除（清理残留的 ``.part``）。"""
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - 极端情况只记日志
        logger.warning("清理临时文件失败：%s", path)
