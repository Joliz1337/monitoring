/**
 * Chart optimization utilities
 * LTTB (Largest Triangle Three Buckets) downsampling algorithm
 * Exponential Moving Average (EMA) smoothing
 * Gap-aware pipeline for series with missing data
 */

export interface ChartPoint {
  x: number
  y: number
}

/** Series point where y === null means "no data collected at this moment" */
export interface ChartPointWithGap {
  x: number
  y: number | null
}

/** Period when a server was unreachable; to === null means it is still down */
export interface ChartGap {
  from: string
  to: string | null
}

export interface GapAwareProcessingOptions {
  smoothing?: number
  maxPoints?: number
}

// Maximum points to render for optimal performance
export const MAX_CHART_POINTS = 150

// Y-axis padding multiplier for stability
export const Y_AXIS_PADDING = 1.1

// Default smoothing factor for EMA (0-1, higher = more smoothing)
export const DEFAULT_SMOOTHING_FACTOR = 0.3

// A segment shorter than this cannot be downsampled by LTTB, only clipped to its ends
const MIN_POINTS_FOR_LTTB = 3

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
  // Replace space with T and add Z to treat as UTC
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

  // Fallback: let browser parse it
  return new Date(timestamp).getTime()
}

/**
 * Exponential Moving Average (EMA) smoothing
 * Smooths out sharp spikes while preserving overall trends
 * 
 * @param data - Array of chart points
 * @param alpha - Smoothing factor (0-1). Higher = more smoothing, slower response
 * @returns Smoothed array of points
 */
export function smoothDataEMA(data: ChartPoint[], alpha: number = DEFAULT_SMOOTHING_FACTOR): ChartPoint[] {
  if (data.length < 2) return data
  
  const smoothed: ChartPoint[] = []
  
  // First point stays the same
  smoothed.push({ ...data[0] })
  
  // Apply EMA: smoothed[i] = alpha * smoothed[i-1] + (1 - alpha) * data[i]
  for (let i = 1; i < data.length; i++) {
    smoothed.push({
      x: data[i].x,
      y: alpha * smoothed[i - 1].y + (1 - alpha) * data[i].y
    })
  }
  
  return smoothed
}

/**
 * Double Exponential Moving Average (DEMA) for even smoother curves
 * Applies EMA twice - forward and backward to reduce lag
 * 
 * @param data - Array of chart points
 * @param alpha - Smoothing factor (0-1)
 * @returns Smoothed array of points
 */
export function smoothDataDEMA(data: ChartPoint[], alpha: number = DEFAULT_SMOOTHING_FACTOR): ChartPoint[] {
  if (data.length < 3) return data
  
  // Forward pass
  const forward = smoothDataEMA(data, alpha)
  
  // Backward pass (reverse, apply EMA, reverse back)
  const reversed = [...forward].reverse()
  const backwardEMA = smoothDataEMA(reversed, alpha)
  const backward = backwardEMA.reverse()
  
  // Average forward and backward for lag-free smoothing
  return data.map((point, i) => ({
    x: point.x,
    y: (forward[i].y + backward[i].y) / 2
  }))
}

/**
 * LTTB (Largest Triangle Three Buckets) downsampling algorithm
 * Reduces data points while preserving visual characteristics (peaks, valleys)
 * 
 * @param data - Array of chart points with x (timestamp) and y (value)
 * @param threshold - Target number of points (default: MAX_CHART_POINTS)
 * @returns Downsampled array of points
 */
