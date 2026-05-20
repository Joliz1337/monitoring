import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { toast } from 'sonner'
import {
  Flame,
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
} from 'lucide-react'
import {
  firewallProfilesApi,
  FirewallProfile,
  FirewallProfileWithServers,
  FirewallProfileRuleData,
  FirewallSyncLogEntry,
  FirewallAvailableServer,
  FirewallProfileServerInfo,
  FirewallRuleProtocol,
  FirewallRuleAction,
  FirewallRuleDirection,
  FirewallDefaultPolicy,
  FirewallSyncStatus,
} from '../api/client'
import { FAQIcon } from '../components/FAQ'

type TabKey = 'rules' | 'servers' | 'log'

const PROTOCOL_OPTIONS: FirewallRuleProtocol[] = ['tcp', 'udp', 'any']
const ACTION_OPTIONS: FirewallRuleAction[] = ['allow', 'deny']
const DIRECTION_OPTIONS: FirewallRuleDirection[] = ['in', 'out']
const POLICY_OPTIONS: FirewallDefaultPolicy[] = ['allow', 'deny', 'reject']

const ACTION_LABELS: Record<FirewallRuleAction, string> = {
  allow: 'разрешить',
  deny: 'запретить',
}
const DIRECTION_LABELS: Record<FirewallRuleDirection, string> = {
  in: 'входящее',
  out: 'исходящее',
}
const POLICY_LABELS: Record<FirewallDefaultPolicy, string> = {
  allow: 'разрешить',
  deny: 'запретить (deny)',
  reject: 'отклонить (reject)',
}

function computeNodePortAllowed(
  rules: FirewallProfileRuleData[],
  defaultIn: FirewallDefaultPolicy,
  nodePort: number,
): boolean {
  if (defaultIn === 'allow') return true
  return rules.some(r =>
    r.port === nodePort
    && (r.protocol === 'tcp' || r.protocol === 'any')
    && r.action === 'allow'
    && r.direction === 'in',
  )
}

const EMPTY_RULE: FirewallProfileRuleData = {
  port: 9100,
  protocol: 'tcp',
  action: 'allow',
  from_ip: null,
  direction: 'in',
  comment: '',
}

const inputCls =
  'w-full px-3 py-1.5 rounded-lg bg-dark-800 border border-dark-700 text-dark-100 text-sm placeholder-dark-600 focus:outline-none focus:border-accent-500/50 transition-colors'

function extractErrorMessage(err: unknown, fallback: string): string {
  const e = err as { response?: { data?: { detail?: unknown } }; message?: string }
  const detail = e?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (e?.message) return e.message
  return fallback
}

