import js from '@eslint/js';
import pluginVue from 'eslint-plugin-vue';
import { withVueTs, vueTsConfigs } from '@vue/eslint-config-typescript';
import eslintConfigPrettier from 'eslint-config-prettier/flat';
import globals from 'globals';
import importX from 'eslint-plugin-import-x';
import promise from 'eslint-plugin-promise';
import vueA11y from 'eslint-plugin-vuejs-accessibility';

/**
 * ESLint 扁平配置（ESLint 10 + flat config）。
 *
 * 规则集来源：`docs/quality-gates.md` 的「工程代码规范方案」。
 *
 * - Vue：`pluginVue.configs['flat/recommended-error']`
 *   （已含 essential + strongly-recommended + recommended，统一 error）
 * - TypeScript：`vueTsConfigs.strictTypeChecked`（严格类型安全）
 *   + `vueTsConfigs.stylisticTypeChecked`（编码风格）
 * - 格式：交给 Prettier（`eslint-config-prettier` 必须放最后）
 *
 * ⚠️ 规则例外必须带**明确理由**（写在配置注释里），不允许为了消错
 *    批量关闭规则——那是把问题藏起来，不是修问题。
 */
export default withVueTs(
  {
    rootDir: import.meta.dirname,
    scriptLangs: ['ts'],
    tsSyntaxInTemplates: true,
  },

  {
    ignores: ['node_modules/**', 'dist/**', 'coverage/**', 'auto-imports.d.ts', 'components.d.ts'],
  },

  js.configs.recommended,

  // Vue 官方完整推荐规则，全部为 error
  pluginVue.configs['flat/recommended-error'],

  // TypeScript 严格类型规则
  vueTsConfigs.strictTypeChecked,

  // TypeScript 严格风格规则
  vueTsConfigs.stylisticTypeChecked,

  {
    rules: {
      // TypeScript 类型安全
      '@typescript-eslint/no-explicit-any': 'error',
      '@typescript-eslint/no-non-null-assertion': 'error',
      '@typescript-eslint/consistent-type-imports': 'error',

      // Promise 安全
      '@typescript-eslint/no-floating-promises': 'error',
      '@typescript-eslint/no-misused-promises': 'error',
      '@typescript-eslint/await-thenable': 'error',
      '@typescript-eslint/no-unnecessary-type-assertion': 'error',

      // 禁止绕过类型检查
      '@typescript-eslint/ban-ts-comment': [
        'error',
        {
          'ts-ignore': true,
          'ts-nocheck': true,
          'ts-expect-error': 'allow-with-description',
          minimumDescriptionLength: 10,
        },
      ],

      // JavaScript 基础规范
      'no-console': 'error',
      'no-debugger': 'error',
      'no-alert': 'error',
      eqeqeq: ['error', 'always'],
      curly: ['error', 'all'],
      'prefer-const': 'error',
      'no-duplicate-imports': 'error',
    },
  },

  {
    // ---- 三个「推荐」插件（方案 §2.2）----
    //
    // import-x：模块导入规范。**必须配 vite resolver**——本项目靠 Vite 的
    // `resolve.extensions`（['.vue', '.ts', '.js', '.json']）允许省略扩展名，
    // import-x 的默认 node resolver 不认这套规则，会把 39 处正常 import
    // 误报成「无法解析」。
    plugins: {
      'import-x': importX,
      promise,
      'vuejs-accessibility': vueA11y,
    },
    settings: {
      'import-x/resolver': {
        // 用 Vite 的解析规则——与构建产物一致，不会误报。
        // ⚠️ ESLint 的规则是同步的，用不了 Vite 的 ResolvedConfig API，
        // 所以这里手写一份**等价于 vite.config.ts 的 resolve 段**。
        // ⚠️ 扩展名列表必须与 vite.config.ts 一致，且**必须含 .vue**——
        // import-x 的默认列表里没有 .vue。
        vite: {
          viteConfig: {
            resolve: {
              extensions: ['.vue', '.ts', '.js', '.json'],
            },
          },
        },
        node: true,
      },
    },
    rules: {
      ...importX.configs.recommended.rules,
      ...promise.configs.recommended.rules,
      ...vueA11y.configs['flat/recommended'].rules,
    },
  },

  {
    // import-x 的解析器把 `@vitejs/plugin-vue` 解析到 **CJS 构建**
    // （dist/index.cjs）——而代码按 package 的 `exports` 映射引的是 **ESM 构建**
    // （dist/index.mjs）。规则读 CJS 那个文件认不出 default 导出 → 误报。
    // 只影响这一个构建配置文件，其余文件该规则照常生效。
    files: ['vite.config.ts'],
    rules: {
      'import-x/default': 'off',
    },
  },

  {
    // Node 环境：`scripts/*.mjs` 是**命令行工具**（CI 闸门脚本），
    // 它们的 stdout 就是产物——`console.log` 是业务输出，不是调试残留。
    // 同时补上 Node 全局（console / process），否则 `no-undef` 全报。
    files: ['scripts/**/*.mjs'],
    languageOptions: {
      globals: { ...globals.node },
    },
    rules: {
      'no-console': 'off',
    },
  },

  // 关闭与 Prettier 冲突的规则，必须放最后
  eslintConfigPrettier,
);
