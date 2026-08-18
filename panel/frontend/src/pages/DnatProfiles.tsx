import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { toast } from 'sonner'
import { useTranslation } from 'react-i18next'
import { Link, useParams } from 'react-router-dom'
import {
  Route as RouteIcon,
  Plus,
  Trash2,
  RefreshCw,
  Loader2,
  Edit3,
  Save,
  X,
  AlertTriangle,
  Server,
  Link2,
  Unlink,
  ShieldAlert,
  ListChecks,
  History,
  CheckCircle2,
  XCircle,
  Clock,
  Copy,
  Search,
  Folder,
  FolderOpen,
  ChevronDown,
  Eye,
  EyeOff,
  Lock,
  Activity,
} from 'lucide-react'
import { Tooltip } from '../components/ui/Tooltip'
import {
  dnatProfilesApi,
  DnatProfile,
  DnatProfileWithServers,
  DnatRuleData,
  DnatSyncLogEntry,
  DnatAvailableServer,
  DnatProfileServerInfo,
  DnatProtocol,
  DnatDistribution,
  DnatSyncStatus,
} from '../api/client'
import { FAQIcon } from '../components/FAQ'
import { formatListen, formatTarget, protocolLabel, splitTargets } from '../utils/dnat'

type TabKey = 'rules' | 'servers' | 'log'
type TranslateFn = (key: string, options?: Record<string, unknown>) => string

const PROTOCOL_OPTIONS: DnatProtocol[] = ['tcp', 'udp', 'both']
const DISTRIBUTION_OPTIONS: DnatDistribution[] = ['per_server', 'random', 'round_robin', 'client_hash']

const EMPTY_RULE: DnatRuleData = {
  name: '',
  protocol: 'tcp',
  listen_port: 443,
  listen_port_end: null,
  target_ip: '',
  distribution: 'per_server',
  target_port: 0,
  masquerade: true,
  mask_ttl: false,
  enabled: true,
  comment: '',
}

const inputCls =
  'w-full px-3 py-1.5 rounded-lg bg-dark-800 border border-dark-700 text-dark-100 text-sm placeholder-dark-600 focus:outline-none focus:border-accent-500/50 transition-colors'

function extractErrorMessage(err: unknown, fallback: string): string {
  const e = err as { response?: { data?: { detail?: unknown } }; message?: string }
  const detail = e?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const msg = (detail[0] as { msg?: string } | undefined)?.msg
    if (msg) return msg.replace(/^Value error, /, '')
  }
  if (e?.message) return e.message
  return fallback
}

function coversSshPort(rules: DnatRuleData[], sshPort: number): boolean {
  return rules.some(r =>
    r.enabled && r.protocol !== 'udp'
    && r.listen_port <= sshPort && (r.listen_port_end ?? r.listen_port) >= sshPort,
  )
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—'
  try {
    return new Date(value).toLocaleString()
  } catch {
    return value
  }
}

