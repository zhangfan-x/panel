import { describe, expect, it } from 'vitest'
import { nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'

import ConfirmDangerDialog from '@/components/ConfirmDangerDialog.vue'

async function mountDialog(): Promise<VueWrapper> {
  const wrapper = mount(ConfirmDangerDialog, {
    props: {
      modelValue: true,
      title: '删除容器',
      resourceName: 'web-1',
      message: '删除后数据不可恢复',
    },
    global: { plugins: [ElementPlus] },
  })
  // el-dialog 在 onMounted 里才置 rendered，等一拍让内容渲染出来。
  await nextTick()
  return wrapper
}

function findButton(wrapper: VueWrapper, text: string) {
  const btn = wrapper.findAll('button').find((b) => b.text() === text)
  if (!btn) {
    throw new Error(`未找到按钮：${text}`)
  }
  return btn
}

describe('ConfirmDangerDialog', () => {
  it('输入资源名前确认按钮禁用，输入一致后启用并发出 confirm（且不自行关闭）', async () => {
    const wrapper = await mountDialog()
    const confirmBtn = findButton(wrapper, '确认')

    expect(confirmBtn.attributes('disabled')).toBeDefined()

    await wrapper.find('input').setValue('web-1')
    expect(confirmBtn.attributes('disabled')).toBeUndefined()

    await confirmBtn.trigger('click')
    expect(wrapper.emitted('confirm')).toHaveLength(1)
    // 确认后对话框不自行关闭，由调用方在操作完成后关闭。
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  })

  it('输入不匹配时点击确认不发出 confirm', async () => {
    const wrapper = await mountDialog()

    await wrapper.find('input').setValue('web-2')
    await findButton(wrapper, '确认').trigger('click')

    expect(wrapper.emitted('confirm')).toBeUndefined()
  })

  it('点击取消发出 cancel 并把 v-model 置为 false', async () => {
    const wrapper = await mountDialog()

    await findButton(wrapper, '取消').trigger('click')

    expect(wrapper.emitted('cancel')).toHaveLength(1)
    expect(wrapper.emitted('update:modelValue')).toEqual([[false]])
  })

  it('重新打开时清空上次输入', async () => {
    const wrapper = await mountDialog()

    await wrapper.find('input').setValue('web-1')
    await wrapper.setProps({ modelValue: false })
    await wrapper.setProps({ modelValue: true })

    expect((wrapper.find('input').element as HTMLInputElement).value).toBe('')
    expect(findButton(wrapper, '确认').attributes('disabled')).toBeDefined()
  })

  it('渲染标题、警示文案与资源名提示', async () => {
    const wrapper = await mountDialog()

    const text = wrapper.text()
    expect(text).toContain('删除容器')
    expect(text).toContain('删除后数据不可恢复')
    expect(text).toContain('web-1')
  })
})
