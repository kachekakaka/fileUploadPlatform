<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { eventStream, get, manage } from '../../api/client'
import type { Page, Task, TaskEvent } from '../../api/types'
import { detailPath } from '../../app/paths'
import { notify } from '../../embed/protocol'
import { platformName, registry } from '../../platforms/registry'
import { date, message, shortId, state } from '../../shared/display'
import Status from '../../shared/Status.vue'
import ManifestView from '../works/ManifestView.vue'

const route = useRoute()
const id = String(route.params.id)
const embedded = route.path.startsWith('/embed/')
const path = '/tasks/' + id
const task = ref<Task>()
const error = ref('')
const actionError = ref('')
const operation = ref('')
const busy = ref(false)
const loading = ref(true)
const newTask = ref<string>()
const conclusion = ref('uncertain')
const rationale = ref('')
const evidence = ref('')
const records = ref<Record<string, unknown>[][]>([])
const events = ref<TaskEvent[]>([])
const disconnected = ref(false)
const categories = [{ path: 'attempts', label: '执行尝试' }, { path: 'queries', label: '远端查询' },
  { path: 'resolutions', label: '人工核验' }]
const operations: Record<string, string> = { cancel: '取消任务', resubmit: '重新提交', query: '远端查询', resolve: '人工核验' }
const resultComponent = computed(() => task.value && registry[task.value.snapshot.target.platform]?.result)
const needsBilibiliVerification = computed(() => {
  const detail = task.value
  if (detail?.status !== 'UNCERTAIN' || detail.snapshot.target.platform !== 'bilibili') return false
  const attempt = detail.attempt
  const history = attempt?.result?.history ?? Object.values(attempt?.execution?.observed_events ?? {})
  return history.some(event => event.phase === 'submit_response' && event.details?.code === 601)
})
let stream: EventSource | undefined
let refreshTimer: ReturnType<typeof setTimeout> | undefined
let after = 0
let alive = true
let issued = 0
let intent: { signature: string; key: string } | undefined

