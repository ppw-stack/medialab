"""WebDAV Provider 的单测。

覆盖三个安全关键行为：

1. **只读** —— 写方法被拒
2. **路径安全** —— 逃出根目录被拒（软链）
3. **Depth 限制** —— 缺省降级为 1、显式 infinity 被拒
"""

from __future__ import annotations

import os

import pytest
from app.webdav.provider import DavProvider
from wsgidav.dav_error import DAVError


@pytest.fixture
def lib(tmp_path):
    """造一棵媒体库目录树。"""
    root = tmp_path / "library"
    (root / "电影" / "阿凡达").mkdir(parents=True)
    (root / "剧集" / "西部世界").mkdir(parents=True)
    (root / "音乐").mkdir()
    (root / "电影" / "阿凡达" / "阿凡达.strm").write_text("http://example.com/302/1/sig")
    (root / "剧集" / "西部世界" / "西部世界.strm").write_text("http://example.com/302/2/sig")
    (root / ".hidden").write_text("secret")
    return root


def _environ(root=None, provider=None, **extra) -> dict:
    env = {
        "REQUEST_METHOD": "PROPFIND",
        "PATH_INFO": "/",
        "wsgidav.provider": provider,
        "wsgidav.config": {},
        "wsgidav.verbose": 1,
    }
    if root is not None:
        from app.webdav.provider import ENV_ROOT

        env[ENV_ROOT] = str(root)
    env.update(extra)
    return env


# --------------------------------------------------------------------------
# 只读
# --------------------------------------------------------------------------


def test_provider_is_readonly(lib):
    """Provider 是只读的。"""
    provider = DavProvider(str(lib))
    assert provider.is_readonly() is True


def test_write_methods_are_rejected(lib):
    """写方法（PUT/DELETE/MKCOL/LOCK）在资源层被拒。

    wsgidav 的 ``RequestServer`` 会因 ``is_readonly()`` 直接不把这些方法
    列入「允许的方法」→ 返 405。这里验证资源层的兜底检查也在。
    """
    provider = DavProvider(str(lib))
    env = _environ(lib, provider=provider)

    res = provider.get_resource_inst("/电影/阿凡达/阿凡达.strm", env)
    assert res is not None
    # begin_write / delete 在 readonly 下抛 403
    with pytest.raises(DAVError):
        res.begin_write()
    with pytest.raises(DAVError):
        res.delete()


# --------------------------------------------------------------------------
# 路径安全
# --------------------------------------------------------------------------


def test_path_escape_rejected(lib, tmp_path):
    """``..`` 逃出根目录被拒。

    注意 wsgidav 自己会先做 ``normalize_path``，但 provider 层也必须挡住
    —— 不能依赖上游一定先规范化过。
    """
    provider = DavProvider(str(lib))
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")

    with pytest.raises(DAVError):
        provider._loc_to_file_path("/../secret.txt", _environ(lib))


def test_symlink_escape_rejected(lib, tmp_path):
    """软链指向根外被拒（realpath 校验）。"""
    provider = DavProvider(str(lib))
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    link = lib / "link.txt"
    os.symlink(outside, link)

    with pytest.raises(DAVError):
        provider._loc_to_file_path("/link.txt", _environ(lib))


def test_symlink_resource_rejected(lib, tmp_path):
    """软链本身不可作为资源返回。"""
    provider = DavProvider(str(lib))
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    os.symlink(outside, lib / "link.txt")

    with pytest.raises(DAVError):
        provider.get_resource_inst("/link.txt", _environ(lib))


def test_normal_path_allowed(lib):
    """正常路径不被误挡。"""
    provider = DavProvider(str(lib))
    path = provider._loc_to_file_path("/电影/阿凡达/阿凡达.strm", _environ(lib))
    assert path.endswith("阿凡达.strm")
    assert os.path.exists(path)


# --------------------------------------------------------------------------
# 根目录切换
# --------------------------------------------------------------------------


def test_root_from_header(lib, tmp_path):
    """请求头里的根目录生效。"""
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.strm").write_text("x")

    provider = DavProvider(str(lib))
    env = _environ(other)
    res = provider.get_resource_inst("/x.strm", env)
    assert res is not None


def test_root_fallback_to_constructor(lib):
    """没有请求头时回退到构造时的根。"""
    provider = DavProvider(str(lib))
    res = provider.get_resource_inst("/电影/阿凡达/阿凡达.strm", _environ())
    assert res is not None