export function downsampleLTTB(data: ChartPoint[], threshold: number = MAX_CHART_POINTS): ChartPoint[] {
  if (data.length <= threshold || threshold < 3) {
    return data
  }

  const sampled: ChartPoint[] = []
  
  // Bucket size (leave room for first and last points)
  const bucketSize = (data.length - 2) / (threshold - 2)
  
  // Always keep the first point
  sampled.push(data[0])
  
  let prevSelectedIndex = 0
  
  for (let i = 0; i < threshold - 2; i++) {
    // Calculate bucket boundaries
    const bucketStart = Math.floor((i + 1) * bucketSize) + 1
    const bucketEnd = Math.floor((i + 2) * bucketSize) + 1
    const nextBucketEnd = Math.min(Math.floor((i + 3) * bucketSize) + 1, data.length)
    
    // Calculate average point of next bucket (for triangle area calculation)
    let avgX = 0
    let avgY = 0
    let avgCount = 0
    
    for (let j = bucketEnd; j < nextBucketEnd; j++) {
      avgX += data[j].x
      avgY += data[j].y
      avgCount++
    }
    
    if (avgCount > 0) {
      avgX /= avgCount
      avgY /= avgCount
    } else {
      // Fallback to last point if no next bucket
      avgX = data[data.length - 1].x
      avgY = data[data.length - 1].y
    }
    
    // Find point in current bucket with largest triangle area
    let maxArea = -1
    let maxAreaIndex = bucketStart
    
    const prevPoint = data[prevSelectedIndex]
    
    for (let j = bucketStart; j < bucketEnd && j < data.length; j++) {
      // Triangle area calculation (simplified - we only need relative comparison)
      const area = Math.abs(
        (prevPoint.x - avgX) * (data[j].y - prevPoint.y) -
        (prevPoint.x - data[j].x) * (avgY - prevPoint.y)
      )
      
      if (area > maxArea) {
        maxArea = area
        maxAreaIndex = j
      }
    }
    
    sampled.push(data[maxAreaIndex])
    prevSelectedIndex = maxAreaIndex
  }
  
  // Always keep the last point
  sampled.push(data[data.length - 1])
  
  return sampled
}

function hasValue(point: ChartPointWithGap): point is ChartPoint {
  return point.y !== null && Number.isFinite(point.y)
}

/**
 * Split a series into continuous segments, dropping the gaps between them
 *
 * @param data - Array of points where null y marks missing data
 * @returns Segments of consecutive points that actually have values
 */
export function splitOnGaps(data: ChartPointWithGap[]): ChartPoint[][] {
  const segments: ChartPoint[][] = []
  let current: ChartPoint[] = []

  for (const point of data) {
    if (!hasValue(point)) {
      if (current.length > 0) {
        segments.push(current)
        current = []
      }
      continue
    }
    current.push({ x: point.x, y: point.y })
  }

  if (current.length > 0) {
    segments.push(current)
  }

  return segments
}

/** LTTB refuses thresholds below 3, so tiny budgets keep only the segment ends */
function limitSegmentPoints(segment: ChartPoint[], budget: number): ChartPoint[] {
  if (segment.length <= budget) return segment
  if (budget < MIN_POINTS_FOR_LTTB) return [segment[0], segment[segment.length - 1]]
  return downsampleLTTB(segment, budget)
}

function gapMarkerX(previous: ChartPoint[], next: ChartPoint[]): number {
  const from = previous[previous.length - 1].x
  const to = next[0].x
  return from + (to - from) / 2
}

/**
 * Smooth and downsample a series that may contain gaps
 *
 * EMA/DEMA and LTTB both spread a single null over the whole series (NaN propagation
 * in the smoothing recursion, unstable triangle areas in LTTB), so the series is cut
 * into continuous segments first and each one goes through the normal pipeline alone.
 * Segments are rejoined with exactly one null point so ApexCharts breaks the line there.
 *
 * The point budget is split proportionally to segment length: giving every segment the
 * full budget would let a short tail after a gap dominate the chart.
 *
 * @param data - Array of points where null y marks missing data
 * @param options - Smoothing factor (0 disables it) and total point budget
 * @returns Points ready for ApexCharts, with nulls only between segments
 */
/**
 * Split the point budget between segments by largest remainder.
 *
 * A plain per-segment floor blows the budget apart: a series alternating value/gap yields
 * hundreds of tiny segments, and two guaranteed points each add up to multiples of
 * MAX_CHART_POINTS. Here the shares sum to exactly maxPoints, and only the unavoidable
 * "at least one point per segment" can push past it — bounded by the segment count itself.
 */
