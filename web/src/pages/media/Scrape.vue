<template>
  <div class="page">
    <PageHeader
      eyebrow="媒体库 · MEDIA"
      title="整理与刮削"
      sub="下载完成的种子 → 识别 → 标准媒体库结构 + NFO / 图片"
      compact
    />

    <!--
      配置区：**始终可见**（与离线下载页同一模式）。

      配置存 DB（``app_env_settings`` / ``app_runtime_params``），改完立即
      生效——作业每轮执行前读最新值，不用重启容器。
    -->
    <a-card size="small" class="tip">
      <template #title>
        配置
        <a-tag :color="config?.ready ? 'green' : 'orange'" class="cfg-tag">
          {{ config?.ready ? "已就绪" : "未就绪" }}
        </a-tag>
      </template>
      <template #extra>
        <a-button
          size="small"
          type="primary"
          ghost
          :loading="saving"
          @click="saveConfig"
        >
          保存配置
        </a-button>
      </template>

      <a-alert v-if="config && !config.ready" type="warning" show-icon class="mb">
        <template #message>配齐下面几项即可启用</template>
        <template #description>
          配置保存在数据库里，<b>改完立即生效</b>——不用改 <code>.env</code>、
          不用重启容器。需要的条件：
          <b>① TMDB API Key</b>（themoviedb.org 免费申请）、
          <b>② 本地媒体库目录</b>（容器内绝对路径）、
          <b>③ 下载器与 tag 过滤</b>（圈定要整理的种子，避免误动辅种库）。
        </template>
      </a-alert>

      <a-row :gutter="[12, 8]" :wrap="true">
        <a-col :xs="24" :md="8">
          <div class="param">
            <div class="param-label">
              ① TMDB API Key
              <a-tag v-if="config?.has_key" color="green" class="mini">已配置</a-tag>
            </div>
            <a-input-password
              v-model:value="form.tmdbApiKey"
              :placeholder="config?.has_key ? '已配置（留空 = 不修改）' : 'TMDB → Settings → API'"
            />
          </div>
        </a-col>
        <a-col :xs="24" :md="8">
          <div class="param">
            <div class="param-label">② 本地媒体库目录</div>
            <a-input
              v-model:value="form.mediaLibraryDir"
              placeholder="/media/library"
            />
          </div>
        </a-col>
        <a-col :xs="24" :md="8">
          <div class="param">
            <div class="param-label">
              ③ 整理源目录
              <a-tooltip title="要整理的视频来源目录（逗号分隔多个）。文件从哪来无所谓——放进源目录就会被扫描、识别、整理到媒体库。建议与辅种下载目录物理分开">
                <QuestionCircleOutlined class="param-help" />
              </a-tooltip>
            </div>
            <a-input
              v-model:value="form.sourceDirs"
              placeholder="/downloads/media,/data/incoming"
            />
          </div>
        </a-col>
      </a-row>

      <a-row :gutter="[12, 8]" :wrap="true" style="margin-top: 8px">
        <a-col :xs="24" :md="6">
          <div class="param">
            <div class="param-label">刮削开关</div>
            <a-radio-group v-model:value="form.scrapeEnabled">
              <a-radio-button :value="0">关闭</a-radio-button>
              <a-radio-button :value="1">开启</a-radio-button>
            </a-radio-group>
          </div>
        </a-col>
        <a-col :xs="24" :md="6">
          <div class="param">
            <div class="param-label">产物已存在时</div>
            <a-radio-group v-model:value="form.scrapePolicy">
              <a-radio-button :value="0">仅补缺</a-radio-button>
              <a-radio-button :value="1">覆盖</a-radio-button>
            </a-radio-group>
          </div>
        </a-col>
        <a-col :xs="24" :md="6">
          <div class="param">
            <div class="param-label">整理开关</div>
            <a-radio-group v-model:value="form.organizeEnabled">
              <a-radio-button :value="0">关闭</a-radio-button>
              <a-radio-button :value="1">开启</a-radio-button>
            </a-radio-group>
          </div>
        </a-col>
        <a-col :xs="24" :md="6">
          <div class="param">
            <div class="param-label">整理动作</div>
            <a-radio-group v-model:value="form.organizeAction">
              <a-radio-button :value="0">复制（源保留）</a-radio-button>
              <a-radio-button :value="1">移动（删源文件）</a-radio-button>
            </a-radio-group>
          </div>
        </a-col>
      </a-row>

      <a-alert type="warning" show-icon class="mb" style="margin-top: 8px">
        <template #message>
          移动会删掉下载器目录里的源文件——做种中的任务会变成 missingFiles，
          只对<b>非辅种</b>下载器使用。
        </template>
      </a-alert>
    </a-card>

    <a-card size="small" title="手动刮削" class="tip">
      <a-row :gutter="[12, 8]" :wrap="true">
        <a-col :xs="24" :md="14">
          <a-input
            v-model:value="scrapePath"
            placeholder="视频文件绝对路径（如 /media/library/Movie Name (2025)/Movie.mkv）"
          />
        </a-col>
        <a-col :xs="24" :md="4">
          <a-input-number
            v-model:value="scrapeTmdbId"
            placeholder="TMDB ID"
            style="width: 100%"
            :min="1"
          />
        </a-col>
        <a-col :xs="24" :md="3">
          <a-button
            type="primary"
            ghost
            :loading="scraping"
            :disabled="!scrapePath.trim()"
            @click="scrapeFile"
          >
            刮削（覆盖）
          </a-button>
        </a-col>
        <a-col :xs="24" :md="3">
          <a-checkbox
            v-model:checked="scrapeRemember"
            :disabled="!scrapeTmdbId"
          >
            记住此修正
          </a-checkbox>
        </a-col>
      </a-row>
      <div class="muted" style="margin-top: 8px">
        走<b>覆盖</b>策略——手动点按钮通常是想重刮。识别错了可填 TMDB ID 强制指定；
        勾选「记住此修正」会把该标题与 TMDB ID 存进<b>识别别名</b>，下次同标题直接命中
        （不再靠搜索猜）。
      </div>
    </a-card>

    <a-card size="small" title="识别别名" class="tip">
      <template #extra>
        <a-button size="small" @click="loadAliases">刷新</a-button>
      </template>

      <a-alert type="info" show-icon class="mb">
        <template #message>
          别名在识别时<b>优先于搜索</b>——固化一次，永久生效。
          适用于：TMDB 的中文译名很机械、或自动识别选错了剧。
        </template>
      </a-alert>

      <a-row :gutter="[12, 8]" :wrap="true">
        <a-col :xs="24" :md="8">
          <div class="param">
            <div class="param-label">标题</div>
            <a-input v-model:value="aliasForm.aliasText" placeholder="文件里解析出的标题" />
          </div>
        </a-col>
        <a-col :xs="24" :md="4">
          <div class="param">
            <div class="param-label">TMDB ID</div>
            <a-input-number
              v-model:value="aliasForm.tmdbId"
              style="width: 100%"
              :min="1"
              placeholder="如 288328"
            />
          </div>
        </a-col>
        <a-col :xs="24" :md="4">
          <div class="param">
            <div class="param-label">类型</div>
            <a-select
              v-model:value="aliasForm.mediaType"
              :options="[
                { value: '', label: '不限' },
                { value: 'movie', label: '电影' },
                { value: 'tv', label: '剧集' },
              ]"
            />
          </div>
        </a-col>
        <a-col :xs="24" :md="5">
          <div class="param">
            <div class="param-label">备注</div>
            <a-input v-model:value="aliasForm.note" placeholder="为什么改" />
          </div>
        </a-col>
        <a-col :xs="24" :md="3">
          <div class="param">
            <a-button
              type="primary"
              ghost
              :loading="savingAlias"
              :disabled="!aliasForm.aliasText.trim() || !aliasForm.tmdbId"
              @click="saveAlias"
            >
              保存别名
            </a-button>
          </div>
        </a-col>
      </a-row>

      <a-table
        v-if="aliases.length"
        style="margin-top: 12px"
        size="small"
        :columns="aliasColumns"
        :data-source="aliases"
        :pagination="false"
        row-key="id"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'action'">
            <a-popconfirm
              title="删除这条别名？"
              ok-text="删除"
              cancel-text="取消"
              @confirm="removeAlias(record.id)"
            >
              <a-button size="small" danger type="link">删除</a-button>
            </a-popconfirm>
          </template>
        </template>
      </a-table>
      <div v-else class="muted" style="margin-top: 12px">
        还没有别名。自动识别选错剧时，在上面手动刮削并勾选「记住此修正」即可。
      </div>
    </a-card>

    <a-card size="small" title="最近刮削记录">
      <DataTable
        :columns="recordColumns"
        :data="records"
        :loading="loadingRecords"
        :pagination="{ pageSize: 15, total: recordTotal, current: page, onChange }"
        row-key="scope_key"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'action'">
            <a-button size="small" type="link" @click="openFix(record)">修正</a-button>
          </template>
        </template>
      </DataTable>
      <template #extra>
        <a-button size="small" @click="loadRecords">刷新</a-button>
      </template>
    </a-card>

    <a-modal
      v-model:open="fixOpen"
      title="修正识别结果"
      ok-text="重刮"
      cancel-text="取消"
      :confirm-loading="fixing"
      @ok="submitFix"
    >
      <a-alert type="info" show-icon style="margin-bottom: 12px">
        <template #message>
          识别错了？填上正确的 TMDB ID 重刮。勾选「记住修正」后，
          这个标题下次会**直接命中**该条目，不再靠搜索猜。
        </template>
      </a-alert>
      <div class="param-label">当前记录</div>
      <p class="muted" style="margin-bottom: 12px">
        {{ fixRecord?.title }} {{ fixRecord?.season ? `S${fixRecord.season}` : ""
        }}{{ fixRecord?.episode ? `E${fixRecord.episode}` : "" }}
        （当前 tmdb={{ fixRecord?.tmdb_id ?? "—" }}）
      </p>
      <div class="param-label">正确的 TMDB ID</div>
      <a-input-number
        v-model:value="fixTmdbId"
        style="width: 100%"
        :min="1"
        placeholder="留空 = 按原识别重刮"
      />
      <a-checkbox v-model:checked="fixRemember" style="margin-top: 12px">
        记住此修正（写入识别别名）
      </a-checkbox>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { message } from "ant-design-vue";
