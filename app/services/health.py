"""库健康检查。

三类问题：

- **断链** —— strm 文件内容指向的条目已不存在 / 签名失效
- **失效** —— strm 文件内容与当前签名不一致（如密钥轮换后没重写）
- **孤儿** —— 磁盘上的 strm 文件在 DB 里没有对应条目

**只读**：本模块**不**修改任何东西。修复动作走显式的批量接口
（见 :mod:`app.api.routes.media`），遵循「破坏性操作要可确认」的约定。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppSettings
from app.core.logging import get_logger
from app.db.models import Driver, MediaFile
from app.services.signing import sign

logger = get_logger(__name__)


@dataclass
class HealthReport:
    """健康检查报告。"""

    checked_at: int = 0
    #: strm 文件内容与期望不符（密钥轮换后未重写等）
    stale: list[int] = field(default_factory=list)
    #: DB 有条目但磁盘上的 strm 文件不存在
    missing_files: list[int] = field(default_factory=list)
    #: 磁盘上的 strm 文件在 DB 里没有对应条目
    orphan_files: list[str] = field(default_factory=list)
    #: 条目签名无效（用当前密钥算不出）
    invalid_signature: list[int] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not (
            self.stale
            or self.missing_files
            or self.orphan_files
            or self.invalid_signature
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "checked_at": self.checked_at,
            "healthy": self.healthy,
            "stale_count": len(self.stale),
            "missing_files_count": len(self.missing_files),
            "orphan_files_count": len(self.orphan_files),
            "invalid_signature_count": len(self.invalid_signature),
            "stale": self.stale[:100],
            "missing_files": self.missing_files[:100],
            "orphan_files": self.orphan_files[:100],
            "invalid_signature": self.invalid_signature[:100],
        }


async def db_available() -> bool:
    """DB 是否可用（能 SELECT 1 即认为可用）。

    API 层的探针端点用它——路由层不碰 ``app.db``（分层约束）。
    """
    from sqlalchemy import text

    from app.db.session import get_engine

    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        return False
    return True


async def library_health_report() -> HealthReport:
    """跑一次全库健康检查（**自开事务**）。

    API 层不碰 DB——它只调本函数。原先路由里直接 ``session_scope()``
    再调 :func:`check_library`，违反了「``api`` 层不依赖 ``db``」的分层
    约束（见 ``tests/unit/test_layering.py``）。
    """
    from app.core.config import get_settings
    from app.db.session import session_scope

    async with session_scope() as db:
        return await check_library(db, get_settings())


async def check_library(
    db: AsyncSession, settings: AppSettings
) -> HealthReport:
    """检查整个媒体库。

    校验三件事：

    1. strm 文件**存在**（DB 有条目 → 磁盘应有文件）
    2. strm 文件内容里的**签名有效**（用当前密钥重算能对上）
    3. 磁盘上的 strm 文件在 DB 里**有对应条目**（无孤儿）
    """
    report = HealthReport(checked_at=int(time.time()))
    secret = settings.signing_secret
    secret_value = secret.get_secret_value() if secret else ""

    media_rows = (await db.execute(select(MediaFile))).scalars().all()
    known_paths: set[str] = set()

    for media in media_rows:
        expected_sig = sign(secret_value, media.id)
        if media.signature != expected_sig:
            report.invalid_signature.append(media.id)

        if not media.strm_path:
            report.missing_files.append(media.id)
            continue

        path = Path(media.strm_path)
        known_paths.add(str(path))
        if not path.is_file():
            report.missing_files.append(media.id)
            continue

        try:
            content = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            report.missing_files.append(media.id)
            continue

        if secret_value and expected_sig not in content:
            report.stale.append(media.id)

    # 孤儿：磁盘上有、DB 里没有
    output_dirs = {
        str(Path(driver.output_dir))
        for driver in (await db.execute(select(Driver))).scalars().all()
        if driver.output_dir
    }
    for output_dir in output_dirs:
        root = Path(output_dir)
        if not root.is_dir():
            continue
        for strm in root.rglob("*.strm"):
            if str(strm) not in known_paths:
                report.orphan_files.append(str(strm))

    logger.info(
        "健康检查完成：stale=%d 缺文件=%d 孤儿=%d 签名无效=%d",
        len(report.stale),
        len(report.missing_files),
        len(report.orphan_files),
        len(report.invalid_signature),
    )
    return report
