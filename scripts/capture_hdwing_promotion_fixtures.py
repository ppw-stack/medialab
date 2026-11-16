#!/usr/bin/env python3
"""抓 hdwing 站点「种子优惠日志」页面，脱敏 + 裁剪成测试 fixture。

**为什么需要真实页面固化**（AGENTS.md）：hdwing 的免费判定只能来自
``GET /promotion.php?id={tid}``（服务端渲染的优惠历史表）——列表页里只有
空占位 ``span.sp_state_placeholder``，而那个 AJAX 接口（``ajax_promotion.php``）
被 Cloudflare Turnstile 闸保护，未过闸时对**任意 id**（含不存在的种子）都返回
兜底的 ``pro_free``，照它解析会把「限免 6 小时」误判成「永久免费」。

真实页面的三种形态都要固化（手写最小 HTML 会漏掉真实写法）：

1. **生效中的限时免费** —— 1 条 Free 行，起止时间齐全
2. **生效中的 Free + 已过期的其它优惠** —— 过期行带
   ``<tr style='text-decoration: line-through;'>>``（删除线）
3. **有表头但无数据行**（该种子从没有过优惠）
4. **Free 行但截止时间已过**（窗口已结束 → 当前不是免费种）

脚本做三件事（与 ``capture_checkin_fixtures.py`` 同款）：

1. **裁剪**：只留 ``div.noraml_box`` 区块（标题 + 优惠历史表 + 分页）
2. **脱敏**：账号 UID / 用户名（navbar）随裁剪一并去掉；标题是公开种子名，保留
3. **自校验**：裁剪后的解析结果必须与整页一致（否则裁剪破坏了判据）

用法::

    # 从 dev 库读 cookie
    uv run python scripts/capture_hdwing_promotion_fixtures.py --db data-dev/medialab.db

    # 或直接给 cookie 文件
    uv run python scripts/capture_hdwing_promotion_fixtures.py --cookie-file /path/cookie.txt

只读：脚本不写任何数据库，只 GET 站点页面。
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import re
import sqlite3
import sys

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.adapters.sites.hdwing import _current_promotion, _parse_promotion_records

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures"

BASE_URL = "https://hdwing.org"

#: 抓取的样本：(fixture 名, 种子 id, 期望的 Free 记录条数)
#:
#: 条数是**自校验**用的：裁剪后解析出的记录数必须与整页一致。
#: 「过期」样本的条数是 1（Free 行还在，只是截止时间已过）。
SAMPLES: tuple[tuple[str, str, int], ...] = (
    ("hdwing_promotion_active.html", "840093", 1),
    ("hdwing_promotion_mixed.html", "840065", 1),
    ("hdwing_promotion_empty.html", "840080", 0),
    ("hdwing_promotion_expired.html", "839486", 1),
)

#: 优惠日志区块的起止标记（真实页面用 ``class="noraml_box"``——拼写就是
#: 错的，NexusPHP 模板的老 typo，别「修正」）
_BLOCK_RE = re.compile(
    r'<div class="noraml_box">.*?<!-- noraml_box end -->', re.DOTALL
)


def _read_cookie(db: str | None, cookie_file: str | None) -> str:
    if cookie_file:
        return pathlib.Path(cookie_file).read_text(encoding="utf-8").strip()
    if db:
        con = sqlite3.connect(db)
        try:
            row = con.execute(
                "select cookies from sites where adapter = 'hdwing' limit 1"
            ).fetchone()
        finally:
            con.close()
        if row is None or not row[0]:
            raise SystemExit(f"{db} 里没有 hdwing 站点的 cookies")
        return str(row[0])
    raise SystemExit("必须给 --db 或 --cookie-file")


def _trim(html: str) -> str:
    m = _BLOCK_RE.search(html)
    if m is None:
        raise SystemExit("页面里没找到 noraml_box 区块（站点改版？）")
    return m.group(0)


async def _capture(cookie: str) -> None:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        )
    }
    cookies: dict[str, str] = {}
    for part in cookie.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()

    async with httpx.AsyncClient(
        timeout=30.0, follow_redirects=True, cookies=cookies, headers=headers
    ) as client:
        for name, tid, expected in SAMPLES:
            resp = await client.get(f"{BASE_URL}/promotion.php?id={tid}")
            if resp.status_code != 200:
                raise SystemExit(f"id={tid} 返回 HTTP {resp.status_code}")
            full_records = _parse_promotion_records(resp.text)
            trimmed = _trim(resp.text)
            trimmed_records = _parse_promotion_records(trimmed)
            if len(full_records) != len(trimmed_records):
                raise SystemExit(
                    f"{name}: 裁剪改变了解析结果（整页 {len(full_records)} 条 / "
                    f"裁剪后 {len(trimmed_records)} 条）"
                )
            if len(full_records) != expected:
                raise SystemExit(
                    f"{name}: Free 记录数 {len(full_records)} != 预期 {expected}"
                    "（样本 id 的页面形态变了？）"
                )
            (FIXTURES / name).write_text(trimmed + "\n", encoding="utf-8")
            current = _current_promotion(full_records)
            print(
                f"{name}: id={tid} 记录 {len(full_records)} 条，"
                f"当前生效={'是（至 ' + str(current.end_at) + '）' if current else '否'}"
            )
    print(f"\n已写入 {FIXTURES}")


def main() -> None:
    ap = argparse.ArgumentParser(description="抓 hdwing 优惠历史 fixture")
    ap.add_argument("--db", help="sqlite 库路径（读 sites.cookies）")
    ap.add_argument("--cookie-file", help="cookie 文本文件")
    args = ap.parse_args()
    cookie = _read_cookie(args.db, args.cookie_file)
    asyncio.run(_capture(cookie))


if __name__ == "__main__":
    main()
