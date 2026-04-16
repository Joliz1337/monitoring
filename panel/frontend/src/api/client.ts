import axios, { AxiosError, AxiosRequestConfig, AxiosResponse, InternalAxiosRequestConfig } from 'axios'

const DEFAULT_TIMEOUT_MS = 30000

const api = axios.create({
  baseURL: '/api',
  withCredentials: true,
  timeout: DEFAULT_TIMEOUT_MS,
  headers: {
    'Content-Type': 'application/json',
  },
})

const MAX_RETRIES = 2
const RETRYABLE_STATUSES = new Set([502, 503, 504])

interface RetryableConfig extends InternalAxiosRequestConfig {
  __retryCount?: number
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

function resolveTimeout(url: string): number {
  if (url.includes('/speedtest') || url.includes('/backup')) return 300_000
  if (url.includes('/system/update') || url.includes('/wildcard-ssl')) return 180_000
  if (
    url.includes('/metrics') ||
    url.endsWith('/haproxy/status') ||
    url.includes('/auth/check')
  ) {
    return 15_000
  }
  return DEFAULT_TIMEOUT_MS
}

api.interceptors.request.use((config) => {
  const url = config.url || ''
  if (config.timeout === DEFAULT_TIMEOUT_MS || config.timeout === undefined) {
    config.timeout = resolveTimeout(url)
  }
  return config
})

api.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    if (error.response?.status === 401) {
      const currentPath = window.location.pathname
      const uid = currentPath.split('/')[1]
      if (uid && !currentPath.includes('/login')) {
        window.location.href = `/${uid}/login`
      }
      return Promise.reject(error)
    }

    const config = error.config as RetryableConfig | undefined
    const method = config?.method?.toLowerCase()
    const status = error.response?.status
    const shouldRetry =
      config &&
      method === 'get' &&
      (status === undefined || RETRYABLE_STATUSES.has(status)) &&
      (config.__retryCount ?? 0) < MAX_RETRIES

    if (shouldRetry) {
      config.__retryCount = (config.__retryCount ?? 0) + 1
      const delay = 300 * Math.pow(2, config.__retryCount - 1)
      await sleep(delay)
      return api.request(config)
    }

    return Promise.reject(error)
  }
)

// GET request deduplication: if the same GET is already in-flight, reuse its promise
const inflightGets = new Map<string, Promise<AxiosResponse>>()
const originalGet = api.get.bind(api)
api.get = function <T = unknown>(url: string, config?: AxiosRequestConfig): Promise<AxiosResponse<T>> {
  const paramsKey = JSON.stringify(config?.params ?? {})
  const acceptKey = (config?.headers?.['Accept'] as string | undefined) ?? ''
  const key = `${url}|${paramsKey}|${acceptKey}`
  const existing = inflightGets.get(key)
  if (existing) return existing as Promise<AxiosResponse<T>>

  const promise = originalGet<T>(url, config).finally(() => {
    inflightGets.delete(key)
  })
  inflightGets.set(key, promise as Promise<AxiosResponse>)
  return promise
} as typeof api.get

export interface Server {
  id: number
  name: string
  url: string
  position: number
  is_active: boolean
  folder?: string | null
  last_seen?: string | null
  last_error?: string | null
  error_code?: number | null
  pki_enabled?: boolean
  uses_shared_cert?: boolean
  auth_kind?: 'shared' | 'per_server' | 'legacy'
}

export interface TimezoneInfo {
  name: string
  offset: string
  offset_seconds: number
}

export interface CertificateExpiry {
  domain: string
  days_left: number
  expiry_date: string
  expired: boolean
}

export interface ServerMetrics {
  timestamp: string
  server_name: string
  timezone?: TimezoneInfo
  cpu: {
    cores_physical: number
    cores_logical: number
    model: string
    usage_percent: number
    per_cpu_percent: number[]
    load_avg_1: number
    load_avg_5: number
    load_avg_15: number
    frequency?: {
      current: number
      min: number
      max: number
    }
  }
  memory: {
    ram: {
      total: number
      used: number
      free: number
      available: number
      percent: number
    }
    swap: {
      total: number
      used: number
      free: number
      percent: number
    }
  }
  disk: {
    partitions: Array<{
      device: string
      mountpoint: string
      fstype: string
      total: number
      used: number
      free: number
      percent: number
    }>
    io: Record<string, {
      read_bytes: number
      write_bytes: number
      read_bytes_per_sec: number
      write_bytes_per_sec: number
    }>
  }
  network: {
    interfaces: Array<{
      name: string
      rx_bytes: number
      tx_bytes: number
      rx_bytes_per_sec: number
      tx_bytes_per_sec: number
      rx_peak_per_sec?: number
      tx_peak_per_sec?: number
      is_up: boolean
      is_virtual?: boolean
    }>
    total: {
      rx_bytes: number
      tx_bytes: number
      rx_bytes_per_sec: number
      tx_bytes_per_sec: number
      rx_peak_per_sec?: number
      tx_peak_per_sec?: number
    }
  }
  system: {
    hostname: string
    os: string
    kernel: string
    architecture: string
    boot_time: string
    uptime_seconds: number
    uptime_human: string
    connections: {
      established: number
      listen: number
      time_wait: number
    }
    connections_detailed?: {
      tcp: {
        total: number
        established: number
        listen: number
        time_wait: number
        close_wait: number
        syn_sent: number
        fin_wait: number
        other: number
      }
      udp: {
        total: number
      }
    }
  }
  processes: {
    total: number
    running: number
    sleeping: number
    top_by_cpu: Array<{
      pid: number
      name: string
      cpu_percent: number
      memory_percent: number
      status: string
    }>
    top_by_memory: Array<{
      pid: number
      name: string
      cpu_percent: number
      memory_percent: number
      status: string
    }>
  }
  certificates?: {
    count: number
    closest_expiry: CertificateExpiry | null
  }
}

export interface HAProxyRule {
  name: string
  rule_type: 'tcp' | 'https'
  listen_port: number
  target_ip: string
  target_port: number
  cert_domain?: string
  target_ssl?: boolean
  send_proxy?: boolean
  use_wildcard?: boolean
}

export interface HAProxyStatus {
  running: boolean
  config_valid: boolean
  config_message: string
}

export interface CertificateFiles {
  pem: string | null
  key: string | null
  cert: string | null
  fullchain: string | null
  chain: string | null
}

export interface Certificate {
  domain: string
  expiry_date: string
  days_left: number
  expired: boolean
  combined_exists?: boolean
  cert_path?: string
  source?: 'letsencrypt' | 'custom'
  files?: CertificateFiles
}

export interface FirewallRule {
  number: number
  port: number
  protocol: string
  action: string
  from_ip: string
  direction: string
  ipv6: boolean
}

export interface FirewallStatus {
  active: boolean
  default_incoming: string
  default_outgoing: string
  logging: string
  error?: string
}

export interface FirewallActionResponse {
  success: boolean
  message: string
  error_log?: string
}

export interface SystemInfo {
  cpu_cores: number
  ram_mb: number
  maxconn: number
  nbthread: number
  ulimit_nofile: number
  optimizations_applied: boolean
}

export interface CertificateGenerateResponse {
  success: boolean
  message: string
  domain: string
  error_log?: string
}

export interface TrafficDataPoint {
  hour?: string
  date?: string
  month?: string
  rx_bytes: number
  tx_bytes: number
}

export interface TrafficData {
  hours?: number
  days?: number
  months?: number
  interface?: string
  port?: number
  data: TrafficDataPoint[]
  total_rx: number
  total_tx: number
}

export interface TrafficSummary {
  days: number
  total: {
    rx_bytes: number
    tx_bytes: number
    days: number
  }
  by_interface: Array<{
    interface: string
    rx_bytes: number
    tx_bytes: number
  }>
  by_port: Array<{
    port: number
    rx_bytes: number
    tx_bytes: number
  }>
  tracked_ports: number[]
}

export interface PortsTraffic {
  days: number
  tracked_ports: number[]
  data: Array<{
    port: number
    rx_bytes: number
    tx_bytes: number
  }>
  total_rx: number
  total_tx: number
}

