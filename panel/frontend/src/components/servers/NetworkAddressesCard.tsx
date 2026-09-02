import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { AnimatePresence, motion } from 'framer-motion'
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, Loader2, Lock, Network, Plus, Trash2, XCircle } from 'lucide-react'
import {
  proxyApi,
  type NetworkAddress,
  type NetworkAddressRef,
  type NetworkInterface,
  type NetworkJobSnapshot,
  type NetworkPreview,
  type NetworkState,
  type NetworkTransaction,
  type Server,
} from '../../api/client'
import { nodeAllows } from '../../utils/nodeCapabilities'
import { Tooltip } from '../ui/Tooltip'
import { CopyableIp } from '../ui/CopyableIp'
import { FAQIcon } from '../FAQ'

const POLL_INTERVAL_MS = 3000
const PREVIEW_DEBOUNCE_MS = 400
const DEFAULT_ROLLBACK_SEC = 120

interface Props {
  serverId: number
  server?: Server | null
}

type ApiError = { response?: { status?: number; data?: { detail?: string } } }

function errorDetail(err: unknown): string | undefined {
  const detail = (err as ApiError).response?.data?.detail
  return typeof detail === 'string' ? detail : undefined
}

function errorStatus(err: unknown): number | undefined {
  return (err as ApiError).response?.status
}

function cidr(ref: NetworkAddressRef): string {
  return `${ref.address}/${ref.prefix}`
}

function secondsLeft(deadline: string | null | undefined, now: number): number | null {
  if (!deadline) return null
  const left = Math.round((new Date(deadline).getTime() - now) / 1000)
  return Math.max(0, left)
}

