import { create } from 'zustand'
import { toast } from 'sonner'
import {
  torrentBlockerApi,
  serversApi,
  type TorrentBlockerSettings,
  type TorrentBlockerStatus,
  type TorrentBlockerInternalStats,
  type TorrentBlockerStatsRange,
  type TorrentBlockerReport,
  type ServerWithMetrics,
} from '../api/client'

interface TorrentBlockerState {
  settings: TorrentBlockerSettings | null
  status: TorrentBlockerStatus | null
  internalStats: TorrentBlockerInternalStats | null
  statsRange: TorrentBlockerStatsRange
  reports: TorrentBlockerReport[]
  reportsTotal: number
  servers: ServerWithMetrics[]
  isLoading: boolean

  fetchSettings: () => Promise<void>
  updateSettings: (data: Partial<TorrentBlockerSettings>) => Promise<void>
  fetchStatus: () => Promise<void>
  fetchInternalStats: (range?: TorrentBlockerStatsRange) => Promise<void>
  setStatsRange: (range: TorrentBlockerStatsRange) => void
  fetchReports: (start?: number, size?: number) => Promise<void>
  fetchServers: () => Promise<void>
  pollNow: () => Promise<void>
  truncateReports: () => Promise<void>
}

export const useTorrentBlockerStore = create<TorrentBlockerState>((set, get) => ({
  settings: null,
  status: null,
  internalStats: null,
  statsRange: '24h',
  reports: [],
  reportsTotal: 0,
  servers: [],
  isLoading: false,

  fetchSettings: async () => {
    try {
      const { data } = await torrentBlockerApi.getSettings()
      set({ settings: data })
    } catch {
      // ignore
    }
  },

  updateSettings: async (data) => {
    try {
      const { data: updated } = await torrentBlockerApi.updateSettings(data)
      set({ settings: updated })
      toast.success('Settings saved')
    } catch {
      toast.error('Failed to save settings')
    }
  },

  fetchStatus: async () => {
    try {
      const { data } = await torrentBlockerApi.getStatus()
      set({ status: data })
    } catch {
      // ignore
    }
  },

  fetchInternalStats: async (range) => {
    const r = range ?? get().statsRange
    try {
      const { data } = await torrentBlockerApi.getInternalStats(r)
      set({ internalStats: data, statsRange: r })
    } catch {
      // ignore
    }
  },

  setStatsRange: (range) => {
    set({ statsRange: range })
    get().fetchInternalStats(range)
  },

  fetchReports: async (start = 0, size = 50) => {
    try {
      const { data } = await torrentBlockerApi.getReports(start, size)
      set({ reports: data.records || [], reportsTotal: data.total || 0 })
    } catch {
      // ignore
    }
  },

  fetchServers: async () => {
    try {
      const { data } = await serversApi.list()
      set({ servers: data.servers || [] })
    } catch {
      // ignore
    }
  },

  pollNow: async () => {
    try {
      await torrentBlockerApi.pollNow()
      toast.success('Poll triggered')
    } catch {
      toast.error('Failed to trigger poll')
    }
  },

  truncateReports: async () => {
    try {
      await torrentBlockerApi.truncate()
      set({ reports: [], reportsTotal: 0 })
      toast.success('Reports cleared')
    } catch {
      toast.error('Failed to clear reports')
    }
  },
}))
