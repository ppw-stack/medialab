"""115 增量同步：基于「生活事件」流，只处理基线之后的变化。

**与全量扫描的关系**：

- **全量扫描**（:func:`app.services.media.scan_driver`）遍历整棵目录树，
  建立基线——本地 ``media_files`` 表覆盖所有历史文件
- **增量同步**（本模块）只处理「基线之后」的事件：**不碰历史记录**

**为什么用事件流而不是重扫**：限速 1s/请求下，全量要十几分钟（实测 986 秒）。
事件流一次请求就能拿到「哪些文件变了」，且**自带 pick_code**（可直接换
直链，不用列目录）。

**游标**：事件流只保留最近 10000 条，不记游标就会漏。游标存在
``settings`` 表（见 :mod:`app.services.settings_store`）。

**首次使用增量前必须先跑一次全量**——没有基线就无法判断「哪些是新的」，
也无法把老文件纳入本地库。此时返回明确错误提示，而不是默默扫不全。
"""

from __future__ import annotations

import asyncio
import datetime
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.drivers import p115_events
from app.adapters.drivers.base import CloudFile, DriverError
from app.adapters.drivers.p115_events import (
    DELETE_TYPES,
    LifeEvent,
    resolve_event_path,
)
from app.core.config import AppSettings
from app.core.logging import get_logger
from app.db.models import Driver, MediaFile
from app.services import settings_store
from app.services.attachments import (
    SYNCED as ATTACHMENT_SYNCED,
)
from app.services.attachments import (
    UNCHANGED as ATTACHMENT_UNCHANGED,
)
from app.services.attachments import (
    sync_attachment,
)
from app.services.media import (
    _upsert_media,
    find_strm_conflict,
    target_attachment_path,
    target_relative_path,
)
from app.services.naming import classify_by_ext, parse_ext_list, sanitize
from app.services.signing import build_redirect_url
from app.services.strm_writer import StrmWriteResult, delete_strm, write_strm

logger = get_logger(__name__)


class IncrementalError(RuntimeError):
    """增量同步失败。

    Attributes:
        kind: 错误分类（``no_baseline`` / ``truncated`` / ``upstream`` …）。
    """

    def __init__(self, message: str, *, kind: str = "unknown") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class IncrementalStats:
    """一次增量同步的统计。"""

    #: 拉到的原始事件数
    events_fetched: int = 0
    #: 落在媒体库内的事件数
    events_in_scope: int = 0
    #: 新建的 strm 数
    created: int = 0
    #: 内容变更（重写）的 strm 数
    updated: int = 0
    #: 未变的 strm 数
    unchanged: int = 0
    #: 删除的 strm 数
    deleted: int = 0
    #: 附件：新增 / 未变 / 失败
    attachment_synced: int = 0
    attachment_unchanged: int = 0
    attachment_failed: int = 0
    #: 因路径冲突跳过（目录改名等）——**提示用户跑全量**
    conflicts: int = 0
    #: 因后缀不匹配跳过
    skipped_unknown: int = 0
    #: 跳过的删除事件（本地找不到对应记录）
    skipped_deleted: int = 0
    #: 失败数
    failed: int = 0
    #: 游标（日期 ``YYYY-MM-DD``）：旧 → 新
    cursor_old: str = ""
    cursor_new: str = ""
    #: 错误信息（限长）
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "events_fetched": self.events_fetched,
            "events_in_scope": self.events_in_scope,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "deleted": self.deleted,
            "attachment_synced": self.attachment_synced,
            "attachment_unchanged": self.attachment_unchanged,
            "attachment_failed": self.attachment_failed,
            "conflicts": self.conflicts,
            "skipped_unknown": self.skipped_unknown,
            "skipped_deleted": self.skipped_deleted,
            "failed": self.failed,
            "cursor_old": self.cursor_old,
            "cursor_new": self.cursor_new,
            "total": (
                self.created
                + self.updated
                + self.unchanged
                + self.deleted
                + self.attachment_synced
                + self.attachment_unchanged
            ),
        }


