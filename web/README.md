# Vue + TypeScript + Vite

Vue 3 SFC（`<script setup lang="ts">`）+ Ant Design Vue 4 + Vue Router 4 + Pinia。

## Develop

```bash
npm run dev
```

## Build

```bash
npm run build
```

## Check

```bash
npm run check          # lint + format:check + type-check（提交前跑这个）
npm run lint          # ESLint（--max-warnings 0）
npm run lint:fix      # ESLint 自动修复
npm run format        # Prettier 格式化
npm run format:check  # Prettier 格式检查
npm run type-check    # vue-tsc 类型检查
node scripts/check-types-placement.mjs   # API 契约类型必须集中在 src/types.ts
node scripts/check-css-classes.mjs        # template 用到的 class 必须有样式定义
```

工程规范见仓库根目录 `docs/quality-gates.md`。

## Git Hooks

Husky + lint-staged（装在 `web/.husky/`，由 `web/package.json` 的 `prepare`
脚本注册，hooksPath 指向 `web/.husky/_`）：

- `pre-commit`：只检查本次暂存文件（`eslint --fix` + `prettier --write`）
- `pre-push`：跑全量 `npm run check`

本项目不保留 e2e 测试（原 Playwright e2e 依赖真实下载器与站点，已移除）。
