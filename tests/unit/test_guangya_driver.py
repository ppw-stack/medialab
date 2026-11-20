"""光鸭盘驱动的契约测试。

用假的 ``guangyaclient`` 客户端（monkeypatch 掉 ``GuangyaClient``），
不联网。

覆盖点：

- 列目录**翻页**（一次拉不完时要继续翻）
- 字段映射（fileId / fileName / fileSize / resType / utime）
- **全量列表遍历**（2026-11-12 生产实测后重写：``parent_id="*"`` 一次
  目录翻页 + 一次文件翻页替代逐目录递归；子树过滤、``relative_dir``
  目录名映射、缺字段 / 缺映射的防御报错）
- 换直链（``signedURL`` + ``urlDuration`` → ``expires_at``、**不绑 UA**）
- 错误码映射（117 → auth、146 → not_found …）
- Token 过期自动刷新 + 刷新回调（写回 DB 的钩子）
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any, ClassVar

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
    #: 批量写调用的实参（记录下来断言「id 一定是字符串」）
    copy_calls: ClassVar[list[tuple[list[str], str]]] = []
    delete_calls: ClassVar[list[list[str]]] = []
    #: 注入业务错误码（模拟服务端拒绝）
    copy_code: int | None = None
    delete_code: int | None = None
    #: ``fs_create_dir`` 的实现覆盖（None = 用默认实现）
    fs_create_dir_impl: Callable[..., dict[str, Any]] | None = None
    #: **模拟 HTTP 401**（token 被服务端判死）：前 N 次业务调用抛 401
    unauthorized_times: ClassVar[int] = 0

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
        #: 每次业务调用携带的 token（断言「续期后业务请求用的是新 token」）
        self.seen_tokens: list[str] = []

    def fs_files(self, **kwargs: Any) -> dict[str, Any]:
        self.seen_tokens.append(self.token)
        impl = type(self).fs_files_impl
        if impl is not None:
            return impl(self, **kwargs)
        return {"code": 0, "msg": "success", "data": {"total": 0, "list": []}}

    def fs_create_dir(
        self,
        name: str,
        parent_id: str | None = None,
        fail_if_name_exist: bool = False,
    ) -> dict[str, Any]:
        """建目录（可注入 401——生产事故就挂在这里）。

        Raises:
            Exception: 注入的 401 未耗尽时抛 ``httpx.HTTPStatusError``。
        """
        self.seen_tokens.append(self.token)
        if type(self).unauthorized_times > 0:
            type(self).unauthorized_times -= 1
            raise _http_401(
                "https://api.guangyapan.com/nd.bizuserres.s/v1/file/create_dir"
            )
        impl = type(self).fs_create_dir_impl
        if impl is not None:
            return impl(self, name, parent_id=parent_id)
        return {"code": 0, "data": {"fileId": "999"}}

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

    def fs_copy(
        self, file_ids: list[str], parent_id: str | None = None
    ) -> dict[str, Any]:
        """复制（记录实参——用来断言 id 是字符串）。"""
        type(self).copy_calls.append((list(file_ids), str(parent_id)))
        code = type(self).copy_code
        if code is not None:
            return {"code": code, "msg": "复制失败"}
        return {"code": 0, "msg": "success", "data": {"taskId": "t-copy"}}

    def fs_delete(self, file_ids: list[str]) -> dict[str, Any]:
        """删除（记录实参——用来断言 id 是字符串）。"""
        type(self).delete_calls.append(list(file_ids))
        code = type(self).delete_code
        if code is not None:
            return {"code": code, "msg": "删除失败"}
        return {"code": 0, "msg": "success", "data": {"taskId": "t-del"}}

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
    FakeGuangyaClient.fs_create_dir_impl = None
    FakeGuangyaClient.copy_calls = []
    FakeGuangyaClient.delete_calls = []
    FakeGuangyaClient.copy_code = None
    FakeGuangyaClient.delete_code = None
    FakeGuangyaClient.unauthorized_times = 0
    monkeypatch.setattr(guangyaclient, "GuangyaClient", FakeGuangyaClient)
    yield
    FakeGuangyaClient.fs_files_impl = None
    FakeGuangyaClient.download_url_impl = None
    FakeGuangyaClient.user_info_impl = None
    FakeGuangyaClient.cloud_task_list_impl = None
    FakeGuangyaClient.fs_create_dir_impl = None
    FakeGuangyaClient.copy_calls = []
    FakeGuangyaClient.delete_calls = []
    FakeGuangyaClient.copy_code = None
    FakeGuangyaClient.delete_code = None
    FakeGuangyaClient.unauthorized_times = 0


def _http_401(url: str) -> Exception:
    """造一个「HTTP 401」异常（``guangyaclient`` 用 ``raise_for_status`` 抛的）。"""
    import httpx

    request = httpx.Request("POST", url)
    response = httpx.Response(401, request=request)
    return httpx.HTTPStatusError(
        f"Client error '401 Unauthorized' for url '{url}'",
        request=request,
        response=response,
    )


def _credential_json(**kwargs: Any) -> str:
    cred = GuangyaCredential(
        access_token=kwargs.get("access_token", "tok"),
        refresh_token=kwargs.get("refresh_token", "ref"),
        device_id=kwargs.get("device_id", "dev"),
        expires_at=kwargs.get("expires_at", 0),
        client_access_token=kwargs.get("client_access_token", ""),
        client_expires_at=kwargs.get("client_expires_at", 0),
        client_device_id=kwargs.get("client_device_id", ""),
        client_refresh_token=kwargs.get("client_refresh_token", ""),
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
        # ⚠️ **不能断言「明文子串」不在密文里**：base64 是 3 字节对齐的编码，
        # 密文里随机出现 "tok" 这类 3 字符子串的概率约 1.5e-4
        # （2026-11-14 CI 就是这么随机挂的）。可靠判据是「完整明文不在密文里」。
        assert cred.to_json() not in encrypted
        assert decode_credential(encrypted) == cred

    def test_encrypt_is_randomized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """同一明文两次加密结果不同（nonce 随机）——密文不可比对。"""
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_SECRET", "test-secret")
        cred = GuangyaCredential(
            access_token="tok", refresh_token="ref", device_id="dev", expires_at=9
        )
        first = encode_credential(cred, "test-secret")
        second = encode_credential(cred, "test-secret")
        assert first != second
        assert decode_credential(first) == decode_credential(second) == cred

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
    """全量列表模式（2026-11-12 生产探针实测后重写）。

    ``fs_files`` 支持 ``parent_id="*"``（全账号）+ ``res_type`` 过滤，
    条目带 ``fullParentIds``（从账号根到父目录的完整 fileId 链）——
    约 16 个只读请求替代旧逐目录递归的成百上千次。
    """

    #: 媒体库根 fileId（探针实测的生产值形状：19 位数字）
    ROOT = "1962124234139361322"

    @staticmethod
    def _dir(file_id: str, name: str, parent_ids: str) -> dict[str, Any]:
        return {
            "fileId": file_id,
            "fileName": name,
            "fileSize": 0,
            "resType": 2,
            "fullParentIds": parent_ids,
            "utime": 111,
        }

    @staticmethod
    def _file(
        file_id: str,
        name: str,
        parent_ids: str,
        *,
        size: int = 1024,
        utime: int = 1700000000,
        res_type: int = 1,
    ) -> dict[str, Any]:
        return {
            "fileId": file_id,
            "fileName": name,
            "fileSize": size,
            "resType": res_type,
            "fullParentIds": parent_ids,
            "utime": utime,
            "ctime": utime,
        }

    @staticmethod
    def _paged_fs_files(
        dirs: list[dict[str, Any]], files: list[dict[str, Any]]
    ) -> Callable[..., dict[str, Any]]:
        """按 res_type 分开、按 page 切页的 fs_files 假实现。"""

        def _impl(_self: Any, **kwargs: Any) -> dict[str, Any]:
            items = dirs if kwargs.get("res_type") == 2 else files
            page = int(kwargs.get("page") or 0)
            size = int(kwargs.get("page_size") or gyp_module.LIST_PAGE_SIZE)
            chunk = items[page * size : (page + 1) * size]
            return {
                "code": 0,
                "msg": "success",
                "data": {"list": chunk, "total": len(items)},
            }

        return _impl

    async def _collect(self, driver: Any, cid: str, **kwargs: Any) -> list[Any]:
        return [f async for f in driver.iter_tree_by_cid(cid, **kwargs)]

    async def test_subtree_filter_and_relative_dir_mapping(self) -> None:
        """子树外条目跳过；链上 fileId 用目录名映射成 relative_dir。"""
        driver = gyp_module.GuangyaDriver(_credential_json())
        root = self.ROOT
        dirs = [
            self._dir("D1", "电影", f"{root}/"),
            self._dir("D2", "剧集", f"{root}/D1/"),
            self._dir("DX", "其它内容", "OTHER/"),
        ]
        files = [
            self._file("F1", "顶层.mkv", f"{root}/"),
            self._file("F2", "电影A.mkv", f"{root}/D1/"),
            self._file("F3", "剧集B.mkv", f"{root}/D1/D2/"),
            self._file("F4", "子树外.mkv", "OTHER/DX/"),
        ]
        FakeGuangyaClient.fs_files_impl = self._paged_fs_files(dirs, files)

        found = await self._collect(driver, root, parent_path="/影视")

        by_id = {f.remote_id: f for f in found}
        assert set(by_id) == {"F1", "F2", "F3"}
        assert by_id["F1"].relative_dir == ""
        assert by_id["F2"].relative_dir == "电影"
        assert by_id["F3"].relative_dir == "电影/剧集"
        # path = parent_path + relative_dir + 文件名
        assert by_id["F1"].path == "/影视/顶层.mkv"
        assert by_id["F3"].path == "/影视/电影/剧集/剧集B.mkv"
        # 字段透传
        assert by_id["F3"].size == 1024
        assert by_id["F3"].mtime == 1700000000
        assert by_id["F3"].is_dir is False

    async def test_include_subdirs_false(self) -> None:
        """False 时只产出媒体库根的直接子文件（链长 1）。"""
        driver = gyp_module.GuangyaDriver(_credential_json())
        root = self.ROOT
        dirs = [self._dir("D1", "电影", f"{root}/")]
        files = [
            self._file("F1", "顶层.mkv", f"{root}/"),
            self._file("F2", "深层.mkv", f"{root}/D1/"),
        ]
        FakeGuangyaClient.fs_files_impl = self._paged_fs_files(dirs, files)

        found = await self._collect(driver, root, include_subdirs=False)

        assert [f.remote_id for f in found] == ["F1"]

    async def test_missing_full_parent_ids_raises(self) -> None:
        """条目缺 fullParentIds = 接口改版——必须报错。

        静默跳过会把故障伪装成「媒体库为空」，prune 还会把源端还在
        的文件当孤儿删掉。
        """
        driver = gyp_module.GuangyaDriver(_credential_json())
        root = self.ROOT
        dirs = [self._dir("D1", "电影", f"{root}/")]
        bad = self._file("F1", "怪条目.mkv", f"{root}/")
        del bad["fullParentIds"]
        FakeGuangyaClient.fs_files_impl = self._paged_fs_files(dirs, [bad])

        with pytest.raises(DriverError, match="fullParentIds"):
            await self._collect(driver, root)

    async def test_dir_name_missing_uses_placeholder(self) -> None:
        """目录映射缺少量 id：用 fileId 占位继续（跳过会让 prune 误删）。"""
        driver = gyp_module.GuangyaDriver(_credential_json())
        root = self.ROOT
        dirs = [self._dir("D1", "电影", f"{root}/")]  # 故意不含 D2
        files = [self._file("F2", "剧集B.mkv", f"{root}/D1/D2/")]
        FakeGuangyaClient.fs_files_impl = self._paged_fs_files(dirs, files)

        found = await self._collect(driver, root)

        assert len(found) == 1
        assert found[0].relative_dir == "电影/D2"
        assert found[0].path == "/电影/D2/剧集B.mkv"

    async def test_dir_name_missing_over_limit_raises(self) -> None:
        """目录映射缺失超过上限：接口可能已改版——宁可扫描失败可见。"""
        driver = gyp_module.GuangyaDriver(_credential_json())
        root = self.ROOT
        files = [
            self._file(f"F{i}", f"{i}.mkv", f"{root}/M{i}/")
            for i in range(gyp_module.MISSING_DIR_NAME_LIMIT + 1)
        ]
        FakeGuangyaClient.fs_files_impl = self._paged_fs_files([], files)

        with pytest.raises(DriverError, match="缺失过多"):
            await self._collect(driver, root)

    async def test_pagination_terminates_and_uses_large_page(self) -> None:
        """翻页在不足一页后停止；page_size 用 LIST_PAGE_SIZE（1000）。"""
        driver = gyp_module.GuangyaDriver(_credential_json())
        root = self.ROOT
        dirs = [self._dir("D1", "电影", f"{root}/")]
        files = [
            self._file("F1", "a.mkv", f"{root}/D1/"),
            self._file("F2", "b.mkv", f"{root}/D1/"),
        ]
        FakeGuangyaClient.fs_files_impl = self._paged_fs_files(dirs, files)

        found = await self._collect(driver, root)

        assert {f.remote_id for f in found} == {"F1", "F2"}

    async def test_res_type_ignored_defense_skips_dirs(self) -> None:
        """服务端忽略 res_type 把目录混进文件列表时——跳过目录条目。"""
        driver = gyp_module.GuangyaDriver(_credential_json())
        root = self.ROOT
        dirs = [self._dir("D1", "电影", f"{root}/")]
        files = [
            self._file("F1", "a.mkv", f"{root}/D1/"),
            self._dir("DX", "混进来的目录", f"{root}/"),
        ]
        FakeGuangyaClient.fs_files_impl = self._paged_fs_files(dirs, files)

        found = await self._collect(driver, root)

        assert [f.remote_id for f in found] == ["F1"]

    async def test_empty_cid_raises(self) -> None:
        """空根 fileId 无法与 fullParentIds 匹配——明确报错。"""
        driver = gyp_module.GuangyaDriver(_credential_json())
        with pytest.raises(DriverError, match="非空"):
            await self._collect(driver, "")

    async def test_nonzero_code_raises(self) -> None:
        """业务错误码必须抛 DriverError（不写 0 兜底的契约）。"""
        driver = gyp_module.GuangyaDriver(_credential_json())
        root = self.ROOT

        def _fs_files(self: Any, **kwargs: Any) -> dict[str, Any]:
            if kwargs.get("res_type") == 2:
                return {"code": 111, "msg": "访问拒绝", "data": {}}
            return {"code": 0, "data": {"total": 0, "list": []}}

        FakeGuangyaClient.fs_files_impl = _fs_files
        with pytest.raises(DriverError, match="访问拒绝"):
            await self._collect(driver, root)


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


class TestJwtExpiresAt:
    """``_jwt_expires_at``：**解不出返回 0**（不得抛异常）。"""

    @staticmethod
    def _token(payload: object) -> str:
        seg = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        return f"header.{seg.rstrip('=')}.signature"

    def test_reads_exp(self) -> None:
        assert gyp_module._jwt_expires_at(self._token({"exp": 123})) == 123

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param([1, 2, 3], id="list"),
            pytest.param("hello", id="string"),
            pytest.param(42, id="number"),
            pytest.param(None, id="null"),
            pytest.param({}, id="no_exp"),
            pytest.param({"exp": "abc"}, id="bad_exp"),
        ],
    )
    def test_malformed_payload_returns_zero(self, payload: object) -> None:
        """合法 JSON 但不是对象 / 取不出 exp → 一律 0，不抛 AttributeError。"""
        assert gyp_module._jwt_expires_at(self._token(payload)) == 0

    def test_garbage_returns_zero(self) -> None:
        assert gyp_module._jwt_expires_at("not-a-jwt") == 0
        assert gyp_module._jwt_expires_at("a.!!!not-base64!!!.c") == 0


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


class TestDirectLinkBucket:
    """换直链走独立限速桶（官方限频 2 次/秒，比列目录的 5 次/秒严）。"""

    async def test_uses_link_bucket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core import rate_limit as rate_limit_module

        seen: list[str] = []

        async def _fake_acquire(provider: str = "default") -> None:
            seen.append(provider)

        monkeypatch.setattr(rate_limit_module.rate_limiter, "acquire", _fake_acquire)
        driver = gyp_module.GuangyaDriver(_credential_json())

        await driver.get_direct_link("123", "Player/1.0")

        assert gyp_module.PROVIDER_LINK in seen
        # 列目录用的桶不能混进来（两者官方限频不同）
        assert gyp_module.PROVIDER not in seen
        assert gyp_module.PROVIDER_LINK != gyp_module.PROVIDER


class TestBatchCopyDelete:
    """批量复制/删除：**id 与 parentId 一律转字符串**。

    光鸭的 ``fs_copy`` / ``fs_delete`` 收到数字 id 会返回
    ``code=112 参数错误``（2026-11-13 生产实测），而第三方客户端
    ``guangyaclient`` 的类型签名写着 ``Optional[int]``——照签名传 int
    必炸。驱动层统一转 str，调用方传什么都安全。
    """

    async def test_copy_files_converts_ids_to_str(self) -> None:
        driver = gyp_module.GuangyaDriver(_credential_json())

        await driver.copy_files([123, "456"], parent_id=789)  # type: ignore[list-item]

        assert FakeGuangyaClient.copy_calls == [(["123", "456"], "789")]

    async def test_delete_files_converts_ids_to_str(self) -> None:
        driver = gyp_module.GuangyaDriver(_credential_json())

        await driver.delete_files([123, "456"])  # type: ignore[list-item]

        assert FakeGuangyaClient.delete_calls == [["123", "456"]]

    async def test_delete_file_uses_same_path(self) -> None:
        """单删与批删走同一条路径（id 转字符串 + 同一套错误映射）。"""
        driver = gyp_module.GuangyaDriver(_credential_json())

        await driver.delete_file("789")

        assert FakeGuangyaClient.delete_calls == [["789"]]

    async def test_copy_files_raises_on_error_code(self) -> None:
        FakeGuangyaClient.copy_code = 112
        driver = gyp_module.GuangyaDriver(_credential_json())

        with pytest.raises(DriverError, match="复制文件失败"):
            await driver.copy_files(["1"], parent_id="2")

    async def test_delete_files_raises_on_error_code(self) -> None:
        FakeGuangyaClient.delete_code = 111
        driver = gyp_module.GuangyaDriver(_credential_json())

        with pytest.raises(DriverError, match="删除文件失败"):
            await driver.delete_files(["1"])


@contextmanager
def _mock_refresh_and_link(refreshed: dict[str, Any]):
    """mock 两个端点：``/v1/auth/token`` 返回 refresh 结果，其余返回直链。"""
    import httpx

    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = request.content.decode("utf-8", "replace")
        calls.append(
            {
                "path": path,
                "authorization": request.headers.get("authorization"),
                "x_client_id": request.headers.get("x-client-id"),
                "x_action": request.headers.get("x-action"),
                "body": json.loads(body) if body else {},
            }
        )
        if path == "/v1/auth/token":
            return httpx.Response(200, json=refreshed)
        return httpx.Response(
            200,
            json={"code": 0, "data": {"signedURL": "https://cdn.test/client", "urlDuration": 21600}},
        )

    original = httpx.AsyncClient

    class _Patched(original):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.pop("transport", None)
            super().__init__(*args, transport=httpx.MockTransport(handler), **kwargs)

    httpx.AsyncClient = _Patched  # type: ignore[misc]
    try:
        yield calls
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]


@contextmanager
def _mock_client_link_api(payload: dict[str, Any], seen: dict[str, Any] | None = None):
    """把 httpx 的 POST 换成固定 payload，并记录请求头（测客户端取链）。"""
    import httpx

    record = seen if seen is not None else {}

    def handler(request: httpx.Request) -> httpx.Response:
        record["authorization"] = request.headers.get("authorization")
        record["dt"] = request.headers.get("dt")
        record["x_client_id"] = request.headers.get("x-client-id")
        record["did"] = request.headers.get("did")
        record["path"] = request.url.path
        record["body"] = request.content.decode("utf-8", "replace")
        return httpx.Response(200, json=payload)

    original = httpx.AsyncClient

    class _Patched(original):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.pop("transport", None)
            super().__init__(*args, transport=httpx.MockTransport(handler), **kwargs)

    httpx.AsyncClient = _Patched  # type: ignore[misc]
    try:
        yield record
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]


class TestClientTokenLink:
    """客户端 token 覆盖层（2026-11-19 逆向实测后加入）。"""

    async def test_uses_client_token_when_valid(self) -> None:
        """有效客户端 token → 走 ``/userres/v1`` + 客户端身份头。"""
        driver = gyp_module.GuangyaDriver(
            _credential_json(
                client_access_token="client-tok",
                client_expires_at=9_999_999_999,
                client_device_id="client-dev",
            )
        )
        with _mock_client_link_api(
            {"code": 0, "data": {"signedURL": "https://cdn.test/c", "urlDuration": 21600}}
        ) as seen:
            link = await driver.get_direct_link("123", "Player/1.0")

        assert link.url == "https://cdn.test/c"
        assert link.ua_required is False
        assert seen["authorization"] == "Bearer client-tok"
        assert seen["path"] == "/userres/v1/get_res_download_url"
        assert seen["dt"] == "6"
        assert seen["x_client_id"] == "al9eLEtLE7_pp8GR"
        assert seen["did"] == "client-dev"
        assert '"fileId": "123"' in seen["body"] or '"fileId":"123"' in seen["body"]

    async def test_falls_back_to_web_when_client_token_expired(self) -> None:
        """客户端 token 过期 → 直接用 Web 路径（不发客户端请求）。"""
        driver = gyp_module.GuangyaDriver(
            _credential_json(client_access_token="client-tok", client_expires_at=1)
        )
        called: list[str] = []

        def _download_url(self: Any, file_id: str) -> dict[str, Any]:
            called.append(file_id)
            return {
                "code": 0,
                "data": {"signedURL": "https://cdn.test/web", "urlDuration": 3600},
            }

        FakeGuangyaClient.download_url_impl = _download_url
        link = await driver.get_direct_link("123", "Player/1.0")

        assert called == ["123"]
        assert link.url == "https://cdn.test/web"

    async def test_falls_back_to_web_on_client_auth_error(self) -> None:
        """客户端 token 被服务端判 401（117）→ 回退 Web，不把错误抛出去。"""
        driver = gyp_module.GuangyaDriver(
            _credential_json(
                client_access_token="client-tok", client_expires_at=9_999_999_999
            )
        )

        def _download_url(self: Any, file_id: str) -> dict[str, Any]:
            return {
                "code": 0,
                "data": {"signedURL": "https://cdn.test/web", "urlDuration": 3600},
            }

        FakeGuangyaClient.download_url_impl = _download_url
        with _mock_client_link_api({"code": 117, "msg": "token expired"}):
            link = await driver.get_direct_link("123", "Player/1.0")

        assert link.url == "https://cdn.test/web"

    async def test_client_non_auth_error_is_raised(self) -> None:
        """非鉴权错误（如 254 大小限制）**不回退**——回退也拿不到，只会掩盖原因。"""
        driver = gyp_module.GuangyaDriver(
            _credential_json(
                client_access_token="client-tok", client_expires_at=9_999_999_999
            )
        )
        with (
            _mock_client_link_api({"code": 254, "msg": "big file"}),
            pytest.raises(DriverError) as exc,
        ):
            await driver.get_direct_link("123", "Player/1.0")
        assert "254" in str(exc.value) or "big file" in str(exc.value)

    async def test_credential_roundtrip_keeps_client_fields(self) -> None:
        cred = GuangyaCredential(
            access_token="web",
            refresh_token="ref",
            device_id="dev",
            expires_at=1,
            client_access_token="client",
            client_expires_at=2,
            client_device_id="client-dev",
            client_refresh_token="client-ref",
        )
        assert GuangyaCredential.from_json(cred.to_json()) == cred

    async def test_client_token_refreshed_with_client_aud(self) -> None:
        """客户端 token 过期 + 有 RT → **自己续期**，不再回退 Web。

        实测口径（2026-11-19）：``client_id`` 必须用客户端值，aud 才不漂移；
        ``refresh_token`` 服务端不轮换。
        """
        seen: dict[str, Any] = {}
        refreshed = {
            "access_token": "fresh-client",
            "refresh_token": "ref-same",
            "expires_in": 7200,
        }

        def _download_url(self: Any, file_id: str) -> dict[str, Any]:
            raise AssertionError("不该回退 Web 路径")

        FakeGuangyaClient.download_url_impl = _download_url
        driver = gyp_module.GuangyaDriver(
            _credential_json(
                client_access_token="old-client",
                client_expires_at=1,
                client_device_id="client-dev",
                client_refresh_token="ref-same",
            ),
            on_token_refresh=lambda c: seen.setdefault("cred", c),
        )
        with _mock_refresh_and_link(refreshed) as calls:
            link = await driver.get_direct_link("123", "Player/1.0")

        assert link.url == "https://cdn.test/client"
        assert calls[0]["path"] == "/v1/auth/token"
        assert calls[0]["x_client_id"] == "al9eLEtLE7_pp8GR"
        assert calls[0]["x_action"] == "401"
        assert calls[0]["body"]["grant_type"] == "refresh_token"
        assert calls[0]["body"]["refresh_token"] == "ref-same"
        assert calls[0]["body"]["client_id"] == "al9eLEtLE7_pp8GR"
        assert calls[1]["authorization"] == "Bearer fresh-client"
        assert seen["cred"].client_access_token == "fresh-client"
        assert seen["cred"].client_refresh_token == "ref-same"

    async def test_client_mode_main_token_uses_client_refresh(self) -> None:
        """主 token 就是客户端 token（client 模式登录）→ 续期走客户端路径。"""
        driver = gyp_module.GuangyaDriver(
            _credential_json(
                access_token="old-client",
                refresh_token="",
                expires_at=1,
                client_access_token="old-client",
                client_expires_at=1,
                client_device_id="client-dev",
                client_refresh_token="ref-same",
            )
        )
        with _mock_refresh_and_link(
            {"access_token": "fresh-client", "refresh_token": "ref-same", "expires_in": 7200}
        ) as calls:
            await driver.list_root()

        # 只发了一次续期请求；Web 的 guangyaclient.refresh_token 没被调用
        assert len(calls) == 1
        assert calls[0]["path"] == "/v1/auth/token"


class TestClientTokenBusinessSync:
    """客户端身份续期后，**业务客户端必须用上新 token**。

    2026-11-20 生产事故：driver 5 的 token 在 12:21 已续期并写回 DB，
    13:11 的 ``fs_create_dir`` 仍 401——新 token 只进了凭据对象，
    没进 ``guangyaclient`` 实例（它的 ``authorization`` 头在**构造时**
    就写死了旧 token）。上传工厂恰好在构造期就把 client 取走 → 命中。
    """

    async def test_refresh_syncs_business_client(self) -> None:
        """客户端身份续期后，**已缓存的业务客户端必须用上新 token**。"""
        driver = gyp_module.GuangyaDriver(
            _credential_json(
                access_token="old-client",
                refresh_token="",
                expires_at=1,
                client_access_token="old-client",
                client_expires_at=1,
                client_device_id="client-dev",
                client_refresh_token="ref-same",
            )
        )
        # ← 上传工厂（build_guangya_uploader）构造期就把 client 取走了
        client = driver.client

        with _mock_refresh_and_link(
            {
                "access_token": "fresh-client",
                "refresh_token": "ref-same",
                "expires_in": 7200,
            }
        ):
            await driver.list_root()

        assert client.token == "fresh-client"
        assert client.seen_tokens == ["fresh-client"]

    async def test_refresh_when_expiry_unknown(self) -> None:
        """过期时间未知（=0）且有 RT → 主动续期。

        旧实现把「未知」当成**永不过期** → token 死了也不续期，
        只能靠撞 401（生产事故的一环）。
        """
        driver = gyp_module.GuangyaDriver(
            _credential_json(
                access_token="old-client",
                refresh_token="",
                expires_at=1,
                client_access_token="old-client",
                client_expires_at=0,
                client_device_id="client-dev",
                client_refresh_token="ref-same",
            )
        )
        with _mock_refresh_and_link(
            {
                "access_token": "fresh-client",
                "refresh_token": "ref-same",
                "expires_in": 7200,
            }
        ):
            await driver._ensure_token()

        assert driver._cred is not None
        assert driver._cred.access_token == "fresh-client"


class TestUnauthorizedFallback:
    """业务接口 401 → 强制刷新一次再重试。

    2026-11-20 生产事故：上传任务 69 个文件全挂在同一处
    ``fs_create_dir`` 401——服务端把 token 判死（账号在别处重新登录会
    **立即作废**旧 token），而本地记录的过期时间还没到 → 不刷新 → 全挂。
    """

    async def test_401_refreshes_and_retries(self) -> None:
        """业务调用遇 401 时强制刷新一次，并以新 token 重试。"""
        FakeGuangyaClient.unauthorized_times = 1
        driver = gyp_module.GuangyaDriver(
            _credential_json(
                access_token="tok", refresh_token="ref", expires_at=9_999_999_999
            )
        )
        client = driver.client

        payload = await driver._call("fs_create_dir", "Season 1", parent_id="123")

        assert payload["code"] == 0
        assert client.refresh_calls == 1
        # 第一次带旧 token（被 401），重试带新 token
        assert client.seen_tokens == ["tok", "new-token-1"]

    async def test_401_without_refresh_token_raises_auth_error(self) -> None:
        """刷新不了（没 RT）→ 明确报鉴权失败，别含糊成「接口调用失败」。"""
        FakeGuangyaClient.unauthorized_times = 99
        driver = gyp_module.GuangyaDriver(
            _credential_json(
                access_token="tok", refresh_token="", expires_at=9_999_999_999
            )
        )
        with pytest.raises(DriverError) as exc:
            await driver._call("fs_create_dir", "Season 1", parent_id="123")
        assert exc.value.kind == "auth"
        assert "401" in str(exc.value)

    async def test_non_401_error_is_not_retried(self) -> None:
        """非 401 的异常**不重试**（别把真 bug 变成重试风暴）。"""

        def _boom(self: Any, name: str, parent_id: str | None = None) -> dict[str, Any]:
            raise RuntimeError("boom")

        FakeGuangyaClient.fs_create_dir_impl = _boom
        driver = gyp_module.GuangyaDriver(
            _credential_json(
                access_token="tok", refresh_token="ref", expires_at=9_999_999_999
            )
        )
        client = driver.client

        with pytest.raises(DriverError) as exc:
            await driver._call("fs_create_dir", "Season 1", parent_id="123")

        assert "boom" in str(exc.value)
        assert client.refresh_calls == 0
        assert client.seen_tokens == ["tok"]
