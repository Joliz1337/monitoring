/**
 * Cloud.ru API client — Evolution и Advanced. Маршруты у редакций одинаковые,
 * отличается префикс; зоны есть только у Evolution (у Advanced один регион).
 * cloudBwApi — ширина канала публичных IP и общих полос Advanced по расписанию и по нагрузке нод.
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

export interface CloudAccount {
  id: number
  label: string
  key_id: string
  key_secret_masked: string
  key_token_masked?: string
  project_id: string
  proxy: string | null
  enabled: boolean
  created_at: string
  caught_ip_count: number
}

export interface CloudAccountInput {
  enabled?: boolean
  key_id?: string
  key_secret?: string
  key_token?: string
  project_id?: string
  proxy?: string
  label?: string
}

export interface CloudCaughtIP {
  id: number
  account_id: number
  ip_address: string
  eip_id: string
  zone: string
  caught_at: string
  account_label: string
}

export interface CloudWhitelistEntry {
  id: number
  cidr: string
  created_at?: string | null
}

export interface CloudSettings {
  delay_burst: number
  delay_round: number
  delay_delete: number
  jitter: number
  provision_timeout: number
  provision_poll: number
  zones?: string[]
  telegram_bot_token: string
  telegram_chat_id: string
  proxy_required: boolean
  log_level: 'info' | 'full'
  mode?: 'random' | 'targeted'
  available_zones?: string[]
  whitelist_cidrs: string[]
}

export interface CloudStatus {
  status: 'stopped' | 'running' | 'starting' | 'stopping'
  pid: number | null
  running: boolean
  active_count?: number
  total_workers?: number
}

export interface CloudWorkerStatus {
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

export interface CloudAccountError {
  id: number
  account_id: number
  error_type: string
  error_message: string
  created_at: string
  resolved: boolean
}

export interface CloudWorkerStatuses {
  workers: Record<number, CloudWorkerStatus>
  errors: CloudAccountError[]
}

export interface CloudManualObtainRequest {
  account_id: number
  zone?: string
  ip_address?: string
}

export interface CloudManualObtainResult {
  success: boolean
  ip_address?: string
  eip_id?: string
  zone?: string
  error?: string
  requested_mismatch?: string
}

function createCloudApi(prefix: string) {
  return {
    // Accounts
    getAccounts: () => api.get<CloudAccount[]>(`${prefix}/accounts`),
    createAccount: (data: CloudAccountInput) => api.post<CloudAccount>(`${prefix}/accounts`, data),
    updateAccount: (id: number, data: CloudAccountInput) => api.put<CloudAccount>(`${prefix}/accounts/${id}`, data),
    deleteAccount: (id: number) => api.delete(`${prefix}/accounts/${id}`),

    // Caught IPs
    getCaughtIps: () => api.get<CloudCaughtIP[]>(`${prefix}/caught-ips`),
    clearAllCaughtIps: () => api.delete<{ success: boolean; deleted: number }>(`${prefix}/caught-ips`),
    deleteCaughtIp: (ipId: number) => api.delete(`${prefix}/caught-ips/${ipId}`),
    clearAccountCaughtIps: (accountId: number) => api.delete(`${prefix}/accounts/${accountId}/caught-ips`),

    // Статистика выданных облаком адресов (accountId не задан — все аккаунты)
    getReceivedStats: (accountId?: number) =>
      api.get<ReceivedStats>(`${prefix}/received-stats`, { params: { account_id: accountId } }),
    getReceivedSubnetIps: (subnet: string, accountId?: number) =>
      api.get<ReceivedIP[]>(`${prefix}/received-ips`, { params: { subnet, account_id: accountId } }),
    clearReceivedIps: (accountId?: number) =>
      api.delete<{ success: boolean; deleted: number }>(`${prefix}/received-ips`, { params: { account_id: accountId } }),

    // Settings
    getSettings: () => api.get<CloudSettings>(`${prefix}/settings`),
    updateSettings: (data: Partial<CloudSettings>) => api.put<CloudSettings>(`${prefix}/settings`, data),

    // Whitelist CIDR (одиночный IP тоже — хранится как /32)
    getWhitelist: () => api.get<CloudWhitelistEntry[]>(`${prefix}/whitelist`),
    addWhitelistCidr: (cidr: string) =>
      api.post<{ success: boolean; id?: number; cidr?: string; error?: string }>(`${prefix}/whitelist`, { cidr }),
    addWhitelistCidrs: (cidrs: string[]) =>
      api.post<{ success: boolean; added: number; skipped: number; invalid: string[] }>(`${prefix}/whitelist/bulk`, { cidrs }),
    removeWhitelistCidr: (id: number) => api.delete(`${prefix}/whitelist/${id}`),
    clearWhitelist: () => api.delete<{ success: boolean; deleted: number }>(`${prefix}/whitelist`),

    // Control (all accounts)
    start: () => api.post<{ success: boolean; pid?: number; error?: string }>(`${prefix}/start`),
    stop: () => api.post<{ success: boolean; error?: string }>(`${prefix}/stop`),
    getStatus: () => api.get<CloudStatus>(`${prefix}/status`),

    // Per-account control
    startAccount: (accountId: number) =>
      api.post<{ success: boolean; pid?: number; error?: string }>(`${prefix}/accounts/${accountId}/start`),
    stopAccount: (accountId: number) =>
      api.post<{ success: boolean; error?: string }>(`${prefix}/accounts/${accountId}/stop`),
    getWorkerStatuses: () => api.get<CloudWorkerStatuses>(`${prefix}/worker/statuses`),
    getAccountErrors: (accountId: number) => api.get<CloudAccountError[]>(`${prefix}/accounts/${accountId}/errors`),
    clearAccountErrors: (accountId: number) => api.delete(`${prefix}/accounts/${accountId}/errors`),

    // Logs
    getLogs: (lines: number = 100) => api.get<{ logs: string[] }>(`${prefix}/logs`, { params: { lines } }),
    clearLogs: () => api.delete(`${prefix}/logs`),
    getLogsStreamUrl: () => `/api/_int${prefix}/logs/stream`,

    // Manual IP
    manualObtainIp: (data: CloudManualObtainRequest) =>
      api.post<CloudManualObtainResult>(`${prefix}/manual-obtain`, data),
  }
}

export type CloudApi = ReturnType<typeof createCloudApi>

export const cloudEvoApi = createCloudApi('/cloud/evo')
export const cloudAdvApi = createCloudApi('/cloud/adv')

// ═══════════════════════════════════════════════════════════════════════
// ШИРИНА КАНАЛА (ADVANCED)
// ═══════════════════════════════════════════════════════════════════════

export type CloudBwMode = 'off' | 'schedule' | 'dynamic'

export interface CloudBwAccount {
  id: number
  label: string
  key_id: string
  key_secret_masked: string
  project_id: string
  ip_count: number
  created_at: string | null
}

export interface CloudBwAccountInput {
  label?: string
  key_id?: string
  key_secret?: string
  project_id?: string
}

/** Конец не входит в интервал; end_hour меньше start_hour — интервал через полночь */
export interface CloudBwScheduleRule {
  start_hour: number
  end_hour: number
  mbps: number
}

