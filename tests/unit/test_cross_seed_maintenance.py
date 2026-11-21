"""辅种维护作业（合并 scan_cross_seed + disk_rotation + scan_orphans）。

**为什么合并**（2026-11-01）：

1. **依赖顺序**——``disk_rotation`` 读 ``cross_seed_snapshot`` 判磁盘
   使用率，而快照由巡查产出。分开跑可能读到**旧快照**。合并后 ② 必定
   基于 ① 的**最新**快照。
2. **省一次全量抓取**——辅种站抓一次要 20s+（3000+ 种子）。

**三段各自容错**：某段失败不影响其余，失败进 ``steps``。
"""

from __future__ import annotations

from typing import Any

import pytest
from app.services import cross_seed_maintenance
from app.services.scheduler import JobStep


class _Ok:
    """一个成功返回的子阶段替身。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def __call__(self, **_kw: Any) -> dict[str, Any]:
        return self.payload


class _Boom:
    """一个抛异常的子阶段替身。"""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def __call__(self, **_kw: Any) -> dict[str, Any]:
        raise self.exc


async def test_三段顺序执行(monkeypatch: pytest.MonkeyPatch) -> None:
    """巡查 → 磁盘流转 → 孤儿扫描，**顺序执行**（依赖顺序正确）。"""
    order: list[str] = []

    def _mk(tag: str, payload: dict[str, Any]) -> _Ok:
        class _Stage(_Ok):
            async def __call__(self, **_kw: Any) -> dict[str, Any]:
                order.append(tag)
                return self.payload

        return _Stage(payload)

    # 巡查：patch 编排模块内的薄封装（service 构造要已初始化的工厂）
    async def _fake_scan() -> dict[str, Any]:
        order.append("scan")
        return {"summary": "巡查 2 个下载器，3 异常", "scanned_downloaders": 2}

    monkeypatch.setattr(
        cross_seed_maintenance, "_run_scan_cross_seed", _fake_scan
    )
    monkeypatch.setattr(
        "app.services.disk_rotation.run_disk_rotation",
        _mk("rotation", {"summary": "使用率 87% < 90%", "deleted": 0}),
    )
    monkeypatch.setattr(
        "app.services.cross_seed_orphan.run_scan_orphans",
        _mk("orphan", {"summary": "孤儿 12 个", "orphan_count": 12}),
    )

    result = await cross_seed_maintenance.run_cross_seed_maintenance()

    # 顺序：巡查 → 磁盘 → 孤儿
    assert order == ["scan", "rotation", "orphan"]
    # 三段结果都在
    assert "cross_seed" in result
    assert "disk_rotation" in result
    assert "scan_orphans" in result
    # steps 三段
    assert len(result["steps"]) == 3
    assert [s.name for s in result["steps"]] == [
        "辅种巡查",
        "磁盘空间流转",
        "孤儿文件扫描",
    ]
    # summary 合成三段
    assert "巡查" in result["summary"]
    assert "孤儿" in result["summary"]


async def test_某段失败不影响其它段(monkeypatch: pytest.MonkeyPatch) -> None:
    """磁盘流转失败 → 巡查与孤儿扫描照常执行，失败进 steps。"""

    async def _fake_scan() -> dict[str, Any]:
        return {"summary": "巡查完成", "scanned_downloaders": 1}

    monkeypatch.setattr(
        cross_seed_maintenance, "_run_scan_cross_seed", _fake_scan
    )
    monkeypatch.setattr(
        "app.services.disk_rotation.run_disk_rotation",
        _Boom(RuntimeError("磁盘流转炸了")),
    )
    monkeypatch.setattr(
        "app.services.cross_seed_orphan.run_scan_orphans",
        _Ok({"summary": "孤儿 3 个", "orphan_count": 3}),
    )

    result = await cross_seed_maintenance.run_cross_seed_maintenance()

    steps: list[JobStep] = result["steps"]
    assert len(steps) == 3
    assert steps[0].status == "ok"
    assert steps[1].status == "fail"
    assert "磁盘流转炸了" in (steps[1].detail or "")
    # 失败段之后的仍执行
    assert steps[2].status == "ok"
    # 失败段的原始返回为空，不污染其它段
    assert result["disk_rotation"] == {}
    assert result["cross_seed"].get("summary") == "巡查完成"


async def test_无辅种下载器时不报错(monkeypatch: pytest.MonkeyPatch) -> None:
    """三段都返回「无辅种下载器」→ 正常返回，不抛。"""

    async def _fake_scan() -> dict[str, Any]:
        return {"summary": "无辅种下载器", "scanned_downloaders": 0}

    monkeypatch.setattr(
        cross_seed_maintenance, "_run_scan_cross_seed", _fake_scan
    )
    monkeypatch.setattr(
        "app.services.disk_rotation.run_disk_rotation",
        _Ok({"summary": "无辅种下载器快照", "deleted": 0}),
    )
    monkeypatch.setattr(
        "app.services.cross_seed_orphan.run_scan_orphans",
        _Ok({"summary": "无辅种下载器", "orphan_count": 0}),
    )

    result = await cross_seed_maintenance.run_cross_seed_maintenance()
    assert result["summary"]
    assert len(result["steps"]) == 3


async def test_阶段含逐项明细(monkeypatch: pytest.MonkeyPatch) -> None:
    """阶段步骤带**逐下载器明细**——回答「哪台下载器出了问题」。"""

    async def _fake_scan() -> dict[str, Any]:
        return {
            "summary": "巡查 2 个下载器",
            "scanned_downloaders": 2,
            "results": [
                {"downloader_name": "tr", "total_torrents": 3000,
                 "anomaly_count": 2},
                {"downloader_name": "qb", "total_torrents": 100},
            ],
        }

    monkeypatch.setattr(
        cross_seed_maintenance, "_run_scan_cross_seed", _fake_scan
    )
    monkeypatch.setattr(
        "app.services.disk_rotation.run_disk_rotation",
        _Ok({
            "summary": "1 个超阈",
            "deleted": 5,
            "results": [{"downloader_name": "tr", "usage_pct": 95.0,
                         "triggered": True, "deleted_count": 5}],
        }),
    )
    monkeypatch.setattr(
        "app.services.cross_seed_orphan.run_scan_orphans",
        _Ok({"summary": "孤儿 3 个", "orphan_count": 3}),
    )

    result = await cross_seed_maintenance.run_cross_seed_maintenance()
    steps = result["steps"]
    assert len(steps) == 3

    scan_step = steps[0]
    assert scan_step.items is not None
    names = {i.name for i in scan_step.items}
    assert names == {"tr", "qb"}
    tr = next(i for i in scan_step.items if i.name == "tr")
    assert "3000 个种子" in (tr.detail or "")

    rot_step = steps[1]
    assert rot_step.items is not None
    assert "删除 5 个" in (rot_step.items[0].detail or "")


async def test_阶段失败明细点名(monkeypatch: pytest.MonkeyPatch) -> None:
    """阶段失败时，明细里点名是哪个下载器失败了。"""

    async def _fake_scan() -> dict[str, Any]:
        return {
            "summary": "1 个失败",
            "scanned_downloaders": 2,
            "failures": [
                {"downloader_id": 2, "downloader_name": "qb", "error": "连接超时"},
            ],
        }

    monkeypatch.setattr(
        cross_seed_maintenance, "_run_scan_cross_seed", _fake_scan
    )
    monkeypatch.setattr(
        "app.services.disk_rotation.run_disk_rotation",
        _Ok({"summary": "无", "deleted": 0}),
    )
    monkeypatch.setattr(
        "app.services.cross_seed_orphan.run_scan_orphans",
        _Ok({"summary": "无", "orphan_count": 0}),
    )

    result = await cross_seed_maintenance.run_cross_seed_maintenance()
    scan_step = result["steps"][0]
    assert scan_step.items is not None
    item = next(i for i in scan_step.items if i.name == "qb")
    assert item.status == "fail"
    assert "连接超时" in (item.detail or "")


async def test_孤儿扫描失败标_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    """③ 用 ``errors`` 表达失败 → 顶层步骤必须 ``warn``.

    2026-11-21 修：``_run_stage`` 原先只认 ``failures`` /
    ``failed_downloaders``，而孤儿扫描返回的是 ``errors``（「下载器名: 原因」
    字符串）→ 扫盘全失败时作业历史**显示正常**（故障被伪装成正常）。
    """

    async def _fake_scan() -> dict[str, Any]:
        return {"summary": "巡查完成", "scanned_downloaders": 1}

    monkeypatch.setattr(
        cross_seed_maintenance, "_run_scan_cross_seed", _fake_scan
    )
    monkeypatch.setattr(
        "app.services.disk_rotation.run_disk_rotation",
        _Ok({"summary": "未超阈", "deleted": 0}),
    )
    monkeypatch.setattr(
        "app.services.cross_seed_orphan.run_scan_orphans",
        _Ok({
            "summary": "2 个下载器，0 个孤儿，2 个扫描失败",
            "scanned_downloaders": 2,
            "orphan_count": 0,
            "errors": ["tr: 挂载不可读", "qb: 拉取超时"],
        }),
    )

    result = await cross_seed_maintenance.run_cross_seed_maintenance()
    orphan_step = result["steps"][2]
    assert orphan_step.status == "warn", "孤儿扫描失败必须让步骤变 warn"
    # 明细要点名「哪台 / 什么原因」
    assert orphan_step.items is not None
    names = {i.name for i in orphan_step.items}
    assert {"tr", "qb"} <= names
    assert all(i.status == "fail" for i in orphan_step.items)


async def test_巡查失败的下载器传给磁盘流转(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """① 的失败名单必须传给 ②（快照没更新的下载器不许参与删除）.

    顺序保证 ≠ 数据有效：① 抓取失败时**不写**快照，② 若无条件读就会按
    几天前的 hash 列表执行不可逆删除。
    """
    seen: dict[str, Any] = {}

    async def _fake_scan() -> dict[str, Any]:
        return {
            "summary": "1/2 个下载器成功",
            "scanned_downloaders": 1,
            "failures": [
                {"downloader_id": 7, "downloader_name": "tr", "error": "连接超时"},
            ],
        }

    async def _fake_rotation(**kw: Any) -> dict[str, Any]:
        seen.update(kw)
        return {"summary": "已跳过 tr", "deleted": 0}

    monkeypatch.setattr(
        cross_seed_maintenance, "_run_scan_cross_seed", _fake_scan
    )
    monkeypatch.setattr(
        "app.services.disk_rotation.run_disk_rotation", _fake_rotation
    )
    monkeypatch.setattr(
        "app.services.cross_seed_orphan.run_scan_orphans",
        _Ok({"summary": "无孤儿", "orphan_count": 0}),
    )

    await cross_seed_maintenance.run_cross_seed_maintenance()

    assert seen.get("skip_downloader_ids") == frozenset({7})


async def test_无失败时不传多余参数(monkeypatch: pytest.MonkeyPatch) -> None:
    """① 全部成功 → 调 ② 时不传 ``skip_downloader_ids``（保持原调用形态）."""

    async def _fake_scan() -> dict[str, Any]:
        return {"summary": "2/2 个下载器成功", "scanned_downloaders": 2}

    seen: dict[str, Any] = {}

    async def _fake_rotation(**kw: Any) -> dict[str, Any]:
        seen.update(kw)
        return {"summary": "未超阈", "deleted": 0}

    monkeypatch.setattr(
        cross_seed_maintenance, "_run_scan_cross_seed", _fake_scan
    )
    monkeypatch.setattr(
        "app.services.disk_rotation.run_disk_rotation", _fake_rotation
    )
    monkeypatch.setattr(
        "app.services.cross_seed_orphan.run_scan_orphans",
        _Ok({"summary": "无孤儿", "orphan_count": 0}),
    )

    await cross_seed_maintenance.run_cross_seed_maintenance()

    assert "skip_downloader_ids" not in seen


async def test_子步骤挂到父步骤(monkeypatch: pytest.MonkeyPatch) -> None:
    """子作业自带的步骤要作为 ``children`` 挂到父步骤上.

    2026-11-01 合并成三段后，巡查的 5 条步骤（扫描下载器 / 写入快照 /
    统计种子总数 / 检测异常 / 自动暂停）只剩一句 summary——页面上看不到
    「写入快照有没有成功」「暂停了几个」。``JobStep.children`` 让它们
    重新可见（前端总是展开，最多 5 条，不撑面板）。
    """

    async def _fake_scan() -> dict[str, Any]:
        return {
            "summary": "1/1 个下载器，3000 种子，2 异常",
            "scanned_downloaders": 1,
            "steps": [
                JobStep("扫描辅种下载器", "ok", 1, "1/1 个实例成功"),
                JobStep("写入快照", "ok", 1, "1 行 cross_seed_snapshot"),
                JobStep("自动暂停", "warn", 2, "已暂停 2 个"),
            ],
        }

    monkeypatch.setattr(
        cross_seed_maintenance, "_run_scan_cross_seed", _fake_scan
    )
    monkeypatch.setattr(
        "app.services.disk_rotation.run_disk_rotation",
        _Ok({"summary": "未超阈", "deleted": 0}),
    )
    monkeypatch.setattr(
        "app.services.cross_seed_orphan.run_scan_orphans",
        _Ok({"summary": "无孤儿", "orphan_count": 0}),
    )

    result = await cross_seed_maintenance.run_cross_seed_maintenance()

    scan_step = result["steps"][0]
    assert scan_step.children is not None
    assert [c.name for c in scan_step.children] == [
        "扫描辅种下载器",
        "写入快照",
        "自动暂停",
    ]
    # 子步骤保留自己的状态与描述（不是被拍平成一句话）
    assert scan_step.children[2].status == "warn"
    assert "已暂停 2 个" in (scan_step.children[2].detail or "")


async def test_子步骤能落库(monkeypatch: pytest.MonkeyPatch) -> None:
    """``children`` 必须能过 **落库序列化**（``asdict`` → 可 JSON）.

    作业结果进 ``job_runs.result_json`` 前会走 :func:`_jsonable`；
    dataclass 里再嵌一层 dataclass 若没被递归处理，前端拿到的是字符串。
    """
    import json

    from app.services.scheduler import _jsonable

    async def _fake_scan() -> dict[str, Any]:
        return {
            "summary": "ok",
            "scanned_downloaders": 1,
            "steps": [JobStep("写入快照", "ok", 1, "1 行")],
        }

    monkeypatch.setattr(
        cross_seed_maintenance, "_run_scan_cross_seed", _fake_scan
    )
    monkeypatch.setattr(
        "app.services.disk_rotation.run_disk_rotation",
        _Ok({"summary": "未超阈", "deleted": 0}),
    )
    monkeypatch.setattr(
        "app.services.cross_seed_orphan.run_scan_orphans",
        _Ok({"summary": "无孤儿", "orphan_count": 0}),
    )

    result = await cross_seed_maintenance.run_cross_seed_maintenance()
    payload = _jsonable(result)
    text = json.dumps(payload, ensure_ascii=False)  # 不抛 = 可落库
    assert '"children"' in text
    assert "写入快照" in text