import { QuestionCircleOutlined } from "@ant-design/icons-vue";
import DataTable from "../../components/DataTable.vue";
import PageHeader from "../../components/PageHeader.vue";
import { mediaApi, errMsg } from "../../api";
import type {
  ScrapeAlias,
  ScrapeConfig,
  ScrapeConfigInput,
  ScrapeFileResult,
  ScrapeRecord,
} from "../../types";

const config = ref<ScrapeConfig | null>(null);
const saving = ref(false);

const form = reactive({
  tmdbApiKey: "",
  mediaLibraryDir: "",
  sourceDirs: "",
  scrapeEnabled: 0,
  scrapePolicy: 0,
  organizeEnabled: 0,
  organizeAction: 0,
});

// ---- 配置加载与保存 ----

async function loadConfig(): Promise<void> {
  config.value = await mediaApi.get<ScrapeConfig>("/scrape/config");
  form.mediaLibraryDir = config.value.media_library_dir;
  form.sourceDirs = config.value.organize_source_dirs;
  form.scrapeEnabled = config.value.scrape_enabled;
  form.scrapePolicy = config.value.scrape_policy;
  form.organizeEnabled = config.value.organize_enabled;
  form.organizeAction = config.value.organize_action;
}

async function saveConfig(): Promise<void> {
  saving.value = true;
  try {
    const payload: ScrapeConfigInput = {
      scrape_enabled: form.scrapeEnabled,
      scrape_policy: form.scrapePolicy,
      media_library_dir: form.mediaLibraryDir.trim(),
      organize_enabled: form.organizeEnabled,
      organize_action: form.organizeAction,
      organize_source_dirs: form.sourceDirs.trim(),
    };
    // 密钥：留空 = 不修改（与离线下载同一语义）
    if (form.tmdbApiKey) payload.tmdb_api_key = form.tmdbApiKey.trim();
    config.value = await mediaApi.put<ScrapeConfig>("/scrape/config", payload);
    form.tmdbApiKey = "";
    message.success("已保存，下一轮作业生效");
  } catch (e) {
    message.error(errMsg(e, "保存失败"));
  } finally {
    saving.value = false;
  }
}

