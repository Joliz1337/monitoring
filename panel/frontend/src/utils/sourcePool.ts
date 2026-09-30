import type { SourcePoolAssignments, SourcePoolBinding, SourcePoolNodeView } from '../api/client'

// Совпадают с нодой; нужны, пока нода ещё не прислала своё состояние
const DEFAULT_MARK_BASE = 101
const DEFAULT_MARK_COUNT = 30

// Без amber/orange: жёлтым подсвечивается адрес, которого нет на интерфейсе
const ADDRESS_DOTS = [
  'bg-accent-400',
  'bg-emerald-400',
  'bg-violet-400',
  'bg-pink-400',
  'bg-blue-400',
  'bg-lime-400',
  'bg-fuchsia-400',
  'bg-teal-300',
]
const ABSENT_ADDRESS_DOT = 'bg-warning'

export function poolMarks(view: SourcePoolNodeView | null): number[] {
  const base = view?.mark_base ?? DEFAULT_MARK_BASE
  const count = view?.mark_count ?? DEFAULT_MARK_COUNT
  return Array.from({ length: count }, (_, index) => base + index)
}

export function addressDotClass(index: number | undefined): string {
  if (index === undefined) return ABSENT_ADDRESS_DOT
  return ADDRESS_DOTS[index % ADDRESS_DOTS.length]
}

export function marksByAddress(bindings: SourcePoolBinding[]): Map<string, number[]> {
  const grouped = new Map<string, number[]>()
  for (const { mark, address } of bindings) {
    grouped.set(address, [...(grouped.get(address) ?? []), mark])
  }
  return grouped
}

// 101, 102, 103, 110 → «101–103, 110»
export function formatMarks(marks: number[]): string {
  const sorted = [...marks].sort((a, b) => a - b)
  const parts: string[] = []
  let start = sorted[0]
  let prev = sorted[0]
  for (const mark of [...sorted.slice(1), NaN]) {
    if (mark === prev + 1) {
      prev = mark
      continue
    }
    if (start !== undefined) parts.push(start === prev ? `${start}` : `${start}–${prev}`)
    start = mark
    prev = mark
  }
  return parts.join(', ')
}

export function roundRobinAssignments(marks: number[], addresses: string[]): SourcePoolAssignments {
  if (addresses.length === 0) return {}
  return Object.fromEntries(marks.map((mark, index) => [String(mark), addresses[index % addresses.length]]))
}

export function bindingsAsAssignments(bindings: SourcePoolBinding[]): SourcePoolAssignments {
  return Object.fromEntries(bindings.map(({ mark, address }) => [String(mark), address]))
}

// Ключи приходят в разном порядке — сравнение не должно от него зависеть
export function assignmentsKey(assignments: SourcePoolAssignments): string {
  return JSON.stringify(Object.entries(assignments).sort(([a], [b]) => Number(a) - Number(b)))
}
