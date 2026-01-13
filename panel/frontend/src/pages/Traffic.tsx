import { useState, useEffect, useCallback, useMemo } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'
import {
  ArrowLeft,
  Network,
  Plus,
  Trash2,
  RefreshCw,
  Download,
  Upload,
  Activity,
  Server,
  AlertCircle,
  Wifi,
  Radio
} from 'lucide-react'
import { proxyApi, TrafficSummary, ServerMetrics } from '../api/client'
import { useServersStore } from '../stores/serversStore'
import { useTranslation } from 'react-i18next'
import { useAutoRefresh } from '../hooks/useAutoRefresh'
import { formatBytes } from '../utils/format'
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

export default function Traffic() {
  const { uid, serverId } = useParams()
  const navigate = useNavigate()
  const { servers, fetchServers } = useServersStore()
  const { t } = useTranslation()
  
  const [summary, setSummary] = useState<TrafficSummary | null>(null)
  const [trafficHistory, setTrafficHistory] = useState<{ timestamp: string; rx: number; tx: number }[]>([])
  const [metrics, setMetrics] = useState<ServerMetrics | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [isRefreshing, setIsRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [period, setPeriod] = useState('24h')
  const [newPort, setNewPort] = useState('')
  const [isAddingPort, setIsAddingPort] = useState(false)
  
  const server = servers.find(s => s.id === Number(serverId))
  
  const fetchData = useCallback(async () => {
    if (!serverId) return
    
    try {
      const [summaryRes, historyRes, metricsRes] = await Promise.all([
        proxyApi.getTrafficSummary(Number(serverId), 30),
        period === '24h' 
          ? proxyApi.getHourlyTraffic(Number(serverId), { hours: 24 })
          : period === '7d'
          ? proxyApi.getDailyTraffic(Number(serverId), { days: 7 })
          : proxyApi.getDailyTraffic(Number(serverId), { days: 30 }),
        proxyApi.getMetrics(Number(serverId)) // Use cached metrics instead of live
      ])
      
      setSummary(summaryRes.data)
      setMetrics(metricsRes.data)
      
      const history = historyRes.data.data.map(d => ({
        timestamp: d.hour || d.date || d.month || '',
        rx: d.rx_bytes,
        tx: d.tx_bytes
      }))
      setTrafficHistory(history)
      setError(null)
    } catch (err: unknown) {
      const error = err as { response?: { status: number; data?: { detail?: string } } }
      setError(error.response?.data?.detail || t('traffic.failed_fetch'))
    } finally {
      setIsLoading(false)
    }
  }, [serverId, period])
  
  useEffect(() => {
    fetchServers()
    fetchData()
  }, [fetchServers, fetchData])
  
  // Auto-refresh traffic data
  useAutoRefresh(fetchData, { immediate: false })
  
  const handleRefresh = async () => {
    setIsRefreshing(true)
    await fetchData()
    setIsRefreshing(false)
  }
  
  const handleAddPort = async () => {
    const port = parseInt(newPort)
    if (isNaN(port) || port < 1 || port > 65535) return
    
    setIsAddingPort(true)
    try {
      const res = await proxyApi.addTrackedPort(Number(serverId), port)
      if (res.data.success) {
        setNewPort('')
        await fetchData()
      } else {
        setError(res.data.message)
      }
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } }
      setError(error.response?.data?.detail || t('traffic.failed_add_port'))
    } finally {
      setIsAddingPort(false)
    }
  }
  
  const handleRemovePort = async (port: number) => {
    try {
      const res = await proxyApi.removeTrackedPort(Number(serverId), port)
      if (res.data.success) {
        await fetchData()
      } else {
        setError(res.data.message)
      }
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } }
      setError(error.response?.data?.detail || t('traffic.failed_remove_port'))
    }
  }
  
  const networkHistory = useMemo(() => [
    { 
      name: t('common.download'), 
      data: trafficHistory.map(h => ({ timestamp: h.timestamp, value: h.rx })), 
      color: '#10b981' 
    },
    { 
      name: t('common.upload'), 
      data: trafficHistory.map(h => ({ timestamp: h.timestamp, value: h.tx })), 
      color: '#22d3ee' 
    },
  ], [trafficHistory, t])
  
  if (isLoading) {
    return (
      <div className="flex flex-col items-center justify-center h-64 gap-4">
        <div className="relative">
          <motion.div
            className="w-12 h-12 border-2 border-accent-500/30 rounded-full"
            animate={{ rotate: 360 }}
            transition={{ duration: 2, repeat: Infinity, ease: 'linear' }}
          />
          <motion.div
            className="absolute inset-0 w-12 h-12 border-2 border-transparent border-t-accent-500 rounded-full"
            animate={{ rotate: 360 }}
            transition={{ duration: 1, repeat: Infinity, ease: 'linear' }}
          />
        </div>
        <p className="text-dark-400">{t('traffic.loading')}</p>
      </div>
    )
  }
  
  return (
    <motion.div
      variants={containerVariants}
      initial="hidden"
      animate="visible"
    >
      {/* Header */}
      <motion.div className="flex items-center gap-4 mb-6" variants={itemVariants}>
        <motion.button
          onClick={() => navigate(`/${uid}/server/${serverId}`)}
          className="p-2.5 hover:bg-dark-800 rounded-xl text-dark-400 hover:text-dark-200 transition-all"
          whileHover={{ scale: 1.05, x: -2 }}
          whileTap={{ scale: 0.95 }}
        >
          <ArrowLeft className="w-5 h-5" />
        </motion.button>
        <div className="flex-1">
          <h1 className="text-2xl font-bold text-dark-50 flex items-center gap-3">
            <Network className="w-6 h-6 text-accent-500" />
            {t('traffic.title')}
          </h1>
          <p className="text-dark-400 mt-1">{server?.name || t('common.server')}</p>
        </div>
        
        <motion.button
          onClick={handleRefresh}
          className="btn btn-secondary"
          whileHover={{ scale: 1.02 }}
          whileTap={{ scale: 0.98 }}
          disabled={isRefreshing}
        >
          <motion.div
            animate={isRefreshing ? { rotate: 360 } : {}}
            transition={{ duration: 1, repeat: isRefreshing ? Infinity : 0, ease: 'linear' }}
          >
            <RefreshCw className="w-4 h-4" />
          </motion.div>
        </motion.button>
      </motion.div>
      
      <AnimatePresence mode="wait">
        {error && (
          <motion.div
            className="card bg-danger/10 border-danger/30 mb-6"
            initial={{ opacity: 0, y: -10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -10 }}
          >
            <div className="flex items-center gap-3">
              <AlertCircle className="w-5 h-5 text-danger" />
              <span className="text-danger">{error}</span>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
      
      {summary && (
        <motion.div variants={containerVariants}>
          {/* Summary Cards */}
          <motion.div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6" variants={itemVariants}>
            <div className="card">
              <div className="flex items-center gap-2 mb-3">
                <Download className="w-5 h-5 text-success" />
                <span className="text-sm text-dark-400">{t('traffic.total_download', { days: 30 })}</span>
              </div>
              <div className="text-2xl font-bold font-mono text-success">
                {formatBytes(summary.total.rx_bytes)}
              </div>
            </div>
            
            <div className="card">
              <div className="flex items-center gap-2 mb-3">
                <Upload className="w-5 h-5 text-accent-400" />
                <span className="text-sm text-dark-400">{t('traffic.total_upload', { days: 30 })}</span>
              </div>
              <div className="text-2xl font-bold font-mono text-accent-400">
                {formatBytes(summary.total.tx_bytes)}
              </div>
            </div>
            
            <div className="card">
              <div className="flex items-center gap-2 mb-3">
                <Activity className="w-5 h-5 text-purple" />
                <span className="text-sm text-dark-400">{t('traffic.total_traffic', { days: 30 })}</span>
              </div>
              <div className="text-2xl font-bold font-mono text-purple">
                {formatBytes(summary.total.rx_bytes + summary.total.tx_bytes)}
              </div>
            </div>
          </motion.div>
          
          {/* Network Connections */}
          {metrics?.system?.connections_detailed && (
            <motion.div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-6" variants={itemVariants}>
              {/* TCP Connections */}
              <div className="card">
                <h3 className="font-semibold text-dark-100 mb-4 flex items-center gap-2">
                  <Wifi className="w-4 h-4 text-accent-500" />
                  {t('traffic.tcp_connections')}
                  <span className="ml-auto text-lg font-mono text-accent-400">
                    {metrics.system.connections_detailed.tcp.total}
                  </span>
                </h3>
                
                <div className="space-y-2">
                  {/* Established */}
                  <div className="flex items-center justify-between p-2.5 bg-dark-800/50 rounded-lg">
                    <div className="flex items-center gap-2">
                      <div className="w-2 h-2 rounded-full bg-success" />
                      <span className="text-dark-300">{t('traffic.established')}</span>
                    </div>
                    <span className="font-mono text-success">
                      {metrics.system.connections_detailed.tcp.established}
                    </span>
                  </div>
                  
                  {/* Listen */}
                  <div className="flex items-center justify-between p-2.5 bg-dark-800/50 rounded-lg">
                    <div className="flex items-center gap-2">
                      <div className="w-2 h-2 rounded-full bg-accent-500" />
                      <span className="text-dark-300">{t('traffic.listen')}</span>
                    </div>
                    <span className="font-mono text-accent-400">
                      {metrics.system.connections_detailed.tcp.listen}
                    </span>
                  </div>
                  
                  {/* Time Wait */}
                  <div className="flex items-center justify-between p-2.5 bg-dark-800/50 rounded-lg">
                    <div className="flex items-center gap-2">
                      <div className="w-2 h-2 rounded-full bg-warning" />
                      <span className="text-dark-300">{t('traffic.time_wait')}</span>
                    </div>
                    <span className="font-mono text-warning">
                      {metrics.system.connections_detailed.tcp.time_wait}
                    </span>
                  </div>
                  
                  {/* Close Wait */}
                  <div className="flex items-center justify-between p-2.5 bg-dark-800/50 rounded-lg">
                    <div className="flex items-center gap-2">
                      <div className="w-2 h-2 rounded-full bg-orange-400" />
                      <span className="text-dark-300">{t('traffic.close_wait')}</span>
                    </div>
                    <span className="font-mono text-orange-400">
                      {metrics.system.connections_detailed.tcp.close_wait}
                    </span>
                  </div>
                  
                  {/* SYN Sent/Recv */}
                  <div className="flex items-center justify-between p-2.5 bg-dark-800/50 rounded-lg">
                    <div className="flex items-center gap-2">
                      <div className="w-2 h-2 rounded-full bg-blue-400" />
                      <span className="text-dark-300">{t('traffic.syn_sent')}</span>
                    </div>
                    <span className="font-mono text-blue-400">
                      {metrics.system.connections_detailed.tcp.syn_sent}
                    </span>
                  </div>
                  
                  {/* FIN Wait */}
                  <div className="flex items-center justify-between p-2.5 bg-dark-800/50 rounded-lg">
                    <div className="flex items-center gap-2">
                      <div className="w-2 h-2 rounded-full bg-purple" />
                      <span className="text-dark-300">{t('traffic.fin_wait')}</span>
                    </div>
                    <span className="font-mono text-purple">
                      {metrics.system.connections_detailed.tcp.fin_wait}
                    </span>
                  </div>
                  
                  {/* Other */}
                  {metrics.system.connections_detailed.tcp.other > 0 && (
                    <div className="flex items-center justify-between p-2.5 bg-dark-800/50 rounded-lg">
                      <div className="flex items-center gap-2">
                        <div className="w-2 h-2 rounded-full bg-dark-500" />
                        <span className="text-dark-300">{t('traffic.other')}</span>
                      </div>
                      <span className="font-mono text-dark-400">
                        {metrics.system.connections_detailed.tcp.other}
                      </span>
                    </div>
                  )}
                </div>
              </div>
              
              {/* UDP Connections */}
              <div className="card">
                <h3 className="font-semibold text-dark-100 mb-4 flex items-center gap-2">
                  <Radio className="w-4 h-4 text-cyan-500" />
                  {t('traffic.udp_sockets')}
                  <span className="ml-auto text-lg font-mono text-cyan-400">
                    {metrics.system.connections_detailed.udp.total}
                  </span>
                </h3>
                
                <div className="flex flex-col items-center justify-center py-6">
                  <div className="relative">
                    <div className="w-28 h-28 rounded-full bg-gradient-to-br from-cyan-500/20 to-cyan-600/5 flex items-center justify-center">
                      <div className="w-20 h-20 rounded-full bg-gradient-to-br from-cyan-500/30 to-cyan-600/10 flex items-center justify-center">
                        <span className="text-3xl font-bold font-mono text-cyan-400">
                          {metrics.system.connections_detailed.udp.total}
                        </span>
                      </div>
                    </div>
                    <div className="absolute -top-1 -right-1 w-4 h-4 rounded-full bg-cyan-500 animate-pulse" />
                  </div>
                  <span className="mt-4 text-dark-400 text-sm">{t('traffic.active_udp')}</span>
                </div>
                
                <div className="mt-4 p-3 bg-dark-800/50 rounded-lg">
                  <div className="flex items-center gap-2 text-dark-400 text-sm">
                    <Activity className="w-4 h-4" />
                    <span>
                      {t('traffic.udp_info')}
                    </span>
                  </div>
                </div>
              </div>
            </motion.div>
          )}
          
          {/* Traffic Chart */}
          <motion.div className="card mb-6" variants={itemVariants}>
            <div className="flex items-center justify-between mb-4">
              <h3 className="font-semibold text-dark-100 flex items-center gap-2">
                <Network className="w-4 h-4 text-accent-500" />
                {t('traffic.history')}
              </h3>
              <PeriodSelector 
                value={period} 
                onChange={setPeriod}
                options={[
                  { value: '24h', label: '24h' },
                  { value: '7d', label: '7d' },
                  { value: '30d', label: '30d' },
                ]}
              />
            </div>
            <MultiLineChart
              series={networkHistory}
              formatValue={formatBytes}
              height={250}
              period={period}
            />
          </motion.div>
          
          {/* Port Tracking */}
          <motion.div className="card mb-6" variants={itemVariants}>
            <h3 className="font-semibold text-dark-100 mb-4 flex items-center gap-2">
              <Server className="w-4 h-4 text-accent-500" />
              {t('traffic.port_tracking')}
            </h3>
            
            {/* Add Port Form */}
            <div className="flex gap-3 mb-4">
              <input
                type="number"
                value={newPort}
                onChange={(e) => setNewPort(e.target.value)}
                placeholder={t('traffic.port_placeholder')}
                className="input flex-1"
                min="1"
                max="65535"
              />
              <motion.button
                onClick={handleAddPort}
                className="btn btn-primary"
                whileHover={{ scale: 1.02 }}
                whileTap={{ scale: 0.98 }}
                disabled={isAddingPort || !newPort}
              >
                <Plus className="w-4 h-4" />
                {t('traffic.add_port')}
              </motion.button>
            </div>
            
            {/* Tracked Ports List */}
            {summary.tracked_ports.length === 0 ? (
              <div className="text-center py-8 text-dark-500">
                <Network className="w-12 h-12 mx-auto mb-3 opacity-50" />
                <p>{t('traffic.no_tracked_ports')}</p>
                <p className="text-sm mt-1">{t('traffic.add_port_hint')}</p>
              </div>
            ) : (
              <div className="space-y-2">
                {summary.tracked_ports.map(port => {
                  const portData = summary.by_port.find(p => p.port === port)
                  return (
                    <motion.div
                      key={port}
                      className="flex items-center justify-between p-3 bg-dark-800/50 rounded-lg"
                      initial={{ opacity: 0, x: -10 }}
                      animate={{ opacity: 1, x: 0 }}
                    >
                      <div className="flex items-center gap-4">
                        <span className="font-mono text-lg text-dark-100">:{port}</span>
                        {portData && (
                          <div className="flex gap-4 text-sm">
                            <span className="text-success">
                              ↓ {formatBytes(portData.rx_bytes)}
                            </span>
                            <span className="text-accent-400">
                              ↑ {formatBytes(portData.tx_bytes)}
                            </span>
                          </div>
                        )}
                        {!portData && (
                          <span className="text-dark-500 text-sm">{t('traffic.no_data_yet')}</span>
                        )}
                      </div>
                      <motion.button
                        onClick={() => handleRemovePort(port)}
                        className="p-2 hover:bg-danger/20 rounded-lg text-dark-400 hover:text-danger transition-colors"
                        whileHover={{ scale: 1.1 }}
                        whileTap={{ scale: 0.9 }}
                      >
                        <Trash2 className="w-4 h-4" />
                      </motion.button>
                    </motion.div>
                  )
                })}
              </div>
            )}
          </motion.div>
          
          {/* Interface Traffic */}
          <motion.div className="card" variants={itemVariants}>
            <h3 className="font-semibold text-dark-100 mb-4 flex items-center gap-2">
              <Network className="w-4 h-4 text-accent-500" />
              {t('traffic.by_interface', { days: 30 })}
            </h3>
            
            {summary.by_interface.length === 0 ? (
              <div className="text-center py-8 text-dark-500">
                <Network className="w-12 h-12 mx-auto mb-3 opacity-50" />
                <p>{t('traffic.no_interface_data')}</p>
              </div>
            ) : (
              <div className="space-y-3">
                {summary.by_interface.map(iface => (
                  <div
                    key={iface.interface}
                    className="flex items-center justify-between p-3 bg-dark-800/50 rounded-lg"
                  >
                    <span className="font-mono text-dark-200">{iface.interface}</span>
                    <div className="flex gap-6 text-sm">
                      <span className="text-success">
                        ↓ {formatBytes(iface.rx_bytes)}
                      </span>
                      <span className="text-accent-400">
                        ↑ {formatBytes(iface.tx_bytes)}
                      </span>
                      <span className="text-dark-400">
                        {t('traffic.total')}: {formatBytes(iface.rx_bytes + iface.tx_bytes)}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </motion.div>
        </motion.div>
      )}
    </motion.div>
  )
}
