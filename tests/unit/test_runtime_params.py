"""运行时参数（runtime_params）契约测试。

覆盖两类东西：

1. **枚举参数契约**（2026-10-23 加入）——0/1 这类「在几个具名行为间二选一」
   的参数用 ``options`` 声明取值，前端据此渲染**单选按钮组**而非数字输入框。
   这里锁住：options 与 min/max 一致、默认值在 options 里、取值不重复。

2. **校验契约**——``validate`` 的三道关：参数已注册 / 是整数 / 在合法范围内
   （枚举参数还必须在 options 里）。

注意：``_assert_enum_options_consistent()`` 是**导入时**执行的——spec 写错
会在 import 阶段直接抛 RuntimeError，故这里测的是「现有 spec 全部合规」，
而不是「写错会抛」（后者由该函数自身的存在保证）。
"""

from __future__ import annotations

import pytest
from app.services.runtime_params import (
    PARAM_SPECS,
    ParamSpec,
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
    """当前只有两个 0/1 枚举参数——新增枚举参数时同步更新本断言。"""
    enum_keys = {s.key for s in PARAM_SPECS if s.options is not None}
    assert enum_keys == {"monitor_delete_expired", "push_within_bucket_order"}


def test_monitor_delete_expired_options() -> None:
    """删种子的枚举：0=仅暂停（默认）/ 1=删除任务+删数据。"""
    spec = lookup_spec("monitor_delete_expired")
    assert spec is not None
    assert spec.options == ((0, "仅暂停"), (1, "删除任务+删数据"))
    assert spec.default == 0, "默认必须是仅暂停（删除不可逆）"
    assert spec.note, "删除不可逆，必须有风险提示"


def test_push_within_bucket_order_options() -> None:
    """同档内排序的枚举：0=按进度升序（默认）/ 1=随机。"""
    spec = lookup_spec("push_within_bucket_order")
    assert spec is not None
    assert spec.options == ((0, "按进度升序"), (1, "随机"))
    assert spec.default == 0
    assert spec.note, "档位是主排序这条说明不能丢"


def test_specs_for_job_keeps_enum_options() -> None:
    """按作业取 spec 时，枚举信息不能丢（前端要靠它渲染 radio）。"""
    for spec in specs_for_job("monitor_free_window"):
        if spec.key == "monitor_delete_expired":
            assert spec.options is not None
            assert spec.note
    for spec in specs_for_job("push_free_torrents"):
        if spec.key == "push_within_bucket_order":
            assert spec.options is not None


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
