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

/** 辅种统计行。 */
export interface CrossSeedStatsRow {
  name: string
  size_gb: number
  count: number
  sample_hash: string
}

/** 辅种扫描结果（`GET /cross-seed-monitor/downloaders/{id}/scan`）。 */
export interface CrossSeedScanResult {
  downloader_id: number
  downloader_name: string
  scanned_at: string
  total_torrents: number
  anomaly_count: number
  unique_count: number
  stats: CrossSeedStatsRow[]
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
}
