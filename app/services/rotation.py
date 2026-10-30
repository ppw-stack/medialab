"""签名密钥轮换。

**为什么需要**：中转 URL 的签名**没有有效期**（见
:mod:`app.services.signing`），换来了「STRM 文件永不过期、无需定期重写」。
代价是：URL 一旦泄露，长期可用直到轮换。

轮换流程：

1. 生成新密钥
2. 批量重算所有 ``media_files.signature``
3. **重写 STRM 文件**（文件名不变，只改内容里的签名）
4. 返回新的密钥（**只在此刻返回一次**）

第 3 步的关键：STRM 文件的**文件名与目录结构不变**，媒体服务器只需刷新
元数据（不需重扫整个库），旧签名立即失效。
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppSettings
from app.core.logging import get_logger
from app.db.models import MediaFile
from app.services.signing import build_redirect_url, sign
from app.services.strm_writer import write_strm

logger = get_logger(__name__)


@dataclass
class RotationResult:
    """轮换结果。"""

    #: 重签的条目数
    resigned: int = 0
    #: 重写文件的数
    rewritten: int = 0
    #: 重写失败的文件数（路径不可写等）
    failed: int = 0
    #: 新密钥（**只返回一次**，调用方须自行持久化）
    new_secret: str = ""

    def to_dict(self, include_secret: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "resigned": self.resigned,
            "rewritten": self.rewritten,
            "failed": self.failed,
        }
        if include_secret:
            payload["new_secret"] = self.new_secret
        return payload


def generate_secret() -> str:
    """生成新密钥（32 字节随机，hex 编码）。"""
    return secrets.token_hex(32)


async def rotate_secret(
    db: AsyncSession,
    settings: AppSettings,
    new_secret: str | None = None,
) -> RotationResult:
    """轮换签名密钥并重签所有条目。

    **原子性**：新密钥在重签的**同时**成为运行时密钥——否则会有一段
    「DB 里是��签名、运行时是旧密钥」的窗口，期间**所有中转 URL 全部 403**。
    轮换完成后服务立即可用；操作者只需把新密钥写进环境变量，
    下次重启仍用同一个。

    Args:
        db: DB session。
        settings: 应用配置（会被就地更新为使用新密钥）。
        new_secret: 新密钥；None 时自动生成。

    Returns:
        轮换结果（含新密钥，**只此一次**）。
    """
    secret = new_secret or generate_secret()
    base_url = settings.resolved_public_base_url
    if not base_url:
        from app.services.media import MediaServiceError

        raise MediaServiceError(
            "未配置 PUBLIC_BASE_URL，无法重写 STRM",
            status_code=500,
            kind="config",
        )

    result = RotationResult(new_secret=secret)
    now = int(time.time())

    rows = (await db.execute(select(MediaFile))).scalars().all()
    for media in rows:
        media.signature = sign(secret, media.id)
        media.updated_at = now
        result.resigned += 1

        if not media.strm_path:
            continue
        url = build_redirect_url(base_url, media.id, secret)
        try:
            write_strm(Path(media.strm_path), url)
            result.rewritten += 1
        except OSError as exc:
            logger.warning("重写 STRM 失败 %s：%s", media.strm_path, exc)
            result.failed += 1

    await db.commit()

    # 运行时密钥同步切换——与重签在同一事务边界外紧邻完成，
    # 保证「重签后的签名」与「运行时校验用的密钥」始终一致。
    settings.signing_secret = SecretStr(secret)

    logger.info(
        "密钥轮换完成：重签 %d 条，重写 %d 个文件，失败 %d",
        result.resigned,
        result.rewritten,
        result.failed,
    )
    return result


async def resign_single(
    db: AsyncSession, settings: AppSettings, media_id: int
) -> bool:
    """重签单条（用当前密钥）。"""
    secret = settings.signing_secret
    if secret is None:
        from app.services.media import MediaServiceError

        raise MediaServiceError(
            "未配置 SIGNING_SECRET", status_code=500, kind="config"
        )
    media = (
        await db.execute(select(MediaFile).where(MediaFile.id == media_id))
    ).scalar_one_or_none()
    if media is None:
        return False

    media.signature = sign(secret.get_secret_value(), media.id)
    media.updated_at = int(time.time())
    if media.strm_path:
        url = build_redirect_url(settings.resolved_public_base_url, media.id, secret.get_secret_value())
        try:
            write_strm(Path(media.strm_path), url)
        except OSError as exc:
            logger.warning("重写 STRM 失败 %s：%s", media.strm_path, exc)
    await db.commit()
    return True
