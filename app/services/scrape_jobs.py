"""刮削作业：把媒体库里未刮削的视频补齐 NFO 与图片。

**为什么是独立作业而不是挂进扫描**：扫描是「网盘 → STRM」的链路，跑得
越快越好；刮削要打 TMDB（每个文件 2-5 个请求），混进去会把扫描拖慢，
且 TMDB 故障时不该拖垮 STRM 生成。故拆开：

- 独立作业（本模块）——兜底扫一遍媒体库，补齐漏刮的
- 整理完成钩子（:mod:`app.services.organize_service`）——刚整理完的文件
  立即刮削，不必等下一轮作业

单文件的解析 / 识别 / 刮削逻辑在
:meth:`app.services.scrape_service.ScrapeService.scrape_video`（作业与
手动 API 共用），本模块只做**作业编排**：扫描目录、去重、汇总步骤明细。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.models import MediaScrape
from app.db.session import session_scope
from app.domain.media_exts import DEFAULT_VIDEO_EXTS
from app.domain.scrape_types import ScrapeStatus
from app.services.meta_parser import parse_meta
from app.services.scheduler import JobStep, JobStepItem, build_items
from app.services.scrape_service import ScrapeService

logger = get_logger(__name__)

__all__ = ["run_scrape_metadata"]


#: 视频后缀集合（小写，不含点）
VIDEO_EXTS: frozenset[str] = frozenset(
    ext.strip().lower().lstrip(".")
    for ext in DEFAULT_VIDEO_EXTS.split(",")
    if ext.strip()
)


def _collect_videos(root: Path) -> list[Path]:
    """递归收集视频文件（同步实现，调用方须包 :func:`asyncio.to_thread`）。

    :param root: 媒体库根目录
    :return: 视频文件路径列表（排序，保证执行顺序稳定）
    """
    videos: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower().lstrip(".") in VIDEO_EXTS:
            videos.append(path)
    return sorted(videos)


async def _scraped_scope_keys() -> set[str]:
    """取已成功刮削的单元键（用于跳过）。"""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(MediaScrape.scope_key, MediaScrape.status)
            )
        ).all()
    return {key for key, status in rows if status == ScrapeStatus.SCRAPED}


async def run_scrape_metadata() -> dict[str, Any]:
    """刮削作业入口。

    :return: ``{"summary": str, "steps": [JobStep]}``
    """
    steps: list[JobStep] = []

    enabled = _runtime_flag("scrape_enabled")
    if not enabled:
        return {
            "summary": "刮削未启用（运行参数「启用刮削」为关闭）",
            "steps": [JobStep("刮削", "skip", None, "未启用")],
        }
    if not ScrapeService.api_key():
        return {
            "summary": "未配置 TMDB API Key，刮削不可用",
            "steps": [JobStep("刮削", "skip", None, "未配置 TMDB API Key")],
        }

    root = _library_root()
    if root is None:
        return {
            "summary": "未配置本地媒体库目录，刮削不可用",
            "steps": [JobStep("刮削", "skip", None, "未配置本地媒体库目录")],
        }

    videos = await asyncio.to_thread(_collect_videos, root)
    if not videos:
        return {
            "summary": "媒体库目录内没有视频文件",
            "steps": [JobStep("刮削", "skip", 0, "没有视频文件")],
        }

    policy = ScrapeService.policy()
    done_keys = await _scraped_scope_keys()
    service = ScrapeService()
    items: list[JobStepItem] = []
    ok_count = 0
    skip_count = 0
    fail_count = 0

    try:
        for path in videos:
            meta = parse_meta(path.name)
            if meta is not None and meta.is_episode:
                key = (
                    f"episode|{path.parent}"
                    f"|S{meta.season}E{meta.episode}"
                )
            else:
                key = f"movie|{path.parent}"
            if key in done_keys:
                skip_count += 1
                continue
            try:
                status, detail, _ = await service.scrape_video(path, policy=policy)
            except Exception as exc:
                status, detail = "fail", f"{type(exc).__name__}: {exc}"
            if status == "ok":
                ok_count += 1
                done_keys.add(key)
            elif status == "skip":
                skip_count += 1
                done_keys.add(key)
            else:
                fail_count += 1
            items.append(
                JobStepItem(name=path.name, status=status, count=1, detail=detail)
            )
    finally:
        await service.aclose()

    truncated, total = build_items(items)
    detail = f"{ok_count} 成功 / {skip_count} 跳过 / {fail_count} 失败"
    step_status = "ok" if fail_count == 0 else ("warn" if ok_count else "fail")
    steps.append(
        JobStep(
            name="刮削",
            status=step_status,
            count=ok_count + skip_count + fail_count,
            detail=detail,
            items=truncated,
            items_total=total,
        )
    )
    return {"summary": detail, "steps": steps}


def _runtime_flag(key: str) -> bool:
    """读运行参数开关（取不到时按关闭处理）。"""
    from app.services.runtime_params import get_runtime_params, lookup_spec

    try:
        return get_runtime_params().get_int(key) == 1
    except Exception:
        spec = lookup_spec(key)
        return bool(spec and spec.default == 1)


def _library_root() -> Path | None:
    """本地媒体库根目录（未配置返回 None）。"""
    from app.services.env_settings import get_env_setting

    raw = get_env_setting("media_library_dir").strip()
    if not raw:
        return None
    return Path(raw)
