import type { HAProxyIpOwners } from '../api/client'

export type NodeAddressKind = 'primary' | 'extra' | 'all'

export interface NodeAddresses {
  primary?: string
  extras: string[]
}

const IPV4_RE = /^\d{1,3}(\.\d{1,3}){3}$/

/**
 * Разворачивает карту «адрес → нода» в «нода → её адреса». У ноды с доменом в URL основных записей две:
 * домен и первый публичный IPv4 интерфейсов. Основным берётся IPv4, домен — только когда IPv4 у ноды не известен.
 */
export function groupAddressesByNode(owners: HAProxyIpOwners): Map<number, NodeAddresses> {
  const collected = new Map<number, { primaries: string[]; extras: { number: number; address: string }[] }>()
  for (const [address, { id, extra_number }] of Object.entries(owners)) {
    const node = collected.get(id) ?? { primaries: [], extras: [] }
    if (extra_number === null) node.primaries.push(address)
    else node.extras.push({ number: extra_number, address })
    collected.set(id, node)
  }

  const nodes = new Map<number, NodeAddresses>()
  for (const [id, { primaries, extras }] of collected) {
    nodes.set(id, {
      primary: primaries.find(address => IPV4_RE.test(address)) ?? primaries[0],
      extras: extras.sort((a, b) => a.number - b.number).map(extra => extra.address),
    })
  }
  return nodes
}

export function pickAddresses(node: NodeAddresses | undefined, kind: NodeAddressKind): string[] {
  if (!node) return []
  const primary = node.primary ? [node.primary] : []
  if (kind === 'primary') return primary
  if (kind === 'extra') return node.extras
  return [...primary, ...node.extras]
}
