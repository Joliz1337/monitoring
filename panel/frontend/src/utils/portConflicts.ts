import type { DnatRuleData, PortRange } from '../api/client'

// Правило DNAT стоит первым в nat/PREROUTING и ловит порт на любом адресе сервера:
// HAProxy на том же TCP-порту остаётся запущенным, но соединений не получает

export interface ServerPorts {
  name: string
  ports: number[]
}

export interface ServerRanges {
  name: string
  ranges: PortRange[]
}

type DnatListen = Pick<DnatRuleData, 'protocol' | 'listen_port' | 'listen_port_end' | 'enabled'>

/** Диапазоны правил DNAT, которые отнимают порт у HAProxy: включённые и ловящие TCP */
export function dnatTcpRanges(rules: DnatListen[]): PortRange[] {
  return rules
    .filter(r => r.enabled && r.protocol !== 'udp' && r.listen_port > 0)
    .map(r => [r.listen_port, r.listen_port_end ?? r.listen_port])
}

export function rangesCover(ranges: PortRange[], port: number): boolean {
  return ranges.some(([low, high]) => low <= port && port <= high)
}

/** Серверы, где порты HAProxy попадают в диапазоны DNAT, — с перехваченными портами */
export function interceptedOnServers(servers: ServerPorts[], ranges: PortRange[]): ServerPorts[] {
  return servers
    .map(s => ({ name: s.name, ports: s.ports.filter(port => rangesCover(ranges, port)) }))
    .filter(s => s.ports.length > 0)
}

/** Серверы, где DNAT перехватывает порты правил HAProxy, — с этими портами */
export function portsInterceptedOnServers(servers: ServerRanges[], ports: number[]): ServerPorts[] {
  return servers
    .map(s => ({ name: s.name, ports: ports.filter(port => rangesCover(s.ranges, port)) }))
    .filter(s => s.ports.length > 0)
}

export function formatPortRanges(ranges: PortRange[]): string {
  return ranges.map(([low, high]) => (low === high ? String(low) : `${low}-${high}`)).join(', ')
}

/** «ams-1 (443), fra-2 (443, 8443)» */
export function formatServerPorts(servers: ServerPorts[]): string {
  return servers.map(s => `${s.name} (${s.ports.join(', ')})`).join(', ')
}
