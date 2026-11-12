/**
 * Selectel API client
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

export interface SelectelAccount {
  id: number
  label: string
  username: string
  password_masked: string
  domain: string
  project_id: string
  proxy: string | null
  enabled: boolean
  created_at: string
  caught_ip_count: number
}

export interface SelectelCaughtIP {
  id: number
  account_id: number
  ip_address: string
  fip_id: string
  region: string
  caught_at: string
  account_label: string
}

export interface SelectelWhitelistEntry {
  id: number
  cidr: string
  created_at?: string | null
}

export interface SelectelSettings {
  delay_burst: number
  delay_round: number
  delay_delete: number
  jitter: number
  provision_timeout: number
  provision_poll: number
  regions: string[]
  telegram_bot_token: string
  telegram_chat_id: string
  proxy_required: boolean
  log_level: 'info' | 'full'
  available_regions: string[]
  whitelist_cidrs: string[]
}

export interface SelectelStatus {
  status: 'stopped' | 'running' | 'starting' | 'stopping'
  pid: number | null
  running: boolean
  active_count?: number
  total_workers?: number
}

export interface SelectelWorkerStatus {
  account_id: number
  label: string
  status: 'running' | 'stopped' | 'error' | 'stopping' | 'proxy_waiting'
  error_type?: string | null
  error_message?: string | null
  started_at?: string | null
  attempts: number
  caught: number
  current_round?: number | null
  pid?: number | null
}

export interface SelectelAccountError {
  id: number
  account_id: number
  error_type: string
  error_message: string
  created_at: string
  resolved: boolean
}

export interface SelectelWorkerStatuses {
  workers: Record<number, SelectelWorkerStatus>
  errors: SelectelAccountError[]
}

export interface SelectelManualObtainRequest {
  account_id: number
  region: string
}

export interface SelectelManualObtainResult {
  success: boolean
  ip_address?: string
  fip_id?: string
  region?: string
  error?: string
}

export interface SelectelMultiqueueRequest {
  auth_url: string
  project_id: string
  image_id: string
  auth_type?: 'password' | 'v3token'
  username?: string
  user_domain_name?: string
  password?: string
  api_token?: string
  region?: string
  proxy?: string
}

export interface SelectelMultiqueueResult {
  success: boolean
  error?: string
  image_name?: string
  image_id?: string
  multiqueue?: string
}

export const selectelApi = {
  // Accounts
  getAccounts: () => api.get<SelectelAccount[]>('/selectel/accounts'),
  createAccount: (data: { username: string; password: string; domain: string; project_id: string; proxy?: string; label?: string }) =>
    api.post<SelectelAccount>('/selectel/accounts', data),
  updateAccount: (id: number, data: { enabled?: boolean; username?: string; password?: string; domain?: string; project_id?: string; proxy?: string; label?: string }) =>
    api.put<SelectelAccount>(`/selectel/accounts/${id}`, data),
  deleteAccount: (id: number) => api.delete(`/selectel/accounts/${id}`),

  // Caught IPs
  getCaughtIps: () => api.get<SelectelCaughtIP[]>('/selectel/caught-ips'),
  clearAllCaughtIps: () => api.delete<{ success: boolean; deleted: number }>('/selectel/caught-ips'),
  deleteCaughtIp: (ipId: number) => api.delete(`/selectel/caught-ips/${ipId}`),
  clearAccountCaughtIps: (accountId: number) => api.delete(`/selectel/accounts/${accountId}/caught-ips`),

  // Статистика выданных облаком адресов (accountId не задан — все аккаунты)
  getReceivedStats: (accountId?: number) =>
    api.get<ReceivedStats>('/selectel/received-stats', { params: { account_id: accountId } }),
  getReceivedSubnetIps: (subnet: string, accountId?: number) =>
    api.get<ReceivedIP[]>('/selectel/received-ips', { params: { subnet, account_id: accountId } }),
  clearReceivedIps: (accountId?: number) =>
    api.delete<{ success: boolean; deleted: number }>('/selectel/received-ips', { params: { account_id: accountId } }),

  // Settings
  getSettings: () => api.get<SelectelSettings>('/selectel/settings'),
  updateSettings: (data: Partial<SelectelSettings>) => api.put<SelectelSettings>('/selectel/settings', data),

  // Whitelist CIDR (одиночный IP тоже — хранится как /32)
  getWhitelist: () => api.get<SelectelWhitelistEntry[]>('/selectel/whitelist'),
  addWhitelistCidr: (cidr: string) => api.post<{ success: boolean; id?: number; cidr?: string; error?: string }>('/selectel/whitelist', { cidr }),
  addWhitelistCidrs: (cidrs: string[]) => api.post<{ success: boolean; added: number; skipped: number; invalid: string[] }>('/selectel/whitelist/bulk', { cidrs }),
  removeWhitelistCidr: (id: number) => api.delete(`/selectel/whitelist/${id}`),
  clearWhitelist: () => api.delete<{ success: boolean; deleted: number }>('/selectel/whitelist'),

  // Control (all accounts)
  start: () => api.post<{ success: boolean; pid?: number; error?: string }>('/selectel/start'),
  stop: () => api.post<{ success: boolean; error?: string }>('/selectel/stop'),
  getStatus: () => api.get<SelectelStatus>('/selectel/status'),

  // Per-account control
  startAccount: (accountId: number) => api.post<{ success: boolean; pid?: number; error?: string }>(`/selectel/accounts/${accountId}/start`),
  stopAccount: (accountId: number) => api.post<{ success: boolean; error?: string }>(`/selectel/accounts/${accountId}/stop`),
  getWorkerStatuses: () => api.get<SelectelWorkerStatuses>('/selectel/worker/statuses'),
  getAccountErrors: (accountId: number) => api.get<SelectelAccountError[]>(`/selectel/accounts/${accountId}/errors`),
  clearAccountErrors: (accountId: number) => api.delete(`/selectel/accounts/${accountId}/errors`),

  // Logs
  getLogs: (lines: number = 100) => api.get<{ logs: string[] }>('/selectel/logs', { params: { lines } }),
  clearLogs: () => api.delete('/selectel/logs'),
  getLogsStreamUrl: () => '/api/_int/selectel/logs/stream',

  // Manual IP
  manualObtainIp: (data: SelectelManualObtainRequest) =>
    api.post<SelectelManualObtainResult>('/selectel/manual-obtain', data),

  // Multiqueue
  enableMultiqueue: (data: SelectelMultiqueueRequest) =>
    api.post<SelectelMultiqueueResult>('/selectel/multiqueue-enable', data),
}

export default selectelApi
