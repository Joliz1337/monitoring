import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
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
  Radio,
  Gauge,
  History
} from 'lucide-react'
import { toast } from 'sonner'
import { proxyApi, trafficApi, TrafficSummary, TrafficStatus, TrafficSeries, ServerMetrics, HistoryPoint, HistoryResponse } from '../api/client'
import { useServersStore } from '../stores/serversStore'
import NodeRestrictedNotice from '../components/servers/NodeRestrictedNotice'
import { nodeAllows } from '../utils/nodeCapabilities'
import { useTranslation } from 'react-i18next'
import { useSmartRefresh } from '../hooks/useAutoRefresh'
import { useChartDisplay } from '../hooks/useChartDisplay'
import { RAW_DISPLAY } from '../config/chartDisplay'
import { formatBytes, createBitsFormatter } from '../utils/format'
import type { ChartGap } from '../utils/chartUtils'
import PeriodSelector from '../components/ui/PeriodSelector'
import { Tooltip } from '../components/ui/Tooltip'
import { FAQIcon } from '../components/FAQ'
import MultiLineChart from '../components/Charts/MultiLineChart'
import TcpStatesHistoryChart from '../components/Charts/TcpStatesHistoryChart'
import { NETWORK_COLORS } from '../components/Charts/chartTheme'
import TrafficUnsupportedNotice from '../components/Traffic/TrafficUnsupportedNotice'

const SUMMARY_DAYS = 30

// Stable identities keep the chart memos below from recomputing on every render
const EMPTY_POINTS: HistoryPoint[] = []
const NO_GAPS: ChartGap[] = []

// Пока перенос истории с ноды не завершён, накопленные бакеты неполные —
// об этом предупреждаем баннером, но страницу не блокируем
const IMPORT_IN_PROGRESS_STATUSES = ['pending', 'node_too_old']

