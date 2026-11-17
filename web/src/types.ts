/**
 * 前后端共享的 API 类型（单一数据源）。
 *
 * 背景：此前 20+ 个 `interface` 散落在各个 `.vue` 里，各写各的——
 * `SchedulerInfo` 在 `SchedulerTab.vue` 与 `Dashboard.vue` 各定义一份
 * （字段还不一样），`JobStep` 在 `JobStepList.vue` 里定义却在别处重复。
 * 后端 DTO 改字段时前端不会报错（类型是手写的，与后端无关联），
 * 直到运行时才发现字段没了。
 *
 * 本文件把**与后端 API 契约绑定**的类型集中到一处。后端改了字段，
 * 前端改这里一处即可；`vue-tsc` 会把用错的地方全报出来。
 *
 * 命名：后端是 snake_case（pydantic 默认），前端保持一致——
 * 不做 camelCase 转换，省一层映射 bug。
 */

// ==================================================================== 通用

/** 站点适配器展示信息（`GET /sites/adapters`）。 */
export interface AdapterInfo {
  name: string
  label: string
  domain: string
}

/**
 * 作业步骤内的一条**明细**（逐站 / 逐种 / 逐任务）。
 *
 * 步骤的 `detail` 是**聚合**描述（如「20 成功，0 失败」），光看它
 * 无法回答「是哪些出了问题」。明细把这份逐项信息还给用户。
 */
export interface JobStepItem {
  /** 主体标识（站名 / 种子名 / hash 前缀 / task id）。 */
  name: string
  status: 'ok' | 'warn' | 'skip' | 'fail'
  count: number | null
  /** 简要描述（如失败原因「DNS 解析失败」）。 */
  detail: string | null
}

/** 作业步骤（调度作业结果里的分步明细）。 */
export interface JobStep {
  name: string
  status: 'ok' | 'warn' | 'skip' | 'fail'
  count: number | null
  detail: string | null
  /**
   * 逐项明细。**默认折叠**——monitor 这类作业可能有几百条，
   * 默认展开会把详情面板撑爆。
   */
  items?: JobStepItem[] | null
  /**
   * 明细的**完整**条数。
   *
   * `items_total > items.length` 说明明细被截断了（后端限流），
   * 前端据此显示「显示 N / 共 M」。
   */
  items_total?: number | null
}

/** 一条作业执行日志（`result.logs` 的元素）。 */
export interface JobLogEntry {
  /** 北京时间字符串（`yyyy-MM-dd HH:mm:ss`）。 */
  ts: string
  /** 日志级别名（INFO / WARNING / ERROR …）。 */
  level: string
  /** 发出日志的 logger 名（如 `app.services.jobs`）。 */
  logger: string
  /** 日志正文（可能含多行异常堆栈）。 */
  message: string
}

/** 可调运行参数的元数据。 */
export interface ParamSpec {
  key: string
  label: string
  min: number
  max: number
  default: number
  /** 补充说明（如「不可恢复」这类风险提示）。无则不渲染。 */
  note?: string | null
  /**
   * 枚举取值 `[[值, 显示名], ...]`。
   *
   * 存在时前端渲染**单选按钮组**（0/1 这类「在几个具名行为间二选一」
   * 的参数用数字输入框反直觉，且看不出 0 和 1 各是什么）；
   * 不存在（null / undefined）时渲染数字输入框（连续数值参数）。
   */
  options?: [number, string][] | null
}

// ==================================================================== 站点

/** 站点配置（`GET /sites`）。 */
export interface Site {
  id: number
  name: string
  domain: string | null
  adapter: string
  enabled: boolean
  rss_url_masked: string
  has_apikey: boolean
  has_cookies: boolean
  created_at: string
  updated_at: string
  /** 是否有用户数据快照（site_user_data 有行）。false = 还没抓到 */
  has_data: boolean
  /** 上传量（字节） */
  upload: number
  /** 下载量（字节） */
  download: number
  /** 当前做种数 */
  seeding_count: number
  /** 时魔：每小时获得的站点积分（合计口径）；null = 该站没有 */
  hourly_bonus: number | null
  /** 时魔单位（站点自称：魔力值 / 爆米花 / 茉莉 / 憨豆 / 积分） */
  hourly_bonus_unit: string | null
  /** 是否已达该站的获取上限（null = 该站页面不提供上限信号） */
  hourly_bonus_capped: boolean | null
  /** 站点进度百分比（>=100 表示封顶；口径由站点自己定） */
  hourly_bonus_progress: number | null
  /** 用户数据最近一次刷新时间（北京时间）；null = 从未刷新 */
  user_data_seen_at: string | null
}

/** 站点连通性与窗口解析自检（`POST /sites/{id}/test`）。 */
export interface SiteSelfTest {
  reachable: boolean
  logged_in: boolean
  detail: string
  self_test: {
    free_type: string
    free_end_at: string | null
    free_source: string
    confidence: number
  }
}

/** 站点种子列表拉取测试（`GET /sites/{id}/feed-test`）。 */
export interface FeedTestResult {
  ok: boolean
  status: string
  item_count: number
  duration_ms: number
  sample_titles: string[]
  error: string
}

/** 单站测连结果（`POST /sites/test-all` 返回的元素）。 */
export interface SiteTestItem {
  site_id: number
  name: string
  reachable: boolean
  logged_in: boolean
  detail: string
}

/** 全部测连结果（`POST /sites/test-all`）。 */
export interface SiteTestAllResult {
  results: SiteTestItem[]
  /** ``unauthorized`` = 可达但**未登录**（凭据失效 / 没配 cookie）——与「不可达」分开计。 */
  summary: { total: number; reachable: number; failed: number; unauthorized?: number }
}

