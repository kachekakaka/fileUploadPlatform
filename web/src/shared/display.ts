export const states: Record<string, string> = {
  QUEUED: '等待执行', CLAIMED: '领取中', VALIDATING: '校验中', RUNNING: '执行中',
  RETRY_WAIT: '等待重试', RECOVERY_PENDING: '等待恢复', UNCERTAIN: '结果待核验',
  SUCCEEDED: '已成功', FAILED: '失败', CANCELLED: '已取消', EXPIRED: '已过期',
  NOT_SUBMITTED: '未提交', NEEDS_ATTENTION: '待核验', IN_PROGRESS: '进行中',
  COMPLETED: '派发完成', COMPLETED_WITH_ERRORS: '派发有错误', DISPATCHING: '派发中',
  PENDING: '待派发', SKIPPED: '已跳过', PARTIAL_SUCCESS: '部分成功',
}
export const state = (value: string) => states[value] ?? value
export const date = (value?: number | null) => value == null ? '—' :
  new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'medium' })
    .format(new Date(value * 1000))
export const message = (error: unknown) => error instanceof Error ? error.message : String(error)
export const shortId = (value: string) => value.slice(0, 8)
