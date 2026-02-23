import axios, { AxiosError, AxiosRequestConfig, AxiosResponse } from 'axios'

const api = axios.create({
  baseURL: '/api',
  withCredentials: true,
  headers: {
    'Content-Type': 'application/json',
  },
})

api.interceptors.response.use(
  (response) => response,
  (error: AxiosError) => {
    if (error.response?.status === 401) {
      const currentPath = window.location.pathname
      const uid = currentPath.split('/')[1]
      if (uid && !currentPath.includes('/login')) {
        window.location.href = `/${uid}/login`
      }
    }
    return Promise.reject(error)
  }
)

// GET request deduplication: if the same GET is already in-flight, reuse its promise
const inflightGets = new Map<string, Promise<AxiosResponse>>()
const originalGet = api.get.bind(api)
api.get = function <T = unknown>(url: string, config?: AxiosRequestConfig): Promise<AxiosResponse<T>> {
  const key = url + (config?.params ? ':' + JSON.stringify(config.params) : '')
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
  api_key?: string
  position: number
  is_active: boolean
  folder?: string | null
  last_seen?: string | null
  last_error?: string | null
  error_code?: number | null
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
  create: (data: { name: string; url: string; api_key: string }) => 
    api.post<{ success: boolean; server: Server }>('/servers', data),
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

export const settingsApi = {
  getAll: () => api.get<{ settings: Record<string, string> }>('/settings'),
  get: (key: string) => api.get<{ key: string; value: string }>(`/settings/${key}`),
  set: (key: string, value: string) => api.put(`/settings/${key}`, { value }),
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
  torrent_behavior_threshold: number
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

export interface GlobalThresholdServerResult {
  server_id: number
  server_name: string
  success: boolean
  error?: string
}

export interface TorrentBlockerStatus {
  server_id: number
  server_name: string
  enabled: boolean
  running: boolean
  started_at: string | null
  total_blocked: number
  tag_blocks: number
  behavior_blocks: number
  active_blocks: number
  active_ips: string[]
  last_block_time: string | null
  behavior_threshold: number
  error?: string
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
  
  // Torrent blocker
  getTorrentBlockerStatus: () =>
    api.get<{ servers: TorrentBlockerStatus[] }>('/blocklist/torrent-blocker'),
  enableTorrentBlocker: (serverId: number) =>
    api.post<{ success: boolean; message: string }>(`/blocklist/torrent-blocker/${serverId}/enable`),
  disableTorrentBlocker: (serverId: number) =>
    api.post<{ success: boolean; message: string }>(`/blocklist/torrent-blocker/${serverId}/disable`),
  updateTorrentBlockerSettings: (serverId: number, data: { behavior_threshold: number }) =>
    api.post<{ success: boolean; behavior_threshold: number }>(`/blocklist/torrent-blocker/${serverId}/settings`, data),
  updateGlobalTorrentSettings: (data: { behavior_threshold: number }) =>
    api.post<{ success: boolean; behavior_threshold: number; servers: GlobalThresholdServerResult[] }>(
      '/blocklist/torrent-blocker/global-settings', data
    ),

  // Torrent blocker whitelist
  getTorrentWhitelist: () =>
    api.get<{ whitelist: string[] }>('/blocklist/torrent-blocker/whitelist'),
  updateTorrentWhitelist: (whitelist: string[]) =>
    api.put<{ success: boolean; whitelist: string[]; servers: GlobalThresholdServerResult[] }>(
      '/blocklist/torrent-blocker/whitelist', { whitelist }
    ),
}

export interface NodeOptimizationsInfo {
  installed: boolean
  version: string | null
}

export interface NodeVersionInfo {
  id: number
  name: string
  url: string
  version: string | null
  status: 'online' | 'offline'
  optimizations?: NodeOptimizationsInfo
}

export interface VersionInfo {
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
  nodes: NodeVersionInfo[]
  update_in_progress: boolean
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

export interface OptimizationsNodeInfo {
  id: number
  name: string
  installed: boolean
  version: string | null
  status: 'online' | 'offline'
  update_available: boolean
}

export interface OptimizationsVersionInfo {
  latest_version: string | null
  nodes: OptimizationsNodeInfo[]
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
  
  // HAProxy rules
  createHAProxyRule: (serverIds: number[], rule: {
    name: string
    rule_type: 'tcp' | 'https'
    listen_port: number
    target_ip: string
    target_port: number
    cert_domain?: string
    target_ssl?: boolean
    send_proxy?: boolean
  }) => api.post<BulkResult[]>('/bulk/haproxy/rules', { server_ids: serverIds, ...rule }),
  
  deleteHAProxyRule: (serverIds: number[], listenPort: number, targetIp: string, targetPort: number) =>
    api.delete<BulkResult[]>('/bulk/haproxy/rules', { 
      data: { server_ids: serverIds, listen_port: listenPort, target_ip: targetIp, target_port: targetPort }
    }),
  
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

  applyHAProxyConfig: (serverIds: number[], configContent: string, reloadAfter: boolean = true) =>
    api.post<BulkResult[]>('/bulk/haproxy/config', {
      server_ids: serverIds, config_content: configContent, reload_after: reloadAfter
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
  getVersion: () => api.get<VersionInfo>('/system/version', { timeout: 30000 }),
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
  getOptimizationsVersion: () => api.get<OptimizationsVersionInfo>('/system/optimizations/version'),
  applyNodeOptimizations: (serverId: number) => 
    api.post<ApplyOptimizationsResponse>(`/proxy/${serverId}/system/optimizations/apply`),
  
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
  // Retention settings (days)
  visit_stats_retention_days: number
  ip_stats_retention_days: number
  ip_destination_retention_days: number
  hourly_stats_retention_days: number
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

export interface RemnawaveNode {
  id: number
  server_id: number
  server_name: string
  enabled: boolean
  last_collected: string | null
  last_error: string | null
}

export interface RemnawaveServerInfo {
  id: number
  name: string
  is_active: boolean
  has_xray_node: boolean
  is_node: boolean
  node_enabled: boolean
}

export interface RemnawaveNodesResponse {
  nodes: RemnawaveNode[]
  all_servers: RemnawaveServerInfo[]
}

export interface RemnawaveCollectorStatus {
  running: boolean
  collecting: boolean
  collection_interval: number
  last_collect_time: string | null
  next_collect_in: number | null
}

export interface RemnawaveCollectResponse {
  success: boolean
  collected_at: string | null
  nodes_count: number
  error?: string
}

export interface RemnawaveSyncResponse {
  success: boolean
  added: number
  removed: number
  total: number
}

export interface RemnawaveSummary {
  period: string
  total_visits: number
  unique_users: number
  unique_destinations: number
}

export interface RemnawaveDestination {
  destination: string
  visits: number
}

export interface RemnawaveUser {
  email: number
  username: string | null
  status: string | null
  total_visits: number
  unique_sites: number
  unique_ips: number
  infrastructure_ips: number
}

export interface RemnawaveUserDestination {
  destination: string
  visits: number
  first_seen: string | null
  last_seen: string | null
}

export interface RemnawaveUserIpServer {
  server_id: number
  server_name: string
  count: number
}

export interface RemnawaveUserIp {
  source_ip: string
  servers: RemnawaveUserIpServer[]
  total_count: number
  first_seen: string | null
  last_seen: string | null
  asn?: string | null
  prefix?: string | null
}

export interface RemnawaveUserDetails {
  email: number
  username: string | null
  status: string | null
  period: string
  total_visits: number
  unique_ips: number
  unique_client_ips: number
  destinations: RemnawaveUserDestination[]
  ips: RemnawaveUserIp[]
  client_ips: RemnawaveUserIp[]
  infrastructure_ips: RemnawaveUserIp[]
}

export interface RemnawaveInfrastructureAddress {
  id: number
  address: string
  resolved_ips: string | null  // JSON array of resolved IPs
  last_resolved: string | null
  description: string | null
  created_at: string | null
}

export interface RemnawaveExcludedDestination {
  id: number
  destination: string
  description: string | null
  created_at: string | null
}

export interface RemnawaveTimelinePoint {
  timestamp: string
  visits: number
  unique_users: number
  unique_destinations: number
}

export interface RemnawaveDestinationUser {
  email: number
  username: string | null
  status: string | null
  visits: number
  percentage: number
  first_seen: string | null
  last_seen: string | null
}

export interface RemnawaveDestinationUsers {
  destination: string
  period: string
  total_visits: number
  users: RemnawaveDestinationUser[]
}

export interface RemnawaveIpDestination {
  destination: string
  connections: number
  percentage: number
  first_seen: string | null
  last_seen: string | null
}

export interface RemnawaveIpDestinations {
  source_ip: string
  email: number
  period: string
  total_connections: number
  destinations: RemnawaveIpDestination[]
}

export interface RemnawaveCachedUser {
  email: number
  uuid: string | null
  username: string | null
  telegram_id: number | null
  status: string | null
}

// Full user info with all Remnawave Panel data
export interface RemnawaveUserFullInfo {
  email: number
  uuid: string | null
  short_uuid: string | null
  username: string | null
  telegram_id: number | null
  status: string | null
  // Subscription info
  expire_at: string | null
  subscription_url: string | null
  sub_revoked_at: string | null
  sub_last_user_agent: string | null
  sub_last_opened_at: string | null
  // Traffic limits
  traffic_limit_bytes: number | null
  traffic_limit_strategy: string | null  // NO_RESET, DAY, WEEK, MONTH
  last_traffic_reset_at: string | null
  // Traffic usage
  used_traffic_bytes: number | null
  lifetime_used_traffic_bytes: number | null
  online_at: string | null
  first_connected_at: string | null
  last_connected_node_uuid: string | null
  // Device limit
  hwid_device_limit: number | null
  // Additional info
  user_email: string | null
  description: string | null
  tag: string | null
  created_at: string | null
  updated_at: string | null
  // Extra data from live API
  active_internal_squads?: Array<{ uuid: string; name: string }>
  subscription_history?: {
    total: number
    records: Array<{
      id: number
      userUuid: string
      requestAt: string
      requestIp: string | null
      userAgent: string | null
    }>
  } | null
  bandwidth_stats?: {
    categories?: string[]  // Date labels (e.g. "2024-01-15")
    sparklineData?: number[]  // Total bytes per day
    topNodes?: Array<{
      uuid: string
      color: string
      name: string
      countryCode: string
      total: number
    }>
    series?: Array<{
      uuid: string
      name: string
      color: string
      countryCode: string
      total: number
      data: number[]  // Bytes per day for this node
    }>
  } | null
  hwid_devices?: {
    total?: number
    devices?: Array<{
      hwid: string
      userUuid: string
      platform: string | null
      osVersion: string | null
      deviceModel: string | null
      userAgent: string | null
      createdAt: string
      updatedAt: string
    }>
  } | null
}

export const remnawaveApi = {
  // Settings
  getSettings: () => api.get<RemnawaveSettings>('/remnawave/settings'),
  updateSettings: (data: Partial<RemnawaveSettings>) => 
    api.put<{ success: boolean; message: string }>('/remnawave/settings', data),
  testConnection: () => 
    api.post<{ success: boolean; api_reachable: boolean; error: string | null }>('/remnawave/settings/test'),
  
  // Ignored users
  getIgnoredUsers: () =>
    api.get<IgnoredUsersResponse>('/remnawave/ignored-users'),
  addIgnoredUser: (userId: number) =>
    api.post<{ success: boolean; message?: string; error?: string; user?: IgnoredUser }>('/remnawave/ignored-users', { user_id: userId }),
  removeIgnoredUser: (userId: number) =>
    api.delete<{ success: boolean; message?: string; error?: string }>(`/remnawave/ignored-users/${userId}`),
  
  // Infrastructure addresses
  getInfrastructureAddresses: () => 
    api.get<{ addresses: RemnawaveInfrastructureAddress[] }>('/remnawave/infrastructure-ips'),
  addInfrastructureAddress: (address: string, description?: string) =>
    api.post<{ success: boolean; address: RemnawaveInfrastructureAddress }>('/remnawave/infrastructure-ips', { address, description }),
  deleteInfrastructureAddress: (id: number) =>
    api.delete<{ success: boolean; message: string }>(`/remnawave/infrastructure-ips/${id}`),
  resolveInfrastructureAddresses: () =>
    api.post<{ success: boolean; total: number; updated: number }>('/remnawave/infrastructure-ips/resolve'),
  rescanInfrastructureIps: () =>
    api.post<{ success: boolean; infrastructure_ips_count: number; total_unique_ips_scanned: number; updated_to_infrastructure: number; updated_to_client: number }>('/remnawave/infrastructure-ips/rescan'),
  
  // Excluded destinations
  getExcludedDestinations: () =>
    api.get<{ destinations: RemnawaveExcludedDestination[] }>('/remnawave/excluded-destinations'),
  addExcludedDestination: (destination: string, description?: string) =>
    api.post<{ success: boolean; destination: RemnawaveExcludedDestination }>('/remnawave/excluded-destinations', { destination, description }),
  deleteExcludedDestination: (id: number) =>
    api.delete<{ success: boolean; message: string }>(`/remnawave/excluded-destinations/${id}`),
  
  // Collector status & control
  getCollectorStatus: () => api.get<RemnawaveCollectorStatus>('/remnawave/status'),
  collectNow: () => api.post<RemnawaveCollectResponse>('/remnawave/collect'),
  
  // Nodes
  getNodes: () => api.get<RemnawaveNodesResponse>('/remnawave/nodes'),
  addNode: (serverId: number) => 
    api.post<{ success: boolean; message: string }>('/remnawave/nodes', { server_id: serverId }),
  removeNode: (serverId: number) => 
    api.delete<{ success: boolean; message: string }>(`/remnawave/nodes/${serverId}`),
  updateNode: (serverId: number, enabled: boolean) =>
    api.put<{ success: boolean; message: string }>(`/remnawave/nodes/${serverId}?enabled=${enabled}`),
  syncNodes: (serverIds: number[]) =>
    api.post<RemnawaveSyncResponse>('/remnawave/nodes/sync', { server_ids: serverIds }),
  
  // Stats
  getStatsBatch: (params: {
    period: string
    dest_limit?: number
    users_limit?: number
    search?: string
  }) => api.get<{
    summary: RemnawaveSummary
    destinations: RemnawaveDestination[]
    users: { period: string; users: RemnawaveUser[]; total: number; offset: number; limit: number }
  }>('/remnawave/stats/batch', { params }),
  getSummary: (period: string, serverIds?: number[]) =>
    api.get<RemnawaveSummary>('/remnawave/stats/summary', { 
      params: { period, server_ids: serverIds?.join(',') } 
    }),
  getTopDestinations: (params: { 
    period: string
    limit?: number
    email?: number
    server_id?: number 
  }) => api.get<{ period: string; destinations: RemnawaveDestination[] }>('/remnawave/stats/top-destinations', { params }),
  getTopUsers: (params: { 
    period: string
    limit?: number
    offset?: number
    server_id?: number
    search?: string
  }) => api.get<{ period: string; users: RemnawaveUser[]; total: number; offset: number; limit: number }>('/remnawave/stats/top-users', { params }),
  getUserStats: (email: number, period: string) =>
    api.get<RemnawaveUserDetails>(`/remnawave/stats/user/${email}`, { params: { period } }),
  getDestinationUsers: (destination: string, period: string, limit?: number) =>
    api.get<RemnawaveDestinationUsers>('/remnawave/stats/destination/users', { 
      params: { destination, period, limit } 
    }),
  getIpDestinations: (sourceIp: string, email: number, period: string, limit?: number) =>
    api.get<RemnawaveIpDestinations>('/remnawave/stats/ip/destinations', {
      params: { source_ip: sourceIp, email, period, limit }
    }),
  getTimeline: (params: { 
    period: string
    email?: number
    server_id?: number 
  }) => api.get<{ period: string; data: RemnawaveTimelinePoint[] }>('/remnawave/stats/timeline', { params }),
  
  // Users cache
  getUsers: (params?: { search?: string; limit?: number }) =>
    api.get<{ count: number; users: RemnawaveCachedUser[] }>('/remnawave/users', { params }),
  refreshUserCache: () =>
    api.post<{ success: boolean; count: number; error: string | null }>('/remnawave/users/refresh'),
  getUserCacheStatus: () =>
    api.get<{ last_update: string | null; updating: boolean; update_interval: number }>('/remnawave/users/cache-status'),
  
  // Full user info (cached)
  getUserFullInfo: (email: number) =>
    api.get<RemnawaveUserFullInfo>(`/remnawave/user/${email}/full`),
  
  // Live user info (fetches fresh data from Remnawave API)
  getUserLiveInfo: (email: number) =>
    api.get<RemnawaveUserFullInfo>(`/remnawave/user/${email}/live`),
  
  // DB info
  getDbInfo: () =>
    api.get<{
      tables: {
        xray_stats: { count: number; first_seen: string | null; last_seen: string | null; size_bytes?: number | null }
        xray_hourly_stats: { count: number; first_hour: string | null; last_hour: string | null; size_bytes?: number | null }
        remnawave_user_cache: { count: number; size_bytes?: number | null }
      }
      total_size_bytes?: number | null
    }>('/remnawave/stats/db-info'),
  
  // Clear all stats
  clearStats: () =>
    api.delete<{
      success: boolean
      deleted: { xray_stats: number; hourly_stats: number }
      message: string
    }>('/remnawave/stats/clear'),
  
  // Clear user IPs
  clearUserIp: (email: number, sourceIp: string) =>
    api.delete<{
      success: boolean
      email: number
      source_ip: string
      deleted_ip_records: number
      deleted_destination_records: number
      message: string
    }>(`/remnawave/stats/user/${email}/ips/${encodeURIComponent(sourceIp)}`),
  
  clearUserAllIps: (email: number) =>
    api.delete<{
      success: boolean
      email: number
      deleted_ip_records: number
      deleted_destination_records: number
      message: string
    }>(`/remnawave/stats/user/${email}/ips`),
  
  // Clear all client IPs globally
  clearAllClientIps: () =>
    api.delete<{
      success: boolean
      deleted_records: number
      message: string
    }>('/remnawave/stats/client-ips/clear'),
  
  // Export
  createExport: (settings: {
    period: string
    include_user_id: boolean
    include_username: boolean
    include_status: boolean
    include_telegram_id: boolean
    include_destinations: boolean
    include_visits_count: boolean
    include_first_seen: boolean
    include_last_seen: boolean
    include_client_ips: boolean
    include_infra_ips: boolean
    include_traffic: boolean
  }) => api.post<{ success: boolean; export_id: number; filename: string; status: string }>('/remnawave/export/create', settings),
  
  listExports: () => api.get<{ exports: Array<{
    id: number
    filename: string
    format: string
    status: string
    file_size: number | null
    rows_count: number | null
    error_message: string | null
    created_at: string | null
    completed_at: string | null
  }> }>('/remnawave/export/list'),
  
  downloadExport: (exportId: number) => api.get(`/remnawave/export/${exportId}/download`, { responseType: 'blob' }),
  
  deleteExport: (exportId: number) => api.delete<{ success: boolean; message: string }>(`/remnawave/export/${exportId}`),
  
  // Traffic Analyzer
  getAnalyzerSettings: () => api.get<{
    enabled: boolean
    check_interval_minutes: number
    traffic_limit_gb: number
    ip_limit_multiplier: number
    check_hwid_anomalies: boolean
    telegram_bot_token: string | null
    telegram_chat_id: string | null
    last_check_at: string | null
    last_error: string | null
  }>('/remnawave/analyzer/settings'),
  
  updateAnalyzerSettings: (data: {
    enabled?: boolean
    check_interval_minutes?: number
    traffic_limit_gb?: number
    ip_limit_multiplier?: number
    check_hwid_anomalies?: boolean
    telegram_bot_token?: string
    telegram_chat_id?: string
  }) => api.put<{ success: boolean; message: string }>('/remnawave/analyzer/settings', data),
  
  getAnalyzerStatus: () => api.get<{
    running: boolean
    analyzing: boolean
    check_interval: number
    last_check_time: string | null
    next_check_in: number | null
  }>('/remnawave/analyzer/status'),
  
  runAnalyzerCheck: () => api.post<{
    success: boolean
    error?: string
    analyzed_users: number
    anomalies_found: number
  }>('/remnawave/analyzer/check'),
  
  testTelegram: (botToken: string, chatId: string) => api.post<{
    success: boolean
    message?: string
    error?: string
  }>('/remnawave/analyzer/test-telegram', { bot_token: botToken, chat_id: chatId }),
  
  getAnomalies: (params?: { limit?: number; offset?: number; anomaly_type?: string; resolved?: boolean }) =>
    api.get<{
      total: number
      offset: number
      limit: number
      anomalies: Array<{
        id: number
        user_email: number
        username: string | null
        telegram_id: number | null
        anomaly_type: string
        severity: string
        details: {
          consumed_gb?: number
          period_minutes?: number
          limit_gb?: number
          exceeded_by_gb?: number
          unique_ips?: number
          unique_asns?: number
          effective_count?: number
          device_limit?: number
          ip_limit?: number
          exceeded_by?: number
          asn_groups?: Array<{
            asn: string | null
            prefix: string | null
            ips: string[]
            count: number
            visits: number
          }>
          total_devices?: number
          suspicious_count?: number
          suspicious_devices?: Array<{
            hwid: string
            user_agent: string
            issues: string[]
          }>
        } | null
        notified: boolean
        resolved: boolean
        created_at: string | null
      }>
    }>('/remnawave/analyzer/anomalies', { params }),
  
  resolveAnomaly: (anomalyId: number) => api.put<{ success: boolean; message: string }>(`/remnawave/analyzer/anomalies/${anomalyId}/resolve`),
  
  deleteAnomaly: (anomalyId: number) => api.delete<{ success: boolean; message: string }>(`/remnawave/analyzer/anomalies/${anomalyId}`),
  
  deleteAllAnomalies: () => api.delete<{
    success: boolean
    deleted: number
    message: string
  }>('/remnawave/analyzer/anomalies/all'),
  
  clearOldAnomalies: (days: number) => api.delete<{
    success: boolean
    deleted: number
    message: string
  }>('/remnawave/analyzer/anomalies/clear', { params: { days } }),
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
  ram_enabled: boolean
  ram_critical_threshold: number
  ram_spike_percent: number
  ram_sustained_seconds: number
  network_enabled: boolean
  network_spike_percent: number
  network_drop_percent: number
  network_sustained_seconds: number
  tcp_established_enabled: boolean
  tcp_established_spike_percent: number
  tcp_established_drop_percent: number
  tcp_established_sustained_seconds: number
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
  billing_type: 'monthly' | 'resource'
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
    billing_type: 'monthly' | 'resource'
    paid_days?: number
    monthly_cost?: number
    account_balance?: number
    currency?: string
    notes?: string
    folder?: string
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
  }) => api.put<BillingServerData>(`/billing/servers/${id}`, data),
  deleteServer: (id: number) =>
    api.delete<{ success: boolean }>(`/billing/servers/${id}`),
  extendServer: (id: number, days: number) =>
    api.post<BillingServerData>(`/billing/servers/${id}/extend`, { days }),
  topupServer: (id: number, amount: number) =>
    api.post<BillingServerData>(`/billing/servers/${id}/topup`, { amount }),
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

export default api
