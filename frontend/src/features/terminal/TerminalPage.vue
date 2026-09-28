<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Monitor } from '@element-plus/icons-vue'
import type { FitAddon } from '@xterm/addon-fit'
import type { Terminal } from '@xterm/xterm'

import type { ApiError } from '@/api/client'
import { problemMessage } from '@/api/problem'
import { createTerminalSession, terminalWsUrl } from '@/api/terminal'
import ReauthDialog from '@/components/ReauthDialog.vue'

/**
 * root 终端页：先经 ReauthDialog 验证身份，再 POST /terminal/session 取一次性票据，
 * 随后打开 WebSocket 桥接浏览器 xterm 与服务端 root PTY。
 * 协议：首帧文本 {"type":"auth","ticket"}；二进制帧 = PTY I/O；
 * JSON 控制帧 {"type":"resize",cols,rows} / 服务端 {"type":"exit",code} / {"type":"error"}。
 * xterm 及 fit 插件按需懒加载（独立 'xterm' chunk）。
 */

type TerminalStatus = 'idle' | 'connecting' | 'connected' | 'unavailable' | 'closed'

/** 服务端 JSON 控制帧（exit：shell 退出；error：终端服务打开失败）。 */
interface ControlFrame {
  type?: string
  code?: number
  message?: string
}

const status = ref<TerminalStatus>('idle')
const reauthVisible = ref(false)
const exitCode = ref<number | null>(null)
const containerRef = ref<HTMLDivElement | null>(null)

// 非响应式的连接期资源（每次会话都重建，随会话销毁）。
let ws: WebSocket | null = null
let term: Terminal | null = null
let fitAddon: FitAddon | null = null
let resizeObserver: ResizeObserver | null = null
const encoder = new TextEncoder()

const closedSubTitle = computed(() =>
  exitCode.value !== null
    ? `Shell 已退出（退出码 ${exitCode.value}）。`
    : '连接已关闭（Shell 退出、空闲超时或达到最长会话时限）。',
)

/** 入口：任何状态下重新打开终端都先经重认证对话框。 */
function openGate(): void {
  reauthVisible.value = true
}

function handleReauthSuccess(): void {
  void connect()
}

async function connect(): Promise<void> {
  if (status.value === 'connecting' || status.value === 'connected') {
    return
  }
  status.value = 'connecting'
  exitCode.value = null
  let ticket: string
  try {
    const session = await createTerminalSession()
    ticket = session.ticket
  } catch (error) {
    const problem = (error as ApiError).problem
    if (problem?.code === 'terminal.unavailable') {
      // 503：mypanel-terminal 服务不可达，展示禁用态而非假占位。
      status.value = 'unavailable'
    } else if (problem?.status === 409) {
      status.value = 'idle'
      ElMessage.error('已有终端会话在运行，请先结束原会话后重试')
    } else {
      status.value = 'idle'
      ElMessage.error(problemMessage(error, '创建终端会话失败'))
    }
    return
  }
  try {
    await createTerm()
  } catch {
    status.value = 'idle'
    ElMessage.error('终端组件加载失败，请刷新页面后重试')
    return
  }
  openSocket(ticket)
}

/** 若 WebSocket 已打开，发送二进制 PTY 输入帧。 */
function send(data: Uint8Array): void {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(data)
  }
}

/** 若 WebSocket 已打开，发送 JSON 控制帧（如 resize）。 */
function sendControl(frame: Record<string, unknown>): void {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(frame))
  }
}

/** 懒加载 xterm（仅在真正打开会话时载入，打包为独立 'xterm' chunk）并创建终端实例。 */
async function createTerm(): Promise<void> {
  destroyTerm()
  const [{ Terminal: XTerminal }, { FitAddon: XFitAddon }] = await Promise.all([
    import('@xterm/xterm'),
    import('@xterm/addon-fit'),
    import('@xterm/xterm/css/xterm.css'),
  ])
  const instance = new XTerminal({
    cursorBlink: true,
    fontSize: 14,
    fontFamily: "Menlo, Consolas, 'DejaVu Sans Mono', 'Courier New', monospace",
    scrollback: 5000,
    theme: {
      background: '#1d1e1f',
      foreground: '#e5eaf3',
    },
  })
  const fit = new XFitAddon()
  instance.loadAddon(fit)
  instance.onData((data) => {
    send(encoder.encode(data))
  })
  instance.onBinary((data) => {
    // onBinary 的字符串每个 charCode 即一个原始字节（二进制粘贴场景）。
    send(Uint8Array.from(data, (ch) => ch.charCodeAt(0) & 0xff))
  })
  instance.onResize(({ cols, rows }) => {
    // fit 改变行列时经此回调把 PTY 尺寸同步给服务端。
    sendControl({ type: 'resize', cols, rows })
  })
  term = instance
  fitAddon = fit
}

function openSocket(ticket: string): void {
  const socket = new WebSocket(terminalWsUrl())
  socket.binaryType = 'arraybuffer'
  ws = socket
  socket.onopen = () => {
    if (ws !== socket) {
      return
    }
    // 首帧必须是一次性票据认证（60 秒内有效、仅可用一次）。
    socket.send(JSON.stringify({ type: 'auth', ticket }))
    void attachTerm()
  }
  socket.onmessage = (event: MessageEvent<ArrayBuffer | string>) => {
    if (ws !== socket) {
      return
    }
    if (typeof event.data === 'string') {
      handleControl(event.data)
    } else {
      term?.write(new Uint8Array(event.data))
    }
  }
  socket.onclose = (event: CloseEvent) => {
    if (ws !== socket) {
      return
    }
    ws = null
    finishSession(event.code)
  }
  socket.onerror = () => {
    // 出错后浏览器必然随后触发 onclose，这里无需额外处理。
  }
}