async def run_incremental(
    db: AsyncSession,
    settings: AppSettings,
    driver: Driver,
    *,
    http_client: httpx.AsyncClient | None = None,
    on_progress: Any = None,
    driver_impl: Any = None,
) -> IncrementalStats:
    """跑一次增量同步。

    Args:
        db: DB session。
        settings: 应用配置。
        driver: 存储源（必须是 115）。
        http_client: 下载附件用的 HTTP 客户端（不传则内部新建）。
        on_progress: 进度回调 ``(path, stats) → Awaitable``。
        driver_impl: 外部构造好的驱动实例（测试注入用）。

    Raises:
        IncrementalError: 未建基线 / 事件流被截断 / 驱动调用失败。
    """
    if driver.type != "p115":
        raise IncrementalError("增量同步目前只支持 115", kind="unsupported")

    stats = IncrementalStats()

    # 1. 读游标 —— 没有就是「没建过基线」
    key = settings_store.cursor_key(driver.id)
    raw_cursor = await settings_store.get_value(db, key)
    if not raw_cursor:
        raise IncrementalError(
            "尚未建立增量基线：请先执行一次**全量扫描**，再使用增量同步",
            kind="no_baseline",
        )
    try:
        cursor_date = str(raw_cursor)
        datetime.date.fromisoformat(cursor_date)
    except ValueError as exc:
        raise IncrementalError(
            f"增量游标损坏（{raw_cursor!r}），请重新跑一次全量扫描",
            kind="no_baseline",
        ) from exc
    stats.cursor_old = cursor_date

    # 2. 拉事件 —— **按天拉**（``date`` 是唯一有效的增量参数；
    #    ``from_id`` 服务端忽略，实测传什么都不影响返回结果）
    from app.services.driver_factory import build_driver

    if driver_impl is None:
        driver_impl = build_driver(
            driver.type, driver.credential, driver.device_type
        )
    client = getattr(driver_impl, "_client", None)
    if client is None:
        raise IncrementalError("115 客户端不可用", kind="upstream")

    today = p115_events.today_str()
    dates = p115_events.date_range(cursor_date, today)
    if not dates:
        # 游标在今天之后（时钟回拨 / 数据异常）→ 只看今天
        dates = [today]

    result = await p115_events.fetch_events(client, dates)
    stats.events_fetched = result.raw_count
    stats.cursor_new = today
    if result.history_full:
        raise IncrementalError(
            "115 事件流已达上限（10000 条），中间可能有事件被挤掉："
            "请先执行一次全量扫描重建基线",
            kind="truncated",
        )

    logger.info(
        "增量同步 driver_id=%s：拉了 %d 天的事件（%d 条），游标 %s → %s",
        driver.id,
        len(dates),
        result.raw_count,
        cursor_date,
        today,
    )

    # 3. 处理事件
    secret = _signing_secret(settings)
    base_url = settings.resolved_public_base_url
    if not base_url:
        raise IncrementalError("未配置 PUBLIC_BASE_URL", kind="config")

    output_dir = Path(driver.output_dir or "")
    video_exts = parse_ext_list(driver.video_exts)
    image_exts = parse_ext_list(driver.image_exts)
    data_exts = parse_ext_list(driver.data_exts)

    owns_client = http_client is None
    if http_client is None:
        http_client = httpx.AsyncClient(
            follow_redirects=True, timeout=httpx.Timeout(60.0)
        )

    try:
        await _process_events(
            db,
            settings,
            driver,
            driver_impl,
            result.events,
            stats,
            secret=secret,
            base_url=base_url,
            output_dir=output_dir,
            video_exts=video_exts,
            image_exts=image_exts,
            data_exts=data_exts,
            http=http_client,
            on_progress=on_progress,
        )
    finally:
        if owns_client:
            await http_client.aclose()

    # 4. 推进游标（记为今天——下次从今天开始拉）
    await settings_store.set_value(db, key, today)
    await db.commit()

    logger.info(
        "增量同步完成 driver_id=%s：新增 %d 更新 %d 删除 %d 附件 %d 冲突 %d",
        driver.id,
        stats.created,
        stats.updated,
        stats.deleted,
        stats.attachment_synced,
        stats.conflicts,
    )
    return stats


async def _process_events(
    db: AsyncSession,
    settings: AppSettings,
    driver: Driver,
    driver_impl: Any,
    events: list[LifeEvent],
    stats: IncrementalStats,
    *,
    secret: str,
    base_url: str,
    output_dir: Path,
    video_exts: frozenset[str],
    image_exts: frozenset[str],
    data_exts: frozenset[str],
    http: httpx.AsyncClient,
    on_progress: Any = None,
) -> None:
    """逐个处理事件。"""
    client = getattr(driver_impl, "_client", None)
    media_root = (driver.media_path or "").rstrip("/")

    # 删除事件单独处理（不还原路径，靠 pick_code 反查本地）
    for event in events:
        if event.type in DELETE_TYPES:
            await _handle_delete(db, driver, event, stats)
            continue

        if not event.pick_code:
            continue

        # 还原目录路径 → 判断是否在媒体库内
        dir_path = await resolve_event_path(client, event.parent_id)
        if not dir_path:
            continue
        if media_root and not (
            dir_path == media_root or dir_path.startswith(media_root + "/")
        ):
            continue
        stats.events_in_scope += 1

        full_path = f"{dir_path}/{event.file_name}"
        category = classify_by_ext(
            event.file_name, video_exts, image_exts, data_exts
        )
        if category == "unknown":
            stats.skipped_unknown += 1
            continue

        cloud_file = CloudFile(
            remote_id=event.pick_code,
            path=full_path,
            name=event.file_name,
            size=event.file_size,
            is_dir=False,
            mtime=event.update_time,
        )

        if category == "video":
            await _handle_video(
                db,
                driver,
                cloud_file,
                stats,
                secret=secret,
                base_url=base_url,
                output_dir=output_dir,
            )
        else:
            await _handle_attachment(
                driver,
                driver_impl,
                cloud_file,
                stats,
                output_dir=output_dir,
                http=http,
            )

        if on_progress is not None:
            await on_progress(full_path, stats)


