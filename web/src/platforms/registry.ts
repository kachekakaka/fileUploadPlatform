import { defineAsyncComponent, type Component } from 'vue'

interface PlatformModule {
  id: string
  name: string
  icon: string
  login?: Component
  result?: Component
}

export const registry: Record<string, PlatformModule> = {
  bilibili: {
    id: 'bilibili', name: 'Bilibili', icon: 'B',
    login: defineAsyncComponent(() => import('./bilibili/Login.vue')),
    result: defineAsyncComponent(() => import('./bilibili/Result.vue')),
  },
  fake: { id: 'fake', name: '模拟平台', icon: 'F' },
}

export const platformName = (id: string) => registry[id]?.name ?? id
export const platformReason = (code: string | null | undefined) => ({
  engine_not_configured: '尚未配置投稿引擎',
  engine_unavailable: '尚未配置投稿引擎',
  engine_incompatible: '引擎能力或版本不匹配',
  engine_interaction_incompatible: '该引擎不支持面板扫码',
  upload_not_open: '当前部署尚未开放普通投稿',
}[code ?? ''] ?? code ?? '')
