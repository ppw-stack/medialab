<template>
  <div class="page">
    <PageHeader
      eyebrow="媒体库 · MEDIA"
      title="存储源"
      sub="光鸭盘账号——凭据、容量与直链能力"
      compact
    >
      <template #actions>
        <a-button size="small" :loading="probingAll" @click="probeAll">全部测连</a-button>
        <a-button type="primary" size="small" @click="openCreate">新增</a-button>
      </template>
    </PageHeader>

    <a-card size="small">
      <DataTable
        :data-source="drivers"
        :columns="columns"
        row-key="id"
        size="small"
        table-layout="fixed"
        :loading="loading"
        :pagination="false"
        :locale="{ emptyText: '暂无存储源' }"
      >
        <template #bodyCell="{ column, record }">
          <!-- 名称 + 状态点 -->
          <template v-if="column.key === 'name'">
            <div class="name-cell">
              <a-badge :status="statusBadge(record.id)" />
              <span>{{ record.name }}</span>
              <a-tag v-if="record.type === 'guangya'" color="cyan">光鸭盘</a-tag>
              <a-tooltip
                v-if="record.role === 'master'"
                title="聚合组的主数据源：内容以它为准（媒体库扫描 / STRM / 播放都只认它）"
              >
                <a-tag color="gold">主</a-tag>
              </a-tooltip>
              <a-tooltip
                v-else-if="record.role === 'secondary'"
                title="聚合组的副数据源：内容是缓冲，不对外展示——归拢到主盘后自动清理副本"
              >
                <a-tag color="orange">副</a-tag>
              </a-tooltip>
            </div>
            <div v-if="statusText(record.id)" class="status-hint">
              {{ statusText(record.id) }}
            </div>
            <div v-if="isStale(record.id)" class="status-hint stale">
              {{ checkedAtText(record.id) }}
            </div>
          </template>

          <!-- 账号 + 容量 -->
          <template v-else-if="column.key === 'account'">
            <template v-if="statusOf(record.id)">
              <div class="mono">{{ statusOf(record.id)?.account_id || '—' }}</div>
              <div class="capacity">
                <a-progress
                  :percent="usedPercent(record.id)"
                  :show-info="false"
                  size="small"
                  :status="usedPercent(record.id) > 90 ? 'exception' : 'normal'"
                />
                <span class="muted">
                  {{ formatBytes(statusOf(record.id)?.used_bytes ?? 0) }} /
                  {{ formatBytes(statusOf(record.id)?.total_bytes ?? 0) }}
                </span>
              </div>
            </template>
            <span v-else class="muted">—</span>
          </template>

          <!-- 媒体库（cid + 路径） -->
          <template v-else-if="column.key === 'library'">
            <template v-if="record.media_cid">
              <div class="mono truncate">{{ record.media_cid }}</div>
              <div class="muted truncate">{{ record.media_path || '—' }}</div>
            </template>
            <a-tag v-else color="default">未配置</a-tag>
          </template>

          <!-- 本地输出目录 -->
          <template v-else-if="column.key === 'output_dir'">
            <span class="mono truncate">{{ record.output_dir || '—' }}</span>
          </template>

          <template v-else-if="column.key === 'status'">
            <a-tag :color="statusColor(record.id)">{{ statusLabel(record.id) }}</a-tag>
          </template>

          <template v-else-if="column.key === 'action'">
            <a-space :size="4">
              <a-tooltip title="测连：检查凭据 / 账号容量 / 能否换直链">
                <a-button
                  size="small"
                  :loading="probingId === record.id"
                  @click="probeDriver(record.id)"
                >
                  测连
                </a-button>
              </a-tooltip>
              <a-tooltip
                :title="record.credential_masked ? '重新登录（会覆盖现有凭据）' : '登录存储源'"
              >
                <a-button size="small" @click="onLogin(record)">登录</a-button>
              </a-tooltip>
              <a-button size="small" @click="openConfig(record)">配置</a-button>
              <!--
                禁用原因必须写出来——否则用户只看到灰按钮，不知道缺什么。

                tooltip 内层包 <span>：antd 的 disabled 按钮**不派发鼠标事件**，
                tooltip 直接包按钮会完全不触发（antd 官方同款处理）。
              -->
              <a-tooltip v-if="isSecondary(record)" title="副数据源是缓冲，不参与媒体库扫描">
                <span>
                  <a-button size="small" disabled>扫描</a-button>
                </span>
              </a-tooltip>
              <a-tooltip
                v-else-if="!record.media_cid || !record.output_dir"
                :title="scanDisabledTip(record)"
              >
                <span>
                  <a-button size="small" disabled>扫描</a-button>
                </span>
              </a-tooltip>
              <a-button
                v-else
                size="small"
                :loading="scanningId === record.id || isScanning(record.id)"
                @click="onScan(record)"
              >
                {{ isScanning(record.id) ? '扫描中' : '扫描' }}
              </a-button>
              <a-popconfirm
                title="删除存储源？关联的媒体条目也会一并删除。"
                ok-text="删除"
                cancel-text="取消"
                @confirm="onDelete(record.id)"
              >
                <a-button size="small" danger>删除</a-button>
              </a-popconfirm>
            </a-space>
          </template>
        </template>
      </DataTable>
    </a-card>

    <!-- 新增存储源 -->
    <a-modal
      v-model:open="createOpen"
      title="新增存储源"
      :confirm-loading="saving"
      ok-text="创建"
      cancel-text="取消"
      @ok="onCreate"
    >
      <a-form layout="vertical">
        <a-form-item label="名称">
          <a-input v-model:value="form.name" placeholder="我的光鸭盘" />
        </a-form-item>
        <a-form-item label="类型">
          <a-input value="光鸭盘" disabled />
        </a-form-item>
      </a-form>
    </a-modal>

    <!-- 媒体库配置 -->
    <a-drawer
      v-model:open="configOpen"
      :title="`媒体库配置 — ${configDriver?.name ?? ''}`"
      :width="560"
      :confirm-loading="saving"
      @ok="onSaveConfig"
    >
      <a-form layout="vertical">
        <!--
          副数据源是**缓冲**：不扫描、不生成 strm、不进媒体库 → 媒体库相关的
          配置项全部隐藏（副盘只配「缓冲根目录」）。
        -->
        <template v-if="configDriver?.role !== 'secondary'">
          <a-form-item label="媒体库目录" required>
            <a-alert
              type="warning"
              show-icon
              style="margin-bottom: 8px"
              message="从账号根目录开始浏览"
              description="根目录下是网盘的**全部**目录（含私人目录）。请只选择真正用于媒体库的那一个。"
            />
            <DirBrowser
              title="网盘媒体库目录"
              :stack="mediaStack"
              :dirs="mediaDirs"
              :loading="loadingMediaDirs"
              @open="openMediaDir"
              @go="goMediaDir"
            />
            <div class="muted hint">已选：{{ mediaSelectedLabel }}</div>
            <div class="hint">
              点进目标目录即选中它（面包屑可回退到上层）。存的是**目录 id**而非路径 ——目录改名后 id
              不变，路径会失效。
            </div>
          </a-form-item>

          <a-form-item v-if="configDriver?.type !== 'guangya'" label="媒体库路径">
            <a-input :value="configForm.media_path || ''" disabled class="mono" />
            <div class="hint">保存时按 cid 自动解析，仅供查看。</div>
          </a-form-item>

          <a-form-item label="本地媒体库目录" required>
            <a-input
              v-model:value="configForm.output_dir"
              placeholder="/app/library"
              class="mono"
            />
          </a-form-item>

          <a-form-item label="视频文件后缀" required>
            <a-select
              v-model:value="configForm.video_exts"
              mode="tags"
              :token-separators="[',', ' ']"
              placeholder="输入后缀后回车"
              class="mono"
            />
            <div class="hint">
              只有这些后缀的文件会生成 strm——strm 文件名会去掉视频后缀（<code>x.mkv</code> →
              <code>x.strm</code>）。
            </div>
          </a-form-item>

          <a-form-item label="媒体图片后缀">
            <a-select
              v-model:value="configForm.image_exts"
              mode="tags"
              :token-separators="[',', ' ']"
              placeholder="jpg jpeg png webp"
              class="mono"
            />
            <div class="hint">
              扫描时**同步到本地**（与 strm 同目录，保持原文件名）。清空 = 不同步图片。
            </div>
          </a-form-item>

          <a-form-item label="媒体数据后缀">
            <a-select
              v-model:value="configForm.data_exts"
              mode="tags"
              :token-separators="[',', ' ']"
              placeholder="nfo ass ssa srt"
              class="mono"
            />
            <div class="hint">
              扫描时**同步到本地**（与 strm 同目录，保持原文件名）。清空 = 不同步元数据。
            </div>
          </a-form-item>

          <a-form-item label="最小视频">
            <a-input-number
              v-model:value="configForm.min_size_mb"
              :min="0"
              :step="10"
              style="width: 160px"
            />
            <span class="unit">MB</span>
            <div class="hint">小于此体积的文件跳过（过滤样片）。0 = 不过滤。</div>
          </a-form-item>

          <a-form-item label="包含子目录">
            <a-switch v-model:checked="configForm.include_subdirs" />
            <div class="hint">关闭则只扫 cid 指向的那一层，不递归其下子目录。</div>
          </a-form-item>

          <a-form-item label="扫描时清理已删除">
            <a-switch v-model:checked="configForm.auto_prune" />
            <div class="hint">
              开启后，扫描会把**网盘上已删除**的文件对应的条目与本地 strm
              一并清理掉。关闭则只加不删（DB 与本地会留下旧数据）。
            </div>
            <a-alert
              v-if="configForm.auto_prune"
              type="warning"
              show-icon
              message="清理会永久删除本地 strm 文件（不进回收站），无法恢复。"
              style="margin-top: 6px"
            />
          </a-form-item>
        </template>
      </a-form>

      <template #footer>
        <a-space>
          <a-button @click="configOpen = false">取消</a-button>
          <a-button type="primary" :loading="saving" @click="onSaveConfig">保存</a-button>
        </a-space>
      </template>
    </a-drawer>

    <!-- 扫描进度 -->
    <a-modal
      v-model:open="scanOpen"
      title="扫描进度"
      :footer="null"
      :width="720"
      :mask-closable="false"
      @cancel="closeScan"
    >
      <template v-if="scanJob">
        <a-descriptions :column="2" size="small" bordered>
          <a-descriptions-item label="存储源">
            {{ scanJob.driver_name }}
          </a-descriptions-item>
          <a-descriptions-item label="状态">
            <a-tag :color="scanStateColor(scanJob.state)">
              {{ scanStateLabel(scanJob.state) }}
            </a-tag>
          </a-descriptions-item>
          <a-descriptions-item label="已处理"> {{ scanJob.processed }} 个文件 </a-descriptions-item>
          <a-descriptions-item label="耗时"> {{ scanJob.elapsed_sec }}s </a-descriptions-item>
          <a-descriptions-item label="新增 / 更新" :span="2">
            <span class="mono">
              STRM {{ scanJob.created }} / {{ scanJob.updated }} （未变 {{ scanJob.unchanged }}）
            </span>
          </a-descriptions-item>
          <a-descriptions-item label="附件" :span="2">
            <span class="mono">
              新增 {{ scanJob.attachment_synced }} / 未变 {{ scanJob.attachment_unchanged }} / 失败
              {{ scanJob.attachment_failed }}
            </span>
          </a-descriptions-item>
          <a-descriptions-item label="跳过" :span="2">
            <span class="mono">
              未知 {{ scanJob.skipped_unknown }} / 太小
              {{ scanJob.skipped_small }}
            </span>
          </a-descriptions-item>
          <a-descriptions-item label="去重 / 清理" :span="2">
            <span class="mono">
              去重 {{ scanJob.deduped }}（同名不同扩展名，保留大的） / 清理
              {{ scanJob.pruned }}（网盘上已消失）
            </span>
          </a-descriptions-item>
          <a-descriptions-item label="正在处理" :span="2">
            <span class="mono path-line" :title="scanJob.current_path">
              {{ scanJob.current_path || '—' }}
            </span>
          </a-descriptions-item>
        </a-descriptions>

        <!-- 去重明细：默认折叠（点开才看，避免详情面板被撑爆） -->
        <div v-if="scanJob.dedup_details?.length" class="dedup-details">
          <a-button type="link" size="small" @click="dedupOpen = !dedupOpen">
            {{ dedupOpen ? '收起' : '展开' }}去重明细（{{ scanJob.dedup_details.length }}）
          </a-button>
          <div v-if="dedupOpen" class="dedup-list">
            <div v-for="(line, i) in scanJob.dedup_details" :key="i" class="dedup-line mono">
              {{ line }}
            </div>
          </div>
        </div>

        <a-alert
          v-if="scanJob.state === 'failed'"
          type="error"
          :message="scanJob.error || '扫描失败'"
          show-icon
          style="margin-top: 12px"
        />

        <div class="scan-log">
          <div v-for="(line, i) in scanJob.logs" :key="i" class="log-line mono">
            {{ line }}
          </div>
        </div>

        <div style="margin-top: 12px; text-align: right">
          <a-space>
            <a-button v-if="scanJob.state === 'running'" size="small" @click="closeScan">
              后台运行
            </a-button>
            <a-button v-else type="primary" size="small" @click="closeScan"> 关闭 </a-button>
          </a-space>
        </div>
      </template>
      <a-empty v-else description="正在启动扫描…" />
    </a-modal>

    <!-- 登录方式选择 -->
    <a-modal v-model:open="loginOpen" title="登录存储源" :footer="null" :width="360">
      <template v-if="loginDriver">
        <p class="hint">存储源「{{ loginDriver.name }}」（光鸭盘）尚未登录。</p>
        <div v-if="loginDriver.type === 'guangya'" class="identity-pick">
          <p class="hint" style="margin: 0 0 6px">扫码身份</p>
          <a-radio-group v-model:value="loginIdentity" button-style="solid" style="width: 100%">
            <a-radio-button value="web" style="width: 50%; text-align: center">
              Web 身份
            </a-radio-button>
            <a-radio-button value="client" style="width: 50%; text-align: center">
              客户端身份
            </a-radio-button>
          </a-radio-group>
          <p class="hint" style="margin: 6px 0 0">{{ loginIdentityHint }}</p>
        </div>
        <a-space direction="vertical" style="width: 100%">
          <a-button block type="primary" @click="onLoginByQr"> 扫码登录 </a-button>
          <a-button v-if="loginDriver.type === 'guangya'" block @click="openSms(loginDriver)">
            短信验证码登录
          </a-button>
        </a-space>
      </template>
    </a-modal>

    <!-- 短信验证码登录（光鸭盘）-->
    <a-modal v-model:open="smsOpen" title="短信验证码登录" :footer="null" :width="360">
      <a-form layout="vertical">
        <a-form-item label="手机号">
          <a-input v-model:value="smsPhone" placeholder="+86 13800138000" class="mono" />
          <div class="hint">需含国际区号（+86 / +1 …）。</div>
        </a-form-item>
        <a-button :loading="smsSending" :disabled="!smsPhone" block @click="onSendSms">
          发送验证码
        </a-button>
        <a-form-item v-if="smsToken" label="验证码">
          <a-input
            v-model:value="smsCode"
            placeholder="6 位数字"
            class="mono"
            @press-enter="onVerifySms"
          />
        </a-form-item>
      </a-form>
      <a-alert
        v-if="smsError"
        type="error"
        :message="smsError"
        show-icon
        style="margin-top: 12px"
      />
      <a-alert
        v-else-if="smsOk"
        type="success"
        message="登录成功，凭据已加密存储"
        show-icon
        style="margin-top: 12px"
      />
      <a-button
        v-if="smsToken && !smsOk"
        type="primary"
        block
        :loading="smsVerifying"
        :disabled="!smsCode"
        style="margin-top: 12px"
        @click="onVerifySms"
      >
        验证并登录
      </a-button>
    </a-modal>

    <!-- 扫码登录 -->
    <a-modal
      v-model:open="qrOpen"
      :title="`光鸭盘 扫码登录${qrMode === 'client' ? '（客户端身份）' : ''}`"
      :footer="null"
      :width="360"
    >
      <div v-if="qrImageSrc" class="qr-box">
        <img :src="qrImageSrc" alt="扫码登录" />
      </div>
      <a-empty v-else description="正在获取二维码…" />

      <a-alert
        v-if="qrDriverType === 'guangya' && qrMode === 'client'"
        type="info"
        message="客户端身份：可换取非会员大文件直链；凭据自带 refresh_token，后台会自动续期"
        show-icon
        style="margin-bottom: 12px"
      />
      <a-alert v-if="qrError" type="error" :message="qrError" show-icon style="margin-top: 12px" />
      <a-alert
        v-else-if="qrKind === 'confirmed'"
        type="success"
        message="登录成功，凭据已加密存储"
        show-icon
        style="margin-top: 12px"
      />
      <a-alert
        v-else-if="qrKind === 'expired'"
        type="warning"
        message="二维码已过期，请重新获取"
        show-icon
        style="margin-top: 12px"
      />
      <p v-else class="hint">
        用光鸭云盘 APP 扫描二维码。{{ qrKind === 'scanned' ? '已扫描，等待确认…' : '' }}
      </p>
      <template #footer>
        <a-space>
          <a-button size="small" @click="refreshQr"> 重新获取二维码 </a-button>
        </a-space>
      </template>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { message } from 'ant-design-vue';
