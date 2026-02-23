import { useState, useEffect, useCallback, useRef, useMemo, memo, startTransition } from 'react'
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
  ChevronDown,
  Download,
  Shield,
  Eye,
  EyeOff,
  Send
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
  RemnawaveInfrastructureAddress,
  RemnawaveExcludedDestination,
  IgnoredUser
} from '../api/client'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import PeriodSelector from '../components/ui/PeriodSelector'
import { Skeleton } from '../components/ui/Skeleton'
import { Checkbox } from '../components/ui/Checkbox'
import { useAutoRefresh } from '../hooks/useAutoRefresh'

type TabType = 'overview' | 'users' | 'destinations' | 'analyzer' | 'export' | 'settings'
type UserSortField = 'email' | 'username' | 'status' | 'total_visits' | 'unique_sites' | 'unique_ips'

const RefreshCountdown = memo(function RefreshCountdown({ intervalMs }: { intervalMs: number }) {
  const [secondsLeft, setSecondsLeft] = useState(Math.round(intervalMs / 1000))

  useEffect(() => {
    setSecondsLeft(Math.round(intervalMs / 1000))
    const id = setInterval(() => {
      setSecondsLeft(prev => (prev <= 1 ? Math.round(intervalMs / 1000) : prev - 1))
    }, 1000)
    return () => clearInterval(id)
  }, [intervalMs])

  return (
    <div className="flex items-center gap-2 text-dark-500 text-sm">
      <RefreshCw className="w-4 h-4" />
      <span>{secondsLeft}s</span>
    </div>
  )
})

const CollectorCountdown = memo(function CollectorCountdown({
  initialSeconds,
  onExpire,
}: {
  initialSeconds: number
  onExpire: () => void
}) {
  const [seconds, setSeconds] = useState(initialSeconds)

  useEffect(() => {
    setSeconds(initialSeconds)
  }, [initialSeconds])

  useEffect(() => {
    if (seconds <= 0) return
    const id = setInterval(() => {
      setSeconds(prev => {
        if (prev <= 1) {
          onExpire()
          return 0
        }
        return prev - 1
      })
    }, 1000)
    return () => clearInterval(id)
  }, [seconds > 0]) // eslint-disable-line react-hooks/exhaustive-deps

  if (seconds <= 0) return null

  return (
    <div className="flex items-center gap-2 text-dark-400">
      <RefreshCw className="w-4 h-4" />
      <span className="text-sm">{seconds}s</span>
    </div>
  )
})

