"""启动配置的密钥强度闸门（P0: 防止 .env.example 原样上线）。"""

from __future__ import annotations

import pytest
from app.core.config import AppSettings
from pydantic import SecretStr, ValidationError


def test_placeholder_web_token_rejected() -> None:
    with pytest.raises(ValidationError, match="占位符"):
        AppSettings(web_token=SecretStr("change-me-long-random-string"))


def test_placeholder_web_password_rejected() -> None:
    with pytest.raises(ValidationError, match="占位符"):
        AppSettings(web_password=SecretStr("change-me-strong"))


def test_placeholder_signing_secret_rejected() -> None:
    with pytest.raises(ValidationError, match="占位符"):
        AppSettings(signing_secret=SecretStr("change-me"))


def test_short_prod_secrets_rejected() -> None:
    """默认 env=prod：短密钥必须挡住。"""
    with pytest.raises(ValidationError, match="至少 32 字符"):
        AppSettings(web_token=SecretStr("short"), signing_secret=SecretStr("short"))


def test_valid_prod_secrets_accepted() -> None:
    settings = AppSettings(
        web_token=SecretStr("a" * 64), signing_secret=SecretStr("b" * 64)
    )
    assert settings.web_token is not None
    assert settings.web_token.get_secret_value() == "a" * 64


def test_test_env_allows_short_secrets() -> None:
    """测试固件大量使用短密钥——test/dev 环境不强制长度。"""
    settings = AppSettings(env="test", web_token=SecretStr("t"), signing_secret=SecretStr("s"))
    assert settings.env == "test"
