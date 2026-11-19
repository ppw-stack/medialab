"""光鸭凭据落库必须**合并**两层（Web 层 + 客户端覆盖层）。

为什么单独测：``store_credential`` 是**整串替换**（更新 ``drivers.credential``
这一列），登录流程若直接新建 ``GuangyaCredential`` 就会把另一层抹掉——
Web 扫码每 1~2 小时刷一次 token，顺手清掉客户端 token 的后果是
「非会员大文件直链静默回退 ``254``」（2026-11-19 记录过的坑）。

另一条契约：``client_mode`` 登录后**主 token 就是客户端 token**
（实测客户端 aud 对扫描 / 列目录 / 上传全部生效），Web 的
``refresh_token`` 清空——否则会走到 ``guangyaclient`` 的 Web 刷新，
把 aud 刷成 Web。
"""

from __future__ import annotations

import logging

import pytest
from app.adapters.drivers.guangya_credential import (
    GuangyaCredential,
    decode_credential,
)
from app.db.models import Driver
from app.db.session import session_scope
from app.services import driver_auth, driver_service

ENV = "CREDENTIAL_ENCRYPTION_SECRET"
SECRET = "test-secret"


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """凭据加密需要密钥（与生产同一环境变量）。"""
    monkeypatch.setenv(ENV, SECRET)


async def _add_driver() -> int:
    async with session_scope() as session:
        row = Driver(name="gy", type="guangya")
        session.add(row)
        await session.flush()
        return row.id


async def _read_credential(driver_id: int) -> GuangyaCredential | None:
    async with session_scope() as session:
        row = await driver_service.get_driver(session, driver_id)
        return decode_credential(row.credential)


async def _seed(
    driver_id: int, cred: GuangyaCredential
) -> None:
    async with session_scope() as session:
        await driver_service.store_credential(
            session, driver_id, cred.to_json(), scheme="gypcipher:v2:"
        )


def _login_payload(token: str = "tok", refresh: str = "ref") -> dict:
    return {
        "access_token": token,
        "refresh_token": refresh,
        "device_id": "dev-1",
        "expires_at": 9_999,
    }


async def test_web_login_keeps_client_layer(db: None) -> None:
    """Web 扫码登录**不得清掉**客户端覆盖层。"""
    driver_id = await _add_driver()
    await _seed(
        driver_id,
        GuangyaCredential(
            access_token="old-web",
            refresh_token="old-ref",
            client_access_token="client",
            client_refresh_token="client-ref",
            client_expires_at=1,
            client_device_id="client-dev",
        ),
    )

    await driver_auth._store_guangya_credential(
        driver_id, _login_payload(), client_mode=False
    )

    cred = await _read_credential(driver_id)
    assert cred is not None
    assert cred.access_token == "tok"
    assert cred.refresh_token == "ref"
    # 覆盖层原样保留 —— 这是本用例的全部意义
    assert cred.client_access_token == "client"
    assert cred.client_refresh_token == "client-ref"
    assert cred.client_device_id == "client-dev"


async def test_client_login_promotes_main_token(db: None) -> None:
    """client 模式登录：主 token 用客户端那把，Web refresh 清空。"""
    driver_id = await _add_driver()

    await driver_auth._store_guangya_credential(
        driver_id,
        _login_payload(token="client-tok", refresh="client-ref"),
        client_mode=True,
    )

    cred = await _read_credential(driver_id)
    assert cred is not None
    assert cred.access_token == "client-tok"
    assert cred.client_access_token == "client-tok"
    assert cred.client_refresh_token == "client-ref"
    assert cred.client_device_id == "dev-1"
    assert cred.device_id == "dev-1"
    # 主路径不能再走 Web 刷新（那是另一个 aud）
    assert cred.refresh_token == ""


async def test_client_login_keeps_web_layer(db: None) -> None:
    """反向同样成立：client 模式登录不得清掉已有的 Web 层。"""
    driver_id = await _add_driver()
    await _seed(
        driver_id,
        GuangyaCredential(
            access_token="web",
            refresh_token="web-ref",
            device_id="web-dev",
            expires_at=1,
        ),
    )

    await driver_auth._store_guangya_credential(
        driver_id, _login_payload(token="client-tok"), client_mode=True
    )

    cred = await _read_credential(driver_id)
    assert cred is not None
    # 主 token 被客户端那把覆盖（client 模式就该如此）
    assert cred.access_token == "client-tok"
    # 但 Web 层自己的 refresh_token 被清空是**预期**——
    # 主 token 已经不是 Web 身份了，留着反而会被 Web 刷新拿去换 aud
    assert cred.refresh_token == ""
    assert cred.client_refresh_token == "ref"


async def test_bad_credential_degrades_and_logs(
    db: None, caplog: pytest.LogCaptureFixture
) -> None:
    """旧密文 / 坏数据 → 降级成新凭据，**且必须留痕**。

    降级本身是对的（不能因为一条坏凭据让登录整个失败），但静默降级会
    把解密层的问题藏起来——客户端 token 覆盖层被无声清空时无从排查。
    """
    driver_id = await _add_driver()
    async with session_scope() as session:
        row = await driver_service.get_driver(session, driver_id)
        row.credential = "gypcipher:v2:这段不是合法密文"
        await session.flush()

    with caplog.at_level(logging.WARNING, logger="app.services.driver_auth"):
        await driver_auth._store_guangya_credential(
            driver_id, _login_payload(), client_mode=False
        )

    assert "光鸭凭据解密失败" in caplog.text
    assert str(driver_id) in caplog.text

    cred = await _read_credential(driver_id)
    assert cred is not None
    assert cred.access_token == "tok"


async def test_unexpected_error_bubbles_up(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """解密层的**编程缺陷**不得被当成「坏数据」吞掉。

    窄化捕获异常的意义就在这里：``AttributeError`` 之类的缺陷要冒泡，
    否则真实 bug 被掩盖，而凭据还会被无声替换成空对象。
    """

    def _boom(_credential: str | None) -> GuangyaCredential:
        raise AttributeError("解密层自身的缺陷")

    monkeypatch.setattr(
        driver_auth.guangya_credential, "decode_credential", _boom
    )

    driver_id = await _add_driver()
    with pytest.raises(AttributeError):
        await driver_auth._store_guangya_credential(
            driver_id, _login_payload(), client_mode=False
        )
