<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { bindingError } from '../api/client'
const route = useRoute()
const embedded = computed(() => route.path.startsWith('/embed/'))
const navigation = [
  { path: '/overview', label: '概览', icon: '◫' }, { path: '/accounts', label: '账号', icon: '◎' },
  { path: '/works', label: '作品', icon: '▤' }, { path: '/tasks', label: '任务', icon: '≡' },
  { path: '/schedules', label: '定时规则', icon: '◷' }, { path: '/settings', label: '设置', icon: '⚙' },
]
const active = (path: string) => route.path === path || route.path.startsWith(path + '/') ||
  (path === '/overview' && route.path === '/')
</script>
<template>
  <div class="shell" :class="{ embedded }">
    <aside v-if="!embedded" class="sidebar">
      <RouterLink to="/" class="brand"><span aria-hidden="true" class="brand-mark">↑</span>文件上传平台</RouterLink>
      <nav aria-label="主导航">
        <RouterLink v-for="item in navigation" :key="item.path" :to="item.path" :class="{ active: active(item.path) }">
          <span aria-hidden="true" class="nav-icon">{{ item.icon }}</span>{{ item.label }}
        </RouterLink>
      </nav>
    </aside>
    <main class="content">
      <p v-if="embedded && bindingError" class="notice error" role="alert">{{ bindingError }}</p>
      <RouterView v-else v-slot="{ Component }"><component :is="Component" :key="route.path" /></RouterView>
    </main>
  </div>
</template>
