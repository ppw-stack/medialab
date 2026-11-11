"""环境变量配置（DB 持久化的字符串型设置）。

与 :mod:`app.services.runtime_params` 的分工：

- :mod:`runtime_params` —— **整型**作业运行参数（推送上限 / 暂停分钟数 /
  枚举档位），归属作业，前端按作业的「配置」弹框分组编辑
- 本模块 —— **字符串型**进程级配置（辅种扫描目录 / 未来其它 env→DB 的
  字符串设置），不归属作业，前端在「设置 → 环境变量」tab 集中编辑

为何独立于 :class:`AppSettings`：

    :class:`AppSettings` 是启动期一次性加载的 pydantic 配置，**不可运行期
    修改**。本模块提供「运行期可变、DB 持久化」的薄层，业务代码
    （:mod:`app.services.cross_seed_orphan`、辅种删除端点）从
    :func:`get_env_setting` 取值，无需关心持久化细节。

启动期初始化（:func:`init_env_settings_async`）：

    - DB 有该 key 的行 → 用 DB 值
    - DB 无该 key 的行 → 用 env 来源（:func:`get_settings`）的当前值
      作为默认值，并写库（首次启动回填）

    这与 AGENTS.md「调度配置全部落库」契约一致：env 改不再生效（残留旧行
    被 ``extra="ignore"`` 静默忽略），但作为首次启动的默认值来源仍有意义
    （老库升级时把 env 当前值迁进 DB，行为不变）。

API 写入路径（:func:`apply_env_settings`）：

    校验 → DB 先 → 内存后。事务提交失败时内存未变，作业不会拿到一个
    重启后就消失的值——与 :func:`runtime_params.apply_runtime_params`
    同一处理。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import AppEnvSetting
from app.db.session import session_scope

logger = get_logger(__name__)


#: 单个字符串配置的元数据。
@dataclass(frozen=True, slots=True)
class EnvSettingSpec:
    """环境变量配置的元数据：键名 + label + 说明 + 占位文案。"""

    #: 参数名（与 ``AppSettings`` 字段名一致，便于启动期从 env 取默认值）
    key: str
    #: 前端表单 label
    label: str
    #: 补充说明（前端渲染在字段下方）
    note: str
    #: textarea 的 placeholder
    placeholder: str
    #: 值的最大长度（拒超长，防止异常输入塞爆 DB / 解析）
    max_length: int = 4096
    #: 是否从「设置 → 环境变量」通用列表中隐藏
    #:
    #: 隐藏的配置仍有自己的专用 UI（如离线下载的三项在「离线下载」页
    #: 的配置区，源根目录还带目录浏览器）——放到通用 textarea 列表里
    #: 既不好用（密钥明文、目录要手敲），又容易两边改出矛盾。
    #: 隐藏项**不走** ``PUT /env-settings``，由各自的专用端点写入。
    hidden: bool = False


#: 已注册的环境变量配置。
#:
#: 新增条目时同步：
#: 1. 在 :class:`AppSettings` 加同名字段（env 来源 / 首次启动默认值）
#: 2. 在 :class:`AppEnvSettingModel`（API 层）加同名字段（导入时校验一致）
#: 3. 业务读取点用 :func:`get_env_setting` 取值
ENV_SETTING_SPECS: tuple[EnvSettingSpec, ...] = (
    EnvSettingSpec(
        key="cross_seed_scan_dirs",
        label="辅种扫描目录",
        note=(
            "逗号分隔的多个目录，每个目录独立扫描 + 独立统计。"
            "路径必须与下载器里的 downloadDir 完全一致"
            "（如 /share/Public/home/downloads/辅种）。"
            "留空 = 不做孤儿扫描（该功能降级，前端显示「未启用」）。"
        ),
        placeholder="/share/Public/home/downloads/辅种",
        max_length=4096,
    ),
    # -- 离线下载（本地 → 光鸭）--
    EnvSettingSpec(
        key="offline_hmac_secret",
        label="离线下载密钥",
        note=(
            "访问本地文件的 HMAC 密钥——**每个文件一个 token**（HMAC(密钥, 相对路径)）。"
            "留空 = 不启用离线下载，文件服务不挂载、文件完全不可访问。"
            "改它会让所有已生成的下载链接失效（下次提交时重新生成）。"
        ),
        placeholder="openssl rand -hex 32 的输出",
        max_length=128,
        hidden=True,
    ),
    EnvSettingSpec(
        key="offline_public_base",
        label="离线下载公网入口",
        note=(
            "光鸭用它来拉本地文件——**必须从公网能访问到本服务**。"
            "如 http://home.banstack.top:8092（不带尾斜杠）。"
            "留空 = 不启用。"
        ),
        placeholder="http://home.banstack.top:8092",
        max_length=256,
        hidden=True,
    ),
    EnvSettingSpec(
        key="offline_files_root",
        label="离线下载源根目录",
        note=(
            "要暴露给光鸭的本地目录（容器内绝对路径）。"
            "源目录在此之下选择；**所有目录都能选**（面板本身已是管理员权限，"
            "与「数据同步」的监听目录一致）。留空 = 不启用。"
        ),
        placeholder="/mnt/remotes/192.168.0.172_Public/home/movietmp",
        max_length=1024,
        hidden=True,
    ),
    # -- 整理与刮削 --
    EnvSettingSpec(
        key="tmdb_api_key",
        label="TMDB API Key",
        note=(
            "刮削用的 TMDB 官方 API Key（v3 的 api_key 形式）。"
            "**留空 = 不启用刮削**（识别与图片都无从谈起）。"
            "密钥不会回显——保存时留空表示不修改。"
        ),
        placeholder="TMDB → Settings → API → API Key",
        max_length=128,
        hidden=True,
    ),
    EnvSettingSpec(
        key="media_library_dir",
        label="本地媒体库目录",
        note=(
            "整理产物的落点（容器内绝对路径）。整理把下载完成的文件"
            "按标准结构复制到这里，再由「数据同步」上传网盘、扫描生成 STRM。"
            "留空 = 不启用整理。"
        ),
        placeholder="/media/library",
        max_length=1024,
        hidden=True,
    ),
    EnvSettingSpec(
        key="organize_source_dirs",
        label="整理源目录",
        note=(
            "要整理的视频来源目录（容器内绝对路径，**逗号分隔多个**）。"
            "文件从哪来无所谓（BT 下载、手动拷贝都行）——放进源目录就会被"
            "扫描、识别、按标准结构整理到媒体库。"
            "建议与辅种下载目录**物理分开**：移动动作会删源文件，"
            "混用会弄死做种。"
        ),
        placeholder="/downloads/media,/data/incoming",
        max_length=4096,
        hidden=True,
    ),
)


def lookup_spec(key: str) -> EnvSettingSpec | None:
    return next((s for s in ENV_SETTING_SPECS if s.key == key), None)


def validate(updates: dict[str, str]) -> None:
    """校验一组字符串配置，失败抛 :class:`ValueError`。

    - key 必须已注册
    - 值必须是 str（拒绝 None / 数字 / bool——API 层的 pydantic 模型已拦，
      这里兜底）
    - 值长度不超过 spec.max_length（防异常输入塞爆 DB / 解析）
    - 不做路径穿越 / 存在性校验：那是 :mod:`app.services.orphan_deleter`
      三道安全闸的职责，这里重复校验会让「配一个还没挂载的目录」变成
      400（用户配好挂载再回来时会困惑）
    """
    for key, value in updates.items():
        spec = lookup_spec(key)
        if spec is None:
            raise ValueError(f"未注册的环境变量配置：{key}")
        if not isinstance(value, str):
            raise ValueError(
                f"{spec.label} 必须是字符串（当前类型 {type(value).__name__}）"
            )
        if len(value) > spec.max_length:
            raise ValueError(
                f"{spec.label} 长度超限（{len(value)} > {spec.max_length}）"
            )


class EnvSettingsManager:
    """进程内单例：DB 持久化的字符串型配置。

    初始化（:func:`init_env_settings_async`）时从 DB 加载，已存在则用 DB 值，
    不存在则用 env 来源的当前值 + 写库。运行期修改走 :meth:`apply`，
    同步 DB + 内存。
    """

    def __init__(self, values: dict[str, str] | None = None) -> None:
        """构造。测试可直接传初始 dict 注入（跳过 DB 加载）。"""
        self._values: dict[str, str] = dict(values or {})

    def get(self, key: str) -> str:
        """取配置值。key 必须是 :data:`ENV_SETTING_SPECS` 中已注册。

        未在内存中（manager 未初始化 / 未加载该 key）时回落到 spec 默认值
        来源——``get_settings()`` 上对应字段。这一回落保证「manager 没初始化
        的测试场景」不会炸（与 :meth:`runtime_params.RuntimeParamManager.get_int`
        回落到 spec.default 同一思路）。
        """
        if key in self._values:
            return self._values[key]
        spec = lookup_spec(key)
        if spec is None:
            raise KeyError(f"未注册的环境变量配置：{key}")
        # 回落到 env 来源（首次启动 / 测试未注入时）
        return getattr(get_settings(), spec.key, "")

    def all(self) -> dict[str, str]:
        return dict(self._values)

    def set_memory(self, key: str, value: str) -> None:
        """仅更新内存（不写 DB）。DB 写入由 :func:`apply` 完成。"""
        self._values[key] = value

    async def apply(self, session: AsyncSession, updates: dict[str, str]) -> None:
        """持久化到 DB（**不**改内存）。

        内存更新由调用方在事务提交成功后执行（:meth:`set`），避免 commit
        失败时内存已变、DB 未变的不一致。
        """
        validate(updates)
        for key, value in updates.items():
            row = await session.get(AppEnvSetting, key)
            if row is None:
                session.add(AppEnvSetting(key=key, value=value))
            else:
                row.value = value


async def _load_and_seed_async(mgr: EnvSettingsManager) -> None:
    """启动期：从 DB 读，未在 DB 则用 env 来源值 + 写库。

    两处取值都不经 :func:`validate`（那是 API 写入路径的校验）——DB 里
    可能有历史遗留、手工改库的值；env 来源值是 pydantic 校验过的字符串，
    天然合法。
    """
    async with session_scope() as session:
        rows = (await session.execute(select(AppEnvSetting))).scalars().all()
        existing = {row.key: row.value for row in rows}
    settings = get_settings()
    for spec in ENV_SETTING_SPECS:
        if spec.key in existing:
            mgr._values[spec.key] = existing[spec.key]
        else:
            default = getattr(settings, spec.key, "")
            mgr._values[spec.key] = default
            async with session_scope() as session:
                session.add(AppEnvSetting(key=spec.key, value=default))
            logger.info(
                "环境变量配置 %s 默认值已写入 DB（长度 %d）",
                spec.key, len(default),
            )
    # 只记 key 与「已配置/未配置」，不打值——里面有 HMAC 密钥这类敏感项
    logger.info(
        "环境变量配置已初始化：%s",
        {k: ("<set>" if v else "") for k, v in mgr._values.items()},
    )


async def init_env_settings_async() -> EnvSettingsManager:
    """启动期初始化（异步）。lifespan 内 await 本函数。"""
    mgr = EnvSettingsManager()
    await _load_and_seed_async(mgr)
    return mgr


def init_env_settings() -> EnvSettingsManager:
    """启动期初始化（同步壳，仅用于无 event loop 的同步上下文如测试）。"""
    mgr = EnvSettingsManager()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_load_and_seed_async(mgr))
        return mgr
    raise RuntimeError(
        "init_env_settings 不能在已运行的 event loop 中调用——"
        "请改用 init_env_settings_async 并 await"
    )


async def apply_env_settings(updates: dict[str, str]) -> None:
    """校验并持久化一组环境变量配置，**提交成功后**再更新内存。

    顺序不能反（DB 先、内存后）：内存先变而 DB 写失败，作业会拿到
    一个重启后就消失的值——用户以为改了，实际没存住。

    :raises ValueError: 配置未注册 / 类型不对 / 超长
    """
    validate(updates)
    mgr = get_env_settings()
    async with session_scope() as session:
        await mgr.apply(session, updates)
    for key, value in updates.items():
        mgr.set_memory(key, value)


#: 全局单例
_manager: EnvSettingsManager | None = None


def get_env_settings() -> EnvSettingsManager:
    """取全局单例。未初始化时抛 RuntimeError（lifespan 未跑）。"""
    if _manager is None:
        raise RuntimeError("环境变量配置未初始化（lifespan 未跑）")
    return _manager


def set_env_settings(mgr: EnvSettingsManager | None) -> None:
    """覆盖全局单例（测试 / 嵌入场景）。传 None 恢复未初始化态。"""
    global _manager
    _manager = mgr


def get_env_setting(key: str) -> str:
    """业务读取入口（便捷函数）。等价于 ``get_env_settings().get(key)``。"""
    return get_env_settings().get(key)
