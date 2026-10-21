"""服务层业务错误（→ API 层 HTTP 状态码）。

服务层抛 :class:`ServiceError`（携带 ``status``），API 层通过**全局异常
处理器**转成 ``HTTPException``。这样：

- 业务代码不必知道 HTTP（服务层不 import fastapi）
- 路由里不用到处写 ``try/except`` 翻译异常（减少样板）

错误响应体与 FastAPI 默认的 ``HTTPException`` 一致：``{"detail": "..."}``，
前端无需区分来源。

状态码约定：

- **400** —— 请求内容不合法（参数越界、状态冲突、凭据缺失等）
- **404** —— 目标不存在
- **409** —— 与现有资源冲突（重名、仍有引用）
- **501** —— 合法但尚未实现（如未支持的下载器类型）
- **502** —— 上游（下载器 / 站点）交互失败
"""

from __future__ import annotations

__all__ = ["ServiceError"]


class ServiceError(RuntimeError):
    """业务错误（服务层 → API 层的状态码契约）。

    :param message: 面向用户的错误描述（会原样进 ``detail``）。
    :param status: 建议的 HTTP 状态码（默认 400）。
    """

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status
