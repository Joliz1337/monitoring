import { useEffect, useRef, useState, useCallback } from 'react'
import { useNodeCapabilities } from '../hooks/useNodeCapabilities'
import { useRememberedState } from '../hooks/useRememberedState'
import { useTranslation } from 'react-i18next'
import { motion, AnimatePresence } from 'framer-motion'
import { toast } from 'sonner'
import {
  Siren, ShieldAlert, ShieldCheck, Settings as SettingsIcon,
  BookOpen, Loader2, Radar, Lock, AlertTriangle, RotateCw,
} from 'lucide-react'
import {
  antiDdosApi, type AntiDdosFailedNode, type AntiDdosFleetAction, type AntiDdosSettings,
  type AntiDdosStatus, type NodeAntiDdosState,
} from '../api/client'
import { Toggle } from '../components/ui/Toggle'

type TabType = 'control' | 'info'

// Пока команда обходит парк, её ход читается чаще общего опроса страницы
const FLEET_PROGRESS_POLL_MS = 2000

function fleetActionKey(action: AntiDdosFleetAction): string {
  if (action.action === 'install') return 'anti_ddos.fleet_action.install'
  return `anti_ddos.fleet_action.${action.action}_${action.enabled ? 'on' : 'off'}`
}

function FailedNodes({ nodes, total }: { nodes: AntiDdosFailedNode[]; total: number }) {
  const { t } = useTranslation()
  return (
    <>
      <ul className="max-h-48 overflow-y-auto space-y-1 text-xs">
        {nodes.map(node => (
          <li key={node.server_id} className="flex gap-2">
            <span className="font-medium text-dark-200 shrink-0">{node.server_name}</span>
            <span className="text-dark-400 truncate" title={node.message}>{node.message}</span>
          </li>
        ))}
      </ul>
      {total > nodes.length && (
        <p className="text-xs text-dark-500">{t('anti_ddos.fleet_more', { count: total - nodes.length })}</p>
      )}
    </>
  )
}

function ModeBadge({ node }: { node: NodeAntiDdosState }) {
  const { t } = useTranslation()
  if (!node.emergency_mode) {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-green-500/15 text-green-400">
        <ShieldCheck className="w-3.5 h-3.5" /> {t('anti_ddos.mode_normal')}
      </span>
    )
  }
  const src = node.source === 'auto' ? t('anti_ddos.source_auto') : t('anti_ddos.source_manual')
  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-red-500/15 text-red-400">
      <ShieldAlert className="w-3.5 h-3.5" /> {t('anti_ddos.mode_emergency')} · {src}
    </span>
  )
}

