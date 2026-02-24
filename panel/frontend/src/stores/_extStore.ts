/**
 * Extension Store
 */

import { create } from 'zustand'
import { extApi, ExtAccount, ExtProject, ExtSettings, ExtStatus, ExtCaughtIP, AccountWorkerStatus, AccountError } from '../api/_ext'

interface NavItem {
  path: string
  icon: string
  label: string
}

interface ExtState {
  enabled: boolean
  navItem: NavItem | null
  accounts: ExtAccount[]
  projects: Record<number, ExtProject[]>
  caughtIps: ExtCaughtIP[]
  settings: ExtSettings | null
  status: ExtStatus
  logs: string[]
  workerStatuses: Record<number, AccountWorkerStatus>
  accountErrors: AccountError[]
  
  isLoadingAccounts: boolean
  isLoadingSettings: boolean
  isLoadingLogs: boolean
  isLoadingCaughtIps: boolean
  isSaving: boolean
  error: string | null
  logEventSource: EventSource | null
  
  // Actions
  initExt: () => Promise<void>
  fetchAccounts: () => Promise<void>
  createAccount: (data: { email: string; password: string; proxy?: string }) => Promise<ExtAccount | null>
  updateAccount: (id: number, data: { enabled?: boolean; password?: string; proxy?: string }) => Promise<ExtAccount | null>
  deleteAccount: (id: number) => Promise<boolean>
  
  fetchProjects: (accountId: number) => Promise<void>
  createProject: (accountId: number, data: {
    project_id: string
    project_name: string
  }) => Promise<ExtProject | null>
  updateProject: (projectId: number, data: Partial<ExtProject>) => Promise<ExtProject | null>
  deleteProject: (projectId: number, accountId: number) => Promise<boolean>
  
  fetchSettings: () => Promise<void>
  updateSettings: (data: Partial<ExtSettings>) => Promise<boolean>
  
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
  
  // Caught IPs
  fetchCaughtIps: (accountId?: number, projectId?: number) => Promise<void>
  clearCaughtIps: (accountId?: number, projectId?: number) => Promise<boolean>
  deleteCaughtIp: (ipId: number) => Promise<boolean>
  
  clearError: () => void
}

