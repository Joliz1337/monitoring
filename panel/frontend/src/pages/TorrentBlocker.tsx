import { useEffect, useState, useCallback, useMemo, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { useParams, Link } from 'react-router-dom'
import {
  ShieldBan, Settings, Activity, BarChart3, FileText,
  Play, Trash2, Save, Loader2, CheckCircle2, XCircle,
  AlertTriangle, Users, Server as ServerIcon, ExternalLink,
  Search, X,
} from 'lucide-react'
import { useTorrentBlockerStore } from '../stores/torrentBlockerStore'
import { Tooltip } from '../components/ui/Tooltip'
import { FAQIcon } from '../components/FAQ'

function formatRelative(iso: string | null): string {
  if (!iso) return '—'
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.floor(hours / 24)}d ago`
}

function StatusBadge({ status }: { status: string | null }) {
  if (!status) return <span className="text-dark-500 text-xs">—</span>
  const colors: Record<string, string> = {
    success: 'bg-green-500/20 text-green-400',
    error: 'bg-red-500/20 text-red-400',
    no_reports: 'bg-dark-600/50 text-dark-400',
  }
  return (
    <span className={`px-2 py-0.5 rounded text-xs font-medium ${colors[status] || 'bg-dark-600/50 text-dark-400'}`}>
      {status}
    </span>
  )
}

export default function TorrentBlocker() {
  const { t } = useTranslation()
  const { uid } = useParams()
  const store = useTorrentBlockerStore()

  const [localSettings, setLocalSettings] = useState({
    enabled: false,
    poll_interval_minutes: 5,
    ban_duration_minutes: 30,
    excluded_server_ids: [] as number[],
  })
  const [saving, setSaving] = useState(false)
  const [reportsPage, setReportsPage] = useState(0)
  const [showReports, setShowReports] = useState(false)
  const [serverSearch, setServerSearch] = useState('')
  const [serverDropdownOpen, setServerDropdownOpen] = useState(false)
  const dropdownRef = useRef<HTMLDivElement>(null)

  // Загрузка данных
  const load = useCallback(() => {
    store.fetchSettings()
    store.fetchStatus()
    store.fetchStats()
    store.fetchServers()
  }, [])

  useEffect(() => { load() }, [load])

  // Авто-обновление статуса и статистики каждые 5 секунд
  useEffect(() => {
    const interval = setInterval(() => {
      store.fetchStatus()
      store.fetchStats()
    }, 5000)
    return () => clearInterval(interval)
  }, [])

  useEffect(() => {
    if (store.settings) {
      setLocalSettings({
        enabled: store.settings.enabled,
        poll_interval_minutes: store.settings.poll_interval_minutes,
        ban_duration_minutes: store.settings.ban_duration_minutes,
        excluded_server_ids: store.settings.excluded_server_ids || [],
      })
    }
  }, [store.settings])

  useEffect(() => {
    if (showReports) {
      store.fetchReports(reportsPage * 50, 50)
    }
  }, [showReports, reportsPage])

  // Закрытие dropdown по клику вне
  useEffect(() => {
    const handleClick = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setServerDropdownOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [])

  const handleSave = async () => {
    setSaving(true)
    await store.updateSettings(localSettings)
    setSaving(false)
  }

  const toggleExclusion = (serverId: number) => {
    setLocalSettings(prev => ({
      ...prev,
      excluded_server_ids: prev.excluded_server_ids.includes(serverId)
        ? prev.excluded_server_ids.filter(id => id !== serverId)
        : [...prev.excluded_server_ids, serverId],
    }))
  }

  const removeExclusion = (serverId: number) => {
    setLocalSettings(prev => ({
      ...prev,
      excluded_server_ids: prev.excluded_server_ids.filter(id => id !== serverId),
    }))
  }

  // Фильтрация серверов по поиску
  const filteredServers = useMemo(() => {
    const q = serverSearch.toLowerCase().trim()
    if (!q) return store.servers
    return store.servers.filter(s =>
      s.name.toLowerCase().includes(q) || (s.url && s.url.toLowerCase().includes(q))
    )
  }, [serverSearch, store.servers])

  // Имена исключённых серверов для отображения тегов
  const excludedServerNames = useMemo(() => {
    const map = new Map(store.servers.map(s => [s.id, s.name]))
    return localSettings.excluded_server_ids
      .map(id => ({ id, name: map.get(id) || `#${id}` }))
  }, [localSettings.excluded_server_ids, store.servers])

  const stats = store.stats
  const status = store.status

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="p-2 bg-red-500/10 rounded-lg">
            <ShieldBan className="w-6 h-6 text-red-400" />
          </div>
          <div>
            <h1 className="text-xl font-semibold text-dark-100 flex items-center gap-2">
              {t('torrent_blocker.title')}
              <FAQIcon screen="PAGE_TORRENT_BLOCKER" />
            </h1>
            <p className="text-sm text-dark-400">{t('torrent_blocker.subtitle')}</p>
          </div>
        </div>
        {status && (
          <div className="flex items-center gap-2">
            <div className={`w-2 h-2 rounded-full ${status.enabled ? 'bg-green-400 animate-pulse' : 'bg-dark-500'}`} />
            <span className="text-sm text-dark-400">
              {status.enabled ? t('torrent_blocker.running') : t('torrent_blocker.stopped')}
            </span>
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Settings Card */}
        <div className="bg-dark-900/50 border border-dark-800 rounded-xl p-5 space-y-4">
          <div className="flex items-center gap-2 text-dark-200 font-medium">
            <Settings className="w-4 h-4" />
            {t('torrent_blocker.settings')}
          </div>

          {/* Enable toggle */}
          <label className="flex items-center justify-between cursor-pointer">
            <span className="text-sm text-dark-300">{t('torrent_blocker.enable')}</span>
            <button
              onClick={() => setLocalSettings(prev => ({ ...prev, enabled: !prev.enabled }))}
              className={`relative w-10 h-5 rounded-full transition-colors ${
                localSettings.enabled ? 'bg-accent-500' : 'bg-dark-700'
              }`}
            >
              <div className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform ${
                localSettings.enabled ? 'translate-x-5' : ''
              }`} />
            </button>
          </label>

          {/* Poll interval */}
          <div className="space-y-1">
            <label className="text-sm text-dark-300">{t('torrent_blocker.poll_interval')}</label>
            <input
              type="number"
              min={1}
              max={60}
              value={localSettings.poll_interval_minutes}
              onChange={e => setLocalSettings(prev => ({ ...prev, poll_interval_minutes: Number(e.target.value) || 5 }))}
              className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-sm text-dark-200 focus:border-accent-500 focus:outline-none"
            />
          </div>

          {/* Ban duration */}
          <div className="space-y-1">
            <label className="text-sm text-dark-300">{t('torrent_blocker.ban_duration')}</label>
            <input
              type="number"
              min={1}
              max={43200}
              value={localSettings.ban_duration_minutes}
              onChange={e => setLocalSettings(prev => ({ ...prev, ban_duration_minutes: Number(e.target.value) || 30 }))}
              className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-sm text-dark-200 focus:border-accent-500 focus:outline-none"
            />
          </div>

          {/* Excluded servers — dropdown с поиском */}
          <div className="space-y-2">
            <label className="text-sm text-dark-300">{t('torrent_blocker.excluded_servers')}</label>
            <p className="text-xs text-dark-500">{t('torrent_blocker.excluded_servers_hint')}</p>

            {/* Теги выбранных серверов */}
            {excludedServerNames.length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                {excludedServerNames.map(s => (
                  <span
                    key={s.id}
                    className="inline-flex items-center gap-1 px-2 py-0.5 bg-dark-800 border border-dark-700 rounded-md text-xs text-dark-300"
                  >
                    <ServerIcon className="w-3 h-3 text-dark-500" />
                    {s.name}
                    <Tooltip label={t('common.remove_from_list')}>
                      <button
                        onClick={() => removeExclusion(s.id)}
                        className="text-dark-500 hover:text-dark-300 ml-0.5"
                      >
                        <X className="w-3 h-3" />
                      </button>
                    </Tooltip>
                  </span>
                ))}
              </div>
            )}

            {/* Dropdown */}
            <div className="relative" ref={dropdownRef}>
              <div
                onClick={() => setServerDropdownOpen(!serverDropdownOpen)}
                className="flex items-center gap-2 px-3 py-2 rounded-lg bg-dark-800 border border-dark-700 cursor-pointer hover:border-dark-600"
              >
                <Search className="w-4 h-4 text-dark-400 shrink-0" />
                <input
                  type="text"
                  value={serverSearch}
                  onChange={e => { setServerSearch(e.target.value); setServerDropdownOpen(true) }}
                  onFocus={() => setServerDropdownOpen(true)}
                  placeholder={t('torrent_blocker.search_servers')}
                  className="bg-transparent text-sm text-dark-100 placeholder-dark-500 outline-none w-full"
                  onClick={e => e.stopPropagation()}
                />
                {serverSearch && (
                  <Tooltip label={t('common.clear_search')}>
                    <button onClick={(e) => { e.stopPropagation(); setServerSearch('') }} className="text-dark-500 hover:text-dark-300">
                      <X className="w-3.5 h-3.5" />
                    </button>
                  </Tooltip>
                )}
              </div>

              {serverDropdownOpen && (
                <div className="absolute z-20 mt-1 w-full bg-dark-900 border border-dark-700 rounded-lg shadow-xl max-h-48 overflow-y-auto">
                  {filteredServers.length === 0 ? (
                    <p className="text-xs text-dark-500 py-3 text-center">{t('torrent_blocker.no_servers')}</p>
                  ) : (
                    filteredServers.map(server => {
                      const isExcluded = localSettings.excluded_server_ids.includes(server.id)
                      return (
                        <button
                          key={server.id}
                          onClick={() => toggleExclusion(server.id)}
                          className={`w-full flex items-center gap-2.5 px-3 py-2 text-left text-sm hover:bg-dark-800/80 transition-colors ${
                            isExcluded ? 'bg-accent-500/10 text-accent-300' : 'text-dark-300'
                          }`}
                        >
                          <div className={`w-4 h-4 rounded border flex items-center justify-center shrink-0 ${
                            isExcluded
                              ? 'bg-accent-500 border-accent-500'
                              : 'border-dark-600 bg-dark-800'
                          }`}>
                            {isExcluded && <CheckCircle2 className="w-3 h-3 text-white" />}
                          </div>
                          <ServerIcon className="w-3.5 h-3.5 text-dark-500 shrink-0" />
                          <span className="truncate">{server.name}</span>
                        </button>
                      )
                    })
                  )}
                </div>
              )}
            </div>
          </div>

          <button
            onClick={handleSave}
            disabled={saving}
            className="w-full flex items-center justify-center gap-2 bg-accent-500 hover:bg-accent-600 text-white rounded-lg px-4 py-2 text-sm font-medium transition-colors disabled:opacity-50"
          >
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
            {t('torrent_blocker.save')}
          </button>
        </div>

        {/* Worker Status Card */}
        <div className="bg-dark-900/50 border border-dark-800 rounded-xl p-5 space-y-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2 text-dark-200 font-medium">
              <Activity className="w-4 h-4" />
              {t('torrent_blocker.status')}
            </div>
            <button
              onClick={() => { store.pollNow(); setTimeout(() => store.fetchStatus(), 3000) }}
              className="flex items-center gap-1.5 bg-dark-800 hover:bg-dark-700 text-dark-300 rounded-lg px-3 py-1.5 text-xs transition-colors"
            >
              <Play className="w-3.5 h-3.5" />
              {t('torrent_blocker.poll_now')}
            </button>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="bg-dark-800/50 rounded-lg p-3">
              <p className="text-xs text-dark-500 mb-1">{t('torrent_blocker.last_poll')}</p>
              <p className="text-sm text-dark-200">{formatRelative(status?.last_poll_at ?? null)}</p>
            </div>
            <div className="bg-dark-800/50 rounded-lg p-3">
              <p className="text-xs text-dark-500 mb-1">{t('torrent_blocker.last_result')}</p>
              <StatusBadge status={status?.last_poll_status ?? null} />
            </div>
            <div className="bg-dark-800/50 rounded-lg p-3">
              <p className="text-xs text-dark-500 mb-1">{t('torrent_blocker.ips_banned')}</p>
              <p className="text-sm text-dark-200 font-mono">{status?.last_ips_banned ?? 0}</p>
            </div>
            <div className="bg-dark-800/50 rounded-lg p-3">
              <p className="text-xs text-dark-500 mb-1">{t('torrent_blocker.reports_processed')}</p>
              <p className="text-sm text-dark-200 font-mono">{status?.last_reports_processed ?? 0}</p>
            </div>
          </div>

          {status?.last_poll_message && (
            <div className="bg-dark-800/30 rounded-lg p-3">
              <p className="text-xs text-dark-400">{status.last_poll_message}</p>
            </div>
          )}

          <div className="border-t border-dark-800 pt-3">
            <div className="flex justify-between text-sm">
              <span className="text-dark-400">{t('torrent_blocker.total_ips')}</span>
              <span className="text-dark-200 font-mono">{status?.total_ips_banned ?? 0}</span>
            </div>
            <div className="flex justify-between text-sm mt-1">
              <span className="text-dark-400">{t('torrent_blocker.total_cycles')}</span>
              <span className="text-dark-200 font-mono">{status?.total_cycles ?? 0}</span>
            </div>
          </div>
        </div>
      </div>

      {/* Remnawave Stats */}
      <div className="bg-dark-900/50 border border-dark-800 rounded-xl p-5 space-y-4">
        <div className="flex items-center gap-2 text-dark-200 font-medium">
          <BarChart3 className="w-4 h-4" />
          {t('torrent_blocker.stats')}
        </div>

        {stats?.stats ? (
          <>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
              <div className="bg-dark-800/50 rounded-lg p-3 text-center">
                <p className="text-2xl font-bold text-dark-200">{stats.stats.totalReports}</p>
                <p className="text-xs text-dark-500">{t('torrent_blocker.total_reports')}</p>
              </div>
              <div className="bg-dark-800/50 rounded-lg p-3 text-center">
                <p className="text-2xl font-bold text-accent-400">{stats.stats.reportsLast24Hours}</p>
                <p className="text-xs text-dark-500">{t('torrent_blocker.reports_24h')}</p>
              </div>
              <div className="bg-dark-800/50 rounded-lg p-3 text-center">
                <p className="text-2xl font-bold text-dark-200">{stats.stats.distinctUsers}</p>
                <p className="text-xs text-dark-500">{t('torrent_blocker.distinct_users')}</p>
              </div>
              <div className="bg-dark-800/50 rounded-lg p-3 text-center">
                <p className="text-2xl font-bold text-dark-200">{stats.stats.distinctNodes}</p>
                <p className="text-xs text-dark-500">{t('torrent_blocker.distinct_nodes')}</p>
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {stats.topUsers && stats.topUsers.length > 0 && (
                <div>
                  <h3 className="text-sm text-dark-400 mb-2 flex items-center gap-1.5">
                    <Users className="w-3.5 h-3.5" /> {t('torrent_blocker.top_users')}
                  </h3>
                  <div className="space-y-1">
                    {stats.topUsers.slice(0, 10).map((u, i) => (
                      <div key={u.uuid} className="flex items-center justify-between px-3 py-1.5 bg-dark-800/40 rounded-lg">
                        <div className="flex items-center gap-2">
                          <span className="text-xs text-dark-500 w-4">{i + 1}</span>
                          <span className="text-sm text-dark-300 truncate">{u.username}</span>
                        </div>
                        <span className="text-sm font-mono text-red-400">{u.total}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {stats.topNodes && stats.topNodes.length > 0 && (
                <div>
                  <h3 className="text-sm text-dark-400 mb-2 flex items-center gap-1.5">
                    <ServerIcon className="w-3.5 h-3.5" /> {t('torrent_blocker.top_nodes')}
                  </h3>
                  <div className="space-y-1">
                    {stats.topNodes.slice(0, 10).map((n, i) => (
                      <div key={n.uuid} className="flex items-center justify-between px-3 py-1.5 bg-dark-800/40 rounded-lg">
                        <div className="flex items-center gap-2">
                          <span className="text-xs text-dark-500 w-4">{i + 1}</span>
                          <span className="text-sm text-dark-300 truncate">{n.name}</span>
                          <span className="text-xs text-dark-500">{n.countryCode}</span>
                        </div>
                        <span className="text-sm font-mono text-orange-400">{n.total}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </>
        ) : (
          <div className="flex items-center gap-2 text-amber-400/80 text-sm py-4 bg-amber-500/5 rounded-lg px-4">
            <AlertTriangle className="w-4 h-4 flex-shrink-0" />
            <span>{t('torrent_blocker.remnawave_not_configured')}</span>
            <Link
              to={`/${uid}/remnawave`}
              className="inline-flex items-center gap-1 text-accent-400 hover:text-accent-300 ml-1 whitespace-nowrap"
            >
              {t('torrent_blocker.go_to_remnawave')}
              <ExternalLink className="w-3.5 h-3.5" />
            </Link>
          </div>
        )}
      </div>

      {/* Current Reports */}
      <div className="bg-dark-900/50 border border-dark-800 rounded-xl p-5 space-y-4">
        <div className="flex items-center justify-between">
          <button
            onClick={() => setShowReports(!showReports)}
            className="flex items-center gap-2 text-dark-200 font-medium hover:text-dark-100 transition-colors"
          >
            <FileText className="w-4 h-4" />
            {t('torrent_blocker.reports')}
            <span className="text-xs text-dark-500">({store.reportsTotal})</span>
          </button>
          {showReports && store.reportsTotal > 0 && (
            <button
              onClick={() => { if (confirm(t('torrent_blocker.truncate_confirm'))) store.truncateReports() }}
              className="flex items-center gap-1.5 bg-red-500/10 hover:bg-red-500/20 text-red-400 rounded-lg px-3 py-1.5 text-xs transition-colors"
            >
              <Trash2 className="w-3.5 h-3.5" />
              {t('torrent_blocker.truncate')}
            </button>
          )}
        </div>

        {showReports && (
          <div className="space-y-2">
            {store.reports.length === 0 ? (
              <p className="text-dark-500 text-sm text-center py-4">{t('torrent_blocker.no_reports')}</p>
            ) : (
              <>
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-dark-500 text-xs border-b border-dark-800">
                        <th className="text-left pb-2 pr-3">{t('torrent_blocker.col_user')}</th>
                        <th className="text-left pb-2 pr-3">{t('torrent_blocker.col_node')}</th>
                        <th className="text-left pb-2 pr-3">IP</th>
                        <th className="text-left pb-2 pr-3">{t('torrent_blocker.col_blocked')}</th>
                        <th className="text-left pb-2">{t('torrent_blocker.col_time')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {store.reports.map(r => (
                        <tr key={r.id} className="border-b border-dark-800/50 hover:bg-dark-800/30">
                          <td className="py-2 pr-3 text-dark-300">{r.user.username}</td>
                          <td className="py-2 pr-3 text-dark-400">{r.node.name}</td>
                          <td className="py-2 pr-3 font-mono text-dark-300">{r.report.actionReport.ip}</td>
                          <td className="py-2 pr-3">
                            {r.report.actionReport.blocked
                              ? <CheckCircle2 className="w-4 h-4 text-green-400" />
                              : <XCircle className="w-4 h-4 text-dark-500" />}
                          </td>
                          <td className="py-2 text-dark-500 text-xs">{formatRelative(r.createdAt)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                {store.reportsTotal > 50 && (
                  <div className="flex items-center justify-center gap-2 pt-2">
                    <button
                      disabled={reportsPage === 0}
                      onClick={() => setReportsPage(p => p - 1)}
                      className="px-3 py-1 text-xs bg-dark-800 rounded hover:bg-dark-700 disabled:opacity-30 text-dark-300"
                    >
                      Prev
                    </button>
                    <span className="text-xs text-dark-500">
                      {reportsPage + 1} / {Math.ceil(store.reportsTotal / 50)}
                    </span>
                    <button
                      disabled={(reportsPage + 1) * 50 >= store.reportsTotal}
                      onClick={() => setReportsPage(p => p + 1)}
                      className="px-3 py-1 text-xs bg-dark-800 rounded hover:bg-dark-700 disabled:opacity-30 text-dark-300"
                    >
                      Next
                    </button>
                  </div>
                )}
              </>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
