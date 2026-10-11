"""保存路径模板渲染（FR-2.3 / FR-3.3 / FR-5.2.5c）。

两条职责边界：

1. **模板只用于"生成新任务路径"**，绝不能用于推导删除目标（FR-5.2.5a）。
   删除目标必须来自下载器上报的文件清单。
2. 渲染结果必须做**路径清洗**：种子名来自站点，可能含 ``../``、绝对路径、
   控制字符、控制台零宽字符等。任何清洗失败都必须抛错而不是"尽力而为"——
   路径穿越是 FR-5.2.5c 要防的首要风险。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

#: 模板占位符
PLACEHOLDERS = frozenset({"site", "name", "category", "info_hash"})

#: 文件名中禁止的字符（Windows 最严格集，跨平台安全）
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
#: 路径穿越片段
_TRAVERSAL = re.compile(r"(^|[\\/])\.\.([\\/]|$)")
#: Windows 盘符（前缀必须是单字母 + 冒号 + 分隔符），``:`` 在 Linux/macOS
#: 文件名中合法，仅拒绝这种盘符形态而不是任意冒号。
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
#: 零宽 / 方向控制字符（钓鱼与显示混淆）。**不得**用裸 `-` 起始字符类，
#: 否则会连同连字符一起删除（曾把 ``example-pt`` 变成 ``examplept``）。
_INVISIBLE = re.compile("[\\u00ad\\u200b-\\u200f\\u202a-\\u202e\\u2060-\\u2064\\ufeff]")

#: 单段最大长度，避免超出文件系统上限
MAX_SEGMENT = 128
#: 单条路径最大总长
MAX_PATH_LEN = 4000


class PathRenderError(ValueError):
    """模板非法或渲染结果不安全。调用方必须跳过并告警（FR-2.3）。"""


@dataclass(frozen=True, slots=True)
class RenderedPath:
    """渲染结果。"""

    path: PurePosixPath
    #: 实际使用的模板（可能因非法字符被替换过）
    template: str


def sanitize_segment(value: str, *, fallback: str = "unnamed") -> str:
    """清洗单个路径段（站点名 / 分类名）。

    - 归一化 Unicode（NFKC），消除全角/半角混淆
    - 去除零宽与方向控制字符
    - 替换非法字符为 ``_``
    - 拒绝 ``.`` / ``..`` / 绝对路径 / 路径穿越
    - 折叠连续空白，去掉首尾空白与尾随点（Windows 不允许）
    """
    if value is None:
        raise PathRenderError("路径段为空")
    raw = unicodedata.normalize("NFKC", str(value))
    cleaned = _INVISIBLE.sub("", raw)
    cleaned = _ILLEGAL_CHARS.sub("_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(". ")

    if not cleaned:
        return fallback
    if cleaned in {".", ".."}:
        raise PathRenderError(f"非法路径段：{value!r}")
    if _TRAVERSAL.search(cleaned):
        raise PathRenderError(f"路径穿越尝试：{value!r}")
    if cleaned.startswith(("/", "\\")) or _WINDOWS_DRIVE.match(cleaned):
        raise PathRenderError(f"绝对路径或盘符：{value!r}")
    if len(cleaned) > MAX_SEGMENT:
        cleaned = cleaned[:MAX_SEGMENT].rstrip(". ") or fallback
    return cleaned


def sanitize_torrent_name(name: str, *, fallback: str = "unnamed") -> str:
    """清洗种子名 → 安全的单段目录名。

    种子名是攻击面最大的输入：来自站点 RSS，完全不可信。
    """
    return sanitize_segment(name, fallback=fallback)


def render_save_path(
    template: str,
    *,
    site: str,
    name: str,
    category: str = "cross-seed",
    info_hash: str = "",
) -> RenderedPath:
    """渲染保存路径（FR-2.3 路径模板）。

    支持占位符 ``{site}`` / ``{name}`` / ``{category}`` / ``{info_hash}``，
    以及纯字面量模板（如 ``/data/torrents/pt``）。

    模板本身也可能是用户误配的不可信输入（例如从站点配置直接粘贴），
    因此**模板同样要清洗**：非法占位符、路径穿越、绝对路径注入都要拒绝。
    """
    if not template or not template.strip():
        raise PathRenderError("保存路径模板为空")

    # 1) 校验并归一化模板：必须是纯 POSIX 绝对路径
    tpl = unicodedata.normalize("NFKC", template.strip())
    if _TRAVERSAL.search(tpl):
        raise PathRenderError(f"模板含路径穿越：{template!r}")
    if _INVISIBLE.search(tpl):
        tpl = _INVISIBLE.sub("", tpl)
    if not tpl.startswith("/"):
        raise PathRenderError(f"模板必须是绝对路径：{template!r}")
    if "\\" in tpl:
        raise PathRenderError(f"模板不得含反斜杠：{template!r}")
    if len(tpl) > MAX_PATH_LEN:
        raise PathRenderError("模板过长")

    # 2) 替换占位符（值先清洗再插入）
    values: dict[str, str] = {
        "site": sanitize_segment(site, fallback="unknown-site"),
        "category": sanitize_segment(category, fallback="cross-seed"),
        "name": sanitize_torrent_name(name),
        "info_hash": sanitize_segment(info_hash, fallback="") if info_hash else "",
    }

    # 3) 只允许已知占位符，避免 {__class__} 之类注入
    for match in re.finditer(r"\{([^{}]*)\}", tpl):
        key = match.group(1)
        if key not in values:
            raise PathRenderError(f"未知占位符 {{{key}}}（允许：{sorted(values)}）")

    rendered = tpl
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", value)

    if "{" in rendered:
        raise PathRenderError(f"渲染后仍含未替换占位符：{rendered!r}")

    # 4) 结果复核：不得含空段、不得穿越、不得超长
    if "//" in rendered:
        rendered = re.sub(r"/{2,}", "/", rendered)
    pure = PurePosixPath(rendered)
    if len(pure.parts) < 2:
        raise PathRenderError(f"渲染路径层级过浅：{rendered!r}")
    for part in pure.parts:
        if part in {".", ".."}:
            raise PathRenderError(f"渲染结果含路径穿越：{rendered!r}")
    if len(rendered) > MAX_PATH_LEN:
        raise PathRenderError("渲染路径过长")

    return RenderedPath(path=pure, template=tpl)


def is_within(root: str, candidate: str) -> bool:
    """候选路径是否位于根目录之下（FR-5.2.5c 第三重校验）。

    两侧都做 ``realpath``：macOS 下 ``tempfile`` 给 ``/var/folders/...`` 但
    ``realpath`` 解析到 ``/private/var/folders/...``，不归一化会让
    字面前缀匹配失败从而误报越界。
    """
    import os
    try:
        root_abs = os.path.realpath(root)
        cand_abs = os.path.realpath(candidate)
    except (TypeError, ValueError, OSError):
        return False
    try:
        root_p = PurePosixPath(root_abs)
        cand_p = PurePosixPath(cand_abs)
    except (TypeError, ValueError):
        return False
    if not root_p.is_absolute():
        return False
    root_parts = root_p.parts
    cand_parts = cand_p.parts
    if len(cand_parts) <= len(root_parts):
        return False
    return cand_parts[: len(root_parts)] == root_parts


def device_of(path: str) -> int | None:
    """取路径所在设备号（FR-5.2.5c ``st_dev`` 校验）。

    路径不存在时返回 ``None``。
    """
    try:
        return Path(path).stat().st_dev
    except OSError:
        return None