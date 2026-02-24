/**
 * Extension API client
 */

import axios from 'axios'

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

export interface ExtAccount {
  id: number
  email: string
  password: string
  proxy: string | null
  enabled: boolean
  created_at: string
  project_count: number
  caught_ip_count: number
}

export interface ExtProject {
  id: number
  account_id: number
  project_id: string
  project_name: string
  disabled: boolean
  created_at: string
  caught_ip_count: number
}

export interface ExtCaughtIP {
  id: number
  account_id: number
  project_id: number | null
  ip_address: string
  subnet_name: string
  caught_at: string
  account_email: string
  project_name: string | null
}

export interface ExtSettings {
  delay_min: number
  delay_max: number
  start_delay_max: number
  max_ip_per_project: number
  stop_on_catch: boolean
  telegram_bot_token: string
  telegram_chat_id: string
  target_subnets: number[]
  proxy_required: boolean
  proxy_check_interval: number
  log_level: 'info' | 'full'
  available_subnets: Record<number, string>
}

export interface ExtStatus {
  status: 'stopped' | 'running' | 'starting' | 'stopping'
  pid: number | null
  running: boolean
  active_count?: number
  total_workers?: number
}

export interface AccountWorkerStatus {
  account_id: number
  email: string
  status: 'running' | 'stopped' | 'error' | 'stopping'
  error_type?: string | null
  error_message?: string | null
  started_at?: string | null
  attempts: number
  caught: number
  pid?: number | null
}

export interface AccountError {
  id: number
  account_id: number
  error_type: string
  error_message: string
  project_name?: string | null
  created_at: string
  resolved: boolean
}

export interface WorkerStatuses {
  workers: Record<number, AccountWorkerStatus>
  errors: AccountError[]
}

export const extApi = {
  // Accounts
  getAccounts: () => api.get<ExtAccount[]>('/ext/accounts'),
  createAccount: (data: { email: string; password: string; proxy?: string }) => 
    api.post<ExtAccount>('/ext/accounts', data),
  updateAccount: (id: number, data: { enabled?: boolean; password?: string; proxy?: string }) => 
    api.put<ExtAccount>(`/ext/accounts/${id}`, data),
  deleteAccount: (id: number) => api.delete(`/ext/accounts/${id}`),
  
  // Projects
  getProjects: (accountId: number) => api.get<ExtProject[]>(`/ext/accounts/${accountId}/projects`),
  getAllProjects: () => api.get<ExtProject[]>('/ext/projects'),
  createProject: (accountId: number, data: {
    project_id: string
    project_name: string
  }) => api.post<ExtProject>(`/ext/accounts/${accountId}/projects`, data),
  updateProject: (projectId: number, data: Partial<ExtProject>) => 
    api.put<ExtProject>(`/ext/projects/${projectId}`, data),
  deleteProject: (projectId: number) => api.delete(`/ext/projects/${projectId}`),
  
  // Caught IPs
  getCaughtIps: () => api.get<ExtCaughtIP[]>('/ext/caught-ips'),
  getAccountCaughtIps: (accountId: number) => api.get<ExtCaughtIP[]>(`/ext/accounts/${accountId}/caught-ips`),
  getProjectCaughtIps: (projectId: number) => api.get<ExtCaughtIP[]>(`/ext/projects/${projectId}/caught-ips`),
  clearAllCaughtIps: () => api.delete<{ success: boolean; deleted: number }>('/ext/caught-ips'),
  clearAccountCaughtIps: (accountId: number) => api.delete<{ success: boolean; deleted: number }>(`/ext/accounts/${accountId}/caught-ips`),
  clearProjectCaughtIps: (projectId: number) => api.delete<{ success: boolean; deleted: number }>(`/ext/projects/${projectId}/caught-ips`),
  deleteCaughtIp: (ipId: number) => api.delete(`/ext/caught-ips/${ipId}`),
  
  // Settings
  getSettings: () => api.get<ExtSettings>('/ext/settings'),
  updateSettings: (data: Partial<ExtSettings>) => api.put<ExtSettings>('/ext/settings', data),
  getSubnets: () => api.get<{ subnets: Record<number, string>; builtin_ids: number[] }>('/ext/subnets'),
  addSubnet: (id: number, cidr: string) => api.post('/ext/subnets', { id, cidr }),
  removeSubnet: (id: number) => api.delete(`/ext/subnets/${id}`),
  
  // Control (all accounts)
  start: () => api.post<{ success: boolean; pid?: number; error?: string; started?: number[]; errors?: string[] }>('/ext/start'),
  stop: () => api.post<{ success: boolean; error?: string; stopped?: number[] }>('/ext/stop'),
  getStatus: () => api.get<ExtStatus>('/ext/status'),
  
  // Per-account control
  startAccount: (accountId: number) => api.post<{ success: boolean; pid?: number; error?: string }>(`/ext/accounts/${accountId}/start`),
  stopAccount: (accountId: number) => api.post<{ success: boolean; error?: string }>(`/ext/accounts/${accountId}/stop`),
  getWorkerStatuses: () => api.get<WorkerStatuses>('/ext/worker/statuses'),
  getAccountErrors: (accountId: number) => api.get<AccountError[]>(`/ext/accounts/${accountId}/errors`),
  clearAccountErrors: (accountId: number) => api.delete(`/ext/accounts/${accountId}/errors`),
  
  // Logs
  getLogs: (lines: number = 100) => api.get<{ logs: string[] }>('/ext/logs', { params: { lines } }),
  clearLogs: () => api.delete('/ext/logs'),
  getLogsStreamUrl: () => '/api/_int/ext/logs/stream',
}

export default extApi
