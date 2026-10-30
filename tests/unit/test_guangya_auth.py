"""光鸭盘授权模块的测试（不联网）。

覆盖点：

- Device Code 扫码：取码 → 轮询 → 成功拿到凭据
- 轮询期间的 ``authorization_pending`` 不算失败
- 二维码是本地渲染的（内容 = ``verification_uri_complete``）
- 会话过期后仍能在保留期内读到结果（前端会多轮询一次）
"""

from __future__ import annotations

from typing import Any

import pytest
from app.adapters.drivers import guangya_auth as auth_module


@pytest.fixture(autouse=True)
def clear_sessions() -> None:
    auth_module.clear_all()
    yield
    auth_module.clear_all()


class TestStartSession:
    async def test_returns_qr_and_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []

        def _post(url: str, payload: dict, headers: dict) -> dict[str, Any]:
            calls.append(url)
            return (
                {
                    "device_code": "DC-123",
                    "user_code": "UC-456",
                    "expires_in": 120,
                    "interval": 2,
                    "verification_uri_complete": "https://account.test/verify?x=1",
                }
            )

        monkeypatch.setattr(auth_module, "_post", _post)

        result = await auth_module.start_qr_session(device_id="dev-1")

        assert result.session_id
        assert result.qr_image_b64
        assert result.user_code == "UC-456"
        assert calls == ["https://account.guangyapan.com/v1/auth/device/code"]

    async def test_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _post(url: str, payload: dict, headers: dict) -> dict[str, Any]:
            return ({"code": 429, "msg": "请求过于频繁"})

        monkeypatch.setattr(auth_module, "_post", _post)

        with pytest.raises(auth_module.GuangyaAuthError) as exc:
            await auth_module.start_qr_session()
        assert exc.value.kind == "rate_limit"

    async def test_incomplete_response_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _post(url: str, payload: dict, headers: dict) -> dict[str, Any]:
            return ({"device_code": "", "verification_uri_complete": ""})

        monkeypatch.setattr(auth_module, "_post", _post)

        with pytest.raises(auth_module.GuangyaAuthError):
            await auth_module.start_qr_session()


class TestPolling:
    async def test_pending_then_confirmed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """先 pending 再确认——最终拿到凭据。"""
        polls: list[int] = []

        def _post(url: str, payload: dict, headers: dict) -> dict[str, Any]:
            if url.endswith("/device/code"):
                return (
                    {
                        "device_code": "DC-1",
                        "user_code": "UC-1",
                        "verification_uri_complete": "https://account.test/v",
                    }
                )
            polls.append(1)
            if len(polls) < 2:
                return ({"error": "authorization_pending"})
            return (
                {
                    "access_token": "AT",
                    "refresh_token": "RT",
                    "expires_in": 7200,
                    "sub": "u-1",
                }
            )

        monkeypatch.setattr(auth_module, "_post", _post)
        # 缩短轮询间隔，测试跑得快
        monkeypatch.setattr(auth_module, "POLL_INTERVAL_SEC", 0.01)

        result = await auth_module.start_qr_session()
        session = await auth_module._get_session(result.session_id)
        assert session is not None
        assert session.task is not None
        await session.task

        status = await auth_module.get_qr_status(result.session_id)
        assert status is not None
        assert status.kind == "confirmed"
        assert status.credential is not None
        assert status.credential["access_token"] == "AT"
        assert status.credential["device_id"] == session.device_id
        assert status.credential["expires_at"] > 0

    async def test_result_survives_retirement(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """扫码成功后结果保留一段时间——前端会再轮询一次拿凭据。"""
        monkeypatch.setattr(auth_module, "_post", _post_confirmed)
        monkeypatch.setattr(auth_module, "POLL_INTERVAL_SEC", 0.01)

        result = await auth_module.start_qr_session()
        session = await auth_module._get_session(result.session_id)
        assert session is not None
        assert session.task is not None
        await session.task

        # 会话已从活跃表移出，但结果还在
        async with auth_module._LOCK:
            assert result.session_id not in auth_module._SESSIONS
            assert result.session_id in auth_module._FINISHED_SESSIONS

        status = await auth_module.get_qr_status(result.session_id)
        assert status is not None
        assert status.kind == "confirmed"

    async def test_unknown_session_returns_none(self) -> None:
        assert await auth_module.get_qr_status("nope") is None


def _post_confirmed(url: str, payload: dict, headers: dict) -> dict[str, Any]:
    """立即确认的假响应。"""
    if url.endswith("/device/code"):
        return (
            {
                "device_code": "DC-1",
                "user_code": "UC-1",
                "verification_uri_complete": "https://account.test/v",
            }
        )
    return (
        {
            "access_token": "AT",
            "refresh_token": "RT",
            "expires_in": 7200,
        }
    )


class TestErrorMapping:
    def test_maps_rate_limit(self) -> None:
        kind, msg = auth_module._map_error({"code": 429, "msg": "频繁"})
        assert kind == "rate_limit"
        assert "429" in msg

    def test_maps_captcha(self) -> None:
        kind, _ = auth_module._map_error({"msg": "需要人机验证 captcha"})
        assert kind == "captcha_required"

    def test_maps_unknown(self) -> None:
        kind, _ = auth_module._map_error({"code": 101, "msg": "内部错误"})
        assert kind == "unknown"
