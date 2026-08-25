import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import type { ChartGap } from '../../utils/chartUtils'
import type { ChartDisplay } from '../../config/chartDisplay'
import MultiLineChart, { type ChartSeriesPoint } from './MultiLineChart'
import { METRIC_COLORS } from './chartTheme'

interface MetricChartProps {
  data: ChartSeriesPoint[]
  title?: string
  color?: string
  height?: number
  unit?: string
  min?: number
  max?: number
  period?: string
  gaps?: ChartGap[]
  display?: ChartDisplay
}

export default function MetricChart({
  data,
  title,
  color = METRIC_COLORS.cpu,
  height = 200,
  unit = '',
  min,
  max,
  period = '1h',
  gaps,
  display,
}: MetricChartProps) {
  const { t } = useTranslation()

  const series = useMemo(
    () => [{ name: title ?? t('common.value'), data, color }],
    [data, title, color, t],
  )

  return (
    <MultiLineChart
      series={series}
      display={display}
      height={height}
      unit={unit}
      period={period}
      gaps={gaps}
      yMin={min}
      yMax={max}
      showLegend={false}
    />
  )
}
