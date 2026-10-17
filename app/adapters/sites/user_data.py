"""用户数据通用模型（FR-1.6 站点用户数据解析契约）。

定义 4 个 dataclass 作为适配器 ↔ 服务层的统一数据结构：
- :class:`UserInfo`：身份 + 流量 + 分享率
- :class:`SeedingStats`：做种/下载数 + 体积（仅汇总，不取每条详情）
- :class:`MessageSummary`：未读消息数
- :class:`HrStatus`：Hit and Run 风险

基础方法默认返回「无能力」的值（None / 0 / False），各适配器选择性实现。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UserInfo:
    """登录用户身份与流量信息。

    :param username: 用户名（站点显示名）
    :param userid: 站内数字或 UUID ID
    :param user_level: 等级文案（e.g. "Power User" / "Elite"）
    :param join_at: 注册时间，``YYYY-MM-DD HH:MM:SS`` 北京时间
    :param bonus: 魔力值/积分
    :param upload: 上传量（字节）
    :param download: 下载量（字节）
    :param ratio: 分享率
    """

    username: str | None
    userid: str | None
    user_level: str | None
    join_at: str | None
    bonus: float
    upload: int
    download: int
    ratio: float


@dataclass(frozen=True)
class SeedingStats:
    """做种与下载汇总。

    设计上**不取**每条做种详情（FR-1.6 决策），仅汇总数据；
    避免触发站点限流。

    :param seeding_count: 做种数
    :param leeching_count: 下载数
    :param seeding_size: 做种总体积（字节）
    :param leeching_size: 下载总体积（字节）
    """

    seeding_count: int
    leeching_count: int
    seeding_size: int
    leeching_size: int


@dataclass(frozen=True)
class MessageSummary:
    """未读消息汇总。

    :param unread_count: 未读消息总数
    """

    unread_count: int


@dataclass(frozen=True)
class HrStatus:
    """H&R 风险状态。

    :param is_hr: 当前是否处于 H&R 风险中
    :param warning_count: 警告计数（NexusPHP 显示 H&R 数量）
    """

    is_hr: bool
    warning_count: int


__all__ = ["HrStatus", "MessageSummary", "SeedingStats", "UserInfo"]
