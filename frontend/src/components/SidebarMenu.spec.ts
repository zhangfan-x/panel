import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import type { Router } from 'vue-router'
import ElementPlus from 'element-plus'

import SidebarMenu from '@/components/SidebarMenu.vue'

function createTestRouter(): Router {
  return createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/:all(.*)*', component: { template: '<div />' } }],
  })
}

describe('SidebarMenu', () => {
  it('渲染全部一级菜单与 Docker 子菜单', async () => {
    const router = createTestRouter()
    await router.push('/dashboard')
    await router.isReady()

    const wrapper = mount(SidebarMenu, {
      global: { plugins: [ElementPlus, router] },
    })

    const text = wrapper.text()
    for (const label of [
      '仪表盘',
      '进程',
      '服务',
      '文件',
      'Docker',
      '任务',
      '终端',
      '审计',
      '设置',
    ]) {
      expect(text).toContain(label)
    }
    // Docker 子菜单项
    expect(text).toContain('容器')
    expect(text).toContain('Compose')
    // 品牌名
    expect(text).toContain('mypanel')
  })
})
