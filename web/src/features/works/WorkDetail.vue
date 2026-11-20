<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { get } from '../../api/client'
import type { Page, Task, Work } from '../../api/types'
import { date, message } from '../../shared/display'
import Status from '../../shared/Status.vue'
import TaskTable from '../../shared/TaskTable.vue'
import ManifestView from './ManifestView.vue'

const route = useRoute()
const embedded = route.path.startsWith('/embed/')
const id = String(route.params.id)
const work = ref<Work>()
const tasks = ref<Task[]>([])
const next = ref<string | null>()
const error = ref('')
const loading = ref(true)

async function load(more = false) {
  loading.value = true
  error.value = ''
  try {
    const path = '/works/' + id
    const [item, page] = await Promise.all([
      more && work.value ? work.value : get<Work>(path),
      get<Page<Task>>(path + '/tasks?limit=25' + (more && next.value ? '&cursor=' + next.value : '')),
    ])
    work.value = item
    tasks.value = more ? [...tasks.value, ...page.items] : page.items
    next.value = page.next_cursor
  } catch (failure) { error.value = message(failure) }
  finally { loading.value = false }
}
onMounted(() => load())
</script>
<template>
  <RouterLink class="back" :to="embedded ? '/embed/works' : '/works'">← 作品</RouterLink>
  <header class="page-head"><div><h1>{{ work?.manifest.work.title || '作品详情' }}</h1><p>{{ id }}</p></div>
    <Status v-if="work" :value="work.status" />
  </header>
  <p v-if="error" class="notice error" role="alert">{{ error }} <ElButton @click="load()">重试</ElButton></p>
  <p v-if="loading" class="muted" role="status">正在读取作品…</p>
  <template v-if="work">
    <section class="panel"><h2>登记信息</h2><dl class="kv">
      <dt>登记时间</dt><dd>{{ date(work.created_at) }}</dd>
      <dt>来源</dt><dd>{{ work.source?.application || '本机' }} {{ work.source?.kind }}</dd>
      <dt>配置版本</dt><dd>{{ work.config_version || '—' }}</dd>
      <dt>配置路径</dt><dd>{{ work.manifest_path || '—' }}</dd>
      <dt>素材根目录</dt><dd>{{ work.directory }}</dd>
    </dl></section>
    <section class="panel"><h2>登记时的配置</h2><ManifestView :manifest="work.manifest" /></section>
    <section class="panel"><h2>任务记录</h2><TaskTable :tasks="tasks" :embedded="embedded" />
      <ElButton v-if="next" :loading="loading" @click="load(true)">更多任务</ElButton>
    </section>
  </template>
</template>