function syncStatusBadge(status: FirewallSyncStatus | null) {
  const map: Record<string, { color: string; label: string; icon: React.ReactNode }> = {
    pending: {
      color: 'text-yellow-400 bg-yellow-500/10 border-yellow-500/20',
      label: 'Ожидает',
      icon: <Clock className="w-3 h-3" />,
    },
    synced: {
      color: 'text-green-400 bg-green-500/10 border-green-500/20',
      label: 'Синхронизирован',
      icon: <CheckCircle2 className="w-3 h-3" />,
    },
    failed: {
      color: 'text-red-400 bg-red-500/10 border-red-500/20',
      label: 'Ошибка',
      icon: <XCircle className="w-3 h-3" />,
    },
    rolled_back: {
      color: 'text-orange-400 bg-orange-500/10 border-orange-500/20',
      label: 'Откат',
      icon: <RefreshCw className="w-3 h-3" />,
    },
    drifted: {
      color: 'text-dark-300 bg-dark-700/40 border-dark-600/40',
      label: 'Расхождение',
      icon: <AlertTriangle className="w-3 h-3" />,
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

function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—'
  try {
    return new Date(value).toLocaleString()
  } catch {
    return value
  }
}


function RuleForm({
  initial,
  isEdit,
  saving,
  onSave,
  onCancel,
}: {
  initial: FirewallProfileRuleData
  isEdit: boolean
  saving: boolean
  onSave: (rule: FirewallProfileRuleData) => void
  onCancel: () => void
}) {
  const [form, setForm] = useState<FirewallProfileRuleData>(initial)

  const update = (patch: Partial<FirewallProfileRuleData>) => setForm(prev => ({ ...prev, ...patch }))

  const handleSubmit = () => {
    if (!form.port || form.port < 1 || form.port > 65535) {
      toast.error('Порт должен быть в диапазоне 1–65535')
      return
    }
    const normalized: FirewallProfileRuleData = {
      ...form,
      from_ip: form.from_ip && form.from_ip.trim() ? form.from_ip.trim() : null,
      comment: form.comment?.trim() ? form.comment.trim() : null,
    }
    onSave(normalized)
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
            {isEdit ? <><Edit3 className="w-3.5 h-3.5 text-accent-400" /> Редактировать правило</> : <><Plus className="w-3.5 h-3.5 text-accent-400" /> Новое правило</>}
          </h4>
          <button onClick={onCancel} className="p-1 hover:bg-dark-700 rounded-lg text-dark-400 transition-colors">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
          <div>
            <label className="block text-xs text-dark-400 mb-1">Действие</label>
            <select value={form.action} onChange={e => update({ action: e.target.value as FirewallRuleAction })} className={inputCls}>
              {ACTION_OPTIONS.map(a => <option key={a} value={a}>{ACTION_LABELS[a]}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs text-dark-400 mb-1">Порт</label>
            <input
              type="number"
              min={1}
              max={65535}
              value={form.port || ''}
              onChange={e => update({ port: parseInt(e.target.value) || 0 })}
              placeholder="22"
              className={inputCls}
            />
          </div>
          <div>
            <label className="block text-xs text-dark-400 mb-1">Протокол</label>
            <select value={form.protocol} onChange={e => update({ protocol: e.target.value as FirewallRuleProtocol })} className={inputCls}>
              {PROTOCOL_OPTIONS.map(p => <option key={p} value={p}>{p}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs text-dark-400 mb-1">Источник (IP / CIDR)</label>
            <input
              type="text"
              value={form.from_ip ?? ''}
              onChange={e => update({ from_ip: e.target.value })}
              placeholder="любой"
              className={inputCls}
            />
          </div>
          <div>
            <label className="block text-xs text-dark-400 mb-1">Направление</label>
            <select value={form.direction} onChange={e => update({ direction: e.target.value as FirewallRuleDirection })} className={inputCls}>
              {DIRECTION_OPTIONS.map(d => <option key={d} value={d}>{DIRECTION_LABELS[d]}</option>)}
            </select>
          </div>
          <div className="col-span-2 sm:col-span-3">
            <label className="block text-xs text-dark-400 mb-1">Комментарий</label>
            <input
              type="text"
              value={form.comment ?? ''}
              onChange={e => update({ comment: e.target.value })}
              placeholder="SSH из офиса"
              className={inputCls}
            />
          </div>
        </div>

        <div className="flex justify-end gap-2 pt-1">
          <button
            onClick={onCancel}
            className="px-3 py-1.5 rounded-lg text-xs text-dark-300 hover:text-dark-100 bg-dark-800 hover:bg-dark-700 border border-dark-700 transition-colors"
          >
            Отмена
          </button>
          <button
            onClick={handleSubmit}
            disabled={saving}
            className="px-3 py-1.5 rounded-lg text-xs font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors disabled:opacity-50 flex items-center gap-1.5"
          >
            {saving && <Loader2 className="w-3 h-3 animate-spin" />}
            {isEdit ? 'Сохранить' : 'Добавить'}
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
  onCreated: (profile: FirewallProfile) => void
}) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [defaultIn, setDefaultIn] = useState<FirewallDefaultPolicy>('deny')
  const [defaultOut, setDefaultOut] = useState<FirewallDefaultPolicy>('allow')
  const [saving, setSaving] = useState(false)
  const [mouseDown, setMouseDown] = useState(false)

  const handleSubmit = async () => {
    if (!name.trim()) {
      toast.error('Имя профиля обязательно')
      return
    }
    setSaving(true)
    try {
      const res = await firewallProfilesApi.create({
        name: name.trim(),
        description: description.trim() || null,
        rules: null,
        default_incoming: defaultIn,
        default_outgoing: defaultOut,
      })
      toast.success('Профиль создан')
      onCreated(res.data)
    } catch (err) {
      toast.error(extractErrorMessage(err, 'Не удалось создать профиль'))
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
              <Flame className="w-5 h-5 text-accent-400" />
            </div>
            <h2 className="text-lg font-semibold text-dark-100">Новый firewall профиль</h2>
          </div>
          <button onClick={onClose} className="p-2 hover:bg-dark-800 rounded-lg text-dark-400 transition-colors">
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="p-5 space-y-4">
          <div>
            <label className="block text-xs text-dark-400 mb-1">Имя</label>
            <input type="text" value={name} onChange={e => setName(e.target.value)} placeholder="prod-frontends" className={inputCls} autoFocus />
          </div>
          <div>
            <label className="block text-xs text-dark-400 mb-1">Описание</label>
            <input type="text" value={description} onChange={e => setDescription(e.target.value)} placeholder="Описание профиля" className={inputCls} />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-dark-400 mb-1">По умолчанию: входящий</label>
              <select value={defaultIn} onChange={e => setDefaultIn(e.target.value as FirewallDefaultPolicy)} className={inputCls}>
                {POLICY_OPTIONS.map(p => <option key={p} value={p}>{POLICY_LABELS[p]}</option>)}
              </select>
            </div>
            <div>
              <label className="block text-xs text-dark-400 mb-1">По умолчанию: исходящий</label>
              <select value={defaultOut} onChange={e => setDefaultOut(e.target.value as FirewallDefaultPolicy)} className={inputCls}>
                {POLICY_OPTIONS.map(p => <option key={p} value={p}>{POLICY_LABELS[p]}</option>)}
              </select>
            </div>
          </div>
          <p className="text-xs text-dark-500">
            По умолчанию будет создано правило allow для порта API ноды (9100/tcp), чтобы панель не потеряла связь с сервером.
          </p>
        </div>

        <div className="flex items-center justify-end gap-3 p-5 border-t border-dark-700">
          <button onClick={onClose} className="px-4 py-2 rounded-lg text-sm text-dark-300 hover:text-dark-100 bg-dark-800 hover:bg-dark-700 border border-dark-700 transition-colors">
            Отмена
          </button>
          <button
            onClick={handleSubmit}
            disabled={saving}
            className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors disabled:opacity-50"
          >
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
            Создать
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
  profile: FirewallProfile
  selected: boolean
  onSelect: (id: number) => void
}) {
  const linked = profile.linked_servers_count
  const synced = profile.synced_servers_count
  const hasUnsync = linked > 0 && synced < linked

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
            <Flame className={`w-4 h-4 shrink-0 ${selected ? 'text-accent-400' : 'text-dark-400'}`} />
            <span className={`text-sm font-medium truncate ${selected ? 'text-dark-100' : 'text-dark-200'}`}>{profile.name}</span>
            {!profile.node_port_allowed && (
              <span title={`Нет правила allow для порта API ноды (${profile.node_api_port}/tcp)`} className="shrink-0 text-yellow-400">
                <ShieldAlert className="w-3.5 h-3.5" />
              </span>
            )}
          </div>
          {profile.description && (
            <div className="text-xs text-dark-500 truncate mt-0.5">{profile.description}</div>
          )}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <span className="inline-flex items-center gap-1 text-xs text-dark-400">
            <Server className="w-3 h-3" /> {synced}/{linked}
          </span>
          {hasUnsync && (
            <span className="w-2 h-2 rounded-full bg-yellow-400" title="Есть несинхронизированные серверы" />
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
  forceSync,
  onForceChange,
  onSyncAll,
  onClone,
  onDelete,
  onSave,
}: {
  profile: FirewallProfileWithServers
  saving: boolean
  syncing: boolean
  forceSync: boolean
  onForceChange: (v: boolean) => void
  onSyncAll: () => void
  onClone: () => void
  onDelete: () => void
  onSave: (patch: { name?: string; description?: string | null }) => Promise<void>
}) {
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
      toast.error('Имя не может быть пустым')
      return
    }
    await onSave({ name: trimmedName, description: description.trim() || null })
    setEditing(false)
  }

  if (editing) {
    return (
      <div className="space-y-3">
        <input value={name} onChange={e => setName(e.target.value)} placeholder="Имя" className={inputCls} />
        <input value={description} onChange={e => setDescription(e.target.value)} placeholder="Описание" className={inputCls} />
        <div className="flex justify-end gap-2">
          <button
            onClick={() => { setEditing(false); setName(profile.name); setDescription(profile.description ?? '') }}
            className="px-3 py-1.5 rounded-lg text-xs text-dark-300 hover:text-dark-100 bg-dark-800 hover:bg-dark-700 border border-dark-700 transition-colors"
          >
            Отмена
          </button>
          <button
            onClick={handleSave}
            disabled={saving}
            className="px-3 py-1.5 rounded-lg text-xs font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors disabled:opacity-50 flex items-center gap-1.5"
          >
            {saving ? <Loader2 className="w-3 h-3 animate-spin" /> : <Save className="w-3 h-3" />}
            Сохранить
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
          <button
            onClick={() => setEditing(true)}
            className="p-1.5 rounded-lg text-dark-400 hover:text-dark-200 hover:bg-dark-800 transition-colors"
            title="Редактировать"
          >
            <Edit3 className="w-3.5 h-3.5" />
          </button>
        </div>
        <p className="text-sm text-dark-400 mt-1">{profile.description || 'Без описания'}</p>
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        <label
          className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs cursor-pointer transition-colors ${
            forceSync
              ? 'text-orange-300 bg-orange-500/10 border border-orange-500/30 hover:border-orange-500/50'
              : 'text-dark-300 bg-dark-800/50 border border-dark-700/50 hover:border-dark-600'
          }`}
          title="Применить даже если в правилах нет allow для порта API ноды (9100/tcp). Опасно — можно потерять связь с сервером. Включайте только когда default_incoming = allow или вы точно знаете, что 9100 разрешён иным способом."
        >
          <input type="checkbox" checked={forceSync} onChange={e => onForceChange(e.target.checked)} className="accent-orange-500" />
          {forceSync && <AlertTriangle className="w-3 h-3" />}
          Принудительно
        </label>
        <button
          onClick={onSyncAll}
          disabled={syncing || profile.servers.length === 0}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors disabled:opacity-50"
          title="Раскатать профиль на все привязанные серверы"
        >
          {syncing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
          Синхронизировать все
        </button>
        <button
          onClick={onClone}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs text-dark-300 hover:text-dark-100 bg-dark-800/50 hover:bg-dark-700/50 border border-dark-700/50 transition-colors"
          title="Создать копию профиля со всеми правилами"
        >
          <Copy className="w-3.5 h-3.5" /> Клонировать
        </button>
        <button
          onClick={onDelete}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs text-red-400 hover:text-red-300 bg-red-500/10 hover:bg-red-500/20 border border-red-500/20 transition-colors"
        >
          <Trash2 className="w-3.5 h-3.5" /> Удалить
        </button>
      </div>
    </div>
  )
}


function RulesTab({
  profile,
  rules,
  defaultIn,
  defaultOut,
  onDefaultChange,
  onAddRule,
  onUpdateRule,
  onDeleteRule,
}: {
  profile: FirewallProfileWithServers
  rules: FirewallProfileRuleData[]
  defaultIn: FirewallDefaultPolicy
  defaultOut: FirewallDefaultPolicy
  onDefaultChange: (patch: { default_incoming?: FirewallDefaultPolicy; default_outgoing?: FirewallDefaultPolicy }) => void
  onAddRule: (rule: FirewallProfileRuleData) => Promise<void>
  onUpdateRule: (index: number, rule: FirewallProfileRuleData) => Promise<void>
  onDeleteRule: (index: number) => Promise<void>
}) {
  const [showForm, setShowForm] = useState(false)
  const [editingIndex, setEditingIndex] = useState<number | null>(null)
  const [saving, setSaving] = useState(false)

  const handleAdd = async (rule: FirewallProfileRuleData) => {
    setSaving(true)
    try {
      await onAddRule(rule)
      setShowForm(false)
    } finally {
      setSaving(false)
    }
  }

  const handleUpdate = async (rule: FirewallProfileRuleData) => {
    if (editingIndex === null) return
    setSaving(true)
    try {
      await onUpdateRule(editingIndex, rule)
      setEditingIndex(null)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="space-y-4">
      {!profile.node_port_allowed && (
        <div className="flex items-start gap-3 p-3 rounded-xl bg-yellow-500/10 border border-yellow-500/30 text-yellow-300">
          <AlertTriangle className="w-5 h-5 shrink-0 mt-0.5" />
          <div className="text-sm">
            Нет правила allow для порта API ноды ({profile.node_api_port}/tcp) — после sync панель потеряет связь с серверами. Добавьте правило или применяйте с force=true только если уверены.
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <label className="block text-xs text-dark-400 mb-1">По умолчанию: входящий</label>
          <select
            value={defaultIn}
            onChange={e => onDefaultChange({ default_incoming: e.target.value as FirewallDefaultPolicy })}
            className={inputCls}
          >
            {POLICY_OPTIONS.map(p => <option key={p} value={p}>{POLICY_LABELS[p]}</option>)}
          </select>
        </div>
        <div>
          <label className="block text-xs text-dark-400 mb-1">По умолчанию: исходящий</label>
          <select
            value={defaultOut}
            onChange={e => onDefaultChange({ default_outgoing: e.target.value as FirewallDefaultPolicy })}
            className={inputCls}
          >
            {POLICY_OPTIONS.map(p => <option key={p} value={p}>{POLICY_LABELS[p]}</option>)}
          </select>
        </div>
      </div>

      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-dark-200">Правила ({rules.length})</h3>
        {!showForm && editingIndex === null && (
          <button
            onClick={() => setShowForm(true)}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors"
          >
            <Plus className="w-3.5 h-3.5" /> Добавить правило
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
          Нет правил
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-dark-800/50">
          <table className="w-full text-sm">
            <thead className="bg-dark-900/40 text-dark-400 text-xs">
              <tr>
                <th className="text-left px-3 py-2 font-medium">Действие</th>
                <th className="text-left px-3 py-2 font-medium">Порт</th>
                <th className="text-left px-3 py-2 font-medium">Протокол</th>
                <th className="text-left px-3 py-2 font-medium">Источник</th>
                <th className="text-left px-3 py-2 font-medium">Напр.</th>
                <th className="text-left px-3 py-2 font-medium">Комментарий</th>
                <th className="text-right px-3 py-2 font-medium">Действия</th>
              </tr>
            </thead>
            <tbody>
              {rules.map((rule, index) => {
                const actionColor = rule.action === 'allow' ? 'text-green-400' : 'text-red-400'
                return (
                  <Fragment key={index}>
                    <tr className="border-t border-dark-800/40 hover:bg-dark-800/30 transition-colors">
                      <td className={`px-3 py-2 font-medium ${actionColor}`}>{ACTION_LABELS[rule.action]}</td>
                      <td className="px-3 py-2 font-mono text-dark-200">{rule.port}</td>
                      <td className="px-3 py-2 text-dark-300">{rule.protocol}</td>
                      <td className="px-3 py-2 text-dark-300 font-mono">{rule.from_ip || 'любой'}</td>
                      <td className="px-3 py-2 text-dark-300">{DIRECTION_LABELS[rule.direction]}</td>
                      <td className="px-3 py-2 text-dark-400 truncate max-w-xs">{rule.comment || '—'}</td>
                      <td className="px-3 py-2">
                        <div className="flex items-center justify-end gap-1">
                          <button
                            onClick={() => { setEditingIndex(index); setShowForm(false) }}
                            className="p-1.5 rounded-lg text-dark-400 hover:text-dark-200 hover:bg-dark-700/50 transition-colors"
                            title="Редактировать"
                          >
                            <Edit3 className="w-3.5 h-3.5" />
                          </button>
                          <button
                            onClick={() => {
                              if (confirm('Удалить правило?')) void onDeleteRule(index)
                            }}
                            className="p-1.5 rounded-lg text-dark-400 hover:text-red-400 hover:bg-red-500/10 transition-colors"
                            title="Удалить"
                          >
                            <Trash2 className="w-3.5 h-3.5" />
                          </button>
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
                )
              })}
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
  forceSync,
  onSyncOne,
  onUnlink,
  onLink,
}: {
  profile: FirewallProfileWithServers
  availableServers: FirewallAvailableServer[]
  syncingServerId: number | null
  forceSync: boolean
  onSyncOne: (serverId: number) => void
  onUnlink: (serverId: number) => void
  onLink: (serverId: number) => void
}) {
  const linkedIds = useMemo(() => new Set(profile.servers.map(s => s.server_id)), [profile.servers])
  const candidates = availableServers.filter(s => !linkedIds.has(s.id))

  return (
    <div className="space-y-5">
      <div>
        <h3 className="text-sm font-medium text-dark-200 mb-3">
          Привязанные серверы ({profile.servers.length})
        </h3>
        {profile.servers.length === 0 ? (
          <div className="text-center text-dark-500 text-sm py-6 bg-dark-900/30 rounded-lg border border-dark-800/50">
            Нет привязанных серверов
          </div>
        ) : (
          <div className="space-y-1.5">
            {profile.servers.map((srv: FirewallProfileServerInfo) => (
              <div key={srv.server_id} className="flex items-center justify-between px-3 py-2 rounded-lg bg-dark-900/30 border border-dark-800/50">
                <div className="flex items-center gap-3 min-w-0">
                  <Server className="w-4 h-4 text-dark-400 shrink-0" />
                  <div className="min-w-0">
                    <div className="text-sm text-dark-200 truncate">{srv.server_name}</div>
                    <div className="text-xs text-dark-500 truncate">{srv.server_url}</div>
                  </div>
                  {syncStatusBadge(srv.sync_status)}
                </div>
                <div className="flex items-center gap-1 shrink-0">
                  <span className="hidden sm:inline text-xs text-dark-500 mr-2">
                    {srv.last_sync_at ? `Синхр: ${formatDateTime(srv.last_sync_at)}` : 'Не синхронизирован'}
                  </span>
                  <button
                    onClick={() => onSyncOne(srv.server_id)}
                    disabled={syncingServerId === srv.server_id}
                    className="p-1.5 rounded-lg text-dark-400 hover:text-accent-400 hover:bg-accent-500/10 transition-colors disabled:opacity-50"
                    title={forceSync ? 'Принудительная синхронизация' : 'Синхронизировать'}
                  >
                    {syncingServerId === srv.server_id ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
                  </button>
                  <button
                    onClick={() => {
                      if (confirm(`Отвязать ${srv.server_name}? Правила на ноде НЕ откатываются.`)) onUnlink(srv.server_id)
                    }}
                    className="p-1.5 rounded-lg text-dark-400 hover:text-red-400 hover:bg-red-500/10 transition-colors"
                    title="Отвязать"
                  >
                    <Unlink className="w-3.5 h-3.5" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div>
        <h3 className="text-sm font-medium text-dark-200 mb-3">Добавить серверы</h3>
        {candidates.length === 0 ? (
          <div className="text-center text-dark-500 text-sm py-6 bg-dark-900/30 rounded-lg border border-dark-800/50">
            Все доступные серверы уже привязаны
          </div>
        ) : (
          <div className="space-y-1.5">
            {candidates.map(srv => (
              <div key={srv.id} className="flex items-center justify-between px-3 py-2 rounded-lg bg-dark-900/30 border border-dark-800/50">
                <div className="flex items-center gap-3 min-w-0">
                  <Server className="w-4 h-4 text-dark-400 shrink-0" />
                  <div className="min-w-0">
                    <div className="text-sm text-dark-200 truncate">{srv.name}</div>
                    <div className="text-xs text-dark-500 truncate">{srv.url}</div>
                  </div>
                  {srv.active_profile_id && (
                    <span className="text-xs text-yellow-400/80 shrink-0">Уже в другом профиле</span>
                  )}
                </div>
                <button
                  onClick={() => onLink(srv.id)}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs text-accent-400 hover:text-accent-300 bg-accent-500/10 hover:bg-accent-500/20 transition-colors"
                >
                  <Link2 className="w-3.5 h-3.5" /> Привязать
                </button>
              </div>
            ))}
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
  log: FirewallSyncLogEntry[]
  loading: boolean
  onRefresh: () => void
}) {
  const statusColor = (status: string): string => {
    const lower = status.toLowerCase()
    if (lower === 'success' || lower === 'synced') return 'text-green-400'
    if (lower === 'failed' || lower === 'error') return 'text-red-400'
    if (lower === 'rolled_back') return 'text-orange-400'
    return 'text-yellow-400'
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-dark-200">История синхронизаций</h3>
        <button
          onClick={onRefresh}
          disabled={loading}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs text-dark-300 hover:text-dark-100 bg-dark-800/50 hover:bg-dark-700/50 border border-dark-700/50 transition-colors disabled:opacity-50"
        >
          {loading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
          Обновить
        </button>
      </div>
      {log.length === 0 ? (
        <div className="text-center text-dark-500 text-sm py-6 bg-dark-900/30 rounded-lg border border-dark-800/50">
          {loading ? 'Загрузка...' : 'История пуста'}
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-dark-800/50">
          <table className="w-full text-sm">
            <thead className="bg-dark-900/40 text-dark-400 text-xs">
              <tr>
                <th className="text-left px-3 py-2 font-medium">Сервер</th>
                <th className="text-left px-3 py-2 font-medium">Статус</th>
                <th className="text-left px-3 py-2 font-medium">Сообщение</th>
                <th className="text-left px-3 py-2 font-medium">Время</th>
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
  onProfileCloned: (clone: FirewallProfile) => void
}) {
  const [profile, setProfile] = useState<FirewallProfileWithServers | null>(null)
  const [loading, setLoading] = useState(true)
  const [tab, setTab] = useState<TabKey>('rules')
  const [availableServers, setAvailableServers] = useState<FirewallAvailableServer[]>([])
  const [log, setLog] = useState<FirewallSyncLogEntry[]>([])
  const [logLoading, setLogLoading] = useState(false)
  const [savingHeader, setSavingHeader] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [syncingServerId, setSyncingServerId] = useState<number | null>(null)
  const [forceSync, setForceSync] = useState(false)

  const fetchProfile = useCallback(async () => {
    try {
      const res = await firewallProfilesApi.get(profileId)
      setProfile(res.data)
    } catch (err) {
      toast.error(extractErrorMessage(err, 'Не удалось загрузить профиль'))
    } finally {
      setLoading(false)
    }
  }, [profileId])

  const refreshProfileSilent = useCallback(async () => {
    try {
      const res = await firewallProfilesApi.get(profileId)
      setProfile(res.data)
    } catch { /* silent */ }
  }, [profileId])

  const fetchAvailableServers = useCallback(async () => {
    try {
      const res = await firewallProfilesApi.getAvailableServers()
      setAvailableServers(res.data)
    } catch {
      // silent
    }
  }, [])

  const fetchLog = useCallback(async () => {
    setLogLoading(true)
    try {
      const res = await firewallProfilesApi.getLog(profileId)
      setLog(res.data)
    } catch (err) {
      toast.error(extractErrorMessage(err, 'Не удалось загрузить лог'))
    } finally {
      setLogLoading(false)
    }
  }, [profileId])

  useEffect(() => {
    setLoading(true)
    setTab('rules')
    fetchProfile()
    fetchAvailableServers()
  }, [profileId, fetchProfile, fetchAvailableServers])

  // Автообновление статусов серверов и счётчиков sync каждые 3 секунды
  useEffect(() => {
    const id = setInterval(refreshProfileSilent, 3000)
    return () => clearInterval(id)
  }, [refreshProfileSilent])

  useEffect(() => {
    if (tab === 'log') fetchLog()
  }, [tab, fetchLog])

  // Автообновление лога пока активен таб «История»
  useEffect(() => {
    if (tab !== 'log') return
    const id = setInterval(() => {
      firewallProfilesApi.getLog(profileId)
        .then(res => setLog(res.data))
        .catch(() => { /* silent */ })
    }, 3000)
    return () => clearInterval(id)
  }, [tab, profileId])

  const handleHeaderSave = async (patch: { name?: string; description?: string | null }) => {
    setSavingHeader(true)
    try {
      await firewallProfilesApi.update(profileId, patch)
      toast.success('Профиль обновлён')
      await fetchProfile()
      onProfileChanged()
    } catch (err) {
      toast.error(extractErrorMessage(err, 'Не удалось обновить'))
    } finally {
      setSavingHeader(false)
    }
  }

  const handleDelete = async () => {
    if (!profile) return
    if (!confirm(`Удалить профиль "${profile.name}"? Серверы будут отвязаны, правила на нодах останутся.`)) return
    try {
      await firewallProfilesApi.delete(profileId)
      toast.success('Профиль удалён')
      onProfileDeleted()
    } catch (err) {
      toast.error(extractErrorMessage(err, 'Не удалось удалить'))
    }
  }

  const handleClone = async () => {
    if (!profile) return
    const suggested = `${profile.name} (копия)`
    const name = prompt('Имя нового профиля:', suggested)
    if (name === null) return
    try {
      const res = await firewallProfilesApi.clone(profileId, name.trim() || undefined)
      toast.success(`Создан профиль "${res.data.name}"`)
      onProfileCloned(res.data)
    } catch (err) {
      toast.error(extractErrorMessage(err, 'Не удалось клонировать'))
    }
  }

  const handleSyncAll = async () => {
    setSyncing(true)
    try {
      const res = await firewallProfilesApi.syncAll(profileId, forceSync)
      const results = res.data.results
      const ok = results.filter(r => r.success).length
      const fail = results.length - ok
      if (fail === 0) toast.success(`Синхронизация успешна: ${ok}/${results.length}`)
      else toast.warning(`Синхронизация: ${ok} успешно, ${fail} с ошибками`)
      await fetchProfile()
      onProfileChanged()
    } catch (err) {
      toast.error(extractErrorMessage(err, 'Синхронизация не удалась'))
    } finally {
      setSyncing(false)
    }
  }

  const handleSyncOne = async (serverId: number) => {
    setSyncingServerId(serverId)
    try {
      const res = await firewallProfilesApi.syncOne(profileId, serverId, forceSync)
      if (res.data.success) toast.success(`${res.data.server_name}: ${res.data.message}`)
      else toast.error(`${res.data.server_name}: ${res.data.message}`)
      await fetchProfile()
      onProfileChanged()
    } catch (err) {
      toast.error(extractErrorMessage(err, 'Синхронизация не удалась'))
    } finally {
      setSyncingServerId(null)
    }
  }

  const handleLink = async (serverId: number) => {
    try {
      await firewallProfilesApi.linkServer(profileId, serverId)
      toast.success('Сервер привязан')
      await Promise.all([fetchProfile(), fetchAvailableServers()])
      onProfileChanged()
    } catch (err) {
      toast.error(extractErrorMessage(err, 'Не удалось привязать'))
    }
  }

  const handleUnlink = async (serverId: number) => {
    try {
      await firewallProfilesApi.unlinkServer(profileId, serverId)
      toast.success('Сервер отвязан')
      await Promise.all([fetchProfile(), fetchAvailableServers()])
      onProfileChanged()
    } catch (err) {
      toast.error(extractErrorMessage(err, 'Не удалось отвязать'))
    }
  }

  const handleDefaultChange = async (patch: { default_incoming?: FirewallDefaultPolicy; default_outgoing?: FirewallDefaultPolicy }) => {
    if (!profile) return
    const merged: FirewallProfileWithServers = { ...profile, ...patch }
    merged.node_port_allowed = computeNodePortAllowed(merged.rules, merged.default_incoming, merged.node_api_port)
    setProfile(merged)
    try {
      await firewallProfilesApi.update(profileId, patch)
      onProfileChanged()
    } catch (err) {
      toast.error(extractErrorMessage(err, 'Не удалось обновить'))
      await fetchProfile()
    }
  }

  const handleAddRule = async (rule: FirewallProfileRuleData) => {
    try {
      const res = await firewallProfilesApi.addRule(profileId, rule)
      toast.success('Правило добавлено')
      setProfile(prev => prev ? {
        ...prev,
        rules: res.data.rules,
        node_port_allowed: computeNodePortAllowed(res.data.rules, prev.default_incoming, prev.node_api_port),
      } : prev)
      onProfileChanged()
    } catch (err) {
      toast.error(extractErrorMessage(err, 'Не удалось добавить правило'))
    }
  }

  const handleUpdateRule = async (index: number, rule: FirewallProfileRuleData) => {
    try {
      const res = await firewallProfilesApi.updateRule(profileId, index, rule)
      toast.success('Правило обновлено')
      setProfile(prev => prev ? {
        ...prev,
        rules: res.data.rules,
        node_port_allowed: computeNodePortAllowed(res.data.rules, prev.default_incoming, prev.node_api_port),
      } : prev)
      onProfileChanged()
    } catch (err) {
      toast.error(extractErrorMessage(err, 'Не удалось обновить правило'))
    }
  }

  const handleDeleteRule = async (index: number) => {
    try {
      const res = await firewallProfilesApi.deleteRule(profileId, index)
      toast.success('Правило удалено')
      setProfile(prev => prev ? {
        ...prev,
        rules: res.data.rules,
        node_port_allowed: computeNodePortAllowed(res.data.rules, prev.default_incoming, prev.node_api_port),
      } : prev)
      onProfileChanged()
    } catch (err) {
      toast.error(extractErrorMessage(err, 'Не удалось удалить правило'))
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
    return <div className="text-center text-dark-500 py-12">Профиль не найден</div>
  }

  const tabs: { key: TabKey; label: string; icon: React.ReactNode }[] = [
    { key: 'rules', label: `Правила (${profile.rules.length})`, icon: <ListChecks className="w-3.5 h-3.5" /> },
    { key: 'servers', label: `Серверы (${profile.servers.length})`, icon: <Server className="w-3.5 h-3.5" /> },
    { key: 'log', label: 'История', icon: <History className="w-3.5 h-3.5" /> },
  ]

  return (
    <div className="space-y-5">
      <ProfileHeader
        profile={profile}
        saving={savingHeader}
        syncing={syncing}
        forceSync={forceSync}
        onForceChange={setForceSync}
        onSyncAll={handleSyncAll}
        onClone={handleClone}
        onDelete={handleDelete}
        onSave={handleHeaderSave}
      />

      <div className="flex items-center gap-1 border-b border-dark-700/60">
        {tabs.map(t => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`flex items-center gap-1.5 px-4 py-2 text-sm border-b-2 transition-colors -mb-px ${
              tab === t.key
                ? 'text-accent-400 border-accent-400'
                : 'text-dark-400 border-transparent hover:text-dark-200'
            }`}
          >
            {t.icon} {t.label}
          </button>
        ))}
      </div>

      {tab === 'rules' && (
        <RulesTab
          profile={profile}
          rules={profile.rules}
          defaultIn={profile.default_incoming}
          defaultOut={profile.default_outgoing}
          onDefaultChange={handleDefaultChange}
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
          forceSync={forceSync}
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


export default function FirewallProfiles() {
  const [profiles, setProfiles] = useState<FirewallProfile[]>([])
  const [loading, setLoading] = useState(true)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [showCreate, setShowCreate] = useState(false)
  const initialLoadDone = useRef(false)

  const fetchProfiles = useCallback(async () => {
    try {
      const res = await firewallProfilesApi.list()
      setProfiles(res.data)
      setSelectedId(prev => {
        if (prev !== null && res.data.some(p => p.id === prev)) return prev
        return res.data[0]?.id ?? null
      })
    } catch (err) {
      if (!initialLoadDone.current) toast.error(extractErrorMessage(err, 'Не удалось загрузить профили'))
    } finally {
      if (!initialLoadDone.current) {
        initialLoadDone.current = true
        setLoading(false)
      }
    }
  }, [])

  useEffect(() => {
    fetchProfiles()
  }, [fetchProfiles])

  // Автообновление списка профилей (счётчики synced/linked) каждые 3 секунды
  useEffect(() => {
    const id = setInterval(fetchProfiles, 3000)
    return () => clearInterval(id)
  }, [fetchProfiles])

  const handleCreated = (profile: FirewallProfile) => {
    setShowCreate(false)
    setProfiles(prev => [...prev, profile])
    setSelectedId(profile.id)
  }

  const handleDeleted = async () => {
    setSelectedId(null)
    await fetchProfiles()
  }

  const handleCloned = async (clone: FirewallProfile) => {
    await fetchProfiles()
    setSelectedId(clone.id)
  }

  return (
    <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="space-y-6">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-accent-500/20 to-accent-600/20 flex items-center justify-center border border-accent-500/20">
            <Flame className="w-5 h-5 text-accent-400" />
          </div>
          <div>
            <h1 className="text-2xl font-bold text-dark-100 flex items-center gap-2">
              Firewall профили
              <FAQIcon screen="PAGE_FIREWALL_PROFILES" />
            </h1>
            <p className="text-sm text-dark-400">Централизованное управление правилами фаервола на нодах</p>
          </div>
        </div>
        <button
          onClick={() => setShowCreate(true)}
          className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors"
        >
          <Plus className="w-4 h-4" /> Создать профиль
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
              Профилей пока нет.
              <br />Создайте первый через кнопку «Создать профиль».
            </div>
          ) : (
            <AnimatePresence>
              {profiles.map(p => (
                <ProfileListItem
                  key={p.id}
                  profile={p}
                  selected={p.id === selectedId}
                  onSelect={setSelectedId}
                />
              ))}
            </AnimatePresence>
          )}
        </div>

        <div className="card">
          {selectedId === null ? (
            <div className="flex flex-col items-center justify-center py-16 text-dark-500">
              <Flame className="w-10 h-10 mb-3 text-dark-600" />
              <p className="text-sm">Выберите профиль слева</p>
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
