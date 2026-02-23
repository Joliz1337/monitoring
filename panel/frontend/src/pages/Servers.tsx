import { useState, useEffect, FormEvent } from 'react'
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
  Key,
  AlertTriangle,
  Power,
  Copy,
  Check,
  Globe
} from 'lucide-react'
import { useServersStore } from '../stores/serversStore'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { systemApi } from '../api/client'

interface ServerFormData {
  name: string
  host: string
  port: string
  api_key: string
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
  const { servers, fetchServers, addServer, deleteServer, testServer, updateServer, toggleServer } = useServersStore()
  const { t } = useTranslation()
  
  const [showForm, setShowForm] = useState(false)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [formData, setFormData] = useState<ServerFormData>({ name: '', host: '', port: '9100', api_key: '' })
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [testResults, setTestResults] = useState<Record<number, { status: string; message?: string }>>({})
  const [testingId, setTestingId] = useState<number | null>(null)
  const [deleteConfirm, setDeleteConfirm] = useState<number | null>(null)
  const [copied, setCopied] = useState(false)
  const [panelIp, setPanelIp] = useState<string | null>(null)
  
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
    fetchServers()
    systemApi.getPanelIp().then(res => {
      setPanelIp(res.data.ip)
    }).catch(() => {})
  }, [fetchServers])
  
  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    setError('')
    setIsSubmitting(true)
    
    if (editingId) {
      // При редактировании отправляем только заполненные поля
      // Если api_key пустой - не отправляем его, чтобы не затереть существующий
      const updateData: { name: string; url: string; api_key?: string } = {
        name: formData.name,
        url: buildServerUrl(formData.host, formData.port),
      }
      if (formData.api_key) {
        updateData.api_key = formData.api_key
      }
      
      try {
        await updateServer(editingId, updateData)
        toast.success(t('servers.server_updated'))
        setEditingId(null)
        setFormData({ name: '', host: '', port: '9100', api_key: '' })
        setShowForm(false)
      } catch {
        toast.error(t('servers.failed_update'))
        setError(t('servers.failed_update'))
      }
    } else {
      const serverData = {
        name: formData.name,
        url: buildServerUrl(formData.host, formData.port),
        api_key: formData.api_key
      }
      const result = await addServer(serverData)
      if (result.success) {
        toast.success(t('servers.server_added'))
        setShowForm(false)
        setFormData({ name: '', host: '', port: '9100', api_key: '' })
      } else {
        toast.error(result.error || t('servers.failed_add'))
        setError(result.error || t('servers.failed_add'))
      }
    }
    
    setIsSubmitting(false)
  }
  
  const handleEdit = (server: typeof servers[0]) => {
    setEditingId(server.id)
    const { host, port } = parseServerUrl(server.url)
    setFormData({
      name: server.name,
      host,
      port,
      api_key: ''
    })
    setShowForm(true)
    setError('')
  }
  
  const handleCancel = () => {
    setShowForm(false)
    setEditingId(null)
    setFormData({ name: '', host: '', port: '9100', api_key: '' })
    setError('')
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
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4 }}
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
            whileHover={{ scale: 1.02 }}
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
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
      >
        <div>
          <motion.h1 
            className="text-2xl font-bold text-dark-50 flex items-center gap-3"
            initial={{ opacity: 0, x: -20 }}
            animate={{ opacity: 1, x: 0 }}
          >
            <ServerIcon className="w-7 h-7 text-accent-400" />
            {t('servers.title')}
          </motion.h1>
          <motion.p 
            className="text-dark-400 mt-1"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 0.1 }}
          >
            {t('servers.subtitle')}
          </motion.p>
        </div>
        
        <AnimatePresence>
          {!showForm && (
            <motion.button 
              onClick={() => setShowForm(true)} 
              className="btn btn-primary"
              initial={{ opacity: 0, scale: 0.9 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.9 }}
              whileHover={{ scale: 1.02 }}
              whileTap={{ scale: 0.98 }}
            >
              <Plus className="w-4 h-4" />
              {t('servers.add_server')}
            </motion.button>
          )}
        </AnimatePresence>
      </motion.div>
      
      {/* Form */}
      <AnimatePresence>
        {showForm && (
          <motion.div 
            className="card mb-6 overflow-hidden"
            initial={{ opacity: 0, height: 0, marginBottom: 0 }}
            animate={{ opacity: 1, height: 'auto', marginBottom: 24 }}
            exit={{ opacity: 0, height: 0, marginBottom: 0 }}
            transition={{ duration: 0.3 }}
          >
            <div className="flex items-center justify-between mb-6">
              <h2 className="text-lg font-semibold text-dark-100 flex items-center gap-2">
                {editingId ? (
                  <>
                    <Edit2 className="w-5 h-5 text-accent-500" />
                    {t('servers.edit_server')}
                  </>
                ) : (
                  <>
                    <Plus className="w-5 h-5 text-accent-500" />
                    {t('servers.add_new_server')}
                  </>
                )}
              </h2>
              <motion.button 
                onClick={handleCancel} 
                className="p-2 hover:bg-dark-700 rounded-xl text-dark-400 transition-colors"
                whileHover={{ scale: 1.1, rotate: 90 }}
                whileTap={{ scale: 0.9 }}
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
            
            <form onSubmit={handleSubmit} className="space-y-5" autoComplete="off" data-form-type="other">
              <motion.div
                initial={{ opacity: 0, x: -20 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: 0.1 }}
              >
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
              </motion.div>
              
              <motion.div
                initial={{ opacity: 0, x: -20 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: 0.15 }}
              >
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
              </motion.div>
              
              <motion.div
                initial={{ opacity: 0, x: -20 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: 0.2 }}
              >
                <label className="block text-sm text-dark-300 mb-2 flex items-center gap-2">
                  <Key className="w-4 h-4" />
                  {t('servers.api_key')}
                  {editingId && (
                    <span className="text-dark-500 font-normal">({t('servers.api_key_hint')})</span>
                  )}
                </label>
                <input
                  type="password"
                  value={formData.api_key}
                  onChange={(e) => setFormData(d => ({ ...d, api_key: e.target.value }))}
                  placeholder={t('servers.api_key_placeholder')}
                  className="input"
                  required={!editingId}
                  autoComplete="new-password"
                  data-lpignore="true"
                  data-form-type="other"
                />
              </motion.div>
              
              <motion.div 
                className="flex gap-3 pt-3"
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.25 }}
              >
                <motion.button 
                  type="submit" 
                  disabled={isSubmitting} 
                  className="btn btn-primary"
                  whileHover={{ scale: 1.02 }}
                  whileTap={{ scale: 0.98 }}
                >
                  {isSubmitting ? (
                    <motion.div
                      animate={{ rotate: 360 }}
                      transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}
                    >
                      <Loader2 className="w-4 h-4" />
                    </motion.div>
                  ) : editingId ? (
                    t('servers.update_server')
                  ) : (
                    t('servers.add_server')
                  )}
                </motion.button>
                <motion.button 
                  type="button" 
                  onClick={handleCancel} 
                  className="btn btn-secondary"
                  whileHover={{ scale: 1.02 }}
                  whileTap={{ scale: 0.98 }}
                >
                  {t('common.cancel')}
                </motion.button>
              </motion.div>
            </form>
          </motion.div>
        )}
      </AnimatePresence>
      
      {/* Server list */}
      <motion.div className="space-y-4" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
        <AnimatePresence mode="popLayout">
          {servers.length === 0 ? (
            <motion.div 
              className="card text-center py-16"
              initial={{ opacity: 0, scale: 0.95 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.95 }}
              key="empty"
            >
              <motion.div
                animate={{ y: [0, -10, 0] }}
                transition={{ duration: 3, repeat: Infinity, ease: 'easeInOut' }}
              >
                <ServerIcon className="w-16 h-16 text-dark-600 mx-auto mb-4" />
              </motion.div>
              <p className="text-dark-400 mb-4">{t('servers.no_servers')}</p>
              <motion.button
                onClick={() => setShowForm(true)}
                className="btn btn-primary mx-auto"
                whileHover={{ scale: 1.05 }}
                whileTap={{ scale: 0.95 }}
              >
                <Plus className="w-4 h-4" />
                {t('servers.add_first_server')}
              </motion.button>
            </motion.div>
          ) : (
            servers.map((server, index) => (
              <motion.div 
                key={server.id} 
                className={`card group transition-all duration-300 ${
                  server.is_active 
                    ? 'hover:border-dark-700' 
                    : 'opacity-60 border-dark-700/50'
                }`}
                layout
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: server.is_active ? 1 : 0.6, y: 0 }}
                exit={{ opacity: 0, x: -100 }}
                transition={{ delay: index * 0.05 }}
                whileHover={{ scale: server.is_active ? 1.01 : 1 }}
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-4">
                    <motion.div 
                      className={`w-12 h-12 rounded-xl bg-gradient-to-br from-dark-700 to-dark-800 
                                 flex items-center justify-center border transition-colors ${
                                   server.is_active 
                                     ? 'border-dark-700/50 group-hover:border-accent-500/30' 
                                     : 'border-dark-700/30'
                                 }`}
                      whileHover={{ rotate: server.is_active ? 5 : 0, scale: server.is_active ? 1.05 : 1 }}
                    >
                      <ServerIcon className={`w-5 h-5 ${server.is_active ? 'text-accent-500' : 'text-dark-500'}`} />
                    </motion.div>
                    <div>
                      <div className="flex items-center gap-2">
                        <h3 className={`font-semibold transition-colors ${
                          server.is_active 
                            ? 'text-dark-100 group-hover:text-white' 
                            : 'text-dark-400'
                        }`}>
                          {server.name}
                        </h3>
                        {!server.is_active && (
                          <span className="text-xs px-2 py-0.5 rounded-md bg-dark-700/50 text-dark-400">
                            {t('servers.disabled')}
                          </span>
                        )}
                      </div>
                      <p className="text-sm text-dark-500 flex items-center gap-1.5">
                        {server.url}
                        <a 
                          href={server.url} 
                          target="_blank" 
                          rel="noopener noreferrer"
                          className="hover:text-accent-400 transition-colors"
                          onClick={(e) => e.stopPropagation()}
                        >
                          <ExternalLink className="w-3 h-3" />
                        </a>
                      </p>
                    </div>
                  </div>
                  
                  <div className="flex items-center gap-2">
                    <AnimatePresence mode="wait">
                      {testResults[server.id] && (
                        <motion.div 
                          className={`flex items-center gap-1.5 text-sm px-3 py-1.5 rounded-lg ${
                            testResults[server.id].status === 'online' 
                              ? 'text-success bg-success/10' 
                              : 'text-danger bg-danger/10'
                          }`}
                          initial={{ opacity: 0, scale: 0.8 }}
                          animate={{ opacity: 1, scale: 1 }}
                          exit={{ opacity: 0, scale: 0.8 }}
                        >
                          {testResults[server.id].status === 'online' ? (
                            <CheckCircle2 className="w-4 h-4" />
                          ) : (
                            <XCircle className="w-4 h-4" />
                          )}
                          <span className="hidden sm:inline">
                            {testResults[server.id].status === 'online' 
                              ? t('common.connected') 
                              : testResults[server.id].message || t('common.failed')}
                          </span>
                        </motion.div>
                      )}
                    </AnimatePresence>
                    
                    {/* Monitoring toggle */}
                    <motion.button
                      onClick={() => toggleServer(server.id, !server.is_active)}
                      className={`relative flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm transition-all ${
                        server.is_active
                          ? 'bg-success/10 text-success hover:bg-success/20'
                          : 'bg-dark-700/50 text-dark-400 hover:bg-dark-700'
                      }`}
                      whileHover={{ scale: 1.02 }}
                      whileTap={{ scale: 0.98 }}
                      title={server.is_active ? t('servers.monitoring_enabled') : t('servers.monitoring_disabled')}
                    >
                      <Power className="w-4 h-4" />
                      <div className={`w-8 h-4 rounded-full transition-colors ${
                        server.is_active ? 'bg-success' : 'bg-dark-600'
                      }`}>
                        <motion.div
                          className="w-3 h-3 rounded-full bg-white shadow-sm mt-0.5"
                          animate={{ x: server.is_active ? 17 : 2 }}
                          transition={{ type: 'spring', stiffness: 500, damping: 30 }}
                        />
                      </div>
                    </motion.button>
                    
                    <motion.button
                      onClick={() => handleTest(server.id)}
                      disabled={testingId === server.id || !server.is_active}
                      className={`btn btn-secondary text-sm ${!server.is_active ? 'opacity-50 cursor-not-allowed' : ''}`}
                      whileHover={{ scale: server.is_active ? 1.05 : 1 }}
                      whileTap={{ scale: server.is_active ? 0.95 : 1 }}
                    >
                      {testingId === server.id ? (
                        <motion.div
                          animate={{ rotate: 360 }}
                          transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}
                        >
                          <Loader2 className="w-4 h-4" />
                        </motion.div>
                      ) : (
                        t('common.test')
                      )}
                    </motion.button>
                    
                    <motion.button
                      onClick={() => handleEdit(server)}
                      className="btn btn-ghost p-2.5"
                      whileHover={{ scale: 1.1 }}
                      whileTap={{ scale: 0.9 }}
                    >
                      <Edit2 className="w-4 h-4" />
                    </motion.button>
                    
                    <motion.button
                      onClick={() => handleDelete(server.id)}
                      className={`btn p-2.5 transition-all ${
                        deleteConfirm === server.id 
                          ? 'bg-danger text-white' 
                          : 'btn-ghost text-danger hover:bg-danger/10'
                      }`}
                      whileHover={{ scale: 1.1 }}
                      whileTap={{ scale: 0.9 }}
                      animate={deleteConfirm === server.id ? { scale: [1, 1.1, 1] } : {}}
                      transition={{ duration: 0.3 }}
                    >
                      <Trash2 className="w-4 h-4" />
                    </motion.button>
                  </div>
                </div>
              </motion.div>
            ))
          )}
        </AnimatePresence>
      </motion.div>
    </motion.div>
  )
}
