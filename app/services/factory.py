"""适配器工厂：数据库配置 → 活着的适配器实例。

两个层次刻意分开：

- **配置行**（``sites`` / ``downloader_instances`` 表）是持久化事实。
- **适配器实例**持有 HTTP 连接池，必须复用（每轮新建连接池会耗尽
  PT 站与下载器的连接配额）。因此工厂缓存实例，并在配置变更时
  通过 :meth:`AdapterFactory.invalidate` 重建。

凭据解密在此集中完成——适配层本身不碰数据库、不碰密钥。
"""

from __future__ import annotations

from datetime import timedelta

from app.adapters.downloaders.base import DownloaderAdapter
from app.adapters.downloaders.qbittorrent import QbittorrentAdapter
from app.adapters.sites import resolve as resolve_site_adapter
from app.adapters.sites.base import SiteAdapter, SiteProfile
from app.core.crypto import CredentialCipher
from app.core.logging import get_logger
from app.core.timeutils import ClockOffset, utcnow
from app.db.models import DownloaderInstance, Site

logger = get_logger(__name__)

#: 时钟偏差重新探测的间隔（FR-3.0.2 自动探测，24h 足够）
CLOCK_PROBE_INTERVAL = timedelta(hours=24)


class CredentialMissing(RuntimeError):
    """凭据无法解密（密钥不匹配或记录被篡改，R6）。"""


def decrypt_field(
    cipher: CredentialCipher, value: str | None, *, owner_type: str, owner_id: str | int
) -> str | None:
    """解密单个凭据字段；失败时抛错而非静默降级（FR-7 禁止明文回退）。"""
    if not value:
        return None
    plain = cipher.try_decrypt(value, owner_type=owner_type, owner_id=owner_id)
    if plain is None:
        raise CredentialMissing(
            f"{owner_type}:{owner_id} 的凭据无法解密——"
            "请检查 ENCRYPTION_KEY 是否与写入时一致（FR-7.1）"
        )
    return plain


def profile_from_site(site: Site) -> SiteProfile:
    """DB 行 → 站点配置对象（FR-1.5.2）。"""
    from app.adapters.sites.base import SiteFeature

    return SiteProfile(
        name=site.name,
        domain=site.domain,
        detail_path_template=site.detail_path_template,
        rss_time_pattern=site.rss_time_pattern,
        window_field_map=dict(site.window_field_map or {}),
        free_type_map={str(k): str(v) for k, v in (site.free_type_map or {}).items()},
        download_factor_map={
            str(k): str(v) for k, v in (site.download_factor_map or {}).items()
        },
        time_unit_multiplier=site.time_unit_multiplier,
        features=frozenset({SiteFeature.RSS, SiteFeature.CLOCK_API, SiteFeature.TIMESTAMP_FIELD}),
    )


def clock_offset_for(site: Site) -> ClockOffset:
    """站点的时钟偏差；超过 24h 未探测则视为过期。"""
    probed = site.clock_offset_probed_at
    if probed and (utcnow() - probed) < CLOCK_PROBE_INTERVAL:
        return ClockOffset(offset=timedelta(seconds=site.clock_offset), probed_at=probed)
    return ClockOffset(offset=timedelta(seconds=site.clock_offset))


class AdapterFactory:
    """适配器实例缓存。

    线程/协程安全由 FastAPI 的单事件循环 + APScheduler 同循环保证；
    若将来引入多进程部署，此处需换为进程内单例 + IPC。
    """

    def __init__(self, cipher: CredentialCipher) -> None:
        self._cipher = cipher
        self._downloaders: dict[int, DownloaderAdapter] = {}
        self._sites: dict[int, SiteAdapter] = {}

    # ------------------------------------------------------------ 下载器
    def downloader(self, row: DownloaderInstance) -> DownloaderAdapter:
        """获取（或创建）下载器适配器。"""
        cached = self._downloaders.get(row.id)
        if cached is not None:
            return cached

        password = decrypt_field(
            self._cipher,
            row.password_enc,
            owner_type="downloader",
            owner_id=row.name,
        )
        adapter = self._build_downloader(row, password)
        self._downloaders[row.id] = adapter
        logger.info(
            "已连接下载器 %s(%s)，可删除数据=%s",
            row.name,
            row.type,
            adapter.can_delete_data,
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
            case "transmission" | "deluge" | "aria2":
                # M2 仅实现 qBittorrent；其余按 FR-1.3 声明能力后降级
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
        # m-team 等现代站点的 API key（FR-1.5.1：解密后注入，不暴露到日志）
        apikey = None
        if row.apikey_enc:
            apikey = decrypt_field(
                self._cipher,
                row.apikey_enc,
                owner_type="site",
                owner_id=row.name,
            )
        # NexusPHP 等站点 session cookies（加密解密后注入）
        # 调用方显式传入的 cookies 优先于 DB 值（覆盖场景）
        db_cookies = None
        if row.cookies_enc:
            db_cookies = decrypt_field(
                self._cipher,
                row.cookies_enc,
                owner_type="site",
                owner_id=row.name,
            )
        resolved_cookies = cookies or db_cookies
        adapter = adapter_cls(
            profile_from_site(row),
            cookies=resolved_cookies,
            apikey=apikey,
            clock_offset=clock_offset_for(row),
        )
        self._sites[row.id] = adapter
        return adapter

    def site_rss_url(self, row: Site) -> str | None:
        """解密站点 RSS 地址（含 passkey，R6 高敏感）。

        API 模式站点（如 m-team 有 apikey）可能未配置 RSS 地址——
        此时返回 None，由 adapter 内部决定走 API 还是需要 URL。
        调用方不应假定站点一定有 RSS URL（FR-1.5 适配层收拢差异）。
        """
        url = decrypt_field(
            self._cipher, row.rss_url_enc, owner_type="site", owner_id=row.name
        )
        return url or None

    async def downloader_for_id(self, downloader_id: int) -> object | None:
        """按 id 取下载器，缺失返回 None（Cleaner 用）。

        异步签名——调用方需 await。
        """
        from sqlalchemy import select

        from app.db.models import DownloaderInstance
        from app.db.session import session_scope

        async with session_scope() as s:
            row = (
                await s.execute(
                    select(DownloaderInstance).where(
                        DownloaderInstance.id == downloader_id,
                        DownloaderInstance.enabled.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return self.downloader(row)


    # ------------------------------------------------------------ 失效
    def invalidate_downloader(self, downloader_id: int) -> None:
        """丢弃缓存，下次访问时用新配置重建。

        旧连接池由 :meth:`aclose_all` 或 GC 回收；此处不阻塞式关闭，
        避免配置保存请求被网络 IO 拖住。
        """
        self._downloaders.pop(downloader_id, None)

    def invalidate_site(self, site_id: int) -> None:
        self._sites.pop(site_id, None)

    def invalidate_all(self) -> None:
        self._downloaders.clear()
        self._sites.clear()

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



def init_factory(cipher: CredentialCipher) -> AdapterFactory:
    global _factory
    _factory = AdapterFactory(cipher)
    return _factory


def get_factory() -> AdapterFactory:
    if _factory is None:
        raise RuntimeError("适配器工厂未初始化")
    return _factory