/** Дополнительные IP-адреса интерфейсов ноды: транзакция с таймером отката, подтверждается панелью. */
export default function NetworkAddressesCard({ serverId, server }: Props) {
  const { t } = useTranslation()
  const writable = nodeAllows(server, 'system', 'write')
  const readable = nodeAllows(server, 'system', 'read')

  const [state, setState] = useState<NetworkState | null>(null)
  const [unsupported, setUnsupported] = useState<{ version?: string; message?: string } | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(false)
  const [job, setJob] = useState<NetworkJobSnapshot | null>(null)
  const [showProgress, setShowProgress] = useState(false)
  const [addOpen, setAddOpen] = useState(false)
  const [removeTarget, setRemoveTarget] = useState<{ iface: string; ref: NetworkAddressRef } | null>(null)
  const [busy, setBusy] = useState(false)
  const [now, setNow] = useState(() => Date.now())

  const load = useCallback(async () => {
    if (!readable) {
      setLoading(false)
      return
    }
    try {
      const res = await proxyApi.getNetworkState(serverId)
      if (!res.data.supported) {
        setUnsupported({ version: res.data.min_node_version, message: res.data.message ?? undefined })
      } else {
        setUnsupported(null)
        setState(res.data)
        if (res.data.job) setJob(res.data.job)
      }
      setLoadError(false)
    } catch (err) {
      if (errorStatus(err) === 404) setUnsupported({})
      else setLoadError(true)
    } finally {
      setLoading(false)
    }
  }, [serverId, readable])

  useEffect(() => {
    load()
  }, [load])

  const transaction = state?.transaction ?? null
  const transactionActive = transaction?.status === 'pending' || transaction?.status === 'applying'
  const jobActive = !!job && job.phase !== 'done'
  const inProgress = jobActive || transactionActive

  useEffect(() => {
    if (!inProgress) return
    const timer = setInterval(load, POLL_INTERVAL_MS)
    return () => clearInterval(timer)
  }, [inProgress, load])

  useEffect(() => {
    if (!inProgress) return
    const tick = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(tick)
  }, [inProgress])

  const previousPhase = useRef<string | null>(null)
  useEffect(() => {
    if (!job) return
    if (previousPhase.current && previousPhase.current !== 'done' && job.phase === 'done') {
      if (job.status === 'confirmed') toast.success(t('server_details.network_toast_confirmed'))
      else if (job.status === 'rolled_back') toast.error(t('server_details.network_toast_rolled_back'))
      else toast.error(job.message || t('server_details.network_toast_failed'))
      load()
    }
    previousPhase.current = job.phase
  }, [job, load, t])

  const runApply = async (iface: string, addText: string, remove: NetworkAddressRef[]): Promise<string | null> => {
    setBusy(true)
    try {
      const res = await proxyApi.applyNetworkAddresses(serverId, { interface: iface, add_text: addText, remove })
      previousPhase.current = res.data.phase === 'done' ? null : res.data.phase
      setJob(res.data)
      setShowProgress(true)
      if (res.data.phase === 'done') {
        if (res.data.status === 'confirmed') toast.success(t('server_details.network_toast_confirmed'))
        else if (res.data.status === 'rolled_back') toast.error(t('server_details.network_toast_rolled_back'))
        else toast.error(res.data.message || t('server_details.network_toast_failed'))
      }
      load()
      return null
    } catch (err) {
      const status = errorStatus(err)
      const detail = errorDetail(err)
      if (status === 409) {
        toast.error(detail || t('server_details.network_tx_in_progress'))
        load()
        return null
      }
      if (status === 400) return detail || t('server_details.network_toast_failed')
      toast.error(detail || t('server_details.network_node_unreachable'))
      setShowProgress(true)
      load()
      return null
    } finally {
      setBusy(false)
    }
  }

  const rollback = async () => {
    const id = job?.transaction_id ?? transaction?.id
    if (!id) return
    setBusy(true)
    try {
      await proxyApi.rollbackNetworkTransaction(serverId, id)
      toast.success(t('server_details.network_toast_cancelled'))
    } catch (err) {
      toast.error(errorDetail(err) || t('server_details.network_toast_failed'))
    } finally {
      setBusy(false)
      load()
    }
  }

  const rollbackTimeout = state?.rollback_timeout_sec ?? DEFAULT_ROLLBACK_SEC

  if (!readable || unsupported) {
    return (
      <div className="card">
        <h3 className="font-semibold text-dark-100 mb-2 flex items-center gap-2">
          <Network className="w-4 h-4 text-accent-500" />
          {t('server_details.network_title')}
        </h3>
        <p className="text-sm text-dark-500">
          {!readable
            ? t('node_caps.row_blocked')
            : unsupported?.message || t('server_details.network_unsupported', { version: unsupported?.version || '10.29.0' })}
        </p>
      </div>
    )
  }

  const canAct = writable && !busy && !inProgress

  return (
    <div className="card">
      <div className="flex items-center justify-between gap-3 mb-3 flex-wrap">
        <h3 className="font-semibold text-dark-100 flex items-center gap-2">
          <Network className="w-4 h-4 text-accent-500" />
          {t('server_details.network_title')}
          <FAQIcon screen="SERVER_DETAILS_NETWORK" size="sm" />
        </h3>
        <div className="flex items-center gap-3 text-xs text-dark-500">
          {state?.backend && (
            <Tooltip label={state.backend_detail || state.backend} maxWidth={360}>
              <span className="cursor-help">{t('server_details.network_backend', { backend: state.backend })}</span>
            </Tooltip>
          )}
          <button
            onClick={() => setAddOpen(true)}
            disabled={!canAct || !state}
            className="px-3 py-1.5 rounded-lg text-xs font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors disabled:opacity-50 flex items-center gap-1.5"
          >
            <Plus className="w-3 h-3" />
            {t('server_details.network_add')}
          </button>
        </div>
      </div>

      {(showProgress || inProgress) && (
        <TransactionProgress
          job={job}
          transaction={transaction}
          now={now}
          busy={busy}
          canCancel={writable}
          onCancel={rollback}
          onHide={() => setShowProgress(false)}
        />
      )}

      {loading && <Loader2 className="w-4 h-4 animate-spin text-dark-500" />}

      {loadError && !loading && (
        <div className="text-sm text-warning flex items-center gap-2 mb-3">
          <AlertTriangle className="w-4 h-4" />
          {t('server_details.network_load_error')}
          <button onClick={load} className="underline text-dark-300 hover:text-dark-100">{t('server_details.network_retry')}</button>
        </div>
      )}

      {state && state.interfaces.length === 0 && !loading && (
        <p className="text-sm text-dark-500">{t('server_details.network_empty_interfaces')}</p>
      )}

      {state && state.interfaces.map(iface => (
        <InterfaceBlock
          key={iface.name}
          iface={iface}
          canRemove={canAct}
          onRemove={ref => setRemoveTarget({ iface: iface.name, ref })}
        />
      ))}

      <AnimatePresence>
        {addOpen && state && (
          <AddAddressesModal
            key="add"
            serverId={serverId}
            interfaces={state.interfaces}
            defaultInterface={state.default_interface ?? state.interfaces[0]?.name ?? ''}
            rollbackTimeout={rollbackTimeout}
            busy={busy}
            onClose={() => setAddOpen(false)}
            onApply={async (iface, text) => {
              const error = await runApply(iface, text, [])
              if (!error) setAddOpen(false)
              return error
            }}
          />
        )}
        {removeTarget && (
          <RemoveAddressModal
            key="remove"
            iface={removeTarget.iface}
            ref_={removeTarget.ref}
            rollbackTimeout={rollbackTimeout}
            busy={busy}
            onClose={() => setRemoveTarget(null)}
            onConfirm={async () => {
              const error = await runApply(removeTarget.iface, '', [removeTarget.ref])
              if (error) toast.error(error)
              setRemoveTarget(null)
            }}
          />
        )}
      </AnimatePresence>
    </div>
  )
}

