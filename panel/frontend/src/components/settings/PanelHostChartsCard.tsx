import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { Cpu, MemoryStick, Activity, LineChart, type LucideIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { systemApi, type HostHistoryPeriod, type PanelHostHistoryPoint, type PanelHostHistoryResponse } from '../../api/client'
import { useAutoRefresh } from '../../hooks/useAutoRefresh'
import { resolveChartDisplay } from '../../config/chartDisplay'
import { METRIC_COLORS } from '../Charts/chartTheme'
import MetricChart from '../Charts/MetricChart'
import ChartLoadingOverlay from '../Charts/ChartLoadingOverlay'
import PeriodSelector from '../ui/PeriodSelector'
import type { ChartGap } from '../../utils/chartUtils'
import { SettingsSection } from './SettingsSection'

const HOST_HISTORY_PERIODS: HostHistoryPeriod[] = ['1h', '24h', '7d', '30d']
const DEFAULT_PERIOD: HostHistoryPeriod = '1h'
// Без настроек: всегда сглаженная линия по средним и полоса пиков
const HOST_CHART_DISPLAY = resolveChartDisplay('smooth', true)
const REFRESH_MS: Record<HostHistoryPeriod, number> = { '1h': 30_000, '24h': 60_000, '7d': 300_000, '30d': 300_000 }
const CHART_HEIGHT = 180
const EMPTY_POINTS: PanelHostHistoryPoint[] = []
const NO_GAPS: ChartGap[] = []

interface HostChartProps {
  icon: LucideIcon
  title: string
  loading: boolean
  children: ReactNode
}

function HostChart({ icon: Icon, title, loading, children }: HostChartProps) {
  return (
    <div className="relative p-3 bg-dark-800/40 rounded-xl border border-dark-700/50">
      <div className="flex items-center gap-2 mb-2">
        <Icon className="w-4 h-4 text-accent-500" />
        <span className="text-sm text-dark-300">{title}</span>
      </div>
      {children}
      <ChartLoadingOverlay visible={loading} />
    </div>
  )
}

export function PanelHostChartsCard({ className = '' }: { className?: string }) {
  const { t } = useTranslation()
  const [period, setPeriod] = useState<HostHistoryPeriod>(DEFAULT_PERIOD)
  const [history, setHistory] = useState<PanelHostHistoryResponse | null>(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async (showLoading: boolean) => {
    if (showLoading) setLoading(true)
    try {
      const res = await systemApi.getStatsHistory(period)
      setHistory(res.data)
    } catch (err) {
      console.error('Failed to fetch panel host history:', err)
    } finally {
      setLoading(false)
    }
  }, [period])

  // Смена периода — с оверлеем; фоновое обновление — без, чтобы графики не мигали
  useEffect(() => { load(true) }, [load])
  useAutoRefresh(() => load(false), { customInterval: REFRESH_MS[period], immediate: false })

  const periodOptions = useMemo(
    () => HOST_HISTORY_PERIODS.map(value => ({ value, label: t(`period.${value}`) })),
    [t],
  )

  const points = history?.data ?? EMPTY_POINTS
  const gaps = history?.gaps ?? NO_GAPS

  const cpuSeries = useMemo(
    () => points.map(p => ({ timestamp: p.timestamp, value: p.cpu_usage, peak: p.max_cpu })),
    [points],
  )
  const memorySeries = useMemo(
    () => points.map(p => ({ timestamp: p.timestamp, value: p.memory_percent, peak: p.max_memory_percent })),
    [points],
  )
  const loadSeries = useMemo(
    () => points.map(p => ({ timestamp: p.timestamp, value: p.load_avg_1, peak: p.max_load })),
    [points],
  )

  return (
    <SettingsSection
      icon={LineChart}
      title={t('settings.host_history')}
      description={t('settings.host_history_desc')}
      right={<PeriodSelector value={period} onChange={value => setPeriod(value as HostHistoryPeriod)} options={periodOptions} />}
      className={className}
    >
      <div className="grid grid-cols-1 xl:grid-cols-3 gap-3">
        <HostChart icon={Cpu} title="CPU" loading={loading}>
          <MetricChart data={cpuSeries} color={METRIC_COLORS.cpu} unit="%" min={0} max={100} height={CHART_HEIGHT} period={period} gaps={gaps} display={HOST_CHART_DISPLAY} />
        </HostChart>
        <HostChart icon={MemoryStick} title="RAM" loading={loading}>
          <MetricChart data={memorySeries} color={METRIC_COLORS.memory} unit="%" min={0} max={100} height={CHART_HEIGHT} period={period} gaps={gaps} display={HOST_CHART_DISPLAY} />
        </HostChart>
        <HostChart icon={Activity} title={t('settings.load_avg')} loading={loading}>
          <MetricChart data={loadSeries} color={METRIC_COLORS.load} min={0} height={CHART_HEIGHT} period={period} gaps={gaps} display={HOST_CHART_DISPLAY} />
        </HostChart>
      </div>
    </SettingsSection>
  )
}
