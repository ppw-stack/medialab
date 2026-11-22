"""存储源账号状态。

回答三个问题：

1. **凭据还有效吗** —— ``login_status()``
2. **账号是谁 / 容量多少** —— ``user_space_info()``
3. **能不能真的换到直链** —— ``download_url()``（这一步最关键：前两步通过
   但换直链被网盘的账号安全验证挡住，是常见情况）

「测连」要区分**三种结果**，不能笼统报「失败」：

- ``ok`` —— 全部正常
- ``needs_verify`` —— 登录态正常，但换直链被账号安全验证挡住
  （需在手机 APP 里验证；**重新扫码无效**）
- ``error`` —— 其它问题（凭据失效 / 限流 / 网络）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AccountStatus:
    """账号状态（测连结果）。"""

    #: 凭据是否有效（``login_status()``）
    logged_in: bool
    #: 账号 id（UID）
    account_id: str = ""
    #: 总容量（字节）
    total_bytes: int = 0
    #: 已用容量（字节）
    used_bytes: int = 0
    #: 剩余容量（字节）
    remain_bytes: int = 0
    #: 能否换到直链（**最关键的一项**）
    can_get_link: bool = False
    #: 状态：``ok`` / ``needs_verify`` / ``no_credential`` / ``error``
    status: str = "error"
    #: 可读的说明
    message: str = ""

    @property
    def healthy(self) -> bool:
        """一切正常。"""
        return self.status == "ok"


#: 状态常量
STATUS_OK = "ok"
STATUS_NEEDS_VERIFY = "needs_verify"
STATUS_NO_CREDENTIAL = "no_credential"
STATUS_ERROR = "error"


async def probe_account(driver: Any) -> AccountStatus:
    """探测账号状态。

    依次做三件事（**每件失败都不中断后续**——要尽量多给出信息）：

    1. ``login_status()`` —— 凭据有效性
    2. ``user_space_info()`` —— 账号 id 与容量
    3. ``get_direct_link()`` —— 能否真正换到直链

    Args:
        driver: 驱动实例。

    Returns:
        账号状态。
    """
    logged_in = False
    account_id = ""
    total = used = remain = 0
    can_link = False
    status = STATUS_ERROR
    message = ""

    # 1. 登录态
    try:
        logged_in = await driver.check_login()
    except Exception:
        logged_in = False

    if not logged_in:
        return AccountStatus(
            logged_in=False,
            status=STATUS_ERROR,
            message=f"{_driver_label(driver)}凭据已失效，请重新登录",
        )

    # 2. 账号信息与容量
    try:
        info = await _fetch_space_info(driver)
        account_id = info.get("account_id", "")
        total = info.get("total_bytes", 0)
        used = info.get("used_bytes", 0)
        remain = info.get("remain_bytes", 0)
    except Exception:
        # 容量拿不到不影响整体判定（只是少一项信息）
        pass

    # 3. 换直链——这一步最关键
    #    **必须用真实存在的 fileId**：用假 id 会被判「文件不存在」，
    #    测不出「账号需安全验证」这种真实故障。
    try:
        file_id = await _pick_any_file(driver)
        if file_id is None:
            can_link = False
            status = STATUS_OK
            message = "账号正常（网盘内没有文件，未验证直链）"
        else:
            await driver.get_direct_link(file_id, _probe_user_agent(driver))
            can_link = True
            status = STATUS_OK
            message = "账号正常，可正常获取直链"
    except Exception as exc:
        kind = getattr(exc, "kind", "")
        label = _driver_label(driver)
        if kind == "auth_verify":
            status = STATUS_NEEDS_VERIFY
            message = (
                f"登录态正常，但换直链被{label}账号安全验证挡住："
                "请用手机 APP 完成验证（大约每天一次）。重新扫码登录无效。"
            )
        elif kind == "auth":
            status = STATUS_ERROR
            message = f"{label}凭据已失效，请重新登录"
        elif kind == "rate_limit":
            status = STATUS_ERROR
            message = f"{label}请求被限流，请稍后重试"
        else:
            status = STATUS_ERROR
            message = f"换直链失败：{exc}"

    return AccountStatus(
        logged_in=logged_in,
        account_id=account_id,
        total_bytes=total,
        used_bytes=used,
        remain_bytes=remain,
        can_get_link=can_link,
        status=status,
        message=message,
    )


def _driver_label(driver: Any) -> str:
    """驱动的显示名（错误文案用）。"""
    name = getattr(driver, "name", "")
    if name == "guangya":
        return "光鸭盘 "
    return ""


async def _pick_any_file(driver: Any) -> str | None:
    """找一个真实存在的文件 id（用于测直链）。

    遍历根目录取第一个文件；根目录全是目录时再往下一层。
    返回 None 表示网盘里确实没有文件。

    光鸭接口**只接受 fileId**，得用 ``list_dir_by_cid("")`` 列根目录，
    再用自己的 fileId 下钻。
    """
    lister = getattr(driver, "list_dir_by_cid", None)
    if lister is None:
        return await _pick_any_file_by_path(driver)
    return await _pick_any_file_by_cid(driver)


async def _pick_any_file_by_path(driver: Any) -> str | None:
    """按路径找文件（驱动只实现 ``list_dir`` 时的兜底）。"""
    try:
        entries = await driver.list_dir("/")
    except Exception:
        return None

    files = [e for e in entries if not e.is_dir]
    if files:
        return str(files[0].remote_id)

    # 根目录只有目录 → 下一层
    for entry in entries[:5]:
        if not entry.is_dir:
            continue
        try:
            children = await driver.list_dir(entry.path)
        except Exception:
            continue
        for child in children:
            if not child.is_dir:
                return str(child.remote_id)
    return None


async def _pick_any_file_by_cid(driver: Any) -> str | None:
    """按 fileId 找文件（光鸭等只接受 id 的驱动）。

    **递归下钻**——光鸭的目录树可能很深（实测：
    ``/影视/音乐/未分类/中塚武/专辑名/`` = 5~6 层），只下钻一层常常
    找不到文件，会误报「网盘内没有文件」。

    限深 8 层、每层最多看 8 个目录——避免在超大目录树上空转。
    """
    return await _pick_by_cid(driver, "", "/", depth=0)


async def _pick_by_cid(
    driver: Any, cid: str, path: str, *, depth: int
) -> str | None:
    """递归找第一个文件（限深）。"""
    if depth > 8:
        return None
    try:
        entries = await driver.list_dir_by_cid(cid, parent_path=path)
    except Exception:
        return None

    files = [e for e in entries if not e.is_dir]
    if files:
        return str(files[0].remote_id)

    for entry in entries[:8]:
        if not entry.is_dir:
            continue
        found = await _pick_by_cid(
            driver, entry.remote_id, entry.path, depth=depth + 1
        )
        if found:
            return found
    return None


def _probe_user_agent(driver: Any) -> str:
    """测连用的 UA。

    用一个固定的桌面 UA——换直链本身会绑 UA，但这里只是验证「能不能换」，
    不关心 UA 具体值。
    """
    from app.core.config import get_settings

    return get_settings().default_user_agent


async def _fetch_space_info(driver: Any) -> dict[str, Any]:
    """取账号 id 与容量。

    驱动实现了 ``get_space_info()`` 就用它（光鸭的容量接口是
    ``user_info()``）；没有该能力时只返回空信息。
    """
    getter = getattr(driver, "get_space_info", None)
    if getter is not None:
        try:
            result = await getter()
        except Exception:
            result = None
        if isinstance(result, dict):
            return result

    return {}
