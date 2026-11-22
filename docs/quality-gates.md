# 前端工程规范与质量门禁

**落地日期：** 2026-11-23
**适用范围：** `web/`（Vue 3 + TypeScript + Vite）
**来源：**《Vue 3 + TypeScript + Vite 工程代码规范方案》v1.0

---

## 1. 目标

AI 生成的代码不能只靠人工审查。**AI 可以自由完成业务实现，但不能自由降低
项目代码质量标准。** 所有代码必须通过统一的检查才能进入主分支。

| 原则 | 说明 |
|---|---|
| 强约束 | 所有核心质量规则用 error（`--max-warnings 0`，警告也不放过） |
| 自动化 | 能自动修复的问题不依赖人工 |
| 可验证 | 每条规则都有明确的执行命令 |
| 不绕过 | 不允许用关闭规则 / `eslint-disable` 掩盖错误 |
| 分层管理 | 每个工具负责独立的质量维度，不重复 |
| 持续集成 | CI 是最终质量门禁，本地 hook 只是第一道 |

---

## 2. 工具与职责

| 工具 | 职责 | 命令 |
|---|---|---|
| **ESLint 10** | 代码质量 + **类型感知**规则 | `npm run lint` |
| ├ `eslint-plugin-vue` | Vue 官方规则 | |
| ├ `@vue/eslint-config-typescript` | Vue + TS 集成（类型感知） | |
| ├ `typescript-eslint` | TS 严格规则 | |
| ├ `eslint-plugin-import-x` | 模块导入规范 | |
| ├ `eslint-plugin-promise` | Promise 使用规范 | |
| ├ `eslint-plugin-vuejs-accessibility` | Vue 无障碍规范 | |
| ├ `eslint-import-resolver-vite` | 让 import-x 认 Vite 的解析规则 | |
| **Prettier 3** | 代码格式统一 | `npm run format:check` |
| **vue-tsc** | Vue SFC 类型检查 | `npm run type-check` |
| **Husky + lint-staged** | 提交前只查暂存文件 | `pre-commit` 钩子 |
| **GitHub Actions** | 最终质量门禁 | `.github/workflows/ci.yml` |

**为什么从 oxlint 换成 ESLint**：oxlint 是 Rust 引擎的纯语法检查器，快但
**没有类型信息**，拿不到 `no-floating-promises`（未处理的 Promise）、
`no-unnecessary-condition`（多余的 null 判定）、`prefer-nullish-coalescing`
这类「真 bug 探测器」。前端 38 个文件规模不大，类型感知规则的收益远大于
那几秒差异。

**为什么用 npm 而不是 pnpm**：本仓库的锁文件、CI（`npm ci`）与 Dockerfile
（`npm ci --no-audit --no-fund`）都是 npm 路径。方案原文的 pnpm 是示例项目
的默认值，不适用于本仓库——**改包管理器不在本次规范落地的范围内**。

---

## 3. ESLint 规则集

配置文件：`web/eslint.config.mjs`（flat config）

```js
pluginVue.configs['flat/recommended-error']   // Vue 官方完整推荐，全部 error
vueTsConfigs.strictTypeChecked                // TypeScript 严格类型安全
vueTsConfigs.stylisticTypeChecked             // TypeScript 编码风格

// 三个「推荐」插件（方案 §2.2）
importX.configs.recommended.rules             // 模块导入规范
promise.configs.recommended.rules             // Promise 使用规范
vueA11y.configs['flat/recommended'].rules      // Vue 无障碍规范
```

### 3.1 import-x 必须配 Vite resolver

本项目靠 Vite 的 `resolve.extensions`（`['.vue', '.ts', '.js', '.json']`）**允许
省略扩展名**（`'../utils/cron'` 实际是 `../utils/cron.ts`）。import-x 的默认
node resolver 不认这套规则，会把 **39 处正常 import** 误报成「无法解析」。

所以配了 `eslint-import-resolver-vite`：

```js
settings: {
  'import-x/resolver': {
    vite: {
      viteConfig: {
        resolve: { extensions: ['.vue', '.ts', '.js', '.json'] },
      },
    },
    node: true,
  },
},
```

⚠️ ESLint 的规则是**同步**的，用不了 Vite 的 `ResolvedConfig` API（异步），
所以这里是手写一份**等价于 `vite.config.ts` 的 resolve 段**——**改 vite 的
resolve 配置时这里要同步改**。

