"""302 中转服务。

流程::

    GET /302/<media_id>/<sig>
      → 校验 HMAC 签名（常量时间比较）
      → 查 media_files 条目 → driver_id + remote_id
      → 解析 UA（无 UA 客户端按 ua_policy 兜底）
      → 查本地直链缓存 (media_id, ua_hash)
      → miss: 单飞 → 调驱动换直链 → 写缓存
      → 302 Location: <网盘 CDN 直链>

**媒体字节流不经过本服务**——302 只是把播放器重定向到网盘 CDN。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.drivers.base import CloudDriver, DirectLink, DriverError
from app.core.config import AppSettings
from app.core.logging import get_logger
from app.db.models import Driver, MediaFile
from app.services.driver_factory import build_driver
from app.services.link_cache import LinkCache, parse_expires_at
from app.services.signing import ua_hash, verify

logger = get_logger(__name__)


async def _persist_refreshed_credential(driver_id: int, cred: Any) -> None:
    """把刷新后的凭据写回 DB。

    **为什么必须写回**：光鸭的 ``access_token`` 只有 1200~7200 秒有效期。
    驱动只在内存里更新它——不落库的话，进程重启后又用旧 token，
    服务端返回 117 / 401，刷新也救不回来（refresh_token 可能已失效）。

    写回**必须合并**「Web 层 + 客户端覆盖层」——整串替换会把另一层抹掉
    （2026-11-20 生产事故：driver 5 的客户端 token 被一个 Web 实例的
    刷新写回抹掉了）。实现见
    :func:`app.services.driver_service.persist_guangya_credential`。

    写回失败**不影响本次请求**（内存里的新 token 仍然有效），只记日志。

    **降级在本层显式做**（try/except + logger.exception），不依赖被调方
    的实现细节：写回失败是「凭据持久化」问题，**绝不能让 302 播放请求
    失败**（与「直链缓存读写失败必须降级，不能让播放 500」同一条铁律）。

    Raises:
        无。写回失败只记日志，不中断本次请求。
    """
    from app.services.driver_service import persist_guangya_credential

    try:
        await persist_guangya_credential(driver_id, cred)
    except Exception:
        logger.exception(
            "写回刷新的光鸭凭据失败（driver_id=%s，本次请求不受影响）", driver_id
        )


class RedirectError(Exception):
    """中转失败。

    Attributes:
        status_code: HTTP 状态码。
        kind: 错误分类（``not_found`` / ``forbidden`` / ``upstream`` / ``config``）。
    """

    def __init__(self, message: str, *, status_code: int = 502, kind: str = "upstream"):
        super().__init__(message)
        self.status_code = status_code
        self.kind = kind


@dataclass(frozen=True)
class RedirectResult:
    """中转结果。"""

    #: 重定向目标 URL（网盘 CDN 直链）
    url: str
    #: 直链是否绑定了 UA（True 时播放器必须用同一 UA 访问）
    ua_required: bool
    #: 访问直链必须携带的额外请求头（115 的 ``f=3`` 模式需要 Cookie）
    extra_headers: dict[str, str]


class RedirectService:
    """302 中转服务。

    驱动实例按 ``driver_id`` 缓存复用——每次请求新建驱动会让 115 客户端
    反复重建（连接池、cookie 状态全丢），且换直链的 API 调用频率暴涨。
    """

    def __init__(self, settings: AppSettings, cache: LinkCache) -> None:
        self._settings = settings
        self._cache = cache
        self._drivers: dict[int, CloudDriver] = {}

    # -- UA 解析 --

    def resolve_user_agent(self, request_ua: str) -> str:
        """按 ``ua_policy`` 解析实际用于换直链的 UA。

        - ``strict``           —— 原样使用请求的 UA（包括空）
        - ``fallback_default`` —— 请求无 UA 时用配置的默认 UA
        - ``relaxed``          —— 同 fallback，但直链不要求绑定 UA
        """
        ua = request_ua.strip()
        if ua:
            return ua
        if self._settings.ua_policy == "strict":
            return ""
        return self._settings.default_user_agent

    # -- 签名 --

    def verify_signature(self, media_id: int, sig: str) -> bool:
        """校验中转 URL 的签名。

        **必须在查库之前调用**——否则用不存在的 media_id 试探会得到 404
        （条目不存在）而非 403（签名不符），泄露条目是否存在。
        """
        secret = self._settings.signing_secret
        if secret is None:
            logger.error("未配置 SIGNING_SECRET，拒绝所有中转请求")
            return False
        return verify(secret.get_secret_value(), media_id, sig)

    # -- 驱动 --

    async def _get_driver(self, db: AsyncSession, driver_id: int) -> CloudDriver:
        """取驱动实例（按 driver_id 缓存复用）。"""
        cached = self._drivers.get(driver_id)
        if cached is not None:
            return cached

        row = (
            await db.execute(select(Driver).where(Driver.id == driver_id))
        ).scalar_one_or_none()
        if row is None:
            raise RedirectError(
                f"存储源不存在：{driver_id}", status_code=404, kind="not_found"
            )

        async def _on_refresh(cred: Any) -> None:
            """Token 刷新后写回 DB（光鸭的 token 只有 1~2 小时有效）。"""
            await _persist_refreshed_credential(driver_id, cred)

        try:
            driver = build_driver(
                row.type,
                row.credential,
                row.device_type,
                on_token_refresh=_on_refresh,
            )
        except DriverError as exc:
            raise RedirectError(
                f"驱动不可用：{exc}", status_code=503, kind="config"
            ) from exc
        self._drivers[driver_id] = driver
        return driver

    def reset_driver(self, driver_id: int | None = None) -> None:
        """清掉缓存的驱动实例（凭据变更 / 重新扫码登录后调用）。"""
        if driver_id is None:
            self._drivers.clear()
        else:
            self._drivers.pop(driver_id, None)

    # -- 主流程 --

    async def resolve(
        self,
        db: AsyncSession,
        media_id: int,
        sig: str,
        request_ua: str,
    ) -> RedirectResult:
        """解析中转请求，返回重定向目标。

        Raises:
            RedirectError: 签名不符 / 条目不存在 / 驱动调用失败。
        """
        # 先验签名再查库：不泄露条目是否存在。
        if not self.verify_signature(media_id, sig):
            raise RedirectError("签名无效", status_code=403, kind="forbidden")

        media = (
            await db.execute(select(MediaFile).where(MediaFile.id == media_id))
        ).scalar_one_or_none()
        if media is None:
            raise RedirectError(
                f"媒体条目不存在：{media_id}", status_code=404, kind="not_found"
            )

        driver = await self._get_driver(db, media.driver_id)
        ua = self.resolve_user_agent(request_ua)
        key = ua_hash(ua)

        async def _fetch() -> tuple[str, int]:
            try:
                link: DirectLink = await driver.get_direct_link(media.remote_id, ua)
            except DriverError as exc:
                # 账号需安全验证 → 403（不是 502：那不是上游故障，
                # 是账号状态问题，用户去 APP 验证一下就能恢复）
                if exc.kind == "auth_verify":
                    raise RedirectError(
                        str(exc), status_code=403, kind="auth_verify"
                    ) from exc
                raise RedirectError(
                    f"驱动调用失败：{exc}", status_code=502, kind="upstream"
                ) from exc
            expires_at = link.expires_at
            if expires_at is None:
                expires_at = parse_expires_at(link.url)
            return link.url, expires_at

        cached = await self._cache.get_or_fetch(media_id, key, _fetch)
        return RedirectResult(
            url=cached.url,
            ua_required=self._settings.ua_policy != "relaxed",
            extra_headers={},
        )
