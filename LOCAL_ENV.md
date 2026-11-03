# 本地环境信息（Local Environment）

> 本文件记录**当前开发机的真实环境状态**，供后续接手的 agent / 开发者快速上手。
> 与 `README.md`（项目功能）、`DEPLOY.md`（部署流程）、`AGENTS.md`（协作规范）配套使用。
>
> **维护约定**：环境变更（换机器、改端口、换部署方式）后请同步更新本文件。
> 本文件不含明文密钥，只记录**获取方式**。

---

## 1. 主机

| 项 | 值 |
|---|---|
| 操作系统 | macOS 27.0.1 (Darwin 26A434) |
| 架构 | `arm64` (Apple Silicon) |
| 主机名 | 本机开发机 |
| LAN IP | `192.168.0.92` (en0) |
| 仓库路径 | `/Users/pw/工作/pt/medialab` |
| 分支 | `main` |
| 远端 | `https://github.com/ppw-stack/medialab.git` |

## 2. 工具链

| 工具 | 版本 | 备注 |
|---|---|---|
| Python | **3.12**（`.python-version` / `requires-python >=3.12`） | 系统自带 `python3` 是 3.9.6，**必须用 `uv`**，勿直接 `python3` |
| uv | 0.12.5 (Homebrew aarch64) | 依赖管理 + 运行入口 |
| Node.js | v26.7.0 | 仅前端用 |
| npm / npx | 12.0.2 | |
| Docker | 29.4.0 | |
| Docker Compose | v5.1.2 | |
| Git | 2.54.0 (Apple Git-157) | |

> ⚠️ `python3` 直接调用会拿到 3.9.6，语法与依赖都不兼容。**统一用 `uv run <cmd>`**。

## 3. 双环境架构

两套环境共享同一份代码，**数据 / 配置 / Token 完全隔离**。

| | dev | prod |
|---|---|---|
| 位置 | 本机（192.168.0.92） | 远端 `192.168.0.128`（Unraid Tower, x86_64） |
| 端口 | **8092** | **8092** |
| 容器名 | `medialab-dev` | `medialab` |
| 镜像 | `medialab:dev-arm64`（本地构建） | `ppw111/medialab:latest`（Docker Hub） |
| 数据目录 | `./data-dev/` | `/mnt/user/appdata/medialab/data` |
| 日志目录 | `./logs-dev/` | `/mnt/user/appdata/medialab/logs` |
| 凭据文件 | `.env` | `.env.prod` |
| Compose | `docker-compose.yml` | `docker-compose.prod.128.yml` |
| 用途 | 开发调试 | 生产运行 |

**SSH 访问 prod**：`ssh mp`（`~/.ssh/config` 中的别名 → `root@192.168.0.128`）

> ✅ **只有两个 compose 文件**（2026-10-21 起删掉了重复的本机 prod）：
>
> | 文件 | 目标 | 端口 | 数据 | 启动方式 |
> |---|---|---|---|---|
> | `docker-compose.yml` | dev（本机） | 8092 | `./data-dev` | `build` 本地构建 |
> | `docker-compose.prod.128.yml` | **远端** prod（128） | 8092 | `/mnt/user/appdata/medialab/data` | 拉 Docker Hub 镜像，无 `build` |
>
> `scripts/deploy.sh` **只管 dev**（原来还有 `prod` / `all` 分支指向已删除的
> 本机 prod compose，那个分支会把人引到错误的地方）。
> 部署到 128 要手动走 `docker-compose.prod.128.yml`（见 §9）。

## 4. 常用地址与端点

### 健康检查（重要）

真实健康端点是 **`/healthz`**，返回 JSON：

```json
{"status":"ok","version":"0.1.0","db":true}
```

> ⚠️ **`/health` 不是健康端点**。前端 SPA 有 fallback 路由，任何不存在的路径都返回 **200 + HTML**，
> 包括 `/health`。用它探活会得到**假阳性**。请用 `/healthz`（容器 healthcheck 也是用这个）。

| 端点 | 用途 |
|---|---|
| `/healthz` | 健康检查（`status` / `version` / `db`） |
| `/metrics` | Prometheus 指标 |
| `/api/v1/...` | REST API（需 Bearer Token） |

### 关键 API 路径