/** 站点用户数据摘要（`GET /sites/{id}/user-data`）。 */
export interface SiteUserDataSummary {
  site_id: number
  site_name: string
  has_data: boolean
  upload?: number
  download?: number
  seeding_count?: number
  /** 时魔：每小时获得的站点积分（合计口径）。 */
  hourly_bonus?: number | null
  /** 时魔单位（站点自称：魔力值 / 爆米花 / 茉莉 / 憨豆 / 积分）。 */
  hourly_bonus_unit?: string | null
  /** 是否已达该站的获取上限（null = 该站无法判断）。 */
  hourly_bonus_capped?: boolean | null
  /** 站点进度百分比（>=100 表示封顶）。 */
  hourly_bonus_progress?: number | null
  last_seen_at?: string | null
}

// ==================================================================== 站点用户数据

/** 所有站最近一次用户数据（`GET /user-data`）。 */
export interface UserData {
  site_id: number
  site_name: string
  adapter: string
  upload: number
  download: number
  seeding_count: number
  /** 时魔：每小时获得的站点积分（合计口径，含各项加成）。 */
  hourly_bonus: number | null
  /** 时魔单位（站点自称：魔力值 / 爆米花 / 茉莉 / 憨豆 / 积分）。 */
  hourly_bonus_unit: string | null
  /** 是否已达该站的获取上限（null = 该站无法判断）。 */
  hourly_bonus_capped: boolean | null
  /** 站点进度百分比（>=100 表示封顶）。 */
  hourly_bonus_progress: number | null
  last_seen_at: string | null
}

/** 用户数据趋势行（`GET /user-data/trends`）。

 *  注：当前后端只存最新一行（UNIQUE site_id），趋势端点是占位。
 */
export interface UserDataTrend {
  site_id: number
  site_name: string
  upload: number
  download: number
  seeding_count: number
  /** 时魔：每小时获得的站点积分（合计口径，含各项加成）。 */
  hourly_bonus: number | null
  /** 时魔单位（站点自称：魔力值 / 爆米花 / 茉莉 / 憨豆 / 积分）。 */
  hourly_bonus_unit: string | null
  /** 是否已达该站的获取上限（null = 该站无法判断）。 */
  hourly_bonus_capped: boolean | null
  /** 站点进度百分比（>=100 表示封顶）。 */
  hourly_bonus_progress: number | null
  last_seen_at: string | null
}

// ==================================================================== 下载器

/** 下载器配置（`GET /downloaders`）。 */
export interface Downloader {
  id: number
  name: string
  type: string
  url: string
  username: string | null
  has_password: boolean
  save_path: string | null
  is_default: boolean
  enabled: boolean
  verify_tls: boolean
  is_cross_seed: boolean
  created_at: string
  updated_at: string
}

/**
 * 单个下载器的任务数（`GET /downloaders/{id}/torrent-count`）。
 *
 * **不在列表接口里**——任务数要实时打下载器接口（辅种站 3000+ 种子，
 * 实测拉一次 7.6s），放列表里会把「加载配置」变成慢操作。
 */
export interface DownloaderTorrentCount {
  downloader_id: number
  /** 任务数；null = 取不到（下载器不可达 / 未启用） */
  count: number | null
}

/**
 * 任务状态元信息（`GET /downloaders/state-meta`）。
 *
 * **后端下发**——状态值与后端 `TorrentState` 枚举逐字一致，前端不再
 * 硬编码映射表（漏改会显示原始英文值）。
 */
export interface TorrentStateMeta {
  /** 状态值（camelCase，与后端枚举一致） */
  value: string
  /** 中文名 */
  label: string
  /** 标签颜色（Ant Design Vue tag color） */
  color: string
}

/** 测连结果（`POST /downloaders/{id}/test` 与 `/test-config`）。 */
export interface DownloaderTestResult {
  reachable: boolean
  detail: string
}

/** 下载器原生任务（`GET /downloaders/{id}/torrents`）。 */
export interface DownloaderTorrent {
  hash: string
  name: string
  state: string
  progress: number
  size: number
  downloaded: number
  uploaded: number
  dlspeed: number
  upspeed: number
  ratio: number
  save_path: string | null
  category: string | null
  tags: string[]
}

/** 原生任务列表响应。 */
export interface DownloaderTorrentList {
  total: number
  items: DownloaderTorrent[]
}

/** 批量暂停/删除结果。 */
export interface BatchOpResult {
  requested: number
  succeeded: number
  failed: number
  errors?: string[]
}

/** 辅种下载器（`GET /cross-seed-monitor/downloaders`）。 */
export interface CrossSeedDownloader {
  id: number
  name: string
  type: string
  url: string
  enabled: boolean
  is_cross_seed: boolean
}

/** 辅种统计行（快照里的分组明细）。 */
export interface CrossSeedStatsRow {
  name: string
  size_gb: number
  count: number
  /** 12 位截断的样本 hash（仅展示用，**不能**用于删除）。 */
  sample_hash: string
  /**
   * 该组的**完整 40 位** hash 列表（删除用）。
   *
   * 旧快照行没有这个字段 → 空数组 → 删除按钮置灰（提示重新抓取）。
   */
  hashes?: string[]
  /**
   * 该组内有加入时间的种子的 ``hash → added_at(unix 秒)`` 映射。
   *
   * disk_rotation 同档（count 相同）内**先删加入更早的**种子需要它。
   * 旧快照行没有这个字段 → 排序时全部降级（与加此特性前一致）。
   * 前端目前只透传不展示，留作未来排序/展示扩展用。
   */
  added_at?: Record<string, number>
}

