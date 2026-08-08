import { useMemo } from 'react'
import ReactApexChart from 'react-apexcharts'
import { ApexOptions } from 'apexcharts'
import { useTranslation } from 'react-i18next'
import {
  processSeriesWithGaps,
  buildGapAnnotations,
  parseTimestamp,
  ChartGap,
} from '../../utils/chartUtils'

interface MetricChartProps {
  data: Array<{ timestamp: string; value: number | null }>
  title?: string
  color?: string
  height?: number
  type?: 'line' | 'area'
  unit?: string
  min?: number
  max?: number
  period?: string
  smoothing?: number // Smoothing factor 0-1 (0 = no smoothing, 1 = max smoothing)
  gaps?: ChartGap[] // Periods without data, highlighted on the x-axis
}

// Stable identity keeps the useMemo below from recomputing on every render
const NO_GAPS: ChartGap[] = []

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
  gaps = NO_GAPS,
}: MetricChartProps) {
  const { t, i18n } = useTranslation()

  const { series, options } = useMemo(() => {
    const lang = i18n.language || 'en'
    const noDataLabel = t('common.no_data')

    const rawData = data.map(d => ({
      x: parseTimestamp(d.timestamp),
      y: d.value,
    }))

    // Smoothing and downsampling run per continuous segment, gaps stay null
    const seriesData = processSeriesWithGaps(rawData, { smoothing })

    const lastTimestamp = rawData.length > 0 ? rawData[rawData.length - 1].x : 0
    const gapRanges = lastTimestamp > 0 ? buildGapAnnotations(gaps, lastTimestamp) : []

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
          formatter: (val: number | null | undefined) =>
            val == null || Number.isNaN(val) ? '' : `${val.toFixed(1)}${unit}`,
        },
      },
      annotations: gapRanges.length > 0 ? { xaxis: gapRanges } : undefined,
      tooltip: {
        theme: 'dark',
        x: {
          formatter: (value) => formatDateLocalized(new Date(value), dateFormat.tooltip, lang),
        },
        y: {
          // Apex passes null for gap points - showing 0 there would fake a metric
          formatter: (val: number | null | undefined) =>
            val == null || Number.isNaN(val) ? noDataLabel : `${val.toFixed(2)}${unit}`,
        },
      },
    }

    return {
      series: [{ name: title || t('common.value'), data: seriesData }],
      options,
    }
  }, [data, color, type, unit, min, max, title, period, smoothing, gaps, i18n.language, t])

  if (data.length === 0 || data.every(d => d.value === null)) {
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
