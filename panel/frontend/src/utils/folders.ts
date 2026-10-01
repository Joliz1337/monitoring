/** Порядок папок как на дашборде: сохранённый пользователем, остальные — по алфавиту */
export function orderFolders(names: string[]): string[] {
  try {
    const saved: string[] = JSON.parse(localStorage.getItem('dashboard_folder_order') || '[]')
    const ordered = saved.filter(f => names.includes(f))
    const rest = names.filter(f => !saved.includes(f)).sort()
    return [...ordered, ...rest]
  } catch {
    return [...names].sort()
  }
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