export interface CloudBwDynamic {
  window_hours: number
  reserve_percent: number
  interval_hours: number
  min_mbps: number
  max_mbps: number
}

export interface CloudBwMember {
  eip_id: string
  ip_address: string
}

/** Адрес с выделенной полосой или общая полоса целиком (shared): у неё нет eip_id, адреса — в members */
export interface CloudBwIp {
  id: number
  account_id: number
  account_label: string
  shared: boolean
  eip_id: string | null
  ip_address: string
  bandwidth_id: string
  bandwidth_name: string | null
  members: CloudBwMember[]
  server_id: number | null
  server_name: string | null
  server_ids: number[]
  mode: CloudBwMode
  schedule: CloudBwScheduleRule[]
  dynamic: CloudBwDynamic
  current_mbps: number | null
  charge_mode: string | null
  share_type: string | null
  last_check_at: string | null
  last_change_at: string | null
  last_peak_mbps: number | null
  last_note: string | null
  last_error: string | null
  next_decrease_at: string | null
}

export interface CloudBwIpConfig {
  server_id: number | null
  server_ids: number[]
  mode: CloudBwMode
  schedule: CloudBwScheduleRule[]
  dyn_window_hours: number
  dyn_reserve_percent: number
  dyn_interval_hours: number
  dyn_min_mbps: number
  dyn_max_mbps: number
}

