"""媒体库服务：扫描 → 生成 STRM → 落库。

流程（每个存储源一套配置）::

    驱动.iter_tree_by_cid(driver.media_cid, include_subdirs)
      → 按后缀分类（视频 / 图片 / 数据 / 未知）
      → 按命名规则算出目标相对路径
      → 写 STRM（幂等）
      → 落库 media_files（含中转签名）

**幂等是硬要求**：重复扫描不能重复写文件，也不能让媒体服务器反复重扫库。

**用 cid 不用路径**：115 的目录 id 在改名后不变，路径会失效。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.drivers.base import CloudFile, DriverError
from app.core.config import AppSettings, get_settings
from app.core.logging import get_logger
from app.core.timeutils import bj_now, format_bj
from app.db.models import Driver, MediaFile
from app.services import attachments
from app.services.driver_factory import build_driver
from app.services.naming import (
    classify_by_ext,
    parse_ext_list,
    sanitize,
    strm_filename,
)
from app.services.rotation import RotationResult, resign_single, rotate_secret
from app.services.signing import build_redirect_url, sign
from app.services.strm_writer import StrmWriteResult, delete_strm, write_strm

logger = get_logger(__name__)


class MediaServiceError(Exception):
    """媒体库操作失败。"""

    def __init__(self, message: str, *, status_code: int = 400, kind: str = "invalid"):
        super().__init__(message)
        self.status_code = status_code
        self.kind = kind


@dataclass
class ScanStats:
    """一次扫描的统计。

    文件按后缀分四类处理（见 :func:`app.services.naming.classify_by_ext`）：

    - **视频** → 生成 ``.strm``（``created`` / ``updated`` / ``unchanged``）
    - **图片** → 同步下载到本地（``attachment_*``）
    - **数据**（nfo / 字幕）→ 同步下载到本地（``attachment_*``）
    - **未知后缀** → 忽略，单独计数（``skipped_unknown``）

    ``attachment_*`` 与 ``created`` 分开计数，是为了能一眼判断
    「strm 生成了没」和「元数据下全了没」这两件不同的事。
    """

    created: int = 0
    updated: int = 0
    unchanged: int = 0
    #: 附件（图片 / nfo / 字幕）新下载
    attachment_synced: int = 0
    #: 附件已存在且大小一致，跳过下载
    attachment_unchanged: int = 0
    #: 附件下载失败（换直链失败 / HTTP 错误 / 大小不符）
    attachment_failed: int = 0
    skipped_unknown: int = 0
    skipped_small: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def skipped(self) -> int:
        return self.skipped_unknown + self.skipped_small

    @property
    def total(self) -> int:
        return (
            self.created
            + self.updated
            + self.unchanged
            + self.attachment_synced
            + self.attachment_unchanged
            + self.attachment_failed
            + self.skipped
            + self.failed
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "attachment_synced": self.attachment_synced,
            "attachment_unchanged": self.attachment_unchanged,
            "attachment_failed": self.attachment_failed,
            "skipped": self.skipped,
            "skipped_unknown": self.skipped_unknown,
            "skipped_small": self.skipped_small,
            "failed": self.failed,
            "total": self.total,
            "errors": self.errors[:20],
        }


def _signing_secret(settings: AppSettings) -> str:
    """取签名密钥。"""
    secret = settings.signing_secret
    if secret is None:
        raise MediaServiceError(
            "未配置 SIGNING_SECRET，无法生成中转 URL",
            status_code=500,
            kind="config",
        )
    return secret.get_secret_value()


# ================================================================ 查询与操作
#
# 以下函数是 **API 层与 DB 之间的唯一通道**——路由层不碰 ``app.db``
# （分层约束，见 ``tests/unit/test_layering.py``）。每个都**自开事务**。


@dataclass(slots=True)
class MediaRow:
    """一条媒体条目（供 API 组装响应）。"""

    id: int
    driver_id: int
    remote_id: str
    path: str
    name: str
    size: int
    strm_path: str | None
    #: 当前中转 URL（前端可直接预览 / 复制）；未配置密钥/地址时为空串
    strm_url: str


@dataclass(slots=True)
class MediaPage:
    """媒体条目分页结果。"""

    total: int
    items: list[MediaRow]


@dataclass(slots=True)
class MediaStats:
    """媒体库统计（仪表盘用）。"""

    total: int
    total_size: int
    #: 按存储源分组的条目数
    per_driver: list[tuple[int, int]]
    server_time: int


async def list_media(
    *,
    q: str | None = None,
    driver_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> MediaPage:
    """列出媒体条目（分页 + 可选关键字 / 存储源过滤）。"""
    from sqlalchemy import select

    from app.db.models import MediaFile
    from app.db.session import session_scope

    conditions: list[Any] = []
    if q:
        conditions.append(MediaFile.name.like(f"%{q}%"))
    if driver_id is not None:
        conditions.append(MediaFile.driver_id == driver_id)

    settings = get_settings()
    secret = settings.signing_secret
    base_url = settings.resolved_public_base_url
    secret_value = secret.get_secret_value() if secret else ""

    async with session_scope() as db:
        total = (
            await db.execute(select(func.count()).select_from(MediaFile).where(*conditions))
        ).scalar_one()
        rows = (
            await db.execute(
                select(MediaFile)
                .where(*conditions)
                .order_by(MediaFile.path)
                .limit(min(limit, 500))
                .offset(offset)
            )
        ).scalars().all()

    items = [
        MediaRow(
            id=r.id,
            driver_id=r.driver_id,
            remote_id=r.remote_id,
            path=r.path,
            name=r.name,
            size=r.size,
            strm_path=r.strm_path,
            strm_url=(
                build_redirect_url(base_url, r.id, secret_value)
                if secret_value and base_url
                else ""
            ),
        )
        for r in rows
    ]
    return MediaPage(total=total, items=items)


async def media_stats() -> MediaStats:
    """媒体库统计（仪表盘用）。"""
    from sqlalchemy import func, select

    from app.db.models import MediaFile
    from app.db.session import session_scope

    async with session_scope() as db:
        total = (await db.execute(select(func.count()).select_from(MediaFile))).scalar_one()
        total_size = (
            await db.execute(select(func.coalesce(func.sum(MediaFile.size), 0)))
        ).scalar_one()
        per_driver = (
            await db.execute(
                select(MediaFile.driver_id, func.count())
                .group_by(MediaFile.driver_id)
                .order_by(MediaFile.driver_id)
            )
        ).all()

    return MediaStats(
        total=total,
        total_size=int(total_size or 0),
        per_driver=[(d, c) for d, c in per_driver],
        server_time=int(time.time()),
    )


async def resign_media(media_ids: list[int]) -> int:
    """批量重签（用当前密钥）并重写 STRM 文件。返回成功数。"""
    from app.db.session import session_scope

    settings = get_settings()
    ok = 0
    async with session_scope() as db:
        for media_id in media_ids:
            if await resign_single(db, settings, media_id):
                ok += 1
    return ok


async def rotate_signing_secret(new_secret: str | None = None) -> RotationResult:
    """轮换签名密钥（并批量重签 + 重写 STRM）。"""
    from app.db.session import session_scope

    settings = get_settings()
    async with session_scope() as db:
        return await rotate_secret(db, settings, new_secret)


def build_strm_url(settings: AppSettings, media_id: int) -> str:
    """构造某条媒体的中转 URL。"""
    base = settings.resolved_public_base_url
    if not base:
        raise MediaServiceError(
            "未配置 PUBLIC_BASE_URL，无法生成中转 URL",
            status_code=500,
            kind="config",
        )
    return build_redirect_url(base, media_id, _signing_secret(settings))


def target_relative_dir(driver: Driver, cloud_file: CloudFile) -> Path:
    """算出目标相对**目录**。

    **优先用驱动给的 ``relative_dir``**（相对媒体库根的目录）——
    光鸭没有「路径 ↔ id」反查接口，拿不到根路径字符串，只能靠这个。

    没有 ``relative_dir`` 时（115）回退到「从绝对路径里剥掉
    ``media_path``」：``/影视/电影/阿凡达 (2009)/阿凡达.mkv`` →
    ``电影/阿凡达 (2009)``。
    """
    if cloud_file.relative_dir is not None:
        rel = cloud_file.relative_dir.strip("/")
        return Path(rel) if rel else Path(".")

    root = (driver.media_path or "").rstrip("/")
    relative_dir = ""
    if root and cloud_file.path.startswith(root):
        relative_dir = cloud_file.path[len(root) :].strip("/")
    return Path(relative_dir).parent if relative_dir else Path(".")


def target_relative_path(
    driver: Driver,
    cloud_file: CloudFile,
) -> Path:
    """算出 STRM 的目标相对路径。

    两个部分都**直接沿用网盘里的原样**：

    - 目录：源路径去掉媒体库根（``media_path``）后的部分
    - 文件名：源文件名 + ``.strm``

    例：媒体库根 ``/影视``，文件 ``/影视/电影/阿凡达 (2009)/阿凡达 (2009) - 2160p.mkv``
    → ``电影/阿凡达 (2009)/阿凡达 (2009) - 2160p.mkv.strm``

    不重命名理由：原文件名已含完整发行信息，Emby / Jellyfin 对标准命名
    解析最准；自己拼容易出错（如把片名里的 ``1900`` 误当年份）。
    """
    return target_relative_dir(driver, cloud_file) / strm_filename(cloud_file.name)


def target_attachment_path(driver: Driver, cloud_file: CloudFile) -> Path:
    """算出附件（图片 / nfo / 字幕）的目标相对路径。

    与 ``.strm`` **同目录、同原文件名** —— Emby 靠文件名识别
    ``poster.jpg`` / ``fanart.jpg`` / ``season.nfo`` 这类约定，
    改名字会让它认不出。
    """
    name = sanitize(cloud_file.name) or cloud_file.name
    return target_relative_dir(driver, cloud_file) / name


def _cloud_mtime_to_bj(mtime: int) -> str | None:
    """网盘返回的 unix 秒 mtime → 北京时间字符串；0/无值 → None。"""
    if not mtime:
        return None
    return format_bj(datetime.fromtimestamp(mtime, UTC))


async def _upsert_media(
    db: AsyncSession,
    *,
    driver: Driver,
    cloud_file: CloudFile,
    strm_path: Path,
    secret: str,
) -> MediaFile:
    """按 (driver_id, remote_id) 落库媒体条目。"""
    existing = (
        await db.execute(
            select(MediaFile).where(
                MediaFile.driver_id == driver.id,
                MediaFile.remote_id == cloud_file.remote_id,
            )
        )
    ).scalar_one_or_none()

    now = bj_now()
    if existing is None:
        media = MediaFile(
            driver_id=driver.id,
            remote_id=cloud_file.remote_id,
            path=cloud_file.path,
            name=cloud_file.name,
            size=cloud_file.size,
            strm_path=str(strm_path),
            signature=None,
            mtime=_cloud_mtime_to_bj(cloud_file.mtime),
            created_at=now,
            updated_at=now,
        )
        db.add(media)
        await db.flush()
        media.signature = sign(secret, media.id)
        return media

    existing.path = cloud_file.path
    existing.name = cloud_file.name
    existing.size = cloud_file.size
    existing.strm_path = str(strm_path)
    existing.mtime = _cloud_mtime_to_bj(cloud_file.mtime)
    existing.updated_at = now
    return existing


def _build_driver(driver: Driver) -> Any:
    """按存储源构造驱动实例。"""
    try:
        return build_driver(driver.type, driver.credential, driver.device_type)
    except DriverError as exc:
        raise MediaServiceError(
            f"驱动不可用：{exc}", status_code=503, kind="config"
        ) from exc


#: 每处理多少个文件提交一次事务
#:
#: 限速 1s/请求下扫描要跑几分钟，整批一个事务意味着中途失败全部回滚。
#: 分批提交让「已扫到的部分」保住——重跑时只需补剩下的。
COMMIT_EVERY = 50


async def scan_driver(
    db: AsyncSession,
    settings: AppSettings,
    driver: Driver,
    *,
    prune: bool = False,
    on_progress: Callable[[str, ScanStats, int], Awaitable[None]] | None = None,
    http_client: httpx.AsyncClient | None = None,
    driver_impl: Any = None,
) -> ScanStats:
    """扫描一个存储源，生成 STRM 并同步附件。

    **单遍**：遍历目录时，视频写 ``.strm``、图片 / 数据（nfo / 字幕）
    当场下载到同目录。不做「先只生成 strm、再回头下附件」两阶段——
    否则 Emby 会在看到 strm 时先用 TMDB 刮一遍，等本地 nfo 落地时
    它默认不会重刮，元数据就永久错了。

    Args:
        db: DB session。
        settings: 应用配置。
        driver: 存储源（含媒体库配置）。
        prune: 是否删除「源端已消失」条目对应的 STRM 文件。
        on_progress: 进度回调 ``(current_path, stats, dirs) → Awaitable``。
            每遍历一个目录 / 处理一个文件都会调用一次（由调用方决定怎么用）。
        http_client: 下载附件用的 HTTP 客户端。不传则内部新建并在结束时关闭
            （测试可注入带 MockTransport 的客户端）。
        driver_impl: 外部构造好的驱动实例。不传则内部构造（此时无法注入
            Token 刷新回调——光鸭需要它把新 token 写回 DB）。

    Raises:
        MediaServiceError: 配置缺失 / 驱动调用失败。
    """
    stats = ScanStats()
    secret = _signing_secret(settings)
    base_url = settings.resolved_public_base_url
    if not base_url:
        raise MediaServiceError(
            "未配置 PUBLIC_BASE_URL，无法生成中转 URL",
            status_code=500,
            kind="config",
        )
    if not driver.media_cid:
        raise MediaServiceError(
            "未配置 115 媒体库 cid，无法扫描",
            status_code=400,
            kind="config",
        )
    if not driver.output_dir:
        raise MediaServiceError(
            "未配置本地媒体库目录，无法扫描",
            status_code=400,
            kind="config",
        )

    driver_impl = driver_impl or _build_driver(driver)

    video_exts = parse_ext_list(driver.video_exts)
    image_exts = parse_ext_list(driver.image_exts)
    data_exts = parse_ext_list(driver.data_exts)
    min_bytes = max(int(driver.min_size_mb or 0), 0) * 1024 * 1024

    output_dir = Path(driver.output_dir)
    seen_remote_ids: set[str] = set()
    processed = 0

    owns_client = http_client is None
    if http_client is None:
        # follow_redirects：网盘直链可能再跳一次 CDN
        http_client = httpx.AsyncClient(
            follow_redirects=True, timeout=httpx.Timeout(60.0)
        )

    try:
        async for cloud_file in driver_impl.iter_tree_by_cid(
            driver.media_cid,
            parent_path=driver.media_path or "",
            include_subdirs=driver.include_subdirs,
        ):
            processed += 1
            if on_progress is not None:
                await on_progress(cloud_file.path, stats, processed)

            category = classify_by_ext(
                cloud_file.name, video_exts, image_exts, data_exts
            )
            if category in ("image", "data"):
                # 图片 / nfo / 字幕 → 下载到与 strm 同目录，保持原文件名
                target = output_dir / target_attachment_path(driver, cloud_file)
                result = await attachments.sync_attachment(
                    driver_impl, cloud_file, target, http=http_client
                )
                if result.status == attachments.SYNCED:
                    stats.attachment_synced += 1
                elif result.status == attachments.UNCHANGED:
                    stats.attachment_unchanged += 1
                else:
                    stats.attachment_failed += 1
                    if result.error and len(stats.errors) < 20:
                        stats.errors.append(f"{cloud_file.path}: {result.error}")
                    logger.warning(
                        "附件同步失败 %s：%s", cloud_file.path, result.error
                    )
                continue
            if category != "video":
                stats.skipped_unknown += 1
                continue
            if min_bytes and cloud_file.size < min_bytes:
                stats.skipped_small += 1
                continue

            seen_remote_ids.add(cloud_file.remote_id)
            relative = target_relative_path(driver, cloud_file)
            strm_path = output_dir / relative

            media = await _upsert_media(
                db,
                driver=driver,
                cloud_file=cloud_file,
                strm_path=strm_path,
                secret=secret,
            )
            url = build_redirect_url(base_url, media.id, secret)
            written = write_strm(strm_path, url)

            if written == StrmWriteResult.CREATED:
                stats.created += 1
            elif written == StrmWriteResult.UPDATED:
                stats.updated += 1
            else:
                stats.unchanged += 1

            # 分批提交——中途失败不至于全丢
            if processed % COMMIT_EVERY == 0:
                await db.commit()
    except DriverError as exc:
        raise MediaServiceError(
            f"扫描失败：{exc}", status_code=502, kind="upstream"
        ) from exc
    finally:
        if owns_client:
            await http_client.aclose()

    if prune:
        stats.skipped_unknown += await _prune_orphans(
            db, driver, seen_remote_ids
        )
    driver.last_scan_at = bj_now()
    driver.last_synced_count = stats.created + stats.updated
    await db.commit()

    logger.info(
        "存储源 %s 扫描完成：strm 新增 %d 更新 %d 未变 %d；"
        "附件新增 %d 未变 %d 失败 %d；跳过 未知 %d 太小 %d",
        driver.id,
        stats.created,
        stats.updated,
        stats.unchanged,
        stats.attachment_synced,
        stats.attachment_unchanged,
        stats.attachment_failed,
        stats.skipped_unknown,
        stats.skipped_small,
    )
    return stats


async def _prune_orphans(
    db: AsyncSession, driver: Driver, seen_remote_ids: set[str]
) -> int:
    """删除「源端已消失」的条目及其 STRM 文件。

    只删**本存储源**产出的、且源端确实没有了的条目。
    """
    rows = (
        await db.execute(
            select(MediaFile).where(MediaFile.driver_id == driver.id)
        )
    ).scalars().all()

    removed = 0
    for media in rows:
        if media.remote_id in seen_remote_ids:
            continue
        if media.strm_path:
            delete_strm(Path(media.strm_path))
        await db.delete(media)
        removed += 1
    if removed:
        await db.flush()
    return removed
