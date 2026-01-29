import { useState, useEffect, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Radio,
  RefreshCw,
  Settings,
  Users,
  Globe,
  BarChart3,
  Plus,
  Trash2,
  Check,
  X,
  AlertCircle,
  Search,
  ChevronRight,
  ExternalLink
} from 'lucide-react'
import { 
  remnawaveApi, 
  RemnawaveSettings, 
  RemnawaveNode, 
  RemnawaveSummary,
  RemnawaveDestination,
  RemnawaveUser,
  RemnawaveUserDetails,
  RemnawaveTimelinePoint
} from '../api/client'
import { useTranslation } from 'react-i18next'
import PeriodSelector from '../components/ui/PeriodSelector'
import MultiLineChart from '../components/Charts/MultiLineChart'

const containerVariants = {
  hidden: { opacity: 0 },
  visible: {
    opacity: 1,
    transition: { staggerChildren: 0.1 }
  }
}

const itemVariants = {
  hidden: { opacity: 0, y: 20 },
  visible: { opacity: 1, y: 0, transition: { duration: 0.4 } }
}

type TabType = 'overview' | 'users' | 'destinations' | 'settings'

export default function Remnawave() {
  const { t } = useTranslation()
  
  // State
  const [activeTab, setActiveTab] = useState<TabType>('overview')
  const [isLoading, setIsLoading] = useState(true)
  const [isRefreshing, setIsRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [period, setPeriod] = useState('24h')
  
  // Settings state
  const [settings, setSettings] = useState<RemnawaveSettings | null>(null)
  const [editSettings, setEditSettings] = useState<Partial<RemnawaveSettings>>({})
  const [isSavingSettings, setIsSavingSettings] = useState(false)
  const [isTestingConnection, setIsTestingConnection] = useState(false)
  const [testResult, setTestResult] = useState<{ success: boolean; error?: string } | null>(null)
  
  // Nodes state
  const [nodes, setNodes] = useState<RemnawaveNode[]>([])
  const [availableServers, setAvailableServers] = useState<Array<{ id: number; name: string }>>([])
  const [selectedServerToAdd, setSelectedServerToAdd] = useState<number | null>(null)
  
  // Stats state
  const [summary, setSummary] = useState<RemnawaveSummary | null>(null)
  const [topDestinations, setTopDestinations] = useState<RemnawaveDestination[]>([])
  const [topUsers, setTopUsers] = useState<RemnawaveUser[]>([])
  const [timeline, setTimeline] = useState<RemnawaveTimelinePoint[]>([])
  
  // User details modal
  const [selectedUser, setSelectedUser] = useState<RemnawaveUserDetails | null>(null)
  const [userSearch, setUserSearch] = useState('')
  const [destSearch, setDestSearch] = useState('')
  
  // Fetch settings
  const fetchSettings = useCallback(async () => {
    try {
      const [settingsRes, nodesRes] = await Promise.all([
        remnawaveApi.getSettings(),
        remnawaveApi.getNodes()
      ])
      setSettings(settingsRes.data)
      setEditSettings({
        api_url: settingsRes.data.api_url || '',
        api_token: '',
        cookie_secret: '',
        enabled: settingsRes.data.enabled,
        collection_interval: settingsRes.data.collection_interval
      })
      setNodes(nodesRes.data.nodes)
      setAvailableServers(nodesRes.data.available_servers)
    } catch (err) {
      console.error('Failed to fetch settings:', err)
    }
  }, [])
  
  // Fetch stats
  const fetchStats = useCallback(async () => {
    try {
      const [summaryRes, destRes, usersRes, timelineRes] = await Promise.all([
        remnawaveApi.getSummary(period),
        remnawaveApi.getTopDestinations({ period, limit: 20 }),
        remnawaveApi.getTopUsers({ period, limit: 20 }),
        remnawaveApi.getTimeline({ period })
      ])
      setSummary(summaryRes.data)
      setTopDestinations(destRes.data.destinations)
      setTopUsers(usersRes.data.users)
      setTimeline(timelineRes.data.data)
      setError(null)
    } catch (err) {
      console.error('Failed to fetch stats:', err)
      setError(t('remnawave.failed_fetch'))
    }
  }, [period, t])
  
  // Initial load
  useEffect(() => {
    const loadData = async () => {
      setIsLoading(true)
      await Promise.all([fetchSettings(), fetchStats()])
      setIsLoading(false)
    }
    loadData()
  }, [fetchSettings, fetchStats])
  
  // Reload stats when period changes
  useEffect(() => {
    if (!isLoading) {
      fetchStats()
    }
  }, [period, fetchStats, isLoading])
  
  const handleRefresh = async () => {
    setIsRefreshing(true)
    await fetchStats()
    setIsRefreshing(false)
  }
  
  const handleSaveSettings = async () => {
    setIsSavingSettings(true)
    try {
      const dataToSave: Partial<RemnawaveSettings> = {
        enabled: editSettings.enabled,
        collection_interval: editSettings.collection_interval
      }
      if (editSettings.api_url) dataToSave.api_url = editSettings.api_url
      if (editSettings.api_token) dataToSave.api_token = editSettings.api_token
      if (editSettings.cookie_secret) dataToSave.cookie_secret = editSettings.cookie_secret
      
      await remnawaveApi.updateSettings(dataToSave)
      await fetchSettings()
      setTestResult(null)
    } catch (err) {
      console.error('Failed to save settings:', err)
    } finally {
      setIsSavingSettings(false)
    }
  }
  
  const handleTestConnection = async () => {
    setIsTestingConnection(true)
    setTestResult(null)
    try {
      const res = await remnawaveApi.testConnection()
      setTestResult({ success: res.data.success, error: res.data.error || undefined })
    } catch (err) {
      setTestResult({ success: false, error: 'Connection failed' })
    } finally {
      setIsTestingConnection(false)
    }
  }
  
  const handleAddNode = async () => {
    if (!selectedServerToAdd) return
    try {
      await remnawaveApi.addNode(selectedServerToAdd)
      await fetchSettings()
      setSelectedServerToAdd(null)
    } catch (err) {
      console.error('Failed to add node:', err)
    }
  }
  
  const handleRemoveNode = async (serverId: number) => {
    try {
      await remnawaveApi.removeNode(serverId)
      await fetchSettings()
    } catch (err) {
      console.error('Failed to remove node:', err)
    }
  }
  
  const handleToggleNode = async (serverId: number, enabled: boolean) => {
    try {
      await remnawaveApi.updateNode(serverId, enabled)
      await fetchSettings()
    } catch (err) {
      console.error('Failed to toggle node:', err)
    }
  }
  
  const handleUserClick = async (email: number) => {
    try {
      const res = await remnawaveApi.getUserStats(email, period)
      setSelectedUser(res.data)
    } catch (err) {
      console.error('Failed to fetch user stats:', err)
    }
  }
  
  // Filter destinations and users
  const filteredDestinations = topDestinations.filter(d => 
    destSearch ? (d.destination.toLowerCase().includes(destSearch.toLowerCase()) ||
                  d.domain?.toLowerCase().includes(destSearch.toLowerCase())) : true
  )
  
  const filteredUsers = topUsers.filter(u =>
    userSearch ? (u.username?.toLowerCase().includes(userSearch.toLowerCase()) ||
                  u.email.toString().includes(userSearch)) : true
  )
  
  // Timeline chart data
  const timelineSeries = [{
    name: t('remnawave.visits'),
    data: timeline.map(t => ({ timestamp: t.timestamp, value: t.visits })),
    color: '#8b5cf6'
  }]
  
  // Tabs
  const tabs: { id: TabType; label: string; icon: React.ReactNode }[] = [
    { id: 'overview', label: t('remnawave.overview'), icon: <BarChart3 className="w-4 h-4" /> },
    { id: 'users', label: t('remnawave.users'), icon: <Users className="w-4 h-4" /> },
    { id: 'destinations', label: t('remnawave.destinations'), icon: <Globe className="w-4 h-4" /> },
    { id: 'settings', label: t('remnawave.settings'), icon: <Settings className="w-4 h-4" /> },
  ]
  
  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <div className="flex flex-col items-center gap-4">
          <div className="w-8 h-8 border-2 border-accent-500 border-t-transparent rounded-full animate-spin" />
          <span className="text-dark-400">{t('common.loading')}</span>
        </div>
      </div>
    )
  }
  
  return (
    <motion.div
      variants={containerVariants}
      initial="hidden"
      animate="visible"
      className="space-y-6"
    >
      {/* Header */}
      <motion.div variants={itemVariants} className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="w-12 h-12 rounded-xl bg-purple-500/10 flex items-center justify-center">
            <Radio className="w-6 h-6 text-purple-400" />
          </div>
          <div>
            <h1 className="text-2xl font-bold text-dark-100">{t('remnawave.title')}</h1>
            <p className="text-dark-400">{t('remnawave.subtitle')}</p>
          </div>
        </div>
        
        {activeTab !== 'settings' && (
          <div className="flex items-center gap-3">
            <PeriodSelector value={period} onChange={setPeriod} />
            <motion.button
              onClick={handleRefresh}
              disabled={isRefreshing}
              className="p-2 rounded-lg bg-dark-800 hover:bg-dark-700 text-dark-300 
                       hover:text-dark-100 transition-colors disabled:opacity-50"
              whileHover={{ scale: 1.05 }}
              whileTap={{ scale: 0.95 }}
            >
              <RefreshCw className={`w-5 h-5 ${isRefreshing ? 'animate-spin' : ''}`} />
            </motion.button>
          </div>
        )}
      </motion.div>
      
      {/* Tabs */}
      <motion.div variants={itemVariants} className="flex gap-2 p-1 bg-dark-800/50 rounded-xl w-fit">
        {tabs.map(tab => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-all ${
              activeTab === tab.id
                ? 'bg-accent-500/20 text-accent-400'
                : 'text-dark-400 hover:text-dark-200 hover:bg-dark-700/50'
            }`}
          >
            {tab.icon}
            {tab.label}
          </button>
        ))}
      </motion.div>
      
      {error && (
        <motion.div variants={itemVariants} className="p-4 rounded-xl bg-danger/10 border border-danger/20">
          <div className="flex items-center gap-3 text-danger">
            <AlertCircle className="w-5 h-5" />
            <span>{error}</span>
          </div>
        </motion.div>
      )}
      
      {/* Tab Content */}
      <AnimatePresence mode="wait">
        {activeTab === 'overview' && (
          <motion.div
            key="overview"
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -20 }}
            className="space-y-6"
          >
            {/* Summary Cards */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <div className="p-6 rounded-xl bg-dark-800/50 border border-dark-700/50">
                <div className="text-dark-400 text-sm mb-2">{t('remnawave.total_visits')}</div>
                <div className="text-3xl font-bold text-dark-100">
                  {summary?.total_visits?.toLocaleString() || 0}
                </div>
              </div>
              <div className="p-6 rounded-xl bg-dark-800/50 border border-dark-700/50">
                <div className="text-dark-400 text-sm mb-2">{t('remnawave.unique_users')}</div>
                <div className="text-3xl font-bold text-dark-100">
                  {summary?.unique_users?.toLocaleString() || 0}
                </div>
              </div>
              <div className="p-6 rounded-xl bg-dark-800/50 border border-dark-700/50">
                <div className="text-dark-400 text-sm mb-2">{t('remnawave.unique_sites')}</div>
                <div className="text-3xl font-bold text-dark-100">
                  {summary?.unique_destinations?.toLocaleString() || 0}
                </div>
              </div>
            </div>
            
            {/* Timeline Chart */}
            {timeline.length > 0 && (
              <div className="p-6 rounded-xl bg-dark-800/50 border border-dark-700/50">
                <h3 className="text-lg font-semibold text-dark-100 mb-4">{t('remnawave.visits_timeline')}</h3>
                <MultiLineChart
                  series={timelineSeries}
                  height={250}
                  showLegend={false}
                />
              </div>
            )}
            
            {/* Top Lists */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
              {/* Top Sites */}
              <div className="p-6 rounded-xl bg-dark-800/50 border border-dark-700/50">
                <h3 className="text-lg font-semibold text-dark-100 mb-4">{t('remnawave.top_sites')}</h3>
                <div className="space-y-3">
                  {topDestinations.slice(0, 10).map((dest, idx) => (
                    <div key={dest.destination} className="flex items-center gap-3">
                      <span className="text-dark-500 text-sm w-6">{idx + 1}</span>
                      <div className="flex-1 min-w-0">
                        <div className="text-dark-200 text-sm truncate">
                          {dest.domain || dest.destination}
                        </div>
                        {dest.domain && (
                          <div className="text-dark-500 text-xs truncate">{dest.destination}</div>
                        )}
                      </div>
                      <span className="text-dark-400 text-sm">{dest.visits.toLocaleString()}</span>
                    </div>
                  ))}
                  {topDestinations.length === 0 && (
                    <div className="text-dark-500 text-sm text-center py-4">{t('remnawave.no_data')}</div>
                  )}
                </div>
              </div>
              
              {/* Top Users */}
              <div className="p-6 rounded-xl bg-dark-800/50 border border-dark-700/50">
                <h3 className="text-lg font-semibold text-dark-100 mb-4">{t('remnawave.top_users')}</h3>
                <div className="space-y-3">
                  {topUsers.slice(0, 10).map((user, idx) => (
                    <div
                      key={user.email}
                      className="flex items-center gap-3 cursor-pointer hover:bg-dark-700/50 p-2 -mx-2 rounded-lg transition-colors"
                      onClick={() => handleUserClick(user.email)}
                    >
                      <span className="text-dark-500 text-sm w-6">{idx + 1}</span>
                      <div className="flex-1 min-w-0">
                        <div className="text-dark-200 text-sm truncate">
                          {user.username || `User #${user.email}`}
                        </div>
                        <div className="text-dark-500 text-xs">
                          {user.unique_sites} {t('remnawave.sites')}
                        </div>
                      </div>
                      <span className="text-dark-400 text-sm">{user.total_visits.toLocaleString()}</span>
                      <ChevronRight className="w-4 h-4 text-dark-500" />
                    </div>
                  ))}
                  {topUsers.length === 0 && (
                    <div className="text-dark-500 text-sm text-center py-4">{t('remnawave.no_data')}</div>
                  )}
                </div>
              </div>
            </div>
          </motion.div>
        )}
        
        {activeTab === 'users' && (
          <motion.div
            key="users"
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -20 }}
            className="space-y-4"
          >
            {/* Search */}
            <div className="relative">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-dark-500" />
              <input
                type="text"
                value={userSearch}
                onChange={(e) => setUserSearch(e.target.value)}
                placeholder={t('remnawave.search_users')}
                className="w-full pl-10 pr-4 py-2 rounded-lg bg-dark-800 border border-dark-700 
                         text-dark-100 placeholder-dark-500 focus:outline-none focus:border-accent-500"
              />
            </div>
            
            {/* Users Table */}
            <div className="rounded-xl bg-dark-800/50 border border-dark-700/50 overflow-hidden">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-dark-700">
                    <th className="text-left p-4 text-dark-400 font-medium text-sm">ID</th>
                    <th className="text-left p-4 text-dark-400 font-medium text-sm">{t('remnawave.username')}</th>
                    <th className="text-left p-4 text-dark-400 font-medium text-sm">{t('remnawave.status')}</th>
                    <th className="text-right p-4 text-dark-400 font-medium text-sm">{t('remnawave.visits')}</th>
                    <th className="text-right p-4 text-dark-400 font-medium text-sm">{t('remnawave.sites')}</th>
                    <th className="w-10"></th>
                  </tr>
                </thead>
                <tbody>
                  {filteredUsers.map(user => (
                    <tr
                      key={user.email}
                      className="border-b border-dark-700/50 hover:bg-dark-700/30 cursor-pointer transition-colors"
                      onClick={() => handleUserClick(user.email)}
                    >
                      <td className="p-4 text-dark-300">{user.email}</td>
                      <td className="p-4 text-dark-200">{user.username || '-'}</td>
                      <td className="p-4">
                        <span className={`px-2 py-1 rounded text-xs font-medium ${
                          user.status === 'ACTIVE' ? 'bg-success/20 text-success' :
                          user.status === 'DISABLED' ? 'bg-danger/20 text-danger' :
                          user.status === 'EXPIRED' ? 'bg-warning/20 text-warning' :
                          'bg-dark-600 text-dark-300'
                        }`}>
                          {user.status || 'Unknown'}
                        </span>
                      </td>
                      <td className="p-4 text-right text-dark-200">{user.total_visits.toLocaleString()}</td>
                      <td className="p-4 text-right text-dark-400">{user.unique_sites}</td>
                      <td className="p-4">
                        <ChevronRight className="w-4 h-4 text-dark-500" />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {filteredUsers.length === 0 && (
                <div className="p-8 text-center text-dark-500">{t('remnawave.no_data')}</div>
              )}
            </div>
          </motion.div>
        )}
        
        {activeTab === 'destinations' && (
          <motion.div
            key="destinations"
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -20 }}
            className="space-y-4"
          >
            {/* Search */}
            <div className="relative">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-dark-500" />
              <input
                type="text"
                value={destSearch}
                onChange={(e) => setDestSearch(e.target.value)}
                placeholder={t('remnawave.search_destinations')}
                className="w-full pl-10 pr-4 py-2 rounded-lg bg-dark-800 border border-dark-700 
                         text-dark-100 placeholder-dark-500 focus:outline-none focus:border-accent-500"
              />
            </div>
            
            {/* Destinations Table */}
            <div className="rounded-xl bg-dark-800/50 border border-dark-700/50 overflow-hidden">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-dark-700">
                    <th className="text-left p-4 text-dark-400 font-medium text-sm">#</th>
                    <th className="text-left p-4 text-dark-400 font-medium text-sm">{t('remnawave.destination')}</th>
                    <th className="text-left p-4 text-dark-400 font-medium text-sm">{t('remnawave.domain')}</th>
                    <th className="text-right p-4 text-dark-400 font-medium text-sm">{t('remnawave.visits')}</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredDestinations.map((dest, idx) => (
                    <tr key={dest.destination} className="border-b border-dark-700/50 hover:bg-dark-700/30 transition-colors">
                      <td className="p-4 text-dark-500">{idx + 1}</td>
                      <td className="p-4 text-dark-200 font-mono text-sm">{dest.destination}</td>
                      <td className="p-4 text-dark-400">{dest.domain || '-'}</td>
                      <td className="p-4 text-right text-dark-200">{dest.visits.toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {filteredDestinations.length === 0 && (
                <div className="p-8 text-center text-dark-500">{t('remnawave.no_data')}</div>
              )}
            </div>
          </motion.div>
        )}
        
        {activeTab === 'settings' && (
          <motion.div
            key="settings"
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -20 }}
            className="space-y-6"
          >
            {/* API Settings */}
            <div className="p-6 rounded-xl bg-dark-800/50 border border-dark-700/50">
              <h3 className="text-lg font-semibold text-dark-100 mb-4">{t('remnawave.api_settings')}</h3>
              <div className="space-y-4">
                <div>
                  <label className="block text-sm text-dark-400 mb-2">{t('remnawave.api_url')}</label>
                  <input
                    type="text"
                    value={editSettings.api_url || ''}
                    onChange={(e) => setEditSettings(s => ({ ...s, api_url: e.target.value }))}
                    placeholder="https://panel.example.com"
                    className="w-full px-4 py-2 rounded-lg bg-dark-900 border border-dark-700 
                             text-dark-100 placeholder-dark-500 focus:outline-none focus:border-accent-500"
                  />
                </div>
                <div>
                  <label className="block text-sm text-dark-400 mb-2">{t('remnawave.api_token')}</label>
                  <input
                    type="password"
                    value={editSettings.api_token || ''}
                    onChange={(e) => setEditSettings(s => ({ ...s, api_token: e.target.value }))}
                    placeholder={settings?.api_token ? '••••••••' : t('remnawave.enter_token')}
                    className="w-full px-4 py-2 rounded-lg bg-dark-900 border border-dark-700 
                             text-dark-100 placeholder-dark-500 focus:outline-none focus:border-accent-500"
                  />
                </div>
                <div>
                  <label className="block text-sm text-dark-400 mb-2">
                    {t('remnawave.cookie_secret')}
                    <span className="text-dark-500 ml-2">({t('common.optional')})</span>
                  </label>
                  <input
                    type="password"
                    value={editSettings.cookie_secret || ''}
                    onChange={(e) => setEditSettings(s => ({ ...s, cookie_secret: e.target.value }))}
                    placeholder={settings?.cookie_secret ? '••••••••' : 'name:value'}
                    className="w-full px-4 py-2 rounded-lg bg-dark-900 border border-dark-700 
                             text-dark-100 placeholder-dark-500 focus:outline-none focus:border-accent-500"
                  />
                  <p className="text-xs text-dark-500 mt-1">{t('remnawave.cookie_secret_hint')}</p>
                </div>
                <div>
                  <label className="block text-sm text-dark-400 mb-2">{t('remnawave.collection_interval')}</label>
                  <input
                    type="number"
                    value={editSettings.collection_interval || 60}
                    onChange={(e) => setEditSettings(s => ({ ...s, collection_interval: parseInt(e.target.value) || 60 }))}
                    min={10}
                    max={3600}
                    className="w-32 px-4 py-2 rounded-lg bg-dark-900 border border-dark-700 
                             text-dark-100 focus:outline-none focus:border-accent-500"
                  />
                  <span className="text-dark-500 ml-2">{t('common.seconds')}</span>
                </div>
                <div className="flex items-center gap-3">
                  <label className="relative inline-flex items-center cursor-pointer">
                    <input
                      type="checkbox"
                      checked={editSettings.enabled || false}
                      onChange={(e) => setEditSettings(s => ({ ...s, enabled: e.target.checked }))}
                      className="sr-only peer"
                    />
                    <div className="w-11 h-6 bg-dark-700 peer-focus:outline-none rounded-full peer 
                                  peer-checked:after:translate-x-full peer-checked:after:border-white 
                                  after:content-[''] after:absolute after:top-[2px] after:left-[2px] 
                                  after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all 
                                  peer-checked:bg-accent-500"></div>
                  </label>
                  <span className="text-dark-200">{t('remnawave.enabled')}</span>
                </div>
                
                <div className="flex items-center gap-3 pt-4 border-t border-dark-700">
                  <motion.button
                    onClick={handleTestConnection}
                    disabled={isTestingConnection}
                    className="px-4 py-2 rounded-lg bg-dark-700 hover:bg-dark-600 text-dark-200 
                             transition-colors disabled:opacity-50 flex items-center gap-2"
                    whileHover={{ scale: 1.02 }}
                    whileTap={{ scale: 0.98 }}
                  >
                    {isTestingConnection ? (
                      <RefreshCw className="w-4 h-4 animate-spin" />
                    ) : (
                      <ExternalLink className="w-4 h-4" />
                    )}
                    {t('remnawave.test_connection')}
                  </motion.button>
                  
                  <motion.button
                    onClick={handleSaveSettings}
                    disabled={isSavingSettings}
                    className="px-4 py-2 rounded-lg bg-accent-500 hover:bg-accent-600 text-white 
                             transition-colors disabled:opacity-50 flex items-center gap-2"
                    whileHover={{ scale: 1.02 }}
                    whileTap={{ scale: 0.98 }}
                  >
                    {isSavingSettings ? (
                      <RefreshCw className="w-4 h-4 animate-spin" />
                    ) : (
                      <Check className="w-4 h-4" />
                    )}
                    {t('common.save')}
                  </motion.button>
                  
                  {testResult && (
                    <span className={`text-sm ${testResult.success ? 'text-success' : 'text-danger'}`}>
                      {testResult.success ? t('remnawave.connection_success') : testResult.error}
                    </span>
                  )}
                </div>
              </div>
            </div>
            
            {/* Nodes */}
            <div className="p-6 rounded-xl bg-dark-800/50 border border-dark-700/50">
              <h3 className="text-lg font-semibold text-dark-100 mb-4">{t('remnawave.select_nodes')}</h3>
              
              {/* Add Node */}
              {availableServers.length > 0 && (
                <div className="flex items-center gap-3 mb-4 pb-4 border-b border-dark-700">
                  <select
                    value={selectedServerToAdd || ''}
                    onChange={(e) => setSelectedServerToAdd(e.target.value ? parseInt(e.target.value) : null)}
                    className="flex-1 px-4 py-2 rounded-lg bg-dark-900 border border-dark-700 
                             text-dark-100 focus:outline-none focus:border-accent-500"
                  >
                    <option value="">{t('remnawave.select_server')}</option>
                    {availableServers.map(s => (
                      <option key={s.id} value={s.id}>{s.name}</option>
                    ))}
                  </select>
                  <motion.button
                    onClick={handleAddNode}
                    disabled={!selectedServerToAdd}
                    className="px-4 py-2 rounded-lg bg-accent-500 hover:bg-accent-600 text-white 
                             transition-colors disabled:opacity-50 flex items-center gap-2"
                    whileHover={{ scale: 1.02 }}
                    whileTap={{ scale: 0.98 }}
                  >
                    <Plus className="w-4 h-4" />
                    {t('common.add')}
                  </motion.button>
                </div>
              )}
              
              {/* Node List */}
              <div className="space-y-3">
                {nodes.map(node => (
                  <div
                    key={node.id}
                    className="flex items-center gap-4 p-3 rounded-lg bg-dark-900/50 border border-dark-700/50"
                  >
                    <label className="relative inline-flex items-center cursor-pointer">
                      <input
                        type="checkbox"
                        checked={node.enabled}
                        onChange={(e) => handleToggleNode(node.server_id, e.target.checked)}
                        className="sr-only peer"
                      />
                      <div className="w-9 h-5 bg-dark-700 peer-focus:outline-none rounded-full peer 
                                    peer-checked:after:translate-x-full peer-checked:after:border-white 
                                    after:content-[''] after:absolute after:top-[2px] after:left-[2px] 
                                    after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all 
                                    peer-checked:bg-accent-500"></div>
                    </label>
                    <div className="flex-1">
                      <div className="text-dark-200">{node.server_name}</div>
                      {node.last_collected && (
                        <div className="text-xs text-dark-500">
                          {t('remnawave.last_collected')}: {new Date(node.last_collected).toLocaleString()}
                        </div>
                      )}
                      {node.last_error && (
                        <div className="text-xs text-danger">{node.last_error}</div>
                      )}
                    </div>
                    <motion.button
                      onClick={() => handleRemoveNode(node.server_id)}
                      className="p-2 rounded-lg hover:bg-danger/20 text-dark-400 hover:text-danger transition-colors"
                      whileHover={{ scale: 1.1 }}
                      whileTap={{ scale: 0.9 }}
                    >
                      <Trash2 className="w-4 h-4" />
                    </motion.button>
                  </div>
                ))}
                {nodes.length === 0 && (
                  <div className="text-center text-dark-500 py-4">{t('remnawave.no_nodes')}</div>
                )}
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
      
      {/* User Details Modal */}
      <AnimatePresence>
        {selectedUser && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm"
            onClick={() => setSelectedUser(null)}
          >
            <motion.div
              initial={{ scale: 0.95, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              exit={{ scale: 0.95, opacity: 0 }}
              className="w-full max-w-2xl max-h-[80vh] overflow-auto bg-dark-900 rounded-xl border border-dark-700 p-6"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="flex items-center justify-between mb-6">
                <div>
                  <h3 className="text-xl font-semibold text-dark-100">
                    {selectedUser.username || `User #${selectedUser.email}`}
                  </h3>
                  <div className="flex items-center gap-3 mt-1">
                    <span className="text-dark-400 text-sm">ID: {selectedUser.email}</span>
                    {selectedUser.status && (
                      <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                        selectedUser.status === 'ACTIVE' ? 'bg-success/20 text-success' :
                        selectedUser.status === 'DISABLED' ? 'bg-danger/20 text-danger' :
                        'bg-dark-600 text-dark-300'
                      }`}>
                        {selectedUser.status}
                      </span>
                    )}
                  </div>
                </div>
                <motion.button
                  onClick={() => setSelectedUser(null)}
                  className="p-2 rounded-lg hover:bg-dark-700 text-dark-400 transition-colors"
                  whileHover={{ scale: 1.1 }}
                  whileTap={{ scale: 0.9 }}
                >
                  <X className="w-5 h-5" />
                </motion.button>
              </div>
              
              <div className="mb-4 p-4 rounded-lg bg-dark-800">
                <span className="text-dark-400">{t('remnawave.total_visits')}:</span>
                <span className="text-dark-100 text-xl font-bold ml-2">
                  {selectedUser.total_visits.toLocaleString()}
                </span>
              </div>
              
              <h4 className="text-sm font-medium text-dark-400 mb-3">{t('remnawave.visited_sites')}</h4>
              <div className="space-y-2 max-h-[300px] overflow-auto">
                {selectedUser.destinations.map((dest, idx) => (
                  <div key={dest.destination} className="flex items-center gap-3 p-2 rounded-lg hover:bg-dark-800 transition-colors">
                    <span className="text-dark-500 text-sm w-6">{idx + 1}</span>
                    <div className="flex-1 min-w-0">
                      <div className="text-dark-200 text-sm truncate font-mono">
                        {dest.destination}
                      </div>
                      {dest.domain && (
                        <div className="text-dark-500 text-xs">{dest.domain}</div>
                      )}
                    </div>
                    <span className="text-dark-400 text-sm">{dest.visits.toLocaleString()}</span>
                  </div>
                ))}
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}
