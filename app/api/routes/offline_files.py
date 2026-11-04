"""离线下载的**只读 HTTP 服务**（供光鸭离线下载器拉取本地文件）。

**为什么需要它**：数据同步是「128 → 光鸭 OSS」的**上行**，受本机上行
带宽限制（实测 ~3.3 MB/s，见 ``docs/upload-bandwidth.md``）。光鸭的
离线下载是**光鸭服务器主动来拉**——流量方向是**下行**，本机只承担接收，
上行需求 ≈ 0。

**为什么在 api 层**：本模块用 FastAPI 路由，故放 ``app/api/routes/``——
services 层不得 import fastapi（``tests/unit/test_layering.py``）。
纯逻辑（token / 路径安全 / Range 解析）在
:mod:`app.services.offline_files`。

**必须处理的两件事**（实测 2026-11-04，真实光鸭账号）：

1. **HEAD 请求**——光鸭先探文件大小再决定怎么拉。不支持 → 405 →
   任务可能直接失败。故 GET 与 HEAD 都要注册。
2. **双重编码的 URL**——光鸭有一条重试路径会把 ``%`` 再编码成 ``%25``。
   那批请求原本全部 403（token 校验失败）。见
   :func:`app.services.offline_files.decode_rel_path`。

**Range 支持**：光鸭可能断点续传，必须返回 206 + ``Content-Range``。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from app.core.logging import get_logger
from app.services.offline_config import current_config
from app.services.offline_files import (
    URL_PREFIX,
    decode_rel_path,
    parse_range,
    safe_join,
    verify_token,
)

logger = get_logger(__name__)

#: 分块大小（256 KB）——大文件不能一次性读进内存。
CHUNK_SIZE = 256 * 1024


async def _iter_file(path: Path, start: int, length: int) -> AsyncIterator[bytes]:
    """按块产出文件内容（``start`` 起共 ``length`` 字节）。

    用 ``asyncio.to_thread`` 读文件——CIFS 上的读是阻塞 IO，直接在
    事件循环里读会卡住整个服务（文档里记过「prod 事件循环被卡死」的教训）。
    """
    left = length
    with path.open("rb") as fh:
        if start:
            await asyncio.to_thread(fh.seek, start)
        while left > 0:
            chunk = await asyncio.to_thread(fh.read, min(CHUNK_SIZE, left))
            if not chunk:
                break
            left -= len(chunk)
            yield chunk


def mount_offline_files(app: FastAPI) -> bool:
    """把只读文件服务挂到 app 上。

    ⚠️ **无条件挂载**——FastAPI 不支持运行时增删路由，而配置（密钥 /
    公网入口 / 源根目录）现在可以在**页面上改**（DB 持久化，改完立即生效，
    不用重启容器）。故路由常驻，**每次请求时**读当前配置决定放行与否。

    未启用（三项未配齐）时返回 **403**——比让 SPA 兜底吞掉更诚实
    （明确「这里有个服务但没开」，而不是「页面不存在」）。

    Args:
        app: FastAPI 应用。

    Returns:
        True = 已挂载。
    """
    # ⚠️ **router 必须在这里新建**——不能用模块级全局对象。
    # 模块级 router 被多次 include_router 到不同 app 时，路由会绑在
    # **第一次**的 app 上（后挂的 app 拿不到），测试里尤其明显。
    router = APIRouter(tags=["offline-files"], include_in_schema=False)

    async def _serve(
        token: str, raw_path: str, request: Request, *, head_only: bool
    ) -> Response:
        """按 token 暴露根目录下的一个文件（**只读**）。"""
        cfg = current_config()
        if not cfg.enabled:
            raise HTTPException(
                status_code=403, detail="离线下载未启用（配置不完整）"
            )

        rel_path = decode_rel_path(raw_path)

        if not verify_token(cfg.secret, rel_path, token):
            logger.warning("离线下载 token 校验失败：%s", raw_path)
            raise HTTPException(status_code=403, detail="token 无效")

        path = safe_join(Path(cfg.files_root), rel_path)
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")

        size = path.stat().st_size

        # HEAD：光鸭**先探文件大小**再决定怎么拉（实测会发 HEAD）。
        # 不支持 HEAD → 405，光鸭可能因此判定任务失败。
        if head_only:
            return Response(
                status_code=200,
                media_type="application/octet-stream",
                headers={
                    "Content-Length": str(size),
                    "Accept-Ranges": "bytes",
                },
            )

        rng = parse_range(request.headers.get("range"), size)

        if rng is None:
            # 整体返回——小文件走这条（FileResponse 自带 Range 支持）
            return FileResponse(
                path,
                media_type="application/octet-stream",
                filename=path.name,
                headers={"Accept-Ranges": "bytes"},
            )

        start, end = rng
        length = end - start + 1
        logger.info(
            "离线下载文件被拉取：%s（%d-%d / %d）", rel_path, start, end, size
        )
        return StreamingResponse(
            _iter_file(path, start, length),
            status_code=206,
            media_type="application/octet-stream",
            headers={
                "Content-Length": str(length),
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Accept-Ranges": "bytes",
            },
        )

    @router.get(f"{URL_PREFIX}/{{token}}/{{rel_path:path}}")
    async def serve_offline_file(
        token: str, rel_path: str, request: Request
    ) -> Response:
        """按 token 暴露根目录下的一个文件（**只读**）。"""
        return await _serve(token, rel_path, request, head_only=False)

    @router.head(f"{URL_PREFIX}/{{token}}/{{rel_path:path}}")
    async def head_offline_file(
        token: str, rel_path: str, request: Request
    ) -> Response:
        """只回文件大小（光鸭的下载器**先探大小**再决定怎么拉）。"""
        return await _serve(token, rel_path, request, head_only=True)

    app.include_router(router)
    logger.info(
        "已挂载离线下载文件服务（前缀 %s，未配齐三项配置时返回 403）",
        URL_PREFIX,
    )
    return True


__all__ = ["mount_offline_files"]
