"""领域枚举与常量（不依赖任何层）。

这些是**业务概念**，不属于数据库层：``free_type`` 表示"这个种子
是什么性质的免费"，与它存在哪张表无关。此前它们住在
``app/db/models.py``，导致 :mod:`app.core` 反向依赖 ``app.db``
（``app/core/windows.py`` → ``app/db/models.py``，全仓唯一的逆向依赖）。

本模块**不 import 任何 app 包**，可被任意层安全依赖。
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "NON_ABORTABLE_FREE_TYPES",
    "TYPES_WITHOUT_WINDOW",
    "FreeSource",
    "FreeType",
    "PushStatus",
]


class FreeType:
    """种子在站点的「免费性质」。

    注：这些是**纯字符串常量**（不是 ``StrEnum``）——它们直接作为
    ``torrent_index.free_type`` 列的值落库，改成枚举会增加
    序列化/比较的复杂度，且列的类型声明是 ``String``。
    """

    TIMED = "freeleech_timed"
    PERMANENT = "freeleech_permanent"
    TWOU_TIMED = "twoup_timed"
    NEUTRAL = "neutral_leech"
    UNKNOWN = "unknown"
    #: 退还/返还种（springsunday "1d 退还"）：上传者已撤种，窗口内仍可下载
    #: 业务规则：不自动推送（push_free 排除）；monitor 视为"非免费"直接暂停
    REFUND = "refund"


class PushStatus:
    """torrent_index 推送状态。

    注：**没有** ``PENDING`` —— ``torrent_index.push_status`` 列是
    nullable，``NULL`` 即表示「从未推过」，不需要再有一个 ``pending``
    取值（历史上有过，从未写入，已删除）。
    """

    PUSHED = "pushed"
    FAILED = "failed"


class FreeSource:
    """免费状态的判定来源（``torrent_index.free_source`` 列）。"""

    API = "api"
    LIST_TEXT = "list_text"
    SITE_DEFAULT = "site_default"


#: 不参与超时中止的免费类型（FR-3.0 / R10「永久免费绝不挂看护」）。
#:
#: **只含 PERMANENT**。NEUTRAL（半价/三折下载）不在其中：
#:
#: - ``app/services/monitor.py`` 的实际行为是「当前不是免费种 → 立即暂停」，
#:   NEUTRAL 走的是这条路径（用户规则）
#: - 「不参与超时中止」≠「永不暂停」——NEUTRAL 没有时间窗口，
#:   本就不存在「超时中止」的场景，把它塞进这个集合是语义混淆
#:
#: 历史上这里含 NEUTRAL，与 monitor 的实际行为矛盾（潜伏 bug：
#: ``is_permanent`` / ``is_actionable`` 零调用点，所以从未暴露）。
#: 现按 monitor 的现行行为对齐。
NON_ABORTABLE_FREE_TYPES: Final = frozenset({FreeType.PERMANENT})

#: **不得携带 free_end_at** 的类型（构造 :class:`app.core.windows.FreeWindow`
#: 时校验）。
#:
#: 与 :data:`NON_ABORTABLE_FREE_TYPES` 是**两个不同概念**：
#:
#: - 非中止类型 = 不参与超时中止（永久免费）
#: - 无窗口类型 = 本身没有时间窗口，带 end_at 是契约违规
#:
#: NEUTRAL（中性下载不限时）与 TWOU_TIMED（2X 永久有效）属后者。
#: 历史上两者混用同一个集合，导致把 NEUTRAL 移出非中止集合时
#: 连带丢掉了「NEUTRAL 不得带 end_at」的校验。
TYPES_WITHOUT_WINDOW: Final = frozenset({
    FreeType.PERMANENT,
    FreeType.NEUTRAL,
    FreeType.TWOU_TIMED,
})