/**
 * 次数分布的一行（分组 tab 的汇总表）。
 *
 * 把所有 (name, size) 分组按「被几个种子共享」聚合成一行：
 * - `count` = 出现次数（= 该组的种子数）
 * - `groups` = 有多少个分组是这个次数
 * - `torrents` = 这些分组一共涉及多少个种子
 * - `sizeBytes` = 这些分组的数据量之和（共享只算一次）
 */
export interface DistributionRow {
  count: number
  groups: number
  torrents: number
  sizeBytes: number
}

/** 辅种异常种子（快照里的异常明细）。 */
export interface CrossSeedAnomaly {
  id: number
  hash: string
  name: string
  size_gb: number
  status: number
  anomaly_type: string
  error_code: number
  error_string: string
  /** 抓取时该种子是否已处于暂停态（跳过重复暂停用）。 */
  paused: boolean
}

/** 单个下载目录的占用（快照里的空间分布明细）。 */
export interface CrossSeedDirUsage {
  /** 下载目录（下载器视角的路径）。 */
  dir: string
  /** 该目录下的种子数。 */
  count: number
  /** 声明占用字节数——**含共享重复计入**（见 `used_bytes` 的说明）。 */
  bytes: number
  /** 声明总大小字节数——同样含共享重复计入。 */
  total_bytes: number
}

/** 辅种快照头部（`GET /cross-seed-monitor/snapshots` 列表项）。 */
export interface CrossSeedSnapshotHeader {
  downloader_id: number
  downloader_name: string
  scanned_at: string
  total_torrents: number
  unique_count: number
  anomaly_count: number
  inflight_count: number
  /** 本次**实际**发出暂停指令的种子数（不含已暂停被跳过的）。 */
  paused_count: number
  /** 暂停失败的种子数。 */
  pause_failed_count: number
  /**
   * 磁盘剩余空间（字节）。null = 没取到。
   *
   * qBittorrent 只提供剩余空间（拿不到总量），Transmission 两者都有。
   */
  disk_free_bytes: number | null
  /** 磁盘总量（字节）。null = 该下载器不提供（qBittorrent）。 */
  disk_total_bytes: number | null
  /**
   * 种子声明占用字节数（各种子 `completed_bytes` 求和）。
   *
   * ⚠️ **不等于磁盘真实占用**：辅种的本质是**同一条数据被多个种子共享**
   * （实测辅种站 4646 个种子只对应 2237 份数据），共享部分被重复计了
   * N 次。它只是「辅种这一块声明了多少」，真实磁盘占用看
   * `disk_total_bytes - disk_free_bytes`。
   */
  used_bytes: number
  /** 种子总大小字节数（理论值）。同样含共享重复计入。 */
  total_size_bytes: number
}

/** 辅种快照详情（含明细；`GET .../snapshot` 与 `POST .../refresh`）。 */
export interface CrossSeedSnapshot extends CrossSeedSnapshotHeader {
  stats: CrossSeedStatsRow[]
  anomalies: CrossSeedAnomaly[]
  /** 按下载目录的占用分布（按占用降序）。 */
  by_dir: CrossSeedDirUsage[]
}

/** 一个孤儿条目（磁盘有、下载器里已无对应任务）。 */
export interface CrossSeedOrphan {
  /** 条目名（顶层目录名或文件名）。 */
  name: string
  /** 绝对路径。 */
  path: string
  /** 占用字节数（空目录为 0）。 */
  bytes: number
  /** 包含的文件数（0 = 空目录）。 */
  file_count: number
  /** 该条目属于哪个扫描目录（= 下载器的 downloadDir）。 */
  scan_dir: string
}

/** 单个扫描目录的统计。 */
export interface CrossSeedScannedDir {
  scan_dir: string
  total_count: number
  orphan_count: number
  orphan_bytes: number
  /** 空目录孤儿数（0 文件）——残留但不占空间。 */
  empty_dir_count: number
  /** 该目录下「有主」的顶层名数（下载器任务覆盖的条目）。 */
  owned_count: number
}

/** 单个辅种下载器的孤儿统计。 */
export interface CrossSeedOrphanStat {
  downloader_id: number
  downloader_name: string
  scanned_at: string
  orphan_count: number
  orphan_bytes: number
  total_scan_bytes: number
  total_scan_count: number
  /** 空目录孤儿数（0 文件）——残留但不占空间。 */
  empty_dir_count: number
  /** 逐目录的扫描统计。 */
  scanned_dirs: CrossSeedScannedDir[]
  orphans: CrossSeedOrphan[]
  /** 扫描失败的原因（null = 成功）。 */
  error: string | null
}

/** 辅种孤儿文件汇总（`GET /cross-seed-monitor/orphans`）。 */
export interface CrossSeedOrphanSummary {
  summary: {
    downloaders: number
    orphan_count: number
    orphan_bytes: number
    empty_dir_count: number
  }
  downloaders: CrossSeedOrphanStat[]
}

/** 孤儿删除的单条结果。 */
export interface CrossSeedOrphanDeleteItem {
  path: string
  /** deleted = 已删；skipped = 复核不通过跳过；failed = 删除失败。 */
  status: 'deleted' | 'skipped' | 'failed'
  bytes?: number
  reason?: string
  /**
   * 失败是否因**权限不足**（NAS 端属主非挂载账号、权限位不含写）。
   * true = 重试无意义，需在 NAS 端改属主/权限或换挂载账号。
   * 仅 status=failed 时有意义。
   */
  permission_denied?: boolean
}

/** 孤儿手动扫描结果（`POST /cross-seed-monitor/orphans/scan`）。 */
export interface CrossSeedOrphanScanResult {
  /** 一句话摘要（作业的 summary）。 */
  summary: string
  orphan_count: number
  orphan_bytes: number
  /** 失败的下载器（点名）。 */
  errors: string[]
  /** 扫描后有孤儿表的新鲜统计。 */
  orphans: CrossSeedOrphanSummary
}

