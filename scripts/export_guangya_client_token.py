"""导出光鸭 macOS 客户端的登录 token（只读，本机运行）。

**为什么需要**：服务端按 access_token 的 ``aud``（客户端 clientId）判定
「是不是客户端」。客户端 token 才能取非会员的大文件直链；Web 扫码 token
对超过阈值的文件返回 ``code=254``（2026-11-19 主盘/小号实测）。

**加密算法**（逆向自 ``@xbase/electron_auth_kit`` 的
``EncryptionKeyUtility``，8 段字符串打散）：

    AES-128-CBC / PKCS7，**固定 key + 固定 IV**（不绑设备、不绑 Keychain）

**key / iv 不落仓库**：它是客户端 bundle 里 ``EncryptionKeyUtility`` 把
8 段字符串打散后拼出来的**凭据类敏感材料**——本脚本在**本机**从已安装
客户端的 ``app.asar`` 动态提取（见 :func:`load_aes_params`）；客户端升级后
bundle 路径变了，可用 ``GUANGYA_AES_KEY_HEX`` / ``GUANGYA_AES_IV_HEX``
环境变量成对覆盖。

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
import os
import re
import sqlite3
import struct
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

#: 横向产品 clientId（沙盒外的客户端定值，服务端按 token 的 aud 认人）
MAC_CLIENT_ID = "al9eLEtLE7_pp8GR"

#: AES 参数的**环境变量**兜底（必须成对提供）——key/iv 不写进仓库
AES_KEY_ENV = "GUANGYA_AES_KEY_HEX"
AES_IV_ENV = "GUANGYA_AES_IV_HEX"

DEFAULT_ROOT = Path.home() / "Library/Application Support/Guangyapan/envs/production"

#: 客户端安装包里的 ``app.asar``（key/iv 的唯一来源）
DEFAULT_ASAR = Path("/Applications/光鸭云盘.app/Contents/Resources/app.asar")

#: bundle 里存放凭据库加密 ``EncryptionKeyUtility`` 的包名（按顺序找）
AES_BUNDLE_PACKAGES = ("@xbase/electron_auth_kit", "@xbase/electron_account_kit")

#: ``EncryptionKeyUtility`` 的两个取值函数（key / IV）
AES_KEY_FUNC = "getAesKeyString"
AES_IV_FUNC = "getAESIVString"

#: 8 段打散的赋值片段（形如 ``e[0]="644f",e[1]="8652",...``）
_SEGMENT_RE = re.compile(r"\[\s*(\d+)\s*\]\s*=\s*\"([0-9a-fA-F]+)\"")


def _default_asar() -> Path:
    """定位客户端的 ``app.asar``（环境变量 > 固定路径 > /Applications 扫描）。"""
    env = os.environ.get("GUANGYA_APP_ASAR", "").strip()
    if env:
        return Path(env)
    if DEFAULT_ASAR.exists():
        return DEFAULT_ASAR
    for candidate in sorted(Path("/Applications").glob("*.app/Contents/Resources/app.asar")):
        name = candidate.parts[-4] if len(candidate.parts) >= 4 else ""
        if "光鸭" in name or "guangya" in name.lower():
            return candidate
    return DEFAULT_ASAR


def _read_asar_tree(asar_path: Path) -> tuple[dict, int]:
    """读 asar 头部目录树。

    Returns:
        tuple: (目录树 dict, 数据段起始偏移)
    """
    with asar_path.open("rb") as fh:
        head = fh.read(8)
        if len(head) < 8:
            raise ValueError(f"asar 头太短：{asar_path}")
        try:
            header_size = struct.unpack("<I", head[4:8])[0]
            fh.seek(8)
            raw = fh.read(header_size)
            json_len = struct.unpack("<I", raw[4:8])[0]
        except struct.error as exc:
            raise ValueError(f"asar 头解析失败：{asar_path}（{exc}）") from exc
    tree = json.loads(raw[8 : 8 + json_len].decode("utf-8"))
    return tree, 8 + header_size


def _iter_asar_entries(tree: dict) -> Iterator[tuple[str, dict]]:
    """遍历 asar 目录树的文件条目（路径以 ``/`` 开头）。"""

    def walk(node: dict, prefix: str) -> Iterator[tuple[str, dict]]:
        for name, meta in node.get("files", {}).items():
            path = f"{prefix}/{name}"
            if "files" in meta:
                yield from walk(meta, path)
            else:
                yield path, meta

    yield from walk(tree, "")


def _read_asar_file(asar_path: Path, meta: dict, data_offset: int) -> bytes:
    """按 asar 条目元数据读出文件内容。"""
    with asar_path.open("rb") as fh:
        fh.seek(data_offset + int(meta["offset"]))
        return fh.read(int(meta["size"]))


def _segments_from_bundle(source: str, func_name: str) -> str:
    """从 bundle 源码里取出 8 段打散的十六进制串（按 ``e[0]..e[7]`` 拼接）。

    Raises:
        ValueError: 找不到该函数，或分段不完整。
    """
    match = re.search(
        re.escape(func_name) + r"\s*=\s*function\s*\(\s*\)\s*\{(.*?)\}", source, re.S
    )
    if match is None:
        raise ValueError(f"bundle 里找不到 {func_name}")
    segments = {int(i): v.lower() for i, v in _SEGMENT_RE.findall(match.group(1))}
    if not segments or sorted(segments) != list(range(len(segments))):
        raise ValueError(f"{func_name} 的分段不完整：{sorted(segments)}")
    return "".join(segments[i] for i in range(len(segments)))


def load_aes_params(asar_path: Path) -> tuple[str, str]:
    """从已安装客户端的 ``app.asar`` 动态提取 AES key / IV。

    来源：``@xbase/electron_auth_kit`` 的 ``EncryptionKeyUtility``
    ——8 段字符串打散后按 ``e[0]..e[7]`` 顺序拼接。

    Returns:
        tuple: (key hex, iv hex)

    Raises:
        FileNotFoundError: 找不到 ``app.asar``。
        ValueError: bundle 结构变了，提取不出 key/iv。
    """
    if not asar_path.exists():
        raise FileNotFoundError(f"找不到客户端 app.asar：{asar_path}")
    tree, data_offset = _read_asar_tree(asar_path)
    candidates = [
        (path, meta)
        for path, meta in _iter_asar_entries(tree)
        if any(pkg in path for pkg in AES_BUNDLE_PACKAGES)
        and path.endswith((".js", ".mjs"))
        and int(meta.get("size", 0)) <= 8 * 1024 * 1024
    ]
    # 包优先级（auth_kit 优先）+ production 优先
    candidates.sort(
        key=lambda item: (
            next(
                (i for i, pkg in enumerate(AES_BUNDLE_PACKAGES) if pkg in item[0]),
                len(AES_BUNDLE_PACKAGES),
            ),
            "production" not in item[0],
            item[0],
        )
    )
    for _bundle_path, meta in candidates:
        source = _read_asar_file(asar_path, meta, data_offset).decode("utf-8", "replace")
        if AES_KEY_FUNC not in source:
            continue
        try:
            key = _segments_from_bundle(source, AES_KEY_FUNC)
            iv = _segments_from_bundle(source, AES_IV_FUNC)
        except ValueError:
            continue
        return key, iv
    raise ValueError(f"app.asar 里提取不到 AES key/iv：{asar_path}")


def resolve_aes_params(asar_path: Path) -> tuple[str, str]:
    """取 AES key / IV：环境变量优先，否则从客户端 bundle 动态提取。

    Raises:
        ValueError: 环境变量只给了一半，或 bundle 结构变了。
    """
    env_key = os.environ.get(AES_KEY_ENV, "").strip()
    env_iv = os.environ.get(AES_IV_ENV, "").strip()
    if env_key or env_iv:
        if not (env_key and env_iv):
            raise ValueError(f"{AES_KEY_ENV} / {AES_IV_ENV} 必须成对提供")
        key, iv = env_key.lower(), env_iv.lower()
    else:
        key, iv = load_aes_params(asar_path)
    if len(key) != 32 or len(iv) != 32:
        raise ValueError(f"AES key/iv 长度不对（应为 32 位 hex）：{len(key)}/{len(iv)}")
    return key, iv


def decrypt_blob(blob: bytes, key_hex: str, iv_hex: str) -> bytes:
    """AES-128-CBC/PKCS7 解密客户端凭据 blob。

    Args:
        blob: 凭据库里的密文。
        key_hex: AES key（32 位 hex）。
        iv_hex: AES IV（32 位 hex）。
    """
    decryptor = Cipher(
        algorithms.AES(bytes.fromhex(key_hex)),
        modes.CBC(bytes.fromhex(iv_hex)),
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


def load_credentials(db_path: Path, sub: str | None, key_hex: str, iv_hex: str) -> dict:
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
        obj = json.loads(decrypt_blob(blob, key_hex, iv_hex).decode("utf-8"))
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
    """导出客户端 token（stdout 或 ``--out``）。

    Returns:
        int: 0 表示成功；失败以 ``SystemExit``（非零码）结束。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--credentials-db",
        default=str(DEFAULT_ROOT / "xbase/user/account" / MAC_CLIENT_ID / "Credentials.db"),
        help="客户端 Credentials.db 路径",
    )
    parser.add_argument("--sub", default="", help="指定账号 sub（默认 current_sub）")
    parser.add_argument("--out", default="", help="输出文件（默认 stdout）")
    parser.add_argument("--summary", action="store_true", help="只打印摘要，不输出 token")
    parser.add_argument(
        "--asar",
        default=str(_default_asar()),
        help="客户端 app.asar 路径（AES key/iv 的来源）",
    )
    args = parser.parse_args()

    db_path = Path(args.credentials_db)
    if not db_path.exists():
        raise SystemExit(f"找不到客户端凭据库：{db_path}")

    try:
        key_hex, iv_hex = resolve_aes_params(Path(args.asar))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"提取 AES key/iv 失败：{exc}") from exc

    result = load_credentials(db_path, args.sub or None, key_hex, iv_hex)
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
