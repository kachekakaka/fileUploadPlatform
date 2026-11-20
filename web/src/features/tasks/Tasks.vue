<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { get } from '../../api/client'
import type { Account, Page, Platform, Task } from '../../api/types'
import { platformName } from '../../platforms/registry'
import { message, state } from '../../shared/display'
import TaskTable from '../../shared/TaskTable.vue'

const route = useRoute()
const embedded = route.path.startsWith('/embed/')
const filters = reactive({ platform: '', account_id: '', work_id: '', status: '', since: '', until: '', sort: 'newest' })
const options = ['QUEUED', 'CLAIMED', 'VALIDATING', 'RUNNING', 'RETRY_WAIT', 'RECOVERY_PENDING',
  'UNCERTAIN', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'EXPIRED']
const accounts = ref<Account[]>([])
const platforms = ref<Platform[]>([])
const tasks = ref<Task[]>([])
const page = ref(0)
const cursors = ref<string[]>([''])
const next = ref<string | null>()
const loading = ref(true)
const error = ref('')
const optionsError = ref('')
let applied = new URLSearchParams({ sort: 'newest', limit: '20' })

async function load(index = 0) {
  loading.value = true
  error.value = ''
  const query = new URLSearchParams(applied)
  if (cursors.value[index]) query.set('cursor', cursors.value[index])
  try {
    const result = await get<Page<Task>>('/tasks?' + query)
    tasks.value = result.items
    next.value = result.next_cursor
    page.value = index
  } catch (failure) { error.value = message(failure) }
  finally { loading.value = false }
}
function search() {
  applied = new URLSearchParams({ limit: '20' })
  for (const [key, value] of Object.entries(filters)) {
    if (value.trim()) applied.set(key, key === 'since' || key === 'until' ? new Date(value).toISOString() : value.trim())
  }
  cursors.value = ['']
  void load()
}
function more() {
  if (next.value) {
    cursors.value[page.value + 1] = next.value
    void load(page.value + 1)
  }
}
onMounted(async () => {
  void load()
  try {
    const [platformList, accountList] = await Promise.all([
      get<Page<Platform>>('/platforms'), get<Page<Account>>('/accounts?view=summary'),
    ])
    platforms.value = platformList.items
    accounts.value = accountList.items
  } catch (failure) { optionsError.value = message(failure) }
})
</script>
<template>
  <header class="page-head"><div><h1>任务</h1><p>按投稿目标、状态和时间查询任务</p></div></header>
  <section class="panel">
    <form @submit.prevent="search">
      <div class="filters">
        <label class="field">平台<select v-model="filters.platform" @change="filters.account_id = ''">
          <option value="">全部</option><option v-for="platform in platforms" :key="platform.id" :value="platform.id">{{ platformName(platform.id) }}</option>
        </select></label>
        <label class="field">账号<select v-model="filters.account_id">
          <option value="">全部</option><option v-for="account in accounts.filter(a => !filters.platform || a.platform === filters.platform)"
            :key="account.id" :value="account.id">{{ account.display_name }}</option>
        </select></label>
        <label class="field">状态<select v-model="filters.status">
          <option value="">全部</option><option v-for="option in options" :key="option" :value="option">{{ state(option) }}</option>
        </select></label>
        <label class="field">作品编号<input v-model="filters.work_id" /></label>
        <label class="field">起始时间<input v-model="filters.since" type="datetime-local" /></label>
        <label class="field">结束时间<input v-model="filters.until" type="datetime-local" /></label>
        <label class="field">排序<select v-model="filters.sort"><option value="newest">最新</option><option value="oldest">最早</option></select></label>
      </div>
      <ElButton native-type="submit" type="primary" :disabled="loading">筛选</ElButton>
    </form>
    <p v-if="optionsError" class="notice error" role="alert">账号与平台选项读取失败。{{ optionsError }}</p>
  </section>
  <p v-if="error" class="notice error" role="alert">{{ error }} <ElButton @click="load(page)">重试</ElButton></p>
  <section class="panel">
    <p v-if="loading" class="muted" role="status">正在读取任务…</p>
    <TaskTable v-else :tasks="tasks" :embedded="embedded" />
    <div v-if="page || next" class="row pager">
      <ElButton :disabled="!page || loading" @click="load(page - 1)">上一页</ElButton>
      <ElButton :disabled="!next || loading" @click="more">下一页</ElButton>
    </div>
  </section>
</template>
