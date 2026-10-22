"""NexusPHP 站点用户数据解析 helper（FR-1.6 精简版 + 时魔，7 站共用）。

7 个 NexusPHP 派生站共用 :func:`parse_user_snapshot`——从页面顶部
**navbar**（``stdhead()`` 渲染，每个登录页都有）同时抽 4 业务字段：

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

时魔（每小时获得的站点积分）见 :func:`parse_hourly_bonus`——数据在
``/mybonus.php``，与 navbar 同一页，**不增加请求数**。
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from app.adapters.sites.base import parse_size_text
from app.adapters.sites.user_data import HourlyBonus, SiteUserSnapshot

__all__ = [
    "parse_bonus_headline",
    "parse_bonus_total_column",
    "parse_hourly_bonus",
    "parse_number",
    "parse_progress_percent",
    "parse_user_snapshot",
]


#: 数字（含千分位 + 小数）："1,234.56" → 1234.56
_NUMBER_RE = re.compile(r"([\d,]+(?:\.\d+)?)")


#: 时魔 headline：「你当前每小时能获取90.029个魔力值 (A = 878.3)」
#:
#: 单位词紧跟数值（"个魔力值" / "个积分" / "爆米花"），各站叫法不同。
_HEADLINE_RE = re.compile(
    r"每小时能获取\s*([\d,]+(?:\.\d+)?)\s*个?\s*([^\s(（,，、]+)"
)

#: 进度条：``<img class="loadbarred" ... alt="11.85%" />``
#:
#: class 后面可能还跟 Tailwind 工具类（hhanclub: ``loadbaryellow !rounded-lg``），
#: 且 ``style`` 的单位各站不同（px / %），故**只认 alt 里的百分比**。
_LOADBAR_IMG_RE = re.compile(
    r"<img[^>]*\bclass=\"[^\"]*\bloadbar\w*[^\"]*\"[^>]*>", re.IGNORECASE
)

#: 进度条 alt 里的百分比："11.850854180538%"
_ALT_PCT_RE = re.compile(r"alt=\"([\d.]+)%\"")

#: 页面显式声明「已达上限」（audiences 侧栏）
_CAPPED_TEXT = "已达上限"

#: 合计表头行里的「合计」列名
_TOTAL_COLUMN = "合计"

#: 合计表里代表「基础奖励」的行（合计值填在这一行）
_BASE_ROW = "基本奖励"


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


def _find_active_count(text: str, html: str = "") -> tuple[int, int]:
    """从多种模板提取做种/下载中数。

    支持的 pattern:
    - "当前活动：181 0"（NexusPHP navbar 主流）
    - "Seeding 152 / Leeching 23"（独立标签）
    - "做种 237 / 下载 2"（中文）
    - "活跃 ↑ 709 / ↓ 1"（audiences 等 SPA 文本模式）
    - JSON "活跃","value":"↑ 709 / ↓ 1"（audiences React SSR）
    """
    seeding_count = 0
    leeching_count = 0

    # 模式 0（audiences 专用）: JSON 风格 "label":"活跃","value":"↑ N / ↓ M"
    if html:
        m = re.search(
            r'(?:&quot;|")label(?:&quot;|")\s*:\s*'
            r'(?:&quot;|")活跃(?:&quot;|")\s*,\s*'
            r'(?:&quot;|")value(?:&quot;|")\s*:\s*'
            r'(?:&quot;|")↑\s*(\d+)\s*/\s*↓\s*(\d+)',
            html,
        )
        if m:
            return _parse_int(m.group(1)), _parse_int(m.group(2))

    # 模式 1: 当前活动 X Y
    m = re.search(r"当前活动[:：]\s*([\d,]+)\s+([\d,]+)", text)
    if m:
        return _parse_int(m.group(1)), _parse_int(m.group(2))

    # 模式 1b: 活跃 ↑ N / ↓ M（audiences 文本）
    m = re.search(r"活跃\s*↑\s*(\d+)\s*/\s*↓\s*(\d+)", text)
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
        # 用 \b 边界 + 排除"已"前缀，避免把"已下载: 100"（历史下载量）
        # 当当前 leeching 数
        m = re.search(r"(?<!已)下载(?:中)?[:：]\s*([\d,]+)", text)
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
    # 优先用 labeled 提取的值；仅当 labeled 为空时才回退到全文 regex。
    # 不允许 regex 命中后覆盖 labeled 已正确解析的值——千分位如
    # "1,234.56 TB" 会被 regex `[\d.]+` 在逗号处截断为 "1"，导致
    # 正确值被错误覆盖。
    upload = parse_size_text(labeled.get("upload", ""))
    if not upload:
        m = re.search(
            r"上传量[:：]\s*([\d.,]+)\s*(TB|GB|MB|KB)", text, re.IGNORECASE
        )
        if m:
            # 去千分位后再 parse
            upload = parse_size_text(
                f"{m.group(1).replace(',', '')} {m.group(2)}"
            )
    download = parse_size_text(labeled.get("download", ""))
    if not download:
        m = re.search(
            r"下载量[:：]\s*([\d.,]+)\s*(TB|GB|MB|KB)", text, re.IGNORECASE
        )
        if m:
            download = parse_size_text(
                f"{m.group(1).replace(',', '')} {m.group(2)}"
            )

    # 2. seeding / leeching 数量
    seeding_count, leeching_count = _find_active_count(text, html)

    # 全部空 → 解析失败
    if not (upload or download or seeding_count or leeching_count):
        return None
    return SiteUserSnapshot(
        upload=upload,
        download=download,
        seeding_count=seeding_count,
        leeching_count=leeching_count,
    )


# ------------------------------------------------------------------ 时魔


def parse_number(text: str | None) -> float | None:
    """从 ``"1,234.56"`` 提浮点数。失败返回 ``None``。"""
    if not text:
        return None
    m = _NUMBER_RE.search(text.replace(" ", ""))
    if m is None:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _to_text(html: str | bytes) -> str:
    """HTML → 纯文本（tag 之间用空格分隔）。"""
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="ignore")
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


def parse_bonus_headline(html: str | bytes) -> tuple[float, str | None] | None:
    """「你当前每小时能获取 X 个<单位>」→ ``(数值, 单位词)``。

    hdsky 的 headline 后面还跟「如果你有捐赠标志 ，你每小时将能获取 2 倍…」，
    这里只取**第一个**（未捐赠时的实际收益）。
    """
    m = _HEADLINE_RE.search(_to_text(html))
    if m is None:
        return None
    value = parse_number(m.group(1))
    if value is None:
        return None
    return value, m.group(2)


def parse_progress_percent(html: str | bytes) -> float | None:
    """进度条百分比（**第一根** = 总进度）。

    只认 ``alt="NN%"``——``style`` 的单位各站不同（px / %），不可靠。
    """
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="ignore")
    for m in _LOADBAR_IMG_RE.finditer(html):
        alt = _ALT_PCT_RE.search(m.group(0))
        if alt is None:
            continue
        try:
            return float(alt.group(1))
        except ValueError:
            continue
    return None


def parse_bonus_total_column(html: str | bytes) -> float | None:
    """标准 NexusPHP 1.9「每小时获得的合计X」表的**合计列**（基本奖励行）。

    表结构（实测 soulvoice）::

        奖励类型 | 数量 | 体积 | A 值 | 基础魔力 | 系数 | 获得魔力 | 合计
        基本奖励 | 582  | 5.134 TB | 2,190.201 | 151.334 | 1 | 151.334 | 151.334

    合计值（跨行单元格）填在「基本奖励」行——那一行是各分项之和。

    没有这张表的老模板（hdsky / ourbits）返回 ``None``，由调用方回退到
    headline（那两站没有加成分项，headline 即合计）。
    """
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="ignore")
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        col = -1
        header_at = -1
        for i, tr in enumerate(rows):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if _TOTAL_COLUMN in cells:
                col = cells.index(_TOTAL_COLUMN)
                header_at = i
                break
        if header_at < 0:
            continue
        for tr in rows[header_at + 1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if not cells:
                continue
            if cells[0].strip() != _BASE_ROW:
                continue
            if 0 <= col < len(cells):
                return parse_number(cells[col])
            return None
    return None


def parse_hourly_bonus(
    html: str | bytes, *, unit: str | None = None
) -> HourlyBonus | None:
    """标准 NexusPHP（1.5 / 1.9 模板）时魔解析。

    取值顺序：合计表「合计」列 → headline（无合计表的老模板）。
    ``unit`` 由调用方（各站适配器）传入站点自称；不传则用页面上的词。

    仅覆盖 1.5 / 1.9 标准模板；hhanclub（div 网格）、hdhome（额外奖励项）、
    audiences（SPA 侧栏）、springsunday（自定义表格）各有自己的解析函数。
    """
    text = _to_text(html)
    headline = _HEADLINE_RE.search(text)
    progress = parse_progress_percent(html)

    capped: bool | None = None
    if progress is not None:
        capped = progress >= 100.0
    if _CAPPED_TEXT in text:
        capped = True

    amount = parse_bonus_total_column(html)
    if amount is None and headline is not None:
        amount = parse_number(headline.group(1))
    if amount is None:
        return None

    resolved_unit = unit if unit is not None else (
        headline.group(2) if headline is not None else None
    )
    return HourlyBonus(
        amount=amount,
        unit=resolved_unit,
        capped=capped,
        progress=progress,
    )
