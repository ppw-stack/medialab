"""运维加固单元测试：/readyz 降级告警 + SQLite WAL 在线备份与恢复演练。"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import Any

import httpx
import respx
from app.core.metrics import render_metrics
from app.services import startup_status
from app.services.alerting import notify_startup_degradations


def _load_backup_module() -> Any:
    script = Path(__file__).resolve().parents[2] / "scripts" / "backup_sqlite.py"
    spec = importlib.util.spec_from_file_location("backup_sqlite", script)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backup_sqlite"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_sqlite_wal_backup_and_restore_drill(tmp_path: Path) -> None:
    """WAL 模式在线热备份 → 破坏原库 → 安全恢复演练（不丢失 WAL 事务）。"""
    mod = _load_backup_module()
    db_file = tmp_path / "medialab.db"
    bak_dir = tmp_path / "backups"

    # 1) 构造 WAL 模式测试库并写入数据（故意不 checkpoint，数据留在 -wal）
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO items (name) VALUES ('keep-me-1'), ('keep-me-2')")
    conn.commit()
    try:
        # 2) 在线热备份（连接仍保持打开，模拟线上运行态）
        bak_path = mod.backup_db(db_file, bak_dir, keep=2, stamp="20261126-190000")
        assert bak_path.exists()
        mod.check_integrity(bak_path)

        # 3) 模拟误操作删除原库数据
        conn.execute("DELETE FROM items")
        conn.commit()
    finally:
        conn.close()

    # 4) 执行恢复演练
    pre_snap = mod.restore_db(bak_path, db_file)
    assert pre_snap is not None and pre_snap.exists()

    # 5) 验证数据 100% 完整恢复
    verify = sqlite3.connect(str(db_file))
    try:
        rows = [r[0] for r in verify.execute("SELECT name FROM items ORDER BY id").fetchall()]
    finally:
        verify.close()
    assert rows == ["keep-me-1", "keep-me-2"]


async def test_startup_degradation_metric_and_webhook_alert() -> None:
    """启动降级同步到 /metrics Gauge，并能安全推送 Webhook 告警（失败不炸）。"""
    startup_status.reset()
    try:
        startup_status.record("watch_manager_start:nas", "目录不可达")
        body, _ = render_metrics()
        assert "seedhelper_startup_degradations 1.0" in body.decode("utf-8")

        degs = startup_status.snapshot()
        with respx.mock(assert_all_called=False) as router:
            hook = router.post("https://alerts.test/hook").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            sent = await notify_startup_degradations(
                degs, webhook_url="https://alerts.test/hook"
            )
        assert sent is True
        assert hook.called
        assert b"readyz_degraded" in hook.calls[0].request.content
        assert b"watch_manager_start:nas" in hook.calls[0].request.content

        # Webhook 失败时返回 False 且不抛异常
        with respx.mock(assert_all_called=False) as router:
            router.post("https://alerts.test/fail").mock(
                return_value=httpx.Response(500, text="boom")
            )
            sent_fail = await notify_startup_degradations(
                degs, webhook_url="https://alerts.test/fail"
            )
        assert sent_fail is False

        # 未配置 URL 时静默跳过
        assert await notify_startup_degradations(degs, webhook_url="") is False

        startup_status.clear("watch_manager_start:nas")
        body2, _ = render_metrics()
        assert "seedhelper_startup_degradations 0.0" in body2.decode("utf-8")
    finally:
        startup_status.reset()
