import { useState, useEffect, useMemo, useRef, FormEvent } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Plus,
  Trash2,
  Server as ServerIcon,
  CheckCircle2,
  XCircle,
  Loader2,
  ExternalLink,
  Edit2,
  X,
  Link as LinkIcon,
  AlertTriangle,
  Power,
  Copy,
  Check,
  Globe,
  Zap,
  ShieldCheck,
  ShieldAlert,
  Search,
  Rocket,
  Terminal,
  KeyRound,
  Save
} from 'lucide-react'
import { useServersStore } from '../stores/serversStore'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import {
  serversApi,
  systemApi,
  serverDeployStreamUrl,
  ServerDeployEvent,
  RemnawaveCertProfile,
} from '../api/client'
import { streamNdjson, StreamUnauthorizedError } from '../utils/ndjsonStream'
import InfraTree from '../components/Infra/InfraTree'
import { Tooltip } from '../components/ui/Tooltip'
import { CopyableIp } from '../components/ui/CopyableIp'
import { extractHost } from '../utils/format'
import { FAQIcon } from '../components/FAQ'
import MigrationBanner from '../components/MigrationBanner'

interface ServerFormData {
  name: string
  host: string
  port: string
}

interface DeployFormData {
  enabled: boolean
  sshPort: string
  sshUser: string
  sshAuth: 'password' | 'key'
  sshPassword: string
  sshPrivateKey: string
  sshPassphrase: string
  installWarp: boolean
  installRemnawave: boolean
  remnaCertMode: 'inline' | 'saved'
  remnaCertInline: string
  remnaCertProfileId: number | null
  installProxy: boolean
  proxyUrl: string
}

const DEPLOY_DEFAULTS: DeployFormData = {
  enabled: false,
  sshPort: '22',
  sshUser: 'root',
  sshAuth: 'password',
  sshPassword: '',
  sshPrivateKey: '',
  sshPassphrase: '',
  installWarp: false,
  installRemnawave: false,
  remnaCertMode: 'inline',
  remnaCertInline: '',
  remnaCertProfileId: null,
  installProxy: false,
  proxyUrl: '',
}

const parseServerUrl = (url: string): { host: string; port: string } => {
  const match = url.match(/^https?:\/\/([^:/]+):?(\d+)?/)
  return {
    host: match?.[1] || '',
    port: match?.[2] || '9100'
  }
}

const buildServerUrl = (host: string, port: string): string => {
  return `https://${host}:${port || '9100'}`
}

