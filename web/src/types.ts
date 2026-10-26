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

/** 作业步骤（调度作业结果里的分步明细）。 */
export interface JobStep {
  name: string
  status: 'ok' | 'warn' | 'skip' | 'fail'
  count: number | null
  detail: string | null
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

/** 站点用户数据摘要（`GET /sites/{id}/user-data`）。 */
export interface SiteUserDataSummary {
  site_id: number
  site_name: string
  has_data: boolean
  upload?: number
  download?: number
  seeding_count?: number
  leeching_count?: number
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
  leeching_count: number
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
  leeching_count: number
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
  /** 判定不完整的警告（部分下载器拉取失败）。 */
  warning?: string
  /** 删除后重扫失败的原因（null = 重扫成功）。 */
  rescan_error?: string | null
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

// ==================================================================== 调度

/** 单个作业（`GET /scheduler` 里的 jobs 数组）。 */
export interface SchedulerJob {
  name: string
  cron: string
  enabled: boolean
  running: boolean
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
 * 两个通用字段：`summary`（人话摘要）与 `steps`（分步明细）。
 * 其余字段用索引签名兜住。
 */
export interface JobRunResult {
  summary?: string
  steps?: JobStep[]
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
