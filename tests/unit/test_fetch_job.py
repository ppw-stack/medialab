"""站点拉取作业的合并契约（拉取 + 站点信息获取，2026-11-22）。

合并的目的是**不给「用户数据」单开一个作业**：两者本来就遍历同一批启用
站点，共用一条 cron 心跳（10 分钟）即可——推送的候选来自 `torrent_index`，
排序来自 `site_user_data`，两个数据源一起新鲜最省事。

锁的语义：

1. 两段都跑，且都进 ``steps``（前端能分别看到逐站明细）；
2. 一段抛错**不影响**另一段；
3. **顶层字段保持向后兼容**（``total_written`` / ``scanned_sites`` 等）——
   旧调用方与 ``tests/integration/test_jobs.py`` 不该因为合并而失效；
4. summary 去重（两段同因失败时会返回同一句）。
"""

from __future__ import annotations

from typing import Any

import pytest
from app.services.scheduler import JobStep


async def _fake_fetch(_factory: Any) -> dict[str, Any]:
    return {
        "total_written": 7,
        "scanned_sites": 2,
        "summary": "拉取 7 条",
        "steps": [JobStep("逐站拉取", "ok", 2, "2 站")],
    }


async def _fake_user_data(_factory: Any) -> dict[str, Any]:
    return {
        "ok": 2,
        "failed": 0,
        "skipped": 0,
        "total": 2,
        "summary": "用户数据 2 站",
        "steps": [JobStep("并发抓取", "ok", 2, "2 站")],
    }


async def _boom_fetch(_factory: Any) -> dict[str, Any]:
    raise RuntimeError("站点全挂")


async def _boom_user_data(_factory: Any) -> dict[str, Any]:
    raise RuntimeError("mybonus 解析失败")


@pytest.fixture
def _patched(monkeypatch: pytest.MonkeyPatch) -> None:
    """替换两个子服务（作业入口是函数内惰性导入，打在模块属性上即生效）。"""
    import app.services.jobs as jobs_mod
    import app.services.user_data as user_mod

    monkeypatch.setattr(jobs_mod, "fetch_all_sites", _fake_fetch)
    monkeypatch.setattr(user_mod, "refresh_all_sites", _fake_user_data)


def _stage(result: dict[str, Any], name: str) -> JobStep:
    for step in result["steps"]:
        if step.name == name:
            return step
    raise AssertionError(f"缺步骤：{name}")


async def test_fetch_job_runs_both_stages(_patched: None) -> None:
    from app.services.job_registry import _run_fetch_all_sites

    out = await _run_fetch_all_sites(None)  # type: ignore[arg-type]

    assert _stage(out, "站点拉取").status == "ok"
    assert _stage(out, "站点信息获取").status == "ok"
    # 子步骤挂成 children（前端步骤卡片展开可见）
    assert _stage(out, "站点拉取").children is not None
    assert _stage(out, "站点信息获取").children is not None


async def test_top_level_keys_stay_compatible(_patched: None) -> None:
    """合并不能让旧字段消失——``total_written`` 仍在顶层。"""
    from app.services.job_registry import _run_fetch_all_sites

    out = await _run_fetch_all_sites(None)  # type: ignore[arg-type]

    assert out["total_written"] == 7
    assert out["scanned_sites"] == 2
    # 用户数据另外挂在 user_data 里（不覆盖顶层）
    assert out["user_data"]["ok"] == 2
    assert "拉取 7 条" in out["summary"]
    assert "用户数据 2 站" in out["summary"]


async def test_fetch_failure_does_not_block_user_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """拉取段抛错 → 信息获取段照跑（各自容错）。"""
    import app.services.jobs as jobs_mod
    import app.services.user_data as user_mod

    monkeypatch.setattr(jobs_mod, "fetch_all_sites", _boom_fetch)
    monkeypatch.setattr(user_mod, "refresh_all_sites", _fake_user_data)

    from app.services.job_registry import _run_fetch_all_sites

    out = await _run_fetch_all_sites(None)  # type: ignore[arg-type]
    assert _stage(out, "站点拉取").status == "fail"
    assert _stage(out, "站点信息获取").status == "ok"


async def test_user_data_failure_does_not_lose_fetch_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """信息获取段抛错 → 拉取结果仍在（用户看得见前半段成效）。"""
    import app.services.jobs as jobs_mod
    import app.services.user_data as user_mod

    monkeypatch.setattr(jobs_mod, "fetch_all_sites", _fake_fetch)
    monkeypatch.setattr(user_mod, "refresh_all_sites", _boom_user_data)

    from app.services.job_registry import _run_fetch_all_sites

    out = await _run_fetch_all_sites(None)  # type: ignore[arg-type]
    assert _stage(out, "站点信息获取").status == "fail"
    assert out["total_written"] == 7
    assert "拉取 7 条" in out["summary"]
