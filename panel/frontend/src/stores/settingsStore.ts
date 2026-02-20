import { create } from 'zustand'
import { toast } from 'sonner'
import i18n from '../i18n'
import { settingsApi } from '../api/client'

// Get browser timezone offset in format "+03:00" or "-05:00"
function getBrowserTimezone(): string {
  const offset = -new Date().getTimezoneOffset()
  const sign = offset >= 0 ? '+' : '-'
  const hours = Math.floor(Math.abs(offset) / 60)
  const minutes = Math.abs(offset) % 60
  return `${sign}${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}`
}

// Get browser timezone name (e.g., "Europe/Moscow")
function getBrowserTimezoneName(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone
}

export interface TimezoneOption {
  value: string
  label: string
  offset: string
}

// Common timezone options
export const TIMEZONE_OPTIONS: TimezoneOption[] = [
  { value: 'auto', label: 'Auto (Browser)', offset: getBrowserTimezone() },
  { value: 'UTC', label: 'UTC', offset: '+00:00' },
  { value: 'Europe/Moscow', label: 'Moscow (MSK)', offset: '+03:00' },
  { value: 'Europe/London', label: 'London (GMT/BST)', offset: '+00:00' },
  { value: 'Europe/Berlin', label: 'Berlin (CET)', offset: '+01:00' },
  { value: 'Europe/Kiev', label: 'Kyiv (EET)', offset: '+02:00' },
  { value: 'Asia/Dubai', label: 'Dubai (GST)', offset: '+04:00' },
  { value: 'Asia/Almaty', label: 'Almaty (ALMT)', offset: '+06:00' },
  { value: 'Asia/Shanghai', label: 'Shanghai (CST)', offset: '+08:00' },
  { value: 'Asia/Tokyo', label: 'Tokyo (JST)', offset: '+09:00' },
  { value: 'America/New_York', label: 'New York (EST)', offset: '-05:00' },
  { value: 'America/Los_Angeles', label: 'Los Angeles (PST)', offset: '-08:00' },
]

export interface TrafficPeriodOption {
  value: number
  label: string
}

export const TRAFFIC_PERIOD_OPTIONS: TrafficPeriodOption[] = [
  { value: 1, label: '1 day' },
  { value: 7, label: '7 days' },
  { value: 30, label: '30 days' },
  { value: 90, label: '90 days' },
]

// Collector interval options with recommended values marked
export interface CollectorIntervalOption {
  value: number
  label: string
  recommended?: boolean
}

export const METRICS_INTERVAL_OPTIONS: CollectorIntervalOption[] = [
  { value: 5, label: '5s' },
  { value: 10, label: '10s', recommended: true },
  { value: 15, label: '15s', recommended: true },
  { value: 30, label: '30s' },
  { value: 60, label: '1m' },
]

export const HAPROXY_INTERVAL_OPTIONS: CollectorIntervalOption[] = [
  { value: 60, label: '1m' },
  { value: 120, label: '2m' },
  { value: 300, label: '5m', recommended: true },
  { value: 600, label: '10m' },
]

export type DetailLevel = 'minimal' | 'standard' | 'detailed'
export type CardScale = 'small' | 'medium' | 'large'

interface SettingsState {
  refreshInterval: number
  compactView: boolean
  timezone: string
  trafficPeriod: number
  detailLevel: DetailLevel
  cardScale: CardScale
  metricsCollectInterval: number
  haproxyCollectInterval: number
  isLoading: boolean
  
  fetchSettings: () => Promise<void>
  setRefreshInterval: (interval: number) => Promise<void>
  setCompactView: (compact: boolean) => Promise<void>
  setTimezone: (tz: string) => Promise<void>
  setTrafficPeriod: (days: number) => Promise<void>
  setDetailLevel: (level: DetailLevel) => Promise<void>
  setCardScale: (scale: CardScale) => Promise<void>
  setMetricsCollectInterval: (interval: number) => Promise<void>
  setHaproxyCollectInterval: (interval: number) => Promise<void>
  getEffectiveTimezone: () => string
  getTimezoneOffset: () => number
}

export const useSettingsStore = create<SettingsState>((set, get) => ({
  refreshInterval: 5,
  compactView: false,
  timezone: 'auto',
  trafficPeriod: 30,
  detailLevel: 'standard',
  cardScale: 'medium',
  metricsCollectInterval: 10,
  haproxyCollectInterval: 300,
  isLoading: true,
  
  fetchSettings: async () => {
    try {
      const { data } = await settingsApi.getAll()
      set({
        refreshInterval: parseInt(data.settings.refresh_interval || '30'),
        compactView: data.settings.compact_view === 'true',
        timezone: data.settings.timezone || 'auto',
        trafficPeriod: parseInt(data.settings.traffic_period || '30'),
        detailLevel: (data.settings.detail_level as DetailLevel) || 'standard',
        cardScale: (data.settings.card_scale as CardScale) || 'medium',
        metricsCollectInterval: parseInt(data.settings.metrics_collect_interval || '10'),
        haproxyCollectInterval: parseInt(data.settings.haproxy_collect_interval || '300'),
        isLoading: false,
      })
    } catch {
      set({ isLoading: false })
    }
  },
  
  setRefreshInterval: async (interval: number) => {
    set({ refreshInterval: interval })
    await settingsApi.set('refresh_interval', interval.toString())
  },
  
  setCompactView: async (compact: boolean) => {
    set({ compactView: compact })
    await settingsApi.set('compact_view', compact.toString())
  },
  
  setTimezone: async (tz: string) => {
    set({ timezone: tz })
    await settingsApi.set('timezone', tz)
  },
  
  setTrafficPeriod: async (days: number) => {
    set({ trafficPeriod: days })
    await settingsApi.set('traffic_period', days.toString())
  },
  
  setDetailLevel: async (level: DetailLevel) => {
    set({ detailLevel: level })
    await settingsApi.set('detail_level', level)
  },
  
  setCardScale: async (scale: CardScale) => {
    set({ cardScale: scale })
    await settingsApi.set('card_scale', scale)
  },
  
  setMetricsCollectInterval: async (interval: number) => {
    set({ metricsCollectInterval: interval })
    await settingsApi.set('metrics_collect_interval', interval.toString())
    toast.success(i18n.t('common.saved'))
  },
  
  setHaproxyCollectInterval: async (interval: number) => {
    set({ haproxyCollectInterval: interval })
    await settingsApi.set('haproxy_collect_interval', interval.toString())
    toast.success(i18n.t('common.saved'))
  },
  
  getEffectiveTimezone: () => {
    const { timezone } = get()
    if (timezone === 'auto') {
      return getBrowserTimezoneName()
    }
    return timezone
  },
  
  getTimezoneOffset: () => {
    const { timezone } = get()
    if (timezone === 'auto') {
      return -new Date().getTimezoneOffset() * 60
    }
    const option = TIMEZONE_OPTIONS.find(o => o.value === timezone)
    if (option) {
      const match = option.offset.match(/([+-])(\d{2}):(\d{2})/)
      if (match) {
        const sign = match[1] === '+' ? 1 : -1
        const hours = parseInt(match[2])
        const minutes = parseInt(match[3])
        return sign * (hours * 3600 + minutes * 60)
      }
    }
    return 3 * 3600 // Default to Moscow (+03:00)
  },
}))
