import pluginVue from 'eslint-plugin-vue'
import { defineConfigWithVueTs, vueTsConfigs } from '@vue/eslint-config-typescript'
import skipFormatting from '@vue/eslint-config-prettier/skip-formatting'

// Flat config：Vue 3 + typescript-eslint + Prettier 兼容（Prettier 只负责格式化，
// skipFormatting 关闭所有与 Prettier 冲突的排版类规则，避免 lint 与 format 打架）。
export default defineConfigWithVueTs(
  {
    name: 'app/files',
    files: ['**/*.{ts,mts,tsx,vue}'],
  },
  {
    name: 'app/ignores',
    ignores: ['**/dist/**', '**/node_modules/**', '**/coverage/**', 'src/api/schema.d.ts'],
  },
  pluginVue.configs['flat/essential'],
  vueTsConfigs.recommended,
  skipFormatting,
)
