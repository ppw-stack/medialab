"""115 驱动的**调用方式**契约测试。

本仓库没有真实 115 账号，故这些测试**不验证网络行为**，只验证
**调用 p115client 的方式正确**——历史上正是这几种写法错了：

1. ``P115Client.download_url(pc, ...)``（类级）→ ``TypeError``
2. ``client.fs_iter(path)`` → ``AttributeError``（没有这个方法）
3. ``client.check_login()`` → ``AttributeError``（是 ``login_status``）
4. ``tool.dir_getid(client, cid, name=...)`` → ``TypeError``
   （签名是 ``dir_getid(client, path)``，吃完整路径）

用 fake client 断言**调用方式**，防止再写错。
"""

from __future__ import annotations

from typing import Any

import pytest
from app.adapters.drivers import p115 as p115_mod
from app.adapters.drivers.base import DriverError


class FakeClient:
    """假 115 客户端——记录调用方式。"""

    def __init__(self, files: list[dict[str, Any]] | None = None) -> None:
        self.files = files or []
        self.download_url_calls: list[dict[str, Any]] = []
        self.login_status_calls: list[dict[str, Any]] = []
        self.iterdir_calls: list[tuple[Any, Any]] = []

    async def download_url(self, pickcode: str, **kwargs: Any) -> str:
        self.download_url_calls.append({"pickcode": pickcode, **kwargs})
        return f"https://cdn.115.com/{pickcode}?t=9999999999&f=1"

    async def login_status(self, **kwargs: Any) -> bool:
        self.login_status_calls.append(kwargs)
        return True


@pytest.fixture
def fake_client(monkeypatch) -> FakeClient:
    """把驱动的 client 工厂替换成 fake。"""
    client = FakeClient()
    monkeypatch.setattr(p115_mod, "_get_client", lambda cookie, device_type=None: client)
    return client


@pytest.fixture
def fake_tool(monkeypatch):
    """替换 p115client.tool 的 iterdir / dir_getid。"""
    calls: dict[str, Any] = {"iterdir": [], "dir_getid": []}

    class FakeTool:
        @staticmethod
        def iterdir(client: Any, cid: Any, **kwargs: Any) -> list[dict[str, Any]]:
            calls["iterdir"].append((client, cid, kwargs))
            return list(calls.get("files", []))

        @staticmethod
        async def dir_getid(client: Any, path: str, **kwargs: Any) -> int:
            calls["dir_getid"].append((client, path, kwargs))
            return 12345

    import sys
    from types import SimpleNamespace

    fake_module = SimpleNamespace(iterdir=FakeTool.iterdir, dir_getid=FakeTool.dir_getid)
    monkeypatch.setitem(sys.modules, "p115client.tool", fake_module)
    monkeypatch.setitem(sys.modules, "p115client", SimpleNamespace(tool=fake_module))
    calls["files"] = []
    return calls


class TestDirectLink:
    async def test_download_url_called_on_instance(self, fake_client):
        """必须是**实例**调用（类级调用会报 missing 'pickcode'）。"""
        driver = p115_mod.P115Driver("UID=1; CID=2")

        link = await driver.get_direct_link("abc123", "Mozilla/5.0")

        assert link.url.startswith("https://cdn.115.com/abc123")
        assert len(fake_client.download_url_calls) == 1

    async def test_user_agent_is_forwarded(self, fake_client):
        """UA 必须透传（115 直链 f=1 绑定 UA）。"""
        driver = p115_mod.P115Driver("UID=1")

        await driver.get_direct_link("abc123", "Infuse/1.0")

        call = fake_client.download_url_calls[0]
        assert call["user_agent"] == "Infuse/1.0"

    async def test_ua_required_is_true(self, fake_client):
        """115 直链默认绑定 UA。"""
        driver = p115_mod.P115Driver("UID=1")
        link = await driver.get_direct_link("abc", "UA")
        assert link.ua_required is True

    async def test_driver_error_on_failure(self, fake_client, monkeypatch):
        """换直链失败 → 抛 DriverError（不返回空）。"""

        async def boom(*args: Any, **kwargs: Any) -> str:
            raise RuntimeError("network down")

        monkeypatch.setattr(fake_client, "download_url", boom)
        driver = p115_mod.P115Driver("UID=1")

        with pytest.raises(DriverError):
            await driver.get_direct_link("abc", "UA")

    async def test_empty_url_is_error(self, fake_client, monkeypatch):
        async def empty(*args: Any, **kwargs: Any) -> str:
            return ""

        monkeypatch.setattr(fake_client, "download_url", empty)
        driver = p115_mod.P115Driver("UID=1")

        with pytest.raises(DriverError):
            await driver.get_direct_link("abc", "UA")


