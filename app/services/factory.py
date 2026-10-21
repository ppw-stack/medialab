"""适配器工厂：数据库配置 → 活着的适配器实例。

两个层次刻意分开：

- **配置行**（``sites`` / ``downloader_instances`` 表）是持久化事实。
- **适配器实例**持有 HTTP 连接池，必须复用（每轮新建连接池会耗尽
  PT 站与下载器的连接配额）。因此工厂缓存实例，并在配置变更时
  通过 :meth:`AdapterFactory.invalidate` 重建。
"""

from __future__ import annotations

from app.adapters.downloaders.base import DownloaderAdapter
from app.adapters.downloaders.qbittorrent import QbittorrentAdapter
from app.adapters.downloaders.transmission import TransmissionAdapter
from app.adapters.sites import resolve as resolve_site_adapter
from app.adapters.sites.base import SiteAdapter, SiteProfile
from app.core.logging import get_logger
from app.db.models import DownloaderInstance, Site

logger = get_logger(__name__)


def profile_from_site(site: Site) -> SiteProfile:
    """DB 行 → 站点配置对象。"""
    return SiteProfile(
        name=site.name,
        domain=site.domain,
    )


class AdapterFactory:
    """适配器实例缓存。

    线程/协程安全由 FastAPI 的单事件循环 + APScheduler 同循环保证；
    若将来引入多进程部署，此处需换为进程内单例 + IPC。
    """

    def __init__(self) -> None:
        self._downloaders: dict[int, DownloaderAdapter] = {}
        self._sites: dict[int, SiteAdapter] = {}

    # ------------------------------------------------------------ 下载器
    def downloader(self, row: DownloaderInstance) -> DownloaderAdapter:
        """获取（或创建）下载器适配器。"""
        cached = self._downloaders.get(row.id)
        if cached is not None:
            return cached

        password = row.password
        adapter = self._build_downloader(row, password)
        self._downloaders[row.id] = adapter
        logger.info(
            "已连接下载器 %s(%s)",
            row.name,
            row.type,
        )
        return adapter

    def _build_downloader(
        self, row: DownloaderInstance, password: str | None
    ) -> DownloaderAdapter:
        match row.type:
            case "qbittorrent":
                return QbittorrentAdapter(
                    row.name,
                    row.url,
                    row.username or "",
                    password or "",
                    verify_tls=row.verify_tls,
                )
            case "transmission":
                return TransmissionAdapter(
                    row.name,
                    row.url,
                    row.username or "",
                    password or "",
                    verify_tls=row.verify_tls,
                )
            case "deluge" | "aria2":
                # M2 仅实现 qBittorrent + Transmission；其余按 FR-1.3 后续实现
                raise NotImplementedError(
                    f"下载器类型 {row.type} 自 M3 起实现（FR-1）"
                )
            case _:
                raise ValueError(f"未知下载器类型：{row.type}")

    # ------------------------------------------------------------ 站点
    def site(self, row: Site, *, cookies: str | None = None) -> SiteAdapter:
        """获取（或创建）站点适配器。

        :class:`Site.adapter` 字段决定具体类（默认 ``mteam``），
        不可识别的值抛 :class:`ValueError`（注册中心不存兜底）。
        """
        cached = self._sites.get(row.id)
        if cached is not None:
            return cached
        adapter_cls = resolve_site_adapter(row.adapter)
        apikey = row.apikey
        # 调用方显式传入的 cookies 优先于 DB 值（覆盖场景）
        resolved_cookies = cookies or row.cookies
        adapter = adapter_cls(
            profile_from_site(row),
            cookies=resolved_cookies,
            apikey=apikey,
        )
        self._sites[row.id] = adapter
        return adapter

    def site_rss_url(self, row: Site) -> str | None:
        """站点拉取 URL（含 passkey，R6 高敏感）。

        兼容层方法（方法名保留以避免 API 变更）。
        API 模式站点（如 m-team 有 apikey）可能未配置此字段——
        此时返回 None，由 adapter 内部决定走 API 还是 HTML。
        调用方不应假定站点一定有此 URL（FR-1.5 适配层收拢差异）。
        """
        return row.rss_url or None

    # ------------------------------------------------------------ 失效
    def invalidate_downloader(self, downloader_id: int) -> None:
        """丢弃缓存，下次访问时用新配置重建。

        旧连接池由 :meth:`aclose_all` 或 GC 回收；此处不阻塞式关闭，
        避免配置保存请求被网络 IO 拖住。
        """
        self._downloaders.pop(downloader_id, None)

    def invalidate_site(self, site_id: int) -> None:
        self._sites.pop(site_id, None)

    async def aclose_all(self) -> None:
        """关闭全部连接池（优雅停机）。"""
        for dl in list(self._downloaders.values()):
            try:
                await dl.close()
            except Exception as exc:
                logger.warning("关闭下载器连接失败：%s", exc)
        for sa in list(self._sites.values()):
            try:
                await sa.close()
            except Exception as exc:
                logger.warning("关闭站点连接失败：%s", exc)
        self._downloaders.clear()
        self._sites.clear()

    async def aclose(self) -> None:
        """:meth:`aclose_all` 的别名，保持调用点语义清晰。"""
        await self.aclose_all()


#: 全局工厂（lifespan 初始化）
_factory: AdapterFactory | None = None



def init_factory() -> AdapterFactory:
    global _factory
    _factory = AdapterFactory()
    return _factory


def get_factory() -> AdapterFactory:
    if _factory is None:
        raise RuntimeError("适配器工厂未初始化")
    return _factory


def invalidate_downloader(downloader_id: int) -> None:
    """丢弃缓存的下载器适配器实例。

    **工厂未初始化时是 no-op**——配置变更后的缓存失效是「尽力而为」：
    DB 写入已经提交成功，不该因为找不到工厂就把整个请求变成 500
    （下次访问时会用新配置重建 anyway）。
    """
    if _factory is not None:
        _factory.invalidate_downloader(downloader_id)


def invalidate_site(site_id: int) -> None:
    """丢弃缓存的站点适配器实例（工厂未初始化时是 no-op，见上）。"""
    if _factory is not None:
        _factory.invalidate_site(site_id)
