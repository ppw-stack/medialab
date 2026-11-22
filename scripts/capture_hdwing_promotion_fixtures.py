#!/usr/bin/env python3
"""抓 hdwing 站点「种子优惠日志」页面，脱敏 + 裁剪成测试 fixture。

**为什么需要真实页面固化**（AGENTS.md）：hdwing 的免费判定只能来自
``GET /promotion.php?id={tid}``（服务端渲染的优惠历史表）——列表页里只有
空占位 ``span.sp_state_placeholder``，而那个 AJAX 接口（``ajax_promotion.php``）
被 Cloudflare Turnstile 闸保护，未过闸时对**任意 id**（含不存在的种子）都返回
兜底的 ``pro_free``，照它解析会把「限免 6 小时」误判成「永久免费」。

真实页面的四种形态都要固化（手写最小 HTML 会漏掉真实写法）：

1. **生效中的限时免费** —— 1 条 Free 行，起止时间齐全
2. **生效中的 Free + 已过期的其它优惠** —— 过期行带
   ``<tr style='text-decoration: line-through;'>``（删除线）
3. **有表头但无 Free 行**（只有 50% 或从没有过优惠）
4. **Free 行但截止时间已过**（窗口已结束 → 当前不是免费种）

脚本做三件事（与 ``capture_checkin_fixtures.py`` 同款）：

1. **裁剪**：只留 ``div.noraml_box`` 区块（标题 + 优惠历史表 + 分页）
2. **脱敏**：账号 UID / 用户名（navbar）随裁剪一并去掉；标题是公开种子名，保留
3. **自校验**：裁剪后的解析结果必须与整页一致（否则裁剪破坏了判据）

用法::

    # 从生产库读 cookie（只读）
    uv run python scripts/capture_hdwing_promotion_fixtures.py \
        --db /path/to/medialab.db

    # 或直接给 cookie 文件
    uv run python scripts/capture_hdwing_promotion_fixtures.py \
        --cookie-file /path/cookie.txt

只读：脚本不写任何数据库，只 GET 站点页面；唯一写入是 tests/fixtures。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import pathlib
import re
import sqlite3
import sys

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.adapters.sites.generic import _current_promotion, _parse_promotion_rows
from app.core.timeutils import bj_now_dt

logger = logging.getLogger(__name__)

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures"

BASE_URL = "https://hdwing.org"

#: 抓取的样本：(fixture 名, 种子 id, 期望的 Free 记录条数)
#:
#: 条数是**自校验**用的：裁剪后解析出的记录数必须与整页一致。
#: 「无 Free」样本的条数是 0；「过期」样本的条数是 1（Free 行还在，只是截止时间已过）。
SAMPLES: tuple[tuple[str, str, int], ...] = (
    ("hdwing_promotion_active.html", "841134", 1),
    ("hdwing_promotion_mixed.html", "640343", 5),
    ("hdwing_promotion_expired.html", "838960", 1),
    ("hdwing_promotion_no_free.html", "841155", 0),
)

#: 优惠日志区块的起止标记（真实页面用 ``class="noraml_box"``——拼写就是
#: 错的，NexusPHP 模板的老 typo，别「修正」）
_BLOCK_RE = re.compile(
    r'<div class="noraml_box">.*?<!-- noraml_box end -->', re.DOTALL
)


def _read_cookie(db: str | None, cookie_file: str | None) -> str:
    if cookie_file:
        try:
            return pathlib.Path(cookie_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SystemExit(f"读 cookie 文件失败：{cookie_file}: {exc}") from exc
    if db:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute(
                "select cookies from sites where domain like ? limit 1",
                ("%hdwing%",),
            ).fetchone()
        finally:
            con.close()
        if row is None or not row[0]:
            raise SystemExit(f"{db} 里没有 hdwing 站点的 cookies")
        return str(row[0])
    raise SystemExit("必须给 --db 或 --cookie-file")


def _trim(html: str) -> str:
    match = _BLOCK_RE.search(html)
    if match is None:
        raise SystemExit("页面里没找到 noraml_box 区块（站点改版？）")
    return match.group(0)


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
            key, value = part.split("=", 1)
            cookies[key.strip()] = value.strip()

    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        cookies=cookies,
        headers=headers,
    ) as client:
        for name, tid, expected in SAMPLES:
            resp = await client.get(f"{BASE_URL}/promotion.php?id={tid}")
            if resp.status_code != 200:
                raise SystemExit(f"id={tid} 返回 HTTP {resp.status_code}")
            full_rows, full_header = _parse_promotion_rows(resp.text)
            trimmed = _trim(resp.text)
            trimmed_rows, trimmed_header = _parse_promotion_rows(trimmed)
            if not full_header or not trimmed_header:
                raise SystemExit(f"{name}: 页面缺少优惠表头")
            if len(full_rows) != len(trimmed_rows):
                raise SystemExit(
                    f"{name}: 裁剪改变了解析结果（整页 {len(full_rows)} 条 / "
                    f"裁剪后 {len(trimmed_rows)} 条）"
                )
            if len(full_rows) != expected:
                raise SystemExit(
                    f"{name}: Free 记录数 {len(full_rows)} != 预期 {expected}"
                    "（样本 id 的页面形态变了？）"
                )
            try:
                (FIXTURES / name).write_text(trimmed + "\n", encoding="utf-8")
            except OSError as exc:
                raise SystemExit(f"写 fixture 失败：{name}: {exc}") from exc
            current = _current_promotion(full_rows, bj_now_dt())
            logger.info(
                "%s: id=%s 记录 %d 条，当前生效=%s",
                name,
                tid,
                len(full_rows),
                (
                    f"是（至 {current.end_at}）"
                    if current
                    else "否"
                ),
            )
    logger.info("已写入 %s", FIXTURES)


def main() -> None:
    parser = argparse.ArgumentParser(description="抓 hdwing 优惠历史 fixture")
    parser.add_argument("--db", help="sqlite 库路径（读 sites.cookies，只读）")
    parser.add_argument("--cookie-file", help="cookie 文本文件")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cookie = _read_cookie(args.db, args.cookie_file)
    asyncio.run(_capture(cookie))


if __name__ == "__main__":
    main()
