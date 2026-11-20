import type { Preview, Receipt } from '../../api/types'

const prefix = 'upload-platform.submission.v1.'
const uuid = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/
const revision = /^[0-9a-f]{64}$/

export interface PendingSubmission {
  key: string
  deployment: string
  workId: string
  configVersion: string
  body: string
}

export function saveSubmission(preview: Preview, deployment: string): PendingSubmission {
  const intent = {
    key: crypto.randomUUID(),
    deployment,
    workId: preview.work_id,
    configVersion: preview.config_version,
    body: JSON.stringify({
      preview_id: preview.preview_id,
      source: { application: 'dashboard', kind: 'local_import' },
    }),
  }
  localStorage.setItem(prefix + intent.key, JSON.stringify(intent))
  return intent
}

export function restoreSubmission(): PendingSubmission | undefined {
  for (let index = 0; index < localStorage.length; index++) {
    const name = localStorage.key(index)
    if (!name?.startsWith(prefix)) continue
    const key = name.slice(prefix.length)
    if (!uuid.test(key)) continue
    try {
      const intent: PendingSubmission = JSON.parse(localStorage.getItem(name) ?? 'null')
      if (intent?.key === key && uuid.test(intent.deployment) && uuid.test(intent.workId) &&
          revision.test(intent.configVersion) && typeof intent.body === 'string' &&
          uuid.test(JSON.parse(intent.body)?.preview_id)) return intent
    } catch { continue }
  }
}

export function forgetSubmission(intent: PendingSubmission) {
  localStorage.removeItem(prefix + intent.key)
}

export function matchesReceipt(value: Receipt, intent: PendingSubmission) {
  return value?.status === 'accepted' && value.work_id === intent.workId &&
    value.config_version === intent.configVersion && Array.isArray(value.task_ids) &&
    value.task_ids.length > 0 && value.task_ids.every(id => uuid.test(id))
}
