"""115 增量同步的测试。

用假的 p115client（monkeypatch ``life_behavior_detail`` / ``tool.get_path``），
不联网。

覆盖点：

- 事件解析（字段映射、类型过滤）
- 增量游标语义（``from_id`` 只取 id 更大的）
- 事件流被 10000 条上限截断时**报错**而不是默默漏
- 增量流程：新增 strm / 附件下载 / 删除 / 路径冲突跳过
- 未建基线时**明确报错**
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from app.adapters.drivers import p115_events as ev
from app.db.models import Driver
from app.services import incremental, settings_store
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
async def driver_row(db_session: AsyncSession) -> Driver:
    """一个 115 存储源（媒体库 cid / 路径 / 输出目录）。"""
    row = Driver(
        id=1,
        type="p115",
        name="测试源",
        credential=None,
        device_type="115android",
        enabled=True,
        media_cid="cid-root",
        media_path="/影视",
        output_dir=None,  # 各用例自己设
        video_exts="mkv,mp4",
        image_exts="jpg,png",
        data_exts="nfo",
        min_size_mb=0,
        include_subdirs=True,
        created_at=int(time.time()),
        updated_at=int(time.time()),
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


def _event(
    *,
    id: int = 100,
    type: int = ev.TYPE_UPLOAD_FILE,
    file_id: str = "fid-1",
    parent_id: str = "cid-parent",
    file_name: str = "movie.mkv",
    file_size: int = 1024,
    pick_code: str = "pc-1",
    parent_name: str = "影视",
    update_time: int = 1700000000,
) -> dict[str, Any]:
    return {
        "id": str(id),
        "type": type,
        "file_id": file_id,
        "parent_id": parent_id,
        "file_name": file_name,
        "file_size": file_size,
        "pick_code": pick_code,
        "parent_name": parent_name,
        "update_time": update_time,
        "sha1": "ABC",
    }


class FakeClient:
    """假的 p115client（按 date 返回预置页面）。"""

    def __init__(self, pages: dict[str, Any] | None = None) -> None:
        #: date → 页面数据
        self.pages = pages or {"*": {"count": 0, "next_page": False, "list": []}}
        self.calls: list[dict[str, Any]] = []

    async def life_behavior_detail(
        self, payload: dict[str, Any], app: str = "web", **kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append(dict(payload))
        date = str(payload.get("date") or "")
        page = self.pages.get(date) or self.pages.get("*") or {
            "count": 0,
            "next_page": False,
            "list": [],
        }
        return {"state": True, "code": 0, "data": page}


class TestEventParsing:
    def test_maps_fields(self) -> None:
        event = ev._to_event(_event())
        assert event is not None
        assert event.id == 100
        assert event.type == ev.TYPE_UPLOAD_FILE
        assert event.pick_code == "pc-1"
        assert event.parent_id == "cid-parent"
        assert event.file_size == 1024
        assert event.is_delete is False

    def test_rejects_invalid_id(self) -> None:
        assert ev._to_event({"id": "", "type": 1}) is None
        assert ev._to_event({"id": "abc", "type": 1}) is None

    def test_delete_event_flagged(self) -> None:
        event = ev._to_event(_event(type=ev.TYPE_DELETE_FILE))
        assert event is not None
        assert event.is_delete is True

    def test_type_names_known(self) -> None:
        assert ev.TYPE_NAMES[ev.TYPE_UPLOAD_FILE] == "upload_file"
        assert ev.TYPE_NAMES[ev.TYPE_DELETE_FILE] == "delete_file"


class TestFetchEvents:
    async def test_uses_date_parameter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """**按天拉**——``date`` 是唯一有效的增量参数。"""
        client = FakeClient(
            {
                "2026-10-27": {
                    "count": 1,
                    "next_page": False,
                    "list": [_event(id=300, pick_code="c")],
                }
            }
        )
        result = await ev.fetch_events(client, ["2026-10-27"])
        assert [e.id for e in result.events] == [300]
        assert client.calls[0]["date"] == "2026-10-27"

    async def test_merges_multiple_dates(self) -> None:
        """多天的事件合并到一起。"""
        client = FakeClient(
            {
                "2026-10-26": {
                    "count": 1,
                    "next_page": False,
                    "list": [_event(id=100, pick_code="a")],
                },
                "2026-10-27": {
                    "count": 1,
                    "next_page": False,
                    "list": [_event(id=200, pick_code="b")],
                },
            }
        )
        result = await ev.fetch_events(client, ["2026-10-26", "2026-10-27"])
        assert sorted(e.id for e in result.events) == [100, 200]
        assert result.raw_count == 2

    async def test_filters_event_types(self) -> None:
        """目录事件（17/20）与浏览事件（7）等**不处理**。"""
        client = FakeClient(
            {
                "*": {
                    "count": 4,
                    "next_page": False,
                    "list": [
                        _event(id=400, type=ev.TYPE_UPLOAD_FILE, pick_code="a"),
                        _event(id=300, type=ev.TYPE_NEW_FOLDER, pick_code="b"),
                        _event(id=200, type=ev.TYPE_FOLDER_RENAME, pick_code="c"),
                        _event(id=100, type=7, pick_code="d"),
                    ],
                }
            }
        )
        result = await ev.fetch_events(client, ["2026-10-27"])
        assert [e.id for e in result.events] == [400]

    async def test_history_full_flagged(self) -> None:
        """count == 10000 → 标记事件流已满（可能挤掉了中间的事件）。"""
        client = FakeClient(
            {
                "*": {
                    "count": ev.EVENT_HISTORY_LIMIT,
                    "next_page": True,
                    "list": [_event(id=1)],
                }
            }
        )
        result = await ev.fetch_events(client, ["2026-10-27"])
        assert result.history_full is True

    async def test_count_at_limit_but_not_truncated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """count 恒为 10000——但只要 next_page=False 就不算「拉满」。

        这条很关键：``count`` 是**事件流总容量**，拿它判断截断会误报。
        """
        client = FakeClient(
            {
                "*": {
                    "count": ev.EVENT_HISTORY_LIMIT,
                    "next_page": False,
                    "list": [_event(id=1)],
                }
            }
        )
        result = await ev.fetch_events(client, ["2026-10-27"])
        # count 到上限但当天数据已拉完 —— 不算历史被挤掉
        assert result.raw_count == 1

    async def test_raises_on_error(self) -> None:
        class BadClient:
            async def life_behavior_detail(self, *a: Any, **k: Any) -> dict:
                raise RuntimeError("boom")

        with pytest.raises(ev.EventFetchError):
            await ev.fetch_events(BadClient(), ["2026-10-27"])


class TestDateHelpers:
    def test_date_range_inclusive(self) -> None:
        assert ev.date_range("2026-10-01", "2026-10-03") == [
            "2026-10-01",
            "2026-10-02",
            "2026-10-03",
        ]

    def test_date_range_single_day(self) -> None:
        assert ev.date_range("2026-10-01", "2026-10-01") == ["2026-10-01"]

    def test_date_range_reversed(self) -> None:
        assert ev.date_range("2026-10-03", "2026-10-01") == []

    def test_date_range_invalid(self) -> None:
        assert ev.date_range("bad", "2026-10-01") == []


class TestCursorStore:
    async def test_set_get_delete(self, db_session) -> None:
        key = settings_store.cursor_key(1)
        assert await settings_store.get_value(db_session, key) is None

        await settings_store.set_value(db_session, key, "12345")
        await db_session.commit()
        assert await settings_store.get_value(db_session, key) == "12345"

        await settings_store.delete_value(db_session, key)
        await db_session.commit()
        assert await settings_store.get_value(db_session, key) is None

    async def test_cursor_keys_differ_per_driver(self) -> None:
        assert settings_store.cursor_key(1) != settings_store.cursor_key(2)


class TestIncrementalErrors:
    async def test_requires_baseline(
        self, db_session, settings, driver_row, fake_driver
    ) -> None:
        """没建基线 → 明确报错（不是默默扫不全）。"""
        driver_row.type = "p115"
        await db_session.commit()

        with pytest.raises(incremental.IncrementalError) as exc:
            await incremental.run_incremental(db_session, settings, driver_row)
        assert exc.value.kind == "no_baseline"

    async def test_rejects_non_115(
        self, db_session, settings, driver_row, fake_driver
    ) -> None:
        driver_row.type = "guangya"
        await db_session.commit()
        with pytest.raises(incremental.IncrementalError) as exc:
            await incremental.run_incremental(db_session, settings, driver_row)
        assert exc.value.kind == "unsupported"

    async def test_corrupt_cursor(
        self, db_session, settings, driver_row, fake_driver
    ) -> None:
        driver_row.type = "p115"
        await db_session.commit()
        await settings_store.set_value(
            db_session, settings_store.cursor_key(driver_row.id), "not-a-date"
        )
        await db_session.commit()

        with pytest.raises(incremental.IncrementalError) as exc:
            await incremental.run_incremental(db_session, settings, driver_row)
        assert exc.value.kind == "no_baseline"


class TestIncrementalFlow:
    async def _setup(self, db_session, driver_row, tmp_path):
        """把 driver 配好（115 类型 + 输出目录 + 游标）。"""
        driver_row.type = "p115"
        driver_row.output_dir = str(tmp_path)
        driver_row.media_path = "/影视"
        driver_row.media_cid = "cid-root"
        driver_row.video_exts = "mkv,mp4"
        driver_row.image_exts = "jpg,png"
        driver_row.data_exts = "nfo"
        await db_session.commit()
        # 游标 = 今天 → 只拉今天的事件
        await settings_store.set_value(
            db_session,
            settings_store.cursor_key(driver_row.id),
            ev.today_str(),
        )
        await db_session.commit()

    async def test_creates_strm_for_new_file(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        """新文件 → 生成 strm，并推进游标。"""
        await self._setup(db_session, driver_row, tmp_path)

        client = FakeClient(
            {
                "*": {
                    "count": 1,
                    "next_page": False,
                    "list": [
                        _event(
                            id=500,
                            file_name="new.mkv",
                            pick_code="pc-new",
                            parent_id="cid-dir",
                        )
                    ],
                }
            }
        )
        _patch_driver(fake_driver, client, {"/影视/电影": "cid-dir"})

        stats = await incremental.run_incremental(
            db_session, settings, driver_row, driver_impl=_FakeDriverImpl(client)
        )

        assert stats.created == 1
        assert stats.events_fetched == 1
        assert stats.events_in_scope == 1
        # strm 真的落盘了
        strms = list(tmp_path.rglob("*.strm"))
        assert len(strms) == 1
        # 游标已写回（记为今天）
        cursor = await settings_store.get_value(
            db_session, settings_store.cursor_key(driver_row.id)
        )
        assert cursor == ev.today_str()
        assert stats.cursor_old == ev.today_str()
        assert stats.cursor_new == ev.today_str()

    async def test_skips_out_of_scope(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        """不在媒体库内的目录 → 跳过。"""
        await self._setup(db_session, driver_row, tmp_path)

        client = FakeClient(
            {
                "*": {
                    "count": 1,
                    "next_page": False,
                    "list": [
                        _event(
                            id=500,
                            file_name="x.mkv",
                            pick_code="pc-x",
                            parent_id="cid-other",
                        )
                    ],
                }
            }
        )
        _patch_driver(fake_driver, client, {"/备份/immich": "cid-other"})

        stats = await incremental.run_incremental(
            db_session, settings, driver_row, driver_impl=_FakeDriverImpl(client)
        )
        assert stats.created == 0
        assert stats.events_in_scope == 0

    async def test_deletes_strm_on_delete_event(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        """删除事件 → 删本地 strm + 记录。"""
        await self._setup(db_session, driver_row, tmp_path)

        # 先造一个本地记录 + strm
        from app.db.models import MediaFile

        strm = tmp_path / "movie.strm"
        strm.write_text("http://x/302/1/sig\n", encoding="utf-8")
        db_session.add(
            MediaFile(
                driver_id=driver_row.id,
                remote_id="pc-del",
                path="/影视/movie.mkv",
                name="movie.mkv",
                size=100,
                strm_path=str(strm),
                signature="sig",
                mtime=0,
                created_at=0,
                updated_at=0,
            )
        )
        await db_session.commit()

        client = FakeClient(
            {
                "*": {
                    "count": 1,
                    "next_page": False,
                    "list": [
                        _event(
                            id=600,
                            type=ev.TYPE_DELETE_FILE,
                            pick_code="pc-del",
                            parent_id="0",
                        )
                    ],
                }
            }
        )
        _patch_driver(fake_driver, client, {})

        stats = await incremental.run_incremental(
            db_session, settings, driver_row, driver_impl=_FakeDriverImpl(client)
        )
        assert stats.deleted == 1
        assert not strm.exists()

    async def test_delete_event_without_local_record_is_skipped(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        """删除事件但本地没记录 → 跳过（不报错）。"""
        await self._setup(db_session, driver_row, tmp_path)
        client = FakeClient(
            {
                "*": {
                    "count": 1,
                    "next_page": False,
                    "list": [
                        _event(
                            id=600,
                            type=ev.TYPE_DELETE_FILE,
                            pick_code="pc-unknown",
                            parent_id="0",
                        )
                    ],
                }
            }
        )
        _patch_driver(fake_driver, client, {})

        stats = await incremental.run_incremental(
            db_session, settings, driver_row, driver_impl=_FakeDriverImpl(client)
        )
        assert stats.deleted == 0
        assert stats.skipped_deleted == 1

    async def test_path_conflict_is_skipped(
        self, db_session, settings, driver_row, fake_driver, tmp_path
    ):
        """同一 pick_code 但路径不同（目录改名）→ 跳过 + 计冲突。"""
        await self._setup(db_session, driver_row, tmp_path)

        from app.db.models import MediaFile

        db_session.add(
            MediaFile(
                driver_id=driver_row.id,
                remote_id="pc-moved",
                path="/影视/旧目录/movie.mkv",
                name="movie.mkv",
                size=100,
                strm_path=str(tmp_path / "old.strm"),
                signature="sig",
                mtime=0,
                created_at=0,
                updated_at=0,
            )
        )
        await db_session.commit()

        client = FakeClient(
            {
                "*": {
                    "count": 1,
                    "next_page": False,
                    "list": [
                        _event(
                            id=700,
                            file_name="movie.mkv",
                            pick_code="pc-moved",
                            parent_id="cid-new",
                        )
                    ],
                }
            }
        )
        _patch_driver(fake_driver, client, {"/影视/新目录": "cid-new"})

        stats = await incremental.run_incremental(
            db_session, settings, driver_row, driver_impl=_FakeDriverImpl(client)
        )
        assert stats.conflicts == 1
        assert stats.created == 0


class _FakeDriverImpl:
    """假的 115 驱动实例（只需 ``_client``）。"""

    def __init__(self, client: Any) -> None:
        self._client = client


def _patch_driver(fake_driver, client, path_map: dict[str, str]) -> None:
    """把假驱动的 ``_client`` 换成假的，并 mock ``tool.get_path``。"""
    fake_driver._client = client  # type: ignore[attr-defined]

    import p115client.tool as tool_mod

    async def _get_path(_client: Any, cid: Any, **kwargs: Any) -> str:
        for path, value in path_map.items():
            if str(value) == str(cid):
                return path
        return ""

    tool_mod.get_path = _get_path  # type: ignore[assignment]
