import { useState, useEffect, useCallback, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Radio,
  RefreshCw,
  Settings,
  Users,
  Globe,
  BarChart3,
  Check,
  X,
  AlertCircle,
  Search,
  ChevronRight,
  ExternalLink,
  Play,
  Clock,
  Server,
  Info,
  Network
} from 'lucide-react'
import { 
  remnawaveApi, 
  RemnawaveSettings, 
  RemnawaveNode, 
  RemnawaveSummary,
  RemnawaveDestination,
  RemnawaveUser,
  RemnawaveUserDetails,
  RemnawaveServerInfo,
  RemnawaveCollectorStatus,
  RemnawaveDestinationUsers
} from '../api/client'
import { useTranslation } from 'react-i18next'
import PeriodSelector from '../components/ui/PeriodSelector'

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
  const [period, setPeriod] = useState('all')
  
  // Settings state
  const [settings, setSettings] = useState<RemnawaveSettings | null>(null)
  const [editSettings, setEditSettings] = useState<Partial<RemnawaveSettings>>({})
  const [isSavingSettings, setIsSavingSettings] = useState(false)
  const [isTestingConnection, setIsTestingConnection] = useState(false)
  const [testResult, setTestResult] = useState<{ success: boolean; error?: string } | null>(null)
  
  // Nodes state
  const [nodes, setNodes] = useState<RemnawaveNode[]>([])
  const [allServers, setAllServers] = useState<RemnawaveServerInfo[]>([])
  const [selectedNodeIds, setSelectedNodeIds] = useState<Set<number>>(new Set())
  const [isSyncingNodes, setIsSyncingNodes] = useState(false)
  
  // Collector status state
  const [collectorStatus, setCollectorStatus] = useState<RemnawaveCollectorStatus | null>(null)
  const [isCollecting, setIsCollecting] = useState(false)
  const [nextCollectIn, setNextCollectIn] = useState<number | null>(null)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  
  // Stats state
  const [summary, setSummary] = useState<RemnawaveSummary | null>(null)
  const [topDestinations, setTopDestinations] = useState<RemnawaveDestination[]>([])
  const [topUsers, setTopUsers] = useState<RemnawaveUser[]>([])
  
  // User details modal
  const [selectedUser, setSelectedUser] = useState<RemnawaveUserDetails | null>(null)
  const [userSearch, setUserSearch] = useState('')
  const [destSearch, setDestSearch] = useState('')
  
  // Destination users modal
  const [selectedDestination, setSelectedDestination] = useState<RemnawaveDestinationUsers | null>(null)
  const [isLoadingDestUsers, setIsLoadingDestUsers] = useState(false)
  const [destUserSearch, setDestUserSearch] = useState('')
  
  // Auto-refresh
  const autoRefreshRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const [nextRefreshIn, setNextRefreshIn] = useState(30)
  
  // Fetch settings
  const fetchSettings = useCallback(async () => {
    try {
      const [settingsRes, nodesRes, statusRes] = await Promise.all([
        remnawaveApi.getSettings(),
        remnawaveApi.getNodes(),
        remnawaveApi.getCollectorStatus()
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
      setAllServers(nodesRes.data.all_servers)
      // Initialize selected nodes from current nodes
      const nodeIds = new Set(nodesRes.data.all_servers.filter(s => s.is_node).map(s => s.id))
      setSelectedNodeIds(nodeIds)
      // Update collector status
      setCollectorStatus(statusRes.data)
      setNextCollectIn(statusRes.data.next_collect_in)
    } catch (err) {
      console.error('Failed to fetch settings:', err)
    }
  }, [])
  
  // Fetch stats
  const fetchStats = useCallback(async () => {
    try {
      const [summaryRes, destRes, usersRes] = await Promise.all([
        remnawaveApi.getSummary(period),
        remnawaveApi.getTopDestinations({ period, limit: 20 }),
        remnawaveApi.getTopUsers({ period, limit: 20 })
      ])
      setSummary(summaryRes.data)
      setTopDestinations(destRes.data.destinations)
      setTopUsers(usersRes.data.users)
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
  
  // Timer countdown effect
  useEffect(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current)
    }
    
    if (nextCollectIn !== null && nextCollectIn > 0 && !isCollecting) {
      timerRef.current = setInterval(() => {
        setNextCollectIn(prev => {
          if (prev === null || prev <= 1) {
            // Refresh status when timer reaches 0
            remnawaveApi.getCollectorStatus().then(res => {
              setCollectorStatus(res.data)
              setNextCollectIn(res.data.next_collect_in)
            })
            return null
          }
          return prev - 1
        })
      }, 1000)
    }
    
    return () => {
      if (timerRef.current) {
        clearInterval(timerRef.current)
      }
    }
  }, [nextCollectIn, isCollecting])
  
  // Auto-refresh effect (30 seconds)
  useEffect(() => {
    if (autoRefreshRef.current) {
      clearInterval(autoRefreshRef.current)
    }
    
    // Reset countdown
    setNextRefreshIn(30)
    
    autoRefreshRef.current = setInterval(() => {
      setNextRefreshIn(prev => {
        if (prev <= 1) {
          // Perform refresh
          fetchStats()
          return 30
        }
        return prev - 1
      })
    }, 1000)
    
    return () => {
      if (autoRefreshRef.current) {
        clearInterval(autoRefreshRef.current)
      }
    }
  }, [fetchStats])
  
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
  
  const handleToggleNodeSelection = (serverId: number) => {
    setSelectedNodeIds(prev => {
      const newSet = new Set(prev)
      if (newSet.has(serverId)) {
        newSet.delete(serverId)
      } else {
        newSet.add(serverId)
      }
      return newSet
    })
  }
  
  const handleSelectAllNodes = () => {
    setSelectedNodeIds(new Set(allServers.map(s => s.id)))
  }
  
  const handleDeselectAllNodes = () => {
    setSelectedNodeIds(new Set())
  }
  
  const handleSyncNodes = async () => {
    setIsSyncingNodes(true)
    try {
      await remnawaveApi.syncNodes(Array.from(selectedNodeIds))
      await fetchSettings()
    } catch (err) {
      console.error('Failed to sync nodes:', err)
    } finally {
      setIsSyncingNodes(false)
    }
  }
  
  const handleForceCollect = async () => {
    setIsCollecting(true)
    try {
      const res = await remnawaveApi.collectNow()
      if (res.data.success) {
        // Refresh stats and status after collection
        await Promise.all([fetchStats(), fetchSettings()])
      }
    } catch (err) {
      console.error('Failed to force collect:', err)
    } finally {
      setIsCollecting(false)
    }
  }
  
  // Check if selected nodes differ from current nodes
  const currentNodeIds = new Set(allServers.filter(s => s.is_node).map(s => s.id))
  const hasNodeChanges = selectedNodeIds.size !== currentNodeIds.size || 
    ![...selectedNodeIds].every(id => currentNodeIds.has(id))
  
  const handleUserClick = async (email: number) => {
    try {
      const res = await remnawaveApi.getUserStats(email, period)
      setSelectedUser(res.data)
    } catch (err) {
      console.error('Failed to fetch user stats:', err)
    }
  }
  
  const handleDestinationClick = async (destination: string) => {
    setIsLoadingDestUsers(true)
    setDestUserSearch('')
    try {
      const res = await remnawaveApi.getDestinationUsers(destination, period, 100)
      setSelectedDestination(res.data)
    } catch (err) {
      console.error('Failed to fetch destination users:', err)
    } finally {
      setIsLoadingDestUsers(false)
    }
  }
  
  // Фильтрация пользователей в модальном окне destination
  const filteredDestUsers = selectedDestination?.users.filter(u =>
    destUserSearch 
      ? (u.username?.toLowerCase().includes(destUserSearch.toLowerCase()) ||
         u.email.toString().includes(destUserSearch))
      : true
  ) || []
  
  // Filter destinations and users
  const filteredDestinations = topDestinations.filter(d => 
    destSearch ? d.destination.toLowerCase().includes(destSearch.toLowerCase()) : true
  )
  
  const filteredUsers = topUsers.filter(u =>
    userSearch ? (u.username?.toLowerCase().includes(userSearch.toLowerCase()) ||
                  u.email.toString().includes(userSearch)) : true
  )
  
  // Bar chart data for top sites
  const barChartData = topDestinations.slice(0, 10)
  const maxVisits = Math.max(...barChartData.map(d => d.visits), 1)
  const totalVisits = barChartData.reduce((sum, d) => sum + d.visits, 0)
  
  // Helper function to get IP info URL
  const getIpInfoUrl = (destination: string) => {
    const host = destination.split(':')[0]
    return `https://check-host.net/ip-info?host=${encodeURIComponent(host)}`
  }
  
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
            <div className="flex items-center gap-2 text-dark-500 text-sm">
              <RefreshCw className="w-4 h-4" />
              <span>{nextRefreshIn}s</span>
            </div>
            <PeriodSelector 
              value={period} 
              onChange={setPeriod}
              options={[
                { value: 'all', label: t('period.all') },
                { value: '24h', label: t('period.24h') },
                { value: '7d', label: t('period.7d') },
                { value: '30d', label: t('period.30d') },
                { value: '365d', label: t('period.365d') },
              ]}
            />
            <motion.button
              onClick={() => {
                handleRefresh()
                setNextRefreshIn(30)
              }}
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
            
            {/* Top Sites Chart */}
            {barChartData.length > 0 && (
              <div className="p-6 rounded-xl bg-dark-800/50 border border-dark-700/50">
                <div className="flex items-center justify-between mb-6">
                  <h3 className="text-lg font-semibold text-dark-100">{t('remnawave.top_sites_chart')}</h3>
                  <div className="flex items-center gap-2 text-dark-400 text-sm">
                    <Globe className="w-4 h-4" />
                    <span>{totalVisits.toLocaleString()} {t('remnawave.total_visits').toLowerCase()}</span>
                  </div>
                </div>
                <div className="space-y-3">
                  {barChartData.map((dest, idx) => {
                    const percentage = (dest.visits / maxVisits) * 100
                    const visitPercentage = totalVisits > 0 ? ((dest.visits / totalVisits) * 100).toFixed(1) : '0'
                    const displayName = dest.destination.split(':')[0]
                    
                    return (
                      <div 
                        key={dest.destination} 
                        className="group cursor-pointer"
                        onClick={() => handleDestinationClick(dest.destination)}
                      >
                        <div className="flex items-center gap-3 mb-1.5">
                          <span className="text-dark-500 text-sm w-5 font-medium">{idx + 1}</span>
                          <div className="flex-1 min-w-0 flex items-center gap-2">
                            <span className="text-dark-200 text-sm truncate group-hover:text-accent-400 transition-colors">
                              {displayName}
                            </span>
                            <ChevronRight className="w-3 h-3 text-dark-600 opacity-0 group-hover:opacity-100 transition-opacity" />
                          </div>
                          <div className="flex items-center gap-3">
                            <span className="text-dark-500 text-xs">{visitPercentage}%</span>
                            <span className="text-dark-300 text-sm font-medium w-16 text-right">
                              {dest.visits.toLocaleString()}
                            </span>
                          </div>
                        </div>
                        <div className="flex items-center gap-3">
                          <div className="w-5" />
                          <div className="flex-1 h-2 bg-dark-700/50 rounded-full overflow-hidden">
                            <motion.div
                              initial={{ width: 0 }}
                              animate={{ width: `${percentage}%` }}
                              transition={{ duration: 0.6, delay: idx * 0.05 }}
                              className="h-full rounded-full bg-gradient-to-r from-accent-500 to-accent-400 
                                       group-hover:from-accent-400 group-hover:to-accent-300 transition-all"
                              style={{
                                boxShadow: '0 0 10px rgba(34, 211, 238, 0.3)'
                              }}
                            />
                          </div>
                        </div>
                      </div>
                    )
                  })}
                </div>
              </div>
            )}
            
            {/* Top Lists */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
              {/* Top Sites */}
              <div className="p-6 rounded-xl bg-dark-800/50 border border-dark-700/50">
                <h3 className="text-lg font-semibold text-dark-100 mb-4">{t('remnawave.top_sites')}</h3>
                <div className="space-y-3 max-h-[600px] overflow-auto">
                  {topDestinations.slice(0, 20).map((dest, idx) => (
                    <div key={dest.destination} className="flex items-center gap-3">
                      <span className="text-dark-500 text-sm w-6">{idx + 1}</span>
                      <div className="flex-1 min-w-0">
                        <div className="text-dark-200 text-sm truncate">
                          {dest.destination}
                        </div>
                      </div>
                      <span className="text-dark-400 text-sm">{dest.visits.toLocaleString()}</span>
                      <a
                        href={getIpInfoUrl(dest.destination)}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="p-1 rounded hover:bg-dark-600 text-dark-500 hover:text-accent-400 transition-colors"
                        title={t('remnawave.ip_info')}
                      >
                        <Info className="w-3.5 h-3.5" />
                      </a>
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
                    <th className="text-right p-4 text-dark-400 font-medium text-sm">IP</th>
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
                      <td className="p-4 text-right">
                        <span className={`inline-flex items-center gap-1 px-2 py-1 rounded text-xs font-medium ${
                          user.unique_ips > 3 ? 'bg-warning/20 text-warning' :
                          user.unique_ips > 0 ? 'bg-accent-500/20 text-accent-400' :
                          'bg-dark-600 text-dark-400'
                        }`}>
                          <Network className="w-3 h-3" />
                          {user.unique_ips}
                        </span>
                      </td>
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
                    <th className="text-right p-4 text-dark-400 font-medium text-sm">{t('remnawave.visits')}</th>
                    <th className="w-10"></th>
                  </tr>
                </thead>
                <tbody>
                  {filteredDestinations.map((dest, idx) => (
                    <tr 
                      key={dest.destination} 
                      className="border-b border-dark-700/50 hover:bg-dark-700/30 cursor-pointer transition-colors"
                      onClick={() => handleDestinationClick(dest.destination)}
                    >
                      <td className="p-4 text-dark-500">{idx + 1}</td>
                      <td className="p-4 text-dark-200 font-mono text-sm">{dest.destination}</td>
                      <td className="p-4 text-right text-dark-200">{dest.visits.toLocaleString()}</td>
                      <td className="p-4">
                        <div className="flex items-center gap-2">
                          <a
                            href={getIpInfoUrl(dest.destination)}
                            target="_blank"
                            rel="noopener noreferrer"
                            onClick={(e) => e.stopPropagation()}
                            className="p-1.5 rounded-lg hover:bg-dark-600 text-dark-400 hover:text-accent-400 transition-colors"
                            title={t('remnawave.ip_info')}
                          >
                            <Info className="w-4 h-4" />
                          </a>
                          <ChevronRight className="w-4 h-4 text-dark-500" />
                        </div>
                      </td>
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
            {/* Collector Status */}
            <div className="p-6 rounded-xl bg-dark-800/50 border border-dark-700/50">
              <h3 className="text-lg font-semibold text-dark-100 mb-4">{t('remnawave.collector_status')}</h3>
              <div className="flex flex-wrap items-center gap-6">
                <div className="flex items-center gap-3">
                  <div className={`w-3 h-3 rounded-full ${collectorStatus?.running ? 'bg-success' : 'bg-dark-500'}`} />
                  <span className="text-dark-300">
                    {collectorStatus?.running ? t('remnawave.collector_running') : t('remnawave.collector_stopped')}
                  </span>
                </div>
                
                {collectorStatus?.last_collect_time && (
                  <div className="flex items-center gap-2 text-dark-400">
                    <Clock className="w-4 h-4" />
                    <span className="text-sm">
                      {t('remnawave.last_collected_at')}: {new Date(collectorStatus.last_collect_time).toLocaleString()}
                    </span>
                  </div>
                )}
                
                {nextCollectIn !== null && nextCollectIn > 0 && (
                  <div className="flex items-center gap-2 text-dark-400">
                    <RefreshCw className="w-4 h-4" />
                    <span className="text-sm">
                      {t('remnawave.next_collect_in')}: {nextCollectIn} {t('common.seconds')}
                    </span>
                  </div>
                )}
                
                <motion.button
                  onClick={handleForceCollect}
                  disabled={isCollecting || !collectorStatus?.running}
                  className="ml-auto px-4 py-2 rounded-lg bg-purple-500 hover:bg-purple-600 text-white 
                           transition-colors disabled:opacity-50 flex items-center gap-2"
                  whileHover={{ scale: 1.02 }}
                  whileTap={{ scale: 0.98 }}
                >
                  {isCollecting ? (
                    <RefreshCw className="w-4 h-4 animate-spin" />
                  ) : (
                    <Play className="w-4 h-4" />
                  )}
                  {isCollecting ? t('remnawave.collecting') : t('remnawave.collect_now')}
                </motion.button>
              </div>
            </div>
            
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
            
            {/* Nodes Selection */}
            <div className="p-6 rounded-xl bg-dark-800/50 border border-dark-700/50">
              <div className="flex items-center justify-between mb-4">
                <h3 className="text-lg font-semibold text-dark-100">{t('remnawave.select_nodes')}</h3>
                <div className="flex items-center gap-2">
                  <button
                    onClick={handleSelectAllNodes}
                    className="px-3 py-1 text-sm rounded-lg bg-dark-700 hover:bg-dark-600 text-dark-300 transition-colors"
                  >
                    {t('remnawave.select_all')}
                  </button>
                  <button
                    onClick={handleDeselectAllNodes}
                    className="px-3 py-1 text-sm rounded-lg bg-dark-700 hover:bg-dark-600 text-dark-300 transition-colors"
                  >
                    {t('remnawave.deselect_all')}
                  </button>
                </div>
              </div>
              
              {/* Server List with Checkboxes */}
              <div className="space-y-2 max-h-[400px] overflow-y-auto">
                {allServers.map(server => {
                  const node = nodes.find(n => n.server_id === server.id)
                  const isSelected = selectedNodeIds.has(server.id)
                  
                  return (
                    <div
                      key={server.id}
                      className={`flex items-center gap-3 p-3 rounded-lg border transition-colors cursor-pointer ${
                        isSelected 
                          ? 'bg-accent-500/10 border-accent-500/30' 
                          : 'bg-dark-900/50 border-dark-700/50 hover:border-dark-600'
                      }`}
                      onClick={() => handleToggleNodeSelection(server.id)}
                    >
                      <input
                        type="checkbox"
                        checked={isSelected}
                        onChange={() => handleToggleNodeSelection(server.id)}
                        onClick={(e) => e.stopPropagation()}
                        className="w-5 h-5 rounded border-dark-600 bg-dark-800 text-accent-500 
                                 focus:ring-accent-500 focus:ring-offset-0 cursor-pointer"
                      />
                      <Server className={`w-4 h-4 ${server.is_active ? 'text-dark-400' : 'text-dark-600'}`} />
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                          <span className={`${server.is_active ? 'text-dark-200' : 'text-dark-500'}`}>
                            {server.name}
                          </span>
                          <span className={`text-xs px-1.5 py-0.5 rounded ${
                            server.is_active ? 'bg-success/20 text-success' : 'bg-dark-600 text-dark-400'
                          }`}>
                            {server.is_active ? 'online' : 'offline'}
                          </span>
                        </div>
                        {node?.last_collected && (
                          <div className="text-xs text-dark-500">
                            {t('remnawave.last_collected')}: {new Date(node.last_collected).toLocaleString()}
                          </div>
                        )}
                        {node?.last_error && (
                          <div className="text-xs text-danger truncate">{node.last_error}</div>
                        )}
                      </div>
                    </div>
                  )
                })}
                {allServers.length === 0 && (
                  <div className="text-center text-dark-500 py-4">{t('remnawave.no_servers')}</div>
                )}
              </div>
              
              {/* Save Nodes Button */}
              {hasNodeChanges && (
                <div className="mt-4 pt-4 border-t border-dark-700">
                  <motion.button
                    onClick={handleSyncNodes}
                    disabled={isSyncingNodes}
                    className="px-4 py-2 rounded-lg bg-accent-500 hover:bg-accent-600 text-white 
                             transition-colors disabled:opacity-50 flex items-center gap-2"
                    whileHover={{ scale: 1.02 }}
                    whileTap={{ scale: 0.98 }}
                  >
                    {isSyncingNodes ? (
                      <RefreshCw className="w-4 h-4 animate-spin" />
                    ) : (
                      <Check className="w-4 h-4" />
                    )}
                    {t('remnawave.save_nodes')} ({selectedNodeIds.size})
                  </motion.button>
                </div>
              )}
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
              
              <div className="mb-4 p-4 rounded-lg bg-dark-800 flex items-center gap-6">
                <div>
                  <span className="text-dark-400">{t('remnawave.total_visits')}:</span>
                  <span className="text-dark-100 text-xl font-bold ml-2">
                    {selectedUser.total_visits.toLocaleString()}
                  </span>
                </div>
                <div>
                  <span className="text-dark-400">{t('remnawave.unique_ips')}:</span>
                  <span className={`text-xl font-bold ml-2 ${
                    selectedUser.unique_ips > 3 ? 'text-warning' : 'text-dark-100'
                  }`}>
                    {selectedUser.unique_ips}
                  </span>
                </div>
              </div>
              
              {/* IP Addresses Section */}
              {selectedUser.ips && selectedUser.ips.length > 0 && (
                <>
                  <h4 className="text-sm font-medium text-dark-400 mb-3 flex items-center gap-2">
                    <Network className="w-4 h-4" />
                    {t('remnawave.ip_addresses')}
                  </h4>
                  <div className="space-y-2 max-h-[200px] overflow-auto mb-6">
                    {selectedUser.ips.map((ip, idx) => (
                      <div key={ip.source_ip} className="flex items-center gap-3 p-2 rounded-lg hover:bg-dark-800 transition-colors">
                        <span className="text-dark-500 text-sm w-6">{idx + 1}</span>
                        <div className="flex-1 min-w-0">
                          <div className="text-dark-200 text-sm font-mono">
                            {ip.source_ip}
                          </div>
                          <div className="text-dark-500 text-xs flex items-center gap-2 flex-wrap">
                            {ip.servers.map((s, i) => (
                              <span key={s.server_id} className="inline-flex items-center gap-1">
                                <Server className="w-3 h-3" />
                                {s.server_name}
                                {i < ip.servers.length - 1 && ','}
                              </span>
                            ))}
                          </div>
                        </div>
                        <span className="text-dark-400 text-sm">{ip.total_count.toLocaleString()}</span>
                        <a
                          href={`https://check-host.net/ip-info?host=${encodeURIComponent(ip.source_ip)}`}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="p-1.5 rounded-lg hover:bg-dark-700 text-dark-500 hover:text-accent-400 transition-colors"
                          title={t('remnawave.ip_info')}
                        >
                          <Info className="w-4 h-4" />
                        </a>
                      </div>
                    ))}
                  </div>
                </>
              )}
              
              <h4 className="text-sm font-medium text-dark-400 mb-3">{t('remnawave.visited_sites')}</h4>
              <div className="space-y-2 max-h-[300px] overflow-auto">
                  {selectedUser.destinations.map((dest, idx) => (
                  <div key={dest.destination} className="flex items-center gap-3 p-2 rounded-lg hover:bg-dark-800 transition-colors">
                    <span className="text-dark-500 text-sm w-6">{idx + 1}</span>
                    <div className="flex-1 min-w-0">
                      <div className="text-dark-200 text-sm truncate font-mono">
                        {dest.destination}
                      </div>
                      {dest.last_seen && (
                        <div className="text-dark-500 text-xs">{t('remnawave.last_seen')}: {new Date(dest.last_seen).toLocaleDateString()}</div>
                      )}
                    </div>
                    <span className="text-dark-400 text-sm">{dest.visits.toLocaleString()}</span>
                    <a
                      href={getIpInfoUrl(dest.destination)}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="p-1.5 rounded-lg hover:bg-dark-700 text-dark-500 hover:text-accent-400 transition-colors"
                      title={t('remnawave.ip_info')}
                    >
                      <Info className="w-4 h-4" />
                    </a>
                  </div>
                ))}
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
      
      {/* Destination Users Modal */}
      <AnimatePresence>
        {selectedDestination && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm"
            onClick={() => {
              setSelectedDestination(null)
              setDestUserSearch('')
            }}
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
                    {t('remnawave.destination_users')}
                  </h3>
                  <div className="flex items-center gap-2 mt-1">
                    <span className="text-dark-400 text-sm font-mono truncate max-w-md">
                      {selectedDestination.destination}
                    </span>
                    <a
                      href={getIpInfoUrl(selectedDestination.destination)}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="flex items-center gap-1 px-2 py-1 rounded-lg bg-dark-700 hover:bg-dark-600 
                               text-dark-300 hover:text-accent-400 transition-colors text-xs"
                      title={t('remnawave.ip_info')}
                    >
                      <Info className="w-3.5 h-3.5" />
                      <span>IP Info</span>
                    </a>
                  </div>
                </div>
                <motion.button
                  onClick={() => {
                    setSelectedDestination(null)
                    setDestUserSearch('')
                  }}
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
                  {selectedDestination.total_visits.toLocaleString()}
                </span>
              </div>
              
              {/* Поиск по пользователям */}
              <div className="relative mb-4">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-dark-500" />
                <input
                  type="text"
                  value={destUserSearch}
                  onChange={(e) => setDestUserSearch(e.target.value)}
                  placeholder={t('remnawave.search_users')}
                  className="w-full pl-9 pr-4 py-2 rounded-lg bg-dark-800 border border-dark-700 
                           text-dark-100 placeholder-dark-500 focus:outline-none focus:border-accent-500 text-sm"
                />
              </div>
              
              <div className="flex items-center justify-between mb-3">
                <h4 className="text-sm font-medium text-dark-400">{t('remnawave.users_visited')}</h4>
                <span className="text-xs text-dark-500">
                  {filteredDestUsers.length} / {selectedDestination.users.length}
                </span>
              </div>
              
              {isLoadingDestUsers ? (
                <div className="flex items-center justify-center py-8">
                  <div className="w-6 h-6 border-2 border-accent-500 border-t-transparent rounded-full animate-spin" />
                </div>
              ) : (
                <div className="space-y-2 max-h-[300px] overflow-auto">
                  {filteredDestUsers.map((user, idx) => (
                    <div 
                      key={user.email} 
                      className="flex items-center gap-3 p-2 rounded-lg hover:bg-dark-800 transition-colors cursor-pointer"
                      onClick={() => {
                        setSelectedDestination(null)
                        setDestUserSearch('')
                        handleUserClick(user.email)
                      }}
                    >
                      <span className="text-dark-500 text-sm w-6">{idx + 1}</span>
                      <div className="flex-1 min-w-0">
                        <div className="text-dark-200 text-sm truncate">
                          {user.username || `User #${user.email}`}
                        </div>
                        {user.status && (
                          <span className={`text-xs px-1.5 py-0.5 rounded ${
                            user.status === 'ACTIVE' ? 'bg-success/20 text-success' :
                            user.status === 'DISABLED' ? 'bg-danger/20 text-danger' :
                            'bg-dark-600 text-dark-300'
                          }`}>
                            {user.status}
                          </span>
                        )}
                      </div>
                      <div className="text-right">
                        <div className="text-dark-200 text-sm">{user.visits.toLocaleString()}</div>
                        <div className="text-dark-500 text-xs">{user.percentage}%</div>
                      </div>
                      <ChevronRight className="w-4 h-4 text-dark-500" />
                    </div>
                  ))}
                  {filteredDestUsers.length === 0 && (
                    <div className="text-dark-500 text-sm text-center py-4">
                      {destUserSearch ? t('remnawave.no_results') : t('remnawave.no_data')}
                    </div>
                  )}
                </div>
              )}
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}
