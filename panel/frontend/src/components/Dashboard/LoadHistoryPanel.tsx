import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useTranslation } from 'react-i18next'
import { serversApi, type FleetHistoryPoint, type FleetHistoryResponse } from '../../api/client'
import { useAutoRefresh } from '../../hooks/useAutoRefresh'
import { useChartDisplay } from '../../hooks/useChartDisplay'
import { useRememberedState } from '../../hooks/useRememberedState'
import { createBitsFormatter } from '../../utils/format'
import type { ChartGap } from '../../utils/chartUtils'
import MultiLineChart, { type ChartSeries } from '../Charts/MultiLineChart'
import ChartLoadingOverlay from '../Charts/ChartLoadingOverlay'
import { METRIC_COLORS, NETWORK_COLORS } from '../Charts/chartTheme'
import PeriodSelector from '../ui/PeriodSelector'

export type LoadMetric = 'cpu' | 'memory' | 'network'

// Историю перечитывать не чаще, чем в ней появляется новая точка;
// 0 — шаг автообновления дашборда
const REFRESH_SEC: Record<string, number> = { '1h': 0, '24h': 60, '7d': 300, '30d': 300, '365d': 300 }

const EMPTY_POINTS: FleetHistoryPoint[] = []
const NO_GAPS: ChartGap[] = []

interface LoadHistoryChartProps {
  metric: LoadMetric
  /** Без папки — весь парк */
  folder?: string
  onlineCount: number
  periodKey: string
  className: string
}

function LoadHistoryChart({ metric, folder, onlineCount, periodKey, className }: LoadHistoryChartProps) {
  const { t } = useTranslation()
  const [period, setPeriod] = useRememberedState(periodKey, '24h')
  const [history, setHistory] = useState<FleetHistoryResponse | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const display = useChartDisplay(metric)

  // Ответ прошлого периода, прилетевший после переключения, рисовать нельзя:
  // под новой подписью висело бы старое окно
  const periodRef = useRef(period)
  periodRef.current = period

  const loadHistory = useCallback(async (withLoader: boolean) => {
    if (withLoader) setIsLoading(true)
    try {
      const res = await serversApi.getFleetHistory(period, folder)
      if (periodRef.current === period) setHistory(res.data)
    } catch {
      // Молчаливое обновление оставляет нарисованное — сеть моргнула, график не
      // должен пропасть; при смене периода показываем «нет данных»
      if (withLoader && periodRef.current === period) setHistory(null)
    } finally {
      if (withLoader) setIsLoading(false)
    }
  }, [period, folder])

  useEffect(() => {
    void loadHistory(true)
  }, [loadHistory])

  const refreshHistory = useCallback(() => loadHistory(false), [loadHistory])

  useAutoRefresh(refreshHistory, {
    immediate: false,
    pauseWhenHidden: true,
    customInterval: REFRESH_SEC[period] ? REFRESH_SEC[period] * 1000 : undefined,
  })

  const points = history?.data ?? EMPTY_POINTS
  const bitsFormatter = useMemo(() => createBitsFormatter(t), [t])

  const series = useMemo<ChartSeries[]>(() => {
    if (metric === 'cpu') {
      return [{
        name: t('common.cpu'),
        data: points.map(p => ({ timestamp: p.timestamp, value: p.cpu_usage, peak: p.max_cpu })),
        color: METRIC_COLORS.cpu,
      }]
    }
    if (metric === 'memory') {
      return [{
        name: t('common.ram'),
        data: points.map(p => ({ timestamp: p.timestamp, value: p.memory_percent, peak: p.max_memory_percent })),
        color: METRIC_COLORS.memory,
      }]
    }
    return [
      {
        name: t('common.download'),
        data: points.map(p => ({ timestamp: p.timestamp, value: p.net_rx_bytes_per_sec, peak: p.max_net_rx_bytes_per_sec })),
        color: NETWORK_COLORS.download,
      },
      {
        name: t('common.upload'),
        data: points.map(p => ({ timestamp: p.timestamp, value: p.net_tx_bytes_per_sec, peak: p.max_net_tx_bytes_per_sec })),
        color: NETWORK_COLORS.upload,
      },
    ]
  }, [metric, points, t])

  const isPercentMetric = metric !== 'network'
  const title = t(folder ? `dashboard.folder_chart_${metric}` : `dashboard.fleet_chart_${metric}`)

  return (
    <div className={`relative p-4 rounded-xl border ${className}`}>
      <div className="flex items-center justify-between gap-3 mb-3 flex-wrap">
        <div className="min-w-0">
          <div className="text-sm font-medium text-dark-200">{title}</div>
          <div className="text-xs text-dark-500">{t('dashboard.fleet_chart_scope', { count: onlineCount })}</div>
        </div>
        <PeriodSelector value={period} onChange={setPeriod} />
      </div>
      <MultiLineChart
        series={series}
        display={display}
        height={260}
        period={period}
        gaps={history?.gaps ?? NO_GAPS}
        unit={isPercentMetric ? '%' : ''}
        formatValue={isPercentMetric ? undefined : bitsFormatter}
        yMax={isPercentMetric ? 100 : undefined}
      />
      <ChartLoadingOverlay visible={isLoading} />
    </div>
  )
}

interface LoadHistoryPanelProps extends Omit<LoadHistoryChartProps, 'metric'> {
  /** null — блок свёрнут, история не запрашивается */
  metric: LoadMetric | null
}

/** Раскрывающийся график истории нагрузки парка или папки: CPU, память, сеть */
export default function LoadHistoryPanel({ metric, ...chartProps }: LoadHistoryPanelProps) {
  return (
    <AnimatePresence>
      {metric && (
        // Постоянный key: без него AnimatePresence теряет уходящего ребёнка при
        // ререндере от поллинга дашборда и оставляет свёрнутый график в DOM
        <motion.div
          key="load-history"
          initial={{ height: 0, opacity: 0 }}
          animate={{ height: 'auto', opacity: 1 }}
          exit={{ height: 0, opacity: 0 }}
          transition={{ duration: 0.3 }}
          className="overflow-hidden"
        >
          <LoadHistoryChart metric={metric} {...chartProps} />
        </motion.div>
      )}
    </AnimatePresence>
  )
}
