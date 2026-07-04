import { useEffect, useState, useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import { motion, AnimatePresence } from 'framer-motion'
import { toast } from 'sonner'
import {
  Siren, ShieldAlert, ShieldCheck, Settings as SettingsIcon, ListChecks,
  BookOpen, Loader2, RefreshCw, Trash2, Save, Power, Radar,
} from 'lucide-react'
import {
  antiDdosApi, type AntiDdosSettings, type AntiDdosStatus, type NodeAntiDdosState,
} from '../api/client'

type TabType = 'control' | 'whitelist' | 'info'

function Toggle({ on, onClick, disabled }: { on: boolean; onClick: () => void; disabled?: boolean }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`relative w-10 h-5 rounded-full transition-colors shrink-0 ${on ? 'bg-accent-500' : 'bg-dark-700'} ${disabled ? 'opacity-50 cursor-not-allowed' : ''}`}
    >
      <div className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform ${on ? 'translate-x-5' : ''}`} />
    </button>
  )
}

function ModeBadge({ node }: { node: NodeAntiDdosState }) {
  const { t } = useTranslation()
  if (!node.emergency_mode) {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-green-500/15 text-green-400">
        <ShieldCheck className="w-3 h-3" /> {t('anti_ddos.mode_normal')}
      </span>
    )
  }
  const src = node.source === 'auto' ? t('anti_ddos.source_auto') : t('anti_ddos.source_manual')
  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-red-500/15 text-red-400">
      <ShieldAlert className="w-3 h-3" /> {t('anti_ddos.mode_emergency')} · {src}
    </span>
  )
}

export default function AntiDdos() {
  const { t } = useTranslation()
  const [activeTab, setActiveTab] = useState<TabType>('control')
  const [status, setStatus] = useState<AntiDdosStatus | null>(null)
  const [settings, setSettings] = useState<AntiDdosSettings | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<string | null>(null)
  const [cidrText, setCidrText] = useState('')

  const loadStatus = useCallback(async () => {
    try {
      const { data } = await antiDdosApi.getStatus()
      setStatus(data)
    } catch { /* keep last */ }
  }, [])

  const loadSettings = useCallback(async () => {
    try {
      const { data } = await antiDdosApi.getSettings()
      setSettings(data)
      setCidrText((data.user_cidrs || []).join('\n'))
    } catch { /* keep last */ }
  }, [])

  useEffect(() => {
    Promise.all([loadStatus(), loadSettings()]).finally(() => setLoading(false))
    const id = setInterval(() => { if (!document.hidden) loadStatus() }, 10000)
    return () => clearInterval(id)
  }, [loadStatus, loadSettings])

  const patchSettings = async (patch: Partial<AntiDdosSettings>) => {
    try {
      const { data } = await antiDdosApi.updateSettings(patch)
      setSettings(data)
      return true
    } catch {
      toast.error(t('anti_ddos.save_failed'))
      return false
    }
  }

  const runAction = async (key: string, fn: () => Promise<unknown>, okMsg: string) => {
    setBusy(key)
    try {
      await fn()
      toast.success(okMsg)
      await loadStatus()
    } catch {
      toast.error(t('anti_ddos.action_failed'))
    } finally {
      setBusy(null)
    }
  }

  const toggleNodeEmergency = (node: NodeAntiDdosState) =>
    runAction(`emg-${node.server_id}`,
      () => antiDdosApi.setNodeEmergency(node.server_id, !node.emergency_mode),
      t('anti_ddos.done'))

  const toggleNodeWatchdog = (node: NodeAntiDdosState) =>
    runAction(`wd-${node.server_id}`,
      () => antiDdosApi.setNodeWatchdog(node.server_id, !node.watchdog),
      t('anti_ddos.done'))

  const tabs: { id: TabType; icon: typeof Siren; label: string }[] = [
    { id: 'control', icon: SettingsIcon, label: t('anti_ddos.tab_control') },
    { id: 'whitelist', icon: ListChecks, label: t('anti_ddos.tab_whitelist') },
    { id: 'info', icon: BookOpen, label: t('anti_ddos.tab_info') },
  ]

  if (loading) {
    return <div className="flex items-center justify-center h-64"><Loader2 className="w-6 h-6 animate-spin text-dark-500" /></div>
  }

  const activeCount = status?.active_count ?? 0

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <Siren className="w-6 h-6 text-accent-500" />
        <div>
          <h1 className="text-xl font-semibold">{t('anti_ddos.title')}</h1>
          <p className="text-sm text-dark-400">{t('anti_ddos.subtitle')}</p>
        </div>
      </div>

      <div className="flex gap-2 border-b border-dark-800">
        {tabs.map(tab => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`flex items-center gap-2 px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
              activeTab === tab.id ? 'border-accent-500 text-accent-400' : 'border-transparent text-dark-400 hover:text-dark-200'
            }`}
          >
            <tab.icon className="w-4 h-4" /> {tab.label}
          </button>
        ))}
      </div>

      <AnimatePresence mode="wait">
        <motion.div key={activeTab} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} transition={{ duration: 0.15 }}>

          {activeTab === 'control' && (
            <div className="space-y-4">
              <div className="card p-5 space-y-4">
                <div className="flex items-center justify-between">
                  <div>
                    <h2 className="text-sm font-semibold flex items-center gap-2"><Power className="w-4 h-4" /> {t('anti_ddos.global_title')}</h2>
                    <p className="text-xs text-dark-400 mt-1">{t('anti_ddos.global_hint')}</p>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-dark-400">{t('anti_ddos.feature_enabled')}</span>
                    <Toggle on={!!settings?.enabled} onClick={() => settings && patchSettings({ enabled: !settings.enabled })} />
                  </div>
                </div>

                <div className={`text-sm px-3 py-2 rounded ${activeCount > 0 ? 'bg-red-500/10 text-red-400' : 'bg-dark-800/60 text-dark-300'}`}>
                  {activeCount > 0
                    ? t('anti_ddos.nodes_under_attack', { count: activeCount, total: status?.total ?? 0 })
                    : t('anti_ddos.all_normal', { total: status?.total ?? 0 })}
                </div>

                <div className="flex flex-wrap gap-2">
                  <button
                    onClick={() => runAction('emg-all-on', () => antiDdosApi.emergencyAll(true), t('anti_ddos.done'))}
                    disabled={busy === 'emg-all-on'}
                    className="flex items-center gap-2 px-3 py-2 text-sm rounded bg-red-500/90 hover:bg-red-500 text-white disabled:opacity-50">
                    {busy === 'emg-all-on' ? <Loader2 className="w-4 h-4 animate-spin" /> : <ShieldAlert className="w-4 h-4" />}
                    {t('anti_ddos.enable_all')}
                  </button>
                  <button
                    onClick={() => runAction('emg-all-off', () => antiDdosApi.emergencyAll(false), t('anti_ddos.done'))}
                    disabled={busy === 'emg-all-off'}
                    className="flex items-center gap-2 px-3 py-2 text-sm rounded bg-dark-700 hover:bg-dark-600 text-dark-100 disabled:opacity-50">
                    {busy === 'emg-all-off' ? <Loader2 className="w-4 h-4 animate-spin" /> : <ShieldCheck className="w-4 h-4" />}
                    {t('anti_ddos.disable_all')}
                  </button>
                  <button
                    onClick={() => runAction('push-wl', () => antiDdosApi.pushWhitelist(), t('anti_ddos.whitelist_pushed'))}
                    disabled={busy === 'push-wl'}
                    className="flex items-center gap-2 px-3 py-2 text-sm rounded bg-dark-700 hover:bg-dark-600 text-dark-100 disabled:opacity-50">
                    {busy === 'push-wl' ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
                    {t('anti_ddos.push_whitelist')}
                  </button>
                </div>
              </div>

              <div className="card divide-y divide-dark-800">
                {(status?.nodes ?? []).length === 0 && (
                  <div className="p-6 text-center text-sm text-dark-500">{t('anti_ddos.no_nodes')}</div>
                )}
                {(status?.nodes ?? []).map(node => (
                  <div key={node.server_id} className="p-4 flex items-center justify-between gap-4">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-medium truncate">{node.server_name}</span>
                        <ModeBadge node={node} />
                      </div>
                      {node.emergency_mode && node.reason && (
                        <p className="text-xs text-dark-400 mt-1 truncate">{t('anti_ddos.reason')}: {node.reason}</p>
                      )}
                    </div>
                    <div className="flex items-center gap-5 shrink-0">
                      <div className="flex items-center gap-2">
                        <Radar className="w-3.5 h-3.5 text-dark-400" />
                        <span className="text-xs text-dark-400">{t('anti_ddos.watchdog')}</span>
                        <Toggle on={node.watchdog} onClick={() => toggleNodeWatchdog(node)} disabled={busy === `wd-${node.server_id}`} />
                      </div>
                      <div className="flex items-center gap-2">
                        <span className="text-xs text-dark-400">{t('anti_ddos.emergency')}</span>
                        <Toggle on={node.emergency_mode} onClick={() => toggleNodeEmergency(node)} disabled={busy === `emg-${node.server_id}`} />
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {activeTab === 'whitelist' && (
            <div className="space-y-4">
              <div className="card p-5 space-y-2">
                <h2 className="text-sm font-semibold flex items-center gap-2"><ShieldCheck className="w-4 h-4" /> {t('anti_ddos.wl_auto_title')}</h2>
                <p className="text-sm text-dark-400">{t('anti_ddos.wl_auto_desc')}</p>
                {settings?.last_push_at && (
                  <p className="text-xs text-dark-500">
                    {t('anti_ddos.wl_last_push', { count: settings.last_push_count })} · {new Date(settings.last_push_at).toLocaleString()}
                  </p>
                )}
              </div>

              <div className="card p-5 space-y-3">
                <h2 className="text-sm font-semibold flex items-center gap-2"><ListChecks className="w-4 h-4" /> {t('anti_ddos.wl_manual_title')}</h2>
                <p className="text-sm text-dark-400">{t('anti_ddos.wl_manual_desc')}</p>
                <textarea
                  value={cidrText}
                  onChange={e => setCidrText(e.target.value)}
                  rows={8}
                  placeholder={"173.245.48.0/20\n103.21.244.0/22\n1.2.3.4"}
                  className="w-full bg-dark-900 border border-dark-700 rounded px-3 py-2 text-sm font-mono focus:border-accent-500 outline-none"
                />
                <div className="flex justify-end">
                  <button
                    onClick={async () => {
                      const list = cidrText.split('\n').map(s => s.trim()).filter(Boolean)
                      setBusy('save-cidr')
                      const ok = await patchSettings({ user_cidrs: list })
                      if (ok) toast.success(t('anti_ddos.saved'))
                      setBusy(null)
                    }}
                    disabled={busy === 'save-cidr'}
                    className="flex items-center gap-2 px-3 py-2 text-sm rounded bg-accent-500 hover:bg-accent-600 text-white disabled:opacity-50">
                    {busy === 'save-cidr' ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                    {t('anti_ddos.save')}
                  </button>
                </div>
                {(settings?.user_cidrs?.length ?? 0) > 0 && (
                  <div className="flex flex-wrap gap-2 pt-1">
                    {settings!.user_cidrs.map(cidr => (
                      <span key={cidr} className="inline-flex items-center gap-1 px-2 py-1 rounded bg-dark-800 text-xs font-mono">
                        {cidr}
                        <button
                          onClick={async () => {
                            const list = settings!.user_cidrs.filter(c => c !== cidr)
                            setCidrText(list.join('\n'))
                            await patchSettings({ user_cidrs: list })
                          }}
                          className="text-dark-500 hover:text-red-400">
                          <Trash2 className="w-3 h-3" />
                        </button>
                      </span>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}

          {activeTab === 'info' && (
            <div className="card p-5 space-y-5 text-sm leading-relaxed">
              {(t('anti_ddos.info_sections', { returnObjects: true }) as { title: string; body: string }[]).map((section, i) => (
                <div key={i}>
                  <h3 className="font-semibold text-dark-100 mb-1">{section.title}</h3>
                  <p className="text-dark-400">{section.body}</p>
                </div>
              ))}
            </div>
          )}

        </motion.div>
      </AnimatePresence>
    </div>
  )
}
