import { useEffect, useState, useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Wifi, WifiOff, Trash2, RefreshCw, Settings2,
  ChevronDown, ChevronRight, Bot, Send, Loader2,
  Link2, KeyRound, Clock, Activity, X,
  Globe, Gauge,
} from 'lucide-react'
import { toast } from 'sonner'
import {
  xrayMonitorApi,
  XrayMonitorSettingsData,
  XrayMonitorSubscription,
  XrayMonitorServer,
  XrayMonitorCheckEntry,
} from '../api/client'

export default function XrayMonitor() {
  const { t } = useTranslation()

  const [settings, setSettings] = useState<XrayMonitorSettingsData | null>(null)
  const [subscriptions, setSubscriptions] = useState<XrayMonitorSubscription[]>([])
  const [manualServers, setManualServers] = useState<XrayMonitorServer[]>([])
  const [loading, setLoading] = useState(true)
  const [settingsOpen, setSettingsOpen] = useState(false)

  const [showAddSub, setShowAddSub] = useState(false)
  const [showAddKeys, setShowAddKeys] = useState(false)
  const [subName, setSubName] = useState('')
  const [subUrl, setSubUrl] = useState('')
  const [keysText, setKeysText] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const [expandedSubs, setExpandedSubs] = useState<Set<number>>(new Set())
  const [historyServerId, setHistoryServerId] = useState<number | null>(null)
  const [historyData, setHistoryData] = useState<XrayMonitorCheckEntry[]>([])
  const [historyLoading, setHistoryLoading] = useState(false)
  const [testing, setTesting] = useState(false)

  const fetchAll = useCallback(async () => {
    try {
      const [sRes, subRes, srvRes] = await Promise.all([
        xrayMonitorApi.getSettings(),
        xrayMonitorApi.getSubscriptions(),
        xrayMonitorApi.getServers(),
      ])
      setSettings(sRes.data)
      setSubscriptions(subRes.data)
      setManualServers(srvRes.data)
    } catch (e) {
      console.error('Failed to load xray monitor data:', e)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetchAll() }, [fetchAll])

  useEffect(() => {
    const interval = setInterval(async () => {
      try {
        const [subRes, srvRes] = await Promise.all([
          xrayMonitorApi.getSubscriptions(),
          xrayMonitorApi.getServers(),
        ])
        setSubscriptions(subRes.data)
        setManualServers(srvRes.data)
      } catch { /* ignore */ }
    }, 15000)
    return () => clearInterval(interval)
  }, [])

  const allServers = [
    ...manualServers,
    ...subscriptions.flatMap(s => s.servers),
  ]
  const onlineCount = allServers.filter(s => s.status === 'online').length
  const offlineCount = allServers.filter(s => s.status === 'offline').length

  const saveSettings = async (patch: Partial<XrayMonitorSettingsData>) => {
    if (!settings) return
    try {
      await xrayMonitorApi.updateSettings(patch)
      setSettings({ ...settings, ...patch })
      toast.success(t('xray_monitor.settings_saved'))
    } catch {
      toast.error(t('common.error'))
    }
  }

  const handleAddSubscription = async () => {
    if (!subName.trim() || !subUrl.trim()) return
    setSubmitting(true)
    try {
      const res = await xrayMonitorApi.addSubscription(subName.trim(), subUrl.trim())
      if (res.data.error) {
        toast.error(res.data.error)
      } else {
        toast.success(t('xray_monitor.subscription_added', { count: res.data.server_count }))
      }
      setShowAddSub(false)
      setSubName('')
      setSubUrl('')
      await fetchAll()
    } catch {
      toast.error(t('common.error'))
    } finally {
      setSubmitting(false)
    }
  }

  const handleRefreshSub = async (id: number) => {
    try {
      const res = await xrayMonitorApi.refreshSubscription(id)
      if (res.data.error) {
        toast.error(res.data.error)
      } else {
        toast.success(t('xray_monitor.subscription_refreshed', { count: res.data.server_count }))
      }
      await fetchAll()
    } catch {
      toast.error(t('common.error'))
    }
  }

  const handleDeleteSub = async (id: number) => {
    try {
      await xrayMonitorApi.deleteSubscription(id)
      toast.success(t('common.success'))
      await fetchAll()
    } catch {
      toast.error(t('common.error'))
    }
  }

  const handleAddKeys = async () => {
    if (!keysText.trim()) return
    setSubmitting(true)
    try {
      const res = await xrayMonitorApi.addKeys(keysText.trim())
      toast.success(t('xray_monitor.keys_added', { count: res.data.added }))
      setShowAddKeys(false)
      setKeysText('')
      await fetchAll()
    } catch {
      toast.error(t('xray_monitor.no_valid_keys'))
    } finally {
      setSubmitting(false)
    }
  }

  const handleDeleteServer = async (id: number) => {
    try {
      await xrayMonitorApi.deleteServer(id)
      setManualServers(prev => prev.filter(s => s.id !== id))
    } catch {
      toast.error(t('common.error'))
    }
  }

  const toggleSubExpand = (subId: number) => {
    setExpandedSubs(prev => {
      const next = new Set(prev)
      if (next.has(subId)) next.delete(subId)
      else next.add(subId)
      return next
    })
  }

  const toggleHistory = async (serverId: number) => {
    if (historyServerId === serverId) {
      setHistoryServerId(null)
      return
    }
    setHistoryServerId(serverId)
    setHistoryLoading(true)
    try {
      const res = await xrayMonitorApi.getServerHistory(serverId, 30)
      setHistoryData(res.data)
    } catch { /* ignore */ }
    finally { setHistoryLoading(false) }
  }

  const handleTestNotification = async () => {
    setTesting(true)
    try {
      const token = settings?.use_custom_bot ? settings.telegram_bot_token : undefined
      const chatId = settings?.use_custom_bot ? settings.telegram_chat_id : undefined
      const res = await xrayMonitorApi.testNotification(token, chatId)
      if (res.data.success) {
        toast.success(t('xray_monitor.test_sent'))
      } else {
        toast.error(res.data.error || t('common.error'))
      }
    } catch {
      toast.error(t('common.error'))
    } finally {
      setTesting(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Loader2 className="w-8 h-8 animate-spin text-accent-400" />
      </div>
    )
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      className="space-y-6"
    >
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-dark-100 flex items-center gap-3">
            <Gauge className="w-7 h-7 text-accent-400" />
            {t('xray_monitor.title')}
          </h1>
          <p className="text-dark-400 mt-1">
            {t('xray_monitor.subtitle', { total: allServers.length, online: onlineCount, offline: offlineCount })}
          </p>
        </div>
        <div className="flex gap-2">
          <button onClick={() => setShowAddKeys(true)} className="btn btn-secondary flex items-center gap-2">
            <KeyRound className="w-4 h-4" />
            {t('xray_monitor.add_keys')}
          </button>
          <button onClick={() => setShowAddSub(true)} className="btn btn-primary flex items-center gap-2">
            <Link2 className="w-4 h-4" />
            {t('xray_monitor.add_subscription')}
          </button>
        </div>
      </div>

      {/* Settings card */}
      <div className="card p-0 overflow-hidden">
        <button
          onClick={() => setSettingsOpen(!settingsOpen)}
          className="w-full flex items-center justify-between p-5 hover:bg-dark-800/30 transition-colors"
        >
          <div className="flex items-center gap-3">
            <Settings2 className="w-5 h-5 text-dark-400" />
            <span className="font-medium text-dark-200">{t('xray_monitor.settings')}</span>
          </div>
          {settingsOpen ? <ChevronDown className="w-5 h-5 text-dark-400" /> : <ChevronRight className="w-5 h-5 text-dark-400" />}
        </button>

        <AnimatePresence>
          {settingsOpen && settings && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.2 }}
              className="overflow-hidden"
            >
              <div className="px-5 pb-5 space-y-4 border-t border-dark-800/50 pt-4">
                <div className="flex items-center justify-between">
                  <span className="text-dark-300">{t('xray_monitor.enabled')}</span>
                  <button
                    onClick={() => saveSettings({ enabled: !settings.enabled })}
                    className={`relative w-12 h-6 rounded-full transition-colors ${settings.enabled ? 'bg-accent-500' : 'bg-dark-700'}`}
                  >
                    <div className={`absolute top-0.5 w-5 h-5 rounded-full bg-white transition-transform ${settings.enabled ? 'translate-x-6' : 'translate-x-0.5'}`} />
                  </button>
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="text-sm text-dark-400 mb-1 block">{t('xray_monitor.check_interval')}</label>
                    <input
                      type="number" className="input w-full" value={settings.check_interval}
                      min={60} max={600}
                      onChange={e => setSettings({ ...settings, check_interval: Number(e.target.value) })}
                      onBlur={() => saveSettings({ check_interval: settings.check_interval })}
                    />
                  </div>
                  <div>
                    <label className="text-sm text-dark-400 mb-1 block">{t('xray_monitor.latency_threshold')}</label>
                    <input
                      type="number" className="input w-full" value={settings.latency_threshold_ms}
                      min={100} max={10000}
                      onChange={e => setSettings({ ...settings, latency_threshold_ms: Number(e.target.value) })}
                      onBlur={() => saveSettings({ latency_threshold_ms: settings.latency_threshold_ms })}
                    />
                  </div>
                </div>

                <div>
                  <label className="text-sm text-dark-400 mb-1 block">{t('xray_monitor.fail_threshold')}</label>
                  <input
                    type="number" className="input w-32" value={settings.fail_threshold}
                    min={1} max={10}
                    onChange={e => setSettings({ ...settings, fail_threshold: Number(e.target.value) })}
                    onBlur={() => saveSettings({ fail_threshold: settings.fail_threshold })}
                  />
                </div>

                <div>
                  <label className="text-sm text-dark-400 mb-1 block">{t('xray_monitor.ignore_list')}</label>
                  <textarea
                    className="input w-full h-24 text-sm font-mono"
                    placeholder={t('xray_monitor.ignore_list_hint')}
                    value={(settings.ignore_list || []).join('\n')}
                    onChange={e => setSettings({ ...settings, ignore_list: e.target.value.split('\n') })}
                    onBlur={() => {
                      const cleaned = (settings.ignore_list || []).map(s => s.trim()).filter(Boolean)
                      saveSettings({ ignore_list: cleaned })
                    }}
                  />
                </div>

                <div className="space-y-2">
                  <span className="text-sm text-dark-400">{t('xray_monitor.notifications')}</span>
                  {(['notify_down', 'notify_recovery', 'notify_latency'] as const).map(key => (
                    <label key={key} className="flex items-center gap-3 cursor-pointer">
                      <input
                        type="checkbox" checked={settings[key]}
                        onChange={() => saveSettings({ [key]: !settings[key] })}
                        className="w-4 h-4 rounded border-dark-600 bg-dark-800 text-accent-500 focus:ring-accent-500"
                      />
                      <span className="text-dark-300 text-sm">{t(`xray_monitor.${key}`)}</span>
                    </label>
                  ))}
                </div>

                <div className="space-y-3 pt-2 border-t border-dark-800/50">
                  <label className="flex items-center gap-3 cursor-pointer">
                    <input
                      type="checkbox" checked={settings.use_custom_bot}
                      onChange={() => saveSettings({ use_custom_bot: !settings.use_custom_bot })}
                      className="w-4 h-4 rounded border-dark-600 bg-dark-800 text-accent-500 focus:ring-accent-500"
                    />
                    <span className="text-dark-300 text-sm flex items-center gap-2">
                      <Bot className="w-4 h-4" />
                      {t('xray_monitor.use_custom_bot')}
                    </span>
                  </label>

                  {settings.use_custom_bot && (
                    <div className="grid grid-cols-2 gap-4 pl-7">
                      <div>
                        <label className="text-sm text-dark-400 mb-1 block">Bot Token</label>
                        <input
                          type="password" className="input w-full" value={settings.telegram_bot_token}
                          onChange={e => setSettings({ ...settings, telegram_bot_token: e.target.value })}
                          onBlur={() => saveSettings({ telegram_bot_token: settings.telegram_bot_token })}
                          placeholder="123456:ABC..."
                        />
                      </div>
                      <div>
                        <label className="text-sm text-dark-400 mb-1 block">Chat ID</label>
                        <input
                          type="text" className="input w-full" value={settings.telegram_chat_id}
                          onChange={e => setSettings({ ...settings, telegram_chat_id: e.target.value })}
                          onBlur={() => saveSettings({ telegram_chat_id: settings.telegram_chat_id })}
                          placeholder="-100..."
                        />
                      </div>
                    </div>
                  )}

                  <button
                    onClick={handleTestNotification} disabled={testing}
                    className="btn btn-ghost text-sm flex items-center gap-2"
                  >
                    {testing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
                    {t('xray_monitor.test_notification')}
                  </button>
                </div>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      {/* Subscriptions with expandable servers */}
      {subscriptions.length > 0 && (
        <div className="card">
          <h2 className="text-lg font-semibold text-dark-100 mb-4 flex items-center gap-2">
            <Link2 className="w-5 h-5 text-accent-400" />
            {t('xray_monitor.subscriptions')}
          </h2>
          <div className="space-y-2">
            {subscriptions.map(sub => {
              const isExpanded = expandedSubs.has(sub.id)
              const subOnline = sub.servers.filter(s => s.status === 'online').length
              const subOffline = sub.servers.filter(s => s.status === 'offline').length
              return (
                <div key={sub.id} className="rounded-lg bg-dark-800/40 border border-dark-700/30 overflow-hidden">
                  {/* Sub header */}
                  <div
                    className="flex items-center justify-between p-3 cursor-pointer hover:bg-dark-800/50 transition-colors"
                    onClick={() => toggleSubExpand(sub.id)}
                  >
                    <div className="flex items-center gap-2 min-w-0 flex-1">
                      {isExpanded
                        ? <ChevronDown className="w-4 h-4 text-dark-400 shrink-0" />
                        : <ChevronRight className="w-4 h-4 text-dark-400 shrink-0" />}
                      <span className="font-medium text-dark-200 truncate">{sub.name}</span>
                      <span className="text-xs px-2 py-0.5 rounded-full bg-dark-700 text-dark-400 shrink-0">
                        {sub.server_count} {t('xray_monitor.servers_count')}
                      </span>
                      {subOnline > 0 && (
                        <span className="text-xs px-1.5 py-0.5 rounded-full bg-success/10 text-success shrink-0">
                          {subOnline} online
                        </span>
                      )}
                      {subOffline > 0 && (
                        <span className="text-xs px-1.5 py-0.5 rounded-full bg-danger/10 text-danger shrink-0">
                          {subOffline} offline
                        </span>
                      )}
                    </div>
                    <div className="flex items-center gap-1 ml-3 shrink-0" onClick={e => e.stopPropagation()}>
                      <button
                        onClick={() => handleRefreshSub(sub.id)}
                        className="p-1.5 rounded-lg hover:bg-dark-700 text-dark-400 hover:text-accent-400 transition-colors"
                        title={t('common.refresh')}
                      >
                        <RefreshCw className="w-4 h-4" />
                      </button>
                      <button
                        onClick={() => handleDeleteSub(sub.id)}
                        className="p-1.5 rounded-lg hover:bg-dark-700 text-dark-400 hover:text-danger transition-colors"
                        title={t('common.delete')}
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  </div>

                  {sub.last_error && (
                    <div className="px-3 pb-2 -mt-1">
                      <span className="text-xs text-danger truncate block">{sub.last_error}</span>
                    </div>
                  )}

                  {/* Expanded: server list */}
                  <AnimatePresence>
                    {isExpanded && (
                      <motion.div
                        initial={{ height: 0, opacity: 0 }}
                        animate={{ height: 'auto', opacity: 1 }}
                        exit={{ height: 0, opacity: 0 }}
                        transition={{ duration: 0.15 }}
                        className="overflow-hidden"
                      >
                        <div className="border-t border-dark-700/30 px-3 pb-3 pt-2">
                          {sub.servers.length === 0 ? (
                            <p className="text-dark-500 text-sm py-2">{t('xray_monitor.no_sub_servers')}</p>
                          ) : (
                            <div className="space-y-1">
                              {sub.servers.map(srv => (
                                <ServerRow
                                  key={srv.id}
                                  server={srv}
                                  historyServerId={historyServerId}
                                  historyData={historyData}
                                  historyLoading={historyLoading}
                                  onToggleHistory={toggleHistory}
                                  onDelete={handleDeleteServer}
                                  t={t}
                                />
                              ))}
                            </div>
                          )}
                        </div>
                      </motion.div>
                    )}
                  </AnimatePresence>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Manual servers */}
      <div className="card">
        <h2 className="text-lg font-semibold text-dark-100 mb-4 flex items-center gap-2">
          <Activity className="w-5 h-5 text-accent-400" />
          {t('xray_monitor.servers')}
          <span className="text-sm font-normal text-dark-400">({manualServers.length})</span>
        </h2>

        {manualServers.length === 0 ? (
          <div className="text-center py-12 text-dark-400">
            <Globe className="w-12 h-12 mx-auto mb-3 opacity-30" />
            <p>{t('xray_monitor.no_servers')}</p>
          </div>
        ) : (
          <div className="space-y-1">
            <div className="grid grid-cols-12 gap-2 px-3 py-2 text-xs text-dark-500 uppercase tracking-wider">
              <div className="col-span-4">{t('xray_monitor.col_name')}</div>
              <div className="col-span-3">{t('xray_monitor.col_address')}</div>
              <div className="col-span-1">{t('xray_monitor.col_protocol')}</div>
              <div className="col-span-1">{t('xray_monitor.col_status')}</div>
              <div className="col-span-1">{t('xray_monitor.col_ping')}</div>
              <div className="col-span-2 text-right">{t('xray_monitor.col_actions')}</div>
            </div>
            {manualServers.map(srv => (
              <ServerRow
                key={srv.id}
                server={srv}
                historyServerId={historyServerId}
                historyData={historyData}
                historyLoading={historyLoading}
                onToggleHistory={toggleHistory}
                onDelete={handleDeleteServer}
                t={t}
              />
            ))}
          </div>
        )}
      </div>

      {/* Add Subscription Modal */}
      <AnimatePresence>
        {showAddSub && (
          <Modal onClose={() => setShowAddSub(false)}>
            <h3 className="text-lg font-semibold text-dark-100 mb-4">{t('xray_monitor.add_subscription')}</h3>
            <div className="space-y-3">
              <div>
                <label className="text-sm text-dark-400 mb-1 block">{t('common.name')}</label>
                <input className="input w-full" value={subName} onChange={e => setSubName(e.target.value)}
                  placeholder={t('xray_monitor.sub_name_placeholder')} />
              </div>
              <div>
                <label className="text-sm text-dark-400 mb-1 block">URL</label>
                <input className="input w-full" value={subUrl} onChange={e => setSubUrl(e.target.value)}
                  placeholder="https://..." />
              </div>
            </div>
            <div className="flex justify-end gap-2 mt-5">
              <button className="btn btn-ghost" onClick={() => setShowAddSub(false)}>{t('common.cancel')}</button>
              <button
                className="btn btn-primary flex items-center gap-2"
                onClick={handleAddSubscription}
                disabled={submitting || !subName.trim() || !subUrl.trim()}
              >
                {submitting && <Loader2 className="w-4 h-4 animate-spin" />}
                {t('common.save')}
              </button>
            </div>
          </Modal>
        )}
      </AnimatePresence>

      {/* Add Keys Modal */}
      <AnimatePresence>
        {showAddKeys && (
          <Modal onClose={() => setShowAddKeys(false)}>
            <h3 className="text-lg font-semibold text-dark-100 mb-4">{t('xray_monitor.add_keys')}</h3>
            <div>
              <label className="text-sm text-dark-400 mb-1 block">{t('xray_monitor.keys_hint')}</label>
              <textarea
                className="input w-full h-48 font-mono text-xs resize-none"
                value={keysText} onChange={e => setKeysText(e.target.value)}
                placeholder="vless://...&#10;vmess://...&#10;trojan://...&#10;ss://..."
              />
            </div>
            <div className="flex justify-end gap-2 mt-5">
              <button className="btn btn-ghost" onClick={() => setShowAddKeys(false)}>{t('common.cancel')}</button>
              <button
                className="btn btn-primary flex items-center gap-2"
                onClick={handleAddKeys}
                disabled={submitting || !keysText.trim()}
              >
                {submitting && <Loader2 className="w-4 h-4 animate-spin" />}
                {t('xray_monitor.import')}
              </button>
            </div>
          </Modal>
        )}
      </AnimatePresence>
    </motion.div>
  )
}

function ServerRow({
  server: srv,
  historyServerId,
  historyData,
  historyLoading,
  onToggleHistory,
  onDelete,
  t,
}: {
  server: XrayMonitorServer
  historyServerId: number | null
  historyData: XrayMonitorCheckEntry[]
  historyLoading: boolean
  onToggleHistory: (id: number) => void
  onDelete: (id: number) => void
  t: (key: string) => string
}) {
  const isOpen = historyServerId === srv.id
  return (
    <div>
      <div
        className={`grid grid-cols-12 gap-2 px-3 py-2 rounded-lg items-center transition-colors cursor-pointer
          ${srv.status === 'offline' ? 'bg-danger/5 border border-danger/20' : 'bg-dark-800/30 border border-dark-700/20 hover:bg-dark-800/50'}`}
        onClick={() => onToggleHistory(srv.id)}
      >
        <div className="col-span-4 flex items-center gap-2 min-w-0">
          {isOpen ? <ChevronDown className="w-3.5 h-3.5 text-dark-400 shrink-0" /> : <ChevronRight className="w-3.5 h-3.5 text-dark-400 shrink-0" />}
          <span className="text-dark-200 truncate text-sm">{srv.name}</span>
        </div>
        <div className="col-span-3 text-dark-400 text-sm truncate">{srv.address}:{srv.port}</div>
        <div className="col-span-1">
          <span className="text-xs px-1.5 py-0.5 rounded bg-dark-700/60 text-dark-300 uppercase">
            {srv.protocol === 'shadowsocks' ? 'ss' : srv.protocol}
          </span>
        </div>
        <div className="col-span-1"><StatusBadge status={srv.status} /></div>
        <div className="col-span-1 text-sm">
          {srv.last_ping_ms != null ? (
            <span className={srv.last_ping_ms > 500 ? 'text-warning' : 'text-success'}>
              {Math.round(srv.last_ping_ms)} ms
            </span>
          ) : (
            <span className="text-dark-500">—</span>
          )}
        </div>
        <div className="col-span-2 flex justify-end">
          <button
            onClick={e => { e.stopPropagation(); onDelete(srv.id) }}
            className="p-1.5 rounded-lg hover:bg-dark-700 text-dark-500 hover:text-danger transition-colors"
          >
            <Trash2 className="w-4 h-4" />
          </button>
        </div>
      </div>

      <AnimatePresence>
        {isOpen && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="overflow-hidden"
          >
            <div className="ml-8 mr-3 my-1 p-3 rounded-lg bg-dark-900/50 border border-dark-800/50">
              {historyLoading ? (
                <div className="flex items-center gap-2 text-dark-400 text-sm">
                  <Loader2 className="w-4 h-4 animate-spin" />
                  {t('common.loading')}
                </div>
              ) : historyData.length === 0 ? (
                <p className="text-dark-500 text-sm">{t('xray_monitor.no_history')}</p>
              ) : (
                <div className="flex flex-wrap gap-1">
                  {historyData.slice().reverse().map(check => (
                    <div
                      key={check.id}
                      className={`w-3 h-6 rounded-sm ${check.status === 'ok' ? 'bg-success/70' : 'bg-danger/70'}`}
                      title={`${check.timestamp ? new Date(check.timestamp).toLocaleTimeString() : '?'} — ${check.status === 'ok' ? `${check.ping_ms} ms` : check.error || 'fail'}`}
                    />
                  ))}
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

function StatusBadge({ status }: { status: string }) {
  const { t } = useTranslation()
  if (status === 'online') {
    return (
      <span className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-success/10 text-success">
        <Wifi className="w-3 h-3" />
        {t('common.online')}
      </span>
    )
  }
  if (status === 'offline') {
    return (
      <span className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-danger/10 text-danger">
        <WifiOff className="w-3 h-3" />
        {t('common.offline')}
      </span>
    )
  }
  return (
    <span className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-dark-700 text-dark-400">
      <Clock className="w-3 h-3" />
      —
    </span>
  )
}

function Modal({ children, onClose }: { children: React.ReactNode; onClose: () => void }) {
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      className="fixed inset-0 z-50 flex items-center justify-center"
    >
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />
      <motion.div
        initial={{ scale: 0.95, opacity: 0 }}
        animate={{ scale: 1, opacity: 1 }}
        exit={{ scale: 0.95, opacity: 0 }}
        className="relative z-10 bg-dark-900 border border-dark-700/50 rounded-2xl p-6 w-full max-w-lg mx-4 shadow-2xl"
      >
        <button onClick={onClose} className="absolute top-4 right-4 p-1.5 rounded-lg hover:bg-dark-800 text-dark-400">
          <X className="w-5 h-5" />
        </button>
        {children}
      </motion.div>
    </motion.div>
  )
}
