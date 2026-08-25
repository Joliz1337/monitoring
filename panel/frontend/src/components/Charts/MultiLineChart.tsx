import { useMemo } from 'react'
import ReactApexChart from 'react-apexcharts'
import type { ApexOptions } from 'apexcharts'
import { useTranslation } from 'react-i18next'
import {
  processSeriesWithGaps,
  bandTop,
  buildGapAnnotations,
  parseTimestamp,
  seriesMaxValue,
  niceAxisMax,
  type ChartGap,
  type ProcessedPoint,
} from '../../utils/chartUtils'
import { DEFAULT_CHART_DISPLAY, type ChartDisplay } from '../../config/chartDisplay'
import {
  AREA_GRADIENT,
  AXIS_FONT_SIZE,
  CHART_LABEL_COLOR,
  LINE_WIDTH,
  PEAK_BAND_OPACITY,
  buildBaseChartOptions,
  formatDateLocalized,
  getDateTimeFormat,
  renderMetricTooltip,
  seriesColor,
} from './chartTheme'

export interface ChartSeriesPoint {
  timestamp: string
  value: number | null
  /** Максимум за интервал точки; без него полоса пиков для точки не рисуется */
  peak?: number | null
}

export interface ChartSeries {
  name: string
  data: ChartSeriesPoint[]
  color?: string
}

interface MultiLineChartProps {
  series: ChartSeries[]
  display?: ChartDisplay
  height?: number
  unit?: string
  formatValue?: (value: number) => string
  period?: string
  gaps?: ChartGap[]
  yMin?: number
  yMax?: number
  showLegend?: boolean
}

type ChartType = 'area' | 'rangeArea'

// Stable identity keeps the useMemo below from recomputing on every render
const NO_GAPS: ChartGap[] = []

function latestTimestamp(seriesData: ProcessedPoint[][]): number {
  return seriesData.reduce((latest, points) => {
    const last = points[points.length - 1]
    return last && last.x > latest ? last.x : latest
  }, 0)
}

export default function MultiLineChart({
  series,
  display = DEFAULT_CHART_DISPLAY,
  height = 250,
  unit = '',
  formatValue,
  period = '1h',
  gaps = NO_GAPS,
  yMin,
  yMax,
  showLegend = series.length > 1,
}: MultiLineChartProps) {
  const { t, i18n } = useTranslation()

  const { chartSeries, chartType, options } = useMemo(() => {
    const lang = i18n.language || 'en'
    const noDataLabel = t('common.no_data')
    const peakLabel = t('common.peak')
    const dateFormat = getDateTimeFormat(period)
    const formatTooltipValue = (value: number) => (formatValue ? formatValue(value) : `${value.toFixed(2)}${unit}`)
    const formatAxisValue = (value: number) => (formatValue ? formatValue(value) : `${value.toFixed(1)}${unit}`)

    const processed = series.map(s =>
      processSeriesWithGaps(
        s.data.map(d => ({ x: parseTimestamp(d.timestamp), y: d.value, peak: d.peak })),
        { smoothing: display.smoothing },
      ),
    )
    const colors = series.map((s, i) => s.color ?? seriesColor(i))

    // Полоса есть только у рядов с пиками: старая нода на 1h их не отдаёт
    const bandIndexes = display.showPeaks
      ? processed.flatMap((points, i) => (points.some(p => p.peak !== null && p.y !== null) ? [i] : []))
      : []
    const hasBand = bandIndexes.length > 0
    const chartType: ChartType = hasBand ? 'rangeArea' : 'area'

    // Полосы идут первыми — рисуются под линиями
    const bandSeries = bandIndexes.map(i => ({
      name: `${series[i].name} · ${peakLabel}`,
      type: 'rangeArea',
      data: processed[i].map(p => {
        const top = bandTop(p)
        return { x: p.x, y: top === null ? null : [p.y as number, top] }
      }),
    }))
    const lineSeries = series.map((s, i) => ({
      name: s.name,
      type: hasBand ? 'line' : 'area',
      data: processed[i].map(p => ({ x: p.x, y: p.y })),
    }))
    const chartSeries = [...bandSeries, ...lineSeries]

    const lastTimestamp = latestTimestamp(processed)
    const gapRanges = lastTimestamp > 0 ? buildGapAnnotations(gaps, lastTimestamp) : []

    const base = buildBaseChartOptions({ lang, period })
    const options: ApexOptions = {
      ...base,
      chart: { ...base.chart, type: chartType },
      colors: [...bandIndexes.map(i => colors[i]), ...colors],
      stroke: {
        curve: display.curve,
        width: hasBand ? [...bandSeries.map(() => 0), ...lineSeries.map(() => LINE_WIDTH)] : LINE_WIDTH,
      },
      fill: hasBand
        ? { type: 'solid', opacity: [...bandSeries.map(() => PEAK_BAND_OPACITY), ...lineSeries.map(() => 1)] }
        : { type: 'gradient', gradient: AREA_GRADIENT },
      yaxis: {
        min: yMin ?? 0,
        max: yMax ?? niceAxisMax(seriesMaxValue(processed)),
        labels: {
          style: { colors: CHART_LABEL_COLOR, fontSize: AXIS_FONT_SIZE },
          formatter: (value: number | null | undefined) =>
            value == null || Number.isNaN(value) ? '' : formatAxisValue(value),
        },
      },
      legend: {
        show: showLegend,
        position: 'top',
        horizontalAlign: 'left',
        labels: { colors: CHART_LABEL_COLOR },
        // Полосы в легенде не показываем, а без штатных пунктов клик по легенде не может
        // скрыть ряд вместе с его полосой — переключение отключено целиком
        customLegendItems: hasBand ? series.map(s => s.name) : [],
        ...(hasBand ? { markers: { fillColors: colors } } : {}),
        onItemClick: { toggleDataSeries: !hasBand },
      },
      // Пустой массив, а не undefined: updateOptions делает поверхностное присваивание
      // для не-объектных значений и стёр бы весь блок annotations вместе с images/texts
      annotations: { xaxis: gapRanges },
      tooltip: {
        theme: 'dark',
        shared: true,
        intersect: false,
        custom: ({ dataPointIndex }: { dataPointIndex: number }) => {
          const anchor = processed.find(points => points[dataPointIndex] !== undefined)?.[dataPointIndex]
          if (!anchor) return ''
          const rows = series.map((s, i) => {
            const point = processed[i][dataPointIndex]
            const hasValue = point !== undefined && point.raw !== null
            return {
              color: colors[i],
              name: s.name,
              value: hasValue ? formatTooltipValue(point.raw as number) : null,
              peak: hasValue && hasBand && point.peak !== null ? formatTooltipValue(point.peak) : null,
            }
          })
          return renderMetricTooltip({
            title: formatDateLocalized(new Date(anchor.x), dateFormat.tooltip, lang),
            rows,
            noDataLabel,
            peakLabel,
          })
        },
      },
    }

    return { chartSeries, chartType, options }
  }, [series, display, unit, formatValue, period, gaps, yMin, yMax, showLegend, i18n.language, t])

  if (series.every(s => s.data.every(d => d.value === null))) {
    return (
      <div className="flex items-center justify-center h-48 text-dark-500">
        {t('common.no_data')}
      </div>
    )
  }

  // Смена типа через updateOptions ненадёжна — при переключении полосы график пересоздаётся
  return (
    <ReactApexChart
      key={chartType}
      options={options}
      series={chartSeries}
      type={chartType}
      height={height}
    />
  )
}
