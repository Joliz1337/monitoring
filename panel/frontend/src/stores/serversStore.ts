import { create } from 'zustand'
import { toast } from 'sonner'
import { serversApi, proxyApi, Server, ServerMetrics } from '../api/client'

interface ServerTraffic {
  rx_bytes: number
  tx_bytes: number
  days: number
}

interface ServerWithMetrics extends Server {
  metrics?: ServerMetrics | null
  traffic?: ServerTraffic | null
  status: 'online' | 'offline' | 'loading' | 'error'
  lastUpdated?: Date
  // These come from backend now
  last_seen?: string | null
  last_error?: string | null
  error_code?: number | null
}

interface ServersState {
  servers: ServerWithMetrics[]
  isLoading: boolean
  error: string | null
  
  fetchServers: () => Promise<void>
  fetchServersWithMetrics: () => Promise<void>
  fetchServerMetrics: (serverId: number) => Promise<void>
  fetchServerLiveMetrics: (serverId: number) => Promise<void>
  fetchServerTraffic: (serverId: number, days?: number) => Promise<void>
  fetchAllMetrics: () => Promise<void>
  fetchAllLiveMetrics: () => Promise<void>
  fetchAllTraffic: (days?: number) => Promise<void>
  addServer: (data: { name: string; url: string; api_key: string }) => Promise<{ success: boolean; error?: string }>
  updateServer: (id: number, data: Partial<Server>) => Promise<void>
  toggleServer: (id: number, isActive: boolean) => Promise<void>
  deleteServer: (id: number) => Promise<void>
  reorderServers: (serverIds: number[]) => Promise<void>
  testServer: (id: number) => Promise<{ success: boolean; status: string; message?: string }>
  moveToFolder: (serverIds: number[], folder: string | null) => Promise<void>
  renameFolder: (oldName: string, newName: string) => Promise<void>
  deleteFolder: (folderName: string) => Promise<void>
}

