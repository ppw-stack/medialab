"""API 层全局异常处理。

服务层抛 :class:`app.services.errors.ServiceError`（带 HTTP 状态码），
这里统一转成 ``{"detail": "..."}`` 响应——与 FastAPI 默认
``HTTPException`` 的响应体一致，前端无需区分错误来源。

为什么不用全局 handler 而在路由里 ``try/except``：

- 服务层**不 import fastapi**（服务层不该知道 HTTP）
- 路由里到处写 ``try/except ... raise HTTPException`` 是纯样板，
  漏一处就会漏成 500（带堆栈，前端拿到的是 "Internal Server Error"
  而不是真实原因）
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.logging import get_logger
from app.services.errors import ServiceError

logger = get_logger(__name__)

__all__ = ["install_exception_handlers"]


def install_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器。"""

    @app.exception_handler(ServiceError)
    async def _handle_service_error(
        _request: Request, exc: ServiceError
    ) -> JSONResponse:
        """业务错误 → 对应 HTTP 状态码。

        状态码由服务层决定（400 / 404 / 409 / 501 / 502）。
        """
        logger.info("业务错误（%s）：%s", exc.status, exc)
        return JSONResponse(status_code=exc.status, content={"detail": str(exc)})
