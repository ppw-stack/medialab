"""分层约束测试（Phase 2）。

依赖矩阵必须是**单向链**：

```
domain → core → db → adapters → services → webdav → api
```

``webdav`` 是协议适配层（ASGI 守卫 + wsgidav 挂载），位于 services 与
api 之间：它可以依赖 services / db / core，但 services 不得反向依赖它。
DAV 根配置的实现因此放在 ``app/services/dav_root.py``，
``app/webdav/paths.py`` 只做兼容转发。

任何逆向依赖都是架构腐化——比如之前 ``app/core/windows.py`` 从
``app.db.models`` 导入领域枚举（core 不该依赖 db），导致 core 层
反向依赖持久化层。

本文件把这条约束写成测试，防止后续重构再引入逆向依赖。
"""

from __future__ import annotations

import ast
import collections
from pathlib import Path

import pytest

#: 各层的名字与合法依赖顺序（数字越小越靠底层）。
#:
#: 注意 ``core`` 是**被所有层依赖**的公共设施（日志、配置、时间），
#: 所以「core → db」才是逆向（core 是更底层），而「adapters → core」
#: 是正常的向下依赖。
LAYER_ORDER: dict[str, int] = {
    "domain": 0,
    "core": 1,
    "db": 2,
    "adapters": 3,
    "services": 4,
    "webdav": 5,
    "api": 6,
}


def _is_allowed(src: str, dst: str) -> bool:
    """src 层能否依赖 dst 层。

    层号越小越靠底层（domain=0 最底，api=6 最上）。
    依赖「同层或更底层」是正常的（services → core、services → db）；
    依赖「更上层」才是逆向（core → db、services → webdav）。

    ``app`` 是应用装配根（``app/main.py`` / ``app/__init__.py``），
    负责把所有层接起来，故允许依赖任意层。

    **未登记的层不再放行**——新增 ``app/<pkg>`` 必须显式加入
    :data:`LAYER_ORDER`，否则这里会拒绝，避免新包绕过分层约束。
    """
    if src == "app":
        return True
    if src not in LAYER_ORDER or dst not in LAYER_ORDER:
        return False
    return LAYER_ORDER[dst] <= LAYER_ORDER[src]


def _collect_layer_edges() -> list[tuple[str, str, str]]:
    """扫描全仓，产出 ``(源层, 目标层, 源文件)`` 列表。"""
    edges: list[tuple[str, str, str]] = []
    for path in sorted(Path("app").rglob("*.py")):
        parts = path.parts
        if parts[0] != "app":
            continue
        src_layer = parts[1] if len(parts) > 2 else "app"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("app"):
                    targets.append(node.module)
            elif isinstance(node, ast.Import):
                targets.extend(a.name for a in node.names if a.name.startswith("app"))
            for tgt in targets:
                segs = tgt.split(".")
                if len(segs) < 2:
                    continue
                dst_layer = segs[1]
                edges.append((src_layer, dst_layer, str(path)))
    return edges


@pytest.fixture(scope="module")
def layer_edges() -> list[tuple[str, str, str]]:
    return _collect_layer_edges()


def test_no_reverse_layer_dependencies(layer_edges) -> None:
    """任何层都不得依赖**更上层**（逆向依赖 = 架构腐化）。"""
    violations: list[str] = []
    for src, dst, path in layer_edges:
        if not _is_allowed(src, dst):
            violations.append(f"{src} → {dst}  ({path})")
    assert not violations, (
        "发现逆向依赖（上层依赖下层）：\n  " + "\n  ".join(violations)
    )


def test_core_does_not_depend_on_db() -> None:
    """core 不该依赖 db——这是历史上唯一的逆向依赖，单列一条断言。"""
    offenders = [
        path
        for src, dst, path in _collect_layer_edges()
        if src == "core" and dst == "db"
    ]
    assert not offenders, f"core 依赖了 db：{offenders}"


def test_domain_depends_on_nothing() -> None:
    """domain 层不依赖任何 app 包——它是最底层。"""
    offenders = [
        path
        for src, dst, path in _collect_layer_edges()
        if src == "domain" and dst != "domain"
    ]
    assert not offenders, f"domain 不该依赖其他层：{offenders}"


def test_adapters_do_not_depend_on_services_or_api() -> None:
    """适配层不依赖服务层 / API 层（适配器应可独立单测）。"""
    offenders = [
        path
        for src, dst, path in _collect_layer_edges()
        if src == "adapters" and dst in ("services", "api")
    ]
    assert not offenders, f"adapters 不该依赖 services/api：{offenders}"


