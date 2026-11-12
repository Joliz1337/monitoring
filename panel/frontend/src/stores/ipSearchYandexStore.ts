/**
 * Yandex Cloud Store
 */

import { create } from 'zustand'
import { ycApi, YcAccount, YcSettings, YcStatus, YcCaughtIP, YcWorkerStatus, YcAccountError, YcManualObtainRequest, YcManualObtainResult } from '../api/ipSearchYandex'

interface YcManualObtainHistoryEntry {
  id: number
  timestamp: string
  accountLabel: string
  zone: string
  success: boolean
  ipAddress?: string
  addressId?: string
  error?: string
}

interface YcState {
  accounts: YcAccount[]
  caughtIps: YcCaughtIP[]
  settings: YcSettings | null
  status: YcStatus
  logs: string[]
  workerStatuses: Record<number, YcWorkerStatus>
  accountErrors: YcAccountError[]

  manualObtainHistory: YcManualObtainHistoryEntry[]
  isObtaining: boolean

  isLoadingAccounts: boolean
  isLoadingSettings: boolean
  isLoadingLogs: boolean
  isLoadingCaughtIps: boolean
  isSaving: boolean
  error: string | null
  logEventSource: EventSource | null

  // Actions
  fetchAccounts: () => Promise<void>
  createAccount: (data: { oauth_token: string; folder_id: string; proxy?: string; label?: string }) => Promise<YcAccount | null>
  updateAccount: (id: number, data: { enabled?: boolean; oauth_token?: string; proxy?: string; label?: string }) => Promise<YcAccount | null>
  deleteAccount: (id: number) => Promise<boolean>

  fetchSettings: () => Promise<void>
  updateSettings: (data: Partial<YcSettings>) => Promise<boolean>

  startWorker: () => Promise<boolean>
  stopWorker: () => Promise<boolean>
  startAccount: (accountId: number) => Promise<boolean>
  stopAccount: (accountId: number) => Promise<boolean>
  fetchStatus: () => Promise<void>
  fetchWorkerStatuses: () => Promise<void>
  clearAccountErrors: (accountId?: number) => Promise<void>

  fetchLogs: (lines?: number) => Promise<void>
  clearLogs: () => Promise<void>
  subscribeToLogs: () => void
  unsubscribeFromLogs: () => void

  fetchCaughtIps: () => Promise<void>
  clearCaughtIps: () => Promise<boolean>
  deleteCaughtIp: (ipId: number) => Promise<boolean>

  manualObtainIp: (data: YcManualObtainRequest, accountLabel: string, zone: string) => Promise<YcManualObtainResult | null>
  clearManualHistory: () => void

  clearError: () => void
}

