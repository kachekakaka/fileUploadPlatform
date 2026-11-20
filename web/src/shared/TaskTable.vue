<script setup lang="ts">
import type { Task } from '../api/types'
import { detailPath } from '../app/paths'
import { platformName } from '../platforms/registry'
import { date, shortId } from './display'
import Status from './Status.vue'
defineProps<{ tasks: Task[]; embedded?: boolean }>()
</script>
<template>
  <div class="table-scroll">
    <table v-if="tasks.length">
      <thead><tr><th>稿件</th><th>平台</th><th>状态</th><th>创建时间</th></tr></thead>
      <tbody>
        <tr v-for="task in tasks" :key="task.id">
          <td><RouterLink :to="detailPath('tasks', task.id, embedded)">
            {{ task.snapshot.metadata.title || shortId(task.id) }}
          </RouterLink><small class="cell-detail">{{ shortId(task.id) }}</small></td>
          <td>{{ platformName(task.snapshot.target.platform) }}</td>
          <td><Status :value="task.status" /></td><td>{{ date(task.created_at) }}</td>
        </tr>
      </tbody>
    </table>
    <div v-else class="empty">没有符合条件的任务</div>
  </div>
</template>