/** 孤儿删除结果（`POST /cross-seed-monitor/orphans/delete`）。 */
export interface CrossSeedOrphanDeleteResult {
  requested: number
  succeeded: number
  failed: number
  /** 复核不通过而跳过的数量（期间已不再是无主条目）。 */
  skipped: number
  freed_bytes: number
  items: CrossSeedOrphanDeleteItem[]
  error: string | null
  /** 因权限不足（NAS 端属主/权限）失败的条目数——重试无意义。 */
  permission_denied: number
  /** 判定不完整的警告（部分下载器拉取失败）。 */
  warning?: string
  /** 删除后重扫失败的原因（null = 重扫成功）。 */
  rescan_error?: string | null
  /**
   * 删除后的重扫是否**仍在后台进行**（重扫耗时超过 15s 上限）。
   *
   * 为 true 时响应里的 `orphans` 是**重扫前**的旧统计——稍后刷新页面即可。
   */
  rescan_pending?: boolean
  /**
   * 删除后**重扫**得到的最新孤儿统计。
   *
   * 孤儿统计表是每天一次的作业快照，不重扫页面会一直显示旧数字
   * （已删的还在列表里、且看不到新产生的孤儿）。
   */
  orphans?: CrossSeedOrphanSummary
}

/** 辅种空间汇总（`GET /cross-seed-monitor/space`）。 */
export interface CrossSeedSpace {
  summary: {
    downloaders: number
    used_bytes: number
    total_size_bytes: number
    disk_free_bytes: number | null
    disk_total_bytes: number | null
  }
  downloaders: Array<{
    downloader_id: number
    downloader_name: string
    scanned_at: string
    used_bytes: number
    total_size_bytes: number
    disk_free_bytes: number | null
    disk_total_bytes: number | null
  }>
}

/** 辅种快照列表（`GET /cross-seed-monitor/snapshots`）。 */
export interface CrossSeedSnapshotList {
  snapshots: CrossSeedSnapshotHeader[]
  /** 是辅种但还没抓过快照的下载器——前端显示「等待首次抓取」。 */
  pending: CrossSeedDownloader[]
}

// ==================================================================== 拉取索引

/** 单条拉取索引条目。 */
export interface TorrentIndexItem {
  id: number
  site_id: number
  site_name: string
  item_id: string | null
  name: string
  size: number
  category: string | null
  free_type: string
  free_end_at: string | null
  free_source: string | null
  free_confidence: number | null
  discount_pct: number | null
  pushable: boolean
  push_status: string | null
  pushed_at: string | null
  info_hash: string | null
  published_at: string | null
  first_seen_at: string
  last_seen_at: string
}

/** 拉取索引分页（`GET /torrent-index`）。 */
export interface TorrentIndexPage {
  total: number
  items: TorrentIndexItem[]
}

/** 拉取索引统计（`GET /torrent-index/stats`）。 */
export interface TorrentIndexStats {
  total: number
  by_type: Record<string, number>
}

/** 站点下拉项（`GET /torrent-index/sites`）。 */
export interface SiteOption {
  id: number
  name: string
}

/** 手动推送结果（`POST /torrent-index/{id}/push`）。 */
export interface PushResponse {
  ok: boolean
  detail: string
  info_hash: string | null
  downloader_id: number | null
}

/**
 * 批量推送的单条结果（`POST /torrent-index/push-batch`）。
 *
 * state：`enqueued`（已入队）/ `skipped_pushed`（已推过）/
 * `skipped_queued`（队列里已有）/ `skipped_no_link`（无可用链接）/
 * `skipped_not_found`（索引不存在）/ `skipped_site_missing`（站点不存在）/
 * `skipped_site_disabled`（站点已停用）/ `failed`。
 */
export interface BatchPushItem {
  id: number
  state: string
  detail: string
}

/**
 * 批量推送汇总（`POST /torrent-index/push-batch`）。
 *
 * **只入队不同步执行**——执行由队列（drain_task_queue）按节流消化，
 * 执行后列表里的「推送状态」才会变成已推送/失败。
 */
export interface BatchPushResponse {
  total: number
  enqueued: number
  skipped: number
  failed: number
  items: BatchPushItem[]
}

// ==================================================================== 调度

/** 单个作业（`GET /scheduler` 里的 jobs 数组）。 */
export interface SchedulerJob {
  name: string
  cron: string
  enabled: boolean
  running: boolean
  /**
   * 中文名（页面标题）——**后端下发**（`JobSpec.title`）。
   *
   * 前端不再硬编码 job → 中文名的映射表：每加一个作业就要手改前端，
   * 漏一处就在调度页显示原始 job 名（如 `scan_orphans`）。
   */
  title?: string
  /** 一句话说明（页面标题下方）——后端下发（`JobSpec.description`）。 */
  description?: string
}

/** 调度中心状态（`GET /scheduler`）。 */
export interface SchedulerInfo {
  enabled: boolean
  jobs: SchedulerJob[]
  inflight: number
  accepting_new_work: boolean
}

/** 手动触发作业的结果（`POST /scheduler/{name}/run`）。 */
export interface RunResult {
  ok: boolean
  started_at: string
  finished_at: string
  duration_ms: number
  error: string
  result?: Record<string, unknown> | null
}

/**
 * 作业执行结果（作业函数返回的 dict）。
 *
 * 后端 `JobRun.result` 是 `Any`（不同作业返回不同结构），但前端只用
 * 几个通用字段：`summary`（人话摘要）、`steps`（分步明细）、
 * `logs`（执行日志，仅详情端点返回）。
 * 其余字段用索引签名兜住。
 */
