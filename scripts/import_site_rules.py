#!/usr/bin/env python3
"""从 MoviePilot-Resources 的加密站点索引包生成 ``rules/indexers.json``。

用法::

    MOVIEPILOT_RULES_FERNET_KEY=<Fernet 密钥> \
        python scripts/import_site_rules.py \
        --resource-dir /path/to/MoviePilot-Resources/resources.v3

流程：

1. 在 ``sites.*.so/.pyd`` 里扫描 zlib 字符串表，取出 Fernet 密钥；
2. 用密钥解密 ``user.sites.v3.bin``（pickle 协议 5）；
3. 安全反序列化（只允许内建类型与 ``ruamel.yaml`` 标量）并写入 JSON。

**密钥不写进仓库**：MoviePilot-Resources 随包分发的 Fernet 密钥通过环境变量
``MOVIEPILOT_RULES_FERNET_KEY`` 传入（未设置时只走二进制扫描路径）。仓库里
出现过的字面量密钥一律视为已泄漏，不要复用。

规则包属于 MoviePilot-Resources（闭源分发），本项目自用；不要把生成物
再作为资源包对外发布。
"""

from __future__ import annotations

import argparse
import importlib
import io
import json
import logging
import os
import pickle
import re
import zlib
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("medialab.import_site_rules")

#: 已知密钥从环境变量读取（MoviePilot-Resources v2/v3 各平台构建共用）。
#:
#: **不要把密钥写进源码**——仓库里出现过的字面量密钥都视为已泄漏。轮换后由
#: 下面的扫描逻辑重新提取，无需改代码。
KNOWN_KEY_ENV = "MOVIEPILOT_RULES_FERNET_KEY"


def _known_key() -> bytes | None:
    """读取环境变量里的已知密钥；未配置时返回 None。"""
    raw = os.environ.get(KNOWN_KEY_ENV, "").strip()
    return raw.encode() if raw else None


_ALLOWED_BUILTINS = {
    "dict", "list", "tuple", "set", "frozenset", "str", "int", "float",
    "bool", "bytes", "bytearray", "complex", "object", "NoneType",
}


class _SafeUnpickler(pickle.Unpickler):
    """只允许内建容器与 ruamel.yaml 标量类型。"""

    def find_class(self, module: str, name: str) -> object:
        if module == "builtins" and name in _ALLOWED_BUILTINS:
            return super().find_class(module, name)
        if module.startswith("ruamel.yaml"):
            return getattr(importlib.import_module(module), name)
        raise pickle.UnpicklingError(f"blocked {module}.{name}")


def _scan_keys(binary: Path) -> list[bytes]:
    """从压缩字符串表里提取所有 44 字符的 Fernet 候选密钥。"""
    data = binary.read_bytes()
    pattern = re.compile(rb"[A-Za-z0-9_-]{43}=")
    found: list[bytes] = []
    for offset in range(len(data) - 2):
        if data[offset] != 0x78 or data[offset + 1] not in (0x01, 0x5E, 0x9C, 0xDA):
            continue
        try:
            out = zlib.decompressobj().decompress(data[offset: offset + 65536], 65536)
        except zlib.error:
            continue
        found.extend(match.group(0) for match in pattern.finditer(out or b""))
    return found


def _resolve_key(resource_dir: Path, token: bytes) -> bytes:
    """用候选密钥逐个试解，返回真正能解开该 token 的那一把。"""
    candidates: list[bytes] = []
    known = _known_key()
    if known:
        candidates.append(known)
    for binary in sorted(resource_dir.glob("sites.*")):
        if binary.suffix in {".so", ".pyd"}:
            candidates.extend(_scan_keys(binary))
    for key in candidates:
        try:
            Fernet(key).decrypt(token)
        except (InvalidToken, ValueError, TypeError):
            continue
        # **不打印密钥本身**——只说明定位成功，避免把凭据写进终端日志。
        logger.info("已定位可解密的 Fernet 密钥")
        return key
    raise SystemExit("未找到可解密的 Fernet 密钥")


def main() -> int:
    """执行导入流程。

    :return: 进程退出码：0 成功，2 输入文件缺失
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--resource-dir", required=True, type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("app/adapters/sites/indexer/rules/indexers.json"),
    )
    parser.add_argument("--version", default="v3", choices=["v2", "v3"])
    args = parser.parse_args()

    blob = args.resource_dir / f"user.sites.{args.version}.bin"
    if not blob.exists():
        logger.error("找不到 %s", blob)
        return 2

    token = blob.read_bytes().strip()
    key = _resolve_key(args.resource_dir, token)
    raw = Fernet(key).decrypt(token)
    payload = _SafeUnpickler(io.BytesIO(raw)).load()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info(
        "写出 %s：version=%s 站点数=%s",
        args.output,
        payload.get("version"),
        len(payload.get("indexers") or {}),
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
