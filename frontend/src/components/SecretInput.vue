<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'

/**
 * 密文输入框：默认以密码态展示，带「显示/隐藏」切换与「复制」按钮。
 * 用于展示或录入令牌、密钥等敏感值。
 */
const props = withDefaults(
  defineProps<{
    /** 输入值（v-model）。 */
    modelValue: string
    /** 占位提示文案。 */
    placeholder?: string
  }>(),
  { placeholder: '' },
)

const emit = defineEmits<{
  /** 同步 v-model 输入值。 */
  (e: 'update:modelValue', value: string): void
}>()

/** 是否明文显示。 */
const revealed = ref(false)

function onInput(value: string): void {
  emit('update:modelValue', value)
}

/** 复制当前值到剪贴板（空值不动作）。 */
async function copyValue(): Promise<void> {
  if (!props.modelValue) {
    return
  }
  try {
    await navigator.clipboard.writeText(props.modelValue)
    ElMessage.success('已复制到剪贴板')
  } catch {
    ElMessage.error('复制失败，请手动选择复制')
  }
}
</script>

<template>
  <el-input
    :model-value="modelValue"
    :type="revealed ? 'text' : 'password'"
    :placeholder="placeholder"
    autocomplete="off"
    spellcheck="false"
    @update:model-value="onInput"
  >
    <template #suffix>
      <el-icon
        class="secret-input__btn"
        :title="revealed ? '隐藏' : '显示'"
        @click="revealed = !revealed"
      >
        <Hide v-if="revealed" />
        <View v-else />
      </el-icon>
      <el-icon class="secret-input__btn" title="复制" @click="copyValue">
        <CopyDocument />
      </el-icon>
    </template>
  </el-input>
</template>

<style scoped>
.secret-input__btn {
  margin-left: 6px;
  color: var(--el-text-color-secondary);
  cursor: pointer;
}

.secret-input__btn:hover {
  color: var(--el-color-primary);
}
</style>