import { ApiError, errMsg, mediaApi as api } from '../../api';
import type {
  DirEntry,
  Driver,
  DriverConfig,
  DriverInput,
  DriverStatus,
  QrStartResult,
  QrStatusKind,
  QrStatusResult,
  ScanJob,
  SmsSendResult,
  SmsVerifyResult,
} from '../../types';
import { formatBytes, formatTime } from '../../utils/format';
import DataTable from '../../components/DataTable.vue';
import DirBrowser from '../../components/DirBrowser.vue';
import PageHeader from '../../components/PageHeader.vue';

const drivers = ref<Driver[]>([]);
const loading = ref(false);
const saving = ref(false);

const createOpen = ref(false);
const form = ref<DriverInput>({ type: 'guangya', name: '' });

/** 媒体库配置抽屉 */
const configOpen = ref(false);
const configDriver = ref<Driver | null>(null);
const configForm = ref<DriverConfig>({
  media_cid: '',
  media_path: '',
  output_dir: '',
  video_exts: [],
  image_exts: [],
  data_exts: [],
  min_size_mb: 0,
  include_subdirs: true,
  auto_prune: false,
});

const qrOpen = ref(false);
const qrDriverId = ref<number | null>(null);
const qrDriverType = ref('guangya');
/** 本次扫码身份：web = Web 身份；client = 客户端身份（aud = 客户端 clientId） */
const qrMode = ref<'web' | 'client'>('web');