function syncStatusBadge(status: DnatSyncStatus, t: TranslateFn) {
  const map: Record<string, { color: string; label: string; icon: React.ReactNode }> = {
    pending: {
      color: 'text-yellow-400 bg-yellow-500/10 border-yellow-500/20',
      label: t('dnat_profiles.status_pending'),
      icon: <Clock className="w-3 h-3" />,
    },
    synced: {
      color: 'text-green-400 bg-green-500/10 border-green-500/20',
      label: t('dnat_profiles.status_synced'),
      icon: <CheckCircle2 className="w-3 h-3" />,
    },
    failed: {
      color: 'text-red-400 bg-red-500/10 border-red-500/20',
      label: t('dnat_profiles.status_failed'),
      icon: <XCircle className="w-3 h-3" />,
    },
    denied: {
      color: 'text-purple bg-purple/10 border-purple/20',
      label: t('node_caps.status_denied'),
      icon: <Lock className="w-3 h-3" />,
    },
  }
  const s = status ? map[status] : null
  if (!s) {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs border text-dark-400 bg-dark-700/30 border-dark-600/40">
        <Clock className="w-3 h-3" /> —
      </span>
    )
  }
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs border ${s.color}`}>
      {s.icon} {s.label}
    </span>
  )
}


function RuleForm({
  initial,
  isEdit,
  saving,
  onSave,
  onCancel,
}: {
  initial: DnatRuleData
  isEdit: boolean
  saving: boolean
  onSave: (rule: DnatRuleData) => void
  onCancel: () => void
}) {
  const { t } = useTranslation()
  const [form, setForm] = useState<DnatRuleData>(initial)
  const [portEndText, setPortEndText] = useState(initial.listen_port_end ? String(initial.listen_port_end) : '')

  const update = (patch: Partial<DnatRuleData>) => setForm(prev => ({ ...prev, ...patch }))

  const handleSubmit = () => {
    const name = form.name.trim()
    if (!/^[a-zA-Z0-9_-]{1,64}$/.test(name)) {
      toast.error(t('dnat_profiles.validation_name'))
      return
    }
    if (!form.listen_port || form.listen_port < 1 || form.listen_port > 65535) {
      toast.error(t('dnat_profiles.validation_port'))
      return
    }
    const portEnd = portEndText.trim() ? parseInt(portEndText, 10) : null
    if (portEnd !== null && (Number.isNaN(portEnd) || portEnd < form.listen_port || portEnd > 65535)) {
      toast.error(t('dnat_profiles.validation_port_range'))
      return
    }
    const targets = splitTargets(form.target_ip)
    if (targets.length === 0 || targets.some(ip => !/^\d{1,3}(\.\d{1,3}){3}$/.test(ip))) {
      toast.error(t('dnat_profiles.validation_target'))
      return
    }
    if (form.target_port < 0 || form.target_port > 65535) {
      toast.error(t('dnat_profiles.validation_port'))
      return
    }
    onSave({
      ...form,
      name,
      listen_port_end: portEnd === form.listen_port ? null : portEnd,
      target_ip: targets.join(','),
      comment: form.comment?.trim() ? form.comment.trim() : null,
    })
  }

  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      exit={{ opacity: 0, height: 0 }}
      className="overflow-hidden"
    >
      <div className="p-4 bg-dark-800/50 rounded-xl border border-dark-700/50 space-y-3">
        <div className="flex items-center justify-between">
          <h4 className="text-sm font-medium text-dark-200 flex items-center gap-2">
            {isEdit
              ? <><Edit3 className="w-3.5 h-3.5 text-accent-400" /> {t('dnat_profiles.edit_rule')}</>
              : <><Plus className="w-3.5 h-3.5 text-accent-400" /> {t('dnat_profiles.new_rule')}</>}
          </h4>
          <button onClick={onCancel} className="p-1 hover:bg-dark-700 rounded-lg text-dark-400 transition-colors">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <div className="col-span-2">
            <label className="block text-xs text-dark-400 mb-1">{t('dnat_profiles.field_name')}</label>
            <input
              type="text"
              value={form.name}
              onChange={e => update({ name: e.target.value })}
              placeholder="vless-de1"
              className={inputCls}
              disabled={isEdit}
              autoFocus={!isEdit}
            />
          </div>
          <div>
            <label className="block text-xs text-dark-400 mb-1">{t('dnat_profiles.field_protocol')}</label>
            <select value={form.protocol} onChange={e => update({ protocol: e.target.value as DnatProtocol })} className={inputCls}>
              {PROTOCOL_OPTIONS.map(p => <option key={p} value={p}>{protocolLabel(p)}</option>)}
            </select>
          </div>
          <div className="flex items-end gap-4 pb-1.5">
            <Tooltip label={t('dnat_profiles.enabled_hint')} maxWidth={340}>
              <label className="flex items-center gap-1.5 text-xs text-dark-300 cursor-pointer">
                <input type="checkbox" checked={form.enabled} onChange={e => update({ enabled: e.target.checked })} className="accent-accent-500" />
                {t('dnat_profiles.field_enabled')}
              </label>
            </Tooltip>
          </div>

          <div>
            <label className="block text-xs text-dark-400 mb-1">{t('dnat_profiles.field_listen_port')}</label>
            <input
              type="number"
              min={1}
              max={65535}
              value={form.listen_port || ''}
              onChange={e => update({ listen_port: parseInt(e.target.value) || 0 })}
              placeholder="443"
              className={inputCls}
            />
          </div>
          <div>
            <label className="block text-xs text-dark-400 mb-1">{t('dnat_profiles.field_listen_port_end')}</label>
            <input
              type="number"
              min={1}
              max={65535}
              value={portEndText}
              onChange={e => setPortEndText(e.target.value)}
              placeholder="—"
              className={inputCls}
            />
          </div>
          <div>
            <label className="block text-xs text-dark-400 mb-1">{t('dnat_profiles.field_target_ip')}</label>
            <input
              type="text"
              value={form.target_ip}
              onChange={e => update({ target_ip: e.target.value })}
              placeholder="10.0.0.2, 10.0.0.3"
              className={inputCls}
            />
            <p className="text-[11px] text-dark-500 mt-1">{t('dnat_profiles.target_ip_hint')}</p>
          </div>
          {splitTargets(form.target_ip).length > 1 && (
            <div className="col-span-2 sm:col-span-4">
              <label className="block text-xs text-dark-400 mb-1">{t('dnat_profiles.field_distribution')}</label>
              <select value={form.distribution} onChange={e => update({ distribution: e.target.value as DnatDistribution })} className={inputCls}>
                {DISTRIBUTION_OPTIONS.map(d => <option key={d} value={d}>{t(`dnat_profiles.distribution_${d}`)}</option>)}
              </select>
              <p className="text-[11px] text-dark-500 mt-1">{t(`dnat_profiles.distribution_${form.distribution}_hint`)}</p>
            </div>
          )}
          <div>
            <label className="block text-xs text-dark-400 mb-1">{t('dnat_profiles.field_target_port')}</label>
            <input
              type="number"
              min={0}
              max={65535}
              value={form.target_port}
              onChange={e => update({ target_port: parseInt(e.target.value) || 0 })}
              placeholder="0"
              className={inputCls}
            />
            <p className="text-[11px] text-dark-500 mt-1">{t('dnat_profiles.target_port_hint')}</p>
          </div>

          <div className="col-span-2 sm:col-span-4">
            <label className="block text-xs text-dark-400 mb-1">{t('dnat_profiles.field_comment')}</label>
            <input
              type="text"
              value={form.comment ?? ''}
              onChange={e => update({ comment: e.target.value })}
              placeholder={t('dnat_profiles.comment_placeholder')}
              className={inputCls}
            />
          </div>
          <div className="col-span-2 sm:col-span-4 flex flex-wrap gap-x-6 gap-y-2">
            <Tooltip label={t('dnat_profiles.masquerade_hint')} maxWidth={360}>
              <label className="inline-flex items-center gap-1.5 text-xs text-dark-300 cursor-pointer">
                <input type="checkbox" checked={form.masquerade} onChange={e => update({ masquerade: e.target.checked })} className="accent-accent-500" />
                {t('dnat_profiles.field_masquerade')}
              </label>
            </Tooltip>
            <Tooltip label={t('dnat_profiles.mask_ttl_hint')} maxWidth={380}>
              <label className="inline-flex items-center gap-1.5 text-xs text-dark-300 cursor-pointer">
                <input type="checkbox" checked={form.mask_ttl} onChange={e => update({ mask_ttl: e.target.checked })} className="accent-accent-500" />
                {t('dnat_profiles.field_mask_ttl')}
              </label>
            </Tooltip>
          </div>
        </div>

        <div className="flex justify-end gap-2 pt-1">
          <button
            onClick={onCancel}
            className="px-3 py-1.5 rounded-lg text-xs text-dark-300 hover:text-dark-100 bg-dark-800 hover:bg-dark-700 border border-dark-700 transition-colors"
          >
            {t('common.cancel')}
          </button>
          <button
            onClick={handleSubmit}
            disabled={saving}
            className="px-3 py-1.5 rounded-lg text-xs font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors disabled:opacity-50 flex items-center gap-1.5"
          >
            {saving && <Loader2 className="w-3 h-3 animate-spin" />}
            {isEdit ? t('common.save') : t('common.add')}
          </button>
        </div>
      </div>
    </motion.div>
  )
}


function CreateProfileModal({
  onClose,
  onCreated,
}: {
  onClose: () => void
  onCreated: (profile: DnatProfile) => void
}) {
  const { t } = useTranslation()
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [saving, setSaving] = useState(false)
  const [mouseDown, setMouseDown] = useState(false)

  const handleSubmit = async () => {
    if (!name.trim()) {
      toast.error(t('dnat_profiles.name_required'))
      return
    }
    setSaving(true)
    try {
      const res = await dnatProfilesApi.create({ name: name.trim(), description: description.trim() || null })
      toast.success(t('dnat_profiles.profile_created'))
      onCreated(res.data)
    } catch (err) {
      toast.error(extractErrorMessage(err, t('dnat_profiles.create_error')))
    } finally {
      setSaving(false)
    }
  }

  return (
    <motion.div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-dark-950/80 backdrop-blur-sm"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      onMouseDown={e => { if (e.target === e.currentTarget) setMouseDown(true) }}
      onClick={e => {
        if (e.target === e.currentTarget && mouseDown) onClose()
        setMouseDown(false)
      }}
    >
      <motion.div
        className="bg-dark-900 border border-dark-700 rounded-2xl shadow-2xl w-full max-w-lg"
        initial={{ opacity: 0, scale: 0.95, y: 20 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.95, y: 20 }}
        onMouseDown={() => setMouseDown(false)}
      >
        <div className="flex items-center justify-between p-5 border-b border-dark-700">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-accent-500/10 flex items-center justify-center">
              <RouteIcon className="w-5 h-5 text-accent-400" />
            </div>
            <h2 className="text-lg font-semibold text-dark-100">{t('dnat_profiles.new_profile_title')}</h2>
          </div>
          <button onClick={onClose} className="p-2 hover:bg-dark-800 rounded-lg text-dark-400 transition-colors">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="p-5 space-y-4">
          <div>
            <label className="block text-xs text-dark-400 mb-1">{t('dnat_profiles.name')}</label>
            <input type="text" value={name} onChange={e => setName(e.target.value)} placeholder="entry-fr" className={inputCls} autoFocus />
          </div>
          <div>
            <label className="block text-xs text-dark-400 mb-1">{t('dnat_profiles.description')}</label>
            <input type="text" value={description} onChange={e => setDescription(e.target.value)} placeholder={t('dnat_profiles.description_placeholder')} className={inputCls} />
          </div>
          <p className="text-xs text-dark-500">{t('dnat_profiles.create_hint')}</p>
        </div>

        <div className="flex items-center justify-end gap-3 p-5 border-t border-dark-700">
          <button onClick={onClose} className="px-4 py-2 rounded-lg text-sm text-dark-300 hover:text-dark-100 bg-dark-800 hover:bg-dark-700 border border-dark-700 transition-colors">
            {t('common.cancel')}
          </button>
          <button
            onClick={handleSubmit}
            disabled={saving}
            className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors disabled:opacity-50"
          >
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
            {t('common.create')}
          </button>
        </div>
      </motion.div>
    </motion.div>
  )
}


function ProfileListItem({
  profile,
  selected,
  onSelect,
}: {
  profile: DnatProfile
  selected: boolean
  onSelect: (id: number) => void
}) {
  const { t } = useTranslation()
  const linked = profile.linked_servers_count
  const synced = profile.synced_servers_count
  const hasUnsync = linked > 0 && synced < linked
  const activeRules = profile.rules.filter(r => r.enabled).length

  return (
    <motion.button
      layout
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -4 }}
      onClick={() => onSelect(profile.id)}
      className={`w-full text-left rounded-xl border transition-all duration-200 ${
        selected
          ? 'bg-accent-500/10 border-accent-500/40'
          : 'bg-dark-800/60 border-dark-700/60 hover:border-dark-600'
      }`}
    >
      <div className="px-4 py-3 flex items-center justify-between gap-3 min-w-0">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <RouteIcon className={`w-4 h-4 shrink-0 ${selected ? 'text-accent-400' : 'text-dark-400'}`} />
            <span className={`text-sm font-medium truncate ${selected ? 'text-dark-100' : 'text-dark-200'}`}>{profile.name}</span>
            {profile.ssh_port_covered && (
              <Tooltip label={t('dnat_profiles.ssh_warning', { port: profile.ssh_default_port })}>
                <span className="shrink-0 text-red-400">
                  <ShieldAlert className="w-3.5 h-3.5" />
                </span>
              </Tooltip>
            )}
          </div>
          <div className="text-xs text-dark-500 truncate mt-0.5">
            {profile.description || t('dnat_profiles.rules_count', { count: activeRules })}
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <span className="inline-flex items-center gap-1 text-xs text-dark-400">
            <Server className="w-3 h-3" /> {synced}/{linked}
          </span>
          {hasUnsync && (
            <Tooltip label={t('dnat_profiles.has_unsynced')}>
              <span className="w-2 h-2 rounded-full bg-yellow-400" />
            </Tooltip>
          )}
        </div>
      </div>
    </motion.button>
  )
}


function ProfileHeader({
  profile,
  saving,
  syncing,
  onSyncAll,
  onClone,
  onDelete,
  onSave,
}: {
  profile: DnatProfileWithServers
  saving: boolean
  syncing: boolean
  onSyncAll: () => void
  onClone: () => void
  onDelete: () => void
  onSave: (patch: { name?: string; description?: string | null }) => Promise<void>
}) {
  const { t } = useTranslation()
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState(profile.name)
  const [description, setDescription] = useState(profile.description ?? '')

  useEffect(() => {
    setName(profile.name)
    setDescription(profile.description ?? '')
  }, [profile.id, profile.name, profile.description])

  const handleSave = async () => {
    const trimmedName = name.trim()
    if (!trimmedName) {
      toast.error(t('dnat_profiles.name_required'))
      return
    }
    await onSave({ name: trimmedName, description: description.trim() || null })
    setEditing(false)
  }

  if (editing) {
    return (
      <div className="space-y-3">
        <input value={name} onChange={e => setName(e.target.value)} placeholder={t('dnat_profiles.name')} className={inputCls} />
        <input value={description} onChange={e => setDescription(e.target.value)} placeholder={t('dnat_profiles.description')} className={inputCls} />
        <div className="flex justify-end gap-2">
          <button
            onClick={() => { setEditing(false); setName(profile.name); setDescription(profile.description ?? '') }}
            className="px-3 py-1.5 rounded-lg text-xs text-dark-300 hover:text-dark-100 bg-dark-800 hover:bg-dark-700 border border-dark-700 transition-colors"
          >
            {t('common.cancel')}
          </button>
          <button
            onClick={handleSave}
            disabled={saving}
            className="px-3 py-1.5 rounded-lg text-xs font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors disabled:opacity-50 flex items-center gap-1.5"
          >
            {saving ? <Loader2 className="w-3 h-3 animate-spin" /> : <Save className="w-3 h-3" />}
            {t('common.save')}
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="flex items-start justify-between gap-4 flex-wrap">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <h2 className="text-xl font-bold text-dark-100 truncate">{profile.name}</h2>
          <Tooltip label={t('common.edit')}>
            <button
              onClick={() => setEditing(true)}
              className="p-1.5 rounded-lg text-dark-400 hover:text-dark-200 hover:bg-dark-800 transition-colors"
            >
              <Edit3 className="w-3.5 h-3.5" />
            </button>
          </Tooltip>
        </div>
        <p className="text-sm text-dark-400 mt-1">{profile.description || t('dnat_profiles.no_description')}</p>
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        <Tooltip label={t('dnat_profiles.sync_all_tooltip')}>
          <button
            onClick={onSyncAll}
            disabled={syncing || profile.servers.length === 0}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors disabled:opacity-50"
          >
            {syncing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
            {t('dnat_profiles.sync_all')}
          </button>
        </Tooltip>
        <Tooltip label={t('dnat_profiles.clone_tooltip')}>
          <button
            onClick={onClone}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs text-dark-300 hover:text-dark-100 bg-dark-800/50 hover:bg-dark-700/50 border border-dark-700/50 transition-colors"
          >
            <Copy className="w-3.5 h-3.5" /> {t('dnat_profiles.clone')}
          </button>
        </Tooltip>
        <button
          onClick={onDelete}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs text-red-400 hover:text-red-300 bg-red-500/10 hover:bg-red-500/20 border border-red-500/20 transition-colors"
        >
          <Trash2 className="w-3.5 h-3.5" /> {t('common.delete')}
        </button>
      </div>
    </div>
  )
}


function RulesTab({
  profile,
  rules,
  onAddRule,
  onUpdateRule,
  onDeleteRule,
}: {
  profile: DnatProfileWithServers
  rules: DnatRuleData[]
  onAddRule: (rule: DnatRuleData) => Promise<boolean>
  onUpdateRule: (index: number, rule: DnatRuleData) => Promise<boolean>
  onDeleteRule: (index: number) => Promise<void>
}) {
  const { t } = useTranslation()
  const [showForm, setShowForm] = useState(false)
  const [editingIndex, setEditingIndex] = useState<number | null>(null)
  const [saving, setSaving] = useState(false)

  const handleAdd = async (rule: DnatRuleData) => {
    setSaving(true)
    try {
      if (await onAddRule(rule)) setShowForm(false)
    } finally {
      setSaving(false)
    }
  }

  const handleUpdate = async (rule: DnatRuleData) => {
    if (editingIndex === null) return
    setSaving(true)
    try {
      if (await onUpdateRule(editingIndex, rule)) setEditingIndex(null)
    } finally {
      setSaving(false)
    }
  }

  const toggleEnabled = (index: number, rule: DnatRuleData) => {
    void onUpdateRule(index, { ...rule, enabled: !rule.enabled })
  }

  return (
    <div className="space-y-4">
      {profile.ssh_port_covered && (
        <div className="flex items-start gap-3 p-3 rounded-xl bg-red-500/10 border border-red-500/30 text-red-300">
          <AlertTriangle className="w-5 h-5 shrink-0 mt-0.5" />
          <div className="text-sm">{t('dnat_profiles.ssh_warning_long', { port: profile.ssh_default_port })}</div>
        </div>
      )}

      <div className="flex items-start gap-3 p-3 rounded-xl bg-dark-800/40 border border-dark-700/40 text-dark-400 text-xs">
        <Activity className="w-4 h-4 shrink-0 mt-0.5 text-accent-400" />
        <div>{t('dnat_profiles.how_it_works', { port: profile.node_api_port })}</div>
      </div>

      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-dark-200">{t('dnat_profiles.rules')} ({rules.length})</h3>
        {!showForm && editingIndex === null && (
          <button
            onClick={() => setShowForm(true)}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors"
          >
            <Plus className="w-3.5 h-3.5" /> {t('dnat_profiles.add_rule')}
          </button>
        )}
      </div>

      <AnimatePresence>
        {showForm && (
          <RuleForm
            initial={EMPTY_RULE}
            isEdit={false}
            saving={saving}
            onSave={handleAdd}
            onCancel={() => setShowForm(false)}
          />
        )}
      </AnimatePresence>

      {rules.length === 0 ? (
        <div className="text-center text-dark-500 text-sm py-6 bg-dark-900/30 rounded-lg border border-dark-800/50">
          {t('dnat_profiles.no_rules')}
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-dark-800/50">
          <table className="w-full text-sm">
            <thead className="bg-dark-900/40 text-dark-400 text-xs">
              <tr>
                <th className="text-left px-3 py-2 font-medium">{t('dnat_profiles.col_rule')}</th>
                <th className="text-left px-3 py-2 font-medium">{t('dnat_profiles.col_protocol')}</th>
                <th className="text-left px-3 py-2 font-medium">{t('dnat_profiles.col_listen')}</th>
                <th className="text-left px-3 py-2 font-medium">{t('dnat_profiles.col_target')}</th>
                <th className="text-left px-3 py-2 font-medium">{t('dnat_profiles.col_options')}</th>
                <th className="text-left px-3 py-2 font-medium">{t('dnat_profiles.field_comment')}</th>
                <th className="text-right px-3 py-2 font-medium">{t('dnat_profiles.col_actions')}</th>
              </tr>
            </thead>
            <tbody>
              {rules.map((rule, index) => (
                <Fragment key={rule.name}>
                  <tr className={`border-t border-dark-800/40 hover:bg-dark-800/30 transition-colors ${rule.enabled ? '' : 'opacity-50'}`}>
                    <td className="px-3 py-2 font-medium text-dark-100 font-mono">{rule.name}</td>
                    <td className="px-3 py-2 text-dark-300">{protocolLabel(rule.protocol)}</td>
                    <td className="px-3 py-2 font-mono text-dark-200">:{formatListen(rule)}</td>
                    <td className="px-3 py-2 font-mono text-dark-200">→ {formatTarget(rule)}</td>
                    <td className="px-3 py-2">
                      <div className="flex items-center gap-1.5 flex-wrap">
                        {!rule.enabled && (
                          <span className="px-2 py-0.5 rounded-md text-[11px] bg-dark-700/50 text-dark-400 border border-dark-600/40">
                            {t('dnat_profiles.disabled_badge')}
                          </span>
                        )}
                        {!rule.masquerade && (
                          <Tooltip label={t('dnat_profiles.masquerade_hint')} maxWidth={360}>
                            <span className="px-2 py-0.5 rounded-md text-[11px] bg-warning/10 text-warning border border-warning/20">
                              {t('dnat_profiles.no_masq_badge')}
                            </span>
                          </Tooltip>
                        )}
                        {rule.mask_ttl && (
                          <Tooltip label={t('dnat_profiles.mask_ttl_hint')} maxWidth={380}>
                            <span className="px-2 py-0.5 rounded-md text-[11px] bg-dark-700/60 text-dark-200 border border-dark-600/50">
                              {t('dnat_profiles.mask_ttl_badge')}
                            </span>
                          </Tooltip>
                        )}
                        {splitTargets(rule.target_ip).length > 1 && (
                          <Tooltip label={t(`dnat_profiles.distribution_${rule.distribution ?? 'per_server'}_hint`)} maxWidth={360}>
                            <span className="px-2 py-0.5 rounded-md text-[11px] bg-accent-500/10 text-accent-400 border border-accent-500/20">
                              {t('dnat_profiles.balancer_badge', { count: splitTargets(rule.target_ip).length })} · {t(`dnat_profiles.distribution_${rule.distribution ?? 'per_server'}`)}
                            </span>
                          </Tooltip>
                        )}
                        {rule.enabled && rule.masquerade && !rule.mask_ttl && splitTargets(rule.target_ip).length <= 1 && <span className="text-dark-600">—</span>}
                      </div>
                    </td>
                    <td className="px-3 py-2 text-dark-400 truncate max-w-xs">{rule.comment || '—'}</td>
                    <td className="px-3 py-2">
                      <div className="flex items-center justify-end gap-1">
                        <Tooltip label={rule.enabled ? t('dnat_profiles.disable_rule') : t('dnat_profiles.enable_rule')}>
                          <button
                            onClick={() => toggleEnabled(index, rule)}
                            className={`p-1.5 rounded-lg transition-colors ${rule.enabled ? 'text-green-400 hover:bg-green-500/10' : 'text-dark-500 hover:text-dark-300 hover:bg-dark-700/50'}`}
                          >
                            {rule.enabled ? <Eye className="w-3.5 h-3.5" /> : <EyeOff className="w-3.5 h-3.5" />}
                          </button>
                        </Tooltip>
                        <Tooltip label={t('common.edit')}>
                          <button
                            onClick={() => { setEditingIndex(index); setShowForm(false) }}
                            className="p-1.5 rounded-lg text-dark-400 hover:text-dark-200 hover:bg-dark-700/50 transition-colors"
                          >
                            <Edit3 className="w-3.5 h-3.5" />
                          </button>
                        </Tooltip>
                        <Tooltip label={t('common.delete')}>
                          <button
                            onClick={() => {
                              if (confirm(t('dnat_profiles.delete_rule_confirm', { name: rule.name }))) void onDeleteRule(index)
                            }}
                            className="p-1.5 rounded-lg text-dark-400 hover:text-red-400 hover:bg-red-500/10 transition-colors"
                          >
                            <Trash2 className="w-3.5 h-3.5" />
                          </button>
                        </Tooltip>
                      </div>
                    </td>
                  </tr>
                  {editingIndex === index && (
                    <tr>
                      <td colSpan={7} className="px-3 py-2 bg-dark-900/40">
                        <RuleForm
                          initial={rule}
                          isEdit={true}
                          saving={saving}
                          onSave={handleUpdate}
                          onCancel={() => setEditingIndex(null)}
                        />
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}


function ServersTab({
  profile,
  availableServers,
  syncingServerId,
  onSyncOne,
  onUnlink,
  onLink,
}: {
  profile: DnatProfileWithServers
  availableServers: DnatAvailableServer[]
  syncingServerId: number | null
  onSyncOne: (serverId: number) => void
  onUnlink: (serverId: number) => void
  onLink: (serverId: number) => void
}) {
  const { t } = useTranslation()
  const { uid } = useParams()
  const [searchQuery, setSearchQuery] = useState('')
  const [showBusy, setShowBusy] = useState(false)
  const [expandedFolders, setExpandedFolders] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem('dnat_add_expanded_folders')
      return raw ? new Set(JSON.parse(raw)) : new Set()
    } catch { return new Set() }
  })

  const toggleCollapsed = (folder: string) => {
    setExpandedFolders(prev => {
      const next = new Set(prev)
      if (next.has(folder)) next.delete(folder)
      else next.add(folder)
      localStorage.setItem('dnat_add_expanded_folders', JSON.stringify([...next]))
      return next
    })
  }

  const linkedIds = useMemo(() => new Set(profile.servers.map(s => s.server_id)), [profile.servers])
  const base = useMemo(() => availableServers.filter(s => !linkedIds.has(s.id)), [availableServers, linkedIds])
  const hiddenBusy = useMemo(() => base.filter(s => s.active_profile_id != null).length, [base])
  const visibleCandidates = useMemo(
    () => (showBusy ? base : base.filter(s => s.active_profile_id == null)),
    [base, showBusy]
  )

  const grouped = useMemo(() => {
    const folders = new Map<string, DnatAvailableServer[]>()
    const noFolder: DnatAvailableServer[] = []
    for (const s of visibleCandidates) {
      if (s.folder) {
        if (!folders.has(s.folder)) folders.set(s.folder, [])
        folders.get(s.folder)!.push(s)
      } else {
        noFolder.push(s)
      }
    }
    return { folders, noFolder }
  }, [visibleCandidates])

  const sortedFolderNames = useMemo(() => {
    const allNames = [...grouped.folders.keys()]
    try {
      const saved: string[] = JSON.parse(localStorage.getItem('dashboard_folder_order') || '[]')
      const ordered = saved.filter(f => allNames.includes(f))
      const rest = allNames.filter(f => !saved.includes(f)).sort()
      return [...ordered, ...rest]
    } catch {
      return allNames.sort()
    }
  }, [grouped.folders])

  const matchesQuery = (name: string, url: string, q: string) =>
    name.toLowerCase().includes(q) || url.toLowerCase().includes(q)

  const filteredGroups = useMemo(() => {
    const q = searchQuery.toLowerCase().trim()
    if (!q) return grouped
    const folders = new Map<string, DnatAvailableServer[]>()
    for (const [name, svrs] of grouped.folders) {
      const matched = svrs.filter(s => matchesQuery(s.name, s.url, q))
      if (matched.length > 0) folders.set(name, matched)
    }
    const noFolder = grouped.noFolder.filter(s => matchesQuery(s.name, s.url, q))
    return { folders, noFolder }
  }, [searchQuery, grouped])

  const filteredLinked = useMemo(() => {
    const q = searchQuery.toLowerCase().trim()
    if (!q) return profile.servers
    return profile.servers.filter(s => matchesQuery(s.server_name, s.server_url, q))
  }, [searchQuery, profile.servers])

  const searching = searchQuery.trim().length > 0
  const hasFolders = grouped.folders.size > 0
  const noCandidates = filteredGroups.folders.size === 0 && filteredGroups.noFolder.length === 0
  const emptyCandidatesMsg = searching
    ? t('dnat_profiles.nothing_found')
    : base.length === 0
      ? t('dnat_profiles.all_linked')
      : hiddenBusy > 0
        ? t('dnat_profiles.no_free_servers')
        : t('dnat_profiles.all_linked')

  const renderCandidate = (srv: DnatAvailableServer) => (
    <div key={srv.id} className="flex items-center justify-between px-3 py-2 rounded-lg bg-dark-900/30 border border-dark-800/50">
      <div className="flex items-center gap-3 min-w-0">
        <Server className="w-4 h-4 text-dark-400 shrink-0" />
        <div className="min-w-0">
          <div className="text-sm text-dark-200 truncate">{srv.name}</div>
          <div className="text-xs text-dark-500 truncate">{srv.url}</div>
        </div>
        {srv.active_profile_id && (
          <span className="text-xs text-yellow-400/80 shrink-0">{t('dnat_profiles.in_other_profile')}</span>
        )}
      </div>
      <button
        onClick={() => onLink(srv.id)}
        className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs text-accent-400 hover:text-accent-300 bg-accent-500/10 hover:bg-accent-500/20 transition-colors"
      >
        <Link2 className="w-3.5 h-3.5" /> {t('dnat_profiles.link')}
      </button>
    </div>
  )

  const renderCandidateGroup = (key: string, label: string, servers: DnatAvailableServer[], isFolder: boolean) => {
    const isCollapsed = !searching && !expandedFolders.has(key)
    const icon = isFolder
      ? (isCollapsed
          ? <Folder className="w-4 h-4 text-accent-400 shrink-0" />
          : <FolderOpen className="w-4 h-4 text-accent-400 shrink-0" />)
      : <Server className="w-4 h-4 text-dark-400 shrink-0" />
    return (
      <div key={key} className="mb-1">
        <div
          className="flex items-center gap-2 p-2 rounded-lg hover:bg-dark-800/50 transition-colors cursor-pointer"
          onClick={() => toggleCollapsed(key)}
        >
          {icon}
          <span className={`font-medium text-sm truncate ${isFolder ? 'text-dark-200' : 'text-dark-400'}`}>{label}</span>
          <span className="text-xs text-dark-500 ml-auto shrink-0">{servers.length}</span>
          <motion.div animate={{ rotate: isCollapsed ? -90 : 0 }} transition={{ duration: 0.15 }}>
            <ChevronDown className="w-3.5 h-3.5 text-dark-500" />
          </motion.div>
        </div>
        <AnimatePresence initial={false}>
          {!isCollapsed && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: 'auto', opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.15 }}
              className="overflow-hidden"
            >
              <div className="pl-4 space-y-1.5 pt-1.5">
                {servers.map(renderCandidate)}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    )
  }

  return (
    <div className="space-y-5">
      <div className="relative">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-dark-500 pointer-events-none" />
        <input
          type="text"
          value={searchQuery}
          onChange={e => setSearchQuery(e.target.value)}
          placeholder={t('dnat_profiles.search_placeholder')}
          className="w-full pl-9 pr-3 py-2 rounded-lg bg-dark-900/50 border border-dark-800 text-sm text-dark-200 placeholder-dark-500 focus:outline-none focus:border-accent-500/50 transition-colors"
        />
      </div>

      <div>
        <h3 className="text-sm font-medium text-dark-200 mb-3">
          {t('dnat_profiles.servers_linked')} ({searching ? `${filteredLinked.length}/${profile.servers.length}` : profile.servers.length})
        </h3>
        {profile.servers.length === 0 ? (
          <div className="text-center text-dark-500 text-sm py-6 bg-dark-900/30 rounded-lg border border-dark-800/50">
            {t('dnat_profiles.no_linked_servers')}
          </div>
        ) : filteredLinked.length === 0 ? (
          <div className="text-center text-dark-500 text-sm py-6 bg-dark-900/30 rounded-lg border border-dark-800/50">
            {t('dnat_profiles.nothing_found')}
          </div>
        ) : (
          <div className="space-y-1.5">
            {filteredLinked.map((srv: DnatProfileServerInfo) => (
              <div key={srv.server_id} className="flex items-center justify-between px-3 py-2 rounded-lg bg-dark-900/30 border border-dark-800/50">
                <div className="flex items-center gap-3 min-w-0">
                  <Server className="w-4 h-4 text-dark-400 shrink-0" />
                  <span className="text-xs text-dark-500 font-mono shrink-0 w-6 text-right">#{srv.link_position}</span>
                  <div className="min-w-0">
                    <Link to={`/${uid}/server/${srv.server_id}/dnat`} className="text-sm text-dark-200 hover:text-accent-300 truncate block transition-colors">
                      {srv.server_name}
                    </Link>
                    <div className="text-xs text-dark-500 truncate">{srv.server_url}</div>
                    {Object.keys(srv.targets ?? {}).length > 0 && (
                      <div className="flex flex-wrap gap-1 mt-1">
                        {Object.entries(srv.targets).map(([ruleName, ip]) => (
                          <span key={ruleName} className="px-1.5 py-0.5 rounded text-[10px] font-mono bg-accent-500/10 text-accent-300 border border-accent-500/20">
                            {ruleName} → {ip}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                  {syncStatusBadge(srv.sync_status, t)}
                </div>
                <div className="flex items-center gap-1 shrink-0">
                  <span className="hidden sm:inline text-xs text-dark-500 mr-2">
                    {srv.last_sync_at ? t('dnat_profiles.synced_at', { time: formatDateTime(srv.last_sync_at) }) : t('dnat_profiles.not_synced')}
                  </span>
                  <Tooltip label={t('dnat_profiles.sync')}>
                    <button
                      onClick={() => onSyncOne(srv.server_id)}
                      disabled={syncingServerId === srv.server_id}
                      className="p-1.5 rounded-lg text-dark-400 hover:text-accent-400 hover:bg-accent-500/10 transition-colors disabled:opacity-50"
                    >
                      {syncingServerId === srv.server_id ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
                    </button>
                  </Tooltip>
                  <Tooltip label={t('dnat_profiles.unlink')}>
                    <button
                      onClick={() => {
                        if (confirm(t('dnat_profiles.unlink_confirm', { name: srv.server_name }))) onUnlink(srv.server_id)
                      }}
                      className="p-1.5 rounded-lg text-dark-400 hover:text-red-400 hover:bg-red-500/10 transition-colors"
                    >
                      <Unlink className="w-3.5 h-3.5" />
                    </button>
                  </Tooltip>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div>
        <div className="flex items-center justify-between gap-2 mb-3">
          <h3 className="text-sm font-medium text-dark-200">{t('dnat_profiles.add_servers')}</h3>
          {hiddenBusy > 0 && (
            <Tooltip label={t('dnat_profiles.busy_hint')}>
              <button
                onClick={() => setShowBusy(v => !v)}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs whitespace-nowrap border transition-colors ${
                  showBusy
                    ? 'text-accent-400 bg-accent-500/10 border-accent-500/30'
                    : 'text-dark-400 bg-dark-900/40 border-dark-800 hover:text-dark-200'
                }`}
              >
                {showBusy ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
                {showBusy ? t('dnat_profiles.hide_busy') : t('dnat_profiles.show_busy', { count: hiddenBusy })}
              </button>
            </Tooltip>
          )}
        </div>
        {noCandidates ? (
          <div className="text-center text-dark-500 text-sm py-6 bg-dark-900/30 rounded-lg border border-dark-800/50">
            {emptyCandidatesMsg}
          </div>
        ) : hasFolders ? (
          <div className="space-y-1">
            {sortedFolderNames
              .filter(name => filteredGroups.folders.has(name))
              .map(name => renderCandidateGroup(name, name, filteredGroups.folders.get(name)!, true))}
            {filteredGroups.noFolder.length > 0 &&
              renderCandidateGroup('__no_folder__', t('dnat_profiles.no_folder'), filteredGroups.noFolder, false)}
          </div>
        ) : (
          <div className="space-y-1.5">
            {filteredGroups.noFolder.map(renderCandidate)}
          </div>
        )}
      </div>
    </div>
  )
}