| 路径 | 说明 |
|---|---|
| `/api/v1/scheduler` | 调度作业列表（**注意：不是 `/scheduler/jobs`**） |
| `/api/v1/scheduler/runs` | 作业执行历史 |
| `/api/v1/scheduler/runs/{run_id}` | 单次执行详情 |
| `/api/v1/scheduler/runtime-params` | 作业运行参数（GET / PUT） |
| `/api/v1/scheduler/{job_name}/toggle` | 启停作业 |
| `/api/v1/scheduler/{job_name}/cron` | 改 cron（**落库**，跨重启保持，见 §6） |
| `/api/v1/cross-seed-monitor/downloaders` | 辅种下载器列表 |
| `/api/v1/cross-seed-monitor/snapshots` | 辅种快照列表 + 未抓过的下载器 |
| `/api/v1/cross-seed-monitor/space` | 辅种空间汇总（磁盘剩余 / 辅种占用） |
| `/api/v1/cross-seed-monitor/orphans` | 辅种孤儿文件统计（磁盘有、任务没了） |
| `/api/v1/cross-seed-monitor/downloaders/{id}/snapshot` | 单个辅种下载器快照详情 |
| `/api/v1/cross-seed-monitor/downloaders/{id}/refresh` | 手动抓取单个辅种下载器（await 完整抓取） |
| `/api/v1/cross-seed-monitor/downloaders/{id}/remove-torrents` | 批量删除种子（不限数量，分批发送；异常明细 + 统计卡片共用） |
| `/api/v1/cross-seed-monitor/downloaders/{id}/scan` | 实时扫描单个辅种下载器（调试用，不落库） |
| `/api/v1/sites` | 站点列表 |
| `/api/v1/sites/{site_id}/feed-test` | 站点拉取测试 |
| `/api/v1/sites/{site_id}/user-data` | 站点用户数据 |
| `/api/v1/user-data` | 全部站点用户数据（FR-1.6） |
| `/api/v1/dashboard` | 仪表盘统计 |
| `/api/v1/downloaders` | 下载器管理（删除走 40 位 hex 白名单） |
| `/api/v1/torrent-index/sites` | 种子索引 |
| `/api/v1/torrent-index/free-types` | 免费种分类 |

### 相关服务

| 服务 | 地址 | 备注 |
|---|---|---|
| qBittorrent（容器） | `http://192.168.0.92:8081` | 本机 qB 容器，宿主 8081 → 容器 8080 |
| Transmission | `http://192.168.0.92:9092` | `tr-local`，仪表盘日志显示**当前不可达** |
| Transmission | `http://192.168.0.172:9091` | `tr-172`，**间歇性超时**（已知现象） |
| dev medialab | http://localhost:8092 | |
| prod medialab | http://192.168.0.128:8092 | |

下载器实例存在 DB 表 `downloader_instances`（字段 `id` / `name` / `type` / `url` / `username` / `password` / `save_path` / `is_default` / `enabled` / `verify_tls` / `is_cross_seed`），
共 4 条：`qb-local`(qbittorrent) / `tr-local`(transmission) / `mock-cs` / `tr-172`(transmission)。

> `192.168.0.172:9090` 是 prod 视角下 qB 的地址（compose 注释所述），
> 与 dev 库里的 `192.168.0.92:8081` 是不同环境的两套配置。

### 辅种站的 NAS 挂载（孤儿文件统计用）

辅种数据在 **192.168.0.172**（NAS）上，medialab 容器看不到它——
除非把目录**只读挂载**进来。下载器本身**没有**浏览文件系统的接口
（TR 的 RPC spec 不提供 filesystem 浏览，qB 也没有列目录 API）。

128 上 Unraid **已自动挂好** CIFS：

```
//192.168.0.172/Public on /mnt/remotes/192.168.0.172_Public type cifs (rw,...)
```

故 prod 只需一行 compose 挂载（已加）：

```yaml
- /mnt/remotes/192.168.0.172_Public/home/downloads:/share/Public/home/downloads:rw
environment:
  CROSS_SEED_SCAN_DIRS: ${CROSS_SEED_SCAN_DIRS:-/share/Public/home/downloads/辅种}
```