// ---- 手动刮削 ----

const scrapePath = ref("");
const scrapeTmdbId = ref<number | undefined>(undefined);
const scrapeRemember = ref(false);
const scraping = ref(false);

async function scrapeFile(): Promise<void> {
  scraping.value = true;
  try {
    const result = await mediaApi.post<ScrapeFileResult>(
      "/scrape/file",
      {
        path: scrapePath.value.trim(),
        ...(scrapeTmdbId.value ? { tmdb_id: scrapeTmdbId.value } : {}),
        ...(scrapeRemember.value ? { remember: true } : {}),
      },
    );
    if (result.status === "ok") message.success(result.detail);
    else message.warning(result.detail);
    if (result.remembered) {
      message.info("已记入识别别名——下次同标题直接命中");
      await loadAliases();
    }
    await loadRecords();
  } catch (e) {
    message.error(errMsg(e, "刮削失败"));
  } finally {
    scraping.value = false;
  }
}

// ---- 识别别名 ----

const aliases = ref<ScrapeAlias[]>([]);
const savingAlias = ref(false);

const aliasForm = reactive({
  aliasText: "",
  tmdbId: undefined as number | undefined,
  mediaType: "",
  note: "",
});

const aliasColumns = [
  { title: "标题", dataIndex: "alias_text", key: "alias_text" },
  { title: "TMDB ID", dataIndex: "tmdb_id", key: "tmdb_id", width: 110 },
  { title: "类型", dataIndex: "mediaTypeLabel", key: "mediaTypeLabel", width: 90 },
  { title: "备注", dataIndex: "note", key: "note" },
  { title: "操作", key: "action", width: 80 },
];

