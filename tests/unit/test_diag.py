"""直链参数诊断的契约测试。

``f`` 参数的实际取值未实测确认（``docs/design.md`` §11）——
本模块把三种可能的语义都固化下来，届时实测值落在哪档都要有对应行为。
"""

from __future__ import annotations

import pytest
from app.services.diag import diagnose_direct_link, parse_direct_link


class TestParseDirectLink:
    def test_flag_zero_needs_nothing(self) -> None:
        diag = parse_direct_link("https://cdn.115.com/f?t=9999999999&f=0")
        assert diag.flag == "0"
        assert diag.ua_required is False
        assert diag.cookie_required is False

    def test_flag_one_requires_ua(self) -> None:
        """f=1：请求直链时的 UA 必须与访问直链时一致。"""
        diag = parse_direct_link("https://cdn.115.com/f?t=9999999999&f=1")
        assert diag.flag == "1"
        assert diag.ua_required is True
        assert diag.cookie_required is False

    def test_flag_three_requires_ua_and_cookie(self) -> None:
        """f=3：还要额外携带 Set-Cookie。"""
        diag = parse_direct_link("https://cdn.115.com/f?t=9999999999&f=3")
        assert diag.flag == "3"
        assert diag.ua_required is True
        assert diag.cookie_required is True

    def test_missing_flag_defaults_to_no_requirement(self) -> None:
        diag = parse_direct_link("https://cdn.115.com/f")
        assert diag.flag is None
        assert diag.ua_required is False

    def test_expires_at_parsed(self) -> None:
        diag = parse_direct_link("https://cdn.115.com/f?t=2000000000&f=1")
        assert diag.expires_at == 2000000000

    def test_ttl_computed(self) -> None:
        """剩余有效期相对当前时刻。"""
        diag = parse_direct_link("https://cdn.115.com/f?t=2000000000", now=1999999000)
        assert diag.ttl_seconds == 1000

    def test_non_numeric_t_ignored(self) -> None:
        diag = parse_direct_link("https://cdn.115.com/f?t=abc")
        assert diag.expires_at is None
        assert diag.ttl_seconds is None

    def test_concurrent_and_user_parsed(self) -> None:
        diag = parse_direct_link("https://cdn.115.com/f?t=1&c=0&u=123&f=1")
        assert diag.concurrent == "0"
        assert diag.user_id == "123"

    def test_url_is_masked(self) -> None:
        """诊断输出脱敏——避免截图泄露可分享的直链。"""
        diag = parse_direct_link("https://cdn.115.com/f?t=2000000000&c=0&u=999&f=1")
        assert "2000000000" not in diag.url_masked
        assert "999" not in diag.url_masked
        assert "f=1" in diag.url_masked

    def test_masking_keeps_host(self) -> None:
        diag = parse_direct_link("https://cdn.115.com/abc?t=1&f=1")
        assert diag.url_masked.startswith("https://cdn.115.com/abc")


class TestDiagnose:
    async def test_uses_driver_get_direct_link(self) -> None:
        """诊断必须走驱动的换直链（保证 UA 一致）。"""
        calls: list[tuple[str, str]] = []

        class FakeDriver:
            async def get_direct_link(self, remote_id: str, user_agent: str):
                calls.append((remote_id, user_agent))
                from app.adapters.drivers.base import DirectLink

                return DirectLink(url="https://cdn.115.com/x?t=2000000000&f=1")

        diag = await diagnose_direct_link(FakeDriver(), "pc1", "TestUA/1.0")

        assert calls == [("pc1", "TestUA/1.0")]
        assert diag.flag == "1"

    async def test_driver_error_propagates(self) -> None:
        from app.adapters.drivers.base import DriverError

        class BadDriver:
            async def get_direct_link(self, remote_id: str, user_agent: str):
                raise DriverError("网盘挂了", kind="network")

        with pytest.raises(DriverError):
            await diagnose_direct_link(BadDriver(), "pc1", "UA")
