"""下载器管理 API（FR-1.1）。

覆盖：
- 列表 / 详情 / 新增 / 更新 / 删除
- 测连（Ping）：走实际适配器，验证 URL/凭据可用性
- 凭据：密码经 AES-GCM 加密存储（FR-7.1），输出按 R6 脱敏

不在此模块做协议级联（这些由 :class:`AdapterFactory` 缓存实例并在
配置变更时通过 :meth:`invalidate_downloader` 重建）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.adapters.downloaders.base import DownloaderError
from app.api.auth import require_session
from app.core.crypto import build_cipher
from app.core.config import get_settings
from app.db.models import DownloaderInstance, Site
from app.db.session import session_scope
from app.services.factory import get_factory

router = APIRouter(
    prefix="/downloaders",
    dependencies=[Depends(require_session)],
    tags=["downloaders"],
)

#: 已实现的下载器类型（M1+M2 范围）。其他类型后续按 FR-1 补齐。
SUPPORTED_TYPES: tuple[str, ...] = ("qbittorrent",)
ALL_TYPES: tuple[str, ...] = ("qbittorrent", "transmission", "deluge", "aria2")


class DownloaderIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    type: str = Field(..., description="qbittorrent/transmission/deluge/aria2")
    url: str = Field(..., min_length=1, max_length=512)
    username: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=512)
    download_root: str | None = Field(default=None, max_length=512)
    max_upload: str | None = Field(default=None, max_length=32)
    max_download: str | None = Field(default=None, max_length=32)
    enabled: bool = True
    verify_tls: bool = True


class DownloaderUpdate(BaseModel):
    id: int  # 必填：标识要更新的下载器
    name: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    url: str | None = Field(default=None, min_length=1, max_length=512)
    username: str | None = Field(default=None, max_length=128)
    # 留空字符串表示不修改凭据
    password: str | None = Field(default=None, max_length=512)
    download_root: str | None = Field(default=None, max_length=512)
    max_upload: str | None = Field(default=None, max_length=32)
    max_download: str | None = Field(default=None, max_length=32)
    enabled: bool | None = None
    verify_tls: bool | None = None


class DownloaderOut(BaseModel):
    id: int
    name: str
    type: str
    url: str
    username: str | None
    has_password: bool
    download_root: str | None
    max_upload: str | None
    max_download: str | None
    enabled: bool
    verify_tls: bool
    capabilities: dict[str, object]
    created_at: str
    updated_at: str


def _to_out(row: DownloaderInstance) -> DownloaderOut:
    return DownloaderOut(
        id=row.id,
        name=row.name,
        type=row.type,
        url=row.url,
        username=row.username,
        has_password=bool(row.password_enc),
        download_root=row.download_root,
        max_upload=row.max_upload,
        max_download=row.max_download,
        enabled=row.enabled,
        verify_tls=row.verify_tls,
        capabilities=row.capabilities or {},
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
    )


@router.get("", summary="下载器列表")
async def list_downloaders() -> list[DownloaderOut]:
    async with session_scope() as session:
        rows = (await session.execute(select(DownloaderInstance).order_by(DownloaderInstance.id))).scalars().all()
    return [_to_out(r) for r in rows]


@router.post("", status_code=201, summary="新增下载器")
async def create_downloader(payload: DownloaderIn) -> DownloaderOut:
    if payload.type not in ALL_TYPES:
        raise HTTPException(status_code=400, detail=f"未知下载器类型：{payload.type}")
    if payload.type not in SUPPORTED_TYPES:
        raise HTTPException(
            status_code=501,
            detail=f"下载器类型 {payload.type} 尚未实现（FR-1 后续版本提供）",
        )
    cipher = build_cipher(get_settings())
    async with session_scope() as session:
        if (await session.execute(select(DownloaderInstance).where(DownloaderInstance.name == payload.name))).first():
            raise HTTPException(status_code=409, detail=f"下载器 {payload.name} 已存在")
        row = DownloaderInstance(
            name=payload.name,
            type=payload.type,
            url=payload.url,
            username=payload.username,
            download_root=payload.download_root,
            max_upload=payload.max_upload,
            max_download=payload.max_download,
            enabled=payload.enabled,
            verify_tls=payload.verify_tls,
            key_version=get_settings().key_version,
        )
        if payload.password:
            row.password_enc = cipher.encrypt(payload.password, owner_type="downloader", owner_id=payload.name)
        session.add(row)
        await session.flush()
        return _to_out(row)


@router.get("/{downloader_id}", summary="下载器详情")
async def get_downloader(downloader_id: int) -> DownloaderOut:
    async with session_scope() as session:
        row = (await session.execute(select(DownloaderInstance).where(DownloaderInstance.id == downloader_id))).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="下载器不存在")
        return _to_out(row)


@router.post("/update", summary="更新下载器")
async def update_downloader(payload: DownloaderUpdate) -> DownloaderOut:
    downloader_id = payload.id
    cipher = build_cipher(get_settings())
    settings = get_settings()
    async with session_scope() as session:
        row = (await session.execute(select(DownloaderInstance).where(DownloaderInstance.id == downloader_id))).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="下载器不存在")
        if payload.name and payload.name != row.name:
            if (await session.execute(select(DownloaderInstance).where(DownloaderInstance.name == payload.name))).first():
                raise HTTPException(status_code=409, detail=f"下载器 {payload.name} 已存在")
            row.name = payload.name
        if payload.url is not None:
            row.url = payload.url
        if payload.username is not None:
            row.username = payload.username
        if payload.password:  # 留空 = 保留
            row.password_enc = cipher.encrypt(payload.password, owner_type="downloader", owner_id=row.name)
            row.key_version = settings.key_version
        if payload.download_root is not None:
            row.download_root = payload.download_root
        if payload.max_upload is not None:
            row.max_upload = payload.max_upload
        if payload.max_download is not None:
            row.max_download = payload.max_download
        if payload.enabled is not None:
            row.enabled = payload.enabled
        if payload.verify_tls is not None:
            row.verify_tls = payload.verify_tls
        await session.flush()
        # 配置变更 → 失效缓存中的适配器实例
        get_factory().invalidate_downloader(row.id)
        return _to_out(row)


@router.post("/delete", summary="删除下载器")
async def delete_downloader(payload: dict) -> dict[str, int]:
    downloader_id = payload.get("id") if isinstance(payload, dict) else None
    if downloader_id is None:
        raise HTTPException(status_code=400, detail="id 必填")
    async with session_scope() as session:
        row = (await session.execute(select(DownloaderInstance).where(DownloaderInstance.id == downloader_id))).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="下载器不存在")
        # 若有站点仍引用此下载器，禁止删除（避免孤儿指向）
        bound = (
            await session.execute(select(Site.id).where(Site.downloader_id == downloader_id))
        ).first()
        if bound is not None:
            raise HTTPException(
                status_code=409,
                detail="仍有站点引用此下载器，请先解绑",
            )
        await session.delete(row)
    get_factory().invalidate_downloader(downloader_id)
    return {"id": downloader_id}


@router.post("/{downloader_id}/test", summary="测连：实际适配器 ping")
async def test_downloader(downloader_id: int) -> dict[str, object]:
    """走实际适配器验证 URL/凭据，返回连通性 + 能力快照。

    能力快照由 ping 后再 list_torrents + get_files 探测（仅当 ping 成功），
    用于前端展示「已支持数据删除」徽标。
    """
    async with session_scope() as session:
        row = (await session.execute(select(DownloaderInstance).where(DownloaderInstance.id == downloader_id))).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="下载器不存在")
        if not row.enabled:
            raise HTTPException(status_code=400, detail="下载器已禁用")
        # 在 session 内部拷贝一份配置，避免后续在 factory 缓存时使用过期 row
        snapshot = DownloaderInstance(
            id=row.id, name=row.name, type=row.type, url=row.url,
            username=row.username, password_enc=row.password_enc,
            download_root=row.download_root, max_upload=row.max_upload,
            max_download=row.max_download, enabled=row.enabled,
            verify_tls=row.verify_tls, key_version=row.key_version,
            capabilities=row.capabilities or {},
        )
    try:
        adapter = get_factory().downloader(snapshot)
        reachable = await adapter.ping()
    except DownloaderError as exc:
        return {"reachable": False, "detail": str(exc), "capabilities": {}}
    except Exception as exc:  # noqa: BLE001 — 测连要对所有错误兜底
        return {"reachable": False, "detail": f"{type(exc).__name__}: {exc}", "capabilities": {}}
    if not reachable:
        return {"reachable": False, "detail": "ping 返回失败（网络/凭据）", "capabilities": {}}
    caps = adapter.capabilities()
    # 同步能力快照回 DB，便于无连接场景下也能展示
    async with session_scope() as session:
        row = (await session.execute(select(DownloaderInstance).where(DownloaderInstance.id == downloader_id))).scalar_one_or_none()
        if row is not None:
            row.capabilities = {"features": sorted(f.value for f in caps.features)}
    return {
        "reachable": True,
        "detail": "ok",
        "capabilities": {"features": sorted(f.value for f in caps.features)},
        "supports_data_deletion": caps.supports_data_deletion(),
    }