function InterfaceBlock({ iface, canRemove, onRemove }: {
  iface: NetworkInterface
  canRemove: boolean
  onRemove: (ref: NetworkAddressRef) => void
}) {
  const { t } = useTranslation()
  return (
    <div className={`mt-3 ${iface.is_up ? '' : 'opacity-60'}`}>
      <div className="flex items-center gap-2 text-sm text-dark-200 font-medium mb-1">
        <span className="font-mono">{iface.name}</span>
        {iface.is_default && <Badge tone="accent">{t('server_details.network_default_badge')}</Badge>}
        {!iface.is_up && <Badge tone="warning">{t('server_details.network_down_badge')}</Badge>}
      </div>
      {iface.addresses.length === 0 && <p className="text-xs text-dark-500 ml-1">{t('server_details.network_no_addresses')}</p>}
      <div className="space-y-1">
        {iface.addresses.map(addr => (
          <AddressRow key={`${addr.address}/${addr.prefix}`} addr={addr} canRemove={canRemove} onRemove={onRemove} />
        ))}
      </div>
    </div>
  )
}

function AddressRow({ addr, canRemove, onRemove }: {
  addr: NetworkAddress
  canRemove: boolean
  onRemove: (ref: NetworkAddressRef) => void
}) {
  const { t } = useTranslation()
  const locked = !addr.managed
  return (
    <div className="flex items-center gap-2 text-sm px-2 py-1 rounded-lg bg-dark-800/60 flex-wrap">
      <span className="font-mono text-dark-100">
        <CopyableIp value={addr.address} display={`${addr.address}/${addr.prefix}`} />
      </span>
      <Badge tone="muted">{addr.family === 'ipv6' ? 'v6' : 'v4'}</Badge>
      {addr.primary && <Badge tone="accent">{t('server_details.network_primary_badge')}</Badge>}
      {addr.dynamic && <Badge tone="muted">DHCP</Badge>}
      {addr.managed && <Badge tone="success">{t('server_details.network_managed_badge')}</Badge>}
      {locked && (
        <Tooltip label={t('server_details.network_locked_hint')} maxWidth={320}>
          <Lock className="w-3.5 h-3.5 text-dark-500 cursor-help" />
        </Tooltip>
      )}
      {addr.managed && (
        <button
          onClick={() => onRemove({ address: addr.address, prefix: addr.prefix })}
          disabled={!canRemove}
          className="ml-auto text-xs text-dark-400 hover:text-danger disabled:opacity-40 flex items-center gap-1"
        >
          <Trash2 className="w-3.5 h-3.5" />
          {t('server_details.network_remove')}
        </button>
      )}
    </div>
  )
}

