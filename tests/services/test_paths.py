"""路径模板渲染与安全校验测试（FR-2.3 / FR-5.2.5c）。

覆盖：正常渲染、非法字符清洗、**路径穿越拦截**、根目录归属判定。
"""

from __future__ import annotations

import pytest
from app.services.paths import (
    PathRenderError,
    device_of,
    is_within,
    render_save_path,
    sanitize_segment,
    sanitize_torrent_name,
)


# ------------------------------------------------------------ 正常渲染
def test_render_basic_template() -> None:
    r = render_save_path(
        "/data/torrents/{site}/{name}", site="example-pt", name="Some.Movie.2024"
    )
    assert str(r.path) == "/data/torrents/example-pt/Some.Movie.2024"


def test_render_with_category_and_hash() -> None:
    r = render_save_path(
        "/data/{site}/{category}/{name}",
        site="pt",
        category="cross-seed",
        name="X",
        info_hash="a" * 40,
    )
    assert str(r.path) == "/data/pt/cross-seed/X"


def test_literal_template_allowed() -> None:
    r = render_save_path("/data/fixed-dir", site="s", name="n")
    assert str(r.path) == "/data/fixed-dir"


def test_hyphen_preserved_in_site_name() -> None:
    """站点名常带连字符（``example-pt``），曾被零宽字符正则误删。

    回归用例：正则字符类写成 ``[-\\u200b...]`` 会把 ``-`` 也删掉。
    """
    assert sanitize_segment("example-pt") == "example-pt"
    r = render_save_path("/data/{site}/{name}", site="example-pt", name="n")
    assert "example-pt" in str(r.path)


# ------------------------------------------------------------ 非法字符清洗
def test_illegal_chars_replaced() -> None:
    assert sanitize_segment('a<b>c:d"e|f?g*h') == "a_b_c_d_e_f_g_h"


def test_zero_width_stripped() -> None:
    assert sanitize_segment("na\u200bme") == "name"
    assert sanitize_segment("a\u202eb") == "ab"


def test_fullwidth_normalized() -> None:
    assert sanitize_segment("ＡＢ") == "AB"


def test_trailing_dot_stripped() -> None:
    assert sanitize_segment("name.") == "name"


def test_long_segment_truncated() -> None:
    out = sanitize_segment("x" * 500)
    assert len(out) <= 128


def test_empty_falls_back() -> None:
    assert sanitize_segment("", fallback="fb") == "fb"
    assert sanitize_segment("   ", fallback="fb") == "fb"


def test_control_chars_removed() -> None:
    assert "\n" not in sanitize_torrent_name("a\nb")


# ------------------------------------------------------------ 路径穿越拦截
@pytest.mark.parametrize(
    "template",
    [
        "../etc/{name}",
        "/data/../etc/{name}",
        "/data/torrents/../../etc/{name}",
        "data/relative/{name}",
        r"C:\data\{name}",
        "/data/{unknown}",
        "/data/{__class__}",
        "/data/{",
        "",
        "   ",
        "/",
    ],
)
def test_bad_templates_rejected(template: str) -> None:
    with pytest.raises(PathRenderError):
        render_save_path(template, site="s", name="n")


def test_traversal_in_name_is_neutralized() -> None:
    """种子名里的 ``../`` 不得逃逸出模板根。

    注意：这里不是"抛错"而是"被中和"——``/`` 已被替换为 ``_``，
    因此结果仍落在模板目录下。任何情况下都不会穿越。
    """
    r = render_save_path(
        "/data/{site}/{name}", site="s", name="../../../etc/passwd"
    )
    assert str(r.path).startswith("/data/s/")
    assert ".." not in str(r.path).replace(".._", "").replace("..", "X")
    assert "passwd" in str(r.path)


def test_absolute_path_in_name_is_neutralized() -> None:
    """种子名以 ``/`` 开头时不得逃逸：分隔符被替换为 ``_``。"""
    r = render_save_path("/data/{name}", site="s", name="/etc/shadow")
    assert str(r.path) == "/data/_etc_shadow"


def test_shallow_path_stays_absolute_under_root() -> None:
    """模板只有一层时（``/{name}``）依然合法，但结果仍是绝对路径。"""
    r = render_save_path("/{name}", site="s", name="n")
    assert str(r.path) == "/n"


# ------------------------------------------------------------ 根目录归属
def test_is_within_true_for_child() -> None:
    assert is_within("/data/torrents", "/data/torrents/pt/a") is True


def test_is_within_false_for_sibling_prefix() -> None:
    """/data/torrents2 不应被视为 /data/torrents 的子目录（前缀相同但层级不同）。"""
    assert is_within("/data/torrents", "/data/torrents2/a") is False


def test_is_within_false_for_self() -> None:
    assert is_within("/data/torrents", "/data/torrents") is False


def test_is_within_false_for_parent() -> None:
    assert is_within("/data/torrents/pt", "/data/torrents") is False


def test_is_within_requires_absolute_root() -> None:
    assert is_within("relative", "/data/x") is False


def test_device_of_missing_path_returns_none() -> None:
    assert device_of("/definitely/not/here/xyz") is None


def test_device_of_existing(tmp_path) -> None:
    assert device_of(str(tmp_path)) == tmp_path.stat().st_dev