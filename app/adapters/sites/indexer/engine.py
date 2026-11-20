"""站点索引引擎（移植 MoviePilot 的搜索/字幕/用户数据调度）。

与 MoviePilot 的 ``IndexerModule`` 相比，去掉了模块框架、DB、统计与站点
认证，只保留**解析**：选择专站 spider 或通用 ``SiteSpider``，执行搜索；
按 ``schema`` 选择用户数据解析器并做一次框架回退。

站点配置由调用方传入（规则 + 用户 cookie/apikey），本层不读数据库。
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Mapping
from functools import lru_cache
from types import MappingProxyType
from typing import Any

from app.adapters.sites.indexer.compat import text as text_tools
from app.adapters.sites.indexer.compat.exceptions import RateLimitExceededException
from app.adapters.sites.indexer.compat.log import logger
from app.adapters.sites.indexer.compat.throttle import (
    async_wait_for_site_request,
    wait_for_site_request,
)
from app.adapters.sites.indexer.compat.types import MediaType
from app.adapters.sites.indexer.parser import SiteParserBase
from app.adapters.sites.indexer.rules import SiteRules, load_site_rules
from app.adapters.sites.indexer.spider import JsonList, SiteSpider
from app.adapters.sites.indexer.spider.fluxtorrent import FluxTorrentSpider
from app.adapters.sites.indexer.spider.haidan import HaiDanSpider
from app.adapters.sites.indexer.spider.hdarea import HDAreaSpider
from app.adapters.sites.indexer.spider.hddolby import HddolbySpider
from app.adapters.sites.indexer.spider.hdwing import HDWingPromotionMixin
from app.adapters.sites.indexer.spider.milkie import MilkieSpider
from app.adapters.sites.indexer.spider.mtorrent import MTorrentSpider
from app.adapters.sites.indexer.spider.piggo import PiggoSpider
from app.adapters.sites.indexer.spider.rousi import RousiSpider
from app.adapters.sites.indexer.spider.sunnypt import SunnyPTSpider
from app.adapters.sites.indexer.spider.tnode import TNodeSpider
from app.adapters.sites.indexer.spider.torrentleech import TorrentLeech
from app.adapters.sites.indexer.spider.yema import YemaSpider

#: 专站爬虫（站点配置 ``parser`` 字段）
SPIDER_PARSER_CLASSES: dict[str, type[Any] | None] = {
    "TNodeSpider": TNodeSpider,
    "TorrentLeech": TorrentLeech,
    "mTorrent": MTorrentSpider,
    "Yema": YemaSpider,
    "Haidan": HaiDanSpider,
    "HDDolby": HddolbySpider,
    "HDArea": HDAreaSpider,
    "HDWing": None,  # 运行时替换为 HDWingSpider
    "RousiPro": RousiSpider,
    "SunnyPT": SunnyPTSpider,
    "Piggo": PiggoSpider,
    "Milkie": MilkieSpider,
    "FluxTorrent": FluxTorrentSpider,
}

#: 搜索参数白名单：只传各专站声明的参数，避免同步/异步契约分叉
_SPECIALIZED_SEARCH_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "TNodeSpider": ("keyword", "page"),
    "TorrentLeech": ("keyword", "mtype", "page"),
    "mTorrent": ("keyword", "mtype", "cat", "page"),
    "SunnyPT": ("keyword", "mtype", "cat", "page"),
    "Piggo": ("keyword", "mtype", "cat", "page"),
    "Yema": ("keyword", "mtype", "cat", "page"),
    "Haidan": ("keyword", "mtype"),
    "HDDolby": ("keyword", "mtype", "cat", "page"),
    "HDArea": ("keyword", "mtype", "cat", "page"),
    "HDWing": ("keyword", "mtype", "cat", "page"),
    "RousiPro": ("keyword", "mtype", "cat", "page"),
    "Milkie": ("keyword", "mtype", "page"),
    "FluxTorrent": ("keyword", "page"),
}

#: 字幕搜索参数白名单
_SPECIALIZED_SUBTITLE_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "mTorrent": ("keyword", "page", "search_type"),
}

#: 用户数据解析失败后允许尝试的通用框架
_COMMON_SITE_USERDATA_FALLBACK_SCHEMAS = frozenset(
    {"Gazelle", "NexusPhp", "Unit3d"}
)


#: 是否用站点 ajax 促销接口补全 HDWinG 的优惠标记。
#:
#: 2026-11-20 实测：该接口受 Cloudflare Turnstile 保护，未过闸时对**任意**
#: 种子 id（含已过期的）都返回兜底的 ``pro_free`` 且 ``timeout`` 恒为空——
#: 直接采用会把「限时免费但已到期」的种子全判成永久免费。实测对照
#: ``promotion.php?id=`` 权威页：838960 的免费窗口 2026-11-11 已结束，
#: 接口仍回 pro_free。
#:
#: 关掉补全后，列表页的优惠占位符不会被填充 → 规则匹配不到 pro_* →
#: 全部按「非免费」处理。这是**保守且安全**的口径（宁可漏推免费种，
#: 也不把付费种当免费种推下去）。要恢复精确判定需接回逐种
#: ``promotion.php?id=`` 查询。
HDWING_PROMOTION_INJECTION = False


class HDWingPageSpider(HDWingPromotionMixin, SiteSpider):
    """在通用页面解析前补全 HDWinG 促销状态（当前默认关闭，见上方常量）。"""

    def __init__(
        self,
        indexer: dict[str, Any],
        keyword: str | None = None,
        mtype: MediaType | None = None,
        cat: str | None = None,
        page: int | None = 0,
    ) -> None:
        self._promotion_site = {
            key: indexer.get(key)
            for key in ("id", "domain", "limit_seconds", "limit_interval", "limit_count")
        }
        super().__init__(indexer=indexer, keyword=keyword, mtype=mtype, cat=cat, page=page)

    def parse(self, html_text: str) -> JsonList:
        if html_text and HDWING_PROMOTION_INJECTION:
            html_text = self.inject_promotions(html_text)
        return super().parse(html_text)


class HDWingSpider:
    """HDWinG 的同步/异步搜索入口。"""

    _size = 100

    def __init__(self, indexer: dict[str, Any]) -> None:
        self._indexer = indexer
        self._spider: HDWingPageSpider | None = None

    @property
    def error_detail(self) -> str | None:
        return self._spider.error_detail if self._spider else None

    @classmethod
    def get_search_page_size(cls, keyword: str | None = None) -> int | None:
        return cls._size

    def search(
        self,
        keyword: str | None = None,
        mtype: MediaType | None = None,
        cat: str | None = None,
        page: int | None = 0,
    ) -> tuple[bool, JsonList]:
        spider = HDWingPageSpider(self._indexer, keyword=keyword, mtype=mtype, cat=cat, page=page)
        self._spider = spider
        return spider.is_error, spider.get_torrents()

    async def async_search(
        self,
        keyword: str | None = None,
        mtype: MediaType | None = None,
        cat: str | None = None,
        page: int | None = 0,
    ) -> tuple[bool, JsonList]:
        spider = HDWingPageSpider(self._indexer, keyword=keyword, mtype=mtype, cat=cat, page=page)
        self._spider = spider
        return spider.is_error, await spider.async_get_torrents()


SPIDER_PARSER_CLASSES["HDWing"] = HDWingSpider


@lru_cache(maxsize=1)
def _load_schema_classes() -> list[type[SiteParserBase]]:
    """导入 parser 包下所有带 ``schema`` 的解析器类。"""
    import app.adapters.sites.indexer.parser as parser_pkg

    classes: list[type[SiteParserBase]] = []
    for module_info in pkgutil.iter_modules(parser_pkg.__path__):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{parser_pkg.__name__}.{module_info.name}")
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and issubclass(value, SiteParserBase)
                and getattr(value, "schema", None) is not None
            ):
                classes.append(value)
    return classes


class IndexerEngine:
    """站点解析引擎。"""

    def __init__(self, rules: SiteRules | None = None) -> None:
        self.rules = rules or load_site_rules()
        self._schema_classes = _load_schema_classes()

    # ------------------------------------------------------------------ 搜索
    @staticmethod
    def get_site_domain(url: str | None) -> str:
        """返回站点域名（netloc 小写）。"""
        from urllib.parse import urlsplit

        if not url:
            return ""
        return (urlsplit(url).netloc or "").lower().removeprefix("www.")

    def _search_allowed(self, site: Mapping[str, Any], keyword: str | None) -> bool:
        if keyword and site.get("language") == "en" and text_tools.contains_chinese(keyword):
            logger.info("%s 不支持中文搜索", site.get("name"))
            return False
        return True

    def _build_request(
        self,
        site: Mapping[str, Any],
        keyword: str | None,
        mtype: MediaType | None,
        cat: str | None,
        page: int,
        search_type: str,
    ) -> tuple[Any, dict[str, Any]] | None:
        if search_type == "subtitles" and not site.get("subtitles"):
            return None
        if not self._search_allowed(site, keyword):
            return None
        search_word = (
            text_tools.remove_punctuation(keyword, replacement=" ", allow_space=True)
            if keyword
            else keyword
        )
        parser_name = str(site.get("parser") or "")
        parser_class = SPIDER_PARSER_CLASSES.get(parser_name)

        if search_type == "subtitles":
            names = _SPECIALIZED_SUBTITLE_ARGUMENTS.get(parser_name)
            if parser_class and names:
                available = {"keyword": search_word, "page": page, "search_type": search_type}
                return parser_class, {name: available[name] for name in names}
            return None, {
                "search_word": search_word,
                "indexer": site,
                "page": page,
                "search_type": search_type,
            }

        names = _SPECIALIZED_SEARCH_ARGUMENTS.get(parser_name)
        if parser_class and names:
            available = {"keyword": search_word, "mtype": mtype, "cat": cat, "page": page}
            return parser_class, {name: available[name] for name in names}
        return None, {
            "search_word": search_word,
            "indexer": site,
            "mtype": mtype,
            "cat": cat,
            "page": page,
        }

    def search_torrents(
        self,
        site: dict[str, Any],
        keyword: str | None = None,
        mtype: MediaType | None = None,
        cat: str | None = None,
        page: int = 0,
    ) -> list[dict[str, Any]]:
        """同步搜索单站；失败抛异常（空列表只表示站点确实没有种子）。"""
        request = self._build_request(site, keyword, mtype, cat, page, "torrents")
        if request is None:
            return []
        parser_class, arguments = request
        wait_for_site_request(site)
        if parser_class is not None:
            spider = parser_class(site)
            error_flag, result = spider.search(**arguments)
            if error_flag:
                raise _fetch_error(site, getattr(spider, "error_detail", None))
            return result
        spider = _generic_spider(arguments)
        result = spider.get_torrents()
        if spider.is_error:
            raise _fetch_error(site, spider.error_detail)
        return result

    async def async_search_torrents(
        self,
        site: dict[str, Any],
        keyword: str | None = None,
        mtype: MediaType | None = None,
        cat: str | None = None,
        page: int = 0,
    ) -> list[dict[str, Any]]:
        """异步搜索单站。"""
        request = self._build_request(site, keyword, mtype, cat, page, "torrents")
        if request is None:
            return []
        parser_class, arguments = request
        await async_wait_for_site_request(site)
        if parser_class is not None:
            spider = parser_class(site)
            error_flag, result = await spider.async_search(**arguments)
            if error_flag:
                raise _fetch_error(site, getattr(spider, "error_detail", None))
            return result
        spider = _generic_spider(arguments)
        result = await spider.async_get_torrents()
        if spider.is_error:
            raise _fetch_error(site, spider.error_detail)
        return result

    # ------------------------------------------------------------------ 用户数据
    def _parser_for(self, site: Mapping[str, Any], schema_value: str) -> SiteParserBase | None:
        for cls in self._schema_classes:
            if cls.schema and cls.schema.value == schema_value:
                site_url = str(site.get("url") or site.get("domain") or "")
                userdata_path = str(site.get("userdata_path") or "").strip()
                if userdata_path and site_url:
                    from urllib.parse import urljoin

                    if userdata_path.startswith(("http://", "https://")):
                        site_url = userdata_path
                    else:
                        site_url = urljoin(f"{site_url.rstrip('/')}/", userdata_path.lstrip("/"))
                return cls(
                    site_name=site.get("name"),
                    url=site_url,
                    site_cookie=site.get("cookie"),
                    apikey=site.get("apikey"),
                    token=site.get("token"),
                    ua=site.get("ua"),
                    proxy=bool(site.get("proxy")),
                    api_url=site.get("api_url"),
                )
        return None

    def refresh_userdata(self, site: dict[str, Any]) -> dict[str, Any] | None:
        """按站点 ``schema`` 解析用户资料；失败抛异常，未实现返回 ``None``。"""
        schema_value = str(site.get("schema") or "")
        parser = self._parser_for(site, schema_value)
        if parser is None:
            if not site.get("public"):
                logger.warning(
                    "站点 %s 未找到解析器，schema=%s", site.get("name"), schema_value
                )
            return None
        parser.parse()
        allow_fallback = getattr(parser, "allow_schema_fallback", True)
        if not parser.userid and not site.get("public") and allow_fallback:
            tried = {schema_value}
            for cls in self._schema_classes:
                if cls.schema is None:
                    continue
                value = cls.schema.value
                if value in tried or value not in _COMMON_SITE_USERDATA_FALLBACK_SCHEMAS:
                    continue
                tried.add(value)
                alt = self._parser_for(site, value)
                if alt is None:
                    continue
                try:
                    alt.parse()
                except Exception as error:
                    logger.error("站点 %s 以 %s 解析失败：%s", site.get("name"), value, error)
                    continue
                if alt.userid:
                    parser = alt
                    break
        return {
            "username": parser.username,
            "userid": parser.userid,
            "user_level": parser.user_level,
            "join_at": parser.join_at,
            "upload": parser.upload,
            "download": parser.download,
            "ratio": parser.ratio,
            "bonus": parser.bonus,
            "seeding": parser.seeding,
            "seeding_size": parser.seeding_size,
            "leeching": parser.leeching,
            "seeding_info": parser.seeding_info,
            "err_msg": parser.err_msg,
        }

    # ------------------------------------------------------------------ 字幕
    def search_subtitles(
        self,
        site: dict[str, Any],
        keyword: str | None = None,
        page: int = 0,
    ) -> list[dict[str, Any]]:
        """同步字幕搜索（无字幕配置的站点返回空列表）。

        :param site: 站点配置
        :param keyword: 搜索关键词
        :param page: 从零开始的页码
        :return: 字幕条目列表
        :raises SiteFetchError: 站点请求失败或响应无法解析
        """
        request = self._build_request(site, keyword, None, None, page, "subtitles")
        if request is None:
            return []
        parser_class, arguments = request
        wait_for_site_request(site)
        if parser_class is not None:
            spider = parser_class(site)
            error_flag, result = spider.search(**arguments)
            if error_flag:
                raise _fetch_error(site, getattr(spider, "error_detail", None))
            return result
        spider = _generic_spider(arguments)
        result = spider.get_torrents()
        if spider.is_error:
            raise _fetch_error(site, spider.error_detail)
        return result


def _generic_spider(arguments: Mapping[str, Any]) -> SiteSpider:
    """把通用搜索参数字典映射到 SiteSpider 的构造参数。"""
    return SiteSpider(
        indexer=arguments.get("indexer"),
        keyword=arguments.get("search_word"),
        mtype=arguments.get("mtype"),
        cat=arguments.get("cat"),
        page=arguments.get("page") or 0,
        search_type=arguments.get("search_type") or "torrents",
    )


def _fetch_error(site: Mapping[str, Any], detail: str | None) -> Exception:
    from app.adapters.sites.base import SiteFetchError

    name = site.get("name") or site.get("domain") or ""
    return SiteFetchError(f"{name} 解析失败：{detail or '页面解析失败'}")


__all__ = [
    "SPIDER_PARSER_CLASSES",
    "IndexerEngine",
    "MappingProxyType",
    "RateLimitExceededException",
]
