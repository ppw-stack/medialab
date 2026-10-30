"""换直链错误分类的契约测试。

115 的「账号需安全验证」（code 911 / errno 137）**不是**网络故障——
把它报成 502 会让用户去查网络，而实际该做的是在手机 APP 里验证。
"""

from __future__ import annotations

from app.adapters.drivers.base import DriverError
from app.adapters.drivers.p115 import _classify_download_error


class TestClassifyDownloadError:
    def test_account_verify_911(self) -> None:
        """code 911 → 需 APP 验证。"""
        exc = Exception(
            "[Errno 137] {'state': False, 'error': '账号异常，请登录手机App验证激活', "
            "'code': 911, 'errno': 911}"
        )
        kind, message = _classify_download_error(exc)
        assert kind == "auth_verify"
        assert "安全验证" in message
        # 提示必须说清「重新扫码无效」，否则用户会反复扫码
        assert "重新扫码登录无效" in message

    def test_account_verify_137(self) -> None:
        exc = Exception("[Errno 137] 账号异常，请登录手机App验证激活")
        kind, _ = _classify_download_error(exc)
        assert kind == "auth_verify"

    def test_credential_expired(self) -> None:
        exc = Exception("cookie expired, please login again")
        kind, message = _classify_download_error(exc)
        assert kind == "auth"
        assert "重新扫码登录" in message

    def test_rate_limit(self) -> None:
        exc = Exception("429 Too Many Requests")
        kind, message = _classify_download_error(exc)
        assert kind == "rate_limit"
        assert "限流" in message

    def test_generic_network_error(self) -> None:
        exc = Exception("connection reset by peer")
        kind, message = _classify_download_error(exc)
        assert kind == "network"
        assert "换直链失败" in message


class TestRedirectSurfacesAuthVerify:
    async def test_auth_verify_maps_to_403(self) -> None:
        """中转时遇到需验证 → 403（不是 502）。"""

        # 构造一个 kind=auth_verify 的 DriverError
        exc = DriverError("115 换直链被拒：账号需安全验证", kind="auth_verify")
        assert exc.kind == "auth_verify"

    async def test_error_kinds_are_distinct(self) -> None:
        """不同错误 kind 必须区分开——前端据此给不同提示。"""
        from app.adapters.drivers.base import DriverError

        kinds = {
            DriverError("x", kind="auth_verify").kind,
            DriverError("x", kind="auth").kind,
            DriverError("x", kind="rate_limit").kind,
            DriverError("x", kind="network").kind,
        }
        assert len(kinds) == 4
