import type { LossTarget } from '../../api/client'
import { LOSS_WARN_PCT } from '../ui/LossProbeBadge'

// Столбцы таблицы адресов, по которым её упорядочивает нажатие на заголовок
export type SortKey = 'address' | 'worst' | 'relays' | 'episode'

export interface TargetSort {
  key: SortKey
  descending: boolean
}

export interface TargetRow {
  target: LossTarget
  lossyRelays: number
}

export function isLossy(lossPct: number): boolean {
  return lossPct > LOSS_WARN_PCT
}

export function describeTargets(targets: LossTarget[]): TargetRow[] {
  return targets.map(target => ({
    target,
    lossyRelays: target.relays.filter(relay => isLossy(relay.loss_pct)).length,
  }))
}

// Адрес сравнивается по числам: «10.0.0.3» раньше «10.0.0.20», а по буквам вышло бы наоборот
function addressParts({ ip, port }: LossTarget): number[] {
  const octets = ip.split('.').map(Number)
  const isIpv4 = octets.length === 4 && octets.every(Number.isInteger)
  return [...(isIpv4 ? octets : []), port]
}

function compareAddress(left: LossTarget, right: LossTarget): number {
  const leftParts = addressParts(left)
  const rightParts = addressParts(right)
  // Не IPv4 — после всех IPv4, между собой по тексту
  if (leftParts.length !== rightParts.length) return rightParts.length - leftParts.length
  for (let index = 0; index < leftParts.length; index += 1) {
    if (leftParts[index] !== rightParts[index]) return leftParts[index] - rightParts[index]
  }
  return left.ip.localeCompare(right.ip)
}

const NO_EPISODE = -1

const COMPARE: Record<SortKey, (left: TargetRow, right: TargetRow) => number> = {
  address: (left, right) => compareAddress(left.target, right.target),
  worst: (left, right) => left.target.worst_loss - right.target.worst_loss,
  relays: (left, right) =>
    left.lossyRelays - right.lossyRelays || left.target.relays.length - right.target.relays.length,
  episode: (left, right) =>
    (left.target.episode?.level ?? NO_EPISODE) - (right.target.episode?.level ?? NO_EPISODE),
}

// Первое нажатие ставит вперёд то, ради чего столбец открывают: адреса — по возрастанию,
// потери, релеи и алерты — худшее сверху
const DESCENDING_FIRST: Record<SortKey, boolean> = { address: false, worst: true, relays: true, episode: true }

export function nextSort(current: TargetSort | null, key: SortKey): TargetSort {
  if (current?.key === key) return { key, descending: !current.descending }
  return { key, descending: DESCENDING_FIRST[key] }
}

/** Пока столбец не выбран, порядок остаётся таким, каким его отдала панель: худшие сверху.
 *  Равные по столбцу строки свой порядок сохраняют */
export function sortTargets(rows: TargetRow[], sort: TargetSort | null): TargetRow[] {
  if (sort === null) return rows
  const direction = sort.descending ? -1 : 1
  return [...rows].sort((left, right) => direction * COMPARE[sort.key](left, right))
}
