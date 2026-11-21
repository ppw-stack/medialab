"""死代码检测（Phase 3）。

判断死代码的**方法**（不是靠眼力翻 grep）：

1. **符号级**：把「定义点」与「引用点」都解析出来，剔除定义本身后
   引用数为 0 → 死代码。
2. **方法级**：对类方法，扫全仓的 ``self.<name>(`` / ``obj.<name>(`` /
   ``<Class>.<name>(`` 调用；只出现在定义处 → 死代码。
3. **枚举成员级**：对枚举成员，扫 ``<Enum>.<MEMBER>`` 的引用；
   只出现在定义处 → 死成员。

**陷阱**（本文件在注释里逐条记下）：

- ``getattr`` / ``importlib`` 动态取名 → 静态扫不到，会误判。
  故本文件只对**明确**的点名调用计数，动态路径由人工确认。
- 测试文件里的引用**不算**活引用——测试自己构造的数据不代表生产用。
  故本文件分别统计「生产引用」与「测试引用」，只把生产引用为 0 的
  判为死代码候选。
- ``__all__`` 里列出的名字是**导出契约**，不算引用（re-export）。
- 同名方法在不同类里各算一次，不混淆。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

#: 仓库根（相对本文件）
REPO = Path(__file__).resolve().parents[2]

#: 生产代码目录
APP_DIR = REPO / "app"

#: 测试目录（引用**不算**活引用）
TEST_DIRS = (REPO / "tests",)


def _py_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _prod_sources() -> list[Path]:
    #: 站点解析引擎（app/adapters/sites/indexer）是 MoviePilot 上游代码的
    #: 移植，保留其原有写法（含 ``link=`` 等），不参与本项目的死代码扫描。
    upstream = APP_DIR / "adapters" / "sites" / "indexer"
    return [p for p in _py_files(APP_DIR) if upstream not in p.parents]


def _test_sources() -> list[Path]:
    out: list[Path] = []
    for d in TEST_DIRS:
        if d.exists():
            out.extend(_py_files(d))
    return out


def _read(paths: list[Path]) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in paths)


def _prod_text() -> str:
    return _read(_prod_sources())


def _test_text() -> str:
    return _read(_test_sources())


def _count_refs(text: str, pattern: str) -> int:
    return len(re.findall(pattern, text))


# ==================================================================== 用例


class TestTaskKind:
    """``TaskKind`` 曾有 8 个成员，只有 ``ADD_TORRENT`` 在用——其余已删。"""

    _DEAD = (
        "REMOVE_TORRENT",
        "SET_CATEGORY",
        "SET_PATH",
        "SET_RATE_LIMIT",
        "PAUSE",
        "RESUME",
        "ABORT_WINDOW",
    )

    @pytest.mark.parametrize("member", _DEAD)
    def test_dead_members_removed(self, member: str) -> None:
        from app.services.task_queue import TaskKind

        assert not hasattr(TaskKind, member)

    @pytest.mark.parametrize("member", _DEAD)
    def test_dead_members_have_no_prod_references(self, member: str) -> None:
        prod = _prod_text()
        refs = _count_refs(prod, rf"TaskKind\.{member}\b")
        assert refs == 0, f"TaskKind.{member} 在生产代码里有 {refs} 处引用——它可能不是死代码"

    def test_add_torrent_is_alive(self) -> None:
        prod = _prod_text()
        assert _count_refs(prod, r"TaskKind\.ADD_TORRENT\b") > 0


class TestPushStatus:
    """``PushStatus.PENDING`` 从未写入（NULL 已表示「未推过」）——已删。"""

    def test_pending_is_removed(self) -> None:
        from app.domain.enums import PushStatus

        assert not hasattr(PushStatus, "PENDING")

    def test_pending_is_dead(self) -> None:
        prod = _prod_text()
        assert _count_refs(prod, r"PushStatus\.PENDING\b") == 0

    def test_pushed_and_failed_are_alive(self) -> None:
        """``PUSHED`` / ``FAILED`` 都在用——不能一起删。"""
        prod = _prod_text()
        assert _count_refs(prod, r"PushStatus\.PUSHED\b") > 0
        assert _count_refs(prod, r"PushStatus\.FAILED\b") > 0


class TestFeedItemLink:
    """``FeedItem.link`` 只被写、从未被读——已删除。"""

    def test_link_field_is_gone(self) -> None:
        """字段已从 dataclass 移除。"""
        from app.adapters.sites.base import FeedItem

        assert not hasattr(FeedItem, "link")

    def test_link_is_never_read(self) -> None:
        """扫描所有 ``.link`` 属性访问（排除 ``rss_url`` 等复合名）。"""
        readers: list[str] = []
        for path in _prod_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr == "link"
                    and isinstance(node.ctx, ast.Load)
                ):
                    readers.append(f"{path}:{node.lineno}")
        assert not readers, f"FeedItem.link 被读了：{readers}"

    def test_link_is_never_written(self) -> None:
        """构造点也不再传 link=（否则 dataclass 会报 unexpected keyword）。"""
        prod = _prod_text()
        assert _count_refs(prod, r"\blink=") == 0


class TestDownloadTorrentUrl:
    """``SiteAdapter.download_torrent_url()`` 零调用——已删除。"""

    def test_method_is_gone(self) -> None:
        from app.adapters.sites.base import SiteAdapter

        assert not hasattr(SiteAdapter, "download_torrent_url")

    def test_never_called(self) -> None:
        prod = _prod_text()
        assert _count_refs(prod, r"\bdownload_torrent_url\b") == 0


class TestSingleWriterQueue:
    """``SingleWriterQueue.write()`` 零调用（见 1.6）。"""

    def test_write_never_called(self) -> None:
        prod = _prod_text()
        # 只排除 ``session.write`` / ``f.write`` 这类别的对象
        calls = re.findall(r"(?<![\w.])\w*_?queue\.write\(", prod, flags=re.I)
        assert not calls, f"队列的 write() 被调了：{calls}"


class TestFactoryDeadMethods:
    """工厂上两个零调用的方法——已删。"""

    def test_downloader_for_id_removed(self) -> None:
        from app.services.factory import AdapterFactory

        assert not hasattr(AdapterFactory, "downloader_for_id")

    def test_invalidate_all_removed(self) -> None:
        from app.services.factory import AdapterFactory

        assert not hasattr(AdapterFactory, "invalidate_all")


class TestDownloaderLocksIsBusy:
    """``DownloaderLocks.is_busy()`` 零调用——已删。"""

    def test_is_busy_removed(self) -> None:
        from app.adapters.downloaders.locks import DownloaderLocks

        assert not hasattr(DownloaderLocks, "is_busy")


class TestFactoryDefaultDownloader:
    """``factory.default_downloader()`` 零调用（见 2.2）。"""

    def test_removed(self) -> None:
        """该方法已删除（Phase 2 批次 2）。"""
        from app.services.factory import AdapterFactory

        assert not hasattr(AdapterFactory, "default_downloader")


class TestPushServicePushError:
    """``PushError`` 仍在用（1.3 手推合流后由 API 翻译）。"""

    def test_push_error_is_alive(self) -> None:
        prod = _prod_text()
        assert _count_refs(prod, r"\bPushError\b") > 0


class TestDomainEnumLocation:
    """领域枚举住在 ``app.domain.enums``，``db.models`` **不再 re-export**。

    re-export 会让人以为「枚举还是 db 层的东西」，于是新代码继续
    ``from app.db.models import FreeType``——分层就白改了。
    """

    def test_db_models_does_not_reexport_enums(self) -> None:
        import app.db.models as models

        for name in ("FreeType", "PushStatus", "FreeSource"):
            assert name not in models.__all__, (
                f"app.db.models 仍在 re-export {name}——请从 app.domain.enums 导入"
            )

    def test_domain_enums_is_the_single_source(self) -> None:
        from app.domain.enums import FreeSource, FreeType, PushStatus

        assert FreeType.PERMANENT == "freeleech_permanent"
        assert PushStatus.PUSHED == "pushed"
        assert FreeSource.API == "api"