⚠️ 扩展名列表**必须含 `.vue`**——import-x 自己的默认列表里没有。

### 3.2 明确禁止

| 检查项 | 规则 |
|---|---|
| 显式 any | `@typescript-eslint/no-explicit-any` |
| 非空断言 `!` | `@typescript-eslint/no-non-null-assertion` |
| `@ts-ignore` / `@ts-nocheck` | `@typescript-eslint/ban-ts-comment` |
| `@ts-expect-error` | 必须带 ≥10 字说明 |
| 未使用变量 / 导入 | `@typescript-eslint/no-unused-vars` / `no-duplicate-imports` |
| 未处理的 Promise | `@typescript-eslint/no-floating-promises` |
| `console` / `alert` / `debugger` | `no-console` / `no-alert` / `no-debugger` |
| 宽松相等 | `eqeqeq` |
| 隐式 any / 不安全类型操作 | `strictTypeChecked` 全套 |

### 3.3 例外规则

**例外必须带明确理由**（写在配置注释里），不允许为了消错批量关闭规则。

当前仅有的两处例外，都在 `eslint.config.mjs` 里写明了理由：

1. **`scripts/**/*.mjs` 放行 `no-console`** —— 这两个脚本是 **CI 闸门工具**
   （`check-types-placement.mjs` / `check-css-classes.mjs`），它们的 stdout
   就是产物，`console.log` 是业务输出而不是调试残留。同时补上 Node 全局
   （`globals.node`），否则 `console` / `process` 会被 `no-undef` 报出来。

2. **`import-x/default` 对 `vite.config.ts` 关闭** —— import-x 的解析器把
   `@vitejs/plugin-vue` 解析到 **CJS 构建**（`dist/index.cjs`），而代码按
   package 的 `exports` 映射引的是 **ESM 构建**（`dist/index.mjs`）。规则读
   CJS 那个文件认不出 default 导出 → 误报。只影响这一个构建配置文件，
   其余文件该规则照常生效。

3. **模板串里的数字必须显式 `String()`** —— `restrict-template-expressions`
   是 `strictTypeChecked` 自带的规则，不是我们加的。代价是 139 处模板串要
   包一层 `String()`；收益是「`${count}` 到底是数字还是字符串」在代码里
   一目了然。

---

## 4. Prettier

配置文件：`web/.prettierrc.json`（singleQuote / printWidth 100 /
trailingComma all / semi / arrowParens always）。

**Prettier 只负责格式，不负责质量判断**；`eslint-config-prettier` 放在
flat config 的**最后**（关闭与 Prettier 冲突的 ESLint 格式规则）。

`.prettierignore` 忽略了 `package-lock.json` —— prettier 会遵守 ignore 文件，
即使显式传路径也会跳过，所以 `npm run format` 不会重排 npm 生成的锁文件。

---

## 5. TypeScript 严格模式

`web/tsconfig.app.json` 与 `web/tsconfig.node.json` 都开满：

```jsonc
{
  "strict": true,
  "noImplicitAny": true,
  "strictNullChecks": true,
  "strictFunctionTypes": true,
  "strictPropertyInitialization": true,
  "noImplicitReturns": true,
  "noFallthroughCasesInSwitch": true,
  "noUncheckedIndexedAccess": true,
  "exactOptionalPropertyTypes": true,
  "allowUnreachableCode": false
}
```

> ⚠️ 本项目在 2026-11-23 之前**根本没开 `strict`**——那意味着 `null` /
> `undefined` 在类型层面完全不可见。开启后一次性暴露 36 个类型错误，
> 已全部修完。

### 5.1 与后端契约的关系

`web/src/types.ts` 里的类型是**手写的后端 DTO 镜像**，与后端 Pydantic 模型
必须一致。`no-unnecessary-condition` 报的「多余的 null 判定」，多数是
**类型在说谎**或**防御写多了**：

- 后端保证非空（`Mapped[str]` NOT NULL、响应体恒带该字段）→ 删掉前端的
  `?? ''` / `?? []` 防御。**先核对后端再删**（`app/db/models.py` 与
  `app/api/routes/`）。
- 前端运行时确实可能拿到 null（如 `a-input-number` 清空时 v-model 写
  null）→ **改类型**，不要删防御。

---

## 6. Git Hooks

