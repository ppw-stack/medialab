"""探针：测出「一次分享」的文件数上限（生产实测）。

背景：2026-11-12 生产报错「光鸭创建分享失败：分享目录文件数量超过限制」
——说明**分享**的上限比**转存**的上限（500）小。

测法（零成本）：先用**不存在的 fileId** 调 ``share_create``——
若服务端**先校验数量**再校验 id 存在性，就能直接二分出上限，
且失败的请求**不会真的创建分享**（不占建分享配额）。

若服务端先校验 id（报「文件不存在」），再退回用真实文件 id 测。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from app.core.config import get_settings
from app.db.models import Driver
from app.db.session import dispose_db, init_db, session_scope
from app.services.driver_factory import build_driver
from sqlalchemy import select

SRC_DRIVER_ID = 3  # 小号1
FAKE_BASE = "196799999999999"


def _fake_ids(n: int) -> list[str]:
    """造 n 个不存在的 fileId（长度与真 id 一致）。"""
    return [f"{FAKE_BASE}{i:04d}" for i in range(n)]


async def try_share(driver: Any, ids: list[str]) -> tuple[str, str]:
    """试着分享这些 id，返回 ``(状态, 原始消息)``。

    状态：``ok`` / ``limit``（数量超限）/ ``invalid``（id 不存在）/ 其它。
    """
    try:
        info = await driver.create_share(ids, title="probe-limit")
    except Exception as exc:
        msg = str(exc)
        if "数量超过限制" in msg or ("数量" in msg and "限制" in msg):
            return "limit", msg
        if "不存在" in msg or "无效" in msg or "参数错误" in msg:
            return "invalid", msg
        return "other", msg
    # 真创建成功 → 立刻删掉，不留记录
    with contextlib.suppress(Exception):
        await driver.delete_share(info.share_id)
    return "ok", info.share_id


async def main() -> None:
    settings = get_settings()
    init_db(settings)
    try:
        async with session_scope() as db:
            row = (
                await db.execute(
                    select(Driver).where(Driver.id == SRC_DRIVER_ID)
                )
            ).scalar_one_or_none()
        if row is None:
            print("找不到源存储源")
            return
        driver = build_driver("guangya", row.credential, row.device_type)
        await driver._ensure_token()

        # ---------- ① 假 id：服务端先校验数量吗 ----------
        print("① 用 2000 个不存在的 id 试…")
        status, msg = await try_share(driver, _fake_ids(2000))
        print(f"   → {status}：{msg[:160]}")

        if status != "limit":
            print("   服务端先校验 id 存在性 —— 改用真实文件 id 测")
            await _probe_with_real_ids(driver)
            return

        # ---------- ② 二分找上限 ----------
        #
        # ⚠️ 「文件不存在」**不是**异常——它说明服务端**已经过了数量检查**
        # （先校验数量、再校验 id 存在性），故这一档算「数量可行」。
        lo, hi = 1, 2000  # lo 已知可行，hi 已知超限
        print("\n② 二分找上限…")
        while hi - lo > 1:
            mid = (lo + hi) // 2
            status, msg = await try_share(driver, _fake_ids(mid))
            print(f"   n={mid} → {status}")
            if status == "limit":
                hi = mid
            elif status in ("ok", "invalid"):
                # invalid = 数量检查已过（卡在 id 存在性）→ 数量可行
                lo = mid
            else:
                print(f"   意外：{msg[:200]}")
                break
            await asyncio.sleep(1)

        print(f"\n★ 一次分享的文件数上限 ≈ {lo}（{lo} 可行，{hi} 超限）")
    finally:
        await dispose_db()


async def _probe_with_real_ids(driver: Any) -> None:
    """退路：用账号里**真实存在**的文件 id 测上限（只读引用，随后删分享）。"""
    print("\n列账号根目录，找一个文件多的目录…")
    try:
        entries = await driver.list_entries("")
    except Exception as exc:
        print(f"列目录失败：{exc}")
        return
    print(f"根目录文件数={len(entries)}")
    # 用根目录下的文件（若有）作为分享对象
    ids = []
    for entry in await driver.list_dir_by_cid(""):
        if not entry.is_dir:
            ids.append(entry.remote_id)
    if not ids:
        print("根目录没有文件，无法测")
        return
    status, msg = await try_share(driver, ids)
    print(f"分享 {len(ids)} 个真实文件 → {status}：{msg[:200]}")


if __name__ == "__main__":
    asyncio.run(main())
