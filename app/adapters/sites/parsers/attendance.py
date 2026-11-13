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

.. note::

   **本模块收录三种协议**（不是所有站都用标准 ``/attendance.php``）：

   - ``parse_checkin_result`` —— NexusPHP 标准 ``/attendance.php``
     （hhanclub / soulvoice / hdhome）
   - ``parse_showup_result`` —— hdsky 自建 ``showup.php`` 三段式（AI 读码）
   - ``parse_plugin_signin_result`` —— hdwing 的 X2015 插件
     ``plugin_sign-in.php``（AJAX 路径，**无需验证码**）
"""

from __future__ import annotations

import re
from typing import Any

from app.adapters.sites.base import CheckinResult, CheckinStatus

__all__ = [
    "UNREASONED_FAILURE",
    "detect_login_redirect",
    "parse_checkin_result",
    "parse_plugin_signin_result",
    "parse_showup_result",
    "parse_showup_sign_state",
    "parse_signin_state",
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

    # 1) 需要人机验证 → 不支持自动签到
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


# ---------------------------------------------------------------- hdsky showup
# hdsky **不用** NexusPHP 标准的 `/attendance.php`，而是自建的一套三段式
# （协议来自站点自己的 `common.js`，2026-10-22 实测）：
#
#   1. ``POST image_code_ajax.php {action:new}``  → {"success":true,"code":"<32 hex>"}
#   2. ``GET  image.php?action=regimage&imagehash=<code>`` → 150x40 PNG 图形验证码
#   3. ``POST showup.php {action:showup, imagehash, imagestring}``
#
# 另有 ``POST showup.php {action:check}`` 可**免验证码**查询今日是否已签
#（该 action 在站点 JS 里是注释掉的，但服务端仍然实现）。

#: hdsky ``showup.php`` 的两种失败码，语义**完全不同**（实测逐条确认）：
#:
#: - ``invalid_imagehash`` —— 验证码**答错**（含大小写错误：站点大小写敏感）
#: - ``date_unmatch``      —— 验证码**答对**，但今日已签到（下一关拦下）
#:
#: 靠这个区别，作业能区分"AI 读错码"（该换一张图重试）与"今天已经签了"
#: （无需重试）。
_SHOWUP_INVALID_CAPTCHA = "invalid_imagehash"
_SHOWUP_ALREADY_SIGNED = "date_unmatch"


def parse_showup_result(payload: Any) -> CheckinResult:
    """解析 hdsky ``showup.php`` 的 JSON 响应。

    :param payload: 已解析的 JSON（``dict``）或原始文本（未登录时会返回
        登录页 HTML，不是 JSON）。
    """
    # 未登录：返回登录页 HTML，压根不是 JSON
    if not isinstance(payload, dict):
        return CheckinResult(
            CheckinStatus.AUTH_EXPIRED, "cookie 已失效，需重新登录"
        )

    if payload.get("success") is True:
        # message 是本次获得的魔力值（站点 JS 用它算连续天数：
        # days = (message - 10) / 2 + 1）
        reward = payload.get("message")
        reward_int = reward if isinstance(reward, int) else None
        return CheckinResult(
            CheckinStatus.SUCCESS,
            f"签到成功（获得 {reward}）" if reward is not None else "签到成功",
            reward=reward_int,
        )

    message = str(payload.get("message") or "")
    if message == _SHOWUP_ALREADY_SIGNED:
        # 验证码通过但今日已签 —— 幂等，不算失败
        return CheckinResult(CheckinStatus.ALREADY, "今日已签到")
    if message == _SHOWUP_INVALID_CAPTCHA:
        return CheckinResult(CheckinStatus.FAILED, "验证码识别错误")
    if not message:
        return CheckinResult(CheckinStatus.FAILED, "showup 响应缺少 message")
    return CheckinResult(CheckinStatus.FAILED, f"签到失败：{message}")


def parse_showup_sign_state(payload: Any) -> bool | None:
    """解析 hdsky ``showup.php {action:check}`` 的签到状态。

    :returns: ``True`` = 今日已签；``False`` = 今日未签；
        ``None`` = 响应不可解读（含 cookie 失效）。
    """
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return None
    sign = payload.get("sign")
    return sign if isinstance(sign, bool) else None


# ---------------------------------------------------------------- hdwing 签到
# hdwing **不用** NexusPHP 标准的 ``/attendance.php``（本站 404），
# 而是用 X2015 主题自带的插件端点 ``plugin_sign-in.php``
# （协议来自站点自己的 ``/plugin/js/base.js`` 的 ``do_signin()``，
# 2026-11-11 生产 cookie 实测）：
#
#   1. ``GET /usercp.php``                 —— 取 ``<meta name="x-csrf">``
#                                            + 看 navbar 是「签 到」还是「已签到」
#   2. ``POST /plugin_sign-in.php?cmd=signin {csrf}`` —— 执行签到
#                                            （**不需要图形验证码**）
#
# 响应（JSON）实测四种：
#
# - ``{"state":"success","signindays":0,"integral":"10"}`` —— 签到成功
# - ``{"state":false}``                     —— 今日已签（**无 msg**）
# - ``{"state":false,"msg":"不正确的CSRF ..."}`` —— csrf 不对
# - （无 cookie 时返回登录页 HTML，不是 JSON）
#
# ⚠️ ``{"state":false}`` **同时**表示「今日已签」与「失败但站点没给原因」——
# 两者同形，无法从响应本身区分。故解析层把它标成
# :data:`UNREASONED_FAILURE`，由调用方**二次查状态**再定性（宁可多一次
# 请求，也不要把幂等的「已签」误报成失败，或把真失败静默吞掉）。

#: 「失败但站点没给原因」的固定文案 —— 调用方据此二次判定是否已签到
UNREASONED_FAILURE = "签到失败：站点未给出原因"

#: navbar 里的「已签到」标记（hdwing 实测形态：
#: ``<a class="label label-default" href="#">已签到</a>``）。
#:
#: 未签到时该位置是 ``<a … onclick="do_signin(this);"><b>签 到</b></a>``
#: ——「签 到」中间有空格且包在 ``<b>`` 里，**不会**命中本正则。
_SIGNED_IN_RE = re.compile(r"<a[^>]*>\s*已签到\s*</a>")

#: 未签到时的「签 到」按钮（``onclick="do_signin(this)"``）
_SIGNIN_BUTTON_RE = re.compile(r"do_signin\s*\(")


def parse_signin_state(html: str) -> bool | None:
    """解析 hdwing navbar 的签到状态。

    :returns: ``True`` = 今日已签；``False`` = 今日未签；
        ``None`` = 页面里两个特征都没找到（**站点改版**）。

    ``None`` 不能当成 ``False`` —— 那是「页面结构变了」，不是「没签到」。
    盲目签到会对着未知页面乱打 POST，故调用方必须区别对待。
    """
    if _SIGNED_IN_RE.search(html):
        return True
    if _SIGNIN_BUTTON_RE.search(html):
        return False
    return None


def _to_int(value: Any) -> int | None:
    """宽松转 int（站点的 ``integral`` 是字符串 ``"10"``）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            return int(text)
    return None


def parse_plugin_signin_result(payload: Any) -> CheckinResult:
    """解析 hdwing ``plugin_sign-in.php?cmd=signin`` 的 JSON 响应。

    :param payload: 已解析的 JSON（``dict``）或原始文本（未登录时返回
        登录页 HTML，不是 JSON）。
    """
    # 未登录：返回登录页 HTML，压根不是 JSON
    if not isinstance(payload, dict):
        return CheckinResult(
            CheckinStatus.AUTH_EXPIRED, "cookie 已失效，需重新登录"
        )

    if payload.get("state") == "success":
        reward = _to_int(payload.get("integral"))
        streak = _to_int(payload.get("signindays"))
        message = "签到成功"
        if reward is not None:
            message = f"签到成功（获得 {reward} 魔力）"
        return CheckinResult(
            CheckinStatus.SUCCESS,
            message,
            streak_days=streak,
            reward=reward,
        )

    msg = str(payload.get("msg") or "")
    if msg:
        return CheckinResult(CheckinStatus.FAILED, f"签到失败：{msg}")

    # ``{"state":false}`` —— 「今日已签」与「失败无原因」同形
    return CheckinResult(CheckinStatus.FAILED, UNREASONED_FAILURE)
