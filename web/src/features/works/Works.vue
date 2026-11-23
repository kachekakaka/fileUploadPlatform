<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { get, manage, previewSubmission, receipt, replaySubmission } from '../../api/client'
import type { Page, Preview, Receipt, Work } from '../../api/types'
import { detailPath } from '../../app/paths'
import { platformName } from '../../platforms/registry'
import { date, message, shortId } from '../../shared/display'
import Status from '../../shared/Status.vue'
import ManifestView from './ManifestView.vue'
import {
  forgetSubmission, matchesReceipt, restoreSubmission, saveSubmission, type PendingSubmission,
} from './pendingSubmission'

const route = useRoute()
const embedded = computed(() => route.path.startsWith('/embed/'))
const works = ref<Work[]>([])
const page = ref(0)
const cursors = ref<string[]>([''])
const next = ref<string | null>()
const error = ref('')
const loading = ref(true)
const editing = ref(false)
const directory = ref('')
const manifestPath = ref('')
const preview = ref<Preview>()
let previewDeployment: string | undefined
const result = ref<Receipt>()
const registered = ref<string>()
const busy = ref(false)
const formError = ref('')
const pending = ref<PendingSubmission>()
const registerKey = ref<string>()
let active = true

async function load(index = 0) {
  loading.value = true
  error.value = ''
  try {
    const value = await get<Page<Work>>('/works?limit=25' +
      (cursors.value[index] ? '&cursor=' + encodeURIComponent(cursors.value[index]) : ''))
    works.value = value.items
    next.value = value.next_cursor
    page.value = index
  } catch (failure) { error.value = message(failure) }
  finally { loading.value = false }
}
function more() {
  if (!next.value) return
  cursors.value[page.value + 1] = next.value
  void load(page.value + 1)
}
function invalidate() {
  preview.value = undefined
  previewDeployment = undefined
  registered.value = undefined
  registerKey.value = undefined
  result.value = undefined
  formError.value = ''
}
async function choose() {
  busy.value = true
  formError.value = ''
  try {
    const selected = await manage<{ directory: string | null }>('/dashboard/pick-directory', {})
    if (selected.directory) {
      invalidate()
      directory.value = selected.directory
    }
  } catch (failure) { formError.value = message(failure) }
  finally { busy.value = false }
}
async function prepare() {
  invalidate()
  busy.value = true
  try {
    const health = await get<{ deployment_id: string }>('/system/health')
    if (!health.deployment_id) throw new Error('上传平台没有返回可核对的部署编号')
    preview.value = await previewSubmission<Preview>({
      asset_root: directory.value.trim(),
      manifest_path: manifestPath.value.trim() || directory.value.trim().replace(/[\\/]+$/, '') + '\\upload.yaml',
    }, health.deployment_id)
    previewDeployment = health.deployment_id
  } catch (failure) { formError.value = message(failure) }
  finally { busy.value = false }
}
async function register() {
  if (!preview.value) return
  busy.value = true
  formError.value = ''
  registerKey.value ??= crypto.randomUUID()
  try {
    const record = await manage<{ work_id: string }>('/dashboard/works', {
      preview_id: preview.value.preview_id, source: { application: 'dashboard', kind: 'local_import' },
    }, 'POST', registerKey.value)
    registered.value = record.work_id
    await load()
  } catch (failure) { formError.value = message(failure) }
  finally { busy.value = false }
}
async function submit(lookup = false) {
  if (busy.value) return
  if (!pending.value && preview.value && previewDeployment && !lookup) {
    try {
      pending.value = saveSubmission(preview.value, previewDeployment)
    } catch {
      formError.value = '浏览器无法保存原请求，本次未发送。请启用此页面的本地存储后重试。'
      return
    }
  }
  const intent = pending.value
  if (!intent) return
  busy.value = true
  formError.value = ''
  try {
    const value = lookup ? await receipt<Receipt>(intent.key, intent.deployment) :
      await replaySubmission<Receipt>(intent.body, intent.key, intent.deployment)
    if (!matchesReceipt(value, intent)) throw new Error('回执与原请求不一致，请核对原单')
    if (!active) return
    result.value = value
    forgetSubmission(intent)
    pending.value = restoreSubmission()
    await load()
  } catch (failure) { formError.value = message(failure) }
  finally { busy.value = false }
}

function dismiss() {
  if (!pending.value) return
  try {
    forgetSubmission(pending.value)
    invalidate()
    pending.value = restoreSubmission()
  } catch { formError.value = '浏览器无法移除本地记录' }
}