> ✅ **`CROSS_SEED_SCAN_DIRS` 已落库**（2026-10-26 起）：
> env 来源（`AppSettings.cross_seed_scan_dirs`）只作**首次启动的默认值**
> （DB 空时回填）。之后改 env 无效——env 里已无对应配置生效路径，
> 残留的旧行被 `extra="ignore"` 静默忽略。
>
> **改扫描目录**：进入「设置 → 环境变量」tab 直接编辑（`PUT /api/v1/env-settings`），
> 改动立即生效 + 落库，**重启后保持**。详见「调度配置全部落库」章节。
>
> > **权限是 `rw`（不是 `ro`）**——孤儿文件删除功能需要写权限。
> 删除有三重安全闸（只删扫描目录的直接子项 / 删除前二次复核 /
> 审计日志），见 `app/services/orphan_deleter.py`。
>
> **路径必须与下载器里的 `downloadDir` 完全一致**（172 上辅种站的
> `downloadDir` 就是 `/share/Public/home/downloads/辅种`）。孤儿判定把
> 任务按 `downloadDir` 分组，路径不一致就分不对组。
>
> **可以配多个路径**（逗号分隔），每个目录独立扫描 + 独立统计。
> 辅种站实测有 3 种 `downloadDir`：`辅种`（4639 个种子）/ 父目录（5 个）/
> `game`（1 个）——只扫「辅种」目录时，父目录与 `game` 的任务**不参与**
> 「有主」判定（它们的文件本来也不在这个目录里）。
>
> ⚠️ **辅种目录被多个下载器共用**：实测 qB 有 6 个任务的 `save_path`
> 也在辅种目录里。故「有主」集合必须**汇总所有下载器**的任务——
> 只看辅种下载器会把那 6 个 qB 任务的数据误判成孤儿（实测误判 480 GB，
> 占「孤儿」总量的 78%）。
>
> **dev（本机 92）无法挂**：172 的 NFS 只导出给 `192.168.0.128`。
> 故 dev 的孤儿统计会降级（记 error「未配置扫描目录」），不影响其他功能。
>
> 全量扫 1696 个顶层条目 / 3.6 万文件：CIFS **约 40 秒**（NFS 57 秒）。

## 5. 凭据获取方式（不在此记录明文）

| 环境 | Token 位置 | 长度要求 |
|---|---|---|
| dev | `.env` → `WEB_TOKEN` | ≥ 32 字符 |
| prod | `.env.prod` → `WEB_TOKEN` | ≥ 32 字符，**必须与 dev 不同** |

密码同理存于 `WEB_PASSWORD`（≥ 12 位）。生成方式：

```bash
python -c "import secrets; print('WEB_TOKEN=' + secrets.token_urlsafe(32))"
```

`.env` / `.env.prod` 已被 `.gitignore` 排除，**严禁提交**。

### 认证方式

除登录页的 cookie 方案外，脚本 / CLI 走 Bearer Token：

```bash
curl -H "Authorization: Bearer $WEB_TOKEN" http://localhost:8092/api/v1/scheduler
```

## 6. 调度作业（7 个）

| 作业 | 默认 cron | 说明 |
|---|---|---|
| `fetch_all_sites` | `*/10 * * * *` | 全站拉取 |
| `push_free_torrents` | `*/30 * * * *` | 免费种推送（走 TaskQueue） |
| `monitor_free_window` | `5,15,25,35,45,55 * * * *` | 免费窗口看护 |
| `refresh_user_data` | `*/30 * * * *` | FR-1.6 用户数据刷新 |
| `drain_task_queue` | `*/5 * * * *` | 任务队列排空 |
| `daily_checkin` | `37 8 * * *` | 站点每日签到（FR-3.6，仅 3 站） |
| `cross_seed_maintenance` | `17 */3 * * *` | 辅种维护（巡查 → 落库快照 + 暂停异常 → 磁盘超阈删种子 → 孤儿扫描） |

专属参数：

- `push_free_torrents`：`push_min_remaining_hours` / `push_max_per_run` / `push_download_interval_ms`
- `monitor_free_window`：`monitor_pre_pause_minutes`

### 运行历史（2026-11-03 起）

「设置 → 调度 → 历史」弹框里能看到：

| 内容 | 来源 | 说明 |
|---|---|---|
| 顶部统计 | `job_runs` 聚合 | 成功率 / 平均耗时 / 最长耗时 / 最近结果摘要 |
| **任务步骤** | `result_json.steps` | 每步含聚合描述 + **逐项明细**（默认折叠） |
| **执行日志** | `result_json.logs` | 作业期间的 Python 日志（时序：哪步慢、中间发生了什么） |

