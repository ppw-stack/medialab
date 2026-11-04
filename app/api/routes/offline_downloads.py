"""离线下载路由（本地文件 → 光鸭，走光鸭服务端拉取）。

与 :mod:`app.api.routes.migrations` 同款结构：只有「手动触发」。

**与「数据同步」（``/upload-tasks``）的区别**：数据同步是**同步**语义
（逐文件直传，调用返回时文件已在云端）；这里是**异步**语义（提交离线
任务，后台跑，前端轮询）。两者**不共用代码路径**。

**与「迁移」（``/migrations``）的区别**：迁移的源是 115（换直链后交给光鸭
拉）；这里的源是**本地文件**（经本机的只读 HTTP 服务交给光鸭拉）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import require_session
from app.services import offline_jobs
from app.services.offline_config import (
    KEY_FILES_ROOT,
    KEY_PUBLIC_BASE,
    KEY_SECRET,
    OfflineConfigError,
    apply_offline_config,
    current_config,
    normalize_public_base,
)
from app.services.offline_download import (
    OfflineParams,
)

router = APIRouter(
    prefix="/api/offline-downloads",
    tags=["offline-downloads"],
    dependencies=[Depends(require_session)],
)


class OfflineRunIn(BaseModel):
    """开跑参数。"""

    model_config = ConfigDict(extra="forbid")

    #: 目标光鸭 driver id
    target_driver_id: int
    #: 目标光鸭目录 fileId（空 = 根目录）
    target_file_id: str = Field(default="", max_length=32)
    #: 目标路径（只读展示）
    target_path: str = Field(default="", max_length=1024)
    #: 本地根目录（容器内绝对路径）
    local_root: str = Field(..., min_length=1, max_length=1024)
    #: 递归子目录
    include_subdirs: bool = True
    #: 体积下限（MB）
    min_size_mb: int = Field(default=0, ge=0)
    #: 体积上限（MB），0 = 不限
    max_size_mb: int = Field(default=0, ge=0)
    #: 排除原盘（iso / m2ts / BDMV / CERTIFICATE）
    exclude_disc: bool = True
    #:
    #: **只要这些扩展名**（逗号分隔，如 ``mkv,mp4``）。空 = 不限（全传）。
    #: 用于「只传视频，跳过 nfo / 字幕 / 海报」。
    exts: str = Field(default="", max_length=256)
    #:
    #: **下载成功后删除本地文件**（不可逆）。
    #:
    #: 破坏性操作——但有三道保护（与「数据同步」同一套
    #: :mod:`app.services.upload_cleanup` 机制）：
    #:
    #: 1. 只对**确实下载成功**的文件删（云端出现同名同大小才算）
    #: 2. 云端大小必须与本地一致
    #: 3. 路径安全（普通文件 / 非隐藏 / 在源根目录内）
    #:
    #: 空目录会一并删掉（向上递归，源根目录本身保留）。
    delete_after_upload: bool = False
    #: 同时进行的离线任务数
    concurrency: int = Field(default=2, ge=1, le=20)
    #: 两次提交之间的最小间隔（秒）
    submit_interval_sec: float = Field(default=2.0, ge=0.0, le=600.0)


@router.get("/capabilities")
async def capabilities() -> dict[str, Any]:
    """离线下载的能力与配置（前端用它决定显示什么）。"""
    cfg = current_config()
    return {
        "enabled": cfg.enabled,
        "public_base": cfg.public_base,
        "files_root": cfg.files_root,
        # 源目录浏览的根（前端从这里开始逐层选）
        "browse_root": cfg.files_root,
        # 三项配置是否齐全（前端逐项显示「已配置 / 未配置」）
        "has_secret": bool(cfg.secret),
        "has_public_base": bool(cfg.public_base),
        "has_files_root": bool(cfg.files_root),
        "hint": (
            "离线下载需要配齐三项：密钥、公网入口、源根目录。"
            "在本页「配置」区改完立即生效（不用重启容器）。"
            "不用时把端口映射关掉即可——服务没配齐时不暴露任何文件。"
        ),
    }


class OfflineConfigIn(BaseModel):
    """离线下载配置（partial update——只传要改的）。"""

    model_config = ConfigDict(extra="forbid")

    #: HMAC 密钥（省略/空 = 不修改——密钥永不回显，不能用空串清除）
    secret: str | None = Field(default=None, max_length=128)
    #: 公网入口
    public_base: str | None = Field(default=None, max_length=256)
    #: 源根目录
    files_root: str | None = Field(default=None, max_length=1024)


@router.put("/config")
async def update_config(payload: OfflineConfigIn) -> dict[str, Any]:
    """修改离线下载配置（**落库 + 立即生效**，不用重启容器）。

    三项配置写进 ``app_env_settings`` 表（与「辅种扫描目录」同一套机制），
    文件服务**每次请求时**读当前配置决定放行与否——故改完立即生效。
    """
    updates: dict[str, str] = {}
    # 密钥留空 = 不修改（它永不回显，故「空串」不能解释成「清除」）
    if payload.secret:
        updates[KEY_SECRET] = payload.secret.strip()
    if payload.public_base is not None:
        updates[KEY_PUBLIC_BASE] = normalize_public_base(payload.public_base)
    if payload.files_root is not None:
        updates[KEY_FILES_ROOT] = payload.files_root.strip()

    try:
        await apply_offline_config(updates)
    except OfflineConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # DB 写失败：内存未变，但要明确告知
        raise HTTPException(status_code=500, detail=f"保存配置失败：{exc}") from exc

    return await capabilities()


@router.get("/local-dirs")
async def browse_local_dirs(path: str = "/") -> dict[str, Any]:
    """浏览**本地**目录（列子目录）——用来选源根目录 / 源目录。

    **没有白名单**：目录完全由用户在面板上配置，与「数据同步」的
    ``/upload-tasks/local-dirs`` 一致。**因此这个端点能列出服务进程能
    看到的任何目录**——包括 ``/app``（含明文密钥的 ``.env``）。故它与
    「面板登录」同级别地敏感（面板本身已是管理员权限）。
    """
    from pathlib import Path

    from app.services.upload_paths import list_subdirs

    target = Path(path or "/")
    if not target.is_absolute():
        raise HTTPException(status_code=400, detail="必须是绝对路径")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail=f"目录不存在：{target}")
    return {
        "path": str(target),
        # ``id`` = 完整路径（前端拿它当「下一层路径」直接请求）
        "dirs": [
            {"id": full, "name": full.rsplit("/", 1)[-1]}
            for full in list_subdirs(target)
        ],
    }


@router.get("/local-files")
async def list_local_files(path: str = "") -> dict[str, Any]:
    """列出源目录下的**文件**（前端展示「将要传哪些」）。

    只列文件、不递归——递归由任务参数 ``include_subdirs`` 控制。
    """
    from pathlib import Path

    target = Path(path or "/")
    if not target.is_absolute():
        raise HTTPException(status_code=400, detail="必须是绝对路径")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail=f"目录不存在：{target}")

    files: list[dict[str, Any]] = []
    for entry in sorted(target.iterdir(), key=lambda p: p.name):
        if not entry.is_file() or entry.is_symlink():
            continue
        try:
            size = entry.stat().st_size
        except OSError:
            continue
        files.append({"name": entry.name, "size": size})
    return {"path": str(target), "files": files}


@router.get("/targets")
async def browse_targets(driver_id: int, parent_id: str = "") -> dict[str, Any]:
    """列出光鸭目录下的子目录（按 fileId 逐层浏览）。"""
    from app.services.migration import MigrationBrowseError, browse_target_dirs

    try:
        dirs = await browse_target_dirs(driver_id, parent_id)
    except MigrationBrowseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {
        "driver_id": driver_id,
        "parent_id": parent_id,
        "dirs": [{"id": rid, "name": name} for rid, name in dirs],
    }


@router.post("/run", status_code=201)
async def run_offline(payload: OfflineRunIn) -> dict[str, Any]:
    """开跑（**立即返回 job_id**，下载在后台跑）。"""
    cfg = current_config()
    if not cfg.secret:
        raise HTTPException(
            status_code=503,
            detail="未配置离线下载密钥，离线下载未启用",
        )
    if not cfg.public_base:
        raise HTTPException(
            status_code=503,
            detail="未配置离线下载公网入口，无法生成公网 URL",
        )

    root = cfg.files_root
    if not root:
        raise HTTPException(
            status_code=503,
            detail="未配置离线下载源根目录，不知道要暴露哪个目录",
        )

    # 源目录必须在源根目录之下——文件服务按源根目录解析 URL 路径并验
    # token，之外的文件**物理上不可暴露**（2026-11-04 prod 事故的防线：
    # 不在提交时拦下，任务就会跑起来后每个文件都 404）。
    from pathlib import Path

    local = Path(payload.local_root).resolve()
    files_root = Path(root).resolve()
    if local != files_root and files_root not in local.parents:
        raise HTTPException(
            status_code=400,
            detail=f"源目录必须在源根目录（{files_root}）之下：{payload.local_root}",
        )

    running = await offline_jobs.find_running()
    if running is not None:
        raise HTTPException(
            status_code=409,
            detail=f"已有离线下载任务在跑（job={running.id}）",
        )

    # 扩展名过滤：逗号分隔 → 去空白 / 去点 / 小写 / 去重
    exts = tuple(
        dict.fromkeys(
            e.strip().lower().lstrip(".")
            for e in payload.exts.split(",")
            if e.strip().strip(".")
        )
    )

    params = OfflineParams(
        target_driver_id=payload.target_driver_id,
        target_file_id=payload.target_file_id,
        target_path=payload.target_path,
        local_root=payload.local_root,
        include_subdirs=payload.include_subdirs,
        min_size_mb=payload.min_size_mb,
        max_size_mb=payload.max_size_mb,
        exclude_disc=payload.exclude_disc,
        exts=exts,
        delete_after_upload=payload.delete_after_upload,
        concurrency=payload.concurrency,
        submit_interval_sec=payload.submit_interval_sec,
        public_base=cfg.public_base,
        secret=cfg.secret,
        files_root=cfg.files_root,
    )
    job = await offline_jobs.start_offline_job(params)
    return {"job_id": job.id, "state": job.state}


@router.get("/jobs")
async def list_jobs() -> list[dict[str, Any]]:
    """最近的离线下载任务（最新在前）——刷新页面可找回进度。"""
    jobs = await offline_jobs.list_jobs()
    return [j.to_dict() for j in jobs]


@router.get("/jobs/{job_id}")
async def job_status(job_id: str) -> dict[str, Any]:
    """查询离线下载进度（前端每 2s 拉一次）。"""
    job = await offline_jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="离线下载任务不存在")
    return job.to_dict()


@router.post("/jobs/{job_id}/cancel")
async def cancel(job_id: str) -> dict[str, bool]:
    """取消离线下载（停止提交；已提交的离线任务留在光鸭继续跑）。"""
    ok = await offline_jobs.cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在或已结束")
    return {"ok": True}
