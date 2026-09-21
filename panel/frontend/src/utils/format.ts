export function formatBytes(bytes: number, decimals = 1): string {
  if (bytes === 0) return '0 B'
  
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(decimals))} ${sizes[i]}`
}

// Unit keys for localization
const BITS_UNITS = ['bps', 'kbps', 'mbps', 'gbps', 'tbps'] as const

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

/** Эмодзи-флаг по ISO-2 коду страны; неизвестная страна — глобус */
export function getFlag(code: string | null | undefined): string {
  if (!code || code === 'XX') return '🌐'
  return code
    .toUpperCase()
    .split('')
    .map(c => String.fromCodePoint(0x1F1E6 + c.charCodeAt(0) - 65))
    .join('')
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
  const remainMinutes = minutes % 60
  if (hours < 24) {
    return remainMinutes > 0 ? `${hours}h ${remainMinutes}m ago` : `${hours}h ago`
  }

  const days = Math.floor(hours / 24)
  const remainHours = hours % 24
  if (days < 30) {
    return remainHours > 0 ? `${days}d ${remainHours}h ago` : `${days}d ago`
  }

  const months = Math.floor(days / 30)
  return `${months}mo ago`
}

/**
 * Извлекает хост (IP/домен) из URL ноды
 */
export function extractHost(url: string): string {
  try {
    return new URL(url).hostname
  } catch {
    const match = url.match(/https?:\/\/([^:/]+)/)
    return match ? match[1] : url
  }
}
