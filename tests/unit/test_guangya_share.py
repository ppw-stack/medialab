"""光鸭**分享 / 转存**能力的契约测试（2026-11-12 生产实测固化）。

覆盖点：

- **匿名 vs 带 Bearer**：分享链路（取令牌 / 列内容）必须**匿名**——
  带 Bearer 一律返回 ``201 分享已失效``；保存（``restore_share``）必须
  **带 Bearer**——匿名返回 ``117 无效token``。这条是最容易踩的坑。
- ``shareId`` 必须拼账号 id 后缀（``{shareId}_{uid}``），缺了是
  ``112 参数错误``
- 成功响应**没有** ``code`` 字段（别用 ``code == 0`` 判断成功）
- 错误码映射：``201`` → ``rate_limit``（**可重试**：同账号连续建分享
  被限流）、``112`` → ``config``（**不可重试**）
- 分享内容的翻页、分享 / 保存 / 删除的入参形状
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
import respx
from app.adapters.drivers import guangya as gyp_module
from app.adapters.drivers.base import DriverError
from app.adapters.drivers.guangya import ShareInfo
from app.adapters.drivers.guangya_credential import GuangyaCredential

API = "https://api.guangyapan.com/nd.bizuserres.s/v1"


def _jwt_credential(sub: str = "uid-1") -> str:
    """造一个带 ``sub`` 的 JWT 凭据（账号 id 从这里取）。"""
    payload = (
        base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode())
        .decode()
        .rstrip("=")
    )
    cred = GuangyaCredential(
        access_token=f"header.{payload}.sig",
        refresh_token="ref",
        device_id="dev",
        expires_at=0,
    )
    return cred.to_json()


class FakeShareClient:
    """假的 guangyaclient 客户端（只实现分享那套接口）。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.share_create_result: dict[str, Any] = {
            "code": 0,
            "data": {
                "shareId": "1967000000000000001",
                "code": "",
                "shareUrl": "https://www.guangyapan.com/s/1967000000000000001_uid-1",
            },
        }
        self.restore_result: dict[str, Any] = {
            "code": 0,
            "data": {"taskId": "1967000000000000002"},
        }
        self.share_delete_result: dict[str, Any] = {"code": 0, "msg": "success"}

    def share_create(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("share_create", args, kwargs))
        return self.share_create_result

    def share_restore(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("share_restore", args, kwargs))
        return self.restore_result

    def share_delete(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("share_delete", args, kwargs))
        return self.share_delete_result

    def refresh_token(self) -> dict[str, Any]:
        return {"access_token": "new", "expires_in": 7200}


@pytest.fixture
def router():
    """嵌套的 respx router（内层优先匹配；未匹配的落到外层的强校验）。"""
    with respx.mock(assert_all_mocked=False) as r:
        yield r


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeShareClient:
    import guangyaclient

    client = FakeShareClient()
    monkeypatch.setattr(guangyaclient, "GuangyaClient", lambda *a, **k: client)
    return client


@pytest.fixture
def driver(fake_client: FakeShareClient) -> gyp_module.GuangyaDriver:
    return gyp_module.GuangyaDriver(_jwt_credential())


class TestCreateShare:
    async def test_uses_public_share_params(self, driver, fake_client) -> None:
        """官方网页端那套参数：公开分享 / 允许下载 / 永久 / 无提取码。"""
        info = await driver.create_share(["111", "222"], title="relay-x")

        assert isinstance(info, ShareInfo)
        assert info.share_id == "1967000000000000001"

        name, args, kwargs = fake_client.calls[0]
        assert name == "share_create"
        assert args[0] == ["111", "222"]
        assert kwargs["share_type"] == 0
        assert kwargs["download_type"] == 1
        assert kwargs["validate_duration"] == 0
        assert kwargs["code"] == ""
        assert kwargs["auto_fill_code"] is False

    async def test_missing_share_id_raises(self, driver, fake_client) -> None:
        fake_client.share_create_result = {"code": 0, "data": {}}
        with pytest.raises(DriverError):
            await driver.create_share(["111"], title="x")


