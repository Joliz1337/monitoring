import { useEffect, useState, useCallback, useRef, useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { motion, AnimatePresence } from 'framer-motion'
import {
  CreditCard, Plus, Pencil, Trash2, Clock, ArrowUpCircle,
  Wallet, X, ChevronDown, ChevronRight, Bell, Loader2,
  CalendarClock, DollarSign, Box, FolderPlus, Folder, FolderOpen, MoveRight,
  CalendarX2,
} from 'lucide-react'
import { toast } from 'sonner'
import { billingApi, BillingServerData, BillingSettingsData } from '../api/client'
import { useSettingsStore } from '../stores/settingsStore'

type ModalState =
  | { kind: 'none' }
  | { kind: 'add' }
  | { kind: 'edit'; server: BillingServerData }
  | { kind: 'extend'; server: BillingServerData }
  | { kind: 'topup'; server: BillingServerData }
  | { kind: 'create-folder' }
  | { kind: 'rename-folder'; folderName: string }
  | { kind: 'move-to-folder'; server: BillingServerData }

const COLLAPSED_KEY = 'billing_collapsed_folders'

function loadCollapsed(): Set<string> {
  try {
    const raw = localStorage.getItem(COLLAPSED_KEY)
    return raw ? new Set(JSON.parse(raw)) : new Set()
  } catch { return new Set() }
}

function saveCollapsed(set: Set<string>) {
  localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...set]))
}

function useBillingDateFormat() {
  const tz = useSettingsStore(s => s.getEffectiveTimezone)()

  const formatDate = useCallback((isoDate: string) => {
    try {
      return new Date(isoDate).toLocaleDateString(undefined, {
        timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
      })
    } catch {
      return new Date(isoDate).toLocaleDateString()
    }
  }, [tz])

  const formatDateTime = useCallback((isoDate: string) => {
    try {
      return new Date(isoDate).toLocaleString(undefined, {
        timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit',
      })
    } catch {
      return new Date(isoDate).toLocaleString()
    }
  }, [tz])

  return { formatDate, formatDateTime }
}