export interface InterfacesTraffic {
  days: number
  data: Array<{
    interface: string
    rx_bytes: number
    tx_bytes: number
  }>
  total_rx: number
  total_tx: number
}

export interface SpeedtestServerConfig {
  host: string
  port: number
  label: string
  region: string
}

export interface SpeedtestResultEntry {
  server: string
  port: number
  download_mbps: number
  upload_mbps?: number
  latency_ms?: number
  server_name?: string
  retransmits?: number
  error?: string
}

export interface SpeedtestResult {
  best_speed_mbps: number
  upload_mbps?: number
  best_server: string
  threshold_mbps: number
  ok: boolean
  test_mode?: string
  method?: string
  results: SpeedtestResultEntry[]
  tested_at: string | null
}

export interface ServerSpeedtest {
  best_speed_mbps: number
  best_server: string
  ok: boolean
  tested_at: string | null
}

export const authApi = {
  login: (password: string) => api.post('/auth/login', { password }),
  logout: () => api.post('/auth/logout'),
  check: () => api.get('/auth/check'),
  validateUid: (uid: string) => api.post<{ valid: boolean }>('/auth/validate-uid', { uid }),
}

export interface ServerWithMetrics extends Server {
  metrics?: ServerMetrics | null
  status?: 'online' | 'offline' | 'loading' | 'error'
}

export const serversApi = {
  list: (includeMetrics?: boolean) =>
    api.get<{ count: number; servers: ServerWithMetrics[] }>('/servers', {
      params: includeMetrics ? { include_metrics: true } : undefined
    }),
  get: (id: number) => api.get<Server>(`/servers/${id}`),
  create: (data: { name: string; url: string }) =>
    api.post<{
      success: boolean
      server: Server
    }>('/servers', data),
  installerToken: () => api.get<{ token: string }>('/servers/installer-token'),
  migrationStatus: () =>
    api.get<{
      total: number
      shared: number
      per_server: number
      legacy: number
      needs_migration: number
    }>('/servers/migration-status'),
  migrateOne: (id: number) =>
    api.post<
      | { status: 'already_shared' }
      | { status: 'auto'; success: boolean }
      | { status: 'manual'; token: string }
    >(`/servers/${id}/migrate`),
  confirmMigration: (id: number) =>
    api.post<{ success: boolean }>(`/servers/${id}/confirm-migration`),
  migrateAll: () =>
    api.post<{
      auto_migrated: { id: number; name: string }[]
      failed: { id: number; name: string; error: string }[]
      manual_required: { id: number; name: string }[]
      token: string | null
    }>('/servers/migrate-all'),
  update: (id: number, data: Partial<Server>) => api.put(`/servers/${id}`, data),
  delete: (id: number) => api.delete(`/servers/${id}`),
  reorder: (serverIds: number[]) => api.post('/servers/reorder', { server_ids: serverIds }),
  test: (id: number) => api.post<{ success: boolean; status: string; message?: string }>(`/servers/${id}/test`),
  moveToFolder: (serverIds: number[], folder: string | null) =>
    api.post<{ success: boolean; moved: number }>('/servers/move-to-folder', { server_ids: serverIds, folder }),
  renameFolder: (oldName: string, newName: string) =>
    api.post<{ success: boolean; renamed: number }>('/servers/folders/rename', { old_name: oldName, new_name: newName }),
  deleteFolder: (folderName: string) =>
    api.delete<{ success: boolean; unfoldered: number }>(`/servers/folders/${encodeURIComponent(folderName)}`),
}

export const proxyApi = {
  // Returns cached metrics from panel's database (collected by background worker)
  getMetrics: (serverId: number) => api.get<ServerMetrics>(`/proxy/${serverId}/metrics`),
  // Returns live metrics directly from node (use sparingly, causes load)
  getLiveMetrics: (serverId: number) => api.get<ServerMetrics>(`/proxy/${serverId}/metrics/live`),
  // History is stored on the panel (collected every 5 seconds)
  // period: '1h', '24h', '7d', '30d', '365d'
  // include_per_cpu: true to include per-CPU usage data (only for raw data periods: 1h, 24h)
  getHistory: (serverId: number, params?: { period?: string; from_time?: string; to_time?: string; limit?: number; include_per_cpu?: boolean }) =>
    api.get(`/proxy/${serverId}/metrics/history`, { params }),
  
  // Cached HAProxy data (status, rules, certs, firewall) - updated every 30s
  getHAProxyCached: (serverId: number) => 
    api.get<{ status?: HAProxyStatus; rules?: { count: number; rules: HAProxyRule[] }; certs?: { certificates: Certificate[]; count: number }; firewall?: { rules: FirewallRule[]; count: number; active: boolean }; cached_at?: string }>(`/proxy/${serverId}/haproxy/cached`),
  
  // Cached Traffic data (summary, tracked_ports) - updated every 30s
  getTrafficCached: (serverId: number) =>
    api.get<{ summary?: TrafficSummary; tracked_ports?: { tracked_ports: number[] }; cached_at?: string }>(`/proxy/${serverId}/traffic/cached`),
  
  getHAProxyStatus: (serverId: number) => api.get<HAProxyStatus>(`/proxy/${serverId}/haproxy/status`),
  getHAProxyRules: (serverId: number) => 
    api.get<{ count: number; rules: HAProxyRule[] }>(`/proxy/${serverId}/haproxy/rules`),
  createHAProxyRule: (serverId: number, rule: Omit<HAProxyRule, 'name'> & { name: string }) =>
    api.post(`/proxy/${serverId}/haproxy/rules`, rule),
  updateHAProxyRule: (serverId: number, name: string, data: Partial<HAProxyRule>) =>
    api.put(`/proxy/${serverId}/haproxy/rules/${name}`, data),
  deleteHAProxyRule: (serverId: number, name: string) =>
    api.delete(`/proxy/${serverId}/haproxy/rules/${name}`),
  reloadHAProxy: (serverId: number) => api.post(`/proxy/${serverId}/haproxy/reload`),
  restartHAProxy: (serverId: number) => api.post(`/proxy/${serverId}/haproxy/restart`),
  startHAProxy: (serverId: number) => api.post(`/proxy/${serverId}/haproxy/start`),
  stopHAProxy: (serverId: number) => api.post(`/proxy/${serverId}/haproxy/stop`),
  applyHAProxyConfig: (serverId: number, configContent: string, reloadAfter: boolean = true) =>
    api.post<{ success: boolean; message: string; config_valid: boolean; reloaded: boolean }>(
      `/proxy/${serverId}/haproxy/config/apply`,
      { config_content: configContent, reload_after: reloadAfter }
    ),
  getHAProxyConfig: (serverId: number) =>
    api.get<{ content: string; path: string }>(`/proxy/${serverId}/haproxy/config`),
  
  getHAProxyCerts: (serverId: number) => 
    api.get<{ certificates: string[] }>(`/proxy/${serverId}/haproxy/certs`),
  getHAProxyCert: (serverId: number, domain: string) =>
    api.get<Certificate>(`/proxy/${serverId}/haproxy/certs/${domain}`),
  getAllCerts: (serverId: number) =>
    api.get<{ certificates: Certificate[]; count: number }>(`/proxy/${serverId}/haproxy/certs/all`),
  generateCert: (serverId: number, data: { domain: string; email?: string; method?: string }) =>
    api.post(`/proxy/${serverId}/haproxy/certs/generate`, data),
  renewCerts: (serverId: number) => 
    api.post<{ success: boolean; message: string; renewed_domains: string[] }>(`/proxy/${serverId}/haproxy/certs/renew`),
  renewSingleCert: (serverId: number, domain: string) => 
    api.post<{ success: boolean; message: string; domain: string; output_log?: string }>(`/proxy/${serverId}/haproxy/certs/${domain}/renew`),
  deleteCert: (serverId: number, domain: string) =>
    api.delete(`/proxy/${serverId}/haproxy/certs/${domain}`),
  uploadCert: (serverId: number, data: { domain: string; cert_content: string; key_content: string }) =>
    api.post(`/proxy/${serverId}/haproxy/certs/upload`, data),
  
  // Firewall management
  getFirewallStatus: (serverId: number) =>
    api.get<FirewallStatus>(`/proxy/${serverId}/haproxy/firewall/status`),
  getFirewallRules: (serverId: number) =>
    api.get<{ rules: FirewallRule[]; count: number; active: boolean }>(`/proxy/${serverId}/haproxy/firewall/rules`),
  allowPort: (serverId: number, port: number, protocol: string = 'tcp') =>
    api.post<FirewallActionResponse>(`/proxy/${serverId}/haproxy/firewall/allow`, { port, protocol }),
  denyPort: (serverId: number, port: number, protocol: string = 'tcp') =>
    api.post<FirewallActionResponse>(`/proxy/${serverId}/haproxy/firewall/deny`, { port, protocol }),
  addFirewallRule: (serverId: number, data: {
    port: number
    protocol: string
    action: 'allow' | 'deny'
    from_ip?: string | null
    direction: 'in' | 'out'
  }) => api.post<FirewallActionResponse>(`/proxy/${serverId}/haproxy/firewall/rule`, data),
  deleteFirewallRule: (serverId: number, port: number, protocol: string = 'tcp') =>
    api.delete<FirewallActionResponse>(`/proxy/${serverId}/haproxy/firewall/${port}?protocol=${protocol}`),
  deleteFirewallRuleByNumber: (serverId: number, ruleNumber: number) =>
    api.delete<FirewallActionResponse>(`/proxy/${serverId}/haproxy/firewall/rule/${ruleNumber}`),
  enableFirewall: (serverId: number) =>
    api.post<FirewallActionResponse>(`/proxy/${serverId}/haproxy/firewall/enable`),
  disableFirewall: (serverId: number) =>
    api.post<FirewallActionResponse>(`/proxy/${serverId}/haproxy/firewall/disable`),
  
  // System info
  getSystemInfo: (serverId: number) =>
    api.get<SystemInfo>(`/proxy/${serverId}/haproxy/system/info`),
  
  // Traffic tracking
  getTrafficSummary: (serverId: number, days: number = 30) =>
    api.get<TrafficSummary>(`/proxy/${serverId}/traffic/summary`, { params: { days } }),
  getHourlyTraffic: (serverId: number, params?: { hours?: number; interface?: string; port?: number }) =>
    api.get<TrafficData>(`/proxy/${serverId}/traffic/hourly`, { params }),
  getDailyTraffic: (serverId: number, params?: { days?: number; interface?: string; port?: number }) =>
    api.get<TrafficData>(`/proxy/${serverId}/traffic/daily`, { params }),
  getMonthlyTraffic: (serverId: number, params?: { months?: number; interface?: string; port?: number }) =>
    api.get<TrafficData>(`/proxy/${serverId}/traffic/monthly`, { params }),
  getPortsTraffic: (serverId: number, days: number = 30) =>
    api.get<PortsTraffic>(`/proxy/${serverId}/traffic/ports`, { params: { days } }),
  getInterfacesTraffic: (serverId: number, days: number = 30) =>
    api.get<InterfacesTraffic>(`/proxy/${serverId}/traffic/interfaces`, { params: { days } }),
  getTrackedPorts: (serverId: number) =>
    api.get<{ tracked_ports: number[] }>(`/proxy/${serverId}/traffic/ports/tracked`),
  addTrackedPort: (serverId: number, port: number) =>
    api.post<{ success: boolean; message: string }>(`/proxy/${serverId}/traffic/ports/add`, { port }),
  removeTrackedPort: (serverId: number, port: number) =>
    api.post<{ success: boolean; message: string }>(`/proxy/${serverId}/traffic/ports/remove`, { port }),
  
  // Command execution
  executeCommand: (serverId: number, command: string, timeout?: number, shell?: 'sh' | 'bash') =>
    api.post<ExecuteResponse>(`/proxy/${serverId}/system/execute`, {
      command,
      timeout: timeout || 30,
      shell: shell || 'sh'
    }),
  
  // Get SSE URL for streaming command execution
  getExecuteStreamUrl: (serverId: number) => `/api/proxy/${serverId}/system/execute-stream`,
  
  // Speed test
  runSpeedtest: (serverId: number) =>
    api.post<{ success: boolean } & SpeedtestResult>(`/proxy/${serverId}/speedtest`),
  getSpeedtest: (serverId: number) =>
    api.get<SpeedtestResult>(`/proxy/${serverId}/speedtest`),
}

