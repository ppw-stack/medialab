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
    #: 补充说明（前端渲染在参数下方）。用于提示**不可逆**之类的关键
    #: 风险——取值语义放在 label 里放不下时用它补充。None = 无补充说明。
    note: str | None = None
    #: 枚举取值（值 → 显示名）。非 None 时前端渲染**单选按钮组**而非
    #: 数字输入框——0/1 这类参数是「在几个具名行为间二选一」，不是
    #: 「调一个数值」，用数字输入框反直觉（且看不出 0 和 1 各是什么）。
    #: None = 连续数值参数（分钟数 / 个数 / 毫秒等，用数字输入框）。
    options: tuple[tuple[int, str], ...] | None = None


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
        key="monitor_delete_expired",
        label="命中条件时删除",
        default=0,
        min=0,
        max=1,
        job="monitor_free_window",
        options=((0, "仅暂停"), (1, "删除任务+删数据")),
        note="选择删除会连带删掉磁盘数据（不可恢复）",
    ),
    ParamSpec(
        key="push_download_interval_ms",
        label="下载间隔（毫秒）",
        default=2000,
        min=0,
        max=30000,
        job="push_free_torrents",
    ),
    ParamSpec(
        key="push_within_bucket_order",
        label="同档内排序",
        default=0,
        min=0,
        max=1,
        job="push_free_torrents",
        options=((0, "按进度升序"), (1, "随机")),
        note="档位（未封顶/无信号/已封顶）始终是主排序，本项只决定同档内怎么排",
    ),
)


def specs_for_job(job_name: str) -> tuple[ParamSpec, ...]:
    """取某作业专属的参数 spec（按注册顺序）。

    无专属参数的作业返回空元组——前端据此隐藏参数区块。
    """
    return tuple(s for s in PARAM_SPECS if s.job == job_name)


def lookup_spec(key: str) -> ParamSpec | None:
    return next((s for s in PARAM_SPECS if s.key == key), None)


def _assert_enum_options_consistent() -> None:
    """校验枚举参数的 options 与 min/max 一致（**导入时执行**）。

    options 是前端渲染单选按钮组的唯一依据——写错会导致：
    - 选项值越界：用户选了 min/max 之外的值 → 后端 :func:`validate` 拒绝，
      但前端已经显示"已选中"，用户一脸茫然
    - 默认值不在选项里：radio 渲染不出选中项
    - 选项值重复：两个选项同值，用户选了哪个都说不清

    故在导入时炸掉，而不是等到用户点错才发现。
    """
    for spec in PARAM_SPECS:
        if spec.options is None:
            continue
        values = [v for v, _ in spec.options]
        if len(values) != len(set(values)):
            raise RuntimeError(f"{spec.key} 的 options 取值重复：{values}")
        for value in values:
            if not (spec.min <= value <= spec.max):
                raise RuntimeError(
                    f"{spec.key} 的 options 取值 {value} 超出范围 "
                    f"{spec.min}–{spec.max}"
                )
        if spec.default not in values:
            raise RuntimeError(
                f"{spec.key} 的默认值 {spec.default} 不在 options {values} 里"
            )


_assert_enum_options_consistent()


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
        # 枚举参数：值必须在 options 里（前端只能选到这些值，
        # 越界值说明是绕过 UI 的直接调用）
        if spec.options is not None:
            allowed = [v for v, _ in spec.options]
            if value not in allowed:
                raise ValueError(
                    f"{spec.label} 只能取 {allowed} 之一（当前 {value}）"
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
        "monitor_delete_expired": settings.monitor_delete_expired,
        "push_download_interval_ms": settings.push_download_interval_ms,
        "push_within_bucket_order": settings.push_within_bucket_order,
    }
    return mapping[key]


