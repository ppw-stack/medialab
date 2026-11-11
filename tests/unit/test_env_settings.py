"""环境变量配置 service 单元测试。

锁住的核心契约：

1. :data:`ENV_SETTING_SPECS` 与 :class:`EnvSettingsUpdateIn`（API 层）
   字段一致——导入时 ``_assert_model_covers_specs`` 会校验，但单测也
   显式断言一次（双保险）
2. :func:`validate` 拒未注册 / 类型错 / 超长
3. :meth:`EnvSettingsManager.get` 内存命中 / 未命中回落到 settings
4. :meth:`EnvSettingsManager.apply` 不改内存（DB 先 / 内存后契约）
5. :func:`init_env_settings_async` 启动期回填：DB 有值用 DB，DB 空用 env
"""
from __future__ import annotations

import pytest
from app.core.config import set_settings
from app.services.env_settings import (
    ENV_SETTING_SPECS,
    EnvSettingsManager,
    apply_env_settings,
    init_env_settings_async,
    lookup_spec,
    set_env_settings,
    validate,
)

# ------------------------------------------------------------------ 元数据


def test_specs_registered() -> None:
    """cross_seed_scan_dirs 必须在注册表里。"""
    spec = lookup_spec("cross_seed_scan_dirs")
    assert spec is not None
    assert spec.key == "cross_seed_scan_dirs"
    assert spec.label
    assert spec.note
    assert spec.placeholder
    assert spec.max_length > 0


def test_api_model_covers_all_specs() -> None:
    """API 层的 EnvSettingsUpdateIn 必须覆盖全部 spec（导入时已校验）。

    显式断言一次——若有人在 ENV_SETTING_SPECS 加新 key 忘了加 API
    字段，导入时就抛 RuntimeError，本测试也会失败。
    """
    from app.api.routes.env_settings import EnvSettingsUpdateIn

    spec_keys = {s.key for s in ENV_SETTING_SPECS}
    model_keys = set(EnvSettingsUpdateIn.model_fields)
    assert spec_keys <= model_keys


# ------------------------------------------------------------------ validate


def test_validate_rejects_unknown_key() -> None:
    with pytest.raises(ValueError, match="未注册"):
        validate({"unknown_key": "x"})


def test_validate_rejects_non_string_value() -> None:
    with pytest.raises(ValueError, match="必须是字符串"):
        validate({"cross_seed_scan_dirs": 123})  # type: ignore[dict-item]


def test_validate_rejects_too_long_value() -> None:
    spec = lookup_spec("cross_seed_scan_dirs")
    assert spec is not None
    with pytest.raises(ValueError, match="长度超限"):
        validate({"cross_seed_scan_dirs": "x" * (spec.max_length + 1)})


def test_validate_accepts_empty_string() -> None:
    """空串合法（= 关闭孤儿扫描）。"""
    validate({"cross_seed_scan_dirs": ""})


def test_validate_accepts_multiple_dirs() -> None:
    validate({"cross_seed_scan_dirs": "/a,/b,/c"})


# ------------------------------------------------------------------ manager


def test_manager_get_hit_memory() -> None:
    mgr = EnvSettingsManager({"cross_seed_scan_dirs": "/foo"})
    assert mgr.get("cross_seed_scan_dirs") == "/foo"


def test_manager_get_miss_falls_back_to_settings() -> None:
    """内存未命中时回落到 settings 上同名字段。

    这是 service 层测试「monkeypatch settings」写法仍生效的根因。
    """
    from app.core.config import AppSettings
    from pydantic import SecretStr

    s = AppSettings(
        env="test",
        app_name="t",
        data_dir="/tmp",
        log_dir="/tmp",
        db_path="/tmp/x.db",
        web_username="t",
        web_password=SecretStr("test-pass-123"),
        web_token=SecretStr("test-token-32-chars-abcdefgh"),
        cross_seed_scan_dirs="/from-env",
    )
    set_settings(s)
    try:
        mgr = EnvSettingsManager()
        assert mgr.get("cross_seed_scan_dirs") == "/from-env"
    finally:
        set_settings(None)


def test_manager_get_unknown_key_raises() -> None:
    mgr = EnvSettingsManager()
    with pytest.raises(KeyError, match="未注册"):
        mgr.get("nope")


def test_manager_all_returns_copy() -> None:
    mgr = EnvSettingsManager({"cross_seed_scan_dirs": "/foo"})
    snapshot = mgr.all()
    snapshot["cross_seed_scan_dirs"] = "/tampered"
    # 改副本不影响 manager
    assert mgr.get("cross_seed_scan_dirs") == "/foo"


def test_manager_set_memory_does_not_touch_db() -> None:
    """set_memory 只改内存——DB 写入是 apply 的职责。"""
    mgr = EnvSettingsManager()
    mgr.set_memory("cross_seed_scan_dirs", "/x")
    assert mgr.get("cross_seed_scan_dirs") == "/x"


