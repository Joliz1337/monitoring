import { useEffect, useState, useCallback, useMemo, useRef, FormEvent } from 'react'
import { useNodeCapabilities } from '../hooks/useNodeCapabilities'
import { nodeAllows } from '../utils/nodeCapabilities'
import { ShieldCheck, RefreshCw, Server, Upload, Globe, Loader2, CheckCircle2, XCircle, Trash2, Eye, EyeOff, Save, Search, Send, Settings2, Info, ChevronDown, ChevronRight, Folder, FolderOpen, ToggleLeft, ToggleRight, Lock, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { motion, AnimatePresence } from 'framer-motion'
import { toast } from 'sonner'
import {
  wildcardSSLApi,
  wildcardDeployStreamUrl,
  WildcardCertificate,
  WildcardDeployResult,
  WildcardSSLSettings,
  WildcardServerConfig,
  WildcardServerConfigPatch,
} from '../api/client'
import { FAQIcon } from '../components/FAQ'
import { Checkbox } from '../components/ui/Checkbox'
import CertificateMaterials from '../components/wildcard/CertificateMaterials'
import { WildcardDeployProgress } from '../components/wildcard/WildcardDeployProgress'
import { useBulkStream, BulkStreamState } from '../hooks/useBulkStream'

const DEFAULT_DEPLOY_PATH = '/etc/letsencrypt/live'
const DEFAULT_FULLCHAIN_NAME = 'fullchain.pem'
const DEFAULT_PRIVKEY_NAME = 'privkey.pem'
const NO_FOLDER = '__no_folder__'

// Wildcard действует ровно на один уровень: *.example.com покрывает
// panel.example.com, но не a.b.example.com (та же логика на бэкенде)
function wildcardCoversDomain(baseDomain: string, domain: string): boolean {
  if (!baseDomain || !domain) return false
  if (domain === baseDomain) return true
  const suffix = '.' + baseDomain
  return domain.endsWith(suffix) && !domain.slice(0, -suffix.length).includes('.')
}

export interface ServerSavePayload {
  deploy_path: string
  reload_cmd: string
  fullchain_name: string
  privkey_name: string
  custom_path_enabled: boolean
  custom_fullchain_path: string
  custom_privkey_path: string
}


function ServerCard({
  srv,
  cert,
  deployingServer,
  expanded,
  selected,
  onToggle,
  onExpand,
  onSelect,
  onSave,
  onDeploy,
  restricted,
  t,
}: {
  srv: WildcardServerConfig
  cert: WildcardCertificate | null
  deployingServer: number | null
  expanded: boolean
  selected: boolean
  onToggle: (id: number, enabled: boolean) => void
  onExpand: (id: number) => void
  onSelect: (id: number) => void
  onSave: (id: number, data: ServerSavePayload) => void
  onDeploy: (id: number) => void
  restricted: boolean
  t: (key: string, opts?: any) => string
}) {
  const [localPath, setLocalPath] = useState(srv.wildcard_ssl_deploy_path)
  const [localCmd, setLocalCmd] = useState(srv.wildcard_ssl_reload_cmd)
  const [localFullchainName, setLocalFullchainName] = useState(srv.wildcard_ssl_fullchain_name)
  const [localPrivkeyName, setLocalPrivkeyName] = useState(srv.wildcard_ssl_privkey_name)
  const [localCustomMode, setLocalCustomMode] = useState(srv.wildcard_ssl_custom_path_enabled)
  const [localCustomFullchainPath, setLocalCustomFullchainPath] = useState(srv.wildcard_ssl_custom_fullchain_path)
  const [localCustomPrivkeyPath, setLocalCustomPrivkeyPath] = useState(srv.wildcard_ssl_custom_privkey_path)
  const [dirty, setDirty] = useState(false)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    setLocalPath(srv.wildcard_ssl_deploy_path)
    setLocalCmd(srv.wildcard_ssl_reload_cmd)
    setLocalFullchainName(srv.wildcard_ssl_fullchain_name)
    setLocalPrivkeyName(srv.wildcard_ssl_privkey_name)
    setLocalCustomMode(srv.wildcard_ssl_custom_path_enabled)
    setLocalCustomFullchainPath(srv.wildcard_ssl_custom_fullchain_path)
    setLocalCustomPrivkeyPath(srv.wildcard_ssl_custom_privkey_path)
    setDirty(false)
  }, [
    srv.wildcard_ssl_deploy_path,
    srv.wildcard_ssl_reload_cmd,
    srv.wildcard_ssl_fullchain_name,
    srv.wildcard_ssl_privkey_name,
    srv.wildcard_ssl_custom_path_enabled,
    srv.wildcard_ssl_custom_fullchain_path,
    srv.wildcard_ssl_custom_privkey_path,
  ])

  const markDirty = () => {
    setDirty(true)
    setSaved(false)
  }

  const handlePathChange = (val: string) => { setLocalPath(val); markDirty() }
  const handleCmdChange = (val: string) => { setLocalCmd(val); markDirty() }
  const handleFullchainNameChange = (val: string) => { setLocalFullchainName(val); markDirty() }
  const handlePrivkeyNameChange = (val: string) => { setLocalPrivkeyName(val); markDirty() }
  const handleCustomFullchainPathChange = (val: string) => { setLocalCustomFullchainPath(val); markDirty() }
  const handleCustomPrivkeyPathChange = (val: string) => { setLocalCustomPrivkeyPath(val); markDirty() }
  const handleCustomModeToggle = () => {
    setLocalCustomMode(prev => !prev)
    markDirty()
  }

  const handleSave = () => {
    onSave(srv.server_id, {
      deploy_path: localPath,
      reload_cmd: localCmd,
      fullchain_name: localFullchainName,
      privkey_name: localPrivkeyName,
      custom_path_enabled: localCustomMode,
      custom_fullchain_path: localCustomFullchainPath,
      custom_privkey_path: localCustomPrivkeyPath,
    })
    setDirty(false)
    setSaved(true)
    setTimeout(() => setSaved(false), 2000)
  }

  const basePath = localPath || DEFAULT_DEPLOY_PATH
  const domain = cert?.base_domain || 'example.com'
  const folderPath = `${basePath.replace(/\/+$/, '')}/${domain}`
  const previewFullchain = localCustomMode
    ? (localCustomFullchainPath || '/etc/pve/local/pveproxy-ssl.pem')
    : `${folderPath}/${localFullchainName || DEFAULT_FULLCHAIN_NAME}`
  const previewPrivkey = localCustomMode
    ? (localCustomPrivkeyPath || '/etc/pve/local/pveproxy-ssl.key')
    : `${folderPath}/${localPrivkeyName || DEFAULT_PRIVKEY_NAME}`
  const isEnabled = srv.wildcard_ssl_enabled

  return (
    <div className={`rounded-xl border transition-all duration-200 ${
      selected
        ? 'bg-accent-500/10 border-accent-500/30'
        : isEnabled
          ? 'bg-dark-800/60 border-dark-700/80'
          : 'bg-dark-800/20 border-dark-800/50'
    }`}>
      {/* Header: checkbox | toggle | clickable area (name) | deploy button */}
      <div className="flex items-center px-4 py-3 gap-3">
        <Checkbox
          checked={selected}
          onClick={e => e.stopPropagation()}
          onChange={() => onSelect(srv.server_id)}
        />

        {/* Toggle — только вкл/выкл */}
        <button
          onClick={e => { e.stopPropagation(); onToggle(srv.server_id, !isEnabled) }}
          className={`relative shrink-0 w-9 h-5 rounded-full transition-colors duration-200 ${
            isEnabled ? 'bg-accent-500' : 'bg-dark-600'
          }`}
        >
          <span className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white shadow-sm transition-all duration-200 ${
            isEnabled ? 'translate-x-4' : 'translate-x-0'
          }`} />
        </button>

        {/* Кликабельная область — раскрытие настроек */}
        <button
          onClick={() => onExpand(srv.server_id)}
          className="flex-1 flex items-center justify-between min-w-0 group"
        >
          <span className={`text-sm font-medium transition-colors truncate ${
            isEnabled ? 'text-dark-100' : 'text-dark-500'
          }`}>
            {srv.server_name}
          </span>
          {restricted && (
            <Lock className="w-3.5 h-3.5 text-purple shrink-0 ml-2" />
          )}
          <ChevronRight className={`w-4 h-4 shrink-0 transition-all duration-200 ${
            expanded ? 'rotate-90 text-accent-400' : 'text-dark-600 group-hover:text-dark-400'
          }`} />
        </button>

        {/* Deploy button */}
        {cert && isEnabled && (
          <button
            onClick={e => { e.stopPropagation(); onDeploy(srv.server_id) }}
            disabled={deployingServer === srv.server_id}
            className="shrink-0 px-3 py-1.5 bg-blue-500/20 text-blue-400 rounded-lg text-xs hover:bg-blue-500/30 transition-colors disabled:opacity-50 flex items-center gap-1.5"
          >
            {deployingServer === srv.server_id
              ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
              : <Upload className="w-3.5 h-3.5" />}
            {t('wildcard_ssl.deploy_one')}
          </button>
        )}
      </div>

      {/* Expandable config — раскрывается по клику на карточку */}
      <AnimatePresence>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="px-4 pb-4 space-y-3 border-t border-dark-700/40 pt-3">
              <div className="flex items-center justify-between gap-3 px-1">
                <div className="min-w-0">
                  <div className="text-xs text-dark-300 font-medium">{t('wildcard_ssl.custom_path_mode')}</div>
                  <div className="text-[11px] text-dark-500 mt-0.5">{t('wildcard_ssl.custom_path_hint')}</div>
                </div>
                <button
                  type="button"
                  onClick={handleCustomModeToggle}
                  className={`relative shrink-0 w-9 h-5 rounded-full transition-colors duration-200 ${
                    localCustomMode ? 'bg-accent-500' : 'bg-dark-600'
                  }`}
                >
                  <span className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white shadow-sm transition-all duration-200 ${
                    localCustomMode ? 'translate-x-4' : 'translate-x-0'
                  }`} />
                </button>
              </div>

              {!localCustomMode ? (
                <>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <div>
                      <label className="block text-xs text-dark-400 mb-1">{t('wildcard_ssl.deploy_path')}</label>
                      <input
                        type="text"
                        value={localPath}
                        onChange={e => handlePathChange(e.target.value)}
                        placeholder={DEFAULT_DEPLOY_PATH}
                        className="w-full px-2.5 py-1.5 bg-dark-900 border border-dark-700 rounded-lg text-dark-200 text-sm placeholder-dark-600 focus:outline-none focus:border-accent-500"
                      />
                      {!localPath ? (
                        <p className="text-[11px] text-accent-400/70 mt-1">{t('wildcard_ssl.deploy_path_default')}</p>
                      ) : (
                        <p className="text-[11px] text-dark-500 mt-1">{t('wildcard_ssl.deploy_path_hint')}</p>
                      )}
                    </div>
                    <div>
                      <label className="block text-xs text-dark-400 mb-1">{t('wildcard_ssl.reload_cmd')}</label>
                      <input
                        type="text"
                        value={localCmd}
                        onChange={e => handleCmdChange(e.target.value)}
                        placeholder={t('wildcard_ssl.reload_cmd_placeholder')}
                        className="w-full px-2.5 py-1.5 bg-dark-900 border border-dark-700 rounded-lg text-dark-200 text-sm placeholder-dark-600 focus:outline-none focus:border-accent-500 font-mono"
                      />
                      {!localCmd ? (
                        <p className="text-[11px] text-dark-500 mt-1">{t('wildcard_ssl.reload_cmd_empty_hint')}</p>
                      ) : (
                        <p className="text-[11px] text-dark-500 mt-1">{t('wildcard_ssl.reload_cmd_hint')}</p>
                      )}
                    </div>
                  </div>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <div>
                      <label className="block text-xs text-dark-400 mb-1">{t('wildcard_ssl.fullchain_filename')}</label>
                      <input
                        type="text"
                        value={localFullchainName}
                        onChange={e => handleFullchainNameChange(e.target.value)}
                        placeholder={DEFAULT_FULLCHAIN_NAME}
                        className="w-full px-2.5 py-1.5 bg-dark-900 border border-dark-700 rounded-lg text-dark-200 text-sm placeholder-dark-600 focus:outline-none focus:border-accent-500 font-mono"
                      />
                      <p className="text-[11px] text-dark-500 mt-1">{t('wildcard_ssl.fullchain_filename_hint')}</p>
                    </div>
                    <div>
                      <label className="block text-xs text-dark-400 mb-1">{t('wildcard_ssl.privkey_filename')}</label>
                      <input
                        type="text"
                        value={localPrivkeyName}
                        onChange={e => handlePrivkeyNameChange(e.target.value)}
                        placeholder={DEFAULT_PRIVKEY_NAME}
                        className="w-full px-2.5 py-1.5 bg-dark-900 border border-dark-700 rounded-lg text-dark-200 text-sm placeholder-dark-600 focus:outline-none focus:border-accent-500 font-mono"
                      />
                      <p className="text-[11px] text-dark-500 mt-1">{t('wildcard_ssl.privkey_filename_hint')}</p>
                    </div>
                  </div>
                </>
              ) : (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label className="block text-xs text-dark-400 mb-1">{t('wildcard_ssl.custom_fullchain_path')}</label>
                    <input
                      type="text"
                      value={localCustomFullchainPath}
                      onChange={e => handleCustomFullchainPathChange(e.target.value)}
                      placeholder="/etc/pve/local/pveproxy-ssl.pem"
                      className="w-full px-2.5 py-1.5 bg-dark-900 border border-dark-700 rounded-lg text-dark-200 text-sm placeholder-dark-600 focus:outline-none focus:border-accent-500 font-mono"
                    />
                    <p className="text-[11px] text-dark-500 mt-1">{t('wildcard_ssl.custom_fullchain_path_hint')}</p>
                  </div>
                  <div>
                    <label className="block text-xs text-dark-400 mb-1">{t('wildcard_ssl.custom_privkey_path')}</label>
                    <input
                      type="text"
                      value={localCustomPrivkeyPath}
                      onChange={e => handleCustomPrivkeyPathChange(e.target.value)}
                      placeholder="/etc/pve/local/pveproxy-ssl.key"
                      className="w-full px-2.5 py-1.5 bg-dark-900 border border-dark-700 rounded-lg text-dark-200 text-sm placeholder-dark-600 focus:outline-none focus:border-accent-500 font-mono"
                    />
                    <p className="text-[11px] text-dark-500 mt-1">{t('wildcard_ssl.custom_privkey_path_hint')}</p>
                  </div>
                  <div className="sm:col-span-2">
                    <label className="block text-xs text-dark-400 mb-1">{t('wildcard_ssl.reload_cmd')}</label>
                    <input
                      type="text"
                      value={localCmd}
                      onChange={e => handleCmdChange(e.target.value)}
                      placeholder={t('wildcard_ssl.reload_cmd_placeholder')}
                      className="w-full px-2.5 py-1.5 bg-dark-900 border border-dark-700 rounded-lg text-dark-200 text-sm placeholder-dark-600 focus:outline-none focus:border-accent-500 font-mono"
                    />
                    {!localCmd ? (
                      <p className="text-[11px] text-dark-500 mt-1">{t('wildcard_ssl.reload_cmd_empty_hint')}</p>
                    ) : (
                      <p className="text-[11px] text-dark-500 mt-1">{t('wildcard_ssl.reload_cmd_hint')}</p>
                    )}
                  </div>
                </div>
              )}

              <p className="text-[11px] text-dark-600 font-mono bg-dark-900/50 px-2.5 py-1.5 rounded-lg break-all">
                ssl_certificate {previewFullchain};<br />
                ssl_certificate_key {previewPrivkey};
              </p>

              {dirty && (
                <motion.div
                  initial={{ opacity: 0, y: -5 }}
                  animate={{ opacity: 1, y: 0 }}
                  className="flex justify-end"
                >
                  <button
                    onClick={handleSave}
                    className="px-3 py-1.5 bg-accent-500 text-white rounded-lg text-xs hover:bg-accent-600 transition-colors flex items-center gap-1.5"
                  >
                    <Save className="w-3.5 h-3.5" />
                    {t('wildcard_ssl.server_save')}
                  </button>
                </motion.div>
              )}
              {saved && (
                <motion.div
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  className="flex justify-end"
                >
                  <span className="text-xs text-green-400 flex items-center gap-1">
                    <CheckCircle2 className="w-3.5 h-3.5" />
                    {t('wildcard_ssl.server_saved')}
                  </span>
                </motion.div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}


const KEEP = '__keep__'

type BulkTextFieldKey =
  | 'wildcard_ssl_deploy_path'
  | 'wildcard_ssl_reload_cmd'
  | 'wildcard_ssl_fullchain_name'
  | 'wildcard_ssl_privkey_name'
  | 'wildcard_ssl_custom_fullchain_path'
  | 'wildcard_ssl_custom_privkey_path'

const BULK_TEXT_FIELDS: { key: BulkTextFieldKey; labelKey: string; placeholder: string }[] = [
  { key: 'wildcard_ssl_deploy_path', labelKey: 'wildcard_ssl.deploy_path', placeholder: DEFAULT_DEPLOY_PATH },
  { key: 'wildcard_ssl_reload_cmd', labelKey: 'wildcard_ssl.reload_cmd', placeholder: '' },
  { key: 'wildcard_ssl_fullchain_name', labelKey: 'wildcard_ssl.fullchain_filename', placeholder: DEFAULT_FULLCHAIN_NAME },
  { key: 'wildcard_ssl_privkey_name', labelKey: 'wildcard_ssl.privkey_filename', placeholder: DEFAULT_PRIVKEY_NAME },
  { key: 'wildcard_ssl_custom_fullchain_path', labelKey: 'wildcard_ssl.custom_fullchain_path', placeholder: '/etc/pve/local/pveproxy-ssl.pem' },
  { key: 'wildcard_ssl_custom_privkey_path', labelKey: 'wildcard_ssl.custom_privkey_path', placeholder: '/etc/pve/local/pveproxy-ssl.key' },
]

// Массовое редактирование: невключённое поле не попадает в патч («не менять»),
// включённое и пустое — сбрасывает значение к дефолту
function WildcardBulkEditForm({
  count,
  saving,
  onSave,
  onClose,
  t,
}: {
  count: number
  saving: boolean
  onSave: (patch: WildcardServerConfigPatch) => void
  onClose: () => void
  t: (key: string, opts?: any) => string
}) {
  const [customMode, setCustomMode] = useState(KEEP)
  const [texts, setTexts] = useState<Record<BulkTextFieldKey, { on: boolean; value: string }>>(
    () => Object.fromEntries(
      BULK_TEXT_FIELDS.map(f => [f.key, { on: false, value: '' }])
    ) as Record<BulkTextFieldKey, { on: boolean; value: string }>
  )

  const setFieldOn = (key: BulkTextFieldKey, on: boolean) =>
    setTexts(prev => ({ ...prev, [key]: { ...prev[key], on } }))
  const setFieldValue = (key: BulkTextFieldKey, value: string) =>
    setTexts(prev => ({ ...prev, [key]: { ...prev[key], value } }))

  const handleSubmit = () => {
    const patch: WildcardServerConfigPatch = {}
    if (customMode !== KEEP) patch.wildcard_ssl_custom_path_enabled = customMode === 'on'
    for (const field of BULK_TEXT_FIELDS) {
      const state = texts[field.key]
      if (state.on) patch[field.key] = state.value.trim()
    }
    if (Object.keys(patch).length === 0) {
      toast.error(t('wildcard_ssl.bulk_no_changes'))
      return
    }
    onSave(patch)
  }

  return (
    <motion.div
      initial={{ height: 0, opacity: 0 }}
      animate={{ height: 'auto', opacity: 1 }}
      exit={{ height: 0, opacity: 0 }}
      transition={{ duration: 0.2 }}
      className="overflow-hidden"
    >
      <div className="p-4 bg-dark-900/60 border border-dark-700 rounded-xl space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-dark-100">
            {t('wildcard_ssl.bulk_edit_title', { count })}
          </h3>
          <button onClick={onClose} className="text-dark-500 hover:text-dark-300">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div>
          <label className="block text-xs text-dark-400 mb-1">{t('wildcard_ssl.custom_path_mode')}</label>
          <select
            value={customMode}
            onChange={e => setCustomMode(e.target.value)}
            className="w-full sm:w-64 px-2.5 py-1.5 bg-dark-900 border border-dark-700 rounded-lg text-dark-200 text-sm focus:outline-none focus:border-accent-500"
          >
            <option value={KEEP}>{t('wildcard_ssl.bulk_keep')}</option>
            <option value="off">{t('wildcard_ssl.bulk_custom_off')}</option>
            <option value="on">{t('wildcard_ssl.bulk_custom_on')}</option>
          </select>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          {BULK_TEXT_FIELDS.map(field => {
            const state = texts[field.key]
            return (
              <div key={field.key}>
                <label className="flex items-center gap-2 text-xs text-dark-400 mb-1 cursor-pointer w-fit">
                  <Checkbox
                    checked={state.on}
                    onChange={() => setFieldOn(field.key, !state.on)}
                  />
                  {t(field.labelKey)}
                </label>
                <input
                  type="text"
                  value={state.value}
                  disabled={!state.on}
                  onChange={e => setFieldValue(field.key, e.target.value)}
                  placeholder={field.placeholder || t('wildcard_ssl.reload_cmd_placeholder')}
                  className="w-full px-2.5 py-1.5 bg-dark-900 border border-dark-700 rounded-lg text-dark-200 text-sm placeholder-dark-600 focus:outline-none focus:border-accent-500 font-mono disabled:opacity-40"
                />
              </div>
            )
          })}
        </div>

        <p className="text-[11px] text-dark-500">{t('wildcard_ssl.bulk_field_clear_hint')}</p>

        <div className="flex justify-end">
          <button
            onClick={handleSubmit}
            disabled={saving}
            className="px-3 py-1.5 bg-accent-500 text-white rounded-lg text-xs hover:bg-accent-600 transition-colors disabled:opacity-50 flex items-center gap-1.5"
          >
            {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
            {t('wildcard_ssl.bulk_apply', { count })}
          </button>
        </div>
      </div>
    </motion.div>
  )
}


export default function WildcardSSL() {
  const { t } = useTranslation()

  // Certificate state
  const [cert, setCert] = useState<WildcardCertificate | null>(null)
  const [certLoading, setCertLoading] = useState(true)
  const [issuing, setIssuing] = useState(false)
  const [renewing, setRenewing] = useState(false)
  const [issueDomain, setIssueDomain] = useState('')
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // Settings state
  const [settings, setSettings] = useState<WildcardSSLSettings | null>(null)
  const [settingsLoading, setSettingsLoading] = useState(true)
  const [cfToken, setCfToken] = useState('')
  const [cfTokenRevealed, setCfTokenRevealed] = useState('')
  const [email, setEmail] = useState('')
  const [autoRenew, setAutoRenew] = useState(false)
  const [renewDays, setRenewDays] = useState(30)
  const [useForPanel, setUseForPanel] = useState(false)
  const [showToken, setShowToken] = useState(false)
  const [savingSettings, setSavingSettings] = useState(false)

  // Servers state
  const [servers, setServers] = useState<WildcardServerConfig[]>([])
  const { servers: allServers } = useNodeCapabilities()
  const [serversLoading, setServersLoading] = useState(true)
  const [deployingServer, setDeployingServer] = useState<number | null>(null)
  const [expandedServer, setExpandedServer] = useState<number | null>(null)

  // Search + bulk selection
  const [searchQuery, setSearchQuery] = useState('')
  const [selectedIds, setSelectedIds] = useState<number[]>([])
  const [showBulkEdit, setShowBulkEdit] = useState(false)
  const [bulkSaving, setBulkSaving] = useState(false)
  const [expandedFolders, setExpandedFolders] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem('wildcard_expanded_folders')
      return raw ? new Set(JSON.parse(raw)) : new Set()
    } catch { return new Set() }
  })
  const { progress: deployProgress, run: runDeploy, cancel: cancelDeploy, reset: resetDeploy } =
    useBulkStream<WildcardDeployResult>()

  // Fetch all data
  const fetchCert = useCallback(async () => {
    try {
      const res = await wildcardSSLApi.getCertificates()
      const certs = res.data.certificates
      setCert(certs.length > 0 ? certs[0] : null)
    } catch { /* ignore */ } finally {
      setCertLoading(false)
    }
  }, [])

  const fetchSettings = useCallback(async () => {
    try {
      const res = await wildcardSSLApi.getSettings()
      setSettings(res.data)
      setEmail(res.data.email)
      setAutoRenew(res.data.auto_renew_enabled)
      setRenewDays(res.data.renew_days_before)
      setUseForPanel(res.data.use_for_panel)
    } catch { /* ignore */ } finally {
      setSettingsLoading(false)
    }
  }, [])

  const fetchServers = useCallback(async () => {
    try {
      const res = await wildcardSSLApi.getServers()
      setServers(res.data.servers)
      setSelectedIds(prev => prev.filter(id => res.data.servers.some(s => s.server_id === id)))
    } catch { /* ignore */ } finally {
      setServersLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchCert()
    fetchSettings()
    fetchServers()
  }, [fetchCert, fetchSettings, fetchServers])

  useEffect(() => {
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [])

  // Issue certificate
  const handleIssue = async (e: FormEvent) => {
    e.preventDefault()
    if (!issueDomain.trim()) return
    setIssuing(true)
    try {
      await wildcardSSLApi.issueCertificate({ domain: issueDomain.trim() })
      pollRef.current = setInterval(async () => {
        try {
          const res = await wildcardSSLApi.getIssueStatus()
          if (!res.data.in_progress) {
            if (pollRef.current) clearInterval(pollRef.current)
            pollRef.current = null
            setIssuing(false)
            if (res.data.last_result === 'success') {
              toast.success(t('wildcard_ssl.issue_success'))
              fetchCert()
            } else {
              toast.error(res.data.last_error || t('wildcard_ssl.issue_error'))
            }
          }
        } catch {
          if (pollRef.current) clearInterval(pollRef.current)
          pollRef.current = null
          setIssuing(false)
        }
      }, 3000)
    } catch (err: any) {
      setIssuing(false)
      toast.error(err?.response?.data?.detail || t('wildcard_ssl.issue_error'))
    }
  }

  const handleRenew = async () => {
    if (!cert) return
    setRenewing(true)
    try {
      await wildcardSSLApi.renewCertificate(cert.id)
      toast.success(t('wildcard_ssl.renew_success'))
      fetchCert()
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || t('wildcard_ssl.renew_error'))
    } finally {
      setRenewing(false)
    }
  }

  // Итоговый тост по done-состоянию стрима; детали ошибок видны в панели прогресса
  const notifyDeployFinished = (state: BulkStreamState<WildcardDeployResult>) => {
    if (state.error) {
      toast.error(state.error, { duration: 8000 })
      return
    }
    if (!state.finished) return
    const failedRows = state.rows.filter(r => r.state === 'error')
    if (failedRows.length === 0) {
      toast.success(t('wildcard_ssl.deploy_success', { success: state.ok, total: state.total }))
    } else {
      const names = failedRows.map(r => r.server_name).join(', ')
      toast.error(t('wildcard_ssl.deploy_partial', { success: state.ok, total: state.total, failed: names }), { duration: 8000 })
    }
  }

  const handleDeployAll = async () => {
    if (!cert) return
    const state = await runDeploy(wildcardDeployStreamUrl(cert.id), { server_ids: null })
    notifyDeployFinished(state)
  }

  const handleDeployOne = async (serverId: number) => {
    if (!cert) return
    setDeployingServer(serverId)
    try {
      const res = await wildcardSSLApi.deployToServer(cert.id, serverId)
      if (res.data.success) {
        toast.success(`${res.data.server_name}: OK`)
      } else {
        toast.error(`${res.data.server_name}: ${res.data.message}`)
      }
    } catch {
      toast.error('Deploy failed')
    } finally {
      setDeployingServer(null)
    }
  }

  const handleDelete = async () => {
    if (!cert || !confirm(t('wildcard_ssl.delete_confirm'))) return
    try {
      await wildcardSSLApi.deleteCertificate(cert.id)
      setCert(null)
      toast.success('OK')
    } catch {
      toast.error('Delete failed')
    }
  }

  const handleSaveSettings = async () => {
    setSavingSettings(true)
    try {
      const data: any = { email, auto_renew_enabled: autoRenew, renew_days_before: renewDays, use_for_panel: useForPanel }
      if (cfToken) data.cloudflare_api_token = cfToken
      const res = await wildcardSSLApi.updateSettings(data)
      toast.success(t('wildcard_ssl.settings_saved'))
      const panelDeploy = res.data?.panel_deploy
      if (panelDeploy) {
        if (panelDeploy.success) {
          toast.success(t('wildcard_ssl.panel_applied'))
        } else {
          toast.error(t('wildcard_ssl.panel_apply_failed', { message: panelDeploy.message }), { duration: 8000 })
        }
      }
      setCfToken('')
      setCfTokenRevealed('')
      setShowToken(false)
      fetchSettings()
    } catch {
      toast.error('Error')
    } finally {
      setSavingSettings(false)
    }
  }

  const handleExpandServer = (serverId: number) => {
    setExpandedServer(prev => prev === serverId ? null : serverId)
  }

  // Toggle отправляется сразу, path/cmd — только по кнопке Save
  const handleServerToggle = async (serverId: number, enabled: boolean) => {
    setServers(prev => prev.map(s => s.server_id === serverId ? { ...s, wildcard_ssl_enabled: enabled } : s))
    try {
      await wildcardSSLApi.updateServer(serverId, { wildcard_ssl_enabled: enabled })
    } catch {
      fetchServers()
    }
  }

  const handleServerSave = async (serverId: number, data: ServerSavePayload) => {
    const payload = {
      wildcard_ssl_deploy_path: data.deploy_path,
      wildcard_ssl_reload_cmd: data.reload_cmd,
      wildcard_ssl_fullchain_name: data.fullchain_name,
      wildcard_ssl_privkey_name: data.privkey_name,
      wildcard_ssl_custom_path_enabled: data.custom_path_enabled,
      wildcard_ssl_custom_fullchain_path: data.custom_fullchain_path,
      wildcard_ssl_custom_privkey_path: data.custom_privkey_path,
    }
    try {
      await wildcardSSLApi.updateServer(serverId, payload)
      setServers(prev => prev.map(s =>
        s.server_id === serverId ? { ...s, ...payload } : s
      ))
    } catch {
      fetchServers()
    }
  }

  const certDaysColor = (days: number | null) => {
    if (days === null) return 'text-dark-400'
    if (days <= 0) return 'text-red-400'
    if (days <= 14) return 'text-yellow-400'
    return 'text-green-400'
  }

  const enabledCount = servers.filter(s => s.wildcard_ssl_enabled).length

  const groupedServers = useMemo(() => {
    const folders = new Map<string, WildcardServerConfig[]>()
    const noFolder: WildcardServerConfig[] = []
    for (const s of servers) {
      if (s.folder) {
        if (!folders.has(s.folder)) folders.set(s.folder, [])
        folders.get(s.folder)!.push(s)
      } else {
        noFolder.push(s)
      }
    }
    return { folders, noFolder }
  }, [servers])

  const sortedFolderNames = useMemo(() => {
    const allNames = [...groupedServers.folders.keys()]
    try {
      const saved: string[] = JSON.parse(localStorage.getItem('dashboard_folder_order') || '[]')
      const ordered = saved.filter(f => allNames.includes(f))
      const rest = allNames.filter(f => !saved.includes(f)).sort()
      return [...ordered, ...rest]
    } catch {
      return allNames.sort()
    }
  }, [groupedServers.folders])

  const filteredGroups = useMemo(() => {
    const q = searchQuery.toLowerCase().trim()
    if (!q) return groupedServers
    const matches = (s: WildcardServerConfig) =>
      s.server_name.toLowerCase().includes(q) || (s.server_url || '').toLowerCase().includes(q)
    const folders = new Map<string, WildcardServerConfig[]>()
    for (const [name, svrs] of groupedServers.folders) {
      // Совпадение по имени папки показывает всю папку целиком
      const matched = name.toLowerCase().includes(q) ? svrs : svrs.filter(matches)
      if (matched.length > 0) folders.set(name, matched)
    }
    return { folders, noFolder: groupedServers.noFolder.filter(matches) }
  }, [searchQuery, groupedServers])

  const filteredServers = useMemo(
    () => [...Array.from(filteredGroups.folders.values()).flat(), ...filteredGroups.noFolder],
    [filteredGroups]
  )

  const hasFolders = groupedServers.folders.size > 0

  const visibleIds = useMemo(() => filteredServers.map(s => s.server_id), [filteredServers])
  const allVisibleSelected = visibleIds.length > 0 && visibleIds.every(id => selectedIds.includes(id))
  const someVisibleSelected = visibleIds.some(id => selectedIds.includes(id))

  const toggleSelect = (id: number) => {
    setSelectedIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id])
  }

  const toggleSelectAllVisible = () => {
    if (allVisibleSelected) {
      setSelectedIds(prev => prev.filter(id => !visibleIds.includes(id)))
    } else {
      setSelectedIds(prev => [...new Set([...prev, ...visibleIds])])
    }
  }

  const clearSelection = () => {
    setSelectedIds([])
    setShowBulkEdit(false)
  }

  const toggleFolderSelect = (folderServers: WildcardServerConfig[]) => {
    const folderIds = folderServers.map(s => s.server_id)
    const allSelected = folderIds.every(id => selectedIds.includes(id))
    if (allSelected) {
      setSelectedIds(prev => prev.filter(id => !folderIds.includes(id)))
    } else {
      setSelectedIds(prev => [...new Set([...prev, ...folderIds])])
    }
  }

  const getFolderCheckState = (folderServers: WildcardServerConfig[]): 'none' | 'some' | 'all' => {
    const count = folderServers.filter(s => selectedIds.includes(s.server_id)).length
    if (count === 0) return 'none'
    if (count === folderServers.length) return 'all'
    return 'some'
  }

  const toggleCollapsed = (folder: string) => {
    setExpandedFolders(prev => {
      const next = new Set(prev)
      if (next.has(folder)) next.delete(folder)
      else next.add(folder)
      localStorage.setItem('wildcard_expanded_folders', JSON.stringify([...next]))
      return next
    })
  }

  // Ноды с закрытым разделом SSL можно выделять (настройки живут в БД панели),
  // но деплой на них панель не отправит
  const eligibleIds = useMemo(
    () => selectedIds.filter(id => nodeAllows(allServers.find(s => s.id === id), 'ssl', 'write')),
    [selectedIds, allServers]
  )
  const blockedCount = selectedIds.length - eligibleIds.length

  const handleBulkToggle = async (enabled: boolean) => {
    const ids = new Set(selectedIds)
    setServers(prev => prev.map(s => ids.has(s.server_id) ? { ...s, wildcard_ssl_enabled: enabled } : s))
    try {
      await wildcardSSLApi.updateServersBulk({ server_ids: selectedIds, wildcard_ssl_enabled: enabled })
    } catch {
      fetchServers()
    }
  }

  const handleBulkDeploy = async () => {
    if (!cert) return
    if (eligibleIds.length === 0) {
      toast.error(t('wildcard_ssl.bulk_deploy_blocked', { count: blockedCount }))
      return
    }
    const state = await runDeploy(wildcardDeployStreamUrl(cert.id), { server_ids: eligibleIds })
    notifyDeployFinished(state)
  }

  const renderServerCard = (srv: WildcardServerConfig) => (
    <ServerCard
      key={srv.server_id}
      srv={srv}
      cert={cert}
      deployingServer={deployingServer}
      expanded={expandedServer === srv.server_id}
      selected={selectedIds.includes(srv.server_id)}
      onToggle={handleServerToggle}
      onExpand={handleExpandServer}
      onSelect={toggleSelect}
      onSave={handleServerSave}
      onDeploy={handleDeployOne}
      restricted={!nodeAllows(allServers.find(s => s.id === srv.server_id), 'ssl', 'write')}
      t={t}
    />
  )

  const handleBulkEditSave = async (patch: WildcardServerConfigPatch) => {
    setBulkSaving(true)
    try {
      const res = await wildcardSSLApi.updateServersBulk({ server_ids: selectedIds, ...patch })
      toast.success(t('wildcard_ssl.bulk_updated', { count: res.data.updated }))
      setShowBulkEdit(false)
      fetchServers()
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || 'Error')
    } finally {
      setBulkSaving(false)
    }
  }

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className="space-y-6"
    >
      {/* Header */}
      <div className="flex items-center gap-3">
        <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-accent-500/20 to-accent-600/20 flex items-center justify-center border border-accent-500/20">
          <ShieldCheck className="w-5 h-5 text-accent-400" />
        </div>
        <div>
          <h1 className="text-2xl font-bold text-dark-100 flex items-center gap-2">
            {t('wildcard_ssl.title')}
            <FAQIcon screen="PAGE_WILDCARD_SSL" />
          </h1>
        </div>
      </div>

      {/* Certificate Section */}
      <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }}
        className="card group hover:border-dark-700 transition-all">
        <div className="flex items-center gap-2 mb-4">
          <Globe className="w-5 h-5 text-accent-400" />
          <h2 className="text-lg font-semibold text-dark-100">{t('wildcard_ssl.certificate')}</h2>
          <FAQIcon screen="WILDCARD_SSL_ACME" size="sm" />
        </div>

        {certLoading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="w-6 h-6 text-accent-400 animate-spin" />
          </div>
        ) : cert ? (
          <div className="space-y-4">
            <div className="p-4 bg-dark-800/50 rounded-xl border border-dark-700/50">
              <div className="flex items-center justify-between flex-wrap gap-3">
                <div>
                  <div className="text-lg font-mono text-dark-100">{cert.domain}</div>
                  <div className="text-sm text-dark-400 mt-1">
                    {t('wildcard_ssl.expires')}: {cert.expiry_date ? new Date(cert.expiry_date).toLocaleDateString() : '—'}
                    <span className={`ml-2 font-medium ${certDaysColor(cert.days_left)}`}>
                      {cert.days_left !== null
                        ? cert.days_left <= 0
                          ? t('wildcard_ssl.expired')
                          : t('wildcard_ssl.days_left', { days: cert.days_left })
                        : ''}
                    </span>
                  </div>
                  {cert.last_renewed && (
                    <div className="text-xs text-dark-500 mt-1">
                      {t('wildcard_ssl.renew')}: {new Date(cert.last_renewed).toLocaleDateString()}
                    </div>
                  )}
                </div>
                <div className="flex items-center gap-2 flex-wrap">
                  <button onClick={handleRenew} disabled={renewing}
                    className="px-3 py-1.5 bg-accent-500/20 text-accent-400 rounded-lg text-sm hover:bg-accent-500/30 transition-colors disabled:opacity-50 flex items-center gap-1.5">
                    {renewing ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
                    {renewing ? t('wildcard_ssl.renewing') : t('wildcard_ssl.renew')}
                  </button>
                  <button onClick={handleDeployAll} disabled={deployProgress.active || enabledCount === 0}
                    className="px-3 py-1.5 bg-blue-500/20 text-blue-400 rounded-lg text-sm hover:bg-blue-500/30 transition-colors disabled:opacity-50 flex items-center gap-1.5">
                    {deployProgress.active ? <Loader2 className="w-4 h-4 animate-spin" /> : <Upload className="w-4 h-4" />}
                    {deployProgress.active ? t('wildcard_ssl.deploying') : t('wildcard_ssl.deploy_all')}
                  </button>
                  <button onClick={handleDelete}
                    className="px-3 py-1.5 bg-red-500/20 text-red-400 rounded-lg text-sm hover:bg-red-500/30 transition-colors flex items-center gap-1.5">
                    <Trash2 className="w-4 h-4" />
                    {t('wildcard_ssl.delete')}
                  </button>
                </div>
              </div>
            </div>

            {/* key по времени выпуска: после продления компонент пересоздаётся и не отдаёт старый PEM */}
            <CertificateMaterials
              key={cert.last_renewed || cert.issued_at || cert.id}
              certId={cert.id}
            />
          </div>
        ) : (
          <form onSubmit={handleIssue} className="space-y-4">
            <p className="text-dark-400 text-sm">{t('wildcard_ssl.no_certificate')}</p>
            {!settings?.cloudflare_api_token_set && (
              <div className="flex items-start gap-2 p-3 bg-yellow-500/10 border border-yellow-500/20 rounded-lg">
                <Info className="w-4 h-4 text-yellow-400 mt-0.5 shrink-0" />
                <span className="text-xs text-yellow-300">{t('wildcard_ssl.issue_prereq')}</span>
              </div>
            )}
            <div>
              <label className="block text-sm text-dark-300 mb-1">{t('wildcard_ssl.domain_label')}</label>
              <input
                type="text"
                value={issueDomain}
                onChange={e => setIssueDomain(e.target.value)}
                placeholder={t('wildcard_ssl.domain_placeholder')}
                className="w-full sm:w-80 px-3 py-2 bg-dark-800 border border-dark-700 rounded-lg text-dark-100 placeholder-dark-500 focus:outline-none focus:border-accent-500"
              />
              <p className="text-xs text-dark-500 mt-1">{t('wildcard_ssl.domain_hint')}</p>
            </div>
            <div className="flex items-center gap-3">
              <button type="submit" disabled={issuing || !issueDomain.trim() || !settings?.cloudflare_api_token_set}
                className="px-4 py-2 bg-accent-500 text-white rounded-lg hover:bg-accent-600 transition-colors disabled:opacity-50 flex items-center gap-2">
                {issuing ? <Loader2 className="w-4 h-4 animate-spin" /> : <ShieldCheck className="w-4 h-4" />}
                {issuing ? t('wildcard_ssl.issuing') : t('wildcard_ssl.issue_new')}
              </button>
              {issuing && <span className="text-xs text-dark-500">{t('wildcard_ssl.dns_note')}</span>}
            </div>
          </form>
        )}
      </motion.div>

      {/* Cloudflare Settings */}
      <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4, delay: 0.1 }}
        className="card group hover:border-dark-700 transition-all">
        <div className="flex items-center gap-2 mb-4">
          <ShieldCheck className="w-5 h-5 text-accent-400" />
          <h2 className="text-lg font-semibold text-dark-100">{t('wildcard_ssl.settings_title')}</h2>
        </div>

        {settingsLoading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="w-6 h-6 text-accent-400 animate-spin" />
          </div>
        ) : (
          <div className="space-y-4">
            <div>
              <label className="block text-sm text-dark-300 mb-1">{t('wildcard_ssl.cf_token')}</label>
              <div className="flex items-center gap-2">
                <div className="relative flex-1 sm:max-w-md">
                  <input
                    type="text"
                    value={cfToken || (showToken && cfTokenRevealed ? cfTokenRevealed : (settings?.cloudflare_api_token_set ? settings.cloudflare_api_token : ''))}
                    onChange={e => { setCfToken(e.target.value); setCfTokenRevealed('') }}
                    onFocus={() => { if (!cfToken && settings?.cloudflare_api_token_set && !showToken) setCfToken('') }}
                    placeholder={settings?.cloudflare_api_token_set ? '' : 'API Token'}
                    name="cf_api_token_field"
                    autoComplete="new-password"
                    data-1p-ignore
                    data-lpignore="true"
                    data-form-type="other"
                    style={showToken ? undefined : { WebkitTextSecurity: 'disc', textSecurity: 'disc' } as any}
                    className="w-full px-3 py-2 bg-dark-800 border border-dark-700 rounded-lg text-dark-100 placeholder-dark-500 focus:outline-none focus:border-accent-500 pr-10"
                  />
                  <button type="button" onClick={async () => {
                    if (showToken) {
                      setShowToken(false)
                    } else {
                      if (cfToken) {
                        setShowToken(true)
                      } else if (settings?.cloudflare_api_token_set) {
                        try {
                          const res = await wildcardSSLApi.getTokenRaw()
                          setCfTokenRevealed(res.data.cloudflare_api_token)
                          setShowToken(true)
                        } catch { /* ignore */ }
                      }
                    }
                  }}
                    className="absolute right-2 top-1/2 -translate-y-1/2 text-dark-500 hover:text-dark-300">
                    {showToken ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                  </button>
                </div>
                {settings?.cloudflare_api_token_set ? (
                  <span className="text-xs text-green-400 flex items-center gap-1">
                    <CheckCircle2 className="w-3.5 h-3.5" />
                    {t('wildcard_ssl.cf_token_set')}
                  </span>
                ) : (
                  <span className="text-xs text-red-400 flex items-center gap-1">
                    <XCircle className="w-3.5 h-3.5" />
                    {t('wildcard_ssl.cf_token_not_set')}
                  </span>
                )}
              </div>
              <p className="text-xs text-dark-500 mt-1">{t('wildcard_ssl.cf_token_hint')}</p>
            </div>

            <div>
              <label className="block text-sm text-dark-300 mb-1">{t('wildcard_ssl.email')}</label>
              <input
                type="email"
                value={email}
                onChange={e => setEmail(e.target.value)}
                placeholder="admin@example.com"
                className="w-full sm:w-80 px-3 py-2 bg-dark-800 border border-dark-700 rounded-lg text-dark-100 placeholder-dark-500 focus:outline-none focus:border-accent-500"
              />
              <p className="text-xs text-dark-500 mt-1">{t('wildcard_ssl.email_hint')}</p>
            </div>

            <div className="space-y-2">
              <div className="flex items-center gap-4 flex-wrap">
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={autoRenew}
                    onChange={e => setAutoRenew(e.target.checked)}
                    className="w-4 h-4 rounded border-dark-600 text-accent-500 focus:ring-accent-500 bg-dark-800"
                  />
                  <span className="text-sm text-dark-200">{t('wildcard_ssl.auto_renew')}</span>
                </label>
                <div className="flex items-center gap-2">
                  <span className="text-sm text-dark-400">{t('wildcard_ssl.renew_days_before')}:</span>
                  <input
                    type="number"
                    value={renewDays}
                    onChange={e => setRenewDays(Number(e.target.value))}
                    min={1}
                    max={90}
                    className="w-16 px-2 py-1 bg-dark-800 border border-dark-700 rounded-lg text-dark-100 text-sm focus:outline-none focus:border-accent-500"
                  />
                </div>
              </div>
              <p className="text-xs text-dark-500 flex items-center gap-1">
                <Info className="w-3.5 h-3.5 shrink-0" />
                {t('wildcard_ssl.auto_renew_hint')}
              </p>
            </div>

            <div className="space-y-2">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={useForPanel}
                  onChange={e => setUseForPanel(e.target.checked)}
                  className="w-4 h-4 rounded border-dark-600 text-accent-500 focus:ring-accent-500 bg-dark-800"
                />
                <span className="text-sm text-dark-200">{t('wildcard_ssl.use_for_panel')}</span>
                {settings?.panel_domain && (
                  <span className="text-xs text-dark-500 font-mono">({settings.panel_domain})</span>
                )}
              </label>
              <p className="text-xs text-dark-500 flex items-center gap-1">
                <Info className="w-3.5 h-3.5 shrink-0" />
                {t('wildcard_ssl.use_for_panel_hint')}
              </p>
              {useForPanel && cert && !!settings?.panel_domain && !wildcardCoversDomain(cert.base_domain, settings.panel_domain) && (
                <div className="flex items-start gap-2 p-3 bg-yellow-500/10 border border-yellow-500/20 rounded-lg">
                  <Info className="w-4 h-4 text-yellow-400 mt-0.5 shrink-0" />
                  <span className="text-xs text-yellow-300">
                    {t('wildcard_ssl.use_for_panel_not_covered', { cert: cert.domain, domain: settings.panel_domain })}
                  </span>
                </div>
              )}
            </div>

            <button onClick={handleSaveSettings} disabled={savingSettings}
              className="px-4 py-2 bg-accent-500 text-white rounded-lg hover:bg-accent-600 transition-colors disabled:opacity-50 flex items-center gap-2">
              {savingSettings ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
              {t('wildcard_ssl.save_settings')}
            </button>
          </div>
        )}
      </motion.div>

      {/* Server Configuration */}
      <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4, delay: 0.2 }}
        className="card group hover:border-dark-700 transition-all">
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2">
            <Server className="w-5 h-5 text-accent-400" />
            <h2 className="text-lg font-semibold text-dark-100">{t('wildcard_ssl.servers_title')}</h2>
            {enabledCount > 0 && (
              <span className="text-xs text-accent-400 bg-accent-500/10 px-2 py-0.5 rounded-full">
                {enabledCount}
              </span>
            )}
          </div>
          {cert && enabledCount > 0 && (
            <button onClick={handleDeployAll} disabled={deployProgress.active}
              className="px-3 py-1.5 bg-blue-500/20 text-blue-400 rounded-lg text-sm hover:bg-blue-500/30 transition-colors disabled:opacity-50 flex items-center gap-1.5">
              {deployProgress.active ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
              {t('wildcard_ssl.deploy_all')}
            </button>
          )}
        </div>

        <p className="text-xs text-dark-500 mb-4 flex items-center gap-1.5">
          <Info className="w-3.5 h-3.5 shrink-0" />
          {t('wildcard_ssl.servers_hint')}
        </p>

        {serversLoading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="w-6 h-6 text-accent-400 animate-spin" />
          </div>
        ) : servers.length === 0 ? (
          <p className="text-dark-400 text-sm py-4">{t('wildcard_ssl.no_servers')}</p>
        ) : (
          <div className="space-y-2">
            {/* Search */}
            <div className="flex items-center gap-2 bg-dark-800 border border-dark-600 rounded-lg px-3 py-1.5">
              <Search className="w-4 h-4 text-dark-400 shrink-0" />
              <input
                type="text"
                value={searchQuery}
                onChange={e => setSearchQuery(e.target.value)}
                placeholder={t('wildcard_ssl.search_placeholder')}
                className="bg-transparent text-sm text-dark-100 placeholder-dark-500 outline-none w-full"
              />
              {searchQuery && (
                <button onClick={() => setSearchQuery('')} className="text-dark-500 hover:text-dark-300 shrink-0">
                  <X className="w-4 h-4" />
                </button>
              )}
            </div>

            {/* Select all */}
            <div className="flex items-center justify-between px-1 pb-1">
              <label className="flex items-center gap-2 cursor-pointer text-xs text-dark-400 hover:text-dark-200 transition-colors">
                <Checkbox
                  checked={allVisibleSelected}
                  indeterminate={someVisibleSelected && !allVisibleSelected}
                  onChange={toggleSelectAllVisible}
                />
                {t('wildcard_ssl.select_all')}
                <span className="text-dark-600">({filteredServers.length})</span>
              </label>
            </div>

            {/* Bulk actions bar */}
            <AnimatePresence>
              {selectedIds.length > 0 && (
                <motion.div
                  initial={{ opacity: 0, y: -5 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -5 }}
                  className="flex items-center justify-between gap-2 flex-wrap px-3 py-2 rounded-lg bg-accent-500/10 border border-accent-500/30"
                >
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-xs text-accent-300">
                      {t('wildcard_ssl.bulk_selected', { count: selectedIds.length })}
                    </span>
                    {blockedCount > 0 && (
                      <span className="text-[11px] text-purple flex items-center gap-1">
                        <Lock className="w-3 h-3" />
                        {t('wildcard_ssl.bulk_deploy_blocked', { count: blockedCount })}
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-1.5 flex-wrap">
                    <button
                      onClick={() => handleBulkToggle(true)}
                      className="px-2.5 py-1 text-xs rounded-lg text-dark-300 hover:text-accent-400 hover:bg-dark-800/60 transition-colors flex items-center gap-1.5"
                    >
                      <ToggleRight className="w-4 h-4" />
                      {t('wildcard_ssl.bulk_enable')}
                    </button>
                    <button
                      onClick={() => handleBulkToggle(false)}
                      className="px-2.5 py-1 text-xs rounded-lg text-dark-300 hover:text-red-400 hover:bg-dark-800/60 transition-colors flex items-center gap-1.5"
                    >
                      <ToggleLeft className="w-4 h-4" />
                      {t('wildcard_ssl.bulk_disable')}
                    </button>
                    {cert && (
                      <button
                        onClick={handleBulkDeploy}
                        disabled={eligibleIds.length === 0 || deployProgress.active}
                        className="px-2.5 py-1 text-xs rounded-lg bg-blue-500/20 text-blue-400 hover:bg-blue-500/30 transition-colors disabled:opacity-50 flex items-center gap-1.5"
                      >
                        {deployProgress.active
                          ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                          : <Upload className="w-3.5 h-3.5" />}
                        {t('wildcard_ssl.bulk_deploy')}
                      </button>
                    )}
                    <button
                      onClick={() => setShowBulkEdit(v => !v)}
                      className={`px-2.5 py-1 text-xs rounded-lg transition-colors flex items-center gap-1.5 ${
                        showBulkEdit
                          ? 'bg-accent-500/20 text-accent-300'
                          : 'text-dark-300 hover:text-accent-400 hover:bg-dark-800/60'
                      }`}
                    >
                      <Settings2 className="w-3.5 h-3.5" />
                      {t('wildcard_ssl.bulk_edit')}
                    </button>
                    <button onClick={clearSelection} className="p-1 text-dark-400 hover:text-dark-200 transition-colors">
                      <X className="w-4 h-4" />
                    </button>
                  </div>
                </motion.div>
              )}
            </AnimatePresence>

            {/* Bulk edit form */}
            <AnimatePresence>
              {showBulkEdit && selectedIds.length > 0 && (
                <WildcardBulkEditForm
                  count={selectedIds.length}
                  saving={bulkSaving}
                  onSave={handleBulkEditSave}
                  onClose={() => setShowBulkEdit(false)}
                  t={t}
                />
              )}
            </AnimatePresence>

            {/* Deploy progress */}
            <AnimatePresence>
              {(deployProgress.active || deployProgress.total > 0) && (
                <WildcardDeployProgress
                  progress={deployProgress}
                  onClose={resetDeploy}
                  onCancel={cancelDeploy}
                />
              )}
            </AnimatePresence>

            {filteredServers.length === 0 ? (
              <div className="text-center py-6">
                <Search className="w-8 h-8 text-dark-600 mx-auto mb-2" />
                <p className="text-dark-400 text-sm">{t('wildcard_ssl.search_empty')}</p>
              </div>
            ) : !hasFolders ? (
              filteredServers.map(renderServerCard)
            ) : (
              <>
                {sortedFolderNames
                  .filter(name => filteredGroups.folders.has(name))
                  .map(folderName => {
                    const folderServers = filteredGroups.folders.get(folderName)!
                    const allFolderServers = groupedServers.folders.get(folderName)!
                    const checkState = getFolderCheckState(allFolderServers)
                    const isCollapsed = !expandedFolders.has(folderName)
                    const selectedInFolder = allFolderServers.filter(s => selectedIds.includes(s.server_id)).length

                    return (
                      <div key={folderName}>
                        <div className="flex items-center gap-2 p-2 rounded-lg hover:bg-dark-800/50 transition-colors">
                          <Checkbox
                            checked={checkState === 'all'}
                            indeterminate={checkState === 'some'}
                            onChange={() => toggleFolderSelect(allFolderServers)}
                          />
                          <div
                            className="flex items-center gap-2 flex-1 min-w-0 cursor-pointer"
                            onClick={() => toggleCollapsed(folderName)}
                          >
                            {isCollapsed
                              ? <Folder className="w-4 h-4 text-accent-400 shrink-0" />
                              : <FolderOpen className="w-4 h-4 text-accent-400 shrink-0" />}
                            <span className="font-medium text-sm text-dark-200 truncate">{folderName}</span>
                            <span className="text-xs text-dark-500 ml-auto shrink-0">{selectedInFolder}/{allFolderServers.length}</span>
                            <motion.div animate={{ rotate: isCollapsed ? -90 : 0 }} transition={{ duration: 0.15 }}>
                              <ChevronDown className="w-3.5 h-3.5 text-dark-500" />
                            </motion.div>
                          </div>
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
                              <div className="pl-6 space-y-2 pt-1 pb-1">
                                {folderServers.map(renderServerCard)}
                              </div>
                            </motion.div>
                          )}
                        </AnimatePresence>
                      </div>
                    )
                  })}

                {filteredGroups.noFolder.length > 0 && (() => {
                  const checkState = getFolderCheckState(groupedServers.noFolder)
                  const isCollapsed = !expandedFolders.has(NO_FOLDER)
                  const selectedInGroup = groupedServers.noFolder.filter(s => selectedIds.includes(s.server_id)).length

                  return (
                    <div>
                      <div className="flex items-center gap-2 p-2 rounded-lg hover:bg-dark-800/50 transition-colors">
                        <Checkbox
                          checked={checkState === 'all'}
                          indeterminate={checkState === 'some'}
                          onChange={() => toggleFolderSelect(groupedServers.noFolder)}
                        />
                        <div
                          className="flex items-center gap-2 flex-1 min-w-0 cursor-pointer"
                          onClick={() => toggleCollapsed(NO_FOLDER)}
                        >
                          <Server className="w-4 h-4 text-dark-400 shrink-0" />
                          <span className="font-medium text-sm text-dark-400 truncate">{t('bulk_actions.no_folder')}</span>
                          <span className="text-xs text-dark-500 ml-auto shrink-0">{selectedInGroup}/{groupedServers.noFolder.length}</span>
                          <motion.div animate={{ rotate: isCollapsed ? -90 : 0 }} transition={{ duration: 0.15 }}>
                            <ChevronDown className="w-3.5 h-3.5 text-dark-500" />
                          </motion.div>
                        </div>
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
                            <div className="pl-6 space-y-2 pt-1 pb-1">
                              {filteredGroups.noFolder.map(renderServerCard)}
                            </div>
                          </motion.div>
                        )}
                      </AnimatePresence>
                    </div>
                  )
                })()}
              </>
            )}
          </div>
        )}
      </motion.div>
    </motion.div>
  )
}