function Badge({ tone, children }: { tone: 'accent' | 'warning' | 'success' | 'muted'; children: React.ReactNode }) {
  const tones = {
    accent: 'bg-accent-500/15 text-accent-400',
    warning: 'bg-warning/15 text-warning',
    success: 'bg-success/15 text-success',
    muted: 'bg-dark-700 text-dark-400',
  }
  return <span className={`text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded ${tones[tone]}`}>{children}</span>
}

function TransactionProgress({ job, transaction, now, busy, canCancel, onCancel, onHide }: {
  job: NetworkJobSnapshot | null
  transaction: NetworkTransaction | null
  now: number
  busy: boolean
  canCancel: boolean
  onCancel: () => void
  onHide: () => void
}) {
  const { t } = useTranslation()
  const [logOpen, setLogOpen] = useState(false)

  // Источник истины — снимок задачи панели; без него (после перезагрузки страницы) — транзакция ноды
  const phase = job?.phase ?? (transaction?.status === 'pending' || transaction?.status === 'applying' ? 'confirming' : 'done')
  const status = job?.status ?? transaction?.status ?? 'pending'
  const done = phase === 'done'
  const deadline = job?.deadline_at ?? transaction?.deadline_at ?? null
  const left = secondsLeft(deadline, now)
  const message = job?.message ?? transaction?.message ?? ''
  const warnings = job?.warnings ?? transaction?.warnings ?? []
  const pendingId = job?.transaction_id ?? transaction?.id ?? null

  const stepIcon = (index: number) => {
    const current = phase === 'applying' ? 0 : phase === 'confirming' ? 1 : 2
    if (index < current || (done && status === 'confirmed')) return <CheckCircle2 className="w-4 h-4 text-success" />
    if (index === current && !done) return <Loader2 className="w-4 h-4 animate-spin text-accent-400" />
    if (done && index === 2) return <XCircle className="w-4 h-4 text-danger" />
    return <span className="w-4 h-4 rounded-full border border-dark-600 inline-block" />
  }

  const finalText = status === 'confirmed'
    ? t('server_details.network_status_confirmed')
    : status === 'rolled_back'
      ? t('server_details.network_status_rolled_back')
      : status === 'failed'
        ? t('server_details.network_status_failed')
        : t('server_details.network_status_pending')

  return (
    <div className="mb-4 p-3 rounded-xl border border-dark-700 bg-dark-800/60 text-sm">
      <div className="flex flex-col gap-1.5">
        <div className="flex items-center gap-2 text-dark-200">{stepIcon(0)}{t('server_details.network_step_apply')}</div>
        <div className="flex items-center gap-2 text-dark-200">
          {stepIcon(1)}{t('server_details.network_step_probe')}
          {phase === 'confirming' && left !== null && (
            <span className="text-dark-500 text-xs">{t('server_details.network_deadline_in', { seconds: left })}</span>
          )}
        </div>
        <div className={`flex items-center gap-2 ${done ? (status === 'confirmed' ? 'text-success' : 'text-danger') : 'text-dark-200'}`}>
          {stepIcon(2)}{done ? finalText : t('server_details.network_step_done')}
        </div>
      </div>

      {phase === 'confirming' && job && job.attempts > 0 && (
        <p className="mt-2 text-xs text-warning">{t('server_details.network_attempt', { n: job.attempts, error: job.last_error || '' })}</p>
      )}
      {done && message && <p className="mt-2 text-xs text-dark-400">{message}</p>}
      {warnings.length > 0 && <p className="mt-1 text-xs text-warning">{warnings.join('; ')}</p>}

      {done && status === 'confirmed' && job && job.added.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-2 text-xs">
          {job.added.map(ref => {
            const reach = job.reachability?.[ref.address]
            const tone = reach === true ? 'text-success' : reach === false ? 'text-warning' : 'text-dark-400'
            const label = reach === true
              ? t('server_details.network_reachable')
              : reach === false
                ? t('server_details.network_unreachable')
                : t('server_details.network_reachability_skipped')
            return (
              <span key={cidr(ref)} className={`inline-flex items-center gap-1 ${tone}`}>
                <span className={`w-1.5 h-1.5 rounded-full ${reach === true ? 'bg-success' : reach === false ? 'bg-warning' : 'bg-dark-500'}`} />
                <span className="font-mono text-dark-200">{cidr(ref)}</span> {label}
              </span>
            )
          })}
        </div>
      )}

      {job?.error_log && (
        <div className="mt-2">
          <button onClick={() => setLogOpen(v => !v)} className="text-xs text-dark-400 hover:text-dark-200 flex items-center gap-1">
            {logOpen ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
            {t('server_details.network_error_log')}
          </button>
          {logOpen && <pre className="mt-1 p-2 rounded-lg bg-dark-900 text-[11px] text-dark-300 overflow-x-auto whitespace-pre-wrap">{job.error_log}</pre>}
        </div>
      )}

      <div className="mt-3 flex justify-end gap-2">
        {!done && status === 'pending' && pendingId && canCancel && (
          <button onClick={onCancel} disabled={busy} className="btn btn-secondary text-xs py-1 px-2 disabled:opacity-50">
            {t('server_details.network_cancel_tx')}
          </button>
        )}
        {done && (
          <button onClick={onHide} className="text-xs text-dark-400 hover:text-dark-200">{t('server_details.network_hide')}</button>
        )}
      </div>
    </div>
  )
}

