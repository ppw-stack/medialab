#!/usr/bin/env python3
"""抓 hdwing 站点签到相关页面，脱敏 + 裁剪成测试 fixture。

**为什么需要真实页面固化**（AGENTS.md）：签到状态的判据挂在 navbar 的
``<a>`` 标签属性上（``onclick="do_signin(this)"`` / ``>已签到<``），
手写最小 HTML 容易漏掉真实写法（实测未签到时是 ``<b>签 到</b>``，
「签 到」中间**有空格**——按「签到」两字匹配会误判）。

hdwing 的签到协议与其它站不同（**不是** NexusPHP 标准 ``/attendance.php``，
本站该端点 404）：

- ``GET /usercp.php`` —— 取 ``<meta name="x-csrf">`` + navbar 签到状态
- ``POST /plugin_sign-in.php?cmd=signin {csrf}`` —— 执行签到（无需验证码）

脚本做三件事（与 ``capture_mybonus_fixtures.py`` 同款）：

1. **裁剪**：只留 csrf meta + navbar 的 ``div.userinfort`` 片段
2. **脱敏**：csrf → 40 位 0 占位符、用户名 → ``TESTUSER``、UID → ``12345``
3. **自校验**：裁剪后的解析结果必须与整页一致（否则裁剪破坏了判据）

用法::

    # 从 dev 库读 cookie
    uv run python scripts/capture_checkin_fixtures.py --db data-dev/medialab.db

    # 或直接给 cookie 文件
    uv run python scripts/capture_checkin_fixtures.py --cookie-file /path/cookie.txt

    # 「未签到」形态的样本（当前账号已签到时，用之前保存的未签到页面）
    uv run python scripts/capture_checkin_fixtures.py \\
        --cookie-file /path/cookie.txt \\
        --unsigned-source /path/index_unsigned.html

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

from app.adapters.sites.parsers import attendance

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures"

#: 脱敏后的 csrf 占位符——**必须是合法 hex**，否则 ``_CSRF_RE``
#: （``[0-9a-fA-F]+``）提取不到，fixture 就测不了「取 csrf」这一步。
#: 40 个 0 一眼可辨是占位符，不是真实凭据。
_FAKE_CSRF = "0" * 40

#: 替换后的占位 UID
_FAKE_UID = "12345"

#: 形如用户名的纯文本（ASCII 标识符形态）
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,19}$")

#: csrf meta（脱敏目标）
_CSRF_META_RE = re.compile(
    r'(<meta\s+name="x-csrf"\s+content=")([0-9a-fA-F]+)(")'
)

#: navbar 签到状态所在容器（X2015 主题）
_USERINFO_CLASS = 'class="userinfort"'


# --------------------------------------------------------------------- 脱敏


def anonymize(html: str) -> str:
    """抹掉凭据与个人信息（保留结构与文案）。"""
    # csrf：可直接用的会话凭据，必须抹
    out = _CSRF_META_RE.sub(rf"\g<1>{_FAKE_CSRF}\g<3>", html)
    out = re.sub(
        r"((?:logout|plugin_sign-in)\.php\?csrf=)[0-9a-fA-F]+",
        r"\1REDACTED",
        out,
    )
    # 用户名 / UID
    out = re.sub(
        r"(userdetails\.php\?id=\d+[^>]*>)(.*?)(</a>)",
        lambda m: m.group(1) + "TESTUSER" + m.group(3),
        out,
        flags=re.DOTALL,
    )
    out = re.sub(r"(UID[:\s]+)\d+", rf"\g<1>{_FAKE_UID}", out)
    out = re.sub(r"(userdetails\.php\?id=)\d+", rf"\g<1>{_FAKE_UID}", out)
    out = re.sub(
        r"(\?(?:id|uid|userid)=)\d+",
        rf"\g<1>{_FAKE_UID}",
        out,
    )
    out = re.sub(
        r'(alt="|title=")([A-Za-z][A-Za-z0-9_]{2,19})(?=[" ])',
        r"\1TESTUSER",
        out,
    )
    return out


# --------------------------------------------------------------------- 裁剪


def crop(html: str) -> str:
    """只留 csrf meta + navbar 的签到按钮片段。

    保留的是**原始标签与属性**——签到判据就挂在 ``onclick`` 与标签文本上，
    裁成纯文本会让解析器测不到真实路径。
    """
    parts: list[str] = []

    meta = re.search(r'<meta\s+name="x-csrf"[^>]*>', html)
    if meta is not None:
        parts.append(meta.group(0))

    i = html.find(_USERINFO_CLASS)
    if i >= 0:
        start = html.rfind("<div", 0, i)
        end = html.find("</div>", i)
        if start >= 0 and end > 0:
            parts.append(html[start : end + len("</div>")])
        else:
            parts.append(html[i : i + 600])
    else:
        # 兜底：navbar 结构变了 → 抓「签 到」附近
        j = html.find("do_signin")
        if j < 0:
            j = html.find("已签到")
        if j >= 0:
            parts.append(html[max(0, j - 400) : j + 200])

    if not parts:
        raise ValueError("裁剪失败：找不到 csrf meta 也没有 navbar 片段")
    return "\n".join(parts)


def _trim(html: str) -> str:
    """去 script/style/svg，压空白（保留结构与属性）。"""
    out = re.sub(r"<script.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    out = re.sub(r"<style.*?</style>", "", out, flags=re.DOTALL | re.IGNORECASE)
    out = re.sub(r"<svg.*?</svg>", "", out, flags=re.DOTALL | re.IGNORECASE)
    out = re.sub(r"(?:src|xlink:href)=\"data:[^\"]*\"", 'src=""', out)
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n\s*\n+", "\n", out)
    return out.strip()


# --------------------------------------------------------------------- 抓取


def _load_cookies(db_path: str) -> dict[str, str]:
    """从 dev 库读 hdwing 的 cookie（只读）。"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select cookies from sites where adapter='hdwing' and enabled=1"
        ).fetchall()
    finally:
        con.close()
    if not rows:
        raise SystemExit("dev 库里没有启用状态的 hdwing 站点")
    return _parse_cookie(str(rows[0][0] or ""))


