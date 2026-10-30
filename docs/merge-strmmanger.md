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
| 前端 | 设计令牌 + Playwright e2e + 类型放置检查 | 无 e2e | pt 更成熟 |
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
| C2 | 登录端点 | **两套都保留**：`/api/v1/auth/*`（辅种）+ `/api/auth/*`（媒体库），共用同一实现 |
| C3 | session 签名 payload | 统一为点分隔（`nonce.expires`）——旧 cookie 失效，重新登录一次 |
| C4 | `app/services/factory.py` | strm 的改名 `svc_factory.py`（pt 的适配器工厂保留原名） |
| C5 | `app/core/auth.py` | 取 strm 版（含明文自动升级）；Bearer 支持 **API Token + session 值**两种口径 |
| C6 | 前端 `/settings` | pt 保留（调度 + 环境变量）；strm 的签名密钥页 → `/media-settings` |
| C7 | SPA 静态托管 | 统一到 `main._mount_frontend`（带 hash 缓存 + 真 404） |

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
| 0 | 以 ptHelper 为基线建仓，引入 strm 代码 | 保留双方 git 历史（`strm-upstream` remote） |
| 1 | 后端合并：DB 模型 20 表、配置、日志、认证、main lifespan | 1521 测试通过 |
| 2 | 补齐分层：API 层下沉到 service 层 | `test_layering.py` 全绿 |
| 3 | 前端合并：单一 SPA，两组菜单 | `vue-tsc` + `vite build` 通过 |
| 4 | 部署合并：Dockerfile / compose / CI / 数据迁移脚本 | compose 校验通过 |

## 数据迁移

两个旧库 → 一个新库，用 SQLite `ATTACH` + **按列名映射**搬表。

```bash
# 1. 备份（务必先做）
cp strmmanger.db strmmanger.db.bak
cp seedhelper.db  seedhelper.db.bak

# 2. 建目标库（让应用自己建表结构——结构以代码为准）
#    见下方「建目标库」

# 3. 搬数据
python scripts/migrate_db.py \
    --strm /path/to/strmmanger.db \
    --seed /path/to/seedhelper.db \
    --out   /path/to/medialab.db
```

### ⚠️ 为什么必须按列名映射

源库与目标库的**列顺序可能不同**：

```sql
-- 源库（strmmanger / ptHelper 时期）
CREATE TABLE sites (
    ..., created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
    breaker_until VARCHAR(32), ...
);

-- 目标库（合并后，create_all 建的）
CREATE TABLE sites (
    ..., breaker_until VARCHAR(32),
    created_at VARCHAR(32) NOT NULL, updated_at VARCHAR(32) NOT NULL, ...
);
```

`INSERT INTO ... SELECT *` 按**位置**对应——`updated_at` 会收到
`breaker_until` 的值，直接触发 NOT NULL 约束失败（或更糟：静默错位）。

脚本因此显式列出**两库共有的列**（按目标库顺序）。

### 实测结果

两个真实旧库 → **16 张表 / 27588 行**：

```
媒体库域：drivers 2 / media_files 7859 / link_cache 8 / settings 1
           dav_users 3 / dav_audit_log 24
辅种域：  sites 9 / downloader_instances 3 / torrent_index 14619
         task_queue 930 / app_runtime_params 8 / app_env_settings 1
         job_runs 4109 / scheduler_job_settings 2 / cross_seed_orphan 1
         site_user_data 9
```

迁移后 ORM 读取字段全部正确，两个域的端点（含**两套登录端点**）HTTP 全通。

## 生产切换（可回滚）

```bash
# 1. 停旧容器（**不删**）
docker stop strmmanger seedhelper-prod

# 2. 备份两个 DB
cp strmmanger.db strmmanger.db.bak-$(date +%s)
cp seedhelper.db  seedhelper.db.bak-$(date +%s)

# 3. 建目标库 + 搬数据
#    （见「数据迁移」）

# 4. 起新容器
docker compose -f docker-compose.prod.128.yml --env-file .env.prod up -d

# 5. 验收清单
curl -fsS http://192.168.0.128:8099/healthz
curl -fsS http://192.168.0.128:8099/readyz
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
| **DB 写锁竞争** | strm 扫描（长写事务）+ pt 调度作业 + `/302` 缓存写 → `database is locked` | 两边生产都是 `DB_JOURNAL_MODE=DELETE`（写事务独占整库）。**建议**：`link_cache` 写失败只记日志，不让 302 失败（缓存是派生数据，不该是正确性依赖） |
| **记忆体** | 单进程要装下两个域 | compose limits 提到 1G（dev）/ 1536M（prod），上线后实测 RSS |
| **覆盖率** | 合并后代码量翻倍 | `fail_under` 重新定档，随补测试逐步抬高（只许升不许降） |
| **旧 cookie 失效** | 用户需重新登录一次 | 刻意为之——两边的 cookie 签名算法不同，无法兼容解析 |
