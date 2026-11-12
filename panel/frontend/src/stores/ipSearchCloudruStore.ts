/**
 * Cloud.ru Store — Evolution и Advanced, по стору на редакцию из одной фабрики
 */

import { create } from 'zustand'
import {
  cloudEvoApi,
  cloudAdvApi,
  CloudApi,
  CloudAccount,
  CloudAccountInput,
  CloudSettings,
  CloudStatus,
  CloudCaughtIP,
  CloudWorkerStatus,
  CloudAccountError,
  CloudWhitelistEntry,
  CloudManualObtainRequest,
  CloudManualObtainResult,
} from '../api/ipSearchCloudru'

interface CloudManualObtainHistoryEntry {
  id: number
  timestamp: string
  accountLabel: string
  zone: string
  success: boolean
  ipAddress?: string
  eipId?: string
  error?: string
}

interface CloudState {
  accounts: CloudAccount[]
  caughtIps: CloudCaughtIP[]
  settings: CloudSettings | null
  whitelist: CloudWhitelistEntry[]
  status: CloudStatus
  logs: string[]
  workerStatuses: Record<number, CloudWorkerStatus>
  accountErrors: CloudAccountError[]

  manualObtainHistory: CloudManualObtainHistoryEntry[]
  isObtaining: boolean

  isLoadingAccounts: boolean
  isLoadingSettings: boolean
  isLoadingLogs: boolean
  isLoadingCaughtIps: boolean
  isLoadingWhitelist: boolean
  isSaving: boolean
  error: string | null
  logEventSource: EventSource | null

  fetchAccounts: () => Promise<void>
  createAccount: (data: CloudAccountInput) => Promise<CloudAccount | null>
  updateAccount: (id: number, data: CloudAccountInput) => Promise<CloudAccount | null>
  deleteAccount: (id: number) => Promise<boolean>

  fetchSettings: () => Promise<void>
  updateSettings: (data: Partial<CloudSettings>) => Promise<boolean>

  fetchWhitelist: () => Promise<void>
  addWhitelistCidr: (cidr: string) => Promise<boolean>
  addWhitelistCidrs: (cidrs: string[]) => Promise<{ added: number; skipped: number; invalid: string[] } | null>
  removeWhitelistCidr: (id: number) => Promise<boolean>
  clearWhitelist: () => Promise<boolean>

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
  clearAccountCaughtIps: (accountId: number) => Promise<boolean>

  manualObtainIp: (data: CloudManualObtainRequest, accountLabel: string, zone: string) => Promise<CloudManualObtainResult | null>
  clearManualHistory: () => void

  clearError: () => void
}

