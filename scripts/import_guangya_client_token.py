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
import base64
import binascii
import json
import logging
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

logger = logging.getLogger(__name__)


def log(msg: str) -> None:
    """输出运行日志（走 logging，stdout 与旧版 print 行为一致）。"""
    logger.info(msg)


def read_token_input() -> str:
    """读取 token 输入（环境变量 / base64 / 文件）。

    Returns:
        str: token 的原始 JSON 文本；没给任何输入时返回空串。

    Raises:
        OSError: 读取 token 文件失败。
        ValueError: base64 载荷解码失败。
    """
    raw = os.environ.get("GUANGYA_CLIENT_TOKEN", "")
    b64 = os.environ.get("GUANGYA_CLIENT_TOKEN_B64", "")
    if b64:
        try:
            raw = base64.b64decode(b64).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise ValueError(f"GUANGYA_CLIENT_TOKEN_B64 不是合法的 base64：{exc}") from exc
    if not raw:
        path = os.environ.get("GUANGYA_CLIENT_TOKEN_FILE", "")
        if path:
            try:
                with open(path, encoding="utf-8") as handle:
                    raw = handle.read()
            except OSError as exc:
                raise OSError(f"读取 token 文件失败（{path}）：{exc}") from exc
    return raw


async def main() -> int:
    """把客户端 token 导入指定光鸭驱动的凭据覆盖层。

    Returns:
        int: 0 表示成功；1 表示参数缺失、输入不可解析、驱动不存在或类型不符。
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)

    raw_driver_id = os.environ.get("MEDIALAB_DRIVER_ID", "").strip()
    if not raw_driver_id:
        log("❌ 需要 MEDIALAB_DRIVER_ID")
        return 1
    try:
        driver_id = int(raw_driver_id)
    except ValueError as exc:
        log(f"❌ MEDIALAB_DRIVER_ID 必须是数字：{exc}")
        return 1
    if driver_id <= 0:
        log(f"❌ MEDIALAB_DRIVER_ID 必须是正整数，实际是 {driver_id}")
        return 1

    try:
        raw = read_token_input()
    except (OSError, ValueError) as exc:
        log(f"❌ {exc}")
        return 1
    if not raw:
        log("❌ 需要 GUANGYA_CLIENT_TOKEN 或 GUANGYA_CLIENT_TOKEN_FILE")
        return 1
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        log(f"❌ token 输入不是合法 JSON：{exc}")
        return 1
    if not isinstance(payload, dict):
        log(f"❌ token 输入必须是 JSON 对象，实际是 {type(payload).__name__}")
        return 1

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

            raw_expires_at = payload.get("expires_at")
            if raw_expires_at is None:
                log("❌ token 输入里缺少 expires_at")
                return 1
            try:
                expires_at = int(raw_expires_at)
            except (TypeError, ValueError) as exc:
                log(f"❌ token 输入里的 expires_at 必须是整数：{exc}")
                return 1
            if expires_at <= 0:
                log(f"❌ token 输入里的 expires_at 必须是正整数时间戳，实际是 {expires_at}")
                return 1

            cred.client_access_token = str(payload.get("access_token") or "")
            cred.client_expires_at = expires_at
            cred.client_device_id = str(payload.get("device_id") or "")
            # **续期钥匙**：有它驱动就能自己刷新（不再每 2 小时回客户端库解密）
            cred.client_refresh_token = str(payload.get("refresh_token") or "")
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
            f"sub={payload.get('sub')} expires_at={expires_at} "
            f"token_len={len(payload.get('access_token') or '')} "
            f"refresh={'有' if cred.client_refresh_token else '无（过期后只能回退 Web）'}"
        )
        return 0
    finally:
        await dispose_db()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