- **逐项明细**回答「**是哪些**」（哪个站失败、推了哪些种）；**执行日志**回答「**什么时候 / 多慢**」
- 7 个作业都填明细；单步最多 50 条，超出显示「显示 N / 共 M」
- **接口分层**：`GET /scheduler/runs`（列表）剥掉日志与大字段；`GET /scheduler/runs/{run_id}`（详情）返回完整数据。前端点行时才拉详情
- **保留期 30 天**，由 `drain_task_queue` 顺带清理（它每 5 分钟跑一次）
- 日志条数受 `LOG_LEVEL` 影响：默认 `INFO` 收完整时间线；调成 `WARNING` 则只剩警告以上

### 调度配置**全部落库**（2026-10-24 起）

cron 与运行参数**都不再走 env**：

| 配置 | 默认值真源 | 用户改动的存放处 | 跨重启 |
|---|---|---|---|
| **cron** | 代码常量 `app/services/job_registry.py` 的 `DEFAULT_CRON` | `scheduler_job_settings.cron` | ✅ 保持 |
| **运行参数** | `app/services/runtime_params.py` 的 `PARAM_SPECS` | `app_runtime_params` | ✅ 保持 |

- 在「设置 → 调度 → 配置」弹框改 cron → **立即生效 + 落库**，重启后保持
- 改运行参数 → 同上（本来就是落库的）
- **改 env 无效**——env 里已无对应配置；残留的旧行会被静默忽略
  （`AppSettings` 用 `extra="ignore"`）
- 想改**默认**值 → 改代码里的 `DEFAULT_CRON` / `ParamSpec.default`
- `scheduler_job_settings` 表两列**互相独立**：改 cron 不碰启停，反之亦然

> ✅ **作业的启停（enabled）已持久化**（2026-10-21，commit `6be6a9d`）：
> 存在 `scheduler_job_settings` 表（`job_name` PK + `enabled` + `cron`）。
> `POST /api/v1/scheduler/{job_name}/toggle?enabled=false` 禁用某作业后，
> **重启容器保持停用**（启动期 `apply_overrides()` 回填，
> 被停用的作业根本不会被安排到 APScheduler）。
>
> ✅ **cron 也已持久化**（2026-10-24）：同一张表的 `cron` 列。
> `POST /api/v1/scheduler/{job_name}/cron` 改的 cron **重启后保持**。
>
> ⚠️ DB 写失败时 API 返 500（配置已在内存生效，但没存住）——
> 与 toggle 端点同一处理。用户会看到「生效了但没存住」的提示。
>
> 仍然**不落库**的只有作业**注册时机**与**函数实现**（代码里的事）。
>
> 排查小技巧：disable 的请求会出现在容器访问日志里，形如
> `POST /api/v1/scheduler/push_free_torrents/toggle?enabled=false`，
> 来源 IP 即操作者（本机是 `192.168.0.253`）。

## 7. 站点适配器（9 站）

`app/adapters/sites/`：`mteam` / `hdsky` / `hhanclub` / `audiences` / `hdhome` / `hddolby` / `ourbits` / `soulvoice` / `springsunday`

其中 7 站共用 `app/adapters/sites/parsers/nexus_php.py` 的 module-level helper。

**时区**：`settings.timezone` 默认 `Asia/Shanghai`（`app/core/config.py`），
必须与站点规则时区一致，否则保种时长 / 免费窗口计算会偏差。

**拉取失败的表达方式**（2026-10-21 修复，commit `3fcfef1`）：
`search_torrents` 返回 `[]` = "站点确实没有种子"（正常）；
拉取失败必须抛 `SiteFetchError`（`app/adapters/sites/base.py`）。
9 个 adapter 此前把 `httpx.HTTPError` 吞掉返回 `[]`，导致
`fetch_all_sites` 的 `failed_sites` 恒为 0，故障站完全不可见。
`feed_fetcher` 把 `SiteFetchError` 归入**可重试**一类（重试 3 次后
`status=http_error`），不是 `parse_error`。

> ⚠️ `hddolby` 站 DNS **间歇性**解析失败（`Name or service not known`），
> dev / prod 都会出现。属站点或网络侧问题，不是代码缺陷。
> 现在会在 `fetch_all_sites` 结果里如实报 `failed_sites: 1` 并点名
> `hddolby`，日志出现 `站点拉取失败 1 站：hddolby(...)`。