# --------------------------------------------------------------------------
# Depth 限制
# --------------------------------------------------------------------------


def test_default_depth_downgraded_to_1(lib):
    """缺省 Depth 降级为 1（防全树遍历）。"""
    provider = DavProvider(str(lib))
    env = _environ(lib)
    captured: list[dict] = []

    def default_handler(environ, start_response):
        captured.append(environ)
        return []

    provider.custom_request_handler(env, None, default_handler)
    assert env["HTTP_DEPTH"] == "1"


def test_explicit_infinity_rejected(lib):
    """显式 Depth: infinity 被拒。"""
    provider = DavProvider(str(lib))
    env = _environ(lib, HTTP_DEPTH="infinity")
    with pytest.raises(DAVError):
        provider.custom_request_handler(env, None, lambda e, s: [])


def test_depth_0_and_1_pass_through(lib):
    """Depth 0 / 1 原样放行。"""
    provider = DavProvider(str(lib))
    for depth in ("0", "1"):
        env = _environ(lib, HTTP_DEPTH=depth)
        provider.custom_request_handler(env, None, lambda e, s: [])
        assert env["HTTP_DEPTH"] == depth


def test_invalid_depth_rejected(lib):
    """非法 Depth 值被拒。"""
    provider = DavProvider(str(lib))
    env = _environ(lib, HTTP_DEPTH="2")
    with pytest.raises(DAVError):
        provider.custom_request_handler(env, None, lambda e, s: [])


# --------------------------------------------------------------------------
# 隐藏文件
# --------------------------------------------------------------------------


def test_hidden_files_excluded(lib):
    """隐藏文件（``.`` 开头）不出现在目录列表里。"""
    provider = DavProvider(str(lib))
    env = _environ(lib, provider=provider)
    res = provider.get_resource_inst("/", env)
    assert res is not None
    names = res.get_member_names()
    assert "电影" in names
    assert ".hidden" not in names


# --------------------------------------------------------------------------
# quota 属性（性能关键）
# --------------------------------------------------------------------------


def test_folder_does_not_report_quota(lib):
    """目录不提供 quota 属性。

    **性能关键**：wsgidav 的 ``FolderResource.get_used_bytes`` 调
    ``shutil.disk_usage()``（``statvfs``）。PROPFIND 一个目录时会对
    **每个子目录**调 4 次（探测 2 次 + 取值 2 次）。

    在 CIFS 网络共享上 ``statvfs`` 是网络往返（实测 6~66 ms）——
    231 个子目录 = 928 次 statvfs = PROPFIND 单个目录 5.5 秒。

    完整扫库（971 个目录）因此要 366 秒，客户端会中途超时。
    """
    provider = DavProvider(str(lib))
    env = _environ(lib, provider=provider)
    res = provider.get_resource_inst("/电影", env)
    assert res is not None
    assert res.get_used_bytes() is None
    assert res.get_available_bytes() is None


def test_file_does_not_report_quota(lib):
    """文件不提供 quota 属性。"""
    provider = DavProvider(str(lib))
    env = _environ(lib, provider=provider)
    res = provider.get_resource_inst("/电影/阿凡达/阿凡达.strm", env)
    assert res is not None
    assert res.get_used_bytes() is None
    assert res.get_available_bytes() is None


def test_propfind_does_not_call_disk_usage(lib, monkeypatch):
    """**关键回归**：PROPFIND 全程不调 ``shutil.disk_usage``。

    用 monkeypatch 计数 —— 若将来 wsgidav 升级改了调用路径，这个测试
    能第一时间发现性能回退。
    """
    import shutil

    calls: list[str] = []
    original = shutil.disk_usage

    def _tracking(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "disk_usage", _tracking)

    from app.webdav.app import build_dav_app
    from app.core.config import AppSettings

    settings = AppSettings(env="test", web_token="t", signing_secret="s")
    dav_app = build_dav_app(settings, str(lib))
    provider = dav_app.provider_map["/"]

    env = _environ(lib, provider=provider)
    res = provider.get_resource_inst("/电影", env)
    assert res is not None
    # 模拟 wsgidav 的 get_properties（PROPFIND 的核心）
    for member in res.get_member_list():
        member.get_properties("allprop")

    assert calls == [], f"PROPFIND 不该调 disk_usage，却调了 {len(calls)} 次"
