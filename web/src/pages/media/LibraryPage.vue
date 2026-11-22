<template>
  <div class="page">
    <PageHeader
      eyebrow="媒体库 · MEDIA"
      title="媒体库"
      sub="STRM 条目——每个媒体条目一个 .strm，指向中转链接"
      compact
    >
      <template #actions>
        <a-input
          v-model:value="keyword"
          placeholder="搜索文件名 / 路径"
          allow-clear
          class="library-search"
        />
        <a-button size="small" :disabled="!selectedIds.length" @click="onResign">
          批量重签 <span v-if="selectedIds.length">({{ selectedIds.length }})</span>
        </a-button>
      </template>
    </PageHeader>

    <a-card size="small">
      <a-spin :spinning="loadingTabs">
        <a-tabs
          v-if="tabs.length"
          v-model:active-key="activeDriverId"
          size="small"
          @change="onTabChange"
        >
          <a-tab-pane v-for="t in tabs" :key="t.id" :tab="`${t.name} (${t.count})`">
            <DataTable
              :data-source="treeData"
              :columns="columns"
              row-key="key"
              size="small"
              table-layout="fixed"
              :loading="loadingTree"
              :row-selection="rowSelection"
              :pagination="false"
              :default-expand-all-rows="false"
              :expand-row-by-click="true"
              :locale="{ emptyText: '暂无媒体条目' }"
              class="lib-tree"
            >
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'name'">
                  <span>{{ record.name }}</span>
                  <span v-if="record.isDir" class="muted dir-count">
                    ({{ record.childCount }} 项)
                  </span>
                </template>
                <template v-else-if="column.key === 'size'">
                  <span class="muted">{{ formatBytes(record.size) }}</span>
                </template>
                <template v-else-if="column.key === 'strm_url'">
                  <a-tooltip v-if="record.strm_url" :title="record.strm_url">
                    <span class="mono truncate url">{{ record.strm_url }}</span>
                  </a-tooltip>
                  <span v-else class="muted">—</span>
                </template>
                <template v-else-if="column.key === 'action'">
                  <a-space v-if="!record.isDir">
                    <a-button size="small" @click="copyUrl(record.strm_url)">复制</a-button>
                    <a-button size="small" @click="onResignOne(record.id)">重签</a-button>
                  </a-space>
                </template>
              </template>
            </DataTable>
          </a-tab-pane>
        </a-tabs>
        <a-empty v-else description="暂无媒体条目，请先到「存储源」扫描生成" />
      </a-spin>
    </a-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { message } from 'ant-design-vue';
import { errMsg, mediaApi as api } from '../../api';
import type { Driver, MediaItem } from '../../types';
import { formatBytes } from '../../utils/format';
import DataTable from '../../components/DataTable.vue';
import PageHeader from '../../components/PageHeader.vue';

interface TreeNode {
  key: string;
  name: string;
  size: number;
  strm_url: string;
  isDir: boolean;
  childCount: number;
  children?: TreeNode[];
  id?: number;
}

interface Tab {
  id: number;
  name: string;
  count: number;
}

const tabs = ref<Tab[]>([]);
const activeDriverId = ref<number | null>(null);
const rawItems = ref<MediaItem[]>([]);
const keyword = ref('');
const loadingTabs = ref(false);
const loadingTree = ref(false);
const selectedIds = ref<number[]>([]);
const selectedKeys = ref<string[]>([]);

const columns = [
  { title: '名称', key: 'name', width: 360 },
  { title: '大小', key: 'size', width: 110 },
  { title: '中转 URL', key: 'strm_url', width: 420 },
  { title: '操作', key: 'action', width: 140 },
];

const PAGE = 500;

onMounted(loadTabs);

const rowSelection = computed(() => ({
  selectedRowKeys: selectedKeys.value,
  onChange: (keys: (string | number)[]) => {
    selectedKeys.value = keys.map(String);
    const ids = new Set<number>();
    const selected = new Set(selectedKeys.value);
    collectLeafIds(filteredTree.value, selected, ids);
    selectedIds.value = Array.from(ids);
  },
}));

const filteredTree = computed<TreeNode[]>(() => {
  const all = buildTree(rawItems.value);
  if (!keyword.value.trim()) return all;
  const kw = keyword.value.trim().toLowerCase();
  return filterTree(all, kw);
});

const treeData = computed<TreeNode[]>(() => filteredTree.value);

async function loadTabs(): Promise<void> {
  loadingTabs.value = true;
  try {
    const [drivers, stats] = await Promise.all([
      api.get<Driver[]>('/drivers'),
      api.get<{ per_driver: { driver_id: number; count: number }[] }>('/media/stats'),
    ]);
    const countMap = new Map<number, number>(stats.per_driver.map((p) => [p.driver_id, p.count]));
    const list: Tab[] = [];
    for (const d of drivers) {
      const c = countMap.get(d.id) ?? 0;
      if (c > 0) list.push({ id: d.id, name: d.name, count: c });
    }
    tabs.value = list;
    if (list.length) {
      const first = list[0];
      if (first) {
        activeDriverId.value = first.id;
        await loadTree(first.id);
      }
    } else {
      rawItems.value = [];
    }
  } catch (e) {
    message.error(errMsg(e, '加载失败'));
  } finally {
    loadingTabs.value = false;
  }
}

async function onTabChange(key: string | number): Promise<void> {
  const id = Number(key);
  activeDriverId.value = id;
  selectedIds.value = [];
  selectedKeys.value = [];
  keyword.value = '';
  await loadTree(id);
}

