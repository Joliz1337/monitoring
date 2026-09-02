import { Activity, Loader2, RefreshCw } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { ExitProxySettings, ExitProxyStatus } from '../../api/client'
import { formatTimeAgo } from '../../utils/format'
import { Tooltip } from '../ui/Tooltip'

interface Props {
  status: ExitProxyStatus | null
  settings: ExitProxySettings | null
  onRefresh: () => void
  refreshing: boolean
}

export default function WorkerStatusCard({ status, settings, onRefresh, refreshing }: Props) {
  const { t } = useTranslation()
  const running = Boolean(status?.running && settings?.enabled)
  const lastTick = status?.last_tick_at ?? null

  return (
    <div className="card p-5 space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-dark-200 font-medium">
          <Activity className="w-4 h-4" />
          {t('exit_proxy.worker_title')}
        </div>
        <button
          onClick={onRefresh}
          disabled={refreshing}
          className="flex items-center gap-1.5 bg-dark-800 hover:bg-dark-700 text-dark-300 rounded-lg px-3 py-1.5 text-xs transition-colors disabled:opacity-50"
        >
          {refreshing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
          {t('exit_proxy.refresh')}
        </button>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <div className="bg-dark-800/50 rounded-lg p-3">
          <p className="text-xs text-dark-500 mb-1">{t('exit_proxy.worker')}</p>
          <p className="text-sm text-dark-200 flex items-center gap-2">
            <span className={`w-2 h-2 rounded-full ${running ? 'bg-green-400 animate-pulse' : 'bg-dark-500'}`} />
            {running ? t('exit_proxy.worker_running') : t('exit_proxy.worker_stopped')}
          </p>
        </div>
        <div className="bg-dark-800/50 rounded-lg p-3">
          <p className="text-xs text-dark-500 mb-1">{t('exit_proxy.last_cycle')}</p>
          <Tooltip label={lastTick ? new Date(lastTick).toLocaleString() : ''} disabled={!lastTick}>
            <p className="text-sm text-dark-200">{lastTick ? formatTimeAgo(lastTick) : t('exit_proxy.never')}</p>
          </Tooltip>
        </div>
        <div className="bg-dark-800/50 rounded-lg p-3">
          <p className="text-xs text-dark-500 mb-1">{t('exit_proxy.local_socks')}</p>
          <p className="text-sm text-dark-200 font-mono">127.0.0.1:{settings?.port ?? '—'}</p>
        </div>
        <div className="bg-dark-800/50 rounded-lg p-3">
          <p className="text-xs text-dark-500 mb-1">{t('exit_proxy.interval')}</p>
          <p className="text-sm text-dark-200">{settings ? t('exit_proxy.interval_value', { count: settings.check_interval_min }) : '—'}</p>
        </div>
      </div>

      {status?.last_error && (
        <div className="bg-red-500/10 border border-red-500/20 rounded-lg p-3">
          <p className="text-xs text-red-400 break-all">{status.last_error}</p>
        </div>
      )}
    </div>
  )
}
