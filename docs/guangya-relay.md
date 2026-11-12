# 光鸭跨账号转存（A 分享 → B 保存）

> 2026-11-12 生产实测 + 实现留档。
> 代码：`app/services/cloud_relay.py`（引擎）、`app/adapters/drivers/guangya.py`（协议）、
> `app/services/relay_jobs.py`（任务）、`app/api/routes/relays.py`（API）。
> 真实验证：`scripts/probe_guangya_relay.py`。

## 为什么走「分享 + 保存」而不是「云添加」

| 路径 | 实测结果 | 结论 |
|---|---|---|
| **分享 + 保存**（本方案） | 100MB **7 秒落盘**（≈14 MB/s），文件名与层级完整保留 | ✅ 主力 |
| 直链 + 云添加 | 快，但**丢文件名**（变成 `download`）+ **每天 3 次/账号**上限 + 失败也扣额度 | ❌ 不适合批量 |
| 本地中转 | 吃本机上行（~3.2 MB/s）+ 等于源大小的临时磁盘 | ❌ 不考虑 |

「云添加」的额度是硬墙：三个光鸭账号在同一天全部撞到
「云添加次数超限，下个自然日刷新」——**每天只能搬 3 个文件**。
（`app/services/migration.py` 的 115→光鸭 迁移走的正是云添加，故它同样受此限制。）

## 协议

```
① 源账号  share_create(fileIds=[...], title, validateDuration=0,
                       shareType=0, downloadType=1, maxRestoreCount=0, code="")
        → { shareId, code, shareUrl }

② 匿名    get_share_access_token { shareId: "{shareId}_{uid}", code }
        → { data: { accessToken } }        ← uid = access_token 的 JWT sub

③ 匿名    get_share_page_files_list { accessToken, parentId:"", page, pageSize, ... }
        → 分享条目（fileId 与源账号 fileId 同一 id 空间）

④ 目标账号 restore_share { accessToken, fileIds, parentId }   ← 必须带 Bearer
        → { data: { taskId } }              ← 异步，7s 内落盘

⑤ 源账号  share_delete([shareId])         ← 收尾，别在源账号留分享记录
```

### 三个必须记住的坑

1. **分享链路必须匿名**（②③ 不带 `Authorization`）。
   带 Bearer 一律返回 `201 分享已失效` —— 那正是分享页给未登录访客走的那条路。
   故驱动另开一条 httpx 通道（`GuangyaDriver._call_public`），
   **不能**走 `guangyaclient` 的客户端（它默认头带 `authorization`）。
2. **保存必须带 Bearer**（④）。匿名 → `117 无效token`。
3. **`shareId` 必须拼账号 id 后缀**：`{shareId}_{uid}`。
   缺后缀 → `112 参数错误`（不是 201，别当限流处理）。

### 成功判定

**成功响应没有 `code` 字段**（`{"msg":"success","data":{...}}`）。
用 `code in (None, 0)` 判成功，别写 `code == 0`。

### 错误码

| 码 | 含义 | 可重试 | 映射 kind |
|---|---|---|---|
| `201` | 分享已失效 | ✅ 等一会儿再建 | `rate_limit` |
| `112` | 参数错误 | ❌ 调用方拼错了 | `config` |
| `117` | 无效 token | 认证问题 | `auth` |
| `159` | 同名目录已存在 | ✅ 幂等（`data.fileId` 仍是目标目录） | — |
| `354` | 限额（如上传流量） | ❌ | `unknown` |

## 实测数据（2026-11-12 生产）

| 事项 | 结果 |
|---|---|
| 100MB 文件落盘 | **7 秒**（服务端内部复制，≈14 MB/s） |
| 目录结构 | 完整递归保留（父 → 子 → 文件） |
| 文件名 | 完整保留（`.mkv` / `.nfo` 原样） |
| 单次保存上限 | **500 个文件**（服务端限制） |
| 建分享频率限制 | 同账号连建会被判 `201`；间隔 90s 以上稳定 |
| 空目录分享 | 失效（`201`）——分享内容必须非空 |
| 同名保存行为 | **不报错、不覆盖**，自动改名 `xxx(1)` ⚠️ |
| 云添加额度 | **3 次/账号/天**，失败也扣 |
| 光鸭上传流量 | 另有每日上限（撞到过「上传流量超限」） |

## 实现要点

### 提交单元 = 同一源目录下的文件批次

- 一个源目录下的文件**一次分享、一次保存**（≤500/批）
- 目标侧逐层 `ensure_dir` 建出对应目录（幂等——同名目录复用，不会建出 `xxx(1)`）
- 为什么不逐文件各建一次分享：1000 个文件 = 1000 次建分享，
  而建分享本身有频率限制（耗时以小时计）。**冲突判定仍然是逐文件的**。

### 冲突判定（逐文件）

```
目标目录 list_entries → {文件名: (大小, fileId)}
  同名 + 同大小 → 跳过（skipped_existing）
  同名 + 不同大小 → 记冲突（conflicts），不覆盖、不改名
  不存在 → 纳入本批
```

⚠️ **预检是必须的**：服务端不覆盖同名文件而是自动改名 `xxx(1)`，
不预检的话重跑一次就多一份垃圾副本，反复重跑会堆满目标目录。

### 「搬完清理源」是显式开关（默认关）

- 只删**已校验成功**的文件（落盘查不到的一律不删——否则数据没了）
- 走 `fs_delete`（进回收站，非永久删除）
- **记审计日志**（删数据不可逆，必须能回答「删了什么」）

### 节流与退避

- `_ShareGate`：两次建分享之间至少间隔 N 秒（默认 30s，面板可调）
- `201` → 自动退避重试（间隔翻倍，上限 5 分钟，最多 5 次）
- `112` → **不重试**（调用方拼错参数，重试只是浪费时间）

## 验证

```bash
# 生产容器跑真实账号的端到端验证（造临时数据 → 真转存 → 校验 → 自删）
docker exec -i medialab /opt/venv/bin/python - < scripts/probe_guangya_relay.py
```

覆盖：转存成功 / 文件名与层级保留 / 同名不同大小记冲突且不覆盖 /
不产生 `(1)` 副本 / 默认不删源 / 清理源开关生效 / 分享用完即删。

⚠️ **mock 单测不算验证**。光鸭适配层改动后必须用真实账号跑一遍上面这个脚本
（`tests/unit/test_guangya_share.py`、`tests/unit/test_cloud_relay.py`、
`tests/integration/test_relay_api.py` 只是 CI 回归网）。