async function loadTree(driverId: number): Promise<void> {
  loadingTree.value = true;
  try {
    const all: MediaItem[] = [];
    let offset = 0;
    // 后端 limit 上限 500，循环拉取该 driver 全量条目
    for (;;) {
      const params = new URLSearchParams({
        driver_id: String(driverId),
        limit: String(PAGE),
        offset: String(offset),
      });
      const res = await api.get<{ total: number; items: MediaItem[] }>(
        `/media?${params.toString()}`,
      );
      all.push(...res.items);
      if (all.length >= res.total || res.items.length < PAGE) break;
      offset += PAGE;
    }
    rawItems.value = all;
    selectedIds.value = [];
    selectedKeys.value = [];
  } catch (e) {
    message.error(errMsg(e, '加载失败'));
  } finally {
    loadingTree.value = false;
  }
}

/** 按 path 的 '/' 切分建目录树。 */
function buildTree(items: MediaItem[]): TreeNode[] {
  const root: TreeNode = {
    key: '__root__',
    name: '',
    size: 0,
    strm_url: '',
    isDir: true,
    childCount: 0,
    children: [],
  };

  for (const item of items) {
    const parts = item.path.split('/').filter(Boolean);
    // 文件名优先用 name（更干净），目录段用 path 切出来的
    const lastPart = parts[parts.length - 1] ?? '';
    const filePart = item.name || lastPart || item.path;
    const dirParts = parts.slice(0, -1);

    let cur = root;
    let prefix = '';
    for (const p of dirParts) {
      prefix = prefix ? `${prefix}/${p}` : p;
      let child = cur.children?.find((c) => c.name === p && c.isDir);
      if (!child) {
        child = {
          key: `d:${prefix}`,
          name: p,
          size: 0,
          strm_url: '',
          isDir: true,
          childCount: 0,
          children: [],
        };
        cur.children?.push(child);
      }
      cur = child;
    }
    cur.children?.push({
      key: `f:${String(item.id)}`,
      name: filePart,
      size: item.size,
      strm_url: item.strm_url,
      isDir: false,
      childCount: 0,
      id: item.id,
    });
  }

  // 排序：目录在前、文件在后；同类型按名称排序
  const finalize = (node: TreeNode): void => {
    if (!node.children) return;
    node.children.sort((a, b) => {
      if (a.isDir !== b.isDir) return a.isDir ? -1 : 1;
      return a.name.localeCompare(b.name, 'zh');
    });
    let leafCount = 0;
    let totalSize = 0;
    for (const c of node.children) {
      finalize(c);
      leafCount += c.isDir ? c.childCount : 1;
      totalSize += c.size;
    }
    node.childCount = leafCount;
    node.size = totalSize;
  };
  finalize(root);
  return root.children ?? [];
}

/** 前端过滤树：保留命中文件 + 其祖先目录链。 */
function filterTree(nodes: TreeNode[], kw: string): TreeNode[] {
  const result: TreeNode[] = [];
  for (const n of nodes) {
    if (n.isDir) {
      const sub = n.children ? filterTree(n.children, kw) : [];
      if (sub.length) {
        result.push({ ...n, children: sub });
      }
    } else {
      const hay = `${n.name} ${n.strm_url}`.toLowerCase();
      // 用 path 也参与匹配，但文件节点没存 path；用 name + strm_url 已够
      if (hay.includes(kw)) {
        result.push(n);
      }
    }
  }
  return result;
}

function collectLeafIds(nodes: TreeNode[], selected: Set<string>, out: Set<number>): void {
  for (const n of nodes) {
    if (n.isDir) {
      // 目录被勾选时，收集其下所有文件 id
      if (selected.has(n.key)) {
        gatherFileIds(n, out);
      } else if (n.children) {
        collectLeafIds(n.children, selected, out);
      }
    } else if (n.id !== undefined && selected.has(n.key)) {
      out.add(n.id);
    }
  }
}

/** 收集某节点下所有文件叶子 id（目录被勾选时级联）。 */
function gatherFileIds(node: TreeNode, out: Set<number>): void {
  if (node.isDir) {
    if (node.children) {
      for (const c of node.children) gatherFileIds(c, out);
    }
  } else if (node.id !== undefined) {
    out.add(node.id);
  }
}

async function onResign(): Promise<void> {
  if (!selectedIds.value.length) return;
  try {
    const res = await api.post<{ resigned: number }>('/media/resign', {
      media_ids: selectedIds.value,
    });
    message.success(`已重签 ${String(res.resigned)} 条`);
    selectedIds.value = [];
    selectedKeys.value = [];
    if (activeDriverId.value !== null) await loadTree(activeDriverId.value);
  } catch (e) {
    message.error(errMsg(e, '重签失败'));
  }
}

async function onResignOne(id: number): Promise<void> {
  try {
    await api.post('/media/resign', { media_ids: [id] });
    message.success('已重签');
    if (activeDriverId.value !== null) await loadTree(activeDriverId.value);
  } catch (e) {
    message.error(errMsg(e, '重签失败'));
  }
}

async function copyUrl(url: string): Promise<void> {
  if (!url) {
    message.warning('无 URL');
    return;
  }
  try {
    await navigator.clipboard.writeText(url);
    message.success('已复制');
  } catch {
    message.error('复制失败');
  }
}
</script>

<style scoped>
.dir-count {
  margin-left: 6px;
}

.url {
  display: inline-block;
  max-width: 280px;
  color: #1677ff;
}

/* 搜索框：桌面 220px，手机收窄 */
.library-search {
  width: 220px;
  max-width: 100%;
}

@media (max-width: 767px) {
  .library-search {
    width: 100%;
  }
}

.lib-tree :deep(.ant-table-tbody > tr > td) {
  vertical-align: middle;
}
</style>