const qrSessionId = ref('');
const qrImage = ref('');
const qrKind = ref<QrStatusKind>('waiting');
const qrError = ref('');

// -- 登录方式选择 --
const loginOpen = ref(false);
const loginDriver = ref<Driver | null>(null);
/**
 * 光鸭扫码身份（**单选组**，不是多开一个按钮）。
 *
 * - `web`    = Web 身份，普通登录；非会员的大文件直链会被 `254` 拒绝
 * - `client` = 客户端身份（token 的 `aud` = 客户端 clientId），可换大文件直链，
 *              且 scope 带 `offline` → 有 `refresh_token` 可自续期
 */
const loginIdentity = ref<'web' | 'client'>('web');

/** 身份说明（跟着单选取值走，别让用户猜两个选项的差别）。 */
const loginIdentityHint = computed(() =>
  loginIdentity.value === 'client'
    ? '客户端身份：可换取非会员大文件直链；凭据自带 refresh_token，后台会自动续期。'
    : 'Web 身份：普通登录；非会员的大文件直链会被服务端拒绝（254）。',
);

// -- 短信登录（光鸭盘）--
const smsOpen = ref(false);
const smsPhone = ref('');
const smsCode = ref('');
const smsToken = ref('');
const smsError = ref('');
const smsOk = ref(false);
const smsSending = ref(false);
const smsVerifying = ref(false);