export interface ExecuteRequest {
  command: string
  timeout?: number
  shell?: 'sh' | 'bash'
}

export interface ExecuteResponse {
  success: boolean
  exit_code: number
  stdout: string
  stderr: string
  execution_time_ms: number
  error: string | null
}

export interface SSEStdoutEvent {
  line: string
}

export interface SSEStderrEvent {
  line: string
}

export interface SSEDoneEvent {
  exit_code: number
  execution_time_ms: number
  success: boolean
}

export interface SSEErrorEvent {
  message: string
}

export interface TimeSyncStatus {
  sync_in_progress: boolean
  last_sync: string | null
  next_sync_in: number | null
  last_results: Array<{
    name: string
    server_id?: number
    success: boolean
    timezone?: string
    ntp_synchronized?: boolean
    error?: string
  }>
}

export const settingsApi = {
  getAll: () => api.get<{ settings: Record<string, string> }>('/settings'),
  get: (key: string) => api.get<{ key: string; value: string }>(`/settings/${key}`),
  set: (key: string, value: string) => api.put(`/settings/${key}`, { value }),
  speedtestTestNotification: (bot_token?: string, chat_id?: string) =>
    api.post<{ success: boolean; error?: string }>('/settings/speedtest/test-notification', { bot_token, chat_id }),
  timeSyncRun: () => api.post<{ success: boolean }>('/settings/time-sync/run'),
  timeSyncStatus: () => api.get<TimeSyncStatus>('/settings/time-sync/status'),
}

// Blocklist types
export type BlocklistDirection = 'in' | 'out'

export interface BlocklistRule {
  id: number
  ip_cidr: string
  server_id: number | null
  is_permanent: boolean
  direction: BlocklistDirection
  comment: string | null
  source: string
  created_at: string
}

export interface BlocklistSource {
  id: number
  name: string
  url: string
  enabled: boolean
  is_default: boolean
  direction: BlocklistDirection
  last_updated: string | null
  ip_count: number
  error_message: string | null
}

export interface BlocklistSettings {
  temp_timeout: number
  auto_update_enabled: boolean
  auto_update_interval: number
}

export interface SyncServerResult {
  server_id: number
  server_name: string
  success: boolean
  in: { success: boolean; message: string; ip_count: number; added?: number; removed?: number }
  out: { success: boolean; message: string; ip_count: number; added?: number; removed?: number }
}

export interface SyncStatus {
  in_progress: boolean
  timestamp: string | null
  servers: Record<string, SyncServerResult>
}

