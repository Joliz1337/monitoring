import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Cpu, MemoryStick, ArrowDownToLine, ArrowUpFromLine } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { serversApi, type FleetHistoryPoint, type FleetHistoryResponse } from '../../api/client'
import type { ServerWithMetrics } from '../../stores/serversStore'
import { useAutoRefresh } from '../../hooks/useAutoRefresh'
import { useChartDisplay } from '../../hooks/useChartDisplay'
import { formatBytes, formatBitsPerSecLocalized, createBitsFormatter } from '../../utils/format'
import type { ChartGap } from '../../utils/chartUtils'
import MultiLineChart, { type ChartSeries } from '../Charts/MultiLineChart'
import ChartLoadingOverlay from '../Charts/ChartLoadingOverlay'
import { METRIC_COLORS, NETWORK_COLORS } from '../Charts/chartTheme'
import PeriodSelector from '../ui/PeriodSelector'
import { Tooltip } from '../ui/Tooltip'

type FleetMetric = 'cpu' | 'memory' | 'network'
type FleetTile = 'cpu' | 'memory' | 'rx' | 'tx'

// Приём и отдача — один график с двумя линиями, но плитки разные: свернуть
// должен только повторный клик по той же плитке, а не по соседней
const TILE_METRIC: Record<FleetTile, FleetMetric> = { cpu: 'cpu', memory: 'memory', rx: 'network', tx: 'network' }

// Историю перечитывать не чаще, чем в ней появляется новая точка;
// 0 — шаг автообновления дашборда
const REFRESH_SEC: Record<string, number> = { '1h': 0, '24h': 60, '7d': 300, '30d': 300, '365d': 300 }

const EMPTY_POINTS: FleetHistoryPoint[] = []
const NO_GAPS: ChartGap[] = []

interface StatTileProps {
  icon: React.ReactNode
  iconBg: string
  label: string
  value: string
  sub?: string
  tile: FleetTile
  isActive: boolean
  onSelect: (tile: FleetTile) => void
}

function StatTile({ icon, iconBg, label, value, sub, tile, isActive, onSelect }: StatTileProps) {
  return (
    <button
      type="button"
      onClick={() => onSelect(tile)}
      aria-expanded={isActive}
      className={`bg-dark-900/50 border rounded-xl px-4 py-3 flex items-center gap-3 min-w-0 text-left transition-colors ${
        isActive ? 'border-accent-500/60 bg-dark-800/60' : 'border-dark-800/50 hover:border-dark-700'
      }`}
    >
      <div className={`w-9 h-9 rounded-lg ${iconBg} flex items-center justify-center flex-shrink-0`}>
        {icon}
      </div>
      <div className="min-w-0 flex-1">
        <div className="text-xs text-dark-400">{label}</div>
        <Tooltip label={sub ? `${value} · ${sub}` : value}>
          <div className="text-sm font-mono text-dark-100 truncate">
            {value}
            {sub && <span className="text-dark-500"> · {sub}</span>}
          </div>
        </Tooltip>
      </div>
    </button>
  )
}