export interface JobRunResult {
  summary?: string
  steps?: JobStep[]
  /**
   * 作业执行期间的 Python 日志（时序视角：哪步慢、中间发生了什么）。
   *
   * **列表端点不返回**（`include_logs` 默认 false）——日志让单条结果
   * 达 10–30KB，列表一次 100 条会到 MB 级。前端选中行时按需拉详情。
   */
  logs?: JobLogEntry[]
  [key: string]: unknown
}

/** 单条作业执行历史（`GET /scheduler/runs` 的 items 元素）。 */
export interface JobRunItem {
  id: number
  job_name: string
  trigger: string
  started_at: string
  finished_at: string | null
  duration_ms: number | null
  ok: boolean
  error: string | null
  result: JobRunResult | null
}

/** 作业历史分页。 */
export interface JobRunPage {
  total: number
  items: JobRunItem[]
}

/** 作业专属配置（`GET /scheduler/{name}/config`）。 */
export interface JobConfig {
  job_name: string
  cron: string
  enabled: boolean
  params: Record<string, number>
  specs: ParamSpec[]
}

/** 运行时参数查询/修改的响应体。 */
export interface RuntimeParamsView {
  params: Record<string, number>
  specs: ParamSpec[]
}

/** 环境变量配置项（`GET/PUT /env-settings`）。
 *
 * 与 `ParamSpec`（运行参数）平行但独立——本类型服务「字符串型」配置
 * （辅种扫描目录 / 未来其它 env→DB 字符串设置），不归属作业。
 */
export interface EnvSettingItem {
  key: string
  label: string
  /** 补充说明（前端渲染在字段下方）。 */
  note: string
  /** textarea 的 placeholder。 */
  placeholder: string
  /** 值的最大长度（前端 maxlength + 后端校验双保险）。 */
  max_length: number
  /** 当前值。 */
  value: string
}

/** 环境变量配置响应。 */
export interface EnvSettingsView {
  items: EnvSettingItem[]
}

// ==================================================================== 仪表盘

/** 仪表盘总览（`GET /dashboard`）。 */
export interface DashboardSnap {
  sites: { total: number; enabled: number }
  downloaders: { total: number; enabled: number }
  free_ending_soon: number | null
  free_index_total: number | null
  inflight_torrents: number | null
  /**
   * 在途数据的抓取时刻（辅种部分来自定时快照，非实时）。
   *
   * null = 还没有辅种快照（辅种下载器未被标记或尚未抓过）。
   */
  inflight_snapshot_at: string | null
}

// ==================================================================== 媒体库域
//
// 以下类型来自 strmmanger（媒体库域）。与辅种域的类型**零重名**——
// ``Driver``（存储源）与 ``Downloader``（下载器）是不同的东西。

// ---- 存储源 ----

export interface Driver {
  id: number
  type: string
  name: string
  device_type: string | null
  enabled: boolean
  /** 凭据掩码（不回传明文） */
  credential_masked: string

  // -- 媒体库配置 --
  /** 115 媒体库 cid（字符串） */
  media_cid: string | null
  /** 媒体库路径（只读展示） */
  media_path: string | null
  /**
   * 媒体库目录 id 的**展示名**（115 叫 cid、光鸭叫 fileId）。
   * 后端按驱动类型给出——前端不写 `if type === "guangya"`。
   */
  media_cid_label: string
  /** 本地媒体库目录 */
  output_dir: string | null
  /** 视频后缀（逗号分隔，扫描依据——生成 strm；strm 文件名会去掉该后缀） */
  video_exts: string
  /** 媒体图片后缀（逗号分隔，附件同步到本地；空 = 不同步） */
  image_exts: string
  /** 媒体数据后缀（逗号分隔，附件同步到本地；空 = 不同步） */
  data_exts: string
  /** 最小视频体积（MB），0 = 不过滤 */
  min_size_mb: number
  /** 是否包含子目录 */
  include_subdirs: boolean
  /** 扫描时是否自动清理「网盘上已消失」的条目（prune） */
  auto_prune: boolean
  last_scan_at: string | null
  last_synced_count: number
  /**
   * 所属聚合组（`null` = 独立存储源，不参与聚合）。
   * 一个聚合组 = 1 个主数据源 + N 个副数据源。
   */
  aggregate_group_id: number | null
  /**
   * 组内角色（`master` / `secondary`；`null` = 不参与聚合）。
   * 副数据源是**缓冲**——内容不对外展示，归拢到主盘后自动清理副本。
   */
  role: string | null
  /**
   * 测连状态（启动自动测连 / 手动测连的缓存）。
   * `null` = 尚未测连。
   */
  conn: DriverConnStatus | null
}

/** 聚合组（1 主 + N 副）。 */
export interface AggregateGroup {
  id: number
  name: string
  enabled: boolean
  created_at: string
  updated_at: string
  /** 归拢目标目录 id（主盘上的目录；null = 未配置） */
  target_dir_id: string | null
  /** 归拢目标目录路径（只读展示） */
  target_dir_path: string | null
}

/** 一个副盘的缓冲根（组 ↔ 副盘 的映射项）。 */
export interface AggregateGroupRoot {
  driver_id: number
  root_id: string
  /** 缓冲根路径（只读展示） */
  root_path: string | null
}

/**
 * 一个聚合组的**完整目录映射**。
 *
 * 两端都是**组级配置**——「从哪些副盘的哪些目录，搬到主盘的哪个目录」
 * 是一条组与组之间的映射关系，不借存储源的媒体库配置。
 */
