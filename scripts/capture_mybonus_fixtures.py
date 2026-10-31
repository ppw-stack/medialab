#!/usr/bin/env python3
"""抓真实站点的 ``/mybonus.php``，脱敏 + 裁剪成测试 fixture。

时魔（每小时获得的站点积分）的解析规则**必须用真实页面固化**（AGENTS.md）：
手写最小 HTML 会漏掉真实文案与 DOM 坑——

- 各站叫法不同：魔力值 / 爆米花 / 茉莉 / 憨豆 / 积分
- 进度条 ``alt="NN%"`` 的 class 后面还跟 Tailwind 工具类（hhanclub）
- 合计值在**跨行单元格**里（hhanclub 的 div 网格，不是 table）
- hdhome 的 headline 只是**基础**时魔，额外奖励在下面单列
- audiences 是 SPA 侧栏，springSunday 是自定义表格

真实页面太大且含个人信息（用户名 / UID / 会话 token），故本脚本：

1. **裁剪**：只留 navbar（4 业务字段）与时魔区块两块
2. **脱敏**：用户名 → ``TESTUSER``，UID → ``12345``，会话 token → ``REDACTED``
3. **校验**：裁剪后的 fixture 与整页解析结果**必须一致**（否则裁剪破坏了
   解析特征，脚本直接报错）

用法::

    uv run python scripts/capture_mybonus_fixtures.py
    uv run python scripts/capture_mybonus_fixtures.py --db /path/to/medialab.db

依赖 dev 库的站点 cookie（``sites.cookies``）——脚本不碰生产库。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import re
import sqlite3
import sys

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.adapters.sites import audiences as audiences_mod
from app.adapters.sites import hdhome as hdhome_mod
from app.adapters.sites import hhanclub as hhanclub_mod
from app.adapters.sites import springsunday as springsunday_mod
from app.adapters.sites.parsers import nexus_php
from app.adapters.sites.user_data import SiteUserSnapshot

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures"

#: 替换后的占位 UID（保持数字形态，避免改动解析上下文）
_FAKE_UID = "12345"

_HEADLINE = "你当前每小时能获取"

#: 形如用户名的纯文本（ASCII 标识符形态）
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,19}$")


# --------------------------------------------------------------------- 脱敏


def _blank_link_text(inner: str) -> str:
    """把链接里的**用户名字本**换成 TESTUSER，保留标签与数字。

    不能整段替换链接内容——hhanclub 的做种/下载数就在
    ``<a href="userdetails.php?id=N&action=2">`` 里面（``&nbsp;167``），
    整段替换会把解析特征一起删掉。
    """
    parts = re.split(r"(<[^>]*>)", inner)
    out: list[str] = []
    for i, part in enumerate(parts):
        # 奇数位是标签；偶数位是文本
        if i % 2 == 1 or not part.strip():
            out.append(part)
        elif _NAME_RE.match(part.strip()):
            out.append("TESTUSER")
        else:
            out.append(part)
    return "".join(out)


def anonymize(html: str) -> str:
    """把用户名与 UID 替换成占位符（与 trim_checkin_fixtures.py 同一套规则）。"""
    # 用户名：``userdetails.php?id=X`` 链接内的文本（含嵌套 <b> / <img>）
    out = re.sub(
        r'(userdetails\.php\?id=\d+[^>]*>)(.*?)(</a>)',
        lambda m: m.group(1) + _blank_link_text(m.group(2)) + m.group(3),
        html,
        flags=re.DOTALL,
    )
    # ``欢迎回来, <b>name</b>`` / ``欢迎回来, name``
    out = re.sub(
        r"(欢迎回来[,，]\s*(?:<[^>]+>\s*)*)([^<>\s,，]{1,20})",
        lambda m: (
            m.group(1) + ("TESTUSER" if _NAME_RE.match(m.group(2)) else m.group(2))
        ),
        out,
    )
    # UID 数字（``UID: 125417`` / ``UID 22187`` / ``userdetails.php?id=125417``）
    out = re.sub(r"(UID[:\s]+)\d+", rf"\g<1>{_FAKE_UID}", out)
    out = re.sub(r"(userdetails\.php\?id=)\d+", rf"\g<1>{_FAKE_UID}", out)
    # 侧栏里的**其他用户**名（audiences 的上传者列表）——不是账号本人，
    # 但仍是真实用户名，一律匿名化
    out = re.sub(
        r'(alt="|title="|data-uploader-label=")([A-Za-z][A-Za-z0-9_]{2,19})(?=[" ])',
        r"\1TESTUSER",
        out,
    )
    # 其余 URL 里的用户 ID（``invite.php?id=`` / ``claim.php?uid=`` /
    # ``peerlist.php?userid=`` …）
    out = re.sub(
        r"(\?(?:id|uid|userid)=)\d+",
        rf"\g<1>{_FAKE_UID}",
        out,
    )
    # **会话 token**（``logout.php?token=``）——这是能直接用的凭据，必须抹掉
    out = re.sub(
        r"((?:logout|takelogout)\.php\?token=)[A-Za-z0-9]+",
        r"\1REDACTED",
        out,
    )
    # 页面 title 里的用户名（``HDSky :: ppwSky 的魔力值``）
    out = re.sub(
        r"(<title>[^<>]*?)\b[A-Za-z][A-Za-z0-9_]{2,19}\b([^<>]*?</title>)",
        r"\1TESTUSER\2",
        out,
    )
    return out


def trim(html: str) -> str:
    """去 script/style/svg，压空白（保留结构与属性）。"""
    out = re.sub(r"<script.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    out = re.sub(r"<style.*?</style>", "", out, flags=re.DOTALL | re.IGNORECASE)
    out = re.sub(r"<svg.*?</svg>", "", out, flags=re.DOTALL | re.IGNORECASE)
    out = re.sub(r'(?:src|xlink:href)="data:[^"]*"', 'src=""', out)
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n\s*\n+", "\n", out)
    return out.strip()


# --------------------------------------------------------------------- 裁剪


def _navbar_block(html: str) -> str:
    """navbar 区块（4 业务字段所在处）。

    两种模板：

    - 标准 NexusPHP：``<td class="bottom" …>…</td>``（hdsky / ourbits /
      soulvoice / hdhome）
    - springsunday：``<div id="info_block" …>…</div>``
    """
    i = html.find("当前活动")
    if i < 0:
        i = html.find("上传量")
    if i < 0:
        raise ValueError("找不到 navbar")
    td = html.rfind('<td class="bottom"', 0, i)
    if td >= 0:
        end = html.find("</td>", i)
        if end >= 0:
            return html[td:end + 5]
    div = html.rfind('<div id="info_block"', 0, i)
    if div >= 0:
        end = html.find("</div>", i)
        if end >= 0:
            return html[div:end + 6]
    raise ValueError("找不到 navbar 容器")


def _hhanclub_navbar(html: str) -> str:
    """hhanclub（Tailwind 主题）navbar：``<img alt="上传">`` 一带。

    窗口边界要**对齐标签**（否则片段从标签中间截断，后续裁剪正则会把
    半个标签当属性吃掉）。
    """
    idxs = [
        html.find(f'alt="{alt}"')
        for alt in ("上传", "下载", "做种数", "下载数")
    ]
    idxs = [i for i in idxs if i >= 0]
    if not idxs:
        raise ValueError("找不到 hhanclub navbar 图标")
    lo, hi = min(idxs), max(idxs)
    start = html.rfind("<", 0, lo)
    end = html.find(">", hi)
    if start < 0 or end < 0:
        raise ValueError("找不到 hhanclub navbar 边界")
    return html[start:end + 1]


def _audiences_navbar(html: str) -> str:
    """audiences（SPA）navbar：``data-uploader-stats`` 属性所在标签。"""
    i = html.find("data-uploader-stats")
    if i < 0:
        raise ValueError("找不到 audiences data-uploader-stats")
    start = html.rfind("<", 0, i)
    end = html.find(">", i)
    return html[start:end + 1]


def _bonus_block(html: str, end_marker: str) -> str:
    """时魔区块：从 headline 所在的 div 到 ``end_marker`` 之前。"""
    i = html.find(_HEADLINE)
    if i < 0:
        raise ValueError("找不到时魔 headline")
    start = html.rfind("<div", 0, i)
    end = html.find(end_marker, i)
    if end < 0:
        end = len(html)
    return html[start:end]


def _table_block(html: str, marker: str) -> str:
    """含 ``marker`` 的整张表。"""
    i = html.find(marker)
    if i < 0:
        raise ValueError(f"找不到 {marker}")
    start = html.rfind("<table", 0, i)
    end = html.find("</table>", i)
    if start < 0 or end < 0:
        raise ValueError(f"找不到含 {marker} 的表")
    return html[start:end + 8]


def _audiences_bonus_block(html: str) -> str:
    """audiences 时魔侧栏：rate 卡 → stats 卡之前。"""
    i = html.find("mybonus-side__rate")
    if i < 0:
        raise ValueError("找不到 audiences 侧栏")
    start = html.rfind("<div", 0, i)
    end = html.find("mybonus-side__stats", i)
    if end < 0:
        end = len(html)
    return html[start:end]


#: 每站的裁剪方案 + 解析入口
_SPECS: dict[str, dict[str, object]] = {
    "hdsky": {
        "nav": _navbar_block,
        "bonus": lambda h: _bonus_block(h, "其他获取方式"),
        "parse": lambda h: (
            nexus_php.parse_user_snapshot(h),
            nexus_php.parse_hourly_bonus(h, unit="魔力值"),
        ),
    },
    "ourbits": {
        "nav": _navbar_block,
        "bonus": lambda h: _bonus_block(h, "其他获取方式"),
        "parse": lambda h: (
            nexus_php.parse_user_snapshot(h),
            nexus_php.parse_hourly_bonus(h, unit="魔力值"),
        ),
    },
    "soulvoice": {
        "nav": _navbar_block,
        "bonus": lambda h: _bonus_block(h, "其他获取方式"),
        "parse": lambda h: (
            nexus_php.parse_user_snapshot(h),
            nexus_php.parse_hourly_bonus(h, unit="魔力值"),
        ),
    },
    "hdhome": {
        "nav": _navbar_block,
        "bonus": lambda h: _bonus_block(h, "其他获取方式"),
        "parse": lambda h: (
            nexus_php.parse_user_snapshot(h),
            hdhome_mod._parse_hdhome_hourly_bonus(h),
        ),
    },
    "hhanclub": {
        "nav": _hhanclub_navbar,
        "bonus": lambda h: _bonus_block(h, "其他获取方式"),
        "parse": lambda h: (
            hhanclub_mod._parse_hhanclub_snapshot(h),
            hhanclub_mod._parse_hhanclub_hourly_bonus(h),
        ),
    },
    "audiences": {
        "nav": _audiences_navbar,
        "bonus": _audiences_bonus_block,
        "parse": lambda h: (
            nexus_php.parse_user_snapshot(h),
            audiences_mod._parse_audiences_hourly_bonus(h),
        ),
    },
    "springsunday": {
        "nav": _navbar_block,
        "bonus": lambda h: _table_block(h, "当前每小时能获得的积分"),
        "parse": lambda h: (
            nexus_php.parse_user_snapshot(h),
            springsunday_mod._parse_springsunday_hourly_bonus(h),
        ),
    },
}


# --------------------------------------------------------------------- 抓取


def _load_sites(db_path: str) -> list[tuple[str, str, str]]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select name, domain, cookies from sites where enabled=1"
        ).fetchall()
    finally:
        con.close()
    return [(str(n), str(d), str(c or "")) for n, d, c in rows]


async def _fetch(domain: str, cookies: str) -> str:
    base = domain.rstrip("/")
    if not base.startswith("http"):
        base = "https://" + base
    jar: dict[str, str] = {}
    for part in cookies.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            jar[k.strip()] = v.strip()
    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        cookies=jar,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
            )
        },
    ) as client:
        resp = await client.get(f"{base}/mybonus.php")
        if resp.status_code >= 400:
            raise RuntimeError(f"{domain} 返回 HTTP {resp.status_code}")
        return resp.text


def _same_snapshot(a: SiteUserSnapshot | None, b: SiteUserSnapshot | None) -> bool:
    if a is None or b is None:
        return a is b
    return (
        a.upload == b.upload
        and a.download == b.download
        and a.seeding_count == b.seeding_count
    )


def _same_bonus(a: object, b: object) -> bool:
    return a == b


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db",
        default=os.environ.get(
            "MEDIALAB_DB",
            str(
                pathlib.Path(__file__).resolve().parents[1]
                / "data-dev"
                / "medialab.db"
            ),
        ),
        help="dev 库路径（默认 data-dev/medialab.db）",
    )
    args = parser.parse_args()
    db_path = pathlib.Path(args.db).expanduser()
    if not db_path.exists():
        print(f"找不到 dev 库：{db_path}", file=sys.stderr)
        return 1

    sites = {name: (domain, cookies) for name, domain, cookies in _load_sites(str(db_path))}
    FIXTURES.mkdir(parents=True, exist_ok=True)
    failed = 0
    for name, spec in _SPECS.items():
        if name not in sites:
            print(f"[{name}] 库里没有该站，跳过")
            continue
        domain, cookies = sites[name]
        try:
            raw = await _fetch(domain, cookies)
        except Exception as exc:
            print(f"[{name}] 抓取失败：{exc}")
            failed += 1
            continue

        # **先整页脱敏 + 裁剪，再抽片段**——反过来（先抽片段再裁剪）会让
        # 片段从标签中间截断，裁剪正则把半个标签当属性吃掉（hhanclub 的
        # headline 曾整段丢失）。
        cleaned_page = trim(anonymize(raw))

        nav_fn = spec["nav"]
        bonus_fn = spec["bonus"]
        parse_fn = spec["parse"]
        try:
            fragment = (
                "<!-- navbar -->\n"
                + str(nav_fn(cleaned_page))
                + "\n<!-- 时魔区块 -->\n"
                + str(bonus_fn(cleaned_page))
            )
        except Exception as exc:
            print(f"[{name}] 裁剪失败：{exc}")
            failed += 1
            continue

        cleaned = trim(fragment)
        # 校验：裁剪后的解析结果必须与**真实整页**一致
        full_snap, full_bonus = parse_fn(raw)
        cut_snap, cut_bonus = parse_fn(cleaned)
        if not _same_snapshot(full_snap, cut_snap):
            print(f"[{name}] 裁剪后 4 业务字段解析不一致：{full_snap} vs {cut_snap}")
            failed += 1
            continue
        if not _same_bonus(full_bonus, cut_bonus):
            print(f"[{name}] 裁剪后时魔解析不一致：{full_bonus} vs {cut_bonus}")
            failed += 1
            continue

        path = FIXTURES / f"{name}_mybonus.html"
        path.write_text(cleaned + "\n", encoding="utf-8")
        print(
            f"[{name}] {len(raw):7d} -> {len(cleaned):6d} bytes  "
            f"时魔={cut_bonus.amount if cut_bonus else None} "
            f"单位={cut_bonus.unit if cut_bonus else None} "
            f"封顶={cut_bonus.capped if cut_bonus else None} "
            f"进度={cut_bonus.progress if cut_bonus else None}"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