/** 各存储源的账号状态（测连结果）—— key = driver id */
const statuses = ref<Record<number, DriverStatus>>({});
const probingId = ref<number | null>(null);
const probingAll = ref(false);
const scanningId = ref<number | null>(null);

let pollTimer: number | undefined;

const columns = [
  { title: '名称', key: 'name' },
  { title: '账号', key: 'account', width: 200 },
  { title: '媒体库', key: 'library', width: 220 },
  { title: '本地目录', key: 'output_dir', width: 180 },
  { title: '状态', key: 'status', width: 100 },
  // 6 个操作按钮（测连/登录/配置/扫描/增量/删除）需要 ~323px——
  // 窄于这个宽度时按钮会被挤出行外，导致表格横向溢出。
  { title: '操作', key: 'action', width: 340 },
];

const qrImageSrc = computed(() => (qrImage.value ? `data:image/png;base64,${qrImage.value}` : ''));

// -- 状态读取 --

function statusOf(id: number): DriverStatus | undefined {
  return statuses.value[id];
}

function statusLabel(id: number): string {
  const s = statusOf(id);
  if (!s) return '未测连';
  switch (s.status) {
    case 'ok':
      return s.can_get_link ? '正常' : '可用';
    case 'needs_verify':
      return '待验证';
    case 'no_credential':
      return '未登录';
    default:
      return '异常';
  }
}

function statusColor(id: number): string {
  const s = statusOf(id);
  if (!s) return 'default';
  switch (s.status) {
    case 'ok':
      return 'green';
    case 'needs_verify':
      return 'orange';
    case 'no_credential':
      return 'default';
    default:
      return 'red';
  }
}

function statusBadge(id: number): 'success' | 'warning' | 'error' | 'default' {
  const s = statusOf(id);
  if (!s) return 'default';
  switch (s.status) {
    case 'ok':
      return 'success';
    case 'needs_verify':
      return 'warning';
    case 'no_credential':
      return 'default';
    default:
      return 'error';
  }
}

function statusText(id: number): string {
  const s = statusOf(id);
  if (!s) return '';
  if (s.status === 'ok') {
    return s.can_get_link ? '直链正常' : '可直链未验证';
  }
  return s.message;
}

/**
 * 测连状态是否已「陈旧」。
 *
 * 后端的测连缓存**只在进程启动时刷新一次**（之后永不自动更新），
 * 所以面板上的「正常」可能已经是几小时前的快照——token 过期了、
 * 账号被登出了、容量早就变了，面板却还显示绿的。
 *
 * 超过 :data:`STALE_AFTER_MIN` 分钟就提示用户重新测连。
 */
const STALE_AFTER_MIN = 30;

