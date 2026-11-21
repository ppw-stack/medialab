# 站点解析引擎（Migrated from MoviePilot）

把 MoviePilot 的**站点数据解析**能力移植进本项目：规则包驱动的搜索/浏览、
字幕、用户数据解析，以及专站爬虫（API 站、加密下载、RSS 配方）。

**不包含站点认证**：cookie / apikey 仍由本项目 `sites` 表提供，不做登录、
换 token、RSA 加密等认证逻辑。

## 目录结构

```text
app/adapters/sites/
├── generic.py                # GenericAdapter：把引擎接入 SiteAdapter 契约
└── indexer/                  # 移植子系统（不依赖 db/services）
    ├── rules.py              # 规则包加载 / 域名匹配
    ├── rules/indexers.json   # 解密后的站点规则（184 站，v3.0.33）
    ├── engine.py             # 搜索/字幕/用户数据调度（替代 IndexerModule）
    ├── http.py               # RequestUtils / AsyncRequestUtils（httpx 实现）
    ├── parser/               # 24 个用户数据解析器（NexusPHP/Unit3D/Gazelle/…）
    ├── spider/               # 通用 SiteSpider + 13 个专站爬虫
    ├── rss.py / download.py  # RSS 配方、下载协议解释
    └── compat/               # MoviePilot 内部工具的兼容实现
```

分层约束：`indexer/` 只依赖标准库、`httpx`、`pyquery`、`jinja2`、`dateutil`
与本项目 `app/core`；**不 import db / services**，可独立单测。

## 使用方式

1. 在 `sites` 表新增站点，`adapter` 填 `generic`，`domain` 填站点域名
   （规则按域名匹配，含 `ext_domains`）。前端「站点 → 新增」里选
   **通用(规则包)** 后会出现 184 站的规则下拉，选中自动填名称与域名
   （目录来自 `GET /api/v1/sites/rules`）。
2. cookie 填浏览器里的站点 Cookie；API 站（m-team 等）填 apikey。
3. 其余流程（拉取、免费窗口、推送、用户数据）不变。

```python
from app.adapters.sites import resolve

adapter = resolve("generic")(
    SiteProfile(name="自由农场", domain="https://pt.0ff.cc/"),
    cookies="uid=...; pass=...",
)
items = await adapter.search_torrents({"keyword": "示例"}, page_number=1)
```

## 能力

| 能力 | 说明 |
|---|---|
| 搜索 / 浏览 | `search` / `browse` 规则：路径、参数、分类、IMDb ID 格式、批量模式 |
| 字段抽取 | `torrents.fields`：title/description/size/seeders/leechers/grabs/date/促销/hr/labels/category |
| 促销 | `downloadvolumefactor` / `uploadvolumefactor` 的 `case` 映射，转成现有免费窗口输入 |
| 字幕 | `SiteAdapter.search_subtitles()`：无配置返回 `[]`，失败抛 `SiteFetchError`；HTML 站点走 `subtitles` 规则，m-team 走 API |
| 用户数据 | `schema` 选择 24 个解析器之一，失败时回退 NexusPhp/Gazelle/Unit3d |
| RSS | `rss.py` 的专站配方（生成链接页 XPath / 表单参数） |
| 专站 spider | m-team / HDArea / HDDolby / HDWinG / TNode / Yema / Rousi / SunnyPT / Piggo / Milkie / FluxTorrent / Haidan / TorrentLeech |

## 规则包更新

规则来自 `MoviePilot-Resources/resources.v3`（Fernet 加密的 pickle）。本项目自用，
已把解密结果随包放在 `indexer/rules/indexers.json`。更新流程：

```bash
python scripts/import_site_rules.py \
  --resource-dir /path/to/MoviePilot-Resources/resources.v3
```

脚本会从扩展模块的压缩字符串表里提取 Fernet 密钥、解密 `.bin`、安全
反序列化后重写 JSON。资源包属于上游闭源分发，生成物不要再对外发布。

## 与旧适配器的关系

- 旧版 10 个站点专用 adapter（mteam / hhanclub / hdsky / …）及 `parsers/`
  **已全部删除**：站点差异只由规则包表达，注册表里只剩 `generic`。
- 旧 adapter 附带的特化能力（签到、HDWing 逐种优惠查询、API 站 token 下载、
  时魔）随实现一并移除；`site_user_data` 的 `hourly_bonus*` 列也已从模型与
  旧库中删除（bootstrap 幂等 drop）。

## 已知限制

- **不做站点认证**：登录、换 token、验证码仍由用户提供 cookie。
- **无 Rust 加速**：`compat/rust.py` 始终回退 Python 解析。
- **API 站连通性**：有 `parser` 的站点优先调用专站 spider 的 `test_connection()`
  （m-team / HDDolby / HDArea / Yema / Rousi / SunnyPT / Piggo / FluxTorrent /
  TNode / Milkie / Haidan），不依赖首页登录标记。
- **Cloudflare 只识别不破解**：命中挑战页按失败处理（抛 `SiteFetchError`）。
- **下载地址**：HTML 站从详情页锚点提取；API 站（m-team / hdarea / piggo）
  优先调专站 spider 的 `get_download_url`。详情页路径默认 `details.php?id=`，
  个别站需在 `torrents.details_path` 覆盖。
- **远端冷却只在进程内**：`compat/throttle.py` 用内存字典，不跨进程/重启。
- 规则选择器里的 `:has("x")` 已在 `SiteSpider` 侧规范化（cssselect 不接受引号写法）。