export const blocklistApi = {
  // Global rules
  getGlobal: (direction: BlocklistDirection = 'in') =>
    api.get<{ count: number; direction: string; rules: BlocklistRule[] }>('/blocklist/global', { params: { direction } }),
  addGlobal: (data: { ip_cidr: string; is_permanent?: boolean; direction?: BlocklistDirection; comment?: string }) =>
    api.post('/blocklist/global', data),
  addGlobalBulk: (ips: string[], is_permanent: boolean = true, direction: BlocklistDirection = 'in') =>
    api.post('/blocklist/global/bulk', { ips, is_permanent, direction }),
  deleteGlobal: (id: number) => api.delete(`/blocklist/global/${id}`),
  
  // Server rules
  getServer: (serverId: number, direction: BlocklistDirection = 'in') => 
    api.get<{ server_id: number; count: number; global_count: number; direction: string; rules: BlocklistRule[] }>(
      `/blocklist/server/${serverId}`, { params: { direction } }
    ),
  addServer: (serverId: number, data: { ip_cidr: string; is_permanent?: boolean; direction?: BlocklistDirection; comment?: string }) =>
    api.post(`/blocklist/server/${serverId}`, data),
  deleteServer: (serverId: number, ruleId: number) =>
    api.delete(`/blocklist/server/${serverId}/${ruleId}`),
  getServerStatus: (serverId: number) =>
    api.get(`/blocklist/server/${serverId}/status`),
  
  // Sources
  getSources: (direction?: BlocklistDirection) =>
    api.get<{ count: number; sources: BlocklistSource[] }>('/blocklist/sources', { params: direction ? { direction } : {} }),
  addSource: (data: { name: string; url: string; direction?: BlocklistDirection }) =>
    api.post('/blocklist/sources', data),
  updateSource: (id: number, data: { enabled?: boolean; name?: string }) =>
    api.put(`/blocklist/sources/${id}`, data),
  deleteSource: (id: number) => api.delete(`/blocklist/sources/${id}`),
  refreshSource: (id: number) => api.post(`/blocklist/sources/${id}/refresh`),
  refreshAll: () => api.post('/blocklist/sources/refresh-all'),
  
  // Settings & sync
  getSettings: () => api.get<{ settings: BlocklistSettings }>('/blocklist/settings'),
  updateSettings: (data: { temp_timeout?: number; auto_update_enabled?: boolean; auto_update_interval?: number }) =>
    api.put('/blocklist/settings', data),
  sync: () => api.post<{ success: boolean; results: Record<string, SyncServerResult> }>('/blocklist/sync'),
  syncServer: (serverId: number) => api.post<SyncServerResult>(`/blocklist/sync/${serverId}`),
  getSyncStatus: () => api.get<SyncStatus>('/blocklist/sync/status'),
  
}

export interface NodeOptimizationsInfo {
  installed: boolean
  version: string | null
  nic_mode?: string
  profile?: string
}

export interface NicInterface {
  name: string
  max_combined: number
  current_combined: number
}

export interface NicInfo {
  nic_mode: string
  multiqueue_supported: boolean
  hybrid_recommended?: boolean
  interfaces: NicInterface[]
}

export interface RemoveOptimizationsResponse {
  success: boolean
  message: string
  removed_files: string[]
  errors?: string[]
}

export interface VersionBaseNode {
  id: number
  name: string
  url: string
}

export interface VersionBaseInfo {
  panel: {
    version: string
    latest_version: string | null
    update_available: boolean
  }
  node: {
    latest_version: string | null
  }
  optimizations: {
    latest_version: string | null
  }
  nodes: VersionBaseNode[]
  update_in_progress: boolean
}

export interface SingleNodeVersion {
  id: number
  name: string
  url: string
  version: string | null
  status: 'online' | 'offline'
  optimizations: NodeOptimizationsInfo
}

export interface UpdateResponse {
  success: boolean
  message: string
  target_version: string
}

export interface UpdateStatus {
  in_progress: boolean
  last_result: 'success' | 'failed' | 'not_due' | null
  last_error: string | null
  output?: string | null
  started_at?: string | null
  completed_at?: string | null
}

export interface PanelCertificateInfo {
  domain: string | null
  expiry_date?: string
  days_left?: number
  expired?: boolean
  error?: string
  renewal_in_progress?: boolean
}

export interface CertRenewalResponse {
  success: boolean
  message: string
}

export interface ApplyOptimizationsResponse {
  success: boolean
  message: string
  version: string | null
}

export interface BulkResult {
  server_id: number
  server_name: string
  success: boolean
  message: string
}

export interface BulkTerminalResult extends BulkResult {
  stdout: string
  stderr: string
  exit_code: number
  execution_time_ms: number
}

export const bulkApi = {
  // HAProxy service
  startHAProxy: (serverIds: number[]) =>
    api.post<BulkResult[]>('/bulk/haproxy/start', { server_ids: serverIds }),
  
  stopHAProxy: (serverIds: number[]) =>
    api.post<BulkResult[]>('/bulk/haproxy/stop', { server_ids: serverIds }),

  restartHAProxy: (serverIds: number[]) =>
    api.post<BulkResult[]>('/bulk/haproxy/restart', { server_ids: serverIds }),

  // Traffic ports
  addTrackedPort: (serverIds: number[], port: number) =>
    api.post<BulkResult[]>('/bulk/traffic/ports', { server_ids: serverIds, port }),
  
  removeTrackedPort: (serverIds: number[], port: number) =>
    api.delete<BulkResult[]>('/bulk/traffic/ports', { data: { server_ids: serverIds, port } }),
  
  // Firewall rules
  addFirewallRule: (serverIds: number[], rule: {
    port: number
    protocol: 'tcp' | 'udp' | 'any'
    action: 'allow' | 'deny'
    from_ip?: string | null
    direction: 'in' | 'out'
  }) => api.post<BulkResult[]>('/bulk/firewall/rules', { server_ids: serverIds, ...rule }),
  
  deleteFirewallRule: (serverIds: number[], port: number) =>
    api.delete<BulkResult[]>('/bulk/firewall/rules', { data: { server_ids: serverIds, port } }),

  executeCommand: (serverIds: number[], command: string, timeout: number = 30, shell: 'sh' | 'bash' = 'sh') =>
    api.post<BulkTerminalResult[]>('/bulk/terminal/execute', {
      server_ids: serverIds, command, timeout, shell
    }),

}

export interface PanelIpInfo {
  ip: string | null
  domain: string
}

export interface PanelServerStats {
  cpu: {
    percent: number
    cores: number
    load_avg_1: number
    load_avg_5: number
    load_avg_15: number
  }
  memory: {
    total: number
    used: number
    available: number
    percent: number
    swap_total: number
    swap_used: number
    swap_percent: number
  }
  disk: {
    total: number
    used: number
    free: number
    percent: number
  }
  disk_var?: {
    total: number
    used: number
    free: number
    percent: number
  } | null
}

export const systemApi = {
  getPanelIp: () => api.get<PanelIpInfo>('/system/panel-ip'),
  getVersionBase: () => api.get<VersionBaseInfo>('/system/version/base'),
  getNodeVersionById: (nodeId: number) => api.get<SingleNodeVersion>(`/system/nodes/${nodeId}/version`, { timeout: 15000 }),
  updatePanel: (targetVersion?: string) => 
    api.post<UpdateResponse>('/system/update', targetVersion ? { target_version: targetVersion } : {}),
  getUpdateStatus: () => api.get<UpdateStatus>('/system/update/status'),
  
  // Node updates via proxy
  getNodeVersion: (serverId: number) => 
    api.get<{ version: string; component: string; node_name: string }>(`/proxy/${serverId}/system/version`),
  updateNode: (serverId: number, targetVersion?: string) =>
    api.post<UpdateResponse>(`/proxy/${serverId}/system/update`, { 
      ...(targetVersion && { target_version: targetVersion })
    }),
  getNodeUpdateStatus: (serverId: number) =>
    api.get<UpdateStatus>(`/proxy/${serverId}/system/update/status`),
  
  // Panel SSL certificate
  getCertificate: () => api.get<PanelCertificateInfo>('/system/certificate'),
  renewCertificate: () => api.post<CertRenewalResponse>('/system/certificate/renew?force=true'),
  getCertRenewalStatus: () => api.get<UpdateStatus>('/system/certificate/renew/status'),
  
  // System Optimizations
  getNodeNicInfo: (serverId: number) =>
    api.get<NicInfo>(`/proxy/${serverId}/system/nic-info`, { timeout: 15000 }),
  applyNodeOptimizations: (serverId: number, nicMode: string = 'rps', optProfile: string = 'vpn') =>
    api.post<ApplyOptimizationsResponse>(`/proxy/${serverId}/system/optimizations/apply`, { nic_mode: nicMode, opt_profile: optProfile }),
  removeNodeOptimizations: (serverId: number) =>
    api.post<RemoveOptimizationsResponse>(`/proxy/${serverId}/system/optimizations/remove`),
  
  // Panel server statistics (CPU, RAM, Disk)
  getServerStats: () => api.get<PanelServerStats>('/system/stats'),
}

// Remnawave types
export interface RemnawaveSettings {
  api_url: string | null
  api_token: string | null
  cookie_secret: string | null
  enabled: boolean
  collection_interval: number
  ignored_user_ids: number[]
  anomaly_enabled: boolean
  anomaly_use_custom_bot: boolean
  anomaly_tg_bot_token: string | null
  anomaly_tg_chat_id: string | null
  anomaly_ignore_ip: number[]
  anomaly_ignore_hwid: number[]
  traffic_anomaly_enabled: boolean
  traffic_threshold_gb: number
  traffic_confirm_count: number
}

