"""中转路由：``/302/<media_id>/<sig>``。

**这是整个项目的核心端点**——它让播放器的媒体流量不经服务器。

不挂鉴权中间件：中转请求来自播放器（不是面板用户），鉴权靠 URL 里的
HMAC 签名。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.core.logging import get_logger
from app.services.redirect import RedirectError
from app.services.svc_factory import resolve_redirect

logger = get_logger(__name__)

router = APIRouter(tags=["redirect"])


@router.get("/302/{media_id}/{sig}")
async def redirect_302(media_id: int, sig: str, request: Request) -> Response:
    """把媒体条目重定向到网盘 CDN 直链。

    播放器跟随这个 302 后，**GB 级的媒体字节流直连网盘 CDN，不经过本服务器**。
    """
    ua = request.headers.get("user-agent", "")
    try:
        result = await resolve_redirect(media_id, sig, ua)
    except RedirectError as exc:
        logger.warning("中转失败 media_id=%s kind=%s: %s", media_id, exc.kind, exc)
        # 直接返回错误状态码比 302 到不可达地址更清晰。
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"kind": exc.kind, "message": str(exc)}},
        )

    headers: dict[str, str] = {}
    # 直链若要求额外请求头（115 的 f=3 模式要 Cookie），必须透传给播放器。
    headers.update(result.extra_headers)
    return RedirectResponse(result.url, status_code=302, headers=headers)
