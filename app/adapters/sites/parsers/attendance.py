"""NexusPHP 站点签到解析 helper（FR-3.6）。

NexusPHP 系的「每日签到」端点是 ``/attendance.php``。本模块把页面
HTML 解析成统一的 :class:`~app.adapters.sites.base.CheckinResult`。

**上游语义**（依据 ``xiaomlove/nexusphp`` 的 ``public/attendance.php``）：

- **GET** —— 只查状态。仅当站点配置 ``captcha.attendance.enabled = false``
  （无图形验证码）时，GET 才会顺带执行签到。
- **POST** —— 执行签到。当 ``captcha.attendance.enabled = true`` 时，
  需要 ``imagehash`` + ``imagestring`` 图形验证码。

已签到时站点返回 200 + "您今天已经签到过了，请勿重复刷新"——**这不是错误**，
是幂等结果。

判定顺序（每条都用真实站点页面验证过）：

1. 需要人机验证（Turnstile / 图形验证码）→ ``UNSUPPORTED``
2. 已签到（"今天已经签到过" / "请勿重复刷新"）→ ``ALREADY``
3. 签到成功（"签到成功" **或** 签到统计文案）→ ``SUCCESS``
4. 兜底：页面提到签到但无法识别 → ``FAILED``
5. 页面完全不像签到页 → ``UNSUPPORTED``

.. note::

   **统计文案本身即证明签到成功**——hhanclub 的成功页**不含**"签到成功"
   四字，只有"这是您的第 196 次签到…本次签到获得 10 个憨豆"。
   只认"签到成功"字样会把 hhanclub 误判成失败。

.. warning::

   **不做验证码绕过**（合规红线，见 ``AGENTS.md``）：检测到人机验证时
   返回 ``UNSUPPORTED``（需人工），不做浏览器自动化、不解 Turnstile。
"""

from __future__ import annotations

import re

from app.adapters.sites.base import CheckinResult, CheckinStatus

__all__ = [
    "detect_login_redirect",
    "parse_checkin_result",
]


#: 人机验证特征（HTML 原文里找，不是纯文本里找）。
#:
#: - ``turnstile`` / ``cf-token`` —— Cloudflare Turnstile（audiences / ourbits）
#: - ``imagehash`` —— NexusPHP 自带图形验证码（hdsky 的 ``showup.php``）
#: - ``captcha`` —— 泛化匹配（注意：``captcha_note`` / ``captcha.attendance``
#:   等都命中）
_CAPTCHA_RE = re.compile(
    r"turnstile|cf-token|imagehash|captcha",
    re.IGNORECASE,
)

#: 显式的人机验证提示（纯文本里找，兜底用）。
_CAPTCHA_TEXT = ("人机验证", "验证程序", "安全验证")


#: "已经签到过" 的各种说法（含站点自定义文案）。
_ALREADY_MARKERS = (
    "今天已经签到过",
    "已经签到过了",
    "已经签到过",
    "请勿重复刷新",
    "已签到，请勿重复",
)


#: NexusPHP **navbar 用户信息块**的特征词。
#:
#: 已登录用户访问任何页面，navbar 都会显示「欢迎回来 / UID / 魔力值 /
#: 分享率 / 上传量 / 下载量」。这些词是**登录态的判据**——
#: 站点改版不会动 navbar，所以「页面既没有签到内容、也没有 navbar」
#: 说明**根本没登录**（cookie 失效）。
#:
#: 简繁两种写法都列（audiences 等繁体站用「上傳量」）。
_LOGGED_IN_MARKERS = (
    "欢迎回来",
    "歡迎回來",
    "UID:",
    "魔力值",
    "分享率",
    "上传量",
    "上傳量",
    "下载量",
    "下載量",
)


#: 签到统计文案（NexusPHP 标准模板，3 站实测一致）：
#: "这是您的第 196 次签到，已连续签到 1 天，本次签到获得 10 个憨豆。"
_STATS_RE = re.compile(
    r"这是您的第\s*(\d+)\s*次签到"
    r"[，,]?\s*已连续签到\s*(\d+)\s*天"
    r"[，,]?\s*本次签到获得\s*(\d+)\s*个"
)


def detect_login_redirect(url: str) -> bool:
    """最终 URL 是否跳到了登录页（cookie 失效）。

    NexusPHP 在未登录时把请求重定向到
    ``login.php?returnto=attendance.php``。这是**最可靠**的失效判据
    ——比在 HTML 里找"登录"字样准得多（登录框附近的文案里到处是"登录"）。
    """
    if not url:
        return False
    lowered = url.lower()
    return "login.php" in lowered


def _strip_html(html: str) -> str:
    """去掉 script/style/标签，压缩空白（便于在纯文本里找中文文案）。"""
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def parse_checkin_result(html: str, *, url: str = "") -> CheckinResult:
    """从 ``/attendance.php`` 的 HTML 解析签到结果。

    :param html: 页面 HTML。
    :param url: 最终 URL（用于判定登录跳转）。**强烈建议传入**——
        cookie 失效时页面仍返回 200，光看 HTML 分不清「登录页」和「签到页」。
    """
    if detect_login_redirect(url):
        return CheckinResult(CheckinStatus.AUTH_EXPIRED, "cookie 已失效，需重新登录")

    plain = _strip_html(html)

    # 1) 需要人机验证 → 不支持自动签到（**不做绕过**）
    if _CAPTCHA_RE.search(html) or any(k in plain for k in _CAPTCHA_TEXT):
        return CheckinResult(
            CheckinStatus.UNSUPPORTED, "该站需人机验证，无法自动签到（需人工）"
        )

    # 2) 已签到 → 幂等成功
    if any(marker in plain for marker in _ALREADY_MARKERS):
        return CheckinResult(CheckinStatus.ALREADY, "今日已签到")

    # 3) 签到成功 —— "签到成功" 字样 **或** 统计文案
    stats = _STATS_RE.search(plain)
    if "签到成功" in plain:
        result = CheckinResult(CheckinStatus.SUCCESS, "签到成功")
    elif stats is not None:
        # 统计文案本身即证明签到成功（hhanclub 没有"签到成功"四字）
        result = CheckinResult(CheckinStatus.SUCCESS, "签到成功")
    elif "签到" in plain:
        return CheckinResult(
            CheckinStatus.FAILED, "签到页结构无法识别（站点可能改版）"
        )
    elif not any(marker in plain for marker in _LOGGED_IN_MARKERS):
        # 既没有签到内容，也**没有 navbar 用户信息块** → 根本没登录。
        #
        # hhanclub 在 cookie 失效时既不跳 login.php、也不渲染"登录"字样，
        # 而是回一个**空壳页**（实测 2026-10-22：plain 文本只有 38 字符）。
        # 不判成 AUTH_EXPIRED 的话会被误报成"该站无签到功能"——
        # 那是**错误结论**（站明明有签到功能），会让用户白折腾。
        return CheckinResult(
            CheckinStatus.AUTH_EXPIRED, "cookie 已失效，需重新登录"
        )
    else:
        return CheckinResult(
            CheckinStatus.UNSUPPORTED, "该站无签到功能"
        )

    # 4) 附加统计（累计次数 / 连续天数 / 本次奖励）
    if stats is not None:
        total, streak, reward = (int(g) for g in stats.groups())
        result = CheckinResult(
            status=result.status,
            message=result.message,
            total_count=total,
            streak_days=streak,
            reward=reward,
        )
    return result
