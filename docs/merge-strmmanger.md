# 合并 strmmanger 的方案与实施记录

> 2026-10-30。两个独立服务、两个独立数据库、两个独立面板 → **一个应用、
> 一个 SQLite、一个 SPA**。

## 为什么合并

两个服务解决的是**同一条链路上的两段**：

```
PT 站 ──► 下载器 ──► 本地文件 ──► 网盘 ──► STRM ──► 播放器
└──── 辅种域（ptHelper）────┘          └── 媒体库域（strmmanger）──┘
```

分开跑时：两套 Docker 镜像、两个面板、两份 `.env`、两套备份、两套监控。
且中间那一段（本地产物 → 网盘）是**断的**——strm 的「迁移」与 pt 的
「辅种」各做一半。

## 选 ptHelper 作基线

| 维度 | ptHelper | strmmanger | 判定 |
|---|---|---|---|
| 分层约束 | `app/domain/` 独立层 + `test_layering.py` **硬约束** | 无 domain 层，api 层直接碰 DB | **基线该选严的** |
| 错误处理 | `ServiceError`（服务层不 import fastapi） | 路由里 `try/except` 样板 | pt 更干净 |
| 日志 | 敏感字段自动脱敏 | 无脱敏 | pt 更安全 |
| 时间口径 | 统一北京时间字符串 | Integer unix 时间戳 | pt 更一致 |
| 前端 | 设计令牌 + Playwright e2e + 类型放置检查 | 无 e2e | pt 更成熟（注：合并后 e2e 已整体移除，现仅保留设计令牌 + 类型/样式/CSS 静态检查） |
| 死代码检测 | `test_dead_code.py` | 无 | pt 更严谨 |

**代价**：strm 的 API 层代码需要下沉到 service 层（见下文「返工范围」）。

## 兼容性前置结论

### 天然不冲突（零改动合并）

| 项 | strmmanger | ptHelper |
|---|---|---|
| 路由前缀 | `/api/*` | `/api/v1/*` |
| DB 表名 | 8 张 | 12 张（**零重名**） |
| 外键目标 | `drivers.id` | `sites.id` / `downloader_instances.id`（各自闭环） |
| `app/adapters/` 子目录 | `drivers/` `dav/` | `sites/` `downloaders/` |
| `app/domain/` | 无 | 有 |

### 必须处理的冲突

| # | 冲突 | 处理 |
|---|---|---|
| C1 | session cookie 名 | 统一为 `medialab_session` |
| C2 | 登录端点 | **统一到 `/api/v1/auth/*`**——删除 strm 的 `/api/auth/*`（含 `/me`）。登录态查询统一用 `/api/v1/auth/status` |
| C3 | session 签名 payload | 统一为点分隔（`nonce.expires`）——旧 cookie 失效，重新登录一次 |
| C4 | `app/services/factory.py` | strm 的改名 `svc_factory.py`（pt 的适配器工厂保留原名） |
| C5 | `app/core/auth.py` | 取 strm 版（含明文自动升级）；Bearer 支持 **API Token + session 值**两种口径（session 值口径仅为面板/CLI 同源便利，**非长期凭据**——CLI 长期使用应存 `WEB_TOKEN`，session cookie 值 24h 过期） |
| C6 | 前端 `/settings` | pt 保留（调度 + 环境变量）；strm 的签名密钥页 → `/media-settings` |
| C7 | SPA 静态托管 | 统一到 `main._mount_frontend`（带 hash 缓存 + 真 404） |

### 认证收敛：取更严的契约

合并前两边各有认证实现，行为有 4 处差异。**统一时取更严的那一边**
（改实现，不改测试）：

| 项 | strmmanger | ptHelper | 采用 | 理由 |
|---|---|---|---|---|
| `SameSite` | `strict` | `lax` | **strict** | 挡 CSRF。面板同源部署 + 全 fetch，无 form 提交，Strict 不会误伤 |
| 空密码 | 401 | 422 | **401** | 「密码错误」不是「参数格式错」——前端该显示"用户名或密码错误" |
| 多余字段 | 422（`extra="forbid"`） | 200 | **422** | 防 typo 字段名静默失效（前端写成 `passwd` 却"登录成功"） |
| 响应体 | `{ok, username}` | `{status, username}` | **`{ok, username}`** | 布尔语义更直白；前端只读 `detail`（错误路径）与 `authenticated`（status 端点），不受影响 |

