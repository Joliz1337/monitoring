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
  Network,
  Calendar,
  Link,
  Smartphone,
  ArrowDownUp,
  Copy,
  CheckCircle,
  MessageCircle,
  Trash2,
  Database,
  ChevronUp,
  ChevronDown
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
  RemnawaveDestinationUsers,
  RemnawaveUserFullInfo,
  RemnawaveIpDestinations,
  RemnawaveInfrastructureAddress
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
  
  // Infrastructure addresses state
  const [infrastructureAddresses, setInfrastructureAddresses] = useState<RemnawaveInfrastructureAddress[]>([])
  const [newInfraAddress, setNewInfraAddress] = useState('')
  const [newInfraDescription, setNewInfraDescription] = useState('')
  const [isAddingInfraAddress, setIsAddingInfraAddress] = useState(false)
  const [isResolvingInfra, setIsResolvingInfra] = useState(false)
  const [isRescanningInfra, setIsRescanningInfra] = useState(false)
  const [lastRescanResult, setLastRescanResult] = useState<{ updated_to_infrastructure: number; updated_to_client: number } | null>(null)
  
  // Collector status state
  const [collectorStatus, setCollectorStatus] = useState<RemnawaveCollectorStatus | null>(null)
  const [isCollecting, setIsCollecting] = useState(false)
  const [nextCollectIn, setNextCollectIn] = useState<number | null>(null)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  
  // Stats state
  const [summary, setSummary] = useState<RemnawaveSummary | null>(null)
  const [topDestinations, setTopDestinations] = useState<RemnawaveDestination[]>([])
  const [topUsers, setTopUsers] = useState<RemnawaveUser[]>([])
  const [totalUsers, setTotalUsers] = useState(0)
  const [usersOffset, setUsersOffset] = useState(0)
  const [isLoadingMoreUsers, setIsLoadingMoreUsers] = useState(false)
  const [isSearchingUsers, setIsSearchingUsers] = useState(false)
  const searchTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const USERS_PAGE_SIZE = 100
  
  // User cache state
  const [userCacheStatus, setUserCacheStatus] = useState<{ last_update: string | null; updating: boolean } | null>(null)
  const [isRefreshingUserCache, setIsRefreshingUserCache] = useState(false)
  
  // User details modal
  const [selectedUser, setSelectedUser] = useState<RemnawaveUserDetails | null>(null)
  const [selectedUserFull, setSelectedUserFull] = useState<RemnawaveUserFullInfo | null>(null)
  const [isLoadingUserFull, setIsLoadingUserFull] = useState(false)
  const [userModalTab, setUserModalTab] = useState<'overview' | 'traffic' | 'ips' | 'history' | 'devices'>('overview')
  const [copiedField, setCopiedField] = useState<string | null>(null)
  const [userSearch, setUserSearch] = useState('')
  const [destSearch, setDestSearch] = useState('')
  
  // Users table sorting
  type UserSortField = 'email' | 'username' | 'status' | 'total_visits' | 'unique_sites' | 'unique_ips'
  const [userSortField, setUserSortField] = useState<UserSortField>('total_visits')
  const [userSortDirection, setUserSortDirection] = useState<'asc' | 'desc'>('desc')
  
  // Destination users modal
  const [selectedDestination, setSelectedDestination] = useState<RemnawaveDestinationUsers | null>(null)
  const [isLoadingDestUsers, setIsLoadingDestUsers] = useState(false)
  const [destUserSearch, setDestUserSearch] = useState('')
  
  // IP destinations (expanded view)
  const [expandedIp, setExpandedIp] = useState<string | null>(null)
  const [ipDestinations, setIpDestinations] = useState<RemnawaveIpDestinations | null>(null)
  const [isLoadingIpDest, setIsLoadingIpDest] = useState(false)
  
  // Auto-refresh
  const autoRefreshRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const [nextRefreshIn, setNextRefreshIn] = useState(30)
  
  // DB info state
  const [dbInfo, setDbInfo] = useState<{
    tables: {
      xray_visit_stats: { count: number; first_seen: string | null; last_seen: string | null }
      xray_hourly_stats: { count: number; first_hour: string | null; last_hour: string | null }
      xray_user_ip_stats: { count: number }
      xray_ip_destination_stats: { count: number }
      remnawave_user_cache: { count: number }
    }
  } | null>(null)
  const [isClearingStats, setIsClearingStats] = useState(false)
  const [showClearConfirm, setShowClearConfirm] = useState(false)
  
  // Fetch settings
  const fetchSettings = useCallback(async () => {
    try {
      const [settingsRes, nodesRes, statusRes, dbInfoRes, infraRes, cacheStatusRes] = await Promise.all([
        remnawaveApi.getSettings(),
        remnawaveApi.getNodes(),
        remnawaveApi.getCollectorStatus(),
        remnawaveApi.getDbInfo(),
        remnawaveApi.getInfrastructureAddresses(),
        remnawaveApi.getUserCacheStatus()
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
      // Update DB info
      setDbInfo(dbInfoRes.data)
      // Update infrastructure addresses
      setInfrastructureAddresses(infraRes.data.addresses)
      // Update user cache status
      setUserCacheStatus(cacheStatusRes.data)
    } catch (err) {
      console.error('Failed to fetch settings:', err)
    }
  }, [])
  
  // Fetch stats
  const fetchStats = useCallback(async () => {
    try {
      const [summaryRes, destRes, usersRes] = await Promise.all([
        remnawaveApi.getSummary(period),
        remnawaveApi.getTopDestinations({ period, limit: 100 }),
        remnawaveApi.getTopUsers({ period, limit: USERS_PAGE_SIZE, offset: 0 })
      ])
      setSummary(summaryRes.data)
      setTopDestinations(destRes.data.destinations)
      setTopUsers(usersRes.data.users)
      setTotalUsers(usersRes.data.total)
      setUsersOffset(usersRes.data.users.length)
      setError(null)
    } catch (err) {
      console.error('Failed to fetch stats:', err)
      setError(t('remnawave.failed_fetch'))
    }
  }, [period, t])
  
  // Load more users (pagination)
  const loadMoreUsers = useCallback(async () => {
    if (isLoadingMoreUsers || usersOffset >= totalUsers) return
    
    setIsLoadingMoreUsers(true)
    try {
      const res = await remnawaveApi.getTopUsers({ 
        period, 
        limit: USERS_PAGE_SIZE, 
        offset: usersOffset,
        search: userSearch || undefined
      })
      setTopUsers(prev => [...prev, ...res.data.users])
      setUsersOffset(prev => prev + res.data.users.length)
      setTotalUsers(res.data.total)
    } catch (err) {
      console.error('Failed to load more users:', err)
    } finally {
      setIsLoadingMoreUsers(false)
    }
  }, [isLoadingMoreUsers, usersOffset, totalUsers, period, userSearch])
  
  // Search users with debounce
  const searchUsers = useCallback(async (searchTerm: string) => {
    setIsSearchingUsers(true)
    try {
      const res = await remnawaveApi.getTopUsers({ 
        period, 
        limit: USERS_PAGE_SIZE, 
        offset: 0,
        search: searchTerm || undefined
      })
      setTopUsers(res.data.users)
      setTotalUsers(res.data.total)
      setUsersOffset(res.data.users.length)
    } catch (err) {
      console.error('Failed to search users:', err)
    } finally {
      setIsSearchingUsers(false)
    }
  }, [period])
  
  // Handle user search input with debounce
  const handleUserSearchChange = useCallback((value: string) => {
    setUserSearch(value)
    
    // Clear previous timeout
    if (searchTimeoutRef.current) {
      clearTimeout(searchTimeoutRef.current)
    }
    
    // Debounce search - wait 300ms after user stops typing
    searchTimeoutRef.current = setTimeout(() => {
      searchUsers(value)
    }, 300)
  }, [searchUsers])
  
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
  
  const handleRefreshUserCache = async () => {
    setIsRefreshingUserCache(true)
    try {
      const res = await remnawaveApi.refreshUserCache()
      if (res.data.success) {
        // Refresh stats to show updated statuses
        await Promise.all([fetchStats(), remnawaveApi.getUserCacheStatus().then(r => setUserCacheStatus(r.data))])
      }
    } catch (err) {
      console.error('Failed to refresh user cache:', err)
    } finally {
      setIsRefreshingUserCache(false)
    }
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
  
  // Infrastructure address handlers
  const handleRescanInfraIps = async () => {
    setIsRescanningInfra(true)
    setLastRescanResult(null)
    try {
      const res = await remnawaveApi.rescanInfrastructureIps()
      setLastRescanResult({
        updated_to_infrastructure: res.data.updated_to_infrastructure,
        updated_to_client: res.data.updated_to_client
      })
    } catch (err) {
      console.error('Failed to rescan infrastructure IPs:', err)
    } finally {
      setIsRescanningInfra(false)
    }
  }
  
  const handleAddInfraAddress = async () => {
    if (!newInfraAddress.trim()) return
    
    setIsAddingInfraAddress(true)
    try {
      await remnawaveApi.addInfrastructureAddress(
        newInfraAddress.trim(), 
        newInfraDescription.trim() || undefined
      )
      setNewInfraAddress('')
      setNewInfraDescription('')
      // Refresh infrastructure addresses
      const res = await remnawaveApi.getInfrastructureAddresses()
      setInfrastructureAddresses(res.data.addresses)
      // Auto-rescan existing data
      await handleRescanInfraIps()
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } }
      console.error('Failed to add infrastructure address:', error.response?.data?.detail || err)
    } finally {
      setIsAddingInfraAddress(false)
    }
  }
  
  const handleDeleteInfraAddress = async (id: number) => {
    try {
      await remnawaveApi.deleteInfrastructureAddress(id)
      // Refresh infrastructure addresses
      const res = await remnawaveApi.getInfrastructureAddresses()
      setInfrastructureAddresses(res.data.addresses)
      // Auto-rescan existing data
      await handleRescanInfraIps()
    } catch (err) {
      console.error('Failed to delete infrastructure address:', err)
    }
  }
  
  const handleResolveInfraAddresses = async () => {
    setIsResolvingInfra(true)
    try {
      await remnawaveApi.resolveInfrastructureAddresses()
      // Refresh infrastructure addresses
      const res = await remnawaveApi.getInfrastructureAddresses()
      setInfrastructureAddresses(res.data.addresses)
      // Auto-rescan existing data after DNS update
      await handleRescanInfraIps()
    } catch (err) {
      console.error('Failed to resolve infrastructure addresses:', err)
    } finally {
      setIsResolvingInfra(false)
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
  
  const handleClearStats = async () => {
    setIsClearingStats(true)
    try {
      await remnawaveApi.clearStats()
      // Refresh all data
      await Promise.all([fetchStats(), fetchSettings()])
      setShowClearConfirm(false)
    } catch (err) {
      console.error('Failed to clear stats:', err)
    } finally {
      setIsClearingStats(false)
    }
  }
  
  // Check if selected nodes differ from current nodes
  const currentNodeIds = new Set(allServers.filter(s => s.is_node).map(s => s.id))
  const hasNodeChanges = selectedNodeIds.size !== currentNodeIds.size || 
    ![...selectedNodeIds].every(id => currentNodeIds.has(id))
  
  const handleUserClick = async (email: number) => {
    setUserModalTab('overview')
    setCopiedField(null)
    setExpandedIp(null)
    setIpDestinations(null)
    try {
      // Fetch visit stats and full user info in parallel
      const [statsRes, fullRes] = await Promise.all([
        remnawaveApi.getUserStats(email, period),
        remnawaveApi.getUserFullInfo(email)
      ])
      setSelectedUser(statsRes.data)
      setSelectedUserFull(fullRes.data)
    } catch (err) {
      console.error('Failed to fetch user stats:', err)
    }
  }
  
  const handleLoadLiveUserInfo = async () => {
    if (!selectedUser) return
    setIsLoadingUserFull(true)
    try {
      const res = await remnawaveApi.getUserLiveInfo(selectedUser.email)
      setSelectedUserFull(res.data)
    } catch (err) {
      console.error('Failed to fetch live user info:', err)
    } finally {
      setIsLoadingUserFull(false)
    }
  }
  
  const copyToClipboard = (text: string, field: string) => {
    navigator.clipboard.writeText(text)
    setCopiedField(field)
    setTimeout(() => setCopiedField(null), 2000)
  }
  
  const formatBytes = (bytes: number | null | undefined): string => {
    if (bytes === null || bytes === undefined) return '-'
    if (bytes === 0) return '0 B'
    const k = 1024
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB']
    const i = Math.floor(Math.log(bytes) / Math.log(k))
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i]
  }
  
  const formatDate = (dateStr: string | null | undefined): string => {
    if (!dateStr) return '-'
    return new Date(dateStr).toLocaleDateString()
  }
  
  const formatDateTime = (dateStr: string | null | undefined): string => {
    if (!dateStr) return '-'
    return new Date(dateStr).toLocaleString()
  }
  
  const getDaysRemaining = (expireAt: string | null | undefined): { days: number; isExpired: boolean } | null => {
    if (!expireAt) return null
    const expireDate = new Date(expireAt)
    const now = new Date()
    const diff = expireDate.getTime() - now.getTime()
    const days = Math.ceil(diff / (1000 * 60 * 60 * 24))
    return { days, isExpired: days < 0 }
  }
  
  const getTrafficUsagePercent = (used: number | null | undefined, limit: number | null | undefined): number | null => {
    if (!used || !limit || limit === 0) return null
    return Math.round((used / limit) * 100)
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
  
  const handleToggleIpExpand = async (sourceIp: string, email: number) => {
    if (expandedIp === sourceIp) {
      // Collapse
      setExpandedIp(null)
      setIpDestinations(null)
      return
    }
    
    // Expand and fetch destinations
    setExpandedIp(sourceIp)
    setIsLoadingIpDest(true)
    setIpDestinations(null)
    
    try {
      const res = await remnawaveApi.getIpDestinations(sourceIp, email, period, 50)
      setIpDestinations(res.data)
    } catch (err) {
      console.error('Failed to fetch IP destinations:', err)
    } finally {
      setIsLoadingIpDest(false)
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
  
  // Filter and sort users
  const filteredUsers = topUsers
    .filter(u =>
      userSearch ? (u.username?.toLowerCase().includes(userSearch.toLowerCase()) ||
                    u.email.toString().includes(userSearch)) : true
    )
    .sort((a, b) => {
      const direction = userSortDirection === 'asc' ? 1 : -1
      switch (userSortField) {
        case 'email':
          return (a.email - b.email) * direction
        case 'username':
          const nameA = a.username || ''
          const nameB = b.username || ''
          return nameA.localeCompare(nameB) * direction
        case 'status':
          const statusA = a.status || ''
          const statusB = b.status || ''
          return statusA.localeCompare(statusB) * direction
        case 'total_visits':
          return (a.total_visits - b.total_visits) * direction
        case 'unique_sites':
          return (a.unique_sites - b.unique_sites) * direction
        case 'unique_ips':
          return (a.unique_ips - b.unique_ips) * direction
        default:
          return 0
      }
    })
  
  // Handle sort click
  const handleUserSort = (field: UserSortField) => {
    if (userSortField === field) {
      setUserSortDirection(prev => prev === 'asc' ? 'desc' : 'asc')
    } else {
      setUserSortField(field)
      setUserSortDirection('desc')
    }
  }
  
  // Sortable header component
  const SortableHeader = ({ field, children, align = 'left' }: { field: UserSortField; children: React.ReactNode; align?: 'left' | 'right' }) => (
    <th
      className={`${align === 'right' ? 'text-right' : 'text-left'} p-4 text-dark-400 font-medium text-sm cursor-pointer hover:text-dark-200 transition-colors select-none`}
      onClick={() => handleUserSort(field)}
    >
      <div className={`flex items-center gap-1 ${align === 'right' ? 'justify-end' : ''}`}>
        {children}
        <span className="flex flex-col">
          <ChevronUp className={`w-3 h-3 -mb-1 ${userSortField === field && userSortDirection === 'asc' ? 'text-accent-400' : 'text-dark-600'}`} />
          <ChevronDown className={`w-3 h-3 ${userSortField === field && userSortDirection === 'desc' ? 'text-accent-400' : 'text-dark-600'}`} />
        </span>
      </div>
    </th>
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
            {/* Search and Cache Refresh */}
            <div className="flex items-center gap-4">
              <div className="relative flex-1">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-dark-500" />
                <input
                  type="text"
                  value={userSearch}
                  onChange={(e) => handleUserSearchChange(e.target.value)}
                  placeholder={t('remnawave.search_users')}
                  className="w-full pl-10 pr-4 py-2 rounded-lg bg-dark-800 border border-dark-700 
                           text-dark-100 placeholder-dark-500 focus:outline-none focus:border-accent-500"
                />
                {isSearchingUsers && (
                  <RefreshCw className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-dark-500 animate-spin" />
                )}
              </div>
              <div className="flex items-center gap-3">
                <span className="text-sm text-dark-400">
                  {topUsers.length} / {totalUsers}
                </span>
                {userCacheStatus?.last_update && (
                  <span className="text-xs text-dark-500 hidden lg:inline">
                    {t('remnawave.cache_updated')}: {new Date(userCacheStatus.last_update).toLocaleTimeString()}
                  </span>
                )}
                <button
                  onClick={handleRefreshUserCache}
                  disabled={isRefreshingUserCache || userCacheStatus?.updating}
                  className="flex items-center gap-2 px-3 py-2 rounded-lg bg-dark-700 hover:bg-dark-600 
                           text-dark-200 text-sm transition-colors disabled:opacity-50"
                  title={t('remnawave.refresh_user_cache')}
                >
                  <RefreshCw className={`w-4 h-4 ${isRefreshingUserCache || userCacheStatus?.updating ? 'animate-spin' : ''}`} />
                  <span className="hidden sm:inline">{t('remnawave.sync_cache')}</span>
                </button>
              </div>
            </div>
            
            {/* Users Table */}
            <div className="rounded-xl bg-dark-800/50 border border-dark-700/50 overflow-hidden">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-dark-700">
                    <SortableHeader field="email">ID</SortableHeader>
                    <SortableHeader field="username">{t('remnawave.username')}</SortableHeader>
                    <SortableHeader field="status">{t('remnawave.status')}</SortableHeader>
                    <SortableHeader field="total_visits" align="right">{t('remnawave.visits')}</SortableHeader>
                    <SortableHeader field="unique_sites" align="right">{t('remnawave.sites')}</SortableHeader>
                    <SortableHeader field="unique_ips" align="right">IP</SortableHeader>
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
                          user.infrastructure_ips > 0 ? 'bg-purple-500/20 text-purple-400' :
                          'bg-dark-600 text-dark-400'
                        }`} title={user.unique_ips === 0 && user.infrastructure_ips > 0 ? t('remnawave.only_infra_ips') : undefined}>
                          <Network className="w-3 h-3" />
                          {user.unique_ips === 0 && user.infrastructure_ips > 0 ? (
                            <span className="flex items-center gap-1">
                              <Server className="w-3 h-3" />
                              {user.infrastructure_ips}
                            </span>
                          ) : (
                            user.unique_ips
                          )}
                        </span>
                      </td>
                      <td className="p-4">
                        <ChevronRight className="w-4 h-4 text-dark-500" />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {filteredUsers.length === 0 && !isSearchingUsers && (
                <div className="p-8 text-center text-dark-500">{t('remnawave.no_data')}</div>
              )}
              {isSearchingUsers && (
                <div className="p-8 text-center text-dark-500">
                  <RefreshCw className="w-5 h-5 animate-spin mx-auto mb-2" />
                  {t('common.loading')}...
                </div>
              )}
            </div>
            
            {/* Load More Button */}
            {topUsers.length < totalUsers && !isSearchingUsers && (
              <div className="flex justify-center">
                <button
                  onClick={loadMoreUsers}
                  disabled={isLoadingMoreUsers}
                  className="flex items-center gap-2 px-6 py-2 rounded-lg bg-dark-700 hover:bg-dark-600 
                           text-dark-200 text-sm transition-colors disabled:opacity-50"
                >
                  {isLoadingMoreUsers ? (
                    <>
                      <RefreshCw className="w-4 h-4 animate-spin" />
                      {t('common.loading')}...
                    </>
                  ) : (
                    <>
                      <ChevronDown className="w-4 h-4" />
                      {t('remnawave.load_more')} ({totalUsers - topUsers.length} {t('remnawave.remaining')})
                    </>
                  )}
                </button>
              </div>
            )}
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
                  <div className="flex items-center gap-2">
                    <input
                      type="number"
                      value={editSettings.collection_interval || 300}
                      onChange={(e) => setEditSettings(s => ({ ...s, collection_interval: Math.min(900, Math.max(60, parseInt(e.target.value) || 300)) }))}
                      min={60}
                      max={900}
                      step={60}
                      className="w-32 px-4 py-2 rounded-lg bg-dark-900 border border-dark-700 
                               text-dark-100 focus:outline-none focus:border-accent-500"
                    />
                    <span className="text-dark-500">{t('common.seconds')}</span>
                    <span className="text-dark-600">({Math.round((editSettings.collection_interval || 300) / 60)} {t('common.minutes')})</span>
                  </div>
                  <p className="text-xs text-yellow-500/80 mt-1">{t('remnawave.collection_interval_hint')}</p>
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
            
            {/* Infrastructure Addresses */}
            <div className="p-6 rounded-xl bg-dark-800/50 border border-dark-700/50">
              <div className="flex items-center justify-between mb-4">
                <h3 className="text-lg font-semibold text-dark-100">{t('remnawave.infrastructure_addresses')}</h3>
                <div className="flex items-center gap-2">
                  <button
                    onClick={handleRescanInfraIps}
                    disabled={isRescanningInfra || isResolvingInfra}
                    className="px-3 py-1 text-sm rounded-lg bg-accent-500/20 hover:bg-accent-500/30 text-accent-400 transition-colors flex items-center gap-1"
                    title={t('remnawave.rescan_hint')}
                  >
                    {isRescanningInfra ? (
                      <RefreshCw className="w-3 h-3 animate-spin" />
                    ) : (
                      <Database className="w-3 h-3" />
                    )}
                    {t('remnawave.rescan_existing')}
                  </button>
                  <button
                    onClick={handleResolveInfraAddresses}
                    disabled={isResolvingInfra || isRescanningInfra}
                    className="px-3 py-1 text-sm rounded-lg bg-dark-700 hover:bg-dark-600 text-dark-300 transition-colors flex items-center gap-1"
                  >
                    {isResolvingInfra ? (
                      <RefreshCw className="w-3 h-3 animate-spin" />
                    ) : (
                      <RefreshCw className="w-3 h-3" />
                    )}
                    {t('remnawave.resolve_dns')}
                  </button>
                </div>
              </div>
              
              <p className="text-dark-500 text-sm mb-4">{t('remnawave.infrastructure_addresses_hint')}</p>
              
              {/* Rescan result */}
              {lastRescanResult && (
                <div className="mb-4 p-3 rounded-lg bg-success/10 border border-success/20 text-success text-sm">
                  {t('remnawave.rescan_result', { 
                    toInfra: lastRescanResult.updated_to_infrastructure,
                    toClient: lastRescanResult.updated_to_client 
                  })}
                </div>
              )}
              
              {/* Add new address */}
              <div className="flex gap-2 mb-4">
                <input
                  type="text"
                  value={newInfraAddress}
                  onChange={(e) => setNewInfraAddress(e.target.value)}
                  placeholder={t('remnawave.infrastructure_address_placeholder')}
                  className="flex-1 px-3 py-2 rounded-lg bg-dark-900 border border-dark-700 
                           text-dark-100 placeholder-dark-500 focus:outline-none focus:border-accent-500"
                  onKeyDown={(e) => e.key === 'Enter' && handleAddInfraAddress()}
                />
                <input
                  type="text"
                  value={newInfraDescription}
                  onChange={(e) => setNewInfraDescription(e.target.value)}
                  placeholder={t('remnawave.infrastructure_description_placeholder')}
                  className="w-48 px-3 py-2 rounded-lg bg-dark-900 border border-dark-700 
                           text-dark-100 placeholder-dark-500 focus:outline-none focus:border-accent-500"
                />
                <button
                  onClick={handleAddInfraAddress}
                  disabled={isAddingInfraAddress || !newInfraAddress.trim()}
                  className="px-4 py-2 rounded-lg bg-accent-500 hover:bg-accent-600 text-white 
                           transition-colors disabled:opacity-50 flex items-center gap-1"
                >
                  {isAddingInfraAddress ? (
                    <RefreshCw className="w-4 h-4 animate-spin" />
                  ) : (
                    <Check className="w-4 h-4" />
                  )}
                  {t('common.add')}
                </button>
              </div>
              
              {/* Address list */}
              <div className="space-y-2 max-h-[300px] overflow-y-auto">
                {infrastructureAddresses.map(addr => (
                  <div
                    key={addr.id}
                    className="flex items-center gap-3 p-3 rounded-lg bg-dark-900/50 border border-dark-700/50"
                  >
                    <Network className="w-4 h-4 text-dark-500 flex-shrink-0" />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-dark-200 font-mono">{addr.address}</span>
                        {addr.description && (
                          <span className="text-dark-500 text-sm">({addr.description})</span>
                        )}
                      </div>
                      {addr.resolved_ips && (
                        <div className="text-dark-500 text-xs mt-1">
                          {t('remnawave.resolved_ips')}: {JSON.parse(addr.resolved_ips).join(', ')}
                        </div>
                      )}
                    </div>
                    <button
                      onClick={() => handleDeleteInfraAddress(addr.id)}
                      className="p-2 rounded-lg hover:bg-dark-700 text-dark-500 hover:text-danger transition-colors"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>
                ))}
                {infrastructureAddresses.length === 0 && (
                  <div className="text-center text-dark-500 py-4">{t('remnawave.no_infrastructure_addresses')}</div>
                )}
              </div>
            </div>
            
            {/* Database Management */}
            <div className="p-6 rounded-xl bg-dark-800/50 border border-dark-700/50">
              <h3 className="text-lg font-semibold text-dark-100 mb-4 flex items-center gap-2">
                <Database className="w-5 h-5" />
                {t('remnawave.db_management')}
              </h3>
              
              {/* DB Stats */}
              {dbInfo && (
                <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-6">
                  <div className="p-3 rounded-lg bg-dark-900/50">
                    <div className="text-dark-500 text-xs mb-1">{t('remnawave.db_visits')}</div>
                    <div className="text-dark-100 text-lg font-semibold">
                      {dbInfo.tables.xray_visit_stats.count.toLocaleString()}
                    </div>
                    {dbInfo.tables.xray_visit_stats.first_seen && (
                      <div className="text-dark-600 text-xs mt-1">
                        {formatDate(dbInfo.tables.xray_visit_stats.first_seen)} - {formatDate(dbInfo.tables.xray_visit_stats.last_seen)}
                      </div>
                    )}
                  </div>
                  <div className="p-3 rounded-lg bg-dark-900/50">
                    <div className="text-dark-500 text-xs mb-1">{t('remnawave.db_ips')}</div>
                    <div className="text-dark-100 text-lg font-semibold">
                      {dbInfo.tables.xray_user_ip_stats.count.toLocaleString()}
                    </div>
                  </div>
                  <div className="p-3 rounded-lg bg-dark-900/50">
                    <div className="text-dark-500 text-xs mb-1">{t('remnawave.db_ip_destinations')}</div>
                    <div className="text-dark-100 text-lg font-semibold">
                      {dbInfo.tables.xray_ip_destination_stats.count.toLocaleString()}
                    </div>
                  </div>
                  <div className="p-3 rounded-lg bg-dark-900/50">
                    <div className="text-dark-500 text-xs mb-1">{t('remnawave.db_hourly')}</div>
                    <div className="text-dark-100 text-lg font-semibold">
                      {dbInfo.tables.xray_hourly_stats.count.toLocaleString()}
                    </div>
                  </div>
                  <div className="p-3 rounded-lg bg-dark-900/50">
                    <div className="text-dark-500 text-xs mb-1">{t('remnawave.db_users_cache')}</div>
                    <div className="text-dark-100 text-lg font-semibold">
                      {dbInfo.tables.remnawave_user_cache.count.toLocaleString()}
                    </div>
                  </div>
                </div>
              )}
              
              <div className="p-4 rounded-lg bg-dark-900/50 border border-dark-700/50">
                <div className="flex items-start gap-4">
                  <div className="flex-1">
                    <h4 className="text-dark-200 font-medium mb-1">{t('remnawave.clear_stats')}</h4>
                    <p className="text-dark-500 text-sm">{t('remnawave.clear_stats_desc')}</p>
                    <p className="text-dark-600 text-xs mt-1">{t('remnawave.clear_stats_note')}</p>
                  </div>
                  <motion.button
                    onClick={() => setShowClearConfirm(true)}
                    disabled={isClearingStats}
                    className="px-4 py-2 rounded-lg bg-danger/20 hover:bg-danger/30 text-danger 
                             border border-danger/30 transition-colors disabled:opacity-50 
                             flex items-center gap-2 whitespace-nowrap"
                    whileHover={{ scale: 1.02 }}
                    whileTap={{ scale: 0.98 }}
                  >
                    {isClearingStats ? (
                      <RefreshCw className="w-4 h-4 animate-spin" />
                    ) : (
                      <Trash2 className="w-4 h-4" />
                    )}
                    {t('remnawave.clear_db')}
                  </motion.button>
                </div>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
      
      {/* User Details Modal - Extended */}
      <AnimatePresence>
        {selectedUser && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm"
            onClick={() => { setSelectedUser(null); setSelectedUserFull(null); setExpandedIp(null); setIpDestinations(null); }}
          >
            <motion.div
              initial={{ scale: 0.95, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              exit={{ scale: 0.95, opacity: 0 }}
              className="w-full max-w-4xl max-h-[90vh] overflow-hidden bg-dark-900 rounded-xl border border-dark-700 flex flex-col"
              onClick={(e) => e.stopPropagation()}
            >
              {/* Header */}
              <div className="flex items-center justify-between p-6 border-b border-dark-700">
                <div className="flex items-center gap-4">
                  <div>
                    <h3 className="text-xl font-semibold text-dark-100">
                      {selectedUser.username || `User #${selectedUser.email}`}
                    </h3>
                    <div className="flex items-center gap-3 mt-1 flex-wrap">
                      <span className="text-dark-400 text-sm">ID: {selectedUser.email}</span>
                      {selectedUserFull?.uuid && (
                        <button
                          onClick={() => copyToClipboard(selectedUserFull.uuid!, 'uuid')}
                          className="flex items-center gap-1 text-dark-500 text-xs hover:text-accent-400 transition-colors"
                          title="Copy UUID"
                        >
                          UUID: {selectedUserFull.uuid.slice(0, 8)}...
                          {copiedField === 'uuid' ? <CheckCircle className="w-3 h-3 text-success" /> : <Copy className="w-3 h-3" />}
                        </button>
                      )}
                      {selectedUser.status && (
                        <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                          selectedUser.status === 'ACTIVE' ? 'bg-success/20 text-success' :
                          selectedUser.status === 'DISABLED' ? 'bg-danger/20 text-danger' :
                          selectedUser.status === 'LIMITED' ? 'bg-warning/20 text-warning' :
                          selectedUser.status === 'EXPIRED' ? 'bg-orange-500/20 text-orange-400' :
                          'bg-dark-600 text-dark-300'
                        }`}>
                          {selectedUser.status}
                        </span>
                      )}
                      {selectedUserFull?.telegram_id && (
                        <span className="flex items-center gap-1 text-dark-400 text-xs">
                          <MessageCircle className="w-3 h-3" />
                          TG: {selectedUserFull.telegram_id}
                        </span>
                      )}
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <motion.button
                    onClick={handleLoadLiveUserInfo}
                    disabled={isLoadingUserFull}
                    className="p-2 rounded-lg bg-dark-800 hover:bg-dark-700 text-dark-300 
                             hover:text-dark-100 transition-colors disabled:opacity-50"
                    whileHover={{ scale: 1.05 }}
                    whileTap={{ scale: 0.95 }}
                    title={t('remnawave.refresh_live')}
                  >
                    <RefreshCw className={`w-4 h-4 ${isLoadingUserFull ? 'animate-spin' : ''}`} />
                  </motion.button>
                  <motion.button
                    onClick={() => { setSelectedUser(null); setSelectedUserFull(null); setExpandedIp(null); setIpDestinations(null); }}
                    className="p-2 rounded-lg hover:bg-dark-700 text-dark-400 transition-colors"
                    whileHover={{ scale: 1.1 }}
                    whileTap={{ scale: 0.9 }}
                  >
                    <X className="w-5 h-5" />
                  </motion.button>
                </div>
              </div>
              
              {/* Modal Tabs */}
              <div className="flex gap-1 p-2 bg-dark-800/50 border-b border-dark-700">
                {[
                  { id: 'overview' as const, label: t('remnawave.overview'), icon: <BarChart3 className="w-4 h-4" /> },
                  { id: 'traffic' as const, label: t('remnawave.traffic'), icon: <ArrowDownUp className="w-4 h-4" /> },
                  { id: 'ips' as const, label: `IP (${selectedUser.unique_client_ips ?? selectedUser.unique_ips})`, icon: <Network className="w-4 h-4" /> },
                  { id: 'history' as const, label: t('remnawave.sub_history'), icon: <Clock className="w-4 h-4" /> },
                  { id: 'devices' as const, label: t('remnawave.devices'), icon: <Smartphone className="w-4 h-4" /> },
                ].map(tab => (
                  <button
                    key={tab.id}
                    onClick={() => setUserModalTab(tab.id)}
                    className={`flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium transition-all ${
                      userModalTab === tab.id
                        ? 'bg-accent-500/20 text-accent-400'
                        : 'text-dark-400 hover:text-dark-200 hover:bg-dark-700/50'
                    }`}
                  >
                    {tab.icon}
                    {tab.label}
                  </button>
                ))}
              </div>
              
              {/* Modal Content */}
              <div className="flex-1 overflow-auto p-6">
                {/* Overview Tab */}
                {userModalTab === 'overview' && (
                  <div className="space-y-6">
                    {/* Stats Summary */}
                    <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                      <div className="p-4 rounded-lg bg-dark-800">
                        <div className="text-dark-400 text-sm">{t('remnawave.total_visits')}</div>
                        <div className="text-2xl font-bold text-dark-100">
                          {selectedUser.total_visits.toLocaleString()}
                        </div>
                      </div>
                      <div className="p-4 rounded-lg bg-dark-800">
                        <div className="text-dark-400 text-sm">{t('remnawave.unique_client_ips')}</div>
                        <div className={`text-2xl font-bold ${(selectedUser.unique_client_ips ?? selectedUser.unique_ips) > 3 ? 'text-warning' : 'text-dark-100'}`}>
                          {selectedUser.unique_client_ips ?? selectedUser.unique_ips}
                        </div>
                      </div>
                      <div className="p-4 rounded-lg bg-dark-800">
                        <div className="text-dark-400 text-sm">{t('remnawave.unique_sites')}</div>
                        <div className="text-2xl font-bold text-dark-100">
                          {selectedUser.destinations?.length || 0}
                        </div>
                      </div>
                      <div className="p-4 rounded-lg bg-dark-800">
                        <div className="text-dark-400 text-sm">{t('remnawave.device_limit')}</div>
                        <div className="text-2xl font-bold text-dark-100">
                          {selectedUserFull?.hwid_device_limit ?? '-'}
                        </div>
                      </div>
                    </div>
                    
                    {/* Subscription Info */}
                    <div className="p-4 rounded-lg bg-dark-800 space-y-4">
                      <h4 className="text-sm font-medium text-dark-300 flex items-center gap-2">
                        <Calendar className="w-4 h-4" />
                        {t('remnawave.subscription_info')}
                      </h4>
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                        {/* Expiration */}
                        <div>
                          <div className="text-dark-500 text-xs mb-1">{t('remnawave.expires_at')}</div>
                          {selectedUserFull?.expire_at ? (
                            <div className="flex items-center gap-2">
                              <span className="text-dark-200">{formatDateTime(selectedUserFull.expire_at)}</span>
                              {(() => {
                                const remaining = getDaysRemaining(selectedUserFull.expire_at)
                                if (!remaining) return null
                                return (
                                  <span className={`text-xs px-2 py-0.5 rounded ${
                                    remaining.isExpired ? 'bg-danger/20 text-danger' :
                                    remaining.days <= 7 ? 'bg-warning/20 text-warning' :
                                    'bg-success/20 text-success'
                                  }`}>
                                    {remaining.isExpired ? t('remnawave.expired') : `${remaining.days} ${t('remnawave.days_left')}`}
                                  </span>
                                )
                              })()}
                            </div>
                          ) : (
                            <span className="text-dark-500">-</span>
                          )}
                        </div>
                        
                        {/* Created */}
                        <div>
                          <div className="text-dark-500 text-xs mb-1">{t('remnawave.created_at')}</div>
                          <span className="text-dark-200">{formatDateTime(selectedUserFull?.created_at)}</span>
                        </div>
                        
                        {/* Last Online */}
                        <div>
                          <div className="text-dark-500 text-xs mb-1">{t('remnawave.last_online')}</div>
                          <span className="text-dark-200">{formatDateTime(selectedUserFull?.online_at)}</span>
                        </div>
                        
                        {/* First Connected */}
                        <div>
                          <div className="text-dark-500 text-xs mb-1">{t('remnawave.first_connected')}</div>
                          <span className="text-dark-200">{formatDateTime(selectedUserFull?.first_connected_at)}</span>
                        </div>
                        
                        {/* Last Sub Opened */}
                        <div>
                          <div className="text-dark-500 text-xs mb-1">{t('remnawave.last_sub_opened')}</div>
                          <span className="text-dark-200">{formatDateTime(selectedUserFull?.sub_last_opened_at)}</span>
                        </div>
                        
                        {/* Tag */}
                        {selectedUserFull?.tag && (
                          <div>
                            <div className="text-dark-500 text-xs mb-1">{t('remnawave.tag')}</div>
                            <span className="text-dark-200 px-2 py-0.5 rounded bg-dark-700 text-sm">{selectedUserFull.tag}</span>
                          </div>
                        )}
                      </div>
                      
                      {/* Subscription URL */}
                      {selectedUserFull?.subscription_url && (
                        <div>
                          <div className="text-dark-500 text-xs mb-1 flex items-center gap-1">
                            <Link className="w-3 h-3" />
                            {t('remnawave.subscription_url')}
                          </div>
                          <div className="flex items-center gap-2">
                            <code className="flex-1 text-xs text-dark-300 bg-dark-900 p-2 rounded truncate">
                              {selectedUserFull.subscription_url}
                            </code>
                            <button
                              onClick={() => copyToClipboard(selectedUserFull.subscription_url!, 'sub_url')}
                              className="p-2 rounded-lg bg-dark-700 hover:bg-dark-600 text-dark-400 hover:text-accent-400 transition-colors"
                              title="Copy URL"
                            >
                              {copiedField === 'sub_url' ? <CheckCircle className="w-4 h-4 text-success" /> : <Copy className="w-4 h-4" />}
                            </button>
                          </div>
                        </div>
                      )}
                      
                      {/* Description */}
                      {selectedUserFull?.description && (
                        <div>
                          <div className="text-dark-500 text-xs mb-1">{t('remnawave.description')}</div>
                          <p className="text-dark-300 text-sm">{selectedUserFull.description}</p>
                        </div>
                      )}
                    </div>
                    
                    {/* Top Sites */}
                    <div>
                      <h4 className="text-sm font-medium text-dark-400 mb-3">{t('remnawave.top_visited_sites')}</h4>
                      <div className="space-y-2 max-h-[200px] overflow-auto">
                        {selectedUser.destinations.slice(0, 10).map((dest, idx) => (
                          <div key={dest.destination} className="flex items-center gap-3 p-2 rounded-lg hover:bg-dark-800 transition-colors">
                            <span className="text-dark-500 text-sm w-6">{idx + 1}</span>
                            <div className="flex-1 min-w-0">
                              <div className="text-dark-200 text-sm truncate font-mono">{dest.destination}</div>
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
                    </div>
                  </div>
                )}
                
                {/* Traffic Tab */}
                {userModalTab === 'traffic' && (
                  <div className="space-y-6">
                    {/* Traffic Usage */}
                    <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                      <div className="p-4 rounded-lg bg-dark-800">
                        <div className="flex items-center gap-2 text-dark-400 text-sm mb-2">
                          <ArrowDownUp className="w-4 h-4" />
                          {t('remnawave.used_traffic')}
                        </div>
                        <div className="text-2xl font-bold text-dark-100">
                          {formatBytes(selectedUserFull?.used_traffic_bytes)}
                        </div>
                        {selectedUserFull?.traffic_limit_bytes && selectedUserFull.traffic_limit_bytes > 0 && (
                          <div className="mt-2">
                            <div className="flex justify-between text-xs text-dark-500 mb-1">
                              <span>{t('remnawave.of')} {formatBytes(selectedUserFull.traffic_limit_bytes)}</span>
                              <span>{getTrafficUsagePercent(selectedUserFull.used_traffic_bytes, selectedUserFull.traffic_limit_bytes)}%</span>
                            </div>
                            <div className="h-2 bg-dark-700 rounded-full overflow-hidden">
                              <div 
                                className={`h-full rounded-full transition-all ${
                                  (getTrafficUsagePercent(selectedUserFull.used_traffic_bytes, selectedUserFull.traffic_limit_bytes) || 0) > 90 
                                    ? 'bg-danger' 
                                    : (getTrafficUsagePercent(selectedUserFull.used_traffic_bytes, selectedUserFull.traffic_limit_bytes) || 0) > 70 
                                      ? 'bg-warning' 
                                      : 'bg-accent-500'
                                }`}
                                style={{ width: `${Math.min(getTrafficUsagePercent(selectedUserFull.used_traffic_bytes, selectedUserFull.traffic_limit_bytes) || 0, 100)}%` }}
                              />
                            </div>
                          </div>
                        )}
                      </div>
                      
                      <div className="p-4 rounded-lg bg-dark-800">
                        <div className="text-dark-400 text-sm mb-2">{t('remnawave.traffic_limit')}</div>
                        <div className="text-2xl font-bold text-dark-100">
                          {selectedUserFull?.traffic_limit_bytes && selectedUserFull.traffic_limit_bytes > 0 
                            ? formatBytes(selectedUserFull.traffic_limit_bytes)
                            : t('remnawave.unlimited')}
                        </div>
                        {selectedUserFull?.traffic_limit_strategy && selectedUserFull.traffic_limit_strategy !== 'NO_RESET' && (
                          <div className="mt-1 text-xs text-dark-500">
                            {t('remnawave.reset_strategy')}: {selectedUserFull.traffic_limit_strategy}
                          </div>
                        )}
                      </div>
                      
                      <div className="p-4 rounded-lg bg-dark-800">
                        <div className="text-dark-400 text-sm mb-2">{t('remnawave.lifetime_traffic')}</div>
                        <div className="text-2xl font-bold text-dark-100">
                          {formatBytes(selectedUserFull?.lifetime_used_traffic_bytes)}
                        </div>
                        {selectedUserFull?.last_traffic_reset_at && (
                          <div className="mt-1 text-xs text-dark-500">
                            {t('remnawave.last_reset')}: {formatDate(selectedUserFull.last_traffic_reset_at)}
                          </div>
                        )}
                      </div>
                    </div>
                    
                    {/* Bandwidth Stats Chart (from live API) */}
                    {selectedUserFull?.bandwidth_stats?.categories && selectedUserFull.bandwidth_stats.sparklineData && selectedUserFull.bandwidth_stats.sparklineData.length > 0 && (
                      <div className="p-4 rounded-lg bg-dark-800">
                        <div className="flex items-center justify-between mb-4">
                          <h4 className="text-sm font-medium text-dark-300">{t('remnawave.daily_traffic')}</h4>
                          <span className="text-dark-500 text-xs">
                            {t('remnawave.total')}: {formatBytes(selectedUserFull.bandwidth_stats.sparklineData.reduce((a, b) => a + b, 0))}
                          </span>
                        </div>
                        
                        {/* Traffic Chart with Date Labels */}
                        <div className="flex gap-1">
                          {(() => {
                            const data = selectedUserFull.bandwidth_stats!.sparklineData!
                            const categories = selectedUserFull.bandwidth_stats!.categories!
                            const maxValue = Math.max(...data, 1)
                            return data.map((value, idx) => {
                              // Parse date and get day number
                              const dateStr = categories[idx]
                              const day = dateStr ? dateStr.split('-')[2]?.replace(/^0/, '') : ''
                              return (
                                <div key={idx} className="flex-1 flex flex-col items-center">
                                  {/* Bar */}
                                  <div className="w-full h-32 flex items-end">
                                    <div
                                      className="w-full bg-accent-500/80 hover:bg-accent-400 rounded-t transition-colors cursor-pointer group relative"
                                      style={{ height: `${Math.max((value / maxValue) * 100, 2)}%` }}
                                      title={`${dateStr}: ${formatBytes(value)}`}
                                    >
                                      <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-1 px-2 py-1 bg-dark-900 
                                                    rounded text-xs text-dark-200 whitespace-nowrap opacity-0 group-hover:opacity-100 
                                                    transition-opacity pointer-events-none z-10">
                                        {formatBytes(value)}
                                      </div>
                                    </div>
                                  </div>
                                  {/* Date Label */}
                                  <span className="text-[10px] text-dark-500 mt-1">{day}</span>
                                </div>
                              )
                            })
                          })()}
                        </div>
                        
                        {/* Top Nodes */}
                        {selectedUserFull.bandwidth_stats.topNodes && selectedUserFull.bandwidth_stats.topNodes.length > 0 && (
                          <div className="mt-4 pt-4 border-t border-dark-700">
                            <h5 className="text-xs font-medium text-dark-400 mb-2">{t('remnawave.top_nodes')}</h5>
                            <div className="space-y-2">
                              {selectedUserFull.bandwidth_stats.topNodes.slice(0, 5).map((node) => (
                                <div key={node.uuid} className="flex items-center gap-2">
                                  <div 
                                    className="w-3 h-3 rounded-full" 
                                    style={{ backgroundColor: node.color }}
                                  />
                                  <span className="text-dark-300 text-sm flex-1 truncate">
                                    {node.name} ({node.countryCode})
                                  </span>
                                  <span className="text-dark-400 text-sm">{formatBytes(node.total)}</span>
                                </div>
                              ))}
                            </div>
                          </div>
                        )}
                      </div>
                    )}
                    
                    {!selectedUserFull?.bandwidth_stats && (
                      <div className="p-6 rounded-lg bg-dark-800 text-center">
                        <ArrowDownUp className="w-8 h-8 text-dark-600 mx-auto mb-2" />
                        <p className="text-dark-500">{t('remnawave.click_refresh_for_stats')}</p>
                        <button
                          onClick={handleLoadLiveUserInfo}
                          disabled={isLoadingUserFull}
                          className="mt-3 px-4 py-2 rounded-lg bg-accent-500 hover:bg-accent-600 text-white 
                                   text-sm transition-colors disabled:opacity-50"
                        >
                          {isLoadingUserFull ? t('common.loading') : t('remnawave.load_live_data')}
                        </button>
                      </div>
                    )}
                  </div>
                )}
                
                {/* IPs Tab */}
                {userModalTab === 'ips' && (
                  <div className="space-y-6">
                    {/* Client IPs Section */}
                    <div>
                      <h4 className="text-sm font-medium text-dark-300 mb-3 flex items-center gap-2">
                        <Users className="w-4 h-4" />
                        {t('remnawave.client_ips')} ({selectedUser.client_ips?.length || selectedUser.ips?.length || 0})
                      </h4>
                      {(selectedUser.client_ips || selectedUser.ips) && (selectedUser.client_ips || selectedUser.ips).length > 0 ? (
                        <div className="space-y-2">
                          {(selectedUser.client_ips || selectedUser.ips).map((ip, idx) => (
                            <div key={ip.source_ip} className="rounded-lg bg-dark-800 overflow-hidden">
                              {/* IP Header - Clickable */}
                              <div 
                                className="flex items-center gap-3 p-3 hover:bg-dark-700 transition-colors cursor-pointer"
                                onClick={() => handleToggleIpExpand(ip.source_ip, selectedUser.email)}
                              >
                                <button className="p-1 text-dark-500 hover:text-dark-300 transition-colors">
                                  {expandedIp === ip.source_ip ? (
                                    <ChevronDown className="w-4 h-4" />
                                  ) : (
                                    <ChevronRight className="w-4 h-4" />
                                  )}
                                </button>
                                <span className="text-dark-500 text-sm w-6">{idx + 1}</span>
                                <div className="flex-1 min-w-0">
                                  <div className="flex items-center gap-2">
                                    <span className="text-dark-200 font-mono">{ip.source_ip}</span>
                                    <button
                                      onClick={(e) => { e.stopPropagation(); copyToClipboard(ip.source_ip, `ip_${idx}`); }}
                                      className="p-1 rounded hover:bg-dark-600 text-dark-500 hover:text-accent-400 transition-colors"
                                    >
                                      {copiedField === `ip_${idx}` ? <CheckCircle className="w-3 h-3 text-success" /> : <Copy className="w-3 h-3" />}
                                    </button>
                                  </div>
                                  <div className="text-dark-500 text-xs flex items-center gap-2 flex-wrap mt-1">
                                    {ip.servers.map((s) => (
                                      <span key={s.server_id} className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-dark-700">
                                        <Server className="w-3 h-3" />
                                        {s.server_name}: {s.count}
                                      </span>
                                    ))}
                                  </div>
                                  {ip.last_seen && (
                                    <div className="text-dark-600 text-xs mt-1">
                                      {t('remnawave.last_seen')}: {formatDateTime(ip.last_seen)}
                                    </div>
                                  )}
                                </div>
                                <span className="text-dark-300 font-medium">{ip.total_count.toLocaleString()}</span>
                                <a
                                  href={`https://check-host.net/ip-info?host=${encodeURIComponent(ip.source_ip)}`}
                                  target="_blank"
                                  rel="noopener noreferrer"
                                  onClick={(e) => e.stopPropagation()}
                                  className="p-2 rounded-lg hover:bg-dark-600 text-dark-400 hover:text-accent-400 transition-colors"
                                  title={t('remnawave.ip_info')}
                                >
                                  <ExternalLink className="w-4 h-4" />
                                </a>
                              </div>
                              
                              {/* Expanded Destinations */}
                              {expandedIp === ip.source_ip && (
                                <div className="border-t border-dark-700 bg-dark-900/50 p-3">
                                  <div className="flex items-center gap-2 mb-3">
                                    <Globe className="w-4 h-4 text-dark-500" />
                                    <span className="text-dark-400 text-sm font-medium">{t('remnawave.destinations_from_ip')}</span>
                                  </div>
                                  
                                  {isLoadingIpDest ? (
                                    <div className="flex items-center justify-center py-4">
                                      <div className="w-5 h-5 border-2 border-accent-500 border-t-transparent rounded-full animate-spin" />
                                    </div>
                                  ) : ipDestinations && ipDestinations.destinations.length > 0 ? (
                                    <div className="space-y-1 max-h-[300px] overflow-auto">
                                      {ipDestinations.destinations.map((dest, destIdx) => (
                                        <div 
                                          key={dest.destination} 
                                          className="flex items-center gap-2 p-2 rounded hover:bg-dark-800 transition-colors"
                                        >
                                          <span className="text-dark-600 text-xs w-5">{destIdx + 1}</span>
                                          <span className="text-dark-300 text-sm font-mono flex-1 truncate">
                                            {dest.destination}
                                          </span>
                                          <span className="text-dark-500 text-xs">
                                            {dest.percentage}%
                                          </span>
                                          <span className="text-dark-400 text-sm w-16 text-right">
                                            {dest.connections.toLocaleString()}
                                          </span>
                                          <a
                                            href={getIpInfoUrl(dest.destination)}
                                            target="_blank"
                                            rel="noopener noreferrer"
                                            className="p-1 rounded hover:bg-dark-700 text-dark-500 hover:text-accent-400 transition-colors"
                                            title={t('remnawave.ip_info')}
                                          >
                                            <Info className="w-3 h-3" />
                                          </a>
                                        </div>
                                      ))}
                                    </div>
                                  ) : (
                                    <div className="text-dark-500 text-sm text-center py-3">
                                      {t('remnawave.no_data')}
                                    </div>
                                  )}
                                  
                                  {ipDestinations && ipDestinations.total_connections > 0 && (
                                    <div className="mt-3 pt-2 border-t border-dark-700 flex justify-between text-xs text-dark-500">
                                      <span>{t('remnawave.total')}: {ipDestinations.destinations.length} {t('remnawave.sites').toLowerCase()}</span>
                                      <span>{ipDestinations.total_connections.toLocaleString()} {t('remnawave.connections').toLowerCase()}</span>
                                    </div>
                                  )}
                                </div>
                              )}
                            </div>
                          ))}
                        </div>
                      ) : (
                        <div className="p-4 rounded-lg bg-dark-800/50 text-center">
                          <p className="text-dark-500 text-sm">{t('remnawave.no_client_ips')}</p>
                        </div>
                      )}
                    </div>
                    
                    {/* Infrastructure IPs Section */}
                    {selectedUser.infrastructure_ips && selectedUser.infrastructure_ips.length > 0 && (
                      <div>
                        <h4 className="text-sm font-medium text-dark-300 mb-3 flex items-center gap-2">
                          <Server className="w-4 h-4" />
                          {t('remnawave.infrastructure_ips')} ({selectedUser.infrastructure_ips.length})
                        </h4>
                        <div className="p-4 rounded-lg bg-dark-800/50 border border-dark-700/50">
                          <p className="text-dark-500 text-xs mb-3">{t('remnawave.infrastructure_ips_hint')}</p>
                          <div className="space-y-2">
                            {selectedUser.infrastructure_ips.map((ip, idx) => (
                              <div key={ip.source_ip} className="flex items-center gap-3 p-2 rounded bg-dark-900/50">
                                <span className="text-dark-600 text-sm w-4">{idx + 1}</span>
                                <span className="text-dark-400 font-mono text-sm">{ip.source_ip}</span>
                                <div className="flex-1 flex items-center gap-1 flex-wrap">
                                  {ip.servers.map((s) => (
                                    <span key={s.server_id} className="text-dark-600 text-xs px-1.5 py-0.5 rounded bg-dark-700">
                                      {s.server_name}: {s.count}
                                    </span>
                                  ))}
                                </div>
                                <span className="text-dark-500 text-sm">{ip.total_count.toLocaleString()}</span>
                              </div>
                            ))}
                          </div>
                        </div>
                      </div>
                    )}
                    
                    {/* Empty state */}
                    {(!selectedUser.client_ips || selectedUser.client_ips.length === 0) && 
                     (!selectedUser.ips || selectedUser.ips.length === 0) &&
                     (!selectedUser.infrastructure_ips || selectedUser.infrastructure_ips.length === 0) && (
                      <div className="p-6 rounded-lg bg-dark-800 text-center">
                        <Network className="w-8 h-8 text-dark-600 mx-auto mb-2" />
                        <p className="text-dark-500">{t('remnawave.no_ip_data')}</p>
                      </div>
                    )}
                  </div>
                )}
                
                {/* Subscription History Tab */}
                {userModalTab === 'history' && (
                  <div className="space-y-4">
                    {selectedUserFull?.subscription_history?.records && selectedUserFull.subscription_history.records.length > 0 ? (
                      <div className="space-y-2">
                        {selectedUserFull.subscription_history.records.map((record, idx) => (
                          <div key={record.id} className="flex items-center gap-3 p-3 rounded-lg bg-dark-800">
                            <span className="text-dark-500 text-sm w-6">{idx + 1}</span>
                            <div className="flex-1 min-w-0">
                              <div className="flex items-center gap-2">
                                <span className="text-dark-200 font-mono">{record.requestIp || 'Unknown IP'}</span>
                                {record.requestIp && (
                                  <a
                                    href={`https://check-host.net/ip-info?host=${encodeURIComponent(record.requestIp)}`}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    className="p-1 rounded hover:bg-dark-700 text-dark-500 hover:text-accent-400 transition-colors"
                                  >
                                    <ExternalLink className="w-3 h-3" />
                                  </a>
                                )}
                              </div>
                              {record.userAgent && (
                                <div className="text-dark-500 text-xs mt-1 truncate" title={record.userAgent}>
                                  {record.userAgent}
                                </div>
                              )}
                            </div>
                            <span className="text-dark-400 text-sm whitespace-nowrap">
                              {formatDateTime(record.requestAt)}
                            </span>
                          </div>
                        ))}
                      </div>
                    ) : !selectedUserFull?.subscription_history ? (
                      <div className="p-6 rounded-lg bg-dark-800 text-center">
                        <Clock className="w-8 h-8 text-dark-600 mx-auto mb-2" />
                        <p className="text-dark-500">{t('remnawave.click_refresh_for_history')}</p>
                        <button
                          onClick={handleLoadLiveUserInfo}
                          disabled={isLoadingUserFull}
                          className="mt-3 px-4 py-2 rounded-lg bg-accent-500 hover:bg-accent-600 text-white 
                                   text-sm transition-colors disabled:opacity-50"
                        >
                          {isLoadingUserFull ? t('common.loading') : t('remnawave.load_live_data')}
                        </button>
                      </div>
                    ) : (
                      <div className="p-6 rounded-lg bg-dark-800 text-center">
                        <Clock className="w-8 h-8 text-dark-600 mx-auto mb-2" />
                        <p className="text-dark-500">{t('remnawave.no_history_data')}</p>
                      </div>
                    )}
                  </div>
                )}
                
                {/* Devices Tab */}
                {userModalTab === 'devices' && (
                  <div className="space-y-4">
                    {/* Device Limit Info */}
                    <div className="p-4 rounded-lg bg-dark-800">
                      <div className="flex items-center justify-between">
                        <div>
                          <div className="text-dark-400 text-sm">{t('remnawave.device_limit')}</div>
                          <div className="text-xl font-bold text-dark-100">
                            {selectedUserFull?.hwid_device_limit ?? t('remnawave.unlimited')}
                          </div>
                        </div>
                        {selectedUserFull?.hwid_devices?.devices && (
                          <div className="text-right">
                            <div className="text-dark-400 text-sm">{t('remnawave.devices_used')}</div>
                            <div className="text-xl font-bold text-dark-100">
                              {selectedUserFull.hwid_devices.devices.length}
                            </div>
                          </div>
                        )}
                      </div>
                    </div>
                    
                    {/* Device List */}
                    {selectedUserFull?.hwid_devices?.devices && selectedUserFull.hwid_devices.devices.length > 0 ? (
                      <div className="space-y-3">
                        {selectedUserFull.hwid_devices.devices.map((device, idx) => (
                          <div key={device.hwid} className="p-4 rounded-lg bg-dark-800">
                            <div className="flex items-start gap-3">
                              <Smartphone className="w-5 h-5 text-dark-500 mt-0.5" />
                              <div className="flex-1 min-w-0">
                                <div className="flex items-center gap-2 flex-wrap">
                                  <span className="text-dark-200 font-medium">
                                    {device.deviceModel || device.platform || `Device ${idx + 1}`}
                                  </span>
                                  {device.platform && (
                                    <span className="px-2 py-0.5 rounded text-xs bg-accent-500/20 text-accent-400">
                                      {device.platform}
                                    </span>
                                  )}
                                  {device.osVersion && (
                                    <span className="px-2 py-0.5 rounded text-xs bg-dark-700 text-dark-300">
                                      {device.osVersion}
                                    </span>
                                  )}
                                </div>
                                <div className="text-dark-500 text-xs font-mono mt-1 truncate" title={device.hwid}>
                                  HWID: {device.hwid}
                                </div>
                                {device.userAgent && (
                                  <div className="text-dark-600 text-xs mt-1 truncate" title={device.userAgent}>
                                    {device.userAgent}
                                  </div>
                                )}
                                <div className="flex items-center gap-4 mt-2 text-xs text-dark-500">
                                  <span>{t('remnawave.added')}: {formatDateTime(device.createdAt)}</span>
                                  <span>{t('remnawave.last_used')}: {formatDateTime(device.updatedAt)}</span>
                                </div>
                              </div>
                            </div>
                          </div>
                        ))}
                      </div>
                    ) : !selectedUserFull?.hwid_devices ? (
                      <div className="p-6 rounded-lg bg-dark-800 text-center">
                        <Smartphone className="w-8 h-8 text-dark-600 mx-auto mb-2" />
                        <p className="text-dark-500">{t('remnawave.click_refresh_for_devices')}</p>
                        <button
                          onClick={handleLoadLiveUserInfo}
                          disabled={isLoadingUserFull}
                          className="mt-3 px-4 py-2 rounded-lg bg-accent-500 hover:bg-accent-600 text-white 
                                   text-sm transition-colors disabled:opacity-50"
                        >
                          {isLoadingUserFull ? t('common.loading') : t('remnawave.load_live_data')}
                        </button>
                      </div>
                    ) : (
                      <div className="p-6 rounded-lg bg-dark-800 text-center">
                        <Smartphone className="w-8 h-8 text-dark-600 mx-auto mb-2" />
                        <p className="text-dark-500">{t('remnawave.no_devices')}</p>
                      </div>
                    )}
                  </div>
                )}
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
      
      {/* Clear Stats Confirmation Modal */}
      <AnimatePresence>
        {showClearConfirm && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm"
            onClick={() => setShowClearConfirm(false)}
          >
            <motion.div
              initial={{ scale: 0.95, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              exit={{ scale: 0.95, opacity: 0 }}
              className="w-full max-w-md bg-dark-900 rounded-xl border border-dark-700 p-6"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="flex items-center gap-3 mb-4">
                <div className="p-2 rounded-lg bg-danger/20">
                  <AlertCircle className="w-6 h-6 text-danger" />
                </div>
                <h3 className="text-lg font-semibold text-dark-100">
                  {t('remnawave.confirm_clear_title')}
                </h3>
              </div>
              
              <p className="text-dark-300 mb-4">
                {t('remnawave.confirm_clear_desc')}
              </p>
              
              {dbInfo && (
                <div className="p-3 rounded-lg bg-dark-800 mb-4">
                  <div className="text-dark-400 text-sm mb-2">{t('remnawave.will_be_deleted')}:</div>
                  <ul className="text-dark-300 text-sm space-y-1">
                    <li>• {dbInfo.tables.xray_visit_stats.count.toLocaleString()} {t('remnawave.db_visits').toLowerCase()}</li>
                    <li>• {dbInfo.tables.xray_user_ip_stats.count.toLocaleString()} {t('remnawave.db_ips').toLowerCase()}</li>
                    <li>• {dbInfo.tables.xray_ip_destination_stats.count.toLocaleString()} {t('remnawave.db_ip_destinations').toLowerCase()}</li>
                    <li>• {dbInfo.tables.xray_hourly_stats.count.toLocaleString()} {t('remnawave.db_hourly').toLowerCase()}</li>
                  </ul>
                </div>
              )}
              
              <div className="flex items-center gap-3 justify-end">
                <motion.button
                  onClick={() => setShowClearConfirm(false)}
                  className="px-4 py-2 rounded-lg bg-dark-700 hover:bg-dark-600 text-dark-200 transition-colors"
                  whileHover={{ scale: 1.02 }}
                  whileTap={{ scale: 0.98 }}
                >
                  {t('common.cancel')}
                </motion.button>
                <motion.button
                  onClick={handleClearStats}
                  disabled={isClearingStats}
                  className="px-4 py-2 rounded-lg bg-danger hover:bg-danger/80 text-white 
                           transition-colors disabled:opacity-50 flex items-center gap-2"
                  whileHover={{ scale: 1.02 }}
                  whileTap={{ scale: 0.98 }}
                >
                  {isClearingStats ? (
                    <RefreshCw className="w-4 h-4 animate-spin" />
                  ) : (
                    <Trash2 className="w-4 h-4" />
                  )}
                  {t('remnawave.confirm_clear_btn')}
                </motion.button>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}
