import { create } from 'zustand'
import { authApi } from '../api/client'

interface AuthState {
  isAuthenticated: boolean
  isLoading: boolean
  panelUid: string | null
  
  checkAuth: () => Promise<boolean>
  login: (password: string) => Promise<{ success: boolean; error?: string }>
  logout: () => Promise<void>
  fetchUid: () => Promise<string | null>
}

export const useAuthStore = create<AuthState>((set) => ({
  isAuthenticated: false,
  isLoading: true,
  panelUid: null,
  
  checkAuth: async () => {
    try {
      await authApi.check()
      set({ isAuthenticated: true, isLoading: false })
      return true
    } catch {
      set({ isAuthenticated: false, isLoading: false })
      return false
    }
  },
  
  login: async (password: string) => {
    try {
      await authApi.login(password)
      set({ isAuthenticated: true })
      return { success: true }
    } catch (error: unknown) {
      const err = error as { response?: { data?: { detail?: string }; status?: number } }
      const detail = err.response?.data?.detail || 'Login failed'
      return { success: false, error: detail }
    }
  },
  
  logout: async () => {
    try {
      await authApi.logout()
    } finally {
      set({ isAuthenticated: false })
    }
  },
  
  fetchUid: async () => {
    // Get UID from build-time env variable (secure - not exposed via API)
    const envUid = import.meta.env.VITE_PANEL_UID
    if (envUid) {
      set({ panelUid: envUid })
      return envUid
    }
    // Fallback for development only
    try {
      const { data } = await authApi.getUid()
      set({ panelUid: data.uid })
      return data.uid
    } catch {
      return null
    }
  },
}))