export default function Traffic() {
  const { uid, serverId } = useParams()
  const navigate = useNavigate()
  const { servers, fetchServers } = useServersStore()
  const { t } = useTranslation()
  const networkDisplay = useChartDisplay('network')

  const [status, setStatus] = useState<TrafficStatus | null>(null)
  const [summary, setSummary] = useState<TrafficSummary | null>(null)
  const [series, setSeries] = useState<TrafficSeries | null>(null)
  const [speedHistory, setSpeedHistory] = useState<HistoryResponse | null>(null)
  const [metrics, setMetrics] = useState<ServerMetrics | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [isRefreshing, setIsRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [period, setPeriod] = useState('24h')
  const [speedPeriod, setSpeedPeriod] = useState('1h')
  const [newPort, setNewPort] = useState('')
  const [isAddingPort, setIsAddingPort] = useState(false)

  const server = servers.find(s => s.id === Number(serverId))
  // Графики и сводка считаются из базы панели, поэтому от прав ноды не зависят.
  // Закрыть можно только управление списком отслеживаемых портов.
  const portsReadable = nodeAllows(server, 'traffic', 'read')
  const portsWritable = nodeAllows(server, 'traffic', 'write')

  const fetchSummary = useCallback(async () => {
    if (!serverId) return
    try {
      const { data } = await trafficApi.getSummary(Number(serverId), SUMMARY_DAYS)
      setSummary(data)
      setError(null)
    } catch {
      setError(t('traffic.failed_fetch'))
    }
  }, [serverId, t])

  const fetchSeries = useCallback(async () => {
    if (!serverId) return
    try {
      const { data } = await trafficApi.getSeries(Number(serverId), { period, scope: 'total' })
      setSeries(data)
    } catch {
      // График остаётся с последними данными — ошибку уже показывает баннер summary
    }
  }, [serverId, period])

  const fetchMetrics = useCallback(async () => {
    if (!serverId) return
    try {
      const { data } = await proxyApi.getMetrics(Number(serverId))
      setMetrics(data)
    } catch {
      // Нода офлайн — блок соединений просто не рисуется, трафик остаётся виден
    }
  }, [serverId])

  const fetchSpeedData = useCallback(async () => {
    if (!serverId) return
    try {
      const { data } = await proxyApi.getHistory(Number(serverId), { period: speedPeriod })
      setSpeedHistory(data)
    } catch {
      // График скорости остаётся с последними данными — нода офлайн не должна ронять страницу
    }
  }, [serverId, speedPeriod])

  const refreshAll = useCallback(async () => {
    await Promise.all([fetchSummary(), fetchSeries(), fetchMetrics(), fetchSpeedData()])
  }, [fetchSummary, fetchSeries, fetchMetrics, fetchSpeedData])

  useEffect(() => {
    fetchServers()
  }, [fetchServers])

  // Статус решает, есть ли смысл грузить остальное: на старой ноде витрин панели ещё нет
  useEffect(() => {
    if (!serverId) return
    let cancelled = false

    const init = async () => {
      setIsLoading(true)
      try {
        const { data } = await trafficApi.getStatus(Number(serverId))
        if (cancelled) return
        setStatus(data)
      } catch {
        if (cancelled) return
        setError(t('traffic.failed_fetch'))
        setIsLoading(false)
        return
      }
      await refreshAll()
      if (!cancelled) setIsLoading(false)
    }

    init()
    return () => { cancelled = true }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverId])

  const trafficPeriodMounted = useRef(false)
  useEffect(() => {
    if (!trafficPeriodMounted.current) {
      trafficPeriodMounted.current = true
      return
    }
    fetchSeries()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [period])

  const speedPeriodMounted = useRef(false)
  useEffect(() => {
    if (!speedPeriodMounted.current) {
      speedPeriodMounted.current = true
      return
    }
    fetchSpeedData()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [speedPeriod])

  const liveRefresh = useCallback(async () => {
    setIsRefreshing(true)
    await refreshAll()
    setIsRefreshing(false)
  }, [refreshAll])

  // Во вкладке в фоне обновляем только итоги — графики всё равно не видны
  const backgroundRefresh = useCallback(async () => {
    await fetchSummary()
  }, [fetchSummary])

  useSmartRefresh(liveRefresh, backgroundRefresh, { immediate: false })

  const handleRefresh = async () => {
    setIsRefreshing(true)
    await refreshAll()
    setIsRefreshing(false)
  }

  // tracked_ports панель узнаёт из ответа ноды на следующем цикле коллектора,
  // поэтому список правим локально — иначе порт пропал бы из таблицы до опроса
  const applyTrackedPorts = (update: (ports: number[]) => number[]) => {
    setSummary(prev => prev && { ...prev, tracked_ports: update(prev.tracked_ports) })
  }

  const handleAddPort = async () => {
    const port = parseInt(newPort)
    if (isNaN(port) || port < 1 || port > 65535 || !portsWritable) return

    setIsAddingPort(true)
    try {
      const res = await proxyApi.addTrackedPort(Number(serverId), port)
      if (res.data.success) {
        toast.success(t('traffic.port_added', { port }))
        setNewPort('')
        applyTrackedPorts(ports => [...new Set([...ports, port])].sort((a, b) => a - b))
      } else {
        setError(res.data.message)
        toast.error(t('traffic.failed_add_port'))
      }
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } }
      setError(error.response?.data?.detail || t('traffic.failed_add_port'))
      toast.error(t('traffic.failed_add_port'))
    } finally {
      setIsAddingPort(false)
    }
  }

  const handleRemovePort = async (port: number) => {
    if (!portsWritable) return
    try {
      const res = await proxyApi.removeTrackedPort(Number(serverId), port)
      if (res.data.success) {
        toast.success(t('traffic.port_removed', { port }))
        applyTrackedPorts(ports => ports.filter(p => p !== port))
      } else {
        setError(res.data.message)
        toast.error(t('traffic.failed_remove_port'))
      }
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } }
      setError(error.response?.data?.detail || t('traffic.failed_remove_port'))
      toast.error(t('traffic.failed_remove_port'))
    }
  }

  const trafficGaps = series?.gaps ?? []

  const networkHistory = useMemo(() => {
    const points = series?.points ?? []
    return [
      {
        name: t('common.download'),
        data: points.map(p => ({ timestamp: p.timestamp, value: p.rx })),
        color: NETWORK_COLORS.download,
      },
      {
        name: t('common.upload'),
        data: points.map(p => ({ timestamp: p.timestamp, value: p.tx })),
        color: NETWORK_COLORS.upload,
      },
    ]
  }, [series, t])

  const speedPoints = speedHistory?.data ?? EMPTY_POINTS
  const speedGaps = speedHistory?.gaps ?? NO_GAPS

  const speedSeries = useMemo(() => [
    {
      name: t('common.download'),
      data: speedPoints.map(h => ({
        timestamp: h.timestamp,
        value: h.net_rx_bytes_per_sec,
        peak: h.max_net_rx_bytes_per_sec,
      })),
      color: NETWORK_COLORS.download,
    },
    {
      name: t('common.upload'),
      data: speedPoints.map(h => ({
        timestamp: h.timestamp,
        value: h.net_tx_bytes_per_sec,
        peak: h.max_net_tx_bytes_per_sec,
      })),
      color: NETWORK_COLORS.upload,
    },
  ], [speedPoints, t])

  const formatSpeed = useMemo(() => createBitsFormatter(t), [t])

  const header = (
    <motion.div className="flex items-center gap-4 mb-6" initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }}>
      <Tooltip label={t('common.back')}>
        <motion.button
          onClick={() => navigate(`/${uid}/server/${serverId}`)}
          className="p-2.5 hover:bg-dark-800 rounded-xl text-dark-400 hover:text-dark-200 transition-all"
          whileHover={{ scale: 1.05, x: -2 }}
          whileTap={{ scale: 0.95 }}
        >
          <ArrowLeft className="w-5 h-5" />
        </motion.button>
      </Tooltip>
      <div className="flex-1">
        <h1 className="text-2xl font-bold text-dark-50 flex items-center gap-3">
          <Network className="w-6 h-6 text-accent-500" />
          {t('traffic.title')}
          <FAQIcon screen="PAGE_TRAFFIC" />
        </h1>
        <p className="text-dark-400 mt-1">{server?.name || t('common.server')}</p>
      </div>

      {status?.supported && (
        <Tooltip label={t('common.refresh_data')}>
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
        </Tooltip>
      )}
    </motion.div>
  )

  if (isLoading) {
    return (
      <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
        <div className="flex items-center gap-4 mb-6">
          <div className="p-2.5"><ArrowLeft className="w-5 h-5 text-dark-600" /></div>
          <div className="flex-1 space-y-2">
            <div className="h-6 w-48 bg-dark-700/50 rounded-lg animate-pulse" />
            <div className="h-4 w-32 bg-dark-700/30 rounded-lg animate-pulse" />
          </div>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="card p-5 space-y-3">
              <div className="h-3 w-20 bg-dark-700/50 rounded animate-pulse" />
              <div className="h-8 w-28 bg-dark-700/30 rounded animate-pulse" />
            </div>
          ))}
        </div>
        <div className="card p-5 mb-6">
          <div className="h-[250px] w-full bg-dark-700/30 rounded-xl animate-pulse" />
        </div>
      </motion.div>
    )
  }

  const importInProgress = status ? IMPORT_IN_PROGRESS_STATUSES.includes(status.import.status) : false

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
    >
      {header}

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

      {importInProgress && (
        <motion.div
          className="flex items-center gap-2.5 px-4 py-2.5 mb-6 bg-accent-500/10 border border-accent-500/25 rounded-xl text-sm text-accent-300"
          initial={{ opacity: 0, y: -10 }}
          animate={{ opacity: 1, y: 0 }}
        >
          <History className="w-4 h-4 flex-shrink-0" />
          <span>{t('traffic.import_in_progress')}</span>
        </motion.div>
      )}

      {summary && (
        <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
          {/* Summary Cards */}
          <motion.div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6" initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }}>
            <div className="card">
              <div className="flex items-center gap-2 mb-3">
                <Download className="w-5 h-5 text-success" />
                <span className="text-sm text-dark-400">{t('traffic.total_download', { days: summary.days })}</span>
              </div>
              <div className="text-2xl font-bold font-mono text-success">
                {formatBytes(summary.total.rx_bytes)}
              </div>
            </div>

            <div className="card">
              <div className="flex items-center gap-2 mb-3">
                <Upload className="w-5 h-5 text-accent-400" />
                <span className="text-sm text-dark-400">{t('traffic.total_upload', { days: summary.days })}</span>
              </div>
              <div className="text-2xl font-bold font-mono text-accent-400">
                {formatBytes(summary.total.tx_bytes)}
              </div>
            </div>

            <div className="card">
              <div className="flex items-center gap-2 mb-3">
                <Activity className="w-5 h-5 text-purple" />
                <span className="text-sm text-dark-400">{t('traffic.total_traffic', { days: summary.days })}</span>
              </div>
              <div className="text-2xl font-bold font-mono text-purple">
                {formatBytes(summary.total.rx_bytes + summary.total.tx_bytes)}
              </div>
            </div>
          </motion.div>

          {/* Network Connections */}
          {metrics?.system?.connections_detailed && (
            <motion.div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-6" initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }}>
              {/* TCP Connections */}
              <div className="card">
                <h3 className="font-semibold text-dark-100 mb-4 flex items-center gap-2">
                  <Wifi className="w-4 h-4 text-accent-500" />
                  {t('traffic.tcp_connections')}
                  <FAQIcon screen="TRAFFIC_TCP_STATES" size="sm" />
                  <span className="ml-auto text-lg font-mono text-accent-400">
                    {metrics.system.connections_detailed.tcp.total}
                  </span>
                </h3>

                <div className="space-y-2">
                  <div className="flex items-center justify-between p-2.5 bg-dark-800/50 rounded-lg">
                    <div className="flex items-center gap-2">
                      <div className="w-2 h-2 rounded-full bg-success" />
                      <span className="text-dark-300">{t('traffic.established')}</span>
                    </div>
                    <span className="font-mono text-success">
                      {metrics.system.connections_detailed.tcp.established}
                    </span>
                  </div>

                  <div className="flex items-center justify-between p-2.5 bg-dark-800/50 rounded-lg">
                    <div className="flex items-center gap-2">
                      <div className="w-2 h-2 rounded-full bg-accent-500" />
                      <span className="text-dark-300">{t('traffic.listen')}</span>
                    </div>
                    <span className="font-mono text-accent-400">
                      {metrics.system.connections_detailed.tcp.listen}
                    </span>
                  </div>

                  <div className="flex items-center justify-between p-2.5 bg-dark-800/50 rounded-lg">
                    <div className="flex items-center gap-2">
                      <div className="w-2 h-2 rounded-full bg-warning" />
                      <span className="text-dark-300">{t('traffic.time_wait')}</span>
                    </div>
                    <span className="font-mono text-warning">
                      {metrics.system.connections_detailed.tcp.time_wait}
                    </span>
                  </div>

                  <div className="flex items-center justify-between p-2.5 bg-dark-800/50 rounded-lg">
                    <div className="flex items-center gap-2">
                      <div className="w-2 h-2 rounded-full bg-orange-400" />
                      <span className="text-dark-300">{t('traffic.close_wait')}</span>
                    </div>
                    <span className="font-mono text-orange-400">
                      {metrics.system.connections_detailed.tcp.close_wait}
                    </span>
                  </div>

                  <div className="flex items-center justify-between p-2.5 bg-dark-800/50 rounded-lg">
                    <div className="flex items-center gap-2">
                      <div className="w-2 h-2 rounded-full bg-blue-400" />
                      <span className="text-dark-300">{t('traffic.syn_sent')}</span>
                    </div>
                    <span className="font-mono text-blue-400">
                      {metrics.system.connections_detailed.tcp.syn_sent}
                    </span>
                  </div>

                  <div className="flex items-center justify-between p-2.5 bg-dark-800/50 rounded-lg">
                    <div className="flex items-center gap-2">
                      <div className="w-2 h-2 rounded-full bg-purple" />
                      <span className="text-dark-300">{t('traffic.fin_wait')}</span>
                    </div>
                    <span className="font-mono text-purple">
                      {metrics.system.connections_detailed.tcp.fin_wait}
                    </span>
                  </div>

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
        </motion.div>
      )}

      {/* Скорость и TCP берутся из истории метрик панели — видны и без сводки трафика */}
      <motion.div className="card mb-6" initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }}>
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-semibold text-dark-100 flex items-center gap-2">
            <Gauge className="w-4 h-4 text-accent-500" />
            {t('traffic.network_speed')}
          </h3>
          <PeriodSelector
            value={speedPeriod}
            onChange={setSpeedPeriod}
            options={[
              { value: '1h', label: '1h' },
              { value: '24h', label: '24h' },
              { value: '7d', label: '7d' },
              { value: '30d', label: '30d' },
              { value: '365d', label: '1y' },
            ]}
          />
        </div>
        <MultiLineChart
          series={speedSeries}
          gaps={speedGaps}
          display={networkDisplay}
          formatValue={formatSpeed}
          height={250}
          period={speedPeriod}
        />
      </motion.div>

      <motion.div className="mb-6" initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }}>
        <TcpStatesHistoryChart
          history={speedPoints}
          period={speedPeriod}
          isLoading={isRefreshing}
        />
      </motion.div>

      {summary && (
        <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
          {/* Traffic Chart */}
          <motion.div className="card mb-6" initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }}>
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
                  { value: '365d', label: '1y' },
                ]}
              />
            </div>
            {/* Сумма байт за бакет — сглаживать нечестно, рисуется как есть */}
            <MultiLineChart
              series={networkHistory}
              gaps={trafficGaps}
              display={RAW_DISPLAY}
              formatValue={formatBytes}
              height={250}
              period={period}
            />
            {trafficGaps.length > 0 && (
              <p className="mt-2 text-xs text-dark-500">
                {t('traffic.gaps_note', { count: trafficGaps.length })}
              </p>
            )}
          </motion.div>

          {/* Port Tracking */}
          <motion.div className="card mb-6" initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }}>
            <h3 className="font-semibold text-dark-100 mb-4 flex items-center gap-2">
              <Server className="w-4 h-4 text-accent-500" />
              {t('traffic.port_tracking')}
              <FAQIcon screen="TRAFFIC_PORT_TRACKING" size="sm" />
            </h3>

            {/* Учёт по портам читает счётчики iptables, их отдаёт только агент 10.13.0+.
                Суммарный трафик и интерфейсы выше работают на ноде любой версии. */}
            {!portsReadable ? (
              <NodeRestrictedNotice server={server} compact />
            ) : status && !status.supported ? (
              <TrafficUnsupportedNotice nodeVersion={status.node_version} minVersion={status.min_version} />
            ) : (
            <>
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
                className="btn btn-primary disabled:opacity-40 disabled:cursor-not-allowed"
                whileHover={{ scale: 1.02 }}
                whileTap={{ scale: 0.98 }}
                disabled={isAddingPort || !newPort || !portsWritable}
                title={portsWritable ? undefined : t('node_caps.write_blocked')}
              >
                <Plus className="w-4 h-4" />
                {t('traffic.add_port')}
              </motion.button>
            </div>

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
                      <Tooltip label={portsWritable ? t('common.delete') : t('node_caps.write_blocked')}>
                        <motion.button
                          onClick={() => handleRemovePort(port)}
                          disabled={!portsWritable}
                          className={`p-2 rounded-lg text-dark-400 transition-colors ${
                            portsWritable ? 'hover:bg-danger/20 hover:text-danger' : 'opacity-40 cursor-not-allowed'
                          }`}
                          whileHover={{ scale: portsWritable ? 1.1 : 1 }}
                          whileTap={{ scale: portsWritable ? 0.9 : 1 }}
                        >
                          <Trash2 className="w-4 h-4" />
                        </motion.button>
                      </Tooltip>
                    </motion.div>
                  )
                })}
              </div>
            )}
            </>
            )}
          </motion.div>

          {/* Interface Traffic */}
          <motion.div className="card" initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }}>
            <h3 className="font-semibold text-dark-100 mb-4 flex items-center gap-2">
              <Network className="w-4 h-4 text-accent-500" />
              {t('traffic.by_interface', { days: summary.days })}
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
