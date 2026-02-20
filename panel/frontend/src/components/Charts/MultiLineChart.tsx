import { useMemo } from 'react'
import ReactApexChart from 'react-apexcharts'
import { ApexOptions } from 'apexcharts'
import { useTranslation } from 'react-i18next'
import { downsampleLTTB, smoothDataDEMA, calculateMultiSeriesYMax, MAX_CHART_POINTS } from '../../utils/chartUtils'

interface Series {
  name: string
  data: Array<{ timestamp: string; value: number }>
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
}

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

/**
 * Parse timestamp string to Unix milliseconds
 * Handles various formats: ISO with Z, ISO with offset, naive datetime, date-only, hour-only
 */
function parseTimestamp(timestamp: string): number {
  // ISO format with explicit timezone (Z or +/-offset)
  if (timestamp.includes('Z') || timestamp.includes('+') || /T.*-\d{2}:\d{2}$/.test(timestamp)) {
    return new Date(timestamp).getTime()
  }
  
  // ISO-like format without timezone (treat as UTC)
  if (timestamp.includes('T')) {
    return new Date(timestamp + 'Z').getTime()
  }
  
  // Traffic API formats: "YYYY-MM-DD HH:00" or "YYYY-MM-DD" or "YYYY-MM"
  // Replace space with T and add Z to treat as UTC
  const normalized = timestamp.replace(' ', 'T')
  
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(normalized)) {
    // "2024-01-12 10:00" -> "2024-01-12T10:00Z"
    return new Date(normalized + ':00Z').getTime()
  }
  
  if (/^\d{4}-\d{2}-\d{2}$/.test(normalized)) {
    // "2024-01-12" -> start of day UTC
    return new Date(normalized + 'T00:00:00Z').getTime()
  }
  
  if (/^\d{4}-\d{2}$/.test(normalized)) {
    // "2024-01" -> start of month UTC
    return new Date(normalized + '-01T00:00:00Z').getTime()
  }
  
  // Fallback: let browser parse it
  return new Date(timestamp).getTime()
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
}: MultiLineChartProps) {
  const { t, i18n } = useTranslation()
  
  const { chartSeries, options } = useMemo(() => {
    const lang = i18n.language || 'en'
    
    // Convert, smooth and downsample each series
    const processedSeries = series.map((s) => {
      const rawData = s.data.map(d => ({
        x: parseTimestamp(d.timestamp),
        y: d.value,
      }))
      // Apply smoothing to reduce sharp spikes (DEMA for lag-free smoothing)
      const smoothedData = smoothing > 0 ? smoothDataDEMA(rawData, smoothing) : rawData
      // Apply LTTB downsampling
      return downsampleLTTB(smoothedData, MAX_CHART_POINTS)
    })
    
    const chartSeries = series.map((s, i) => ({
      name: s.name,
      data: processedSeries[i],
    }))
    
    // Calculate dynamic Y-max for all series
    const dynamicYMax = calculateMultiSeriesYMax(processedSeries)
    
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
          formatter: (val) => formatValue ? formatValue(val) : `${val.toFixed(1)}${unit}`,
        },
      },
      legend: {
        position: 'top',
        horizontalAlign: 'left',
        labels: { colors: '#8e8ea0' },
      },
      tooltip: {
        theme: 'dark',
        shared: true,
        intersect: false,
        x: {
          formatter: (value) => formatDateLocalized(new Date(value), dateFormat.tooltip, lang),
        },
        y: {
          formatter: (val) => formatValue ? formatValue(val || 0) : `${val?.toFixed(2) || 0}${unit}`,
        },
      },
    }
    
    return { chartSeries, options }
  }, [series, unit, stacked, formatValue, period, smoothing, i18n.language])
  
  if (series.every(s => s.data.length === 0)) {
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
