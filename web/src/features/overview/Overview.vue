<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { get } from '../../api/client'
import type { Overview } from '../../api/types'
import { platformName, platformReason } from '../../platforms/registry'
import { date, message } from '../../shared/display'
import TaskTable from '../../shared/TaskTable.vue'

const data = ref<Overview>()
const busy = ref(false)
const error = ref('')
const metrics = computed(() => [
  ['等待执行', data.value?.task_counts.QUEUED ?? 0], ['执行中', data.value?.task_counts.RUNNING ?? 0],
  ['等待重试', data.value?.task_counts.RETRY_WAIT ?? 0], ['结果待核验', data.value?.task_counts.UNCERTAIN ?? 0],
  ['已结束', data.value?.terminal_tasks ?? 0], ['账号', data.value?.accounts ?? 0],
])
async function load() {
  busy.value = true
  error.value = ''
  try { data.value = await get<Overview>('/system/overview') }
  catch (failure) { error.value = message(failure) }
  finally { busy.value = false }
}
onMounted(load)
</script>
<template>
  <header class="page-head"><div><h1>概览</h1><p>本机服务状态与近期投稿</p></div>
    <ElButton :loading="busy" @click="load">刷新</ElButton>
  </header>
  <p v-if="error" class="notice error" role="alert">{{ error }}</p>
  <p v-if="busy && !data" class="muted" role="status">正在读取概览…</p>
  <template v-if="data">
    <div class="grid">
      <section class="panel"><h2>运行服务</h2><dl class="kv">
        <dt>API</dt><dd>{{ data.api === 'ready' ? '正常' : data.api }}</dd>
        <dt>数据库</dt><dd>{{ data.database === 'ready' ? '正常' : data.database }}</dd>
        <dt>Worker</dt><dd>{{ data.worker_active ? '心跳正常' : '无活动心跳' }}
          <small v-for="worker in data.workers.filter(w => w.active)" :key="worker.id" class="cell-detail">{{ date(worker.heartbeat_at) }}</small>
        </dd>
      </dl></section>
      <section class="panel"><h2>平台</h2><dl class="kv">
        <template v-for="platform in data.platforms" :key="platform.id">
          <dt>{{ platformName(platform.id) }}</dt>
          <dd>{{ platform.enabled ? '可受理' : platformReason(platform.reason) }}
            <small v-if="platform.engine_build" class="cell-detail">{{ platform.engine_build }}</small></dd>
        </template>
      </dl></section>
    </div>
    <section class="panel"><h2>任务统计</h2>
      <div class="metrics"><div v-for="[label, count] in metrics" :key="label" class="metric">
        <small>{{ label }}</small><b>{{ count }}</b></div></div>
    </section>
    <section class="panel"><h2>近期任务</h2><TaskTable :tasks="data.recent_tasks" /></section>
    <section v-if="data.incidents.length" class="panel"><h2>近期异常</h2><TaskTable :tasks="data.incidents" /></section>
  </template>
</template>
