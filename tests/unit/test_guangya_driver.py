"""光鸭盘驱动的契约测试。

用假的 ``guangyaclient`` 客户端（monkeypatch 掉 ``GuangyaClient``），
不联网。

覆盖点：

- 列目录**翻页**（一次拉不完时要继续翻）
- 字段映射（fileId / fileName / fileSize / resType / utime）
- 递归遍历（流式产出、目录不产出）
- 换直链（``signedURL`` + ``urlDuration`` → ``expires_at``、**不绑 UA**）
- 错误码映射（117 → auth、146 → not_found …）
- Token 过期自动刷新 + 刷新回调（写回 DB 的钩子）
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

import pytest
from app.adapters.drivers import guangya as gyp_module
from app.adapters.drivers.base import DriverError
from app.adapters.drivers.guangya_credential import (
    GuangyaCredential,
    decode_credential,
    encode_credential,
)


class FakeGuangyaClient:
    """假的 guangyaclient 客户端。"""

    #: 由测试设置的接口实现（None = 用默认实现）
    fs_files_impl: Callable[..., dict[str, Any]] | None = None
    download_url_impl: Callable[..., dict[str, Any]] | None = None
    user_info_impl: Callable[..., dict[str, Any]] | None = None
    #: 光鸭**忽略** page 参数——默认实现就模拟这个行为
    cloud_task_list_impl: Callable[..., dict[str, Any]] | None = None

    def __init__(
        self,
        access_token: str | None = None,
        refresh_token: str | None = None,
        device_id: str | None = None,
    ) -> None:
        self.token = access_token or ""
        self.refresh_token_value = refresh_token
        self.device_id = device_id or "fake-device"
        self.refresh_calls = 0

    def fs_files(self, **kwargs: Any) -> dict[str, Any]:
        impl = type(self).fs_files_impl
        if impl is not None:
            return impl(self, **kwargs)
        return {"code": 0, "msg": "success", "data": {"total": 0, "list": []}}

    def download_url(self, file_id: str) -> dict[str, Any]:
        impl = type(self).download_url_impl
        if impl is not None:
            return impl(self, file_id)
        return {
            "code": 0,
            "data": {"signedURL": f"https://cdn.test/{file_id}", "urlDuration": 3600},
        }

    def user_info(self) -> dict[str, Any]:
        impl = type(self).user_info_impl
        if impl is not None:
            return impl(self)
        return {
            "code": 0,
            "data": {"userId": "u-1", "totalSpaceSize": 100, "usedSpaceSize": 40},
        }

    def refresh_token(self) -> dict[str, Any]:
        self.refresh_calls += 1
        self.token = f"new-token-{self.refresh_calls}"
        return {"access_token": self.token, "expires_in": 7200}

    def cloud_task_list(
        self, page: int = 0, page_size: int = 50, status: list[int] | None = None
    ) -> dict[str, Any]:
        """列离线任务。**默认模拟「服务端忽略 page」的真实行为**。"""
        impl = type(self).cloud_task_list_impl
        if impl is not None:
            return impl(self, page=page, page_size=page_size, status=status)
        return {"code": 0, "data": {"list": [], "total": 0}}

    def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def fake_gyp(monkeypatch: pytest.MonkeyPatch):
    """把 ``guangyaclient.GuangyaClient`` 换成假的，并复位各接口实现。"""
    import guangyaclient

    FakeGuangyaClient.fs_files_impl = None
    FakeGuangyaClient.download_url_impl = None
    FakeGuangyaClient.user_info_impl = None
    FakeGuangyaClient.cloud_task_list_impl = None
    monkeypatch.setattr(guangyaclient, "GuangyaClient", FakeGuangyaClient)
    yield
    FakeGuangyaClient.fs_files_impl = None
    FakeGuangyaClient.download_url_impl = None
    FakeGuangyaClient.user_info_impl = None
    FakeGuangyaClient.cloud_task_list_impl = None


def _credential_json(**kwargs: Any) -> str:
    cred = GuangyaCredential(
        access_token=kwargs.get("access_token", "tok"),
        refresh_token=kwargs.get("refresh_token", "ref"),
        device_id=kwargs.get("device_id", "dev"),
        expires_at=kwargs.get("expires_at", 0),
    )
    return cred.to_json()


class TestCredential:
    def test_roundtrip(self) -> None:
        cred = GuangyaCredential(
            access_token="a", refresh_token="b", device_id="c", expires_at=123
        )
        back = GuangyaCredential.from_json(cred.to_json())
        assert back == cred

    def test_encrypt_decrypt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_SECRET", "test-secret")
        cred = GuangyaCredential(
            access_token="tok", refresh_token="ref", device_id="dev", expires_at=9
        )
        encrypted = encode_credential(cred, "test-secret")
        assert encrypted.startswith("gypcipher:v2:")
        assert "tok" not in encrypted
        assert decode_credential(encrypted) == cred

    def test_plaintext_accepted(self) -> None:
        raw = _credential_json(access_token="plain")
        assert decode_credential(raw).access_token == "plain"

    def test_none_returns_none(self) -> None:
        assert decode_credential(None) is None


class TestListDir:
    async def test_maps_fields(self) -> None:
        driver = gyp_module.GuangyaDriver(_credential_json())

        def _fs_files(self: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "code": 0,
                "data": {
                    "total": 2,
                    "list": [
                        {
                            "fileId": "111",
                            "fileName": "movie.mkv",
                            "fileSize": 1024,
                            "resType": 1,
                            "utime": 1700000000,
                        },
                        {
                            "fileId": "222",
                            "fileName": "folder",
                            "fileSize": 0,
                            "resType": 2,
                            "utime": 1700000001,
                        },
                    ],
                },
            }

        FakeGuangyaClient.fs_files_impl = _fs_files
        files = await driver.list_dir_by_cid("root", parent_path="/影视")

        assert len(files) == 2
        movie = files[0]
        assert movie.remote_id == "111"
        assert movie.name == "movie.mkv"
        assert movie.size == 1024
        assert movie.is_dir is False
        assert movie.mtime == 1700000000
        assert movie.path == "/影视/movie.mkv"
        assert files[1].is_dir is True

    async def test_paginates(self) -> None:
        """一次拉不完时要继续翻页。"""
        driver = gyp_module.GuangyaDriver(_credential_json())
        calls: list[int] = []

        def _fs_files(self: Any, **kwargs: Any) -> dict[str, Any]:
            page = kwargs["page"]
            calls.append(page)
            if page == 0:
                items = [
                    {
                        "fileId": str(i),
                        "fileName": f"f{i}.mkv",
                        "fileSize": 10,
                        "resType": 1,
                    }
                    for i in range(gyp_module.PAGE_SIZE)
                ]
            else:
                items = [
                    {
                        "fileId": "last",
                        "fileName": "last.mkv",
                        "fileSize": 1,
                        "resType": 1,
                    }
                ]
            return {
                "code": 0,
                "data": {"total": gyp_module.PAGE_SIZE + 1, "list": items},
            }

        FakeGuangyaClient.fs_files_impl = _fs_files
        files = await driver.list_dir_by_cid("root")

        assert len(files) == gyp_module.PAGE_SIZE + 1
        assert calls == [0, 1]

    async def test_error_code_raises(self) -> None:
        driver = gyp_module.GuangyaDriver(_credential_json())

        def _fs_files(self: Any, **kwargs: Any) -> dict[str, Any]:
            return {"code": 117, "msg": "无效token"}

        FakeGuangyaClient.fs_files_impl = _fs_files
        with pytest.raises(DriverError) as exc:
            await driver.list_dir_by_cid("root")

        assert exc.value.kind == "auth"


class TestIterTree:
    async def test_recurses_and_streams(self) -> None:
        driver = gyp_module.GuangyaDriver(_credential_json())

        tree = {
            "": [
                {"fileId": "d1", "fileName": "电影", "resType": 2},
                {"fileId": "f0", "fileName": "top.mkv", "fileSize": 5, "resType": 1},
            ],
            "d1": [
                {"fileId": "f1", "fileName": "a.mkv", "fileSize": 6, "resType": 1},
            ],
        }

        def _fs_files(self: Any, **kwargs: Any) -> dict[str, Any]:
            items = tree.get(str(kwargs.get("parent_id") or ""), [])
            return {"code": 0, "data": {"total": len(items), "list": items}}

        FakeGuangyaClient.fs_files_impl = _fs_files
        names = []
        async for f in driver.iter_tree_by_cid("", parent_path="/影视"):
            names.append(f.name)

        assert sorted(names) == ["a.mkv", "top.mkv"]
        # 目录不产出
        assert "电影" not in names

    async def test_include_subdirs_false(self) -> None:
        driver = gyp_module.GuangyaDriver(_credential_json())

        def _fs_files(self: Any, **kwargs: Any) -> dict[str, Any]:
            items = [
                {"fileId": "d1", "fileName": "电影", "resType": 2},
                {"fileId": "f0", "fileName": "top.mkv", "fileSize": 5, "resType": 1},
            ]
            return {"code": 0, "data": {"total": len(items), "list": items}}

        FakeGuangyaClient.fs_files_impl = _fs_files
        names = []
        async for f in driver.iter_tree_by_cid("", include_subdirs=False):
            names.append(f.name)

        assert names == ["top.mkv"]


class TestDirectLink:
    async def test_returns_signed_url_and_expiry(self) -> None:
        driver = gyp_module.GuangyaDriver(_credential_json())
        link = await driver.get_direct_link("123", "Player/1.0")

        assert link.url == "https://cdn.test/123"
        assert link.expires_at is not None
        assert link.expires_at > 0
        # 光鸭直链**不绑 UA**——与 115 的 f=1 不同
        assert link.ua_required is False

    async def test_empty_url_raises(self) -> None:
        driver = gyp_module.GuangyaDriver(_credential_json())

        def _download_url(self: Any, file_id: str) -> dict[str, Any]:
            return {"code": 0, "data": {}}

        FakeGuangyaClient.download_url_impl = _download_url
        with pytest.raises(DriverError):
            await driver.get_direct_link("123", "Player/1.0")


class TestTokenRefresh:
    async def test_refreshes_when_expired(self) -> None:
        """Token 过期 → 自动刷新 + 回调（用于写回 DB）。"""
        refreshed: list[GuangyaCredential] = []

        async def _on_refresh(cred: GuangyaCredential) -> None:
            refreshed.append(cred)

        driver = gyp_module.GuangyaDriver(
            _credential_json(expires_at=1), on_token_refresh=_on_refresh
        )
        client = driver.client
        assert client.refresh_calls == 0

        await driver._ensure_token()

        assert client.refresh_calls == 1
        assert driver._cred is not None
        assert driver._cred.access_token == "new-token-1"
        assert len(refreshed) == 1
        assert refreshed[0].access_token == "new-token-1"

    async def test_no_refresh_when_valid(self) -> None:
        driver = gyp_module.GuangyaDriver(
            _credential_json(expires_at=9_999_999_999)
        )
        await driver._ensure_token()
        assert driver.client.refresh_calls == 0

    async def test_expired_without_refresh_token_raises(self) -> None:
        driver = gyp_module.GuangyaDriver(
            _credential_json(expires_at=1, refresh_token="")
        )
        with pytest.raises(DriverError) as exc:
            await driver._ensure_token()
        assert exc.value.kind == "auth"


class TestSpaceInfo:
    async def test_returns_capacity(self) -> None:
        """容量来自 ``get_assets``——``user_info()`` 那个端点已被平台禁用。"""
        driver = gyp_module.GuangyaDriver(_credential_json())
        with _mock_assets(_FAKE_ASSETS):
            info = await driver.get_space_info()
        assert info["total_bytes"] == 100
        assert info["used_bytes"] == 40
        assert info["remain_bytes"] == 60
        assert info["vip_status"] == 2

    async def test_account_id_from_jwt(self) -> None:
        """账号 id 从 access_token 的 JWT payload 里取 ``sub``。"""
        import base64
        import json as _json

        payload = _json.dumps({"sub": "user-42", "exp": 1}).encode()
        b64 = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        token = f"header.{b64}.signature"
        driver = gyp_module.GuangyaDriver(_credential_json(access_token=token))
        assert driver._account_id() == "user-42"

    async def test_account_id_empty_on_bad_token(self) -> None:
        driver = gyp_module.GuangyaDriver(_credential_json(access_token="not-a-jwt"))
        assert driver._account_id() == ""


#: 假的 get_assets 响应
_FAKE_ASSETS = {
    "code": 0,
    "data": {
        "totalSpaceSize": 100,
        "usedSpaceSize": 40,
        "vipStatus": 2,
        "vipLeftTime": 1000,
        "freeDirectLinkTraffic": 20,
    },
}


@contextmanager
def _mock_assets(payload: dict[str, Any]):
    """把 httpx 的 POST 换成返回固定 payload（测 ``get_assets``）。"""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    original = httpx.AsyncClient

    class _Patched(original):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.pop("transport", None)
            super().__init__(*args, transport=httpx.MockTransport(handler), **kwargs)

    httpx.AsyncClient = _Patched  # type: ignore[misc]
    try:
        yield
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]


class TestCodeMapping:
    def test_maps_known_codes(self) -> None:
        assert gyp_module._map_code(117) == "auth"
        assert gyp_module._map_code(111) == "auth"
        assert gyp_module._map_code(123) == "config"
        assert gyp_module._map_code(146) == "not_found"
        assert gyp_module._map_code(430) == "auth"
        assert gyp_module._map_code(999) == "unknown"
        assert gyp_module._map_code("bad") == "unknown"


class TestListOfflineTasks:
    """离线任务列表：**不能分页**（光鸭忽略 page 参数）。"""

    async def test_no_infinite_loop_when_count_exceeds_page_size(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """任务数 ≥ page_size 时必须**只请求一次**。

        实测光鸭 ``cloud_task_list`` 完全忽略 ``page`` 参数（page=0/1/2
        返回完全相同的列表）。若按 ``len(items) < PAGE_SIZE`` 判终止条件，
        任务数恰好 ≥ 100 时会无限循环——实测 1.5 秒打出 22837 次请求，
        把光鸭打到风控，并让迁移的轮询协程永远卡住。
        """
        calls = 0

        def _cloud_task_list(self: Any, **kwargs: Any) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            # 服务端忽略 page：每次都返回同一批
            items = [
                {"taskId": f"t{i}", "fileName": f"f{i}.mkv", "progress": 50}
                for i in range(150)
            ]
            return {"code": 0, "data": {"list": items, "total": 150}}

        monkeypatch.setattr(
            FakeGuangyaClient, "cloud_task_list_impl", _cloud_task_list, raising=False
        )
        driver = gyp_module.GuangyaDriver(_credential_json())

        tasks = await driver.list_active_offline_tasks()

        assert len(tasks) == 150
        assert calls == 1, f"分页导致重复请求（{calls} 次）"

    async def test_failed_tasks_listed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _cloud_task_list(self: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "code": 0,
                "data": {
                    "list": [
                        {
                            "taskId": "t1",
                            "fileName": "x.mkv",
                            "errMsg": "云添加失败",
                            "errCode": 8,
                        }
                    ],
                    "total": 1,
                },
            }

        monkeypatch.setattr(
            FakeGuangyaClient, "cloud_task_list_impl", _cloud_task_list, raising=False
        )
        driver = gyp_module.GuangyaDriver(_credential_json())

        tasks = await driver.list_failed_offline_tasks()

        assert len(tasks) == 1
        assert tasks[0]["errCode"] == 8
