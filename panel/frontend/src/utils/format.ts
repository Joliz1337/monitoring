export function formatBytes(bytes: number, decimals = 1): string {
  if (bytes === 0) return '0 B'
  
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(decimals))} ${sizes[i]}`
}

export function formatBytesPerSec(bytesPerSec: number): string {
  return `${formatBytes(bytesPerSec)}/s`
}

// Unit keys for localization
const BITS_UNITS = ['bps', 'kbps', 'mbps', 'gbps', 'tbps'] as const
const BYTES_UNITS = ['b_per_s', 'kb_per_s', 'mb_per_s', 'gb_per_s', 'tb_per_s'] as const

// Fallback units (English-style)
const BITS_FALLBACK = ['bit/s', 'Kbit/s', 'Mbit/s', 'Gbit/s', 'Tbit/s']

type TranslateFunction = (key: string) => string

export function formatBitsPerSec(bytesPerSec: number, decimals = 1): string {
  const bitsPerSec = bytesPerSec * 8
  if (bitsPerSec === 0) return `0 ${BITS_FALLBACK[0]}`
  
  const k = 1000
  const i = Math.min(Math.floor(Math.log(bitsPerSec) / Math.log(k)), BITS_FALLBACK.length - 1)
  
  return `${parseFloat((bitsPerSec / Math.pow(k, i)).toFixed(decimals))} ${BITS_FALLBACK[i]}`
}

export function formatBitsPerSecLocalized(
  bytesPerSec: number,
  t: TranslateFunction,
  decimals = 1
): string {
  const bitsPerSec = bytesPerSec * 8
  if (bitsPerSec === 0) return `0 ${t(`units.${BITS_UNITS[0]}`)}`
  
  const k = 1000
  const i = Math.min(Math.floor(Math.log(bitsPerSec) / Math.log(k)), BITS_UNITS.length - 1)
  const unit = t(`units.${BITS_UNITS[i]}`)
  
  return `${parseFloat((bitsPerSec / Math.pow(k, i)).toFixed(decimals))} ${unit}`
}

export function createBitsFormatter(t: TranslateFunction, decimals = 1) {
  return (bytesPerSec: number) => formatBitsPerSecLocalized(bytesPerSec, t, decimals)
}

export function formatBytesPerSecLocalized(
  bytesPerSec: number,
  t: TranslateFunction,
  decimals = 1
): string {
  if (bytesPerSec === 0) return `0 ${t(`units.${BYTES_UNITS[0]}`)}`
  
  const k = 1024
  const i = Math.min(Math.floor(Math.log(bytesPerSec) / Math.log(k)), BYTES_UNITS.length - 1)
  const unit = t(`units.${BYTES_UNITS[i]}`)
  
  return `${parseFloat((bytesPerSec / Math.pow(k, i)).toFixed(decimals))} ${unit}`
}

export function createBytesPerSecFormatter(t: TranslateFunction, decimals = 1) {
  return (bytesPerSec: number) => formatBytesPerSecLocalized(bytesPerSec, t, decimals)
}

export function formatUptime(seconds: number): string {
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  
  if (days > 0) {
    return `${days}d ${hours}h`
  }
  if (hours > 0) {
    return `${hours}h ${minutes}m`
  }
  return `${minutes}m`
}

export function formatNumber(num: number, decimals = 1): string {
  if (num >= 1000000) {
    return `${(num / 1000000).toFixed(decimals)}M`
  }
  if (num >= 1000) {
    return `${(num / 1000).toFixed(decimals)}K`
  }
  return num.toString()
}

export function formatPercent(value: number, decimals = 1): string {
  return `${value.toFixed(decimals)}%`
}

export function formatDate(date: string | Date): string {
  const d = new Date(date)
  return d.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function formatTimeAgo(date: string | Date | null | undefined): string {
  if (!date) return 'Never'
  
  const d = new Date(date)
  const now = new Date()
  const seconds = Math.floor((now.getTime() - d.getTime()) / 1000)
  
  if (seconds < 60) return 'Just now'
  
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days}d ago`
  
  const months = Math.floor(days / 30)
  return `${months}mo ago`
}

/**
 * Convert timestamp from server timezone to target timezone
 * @param timestamp - ISO timestamp string from server
 * @param serverOffsetSeconds - Server timezone offset in seconds (e.g., 10800 for +03:00)
 * @param targetOffsetSeconds - Target timezone offset in seconds
 * @returns Adjusted Date object in target timezone
 */
export function convertTimezone(
  timestamp: string,
  serverOffsetSeconds: number,
  targetOffsetSeconds: number
): Date {
  const date = new Date(timestamp)
  
  // If timestamp has no timezone info (naive datetime from server),
  // we need to interpret it as server's local time
  if (!timestamp.includes('Z') && !timestamp.includes('+') && !timestamp.match(/-\d{2}:\d{2}$/)) {
    // Naive timestamp - interpret as server local time
    // Get the UTC time by subtracting server offset
    const utcTime = date.getTime() - serverOffsetSeconds * 1000
    // Apply target offset to get target local time
    return new Date(utcTime + targetOffsetSeconds * 1000)
  }
  
  // If timestamp has timezone info, convert normally
  const utcTime = date.getTime()
  return new Date(utcTime + targetOffsetSeconds * 1000)
}

/**
 * Format timestamp for display in target timezone
 */
export function formatTimestampInTimezone(
  timestamp: string,
  serverOffsetSeconds: number,
  targetTimezone: string,
  format: 'time' | 'datetime' | 'full' = 'datetime'
): string {
  try {
    const date = new Date(timestamp)
    
    // For naive timestamps, adjust for server timezone first
    let adjustedDate = date
    if (!timestamp.includes('Z') && !timestamp.includes('+') && !timestamp.match(/-\d{2}:\d{2}$/)) {
      const utcTime = date.getTime() - serverOffsetSeconds * 1000
      adjustedDate = new Date(utcTime)
    }
    
    const options: Intl.DateTimeFormatOptions = { timeZone: targetTimezone }
    
    switch (format) {
      case 'time':
        options.hour = '2-digit'
        options.minute = '2-digit'
        options.second = '2-digit'
        break
      case 'datetime':
        options.month = 'short'
        options.day = 'numeric'
        options.hour = '2-digit'
        options.minute = '2-digit'
        break
      case 'full':
        options.year = 'numeric'
        options.month = 'short'
        options.day = 'numeric'
        options.hour = '2-digit'
        options.minute = '2-digit'
        options.second = '2-digit'
        break
    }
    
    return adjustedDate.toLocaleString('en-US', options)
  } catch {
    return formatDate(timestamp)
  }
}

/**
 * Get timezone offset string from seconds (e.g., 10800 -> "+03:00")
 */
export function formatTimezoneOffset(offsetSeconds: number): string {
  const sign = offsetSeconds >= 0 ? '+' : '-'
  const absSeconds = Math.abs(offsetSeconds)
  const hours = Math.floor(absSeconds / 3600)
  const minutes = Math.floor((absSeconds % 3600) / 60)
  return `${sign}${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}`
}