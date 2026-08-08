import { useMemo } from 'react'
import ReactApexChart from 'react-apexcharts'
import { ApexOptions } from 'apexcharts'
import { useTranslation } from 'react-i18next'
import {
  processSeriesWithGaps,
  calculateMultiSeriesYMax,
  buildGapAnnotations,
  parseTimestamp,
  ChartGap,
} from '../../utils/chartUtils'

interface Series {
  name: string
  data: Array<{ timestamp: string; value: number | null }>
  color?: string
}

interface MultiLineChartProps {
  series: Series[]
  height?: number
  unit?: string
  stacked?: boolean
  formatValue?: (val: number) => string
  period?: string
  smoothing?: number // Smoothing factor 0-1 (0 = no smoothing, 1 = max smoothing)
  gaps?: ChartGap[] // Periods without data, highlighted on the x-axis
}

// Stable identity keeps the useMemo below from recomputing on every render
const NO_GAPS: ChartGap[] = []

const DEFAULT_COLORS = [
  '#22d3ee', // cyan
  '#10b981', // green
  '#f59e0b', // yellow
  '#ef4444', // red
  '#8b5cf6', // purple
  '#ec4899', // pink
]

// Локализованные названия месяцев
const MONTHS: Record<string, string[]> = {
  ru: ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'],
  en: ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'],
}

function formatDateLocalized(date: Date, format: string, lang: string): string {
  const months = MONTHS[lang] || MONTHS.en
  // Use local time methods to show time in user's timezone
  const day = date.getDate().toString().padStart(2, '0')
  const month = months[date.getMonth()]
  const year = date.getFullYear()
  const shortYear = year.toString().slice(-2)
  const hours = date.getHours().toString().padStart(2, '0')
  const minutes = date.getMinutes().toString().padStart(2, '0')
  const seconds = date.getSeconds().toString().padStart(2, '0')
  
  return format
    .replace('dd', day)
    .replace('MMM', month)
    .replace('yyyy', year.toString())
    .replace('yy', shortYear)
    .replace('HH', hours)
    .replace('mm', minutes)
    .replace('ss', seconds)
}

function getDateTimeFormat(period: string) {
  switch (period) {
    case '1h':
      return { xaxis: 'HH:mm', tooltip: 'HH:mm:ss' }
    case '24h':
      return { xaxis: 'HH:mm', tooltip: 'dd MMM HH:mm' }
    case '7d':
      return { xaxis: 'dd MMM', tooltip: 'dd MMM HH:mm' }
    case '30d':
      return { xaxis: 'dd MMM', tooltip: 'dd MMM yyyy' }
    case '365d':
      return { xaxis: 'MMM yy', tooltip: 'dd MMM yyyy' }
    default:
      return { xaxis: 'HH:mm', tooltip: 'HH:mm:ss' }
  }
}

export default function MultiLineChart({
  series,
  height = 250,
  unit = '',
  stacked = false,
  formatValue,
  period = '1h',
  smoothing = 0.35, // Default smoothing factor for pleasant curves
  gaps = NO_GAPS,
}: MultiLineChartProps) {
  const { t, i18n } = useTranslation()

  const { chartSeries, options } = useMemo(() => {
    const lang = i18n.language || 'en'
    const noDataLabel = t('common.no_data')

    // Smoothing and downsampling run per continuous segment, gaps stay null
    const processedSeries = series.map(s =>
      processSeriesWithGaps(
        s.data.map(d => ({ x: parseTimestamp(d.timestamp), y: d.value })),
        { smoothing },
      ),
    )

    const chartSeries = series.map((s, i) => ({
      name: s.name,
      data: processedSeries[i],
    }))

    const dynamicYMax = calculateMultiSeriesYMax(processedSeries)

    const lastTimestamp = processedSeries.reduce((latest, points) => {
      const lastPoint = points[points.length - 1]
      return lastPoint && lastPoint.x > latest ? lastPoint.x : latest
    }, 0)
    const gapRanges = lastTimestamp > 0 ? buildGapAnnotations(gaps, lastTimestamp) : []

    const colors = series.map((s, i) => s.color || DEFAULT_COLORS[i % DEFAULT_COLORS.length])
    const dateFormat = getDateTimeFormat(period)

    const options: ApexOptions = {
      chart: {
        type: 'area',
        stacked,
        toolbar: { show: false },
        zoom: { enabled: false },
        background: 'transparent',
        animations: {
          enabled: false, // Disabled for performance
        },
        redrawOnParentResize: true,
        redrawOnWindowResize: true,
      },
      theme: { mode: 'dark' },
      colors,
      stroke: {
        curve: 'monotoneCubic', // Better than 'smooth' - no artifacts on sharp spikes
        width: 2,
      },
      fill: {
        type: 'gradient',
        gradient: {
          shadeIntensity: 1,
          opacityFrom: stacked ? 0.6 : 0.3,
          opacityTo: stacked ? 0.2 : 0,
          stops: [0, 100],
        },
      },
      dataLabels: { enabled: false },
      grid: {
        borderColor: '#343541',
        strokeDashArray: 4,
        xaxis: { lines: { show: false } },
      },
      xaxis: {
        type: 'datetime',
        labels: {
          style: { colors: '#8e8ea0', fontSize: '11px' },
          formatter: (value) => formatDateLocalized(new Date(value), dateFormat.xaxis, lang),
        },
        axisBorder: { show: false },
        axisTicks: { show: false },
      },
      yaxis: {
        min: 0,
        max: dynamicYMax,
        labels: {
          style: { colors: '#8e8ea0', fontSize: '11px' },
          formatter: (val: number | null | undefined) => {
            if (val == null || Number.isNaN(val)) return ''
            return formatValue ? formatValue(val) : `${val.toFixed(1)}${unit}`
          },
        },
      },
      legend: {
        position: 'top',
        horizontalAlign: 'left',
        labels: { colors: '#8e8ea0' },
      },
      // Пустой массив, а не undefined: updateOptions делает поверхностное присваивание
      // для не-объектных значений и стёр бы весь блок annotations вместе с images/texts
      annotations: { xaxis: gapRanges },
      tooltip: {
        theme: 'dark',
        shared: true,
        intersect: false,
        x: {
          formatter: (value) => formatDateLocalized(new Date(value), dateFormat.tooltip, lang),
        },
        y: {
          // Apex passes null for gap points - showing 0 there would fake a metric
          formatter: (val: number | null | undefined) => {
            if (val == null || Number.isNaN(val)) return noDataLabel
            return formatValue ? formatValue(val) : `${val.toFixed(2)}${unit}`
          },
        },
      },
    }

    return { chartSeries, options }
  }, [series, unit, stacked, formatValue, period, smoothing, gaps, i18n.language, t])

  if (series.every(s => s.data.every(d => d.value === null))) {
    return (
      <div className="flex items-center justify-center h-48 text-dark-500">
        {t('common.no_data')}
      </div>
    )
  }
  
  return (
    <ReactApexChart
      options={options}
      series={chartSeries}
      type="area"
      height={height}
    />
  )
}