/**
 * 丢掉某存储源的本地测连状态（凭据变更后调用）。
 *
 * **为什么必须丢**：重新扫码/短信登录换的是**凭据**，旧状态（旧账号的
 * uid / 容量）对新凭据没有意义。而 ``load()`` 的合并逻辑会保留本地
 * 较新的状态——若不在这里显式丢掉，旧状态会一直显示到后台重探完成
 * （几秒），期间用户看到的是**上一个账号**的信息。
 */
function dropStatus(id: number): void {
  if (!(id in statuses.value)) return;
  const { [id]: _dropped, ...rest } = statuses.value;
  statuses.value = rest;
}

function isStale(id: number): boolean {
  const s = statusOf(id);
  if (!s?.checked_at) return false;
  // 后端给的是**北京时间字符串**（``yyyy-MM-dd HH:mm:ss``）→ 显式带 +08:00，
  // 否则会被当成本地时区解析，算出来的「年龄」偏差 8 小时。
  // （同 Dashboard.vue / TorrentIndex.vue 的口径）
  const checked = new Date(s.checked_at.replace(' ', 'T') + '+08:00').getTime();
  if (Number.isNaN(checked)) return false;
  return Date.now() - checked > STALE_AFTER_MIN * 60_000;
}

/** 陈旧提示文案（含测连时间）。 */
function checkedAtText(id: number): string {
  const s = statusOf(id);
  if (!s?.checked_at) return '';
  return `状态已过期（${formatTime(s.checked_at)} 测连），建议重新测连`;
}

function usedPercent(id: number): number {
  const s = statusOf(id);
  if (!s || !s.total_bytes) return 0;
  return Math.round((s.used_bytes / s.total_bytes) * 100);
}

// -- 加载 --

async function load(): Promise<void> {
  loading.value = true;
  try {
    drivers.value = await api.get<Driver[]>('/drivers');
    // 用后端缓存的测连状态初始化（启动自动测连 / 手动测连的结果）。
    //
    // **合并而非替换**，且按 ``checked_at`` 取**更新**的一份：
    //
    // - 后端缓存里没有的 driver（conn == null）不能把本地已有的状态
    //   抹掉——比如后端刚重启（缓存空）或刚手动测连完就触发一次 load()
    // - 后端缓存是 load() **发起时刻**的快照，可能比本地旧：手动测连
    //   在 load() 在途期间完成时，若无条件覆盖，面板会显示**较旧**的
    //   状态（「倒退」）。故用 ``checked_at`` 比较，谁新用谁
    //   （北京时间字符串定宽 → 字典序 == 时间序）
    const next: Record<number, DriverStatus> = { ...statuses.value };
    for (const d of drivers.value) {
      if (!d.conn) continue;
      const localAt = next[d.id]?.checked_at ?? null;
      const remoteAt = d.conn.checked_at ?? null;
      if (localAt && (!remoteAt || localAt > remoteAt)) continue;
      next[d.id] = {
        logged_in: d.conn.logged_in,
        status: d.conn.status,
        message: d.conn.message,
        account_id: d.conn.account_id,
        total_bytes: d.conn.total_bytes,
        used_bytes: d.conn.used_bytes,
        remain_bytes: d.conn.remain_bytes,
        can_get_link: d.conn.can_get_link,
        checked_at: remoteAt,
      };
    }
    // 已删除的存储源要从状态表里移除（否则 id 复用会串状态）
    const liveIds = new Set(drivers.value.map((d) => d.id));
    for (const id of Object.keys(next)) {
      if (!liveIds.has(Number(id))) delete next[Number(id)];
    }
    statuses.value = next;
  } catch (e) {
    message.error(errMsg(e, '加载存储源失败'));
  } finally {
    loading.value = false;
  }
}

onMounted(() => {
  void load();
  // 页面刷新后**找回正在跑的后台任务**——否则关掉页面就看不到进度了，
  // 而后台任务其实还在跑。
  void resumeRunningJob();
});

/** 找回正在运行的扫描任务（刷新/重新进入页面时）。 */
async function resumeRunningJob(): Promise<void> {
  try {
    const jobs = await api.get<ScanJob[]>('/scan-jobs');
    const running = jobs.find((j) => j.state === 'running');
    if (running) {
      scanJob.value = running;
      scanOpen.value = true;
      startScanPolling(running.id);
    }
  } catch {
    // 找回失败不打扰用户——可能本来就没有任务
  }
}

/** 该存储源是否正在扫描。 */
function isScanning(driverId: number): boolean {
  return scanJob.value?.state === 'running' && scanJob.value.driver_id === driverId;
}

// -- 新增 --

function openCreate(): void {
  form.value = { type: 'guangya', name: '' };
  createOpen.value = true;
}

async function onCreate(): Promise<void> {
  saving.value = true;
  try {
    await api.post<Driver>('/drivers', form.value);
    message.success('已创建');
    createOpen.value = false;
    await load();
  } catch (e) {
    message.error(errMsg(e, '创建失败'));
  } finally {
    saving.value = false;
  }
}

// -- 配置 --

/**
 * 媒体后缀的**默认值**（存量行未配置时的预填值）。
 *
 * 真源在 ``app/domain/media_exts.py``（列默认值同源）——前端这份只为
 * 打开配置抽屉时预填，两边保持一致；不一致的后果只是预填值与新建存储源
 * 的列默认不同，不会崩。
 *
 * 为什么不在后端 ``DriverOut`` 层兜底：清空后保存的行（``image_exts=""``
 * = 关闭附件同步）如果被 ``or 默认`` 改写，用户清空后重开抽屉会看到默认值，
 * 以为没存上。预填只发生在**打开抽屉**这一刻，保存后以 DB 为准。
 */
const DEFAULT_IMAGE_EXTS = ['jpg', 'jpeg', 'png', 'webp'];
const DEFAULT_DATA_EXTS = ['nfo', 'ass', 'ssa', 'srt'];

/**
 * 网盘目录浏览的**面包屑栈**（栈底 = 账号根目录）。
 *
 * 栈底的 id 是**空串**——后端按驱动类型把空串解析成真正的根
 * （光鸭的根 fileId 就是空串），前端不必知道这个差异。
 */
