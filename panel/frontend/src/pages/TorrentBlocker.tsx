import { useEffect, useState, useCallback, useMemo, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import ReactApexChart from 'react-apexcharts'
import type { ApexOptions } from 'apexcharts'
import {
  ShieldBan, Settings, Activity, BarChart3, FileText,
  Play, Trash2, Save, Loader2, CheckCircle2, XCircle,
  Server as ServerIcon, Search, X, Webhook, Send,
} from 'lucide-react'
import { useTorrentBlockerStore } from '../stores/torrentBlockerStore'
import type { TorrentBlockerStatsRange } from '../api/client'
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

const RANGES: TorrentBlockerStatsRange[] = ['24h', '7d', '30d']

export default function TorrentBlocker() {
  const { t, i18n } = useTranslation()
  const store = useTorrentBlockerStore()

  const [localSettings, setLocalSettings] = useState({
    enabled: false,
    poll_interval_minutes: 5,
    ban_duration_minutes: 30,
    excluded_server_ids: [] as number[],
    webhook_enabled: false,
    webhook_url: '',
    webhook_secret: '',
    webhook_delay_seconds: 60,
  })
  const [saving, setSaving] = useState(false)
  const [testingWebhook, setTestingWebhook] = useState(false)
  const [reportsPage, setReportsPage] = useState(0)
  const [showReports, setShowReports] = useState(false)
  const [serverSearch, setServerSearch] = useState('')
  const [serverDropdownOpen, setServerDropdownOpen] = useState(false)
  const dropdownRef = useRef<HTMLDivElement>(null)

  const load = useCallback(() => {
    store.fetchSettings()
    store.fetchStatus()
    store.fetchInternalStats()
    store.fetchServers()
  }, [])

  useEffect(() => { load() }, [load])

  useEffect(() => {
    const interval = setInterval(() => {
      store.fetchStatus()
      store.fetchInternalStats()
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
        webhook_enabled: store.settings.webhook_enabled ?? false,
        webhook_url: store.settings.webhook_url ?? '',
        webhook_secret: store.settings.webhook_secret ?? '',
        webhook_delay_seconds: store.settings.webhook_delay_seconds ?? 60,
      })
    }
  }, [store.settings])

  useEffect(() => {
    if (showReports) {
      store.fetchReports(reportsPage * 50, 50)
    }
  }, [showReports, reportsPage])

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

  const handleTestWebhook = async () => {
    setTestingWebhook(true)
    await store.testWebhook(localSettings.webhook_url, localSettings.webhook_secret)
    setTestingWebhook(false)
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

  const filteredServers = useMemo(() => {
    const q = serverSearch.toLowerCase().trim()
    if (!q) return store.servers
    return store.servers.filter(s =>
      s.name.toLowerCase().includes(q) || (s.url && s.url.toLowerCase().includes(q))
    )
  }, [serverSearch, store.servers])

  const excludedServerNames = useMemo(() => {
    const map = new Map(store.servers.map(s => [s.id, s.name]))
    return localSettings.excluded_server_ids
      .map(id => ({ id, name: map.get(id) || `#${id}` }))
  }, [localSettings.excluded_server_ids, store.servers])

  const status = store.status
  const stats = store.internalStats
  const statsRange = store.statsRange

  const chart = useMemo(() => {
    const buckets = stats?.buckets || []
    const series = [{
      name: t('torrent_blocker.stats_chart_title'),
      data: buckets.map(b => ({ x: new Date(b.time).getTime(), y: b.count })),
    }]
    const isHourly = statsRange === '24h'
    const lang = i18n.language || 'en'
    const options: ApexOptions = {
      chart: {
        type: 'bar',
        toolbar: { show: false },
        zoom: { enabled: false },
        animations: { enabled: false },
        background: 'transparent',
      },
      theme: { mode: 'dark' },
      colors: ['#22d3ee'],
      plotOptions: {
        bar: { borderRadius: 3, columnWidth: '70%' },
      },
      dataLabels: { enabled: false },
      grid: {
        borderColor: '#343541',
        strokeDashArray: 4,
        xaxis: { lines: { show: false } },
        yaxis: { lines: { show: true } },
      },
      xaxis: {
        type: 'datetime',
        labels: {
          style: { colors: '#8e8ea0', fontSize: '11px' },
          datetimeUTC: false,
          formatter: (_value, timestamp) => {
            const ts = typeof timestamp === 'number' ? timestamp : Number(_value)
            const d = new Date(ts)
            if (isHourly) {
              return d.toLocaleTimeString(lang, { hour: '2-digit', minute: '2-digit' })
            }
            return d.toLocaleDateString(lang, { day: '2-digit', month: 'short' })
          },
        },
        axisBorder: { show: false },
        axisTicks: { show: false },
      },
      yaxis: {
        min: 0,
        forceNiceScale: true,
        labels: {
          style: { colors: '#8e8ea0', fontSize: '11px' },
          formatter: val => `${Math.round(val)}`,
        },
      },
      tooltip: {
        theme: 'dark',
        x: {
          formatter: value => {
            const d = new Date(value)
            if (isHourly) {
              return d.toLocaleString(lang, { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })
            }
            return d.toLocaleDateString(lang, { day: '2-digit', month: 'short', year: 'numeric' })
          },
        },
        y: {
          formatter: val => `${val}`,
        },
      },
    }
    return { series, options }
  }, [stats, statsRange, i18n.language, t])

  return (
    <div className="space-y-6">
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
        {/* Settings */}
        <div className="bg-dark-900/50 border border-dark-800 rounded-xl p-5 space-y-4">
          <div className="flex items-center gap-2 text-dark-200 font-medium">
            <Settings className="w-4 h-4" />
            {t('torrent_blocker.settings')}
          </div>

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

          <div className="space-y-2">
            <label className="text-sm text-dark-300">{t('torrent_blocker.excluded_servers')}</label>
            <p className="text-xs text-dark-500">{t('torrent_blocker.excluded_servers_hint')}</p>

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

          <div className="border-t border-dark-800 pt-4 space-y-3">
            <label className="flex items-center justify-between cursor-pointer">
              <span className="flex items-center gap-2 text-sm text-dark-300">
                <Webhook className="w-4 h-4 text-dark-400" />
                {t('torrent_blocker.webhook_enable')}
              </span>
              <button
                onClick={() => setLocalSettings(prev => ({ ...prev, webhook_enabled: !prev.webhook_enabled }))}
                className={`relative w-10 h-5 rounded-full transition-colors ${
                  localSettings.webhook_enabled ? 'bg-accent-500' : 'bg-dark-700'
                }`}
              >
                <div className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform ${
                  localSettings.webhook_enabled ? 'translate-x-5' : ''
                }`} />
              </button>
            </label>
            <p className="text-xs text-dark-500">{t('torrent_blocker.webhook_hint')}</p>

            {localSettings.webhook_enabled && (
              <div className="space-y-3">
                <div className="space-y-1">
                  <label className="text-sm text-dark-300">{t('torrent_blocker.webhook_url')}</label>
                  <input
                    type="url"
                    value={localSettings.webhook_url}
                    onChange={e => setLocalSettings(prev => ({ ...prev, webhook_url: e.target.value }))}
                    placeholder="https://bot.example.com/torrent-webhook"
                    className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-sm text-dark-200 focus:border-accent-500 focus:outline-none"
                  />
                </div>

                <div className="space-y-1">
                  <label className="text-sm text-dark-300">{t('torrent_blocker.webhook_secret')}</label>
                  <input
                    type="password"
                    value={localSettings.webhook_secret}
                    onChange={e => setLocalSettings(prev => ({ ...prev, webhook_secret: e.target.value }))}
                    placeholder={t('torrent_blocker.webhook_secret_placeholder')}
                    className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-sm text-dark-200 focus:border-accent-500 focus:outline-none"
                  />
                  <p className="text-xs text-dark-500">{t('torrent_blocker.webhook_secret_hint')}</p>
                </div>

                <div className="space-y-1">
                  <label className="text-sm text-dark-300">{t('torrent_blocker.webhook_delay')}</label>
                  <input
                    type="number"
                    min={0}
                    max={1800}
                    value={localSettings.webhook_delay_seconds}
                    onChange={e => setLocalSettings(prev => ({ ...prev, webhook_delay_seconds: Number(e.target.value) || 0 }))}
                    className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-sm text-dark-200 focus:border-accent-500 focus:outline-none"
                  />
                  <p className="text-xs text-dark-500">{t('torrent_blocker.webhook_delay_hint')}</p>
                </div>

                <button
                  onClick={handleTestWebhook}
                  disabled={testingWebhook || !localSettings.webhook_url}
                  className="w-full flex items-center justify-center gap-2 bg-dark-800 hover:bg-dark-700 text-dark-200 rounded-lg px-4 py-2 text-sm font-medium transition-colors disabled:opacity-50"
                >
                  {testingWebhook ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
                  {t('torrent_blocker.webhook_test')}
                </button>
              </div>
            )}
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

        {/* Worker Status */}
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

      {/* Own ban statistics */}
      <div className="bg-dark-900/50 border border-dark-800 rounded-xl p-5 space-y-4">
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div className="flex items-center gap-2 text-dark-200 font-medium">
            <BarChart3 className="w-4 h-4" />
            {t('torrent_blocker.stats')}
          </div>
          <div className="flex items-center gap-1 bg-dark-800/60 border border-dark-700 rounded-lg p-0.5">
            {RANGES.map(r => (
              <button
                key={r}
                onClick={() => store.setStatsRange(r)}
                className={`px-3 py-1 rounded-md text-xs font-medium transition-colors ${
                  statsRange === r
                    ? 'bg-accent-500 text-white'
                    : 'text-dark-400 hover:text-dark-200'
                }`}
              >
                {t(`torrent_blocker.range_${r}`)}
              </button>
            ))}
          </div>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div className="bg-dark-800/50 rounded-lg p-4">
            <p className="text-xs text-dark-500 mb-1">{t('torrent_blocker.stats_currently_banned')}</p>
            <p className="text-2xl font-bold text-red-400 font-mono">{stats?.currently_banned ?? 0}</p>
          </div>
          <div className="bg-dark-800/50 rounded-lg p-4">
            <p className="text-xs text-dark-500 mb-1">
              {t('torrent_blocker.stats_total_in_range')} · {t(`torrent_blocker.range_${statsRange}`)}
            </p>
            <p className="text-2xl font-bold text-accent-400 font-mono">{stats?.total_in_range ?? 0}</p>
          </div>
        </div>

        <div>
          {(stats?.buckets?.length ?? 0) === 0 || (stats?.total_in_range ?? 0) === 0 ? (
            <div className="flex items-center justify-center h-48 text-dark-500 text-sm">
              {t('torrent_blocker.stats_no_data')}
            </div>
          ) : (
            <ReactApexChart
              key={statsRange}
              options={chart.options}
              series={chart.series}
              type="bar"
              height={260}
            />
          )}
        </div>
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
