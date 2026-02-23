import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'
import { useTranslation } from 'react-i18next'
import {
  ArrowLeft,
  Cpu,
  MemoryStick,
  HardDrive,
  Network,
  Activity,
  Clock,
  Server,
  RefreshCw,
  Settings,
  ChevronRight,
  Zap,
  Globe,
  Layers,
  Database,
  Power,
  RotateCcw,
  AlertTriangle
} from 'lucide-react'
import { toast } from 'sonner'
import { proxyApi, ServerMetrics } from '../api/client'
import { useServersStore } from '../stores/serversStore'
import { useSmartRefresh } from '../hooks/useAutoRefresh'
import ProgressBar from '../components/ui/ProgressBar'
import StatusBadge from '../components/ui/StatusBadge'
import PeriodSelector from '../components/ui/PeriodSelector'
import MetricChart from '../components/Charts/MetricChart'
import MultiLineChart from '../components/Charts/MultiLineChart'
import ProcessTable from '../components/Processes/ProcessTable'
import CpuCoresChart from '../components/Charts/CpuCoresChart'
import CpuCoresHistoryChart from '../components/Charts/CpuCoresHistoryChart'
import Terminal from '../components/Terminal/Terminal'
import { formatBytes, formatUptime, formatPercent, createBitsFormatter, formatTimeAgo } from '../utils/format'
import { useCachedData, createServerCacheKey } from '../hooks/useCachedData'
import CachedDataBanner from '../components/ui/CachedDataBanner'
import { MetricCardSkeleton, ChartSkeleton } from '../components/ui/Skeleton'

function getLoadColor(percent: number): string {
  if (percent >= 80) return 'text-danger'
  if (percent >= 60) return 'text-warning'
  return 'text-success'
}

interface HistoryData {
  timestamp: string
  cpu_usage: number
  memory_used: number
  memory_available: number
  memory_percent?: number
  net_rx_bytes_per_sec: number
  net_tx_bytes_per_sec: number
  disk_percent?: number
  disk_read_bytes_per_sec: number
  disk_write_bytes_per_sec: number
  process_count?: number
  per_cpu_percent?: number[]
}

interface MetricsCacheData {
  metrics: ServerMetrics | null
  history: HistoryData[]
}