## 返工范围（选 ptHelper 基线的唯一代价）

ptHelper 的 `tests/unit/test_layering.py` 是**硬约束测试**，会直接判失败。
实测 strm 的违规：

| 违规 | 文件数 | 处理 |
|---|---|---|
| api 层直接用 `session_scope` | 7 | 下沉到 service 层 |
| api 层 import `app.db` / `app.adapters` | 8 | 下沉到 service 层 |
| adapters 层 import `app.db` | 1（`dav/paths.py`） | `app/adapters/dav/` → `app/webdav/`（**移出 adapters 层**） |
| adapters 层 import `app.services` | 1（`dav/guard.py`） | 同上——`app/webdav` 是 ASGI 接入层，不是云盘适配器 |

**新增的 service 层模块**：

- `app/services/driver_auth.py` —— 扫码 / 短信登录（原在路由层）
- `app/services/dav_users.py` —— DAV 账号 CRUD + 审计查询 + 根配置

**移层**：`app/adapters/drivers/device_types.py` → `app/domain/device_types.py`
（纯数据，属领域层；API 层要用其 `DeviceTypeLiteral` 做校验）。

## 实施记录

| Phase | 内容 | 结果 |
|---|---|---|
| 0 | 以 ptHelper 为基线建仓，引入 strm 代码 | 保留双方 git 历史（`strm-upstream` remote，2026-11-02 随旧仓库一并清理，历史已并入本仓） |
| 1 | 后端合并：DB 模型 20 表、配置、日志、认证、main lifespan | 1521 测试通过 |
| 2 | 补齐分层：API 层下沉到 service 层 | `test_layering.py` 全绿 |
| 3 | 前端合并：单一 SPA，两组菜单 | `vue-tsc` + `vite build` 通过 |
| 4 | 部署合并：Dockerfile / compose / CI / 数据迁移脚本 | compose 校验通过 |

## 数据迁移

**2026-10-30 修订：原 `scripts/migrate_db.py` 已删除**——合并后的时间口径
统一工程（见下方「时间口径统一」）让存量数据迁移失去意义：媒体库域 8 张
表的 16 个时间列从 `Integer`（unix 秒）改为 `String(32)`（北京时间字符串），
按列名映射搬表无法处理这种类型转换，且 `0 = 从未` 的语义要改为 `NULL`。

**后续做全新部署**：`create_all` 直接按新模型建最终态表结构，数据从零
开始。dev 库若有过旧结构（datetime / unix 秒时间列），删掉 dev 库重建
即可——`migrate_datetime_to_bj_str` 迁移函数已同步删除（见下文）。

### 历史记录（保留备查）

合并初期曾用 SQLite `ATTACH` + 按列名映射搬表，把两个旧库（`strmmanger.db`
+ `seedhelper.db`）合成 `medialab.db`。两个真实旧库 → 16 张表 / 27588 行，
迁移后 ORM 读取字段全部正确。该脚本与那次迁移的实测结果仅供历史参考，
不再可用（列类型已变）。

## 时间口径统一（2026-10-30）

合并时保留了两域时间列口径差异（辅种域 `String(32)` 北京时间字符串、
媒体库域 `Integer` unix 秒），2026-10-30 统一为北京时间字符串。

**统一范围**：媒体库域 8 张表的 16 个「DB 记录时刻」类列：

| 表 | 列 |
|---|---|
| `drivers` | `created_at` / `updated_at` / `last_scan_at` |
| `media_files` | `created_at` / `updated_at` / `mtime` |
| `link_cache` | `created_at` |
| `settings` | `updated_at` |
| `dav_users` | `last_login_at` / `created_at` / `updated_at` |
| `dav_audit_log` | `created_at` |
| `upload_tasks` | `created_at` / `updated_at` |
| `probe_results` | `probed_at` |

