import { useState, useEffect, useMemo, FormEvent } from 'react'
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
  Search
} from 'lucide-react'
import { useServersStore } from '../stores/serversStore'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { serversApi, systemApi } from '../api/client'
import InfraTree from '../components/Infra/InfraTree'
import { Tooltip } from '../components/ui/Tooltip'
import { FAQIcon } from '../components/FAQ'
import MigrationBanner from '../components/MigrationBanner'

interface ServerFormData {
  name: string
  host: string
  port: string
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
    setShowForm(false)
    setEditingId(null)
    setFormData({ name: '', host: '', port: '9100' })
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

              <div className="flex gap-3 pt-3">
                <motion.button
                  type="submit"
                  disabled={isSubmitting}
                  className="btn btn-primary"
                  whileTap={{ scale: 0.98 }}
                >
                  {isSubmitting ? (
                    <Loader2 className="w-4 h-4 animate-spin" />
                  ) : (
                    t('servers.add_server')
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
                          <span className="truncate">{server.url}</span>
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