> ⚠️ `BreakerTripped` 熔断机制的**落库部分**从未实现：`feeds` 表不存在，
> `should_pull()` 等熔断代码已于 2026-10-21（commit `522a47b`）作为死代码删除。
> 但 `BreakerTripped` 异常类**保留**——mteam / hdhome 的下载路径会抛它，
> `feed_fetcher` 捕获后直接放弃本次拉取。
>
> 现存表：`sites` / `torrent_index` / `task_queue` / `app_runtime_params` /
> `job_runs` / `site_user_data` / `downloader_instances` /
> `scheduler_job_settings`（+ 历史遗留的 `torrents` 空表）。

### 7.1 每日签到（FR-3.6，2026-10-22）

`daily_checkin` 作业（cron `37 8 * * *`）逐站调用
`SiteAdapter.checkin()`。**9 站里 4 站实测可自动签到**：

| 站点 | 端点 | 状态 |
|---|---|---|
| hhanclub | `GET/POST /attendance.php` | ✅ 已实现（签到得憨豆） |
| soulvoice | `GET/POST /attendance.php` | ✅ 已实现（签到得魔力值） |
| hdhome | `GET/POST /attendance.php` | ✅ 已实现（签到得魔力值） |
| **hdsky** | 自建 `showup.php` 三段式 + 图形验证码 | ✅ **已实现（AI 读码）** |
| audiences | `GET /attendance.php` | ❌ Cloudflare Turnstile（`TurnstileError 600010`） |
| ourbits | `GET /attendance.php` | ❌ Turnstile + `form#attendance` |
| springsunday | — | ❌ 无签到功能（只有论坛"签到帖"） |
| mteam | — | ❌ mTorrent API 无签到端点 |
| hddolby | — | ❌ 未实现（SSL/DNS 失败，未验证） |

hdsky 的签到不是 NexusPHP 标准的 `/attendance.php`（本站 404），而是自建三段式：

```
POST showup.php {action:check}                    → {"success":true,"sign":bool}   免验证码查状态
POST image_code_ajax.php {action:new}             → {"success":true,"code":"<32hex>"}
GET  image.php?action=regimage&imagehash=<code>   → 150x40 PNG 图形验证码
POST showup.php {action:showup, imagehash, imagestring} → 提交
```

服务端对两种失败返回**不同错误码**（这是重试逻辑的前提）：

- `invalid_imagehash` —— 验证码**答错**（含大小写：站点大小写敏感）→ 换图重试
- `date_unmatch` —— 验证码**答对**但今日已签 → 幂等，不重试

> ⚠️ hdsky 的 AI 读码需要 prod `.env.prod` 配 `AI_API_BASE` / `AI_API_KEY` /
> `AI_MODEL` 三项。**未配置时该站降级为「需人工签到」，作业不会变红。**
> 端点须 prod 可直连（实测 `llmapi.banstack.top` 与 `api.minimaxi.com` 均可达；
> OpenAI 直连不可达）。详见 AGENTS.md「FR-3.6 AI 读码」。

> ⚠️ **Cloudflare Turnstile 本仓库不实现**：audiences / ourbits 的挑战在
> 服务端环境**无法完成**（实测 `TurnstileError 600010`：headless / 有头真实
> Chrome、反自动化参数、关代理、把 `brunhild.challenges.cloudflare.com`
> 映射到 IPv4 anycast、先访问首页 — 全部无效）。这两站返回 `UNSUPPORTED`。
>
> 上游语义（`xiaomlove/nexusphp` 的 `public/attendance.php`）：**GET 只查状态**，
> 仅当站点 `captcha.attendance.enabled = false` 时才顺带执行签到；**POST 才是
> 执行签到**。这 3 站实测 GET 即完成签到（说明无图形验证码），
> POST 仅作兜底。已签到时 POST 返回 200 + "已经签到过"，**不是错误**。
>
> 结果**不落库**（不建表）——作业返回值进 `job_runs.result_json`
> （含 `summary` + `steps`），前端「调度 → 历史」直接可见。
> 解析规则用真实页面固化成 fixture：`tests/fixtures/*_attendance.html`。

### 7.2 时魔抓取（FR-1.6 增补，2026-10-22）

`refresh_user_data` 作业现在抓 **时魔**（每小时获得的站点积分）。
数据在 NexusPHP 的 `/mybonus.php`——**该页 navbar 与 `/userdetails.php`
同一套**（`stdhead()` 渲染），所以 4 业务字段 + 时魔**一页拿全**，
每站仍是 1 次请求（navbar 解析不出 4 字段时才回退 `/userdetails.php`）。

