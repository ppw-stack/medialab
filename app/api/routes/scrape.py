"""整理与刮削配置 / 手动刮削 / 记录查询 API。

端点（prefix 挂在 ``/api/scrape``，媒体库域——**自带前缀**，与辅种域的
``/api/v1/*`` 零冲突）：

- ``GET  /scrape/config``：当前配置（Key **不回显**，只给 ``has_key``）
- ``PUT  /scrape/config``：更新配置（Key 留空 = 不修改）
- ``POST /scrape/file``：手动刮削单个视频文件（覆盖模式，用于修识别错误）
- ``GET  /scrape/records``：刮削记录列表

路由层只做参数校验与响应组装——业务逻辑在
:mod:`app.services.scrape_service` / :mod:`app.services.scrape_jobs`。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import require_session
from app.core.logging import get_logger
from app.services.env_settings import (
    apply_env_settings,
    get_env_setting,
)
from app.services.errors import ServiceError
from app.services.runtime_params import (
    apply_runtime_params,
    get_runtime_params,
    lookup_spec,
)
from app.services.scrape_service import ScrapeService

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/scrape", tags=["scrape"], dependencies=[Depends(require_session)]
)


# ------------------------------------------------------------------ 配置


class ScrapeConfigOut(BaseModel):
    """刮削配置响应。

    ``tmdb_api_key`` **永不回显**——只给 ``has_key``（与离线下载密钥
    同一口径：泄露面最小的暴露方式）。
    """

    scrape_enabled: int
    scrape_policy: int
    has_key: bool
    media_library_dir: str
    organize_enabled: int
    organize_action: int
    organize_source_dirs: str
    #: 整理与刮削的开启条件是否齐备（前端据此显示「怎么开」而非报错入口）
    ready: bool


def _read_config() -> ScrapeConfigOut:
    """读当前配置（service 层职责，避免路由散落读取逻辑）。"""
    params = get_runtime_params()
    has_key = bool(ScrapeService.api_key())
    media_dir = get_env_setting("media_library_dir").strip()
    return ScrapeConfigOut(
        scrape_enabled=params.get_int("scrape_enabled"),
        scrape_policy=params.get_int("scrape_policy"),
        has_key=has_key,
        media_library_dir=media_dir,
        organize_enabled=params.get_int("organize_enabled"),
        organize_action=params.get_int("organize_action"),
        organize_source_dirs=get_env_setting("organize_source_dirs"),
        ready=has_key and bool(media_dir),
    )


@router.get("/config", response_model=ScrapeConfigOut, summary="刮削配置")
async def get_config() -> ScrapeConfigOut:
    """当前刮削配置。"""
    return _read_config()


class ScrapeConfigUpdateIn(BaseModel):
    """配置更新请求（部分更新；Key 留空 = 不修改）。"""

    model_config = ConfigDict(extra="forbid")

    scrape_enabled: int | None = Field(default=None, ge=0, le=1)
    scrape_policy: int | None = Field(default=None, ge=0, le=1)
    tmdb_api_key: str | None = Field(default=None, max_length=128)
    media_library_dir: str | None = Field(default=None, max_length=1024)
    organize_enabled: int | None = Field(default=None, ge=0, le=1)
    organize_action: int | None = Field(default=None, ge=0, le=1)
    organize_source_dirs: str | None = Field(default=None, max_length=4096)


#: 运行参数字段与字符串配置字段的分组（更新时各走各的持久化容器）
_RUNTIME_KEYS = ("scrape_enabled", "scrape_policy", "organize_enabled", "organize_action")
_ENV_KEYS = ("media_library_dir", "organize_source_dirs")


@router.put("/config", response_model=ScrapeConfigOut, summary="更新刮削配置")
async def update_config(body: ScrapeConfigUpdateIn) -> ScrapeConfigOut:
    """更新刮削配置。

    - 运行参数（开关 / 策略 / 动作）→ ``app_runtime_params``
    - 字符串配置（Key / 媒体库目录 / 下载器 / tag）→ ``app_env_settings``
    - ``tmdb_api_key`` 为空 = 不修改（防误清）
    """
    runtime_updates: dict[str, int] = {
        key: value
        for key in _RUNTIME_KEYS
        if (value := getattr(body, key)) is not None
    }
    env_updates: dict[str, str] = {}
    if body.tmdb_api_key is not None and body.tmdb_api_key.strip():
        env_updates["tmdb_api_key"] = body.tmdb_api_key.strip()
    for key in _ENV_KEYS:
        value = getattr(body, key)
        if value is not None:
            env_updates[key] = value.strip()

    try:
        if runtime_updates:
            await apply_runtime_params(runtime_updates)
        if env_updates:
            await apply_env_settings(env_updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return _read_config()


# ------------------------------------------------------------------ 手动刮削


class ScrapeFileIn(BaseModel):
    """手动刮削单文件请求。"""

    model_config = ConfigDict(extra="forbid")

    #: 视频文件的**绝对路径**
    path: str = Field(..., min_length=1)
    #: 指定 TMDB ID（纠正识别错误；不传走正常识别）
    tmdb_id: int | None = Field(default=None, ge=1)
    #: 是否把这次修正记成**识别别名**（下次同标题直接命中）
    remember: bool = False


class ScrapeFileOut(BaseModel):
    """手动刮削结果。"""

    status: str
    detail: str
    #: 是否已记入别名表
    remembered: bool = False


class RescrapeIn(BaseModel):
    """按记录重刮的请求。"""

    model_config = ConfigDict(extra="forbid")

    #: 刮削记录的 scope_key
    scope_key: str = Field(..., min_length=1)
    #: 指定 TMDB ID（纠正识别错误；不传则按原识别重刮）
    tmdb_id: int | None = Field(default=None, ge=1)
    #: 是否把这次修正记成识别别名
    remember: bool = False


@router.post("/file", response_model=ScrapeFileOut, summary="手动刮削单个文件")
async def scrape_file(body: ScrapeFileIn) -> ScrapeFileOut:
    """手动刮削单个视频文件。

    固定走**覆盖**策略——手动点按钮通常是想重刮，走「仅补缺」会一直跳过。

    带 ``tmdb_id`` 且勾了 ``remember`` 时，把「文件名解析出的标题 → 该 ID」
    记进别名表，下次同标题直接命中（不再靠搜索猜）。
    """
    if not ScrapeService.api_key():
        raise HTTPException(status_code=400, detail="未配置 TMDB API Key")

    from app.domain.scrape_types import ScrapePolicy

    service = ScrapeService()
    try:
        status, detail, remembered = await service.scrape_video(
            Path(body.path),
            policy=ScrapePolicy.OVERWRITE,
            tmdb_id=body.tmdb_id,
            remember_alias=body.remember,
        )
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    finally:
        await service.aclose()
    return ScrapeFileOut(status=status, detail=detail, remembered=remembered)


# ------------------------------------------------------------------ 记录


@router.get("/records", summary="刮削记录列表")
async def list_records(
    limit: int = 100, offset: int = 0
) -> dict[str, object]:
    """刮削记录（按更新时间倒序）。"""
    if not ScrapeService.api_key():
        # 未配置 Key 时记录表必然为空，直接返回空集，不报错
        return {"total": 0, "items": []}
    service = ScrapeService()
    return await service.list_records(limit=limit, offset=offset)


@router.post("/records/rescrape", response_model=ScrapeFileOut, summary="按记录重刮")
async def rescrape_record(body: RescrapeIn) -> ScrapeFileOut:
    """按刮削记录重刮——「一键修正」的入口。

    场景：记录里看到识别错了（标题不对 / 集数不对），填上正确的 TMDB ID
    重刮；勾了「记住修正」还会把该标题存进别名表，下次自动命中。
    """
    if not ScrapeService.api_key():
        raise HTTPException(status_code=400, detail="未配置 TMDB API Key")

    service = ScrapeService()
    try:
        status, detail, remembered = await service.rescrape(
            body.scope_key,
            tmdb_id=body.tmdb_id,
            remember=body.remember,
        )
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    finally:
        await service.aclose()
    return ScrapeFileOut(status=status, detail=detail, remembered=remembered)


# ------------------------------------------------------------------ 别名


class ScrapeAliasOut(BaseModel):
    """别名条目。"""

    id: int
    alias_text: str
    alias_key: str
    tmdb_id: int
    media_type: str | None
    note: str | None
    updated_at: str


class ScrapeAliasIn(BaseModel):
    """别名新增 / 更新请求。"""

    model_config = ConfigDict(extra="forbid")

    #: 标题（识别时用它匹配）
    alias_text: str = Field(..., min_length=1, max_length=512)
    #: 目标 TMDB ID
    tmdb_id: int = Field(..., ge=1)
    #: 媒体类型；不传 = 不限
    media_type: str | None = Field(default=None)
    #: 备注（为什么改）
    note: str | None = Field(default=None, max_length=1024)


@router.get("/aliases", summary="识别别名列表")
async def list_aliases() -> list[ScrapeAliasOut]:
    """全部识别别名（按更新时间倒序）。"""
    service = ScrapeService()
    return [ScrapeAliasOut(**item) for item in await service.list_aliases()]


@router.post("/aliases", summary="新增 / 更新识别别名")
async def save_alias(body: ScrapeAliasIn) -> ScrapeAliasOut:
    """新增或更新一条别名（同标题覆盖）。

    别名在识别时**优先于搜索**——固化一次，永久生效。
    """
    service = ScrapeService()
    try:
        item = await service.save_alias(
            body.alias_text,
            body.tmdb_id,
            media_type=body.media_type,
            note=body.note,
        )
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return ScrapeAliasOut(**item)


@router.delete("/aliases/{alias_id}", summary="删除识别别名")
async def delete_alias(alias_id: int) -> dict[str, bool]:
    """删除一条别名。"""
    service = ScrapeService()
    return {"deleted": await service.delete_alias(alias_id)}


def _assert_specs_registered() -> None:
    """导入时校验：本模块引用的运行参数必须已注册。

    与 runtime_params 的 ``_assert_enum_options_consistent()`` 同款思路——
    参数名手写错了（比如改名后漏改这里）会在**导入时**报错，而不是
    等用户点保存时才发现。
    """
    for key in ("scrape_enabled", "scrape_policy", "organize_enabled", "organize_action"):
        if lookup_spec(key) is None:
            raise RuntimeError(f"运行参数未注册：{key}")


_assert_specs_registered()
