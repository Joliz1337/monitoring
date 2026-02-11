import { useEffect, useState, useCallback } from 'react'
import { Shield, Plus, Trash2, RefreshCw, Server, Globe, List, Loader2, ExternalLink, AlertCircle, Check, X, ArrowDownToLine, ArrowUpFromLine, ShieldBan, Power, PowerOff, Info } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { motion, AnimatePresence } from 'framer-motion'
import { blocklistApi, serversApi, Server as ServerType, BlocklistRule, BlocklistSource, BlocklistDirection, TorrentBlockerStatus } from '../api/client'

type TabType = 'global' | 'servers' | 'sources' | 'torrent'

export default function Blocklist() {
  const { t } = useTranslation()
  
  // Direction state
  const [direction, setDirection] = useState<BlocklistDirection>('in')
  
  // State
  const [activeTab, setActiveTab] = useState<TabType>('global')
  const [loading, setLoading] = useState(true)
  const [syncing, setSyncing] = useState(false)
  
  // Global rules
  const [globalRules, setGlobalRules] = useState<BlocklistRule[]>([])
  const [newGlobalIps, setNewGlobalIps] = useState('')
  const [addingGlobal, setAddingGlobal] = useState(false)
  
  // Server rules
  const [servers, setServers] = useState<ServerType[]>([])
  const [selectedServerId, setSelectedServerId] = useState<number | null>(null)
  const [serverRules, setServerRules] = useState<BlocklistRule[]>([])
  const [newServerIp, setNewServerIp] = useState('')
  const [addingServer, setAddingServer] = useState(false)
  const [serverGlobalCount, setServerGlobalCount] = useState(0)
  
  // Sources
  const [sources, setSources] = useState<BlocklistSource[]>([])
  const [newSourceName, setNewSourceName] = useState('')
  const [newSourceUrl, setNewSourceUrl] = useState('')
  const [addingSource, setAddingSource] = useState(false)
  const [refreshingSource, setRefreshingSource] = useState<number | null>(null)
  
  // Settings
  const [tempTimeout, setTempTimeout] = useState(600)
  const [savingSettings, setSavingSettings] = useState(false)
  
  // Torrent blocker
  const [torrentStatuses, setTorrentStatuses] = useState<TorrentBlockerStatus[]>([])
  const [torrentLoading, setTorrentLoading] = useState(false)
  const [togglingServer, setTogglingServer] = useState<number | null>(null)
  
  // Fetch data
  const fetchGlobalRules = useCallback(async () => {
    try {
      const response = await blocklistApi.getGlobal(direction)
      setGlobalRules(response.data.rules)
    } catch (err) {
      console.error('Failed to fetch global rules:', err)
    }
  }, [direction])
  
  const fetchServers = useCallback(async () => {
    try {
      const response = await serversApi.list()
      setServers(response.data.servers)
      if (response.data.servers.length > 0 && !selectedServerId) {
        setSelectedServerId(response.data.servers[0].id)
      }
    } catch (err) {
      console.error('Failed to fetch servers:', err)
    }
  }, [selectedServerId])
  
  const fetchServerRules = useCallback(async () => {
    if (!selectedServerId) return
    try {
      const response = await blocklistApi.getServer(selectedServerId, direction)
      setServerRules(response.data.rules)
      setServerGlobalCount(response.data.global_count)
    } catch (err) {
      console.error('Failed to fetch server rules:', err)
    }
  }, [selectedServerId, direction])
  
  const fetchSources = useCallback(async () => {
    try {
      const response = await blocklistApi.getSources(direction)
      setSources(response.data.sources)
    } catch (err) {
      console.error('Failed to fetch sources:', err)
    }
  }, [direction])
  
  const fetchSettings = useCallback(async () => {
    try {
      const response = await blocklistApi.getSettings()
      setTempTimeout(response.data.settings.temp_timeout || 600)
    } catch (err) {
      console.error('Failed to fetch settings:', err)
    }
  }, [])
  
  const fetchTorrentStatus = useCallback(async () => {
    setTorrentLoading(true)
    try {
      const response = await blocklistApi.getTorrentBlockerStatus()
      setTorrentStatuses(response.data.servers)
    } catch (err) {
      console.error('Failed to fetch torrent blocker status:', err)
    } finally {
      setTorrentLoading(false)
    }
  }, [])
  
  const [initialLoaded, setInitialLoaded] = useState(false)
  
  useEffect(() => {
    const loadData = async () => {
      setLoading(true)
      await Promise.all([
        fetchGlobalRules(),
        fetchServers(),
        fetchSources(),
        fetchSettings()
      ])
      setLoading(false)
      setInitialLoaded(true)
    }
    if (!initialLoaded) {
      loadData()
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  
  useEffect(() => {
    if (selectedServerId) {
      fetchServerRules()
    }
  }, [selectedServerId, fetchServerRules])
  
  // Re-fetch data when direction changes (without full loading state)
  useEffect(() => {
    if (!initialLoaded) return
    fetchGlobalRules()
    fetchSources()
    if (selectedServerId) {
      fetchServerRules()
    }
  }, [direction]) // eslint-disable-line react-hooks/exhaustive-deps
  
  // Load torrent blocker data when tab is selected
  const [torrentLoaded, setTorrentLoaded] = useState(false)
  useEffect(() => {
    if (activeTab === 'torrent' && !torrentLoaded) {
      fetchTorrentStatus().then(() => setTorrentLoaded(true))
    }
  }, [activeTab]) // eslint-disable-line react-hooks/exhaustive-deps
  
  // Handlers
  const handleAddGlobalRules = async () => {
    if (!newGlobalIps.trim()) return
    
    setAddingGlobal(true)
    try {
      const ips = newGlobalIps.split('\n').map(ip => ip.trim()).filter(ip => ip)
      await blocklistApi.addGlobalBulk(ips, true, direction)
      setNewGlobalIps('')
      await fetchGlobalRules()
    } catch (err: any) {
      console.error('Failed to add rules:', err)
      alert(err.response?.data?.detail || 'Failed to add rules')
    } finally {
      setAddingGlobal(false)
    }
  }
  
  const handleDeleteGlobalRule = async (ruleId: number) => {
    try {
      await blocklistApi.deleteGlobal(ruleId)
      await fetchGlobalRules()
    } catch (err) {
      console.error('Failed to delete rule:', err)
    }
  }
  
  const handleAddServerRule = async () => {
    if (!newServerIp.trim() || !selectedServerId) return
    
    setAddingServer(true)
    try {
      await blocklistApi.addServer(selectedServerId, { ip_cidr: newServerIp.trim(), direction })
      setNewServerIp('')
      await fetchServerRules()
    } catch (err: any) {
      console.error('Failed to add rule:', err)
      alert(err.response?.data?.detail || 'Failed to add rule')
    } finally {
      setAddingServer(false)
    }
  }
  
  const handleDeleteServerRule = async (ruleId: number) => {
    if (!selectedServerId) return
    try {
      await blocklistApi.deleteServer(selectedServerId, ruleId)
      await fetchServerRules()
    } catch (err) {
      console.error('Failed to delete rule:', err)
    }
  }
  
  const handleAddSource = async () => {
    if (!newSourceName.trim() || !newSourceUrl.trim()) return
    
    setAddingSource(true)
    try {
      await blocklistApi.addSource({ name: newSourceName.trim(), url: newSourceUrl.trim(), direction })
      setNewSourceName('')
      setNewSourceUrl('')
      await fetchSources()
    } catch (err: any) {
      console.error('Failed to add source:', err)
      alert(err.response?.data?.detail || 'Failed to add source')
    } finally {
      setAddingSource(false)
    }
  }
  
  const handleToggleSource = async (sourceId: number, enabled: boolean) => {
    try {
      await blocklistApi.updateSource(sourceId, { enabled })
      await fetchSources()
    } catch (err) {
      console.error('Failed to update source:', err)
    }
  }
  
  const handleRefreshSource = async (sourceId: number) => {
    setRefreshingSource(sourceId)
    try {
      await blocklistApi.refreshSource(sourceId)
      await fetchSources()
    } catch (err: any) {
      console.error('Failed to refresh source:', err)
      alert(err.response?.data?.detail || 'Failed to refresh')
    } finally {
      setRefreshingSource(null)
    }
  }
  
  const handleRefreshAllSources = async () => {
    setRefreshingSource(-1)
    try {
      await blocklistApi.refreshAll()
      await fetchSources()
    } catch (err) {
      console.error('Failed to refresh sources:', err)
    } finally {
      setRefreshingSource(null)
    }
  }
  
  const handleDeleteSource = async (sourceId: number) => {
    if (!confirm(t('blocklist.confirm_delete_source'))) return
    try {
      await blocklistApi.deleteSource(sourceId)
      await fetchSources()
    } catch (err: any) {
      console.error('Failed to delete source:', err)
      alert(err.response?.data?.detail || 'Cannot delete default source')
    }
  }
  
  const handleSyncAll = async () => {
    setSyncing(true)
    try {
      await blocklistApi.sync()
    } catch (err) {
      console.error('Failed to sync:', err)
    } finally {
      setSyncing(false)
    }
  }
  
  const handleSaveSettings = async () => {
    setSavingSettings(true)
    try {
      await blocklistApi.updateSettings({ temp_timeout: tempTimeout })
    } catch (err) {
      console.error('Failed to save settings:', err)
    } finally {
      setSavingSettings(false)
    }
  }
  
  const handleToggleTorrentBlocker = async (serverId: number, enable: boolean) => {
    setTogglingServer(serverId)
    try {
      if (enable) {
        await blocklistApi.enableTorrentBlocker(serverId)
      } else {
        await blocklistApi.disableTorrentBlocker(serverId)
      }
      await fetchTorrentStatus()
    } catch (err: any) {
      console.error('Failed to toggle torrent blocker:', err)
      alert(err.response?.data?.detail || 'Failed to toggle torrent blocker')
    } finally {
      setTogglingServer(null)
    }
  }
  
  const directionButtons = [
    { id: 'in' as BlocklistDirection, icon: ArrowDownToLine, label: t('blocklist.direction_incoming') },
    { id: 'out' as BlocklistDirection, icon: ArrowUpFromLine, label: t('blocklist.direction_outgoing') }
  ]
  
  const tabs = [
    { id: 'global' as TabType, icon: Globe, label: t('blocklist.global_rules') },
    { id: 'servers' as TabType, icon: Server, label: t('blocklist.server_rules') },
    { id: 'sources' as TabType, icon: List, label: t('blocklist.auto_lists') },
    { id: 'torrent' as TabType, icon: ShieldBan, label: t('blocklist.torrent_blocker') }
  ]
  
  const containerVariants = {
    hidden: { opacity: 0 },
    visible: { 
      opacity: 1,
      transition: { staggerChildren: 0.1 }
    }
  }
  
  const itemVariants = {
    hidden: { opacity: 0, y: 20 },
    visible: { opacity: 1, y: 0 }
  }
  
  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-8 h-8 text-accent-500 animate-spin" />
      </div>
    )
  }
  
  return (
    <motion.div
      initial="hidden"
      animate="visible"
      variants={containerVariants}
      className="space-y-6"
    >
      {/* Header */}
      <motion.div variants={itemVariants} className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Shield className="w-7 h-7 text-accent-400" />
          <div>
            <h1 className="text-2xl font-bold text-dark-50">{t('blocklist.title')}</h1>
            <p className="text-dark-400 text-sm">{t('blocklist.subtitle')}</p>
          </div>
        </div>
        
        <div className="flex items-center gap-3">
          <motion.button
            onClick={handleSyncAll}
            disabled={syncing}
            className="btn btn-primary"
            whileHover={{ scale: 1.02 }}
            whileTap={{ scale: 0.98 }}
          >
            {syncing ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <RefreshCw className="w-4 h-4" />
            )}
            {t('blocklist.sync_all')}
          </motion.button>
        </div>
      </motion.div>
      
      {/* Sync Warning */}
      <motion.div
        variants={itemVariants}
        className="flex items-start gap-3 p-4 bg-amber-500/10 border border-amber-500/30 rounded-lg"
      >
        <AlertCircle className="w-5 h-5 text-amber-400 shrink-0 mt-0.5" />
        <p className="text-sm text-amber-200">{t('blocklist.sync_warning')}</p>
      </motion.div>
      
      {/* Direction Toggle */}
      <motion.div variants={itemVariants} className="flex gap-2">
        {directionButtons.map((btn) => (
          <button
            key={btn.id}
            onClick={() => setDirection(btn.id)}
            className={`flex items-center gap-2 px-5 py-2.5 rounded-lg text-sm font-medium transition-all ${
              direction === btn.id
                ? btn.id === 'in'
                  ? 'bg-blue-500 text-white'
                  : 'bg-orange-500 text-white'
                : 'text-dark-400 hover:text-dark-200 hover:bg-dark-800 border border-dark-700'
            }`}
          >
            <btn.icon className="w-4 h-4" />
            {btn.label}
          </button>
        ))}
      </motion.div>
      
      {/* Tabs */}
      <motion.div variants={itemVariants} className="flex gap-2 border-b border-dark-700 pb-2">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-all ${
              activeTab === tab.id
                ? 'bg-accent-500 text-dark-950'
                : 'text-dark-400 hover:text-dark-200 hover:bg-dark-800'
            }`}
          >
            <tab.icon className="w-4 h-4" />
            {tab.label}
          </button>
        ))}
      </motion.div>
      
      {/* Tab Content */}
      <AnimatePresence mode="wait">
        {/* Global Rules Tab */}
        {activeTab === 'global' && (
          <motion.div
            key="global"
            initial={{ opacity: 0, x: -20 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: 20 }}
            className="space-y-4"
          >
            {/* Add Form */}
            <div className="card">
              <h3 className="text-lg font-semibold text-dark-100 mb-4">
                {t('blocklist.add_global')}
              </h3>
              <p className="text-sm text-dark-400 mb-4">
                {direction === 'in' ? t('blocklist.add_global_desc_in') : t('blocklist.add_global_desc_out')}
              </p>
              
              <div className="space-y-3">
                <textarea
                  value={newGlobalIps}
                  onChange={(e) => setNewGlobalIps(e.target.value)}
                  placeholder={t('blocklist.ip_placeholder')}
                  rows={4}
                  className="input w-full resize-none font-mono text-sm"
                />
                
                <motion.button
                  onClick={handleAddGlobalRules}
                  disabled={addingGlobal || !newGlobalIps.trim()}
                  className="btn btn-primary"
                  whileHover={{ scale: 1.02 }}
                  whileTap={{ scale: 0.98 }}
                >
                  {addingGlobal ? (
                    <Loader2 className="w-4 h-4 animate-spin" />
                  ) : (
                    <Plus className="w-4 h-4" />
                  )}
                  {t('blocklist.add')}
                </motion.button>
              </div>
            </div>
            
            {/* Rules List */}
            <div className="card">
              <div className="flex items-center justify-between mb-4">
                <h3 className="text-lg font-semibold text-dark-100">
                  {t('blocklist.current_rules')}
                </h3>
                <span className="text-sm text-dark-400">
                  {globalRules.length} {t('blocklist.rules')}
                </span>
              </div>
              
              {globalRules.length === 0 ? (
                <p className="text-dark-400 text-center py-8">{t('blocklist.no_rules')}</p>
              ) : (
                <div className="space-y-2 max-h-96 overflow-y-auto">
                  {globalRules.map((rule) => (
                    <div
                      key={rule.id}
                      className="flex items-center justify-between p-3 bg-dark-800/50 rounded-lg border border-dark-700/50"
                    >
                      <div className="flex items-center gap-3">
                        <code className="text-sm text-dark-200 font-mono">{rule.ip_cidr}</code>
                        {rule.comment && (
                          <span className="text-xs text-dark-500">{rule.comment}</span>
                        )}
                      </div>
                      <button
                        onClick={() => handleDeleteGlobalRule(rule.id)}
                        className="p-1.5 text-dark-400 hover:text-danger transition-colors"
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </motion.div>
        )}
        
        {/* Server Rules Tab */}
        {activeTab === 'servers' && (
          <motion.div
            key="servers"
            initial={{ opacity: 0, x: -20 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: 20 }}
            className="space-y-4"
          >
            {/* Server Selector */}
            <div className="card">
              <label className="block text-sm text-dark-400 mb-2">
                {t('blocklist.select_server')}
              </label>
              <select
                value={selectedServerId || ''}
                onChange={(e) => setSelectedServerId(parseInt(e.target.value))}
                className="input w-full max-w-xs"
              >
                {servers.map((server) => (
                  <option key={server.id} value={server.id}>
                    {server.name}
                  </option>
                ))}
              </select>
              
              {selectedServerId && (
                <p className="text-xs text-dark-500 mt-2">
                  {t('blocklist.server_rules_info', { 
                    local: serverRules.length, 
                    global: serverGlobalCount 
                  })}
                </p>
              )}
            </div>
            
            {/* Add Form */}
            {selectedServerId && (
              <div className="card">
                <h3 className="text-lg font-semibold text-dark-100 mb-4">
                  {t('blocklist.add_server_rule')}
                </h3>
                
                <div className="flex gap-3">
                  <input
                    type="text"
                    value={newServerIp}
                    onChange={(e) => setNewServerIp(e.target.value)}
                    placeholder="192.168.1.0/24"
                    className="input flex-1 font-mono"
                  />
                  <motion.button
                    onClick={handleAddServerRule}
                    disabled={addingServer || !newServerIp.trim()}
                    className="btn btn-primary"
                    whileHover={{ scale: 1.02 }}
                    whileTap={{ scale: 0.98 }}
                  >
                    {addingServer ? (
                      <Loader2 className="w-4 h-4 animate-spin" />
                    ) : (
                      <Plus className="w-4 h-4" />
                    )}
                    {t('blocklist.add')}
                  </motion.button>
                </div>
              </div>
            )}
            
            {/* Rules List */}
            {selectedServerId && (
              <div className="card">
                <h3 className="text-lg font-semibold text-dark-100 mb-4">
                  {t('blocklist.server_rules_only')}
                </h3>
                
                {serverRules.length === 0 ? (
                  <p className="text-dark-400 text-center py-8">{t('blocklist.no_server_rules')}</p>
                ) : (
                  <div className="space-y-2 max-h-96 overflow-y-auto">
                    {serverRules.map((rule) => (
                      <div
                        key={rule.id}
                        className="flex items-center justify-between p-3 bg-dark-800/50 rounded-lg border border-dark-700/50"
                      >
                        <code className="text-sm text-dark-200 font-mono">{rule.ip_cidr}</code>
                        <button
                          onClick={() => handleDeleteServerRule(rule.id)}
                          className="p-1.5 text-dark-400 hover:text-danger transition-colors"
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </motion.div>
        )}
        
        {/* Sources Tab */}
        {activeTab === 'sources' && (
          <motion.div
            key="sources"
            initial={{ opacity: 0, x: -20 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: 20 }}
            className="space-y-4"
          >
            {/* Refresh All Button */}
            <div className="flex justify-end">
              <motion.button
                onClick={handleRefreshAllSources}
                disabled={refreshingSource !== null}
                className="btn btn-secondary"
                whileHover={{ scale: 1.02 }}
                whileTap={{ scale: 0.98 }}
              >
                {refreshingSource === -1 ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <RefreshCw className="w-4 h-4" />
                )}
                {t('blocklist.refresh_all')}
              </motion.button>
            </div>
            
            {/* Sources List */}
            <div className="grid gap-4">
              {sources.map((source) => (
                <div
                  key={source.id}
                  className={`card ${!source.enabled ? 'opacity-60' : ''}`}
                >
                  <div className="flex items-start justify-between">
                    <div className="flex-1">
                      <div className="flex items-center gap-3 mb-2">
                        <h3 className="font-semibold text-dark-100">{source.name}</h3>
                        {source.is_default && (
                          <span className="px-2 py-0.5 text-xs bg-accent-500/20 text-accent-400 rounded">
                            {t('blocklist.default')}
                          </span>
                        )}
                        {source.error_message && (
                          <span className="flex items-center gap-1 text-xs text-danger">
                            <AlertCircle className="w-3 h-3" />
                            {t('common.error')}
                          </span>
                        )}
                      </div>
                      
                      <a
                        href={source.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="flex items-center gap-1 text-xs text-dark-400 hover:text-accent-400 transition-colors mb-3"
                      >
                        <ExternalLink className="w-3 h-3" />
                        <span className="truncate max-w-md">{source.url}</span>
                      </a>
                      
                      <div className="flex items-center gap-4 text-sm text-dark-400">
                        <span>{source.ip_count} IPs</span>
                        {source.last_updated && (
                          <span>
                            {t('blocklist.last_updated')}: {new Date(source.last_updated).toLocaleDateString()}
                          </span>
                        )}
                      </div>
                      
                      {source.error_message && (
                        <p className="text-xs text-danger mt-2">{source.error_message}</p>
                      )}
                    </div>
                    
                    <div className="flex items-center gap-2">
                      {/* Toggle */}
                      <button
                        onClick={() => handleToggleSource(source.id, !source.enabled)}
                        className={`p-2 rounded-lg transition-colors ${
                          source.enabled
                            ? 'bg-success/20 text-success'
                            : 'bg-dark-700 text-dark-400'
                        }`}
                      >
                        {source.enabled ? <Check className="w-4 h-4" /> : <X className="w-4 h-4" />}
                      </button>
                      
                      {/* Refresh */}
                      <button
                        onClick={() => handleRefreshSource(source.id)}
                        disabled={refreshingSource !== null}
                        className="p-2 text-dark-400 hover:text-accent-400 transition-colors"
                      >
                        {refreshingSource === source.id ? (
                          <Loader2 className="w-4 h-4 animate-spin" />
                        ) : (
                          <RefreshCw className="w-4 h-4" />
                        )}
                      </button>
                      
                      {/* Delete */}
                      {!source.is_default && (
                        <button
                          onClick={() => handleDeleteSource(source.id)}
                          className="p-2 text-dark-400 hover:text-danger transition-colors"
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              ))}
            </div>
            
            {/* Add Source Form */}
            <div className="card">
              <h3 className="text-lg font-semibold text-dark-100 mb-4">
                {t('blocklist.add_source')}
              </h3>
              
              <div className="space-y-3">
                <div>
                  <label className="block text-sm text-dark-400 mb-1">{t('common.name')}</label>
                  <input
                    type="text"
                    value={newSourceName}
                    onChange={(e) => setNewSourceName(e.target.value)}
                    placeholder="My Blocklist"
                    className="input w-full"
                  />
                </div>
                
                <div>
                  <label className="block text-sm text-dark-400 mb-1">URL</label>
                  <input
                    type="text"
                    value={newSourceUrl}
                    onChange={(e) => setNewSourceUrl(e.target.value)}
                    placeholder="https://example.com/blocklist.txt"
                    className="input w-full font-mono text-sm"
                  />
                </div>
                
                <motion.button
                  onClick={handleAddSource}
                  disabled={addingSource || !newSourceName.trim() || !newSourceUrl.trim()}
                  className="btn btn-primary"
                  whileHover={{ scale: 1.02 }}
                  whileTap={{ scale: 0.98 }}
                >
                  {addingSource ? (
                    <Loader2 className="w-4 h-4 animate-spin" />
                  ) : (
                    <Plus className="w-4 h-4" />
                  )}
                  {t('blocklist.add_source')}
                </motion.button>
              </div>
            </div>
          </motion.div>
        )}
        
        {/* Torrent Blocker Tab */}
        {activeTab === 'torrent' && (
          <motion.div
            key="torrent"
            initial={{ opacity: 0, x: -20 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: 20 }}
            className="space-y-4"
          >
            {/* Description */}
            <div className="card">
              <div className="flex items-start gap-3">
                <ShieldBan className="w-5 h-5 text-accent-400 shrink-0 mt-0.5" />
                <div>
                  <h3 className="text-lg font-semibold text-dark-100 mb-1">{t('blocklist.torrent_blocker')}</h3>
                  <p className="text-sm text-dark-400">{t('blocklist.torrent_desc')}</p>
                </div>
              </div>
            </div>
            
            {/* Xray Config Instructions */}
            <div className="card border border-amber-500/30 bg-amber-500/5">
              <div className="flex items-start gap-3 mb-4">
                <Info className="w-5 h-5 text-amber-400 shrink-0 mt-0.5" />
                <div>
                  <h3 className="font-semibold text-amber-200">{t('blocklist.torrent_config_title')}</h3>
                  <p className="text-sm text-dark-400 mt-1">{t('blocklist.torrent_config_desc')}</p>
                </div>
              </div>
              
              <div className="space-y-3">
                <div>
                  <p className="text-xs text-dark-400 mb-1.5">{t('blocklist.torrent_config_routing')}</p>
                  <pre className="bg-dark-900 rounded-lg p-3 text-xs font-mono text-dark-200 overflow-x-auto">{`{
  "port": "6881-6999",
  "type": "field",
  "outboundTag": "torrent"
},
{
  "type": "field",
  "protocol": ["bittorrent"],
  "outboundTag": "torrent"
}`}</pre>
                </div>
                
                <div>
                  <p className="text-xs text-dark-400 mb-1.5">{t('blocklist.torrent_config_outbound')}</p>
                  <pre className="bg-dark-900 rounded-lg p-3 text-xs font-mono text-dark-200 overflow-x-auto">{`{
  "tag": "torrent",
  "protocol": "blackhole"
}`}</pre>
                </div>
              </div>
            </div>
            
            {/* Temp Timeout Settings */}
            <div className="card">
              <h3 className="text-lg font-semibold text-dark-100 mb-4">{t('blocklist.settings')}</h3>
              
              <div className="flex items-end gap-4">
                <div className="flex-1 max-w-xs">
                  <label className="block text-sm text-dark-400 mb-2">
                    {t('blocklist.temp_timeout')}
                  </label>
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      value={tempTimeout}
                      onChange={(e) => setTempTimeout(parseInt(e.target.value) || 600)}
                      min={1}
                      max={2592000}
                      className="input w-32"
                    />
                    <span className="text-dark-400 text-sm">{t('common.seconds')}</span>
                  </div>
                  <p className="text-xs text-dark-500 mt-1">{t('blocklist.temp_timeout_desc')}</p>
                </div>
                
                <motion.button
                  onClick={handleSaveSettings}
                  disabled={savingSettings}
                  className="btn btn-primary"
                  whileHover={{ scale: 1.02 }}
                  whileTap={{ scale: 0.98 }}
                >
                  {savingSettings ? (
                    <Loader2 className="w-4 h-4 animate-spin" />
                  ) : (
                    <Check className="w-4 h-4" />
                  )}
                  {t('common.save')}
                </motion.button>
              </div>
            </div>
            
            {/* Servers List */}
            <div className="card">
              <div className="flex items-center justify-between mb-4">
                <h3 className="text-lg font-semibold text-dark-100">{t('blocklist.torrent_servers')}</h3>
                <motion.button
                  onClick={() => { setTorrentLoaded(false); fetchTorrentStatus().then(() => setTorrentLoaded(true)) }}
                  disabled={torrentLoading}
                  className="btn btn-secondary text-sm"
                  whileHover={{ scale: 1.02 }}
                  whileTap={{ scale: 0.98 }}
                >
                  {torrentLoading ? (
                    <Loader2 className="w-4 h-4 animate-spin" />
                  ) : (
                    <RefreshCw className="w-4 h-4" />
                  )}
                  {t('common.refresh')}
                </motion.button>
              </div>
              
              {torrentLoading && torrentStatuses.length === 0 ? (
                <div className="flex items-center justify-center py-8">
                  <Loader2 className="w-6 h-6 text-accent-500 animate-spin" />
                </div>
              ) : torrentStatuses.length === 0 ? (
                <p className="text-dark-400 text-center py-8">{t('bulk_actions.no_servers')}</p>
              ) : (
                <div className="space-y-3">
                  {torrentStatuses.map((srv) => (
                    <div
                      key={srv.server_id}
                      className={`p-4 rounded-lg border transition-all ${
                        srv.enabled
                          ? 'bg-dark-800/50 border-accent-500/30'
                          : 'bg-dark-800/30 border-dark-700/50'
                      }`}
                    >
                      <div className="flex items-center justify-between mb-3">
                        <div className="flex items-center gap-3">
                          <div className={`w-2.5 h-2.5 rounded-full ${
                            srv.error ? 'bg-danger' : srv.running ? 'bg-success animate-pulse' : 'bg-dark-500'
                          }`} />
                          <span className="font-medium text-dark-100">{srv.server_name}</span>
                          {srv.error ? (
                            <span className="text-xs text-danger">{t('blocklist.torrent_error')}</span>
                          ) : srv.running ? (
                            <span className="text-xs text-success">{t('blocklist.torrent_running')}</span>
                          ) : (
                            <span className="text-xs text-dark-500">{t('blocklist.torrent_not_running')}</span>
                          )}
                        </div>
                        
                        <motion.button
                          onClick={() => handleToggleTorrentBlocker(srv.server_id, !srv.enabled)}
                          disabled={togglingServer === srv.server_id}
                          className={`flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-medium transition-all ${
                            srv.enabled
                              ? 'bg-danger/20 text-danger hover:bg-danger/30'
                              : 'bg-success/20 text-success hover:bg-success/30'
                          }`}
                          whileHover={{ scale: 1.02 }}
                          whileTap={{ scale: 0.98 }}
                        >
                          {togglingServer === srv.server_id ? (
                            <Loader2 className="w-4 h-4 animate-spin" />
                          ) : srv.enabled ? (
                            <PowerOff className="w-4 h-4" />
                          ) : (
                            <Power className="w-4 h-4" />
                          )}
                          {srv.enabled ? t('blocklist.torrent_disable') : t('blocklist.torrent_enable')}
                        </motion.button>
                      </div>
                      
                      {srv.enabled && !srv.error && (
                        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-3">
                          <div className="bg-dark-900/50 rounded-lg p-2.5">
                            <p className="text-xs text-dark-500">{t('blocklist.torrent_blocked_count')}</p>
                            <p className="text-lg font-bold text-dark-100">{srv.total_blocked}</p>
                          </div>
                          <div className="bg-dark-900/50 rounded-lg p-2.5">
                            <p className="text-xs text-dark-500">{t('blocklist.torrent_unique_ips')}</p>
                            <p className="text-lg font-bold text-dark-100">{srv.unique_ips_blocked}</p>
                          </div>
                          <div className="bg-dark-900/50 rounded-lg p-2.5 col-span-2">
                            <p className="text-xs text-dark-500">{t('blocklist.torrent_last_block')}</p>
                            <p className="text-sm font-medium text-dark-200">
                              {srv.last_block_time
                                ? new Date(srv.last_block_time).toLocaleString()
                                : '—'}
                            </p>
                          </div>
                        </div>
                      )}
                      
                      {srv.enabled && !srv.error && srv.recent_blocks && srv.recent_blocks.length > 0 && (
                        <div className="mt-3">
                          <p className="text-xs text-dark-500 mb-2">{t('blocklist.torrent_recent_blocks')}</p>
                          <div className="flex flex-wrap gap-1.5">
                            {srv.recent_blocks.slice(-10).reverse().map((block, idx) => (
                              <span
                                key={idx}
                                className="px-2 py-0.5 text-xs font-mono bg-dark-900 text-dark-300 rounded border border-dark-700/50"
                                title={new Date(block.time).toLocaleString()}
                              >
                                {block.ip}
                              </span>
                            ))}
                          </div>
                        </div>
                      )}
                      
                      {srv.enabled && !srv.error && (!srv.recent_blocks || srv.recent_blocks.length === 0) && (
                        <p className="text-xs text-dark-500 mt-2">{t('blocklist.torrent_no_blocks')}</p>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}
