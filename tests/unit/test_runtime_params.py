"""运行时参数（runtime_params）契约测试。

覆盖两类东西：

1. **枚举参数契约**（2026-10-23 加入）——0/1 这类「在几个具名行为间二选一」
   的参数用 ``options`` 声明取值，前端据此渲染**单选按钮组**而非数字输入框。
   这里锁住：options 与 min/max 一致、默认值在 options 里、取值不重复。

2. **校验契约**——``validate`` 的三道关：参数已注册 / 是整数 / 在合法范围内
   （枚举参数还必须在 options 里）。

3. **启动期加载的收敛**（2026-10-23 加入）——启动加载路径不经过
   ``validate``（那是 API 写入路径的校验），脏数据必须由
   ``_sanitize_loaded`` 收敛到 default，而不是直接进内存。

注意：``_assert_enum_options_consistent()`` 是**导入时**执行的——spec 写错
会在 import 阶段直接抛 RuntimeError，故这里测的是「现有 spec 全部合规」，
而不是「写错会抛」（后者由该函数自身的存在保证）。
"""

from __future__ import annotations

import pytest
from app.services.runtime_params import (
    PARAM_SPECS,
    ParamSpec,
    _sanitize_loaded,
    lookup_spec,
    specs_for_job,
    validate,
)

# ------------------------------------------------------- 枚举参数契约


def test_enum_params_options_match_min_max() -> None:
    """枚举取值必须全部落在 [min, max] 内，且不重复。"""
    for spec in PARAM_SPECS:
        if spec.options is None:
            continue
        values = [v for v, _ in spec.options]
        assert len(values) == len(set(values)), f"{spec.key} 的 options 取值重复"
        for value in values:
            assert spec.min <= value <= spec.max, (
                f"{spec.key} 的 options 取值 {value} 超出 {spec.min}–{spec.max}"
            )


def test_enum_params_default_in_options() -> None:
    """默认值必须是某个选项的值——否则 radio 渲染不出选中项。"""
    for spec in PARAM_SPECS:
        if spec.options is None:
            continue
        values = [v for v, _ in spec.options]
        assert spec.default in values, (
            f"{spec.key} 的默认值 {spec.default} 不在 options {values} 里"
        )


def test_enum_params_have_options_and_labels() -> None:
    """枚举参数：每个选项都要有显示名（空名用户看不懂）。"""
    for spec in PARAM_SPECS:
        if spec.options is None:
            continue
        for value, text in spec.options:
            assert text.strip(), f"{spec.key} 的选项 {value} 缺少显示名"


def test_continuous_params_have_no_options() -> None:
    """连续数值参数不带 options（前端渲染数字输入框）。"""
    for spec in PARAM_SPECS:
        if spec.min == spec.max:
            continue
        # min != max 说明是连续区间，不该声明枚举
        if spec.max - spec.min > 1:
            assert spec.options is None, (
                f"{spec.key} 是连续数值参数，不该有 options"
            )


def test_enum_specs_are_the_zero_one_ones() -> None:
    """当前所有 0/1 枚举参数——新增枚举参数时同步更新本断言."""
    enum_keys = {s.key for s in PARAM_SPECS if s.options is not None}
    assert enum_keys == {
        "monitor_delete_expired",
        "disk_rotation_action",
        "disk_rotation_dry_run",
        "scrape_enabled",
        "scrape_policy",
        "organize_enabled",
        "organize_action",
        "organize_conflict_policy",
        "scrape_season_image_to_root",
        "scrape_original_image",
        "aggregate_transfer_mode",
    }


def test_aggregate_max_submissions_spec() -> None:
    """聚合归拢的「单轮提交上限」参数规格。

    - 归属 ``aggregate_sync``（只出现在该作业的「配置」弹框里）
    - 默认 20（无人值守的运行时保护）
    - **0 = 不限**（与仓库「0 = 不限」的惯例一致）
    """
    spec = lookup_spec("aggregate_max_submissions")
    assert spec is not None
    assert spec.job == "aggregate_sync"
    assert spec.default == 20
    assert spec.min == 0
    assert spec.max == 200
    assert spec.options is None  # 连续数值 → 数字输入框
    assert spec.note and "0 = 不限" in spec.note


def test_monitor_delete_expired_options() -> None:
    """删种子的枚举：0=仅暂停（默认）/ 1=删除任务+删数据。"""
    spec = lookup_spec("monitor_delete_expired")
    assert spec is not None
    assert spec.options == ((0, "仅暂停"), (1, "删除任务+删数据"))
    assert spec.default == 0, "默认必须是仅暂停（删除不可逆）"
    assert spec.note, "删除不可逆，必须有风险提示"


def test_specs_for_job_keeps_enum_options() -> None:
    """按作业取 spec 时，枚举信息不能丢（前端要靠它渲染 radio）。"""
    for spec in specs_for_job("monitor_free_window"):
        if spec.key == "monitor_delete_expired":
            assert spec.options is not None
            assert spec.note
    # 注：``push_within_bucket_order`` 已随「时魔」功能删除（2026-11-20），
    # 推送作业不再有枚举型运行参数。


# ------------------------------------------------------- 校验契约


def test_validate_accepts_enum_values() -> None:
    """枚举的合法取值要能通过校验。"""
    validate({"monitor_delete_expired": 0})
    validate({"monitor_delete_expired": 1})


def test_validate_rejects_unregistered_key() -> None:
    with pytest.raises(ValueError, match="未注册"):
        validate({"no_such_param": 1})


