"""光鸭探针补充验证（只读）。

1. 用 DB 里的凭据（新进程）验证登录态与 token 刷新链路健康
   —— 上一次探针触发过刷新但写回 DB 失败（锁竞争），若光鸭轮换了
   refresh_token，DB 里的旧值会失效 → 必须现在确认
2. 确认 ``fs_files(parent_id="*")`` 能否只列目录（res_type=2）
   —— 决定全量列表方案能否拿到「fileId → 目录名」映射
"""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Driver
from app.db.session import dispose_db, init_db, session_scope
from app.services.driver_factory import build_driver


async def main() -> None:
    settings = get_settings()
    init_db(settings)
    try:
        async with session_scope() as db:
            row = (
                await db.execute(select(Driver).where(Driver.type == "guangya"))
            ).scalars().first()
        if row is None:
            print("没有 guangya 驱动行")
            return
        print(f"存储源：id={row.id} name={row.name}")

        from app.services.redirect import _persist_refreshed_credential

        async def _on_refresh(cred: object) -> None:
            try:
                await _persist_refreshed_credential(row.id, cred)
                print(">>> token 已刷新并成功写回 DB")
            except Exception as exc:  # noqa: BLE001
                print(f">>> token 刷新写回失败：{exc}")

        driver = build_driver(
            row.type, row.credential, row.device_type, on_token_refresh=_on_refresh
        )

        # 1. 登录态（顺带触发 _ensure_token 的刷新判定）
        try:
            ok = await driver.check_login()
            print(f"check_login: {ok}")
        except Exception as exc:  # noqa: BLE001
            print(f"check_login 异常：{type(exc).__name__}: {exc}")
        try:
            space = await driver.get_space_info()
            print(f"get_space_info: {json.dumps(space, ensure_ascii=False)}")
        except Exception as exc:  # noqa: BLE001
            print(f"get_space_info 异常：{type(exc).__name__}: {exc}")

        # 2. res_type=2 只列目录
        try:
            payload = await driver._call(
                "fs_files",
                parent_id="*",
                page=0,
                page_size=1000,
                order_by=0,
                sort_type=0,
                file_types=[],
                res_type=2,
            )
            data = payload.get("data") or {}
            items = data.get("list") or []
            print(
                f"\nres_type=2（只列目录）: code={payload.get('code')} "
                f"返回条数={len(items)} total={data.get('total')}"
            )
            if items:
                sample = items[0]
                keep = {
                    k: sample.get(k)
                    for k in (
                        "fileId",
                        "fileName",
                        "parentId",
                        "fullParentIds",
                        "depth",
                        "resType",
                        "utime",
                    )
                }
                print(f"目录条目样例：{json.dumps(keep, ensure_ascii=False)}")
        except Exception as exc:  # noqa: BLE001
            print(f"只列目录探测异常：{type(exc).__name__}: {exc}")
    finally:
        await dispose_db()


if __name__ == "__main__":
    asyncio.run(main())
