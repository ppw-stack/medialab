/**
 * 「目标子目录」浏览器（**网盘聚合模式**）。
 *
 * ## 为什么需要它
 *
 * 「本地同步 → 网盘聚合」的目标是**一个聚合组**：文件先落到组内某个副盘
 * 的缓冲根，再由归拢作业搬到主盘的「归拢目标目录」。
 *
 * 目录映射（主盘落点 + 各副盘缓冲根）是**组级配置**——在「网盘聚合」页
 * 配一次即可。但「这个任务要传到 `电影/外语`、那个任务传到 `电视剧/国产`」
 * 是**任务级**的需求，故这里提供任务级的**子路径覆盖**。
 *
 * ## 语义
 *
 * 子目录是**主盘归拢目标目录下的相对路径**。浏览器的起点就是归拢目标
 * 目录，面包屑去掉起点后剩下的段即相对路径。
 *
 * ⚠️ **相对路径取目录「名」而不是 id**——云端路径由目录名拼出（光鸭的
 * fileId 是 19 位数字，与路径无关）。
 */
import { ref } from 'vue';
import { message } from 'ant-design-vue';
import { errMsg, mediaApi as api } from '../api';
import type { AggregateTargetGroup, DirEntry } from '../types';

export function useSubPathPicker(group: () => AggregateTargetGroup | undefined) {
  /** 浏览器弹框是否打开 */
  const subPathOpen = ref(false);
  /** 面包屑（第一项恒为「归拢目标目录」——即起点） */
  const subPathStack = ref<DirEntry[]>([]);
  /** 当前层的子目录 */
  const subPathDirs = ref<DirEntry[]>([]);
  const loadingSubPath = ref(false);

  /** 列出某个目录下的子目录（主盘 = 读能力，不校验是否支持上传）。 */
  async function loadSubPathDirs(parentId: string): Promise<void> {
    const g = group();
    if (!g) return;
    loadingSubPath.value = true;
    try {
      const resp = await api.get<{ dirs: DirEntry[] }>(
        `/drivers/${String(g.master_driver_id)}/browse-dirs?parent_id=${encodeURIComponent(parentId)}`,
      );
      subPathDirs.value = resp.dirs;
    } catch (e) {
      message.error(errMsg(e, '读取主盘目录失败'));
      subPathDirs.value = [];
    } finally {
      loadingSubPath.value = false;
    }
  }

  /** 打开浏览器（起点 = 主盘归拢目标目录）。 */
  function openSubPathPicker(): void {
    const g = group();
    if (!g) return;
    if (!g.target_dir_id) {
      message.warning('该聚合组未配置「归拢目标目录」——请先到「网盘聚合」页配置');
      return;
    }
    subPathStack.value = [
      { id: g.target_dir_id, name: (g.target_dir_path ?? '') || '归拢目标目录' },
    ];
    subPathDirs.value = [];
    subPathOpen.value = true;
    void loadSubPathDirs(g.target_dir_id);
  }

  /** 进入子目录（面包屑往下钻）。 */
  function openSubPath(dir: DirEntry): void {
    subPathStack.value = [...subPathStack.value, dir];
    void loadSubPathDirs(dir.id);
  }

  /** 面包屑回退。 */
  function goSubPath(index: number): void {
    subPathStack.value = subPathStack.value.slice(0, index + 1);
    const cur = subPathStack.value[subPathStack.value.length - 1];
    if (cur) void loadSubPathDirs(cur.id);
  }

  /**
   * 确认：把面包屑（**去掉起点**）转成相对子路径。
   *
   * 停在起点 = 空串 = 不覆盖（直接落在归拢目标目录根）。
   */
  function applySubPath(): string {
    subPathOpen.value = false;
    return subPathStack.value
      .slice(1)
      .map((e) => e.name)
      .filter(Boolean)
      .join('/');
  }

  return {
    subPathOpen,
    subPathStack,
    subPathDirs,
    loadingSubPath,
    openSubPathPicker,
    openSubPath,
    goSubPath,
    applySubPath,
  };
}