class TestShareAccessToken:
    async def test_is_anonymous(self, driver, router) -> None:
        """⚠️ 分享链路必须**匿名**——带 Bearer 一律「分享已失效」。"""
        route = router.post(f"{API}/get_share_access_token").mock(
            return_value=httpx.Response(
                200, json={"msg": "success", "data": {"accessToken": "tok-1"}}
            )
        )

        token = await driver.share_access_token("1967000000000000001")

        assert token == "tok-1"
        request = route.calls[0].request
        assert "authorization" not in request.headers, "分享链路带了 Authorization"

    async def test_share_id_carries_account_suffix(self, driver, router) -> None:
        """``shareId`` 必须拼账号 id——缺了服务端报 112。"""
        route = router.post(f"{API}/get_share_access_token").mock(
            return_value=httpx.Response(
                200, json={"data": {"accessToken": "tok-1"}}
            )
        )

        await driver.share_access_token("1967000000000000001")

        body = json.loads(route.calls[0].request.content)
        assert body["shareId"] == "1967000000000000001_uid-1"

    async def test_success_response_has_no_code_field(self, driver, router) -> None:
        """成功响应**没有** ``code`` 字段——不能用 ``code == 0`` 判断。"""
        router.post(f"{API}/get_share_access_token").mock(
            return_value=httpx.Response(200, json={"msg": "success", "data": {"accessToken": "t"}})
        )
        assert await driver.share_access_token("1") == "t"

    async def test_expired_share_maps_to_rate_limit(self, driver, router) -> None:
        """``201 分享已失效`` → ``rate_limit``（可重试：等一会儿再建）。"""
        router.post(f"{API}/get_share_access_token").mock(
            return_value=httpx.Response(200, json={"code": 201, "msg": "分享已失效"})
        )
        with pytest.raises(DriverError) as exc:
            await driver.share_access_token("1")
        assert exc.value.kind == "rate_limit"

    async def test_param_error_maps_to_config(self, driver, router) -> None:
        """``112 参数错误`` → ``config``（**不可重试**）。"""
        router.post(f"{API}/get_share_access_token").mock(
            return_value=httpx.Response(200, json={"code": 112, "msg": "参数错误"})
        )
        with pytest.raises(DriverError) as exc:
            await driver.share_access_token("1")
        assert exc.value.kind == "config"


class TestShareFilesList:
    async def test_paginates(self, driver, router) -> None:
        """分享里可能几百个文件——必须翻页取全。"""
        pages = [
            {
                "data": {
                    "total": 150,
                    "list": [
                        {"fileId": str(i), "fileName": f"f{i}", "fileSize": 10}
                        for i in range(100)
                    ],
                }
            },
            {
                "data": {
                    "total": 150,
                    "list": [
                        {"fileId": str(i), "fileName": f"f{i}", "fileSize": 10}
                        for i in range(100, 150)
                    ],
                }
            },
        ]

        def _handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            idx = 0 if body["page"] == 1 else 1
            return httpx.Response(200, json=pages[idx])

        router.post(f"{API}/get_share_page_files_list").mock(side_effect=_handler)

        items = await driver.share_files_list("tok")

        assert len(items) == 150
        assert items[0]["fileName"] == "f0"
        assert items[-1]["fileName"] == "f149"

    async def test_is_anonymous(self, driver, router) -> None:
        route = router.post(f"{API}/get_share_page_files_list").mock(
            return_value=httpx.Response(200, json={"data": {"total": 0, "list": []}})
        )
        await driver.share_files_list("tok")
        assert "authorization" not in route.calls[0].request.headers


class TestRestoreShare:
    async def test_returns_task_id(self, driver, fake_client) -> None:
        task_id = await driver.restore_share("tok", ["111"], "dir-1")
        assert task_id == "1967000000000000002"

        name, args, _ = fake_client.calls[0]
        assert name == "share_restore"
        assert args[0] == "tok"
        assert args[1] == ["111"]
        assert args[2] == "dir-1"

    async def test_missing_task_id_raises(self, driver, fake_client) -> None:
        fake_client.restore_result = {"code": 0, "data": {}}
        with pytest.raises(DriverError):
            await driver.restore_share("tok", ["111"], "dir-1")

    async def test_missing_token_raises(self, driver, fake_client) -> None:
        """保存**必须带 Bearer**——匿名会「无效 token」。"""
        fake_client.restore_result = {"code": 117, "msg": "无效token"}
        with pytest.raises(DriverError) as exc:
            await driver.restore_share("tok", ["111"], "dir-1")
        assert exc.value.kind == "auth"


class TestDeleteShare:
    async def test_deletes(self, driver, fake_client) -> None:
        await driver.delete_share("1967000000000000001")
        name, args, _ = fake_client.calls[0]
        assert name == "share_delete"
        assert args[0] == ["1967000000000000001"]

    async def test_error_raises(self, driver, fake_client) -> None:
        fake_client.share_delete_result = {"code": 146, "msg": "文件不存在"}
        with pytest.raises(DriverError):
            await driver.delete_share("1")
