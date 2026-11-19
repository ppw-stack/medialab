"""导出光鸭 macOS 客户端的登录 token（只读，本机运行）。

**为什么需要**：服务端按 access_token 的 ``aud``（客户端 clientId）判定
「是不是客户端」。客户端 token 才能取非会员的大文件直链；Web 扫码 token
对超过阈值的文件返回 ``code=254``（2026-11-19 主盘/小号实测）。

**加密算法**（逆向自 ``@xbase/electron_xl_analysis_kit`` 的
``EncryptionKeyUtility`` + ``electron_auth_kit`` 的调用点）：

    AES-128-CBC / PKCS7，**固定 key + 固定 IV**（不绑设备、不绑 Keychain）

密钥以 8 段字符串在代码里打散，拼回来是：

    key = "644f8652348d46e483cbdd298a28e9d8"   (16 字节)
    iv  = "8d752ee1e3af4daa963200cd53909868"   (16 字节)

输出（stdout 或 ``--out``）为一行的 JSON，**不含任何密钥**：

    {"sub": "...", "device_id": "...", "access_token": "...",
     "refresh_token": "...", "expires_at": 1795070050, "source": "guangya-mac-client"}

用法::

    uv run python scripts/export_guangya_client_token.py --summary
    uv run python scripts/export_guangya_client_token.py > /tmp/gytoken.json

安全边界：只读客户端自己的数据库；不刷新 token、不写回、不碰登录态。
"""

from __future__ import annotations

import argparse
import base64
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

#: 横向产品 clientId（沙盒外的客户端定值，服务端按 token 的 aud 认人）
MAC_CLIENT_ID = "al9eLEtLE7_pp8GR"

#: 客户端凭据库的 AES-128-CBC 固定参数（见模块 docstring）
AES_KEY_HEX = "644f8652348d46e483cbdd298a28e9d8"
AES_IV_HEX = "8d752ee1e3af4daa963200cd53909868"

DEFAULT_ROOT = Path.home() / "Library/Application Support/Guangyapan/envs/production"


def decrypt_blob(blob: bytes) -> bytes:
    """AES-128-CBC/PKCS7 解密客户端凭据 blob。"""
    decryptor = Cipher(
        algorithms.AES(bytes.fromhex(AES_KEY_HEX)),
        modes.CBC(bytes.fromhex(AES_IV_HEX)),
    ).decryptor()
    plain = decryptor.update(blob) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(plain) + unpadder.finalize()


def read_device_id(root: Path) -> str:
    """客户端设备 id（``app-lifecycle/state.json``）。"""
    state = root.parent.parent / "app-lifecycle/state.json"
    if not state.exists():
        return ""
    try:
        return str(json.loads(state.read_text()).get("deviceId") or "")
    except (ValueError, OSError):
        return ""


def jwt_exp(token: str) -> int:
    """从 JWT 里取 ``exp``（拿不到就返回 0）。"""
    parts = token.split(".")
    if len(parts) != 3:
        return 0
    seg = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(seg))
    except (ValueError, TypeError):
        return 0
    return int(payload.get("exp") or 0)


def load_credentials(db_path: Path, sub: str | None) -> dict:
    """读 ``current_sub``（或指定 sub）的凭据并解密。"""
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        current = ""
        for key, value in c.execute("select key, value from keyValue"):
            if key == "current_sub":
                current = value
        target = sub or current
        if not target:
            raise SystemExit("客户端没有 current_sub（未登录？）")
        row = c.execute(
            "select sub, expiresAt, credentials from credentials2 where sub=?",
            (target,),
        ).fetchone()
        if row is None:
            raise SystemExit(f"credentials2 里没有 sub={target}")
        _sub, expires_at_ms, blob = row
        obj = json.loads(decrypt_blob(blob).decode("utf-8"))
    finally:
        c.close()

    token = str(obj.get("access_token") or "")
    if not token:
        raise SystemExit("解密结果里没有 access_token")
    # 客户端存的是毫秒级 expiresAt；token 自己带 exp（更准）→ 优先 JWT
    exp = jwt_exp(token) or int(expires_at_ms or 0) // 1000
    return {
        "sub": target,
        "device_id": read_device_id(DEFAULT_ROOT),
        "access_token": token,
        "refresh_token": str(obj.get("refresh_token") or ""),
        "expires_at": exp,
        "source": "guangya-mac-client",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--credentials-db",
        default=str(DEFAULT_ROOT / "xbase/user/account" / MAC_CLIENT_ID / "Credentials.db"),
        help="客户端 Credentials.db 路径",
    )
    parser.add_argument("--sub", default="", help="指定账号 sub（默认 current_sub）")
    parser.add_argument("--out", default="", help="输出文件（默认 stdout）")
    parser.add_argument("--summary", action="store_true", help="只打印摘要，不输出 token")
    args = parser.parse_args()

    db_path = Path(args.credentials_db)
    if not db_path.exists():
        raise SystemExit(f"找不到客户端凭据库：{db_path}")

    result = load_credentials(db_path, args.sub or None)
    if args.summary:
        print(
            "sub={sub} device_id={device_id} token_len={tl} expires_at={exp} ({bj})".format(
                sub=result["sub"],
                device_id=result["device_id"],
                tl=len(result["access_token"]),
                exp=result["expires_at"],
                bj=datetime.fromtimestamp(result["expires_at"]).strftime("%Y-%m-%d %H:%M:%S")
                if result["expires_at"]
                else "unknown",
            )
        )
        return 0

    text = json.dumps(result, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text)
        print(f"已写入 {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
