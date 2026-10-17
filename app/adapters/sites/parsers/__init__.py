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


def parse_user_info(html: str | bytes) -> UserInfo | None:
    """从 ``/userdetails.php?id=X`` HTML 提取用户身份/流量。

    字段缺失（如某些站不显示 bonus）→ 0 / None，不抛异常。
    整个页面无 ``userdetails`` 标识或解析全空 → 返回 ``None``。
    """
    soup = BeautifulSoup(html, "html.parser")

    # 1. username: <h1>username</h1> 或 <title>... :: Username ...</title>
    username: str | None = None
    h1 = soup.find("h1")
    if h1 is not None:
        username = h1.get_text(strip=True) or None
    if not username:
        title = soup.find("title")
        if title is not None:
            m = re.search(r"::\s*([^\s│|].+?)\s*(?:│|\||$)", title.get_text())
            if m:
                username = m.group(1).strip()

    # 2. userid: URL 上有 ?id=N
    userid: str | None = None
    userid_input = soup.find("input", attrs={"name": "id"})
    if userid_input is not None:
        userid = str(userid_input.get("value", ""))
    if not userid:
        # 退化：找 userdetails.php?id=N 链接
        a = soup.find("a", href=re.compile(r"userdetails\.php\?id=(\d+)"))
        if a is not None:
            m = re.search(r"id=(\d+)", str(a.get("href", "")))
            if m:
                userid = m.group(1)

    # 3. user_level: <img alt="Power User" class="class" /> 或 <font class="...">Power User</font>
    user_level: str | None = None
    img = soup.find(
        "img",
        attrs={
            "alt": re.compile(
                r"^(User|Power|Elite|VIP|Admin|Manager|Uploader|Peer|Sysop)",
                re.IGNORECASE,
            )
        },
    )
    if img is not None:
        user_level = str(img.get("alt") or "").strip() or None
    if not user_level:
        for cls in ("class", "role", "rank"):
            font = soup.find("font", class_=re.compile(cls, re.IGNORECASE))
            if font is not None and font.find("img") is None:
                t = font.get_text(strip=True)
                if t and len(t) < 32:
                    user_level = t
                    break

    # 4. join_at: 找 Join date 标签相邻 td
    join_at: str | None = None
    td = _find_value_td_after_label(soup, _JOIN_DATE_LABELS)
    if td is not None:
        raw = td.get_text(" ", strip=True)
        m = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", raw)
        if m:
            join_at = m.group(1)

    # 5. bonus / upload / download / ratio
    bonus_td = _find_value_td_after_label(soup, _BONUS_LABELS)
    bonus = _parse_float(bonus_td.get_text(" ", strip=True)) if bonus_td else 0.0

    up_td = _find_value_td_after_label(soup, _UPLOADED_LABELS)
    upload = parse_size_from_br(up_td.get_text(" ", strip=True)) if up_td else 0

    dn_td = _find_value_td_after_label(soup, _DOWNLOADED_LABELS)
    download = parse_size_from_br(dn_td.get_text(" ", strip=True)) if dn_td else 0

    ratio_td = _find_value_td_after_label(soup, _RATIO_LABELS)
    ratio = _parse_float(ratio_td.get_text(" ", strip=True)) if ratio_td else 0.0

    # 全部为空 → 解析失败
    if not any([username, userid, user_level]):
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
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    seeding_count = 0
    leeching_count = 0
    seeding_size = 0
    leeching_size = 0

    # 模式 1：独立 "Seeding: 152" 标签
    for m in re.finditer(r"seeding[:：]\s*([\d,]+)", text, re.IGNORECASE):
        seeding_count = _parse_int(m.group(1))
        break
    for m in re.finditer(r"leeching[:：]\s*([\d,]+)", text, re.IGNORECASE):
        leeching_count = _parse_int(m.group(1))
        break
    # 模式 2：中文标签 "做种 152" / "下载 23"
    if seeding_count == 0:
        for m in re.finditer(r"做种[:：]?\s*([\d,]+)", text):
            seeding_count = _parse_int(m.group(1))
            break
    if leeching_count == 0:
        for m in re.finditer(r"下载(?:中)?[:：]?\s*([\d,]+)", text):
            leeching_count = _parse_int(m.group(1))
            break

    # 模式 3：链接形式 "seeding.php?id=X (152)" / "seeding.php? ...[152]"
    if seeding_count == 0:
        a = soup.find("a", href=re.compile(r"seeding\.php", re.IGNORECASE))
        if a is not None:
            t = a.get_text(" ", strip=True)
            mm = re.search(r"[\(【\[（]\s*([\d,]+)\s*[\)】\]）]", t)
            if mm:
                seeding_count = _parse_int(mm.group(1))
    if leeching_count == 0:
        a = soup.find("a", href=re.compile(r"leeching\.php", re.IGNORECASE))
        if a is not None:
            t = a.get_text(" ", strip=True)
            mm = re.search(r"[\(【\[（]\s*([\d,]+)\s*[\)】\]）]", t)
            if mm:
                leeching_count = _parse_int(mm.group(1))

    # seeding/leeching size（"Seeding (1.2 TB)"）——分两个变量避免 mypy narrow
    s_size_m = re.search(r"seeding[^()]*?[\(【\[（]\s*([\d.]+)\s*(TB|GB|MB|KB)", text, re.IGNORECASE)
    if s_size_m:
        seeding_size = parse_size_text(f"{s_size_m.group(1)} {s_size_m.group(2)}")
    l_size_m = re.search(r"leeching[^()]*?[\(【\[（]\s*([\d.]+)\s*(TB|GB|MB|KB)", text, re.IGNORECASE)
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
