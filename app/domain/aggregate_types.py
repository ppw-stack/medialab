"""网盘聚合（多账号主从）的**单一真源**（domain 层，不依赖任何层）。

## 业务模型

一个**聚合组**（aggregate group）= 1 个**主数据源** + N 个**副数据源**：

```
聚合组 G1: 主=光鸭A（唯一真源）  副=光鸭B、光鸭C（缓冲，不对外展示）
```

- **主数据源**：内容永远以它为准（媒体库扫描 / STRM / 播放都只认它）
- **副数据源**：**缓冲**——主盘额度用尽时把新文件先放副盘，后台再归拢回主盘；
  归拢完成（主盘校验落盘同名同大小）后**自动清理副盘副本**

**副盘内容永不在面板展示**（用户视角：东西要么在主盘，要么在路上）。

## 硬约束（都来自实测，不是设计偏好）

- :data:`GUANGYA_DAILY_CLOUD_ADD_QUOTA` —— 光鸭「云添加」**3 次/账号/天**，
  **失败也扣**。这是整个聚合体系唯一的硬墙，且消耗在**目标账号**（主盘）头上
  （源端取直链不吃额度）。
  **服务端不提供「剩余次数」查询**，故只能**撞墙探测**：撞到「次数超限」类
  文案 → 整组转 ``paused``（不硬撞不重试），次日额度窗口打开时自动续传。
- 副 → 主的唯一传输路线是**云下载**（光鸭 → 光鸭，服务端内部复制，不吃本机
  带宽）。落盘名取自 URL 末段（光鸭直链末段是 ``download``）→ **必须改名**。
- **一盘一组一角色**：一个存储源最多属于一个聚合组、组内一个角色
  （主 / 副）。不允许跨组嵌套——否则归拢链路会成环、额度归属混乱。
- **不做跨组搬迁**：跨组需求走既有的迁移功能手动处理。

## 为什么在 domain 层

角色取值、台账状态取值、云添加额度这些**枚举与常量**散落在多处时，
前后端各写一份必然漂移（此前 ``UPLOADABLE_DRIVER_TYPES`` /
``PROBEABLE_DRIVER_TYPES`` 都是同样的教训）。收敛到 domain 后，
service / api / 前端类型都从这里取。
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "GUANGYA_DAILY_CLOUD_ADD_QUOTA",
    "ROLE_LABELS",
    "ROLE_MASTER",
    "ROLE_SECONDARY",
    "STAGING_FAILED",
    "STAGING_PAUSED",
    "STAGING_PENDING",
    "STAGING_STATES",
    "STAGING_SYNCING",
    "STAGING_TERMINAL",
    "STAGING_VERIFIED",
    "is_valid_role",
]

#: 组内角色——主数据源（唯一真源）。
ROLE_MASTER: Final = "master"

#: 组内角色——副数据源（缓冲，不展示内容）。
ROLE_SECONDARY: Final = "secondary"

#: 合法的角色取值。
ROLES: Final = frozenset({ROLE_MASTER, ROLE_SECONDARY})

#: 角色的展示名（前端文案，后端报错文案共用）。
ROLE_LABELS: Final[dict[str, str]] = {
    ROLE_MASTER: "主数据源",
    ROLE_SECONDARY: "副数据源",
}

#: 光鸭「云添加」每日额度（次/账号）。
#:
#: **实测 3 次/账号/天**（前端文案「VIP 1000 次/天」对实测账号不成立），
#: 且**失败也扣**。记在**目标账号**（主盘）头上。
#:
#: 这个数字决定了归拢速度：每天最多搬 3 个文件。
GUANGYA_DAILY_CLOUD_ADD_QUOTA: Final = 3

# -- 台账状态 ---------------------------------------------------------------

#: 等待归拢。
STAGING_PENDING: Final = "pending"
#: 归拢中（已提交云下载任务）。
STAGING_SYNCING: Final = "syncing"
#: 已落盘并校验通过（主盘已有同名同大小文件），等待清理副盘副本。
STAGING_VERIFIED: Final = "verified"
#: 已完成（副盘副本已清理或无需清理）。
STAGING_DONE: Final = "done"
#: 失败（需人工介入或下轮重试）。
STAGING_FAILED: Final = "failed"
#: 暂停（云添加额度耗尽——当日不恢复，次日自动续传）。
STAGING_PAUSED: Final = "paused"

#: 全部台账状态。
STAGING_STATES: Final = frozenset(
    {
        STAGING_PENDING,
        STAGING_SYNCING,
        STAGING_VERIFIED,
        STAGING_DONE,
        STAGING_FAILED,
        STAGING_PAUSED,
    }
)

#: **终态**（不会再被归拢引擎处理）。
#:
#: 终态之外的行都算「未消化完」——角色切换 / 解散组时必须先处理掉，
#: 否则台账里的目标盘归属会与新的角色配置矛盾。
STAGING_TERMINAL: Final = frozenset({STAGING_DONE})


def is_valid_role(role: str | None) -> bool:
    """角色取值是否合法（``None`` = 不参与聚合）。"""
    return role in ROLES
