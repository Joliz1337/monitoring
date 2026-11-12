import type { ServerWithMetrics } from '../stores/serversStore'

/** IP сетевых карт из списка серверов: основной, доп. адреса, вторые карты.
 *  Петлю, link-local и виртуальные карты (docker, veth, wg) отсеивает бэкенд, когда
 *  собирает срез ответа ноды: по ним поиск находил бы весь парк */
function interfaceAddresses(server: ServerWithMetrics): string[] {
  return (server.metrics?.network.interfaces ?? [])
    .flatMap(iface => iface.addresses)
    .map(entry => entry.address.toLowerCase())
}

/** query — уже в нижнем регистре и без пробелов по краям */
export function matchesServerSearch(server: ServerWithMetrics, query: string): boolean {
  return server.name.toLowerCase().includes(query)
    || server.url.toLowerCase().includes(query)
    || interfaceAddresses(server).some(address => address.includes(query))
}