class TestListDir:
    async def test_uses_tool_iterdir(self, fake_client, fake_tool):
        """列目录必须走 tool.iterdir（**没有** fs_iter 方法）。"""
        fake_tool["files"] = [
            {
                "id": 1,
                "name": "movie.mkv",
                "size": 1024,
                "pick_code": "pc_abc",
                "is_dir": False,
                "mtime": 1700000000,
                "sha1": "deadbeef",
            }
        ]
        driver = p115_mod.P115Driver("UID=1")

        files = await driver.list_dir("/电影")

        assert len(files) == 1
        assert files[0].name == "movie.mkv"
        assert files[0].remote_id == "pc_abc"
        assert fake_tool["iterdir"], "应调用 tool.iterdir"

    async def test_dir_getid_called_with_full_path(self, fake_client, fake_tool):
        """dir_getid 吃**完整路径**（不是 cid + name）。"""
        driver = p115_mod.P115Driver("UID=1")

        await driver.list_dir("/电影/科幻")

        assert fake_tool["dir_getid"], "应调用 tool.dir_getid"
        _client, path, _kwargs = fake_tool["dir_getid"][0]
        assert path == "/电影/科幻"

    async def test_iterdir_uses_web_app(self, fake_client, fake_tool):
        """**必须固定用 app="web"**（不能用 device_type）。

        回归背景：``app="web"`` 走 ``client.fs_files``（webapi，实测正常）；
        其它 app 走 ``client.fs_files_app``（proapi，**返回 405**）。
        实测：``app=alipaymini`` 时 ``/影视/电影`` 返回 405，改用 web 正常。
        ``device_type`` 只影响「这次登录记成哪台设备」，与列目录无关。
        """
        driver = p115_mod.P115Driver("UID=1", device_type="alipaymini")

        await driver.list_dir_by_cid("12345")

        _client, _cid, kwargs = fake_tool["iterdir"][0]
        assert kwargs.get("app") == "web", "列目录必须用 app=web（其它 app 会 405）"

    async def test_iterdir_app_is_web_regardless_of_device_type(
        self, fake_client, fake_tool
    ):
        """无 device_type 时同样是 web。"""
        driver = p115_mod.P115Driver("UID=1", device_type=None)

        await driver.list_dir_by_cid("12345")

        _client, _cid, kwargs = fake_tool["iterdir"][0]
        assert kwargs.get("app") == "web"

    async def test_normalized_field_names(self, fake_client, fake_tool):
        """字段名按 tool.iterdir 的归一化结果：id/name/size/pick_code/is_dir。"""
        fake_tool["files"] = [
            {
                "id": 99,
                "name": "clip.mkv",
                "size": 2048,
                "pick_code": "pc_clip",
                "is_dir": False,
                "mtime": 123,
                "sha1": "abc",
            }
        ]
        driver = p115_mod.P115Driver("UID=1")

        files = await driver.list_dir("/")

        f = files[0]
        assert f.remote_id == "pc_clip"
        assert f.name == "clip.mkv"
        assert f.size == 2048
        assert f.is_dir is False
        assert f.mtime == 123
        assert f.sha1 == "abc"

    async def test_directory_uses_id_as_remote_id(self, fake_client, fake_tool):
        """目录没有 pick_code → 用 id 占位。"""
        fake_tool["files"] = [{"id": 77, "name": "电影", "is_dir": True, "size": 0}]
        driver = p115_mod.P115Driver("UID=1")

        files = await driver.list_dir("/")

        assert files[0].remote_id == "77"
        assert files[0].is_dir is True

    async def test_driver_error_on_missing_dir(self, fake_client, fake_tool, monkeypatch):
        """目录不存在 → DriverError(kind=not_found)。"""
        import sys
        from types import SimpleNamespace

        async def boom(client: Any, path: str, **kwargs: Any) -> int:
            raise RuntimeError("目录不存在")

        fake_module = SimpleNamespace(
            iterdir=lambda c, cid: [],
            dir_getid=boom,
        )
        monkeypatch.setitem(sys.modules, "p115client.tool", fake_module)
        monkeypatch.setitem(sys.modules, "p115client", SimpleNamespace(tool=fake_module))

        driver = p115_mod.P115Driver("UID=1")
        with pytest.raises(DriverError) as exc:
            await driver.list_dir("/不存在")
        assert exc.value.kind == "not_found"


