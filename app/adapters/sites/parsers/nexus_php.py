"""NexusPHP 站点用户数据解析 helper（FR-1.6 精简版，7 站共用）。

7 个 NexusPHP 派生站共用 :func:`parse_user_snapshot`——从
``/userdetails.php`` 一页同时抽 4 业务字段：

- **upload** (上传量, bytes)
- **download** (下载量, bytes)
- **seeding_count** (做种数)
- **leeching_count** (下载/未完成数)

解析覆盖 3 种模板：

1. **navbar 模式**（NexusPHP 1.5 主流）::

       <font class="color_uploaded">上传量：</font> 108.815 TB
       <font class="color_active">当前活动：</font> 181 0

2. **JSON/React SSR 模式**（audiences 等 SPA 站）::

       "上传量：","value":"167.854 TB"

3. **表格模式**（NexusPHP 1.6+ 详细资料表）::

       <td>上传量</td><td>108.815 TB</td>
       <td>Seeding</td><td>181</td>
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from app.adapters.sites.base import parse_size_text
from app.adapters.sites.user_data import SiteUserSnapshot

__all__ = ["parse_user_snapshot"]


#: 数字（含千分位 + 小数）："1,234.56" → 1234.56
_NUMBER_RE = re.compile(r"([\d,]+(?:\.\d+)?)")


#: 标签 → 字段名（navbar/JSON 模式 1 + 2 共用）
_LABEL_FIELD_MAP: dict[str, str] = {
    "上传量": "upload", "uploaded": "upload",
    "下载量": "download", "downloaded": "download",
    "做种": "seeding", "seeding": "seeding",
    "下载中": "leeching", "leeching": "leeching", "下载中:": "leeching",
}


def _parse_int(text: str) -> int:
    """从 ``"1,234"`` 提整数。失败返回 0。"""
    if not text:
        return 0
    m = _NUMBER_RE.search(text.replace(" ", ""))
    if m is None:
        return 0
    try:
        return int(float(m.group(1).replace(",", "")))
    except ValueError:
        return 0


def _extract_labeled_values(html: str) -> dict[str, str]:
    """从 userdetails HTML 提取"Label：Value"对，覆盖 3 种模板。"""
    out: dict[str, str] = {}

    # 模式 1: <font class="color_XXX">Label：</font> Value
    # value 用 BeautifulSoup-friendly：剥掉 font 后的 HTML 标签
    for m in re.finditer(
        r'<font[^>]*class="[^"]*color_(\w+)[^"]*"[^>]*>(.*?)</font>',
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        field = m.group(1).lower()
        if field not in ("uploaded", "downloaded", "seeding", "leeching"):
            continue
        font_end = m.end()
        tail = html[font_end:font_end + 500]
        tail_text = re.sub(r"<[^>]+>", " ", tail)
        tail_text = re.sub(r"\s+", " ", tail_text).strip()
        vm = re.match(r"([\d,.]+\s*(?:TB|GB|MB|KB|%)?)", tail_text)
        if vm is None:
            continue
        value = vm.group(1).strip()
        label_text = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        label = label_text.rstrip("：:")
        mapped = _LABEL_FIELD_MAP.get(label.lower())
        if mapped and mapped not in out:
            out[mapped] = value

    # 模式 2: JSON 风格 "Label：","value":"X Y"（含 HTML 实体 &quot;）
    for m in re.finditer(
        r'(?:&quot;|")([^&"]+?[：:])(?:&quot;|")\s*,\s*'
        r'(?:&quot;|")value(?:&quot;|")\s*:\s*(?:&quot;|")([^&"]+?)(?:&quot;|")',
        html,
    ):
        label = m.group(1).strip().rstrip("：:").replace("&amp;", "&")
        value = m.group(2).strip()
        mapped = _LABEL_FIELD_MAP.get(label.lower())
        if mapped and mapped not in out:
            out[mapped] = value

    # 模式 3: 表格 <td>Label</td><td>Value</td>
    for m in re.finditer(
        r"<td[^>]*>\s*([\w\s\u4e00-\u9fa5]+?)\s*</td>\s*<td[^>]*>\s*([^<]+?)\s*</td>",
        html,
        re.IGNORECASE,
    ):
        label = m.group(1).strip().rstrip("：:")
        value = m.group(2).strip()
        mapped = _LABEL_FIELD_MAP.get(label.lower())
        if mapped and mapped not in out:
            out[mapped] = value

    return out


def _find_active_count(text: str) -> tuple[int, int]:
    """从 "当前活动：181 0" / "Seeding 152 Leeching 23" / "做种 237 下载 2" 提取。"""
    seeding_count = 0
    leeching_count = 0

    # 模式 1: 当前活动 X Y
    m = re.search(r"当前活动[:：]\s*([\d,]+)\s+([\d,]+)", text)
    if m:
        return _parse_int(m.group(1)), _parse_int(m.group(2))

    # 模式 2: Seeding 152 / Leeching 23（独立标签）
    m1 = re.search(r"\bseeding\b[:：]?\s*([\d,]+)", text, re.IGNORECASE)
    m2 = re.search(r"\bleeching\b[:：]?\s*([\d,]+)", text, re.IGNORECASE)
    if m1:
        seeding_count = _parse_int(m1.group(1))
    if m2:
        leeching_count = _parse_int(m2.group(1))

    # 模式 3: 做种 237 / 下载 2（中文）
    if seeding_count == 0:
        m = re.search(r"做种[:：]?\s*([\d,]+)", text)
        if m:
            ctx = text[max(0, m.start() - 5):m.end()]
            if "积分" not in ctx and "bonus" not in ctx.lower():
                seeding_count = _parse_int(m.group(1))
    if leeching_count == 0:
        m = re.search(r"下载(?:中)?[:：]\s*([\d,]+)", text)
        if m:
            ctx = text[max(0, m.start() - 5):m.end()]
            if "量" not in ctx and "ed" not in ctx.lower():
                leeching_count = _parse_int(m.group(1))

    return seeding_count, leeching_count


def parse_user_snapshot(html: str | bytes) -> SiteUserSnapshot | None:
    """从 ``/userdetails.php`` HTML 提取 4 业务字段。

    全部字段缺失 → 返回 ``None``。
    """
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="ignore")
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    labeled = _extract_labeled_values(html)

    # 1. upload / download（navbar/JSON 模式拿 value，转字节）
    upload = parse_size_text(labeled.get("upload", ""))
    m = re.search(r"上传量[:：]\s*([\d.]+)\s*(TB|GB|MB|KB)", text, re.IGNORECASE)
    if m:
        upload = parse_size_text(f"{m.group(1)} {m.group(2)}")
    download = parse_size_text(labeled.get("download", ""))
    m = re.search(r"下载量[:：]\s*([\d.]+)\s*(TB|GB|MB|KB)", text, re.IGNORECASE)
    if m:
        download = parse_size_text(f"{m.group(1)} {m.group(2)}")

    # 2. seeding / leeching 数量
    seeding_count, leeching_count = _find_active_count(text)

    # 全部空 → 解析失败
    if not (upload or download or seeding_count or leeching_count):
        return None
    return SiteUserSnapshot(
        upload=upload,
        download=download,
        seeding_count=seeding_count,
        leeching_count=leeching_count,
    )
