"""115 限流退避重试的测试。

**背景**：115 对高频列目录 / 换直链会限流（``405 Method Not Allowed``）。
实测生产环境跑全量 4.75 小时后触发一次，**整个扫描任务终止**，
剩下 1460 个文件（5%）全没扫。

修法：退避重试（等 30s / 60s / 90s / 120s），只在**限流**时重试——
其它错误（目录不存在 / 凭据失效）重试也没用，立即抛出。
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest
from app.adapters.drivers import p115 as p115_mod
from app.adapters.drivers.base import DriverError


class FlakyClient:
    """前 N 次调用抛 405，之后正常。"""

    def __init__(self, fail_times: int, exc: Exception | None = None) -> None:
        self.fail_times = fail_times
        self.exc = exc
        self.calls = 0

    async def download_url(self, pickcode: str, **kwargs: Any) -> str:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc or RuntimeError("HTTP Error 405: Method Not Allowed")
        return f"https://cdn.115.com/{pickcode}?t=9999999999&f=1"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """把退避等待记录下来（不真的等）。"""
    waits: list[float] = []

    async def _sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(p115_mod.asyncio, "sleep", _sleep)
    return waits


@pytest.fixture
def fake_tool(monkeypatch: pytest.MonkeyPatch):
    """替换 p115client.tool.iterdir（可编排失败次数）。"""
    calls: dict[str, Any] = {"iterdir": 0, "fail_times": 0}

    class FakeTool:
        @staticmethod
        def iterdir(client: Any, cid: Any, **kwargs: Any) -> list[dict[str, Any]]:
            calls["iterdir"] += 1
            if calls["iterdir"] <= calls["fail_times"]:
                raise RuntimeError("HTTP Error 405: Method Not Allowed")
            return [
                {
                    "name": "a.mkv",
                    "size": 100,
                    "is_dir": False,
                    "id": "fid-1",
                    "pick_code": "pc-1",
                    "mtime": 0,
                }
            ]

    fake_module = SimpleNamespace(iterdir=FakeTool.iterdir)
    monkeypatch.setitem(sys.modules, "p115client.tool", fake_module)
    monkeypatch.setitem(sys.modules, "p115client", SimpleNamespace(tool=fake_module))
    return calls


class TestListDirRetry:
    async def test_retries_on_rate_limit(
        self, monkeypatch: pytest.MonkeyPatch, fake_tool, no_sleep: list[float]
    ) -> None:
        """限流 → 退避重试 → 成功。"""
        client = FlakyClient(0)
        monkeypatch.setattr(
            p115_mod, "_get_client", lambda cookie, device_type=None: client
        )
        fake_tool["fail_times"] = 2
        driver = p115_mod.P115Driver("cookie")

        files = await driver.list_dir_by_cid("123")

        assert len(files) == 1
        assert fake_tool["iterdir"] == 3  # 失败 2 次 + 成功 1 次
        # 退避递增
        assert no_sleep == [30.0, 60.0]

    async def test_gives_up_after_max_retries(
        self, monkeypatch: pytest.MonkeyPatch, fake_tool, no_sleep: list[float]
    ) -> None:
        """一直限流 → 重试耗尽后抛 DriverError（kind=rate_limit）。"""
        client = FlakyClient(0)
        monkeypatch.setattr(
            p115_mod, "_get_client", lambda cookie, device_type=None: client
        )
        fake_tool["fail_times"] = 99
        driver = p115_mod.P115Driver("cookie")

        with pytest.raises(DriverError) as exc:
            await driver.list_dir_by_cid("123")

        assert exc.value.kind == "rate_limit"
        assert fake_tool["iterdir"] == p115_mod.LIST_RETRY_MAX
        assert len(no_sleep) == p115_mod.LIST_RETRY_MAX - 1

    async def test_does_not_retry_other_errors(
        self, monkeypatch: pytest.MonkeyPatch, fake_tool, no_sleep: list[float]
    ) -> None:
        """非限流错误**立即抛出**（重试也没用）。"""
        client = FlakyClient(0)
        monkeypatch.setattr(
            p115_mod, "_get_client", lambda cookie, device_type=None: client
        )
        fake_tool["fail_times"] = 0
        driver = p115_mod.P115Driver("cookie")

        # 造一个「目录不存在」错误
        def _boom(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            raise RuntimeError("目录不存在")

        import p115client.tool as tool_mod

        tool_mod.iterdir = _boom  # type: ignore[assignment]
        with pytest.raises(DriverError) as exc:
            await driver.list_dir_by_cid("123")

        assert exc.value.kind == "not_found"
        assert no_sleep == []  # 没等过


class TestDirectLinkRetry:
    async def test_retries_on_rate_limit(
        self, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
    ) -> None:
        """换直链被限流 → 退避重试 → 成功。"""
        client = FlakyClient(2)
        monkeypatch.setattr(
            p115_mod, "_get_client", lambda cookie, device_type=None: client
        )
        driver = p115_mod.P115Driver("cookie")

        link = await driver.get_direct_link("pc-1", "Player/1.0")

        assert link.url.startswith("https://cdn.115.com/pc-1")
        assert client.calls == 3
        assert no_sleep == [30.0, 60.0]

    async def test_gives_up_after_max_retries(
        self, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
    ) -> None:
        client = FlakyClient(99)
        monkeypatch.setattr(
            p115_mod, "_get_client", lambda cookie, device_type=None: client
        )
        driver = p115_mod.P115Driver("cookie")

        with pytest.raises(DriverError) as exc:
            await driver.get_direct_link("pc-1", "Player/1.0")

        assert exc.value.kind == "rate_limit"
        assert client.calls == p115_mod.LIST_RETRY_MAX

    async def test_does_not_retry_auth_errors(
        self, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
    ) -> None:
        """凭据失效（非限流）→ 立即抛出，不重试。"""
        client = FlakyClient(99, exc=RuntimeError("登录已失效"))
        monkeypatch.setattr(
            p115_mod, "_get_client", lambda cookie, device_type=None: client
        )
        driver = p115_mod.P115Driver("cookie")

        with pytest.raises(DriverError):
            await driver.get_direct_link("pc-1", "Player/1.0")

        assert client.calls == 1
        assert no_sleep == []
