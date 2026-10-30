"""扫码会话生命周期的契约测试。

**回归背景**：扫码成功后 ``_run_qr_session`` 的 ``finally`` 块把会话从
``_SESSIONS`` 删掉了。前端扫码后还要再轮询一次才能拿到 cookie——
此时会话已消失 → 拿到 404 → **把「登录成功」误显示成「二维码过期」**。

实测日志（修复前）::

    21:17:25  115 扫码确认成功：T094CDn-...
    21:17:25  GET /qr/status → 404 Not Found   ← 紧接着就 404

故契约：**扫码成功后，结果必须能被后续轮询读到**。
"""

from __future__ import annotations

import time

import pytest
from app.adapters.drivers import p115_qr


@pytest.fixture(autouse=True)
def clean_sessions():
    """每个用例前后清空会话表。"""
    p115_qr._SESSIONS.clear()
    p115_qr._FINISHED_SESSIONS.clear()
    p115_qr._FINISHED_EXPIRY.clear()
    yield
    p115_qr._SESSIONS.clear()
    p115_qr._FINISHED_SESSIONS.clear()
    p115_qr._FINISHED_EXPIRY.clear()


class TestSessionRetention:
    async def test_confirmed_session_stays_readable(self) -> None:
        """**核心回归**：扫码成功后仍能查到结果。"""
        session = p115_qr.QRSession(
            session_id="s1",
            app="115android",
            payload={"uid": "u1"},
            qr_image_b64="x",
        )
        session.kind = "confirmed"
        session.cookie_str = "UID=1; CID=2"

        # 模拟 _run_qr_session 结束时的清理
        await p115_qr._retire_session(session)

        # 前端这时才来轮询——必须还能拿到 confirmed + cookie
        result = await p115_qr.get_qr_status("s1")
        assert result is not None, "扫码成功后查不到结果！"
        assert result.kind == "confirmed"
        assert result.cookie_str == "UID=1; CID=2"

    async def test_expired_session_is_removed(self) -> None:
        """过期/失败的会话直接删（没有结果可保留）。"""
        session = p115_qr.QRSession(
            session_id="s2",
            app="115android",
            payload={"uid": "u2"},
            qr_image_b64="x",
        )
        session.kind = "expired"

        await p115_qr._retire_session(session)

        assert await p115_qr.get_qr_status("s2") is None
        assert "s2" not in p115_qr._FINISHED_SESSIONS

    async def test_finished_session_purged_after_ttl(self) -> None:
        """超过保留期后清理——不能让表无限增长。"""
        session = p115_qr.QRSession(
            session_id="s3",
            app="115android",
            payload={"uid": "u3"},
            qr_image_b64="x",
        )
        session.kind = "confirmed"
        session.cookie_str = "UID=1"

        await p115_qr._retire_session(session)
        assert await p115_qr.get_qr_status("s3") is not None

        # 把过期时间设到过去
        p115_qr._FINISHED_EXPIRY["s3"] = time.monotonic() - 1
        p115_qr._purge_finished()

        assert await p115_qr.get_qr_status("s3") is None
        assert "s3" not in p115_qr._FINISHED_SESSIONS

    async def test_active_session_readable(self) -> None:
        """进行中的会话照常可查。"""
        session = p115_qr.QRSession(
            session_id="s4",
            app="115android",
            payload={"uid": "u4"},
            qr_image_b64="x",
        )
        session.kind = "scanned"
        p115_qr._SESSIONS["s4"] = session

        result = await p115_qr.get_qr_status("s4")
        assert result is not None
        assert result.kind == "scanned"

    async def test_unknown_session_returns_none(self) -> None:
        assert await p115_qr.get_qr_status("nope") is None


class TestPollLoopCleanup:
    async def test_confirmed_session_not_purged_by_finally(self, monkeypatch) -> None:
        """``finally`` 块不能删掉已 confirmed 的会话。

        这是本 bug 的直接原因——轮询循环结束时无条件 pop。
        """
        session = p115_qr.QRSession(
            session_id="s5",
            app="115android",
            payload={"uid": "u5"},
            qr_image_b64="x",
        )
        p115_qr._SESSIONS["s5"] = session

        # 让 _qr_status 立刻返回 status=2（已登录）
        monkeypatch.setattr(
            p115_qr, "_qr_status", lambda payload: {"data": {"status": 2}}
        )
        # 确认阶段直接标记成功
        async def _fake_confirm(sess) -> None:
            sess.kind = "confirmed"
            sess.cookie_str = "UID=1"

        monkeypatch.setattr(p115_qr, "_confirm_session", _fake_confirm)
        monkeypatch.setattr(p115_qr.asyncio, "sleep", _no_sleep)

        await p115_qr._run_qr_session(session)

        # 结果必须还能读到
        result = await p115_qr.get_qr_status("s5")
        assert result is not None, "扫码成功后结果被 finally 删掉了"
        assert result.kind == "confirmed"


async def _no_sleep(_seconds: float) -> None:
    """跳过等待（测试里不想真的 sleep）。"""
    return None
