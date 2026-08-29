/**
 * Подготовка рядов истории к отрисовке: разрывы, сглаживание, ось Y
 */

/** Точка ряда: y === null — данных в этот момент нет, peak — максимум за интервал точки */
export interface SeriesPoint {
  x: number
  y: number | null
  peak?: number | null
}

export interface ProcessedPoint {
  x: number
  /** Значение для линии — сглаженное в режиме «Сглаженные» */
  y: number | null
  /** Значение как записано — для подсказки */
  raw: number | null
  peak: number | null
}

/** Период недоступности сервера; to === null — простой ещё не закончился */
export interface ChartGap {
  from: string
  to: string | null
}

export interface SeriesProcessingOptions {
  /** 0 — без сглаживания, ближе к 1 — сильнее */
  smoothing: number
}

// Запас над максимумом ряда, чтобы линия не упиралась в верх графика
const Y_AXIS_PADDING = 1.1

// «Красивые» шаги сетки: 1, 2, 2.5, 5 × 10ⁿ
const NICE_STEPS = [1, 2, 2.5, 5, 10]

// Примерное число делений оси, от него считается шаг сетки
const Y_AXIS_TICKS = 5

const GAP_FILL_COLOR = '#ef4444'
const GAP_FILL_OPACITY = 0.12

/**
 * Parse timestamp string to Unix milliseconds
 * Handles various formats: ISO with Z, ISO with offset, naive datetime, date-only, hour-only
 */
export function parseTimestamp(timestamp: string): number {
  // ISO format with explicit timezone (Z or +/-offset)
  if (timestamp.includes('Z') || timestamp.includes('+') || /T.*-\d{2}:\d{2}$/.test(timestamp)) {
    return new Date(timestamp).getTime()
  }

  // ISO-like format without timezone (treat as UTC)
  if (timestamp.includes('T')) {
    return new Date(timestamp + 'Z').getTime()
  }

  // Traffic API formats: "YYYY-MM-DD HH:00" or "YYYY-MM-DD" or "YYYY-MM"
  const normalized = timestamp.replace(' ', 'T')

  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(normalized)) {
    return new Date(normalized + ':00Z').getTime()
  }

  if (/^\d{4}-\d{2}-\d{2}$/.test(normalized)) {
    return new Date(normalized + 'T00:00:00Z').getTime()
  }

  if (/^\d{4}-\d{2}$/.test(normalized)) {
    return new Date(normalized + '-01T00:00:00Z').getTime()
  }

  return new Date(timestamp).getTime()
}

function isFinitePeak(peak: number | null | undefined): peak is number {
  return peak != null && Number.isFinite(peak)
}

function smoothEMA(values: number[], alpha: number): number[] {
  const smoothed: number[] = [values[0]]
  for (let i = 1; i < values.length; i++) {
    smoothed.push(alpha * smoothed[i - 1] + (1 - alpha) * values[i])
  }
  return smoothed
}

/** Прямой и обратный проход EMA, усреднённые — сглаживание без запаздывания */
function smoothDEMA(values: number[], alpha: number): number[] {
  if (values.length < 3) return values
  const forward = smoothEMA(values, alpha)
  const backward = smoothEMA([...values].reverse(), alpha).reverse()
  return values.map((_, i) => (forward[i] + backward[i]) / 2)
}

/**
 * Сглаживает непрерывные отрезки ряда, не трогая точки-разрывы.
 *
 * Рекурсия EMA размазала бы один null на весь ряд, поэтому сглаживание идёт по каждому
 * отрезку между разрывами отдельно. Позиции точек сохраняются один к одному — у всех рядов
 * одного графика остаётся общая сетка X, и индекс точки в тултипе одинаков для всех серий.
 * Сглаживается только среднее; пик — уже максимум, а записанное значение остаётся в `raw`
 * для подсказки: показывать там сглаженную цифру значило бы врать о зафиксированном всплеске.
 */
export function processSeriesWithGaps(
  points: SeriesPoint[],
  { smoothing }: SeriesProcessingOptions,
): ProcessedPoint[] {
  const result: ProcessedPoint[] = points.map(point => {
    const value = point.y !== null && Number.isFinite(point.y) ? point.y : null
    return { x: point.x, y: value, raw: value, peak: isFinitePeak(point.peak) ? point.peak : null }
  })

  if (smoothing > 0) {
    let segmentStart = 0
    for (let i = 0; i <= result.length; i++) {
      const atSegmentEnd = i === result.length || result[i].y === null
      if (!atSegmentEnd) continue
      if (i > segmentStart) {
        const smoothed = smoothDEMA(result.slice(segmentStart, i).map(p => p.y as number), smoothing)
        smoothed.forEach((value, offset) => { result[segmentStart + offset].y = value })
      }
      segmentStart = i + 1
    }
  }

  return result
}

/** Верх полосы пиков: сглаженная линия может подняться выше записанного максимума */
export function bandTop(point: ProcessedPoint): number | null {
  if (point.y === null || point.peak === null) return null
  return Math.max(point.peak, point.y)
}

/**
 * Build ApexCharts x-axis range annotations highlighting periods without data
 *
 * @param gaps - Unavailability periods; an open interval is closed at ongoingEnd
 * @param ongoingEnd - Timestamp (ms) used as the right edge for an unfinished gap
 */
export function buildGapAnnotations(gaps: ChartGap[], ongoingEnd: number) {
  return gaps
    .map(gap => ({
      x: parseTimestamp(gap.from),
      x2: gap.to ? parseTimestamp(gap.to) : ongoingEnd,
    }))
    .filter(range => Number.isFinite(range.x) && Number.isFinite(range.x2) && range.x2 > range.x)
    .map(range => ({
      ...range,
      fillColor: GAP_FILL_COLOR,
      opacity: GAP_FILL_OPACITY,
      borderColor: 'transparent',
    }))
}

/** Максимум по всем рядам с учётом пиков; null-точки не участвуют */
export function seriesMaxValue(seriesData: ProcessedPoint[][]): number {
  let max = 0
  for (const series of seriesData) {
    for (const point of series) {
      if (point.y !== null && point.y > max) max = point.y
      if (point.peak !== null && point.peak > max) max = point.peak
    }
  }
  return max
}

/**
 * Верх оси Y: максимум с запасом, округлённый вверх до кратного «красивому» шагу сетки.
 * К шагу округляется именно шаг, а не весь максимум: иначе на границе декады ось
 * прыгала бы вдвое (91 → 200, 455 → 1000), и линия ложилась бы в нижнюю половину графика.
 */
export function niceAxisMax(max: number): number | undefined {
  if (!Number.isFinite(max) || max <= 0) return undefined
  const padded = max * Y_AXIS_PADDING
  const roughStep = padded / Y_AXIS_TICKS
  const magnitude = Math.pow(10, Math.floor(Math.log10(roughStep)))
  const step = (NICE_STEPS.find(candidate => candidate * magnitude >= roughStep) ?? NICE_STEPS[NICE_STEPS.length - 1]) * magnitude
  return Math.ceil(padded / step) * step
}