**时魔 = "合计"口径**（含官种/勋章/后宫等各项加成），不是"基础时魔"。
各站实测（2026-10-22，dev 真实 cookie）：

| 站点 | 站点叫法 | 合计取法 | 上限信号 |
|---|---|---|---|
| hdsky | 魔力值 | headline（无合计表） | 进度条 `alt="11.8%"` |
| ourbits | 魔力值 | headline（无合计表） | 进度条 `alt="95.3%"` |
| soulvoice | 魔力值 | 合计表「合计」列 | 进度条 `alt="94.7%"` |
| hdhome | 魔力值 | headline **+ 额外奖励**（黄星/保种） | 进度条 `alt="55.4%"`（共 3 根，第 1 根是总进度） |
| hhanclub | 憨豆 | div 网格的**跨行单元格**（`row-span-4`） | 进度条 `alt="37.1%"` |
| audiences | 爆米花 | 侧栏 `mybonus-side__value`（已含 18% 勋章加成） | 文案「已达上限」+ 110% |
| springsunday | 茉莉 | 表「每小时茉莉」列（我的数据行，含加成系数） | **无** → 上限未知 |
| mteam | 魔力值 | API `POST /api/tracker/mybonus` 的 `data.formulaParams.finalBs` | **无** → 上限未知 |
| hddolby | — | DNS 解析失败（已知间歇故障） | — |

> ⚠️ **进度百分比只认 `alt="NN%"`**：各站 `style` 的单位不同
> （hdsky / ourbits / soulvoice 用 px，hhanclub 用 %），且 hhanclub 的
> `class` 后面还跟 Tailwind 工具类（`loadbaryellow !rounded-lg`）。
>
> ⚠️ **进度是站点自有口径**（通常相对**基础**时魔算），而我们存的是
> **合计**——hhanclub 就是 progress 37% 但合计 58.7。UI 上分开显示。

> ⚠️ **mteam 是 API 站，走的不是 `/mybonus.php`**：
> 它的积分商城页 `kp.m-team.cc/mybonus` 是**纯 SPA 壳**（HTML 里没有数据，
> "You need to enable JavaScript to run"），页面要的数据来自
> `POST /api/tracker/mybonus`（x-api-key 鉴权，**无需新凭据**）。
> 时魔取 `data.formulaParams.finalBs`（**合计**列，`rowSpan=6` 跨全部 6 个
> 奖励行）；同行的 `allBonus` 只是**基础**部分。字段语义来自站点自己的前端
> bundle（`static/js/user.*.chunk.js`）。
> ⚠️ `data.memberCount.bonus` 是**累计余额**（百万级），**不是**每小时收益。
> 该端点失败 → 抛 `SiteFetchError`（整个快照作废，保留上次数据），
> 降级成 `hourly_bonus=None` 会让 upsert 把上次的时魔值**覆盖成 NULL**。

> ⚠️ **端点失败必须抛错**（mybonus 端点失败 → 抛 `SiteFetchError`，整个快照作废、
> 保留上次数据）：降级成 `hourly_bonus=None` 看着无害，但 upsert 会把**上次的
> 时魔值覆盖成 NULL**——UI 上突然空白，且没有任何错误提示。

#### 时魔也用于 push_free 选种优先级

`push_free_torrents` 按**时魔封顶档**排候选（2026-10-22 加入）：

| 档 | 含义 | 站（prod 实测 2026-10-22） | 候选数 |
|---|---|---|---|
| 0 | 明确未封顶 | hdsky 11.7% / hhanclub 36.9% / hdhome 55.5% / soulvoice 94.8% / ourbits 95.4% | 589 |
| 1 | 无上限信号 | mteam / springsunday / hddolby | 338 |
| 2 | 已封顶 | audiences 110.2% | 853 |

依据是 NexusPHP 魔力公式 `每小时 = B0 × (1 − e^(−A/B0))`：A 越大越逼近上限，
**多推一个种子的边际收益越小**，已封顶的站再推也不涨时魔。所以推送名额
（`push_max_per_run`）优先给「离上限还远」的站——audiences 候选最多（853）却
被排到最后，首个候选落到第 928 位。

