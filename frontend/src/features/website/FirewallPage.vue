<script setup lang="ts">
// 防火墙（ufw）可视化管理：状态开关、默认策略、规则增删、常用端口快捷放行。
//
// 自锁保护：SSH 端口与面板端口被标为「受保护」。启用防火墙、删除会波及它们的
// 规则、或收紧入站默认策略时，后端会返回 409；此时弹出风险确认框，用户确认后
// 才带 confirm_lockout 重试。删除还会带上规则指纹，避免 ufw 重排序号后误删。
import { computed, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { useQuery } from '@tanstack/vue-query'

import type { ApiError } from '@/api/client'
import {
  addFirewallRule,
  deleteFirewallRule,
  disableFirewall,
  enableFirewall,
  getFirewallStatus,
  reloadFirewall,
  ruleFingerprint,
  setFirewallDefault,
} from '@/api/firewall'
import type { AddRuleRequest, FirewallRule } from '@/api/firewall'
import { problemMessage } from '@/api/problem'
import ReauthDialog from '@/components/ReauthDialog.vue'

const { data, isPending, isError, error, refetch } = useQuery({
  queryKey: ['firewall-status'],
  queryFn: getFirewallStatus,
})

const errorMessage = computed(() => problemMessage(error.value, '获取防火墙状态失败'))
const status = computed(() => data.value)
const rules = computed(() => data.value?.rules ?? [])
const protectedPorts = computed(() => new Set(data.value?.protected_ports ?? []))
const uncovered = computed(() => data.value?.uncovered_ports ?? [])

const busy = ref(false)
const reauthVisible = ref(false)
/** 重认证成功后要重放的操作。 */
let pendingAction: (() => Promise<void>) | null = null

/** 风险确认框状态：后端 409 时填充。 */
const riskVisible = ref(false)
const riskMessage = ref('')
let riskAction: ((confirmed: boolean) => Promise<void>) | null = null

function problemCode(err: unknown): string | undefined {
  return (err as ApiError | undefined)?.problem?.code
}

/**
 * 统一执行一个防火墙变更：
 * - 409（自锁风险）→ 弹风险确认框，确认后带 confirm_lockout 重试
 * - auth.reauth_required → 弹重认证框，通过后重放
 */
async function run(
  action: (confirmed: boolean) => Promise<void>,
  { confirmed = false, fallbackMessage = '操作失败' } = {},
): Promise<void> {
  busy.value = true
  try {
    await action(confirmed)
    await refetch()
  } catch (err) {
    const code = problemCode(err)
    if (code === 'conflict') {
      riskMessage.value = problemMessage(err, '该操作有断连风险')
      riskAction = action
      riskVisible.value = true
    } else if (code === 'auth.reauth_required') {
      pendingAction = () => run(action, { confirmed, fallbackMessage })
      reauthVisible.value = true
    } else {
      ElMessage.error(problemMessage(err, fallbackMessage))
    }
  } finally {
    busy.value = false
  }
}

async function confirmRisk(): Promise<void> {
  const action = riskAction
  riskVisible.value = false
  riskAction = null
  if (action) {
    await run(action, { confirmed: true })
  }
}

function onReauthSuccess(): void {
  const action = pendingAction
  pendingAction = null
  void action?.()
}

// —— 开关与默认策略 ——

async function toggleActive(next: boolean): Promise<void> {
  if (next) {
    await run((confirmed) => enableFirewall(confirmed).then(() => undefined), {
      fallbackMessage: '启用防火墙失败',
    })
  } else {
    await run(() => disableFirewall().then(() => undefined), {
      fallbackMessage: '停用防火墙失败',
    })
  }
}

async function changeDefault(
  direction: 'incoming' | 'outgoing',
  policy: string,
): Promise<void> {
  const value = policy as 'allow' | 'deny' | 'reject'
  await run(
    (confirmed) => setFirewallDefault(direction, value, confirmed).then(() => undefined),
    { fallbackMessage: '修改默认策略失败' },
  )
}

async function handleReload(): Promise<void> {
  await run(() => reloadFirewall().then(() => undefined), { fallbackMessage: '重载失败' })
  ElMessage.success('已重载防火墙规则')
}

// —— 规则表格 ——

function isProtectedRule(rule: FirewallRule): boolean {
  if (rule.v6 || !rule.action.toUpperCase().startsWith('ALLOW')) {
    return false
  }
  const match = /^([\d,:]+)(?:\/(?:tcp|udp))?$/.exec(rule.to.trim())
  if (!match) {
    return false
  }
  return match[1]!
    .split(',')
    .some((part) => {
      if (part.includes(':')) {
        const [start, end] = part.split(':').map(Number)
        return [...protectedPorts.value].some((p) => p >= (start ?? 0) && p <= (end ?? 0))
      }
      return protectedPorts.value.has(Number(part))
    })
}

async function handleDelete(rule: FirewallRule): Promise<void> {
  await run(
    (confirmed) =>
      deleteFirewallRule(rule.num, ruleFingerprint(rule), confirmed).then(() => undefined),
    { fallbackMessage: '删除规则失败' },
  )
}

// —— 新增规则 ——

const addVisible = ref(false)
const addFormRef = ref<FormInstance>()
const addForm = reactive<AddRuleRequest>({
  action: 'allow',
  direction: 'in',
  port: '',
  proto: 'tcp',
  from_addr: 'any',
  to_addr: 'any',
  comment: '',
})

const addRules: FormRules<typeof addForm> = {
  port: [
    {
      pattern: /^$|^\d{1,5}(:\d{1,5})?$/,
      message: '端口需为 1-65535 或 起始:结束 区间',
      trigger: 'blur',
    },
  ],
  from_addr: [
    {
      pattern: /^(any|[0-9a-fA-F.:]+(\/\d{1,3})?)$/,
      message: '需填 any 或 IP / CIDR',
      trigger: 'blur',
    },
  ],
}

function openAdd(port = ''): void {
  addForm.action = 'allow'
  addForm.direction = 'in'
  addForm.port = port
  addForm.proto = 'tcp'
  addForm.from_addr = 'any'
  addForm.to_addr = 'any'
  addForm.comment = ''
  addVisible.value = true
}

async function submitAdd(): Promise<void> {
  if (!addFormRef.value) {
    return
  }
  const valid = await addFormRef.value.validate().catch(() => false)
  if (!valid) {
    return
  }
  if (!addForm.port && addForm.from_addr === 'any') {
    ElMessage.error('必须指定端口或来源地址，否则该规则会放行/阻断全部流量')
    return
  }
  await run(() => addFirewallRule({ ...addForm }).then(() => undefined), {
    fallbackMessage: '添加规则失败',
  })
  addVisible.value = false
}

/** 常用端口快捷放行；面板端口来自后端，避免前端猜。 */
const quickPorts = computed(() => [
  { label: 'SSH', ports: status.value?.ssh_ports ?? [22] },
  { label: 'HTTP (80)', ports: [80] },
  { label: 'HTTPS (443)', ports: [443] },
  { label: `面板 (${status.value?.panel_port ?? '—'})`, ports: [status.value?.panel_port ?? 0] },
])
</script>

<template>
  <div class="firewall">
    <el-card shadow="never">
      <template #header>
        <div class="firewall__header">
          <span class="firewall__title">防火墙 (ufw)</span>
          <div class="firewall__actions">
            <el-button :disabled="busy || !status?.installed" @click="openAdd()">
              添加规则
            </el-button>
            <el-button :disabled="busy || !status?.active" @click="handleReload">重载</el-button>
            <el-button :loading="isPending" @click="() => refetch()">刷新</el-button>
          </div>
        </div>
      </template>

      <el-alert v-if="isError" :title="errorMessage" type="error" :closable="false" show-icon />
      <el-skeleton v-else-if="isPending" :rows="6" animated />
      <el-alert
        v-else-if="!status?.installed"
        title="本机未安装 ufw"
        type="info"
        :closable="false"
        show-icon
        description="安装后（apt install ufw）刷新本页即可在此管理防火墙规则。"
      />
      <template v-else>
        <el-alert
          v-if="uncovered.length > 0"
          type="warning"
          :closable="false"
          show-icon
          class="firewall__notice"
          :title="`受保护端口 ${uncovered.join('、')} 当前没有放行规则`"
          description="若在此状态下启用防火墙或收紧入站默认策略，可能导致无法再远程连接本机。建议先为这些端口添加放行规则。"
        />

        <el-descriptions :column="2" border class="firewall__summary">
          <el-descriptions-item label="状态">
            <el-switch
              :model-value="status.active"
              :loading="busy"
              active-text="已启用"
              inactive-text="已停用"
              @update:model-value="(v: string | number | boolean) => toggleActive(Boolean(v))"
            />
          </el-descriptions-item>
          <el-descriptions-item label="日志">{{ status.logging || '—' }}</el-descriptions-item>
          <el-descriptions-item label="默认入站">
            <el-select
              :model-value="status.default_incoming"
              size="small"
              :disabled="busy"
              class="firewall__policy"
              @update:model-value="(v: string | number | boolean) => changeDefault('incoming', String(v))"
            >
              <el-option label="deny" value="deny" />
              <el-option label="reject" value="reject" />
              <el-option label="allow" value="allow" />
            </el-select>
          </el-descriptions-item>
          <el-descriptions-item label="默认出站">
            <el-select
              :model-value="status.default_outgoing"
              size="small"
              :disabled="busy"
              class="firewall__policy"
              @update:model-value="(v: string | number | boolean) => changeDefault('outgoing', String(v))"
            >
              <el-option label="allow" value="allow" />
              <el-option label="deny" value="deny" />
              <el-option label="reject" value="reject" />
            </el-select>
          </el-descriptions-item>
          <el-descriptions-item label="受保护端口" :span="2">
            <el-tag
              v-for="port in status.protected_ports"
              :key="port"
              size="small"
              :type="uncovered.includes(port) ? 'danger' : 'success'"
              effect="plain"
              class="firewall__port-tag"
            >
              🔒 {{ port }}
            </el-tag>
            <span class="firewall__hint">（SSH 与面板端口，删除其放行规则需二次确认）</span>
          </el-descriptions-item>
        </el-descriptions>

        <div class="firewall__quick">
          <span class="firewall__hint">快捷放行：</span>
          <el-button
            v-for="item in quickPorts"
            :key="item.label"
            size="small"
            :disabled="busy || item.ports.length === 0 || item.ports[0] === 0"
            @click="openAdd(String(item.ports[0]))"
          >
            {{ item.label }}
          </el-button>
        </div>

        <el-table :data="rules" size="small" class="firewall__table">
          <el-table-column prop="num" label="#" width="56" />
          <el-table-column label="至" min-width="180">
            <template #default="{ row }">
              <span>{{ row.to }}</span>
              <el-tag
                v-if="isProtectedRule(row)"
                size="small"
                type="warning"
                effect="plain"
                class="firewall__port-tag"
              >
                🔒 受保护
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="action" label="动作" width="110" />
          <el-table-column prop="from" label="来自" min-width="160" />
          <el-table-column prop="comment" label="备注" min-width="160" />
          <el-table-column label="操作" width="90" align="right">
            <template #default="{ row }">
              <el-button link type="danger" :disabled="busy" @click="handleDelete(row)">
                删除
              </el-button>
            </template>
          </el-table-column>
          <template #empty>
            <span>{{ status.active ? '暂无规则' : '防火墙未启用' }}</span>
          </template>
        </el-table>
      </template>
    </el-card>

    <!-- 新增规则 -->
    <el-dialog v-model="addVisible" title="添加防火墙规则" width="520px">
      <el-form ref="addFormRef" :model="addForm" :rules="addRules" label-width="90px">
        <el-form-item label="动作">
          <el-select v-model="addForm.action">
            <el-option label="allow（放行）" value="allow" />
            <el-option label="deny（丢弃）" value="deny" />
            <el-option label="reject（拒绝）" value="reject" />
            <el-option label="limit（限速）" value="limit" />
          </el-select>
        </el-form-item>
        <el-form-item label="方向">
          <el-radio-group v-model="addForm.direction">
            <el-radio value="in">入站</el-radio>
            <el-radio value="out">出站</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="端口" prop="port">
          <el-input v-model="addForm.port" placeholder="22 或 6000:6010，留空表示不限" />
        </el-form-item>
        <el-form-item label="协议">
          <el-select v-model="addForm.proto">
            <el-option label="tcp" value="tcp" />
            <el-option label="udp" value="udp" />
            <el-option label="不限" value="" />
          </el-select>
        </el-form-item>
        <el-form-item label="来源" prop="from_addr">
          <el-input v-model="addForm.from_addr" placeholder="any 或 192.168.1.0/24" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="addForm.comment" maxlength="120" placeholder="可选" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="addVisible = false">取消</el-button>
        <el-button type="primary" :loading="busy" @click="submitAdd">添加</el-button>
      </template>
    </el-dialog>

    <!-- 断连风险确认 -->
    <el-dialog v-model="riskVisible" title="可能导致无法连接本机" width="520px">
      <el-alert type="error" :closable="false" show-icon :title="riskMessage" />
      <p class="firewall__risk-hint">
        请确认你有其它方式访问本机（如云厂商控制台 / VNC），否则该操作可能让你彻底失去连接。
      </p>
      <template #footer>
        <el-button @click="riskVisible = false">取消</el-button>
        <el-button type="danger" :loading="busy" @click="confirmRisk">知晓风险，继续</el-button>
      </template>
    </el-dialog>

    <ReauthDialog
      v-model="reauthVisible"
      message="修改防火墙规则需要再次验证身份，请输入登录密码。"
      @success="onReauthSuccess"
    />
  </div>
</template>

<style scoped>
.firewall__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.firewall__title {
  font-weight: 600;
}

.firewall__actions {
  display: flex;
  gap: 8px;
}

.firewall__notice {
  margin-bottom: 12px;
}

.firewall__summary {
  margin-bottom: 12px;
}

.firewall__policy {
  width: 120px;
}

.firewall__port-tag {
  margin-left: 6px;
}

.firewall__quick {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.firewall__hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.firewall__risk-hint {
  margin: 12px 0 0;
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.6;
}
</style>