export default function AntiDdos() {
  const { t } = useTranslation()
  const [activeTab, setActiveTab] = useRememberedState<TabType>('anti-ddos.tab', 'control')
  const [status, setStatus] = useState<AntiDdosStatus | null>(null)
  const [settings, setSettings] = useState<AntiDdosSettings | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<string | null>(null)
  const { allows } = useNodeCapabilities()
  // started_at команды, запущенной с этой страницы: по её завершении — тост
  const trackedAction = useRef<string | null>(null)

  const loadStatus = useCallback(async (): Promise<AntiDdosStatus | null> => {
    try {
      const { data } = await antiDdosApi.getStatus()
      setStatus(data)
      return data
    } catch {
      return null  // keep last
    }
  }, [])

  const loadSettings = useCallback(async () => {
    try {
      const { data } = await antiDdosApi.getSettings()
      setSettings(data)
    } catch { /* keep last */ }
  }, [])

  useEffect(() => {
    Promise.all([loadStatus(), loadSettings()]).finally(() => setLoading(false))
    const id = setInterval(() => { if (!document.hidden) loadStatus() }, 10000)
    return () => clearInterval(id)
  }, [loadStatus, loadSettings])

  const lastAction = status?.last_action ?? null
  const fleetRunning = !!lastAction && !lastAction.finished_at
  const rollout = status?.rollout ?? null

  useEffect(() => {
    if (!fleetRunning) return
    const id = setInterval(() => { if (!document.hidden) loadStatus() }, FLEET_PROGRESS_POLL_MS)
    return () => clearInterval(id)
  }, [fleetRunning, loadStatus])

  useEffect(() => {
    if (!lastAction?.finished_at || lastAction.started_at !== trackedAction.current) return
    trackedAction.current = null
    // Неудачу показывает блок со списком нод, тост — только об успехе
    if (lastAction.failed_count === 0) toast.success(t('anti_ddos.done'))
  }, [lastAction, t])

  const patchSettings = async (patch: Partial<AntiDdosSettings>) => {
    const previousAction = lastAction?.started_at
    try {
      const { data } = await antiDdosApi.updateSettings(patch)
      setSettings(data)
    } catch {
      toast.error(t('anti_ddos.save_failed'))
      return
    }
    if (patch.enabled === undefined) return
    // Мастер-переключатель запускает обход парка — его ход виден в last_action
    const started = (await loadStatus())?.last_action?.started_at
    if (started && started !== previousAction) trackedAction.current = started
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

  const runFleetAction = async (key: string, start: () => Promise<{ data: { action: AntiDdosFleetAction } }>) => {
    setBusy(key)
    try {
      const { data } = await start()
      trackedAction.current = data.action.started_at
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
                {/* Auto-detection (watchdog) — fleet-wide master switch */}
                <div className="flex items-center justify-between">
                  <div>
                    <h2 className="text-sm font-semibold flex items-center gap-2"><Radar className="w-4 h-4" /> {t('anti_ddos.autodetect_title')}</h2>
                    <p className="text-xs text-dark-400 mt-1">{t('anti_ddos.autodetect_hint')}</p>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <span className="text-xs text-dark-400">{t('anti_ddos.autodetect_all')}</span>
                    <Toggle on={!!settings?.enabled} onClick={() => settings && patchSettings({ enabled: !settings.enabled })} />
                  </div>
                </div>

                <div className={`text-sm px-3 py-2 rounded ${activeCount > 0 ? 'bg-red-500/10 text-red-400' : 'bg-dark-800/60 text-dark-300'}`}>
                  {activeCount > 0
                    ? t('anti_ddos.nodes_under_attack', { count: activeCount, total: status?.total ?? 0 })
                    : t('anti_ddos.all_normal', { total: status?.total ?? 0 })}
                </div>

                {/* Manual emergency — separate control, independent of auto-detection */}
                <div className="border-t border-dark-800 pt-3">
                  <div className="text-xs font-medium text-dark-300 mb-2 flex items-center gap-1.5">
                    <ShieldAlert className="w-3.5 h-3.5 text-red-400" /> {t('anti_ddos.emergency_manual_title')}
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <button
                      onClick={() => runFleetAction('emg-all-on', () => antiDdosApi.emergencyAll(true))}
                      disabled={busy === 'emg-all-on'}
                      className="flex items-center gap-2 px-3 py-2 text-sm rounded bg-red-500/90 hover:bg-red-500 text-white disabled:opacity-50">
                      {busy === 'emg-all-on' ? <Loader2 className="w-4 h-4 animate-spin" /> : <ShieldAlert className="w-4 h-4" />}
                      {t('anti_ddos.enable_all')}
                    </button>
                    <button
                      onClick={() => runFleetAction('emg-all-off', () => antiDdosApi.emergencyAll(false))}
                      disabled={busy === 'emg-all-off'}
                      className="flex items-center gap-2 px-3 py-2 text-sm rounded bg-dark-700 hover:bg-dark-600 text-dark-100 disabled:opacity-50">
                      {busy === 'emg-all-off' ? <Loader2 className="w-4 h-4 animate-spin" /> : <ShieldCheck className="w-4 h-4" />}
                      {t('anti_ddos.disable_all')}
                    </button>
                  </div>
                </div>

                {lastAction && fleetRunning && (
                  <div className="text-sm px-3 py-2 rounded bg-dark-800/60 text-dark-300 flex items-center gap-2">
                    <Loader2 className="w-4 h-4 animate-spin shrink-0" />
                    {t('anti_ddos.fleet_running', {
                      action: t(fleetActionKey(lastAction)),
                      done: lastAction.ok + lastAction.failed_count,
                      total: lastAction.total,
                    })}
                  </div>
                )}

                {lastAction && !fleetRunning && lastAction.failed_count > 0 && (
                  <div className="rounded border border-yellow-500/30 bg-yellow-500/10 p-3 space-y-2">
                    <div className="flex items-start justify-between gap-3">
                      <div className="text-sm text-yellow-400 flex items-center gap-2">
                        <AlertTriangle className="w-4 h-4 shrink-0" />
                        {t('anti_ddos.fleet_not_applied', {
                          action: t(fleetActionKey(lastAction)),
                          count: lastAction.failed_count,
                          total: lastAction.total,
                        })}
                      </div>
                      <button
                        onClick={() => runFleetAction('retry-failed', antiDdosApi.retryFailed)}
                        disabled={busy === 'retry-failed'}
                        className="flex items-center gap-2 px-3 py-1.5 text-xs rounded bg-dark-700 hover:bg-dark-600 text-dark-100 disabled:opacity-50 shrink-0">
                        {busy === 'retry-failed' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RotateCw className="w-3.5 h-3.5" />}
                        {t('anti_ddos.retry')}
                      </button>
                    </div>
                    <FailedNodes nodes={lastAction.failed} total={lastAction.failed_count} />
                  </div>
                )}

                {rollout && rollout.failed > 0 && (
                  <div className="rounded border border-yellow-500/30 bg-yellow-500/10 p-3 space-y-2">
                    <div className="text-sm text-yellow-400 flex items-center gap-2">
                      <AlertTriangle className="w-4 h-4 shrink-0" />
                      {t(rollout.halted ? 'anti_ddos.rollout_halted' : 'anti_ddos.rollout_failed', {
                        version: rollout.version,
                        count: rollout.failed,
                      })}
                    </div>
                    <FailedNodes nodes={rollout.failed_nodes} total={rollout.failed} />
                  </div>
                )}
              </div>

              <div className="card divide-y divide-dark-800">
                {(status?.nodes ?? []).length === 0 && (
                  <div className="p-6 text-center text-sm text-dark-500">{t('anti_ddos.no_nodes')}</div>
                )}
                {(status?.nodes ?? []).map(node => {
                  const nodeAllowed = allows(node.server_id, 'antiddos', 'write')
                  return (
                  <div key={node.server_id} className="p-4 flex items-center justify-between gap-4">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-medium truncate">{node.server_name}</span>
                        <ModeBadge node={node} />
                        {!nodeAllowed && (
                          <span className="flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-purple/10 text-purple text-2xs font-medium">
                            <Lock className="w-3.5 h-3.5" />
                            {t('node_caps.row_blocked')}
                          </span>
                        )}
                      </div>
                      {node.emergency_mode && node.reason && (
                        <p className="text-xs text-dark-400 mt-1 truncate">{t('anti_ddos.reason')}: {node.reason}</p>
                      )}
                    </div>
                    <div className="flex items-center gap-5 shrink-0">
                      <div className="flex items-center gap-2">
                        <Radar className="w-3.5 h-3.5 text-dark-400" />
                        <span className="text-xs text-dark-400">{t('anti_ddos.watchdog')}</span>
                        <Toggle on={node.watchdog} onClick={() => toggleNodeWatchdog(node)} disabled={!nodeAllowed || busy === `wd-${node.server_id}`} />
                      </div>
                      <div className="flex items-center gap-2">
                        <span className="text-xs text-dark-400">{t('anti_ddos.emergency')}</span>
                        <Toggle on={node.emergency_mode} onClick={() => toggleNodeEmergency(node)} disabled={!nodeAllowed || busy === `emg-${node.server_id}`} />
                      </div>
                    </div>
                  </div>
                  )
                })}
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