function ModalShell({ title, danger, onClose, children }: {
  title: string
  danger?: boolean
  onClose: () => void
  children: React.ReactNode
}) {
  return (
    <motion.div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      onClick={onClose}
    >
      <motion.div
        className="bg-dark-800 rounded-2xl p-6 max-w-lg w-full mx-4 border border-dark-700"
        initial={{ scale: 0.9, opacity: 0 }}
        animate={{ scale: 1, opacity: 1 }}
        exit={{ scale: 0.9, opacity: 0 }}
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center gap-3 mb-4">
          <div className={`p-2 rounded-xl ${danger ? 'bg-danger/20' : 'bg-accent-500/20'}`}>
            {danger ? <AlertTriangle className="w-5 h-5 text-danger" /> : <Network className="w-5 h-5 text-accent-400" />}
          </div>
          <h3 className="text-lg font-semibold text-dark-100">{title}</h3>
        </div>
        {children}
      </motion.div>
    </motion.div>
  )
}

function RollbackWarning({ seconds }: { seconds: number }) {
  const { t } = useTranslation()
  return (
    <div className="p-3 rounded-lg bg-warning/10 border border-warning/30 text-warning text-xs flex gap-2">
      <AlertTriangle className="w-4 h-4 shrink-0" />
      <span>{t('server_details.network_rollback_warning', { seconds })}</span>
    </div>
  )
}

