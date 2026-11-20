<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { get, manage } from '../../api/client'
import type { Page, RunSummary, Schedule, ScheduleConfig } from '../../api/types'
import { date, message } from '../../shared/display'
import Status from '../../shared/Status.vue'

const rules = ref<Schedule[]>([])
const error = ref('')
const editError = ref('')
const loading = ref(true)
const busy = ref(false)
const next = ref<string | null>()
const selected = ref<Schedule | null>()
const run = ref<{ summary: RunSummary; works: { id: string; directory: string; error?: { message: string } }[] }>()
const defaults = (): ScheduleConfig => ({
  parent_directory: '', kind: 'daily', timezone: 'Asia/Shanghai',
  local_time: '09:00:00', weekday: null, enabled: true,
})
const form = reactive(defaults())
const weekdays = ['星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日']
let intent: { signature: string; key: string } | undefined

async function load(more = false) {
  loading.value = true
  error.value = ''
  try {
    const response = await get<Page<Schedule>>('/schedules?view=summary&limit=25' +
      (more && next.value ? '&cursor=' + next.value : ''))
    rules.value = more ? [...rules.value, ...response.items] : response.items
    next.value = response.next_cursor
  } catch (failure) { error.value = message(failure) }
  finally { loading.value = false }
}
function edit(rule: Schedule | null) {
  selected.value = rule
  Object.assign(form, rule?.config ?? defaults())
  editError.value = ''
}
async function change(path: string, data: object, method: string) {
  const signature = method + path + JSON.stringify(data)
  if (intent?.signature !== signature) intent = { signature, key: crypto.randomUUID() }
  busy.value = true
  editError.value = ''
  try {
    await manage(path, data, method, intent.key)
    intent = undefined
    selected.value = undefined
    await load()
  } catch (failure) { editError.value = message(failure) }
  finally { busy.value = false }
}
function save() {
  const data = {
    parent_directory: form.parent_directory.trim(), kind: form.kind, timezone: form.timezone.trim(),
    local_time: form.local_time.length === 5 ? form.local_time + ':00' : form.local_time,
    weekday: form.kind === 'weekly' ? form.weekday : null, enabled: form.enabled,
  }
  const rule = selected.value
  if (rule) return change('/schedules/' + rule.id, { ...data, expected_version: rule.version }, 'PATCH')
  return change('/schedules', data, 'POST')
}
function toggle(rule: Schedule) {
  return change('/schedules/' + rule.id, { enabled: !rule.enabled, expected_version: rule.version }, 'PATCH')
}
async function openRun(id: string) {
  try { run.value = await get('/schedule-runs/' + id) }
  catch (failure) { error.value = message(failure) }
}
onMounted(() => load())
</script>
<template>
  <header class="page-head"><div><h1>定时规则</h1><p>每日或每周发现已就绪作品并派发任务</p></div>
    <div class="row"><ElButton :loading="loading" @click="load()">刷新</ElButton>
      <ElButton type="primary" @click="edit(null)">新建规则</ElButton></div>
  </header>
  <p v-if="error" class="notice error" role="alert">{{ error }}</p>
  <p v-if="editError" class="notice error" role="alert">{{ editError }}</p>
  <section v-if="selected !== undefined" class="panel">
    <h2>{{ selected ? '编辑规则' : '新建规则' }}</h2>
    <form @submit.prevent="save">
      <label class="field">作品父目录<input v-model="form.parent_directory" required /></label>
      <div class="filters" style="margin-top: 14px">
        <label class="field">频率<select v-model="form.kind"><option value="daily">每日</option><option value="weekly">每周</option></select></label>
        <label v-if="form.kind === 'weekly'" class="field">星期<select v-model="form.weekday" required>
          <option :value="null">请选择</option><option v-for="(day, i) in weekdays" :key="day" :value="i + 1">{{ day }}</option>
        </select></label>
        <label class="field">执行时间<input v-model="form.local_time" type="time" step="1" required /></label>
        <label class="field">IANA 时区<input v-model="form.timezone" required /></label>
      </div>
      <p><label><input v-model="form.enabled" type="checkbox" /> 启用规则</label></p>
      <div class="row">
        <ElButton type="primary" native-type="submit" :loading="busy">保存</ElButton>
        <ElButton :disabled="busy" @click="selected = undefined">取消编辑</ElButton>
      </div>
    </form>
  </section>
  <section class="panel">
    <p v-if="loading && !rules.length" class="muted" role="status">正在读取定时规则…</p>
    <div v-else-if="!rules.length" class="empty">还没有定时规则</div>
    <div v-else class="table-scroll"><table>
      <thead><tr><th>作品父目录</th><th>规则</th><th>下次派发</th><th>最近运行</th><th>操作</th></tr></thead>
      <tbody><tr v-for="rule in rules" :key="rule.id">
        <td class="break">{{ rule.config.parent_directory }}</td>
        <td>{{ rule.config.kind === 'daily' ? '每日' : weekdays[(rule.config.weekday ?? 1) - 1] }}
          {{ rule.config.local_time }}<small class="cell-detail">{{ rule.enabled ? '已启用' : '已停用' }} · {{ rule.config.timezone }}</small></td>
        <td>{{ date(rule.next_fire_at) }}</td>
        <td><template v-if="rule.recent_run">
          <Status :value="rule.recent_run.status" />
          <small class="cell-detail">发现 {{ rule.recent_run.discovered }} · 新建 {{ rule.recent_run.created_tasks }}
            · 跳过 {{ rule.recent_run.skipped }} · 失败 {{ rule.recent_run.failed }}</small>
          <ElButton size="small" text @click="openRun(rule.recent_run.id)">详情</ElButton>
        </template><span v-else class="muted">尚未运行</span></td>
        <td><div class="row"><ElButton size="small" :disabled="busy" @click="edit(rule)">编辑</ElButton>
          <ElButton size="small" :disabled="busy" @click="toggle(rule)">{{ rule.enabled ? '停用' : '启用' }}</ElButton></div></td>
      </tr></tbody>
    </table></div>
    <ElButton v-if="next" :loading="loading" @click="load(true)">更多规则</ElButton>
  </section>
  <section v-if="run" class="panel">
    <div class="page-head"><h2>最近运行详情</h2><ElButton @click="run = undefined">关闭</ElButton></div>
    <p>发现 {{ run.summary.discovered }} 项，创建 {{ run.summary.created_tasks }} 个任务，
      复用 {{ run.summary.reused_tasks }} 个，跳过 {{ run.summary.skipped }} 项，失败 {{ run.summary.failed }} 项。</p>
    <p v-if="run.summary.error" class="notice error">{{ run.summary.error }}</p>
    <ul><li v-for="work in run.works.filter(item => item.error)" :key="work.id">
      {{ work.directory }} · {{ work.error?.message }}</li></ul>
  </section>
</template>
