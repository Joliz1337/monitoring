import { create } from 'zustand'
import { authApi } from '../api/client'

interface AuthState {
  isAuthenticated: boolean
  isLoading: boolean
  
  checkAuth: () => Promise<boolean>
  login: (password: string) => Promise<{ success: boolean; error?: string }>
  logout: () => Promise<void>
  validateUid: (uid: string) => Promise<boolean>
}

export const useAuthStore = create<AuthState>((set) => ({
  isAuthenticated: false,
  isLoading: true,
  
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
  
  validateUid: async (uid: string) => {
    try {
      const { data } = await authApi.validateUid(uid)
      return data.valid
    } catch {
      // Connection dropped or error = invalid UID
      return false
    }
  },
}))