def _sanitize_loaded(spec: ParamSpec, value: int, source: str) -> int:
    """把加载到的值收敛到合法范围（启动期用）。

    启动加载路径（DB 值 / env 默认值）**不经过** :func:`validate`——
    那是 API 写入路径的校验。DB 里可能存着历史遗留、手工改库、或参数
    定义变更后失效的值（如枚举参数新增了取值、旧的 0/1 语义变了）。

    非法值不能直接进内存：
    - 枚举参数：值不在 options 里 → 内存里是"非法态"，前端 radio
      **无选中项**，用户看到两个都没选，不知道当前是什么状态
    - 连续参数：越界值 → 作业可能拿到意料外的阈值

    故一律**回退到 spec.default 并记 warning**——启动不能因为一个脏
    数据就崩，但要让问题可见。

    :param source: 值的来源（"DB" / "env"），用于日志定位。
    :return: 合法值（合法则原样返回，非法则返回 spec.default）。
    """
    try:
        validate({spec.key: value})
    except ValueError as exc:
        logger.warning(
            "运行时参数 %s 的%s 值 %d 非法（%s），已回退到默认值 %d",
            spec.key, source, value, exc, spec.default,
        )
        return spec.default
    return value


async def _load_and_seed_async(mgr: RuntimeParamManager, settings: AppSettings) -> None:
    """启动期：从 DB 读，未在 DB 则用 env 默认 + 写 DB。

    两处取值都会经 :func:`_sanitize_loaded` 收敛到合法范围——启动加载
    路径不经过 :func:`validate`（那是 API 写入路径的校验），脏数据不能
    直接进内存。
    """
    async with session_scope() as session:
        rows = (await session.execute(select(AppRuntimeParam))).scalars().all()
        existing = {row.key: row.value_int for row in rows}
    for spec in PARAM_SPECS:
        existing_val = existing.get(spec.key)
        if existing_val is not None:
            mgr._values[spec.key] = _sanitize_loaded(spec, existing_val, "DB")
        else:
            env_default = _sanitize_loaded(
                spec, _env_default(settings, spec.key), "env"
            )
            mgr._values[spec.key] = env_default
            async with session_scope() as session:
                session.add(AppRuntimeParam(key=spec.key, value_int=env_default))
            logger.info("运行时参数 %s 默认值 %d 已写入 DB", spec.key, env_default)
    logger.info("运行时参数已初始化：%s", mgr._values)


async def init_runtime_params_async(
    settings: AppSettings,
) -> RuntimeParamManager:
    """启动期初始化（异步）。lifespan 内 await 本函数。

    之前有同步壳 ``init_runtime_params`` 在已运行的 event loop 中开新线程
    跑 ``asyncio.run``，导致跨 loop 使用 async engine（aiosqlite 连接池
    绑定到创建时的 loop），会抛 InterfaceError 或静默错乱。
    """
    mgr = RuntimeParamManager()
    await _load_and_seed_async(mgr, settings)
    return mgr


def init_runtime_params(settings: AppSettings) -> RuntimeParamManager:
    """启动期初始化（同步壳，仅用于无 event loop 的同步上下文如测试）。

    已在 event loop 中时（lifespan 内）请改用
    :func:`init_runtime_params_async` 并 ``await``。
    """
    mgr = RuntimeParamManager()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_load_and_seed_async(mgr, settings))
        return mgr
    raise RuntimeError(
        "init_runtime_params 不能在已运行的 event loop 中调用——"
        "请改用 init_runtime_params_async 并 await"
    )


async def apply_runtime_params(updates: dict[str, int]) -> None:
    """校验并持久化一组运行时参数，**提交成功后**再更新内存。

    顺序不能反（DB 先、内存后）：内存先变而 DB 写失败，作业会拿到
    一个重启后就消失的值——用户以为改了参数，实际没存住。

    :raises ValueError: 参数未注册 / 类型不对 / 越界
    """
    validate(updates)
    mgr = get_runtime_params()
    async with session_scope() as session:
        await mgr.apply(session, updates)
    # 事务已提交成功，再更新内存
    for key, value in updates.items():
        mgr.set_memory(key, value)


#: 全局单例
_manager: RuntimeParamManager | None = None


def get_runtime_params() -> RuntimeParamManager:
    if _manager is None:
        raise RuntimeError("运行时参数未初始化（lifespan 未跑）")
    return _manager


def set_runtime_params(mgr: RuntimeParamManager) -> None:
    global _manager
    _manager = mgr