async function loadAliases(): Promise<void> {
  try {
    const rows = await mediaApi.get<ScrapeAlias[]>("/scrape/aliases");
    aliases.value = rows.map((item) => ({
      ...item,
      mediaTypeLabel: item.media_type === "movie" ? "电影" : item.media_type === "tv" ? "剧集" : "不限",
    })) as unknown as ScrapeAlias[];
  } catch (e) {
    message.error(errMsg(e, "加载别名失败"));
  }
}

async function saveAlias(): Promise<void> {
  savingAlias.value = true;
  try {
    await mediaApi.post<ScrapeAlias>("/scrape/aliases", {
      alias_text: aliasForm.aliasText.trim(),
      tmdb_id: aliasForm.tmdbId,
      ...(aliasForm.mediaType ? { media_type: aliasForm.mediaType } : {}),
      ...(aliasForm.note.trim() ? { note: aliasForm.note.trim() } : {}),
    });
    message.success("别名已保存");
    aliasForm.aliasText = "";
    aliasForm.tmdbId = undefined;
    aliasForm.mediaType = "";
    aliasForm.note = "";
    await loadAliases();
  } catch (e) {
    message.error(errMsg(e, "保存别名失败"));
  } finally {
    savingAlias.value = false;
  }
}

async function removeAlias(id: number): Promise<void> {
  try {
    await mediaApi.del<void>(`/scrape/aliases/${id}`);
    message.success("已删除");
    await loadAliases();
  } catch (e) {
    message.error(errMsg(e, "删除失败"));
  }
}

