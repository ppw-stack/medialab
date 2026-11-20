"""数据库迁移（幂等，每次启动跑一遍）。

从 :mod:`app.db.session` 拆出——原先 576 行的文件混了 4 种职责
（engine 管理 / 幂等 ALTER / datetime 迁移 / 回填），职责边界不清。

本模块只管**迁移**，不碰 engine 生命周期。

两类迁移：

1. **幂等 ALTER**：建表后的列增删改名（``create_all`` 不给已有表加列）
2. **数据回填**：从已有列推导新列（如 ``discount_pct``）

历史：原 ``migrate_datetime_to_bj_str``（把历史 DateTime(UTC) 列 +8h
转为北京时间字符串）已在「时间口径统一」时删除——新模型 DB 时间列
直接声明为 ``String(32)`` 北京时间字符串（见 :mod:`app.db.models`），
``create_all`` 建表即为最终态，无需运行时迁移。

注：**不引入 Alembic**。SQLAlchemy 2.0 async 需要额外适配
（``run_sync``），且现有迁移已跑通；表结构改动超过 10 次再评估。
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import CursorResult, text

from app.core.logging import get_logger
from app.db.models import TorrentIndex
from app.db.session import get_engine, get_session_factory
from app.domain.enums import FreeType
from app.domain.upload_types import DEFAULT_UPLOAD_CONCURRENCY

logger = get_logger(__name__)

__all__ = [
    "ADAPTER_VALUE_MIGRATIONS",
    "OBSOLETE_TABLES",
    "backfill_discount_pct",
    "backfill_upload_concurrency",
    "backup_and_drop_table",
    "delete_offline_env_settings",
    "drop_obsolete_tables_once",
    "migrate_adapter_values",
]

#: 「concurrency 占位值回填」完成后的 ``PRAGMA user_version`` 值。
#:
#: SQLite 官方的 schema 版本槽——**不占任何业务表**（曾考虑写
#: ``app_env_settings`` kv 表，被「校验失败时 env 表必须零行」的
#: 既有契约挡住；user_version 就是为一次性迁移而生的，且随 DB 文件
#: 持久、对应用完全透明）。迁移按版本号递增，只增不减（现已用到 3）。
_CONCURRENCY_BACKFILL_VERSION = 1

#: 「废弃表删除」完成后的 user_version。
#:
#: 用 **3** 而不是 2：合并前的辅种域旧库 ``user_version`` 已经是 2
#: （见 ``scripts/migrate_legacy_db.py``），若用 2 会把那批库判成「已迁移」
#: 而永远不删废弃表。版本号只增不减。
_OBSOLETE_TABLES_DROP_VERSION = 3

#: 启动删表的完整白名单——表名只能来自这里，绝不拼外部输入。
OBSOLETE_TABLES: tuple[str, ...] = ("scan_rules", "dav_acl", "quota_ledger")
#: 允许被备份后重建的表（旧结构的 organize_history，判重模型改过）
_BACKUP_DROP_WHITELIST: frozenset[str] = frozenset((*OBSOLETE_TABLES, "organize_history"))
_BACKUP_PREFIX = "medialab_bak_v3"


async def backup_and_drop_table(conn: object, name: str) -> bool:
    """在**同一事务**里先备份再删除一张废弃表；返回是否真的删了。

    删表不可逆，故：
    - ``name`` 必须在白名单里（防注入 / 防误删现役表）
    - 备份表名固定（``medialab_bak_v3_<name>``），已存在则不覆盖
    - 备份失败 → 抛异常 → 事务回滚 → 表不会被删

    ``conn`` 是 SQLAlchemy AsyncConnection（用 ``object`` 标注以避开
    mypy 对 engine.begin() 返回类型的严格检查）。
    """
    if name not in _BACKUP_DROP_WHITELIST:
        raise ValueError(f"拒绝删除未登记的废弃表：{name}")

    async def _exists(table: str) -> bool:
        row = (
            await conn.execute(  # type: ignore[attr-defined]
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name=:name"
                ),
                {"name": table},
            )
        ).first()
        return row is not None

    if not await _exists(name):
        return False
    backup_name = f"{_BACKUP_PREFIX}_{name}"
    if not await _exists(backup_name):
        await conn.execute(  # type: ignore[attr-defined]
            text(f"CREATE TABLE {backup_name} AS SELECT * FROM {name}")
        )
        logger.info("已备份废弃表 %s → %s", name, backup_name)
    await conn.execute(text(f"DROP TABLE {name}"))  # type: ignore[attr-defined]
    logger.info("已删除废弃表 %s", name)
    return True


async def drop_obsolete_tables_once() -> None:
    """一次性删除废弃表（``PRAGMA user_version`` 版本门槛 + 先备份）。

    旧实现在**每次启动**都执行 ``DROP TABLE``：一旦表名被新功能复用，
    或从备份恢复出同名业务数据，启动即永久删除。现在只在
    ``user_version < 3`` 时执行一次，之后即使有人重建了同名表也不碰。
    """
    engine = get_engine()
    async with engine.begin() as conn:
        version = (await conn.execute(text("PRAGMA user_version"))).scalar()
        if version is not None and int(version) >= _OBSOLETE_TABLES_DROP_VERSION:
            return
        for name in OBSOLETE_TABLES:
            await backup_and_drop_table(conn, name)
        await conn.execute(
            text(f"PRAGMA user_version = {_OBSOLETE_TABLES_DROP_VERSION}")
        )
        logger.info(
            "一次性删表迁移完成：user_version=%d", _OBSOLETE_TABLES_DROP_VERSION
        )


# ---------------------------------------------------------------- 值迁移

#: 站点 adapter 迁移：从旧值映射到新值。空 dict 表示无迁移。
ADAPTER_VALUE_MIGRATIONS: dict[str, str] = {
    # 旧版 10 个站点专用 adapter → 规则包驱动的通用 adapter（唯一实现）
    "mteam": "generic",
    "hhanclub": "generic",
    "hdsky": "generic",
    "ourbits": "generic",
    "audiences": "generic",
    "springsunday": "generic",
    "hdhome": "generic",
    "hddolby": "generic",
    "soulvoice": "generic",
    "hdwing": "generic",
}


async def migrate_adapter_values() -> None:
    """把 :data:`ADAPTER_VALUE_MIGRATIONS` 指定的旧值批量改写为新值（幂等）。

    启动时跑一次：若旧值已不存在则无副作用。
    """
    if not ADAPTER_VALUE_MIGRATIONS:
        return
    factory = get_session_factory()
    from sqlalchemy import update

    from app.db.models import Site

    async with factory() as session:
        for old_value, new_value in ADAPTER_VALUE_MIGRATIONS.items():
            stmt = (
                update(Site)
                .where(Site.adapter == old_value)
                .values(adapter=new_value)
            )
            result = cast(CursorResult[object], await session.execute(stmt))
            if result.rowcount:
                logger.info(
                    "站点 adapter 迁移：%s → %s，影响 %d 行",
                    old_value, new_value, result.rowcount,
                )
        await session.commit()


# ---------------------------------------------------------------- 回填

def _extract_discount_pct_from_payload(payload: object) -> int | None:
    """从已落库的 free_payload 反推 discount_pct（用于历史回填）。

    支持 mteam status.discount == "PERCENT_N"，以及 NexusPHP
    free_classes 含 halfdown / thirtypercent / 50 / 30。
    """
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    if isinstance(status, dict):
        disc = status.get("discount")
        if isinstance(disc, str):
            up = disc.upper()
            if up == "FREE":
                return 100
            if up.startswith("PERCENT_"):
                try:
                    return int(up.split("_", 1)[1])
                except (IndexError, ValueError):
                    return None
        if isinstance(disc, (int, float)):
            return abs(int(disc))
    free_classes = payload.get("free_classes")
    if isinstance(free_classes, list):
        for cls in free_classes:
            if not isinstance(cls, str):
                continue
            c = cls.lower()
            if c in ("halfdown", "twouphalfdown", "50"):
                return 50
            if c in ("thirtypercent", "30"):
                return 30
    return None


async def backfill_upload_concurrency() -> None:
    """把 upload_tasks.concurrency 的旧占位值 1 回填为新默认（**一次性**）。

    ``concurrency`` 列在双通道改造（2026-11-07）之前是「保留但不生效」
    的占位参数——存量行里的值**从未表达过用户意图**。本回填把占位行
    提到新默认 4（:data:`DEFAULT_UPLOAD_CONCURRENCY`），让存量任务
    重启后**立刻**享受小文件并发泄洪。

    **一次性靠 ``PRAGMA user_version``**（不是幂等重跑）：迁移系统是
    「每次启动跑一遍」，而「1 → 4」不天然幂等——用户回填后**显式**
    改回 1 是合法选择，不能每次启动又被改回 4。回填完成把 user_version
    提到 :data:`_CONCURRENCY_BACKFILL_VERSION`，不低于该版本即跳过。

    只动 ``concurrency == 1`` 的行——曾设过其它值（2-8）的行视为
    已有偏好，保留。
    """
    from sqlalchemy import text, update

    from app.db.models import UploadTask

    factory = get_session_factory()
    async with factory() as session:
        version = (await session.execute(text("PRAGMA user_version"))).scalar()
        if version is not None and int(version) >= _CONCURRENCY_BACKFILL_VERSION:
            return
        result = cast(
            CursorResult[object],
            await session.execute(
                update(UploadTask)
                .where(UploadTask.concurrency == 1)
                .values(concurrency=DEFAULT_UPLOAD_CONCURRENCY)
            ),
        )
        # PRAGMA 不能参数化——值是模块常量（int），无注入面
        await session.execute(
            text(f"PRAGMA user_version = {_CONCURRENCY_BACKFILL_VERSION}")
        )
        await session.commit()
        if result.rowcount:
            logger.info(
                "upload_tasks.concurrency 占位值回填：1 → %d，影响 %d 行"
                "（小文件通道并发，大文件通道仍恒串行）",
                DEFAULT_UPLOAD_CONCURRENCY,
                result.rowcount,
            )


async def backfill_discount_pct() -> None:
    """回填 torrent_index.discount_pct。

    仅更新 free_type ∈ {neutral_leech, freeleech_permanent} 的行；
    幂等：仅在 discount_pct IS NULL 时填，已填的不动。
    """
    from sqlalchemy import select, update

    factory = get_session_factory()
    async with factory() as session:
        stmt = select(
            TorrentIndex.id, TorrentIndex.free_type, TorrentIndex.free_payload
        ).where(
            TorrentIndex.free_type.in_(
                (FreeType.NEUTRAL, FreeType.PERMANENT)
            ),
            TorrentIndex.discount_pct.is_(None),
        )
        rows = (await session.execute(stmt)).all()
        if not rows:
            return
        updates: list[tuple[int, int]] = []
        for row_id, free_type, free_payload in rows:
            if free_type == FreeType.PERMANENT:
                pct: int | None = 100
            else:
                pct = _extract_discount_pct_from_payload(free_payload)
            if pct is not None:
                updates.append((row_id, pct))
        if not updates:
            return
        # 批量更新（按值分组）
        from collections import defaultdict

        by_pct: dict[int, list[int]] = defaultdict(list)
        for row_id, pct in updates:
            by_pct[pct].append(row_id)
        for pct, ids in by_pct.items():
            await session.execute(
                update(TorrentIndex)
                .where(TorrentIndex.id.in_(ids))
                .values(discount_pct=pct)
            )
        await session.commit()
        logger.info("discount_pct 历史回填：%d 行", len(updates))


# ---------------------------------------------------------------- 废弃配置清理

#: 「离线下载」功能废弃后需要从 ``app_env_settings`` 删掉的行。
#:
#: 该功能（本地文件 → 光鸭，走光鸭服务端拉取）已整体移除，其三项配置
#: 的 :data:`app.services.env_settings.ENV_SETTING_SPECS` 注册与
#: :class:`app.core.config.AppSettings` 字段一并删除 → 残留行**永远不会被
#: 读到**（加载只按已注册 spec 走），留着只是垃圾数据。
#:
#: 顺带清掉一个**死密钥**（``offline_hmac_secret``）——它曾是本地文件服务
#: 的 HMAC 密钥，功能没了就不该继续躺在 DB 里。
_OFFLINE_ENV_KEYS: tuple[str, ...] = (
    "offline_hmac_secret",
    "offline_public_base",
    "offline_files_root",
)


async def delete_offline_env_settings() -> None:
    """删掉「离线下载」遗留的三项环境变量配置行（幂等）。

    **天然幂等**——行不存在时删 0 行，无需 ``PRAGMA user_version`` 一次性
    标记（那个机制只给「不幂等的回填」用，如
    :func:`backfill_upload_concurrency`）。每次启动跑一遍：首次删掉存量
    行，之后恒为 0 行、静默通过。
    """
    from sqlalchemy import delete

    from app.db.models import AppEnvSetting

    factory = get_session_factory()
    async with factory() as session:
        result = cast(
            CursorResult[object],
            await session.execute(
                delete(AppEnvSetting).where(AppEnvSetting.key.in_(_OFFLINE_ENV_KEYS))
            ),
        )
        await session.commit()
        if result.rowcount:
            logger.info(
                "清理「离线下载」废弃配置：%d 行（%s）",
                result.rowcount,
                "、".join(_OFFLINE_ENV_KEYS),
            )

