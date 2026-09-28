import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    proxy: {
      // 后端在中间件里按配置的管理地址校验 Host，因此代理必须用 changeOrigin: true
      // 把请求 Host 改写为目标（127.0.0.1:25443），否则会被判为 request.bad_host。
      '/api': {
        target: 'http://127.0.0.1:25443',
        changeOrigin: true,
      },
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) {
            return undefined
          }
          // Element Plus 全量引入，单独分包，避免与业务代码混在主 chunk。
          if (id.includes('element-plus') || id.includes('@element-plus')) {
            return 'element-plus'
          }
          // 重型依赖按路由懒加载 + 单独分包（页面里 import 时才进入产物）：
          if (id.includes('echarts') || id.includes('vue-echarts')) {
            return 'echarts'
          }
          if (id.includes('monaco-editor')) {
            return 'monaco'
          }
          if (id.includes('@xterm')) {
            return 'xterm'
          }
          return undefined
        },
      },
    },
  },
})
