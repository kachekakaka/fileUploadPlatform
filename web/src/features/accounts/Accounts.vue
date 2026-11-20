<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { get, manage } from '../../api/client'
import type { Account, Page, Platform } from '../../api/types'
import { notify } from '../../embed/protocol'
import { platformName, platformReason, registry } from '../../platforms/registry'
import { message } from '../../shared/display'

const route = useRoute()
const items = ref<Account[]>([])
const platform = ref<Platform>()
const error = ref('')
const loading = ref(true)
const action = ref('')
const loginOpen = ref(false)
const selected = ref<string | null>(null)
const highlighted = computed(() => selected.value || route.query.account)
const loginComponent = registry.bilibili.login
const availability: Record<string, string> = { ready: '可投稿', disabled: '已停用', blocked: '受阻' }
const blocks: Record<string, string> = {
  maintenance: '账号维护中', credential_unverified: '凭据待核验', credential_invalid: '凭据失效',
}

async function load() {
  error.value = ''
  loading.value = true
  try {
    const [accounts, capabilities] = await Promise.all([
      get<Page<Account>>('/accounts?view=summary'), get<Page<Platform>>('/platforms'),
    ])
    items.value = accounts.items.filter(account => account.platform !== 'fake')
    platform.value = capabilities.items.find(item => item.id === 'bilibili')
  } catch (failure) { error.value = message(failure) }
  finally { loading.value = false }
}
function open(id: string | null) {
  selected.value = id
  loginOpen.value = true
}
async function toggle(account: Account) {
  action.value = account.id
  error.value = ''
  try {
    await manage('/accounts/' + account.id, { enabled: !account.enabled }, 'PATCH')
    await load()
    const updated = items.value.find(item => item.id === account.id)
    if (updated) notify('account-updated', updated.id, updated.availability)
  } catch (failure) { error.value = message(failure) }
  finally { action.value = '' }
}
onMounted(load)
</script>
<template>
  <header class="page-head"><div><h1>账号</h1><p>添加、查看和维护投稿账号</p></div>
    <ElButton type="primary" :disabled="!platform?.capabilities.login" @click="open(null)">添加 Bilibili 账号</ElButton>
  </header>
  <p v-if="error" class="notice error" role="alert">{{ error }} <ElButton size="small" @click="load">重试</ElButton></p>
  <p v-if="platform && !platform.capabilities.login" class="notice">
    扫码暂不可用。{{ platformReason(platform.capabilities.login_reason || platform.reason) }}
  </p>
  <section class="panel">
    <p v-if="loading" class="muted" role="status">正在读取账号…</p>
    <div v-else-if="!items.length" class="empty">还没有 Bilibili 投稿账号</div>
    <div v-else class="table-scroll"><table>
      <thead><tr><th>账号</th><th>平台</th><th>状态</th><th>操作</th></tr></thead>
      <tbody><tr v-for="account in items" :key="account.id" :aria-selected="highlighted === account.id">
        <td><strong>{{ account.display_name }}</strong><small class="cell-detail">MID {{ account.remote_user_id ?? '—' }}</small></td>
        <td>{{ platformName(account.platform) }}</td>
        <td>{{ availability[account.availability] }}
          <small class="cell-detail">{{ account.block_reasons.map(reason => blocks[reason] || reason).join('、') }}</small></td>
        <td class="row">
          <ElButton size="small" :disabled="!platform?.capabilities.login" @click="open(account.id)">重新登录</ElButton>
          <ElButton size="small" :disabled="!!action" @click="toggle(account)">{{ account.enabled ? '停用' : '启用' }}</ElButton>
        </td>
      </tr></tbody>
    </table></div>
  </section>
  <component :is="loginComponent" :account-id="selected" :available="!!platform?.capabilities.login"
    :open="loginOpen" @changed="load" />
</template>
<style scoped>
tr[aria-selected="true"] { background: #edf2ff; }
</style>
