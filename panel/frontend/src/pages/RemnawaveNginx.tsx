import { useEffect, useState, useCallback, useRef } from 'react'
import { useNodeCapabilities } from '../hooks/useNodeCapabilities'
import { Waypoints, Plus, RefreshCw, Trash2, Server, ChevronDown, ChevronRight, Edit3, Link2, Unlink, Loader2, CheckCircle2, XCircle, AlertCircle, Clock, History, X, Code, Save, AlertTriangle, Activity, Globe, Lock, RotateCw, Download, Settings2, Power } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { motion, AnimatePresence } from 'framer-motion'
import { toast } from 'sonner'
import {
  remnawaveNginxApi,
  RemnawaveNginxProfile,
  RemnawaveNginxProfileDetail,
  RemnawaveNginxRule,
  RemnawaveNginxOptions,
  RemnawaveNginxSyncResult,
  RemnawaveNginxSyncLogEntry,
  RemnawaveNginxAvailableServer,
  RemnawaveNginxServerStatus,
  RemnawaveNginxNodeStatus,
} from '../api/client'
import { Tooltip } from '../components/ui/Tooltip'
import { FAQIcon } from '../components/FAQ'

const DEFAULT_CLIENT_KEEPALIVE = '30s:10s:3'

function SyncStatusBadge({ status, online }: { status: string | null; online?: boolean }) {
  const { t } = useTranslation()
  if (!status) return null

  if (status === 'pending' && online === false) {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs border text-dark-300 bg-dark-700/40 border-dark-600/40">
        <Clock className="w-3 h-3" /> {t('remnawave_nginx.waiting_server')}
      </span>
    )
  }

  const map: Record<string, { color: string; icon: React.ReactNode; label: string }> = {
    synced: { color: 'text-green-400 bg-green-500/10 border-green-500/20', icon: <CheckCircle2 className="w-3 h-3" />, label: t('remnawave_nginx.synced') },
    pending: { color: 'text-yellow-400 bg-yellow-500/10 border-yellow-500/20', icon: <Clock className="w-3 h-3" />, label: t('remnawave_nginx.pending') },
    failed: { color: 'text-red-400 bg-red-500/10 border-red-500/20', icon: <XCircle className="w-3 h-3" />, label: t('remnawave_nginx.failed') },
  }
  const s = map[status] || map.pending
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs border ${s.color}`}>
      {s.icon} {s.label}
    </span>
  )
}

function Toggle({ value, onChange, label }: { value: boolean; onChange: (v: boolean) => void; label?: string }) {
  return (
    <div className="flex items-center gap-2">
      {label && <span className="text-xs text-dark-300">{label}</span>}
      <button type="button" onClick={() => onChange(!value)}
        className={`relative w-9 h-5 rounded-full transition-colors duration-200 ${value ? 'bg-green-500' : 'bg-dark-600'}`}>
        <span className={`absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full shadow transition-transform duration-200 ${value ? 'translate-x-4' : 'translate-x-0'}`} />
      </button>
    </div>
  )
}

// ==================== Rule Form ====================

type RuleType = 'grpc' | 'xhttp' | 'proxy'

interface RuleFormData {
  name: string
  rule_type: RuleType
  service_path: string
  port: string
  path: string
  target_url: string
}

const EMPTY_RULE_FORM: RuleFormData = {
  name: '', rule_type: 'grpc', service_path: '', port: '', path: '/', target_url: '',
}

function RuleForm({
  initial, isEdit, saving, onSave, onCancel,
}: {
  initial: RuleFormData
  isEdit: boolean
  saving: boolean
  onSave: (data: RuleFormData) => void
  onCancel: () => void
}) {
  const { t } = useTranslation()
  const [form, setForm] = useState(initial)
  const inp = "w-full px-3 py-1.5 rounded-lg bg-dark-800 border border-dark-700 text-dark-100 text-sm focus:outline-none focus:border-accent-500/50"

  return (
    <motion.div
      className="p-4 bg-dark-800/50 rounded-xl border border-dark-700/50"
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      exit={{ opacity: 0, height: 0 }}
    >
      <div className="flex items-center justify-between mb-3">
        <h4 className="text-sm font-medium text-dark-200 flex items-center gap-2">
          {isEdit ? <><Edit3 className="w-3.5 h-3.5 text-accent-500" /> {t('remnawave_nginx.edit_rule')}</> : <><Plus className="w-3.5 h-3.5 text-accent-500" /> {t('remnawave_nginx.new_rule')}</>}
        </h4>
        <button onClick={onCancel} className="p-1 hover:bg-dark-700 rounded-lg text-dark-400 transition-colors">
          <X className="w-4 h-4" />
        </button>
      </div>

      <div className="space-y-3">
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-xs text-dark-400 mb-1">{t('common.name')}</label>
            <input type="text" value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
              placeholder="trgrpc" className={inp} disabled={isEdit} />
          </div>
          <div>
            <label className="block text-xs text-dark-400 mb-1">{t('remnawave_nginx.rule_type')}</label>
            <select value={form.rule_type} onChange={e => {
              const rule_type = e.target.value as RuleType
              // Дефолтный «/» подходит proxy-правилу, но XHTTP-путь всегда длиннее
              setForm(f => ({ ...f, rule_type, path: rule_type === 'xhttp' && f.path === '/' ? '' : f.path }))
            }} className={inp}>
              <option value="grpc">{t('remnawave_nginx.rule_type_grpc')}</option>
              <option value="xhttp">{t('remnawave_nginx.rule_type_xhttp')}</option>
              <option value="proxy">{t('remnawave_nginx.rule_type_proxy')}</option>
            </select>
          </div>
        </div>

        {form.rule_type === 'grpc' ? (
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-dark-400 mb-1">{t('remnawave_nginx.service_path')}</label>
              <input type="text" value={form.service_path} onChange={e => setForm(f => ({ ...f, service_path: e.target.value }))}
                placeholder="trgrpc" className={inp} />
              <p className="text-[10px] text-dark-500 mt-0.5">{t('remnawave_nginx.service_path_hint')}</p>
            </div>
            <div>
              <label className="block text-xs text-dark-400 mb-1">{t('remnawave_nginx.xray_port')}</label>
              <input type="number" value={form.port} onChange={e => setForm(f => ({ ...f, port: e.target.value }))}
                placeholder="8443" className={inp} />
              <p className="text-[10px] text-dark-500 mt-0.5">{t('remnawave_nginx.xray_port_hint')}</p>
            </div>
          </div>
        ) : form.rule_type === 'xhttp' ? (
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-dark-400 mb-1">{t('remnawave_nginx.xhttp_path')}</label>
              <input type="text" value={form.path} onChange={e => setForm(f => ({ ...f, path: e.target.value }))}
                placeholder="/api/v2/upload" className={inp} />
              <p className="text-[10px] text-dark-500 mt-0.5">{t('remnawave_nginx.xhttp_path_hint')}</p>
            </div>
            <div>
              <label className="block text-xs text-dark-400 mb-1">{t('remnawave_nginx.xray_port')}</label>
              <input type="number" value={form.port} onChange={e => setForm(f => ({ ...f, port: e.target.value }))}
                placeholder="2081" className={inp} />
              <p className="text-[10px] text-dark-500 mt-0.5">{t('remnawave_nginx.xray_port_hint')}</p>
            </div>
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-dark-400 mb-1">{t('remnawave_nginx.location_path')}</label>
              <input type="text" value={form.path} onChange={e => setForm(f => ({ ...f, path: e.target.value }))}
                placeholder="/" className={inp} />
            </div>
            <div>
              <label className="block text-xs text-dark-400 mb-1">{t('remnawave_nginx.target_url')}</label>
              <input type="text" value={form.target_url} onChange={e => setForm(f => ({ ...f, target_url: e.target.value }))}
                placeholder="https://1.2.3.4:8445" className={inp} />
            </div>
          </div>
        )}

        <div className="flex justify-end gap-2 pt-1">
          <button onClick={onCancel} className="px-3 py-1.5 rounded-lg text-xs text-dark-300 hover:text-dark-100 bg-dark-800 hover:bg-dark-700 border border-dark-700 transition-colors">
            {t('common.cancel')}
          </button>
          <button onClick={() => onSave(form)} disabled={saving}
            className="px-3 py-1.5 rounded-lg text-xs font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors disabled:opacity-50 flex items-center gap-1.5">
            {saving && <Loader2 className="w-3 h-3 animate-spin" />}
            {isEdit ? t('common.save') : t('remnawave_nginx.add_rule')}
          </button>
        </div>
      </div>
    </motion.div>
  )
}

// ==================== Real IP Options Section ====================

function OptionsSection({
  profileId, initial, onSaved,
}: {
  profileId: number
  initial: RemnawaveNginxOptions
  onSaved: (config: string) => void
}) {
  const { t } = useTranslation()
  const [opts, setOpts] = useState(initial)
  const [cdnRangesText, setCdnRangesText] = useState(initial.cdn_ranges.join('\n'))
  const [saving, setSaving] = useState(false)
  const upd = (patch: Partial<RemnawaveNginxOptions>) => setOpts(o => ({ ...o, ...patch }))
  const inp = "w-full px-3 py-1.5 rounded-lg bg-dark-800 border border-dark-700 text-dark-100 text-sm focus:outline-none focus:border-accent-500/50"

  const loadCloudflare = async () => {
    try {
      const res = await remnawaveNginxApi.getCloudflareRanges()
      setCdnRangesText(res.data.ranges.join('\n'))
      toast.success(t('remnawave_nginx.cloudflare_loaded'))
    } catch {
      toast.error(t('remnawave_nginx.fetch_error'))
    }
  }

  const handleSave = async () => {
    setSaving(true)
    try {
      const ranges = cdnRangesText.split('\n').map(s => s.trim()).filter(Boolean)
      const res = await remnawaveNginxApi.updateOptions(profileId, { ...opts, cdn_ranges: ranges })
      toast.success(t('remnawave_nginx.options_saved'))
      onSaved(res.data.config_content)
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('remnawave_nginx.save_error'))
    } finally { setSaving(false) }
  }

  return (
    <div className="p-4 bg-dark-900/40 rounded-xl border border-dark-700/40 space-y-4">
      <p className="text-xs text-dark-500">{t('remnawave_nginx.options_hint')}</p>

      <div className="flex items-start gap-2 px-3 py-2 rounded-lg text-xs text-yellow-400 bg-yellow-500/10 border border-yellow-500/20">
        <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" /> {t('remnawave_nginx.xray_requirements')}
      </div>

      {/* Fallback: мусорный трафик и ошибки Xray → обычный сайт */}
      <div>
        <label className="block text-xs text-dark-400 mb-1">{t('remnawave_nginx.fallback_url')}</label>
        <input type="text" value={opts.fallback_url} onChange={e => upd({ fallback_url: e.target.value })}
          placeholder="https://example.com" className={`${inp} font-mono text-xs`} spellCheck={false} />
        <p className="text-[10px] text-dark-500 mt-0.5">{t('remnawave_nginx.fallback_url_hint')}</p>
      </div>

      {/* CDN */}
      <div className="space-y-2">
        <Toggle value={opts.cdn_enabled} onChange={v => upd({ cdn_enabled: v })} label={t('remnawave_nginx.cdn_enabled')} />
        {opts.cdn_enabled && (
          <div className="pl-2 border-l-2 border-dark-700/40 space-y-2">
            <div className="flex items-center justify-between">
              <label className="text-xs text-dark-400">{t('remnawave_nginx.cdn_ranges')}</label>
              <button type="button" onClick={loadCloudflare}
                className="text-xs text-accent-400 hover:text-accent-300 transition-colors">
                {t('remnawave_nginx.cloudflare_defaults')}
              </button>
            </div>
            <textarea value={cdnRangesText} onChange={e => setCdnRangesText(e.target.value)} spellCheck={false}
              placeholder={'173.245.48.0/20\n103.21.244.0/22'} rows={5}
              className="w-full px-3 py-2 rounded-lg bg-dark-950 border border-dark-700 text-dark-200 text-xs font-mono focus:outline-none focus:border-accent-500/50 resize-y" />
            <p className="text-[10px] text-dark-500">{t('remnawave_nginx.cdn_ranges_hint')}</p>
          </div>
        )}
      </div>

      {/* PROXY protocol */}
      <div className="space-y-2">
        <Toggle value={opts.proxy_protocol_enabled} onChange={v => upd({ proxy_protocol_enabled: v })} label={t('remnawave_nginx.pp_enabled')} />
        {opts.proxy_protocol_enabled && (
          <div className="pl-2 border-l-2 border-dark-700/40 grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-dark-400 mb-1">{t('remnawave_nginx.pp_port')}</label>
              <input type="number" value={opts.proxy_protocol_port} onChange={e => upd({ proxy_protocol_port: parseInt(e.target.value) || 8449 })}
                className={inp} />
            </div>
            <div>
              <label className="block text-xs text-dark-400 mb-1">{t('remnawave_nginx.haproxy_ip')}</label>
              <input type="text" value={opts.haproxy_ip} onChange={e => upd({ haproxy_ip: e.target.value })}
                placeholder="10.0.0.1" className={inp} />
            </div>
            <p className="text-[10px] text-dark-500 col-span-2">{t('remnawave_nginx.pp_hint')}</p>
          </div>
        )}
      </div>

      {/* Прочее */}
      <div className="space-y-1">
        <div className="flex flex-wrap gap-x-6 gap-y-2">
          <Toggle value={opts.http_redirect_enabled} onChange={v => upd({ http_redirect_enabled: v })} label={t('remnawave_nginx.http_redirect')} />
          <Toggle value={opts.acme_enabled} onChange={v => upd({ acme_enabled: v })} label={t('remnawave_nginx.acme')} />
          <Toggle value={opts.reject_default_server} onChange={v => upd({ reject_default_server: v })} label={t('remnawave_nginx.reject_default')} />
        </div>
        <p className="text-[10px] text-dark-500">{t('remnawave_nginx.acme_hint')}</p>
      </div>

      {/* TLS и соединения */}
      <div className="space-y-2">
        <Toggle value={opts.tls_session_tickets} onChange={v => upd({ tls_session_tickets: v })} label={t('remnawave_nginx.tls_session_tickets')} />
        <p className="text-[10px] text-dark-500">{t('remnawave_nginx.tls_session_tickets_hint')}</p>
        <Toggle value={opts.client_tcp_keepalive !== ''} onChange={v => upd({ client_tcp_keepalive: v ? DEFAULT_CLIENT_KEEPALIVE : '' })} label={t('remnawave_nginx.client_keepalive')} />
        {opts.client_tcp_keepalive !== '' && (
          <div className="pl-2 border-l-2 border-dark-700/40">
            <label className="block text-xs text-dark-400 mb-1">{t('remnawave_nginx.client_keepalive_value')}</label>
            <input type="text" value={opts.client_tcp_keepalive} onChange={e => upd({ client_tcp_keepalive: e.target.value })}
              placeholder={DEFAULT_CLIENT_KEEPALIVE} className={`${inp} font-mono text-xs`} spellCheck={false} />
          </div>
        )}
        <p className="text-[10px] text-dark-500">{t('remnawave_nginx.client_keepalive_hint')}</p>
        <Toggle value={opts.access_log_enabled} onChange={v => upd({ access_log_enabled: v })} label={t('remnawave_nginx.access_log')} />
        <p className="text-[10px] text-dark-500">{t('remnawave_nginx.access_log_hint')}</p>
      </div>

      {/* Wildcard-домен: один на все ноды профиля */}
      <div className="space-y-1.5">
        <label className="block text-xs text-dark-400">{t('remnawave_nginx.wildcard_domain')}</label>
        <input type="text" value={opts.wildcard_domain} onChange={e => upd({ wildcard_domain: e.target.value })}
          placeholder="example.com" className={`${inp} font-mono text-xs`} spellCheck={false} />
        <p className="text-[10px] text-dark-500">{t('remnawave_nginx.wildcard_domain_hint')}</p>
        {opts.wildcard_domain.trim() !== '' && (
          <div className="flex items-start gap-2 px-3 py-2 rounded-lg text-xs text-yellow-400 bg-yellow-500/10 border border-yellow-500/20">
            <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
            {t('remnawave_nginx.wildcard_domain_warning', { domain: opts.wildcard_domain.trim() })}
          </div>
        )}
      </div>

      {/* Пути сертификатов */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <label className="block text-xs text-dark-400 mb-1">{t('remnawave_nginx.ssl_cert_path')}</label>
          <input type="text" value={opts.ssl_cert_path} onChange={e => upd({ ssl_cert_path: e.target.value })}
            className={`${inp} font-mono text-xs`} spellCheck={false} />
        </div>
        <div>
          <label className="block text-xs text-dark-400 mb-1">{t('remnawave_nginx.ssl_key_path')}</label>
          <input type="text" value={opts.ssl_key_path} onChange={e => upd({ ssl_key_path: e.target.value })}
            className={`${inp} font-mono text-xs`} spellCheck={false} />
        </div>
      </div>

      <div className="flex items-center justify-between pt-1">
        <div className="flex items-center gap-1.5 text-xs text-dark-500">
          <AlertTriangle className="w-3.5 h-3.5" /> {t('remnawave_nginx.options_rebuild_warning')}
        </div>
        <button onClick={handleSave} disabled={saving}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors disabled:opacity-50">
          {saving ? <Loader2 className="w-3 h-3 animate-spin" /> : <Save className="w-3 h-3" />} {t('remnawave_nginx.apply_options')}
        </button>
      </div>
    </div>
  )
}

// ==================== Profile Card ====================

function ProfileCard({
  profile, expanded, onExpand, onEdit, onDelete,
}: {
  profile: RemnawaveNginxProfile; expanded: boolean
  onExpand: (id: number) => void; onEdit: (p: RemnawaveNginxProfile) => void; onDelete: (id: number) => void
}) {
  const { t } = useTranslation()
  const allSynced = profile.linked_servers_count > 0 && profile.synced_servers_count === profile.linked_servers_count
  const hasUnsync = profile.linked_servers_count > 0 && profile.synced_servers_count < profile.linked_servers_count

  return (
    <motion.div layout initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -10 }}
      className="rounded-xl border bg-dark-800/60 border-dark-700/80 transition-all duration-200 hover:border-dark-600/80">
      <div className="flex items-center justify-between px-4 py-3 cursor-pointer select-none" onClick={() => onExpand(profile.id)}>
        <div className="flex items-center gap-3 min-w-0">
          {expanded ? <ChevronDown className="w-4 h-4 text-dark-400 shrink-0" /> : <ChevronRight className="w-4 h-4 text-dark-400 shrink-0" />}
          <Waypoints className="w-5 h-5 text-accent-400 shrink-0" />
          <div className="min-w-0">
            <div className="font-medium text-dark-100 truncate">{profile.name}</div>
            {profile.description && <div className="text-xs text-dark-400 truncate mt-0.5">{profile.description}</div>}
          </div>
        </div>
        <div className="flex items-center gap-3 shrink-0">
          <div className="flex items-center gap-1.5 text-xs text-dark-400"><Server className="w-3.5 h-3.5" /><span>{profile.linked_servers_count}</span></div>
          {allSynced && (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs border text-green-400 bg-green-500/10 border-green-500/20">
              <CheckCircle2 className="w-3 h-3" /> {t('remnawave_nginx.all_synced')}
            </span>
          )}
          {hasUnsync && (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs border text-yellow-400 bg-yellow-500/10 border-yellow-500/20">
              <AlertCircle className="w-3 h-3" /> {profile.linked_servers_count - profile.synced_servers_count} {t('remnawave_nginx.out_of_sync')}
            </span>
          )}
          <Tooltip label={t('common.edit')}>
            <button onClick={e => { e.stopPropagation(); onEdit(profile) }} className="p-1.5 rounded-lg text-dark-400 hover:text-dark-200 hover:bg-dark-700/50 transition-colors"><Edit3 className="w-4 h-4" /></button>
          </Tooltip>
          <Tooltip label={t('common.delete')}>
            <button onClick={e => { e.stopPropagation(); onDelete(profile.id) }} className="p-1.5 rounded-lg text-dark-400 hover:text-red-400 hover:bg-red-500/10 transition-colors"><Trash2 className="w-4 h-4" /></button>
          </Tooltip>
        </div>
      </div>
    </motion.div>
  )
}

// ==================== Profile Detail Panel ====================

function ProfileDetailPanel({ profileId, onRefreshList }: { profileId: number; onRefreshList: () => void }) {
  const { t } = useTranslation()
  const [detail, setDetail] = useState<RemnawaveNginxProfileDetail | null>(null)
  const [rules, setRules] = useState<RemnawaveNginxRule[]>([])
  const [hasMarkers, setHasMarkers] = useState(true)
  const wildcardDomain = detail?.options.wildcard_domain || ''
  const [availableServers, setAvailableServers] = useState<RemnawaveNginxAvailableServer[]>([])
  const [serversStatus, setServersStatus] = useState<RemnawaveNginxServerStatus[]>([])
  const [nodeStatuses, setNodeStatuses] = useState<Record<number, RemnawaveNginxNodeStatus>>({})
  const [syncLog, setSyncLog] = useState<RemnawaveNginxSyncLogEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [syncing, setSyncing] = useState(false)
  const [syncingServerId, setSyncingServerId] = useState<number | null>(null)
  const [restartingServerId, setRestartingServerId] = useState<number | null>(null)
  const [showLog, setShowLog] = useState(false)
  const [showAddServer, setShowAddServer] = useState(false)
  const [linkingServer, setLinkingServer] = useState<RemnawaveNginxAvailableServer | null>(null)
  const [linkDomain, setLinkDomain] = useState('')
  const [editingDomainId, setEditingDomainId] = useState<number | null>(null)
  const [domainEdit, setDomainEdit] = useState('')
  const [showOptions, setShowOptions] = useState(false)
  const [showRuleForm, setShowRuleForm] = useState(false)
  const [editingRules, setEditingRules] = useState<Set<string>>(new Set())
  const [ruleSaving, setRuleSaving] = useState(false)
  const [showConfig, setShowConfig] = useState(false)
  const [configEdit, setConfigEdit] = useState('')
  const [configSaving, setConfigSaving] = useState(false)
  const [configValidating, setConfigValidating] = useState(false)
  const [showImport, setShowImport] = useState(false)
  const [importing, setImporting] = useState(false)
  const [serverSearch, setServerSearch] = useState('')
  const [configModalMouseDown, setConfigModalMouseDown] = useState(false)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const prevSyncedRef = useRef<string>('')
  const { allows } = useNodeCapabilities()

  const fetchNodeStatuses = useCallback(async (servers: { server_id: number; online: boolean }[]) => {
    // Опрос идёт каждые три секунды: закрытая нода иначе давала бы двадцать
    // отказов в минуту на пустом месте
    const online = servers.filter(s => s.online && allows(s.server_id, 'remnawave'))
    const results = await Promise.allSettled(online.map(s => remnawaveNginxApi.getNodeStatus(s.server_id)))
    setNodeStatuses(prev => {
      const next = { ...prev }
      online.forEach((s, i) => {
        const r = results[i]
        if (r.status === 'fulfilled') next[s.server_id] = r.value.data
      })
      return next
    })
  }, [allows])

  const fetchServersStatus = useCallback(async () => {
    try {
      const res = await remnawaveNginxApi.getServersStatus(profileId)
      setServersStatus(res.data)

      const syncKey = res.data.map((s: RemnawaveNginxServerStatus) => `${s.server_id}:${s.sync_status}`).join(',')
      if (prevSyncedRef.current && prevSyncedRef.current !== syncKey) {
        onRefreshList()
      }
      prevSyncedRef.current = syncKey
    } catch { /* silent */ }
  }, [profileId, onRefreshList])

  const fetchDetail = useCallback(async () => {
    try {
      const [detailRes, serversRes, rulesRes, statusRes] = await Promise.all([
        remnawaveNginxApi.getProfile(profileId),
        remnawaveNginxApi.getAvailableServers(),
        remnawaveNginxApi.getRules(profileId),
        remnawaveNginxApi.getServersStatus(profileId),
      ])
      setDetail(detailRes.data)
      setAvailableServers(serversRes.data)
      setRules(rulesRes.data.rules)
      setHasMarkers(rulesRes.data.has_markers)
      setConfigEdit(detailRes.data.config_content)
      setServersStatus(statusRes.data)
      fetchNodeStatuses(statusRes.data)
    } catch {
      toast.error(t('remnawave_nginx.fetch_error'))
    } finally {
      setLoading(false)
    }
  }, [profileId, t, fetchNodeStatuses])

  useEffect(() => { fetchDetail() }, [fetchDetail])

  // Поллинг статусов синка каждые 3 секунды (без обращений к нодам)
  useEffect(() => {
    pollRef.current = setInterval(() => { if (!document.hidden) fetchServersStatus() }, 3000)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [fetchServersStatus])

  // ---- Rules CRUD ----
  const buildRulePayload = (form: RuleFormData): RemnawaveNginxRule => {
    if (form.rule_type === 'grpc') {
      return { name: form.name, rule_type: 'grpc', service_path: form.service_path, port: parseInt(form.port) || 0 }
    }
    if (form.rule_type === 'xhttp') {
      return { name: form.name, rule_type: 'xhttp', path: form.path, port: parseInt(form.port) || 0 }
    }
    return { name: form.name, rule_type: 'proxy', path: form.path, target_url: form.target_url }
  }

  const isRuleFormIncomplete = (form: RuleFormData): boolean => {
    if (!form.name) return true
    if (form.rule_type === 'grpc') return !form.service_path || !form.port
    if (form.rule_type === 'xhttp') return !form.path || !form.port
    return !form.path || !form.target_url
  }

  const applyRulesResponse = (data: { rules: RemnawaveNginxRule[]; has_markers: boolean }) => {
    setRules(data.rules)
    setHasMarkers(data.has_markers)
  }

  const handleAddRule = async (form: RuleFormData) => {
    if (isRuleFormIncomplete(form)) {
      toast.error(t('remnawave_nginx.rule_fields_required')); return
    }
    setRuleSaving(true)
    try {
      const res = await remnawaveNginxApi.addRule(profileId, buildRulePayload(form))
      applyRulesResponse(res.data)
      setShowRuleForm(false)
      toast.success(t('remnawave_nginx.rule_added'))
      fetchDetail(); onRefreshList()
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('remnawave_nginx.rule_error'))
    } finally { setRuleSaving(false) }
  }

  const handleUpdateRule = async (form: RuleFormData) => {
    setRuleSaving(true)
    try {
      const res = await remnawaveNginxApi.updateRule(profileId, form.name, buildRulePayload(form))
      applyRulesResponse(res.data)
      setEditingRules(prev => { const next = new Set(prev); next.delete(form.name); return next })
      toast.success(t('remnawave_nginx.rule_updated'))
      fetchDetail(); onRefreshList()
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('remnawave_nginx.rule_error'))
    } finally { setRuleSaving(false) }
  }

  const handleDeleteRule = async (ruleName: string) => {
    try {
      const res = await remnawaveNginxApi.deleteRule(profileId, ruleName)
      applyRulesResponse(res.data)
      toast.success(t('remnawave_nginx.rule_deleted'))
      fetchDetail(); onRefreshList()
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('remnawave_nginx.rule_error'))
    }
  }

  const toggleEditRule = (rule: RemnawaveNginxRule) => {
    setEditingRules(prev => {
      const next = new Set(prev)
      if (next.has(rule.name)) next.delete(rule.name)
      else next.add(rule.name)
      return next
    })
  }

  // ---- Config raw edit ----
  const handleSaveConfig = async () => {
    setConfigSaving(true)
    try {
      await remnawaveNginxApi.updateProfile(profileId, { config_content: configEdit })
      toast.success(t('remnawave_nginx.config_saved'))
      fetchDetail(); onRefreshList()
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('remnawave_nginx.save_error'))
    } finally { setConfigSaving(false) }
  }

  const handleValidateConfig = async () => {
    setConfigValidating(true)
    try {
      const res = await remnawaveNginxApi.validateConfig(configEdit)
      if (res.data.valid) toast.success(t('remnawave_nginx.config_valid'))
      else toast.error(`${t('remnawave_nginx.config_invalid')}: ${res.data.message}`)
    } catch {
      toast.error(t('remnawave_nginx.validate_error'))
    } finally { setConfigValidating(false) }
  }

  const handleApplyTemplate = async () => {
    try {
      const res = await remnawaveNginxApi.regenerateConfig(profileId)
      setConfigEdit(res.data.config_content)
      toast.success(t('remnawave_nginx.template_applied'))
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('remnawave_nginx.rule_error'))
    }
  }

  const handleImportFromNode = async (serverId: number) => {
    setImporting(true)
    try {
      const res = await remnawaveNginxApi.importFromNode(serverId)
      setConfigEdit(res.data.content)
      setShowImport(false)
      if (res.data.detected_domain) {
        toast.success(t('remnawave_nginx.imported_with_domain', { domain: res.data.detected_domain }))
      } else {
        toast.success(t('remnawave_nginx.imported'))
      }
      if (!res.data.has_markers) toast.info(t('remnawave_nginx.imported_no_markers'))
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('remnawave_nginx.import_error'))
    } finally { setImporting(false) }
  }

  // ---- Sync ----
  const handleSyncAll = async () => {
    setSyncing(true)
    try {
      const res = await remnawaveNginxApi.syncAll(profileId)
      const results = res.data.results
      const ok = results.filter((r: RemnawaveNginxSyncResult) => r.status === 'success').length
      const queued = results.filter((r: RemnawaveNginxSyncResult) => r.status === 'queued').length
      const fail = results.filter((r: RemnawaveNginxSyncResult) => r.status === 'failed').length
      if (fail === 0 && queued === 0) toast.success(t('remnawave_nginx.sync_success', { count: ok }))
      else if (fail === 0 && queued > 0) toast.info(t('remnawave_nginx.sync_queued', { ok, queued }))
      else toast.warning(t('remnawave_nginx.sync_partial', { ok, fail }))
      await fetchDetail(); onRefreshList()
    } catch { toast.error(t('remnawave_nginx.sync_error')) }
    finally { setSyncing(false) }
  }

  const handleSyncOne = async (serverId: number) => {
    setSyncingServerId(serverId)
    try {
      const res = await remnawaveNginxApi.syncOne(profileId, serverId)
      if (res.data.status === 'queued') toast.info(t('remnawave_nginx.sync_one_queued'))
      else if (res.data.success) toast.success(res.data.message)
      else toast.error(res.data.message)
      await fetchDetail(); onRefreshList()
    } catch { toast.error(t('remnawave_nginx.sync_error')) }
    finally { setSyncingServerId(null) }
  }

  const handleRestartNode = async (serverId: number) => {
    if (!confirm(t('remnawave_nginx.restart_confirm'))) return
    setRestartingServerId(serverId)
    try {
      await remnawaveNginxApi.restartNode(serverId)
      toast.success(t('remnawave_nginx.restarted'))
      fetchNodeStatuses([{ server_id: serverId, online: true }])
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('remnawave_nginx.restart_error'))
    } finally { setRestartingServerId(null) }
  }

  // ---- Server binding ----
  const handleLinkServer = async () => {
    if (!linkingServer) return
    if (!wildcardDomain && !linkDomain.trim()) { toast.error(t('remnawave_nginx.domain_required')); return }
    try {
      await remnawaveNginxApi.linkServer(profileId, linkingServer.id, wildcardDomain ? null : linkDomain.trim())
      toast.success(t('remnawave_nginx.server_linked'))
      setLinkingServer(null); setLinkDomain(''); setShowAddServer(false)
      await fetchDetail(); onRefreshList()
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('remnawave_nginx.link_error'))
    }
  }

  const handleUnlinkServer = async (serverId: number) => {
    if (!confirm(t('remnawave_nginx.unlink_confirm'))) return
    try {
      await remnawaveNginxApi.unlinkServer(profileId, serverId)
      toast.success(t('remnawave_nginx.server_unlinked'))
      await fetchDetail(); onRefreshList()
    } catch { toast.error(t('remnawave_nginx.unlink_error')) }
  }

  const handleSaveDomain = async (serverId: number) => {
    if (!domainEdit.trim()) { toast.error(t('remnawave_nginx.domain_required')); return }
    try {
      await remnawaveNginxApi.updateServerDomain(profileId, serverId, domainEdit.trim())
      toast.success(t('remnawave_nginx.domain_updated'))
      setEditingDomainId(null)
      await fetchDetail(); onRefreshList()
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('remnawave_nginx.save_error'))
    }
  }

  const fetchLog = async () => {
    try { const res = await remnawaveNginxApi.getSyncLog(profileId); setSyncLog(res.data) }
    catch { toast.error(t('remnawave_nginx.log_error')) }
  }
  const toggleLog = () => { if (!showLog) fetchLog(); setShowLog(!showLog) }

  const unlinkedServers = availableServers
    .filter(s => s.active_profile_id === null || s.active_profile_id !== profileId)
    .filter(s => {
      if (!serverSearch) return true
      const q = serverSearch.toLowerCase()
      return s.name.toLowerCase().includes(q) || s.url.toLowerCase().includes(q)
    })

  const linkedDetectedServers = availableServers.filter(s => s.detected)

  if (loading) return <div className="flex items-center justify-center py-12"><Loader2 className="w-6 h-6 text-accent-400 animate-spin" /></div>
  if (!detail) return null

  return (
    <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }}
      className="border-t border-dark-700/50">
      <div className="p-4 space-y-4">

        {/* ===== Rules Section ===== */}
        <div>
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-medium text-dark-200">{t('remnawave_nginx.rules')}</h3>
            <div className="flex items-center gap-2">
              <button onClick={() => setShowOptions(!showOptions)}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs border transition-colors ${showOptions ? 'text-accent-400 bg-accent-500/10 border-accent-500/30' : 'text-dark-300 hover:text-dark-100 bg-dark-800/50 hover:bg-dark-700/50 border-dark-700/50'}`}>
                <Settings2 className="w-3.5 h-3.5" /> {t('remnawave_nginx.real_ip_options')}
              </button>
              <button onClick={() => { setShowConfig(!showConfig); if (!showConfig) setConfigEdit(detail.config_content) }}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs text-dark-300 hover:text-dark-100 bg-dark-800/50 hover:bg-dark-700/50 border border-dark-700/50 transition-colors">
                <Code className="w-3.5 h-3.5" /> {t('remnawave_nginx.show_config')}
              </button>
              {!showRuleForm && hasMarkers && (
                <button onClick={() => setShowRuleForm(true)}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors">
                  <Plus className="w-3.5 h-3.5" /> {t('remnawave_nginx.add_rule')}
                </button>
              )}
            </div>
          </div>

          {/* Real IP options */}
          <AnimatePresence>
            {showOptions && (
              <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }} className="mb-3 overflow-hidden">
                <OptionsSection
                  profileId={profileId}
                  initial={detail.options}
                  onSaved={config => { setConfigEdit(config); fetchDetail(); onRefreshList() }}
                />
              </motion.div>
            )}
          </AnimatePresence>

          {!hasMarkers && (
            <div className="flex items-center gap-2 mb-3 px-3 py-2 rounded-lg text-xs text-yellow-400 bg-yellow-500/10 border border-yellow-500/20">
              <AlertTriangle className="w-3.5 h-3.5 shrink-0" /> {t('remnawave_nginx.raw_mode_hint')}
            </div>
          )}

          <AnimatePresence>
            {showRuleForm && (
              <div className="mb-3">
                <RuleForm
                  initial={EMPTY_RULE_FORM}
                  isEdit={false}
                  saving={ruleSaving}
                  onSave={handleAddRule}
                  onCancel={() => setShowRuleForm(false)}
                />
              </div>
            )}
          </AnimatePresence>

          {rules.length === 0 ? (
            hasMarkers && (
              <div className="text-center text-dark-500 text-sm py-6 bg-dark-900/30 rounded-lg border border-dark-800/50">
                {t('remnawave_nginx.no_rules')}
              </div>
            )
          ) : (
            <div className="space-y-1.5">
              {rules.map(r => {
                const isEditing = editingRules.has(r.name)
                const editInitial: RuleFormData = {
                  name: r.name, rule_type: r.rule_type,
                  service_path: r.service_path ?? '', port: r.port != null ? String(r.port) : '',
                  path: r.path ?? '/', target_url: r.target_url ?? '',
                }
                return (
                  <div key={r.name}>
                    <div
                      className={`flex items-center justify-between px-3 py-2 rounded-lg border cursor-pointer transition-colors ${isEditing ? 'bg-dark-800/60 border-accent-500/30' : 'bg-dark-900/30 border-dark-800/50 hover:bg-dark-800/40'} group`}
                      onClick={() => toggleEditRule(r)}
                    >
                      <div className="flex items-center gap-3 min-w-0">
                        {r.rule_type === 'proxy'
                          ? <Globe className="w-3.5 h-3.5 text-dark-400 shrink-0" />
                          : <Lock className="w-3.5 h-3.5 text-accent-400 shrink-0" />}
                        <span className="text-sm text-dark-200 font-medium">{r.name}</span>
                        {r.rule_type === 'grpc' ? (
                          <>
                            <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-accent-500/10 text-accent-400 border border-accent-500/20">gRPC</span>
                            <span className="text-xs text-dark-500">/{r.service_path} → 127.0.0.1:{r.port}</span>
                          </>
                        ) : r.rule_type === 'xhttp' ? (
                          <>
                            <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-accent-500/10 text-accent-400 border border-accent-500/20">XHTTP</span>
                            <span className="text-xs text-dark-500 truncate">{r.path} → 127.0.0.1:{r.port}</span>
                          </>
                        ) : (
                          <>
                            <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-dark-700/60 text-dark-300 border border-dark-600/40">PROXY</span>
                            <span className="text-xs text-dark-500 truncate">{r.path} → {r.target_url}</span>
                          </>
                        )}
                      </div>
                      <div className="flex items-center gap-1 shrink-0 opacity-0 group-hover:opacity-100 transition-opacity">
                        <Tooltip label={t('common.delete')}>
                          <button onClick={e => { e.stopPropagation(); handleDeleteRule(r.name) }} className="p-1.5 rounded-lg text-dark-400 hover:text-red-400 hover:bg-red-500/10 transition-colors">
                            <Trash2 className="w-3.5 h-3.5" />
                          </button>
                        </Tooltip>
                      </div>
                    </div>
                    <AnimatePresence>
                      {isEditing && (
                        <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }} className="overflow-hidden">
                          <div className="mt-1">
                            <RuleForm
                              initial={editInitial}
                              isEdit={true}
                              saving={ruleSaving}
                              onSave={handleUpdateRule}
                              onCancel={() => setEditingRules(prev => { const next = new Set(prev); next.delete(r.name); return next })}
                            />
                          </div>
                        </motion.div>
                      )}
                    </AnimatePresence>
                  </div>
                )
              })}
            </div>
          )}
        </div>

        {/* ===== Raw Config Modal ===== */}
        <AnimatePresence>
          {showConfig && (
            <motion.div
              className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-dark-950/80 backdrop-blur-sm"
              initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
              onMouseDown={e => { if (e.target === e.currentTarget) setConfigModalMouseDown(true) }}
              onClick={e => { if (e.target === e.currentTarget && configModalMouseDown) setShowConfig(false); setConfigModalMouseDown(false) }}
            >
              <motion.div
                className="bg-dark-900 border border-dark-700 rounded-2xl shadow-2xl w-full max-w-4xl max-h-[90vh] flex flex-col"
                initial={{ opacity: 0, scale: 0.95, y: 20 }} animate={{ opacity: 1, scale: 1, y: 0 }} exit={{ opacity: 0, scale: 0.95, y: 20 }}
                onMouseDown={() => setConfigModalMouseDown(false)}
              >
                <div className="flex items-center justify-between p-5 border-b border-dark-700">
                  <div className="flex items-center gap-3">
                    <div className="w-10 h-10 rounded-xl bg-accent-500/10 flex items-center justify-center">
                      <Code className="w-5 h-5 text-accent-500" />
                    </div>
                    <div>
                      <h2 className="text-lg font-semibold text-dark-100">{t('remnawave_nginx.raw_config')}</h2>
                      <p className="text-xs text-dark-500">{detail?.name}</p>
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <button onClick={() => setShowImport(!showImport)}
                      className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs text-dark-300 hover:text-dark-100 bg-dark-800 hover:bg-dark-700 border border-dark-700 transition-colors">
                      <Download className="w-3.5 h-3.5" /> {t('remnawave_nginx.import_from_node')}
                    </button>
                    <motion.button onClick={() => setShowConfig(false)}
                      className="p-2 hover:bg-dark-800 rounded-lg text-dark-400 transition-colors"
                      whileHover={{ scale: 1.1, rotate: 90 }} whileTap={{ scale: 0.9 }}>
                      <X className="w-5 h-5" />
                    </motion.button>
                  </div>
                </div>
                <AnimatePresence>
                  {showImport && (
                    <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }} className="overflow-hidden border-b border-dark-700">
                      <div className="p-4 bg-dark-950/50">
                        <div className="text-xs text-dark-400 mb-2">{t('remnawave_nginx.import_select_server')}</div>
                        {linkedDetectedServers.length === 0 ? (
                          <div className="text-xs text-dark-500">{t('remnawave_nginx.no_detected_servers')}</div>
                        ) : (
                          <div className="flex flex-wrap gap-2">
                            {linkedDetectedServers.map(s => (
                              <button key={s.id} onClick={() => handleImportFromNode(s.id)} disabled={importing}
                                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs text-dark-200 bg-dark-800 hover:bg-dark-700 border border-dark-700 transition-colors disabled:opacity-50">
                                {importing ? <Loader2 className="w-3 h-3 animate-spin" /> : <Server className="w-3 h-3 text-dark-400" />} {s.name}
                              </button>
                            ))}
                          </div>
                        )}
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>
                <div className="flex-1 overflow-hidden p-5">
                  <textarea value={configEdit} onChange={e => setConfigEdit(e.target.value)} spellCheck={false}
                    className="w-full h-[50vh] px-4 py-3 rounded-xl bg-dark-950 border border-dark-700 text-dark-200 text-sm font-mono focus:outline-none focus:border-accent-500/50 transition-colors resize-y
                      scrollbar-thin scrollbar-thumb-dark-700 scrollbar-track-transparent" />
                  <div className="flex items-center gap-1.5 mt-2 text-xs text-dark-500">
                    <AlertTriangle className="w-3.5 h-3.5" /> {t('remnawave_nginx.raw_config_warning')}
                  </div>
                </div>
                <div className="flex items-center justify-between p-5 border-t border-dark-700">
                  <button onClick={handleApplyTemplate}
                    className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm text-dark-300 hover:text-dark-100 bg-dark-800 hover:bg-dark-700 border border-dark-700 transition-colors">
                    <RefreshCw className="w-4 h-4" /> {t('remnawave_nginx.apply_template')}
                  </button>
                  <div className="flex items-center gap-3">
                    <button onClick={handleValidateConfig} disabled={configValidating}
                      className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm text-dark-300 hover:text-dark-100 bg-dark-800 hover:bg-dark-700 border border-dark-700 transition-colors disabled:opacity-50">
                      {configValidating ? <Loader2 className="w-4 h-4 animate-spin" /> : <CheckCircle2 className="w-4 h-4" />} {t('remnawave_nginx.validate_config')}
                    </button>
                    <button onClick={() => setShowConfig(false)}
                      className="px-4 py-2 rounded-lg text-sm text-dark-300 hover:text-dark-100 bg-dark-800 hover:bg-dark-700 border border-dark-700 transition-colors">
                      {t('common.close')}
                    </button>
                    <button onClick={handleSaveConfig} disabled={configSaving}
                      className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors disabled:opacity-50">
                      {configSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />} {t('common.save')}
                    </button>
                  </div>
                </div>
              </motion.div>
            </motion.div>
          )}
        </AnimatePresence>

        {/* ===== Servers Section ===== */}
        <div>
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-medium text-dark-200">{t('remnawave_nginx.linked_servers')}</h3>
            <div className="flex items-center gap-2">
              <button onClick={toggleLog}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs text-dark-300 hover:text-dark-100 bg-dark-800/50 hover:bg-dark-700/50 border border-dark-700/50 transition-colors">
                <History className="w-3.5 h-3.5" /> {t('remnawave_nginx.sync_log')}
              </button>
              <button onClick={() => setShowAddServer(!showAddServer)}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs text-dark-300 hover:text-dark-100 bg-dark-800/50 hover:bg-dark-700/50 border border-dark-700/50 transition-colors">
                <Link2 className="w-3.5 h-3.5" /> {t('remnawave_nginx.add_server')}
              </button>
              {serversStatus.length > 0 && (
                <button onClick={handleSyncAll} disabled={syncing}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors disabled:opacity-50">
                  {syncing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />} {t('remnawave_nginx.sync_all')}
                </button>
              )}
            </div>
          </div>

          <AnimatePresence>
            {showAddServer && (
              <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }} className="mb-3 overflow-hidden">
                <div className="rounded-lg border border-dark-700/50 bg-dark-900/50 p-3">
                  {linkingServer ? (
                    <div className="space-y-2">
                      <div className="flex items-center justify-between">
                        <span className="text-xs text-dark-300 flex items-center gap-2">
                          <Server className="w-3.5 h-3.5 text-dark-400" /> {linkingServer.name}
                        </span>
                        <button onClick={() => { setLinkingServer(null); setLinkDomain('') }} className="p-1 text-dark-400 hover:text-dark-200"><X className="w-3.5 h-3.5" /></button>
                      </div>
                      {wildcardDomain ? (
                        <div className="flex items-center justify-between gap-2">
                          <span className="flex items-center gap-1.5 text-xs text-dark-400">
                            <Globe className="w-3 h-3" />
                            {t('remnawave_nginx.wildcard_link_hint', { domain: wildcardDomain })}
                          </span>
                          <button onClick={handleLinkServer} autoFocus
                            className="px-3 py-1.5 rounded-lg text-xs font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors">
                            {t('remnawave_nginx.link')}
                          </button>
                        </div>
                      ) : (
                        <>
                          <label className="block text-xs text-dark-400">{t('remnawave_nginx.node_domain')}</label>
                          <div className="flex items-center gap-2">
                            <input type="text" value={linkDomain} onChange={e => setLinkDomain(e.target.value)}
                              placeholder="node1.example.com" autoFocus
                              onKeyDown={e => { if (e.key === 'Enter') handleLinkServer() }}
                              className="flex-1 px-3 py-1.5 rounded-lg bg-dark-800 border border-dark-700 text-dark-100 text-sm font-mono focus:outline-none focus:border-accent-500/50" />
                            <button onClick={handleLinkServer}
                              className="px-3 py-1.5 rounded-lg text-xs font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors">
                              {t('remnawave_nginx.link')}
                            </button>
                          </div>
                          <p className="text-[10px] text-dark-500">{t('remnawave_nginx.node_domain_hint')}</p>
                        </>
                      )}
                      <div className="flex items-start gap-2 px-2.5 py-2 rounded-lg text-[10px] text-yellow-400 bg-yellow-500/10 border border-yellow-500/20">
                        <AlertTriangle className="w-3 h-3 shrink-0 mt-0.5" /> {t('remnawave_nginx.link_replaces_config_warning')}
                      </div>
                    </div>
                  ) : (
                    <>
                      <div className="text-xs text-dark-400 mb-2">{t('remnawave_nginx.select_server')}</div>
                      <input type="text" value={serverSearch} onChange={e => setServerSearch(e.target.value)}
                        placeholder={t('remnawave_nginx.search_server')}
                        className="w-full px-3 py-1.5 mb-2 rounded-lg bg-dark-800 border border-dark-700 text-dark-100 text-sm focus:outline-none focus:border-accent-500/50 transition-colors" autoFocus />
                      {unlinkedServers.length === 0 ? (
                        <div className="text-xs text-dark-500">{t('remnawave_nginx.no_available_servers')}</div>
                      ) : (
                        <div className="space-y-1 max-h-48 overflow-y-auto">
                          {unlinkedServers.map(s => (
                            <button key={s.id} onClick={() => { setLinkingServer(s); setLinkDomain(s.domain || '') }}
                              className="w-full flex items-center justify-between px-3 py-2 rounded-lg text-sm text-dark-200 hover:bg-dark-700/50 transition-colors">
                              <span className="flex items-center gap-2">
                                <Server className="w-3.5 h-3.5 text-dark-400" />{s.name}
                                {s.detected && (
                                  <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium bg-green-500/10 text-green-400 border border-green-500/20">
                                    {t('remnawave_nginx.detected')}
                                  </span>
                                )}
                              </span>
                              <span className="flex items-center gap-2 text-xs text-dark-500">
                                {s.domain && <span className="font-mono">{s.domain}</span>}
                                {s.active_profile_id && <span>{t('remnawave_nginx.has_other_profile')}</span>}
                              </span>
                            </button>
                          ))}
                        </div>
                      )}
                    </>
                  )}
                </div>
              </motion.div>
            )}
          </AnimatePresence>

          {serversStatus.length === 0 ? (
            <div className="text-center text-dark-500 text-sm py-6 bg-dark-900/30 rounded-lg border border-dark-800/50">{t('remnawave_nginx.no_servers')}</div>
          ) : (
            <div className="space-y-1.5">
              {serversStatus.map(s => {
                const nodeStatus = nodeStatuses[s.server_id]
                const running = nodeStatus?.container?.running
                return (
                  <div key={s.server_id} className={`flex items-center justify-between px-3 py-2 rounded-lg bg-dark-900/30 border border-dark-800/50 ${s.online ? '' : 'opacity-60'}`}>
                    <div className="flex items-center gap-2.5 min-w-0">
                      <Tooltip label={s.online ? t('remnawave_nginx.server_online') : t('remnawave_nginx.server_offline')}>
                        <span className="relative flex w-2.5 h-2.5 shrink-0">
                          {s.online && <span className="absolute inline-flex w-full h-full rounded-full bg-green-400/60 animate-ping" />}
                          <span className={`relative inline-flex w-2.5 h-2.5 rounded-full ${s.online ? 'bg-green-400' : 'bg-red-500'}`} />
                        </span>
                      </Tooltip>
                      <span className="text-sm text-dark-200 truncate">{s.server_name}</span>
                      {s.online && running != null && (
                        <Tooltip label={running ? t('remnawave_nginx.container_running') : t('remnawave_nginx.container_stopped')}>
                          <Activity className={`w-3.5 h-3.5 shrink-0 ${running ? 'text-green-400/70' : 'text-red-400/70'}`} />
                        </Tooltip>
                      )}
                      {!s.detected && (
                        <Tooltip label={t('remnawave_nginx.not_detected_hint')}>
                          <AlertTriangle className="w-3.5 h-3.5 text-yellow-400/70 shrink-0" />
                        </Tooltip>
                      )}
                      {wildcardDomain ? (
                        <span className="flex items-center gap-1 text-xs font-mono text-dark-400">
                          <Globe className="w-3 h-3" /> *.{wildcardDomain}
                        </span>
                      ) : editingDomainId === s.server_id ? (
                        <span className="flex items-center gap-1" onClick={e => e.stopPropagation()}>
                          <input type="text" value={domainEdit} onChange={e => setDomainEdit(e.target.value)}
                            onKeyDown={e => { if (e.key === 'Enter') handleSaveDomain(s.server_id); if (e.key === 'Escape') setEditingDomainId(null) }}
                            className="px-2 py-0.5 rounded bg-dark-800 border border-accent-500/50 text-dark-100 text-xs font-mono focus:outline-none w-48" autoFocus />
                          <button onClick={() => handleSaveDomain(s.server_id)} className="p-1 text-green-400 hover:text-green-300"><CheckCircle2 className="w-3.5 h-3.5" /></button>
                          <button onClick={() => setEditingDomainId(null)} className="p-1 text-dark-400 hover:text-dark-200"><X className="w-3.5 h-3.5" /></button>
                        </span>
                      ) : (
                        <button onClick={() => { setEditingDomainId(s.server_id); setDomainEdit(s.domain || '') }}
                          className="flex items-center gap-1 text-xs font-mono text-dark-400 hover:text-dark-200 transition-colors group/domain">
                          <Globe className="w-3 h-3" /> {s.domain || t('remnawave_nginx.no_domain')}
                          <Edit3 className="w-2.5 h-2.5 opacity-0 group-hover/domain:opacity-100 transition-opacity" />
                        </button>
                      )}
                      <SyncStatusBadge status={s.sync_status} online={s.online} />
                    </div>
                    <div className="flex items-center gap-1 shrink-0">
                      {s.online && (
                        <Tooltip label={t('remnawave_nginx.restart_container')}>
                          <button onClick={() => handleRestartNode(s.server_id)} disabled={restartingServerId === s.server_id}
                            className="p-1.5 rounded-lg text-dark-400 hover:text-yellow-400 hover:bg-yellow-500/10 transition-colors disabled:opacity-50">
                            {restartingServerId === s.server_id ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Power className="w-3.5 h-3.5" />}
                          </button>
                        </Tooltip>
                      )}
                      <Tooltip label={t('remnawave_nginx.sync_server')}>
                        <button onClick={() => handleSyncOne(s.server_id)} disabled={syncingServerId === s.server_id}
                          className="p-1.5 rounded-lg text-dark-400 hover:text-accent-400 hover:bg-accent-500/10 transition-colors">
                          {syncingServerId === s.server_id ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RotateCw className="w-3.5 h-3.5" />}
                        </button>
                      </Tooltip>
                      <Tooltip label={t('remnawave_nginx.unlink_server')}>
                        <button onClick={() => handleUnlinkServer(s.server_id)}
                          className="p-1.5 rounded-lg text-dark-400 hover:text-red-400 hover:bg-red-500/10 transition-colors">
                          <Unlink className="w-3.5 h-3.5" />
                        </button>
                      </Tooltip>
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>

        {/* Sync Log */}
        <AnimatePresence>
          {showLog && (
            <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }} className="overflow-hidden">
              <div className="rounded-lg border border-dark-700/50 bg-dark-900/50 p-3">
                <div className="text-xs font-medium text-dark-300 mb-2">{t('remnawave_nginx.recent_sync_log')}</div>
                {syncLog.length === 0 ? (
                  <div className="text-xs text-dark-500">{t('remnawave_nginx.no_log')}</div>
                ) : (
                  <div className="space-y-1 max-h-48 overflow-y-auto">
                    {syncLog.map(entry => (
                      <div key={entry.id} className="flex items-center justify-between text-xs px-2 py-1.5 rounded bg-dark-800/50">
                        <div className="flex items-center gap-2 min-w-0">
                          {entry.status === 'success' ? <CheckCircle2 className="w-3 h-3 text-green-400 shrink-0" /> : <XCircle className="w-3 h-3 text-red-400 shrink-0" />}
                          <span className="text-dark-300 truncate">{entry.server_name}</span>
                          {entry.message && <span className="text-dark-500 truncate hidden sm:block">— {entry.message}</span>}
                        </div>
                        <span className="text-dark-500 shrink-0 ml-2">{entry.created_at ? new Date(entry.created_at).toLocaleString() : ''}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </motion.div>
  )
}

// ==================== Create/Edit Name Modal ====================

function ProfileModal({ profile, onClose, onSaved }: { profile: RemnawaveNginxProfile | null; onClose: () => void; onSaved: () => void }) {
  const { t } = useTranslation()
  const isEdit = !!profile
  const [name, setName] = useState(profile?.name || '')
  const [description, setDescription] = useState(profile?.description || '')
  const [saving, setSaving] = useState(false)

  const handleSave = async () => {
    if (!name.trim()) { toast.error(t('remnawave_nginx.name_required')); return }
    setSaving(true)
    try {
      if (isEdit) {
        await remnawaveNginxApi.updateProfile(profile!.id, { name: name.trim(), description: description.trim() || undefined })
        toast.success(t('remnawave_nginx.profile_updated'))
      } else {
        await remnawaveNginxApi.createProfile({ name: name.trim(), description: description.trim() || undefined })
        toast.success(t('remnawave_nginx.profile_created'))
      }
      onSaved(); onClose()
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('remnawave_nginx.save_error'))
    } finally { setSaving(false) }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />
      <motion.div initial={{ opacity: 0, scale: 0.95, y: 10 }} animate={{ opacity: 1, scale: 1, y: 0 }} exit={{ opacity: 0, scale: 0.95, y: 10 }}
        className="relative w-full max-w-lg bg-dark-900 border border-dark-700/80 rounded-2xl shadow-2xl overflow-hidden">
        <div className="flex items-center justify-between px-6 py-4 border-b border-dark-800">
          <h2 className="text-lg font-semibold text-dark-100">{isEdit ? t('remnawave_nginx.edit_profile') : t('remnawave_nginx.create_profile')}</h2>
          <button onClick={onClose} className="p-1.5 rounded-lg text-dark-400 hover:text-dark-200 hover:bg-dark-700/50 transition-colors"><X className="w-5 h-5" /></button>
        </div>
        <div className="p-6 space-y-4">
          <div>
            <label className="block text-xs font-medium text-dark-300 mb-1.5">{t('remnawave_nginx.profile_name')}</label>
            <input type="text" value={name} onChange={e => setName(e.target.value)} placeholder={t('remnawave_nginx.name_placeholder')}
              className="w-full px-3 py-2 rounded-lg bg-dark-800 border border-dark-700 text-dark-100 text-sm focus:outline-none focus:border-accent-500/50 transition-colors" autoFocus />
          </div>
          <div>
            <label className="block text-xs font-medium text-dark-300 mb-1.5">{t('remnawave_nginx.description')}</label>
            <input type="text" value={description} onChange={e => setDescription(e.target.value)} placeholder={t('remnawave_nginx.description_placeholder')}
              className="w-full px-3 py-2 rounded-lg bg-dark-800 border border-dark-700 text-dark-100 text-sm focus:outline-none focus:border-accent-500/50 transition-colors" />
          </div>
          {!isEdit && <div className="text-xs text-dark-500">{t('remnawave_nginx.default_config_hint')}</div>}
        </div>
        <div className="flex items-center justify-end gap-3 px-6 py-4 border-t border-dark-800">
          <button onClick={onClose} className="px-4 py-2 rounded-lg text-sm text-dark-300 hover:text-dark-100 bg-dark-800 hover:bg-dark-700 border border-dark-700 transition-colors">{t('common.cancel')}</button>
          <button onClick={handleSave} disabled={saving}
            className="px-4 py-2 rounded-lg text-sm font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors disabled:opacity-50 flex items-center gap-2">
            {saving && <Loader2 className="w-4 h-4 animate-spin" />}
            {isEdit ? t('common.save') : t('remnawave_nginx.create')}
          </button>
        </div>
      </motion.div>
    </div>
  )
}

// ==================== Main Page ====================

export default function RemnawaveNginx() {
  const { t } = useTranslation()
  const [profiles, setProfiles] = useState<RemnawaveNginxProfile[]>([])
  const [loading, setLoading] = useState(true)
  const [expandedId, setExpandedId] = useState<number | null>(null)
  const [modalProfile, setModalProfile] = useState<RemnawaveNginxProfile | null | 'new'>(null)

  const initialLoadDone = useRef(false)

  const fetchProfiles = useCallback(async () => {
    try {
      const res = await remnawaveNginxApi.getProfiles()
      setProfiles(res.data)
    } catch {
      if (!initialLoadDone.current) toast.error(t('remnawave_nginx.fetch_error'))
    } finally {
      if (!initialLoadDone.current) { initialLoadDone.current = true; setLoading(false) }
    }
  }, [t])

  useEffect(() => { fetchProfiles() }, [fetchProfiles])

  useEffect(() => {
    const id = setInterval(() => { if (!document.hidden) fetchProfiles() }, 3000)
    return () => clearInterval(id)
  }, [fetchProfiles])

  const handleExpand = (id: number) => setExpandedId(prev => prev === id ? null : id)

  const handleDelete = async (id: number) => {
    if (!confirm(t('remnawave_nginx.delete_confirm'))) return
    try {
      await remnawaveNginxApi.deleteProfile(id)
      toast.success(t('remnawave_nginx.profile_deleted'))
      if (expandedId === id) setExpandedId(null)
      await fetchProfiles()
    } catch { toast.error(t('remnawave_nginx.delete_error')) }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="p-2.5 rounded-xl bg-gradient-to-br from-dark-800 to-dark-900 border border-dark-700/50">
            <Waypoints className="w-6 h-6 text-accent-400" />
          </div>
          <div>
            <h1 className="text-xl font-bold text-dark-100 flex items-center gap-2">
              {t('remnawave_nginx.title')}
              <FAQIcon screen="PAGE_REMNAWAVE_NGINX" />
            </h1>
            <p className="text-sm text-dark-400">{t('remnawave_nginx.subtitle')}</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={fetchProfiles} className="p-2 rounded-lg text-dark-400 hover:text-dark-200 hover:bg-dark-800/50 border border-dark-700/50 transition-colors">
            <RefreshCw className="w-4 h-4" />
          </button>
          <button onClick={() => setModalProfile('new')} className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors">
            <Plus className="w-4 h-4" /> {t('remnawave_nginx.create_profile')}
          </button>
        </div>
      </div>

      {loading ? (
        <div className="flex items-center justify-center py-16"><Loader2 className="w-6 h-6 text-accent-400 animate-spin" /></div>
      ) : profiles.length === 0 ? (
        <div className="text-center py-16 text-dark-500">
          <Waypoints className="w-12 h-12 mx-auto mb-3 opacity-30" />
          <p className="text-lg">{t('remnawave_nginx.no_profiles')}</p>
          <p className="text-sm mt-1">{t('remnawave_nginx.no_profiles_hint')}</p>
        </div>
      ) : (
        <div className="space-y-2">
          <AnimatePresence mode="popLayout">
            {profiles.map(p => (
              <div key={p.id}>
                <ProfileCard profile={p} expanded={expandedId === p.id} onExpand={handleExpand} onEdit={setModalProfile} onDelete={handleDelete} />
                <AnimatePresence>
                  {expandedId === p.id && <ProfileDetailPanel profileId={p.id} onRefreshList={fetchProfiles} />}
                </AnimatePresence>
              </div>
            ))}
          </AnimatePresence>
        </div>
      )}

      <AnimatePresence>
        {modalProfile && (
          <ProfileModal profile={modalProfile === 'new' ? null : modalProfile} onClose={() => setModalProfile(null)} onSaved={fetchProfiles} />
        )}
      </AnimatePresence>
    </div>
  )
}
