import type { BackendServer } from '../api/client'

// Вес на лету меняют только динамические алгоритмы HAProxy: static-rr принимает
// лишь 0 и 100%, а hash-алгоритмы при смене веса перекидывают уже распределённых клиентов
export const DYNAMIC_ALGORITHMS: ReadonlySet<string> = new Set(['roundrobin', 'leastconn', 'random'])

// Та же шкала и пределы, что у расчёта панели (haproxy_load_weights.py, models.py)
const MAX_WEIGHT = 100
const LINK_CAPACITY_MIN_MBIT = 10
const LINK_CAPACITY_MAX_MBIT = 400_000

export interface BackendServerGroup {
  serverId: number
  addresses: string[]
}

const takesPart = (srv: BackendServer) => !srv.backup && !srv.disabled && srv.address.trim() !== ''
const roundHalfUp = (value: number) => Math.floor(value + 0.5)

/** Пустое поле — канал не задан; иначе целое в допустимых пределах или null для мусора */
export function parseCapacity(value: string | undefined): number | null {
  const text = (value ?? '').trim()
  if (!text) return null
  const mbit = Number(text)
  return Number.isInteger(mbit) && mbit >= LINK_CAPACITY_MIN_MBIT && mbit <= LINK_CAPACITY_MAX_MBIT ? mbit : null
}

export const isCapacityInvalid = (value: string | undefined) => (value ?? '').trim() !== '' && parseCapacity(value) === null

/** Строки бэкенда по серверам панели: основной и доп. IP одного сервера — одна группа */
export function groupBackendsByServer(
  servers: BackendServer[],
  ownerOf: (address: string) => number | undefined,
): { groups: BackendServerGroup[]; foreign: string[] } {
  const groups = new Map<number, string[]>()
  const foreign: string[] = []
  for (const srv of servers.filter(takesPart)) {
    const address = srv.address.trim()
    const serverId = ownerOf(address)
    const bucket = serverId === undefined ? foreign : (groups.get(serverId) ?? [])
    if (!bucket.includes(address)) bucket.push(address)
    if (serverId !== undefined) groups.set(serverId, bucket)
  }
  return { groups: [...groups].map(([serverId, addresses]) => ({ serverId, addresses })), foreign }
}

/**
 * Запасные веса по каналам — тот же счёт, что у панели без нагрузки: доля по каналу,
 * вес сервера делится между его адресами (копии одного адреса:порта — одна цель).
 * Адреса не из панели, как и у панели, сохраняют свой вес.
 * null — у сервера панели в правиле не указан канал или серверов панели нет.
 */
export function capacityWeights(
  servers: BackendServer[],
  ownerOf: (address: string) => number | undefined,
  capacityOf: (serverId: number) => number | null | undefined,
): BackendServer[] | null {
  const capacities = new Map<number, number>()
  const targets = new Map<number, Set<string>>()
  for (const srv of servers.filter(takesPart)) {
    const serverId = ownerOf(srv.address.trim())
    if (serverId === undefined) continue
    const capacity = capacityOf(serverId)
    if (!capacity) return null
    capacities.set(serverId, capacity)
    const set = targets.get(serverId) ?? new Set<string>()
    set.add(`${srv.address.trim().toLowerCase()}:${srv.port}`)
    targets.set(serverId, set)
  }
  if (capacities.size === 0) return null

  const maxCapacity = Math.max(...capacities.values())
  return servers.map(srv => {
    const serverId = takesPart(srv) ? ownerOf(srv.address.trim()) : undefined
    if (serverId === undefined) return srv
    const nodeWeight = roundHalfUp(MAX_WEIGHT * capacities.get(serverId)! / maxCapacity)
    return { ...srv, weight: Math.max(1, roundHalfUp(nodeWeight / targets.get(serverId)!.size)) }
  })
}
