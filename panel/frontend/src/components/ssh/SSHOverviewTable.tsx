import { useCallback, useEffect, useRef, useState } from 'react'
import { motion } from 'framer-motion'
import { useTranslation } from 'react-i18next'
import { RefreshCw, Loader2, AlertTriangle, ShieldAlert } from 'lucide-react'
import { Server as ServerType, SSHStatus, SSHStatusEvent, sshBulkStreamUrls } from '../../api/client'
import { streamNdjson, StreamUnauthorizedError } from '../../utils/ndjsonStream'

interface OverviewRow {
  server_id: number
  server_name: string
  state: 'loading' | 'ok' | 'offline' | 'outdated'
  status?: SSHStatus
  error?: string
}

interface SSHOverviewTableProps {
  servers: ServerType[]
  onOpenServer: (id: number) => void
}

export function SSHOverviewTable({ servers, onOpenServer }: SSHOverviewTableProps) {
  const { t } = useTranslation()
  const [rows, setRows] = useState<OverviewRow[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  const run = useCallback(async () => {
    if (servers.length === 0) return
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    setLoading(true)
    setError(null)
    setRows(servers.map(s => ({ server_id: s.id, server_name: s.name, state: 'loading' as const })))

    try {
      await streamNdjson<SSHStatusEvent>(
        sshBulkStreamUrls.status,
        { server_ids: servers.map(s => s.id) },
        ev => {
          if (ev.type === 'result') {
            setRows(prev => prev.map(r =>
              r.server_id === ev.server_id
                ? {
                    server_id: ev.server_id,
                    server_name: ev.server_name,
                    state: ev.reachable ? 'ok' : ev.outdated ? 'outdated' : 'offline',
                    status: ev.status,
                    error: ev.error,
                  }
                : r,
            ))
          }
        },
        controller.signal,
      )
    } catch (e) {
      if (!controller.signal.aborted && !(e instanceof StreamUnauthorizedError)) {
        setError(e instanceof Error ? e.message : String(e))
      }
    } finally {
      setLoading(false)
    }
  }, [servers])

  useEffect(() => {
    run()
    return () => abortRef.current?.abort()
  }, [run])

  const authLabel = (method?: string) => {
    if (method === 'both') return t('ssh_security.overview_auth_both')
    if (method === 'key') return t('ssh_security.overview_auth_key')
    if (method === 'password') return t('ssh_security.overview_auth_password')
    return t('ssh_security.overview_auth_none')
  }

  return (
    <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} className="card">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-lg font-semibold text-dark-100">{t('ssh_security.overview_title')}</h3>
        <button
          onClick={run}
          disabled={loading}
          className="btn btn-secondary text-xs px-3 py-1.5 flex items-center gap-1.5"
        >
          {loading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
          {t('ssh_security.overview_refresh')}
        </button>
      </div>

      {error && (
        <div className="flex items-start gap-2 p-3 bg-red-500/10 border border-red-500/20 rounded-lg mb-3">
          <AlertTriangle className="w-4 h-4 text-red-400 mt-0.5 shrink-0" />
          <p className="text-sm text-red-300">{error}</p>
        </div>
      )}

      {rows.length === 0 ? (
        <p className="text-dark-400 text-sm text-center py-6">{t('ssh_security.no_servers')}</p>
      ) : (
        <div className="overflow-x-auto">
          <div className="min-w-[640px]">
            <div className="grid grid-cols-[1.6fr_0.8fr_1fr_1.2fr_0.7fr_1fr] gap-3 px-3 py-2
                            text-xs text-dark-500 font-medium uppercase tracking-wider">
              <span>{t('ssh_security.overview_col_server')}</span>
              <span>{t('ssh_security.overview_col_port')}</span>
              <span>{t('ssh_security.overview_col_auth')}</span>
              <span>{t('ssh_security.overview_col_fail2ban')}</span>
              <span>{t('ssh_security.overview_col_keys')}</span>
              <span>{t('ssh_security.overview_col_status')}</span>
            </div>
            <div className="space-y-1">
              {rows.map(row => {
                const offline = row.state === 'offline' || row.state === 'outdated'
                return (
                  <div
                    key={row.server_id}
                    onClick={() => onOpenServer(row.server_id)}
                    className={`grid grid-cols-[1.6fr_0.8fr_1fr_1.2fr_0.7fr_1fr] gap-3 px-3 py-2.5 items-center
                               rounded-lg border cursor-pointer transition-colors text-sm
                               ${offline
                                 ? 'bg-dark-900/40 border-dark-800 opacity-70 hover:opacity-100'
                                 : 'bg-dark-800/50 border-dark-700/50 hover:bg-dark-800'}`}
                  >
                    <span className="font-medium text-dark-100 truncate">{row.server_name}</span>

                    {row.state === 'loading' ? (
                      <span className="col-span-5 flex items-center gap-2 text-dark-500 text-xs">
                        <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        {t('ssh_security.overview_loading')}
                      </span>
                    ) : row.state === 'ok' && row.status ? (
                      <>
                        <span className="text-dark-200 font-mono">{row.status.sshd_port}</span>
                        <span className="text-dark-300">{authLabel(row.status.auth_method)}</span>
                        <span>
                          {row.status.fail2ban_running ? (
                            <span className="text-emerald-400">
                              {t('ssh_security.overview_f2b_on')}
                              {row.status.fail2ban_banned_count > 0 && ` (${row.status.fail2ban_banned_count})`}
                            </span>
                          ) : (
                            <span className="text-dark-500">{t('ssh_security.overview_f2b_off')}</span>
                          )}
                        </span>
                        <span className="text-dark-300 tabular-nums">{row.status.authorized_keys_count}</span>
                        <span>
                          {row.status.sshd_running ? (
                            <span className="px-2 py-0.5 rounded-full text-xs bg-emerald-500/15 text-emerald-400">
                              {t('ssh_security.overview_online')}
                            </span>
                          ) : (
                            <span className="px-2 py-0.5 rounded-full text-xs bg-red-500/15 text-red-400">
                              {t('ssh_security.overview_sshd_down')}
                            </span>
                          )}
                        </span>
                      </>
                    ) : (
                      <span className="col-span-5 flex items-center gap-2">
                        <span className={`px-2 py-0.5 rounded-full text-xs flex items-center gap-1 ${
                          row.state === 'outdated'
                            ? 'bg-orange-500/15 text-orange-400'
                            : 'bg-red-500/15 text-red-400'
                        }`}>
                          {row.state === 'outdated' ? <ShieldAlert className="w-3 h-3" /> : <AlertTriangle className="w-3 h-3" />}
                          {row.state === 'outdated'
                            ? t('ssh_security.overview_outdated')
                            : t('ssh_security.overview_offline')}
                        </span>
                        {row.error && <span className="text-dark-500 text-xs truncate">{row.error}</span>}
                      </span>
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        </div>
      )}
    </motion.div>
  )
}