export default function ServerDetails() {
  const { uid, serverId } = useParams()
  const navigate = useNavigate()
  const { servers, fetchServers } = useServersStore()
  const { t } = useTranslation()
  
  const [metrics, setMetrics] = useState<ServerMetrics | null>(null)
  const [history, setHistory] = useState<HistoryData[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [isRefreshing, setIsRefreshing] = useState(false)
  const [isHistoryLoading, setIsHistoryLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [period, setPeriod] = useState('1h')
  
  const [powerAction, setPowerAction] = useState<'reboot' | 'shutdown' | null>(null)
  const [isPowerActionLoading, setIsPowerActionLoading] = useState(false)
  const [powerActionError, setPowerActionError] = useState<string | null>(null)
  
  const cacheKey = serverId ? createServerCacheKey(serverId, 'metrics') : ''
  const { isCached, cachedAt, saveToCache, loadFromCache, setIsCached, setCachedAt } = useCachedData<MetricsCacheData>(cacheKey)
  
  // Refs to avoid stale closures without adding state to useCallback deps
  const metricsRef = useRef(metrics)
  const historyRef = useRef(history)
  metricsRef.current = metrics
  historyRef.current = history
  
  const server = servers.find(s => s.id === Number(serverId))
  
  const handleFetchError = useCallback((err: unknown) => {
    const cached = loadFromCache()
    if (cached && cached.metrics) {
      setMetrics(cached.metrics)
      setHistory(cached.history)
      setError(null)
    } else {
      const error = err as { response?: { status: number; data?: { detail?: string } } }
      const errorCode = error.response?.status
      const detail = error.response?.data?.detail
      
      if (errorCode === 504) {
        setError(t('server_details.connection_timeout'))
      } else if (errorCode === 502) {
        setError(detail || t('server_details.connection_refused'))
      } else if (errorCode === 401 || errorCode === 403) {
        setError(t('server_details.auth_failed'))
      } else {
        setError(detail || t('server_details.fetch_failed'))
      }
    }
  }, [loadFromCache, t])
  
  const fetchLiveData = useCallback(async (historyOnly = false, useCached = false) => {
    if (!serverId) return
    
    try {
      const includePerCpu = period === '1h' || period === '24h'
      
      if (historyOnly) {
        setIsHistoryLoading(true)
        const historyRes = await proxyApi.getHistory(Number(serverId), { period, include_per_cpu: includePerCpu })
        const historyData = historyRes.data.data || []
        setHistory(historyData)
        setIsHistoryLoading(false)
        
        if (metricsRef.current) {
          saveToCache({ metrics: metricsRef.current, history: historyData })
        }
      } else {
        const [metricsRes, historyRes] = await Promise.all([
          useCached 
            ? proxyApi.getMetrics(Number(serverId))
            : proxyApi.getLiveMetrics(Number(serverId)),
          proxyApi.getHistory(Number(serverId), { period, include_per_cpu: includePerCpu }),
        ])
        
        const metricsData = metricsRes.data
        const historyData = historyRes.data.data || []
        
        setMetrics(metricsData)
        setHistory(historyData)
        setError(null)
        setIsCached(false)
        setCachedAt(null)
        saveToCache({ metrics: metricsData, history: historyData })
      }
    } catch (err: unknown) {
      handleFetchError(err)
      setIsHistoryLoading(false)
    } finally {
      setIsLoading(false)
    }
  }, [serverId, period, saveToCache, handleFetchError, setIsCached, setCachedAt])
  
  const fetchCachedData = useCallback(async () => {
    if (!serverId) return
    
    try {
      const metricsRes = await proxyApi.getMetrics(Number(serverId))
      const metricsData = metricsRes.data
      setMetrics(metricsData)
      setError(null)
      setIsCached(false)
      setCachedAt(null)
      saveToCache({ metrics: metricsData, history: historyRef.current })
    } catch (err: unknown) {
      handleFetchError(err)
    }
  }, [serverId, saveToCache, handleFetchError, setIsCached, setCachedAt])
  
  useEffect(() => {
    fetchServers()
    fetchLiveData(false, true)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fetchServers])
  
  const initialLoadDone = useRef(false)
  useEffect(() => {
    if (!initialLoadDone.current) {
      initialLoadDone.current = true
      return
    }
    fetchLiveData(true)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [period])
  
  // Smart refresh: live metrics when page visible, cached when hidden
  const { isPageVisible } = useSmartRefresh(
    async () => {
      setIsRefreshing(true)
      await fetchLiveData()
      setIsRefreshing(false)
    },
    fetchCachedData,
    { immediate: false }
  )
  
  const handleManualRefresh = async () => {
    setIsRefreshing(true)
    await fetchLiveData() // Always use live data for manual refresh
    setIsRefreshing(false)
  }
  
  const handlePowerAction = async () => {
    if (!serverId || !powerAction) return
    
    setIsPowerActionLoading(true)
    setPowerActionError(null)
    
    try {
      const command = powerAction === 'reboot' ? 'reboot' : 'poweroff'
      await proxyApi.executeCommand(Number(serverId), command, 30, 'sh')
      toast.success(powerAction === 'reboot' ? t('server_details.reboot_initiated') : t('server_details.shutdown_initiated'))
      setPowerAction(null)
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } }
      const msg = error.response?.data?.detail || t('server_details.power_action_failed')
      toast.error(msg)
      setPowerActionError(msg)
    } finally {
      setIsPowerActionLoading(false)
    }
  }
  
  // Memoized chart data - must be before any conditional returns to follow React hooks rules
  const cpuHistory = useMemo(() => 
    history.map(h => ({ timestamp: h.timestamp, value: h.cpu_usage || 0 })),
    [history]
  )
  
  const memoryHistory = useMemo(() => 
    history.map(h => ({
      timestamp: h.timestamp,
      value: h.memory_percent || (h.memory_used && h.memory_available 
        ? (h.memory_used / (h.memory_used + h.memory_available)) * 100 
        : 0)
    })),
    [history]
  )
  
  const networkHistory = useMemo(() => [
    { 
      name: t('common.download'), 
      data: history.map(h => ({ 
        timestamp: h.timestamp, 
        value: h.net_rx_bytes_per_sec || 0
      })), 
      color: '#10b981' 
    },
    { 
      name: t('common.upload'), 
      data: history.map(h => ({ 
        timestamp: h.timestamp, 
        value: h.net_tx_bytes_per_sec || 0
      })), 
      color: '#22d3ee' 
    },
  ], [history, t])
  
  // Memoized localized bits formatter
  const bitsFormatter = useMemo(() => createBitsFormatter(t), [t])
  
  if (isLoading) {
    return (
      <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
        {/* Header skeleton */}
        <div className="flex items-center gap-4 mb-6">
          <div className="p-2.5">
            <ArrowLeft className="w-5 h-5 text-dark-600" />
          </div>
          <div className="flex-1 space-y-2">
            <div className="h-6 w-40 bg-dark-700/50 rounded-lg animate-pulse" />
            <div className="h-4 w-56 bg-dark-700/30 rounded-lg animate-pulse" />
          </div>
        </div>
        {/* Metric cards skeleton */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
          {Array.from({ length: 4 }).map((_, i) => (
            <MetricCardSkeleton key={i} />
          ))}
        </div>
        {/* Charts skeleton */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
          <ChartSkeleton />
          <ChartSkeleton />
        </div>
      </motion.div>
    )
  }
  
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
    >
      {/* Header */}
      <motion.div 
        className="flex items-center gap-4 mb-6"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
      >
        <motion.button
          onClick={() => navigate(`/${uid}`)}
          className="p-2.5 hover:bg-dark-800 rounded-xl text-dark-400 hover:text-dark-200 transition-all"
          whileHover={{ scale: 1.05, x: -2 }}
          whileTap={{ scale: 0.95 }}
        >
          <ArrowLeft className="w-5 h-5" />
        </motion.button>
        <div className="flex-1">
          <div className="flex items-center gap-3">
            <motion.h1 
              className="text-2xl font-bold text-dark-50"
              initial={{ opacity: 0, x: -10 }}
              animate={{ opacity: 1, x: 0 }}
            >
              {server?.name || t('common.server')}
            </motion.h1>
            <StatusBadge status={error ? 'offline' : (server?.status || 'online')} />
          </div>
          {metrics && (
            <motion.p 
              className="text-dark-400 mt-1 flex items-center gap-2"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ delay: 0.2 }}
            >
              <Globe className="w-3.5 h-3.5" />
              {metrics.system.hostname} • {metrics.system.os}
            </motion.p>
          )}
        </div>
        
        <motion.div 
          className="flex items-center gap-3"
          initial={{ opacity: 0, x: 20 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ delay: 0.3 }}
        >
          <motion.div 
            className="text-xs text-dark-500 hidden sm:flex items-center gap-1.5 bg-dark-800/40 px-3 py-2 rounded-lg"
            animate={{ opacity: [0.5, 1, 0.5] }}
            transition={{ duration: 2, repeat: Infinity }}
          >
            {isPageVisible ? (
              <>
                <Zap className="w-3.5 h-3.5 text-accent-500" />
                <span className="text-accent-400">{t('common.live')}</span>
              </>
            ) : (
              <>
                <Database className="w-3.5 h-3.5 text-dark-500" />
                <span>{t('common.background')}</span>
              </>
            )}
          </motion.div>
          <motion.button 
            onClick={handleManualRefresh} 
            className="btn btn-secondary"
            whileHover={{ scale: 1.02 }}
            whileTap={{ scale: 0.98 }}
          >
            <motion.div
              animate={isRefreshing ? { rotate: 360 } : {}}
              transition={{ duration: 1, repeat: isRefreshing ? Infinity : 0, ease: 'linear' }}
            >
              <RefreshCw className="w-4 h-4" />
            </motion.div>
          </motion.button>
          
          {/* Power control buttons */}
          <motion.button
            onClick={() => setPowerAction('reboot')}
            className="btn btn-secondary"
            whileHover={{ scale: 1.02 }}
            whileTap={{ scale: 0.98 }}
            title={t('server_details.reboot')}
          >
            <RotateCcw className="w-4 h-4" />
          </motion.button>
          <motion.button
            onClick={() => setPowerAction('shutdown')}
            className="btn btn-secondary text-danger hover:bg-danger/10"
            whileHover={{ scale: 1.02 }}
            whileTap={{ scale: 0.98 }}
            title={t('server_details.shutdown')}
          >
            <Power className="w-4 h-4" />
          </motion.button>
          
          <Link to={`/${uid}/server/${serverId}/traffic`}>
            <motion.div
              className="btn btn-secondary"
              whileHover={{ scale: 1.02 }}
              whileTap={{ scale: 0.98 }}
            >
              <Network className="w-4 h-4" />
              {t('server_details.traffic')}
            </motion.div>
          </Link>
          <Link to={`/${uid}/server/${serverId}/haproxy`}>
            <motion.div
              className="btn btn-primary"
              whileHover={{ scale: 1.02 }}
              whileTap={{ scale: 0.98 }}
            >
              <Settings className="w-4 h-4" />
              {t('haproxy.server_settings')}
              <ChevronRight className="w-4 h-4" />
            </motion.div>
          </Link>
        </motion.div>
      </motion.div>
      
      <AnimatePresence mode="wait">
        {error ? (
          <motion.div 
            className="card text-center py-16"
            initial={{ opacity: 0, scale: 0.95 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.95 }}
            key="error"
          >
            <motion.div
              animate={{ y: [0, -5, 0] }}
              transition={{ duration: 2, repeat: Infinity }}
            >
              <Server className="w-16 h-16 text-danger/50 mx-auto mb-4" />
            </motion.div>
            <h2 className="text-xl font-semibold text-dark-200 mb-2">{t('server_details.server_unavailable')}</h2>
            <p className="text-danger mb-4">{error}</p>
            
            {server?.error_code && (
              <div className="inline-flex items-center gap-2 px-3 py-1.5 bg-dark-800 rounded-lg text-sm mb-3">
                <span className="text-dark-400">{t('server_details.error_code')}:</span>
                <span className="font-mono text-dark-200">{server.error_code}</span>
              </div>
            )}
            
            {server?.last_seen && (
              <p className="text-dark-500 text-sm">
                {t('server_details.last_online')}: {formatTimeAgo(server.last_seen)}
              </p>
            )}
          </motion.div>
        ) : metrics && (
          <motion.div key="content" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
            {/* Cached data indicator */}
            <AnimatePresence>
              {isCached && (
                <CachedDataBanner cachedAt={cachedAt} />
              )}
            </AnimatePresence>
            
            {/* Metric cards */}
            <motion.div 
              className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6"
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.4 }}
            >
              <MetricCard
                icon={<Cpu className="w-5 h-5" />}
                label={t('common.cpu')}
                value={metrics.cpu.usage_percent}
                subtext={`${metrics.cpu.cores_physical}/${metrics.cpu.cores_logical} cores`}
                showCores={metrics.cpu.per_cpu_percent && metrics.cpu.per_cpu_percent.length > 0}
                perCpuPercent={metrics.cpu.per_cpu_percent}
                delay={0}
              />
              
              <MetricCard
                icon={<MemoryStick className="w-5 h-5" />}
                label={t('common.memory')}
                value={metrics.memory.ram.percent}
                subtext={`${formatBytes(metrics.memory.ram.used)} / ${formatBytes(metrics.memory.ram.total)}`}
                delay={0.1}
              />
              
              <MetricCard
                icon={<HardDrive className="w-5 h-5" />}
                label={t('common.disk')}
                value={metrics.disk.partitions[0]?.percent || 0}
                subtext={`${formatBytes(metrics.disk.partitions[0]?.used || 0)} / ${formatBytes(metrics.disk.partitions[0]?.total || 0)}`}
                delay={0.2}
              />
              
              <motion.div 
                className="card"
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.3 }}
              >
                <div className="flex items-center gap-2 mb-3">
                  <Clock className="w-5 h-5 text-accent-500" />
                  <span className="text-base text-dark-400">{t('common.uptime')}</span>
                </div>
                <motion.div 
                  className="text-4xl font-bold font-mono text-dark-100"
                  initial={{ scale: 0.9 }}
                  animate={{ scale: 1 }}
                  transition={{ delay: 0.4, type: 'spring' }}
                >
                  {formatUptime(metrics.system.uptime_seconds)}
                </motion.div>
                <p className="text-sm text-dark-400 mt-4 flex items-center gap-2 font-medium">
                  <Layers className="w-4 h-4" />
                  {metrics.processes.total} proc • TCP: {metrics.system.connections_detailed?.tcp.total ?? metrics.system.connections.established} • UDP: {metrics.system.connections_detailed?.udp.total ?? 0}
                </p>
              </motion.div>
            </motion.div>
            
            {/* Charts section */}
            <motion.div 
              className="flex items-center justify-between mb-6"
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.4 }}
            >
              <h2 className="text-lg font-semibold text-dark-100 flex items-center gap-2">
                <Zap className="w-5 h-5 text-accent-500" />
                {t('server_details.performance_history')}
              </h2>
              <PeriodSelector value={period} onChange={setPeriod} />
            </motion.div>
            
            <motion.div 
              className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6"
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.4 }}
            >
              <div className="space-y-3">
                <ChartCard
                  icon={<Cpu className="w-4 h-4 text-accent-500" />}
                  title={t('server_details.cpu_usage')}
                  isLoading={isHistoryLoading}
                >
                  <MetricChart
                    data={cpuHistory}
                    color="#22d3ee"
                    unit="%"
                    min={0}
                    max={100}
                    period={period}
                  />
                </ChartCard>
                
                {/* Per-CPU cores history (collapsible) */}
                {(period === '1h' || period === '24h') && metrics?.cpu?.per_cpu_percent && (
                  <CpuCoresHistoryChart
                    history={history}
                    period={period}
                    coreCount={metrics.cpu.per_cpu_percent.length}
                    isLoading={isHistoryLoading}
                  />
                )}
              </div>
              
              <ChartCard
                icon={<MemoryStick className="w-4 h-4 text-accent-500" />}
                title={t('server_details.memory_usage')}
                isLoading={isHistoryLoading}
              >
                <MetricChart
                  data={memoryHistory}
                  color="#10b981"
                  unit="%"
                  min={0}
                  max={100}
                  period={period}
                />
              </ChartCard>
            </motion.div>
            
            <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="space-y-3">
              <ChartCard
                icon={<Network className="w-4 h-4 text-accent-500" />}
                title={t('server_details.network_traffic')}
                isLoading={isHistoryLoading}
              >
                <MultiLineChart
                  series={networkHistory}
                  formatValue={bitsFormatter}
                  height={250}
                  period={period}
                />
              </ChartCard>
              
            </motion.div>
            
            {/* Bottom section */}
            <motion.div 
              className="grid grid-cols-1 lg:grid-cols-2 gap-6"
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.4 }}
            >
              <div className="card">
                <h3 className="font-semibold text-dark-100 mb-4 flex items-center gap-2">
                  <Activity className="w-4 h-4 text-accent-500" />
                  {t('server_details.processes')}
                </h3>
                <ProcessTable 
                  processes={[...metrics.processes.top_by_cpu, ...metrics.processes.top_by_memory]
                    .filter((proc, index, self) => 
                      index === self.findIndex(p => p.pid === proc.pid)
                    )
                  } 
                />
              </div>
              
              <div className="card">
                <h3 className="font-semibold text-dark-100 mb-4 flex items-center gap-2">
                  <Server className="w-4 h-4 text-accent-500" />
                  {t('server_details.system_info')}
                </h3>
                <div className="space-y-3 text-sm">
                  <InfoRow label={t('server_details.hostname')} value={metrics.system.hostname} mono />
                  <InfoRow label={t('server_details.os')} value={metrics.system.os} />
                  <InfoRow label={t('server_details.kernel')} value={metrics.system.kernel} mono />
                  <InfoRow label={t('server_details.architecture')} value={metrics.system.architecture} />
                  <InfoRow label={t('server_details.cpu_model')} value={metrics.cpu.model} />
                  {metrics.timezone && (
                    <InfoRow 
                      label={t('server_details.timezone')} 
                      value={`${metrics.timezone.name} (${metrics.timezone.offset})`}
                    />
                  )}
                  {metrics.system.connections_detailed ? (
                    <>
                      <InfoRow 
                        label={t('server_details.tcp_connections')} 
                        value={`${metrics.system.connections_detailed.tcp.total} total (${metrics.system.connections_detailed.tcp.established} est)`}
                      />
                      <InfoRow 
                        label={t('server_details.tcp_states')} 
                        value={`${metrics.system.connections_detailed.tcp.listen} listen / ${metrics.system.connections_detailed.tcp.time_wait} tw / ${metrics.system.connections_detailed.tcp.close_wait} cw`}
                      />
                      <InfoRow 
                        label={t('server_details.udp_sockets')} 
                        value={`${metrics.system.connections_detailed.udp.total}`}
                      />
                    </>
                  ) : (
                    <InfoRow 
                      label={t('server_details.connections')} 
                      value={`${metrics.system.connections.established} est / ${metrics.system.connections.listen} listen`} 
                    />
                  )}
                </div>
              </div>
            </motion.div>
            
            {/* Terminal section */}
            <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="mt-6">
              <Terminal serverId={Number(serverId)} />
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
      
      {/* Power action confirmation modal */}
      <AnimatePresence>
        {powerAction && (
          <motion.div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={() => !isPowerActionLoading && setPowerAction(null)}
          >
            <motion.div
              className="bg-dark-800 rounded-2xl p-6 max-w-md w-full mx-4 border border-dark-700"
              initial={{ scale: 0.9, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              exit={{ scale: 0.9, opacity: 0 }}
              onClick={(e) => e.stopPropagation()}
            >
              <div className="flex items-center gap-3 mb-4">
                <div className={`p-2 rounded-xl ${powerAction === 'shutdown' ? 'bg-danger/20' : 'bg-warning/20'}`}>
                  <AlertTriangle className={`w-6 h-6 ${powerAction === 'shutdown' ? 'text-danger' : 'text-warning'}`} />
                </div>
                <h3 className="text-lg font-semibold text-dark-100">
                  {powerAction === 'reboot' ? t('server_details.reboot') : t('server_details.shutdown')}
                </h3>
              </div>
              
              <p className="text-dark-300 mb-6">
                {powerAction === 'reboot' 
                  ? t('server_details.confirm_reboot')
                  : t('server_details.confirm_shutdown')
                }
              </p>
              
              {powerActionError && (
                <div className="mb-4 p-3 bg-danger/10 border border-danger/30 rounded-lg text-danger text-sm">
                  {powerActionError}
                </div>
              )}
              
              <div className="flex justify-end gap-3">
                <motion.button
                  className="btn btn-secondary"
                  onClick={() => setPowerAction(null)}
                  disabled={isPowerActionLoading}
                  whileHover={{ scale: 1.02 }}
                  whileTap={{ scale: 0.98 }}
                >
                  {t('common.cancel')}
                </motion.button>
                <motion.button
                  className={`btn ${powerAction === 'shutdown' ? 'bg-danger hover:bg-danger/80' : 'bg-warning hover:bg-warning/80'} text-white`}
                  onClick={handlePowerAction}
                  disabled={isPowerActionLoading}
                  whileHover={{ scale: 1.02 }}
                  whileTap={{ scale: 0.98 }}
                >
                  {isPowerActionLoading ? (
                    <motion.div
                      animate={{ rotate: 360 }}
                      transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}
                    >
                      <RefreshCw className="w-4 h-4" />
                    </motion.div>
                  ) : powerAction === 'reboot' ? (
                    <>
                      <RotateCcw className="w-4 h-4" />
                      {t('server_details.reboot')}
                    </>
                  ) : (
                    <>
                      <Power className="w-4 h-4" />
                      {t('server_details.shutdown')}
                    </>
                  )}
                </motion.button>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}

interface MetricCardProps {
  icon: React.ReactNode
  label: string
  value: number
  subtext: string
  delay: number
  showCores?: boolean
  perCpuPercent?: number[]
}

function MetricCard({ icon, label, value, subtext, delay, showCores, perCpuPercent }: MetricCardProps) {
  return (
    <motion.div 
      className="card"
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay }}
    >
      <div className="flex items-center gap-2 mb-3">
        <span className="text-accent-500">{icon}</span>
        <span className="text-base text-dark-400">{label}</span>
      </div>
      <motion.div 
        className={`text-4xl font-bold font-mono ${getLoadColor(value)}`}
        initial={{ scale: 0.9 }}
        animate={{ scale: 1 }}
        transition={{ delay: delay + 0.1, type: 'spring' }}
      >
        {formatPercent(value)}
      </motion.div>
      <ProgressBar value={value} size="md" className="mt-3" animated />
      <p className="text-sm text-dark-400 mt-3 font-medium">{subtext}</p>
      
      {showCores && perCpuPercent && (
        <div className="mt-4 pt-4 border-t border-dark-700/50">
          <CpuCoresChart perCpuPercent={perCpuPercent} />
        </div>
      )}
    </motion.div>
  )
}

