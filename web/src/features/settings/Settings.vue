<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { get } from '../../api/client'
import type { Overview } from '../../api/types'
import { platformName, platformReason } from '../../platforms/registry'
import { date, message } from '../../shared/display'

const data = ref<Overview>()
const busy = ref(false)
const error = ref('')
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
  <header class="page-head"><div><h1>设置</h1><p>查看本机运行环境与引擎配置</p></div>
    <ElButton :loading="busy" @click="load">刷新诊断</ElButton>
  </header>
  <p v-if="error" class="notice error" role="alert">{{ error }}</p>
  <p v-if="busy && !data" class="muted" role="status">服务诊断读取中…</p>
  <template v-if="data">
    <section class="panel"><h2>运行环境</h2>
      <dl class="kv">
        <dt>服务版本</dt><dd>{{ data.version }}</dd>
        <dt>API / 数据库</dt><dd>{{ data.api }} / {{ data.database }}</dd>
        <dt>数据目录</dt><dd>{{ data.data_directory }}</dd>
        <dt>配置目录</dt><dd>{{ data.manifest_roots.join('、') || '未配置' }}</dd>
        <dt>素材根目录</dt><dd>{{ data.asset_roots.join('、') || '未配置' }}</dd>
      </dl>
    </section>
    <section class="panel"><h2>Worker 心跳</h2>
      <table v-if="data.workers.length"><thead><tr><th>进程</th><th>状态</th><th>最后心跳</th></tr></thead>
        <tbody><tr v-for="worker in data.workers" :key="worker.id"><td>{{ worker.pid }}</td>
          <td>{{ worker.active ? '在线' : '不活跃' }}</td><td>{{ date(worker.heartbeat_at) }}</td></tr></tbody></table>
      <div v-else class="empty">尚未收到 Worker 心跳</div>
    </section>
    <section class="panel"><h2>平台引擎</h2><div class="table-scroll"><table>
      <thead><tr><th>平台</th><th>已实现</th><th>已配置</th><th>已开放</th><th>可用账号</th><th>引擎与诊断</th></tr></thead>
      <tbody><tr v-for="platform in data.platforms" :key="platform.id">
        <td>{{ platformName(platform.id) }}</td><td>{{ platform.implemented ? '是' : '否' }}</td>
        <td>{{ platform.configured ? '是' : '否' }}</td><td>{{ platform.enabled ? '是' : '否' }}</td>
        <td>{{ platform.available_accounts }}</td><td>{{ platform.engine_build || platformReason(platform.reason) || '内置' }}</td>
      </tr></tbody>
    </table></div></section>
  </template>
</template>
