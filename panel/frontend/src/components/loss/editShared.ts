import type { BackendEditAction, BackendEditRule } from '../../api/client'

export const PREVIEW_DEBOUNCE_MS = 400
export const IPV4_RE = /^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}$/

export const inputCls = 'w-full px-3 py-2 bg-dark-800 border border-dark-700 rounded-lg text-sm text-dark-100 font-mono placeholder-dark-500 focus:outline-none focus:border-accent-500/50'

export function outcomeText(
  rule: BackendEditRule,
  action: BackendEditAction,
  noop: boolean,
  t: (key: string) => string,
): string {
  if (rule.outcome === 'skipped') return t(`loss.reason_${rule.reason}`)
  if (rule.outcome === 'merged') return t('loss.outcome_merged')
  if (noop) return t('loss.outcome_found')
  return action === 'delete' ? t('loss.outcome_deleted') : t('loss.outcome_changed')
}
