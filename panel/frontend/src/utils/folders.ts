import { writeStorage } from './storage'

// Порядок папок общий для всей панели: его задают перетаскиванием на дашборде и в «Серверах»
const FOLDER_ORDER_KEY = 'dashboard_folder_order'

export function readFolderOrder(): string[] {
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(FOLDER_ORDER_KEY) || '[]')
    return Array.isArray(saved) ? saved : []
  } catch {
    return []
  }
}

export function saveFolderOrder(order: string[]) {
  writeStorage(FOLDER_ORDER_KEY, JSON.stringify(order))
}

/** Порядок папок как на дашборде: сохранённый пользователем, остальные — по алфавиту */
export function orderFolders(names: string[], order: string[] = readFolderOrder()): string[] {
  const ordered = order.filter(f => names.includes(f))
  const rest = names.filter(f => !order.includes(f)).sort()
  return [...ordered, ...rest]
}

/** Папки серверов плюс созданные, но ещё пустые — в пользовательском порядке */
export function collectFolders<T extends { folder?: string | null }>(
  items: T[],
  emptyFolders: string[],
  order: string[],
): string[] {
  const names = new Set<string>(emptyFolders)
  for (const item of items) if (item.folder) names.add(item.folder)
  return orderFolders([...names], order)
}

/** Раскладка по папкам с сохранением порядка внутри папки; ключ null — серверы без папки */
export function groupByFolder<T extends { folder?: string | null }>(items: T[]): Map<string | null, T[]> {
  const map = new Map<string | null, T[]>()
  for (const item of items) {
    const key = item.folder || null
    const bucket = map.get(key)
    if (bucket) bucket.push(item)
    else map.set(key, [item])
  }
  return map
}
