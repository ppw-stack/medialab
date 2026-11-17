# Vue + TypeScript + Vite

This template uses Vue 3 SFC (`<script setup lang="ts">`), Ant Design Vue 4, Vue Router 4, and Pinia.

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
npm run lint
node scripts/check-types-placement.mjs
node scripts/check-css-classes.mjs
```

本项目不保留 e2e 测试（原 Playwright e2e 依赖真实下载器与站点，已移除）。
