import { useCallback, useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { AnimatePresence, motion } from 'framer-motion'
import { ShieldOff, Loader2, RefreshCw, AlertTriangle, CheckCircle2, ScanLine } from 'lucide-react'
import {
  proxyApi,
  type HosterScanState,
  type HosterFinding,
  type HosterPurgeResultItem,
  type Server,
} from '../../api/client'
import { nodeAllows } from '../../utils/nodeCapabilities'
import { Tooltip } from '../ui/Tooltip'
import { FAQIcon } from '../FAQ'

interface Props {
  serverId: number
  server?: Server | null
}

const SEVERITY_ORDER: Record<string, number> = { danger: 0, warning: 1, info: 2 }

const severityDot = (severity: string) => {
  if (severity === 'danger') return 'bg-danger'
  if (severity === 'warning') return 'bg-warning'
  return 'bg-dark-500'
}

/** Разведка и вырезание средств доступа хостера (агенты, cloud-init, чужие ключи, репозитории). */
export default function HosterAccessCard({ serverId, server }: Props) {
  const { t } = useTranslation()
  const readable = nodeAllows(server, 'system', 'read')
  const writable = nodeAllows(server, 'system', 'write')

  const [state, setState] = useState<HosterScanState | null>(null)
  const [loading, setLoading] = useState(true)
  const [scanning, setScanning] = useState(false)
  const [purging, setPurging] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [results, setResults] = useState<HosterPurgeResultItem[] | null>(null)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [confirmAck, setConfirmAck] = useState(false)

  const applyScan = useCallback((data: HosterScanState) => {
    setState(data)
    setResults(null)
    setSelected(new Set(data.findings.filter(f => f.default_selected).map(f => f.id)))
  }, [])

  const load = useCallback(async () => {
    if (!readable) {
      setLoading(false)
      return
    }
    try {
      const res = await proxyApi.scanHosterAccess(serverId)
      applyScan(res.data)
    } catch {
      setState(null)
    } finally {
      setLoading(false)
    }
  }, [serverId, readable, applyScan])

  useEffect(() => {
    load()
  }, [load])

  const rescan = async () => {
    setScanning(true)
    try {
      const res = await proxyApi.scanHosterAccess(serverId)
      applyScan(res.data)
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
      toast.error(detail || t('server_details.hoster_load_error'))
    } finally {
      setScanning(false)
    }
  }

  const findings = useMemo(
    () =>
      [...(state?.findings ?? [])].sort(
        (a, b) => (SEVERITY_ORDER[a.severity] ?? 3) - (SEVERITY_ORDER[b.severity] ?? 3),
      ),
    [state],
  )

  const toggle = (id: string) => {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const selectedList = findings.filter(f => selected.has(f.id))
  const anyCritical = selectedList.some(f => f.access_critical)

  const runPurge = async () => {
    setConfirmOpen(false)
    setPurging(true)
    try {
      const res = await proxyApi.purgeHosterAccess(serverId, [...selected])
      setResults(res.data.results)
      const ok = res.data.results.filter(r => r.ok).length
      toast.success(t('server_details.hoster_purged', { ok, total: res.data.results.length }))
      if (res.data.reboot_recommended) toast.warning(t('server_details.hoster_reboot'))
      const after = await proxyApi.scanHosterAccess(serverId)
      applyScan(after.data)
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
      toast.error(detail || t('server_details.hoster_purge_error'))
    } finally {
      setPurging(false)
      setConfirmAck(false)
    }
  }

  const header = (
    <h3 className="font-semibold text-dark-100 flex items-center gap-2">
      <ShieldOff className="w-4 h-4 text-accent-500" />
      {t('server_details.hoster_title')}
      <Tooltip label={t('server_details.hoster_hint')} maxWidth={420}>
        <span className="text-dark-500 text-xs font-normal cursor-help">?</span>
      </Tooltip>
      <FAQIcon screen="SERVER_DETAILS_HOSTER" />
    </h3>
  )

  if (!readable) {
    return (
      <div className="card">
        {header}
        <p className="text-sm text-dark-500 mt-2">{t('node_caps.row_blocked')}</p>
      </div>
    )
  }

  if (!loading && state && state.supported === false) {
    return (
      <div className="card">
        {header}
        <p className="text-sm text-dark-500 mt-2">
          {t('server_details.hoster_unsupported', { version: state.min_node_version || '10.29.0' })}
        </p>
      </div>
    )
  }

  return (
    <div className="card">
      <div className="flex items-center justify-between gap-3 mb-1 flex-wrap">
        {header}
        <div className="flex items-center gap-3">
          {state?.hoster_hint && (
            <span className="text-xs text-dark-500">
              {t('server_details.hoster_hoster_label', { name: state.hoster_hint })}
            </span>
          )}
          <button
            onClick={rescan}
            disabled={scanning || purging || loading}
            className="text-xs text-dark-400 hover:text-dark-200 inline-flex items-center gap-1.5 disabled:opacity-50"
          >
            {scanning ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
            {t('server_details.hoster_rescan')}
          </button>
        </div>
      </div>

      {loading ? (
        <div className="flex items-center gap-2 text-sm text-dark-500 py-4">
          <Loader2 className="w-4 h-4 animate-spin" />
          {t('server_details.hoster_scanning')}
        </div>
      ) : findings.length === 0 ? (
        <p className="text-sm text-success inline-flex items-center gap-1.5 py-3">
          <CheckCircle2 className="w-4 h-4" />
          {t('server_details.hoster_clean')}
        </p>
      ) : (
        <>
          <div className="space-y-2 mt-2">
            {findings.map(f => (
              <FindingRow
                key={f.id}
                finding={f}
                checked={selected.has(f.id)}
                result={results?.find(r => r.id === f.id)}
                disabled={!writable || purging}
                onToggle={() => toggle(f.id)}
              />
            ))}
          </div>

          <div className="flex items-center justify-between gap-3 mt-4 flex-wrap">
            <span className="text-xs text-dark-500">
              {t('server_details.hoster_found', { count: findings.length })}
            </span>
            <button
              onClick={() => {
                setConfirmAck(false)
                setConfirmOpen(true)
              }}
              disabled={!writable || purging || selected.size === 0}
              className="px-3 py-1.5 rounded-lg text-xs font-medium bg-danger/90 hover:bg-danger text-white transition-colors disabled:opacity-50 flex items-center gap-1.5"
            >
              {purging ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <ScanLine className="w-3.5 h-3.5" />}
              {t('server_details.hoster_purge_btn', { count: selected.size })}
            </button>
          </div>
        </>
      )}

      <AnimatePresence>
        {confirmOpen && (
          <motion.div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm p-4"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={() => setConfirmOpen(false)}
          >
            <motion.div
              className="bg-dark-800 rounded-2xl p-6 max-w-lg w-full border border-dark-700"
              initial={{ scale: 0.9, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              exit={{ scale: 0.9, opacity: 0 }}
              onClick={e => e.stopPropagation()}
            >
              <div className="flex items-center gap-3 mb-3">
                <div className="p-2 rounded-xl bg-danger/20">
                  <AlertTriangle className="w-6 h-6 text-danger" />
                </div>
                <h3 className="text-lg font-semibold text-dark-100">
                  {t('server_details.hoster_confirm_title')}
                </h3>
              </div>

              <p className="text-sm text-dark-300 mb-3">{t('server_details.hoster_confirm_body')}</p>

              <ul className="text-sm text-dark-200 space-y-1 mb-3 max-h-48 overflow-y-auto pr-1">
                {selectedList.map(f => (
                  <li key={f.id} className="flex items-start gap-2">
                    <span className={`w-1.5 h-1.5 rounded-full mt-1.5 flex-shrink-0 ${severityDot(f.severity)}`} />
                    <span>{f.title}</span>
                  </li>
                ))}
              </ul>

              {anyCritical && (
                <p className="text-sm text-warning bg-warning/10 border border-warning/20 rounded-lg p-3 mb-3">
                  {t('server_details.hoster_confirm_warn_critical')}
                </p>
              )}

              <label className="flex items-start gap-2 text-sm text-dark-300 cursor-pointer mb-4">
                <input
                  type="checkbox"
                  checked={confirmAck}
                  onChange={e => setConfirmAck(e.target.checked)}
                  className="accent-accent-500 mt-0.5"
                />
                {t('server_details.hoster_confirm_ack')}
              </label>

              <div className="flex justify-end gap-2">
                <button onClick={() => setConfirmOpen(false)} className="btn btn-secondary">
                  {t('server_details.hoster_confirm_cancel')}
                </button>
                <button
                  onClick={runPurge}
                  disabled={!confirmAck}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-danger hover:bg-danger/90 text-white transition-colors disabled:opacity-50"
                >
                  {t('server_details.hoster_confirm_go')}
                </button>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

function FindingRow({
  finding,
  checked,
  result,
  disabled,
  onToggle,
}: {
  finding: HosterFinding
  checked: boolean
  result?: HosterPurgeResultItem
  disabled: boolean
  onToggle: () => void
}) {
  const { t } = useTranslation()
  return (
    <label
      className={`flex items-start gap-3 p-3 rounded-xl border cursor-pointer transition-colors ${
        checked ? 'border-accent-500/40 bg-accent-500/5' : 'border-dark-700 bg-dark-800/40'
      } ${disabled ? 'opacity-60 cursor-default' : 'hover:border-dark-600'}`}
    >
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={onToggle}
        className="accent-accent-500 mt-0.5"
      />
      <span className={`w-1.5 h-1.5 rounded-full mt-2 flex-shrink-0 ${severityDot(finding.severity)}`} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm font-medium text-dark-100">{finding.title}</span>
          {finding.access_critical && (
            <span className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-danger/15 text-danger">
              {t('server_details.hoster_badge_critical')}
            </span>
          )}
          {result && (
            <span className={`text-xs ${result.ok ? 'text-success' : 'text-danger'}`}>
              {result.ok ? `✓ ${result.message}` : `✗ ${result.message}`}
            </span>
          )}
        </div>
        <p className="text-xs text-dark-400 mt-0.5 break-words">{finding.detail}</p>
      </div>
    </label>
  )
}
