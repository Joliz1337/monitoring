import { useTranslation } from 'react-i18next'
import { CheckCircle2, Loader2, Square, XCircle } from 'lucide-react'
import type { SshCheckFailure, SshCheckState } from '../../api/client'

interface Props {
  check: SshCheckState
  busy: boolean
  onCancel: () => void
  onAcceptHostKeys: (serverIds: number[]) => void
}

/** Ход идущей проверки доступа или итог последней: сколько серверов пустили панель и где не вышло */
export default function SshAccessCheck({ check, busy, onCancel, onAcceptHostKeys }: Props) {
  const { t } = useTranslation()

  const okCount = check.counts.ok ?? 0
  const failedCount = check.done - okCount
  const hiddenFailed = failedCount - check.failed.length
  const changedKeys = check.failed.filter(row => row.status === 'host_key_changed').map(row => row.server_id)

  const reason = (row: SshCheckFailure) => {
    const level = row.login_level ? ` (${t(`sshAccess.from_${row.login_level}`)})` : ''
    return `${t(`sshAccess.status_${row.status}`)}${level}`
  }

  return (
    <div className="space-y-2 rounded-lg border border-dark-700/40 bg-dark-900/40 p-3">
      <div className="flex items-center gap-x-4 gap-y-1 flex-wrap text-sm">
        {check.status === 'running' && (
          <>
            <span className="flex items-center gap-1.5 text-dark-300">
              <Loader2 className="w-4 h-4 animate-spin" />
              {t('sshAccess.check_progress', { done: check.done, total: check.total })}
            </span>
            <button type="button" onClick={onCancel} disabled={busy} className="btn btn-secondary text-xs py-1 px-2">
              <Square className="w-3 h-3" />
              {t('sshAccess.check_cancel')}
            </button>
          </>
        )}
        {check.status === 'cancelled' && <span className="text-dark-400">{t('sshAccess.check_cancelled')}</span>}
        <span className="flex items-center gap-1.5 text-success">
          <CheckCircle2 className="w-4 h-4" />
          {t('sshAccess.check_ok', { count: okCount })}
        </span>
        {check.ok_via_proxy > 0 && (
          <span className="text-dark-400">{t('sshAccess.check_via_proxy', { count: check.ok_via_proxy })}</span>
        )}
        {failedCount > 0 && (
          <span className="flex items-center gap-1.5 text-danger">
            <XCircle className="w-4 h-4" />
            {t('sshAccess.check_failed', { count: failedCount })}
          </span>
        )}
      </div>

      {changedKeys.length > 1 && (
        <button
          type="button"
          onClick={() => onAcceptHostKeys(changedKeys)}
          disabled={busy}
          className="btn btn-secondary text-xs py-1 px-2"
        >
          {t('sshAccess.accept_host_keys', { count: changedKeys.length })}
        </button>
      )}

      {check.failed.length > 0 && (
        <ul className="space-y-1 max-h-56 overflow-y-auto pr-1 text-sm">
          {check.failed.map(row => (
            <li key={row.server_id} className="flex items-baseline gap-2 flex-wrap">
              <span className="text-dark-200 font-medium">{row.name}</span>
              <span className="text-danger">{reason(row)}</span>
              {row.detail && <span className="text-xs text-dark-500 break-all">{row.detail}</span>}
              {row.status === 'host_key_changed' && (
                <button
                  type="button"
                  onClick={() => onAcceptHostKeys([row.server_id])}
                  disabled={busy}
                  className="text-xs text-accent-400 hover:underline disabled:opacity-50"
                >
                  {t('sshAccess.accept_host_key')}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {hiddenFailed > 0 && <p className="text-xs text-dark-500">{t('sshAccess.and_more', { count: hiddenFailed })}</p>}
    </div>
  )
}
