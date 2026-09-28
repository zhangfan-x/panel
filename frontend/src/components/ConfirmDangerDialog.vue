<script setup lang="ts">
import { computed, ref, watch } from 'vue'

/**
 * 危险操作确认对话框：要求用户原样输入资源名后才能点击确认，防止误操作。
 *
 * 约定：点击确认只发出 confirm 事件，对话框不会自行关闭——调用方在异步操作
 * 期间把 loading 置 true，完成后再把 v-model 置 false 关闭。
 */
const props = withDefaults(
  defineProps<{
    /** 对话框可见性（v-model）。 */
    modelValue: boolean
    /** 对话框标题（如「删除容器」）。 */
    title: string
    /** 需要用户原样输入以启用确认按钮的资源名。 */
    resourceName: string
    /** 警示文案，说明该操作的后果（如「删除后数据不可恢复」）。 */
    message?: string
    /** 确认按钮的 loading 态，由调用方在执行异步操作时置 true。 */
    loading?: boolean
  }>(),
  { message: '', loading: false },
)

const emit = defineEmits<{
  /** 同步 v-model 可见性（关闭按钮 / ESC 也会触发 false）。 */
  (e: 'update:modelValue', value: boolean): void
  /** 用户已输入匹配的资源名并点击确认；对话框保持打开，由调用方善后。 */
  (e: 'confirm'): void
  /** 用户点击取消按钮（随后会同步 v-model 为 false）。 */
  (e: 'cancel'): void
}>()

const input = ref('')

// 每次打开时清空上次输入，避免残留输入导致误确认。
watch(
  () => props.modelValue,
  (open) => {
    if (open) {
      input.value = ''
    }
  },
)

/** 输入与资源名完全一致时才允许确认。 */
const canConfirm = computed(() => input.value === props.resourceName)

function onVisibleChange(value: boolean): void {
  emit('update:modelValue', value)
}

function handleCancel(): void {
  emit('cancel')
  emit('update:modelValue', false)
}

function handleConfirm(): void {
  if (!canConfirm.value) {
    return
  }
  emit('confirm')
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="title"
    width="460px"
    :close-on-click-modal="false"
    @update:model-value="onVisibleChange"
  >
    <el-alert
      v-if="message"
      type="warning"
      :title="message"
      :closable="false"
      show-icon
      class="danger-dialog__alert"
    />
    <p class="danger-dialog__prompt">
      请输入
      <code class="danger-dialog__name">{{ resourceName }}</code>
      以确认操作：
    </p>
    <el-input
      v-model="input"
      :placeholder="resourceName"
      autocomplete="off"
      spellcheck="false"
      @keyup.enter="handleConfirm"
    />
    <template #footer>
      <el-button @click="handleCancel">取消</el-button>
      <el-button type="danger" :disabled="!canConfirm" :loading="loading" @click="handleConfirm">
        确认
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.danger-dialog__alert {
  margin-bottom: 12px;
}

.danger-dialog__prompt {
  margin: 0 0 8px;
  color: var(--el-text-color-regular);
  font-size: 14px;
}

.danger-dialog__name {
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--el-fill-color-light);
  color: var(--el-color-danger);
  font-weight: 600;
  user-select: all;
}
</style>