export default function Billing() {
  const { t } = useTranslation()
  const { formatDate: formatBillingDate, formatDateTime: formatBillingDateTime } = useBillingDateFormat()

  const [servers, setServers] = useState<BillingServerData[]>([])
  const [settings, setSettings] = useState<BillingSettingsData | null>(null)
  const [loading, setLoading] = useState(true)
  const [modal, setModal] = useState<ModalState>({ kind: 'none' })
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [collapsed, setCollapsed] = useState<Set<string>>(loadCollapsed)
  const [emptyFolders, setEmptyFolders] = useState<string[]>([])

  const folders = useMemo(() => {
    const set = new Set<string>()
    for (const s of servers) {
      if (s.folder) set.add(s.folder)
    }
    for (const f of emptyFolders) set.add(f)
    return [...set].sort((a, b) => a.localeCompare(b))
  }, [servers, emptyFolders])

  const grouped = useMemo(() => {
    const map = new Map<string | null, BillingServerData[]>()
    for (const s of servers) {
      const key = s.folder || null
      if (!map.has(key)) map.set(key, [])
      map.get(key)!.push(s)
    }
    for (const arr of map.values()) arr.sort(sortServers)
    return map
  }, [servers])

  const toggleCollapsed = (folder: string) => {
    setCollapsed(prev => {
      const next = new Set(prev)
      if (next.has(folder)) next.delete(folder)
      else next.add(folder)
      saveCollapsed(next)
      return next
    })
  }

  const fetchAll = useCallback(async () => {
    try {
      const [srvRes, setRes] = await Promise.all([
        billingApi.getServers(),
        billingApi.getSettings(),
      ])
      setServers(srvRes.data.servers)
      setSettings(setRes.data)
    } catch {
      toast.error(t('common.error'))
    } finally {
      setLoading(false)
    }
  }, [t])

  useEffect(() => { fetchAll() }, [fetchAll])

  const handleDelete = async (id: number) => {
    if (!confirm(t('billing.confirm_delete'))) return
    try {
      await billingApi.deleteServer(id)
      setServers(prev => prev.filter(s => s.id !== id))
      toast.success(t('common.deleted'))
    } catch {
      toast.error(t('common.action_failed'))
    }
  }

  const handleDeleteFolder = async (folderName: string) => {
    if (!confirm(t('billing.confirm_delete_folder'))) return
    try {
      await billingApi.deleteFolder(folderName)
      setServers(prev => prev.map(s => s.folder === folderName ? { ...s, folder: null } : s))
      setEmptyFolders(prev => prev.filter(f => f !== folderName))
      toast.success(t('billing.folder_deleted'))
    } catch {
      toast.error(t('common.action_failed'))
    }
  }

  const handleMoveToFolder = async (serverId: number, folder: string | null) => {
    try {
      await billingApi.moveToFolder([serverId], folder)
      setServers(prev => prev.map(s => s.id === serverId ? { ...s, folder } : s))
      toast.success(t('billing.items_moved'))
    } catch {
      toast.error(t('common.action_failed'))
    }
  }

  const handleSaveSettings = async (patch: Partial<BillingSettingsData>) => {
    try {
      const res = await billingApi.updateSettings(patch)
      setSettings(res.data)
      toast.success(t('common.saved'))
    } catch {
      toast.error(t('common.action_failed'))
    }
  }

  if (loading) {
    return (
      <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="space-y-6">
        <div className="space-y-2">
          <div className="h-7 w-48 bg-dark-700/50 rounded-lg animate-pulse" />
          <div className="h-4 w-64 bg-dark-700/30 rounded-lg animate-pulse" />
        </div>
        {Array.from({ length: 3 }).map((_, i) => (
          <div key={i} className="card p-5 space-y-3">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 bg-dark-700/50 rounded-xl animate-pulse" />
              <div className="space-y-2 flex-1">
                <div className="h-4 w-40 bg-dark-700/50 rounded animate-pulse" />
                <div className="h-3 w-56 bg-dark-700/30 rounded animate-pulse" />
              </div>
            </div>
          </div>
        ))}
      </motion.div>
    )
  }

  const renderServerCards = (list: BillingServerData[], indexOffset = 0) => (
    <div className="grid gap-3">
      {list.map((srv, idx) => (
        <ProjectCard
          key={srv.id}
          server={srv}
          index={indexOffset + idx}
          t={t}
          formatDate={formatBillingDate}
          formatDateTime={formatBillingDateTime}
          onExtend={() => setModal({ kind: 'extend', server: srv })}
          onTopup={() => setModal({ kind: 'topup', server: srv })}
          onEdit={() => setModal({ kind: 'edit', server: srv })}
          onDelete={() => handleDelete(srv.id)}
          onMoveToFolder={() => setModal({ kind: 'move-to-folder', server: srv })}
        />
      ))}
    </div>
  )

  const unfolderedServers = grouped.get(null) || []

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-emerald-500/20 to-teal-500/20 flex items-center justify-center">
            <CreditCard className="w-5 h-5 text-emerald-400" />
          </div>
          <div>
            <h1 className="text-xl font-semibold text-white">{t('billing.title')}</h1>
            <p className="text-sm text-dark-400">{t('billing.subtitle')}</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setModal({ kind: 'create-folder' })}
            className="flex items-center gap-2 px-3 py-2 bg-dark-800 hover:bg-dark-700
                       text-dark-300 hover:text-white rounded-xl text-sm font-medium transition border border-dark-700/50"
          >
            <FolderPlus className="w-4 h-4" />
            {t('billing.create_folder')}
          </button>
          <button
            onClick={() => setModal({ kind: 'add' })}
            className="flex items-center gap-2 px-4 py-2 bg-accent-500 hover:bg-accent-600
                       text-white rounded-xl text-sm font-medium transition"
          >
            <Plus className="w-4 h-4" />
            {t('billing.add')}
          </button>
        </div>
      </div>

      {servers.length === 0 ? (
        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          className="bg-dark-900/50 rounded-xl border border-dark-800/50 p-12 text-center"
        >
          <Box className="w-10 h-10 text-dark-600 mx-auto mb-3" />
          <p className="text-dark-400 text-sm">{t('billing.no_items')}</p>
        </motion.div>
      ) : (
        <div className="space-y-4">
          {/* Folder groups */}
          {folders.map(folderName => {
            const isCollapsed = collapsed.has(folderName)
            const folderServers = grouped.get(folderName) || []
            const daysArr = folderServers.map(s => s.days_left ?? 9999)
            const worstDays = daysArr.length > 0 ? Math.min(...daysArr) : 9999

            return (
              <motion.div
                key={folderName}
                initial={{ opacity: 0, y: 12 }}
                animate={{ opacity: 1, y: 0 }}
                className="bg-dark-900/50 rounded-xl border border-dark-800/50 overflow-hidden"
              >
                <div className="flex items-center justify-between px-4 py-3">
                  <button
                    onClick={() => toggleCollapsed(folderName)}
                    className="flex items-center gap-2.5 flex-1 min-w-0 group"
                  >
                    <div className="w-8 h-8 rounded-lg bg-blue-500/15 flex items-center justify-center flex-shrink-0">
                      {isCollapsed
                        ? <Folder className="w-4 h-4 text-blue-400" />
                        : <FolderOpen className="w-4 h-4 text-blue-400" />
                      }
                    </div>
                    <span className="text-sm font-semibold text-white truncate group-hover:text-blue-300 transition">
                      {folderName}
                    </span>
                    <span className="text-xs text-dark-500 flex-shrink-0">
                      {folderServers.length}
                    </span>
                    {isCollapsed && (
                      <span className={`text-xs font-medium flex-shrink-0 ${statusColor(worstDays)}`}>
                        {formatDays(worstDays === 9999 ? null : worstDays, t)}
                      </span>
                    )}
                    {isCollapsed
                      ? <ChevronRight className="w-3.5 h-3.5 text-dark-600 flex-shrink-0" />
                      : <ChevronDown className="w-3.5 h-3.5 text-dark-600 flex-shrink-0" />
                    }
                  </button>
                  <div className="flex items-center gap-1 flex-shrink-0 ml-2">
                    <button
                      onClick={() => setModal({ kind: 'rename-folder', folderName })}
                      className="p-1.5 text-dark-500 hover:text-dark-300 transition rounded-lg hover:bg-dark-800/50"
                    >
                      <Pencil className="w-3.5 h-3.5" />
                    </button>
                    <button
                      onClick={() => handleDeleteFolder(folderName)}
                      className="p-1.5 text-dark-500 hover:text-red-400 transition rounded-lg hover:bg-dark-800/50"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>
                </div>
                <AnimatePresence initial={false}>
                  {!isCollapsed && (
                    <motion.div
                      initial={{ height: 0, opacity: 0 }}
                      animate={{ height: 'auto', opacity: 1 }}
                      exit={{ height: 0, opacity: 0 }}
                      transition={{ duration: 0.2 }}
                      className="overflow-hidden"
                    >
                      <div className="px-3 pb-3">
                        {folderServers.length > 0
                          ? renderServerCards(folderServers)
                          : (
                            <div className="py-6 text-center text-dark-500 text-xs">
                              {t('billing.no_items')}
                            </div>
                          )
                        }
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>
              </motion.div>
            )
          })}

          {/* Servers without folder */}
          {unfolderedServers.length > 0 && (
            <div className="grid gap-4">
              {renderServerCards(unfolderedServers)}
            </div>
          )}
        </div>
      )}

      {/* Notification settings */}
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        className="bg-dark-900/50 rounded-xl border border-dark-800/50 overflow-hidden"
      >
        <button
          onClick={() => setSettingsOpen(v => !v)}
          className="w-full flex items-center justify-between p-5 hover:bg-dark-800/30 transition"
        >
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-amber-500/20 flex items-center justify-center">
              <Bell className="w-4 h-4 text-amber-400" />
            </div>
            <div className="text-left">
              <span className="text-sm font-medium text-dark-200">{t('billing.notification_settings')}</span>
              <p className="text-xs text-dark-500">{t('billing.notification_hint')}</p>
            </div>
          </div>
          <ChevronDown className={`w-4 h-4 text-dark-500 transition-transform ${settingsOpen ? 'rotate-180' : ''}`} />
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
              <div className="px-5 pb-5 space-y-4">
                <ToggleRow
                  label={t('billing.enable_notifications')}
                  checked={settings.enabled}
                  onChange={v => handleSaveSettings({ enabled: v })}
                />
                <div className="space-y-2">
                  <span className="text-sm text-dark-300">{t('billing.notify_before_days')}</span>
                  <div className="flex flex-wrap gap-2">
                    {[1, 3, 7, 14, 30].map(d => {
                      const active = settings.notify_days.includes(d)
                      return (
                        <button
                          key={d}
                          onClick={() => {
                            const next = active
                              ? settings.notify_days.filter(x => x !== d)
                              : [...settings.notify_days, d].sort((a, b) => a - b)
                            handleSaveSettings({ notify_days: next })
                          }}
                          className={`px-3 py-1.5 rounded-lg text-xs font-medium transition ${
                            active
                              ? 'bg-accent-500/20 text-accent-400 border border-accent-500/30'
                              : 'bg-dark-800 text-dark-400 border border-dark-700/50 hover:border-dark-600'
                          }`}
                        >
                          {d} {t('common.days')}
                        </button>
                      )
                    })}
                  </div>
                </div>
                <div className="space-y-1">
                  <span className="text-sm text-dark-300">{t('billing.check_interval')}</span>
                  <div className="flex flex-wrap gap-2">
                    {[30, 60, 120, 360, 720].map(m => {
                      const active = settings.check_interval_minutes === m
                      const label = m < 60 ? `${m}m` : `${m / 60}h`
                      return (
                        <button
                          key={m}
                          onClick={() => handleSaveSettings({ check_interval_minutes: m })}
                          className={`px-3 py-1.5 rounded-lg text-xs font-medium transition ${
                            active
                              ? 'bg-accent-500/20 text-accent-400 border border-accent-500/30'
                              : 'bg-dark-800 text-dark-400 border border-dark-700/50 hover:border-dark-600'
                          }`}
                        >
                          {label}
                        </button>
                      )
                    })}
                  </div>
                </div>
                <p className="text-xs text-dark-500">{t('billing.telegram_from_alerts')}</p>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </motion.div>

      {/* Modals */}
      {modal.kind === 'add' && (
        <AddModal
          t={t}
          folders={folders}
          onClose={() => setModal({ kind: 'none' })}
          onCreated={srv => {
            setServers(prev => [...prev, srv].sort(sortServers))
            setModal({ kind: 'none' })
          }}
        />
      )}
      {modal.kind === 'edit' && (
        <EditModal
          t={t}
          server={modal.server}
          folders={folders}
          onClose={() => setModal({ kind: 'none' })}
          onSaved={srv => {
            setServers(prev => prev.map(s => s.id === srv.id ? srv : s).sort(sortServers))
            setModal({ kind: 'none' })
          }}
        />
      )}
      {modal.kind === 'extend' && (
        <ExtendModal
          t={t}
          server={modal.server}
          onClose={() => setModal({ kind: 'none' })}
          onDone={srv => {
            setServers(prev => prev.map(s => s.id === srv.id ? srv : s).sort(sortServers))
            setModal({ kind: 'none' })
          }}
        />
      )}
      {modal.kind === 'topup' && (
        <TopupModal
          t={t}
          server={modal.server}
          onClose={() => setModal({ kind: 'none' })}
          onDone={srv => {
            setServers(prev => prev.map(s => s.id === srv.id ? srv : s).sort(sortServers))
            setModal({ kind: 'none' })
          }}
        />
      )}
      {modal.kind === 'create-folder' && (
        <CreateFolderModal
          t={t}
          existingFolders={folders}
          onClose={() => setModal({ kind: 'none' })}
          onCreated={(name) => {
            setEmptyFolders(prev => prev.includes(name) ? prev : [...prev, name])
            setModal({ kind: 'none' })
          }}
        />
      )}
      {modal.kind === 'rename-folder' && (
        <RenameFolderModal
          t={t}
          folderName={modal.folderName}
          onClose={() => setModal({ kind: 'none' })}
          onRenamed={(oldName, newName) => {
            setServers(prev => prev.map(s => s.folder === oldName ? { ...s, folder: newName } : s))
            setEmptyFolders(prev => prev.map(f => f === oldName ? newName : f))
            setModal({ kind: 'none' })
          }}
        />
      )}
      {modal.kind === 'move-to-folder' && (
        <MoveToFolderModal
          t={t}
          server={modal.server}
          folders={folders}
          onClose={() => setModal({ kind: 'none' })}
          onMoved={(serverId, folder) => {
            handleMoveToFolder(serverId, folder)
            setModal({ kind: 'none' })
          }}
        />
      )}
    </div>
  )
}

