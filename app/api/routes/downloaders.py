"""下载器管理 API（FR-1.1）。

路由层只做三件事：**参数校验 → 调 service → 结果转换**。

业务逻辑（DB 查询、适配器构造、网络调用）全在
:mod:`app.services.downloader_service`。服务层抛
:class:`app.services.errors.ServiceError`，由全局异常处理器（见
:mod:`app.api.errors`）转成对应的 HTTP 状态码。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.auth import require_session
from app.services import downloader_service
from app.services.downloader_service import (
    ALL_TYPES,
    BATCH_LIMIT,
    SUPPORTED_TYPES,
)

router = APIRouter(
    prefix="/downloaders",
    dependencies=[Depends(require_session)],
    tags=["downloaders"],
)

__all__ = ["ALL_TYPES", "BATCH_LIMIT", "SUPPORTED_TYPES"]


class DownloaderIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    type: str = Field(..., description="qbittorrent/transmission/deluge/aria2")
    url: str = Field(..., min_length=1, max_length=512)
    username: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=512)
    save_path: str | None = Field(default=None, max_length=512)
    is_default: bool = False
    enabled: bool = True
    verify_tls: bool = True
    is_cross_seed: bool = False


class DownloaderUpdate(BaseModel):
    id: int
    name: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$"
    )
    url: str | None = Field(default=None, max_length=512)
    username: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=512)
    save_path: str | None = Field(default=None, max_length=512)
    is_default: bool | None = None
    enabled: bool | None = None
    verify_tls: bool | None = None
    is_cross_seed: bool | None = None


class DownloaderTestIn(BaseModel):
    """测连请求体（测**未保存**的表单值）。

    编辑弹窗里用户可能刚改了 URL / 账号 / 密码就点"测连"——
    若只按 id 测 DB 里的旧配置，测出来的结果与表单内容矛盾。
    故提供"按表单值测"的入口：全部字段可选，缺省时回落到 DB 里的值。
    """

    id: int
    type: str | None = Field(default=None, description="下载器类型")
    url: str | None = Field(default=None, max_length=512)
    username: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=512)
    verify_tls: bool | None = None


class DownloaderOut(BaseModel):
    id: int
    name: str
    type: str
    url: str
    username: str | None
    has_password: bool
    save_path: str | None
    is_default: bool
    enabled: bool
    verify_tls: bool
    is_cross_seed: bool
    created_at: str
    updated_at: str


class DownloaderTorrentOut(BaseModel):
    """下载器原生任务视图（来自下载器 adapter.list_torrents）。"""

    hash: str
    name: str
    state: str
    progress: float
    size: int
    downloaded: int
    uploaded: int
    dlspeed: int
    upspeed: int
    ratio: float
    save_path: str | None = None
    category: str | None = None
    tags: list[str] = []


class DownloaderBatchOpRequest(BaseModel):
    hashes: list[str] = Field(..., min_length=1)
    delete_data: bool = False
    paused: bool = True


class DownloaderBatchOpResult(BaseModel):
    requested: int
    succeeded: int
    failed: int
    errors: list[str] = []


@router.get("", summary="下载器列表")
async def list_downloaders() -> list[DownloaderOut]:
    return [DownloaderOut(**asdict(d)) for d in await downloader_service.list_downloaders()]


@router.get("/{downloader_id}", summary="下载器详情")
async def get_downloader(downloader_id: int) -> DownloaderOut:
    return DownloaderOut(**asdict(await downloader_service.get_downloader(downloader_id)))


@router.post("", status_code=201, summary="新增下载器")
async def create_downloader(payload: DownloaderIn) -> DownloaderOut:
    snap = await downloader_service.create_downloader(
        name=payload.name,
        type=payload.type,
        url=payload.url,
        username=payload.username,
        password=payload.password,
        save_path=payload.save_path,
        is_default=payload.is_default,
        enabled=payload.enabled,
        verify_tls=payload.verify_tls,
    )
    return DownloaderOut(**asdict(snap))


@router.post("/update", summary="更新下载器")
async def update_downloader(payload: DownloaderUpdate) -> DownloaderOut:
    """更新下载器配置（id 在 body）。

    ``password`` / ``save_path`` 留空字符串 = 清空该项；
    未提供（None） = 不修改。
    """
    fields = payload.model_fields_set
    snap = await downloader_service.update_downloader(
        payload.id,
        name=payload.name,
        url=payload.url,
        username=payload.username,
        password=payload.password if "password" in fields else None,
        save_path=payload.save_path if "save_path" in fields else None,
        is_default=payload.is_default,
        enabled=payload.enabled,
        verify_tls=payload.verify_tls,
        is_cross_seed=payload.is_cross_seed,
    )
    return DownloaderOut(**asdict(snap))


class DownloaderDeleteIn(BaseModel):
    id: int = Field(..., ge=1)


@router.post("/delete", summary="删除下载器")
async def delete_downloader(payload: DownloaderDeleteIn) -> dict[str, int]:
    return {"id": await downloader_service.delete_downloader(payload.id)}


@router.post("/{downloader_id}/test", summary="测连（验证 URL + 凭据）")
async def test_downloader(downloader_id: int) -> dict[str, Any]:
    reachable = await downloader_service.test_downloader(downloader_id)
    return {
        "reachable": reachable,
        "detail": "ok" if reachable else "unreachable",
    }


@router.post("/test-config", summary="测连（用未保存的表单值）")
async def test_downloader_config(payload: DownloaderTestIn) -> dict[str, Any]:
    """用**表单里的值**测连（不落库）。

    编辑弹窗的"测连"按钮与凭据输入框相邻，用户很可能刚改完 URL/账号/
    密码就点测连。按 id 测 DB 里的旧配置会得到与表单矛盾的结果
    （表单看着是新的，测的是旧的）。故这里用表单值构造临时适配器测。

    未提供的字段回落到 DB 里的值（编辑时只改了部分字段也能测）。
    """
    reachable = await downloader_service.test_downloader_config(
        payload.id,
        type=payload.type,
        url=payload.url,
        username=payload.username,
        password=payload.password,
        verify_tls=payload.verify_tls,
    )
    return {
        "reachable": reachable,
        "detail": "ok" if reachable else "unreachable",
    }


# ================================================================ 下载器原生任务


@router.get("/{downloader_id}/torrents", summary="列出下载器原生任务")
async def list_downloader_torrents(
    downloader_id: int,
    state: str | None = None,
    category: str | None = None,
) -> dict[str, Any]:
    """调 downloader.adapter.list_torrents() 返回原生任务视图。

    过滤：state（downloading/uploading/paused/error 等）/ category。
    """
    items = await downloader_service.list_downloader_torrents(
        downloader_id, state=state, category=category
    )
    return {
        "total": len(items),
        "items": [DownloaderTorrentOut(**asdict(t)) for t in items],
    }


@router.post("/{downloader_id}/torrents/pause", summary="暂停/恢复下载器原生任务")
async def pause_downloader_torrents(
    downloader_id: int, payload: DownloaderBatchOpRequest
) -> DownloaderBatchOpResult:
    result = await downloader_service.pause_downloader_torrents(
        downloader_id, payload.hashes, paused=payload.paused
    )
    return DownloaderBatchOpResult(**asdict(result))


@router.post("/{downloader_id}/torrents/remove", summary="删除下载器原生任务")
async def remove_downloader_torrents(
    downloader_id: int, payload: DownloaderBatchOpRequest
) -> DownloaderBatchOpResult:
    result = await downloader_service.remove_downloader_torrents(
        downloader_id, payload.hashes, delete_data=payload.delete_data
    )
    return DownloaderBatchOpResult(**asdict(result))
