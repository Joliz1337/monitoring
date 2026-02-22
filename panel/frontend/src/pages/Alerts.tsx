import { useEffect, useState, useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Bell, Bot, Send, CheckCircle2, XCircle, Loader2,
  ChevronDown, ChevronRight, Trash2, Server,
  Cpu, MemoryStick, Network, Cable, Power,
  RefreshCw, Clock,
} from 'lucide-react'
import { toast } from 'sonner'
import { alertsApi, AlertSettingsData, AlertHistoryItem, AlertStatus } from '../api/client'

type TriggerSection = 'offline' | 'cpu' | 'ram' | 'network' | 'tcp'

export default function Alerts() {
  const { t } = useTranslation()

  const [settings, setSettings] = useState<AlertSettingsData | null>(null)
  const [status, setStatus] = useState<AlertStatus | null>(null)
  const [history, setHistory] = useState<AlertHistoryItem[]>([])
  const [historyTotal, setHistoryTotal] = useState(0)
  const [historyPage, setHistoryPage] = useState(0)

  const [loading, setLoading] = useState(true)
  const [, setSaving] = useState(false)
  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null)
  const [testing, setTesting] = useState(false)
  const [clearing, setClearing] = useState(false)

  const [expanded, setExpanded] = useState<Set<TriggerSection>>(new Set())
  const [historyFilter, setHistoryFilter] = useState<string>('')

  const PAGE_SIZE = 20

  const fetchAll = useCallback(async () => {
    try {
      const [sRes, stRes, hRes] = await Promise.all([
        alertsApi.getSettings(),
        alertsApi.getStatus(),
        alertsApi.getHistory({ limit: PAGE_SIZE, offset: 0 }),
      ])
      setSettings(sRes.data)
      setStatus(stRes.data)
      setHistory(hRes.data.items)
      setHistoryTotal(hRes.data.total)
    } catch (e) {
      console.error('Failed to load alert data:', e)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchAll()
  }, [fetchAll])

  const fetchHistory = useCallback(async (offset: number, filter?: string) => {
    try {
      const params: Record<string, unknown> = { limit: PAGE_SIZE, offset }
      if (filter) params.alert_type = filter
      const res = await alertsApi.getHistory(params as never)
      setHistory(res.data.items)
      setHistoryTotal(res.data.total)
    } catch (e) {
      console.error('Failed to load history:', e)
    }
  }, [])

  const save = useCallback(async (patch: Partial<AlertSettingsData>) => {
    if (!settings) return
    setSaving(true)
    try {
      const res = await alertsApi.updateSettings(patch)
      setSettings(res.data)
      toast.success(t('alerts.settings_saved'))
    } catch (e) {
      console.error('Failed to save settings:', e)
      toast.error(t('alerts.settings_save_failed'))
    } finally {
      setSaving(false)
    }
  }, [settings, t])

  const handleTest = async () => {
    if (!settings) return
    setTesting(true)
    setTestResult(null)
    try {
      const res = await alertsApi.testTelegram(settings.telegram_bot_token, settings.telegram_chat_id)
      setTestResult({ success: res.data.success, message: res.data.error || res.data.message || '' })
    } catch (e) {
      setTestResult({ success: false, message: 'Connection error' })
    } finally {
      setTesting(false)
    }
  }

  const handleClearHistory = async () => {
    if (!confirm(t('alerts.confirm_clear'))) return
    setClearing(true)
    try {
      await alertsApi.clearHistory()
      setHistory([])
      setHistoryTotal(0)
      setHistoryPage(0)
    } catch (e) {
      console.error('Failed to clear history:', e)
    } finally {
      setClearing(false)
    }
  }

  const toggle = (section: TriggerSection) => {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(section)) next.delete(section)
      else next.add(section)
      return next
    })
  }

  if (loading || !settings) {
    return (
      <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="space-y-6">
        <div className="space-y-2">
          <div className="h-7 w-40 bg-dark-700/50 rounded-lg animate-pulse" />
          <div className="h-4 w-64 bg-dark-700/30 rounded-lg animate-pulse" />
        </div>
        {Array.from({ length: 3 }).map((_, i) => (
          <div key={i} className="card p-5 space-y-4">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 bg-dark-700/50 rounded-xl animate-pulse" />
              <div className="space-y-2 flex-1">
                <div className="h-4 w-32 bg-dark-700/50 rounded animate-pulse" />
                <div className="h-3 w-48 bg-dark-700/30 rounded animate-pulse" />
              </div>
            </div>
          </div>
        ))}
      </motion.div>
    )
  }

  const alertTypeLabel = (t_: string) => {
    const map: Record<string, string> = {
      offline: t('alerts.type_offline'),
      recovery: t('alerts.type_recovery'),
      cpu_critical: t('alerts.type_cpu_critical'),
      cpu_spike: t('alerts.type_cpu_spike'),
      ram_critical: t('alerts.type_ram_critical'),
      ram_spike: t('alerts.type_ram_spike'),
      network_spike: t('alerts.type_network_spike'),
      network_drop: t('alerts.type_network_drop'),
      tcp_established_spike: t('alerts.type_tcp_est_spike'),
      tcp_established_drop: t('alerts.type_tcp_est_drop'),
      tcp_listen_spike: t('alerts.type_tcp_listen_spike'),
      tcp_timewait_spike: t('alerts.type_tcp_tw_spike'),
      tcp_closewait_spike: t('alerts.type_tcp_cw_spike'),
      tcp_synsent_spike: t('alerts.type_tcp_synsent_spike'),
      tcp_synrecv_spike: t('alerts.type_tcp_synrecv_spike'),
      tcp_finwait_spike: t('alerts.type_tcp_finwait_spike'),
    }
    return map[t_] || t_
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-amber-500/20 to-orange-500/20 flex items-center justify-center">
            <Bell className="w-5 h-5 text-amber-400" />
          </div>
          <div>
            <h1 className="text-xl font-semibold text-white">{t('alerts.title')}</h1>
            <p className="text-sm text-dark-400">{t('alerts.subtitle')}</p>
          </div>
        </div>
        {status && (
          <div className="flex items-center gap-2 text-xs text-dark-400">
            <div className={`w-2 h-2 rounded-full ${status.running && settings.enabled ? 'bg-green-400' : 'bg-dark-600'}`} />
            {status.running && settings.enabled
              ? `${t('alerts.monitoring')} ${status.monitored_servers} ${t('alerts.servers_count')}`
              : t('alerts.disabled')}
          </div>
        )}
      </div>

      {/* Telegram + Enable */}
      <Section title={t('alerts.telegram_settings')} icon={<Bot className="w-4 h-4" />}>
        <div className="space-y-4">
          <ToggleRow
            label={t('alerts.enable_alerts')}
            checked={settings.enabled}
            onChange={v => save({ enabled: v })}
          />
          <InputRow
            label={t('alerts.bot_token')}
            value={settings.telegram_bot_token}
            placeholder="123456:ABC-DEF..."
            type="password"
            onSave={v => save({ telegram_bot_token: v })}
          />
          <InputRow
            label={t('alerts.chat_id')}
            value={settings.telegram_chat_id}
            placeholder="-1001234567890"
            onSave={v => save({ telegram_chat_id: v })}
          />
          <div className="space-y-1">
            <label className="text-sm text-dark-300">{t('alerts.notification_language')}</label>
            <select
              value={settings.language || 'en'}
              onChange={e => save({ language: e.target.value })}
              className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-sm text-dark-200
                         focus:border-accent-500/50 focus:outline-none transition"
            >
              <option value="en">English</option>
              <option value="ru">Русский</option>
            </select>
          </div>
          <div className="flex items-center gap-3">
            <button
              onClick={handleTest}
              disabled={testing || !settings.telegram_bot_token || !settings.telegram_chat_id}
              className="px-4 py-2 bg-accent-500/20 text-accent-400 rounded-lg text-sm font-medium
                         hover:bg-accent-500/30 transition disabled:opacity-40 disabled:cursor-not-allowed
                         flex items-center gap-2"
            >
              {testing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
              {t('alerts.test_send')}
            </button>
            <AnimatePresence>
              {testResult && (
                <motion.span
                  initial={{ opacity: 0, x: -10 }}
                  animate={{ opacity: 1, x: 0 }}
                  exit={{ opacity: 0 }}
                  className={`text-sm flex items-center gap-1 ${testResult.success ? 'text-green-400' : 'text-red-400'}`}
                >
                  {testResult.success ? <CheckCircle2 className="w-4 h-4" /> : <XCircle className="w-4 h-4" />}
                  {testResult.success ? t('alerts.test_ok') : testResult.message}
                </motion.span>
              )}
            </AnimatePresence>
          </div>
        </div>
      </Section>

      {/* General settings */}
      <Section title={t('alerts.general_settings')} icon={<Clock className="w-4 h-4" />}>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <SliderRow
            label={t('alerts.check_interval')}
            value={settings.check_interval}
            min={10} max={300} step={10}
            format={v => `${v}s`}
            onSave={v => save({ check_interval: v })}
          />
          <SliderRow
            label={t('alerts.cooldown')}
            value={settings.alert_cooldown}
            min={300} max={7200} step={300}
            format={v => `${Math.floor(v / 60)} ${t('alerts.min')}`}
            onSave={v => save({ alert_cooldown: v })}
          />
        </div>
      </Section>

      {/* Triggers */}
      <div className="space-y-3">
        <h2 className="text-sm font-medium text-dark-300 uppercase tracking-wider">{t('alerts.triggers')}</h2>

        {/* Offline */}
        <TriggerBlock
          title={t('alerts.trigger_offline')}
          icon={<Power className="w-4 h-4" />}
          enabled={settings.offline_enabled}
          onToggle={v => save({ offline_enabled: v })}
          expanded={expanded.has('offline')}
          onExpand={() => toggle('offline')}
        >
          <SliderRow label={t('alerts.fail_threshold')} value={settings.offline_fail_threshold} min={1} max={10} step={1} format={v => `${v}`} onSave={v => save({ offline_fail_threshold: v })} />
          <ToggleRow label={t('alerts.recovery_notify')} checked={settings.offline_recovery_notify} onChange={v => save({ offline_recovery_notify: v })} />
        </TriggerBlock>

        {/* CPU */}
        <TriggerBlock
          title={t('alerts.trigger_cpu')}
          icon={<Cpu className="w-4 h-4" />}
          enabled={settings.cpu_enabled}
          onToggle={v => save({ cpu_enabled: v })}
          expanded={expanded.has('cpu')}
          onExpand={() => toggle('cpu')}
        >
          <SliderRow label={t('alerts.critical_threshold')} value={settings.cpu_critical_threshold} min={50} max={100} step={1} format={v => `${v}%`} onSave={v => save({ cpu_critical_threshold: v })} />
          <SliderRow label={t('alerts.spike_percent')} value={settings.cpu_spike_percent} min={10} max={200} step={5} format={v => `${v}%`} onSave={v => save({ cpu_spike_percent: v })} />
          <SliderRow label={t('alerts.sustained')} value={settings.cpu_sustained_seconds} min={60} max={900} step={30} format={v => `${v}s`} onSave={v => save({ cpu_sustained_seconds: v })} />
        </TriggerBlock>

        {/* RAM */}
        <TriggerBlock
          title={t('alerts.trigger_ram')}
          icon={<MemoryStick className="w-4 h-4" />}
          enabled={settings.ram_enabled}
          onToggle={v => save({ ram_enabled: v })}
          expanded={expanded.has('ram')}
          onExpand={() => toggle('ram')}
        >
          <SliderRow label={t('alerts.critical_threshold')} value={settings.ram_critical_threshold} min={50} max={100} step={1} format={v => `${v}%`} onSave={v => save({ ram_critical_threshold: v })} />
          <SliderRow label={t('alerts.spike_percent')} value={settings.ram_spike_percent} min={10} max={200} step={5} format={v => `${v}%`} onSave={v => save({ ram_spike_percent: v })} />
          <SliderRow label={t('alerts.sustained')} value={settings.ram_sustained_seconds} min={60} max={900} step={30} format={v => `${v}s`} onSave={v => save({ ram_sustained_seconds: v })} />
        </TriggerBlock>

        {/* Network */}
        <TriggerBlock
          title={t('alerts.trigger_network')}
          icon={<Network className="w-4 h-4" />}
          enabled={settings.network_enabled}
          onToggle={v => save({ network_enabled: v })}
          expanded={expanded.has('network')}
          onExpand={() => toggle('network')}
        >
          <SliderRow label={t('alerts.spike_percent')} value={settings.network_spike_percent} min={50} max={1000} step={25} format={v => `${v}%`} onSave={v => save({ network_spike_percent: v })} />
          <SliderRow label={t('alerts.drop_percent')} value={settings.network_drop_percent} min={30} max={100} step={5} format={v => `${v}%`} onSave={v => save({ network_drop_percent: v })} />
          <SliderRow label={t('alerts.sustained')} value={settings.network_sustained_seconds} min={60} max={900} step={30} format={v => `${v}s`} onSave={v => save({ network_sustained_seconds: v })} />
        </TriggerBlock>

        {/* TCP */}
        <TriggerBlock
          title={t('alerts.trigger_tcp')}
          icon={<Cable className="w-4 h-4" />}
          enabled={settings.tcp_established_enabled || settings.tcp_listen_enabled || settings.tcp_timewait_enabled || settings.tcp_closewait_enabled || settings.tcp_synsent_enabled || settings.tcp_synrecv_enabled || settings.tcp_finwait_enabled}
          onToggle={() => toggle('tcp')}
          expanded={expanded.has('tcp')}
          onExpand={() => toggle('tcp')}
          hideMainToggle
        >
          {/* Established */}
          <div className="border-t border-dark-700/50 pt-3">
            <ToggleRow label="TCP Established" checked={settings.tcp_established_enabled} onChange={v => save({ tcp_established_enabled: v })} />
            {settings.tcp_established_enabled && (
              <div className="ml-4 mt-2 space-y-2">
                <SliderRow label={t('alerts.spike_percent')} value={settings.tcp_established_spike_percent} min={50} max={1000} step={25} format={v => `${v}%`} onSave={v => save({ tcp_established_spike_percent: v })} />
                <SliderRow label={t('alerts.drop_percent')} value={settings.tcp_established_drop_percent} min={30} max={100} step={5} format={v => `${v}%`} onSave={v => save({ tcp_established_drop_percent: v })} />
                <SliderRow label={t('alerts.sustained')} value={settings.tcp_established_sustained_seconds} min={60} max={900} step={30} format={v => `${v}s`} onSave={v => save({ tcp_established_sustained_seconds: v })} />
              </div>
            )}
          </div>
          {/* Listen */}
          <div className="border-t border-dark-700/50 pt-3">
            <ToggleRow label="TCP Listen" checked={settings.tcp_listen_enabled} onChange={v => save({ tcp_listen_enabled: v })} />
            {settings.tcp_listen_enabled && (
              <div className="ml-4 mt-2 space-y-2">
                <SliderRow label={t('alerts.spike_percent')} value={settings.tcp_listen_spike_percent} min={50} max={1000} step={25} format={v => `${v}%`} onSave={v => save({ tcp_listen_spike_percent: v })} />
                <SliderRow label={t('alerts.sustained')} value={settings.tcp_listen_sustained_seconds} min={60} max={900} step={30} format={v => `${v}s`} onSave={v => save({ tcp_listen_sustained_seconds: v })} />
              </div>
            )}
          </div>
          {/* Time Wait */}
          <div className="border-t border-dark-700/50 pt-3">
            <ToggleRow label="TCP Time Wait" checked={settings.tcp_timewait_enabled} onChange={v => save({ tcp_timewait_enabled: v })} />
            {settings.tcp_timewait_enabled && (
              <div className="ml-4 mt-2 space-y-2">
                <SliderRow label={t('alerts.spike_percent')} value={settings.tcp_timewait_spike_percent} min={50} max={1000} step={25} format={v => `${v}%`} onSave={v => save({ tcp_timewait_spike_percent: v })} />
                <SliderRow label={t('alerts.sustained')} value={settings.tcp_timewait_sustained_seconds} min={60} max={900} step={30} format={v => `${v}s`} onSave={v => save({ tcp_timewait_sustained_seconds: v })} />
              </div>
            )}
          </div>
          {/* Close Wait */}
          <div className="border-t border-dark-700/50 pt-3">
            <ToggleRow label="TCP Close Wait" checked={settings.tcp_closewait_enabled} onChange={v => save({ tcp_closewait_enabled: v })} />
            {settings.tcp_closewait_enabled && (
              <div className="ml-4 mt-2 space-y-2">
                <SliderRow label={t('alerts.spike_percent')} value={settings.tcp_closewait_spike_percent} min={50} max={1000} step={25} format={v => `${v}%`} onSave={v => save({ tcp_closewait_spike_percent: v })} />
                <SliderRow label={t('alerts.sustained')} value={settings.tcp_closewait_sustained_seconds} min={60} max={900} step={30} format={v => `${v}s`} onSave={v => save({ tcp_closewait_sustained_seconds: v })} />
              </div>
            )}
          </div>
          {/* SYN Sent */}
          <div className="border-t border-dark-700/50 pt-3">
            <ToggleRow label="TCP SYN Sent" checked={settings.tcp_synsent_enabled} onChange={v => save({ tcp_synsent_enabled: v })} />
            {settings.tcp_synsent_enabled && (
              <div className="ml-4 mt-2 space-y-2">
                <SliderRow label={t('alerts.spike_percent')} value={settings.tcp_synsent_spike_percent} min={50} max={1000} step={25} format={v => `${v}%`} onSave={v => save({ tcp_synsent_spike_percent: v })} />
                <SliderRow label={t('alerts.sustained')} value={settings.tcp_synsent_sustained_seconds} min={60} max={900} step={30} format={v => `${v}s`} onSave={v => save({ tcp_synsent_sustained_seconds: v })} />
              </div>
            )}
          </div>
          {/* SYN Recv */}
          <div className="border-t border-dark-700/50 pt-3">
            <ToggleRow label="TCP SYN Recv" checked={settings.tcp_synrecv_enabled} onChange={v => save({ tcp_synrecv_enabled: v })} />
            {settings.tcp_synrecv_enabled && (
              <div className="ml-4 mt-2 space-y-2">
                <SliderRow label={t('alerts.spike_percent')} value={settings.tcp_synrecv_spike_percent} min={50} max={1000} step={25} format={v => `${v}%`} onSave={v => save({ tcp_synrecv_spike_percent: v })} />
                <SliderRow label={t('alerts.sustained')} value={settings.tcp_synrecv_sustained_seconds} min={60} max={900} step={30} format={v => `${v}s`} onSave={v => save({ tcp_synrecv_sustained_seconds: v })} />
              </div>
            )}
          </div>
          {/* FIN Wait */}
          <div className="border-t border-dark-700/50 pt-3">
            <ToggleRow label="TCP FIN Wait" checked={settings.tcp_finwait_enabled} onChange={v => save({ tcp_finwait_enabled: v })} />
            {settings.tcp_finwait_enabled && (
              <div className="ml-4 mt-2 space-y-2">
                <SliderRow label={t('alerts.spike_percent')} value={settings.tcp_finwait_spike_percent} min={50} max={1000} step={25} format={v => `${v}%`} onSave={v => save({ tcp_finwait_spike_percent: v })} />
                <SliderRow label={t('alerts.sustained')} value={settings.tcp_finwait_sustained_seconds} min={60} max={900} step={30} format={v => `${v}s`} onSave={v => save({ tcp_finwait_sustained_seconds: v })} />
              </div>
            )}
          </div>
        </TriggerBlock>
      </div>

      {/* History */}
      <Section
        title={t('alerts.history')}
        icon={<Clock className="w-4 h-4" />}
        right={
          <div className="flex items-center gap-2">
            <select
              value={historyFilter}
              onChange={e => {
                setHistoryFilter(e.target.value)
                setHistoryPage(0)
                fetchHistory(0, e.target.value)
              }}
              className="bg-dark-800 border border-dark-700 rounded-lg px-2 py-1 text-xs text-dark-300"
            >
              <option value="">{t('alerts.all_types')}</option>
              <option value="offline">{t('alerts.type_offline')}</option>
              <option value="recovery">{t('alerts.type_recovery')}</option>
              <option value="cpu_critical">{t('alerts.type_cpu_critical')}</option>
              <option value="cpu_spike">{t('alerts.type_cpu_spike')}</option>
              <option value="ram_critical">{t('alerts.type_ram_critical')}</option>
              <option value="ram_spike">{t('alerts.type_ram_spike')}</option>
              <option value="network_spike">{t('alerts.type_network_spike')}</option>
              <option value="network_drop">{t('alerts.type_network_drop')}</option>
              <option value="tcp_established_spike">{t('alerts.type_tcp_est_spike')}</option>
              <option value="tcp_established_drop">{t('alerts.type_tcp_est_drop')}</option>
              <option value="tcp_listen_spike">{t('alerts.type_tcp_listen_spike')}</option>
              <option value="tcp_timewait_spike">{t('alerts.type_tcp_tw_spike')}</option>
              <option value="tcp_closewait_spike">{t('alerts.type_tcp_cw_spike')}</option>
              <option value="tcp_synsent_spike">{t('alerts.type_tcp_synsent_spike')}</option>
              <option value="tcp_synrecv_spike">{t('alerts.type_tcp_synrecv_spike')}</option>
              <option value="tcp_finwait_spike">{t('alerts.type_tcp_finwait_spike')}</option>
            </select>
            <button
              onClick={handleClearHistory}
              disabled={clearing || history.length === 0}
              className="px-3 py-1 bg-red-500/10 text-red-400 rounded-lg text-xs hover:bg-red-500/20
                         transition disabled:opacity-40 flex items-center gap-1"
            >
              {clearing ? <Loader2 className="w-3 h-3 animate-spin" /> : <Trash2 className="w-3 h-3" />}
              {t('alerts.clear')}
            </button>
            <button
              onClick={() => fetchHistory(historyPage * PAGE_SIZE, historyFilter)}
              className="p-1 text-dark-400 hover:text-dark-200 transition"
            >
              <RefreshCw className="w-4 h-4" />
            </button>
          </div>
        }
      >
        {history.length === 0 ? (
          <p className="text-dark-500 text-sm text-center py-8">{t('alerts.no_history')}</p>
        ) : (
          <div className="space-y-2">
            {history.map(item => (
              <div
                key={item.id}
                className="flex items-start gap-3 p-3 bg-dark-800/50 rounded-lg border border-dark-700/30"
              >
                <div className={`mt-0.5 w-2 h-2 rounded-full flex-shrink-0 ${
                  item.severity === 'critical' ? 'bg-red-400' : item.severity === 'warning' ? 'bg-yellow-400' : 'bg-green-400'
                }`} />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-xs font-medium text-dark-300">{alertTypeLabel(item.alert_type)}</span>
                    <span className="text-xs text-dark-500">|</span>
                    <span className="text-xs text-dark-400 flex items-center gap-1">
                      <Server className="w-3 h-3" />
                      {item.server_name}
                    </span>
                    {item.notified && (
                      <span className="text-xs text-accent-500">TG</span>
                    )}
                  </div>
                  <p className="text-sm text-dark-200 mt-1 break-words">{item.message}</p>
                  <span className="text-xs text-dark-500 mt-1 block">
                    {item.created_at ? new Date(item.created_at).toLocaleString() : ''}
                  </span>
                </div>
              </div>
            ))}

            {/* Pagination */}
            {historyTotal > PAGE_SIZE && (
              <div className="flex items-center justify-center gap-3 pt-2">
                <button
                  onClick={() => {
                    const p = Math.max(0, historyPage - 1)
                    setHistoryPage(p)
                    fetchHistory(p * PAGE_SIZE, historyFilter)
                  }}
                  disabled={historyPage === 0}
                  className="px-3 py-1 text-xs bg-dark-800 text-dark-300 rounded-lg disabled:opacity-30"
                >
                  {t('alerts.prev')}
                </button>
                <span className="text-xs text-dark-400">
                  {historyPage + 1} / {Math.ceil(historyTotal / PAGE_SIZE)}
                </span>
                <button
                  onClick={() => {
                    const p = historyPage + 1
                    setHistoryPage(p)
                    fetchHistory(p * PAGE_SIZE, historyFilter)
                  }}
                  disabled={(historyPage + 1) * PAGE_SIZE >= historyTotal}
                  className="px-3 py-1 text-xs bg-dark-800 text-dark-300 rounded-lg disabled:opacity-30"
                >
                  {t('alerts.next')}
                </button>
              </div>
            )}
          </div>
        )}
      </Section>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Reusable UI components                                             */
/* ------------------------------------------------------------------ */

function Section({ title, icon, children, right }: {
  title: string
  icon?: React.ReactNode
  children: React.ReactNode
  right?: React.ReactNode
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      className="bg-dark-900/50 rounded-xl border border-dark-800/50 p-5"
    >
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2 text-dark-200 text-sm font-medium">
          {icon}
          {title}
        </div>
        {right}
      </div>
      {children}
    </motion.div>
  )
}

function TriggerBlock({ title, icon, enabled, onToggle, expanded, onExpand, children, hideMainToggle }: {
  title: string
  icon: React.ReactNode
  enabled: boolean
  onToggle: (v: boolean) => void
  expanded: boolean
  onExpand: () => void
  children: React.ReactNode
  hideMainToggle?: boolean
}) {
  return (
    <div className="bg-dark-900/50 rounded-xl border border-dark-800/50 overflow-hidden">
      <button
        onClick={onExpand}
        className="w-full flex items-center justify-between p-4 hover:bg-dark-800/30 transition"
      >
        <div className="flex items-center gap-3">
          <div className={`w-8 h-8 rounded-lg flex items-center justify-center ${enabled ? 'bg-accent-500/20 text-accent-400' : 'bg-dark-800 text-dark-500'}`}>
            {icon}
          </div>
          <span className="text-sm font-medium text-dark-200">{title}</span>
          <div className={`w-2 h-2 rounded-full ${enabled ? 'bg-green-400' : 'bg-dark-600'}`} />
        </div>
        {expanded ? <ChevronDown className="w-4 h-4 text-dark-500" /> : <ChevronRight className="w-4 h-4 text-dark-500" />}
      </button>
      <AnimatePresence>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="px-4 pb-4 space-y-3">
              {!hideMainToggle && (
                <ToggleRow label="" checked={enabled} onChange={onToggle} inline />
              )}
              {children}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

function ToggleRow({ label, checked, onChange, inline }: {
  label: string
  checked: boolean
  onChange: (v: boolean) => void
  inline?: boolean
}) {
  return (
    <div className={`flex items-center ${inline ? 'justify-end' : 'justify-between'} gap-3`}>
      {label && <span className="text-sm text-dark-300">{label}</span>}
      <button
        onClick={() => onChange(!checked)}
        className={`relative w-10 h-5 rounded-full transition-colors ${checked ? 'bg-accent-500' : 'bg-dark-700'}`}
      >
        <motion.div
          className="absolute top-0.5 w-4 h-4 bg-white rounded-full shadow"
          animate={{ left: checked ? 22 : 2 }}
          transition={{ type: 'spring', stiffness: 500, damping: 30 }}
        />
      </button>
    </div>
  )
}

function SliderRow({ label, value, min, max, step, format, onSave }: {
  label: string
  value: number
  min: number
  max: number
  step: number
  format: (v: number) => string
  onSave: (v: number) => void
}) {
  const [local, setLocal] = useState(value)
  useEffect(() => setLocal(value), [value])

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between">
        <span className="text-sm text-dark-300">{label}</span>
        <span className="text-sm text-accent-400 font-mono">{format(local)}</span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={local}
        onChange={e => setLocal(Number(e.target.value))}
        onMouseUp={() => { if (local !== value) onSave(local) }}
        onTouchEnd={() => { if (local !== value) onSave(local) }}
        className="w-full h-1.5 bg-dark-700 rounded-full appearance-none cursor-pointer
                   [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:w-4 [&::-webkit-slider-thumb]:h-4
                   [&::-webkit-slider-thumb]:bg-accent-400 [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:cursor-pointer
                   [&::-moz-range-thumb]:w-4 [&::-moz-range-thumb]:h-4 [&::-moz-range-thumb]:bg-accent-400
                   [&::-moz-range-thumb]:rounded-full [&::-moz-range-thumb]:border-0 [&::-moz-range-thumb]:cursor-pointer"
      />
    </div>
  )
}

function InputRow({ label, value, placeholder, type, onSave }: {
  label: string
  value: string
  placeholder?: string
  type?: string
  onSave: (v: string) => void
}) {
  const [local, setLocal] = useState(value)
  useEffect(() => setLocal(value), [value])

  return (
    <div className="space-y-1">
      <label className="text-sm text-dark-300">{label}</label>
      <input
        type={type || 'text'}
        value={local}
        placeholder={placeholder}
        onChange={e => setLocal(e.target.value)}
        onBlur={() => { if (local !== value) onSave(local) }}
        className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-sm text-dark-200
                   placeholder-dark-600 focus:border-accent-500/50 focus:outline-none transition"
      />
    </div>
  )
}