export default function Servers() {
  const { servers, fetchServersWithMetrics, addServer, deleteServer, testServer, updateServer, toggleServer } = useServersStore()
  const { t } = useTranslation()

  const [showForm, setShowForm] = useState(false)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [formData, setFormData] = useState<ServerFormData>({ name: '', host: '', port: '9100' })
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [testResults, setTestResults] = useState<Record<number, { status: string; message?: string }>>({})
  const [testingId, setTestingId] = useState<number | null>(null)
  const [deleteConfirm, setDeleteConfirm] = useState<number | null>(null)
  const [copied, setCopied] = useState(false)
  const [panelIp, setPanelIp] = useState<string | null>(null)
  const [installerToken, setInstallerToken] = useState<string | null>(null)
  const [tokenCopied, setTokenCopied] = useState(false)
  const [searchQuery, setSearchQuery] = useState('')

  const [deploy, setDeploy] = useState<DeployFormData>(DEPLOY_DEFAULTS)
  const [remnaCertProfiles, setRemnaCertProfiles] = useState<RemnawaveCertProfile[]>([])
  const [deployLog, setDeployLog] = useState<string[]>([])
  const [isDeploying, setIsDeploying] = useState(false)
  const [savingCert, setSavingCert] = useState(false)
  const deployLogRef = useRef<HTMLPreElement>(null)

  useEffect(() => {
    const el = deployLogRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [deployLog])

  const filteredServers = useMemo(() => {
    const q = searchQuery.toLowerCase().trim()
    if (!q) return servers
    return servers.filter(s =>
      s.name.toLowerCase().includes(q) || s.url.toLowerCase().includes(q)
    )
  }, [searchQuery, servers])

  const handleCopyPanelIp = async () => {
    if (!panelIp) return
    try {
      await navigator.clipboard.writeText(panelIp)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      // Fallback for older browsers
      const textArea = document.createElement('textarea')
      textArea.value = panelIp
      document.body.appendChild(textArea)
      textArea.select()
      document.execCommand('copy')
      document.body.removeChild(textArea)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    }
  }
  
  useEffect(() => {
    fetchServersWithMetrics()
    systemApi.getPanelIp().then(res => {
      setPanelIp(res.data.ip)
    }).catch(() => {})
  }, [fetchServersWithMetrics])

  useEffect(() => {
    if (!showForm || editingId) return
    if (installerToken) return
    serversApi.installerToken()
      .then(res => setInstallerToken(res.data.token))
      .catch(() => {})
  }, [showForm, editingId, installerToken])

  const loadRemnaCertProfiles = () => {
    serversApi.remnawaveCerts()
      .then(res => setRemnaCertProfiles(res.data.profiles))
      .catch(() => {})
  }

  useEffect(() => {
    if (!showForm || editingId) return
    loadRemnaCertProfiles()
  }, [showForm, editingId])

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setError('')
    setIsSubmitting(true)

    if (editingId) {
      const updateData: { name: string; url: string } = {
        name: formData.name,
        url: buildServerUrl(formData.host, formData.port),
      }

      try {
        await updateServer(editingId, updateData)
        toast.success(t('servers.server_updated'))
        setEditingId(null)
        setFormData({ name: '', host: '', port: '9100' })
        setShowForm(false)
      } catch {
        toast.error(t('servers.failed_update'))
        setError(t('servers.failed_update'))
      }
    } else if (deploy.enabled) {
      setIsSubmitting(false)
      await handleDeploy()
      return
    } else {
      const serverData = {
        name: formData.name,
        url: buildServerUrl(formData.host, formData.port),
      }
      const result = await addServer(serverData)
      if (result.success) {
        toast.success(t('servers.server_added'))
        setShowForm(false)
        setFormData({ name: '', host: '', port: '9100' })
      } else {
        toast.error(result.error || t('servers.failed_add'))
        setError(result.error || t('servers.failed_add'))
      }
    }

    setIsSubmitting(false)
  }

  const handleDeploy = async () => {
    if (deploy.sshAuth === 'password' && !deploy.sshPassword.trim()) {
      setError(t('servers.deploy_no_password'))
      return
    }
    if (deploy.sshAuth === 'key' && !deploy.sshPrivateKey.trim()) {
      setError(t('servers.deploy_no_key'))
      return
    }
    if (deploy.installRemnawave) {
      const hasInline = deploy.remnaCertMode === 'inline' && deploy.remnaCertInline.trim()
      const hasSaved = deploy.remnaCertMode === 'saved' && deploy.remnaCertProfileId != null
      if (!hasInline && !hasSaved) {
        setError(t('servers.deploy_no_remna_cert'))
        return
      }
    }
    if (deploy.installProxy && !deploy.proxyUrl.trim()) {
      setError(t('servers.deploy_no_proxy'))
      return
    }

    setError('')
    setDeployLog([])
    setIsDeploying(true)

    const body = {
      name: formData.name,
      host: formData.host,
      monitoring_port: parseInt(formData.port || '9100', 10),
      ssh_port: parseInt(deploy.sshPort || '22', 10),
      ssh_user: deploy.sshUser.trim() || 'root',
      ssh_password: deploy.sshAuth === 'password' ? deploy.sshPassword : null,
      ssh_private_key: deploy.sshAuth === 'key' ? deploy.sshPrivateKey : null,
      ssh_key_passphrase: deploy.sshAuth === 'key' ? deploy.sshPassphrase : null,
      install_warp: deploy.installWarp,
      install_remnawave: deploy.installRemnawave,
      remnawave_cert_profile_id:
        deploy.installRemnawave && deploy.remnaCertMode === 'saved' ? deploy.remnaCertProfileId : null,
      remnawave_cert_inline:
        deploy.installRemnawave && deploy.remnaCertMode === 'inline' ? deploy.remnaCertInline : null,
      install_proxy: deploy.installProxy,
      proxy_url: deploy.installProxy ? deploy.proxyUrl : null,
    }

    let succeeded = false
    try {
      await streamNdjson<ServerDeployEvent>(
        serverDeployStreamUrl,
        body,
        (ev) => {
          if (ev.type === 'log') {
            setDeployLog(prev => [...prev, ev.line])
          } else if (ev.type === 'start') {
            setDeployLog(prev => [...prev, `--- ${ev.host} ---`])
          } else if (ev.type === 'error') {
            setDeployLog(prev => [...prev, `[ERROR] ${ev.message}`])
            setError(ev.message)
          } else if (ev.type === 'done') {
            succeeded = ev.exit_code === 0
          }
        },
        new AbortController().signal,
      )
    } catch (e) {
      if (!(e instanceof StreamUnauthorizedError)) {
        const msg = e instanceof Error ? e.message : String(e)
        setError(msg)
        setDeployLog(prev => [...prev, `[ERROR] ${msg}`])
      }
    }

    setIsDeploying(false)

    if (succeeded) {
      toast.success(t('servers.deploy_success'))
      await fetchServersWithMetrics()
      setShowForm(false)
      setEditingId(null)
      setFormData({ name: '', host: '', port: '9100' })
      setDeploy(DEPLOY_DEFAULTS)
      setDeployLog([])
      setError('')
    } else {
      toast.error(t('servers.deploy_failed'))
    }
  }

  const handleSaveCert = async () => {
    const name = window.prompt(t('servers.deploy_remna_save_prompt'))
    if (!name || !name.trim()) return
    setSavingCert(true)
    try {
      await serversApi.saveRemnawaveCert(name.trim(), deploy.remnaCertInline.trim())
      toast.success(t('servers.deploy_remna_saved'))
      loadRemnaCertProfiles()
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string } } }
      toast.error(err.response?.data?.detail || t('servers.deploy_remna_save_failed'))
    }
    setSavingCert(false)
  }

  const handleEdit = (server: typeof servers[0]) => {
    setShowForm(false)
    setEditingId(server.id)
    const { host, port } = parseServerUrl(server.url)
    setFormData({
      name: server.name,
      host,
      port,
    })
    setError('')
  }

  const handleCancel = () => {
    if (isDeploying) return
    setShowForm(false)
    setEditingId(null)
    setFormData({ name: '', host: '', port: '9100' })
    setDeploy(DEPLOY_DEFAULTS)
    setDeployLog([])
    setError('')
  }

  const handleCopyToken = async () => {
    if (!installerToken) return
    try {
      await navigator.clipboard.writeText(installerToken)
    } catch {
      const ta = document.createElement('textarea')
      ta.value = installerToken
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    setTokenCopied(true)
    setTimeout(() => setTokenCopied(false), 2000)
  }

  const handleTest = async (serverId: number) => {
    setTestingId(serverId)
    const result = await testServer(serverId)
    setTestResults(prev => ({ ...prev, [serverId]: result }))
    if (result.status === 'online') {
      toast.success(t('servers.test_success'))
    } else {
      toast.error(result.message || t('servers.test_failed'))
    }
    setTestingId(null)
  }
  
  const handleDelete = async (serverId: number) => {
    if (deleteConfirm === serverId) {
      await deleteServer(serverId)
      toast.success(t('servers.server_deleted'))
      setDeleteConfirm(null)
    } else {
      setDeleteConfirm(serverId)
      setTimeout(() => setDeleteConfirm(null), 3000)
    }
  }
  
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
    >
      {/* Panel IP */}
      {panelIp && (
        <motion.div
          className="mb-6 p-4 rounded-xl bg-dark-800/40 border border-dark-700/50 flex items-center justify-between"
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.15 }}
        >
          <div className="flex items-center gap-3">
            <Globe className="w-5 h-5 text-accent-500" />
            <span className="text-dark-300">{t('servers.panel_ip')}</span>
            <code className="px-2 py-1 bg-dark-900/50 rounded-lg text-dark-100 font-mono text-sm">
              {panelIp}
            </code>
          </div>
          <motion.button
            onClick={handleCopyPanelIp}
            className={`btn btn-secondary text-sm ${copied ? 'bg-success/20 text-success border-success/30' : ''}`}
            whileTap={{ scale: 0.98 }}
          >
            {copied ? (
              <>
                <Check className="w-4 h-4" />
                {t('servers.copied')}
              </>
            ) : (
              <>
                <Copy className="w-4 h-4" />
                {t('common.copy')}
              </>
            )}
          </motion.button>
        </motion.div>
      )}

      {/* Header */}
      <motion.div
        className="flex items-center justify-between mb-8"
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.15 }}
      >
        <div>
          <h1 className="text-2xl font-bold text-dark-50 flex items-center gap-3">
            <ServerIcon className="w-7 h-7 text-accent-400" />
            {t('servers.title')}
            <FAQIcon screen="PAGE_SERVERS" />
          </h1>
          <p className="text-dark-400 mt-1">
            {t('servers.subtitle')}
          </p>
        </div>

        <AnimatePresence>
          {!showForm && (
            <motion.button
              onClick={() => setShowForm(true)}
              className="btn btn-primary"
              initial={{ opacity: 0, scale: 0.9 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.9 }}
              whileTap={{ scale: 0.98 }}
            >
              <Plus className="w-4 h-4" />
              {t('servers.add_server')}
            </motion.button>
          )}
        </AnimatePresence>
      </motion.div>
      
      {/* Infrastructure Tree */}
      <InfraTree />

      <MigrationBanner onMigrated={fetchServersWithMetrics} />

      {/* Search */}
      {servers.length > 0 && (
        <div className="flex items-center gap-2 mb-4">
          <div className="flex-1 flex items-center gap-2 bg-dark-800/50 border border-dark-700/50 rounded-xl px-3 py-2">
            <Search className="w-4 h-4 text-dark-400 shrink-0" />
            <input
              type="text"
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
              placeholder={t('servers.search_placeholder')}
              className="bg-transparent text-sm text-dark-100 placeholder-dark-500 outline-none w-full"
            />
          </div>
        </div>
      )}

      {/* Add Server Form */}
      <AnimatePresence>
        {showForm && !editingId && (
          <motion.div 
            className="card mb-6 overflow-hidden"
            initial={{ opacity: 0, height: 0, marginBottom: 0 }}
            animate={{ opacity: 1, height: 'auto', marginBottom: 24 }}
            exit={{ opacity: 0, height: 0, marginBottom: 0 }}
            transition={{ duration: 0.15 }}
          >
            <div className="flex items-center justify-between mb-6">
              <h2 className="text-lg font-semibold text-dark-100 flex items-center gap-2">
                <Plus className="w-5 h-5 text-accent-500" />
                {t('servers.add_new_server')}
              </h2>
              <motion.button
                onClick={handleCancel}
                className="p-2 hover:bg-dark-700 rounded-xl text-dark-400 transition-colors"
                whileTap={{ scale: 0.95 }}
              >
                <X className="w-5 h-5" />
              </motion.button>
            </div>
            
            <AnimatePresence>
              {error && (
                <motion.div
                  className="flex items-center gap-3 p-4 mb-6 bg-danger/10 border border-danger/20 rounded-xl text-danger"
                  initial={{ opacity: 0, y: -10 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -10 }}
                >
                  <AlertTriangle className="w-5 h-5 flex-shrink-0" />
                  <span className="text-sm">{error}</span>
                </motion.div>
              )}
            </AnimatePresence>

            <div className="mb-6 p-4 rounded-xl bg-accent-500/5 border border-accent-500/20">
              <div className="flex items-center gap-2 mb-2">
                <ShieldCheck className="w-5 h-5 text-accent-500" />
                <h3 className="text-sm font-semibold text-dark-100">{t('servers.installer_token_title')}</h3>
              </div>
              <p className="text-xs text-dark-400 mb-3">{t('servers.installer_token_subtitle')}</p>
              {installerToken ? (
                <>
                  <textarea
                    readOnly
                    value={installerToken}
                    className="input font-mono text-xs break-all resize-none w-full min-h-[88px] mb-3"
                    onClick={(e) => (e.target as HTMLTextAreaElement).select()}
                  />
                  <motion.button
                    type="button"
                    onClick={handleCopyToken}
                    className={`btn text-sm ${tokenCopied ? 'bg-success/20 text-success border-success/30' : 'btn-secondary'}`}
                    whileTap={{ scale: 0.98 }}
                  >
                    {tokenCopied ? (
                      <><Check className="w-4 h-4" />{t('servers.copied')}</>
                    ) : (
                      <><Copy className="w-4 h-4" />{t('servers.copy_installer_token')}</>
                    )}
                  </motion.button>
                  <details className="mt-3 text-xs text-dark-300">
                    <summary className="cursor-pointer text-dark-200 font-medium">{t('servers.install_steps_title')}</summary>
                    <ol className="list-decimal list-inside mt-2 space-y-1 text-dark-400">
                      <li>{t('servers.install_step_1')}</li>
                      <li>{t('servers.install_step_2')}</li>
                      <li>{t('servers.install_step_3')}</li>
                      <li>{t('servers.install_step_4')}</li>
                    </ol>
                  </details>
                </>
              ) : (
                <div className="flex items-center gap-2 text-dark-400 text-sm">
                  <Loader2 className="w-4 h-4 animate-spin" />
                  {t('common.loading')}
                </div>
              )}
            </div>

            <form onSubmit={handleSubmit} className="space-y-5" autoComplete="off" data-form-type="other">
              <div>
                <label className="block text-sm text-dark-300 mb-2 flex items-center gap-2">
                  <ServerIcon className="w-4 h-4" />
                  {t('servers.server_name')}
                </label>
                <input
                  type="text"
                  value={formData.name}
                  onChange={(e) => setFormData(d => ({ ...d, name: e.target.value }))}
                  placeholder={t('servers.server_name_placeholder')}
                  className="input"
                  required
                />
              </div>
              
              <div>
                <label className="block text-sm text-dark-300 mb-2 flex items-center gap-2">
                  <LinkIcon className="w-4 h-4" />
                  {t('servers.server_host')}
                </label>
                <div className="flex gap-3">
                  <div className="flex-1">
                    <input
                      type="text"
                      value={formData.host}
                      onChange={(e) => setFormData(d => ({ ...d, host: e.target.value }))}
                      placeholder={t('servers.server_host_placeholder')}
                      className="input"
                      required
                    />
                  </div>
                  <div className="w-28">
                    <input
                      type="text"
                      value={formData.port}
                      onChange={(e) => setFormData(d => ({ ...d, port: e.target.value.replace(/\D/g, '') }))}
                      placeholder={t('servers.server_port_placeholder')}
                      className="input text-center"
                    />
                  </div>
                </div>
                <p className="text-xs text-dark-500 mt-1.5">
                  {t('servers.server_host_hint')}
                </p>
              </div>

              {/* Авторазвёртывание по SSH */}
              <div className="rounded-xl border border-dark-700/50 bg-dark-800/30 overflow-hidden">
                <label className="flex items-center gap-3 p-4 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={deploy.enabled}
                    onChange={(e) => setDeploy(d => ({ ...d, enabled: e.target.checked }))}
                    className="w-4 h-4 rounded accent-accent-500 cursor-pointer"
                  />
                  <Rocket className="w-4 h-4 text-accent-500 flex-shrink-0" />
                  <div>
                    <span className="text-sm font-medium text-dark-100">{t('servers.deploy_title')}</span>
                    <p className="text-xs text-dark-500">{t('servers.deploy_subtitle')}</p>
                  </div>
                </label>

                <AnimatePresence>
                  {deploy.enabled && (
                    <motion.div
                      className="px-4 pb-4 space-y-4 border-t border-dark-700/50 overflow-hidden"
                      initial={{ height: 0, opacity: 0 }}
                      animate={{ height: 'auto', opacity: 1 }}
                      exit={{ height: 0, opacity: 0 }}
                      transition={{ duration: 0.15 }}
                    >
                      <div className="grid grid-cols-3 gap-3 pt-4">
                        <div>
                          <label className="block text-xs text-dark-400 mb-1.5">{t('servers.deploy_ssh_port')}</label>
                          <input
                            type="text"
                            value={deploy.sshPort}
                            onChange={(e) => setDeploy(d => ({ ...d, sshPort: e.target.value.replace(/\D/g, '') }))}
                            placeholder="22"
                            className="input text-center"
                          />
                        </div>
                        <div className="col-span-2">
                          <label className="block text-xs text-dark-400 mb-1.5">{t('servers.deploy_ssh_user')}</label>
                          <input
                            type="text"
                            value={deploy.sshUser}
                            onChange={(e) => setDeploy(d => ({ ...d, sshUser: e.target.value }))}
                            placeholder="root"
                            className="input"
                            autoComplete="off"
                          />
                        </div>
                      </div>

                      <div>
                        <label className="block text-xs text-dark-400 mb-1.5">{t('servers.deploy_ssh_auth')}</label>
                        <div className="flex gap-2 mb-2">
                          {(['password', 'key'] as const).map(m => (
                            <button
                              key={m}
                              type="button"
                              onClick={() => setDeploy(d => ({ ...d, sshAuth: m }))}
                              className={`btn text-sm flex-1 ${deploy.sshAuth === m ? 'btn-primary' : 'btn-secondary'}`}
                            >
                              {m === 'password' ? <KeyRound className="w-4 h-4" /> : <Terminal className="w-4 h-4" />}
                              {t(m === 'password' ? 'servers.deploy_auth_password' : 'servers.deploy_auth_key')}
                            </button>
                          ))}
                        </div>
                        {deploy.sshAuth === 'password' ? (
                          <input
                            type="password"
                            value={deploy.sshPassword}
                            onChange={(e) => setDeploy(d => ({ ...d, sshPassword: e.target.value }))}
                            placeholder={t('servers.deploy_ssh_password')}
                            className="input"
                            autoComplete="new-password"
                          />
                        ) : (
                          <div className="space-y-2">
                            <textarea
                              value={deploy.sshPrivateKey}
                              onChange={(e) => setDeploy(d => ({ ...d, sshPrivateKey: e.target.value }))}
                              placeholder={t('servers.deploy_ssh_key_placeholder')}
                              className="input font-mono text-xs resize-none w-full min-h-[88px]"
                            />
                            <input
                              type="password"
                              value={deploy.sshPassphrase}
                              onChange={(e) => setDeploy(d => ({ ...d, sshPassphrase: e.target.value }))}
                              placeholder={t('servers.deploy_ssh_passphrase')}
                              className="input"
                              autoComplete="new-password"
                            />
                          </div>
                        )}
                      </div>

                      <div className="space-y-3 pt-1">
                        <p className="text-xs text-dark-400">{t('servers.deploy_extras')}</p>

                        {/* WARP */}
                        <label className="flex items-center gap-2.5 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={deploy.installWarp}
                            onChange={(e) => setDeploy(d => ({ ...d, installWarp: e.target.checked }))}
                            className="w-4 h-4 rounded accent-accent-500 cursor-pointer"
                          />
                          <span className="text-sm text-dark-200">{t('servers.deploy_install_warp')}</span>
                        </label>

                        {/* Remnawave */}
                        <label className="flex items-center gap-2.5 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={deploy.installRemnawave}
                            onChange={(e) => setDeploy(d => ({
                              ...d,
                              installRemnawave: e.target.checked,
                              remnaCertMode: e.target.checked && remnaCertProfiles.length > 0 ? 'saved' : 'inline',
                            }))}
                            className="w-4 h-4 rounded accent-accent-500 cursor-pointer"
                          />
                          <span className="text-sm text-dark-200">{t('servers.deploy_install_remnawave')}</span>
                        </label>
                        {deploy.installRemnawave && (
                          <div className="ml-6 space-y-2">
                            {remnaCertProfiles.length > 0 && (
                              <div className="flex gap-2">
                                {(['saved', 'inline'] as const).map(mode => (
                                  <button
                                    key={mode}
                                    type="button"
                                    onClick={() => setDeploy(d => ({ ...d, remnaCertMode: mode }))}
                                    className={`btn text-xs flex-1 ${deploy.remnaCertMode === mode ? 'btn-primary' : 'btn-secondary'}`}
                                  >
                                    {t(mode === 'saved' ? 'servers.deploy_remna_use_saved' : 'servers.deploy_remna_new')}
                                  </button>
                                ))}
                              </div>
                            )}
                            {deploy.remnaCertMode === 'saved' && remnaCertProfiles.length > 0 ? (
                              <select
                                value={deploy.remnaCertProfileId ?? ''}
                                onChange={(e) => setDeploy(d => ({
                                  ...d,
                                  remnaCertProfileId: e.target.value ? Number(e.target.value) : null,
                                }))}
                                className="input"
                              >
                                <option value="">{t('servers.deploy_remna_select')}</option>
                                {remnaCertProfiles.map(p => (
                                  <option key={p.id} value={p.id}>{p.name}</option>
                                ))}
                              </select>
                            ) : (
                              <>
                                <textarea
                                  value={deploy.remnaCertInline}
                                  onChange={(e) => setDeploy(d => ({ ...d, remnaCertInline: e.target.value }))}
                                  placeholder={t('servers.deploy_remna_cert_placeholder')}
                                  className="input font-mono text-xs resize-none w-full min-h-[72px]"
                                />
                                <button
                                  type="button"
                                  onClick={handleSaveCert}
                                  disabled={savingCert || !deploy.remnaCertInline.trim()}
                                  className="btn btn-secondary text-xs"
                                >
                                  {savingCert ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
                                  {t('servers.deploy_remna_save')}
                                </button>
                              </>
                            )}
                          </div>
                        )}

                        {/* Installer proxy */}
                        <label className="flex items-center gap-2.5 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={deploy.installProxy}
                            onChange={(e) => setDeploy(d => ({ ...d, installProxy: e.target.checked }))}
                            className="w-4 h-4 rounded accent-accent-500 cursor-pointer"
                          />
                          <span className="text-sm text-dark-200">{t('servers.deploy_install_proxy')}</span>
                        </label>
                        {deploy.installProxy && (
                          <div className="ml-6">
                            <input
                              type="text"
                              value={deploy.proxyUrl}
                              onChange={(e) => setDeploy(d => ({ ...d, proxyUrl: e.target.value }))}
                              placeholder={t('servers.deploy_proxy_placeholder')}
                              className="input"
                              autoComplete="off"
                            />
                            <p className="text-xs text-dark-500 mt-1">{t('servers.deploy_proxy_hint')}</p>
                          </div>
                        )}
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>

              {/* Лог установки */}
              {(isDeploying || deployLog.length > 0) && (
                <div className="rounded-xl bg-dark-900/70 border border-dark-700/50 p-3">
                  <div className="flex items-center gap-2 mb-2 text-xs text-dark-400">
                    {isDeploying
                      ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                      : <Terminal className="w-3.5 h-3.5" />}
                    {t('servers.deploy_log')}
                  </div>
                  <pre
                    ref={deployLogRef}
                    className="text-[11px] leading-relaxed font-mono text-dark-300 max-h-64 overflow-auto whitespace-pre-wrap"
                  >
                    {deployLog.join('\n')}
                  </pre>
                </div>
              )}

              <div className="flex gap-3 pt-3">
                <motion.button
                  type="submit"
                  disabled={isSubmitting || isDeploying}
                  className="btn btn-primary"
                  whileTap={{ scale: 0.98 }}
                >
                  {isSubmitting || isDeploying ? (
                    <Loader2 className="w-4 h-4 animate-spin" />
                  ) : deploy.enabled ? (
                    <><Rocket className="w-4 h-4" />{t('servers.deploy_btn')}</>
                  ) : (
                    t('servers.add_server')
                  )}
                </motion.button>
                <motion.button
                  type="button"
                  onClick={handleCancel}
                  disabled={isDeploying}
                  className="btn btn-secondary"
                  whileTap={{ scale: 0.98 }}
                >
                  {t('common.cancel')}
                </motion.button>
              </div>
            </form>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Server list */}
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
        <AnimatePresence mode="popLayout">
          {filteredServers.length === 0 ? (
            searchQuery.trim() ? (
              <motion.div
                className="card text-center py-16 col-span-full"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                key="no-results"
              >
                <Search className="w-12 h-12 text-dark-600 mx-auto mb-3" />
                <p className="text-dark-400">{t('common.no_results')}</p>
              </motion.div>
            ) : (
              <motion.div
                className="card text-center py-16 col-span-full"
                initial={{ opacity: 0, scale: 0.95 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.95 }}
                key="empty"
              >
                <motion.div
                  animate={{ y: [0, -6, 0] }}
                  transition={{ duration: 2.5, repeat: Infinity, ease: 'easeInOut' }}
                >
                  <ServerIcon className="w-16 h-16 text-dark-600 mx-auto mb-4" />
                </motion.div>
                <p className="text-dark-400 mb-4">{t('servers.no_servers')}</p>
                <motion.button
                  onClick={() => setShowForm(true)}
                  className="btn btn-primary mx-auto"
                  whileTap={{ scale: 0.97 }}
                >
                  <Plus className="w-4 h-4" />
                  {t('servers.add_first_server')}
                </motion.button>
              </motion.div>
            )
          ) : (
            filteredServers.map((server) => {
              const isEditing = editingId === server.id
              const isTesting = testingId === server.id
              const testResult = testResults[server.id]

              return (
                <motion.div
                  key={server.id}
                  className={isEditing ? 'col-span-full' : ''}
                  layout
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, scale: 0.95 }}
                  transition={{ duration: 0.15 }}
                >
                  <div
                    className={`card group transition-all overflow-visible flex flex-col ${
                      server.is_active
                        ? 'hover:border-dark-700'
                        : 'opacity-60 border-dark-700/50'
                    } ${isEditing ? 'rounded-b-none border-b-0 border-accent-500/30' : ''}`}
                  >
                    {/* Шапка: иконка + имя + URL */}
                    <div className="flex items-center gap-3">
                      <div
                        className={`w-10 h-10 rounded-xl flex items-center justify-center border flex-shrink-0 transition-colors ${
                          server.is_active
                            ? 'bg-gradient-to-br from-dark-700 to-dark-800 border-dark-700/50 group-hover:border-accent-500/30'
                            : 'bg-dark-800/50 border-dark-700/30'
                        }`}
                      >
                        <ServerIcon className={`w-4 h-4 ${server.is_active ? 'text-accent-500' : 'text-dark-500'}`} />
                      </div>
                      <div className="min-w-0 flex-1">
                        <h3 className="font-semibold text-dark-100 flex items-center gap-2 truncate">
                          <span className="truncate">{server.name}</span>
                          <span
                            className={`w-2 h-2 rounded-full flex-shrink-0 ${
                              !server.is_active
                                ? 'bg-dark-500'
                                : server.status === 'online'
                                  ? 'bg-success'
                                  : server.status === 'loading'
                                    ? 'bg-dark-400 animate-pulse'
                                    : 'bg-danger'
                            }`}
                          />
                          {server.uses_shared_cert ? (
                            <Tooltip label={t('servers.shared_cert_tooltip')}>
                              <ShieldCheck className="w-3.5 h-3.5 text-success flex-shrink-0" />
                            </Tooltip>
                          ) : (
                            <Tooltip label={t('servers.needs_migration_tooltip')}>
                              <span className="flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-warning/10 text-warning text-[10px] font-medium flex-shrink-0">
                                <ShieldAlert className="w-3 h-3" />
                                {server.auth_kind === 'legacy'
                                  ? t('servers.legacy_badge')
                                  : t('servers.old_key_badge')}
                              </span>
                            </Tooltip>
                          )}
                        </h3>
                        <p className="text-xs text-dark-500 flex items-center gap-1.5 min-w-0">
                          <CopyableIp value={extractHost(server.url)} display={server.url} className="truncate" />
                          <a
                            href={server.url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="hover:text-accent-400 transition-colors flex-shrink-0"
                            onClick={(e) => e.stopPropagation()}
                          >
                            <ExternalLink className="w-3 h-3" />
                          </a>
                        </p>
                      </div>
                    </div>

                    {/* Статус + кнопки действий */}
                    <div className="flex items-center justify-between mt-3 pt-3 border-t border-dark-700/30 gap-2">
                      <div className="flex-1 min-w-0">
                        <AnimatePresence mode="wait">
                          {testResult ? (
                            <motion.div
                              key="test-result"
                              className={`flex items-center gap-1.5 text-xs px-2 py-1 rounded-lg w-fit ${
                                testResult.status === 'online'
                                  ? 'text-success bg-success/10'
                                  : 'text-danger bg-danger/10'
                              }`}
                              initial={{ opacity: 0, scale: 0.8 }}
                              animate={{ opacity: 1, scale: 1 }}
                              exit={{ opacity: 0, scale: 0.8 }}
                            >
                              {testResult.status === 'online' ? (
                                <CheckCircle2 className="w-3.5 h-3.5" />
                              ) : (
                                <XCircle className="w-3.5 h-3.5" />
                              )}
                              <span className="truncate max-w-[140px]">
                                {testResult.status === 'online'
                                  ? t('common.connected')
                                  : testResult.message || t('common.failed')}
                              </span>
                            </motion.div>
                          ) : !server.is_active ? (
                            <motion.span
                              key="disabled"
                              className="px-2 py-0.5 text-xs font-medium bg-dark-700/50 text-dark-400 rounded-full"
                              initial={{ opacity: 0 }}
                              animate={{ opacity: 1 }}
                            >
                              {t('servers.disabled')}
                            </motion.span>
                          ) : server.status === 'online' ? (
                            <motion.span
                              key="online"
                              className="flex items-center gap-1.5 text-xs text-success"
                              initial={{ opacity: 0 }}
                              animate={{ opacity: 1 }}
                            >
                              <CheckCircle2 className="w-3.5 h-3.5" />
                              {t('common.online')}
                            </motion.span>
                          ) : server.status === 'loading' ? (
                            <motion.span
                              key="loading"
                              className="flex items-center gap-1.5 text-xs text-dark-400"
                              initial={{ opacity: 0 }}
                              animate={{ opacity: 1 }}
                            >
                              <Loader2 className="w-3.5 h-3.5 animate-spin" />
                              {t('common.loading')}
                            </motion.span>
                          ) : (
                            <motion.span
                              key="offline"
                              className="flex items-center gap-1.5 text-xs text-danger"
                              initial={{ opacity: 0 }}
                              animate={{ opacity: 1 }}
                            >
                              <XCircle className="w-3.5 h-3.5" />
                              <span className="truncate max-w-[140px]">
                                {server.last_error || t('common.offline')}
                              </span>
                            </motion.span>
                          )}
                        </AnimatePresence>
                      </div>

                      <div className="flex items-center gap-1 flex-shrink-0">
                        <Tooltip label={server.is_active ? t('servers.monitoring_enabled') : t('servers.monitoring_disabled')}>
                          <motion.button
                            onClick={() => toggleServer(server.id, !server.is_active)}
                            className={`p-2 rounded-lg transition-all ${
                              server.is_active
                                ? 'text-success hover:bg-success/10'
                                : 'text-dark-500 hover:bg-dark-700/50'
                            }`}
                            whileTap={{ scale: 0.9 }}
                          >
                            <Power className="w-3.5 h-3.5" />
                          </motion.button>
                        </Tooltip>

                        <Tooltip label={t('common.test')}>
                          <motion.button
                            onClick={() => handleTest(server.id)}
                            disabled={isTesting || !server.is_active}
                            className={`p-2 rounded-lg transition-all text-dark-300 hover:bg-dark-700/50 ${
                              !server.is_active ? 'opacity-40 cursor-not-allowed' : ''
                            }`}
                            whileTap={{ scale: server.is_active ? 0.9 : 1 }}
                          >
                            {isTesting ? (
                              <Loader2 className="w-3.5 h-3.5 animate-spin" />
                            ) : (
                              <Zap className="w-3.5 h-3.5" />
                            )}
                          </motion.button>
                        </Tooltip>

                        <Tooltip label={isEditing ? t('common.cancel') : t('common.edit')}>
                          <motion.button
                            onClick={() => (isEditing ? handleCancel() : handleEdit(server))}
                            className={`p-2 rounded-lg transition-all ${
                              isEditing
                                ? 'text-accent-400 bg-accent-500/10'
                                : 'text-dark-300 hover:bg-dark-700/50'
                            }`}
                            whileTap={{ scale: 0.9 }}
                          >
                            {isEditing ? <X className="w-3.5 h-3.5" /> : <Edit2 className="w-3.5 h-3.5" />}
                          </motion.button>
                        </Tooltip>

                        <Tooltip label={t('common.delete')}>
                          <motion.button
                            onClick={() => handleDelete(server.id)}
                            className={`p-2 rounded-lg transition-all ${
                              deleteConfirm === server.id
                                ? 'bg-danger text-white'
                                : 'text-danger hover:bg-danger/10'
                            }`}
                            whileTap={{ scale: 0.9 }}
                            animate={deleteConfirm === server.id ? { scale: [1, 1.1, 1] } : {}}
                            transition={{ duration: 0.15 }}
                          >
                            <Trash2 className="w-3.5 h-3.5" />
                          </motion.button>
                        </Tooltip>
                      </div>
                    </div>
                  </div>

                  {/* Inline Edit Form */}
                  <AnimatePresence>
                    {isEditing && (
                      <motion.div
                        className="card rounded-t-none border-t-0 border-accent-500/30 bg-dark-800/60"
                        initial={{ opacity: 0, height: 0 }}
                        animate={{ opacity: 1, height: 'auto' }}
                        exit={{ opacity: 0, height: 0 }}
                        transition={{ duration: 0.15 }}
                      >
                        <AnimatePresence>
                          {error && (
                            <motion.div
                              className="flex items-center gap-3 p-4 mb-4 bg-danger/10 border border-danger/20 rounded-xl text-danger"
                              initial={{ opacity: 0, y: -10 }}
                              animate={{ opacity: 1, y: 0 }}
                              exit={{ opacity: 0, y: -10 }}
                            >
                              <AlertTriangle className="w-5 h-5 flex-shrink-0" />
                              <span className="text-sm">{error}</span>
                            </motion.div>
                          )}
                        </AnimatePresence>

                        <form onSubmit={handleSubmit} className="space-y-4" autoComplete="off" data-form-type="other">
                          <div>
                            <label className="block text-sm text-dark-300 mb-2 flex items-center gap-2">
                              <ServerIcon className="w-4 h-4" />
                              {t('servers.server_name')}
                            </label>
                            <input
                              type="text"
                              value={formData.name}
                              onChange={(e) => setFormData(d => ({ ...d, name: e.target.value }))}
                              placeholder={t('servers.server_name_placeholder')}
                              className="input"
                              required
                            />
                          </div>

                          <div>
                            <label className="block text-sm text-dark-300 mb-2 flex items-center gap-2">
                              <LinkIcon className="w-4 h-4" />
                              {t('servers.server_host')}
                            </label>
                            <div className="flex gap-3">
                              <div className="flex-1">
                                <input
                                  type="text"
                                  value={formData.host}
                                  onChange={(e) => setFormData(d => ({ ...d, host: e.target.value }))}
                                  placeholder={t('servers.server_host_placeholder')}
                                  className="input"
                                  required
                                />
                              </div>
                              <div className="w-28">
                                <input
                                  type="text"
                                  value={formData.port}
                                  onChange={(e) => setFormData(d => ({ ...d, port: e.target.value.replace(/\D/g, '') }))}
                                  placeholder={t('servers.server_port_placeholder')}
                                  className="input text-center"
                                />
                              </div>
                            </div>
                            <p className="text-xs text-dark-500 mt-1.5">
                              {t('servers.server_host_hint')}
                            </p>
                          </div>

                          <div className="flex gap-3 pt-2">
                            <motion.button
                              type="submit"
                              disabled={isSubmitting}
                              className="btn btn-primary"
                              whileTap={{ scale: 0.98 }}
                            >
                              {isSubmitting ? (
                                <Loader2 className="w-4 h-4 animate-spin" />
                              ) : (
                                t('servers.update_server')
                              )}
                            </motion.button>
                            <motion.button
                              type="button"
                              onClick={handleCancel}
                              className="btn btn-secondary"
                              whileTap={{ scale: 0.98 }}
                            >
                              {t('common.cancel')}
                            </motion.button>
                          </div>
                        </form>
                      </motion.div>
                    )}
                  </AnimatePresence>
                </motion.div>
              )
            })
          )}
        </AnimatePresence>
      </div>

    </motion.div>
  )
}
