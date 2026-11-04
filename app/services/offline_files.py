"""离线下载的**纯逻辑**部分：token 生成 / 校验、路径安全、URL 拼接。

**为什么与 HTTP 层分开**：本模块**不 import fastapi**——项目分层约束
（``app/domain`` → ``app/core`` → ``app/db`` → ``app/adapters`` →
``app/services`` → ``app/api``）规定 services 层不得依赖 fastapi
（见 ``tests/unit/test_layering.py``）。

HTTP handler 在 :mod:`app.api.routes.offline_files`。

**鉴权模型**：光鸭的 ``cloud_create_task(url, parent_id)`` **只收 URL，
不支持自定义 Header**——所以鉴权只能做在 URL 里。

用 **每文件一个 token**（``HMAC(secret, 相对路径)`` 的十六进制前 32 位）：

- 单个文件的 URL 泄露只影响那一个文件（不是「一把钥匙开所有文件」）
- token 与路径**绑定** → 无法把 token 挪到别的路径上（防目录穿越）
- 校验用 ``hmac.compare_digest``（常量时间，防时序攻击）
"""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from urllib.parse import quote, unquote

#: 路由前缀。**必须**在 SPA 兜底之前挂载（见 ``app/main.py``）。
URL_PREFIX = "/offline-files"

#: token 的十六进制长度
TOKEN_LENGTH = 32


def _secret_bytes(secret: str) -> bytes:
    return secret.encode("utf-8")


def make_token(secret: str, rel_path: str) -> str:
    """算某个相对路径的访问 token。

    Args:
        secret: HMAC 密钥（来自配置）。
        rel_path: 相对根目录的路径（**必须与请求时用的完全一致**，
            含分隔符形式）。

    Returns:
        token 十六进制串（32 字符）。
    """
    digest = hmac.new(
        _secret_bytes(secret), rel_path.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return digest[:TOKEN_LENGTH]


def verify_token(secret: str, rel_path: str, token: str) -> bool:
    """校验 token（常量时间比较）。"""
    expected = make_token(secret, rel_path)
    return hmac.compare_digest(expected, token)


def decode_rel_path(raw: str) -> str:
    """把请求里的路径参数还原成**未编码**的相对路径。

    ⚠️ **光鸭的下载器有两条路径的编码方式不同**（实测 2026-11-04）：

    - 正常：``%E7%94%B5``（单次编码）—— 我们提交的形式
    - 重试：``%25E7%2594%25B5``（**双重编码**）—— 光鸭把 ``%`` 又编码了一遍

    双重编码的那批请求原本**全部 403**（token 校验失败）。
    故这里**多解一次**：``unquote`` 是幂等的，普通编码不受影响。
    """
    decoded = unquote(raw)
    if "%" in decoded:
        decoded = unquote(decoded)
    return decoded


def safe_join(root: Path, rel_path: str) -> Path | None:
    """把相对路径拼到根目录下，并确保结果**仍在根目录内**。

    防目录穿越：``../`` 之类的相对路径 resolve 后会跑出根目录。

    Returns:
        拼好的绝对路径；越界或非法时返回 ``None``。
    """
    if not rel_path or rel_path.startswith("/"):
        return None
    candidate = (root / rel_path).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def build_public_url(public_base: str, secret: str, rel_path: str) -> str:
    """给某个相对路径拼出公网 URL（含 token）。

    ⚠️ **token 按「未编码」的相对路径算**——服务端拿到的是解码后的
    path 参数，两者必须一致。URL 里只对路径做百分号编码。
    """
    base = public_base.rstrip("/")
    token = make_token(secret, rel_path)
    return f"{base}{URL_PREFIX}/{token}/{quote(rel_path)}"


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """解析 ``Range: bytes=start-end``。

    支持三种写法：
    - ``bytes=0-99``   → 前 100 字节
    - ``bytes=100-``   → 从第 100 字节到末尾
    - ``bytes=-100``   → **最后** 100 字节（后缀范围）

    Returns:
        ``(start, end)``（闭区间）；不支持的格式返回 ``None``。
    """
    if not header or not header.startswith("bytes="):
        return None
    spec = header[6:].strip()
    if "," in spec:  # 多段 Range 不支持
        return None
    start_s, _, end_s = spec.partition("-")
    try:
        if not start_s and end_s:
            # 后缀范围：最后 N 字节
            suffix = int(end_s)
            if suffix <= 0:
                return None
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    if start >= size:
        return None
    end = min(end, size - 1)
    if end < start:
        return None
    return start, end


__all__ = [
    "TOKEN_LENGTH",
    "URL_PREFIX",
    "build_public_url",
    "decode_rel_path",
    "make_token",
    "parse_range",
    "safe_join",
    "verify_token",
]
