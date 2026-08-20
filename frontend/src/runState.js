export const BLOCKING_RUN_STATUSES = Object.freeze([
  'queued',
  'running',
  'waiting_approval',
  'retry_wait',
  'needs_reconciliation',
]);

export const STREAMABLE_RUN_STATUSES = Object.freeze([
  'queued',
  'running',
  'retry_wait',
]);

export function isRunBlocking(status) {
  return BLOCKING_RUN_STATUSES.includes(status);
}

export function isRunStreamable(status) {
  return STREAMABLE_RUN_STATUSES.includes(status);
}

export function isRunSteerable(status) {
  return ['queued', 'running', 'retry_wait'].includes(status);
}

export function runStatusLabel(status) {
  return {
    queued: '等待处理',
    running: '处理中',
    waiting_approval: '等待确认',
    retry_wait: '等待重试',
    needs_reconciliation: '需要人工处理',
    completed: '已完成',
    failed: '失败',
    cancelled: '已停止',
  }[status] || '空闲';
}