async def test_apply_does_not_change_memory_before_commit(db: None) -> None:
    """apply 完成后内存由调用方负责更新（apply_env_settings 会调 set_memory）。

    直接调 mgr.apply 不应改内存——这是「DB 先 / 内存后」契约的核心：
    commit 失败时内存未变，作业不会拿到一个重启后就消失的值。
    """
    from app.db.models import AppEnvSetting
    from app.db.session import session_scope
    from sqlalchemy import select

    mgr = EnvSettingsManager({"cross_seed_scan_dirs": "/old"})
    async with session_scope() as session:
        await mgr.apply(session, {"cross_seed_scan_dirs": "/new"})
        # 事务内已 stage 但未 commit，内存应未变
        assert mgr.get("cross_seed_scan_dirs") == "/old"
    # 事务提交后内存仍未变（apply 不动内存）
    assert mgr.get("cross_seed_scan_dirs") == "/old"
    # DB 已写入
    async with session_scope() as session:
        row = (
            await session.execute(
                select(AppEnvSetting).where(
                    AppEnvSetting.key == "cross_seed_scan_dirs"
                )
            )
        ).scalar_one()
        assert row.value == "/new"


# ------------------------------------------------------------------ 启动期


async def test_init_seeds_default_from_env_when_db_empty(db: None) -> None:
    """DB 空 → 用 env 来源值作为默认 + 写库。"""
    from app.core.config import AppSettings, set_settings
    from pydantic import SecretStr

    s = AppSettings(
        env="test",
        app_name="t",
        data_dir="/tmp",
        log_dir="/tmp",
        db_path="/tmp/x.db",
        web_username="t",
        web_password=SecretStr("test-pass-123"),
        web_token=SecretStr("test-token-32-chars-abcdefgh"),
        cross_seed_scan_dirs="/seed-from-env",
    )
    set_settings(s)
    try:
        mgr = await init_env_settings_async()
        assert mgr.get("cross_seed_scan_dirs") == "/seed-from-env"
    finally:
        set_settings(None)


async def test_init_uses_db_value_when_present(db: None) -> None:
    """DB 有该 key → 用 DB 值，忽略 env 来源。"""

    from app.db.models import AppEnvSetting
    from app.db.session import session_scope

    async with session_scope() as session:
        session.add(
            AppEnvSetting(key="cross_seed_scan_dirs", value="/from-db")
        )

    mgr = await init_env_settings_async()
    assert mgr.get("cross_seed_scan_dirs") == "/from-db"


async def test_init_log_never_leaks_secret(
    db: None, caplog: pytest.LogCaptureFixture
) -> None:
    """启动日志只记 key 与「是否已配置」，**不打配置值**。"""
    import logging

    from app.core.config import AppSettings, set_settings
    from pydantic import SecretStr

    s = AppSettings(
        env="test",
        app_name="t",
        data_dir="/tmp",
        log_dir="/tmp",
        db_path="/tmp/x.db",
        web_username="t",
        web_password=SecretStr("test-pass-123"),
        web_token=SecretStr("test-token-32-chars-abcdefgh"),
        cross_seed_scan_dirs="/super-secret-path",
    )
    set_settings(s)
    try:
        with caplog.at_level(logging.INFO, logger="app.services.env_settings"):
            await init_env_settings_async()
        assert "环境变量配置已初始化" in caplog.text
        assert "super-secret-path" not in caplog.text
    finally:
        set_settings(None)


# ------------------------------------------------------------------ apply_env_settings


async def test_apply_env_settings_updates_db_and_memory(db: None) -> None:
    """apply_env_settings：DB 先 / 内存后，事务提交成功后两者一致。"""
    from app.db.models import AppEnvSetting
    from app.db.session import session_scope
    from sqlalchemy import select

    set_env_settings(EnvSettingsManager({"cross_seed_scan_dirs": "/old"}))
    try:
        await apply_env_settings({"cross_seed_scan_dirs": "/new"})
        # 内存已更新
        from app.services.env_settings import get_env_settings

        assert get_env_settings().get("cross_seed_scan_dirs") == "/new"
        # DB 已更新
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(AppEnvSetting).where(
                        AppEnvSetting.key == "cross_seed_scan_dirs"
                    )
                )
            ).scalar_one()
            assert row.value == "/new"
    finally:
        set_env_settings(None)


async def test_apply_env_settings_rejects_invalid(db: None) -> None:
    """非法值抛 ValueError，不动 DB / 不动内存。"""
    from app.db.models import AppEnvSetting
    from app.db.session import session_scope
    from sqlalchemy import select

    set_env_settings(EnvSettingsManager({"cross_seed_scan_dirs": "/keep"}))
    try:
        with pytest.raises(ValueError):
            await apply_env_settings({"cross_seed_scan_dirs": 123})  # type: ignore[dict-item]
        from app.services.env_settings import get_env_settings

        assert get_env_settings().get("cross_seed_scan_dirs") == "/keep"
        async with session_scope() as session:
            rows = (
                await session.execute(select(AppEnvSetting))
            ).scalars().all()
            # 没有任何行被写入
            assert rows == []
    finally:
        set_env_settings(None)