const SortableHeader = memo(function SortableHeader({
  field,
  children,
  align = 'left',
  currentField,
  direction,
  onSort,
}: {
  field: UserSortField
  children: React.ReactNode
  align?: 'left' | 'right'
  currentField: UserSortField
  direction: 'asc' | 'desc'
  onSort: (field: UserSortField) => void
}) {
  return (
    <th
      className={`${align === 'right' ? 'text-right' : 'text-left'} p-4 text-dark-400 font-medium text-sm cursor-pointer hover:text-dark-200 transition-colors select-none`}
      onClick={() => onSort(field)}
    >
      <div className={`flex items-center gap-1 ${align === 'right' ? 'justify-end' : ''}`}>
        {children}
        <span className="flex flex-col">
          <ChevronUp className={`w-3 h-3 -mb-1 ${currentField === field && direction === 'asc' ? 'text-accent-400' : 'text-dark-600'}`} />
          <ChevronDown className={`w-3 h-3 ${currentField === field && direction === 'desc' ? 'text-accent-400' : 'text-dark-600'}`} />
        </span>
      </div>
    </th>
  )
})

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
  
  // Excluded destinations state
  const [excludedDestinations, setExcludedDestinations] = useState<RemnawaveExcludedDestination[]>([])
  const [newExcludedDest, setNewExcludedDest] = useState('')
  const [newExcludedDestDescription, setNewExcludedDestDescription] = useState('')
  const [isAddingExcludedDest, setIsAddingExcludedDest] = useState(false)
  
  // Ignored users state
  const [ignoredUsers, setIgnoredUsers] = useState<IgnoredUser[]>([])
  const [newIgnoredUserId, setNewIgnoredUserId] = useState('')
  const [isAddingIgnoredUser, setIsAddingIgnoredUser] = useState(false)
  const [lastRescanResult, setLastRescanResult] = useState<{ updated_to_infrastructure: number; updated_to_client: number } | null>(null)
  
  // Collector status state
  const [collectorStatus, setCollectorStatus] = useState<RemnawaveCollectorStatus | null>(null)
  const [isCollecting, setIsCollecting] = useState(false)
  
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
  
  // ASN group expansion
  const [expandedAsns, setExpandedAsns] = useState<Set<string>>(new Set())
  
  // IP clearing state
  const [isClearingIp, setIsClearingIp] = useState(false)
  const [clearIpConfirm, setClearIpConfirm] = useState<{ type: 'single' | 'all'; sourceIp?: string } | null>(null)
  
  // Global client IPs clearing
  const [isClearingAllClientIps, setIsClearingAllClientIps] = useState(false)
  const [showClearAllClientIpsConfirm, setShowClearAllClientIpsConfirm] = useState(false)
  
  // DB info state
  const [dbInfo, setDbInfo] = useState<{
    tables: {
      xray_stats: { count: number; first_seen: string | null; last_seen: string | null; size_bytes?: number | null }
      xray_hourly_stats: { count: number; first_hour: string | null; last_hour: string | null; size_bytes?: number | null }
      remnawave_user_cache: { count: number; size_bytes?: number | null }
    }
    total_size_bytes?: number | null
  } | null>(null)
  const [isClearingStats, setIsClearingStats] = useState(false)
  const [showClearConfirm, setShowClearConfirm] = useState(false)
  
  // Export state
  const [exportPeriod, setExportPeriod] = useState('all')
  const [exportSettings, setExportSettings] = useState({
    include_user_id: true,
    include_username: true,
    include_status: true,
    include_telegram_id: false,
    include_destinations: true,
    include_visits_count: true,
    include_first_seen: true,
    include_last_seen: true,
    include_client_ips: false,
    include_infra_ips: false,
    include_traffic: false
  })
  const [isExporting, setIsExporting] = useState(false)
  const [exports, setExports] = useState<Array<{
    id: number
    filename: string
    format: string
    status: string
    file_size: number | null
    rows_count: number | null
    error_message: string | null
    created_at: string | null
    completed_at: string | null
  }>>([])
  const [isLoadingExports, setIsLoadingExports] = useState(false)
  
  // Analyzer state
  const [analyzerSettings, setAnalyzerSettings] = useState<{
    enabled: boolean
    check_interval_minutes: number
    traffic_limit_gb: number
    ip_limit_multiplier: number
    check_hwid_anomalies: boolean
    telegram_bot_token: string | null
    telegram_chat_id: string | null
    last_check_at: string | null
    last_error: string | null
  } | null>(null)
  const [editAnalyzerSettings, setEditAnalyzerSettings] = useState<{
    enabled?: boolean
    check_interval_minutes?: number
    traffic_limit_gb?: number
    ip_limit_multiplier?: number
    check_hwid_anomalies?: boolean
    telegram_bot_token?: string
    telegram_chat_id?: string
  }>({})
  const [isSavingAnalyzer, setIsSavingAnalyzer] = useState(false)
  const [isTestingTelegram, setIsTestingTelegram] = useState(false)
  const [telegramTestResult, setTelegramTestResult] = useState<{ success: boolean; error?: string } | null>(null)
  const [, setAnalyzerStatus] = useState<{
    running: boolean
    analyzing: boolean
    check_interval: number
    last_check_time: string | null
    next_check_in: number | null
  } | null>(null)
  const [isRunningCheck, setIsRunningCheck] = useState(false)
  const [anomalies, setAnomalies] = useState<Array<{
    id: number
    user_email: number
    username: string | null
    telegram_id: number | null
    anomaly_type: string
    severity: string
    details: Record<string, unknown> | null
    notified: boolean
    resolved: boolean
    created_at: string | null
  }>>([])
  const [anomaliesTotal, setAnomaliesTotal] = useState(0)
  const [, setAnomaliesOffset] = useState(0)
  const [isLoadingAnomalies, setIsLoadingAnomalies] = useState(false)
  const [anomalyFilter, setAnomalyFilter] = useState<'all' | 'active' | 'resolved'>('active')
  const [anomalyTypeFilter, setAnomalyTypeFilter] = useState<string>('')
  const [showTelegramToken, setShowTelegramToken] = useState(false)
  
  // Track which data has been loaded
  const [statsLoaded, setStatsLoaded] = useState(false)
  const [settingsDataLoaded, setSettingsDataLoaded] = useState(false)
  
  // Fetch basic settings (always needed on page load)
  // Uses Promise.allSettled to handle partial failures gracefully
  const fetchBasicSettings = useCallback(async () => {
    const [settingsRes, nodesRes, statusRes] = await Promise.allSettled([
      remnawaveApi.getSettings(),
      remnawaveApi.getNodes(),
      remnawaveApi.getCollectorStatus()
    ])
    
    if (settingsRes.status === 'fulfilled') {
      const data = settingsRes.value.data
      setSettings(data)
      setEditSettings({
        api_url: data.api_url || '',
        api_token: '',
        cookie_secret: '',
        enabled: data.enabled,
        collection_interval: data.collection_interval,
        visit_stats_retention_days: data.visit_stats_retention_days,
        ip_stats_retention_days: data.ip_stats_retention_days,
        ip_destination_retention_days: data.ip_destination_retention_days,
        hourly_stats_retention_days: data.hourly_stats_retention_days
      })
    }
    
    if (nodesRes.status === 'fulfilled') {
      setNodes(nodesRes.value.data.nodes)
      setAllServers(nodesRes.value.data.all_servers)
      const nodeIds = new Set(nodesRes.value.data.all_servers.filter(s => s.is_node).map(s => s.id))
      setSelectedNodeIds(nodeIds)
    }
    
    if (statusRes.status === 'fulfilled') {
      setCollectorStatus(statusRes.value.data)
    }
  }, [])
  
  // Fetch settings tab data (lazy loaded when settings tab is opened)
  const fetchSettingsTabData = useCallback(async () => {
    if (settingsDataLoaded) return
    
    const [dbInfoRes, infraRes, excludedRes, cacheStatusRes, ignoredRes] = await Promise.allSettled([
      remnawaveApi.getDbInfo(),
      remnawaveApi.getInfrastructureAddresses(),
      remnawaveApi.getExcludedDestinations(),
      remnawaveApi.getUserCacheStatus(),
      remnawaveApi.getIgnoredUsers()
    ])
    
    if (dbInfoRes.status === 'fulfilled') setDbInfo(dbInfoRes.value.data)
    if (infraRes.status === 'fulfilled') setInfrastructureAddresses(infraRes.value.data.addresses)
    if (excludedRes.status === 'fulfilled') setExcludedDestinations(excludedRes.value.data.destinations)
    if (cacheStatusRes.status === 'fulfilled') setUserCacheStatus(cacheStatusRes.value.data)
    if (ignoredRes.status === 'fulfilled') setIgnoredUsers(ignoredRes.value.data.ignored_users)
    
    setSettingsDataLoaded(true)
  }, [settingsDataLoaded])
  
  // Legacy fetchSettings for refresh button compatibility  
  const fetchSettings = useCallback(async () => {
    await fetchBasicSettings()
    if (activeTab === 'settings') {
      setSettingsDataLoaded(false) // Force reload
      await fetchSettingsTabData()
    }
  }, [fetchBasicSettings, fetchSettingsTabData, activeTab])
  
  // Fetch stats via single batch endpoint (1 HTTP request instead of 3)
  const fetchStats = useCallback(async () => {
    try {
      const res = await remnawaveApi.getStatsBatch({
        period,
        dest_limit: 100,
        users_limit: USERS_PAGE_SIZE,
        search: userSearch || undefined
      })
      const data = res.data
      setSummary(data.summary)
      setTopDestinations(data.destinations)
      setTopUsers(data.users.users)
      setTotalUsers(data.users.total)
      setUsersOffset(data.users.users.length)
      setError(null)
      return true
    } catch {
      setError(t('remnawave.failed_fetch'))
      return false
    }
  }, [period, t, userSearch])
  
  // Fetch analyzer settings and anomalies
  const fetchAnalyzerData = useCallback(async () => {
    setIsLoadingAnomalies(true)
    
    const [settingsRes, statusRes, anomaliesRes] = await Promise.allSettled([
      remnawaveApi.getAnalyzerSettings(),
      remnawaveApi.getAnalyzerStatus(),
      remnawaveApi.getAnomalies({ 
        limit: 50, 
        offset: 0,
        resolved: anomalyFilter === 'resolved' ? true : anomalyFilter === 'active' ? false : undefined,
        anomaly_type: anomalyTypeFilter || undefined
      })
    ])
    
    if (settingsRes.status === 'fulfilled') {
      setAnalyzerSettings(settingsRes.value.data)
      setEditAnalyzerSettings({
        enabled: settingsRes.value.data.enabled,
        check_interval_minutes: settingsRes.value.data.check_interval_minutes,
        traffic_limit_gb: settingsRes.value.data.traffic_limit_gb,
        ip_limit_multiplier: settingsRes.value.data.ip_limit_multiplier,
        check_hwid_anomalies: settingsRes.value.data.check_hwid_anomalies,
        telegram_bot_token: '',
        telegram_chat_id: settingsRes.value.data.telegram_chat_id || ''
      })
    }
    
    if (statusRes.status === 'fulfilled') setAnalyzerStatus(statusRes.value.data)
    
    if (anomaliesRes.status === 'fulfilled') {
      setAnomalies(anomaliesRes.value.data.anomalies)
      setAnomaliesTotal(anomaliesRes.value.data.total)
      setAnomaliesOffset(anomaliesRes.value.data.anomalies.length)
    }
    
    setIsLoadingAnomalies(false)
  }, [anomalyFilter, anomalyTypeFilter])
  
  // Save analyzer settings
  const handleSaveAnalyzerSettings = async () => {
    setIsSavingAnalyzer(true)
    try {
      const dataToSave: Record<string, unknown> = {}
      if (editAnalyzerSettings.enabled !== undefined) dataToSave.enabled = editAnalyzerSettings.enabled
      if (editAnalyzerSettings.check_interval_minutes !== undefined) dataToSave.check_interval_minutes = editAnalyzerSettings.check_interval_minutes
      if (editAnalyzerSettings.traffic_limit_gb !== undefined) dataToSave.traffic_limit_gb = editAnalyzerSettings.traffic_limit_gb
      if (editAnalyzerSettings.ip_limit_multiplier !== undefined) dataToSave.ip_limit_multiplier = editAnalyzerSettings.ip_limit_multiplier
      if (editAnalyzerSettings.check_hwid_anomalies !== undefined) dataToSave.check_hwid_anomalies = editAnalyzerSettings.check_hwid_anomalies
      if (editAnalyzerSettings.telegram_bot_token) dataToSave.telegram_bot_token = editAnalyzerSettings.telegram_bot_token
      if (editAnalyzerSettings.telegram_chat_id !== undefined) dataToSave.telegram_chat_id = editAnalyzerSettings.telegram_chat_id
      
      await remnawaveApi.updateAnalyzerSettings(dataToSave)
      await fetchAnalyzerData()
      toast.success(t('common.saved'))
    } catch (err) {
      console.error('Failed to save analyzer settings:', err)
      toast.error(t('common.action_failed'))
    } finally {
      setIsSavingAnalyzer(false)
    }
  }
  
  // Test Telegram notification
  const handleTestTelegram = async () => {
    const token = editAnalyzerSettings.telegram_bot_token || (analyzerSettings?.telegram_bot_token === '***' ? '' : analyzerSettings?.telegram_bot_token)
    const chatId = editAnalyzerSettings.telegram_chat_id || analyzerSettings?.telegram_chat_id
    
    if (!token || !chatId) {
      setTelegramTestResult({ success: false, error: 'Bot token and chat ID required' })
      return
    }
    
    setIsTestingTelegram(true)
    setTelegramTestResult(null)
    try {
      const res = await remnawaveApi.testTelegram(token, chatId)
      setTelegramTestResult(res.data)
      if (res.data.success) {
        toast.success(t('common.test_success') || 'Test successful')
      } else {
        toast.error(res.data.error || t('common.action_failed'))
      }
    } catch (err) {
      setTelegramTestResult({ success: false, error: 'Failed to test' })
      toast.error(t('common.action_failed'))
    } finally {
      setIsTestingTelegram(false)
    }
  }
  
  // Run manual analyzer check
  const handleRunAnalyzerCheck = async () => {
    setIsRunningCheck(true)
    try {
      await remnawaveApi.runAnalyzerCheck()
      await fetchAnalyzerData()
      toast.success(t('common.action_success') || 'Check completed')
    } catch (err) {
      console.error('Failed to run analyzer check:', err)
      toast.error(t('common.action_failed'))
    } finally {
      setIsRunningCheck(false)
    }
  }
  
  // Delete single anomaly
  const handleDeleteAnomaly = async (anomalyId: number) => {
    try {
      await remnawaveApi.deleteAnomaly(anomalyId)
      setAnomalies(prev => prev.filter(a => a.id !== anomalyId))
      setAnomaliesTotal(prev => Math.max(0, prev - 1))
      toast.success(t('common.deleted'))
    } catch (err) {
      console.error('Failed to delete anomaly:', err)
      toast.error(t('common.action_failed'))
    }
  }
  
  // Delete all anomalies
  const handleDeleteAllAnomalies = async () => {
    if (!window.confirm(t('remnawave.confirm_delete_all_anomalies'))) return
    try {
      await remnawaveApi.deleteAllAnomalies()
      setAnomalies([])
      setAnomaliesTotal(0)
      toast.success(t('common.deleted'))
    } catch (err) {
      console.error('Failed to delete all anomalies:', err)
      toast.error(t('common.action_failed'))
    }
  }
  
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
  
  // Initial load - basic settings with retry on failure
  useEffect(() => {
    let retryTimer: ReturnType<typeof setTimeout> | null = null
    const loadData = async () => {
      setIsLoading(true)
      await fetchBasicSettings()
      setIsLoading(false)
      
      // Retry once after 2s if settings failed to load (settings will be null)
      if (!settings) {
        retryTimer = setTimeout(async () => {
          await fetchBasicSettings()
        }, 2000)
      }
    }
    loadData()
    return () => { if (retryTimer) clearTimeout(retryTimer) }
  }, [fetchBasicSettings]) // eslint-disable-line react-hooks/exhaustive-deps
  
  // Lazy load stats when needed (overview, users, destinations tabs)
  // Auto-retries up to 2 times with 2s delay if initial load has errors
  const statsRetryRef = useRef(0)
  const statsRetryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    const statsNeeded = activeTab === 'overview' || activeTab === 'users' || activeTab === 'destinations'
    if (statsNeeded && !isLoading && !statsLoaded) {
      const attemptLoad = async () => {
        const success = await fetchStats()
        if (success) {
          setStatsLoaded(true)
          statsRetryRef.current = 0
        } else if (statsRetryRef.current < 2) {
          statsRetryRef.current++
          statsRetryTimerRef.current = setTimeout(async () => {
            const retrySuccess = await fetchStats()
            if (retrySuccess || statsRetryRef.current >= 2) {
              setStatsLoaded(true)
              statsRetryRef.current = 0
            } else {
              statsRetryRef.current++
              statsRetryTimerRef.current = setTimeout(async () => {
                await fetchStats()
                setStatsLoaded(true)
                statsRetryRef.current = 0
              }, 3000)
            }
          }, 2000)
        } else {
          setStatsLoaded(true)
          statsRetryRef.current = 0
        }
      }
      attemptLoad()
    }
    return () => {
      if (statsRetryTimerRef.current) clearTimeout(statsRetryTimerRef.current)
    }
  }, [activeTab, isLoading, statsLoaded, fetchStats])
  
  // Reload stats when period changes (only if stats tab is active)
  useEffect(() => {
    const statsNeeded = activeTab === 'overview' || activeTab === 'users' || activeTab === 'destinations'
    if (!isLoading && statsLoaded && statsNeeded) {
      fetchStats()
    }
  }, [period]) // eslint-disable-line react-hooks/exhaustive-deps
  
  // Fetch settings tab data when settings tab is selected
  useEffect(() => {
    if (activeTab === 'settings') {
      fetchSettingsTabData()
    }
  }, [activeTab, fetchSettingsTabData])
  
  // Fetch analyzer data when tab is selected
  useEffect(() => {
    if (activeTab === 'analyzer') {
      fetchAnalyzerData()
    }
  }, [activeTab, fetchAnalyzerData])
  
  const handleCollectorExpire = useCallback(() => {
    remnawaveApi.getCollectorStatus().then(res => {
      setCollectorStatus(res.data)
    })
  }, [])

  useAutoRefresh(() => { fetchStats() }, {
    customInterval: 60000,
    immediate: false,
    pauseWhenHidden: true,
    refreshOnVisible: true
  })
  
  const handleRefresh = async () => {
    setIsRefreshing(true)
    // Refresh data based on active tab
    if (activeTab === 'overview' || activeTab === 'users' || activeTab === 'destinations') {
      await fetchStats()
    } else if (activeTab === 'settings') {
      setSettingsDataLoaded(false)
      await fetchSettingsTabData()
    } else if (activeTab === 'analyzer') {
      await fetchAnalyzerData()
    }
    setIsRefreshing(false)
  }
  
  const handleRefreshUserCache = async () => {
    setIsRefreshingUserCache(true)
    try {
      const res = await remnawaveApi.refreshUserCache()
      if (res.data.success) {
        // Refresh stats to show updated statuses
        await Promise.all([fetchStats(), remnawaveApi.getUserCacheStatus().then(r => setUserCacheStatus(r.data))])
        toast.success(t('common.action_success') || 'Cache refreshed')
      }
    } catch (err) {
      console.error('Failed to refresh user cache:', err)
      toast.error(t('common.action_failed'))
    } finally {
      setIsRefreshingUserCache(false)
    }
  }
  
  const handleClearAllClientIps = async () => {
    setIsClearingAllClientIps(true)
    try {
      const res = await remnawaveApi.clearAllClientIps()
      if (res.data.success) {
        await fetchStats()
        toast.success(t('common.action_success') || 'IPs cleared')
      }
    } catch (err) {
      console.error('Failed to clear all client IPs:', err)
      toast.error(t('common.action_failed'))
    } finally {
      setIsClearingAllClientIps(false)
      setShowClearAllClientIpsConfirm(false)
    }
  }
  
  const handleSaveSettings = async () => {
    setIsSavingSettings(true)
    try {
      const dataToSave: Partial<RemnawaveSettings> = {
        enabled: editSettings.enabled,
        collection_interval: editSettings.collection_interval,
        visit_stats_retention_days: editSettings.visit_stats_retention_days,
        ip_stats_retention_days: editSettings.ip_stats_retention_days,
        ip_destination_retention_days: editSettings.ip_destination_retention_days,
        hourly_stats_retention_days: editSettings.hourly_stats_retention_days
      }
      if (editSettings.api_url) dataToSave.api_url = editSettings.api_url
      if (editSettings.api_token) dataToSave.api_token = editSettings.api_token
      if (editSettings.cookie_secret) dataToSave.cookie_secret = editSettings.cookie_secret
      
      await remnawaveApi.updateSettings(dataToSave)
      await fetchSettings()
      setTestResult(null)
      toast.success(t('common.saved'))
    } catch (err) {
      console.error('Failed to save settings:', err)
      toast.error(t('common.action_failed'))
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
      if (res.data.success) {
        toast.success(t('common.test_success') || 'Connection successful')
      } else {
        toast.error(res.data.error || t('common.action_failed'))
      }
    } catch (err) {
      setTestResult({ success: false, error: 'Connection failed' })
      toast.error(t('common.action_failed'))
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
      toast.success(t('common.action_success') || 'Rescan completed')
    } catch (err) {
      console.error('Failed to rescan infrastructure IPs:', err)
      toast.error(t('common.action_failed'))
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
      toast.success(t('common.added'))
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } }
      console.error('Failed to add infrastructure address:', error.response?.data?.detail || err)
      toast.error(error.response?.data?.detail || t('common.action_failed'))
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
      toast.success(t('common.deleted'))
    } catch (err) {
      console.error('Failed to delete infrastructure address:', err)
      toast.error(t('common.action_failed'))
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
      toast.success(t('common.action_success') || 'Addresses resolved')
    } catch (err) {
      console.error('Failed to resolve infrastructure addresses:', err)
      toast.error(t('common.action_failed'))
    } finally {
      setIsResolvingInfra(false)
    }
  }
  
  // Excluded destinations handlers
  const handleAddExcludedDest = async () => {
    if (!newExcludedDest.trim()) return
    
    setIsAddingExcludedDest(true)
    try {
      await remnawaveApi.addExcludedDestination(
        newExcludedDest.trim(),
        newExcludedDestDescription.trim() || undefined
      )
      setNewExcludedDest('')
      setNewExcludedDestDescription('')
      // Refresh excluded destinations
      const res = await remnawaveApi.getExcludedDestinations()
      setExcludedDestinations(res.data.destinations)
      toast.success(t('common.added'))
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } }
      console.error('Failed to add excluded destination:', error.response?.data?.detail || err)
      toast.error(error.response?.data?.detail || t('common.action_failed'))
    } finally {
      setIsAddingExcludedDest(false)
    }
  }
  
  const handleDeleteExcludedDest = async (id: number) => {
    try {
      await remnawaveApi.deleteExcludedDestination(id)
      // Refresh excluded destinations
      const res = await remnawaveApi.getExcludedDestinations()
      setExcludedDestinations(res.data.destinations)
      toast.success(t('common.deleted'))
    } catch (err) {
      console.error('Failed to delete excluded destination:', err)
      toast.error(t('common.action_failed'))
    }
  }
  
  // Ignored users handlers
  const handleAddIgnoredUser = async () => {
    const userId = parseInt(newIgnoredUserId.trim())
    if (isNaN(userId) || userId <= 0) return
    
    setIsAddingIgnoredUser(true)
    try {
      const res = await remnawaveApi.addIgnoredUser(userId)
      if (res.data.success) {
        setNewIgnoredUserId('')
        // Refresh ignored users
        const ignoredRes = await remnawaveApi.getIgnoredUsers()
        setIgnoredUsers(ignoredRes.data.ignored_users)
        toast.success(t('common.added'))
      } else {
        console.error('Failed to add ignored user:', res.data.error)
        toast.error(res.data.error || t('common.action_failed'))
      }
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } }
      console.error('Failed to add ignored user:', error.response?.data?.detail || err)
      toast.error(error.response?.data?.detail || t('common.action_failed'))
    } finally {
      setIsAddingIgnoredUser(false)
    }
  }
  
  const handleRemoveIgnoredUser = async (userId: number) => {
    try {
      await remnawaveApi.removeIgnoredUser(userId)
      // Refresh ignored users
      const ignoredRes = await remnawaveApi.getIgnoredUsers()
      setIgnoredUsers(ignoredRes.data.ignored_users)
      toast.success(t('common.deleted'))
    } catch (err) {
      console.error('Failed to remove ignored user:', err)
      toast.error(t('common.action_failed'))
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
    setSelectedNodeIds(new Set(allServers.filter(s => s.has_xray_node).map(s => s.id)))
  }
  
  const handleDeselectAllNodes = () => {
    setSelectedNodeIds(new Set())
  }
  
  const handleSyncNodes = async () => {
    setIsSyncingNodes(true)
    try {
      await remnawaveApi.syncNodes(Array.from(selectedNodeIds))
      await fetchSettings()
      toast.success(t('common.action_success') || 'Nodes synced')
    } catch (err) {
      console.error('Failed to sync nodes:', err)
      toast.error(t('common.action_failed'))
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
        toast.success(t('common.action_success') || 'Collection started')
      }
    } catch (err) {
      console.error('Failed to force collect:', err)
      toast.error(t('common.action_failed'))
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
      toast.success(t('common.action_success') || 'Stats cleared')
    } catch (err) {
      console.error('Failed to clear stats:', err)
      toast.error(t('common.action_failed'))
    } finally {
      setIsClearingStats(false)
    }
  }
  
  const fetchExports = useCallback(async () => {
    setIsLoadingExports(true)
    try {
      const res = await remnawaveApi.listExports()
      setExports(res.data.exports)
    } catch (err) {
      console.error('Failed to fetch exports:', err)
    } finally {
      setIsLoadingExports(false)
    }
  }, [])
  
  const handleCreateExport = async () => {
    setIsExporting(true)
    try {
      await remnawaveApi.createExport({
        period: exportPeriod,
        ...exportSettings
      })
      // Refresh exports list
      await fetchExports()
      toast.success(t('common.action_success') || 'Export created')
    } catch (err) {
      console.error('Failed to create export:', err)
      toast.error(t('common.action_failed'))
    } finally {
      setIsExporting(false)
    }
  }
  
  const handleDownloadExport = async (exportId: number, filename: string) => {
    try {
      const response = await remnawaveApi.downloadExport(exportId)
      const blob = new Blob([response.data])
      const url = window.URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      window.URL.revokeObjectURL(url)
      document.body.removeChild(a)
    } catch (err) {
      console.error('Failed to download export:', err)
    }
  }
  
  const handleDeleteExport = async (exportId: number) => {
    try {
      await remnawaveApi.deleteExport(exportId)
      await fetchExports()
      toast.success(t('common.deleted'))
    } catch (err) {
      console.error('Failed to delete export:', err)
      toast.error(t('common.action_failed'))
    }
  }
  
  // Fetch exports when switching to export tab
  useEffect(() => {
    if (activeTab === 'export') {
      fetchExports()
    }
  }, [activeTab, fetchExports])
  
  // Auto-refresh exports while there are pending/processing tasks
  useEffect(() => {
    if (activeTab !== 'export') return
    
    const hasPending = exports.some(e => e.status === 'pending' || e.status === 'processing')
    if (!hasPending) return
    
    const interval = setInterval(fetchExports, 2000)
    return () => clearInterval(interval)
  }, [activeTab, exports, fetchExports])
  
  // Check if selected nodes differ from current nodes
  const currentNodeIds = useMemo(() => new Set(allServers.filter(s => s.is_node).map(s => s.id)), [allServers])
  const hasNodeChanges = useMemo(() =>
    selectedNodeIds.size !== currentNodeIds.size || 
    ![...selectedNodeIds].every(id => currentNodeIds.has(id))
  , [selectedNodeIds, currentNodeIds])
  
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
  
  // Clear single user IP
  const handleClearUserIp = async (email: number, sourceIp: string) => {
    setIsClearingIp(true)
    try {
      await remnawaveApi.clearUserIp(email, sourceIp)
      // Refresh user stats to update the IPs list
      const res = await remnawaveApi.getUserStats(email, period)
      setSelectedUser(res.data)
      // Close expanded IP if it was the deleted one
      if (expandedIp === sourceIp) {
        setExpandedIp(null)
        setIpDestinations(null)
      }
      toast.success(t('common.action_success') || 'IP cleared')
    } catch (err) {
      console.error('Failed to clear user IP:', err)
      toast.error(t('common.action_failed'))
    } finally {
      setIsClearingIp(false)
      setClearIpConfirm(null)
    }
  }
  
  // Clear all user IPs
  const handleClearUserAllIps = async (email: number) => {
    setIsClearingIp(true)
    try {
      await remnawaveApi.clearUserAllIps(email)
      // Refresh user stats to update the IPs list
      const res = await remnawaveApi.getUserStats(email, period)
      setSelectedUser(res.data)
      // Close any expanded IP
      setExpandedIp(null)
      setIpDestinations(null)
      toast.success(t('common.action_success') || 'All IPs cleared')
    } catch (err) {
      console.error('Failed to clear all user IPs:', err)
      toast.error(t('common.action_failed'))
    } finally {
      setIsClearingIp(false)
      setClearIpConfirm(null)
    }
  }
  
  const filteredDestUsers = useMemo(() =>
    selectedDestination?.users.filter(u =>
      destUserSearch 
        ? (u.username?.toLowerCase().includes(destUserSearch.toLowerCase()) ||
           u.email.toString().includes(destUserSearch))
        : true
    ) || []
  , [selectedDestination, destUserSearch])
  
  const filteredDestinations = useMemo(() =>
    topDestinations.filter(d => 
      destSearch ? d.destination.toLowerCase().includes(destSearch.toLowerCase()) : true
    )
  , [topDestinations, destSearch])
  
  const filteredUsers = useMemo(() => {
    const filtered = topUsers.filter(u =>
      userSearch ? (u.username?.toLowerCase().includes(userSearch.toLowerCase()) ||
                    u.email.toString().includes(userSearch)) : true
    )
    const direction = userSortDirection === 'asc' ? 1 : -1
    return filtered.sort((a, b) => {
      switch (userSortField) {
        case 'email':
          return (a.email - b.email) * direction
        case 'username': {
          const nameA = a.username || ''
          const nameB = b.username || ''
          return nameA.localeCompare(nameB) * direction
        }
        case 'status': {
          const statusA = a.status || ''
          const statusB = b.status || ''
          return statusA.localeCompare(statusB) * direction
        }
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
  }, [topUsers, userSearch, userSortField, userSortDirection])
  
  // Handle sort click
  const handleUserSort = (field: UserSortField) => {
    if (userSortField === field) {
      setUserSortDirection(prev => prev === 'asc' ? 'desc' : 'asc')
    } else {
      setUserSortField(field)
      setUserSortDirection('desc')
    }
  }
  
  const { barChartData, maxVisits, totalVisits } = useMemo(() => {
    const data = topDestinations.slice(0, 10)
    return {
      barChartData: data,
      maxVisits: Math.max(...data.map(d => d.visits), 1),
      totalVisits: data.reduce((sum, d) => sum + d.visits, 0),
    }
  }, [topDestinations])

  const getIpInfoUrl = useCallback((destination: string) => {
    const host = destination.split(':')[0]
    return `https://check-host.net/ip-info?host=${encodeURIComponent(host)}`
  }, [])
  
  const tabs = useMemo<{ id: TabType; label: string; icon: React.ReactNode }[]>(() => [
    { id: 'overview', label: t('remnawave.overview'), icon: <BarChart3 className="w-4 h-4" /> },
    { id: 'users', label: t('remnawave.users'), icon: <Users className="w-4 h-4" /> },
    { id: 'destinations', label: t('remnawave.destinations'), icon: <Globe className="w-4 h-4" /> },
    { id: 'analyzer', label: t('remnawave.analyzer'), icon: <Shield className="w-4 h-4" /> },
    { id: 'export', label: t('remnawave.export'), icon: <Download className="w-4 h-4" /> },
    { id: 'settings', label: t('remnawave.settings'), icon: <Settings className="w-4 h-4" /> },
  ], [t])
  
  if (isLoading) {
    return (
      <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="space-y-6">
        <div className="flex items-center gap-4">
          <Skeleton className="w-12 h-12 rounded-xl" />
          <div>
            <Skeleton className="h-7 w-48 mb-2" />
            <Skeleton className="h-4 w-72" />
          </div>
        </div>
        <div className="flex gap-2">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-10 w-28 rounded-xl" />
          ))}
        </div>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="card">
              <Skeleton className="h-4 w-24 mb-3" />
              <Skeleton className="h-8 w-20" />
            </div>
          ))}
        </div>
        <div className="card">
          <Skeleton className="h-64 w-full" />
        </div>
      </motion.div>
    )
  }
  
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className="space-y-6"
    >
      {/* Header */}
      <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <div className="w-12 h-12 rounded-xl bg-purple-500/10 flex items-center justify-center">
            <Radio className="w-6 h-6 text-purple-400" />
          </div>
          <div>
            <h1 className="text-2xl font-bold text-dark-100">{t('remnawave.title')}</h1>
            <p className="text-dark-400">{t('remnawave.subtitle')}</p>
          </div>
        </div>
        
        {activeTab !== 'settings' && activeTab !== 'export' && (
          <div className="flex items-center gap-3">
            <RefreshCountdown intervalMs={60000} />
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
      <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="flex gap-2 p-1 bg-dark-800/50 rounded-xl w-fit">
        {tabs.map(tab => (
          <button
            key={tab.id}
            onClick={() => startTransition(() => setActiveTab(tab.id))}
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
        <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="p-4 rounded-xl bg-danger/10 border border-danger/20">
          <div className="flex items-center gap-3 text-danger">
            <AlertCircle className="w-5 h-5" />
            <span>{error}</span>
          </div>
        </motion.div>
      )}
      
      {/* Tab Content */}
      <AnimatePresence mode="popLayout" initial={false}>
        {activeTab === 'overview' && (
          <motion.div
            key="overview"
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
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
                  {topDestinations.length === 0 && !statsLoaded && (
                    <div className="flex flex-col items-center py-4">
                      <RefreshCw className="w-5 h-5 animate-spin text-dark-500 mb-2" />
                      <span className="text-dark-500 text-sm">{t('common.loading')}...</span>
                    </div>
                  )}
                  {topDestinations.length === 0 && statsLoaded && (
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
                  {topUsers.length === 0 && !statsLoaded && (
                    <div className="flex flex-col items-center py-4">
                      <RefreshCw className="w-5 h-5 animate-spin text-dark-500 mb-2" />
                      <span className="text-dark-500 text-sm">{t('common.loading')}...</span>
                    </div>
                  )}
                  {topUsers.length === 0 && statsLoaded && (
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
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
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
                <button
                  onClick={() => setShowClearAllClientIpsConfirm(true)}
                  disabled={isClearingAllClientIps}
                  className="flex items-center gap-2 px-3 py-2 rounded-lg bg-danger/10 hover:bg-danger/20
                           text-danger text-sm transition-colors disabled:opacity-50"
                  title={t('remnawave.clear_all_client_ips')}
                >
                  {isClearingAllClientIps 
                    ? <RefreshCw className="w-4 h-4 animate-spin" />
                    : <Trash2 className="w-4 h-4" />
                  }
                  <span className="hidden sm:inline">{t('remnawave.clear_all_client_ips')}</span>
                </button>
              </div>
            </div>
            
            {/* Users Table */}
            <div className="rounded-xl bg-dark-800/50 border border-dark-700/50 overflow-hidden">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-dark-700">
                    <SortableHeader field="email" currentField={userSortField} direction={userSortDirection} onSort={handleUserSort}>ID</SortableHeader>
                    <SortableHeader field="username" currentField={userSortField} direction={userSortDirection} onSort={handleUserSort}>{t('remnawave.username')}</SortableHeader>
                    <SortableHeader field="status" currentField={userSortField} direction={userSortDirection} onSort={handleUserSort}>{t('remnawave.status')}</SortableHeader>
                    <SortableHeader field="total_visits" align="right" currentField={userSortField} direction={userSortDirection} onSort={handleUserSort}>{t('remnawave.visits')}</SortableHeader>
                    <SortableHeader field="unique_sites" align="right" currentField={userSortField} direction={userSortDirection} onSort={handleUserSort}>{t('remnawave.sites')}</SortableHeader>
                    <SortableHeader field="unique_ips" align="right" currentField={userSortField} direction={userSortDirection} onSort={handleUserSort}>IP</SortableHeader>
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
                        {user.unique_ips === 0 && user.infrastructure_ips > 0 ? (
                          <span 
                            className="inline-flex items-center gap-1.5 px-2 py-1 rounded text-xs font-medium bg-purple-500/20 text-purple-400"
                            title={t('remnawave.only_infra_ips')}
                          >
                            <Server className="w-3 h-3" />
                            <span>{user.infrastructure_ips}</span>
                          </span>
                        ) : (
                          <span className={`inline-flex items-center gap-1 px-2 py-1 rounded text-xs font-medium ${
                            user.unique_ips > 3 ? 'bg-warning/20 text-warning' :
                            user.unique_ips > 0 ? 'bg-accent-500/20 text-accent-400' :
                            'bg-dark-600 text-dark-400'
                          }`}>
                            <Network className="w-3 h-3" />
                            {user.unique_ips}
                          </span>
                        )}
                      </td>
                      <td className="p-4">
                        <ChevronRight className="w-4 h-4 text-dark-500" />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {filteredUsers.length === 0 && !isSearchingUsers && statsLoaded && (
                <div className="p-8 text-center text-dark-500">{t('remnawave.no_data')}</div>
              )}
              {(isSearchingUsers || (filteredUsers.length === 0 && !statsLoaded)) && (
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
            
            {/* Clear All Client IPs Confirmation Dialog */}
            {showClearAllClientIpsConfirm && (
              <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={() => setShowClearAllClientIpsConfirm(false)}>
                <div className="bg-dark-800 border border-dark-700 rounded-xl p-6 max-w-md mx-4" onClick={e => e.stopPropagation()}>
                  <h3 className="text-lg font-semibold text-dark-100 mb-2">{t('remnawave.confirm_clear_all_client_ips_title')}</h3>
                  <p className="text-dark-400 text-sm mb-4">{t('remnawave.confirm_clear_all_client_ips')}</p>
                  <div className="flex justify-end gap-3">
                    <button
                      onClick={() => setShowClearAllClientIpsConfirm(false)}
                      disabled={isClearingAllClientIps}
                      className="px-4 py-2 rounded-lg bg-dark-700 hover:bg-dark-600 text-dark-200 text-sm transition-colors"
                    >
                      {t('common.cancel')}
                    </button>
                    <button
                      onClick={handleClearAllClientIps}
                      disabled={isClearingAllClientIps}
                      className="flex items-center gap-2 px-4 py-2 rounded-lg bg-danger hover:bg-danger/80 text-white text-sm transition-colors disabled:opacity-50"
                    >
                      {isClearingAllClientIps && <RefreshCw className="w-4 h-4 animate-spin" />}
                      {t('remnawave.clear_all_client_ips')}
                    </button>
                  </div>
                </div>
              </div>
            )}
          </motion.div>
        )}
        
        {activeTab === 'destinations' && (
          <motion.div
            key="destinations"
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
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
              {filteredDestinations.length === 0 && statsLoaded && (
                <div className="p-8 text-center text-dark-500">{t('remnawave.no_data')}</div>
              )}
              {filteredDestinations.length === 0 && !statsLoaded && (
                <div className="p-8 text-center text-dark-500">
                  <RefreshCw className="w-5 h-5 animate-spin mx-auto mb-2" />
                  {t('common.loading')}...
                </div>
              )}
            </div>
          </motion.div>
        )}
        
        {activeTab === 'analyzer' && (
          <motion.div
            key="analyzer"
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="space-y-6"
          >
            {/* Analyzer Status */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <div className="p-4 rounded-xl bg-dark-800/50 border border-dark-700/50">
                <div className="flex items-center gap-3 mb-2">
                  <div className={`w-3 h-3 rounded-full ${analyzerSettings?.enabled ? 'bg-success-500 animate-pulse' : 'bg-dark-500'}`} />
                  <span className="text-dark-400 text-sm">{t('remnawave.analyzer_status')}</span>
                </div>
                <div className="text-xl font-semibold text-dark-100">
                  {analyzerSettings?.enabled ? t('remnawave.enabled') : t('remnawave.disabled')}
                </div>
              </div>
              <div className="p-4 rounded-xl bg-dark-800/50 border border-dark-700/50">
                <div className="text-dark-400 text-sm mb-2">{t('remnawave.last_check')}</div>
                <div className="text-xl font-semibold text-dark-100">
                  {analyzerSettings?.last_check_at 
                    ? new Date(analyzerSettings.last_check_at).toLocaleString() 
                    : t('remnawave.never')}
                </div>
              </div>
              <div className="p-4 rounded-xl bg-dark-800/50 border border-dark-700/50">
                <div className="text-dark-400 text-sm mb-2">{t('remnawave.anomalies_found')}</div>
                <div className="text-xl font-semibold text-dark-100">
                  {anomaliesTotal}
                </div>
              </div>
            </div>
            
            {/* Analyzer Settings */}
            <div className="p-6 rounded-xl bg-dark-800/50 border border-dark-700/50">
              <div className="flex items-center justify-between mb-6">
                <div className="flex items-center gap-3">
                  <div className="p-2 rounded-lg bg-accent-500/10">
                    <Settings className="w-5 h-5 text-accent-400" />
                  </div>
                  <div>
                    <h3 className="text-lg font-semibold text-dark-100">{t('remnawave.analyzer_settings')}</h3>
                    <p className="text-dark-400 text-sm">{t('remnawave.analyzer_description')}</p>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <motion.button
                    onClick={handleRunAnalyzerCheck}
                    disabled={isRunningCheck || !analyzerSettings?.enabled}
                    className="flex items-center gap-2 px-4 py-2 rounded-lg bg-accent-500/20 text-accent-400 
                             hover:bg-accent-500/30 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                    whileHover={{ scale: 1.02 }}
                    whileTap={{ scale: 0.98 }}
                  >
                    <Play className={`w-4 h-4 ${isRunningCheck ? 'animate-spin' : ''}`} />
                    {t('remnawave.run_check')}
                  </motion.button>
                </div>
              </div>
              
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                {/* Left column - Analysis settings */}
                <div className="space-y-4">
                  <h4 className="text-sm font-medium text-dark-300 mb-3">{t('remnawave.analysis_criteria')}</h4>
                  
                  {/* Enable/Disable */}
                  <label className="flex items-center justify-between p-3 rounded-lg bg-dark-700/30 cursor-pointer hover:bg-dark-700/50 transition-colors">
                    <div>
                      <span className="text-dark-200">{t('remnawave.enable_analyzer')}</span>
                      <p className="text-dark-500 text-xs mt-1">{t('remnawave.enable_analyzer_desc')}</p>
                    </div>
                    <Checkbox
                      size="md"
                      checked={editAnalyzerSettings.enabled ?? analyzerSettings?.enabled ?? false}
                      onChange={(e) => setEditAnalyzerSettings(prev => ({ ...prev, enabled: (e.target as HTMLInputElement).checked }))}
                    />
                  </label>
                  
                  {/* Check interval */}
                  <div className="p-3 rounded-lg bg-dark-700/30">
                    <label className="text-dark-200 text-sm">{t('remnawave.check_interval')}</label>
                    <select
                      value={editAnalyzerSettings.check_interval_minutes ?? analyzerSettings?.check_interval_minutes ?? 30}
                      onChange={(e) => setEditAnalyzerSettings(prev => ({ ...prev, check_interval_minutes: Number(e.target.value) }))}
                      className="w-full mt-2 px-3 py-2 rounded-lg bg-dark-800 border border-dark-600 text-dark-200 
                               focus:border-accent-500 focus:ring-1 focus:ring-accent-500 focus:outline-none"
                    >
                      <option value={15}>15 {t('remnawave.minutes')}</option>
                      <option value={30}>30 {t('remnawave.minutes')}</option>
                      <option value={60}>60 {t('remnawave.minutes')}</option>
                      <option value={120}>120 {t('remnawave.minutes')}</option>
                    </select>
                  </div>
                  
                  {/* Traffic limit */}
                  <div className="p-3 rounded-lg bg-dark-700/30">
                    <label className="text-dark-200 text-sm">{t('remnawave.traffic_limit_gb')}</label>
                    <input
                      type="number"
                      value={editAnalyzerSettings.traffic_limit_gb ?? analyzerSettings?.traffic_limit_gb ?? 100}
                      onChange={(e) => setEditAnalyzerSettings(prev => ({ ...prev, traffic_limit_gb: Number(e.target.value) }))}
                      min={1}
                      max={10000}
                      className="w-full mt-2 px-3 py-2 rounded-lg bg-dark-800 border border-dark-600 text-dark-200 
                               focus:border-accent-500 focus:ring-1 focus:ring-accent-500 focus:outline-none"
                    />
                    <p className="text-dark-500 text-xs mt-1">{t('remnawave.traffic_limit_desc')}</p>
                  </div>
                  
                  {/* IP multiplier */}
                  <div className="p-3 rounded-lg bg-dark-700/30">
                    <label className="text-dark-200 text-sm">{t('remnawave.ip_limit_multiplier')}</label>
                    <select
                      value={editAnalyzerSettings.ip_limit_multiplier ?? analyzerSettings?.ip_limit_multiplier ?? 2}
                      onChange={(e) => setEditAnalyzerSettings(prev => ({ ...prev, ip_limit_multiplier: Number(e.target.value) }))}
                      className="w-full mt-2 px-3 py-2 rounded-lg bg-dark-800 border border-dark-600 text-dark-200 
                               focus:border-accent-500 focus:ring-1 focus:ring-accent-500 focus:outline-none"
                    >
                      <option value={1.5}>1.5x</option>
                      <option value={2}>2x</option>
                      <option value={3}>3x</option>
                      <option value={5}>5x</option>
                    </select>
                    <p className="text-dark-500 text-xs mt-1">{t('remnawave.ip_limit_desc')}</p>
                  </div>
                  
                  {/* HWID check */}
                  <label className="flex items-center justify-between p-3 rounded-lg bg-dark-700/30 cursor-pointer hover:bg-dark-700/50 transition-colors">
                    <div>
                      <span className="text-dark-200">{t('remnawave.check_hwid')}</span>
                      <p className="text-dark-500 text-xs mt-1">{t('remnawave.check_hwid_desc')}</p>
                    </div>
                    <Checkbox
                      size="md"
                      checked={editAnalyzerSettings.check_hwid_anomalies ?? analyzerSettings?.check_hwid_anomalies ?? true}
                      onChange={(e) => setEditAnalyzerSettings(prev => ({ ...prev, check_hwid_anomalies: (e.target as HTMLInputElement).checked }))}
                    />
                  </label>
                </div>
                
                {/* Right column - Telegram settings */}
                <div className="space-y-4">
                  <h4 className="text-sm font-medium text-dark-300 mb-3">{t('remnawave.telegram_notifications')}</h4>
                  
                  {/* Bot token */}
                  <div className="p-3 rounded-lg bg-dark-700/30">
                    <label className="text-dark-200 text-sm">{t('remnawave.telegram_bot_token')}</label>
                    <div className="relative mt-2">
                      <input
                        type={showTelegramToken ? 'text' : 'password'}
                        value={editAnalyzerSettings.telegram_bot_token ?? ''}
                        onChange={(e) => setEditAnalyzerSettings(prev => ({ ...prev, telegram_bot_token: e.target.value }))}
                        placeholder={analyzerSettings?.telegram_bot_token ? '***' : 'Enter bot token'}
                        className="w-full px-3 py-2 pr-10 rounded-lg bg-dark-800 border border-dark-600 text-dark-200 
                                 focus:border-accent-500 focus:ring-1 focus:ring-accent-500 focus:outline-none"
                      />
                      <button
                        type="button"
                        onClick={() => setShowTelegramToken(!showTelegramToken)}
                        className="absolute right-2 top-1/2 -translate-y-1/2 text-dark-400 hover:text-dark-200"
                      >
                        {showTelegramToken ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                      </button>
                    </div>
                  </div>
                  
                  {/* Chat ID */}
                  <div className="p-3 rounded-lg bg-dark-700/30">
                    <label className="text-dark-200 text-sm">{t('remnawave.telegram_chat_id')}</label>
                    <input
                      type="text"
                      value={editAnalyzerSettings.telegram_chat_id ?? analyzerSettings?.telegram_chat_id ?? ''}
                      onChange={(e) => setEditAnalyzerSettings(prev => ({ ...prev, telegram_chat_id: e.target.value }))}
                      placeholder="Enter chat ID"
                      className="w-full mt-2 px-3 py-2 rounded-lg bg-dark-800 border border-dark-600 text-dark-200 
                               focus:border-accent-500 focus:ring-1 focus:ring-accent-500 focus:outline-none"
                    />
                  </div>
                  
                  {/* Test Telegram button */}
                  <motion.button
                    onClick={handleTestTelegram}
                    disabled={isTestingTelegram}
                    className="flex items-center gap-2 px-4 py-2 rounded-lg bg-dark-700 text-dark-200 
                             hover:bg-dark-600 disabled:opacity-50 transition-colors"
                    whileHover={{ scale: 1.02 }}
                    whileTap={{ scale: 0.98 }}
                  >
                    <Send className={`w-4 h-4 ${isTestingTelegram ? 'animate-pulse' : ''}`} />
                    {t('remnawave.test_telegram')}
                  </motion.button>
                  
                  {telegramTestResult && (
                    <div className={`p-3 rounded-lg ${telegramTestResult.success ? 'bg-success-500/10 text-success-400' : 'bg-danger-500/10 text-danger-400'}`}>
                      {telegramTestResult.success ? t('remnawave.telegram_test_success') : telegramTestResult.error || t('remnawave.telegram_test_failed')}
                    </div>
                  )}
                  
                  {analyzerSettings?.last_error && (
                    <div className="p-3 rounded-lg bg-danger-500/10 border border-danger-500/20">
                      <div className="flex items-center gap-2 text-danger-400 text-sm">
                        <AlertCircle className="w-4 h-4" />
                        {analyzerSettings.last_error}
                      </div>
                    </div>
                  )}
                </div>
              </div>
              
              {/* Save button */}
              <div className="flex justify-end mt-6 pt-4 border-t border-dark-700">
                <motion.button
                  onClick={handleSaveAnalyzerSettings}
                  disabled={isSavingAnalyzer}
                  className="flex items-center gap-2 px-6 py-2 rounded-lg bg-accent-500 text-dark-900 font-medium
                           hover:bg-accent-400 disabled:opacity-50 transition-colors"
                  whileHover={{ scale: 1.02 }}
                  whileTap={{ scale: 0.98 }}
                >
                  {isSavingAnalyzer ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
                  {t('remnawave.save_settings')}
                </motion.button>
              </div>
            </div>
            
            {/* Anomalies List */}
            <div className="p-6 rounded-xl bg-dark-800/50 border border-dark-700/50">
              <div className="flex items-center justify-between mb-6">
                <div className="flex items-center gap-3">
                  <div className="p-2 rounded-lg bg-warning-500/10">
                    <AlertCircle className="w-5 h-5 text-warning-400" />
                  </div>
                  <div>
                    <h3 className="text-lg font-semibold text-dark-100">{t('remnawave.anomalies')}</h3>
                    <p className="text-dark-400 text-sm">{t('remnawave.anomalies_description')}</p>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <select
                    value={anomalyFilter}
                    onChange={(e) => {
                      setAnomalyFilter(e.target.value as 'all' | 'active' | 'resolved')
                      setAnomaliesOffset(0)
                    }}
                    className="px-3 py-2 rounded-lg bg-dark-700 border border-dark-600 text-dark-200 text-sm"
                  >
                    <option value="all">{t('remnawave.all')}</option>
                    <option value="active">{t('remnawave.active')}</option>
                    <option value="resolved">{t('remnawave.resolved')}</option>
                  </select>
                  <select
                    value={anomalyTypeFilter}
                    onChange={(e) => {
                      setAnomalyTypeFilter(e.target.value)
                      setAnomaliesOffset(0)
                    }}
                    className="px-3 py-2 rounded-lg bg-dark-700 border border-dark-600 text-dark-200 text-sm"
                  >
                    <option value="">{t('remnawave.all_types')}</option>
                    <option value="traffic">{t('remnawave.type_traffic')}</option>
                    <option value="ip_count">{t('remnawave.type_ip')}</option>
                    <option value="hwid">{t('remnawave.type_hwid')}</option>
                  </select>
                  <motion.button
                    onClick={fetchAnalyzerData}
                    disabled={isLoadingAnomalies}
                    className="p-2 rounded-lg bg-dark-700 text-dark-300 hover:text-dark-100 transition-colors"
                    whileHover={{ scale: 1.05 }}
                    whileTap={{ scale: 0.95 }}
                    title={t('remnawave.refresh')}
                  >
                    <RefreshCw className={`w-4 h-4 ${isLoadingAnomalies ? 'animate-spin' : ''}`} />
                  </motion.button>
                  {anomalies.length > 0 && (
                    <motion.button
                      onClick={handleDeleteAllAnomalies}
                      className="flex items-center gap-1 px-3 py-2 rounded-lg bg-danger-500/20 text-danger-400 
                               hover:bg-danger-500/30 transition-colors text-sm"
                      whileHover={{ scale: 1.05 }}
                      whileTap={{ scale: 0.95 }}
                    >
                      <Trash2 className="w-4 h-4" />
                      {t('remnawave.clear_all')}
                    </motion.button>
                  )}
                </div>
              </div>
              
              {/* Anomalies table */}
              <div className="overflow-x-auto">
                <table className="w-full">
                  <thead>
                    <tr className="text-left text-dark-400 text-sm border-b border-dark-700">
                      <th className="pb-3 font-medium">{t('remnawave.date')}</th>
                      <th className="pb-3 font-medium">{t('remnawave.user')}</th>
                      <th className="pb-3 font-medium">{t('remnawave.type')}</th>
                      <th className="pb-3 font-medium">{t('remnawave.severity')}</th>
                      <th className="pb-3 font-medium">{t('remnawave.details')}</th>
                      <th className="pb-3 font-medium w-10"></th>
                    </tr>
                  </thead>
                  <tbody className="text-dark-200">
                    {anomalies.map((anomaly) => (
                      <tr 
                        key={anomaly.id} 
                        className={`border-b border-dark-700/50 hover:bg-dark-700/30 cursor-pointer transition-colors ${anomaly.resolved ? 'opacity-50' : ''}`}
                        onClick={() => handleUserClick(anomaly.user_email)}
                      >
                        <td className="py-3 text-sm">
                          {anomaly.created_at ? new Date(anomaly.created_at).toLocaleString() : '-'}
                        </td>
                        <td className="py-3">
                          <div className="text-sm">{anomaly.username || `ID: ${anomaly.user_email}`}</div>
                          {anomaly.telegram_id && (
                            <div className="text-xs text-dark-500">TG: {anomaly.telegram_id}</div>
                          )}
                        </td>
                        <td className="py-3">
                          <span className={`px-2 py-1 rounded text-xs font-medium ${
                            anomaly.anomaly_type === 'traffic' ? 'bg-blue-500/20 text-blue-400' :
                            anomaly.anomaly_type === 'ip_count' ? 'bg-purple-500/20 text-purple-400' :
                            'bg-orange-500/20 text-orange-400'
                          }`}>
                            {anomaly.anomaly_type === 'traffic' ? t('remnawave.type_traffic') :
                             anomaly.anomaly_type === 'ip_count' ? t('remnawave.type_ip') :
                             t('remnawave.type_hwid')}
                          </span>
                        </td>
                        <td className="py-3">
                          <span className={`px-2 py-1 rounded text-xs font-medium ${
                            anomaly.severity === 'critical' ? 'bg-danger-500/20 text-danger-400' : 'bg-warning-500/20 text-warning-400'
                          }`}>
                            {anomaly.severity}
                          </span>
                        </td>
                        <td className="py-3 text-sm max-w-xs">
                          {anomaly.details && (
                            <div className="text-dark-400">
                              {anomaly.anomaly_type === 'traffic' && (
                                <span>
                                  {(anomaly.details as { consumed_gb?: number }).consumed_gb} GB / {(anomaly.details as { limit_gb?: number }).limit_gb} GB 
                                  <span className="text-dark-500 ml-1">
                                    ({(anomaly.details as { period_minutes?: number }).period_minutes || 30} {t('remnawave.minutes')})
                                  </span>
                                </span>
                              )}
                              {anomaly.anomaly_type === 'ip_count' && (() => {
                                const d = anomaly.details as { unique_ips?: number; effective_count?: number; ip_limit?: number; asn_groups?: Array<{ asn: string | null; prefix: string | null; count: number; visits: number }> }
                                const eff = d.effective_count ?? d.unique_ips ?? 0
                                return (
                                  <div>
                                    <span>{eff} {t('remnawave.asn_groups')} / {d.ip_limit} {t('remnawave.limit')}</span>
                                    <span className="text-dark-500 ml-1">({d.unique_ips} IP)</span>
                                    {d.asn_groups && d.asn_groups.length > 0 && (
                                      <div className="mt-1 space-y-0.5">
                                        {d.asn_groups.slice(0, 3).map((g, gi) => (
                                          <div key={gi} className="text-dark-500 text-xs">
                                            {g.asn ? `ASN ${g.asn}` : '???'}{g.prefix ? ` (${g.prefix})` : ''}: {g.count} IP, {g.visits?.toLocaleString()} vis
                                          </div>
                                        ))}
                                        {d.asn_groups.length > 3 && (
                                          <div className="text-dark-600 text-xs">+{d.asn_groups.length - 3} ...</div>
                                        )}
                                      </div>
                                    )}
                                  </div>
                                )
                              })()}
                              {anomaly.anomaly_type === 'hwid' && (
                                <span>{(anomaly.details as { suspicious_count?: number }).suspicious_count} {t('remnawave.suspicious_devices')}</span>
                              )}
                            </div>
                          )}
                        </td>
                        <td className="py-3">
                          <motion.button
                            onClick={(e) => { e.stopPropagation(); handleDeleteAnomaly(anomaly.id) }}
                            className="flex items-center gap-1 px-2 py-1 rounded text-xs bg-danger-500/20 text-danger-400 
                                     hover:bg-danger-500/30 transition-colors"
                            whileHover={{ scale: 1.05 }}
                            whileTap={{ scale: 0.95 }}
                            title={t('remnawave.delete')}
                          >
                            <Trash2 className="w-3 h-3" />
                          </motion.button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {anomalies.length === 0 && !isLoadingAnomalies && (
                  <div className="p-8 text-center text-dark-500">{t('remnawave.no_anomalies')}</div>
                )}
                {anomalies.length === 0 && isLoadingAnomalies && (
                  <div className="p-8 text-center text-dark-500">
                    <RefreshCw className="w-5 h-5 animate-spin mx-auto mb-2" />
                    {t('common.loading')}...
                  </div>
                )}
              </div>
            </div>
          </motion.div>
        )}
        
        {activeTab === 'export' && (
          <motion.div
            key="export"
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="space-y-6"
          >
            {/* Export Settings */}
            <div className="p-6 rounded-xl bg-dark-800/50 border border-dark-700/50">
              <div className="flex items-center gap-3 mb-6">
                <div className="p-2 rounded-lg bg-accent-500/10">
                  <Download className="w-5 h-5 text-accent-400" />
                </div>
                <div>
                  <h3 className="text-lg font-semibold text-dark-100">{t('remnawave.export_settings')}</h3>
                  <p className="text-dark-400 text-sm">{t('remnawave.export_description')}</p>
                </div>
              </div>
              
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                {/* Left Column - Period & Button */}
                <div className="space-y-6">
                  {/* Period Selection */}
                  <div>
                    <label className="block text-sm font-medium text-dark-300 mb-3">
                      {t('remnawave.export_period')}
                    </label>
                    <PeriodSelector 
                      value={exportPeriod} 
                      onChange={setExportPeriod}
                      options={[
                        { value: 'all', label: t('period.all') },
                        { value: '24h', label: t('period.24h') },
                        { value: '7d', label: t('period.7d') },
                        { value: '30d', label: t('period.30d') },
                        { value: '365d', label: t('period.365d') },
                      ]}
                    />
                  </div>
                  
                  {/* Start Export Button */}
                  <motion.button
                    onClick={handleCreateExport}
                    disabled={isExporting}
                    className="w-full flex items-center justify-center gap-2 px-6 py-3 rounded-lg bg-accent-500 hover:bg-accent-600 
                             text-white font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                    whileHover={{ scale: 1.02 }}
                    whileTap={{ scale: 0.98 }}
                  >
                    {isExporting ? (
                      <>
                        <RefreshCw className="w-5 h-5 animate-spin" />
                        {t('remnawave.export_starting')}
                      </>
                    ) : (
                      <>
                        <Download className="w-5 h-5" />
                        {t('remnawave.export_start')}
                      </>
                    )}
                  </motion.button>
                </div>
                
                {/* Right Column - Field Toggles */}
                <div className="space-y-4">
                  {/* User Fields */}
                  <div className="p-4 rounded-lg bg-dark-700/30">
                    <h4 className="text-sm font-medium text-dark-300 mb-3">{t('remnawave.export_user_fields')}</h4>
                    <div className="space-y-2">
                      {[
                        { key: 'include_user_id', label: t('remnawave.export_user_id') },
                        { key: 'include_username', label: t('remnawave.export_username') },
                        { key: 'include_status', label: t('remnawave.export_status') },
                        { key: 'include_telegram_id', label: t('remnawave.export_telegram_id') },
                      ].map(({ key, label }) => (
                        <label key={key} className="flex items-center gap-3 cursor-pointer">
                          <Checkbox
                            checked={exportSettings[key as keyof typeof exportSettings]}
                            onChange={(e) => setExportSettings(prev => ({ ...prev, [key]: (e.target as HTMLInputElement).checked }))}
                          />
                          <span className="text-dark-200 text-sm">{label}</span>
                        </label>
                      ))}
                    </div>
                  </div>
                  
                  {/* Destination Fields */}
                  <div className="p-4 rounded-lg bg-dark-700/30">
                    <h4 className="text-sm font-medium text-dark-300 mb-3">{t('remnawave.export_destination_fields')}</h4>
                    <div className="space-y-2">
                      {[
                        { key: 'include_destinations', label: t('remnawave.export_destinations') },
                        { key: 'include_visits_count', label: t('remnawave.export_visits_count') },
                        { key: 'include_first_seen', label: t('remnawave.export_first_seen') },
                        { key: 'include_last_seen', label: t('remnawave.export_last_seen') },
                      ].map(({ key, label }) => (
                        <label key={key} className="flex items-center gap-3 cursor-pointer">
                          <Checkbox
                            checked={exportSettings[key as keyof typeof exportSettings]}
                            onChange={(e) => setExportSettings(prev => ({ ...prev, [key]: (e.target as HTMLInputElement).checked }))}
                          />
                          <span className="text-dark-200 text-sm">{label}</span>
                        </label>
                      ))}
                    </div>
                  </div>
                  
                  {/* IP & Traffic Fields */}
                  <div className="p-4 rounded-lg bg-dark-700/30">
                    <h4 className="text-sm font-medium text-dark-300 mb-3">{t('remnawave.export_ip_fields')} / {t('remnawave.export_traffic_fields')}</h4>
                    <div className="space-y-2">
                      {[
                        { key: 'include_client_ips', label: t('remnawave.export_client_ips') },
                        { key: 'include_infra_ips', label: t('remnawave.export_infra_ips') },
                        { key: 'include_traffic', label: t('remnawave.export_traffic') },
                      ].map(({ key, label }) => (
                        <label key={key} className="flex items-center gap-3 cursor-pointer">
                          <Checkbox
                            checked={exportSettings[key as keyof typeof exportSettings]}
                            onChange={(e) => setExportSettings(prev => ({ ...prev, [key]: (e.target as HTMLInputElement).checked }))}
                          />
                          <span className="text-dark-200 text-sm">{label}</span>
                        </label>
                      ))}
                    </div>
                  </div>
                </div>
              </div>
            </div>
            
            {/* Export History */}
            <div className="p-6 rounded-xl bg-dark-800/50 border border-dark-700/50">
              <div className="flex items-center justify-between mb-4">
                <h3 className="text-lg font-semibold text-dark-100">{t('remnawave.export_history')}</h3>
                <motion.button
                  onClick={fetchExports}
                  className="p-2 rounded-lg bg-dark-700 hover:bg-dark-600 text-dark-300 transition-colors"
                  whileHover={{ scale: 1.05 }}
                  whileTap={{ scale: 0.95 }}
                >
                  <RefreshCw className={`w-4 h-4 ${isLoadingExports ? 'animate-spin' : ''}`} />
                </motion.button>
              </div>
              
              {exports.length === 0 ? (
                <div className="text-center py-8 text-dark-500">
                  {t('remnawave.export_no_exports')}
                </div>
              ) : (
                <div className="space-y-3">
                  {exports.map(exp => (
                    <div key={exp.id} className="flex items-center justify-between p-4 rounded-lg bg-dark-700/50">
                      <div className="flex items-center gap-4">
                        <div className={`w-2 h-2 rounded-full ${
                          exp.status === 'completed' ? 'bg-success' :
                          exp.status === 'processing' ? 'bg-warning animate-pulse' :
                          exp.status === 'failed' ? 'bg-danger' : 'bg-dark-500'
                        }`} />
                        <div>
                          <div className="text-dark-200 font-medium">{exp.filename}</div>
                          <div className="text-dark-500 text-sm flex items-center gap-3">
                            <span>
                              {exp.status === 'pending' && t('remnawave.export_status_pending')}
                              {exp.status === 'processing' && t('remnawave.export_status_processing')}
                              {exp.status === 'completed' && t('remnawave.export_status_completed')}
                              {exp.status === 'failed' && t('remnawave.export_status_failed')}
                            </span>
                            {exp.rows_count !== null && (
                              <span>{exp.rows_count.toLocaleString()} {t('remnawave.export_rows')}</span>
                            )}
                            {exp.file_size !== null && (
                              <span>{formatBytes(exp.file_size)}</span>
                            )}
                            {exp.error_message && (
                              <span className="text-danger">{exp.error_message}</span>
                            )}
                          </div>
                        </div>
                      </div>
                      <div className="flex items-center gap-2">
                        {exp.status === 'completed' && (
                          <motion.button
                            onClick={() => handleDownloadExport(exp.id, exp.filename)}
                            className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-accent-500/20 hover:bg-accent-500/30 
                                     text-accent-400 text-sm transition-colors"
                            whileHover={{ scale: 1.02 }}
                            whileTap={{ scale: 0.98 }}
                          >
                            <Download className="w-4 h-4" />
                            {t('remnawave.export_download')}
                          </motion.button>
                        )}
                        <motion.button
                          onClick={() => handleDeleteExport(exp.id)}
                          className="p-2 rounded-lg bg-danger/20 hover:bg-danger/30 text-danger transition-colors"
                          whileHover={{ scale: 1.05 }}
                          whileTap={{ scale: 0.95 }}
                        >
                          <Trash2 className="w-4 h-4" />
                        </motion.button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </motion.div>
        )}
        
        {activeTab === 'settings' && (
          <motion.div
            key="settings"
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
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
                
                {collectorStatus?.next_collect_in != null && collectorStatus.next_collect_in > 0 && (
                  <CollectorCountdown
                    initialSeconds={collectorStatus.next_collect_in}
                    onExpire={handleCollectorExpire}
                  />
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
                
                {/* Retention Settings */}
                <div className="pt-4 border-t border-dark-700">
                  <h4 className="text-sm font-medium text-dark-300 mb-3">{t('remnawave.retention_settings')}</h4>
                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <label className="block text-xs text-dark-400 mb-1">{t('remnawave.visit_stats_retention')}</label>
                      <div className="flex items-center gap-2">
                        <input
                          type="number"
                          value={editSettings.visit_stats_retention_days || 365}
                          onChange={(e) => setEditSettings(s => ({ ...s, visit_stats_retention_days: Math.min(365, Math.max(7, parseInt(e.target.value) || 365)) }))}
                          min={7}
                          max={365}
                          className="w-20 px-3 py-1.5 rounded-lg bg-dark-900 border border-dark-700 
                                   text-dark-100 text-sm focus:outline-none focus:border-accent-500"
                        />
                        <span className="text-xs text-dark-500">{t('common.days')}</span>
                      </div>
                    </div>
                    <div>
                      <label className="block text-xs text-dark-400 mb-1">{t('remnawave.hourly_stats_retention')}</label>
                      <div className="flex items-center gap-2">
                        <input
                          type="number"
                          value={editSettings.hourly_stats_retention_days || 365}
                          onChange={(e) => setEditSettings(s => ({ ...s, hourly_stats_retention_days: Math.min(365, Math.max(7, parseInt(e.target.value) || 365)) }))}
                          min={7}
                          max={365}
                          className="w-20 px-3 py-1.5 rounded-lg bg-dark-900 border border-dark-700 
                                   text-dark-100 text-sm focus:outline-none focus:border-accent-500"
                        />
                        <span className="text-xs text-dark-500">{t('common.days')}</span>
                      </div>
                    </div>
                    <div>
                      <label className="block text-xs text-dark-400 mb-1">{t('remnawave.ip_stats_retention')}</label>
                      <div className="flex items-center gap-2">
                        <input
                          type="number"
                          value={editSettings.ip_stats_retention_days || 90}
                          onChange={(e) => setEditSettings(s => ({ ...s, ip_stats_retention_days: Math.min(365, Math.max(7, parseInt(e.target.value) || 90)) }))}
                          min={7}
                          max={365}
                          className="w-20 px-3 py-1.5 rounded-lg bg-dark-900 border border-dark-700 
                                   text-dark-100 text-sm focus:outline-none focus:border-accent-500"
                        />
                        <span className="text-xs text-dark-500">{t('common.days')}</span>
                      </div>
                    </div>
                    <div>
                      <label className="block text-xs text-dark-400 mb-1">{t('remnawave.ip_destination_retention')}</label>
                      <div className="flex items-center gap-2">
                        <input
                          type="number"
                          value={editSettings.ip_destination_retention_days || 90}
                          onChange={(e) => setEditSettings(s => ({ ...s, ip_destination_retention_days: Math.min(365, Math.max(7, parseInt(e.target.value) || 90)) }))}
                          min={7}
                          max={365}
                          className="w-20 px-3 py-1.5 rounded-lg bg-dark-900 border border-dark-700 
                                   text-dark-100 text-sm focus:outline-none focus:border-accent-500"
                        />
                        <span className="text-xs text-dark-500">{t('common.days')}</span>
                      </div>
                    </div>
                  </div>
                  <p className="text-xs text-dark-500 mt-2">{t('remnawave.retention_hint')}</p>
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
              
              {/* Server List with Checkboxes — only servers with xray */}
              <div className="space-y-2 max-h-[400px] overflow-y-auto">
                {allServers.filter(s => s.has_xray_node).map(server => {
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
                      <Checkbox
                        size="md"
                        checked={isSelected}
                        onChange={() => handleToggleNodeSelection(server.id)}
                        onClick={(e) => e.stopPropagation()}
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
                          <span className="text-xs px-1.5 py-0.5 rounded bg-accent-500/20 text-accent-400">
                            xray
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
                {allServers.filter(s => s.has_xray_node).length === 0 && (
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
            
            {/* Excluded Destinations */}
            <div className="p-6 rounded-xl bg-dark-800/50 border border-dark-700/50">
              <h3 className="text-lg font-semibold text-dark-100 mb-2">{t('remnawave.excluded_destinations')}</h3>
              <p className="text-dark-500 text-sm mb-4">{t('remnawave.excluded_destinations_hint')}</p>
              
              {/* Add new excluded destination */}
              <div className="flex gap-2 mb-4">
                <input
                  type="text"
                  value={newExcludedDest}
                  onChange={(e) => setNewExcludedDest(e.target.value)}
                  placeholder={t('remnawave.excluded_destination_placeholder')}
                  className="flex-1 px-3 py-2 rounded-lg bg-dark-900 border border-dark-700 
                           text-dark-100 placeholder-dark-500 focus:outline-none focus:border-accent-500"
                  onKeyDown={(e) => e.key === 'Enter' && handleAddExcludedDest()}
                />
                <input
                  type="text"
                  value={newExcludedDestDescription}
                  onChange={(e) => setNewExcludedDestDescription(e.target.value)}
                  placeholder={t('remnawave.excluded_destination_description_placeholder')}
                  className="w-48 px-3 py-2 rounded-lg bg-dark-900 border border-dark-700 
                           text-dark-100 placeholder-dark-500 focus:outline-none focus:border-accent-500"
                />
                <button
                  onClick={handleAddExcludedDest}
                  disabled={isAddingExcludedDest || !newExcludedDest.trim()}
                  className="px-4 py-2 rounded-lg bg-accent-500 hover:bg-accent-600 text-white 
                           transition-colors disabled:opacity-50 flex items-center gap-1"
                >
                  {isAddingExcludedDest ? (
                    <RefreshCw className="w-4 h-4 animate-spin" />
                  ) : (
                    <Check className="w-4 h-4" />
                  )}
                  {t('common.add')}
                </button>
              </div>
              
              {/* Excluded destinations list */}
              <div className="space-y-2 max-h-[300px] overflow-y-auto">
                {excludedDestinations.map(dest => (
                  <div
                    key={dest.id}
                    className="flex items-center gap-3 p-3 rounded-lg bg-dark-900/50 border border-dark-700/50"
                  >
                    <Globe className="w-4 h-4 text-dark-500 flex-shrink-0" />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-dark-200 font-mono">{dest.destination}</span>
                        {dest.description && (
                          <span className="text-dark-500 text-sm">({dest.description})</span>
                        )}
                      </div>
                    </div>
                    <button
                      onClick={() => handleDeleteExcludedDest(dest.id)}
                      className="p-2 rounded-lg hover:bg-dark-700 text-dark-500 hover:text-danger transition-colors"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>
                ))}
                {excludedDestinations.length === 0 && (
                  <div className="text-center text-dark-500 py-4">{t('remnawave.no_excluded_destinations')}</div>
                )}
              </div>
            </div>
            
            {/* Ignored Users */}
            <div className="p-6 rounded-xl bg-dark-800/50 border border-dark-700/50">
              <h3 className="text-lg font-semibold text-dark-100 mb-2 flex items-center gap-2">
                <EyeOff className="w-5 h-5" />
                {t('remnawave.ignored_users_title')}
              </h3>
              <p className="text-dark-500 text-sm mb-4">{t('remnawave.ignored_users_description')}</p>
              
              {/* Add new ignored user */}
              <div className="flex gap-2 mb-4">
                <input
                  type="text"
                  value={newIgnoredUserId}
                  onChange={(e) => setNewIgnoredUserId(e.target.value.replace(/\D/g, ''))}
                  placeholder={t('remnawave.ignored_user_id_placeholder')}
                  className="flex-1 px-3 py-2 rounded-lg bg-dark-900 border border-dark-700 
                           text-dark-100 placeholder-dark-500 focus:outline-none focus:border-accent-500"
                  onKeyDown={(e) => e.key === 'Enter' && handleAddIgnoredUser()}
                />
                <button
                  onClick={handleAddIgnoredUser}
                  disabled={isAddingIgnoredUser || !newIgnoredUserId.trim()}
                  className="px-4 py-2 rounded-lg bg-accent-500 hover:bg-accent-600 text-white 
                           transition-colors disabled:opacity-50 flex items-center gap-1"
                >
                  {isAddingIgnoredUser ? (
                    <RefreshCw className="w-4 h-4 animate-spin" />
                  ) : (
                    <Check className="w-4 h-4" />
                  )}
                  {t('common.add')}
                </button>
              </div>
              
              {/* Ignored users list */}
              <div className="space-y-2 max-h-[300px] overflow-y-auto">
                {ignoredUsers.map(user => (
                  <div
                    key={user.user_id}
                    className="flex items-center gap-3 p-3 rounded-lg bg-dark-900/50 border border-dark-700/50"
                  >
                    <Users className="w-4 h-4 text-dark-500 flex-shrink-0" />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-dark-200 font-mono">{user.user_id}</span>
                        {user.username && (
                          <span className="text-dark-400 text-sm">{user.username}</span>
                        )}
                        {user.status && (
                          <span className={`text-xs px-1.5 py-0.5 rounded ${
                            user.status === 'ACTIVE' ? 'bg-success/20 text-success' : 
                            user.status === 'DISABLED' ? 'bg-danger/20 text-danger' : 
                            'bg-dark-600 text-dark-400'
                          }`}>
                            {user.status}
                          </span>
                        )}
                      </div>
                      {user.telegram_id && (
                        <div className="text-dark-500 text-xs mt-1">
                          Telegram: {user.telegram_id}
                        </div>
                      )}
                    </div>
                    <button
                      onClick={() => handleRemoveIgnoredUser(user.user_id)}
                      className="p-2 rounded-lg hover:bg-dark-700 text-dark-500 hover:text-danger transition-colors"
                      title={t('common.delete')}
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>
                ))}
                {ignoredUsers.length === 0 && (
                  <div className="text-center text-dark-500 py-4">{t('remnawave.no_ignored_users')}</div>
                )}
              </div>
            </div>
            
            {/* Database Management */}
            <div className="p-6 rounded-xl bg-dark-800/50 border border-dark-700/50">
              <h3 className="text-lg font-semibold text-dark-100 mb-4 flex items-center gap-2">
                <Database className="w-5 h-5" />
                {t('remnawave.db_management')}
              </h3>
              
              {/* DB Total Size */}
              {dbInfo && dbInfo.total_size_bytes !== null && dbInfo.total_size_bytes !== undefined && (
                <div className="p-4 rounded-lg bg-gradient-to-r from-accent-500/10 to-purple-500/10 border border-accent-500/20 mb-6">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <Database className="w-5 h-5 text-accent-400" />
                      <div>
                        <div className="text-dark-400 text-sm">{t('remnawave.db_total_size')}</div>
                        <div className="text-dark-100 text-2xl font-bold">{formatBytes(dbInfo.total_size_bytes)}</div>
                      </div>
                    </div>
                    <div className="text-right">
                      <div className="text-dark-500 text-xs">{t('remnawave.db_tables_count')}</div>
                      <div className="text-dark-300 text-sm">3 {t('remnawave.tables')}</div>
                    </div>
                  </div>
                </div>
              )}
              
              {/* DB Stats */}
              {dbInfo && (
                <div className="grid grid-cols-3 gap-4 mb-6">
                  <div className="p-3 rounded-lg bg-dark-900/50">
                    <div className="text-dark-500 text-xs mb-1">{t('remnawave.db_visits')}</div>
                    <div className="text-dark-100 text-lg font-semibold">
                      {dbInfo.tables.xray_stats.count.toLocaleString()}
                    </div>
                    {dbInfo.tables.xray_stats.size_bytes && (
                      <div className="text-accent-400 text-xs mt-1">
                        {formatBytes(dbInfo.tables.xray_stats.size_bytes)}
                      </div>
                    )}
                    {dbInfo.tables.xray_stats.first_seen && (
                      <div className="text-dark-600 text-xs mt-1">
                        {formatDate(dbInfo.tables.xray_stats.first_seen)} - {formatDate(dbInfo.tables.xray_stats.last_seen)}
                      </div>
                    )}
                  </div>
                  <div className="p-3 rounded-lg bg-dark-900/50">
                    <div className="text-dark-500 text-xs mb-1">{t('remnawave.db_hourly')}</div>
                    <div className="text-dark-100 text-lg font-semibold">
                      {dbInfo.tables.xray_hourly_stats.count.toLocaleString()}
                    </div>
                    {dbInfo.tables.xray_hourly_stats.size_bytes && (
                      <div className="text-accent-400 text-xs mt-1">
                        {formatBytes(dbInfo.tables.xray_hourly_stats.size_bytes)}
                      </div>
                    )}
                  </div>
                  <div className="p-3 rounded-lg bg-dark-900/50">
                    <div className="text-dark-500 text-xs mb-1">{t('remnawave.db_users_cache')}</div>
                    <div className="text-dark-100 text-lg font-semibold">
                      {dbInfo.tables.remnawave_user_cache.count.toLocaleString()}
                    </div>
                    {dbInfo.tables.remnawave_user_cache.size_bytes && (
                      <div className="text-accent-400 text-xs mt-1">
                        {formatBytes(dbInfo.tables.remnawave_user_cache.size_bytes)}
                      </div>
                    )}
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
                    
                    {/* Bandwidth Stats Chart (from live API) - Stacked by Server */}
                    {selectedUserFull?.bandwidth_stats?.categories && selectedUserFull.bandwidth_stats.sparklineData && selectedUserFull.bandwidth_stats.sparklineData.length > 0 && (
                      <div className="p-4 rounded-lg bg-dark-800">
                        <div className="flex items-center justify-between mb-4">
                          <h4 className="text-sm font-medium text-dark-300">{t('remnawave.daily_traffic')}</h4>
                          <span className="text-dark-500 text-xs">
                            {t('remnawave.total')}: {formatBytes(selectedUserFull.bandwidth_stats.sparklineData.reduce((a, b) => a + b, 0))}
                          </span>
                        </div>
                        
                        {/* Stacked Traffic Chart with Date Labels - by Server */}
                        <div className="flex gap-1">
                          {(() => {
                            const categories = selectedUserFull.bandwidth_stats!.categories!
                            const series = selectedUserFull.bandwidth_stats!.series
                            const sparklineData = selectedUserFull.bandwidth_stats!.sparklineData!
                            
                            // If we have series data, use it for stacked chart
                            if (series && series.length > 0) {
                              // Calculate max total for any single day
                              const maxValue = Math.max(...sparklineData, 1)
                              
                              return categories.map((dateStr, dayIdx) => {
                                const day = dateStr ? dateStr.split('-')[2]?.replace(/^0/, '') : ''
                                const dayTotal = sparklineData[dayIdx] || 0
                                
                                // Get data for each server for this day
                                const serverData = series
                                  .map(s => ({
                                    name: s.name,
                                    countryCode: s.countryCode,
                                    color: s.color,
                                    value: s.data[dayIdx] || 0
                                  }))
                                  .filter(s => s.value > 0)
                                  .sort((a, b) => b.value - a.value)
                                
                                return (
                                  <div key={dayIdx} className="flex-1 flex flex-col items-center group">
                                    {/* Stacked Bar */}
                                    <div className="w-full h-32 flex flex-col-reverse items-stretch relative">
                                      {serverData.map((server, sIdx) => {
                                        const heightPercent = (server.value / maxValue) * 100
                                        return (
                                          <div
                                            key={server.name}
                                            className="w-full transition-opacity hover:opacity-80"
                                            style={{ 
                                              height: `${heightPercent}%`,
                                              backgroundColor: server.color,
                                              borderTopLeftRadius: sIdx === serverData.length - 1 ? '4px' : '0',
                                              borderTopRightRadius: sIdx === serverData.length - 1 ? '4px' : '0'
                                            }}
                                          />
                                        )
                                      })}
                                      {/* Tooltip on hover */}
                                      <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-1 px-2 py-1.5 bg-dark-900 
                                                    rounded text-xs text-dark-200 whitespace-nowrap opacity-0 group-hover:opacity-100 
                                                    transition-opacity pointer-events-none z-10 min-w-[120px]">
                                        <div className="font-medium mb-1">{dateStr}: {formatBytes(dayTotal)}</div>
                                        {serverData.slice(0, 5).map(s => (
                                          <div key={s.name} className="flex items-center gap-1.5 text-[10px]">
                                            <div className="w-2 h-2 rounded-full flex-shrink-0" style={{ backgroundColor: s.color }} />
                                            <span className="truncate">{s.name}</span>
                                            <span className="text-dark-400 ml-auto">{formatBytes(s.value)}</span>
                                          </div>
                                        ))}
                                        {serverData.length > 5 && (
                                          <div className="text-dark-500 text-[10px] mt-0.5">+{serverData.length - 5} more</div>
                                        )}
                                      </div>
                                    </div>
                                    {/* Date Label */}
                                    <span className="text-[10px] text-dark-500 mt-1">{day}</span>
                                  </div>
                                )
                              })
                            } else {
                              // Fallback to simple chart if no series data
                              const maxValue = Math.max(...sparklineData, 1)
                              return sparklineData.map((value, idx) => {
                                const dateStr = categories[idx]
                                const day = dateStr ? dateStr.split('-')[2]?.replace(/^0/, '') : ''
                                return (
                                  <div key={idx} className="flex-1 flex flex-col items-center">
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
                                    <span className="text-[10px] text-dark-500 mt-1">{day}</span>
                                  </div>
                                )
                              })
                            }
                          })()}
                        </div>
                        
                        {/* Server Legend */}
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
                      <div className="flex items-center justify-between mb-3">
                        <h4 className="text-sm font-medium text-dark-300 flex items-center gap-2">
                          <Users className="w-4 h-4" />
                          {t('remnawave.client_ips')} ({selectedUser.client_ips?.length || selectedUser.ips?.length || 0})
                        </h4>
                        {((selectedUser.client_ips || selectedUser.ips)?.length || 0) > 0 && (
                          <button
                            onClick={() => setClearIpConfirm({ type: 'all' })}
                            disabled={isClearingIp}
                            className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-red-500/10 text-red-400 hover:bg-red-500/20 transition-colors text-xs disabled:opacity-50"
                          >
                            <Trash2 className="w-3.5 h-3.5" />
                            {t('remnawave.clear_all_ips')}
                          </button>
                        )}
                      </div>
                      {(selectedUser.client_ips || selectedUser.ips) && (selectedUser.client_ips || selectedUser.ips).length > 0 ? (() => {
                        const allIps = selectedUser.client_ips || selectedUser.ips
                        // Group IPs by ASN
                        const asnGroupsMap: Record<string, typeof allIps> = {}
                        const ungroupedIps: typeof allIps = []
                        
                        for (const ip of allIps) {
                          if (ip.asn) {
                            const key = ip.asn
                            if (!asnGroupsMap[key]) asnGroupsMap[key] = []
                            asnGroupsMap[key].push(ip)
                          } else {
                            ungroupedIps.push(ip)
                          }
                        }
                        
                        const hasAsnGroups = Object.keys(asnGroupsMap).length > 0
                        const toggleAsn = (asn: string) => {
                          setExpandedAsns(prev => {
                            const next = new Set(prev)
                            if (next.has(asn)) next.delete(asn)
                            else next.add(asn)
                            return next
                          })
                        }
                        
                        // Render a single IP row (reused in both grouped and ungrouped)
                        const renderIpRow = (ip: typeof allIps[0], idx: number) => (
                          <div key={ip.source_ip} className="rounded-lg bg-dark-800 overflow-hidden">
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
                              <button
                                onClick={(e) => { e.stopPropagation(); setClearIpConfirm({ type: 'single', sourceIp: ip.source_ip }); }}
                                disabled={isClearingIp}
                                className="p-2 rounded-lg hover:bg-red-500/20 text-dark-500 hover:text-red-400 transition-colors disabled:opacity-50"
                                title={t('remnawave.clear_ip')}
                              >
                                <Trash2 className="w-4 h-4" />
                              </button>
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
                        )
                        
                        return (
                          <div className="space-y-3">
                            {/* ASN Groups */}
                            {hasAsnGroups && Object.entries(asnGroupsMap)
                              .sort(([, a], [, b]) => b.length - a.length)
                              .map(([asn, ips]) => {
                                const isExpanded = expandedAsns.has(asn)
                                const prefix = ips[0]?.prefix || ''
                                const totalCount = ips.reduce((sum, ip) => sum + ip.total_count, 0)
                                return (
                                  <div key={asn} className="rounded-xl border border-dark-700/50 overflow-hidden">
                                    {/* ASN Header */}
                                    <div
                                      className="flex items-center gap-3 p-3 bg-dark-800/80 hover:bg-dark-700/80 transition-colors cursor-pointer"
                                      onClick={() => toggleAsn(asn)}
                                    >
                                      <button className="p-1 text-dark-500 hover:text-dark-300 transition-colors">
                                        {isExpanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
                                      </button>
                                      <div className="flex items-center gap-2 flex-1 min-w-0">
                                        <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded bg-accent-500/15 text-accent-400 text-xs font-medium">
                                          ASN {asn}
                                        </span>
                                        {prefix && (
                                          <span className="text-dark-500 text-xs font-mono">{prefix}</span>
                                        )}
                                      </div>
                                      <span className="text-dark-400 text-xs">{ips.length} IP</span>
                                      <span className="text-dark-300 font-medium text-sm">{totalCount.toLocaleString()}</span>
                                    </div>
                                    {/* Expanded IPs */}
                                    {isExpanded && (
                                      <div className="border-t border-dark-700/50 p-2 space-y-1">
                                        {ips.map((ip, idx) => renderIpRow(ip, idx))}
                                      </div>
                                    )}
                                  </div>
                                )
                              })}
                            
                            {/* Ungrouped IPs (no ASN) */}
                            {ungroupedIps.length > 0 && (
                              hasAsnGroups ? (
                                <div className="rounded-xl border border-dark-700/50 overflow-hidden">
                                  <div className="p-3 bg-dark-800/50">
                                    <span className="text-dark-400 text-xs font-medium">{t('remnawave.other_ips')} ({ungroupedIps.length})</span>
                                  </div>
                                  <div className="border-t border-dark-700/50 p-2 space-y-1">
                                    {ungroupedIps.map((ip, idx) => renderIpRow(ip, idx))}
                                  </div>
                                </div>
                              ) : (
                                <div className="space-y-2">
                                  {ungroupedIps.map((ip, idx) => renderIpRow(ip, idx))}
                                </div>
                              )
                            )}
                          </div>
                        )
                      })() : (
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
                    
                    {/* Clear IP Confirmation Dialog */}
                    {clearIpConfirm && (
                      <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={() => setClearIpConfirm(null)}>
                        <div className="bg-dark-800 rounded-xl p-6 max-w-md w-full mx-4 shadow-xl" onClick={(e) => e.stopPropagation()}>
                          <h3 className="text-lg font-semibold text-dark-100 mb-2">
                            {clearIpConfirm.type === 'all' ? t('remnawave.confirm_clear_all_ips_title') : t('remnawave.confirm_clear_ip_title')}
                          </h3>
                          <p className="text-dark-400 text-sm mb-4">
                            {clearIpConfirm.type === 'all' 
                              ? t('remnawave.confirm_clear_all_ips', { count: (selectedUser.client_ips || selectedUser.ips)?.length || 0 })
                              : t('remnawave.confirm_clear_ip', { ip: clearIpConfirm.sourceIp })}
                          </p>
                          <div className="flex justify-end gap-3">
                            <button
                              onClick={() => setClearIpConfirm(null)}
                              disabled={isClearingIp}
                              className="px-4 py-2 rounded-lg bg-dark-700 text-dark-200 hover:bg-dark-600 transition-colors text-sm disabled:opacity-50"
                            >
                              {t('common.cancel')}
                            </button>
                            <button
                              onClick={() => {
                                if (clearIpConfirm.type === 'all') {
                                  handleClearUserAllIps(selectedUser.email)
                                } else if (clearIpConfirm.sourceIp) {
                                  handleClearUserIp(selectedUser.email, clearIpConfirm.sourceIp)
                                }
                              }}
                              disabled={isClearingIp}
                              className="px-4 py-2 rounded-lg bg-red-500 text-white hover:bg-red-600 transition-colors text-sm flex items-center gap-2 disabled:opacity-50"
                            >
                              {isClearingIp && <RefreshCw className="w-4 h-4 animate-spin" />}
                              {t('common.delete')}
                            </button>
                          </div>
                        </div>
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
                    <li>• {dbInfo.tables.xray_stats.count.toLocaleString()} {t('remnawave.db_visits').toLowerCase()}</li>
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