**保留 unix 秒的列**（协议侧时刻，与 `time.time()` 直接比较）：

- `link_cache.expires_at` —— L2 缓存过期时刻
- `cross_seed_snapshot.stats[].added_at` —— 下载器入队时刻（JSON 内）
- 进程内 token 有效期（非 DB 列）

**语义变更**：`0 = 从未` → `NULL`（`last_*_at` 列 nullable）；代码里
判断「有过」用 `is not None`，不用 `> 0`。

**`mtime` 处理**：网盘 adapter 返回的 `FileEntry.mtime` 仍是 unix 秒
（协议契约），service 层消费时用 `format_bj(datetime.fromtimestamp(ts, UTC))`
转换写入。增量扫描比较从 int 大小比较变为字符串字典序比较——`%Y-%m-%d
%H:%M:%S` 格式保证字典序与时序一致（回归测试见
`tests/unit/test_timeutils.py::test_bj_string_lexicographic_order_matches_chronology`）。

**迁移层处理**：原 `migrate_datetime_to_bj_str`（把历史 DateTime(UTC) 列
+8h 转为北京时间字符串）已删除——新模型 DB 时间列直接声明为 `String(32)`，
`create_all` 建表即为最终态，无需运行时迁移。`app/db/migrations.py` 的
`__all__` 与 `app/db/bootstrap.py` 的 `create_all` 同步移除调用。

**契约由 `tests/integration/test_db_models.py` + `tests/unit/test_timeutils.py` 固化**。

## 生产切换（可回滚）

> **状态（2026-11-02）**：切换已完成并稳定运行。旧环境**已全部清理**——
> 旧容器（`strmmanger` / `seedhelper-prod`）、旧镜像、旧 appdata 目录
> （`/mnt/user/appdata/seedhelper` / `strmmanger`）均已删除，
> **下面的回滚路径已不再可用**。此处保留为历史实施记录。

```bash
# 1. 停旧容器（**不删**）
docker stop strmmanger seedhelper-prod

# 2. 备份两个 DB
cp strmmanger.db strmmanger.db.bak-$(date +%s)
cp seedhelper.db  seedhelper.db.bak-$(date +%s)

# 3. 建目标库 + 搬数据
#    （见「数据迁移」）

# 4. 起新容器
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d

# 5. 验收清单
#    （端口 8092：2026-11-03 从 8099 迁回——seedhelper 移除后 8092 空闲，
#      prod 与 dev 统一用 8092）
curl -fsS http://<生产机IP>:8092/healthz
curl -fsS http://<生产机IP>:8092/readyz
#    面板：媒体库域（存储源/媒体库/上传/健康/DAV/签名密钥）
#          辅种域（站点/下载器/任务/拉取索引/用户数据/辅种统计/调度）
#    播放一个 .strm → 302 正常
#    Infuse 扫 WebDAV
#    辅种作业跑一轮（调度页看 job_runs）

# 6. 观察 24h。旧容器保留 → 出问题直接切回
docker start strmmanger && docker start seedhelper-prod
```

## 已知风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| **DB 写锁竞争** | strm 扫描（长写事务）+ pt 调度作业 + `/302` 缓存写 → `database is locked` | 真正的写安全靠「短事务 + 不在事务内做网络 IO」的约定（见 AGENTS.md 与 `app/services/jobs.py`），非 `link_cache` 降级。`SingleWriterQueue` 已删除（单进程单 worker 下不需要）。`link_cache` 写失败只记日志、不让 302 失败（缓存是派生数据，不该是正确性依赖）——这是补充措施，非主要手段 |
| **记忆体** | 单进程要装下两个域 | compose limits 提到 1G（dev）/ 1536M（prod），上线后实测 RSS |
| **覆盖率** | 合并后代码量翻倍 | `fail_under` 重新定档，随补测试逐步抬高（只许升不许降） |
| **旧 cookie 失效** | 用户需重新登录一次 | 刻意为之——两边的 cookie 签名算法不同，无法兼容解析 |