装在 `web/.husky/`，由 `web/package.json` 的 `prepare` 脚本注册。

```
web/package.json  "prepare": "cd .. && husky web/.husky"
```

> ⚠️ **husky 必须在 git 仓库根运行**——它找的是当前工作目录下的 `.git`，
> 而 `web/` 里没有。故 prepare 脚本先 `cd ..`，并把 hooksPath 设成
> `web/.husky/_`（相对仓库根，git 会在工作树顶层解析）。
>
> Git hook 的 cwd 是**仓库根**，所以 hook 脚本内部要 `cd web` 再跑命令。

| 钩子 | 内容 |
|---|---|
| `pre-commit` | `cd web && npx lint-staged` —— 只检查本次暂存文件 |
| `pre-push` | `cd web && npm run check` —— 全量质量检查 |

**hook 不是唯一保障**：CI 才是最终门禁（hook 可以被 `--no-verify` 跳过）。

### 6.1 Docker 构建

Dockerfile 的 `npm ci` 加 `HUSKY=0`——构建上下文里没有 `.git`，装 hook
没有意义。（不加也不会失败，husky 找不到 `.git` 时返回提示并以 0 退出，
但输出是噪音。）

---

## 7. 命令

```bash
cd web

npm run dev            # 本地开发
npm run build          # vue-tsc -b && vite build
npm run lint           # eslint . --max-warnings 0
npm run lint:fix       # eslint . --fix
npm run format         # prettier --write .
npm run format:check   # prettier --check .
npm run type-check     # vue-tsc -b
npm run check          # lint && format:check && type-check（提交前跑这个）

# 两条项目自研的存量闸门（与本规范并行，独立 node 脚本）
node scripts/check-types-placement.mjs   # API 契约类型集中在 src/types.ts
node scripts/check-css-classes.mjs      # template 用到的 class 有样式定义
```

---

## 8. CI 门禁

`.github/workflows/ci.yml` 的 `frontend` job，5 步全绿才允许合并：

1. `npm run build`（vue-tsc 类型检查 + 构建）
2. `node scripts/check-types-placement.mjs`
3. `node scripts/check-css-classes.mjs`
4. `npm run lint`
5. `npm run format:check`

---

## 9. 质量门禁标准

| 检查项 | 合格标准 |
|---|---|
| ESLint errors | 0 |
| ESLint warnings | 0（`--max-warnings 0`） |
| TypeScript errors | 0 |
| Prettier 格式问题 | 0 |
| 未处理 Promise | 0 |
| 显式 any | 0 |
| 禁止的 TS 注释 | 0 |
| Build errors | 0 |
| CI failures | 0 |

### 执行层级

| 阶段 | 检查内容 | 失败处理 |
|---|---|---|
| 编辑器保存 | Prettier + ESLint 自动修复 | 提示 |
| Git Commit | lint-staged（只查暂存文件） | 禁止提交 |
| Git Push | 全量 `npm run check` | 禁止推送 |
| CI | ESLint + Prettier + 类型 + 构建 | 禁止合并 |

---

## 10. AI 编码行为约束

**AI 不允许自行修改工程质量配置来规避检查。** 如确实需要修改规范，应当先
说明原因并获得项目维护者认可。

- 遵守现有架构与约定，不引入不必要的依赖
- 不重复造轮子，不留无用代码，不留调试语句
- 严格 TypeScript：不用 `any` 绕过检查、不用 `@ts-ignore` / `@ts-nocheck`、
  不用非空断言、不做掩盖真实类型问题的断言
- Vue 3 Composition API + `<script setup lang="ts">`；Props / Emits 显式声明；
  派生状态优先用 `computed`；清理事件监听与副作用
- 改完代码跑 `npm run lint` + `npm run type-check`；收尾前跑 `npm run format`
  与 `npm run build`
- **修错误而不是压制错误**；检查没过就**不许声称完成**
- 汇报时列出：改了哪些文件、跑了哪些检查、还有哪些未解决

---

## 11. 维护

- 升级 ESLint / eslint-plugin-vue / TypeScript 时，必须**重新执行完整检查**
  （规则集会随版本变化，可能冒出新错误）
- 新增 ESLint 规则时：先在存量代码上跑一遍，看错误量是否可接受；超过 ~20 个
  要逐条评估，不要直接批量关闭规则
- 本文件与 `AGENTS.md` 的「前端检查」章节要同步更新
