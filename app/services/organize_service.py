"""整理服务：源目录里的视频 → 识别 → 标准结构落盘 → 触发刮削。

**输入是「源目录」，不是「下载器任务」**——整理与下载器彻底解耦：文件从
哪来无所谓（BT 下载、手动拷贝、其它工具），把视频放进源目录就会被扫到。
这与本项目的心智一致：数据同步是「本地目录 → 网盘」，离线下载是
「本地目录 → 光鸭」，整理是「源目录 → 媒体库」——全是目录驱动。

**这是 MoviePilot「整理」的精简重写**——它的 transhandler 有 3000 行（覆盖
蓝光原盘 / 音乐 / 多种存储 / 断点续传），本项目只做一件事：把源目录里的
视频按标准结构 copy/move 到本地媒体库。不搬它的搬运引擎（GPL + 架构不同），
只对齐**产物形态**。

流程::

    源目录（可配多个，逗号分隔）
      → 递归收集视频文件（+ 同 stem 字幕）
      → 识别（meta_parser → TMDB，结果落 scrape_cache）
      → 计划目标路径（固定模板：标题 (年份)/Season XX/标题 - SxxExx）
      → copy / move（复制 → 大小校验 → 才删源）
      → 记 organize_history（判重键：source_path）
      → 触发刮削（复用 ScrapeService.scrape_video）

**失败语义**：识别不到 / 目标冲突 / 拷贝出错，一律 failed 并点名；源文件
留在源目录，下轮作业重试。
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.models import OrganizeHistory
from app.db.session import session_scope
from app.domain.scrape_types import MediaKind, OrganizeAction, OrganizeStatus
from app.services.env_settings import get_env_setting
from app.services.errors import ServiceError
from app.services.meta_parser import ParsedMeta, parse_meta
from app.services.naming import sanitize
from app.services.runtime_params import get_runtime_params
from app.services.scrape_service import ScrapeService

logger = get_logger(__name__)

__all__ = ["run_organize"]


#: 视频后缀（小写，不含点）
_VIDEO_EXTS: frozenset[str] = frozenset(
    {"mkv", "mp4", "avi", "mov", "wmv", "flv", "m4v", "ts", "m2ts", "rmvb", "rm", "webm", "iso"}
)

#: 字幕后缀（同 stem 的字幕随视频整理）
_SUBTITLE_EXTS: frozenset[str] = frozenset({"srt", "ass", "ssa", "sub"})


# ------------------------------------------------------------------ 配置


def _runtime_flag(key: str) -> bool:
    """读运行参数开关（取不到时按关闭处理）。"""
    from app.services.runtime_params import lookup_spec

    try:
        return get_runtime_params().get_int(key) == 1
    except Exception:
        spec = lookup_spec(key)
        return bool(spec and spec.default == 1)


def _action() -> str:
    """整理动作（copy / move）。"""
    from app.services.runtime_params import lookup_spec

    try:
        value = get_runtime_params().get_int("organize_action")
    except Exception:
        spec = lookup_spec("organize_action")
        value = spec.default if spec else 0
    return OrganizeAction.MOVE if value == 1 else OrganizeAction.COPY


def _conflict_policy() -> str:
    """同名冲突策略：``fail``（记失败）/ ``disambiguate``（加序号消歧）。"""
    from app.services.runtime_params import lookup_spec

    try:
        value = get_runtime_params().get_int("organize_conflict_policy")
    except Exception:
        spec = lookup_spec("organize_conflict_policy")
        value = spec.default if spec else 0
    return "disambiguate" if value == 1 else "fail"


def _library_root() -> Path | None:
    """本地媒体库根目录（未配置返回 None）。"""
    raw = get_env_setting("media_library_dir").strip()
    return Path(raw) if raw else None


def _source_dirs() -> list[Path]:
    """整理源目录列表（逗号分隔多个；未配置返回空）。"""
    raw = get_env_setting("organize_source_dirs").strip()
    if not raw:
        return []
    dirs: list[Path] = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            dirs.append(Path(part))
    return dirs


# ------------------------------------------------------------------ 历史


async def _organized_source_paths() -> set[str]:
    """已整理过的源文件路径（status != failed——失败的允许重试）。"""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(OrganizeHistory.source_path).where(
                    OrganizeHistory.status != OrganizeStatus.FAILED,
                )
            )
        ).scalars()
        return set(rows)


async def _record_history(
    *,
    source_path: str,
    target_path: str | None,
    action: str,
    status: str,
    media_type: str | None,
    tmdb_id: int | None,
    title: str | None,
    year: int | None,
    season: int | None,
    episode: int | None,
    error: str | None,
) -> None:
    """写整理历史（按 source_path upsert）。

    失败也要记录——它是「下轮重试」的依据，也是审计日志。
    """
    from app.core.timeutils import bj_now

    async with session_scope() as session:
        row = (
            await session.execute(
                select(OrganizeHistory).where(
                    OrganizeHistory.source_path == source_path,
                )
            )
        ).scalar_one_or_none()
        values = {
            "target_path": target_path,
            "action": action,
            "status": status,
            "media_type": media_type,
            "tmdb_id": tmdb_id,
            "title": title,
            "year": year,
            "season": season,
            "episode": episode,
            "error": error,
            "organized_at": bj_now(),
        }
        if row is None:
            session.add(OrganizeHistory(source_path=source_path, **values))
        else:
            for key, value in values.items():
                setattr(row, key, value)


# ------------------------------------------------------------------ 文件收集


def _collect_videos(root: Path) -> list[Path]:
    """递归收集目录里的视频文件（同步实现，调用方须包 to_thread）。

    :param root: 源目录
    :return: 视频文件路径列表（排序，保证执行顺序稳定）
    """
    videos: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower().lstrip(".") in _VIDEO_EXTS:
            videos.append(path)
    return sorted(videos)


def _subtitle_companions(video: Path) -> list[Path]:
    """与视频同 stem 的字幕文件（同步实现，调用方须包 to_thread）。

    ``Show.S01E02.chs.ass`` 算 ``Show.S01E02.mkv`` 的字幕——``path.stem``
    只剥最后一个后缀，中文标记（``chs``）是 stem 的一部分，**不能用
    ``stem ==`` 判定**，要用「文件名以视频 stem + ``.`` 开头」。
    """
    if not video.parent.is_dir():
        return []
    prefix = video.stem + "."
    companions: list[Path] = []
    for path in sorted(video.parent.iterdir()):
        if (
            path.is_file()
            and path.name.startswith(prefix)
            and path.suffix.lower().lstrip(".") in _SUBTITLE_EXTS
        ):
            companions.append(path)
    return companions


# ------------------------------------------------------------------ 目标路径


def _movie_target(
    root: Path,
    meta: ParsedMeta,
    info_title: str,
    info_year: int | None,
    source: Path,
) -> Path:
    """电影目标路径：``标题 (年份)/标题 (年份).ext``。

    **用 TMDB 权威标题与年份**（与剧集路径同款）：文件名里解析出的标题
    常是英文原名（``Fight Club``），而 TMDB 的中文名才是媒体服务器界面
    里显示的那个（``搏击俱乐部``）。年份同理——TMDB 的首播年比文件名
    里的年份可靠。
    """
    title = sanitize(info_title)
    year_part = f" ({info_year})" if info_year else ""
    directory = root / sanitize(f"{title}{year_part}")
    filename = sanitize(f"{title}{year_part}{source.suffix.lower()}")
    return directory / filename


def _episode_target(
    root: Path, meta: ParsedMeta, info_title: str, info_year: int | None, source: Path
) -> Path:
    """剧集目标路径：``剧名 (年份)/Season NN/剧名 - SxxExx.ext``。

    目录名与剧集名用 **TMDB 权威数据**（不是文件名解析结果）——这正是
    整理的价值：识别结果是对的，重命名才可靠。
    """
    season = meta.season or 1
    episode = meta.episode or 0
    title = sanitize(info_title)
    year_part = f" ({info_year})" if info_year else ""
    directory = root / sanitize(f"{title}{year_part}") / sanitize(f"Season {season:02d}")
    filename = sanitize(f"{title} - S{season:02d}E{episode:02d}{source.suffix.lower()}")
    return directory / filename


def _subtitle_target(
    source: Path,
    original_stem: str,
    new_stem: str,
    target_dir: Path,
) -> Path:
    """字幕目标：**目标目录** + 新 stem + 原文件名 stem 之后的剩余部分。

    ``Show.S01E02.chs.ass``（stem ``Show.S01E02``）→ ``目标目录/新stem.chs.ass``；
    ``Show.S01E02.ass``（stem 完全相等）→ ``目标目录/新stem.ass``。

    tail **保留原文件名里的点**（``len(stem)`` 截断自然带出 ``.chs.ass``）。
    """
    tail = source.name[len(original_stem):]
    return target_dir / f"{new_stem}{tail}"


# ------------------------------------------------------------------ 执行


@dataclass(slots=True)
class _Identity:
    """识别结果快照（落 organize_history 用）。"""

    media_type: str
    tmdb_id: int
    title: str
    year: int | None
    season: int | None
    episode: int | None


@dataclass(slots=True)
class _FilePlan:
    """单个视频文件的整理计划。"""

    source: Path
    target: Path
    #: 字幕跟随清单 ``(源, 目标)``
    subtitles: list[tuple[Path, Path]] = field(default_factory=list)


def _disambiguate_target(target: Path) -> Path | None:
    """同名冲突时的消歧目标：``标题 (2025) - 2.ext``、``- 3``……

    用于「同一目录有多版本」（导演剪辑版 / 加长版 / 不同压制组）——它们
    合法地同名但内容不同，不该互相覆盖。

    :param target: 原始目标路径
    :return: 消歧后的路径；序号用尽（2..99 全被占）返回 None
    """
    for index in range(2, 100):
        candidate = target.with_name(f"{target.stem} - {index}{target.suffix}")
        if not candidate.exists():
            return candidate
    return None


def _execute_plan_sync(
    plan: _FilePlan, action: str, conflict_policy: str = "fail"
) -> str:
    """执行单个文件的整理计划（同步实现，调用方须包 to_thread）。

    :param conflict_policy: ``fail`` = 同名不同内容记冲突失败；
        ``disambiguate`` = 自动加序号消歧（``标题 - 2.ext``）
    :return: ``"copied"`` / ``"moved"`` / ``"skipped"`` / ``"conflict"``
    """
    source = plan.source
    target = plan.target
    if not source.exists():
        return "skipped"

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.stat().st_size == source.stat().st_size:
            # 幂等：目标已存在且大小一致 → 源文件按动作语义处理
            if action == OrganizeAction.MOVE:
                source.unlink()
                for sub_src, sub_dst in plan.subtitles:
                    if sub_src.exists():
                        sub_dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(sub_src), str(sub_dst))
            return "skipped"
        # 大小不同 = 内容冲突
        if conflict_policy == "disambiguate":
            # 多版本合法同名 → 换名入库，**不覆盖**任何已有文件
            disambiguated = _disambiguate_target(target)
            if disambiguated is None:
                return "conflict"
            original_stem = plan.source.stem
            target = disambiguated
            plan = _FilePlan(
                source=plan.source,
                target=target,
                subtitles=[
                    (
                        sub_src,
                        target.parent
                        / f"{target.stem}{sub_src.name[len(original_stem):]}",
                    )
                    for sub_src, _sub_dst in plan.subtitles
                ],
            )
        else:
            # **绝不覆盖**（不误删底线）
            return "conflict"

    shutil.copy2(str(source), str(target))
    if target.stat().st_size != source.stat().st_size:
        # 校验失败：删掉坏拷贝，保留源文件——**宁可重试也不能丢数据**
        target.unlink()
        return "conflict"

    # 字幕随视频走：copy 模式复制、move 模式搬移
    for sub_src, sub_dst in plan.subtitles:
        if not sub_src.exists():
            continue
        sub_dst.parent.mkdir(parents=True, exist_ok=True)
        if action == OrganizeAction.MOVE:
            shutil.move(str(sub_src), str(sub_dst))
        else:
            shutil.copy2(str(sub_src), str(sub_dst))

    if action == OrganizeAction.MOVE:
        source.unlink()
    return "moved" if action == OrganizeAction.MOVE else "copied"


# ------------------------------------------------------------------ 单文件整理


def _plan_for(video: Path, meta: ParsedMeta, info: Any, root: Path) -> _FilePlan | None:
    """为单个视频文件制定整理计划（含字幕跟随）。

    **同步实现**（要扫字幕目录），调用方须包 :func:`asyncio.to_thread`。
    """
    if info.media_type == MediaKind.TV and meta.episode is not None:
        target = _episode_target(root, meta, info.title, info.year, video)
    else:
        target = _movie_target(root, meta, info.title, info.year, video)
    plan = _FilePlan(source=video, target=target)
    companions = _subtitle_companions(video)
    original_stem = video.stem
    new_stem = target.stem
    target_dir = target.parent
    for companion in companions:
        plan.subtitles.append(
            (
                companion,
                _subtitle_target(companion, original_stem, new_stem, target_dir),
            )
        )
    return plan


async def _organize_video(
    video: Path,
    *,
    root: Path,
    action: str,
    service: ScrapeService,
) -> tuple[str, str]:
    """整理单个视频文件。

    :return: ``(状态, 说明)``；状态 ``ok`` / ``skip`` / ``fail``
    """
    meta = parse_meta(video.name)
    if meta is None:
        return "fail", f"文件名解析失败：{video.name}"
    if meta.is_episode and meta.season is None:
        return "fail", f"识别出集号但缺季号：{video.name}"

    media_type = MediaKind.TV if meta.is_episode else None
    try:
        info = await service.recognize_full(
            meta.title,
            year=meta.year,
            media_type=media_type,
            alt_titles=meta.alt_titles,
        )
    except ServiceError as exc:
        return "fail", f"识别失败：{exc}"
    if info is None:
        return "fail", f"未识别到媒体：{meta.title}"

    plan = await asyncio.to_thread(_plan_for, video, meta, info, root)
    if plan is None:
        return "fail", "无法制定整理计划"

    result = await asyncio.to_thread(
        _execute_plan_sync, plan, action, _conflict_policy()
    )

    identity = _Identity(
        media_type=info.media_type,
        tmdb_id=info.tmdb_id,
        title=info.title,
        year=info.year,
        season=meta.season,
        episode=meta.episode,
    )

    if result == "conflict":
        detail = f"目标冲突（已存在且大小不同）：{plan.target}"
        await _record_history(
            source_path=str(video),
            target_path=str(plan.target),
            action=action,
            status=OrganizeStatus.FAILED,
            error=detail,
            media_type=identity.media_type,
            tmdb_id=identity.tmdb_id,
            title=identity.title,
            year=identity.year,
            season=identity.season,
            episode=identity.episode,
        )
        return "fail", detail

    if result == "skipped":
        await _record_history(
            source_path=str(video),
            target_path=str(plan.target),
            action=action,
            status=OrganizeStatus.SKIPPED,
            error=None,
            media_type=identity.media_type,
            tmdb_id=identity.tmdb_id,
            title=identity.title,
            year=identity.year,
            season=identity.season,
            episode=identity.episode,
        )
        return "skip", "目标已存在（大小一致），跳过"

    await _record_history(
        source_path=str(video),
        target_path=str(plan.target),
        action=action,
        status=OrganizeStatus.SUCCESS,
        error=None,
        media_type=identity.media_type,
        tmdb_id=identity.tmdb_id,
        title=identity.title,
        year=identity.year,
        season=identity.season,
        episode=identity.episode,
    )

    # 刮削钩子：整理成功的文件立即刮（不等下一轮作业）
    scrape_fail = 0
    if plan.target.exists():
        try:
            status, _, _ = await service.scrape_video(
                plan.target, policy=ScrapeService.policy()
            )
            if status == "fail":
                scrape_fail = 1
        except ServiceError as exc:
            logger.warning("整理后刮削失败（%s）：%s", plan.target, exc)
            scrape_fail = 1

    verb = "移动" if result == "moved" else "复制"
    detail = f"{verb}：{info.title}"
    if scrape_fail:
        detail += "（刮削失败，见日志）"
    return "ok", detail


# ------------------------------------------------------------------ 作业入口


async def run_organize() -> dict[str, Any]:
    """整理作业入口。

    :return: ``{"summary": str, "steps": [JobStep]}``
    """
    from app.services.scheduler import JobStep, JobStepItem, build_items

    steps: list[JobStep] = []

    if not _runtime_flag("organize_enabled"):
        return {
            "summary": "整理未启用（运行参数「启用整理」为关闭）",
            "steps": [JobStep("整理", "skip", None, "未启用")],
        }
    root = _library_root()
    if root is None:
        return {
            "summary": "未配置本地媒体库目录，整理不可用",
            "steps": [JobStep("整理", "skip", None, "未配置本地媒体库目录")],
        }
    source_dirs = _source_dirs()
    if not source_dirs:
        return {
            "summary": "未配置整理源目录，整理不可用",
            "steps": [JobStep("整理", "skip", None, "未配置整理源目录")],
        }

    action = _action()
    done_paths = await _organized_source_paths()
    service = ScrapeService()
    items: list[JobStepItem] = []
    ok_count = 0
    skip_count = 0
    fail_count = 0

    try:
        for source_dir in source_dirs:
            if not source_dir.is_dir():
                # 源目录不存在**必须点名**——静默跳过会让「路径填错」
                # 被伪装成「目录里没有文件」
                items.append(
                    JobStepItem(
                        name=str(source_dir),
                        status="fail",
                        count=None,
                        detail="源目录不存在或不可访问",
                    )
                )
                fail_count += 1
                continue

            videos = await asyncio.to_thread(_collect_videos, source_dir)
            for video in videos:
                key = str(video)
                if key in done_paths:
                    skip_count += 1
                    continue
                try:
                    status, detail = await _organize_video(
                        video, root=root, action=action, service=service
                    )
                except Exception as exc:
                    status, detail = "fail", f"{type(exc).__name__}: {exc}"
                if status == "ok":
                    ok_count += 1
                    done_paths.add(key)
                elif status == "skip":
                    skip_count += 1
                    done_paths.add(key)
                else:
                    fail_count += 1
                items.append(
                    JobStepItem(
                        name=video.name, status=status, count=1, detail=detail
                    )
                )
    finally:
        await service.aclose()

    truncated, total = build_items(items)
    summary = f"{ok_count} 整理 / {skip_count} 跳过 / {fail_count} 失败"
    step_status = "ok" if fail_count == 0 else ("warn" if ok_count else "fail")
    steps.append(
        JobStep(
            name="整理",
            status=step_status,
            count=ok_count + skip_count + fail_count,
            detail=summary,
            items=truncated,
            items_total=total,
        )
    )
    return {"summary": summary, "steps": steps}