export const useYcStore = create<YcState>((set, get) => ({
  accounts: [],
  caughtIps: [],
  settings: null,
  status: { status: 'stopped', pid: null, running: false },
  logs: [],
  workerStatuses: {},
  accountErrors: [],

  manualObtainHistory: [],
  isObtaining: false,

  isLoadingAccounts: false,
  isLoadingSettings: false,
  isLoadingLogs: false,
  isLoadingCaughtIps: false,
  isSaving: false,
  error: null,
  logEventSource: null,

  fetchAccounts: async () => {
    set({ isLoadingAccounts: true, error: null })
    try {
      const response = await ycApi.getAccounts()
      set({ accounts: response.data, isLoadingAccounts: false })
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isLoadingAccounts: false })
    }
  },

  createAccount: async (data) => {
    set({ isSaving: true, error: null })
    try {
      const response = await ycApi.createAccount(data)
      set(state => ({ accounts: [...state.accounts, response.data], isSaving: false }))
      return response.data
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isSaving: false })
      return null
    }
  },

  updateAccount: async (id, data) => {
    set({ isSaving: true, error: null })
    try {
      const response = await ycApi.updateAccount(id, data)
      set(state => ({
        accounts: state.accounts.map(a => a.id === id ? response.data : a),
        isSaving: false
      }))
      return response.data
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isSaving: false })
      return null
    }
  },

  deleteAccount: async (id) => {
    set({ isSaving: true, error: null })
    try {
      await ycApi.deleteAccount(id)
      set(state => ({
        accounts: state.accounts.filter(a => a.id !== id),
        isSaving: false
      }))
      return true
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isSaving: false })
      return false
    }
  },

  fetchSettings: async () => {
    set({ isLoadingSettings: true, error: null })
    try {
      const response = await ycApi.getSettings()
      set({ settings: response.data, isLoadingSettings: false })
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isLoadingSettings: false })
    }
  },

  updateSettings: async (data) => {
    set({ isSaving: true, error: null })
    try {
      const response = await ycApi.updateSettings(data)
      set({ settings: response.data, isSaving: false })
      return true
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isSaving: false })
      return false
    }
  },

  startWorker: async () => {
    set({ isSaving: true, error: null })
    try {
      const response = await ycApi.start()
      if (response.data.success) {
        set({
          status: { status: 'running', pid: null, running: true },
          isSaving: false
        })
        get().subscribeToLogs()
        await get().fetchWorkerStatuses()
        return true
      } else {
        set({ error: response.data.error || 'Failed', isSaving: false })
        return false
      }
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isSaving: false })
      return false
    }
  },

  stopWorker: async () => {
    set({ isSaving: true, error: null })
    try {
      const response = await ycApi.stop()
      if (response.data.success) {
        set({
          status: { status: 'stopped', pid: null, running: false },
          isSaving: false,
          workerStatuses: {},
        })
        get().unsubscribeFromLogs()
        return true
      } else {
        set({ error: response.data.error || 'Failed', isSaving: false })
        return false
      }
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isSaving: false })
      return false
    }
  },

  startAccount: async (accountId) => {
    set({ isSaving: true, error: null })
    try {
      const response = await ycApi.startAccount(accountId)
      if (response.data.success) {
        set({ isSaving: false })
        await get().fetchWorkerStatuses()
        await get().fetchStatus()
        get().subscribeToLogs()
        return true
      } else {
        set({ error: response.data.error || 'Failed', isSaving: false })
        return false
      }
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isSaving: false })
      return false
    }
  },

  stopAccount: async (accountId) => {
    set({ isSaving: true, error: null })
    try {
      const response = await ycApi.stopAccount(accountId)
      if (response.data.success) {
        set({ isSaving: false })
        await get().fetchWorkerStatuses()
        await get().fetchStatus()
        return true
      } else {
        set({ error: response.data.error || 'Failed', isSaving: false })
        return false
      }
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isSaving: false })
      return false
    }
  },

  fetchStatus: async () => {
    try {
      const response = await ycApi.getStatus()
      set({ status: response.data })
    } catch {
      // ignore
    }
  },

  fetchWorkerStatuses: async () => {
    try {
      const response = await ycApi.getWorkerStatuses()
      set({
        workerStatuses: response.data.workers || {},
        accountErrors: response.data.errors || [],
      })
    } catch {
      // ignore
    }
  },

  clearAccountErrors: async (accountId?: number) => {
    try {
      if (accountId !== undefined) {
        await ycApi.clearAccountErrors(accountId)
      }
      await get().fetchWorkerStatuses()
    } catch {
      // ignore
    }
  },

  fetchLogs: async (lines = 100) => {
    set({ isLoadingLogs: true })
    try {
      const response = await ycApi.getLogs(lines)
      set({ logs: response.data.logs, isLoadingLogs: false })
    } catch {
      set({ isLoadingLogs: false })
    }
  },

  clearLogs: async () => {
    try {
      await ycApi.clearLogs()
      set({ logs: [] })
    } catch {
      // ignore
    }
  },

  subscribeToLogs: () => {
    const { logEventSource } = get()
    if (logEventSource) return

    let synced = false
    const initialBatch: string[] = []

    const es = new EventSource(ycApi.getLogsStreamUrl(), { withCredentials: true })

    es.addEventListener('sync', () => {
      set({ logs: initialBatch })
      synced = true
    })

    es.onmessage = (event) => {
      if (!synced) {
        initialBatch.push(event.data)
        return
      }
      set(state => ({
        logs: [...state.logs, event.data].slice(-1000)
      }))
    }

    es.addEventListener('status', (event) => {
      const status = event.data as 'stopped' | 'running' | 'starting' | 'stopping'
      set(state => ({ status: { ...state.status, status } }))
    })

    es.onerror = () => {
      es.close()
      set({ logEventSource: null })
      setTimeout(() => {
        if (get().status.running) get().subscribeToLogs()
      }, 5000)
    }

    set({ logEventSource: es })
  },

  unsubscribeFromLogs: () => {
    const { logEventSource } = get()
    if (logEventSource) {
      logEventSource.close()
      set({ logEventSource: null })
    }
  },

  fetchCaughtIps: async () => {
    set({ isLoadingCaughtIps: true })
    try {
      const response = await ycApi.getCaughtIps()
      set({ caughtIps: response.data, isLoadingCaughtIps: false })
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isLoadingCaughtIps: false })
    }
  },

  clearCaughtIps: async () => {
    set({ isSaving: true, error: null })
    try {
      await ycApi.clearAllCaughtIps()
      await get().fetchAccounts()
      set({ caughtIps: [], isSaving: false })
      return true
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isSaving: false })
      return false
    }
  },

  deleteCaughtIp: async (ipId) => {
    set({ isSaving: true, error: null })
    try {
      await ycApi.deleteCaughtIp(ipId)
      set(state => ({
        caughtIps: state.caughtIps.filter(ip => ip.id !== ipId),
        isSaving: false
      }))
      await get().fetchAccounts()
      return true
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isSaving: false })
      return false
    }
  },

  manualObtainIp: async (data, accountLabel, zone) => {
    set({ isObtaining: true, error: null })
    try {
      const response = await ycApi.manualObtainIp(data)
      const result = response.data
      const entry: YcManualObtainHistoryEntry = {
        id: Date.now(),
        timestamp: new Date().toLocaleString('ru-RU'),
        accountLabel,
        zone,
        success: result.success,
        ipAddress: result.ip_address,
        addressId: result.address_id,
        error: result.error,
      }
      set(state => ({
        manualObtainHistory: [entry, ...state.manualObtainHistory],
        isObtaining: false,
      }))
      return result
    } catch (error: any) {
      const errMsg = error.response?.data?.detail || error.response?.data?.error || 'Ошибка запроса'
      const entry: YcManualObtainHistoryEntry = {
        id: Date.now(),
        timestamp: new Date().toLocaleString('ru-RU'),
        accountLabel,
        zone,
        success: false,
        error: errMsg,
      }
      set(state => ({
        manualObtainHistory: [entry, ...state.manualObtainHistory],
        isObtaining: false,
        error: errMsg,
      }))
      return null
    }
  },

  clearManualHistory: () => set({ manualObtainHistory: [] }),

  clearError: () => set({ error: null }),
}))

export default useYcStore
