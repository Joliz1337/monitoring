import { motion } from 'framer-motion'
import { useTranslation } from 'react-i18next'
import { CheckCircle2, XCircle, Loader2, X, AlertTriangle } from 'lucide-react'
import { WildcardDeployResult } from '../../api/client'
import { BulkStreamState } from '../../hooks/useBulkStream'

interface WildcardDeployProgressProps {
  progress: BulkStreamState<WildcardDeployResult>
  onClose: () => void
  onCancel: () => void
}

export function WildcardDeployProgress({ progress, onClose, onCancel }: WildcardDeployProgressProps) {
  const { t } = useTranslation()
  const { rows, total, active, finished, error } = progress

  const resolved = rows.filter(r => r.state !== 'running').length
  const okCount = rows.filter(r => r.state === 'success').length
  const failedCount = rows.filter(r => r.state === 'error').length
  const percent = total > 0 ? (resolved / total) * 100 : 0
  const allOk = finished && failedCount === 0
  const allFailed = finished && okCount === 0

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: 10 }}
      className="p-4 bg-dark-900/60 border border-dark-700 rounded-xl"
    >
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-3">
          <h3 className="text-sm font-semibold text-dark-100">{t('wildcard_ssl.deploy_progress_title')}</h3>
          <span className={`px-2.5 py-0.5 rounded-full text-xs font-medium ${
            !finished ? 'bg-accent-500/15 text-accent-400'
            : allOk ? 'bg-emerald-500/15 text-emerald-400'
            : allFailed ? 'bg-red-500/15 text-red-400'
            : 'bg-orange-500/15 text-orange-400'
          }`}>
            {okCount} / {total}
          </span>
        </div>
        <div className="flex items-center gap-3">
          {active && (
            <button onClick={onCancel} className="text-dark-400 hover:text-dark-200 text-xs">
              {t('wildcard_ssl.deploy_cancel')}
            </button>
          )}
          {!active && (
            <button onClick={onClose} className="text-dark-500 hover:text-dark-300">
              <X className="w-4 h-4" />
            </button>
          )}
        </div>
      </div>

      {error && (
        <div className="flex items-start gap-2 p-3 bg-red-500/10 border border-red-500/20 rounded-lg mb-3">
          <AlertTriangle className="w-4 h-4 text-red-400 mt-0.5 shrink-0" />
          <p className="text-sm text-red-300">{error}</p>
        </div>
      )}

      <div className="flex items-center gap-2 mb-3">
        <div className="flex-1 h-1.5 rounded-full bg-dark-800 overflow-hidden">
          <div
            className={`h-full rounded-full transition-all ${
              allOk ? 'bg-emerald-400' : allFailed ? 'bg-red-400'
              : finished ? 'bg-orange-400' : 'bg-accent-400'
            }`}
            style={{ width: `${percent}%` }}
          />
        </div>
        <span className="text-xs text-dark-500 shrink-0 tabular-nums">{resolved} / {total}</span>
      </div>

      <div className="space-y-1.5 max-h-[480px] overflow-y-auto pr-1">
        {rows.map(row => {
          const reload = row.result?.reload_result
          const note = row.result?.message || row.result?.error
          return (
            <div key={row.server_id} className="text-sm">
              <div className="flex items-center gap-2">
                {row.state === 'running' && <Loader2 className="w-4 h-4 text-accent-400 animate-spin shrink-0" />}
                {row.state === 'success' && <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0" />}
                {row.state === 'error' && <XCircle className="w-4 h-4 text-red-400 shrink-0" />}
                <span className="text-dark-200 font-medium">{row.server_name}</span>
                {row.state !== 'running' && note && (
                  <span className={`text-xs truncate ${row.state === 'error' ? 'text-red-400' : 'text-dark-500'}`}>
                    {note}
                  </span>
                )}
              </div>
              {reload && reload.exit_code !== 0 && (
                <p className="pl-6 mt-0.5 text-xs text-dark-500 break-all">
                  {reload.stderr || reload.stdout}
                </p>
              )}
            </div>
          )
        })}
      </div>
    </motion.div>
  )
}
