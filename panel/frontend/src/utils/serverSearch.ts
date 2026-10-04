import type { ServerWithMetrics } from '../stores/serversStore'

// Петля и link-local есть на каждом сервере — по ним поиск находил бы весь парк
const NON_SEARCHABLE_PREFIXES = ['127.', '169.254.', '::1', 'fe80:']

/** IP сетевых карт из последних метрик: основной, доп. адреса, вторые карты.
 *  Виртуальные карты (docker, veth, wg) пропускаются — их адреса повторяются на многих нодах */
function interfaceAddresses(server: ServerWithMetrics): string[] {
  return (server.metrics?.network?.interfaces ?? [])
    .filter(iface => !iface.is_virtual)
    .flatMap(iface => iface.addresses ?? [])
    .map(entry => entry.address.toLowerCase())
    .filter(address => !NON_SEARCHABLE_PREFIXES.some(prefix => address.startsWith(prefix)))
}

/** query — уже в нижнем регистре и без пробелов по краям */
export function matchesServerSearch(server: ServerWithMetrics, query: string): boolean {
  return server.name.toLowerCase().includes(query)
    || server.url.toLowerCase().includes(query)
    || interfaceAddresses(server).some(address => address.includes(query))
}