export interface IgnoredUser {
  user_id: number
  username: string | null
  status: string | null
  telegram_id: number | null
}

export interface IgnoredUsersResponse {
  ignored_users: IgnoredUser[]
  count: number
}

export interface RemnawaveApiNode {
  uuid: string
  name: string
  address: string
  is_connected: boolean
  is_disabled: boolean
  country_code: string
  users_online: number
}

export interface RemnawaveNodesResponse {
  nodes: RemnawaveApiNode[]
  error?: string
}

export interface RemnawaveCollectorStatus {
  running: boolean
  collecting: boolean
  collection_interval: number
  last_collect_time: string | null
  next_collect_in: number | null
  last_nodes_count: number
}

export interface RemnawaveCollectResponse {
  success: boolean
  collected_at: string | null
  nodes_count?: number
  error?: string
}

export interface RemnawaveSummary {
  unique_users: number
  unique_ips: number
  total_devices: number
}

export interface RemnawaveUser {
  email: number
  username: string | null
  status: string | null
  unique_ips: number
  device_count: number
}

export interface RemnawaveUserIp {
  source_ip: string
  last_seen: string | null
}

export interface RemnawaveHwidDevice {
  hwid: string
  user_uuid?: string
  username?: string | null
  platform: string | null
  os_version: string | null
  device_model: string | null
  user_agent?: string | null
  created_at: string | null
}

export interface RemnawaveUserDetails {
  email: number
  username: string | null
  status: string | null
  unique_ips: number
  ips: RemnawaveUserIp[]
  devices: RemnawaveHwidDevice[]
}

export interface RemnawaveAnomaly {
  type: 'ip_exceeds_limit' | 'hwid_exceeds_limit' | 'unknown_user_agent' | 'traffic_exceeds_limit' | 'invalid_device_data'
  severity: 'high' | 'medium' | 'low'
  email: number | null
  username: string | null
  status: string | null
  current: number | string
  limit: number | null
  detail: string
}

export interface RemnawaveAnomaliesResponse {
  anomalies: RemnawaveAnomaly[]
  summary: { ip_exceeds: number; hwid_exceeds: number; unknown_ua: number; traffic_exceeds: number; invalid_device: number; total: number }
  minutes: number
}

export interface RemnawaveCachedUser {
  email: number
  uuid: string | null
  username: string | null
  telegram_id: number | null
  status: string | null
}

export const remnawaveApi = {
  getSettings: () => api.get<RemnawaveSettings>('/remnawave/settings'),
  updateSettings: (data: Partial<RemnawaveSettings>) =>
    api.put<{ success: boolean; message: string }>('/remnawave/settings', data),
  testConnection: () =>
    api.post<{ success: boolean; api_reachable: boolean; error: string | null }>('/remnawave/settings/test'),

  getIgnoredUsers: () => api.get<IgnoredUsersResponse>('/remnawave/ignored-users'),
  addIgnoredUser: (userId: number) =>
    api.post<{ success: boolean; message?: string; error?: string }>('/remnawave/ignored-users', { user_id: userId }),
  removeIgnoredUser: (userId: number) =>
    api.delete<{ success: boolean; message?: string; error?: string }>(`/remnawave/ignored-users/${userId}`),

  getCollectorStatus: () => api.get<RemnawaveCollectorStatus>('/remnawave/status'),
  collectNow: () => api.post<RemnawaveCollectResponse>('/remnawave/collect'),

  getAnomalies: (minutes?: number) =>
    api.get<RemnawaveAnomaliesResponse>('/remnawave/anomalies', { params: { minutes } }),
  addAnomalyIgnore: (userId: number, listType: 'ip' | 'hwid' | 'all') =>
    api.post<{ success: boolean }>('/remnawave/anomalies/ignore', { user_id: userId, list_type: listType }),
  removeAnomalyIgnore: (userId: number, listType: 'ip' | 'hwid' | 'all') =>
    api.delete<{ success: boolean }>('/remnawave/anomalies/ignore', { data: { user_id: userId, list_type: listType } }),

  getIgnoreLists: () =>
    api.get<{ all: IgnoredUser[]; ip: IgnoredUser[]; hwid: IgnoredUser[] }>('/remnawave/ignore-lists'),
  removeFromIgnoreList: (listType: 'all' | 'ip' | 'hwid', userId: number) =>
    api.delete<{ success: boolean }>(`/remnawave/ignore-lists/${listType}/${userId}`),

  getNodes: () => api.get<RemnawaveNodesResponse>('/remnawave/nodes'),

  getDevices: (params?: { limit?: number; offset?: number; search?: string; platform?: string }) =>
    api.get<{ devices: RemnawaveHwidDevice[]; total: number; offset: number; limit: number }>('/remnawave/devices', { params }),
  getUserDevices: (userUuid: string) =>
    api.get<{ devices: RemnawaveHwidDevice[]; count: number }>(`/remnawave/devices/user/${userUuid}`),
  syncDevices: () =>
    api.post<{ success: boolean; error?: string }>('/remnawave/devices/sync'),

  getSummary: () =>
    api.get<RemnawaveSummary>('/remnawave/stats/summary'),
  getTopUsers: (params: { limit?: number; offset?: number; search?: string; status?: string; source_ip?: string; sort_by?: string; sort_dir?: string }) =>
    api.get<{ users: RemnawaveUser[]; total: number; offset: number; limit: number }>('/remnawave/stats/top-users', { params }),
  getUserStats: (email: number) =>
    api.get<RemnawaveUserDetails>(`/remnawave/stats/user/${email}`),
  clearStats: () =>
    api.delete<{ success: boolean; deleted: { xray_stats: number }; message: string }>('/remnawave/stats/clear'),
  clearUserIp: (email: number, sourceIp: string) =>
    api.delete<{ success: boolean; deleted_records: number; message: string }>(`/remnawave/stats/user/${email}/ips/${encodeURIComponent(sourceIp)}`),
  clearUserAllIps: (email: number) =>
    api.delete<{ success: boolean; deleted_records: number; message: string }>(`/remnawave/stats/user/${email}/ips`),

  getUsers: (params?: { search?: string; limit?: number }) =>
    api.get<{ count: number; users: RemnawaveCachedUser[] }>('/remnawave/users', { params }),
  refreshUserCache: () =>
    api.post<{ success: boolean; count: number; error: string | null }>('/remnawave/users/refresh'),
  getUserCacheStatus: () =>
    api.get<{ last_update: string | null; updating: boolean; update_interval: number }>('/remnawave/users/cache-status'),
}

// Server Alerts
export interface AlertSettingsData {
  enabled: boolean
  telegram_bot_token: string
  telegram_chat_id: string
  language: string
  check_interval: number
  alert_cooldown: number
  offline_enabled: boolean
  offline_fail_threshold: number
  offline_recovery_notify: boolean
  cpu_enabled: boolean
  cpu_critical_threshold: number
  cpu_spike_percent: number
  cpu_sustained_seconds: number
  cpu_min_value: number
  ram_enabled: boolean
  ram_critical_threshold: number
  ram_spike_percent: number
  ram_sustained_seconds: number
  ram_min_value: number
  network_enabled: boolean
  network_spike_percent: number
  network_drop_percent: number
  network_sustained_seconds: number
  network_min_bytes: number
  tcp_established_enabled: boolean
  tcp_established_spike_percent: number
  tcp_established_drop_percent: number
  tcp_established_sustained_seconds: number
  tcp_min_connections: number
  tcp_listen_enabled: boolean
  tcp_listen_spike_percent: number
  tcp_listen_sustained_seconds: number
  tcp_timewait_enabled: boolean
  tcp_timewait_spike_percent: number
  tcp_timewait_sustained_seconds: number
  tcp_closewait_enabled: boolean
  tcp_closewait_spike_percent: number
  tcp_closewait_sustained_seconds: number
  tcp_synsent_enabled: boolean
  tcp_synsent_spike_percent: number
  tcp_synsent_sustained_seconds: number
  tcp_synrecv_enabled: boolean
  tcp_synrecv_spike_percent: number
  tcp_synrecv_sustained_seconds: number
  tcp_finwait_enabled: boolean
  tcp_finwait_spike_percent: number
  tcp_finwait_sustained_seconds: number
  load_avg_enabled: boolean
  load_avg_threshold_offset: number
  load_avg_sustained_checks: number
  excluded_server_ids: number[]
  offline_excluded_server_ids: number[]
  cpu_excluded_server_ids: number[]
  ram_excluded_server_ids: number[]
  network_excluded_server_ids: number[]
  tcp_excluded_server_ids: number[]
  load_avg_excluded_server_ids: number[]
}

