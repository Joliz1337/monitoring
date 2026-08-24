import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import {
  ArrowLeft,
  Route as RouteIcon,
  RefreshCw,
  Loader2,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Activity,
  FileText,
  Trash2,
  Wrench,
  ArrowDownToLine,
  ArrowUpFromLine,
} from 'lucide-react'
import { proxyApi, dnatProfilesApi, DnatNodeState, DnatRuleCounters } from '../api/client'
import { useServersStore } from '../stores/serversStore'
import NodeRestrictedNotice from '../components/servers/NodeRestrictedNotice'
import { nodeAllows } from '../utils/nodeCapabilities'
import { formatBytes, formatBitsPerSecLocalized } from '../utils/format'
import { formatListen, formatTarget, protocolLabel } from '../utils/dnat'
import { useAutoRefresh } from '../hooks/useAutoRefresh'
import { useModuleEnabled } from '../hooks/useModuleEnabled'
import { Tooltip } from '../components/ui/Tooltip'
import { FAQIcon } from '../components/FAQ'

const REFRESH_INTERVAL_MS = 5000

interface RuleRates {
  connsPerSec: number
  bytesInPerSec: number
  bytesOutPerSec: number
}

interface CounterSample {
  at: number
  byName: Record<string, DnatRuleCounters>
}

/** Скорости между двумя опросами: ядро отдаёт только кумулятивные счётчики */
function computeRates(previous: CounterSample | null, current: CounterSample): Record<string, RuleRates> {
  const rates: Record<string, RuleRates> = {}
  if (!previous) return rates
  const seconds = (current.at - previous.at) / 1000
  if (seconds <= 0) return rates
  for (const [name, now] of Object.entries(current.byName)) {
    const before = previous.byName[name]
    if (!before) continue
    const delta = (a: number, b: number) => (a >= b ? (a - b) / seconds : 0)
    rates[name] = {
      connsPerSec: delta(now.conns, before.conns),
      bytesInPerSec: delta(now.bytes_in, before.bytes_in),
      bytesOutPerSec: delta(now.bytes_out, before.bytes_out),
    }
  }
  return rates
}

function extractErrorMessage(err: unknown, fallback: string): string {
  const e = err as { response?: { data?: { detail?: unknown } }; message?: string }
  const detail = e?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (e?.message) return e.message
  return fallback
}

