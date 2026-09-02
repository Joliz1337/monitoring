import { useEffect, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useExitProxyStore } from '../../stores/exitProxyStore'
import { BADGE, TONE } from './badges'

const LIMITS = [100, 500]
const KIND_TONE: Record<string, keyof typeof TONE> = {
  switched: 'yellow',
  manual_switch: 'dark',
  no_healthy: 'red',
  recovered: 'green',
  self_test_failed: 'red',
  self_test_recovered: 'green',
  check_failed: 'orange',
  started: 'dark',
  stopped: 'dark',
}

function label(value: string | null): string {
  if (!value) return '—'
  return value === 'warp' ? 'WARP' : value.replace(/^ip:/, '')
}

export default function LogTab() {
  const { t } = useTranslation()
  const log = useExitProxyStore(s => s.log)
  const fetchLog = useExitProxyStore(s => s.fetchLog)
  const [limit, setLimit] = useState(LIMITS[0])

  useEffect(() => {
    fetchLog(limit)
  }, [fetchLog, limit])

  return (
    <div className="card p-5 space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <h3 className="text-sm font-medium text-dark-200">{t('exit_proxy.log_title')}</h3>
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1 bg-dark-800/60 border border-dark-700 rounded-lg p-0.5">
            {LIMITS.map(value => (
              <button
                key={value}
                onClick={() => setLimit(value)}
                className={`px-3 py-1 rounded-md text-xs font-medium transition-colors ${limit === value ? 'bg-accent-500 text-white' : 'text-dark-400 hover:text-dark-200'}`}
              >
                {value}
              </button>
            ))}
          </div>
          <button onClick={() => fetchLog(limit)} className="flex items-center gap-1.5 bg-dark-800 hover:bg-dark-700 text-dark-300 rounded-lg px-3 py-1.5 text-xs transition-colors">
            <RefreshCw className="w-3.5 h-3.5" />
            {t('exit_proxy.refresh')}
          </button>
        </div>
      </div>

      {log.length === 0 ? (
        <p className="text-xs text-dark-500">{t('exit_proxy.log_empty')}</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-dark-500 text-xs border-b border-dark-800">
                <th className="text-left py-2 pr-3 font-medium">{t('exit_proxy.col_time')}</th>
                <th className="text-left py-2 pr-3 font-medium">{t('exit_proxy.col_server')}</th>
                <th className="text-left py-2 pr-3 font-medium">{t('exit_proxy.col_event')}</th>
                <th className="text-left py-2 pr-3 font-medium">{t('exit_proxy.col_from_to')}</th>
                <th className="text-left py-2 font-medium">{t('exit_proxy.col_reason')}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-dark-800/60">
              {log.map(entry => (
                <tr key={entry.id}>
                  <td className="py-2 pr-3 text-dark-400 whitespace-nowrap text-xs">{entry.at ? new Date(entry.at).toLocaleString() : '—'}</td>
                  <td className="py-2 pr-3 text-dark-200">{entry.server_name}</td>
                  <td className="py-2 pr-3">
                    <span className={`${BADGE} ${TONE[KIND_TONE[entry.kind] ?? 'dark']}`}>
                      {t(`exit_proxy.event_${entry.kind}`, { defaultValue: entry.kind })}
                    </span>
                  </td>
                  <td className="py-2 pr-3 font-mono text-xs text-dark-300 whitespace-nowrap">
                    {entry.from || entry.to ? `${label(entry.from)} → ${label(entry.to)}` : '—'}
                  </td>
                  <td className="py-2 text-xs text-dark-400 break-all">
                    {entry.reason ? t(`exit_proxy.reason_${entry.reason}`, { defaultValue: entry.reason }) : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