档内排序由运行时参数 **`push_within_bucket_order`** 决定（UI 的「调度 →
配置」弹框可改）：

- `0`（默认）= 各站时魔进度升序（同进度退化为 `last_seen_at DESC`）
- `1` = 整个档内随机打乱（含站点顺序）

> ⚠️ 时魔数据来自 `refresh_user_data`（30 分钟一次），最多滞后 30 分钟。
> 站点没有 `site_user_data` 行 → 落「无信号」档，行为与加此特性前一致。

fixture 由脚本抓取（裁剪 + 脱敏 + **裁剪后解析结果必须与整页一致**的自校验）：

```bash
uv run python scripts/capture_mybonus_fixtures.py --db <dev 库路径>
```

> ⚠️ 抓取脚本必须**先整页脱敏+裁剪再抽片段**：反过来（先抽片段再裁剪）会让
> 片段从标签中间截断，裁剪正则把半个标签当属性吃掉——hhanclub 的 headline
> 曾整段丢失（解析出 0 个时魔却不报错）。

## 8. 数据与存储

| 项 | 位置 |
|---|---|
| dev SQLite | `data-dev/medialab.db`（另有 1 个 `.bak.*`） |
| prod SQLite | 远端 `/mnt/user/appdata/medialab/data/` |
| 日志 | `logs-dev/` / 远端 `/mnt/user/appdata/medialab/logs/` |
| 下载目录 | `/data/torrents`（dev 挂载） |

- SQLite 默认走 **WAL**，但 dev / prod compose 都设了 `DB_JOURNAL_MODE=DELETE`
  （commit 后立即落盘，避免重启丢 info_hash；prod 于 `2f2bc08` 从 WAL 改为 DELETE）。
  部署后应确认 `-wal` / `-shm` 文件已清除。
- `user_version` 迁移为**批次制**，老库不会重复 +8h，重复执行幂等。
- 配置目录 `config/` 挂载进容器（`local.yaml` 属本地配置，已 gitignore）。
- **备份无自动轮转**（按决策不引入新机制）：备份文件靠人工清理。
  2026-10-21 已清理一轮 —— dev 只留 `medialab.db` + 1 个 `.bak.*`；
  prod 留 3 个（最新两次部署前 + `predeploy` 基线）。
  清理前务必逐个 `PRAGMA integrity_check` 确认，**损坏的副本没有抢救价值**
  （`.recover` 早已做过），可直接删。

## 9. 常用命令

```bash
# 依赖 + 静态检查（AGENTS.md 规定，静态检查不通过禁止合并）
uv sync --extra dev
uv run ruff check app/
uv run mypy app/
cd web && npx vue-tsc -b
cd web && npx vite build

# 前端测试（需先起 dev 容器 localhost:8092）
cd web && npx playwright test

# 部署（脚本只管 dev；prod 见下方「真实部署链路」）
./scripts/deploy.sh                 # dev：build + up + healthcheck（本机 8092）
./scripts/deploy.sh status          # 查状态
./scripts/deploy.sh logs            # 跟日志

# 远端操作
ssh mp "docker ps --filter name=medialab"
ssh mp "docker logs -f medialab --tail 100"
```

### 本机构建镜像给 prod 用

本机是 arm64，prod（128）是 x86_64，**不能直接 `docker save` 搬运**。需要跨架构时用 buildx：

```bash
docker buildx build --platform linux/amd64 -t ppw111/medialab:latest --push .
```

通常走 CI 即可：push 到 `main` → GitHub Actions 自动构建并推 Docker Hub → 128 上 `docker pull` 后重建。

### prod（128）的真实部署链路

CI **只构建推送镜像，不部署**（无 ssh/deploy job）。实际链路是手动三步：

```bash
# 1) 本机 push 触发 CI 构建（等 CI 绿）
git push origin main

# 2) 128 上拉新镜像
ssh mp "docker pull ppw111/medialab:latest"

# 3) 128 上重建容器（注意用 .128.yml 那个文件 + TAG=latest）
ssh mp "cd /mnt/user/appdata/medialab/app && TAG=latest docker compose -f docker-compose.prod.128.yml --env-file .env.prod up -d"
```

