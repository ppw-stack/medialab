"""库健康检查。

三类问题：

- **断链** —— strm 文件内容指向的条目已不存在 / 签名失效
- **失效** —— strm 文件内容与当前签名不一致（如密钥轮换后没重写）
- **孤儿** —— 磁盘上的 strm 文件在 DB 里没有对应条目

**只读**：本模块**不**修改任何东西。修复动作走显式的批量接口
（见 :mod:`app.api.routes.media`），遵循「破坏性操作要可确认」的约定。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppSettings
from app.core.logging import get_logger
from app.db.models import Driver, MediaFile
from app.services.signing import sign

logger = get_logger(__name__)

#: 模块级别名——测试可替换为 spy 验证「扫盘确实离开事件循环」。
#: 不要直接 monkeypatch ``asyncio.to_thread``（那是全局模块，会污染整个进程）。
_to_thread = asyncio.to_thread


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
    #: 配置缺失导致签名校验无法进行（**不是**「签名失效」，别诱导批量重签）
    configuration_error: str | None = None

    @property
    def healthy(self) -> bool:
        return not (
            self.stale
            or self.missing_files
            or self.orphan_files
            or self.invalid_signature
            or self.configuration_error
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
            "configuration_error": self.configuration_error,
        }


class _MediaSnapshot(NamedTuple):
    """从 DB 读出的纯数据（不带 session，供线程里安全使用）。"""

    id: int
    strm_path: str | None
    signature: str | None


@dataclass(frozen=True)
class _LibrarySnapshot:
    """磁盘扫描所需的全部输入——事务关闭后只剩这些数据。"""

    checked_at: int
    media: tuple[_MediaSnapshot, ...]
    output_dirs: tuple[str, ...]
    secret: str  # 空串 = 未配置，跳过签名校验


async def _read_snapshot(db: AsyncSession, settings: AppSettings) -> _LibrarySnapshot:
    """短事务读 DB，立即转成纯数据——不在这里做磁盘 IO。"""
    media_rows = (await db.execute(select(MediaFile))).scalars().all()
    output_dirs = {
        str(Path(driver.output_dir))
        for driver in (await db.execute(select(Driver))).scalars().all()
        if driver.output_dir
    }
    secret = settings.signing_secret
    return _LibrarySnapshot(
        checked_at=int(time.time()),
        media=tuple(
            _MediaSnapshot(id=m.id, strm_path=m.strm_path, signature=m.signature)
            for m in media_rows
        ),
        output_dirs=tuple(output_dirs),
        secret=secret.get_secret_value() if secret else "",
    )


def _scan_snapshot(snapshot: _LibrarySnapshot) -> HealthReport:
    """纯同步磁盘扫描（在线程池里跑，CIFS 网络往返不卡事件循环）。"""
    report = HealthReport(checked_at=snapshot.checked_at)
    # 注意：**不能 strip 后拿去签名**——签发侧用的是原始密钥，
    # 这里 strip 会把带空白的合法密钥误判成签名失效。
    secret_value = snapshot.secret
    if not secret_value.strip():
        # 空密钥时用 "" 重算签名会把全库判成「签名无效」——那是错误的
        # 诊断结论，会诱导运维执行批量重签（改写全部 STRM）。
        report.configuration_error = (
            "SIGNING_SECRET 未配置，签名校验已跳过；"
            "请在 .env 配置后重启服务（不要据此批量重签）"
        )

    known_paths: set[str] = set()
    for media in snapshot.media:
        expected_sig = sign(secret_value, media.id) if secret_value else ""
        if secret_value and media.signature != expected_sig:
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
    for output_dir in snapshot.output_dirs:
        root = Path(output_dir)
        if not root.is_dir():
            continue
        for strm in root.rglob("*.strm"):
            if str(strm) not in known_paths:
                report.orphan_files.append(str(strm))

    logger.info(
        "健康检查完成：stale=%d 缺文件=%d 孤儿=%d 签名无效=%d 配置错误=%s",
        len(report.stale),
        len(report.missing_files),
        len(report.orphan_files),
        len(report.invalid_signature),
        report.configuration_error or "无",
    )
    return report


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
    """跑一次全库健康检查（**短事务读 DB → 线程池扫盘**）。

    API 层不碰 DB——它只调本函数。原先路由里直接 ``session_scope()``
    再调 :func:`check_library`，违反了「``api`` 层不依赖 ``db``」的分层
    约束（见 ``tests/unit/test_layering.py``）。

    ⚠️ **事务必须先在磁盘扫描前关闭**：媒体库常挂在 CIFS 上，递归
    ``rglob`` / 逐文件 ``stat`` 是网络往返，可能耗时数秒到数十秒。
    旧实现把这段同步 IO 放在 async 请求路径 + DB 事务里，会同时冻结
    事件循环并长时间占用 SQLite 写锁（同类事故见 ``app.main`` 的
    runtime_guard 注释）。
    """
    from app.core.config import get_settings
    from app.db.session import session_scope

    settings = get_settings()
    async with session_scope() as db:
        snapshot = await _read_snapshot(db, settings)
    # 事务已关闭；文件 IO 全部在线程池，事件循环保持可用。
    return await _to_thread(_scan_snapshot, snapshot)


async def check_library(
    db: AsyncSession, settings: AppSettings
) -> HealthReport:
    """检查整个媒体库（兼容入口：事务内读快照，再切线程扫盘）。

    校验三件事：

    1. strm 文件**存在**（DB 有条目 → 磁盘应有文件）
    2. strm 文件内容里的**签名有效**（用当前密钥重算能对上）
    3. 磁盘上的 strm 文件在 DB 里**有对应条目**（无孤儿）

    调用方若要避免长事务，请使用 :func:`library_health_report`（它先关
    事务再调 :func:`_scan_snapshot`）。本入口保留给已持有 session 的
    服务层 / 测试使用。
    """
    snapshot = await _read_snapshot(db, settings)
    return await _to_thread(_scan_snapshot, snapshot)
