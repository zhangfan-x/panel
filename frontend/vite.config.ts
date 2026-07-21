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
          // 后续里程碑拆包预留（M1 未在页面引入这些依赖，暂不会进入产物）：
          //   echarts / vue-echarts → 'echarts'（M2 监控图表，按路由懒加载）
          //   monaco-editor         → 'monaco'（M3 文件在线编辑，按路由懒加载）
          //   @xterm/xterm          → 'xterm'（M5 root 终端，按路由懒加载）
          return undefined
        },
      },
    },
  },
})