export interface AlertHistoryItem {
  id: number
  server_id: number
  server_name: string
  alert_type: string
  severity: string
  message: string
  details: string | null
  notified: boolean
  created_at: string | null
}

export interface AlertStatus {
  running: boolean
  last_check: string | null
  next_check_in: number | null
  monitored_servers: number
  active_conditions: Record<number, string[]>
}

export const alertsApi = {
  getSettings: () => api.get<AlertSettingsData>('/alerts/settings'),
  updateSettings: (data: Partial<AlertSettingsData>) =>
    api.put<AlertSettingsData>('/alerts/settings', data),
  testTelegram: (botToken: string, chatId: string) =>
    api.post<{ success: boolean; message?: string; error?: string }>('/alerts/test-telegram', {
      bot_token: botToken,
      chat_id: chatId,
    }),
  getStatus: () => api.get<AlertStatus>('/alerts/status'),
  getHistory: (params?: { server_id?: number; alert_type?: string; limit?: number; offset?: number }) =>
    api.get<{ items: AlertHistoryItem[]; total: number }>('/alerts/history', { params }),
  clearHistory: () => api.delete<{ deleted: number }>('/alerts/history'),
}

// Billing types
export interface BillingServerData {
  id: number
  name: string
  billing_type: 'monthly' | 'resource' | 'yandex_cloud'
  paid_until: string | null
  days_left: number | null
  monthly_cost: number | null
  account_balance: number | null
  balance_updated_at: string | null
  currency: string
  notes: string | null
  folder: string | null
  created_at: string | null
  updated_at: string | null
  yc_billing_account_id: string | null
  yc_balance_threshold: number | null
  yc_daily_cost: number | null
  yc_last_sync_at: string | null
  yc_last_error: string | null
  has_yc_token: boolean
}

export interface BillingSettingsData {
  enabled: boolean
  notify_days: number[]
  check_interval_minutes: number
}

export const billingApi = {
  getServers: () =>
    api.get<{ servers: BillingServerData[]; count: number }>('/billing/servers'),
  createServer: (data: {
    name: string
    billing_type: 'monthly' | 'resource' | 'yandex_cloud'
    paid_days?: number
    paid_until?: string
    monthly_cost?: number
    account_balance?: number
    currency?: string
    notes?: string
    folder?: string
    yc_oauth_token?: string
    yc_billing_account_id?: string
    yc_balance_threshold?: number
  }) => api.post<{ success: boolean; server: BillingServerData }>('/billing/servers', data),
  updateServer: (id: number, data: {
    name?: string
    billing_type?: string
    paid_until?: string
    monthly_cost?: number
    account_balance?: number
    currency?: string
    notes?: string
    folder?: string | null
    yc_oauth_token?: string
    yc_billing_account_id?: string
    yc_balance_threshold?: number
  }) => api.put<BillingServerData>(`/billing/servers/${id}`, data),
  deleteServer: (id: number) =>
    api.delete<{ success: boolean }>(`/billing/servers/${id}`),
  extendServer: (id: number, days: number) =>
    api.post<BillingServerData>(`/billing/servers/${id}/extend`, { days }),
  topupServer: (id: number, amount: number) =>
    api.post<BillingServerData>(`/billing/servers/${id}/topup`, { amount }),
  syncYc: (id: number) =>
    api.post<BillingServerData>(`/billing/servers/${id}/yc-sync`),
  moveToFolder: (serverIds: number[], folder: string | null) =>
    api.post<{ success: boolean; moved: number }>('/billing/servers/move-to-folder', { server_ids: serverIds, folder }),
  renameFolder: (oldName: string, newName: string) =>
    api.post<{ success: boolean; renamed: number }>('/billing/folders/rename', { old_name: oldName, new_name: newName }),
  deleteFolder: (folderName: string) =>
    api.delete<{ success: boolean; unfoldered: number }>(`/billing/folders/${encodeURIComponent(folderName)}`),
  getSettings: () =>
    api.get<BillingSettingsData>('/billing/settings'),
  updateSettings: (data: Partial<BillingSettingsData>) =>
    api.put<BillingSettingsData>('/billing/settings', data),
}

// Backup & Restore
export interface BackupInfo {
  filename: string
  size: number
  created_at: string
  version: string | null
}

export interface BackupStatus {
  state: 'idle' | 'creating' | 'restoring'
  filename: string | null
  error: string | null
  started_at: string | null
  completed_at: string | null
}

export const backupApi = {
  create: () =>
    api.post<{ success: boolean; message: string }>('/backup/create'),
  list: () =>
    api.get<{ backups: BackupInfo[]; count: number }>('/backup/list'),
  download: (filename: string) =>
    api.get(`/backup/${filename}/download`, { responseType: 'blob' }),
  delete: (filename: string) =>
    api.delete<{ success: boolean }>(`/backup/${filename}`),
  restore: (file: File) => {
    const formData = new FormData()
    formData.append('file', file)
    return api.post<{ success: boolean; message: string }>('/backup/restore', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 600000,
    })
  },
  getStatus: () =>
    api.get<BackupStatus>('/backup/status'),
}

// SSH Security
export interface SSHConfig {
  port: number
  permit_root_login: 'yes' | 'no' | 'prohibit-password'
  password_authentication: boolean
  pubkey_authentication: boolean
  permit_empty_passwords: boolean
  max_auth_tries: number
  login_grace_time: number
  client_alive_interval: number
  client_alive_count_max: number
  max_sessions: number
  max_startups: string
  allow_users: string[]
  x11_forwarding: boolean
}

export interface Fail2banConfig {
  installed: boolean
  enabled: boolean
  max_retry: number
  ban_time: number
  find_time: number
}

export interface Fail2banBannedIP {
  ip: string
  ban_time_remaining: number
}

export interface SSHKey {
  type: string
  fingerprint: string
  comment: string
  key_data: string
}

export interface SSHStatus {
  sshd_running: boolean
  sshd_port: number
  fail2ban_installed: boolean
  fail2ban_running: boolean
  fail2ban_banned_count: number
  auth_method: 'password' | 'key' | 'both' | 'none'
  authorized_keys_count: number
}

export interface SSHPresetData {
  ssh: Record<string, unknown>
  fail2ban: Record<string, unknown>
}

export interface SSHCustomPreset extends SSHPresetData {
  name: string
}

export interface SSHPresets {
  recommended: SSHPresetData
  maximum: SSHPresetData
  custom: SSHCustomPreset[]
}

export interface BulkSSHResult {
  server_id: number
  server_name: string
  success: boolean
  error?: string
  message?: string
  warnings?: string[]
}

