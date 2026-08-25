export type ChartMode = 'smooth' | 'raw'
export type ChartMetric = 'cpu' | 'memory' | 'network' | 'load' | 'tcp'
export type ChartCurve = 'monotoneCubic' | 'straight'

export const CHART_METRICS: readonly ChartMetric[] = ['cpu', 'memory', 'network', 'load', 'tcp']

export interface ChartDisplay {
  mode: ChartMode
  showPeaks: boolean
  smoothing: number
  curve: ChartCurve
}

export type ChartModeOverrides = Partial<Record<ChartMetric, ChartMode>>

export const CHART_MODE_PRESETS: Record<ChartMode, { smoothing: number; curve: ChartCurve }> = {
  smooth: { smoothing: 0.35, curve: 'monotoneCubic' },
  raw: { smoothing: 0, curve: 'straight' },
}

export const DEFAULT_CHART_MODE: ChartMode = 'smooth'

const KNOWN_MODES = new Set<string>(['smooth', 'raw'])
const KNOWN_METRICS = new Set<string>(CHART_METRICS)

export function parseChartMode(raw: string | undefined | null): ChartMode {
  return raw && KNOWN_MODES.has(raw) ? (raw as ChartMode) : DEFAULT_CHART_MODE
}

/** Формат хранения — "cpu:raw,network:smooth"; незнакомые метрики и режимы отбрасываются */
export function parseChartModeOverrides(raw: string | undefined | null): ChartModeOverrides {
  if (!raw) return {}
  const overrides: ChartModeOverrides = {}
  for (const entry of raw.split(',')) {
    const [metric, mode] = entry.split(':').map(part => part.trim())
    if (!KNOWN_METRICS.has(metric) || !KNOWN_MODES.has(mode)) continue
    overrides[metric as ChartMetric] = mode as ChartMode
  }
  return overrides
}

export function serializeChartModeOverrides(overrides: ChartModeOverrides): string {
  return CHART_METRICS
    .filter(metric => overrides[metric] !== undefined)
    .map(metric => `${metric}:${overrides[metric]}`)
    .join(',')
}

export function resolveChartDisplay(mode: ChartMode, showPeaks: boolean): ChartDisplay {
  return { mode, showPeaks, ...CHART_MODE_PRESETS[mode] }
}

export const DEFAULT_CHART_DISPLAY: ChartDisplay = resolveChartDisplay(DEFAULT_CHART_MODE, false)

/** Живые показатели на дашборде и в шапке сервера: последняя секунда или среднее за интервал опроса */
export type LiveValuesMode = 'instant' | 'average'
export const DEFAULT_LIVE_VALUES_MODE: LiveValuesMode = 'instant'

export function parseLiveValuesMode(raw: string | undefined | null): LiveValuesMode {
  return raw === 'average' ? 'average' : DEFAULT_LIVE_VALUES_MODE
}

/** Для рядов, где сглаживать нечестно — например, сумма байт за час */
export const RAW_DISPLAY: ChartDisplay = resolveChartDisplay('raw', false)
