import { create } from 'zustand'
import { toast } from 'sonner'
import i18n from '../i18n'
import { settingsApi } from '../api/client'
import { parseHiddenModules, serializeHiddenModules } from '../config/modules'
import {
  parseChartMode,
  parseChartModeOverrides,
  serializeChartModeOverrides,
  DEFAULT_CHART_MODE,
  type ChartMode,
  type ChartMetric,
  type ChartModeOverrides,
} from '../config/chartDisplay'

// Зеркало списка скрытых разделов в браузере: меню рисуется до ответа
// /settings, иначе при каждой загрузке страницы мигал бы полный список вкладок.
const HIDDEN_MODULES_CACHE_KEY = 'panel_hidden_modules'

function readCachedHiddenModules(): string[] {
  try {
    return parseHiddenModules(localStorage.getItem(HIDDEN_MODULES_CACHE_KEY))
  } catch {
    return []
  }
}

function cacheHiddenModules(ids: string[]): void {
  try {
    localStorage.setItem(HIDDEN_MODULES_CACHE_KEY, serializeHiddenModules(ids))
  } catch { /* приватный режим браузера */ }
}

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
  serverTimezone: string
  timeSyncEnabled: boolean
  remnawaveNginxPath: string
  updateBranch: string
  cpuAffinityEnabled: boolean
  hiddenModules: string[]
  chartMode: ChartMode
  chartPeaks: boolean
  chartModeOverrides: ChartModeOverrides
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
  setServerTimezone: (tz: string) => Promise<void>
  setTimeSyncEnabled: (enabled: boolean) => Promise<void>
  setRemnawaveNginxPath: (path: string) => Promise<void>
  setUpdateBranch: (branch: string) => Promise<void>
  setCpuAffinityEnabled: (enabled: boolean) => Promise<void>
  setHiddenModules: (ids: string[]) => Promise<void>
  setChartMode: (mode: ChartMode) => Promise<void>
  setChartPeaks: (enabled: boolean) => Promise<void>
  setChartModeOverride: (metric: ChartMetric, mode: ChartMode | null) => Promise<void>
  getEffectiveTimezone: () => string
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
  serverTimezone: 'Europe/Moscow',
  timeSyncEnabled: true,
  remnawaveNginxPath: '/opt/remnawave',
  updateBranch: 'main',
  cpuAffinityEnabled: false,
  hiddenModules: readCachedHiddenModules(),
  chartMode: DEFAULT_CHART_MODE,
  chartPeaks: true,
  chartModeOverrides: {},
  isLoading: true,

  fetchSettings: async () => {
    try {
      const { data } = await settingsApi.getAll()
      const hiddenModules = parseHiddenModules(data.settings.hidden_modules)
      cacheHiddenModules(hiddenModules)
      set({
        refreshInterval: parseInt(data.settings.refresh_interval || '30'),
        compactView: data.settings.compact_view === 'true',
        timezone: data.settings.timezone || 'auto',
        trafficPeriod: parseInt(data.settings.traffic_period || '30'),
        detailLevel: (data.settings.detail_level as DetailLevel) || 'standard',
        cardScale: (data.settings.card_scale as CardScale) || 'medium',
        metricsCollectInterval: parseInt(data.settings.metrics_collect_interval || '10'),
        haproxyCollectInterval: parseInt(data.settings.haproxy_collect_interval || '300'),
        serverTimezone: data.settings.server_timezone || 'Europe/Moscow',
        timeSyncEnabled: data.settings.time_sync_enabled !== 'false',
        remnawaveNginxPath: data.settings.remnawave_nginx_path || '/opt/remnawave',
        updateBranch: data.settings.update_branch || 'main',
        cpuAffinityEnabled: data.settings.cpu_affinity_enabled === 'true',
        hiddenModules,
        chartMode: parseChartMode(data.settings.chart_mode),
        chartPeaks: data.settings.chart_peaks !== 'false',
        chartModeOverrides: parseChartModeOverrides(data.settings.chart_mode_overrides),
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

  setServerTimezone: async (tz: string) => {
    set({ serverTimezone: tz })
    await settingsApi.set('server_timezone', tz)
    toast.success(i18n.t('common.saved'))
  },

  setTimeSyncEnabled: async (enabled: boolean) => {
    set({ timeSyncEnabled: enabled })
    await settingsApi.set('time_sync_enabled', enabled.toString())
    toast.success(i18n.t('common.saved'))
  },

  setRemnawaveNginxPath: async (path: string) => {
    set({ remnawaveNginxPath: path })
    await settingsApi.set('remnawave_nginx_path', path)
    toast.success(i18n.t('common.saved'))
  },

  setUpdateBranch: async (branch: string) => {
    set({ updateBranch: branch })
    await settingsApi.set('update_branch', branch)
    toast.success(i18n.t('common.saved'))
  },

  setCpuAffinityEnabled: async (enabled: boolean) => {
    set({ cpuAffinityEnabled: enabled })
    await settingsApi.set('cpu_affinity_enabled', enabled.toString())
    toast.success(i18n.t('common.saved'))
  },

  // Без тоста: результат виден сразу в боковом меню, а разделы переключают пачкой
  setHiddenModules: async (ids: string[]) => {
    set({ hiddenModules: ids })
    cacheHiddenModules(ids)
    await settingsApi.set('hidden_modules', serializeHiddenModules(ids))
  },

  // Без тоста: результат виден сразу на графиках
  setChartMode: async (mode: ChartMode) => {
    set({ chartMode: mode })
    await settingsApi.set('chart_mode', mode)
  },

  setChartPeaks: async (enabled: boolean) => {
    set({ chartPeaks: enabled })
    await settingsApi.set('chart_peaks', enabled.toString())
  },

  setChartModeOverride: async (metric: ChartMetric, mode: ChartMode | null) => {
    const overrides = { ...get().chartModeOverrides }
    if (mode === null) {
      delete overrides[metric]
    } else {
      overrides[metric] = mode
    }
    set({ chartModeOverrides: overrides })
    await settingsApi.set('chart_mode_overrides', serializeChartModeOverrides(overrides))
  },

  getEffectiveTimezone: () => {
    const { timezone } = get()
    if (timezone === 'auto') {
      return getBrowserTimezoneName()
    }
    return timezone
  },
  
}))
