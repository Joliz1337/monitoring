import { useCallback, useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { motion } from 'framer-motion'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import {
  ArrowLeft,
  Shuffle,
  RefreshCw,
  Loader2,
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Clock,
} from 'lucide-react'
import {
  sourcePoolApi,
  type SourcePoolAssignments,
  type SourcePoolInstallStatus,
  type SourcePoolMode,
  type SourcePoolNodePatch,
  type SourcePoolNodeView,
} from '../api/client'
import { useServersStore } from '../stores/serversStore'
import NodeRestrictedNotice from '../components/servers/NodeRestrictedNotice'
import MarkLayoutCard from '../components/sourcepool/MarkLayoutCard'
import SourcePoolSnippetBlock from '../components/sourcepool/SourcePoolSnippetBlock'
import { nodeAllows } from '../utils/nodeCapabilities'
import { addressDotClass, bindingsAsAssignments, formatMarks, marksByAddress, poolMarks } from '../utils/sourcePool'
import { useAutoRefresh } from '../hooks/useAutoRefresh'
import { Tooltip } from '../components/ui/Tooltip'
import { Toggle } from '../components/ui/Toggle'
import { Checkbox } from '../components/ui/Checkbox'
import { FAQIcon } from '../components/FAQ'

const REFRESH_INTERVAL_MS = 10000

function extractErrorMessage(err: unknown, fallback: string): string {
  const e = err as { response?: { data?: { detail?: unknown } }; message?: string }
  const detail = e?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (e?.message) return e.message
  return fallback
}

interface StatusCard {
  icon: JSX.Element
  label: string
  hint: string
  color: string
}

export default function SourcePool() {
  const { uid, serverId } = useParams()
  const navigate = useNavigate()
  const { t } = useTranslation()
  const { servers, fetchServers } = useServersStore()
  const server = servers.find(s => s.id === Number(serverId))
  const systemWritable = nodeAllows(server, 'system', 'write')

  const [view, setView] = useState<SourcePoolNodeView | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [isRefreshing, setIsRefreshing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const fetchView = useCallback(async () => {
    if (!serverId) return
    setIsRefreshing(true)
    try {
      const res = await sourcePoolApi.getNode(Number(serverId))
      setView(res.data)
      setError(null)
    } catch (err) {
      setError(extractErrorMessage(err, t('source_pool.load_error')))
    } finally {
      setIsLoading(false)
      setIsRefreshing(false)
    }
  }, [serverId, t])

  useEffect(() => {
    fetchServers()
  }, [fetchServers])

  useAutoRefresh(fetchView, { customInterval: REFRESH_INTERVAL_MS })

  // Ручное обновление — с опросом ноды, а не только из базы панели
  const handleRefresh = async () => {
    if (!serverId || !view?.enabled) {
      await fetchView()
      return
    }
    setIsRefreshing(true)
    try {
      const res = await sourcePoolApi.refreshNode(Number(serverId))
      setView(res.data)
      setError(null)
    } catch (err) {
      toast.error(extractErrorMessage(err, t('source_pool.load_error')))
    } finally {
      setIsRefreshing(false)
    }
  }

  const save = async (patch: SourcePoolNodePatch) => {
    if (!serverId) return
    setSaving(true)
    try {
      const res = await sourcePoolApi.updateNode(Number(serverId), patch)
      setView(res.data)
      if (res.data.sync_error) toast.error(res.data.sync_error)
      else toast.success(t('source_pool.saved'))
    } catch (err) {
      toast.error(extractErrorMessage(err, t('source_pool.save_error')))
    } finally {
      setSaving(false)
    }
  }

  const toggleAddress = (address: string, participates: boolean) => {
    if (!view) return
    const excluded = new Set(view.excluded)
    if (participates) excluded.delete(address)
    else excluded.add(address)
    save({ excluded: Array.from(excluded) })
  }

  const changeMode = (mode: SourcePoolMode) => {
    if (!view) return
    // Первый переход на ручную начинается с текущей раскладки, чтобы трафик не сместился в момент переключения
    if (mode === 'manual' && Object.keys(view.assignments).length === 0) {
      save({ mode, assignments: bindingsAsAssignments(view.bindings) })
      return
    }
    save({ mode })
  }

  const saveAssignments = (assignments: SourcePoolAssignments) => save({ assignments })

  const statusLabel = (status: SourcePoolInstallStatus) => t(`source_pool.status_${status}`)

  const activeHint = (current: SourcePoolNodeView): string => {
    if (current.mode === 'manual') {
      const parts = [
        current.bindings.length > 0
          ? t('source_pool.summary', { marks: current.bindings.length, addresses: current.active_count })
          : t('source_pool.manual_empty'),
      ]
      if (current.unavailable_marks.length > 0) {
        parts.push(t('source_pool.unavailable_hint', { items: formatMarks(current.unavailable_marks) }))
      }
      return parts.join(' · ')
    }
    return current.active_count >= 2
      ? t('source_pool.summary', { marks: current.mark_count ?? 0, addresses: current.active_count })
      : t('source_pool.single_address')
  }

  const statusCard = ((): StatusCard | null => {
    if (!view) return null
    const base = { label: statusLabel(view.install_status), hint: '' }
    switch (view.install_status) {
      case 'active':
        return { ...base, icon: <CheckCircle2 className="w-6 h-6 text-success" />, color: 'border-success/30',
          hint: activeHint(view) }
      case 'drift':
        return { ...base, icon: <AlertTriangle className="w-6 h-6 text-warning" />, color: 'border-warning/30',
          hint: [t('source_pool.status_drift_hint', { items: view.missing_marks.join(', ') }), view.node_error].filter(Boolean).join(' · ') }
      case 'pending':
        return { ...base, icon: <Clock className="w-6 h-6 text-accent-400" />, color: 'border-accent-500/30', hint: view.sync_error ?? '' }
      case 'off':
        return { ...base, icon: <CheckCircle2 className="w-6 h-6 text-dark-400" />, color: 'border-dark-700/50', hint: '' }
      default:
        return { ...base, icon: <XCircle className="w-6 h-6 text-danger" />, color: 'border-danger/30',
          hint: view.conflict ?? view.sync_error ?? view.node_error ?? '' }
    }
  })()

  const canConfigure = systemWritable && !!view && view.supported_by_node
  const canToggle = canConfigure && !saving
  const marks = poolMarks(view)
  const manual = view?.mode === 'manual'
  const addressMarks = marksByAddress(view?.enabled ? view.bindings : [])

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
            <Shuffle className="w-7 h-7 text-accent-400" />
            {t('source_pool.title')}
            <FAQIcon screen="PAGE_SOURCE_POOL" />
            {isRefreshing && (
              <motion.div initial={{ opacity: 0, scale: 0.8 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0 }}>
                <Loader2 className="w-5 h-5 text-accent-400 animate-spin" />
              </motion.div>
            )}
          </motion.h1>
          <motion.p className="text-dark-400 mt-1" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.1 }}>
            {server?.name} · {t('source_pool.subtitle')}
          </motion.p>
        </div>
        <Tooltip label={t('common.refresh_data')}>
          <motion.button
            onClick={handleRefresh}
            disabled={isRefreshing}
            className="p-2.5 hover:bg-dark-800 rounded-xl text-dark-400 hover:text-dark-200 transition-all disabled:opacity-50"
            whileHover={{ scale: 1.05 }}
            whileTap={{ scale: 0.95 }}
          >
            <RefreshCw className={`w-5 h-5 ${isRefreshing ? 'animate-spin' : ''}`} />
          </motion.button>
        </Tooltip>
      </motion.div>

      {!systemWritable && <div className="mb-4"><NodeRestrictedNotice server={server} variant="readonly" /></div>}

      {isLoading ? (
        <div className="flex items-center justify-center h-48">
          <Loader2 className="w-6 h-6 animate-spin text-dark-500" />
        </div>
      ) : error ? (
        <div className="card text-center py-12">
          <XCircle className="w-12 h-12 text-danger/50 mx-auto mb-3" />
          <p className="text-danger">{error}</p>
        </div>
      ) : view && (
        <div className="space-y-4">
          {!view.supported_by_node && (
            <motion.div
              initial={{ opacity: 0, y: -10 }}
              animate={{ opacity: 1, y: 0 }}
              className="flex items-start gap-3 px-4 py-3 rounded-xl bg-warning/10 border border-warning/20"
            >
              <AlertTriangle className="w-5 h-5 text-warning shrink-0 mt-0.5" />
              <div className="text-sm">
                <div className="text-dark-100 font-medium">{t('source_pool.node_too_old')}</div>
                <div className="text-dark-400 text-xs mt-0.5">{t('source_pool.node_too_old_hint', { version: view.min_node_version })}</div>
              </div>
            </motion.div>
          )}

          <motion.div
            className={`card border ${statusCard?.color ?? 'border-dark-700/50'}`}
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
          >
            <div className="flex items-start gap-4">
              <div className="shrink-0 mt-0.5">{statusCard?.icon}</div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center justify-between gap-4">
                  <div>
                    <div className="text-dark-100 font-medium">{t('source_pool.enable')}</div>
                    <div className="text-xs text-dark-400 mt-0.5">{t('source_pool.enable_hint', { from: marks[0], to: marks[marks.length - 1] })}</div>
                  </div>
                  <Toggle
                    on={view.enabled}
                    disabled={!canToggle}
                    onClick={() => save({ enabled: !view.enabled })}
                    title={canToggle ? undefined : t('node_caps.exec_blocked')}
                  />
                </div>
                <div className="mt-3 flex items-center gap-2 text-sm">
                  <span className="font-medium text-dark-200">{statusCard?.label}</span>
                  {statusCard?.hint && <span className="text-dark-400 text-xs">— {statusCard.hint}</span>}
                </div>
              </div>
            </div>
            <p className="text-xs text-dark-500 mt-4 leading-relaxed">{t('source_pool.how_it_works')}</p>
          </motion.div>

          <motion.div className="card" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.05 }}>
            <div className="flex items-center justify-between mb-1">
              <h2 className="text-dark-100 font-medium">
                {t('source_pool.addresses', { iface: view.interface ?? '—' })}
              </h2>
              {view.addresses.length > 0 && !manual && (
                <span className="text-xs text-dark-500">
                  {t('source_pool.summary', { marks: marks.length, addresses: view.active_count })}
                </span>
              )}
            </div>
            <p className="text-xs text-dark-500 mb-3">
              {manual ? t('source_pool.addresses_hint_manual') : t('source_pool.addresses_hint')}
            </p>
            {view.addresses.length === 0 ? (
              <p className="text-sm text-dark-400 py-4 text-center">{t('source_pool.no_addresses')}</p>
            ) : (
              <div className="divide-y divide-dark-800">
                {view.addresses.map((item, index) => {
                  const ownMarks = addressMarks.get(item.address) ?? []
                  const struck = !manual && item.excluded
                  const label = struck
                    ? t('source_pool.excluded')
                    : view.enabled && item.marks > 0
                      ? t('source_pool.marks', { count: item.marks })
                      : manual ? t('source_pool.unassigned') : t('source_pool.participates')
                  return (
                    <div key={item.address} className="flex items-center gap-3 py-2.5">
                      {!manual && (
                        <Checkbox
                          checked={!item.excluded}
                          disabled={!canToggle}
                          onChange={e => toggleAddress(item.address, e.target.checked)}
                        />
                      )}
                      <span className={`w-2 h-2 rounded-full shrink-0 ${struck ? 'bg-dark-600' : addressDotClass(index)}`} />
                      <div className="min-w-0 flex-1">
                        <div className={`font-mono text-sm ${struck ? 'text-dark-500 line-through' : 'text-dark-100'}`}>
                          {item.address}
                        </div>
                        {ownMarks.length > 0 && (
                          <div className="font-mono text-2xs text-dark-500 mt-0.5 break-words">{formatMarks(ownMarks)}</div>
                        )}
                      </div>
                      <span className="text-xs text-dark-500 shrink-0">{label}</span>
                    </div>
                  )
                })}
              </div>
            )}
          </motion.div>

          <MarkLayoutCard
            view={view}
            editable={canConfigure && view.enabled}
            saving={saving}
            onModeChange={changeMode}
            onSaveAssignments={saveAssignments}
          />

          <SourcePoolSnippetBlock />
        </div>
      )}
    </div>
  )
}
