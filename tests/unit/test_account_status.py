"""账号状态探测（测连）的契约测试。

测连要区分**三种结果**，不能笼统报「失败」：

- ``ok`` —— 全部正常
- ``needs_verify`` —— 登录态正常，但换直链被 115 账号安全验证挡住
  （**最常见**的「看起来正常但不能用」）
- ``error`` —— 其它问题
"""

from __future__ import annotations

from typing import Any

from app.adapters.drivers.base import CloudFile, DriverError
from app.services.account_status import (
    STATUS_ERROR,
    STATUS_NEEDS_VERIFY,
    STATUS_OK,
    probe_account,
)


class FakeDriver:
    """假驱动——可编排各步骤的成败。

    容量走驱动契约 ``get_space_info()``（真实驱动各自适配自己的接口）。
    """

    def __init__(
        self,
        *,
        logged_in: bool = True,
        link_error: Exception | None = None,
        space_error: Exception | None = None,
        files: list[CloudFile] | None = None,
    ) -> None:
        self.logged_in = logged_in
        self.link_error = link_error
        self.space_error = space_error
        self.files = files if files is not None else []
        self.link_calls: list[str] = []

    async def check_login(self) -> bool:
        return self.logged_in

    async def list_dir(self, path: str) -> list[CloudFile]:
        if path == "/":
            return list(self.files)
        return []

    async def get_direct_link(self, remote_id: str, user_agent: str):
        self.link_calls.append(remote_id)
        if self.link_error:
            raise self.link_error
        from app.adapters.drivers.base import DirectLink

        return DirectLink(url="https://cdn/f?t=1&f=1")

    async def get_space_info(self) -> dict[str, Any]:
        """容量信息（与真实驱动的契约一致）。"""
        if self.space_error:
            raise self.space_error
        return {
            "account_id": "102140588",
            "total_bytes": 1000,
            "used_bytes": 400,
            "remain_bytes": 600,
        }


class TestProbeAccount:
    async def test_all_good(self) -> None:
        """一切正常。"""
        driver = FakeDriver(
            files=[CloudFile(remote_id="pc1", path="/a.mkv", name="a.mkv")]
        )
        status = await probe_account(driver)
        assert status.status == STATUS_OK
        assert status.logged_in is True
        assert status.can_get_link is True
        assert status.account_id == "102140588"
        assert status.total_bytes == 1000
        assert status.used_bytes == 400
        assert status.remain_bytes == 600

    async def test_needs_verify(self) -> None:
        """账号安全验证——最常见的「看起来正常但不能用」。"""
        driver = FakeDriver(
            link_error=DriverError("需安全验证", kind="auth_verify"),
            files=[CloudFile(remote_id="pc1", path="/a.mkv", name="a.mkv")],
        )
        status = await probe_account(driver)
        assert status.status == STATUS_NEEDS_VERIFY
        assert status.logged_in is True
        assert status.can_get_link is False
        # 提示必须说清「重新扫码无效」
        assert "重新扫码" in status.message

    async def test_credential_expired(self) -> None:
        """凭据失效。"""
        driver = FakeDriver(logged_in=False)
        status = await probe_account(driver)
        assert status.status == STATUS_ERROR
        assert status.logged_in is False
        # 文案按驱动走——不能写死「115」（光鸭用户看到会莫名其妙）
        assert "凭据已失效" in status.message
        assert "重新" in status.message

    async def test_rate_limited(self) -> None:
        driver = FakeDriver(
            link_error=DriverError("限流", kind="rate_limit"),
            files=[CloudFile(remote_id="pc1", path="/a.mkv", name="a.mkv")],
        )
        status = await probe_account(driver)
        assert status.status == STATUS_ERROR
        assert "限流" in status.message

    async def test_space_info_failure_does_not_break(self) -> None:
        """容量拿不到不影响整体判定——少一项信息而已。"""
        driver = FakeDriver(
            space_error=RuntimeError("space failed"),
            files=[CloudFile(remote_id="pc1", path="/a.mkv", name="a.mkv")],
        )
        status = await probe_account(driver)
        assert status.status == STATUS_OK
        assert status.total_bytes == 0

    async def test_empty_drive_skips_link_check(self) -> None:
        """网盘里没文件 → 不测直链（没样本可测），仍算可用。"""
        driver = FakeDriver(files=[])
        status = await probe_account(driver)
        assert status.status == STATUS_OK
        assert status.can_get_link is False
        assert "没有文件" in status.message

    async def test_uses_real_pickcode(self) -> None:
        """**必须用真实存在的 pickcode**——用假 id 会被判「文件不存在」，
        测不出「账号需安全验证」这种真实故障。"""
        driver = FakeDriver(
            files=[CloudFile(remote_id="pc_real", path="/a.mkv", name="a.mkv")]
        )
        await probe_account(driver)
        assert driver.link_calls == ["pc_real"]

    async def test_falls_back_to_second_level(self) -> None:
        """根目录只有目录时，往下一层找文件。"""
        driver = FakeDriver(
            files=[CloudFile(remote_id="99", path="/sub", name="sub", is_dir=True)]
        )
        # 让 list_dir("/sub") 返回一个文件
        original = driver.list_dir

        async def list_dir(path: str) -> list[CloudFile]:
            if path == "/sub":
                return [CloudFile(remote_id="pc_nested", path="/sub/a.mkv", name="a.mkv")]
            return await original(path)

        driver.list_dir = list_dir  # type: ignore[method-assign]
        await probe_account(driver)
        assert driver.link_calls == ["pc_nested"]
