"""中转 URL 签名的契约测试。"""

from __future__ import annotations

import pytest
from app.services.signing import build_redirect_url, sign, ua_hash, verify

SECRET = "s3cret"


class TestSign:
    def test_signature_is_deterministic(self) -> None:
        """同一输入必须产出同一签名（否则 STRM 文件内容会漂移）。"""
        assert sign(SECRET, 42) == sign(SECRET, 42)

    def test_signature_differs_per_media_id(self) -> None:
        assert sign(SECRET, 1) != sign(SECRET, 2)

    def test_signature_differs_per_secret(self) -> None:
        assert sign(SECRET, 1) != sign("other", 1)

    def test_signature_has_fixed_length(self) -> None:
        assert len(sign(SECRET, 1)) == 22

    def test_signature_is_url_safe(self) -> None:
        """签名进 URL 路径，不能含 ``/`` ``+`` ``=`` 等需转义的字符。"""
        for media_id in range(1, 50):
            sig = sign(SECRET, media_id)
            assert all(c.isalnum() or c in "-_" for c in sig), sig


class TestVerify:
    def test_valid_signature_passes(self) -> None:
        assert verify(SECRET, 42, sign(SECRET, 42))

    def test_tampered_media_id_fails(self) -> None:
        sig = sign(SECRET, 42)
        assert not verify(SECRET, 43, sig)

    def test_wrong_secret_fails(self) -> None:
        assert not verify("other", 42, sign(SECRET, 42))

    def test_empty_signature_fails(self) -> None:
        assert not verify(SECRET, 42, "")

    def test_malformed_signature_fails(self) -> None:
        assert not verify(SECRET, 42, "not-a-valid-signature!!")

    @pytest.mark.parametrize("bad", ["", "x" * 21, "x" * 23, "abc/def", "a b c"])
    def test_various_malformed_inputs_fail(self, bad: str) -> None:
        assert not verify(SECRET, 42, bad)


class TestUaHash:
    def test_same_ua_same_hash(self) -> None:
        assert ua_hash("Infuse/1.0") == ua_hash("Infuse/1.0")

    def test_different_ua_different_hash(self) -> None:
        assert ua_hash("Infuse/1.0") != ua_hash("Kodi/21.0")

    def test_empty_ua_is_its_own_bucket(self) -> None:
        """空 UA（Infuse 等不发 UA）参与分桶——与具体 UA 不是同一访问者。"""
        assert ua_hash("") != ua_hash("Infuse/1.0")

    def test_hash_is_stable_length(self) -> None:
        assert len(ua_hash("anything")) == 64


class TestBuildRedirectUrl:
    def test_url_shape(self) -> None:
        url = build_redirect_url("http://host:8080/", 42, SECRET)
        assert url == f"http://host:8080/302/42/{sign(SECRET, 42)}"

    def test_strips_trailing_slash(self) -> None:
        url = build_redirect_url("http://host:8080///", 42, SECRET)
        assert url.startswith("http://host:8080/302/")
        assert "//302" not in url[1:]