function allocateSegmentBudgets(segments: ChartPoint[][], maxPoints: number): number[] {
  const totalPoints = segments.reduce((sum, segment) => sum + segment.length, 0)
  if (totalPoints === 0) return segments.map(() => 0)

  if (segments.length >= maxPoints) return segments.map(() => 1)

  const quotas = segments.map((segment) => (maxPoints * segment.length) / totalPoints)
  const budgets = quotas.map((quota) => Math.max(1, Math.floor(quota)))

  let spare = maxPoints - budgets.reduce((sum, value) => sum + value, 0)
  const byRemainder = quotas
    .map((quota, index) => ({ index, remainder: quota - Math.floor(quota) }))
    .sort((a, b) => b.remainder - a.remainder)

  for (let i = 0; spare > 0 && i < byRemainder.length; i += 1, spare -= 1) {
    budgets[byRemainder[i].index] += 1
  }
  return budgets.map((budget, index) => Math.min(budget, segments[index].length))
}

export function processSeriesWithGaps(
  data: ChartPointWithGap[],
  options: GapAwareProcessingOptions = {},
): ChartPointWithGap[] {
  const { smoothing = DEFAULT_SMOOTHING_FACTOR, maxPoints = MAX_CHART_POINTS } = options

  const segments = splitOnGaps(data)
  if (segments.length === 0) return []

  const budgets = allocateSegmentBudgets(segments, maxPoints)
  const result: ChartPointWithGap[] = []

  // Keep the original bounds so the x-axis still spans the requested period
  if (!hasValue(data[0])) {
    result.push({ x: data[0].x, y: null })
  }

  segments.forEach((segment, index) => {
    if (index > 0) {
      result.push({ x: gapMarkerX(segments[index - 1], segment), y: null })
    }

    const smoothed = smoothing > 0 ? smoothDataDEMA(segment, smoothing) : segment
    result.push(...limitSegmentPoints(smoothed, budgets[index]))
  })

  const lastInputPoint = data[data.length - 1]
  if (!hasValue(lastInputPoint)) {
    result.push({ x: lastInputPoint.x, y: null })
  }

  return result
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

/** Gaps must never reach Math.max: a single null turns the whole axis bound into NaN */
function collectFiniteValues(seriesData: ChartPointWithGap[][]): number[] {
  return seriesData.flatMap(series => series.filter(hasValue).map(point => point.y))
}

/**
 * Calculate dynamic Y-axis maximum with padding
 * Prevents scale from jumping on data updates
 *
 * @param data - Array of chart points, gaps are ignored
 * @param minMax - Minimum maximum value (e.g., 100 for percentages)
 * @returns Calculated maximum with padding
 */
export function calculateDynamicYMax(data: ChartPointWithGap[], minMax?: number): number | undefined {
  const values = collectFiniteValues([data])

  if (values.length === 0) return minMax

  const maxValue = Math.max(...values)

  if (maxValue === 0) return minMax
  
  // Add padding to prevent scale from touching the line
  const paddedMax = maxValue * Y_AXIS_PADDING
  
  // Round up to a nice number for cleaner axis labels
  const magnitude = Math.pow(10, Math.floor(Math.log10(paddedMax)))
  const rounded = Math.ceil(paddedMax / magnitude) * magnitude
  
  // Return the larger of calculated max or minimum required max
  if (minMax !== undefined) {
    return Math.max(rounded, minMax)
  }
  
  return rounded
}

/**
 * Calculate dynamic Y-axis maximum for multiple series
 *
 * @param seriesData - Array of series, each containing array of points; gaps are ignored
 * @returns Calculated maximum with padding
 */
export function calculateMultiSeriesYMax(seriesData: ChartPointWithGap[][]): number | undefined {
  const allValues = collectFiniteValues(seriesData)

  if (allValues.length === 0) return undefined

  const maxValue = Math.max(...allValues)

  if (maxValue === 0) return undefined
  
  // Add padding
  const paddedMax = maxValue * Y_AXIS_PADDING
  
  // Round up to a nice number
  const magnitude = Math.pow(10, Math.floor(Math.log10(paddedMax)))
  return Math.ceil(paddedMax / magnitude) * magnitude
}
