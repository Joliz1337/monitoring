import { useMemo } from 'react'
import ReactApexChart from 'react-apexcharts'
import { ApexOptions } from 'apexcharts'
import { useTranslation } from 'react-i18next'
import { downsampleLTTB, smoothDataDEMA, MAX_CHART_POINTS } from '../../utils/chartUtils'

interface MetricChartProps {
  data: Array<{ timestamp: string; value: number }>
  title?: string
  color?: string
  height?: number
  type?: 'line' | 'area'
  unit?: string
  min?: number
  max?: number
  period?: string
  smoothing?: number // Smoothing factor 0-1 (0 = no smoothing, 1 = max smoothing)
}

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

export default function MetricChart({
  data,
  title,
  color = '#22d3ee',
  height = 200,
  type = 'area',
  unit = '',
  min,
  max,
  period = '1h',
  smoothing = 0.35, // Default smoothing factor for pleasant curves
}: MetricChartProps) {
  const { t, i18n } = useTranslation()
  
  const { series, options } = useMemo(() => {
    const lang = i18n.language || 'en'
    
    // Convert to chart points
    const rawData = data.map(d => ({
      x: parseTimestamp(d.timestamp),
      y: d.value,
    }))
    
    // Apply smoothing to reduce sharp spikes (DEMA for lag-free smoothing)
    const smoothedData = smoothing > 0 ? smoothDataDEMA(rawData, smoothing) : rawData
    
    // Apply LTTB downsampling to reduce points while preserving visual quality
    const seriesData = downsampleLTTB(smoothedData, MAX_CHART_POINTS)
    
    const dateFormat = getDateTimeFormat(period)
    
    const options: ApexOptions = {
      chart: {
        type,
        toolbar: { show: false },
        zoom: { enabled: false },
        animations: {
          enabled: false, // Disabled for performance
        },
        background: 'transparent',
        redrawOnParentResize: true,
        redrawOnWindowResize: true,
      },
      theme: {
        mode: 'dark',
      },
      colors: [color],
      stroke: {
        curve: 'monotoneCubic', // Better than 'smooth' - no artifacts on sharp spikes
        width: 2,
      },
      fill: {
        type: 'gradient',
        gradient: {
          shadeIntensity: 1,
          opacityFrom: 0.4,
          opacityTo: 0,
          stops: [0, 100],
        },
      },
      dataLabels: { enabled: false },
      grid: {
        borderColor: '#343541',
        strokeDashArray: 4,
        xaxis: { lines: { show: false } },
        yaxis: { lines: { show: true } },
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
        min: min,
        max: max,
        labels: {
          style: { colors: '#8e8ea0', fontSize: '11px' },
          formatter: (val) => `${val.toFixed(1)}${unit}`,
        },
      },
      tooltip: {
        theme: 'dark',
        x: {
          formatter: (value) => formatDateLocalized(new Date(value), dateFormat.tooltip, lang),
        },
        y: {
          formatter: (val) => `${val.toFixed(2)}${unit}`,
        },
      },
    }
    
    return {
      series: [{ name: title || t('common.value'), data: seriesData }],
      options,
    }
  }, [data, color, type, unit, min, max, title, period, smoothing, i18n.language, t])
  
  if (data.length === 0) {
    return (
      <div className="flex items-center justify-center h-48 text-dark-500">
        {t('common.no_data')}
      </div>
    )
  }
  
  return (
    <ReactApexChart
      options={options}
      series={series}
      type={type}
      height={height}
    />
  )
}