export default function Dnat() {
  const { uid, serverId } = useParams()
  const navigate = useNavigate()
  const { t } = useTranslation()
  const { servers, fetchServers } = useServersStore()
  const server = servers.find(s => s.id === Number(serverId))
  const dnatReadable = nodeAllows(server, 'dnat', 'read')
  const dnatWritable = nodeAllows(server, 'dnat', 'write')
  const profilesEnabled = useModuleEnabled('dnat-profiles')

  const [state, setState] = useState<DnatNodeState | null>(null)
  const [rates, setRates] = useState<Record<string, RuleRates>>({})
  const [isLoading, setIsLoading] = useState(true)
  const [isRefreshing, setIsRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [unsupported, setUnsupported] = useState(false)
  const [actionLoading, setActionLoading] = useState<'reapply' | 'clear' | null>(null)
  const [profileInfo, setProfileInfo] = useState<{ name: string; id: number } | null>(null)
  const previousSampleRef = useRef<CounterSample | null>(null)

  useEffect(() => {
    if (!serverId) return
    dnatProfilesApi.getAvailableServers().then(res => {
      const srv = res.data.find(s => s.id === Number(serverId))
      if (srv?.active_profile_id) {
        dnatProfilesApi.get(srv.active_profile_id)
          .then(pRes => setProfileInfo({ name: pRes.data.name, id: pRes.data.id }))
          .catch(() => {})
      }
    }).catch(() => {})
  }, [serverId])

  const fetchState = useCallback(async () => {
    if (!serverId || !dnatReadable) {
      setIsLoading(false)
      return
    }
    setIsRefreshing(true)
    try {
      const res = await proxyApi.getDnatState(Number(serverId))
      const sample: CounterSample = {
        at: Date.now(),
        byName: Object.fromEntries(res.data.counters.map(c => [c.name, c])),
      }
      setRates(computeRates(previousSampleRef.current, sample))
      previousSampleRef.current = sample
      setState(res.data)
      setError(null)
      setUnsupported(false)
    } catch (err) {
      const status = (err as { response?: { status?: number } }).response?.status
      if (status === 404) {
        setUnsupported(true)
        setError(null)
      } else {
        setError(extractErrorMessage(err, t('dnat.load_error')))
      }
    } finally {
      setIsLoading(false)
      setIsRefreshing(false)
    }
  }, [serverId, dnatReadable, t])

  useEffect(() => {
    fetchServers()
  }, [fetchServers])

  useAutoRefresh(fetchState, { customInterval: REFRESH_INTERVAL_MS })

  const handleReapply = async () => {
    if (!serverId) return
    setActionLoading('reapply')
    try {
      const res = await proxyApi.reapplyDnat(Number(serverId))
      toast.success(res.data.message)
      await fetchState()
    } catch (err) {
      toast.error(extractErrorMessage(err, t('dnat.action_error')))
    } finally {
      setActionLoading(null)
    }
  }

  const handleClear = async () => {
    if (!serverId) return
    if (!confirm(t('dnat.clear_confirm'))) return
    setActionLoading('clear')
    try {
      const res = await proxyApi.clearDnat(Number(serverId))
      if (res.data.success) toast.success(t('dnat.cleared'))
      else toast.error(res.data.message)
      previousSampleRef.current = null
      await fetchState()
    } catch (err) {
      toast.error(extractErrorMessage(err, t('dnat.action_error')))
    } finally {
      setActionLoading(null)
    }
  }

  const countersByName = useMemo(
    () => Object.fromEntries((state?.counters ?? []).map(c => [c.name, c])),
    [state],
  )
  const activeRules = state?.rules.filter(r => r.enabled).length ?? 0
  const totals = useMemo(() => {
    const list = state?.counters ?? []
    return {
      conns: list.reduce((sum, c) => sum + c.conns, 0),
      bytesIn: list.reduce((sum, c) => sum + c.bytes_in, 0),
      bytesOut: list.reduce((sum, c) => sum + c.bytes_out, 0),
    }
  }, [state])

  const healthCard = (() => {
    if (!state) return null
    if (!state.available) {
      return { icon: <XCircle className="w-6 h-6 text-danger" />, label: t('dnat.status_unavailable'), hint: state.message ?? '', color: 'border-danger/30' }
    }
    if (!state.healthy) {
      return { icon: <AlertTriangle className="w-6 h-6 text-warning" />, label: t('dnat.status_drift'), hint: t('dnat.status_drift_hint', { items: state.missing.join(', ') }), color: 'border-warning/30' }
    }
    if (activeRules === 0) {
      return { icon: <CheckCircle2 className="w-6 h-6 text-dark-400" />, label: t('dnat.status_idle'), hint: t('dnat.status_idle_hint'), color: 'border-dark-700/50' }
    }
    return { icon: <CheckCircle2 className="w-6 h-6 text-success" />, label: t('dnat.status_ok'), hint: t('dnat.status_ok_hint', { count: activeRules }), color: 'border-success/30' }
  })()

  return (
    <div>
      <motion.div
        className="flex items-center gap-4 mb-6"
        initial={{ opacity: 0, y: -20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
      >
        <Tooltip label={t('common.back')}>
          <motion.button
            onClick={() => navigate(`/${uid}/server/${serverId}`)}
            className="p-2.5 hover:bg-dark-800 rounded-xl text-dark-400 hover:text-dark-200 transition-all"
            whileHover={{ scale: 1.05, x: -2 }}
            whileTap={{ scale: 0.95 }}
          >
            <ArrowLeft className="w-5 h-5" />
          </motion.button>
        </Tooltip>
        <div className="flex-1">
          <motion.h1
            className="text-2xl font-bold text-dark-50 flex items-center gap-3"
            initial={{ opacity: 0, x: -10 }}
            animate={{ opacity: 1, x: 0 }}
          >
            <RouteIcon className="w-7 h-7 text-accent-400" />
            {t('dnat.title')}
            <FAQIcon screen="PAGE_DNAT_PROFILES" />
            {isRefreshing && (
              <motion.div initial={{ opacity: 0, scale: 0.8 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0 }}>
                <Loader2 className="w-5 h-5 text-accent-400 animate-spin" />
              </motion.div>
            )}
          </motion.h1>
          <motion.p className="text-dark-400 mt-1" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.1 }}>
            {server?.name}
          </motion.p>
        </div>
        {dnatWritable && state && (
          <>
            <Tooltip label={t('dnat.reapply_hint')}>
              <motion.button
                onClick={handleReapply}
                disabled={actionLoading !== null}
                className="btn btn-secondary text-sm"
                whileHover={{ scale: 1.02 }}
                whileTap={{ scale: 0.98 }}
              >
                {actionLoading === 'reapply' ? <Loader2 className="w-4 h-4 animate-spin" /> : <Wrench className="w-4 h-4" />}
                {t('dnat.reapply')}
              </motion.button>
            </Tooltip>
            <Tooltip label={t('dnat.clear_hint')}>
              <motion.button
                onClick={handleClear}
                disabled={actionLoading !== null || state.rules.length === 0}
                className="btn btn-secondary text-sm text-danger hover:bg-danger/10 disabled:opacity-40"
                whileHover={{ scale: 1.02 }}
                whileTap={{ scale: 0.98 }}
              >
                {actionLoading === 'clear' ? <Loader2 className="w-4 h-4 animate-spin" /> : <Trash2 className="w-4 h-4" />}
                {t('dnat.clear')}
              </motion.button>
            </Tooltip>
          </>
        )}
        <Tooltip label={t('common.refresh_data')}>
          <motion.button
            onClick={fetchState}
            disabled={isRefreshing}
            className="p-2.5 hover:bg-dark-800 rounded-xl text-dark-400 hover:text-dark-200 transition-all disabled:opacity-50"
            whileHover={{ scale: 1.05 }}
            whileTap={{ scale: 0.95 }}
          >
            <RefreshCw className={`w-5 h-5 ${isRefreshing ? 'animate-spin' : ''}`} />
          </motion.button>
        </Tooltip>
      </motion.div>

      {profileInfo ? (
        <motion.div
          initial={{ opacity: 0, y: -10 }}
          animate={{ opacity: 1, y: 0 }}
          className="mb-4 flex items-center justify-between px-4 py-3 rounded-xl bg-accent-500/10 border border-accent-500/20"
        >
          <div className="flex items-center gap-2 text-sm">
            <FileText className="w-4 h-4 text-accent-400" />
            <span className="text-dark-300">{t('dnat.managed_by_profile')}:</span>
            <span className="font-medium text-accent-300">{profileInfo.name}</span>
          </div>
          {profilesEnabled && (
            <Link to={`/${uid}/dnat-profiles`} className="text-xs text-accent-400 hover:text-accent-300 transition-colors">
              {t('dnat.view_profile')} →
            </Link>
          )}
        </motion.div>
      ) : (
        <motion.div
          initial={{ opacity: 0, y: -10 }}
          animate={{ opacity: 1, y: 0 }}
          className="mb-4 flex items-center justify-between px-4 py-3 rounded-xl bg-dark-800/40 border border-dark-700/40"
        >
          <span className="text-sm text-dark-400">{t('dnat.no_profile')}</span>
          {profilesEnabled && (
            <Link to={`/${uid}/dnat-profiles`} className="text-xs text-accent-400 hover:text-accent-300 transition-colors">
              {t('dnat.open_profiles')} →
            </Link>
          )}
        </motion.div>
      )}

      <AnimatePresence mode="wait">
        {!dnatReadable ? (
          <motion.div key="restricted">
            <NodeRestrictedNotice server={server} />
          </motion.div>
        ) : isLoading ? (
          <motion.div key="loading" className="flex items-center justify-center py-20">
            <Loader2 className="w-8 h-8 text-accent-400 animate-spin" />
          </motion.div>
        ) : unsupported ? (
          <motion.div key="unsupported" className="card text-center py-16" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
            <AlertTriangle className="w-16 h-16 text-warning/70 mx-auto mb-4" />
            <h2 className="text-lg font-semibold text-dark-200 mb-2">{t('dnat.node_too_old')}</h2>
            <p className="text-dark-400 text-sm">{t('dnat.node_too_old_hint')}</p>
          </motion.div>
        ) : error ? (
          <motion.div key="error" className="card text-center py-16" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
            <AlertTriangle className="w-16 h-16 text-warning/70 mx-auto mb-4" />
            <p className="text-dark-400">{error}</p>
          </motion.div>
        ) : state && healthCard ? (
          <motion.div key="content" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
            {!dnatWritable && <div className="mb-4"><NodeRestrictedNotice server={server} variant="readonly" compact /></div>}

            <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
              <div className={`card border ${healthCard.color}`}>
                <div className="flex items-center gap-3">
                  {healthCard.icon}
                  <div className="min-w-0">
                    <p className="font-semibold text-dark-100">{healthCard.label}</p>
                    <p className="text-xs text-dark-400 truncate">{healthCard.hint}</p>
                  </div>
                </div>
              </div>
              <div className={`card border ${state.ip_forward ? 'border-dark-700/50' : 'border-warning/30'}`}>
                <div className="flex items-center gap-3">
                  {state.ip_forward
                    ? <CheckCircle2 className="w-6 h-6 text-success" />
                    : <AlertTriangle className="w-6 h-6 text-warning" />}
                  <div>
                    <p className="font-semibold text-dark-100">net.ipv4.ip_forward</p>
                    <p className="text-xs text-dark-400">{state.ip_forward ? t('dnat.ip_forward_on') : t('dnat.ip_forward_off')}</p>
                  </div>
                </div>
              </div>
              <div className="card border border-dark-700/50">
                <div className="flex items-center gap-3">
                  <Activity className="w-6 h-6 text-accent-400" />
                  <div>
                    <p className="font-semibold text-dark-100">
                      {t('dnat.totals', { conns: totals.conns.toLocaleString() })}
                    </p>
                    <p className="text-xs text-dark-400 font-mono">
                      ↓ {formatBytes(totals.bytesIn)} · ↑ {formatBytes(totals.bytesOut)}
                    </p>
                  </div>
                </div>
              </div>
            </div>

            <div className="card">
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-lg font-semibold text-dark-100 flex items-center gap-2">
                  <RouteIcon className="w-5 h-5 text-accent-500" />
                  {t('dnat.rules')}
                  <span className="text-sm text-dark-500">({activeRules}/{state.rules.length})</span>
                </h2>
                {state.applied_at && (
                  <span className="text-xs text-dark-500">
                    {t('dnat.applied_at', { time: new Date(state.applied_at).toLocaleString() })}
                  </span>
                )}
              </div>

              {state.rules.length === 0 ? (
                <div className="text-center py-12 text-dark-500">
                  <RouteIcon className="w-12 h-12 mx-auto mb-3 opacity-50" />
                  <p>{t('dnat.no_rules')}</p>
                  <p className="text-xs mt-1">{t('dnat.no_rules_hint')}</p>
                </div>
              ) : (
                <div className="overflow-x-auto rounded-lg border border-dark-800/50">
                  <table className="w-full text-sm">
                    <thead className="bg-dark-900/40 text-dark-400 text-xs">
                      <tr>
                        <th className="text-left px-3 py-2 font-medium">{t('dnat.col_rule')}</th>
                        <th className="text-left px-3 py-2 font-medium">{t('dnat.col_route')}</th>
                        <th className="text-right px-3 py-2 font-medium">{t('dnat.col_conns')}</th>
                        <th className="text-right px-3 py-2 font-medium">
                          <span className="inline-flex items-center gap-1"><ArrowDownToLine className="w-3 h-3" /> {t('dnat.col_in')}</span>
                        </th>
                        <th className="text-right px-3 py-2 font-medium">
                          <span className="inline-flex items-center gap-1"><ArrowUpFromLine className="w-3 h-3" /> {t('dnat.col_out')}</span>
                        </th>
                        <th className="text-right px-3 py-2 font-medium">{t('dnat.col_status')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {state.rules.map(rule => {
                        const counters = countersByName[rule.name]
                        const rate = rates[rule.name]
                        const targetRows = (counters?.targets ?? []).length > 1 ? counters.targets : []
                        return (
                          <Fragment key={rule.name}>
                          <tr className={`border-t border-dark-800/40 hover:bg-dark-800/30 transition-colors ${rule.enabled ? '' : 'opacity-50'}`}>
                            <td className="px-3 py-2">
                              <div className="font-medium text-dark-100 font-mono">{rule.name}</div>
                              {rule.comment && <div className="text-xs text-dark-500 truncate max-w-xs">{rule.comment}</div>}
                            </td>
                            <td className="px-3 py-2 font-mono text-dark-200 whitespace-nowrap">
                              <span className="text-dark-400 text-xs mr-2">{protocolLabel(rule.protocol)}</span>
                              :{formatListen(rule)} → {formatTarget(rule)}
                              {!rule.masquerade && (
                                <span className="ml-2 px-1.5 py-0.5 rounded text-[10px] bg-warning/10 text-warning border border-warning/20">
                                  {t('dnat_profiles.no_masq_badge')}
                                </span>
                              )}
                            </td>
                            <td className="px-3 py-2 text-right font-mono text-dark-200 whitespace-nowrap">
                              {counters ? counters.conns.toLocaleString() : '—'}
                              {rate && rate.connsPerSec > 0 && (
                                <span className="block text-[11px] text-accent-400">+{rate.connsPerSec.toFixed(1)}/s</span>
                              )}
                            </td>
                            <td className="px-3 py-2 text-right font-mono text-dark-200 whitespace-nowrap">
                              {counters ? formatBytes(counters.bytes_in) : '—'}
                              {rate && (
                                <span className="block text-[11px] text-dark-500">{formatBitsPerSecLocalized(rate.bytesInPerSec, t)}</span>
                              )}
                            </td>
                            <td className="px-3 py-2 text-right font-mono text-dark-200 whitespace-nowrap">
                              {counters ? formatBytes(counters.bytes_out) : '—'}
                              {rate && (
                                <span className="block text-[11px] text-dark-500">{formatBitsPerSecLocalized(rate.bytesOutPerSec, t)}</span>
                              )}
                            </td>
                            <td className="px-3 py-2 text-right">
                              {!rule.enabled ? (
                                <span className="px-2 py-0.5 rounded-md text-[11px] bg-dark-700/50 text-dark-400 border border-dark-600/40">
                                  {t('dnat_profiles.disabled_badge')}
                                </span>
                              ) : counters?.present ? (
                                <span className="px-2 py-0.5 rounded-md text-[11px] bg-success/10 text-success border border-success/20">
                                  {t('dnat.rule_active')}
                                </span>
                              ) : (
                                <span className="px-2 py-0.5 rounded-md text-[11px] bg-danger/10 text-danger border border-danger/20">
                                  {t('dnat.rule_missing')}
                                </span>
                              )}
                            </td>
                          </tr>
                          {targetRows.map(target => (
                            <tr key={`${rule.name}@${target.ip}`} className="bg-dark-900/30 text-xs">
                              <td className="px-3 py-1 text-dark-500 pl-8">↳ {t(`dnat_profiles.distribution_${rule.distribution ?? 'per_server'}`)}</td>
                              <td className="px-3 py-1 font-mono text-dark-300">→ {target.ip}</td>
                              <td className="px-3 py-1 text-right font-mono text-dark-300">{target.conns.toLocaleString()}</td>
                              <td className="px-3 py-1 text-right font-mono text-dark-300">{formatBytes(target.bytes_in)}</td>
                              <td className="px-3 py-1 text-right font-mono text-dark-300">{formatBytes(target.bytes_out)}</td>
                              <td className="px-3 py-1 text-right">
                                {target.present
                                  ? <span className="text-success">{t('dnat.rule_active')}</span>
                                  : <span className="text-danger">{t('dnat.rule_missing')}</span>}
                              </td>
                            </tr>
                          ))}
                          </Fragment>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </motion.div>
        ) : null}
      </AnimatePresence>
    </div>
  )
}