export interface AggregateGroupMapping {
  group_id: number
  /** 主盘上的归拢目标目录（null = 未配置） */
  target_dir_id: string | null
  target_dir_path: string | null
  /** 各副盘的缓冲根 */
  roots: AggregateGroupRoot[]
}

/** 聚合组水位（台账统计 + 主盘云添加额度）。 */
export interface AggregateGroupStats {
  group_id: number
  group_name: string
  /** 主数据源 id（`null` = 组还没配主盘） */
  master_driver_id: number | null
  /** 副数据源 id 列表 */
  secondary_driver_ids: number[]
  /** 各状态的条数（pending / syncing / verified / done / failed / paused） */
  counts: Record<string, number>
  /** 待归拢字节数 */
  pending_bytes: number
}

/** 手动归拢任务的运行状态（后台跑，前端轮询）。 */
export interface AggregateSyncJob {
  id: string
  group_id: number
  group_name: string
  /** running / success / failed / cancelled */
  state: string
  started_at: number
  finished_at: number | null
  elapsed_sec: number
  synced: number
  cleaned: number
  paused: number
  failed: number
  skipped: number
  details: string[]
  logs: string[]
  error: string | null
}

/** 在途台账一行（副盘 → 主盘的待归拢文件）。 */
export interface StagingRow {
  id: number
  group_id: number
  source_driver_id: number
  target_driver_id: number
  remote_id: string
  file_name: string
  source_dir: string
  target_dir: string
  size_bytes: number
  /** pending / syncing / verified / done / failed / paused */
  state: string
  /**
   * 搬运通道（`cloud_download` / `share_transfer`；null = 还没搬）。
   * `share_transfer` = 分享转存（服务端内部复制，**不消耗云添加额度**）。
   */
  transfer_mode: string | null
  attempts: number
  last_error: string | null
  created_at: string
  updated_at: string
  synced_at: string | null
  cleaned_at: string | null
}

/** 存储源测连缓存（后端进程内缓存，含最近测连时间）。 */
export interface DriverConnStatus {
  logged_in: boolean
  status: string
  message: string
  account_id: string
  total_bytes: number
  used_bytes: number
  remain_bytes: number
  can_get_link: boolean
  /** 最近测连时间（北京时间字符串） */
  checked_at: string | null
}

/** 115 设备类型（后端是单一真源，前端不手写）。 */
export interface DeviceType {
  value: string
  label: string
  /** 能否用于扫码登录（实测：18 个里只有 10 个能取到二维码） */
  qr_capable: boolean
  note: string
}

export interface DriverInput {
  type: string
  name: string
  device_type?: string | null
}

/** 修改存储源（只传要改的字段）。 */
export interface DriverUpdate {
  name?: string | null
  device_type?: string | null
}

/** 媒体库配置（每个存储源一套）。 */
export interface DriverConfig {
  /** 115 媒体库 cid（字符串） */
  media_cid: string
  /** 媒体库路径（只读展示，后端按 cid 自动解析） */
  media_path: string
  /** 本地媒体库目录 */
  output_dir: string
  /** 视频后缀（数组形式，提交时转逗号分隔） */
  video_exts: string[]
  /** 媒体图片后缀（当前忽略） */
  image_exts: string[]
  /** 媒体数据后缀（当前忽略） */
  data_exts: string[]
  /** 最小视频体积（MB），0 = 不过滤 */
  min_size_mb: number
  /** 是否包含子目录 */
  include_subdirs: boolean
  /** 扫描时是否自动清理「网盘上已消失」的条目（prune） */
  auto_prune: boolean
}

/** 扫描统计。 */
export interface ScanStats {
  created: number
  updated: number
  unchanged: number
  attachment_synced: number
  attachment_unchanged: number
  attachment_failed: number
  skipped: number
  skipped_unknown: number
  skipped_small: number
  /** 同名不同扩展名的去重数（保留大的，小的不落库） */
  deduped: number
  /** prune 清理的「源端已消失」条目数（与「未知后缀」是两回事） */
  pruned: number
  failed: number
  total: number
  errors: string[]
  /** 去重明细（点名两个源文件 + 谁胜出）——去重不是错误，单独一条通道 */
  dedup_details?: string[]
}

/** 增量同步任务（115 生活事件流）。 */
export interface IncrementalJob {
  id: string
  driver_id: number
  driver_name: string
  /** running / success / failed / cancelled */
  state: string
  started_at: number
  finished_at: number | null
  elapsed_sec: number
  current_path: string
  logs: string[]
  error: string | null
  /** 拉到的原始事件数 */
  events_fetched: number
  /** 落在媒体库内的事件数 */
  events_in_scope: number
  created: number
  updated: number
  unchanged: number
  deleted: number
  attachment_synced: number
  attachment_unchanged: number
  attachment_failed: number
  /** 路径冲突（疑似目录改名）——已跳过 */
  conflicts: number
  /** 同名不同扩展名的去重数（保留大的，小的不落库） */
  deduped: number
  /** 去重明细（点名两个源文件 + 谁胜出）——去重不是错误，单独一条通道 */
  dedup_details?: string[]
  skipped_unknown: number
  skipped_deleted: number
  failed: number
  cursor_old: number
  cursor_new: number
  total: number
}

/** 扫描任务（后台任务 + 轮询进度）。 */
export interface ScanJob {
  id: string
  driver_id: number
  driver_name: string
  /** running / success / failed / cancelled */
  state: string
  started_at: number
  finished_at: number | null
  elapsed_sec: number
  /** 当前正在处理的文件路径 */
  current_path: string
  /** 已处理的文件数（含跳过） */
  processed: number
  dirs: number
  created: number
  updated: number
  unchanged: number
  attachment_synced: number
  attachment_unchanged: number
  attachment_failed: number
  skipped_unknown: number
  skipped_small: number
  /** 同名不同扩展名的去重数（保留大的，小的不落库） */
  deduped: number
  /** prune 清理的「源端已消失」条目数（与「未知后缀」是两回事） */
  pruned: number
  failed: number
  /** 去重明细（点名两个源文件 + 谁胜出）——去重不是错误，单独一条通道 */
  dedup_details?: string[]
  /** 实时日志（环形，最多 500 条） */
  logs: string[]
  error: string | null
}

