#!/usr/bin/env python3
"""把真实站点页面脱敏 + 裁剪成测试 fixture。

真实页面有两个问题：太大、含个人信息。

**太大**：hhanclub 的 ``attendance.php`` 有 293KB，其中 170KB 是站点自己的
导航 / CSS / 内联 SVG。直接进 git 太重。

**含个人信息**：页面 navbar 里有真实**用户名**（``ppwHhc`` / ``ppwWb`` /
``ppwLy`` / ``qbz``）和 **UID**（15759 / 54635 / 127548 / 125417 / 22187）。
这些是个人标识，**不该进 git**（R6：密钥与敏感数据严禁入库）。

但**手写最小 HTML** 会漏掉真实文案（hhanclub 成功页不含"签到成功"四字、
cookie 失效时回空壳页……），那些恰恰是判定规则要覆盖的坑。

故本脚本做两件事：

1. **脱敏**：把用户名 / UID 替换成占位符（``TESTUSER`` / ``12345``），
   保留结构与上下文，不破坏任何解析特征
2. **裁剪**：去掉 ``<script>`` / ``<style>`` / ``<svg>`` 块与 data URI

**不能删 ``<form>`` / ``<input>`` 等标签**——人机验证标记
（turnstile / cf-token / imagehash）就挂在那些标签的属性上，解析器要靠
**原始 HTML**（不是纯文本）判定。

用法::

    uv run python scripts/trim_checkin_fixtures.py
"""
from __future__ import annotations

import pathlib
import re

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures"

#: 替换后的占位 UID（保持数字形态，避免改动解析上下文）
_FAKE_UID = "12345"

#: 替换后的占位 csrf —— **必须是合法 hex**（``_CSRF_RE`` 匹配
#: ``[0-9a-fA-F]+``），否则 fixture 测不了「从页面取 csrf」这一步。
#: 40 个 0 一眼可辨是占位符，不是真实凭据。
_FAKE_CSRF = "0" * 40


def anonymize(html: str) -> str:
    """把用户名与 UID 替换成占位符。

    只动**标识符本身**，不动周围的结构与文案——解析规则关心的
    「UID: 在不在」「用户名在不在」这两个特征必须保持。
    """
    # 用户名：``userdetails.php?id=X`` 链接内的文本（含嵌套 <b> / <img>）
    out = re.sub(
        r'(userdetails\.php\?id=\d+[^>]*>)(.*?)(</a>)',
        lambda m: m.group(1) + "TESTUSER" + m.group(3),
        html,
        flags=re.DOTALL,
    )
    # ``欢迎回来, <b>name</b>`` / ``欢迎回来, name``
    out = re.sub(
        r"(欢迎回来[,，]\s*(?:<[^>]+>\s*)*)([^<>\s,，]{1,20})",
        r"\1TESTUSER",
        out,
    )
    # UID 数字（``UID: 125417`` 与 ``userdetails.php?id=125417``）
    out = re.sub(r"(UID:\s*)\d+", rf"\g<1>{_FAKE_UID}", out)
    out = re.sub(r"(userdetails\.php\?id=)\d+", rf"\g<1>{_FAKE_UID}", out)
    # 站点自有 UID 变量（ourbits 的 ``userId`` hidden input）
    out = re.sub(r'(name="userId"[^>]*value=")\d+', rf"\g<1>{_FAKE_UID}", out)
    # 侧栏里的**其他用户**名（audiences 的上传者列表）——不是账号本人，
    # 但仍是真实用户名，一律匿名化
    out = re.sub(
        r'(alt="|title="|data-uploader-label=")([A-Za-z][A-Za-z0-9_]{2,19})(?=[" ])',
        r"\1TESTUSER",
        out,
    )
    # 其余 URL 里的用户 ID（``invite.php?id=`` / ``claim.php?uid=`` …）
    out = re.sub(
        r"(\?(?:id|uid)=)\d+",
        rf"\g<1>{_FAKE_UID}",
        out,
    )
    # **会话 token**（``logout.php?token=``）——这是能直接用的凭据，必须抹掉
    out = re.sub(
        r"((?:logout|takelogout)\.php\?token=)[A-Za-z0-9]+",
        r"\1REDACTED",
        out,
    )
    # **csrf**（``<meta name="x-csrf">`` / ``logout.php?csrf=``）——同上，
    # 是能直接用的凭据。meta 里的换成**合法 hex 占位符**（解析器要能
    # 提取到），URL 里的直接抹掉。
    out = re.sub(
        r'(<meta\s+name="x-csrf"\s+content=")[0-9a-fA-F]+(")',
        rf"\g<1>{_FAKE_CSRF}\g<2>",
        out,
    )
    out = re.sub(
        r"((?:logout|plugin_sign-in)\.php\?csrf=)[0-9a-fA-F]+",
        r"\1REDACTED",
        out,
    )
    return out


def trim(html: str) -> str:
    """裁剪：去 script/style/svg，压空白。

    **不能删 ``<form>`` / ``<input>`` 等标签**——人机验证标记
    （turnstile / cf-token / imagehash）就挂在那些标签的属性上，解析器要靠
    **原始 HTML**（不是纯文本）判定。
    """
    out = re.sub(r"<script.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    out = re.sub(r"<style.*?</style>", "", out, flags=re.DOTALL | re.IGNORECASE)
    out = re.sub(r"<svg.*?</svg>", "", out, flags=re.DOTALL | re.IGNORECASE)
    # 残留的 data: URI（内联图片）
    out = re.sub(r'(?:src|xlink:href)="data:[^"]*"', 'src=""', out)
    out = re.sub(r"[ 	]+", " ", out)
    out = re.sub(r"\n\s*\n+", "\n", out)
    return out.strip()


def main() -> None:
    for path in (
        sorted(FIXTURES.glob("*_attendance.html"))
        + sorted(FIXTURES.glob("*_login.html"))
        # hdwing 的签到状态页（navbar 片段 + csrf meta）
        + sorted(FIXTURES.glob("*_signin_state*.html"))
    ):
        original = path.read_text(encoding="utf-8")
        cleaned = trim(anonymize(original))
        path.write_text(cleaned + "\n", encoding="utf-8")
        print(
            f"{path.name:34s} {len(original):7d} -> {len(cleaned):7d} bytes"
        )


if __name__ == "__main__":
    main()
