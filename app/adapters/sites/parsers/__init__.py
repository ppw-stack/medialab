"""NexusPHP 7 站共用解析 helper 模块包。

各 NexusPHP 适配器（hdsky/hhanclub/audiences/hdhome/hddolby/ourbits/
springsunday）import :mod:`app.adapters.sites.parsers.nexus_php` 后
调 4 个函数::

    from app.adapters.sites.parsers import nexus_php
    info = nexus_php.parse_user_info(html)
"""

from app.adapters.sites.parsers.nexus_php import (
    parse_hr_status,
    parse_message_unread,
    parse_user_info,
    parse_user_seeding_stats,
)

__all__ = [
    "nexus_php",
    "parse_hr_status",
    "parse_message_unread",
    "parse_user_info",
    "parse_user_seeding_stats",
]
