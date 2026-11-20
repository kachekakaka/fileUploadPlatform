import type { Router } from 'vue-router'
import { detailPath } from '../app/paths'

const parentOrigin = 'http://127.0.0.1:8000'
const protocol = 'upload-platform-embed'
const identifier = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const embedded = () => window.parent !== window && location.pathname.startsWith('/embed/')

type UpdateType = 'task-updated' | 'account-updated'
export function notify(type: UpdateType, id: string, status: string) {
  if (embedded() && identifier.test(id)) send(type, { id, status })
}

function send(type: 'ready' | UpdateType, payload: object) {
  window.parent.postMessage({ protocol, version: 1, type, payload }, parentOrigin)
}

export async function startEmbed(router: Router) {
  await router.isReady()
  if (!embedded()) return
  window.addEventListener('message', event => {
    if (event.origin !== parentOrigin || event.source !== window.parent) return
    const data = event.data
    if (!data || typeof data !== 'object' || data.protocol !== protocol || data.version !== 1) return
    const payload = data.payload
    if (!payload || typeof payload !== 'object') return
    if (data.type === 'open-task' && typeof payload.task_id === 'string' && identifier.test(payload.task_id)) {
      void router.push(detailPath('tasks', payload.task_id, true))
    }
    if (data.type === 'open-account' && typeof payload.account_id === 'string' && identifier.test(payload.account_id)) {
      void router.push({ path: '/embed/accounts', query: { account: payload.account_id } })
    }
  })
  send('ready', {})
}