function FleetSummaryInner({ servers }: { servers: ServerWithMetrics[] }) {
  const { t } = useTranslation()
  const [expanded, setExpanded] = useState<FleetTile | null>(null)
  const [period, setPeriod] = useState('24h')
  const [history, setHistory] = useState<FleetHistoryResponse | null>(null)
  const [isHistoryLoading, setIsHistoryLoading] = useState(false)

  const cpuDisplay = useChartDisplay('cpu')
  const memoryDisplay = useChartDisplay('memory')
  const networkDisplay = useChartDisplay('network')

  const totals = useMemo(() => {
    let count = 0
    let cores = 0
    let cpuWeighted = 0
    let ramUsed = 0
    let ramTotal = 0
    let rx = 0
    let tx = 0

    for (const s of servers) {
      if (!s.is_active || s.status !== 'online' || !s.metrics) continue
      const m = s.metrics
      count++
      const serverCores = m.cpu.cores_logical > 0 ? m.cpu.cores_logical : 1
      cores += serverCores
      cpuWeighted += (m.cpu.usage_percent || 0) * serverCores
      ramUsed += m.memory.ram.used || 0
      ramTotal += m.memory.ram.total || 0
      rx += m.network.total?.rx_bytes_per_sec || 0
      tx += m.network.total?.tx_bytes_per_sec || 0
    }

    if (count === 0) return null

    return {
      count,
      cores,
      cpuPercent: cpuWeighted / cores,
      ramUsed,
      ramTotal,
      ramPercent: ramTotal > 0 ? (ramUsed / ramTotal) * 100 : 0,
      rx,
      tx,
    }
  }, [servers])

  const isOpen = expanded !== null

  // Ответ прошлого периода, прилетевший после переключения, рисовать нельзя:
  // под новой подписью висело бы старое окно
  const periodRef = useRef(period)
  periodRef.current = period

  const loadHistory = useCallback(async (withLoader: boolean) => {
    if (withLoader) setIsHistoryLoading(true)
    try {
      const res = await serversApi.getFleetHistory(period)
      if (periodRef.current === period) setHistory(res.data)
    } catch {
      // Молчаливое обновление оставляет нарисованное — сеть моргнула, график не
      // должен пропасть; при смене периода показываем «нет данных»
      if (withLoader && periodRef.current === period) setHistory(null)
    } finally {
      if (withLoader) setIsHistoryLoading(false)
    }
  }, [period])

  useEffect(() => {
    if (isOpen) void loadHistory(true)
  }, [isOpen, loadHistory])

  const refreshHistory = useCallback(() => loadHistory(false), [loadHistory])

  useAutoRefresh(refreshHistory, {
    enabled: isOpen,
    immediate: false,
    pauseWhenHidden: true,
    customInterval: REFRESH_SEC[period] ? REFRESH_SEC[period] * 1000 : undefined,
  })

  const points = history?.data ?? EMPTY_POINTS
  const gaps = history?.gaps ?? NO_GAPS
  const bitsFormatter = useMemo(() => createBitsFormatter(t), [t])

  const series = useMemo<Record<FleetMetric, ChartSeries[]>>(() => ({
    cpu: [{
      name: t('common.cpu'),
      data: points.map(p => ({ timestamp: p.timestamp, value: p.cpu_usage, peak: p.max_cpu })),
      color: METRIC_COLORS.cpu,
    }],
    memory: [{
      name: t('common.ram'),
      data: points.map(p => ({ timestamp: p.timestamp, value: p.memory_percent, peak: p.max_memory_percent })),
      color: METRIC_COLORS.memory,
    }],
    network: [
      {
        name: t('common.download'),
        data: points.map(p => ({
          timestamp: p.timestamp,
          value: p.net_rx_bytes_per_sec,
          peak: p.max_net_rx_bytes_per_sec,
        })),
        color: NETWORK_COLORS.download,
      },
      {
        name: t('common.upload'),
        data: points.map(p => ({
          timestamp: p.timestamp,
          value: p.net_tx_bytes_per_sec,
          peak: p.max_net_tx_bytes_per_sec,
        })),
        color: NETWORK_COLORS.upload,
      },
    ],
  }), [points, t])

  if (!totals) return null

  const toggle = (tile: FleetTile) => setExpanded(current => (current === tile ? null : tile))

  const metric = expanded && TILE_METRIC[expanded]
  const isPercentMetric = metric !== 'network'
  const chartTitle = metric ? t(`dashboard.fleet_chart_${metric}`) : ''
  const chartDisplay = metric === 'cpu' ? cpuDisplay : metric === 'memory' ? memoryDisplay : networkDisplay

  return (
    <div className="mb-6 fade-in">
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <StatTile
          icon={<Cpu className="w-4 h-4 text-accent-400" />}
          iconBg="bg-accent-500/15"
          label={t('common.cpu')}
          value={`${totals.cpuPercent.toFixed(0)}%`}
          sub={t('common.cores_count', { count: totals.cores })}
          tile="cpu"
          isActive={expanded === 'cpu'}
          onSelect={toggle}
        />
        <StatTile
          icon={<MemoryStick className="w-4 h-4 text-purple" />}
          iconBg="bg-purple/15"
          label={t('common.ram')}
          value={`${formatBytes(totals.ramUsed, 0)} / ${formatBytes(totals.ramTotal, 0)}`}
          sub={`${totals.ramPercent.toFixed(0)}%`}
          tile="memory"
          isActive={expanded === 'memory'}
          onSelect={toggle}
        />
        <StatTile
          icon={<ArrowDownToLine className="w-4 h-4 text-success" />}
          iconBg="bg-success/15"
          label={t('common.download')}
          value={formatBitsPerSecLocalized(totals.rx, t)}
          tile="rx"
          isActive={expanded === 'rx'}
          onSelect={toggle}
        />
        <StatTile
          icon={<ArrowUpFromLine className="w-4 h-4 text-accent-400" />}
          iconBg="bg-accent-500/15"
          label={t('common.upload')}
          value={formatBitsPerSecLocalized(totals.tx, t)}
          tile="tx"
          isActive={expanded === 'tx'}
          onSelect={toggle}
        />
      </div>

      <AnimatePresence>
        {metric && (
          // Постоянный key: без него AnimatePresence теряет уходящего ребёнка при
          // ререндере от поллинга дашборда и оставляет свёрнутый график в DOM
          <motion.div
            key="fleet-chart"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.3 }}
            className="overflow-hidden"
          >
            <div className="mt-3 p-4 bg-dark-900/50 border border-dark-800/50 rounded-xl relative">
              <div className="flex items-center justify-between gap-3 mb-3 flex-wrap">
                <div className="min-w-0">
                  <div className="text-sm font-medium text-dark-200">{chartTitle}</div>
                  <div className="text-xs text-dark-500">
                    {t('dashboard.fleet_chart_scope', { count: totals.count })}
                  </div>
                </div>
                <PeriodSelector value={period} onChange={setPeriod} />
              </div>
              <MultiLineChart
                series={series[metric]}
                display={chartDisplay}
                height={260}
                period={period}
                gaps={gaps}
                unit={isPercentMetric ? '%' : ''}
                formatValue={isPercentMetric ? undefined : bitsFormatter}
                yMax={isPercentMetric ? 100 : undefined}
              />
              <ChartLoadingOverlay visible={isHistoryLoading} />
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

const FleetSummary = memo(FleetSummaryInner)
export default FleetSummary