function AddAddressesModal({ serverId, interfaces, defaultInterface, rollbackTimeout, busy, onClose, onApply }: {
  serverId: number
  interfaces: NetworkInterface[]
  defaultInterface: string
  rollbackTimeout: number
  busy: boolean
  onClose: () => void
  onApply: (iface: string, text: string) => Promise<string | null>
}) {
  const { t } = useTranslation()
  const [iface, setIface] = useState(defaultInterface)
  const [text, setText] = useState('')
  const [preview, setPreview] = useState<NetworkPreview | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)

  useEffect(() => {
    if (!text.trim()) {
      setPreview(null)
      setPreviewError(null)
      return
    }
    setPreviewing(true)
    const timer = setTimeout(async () => {
      try {
        const res = await proxyApi.previewNetworkAddresses(serverId, text)
        setPreview(res.data)
        setPreviewError(null)
      } catch (err) {
        setPreview(null)
        setPreviewError(errorDetail(err) || t('server_details.network_preview_error'))
      } finally {
        setPreviewing(false)
      }
    }, PREVIEW_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [text, serverId, t])

  const present = useMemo(() => {
    const target = interfaces.find(i => i.name === iface)
    return new Set((target?.addresses ?? []).map(a => a.address))
  }, [interfaces, iface])
  const alreadyPresent = preview ? preview.addresses.filter(a => present.has(a.address)).length : 0
  const willAdd = preview ? preview.count - alreadyPresent : 0

  const submit = async () => {
    setSubmitError(null)
    const error = await onApply(iface, text)
    if (error) setSubmitError(error)
  }

  return (
    <ModalShell title={t('server_details.network_modal_add_title')} onClose={() => !busy && onClose()}>
      <label className="block text-xs text-dark-400 mb-1">{t('server_details.network_interface')}</label>
      <select
        value={iface}
        onChange={e => setIface(e.target.value)}
        className="w-full mb-3 px-3 py-2 rounded-lg bg-dark-900 border border-dark-700 text-dark-100 text-sm focus:outline-none focus:border-accent-500/50"
      >
        {interfaces.map(i => (
          <option key={i.name} value={i.name} disabled={!i.is_up}>
            {i.name}{i.is_default ? ` — ${t('server_details.network_default_badge')}` : ''}{i.is_up ? '' : ` (${t('server_details.network_down_badge')})`}
          </option>
        ))}
      </select>

      <label className="block text-xs text-dark-400 mb-1">{t('server_details.network_addresses_label')}</label>
      <textarea
        value={text}
        onChange={e => setText(e.target.value)}
        rows={5}
        placeholder={'203.0.113.10\n203.0.113.20-203.0.113.25\n203.0.113.32/29\n2001:db8::2/64'}
        className="w-full font-mono text-sm px-3 py-2 rounded-lg bg-dark-900 border border-dark-700 text-dark-100 placeholder-dark-600 focus:outline-none focus:border-accent-500/50"
      />
      <p className="text-xs text-dark-500 mt-1 mb-3">{t('server_details.network_format_hint')}</p>

      <div className="min-h-[1.5rem] text-xs mb-3">
        {previewing && <span className="text-dark-500 flex items-center gap-1"><Loader2 className="w-3 h-3 animate-spin" />{t('server_details.network_preview_wait')}</span>}
        {!previewing && previewError && <span className="text-danger">{previewError}</span>}
        {!previewing && preview && (
          <span className="text-dark-300">
            {t('server_details.network_preview_count', { count: willAdd, ipv4: preview.ipv4, ipv6: preview.ipv6 })}
            {alreadyPresent > 0 && <span className="text-dark-500"> · {t('server_details.network_already_present', { count: alreadyPresent })}</span>}
          </span>
        )}
      </div>

      <RollbackWarning seconds={rollbackTimeout} />

      {submitError && (
        <div className="mt-3 p-3 bg-danger/10 border border-danger/30 rounded-lg text-danger text-sm">{submitError}</div>
      )}

      <div className="flex justify-end gap-3 mt-5">
        <button className="btn btn-secondary" onClick={onClose} disabled={busy}>{t('common.cancel')}</button>
        <button
          className="btn bg-accent-600 hover:bg-accent-500 text-white flex items-center gap-2 disabled:opacity-50"
          onClick={submit}
          disabled={busy || previewing || !preview || !!previewError || willAdd === 0}
        >
          {busy && <Loader2 className="w-4 h-4 animate-spin" />}
          {t('server_details.network_apply')}
        </button>
      </div>
    </ModalShell>
  )
}

function RemoveAddressModal({ iface, ref_, rollbackTimeout, busy, onClose, onConfirm }: {
  iface: string
  ref_: NetworkAddressRef
  rollbackTimeout: number
  busy: boolean
  onClose: () => void
  onConfirm: () => Promise<void>
}) {
  const { t } = useTranslation()
  return (
    <ModalShell title={t('server_details.network_modal_remove_title')} danger onClose={() => !busy && onClose()}>
      <p className="text-dark-300 text-sm mb-4">
        {t('server_details.network_remove_confirm', { address: cidr(ref_), iface })}
      </p>
      <RollbackWarning seconds={rollbackTimeout} />
      <div className="flex justify-end gap-3 mt-5">
        <button className="btn btn-secondary" onClick={onClose} disabled={busy}>{t('common.cancel')}</button>
        <button className="btn bg-danger hover:bg-danger/80 text-white flex items-center gap-2 disabled:opacity-50" onClick={onConfirm} disabled={busy}>
          {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Trash2 className="w-4 h-4" />}
          {t('server_details.network_remove')}
        </button>
      </div>
    </ModalShell>
  )
}