async def _handle_video(
    db: AsyncSession,
    driver: Driver,
    cloud_file: CloudFile,
    stats: IncrementalStats,
    *,
    secret: str,
    base_url: str,
    output_dir: Path,
) -> None:
    """处理视频事件：生成 / 更新 strm。"""
    strm_path = output_dir / target_relative_path(driver, cloud_file)

    # **冲突检测**：同一 pick_code 已在本地，但路径不同 —— 说明目录被改名了。
    # 增量不处理目录改名（要重定位整棵子树），跳过并提示跑全量。
    existing = (
        await db.execute(
            select(MediaFile).where(
                MediaFile.driver_id == driver.id,
                MediaFile.remote_id == cloud_file.remote_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None and existing.path != cloud_file.path:
        stats.conflicts += 1
        logger.warning(
            "路径冲突（疑似目录改名），跳过：%s → 本地记的是 %s",
            cloud_file.path,
            existing.path,
        )
        return

    # 同名不同扩展名的冲突（去掉视频扩展名后新增的风险）：报告不阻止
    conflict = await find_strm_conflict(db, driver, cloud_file, strm_path)
    if conflict is not None:
        stats.conflicts += 1
        logger.warning(
            "strm 文件名冲突：%s 与 %s 映射到同一个 %s",
            cloud_file.path,
            conflict.path,
            strm_path.name,
        )

    # 改名遗留的旧 strm 路径（命名规则变更时才有）
    stale_paths: list[Path] = []
    media = await _upsert_media(
        db,
        driver=driver,
        cloud_file=cloud_file,
        strm_path=strm_path,
        secret=secret,
        stale_paths=stale_paths,
    )
    url = build_redirect_url(base_url, media.id, secret)
    # ⚠️ 写文件放线程池：输出目录在 CIFS 上，同步 IO 卡住会冻住事件循环
    # （2026-11-06 prod 事故的同款教训，media.py 那边已改）
    result = await asyncio.to_thread(write_strm, strm_path, url)

    # **先写新、再删旧**（改名遗留）——反过来会让媒体服务器扫库时
    # 短暂看到文件消失
    for old_path in stale_paths:
        await asyncio.to_thread(delete_strm, old_path)

    if result == StrmWriteResult.CREATED:
        stats.created += 1
    elif result == StrmWriteResult.UPDATED:
        stats.updated += 1
    else:
        stats.unchanged += 1
    await db.commit()


async def _handle_attachment(
    driver: Driver,
    driver_impl: Any,
    cloud_file: CloudFile,
    stats: IncrementalStats,
    *,
    output_dir: Path,
    http: httpx.AsyncClient,
) -> None:
    """处理附件事件（图片 / nfo / 字幕）。"""
    target = output_dir / target_attachment_path(driver, cloud_file)
    result = await sync_attachment(driver_impl, cloud_file, target, http=http)
    if result.status == ATTACHMENT_SYNCED:
        stats.attachment_synced += 1
    elif result.status == ATTACHMENT_UNCHANGED:
        stats.attachment_unchanged += 1
    else:
        stats.attachment_failed += 1
        if result.error and len(stats.errors) < 20:
            stats.errors.append(f"{cloud_file.path}: {result.error}")
        logger.warning("附件同步失败 %s：%s", cloud_file.path, result.error)


async def _handle_delete(
    db: AsyncSession,
    driver: Driver,
    event: LifeEvent,
    stats: IncrementalStats,
) -> None:
    """处理删除事件：删本地 strm + 记录。

    **删除事件的父目录信息已失效**（``parent_id`` 恒为 0），所以靠
    ``pick_code`` 反查本地记录 —— 本地记录的 ``path`` 才是可靠依据。
    """
    if not event.pick_code:
        stats.skipped_deleted += 1
        return

    existing = (
        await db.execute(
            select(MediaFile).where(
                MediaFile.driver_id == driver.id,
                MediaFile.remote_id == event.pick_code,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        # 本地没有这条记录 —— 可能本来就没扫到过，或已删过
        stats.skipped_deleted += 1
        return

    if existing.strm_path:
        # ⚠️ 删文件放线程池（CIFS 卡死时同步 unlink 会卡住事件循环）
        await asyncio.to_thread(delete_strm, Path(existing.strm_path))
    await db.delete(existing)
    stats.deleted += 1
    await db.commit()


def _signing_secret(settings: AppSettings) -> str:
    """取签名密钥。"""
    secret = settings.signing_secret
    if secret is None:
        raise IncrementalError("未配置 SIGNING_SECRET", kind="config")
    return secret.get_secret_value()


def now_ts() -> int:
    """当前 unix 秒。"""
    return int(time.time())


__all__ = [
    "DriverError",
    "IncrementalError",
    "IncrementalStats",
    "run_incremental",
    "sanitize",
]
