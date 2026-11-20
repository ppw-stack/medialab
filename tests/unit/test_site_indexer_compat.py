"""站点索引移植层的回归测试（PR #27 审查修复的契约固化）。

覆盖的都是**真会炸**的路径，不是风格问题：

- ``cached`` 装饰器对 ``async def`` 必须缓存 **await 之后的结果**——
  缓存协程对象会让第二次命中抛「cannot reuse already awaited coroutine」。
- ``MemoryBackend`` 必须提供 ``region`` / ``delete``——猪猪站按这两个参数用。
- 规则包缺失 / 损坏时的报错必须带**文件路径**。
- 兜底 UA 不能是字面量 ``"None"``（会被站点 WAF 拒绝）。
- 协议侧 unix 秒只在**落库边界**转成北京时间字符串。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.adapters.sites.generic import _published_at, _to_feed_item
from app.adapters.sites.indexer.compat.cache import MemoryBackend, cached
from app.adapters.sites.indexer.compat.settings import get_runtime_setting
from app.adapters.sites.indexer.rules import load_site_rules
from app.adapters.sites.indexer.spider.fluxtorrent import FluxTorrentSpider
from app.adapters.sites.indexer.spider.hdarea import HDAreaSpider
from app.adapters.sites.indexer.spider.milkie import MilkieSpider


class TestCachedDecorator:
    """``cached`` 必须同时支持同步与异步函数。"""

    def test_sync_function_is_cached(self) -> None:
        calls: list[int] = []

        @cached(maxsize=8, ttl=60)
        def double(value: int) -> int:
            calls.append(value)
            return value * 2

        assert double(3) == 6
        assert double(3) == 6
        assert calls == [3]

    @pytest.mark.asyncio
    async def test_async_function_caches_awaited_result(self) -> None:
        """异步函数缓存的是 await 后的值，不是协程对象。"""
        calls: list[int] = []

        @cached(maxsize=8, ttl=60)
        async def double(value: int) -> int:
            calls.append(value)
            return value * 2

        assert await double(3) == 6
        # 第二次命中缓存——若缓存的是协程对象，这里会抛 RuntimeError
        assert await double(3) == 6
        assert calls == [3]


class TestMemoryBackend:
    """``MemoryBackend`` 必须兼容 MoviePilot 的 region / delete 用法。"""

    def test_region_argument_is_accepted_and_ignored(self) -> None:
        backend = MemoryBackend(maxsize=8, ttl=60)
        backend.set("key", "value", region="piggo-catalog")
        assert backend.get("key", region="piggo-catalog") == "value"
        assert backend.get("key") == "value"

    def test_delete_removes_entry(self) -> None:
        backend = MemoryBackend(maxsize=8, ttl=60)
        backend.set("key", "value", region="piggo-catalog")
        backend.delete("key", region="piggo-catalog")
        assert backend.get("key") is None

    def test_delete_missing_key_is_noop(self) -> None:
        backend = MemoryBackend(maxsize=8, ttl=60)
        backend.delete("missing", region="piggo-catalog")
        assert backend.get("missing") is None

    def test_expired_entry_reads_as_miss(self) -> None:
        backend = MemoryBackend(maxsize=8, ttl=0)
        backend.set("key", "value")
        assert backend.get("key") is None


class TestRulesLoading:
    """规则包缺失 / 损坏时必须报出带路径的错误。"""

    def test_missing_file_reports_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import app.adapters.sites.indexer.rules as rules_module

        missing = tmp_path / "indexers.json"
        monkeypatch.setattr(rules_module, "_RULES_PATH", missing)
        rules_module.load_site_rules.cache_clear()
        try:
            with pytest.raises(RuntimeError) as excinfo:
                rules_module.load_site_rules()
            assert str(missing) in str(excinfo.value)
        finally:
            rules_module.load_site_rules.cache_clear()

    def test_broken_json_reports_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import app.adapters.sites.indexer.rules as rules_module

        broken = tmp_path / "indexers.json"
        broken.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(rules_module, "_RULES_PATH", broken)
        rules_module.load_site_rules.cache_clear()
        try:
            with pytest.raises(RuntimeError) as excinfo:
                rules_module.load_site_rules()
            assert str(broken) in str(excinfo.value)
        finally:
            rules_module.load_site_rules.cache_clear()

    def test_shipped_rules_pack_loads(self) -> None:
        rules = load_site_rules()
        assert rules.version
        assert len(rules.indexers) >= 180


class TestUserAgentFallback:
    """兜底 UA 不能是字面量 "None"。"""

    def test_normal_user_agent_is_mapped(self) -> None:
        ua = get_runtime_setting("NORMAL_USER_AGENT")
        assert ua
        assert ua != "None"

    def test_flux_torrent_user_agent_never_literal_none(self) -> None:
        assert FluxTorrentSpider.get_user_agent(None) != "None"
        assert FluxTorrentSpider.get_user_agent("") != "None"
        assert FluxTorrentSpider.get_user_agent("Custom/1.0") == "Custom/1.0"


class TestPublishedAtBoundary:
    """协议侧 unix 秒只在落库边界转成北京时间字符串。"""

    def test_unix_seconds_convert_to_beijing_string(self) -> None:
        # 2024-05-01 12:00:00 +08:00 == 2024-05-01 04:00:00 UTC
        assert _published_at(1714536000) == "2024-05-01 12:00:00"

    def test_datetime_text_kept_as_is(self) -> None:
        assert _published_at("2024-05-01 12:00:00") == "2024-05-01 12:00:00"

    def test_empty_value_is_none(self) -> None:
        assert _published_at(None) is None
        assert _published_at("") is None

    def test_feed_item_publishes_unix_input(self) -> None:
        item = _to_feed_item({"title": "x", "size": 1, "pubdate": 1714536000}, {})
        assert item.published_at == "2024-05-01 12:00:00"


class TestSpiderInputValidation:
    """构造器必须校验必填字段，别在运行期才炸。"""

    def test_milkie_requires_domain(self) -> None:
        with pytest.raises(ValueError):
            MilkieSpider({"name": "milkie"})

    def test_milkie_accepts_domain(self) -> None:
        spider = MilkieSpider({"name": "milkie", "domain": "https://milkie.example/"})
        assert spider is not None

    def test_hdarea_tolerates_category_without_id(self) -> None:
        """脏规则包只降级该分类，不让整个适配器实例化失败。"""
        spider = HDAreaSpider({
            "name": "hdarea",
            "domain": "https://hdarea.example/",
            "apikey": "k",
            "category": {"movie": [{"id": 1}, {"name": "缺 id"}]},
        })
        assert spider is not None

    def test_hdarea_reads_api_payload(self) -> None:
        """API 站点的协议侧时间保持 unix 秒，不在这一层转字符串。"""
        spider = HDAreaSpider({
            "name": "hdarea",
            "domain": "https://hdarea.example/",
            "apikey": "k",
        })
        payload = {
            "id": 1,
            "name": "示例",
            "category_id": 401,
            "added": 1714555200,
            "size": 1024,
        }
        item = spider._parse_item(payload)
        assert item["pubdate"] == 1714555200
        assert json.dumps(item, ensure_ascii=False)