export const useServersStore = create<ServersState>((set, get) => ({
  servers: [],
  isLoading: false,
  error: null,
  
  fetchServers: async () => {
    const { servers: existingServers } = get()
    set({ isLoading: true, error: null })
    try {
      const { data } = await serversApi.list()
      const serversWithStatus = data.servers.map(s => {
        const existing = existingServers.find(es => es.id === s.id)
        if (existing && existing.metrics) {
          return {
            ...s,
            metrics: existing.metrics,
            traffic: existing.traffic,
            status: existing.status,
            lastUpdated: existing.lastUpdated,
            last_seen: existing.last_seen,
            last_error: existing.last_error,
            error_code: existing.error_code,
          }
        }
        return {
          ...s,
          status: 'loading' as const,
        }
      })
      set({ servers: serversWithStatus, isLoading: false })
    } catch (error: unknown) {
      const err = error as { message?: string }
      set({ error: err.message || 'Failed to fetch servers', isLoading: false })
    }
  },
  
  fetchServersWithMetrics: async () => {
    set({ isLoading: true, error: null })
    try {
      const { data } = await serversApi.list(true)
      const serversWithStatus = data.servers.map(s => ({
        ...s,
        // Traffic data now comes from server response
        traffic: (s as { traffic?: ServerTraffic }).traffic || null,
        // Status priority: backend status > error check > metrics check > loading
        // If last_error exists, server is offline even if we have cached metrics
        status: (s.status || (s.last_error ? 'offline' : (s.metrics ? 'online' : 'loading'))) as 'online' | 'offline' | 'loading' | 'error',
        lastUpdated: new Date(),
      }))
      set({ servers: serversWithStatus, isLoading: false })
    } catch (error: unknown) {
      const err = error as { message?: string }
      set({ error: err.message || 'Failed to fetch servers', isLoading: false })
    }
  },
  
  fetchServerMetrics: async (serverId: number) => {
    const { servers } = get()
    const serverIndex = servers.findIndex(s => s.id === serverId)
    if (serverIndex === -1) return
    
    try {
      const { data } = await proxyApi.getMetrics(serverId)
      set({
        servers: servers.map(s => 
          s.id === serverId 
            ? { 
                ...s, 
                metrics: data, 
                status: 'online' as const, 
                lastUpdated: new Date(),
                last_error: null,
                error_code: null
              }
            : s
        ),
      })
    } catch (err: unknown) {
      const error = err as { response?: { status: number; data?: { detail?: string } } }
      const errorCode = error.response?.status || 500
      let errorMessage = error.response?.data?.detail || 'Connection failed'
      
      // Translate common errors
      if (errorCode === 504) errorMessage = 'Connection timeout'
      else if (errorCode === 502) errorMessage = 'Connection refused'
      
      set({
        servers: servers.map(s => 
          s.id === serverId 
            ? { 
                ...s, 
                metrics: null, 
                status: 'offline' as const, 
                lastUpdated: new Date(),
                last_error: errorMessage,
                error_code: errorCode
              }
            : s
        ),
      })
    }
  },
  
  fetchAllMetrics: async () => {
    const { servers, fetchServerMetrics } = get()
    await Promise.all(servers.map(s => fetchServerMetrics(s.id)))
  },
  
  fetchServerLiveMetrics: async (serverId: number) => {
    const { servers } = get()
    const serverIndex = servers.findIndex(s => s.id === serverId)
    if (serverIndex === -1) return
    
    try {
      const { data } = await proxyApi.getLiveMetrics(serverId)
      set({
        servers: servers.map(s => 
          s.id === serverId 
            ? { 
                ...s, 
                metrics: data, 
                status: 'online' as const, 
                lastUpdated: new Date(),
                last_error: null,
                error_code: null
              }
            : s
        ),
      })
    } catch (err: unknown) {
      const error = err as { response?: { status: number; data?: { detail?: string } } }
      const errorCode = error.response?.status || 500
      let errorMessage = error.response?.data?.detail || 'Connection failed'
      
      if (errorCode === 504) errorMessage = 'Connection timeout'
      else if (errorCode === 502) errorMessage = 'Connection refused'
      
      set({
        servers: servers.map(s => 
          s.id === serverId 
            ? { 
                ...s, 
                metrics: null, 
                status: 'offline' as const, 
                lastUpdated: new Date(),
                last_error: errorMessage,
                error_code: errorCode
              }
            : s
        ),
      })
    }
  },
  
  fetchAllLiveMetrics: async () => {
    const { servers, fetchServerLiveMetrics } = get()
    await Promise.all(servers.map(s => fetchServerLiveMetrics(s.id)))
  },
  
  fetchServerTraffic: async (serverId: number, days: number = 30) => {
    const { servers } = get()
    const serverIndex = servers.findIndex(s => s.id === serverId)
    if (serverIndex === -1) return
    
    try {
      const { data } = await proxyApi.getTrafficSummary(serverId, days)
      set({
        servers: servers.map(s => 
          s.id === serverId 
            ? { 
                ...s, 
                traffic: {
                  rx_bytes: data.total.rx_bytes,
                  tx_bytes: data.total.tx_bytes,
                  days: data.days
                }
              }
            : s
        ),
      })
    } catch {
      // Traffic data not available - ignore silently
      set({
        servers: servers.map(s => 
          s.id === serverId 
            ? { ...s, traffic: null }
            : s
        ),
      })
    }
  },
  
  fetchAllTraffic: async (days: number = 30) => {
    const { servers, fetchServerTraffic } = get()
    await Promise.all(servers.map(s => fetchServerTraffic(s.id, days)))
  },
  
  addServer: async (data) => {
    try {
      const { data: result } = await serversApi.create(data)
      if (result.success) {
        await get().fetchServers()
        return { success: true }
      }
      return { success: false, error: 'Failed to add server' }
    } catch (error: unknown) {
      const err = error as { response?: { data?: { detail?: string } } }
      return { success: false, error: err.response?.data?.detail || 'Failed to add server' }
    }
  },
  
  updateServer: async (id, data) => {
    await serversApi.update(id, data)
    await get().fetchServers()
  },
  
  toggleServer: async (id, isActive) => {
    set({
      servers: get().servers.map(s => 
        s.id === id ? { ...s, is_active: isActive } : s
      )
    })
    try {
      await serversApi.update(id, { is_active: isActive })
    } catch {
      toast.error('Failed to toggle monitoring')
      set({
        servers: get().servers.map(s => 
          s.id === id ? { ...s, is_active: !isActive } : s
        )
      })
    }
  },
  
  deleteServer: async (id) => {
    await serversApi.delete(id)
    set({ servers: get().servers.filter(s => s.id !== id) })
  },
  
  reorderServers: async (serverIds) => {
    const { servers } = get()
    const reordered = serverIds.map((id, index) => {
      const server = servers.find(s => s.id === id)!
      return { ...server, position: index }
    })
    set({ servers: reordered })
    await serversApi.reorder(serverIds)
  },
  
  testServer: async (id) => {
    const { data } = await serversApi.test(id)
    return data
  },

  moveToFolder: async (serverIds, folder) => {
    await serversApi.moveToFolder(serverIds, folder)
    set({
      servers: get().servers.map(s =>
        serverIds.includes(s.id) ? { ...s, folder } : s
      )
    })
  },

  renameFolder: async (oldName, newName) => {
    await serversApi.renameFolder(oldName, newName)
    set({
      servers: get().servers.map(s =>
        s.folder === oldName ? { ...s, folder: newName } : s
      )
    })
  },

  deleteFolder: async (folderName) => {
    await serversApi.deleteFolder(folderName)
    set({
      servers: get().servers.map(s =>
        s.folder === folderName ? { ...s, folder: null } : s
      )
    })
  },
}))
