import { useEffect, useState, useCallback, useRef } from 'react'
import { FileCode2, Plus, RefreshCw, Trash2, Server, ChevronDown, ChevronRight, Edit3, Link2, Unlink, Loader2, CheckCircle2, XCircle, AlertCircle, Clock, History, X, Code, Save, AlertTriangle, Activity, Scale, Cpu } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { motion, AnimatePresence } from 'framer-motion'
import { toast } from 'sonner'
import { formatBitsPerSec } from '../utils/format'
import {
  haproxyProfilesApi,
  HAProxyConfigProfile,
  HAProxyProfileDetail,
  HAProxyProfileRule,
  HAProxySyncResult,
  HAProxySyncLogEntry,
  HAProxyAvailableServer,
  HAProxyServerStatus,
  BackendServer,
  BalancerOptions,
} from '../api/client'
import { Tooltip } from '../components/ui/Tooltip'
import { FAQIcon } from '../components/FAQ'


function SyncStatusBadge({ status }: { status: string | null }) {
  const { t } = useTranslation()
  if (!status) return null

  const map: Record<string, { color: string; icon: React.ReactNode; label: string }> = {
    synced: { color: 'text-green-400 bg-green-500/10 border-green-500/20', icon: <CheckCircle2 className="w-3 h-3" />, label: t('haproxy_configs.synced') },
    pending: { color: 'text-yellow-400 bg-yellow-500/10 border-yellow-500/20', icon: <Clock className="w-3 h-3" />, label: t('haproxy_configs.pending') },
    failed: { color: 'text-red-400 bg-red-500/10 border-red-500/20', icon: <XCircle className="w-3 h-3" />, label: t('haproxy_configs.failed') },
  }
  const s = map[status] || map.pending
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs border ${s.color}`}>
      {s.icon} {s.label}
    </span>
  )
}


// ==================== Balancer Constants ====================

const ALGORITHMS = [
  'leastconn', 'roundrobin', 'source', 'first',
] as const

const ALGO_NEEDS_HASH = new Set(['source'])

const DEFAULT_SERVER: BackendServer = {
  name: 'srv1', address: '', port: 0, weight: 1,
  maxconn: 100000, check: true, inter: '5s', fall: 3, rise: 2,
  send_proxy: false, send_proxy_v2: true,
  backup: false, slowstart: '60s', disabled: false,
}

const DEFAULT_BALANCER_OPTIONS: BalancerOptions = {
  algorithm: 'leastconn', retries: 3, redispatch: true,
  health_check_type: 'tcp-check',
  sticky_type: 'stick-table', stick_table_type: 'ip',
  stick_table_size: '500k', stick_table_expire: '1h',
  fullconn: 100000, timeout_queue: '30s',
}

// ==================== Toggle Component ====================

function Toggle({ value, onChange, label }: { value: boolean; onChange: (v: boolean) => void; label?: string }) {
  return (
    <div className="flex items-center justify-between">
      {label && <span className="text-xs text-dark-300">{label}</span>}
      <button type="button" onClick={() => onChange(!value)}
        className={`relative w-9 h-5 rounded-full transition-colors duration-200 ${value ? 'bg-green-500' : 'bg-dark-600'}`}>
        <span className={`absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full shadow transition-transform duration-200 ${value ? 'translate-x-4' : 'translate-x-0'}`} />
      </button>
    </div>
  )
}

// ==================== Backend Server Row ====================

function BackendServerRow({
  srv, index, onChange, onRemove, canRemove, t,
}: {
  srv: BackendServer; index: number
  onChange: (i: number, s: BackendServer) => void
  onRemove: (i: number) => void
  canRemove: boolean
  t: (k: string) => string
}) {
  const [expanded, setExpanded] = useState(false)
  const upd = (patch: Partial<BackendServer>) => onChange(index, { ...srv, ...patch })
  const inp = "w-full px-2.5 py-1 rounded-lg bg-dark-800 border border-dark-700 text-dark-100 text-sm focus:outline-none focus:border-accent-500/50"

  return (
    <div className="p-3 bg-dark-900/40 rounded-lg border border-dark-700/40 space-y-2">
      <div className="flex items-center gap-2">
        <div className="flex-1 grid grid-cols-4 gap-2">
          <input type="text" value={srv.name} onChange={e => upd({ name: e.target.value })}
            placeholder="srv1" className={inp} title={t('balancer.server_name')} />
          <input type="text" value={srv.address} onChange={e => upd({ address: e.target.value })}
            placeholder="1.2.3.4" className={inp} title={t('balancer.address')} />
          <input type="number" value={srv.port || ''} onChange={e => upd({ port: parseInt(e.target.value) || 0 })}
            placeholder="8080" className={inp} title={t('haproxy.target_port')} />
          <input type="number" value={srv.weight ?? 1} onChange={e => upd({ weight: parseInt(e.target.value) || 1 })}
            placeholder="1" className={inp} title={t('balancer.weight')} min={1} max={256} />
        </div>
        <button type="button" onClick={() => setExpanded(e => !e)}
          className="p-1 text-dark-400 hover:text-dark-200 transition-colors">
          {expanded ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
        </button>
        {canRemove && (
          <Tooltip label={t('common.delete')}>
            <button type="button" onClick={() => onRemove(index)}
              className="p-1 text-dark-400 hover:text-red-400 transition-colors">
              <Trash2 className="w-3.5 h-3.5" />
            </button>
          </Tooltip>
        )}
      </div>

      {/* Labels row */}
      {!expanded && (
        <div className="grid grid-cols-4 gap-2 -mt-1">
          <span className="text-[10px] text-dark-500">{t('balancer.server_name')}</span>
          <span className="text-[10px] text-dark-500">{t('balancer.address')}</span>
          <span className="text-[10px] text-dark-500">{t('haproxy.target_port')}</span>
          <span className="text-[10px] text-dark-500">{t('balancer.weight')}</span>
        </div>
      )}

      {expanded && (
        <div className="space-y-2 pt-1 border-t border-dark-700/30">
          <div className="grid grid-cols-3 gap-2">
            <div>
              <label className="block text-[10px] text-dark-500 mb-0.5">{t('balancer.maxconn')}</label>
              <input type="number" value={srv.maxconn ?? ''} onChange={e => upd({ maxconn: e.target.value ? parseInt(e.target.value) : undefined })}
                placeholder="500" className={inp} />
            </div>
            <div>
              <label className="block text-[10px] text-dark-500 mb-0.5">{t('balancer.slowstart')}</label>
              <input type="text" value={srv.slowstart ?? ''} onChange={e => upd({ slowstart: e.target.value || undefined })}
                placeholder="60s" className={inp} />
            </div>
            <div>
              <label className="block text-[10px] text-dark-500 mb-0.5">{t('balancer.inter')}</label>
              <input type="text" value={srv.inter ?? '5s'} onChange={e => upd({ inter: e.target.value || '5s' })}
                placeholder="5s" className={inp} />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="block text-[10px] text-dark-500 mb-0.5">Fall</label>
              <input type="number" value={srv.fall ?? 3} onChange={e => upd({ fall: parseInt(e.target.value) || 3 })}
                className={inp} min={1} />
            </div>
            <div>
              <label className="block text-[10px] text-dark-500 mb-0.5">Rise</label>
              <input type="number" value={srv.rise ?? 2} onChange={e => upd({ rise: parseInt(e.target.value) || 2 })}
                className={inp} min={1} />
            </div>
          </div>
          <div className="flex flex-wrap gap-x-5 gap-y-1">
            <Toggle value={srv.check ?? true} onChange={v => upd({ check: v })} label={t('balancer.check')} />
            <Toggle value={srv.send_proxy ?? false} onChange={v => upd({ send_proxy: v, send_proxy_v2: v ? false : srv.send_proxy_v2 })} label="PROXY v1" />
            <Toggle value={srv.send_proxy_v2 ?? false} onChange={v => upd({ send_proxy_v2: v, send_proxy: v ? false : srv.send_proxy })} label="PROXY v2" />
            <Toggle value={srv.backup ?? false} onChange={v => upd({ backup: v })} label={t('balancer.backup')} />
            <Toggle value={srv.disabled ?? false} onChange={v => upd({ disabled: v })} label={t('balancer.disabled')} />
          </div>
        </div>
      )}
    </div>
  )
}

// ==================== Balancer Settings ====================

function BalancerSettingsSection({
  opts, onChange, t,
}: {
  opts: BalancerOptions; onChange: (o: BalancerOptions) => void; t: (k: string) => string
}) {
  const [expanded, setExpanded] = useState(false)
  const upd = (patch: Partial<BalancerOptions>) => onChange({ ...opts, ...patch })
  const inp = "w-full px-2.5 py-1 rounded-lg bg-dark-800 border border-dark-700 text-dark-100 text-sm focus:outline-none focus:border-accent-500/50"

  return (
    <div className="space-y-3">
      {/* Algorithm */}
      <div>
        <label className="block text-xs text-dark-400 mb-1">{t('balancer.algorithm')}</label>
        <select value={opts.algorithm} onChange={e => upd({ algorithm: e.target.value })} className={inp}>
          {ALGORITHMS.map(a => (
            <option key={a} value={a}>{t(`balancer.alg.${a.replace('-', '_')}`)}</option>
          ))}
        </select>
        <p className="text-[10px] text-dark-500 mt-0.5">{t(`balancer.alg.${opts.algorithm.replace('-', '_')}_hint`)}</p>
      </div>

      {ALGO_NEEDS_HASH.has(opts.algorithm) && (
        <div>
          <label className="block text-xs text-dark-400 mb-1">{t('balancer.hash_type')}</label>
          <select value={opts.hash_type ?? ''} onChange={e => upd({ hash_type: e.target.value || undefined })} className={inp}>
            <option value="">—</option>
            <option value="consistent">{t('balancer.consistent')}</option>
            <option value="map-based">{t('balancer.map_based')}</option>
          </select>
        </div>
      )}

      {/* Advanced toggle */}
      <button type="button" onClick={() => setExpanded(e => !e)}
        className="flex items-center gap-1.5 text-xs text-dark-400 hover:text-dark-200 transition-colors">
        {expanded ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
        {t('balancer.advanced')}
      </button>

      {expanded && (
        <div className="space-y-3 pl-2 border-l-2 border-dark-700/40">
          {/* Health check */}
          <div>
            <label className="block text-xs text-dark-400 mb-1">{t('balancer.health_check_type')}</label>
            <select value={opts.health_check_type ?? ''} onChange={e => upd({ health_check_type: e.target.value || undefined })} className={inp}>
              <option value="">—</option>
              <option value="tcp-check">{t('balancer.tcp_check')}</option>
              <option value="httpchk">{t('balancer.http_check')}</option>
            </select>
          </div>

          {opts.health_check_type === 'httpchk' && (
            <div className="grid grid-cols-3 gap-2">
              <div>
                <label className="block text-[10px] text-dark-500 mb-0.5">{t('balancer.httpchk_method')}</label>
                <select value={opts.httpchk_method ?? 'GET'} onChange={e => upd({ httpchk_method: e.target.value })} className={inp}>
                  <option>GET</option><option>HEAD</option><option>OPTIONS</option>
                </select>
              </div>
              <div>
                <label className="block text-[10px] text-dark-500 mb-0.5">{t('balancer.httpchk_uri')}</label>
                <input type="text" value={opts.httpchk_uri ?? ''} onChange={e => upd({ httpchk_uri: e.target.value || undefined })}
                  placeholder="/health" className={inp} />
              </div>
              <div>
                <label className="block text-[10px] text-dark-500 mb-0.5">{t('balancer.httpchk_expect')}</label>
                <input type="text" value={opts.httpchk_expect ?? ''} onChange={e => upd({ httpchk_expect: e.target.value || undefined })}
                  placeholder="status 200" className={inp} />
              </div>
            </div>
          )}

          {/* Sticky sessions */}
          <div>
            <label className="block text-xs text-dark-400 mb-1">{t('balancer.sticky')}</label>
            <select value={opts.sticky_type ?? ''} onChange={e => upd({ sticky_type: e.target.value || undefined })} className={inp}>
              <option value="">{t('balancer.sticky_none')}</option>
              <option value="cookie">{t('balancer.sticky_cookie')}</option>
              <option value="stick-table">{t('balancer.sticky_stick_table')}</option>
            </select>
          </div>

          {opts.sticky_type === 'cookie' && (
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="block text-[10px] text-dark-500 mb-0.5">{t('balancer.cookie_name')}</label>
                <input type="text" value={opts.cookie_name ?? ''} onChange={e => upd({ cookie_name: e.target.value || undefined })}
                  placeholder="SERVERID" className={inp} />
              </div>
              <div>
                <label className="block text-[10px] text-dark-500 mb-0.5">Options</label>
                <input type="text" value={opts.cookie_options ?? ''} onChange={e => upd({ cookie_options: e.target.value || undefined })}
                  placeholder="insert indirect nocache" className={inp} />
              </div>
            </div>
          )}

          {opts.sticky_type === 'stick-table' && (
            <div className="grid grid-cols-3 gap-2">
              <div>
                <label className="block text-[10px] text-dark-500 mb-0.5">Type</label>
                <select value={opts.stick_table_type ?? 'ip'} onChange={e => upd({ stick_table_type: e.target.value })} className={inp}>
                  <option value="ip">ip</option><option value="string">string</option>
                </select>
              </div>
              <div>
                <label className="block text-[10px] text-dark-500 mb-0.5">{t('balancer.stick_table_size')}</label>
                <input type="text" value={opts.stick_table_size ?? ''} onChange={e => upd({ stick_table_size: e.target.value || undefined })}
                  placeholder="200k" className={inp} />
              </div>
              <div>
                <label className="block text-[10px] text-dark-500 mb-0.5">{t('balancer.stick_table_expire')}</label>
                <input type="text" value={opts.stick_table_expire ?? ''} onChange={e => upd({ stick_table_expire: e.target.value || undefined })}
                  placeholder="30m" className={inp} />
              </div>
            </div>
          )}

          {/* Reliability */}
          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="block text-xs text-dark-400 mb-1">{t('balancer.retries')}</label>
              <input type="number" value={opts.retries ?? 3} onChange={e => upd({ retries: parseInt(e.target.value) || 3 })}
                className={inp} min={0} />
            </div>
            <div>
              <label className="block text-xs text-dark-400 mb-1">{t('balancer.timeout_queue')}</label>
              <input type="text" value={opts.timeout_queue ?? ''} onChange={e => upd({ timeout_queue: e.target.value || undefined })}
                placeholder="30s" className={inp} />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="block text-xs text-dark-400 mb-1">{t('balancer.fullconn')}</label>
              <input type="number" value={opts.fullconn ?? ''} onChange={e => upd({ fullconn: e.target.value ? parseInt(e.target.value) : undefined })}
                placeholder="1000" className={inp} />
            </div>
          </div>
          <div className="flex flex-wrap gap-x-5 gap-y-1">
            <Toggle value={opts.redispatch ?? true} onChange={v => upd({ redispatch: v })} label={t('balancer.redispatch')} />
            <Toggle value={opts.allbackups ?? false} onChange={v => upd({ allbackups: v })} label={t('balancer.allbackups')} />
          </div>
        </div>
      )}
    </div>
  )
}

// ==================== Rule Form ====================

interface RuleFormData {
  name: string
  listen_port: string
  target_ip: string
  target_port: string
  send_proxy: boolean
  is_balancer: boolean
  servers: BackendServer[]
  balancer_options: BalancerOptions
}

const EMPTY_RULE_FORM: RuleFormData = {
  name: '', listen_port: '', target_ip: '', target_port: '', send_proxy: false,
  is_balancer: false, servers: [], balancer_options: { ...DEFAULT_BALANCER_OPTIONS },
}

function RuleForm({
  initial,
  isEdit,
  saving,
  onSave,
  onCancel,
  profileId,
}: {
  initial: RuleFormData
  isEdit: boolean
  saving: boolean
  onSave: (data: RuleFormData) => void
  onCancel: () => void
  profileId: number
}) {
  const { t } = useTranslation()
  const [form, setForm] = useState(initial)

  const toggleBalancer = (enabled: boolean) => {
    if (enabled && form.servers.length === 0) {
      const first: BackendServer = {
        ...DEFAULT_SERVER,
        address: form.target_ip || '', port: parseInt(form.target_port) || 0,
        send_proxy: form.send_proxy,
      }
      setForm(f => ({ ...f, is_balancer: true, servers: [first], balancer_options: f.balancer_options || { ...DEFAULT_BALANCER_OPTIONS } }))
    } else if (!enabled && form.servers.length > 0) {
      const first = form.servers[0]
      setForm(f => ({ ...f, is_balancer: false, target_ip: first.address, target_port: String(first.port), send_proxy: first.send_proxy ?? false }))
    } else {
      setForm(f => ({ ...f, is_balancer: enabled }))
    }
  }

  const updateServer = (i: number, srv: BackendServer) => {
    setForm(f => ({ ...f, servers: f.servers.map((s, idx) => idx === i ? srv : s) }))
  }

  const removeServer = (i: number) => {
    setForm(f => ({ ...f, servers: f.servers.filter((_, idx) => idx !== i) }))
  }

  const addServer = () => {
    const num = form.servers.length + 1
    setForm(f => ({ ...f, servers: [...f.servers, { ...DEFAULT_SERVER, name: `srv${num}` }] }))
  }

  const autoWeights = async () => {
    if (form.servers.length === 0) return
    const addresses = form.servers.map(s => s.address).filter(Boolean)
    if (addresses.length === 0) return

    try {
      const res = await haproxyProfilesApi.getServerCores(profileId, addresses)
      const coresMap = res.data  // { "nd2.nexyonn.com": 6, "nd.nexyonn.com": 4 }

      const serverCores = form.servers.map(s => coresMap[s.address] || 0)
      const totalCores = serverCores.reduce((a, b) => a + b, 0)

      if (totalCores === 0) {
        setForm(f => ({
          ...f,
          servers: f.servers.map(s => ({ ...s, weight: Math.round(100 / f.servers.length) })),
        }))
        return
      }

      setForm(f => ({
        ...f,
        servers: f.servers.map((s, i) => ({
          ...s,
          weight: Math.max(1, Math.min(256, Math.round((serverCores[i] / totalCores) * 100 * f.servers.length))),
        })),
      }))
    } catch {
      setForm(f => ({
        ...f,
        servers: f.servers.map(s => ({ ...s, weight: Math.round(100 / f.servers.length) })),
      }))
    }
  }

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
          {isEdit ? <><Edit3 className="w-3.5 h-3.5 text-accent-500" /> {t('haproxy_configs.edit_rule')}</> : <><Plus className="w-3.5 h-3.5 text-accent-500" /> {t('haproxy_configs.new_rule')}</>}
        </h4>
        <button onClick={onCancel} className="p-1 hover:bg-dark-700 rounded-lg text-dark-400 transition-colors">
          <X className="w-4 h-4" />
        </button>
      </div>

      <div className="space-y-3">
        {/* Name + Listen Port */}
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-xs text-dark-400 mb-1">{t('common.name')}</label>
            <input type="text" value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
              placeholder="my-proxy" className={inp} disabled={isEdit} />
          </div>
          <div>
            <label className="block text-xs text-dark-400 mb-1">{t('haproxy.listen_port')}</label>
            <input type="number" value={form.listen_port} onChange={e => setForm(f => ({ ...f, listen_port: e.target.value }))}
              placeholder="443" className={inp} />
          </div>
        </div>

        {/* Balancer toggle */}
        <div className="flex items-center gap-2">
          <span className="text-xs text-dark-300">{t('balancer.enable')}</span>
          <button type="button" onClick={() => toggleBalancer(!form.is_balancer)}
            className={`relative w-9 h-5 rounded-full transition-colors duration-200 ${form.is_balancer ? 'bg-green-500' : 'bg-dark-600'}`}>
            <span className={`absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full shadow transition-transform duration-200 ${form.is_balancer ? 'translate-x-4' : 'translate-x-0'}`} />
          </button>
        </div>

        {!form.is_balancer ? (
          /* Simple mode */
          <>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs text-dark-400 mb-1">{t('haproxy.target_ip')}</label>
                <input type="text" value={form.target_ip} onChange={e => setForm(f => ({ ...f, target_ip: e.target.value }))}
                  placeholder="192.168.1.10" className={inp} />
              </div>
              <div>
                <label className="block text-xs text-dark-400 mb-1">{t('haproxy.target_port')}</label>
                <input type="number" value={form.target_port} onChange={e => setForm(f => ({ ...f, target_port: e.target.value }))}
                  placeholder="8080" className={inp} />
              </div>
            </div>
            <Toggle value={form.send_proxy} onChange={v => setForm(f => ({ ...f, send_proxy: v }))} label={t('haproxy.send_proxy')} />
          </>
        ) : (
          /* Balancer mode */
          <>
            {/* Algorithm + Settings */}
            <BalancerSettingsSection
              opts={form.balancer_options || { ...DEFAULT_BALANCER_OPTIONS }}
              onChange={o => setForm(f => ({ ...f, balancer_options: o }))}
              t={t}
            />

            {/* Servers */}
            <div>
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs text-dark-400 font-medium">{t('balancer.servers')}</span>
                <div className="flex items-center gap-3">
                  {form.servers.length > 1 && (
                    <button type="button" onClick={autoWeights}
                      className="flex items-center gap-1 text-xs text-dark-400 hover:text-dark-200 transition-colors"
                      title={t('balancer.auto_weight')}>
                      <Cpu className="w-3 h-3" /> {t('balancer.auto_weight')}
                    </button>
                  )}
                  <button type="button" onClick={addServer}
                    className="flex items-center gap-1 text-xs text-accent-400 hover:text-accent-300 transition-colors">
                    <Plus className="w-3 h-3" /> {t('balancer.add_server')}
                  </button>
                </div>
              </div>
              <div className="space-y-2">
                {form.servers.map((srv, i) => (
                  <BackendServerRow key={i} srv={srv} index={i} onChange={updateServer}
                    onRemove={removeServer} canRemove={form.servers.length > 1} t={t} />
                ))}
              </div>
            </div>
          </>
        )}

        {/* Actions */}
        <div className="flex justify-end gap-2 pt-1">
          <button onClick={onCancel} className="px-3 py-1.5 rounded-lg text-xs text-dark-300 hover:text-dark-100 bg-dark-800 hover:bg-dark-700 border border-dark-700 transition-colors">
            {t('common.cancel')}
          </button>
          <button onClick={() => onSave(form)} disabled={saving}
            className="px-3 py-1.5 rounded-lg text-xs font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors disabled:opacity-50 flex items-center gap-1.5">
            {saving && <Loader2 className="w-3 h-3 animate-spin" />}
            {isEdit ? t('common.save') : t('haproxy_configs.add_rule')}
          </button>
        </div>
      </div>
    </motion.div>
  )
}


// ==================== Profile Card (unchanged) ====================

function ProfileCard({
  profile, expanded, onExpand, onEdit, onDelete,
}: {
  profile: HAProxyConfigProfile; expanded: boolean
  onExpand: (id: number) => void; onEdit: (p: HAProxyConfigProfile) => void; onDelete: (id: number) => void
}) {
  const { t } = useTranslation()
  const allSynced = profile.linked_servers_count > 0 && profile.synced_servers_count === profile.linked_servers_count
  const hasUnsync = profile.linked_servers_count > 0 && profile.synced_servers_count < profile.linked_servers_count

  const totalNet = profile.total_net_rx + profile.total_net_tx
  const hasNet = profile.linked_servers_count > 0 && totalNet > 0

  return (
    <motion.div layout initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -10 }}
      className="rounded-xl border bg-dark-800/60 border-dark-700/80 transition-all duration-200 hover:border-dark-600/80">
      <div className="flex items-center justify-between px-4 py-3 cursor-pointer select-none" onClick={() => onExpand(profile.id)}>
        <div className="flex items-center gap-3 min-w-0">
          {expanded ? <ChevronDown className="w-4 h-4 text-dark-400 shrink-0" /> : <ChevronRight className="w-4 h-4 text-dark-400 shrink-0" />}
          <FileCode2 className="w-5 h-5 text-accent-400 shrink-0" />
          <div className="min-w-0">
            <div className="font-medium text-dark-100 truncate">{profile.name}</div>
            {profile.description && <div className="text-xs text-dark-400 truncate mt-0.5">{profile.description}</div>}
          </div>
        </div>
        <div className="flex items-center gap-3 shrink-0">
          <div className="flex items-center gap-1.5 text-xs text-dark-400"><Server className="w-3.5 h-3.5" /><span>{profile.linked_servers_count}</span></div>
          {hasNet && (
            <div className="hidden sm:flex items-center gap-1.5 text-sm font-mono text-dark-200 font-medium">
              <Activity className="w-4 h-4 text-accent-400" />
              <span>↓{formatBitsPerSec(profile.total_net_rx)} ↑{formatBitsPerSec(profile.total_net_tx)}</span>
            </div>
          )}
          {allSynced && profile.linked_servers_count > 0 && (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs border text-green-400 bg-green-500/10 border-green-500/20">
              <CheckCircle2 className="w-3 h-3" /> {t('haproxy_configs.all_synced')}
            </span>
          )}
          {hasUnsync && (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs border text-yellow-400 bg-yellow-500/10 border-yellow-500/20">
              <AlertCircle className="w-3 h-3" /> {profile.linked_servers_count - profile.synced_servers_count} {t('haproxy_configs.out_of_sync')}
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


// ==================== Profile Detail Panel (with rules GUI) ====================

function ProfileDetailPanel({ profileId, onRefreshList }: { profileId: number; onRefreshList: () => void }) {
  const { t } = useTranslation()
  const [detail, setDetail] = useState<HAProxyProfileDetail | null>(null)
  const [rules, setRules] = useState<HAProxyProfileRule[]>([])
  const [availableServers, setAvailableServers] = useState<HAProxyAvailableServer[]>([])
  const [serversStatus, setServersStatus] = useState<HAProxyServerStatus[]>([])
  const [syncLog, setSyncLog] = useState<HAProxySyncLogEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [syncing, setSyncing] = useState(false)
  const [syncingServerId, setSyncingServerId] = useState<number | null>(null)
  const [showLog, setShowLog] = useState(false)
  const [showAddServer, setShowAddServer] = useState(false)
  const [showRuleForm, setShowRuleForm] = useState(false)
  const [editingRules, setEditingRules] = useState<Set<string>>(new Set())
  const [ruleSaving, setRuleSaving] = useState(false)
  const [showConfig, setShowConfig] = useState(false)
  const [configEdit, setConfigEdit] = useState('')
  const [configSaving, setConfigSaving] = useState(false)
  const [serverSearch, setServerSearch] = useState('')
  const [configModalMouseDown, setConfigModalMouseDown] = useState(false)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const prevSyncedRef = useRef<string>('')

  const fetchServersStatus = useCallback(async () => {
    try {
      const res = await haproxyProfilesApi.getServersStatus(profileId)
      setServersStatus(res.data)

      // Обновить карточку профиля при изменении статусов синхронизации
      const syncKey = res.data.map((s: HAProxyServerStatus) => `${s.server_id}:${s.sync_status}`).join(',')
      if (prevSyncedRef.current && prevSyncedRef.current !== syncKey) {
        onRefreshList()
      }
      prevSyncedRef.current = syncKey
    } catch { /* silent */ }
  }, [profileId, onRefreshList])

  const fetchDetail = useCallback(async () => {
    try {
      const [detailRes, serversRes, rulesRes] = await Promise.all([
        haproxyProfilesApi.getProfile(profileId),
        haproxyProfilesApi.getAvailableServers(),
        haproxyProfilesApi.getRules(profileId),
      ])
      setDetail(detailRes.data)
      setAvailableServers(serversRes.data)
      setRules(rulesRes.data)
      setConfigEdit(detailRes.data.config_content)
      fetchServersStatus()
    } catch {
      toast.error(t('haproxy_configs.fetch_error'))
    } finally {
      setLoading(false)
    }
  }, [profileId, t, fetchServersStatus])

  useEffect(() => { fetchDetail() }, [fetchDetail])

  // Автообновление статусов серверов каждые 3 секунды
  useEffect(() => {
    pollRef.current = setInterval(fetchServersStatus, 3000)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [fetchServersStatus])

  // ---- Rules CRUD ----
  const buildRulePayload = (form: RuleFormData): Omit<HAProxyProfileRule, 'name'> & { name: string } => {
    if (form.is_balancer) {
      const first = form.servers[0]
      return {
        name: form.name, rule_type: 'tcp',
        listen_port: parseInt(form.listen_port) || 0,
        target_ip: first?.address || '', target_port: first?.port || 0,
        send_proxy: false, is_balancer: true,
        servers: form.servers, balancer_options: form.balancer_options,
      }
    }
    return {
      name: form.name, rule_type: 'tcp',
      listen_port: parseInt(form.listen_port) || 0,
      target_ip: form.target_ip, target_port: parseInt(form.target_port) || 0,
      send_proxy: form.send_proxy, is_balancer: false,
    }
  }

  const handleAddRule = async (form: RuleFormData) => {
    if (!form.name || !form.listen_port) {
      toast.error(t('haproxy_configs.rule_fields_required')); return
    }
    if (!form.is_balancer && (!form.target_ip || !form.target_port)) {
      toast.error(t('haproxy_configs.rule_fields_required')); return
    }
    if (form.is_balancer && form.servers.length === 0) {
      toast.error(t('balancer.min_one_server')); return
    }
    setRuleSaving(true)
    try {
      const res = await haproxyProfilesApi.addRule(profileId, buildRulePayload(form))
      setRules(res.data.rules)
      setShowRuleForm(false)
      toast.success(t('haproxy_configs.rule_added'))
      fetchDetail()
      onRefreshList()
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('haproxy_configs.rule_error'))
    } finally { setRuleSaving(false) }
  }

  const handleUpdateRule = async (form: RuleFormData) => {
    setRuleSaving(true)
    try {
      const res = await haproxyProfilesApi.updateRule(profileId, form.name, buildRulePayload(form))
      setRules(res.data.rules)
      setEditingRules(prev => { const next = new Set(prev); next.delete(form.name); return next })
      toast.success(t('haproxy_configs.rule_updated'))
      fetchDetail()
      onRefreshList()
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('haproxy_configs.rule_error'))
    } finally { setRuleSaving(false) }
  }

  const handleDeleteRule = async (ruleName: string) => {
    try {
      const res = await haproxyProfilesApi.deleteRule(profileId, ruleName)
      setRules(res.data.rules)
      toast.success(t('haproxy_configs.rule_deleted'))
      fetchDetail()
      onRefreshList()
    } catch {
      toast.error(t('haproxy_configs.rule_error'))
    }
  }

  const toggleEditRule = (rule: HAProxyProfileRule) => {
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
      await haproxyProfilesApi.updateProfile(profileId, { config_content: configEdit })
      toast.success(t('haproxy_configs.config_saved'))
      fetchDetail()
      onRefreshList()
    } catch {
      toast.error(t('haproxy_configs.save_error'))
    } finally { setConfigSaving(false) }
  }

  const handleApplyTemplate = async () => {
    try {
      const res = await haproxyProfilesApi.regenerateConfig(profileId)
      setConfigEdit(res.data.config_content)
      toast.success(t('haproxy_configs.template_applied'))
    } catch {
      toast.error(t('haproxy_configs.rule_error'))
    }
  }

  // ---- Sync ----
  const handleSyncAll = async () => {
    setSyncing(true)
    try {
      const res = await haproxyProfilesApi.syncAll(profileId)
      const results = res.data.results
      const ok = results.filter((r: HAProxySyncResult) => r.success).length
      const fail = results.filter((r: HAProxySyncResult) => !r.success).length
      if (fail === 0) toast.success(t('haproxy_configs.sync_success', { count: ok }))
      else toast.warning(t('haproxy_configs.sync_partial', { ok, fail }))
      await fetchDetail(); onRefreshList()
    } catch { toast.error(t('haproxy_configs.sync_error')) }
    finally { setSyncing(false) }
  }

  const handleSyncOne = async (serverId: number) => {
    setSyncingServerId(serverId)
    try {
      const res = await haproxyProfilesApi.syncOne(profileId, serverId)
      if (res.data.success) toast.success(res.data.message)
      else toast.error(res.data.message)
      await fetchDetail(); onRefreshList()
    } catch { toast.error(t('haproxy_configs.sync_error')) }
    finally { setSyncingServerId(null) }
  }

  const handleLinkServer = async (serverId: number) => {
    try {
      await haproxyProfilesApi.linkServer(profileId, serverId)
      toast.success(t('haproxy_configs.server_linked'))
      await fetchDetail(); onRefreshList(); setShowAddServer(false)
    } catch { toast.error(t('haproxy_configs.link_error')) }
  }

  const handleUnlinkServer = async (serverId: number) => {
    try {
      await haproxyProfilesApi.unlinkServer(profileId, serverId)
      toast.success(t('haproxy_configs.server_unlinked'))
      await fetchDetail(); onRefreshList()
    } catch { toast.error(t('haproxy_configs.unlink_error')) }
  }


  const fetchLog = async () => {
    try { const res = await haproxyProfilesApi.getSyncLog(profileId); setSyncLog(res.data) }
    catch { toast.error(t('haproxy_configs.log_error')) }
  }
  const toggleLog = () => { if (!showLog) fetchLog(); setShowLog(!showLog) }

  const unlinkedServers = availableServers
    .filter(s => s.active_profile_id === null || s.active_profile_id !== profileId)
    .filter(s => {
      if (!serverSearch) return true
      const q = serverSearch.toLowerCase()
      return s.name.toLowerCase().includes(q) || s.url.toLowerCase().includes(q)
    })

  if (loading) return <div className="flex items-center justify-center py-12"><Loader2 className="w-6 h-6 text-accent-400 animate-spin" /></div>
  if (!detail) return null

  return (
    <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }}
      className="border-t border-dark-700/50">
      <div className="p-4 space-y-4">

        {/* ===== Rules Section ===== */}
        <div>
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-medium text-dark-200">{t('haproxy_configs.rules')}</h3>
            <div className="flex items-center gap-2">
              <button onClick={() => { setShowConfig(!showConfig); if (!showConfig) setConfigEdit(detail.config_content) }}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs text-dark-300 hover:text-dark-100 bg-dark-800/50 hover:bg-dark-700/50 border border-dark-700/50 transition-colors">
                <Code className="w-3.5 h-3.5" /> {t('haproxy_configs.show_config')}
              </button>
              {!showRuleForm && (
                <button onClick={() => setShowRuleForm(true)}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors">
                  <Plus className="w-3.5 h-3.5" /> {t('haproxy_configs.add_rule')}
                </button>
              )}
            </div>
          </div>

          {/* Add Rule Form (for new rules only) */}
          <AnimatePresence>
            {showRuleForm && (
              <div className="mb-3">
                <RuleForm
                  initial={EMPTY_RULE_FORM}
                  isEdit={false}
                  saving={ruleSaving}
                  onSave={handleAddRule}
                  onCancel={() => { setShowRuleForm(false) }}
                  profileId={profileId}
                />
              </div>
            )}
          </AnimatePresence>

          {/* Rules List with inline edit */}
          {rules.length === 0 ? (
            <div className="text-center text-dark-500 text-sm py-6 bg-dark-900/30 rounded-lg border border-dark-800/50">
              {t('haproxy_configs.no_rules')}
            </div>
          ) : (
            <div className="space-y-1.5">
              {rules.map(r => {
                const isEditing = editingRules.has(r.name)
                const editInitial: RuleFormData = {
                  name: r.name, listen_port: String(r.listen_port),
                  target_ip: r.target_ip, target_port: String(r.target_port),
                  send_proxy: r.send_proxy, is_balancer: r.is_balancer ?? false,
                  servers: r.servers ?? [],
                  balancer_options: r.balancer_options ? { ...DEFAULT_BALANCER_OPTIONS, ...r.balancer_options } : { ...DEFAULT_BALANCER_OPTIONS },
                }
                return (
                  <div key={r.name}>
                    <div
                      className={`flex items-center justify-between px-3 py-2 rounded-lg border cursor-pointer transition-colors ${isEditing ? 'bg-dark-800/60 border-accent-500/30' : 'bg-dark-900/30 border-dark-800/50 hover:bg-dark-800/40'} group`}
                      onClick={() => toggleEditRule(r)}
                    >
                      <div className="flex items-center gap-3 min-w-0">
                        <span className="text-sm text-dark-200 font-medium">{r.name}</span>
                        {r.is_balancer ? (
                          <>
                            <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium bg-accent-500/10 text-accent-400 border border-accent-500/20">
                              <Scale className="w-2.5 h-2.5" /> LB
                            </span>
                            <span className="text-xs text-dark-500">
                              :{r.listen_port} → {r.servers?.length ?? 0} {t('balancer.servers').toLowerCase()}
                            </span>
                            <span className="text-[10px] text-dark-500 hidden sm:block">{r.balancer_options?.algorithm}</span>
                          </>
                        ) : (
                          <>
                            <span className="text-xs text-dark-500">
                              :{r.listen_port} → {r.target_ip}:{r.target_port}
                            </span>
                            {r.send_proxy && <span className="text-[10px] text-yellow-400/60 hidden sm:block">PROXY</span>}
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
                              profileId={profileId}
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
                      <h2 className="text-lg font-semibold text-dark-100">{t('haproxy_configs.raw_config')}</h2>
                      <p className="text-xs text-dark-500">{detail?.name}</p>
                    </div>
                  </div>
                  <motion.button onClick={() => setShowConfig(false)}
                    className="p-2 hover:bg-dark-800 rounded-lg text-dark-400 transition-colors"
                    whileHover={{ scale: 1.1, rotate: 90 }} whileTap={{ scale: 0.9 }}>
                    <X className="w-5 h-5" />
                  </motion.button>
                </div>
                <div className="flex-1 overflow-hidden p-5">
                  <textarea value={configEdit} onChange={e => setConfigEdit(e.target.value)} spellCheck={false}
                    className="w-full h-[50vh] px-4 py-3 rounded-xl bg-dark-950 border border-dark-700 text-dark-200 text-sm font-mono focus:outline-none focus:border-accent-500/50 transition-colors resize-y
                      scrollbar-thin scrollbar-thumb-dark-700 scrollbar-track-transparent" />
                  <div className="flex items-center gap-1.5 mt-2 text-xs text-dark-500">
                    <AlertTriangle className="w-3.5 h-3.5" /> {t('haproxy_configs.raw_config_warning')}
                  </div>
                </div>
                <div className="flex items-center justify-between p-5 border-t border-dark-700">
                  <button onClick={handleApplyTemplate}
                    className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm text-dark-300 hover:text-dark-100 bg-dark-800 hover:bg-dark-700 border border-dark-700 transition-colors">
                    <RefreshCw className="w-4 h-4" /> {t('haproxy_configs.apply_template')}
                  </button>
                  <div className="flex items-center gap-3">
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
            <h3 className="text-sm font-medium text-dark-200">{t('haproxy_configs.linked_servers')}</h3>
            <div className="flex items-center gap-2">
              <button onClick={toggleLog}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs text-dark-300 hover:text-dark-100 bg-dark-800/50 hover:bg-dark-700/50 border border-dark-700/50 transition-colors">
                <History className="w-3.5 h-3.5" /> {t('haproxy_configs.sync_log')}
              </button>
              <button onClick={() => setShowAddServer(!showAddServer)}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs text-dark-300 hover:text-dark-100 bg-dark-800/50 hover:bg-dark-700/50 border border-dark-700/50 transition-colors">
                <Link2 className="w-3.5 h-3.5" /> {t('haproxy_configs.add_server')}
              </button>
              {serversStatus.length > 0 && (
                <button onClick={handleSyncAll} disabled={syncing}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors disabled:opacity-50">
                  {syncing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />} {t('haproxy_configs.sync_all')}
                </button>
              )}
            </div>
          </div>

          <AnimatePresence>
            {showAddServer && (
              <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }} className="mb-3 overflow-hidden">
                <div className="rounded-lg border border-dark-700/50 bg-dark-900/50 p-3">
                  <div className="text-xs text-dark-400 mb-2">{t('haproxy_configs.select_server')}</div>
                  <input type="text" value={serverSearch} onChange={e => setServerSearch(e.target.value)}
                    placeholder={t('haproxy_configs.search_server')}
                    className="w-full px-3 py-1.5 mb-2 rounded-lg bg-dark-800 border border-dark-700 text-dark-100 text-sm focus:outline-none focus:border-accent-500/50 transition-colors" autoFocus />
                  {unlinkedServers.length === 0 ? (
                    <div className="text-xs text-dark-500">{t('haproxy_configs.no_available_servers')}</div>
                  ) : (
                    <div className="space-y-1 max-h-48 overflow-y-auto">
                      {unlinkedServers.map(s => (
                        <button key={s.id} onClick={() => handleLinkServer(s.id)}
                          className="w-full flex items-center justify-between px-3 py-2 rounded-lg text-sm text-dark-200 hover:bg-dark-700/50 transition-colors">
                          <span className="flex items-center gap-2"><Server className="w-3.5 h-3.5 text-dark-400" />{s.name}</span>
                          {s.active_profile_id && <span className="text-xs text-dark-500">{t('haproxy_configs.has_other_profile')}</span>}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              </motion.div>
            )}
          </AnimatePresence>

          {serversStatus.length === 0 && detail.servers.length === 0 ? (
            <div className="text-center text-dark-500 text-sm py-6 bg-dark-900/30 rounded-lg border border-dark-800/50">{t('haproxy_configs.no_servers')}</div>
          ) : (
            <div className="space-y-1.5">
              {serversStatus.map(s => {
                const m = s.metrics
                const fmtSpeed = (v: number | null | undefined) => {
                  if (v == null) return '-'
                  return formatBitsPerSec(v)
                }
                const laPercent = m?.la1 != null && m?.cores ? (m.la1 / m.cores * 100) : null
                return (
                  <div key={s.server_id} className="flex items-center justify-between px-3 py-2 rounded-lg bg-dark-900/30 border border-dark-800/50">
                    <div className="flex items-center gap-2.5 min-w-0">
                      <div className={`w-2 h-2 rounded-full shrink-0 ${s.haproxy_running ? 'bg-green-400' : s.haproxy_running === false ? 'bg-red-400' : 'bg-dark-600'}`}
                        title={s.haproxy_running ? 'HAProxy running' : s.haproxy_running === false ? 'HAProxy stopped' : 'Unknown'} />
                      <span className="text-sm text-dark-200 truncate">{s.server_name}</span>
                      <SyncStatusBadge status={s.sync_status} />
                    </div>
                    <div className="flex items-center gap-3 shrink-0">
                      {m && (() => {
                        const pctColor = (v: number) => v < 50 ? 'text-green-400' : v < 80 ? 'text-yellow-400' : 'text-red-400'
                        return (
                          <div className="hidden sm:flex items-center gap-3 text-xs font-mono">
                            {m.cpu != null && <span><span className="text-dark-500 mr-1">CPU</span><span className={pctColor(m.cpu)}>{m.cpu.toFixed(0)}%</span></span>}
                            {m.ram != null && <span><span className="text-dark-500 mr-1">RAM</span><span className={pctColor(m.ram)}>{m.ram.toFixed(0)}%</span></span>}
                            {laPercent != null && <span><span className="text-dark-500 mr-1">LA</span><span className={pctColor(laPercent)}>{m.la1!.toFixed(2)}</span></span>}
                            {(m.net_rx != null || m.net_tx != null) && <span><span className="text-dark-500 mr-1">NET</span><span className="text-dark-200">↓{fmtSpeed(m.net_rx)} ↑{fmtSpeed(m.net_tx)}</span></span>}
                          </div>
                        )
                      })()}
                      <div className="flex items-center gap-1">
                        <Tooltip label={t('haproxy_configs.sync_server')}>
                          <button onClick={() => handleSyncOne(s.server_id)} disabled={syncingServerId === s.server_id}
                            className="p-1.5 rounded-lg text-dark-400 hover:text-accent-400 hover:bg-accent-500/10 transition-colors">
                            {syncingServerId === s.server_id ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
                          </button>
                        </Tooltip>
                        <Tooltip label={t('haproxy_configs.unlink_server')}>
                          <button onClick={() => handleUnlinkServer(s.server_id)}
                            className="p-1.5 rounded-lg text-dark-400 hover:text-red-400 hover:bg-red-500/10 transition-colors">
                            <Unlink className="w-3.5 h-3.5" />
                          </button>
                        </Tooltip>
                      </div>
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
                <div className="text-xs font-medium text-dark-300 mb-2">{t('haproxy_configs.recent_sync_log')}</div>
                {syncLog.length === 0 ? (
                  <div className="text-xs text-dark-500">{t('haproxy_configs.no_log')}</div>
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

function ProfileModal({ profile, onClose, onSaved }: { profile: HAProxyConfigProfile | null; onClose: () => void; onSaved: () => void }) {
  const { t } = useTranslation()
  const isEdit = !!profile
  const [name, setName] = useState(profile?.name || '')
  const [description, setDescription] = useState(profile?.description || '')
  const [saving, setSaving] = useState(false)

  const handleSave = async () => {
    if (!name.trim()) { toast.error(t('haproxy_configs.name_required')); return }
    setSaving(true)
    try {
      if (isEdit) {
        await haproxyProfilesApi.updateProfile(profile!.id, { name: name.trim(), description: description.trim() || undefined })
        toast.success(t('haproxy_configs.profile_updated'))
      } else {
        await haproxyProfilesApi.createProfile({ name: name.trim(), description: description.trim() || undefined })
        toast.success(t('haproxy_configs.profile_created'))
      }
      onSaved(); onClose()
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('haproxy_configs.save_error'))
    } finally { setSaving(false) }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />
      <motion.div initial={{ opacity: 0, scale: 0.95, y: 10 }} animate={{ opacity: 1, scale: 1, y: 0 }} exit={{ opacity: 0, scale: 0.95, y: 10 }}
        className="relative w-full max-w-lg bg-dark-900 border border-dark-700/80 rounded-2xl shadow-2xl overflow-hidden">
        <div className="flex items-center justify-between px-6 py-4 border-b border-dark-800">
          <h2 className="text-lg font-semibold text-dark-100">{isEdit ? t('haproxy_configs.edit_profile') : t('haproxy_configs.create_profile')}</h2>
          <button onClick={onClose} className="p-1.5 rounded-lg text-dark-400 hover:text-dark-200 hover:bg-dark-700/50 transition-colors"><X className="w-5 h-5" /></button>
        </div>
        <div className="p-6 space-y-4">
          <div>
            <label className="block text-xs font-medium text-dark-300 mb-1.5">{t('haproxy_configs.profile_name')}</label>
            <input type="text" value={name} onChange={e => setName(e.target.value)} placeholder={t('haproxy_configs.name_placeholder')}
              className="w-full px-3 py-2 rounded-lg bg-dark-800 border border-dark-700 text-dark-100 text-sm focus:outline-none focus:border-accent-500/50 transition-colors" autoFocus />
          </div>
          <div>
            <label className="block text-xs font-medium text-dark-300 mb-1.5">{t('haproxy_configs.description')}</label>
            <input type="text" value={description} onChange={e => setDescription(e.target.value)} placeholder={t('haproxy_configs.description_placeholder')}
              className="w-full px-3 py-2 rounded-lg bg-dark-800 border border-dark-700 text-dark-100 text-sm focus:outline-none focus:border-accent-500/50 transition-colors" />
          </div>
          {!isEdit && <div className="text-xs text-dark-500">{t('haproxy_configs.default_config_hint')}</div>}
        </div>
        <div className="flex items-center justify-end gap-3 px-6 py-4 border-t border-dark-800">
          <button onClick={onClose} className="px-4 py-2 rounded-lg text-sm text-dark-300 hover:text-dark-100 bg-dark-800 hover:bg-dark-700 border border-dark-700 transition-colors">{t('common.cancel')}</button>
          <button onClick={handleSave} disabled={saving}
            className="px-4 py-2 rounded-lg text-sm font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors disabled:opacity-50 flex items-center gap-2">
            {saving && <Loader2 className="w-4 h-4 animate-spin" />}
            {isEdit ? t('common.save') : t('haproxy_configs.create')}
          </button>
        </div>
      </motion.div>
    </div>
  )
}


// ==================== Main Page ====================

export default function HAProxyConfigs() {
  const { t } = useTranslation()
  const [profiles, setProfiles] = useState<HAProxyConfigProfile[]>([])
  const [loading, setLoading] = useState(true)
  const [expandedId, setExpandedId] = useState<number | null>(null)
  const [modalProfile, setModalProfile] = useState<HAProxyConfigProfile | null | 'new'>(null)

  const initialLoadDone = useRef(false)

  const fetchProfiles = useCallback(async () => {
    try {
      const res = await haproxyProfilesApi.getProfiles()
      setProfiles(res.data)
    } catch {
      if (!initialLoadDone.current) toast.error(t('haproxy_configs.fetch_error'))
    } finally {
      if (!initialLoadDone.current) { initialLoadDone.current = true; setLoading(false) }
    }
  }, [t])

  useEffect(() => { fetchProfiles() }, [fetchProfiles])

  useEffect(() => {
    const id = setInterval(fetchProfiles, 3000)
    return () => clearInterval(id)
  }, [fetchProfiles])

  const handleExpand = (id: number) => setExpandedId(prev => prev === id ? null : id)

  const handleDelete = async (id: number) => {
    try {
      await haproxyProfilesApi.deleteProfile(id)
      toast.success(t('haproxy_configs.profile_deleted'))
      if (expandedId === id) setExpandedId(null)
      await fetchProfiles()
    } catch { toast.error(t('haproxy_configs.delete_error')) }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="p-2.5 rounded-xl bg-gradient-to-br from-dark-800 to-dark-900 border border-dark-700/50">
            <FileCode2 className="w-6 h-6 text-accent-400" />
          </div>
          <div>
            <h1 className="text-xl font-bold text-dark-100 flex items-center gap-2">
              {t('haproxy_configs.title')}
              <FAQIcon screen="PAGE_HAPROXY_CONFIGS" />
            </h1>
            <p className="text-sm text-dark-400">{t('haproxy_configs.subtitle')}</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={fetchProfiles} className="p-2 rounded-lg text-dark-400 hover:text-dark-200 hover:bg-dark-800/50 border border-dark-700/50 transition-colors">
            <RefreshCw className="w-4 h-4" />
          </button>
          <button onClick={() => setModalProfile('new')} className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium bg-accent-600 hover:bg-accent-500 text-white transition-colors">
            <Plus className="w-4 h-4" /> {t('haproxy_configs.create_profile')}
          </button>
        </div>
      </div>

      {loading ? (
        <div className="flex items-center justify-center py-16"><Loader2 className="w-6 h-6 text-accent-400 animate-spin" /></div>
      ) : profiles.length === 0 ? (
        <div className="text-center py-16 text-dark-500">
          <FileCode2 className="w-12 h-12 mx-auto mb-3 opacity-30" />
          <p className="text-lg">{t('haproxy_configs.no_profiles')}</p>
          <p className="text-sm mt-1">{t('haproxy_configs.no_profiles_hint')}</p>
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