def test_validate_rejects_non_int() -> None:
    with pytest.raises(ValueError, match="整数"):
        validate({"monitor_delete_expired": "1"})  # type: ignore[dict-item]


def test_validate_rejects_bool() -> None:
    """bool 是 int 的子类——必须显式拒绝（True 会被当成 1 存进去）。"""
    with pytest.raises(ValueError, match="整数"):
        validate({"monitor_delete_expired": True})  # type: ignore[dict-item]


def test_validate_rejects_out_of_range() -> None:
    with pytest.raises(ValueError, match="之间"):
        validate({"monitor_delete_expired": 2})


def test_validate_rejects_negative() -> None:
    with pytest.raises(ValueError, match="之间"):
        validate({"monitor_pre_pause_minutes": -1})


def test_validate_enum_rejects_value_outside_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """枚举参数：值必须在 options 里。

    min/max 只是范围兜底；真正的「合法取值集」是 options——前端只能
    选到 options 里的值，越界值说明是绕过 UI 的直接调用。

    注意：现有枚举参数都是 0/1（min/max 恰好等于 options 全集），范围
    检查会**先**触发，options 分支走不到。故这里临时注册一个
    「范围宽于选项集」的参数来覆盖该分支。
    """
    from app.services import runtime_params as rp

    probe = ParamSpec(
        key="probe_enum",
        label="探针",
        default=0,
        min=0,
        max=5,
        options=((0, "甲"), (1, "乙")),
    )
    monkeypatch.setattr(rp, "PARAM_SPECS", (*rp.PARAM_SPECS, probe))

    # 2 在 min/max 内但不在 options 里 → 必须被拒
    with pytest.raises(ValueError, match="只能取"):
        rp.validate({"probe_enum": 2})
    # options 内的值要能过
    rp.validate({"probe_enum": 0})
    rp.validate({"probe_enum": 1})
    # 超出 min/max → 范围错误（先于 options 检查）
    with pytest.raises(ValueError, match="之间"):
        rp.validate({"probe_enum": 9})


def test_enum_validation_error_message_mentions_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """枚举越界的报错要列出合法取值（用户能看懂该填什么）。"""
    from app.services import runtime_params as rp

    probe = ParamSpec(
        key="probe_enum2",
        label="探针2",
        default=0,
        min=0,
        max=5,
        options=((0, "甲"), (1, "乙")),
    )
    monkeypatch.setattr(rp, "PARAM_SPECS", (*rp.PARAM_SPECS, probe))

    with pytest.raises(ValueError) as exc:
        rp.validate({"probe_enum2": 3})
    assert "只能取" in str(exc.value)
    assert "[0, 1]" in str(exc.value), "报错要列出合法取值"


# ------------------------------------------------------- 启动期加载的收敛


def test_sanitize_loaded_keeps_legal_values() -> None:
    """合法值原样保留。"""
    spec = lookup_spec("monitor_delete_expired")
    assert spec is not None
    assert _sanitize_loaded(spec, 0, "DB") == 0
    assert _sanitize_loaded(spec, 1, "DB") == 1


def test_sanitize_loaded_falls_back_on_illegal_enum() -> None:
    """枚举外的值 → 回退 default（不能进内存）。

    否则前端 radio **无选中项**——用户看到两个都没选，不知道当前状态。
    """
    spec = lookup_spec("monitor_delete_expired")
    assert spec is not None
    assert _sanitize_loaded(spec, 5, "DB") == spec.default


def test_sanitize_loaded_falls_back_on_out_of_range() -> None:
    """越界的连续数值 → 回退 default。"""
    spec = lookup_spec("monitor_pre_pause_minutes")
    assert spec is not None
    assert _sanitize_loaded(spec, 999, "env") == spec.default
    assert _sanitize_loaded(spec, -5, "env") == spec.default


def test_sanitize_loaded_warns_on_illegal_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """非法值必须留下 warning（问题要可见，不能静默修正）。"""
    import logging

    spec = lookup_spec("monitor_delete_expired")
    assert spec is not None
    with caplog.at_level(logging.WARNING):
        _sanitize_loaded(spec, 5, "DB")
    assert any("monitor_delete_expired" in r.message for r in caplog.records)
    assert any("回退" in r.message for r in caplog.records)


# --------------------------------------------------------------------------
# 参数归属的一致性（2026-11-22 加：整理作业的参数曾全部挂在不存在的
# ``organize_torrents`` 下 → 作业配置弹框空着，用户无法开启整理）
# --------------------------------------------------------------------------


def test_spec_job_names_exist_in_registry() -> None:
    """每个参数的 ``job`` 必须指向**已注册**的作业。

    归属名写错不会报错：``specs_for_job(job_name)`` 只是按字符串过滤，
    查不到就返回空元组——配置弹框静默空白，作业参数永远停在默认值。
    故把「归属名 ⊆ 作业名」写成测试。
    """
    from app.services.job_registry import JOB_META
    from app.services.runtime_params import PARAM_SPECS

    job_names = {meta.name for meta in JOB_META}
    unknown = sorted(
        {spec.job for spec in PARAM_SPECS if spec.job and spec.job not in job_names}
    )
    assert not unknown, (
        f"这些参数挂在未注册的作业名下：{unknown}（可用作业：{sorted(job_names)}）"
    )


def test_organize_job_exposes_its_params() -> None:
    """整理作业必须拿得到自己的三个参数（否则面板上无法启用 / 调整）。"""
    from app.services.runtime_params import specs_for_job

    keys = {spec.key for spec in specs_for_job("organize_files")}
    assert keys == {"organize_enabled", "organize_action", "organize_conflict_policy"}
