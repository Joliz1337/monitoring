/** Поиск IP: клиент API VK Cloud */

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

export interface ExtCloudServerIp {
  address: string
  kind: 'floating' | 'direct'
}

export interface ExtCloudServer {
  id: string
  name: string
  status: string
  ips: ExtCloudServerIp[]
}

/** Флоатинг, которого нет в адресах ВМ: свободный или на порту без ВМ */
export interface ExtCloudFloatingIp {
  address: string
  status: string
  bound: boolean
  fixed_ip: string | null
  attached_to: string | null
}

export interface ExtProjectCloudIps {
  project_db_id: number
  project_id: string
  project_name: string
  disabled: boolean
  error: string | null
  servers: ExtCloudServer[]
  floating_ips: ExtCloudFloatingIp[]
}

export interface ExtAccountCloudIps {
  account_id: number
  checked_at: string
  projects: ExtProjectCloudIps[]
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
  subnet_mode: 'per_account' | 'rotate'
  available_subnets: Record<number, string>
  precise_cidrs: string[]
}

export interface ExtPreciseCidr {
  id: number
  cidr: string
  created_at: string
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
  status: 'running' | 'stopped' | 'error' | 'stopping' | 'proxy_waiting'
  error_type?: string | null
  error_message?: string | null
  started_at?: string | null
  attempts: number
  caught: number
  released?: number
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

export interface ManualObtainRequest {
  account_id: number
  project_db_id: number
  subnet_key: number
  custom_subnet_name?: string
}

export interface ManualObtainResult {
  success: boolean
  ip_address?: string
  error?: string
  subnet_name: string
}

// Статистика выданных облаком адресов — общий формат у всех провайдеров.
// zone — зона Cloud.ru и Yandex Cloud, регион Selectel, внешняя подсеть VK
interface ReceivedStatsCounters {
  received: number
  unique_ips: number
  caught: number
}

export interface ReceivedZoneStats extends ReceivedStatsCounters {
  zone: string
  first_seen: string
  last_seen: string
}

export interface ReceivedSubnetStats extends ReceivedStatsCounters {
  subnet: string
  last_seen: string
  in_whitelist: boolean
  zones: Record<string, number>
}

export interface ReceivedStats {
  totals: ReceivedStatsCounters & {
    subnets: number
    first_seen: string | null
    last_seen: string | null
  }
  zones: ReceivedZoneStats[]
  subnets: ReceivedSubnetStats[]
}

export interface ReceivedIP {
  ip_address: string
  zone: string
  received: number
  caught: boolean
  first_seen: string
  last_seen: string
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

  // Адреса на ВМ и флоатинги аккаунта — живой опрос VK по всем проектам
  getAccountCloudIps: (accountId: number) =>
    api.get<ExtAccountCloudIps>(`/ext/accounts/${accountId}/cloud-ips`),

  // Статистика выданных облаком адресов (accountId не задан — все аккаунты)
  getReceivedStats: (accountId?: number) =>
    api.get<ReceivedStats>('/ext/received-stats', { params: { account_id: accountId } }),
  getReceivedSubnetIps: (subnet: string, accountId?: number) =>
    api.get<ReceivedIP[]>('/ext/received-ips', { params: { subnet, account_id: accountId } }),
  clearReceivedIps: (accountId?: number) =>
    api.delete<{ success: boolean; deleted: number }>('/ext/received-ips', { params: { account_id: accountId } }),

  // Settings
  getSettings: () => api.get<ExtSettings>('/ext/settings'),
  updateSettings: (data: Partial<ExtSettings>) => api.put<ExtSettings>('/ext/settings', data),
  getSubnets: () => api.get<{ subnets: Record<number, string>; builtin_ids: number[]; subnet_names: Record<number, string> }>('/ext/subnets'),
  addSubnet: (id: number, cidr: string) => api.post('/ext/subnets', { id, cidr }),
  removeSubnet: (id: number) => api.delete(`/ext/subnets/${id}`),

  // Precise CIDR filter (узкий целевой диапазон)
  getPreciseCidrs: () => api.get<ExtPreciseCidr[]>('/ext/precise-cidrs'),
  addPreciseCidr: (cidr: string) => api.post<{ success: boolean; id?: number; cidr?: string; error?: string }>('/ext/precise-cidrs', { cidr }),
  removePreciseCidr: (id: number) => api.delete(`/ext/precise-cidrs/${id}`),
  
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

  // Manual IP Obtainment
  manualObtainIp: (data: ManualObtainRequest) =>
    api.post<ManualObtainResult>('/ext/manual-obtain', data),

  // Multiqueue
  enableMultiqueue: (data: {
    auth_url: string; project_id: string; image_id: string;
    auth_type: string; username?: string; user_domain_name?: string;
    password?: string; api_token?: string; proxy?: string;
  }) => api.post<{ success: boolean; error?: string; image_name?: string; image_id?: string; multiqueue?: string }>('/ext/multiqueue-enable', data),
}

export default extApi
