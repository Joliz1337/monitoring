/**
 * Chart optimization utilities
 * LTTB (Largest Triangle Three Buckets) downsampling algorithm
 * Exponential Moving Average (EMA) smoothing
 */

export interface ChartPoint {
  x: number
  y: number
}

// Maximum points to render for optimal performance
export const MAX_CHART_POINTS = 150

// Y-axis padding multiplier for stability
export const Y_AXIS_PADDING = 1.1

// Default smoothing factor for EMA (0-1, higher = more smoothing)
export const DEFAULT_SMOOTHING_FACTOR = 0.3

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

/**
 * Calculate dynamic Y-axis maximum with padding
 * Prevents scale from jumping on data updates
 * 
 * @param data - Array of chart points
 * @param minMax - Minimum maximum value (e.g., 100 for percentages)
 * @returns Calculated maximum with padding
 */
export function calculateDynamicYMax(data: ChartPoint[], minMax?: number): number | undefined {
  if (data.length === 0) return minMax
  
  const maxValue = Math.max(...data.map(d => d.y))
  
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
 * @param seriesData - Array of series, each containing array of points
 * @returns Calculated maximum with padding
 */
export function calculateMultiSeriesYMax(seriesData: ChartPoint[][]): number | undefined {
  if (seriesData.length === 0) return undefined
  
  const allValues = seriesData.flatMap(series => series.map(d => d.y))
  
  if (allValues.length === 0) return undefined
  
  const maxValue = Math.max(...allValues)
  
  if (maxValue === 0) return undefined
  
  // Add padding
  const paddedMax = maxValue * Y_AXIS_PADDING
  
  // Round up to a nice number
  const magnitude = Math.pow(10, Math.floor(Math.log10(paddedMax)))
  return Math.ceil(paddedMax / magnitude) * magnitude
}