/** WebSocket 打开后把 xterm 挂载到容器并自适应尺寸。 */
async function attachTerm(): Promise<void> {
  status.value = 'connected'
  await nextTick()
  const container = containerRef.value
  const instance = term
  if (!container || !instance) {
    return
  }
  instance.open(container)
  fitNow()
  instance.focus()
  resizeObserver = new ResizeObserver(() => {
    fitNow()
  })
  resizeObserver.observe(container)
}

function fitNow(): void {
  try {
    fitAddon?.fit()
  } catch {
    // 容器尚不可见时 fit 可能失败，忽略即可（下次尺寸变化会重试）。
  }
}

function handleControl(text: string): void {
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    return
  }
  if (!parsed || typeof parsed !== 'object') {
    return
  }
  const frame = parsed as ControlFrame
  if (frame.type === 'exit') {
    exitCode.value = typeof frame.code === 'number' ? frame.code : null
  } else if (frame.type === 'error') {
    // 服务端在打开 PTY 失败时发送 error 帧（随后关闭连接）。
    status.value = 'unavailable'
  }
}

/** 连接结束的统一收尾：释放终端资源并根据关闭原因落到对应状态。 */
function finishSession(closeCode: number): void {
  resizeObserver?.disconnect()
  resizeObserver = null
  destroyTerm()
  if (status.value === 'unavailable') {
    return
  }
  if (closeCode === 1008) {
    // 策略拒绝：票据无效/过期、Origin 或会话校验失败。
    status.value = 'idle'
    ElMessage.error('终端连接被拒绝，请重新验证身份后再试')
    return
  }
  status.value = 'closed'
}

function destroyTerm(): void {
  term?.dispose()
  term = null
  fitAddon = null
}

/** 用户主动断开：关闭 WebSocket，服务端随之关闭 PTY 并记录审计。 */
function disconnect(): void {
  ws?.close(1000)
}

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  resizeObserver = null
  if (ws) {
    const socket = ws
    ws = null
    socket.onopen = null
    socket.onmessage = null
    socket.onclose = null
    socket.onerror = null
    socket.close(1000)
  }
  destroyTerm()
})
</script>

<template>
  <div class="terminal-page">
    <el-card shadow="never" class="terminal-page__card">
      <template #header>
        <div class="terminal-page__header">
          <span class="terminal-page__title">root 终端</span>
          <div class="terminal-page__actions">
            <el-tag v-if="status === 'connected'" type="success" effect="plain">已连接</el-tag>
            <el-button
              v-if="status === 'connected' || status === 'connecting'"
              type="danger"
              plain
              size="small"
              @click="disconnect"
            >
              断开连接
            </el-button>
          </div>
        </div>
      </template>

      <!-- 入口门：先重认证，再签发一次性票据 -->
      <div v-if="status === 'idle'" class="terminal-page__gate">
        <el-icon :size="48" class="terminal-page__gate-icon"><Monitor /></el-icon>
        <p class="terminal-page__gate-text">
          在浏览器中打开主机的 root Shell。该会话以 root 权限执行、全程审计记录，
          并受空闲与最长时限约束；打开前需再次验证身份。
        </p>
        <el-button type="primary" @click="openGate">打开 root 终端</el-button>
      </div>

      <!-- 终端服务不可达（503 terminal.unavailable） -->
      <el-result
        v-else-if="status === 'unavailable'"
        icon="warning"
        title="终端服务未运行"
        sub-title="mypanel-terminal 服务不可用，请在主机上检查该服务状态后重试。"
      >
        <template #extra>
          <el-button type="primary" @click="openGate">重试</el-button>
        </template>
      </el-result>

      <!-- 会话已结束 -->
      <el-result
        v-else-if="status === 'closed'"
        icon="info"
        title="终端会话已结束"
        :sub-title="closedSubTitle"
      >
        <template #extra>
          <el-button type="primary" @click="openGate">重新连接</el-button>
        </template>
      </el-result>

      <!-- connecting / connected：终端容器 -->
      <div
        v-else
        v-loading="status === 'connecting'"
        element-loading-text="正在建立终端连接…"
        class="terminal-page__term-wrap"
      >
        <div ref="containerRef" class="terminal-page__term" />
      </div>
    </el-card>

    <ReauthDialog
      v-model="reauthVisible"
      title="打开 root 终端"
      message="root 终端拥有最高权限，请输入当前登录密码以确认身份。"
      @success="handleReauthSuccess"
    />
  </div>
</template>

<style scoped>
.terminal-page__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.terminal-page__title {
  font-weight: 600;
}

.terminal-page__actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.terminal-page__gate {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
  padding: 64px 24px;
  text-align: center;
}

.terminal-page__gate-icon {
  color: var(--el-text-color-secondary);
}

.terminal-page__gate-text {
  max-width: 480px;
  margin: 0;
  color: var(--el-text-color-regular);
  font-size: 14px;
  line-height: 1.6;
}

.terminal-page__term-wrap {
  height: calc(100vh - 240px);
  min-height: 360px;
  padding: 8px;
  border-radius: 4px;
  background: #1d1e1f;
}

.terminal-page__term {
  height: 100%;
}
</style>
