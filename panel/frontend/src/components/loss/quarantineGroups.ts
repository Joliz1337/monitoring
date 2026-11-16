import type { QuarantineItem, QuarantineOverview } from '../../api/client'

export interface BackendAddress {
  ip: string
  port: number
}

// Без порта — адрес целиком, на всех портах
export interface ExemptAddress {
  ip: string
  port: number | null
}

export interface AddressGroup {
  // `ip:порт`, а когда порты не различаются — сам IP; он же подпись строки
  key: string
  ip: string
  ports: number[]
  owner: string | null
  items: QuarantineItem[]
  chronicStrikes?: number
}

export interface NodeGroup {
  owner: string | null
  addresses: AddressGroup[]
}

type AddressRef = Pick<QuarantineItem, 'target' | 'ip' | 'port' | 'owner'>

function groupByAddress({ items, chronic }: QuarantineOverview, mergePorts: boolean): AddressGroup[] {
  const groups = new Map<string, AddressGroup>()
  const groupOf = ({ target, ip, port, owner }: AddressRef): AddressGroup => {
    const key = mergePorts ? ip : target
    const group = groups.get(key) ?? { key, ip, ports: [], owner, items: [] }
    groups.set(key, group)
    if (!group.ports.includes(port)) group.ports.push(port)
    return group
  }

  for (const item of items) groupOf(item).items.push(item)
  // Адрес с постоянными проблемами показывается и когда он сейчас в работе — строкой без релеев.
  // У склеенного IP — худший счёт из его портов
  for (const address of chronic) {
    const group = groupOf(address)
    group.chronicStrikes = Math.max(group.chronicStrikes ?? 0, address.strikes)
  }
  for (const group of groups.values()) group.ports.sort((a, b) => a - b)
  return [...groups.values()]
}

const REFUSED_FIRST = 0
const QUARANTINED_NEXT = 1
const IN_SERVICE_LAST = 2

function attentionRank({ items }: AddressGroup): number {
  if (items.some(item => item.state === 'blocked')) return REFUSED_FIRST
  return items.length > 0 ? QUARANTINED_NEXT : IN_SERVICE_LAST
}

// Карточка ноды показывает только первые адреса, поэтому выше идёт то, что требует внимания:
// адрес не отвечает, а релей отключить его отказался. Внутри ранга порядок прежний — по адресу
function attentionFirst(addresses: AddressGroup[]): AddressGroup[] {
  const ranks = new Map(addresses.map(address => [address, attentionRank(address)]))
  return [...addresses].sort((left, right) => ranks.get(left)! - ranks.get(right)!)
}

export function groupByNode(view: QuarantineOverview, mergePorts: boolean): NodeGroup[] {
  const nodes = new Map<string | null, AddressGroup[]>()
  for (const address of groupByAddress(view, mergePorts)) {
    const addresses = nodes.get(address.owner) ?? []
    nodes.set(address.owner, addresses)
    addresses.push(address)
  }
  return [...nodes].map(([owner, addresses]) => ({ owner, addresses: attentionFirst(addresses) }))
}

// Релеи адреса с одним состоянием и одной причиной идут одной строкой
export function groupBySituation(items: QuarantineItem[]): QuarantineItem[][] {
  const situations = new Map<string, QuarantineItem[]>()
  for (const item of items) {
    const key = `${item.state}|${item.reason.kind}|${item.blocked_reason}|${item.partial_reason}`
    const situation = situations.get(key)
    if (situation) situation.push(item)
    else situations.set(key, [item])
  }
  return [...situations.values()]
}

export function releasable(items: QuarantineItem[]): QuarantineItem[] {
  return items.filter(item => item.state === 'disabled' || item.state === 'pending')
}

// Без различия портов один релей приходит несколькими записями — по одной на порт
export function relayNames(items: QuarantineItem[]): string[] {
  const names = new Map<number, string>()
  for (const item of items) names.set(item.server_id, item.server_name)
  return [...names.values()]
}
