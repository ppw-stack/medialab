"""作业编排助手（把一个作业拆成若干「子阶段」时共用）。

**为什么需要**：从 2026-11-22 起，两个作业都是「一个 JobSpec、内部多段」：

- `push_and_monitor_free` — 窗口监测 + 免费种推送
- `fetch_all_sites` — 站点拉取 + 站点信息获取

两处都要做同样三件事，且**口径必须一致**（否则同一个概念在页面上有两种
表现）：

1. **子阶段失败折叠成 `fail` 步骤**，不影响其它段（作业级异常隔离的延伸）；
2. **子阶段自带的 steps 挂成 `children`**（前端步骤卡片展开看逐站/逐种明细）；
3. **summary 拼接 + 去重**（两段共用前置条件时会返回同一句，例如都没配
   下载器——拼两遍会像 bug）。

早期 `push_and_monitor.py` 里有一份私有实现，这里抽出来共用。**注意保留
`JobStep` 对象**：直接 `asdict` 会把子步骤变成 dict，`isinstance` 判定失败，
children 就挂了（2026-11-22 真机踩过）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict
from typing import Any

from app.core.logging import get_logger
from app.services.scheduler import JobStep

logger = get_logger(__name__)

__all__ = ["build_summary", "extract_count", "run_stage", "to_payload"]


def to_payload(result: Any) -> dict[str, Any]:
    """dataclass 结果 → dict，但**保留 ``steps`` 里的 JobStep 对象**。

    ``asdict`` 会递归把 JobStep 变成普通 dict，:func:`run_stage` 就认不出
    子步骤了（``isinstance`` 失败）→ 前端「步骤卡片」看不到子服务的
    逐站/逐种明细。

    保留对象是安全的：最终 ``_record_run`` 走 ``_jsonable``（递归
    ``asdict``）再 ``json.dumps``，JobStep 会被正确序列化——与
    ``cross_seed_maintenance`` 返回 ``steps=[JobStep(...)]`` 的做法一致。
    """
    if isinstance(result, dict):  # 服务已经返回 dict（如 refresh_all_sites）
        return result
    payload = asdict(result)
    steps = getattr(result, "steps", None)
    if steps:
        payload["steps"] = list(steps)
    return payload


async def run_stage(
    name: str,
    coro_factory: Callable[[], Awaitable[Any]],
) -> tuple[dict[str, Any], JobStep]:
    """跑一个子阶段，把失败折叠成 ``fail`` 步骤（不影响其它阶段）。

    状态判定（口径与 ``cross_seed_maintenance._run_stage`` 一致，
    只是这里多认一个 ``skipped_by_interval``）：

    - 抛异常 → ``fail``（附类型名与消息）
    - ``errors`` / ``failed`` 非空 → ``warn``（段内部分失败）
    - ``skipped_by_interval`` → ``skip``（节流跳过，不是成功也不是失败）
    - 其余 → ``ok``

    Returns:
        ``(子阶段返回 dict, JobStep)``——失败时 dict 为空。
    """
    try:
        raw = await coro_factory()
    except Exception as exc:
        logger.exception("作业子阶段失败：%s", name)
        return {}, JobStep(name, "fail", None, f"{type(exc).__name__}: {exc}")

    if isinstance(raw, dict):
        data = raw
    elif raw is None:
        data = {}
    else:
        data = to_payload(raw)

    detail = str(data.get("summary") or "")
    status = "ok"
    if data.get("errors") or data.get("failed"):
        status = "warn"
    if data.get("skipped_by_interval"):
        status = "skip"

    children = [s for s in (data.get("steps") or []) if isinstance(s, JobStep)]
    return data, JobStep(
        name,
        status,
        extract_count(data),
        detail or None,
        children=children or None,
    )


def extract_count(raw: dict[str, Any]) -> int | None:
    """从子阶段返回里取一个「涉及数量」用于步骤卡片。

    各服务的计数键名不同，按「最能代表工作量」的顺序取第一个存在的。
    """
    for key in ("pushed", "total_written", "ok", "matched", "scanned"):
        value = raw.get(key)
        if isinstance(value, int):
            return value
    return None


def build_summary(
    results: dict[str, dict[str, Any]],
    keys: Sequence[str],
) -> str:
    """按 ``keys`` 顺序拼接各段 summary，**去重**。

    去重不是洁癖：两段共用同一个前置条件（例如都没配默认下载器、都没有
    启用站点）时会返回**完全相同**的一句，拼起来是「未配置默认下载器；
    未配置默认下载器」——用户看不出这是两段的结论，只像 bug。
    """
    parts: list[str] = []
    for key in keys:
        text = str((results.get(key) or {}).get("summary") or "")
        if text and text not in parts:
            parts.append(text)
    return "；".join(parts) if parts else "无动作"