function LogTab({
  log,
  loading,
  onRefresh,
}: {
  log: DnatSyncLogEntry[]
  loading: boolean
  onRefresh: () => void
}) {
  const { t } = useTranslation()
  const statusColor = (status: string): string => {
    const lower = status.toLowerCase()
    if (lower === 'success') return 'text-green-400'
    if (lower === 'failed') return 'text-red-400'
    return 'text-yellow-400'
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-dark-200">{t('dnat_profiles.log_title')}</h3>
        <button
          onClick={onRefresh}
          disabled={loading}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs text-dark-300 hover:text-dark-100 bg-dark-800/50 hover:bg-dark-700/50 border border-dark-700/50 transition-colors disabled:opacity-50"
        >
          {loading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
          {t('common.refresh')}
        </button>
      </div>
      {log.length === 0 ? (
        <div className="text-center text-dark-500 text-sm py-6 bg-dark-900/30 rounded-lg border border-dark-800/50">
          {loading ? t('common.loading') : t('dnat_profiles.log_empty')}
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-dark-800/50">
          <table className="w-full text-sm">
            <thead className="bg-dark-900/40 text-dark-400 text-xs">
              <tr>
                <th className="text-left px-3 py-2 font-medium">{t('dnat_profiles.col_server')}</th>
                <th className="text-left px-3 py-2 font-medium">{t('dnat_profiles.col_status')}</th>
                <th className="text-left px-3 py-2 font-medium">{t('dnat_profiles.col_message')}</th>
                <th className="text-left px-3 py-2 font-medium">{t('dnat_profiles.col_time')}</th>
              </tr>
            </thead>
            <tbody>
              {log.map(entry => (
                <tr key={entry.id} className="border-t border-dark-800/40 hover:bg-dark-800/30 transition-colors">
                  <td className="px-3 py-2 text-dark-200">{entry.server_name}</td>
                  <td className={`px-3 py-2 font-medium ${statusColor(entry.status)}`}>{entry.status}</td>
                  <td className="px-3 py-2 text-dark-400 break-words max-w-md">{entry.message || '—'}</td>
                  <td className="px-3 py-2 text-dark-500 whitespace-nowrap">{formatDateTime(entry.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}


function ProfileDetail({
  profileId,
  onProfileDeleted,
  onProfileChanged,
  onProfileCloned,
}: {
  profileId: number
  onProfileDeleted: () => void
  onProfileChanged: () => void
  onProfileCloned: (clone: DnatProfile) => void
}) {
  const { t } = useTranslation()
  const [profile, setProfile] = useState<DnatProfileWithServers | null>(null)
  const [loading, setLoading] = useState(true)
  const [tab, setTab] = useState<TabKey>('rules')
  const [availableServers, setAvailableServers] = useState<DnatAvailableServer[]>([])
  const [log, setLog] = useState<DnatSyncLogEntry[]>([])
  const [logLoading, setLogLoading] = useState(false)
  const [savingHeader, setSavingHeader] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [syncingServerId, setSyncingServerId] = useState<number | null>(null)

  const fetchProfile = useCallback(async () => {
    try {
      const res = await dnatProfilesApi.get(profileId)
      setProfile(res.data)
    } catch (err) {
      toast.error(extractErrorMessage(err, t('dnat_profiles.load_error')))
    } finally {
      setLoading(false)
    }
  }, [profileId, t])

  const refreshProfileSilent = useCallback(async () => {
    try {
      const res = await dnatProfilesApi.get(profileId)
      setProfile(res.data)
    } catch { /* silent */ }
  }, [profileId])

  const fetchAvailableServers = useCallback(async () => {
    try {
      const res = await dnatProfilesApi.getAvailableServers()
      setAvailableServers(res.data)
    } catch { /* silent */ }
  }, [])

  const fetchLog = useCallback(async () => {
    setLogLoading(true)
    try {
      const res = await dnatProfilesApi.getLog(profileId)
      setLog(res.data)
    } catch (err) {
      toast.error(extractErrorMessage(err, t('dnat_profiles.log_error')))
    } finally {
      setLogLoading(false)
    }
  }, [profileId, t])

  useEffect(() => {
    setLoading(true)
    setTab('rules')
    fetchProfile()
    fetchAvailableServers()
  }, [profileId, fetchProfile, fetchAvailableServers])

  // Статусы серверов обновляются в фоне; в скрытой вкладке бэкенд не дёргаем
  useEffect(() => {
    const id = setInterval(() => { if (!document.hidden) refreshProfileSilent() }, 3000)
    return () => clearInterval(id)
  }, [refreshProfileSilent])

  useEffect(() => {
    if (tab === 'log') fetchLog()
  }, [tab, fetchLog])

  useEffect(() => {
    if (tab !== 'log') return
    const id = setInterval(() => {
      dnatProfilesApi.getLog(profileId).then(res => setLog(res.data)).catch(() => { /* silent */ })
    }, 3000)
    return () => clearInterval(id)
  }, [tab, profileId])

  const handleHeaderSave = async (patch: { name?: string; description?: string | null }) => {
    setSavingHeader(true)
    try {
      await dnatProfilesApi.update(profileId, patch)
      toast.success(t('dnat_profiles.profile_updated'))
      await fetchProfile()
      onProfileChanged()
    } catch (err) {
      toast.error(extractErrorMessage(err, t('dnat_profiles.update_error')))
    } finally {
      setSavingHeader(false)
    }
  }

  const handleDelete = async () => {
    if (!profile) return
    if (!confirm(t('dnat_profiles.delete_confirm', { name: profile.name }))) return
    try {
      await dnatProfilesApi.delete(profileId)
      toast.success(t('dnat_profiles.profile_deleted'))
      onProfileDeleted()
    } catch (err) {
      toast.error(extractErrorMessage(err, t('dnat_profiles.delete_error')))
    }
  }

  const handleClone = async () => {
    if (!profile) return
    const name = prompt(t('dnat_profiles.clone_prompt'), t('dnat_profiles.clone_suffix', { name: profile.name }))
    if (name === null) return
    try {
      const res = await dnatProfilesApi.clone(profileId, name.trim() || undefined)
      toast.success(t('dnat_profiles.cloned', { name: res.data.name }))
      onProfileCloned(res.data)
    } catch (err) {
      toast.error(extractErrorMessage(err, t('dnat_profiles.clone_error')))
    }
  }

  const handleSyncAll = async () => {
    setSyncing(true)
    try {
      const res = await dnatProfilesApi.syncAll(profileId)
      const results = res.data.results
      const ok = results.filter(r => r.success).length
      const queued = results.filter(r => r.queued).length
      const fail = results.length - ok - queued
      const queuedNote = queued > 0 ? t('dnat_profiles.queued_note', { count: queued }) : ''
      if (fail === 0) toast.success(t('dnat_profiles.sync_success', { ok, total: results.length, note: queuedNote }))
      else toast.warning(t('dnat_profiles.sync_partial', { ok, fail, note: queuedNote }))
      await fetchProfile()
      onProfileChanged()
    } catch (err) {
      toast.error(extractErrorMessage(err, t('dnat_profiles.sync_error')))
    } finally {
      setSyncing(false)
    }
  }

  const handleSyncOne = async (serverId: number) => {
    setSyncingServerId(serverId)
    try {
      const res = await dnatProfilesApi.syncOne(profileId, serverId)
      if (res.data.success) toast.success(`${res.data.server_name}: ${res.data.message}`)
      else if (res.data.queued) toast.warning(`${res.data.server_name}: ${res.data.message}`)
      else toast.error(`${res.data.server_name}: ${res.data.message}`)
      await fetchProfile()
      onProfileChanged()
    } catch (err) {
      toast.error(extractErrorMessage(err, t('dnat_profiles.sync_error')))
    } finally {
      setSyncingServerId(null)
    }
  }

  const handleLink = async (serverId: number) => {
    try {
      await dnatProfilesApi.linkServer(profileId, serverId)
      toast.success(t('dnat_profiles.linked'))
      await Promise.all([fetchProfile(), fetchAvailableServers()])
      onProfileChanged()
    } catch (err) {
      toast.error(extractErrorMessage(err, t('dnat_profiles.link_error')))
    }
  }

  const handleUnlink = async (serverId: number) => {
    try {
      await dnatProfilesApi.unlinkServer(profileId, serverId)
      toast.success(t('dnat_profiles.unlinked'))
      await Promise.all([fetchProfile(), fetchAvailableServers()])
      onProfileChanged()
    } catch (err) {
      toast.error(extractErrorMessage(err, t('dnat_profiles.unlink_error')))
    }
  }

  // Правки правил применяются оптимистично, без перезагрузки профиля —
  // предупреждение про SSH пересчитывается здесь же, иначе отставало бы на шаг
  const applyRules = (rules: DnatRuleData[]) => {
    setProfile(prev => prev ? { ...prev, rules, ssh_port_covered: coversSshPort(rules, prev.ssh_default_port) } : prev)
  }

  const handleAddRule = async (rule: DnatRuleData): Promise<boolean> => {
    try {
      const res = await dnatProfilesApi.addRule(profileId, rule)
      toast.success(t('dnat_profiles.rule_added'))
      applyRules(res.data.rules)
      onProfileChanged()
      return true
    } catch (err) {
      toast.error(extractErrorMessage(err, t('dnat_profiles.rule_add_error')))
      return false
    }
  }

  const handleUpdateRule = async (index: number, rule: DnatRuleData): Promise<boolean> => {
    try {
      const res = await dnatProfilesApi.updateRule(profileId, index, rule)
      toast.success(t('dnat_profiles.rule_updated'))
      applyRules(res.data.rules)
      onProfileChanged()
      return true
    } catch (err) {
      toast.error(extractErrorMessage(err, t('dnat_profiles.rule_update_error')))
      return false
    }
  }

  const handleDeleteRule = async (index: number) => {
    try {
      const res = await dnatProfilesApi.deleteRule(profileId, index)
      toast.success(t('dnat_profiles.rule_deleted'))
      applyRules(res.data.rules)
      onProfileChanged()
    } catch (err) {
      toast.error(extractErrorMessage(err, t('dnat_profiles.rule_delete_error')))
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader2 className="w-6 h-6 text-accent-400 animate-spin" />
      </div>
    )
  }
  if (!profile) {
    return <div className="text-center text-dark-500 py-12">{t('dnat_profiles.profile_not_found')}</div>
  }

  const tabs: { key: TabKey; label: string; icon: React.ReactNode }[] = [
    { key: 'rules', label: `${t('dnat_profiles.tab_rules')} (${profile.rules.length})`, icon: <ListChecks className="w-3.5 h-3.5" /> },
    { key: 'servers', label: `${t('dnat_profiles.tab_servers')} (${profile.servers.length})`, icon: <Server className="w-3.5 h-3.5" /> },
    { key: 'log', label: t('dnat_profiles.tab_log'), icon: <History className="w-3.5 h-3.5" /> },
  ]

  return (
    <div className="space-y-5">
      <ProfileHeader
        profile={profile}
        saving={savingHeader}
        syncing={syncing}
        onSyncAll={handleSyncAll}
        onClone={handleClone}
        onDelete={handleDelete}
        onSave={handleHeaderSave}
      />

      <div className="flex items-center gap-1 border-b border-dark-700/60">
        {tabs.map(tabItem => (
          <button
            key={tabItem.key}
            onClick={() => setTab(tabItem.key)}
            className={`flex items-center gap-1.5 px-4 py-2 text-sm border-b-2 transition-colors -mb-px ${
              tab === tabItem.key
                ? 'text-accent-400 border-accent-400'
                : 'text-dark-400 border-transparent hover:text-dark-200'
            }`}
          >
            {tabItem.icon} {tabItem.label}
          </button>
        ))}
      </div>

      {tab === 'rules' && (
        <RulesTab
          profile={profile}
          rules={profile.rules}
          onAddRule={handleAddRule}
          onUpdateRule={handleUpdateRule}
          onDeleteRule={handleDeleteRule}
        />
      )}
      {tab === 'servers' && (
        <ServersTab
          profile={profile}
          availableServers={availableServers}
          syncingServerId={syncingServerId}
          onSyncOne={handleSyncOne}
          onUnlink={handleUnlink}
          onLink={handleLink}
        />
      )}
      {tab === 'log' && (
        <LogTab log={log} loading={logLoading} onRefresh={fetchLog} />
      )}
    </div>
  )
}


export default function DnatProfiles() {
  const { t } = useTranslation()
  const [profiles, setProfiles] = useState<DnatProfile[]>([])
  const [loading, setLoading] = useState(true)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [showCreate, setShowCreate] = useState(false)
  const initialLoadDone = useRef(false)

  const fetchProfiles = useCallback(async () => {
    try {
      const res = await dnatProfilesApi.list()
      setProfiles(res.data)
      setSelectedId(prev => {
        if (prev !== null && res.data.some(p => p.id === prev)) return prev
        return res.data[0]?.id ?? null
      })
    } catch (err) {
      if (!initialLoadDone.current) toast.error(extractErrorMessage(err, t('dnat_profiles.load_profiles_error')))
    } finally {
      if (!initialLoadDone.current) {
        initialLoadDone.current = true
        setLoading(false)
      }
    }
  }, [t])

  useEffect(() => {
    fetchProfiles()
  }, [fetchProfiles])

  useEffect(() => {
    const id = setInterval(() => { if (!document.hidden) fetchProfiles() }, 3000)
    return () => clearInterval(id)
  }, [fetchProfiles])

  const handleCreated = (profile: DnatProfile) => {
    setShowCreate(false)
    setProfiles(prev => [...prev, profile])
    setSelectedId(profile.id)
  }

  const handleDeleted = async () => {
    setSelectedId(null)
    await fetchProfiles()
  }

  const handleCloned = async (clone: DnatProfile) => {
    await fetchProfiles()
    setSelectedId(clone.id)
  }

  return (
    <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="space-y-6">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-accent-500/20 to-accent-600/20 flex items-center justify-center border border-accent-500/20">
            <RouteIcon className="w-5 h-5 text-accent-400" />
          </div>
          <div>
            <h1 className="text-2xl font-bold text-dark-100 flex items-center gap-2">
              {t('dnat_profiles.title')}
              <FAQIcon screen="PAGE_DNAT_PROFILES" />
            </h1>
            <p className="text-sm text-dark-400">{t('dnat_profiles.subtitle')}</p>
          </div>
        </div>
        <button
          onClick={() => setShowCreate(true)}
          className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors"
        >
          <Plus className="w-4 h-4" /> {t('dnat_profiles.create_profile')}
        </button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[320px_1fr] gap-6">
        <div className="space-y-2">
          {loading ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="w-6 h-6 text-accent-400 animate-spin" />
            </div>
          ) : profiles.length === 0 ? (
            <div className="text-center text-dark-500 text-sm py-8 bg-dark-900/30 rounded-xl border border-dark-800/50">
              {t('dnat_profiles.no_profiles')}
              <br />{t('dnat_profiles.no_profiles_hint')}
            </div>
          ) : (
            <AnimatePresence>
              {profiles.map(p => (
                <ProfileListItem key={p.id} profile={p} selected={p.id === selectedId} onSelect={setSelectedId} />
              ))}
            </AnimatePresence>
          )}
        </div>

        <div className="card">
          {selectedId === null ? (
            <div className="flex flex-col items-center justify-center py-16 text-dark-500">
              <RouteIcon className="w-10 h-10 mb-3 text-dark-600" />
              <p className="text-sm">{t('dnat_profiles.select_profile')}</p>
            </div>
          ) : (
            <ProfileDetail
              key={selectedId}
              profileId={selectedId}
              onProfileDeleted={handleDeleted}
              onProfileChanged={fetchProfiles}
              onProfileCloned={handleCloned}
            />
          )}
        </div>
      </div>

      <AnimatePresence>
        {showCreate && (
          <CreateProfileModal onClose={() => setShowCreate(false)} onCreated={handleCreated} />
        )}
      </AnimatePresence>
    </motion.div>
  )
}
