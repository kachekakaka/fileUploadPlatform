const prefix = '/api/v1'
const deploymentHeader = 'X-Upload-Platform-Deployment'
let csrf: Promise<string> | undefined

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message)
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  let response: Response
  try {
    response = await fetch(prefix + path, { ...options, cache: 'no-store', credentials: 'omit' })
  } catch {
    throw new ApiError(0, 'network_error', '连接上传平台失败。请检查服务是否还在运行。')
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new ApiError(response.status, body.code ?? 'http_error',
      body.message ?? `请求失败（${response.status}）`)
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
  return new EventSource(`${prefix}/tasks/${task}/events/stream?after_sequence=${after}`)
}
