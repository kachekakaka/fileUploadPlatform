import { ref } from 'vue'

const prefix = '/api/v1'
const deploymentHeader = 'X-Upload-Platform-Deployment'
const embedded = location.pathname.startsWith('/embed/')
const bindings = new URLSearchParams(location.search).getAll('deployment')
export const embeddedDeployment = embedded && bindings.length === 1 &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(bindings[0])
  ? bindings[0] : undefined
export const bindingError = ref(embedded && !embeddedDeployment ? '此 MuMu 工作区尚未绑定上传平台' : '')
let csrf: Promise<string> | undefined

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message)
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  if (embedded) {
    if (!embeddedDeployment || bindingError.value) throw new ApiError(409, 'binding', bindingError.value)
    if (headers.has(deploymentHeader) && headers.get(deploymentHeader) !== embeddedDeployment) {
      throw new ApiError(409, 'binding', '原请求属于其他上传部署，请核对原单')
    }
    headers.set(deploymentHeader, embeddedDeployment)
  }
  let response: Response
  try {
    response = await fetch(prefix + path, { ...options, headers, cache: 'no-store', credentials: 'omit' })
  } catch {
    throw new ApiError(0, 'network_error', '连接上传平台失败。请检查服务是否还在运行。')
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    if (embedded && body.code === 'binding') bindingError.value = body.message
    throw new ApiError(response.status, body.code ?? 'http_error',
      body.message ?? `请求失败（${response.status}）`)
  }
  if (embedded && response.headers.get(deploymentHeader) !== embeddedDeployment) {
    bindingError.value = '上传平台未确认此 MuMu 工作区的部署编号'
    throw new ApiError(409, 'binding', bindingError.value)
  }
  return response.json()
}

export function get<T>(path: string) {
  return request<T>(path)
}

function session() {
  csrf ??= get<{ csrf_token: string }>('/dashboard/session')
    .then(value => value.csrf_token)
    .catch(error => { csrf = undefined; throw error })
  return csrf
}

export async function manage<T>(
  path: string, body: object, method = 'POST', key: string = crypto.randomUUID(),
) {
  for (let attempt = 0; ; attempt++) {
    const token = await session()
    try {
      return await request<T>(path, {
        method,
        body: JSON.stringify(body),
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key': key,
          'X-Upload-Platform-CSRF': token,
        },
      })
    } catch (error) {
      if (attempt || !(error instanceof ApiError) || error.status !== 403) throw error
      csrf = undefined
    }
  }
}

export function previewSubmission<T>(body: object, deployment: string) {
  return request<T>('/submissions/preview', {
    method: 'POST',
    body: JSON.stringify(body),
    headers: { 'Content-Type': 'application/json', [deploymentHeader]: deployment },
  })
}

export function replaySubmission<T>(body: string, key: string, deployment: string) {
  const headers = new Headers({ 'Content-Type': 'application/json' })
  headers.set(deploymentHeader, deployment)
  headers.set('Idempotency-Key', key)
  return request<T>('/submissions', { method: 'POST', headers, body })
}

export function receipt<T>(key: string, deployment: string) {
  return request<T>('/submissions/result', {
    headers: { 'Idempotency-Key': key, [deploymentHeader]: deployment },
  })
}

export function eventStream(task: string, after: number) {
  return new EventSource(boundResource(`${prefix}/tasks/${task}/events/stream?after_sequence=${after}`))
}

export function boundResource(path: string) {
  if (!embedded) return path
  if (!embeddedDeployment || bindingError.value) throw new ApiError(409, 'binding', bindingError.value)
  const url = new URL(path, location.origin)
  url.searchParams.set('deployment', embeddedDeployment)
  return url.pathname + url.search
}
