// H6 双轨门禁之二（eslint 轨）：网络 API 仅允许出现在 src/api-client/。
// 权威门禁是 `npm run lint:isolation`（ARCHITECTURE.md §5.2 的 grep 逐字实现）。
import pluginVue from 'eslint-plugin-vue'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  { ignores: ['dist/**', 'node_modules/**'] },
  ...tseslint.configs.recommended,
  ...pluginVue.configs['flat/essential'],
  {
    files: ['**/*.vue'],
    languageOptions: {
      parserOptions: { parser: tseslint.parser },
    },
  },
  {
    rules: {
      // 与 tsc noUnusedParameters 的下划线豁免口径对齐（保留参数占位以表达契约签名）
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
    },
  },
  {
    files: ['src/**/*.{ts,vue}'],
    ignores: ['src/api-client/**'], // api-client 是唯一网络出口（H6 豁免面）
    rules: {
      'no-restricted-syntax': [
        'error',
        { selector: "CallExpression[callee.name='fetch']", message: 'H6: 网络访问只能出现在 src/api-client/（UI 层不直接 fetch）' },
        { selector: "NewExpression[callee.name='EventSource']", message: 'H6: 网络访问只能出现在 src/api-client/（UI 层不直接开 SSE）' },
        { selector: "NewExpression[callee.name='XMLHttpRequest']", message: 'H6: 网络访问只能出现在 src/api-client/' },
        { selector: "NewExpression[callee.name='WebSocket']", message: 'H6: 网络访问只能出现在 src/api-client/' },
      ],
    },
  },
)
