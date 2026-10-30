"""115「生活事件」流——增量同步的数据源。

**为什么用它**：全量扫描要遍历整棵目录树（媒体库约 2000 个目录），
限速 1s/请求下要十几分钟。而 115 的行为事件流直接告诉你
「哪些文件变了」，**还带 pick_code**（可直接换直链，不用列目录）。

**接口**::

    GET https://webapi.115.com/behavior/detail
    参数：type / limit / offset / date / from_id

**实测结论（重要，别凭记忆改）**：

1. **``date`` 是唯一有效的增量参数**——按天拉，``count`` 是那天的真实条数，
   一天的数据一次就能拉完（``next_page=False``）。
2. **``from_id`` 对服务端完全无效**——传 0、传最新 id、传不存在的值，
   返回结果**完全一样**。它只是 ``p115client`` 客户端侧的过滤，
   服务端忽略。别拿它做增量游标。
3. ``count`` 恒为 10000（事件流总容量），**不代表「这次拉满了」**——
   拿它判断截断会误报。
4. **必须直调** ``client.life_behavior_detail()``——``p115client`` 提供的
   ``tool.iter_life_behavior()`` 包装层对所有 app 都返回 405
   （实测：web / android / ios / windows … 全部失败）。
5. ``file_type`` 是 115 的**内部类型码**（415=mov / 101=jpg / 447=mkv /
   196=mp4 / 238=nfo），既不完整也不直观 → **分类必须看文件名的扩展名**。
6. 删除事件（type=22）的 ``parent_id`` 恒为 ``0``、``parent_name`` 恒为
   「根目录」——**父目录信息已失效**，不能靠路径前缀判断它是否在媒体库内，
   只能靠 ``pick_code`` 反查本地记录。
"""

from __future__ import annotations

import datetime
import time
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

#: 事件流最多保留的条数（超出部分会被挤掉）
EVENT_HISTORY_LIMIT = 10_000

#: 拉取事件时的单页条数
PAGE_LIMIT = 1000

#: 事件类型（115 官方语义，见 ``life_behavior_detail`` 的 docstring）
TYPE_UPLOAD_IMAGE = 1
TYPE_UPLOAD_FILE = 2
TYPE_MOVE_IMAGE = 5
TYPE_MOVE_FILE = 6
TYPE_UPLOAD_VIDEO = 11
TYPE_RECEIVE_FILES = 14
TYPE_NEW_FOLDER = 17
TYPE_FOLDER_RENAME = 20
TYPE_DELETE_FILE = 22
TYPE_COPY_FILE = 23
TYPE_FILE_RENAME = 24

#: 与**文件内容变化**相关的事件类型（其余忽略）
#:
#: 刻意**不含**目录事件（``new_folder`` / ``folder_rename``）——目录改名
#: 要重定位整棵子树的 strm，复杂度远高于收益。目录变更靠全量扫描兜底。
FILE_EVENT_TYPES: frozenset[int] = frozenset(
    {
        TYPE_UPLOAD_IMAGE,
        TYPE_UPLOAD_FILE,
        TYPE_MOVE_IMAGE,
        TYPE_MOVE_FILE,
        TYPE_UPLOAD_VIDEO,
        TYPE_RECEIVE_FILES,
        TYPE_DELETE_FILE,
        TYPE_COPY_FILE,
        TYPE_FILE_RENAME,
    }
)

#: 删除事件（父目录信息失效，需靠 pick_code 反查）
DELETE_TYPES: frozenset[int] = frozenset({TYPE_DELETE_FILE})

#: 类型 → 官方名称（用于日志）
TYPE_NAMES: dict[int, str] = {
    TYPE_UPLOAD_IMAGE: "upload_image_file",
    TYPE_UPLOAD_FILE: "upload_file",
    TYPE_MOVE_IMAGE: "move_image_file",
    TYPE_MOVE_FILE: "move_file",
    TYPE_UPLOAD_VIDEO: "upload_video",
    TYPE_RECEIVE_FILES: "receive_files",
    TYPE_NEW_FOLDER: "new_folder",
    TYPE_FOLDER_RENAME: "folder_rename",
    TYPE_DELETE_FILE: "delete_file",
    TYPE_COPY_FILE: "copy_file",
    TYPE_FILE_RENAME: "file_rename",
}


class EventFetchError(RuntimeError):
    """事件拉取失败。"""


@dataclass(frozen=True)
class LifeEvent:
    """一条生活事件。

    Attributes:
        id: 事件 id。
        type: 事件类型。
        file_id: 115 的文件 id（cid）。
        parent_id: 父目录 id。**删除事件恒为 0**。
        file_name: 文件名（含扩展名）。
        file_size: 字节数。
        pick_code: 提取码——**可直接换直链**。
        parent_name: 父目录名（删除事件恒为「根目录」）。
        update_time: 事件发生时间（unix 秒）。
        date: 事件所属日期（``YYYY-MM-DD``）。
    """

    id: int
    type: int
    file_id: str
    parent_id: str
    file_name: str
    file_size: int
    pick_code: str
    parent_name: str
    update_time: int
    date: str = ""

    @property
    def is_delete(self) -> bool:
        """是否删除事件。"""
        return self.type in DELETE_TYPES

    @property
    def type_name(self) -> str:
        """事件类型的官方名称。"""
        return TYPE_NAMES.get(self.type, f"type_{self.type}")