export const sshSecurityApi = {
  getConfig: (serverId: number) =>
    api.get<{ config: SSHConfig }>(`/ssh-security/server/${serverId}/config`),
  updateConfig: (serverId: number, config: Partial<SSHConfig>) =>
    api.post<{ success: boolean; message: string; warnings: string[] }>(`/ssh-security/server/${serverId}/config`, config),
  testConfig: (serverId: number, config: Partial<SSHConfig>) =>
    api.post<{ valid: boolean; errors: string[] }>(`/ssh-security/server/${serverId}/config/test`, config),

  getFail2ban: (serverId: number) =>
    api.get<Fail2banConfig>(`/ssh-security/server/${serverId}/fail2ban/status`),
  updateFail2ban: (serverId: number, config: Partial<Fail2banConfig>) =>
    api.post<{ success: boolean; message: string }>(`/ssh-security/server/${serverId}/fail2ban/config`, config),
  getBanned: (serverId: number) =>
    api.get<{ count: number; ips: Fail2banBannedIP[] }>(`/ssh-security/server/${serverId}/fail2ban/banned`),
  unbanIp: (serverId: number, ip: string) =>
    api.post<{ success: boolean }>(`/ssh-security/server/${serverId}/fail2ban/unban`, { ip }),
  unbanAll: (serverId: number) =>
    api.post<{ success: boolean }>(`/ssh-security/server/${serverId}/fail2ban/unban-all`),

  getKeys: (serverId: number) =>
    api.get<{ user: string; count: number; keys: SSHKey[] }>(`/ssh-security/server/${serverId}/keys`),
  addKey: (serverId: number, publicKey: string, user: string = 'root') =>
    api.post<{ success: boolean; fingerprint: string }>(`/ssh-security/server/${serverId}/keys`, { public_key: publicKey, user }),
  removeKey: (serverId: number, fingerprint: string, user: string = 'root') =>
    api.delete<{ success: boolean }>(`/ssh-security/server/${serverId}/keys`, { data: { fingerprint, user } }),

  getStatus: (serverId: number) =>
    api.get<SSHStatus>(`/ssh-security/server/${serverId}/status`),

  bulkConfig: (serverIds: number[], config: Partial<SSHConfig>) =>
    api.post<{ results: BulkSSHResult[] }>('/ssh-security/bulk/config', { server_ids: serverIds, config }),
  bulkFail2ban: (serverIds: number[], config: Partial<Fail2banConfig>) =>
    api.post<{ results: BulkSSHResult[] }>('/ssh-security/bulk/fail2ban', { server_ids: serverIds, config }),
  bulkKeys: (serverIds: number[], publicKey: string, user: string = 'root') =>
    api.post<{ results: BulkSSHResult[] }>('/ssh-security/bulk/keys', { server_ids: serverIds, public_key: publicKey, user }),

  changePassword: (serverId: number, password: string, user: string = 'root') =>
    api.post<{ success: boolean; message: string }>(`/ssh-security/server/${serverId}/password`, { user, password }),
  bulkPassword: (serverIds: number[], password: string, user: string = 'root') =>
    api.post<{ results: BulkSSHResult[] }>('/ssh-security/bulk/password', { server_ids: serverIds, user, password }),

  getPresets: () =>
    api.get<SSHPresets>('/ssh-security/presets'),
  saveCustomPreset: (name: string, ssh: Record<string, unknown>, fail2ban: Record<string, unknown>) =>
    api.post<{ success: boolean; presets: SSHCustomPreset[] }>('/ssh-security/presets/custom', { name, ssh, fail2ban }),
  deleteCustomPreset: (name: string) =>
    api.delete<{ success: boolean; presets: SSHCustomPreset[] }>('/ssh-security/presets/custom', { data: { name } }),
}

// ==================== Infrastructure Tree ====================

export interface InfraProject {
  id: number
  name: string
  position: number
  server_ids: number[]
}

export interface InfraAccount {
  id: number
  name: string
  position: number
  projects: InfraProject[]
}

export interface InfraTree {
  accounts: InfraAccount[]
  unassigned_server_ids: number[]
}

export const infraApi = {
  getTree: () =>
    api.get<InfraTree>('/infra/tree'),
  createAccount: (name: string) =>
    api.post<{ success: boolean; account: InfraAccount }>('/infra/accounts', { name }),
  updateAccount: (id: number, name: string) =>
    api.put(`/infra/accounts/${id}`, { name }),
  deleteAccount: (id: number) =>
    api.delete(`/infra/accounts/${id}`),
  createProject: (accountId: number, name: string) =>
    api.post<{ success: boolean; project: InfraProject }>('/infra/projects', { account_id: accountId, name }),
  updateProject: (id: number, data: { name?: string; account_id?: number }) =>
    api.put(`/infra/projects/${id}`, data),
  deleteProject: (id: number) =>
    api.delete(`/infra/projects/${id}`),
  addServerToProject: (projectId: number, serverId: number) =>
    api.post(`/infra/projects/${projectId}/servers`, { server_id: serverId }),
  removeServerFromProject: (projectId: number, serverId: number) =>
    api.delete(`/infra/projects/${projectId}/servers/${serverId}`),
}

// ==================== Shared Notes ====================

export interface SharedTask {
  id: number
  text: string
  is_done: boolean
}

export const notesApi = {
  getContent: () =>
    api.get<{ content: string; version: number }>('/notes/content'),
  saveContent: (content: string, version: number) =>
    api.post<{ status: string; version: number; content?: string }>('/notes/content', { content, version }),
  getTasks: () =>
    api.get<{ tasks: SharedTask[] }>('/notes/tasks'),
  createTask: (text: string) =>
    api.post<{ success: boolean; tasks: SharedTask[] }>('/notes/tasks', { text }),
  toggleTask: (id: number, isDone: boolean) =>
    api.put<{ success: boolean; tasks: SharedTask[] }>(`/notes/tasks/${id}`, { is_done: isDone }),
  deleteTask: (id: number) =>
    api.delete<{ success: boolean; tasks: SharedTask[] }>(`/notes/tasks/${id}`),
  getStreamUrl: () => '/api/notes/stream',
}

// ==================== Wildcard SSL ====================

export interface WildcardCertificate {
  id: number
  domain: string
  base_domain: string
  expiry_date: string | null
  days_left: number | null
  expired: boolean
  issued_at: string | null
  last_renewed: string | null
  auto_renew: boolean
}

export interface WildcardSSLSettings {
  cloudflare_api_token: string
  cloudflare_api_token_set: boolean
  email: string
  auto_renew_enabled: boolean
  renew_days_before: number
}

export interface WildcardServerConfig {
  server_id: number
  server_name: string
  wildcard_ssl_enabled: boolean
  wildcard_ssl_deploy_path: string
  wildcard_ssl_reload_cmd: string
  wildcard_ssl_fullchain_name: string
  wildcard_ssl_privkey_name: string
  wildcard_ssl_custom_path_enabled: boolean
  wildcard_ssl_custom_fullchain_path: string
  wildcard_ssl_custom_privkey_path: string
}

export interface WildcardDeployResult {
  success: boolean
  message: string
  server_id?: number
  server_name?: string
  reload_result?: { exit_code: number; stdout: string; stderr: string } | null
}

export const wildcardSSLApi = {
  getCertificates: () =>
    api.get<{ certificates: WildcardCertificate[] }>('/wildcard-ssl/certificates'),
  getCertificate: (id: number) =>
    api.get<WildcardCertificate>(`/wildcard-ssl/certificates/${id}`),
  issueCertificate: (data: { domain: string; email?: string }) =>
    api.post<{ success: boolean; message: string }>('/wildcard-ssl/certificates/issue', data),
  getIssueStatus: () =>
    api.get<{ in_progress: boolean; last_result: string | null; last_error: string | null; output: string | null }>('/wildcard-ssl/issue-status'),
  renewCertificate: (id: number) =>
    api.post<{ success: boolean; message: string }>(`/wildcard-ssl/certificates/${id}/renew`),
  deleteCertificate: (id: number) =>
    api.delete(`/wildcard-ssl/certificates/${id}`),
  deployToAll: (id: number) =>
    api.post<{ results: WildcardDeployResult[] }>(`/wildcard-ssl/certificates/${id}/deploy`),
  deployToServer: (id: number, serverId: number) =>
    api.post<WildcardDeployResult>(`/wildcard-ssl/certificates/${id}/deploy/${serverId}`),
  getSettings: () =>
    api.get<WildcardSSLSettings>('/wildcard-ssl/settings'),
  getTokenRaw: () =>
    api.get<{ cloudflare_api_token: string }>('/wildcard-ssl/settings/token'),
  updateSettings: (data: Partial<WildcardSSLSettings>) =>
    api.put('/wildcard-ssl/settings', data),
  getServers: () =>
    api.get<{ servers: WildcardServerConfig[] }>('/wildcard-ssl/servers'),
  updateServer: (serverId: number, data: Partial<WildcardServerConfig>) =>
    api.put(`/wildcard-ssl/servers/${serverId}`, data),
}