class TestCheckLogin:
    async def test_uses_login_status(self, fake_client):
        """必须用 login_status（**没有** check_login 方法）。"""
        driver = p115_mod.P115Driver("UID=1")

        ok = await driver.check_login()

        assert ok is True
        assert fake_client.login_status_calls, "应调用 login_status"

    async def test_returns_false_on_exception(self, fake_client, monkeypatch):
        async def boom(**kwargs: Any) -> bool:
            raise RuntimeError("network")

        monkeypatch.setattr(fake_client, "login_status", boom)
        driver = p115_mod.P115Driver("UID=1")

        assert await driver.check_login() is False


class TestCloudFileConversion:
    def test_none_item_skipped(self) -> None:
        assert p115_mod._to_cloud_file(None) is None

    def test_empty_id_skipped(self) -> None:
        assert p115_mod._to_cloud_file({"name": "x", "is_dir": False}) is None

    def test_pickcode_alias_accepted(self) -> None:
        """兼容 pickcode（无下划线）别名。"""
        cf = p115_mod._to_cloud_file(
            {"id": 1, "name": "a.mkv", "pickcode": "pc1", "is_dir": False}
        )
        assert cf is not None
        assert cf.remote_id == "pc1"


class TestAbsolutePathBuilding:
    """``tool.iterdir`` 的 path 字段是**相对当前目录**的——必须自己拼。

    回归背景：曾直接用 ``item["path"]``，导致列 ``/影视/电影`` 时子项
    path 变成 ``/阿凡达 (2009)``（丢了父目录），递归时再也列不出来。
    实测报错：``115 目录不存在或无权限：/不说话的爱 (2025)``
    """

    def test_path_uses_parent_directory(self) -> None:
        cf = p115_mod._to_cloud_file(
            {"id": 1, "name": "阿凡达 (2009)", "is_dir": True},
            parent_path="/影视/电影",
        )
        assert cf is not None
        assert cf.path == "/影视/电影/阿凡达 (2009)"

    def test_ignores_relative_path_field(self) -> None:
        """即使 item 自带相对 path，也用父目录拼出的绝对路径。"""
        cf = p115_mod._to_cloud_file(
            {"id": 1, "name": "阿凡达", "path": "/阿凡达", "is_dir": True},
            parent_path="/影视/电影",
        )
        assert cf is not None
        assert cf.path == "/影视/电影/阿凡达"

    def test_root_parent(self) -> None:
        cf = p115_mod._to_cloud_file(
            {"id": 1, "name": "影视", "is_dir": True}, parent_path="/"
        )
        assert cf is not None
        assert cf.path == "/影视"

    def test_empty_parent(self) -> None:
        cf = p115_mod._to_cloud_file(
            {"id": 1, "name": "影视", "is_dir": True}, parent_path=""
        )
        assert cf is not None
        assert cf.path == "/影视"

    def test_trailing_slash_normalized(self) -> None:
        cf = p115_mod._to_cloud_file(
            {"id": 1, "name": "x", "is_dir": True}, parent_path="/影视/"
        )
        assert cf is not None
        assert cf.path == "/影视/x"

    def test_join_path_helper(self) -> None:
        assert p115_mod._join_path("/a", "b") == "/a/b"
        assert p115_mod._join_path("/a/", "b") == "/a/b"
        assert p115_mod._join_path("/", "b") == "/b"
        assert p115_mod._join_path("", "b") == "/b"