const createCloudStore = (cloudApi: CloudApi) => create<CloudState>((set, get) => ({
  accounts: [],
  caughtIps: [],
  settings: null,
  whitelist: [],
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
  isLoadingWhitelist: false,
  isSaving: false,
  error: null,
  logEventSource: null,

  fetchAccounts: async () => {
    set({ isLoadingAccounts: true, error: null })
    try {
      const response = await cloudApi.getAccounts()
      set({ accounts: response.data, isLoadingAccounts: false })
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isLoadingAccounts: false })
    }
  },

  createAccount: async (data) => {
    set({ isSaving: true, error: null })
    try {
      const response = await cloudApi.createAccount(data)
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
      const response = await cloudApi.updateAccount(id, data)
      set(state => ({
        accounts: state.accounts.map(a => a.id === id ? response.data : a),
        isSaving: false,
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
      await cloudApi.deleteAccount(id)
      set(state => ({
        accounts: state.accounts.filter(a => a.id !== id),
        isSaving: false,
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
      const response = await cloudApi.getSettings()
      set({ settings: response.data, isLoadingSettings: false })
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isLoadingSettings: false })
    }
  },

  updateSettings: async (data) => {
    set({ isSaving: true, error: null })
    try {
      const response = await cloudApi.updateSettings(data)
      set({ settings: response.data, isSaving: false })
      return true
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isSaving: false })
      return false
    }
  },

  fetchWhitelist: async () => {
    set({ isLoadingWhitelist: true })
    try {
      const response = await cloudApi.getWhitelist()
      set({ whitelist: response.data, isLoadingWhitelist: false })
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isLoadingWhitelist: false })
    }
  },

  addWhitelistCidr: async (cidr) => {
    set({ isSaving: true, error: null })
    try {
      const response = await cloudApi.addWhitelistCidr(cidr)
      if (response.data.success) {
        await get().fetchWhitelist()
        await get().fetchSettings()
        set({ isSaving: false })
        return true
      }
      set({ error: response.data.error || 'Failed', isSaving: false })
      return false
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isSaving: false })
      return false
    }
  },

  addWhitelistCidrs: async (cidrs) => {
    set({ isSaving: true, error: null })
    try {
      const response = await cloudApi.addWhitelistCidrs(cidrs)
      await get().fetchWhitelist()
      await get().fetchSettings()
      set({ isSaving: false })
      return response.data
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isSaving: false })
      return null
    }
  },

  removeWhitelistCidr: async (id) => {
    set({ isSaving: true, error: null })
    try {
      await cloudApi.removeWhitelistCidr(id)
      set(state => ({
        whitelist: state.whitelist.filter(w => w.id !== id),
        isSaving: false,
      }))
      await get().fetchSettings()
      return true
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isSaving: false })
      return false
    }
  },

  clearWhitelist: async () => {
    set({ isSaving: true, error: null })
    try {
      await cloudApi.clearWhitelist()
      set({ whitelist: [], isSaving: false })
      await get().fetchSettings()
      return true
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isSaving: false })
      return false
    }
  },

  startWorker: async () => {
    set({ isSaving: true, error: null })
    try {
      const response = await cloudApi.start()
      if (response.data.success) {
        set({
          status: { status: 'running', pid: null, running: true },
          isSaving: false,
        })
        get().subscribeToLogs()
        await get().fetchWorkerStatuses()
        return true
      }
      set({ error: response.data.error || 'Failed', isSaving: false })
      return false
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isSaving: false })
      return false
    }
  },

  stopWorker: async () => {
    set({ isSaving: true, error: null })
    try {
      const response = await cloudApi.stop()
      if (response.data.success) {
        set({
          status: { status: 'stopped', pid: null, running: false },
          isSaving: false,
          workerStatuses: {},
        })
        get().unsubscribeFromLogs()
        return true
      }
      set({ error: response.data.error || 'Failed', isSaving: false })
      return false
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isSaving: false })
      return false
    }
  },

  startAccount: async (accountId) => {
    set({ isSaving: true, error: null })
    try {
      const response = await cloudApi.startAccount(accountId)
      if (response.data.success) {
        set({ isSaving: false })
        await get().fetchWorkerStatuses()
        await get().fetchStatus()
        get().subscribeToLogs()
        return true
      }
      set({ error: response.data.error || 'Failed', isSaving: false })
      return false
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isSaving: false })
      return false
    }
  },

  stopAccount: async (accountId) => {
    set({ isSaving: true, error: null })
    try {
      const response = await cloudApi.stopAccount(accountId)
      if (response.data.success) {
        set({ isSaving: false })
        await get().fetchWorkerStatuses()
        await get().fetchStatus()
        return true
      }
      set({ error: response.data.error || 'Failed', isSaving: false })
      return false
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isSaving: false })
      return false
    }
  },

  fetchStatus: async () => {
    try {
      const response = await cloudApi.getStatus()
      set({ status: response.data })
    } catch {
      // ignore
    }
  },

  fetchWorkerStatuses: async () => {
    try {
      const response = await cloudApi.getWorkerStatuses()
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
        await cloudApi.clearAccountErrors(accountId)
      }
      await get().fetchWorkerStatuses()
    } catch {
      // ignore
    }
  },

  fetchLogs: async (lines = 100) => {
    set({ isLoadingLogs: true })
    try {
      const response = await cloudApi.getLogs(lines)
      set({ logs: response.data.logs, isLoadingLogs: false })
    } catch {
      set({ isLoadingLogs: false })
    }
  },

  clearLogs: async () => {
    try {
      await cloudApi.clearLogs()
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

    const es = new EventSource(cloudApi.getLogsStreamUrl(), { withCredentials: true })

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
        logs: [...state.logs, event.data].slice(-1000),
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
      const response = await cloudApi.getCaughtIps()
      set({ caughtIps: response.data, isLoadingCaughtIps: false })
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isLoadingCaughtIps: false })
    }
  },

  clearCaughtIps: async () => {
    set({ isSaving: true, error: null })
    try {
      await cloudApi.clearAllCaughtIps()
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
      await cloudApi.deleteCaughtIp(ipId)
      set(state => ({
        caughtIps: state.caughtIps.filter(ip => ip.id !== ipId),
        isSaving: false,
      }))
      await get().fetchAccounts()
      return true
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isSaving: false })
      return false
    }
  },

  clearAccountCaughtIps: async (accountId) => {
    set({ isSaving: true, error: null })
    try {
      await cloudApi.clearAccountCaughtIps(accountId)
      set(state => ({
        caughtIps: state.caughtIps.filter(ip => ip.account_id !== accountId),
        isSaving: false,
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
      const response = await cloudApi.manualObtainIp(data)
      const result = response.data
      const entry: CloudManualObtainHistoryEntry = {
        id: Date.now(),
        timestamp: new Date().toLocaleString('ru-RU'),
        accountLabel,
        zone,
        success: result.success,
        ipAddress: result.ip_address,
        eipId: result.eip_id,
        error: result.error,
      }
      set(state => ({
        manualObtainHistory: [entry, ...state.manualObtainHistory],
        isObtaining: false,
      }))
      return result
    } catch (error: any) {
      const errMsg = error.response?.data?.detail || error.response?.data?.error || 'Ошибка запроса'
      const entry: CloudManualObtainHistoryEntry = {
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

export type CloudStoreHook = ReturnType<typeof createCloudStore>

export const useCloudEvoStore = createCloudStore(cloudEvoApi)
export const useCloudAdvStore = createCloudStore(cloudAdvApi)
