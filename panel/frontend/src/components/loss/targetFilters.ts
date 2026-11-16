import type { LossTarget } from '../../api/client'
import { LOSS_WARN_PCT } from '../ui/LossProbeBadge'
import { spreadLevel, type SpreadLevel } from './quarantineSpread'

// Группы кнопок-фильтров: по одной на то, что видно в строке таблицы
export type FacetKey = 'worst' | 'spread' | 'quarantine' | 'owner'
export type WorstState = 'no_reply' | 'lossy' | 'clean'
export type QuarantineState = 'disabled' | 'active'

export const FACETS: FacetKey[] = ['worst', 'spread', 'quarantine', 'owner']
export const UNKNOWN_OWNER = ''

const WORST_ORDER: WorstState[] = ['no_reply', 'lossy', 'clean']
const SPREAD_ORDER: SpreadLevel[] = ['all', 'most', 'limited']
const QUARANTINE_ORDER: QuarantineState[] = ['disabled', 'active']
const FIXED_ORDER: Record<Exclude<FacetKey, 'owner'>, string[]> = {
  worst: WORST_ORDER,
  spread: SPREAD_ORDER,
  quarantine: QUARANTINE_ORDER,
}

export type FilterSelection = Record<FacetKey, string[]>

export const NO_FILTERS: FilterSelection = { worst: [], spread: [], quarantine: [], owner: [] }

export interface TargetRow {
  target: LossTarget
  lossyRelays: number
  // null — признака у строки нет: у адреса без потерь нет ширины потерь
  traits: Record<FacetKey, string | null>
}

export interface FacetOption {
  value: string
  // Сколько адресов даст кнопка при уже выбранных фильтрах остальных групп
  count: number
  active: boolean
}

export function isLossy(lossPct: number): boolean {
  return lossPct > LOSS_WARN_PCT
}

function worstState(target: LossTarget): WorstState {
  if (target.relays.some(relay => relay.answers === false)) return 'no_reply'
  return isLossy(target.worst_loss) ? 'lossy' : 'clean'
}

export function describeTargets(targets: LossTarget[]): TargetRow[] {
  return targets.map(target => {
    const lossyRelays = target.relays.filter(relay => isLossy(relay.loss_pct)).length
    const quarantine: QuarantineState = target.quarantined.length > 0 ? 'disabled' : 'active'
    return {
      target,
      lossyRelays,
      traits: {
        worst: worstState(target),
        spread: lossyRelays > 0 ? spreadLevel({ failing: lossyRelays, total: target.relays.length }) : null,
        quarantine,
        owner: target.owner ?? UNKNOWN_OWNER,
      },
    }
  })
}

// Кнопки одной группы складываются, группы между собой — сужают
function matches(row: TargetRow, selection: FilterSelection, except?: FacetKey): boolean {
  return FACETS.every(facet => {
    const picked = selection[facet]
    if (facet === except || picked.length === 0) return true
    const trait = row.traits[facet]
    return trait !== null && picked.includes(trait)
  })
}

export function applyFilters(rows: TargetRow[], selection: FilterSelection): TargetRow[] {
  return rows.filter(row => matches(row, selection))
}

function countBy(rows: TargetRow[], facet: FacetKey): Map<string, number> {
  const counts = new Map<string, number>()
  for (const { traits } of rows) {
    const trait = traits[facet]
    if (trait !== null) counts.set(trait, (counts.get(trait) ?? 0) + 1)
  }
  return counts
}

function fixedOrder(order: string[]) {
  return (left: string, right: string) => order.indexOf(left) - order.indexOf(right)
}

// Ноды с большим числом адресов — первыми. Порядок считается по всему списку, а не по
// отобранному: иначе кнопки менялись бы местами от нажатия на соседнюю группу
function busiestFirst(offered: Map<string, number>) {
  return (left: string, right: string) =>
    (offered.get(right) ?? 0) - (offered.get(left) ?? 0) || left.localeCompare(right)
}

// Набор кнопок — всё, что есть в списке, плюс выбранное: нажатую кнопку должно быть чем отжать,
// даже когда её адреса из списка ушли
function optionsOf(rows: TargetRow[], selection: FilterSelection, facet: FacetKey): FacetOption[] {
  const offered = countBy(rows, facet)
  const reachable = countBy(rows.filter(row => matches(row, selection, facet)), facet)
  const picked = selection[facet]
  return [...new Set([...offered.keys(), ...picked])]
    .sort(facet === 'owner' ? busiestFirst(offered) : fixedOrder(FIXED_ORDER[facet]))
    .map(value => ({ value, count: reachable.get(value) ?? 0, active: picked.includes(value) }))
}

export function facetOptions(rows: TargetRow[], selection: FilterSelection): Record<FacetKey, FacetOption[]> {
  return {
    worst: optionsOf(rows, selection, 'worst'),
    spread: optionsOf(rows, selection, 'spread'),
    quarantine: optionsOf(rows, selection, 'quarantine'),
    owner: optionsOf(rows, selection, 'owner'),
  }
}

export function toggleFilter(selection: FilterSelection, facet: FacetKey, value: string): FilterSelection {
  const picked = selection[facet]
  return {
    ...selection,
    [facet]: picked.includes(value) ? picked.filter(item => item !== value) : [...picked, value],
  }
}