// ---- 数据同步任务（本地目录 → 网盘）----

/** 上传任务。 */
export interface UploadTask {
  id: number
  name: string
  /** 目标存储源（网盘） */
  driver_id: number
  /** 目标目录 id（网盘侧） */
  target_file_id: string
  /** 目标目录路径（只读展示） */
  target_path: string | null
  /** 监听目录（容器内绝对路径，完整路径） */
  watch_root: string
  include_subdirs: boolean
  enabled: boolean
  /** 同步成功后清理本地文件（**彻底删除 unlink，不可恢复**；空目录一并删） */
  delete_after_upload: boolean
  concurrency: number
  /**
   * 同名且大小不同时的冲突策略：
   * - `0` 本地优先：删掉云端旧的，传本地的
   * - `1` **保留大的**（默认）：本地更小则跳过上传，保住云端那份
   */
  conflict_policy: number
  created_at: string
  updated_at: string
  /** 运行态：监听器是否在跑 */
  listening: boolean
  /** 当前正在上传的文件（大文件通道） */
  current: string
  /** 当前正在上传的文件（小文件通道） */
  current_small: string
  total_uploaded: number
  total_failed: number
  total_bytes: number
  /** 真正被丢弃的文件数（队列与溢出暂存都满时；>0 需关注日志） */
  dropped: number
  /** 运行态的最近一次错误（监听启动失败等） */
  runtime_error: string | null
}

/**
 * 本环境**支持上传的存储源类型**（后端声明）。
 *
 * 前端靠它过滤「目标网盘」下拉——加新网盘时前端零改动。
 */
export interface UploadCapabilities {
  uploadable_types: string[]
}

/** 上传任务的运行态（轮询用）。 */
export interface UploadRuntime {
  task_id: number
  listening: boolean
  /** 当前正在上传的文件（大文件通道） */
  current: string
  /** 当前正在上传的文件（小文件通道） */
  current_small: string
  total_uploaded: number
  total_failed: number
  total_bytes: number
  /** 大文件队列待处理数 */
  queue_large: number
  /** 小文件队列待处理数 */
  queue_small: number
  /** 溢出暂存量（队列满时暂存、空闲自动回灌——不丢） */
  overflow: number
  /** 真正被丢弃的文件数（队列与溢出暂存都满时） */
  dropped: number
  logs: string[]
  error: string | null
  syncing: boolean
  /** 监听器统计 */
  watch: {
    events?: number
    dispatched?: number
    filtered?: number
    rejected?: number
    last_event_at?: number
  }
}

/** 立即全量同步的结果。 */
export interface SyncNowResult {
  uploaded: number
  overwritten: number
  unchanged: number
  failed: number
  conflicts: number
  /** 本地删除的文件数（delete_after_upload 开启时；删除在后台异步执行） */
  deleted_local: number
  uploaded_bytes: number
  /** 因「事件队列正在处理」被全量同步跳过的文件数（防同文件双传） */
  skipped_inflight?: number
}

/** 存储源账号状态（测连结果）。 */
export interface DriverStatus {
  /** 凭据是否有效 */
  logged_in: boolean
  /**
   * 状态：
   * - `ok` —— 一切正常
   * - `needs_verify` —— 登录态正常，但换直链被 115 账号安全验证挡住
   * - `no_credential` —— 尚未扫码登录
   * - `error` —— 其它问题
   */
  status: string
  /** 可读说明（直接显示给用户） */
  message: string
  /** 账号 id（UID） */
  account_id: string
  /** 总容量（字节） */
  total_bytes: number
  /** 已用容量（字节） */
  used_bytes: number
  /** 剩余容量（字节） */
  remain_bytes: number
  /** 能否换到直链（最关键的一项） */
  can_get_link: boolean
  /**
   * 本次测连时间（北京时间字符串）。
   *
   * 后端缓存里带的这份是**探测发生的时刻**——面板显示「正常」时，
   * 用户需要知道这是多久前的状态（缓存只在进程启动时刷新一次）。
   */
  checked_at?: string | null
}

export interface QrStartResult {
  session_id: string
  qr_image_b64: string
}

export type QrStatusKind = 'waiting' | 'scanned' | 'confirmed' | 'expired' | 'error'

export interface QrStatusResult {
  kind: QrStatusKind
  error_kind: string | null
  error_msg: string | null
  confirmed: boolean
}

/** 发送短信验证码的结果。 */
export interface SmsSendResult {
  captcha_token: string
  /** 需要人机验证时给出 URL */
  captcha_url: string
}

/** 校验短信验证码的结果。 */
export interface SmsVerifyResult {
  ok: boolean
  message: string
}

// ---- 媒体库 ----

export interface MediaItem {
  id: number
  driver_id: number
  remote_id: string
  path: string
  name: string
  size: number
  strm_path: string | null
  /** 当前中转 URL */
  strm_url: string
}

export interface MediaList {
  total: number
  items: MediaItem[]
}

export interface MediaStats {
  total: number
  total_size: number
  per_driver: { driver_id: number; count: number }[]
  server_time: number
}

// ---- 健康检查 ----

