"""设备类型的契约测试。

**核心契约**：``device_type`` 必须真正传到扫码绑定那一步。

本项目踩过的坑：前端手写 4 个选项、后端裸 ``str`` 无校验、
``start_qr_session()`` 无参调用导致**恒为 web** —— 用户选什么都无效。
"""

from __future__ import annotations

import pytest
from app.domain.device_types import (
    ALL_DEVICE_TYPE_VALUES,
    ALL_DEVICE_TYPES,
    DEFAULT_DEVICE_TYPE,
    QR_CAPABLE_DEVICE_TYPE_VALUES,
    QR_CAPABLE_DEVICE_TYPES,
    describe_qr_incapable,
    get_device_type,
    is_qr_capable,
)


class TestDeviceTypeCatalog:
    def test_all_types_count(self) -> None:
        """完整清单 18 个（对齐 p115client 的 AVAILABLE_APPS）。"""
        assert len(ALL_DEVICE_TYPES) == 18

    def test_qr_capable_count(self) -> None:
        """能扫码的 10 个（实测）。"""
        assert len(QR_CAPABLE_DEVICE_TYPES) == 10

    def test_qr_capable_is_subset(self) -> None:
        assert QR_CAPABLE_DEVICE_TYPE_VALUES <= ALL_DEVICE_TYPE_VALUES

    def test_values_unique(self) -> None:
        values = [d.value for d in ALL_DEVICE_TYPES]
        assert len(values) == len(set(values))

    def test_every_type_has_label(self) -> None:
        for d in ALL_DEVICE_TYPES:
            assert d.label, f"{d.value} 缺显示名"

    def test_default_is_qr_capable(self) -> None:
        """默认值必须能扫码，否则新建的存储源开箱即不可用。"""
        assert DEFAULT_DEVICE_TYPE in QR_CAPABLE_DEVICE_TYPE_VALUES

    def test_default_is_android(self) -> None:
        """默认 115android——避开 web 的「IP登录异常」。"""
        assert DEFAULT_DEVICE_TYPE == "115android"

    @pytest.mark.parametrize(
        "value",
        [
            "115android",
            "115ios",
            "android",
            "harmony",
            "ios",
            "qandroid",
            "qios",
            "web",
            "wechatmini",
            "alipaymini",
        ],
    )
    def test_known_qr_capable(self, value: str) -> None:
        """实测能取到二维码的 10 个。"""
        assert is_qr_capable(value)

    @pytest.mark.parametrize(
        "value",
        [
            "115ipad",
            "ipad",
            "qipad",
            "tv",
            "apple_tv",
            "os_windows",
            "os_mac",
            "os_linux",
        ],
    )
    def test_known_qr_incapable(self, value: str) -> None:
        """实测取不到二维码的 8 个（返回 state=1 但无 qrcode 字段）。"""
        assert not is_qr_capable(value)

    def test_unknown_value_not_capable(self) -> None:
        assert not is_qr_capable("115_android")
        assert not is_qr_capable("")
        assert not is_qr_capable(None)


class TestDescribe:
    def test_describe_incapable_mentions_label(self) -> None:
        msg = describe_qr_incapable("os_windows")
        assert "Windows" in msg
        assert "不支持扫码" in msg

    def test_describe_unknown_value(self) -> None:
        msg = describe_qr_incapable("nonsense")
        assert "nonsense" in msg

    def test_get_device_type(self) -> None:
        d = get_device_type("115android")
        assert d is not None
        assert d.qr_capable is True

    def test_get_device_type_unknown(self) -> None:
        assert get_device_type("nope") is None
        assert get_device_type(None) is None


class TestQRSessionPassesDeviceType:
    """**最关键的一条**：device_type 真的要传进扫码绑定。"""

    async def test_device_type_reaches_session(self, monkeypatch) -> None:
        """启动扫码后，session.app 必须是用户选的 device_type。"""
        from app.adapters.drivers import p115_qr

        fake_payload = {"uid": "u1", "qrcode": "https://115.com/qr"}
        monkeypatch.setattr(p115_qr, "_qr_token", lambda: {"data": fake_payload})
        monkeypatch.setattr(p115_qr, "_render_qr_png", lambda url: b"\x89PNG\r\n\x1a\nfake")

        # 不真正起轮询任务（关掉协程避免 RuntimeWarning）
        def _drop(coro):
            coro.close()
            return None

        monkeypatch.setattr(p115_qr.asyncio, "create_task", _drop)

        result = await p115_qr.start_qr_session("115android")

        assert result.session_id
        assert result.qr_image_b64

        # 从内部会话表里取出 app
        session = p115_qr._SESSIONS[result.session_id]
        assert session.app == "115android", "device_type 没有传到扫码会话！"

        # 清理
        p115_qr._SESSIONS.pop(result.session_id, None)

    async def test_incapable_device_type_rejected_before_network(
        self, monkeypatch
    ) -> None:
        """不能扫码的类型 → **发起前**就报错，不打 115 API。"""
        from app.adapters.drivers import p115_qr

        called = {"token": False}

        def _token() -> dict:
            called["token"] = True
            return {"data": {"uid": "u", "qrcode": "x"}}

        monkeypatch.setattr(p115_qr, "_qr_token", _token)

        with pytest.raises(p115_qr.QRCapabilityError) as exc:
            await p115_qr.start_qr_session("os_windows")

        assert "不支持扫码" in str(exc.value)
        assert called["token"] is False, "不支持扫码的类型不该发起网络请求"

    async def test_default_is_used_when_omitted(self, monkeypatch) -> None:
        """不传参数时用默认值（115android，不是 web）。"""
        from app.adapters.drivers import p115_qr

        monkeypatch.setattr(
            p115_qr, "_qr_token", lambda: {"data": {"uid": "u", "qrcode": "x"}}
        )
        monkeypatch.setattr(p115_qr, "_render_qr_png", lambda url: b"png")

        def _drop(coro):
            coro.close()
            return None

        monkeypatch.setattr(p115_qr.asyncio, "create_task", _drop)

        result = await p115_qr.start_qr_session()
        session = p115_qr._SESSIONS[result.session_id]
        assert session.app == DEFAULT_DEVICE_TYPE
        p115_qr._SESSIONS.pop(result.session_id, None)
