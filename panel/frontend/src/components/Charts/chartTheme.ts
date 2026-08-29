import type { ApexOptions } from 'apexcharts'

export const CHART_LABEL_COLOR = '#8e8ea0'
export const CHART_GRID_COLOR = '#2a2a32'
export const LINE_WIDTH = 2
export const PEAK_BAND_OPACITY = 0.12
export const AXIS_FONT_SIZE = '11px'

export const AREA_GRADIENT = {
  shadeIntensity: 1,
  opacityFrom: 0.3,
  opacityTo: 0,
  stops: [0, 100],
}

export const SERIES_PALETTE = [
  '#22d3ee', // cyan
  '#10b981', // green
  '#f59e0b', // amber
  '#ef4444', // red
  '#8b5cf6', // violet
  '#ec4899', // pink
  '#06b6d4', // cyan-500
  '#14b8a6', // teal
  '#f97316', // orange
  '#a855f7', // purple
  '#3b82f6', // blue
  '#84cc16', // lime
  '#eab308', // yellow
  '#e11d48', // rose
  '#6366f1', // indigo
  '#0ea5e9', // sky
]

export function seriesColor(index: number): string {
  return SERIES_PALETTE[index % SERIES_PALETTE.length]
}

export const METRIC_COLORS = {
  cpu: '#22d3ee',
  memory: '#10b981',
  load: '#f59e0b',
}

export const NETWORK_COLORS = {
  download: '#10b981',
  upload: '#22d3ee',
}

/** Зелёный → жёлтый → красный, как у живых баров по ядрам (пороги 50/80 там же) */
export const HEATMAP_SCALE = ['#1f5f4a', '#10b981', '#a3b83a', '#f59e0b', '#f0713a', '#ef4444']
/** Ячейка без данных — еле заметный след поверхности, чтобы простой читался как пропуск */
export const HEATMAP_EMPTY_COLOR = 'rgba(64, 65, 79, 0.25)'

const HEATMAP_STOPS = HEATMAP_SCALE.map(hex => [
  parseInt(hex.slice(1, 3), 16),
  parseInt(hex.slice(3, 5), 16),
  parseInt(hex.slice(5, 7), 16),
])

export function heatmapColor(percent: number): string {
  const position = Math.min(1, Math.max(0, percent / 100)) * (HEATMAP_STOPS.length - 1)
  const lower = Math.floor(position)
  const upper = Math.min(lower + 1, HEATMAP_STOPS.length - 1)
  const weight = position - lower
  const channel = (i: number) => Math.round(HEATMAP_STOPS[lower][i] + (HEATMAP_STOPS[upper][i] - HEATMAP_STOPS[lower][i]) * weight)
  return `rgb(${channel(0)}, ${channel(1)}, ${channel(2)})`
}

export const MONTHS: Record<string, string[]> = {
  ru: ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'],
  en: ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'],
}

/** Локальное время браузера — часовой пояс панели применяется на уровне Date */
export function formatDateLocalized(date: Date, format: string, lang: string): string {
  const months = MONTHS[lang] || MONTHS.en
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

export interface DateTimeFormat {
  xaxis: string
  tooltip: string
}

export function getDateTimeFormat(period: string): DateTimeFormat {
  switch (period) {
    case '24h':
      return { xaxis: 'HH:mm', tooltip: 'dd MMM HH:mm' }
    case '7d':
      return { xaxis: 'dd MMM', tooltip: 'dd MMM HH:mm' }
    case '30d':
      return { xaxis: 'dd MMM', tooltip: 'dd MMM HH:mm' }
    case '365d':
      return { xaxis: 'MMM yy', tooltip: 'dd MMM yyyy' }
    default:
      return { xaxis: 'HH:mm', tooltip: 'HH:mm:ss' }
  }
}

export function buildBaseChartOptions({ lang, period }: { lang: string; period: string }): ApexOptions {
  const dateFormat = getDateTimeFormat(period)
  return {
    chart: {
      toolbar: { show: false },
      zoom: { enabled: false },
      background: 'transparent',
      animations: { enabled: false },
      redrawOnParentResize: true,
      redrawOnWindowResize: true,
    },
    theme: { mode: 'dark' },
    dataLabels: { enabled: false },
    grid: {
      borderColor: CHART_GRID_COLOR,
      strokeDashArray: 0,
      xaxis: { lines: { show: false } },
      yaxis: { lines: { show: true } },
    },
    xaxis: {
      type: 'datetime',
      labels: {
        style: { colors: CHART_LABEL_COLOR, fontSize: AXIS_FONT_SIZE },
        formatter: (value) => formatDateLocalized(new Date(value), dateFormat.xaxis, lang),
      },
      axisBorder: { show: false },
      axisTicks: { show: false },
    },
  }
}

export interface TooltipRow {
  color: string
  name: string
  value: string | null
  peak: string | null
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/** Тултип собирается руками: стандартный у rangeArea показывает «undefined - undefined» для линий */
export function renderMetricTooltip({
  title,
  rows,
  noDataLabel,
  peakLabel,
}: {
  title: string
  rows: TooltipRow[]
  noDataLabel: string
  peakLabel: string
}): string {
  const lines = rows.map(row => {
    const value = row.value === null
      ? `<span style="color:${CHART_LABEL_COLOR}">${escapeHtml(noDataLabel)}</span>`
      : `<b>${escapeHtml(row.value)}</b>`
    const peak = row.peak === null
      ? ''
      : ` <span style="color:${CHART_LABEL_COLOR}">· ${escapeHtml(peakLabel)} ${escapeHtml(row.peak)}</span>`
    return (
      `<div style="display:flex;align-items:center;gap:6px;white-space:nowrap">` +
      `<span style="width:8px;height:8px;border-radius:50%;background:${row.color};flex-shrink:0"></span>` +
      `<span>${escapeHtml(row.name)} ${value}${peak}</span>` +
      `</div>`
    )
  })
  return (
    `<div style="padding:8px 10px;font-size:12px;line-height:1.5">` +
    `<div style="color:${CHART_LABEL_COLOR};margin-bottom:4px">${escapeHtml(title)}</div>` +
    lines.join('') +
    `</div>`
  )
}
