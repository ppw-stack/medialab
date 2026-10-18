"""NexusPHP 站点用户数据解析 helper（FR-1.6，7 站共用）。

7 个 NexusPHP 派生站点共用以下页面结构：

- ``/index.php`` 导航栏 → ``<a href="messages.php">`` 内含未读消息数
  ``<strong><a href="messages.php">[<span>5</span>]</a></strong>`` 或
  ``<span class="unread-count">5</span>``（不同站点模板略有差异，宽松匹配）
- ``/userdetails.php?id=X`` 表格显示：
  - 等级：``<td><font class="...">Power User</font></td>`` 或
    ``<img class="class" alt="Power User" />``
  - Join Date：``<td>2008-01-15 10:30:42</td>`` 紧邻 ``Join date`` 标签
  - Uploaded / Downloaded：紧邻标签的 td 文本
  - Share ratio：紧邻 ``Share ratio`` 标签的 td 文本
  - Bonus：紧邻 ``Bonus`` 标签的 td 文本（如 "21,234.56" 或 "21,234"）
  - Seeding / Leeching：通常在 userdetails 右上角的 ``<ul>`` 或
    ``<table>`` 中，文本形如 ``Seeding: 152`` / ``Leeching: 23``
  - H&R 风险：``<img class="hitandrun" />`` 或 ``<font class="hitandrun">``
    存在即说明有 H&R 警告

设计选择：这里**只提供函数**（不抽 base class），各适配器拿到 HTML 后调
对应函数。理由是各站的反爬/特殊头（ourbits JWT / hddolby cf_clearance）
实现差异大，但 HTML 解析逻辑可以 100% 共享。
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup
from bs4.element import Tag

from app.adapters.sites.base import parse_size_from_br, parse_size_text
from app.adapters.sites.user_data import (
    HrStatus,
    MessageSummary,
    SeedingStats,
    UserInfo,
)

# ---------------------------------------------------------------- regex

#: Bonus 文本："21,234.56" → 21234.56
_BONUS_RE = re.compile(r"([\d,]+\.?\d*)")

#: 数字（千分位 + 小数）："1,234.56" → 1234.56
_NUMBER_RE = re.compile(r"([\d,]+(?:\.\d+)?)")

#: Join date 标签（"Join date" / "加入日期" / "注册时间"）
_JOIN_DATE_LABELS = ("join date", "加入日期", "注册时间", "registered on")

#: 分享率标签
_RATIO_LABELS = ("share ratio", "分享率", "分享率")

#: 上传量标签
_UPLOADED_LABELS = ("uploaded", "上传量", "总上传")

#: 下载量标签
_DOWNLOADED_LABELS = ("downloaded", "下载量", "总下载")

#: Bonus 标签
_BONUS_LABELS = ("bonus", "魔力值", "积分", "magic")

#: H&R 警示图标 class（NexusPHP 1.5+ 标准）
_HR_CLASSES = ("hitandrun", "hit-and-run", "warning")


# ---------------------------------------------------------------- nav parser


__all__ = [
    "parse_hr_status",
    "parse_message_unread",
    "parse_user_info",
    "parse_user_seeding_stats",
]


def parse_message_unread(html: str | bytes) -> MessageSummary:
    """从 ``/index.php`` HTML 提取 navbar 未读消息数。

    各站模板差异（都宽松处理）：
    - ``<a href="messages.php"><strong>[<span>5</span>]</strong></a>``
    - ``<a href="messages.php">站内信(5)</a>``
    - ``<span class="unread-count">5</span>``
    - 找不到未读数 → 0（不是错误）
    """
    soup = BeautifulSoup(html, "html.parser")
    # 找 messages.php 锚点
    a = soup.find("a", href=re.compile(r"messages\.php", re.IGNORECASE))
    if a is None:
        return MessageSummary(unread_count=0)
    # 优先读 .unread-count span
    span = a.find("span", class_="unread-count")
    if span is not None:
        text = span.get_text(strip=True)
        m = _NUMBER_RE.search(text)
        if m:
            try:
                return MessageSummary(unread_count=int(m.group(1).replace(",", "")))
            except ValueError:
                pass
    # 退化：整段文本里提数字（"站内信(5)" / "messages(5)" / "[5]"）
    text = a.get_text(" ", strip=True)
    m = re.search(r"[\(【\[（]\s*(\d+)\s*[\)】\]）]", text)
    if m:
        try:
            return MessageSummary(unread_count=int(m.group(1)))
        except ValueError:
            pass
    return MessageSummary(unread_count=0)


# ---------------------------------------------------------------- userdetails parser


def _find_value_td_after_label(
    soup: BeautifulSoup, labels: tuple[str, ...]
) -> Tag | None:
    """在 userdetails 表格里找 ``<td>label</td><td>value</td>`` 的 value td。

    NexusPHP userdetails 表格常用 ``<tr>...<td>Label</td><td>Value</td>...</tr>`` 结构。
    模糊匹配 label 文本（lowercase contains）。
    """
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 2:
            continue
        label_text = tds[0].get_text(" ", strip=True).lower()
        if any(lbl in label_text for lbl in labels):
            return tds[1]
    return None


def _find_text_near_label(
    soup: BeautifulSoup, labels: tuple[str, ...]
) -> str:
    """宽松版：找文本里包含 label 的 td 块，返回同 td 文本或后续 td 文本。"""
    for td in soup.find_all("td"):
        text = td.get_text(" ", strip=True).lower()
        if any(lbl in text for lbl in labels):
            # 尝试下一个 td
            nxt = td.find_next_sibling("td")
            if nxt is not None:
                return nxt.get_text(" ", strip=True)
            # 退化：本 td 去掉 label 前缀
            return text
    return ""


def _parse_int(text: str) -> int:
    """从 ``"1,234"`` / ``"1.23 K"`` / ``"1234"`` 提整数。失败返回 0。"""
    if not text:
        return 0
    m = _NUMBER_RE.search(text.replace(" ", ""))
    if m is None:
        return 0
    try:
        return int(float(m.group(1).replace(",", "")))
    except ValueError:
        return 0


def _parse_float(text: str) -> float:
    """从 ``"21,234.56"`` 提 float。失败返回 0.0。"""
    if not text:
        return 0.0
    m = _BONUS_RE.search(text.replace(" ", ""))
    if m is None:
        return 0.0
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return 0.0


# ---------------------------------------------------------------- label-aware extract

#: 标签 → 字段名（多语言 + 别名）
_LABEL_FIELD_MAP: dict[str, str] = {
    "上传量": "upload", "uploaded": "upload",
    "下载量": "download", "downloaded": "download",
    "分享率": "ratio", "share ratio": "ratio", "share ratio:": "ratio",
    "魔力值": "bonus", "鲸币": "bonus", "积分": "bonus", "bonus": "bonus",
    "做种积分": "bonus", "seeding bonus": "bonus",
    "做种": "seeding", "seeding": "seeding",
    "下载中": "leeching", "下载中:": "leeching", "leeching": "leeching",
    "加入日期": "join_at", "join date": "join_at", "注册时间": "join_at",
    "签到得魔力": "ignored", "签到得憨豆": "ignored", "签到得鲸币": "ignored",
    "认领": "ignored", "邀请": "ignored", "勋章": "ignored", "任务": "ignored",
    "下载筐": "ignored", "收藏": "ignored", "rss": "ignored",
    "当前活动": "active", "active": "active",
}


def _extract_labeled_values(html: str) -> dict[str, str]:
    """从 userdetails HTML 提取"Label：Value"对，覆盖 3 种模板。

    **模式 1（navbar，NexusPHP 1.5）**::

        <font class="color_ratio">分享率：</font> 14.904

    **模式 2（JSON/React，audiences）**::

        "上传量：","value":"167.854 TB"

    **模式 3（表格，NexusPHP 1.6+）**::

        <td>分享率</td><td>14.904</td>
    """
    out: dict[str, str] = {}

    # 模式 1: <font class="color_XXX">Label：</font> Value
    for m in re.finditer(
        r'<font[^>]*class="[^"]*color_(\w+)[^"]*"[^>]*>([^<]*)</font>\s*([^<\n]+?)(?=<|$)',
        html,
        re.IGNORECASE,
    ):
        field = m.group(1).lower()
        label = m.group(2).strip().rstrip("：:")
        value = m.group(3).strip()
        # 跳过"color_active"等 UI 标签
        if field in ("uploaded", "downloaded", "ratio", "bonus", "active", "seeding", "leeching"):
            # 优先用 label 内容（"上传量"）映射
            mapped = _LABEL_FIELD_MAP.get(label.lower())
            if mapped and mapped not in out:
                out[mapped] = value

    # 模式 2: JSON 风格 "Label：","value":"X Y"
    for m in re.finditer(
        r'"([^"]+[：:])"\s*,\s*"value"\s*:\s*"([^"]+)"',
        html,
    ):
        label = m.group(1).strip().rstrip("：:")
        value = m.group(2).strip()
        mapped = _LABEL_FIELD_MAP.get(label.lower())
        if mapped and mapped not in out and mapped != "ignored":
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
        if mapped and mapped not in out and mapped != "ignored":
            out[mapped] = value

    return out


def _find_seeding_leeching_in_text(text: str) -> tuple[int, int]:
    """从 "当前活动：181 0" / "Seeding: 152 Leeching: 23" / "做种 237 下载 2" 模式提取。"""
    seeding_count = 0
    leeching_count = 0

    # 模式 1: 当前活动 X Y
    m = re.search(r"当前活动[:：]\s*([\d,]+)\s+([\d,]+)", text)
    if m:
        seeding_count = _parse_int(m.group(1))
        leeching_count = _parse_int(m.group(2))
        return seeding_count, leeching_count

    # 模式 2: Seeding 152 / Leeching 23
    m1 = re.search(r"\bseeding\b[:：]?\s*([\d,]+)", text, re.IGNORECASE)
    m2 = re.search(r"\bleeching\b[:：]?\s*([\d,]+)", text, re.IGNORECASE)
    if m1:
        seeding_count = _parse_int(m1.group(1))
    if m2:
        leeching_count = _parse_int(m2.group(2))

    # 模式 3: 做种 237 / 下载 2（需排除"下载量"）
    if seeding_count == 0:
        m = re.search(r"做种[:：]?\s*([\d,]+)", text)
        if m:
            # 确认不是"做种积分：1599009.2"
            ctx_start = max(0, m.start() - 5)
            ctx = text[ctx_start:m.end()]
            if "积分" not in ctx and "bonus" not in ctx.lower():
                seeding_count = _parse_int(m.group(1))
    if leeching_count == 0:
        m = re.search(r"下载(?:中)?[:：]\s*([\d,]+)", text)
        if m:
            ctx_start = max(0, m.start() - 5)
            ctx = text[ctx_start:m.end()]
            if "量" not in ctx and "ed" not in ctx.lower():
                leeching_count = _parse_int(m.group(1))

    return seeding_count, leeching_count


def _find_join_at_in_text(text: str) -> str | None:
    """从文本提取 "2008-01-15 10:30:42" 加入时间。"""
    m = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", text)
    if m:
        return m.group(1)
    m = re.search(r"加入日期[:：]?\s*(\d{4}-\d{2}-\d{2})", text)
    if m:
        return f"{m.group(1)} 00:00:00"
    return None


def _find_username_in_html(soup: BeautifulSoup, html: str) -> str | None:
    """从 userdetails 找 username（多模式）。"""
    # 模式 1: <h1>username</h1>
    h1 = soup.find("h1")
    if h1 is not None:
        t = h1.get_text(strip=True)
        if t:
            return t
    # 模式 2: <title>:: username ::</title>
    title = soup.find("title")
    if title is not None:
        t = title.get_text(" ", strip=True)
        m = re.search(r"::\s*([^\s│|].+?)\s*(?:│|\||$)", t)
        if m:
            return m.group(1).strip()
    # 模式 3: navbar 里 class="USER-NAME" 或 [USER]<a>
    for el in soup.find_all(True, {"class": re.compile(r"username|user-name", re.IGNORECASE)}):
        t = el.get_text(strip=True)
        if t and len(t) < 32:
            return t
    return None


def _find_user_level_in_html(soup: BeautifulSoup) -> str | None:
    """从 userdetails 找 user_level（多模式）。"""
    # 模式 1: <img alt="Power User" class="...">
    img = soup.find(
        "img",
        attrs={
            "alt": re.compile(
                r"^(User|Power|Elite|VIP|Admin|Manager|Uploader|Peer|Sysop|User$|贵宾|精英|管理员|上传员)",
                re.IGNORECASE,
            )
        },
    )
    if img is not None:
        return str(img.get("alt") or "").strip() or None
    # 模式 2: <font class="...">Power User</font>
    for cls in ("class", "role", "rank", "user-class"):
        font = soup.find("font", class_=re.compile(cls, re.IGNORECASE))
        if font is not None and font.find("img") is None:
            t = font.get_text(strip=True)
            if t and len(t) < 32:
                return t
    # 模式 3: <a href="userdetails.php?id=X"><b>Power User</b></a>
    a = soup.find("a", href=re.compile(r"userdetails\.php", re.IGNORECASE))
    if a is not None:
        for child in a.find_all(["b", "strong", "font"]):
            t = child.get_text(strip=True)
            if t and len(t) < 32:
                return t
    return None


def parse_user_info(html: str | bytes) -> UserInfo | None:
    """从 ``/userdetails.php?id=X`` HTML 提取用户身份/流量。

    字段缺失（如某些站不显示 bonus）→ 0 / None，不抛异常。
    整个页面无 ``userdetails`` 标识或解析全空 → 返回 ``None``。

    解析优先级：
    1. navbar 模式（NexusPHP 1.5 主流）
    2. JSON/React SSR 模式（audiences 等 SPA 站）
    3. 表格模式（NexusPHP 1.6+ 详细资料表）
    """
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="ignore")
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    # 抽取 label/value 对
    labeled = _extract_labeled_values(html)

    # 1. username
    username = _find_username_in_html(soup, html)

    # 2. userid: URL 上有 ?id=N
    userid: str | None = None
    m = re.search(r"userdetails\.php\?id=(\d+)", html)
    if m:
        userid = m.group(1)

    # 3. user_level
    user_level = _find_user_level_in_html(soup)

    # 4. join_at
    join_at = _find_join_at_in_text(text)

    # 5. 数值字段：优先 labeled，其次 text 正则
    upload = parse_size_text(labeled.get("upload", "")) or parse_size_from_br(text)
    # 更精确：上传量 X TB
    m = re.search(r"上传量[:：]\s*([\d.]+)\s*(TB|GB|MB|KB)", text, re.IGNORECASE)
    if m:
        upload = parse_size_text(f"{m.group(1)} {m.group(2)}")

    download = parse_size_text(labeled.get("download", "")) or parse_size_from_br(text)
    m = re.search(r"下载量[:：]\s*([\d.]+)\s*(TB|GB|MB|KB)", text, re.IGNORECASE)
    if m:
        download = parse_size_text(f"{m.group(1)} {m.group(2)}")

    ratio = _parse_float(labeled.get("ratio", "")) or _parse_float(labeled.get("ratio", "0"))
    m = re.search(r"分享率[:：]\s*([\d.]+)", text)
    if m:
        ratio = _parse_float(m.group(1))

    bonus = _parse_float(labeled.get("bonus", ""))
    m = re.search(r"(?:魔力值|鲸币|积分)[:：]\s*([\d,.]+)", text)
    if m:
        bonus = _parse_float(m.group(1))

    # 全部为空 → 解析失败
    if not any([username, userid, user_level, upload, download, ratio]):
        return None
    return UserInfo(
        username=username,
        userid=userid,
        user_level=user_level,
        join_at=join_at,
        bonus=bonus,
        upload=upload,
        download=download,
        ratio=ratio,
    )


def parse_user_seeding_stats(html: str | bytes) -> SeedingStats:
    """从 ``/userdetails.php?id=X`` HTML 提取做种/下载汇总。

    表格常见写法：
    - 独立 td：``<td>Seeding: 152</td>`` / ``<td>Leeching: 23</td>``
    - 或 userdetails 顶部 ``<ul>``/``<div>`` 的状态栏
    - 偶尔会带 size：``Seeding: 152 (1.2 TB)``

    只取 count + 总 size（"Seeding (1.2 TB)"）—— 不取每条做种详情。
    """
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="ignore")
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    # 优先用 navbar 模式 "当前活动 X Y"（最稳，覆盖 7 站）
    seeding_count, leeching_count = _find_seeding_leeching_in_text(text)

    # seeding/leeching size（"Seeding (1.2 TB)"）——分两个变量避免 mypy narrow
    seeding_size = 0
    leeching_size = 0
    size_re = r"[\(【\[（]\s*([\d.]+)\s*(TB|GB|MB|KB)"
    s_size_m = re.search(r"seeding[^()]*?" + size_re, text, re.IGNORECASE)
    if s_size_m:
        seeding_size = parse_size_text(f"{s_size_m.group(1)} {s_size_m.group(2)}")
    l_size_m = re.search(r"leeching[^()]*?" + size_re, text, re.IGNORECASE)
    if l_size_m:
        leeching_size = parse_size_text(f"{l_size_m.group(1)} {l_size_m.group(2)}")

    return SeedingStats(
        seeding_count=seeding_count,
        leeching_count=leeching_count,
        seeding_size=seeding_size,
        leeching_size=leeching_size,
    )


def parse_hr_status(html: str | bytes) -> HrStatus:
    """从 userdetails HTML 检测 H&R 风险。

    标记：``<img class="hitandrun" />`` / ``<font class="hitandrun">`` /
    文本 "Hit and Run warning" / "H&R 警告" / "因断流被警告"。

    warning_count 多数站不显示具体数量 → 0；is_hr 才是核心信号。
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True).lower()

    is_hr = False
    warning_count = 0

    # CSS class 命中
    for el in soup.find_all(True, {"class": True}):
        classes_val: object = el.get("class")
        if classes_val is None:
            continue
        if isinstance(classes_val, str):
            classes_list: list[str] = [classes_val]
        elif isinstance(classes_val, list):
            classes_list = [str(c) for c in classes_val]
        else:
            continue
        cls_text = " ".join(classes_list).lower()
        if any(h in cls_text for h in _HR_CLASSES):
            is_hr = True
            break
    # 文本命中（class 没命中时 fallback）
    if not is_hr and (
        "hit and run" in text or "hit-and-run" in text or "h&r" in text or "断流" in text
    ):
        is_hr = True

    # warning count:  "H&R warning: 2" / "警告次数：2"
    m = re.search(r"h[&＆]?r[^a-z\d]{0,10}warning[^a-z\d]{0,10}(\d+)", text)
    if m:
        warning_count = _parse_int(m.group(1))
    else:
        m = re.search(r"警告[次数]*[：:]?\s*(\d+)", text)
        if m:
            warning_count = _parse_int(m.group(1))

    return HrStatus(is_hr=is_hr, warning_count=warning_count)