export interface HealthReport {
  checked_at: number
  healthy: boolean
  stale_count: number
  missing_files_count: number
  orphan_files_count: number
  invalid_signature_count: number
  stale: number[]
  missing_files: number[]
  orphan_files: string[]
  invalid_signature: number[]
  // 配置缺失导致签名无法校验（不是「签名失效」，别据此批量重签）
  configuration_error: string | null
}

// ---- 作业 ----

// ---- 通用 ----

export interface CacheStats {
  l1_size: number
  l1_capacity: number
  hits: number
  misses: number
  hit_rate: number
  in_flight: number
}

export interface ReadyResponse {
  status: string
  database: boolean
  cache: CacheStats | null
}

// ---- 迁移（115 → 光鸭盘） ----

/** 目录浏览项（115 用 cid，光鸭用 fileId）。 */
export interface DirEntry {
  id: string
  name: string
}

/** 迁移失败 / 冲突明细。 */
export interface MigrationJobItem {
  name: string
  path: string
  size_gb?: number
  reason?: string
  local_gb?: number
  remote_gb?: number
}

/** 一次迁移任务的进度快照。 */
export interface MigrationJob {
  id: string
  state: "running" | "success" | "failed" | "cancelled" | "paused"
  started_at: number
  finished_at: number | null
  elapsed_sec: number
  params: {
    source_driver_id: number
    source_cid: string
    source_path: string
    target_driver_id: number
    target_file_id: string
    target_path: string
    min_size_mb: number
    max_size_mb: number
    /** 两次提交给光鸭之间的最小间隔（秒） */
    submit_interval_sec: number
    exclude_disc: boolean
    /** 搬完清理源：迁移成功后删除源端文件（默认关；仅光鸭源支持） */
    delete_source?: boolean
  }
  source_path: string
  current_path: string
  dirs_walked: number
  discovered: number
  in_flight: number
  /** 在途任务：taskId → { 完整路径, 文件名, 大小 }（不带进度——光鸭该字段不可靠） */
  in_flight_items: Record<string, { path: string; name: string; size: number }>
  success: number
  failed: number
  skipped_existing: number
  skipped_inflight: number
  conflicts: number
  filtered_small: number
  filtered_large: number
  filtered_ext: number
  migrated_bytes: number
  /** 已清理的源文件数（「搬完清理源」开启时） */
  deleted_source?: number
  legacy_inflight: number
  failed_items: MigrationJobItem[]
  conflict_items: MigrationJobItem[]
  logs: string[]
  error: string | null
}

/** 开跑参数。 */
export interface MigrationRunInput {
  source_driver_id: number
  source_cid: string
  source_path: string
  target_driver_id: number
  target_file_id: string
  target_path: string
  min_size_mb: number
  max_size_mb: number
  submit_interval_sec: number
  exclude_disc: boolean
  /** 搬完清理源：迁移成功后删除源端文件（默认关；仅光鸭源支持） */
  delete_source?: boolean
}

// ---- WebDAV ----

/** WebDAV 账号。 */
export interface DavUser {
  id: number
  username: string
  is_active: boolean
  /** 上次登录时间（北京时间字符串），null = 从未登录 */
  last_login_at: string | null
  created_at: string
}

/** 新建账号。 */
export interface DavUserInput {
  username: string
  password: string
}

/** 修改账号（全部可选）。 */
export interface DavUserUpdate {
  password?: string
  is_active?: boolean
}

/** 审计记录。 */
export interface DavAuditItem {
  id: number
  user_id: number
  username: string
  method: string
  path: string
  status: number
  client: string
  remote_addr: string
  duration_ms: number
  item_count: number
  created_at: string
}

/** 审计查询结果（分页）。 */
export interface DavAuditPage {
  items: DavAuditItem[]
  total: number
}

/** 可选作为 DAV 根的存储源。 */
export interface DavRootDriver {
  id: number
  name: string
  output_dir: string | null
  /** 能否作为 DAV 根（有 output_dir 且目录存在） */
  usable: boolean
}

/** DAV 根配置。 */
export interface DavRootConfig {
  /** 当前指向的存储源 id；null = 自动（第一个可用） */
  driver_id: number | null
  drivers: DavRootDriver[]
}

/** 刮削配置（GET/PUT /api/scrape/config）。 */
export interface ScrapeConfig {
  scrape_enabled: number
  scrape_policy: number
  /** TMDB API Key 是否已配置（**永不回显明文**） */
  has_key: boolean
  media_library_dir: string
  organize_enabled: number
  organize_action: number
  organize_source_dirs: string
  /** 整理与刮削的开启条件是否齐备（前端据此显示「怎么开」） */
  ready: boolean
}

/** 刮削配置更新请求（部分更新；key 留空 = 不修改）。 */
export interface ScrapeConfigInput {
  scrape_enabled?: number
  scrape_policy?: number
  tmdb_api_key?: string
  media_library_dir?: string
  organize_enabled?: number
  organize_action?: number
  organize_source_dirs?: string
}

/** 单条刮削记录（GET /api/scrape/records）。 */
export interface ScrapeRecord {
  scope_key: string
  kind: string
  status: string
  media_type: string | null
  tmdb_id: number | null
  title: string | null
  year: number | null
  season: number | null
  episode: number | null
  nfo_path: string | null
  error: string | null
  scraped_at: string | null
}

/** 手动刮削结果（POST /api/scrape/file）。 */
export interface ScrapeFileResult {
  status: string
  detail: string
  /** 是否已记入识别别名表 */
  remembered: boolean
}

/** 识别别名（标题 → TMDB ID 的固定映射）。 */
export interface ScrapeAlias {
  id: number
  /** 原始标题（展示用） */
  alias_text: string
  /** 归一化标题（唯一键） */
  alias_key: string
  tmdb_id: number
  media_type: string | null
  note: string | null
  updated_at: string
}