function merge(incoming: TaskEvent[]) {
  const known = new Map(events.value.map(event => [event.sequence, event]))
  for (const event of incoming) known.set(event.sequence, event)
  events.value = [...known.values()].sort((a, b) => a.sequence - b.sequence)
}
async function catchUp() {
  let cursor = after
  while (alive) {
    const value = await get<Page<TaskEvent> & { next_sequence: number }>(
      path + '/events?limit=200&after_sequence=' + cursor,
    )
    if (!alive) return
    merge(value.items)
    after = Math.max(after, value.next_sequence)
    if (value.items.length < 200 || value.next_sequence <= cursor) return
    cursor = value.next_sequence
  }
}
function refresh(withEvents = false) {
  if (refreshTimer) clearTimeout(refreshTimer)
  refreshTimer = setTimeout(() => {
    refreshTimer = undefined
    void load(withEvents)
  }, 300)
}
function watchStream() {
  if (!alive || stream) return
  stream = eventStream(id, after)
  stream.addEventListener('task', event => {
    const item = JSON.parse((event as MessageEvent).data) as TaskEvent
    merge([item])
    after = Math.max(after, item.sequence)
    refresh()
  })
  stream.onopen = () => { disconnected.value = false }
  stream.onerror = () => { disconnected.value = true; refresh(true) }
}
async function load(withEvents = false) {
  const query = ++issued
  try {
    if (withEvents) await catchUp()
    const [detail, ...pages] = await Promise.all([
      get<Task>(path), ...categories.map(item => get<Page<Record<string, unknown>>>(path + '/' + item.path)),
    ])
    if (!alive || query !== issued) return
    const before = task.value?.status
    task.value = detail
    records.value = pages.map(page => page.items)
    error.value = ''
    if (before && before !== detail.status) notify('task-updated', id, detail.status)
    watchStream()
  } catch (failure) { if (alive && query === issued) error.value = message(failure) }
  finally { if (alive && query === issued) loading.value = false }
}
async function execute() {
  if (!task.value || !operation.value || busy.value) return
  const name = operation.value
  const body = name === 'resolve' ? {
    expected_task_version: task.value.version,
    conclusion: conclusion.value, reason: rationale.value.trim(),
    evidence_refs: evidence.value.split('\n').map(line => line.trim()).filter(Boolean),
  } : {}
  const signature = name + JSON.stringify(body)
  if (intent?.signature !== signature) intent = { signature, key: crypto.randomUUID() }
  busy.value = true
  actionError.value = ''
  try {
    const value = await manage<{ tasks?: { task_id: string }[] }>(path + '/' + name, body, 'POST', intent.key)
    newTask.value = value.tasks?.[0]?.task_id
    intent = undefined
    operation.value = ''
    await load(true)
  } catch (failure) {
    actionError.value = message(failure)
    await load(true)
  } finally { busy.value = false }
}
onMounted(() => load(true))
onUnmounted(() => {
  alive = false
  stream?.close()
  if (refreshTimer) clearTimeout(refreshTimer)
})
</script>
<template>
  <RouterLink class="back" :to="embedded ? '/embed/tasks' : '/tasks'">← 任务</RouterLink>
  <header class="page-head"><div><h1>{{ task?.snapshot.metadata.title || '任务详情' }}</h1><p>{{ id }}</p></div>
    <Status v-if="task" :value="task.status" />
  </header>
  <p v-if="loading" class="muted" role="status">正在读取任务…</p>
  <p v-if="error" class="notice error" role="alert">{{ error }} <ElButton @click="load(true)">重试</ElButton></p>
  <p v-if="disconnected" class="notice" role="status">实时连接已断开，正在通过接口回查并重连</p>
  <template v-if="task">
    <p v-if="needsBilibiliVerification" class="notice">B 站尚未确认收稿，可能需要本人验证。请先查回原任务，避免重复投稿。</p>
    <p v-else-if="task.status === 'UNCERTAIN'" class="notice">远端结果待核验。请检查原稿件并记录核验依据。</p>
    <section class="panel">
      <h2>任务操作</h2>
      <div class="row">
        <ElButton v-for="(label, name) in operations" :key="name" :disabled="busy || !task.available_actions?.[name]?.allowed"
          :title="task.available_actions?.[name]?.reason || undefined" @click="operation = name">{{ label }}</ElButton>
      </div>
      <form v-if="operation" @submit.prevent="execute">
        <p>确认{{ operations[operation] }}？</p>
        <div v-if="operation === 'resolve'" class="filters">
          <label class="field">结论<select v-model="conclusion"><option value="uncertain">仍待核验</option>
            <option value="succeeded">已成功</option><option value="failed">失败</option></select></label>
          <label class="field">核验说明<textarea v-model="rationale" required /></label>
          <label class="field">核验依据（每行一条）<textarea v-model="evidence" :placeholder="task.evidence_ref" /></label>
        </div>
        <div class="row"><ElButton native-type="submit" type="primary" :loading="busy">确认</ElButton>
          <ElButton :disabled="busy" @click="operation = ''">返回</ElButton></div>
      </form>
      <p v-if="actionError" role="alert" class="notice error">{{ actionError }}</p>
      <p v-if="newTask" class="notice">已提交新一轮任务 <RouterLink :to="detailPath('tasks', newTask, embedded)">{{ shortId(newTask) }}</RouterLink></p>
    </section>
    <section class="panel"><h2>任务快照</h2>
      <dl class="kv">
        <dt>作品</dt><dd><RouterLink :to="detailPath('works', task.work_id, embedded)">{{ task.work_id }}</RouterLink></dd>
        <dt>投稿目标</dt><dd>{{ platformName(task.snapshot.target.platform) }} · {{ task.snapshot.target.account }}</dd>
        <dt>配置版本</dt><dd>{{ task.snapshot.config_version || '—' }}</dd>
        <dt>素材根目录</dt><dd>{{ task.snapshot.directory }}</dd>
        <dt>创建时间</dt><dd>{{ date(task.created_at) }}</dd>
        <dt>计划时间</dt><dd>{{ date(task.scheduled_at) }}</dd>
        <dt>提交轮次</dt><dd>{{ task.submission_round }}</dd>
      </dl>
      <h3 style="margin-top: 18px">视频顺序</h3>
      <ol><li v-for="part in task.snapshot.parts" :key="part.video_id">{{ part.title }}</li></ol>
      <details><summary>素材及投稿选项</summary><pre>{{ JSON.stringify({
        assets: task.snapshot.assets, options: task.snapshot.target.options,
      }, null, 2) }}</pre></details>
      <details v-if="task.snapshot.manifest"><summary>任务使用的配置</summary><ManifestView :manifest="task.snapshot.manifest" /></details>
    </section>
    <section class="panel"><h2>事件时间线</h2>
      <ol v-if="events.length" class="timeline"><li v-for="event in events" :key="event.sequence">
        <details><summary>{{ date(event.created_at) }} · {{ event.type.startsWith('task.') ?
          state(event.type.slice(5).toUpperCase()) : event.type }}</summary><pre>{{ JSON.stringify(event.payload, null, 2) }}</pre></details>
      </li></ol><div v-else class="empty">暂无事件</div>
    </section>
    <section class="panel"><h2>执行和回执</h2>
      <component :is="resultComponent" v-if="resultComponent && task.receipt" :receipt="task.receipt" />
      <details v-else-if="task.receipt"><summary>投稿回执</summary><pre>{{ JSON.stringify(task.receipt, null, 2) }}</pre></details>
      <p v-else class="muted">暂无投稿回执</p>
      <details v-for="(category, index) in categories" :key="category.path">
        <summary>{{ category.label }}（{{ records[index]?.length || 0 }}）</summary>
        <pre v-if="records[index]?.length">{{ JSON.stringify(records[index], null, 2) }}</pre>
        <p v-else class="muted">暂无记录</p>
      </details>
    </section>
  </template>
</template>
