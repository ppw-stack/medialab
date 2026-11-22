/**
 * CSS class 定义检查。
 *
 * 背景：`544f8f9 feat(downloaders): 合并「下载器」页与「任务」页` 那个提交
 * 把 `Torrents.vue` 并入 `Downloaders.vue` 时，**只搬了 template + script，
 * 没搬 `<style scoped>`**——原文件里的 `.filter-row` / `.progress-cell` 等
 * 6 个 class 定义就此丢失。页面上表现为：筛选栏控件挤成一团、进度条与
 * 百分比上下堆叠。**没有任何闸门能发现这件事**——vue-tsc 不管 CSS，
 * oxlint 也不管 CSS。
 *
 * 约定：template 里用到的 class，必须在
 *   ① 本文件的 `<style>` 里，或
 *   ② `web/src/styles/global.css` 里
 * 有对应定义。否则视为「样式丢失」。
 *
 * 独立 node 脚本（不依赖浏览器 / 容器）——CI 里能直接跑。
 *
 * 用法：node web/scripts/check-css-classes.mjs
 */

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = join(ROOT, 'src');
const GLOBAL_CSS = join(SRC, 'styles', 'global.css');

/** 递归列出 src 下的 .vue 文件 */
function collectVue(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) {
      out.push(...collectVue(path));
    } else if (path.endsWith('.vue')) {
      out.push(path);
    }
  }
  return out;
}

/** 取出 <style> 块的内容 */
function styleBlocks(src) {
  return [...src.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join('\n');
}

/**
 * 取 template 部分（去掉 script / style）。
 *
 * 注意：Vue SFC 里 `<script>` 内的字符串可能含 `class="..."`（比如拼 HTML
 * 字符串），必须先剥掉，否则会误报。
 */
function templateOf(src) {
  return src.replace(/<script[\s\S]*?<\/script>/g, '').replace(/<style[\s\S]*?<\/style>/g, '');
}

/** 抽出一个 CSS 文件里定义的所有 class 名 */
function definedClasses(css) {
  // 只取「选择器位置」的 .name —— 排除小数（.5）、属性值里的内容
  const out = new Set();
  // 匹配选择器部分：{} 之前
  for (const m of css.matchAll(/([^{}]+)\{/g)) {
    const selector = m[1];
    // 去掉注释
    const clean = selector.replace(/\/\*[\s\S]*?\*\//g, '');
    for (const c of clean.matchAll(/\.(-?[_a-zA-Z][\w-]*)/g)) {
      out.add(c[1]);
    }
  }
  return out;
}

/**
 * 豁免名单：这些 class **有意**没有自己的样式规则。
 *
 * 两种情况：
 *   ① BEM 子元素——样式由父元素的规则覆盖（如 `.cron-preview__desc`
 *      继承 `.cron-preview` 的排版），不需要独立规则。
 *   ② 语义标记——只用来看源码/测试定位，视觉上完全由父元素决定。
 *
 * 键 = 相对 src 的文件路径，值 = 该文件里豁免的 class 名 → 豁免理由。
 * **加豁免必须写理由**——否则会变成「什么都往里塞」的垃圾桶。
 */
const EXEMPTIONS = {
  'components/CronBuilder.vue': {
    // .cron-preview 是 flex 列容器，__desc / __next 只是它的两个子块，
    // 排版（字号/颜色/间距）完全继承父元素，不需要独立规则。
    'cron-preview__desc': 'BEM 子元素，继承 .cron-preview',
    'cron-preview__next': 'BEM 子元素，继承 .cron-preview',
  },
  // 2026-11-22：`pages/settings/SchedulerTab.vue` 迁成了独立页面
  // `pages/SchedulerPage.vue`（定时任务菜单拆分），豁免要跟着文件走——
  // 否则检查脚本会把它当「样式丢失」报出来。
  'pages/SchedulerPage.vue': {
    // .run-chip 是 inline-flex 胶囊，__time 只是内部的时间文本
    // （.mono 字体由 global.css 提供），不需要独立规则。
    'run-chip__time': 'BEM 子元素，字体由 global.css 的 .mono 提供',
  },
};

/**
 * 抽出一个 template 里用到的 class 名。
 *
 * 处理三类写法：
 *   ① `class="a b"`            —— 静态
 *   ② `:class="'a'"` / `:class="{ x: 'a' }"` —— 动态里的字面量
 *   ③ `class="a"` 中的 `{{ }}` —— 插值部分跳过
 *
 * 跳过：
 *   - `:deep(.foo)` 里的 foo —— 那是**穿透**选择器，作用在子组件
 *     内部的 DOM 上，定义在别处（通常是 ant-design-vue 内部），不算缺失
 *   - 引号内不是纯标识符的片段（表达式、函数调用）
 */
function usedClasses(tpl) {
  const out = new Set();

  // ① 静态 class="..."
  //
  // 前面不能是 `:` —— 否则 `:class="..."` 的尾部会被误当成静态 class。
  for (const m of tpl.matchAll(/(?<![:\w-])class="([^"]*)"/g)) {
    // 去掉插值
    const raw = m[1].replace(/\{\{[^}]*\}\}/g, ' ');
    for (const c of raw.split(/\s+/)) {
      if (c) out.add(c);
    }
  }

  // ② / ③ 动态 :class="..." / :class='...'
  //
  // **只取**对象字面量的键值对形式：`{ 'a': cond }` 或 `{ 'a-b': cond }`。
  //
  // 不能简单地「取所有引号里的标识符」——`:class="freeEndChip(a, b).cls"`
  // 里的函数参数 `'free_end_at'` 会被误当成 class 名。
  // 判据：引号前必须是 `{` 或 `,`（后跟空白），即**键值对的值位置**。
  const kvLiteral = /[{,]\s*['"]([A-Za-z_][\w-]*)['"]\s*:/g;
  for (const m of tpl.matchAll(/\s:class=(?:"([^"]*)"|'([^']*)')/g)) {
    const expr = m[1] ?? m[2] ?? '';
    for (const s of expr.matchAll(kvLiteral)) {
      out.add(s[1]);
    }
  }

  return out;
}

/** 该 class 是否是 :deep() 穿透选择器的目标（不算缺失） */
function isDeepTarget(tpl, name) {
  return new RegExp(`:deep\\([^)]*\\.${name}\\b`).test(tpl);
}

const globalClasses = definedClasses(readFileSync(GLOBAL_CSS, 'utf-8'));

const failures = [];

for (const abs of collectVue(SRC)) {
  const rel = relative(SRC, abs);
  const src = readFileSync(abs, 'utf-8');
  const tpl = templateOf(src);
  const local = definedClasses(styleBlocks(src));
  const exempt = EXEMPTIONS[rel] ?? {};

  const missing = [];
  for (const name of usedClasses(tpl)) {
    if (local.has(name) || globalClasses.has(name)) continue;
    if (isDeepTarget(tpl, name)) continue;
    if (name in exempt) continue;
    missing.push(name);
  }

  if (missing.length) {
    failures.push({ rel, missing: missing.sort() });
  }
}

if (failures.length) {
  console.error('CSS class 定义检查失败（template 用了但没有样式定义）：');
  for (const f of failures) {
    console.error(`  - src/${f.rel}: ${f.missing.join(', ')}`);
  }
  console.error('');
  console.error('修法：在该文件的 <style> 里补上定义，或把它提到 styles/global.css。');
  process.exit(1);
}

console.log('CSS class 定义检查通过');