def test_adapters_do_not_depend_on_db() -> None:
    """适配层不写数据库（AGENTS.md：适配层可独立单测）。"""
    offenders = [
        path
        for src, dst, path in _collect_layer_edges()
        if src == "adapters" and dst == "db"
    ]
    assert not offenders, f"adapters 不该依赖 db：{offenders}"


def test_api_does_not_depend_on_db_or_adapters() -> None:
    """API 层不直接碰 DB / 适配层——业务逻辑全在 service 层。

    此前 ``app/api/routes/`` 里有 40 处 ``session_scope`` 直写 SQL，
    还有 11 处直接构造 / 调用适配器（测连、拉取测试、列任务……）。
    路由层同时依赖 DB 与网络，无法脱离 IO 单测。

    现在 API 层只依赖 ``api`` / ``core`` / ``services`` 三层。
    """
    offenders = [
        f"{dst}  ({path})"
        for src, dst, path in _collect_layer_edges()
        if src == "api" and dst in ("db", "adapters")
    ]
    assert not offenders, (
        "API 层不该直接依赖 db / adapters：\n  " + "\n  ".join(offenders)
    )


def test_api_does_not_use_session_scope() -> None:
    """API 层不得出现 ``session_scope``——DB 访问一律走 service。"""
    offenders: list[str] = []
    for path in sorted(Path("app/api").rglob("*.py")):
        if "session_scope" in path.read_text(encoding="utf-8"):
            offenders.append(str(path))
    assert not offenders, f"API 层不该直接用 session_scope：{offenders}"


def test_services_do_not_import_fastapi() -> None:
    """服务层不 import fastapi——HTTP 是 API 层的事。

    服务层抛 :class:`app.services.errors.ServiceError`（带状态码），
    由 API 层的全局异常处理器翻译。

    只看**真实的 import 语句**（不看文档字符串——``errors.py`` 的
    docstring 里提到 fastapi 是解释性的，不是依赖）。
    """
    offenders: list[str] = []
    for path in sorted(Path("app/services").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(n == "fastapi" or n.startswith("fastapi.") for n in names):
                offenders.append(str(path))
                break
    assert not offenders, f"services 不该 import fastapi：{offenders}"


def test_services_do_not_raise_http_exception() -> None:
    """服务层不得抛 ``HTTPException``——统一用 :class:`ServiceError`。"""
    offenders: list[str] = []
    for path in sorted(Path("app/services").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.asname or a.name for a in node.names]
            else:
                continue
            if "HTTPException" in names:
                offenders.append(str(path))
                break
    assert not offenders, f"services 不该用 HTTPException：{offenders}"


def test_layer_order_is_sane() -> None:
    """层级顺序本身要合理（防止有人改坏了这张表）。"""
    assert list(LAYER_ORDER) == [
        "domain", "core", "db", "adapters", "services", "webdav", "api"
    ]
    assert LAYER_ORDER["domain"] < LAYER_ORDER["core"] < LAYER_ORDER["db"]
    assert LAYER_ORDER["db"] < LAYER_ORDER["adapters"] < LAYER_ORDER["services"]
    assert LAYER_ORDER["services"] < LAYER_ORDER["webdav"] < LAYER_ORDER["api"]


def test_all_app_packages_are_registered() -> None:
    """新增 app 子包必须登记层级，否则会绕过逆向依赖检查。"""
    packages = {
        p.relative_to("app").parts[0]
        for p in Path("app").iterdir()
        if p.is_dir() and (p / "__init__.py").exists()
    }
    unregistered = packages - set(LAYER_ORDER)
    assert not unregistered, f"未登记到 LAYER_ORDER 的 app 子包：{sorted(unregistered)}"


def test_services_do_not_depend_on_webdav() -> None:
    """services 不得反向依赖 webdav——DAV 根实现放在 services 层。"""
    offenders = [
        path
        for src, dst, path in _collect_layer_edges()
        if src == "services" and dst == "webdav"
    ]
    assert not offenders, f"services 反向依赖 webdav：{offenders}"


def test_webdav_does_not_use_session_scope() -> None:
    """webdav 是协议层，DB 访问一律走 services。"""
    offenders = [
        str(path)
        for path in sorted(Path("app/webdav").rglob("*.py"))
        if "session_scope" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"webdav 不该直接用 session_scope：{offenders}"


def test_all_layers_have_files() -> None:
    """每层都要有文件（防止层被误删导致检查失效）。"""
    counts: collections.Counter[str] = collections.Counter(
        layer for layer, _, _ in _collect_layer_edges()
    )
    for layer in LAYER_ORDER:
        assert counts[layer] > 0, f"层 {layer} 没有文件"
