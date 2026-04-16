import { useEffect, useState, useCallback, useRef, FormEvent } from 'react'
import { ShieldCheck, RefreshCw, Server, Upload, Globe, Loader2, CheckCircle2, XCircle, Trash2, Eye, EyeOff, Save, Send, Info, ChevronRight, ToggleLeft, ToggleRight } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { motion, AnimatePresence } from 'framer-motion'
import { toast } from 'sonner'
import { wildcardSSLApi, WildcardCertificate, WildcardSSLSettings, WildcardServerConfig } from '../api/client'
import { FAQIcon } from '../components/FAQ'

const DEFAULT_DEPLOY_PATH = '/etc/letsencrypt/live'
const DEFAULT_FULLCHAIN_NAME = 'fullchain.pem'
const DEFAULT_PRIVKEY_NAME = 'privkey.pem'

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
  onToggle,
  onExpand,
  onSave,
  onDeploy,
  t,
}: {
  srv: WildcardServerConfig
  cert: WildcardCertificate | null
  deployingServer: number | null
  expanded: boolean
  onToggle: (id: number, enabled: boolean) => void
  onExpand: (id: number) => void
  onSave: (id: number, data: ServerSavePayload) => void
  onDeploy: (id: number) => void
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
      isEnabled
        ? 'bg-dark-800/60 border-dark-700/80'
        : 'bg-dark-800/20 border-dark-800/50'
    }`}>
      {/* Header: toggle | clickable area (name) | deploy button */}
      <div className="flex items-center px-4 py-3 gap-3">
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


export default function WildcardSSL() {
  const { t } = useTranslation()

  // Certificate state
  const [cert, setCert] = useState<WildcardCertificate | null>(null)
  const [certLoading, setCertLoading] = useState(true)
  const [issuing, setIssuing] = useState(false)
  const [renewing, setRenewing] = useState(false)
  const [deploying, setDeploying] = useState(false)
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
  const [showToken, setShowToken] = useState(false)
  const [savingSettings, setSavingSettings] = useState(false)

  // Servers state
  const [servers, setServers] = useState<WildcardServerConfig[]>([])
  const [serversLoading, setServersLoading] = useState(true)
  const [deployingServer, setDeployingServer] = useState<number | null>(null)
  const [expandedServer, setExpandedServer] = useState<number | null>(null)

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
    } catch { /* ignore */ } finally {
      setSettingsLoading(false)
    }
  }, [])

  const fetchServers = useCallback(async () => {
    try {
      const res = await wildcardSSLApi.getServers()
      setServers(res.data.servers)
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

  const handleDeployAll = async () => {
    if (!cert) return
    setDeploying(true)
    try {
      const res = await wildcardSSLApi.deployToAll(cert.id)
      const results = res.data.results
      const ok = results.filter(r => r.success).length
      toast.success(t('wildcard_ssl.deploy_success', { success: ok, total: results.length }))
    } catch {
      toast.error('Deploy failed')
    } finally {
      setDeploying(false)
    }
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
      const data: any = { email, auto_renew_enabled: autoRenew, renew_days_before: renewDays }
      if (cfToken) data.cloudflare_api_token = cfToken
      await wildcardSSLApi.updateSettings(data)
      toast.success(t('wildcard_ssl.settings_saved'))
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

  const handleToggleAll = async (enabled: boolean) => {
    setServers(prev => prev.map(s => ({ ...s, wildcard_ssl_enabled: enabled })))
    try {
      await Promise.all(
        servers.map(s => wildcardSSLApi.updateServer(s.server_id, { wildcard_ssl_enabled: enabled }))
      )
    } catch {
      fetchServers()
    }
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
                  <button onClick={handleDeployAll} disabled={deploying || enabledCount === 0}
                    className="px-3 py-1.5 bg-blue-500/20 text-blue-400 rounded-lg text-sm hover:bg-blue-500/30 transition-colors disabled:opacity-50 flex items-center gap-1.5">
                    {deploying ? <Loader2 className="w-4 h-4 animate-spin" /> : <Upload className="w-4 h-4" />}
                    {deploying ? t('wildcard_ssl.deploying') : t('wildcard_ssl.deploy_all')}
                  </button>
                  <button onClick={handleDelete}
                    className="px-3 py-1.5 bg-red-500/20 text-red-400 rounded-lg text-sm hover:bg-red-500/30 transition-colors flex items-center gap-1.5">
                    <Trash2 className="w-4 h-4" />
                    {t('wildcard_ssl.delete')}
                  </button>
                </div>
              </div>
            </div>
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
            <button onClick={handleDeployAll} disabled={deploying}
              className="px-3 py-1.5 bg-blue-500/20 text-blue-400 rounded-lg text-sm hover:bg-blue-500/30 transition-colors disabled:opacity-50 flex items-center gap-1.5">
              {deploying ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
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
            {/* Toggle all bar */}
            <div className="flex items-center justify-end gap-2 pb-1">
              {enabledCount < servers.length ? (
                <button
                  onClick={() => handleToggleAll(true)}
                  className="px-2.5 py-1 text-xs text-dark-400 hover:text-accent-400 transition-colors flex items-center gap-1.5"
                >
                  <ToggleRight className="w-4 h-4" />
                  {t('wildcard_ssl.enable_all')}
                </button>
              ) : (
                <button
                  onClick={() => handleToggleAll(false)}
                  className="px-2.5 py-1 text-xs text-dark-400 hover:text-red-400 transition-colors flex items-center gap-1.5"
                >
                  <ToggleLeft className="w-4 h-4" />
                  {t('wildcard_ssl.disable_all')}
                </button>
              )}
            </div>

            {servers.map(srv => (
              <ServerCard
                key={srv.server_id}
                srv={srv}
                cert={cert}
                deployingServer={deployingServer}
                expanded={expandedServer === srv.server_id}
                onToggle={handleServerToggle}
                onExpand={handleExpandServer}
                onSave={handleServerSave}
                onDeploy={handleDeployOne}
                t={t}
              />
            ))}
          </div>
        )}
      </motion.div>
    </motion.div>
  )
}
