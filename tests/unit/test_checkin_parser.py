"""站点签到解析 helper 的单元测试（FR-3.6）。

**用真实站点页面做 fixture**（2026-10-22 抓取，见 ``tests/fixtures/``）——
不手写最小 HTML，因为真实页面里那些"意外的文案"正是判定规则的坑。

最关键的一条：**hhanclub 的成功页不含"签到成功"四字**，只有
"这是您的第 196 次签到…本次签到获得 10 个憨豆"。只认"签到成功"字样
会把 hhanclub 误判成失败——这个坑必须用测试钉死。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.adapters.sites.base import CheckinStatus
from app.adapters.sites.parsers.attendance import (
    UNREASONED_FAILURE,
    detect_login_redirect,
    parse_checkin_result,
    parse_plugin_signin_result,
    parse_showup_result,
    parse_showup_sign_state,
    parse_signin_state,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ------------------------------------------------------------------ 状态判定


def test_soulvoice_success_page() -> None:
    """soulvoice：页面上有"签到成功"字样 → SUCCESS。"""
    result = parse_checkin_result(
        _load("soulvoice_attendance.html"),
        url="https://pt.soulvoice.club/attendance.php",
    )
    assert result.status == CheckinStatus.SUCCESS


def test_hhanclub_success_page_detected_by_stats_only() -> None:
    """hhanclub：成功页**没有**"签到成功"四字，只有统计文案。

    这是实测发现的坑——统计文案本身就证明签到成功了。
    """
    html = _load("hhanclub_attendance.html")
    assert "签到成功" not in html, "fixture 前提变了：hhanclub 页面现在含'签到成功'"

    result = parse_checkin_result(
        html, url="https://hhanclub.net/attendance.php"
    )
    assert result.status == CheckinStatus.SUCCESS


def test_hdhome_already_page() -> None:
    """hdhome：当日已签 → ALREADY（幂等，不算失败）。"""
    result = parse_checkin_result(
        _load("hdhome_attendance.html"),
        url="https://hdhome.org/attendance.php",
    )
    assert result.status == CheckinStatus.ALREADY


def test_audiences_turnstile_is_unsupported() -> None:
    """audiences：Cloudflare Turnstile → UNSUPPORTED（不做绕过）。"""
    result = parse_checkin_result(
        _load("audiences_attendance.html"),
        url="https://audiences.me/attendance.php",
    )
    assert result.status == CheckinStatus.UNSUPPORTED
    assert "人工" in result.message


def test_ourbits_turnstile_is_unsupported() -> None:
    """ourbits：Turnstile + 签到表单 → UNSUPPORTED。"""
    result = parse_checkin_result(
        _load("ourbits_attendance.html"),
        url="https://ourbits.club/attendance.php",
    )
    assert result.status == CheckinStatus.UNSUPPORTED


def test_login_redirect_is_auth_expired() -> None:
    """cookie 失效（跳 login.php）→ AUTH_EXPIRED。

    判据是**最终 URL**，不是 HTML 里的"登录"字样——登录页附近到处是
    "登录"文案，光看 HTML 会误判。
    """
    result = parse_checkin_result(
        _load("hhanclub_login.html"),
        url="https://hhanclub.net/login.php?returnto=attendance.php",
    )
    assert result.status == CheckinStatus.AUTH_EXPIRED


def test_empty_shell_page_is_auth_expired() -> None:
    """hhanclub cookie 失效时回**空壳页**（不跳 login.php、也无"登录"字样）。

    实测：plain 文本只有 38 字符，既没有签到内容，也没有 navbar 用户信息块。

    判据是 navbar 的登录态特征词（"欢迎回来" / "UID:" / "魔力值" …）——
    站点改版不会动 navbar，所以"既无签到内容又无 navbar"= 没登录。

    不判成 AUTH_EXPIRED 会被误报成"该站无签到功能"，那是**错误结论**
    （站明明有签到功能），会让用户白折腾。
    """
    result = parse_checkin_result(
        _load("hhanclub_login.html"),
        url="https://hhanclub.net/attendance.php",
    )
    assert result.status == CheckinStatus.AUTH_EXPIRED


def test_page_with_navbar_but_no_checkin_is_unsupported() -> None:
    """有 navbar（已登录）但没签到内容 → UNSUPPORTED（真的没这功能）。

    与上面的空壳页区分开：logged-in 才能断言"该站不支持签到"。
    """
    html = (
        "<html><body>"
        "<div>欢迎回来, tester (UID: 12345) 魔力值 [使用]: 1,000.5 "
        "分享率：1.234 上传量：10.00 GB 下载量：20.00 GB</div>"
        "</body></html>"
    )
    result = parse_checkin_result(html, url="https://x.test/attendance.php")
    assert result.status == CheckinStatus.UNSUPPORTED


def test_detect_login_redirect() -> None:
    assert detect_login_redirect("https://x.test/login.php?returnto=attendance.php")
    assert not detect_login_redirect("https://x.test/attendance.php")
    assert not detect_login_redirect("")


# ------------------------------------------------------------------ 统计提取


def test_stats_extracted_from_hhanclub() -> None:
    """hhanclub：第 196 次签到 / 连续 1 天 / 10 个憨豆。"""
    result = parse_checkin_result(
        _load("hhanclub_attendance.html"),
        url="https://hhanclub.net/attendance.php",
    )
    assert result.total_count == 196
    assert result.streak_days == 1
    assert result.reward == 10


def test_stats_extracted_from_soulvoice() -> None:
    """soulvoice：第 614 次签到 / 连续 1 天 / 10 个魔力值。"""
    result = parse_checkin_result(
        _load("soulvoice_attendance.html"),
        url="https://pt.soulvoice.club/attendance.php",
    )
    assert result.total_count == 614
    assert result.streak_days == 1
    assert result.reward == 10


def test_already_page_has_no_stats() -> None:
    """已签到页（hdhome）没有统计文案 → 统计字段为 None。"""
    result = parse_checkin_result(
        _load("hdhome_attendance.html"),
        url="https://hdhome.org/attendance.php",
    )
    assert result.total_count is None
    assert result.reward is None


# ------------------------------------------------------------------ 兜底


def test_unknown_page_is_unsupported() -> None:
    """有 navbar（已登录）但完全不像签到页 → UNSUPPORTED（不是 FAILED）。

    页面里连"签到"二字都没有，且**有** navbar 用户信息块，说明该站
    没有签到功能——这是**正常状态**，不是故障。
    """
    html = (
        "<html><body><p>hello</p>"
        "<div>欢迎回来, tester (UID: 12345) 魔力值: 1,000</div>"
        "</body></html>"
    )
    result = parse_checkin_result(html, url="https://x.test/attendance.php")
    assert result.status == CheckinStatus.UNSUPPORTED


def test_bare_page_without_navbar_is_auth_expired() -> None:
    """既无签到内容、又无 navbar → AUTH_EXPIRED（没登录）。

    见 :func:`test_empty_shell_page_is_auth_expired` 的说明。
    """
    result = parse_checkin_result(
        "<html><body><p>hello</p></body></html>", url="https://x.test/attendance.php"
    )
    assert result.status == CheckinStatus.AUTH_EXPIRED


def test_unrecognized_checkin_page_is_failed() -> None:
    """页面提到"签到"但认不出结果 → FAILED（站点改版，需要人看）。"""
    result = parse_checkin_result(
        "<html><body><p>签到</p><p>???</p></body></html>",
        url="https://x.test/attendance.php",
    )
    assert result.status == CheckinStatus.FAILED


@pytest.mark.parametrize(
    "html",
    [
        '<div class="cf-turnstile" data-sitekey="x"></div>',
        '<form><input type="hidden" name="imagehash" value=""></form>',
        "<p>请完成人机验证</p>",
    ],
)
def test_captcha_markers_detected(html: str) -> None:
    """人机验证的各种形态 → UNSUPPORTED（本仓库不实现 Turnstile）。"""
    result = parse_checkin_result(html, url="https://x.test/attendance.php")
    assert result.status == CheckinStatus.UNSUPPORTED


def test_captcha_detection_wins_over_success_marker() -> None:
    """页面同时含"签到成功"与人机验证特征 → 判 UNSUPPORTED。

    Turnstile 页面可能在别处带"签到"字样，但人机验证没通过就等于没签到，
    不能让"签到成功"字样误导成成功。
    """
    result = parse_checkin_result(
        '<p>签到成功</p><div class="cf-turnstile"></div>',
        url="https://x.test/attendance.php",
    )
    assert result.status == CheckinStatus.UNSUPPORTED


# ----------------------------------------------------- hdsky showup 协议

# hdsky 不用 NexusPHP 标准的 /attendance.php（本站 404），而是自建的三段式
# JSON 协议。**最关键的一点**：服务端对"验证码答错"与"今日已签"返回
# **不同的 message**——作业靠这个区分"换验证码重试"与"已签到"，两个都当失败
# 处理会让作业永远显示失败。


def test_showup_success() -> None:
    """签到成功：message 是本次获得的魔力值。"""
    result = parse_showup_result({"success": True, "message": 10})
    assert result.status == CheckinStatus.SUCCESS
    assert result.reward == 10
    assert "10" in result.message


def test_showup_already_signed_is_not_failure() -> None:
    """``date_unmatch`` = 验证码**答对**但今日已签 → ALREADY（幂等）。"""
    result = parse_showup_result({"success": False, "message": "date_unmatch"})
    assert result.status == CheckinStatus.ALREADY


def test_showup_invalid_captcha_is_failure() -> None:
    """``invalid_imagehash`` = 验证码**答错** → FAILED（调用方换图重试）。"""
    result = parse_showup_result({"success": False, "message": "invalid_imagehash"})
    assert result.status == CheckinStatus.FAILED
    assert "验证码" in result.message


def test_showup_already_and_wrong_are_distinguishable() -> None:
    """两个码必须能区分——这是重试逻辑的前提。"""
    wrong = parse_showup_result({"success": False, "message": "invalid_imagehash"})
    already = parse_showup_result({"success": False, "message": "date_unmatch"})
    assert wrong.status is not already.status
    assert wrong.status == CheckinStatus.FAILED
    assert already.status == CheckinStatus.ALREADY


def test_showup_login_page_is_auth_expired() -> None:
    """未登录时 showup 返回登录页 HTML（不是 JSON）→ AUTH_EXPIRED。"""
    result = parse_showup_result("<html><body>登录</body></html>")
    assert result.status == CheckinStatus.AUTH_EXPIRED


def test_showup_unknown_message_is_failure() -> None:
    """未知 message 也要如实报失败（不静默当成功）。"""
    result = parse_showup_result({"success": False, "message": "something_new"})
    assert result.status == CheckinStatus.FAILED
    assert "something_new" in result.message


def test_showup_missing_message_is_failure() -> None:
    result = parse_showup_result({"success": False})
    assert result.status == CheckinStatus.FAILED


@pytest.mark.parametrize(
    ("payload", "expect"),
    [
        ({"success": True, "sign": False}, False),
        ({"success": True, "sign": True}, True),
        ({"success": False, "message": "date_unmatch"}, None),
        ("<html>登录页</html>", None),
        ({"success": True}, None),
        ({"success": True, "sign": "yes"}, None),
    ],
)
def test_showup_sign_state(payload: object, expect: bool | None) -> None:
    """``{action:check}`` 的签到状态解析；不可解读时返回 None（含 cookie 失效）。"""
    assert parse_showup_sign_state(payload) is expect


# --------------------------------------------- hdwing plugin_sign-in 协议

# hdwing 既不用 NexusPHP 标准的 /attendance.php（本站 404），也不用 hdsky
# 的 showup 三段式，而是 X2015 主题自带的插件端点 plugin_sign-in.php：
#
#   GET /usercp.php                              → csrf meta + navbar 签到状态
#   POST /plugin_sign-in.php?cmd=signin {csrf}  → {"state":...}
#
# **不需要图形验证码**（实测裸 csrf 即通过）。fixture 是 2026-11-11
# 生产 cookie 实测的页面裁剪（见 scripts/capture_checkin_fixtures.py）。


def test_hdwing_signed_page_detected() -> None:
    """navbar 上是「已签到」→ True。"""
    assert parse_signin_state(_load("hdwing_signin_state.html")) is True


def test_hdwing_unsigned_page_detected() -> None:
    """navbar 上是「签 到」按钮（``onclick="do_signin(this)"``）→ False。

    ⚠️ 未签到时按钮文本是 ``<b>签 到</b>``（中间有空格）——按「签到」
    两字匹配会漏判，故判据挂在 ``onclick`` 上。
    """
    assert parse_signin_state(_load("hdwing_signin_state_unsigned.html")) is False


def test_hdwing_unknown_page_is_none() -> None:
    """两个特征都不在 → None（**不是** False）。

    那是「页面结构变了」，不是「没签到」——当成 False 会对着未知页面盲签。
    """
    assert parse_signin_state("<html><body>???</body></html>") is None


def test_hdwing_signin_success() -> None:
    """签到成功 → SUCCESS，并带出连续天数与本次获得的魔力值。

    ``integral`` 在真实响应里是**字符串** ``"10"``——必须能转成 int。
    """
    result = parse_plugin_signin_result(
        {"state": "success", "signindays": 0, "integral": "10"}
    )
    assert result.status == CheckinStatus.SUCCESS
    assert result.reward == 10
    assert result.streak_days == 0
    assert "10" in result.message


def test_hdwing_signin_already_is_unreasoned_failure() -> None:
    """``{"state":false}``（今日已签，**无 msg**）→ 标记为「失败但无原因」。

    它与「真失败」同形，**不能**直接判 ALREADY——那会把真失败静默吞掉。
    调用方据此二次查状态再定性（见 ``test_site_adapters.py``）。
    """
    result = parse_plugin_signin_result({"state": False})
    assert result.status == CheckinStatus.FAILED
    assert result.message == UNREASONED_FAILURE


def test_hdwing_signin_csrf_error_is_failure_with_reason() -> None:
    """csrf 不对 → FAILED，且**带上站点的原文**（便于排查）。"""
    result = parse_plugin_signin_result(
        {"state": False, "msg": "不正确的CSRF / Incorrect CSRF token"}
    )
    assert result.status == CheckinStatus.FAILED
    assert result.message != UNREASONED_FAILURE
    assert "CSRF" in result.message


def test_hdwing_signin_login_page_is_auth_expired() -> None:
    """未登录时返回登录页 HTML（不是 JSON）→ AUTH_EXPIRED。"""
    result = parse_plugin_signin_result("<html><body>登录</body></html>")
    assert result.status == CheckinStatus.AUTH_EXPIRED
