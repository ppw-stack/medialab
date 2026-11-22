/**
 * 前端类型放置约束检查（Phase 4）。
 *
 * 背景：此前 20+ 个 `interface` 散落在各个 `.vue` 里，各写各的——
 * `SchedulerInfo` 在两处定义（字段还不一样），`JobStep` 在组件里定义
 * 却在别处重复。后端 DTO 改字段时前端不会报错（类型是手写的，
 * 与后端无关联），直到运行时才发现字段没了。
 *
 * 约定：**与后端 API 契约绑定的类型**统一放 `web/src/types.ts`；
 * **纯视图内部**的类型（Tile / Countdown / 格式化选项）留在组件里——
 * 它们不是 API 契约，放到共享文件反而是噪音。
 *
 * 独立 node 脚本（不依赖浏览器 / 容器）——CI 里能直接跑。
 *
 * 用法：node web/scripts/check-types-placement.mjs
 */

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');

/** 允许定义「视图内部类型」的文件白名单（这些类型不是 API 契约）。 */
const VIEW_LOCAL_ALLOWLIST = new Set([
  'utils/format.ts',
  // cron 构建器的字段状态（CronFields / CronFieldState）——**纯前端内部
  // 结构**（用户选预设或写自定义串），不是后端 DTO。
  'utils/cron.ts',
  'pages/TorrentIndex.vue',
  // 媒体库域：目录树节点（TreeNode）与 tab 描述（Tab）是**视图内部**
  // 结构——由 MediaItem 在前端组装而成，不是后端 DTO。
  'pages/media/Library.vue',
]);

/** 递归列出 src 下的 .ts / .vue 文件 */
function collectFiles(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) {
      out.push(...collectFiles(path));
    } else if (path.endsWith('.ts') || path.endsWith('.vue')) {
      out.push(path);
    }
  }
  return out;
}

const failures = [];

// ---- 1) types.ts 必须存在且导出接口类型
const typesSrc = readFileSync(join(SRC, 'types.ts'), 'utf-8');
if (!typesSrc.includes('export interface')) {
  failures.push('src/types.ts 没有导出任何 interface');
}

// ---- 2) 除白名单外，src 下不再有本地 interface 定义
for (const abs of collectFiles(SRC)) {
  const rel = relative(SRC, abs);
  if (rel === 'types.ts') continue;
  if (VIEW_LOCAL_ALLOWLIST.has(rel)) continue;
  const src = readFileSync(abs, 'utf-8');
  // 只看顶层定义（行首无缩进）
  if (/^(export )?interface \w+/m.test(src)) {
    failures.push(`src/${rel} 有本地 interface 定义（API 契约类型应放 src/types.ts）`);
  }
}

// ---- 3) 共享类型文件本身不重复定义同名 interface
{
  const names = [...typesSrc.matchAll(/^export interface (\w+)/gm)].map((m) => m[1]);
  const seen = new Set();
  for (const n of names) {
    if (seen.has(n)) failures.push(`src/types.ts 重复定义 interface ${n}`);
    seen.add(n);
  }
}

if (failures.length) {
  console.error('前端类型放置检查失败：');
  for (const f of failures) console.error('  - ' + f);
  process.exit(1);
}

console.log('前端类型放置检查通过');