function currencySymbol(currency: string): string {
  switch (currency) {
    case 'RUB': return '₽'
    case 'USD': return '$'
    case 'EUR': return '€'
    default: return currency
  }
}

function sortServers(a: BillingServerData, b: BillingServerData) {
  const da = a.days_left ?? 9999
  const db = b.days_left ?? 9999
  return da - db
}

function statusColor(daysLeft: number | null): string {
  if (daysLeft === null) return 'text-dark-500'
  if (daysLeft <= 0) return 'text-red-400'
  if (daysLeft <= 3) return 'text-red-400'
  if (daysLeft <= 7) return 'text-yellow-400'
  return 'text-emerald-400'
}

function barColor(daysLeft: number | null): string {
  if (daysLeft === null) return 'bg-dark-600'
  if (daysLeft <= 0) return 'bg-red-500'
  if (daysLeft <= 3) return 'bg-red-500'
  if (daysLeft <= 7) return 'bg-yellow-500'
  return 'bg-emerald-500'
}

function formatDays(days: number | null, t: (k: string) => string): string {
  if (days === null) return '—'
  if (days <= 0) return t('billing.expired')
  if (days < 1) return `${Math.round(days * 24)}h`
  return `${Math.round(days)}d`
}

/* ------------------------------------------------------------------ */
/*  Project Card                                                       */
/* ------------------------------------------------------------------ */

