import js from '@eslint/js';
import pluginVue from 'eslint-plugin-vue';
import { withVueTs, vueTsConfigs } from '@vue/eslint-config-typescript';
import eslintConfigPrettier from 'eslint-config-prettier/flat';
import globals from 'globals';

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
    ignores: [
      'node_modules/**',
      'dist/**',
      'coverage/**',
      'auto-imports.d.ts',
      'components.d.ts',
    ],
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
