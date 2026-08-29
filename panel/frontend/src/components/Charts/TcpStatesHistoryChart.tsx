import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { Network } from 'lucide-react'
import type { HistoryPoint } from '../../api/client'
import { useChartDisplay } from '../../hooks/useChartDisplay'
import MultiLineChart, { type ChartSeries } from './MultiLineChart'
import CollapsibleChartSection from './CollapsibleChartSection'
import { seriesColor } from './chartTheme'

interface TcpStatesHistoryChartProps {
  history: HistoryPoint[]
  period: string
  isLoading?: boolean
  className?: string
}

const TCP_STATE_KEYS = [
  'tcp_established',
  'tcp_listen',
  'tcp_time_wait',
  'tcp_close_wait',
  'tcp_syn_sent',
  'tcp_syn_recv',
  'tcp_fin_wait',
] as const

export default function TcpStatesHistoryChart({
  history,
  period,
  isLoading = false,
  className = '',
}: TcpStatesHistoryChartProps) {
  const { t } = useTranslation()
  const display = useChartDisplay('tcp')

  // Старая нода состояния TCP не отдаёт — тогда блока нет вовсе
  const hasTcpData = useMemo(
    () => history.some(h => h.tcp_established != null || h.tcp_listen != null),
    [history],
  )

  // Ряды из одних нулей остаются: состав легенды не должен меняться от периода к периоду
  const series = useMemo<ChartSeries[]>(
    () => TCP_STATE_KEYS.map((key, index) => ({
      name: t(`tcp_chart.${key}`),
      data: history.map(h => ({ timestamp: h.timestamp, value: h[key] })),
      color: seriesColor(index),
    })),
    [history, t],
  )

  if (!hasTcpData) return null

  return (
    <CollapsibleChartSection
      icon={<Network className="w-4 h-4 text-accent-500" />}
      title={t('tcp_chart.states_history')}
      isLoading={isLoading}
      className={className}
    >
      <MultiLineChart series={series} display={display} height={300} period={period} />
    </CollapsibleChartSection>
  )
}