function ProjectCard({ server, index, t, formatDate, formatDateTime, onExtend, onTopup, onEdit, onDelete, onMoveToFolder }: {
  server: BillingServerData
  index: number
  t: (k: string, opts?: Record<string, unknown>) => string
  formatDate: (iso: string) => string
  formatDateTime: (iso: string) => string
  onExtend: () => void
  onTopup: () => void
  onEdit: () => void
  onDelete: () => void
  onMoveToFolder: () => void
}) {
  const dl = server.days_left
  const maxDays = 30
  const pct = dl !== null ? Math.min(100, Math.max(0, (dl / maxDays) * 100)) : 0
  const dailyCost = server.monthly_cost ? server.monthly_cost / 30 : null

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.04 }}
      className="bg-dark-900/50 rounded-xl border border-dark-800/50 p-5"
    >
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-center gap-3 min-w-0">
          <div className={`w-10 h-10 rounded-xl flex items-center justify-center flex-shrink-0 ${
            server.billing_type === 'monthly'
              ? 'bg-blue-500/20'
              : 'bg-purple-500/20'
          }`}>
            {server.billing_type === 'monthly'
              ? <CalendarClock className="w-5 h-5 text-blue-400" />
              : <Wallet className="w-5 h-5 text-purple-400" />
            }
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-semibold text-white truncate">{server.name}</h3>
              <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-medium uppercase ${
                server.billing_type === 'monthly'
                  ? 'bg-blue-500/15 text-blue-400'
                  : 'bg-purple-500/15 text-purple-400'
              }`}>
                {t(`billing.type_${server.billing_type}`)}
              </span>
            </div>
            <div className="flex items-center gap-3 mt-1 text-xs text-dark-400 flex-wrap">
              {server.billing_type === 'resource' && server.account_balance !== null && (
                <span className="flex items-center gap-1">
                  <DollarSign className="w-3 h-3" />
                  {server.account_balance.toFixed(2)} {currencySymbol(server.currency)}
                </span>
              )}
              {dailyCost !== null && dailyCost > 0 && (
                <span className="flex items-center gap-1 text-dark-500">
                  {dailyCost.toFixed(2)} {currencySymbol(server.currency)}{t('billing.per_day')}
                </span>
              )}
              {server.notes && (
                <span className="truncate max-w-[200px]">{server.notes}</span>
              )}
            </div>
          </div>
        </div>

        <div className="flex items-center gap-2 flex-shrink-0">
          <span className={`text-lg font-bold tabular-nums ${statusColor(dl)}`}>
            {formatDays(dl, t)}
          </span>
        </div>
      </div>

      {/* Expiration date */}
      {server.paid_until && (
        <div className={`mt-2.5 flex items-center gap-1.5 text-xs ${
          dl !== null && dl <= 3 ? 'text-red-400/80' : dl !== null && dl <= 7 ? 'text-yellow-400/80' : 'text-dark-400'
        }`}>
          <CalendarX2 className="w-3.5 h-3.5" />
          <span>{t('billing.expires_at')}:</span>
          <span className="font-medium">{formatDateTime(server.paid_until)}</span>
        </div>
      )}

      {/* Progress bar */}
      <div className="mt-2.5 h-1.5 bg-dark-800 rounded-full overflow-hidden">
        <motion.div
          className={`h-full rounded-full ${barColor(dl)}`}
          initial={{ width: 0 }}
          animate={{ width: `${pct}%` }}
          transition={{ duration: 0.5, ease: 'easeOut' }}
        />
      </div>

      {/* Actions */}
      <div className="flex items-center justify-between mt-3">
        <div className="flex gap-2">
          {server.billing_type === 'monthly' ? (
            <button
              onClick={onExtend}
              className="flex items-center gap-1.5 px-4 py-2 text-xs font-semibold
                         bg-gradient-to-r from-emerald-500/20 to-teal-500/20 text-emerald-400
                         hover:from-emerald-500/30 hover:to-teal-500/30
                         border border-emerald-500/20 hover:border-emerald-500/40
                         rounded-xl transition-all shadow-sm shadow-emerald-500/5"
            >
              <ArrowUpCircle className="w-4 h-4" />
              {t('billing.extend')}
            </button>
          ) : (
            <button
              onClick={onTopup}
              className="flex items-center gap-1.5 px-4 py-2 text-xs font-semibold
                         bg-gradient-to-r from-purple-500/20 to-violet-500/20 text-purple-400
                         hover:from-purple-500/30 hover:to-violet-500/30
                         border border-purple-500/20 hover:border-purple-500/40
                         rounded-xl transition-all shadow-sm shadow-purple-500/5"
            >
              <Wallet className="w-4 h-4" />
              {t('billing.topup')}
            </button>
          )}
        </div>
        <div className="flex gap-1">
          <button
            onClick={onMoveToFolder}
            className="p-1.5 text-dark-500 hover:text-blue-400 transition rounded-lg hover:bg-dark-800/50"
            title={t('billing.move_to_folder')}
          >
            <MoveRight className="w-3.5 h-3.5" />
          </button>
          <button
            onClick={onEdit}
            className="p-1.5 text-dark-500 hover:text-dark-300 transition rounded-lg hover:bg-dark-800/50"
          >
            <Pencil className="w-3.5 h-3.5" />
          </button>
          <button
            onClick={onDelete}
            className="p-1.5 text-dark-500 hover:text-red-400 transition rounded-lg hover:bg-dark-800/50"
          >
            <Trash2 className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>
    </motion.div>
  )
}

/* ------------------------------------------------------------------ */
/*  Modals                                                             */
/* ------------------------------------------------------------------ */

function Overlay({ children, onClose }: { children: React.ReactNode; onClose: () => void }) {
  const mouseDownTarget = useRef<EventTarget | null>(null)

  return (
    <AnimatePresence>
      <motion.div
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        onMouseDown={e => { mouseDownTarget.current = e.target }}
        onClick={e => {
          if (e.target === e.currentTarget && mouseDownTarget.current === e.currentTarget) onClose()
        }}
      >
        <motion.div
          initial={{ opacity: 0, scale: 0.95, y: 20 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.95, y: 20 }}
          transition={{ duration: 0.2 }}
          className="bg-dark-900 border border-dark-800 rounded-2xl shadow-2xl w-full max-w-md"
          onClick={e => e.stopPropagation()}
        >
          {children}
        </motion.div>
      </motion.div>
    </AnimatePresence>
  )
}

function AddModal({ t, folders, onClose, onCreated }: {
  t: (k: string) => string
  folders: string[]
  onClose: () => void
  onCreated: (s: BillingServerData) => void
}) {
  const [name, setName] = useState('')
  const [billingType, setBillingType] = useState<'monthly' | 'resource'>('resource')
  const [paidDays, setPaidDays] = useState(30)
  const [dailyCost, setDailyCost] = useState('')
  const [balance, setBalance] = useState('')
  const [currency, setCurrency] = useState('RUB')
  const [notes, setNotes] = useState('')
  const [folder, setFolder] = useState('')
  const [saving, setSaving] = useState(false)

  const submit = async () => {
    if (!name.trim()) return
    setSaving(true)
    try {
      const dailyNum = parseFloat(dailyCost) || 0
      const res = await billingApi.createServer({
        name: name.trim(),
        billing_type: billingType,
        paid_days: billingType === 'monthly' ? paidDays : undefined,
        monthly_cost: billingType === 'resource' ? dailyNum * 30 : undefined,
        account_balance: billingType === 'resource' ? parseFloat(balance) || 0 : undefined,
        currency,
        notes: notes.trim() || undefined,
        folder: folder || undefined,
      })
      onCreated(res.data.server)
      toast.success(t('common.added'))
    } catch {
      toast.error(t('common.action_failed'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Overlay onClose={onClose}>
      <div className="p-6">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-semibold text-white">{t('billing.add')}</h2>
          <button onClick={onClose} className="text-dark-500 hover:text-dark-300 transition">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="space-y-4">
          <Field label={t('billing.billing_type')}>
            <div className="flex gap-2">
              {(['monthly', 'resource'] as const).map(bt => (
                <button
                  key={bt}
                  onClick={() => setBillingType(bt)}
                  className={`flex-1 py-2.5 rounded-lg text-sm font-medium transition ${
                    billingType === bt
                      ? 'bg-accent-500/20 text-accent-400 border border-accent-500/30'
                      : 'bg-dark-800 text-dark-400 border border-dark-700/50'
                  }`}
                >
                  <div>{t(`billing.type_${bt}`)}</div>
                  <div className={`text-[10px] mt-0.5 ${billingType === bt ? 'text-accent-400/60' : 'text-dark-500'}`}>
                    {t(`billing.type_${bt}_hint`)}
                  </div>
                </button>
              ))}
            </div>
          </Field>

          <Field label={t('common.name')}>
            <input
              value={name}
              onChange={e => setName(e.target.value)}
              placeholder={t('billing.name_placeholder')}
              className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-sm text-dark-200
                         placeholder-dark-600 focus:border-accent-500/50 focus:outline-none transition"
              autoFocus
            />
          </Field>

          {billingType === 'monthly' ? (
            <Field label={t('billing.paid_days')}>
              <input
                type="number"
                value={paidDays}
                onChange={e => setPaidDays(parseInt(e.target.value) || 0)}
                min={1}
                className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-sm text-dark-200
                         placeholder-dark-600 focus:border-accent-500/50 focus:outline-none transition"
              />
            </Field>
          ) : (
            <>
              <Field label={t('billing.daily_cost')}>
                <input
                  type="number"
                  step="0.01"
                  value={dailyCost}
                  onChange={e => setDailyCost(e.target.value)}
                  placeholder="0.00"
                  className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-sm text-dark-200
                         placeholder-dark-600 focus:border-accent-500/50 focus:outline-none transition"
                />
              </Field>
              <Field label={t('billing.account_balance')}>
                <input
                  type="number"
                  step="0.01"
                  value={balance}
                  onChange={e => setBalance(e.target.value)}
                  placeholder="0.00"
                  className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-sm text-dark-200
                         placeholder-dark-600 focus:border-accent-500/50 focus:outline-none transition"
                />
              </Field>
            </>
          )}

          <Field label={t('billing.currency')}>
            <div className="flex gap-2">
              {(['RUB', 'USD', 'EUR'] as const).map(c => (
                <button
                  key={c}
                  onClick={() => setCurrency(c)}
                  className={`px-3 py-1.5 rounded-lg text-xs font-medium transition ${
                    currency === c
                      ? 'bg-accent-500/20 text-accent-400 border border-accent-500/30'
                      : 'bg-dark-800 text-dark-400 border border-dark-700/50'
                  }`}
                >
                  {currencySymbol(c)} {c}
                </button>
              ))}
            </div>
          </Field>

          <Field label={t('billing.notes') + ` (${t('common.optional')})`}>
            <input
              value={notes}
              onChange={e => setNotes(e.target.value)}
              placeholder={t('billing.notes_placeholder')}
              className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-sm text-dark-200
                         placeholder-dark-600 focus:border-accent-500/50 focus:outline-none transition"
            />
          </Field>

          {folders.length > 0 && (
            <Field label={t('billing.folder') + ` (${t('common.optional')})`}>
              <select
                value={folder}
                onChange={e => setFolder(e.target.value)}
                className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-sm text-dark-200
                           focus:border-accent-500/50 focus:outline-none transition"
              >
                <option value="">{t('billing.no_folder')}</option>
                {folders.map(f => <option key={f} value={f}>{f}</option>)}
              </select>
            </Field>
          )}
        </div>

        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 py-2.5 bg-dark-800 text-dark-300 rounded-xl text-sm font-medium hover:bg-dark-700 transition">
            {t('common.cancel')}
          </button>
          <button
            onClick={submit}
            disabled={!name.trim() || saving}
            className="flex-1 py-2.5 bg-accent-500 text-white rounded-xl text-sm font-medium hover:bg-accent-600 transition
                       disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2"
          >
            {saving && <Loader2 className="w-4 h-4 animate-spin" />}
            {t('common.add')}
          </button>
        </div>
      </div>
    </Overlay>
  )
}

function EditModal({ t, server, folders, onClose, onSaved }: {
  t: (k: string) => string
  server: BillingServerData
  folders: string[]
  onClose: () => void
  onSaved: (s: BillingServerData) => void
}) {
  const [name, setName] = useState(server.name)
  const currentDaily = server.monthly_cost ? (server.monthly_cost / 30) : 0
  const [dailyCost, setDailyCost] = useState(currentDaily ? currentDaily.toFixed(2) : '')
  const [balance, setBalance] = useState(server.account_balance?.toString() || '')
  const [currency, setCurrency] = useState(server.currency)
  const [notes, setNotes] = useState(server.notes || '')
  const [folder, setFolder] = useState(server.folder || '')
  const [paidUntil, setPaidUntil] = useState(
    server.paid_until ? server.paid_until.slice(0, 16) : ''
  )
  const [saving, setSaving] = useState(false)

  const submit = async () => {
    setSaving(true)
    try {
      const payload: Record<string, unknown> = {
        name, currency, notes: notes || null,
        folder: folder || null,
      }
      if (server.billing_type === 'monthly' && paidUntil) {
        payload.paid_until = new Date(paidUntil).toISOString()
      }
      if (server.billing_type === 'resource') {
        const dailyNum = parseFloat(dailyCost) || 0
        payload.monthly_cost = dailyNum * 30
        payload.account_balance = parseFloat(balance) || 0
      }
      const res = await billingApi.updateServer(server.id, payload as never)
      onSaved(res.data)
      toast.success(t('common.saved'))
    } catch {
      toast.error(t('common.action_failed'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Overlay onClose={onClose}>
      <div className="p-6">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-semibold text-white">{t('common.edit')}</h2>
          <button onClick={onClose} className="text-dark-500 hover:text-dark-300 transition">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="space-y-4">
          <Field label={t('common.name')}>
            <input value={name} onChange={e => setName(e.target.value)} className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-sm text-dark-200
                         placeholder-dark-600 focus:border-accent-500/50 focus:outline-none transition" />
          </Field>

          {server.billing_type === 'monthly' && (
            <Field label={t('billing.paid_until')}>
              <input
                type="datetime-local"
                value={paidUntil}
                onChange={e => setPaidUntil(e.target.value)}
                className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-sm text-dark-200
                         placeholder-dark-600 focus:border-accent-500/50 focus:outline-none transition"
              />
            </Field>
          )}

          {server.billing_type === 'resource' && (
            <>
              <Field label={t('billing.daily_cost')}>
                <input
                  type="number"
                  step="0.01"
                  value={dailyCost}
                  onChange={e => setDailyCost(e.target.value)}
                  className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-sm text-dark-200
                         placeholder-dark-600 focus:border-accent-500/50 focus:outline-none transition"
                />
              </Field>
              <Field label={t('billing.account_balance')}>
                <input
                  type="number"
                  step="0.01"
                  value={balance}
                  onChange={e => setBalance(e.target.value)}
                  className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-sm text-dark-200
                         placeholder-dark-600 focus:border-accent-500/50 focus:outline-none transition"
                />
              </Field>
            </>
          )}

          <Field label={t('billing.currency')}>
            <div className="flex gap-2">
              {(['RUB', 'USD', 'EUR'] as const).map(c => (
                <button
                  key={c}
                  onClick={() => setCurrency(c)}
                  className={`px-3 py-1.5 rounded-lg text-xs font-medium transition ${
                    currency === c
                      ? 'bg-accent-500/20 text-accent-400 border border-accent-500/30'
                      : 'bg-dark-800 text-dark-400 border border-dark-700/50'
                  }`}
                >
                  {currencySymbol(c)} {c}
                </button>
              ))}
            </div>
          </Field>

          <Field label={t('billing.notes') + ` (${t('common.optional')})`}>
            <input value={notes} onChange={e => setNotes(e.target.value)} className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-sm text-dark-200
                         placeholder-dark-600 focus:border-accent-500/50 focus:outline-none transition" />
          </Field>

          <Field label={t('billing.folder') + ` (${t('common.optional')})`}>
            <select
              value={folder}
              onChange={e => setFolder(e.target.value)}
              className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-sm text-dark-200
                         focus:border-accent-500/50 focus:outline-none transition"
            >
              <option value="">{t('billing.no_folder')}</option>
              {folders.map(f => <option key={f} value={f}>{f}</option>)}
            </select>
          </Field>
        </div>

        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 py-2.5 bg-dark-800 text-dark-300 rounded-xl text-sm font-medium hover:bg-dark-700 transition">
            {t('common.cancel')}
          </button>
          <button
            onClick={submit}
            disabled={saving}
            className="flex-1 py-2.5 bg-accent-500 text-white rounded-xl text-sm font-medium hover:bg-accent-600 transition
                       disabled:opacity-40 flex items-center justify-center gap-2"
          >
            {saving && <Loader2 className="w-4 h-4 animate-spin" />}
            {t('common.save')}
          </button>
        </div>
      </div>
    </Overlay>
  )
}

function ExtendModal({ t, server, onClose, onDone }: {
  t: (k: string) => string
  server: BillingServerData
  onClose: () => void
  onDone: (s: BillingServerData) => void
}) {
  const [days, setDays] = useState(30)
  const [saving, setSaving] = useState(false)

  const submit = async () => {
    if (days <= 0) return
    setSaving(true)
    try {
      const res = await billingApi.extendServer(server.id, days)
      onDone(res.data)
      toast.success(t('billing.extended'))
    } catch {
      toast.error(t('common.action_failed'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Overlay onClose={onClose}>
      <div className="p-6">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-semibold text-white">{t('billing.extend')} — {server.name}</h2>
          <button onClick={onClose} className="text-dark-500 hover:text-dark-300 transition">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="space-y-4">
          <Field label={t('billing.extend_days')}>
            <input
              type="number"
              value={days}
              onChange={e => setDays(parseInt(e.target.value) || 0)}
              min={1}
              className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-sm text-dark-200
                         placeholder-dark-600 focus:border-accent-500/50 focus:outline-none transition"
              autoFocus
            />
          </Field>
          <div className="flex flex-wrap gap-2">
            {[7, 14, 30, 60, 90].map(d => (
              <button
                key={d}
                onClick={() => setDays(d)}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium transition ${
                  days === d
                    ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
                    : 'bg-dark-800 text-dark-400 border border-dark-700/50'
                }`}
              >
                +{d}d
              </button>
            ))}
          </div>
        </div>

        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 py-2.5 bg-dark-800 text-dark-300 rounded-xl text-sm font-medium hover:bg-dark-700 transition">
            {t('common.cancel')}
          </button>
          <button
            onClick={submit}
            disabled={days <= 0 || saving}
            className="flex-1 py-2.5 bg-emerald-500 text-white rounded-xl text-sm font-medium hover:bg-emerald-600 transition
                       disabled:opacity-40 flex items-center justify-center gap-2"
          >
            {saving && <Loader2 className="w-4 h-4 animate-spin" />}
            {t('billing.extend')}
          </button>
        </div>
      </div>
    </Overlay>
  )
}