const mediaStack = ref<DirEntry[]>([{ id: '', name: '网盘根目录' }]);
const mediaDirs = ref<DirEntry[]>([]);
const loadingMediaDirs = ref(false);

/** 本次浏览选中的目录（已有配置时为空——只有 id，没有路径可拼）。 */
const mediaPicked = ref<{ id: string; path: string } | null>(null);

/**
 * 「已选」展示文案。
 *
 * 三种来源：本次浏览选的（有路径）/ 已有配置（光鸭没有路径反查，只有 id）/
 * 什么都没选。
 */
const mediaSelectedLabel = computed(() => {
  if (mediaPicked.value) {
    return `${mediaPicked.value.path}（id: ${mediaPicked.value.id}）`;
  }
  if (configForm.value.media_cid) {
    return configForm.value.media_path
      ? `${configForm.value.media_path}（id: ${configForm.value.media_cid}）`
      : `id: ${configForm.value.media_cid}`;
  }
  return '（未选）';
});

function openConfig(driver: Driver): void {
  configDriver.value = driver;
  configForm.value = {
    media_cid: driver.media_cid ?? '',
    media_path: driver.media_path ?? '',
    output_dir: driver.output_dir ?? '',
    video_exts: splitExts(driver.video_exts),
    // 未配置时预填默认值——用户不用手抄常用清单（真源见 DEFAULT_IMAGE_EXTS 注释）
    image_exts: splitExts(driver.image_exts).length
      ? splitExts(driver.image_exts)
      : [...DEFAULT_IMAGE_EXTS],
    data_exts: splitExts(driver.data_exts).length
      ? splitExts(driver.data_exts)
      : [...DEFAULT_DATA_EXTS],
    min_size_mb: driver.min_size_mb ?? 0,
    include_subdirs: driver.include_subdirs ?? true,
    auto_prune: driver.auto_prune ?? false,
  };
  // 浏览状态回到账号根——本次浏览的选中记录清空（已有配置仍显示在下方）
  mediaStack.value = [{ id: '', name: '网盘根目录' }];
  mediaDirs.value = [];
  mediaPicked.value = null;
  configOpen.value = true;
  void loadMediaDirs('');
}

/** 加载某目录下的**子目录**（逐层懒加载，不一次性拉全树）。 */
async function loadMediaDirs(parentId: string): Promise<void> {
  const driver = configDriver.value;
  if (!driver) return;
  loadingMediaDirs.value = true;
  try {
    const resp = await api.get<{ dirs: DirEntry[] }>(
      `/drivers/${driver.id}/browse-dirs?parent_id=${encodeURIComponent(parentId)}`,
    );
    mediaDirs.value = resp.dirs ?? [];
  } catch (e) {
    message.error(errMsg(e, '加载网盘目录失败'));
  } finally {
    loadingMediaDirs.value = false;
  }
}

/** 副数据源？——副盘不参与媒体库扫描（内容会归拢到主盘）。 */
function isSecondary(record: Driver): boolean {
  return record.role === 'secondary';
}

/**
 * 进入子目录 —— **同时把它设为选中的媒体库目录**。
 *
 * 语义与「数据同步」页的云端目标目录一致：正在看哪个目录 = 要用哪个目录。
 */
function openMediaDir(entry: DirEntry): void {
  mediaStack.value = [...mediaStack.value, entry];
  pickMediaDir(entry);
  void loadMediaDirs(entry.id);
}

/** 面包屑回退 —— 选回那一层目录（回到根 = 取消选择）。 */
function goMediaDir(index: number): void {
  mediaStack.value = mediaStack.value.slice(0, index + 1);
  const picked = mediaStack.value[index];
  if (!picked) return;
  pickMediaDir(picked);
  void loadMediaDirs(picked.id);
}

function pickMediaDir(entry: DirEntry): void {
  mediaPicked.value = { id: entry.id, path: mediaPathOf(mediaStack.value) };
  configForm.value.media_cid = entry.id;
}

/** 由面包屑栈拼出路径（展示用）——根节点不参与拼接。 */
function mediaPathOf(stack: DirEntry[]): string {
  return stack
    .map((c) => c.name)
    .filter((n) => n !== '网盘根目录')
    .join('/');
}

function splitExts(raw: string | null | undefined): string[] {
  if (!raw) return [];
  return raw
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
}

async function onSaveConfig(): Promise<void> {
  if (configDriver.value === null) return;
  saving.value = true;
  try {
    await api.put<Driver>(`/drivers/${configDriver.value.id}/config`, {
      media_cid: configForm.value.media_cid || null,
      output_dir: configForm.value.output_dir || null,
      video_exts: configForm.value.video_exts.join(','),
      image_exts: configForm.value.image_exts.join(','),
      data_exts: configForm.value.data_exts.join(','),
      min_size_mb: configForm.value.min_size_mb ?? 0,
      include_subdirs: configForm.value.include_subdirs ?? true,
      auto_prune: configForm.value.auto_prune ?? false,
    });
    message.success('已保存');
    configOpen.value = false;
    await load();
  } catch (e) {
    message.error(errMsg(e, '保存失败'));
  } finally {
    saving.value = false;
  }
}

// -- 扫描 --

/**
 * 扫描按钮的**禁用原因**（tooltip 文案）。
 *
 * 必须**点名缺哪一项**——否则用户只看到一个灰按钮，不知道该去补什么。
 * id 的叫法由后端给出（``media_cid_label``），前端不按类型分支。
 */
function scanDisabledTip(record: Driver): string {
  const missing: string[] = [];
  if (!record.media_cid) {
    missing.push(record.media_cid_label || '媒体库目录');
  }
  if (!record.output_dir) missing.push('本地媒体库目录');
  return `尚未配置${missing.join('、')}，请先点「配置」补齐后再扫描`;
}