// ---- 刮削记录 ----

// 列配置直接字面量（列结构是视图内部细节，非 API 契约）
const recordColumns = [
  { title: "标题", dataIndex: "title", key: "title" },
  { title: "类型", dataIndex: "kindLabel", key: "kindLabel", width: 90 },
  { title: "状态", dataIndex: "statusLabel", key: "statusLabel", width: 90 },
  { title: "错误", dataIndex: "error", key: "error", width: 240 },
  { title: "时间", dataIndex: "scraped_at", key: "scraped_at", width: 170 },
  { title: "操作", key: "action", width: 80 },
];

const records = ref<ScrapeRecord[]>([]);
const recordTotal = ref(0);
const page = ref(1);
const loadingRecords = ref(false);

function kindLabel(row: Record<string, unknown>): string {
  const kind = String(row.kind ?? "");
  const season = row.season as number | null;
  const episode = row.episode as number | null;
  if (kind === "movie") return "电影";
  if (kind === "tvshow") return "整剧";
  if (kind === "season") return `季 ${season ?? ""}`;
  if (kind === "episode") return `S${season}E${episode}`;
  return kind;
}

function statusLabel(row: Record<string, unknown>): string {
  const status = String(row.status ?? "");
  if (status === "scraped") return "已刮";
  if (status === "failed") return "失败";
  return "跳过";
}

async function loadRecords(): Promise<void> {
  loadingRecords.value = true;
  try {
    const pageData = await mediaApi.get<{ total: number; items: ScrapeRecord[] }>(
      `/scrape/records?limit=50&offset=${(page.value - 1) * 50}`,
    );
    recordTotal.value = pageData.total;
    records.value = pageData.items.map((item) => ({
      ...item,
      kindLabel: kindLabel(item as unknown as Record<string, unknown>),
      statusLabel: statusLabel(item as unknown as Record<string, unknown>),
    })) as unknown as ScrapeRecord[];
  } catch (e) {
    message.error(errMsg(e, "加载记录失败"));
  } finally {
    loadingRecords.value = false;
  }
}

function onChange(current: number): void {
  page.value = current;
  void loadRecords();
}

// ---- 一键修正（按记录重刮）----

const fixOpen = ref(false);
const fixing = ref(false);
const fixRecord = ref<ScrapeRecord | null>(null);
const fixTmdbId = ref<number | undefined>(undefined);
const fixRemember = ref(false);

function openFix(record: ScrapeRecord): void {
  fixRecord.value = record;
  fixTmdbId.value = undefined;
  fixRemember.value = false;
  fixOpen.value = true;
}

async function submitFix(): Promise<void> {
  if (!fixRecord.value) return;
  fixing.value = true;
  try {
    const result = await mediaApi.post<ScrapeFileResult>(
      "/scrape/records/rescrape",
      {
        scope_key: fixRecord.value.scope_key,
        ...(fixTmdbId.value ? { tmdb_id: fixTmdbId.value } : {}),
        ...(fixRemember.value ? { remember: true } : {}),
      },
    );
    if (result.status === "ok") message.success(result.detail);
    else message.warning(result.detail);
    if (result.remembered) message.info("已记入识别别名");
    fixOpen.value = false;
    await loadRecords();
  } catch (e) {
    message.error(errMsg(e, "重刮失败"));
  } finally {
    fixing.value = false;
  }
}

onMounted(() => {
  void loadConfig();
  void loadRecords();
  void loadAliases();
});
</script>

<style scoped>
.tip {
  margin-bottom: 16px;
}
.cfg-tag {
  margin-left: 8px;
}
.mini {
  font-size: 11px;
}
.mb {
  margin-bottom: 12px;
}
.param {
  width: 100%;
}
.param-label {
  margin-bottom: 4px;
  color: var(--ant-color-text-secondary, #666);
}
.param-help {
  margin-left: 4px;
  color: #999;
}
.muted {
  color: #999;
}
</style>
