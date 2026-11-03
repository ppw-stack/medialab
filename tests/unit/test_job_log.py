"""作业执行日志采集（``app.core.job_log``）单元测试。

重点覆盖三件事：

1. **并发隔离**——两个作业并发跑时，日志**不能串**（这是
   :class:`contextvars.ContextVar` 存在的全部理由）
2. **上限截断**——超 ``MAX_LOG_ENTRIES`` 丢最旧的
3. **异常堆栈不丢**——失败作业的堆栈是最有价值的排障信息
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator

import pytest
from app.core.job_log import (
    MAX_LOG_ENTRIES,
    JobLogCollector,
    capture_job_logs,
)


@pytest.fixture
def info_logs() -> Iterator[None]:
    """把 root logger 级别降到 INFO。

    **必要的**：``logger.info()`` 在 root 级别是 WARNING 时压根不产生
    record，handler 无从收到——这正是「作业日志为空」的原因。
    测试里默认级别是 WARNING（见 ``conftest`` 的 ``log_level``）。
    """
    root = logging.getLogger()
    old = root.level
    root.setLevel(logging.INFO)
    try:
        yield
    finally:
        root.setLevel(old)


def test_采集到日志条目(info_logs: None) -> None:
    """基本功能：日志被收下，字段正确。"""
    collector = JobLogCollector()
    with capture_job_logs(collector):
        logging.getLogger("app.test").info("你好 %s", "世界")
        logging.getLogger("app.test").warning("小心")

    entries = collector.entries()
    assert len(entries) == 2
    assert entries[0]["message"] == "你好 世界"
    assert entries[0]["level"] == "INFO"
    assert entries[0]["logger"] == "app.test"
    assert entries[1]["level"] == "WARNING"
    # 时间戳是北京时间字符串
    assert len(entries[0]["ts"]) == 19


def test_退出上下文后不再采集(info_logs: None) -> None:
    """采集器只在上下文内生效。"""
    collector = JobLogCollector()
    with capture_job_logs(collector):
        logging.getLogger("app.test").info("在内")
    logging.getLogger("app.test").info("在外")

    entries = collector.entries()
    assert len(entries) == 1
    assert entries[0]["message"] == "在内"


def test_超限丢最旧的(info_logs: None) -> None:
    """超过上限时丢**最旧**的条目（保留最新的）。"""
    collector = JobLogCollector()
    with capture_job_logs(collector):
        log = logging.getLogger("app.test")
        for i in range(MAX_LOG_ENTRIES + 10):
            log.info("第 %d 条", i)

    entries = collector.entries()
    assert len(entries) == MAX_LOG_ENTRIES
    # 最新的一条在
    assert entries[-1]["message"] == f"第 {MAX_LOG_ENTRIES + 9} 条"
    # 最旧的被丢了
    assert entries[0]["message"] == "第 10 条"
    assert collector.truncated == 10


def test_异常堆栈被保留(info_logs: None) -> None:
    """失败作业的堆栈不能丢。"""
    collector = JobLogCollector()
    with capture_job_logs(collector):
        try:
            raise ValueError("炸了")
        except ValueError:
            logging.getLogger("app.test").exception("处理失败")

    entries = collector.entries()
    assert len(entries) == 1
    assert "炸了" in entries[0]["message"]
    assert "Traceback" in entries[0]["message"]


async def test_并发作业日志不串(info_logs: None) -> None:
    """**核心**：并发跑的作业，各自只收到自己的日志。

    没有 ContextVar 隔离时，两个作业的日志会互相污染——
    用户看到「A 作业的日志里有 B 作业的输出」，完全无法排查。
    """
    log = logging.getLogger("app.test")

    async def job(name: str, delay: float) -> list[dict]:
        collector = JobLogCollector()
        with capture_job_logs(collector):
            log.info("%s 开始", name)
            await asyncio.sleep(delay)
            log.info("%s 结束", name)
        return collector.entries()

    # 交错执行，确保两个采集器同时存活
    a, b = await asyncio.gather(job("A", 0.02), job("B", 0.01))

    a_msgs = [e["message"] for e in a]
    b_msgs = [e["message"] for e in b]

    assert a_msgs == ["A 开始", "A 结束"]
    assert b_msgs == ["B 开始", "B 结束"]
    # 交叉验证：不该看到对方的
    assert not any("B" in m for m in a_msgs)
    assert not any("A" in m for m in b_msgs)


async def test_并发子任务继承上下文(info_logs: None) -> None:
    """``asyncio.gather`` 派生的子任务应继承采集上下文。

    ``user_data`` 那种「并发抓 9 站」的作业，其子任务的日志
    也必须被收进来——否则并发部分的日志全丢。
    """
    log = logging.getLogger("app.test")

    async def child(name: str) -> None:
        log.info("子任务 %s", name)

    collector = JobLogCollector()
    with capture_job_logs(collector):
        await asyncio.gather(*(child(f"c{i}") for i in range(3)))

    entries = collector.entries()
    assert len(entries) == 3
    assert {e["message"] for e in entries} == {"子任务 c0", "子任务 c1", "子任务 c2"}


def test_未注册采集器时不收集(info_logs: None) -> None:
    """不在采集上下文里时，不该有采集器收到日志。"""
    collector = JobLogCollector()
    logging.getLogger("app.test").info("没人收")
    assert collector.entries() == []


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR"])
def test_各级别都被收集(info_logs: None, level: str) -> None:
    """各级别都收（DEBUG 低于 root 的 INFO 门槛 → 不收）。"""
    collector = JobLogCollector()
    with capture_job_logs(collector):
        logging.getLogger("app.test").log(
            getattr(logging, level), "级别 %s", level
        )

    entries = collector.entries()
    if level == "DEBUG":
        # DEBUG 低于 root 的 INFO 门槛 → 不产生 record
        assert entries == []
    else:
        assert len(entries) == 1
        assert entries[0]["level"] == level