export interface CloudBwCloudEip {
  eip_id: string
  ip_address: string
  bandwidth_id: string | null
  bandwidth_name: string | null
  bandwidth_mbps: number | null
  share_type: string | null
  status: string | null
  attached: boolean
  added: boolean
  suggested_server_id: number | null
  suggested_server_name: string | null
}

export interface CloudBwHistoryEntry {
  id: number
  created_at: string | null
  old_mbps: number | null
  new_mbps: number | null
  note: string | null
  error: string | null
}

export type CloudBwUsagePeriod = 'day' | 'week' | '30d' | 'month' | 'prev_month'

/** Средняя ширина и оплата записи за период; рубли — с НДС */
export interface CloudBwUsageItem {
  ip_id: number
  shared: boolean
  ip_address: string
  bandwidth_name: string | null
  account_label: string
  current_mbps: number | null
  /** Начало посчитанного отрезка: позже начала периода, если запись добавили посреди него */
  covered_from: string | null
  hours: number
  avg_mbps: number | null
  min_mbps: number | null
  max_mbps: number | null
  mbps_hours: number
  eip_count: number
  bandwidth_rub: number
  eip_rub: number
  total_rub: number
}

export interface CloudBwUsage {
  period: CloudBwUsagePeriod
  start: string
  end: string
  hours: number
  items: CloudBwUsageItem[]
  total: {
    avg_mbps: number
    mbps_hours: number
    bandwidth_rub: number
    eip_rub: number
    total_rub: number
    total_rub_without_vat: number
    forecast_30d_rub: number
  }
  /** Цены без НДС */
  tariff: {
    bandwidth_per_mbps_hour: number
    low_tier_per_mbps_hour: number
    dedicated_low_tier_mbps: number
    shared_low_tier_mbps: number
    eip_per_hour: number
    vat_percent: number
  }
}

const BW_PREFIX = '/cloud/bw'

export const cloudBwApi = {
  getAccounts: () => api.get<CloudBwAccount[]>(`${BW_PREFIX}/accounts`),
  createAccount: (data: CloudBwAccountInput) => api.post<CloudBwAccount>(`${BW_PREFIX}/accounts`, data),
  updateAccount: (id: number, data: CloudBwAccountInput) => api.put<CloudBwAccount>(`${BW_PREFIX}/accounts/${id}`, data),
  deleteAccount: (id: number) => api.delete(`${BW_PREFIX}/accounts/${id}`),
  getCloudEips: (accountId: number) => api.get<CloudBwCloudEip[]>(`${BW_PREFIX}/accounts/${accountId}/eips`),

  getIps: () => api.get<CloudBwIp[]>(`${BW_PREFIX}/ips`),
  addIp: (data: { account_id: number; eip_id: string; server_id: number | null }) =>
    api.post<CloudBwIp>(`${BW_PREFIX}/ips`, data),
  addShared: (data: { account_id: number; bandwidth_id: string; server_ids: number[] }) =>
    api.post<CloudBwIp>(`${BW_PREFIX}/shared`, data),
  updateIp: (id: number, config: CloudBwIpConfig) => api.put<CloudBwIp>(`${BW_PREFIX}/ips/${id}`, config),
  applyIp: (id: number) => api.post<CloudBwIp>(`${BW_PREFIX}/ips/${id}/apply`),
  deleteIp: (id: number) => api.delete(`${BW_PREFIX}/ips/${id}`),
  getHistory: (id: number) => api.get<CloudBwHistoryEntry[]>(`${BW_PREFIX}/ips/${id}/history`),
  getUsage: (period: CloudBwUsagePeriod) => api.get<CloudBwUsage>(`${BW_PREFIX}/usage`, { params: { period } }),
}
