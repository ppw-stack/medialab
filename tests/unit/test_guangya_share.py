"""光鸭「分享 → 转存」通道的单元测试（纯逻辑，不碰网络）。

实测结论（2026-11-16，生产 + 本地真机联调）：

- 公开接口只认 **URL 形态**的分享 id（``{shareId}_{账号分享key}``）——
  裸 ``shareId`` 一律 code=112「参数错误」
- **查询串必须剥掉**：``shareUrl`` 形如
  ``https://www.guangyapan.com/s/{shareId}_{key}?code=xxxx``——不剥会让 id
  变成「…_key?code=xxxx」→ 取 token 报「分享链接错误」
- 提取码**必需**（``needCode: true``），否则 code=209「提取码错误」
- 转存落地的文件**保持原名**（不像云下载要落成 ``download`` 再改名）
"""

from __future__ import annotations

from app.adapters.drivers.guangya import share_url_id


def test_share_url_id_parses_url_form() -> None:
    """正常形态：URL 末段就是 URL 形态 id。"""
    assert (
        share_url_id(
            "https://www.guangyapan.com/s/1969344904993595419_auydJtaCXqJvUFqK",
            "1969344904993595419",
        )
        == "1969344904993595419_auydJtaCXqJvUFqK"
    )


def test_share_url_id_strips_query_string() -> None:
    """⚠️ **必须剥掉查询串**——``?code=xxxx`` 会被算进 id（本地真机踩到）。"""
    assert (
        share_url_id(
            "https://www.guangyapan.com/s/1969344960970784855_auydJtaCXqJvUFqK?code=cs8h",
            "1969344960970784855",
        )
        == "1969344960970784855_auydJtaCXqJvUFqK"
    )


def test_share_url_id_falls_back_to_share_id() -> None:
    """URL 异常（空 / 没有下划线）→ 退回裸 shareId（至少能调删除接口）。"""
    assert share_url_id("", "123") == "123"
    assert share_url_id("https://x/s/123", "123") == "123"
    assert share_url_id("https://x/s/123_", "123") == "123_"