> ⚠️ **第 3 步必须带 `TAG=latest`**（2026-10-22 实测确认）。
> `docker-compose.prod.128.yml` 里写的是 `image: docker.io/ppw111/medialab:${TAG:-0.1.0}`，
> 而 `.env.prod` **没有** `TAG` 键 → compose 会解析成 `0.1.0`，
> 部署的是**旧镜像**（`0.1.0` 标签自 10 月起就没更新过），
> 容器却会显示"重建成功"，**极易误判成部署成功**。
> 现网跑的容器用的是 `latest`，所以显式传 `TAG=latest` 才与现状一致。
>
> ⚠️ **部署前先备份 DB**（手工，见 DEPLOY.md §8）：
> ```bash
> ssh mp "cd /mnt/user/appdata/medialab/data && sqlite3 medialab.db \".backup './medialab.db.bak.\$(date +%Y%m%d_%H%M%S)'\""
> ```
> 备份后用 `PRAGMA integrity_check;` 确认（损坏的副本没有抢救价值）。
>
> ⚠️ **CI 的镜像可见性校验**（2026-10-23 改）：原先有个
> "Set Docker Hub repo visibility to public" 步骤去 PATCH Docker Hub API，
> 但那个接口现在对 JWT 认证返回 **403**（连续 3 次 CI 全 403，从未成功过），
> 已删除。改成只读的 "Verify image is publicly pullable"——用**匿名** token
> 拉 manifest，200 = public 可拉（128 主机没有 Docker Hub 凭据，靠 public 拉）。
> 该步骤失败 = 镜像真拉不动，此时 workflow 会变红，是真问题。
>
> 判断镜像有没有推上去仍要看 Docker Hub 的 tag `last_updated`，
> 不要只看 workflow 的红绿灯。

> ⚠️ 128 上的 `/mnt/user/appdata/medialab/app/` **不是 git 仓库**，且代码副本已陈旧
> （仍是 10 月 14 日的版本）。容器真正跑的是 Docker Hub 镜像里的代码，
> 挂载到容器的只有 `config/` `data/` `logs/` 三个目录。
> **不要以为改 128 上的 `app/` 目录就能生效** —— 改 compose 文件需同步在 128 上落地。
>
> ⚠️ compose 文件改了也要同步到 128（它不是 git 仓库，不会自动更新）。

## 10. CI

两个 workflow，**有依赖关系**（2026-10-23 改）：

```
push main ──→ ci（ruff+mypy+pytest / vue-tsc+oxlint）──全绿──→ docker-image（构建+推送）
                  ↓ 红                                              ↑
              不构建不推送                                     产出 latest/main/sha-*
```

`.github/workflows/ci.yml`（闸门）

- 触发：`push` 到 `main` 或 `v*` tag；`pull_request` 也触发
- 闸门：backend（ruff + mypy + pytest）+ frontend（vue-tsc + types-placement + oxlint）
- 刻意不含 Playwright e2e：连的是真实下载器与真实站点，CI 上跑不了
- 不上传 coverage artifact（无 codecov 等消费者，纯堆积）

`.github/workflows/docker-image.yml`（构建推送）

- 触发：**`workflow_run`**（上游 `ci` 完成）——不是 push
  - 只有 `ci` **全绿**才构建推送；红了**不产出镜像**（闸门真成了闸门）
  - PR 的 ci 通过了也不构建：PR 代码不可信，不该在持有 Docker Hub 凭据的
    job 里被 checkout + 执行（**安全边界**）
- ⚠️ **`workflow_run` 的两个陷阱**（已处理，勿改回去）：
  1. `github.sha` / `github.ref` 指向**默认分支 HEAD**，不是被测 commit
     → 必须 `actions/checkout` 用 `ref: ${{ github.event.workflow_run.head_sha }}`
  2. `docker/metadata-action` 的 `context.sha` 同样是默认分支 HEAD，
     `context.ref` 永远是 `refs/heads/main` → `type=sha` / `type=semver`
     会产出**错误**的 tag。故**不用 metadata-action**，改为自算 tag
     （`Compute image tags` 步骤），OCI `revision` 标签也用被测 commit
- 产物 tag：`latest` + `main` + `sha-<short>`；命中 `v*` semver tag 时额外
  产出版本号 tag（`v1.2.3` → `1.2.3` 与 `1.2`）
- 产物：`docker.io/ppw111/medialab`（public，构建后校验匿名可拉）
- 基镜像：`python:3.12-slim`
- 缓存：`type=gha`
- 已硬化（`a82cd10`）：timeout、concurrency、`set -euo pipefail`、jq 安全构造 JWT、凭据不回显