function TopupModal({ t, server, onClose, onDone }: {
  t: (k: string) => string
  server: BillingServerData
  onClose: () => void
  onDone: (s: BillingServerData) => void
}) {
  const [amount, setAmount] = useState('')
  const [saving, setSaving] = useState(false)

  const numAmount = parseFloat(amount) || 0

  const submit = async () => {
    if (numAmount <= 0) return
    setSaving(true)
    try {
      const res = await billingApi.topupServer(server.id, numAmount)
      onDone(res.data)
      toast.success(t('billing.topped_up'))
    } catch {
      toast.error(t('common.action_failed'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Overlay onClose={onClose}>
      <div className="p-6">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-semibold text-white">{t('billing.topup')} — {server.name}</h2>
          <button onClick={onClose} className="text-dark-500 hover:text-dark-300 transition">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="space-y-4">
          <div className="bg-dark-800/50 rounded-xl p-3 border border-dark-700/50">
            <div className="flex items-center justify-between">
              <span className="text-xs text-dark-500">{t('billing.current_balance')}</span>
              <span className="text-lg font-bold text-dark-200">
                {(server.account_balance ?? 0).toFixed(2)} {currencySymbol(server.currency)}
              </span>
            </div>
            {server.monthly_cost && server.monthly_cost > 0 && (
              <div className="flex items-center justify-between mt-1">
                <span className="text-xs text-dark-500">{t('billing.daily_cost')}</span>
                <span className="text-xs text-dark-400">{(server.monthly_cost / 30).toFixed(2)} {currencySymbol(server.currency)}{t('billing.per_day')}</span>
              </div>
            )}
          </div>
          <Field label={t('billing.topup_amount') + ` (${currencySymbol(server.currency)})`}>
            <input
              type="number"
              step="0.01"
              value={amount}
              onChange={e => setAmount(e.target.value)}
              placeholder="0.00"
              className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-sm text-dark-200
                         placeholder-dark-600 focus:border-accent-500/50 focus:outline-none transition"
              autoFocus
            />
          </Field>
          <div className="flex flex-wrap gap-2">
            {[100, 500, 1000, 2000, 5000].map(v => (
              <button
                key={v}
                onClick={() => setAmount(v.toString())}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium transition ${
                  amount === v.toString()
                    ? 'bg-purple-500/20 text-purple-400 border border-purple-500/30'
                    : 'bg-dark-800 text-dark-400 border border-dark-700/50 hover:border-dark-600'
                }`}
              >
                +{v} {currencySymbol(server.currency)}
              </button>
            ))}
          </div>
          {numAmount > 0 && server.monthly_cost && server.monthly_cost > 0 && (
            <div className="text-xs text-emerald-400/80 flex items-center gap-1 bg-emerald-500/10 rounded-lg px-3 py-2">
              <Clock className="w-3 h-3" />
              ≈ +{Math.round((numAmount / server.monthly_cost) * 30)} {t('common.days')}
            </div>
          )}
        </div>

        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 py-2.5 bg-dark-800 text-dark-300 rounded-xl text-sm font-medium hover:bg-dark-700 transition">
            {t('common.cancel')}
          </button>
          <button
            onClick={submit}
            disabled={numAmount <= 0 || saving}
            className="flex-1 py-2.5 bg-purple-500 text-white rounded-xl text-sm font-medium hover:bg-purple-600 transition
                       disabled:opacity-40 flex items-center justify-center gap-2"
          >
            {saving && <Loader2 className="w-4 h-4 animate-spin" />}
            {t('billing.topup')}
          </button>
        </div>
      </div>
    </Overlay>
  )
}

/* ------------------------------------------------------------------ */
/*  Folder Modals                                                      */
/* ------------------------------------------------------------------ */

function CreateFolderModal({ t, existingFolders, onClose, onCreated }: {
  t: (k: string) => string
  existingFolders: string[]
  onClose: () => void
  onCreated: (name: string) => void
}) {
  const [name, setName] = useState('')
  const trimmed = name.trim()
  const duplicate = existingFolders.includes(trimmed)

  const handleCreate = () => {
    if (!trimmed || duplicate) return
    onCreated(trimmed)
    toast.success(t('billing.folder_created'))
  }

  return (
    <Overlay onClose={onClose}>
      <div className="p-6">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-semibold text-white">{t('billing.create_folder')}</h2>
          <button onClick={onClose} className="text-dark-500 hover:text-dark-300 transition">
            <X className="w-5 h-5" />
          </button>
        </div>
        <Field label={t('billing.folder_name')}>
          <input
            value={name}
            onChange={e => setName(e.target.value)}
            placeholder={t('billing.folder_name_placeholder')}
            className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-sm text-dark-200
                       placeholder-dark-600 focus:border-accent-500/50 focus:outline-none transition"
            autoFocus
            onKeyDown={e => { if (e.key === 'Enter') handleCreate() }}
          />
        </Field>
        {duplicate && (
          <p className="text-xs text-red-400 mt-2">{trimmed} — already exists</p>
        )}
        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 py-2.5 bg-dark-800 text-dark-300 rounded-xl text-sm font-medium hover:bg-dark-700 transition">
            {t('common.cancel')}
          </button>
          <button
            onClick={handleCreate}
            disabled={!trimmed || duplicate}
            className="flex-1 py-2.5 bg-accent-500 text-white rounded-xl text-sm font-medium hover:bg-accent-600 transition
                       disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {t('common.create')}
          </button>
        </div>
      </div>
    </Overlay>
  )
}

function RenameFolderModal({ t, folderName, onClose, onRenamed }: {
  t: (k: string) => string
  folderName: string
  onClose: () => void
  onRenamed: (oldName: string, newName: string) => void
}) {
  const [name, setName] = useState(folderName)
  const [saving, setSaving] = useState(false)

  const submit = async () => {
    const trimmed = name.trim()
    if (!trimmed || trimmed === folderName) return
    setSaving(true)
    try {
      await billingApi.renameFolder(folderName, trimmed)
      onRenamed(folderName, trimmed)
      toast.success(t('billing.folder_renamed'))
    } catch {
      toast.error(t('common.action_failed'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Overlay onClose={onClose}>
      <div className="p-6">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-semibold text-white">{t('billing.rename_folder')}</h2>
          <button onClick={onClose} className="text-dark-500 hover:text-dark-300 transition">
            <X className="w-5 h-5" />
          </button>
        </div>
        <Field label={t('billing.folder_name')}>
          <input
            value={name}
            onChange={e => setName(e.target.value)}
            className="w-full bg-dark-800 border border-dark-700 rounded-lg px-3 py-2 text-sm text-dark-200
                       placeholder-dark-600 focus:border-accent-500/50 focus:outline-none transition"
            autoFocus
            onKeyDown={e => { if (e.key === 'Enter') submit() }}
          />
        </Field>
        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 py-2.5 bg-dark-800 text-dark-300 rounded-xl text-sm font-medium hover:bg-dark-700 transition">
            {t('common.cancel')}
          </button>
          <button
            onClick={submit}
            disabled={!name.trim() || name.trim() === folderName || saving}
            className="flex-1 py-2.5 bg-accent-500 text-white rounded-xl text-sm font-medium hover:bg-accent-600 transition
                       disabled:opacity-40 flex items-center justify-center gap-2"
          >
            {saving && <Loader2 className="w-4 h-4 animate-spin" />}
            {t('common.save')}
          </button>
        </div>
      </div>
    </Overlay>
  )
}

function MoveToFolderModal({ t, server, folders, onClose, onMoved }: {
  t: (k: string) => string
  server: BillingServerData
  folders: string[]
  onClose: () => void
  onMoved: (serverId: number, folder: string | null) => void
}) {
  const [selected, setSelected] = useState(server.folder || '')

  return (
    <Overlay onClose={onClose}>
      <div className="p-6">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-semibold text-white">{t('billing.move_to_folder')}</h2>
          <button onClick={onClose} className="text-dark-500 hover:text-dark-300 transition">
            <X className="w-5 h-5" />
          </button>
        </div>
        <p className="text-sm text-dark-400 mb-4">{server.name}</p>
        <div className="space-y-1.5">
          <button
            onClick={() => setSelected('')}
            className={`w-full flex items-center gap-2.5 px-3 py-2.5 rounded-lg text-sm transition ${
              selected === ''
                ? 'bg-accent-500/15 text-accent-400 border border-accent-500/30'
                : 'bg-dark-800/50 text-dark-300 border border-dark-700/50 hover:border-dark-600'
            }`}
          >
            <Box className="w-4 h-4" />
            {t('billing.no_folder')}
          </button>
          {folders.map(f => (
            <button
              key={f}
              onClick={() => setSelected(f)}
              className={`w-full flex items-center gap-2.5 px-3 py-2.5 rounded-lg text-sm transition ${
                selected === f
                  ? 'bg-accent-500/15 text-accent-400 border border-accent-500/30'
                  : 'bg-dark-800/50 text-dark-300 border border-dark-700/50 hover:border-dark-600'
              }`}
            >
              <Folder className="w-4 h-4" />
              {f}
            </button>
          ))}
        </div>
        <div className="flex gap-3 mt-6">
          <button onClick={onClose} className="flex-1 py-2.5 bg-dark-800 text-dark-300 rounded-xl text-sm font-medium hover:bg-dark-700 transition">
            {t('common.cancel')}
          </button>
          <button
            onClick={() => onMoved(server.id, selected || null)}
            className="flex-1 py-2.5 bg-accent-500 text-white rounded-xl text-sm font-medium hover:bg-accent-600 transition"
          >
            {t('common.save')}
          </button>
        </div>
      </div>
    </Overlay>
  )
}

/* ------------------------------------------------------------------ */
/*  Small helpers                                                      */
/* ------------------------------------------------------------------ */

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <label className="text-sm text-dark-300">{label}</label>
      {children}
    </div>
  )
}

function ToggleRow({ label, checked, onChange }: {
  label: string
  checked: boolean
  onChange: (v: boolean) => void
}) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-sm text-dark-300">{label}</span>
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
