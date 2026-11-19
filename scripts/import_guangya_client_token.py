"""把客户端 token 写入光鸭驱动的**覆盖层**（容器内运行）。

不覆盖 Web 凭据：``access_token`` / ``refresh_token`` 原样保留，只写
``client_access_token`` / ``client_expires_at`` / ``client_device_id``。
扫描、列目录、上传仍走 Web token；只有换直链时优先用客户端 token
（服务端按 token 的 ``aud`` 认客户端身份，非会员大文件只有它能过）。

输入（``GUANGYA_CLIENT_TOKEN`` 环境变量，或 ``--file``）::

    {"sub":"...","device_id":"...","access_token":"...",
     "refresh_token":"...","expires_at":1795070050,"source":"guangya-mac-client"}

用法（凭据不出容器）::

    ssh unraid "docker exec -i -e GUANGYA_CLIENT_TOKEN_B64=<base64> \\
        -e MEDIALAB_DRIVER_ID=5 medialab /opt/venv/bin/python - " \\
        < scripts/import_guangya_client_token.py

安全边界：只写这一个驱动的凭据行；不刷新、不登录、不碰其它数据。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.environ.get("MEDIALAB_APP_PATH", "/app"))

from app.adapters.drivers.guangya_credential import (
    SCHEME,
    decode_credential,
)
from app.core.config import get_settings
from app.db.models import Driver
from app.db.session import dispose_db, init_db, session_scope
from app.services.driver_service import store_credential
from sqlalchemy import select


def log(msg: str) -> None:
    print(msg, flush=True)


async def main() -> int:
    driver_id = int(os.environ.get("MEDIALAB_DRIVER_ID", "0") or 0)
    if not driver_id:
        log("❌ 需要 MEDIALAB_DRIVER_ID")
        return 1

    raw = os.environ.get("GUANGYA_CLIENT_TOKEN", "")
    b64 = os.environ.get("GUANGYA_CLIENT_TOKEN_B64", "")
    if b64:
        import base64
        raw = base64.b64decode(b64).decode("utf-8")
    if not raw:
        path = os.environ.get("GUANGYA_CLIENT_TOKEN_FILE", "")
        if path:
            with open(path, encoding="utf-8") as handle:
                raw = handle.read()
    if not raw:
        log("❌ 需要 GUANGYA_CLIENT_TOKEN 或 GUANGYA_CLIENT_TOKEN_FILE")
        return 1
    payload = json.loads(raw)

    settings = get_settings()
    init_db(settings)
    try:
        async with session_scope() as db:
            row = (
                (await db.execute(select(Driver).where(Driver.id == driver_id)))
                .scalars()
                .first()
            )
            if row is None:
                log(f"❌ 存储源 {driver_id} 不存在")
                return 1
            if row.type != "guangya":
                log(f"❌ 存储源 {driver_id} 不是光鸭（type={row.type}）")
                return 1

            cred = decode_credential(row.credential)
            if cred is None or not cred.access_token:
                log("❌ 该驱动还没有 Web 凭据——先扫码/短信登录一次，再导入客户端 token")
                return 1

            cred.client_access_token = str(payload.get("access_token") or "")
            cred.client_expires_at = int(payload.get("expires_at") or 0)
            cred.client_device_id = str(payload.get("device_id") or "")
            if not cred.client_access_token:
                log("❌ 输入里没有 access_token")
                return 1

            await store_credential(
                db,
                driver_id,
                cred.to_json(),
                scheme=SCHEME,
                invalidate_cache=False,
            )

        log(
            f"✅ 已写入驱动 {driver_id} 的客户端 token："
            f"sub={payload.get('sub')} expires_at={payload.get('expires_at')} "
            f"token_len={len(payload.get('access_token') or '')}"
        )
        return 0
    finally:
        await dispose_db()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