export const useExtStore = create<ExtState>((set, get) => ({
  enabled: false,
  navItem: null,
  accounts: [],
  projects: {},
  caughtIps: [],
  settings: null,
  status: { status: 'stopped', pid: null, running: false },
  logs: [],
  workerStatuses: {},
  accountErrors: [],
  
  isLoadingAccounts: false,
  isLoadingSettings: false,
  isLoadingLogs: false,
  isLoadingCaughtIps: false,
  isSaving: false,
  error: null,
  logEventSource: null,
  
  initExt: async () => {
    if (get().enabled) return
    try {
      await extApi.getStatus()
      set({ enabled: true, navItem: { path: 'ip-search', icon: 'Search', label: 'IP Search' } })
    } catch {
      set({ enabled: false, navItem: null })
    }
  },
  
  fetchAccounts: async () => {
    set({ isLoadingAccounts: true, error: null })
    try {
      const response = await extApi.getAccounts()
      set({
        accounts: response.data,
        isLoadingAccounts: false,
        enabled: true,
        navItem: { path: 'ip-search', icon: 'Search', label: 'IP Search' },
      })
    } catch (error: any) {
      const status = error.response?.status
      if (status === 404 || status === 503) {
        set({ enabled: false, navItem: null, isLoadingAccounts: false })
        return
      }
      set({ error: error.response?.data?.detail || 'Failed', isLoadingAccounts: false })
    }
  },
  
  createAccount: async (data: { email: string; password: string; proxy?: string }) => {
    set({ isSaving: true, error: null })
    try {
      const response = await extApi.createAccount(data)
      set(state => ({ accounts: [...state.accounts, response.data], isSaving: false }))
      return response.data
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isSaving: false })
      return null
    }
  },
  
  updateAccount: async (id: number, data: { enabled?: boolean; password?: string; proxy?: string }) => {
    set({ isSaving: true, error: null })
    try {
      const response = await extApi.updateAccount(id, data)
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
  
  deleteAccount: async (id: number) => {
    set({ isSaving: true, error: null })
    try {
      await extApi.deleteAccount(id)
      set(state => {
        const { [id]: _, ...restProjects } = state.projects
        return {
          accounts: state.accounts.filter(a => a.id !== id),
          projects: restProjects,
          isSaving: false
        }
      })
      return true
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isSaving: false })
      return false
    }
  },
  
  fetchProjects: async (accountId: number) => {
    try {
      const response = await extApi.getProjects(accountId)
      set(state => ({ projects: { ...state.projects, [accountId]: response.data } }))
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed' })
    }
  },
  
  createProject: async (accountId: number, data: { project_id: string; project_name: string }) => {
    set({ isSaving: true, error: null })
    try {
      const response = await extApi.createProject(accountId, data)
      set(state => {
        const accountProjects = state.projects[accountId] || []
        return {
          projects: { ...state.projects, [accountId]: [...accountProjects, response.data] },
          accounts: state.accounts.map(a =>
            a.id === accountId ? { ...a, project_count: a.project_count + 1 } : a
          ),
          isSaving: false
        }
      })
      return response.data
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isSaving: false })
      return null
    }
  },
  
  updateProject: async (projectId, data) => {
    set({ isSaving: true, error: null })
    try {
      const response = await extApi.updateProject(projectId, data)
      set(state => {
        const accountId = response.data.account_id
        const accountProjects = state.projects[accountId] || []
        return {
          projects: {
            ...state.projects,
            [accountId]: accountProjects.map(p => p.id === projectId ? response.data : p)
          },
          isSaving: false
        }
      })
      return response.data
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isSaving: false })
      return null
    }
  },
  
  deleteProject: async (projectId, accountId) => {
    set({ isSaving: true, error: null })
    try {
      await extApi.deleteProject(projectId)
      set(state => {
        const accountProjects = state.projects[accountId] || []
        return {
          projects: {
            ...state.projects,
            [accountId]: accountProjects.filter(p => p.id !== projectId)
          },
          accounts: state.accounts.map(a =>
            a.id === accountId ? { ...a, project_count: Math.max(0, a.project_count - 1) } : a
          ),
          isSaving: false
        }
      })
      return true
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isSaving: false })
      return false
    }
  },
  
  fetchSettings: async () => {
    set({ isLoadingSettings: true, error: null })
    try {
      const response = await extApi.getSettings()
      set({ settings: response.data, isLoadingSettings: false })
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isLoadingSettings: false })
    }
  },
  
  updateSettings: async (data) => {
    set({ isSaving: true, error: null })
    try {
      const response = await extApi.updateSettings(data)
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
      const response = await extApi.start()
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
      const response = await extApi.stop()
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
  
  startAccount: async (accountId: number) => {
    set({ isSaving: true, error: null })
    try {
      const response = await extApi.startAccount(accountId)
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
  
  stopAccount: async (accountId: number) => {
    set({ isSaving: true, error: null })
    try {
      const response = await extApi.stopAccount(accountId)
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
  
  fetchWorkerStatuses: async () => {
    try {
      const response = await extApi.getWorkerStatuses()
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
        await extApi.clearAccountErrors(accountId)
      }
      await get().fetchWorkerStatuses()
    } catch {
      // ignore
    }
  },
  
  fetchStatus: async () => {
    try {
      const response = await extApi.getStatus()
      set({ status: response.data })
    } catch {
      // ignore
    }
  },
  
  fetchLogs: async (lines = 100) => {
    set({ isLoadingLogs: true })
    try {
      const response = await extApi.getLogs(lines)
      set({ logs: response.data.logs, isLoadingLogs: false })
    } catch {
      set({ isLoadingLogs: false })
    }
  },
  
  clearLogs: async () => {
    try {
      await extApi.clearLogs()
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
    
    const es = new EventSource(extApi.getLogsStreamUrl(), { withCredentials: true })
    
    // Server sends all existing logs, then 'sync' event, then new logs
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
  
  // Caught IPs
  fetchCaughtIps: async (accountId?: number, projectId?: number) => {
    set({ isLoadingCaughtIps: true })
    try {
      let response
      if (projectId !== undefined) {
        response = await extApi.getProjectCaughtIps(projectId)
      } else if (accountId !== undefined) {
        response = await extApi.getAccountCaughtIps(accountId)
      } else {
        response = await extApi.getCaughtIps()
      }
      set({ caughtIps: response.data, isLoadingCaughtIps: false })
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isLoadingCaughtIps: false })
    }
  },
  
  clearCaughtIps: async (accountId?: number, projectId?: number) => {
    set({ isSaving: true, error: null })
    try {
      if (projectId !== undefined) {
        await extApi.clearProjectCaughtIps(projectId)
      } else if (accountId !== undefined) {
        await extApi.clearAccountCaughtIps(accountId)
      } else {
        await extApi.clearAllCaughtIps()
      }
      // Refresh accounts to update counts
      await get().fetchAccounts()
      set({ caughtIps: [], isSaving: false })
      return true
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isSaving: false })
      return false
    }
  },
  
  deleteCaughtIp: async (ipId: number) => {
    set({ isSaving: true, error: null })
    try {
      await extApi.deleteCaughtIp(ipId)
      set(state => ({
        caughtIps: state.caughtIps.filter(ip => ip.id !== ipId),
        isSaving: false
      }))
      // Refresh accounts to update counts
      await get().fetchAccounts()
      return true
    } catch (error: any) {
      set({ error: error.response?.data?.detail || 'Failed', isSaving: false })
      return false
    }
  },
  
  clearError: () => set({ error: null }),
}))

export default useExtStore