/** 扫描进度弹窗 */
const scanOpen = ref(false);
const scanJob = ref<ScanJob | null>(null);
/** 去重明细是否展开（默认折叠——展开会撑高详情面板） */
const dedupOpen = ref(false);
let scanTimer: number | undefined;

async function onScan(driver: Driver): Promise<void> {
  scanningId.value = driver.id;
  try {
    const res = await api.post<{ job_id: string }>(`/drivers/${driver.id}/scan`);
    scanOpen.value = true;
    scanJob.value = null;
    dedupOpen.value = false;
    startScanPolling(res.job_id);
  } catch (e) {
    // 409 = 该存储源已有任务在跑 → 直接接管那个任务，而不是报错
    if (e instanceof ApiError && e.status === 409) {
      await resumeRunningJob();
      message.info('该存储源已有扫描任务，已切换到它的进度');
    } else {
      message.error(errMsg(e, '启动扫描失败'));
    }
  } finally {
    scanningId.value = null;
  }
}

function startScanPolling(jobId: string): void {
  stopScanPolling();
  const tick = async (): Promise<void> => {
    try {
      const job = await api.get<ScanJob>(`/scan-jobs/${jobId}`);
      scanJob.value = job;
      if (job.state !== 'running') {
        stopScanPolling();
        // 完成/失败后刷新列表（last_scan_at / 条目数变了）
        await load();
      }
    } catch (e) {
      stopScanPolling();
      message.error(errMsg(e, '查询扫描进度失败'));
    }
  };
  void tick();
  scanTimer = window.setInterval(tick, 1500);
}

function stopScanPolling(): void {
  if (scanTimer !== undefined) {
    window.clearInterval(scanTimer);
    scanTimer = undefined;
  }
}

function closeScan(): void {
  stopScanPolling();
  scanOpen.value = false;
}

function scanStateLabel(state: string): string {
  switch (state) {
    case 'running':
      return '扫描中';
    case 'success':
      return '完成';
    case 'cancelled':
      return '已取消';
    default:
      return '失败';
  }
}

function scanStateColor(state: string): string {
  switch (state) {
    case 'running':
      return 'processing';
    case 'success':
      return 'green';
    case 'cancelled':
      return 'default';
    default:
      return 'red';
  }
}

// -- 测连 --

async function probeDriver(id: number, silent = false): Promise<void> {
  probingId.value = id;
  try {
    const status = await api.get<DriverStatus>(`/drivers/${id}/probe`);
    statuses.value = { ...statuses.value, [id]: status };

    if (silent) return; // 后台自动测连（扫码/短信登录后）不打扰
    switch (status.status) {
      case 'ok':
        message.success(status.message);
        break;
      case 'needs_verify':
        message.warning(status.message);
        break;
      case 'no_credential':
        message.info(status.message);
        break;
      default:
        message.error(status.message);
    }
  } catch (e) {
    message.error(errMsg(e, '测连失败'));
  } finally {
    probingId.value = null;
  }
}

async function probeAll(): Promise<void> {
  probingAll.value = true;
  try {
    for (const driver of drivers.value) {
      await probeDriver(driver.id);
    }
  } finally {
    probingAll.value = false;
  }
}

// -- 删除 --

async function onDelete(id: number): Promise<void> {
  try {
    await api.del(`/drivers/${id}`);
    message.success('已删除');
    await load();
  } catch (e) {
    message.error(errMsg(e, '删除失败'));
  }
}

// -- 登录 --

/** 打开登录方式选择。 */
function onLogin(driver: Driver): void {
  loginDriver.value = driver;
  // 每次都从默认身份开始——上一次选的客户端身份不该静默延续
  loginIdentity.value = 'web';
  loginOpen.value = true;
}

/** 用扫码方式登录（身份取当前单选值）。 */
function onLoginByQr(): void {
  const driver = loginDriver.value;
  if (!driver) return;
  const mode = driver.type === 'guangya' ? loginIdentity.value : 'web';
  loginOpen.value = false;
  void openQr(driver, mode);
}

/** 打开短信登录（光鸭盘）。 */
function openSms(_driver: Driver): void {
  loginOpen.value = false;
  smsOpen.value = true;
  smsPhone.value = '';
  smsCode.value = '';
  smsToken.value = '';
  smsError.value = '';
  smsOk.value = false;
}

async function onSendSms(): Promise<void> {
  const driver = loginDriver.value;
  if (!driver) return;
  smsSending.value = true;
  smsError.value = '';
  try {
    const res = await api.post<SmsSendResult>(`/drivers/${driver.id}/login/sms/send`, {
      phone: smsPhone.value,
    });
    if (res.captcha_url) {
      smsError.value = `需要人机验证，请先打开：${res.captcha_url}`;
      return;
    }
    smsToken.value = res.captcha_token;
    message.success('验证码已发送');
  } catch (e) {
    smsError.value = errMsg(e, '发送验证码失败');
  } finally {
    smsSending.value = false;
  }
}

async function onVerifySms(): Promise<void> {
  const driver = loginDriver.value;
  if (!driver) return;
  smsVerifying.value = true;
  smsError.value = '';
  try {
    const res = await api.post<SmsVerifyResult>(`/drivers/${driver.id}/login/sms/verify`, {
      phone: smsPhone.value,
      code: smsCode.value,
      captcha_token: smsToken.value,
    });
    if (!res.ok) {
      smsError.value = res.message || '验证失败';
      return;
    }
    smsOk.value = true;
    message.success('登录成功');
    // 凭据换了 → 本地旧状态作废（合并逻辑会保留较新的本地状态，
    // 这里必须显式丢掉，否则旧账号的 uid 会显示到后台重探完成）
    dropStatus(driver.id);
    await load();
    // 登录成功立刻后台测连 + 自动关闭短信弹框和登录选择框
    void probeDriver(driver.id, true);
    closeAfterDelay(() => {
      smsOpen.value = false;
      loginOpen.value = false;
    });
  } catch (e) {
    smsError.value = errMsg(e, '验证失败');
  } finally {
    smsVerifying.value = false;
  }
}

