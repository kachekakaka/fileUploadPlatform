export interface Page<T> { items: T[]; next_cursor?: string | null }
export interface Platform {
  id: string
  implemented: boolean
  configured: boolean
  enabled: boolean
  reason: string | null
  available_accounts: number
  engine_build: string | null
  capabilities: { login: boolean; login_reason?: string; multipart: boolean; remote_query: boolean }
}
export interface Account {
  id: string; platform: string; display_name: string; reference: string
  remote_user_id: string | null; enabled: boolean; availability: string; block_reasons: string[]
}
export interface Login {
  session_id: string; account_id: string | null; phase: string; outcome: string | null; revision: number
  challenge: { image_path: string; challenge_id: string; deadline_at: string } | null
  can_cancel: boolean; can_resume: boolean; can_retry: boolean
  account_summary: Account | null; error: { message: string } | null
}
export interface Target { platform: string; account: string; enabled: boolean; options: Record<string, unknown> }
export interface Manifest {
  work: { id: string; title?: string; ready: boolean }
  assets: Record<string, { id: string; path: string }[]>
  upload_units: { id: string; mode: string; videos: string[]; targets: Target[] }[]
}
export interface Work {
  id: string; manifest: Manifest; directory: string; manifest_path?: string
  config_version?: string; source?: { application: string; kind?: string }
  created_at: number; status: string; counts: Record<string, number>
}
export interface Preview {
  preview_id: string; work_id: string; config_version: string; expires_at: string
  manifest: Manifest; warnings: string[]
}
export interface Receipt {
  work_id: string; task_ids: string[]; status: 'accepted'; config_version: string
}
export interface Snapshot {
  target: Target; metadata: { title: string; description: string }
  parts: { video_id: string; title: string }[]; assets: { relative_path: string; role: string }[]
  directory: string; manifest?: Manifest; config_version?: string; manifest_path?: string
}
interface BilibiliProgress {
  phase: string; details?: { code?: number } | null
}
export interface Task {
  id: string; status: string; work_id: string; account_id: string; snapshot: Snapshot
  version: number; submission_round: number; attempt_count: number; created_at: number
  scheduled_at: number; next_attempt_at: number; execution_type: string
  available_actions?: Record<string, { allowed: boolean; reason: string | null }>
  receipt?: Record<string, unknown> | null
  evidence_ref?: string
  attempt?: {
    result?: { history?: BilibiliProgress[] } | null
    execution?: { observed_events?: Record<string, BilibiliProgress> }
  } | null
}
export interface TaskEvent {
  id: string; sequence: number; type: string; created_at: number; payload: Record<string, unknown>
}
export interface Overview {
  version: string; api: string; database: string; data_directory: string
  manifest_roots: string[]; asset_roots: string[]
  workers: { id: string; pid: number; heartbeat_at: number; active: boolean }[]
  worker_active: boolean; accounts: number; terminal_tasks: number
  task_counts: Record<string, number>; recent_tasks: Task[]; incidents: Task[]
  platforms: Platform[]
}
export interface ScheduleConfig {
  parent_directory: string; kind: 'daily' | 'weekly'; timezone: string; local_time: string
  weekday: number | null; enabled: boolean
}
export interface RunSummary {
  discovered: number; created_tasks: number; reused_tasks: number; skipped: number
  failed: number; error: string | null
}
export interface Schedule {
  id: string; config: ScheduleConfig; version: number; enabled: boolean; next_fire_at: number | null
  recent_run: (RunSummary & { id: string; planned_at: number; status: string }) | null
}
