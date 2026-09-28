import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import type { Pinia } from 'pinia'
import ElementPlus, { ElMessage } from 'element-plus'
import type { Mock } from 'vitest'

vi.mock('@/api/client', () => ({
  client: { post: vi.fn() },
}))

import { client } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import ReauthDialog from '@/components/ReauthDialog.vue'

const postMock = client.post as unknown as Mock

let pinia: Pinia

async function mountDialog(): Promise<VueWrapper> {
  const wrapper = mount(ReauthDialog, {
    props: { modelValue: true },
    global: { plugins: [ElementPlus, pinia] },
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

describe('ReauthDialog', () => {
  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
    vi.clearAllMocks()
  })

  it('空密码时确认按钮禁用', async () => {
    const wrapper = await mountDialog()

    expect(findButton(wrapper, '确认').attributes('disabled')).toBeDefined()
  })

  it('验证成功：调用 /auth/reauth、置位 store.reauthRecent、发出 success 并关闭', async () => {
    postMock.mockResolvedValue({ data: { reauth_recent: true } })
    const wrapper = await mountDialog()

    await wrapper.find('input').setValue('correct horse battery staple')
    await findButton(wrapper, '确认').trigger('click')
    await flushPromises()

    expect(postMock).toHaveBeenCalledWith('/auth/reauth', {
      password: 'correct horse battery staple',
    })
    expect(wrapper.emitted('success')).toHaveLength(1)
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([false])
    expect(useAuthStore().reauthRecent).toBe(true)
  })

  it('验证失败：提示 problem 中文消息、清空密码、保持打开、不发出 success', async () => {
    const errorSpy = vi.spyOn(ElMessage, 'error')
    postMock.mockRejectedValue({
      problem: {
        type: 'about:blank',
        title: 'Invalid Credentials',
        status: 401,
        code: 'auth.invalid_credentials',
        detail: '密码错误',
      },
    })
    const wrapper = await mountDialog()

    await wrapper.find('input').setValue('wrong-password')
    await findButton(wrapper, '确认').trigger('click')
    await flushPromises()

    expect(errorSpy).toHaveBeenCalledWith('密码错误')
    expect(wrapper.emitted('success')).toBeUndefined()
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    expect((wrapper.find('input').element as HTMLInputElement).value).toBe('')
    expect(useAuthStore().reauthRecent).toBe(false)
    errorSpy.mockRestore()
  })

  it('点击取消发出 cancel 并关闭，不发请求', async () => {
    const wrapper = await mountDialog()

    await findButton(wrapper, '取消').trigger('click')

    expect(wrapper.emitted('cancel')).toHaveLength(1)
    expect(wrapper.emitted('update:modelValue')).toEqual([[false]])
    expect(postMock).not.toHaveBeenCalled()
  })
})
