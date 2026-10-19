"""应用运行时参数（DB 持久化的可调设置）。

- 启动期：DB 空 → 写入 env 默认；DB 有 → 用 DB 覆盖
- 运行期：API 修改 → 同时写 DB + 更新内存
- 业务侧：作业每次执行前从 manager 读取最新值（无缓存 / 即时一致）

为何独立于 :class:`AppSettings`：
    :class:`AppSettings` 是启动期一次性加载的 pydantic 配置；env 改完需重启。
    本模块提供"运行期可变、DB 持久化"的薄层，业务代码
    （:class:`PushFreeService`、:class:`MonitorFreeWindowService`）从
    :func:`get_runtime_params` 取值，无需关心持久化细节。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppSettings
from app.core.logging import get_logger
from app.db.models import AppRuntimeParam
from app.db.session import session_scope

logger = get_logger(__name__)


#: 已注册的参数（key → 默认值 / 校验范围）
@dataclass(frozen=True, slots=True)
class ParamSpec:
    """参数元数据：键名 + 默认值 + 合法范围 + 归属作业。"""

    key: str
    label: str
    default: int
    min: int
    max: int
    #: 归属作业名（None = 未归属/全局）。决定该参数出现在哪个作业的
    #: 「配置」弹框里（见 :func:`specs_for_job`）。
    job: str | None = None


PARAM_SPECS: tuple[ParamSpec, ...] = (
    ParamSpec(
        key="push_min_remaining_hours",
        label="限时种最少剩余（小时）",
        default=6,
        min=0,
        max=72,
        job="push_free_torrents",
    ),
    ParamSpec(
        key="push_max_per_run",
        label="单次推送上限（个）",
        default=20,
        min=1,
        max=200,
        job="push_free_torrents",
    ),
    ParamSpec(
        key="monitor_pre_pause_minutes",
        label="免费窗口提前暂停（分钟）",
        default=10,
        min=1,
        max=120,
        job="monitor_free_window",
    ),
    ParamSpec(
        key="push_download_interval_ms",
        label="下载间隔（毫秒）",
        default=2000,
        min=0,
        max=30000,
        job="push_free_torrents",
    ),
)


def specs_for_job(job_name: str) -> tuple[ParamSpec, ...]:
    """取某作业专属的参数 spec（按注册顺序）。

    无专属参数的作业返回空元组——前端据此隐藏参数区块。
    """
    return tuple(s for s in PARAM_SPECS if s.job == job_name)


def lookup_spec(key: str) -> ParamSpec | None:
    return next((s for s in PARAM_SPECS if s.key == key), None)


def validate(updates: dict[str, int]) -> None:
    """校验一组参数值，失败抛 :class:`ValueError`。"""
    for key, value in updates.items():
        spec = lookup_spec(key)
        if spec is None:
            raise ValueError(f"未注册的参数：{key}")
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{key} 必须是整数（当前类型 {type(value).__name__}）")
        if not (spec.min <= value <= spec.max):
            raise ValueError(
                f"{spec.label} 必须在 {spec.min}–{spec.max} 之间（当前 {value}）"
            )


class RuntimeParamManager:
    """进程内单例：DB 持久化的可调参数。

    初始化（:func:`init_runtime_params`）时从 DB 加载，已存在则用 DB 值，
    不存在则写入 env 默认值。运行期修改走 :meth:`apply`，同步 DB + 内存。
    """

    def __init__(self) -> None:
        self._values: dict[str, int] = {}

    def get_int(self, key: str) -> int:
        """取整型参数值。key 必须是 :data:`PARAM_SPECS` 中已注册。"""
        if key not in self._values:
            spec = lookup_spec(key)
            if spec is None:
                raise KeyError(f"未注册的运行时参数：{key}")
            return spec.default
        return self._values[key]

    def all(self) -> dict[str, int]:
        return dict(self._values)

    def set_memory(self, key: str, value: int) -> None:
        """仅更新内存（不写 DB）。DB 写入由 :func:`apply` 完成。"""
        self._values[key] = value

    async def apply(self, session: AsyncSession, updates: dict[str, int]) -> None:
        """持久化参数变更到 DB（**不**改内存）。

        内存更新由调用方在事务提交成功后执行（:meth:`set`），避免 commit
        失败时内存已变、DB 未变的不一致（作业会用未持久化的值）。
        """
        validate(updates)
        for key, value in updates.items():
            row = await session.get(AppRuntimeParam, key)
            if row is None:
                session.add(AppRuntimeParam(key=key, value_int=value))
            else:
                row.value_int = value


def _env_default(settings: AppSettings, key: str) -> int:
    mapping = {
        "push_min_remaining_hours": settings.push_min_remaining_hours,
        "push_max_per_run": settings.push_max_per_run,
        "monitor_pre_pause_minutes": settings.monitor_pre_pause_minutes,
        "push_download_interval_ms": settings.push_download_interval_ms,
    }
    return mapping[key]


async def _load_and_seed_async(mgr: RuntimeParamManager, settings: AppSettings) -> None:
    """启动期：从 DB 读，未在 DB 则用 env 默认 + 写 DB。"""
    async with session_scope() as session:
        rows = (await session.execute(select(AppRuntimeParam))).scalars().all()
        existing = {row.key: row.value_int for row in rows}
    for spec in PARAM_SPECS:
        existing_val = existing.get(spec.key)
        if existing_val is not None:
            mgr._values[spec.key] = existing_val
        else:
            env_default = _env_default(settings, spec.key)
            mgr._values[spec.key] = env_default
            async with session_scope() as session:
                session.add(AppRuntimeParam(key=spec.key, value_int=env_default))
            logger.info("运行时参数 %s 默认值 %d 已写入 DB", spec.key, env_default)
    logger.info("运行时参数已初始化：%s", mgr._values)


def init_runtime_params(settings: AppSettings) -> RuntimeParamManager:
    """启动期初始化（同步壳）。"""
    mgr = RuntimeParamManager()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 同步上下文：直接 asyncio.run
        asyncio.run(_load_and_seed_async(mgr, settings))
        return mgr
    # 已在 event loop 中（罕见）：在后台线程跑 init
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        ex.submit(lambda: asyncio.run(_load_and_seed_async(mgr, settings))).result()
    return mgr


#: 全局单例
_manager: RuntimeParamManager | None = None


def get_runtime_params() -> RuntimeParamManager:
    if _manager is None:
        raise RuntimeError("运行时参数未初始化（lifespan 未跑）")
    return _manager


def set_runtime_params(mgr: RuntimeParamManager) -> None:
    global _manager
    _manager = mgr