@dataclass(frozen=True)
class FetchResult:
    """一次事件拉取的结果。"""

    events: list[LifeEvent]
    #: 覆盖到的日期
    dates: list[str]
    #: 拉到的原始条数
    raw_count: int
    #: 事件流是否已满（满了说明中间可能有事件被挤掉）
    history_full: bool


async def fetch_events(
    client: Any,
    dates: list[str],
    *,
    max_events: int = 5000,
) -> FetchResult:
    """按**日期**拉取事件。

    Args:
        client: p115client 实例。
        dates: 要拉取的日期（``YYYY-MM-DD``）——通常是「上次同步那天」
            到「今天」。**按天拉**是唯一有效的增量手段（``from_id`` 无效）。
        max_events: 最多拉多少条（防跑飞）。

    Returns:
        拉取结果。

    Raises:
        EventFetchError: 接口调用失败。
    """
    from app.core.rate_limit import rate_limiter

    collected: list[LifeEvent] = []
    raw_count = 0
    history_full = False

    for date in dates:
        offset = 0
        while len(collected) < max_events:
            payload: dict[str, Any] = {
                "type": "",
                "limit": PAGE_LIMIT,
                "offset": offset,
                "date": date,
            }

            await rate_limiter.acquire("p115")
            try:
                resp = await client.life_behavior_detail(
                    payload, app="web", async_=True
                )
            except Exception as exc:
                raise EventFetchError(f"115 生活事件拉取失败：{exc}") from exc

            if not isinstance(resp, dict):
                raise EventFetchError("115 生活事件返回格式异常")
            if resp.get("state") is False or resp.get("code") not in (None, 0):
                raise EventFetchError(
                    f"115 生活事件返回错误：{resp.get('message') or resp.get('msg')}"
                )

            data = resp.get("data") or {}
            items = data.get("list") or []
            if not items:
                break

            raw_count += len(items)
            for item in items:
                event = _to_event(item, date=date)
                if event is None:
                    continue
                if event.type not in FILE_EVENT_TYPES:
                    continue
                collected.append(event)

            # count 是**事件流总容量**（恒为 10000），不代表这次拉满了。
            # 但它满了就意味着中间的事件可能被挤掉 → 提示重建基线。
            if int(data.get("count") or 0) >= EVENT_HISTORY_LIMIT:
                history_full = True

            if not data.get("next_page"):
                break
            offset += len(items)
            if len(items) < PAGE_LIMIT:
                break

    return FetchResult(
        events=collected,
        dates=list(dates),
        raw_count=raw_count,
        history_full=history_full,
    )


def _to_event(item: dict[str, Any], *, date: str = "") -> LifeEvent | None:
    """把接口返回的一项转成 :class:`LifeEvent`。"""
    try:
        event_id = int(item.get("id") or 0)
    except (TypeError, ValueError):
        return None
    if not event_id:
        return None

    return LifeEvent(
        id=event_id,
        type=int(item.get("type") or 0),
        file_id=str(item.get("file_id") or ""),
        parent_id=str(item.get("parent_id") or ""),
        file_name=str(item.get("file_name") or ""),
        file_size=int(item.get("file_size") or 0),
        pick_code=str(item.get("pick_code") or ""),
        parent_name=str(item.get("parent_name") or ""),
        update_time=int(item.get("update_time") or 0),
        date=date,
    )


async def resolve_event_path(client: Any, parent_id: str) -> str:
    """把事件的 ``parent_id`` 反解成目录路径。

    用 ``tool.get_path``——实测能还原**完整路径**（含深层目录），
    例如 ``/影视/电影/动画电影/你的名字。 (2016)``。

    Args:
        client: p115client 实例。
        parent_id: 父目录 id。

    Returns:
        目录路径（绝对）。解析失败返回空串。
    """
    if not parent_id or parent_id == "0":
        return ""
    try:
        from p115client import tool

        from app.core.rate_limit import rate_limiter

        await rate_limiter.acquire("p115")
        path = await tool.get_path(
            client,
            int(parent_id) if parent_id.isdigit() else parent_id,
            async_=True,
        )
    except Exception as exc:
        logger.warning("解析 parent_id=%s 的路径失败：%s", parent_id, exc)
        return ""
    return str(path or "")


def today_str() -> str:
    """今天的日期（``YYYY-MM-DD``）。"""
    return time.strftime("%Y-%m-%d")


def date_range(start: str, end: str) -> list[str]:
    """生成 ``[start, end]`` 之间的日期列表（含两端）。

    Args:
        start: 起始日期（``YYYY-MM-DD``）。
        end: 结束日期（``YYYY-MM-DD``）。

    Returns:
        日期列表。``start > end`` 时返回空列表。
    """
    try:
        begin = datetime.date.fromisoformat(start)
        finish = datetime.date.fromisoformat(end)
    except ValueError:
        return []
    if begin > finish:
        return []
    out: list[str] = []
    cur = begin
    while cur <= finish:
        out.append(cur.isoformat())
        cur += datetime.timedelta(days=1)
    return out


def now_ts() -> int:
    """当前 unix 秒。"""
    return int(time.time())