interface ChartCardProps {
  icon: React.ReactNode
  title: string
  isLoading: boolean
  className?: string
  children: React.ReactNode
}

function ChartCard({ icon, title, isLoading, className = '', children }: ChartCardProps) {
  return (
    <motion.div 
      className={`card relative transition-opacity duration-200 ${isLoading ? 'opacity-60' : ''} ${className}`}
      whileHover={{ scale: 1.01 }}
      transition={{ duration: 0.2 }}
    >
      <h3 className="font-semibold text-dark-100 mb-4 flex items-center gap-2">
        {icon}
        {title}
      </h3>
      {children}
      <AnimatePresence>
        {isLoading && (
          <motion.div 
            className="absolute inset-0 flex items-center justify-center bg-dark-900/50 backdrop-blur-sm rounded-2xl"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
          >
            <div className="relative">
              <motion.div
                className="w-8 h-8 border-2 border-accent-500/30 rounded-full"
                animate={{ rotate: 360 }}
                transition={{ duration: 1.5, repeat: Infinity, ease: 'linear' }}
              />
              <motion.div
                className="absolute inset-0 w-8 h-8 border-2 border-transparent border-t-accent-500 rounded-full"
                animate={{ rotate: 360 }}
                transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}
              />
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}

interface InfoRowProps {
  label: string
  value: string
  mono?: boolean
}

function InfoRow({ label, value, mono }: InfoRowProps) {
  return (
    <motion.div 
      className="flex justify-between items-start gap-4"
      initial={{ opacity: 0, x: -10 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ duration: 0.3 }}
    >
      <span className="text-dark-400 flex-shrink-0">{label}</span>
      <span className={`text-dark-200 text-right break-words ${mono ? 'font-mono' : ''}`}>
        {value}
      </span>
    </motion.div>
  )
}
