/**
 * Yandex Cloud API client
 */

import axios from 'axios'
import type { ReceivedStats, ReceivedIP } from './ipSearchVk'

const api = axios.create({
  baseURL: '/api/_int',
  withCredentials: true,
  headers: { 'Content-Type': 'application/json' },
})

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      const uid = window.location.pathname.split('/')[1]
      if (uid && !window.location.pathname.includes('/login')) {
        window.location.href = `/${uid}/login`
      }
    }
    return Promise.reject(error)
  }
)

export interface YcAccount {
  id: number
  label: string
  folder_id: string
  oauth_token_masked: string
  proxy: string | null
  enabled: boolean
  created_at: string
  caught_ip_count: number
}

export interface YcCaughtIP {
  id: number
  account_id: number
  ip_address: string
  address_id: string
  zone: string
  caught_at: string
  account_label: string
}

export interface YcSettings {
  delay_min: number
  delay_max: number
  start_delay_max: number
  stop_on_catch: boolean
  telegram_bot_token: string
  telegram_chat_id: string
  target_subnets: number[]
  zones: string[]
  proxy_required: boolean
  proxy_check_interval: number
  log_level: 'info' | 'full'
  available_subnets: string[]
  available_zones: string[]
}

export interface YcStatus {
  status: 'stopped' | 'running' | 'starting' | 'stopping'
  pid: number | null
  running: boolean
  active_count?: number
  total_workers?: number
}

export interface YcWorkerStatus {
  account_id: number
  label: string
  status: 'running' | 'stopped' | 'error' | 'stopping' | 'proxy_waiting'
  error_type?: string | null
  error_message?: string | null
  started_at?: string | null
  attempts: number
  caught: number
  pid?: number | null
}

export interface YcAccountError {
  id: number
  account_id: number
  error_type: string
  error_message: string
  created_at: string
  resolved: boolean
}

export interface YcWorkerStatuses {
  workers: Record<number, YcWorkerStatus>
  errors: YcAccountError[]
}

export interface YcManualObtainRequest {
  account_id: number
  zone: string
}

export interface YcManualObtainResult {
  success: boolean
  ip_address?: string
  address_id?: string
  zone?: string
  error?: string
}

export const ycApi = {
  // Accounts
  getAccounts: () => api.get<YcAccount[]>('/yc/accounts'),
  createAccount: (data: { oauth_token: string; folder_id: string; proxy?: string; label?: string }) =>
    api.post<YcAccount>('/yc/accounts', data),
  updateAccount: (id: number, data: { enabled?: boolean; oauth_token?: string; proxy?: string; label?: string }) =>
    api.put<YcAccount>(`/yc/accounts/${id}`, data),
  deleteAccount: (id: number) => api.delete(`/yc/accounts/${id}`),

  // Caught IPs
  getCaughtIps: () => api.get<YcCaughtIP[]>('/yc/caught-ips'),
  clearAllCaughtIps: () => api.delete<{ success: boolean; deleted: number }>('/yc/caught-ips'),
  deleteCaughtIp: (ipId: number) => api.delete(`/yc/caught-ips/${ipId}`),

  // Статистика выданных облаком адресов (accountId не задан — все аккаунты)
  getReceivedStats: (accountId?: number) =>
    api.get<ReceivedStats>('/yc/received-stats', { params: { account_id: accountId } }),
  getReceivedSubnetIps: (subnet: string, accountId?: number) =>
    api.get<ReceivedIP[]>('/yc/received-ips', { params: { subnet, account_id: accountId } }),
  clearReceivedIps: (accountId?: number) =>
    api.delete<{ success: boolean; deleted: number }>('/yc/received-ips', { params: { account_id: accountId } }),

  // Settings
  getSettings: () => api.get<YcSettings>('/yc/settings'),
  updateSettings: (data: Partial<YcSettings>) => api.put<YcSettings>('/yc/settings', data),

  // Subnets (CIDR strings)
  getSubnets: () => api.get<{ subnets: string[]; builtin: string[] }>('/yc/subnets'),
  addSubnet: (cidr: string) => api.post('/yc/subnets', { cidr }),
  removeSubnet: (cidr: string) => api.delete('/yc/subnets', { data: { cidr } }),

  // Control (all accounts)
  start: () => api.post<{ success: boolean; pid?: number; error?: string }>('/yc/start'),
  stop: () => api.post<{ success: boolean; error?: string }>('/yc/stop'),
  getStatus: () => api.get<YcStatus>('/yc/status'),

  // Per-account control
  startAccount: (accountId: number) => api.post<{ success: boolean; pid?: number; error?: string }>(`/yc/accounts/${accountId}/start`),
  stopAccount: (accountId: number) => api.post<{ success: boolean; error?: string }>(`/yc/accounts/${accountId}/stop`),
  getWorkerStatuses: () => api.get<YcWorkerStatuses>('/yc/worker/statuses'),
  getAccountErrors: (accountId: number) => api.get<YcAccountError[]>(`/yc/accounts/${accountId}/errors`),
  clearAccountErrors: (accountId: number) => api.delete(`/yc/accounts/${accountId}/errors`),

  // Logs
  getLogs: (lines: number = 100) => api.get<{ logs: string[] }>('/yc/logs', { params: { lines } }),
  clearLogs: () => api.delete('/yc/logs'),
  getLogsStreamUrl: () => '/api/_int/yc/logs/stream',

  // Manual IP
  manualObtainIp: (data: YcManualObtainRequest) =>
    api.post<YcManualObtainResult>('/yc/manual-obtain', data),
}

export default ycApi