onMounted(() => {
  try {
    pending.value = restoreSubmission()
    if (pending.value) editing.value = true
  } catch { formError.value = '浏览器无法读取本地待查请求' }
  void load()
})
onUnmounted(() => { active = false })
</script>
<template>
  <header class="page-head"><div><h1>作品</h1><p>查看已登记作品，或从本机配置登记</p></div>
    <ElButton type="primary" @click="editing = !editing">{{ editing ? '收起登记' : '登记本机作品' }}</ElButton>
  </header>
  <section v-if="editing" class="panel">
    <h2>本机投稿配置</h2>
    <form @submit.prevent="prepare">
      <div class="row">
        <label class="field field-wide">素材根目录<input v-model="directory" :disabled="busy || !!pending" required
          placeholder="D:\Media\作品" @input="invalidate" /></label>
        <ElButton :disabled="busy || !!pending" @click="choose">选择素材根目录</ElButton>
      </div>
      <label class="field">配置路径（配置与素材分开时填写）<input v-model="manifestPath"
        :disabled="busy || !!pending" placeholder="留空时读取素材根目录中的 upload.yaml" @input="invalidate" /></label>
      <div class="row" style="margin-top: 12px">
        <ElButton native-type="submit" :loading="busy" :disabled="!!pending">预览配置</ElButton>
      </div>
    </form>
    <p v-if="formError" class="notice error" role="alert">{{ formError }}</p>
    <template v-if="preview">
      <hr /><ManifestView :manifest="preview.manifest" />
      <p class="break muted">配置版本 {{ preview.config_version }}</p>
      <p v-for="warning in preview.warnings" :key="warning" class="notice">{{ warning }}</p>
      <div v-if="!result && !pending" class="row">
        <ElButton :disabled="busy || !!registered" @click="register">仅登记作品</ElButton>
        <ElButton type="primary" :disabled="busy" @click="submit()">提交上传任务</ElButton>
      </div>
    </template>
    <div v-if="pending && !busy" class="row">
      <span class="muted">待核对作品 {{ shortId(pending.workId) }}</span>
      <ElButton @click="submit(true)">查回原请求</ElButton>
      <ElButton @click="submit()">重试原请求</ElButton>
      <ElButton @click="dismiss">结束本次操作</ElButton>
    </div>
    <p v-if="registered" class="notice" role="status">
      已登记 <RouterLink :to="detailPath('works', registered, embedded)">查看作品</RouterLink>
    </p>
    <div v-if="result" class="notice" role="status">
      已受理 {{ result.task_ids.length }} 个任务
      <ul><li v-for="id in result.task_ids" :key="id">
        <RouterLink :to="detailPath('tasks', id, embedded)">查看任务 {{ shortId(id) }}</RouterLink>
      </li></ul>
    </div>
  </section>
  <p v-if="error" class="notice error" role="alert">{{ error }} <ElButton size="small" @click="load(page)">重试</ElButton></p>
  <section class="panel">
    <p v-if="loading" class="muted" role="status">正在读取作品…</p>
    <div v-else-if="!works.length" class="empty">还没有登记作品</div>
    <div v-else class="table-scroll"><table>
      <thead><tr><th>作品</th><th>来源</th><th>素材数</th><th>目标平台</th><th>任务状态</th><th>登记时间</th></tr></thead>
      <tbody><tr v-for="work in works" :key="work.id">
        <td><RouterLink :to="detailPath('works', work.id, embedded)">{{ work.manifest.work.title || shortId(work.id) }}</RouterLink></td>
        <td>{{ work.source?.kind || work.source?.application || '本机' }}</td>
        <td>{{ Object.values(work.manifest.assets).reduce((total, group) => total + group.length, 0) }}</td>
        <td>{{ [...new Set(work.manifest.upload_units.flatMap(unit =>
          unit.targets.filter(target => target.enabled).map(target => platformName(target.platform))))].join('、') }}</td>
        <td><Status :value="work.status" /><small class="cell-detail">{{ Object.values(work.counts).reduce((a, b) => a + b, 0) }} 个任务</small></td>
        <td>{{ date(work.created_at) }}</td>
      </tr></tbody>
    </table></div>
    <div v-if="page || next" class="row pager">
      <ElButton :disabled="!page || loading" @click="load(page - 1)">上一页</ElButton>
      <ElButton :disabled="!next || loading" @click="more">下一页</ElButton>
    </div>
  </section>
</template>