// ==================== HAProxy Config Profiles ====================

export interface HAProxyConfigProfile {
  id: number
  name: string
  description: string | null
  config_content: string
  position: number
  linked_servers_count: number
  synced_servers_count: number
  total_net_rx: number
  total_net_tx: number
  created_at: string | null
  updated_at: string | null
}

export interface HAProxyProfileServer {
  server_id: number
  server_name: string
  sync_status: 'synced' | 'pending' | 'failed' | null
  config_hash: string | null
  is_synced: boolean
  last_sync_at: string | null
}

export interface HAProxyProfileDetail extends Omit<HAProxyConfigProfile, 'linked_servers_count' | 'synced_servers_count'> {
  servers: HAProxyProfileServer[]
}

export interface HAProxySyncResult {
  server_id: number
  server_name: string
  success: boolean
  message: string
}

export interface HAProxySyncLogEntry {
  id: number
  server_id: number
  server_name: string
  status: string
  message: string | null
  config_hash: string | null
  created_at: string | null
}

export interface HAProxyAvailableServer {
  id: number
  name: string
  url: string
  active_profile_id: number | null
  sync_status: string | null
}

export interface HAProxyServerStatus {
  server_id: number
  server_name: string
  server_url: string
  sync_status: 'synced' | 'pending' | 'failed' | null
  config_hash: string | null
  last_sync_at: string | null
  haproxy_running: boolean | null
  metrics: {
    cpu: number | null
    ram: number | null
    net_rx: number | null
    net_tx: number | null
    la1: number | null
    cores: number | null
  } | null
}

export interface BackendServer {
  name: string
  address: string
  port: number
  weight?: number
  maxconn?: number
  check?: boolean
  inter?: string
  fall?: number
  rise?: number
  send_proxy?: boolean
  send_proxy_v2?: boolean
  backup?: boolean
  slowstart?: string
  on_marked_down?: string | null
  on_marked_up?: string | null
  disabled?: boolean
}

export interface BalancerOptions {
  algorithm: string
  algorithm_param?: string | null
  hash_type?: string | null
  health_check_type?: string | null
  httpchk_method?: string | null
  httpchk_uri?: string | null
  httpchk_expect?: string | null
  sticky_type?: string | null
  cookie_name?: string | null
  cookie_options?: string | null
  stick_table_type?: string | null
  stick_table_size?: string | null
  stick_table_expire?: string | null
  retries?: number
  redispatch?: boolean
  allbackups?: boolean
  fullconn?: number | null
  timeout_queue?: string | null
}

export interface HAProxyProfileRule {
  name: string
  rule_type: 'tcp' | 'https'
  listen_port: number
  target_ip: string
  target_port: number
  cert_domain?: string | null
  target_ssl?: boolean
  send_proxy: boolean
  use_wildcard?: boolean
  is_balancer?: boolean
  servers?: BackendServer[]
  balancer_options?: BalancerOptions | null
}

export const haproxyProfilesApi = {
  getServerCores: (profileId: number, addresses: string[]) =>
    api.post<Record<string, number>>('/haproxy-profiles/server-cores', { profile_id: profileId, addresses }),
  getProfiles: () =>
    api.get<HAProxyConfigProfile[]>('/haproxy-profiles/'),
  createProfile: (data: { name: string; description?: string; config_content?: string }) =>
    api.post<HAProxyConfigProfile>('/haproxy-profiles/', data),
  getProfile: (id: number) =>
    api.get<HAProxyProfileDetail>(`/haproxy-profiles/${id}`),
  updateProfile: (id: number, data: { name?: string; description?: string; config_content?: string }) =>
    api.put<{ success: boolean; id: number }>(`/haproxy-profiles/${id}`, data),
  deleteProfile: (id: number) =>
    api.delete(`/haproxy-profiles/${id}`),
  reorderProfiles: (profile_ids: number[]) =>
    api.post('/haproxy-profiles/reorder', { profile_ids }),
  linkServer: (profileId: number, serverId: number) =>
    api.post(`/haproxy-profiles/${profileId}/servers/${serverId}`),
  unlinkServer: (profileId: number, serverId: number) =>
    api.delete(`/haproxy-profiles/${profileId}/servers/${serverId}`),
  syncAll: (profileId: number) =>
    api.post<{ results: HAProxySyncResult[] }>(`/haproxy-profiles/${profileId}/sync`),
  syncOne: (profileId: number, serverId: number) =>
    api.post<HAProxySyncResult>(`/haproxy-profiles/${profileId}/sync/${serverId}`),
  getSyncLog: (profileId: number, limit?: number) =>
    api.get<HAProxySyncLogEntry[]>(`/haproxy-profiles/${profileId}/log`, { params: { limit: limit || 50 } }),
  getAvailableServers: () =>
    api.get<HAProxyAvailableServer[]>('/haproxy-profiles/available-servers'),
  getServersStatus: (profileId: number) =>
    api.get<HAProxyServerStatus[]>(`/haproxy-profiles/${profileId}/servers-status`),
  getRules: (profileId: number) =>
    api.get<HAProxyProfileRule[]>(`/haproxy-profiles/${profileId}/rules`),
  addRule: (profileId: number, rule: Omit<HAProxyProfileRule, 'name'> & { name: string }) =>
    api.post<{ success: boolean; rules: HAProxyProfileRule[] }>(`/haproxy-profiles/${profileId}/rules`, rule),
  updateRule: (profileId: number, ruleName: string, rule: HAProxyProfileRule) =>
    api.put<{ success: boolean; rules: HAProxyProfileRule[] }>(`/haproxy-profiles/${profileId}/rules/${ruleName}`, rule),
  deleteRule: (profileId: number, ruleName: string) =>
    api.delete<{ success: boolean; rules: HAProxyProfileRule[] }>(`/haproxy-profiles/${profileId}/rules/${ruleName}`),
  regenerateConfig: (profileId: number) =>
    api.post<{ config_content: string }>(`/haproxy-profiles/${profileId}/regenerate-config`),
}

// ==================== Torrent Blocker ====================

export interface TorrentBlockerSettings {
  enabled: boolean
  poll_interval_minutes: number
  ban_duration_minutes: number
  excluded_server_ids: number[]
}

export interface TorrentBlockerStatus {
  running: boolean
  enabled: boolean
  last_poll_at: string | null
  last_poll_status: string | null
  last_poll_message: string | null
  last_ips_banned: number
  last_reports_processed: number
  total_ips_banned: number
  total_cycles: number
}

export interface TorrentBlockerStats {
  stats: {
    distinctNodes: number
    distinctUsers: number
    totalReports: number
    reportsLast24Hours: number
  }
  topUsers: { uuid: string; username: string; total: number }[]
  topNodes: { uuid: string; name: string; countryCode: string; total: number }[]
}

export interface TorrentBlockerReport {
  id: number
  user: { uuid: string; username: string }
  node: { uuid: string; name: string; countryCode: string }
  report: {
    actionReport: {
      blocked: boolean
      ip: string
      blockDuration: number
      willUnblockAt: string
      processedAt: string
    }
    xrayReport: {
      source: string
      destination: string
      protocol: string | null
      network: string
    }
  }
  createdAt: string
}

export const torrentBlockerApi = {
  getSettings: () =>
    api.get<TorrentBlockerSettings>('/torrent-blocker/settings'),
  updateSettings: (data: Partial<TorrentBlockerSettings>) =>
    api.put<TorrentBlockerSettings>('/torrent-blocker/settings', data),
  getStatus: () =>
    api.get<TorrentBlockerStatus>('/torrent-blocker/status'),
  getStats: () =>
    api.get<TorrentBlockerStats>('/torrent-blocker/stats'),
  getReports: (start = 0, size = 50) =>
    api.get<{ records: TorrentBlockerReport[]; total: number }>('/torrent-blocker/reports', { params: { start, size } }),
  pollNow: () =>
    api.post('/torrent-blocker/poll-now'),
  truncate: () =>
    api.delete('/torrent-blocker/truncate'),
}

export default api