def _parse_cookie(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


async def _fetch(url: str, jar: dict[str, str]) -> str:
    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        cookies=jar,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    ) as client:
        resp = await client.get(url)
        if resp.status_code >= 400:
            raise RuntimeError(f"{url} 返回 HTTP {resp.status_code}")
        return resp.text


# --------------------------------------------------------------------- 主流程


async def _run(args: argparse.Namespace) -> None:
    if args.cookie_file:
        jar = _parse_cookie(
            pathlib.Path(args.cookie_file).read_text(encoding="utf-8").strip()
        )
    else:
        jar = _load_cookies(args.db)
    if not jar:
        raise SystemExit("没有 cookie")

    base = "https://hdwing.org"
    raw = await _fetch(f"{base}/usercp.php", jar)

    whole_state = attendance.parse_signin_state(raw)
    if whole_state is None:
        raise SystemExit(
            "整页解析不出签到状态（站点改版？）——先人工确认再更新 fixture"
        )

    cleaned = _trim(anonymize(crop(raw)))
    cropped_state = attendance.parse_signin_state(cleaned)
    if cropped_state != whole_state:
        raise SystemExit(
            f"裁剪破坏了判据：整页 {whole_state} vs 裁剪后 {cropped_state}"
        )
    if not re.search(r'<meta\s+name="x-csrf"', cleaned):
        raise SystemExit("裁剪后丢了 csrf meta")

    target = FIXTURES / "hdwing_signin_state.html"
    target.write_text(cleaned + "\n", encoding="utf-8")
    print(
        f"{target.name:36s} 状态={whole_state} "
        f"{len(raw):7d} -> {len(cleaned):7d} bytes"
    )

    if args.unsigned_source:
        raw_u = pathlib.Path(args.unsigned_source).read_text(encoding="utf-8")
        whole_u = attendance.parse_signin_state(raw_u)
        if whole_u is not False:
            raise SystemExit(
                f"未签到样本解析出来是 {whole_u}（应为 False）——样本不对"
            )
        cleaned_u = _trim(anonymize(crop(raw_u)))
        cropped_u = attendance.parse_signin_state(cleaned_u)
        if cropped_u != whole_u:
            raise SystemExit(
                f"裁剪破坏了判据：整页 {whole_u} vs 裁剪后 {cropped_u}"
            )
        target_u = FIXTURES / "hdwing_signin_state_unsigned.html"
        target_u.write_text(cleaned_u + "\n", encoding="utf-8")
        print(
            f"{target_u.name:36s} 状态={whole_u} "
            f"{len(raw_u):7d} -> {len(cleaned_u):7d} bytes"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data-dev/medialab.db", help="dev 库路径")
    parser.add_argument("--cookie-file", help="cookie 文件（优先于 --db）")
    parser.add_argument(
        "--unsigned-source",
        help="「未签到」形态的原始页面（本地文件），用于固化未签到分支",
    )
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
