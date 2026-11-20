<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { get, manage } from '../../api/client'
import type { Login, Page } from '../../api/types'
import { notify } from '../../embed/protocol'
import { date, message, shortId } from '../../shared/display'

const props = defineProps<{ accountId: string | null; available: boolean; open: boolean }>()
const emit = defineEmits<{ changed: [] }>()
const sessions = ref<Login[]>([])
const error = ref('')
const busy = ref(false)
let stopped = false
let pollTimer: ReturnType<typeof setTimeout>
const keys = new Map<string, string>()
const phases: Record<string, string> = {
  queued: '等待启动', waiting_for_executions: '等待正在执行的任务', starting: '启动扫码',
  waiting_confirmation: '请扫码并确认', verifying_identity: '正在核对账号',
  waiting_for_account: '等待账号可用', finalizing: '保存登录结果', cancelling: '取消中',
  recovering: '可恢复的扫码操作', succeeded: '登录完成', failed: '登录失败',
  cancelled: '已取消', expired: '二维码已过期',
}

function merge(value: Login) {
  const previous = sessions.value.find(item => item.session_id === value.session_id)
  if (previous && previous.revision > value.revision) return
  sessions.value = previous ? sessions.value.map(
    item => item.session_id === value.session_id ? value : item,
  ) : [value, ...sessions.value]
  if (!previous?.outcome && value.outcome === 'succeeded') {
    emit('changed')
    const account = value.account_summary
    if (account) notify('account-updated', account.id, account.availability)
  }
}

async function poll() {
  try {
    if (!busy.value) {
      const pending = sessions.value.filter(value => !value.outcome)
      if (pending.length) {
        await Promise.all(pending.map(async value => merge(
          await get<Login>('/account-login-sessions/' + value.session_id),
        )))
      }
    }
  } catch (failure) { error.value = message(failure) }
  finally {
    if (!stopped) pollTimer = setTimeout(poll, 2000)
  }
}

async function action(kind: 'start' | 'resume' | 'cancel', login?: Login) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  const account = login?.account_id ?? (login ? null : props.accountId)
  const path = kind === 'start' ? '/account-login-sessions' :
    `/account-login-sessions/${login?.session_id}/${kind}`
  const operation = kind + ':' + (kind === 'start' ? account : login?.session_id)
  const key = keys.get(operation) ?? crypto.randomUUID()
  keys.set(operation, key)
  try {
    merge(await manage<Login>(path, kind === 'start' ? { platform: 'bilibili', account_id: account } : {}, 'POST', key))
    keys.delete(operation)
  } catch (failure) { error.value = message(failure) }
  finally { busy.value = false }
}

onMounted(async () => {
  try {
    for (const value of (await get<Page<Login>>('/account-login-sessions?active=true')).items.reverse()) merge(value)
  }
  catch (failure) { error.value = message(failure) }
  finally { if (!stopped) void poll() }
})
onUnmounted(() => { stopped = true; clearTimeout(pollTimer) })
</script>
<template>
  <section v-if="open || sessions.length || error" class="panel">
    <div class="row"><h2>Bilibili 扫码</h2></div>
    <p v-if="error" class="notice error" role="alert">{{ error }}</p>
    <ElButton v-if="open" type="primary" :disabled="!available || busy" @click="action('start')">
      {{ accountId ? '为选中账号开始扫码' : '开始扫码' }}
    </ElButton>
    <article v-for="login in sessions" :key="login.session_id">
      <p><strong>{{ phases[login.outcome ?? login.phase] ?? login.phase }}</strong>
        <span class="muted"> · {{ shortId(login.session_id) }}</span></p>
      <template v-if="login.challenge">
        <img class="qr" :src="login.challenge.image_path" alt="Bilibili 扫码登录二维码" />
        <p class="muted">本机确认时限 {{ date(Date.parse(login.challenge.deadline_at) / 1000) }}</p>
      </template>
      <p v-if="login.error" class="notice error" role="alert">{{ login.error.message }}</p>
      <div class="row">
        <ElButton v-if="login.can_cancel" :disabled="busy" @click="action('cancel', login)">取消扫码</ElButton>
        <ElButton v-if="login.can_resume" :disabled="busy" @click="action('resume', login)">恢复扫码</ElButton>
        <ElButton v-if="login.can_retry" :disabled="busy || !available" @click="action('start', login)">重新扫码</ElButton>
      </div>
    </article>
  </section>
</template>