// -- 扫码 --

async function openQr(driver: Driver, mode: 'web' | 'client' = 'web'): Promise<void> {
  qrDriverId.value = driver.id;
  qrDriverType.value = driver.type;
  qrOpen.value = true;
  await startQrSession(driver, mode);
}

/**
 * 取码并开始轮询（弹框已打开时也可重复调用）。
 *
 * `mode=client` 只对光鸭有意义：服务端用**客户端 clientId** 签发 token，
 * 其 `aud` 决定能否换非会员大文件直链；scope 带 `offline` 因此有
 * `refresh_token`，后台可自行续期。
 */
async function startQrSession(driver: Driver, mode: 'web' | 'client' = 'web'): Promise<void> {
  qrMode.value = mode;
  qrSessionId.value = '';
  qrImage.value = '';
  qrKind.value = 'waiting';
  qrError.value = '';
  stopPolling();
  try {
    const url =
      mode === 'client'
        ? `/drivers/${driver.id}/qr/start?mode=client`
        : `/drivers/${driver.id}/qr/start`;
    const res = await api.post<QrStartResult>(url);
    qrSessionId.value = res.session_id;
    qrImage.value = res.qr_image_b64;
    startPolling();
  } catch (e) {
    qrError.value = errMsg(e, '获取二维码失败');
  }
}

/** 弹框内「重新获取二维码」：沿用同一身份，别偷偷退回 Web。 */
async function refreshQr(): Promise<void> {
  const driver = drivers.value.find((d) => d.id === qrDriverId.value);
  if (!driver) return;
  await startQrSession(driver, qrMode.value);
}

function startPolling(): void {
  stopPolling();
  pollTimer = window.setInterval(async () => {
    if (!qrDriverId.value || !qrSessionId.value) return;
    try {
      const res = await api.get<QrStatusResult>(
        `/drivers/${qrDriverId.value}/qr/status?session_id=${encodeURIComponent(qrSessionId.value)}`,
      );
      qrKind.value = res.kind;
      if (res.error_kind) {
        qrError.value = res.error_msg ?? res.error_kind;
        stopPolling();
        return;
      }
      if (res.confirmed) {
        stopPolling();
        qrKind.value = 'confirmed';
        message.success('扫码登录成功');
        // 凭据换了 → 本地旧状态作废（理由同 onVerifySms）
        const confirmedId = qrDriverId.value;
        if (confirmedId !== null) dropStatus(confirmedId);
        await load();
        // 登录成功立刻后台测连（刷新连通/容量/直链状态）
        const id = qrDriverId.value;
        if (id !== null) void probeDriver(id, true);
        // 停留 1.2 秒让用户看到成功提示，然后自动关闭
        closeAfterDelay(() => {
          qrOpen.value = false;
        });
      }
    } catch (e) {
      stopPolling();
      const status = e instanceof ApiError ? e.status : 0;
      if (status === 404) {
        qrKind.value = 'expired';
        qrError.value = '扫码会话已结束，请重新获取二维码';
      } else {
        qrError.value = errMsg(e, '查询扫码状态失败');
      }
    }
  }, 2000);
}

function stopPolling(): void {
  if (pollTimer !== undefined) {
    window.clearInterval(pollTimer);
    pollTimer = undefined;
  }
}

/**
 * 延时关闭弹框（登录成功后停留一下让用户看到成功提示）。
 *
 * **必须走这个而不是裸 setTimeout**：句柄要记下来，组件卸载时清掉——
 * 否则用户在 1.2 秒内切页，定时器仍会触发 → 写一个已卸载组件的 ref。
 */
let closeTimer: number | undefined;

function closeAfterDelay(fn: () => void, delay = 1200): void {
  if (closeTimer !== undefined) window.clearTimeout(closeTimer);
  closeTimer = window.setTimeout(() => {
    closeTimer = undefined;
    fn();
  }, delay);
}

onBeforeUnmount(() => {
  stopPolling();
  if (closeTimer !== undefined) {
    window.clearTimeout(closeTimer);
    closeTimer = undefined;
  }
});
</script>

<style scoped>
.identity-pick {
  margin-bottom: 12px;
}

.name-cell {
  display: flex;
  align-items: center;
  gap: 8px;
}

.status-hint {
  color: #999;
  font-size: 12px;
  margin-top: 2px;
  margin-left: 20px;
}

/* 陈旧的测连状态：缓存只在启动时刷新一次，超过阈值就该提示用户 */
.status-hint.stale {
  color: #d48806;
}

.capacity {
  margin-top: 4px;
  min-width: 170px;
}

.hint {
  color: #999;
  font-size: 12px;
  margin-top: 4px;
}

.unit {
  margin-left: 8px;
  color: #888;
}

.qr-box {
  text-align: center;
}

.qr-box img {
  width: 220px;
  height: 220px;
  image-rendering: pixelated;
}

.scan-log {
  margin-top: 12px;
  max-height: 260px;
  overflow-y: auto;
  background: #fafafa;
  border: 1px solid #f0f0f0;
  border-radius: 4px;
  padding: 8px 10px;
}

.log-line {
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}

/* 去重明细：默认折叠，展开后限高滚动 */
.dedup-details {
  margin-top: 8px;
}

.dedup-list {
  margin-top: 4px;
  max-height: 200px;
  overflow-y: auto;
  background: #fafafa;
  border: 1px solid #f0f0f0;
  border-radius: 4px;
  padding: 8px 10px;
}

.dedup-line {
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}

/* 「正在处理」可能是一条很长的网盘路径——单行省略，悬停看全文 */
.path-line {
  display: block;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  font-size: 12px;
}
</style>
